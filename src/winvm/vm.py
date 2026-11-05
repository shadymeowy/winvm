"""Instances: a working disk on top of an image, a QEMU process, SSH access, host mounts.

  ${data_dir}/instances/<name>/   disk.qcow2 (the instance's own changes), OVMF_VARS.fd, instance.json
  $XDG_RUNTIME_DIR/winvm-*/<name>/ while it runs: qemu.pid, vm.json (ports, RAM), QMP/QGA
                                  sockets, serial.log, ssh_config + shared connection, smb/
"""
from __future__ import annotations

import base64
import json
import os
import pwd
import shutil
import signal
import socket
import subprocess as sp
import sys
import time
from pathlib import Path

from . import guest, host
from .balloon import ram_range
from .config import Config
from .store import Store, label
from .util import Chan, check_mount_points, die, log, meminfo, mib, mount_point, run, warn

HYPERV = ("hv-relaxed,hv-vapic,hv-spinlocks=0x1fff,hv-vpindex,hv-runtime,hv-time,hv-synic,"
          "hv-stimer,hv-frequencies,hv-tlbflush,hv-ipi,hv-reset")
SMB_HOST = "10.0.2.4"           # where the guest reaches the instance's Samba server
LINKS = r"C:\ProgramData\winvm\mount-links.txt"   # in the guest: the folder links winvm made


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

    # --- instance.json: the image it was created on, that image's file, its own settings
    @property
    def meta(self) -> dict:
        try:
            return json.loads((self.home / "instance.json").read_text())
        except (OSError, ValueError):
            return {}

    @meta.setter
    def meta(self, value: dict) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "instance.json").write_text(json.dumps(value, indent=1) + "\n")

    @property
    def image(self) -> str:
        return self.meta.get("image") or "base"

    @property
    def settings(self) -> dict:
        """ram/cpus/mounts: machine defaults < its image's `run:` < its own (up --ram/--cpus)."""
        if self.builder:
            return self.cfg.run_settings(None)
        return self.cfg.run_settings(self.image, self.meta.get("run"))

    def layer_meta(self) -> dict:
        """Metadata of the image layer the disk sits on (guest user/password live there)."""
        store = Store(self.cfg)
        return store.meta(store.parent(self.disk)) if self.disk.exists() else {}

    # --- disk
    def create(self, image: str, on: Path, run: dict | None = None) -> None:
        """Fresh, empty disk on layer `on` (a version of `image`) + fresh UEFI variables;
        `run`: the instance's own settings (kept from before if None)."""
        Store(self.cfg).overlay(self.disk, on)
        shutil.copyfile(host.firmware(self.cfg)[1], self.vars)
        if not self.builder:
            src = self.cfg.sources.get(image)
            self.meta = {"image": image, "source": str(src) if src else None,
                         "run": self.meta.get("run", {}) if run is None else run}
        log(f"fresh disk for {self.name} on {label(on)}")

    def require_stopped(self) -> None:
        if self.alive:
            die(f"{self.name} is running - winvm -i {self.name} stop first")

    # --- QEMU command line
    def qemu_argv(self, *, install=False, gui=False, cdroms=(), disks=(), st: dict | None = None) -> list[str]:
        """install: offline build VM (no NIC, own QMP socket); cdroms: the first one boots
        (the disk is still empty); disks: extra (path, readonly) disks after the main one."""
        st, cfg = st or self.state, self.cfg
        code = host.firmware(cfg)[0]
        a = [host.qemu(cfg), "-name", self.qemu_name, "-enable-kvm",
             "-uuid", cfg.s["uuid"],
             # q35 + host CPU with Hyper-V enlightenments: what Windows runs fastest on
             "-machine", "q35,hpet=off,vmport=off", "-cpu", f"host,{HYPERV}",
             "-smp", self.settings["cpus"], "-m", f"{st['ram']}M",
             "-rtc", "base=utc,driftfix=slew", "-global", "kvm-pit.lost_tick_policy=delay",
             "-global", "ICH9-LPC.disable_s3=1", "-global", "ICH9-LPC.disable_s4=1",
             # UEFI: shared read-only firmware + this instance's variable store
             "-drive", f"if=pflash,format=raw,readonly=on,file={qfile(code)}",
             "-drive", f"if=pflash,format=raw,file={qfile(self.vars)}",
             # disk on virtio-scsi; discard passes guest TRIM down so qcow2 files shrink.
             # cache=unsafe while installing: a host crash means reinstalling anyway
             "-device", "virtio-scsi-pci,id=scsi0",
             "-drive", f"if=none,id=disk0,file={qfile(self.disk)},format=qcow2,discard=unmap,detect-zeroes=unmap,"
                       f"cache={'unsafe' if install else 'writeback'}",
             "-device", "scsi-hd,drive=disk0,bus=scsi0.0,scsi-id=0,rotation_rate=1,bootindex=0",
             # paravirtual devices: balloon (give RAM back), rng, absolute mouse, keyboard
             "-device", "virtio-balloon-pci,id=balloon0", "-device", "virtio-rng-pci",
             "-device", "virtio-tablet-pci", "-device", "virtio-keyboard-pci",
             # guest agent + control sockets
             "-device", "virtio-serial-pci",
             "-chardev", f"socket,id=qga0,path={self.dir}/qga.sock,server=on,wait=off",
             "-device", "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
             "-qmp", f"unix:{self.dir}/qmp.sock,server=on,wait=off",
             *(["-qmp", f"unix:{self.dir}/qmp-balloon.sock,server=on,wait=off"] if self.auto_ram else []),
             "-monitor", f"unix:{self.dir}/monitor.sock,server=on,wait=off",
             "-vga", "std", "-serial", f"file:{self.dir}/serial.log"]
        for i, (path, ro) in enumerate(disks, 1):                # Windows numbers them in this order
            a += ["-drive", f"if=none,id=disk{i},file={qfile(path)},format=qcow2,readonly={'on' if ro else 'off'}",
                  "-device", f"scsi-hd,drive=disk{i},bus=scsi0.0,scsi-id={i}"]
        if install:
            # no NIC while installing: nothing phones home and Windows knows it is offline;
            # a second QMP socket for the build's keypresses and shutdown events
            a += ["-nic", "none", "-qmp", f"unix:{self.dir}/qmp-build.sock,server=on,wait=off"]
        else:
            # user-mode network: localhost-only port forwards; Samba for mounts (see write_smb)
            net = f"user,id=net0,hostname={self.name},hostfwd=tcp:127.0.0.1:{st['ssh']}-:22"
            if st.get("rdp"):
                net += f",hostfwd=tcp:127.0.0.1:{st['rdp']}-:3389"
            if (smb := self.dir / "smb").is_dir():
                cmd = f"{smbd()} -l {smb} -s {smb}/smb.conf".replace(",", ",,")   # ',' is QEMU's separator
                net += f",guestfwd=tcp:{SMB_HOST}:445-cmd:{cmd}"
            a += ["-netdev", net, "-device", "virtio-net-pci,netdev=net0,mac=52:54:00:57:53:4c"]
        for i, iso in enumerate(cdroms):
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
            host.require(self.cfg, gui)
            self.claim(self.ram)
            self.write_smb({} if self.builder else self.settings["mounts"])
            self.launch(self.qemu_argv(gui=gui), gui)
            self.ssh_config()                                  # for `Include .../ssh_config`
            st = self.state
            log(f"started {self.name} ssh=127.0.0.1:{st['ssh']}"
                + (f" rdp=127.0.0.1:{st['rdp']}" if st.get("rdp") else "")
                + (f" vnc=127.0.0.1:{st['vnc']}" if st.get("vnc") else ""))
        if wait:
            self.wait_ssh()
            if not self.builder:
                self.sync_keys()
                self.sync_mounts()
                self.auto_memory()

    def stop(self, timeout=120) -> bool:
        """ACPI shutdown, a hard kill after `timeout` s. False if it had to be killed."""
        return self.wait_stopped(timeout) if self.power_button() else True

    def power_button(self) -> bool:
        """Press the ACPI power button; False if it was not running."""
        if not self.alive:
            log(f"{self.name} not running")
            return False
        self.close_ssh()
        Chan(self.dir / "qmp.sock", qmp=True)("system_powerdown")
        return True

    def wait_stopped(self, timeout=120, since: float | None = None) -> bool:
        """After power_button: wait until it is off, kill it after `timeout` s (counted
        from `since`). False if it had to be killed."""
        end = (since or time.time()) + timeout
        while time.time() < end:
            if not self.alive:
                log(f"stopped {self.name}")
                return True
            time.sleep(0.5)
        warn(f"{self.name} did not power off in {timeout} s - killing it")
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
        user = self.layer_meta().get("user") or self.cfg.builtin["user"]
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
            if self.auto_ram and not self.balloon_pid:          # its helper died: restart it
                self.auto_memory()
        return self

    # --- dynamic memory (ram: {min, max}, see balloon.py)
    @property
    def auto_ram(self) -> tuple[int, int] | None:
        return None if self.builder else ram_range(self.settings)

    @property
    def ram(self) -> int:
        """MiB the VM gets (with dynamic memory: its max)."""
        rng = ram_range(self.settings)
        return rng[1] if rng else mib(self.settings["ram"])

    @property
    def balloon_pid(self) -> int | None:
        try:
            pid = int((self.dir / "balloon.pid").read_text())
            return pid if b"winvm.balloon" in Path(f"/proc/{pid}/cmdline").read_bytes() else None
        except (OSError, ValueError):
            return None

    def auto_memory(self) -> None:
        """Dynamic memory: a pagefile in the guest (absorbs spikes; set once, one reboot) and
        the balloon helper process."""
        if not self.auto_ram or not (self.dir / "qmp-balloon.sock").is_socket():
            if self.auto_ram:
                warn(f"{self.name} was started without dynamic memory - restart it to enable it")
            return
        mm = r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management"
        r = guest.capture(self.ssh, f"""if (Get-CimInstance Win32_PageFileUsage) {{ 'ok' }} else {{
    reg add "{mm}" /v PagingFiles /t REG_MULTI_SZ /d "C:\\pagefile.sys 1024 4096" /f | Out-Null; 'set' }}""")
        if r.stdout.strip().endswith("set"):
            log(f"dynamic memory: adding a pagefile to {self.name} (once, one reboot)")
            guest.capture(self.ssh, "shutdown.exe /r /t 0")
            self.close_ssh()
            for _ in range(60):                                # until it is really gone
                if sp.run(self.ssh("-o", "ConnectTimeout=2") + ["exit 0"], stdin=sp.DEVNULL,
                          stdout=sp.DEVNULL, stderr=sp.DEVNULL).returncode:
                    break
                time.sleep(1)
            self.close_ssh()
            self.wait_ssh()
            self.sync_mounts()
        if self.balloon_pid:
            return
        env = {**os.environ, "WINVM_DATA_DIR": str(self.cfg.data), "WINVM_CACHE_DIR": str(self.cfg.cache),
               **({"WINVM_CONFIG": str(self.cfg.file)} if self.cfg.file else {})}
        with open(self.dir / "balloon.log", "a") as logf:
            sp.Popen([sys.executable, "-m", "winvm.balloon", self.name], env=env, stdin=sp.DEVNULL,
                     stdout=logf, stderr=sp.STDOUT, start_new_session=True)
        lo, hi = self.auto_ram
        log(f"dynamic memory: {self.name} uses {lo}-{hi} MiB as needed (log: {self.dir}/balloon.log)")

    # --- mounts: each instance has its own Samba server, reached by the guest at
    # \\10.0.2.4 through QEMU (guestfwd starts smbd per connection, as QEMU's smb= does).
    # Its config can change while the guest runs: a new share works at once. A removed or
    # changed one stays served by the running smbd (a reload signal does not drop it), so
    # that smbd is ended: Windows reconnects by itself and the new smbd reads the new
    # config. The guest sees each mount as a folder link, C:\mnt\<name> or where its `at`
    # says - drive letters would exist only in the logon session that mapped them. The
    # guest keeps a list of the links winvm made (LINKS), so it can remove them later;
    # anything else at a mount point is never touched.
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
                warn(f"smbd not found, so no mounts (C:\\mnt) - {host.install_hint('samba')}")
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

    def sync_mounts(self) -> list[str]:
        """Make the guest's folder links match the mounts. Returns the mount points left
        without a link: taken by something else (a folder, a file, another link) or failed."""
        want = "; ".join(f"{guest.quote(mount_point(n, m))} = {guest.quote(f'//{SMB_HOST}/{n}'.replace('/', chr(92)))}"
                         for n, m in self.mounts.items())
        r = guest.capture(self.ssh, f"""$want = @{{ {want} }}
$list = '{LINKS}'
function ours($i) {{ $i -and $i.LinkType -and "$($i.Target)" -like '*{SMB_HOST}\\*' }}
New-Item -ItemType Directory -Force C:\\mnt, (Split-Path $list) | Out-Null
$old = @(Get-Content -LiteralPath $list -ErrorAction SilentlyContinue) + @(Get-ChildItem -LiteralPath C:\\mnt -Force | ForEach-Object FullName)
foreach ($p in $old) {{                         # links no longer wanted, or pointing elsewhere
    $i = Get-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
    if ((ours $i) -and -not ($want.ContainsKey($p) -and "$($i.Target)" -like "*$($want[$p].TrimStart('\\'))")) {{ $i.Delete() }}
}}
foreach ($p in $want.Keys) {{
    $i = Get-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
    if ($i) {{ if (-not (ours $i)) {{ "TAKEN $p" }}; continue }}
    try {{
        $d = Split-Path $p                         # (C:\\ for C:\\x: there already)
        if (-not (Test-Path -LiteralPath $d)) {{ New-Item -ItemType Directory -Force $d -ErrorAction Stop | Out-Null }}
        # mklink, not New-Item: a share just added may take a moment to be seen
        $out = cmd /c mklink /D "$p" "$($want[$p])" 2>&1
        if ($LASTEXITCODE) {{ "FAILED $p - $out" }}
    }} catch {{ "FAILED $p - $($_.Exception.Message)" }}
}}
Set-Content -LiteralPath $list -Value @($want.Keys)
$end = (Get-Date).AddSeconds(15)                 # until the shares answer
while (@($want.Values | Where-Object {{ -not (Test-Path -LiteralPath $_) }}).Count -and (Get-Date) -lt $end) {{
    Start-Sleep -Milliseconds 500
}}
# images from before winvm 2 link C:\\host to QEMU's old fixed share
$h = Get-Item -LiteralPath C:\\host -Force -ErrorAction SilentlyContinue
if ($h.LinkType -and "$($h.Target)" -like '*10.0.2.4\\qemu*') {{ $h.Delete() }}
""")
        missing = []
        for ln in r.stdout.splitlines():
            if ln.startswith("TAKEN "):
                missing.append(ln[6:])
                warn(f"{ln[6:]} already exists in {self.name} and is not a winvm mount - left alone, not mounted there")
            elif ln.startswith("FAILED "):
                missing.append(ln[7:].split(" - ", 1)[0])
                warn(f"could not link {ln[7:]}")
        if r.returncode:
            warn(f"could not set up the mounts in {self.name}: {r.stdout.strip()[:300]}")
        return missing

    def sync_keys(self) -> None:
        """Bring the guest's authorized keys up to date: winvm's own key and settings
        ssh_pubkeys as they are now (not as they were when the image was built)."""
        keys = base64.b64encode(self.cfg.authorized_keys().replace("\n", "\r\n").encode()).decode()
        r = guest.capture(self.ssh, f"""$ak = 'C:\\ProgramData\\ssh\\administrators_authorized_keys'
$want = [Text.Encoding]::ASCII.GetString([Convert]::FromBase64String('{keys}'))
if ((Test-Path $ak) -and [IO.File]::ReadAllText($ak) -ne $want) {{ [IO.File]::WriteAllText($ak, $want) }}""")
        if r.returncode:
            warn(f"could not update the SSH keys in {self.name}: {r.stdout.strip()[:200]}")

    def require_running(self) -> None:
        if not self.alive:
            die(f"{self.name} is not running - start it, or list the mount in its image's "
                f"`run: mounts:` (or the machine's, ~/.config/winvm/winvm.yaml)")

    def mount(self, name: str, path: Path, readonly=False, at: str | None = None) -> str:
        """Share host folder `path` as `name`, linked at `at` (default C:\\mnt\\NAME).
        Returns the mount point."""
        self.require_running()
        if not (self.dir / "smb").is_dir():
            die(f"no Samba on this instance - {host.install_hint('samba')}, then restart it")
        mounts, spot = self.mounts, mount_point(name, {"at": at})
        if name in mounts and mount_point(name, mounts[name]).lower() != spot.lower():
            die(f"mount '{name}' is already at {mount_point(name, mounts[name])} - umount it first, "
                f"or name this one: winvm mount PATH NAME")
        new = {**mounts, name: {"path": str(path), "readonly": readonly, **({"at": at} if at else {})}}
        check_mount_points(new, "mount")
        self.up()
        r = guest.capture(self.ssh, f"""if (-not (Test-Path -LiteralPath {guest.quote(spot[:3])})) {{ exit 4 }}
$i = Get-Item -LiteralPath {guest.quote(spot)} -Force -ErrorAction SilentlyContinue
if ($i -and -not ($i.LinkType -and "$($i.Target)" -like '*{SMB_HOST}\\*')) {{ exit 3 }}""")
        if r.returncode == 4:
            die(f"{self.name} has no drive {spot[:2]}")
        if r.returncode == 3:
            die(f"{spot} already exists in {self.name} and is not a winvm mount - pick another place")
        self.write_smb(new)
        if name in mounts:
            self.restart_smb()
        if spot in self.sync_mounts():                         # undo: no half-made mount
            self.write_smb(mounts)
            self.restart_smb()
            self.sync_mounts()
            die(f"could not mount at {spot}")
        return spot

    def umount(self, which: str) -> str:
        """Remove a mount, by name or by mount point. Returns its name."""
        self.require_running()
        mounts = self.mounts
        name = which if which in mounts else \
            next((n for n, m in mounts.items() if mount_point(n, m).lower() == which.replace("/", "\\").rstrip("\\").lower()), None)
        if not name:
            die(f"{self.name} has no mount '{which}' (`winvm -i {self.name} mount` lists them)")
        del mounts[name]
        self.write_smb(mounts)
        self.restart_smb()
        self.up().sync_mounts()
        return name

    def guest_path(self, host: Path) -> str | None:
        """Guest path of a host path inside one of the mounts (longest match wins)."""
        best, host = None, host.resolve()
        for name, m in self.mounts.items():
            root = Path(m["path"]).resolve()
            if host.is_relative_to(root) and (best is None or len(root.parts) > best[0]):
                rel = host.relative_to(root).parts
                best = (len(root.parts), mount_point(name, m) + "".join(f"\\{p}" for p in rel))
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
