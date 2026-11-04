"""Instances: a working disk on top of an image, a QEMU process, SSH access, host mounts.

  ${data_dir}/instances/<name>/   disk.qcow2 (the instance's own changes), OVMF_VARS.fd, instance.json
  $XDG_RUNTIME_DIR/winvm-*/<name>/ while it runs: qemu.pid, vm.json (ports, RAM), QMP/QGA
                                  sockets, serial.log, ssh_config + shared connection, smb/
"""
from __future__ import annotations

import json
import os
import pwd
import shutil
import signal
import socket
import subprocess as sp
import time
from pathlib import Path

from . import guest
from .config import Config
from .store import Store, label
from .util import Chan, die, log, meminfo, mib, need, run, warn

HYPERV = ("hv-relaxed,hv-vapic,hv-spinlocks=0x1fff,hv-vpindex,hv-runtime,hv-time,hv-synic,"
          "hv-stimer,hv-frequencies,hv-tlbflush,hv-ipi,hv-reset")
OVMF_PAIRS = [  # (code, vars) of non-secure-boot builds, per distro
    ("/usr/share/edk2/ovmf/OVMF_CODE.fd", "/usr/share/edk2/ovmf/OVMF_VARS.fd"),
    ("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"),
    ("/usr/share/OVMF/OVMF_CODE.fd", "/usr/share/OVMF/OVMF_VARS.fd"),
    ("/usr/share/edk2/x64/OVMF_CODE.4m.fd", "/usr/share/edk2/x64/OVMF_VARS.4m.fd"),
    ("/usr/share/edk2-ovmf/x64/OVMF_CODE.fd", "/usr/share/edk2-ovmf/x64/OVMF_VARS.fd"),
]
SMB_HOST = "10.0.2.4"           # where the guest reaches the instance's Samba server


def ovmf(cfg: Config) -> tuple[str, str]:
    """UEFI firmware: (code, empty variable store)."""
    code, vars_ = cfg.s.get("ovmf_code"), cfg.s.get("ovmf_vars")
    if code or vars_:
        if not (code and vars_):
            die("set both settings.ovmf_code and settings.ovmf_vars (or neither: autodetect)")
        return str(cfg.path(code)), str(cfg.path(vars_))
    for pair in OVMF_PAIRS:
        if all(Path(p).exists() for p in pair):
            return pair
    die("OVMF firmware not found (install edk2-ovmf, or set settings.ovmf_code/ovmf_vars)")


def qfile(path) -> str:
    """A path inside a QEMU option string (',' separates options there)."""
    return str(path).replace(",", ",,")


def smbd() -> str | None:
    return shutil.which("smbd") or ("/usr/sbin/smbd" if Path("/usr/sbin/smbd").exists() else None)


class Instance:
    def __init__(self, cfg: Config, name: str, builder=False):
        self.cfg, self.name, self.builder = cfg, name, builder
        # builders (image builds) are scratch VMs: their disk lives in the cache
        self.home = (cfg.build if builder else cfg.data / "instances") / name
        self.disk = self.home / "disk.qcow2"
        self.vars = self.home / "OVMF_VARS.fd"
        self.dir = cfg.runtime / name
        self.pidfile = self.dir / "qemu.pid"
        self.qemu_name = f"winvm-{name},process=winvm-{name}"  # QEMU's -name: identifies its process

    # --- state: pid from QEMU's pidfile; ports/RAM chosen at start in vm.json
    @property
    def pid(self) -> int | None:
        try:
            pid = int(self.pidfile.read_text())
            # still this QEMU? (the pidfile of a killed QEMU may name another process by now)
            cmdline = Path(f"/proc/{pid}/cmdline").read_text().split("\0")
        except (OSError, ValueError):
            return None
        return pid if self.qemu_name in cmdline else None

    @property
    def alive(self) -> bool:
        return self.pid is not None

    @property
    def state(self) -> dict:
        try:
            return json.loads((self.dir / "vm.json").read_text())
        except (OSError, ValueError):
            return {}

    @state.setter
    def state(self, value: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "vm.json").write_text(json.dumps(value, indent=1) + "\n")

    @property
    def rss(self) -> int:
        """Resident memory of the QEMU process in MiB."""
        try:
            return int(Path(f"/proc/{self.pid}/status").read_text().split("VmRSS:")[1].split()[0]) // 1024
        except (OSError, IndexError, TypeError):
            return 0

    @property
    def settings(self) -> dict:
        return self.cfg.instance(self.name)

    @property
    def image(self) -> str:
        """The image this instance was created on (instance.json), else the configured one."""
        try:
            return json.loads((self.home / "instance.json").read_text())["image"]
        except (OSError, ValueError, KeyError):
            return self.settings["image"]

    def layer_meta(self) -> dict:
        """Metadata of the image layer the disk sits on (guest user/password live there)."""
        store = Store(self.cfg)
        return store.meta(store.parent(self.disk)) if self.disk.exists() else {}

    # --- disk
    def create(self, image: str, on: Path) -> None:
        """Fresh, empty disk on layer `on` (a version of `image`) + fresh UEFI variables."""
        Store(self.cfg).overlay(self.disk, on)
        shutil.copyfile(ovmf(self.cfg)[1], self.vars)
        if not self.builder:
            (self.home / "instance.json").write_text(json.dumps({"image": image}) + "\n")
        log(f"fresh disk for {self.name} on {label(on)}")

    def require_stopped(self) -> None:
        if self.alive:
            die(f"{self.name} is running - winvm -i {self.name} stop first")

    # --- QEMU command line
    def qemu_argv(self, *, install=False, gui=False, cdroms=(), st: dict | None = None) -> list[str]:
        st, cfg = st or self.state, self.cfg
        a = [cfg.s["qemu"], "-name", self.qemu_name, "-enable-kvm",
             "-uuid", cfg.s["uuid"],
             # q35 + host CPU with Hyper-V enlightenments: what Windows runs fastest on
             "-machine", "q35,hpet=off,vmport=off", "-cpu", f"host,{HYPERV}",
             "-smp", self.settings["cpus"], "-m", f"{st['ram']}M",
             "-rtc", "base=utc,driftfix=slew", "-global", "kvm-pit.lost_tick_policy=delay",
             "-global", "ICH9-LPC.disable_s3=1", "-global", "ICH9-LPC.disable_s4=1",
             # UEFI: shared read-only firmware + this instance's variable store
             "-drive", f"if=pflash,format=raw,readonly=on,file={qfile(ovmf(cfg)[0])}",
             "-drive", f"if=pflash,format=raw,file={qfile(self.vars)}",
             # disk on virtio-scsi; discard passes guest TRIM down so qcow2 files shrink.
             # cache=unsafe while installing: a host crash means reinstalling anyway
             "-device", "virtio-scsi-pci,id=scsi0",
             "-drive", f"if=none,id=disk0,file={qfile(self.disk)},format=qcow2,discard=unmap,detect-zeroes=unmap,"
                       f"cache={'unsafe' if install else 'writeback'}",
             "-device", "scsi-hd,drive=disk0,bus=scsi0.0,rotation_rate=1,bootindex=0",
             # paravirtual devices: balloon (give RAM back), rng, absolute mouse, keyboard
             "-device", "virtio-balloon-pci", "-device", "virtio-rng-pci",
             "-device", "virtio-tablet-pci", "-device", "virtio-keyboard-pci",
             # guest agent + control sockets
             "-device", "virtio-serial-pci",
             "-chardev", f"socket,id=qga0,path={self.dir}/qga.sock,server=on,wait=off",
             "-device", "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
             "-qmp", f"unix:{self.dir}/qmp.sock,server=on,wait=off",
             "-monitor", f"unix:{self.dir}/monitor.sock,server=on,wait=off",
             "-vga", "std", "-serial", f"file:{self.dir}/serial.log"]
        if install:
            # no NIC while installing: nothing phones home and Windows knows it is offline
            a += ["-nic", "none"]
        else:
            # user-mode network: localhost-only port forwards; Samba for mounts (see write_smb)
            net = f"user,id=net0,hostname={self.name},hostfwd=tcp:127.0.0.1:{st['ssh']}-:22"
            if st.get("rdp"):
                net += f",hostfwd=tcp:127.0.0.1:{st['rdp']}-:3389"
            if (smb := self.dir / "smb").is_dir():
                cmd = f"{smbd()} -l {smb} -s {smb}/smb.conf".replace(",", ",,")   # ',' is QEMU's separator
                net += f",guestfwd=tcp:{SMB_HOST}:445-cmd:{cmd}"
            a += ["-netdev", net, "-device", "virtio-net-pci,netdev=net0,mac=52:54:00:57:53:4c"]
        for i, iso in enumerate(cdroms):                       # the first CD boots (disk is still empty)
            a += ["-drive", f"if=none,id=cd{i},media=cdrom,readonly=on,file={qfile(iso)}",
                  "-device", f"ide-cd,drive=cd{i},bus=ide.{i}" + (",bootindex=1" if i == 0 else "")]
        if gui:
            a += ["-display", "gtk,zoom-to-fit=on",           # + clipboard sharing via spice-vdagent
                  "-chardev", "qemu-vdagent,id=vdagent0,clipboard=on,mouse=off",
                  "-device", "virtserialport,chardev=vdagent0,name=com.redhat.spice.0"]
        else:
            a += ["-display", "none"] + (["-vnc", f"127.0.0.1:{st['vnc'] - 5900}"] if st.get("vnc") else [])
        return [str(x) for x in a]

    # --- lifecycle
    def claim(self, ram: int) -> None:
        """Pick a port slot and record RAM/ports for this run."""
        need_mem(self.cfg, ram, f"{'build VM' if self.builder else 'instance'} {self.name}")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state = {"ram": ram, **ports(self.cfg, free_slot(self.cfg, self.state.get("slot")))}

    def launch(self, argv: list[str], gui=False) -> None:
        """Start QEMU in the background and wait for its pidfile."""
        argv = [*argv, "-pidfile", str(self.pidfile)]
        if gui:
            with open(self.dir / "qemu.log", "w") as logf:
                sp.Popen(argv, stdout=logf, stderr=sp.STDOUT, start_new_session=True)
        else:
            run(*argv, "-daemonize")                            # its errors go to the terminal
        for _ in range(50):
            if self.alive:
                break
            time.sleep(0.1)
        else:
            die("QEMU failed to start" + (f" (see {self.dir}/qemu.log)" if gui else ""))

    def start(self, gui=False, wait=True) -> None:
        """Boot the instance (its disk must exist); wait for SSH and set up the mounts."""
        if self.alive:
            log(f"{self.name} already running (pid {self.pid})")
        else:
            need(self.cfg.s["qemu"])
            self.claim(mib(self.settings["ram"]))
            self.write_smb({} if self.builder else self.cfg.mounts(self.name))
            self.launch(self.qemu_argv(gui=gui), gui)
            self.ssh_config()                                  # for `Include .../ssh_config`
            st = self.state
            log(f"started {self.name} ssh=127.0.0.1:{st['ssh']}"
                + (f" rdp=127.0.0.1:{st['rdp']}" if st.get("rdp") else "")
                + (f" vnc=127.0.0.1:{st['vnc']}" if st.get("vnc") else ""))
        if wait:
            self.wait_ssh()
            if not self.builder:
                self.sync_mounts()

    def stop(self, timeout=120) -> bool:
        """ACPI shutdown, a hard kill after `timeout` s. False if it had to be killed."""
        if not self.alive:
            log(f"{self.name} not running")
            return True
        self.close_ssh()
        Chan(self.dir / "qmp.sock", qmp=True)("system_powerdown")      # ACPI power button
        for _ in range(timeout):
            if not self.alive:
                log(f"stopped {self.name}")
                return True
            time.sleep(1)
        warn(f"{self.name} did not power off in time - killing")
        self.kill()
        return False

    def kill(self) -> bool:
        """Power off at once; False if it was not running."""
        self.close_ssh()
        if not (pid := self.pid):
            return False
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if not self.alive:
                    break
                time.sleep(0.1)
            else:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:                             # gone in the meantime
            pass
        self.pidfile.unlink(missing_ok=True)
        return True

    # --- SSH
    def ssh_config(self) -> Path:
        st = self.state or die(f"{self.name} has never been started")
        user = self.layer_meta().get("user") or self.cfg.image_defaults.get("user") or "user"
        cfg = self.dir / "ssh_config"
        cfg.write_text(f"""Host {self.name}
  HostName 127.0.0.1
  Port {st['ssh']}
  User {user}
  IdentityFile "{self.cfg.key}"
  IdentitiesOnly yes
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  LogLevel ERROR
  ConnectTimeout 5
  ServerAliveInterval 10
  ControlMaster auto
  ControlPath "{self.dir}/mux"
  ControlPersist 120
""")
        return cfg

    def ssh(self, *opts: str) -> list[str]:
        """ssh argv up to and including the host name."""
        cfg = self.dir / "ssh_config"
        return ["ssh", "-F", str(cfg if cfg.exists() else self.ssh_config()), *opts, self.name]

    def close_ssh(self) -> None:
        """End the shared SSH connection (it would outlive a stopped guest for a while)."""
        if (self.dir / "mux").exists():
            sp.run(self.ssh("-O", "exit"), stdout=sp.DEVNULL, stderr=sp.DEVNULL)

    def wait_ssh(self, timeout=600) -> None:
        if (self.dir / "mux").exists() and not sp.run(   # shared connection up: nothing to wait for
                self.ssh("-O", "check"), stdout=sp.DEVNULL, stderr=sp.DEVNULL).returncode:
            return
        end = time.time() + timeout
        while sp.run(self.ssh("-o", "BatchMode=yes", "-o", "ConnectTimeout=3") + ["exit 0"],
                     stdin=sp.DEVNULL, stdout=sp.DEVNULL, stderr=sp.DEVNULL).returncode:
            if not self.alive:
                die(f"{self.name} is not running")
            if time.time() > end:
                die(f"timeout waiting for SSH on {self.name}")
            time.sleep(2)

    def up(self) -> Instance:
        """Make sure the instance runs and SSH answers (for sh/cp/ssh)."""
        if not self.alive:
            if not self.disk.exists():
                die(f"{self.name} does not exist yet - winvm up {self.name}")
            self.start()
        else:
            self.wait_ssh()
        return self

    # --- mounts: each instance has its own Samba server, reached by the guest at
    # \\10.0.2.4 through QEMU (guestfwd starts smbd per connection, as QEMU's smb= does).
    # Its config can change while the guest runs: a new share works at once. A removed or
    # changed one stays served by the running smbd (a reload signal does not drop it), so
    # that smbd is ended: Windows reconnects by itself and the new smbd reads the new
    # config. The guest sees each mount as a folder link C:\mnt\<name> - drive letters
    # would exist only in the logon session that mapped them.
    @property
    def mounts(self) -> dict[str, dict]:
        try:
            return json.loads((self.dir / "mounts.json").read_text())
        except (OSError, ValueError):
            return {}

    def write_smb(self, mounts: dict[str, dict]) -> None:
        d = self.dir / "smb"
        if not smbd():
            if mounts:
                warn("smbd not found (install samba) - no mounts")
            shutil.rmtree(d, ignore_errors=True)
            (self.dir / "mounts.json").unlink(missing_ok=True)
            return
        d.mkdir(parents=True, exist_ok=True)
        for m in mounts.values():
            Path(m["path"]).mkdir(parents=True, exist_ok=True)
        conf = "".join(f"{k}={v}\n" for k, v in {
            "private dir": d, "interfaces": "127.0.0.1", "bind interfaces only": "yes",
            "pid directory": d, "lock directory": d, "state directory": d, "cache directory": d,
            "ncalrpc dir": f"{d}/ncalrpc", "log file": f"{d}/log.smbd", "smb passwd file": f"{d}/smbpasswd",
            "security": "user", "map to guest": "Bad User", "load printers": "no", "printing": "bsd",
            "disable spoolss": "yes", "usershare max shares": "0"}.items())
        user = pwd.getpwuid(os.getuid()).pw_name            # the guest acts as you on the host
        for name, m in mounts.items():
            conf += (f"\n[{name}]\npath={m['path']}\nread only={'yes' if m['readonly'] else 'no'}\n"
                     f"guest ok=yes\nforce user={user}\n")
        (d / "smb.conf").write_text("[global]\n" + conf)
        (self.dir / "mounts.json").write_text(json.dumps(mounts, indent=1) + "\n")

    def restart_smb(self) -> None:
        """End this instance's smbd (matched by its config path, not QEMU's command line)."""
        conf = f"-s {self.dir}/smb/smb.conf"
        for p in Path("/proc").glob("[0-9]*"):
            try:
                if (p / "comm").read_text().strip() == "smbd" and conf in (p / "cmdline").read_text().replace("\0", " "):
                    os.kill(int(p.name), signal.SIGTERM)
            except OSError:
                pass

    def sync_mounts(self) -> None:
        """Make C:\\mnt in the guest match the mounts: one folder link per share."""
        want = "; ".join(f"{guest.quote(n)} = {guest.quote(f'//{SMB_HOST}/{n}'.replace('/', chr(92)))}"
                         for n in self.mounts)
        r = guest.capture(self.ssh, f"""$want = @{{ {want} }}
New-Item -ItemType Directory -Force C:\\mnt | Out-Null
Get-ChildItem -LiteralPath C:\\mnt -Force | Where-Object {{ $_.LinkType -and -not $want.ContainsKey($_.Name) }} |
    ForEach-Object {{ $_.Delete() }}
foreach ($n in $want.Keys) {{
    if (-not (Get-Item -LiteralPath "C:\\mnt\\$n" -Force -ErrorAction SilentlyContinue)) {{
        New-Item -ItemType SymbolicLink -Path "C:\\mnt\\$n" -Target $want[$n] | Out-Null }}
}}
# images from before winvm 2 link C:\\host to QEMU's old fixed share
$h = Get-Item -LiteralPath C:\\host -Force -ErrorAction SilentlyContinue
if ($h.LinkType -and "$($h.Target)" -like '*10.0.2.4\\qemu*') {{ $h.Delete() }}
""")
        if r.returncode:
            warn(f"could not set up C:\\mnt in {self.name}: {r.stdout.strip()[:300]}")

    def require_running(self) -> None:
        if not self.alive:
            die(f"{self.name} is not running - start it, or change the mounts in the config "
                f"(instances.{self.name}.mounts)")

    def mount(self, name: str, path: Path, readonly=False) -> None:
        self.require_running()
        if not (self.dir / "smb").is_dir():
            die("no Samba on this instance (install samba, then restart it)")
        mounts = self.mounts
        changed = name in mounts
        mounts[name] = {"path": str(path), "readonly": readonly}
        self.write_smb(mounts)
        if changed:
            self.restart_smb()
        self.up().sync_mounts()

    def umount(self, name: str) -> None:
        self.require_running()
        mounts = self.mounts
        if name not in mounts:
            die(f"{self.name} has no mount '{name}' (`winvm -i {self.name} mount` lists them)")
        del mounts[name]
        self.write_smb(mounts)
        self.restart_smb()
        self.up().sync_mounts()

    def guest_path(self, host: Path) -> str | None:
        """Guest path of a host path inside one of the mounts (longest match wins)."""
        best, host = None, host.resolve()
        for name, m in self.mounts.items():
            root = Path(m["path"]).resolve()
            if host.is_relative_to(root) and (best is None or len(root.parts) > best[0]):
                rel = host.relative_to(root).parts
                best = (len(root.parts), f"C:\\mnt\\{name}" + "".join(f"\\{p}" for p in rel))
        return best and best[1]


# ------------------------------------------------------------------ host resources

def instances(cfg: Config) -> list[Instance]:
    """Every instance with a run folder (running or not), builders included."""
    return [Instance(cfg, d.name, builder=(cfg.build / d.name).is_dir())
            for d in sorted(cfg.runtime.glob("*")) if d.is_dir()]


def running(cfg: Config) -> list[Instance]:
    return [vm for vm in instances(cfg) if vm.alive]


def need_mem(cfg: Config, want: int, what: str) -> None:
    """Refuse to start something the host can't hold. Running VMs count with the RAM
    they may still grow into, not just what they touched so far."""
    growth = sum(max(0, vm.state.get("ram", 0) - vm.rss) for vm in running(cfg))
    have = meminfo("MemAvailable") - growth
    reserve = mib(cfg.s.get("mem_reserve") or 0)
    if have < want + reserve:
        die(f"{what} needs ~{want} MiB RAM (+{reserve} reserve) but only {have} MiB is free "
            f"for it - stop an instance or lower its RAM")


def ports(cfg: Config, slot: int) -> dict:
    """Host ports of port slot n: the configured ports + n (None = forward off)."""
    p = cfg.s.get("ports") or {}
    if not p.get("ssh"):
        die("settings.ports.ssh must be set: winvm talks to the guest over SSH")
    if p.get("vnc") and int(p["vnc"]) < 5900:
        die("settings.ports.vnc must be 5900 or higher (VNC display = port - 5900)")
    return {"slot": slot, **{k: int(p[k]) + slot if p.get(k) else None for k in ("ssh", "rdp", "vnc")}}


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def free_slot(cfg: Config, prefer: int | None = None) -> int:
    """Lowest slot whose ports are all free (preferring the one used last time)."""
    taken = {vm.state.get("slot") for vm in running(cfg)}

    def ok(n: int) -> bool:
        return n not in taken and all(port_free(v) for k, v in ports(cfg, n).items() if k != "slot" and v)

    if prefer is not None and ok(prefer):
        return prefer
    slot = next((n for n in range(100) if ok(n)), None)
    return die("no free port slot") if slot is None else slot
