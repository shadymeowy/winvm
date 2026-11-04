"""Building images.

ISO images (offline, unattended), from Microsoft's own, unmodified ISO:
  1. a small payload CD: autounattend.xml, winslim/ (pe.cmd + lists + unattend.xml),
     the first-boot payload and the image's `setup` steps (written in pure Python)
  2. a VM without network boots the Windows ISO (Enter is pressed for "Press any key
     to boot from CD"), with the payload and virtio-win CDs. Setup finds autounattend.xml
     and runs start.cmd -> pe.cmd: apply Windows, debloat it offline (data/guest/lists),
     inject drivers, reboot. Windows' own setup then runs the payload: guest tools,
     OpenSSH, your setup steps, cleanup; one more boot, power off
  3. the disk becomes a compressed read-only layer
  With `slim: true` the debloated image is captured once onto a disk kept in
  cache/slim; later builds apply it directly and skip the debloat.

Images `from` another image: boot a scratch VM on the image below (with network), run
the steps over SSH, shut it down and keep its changes as a compressed layer.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import socket
import subprocess as sp
import threading
import time
from pathlib import Path
from xml.sax.saxutils import escape

from . import __version__, guest, host
from .config import GUEST, Config
from .media import check_edition, write_iso
from .store import Store, label
from .util import Fatal, die, dos, log, run, warn
from .vm import Instance, running

# virtio driver folders injected into Windows (w11/amd64 flavour)
VIRTIO_DRIVERS = "vioscsi viostor NetKVM Balloon vioserial viorng vioinput pvpanic fwcfg viogpudo".split()
CAPTURE_GB = 30                 # size of the slim-capture disk; pe.cmd recognizes it by this
WORK = r"C:\winvm-build"        # work folder of `from` builds: downloads, copies, cwd of run steps
BOM = b"\xef\xbb\xbf"             # Windows PowerShell reads .ps1 files without it as ANSI, not UTF-8


def build(cfg: Config, names: list[str], force=False, gui=False) -> None:
    """Build whatever `names` need, bottom-up; up-to-date images are skipped. Asks once
    for the downloads that takes (cfg.yes: don't ask)."""
    store = Store(cfg)
    order: list[str] = []
    for n in names:
        order += [x for x in cfg.chain(n) if x not in order]

    def pending(n: str) -> bool:
        try:
            return force and n in names or not store.current(n)
        except Fatal:                                          # e.g. no ISO yet: must be built
            return True

    todo = [n for n in order if pending(n)]
    if not todo:
        return
    host.qemu(cfg)                                             # the host can run it at all?
    host.qemu_img(cfg)                                         # (before downloading gigabytes)
    if problem := host.kvm_problem():
        die(problem)
    items = host.downloads_for(cfg, todo)
    host.consent(items, cfg.yes)
    host.fetch_all(cfg, items)
    cfg.forget()                                               # an ISO may have arrived
    for n in order:
        cur = store.current(n)
        if cur and not (force and n in names):
            continue
        if cur and (users := store.users(cur)):
            die(f"{label(cur)} is in use by {', '.join(users)} - rm/reset those to rebuild it")
        t0 = time.time()
        (build_overlay if "from" in cfg.image(n) else build_iso)(cfg, n, gui)
        log(f"built {label(store.require(n))} in {(time.time() - t0) / 60:.0f} min")


def tools(cfg: Config) -> dict[str, Path]:
    """The pinned virtio-win ISO and OpenSSH MSI (downloaded by build() already)."""
    return {k: host.fetch(p["url"], p["dest"], p["sha256"], p.get("size"))
            for k in ("virtio", "openssh") for p in [host.pinned(cfg, k)]}


def fetch_step(cfg: Config, st: dict) -> Path:
    d = st["download"]
    return host.fetch(d["url"], cfg.cache / "downloads" / d["sha256"][:16] / d["name"], d["sha256"])


# ------------------------------------------------------------------ ISO images

def build_iso(cfg: Config, name: str, gui=False) -> None:
    img, h, store = cfg.image(name), cfg.hash(name), Store(cfg)
    check_names(cfg, name)
    iso = cfg.iso(name)
    check_edition(iso, img.get("edition"), img.get("index"))
    inst = Instance(cfg, f"_install-{name}", builder=True)
    inst.require_stopped()
    shutil.rmtree(inst.home, ignore_errors=True)
    inst.home.mkdir(parents=True)
    t = tools(cfg)
    disks = []
    if img.get("slim"):
        slim = slim_image(cfg, name, inst, t)
        log(f"applying the cached slim image {slim.name}")
        disks = [(slim, True)]
    payload = make_payload(cfg, name, inst.home, t, "install", preserviced=bool(disks))

    run(host.qemu_img(cfg), "create", "-q", "-f", "qcow2", inst.disk, img["disk_size"])
    log(f"installing Windows for image '{name}' (unattended, offline)")
    serial, _ = run_installer(inst, [iso, payload, t["virtio"]], disks, gui)
    if "WINSLIM-FAIL" in serial:
        die(f"install failed - see {inst.dir}/serial.log (also C:\\Windows\\Setup on {inst.disk})")
    if "WINSLIM-DONE" not in serial:
        die(f"install did not finish; disk kept at {inst.disk}. If it stopped in WinPE (nothing after "
            f"`installing`), WINVM_DEBUG=1 winvm build {name} keeps the error on screen "
            f"(winvm -i _install-{name} screenshot)")
    log(f"writing image layer {name}@{h[:12]}")
    store.add(name, h, inst.disk, None, user=img["user"], password=img["password"],
              source=str(cfg.sources[name]))
    shutil.rmtree(inst.home)
    shutil.rmtree(inst.dir, ignore_errors=True)


def check_names(cfg: Config, name: str) -> None:
    """Values Windows Setup would stop at with an error dialog (the build would wait forever)."""
    img = cfg.image(name)
    host_name, user = str(img["hostname"]), str(img["user"])
    if not re.fullmatch(r"[A-Za-z0-9-]{1,15}", host_name) or host_name.isdigit():
        die(f"image '{name}': hostname '{host_name}' - Windows takes 1-15 letters, digits and -, "
            f"not only digits")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,20}", user) or user.endswith("."):
        die(f"image '{name}': user '{user}' - winvm takes 1-20 letters, digits and . _ -, not ending in .")


def slim_image(cfg: Config, name: str, inst: Instance, t: dict) -> Path:
    """A disk holding the captured, debloated image (winslim-image\\install.wim) for this
    ISO/edition/debloat set, made once and kept in cache/slim."""
    disk = cfg.cache / "slim" / f"{cfg.slim_key(name)[:16]}.qcow2"
    disk.parent.mkdir(parents=True, exist_ok=True)
    with open(disk.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)                       # data dirs share it: one maker at a time
        return disk if disk.exists() else make_slim(cfg, name, inst, t, disk)


def make_slim(cfg: Config, name: str, inst: Instance, t: dict, disk: Path) -> Path:
    if running(cfg):
        warn("other VMs are running; the capture VM needs its RAM too")
    out, target = disk.with_suffix(".part"), inst.home / "capture.qcow2"
    for f in (out, target):
        run(host.qemu_img(cfg), "create", "-q", "-f", "qcow2", f, f"{CAPTURE_GB if f == out else 64}G")
    payload = make_payload(cfg, name, inst.home, t, "capture")
    log("slim image: apply + offline debloat + capture (once; the install follows)")
    cap = Instance(cfg, inst.name, builder=True)
    cap.disk = target
    _, reason = run_installer(cap, [cfg.iso(name), payload, t["virtio"]], [(out, False)], no_reboot=True)
    if reason != "guest-reset":                                # pe.cmd reboots on success
        out.unlink(missing_ok=True)
        die(f"capture failed (VM ended with {reason or 'no event'}); "
            f"WINVM_DEBUG=1 winvm build {name} keeps the error on screen")
    out.replace(disk)
    target.unlink()
    payload.unlink()
    return disk


def list_file(name: str) -> str:
    """A data/guest/lists/*.txt file without comments and blank lines."""
    lines = (ln.rstrip() for ln in (GUEST / "lists" / f"{name}.txt").read_text().splitlines())
    return "".join(f"{ln}\n" for ln in lines if ln.strip() and not ln.lstrip().startswith("#"))


def ps_bool(v) -> str:
    return "$true" if v else "$false"


def make_payload(cfg: Config, name: str, work: Path, t: dict, mode: str, preserviced=False) -> Path:
    """The payload CD: autounattend.xml + winslim/ (pe.cmd, lists, config, unattend.xml,
    first-boot payload, setup steps)."""
    img = cfg.image(name)
    debloat = img.get("debloat") or {}
    tree = work / "payload"
    shutil.rmtree(tree, ignore_errors=True)
    ws, pl = tree / "winslim", tree / "winslim/payload"
    pl.mkdir(parents=True)
    (tree / "autounattend.xml").write_bytes(dos((GUEST / "autounattend.xml").read_text()))
    for f in ("pe.cmd", "start.cmd"):
        (ws / f).write_bytes(dos((GUEST / f).read_text()))
    for f in ("appx-keep", "capabilities-remove", "features-remove", "features-keep-payload",
              "services-disable", "offline-delete"):
        (ws / f"{f}.txt").write_bytes(dos(list_file(f)))
    (ws / "offline-reg.txt").write_bytes(dos((GUEST / "lists/offline-reg.txt").read_text()))
    knobs = {"WIN_EDITION": img.get("edition") or "",
             "WIN_INDEX": 1 if preserviced else img.get("index") or "",
             "WIN_STRIP_PAYLOADS": int(bool(debloat.get("strip_payloads"))),
             "WIN_COMPACT": int(bool(debloat.get("compact_os"))),
             "WINSLIM_MODE": mode, "WIN_PRESERVICED": int(preserviced),
             "VIRTIO_DRIVERS": " ".join(VIRTIO_DRIVERS), "CAPTURE_GB": CAPTURE_GB,
             "WINSLIM_DEBUG": int(os.environ.get("WINVM_DEBUG") == "1"),
             "WINSLIM_BUILD": f"winvm-{__version__}-{name}-{time.strftime('%Y%m%d')}"}
    (ws / "config.cmd").write_bytes(dos("".join(f'set "{k}={v}"\n' for k, v in knobs.items())))

    xml = {"WIN_USER": img["user"], "WIN_PASSWORD": img["password"], "WIN_HOSTNAME": img["hostname"],
           "WIN_TIMEZONE": img["timezone"], "WIN_LOCALE": img["locale"], "WIN_KEYBOARD": img["keyboard"]}
    unattend = (GUEST / "unattend.xml.in").read_text()
    for k, v in xml.items():
        unattend = unattend.replace(f"@{k}@", escape(str(v), {'"': "&quot;", "'": "&apos;"}))
    (ws / "unattend.xml").write_bytes(dos(unattend))

    for f in ("SetupComplete.cmd", "setupcomplete.ps1", "firstlogon.ps1", "final.ps1", "common.ps1"):
        (pl / f).write_bytes(dos((GUEST / "payload" / f).read_text()))
    for f in ("appx-keep", "services-disable"):
        (pl / f"{f}.txt").write_bytes(dos(list_file(f)))
    (pl / "settings.ps1").write_bytes(BOM + dos(
        f"$WinUser = {guest.quote(img['user'])}\n$WinCompact = {ps_bool(debloat.get('compact_os'))}\n"
        f"$RemoveWebView = {ps_bool(debloat.get('remove_webview'))}\n"))
    shutil.copy(t["openssh"], pl / "OpenSSH.msi")              # virtio guest tools: pe.cmd takes them off the CD
    (pl / "authorized_keys").write_bytes(dos(cfg.authorized_keys()))
    if mode == "install":                                      # a capture stops before the payload
        stage_setup(cfg, name, pl / "extra")
    out = write_iso(tree, work / f"payload-{mode}.iso", "WINVM")
    shutil.rmtree(tree)
    return out


def stage_setup(cfg: Config, name: str, extra: Path) -> None:
    """`setup` steps -> payload/extra: setup.ps1, which setupcomplete.ps1 runs as SYSTEM after
    the debloat (a failing step fails the build), and the files the steps use in extra/files,
    where the steps run."""
    steps = cfg.steps(name)
    if not steps:
        return
    files = extra / "files"
    files.mkdir(parents=True)
    ps = ["$ErrorActionPreference = 'Stop'", "Set-Location -LiteralPath (Join-Path $PSScriptRoot files)"]
    for i, st in enumerate(steps, 1):
        if "run" not in st:
            src = fetch_step(cfg, st) if "download" in st else Path(st["copy"][0])
            dst = files / (st["download"]["name"] if "download" in st else src.name)
            if dst.exists():
                die(f"image '{name}' setup step {i}: a file named {dst.name} is already staged")
            (shutil.copytree if src.is_dir() else shutil.copy)(src, dst)
        else:
            ps += [f"# step {i}", "$global:LASTEXITCODE = 0", st["run"],
                   f"if ($LASTEXITCODE) {{ throw \"setup step {i} exited with $LASTEXITCODE\" }}"]
    (extra / "setup.ps1").write_bytes(BOM + dos("\n".join(ps)))
    log(f"setup: {len(steps)} step(s) staged")


def run_installer(inst: Instance, cdroms: list[Path], disks=(), gui=False, no_reboot=False) -> tuple[str, str]:
    """Boot an offline installer VM, press Enter for the CD's "Press any key", stream its
    [SC]/[FL]/[WIN] progress lines from the serial log until QEMU exits. Returns the whole
    log and why the VM stopped (QMP SHUTDOWN reason, e.g. guest-shutdown / guest-reset)."""
    shutil.copyfile(host.firmware(inst.cfg)[1], inst.vars)
    inst.claim(inst.ram)
    serial = inst.dir / "serial.log"
    serial.write_bytes(b"")
    if not gui and inst.state.get("vnc"):
        log(f"watch it: vncviewer 127.0.0.1:{inst.state['vnc']}  (serial log: {serial})")
    t0 = time.time()
    argv = [*inst.qemu_argv(install=True, gui=gui, cdroms=cdroms, disks=disks), "-pidfile", str(inst.pidfile)]
    qemu = sp.Popen(argv + (["-no-reboot"] if no_reboot else []))
    reason: list[str] = []
    watcher = threading.Thread(target=watch_qmp, args=(inst.dir / "qmp-build.sock", qemu, reason), daemon=True)
    watcher.start()
    buf = b""
    try:
        with serial.open("rb") as f:
            while True:
                done = qemu.poll() is not None                 # read once more after QEMU exits
                buf += f.read()
                *lines, buf = buf.split(b"\n")
                for ln in lines:
                    if ln[:4] in (b"[SC]", b"[FL]", b"[WIN"):
                        print(ln.decode(errors="replace").rstrip("\r"), flush=True)
                if done:
                    break
                time.sleep(0.5)
    finally:
        if qemu.poll() is None:
            qemu.terminate()
            qemu.wait()
        inst.pidfile.unlink(missing_ok=True)
    watcher.join(timeout=10)                                   # it may not have read SHUTDOWN yet
    log(f"installer VM finished after {(time.time() - t0) / 60:.0f} min")
    return serial.read_text(errors="replace"), (reason[-1] if reason else "")


def watch_qmp(sock: Path, qemu: sp.Popen, reason: list[str]) -> None:
    """The installer VM's own QMP socket: press Enter for ~12 s (Microsoft's ISO asks to
    "Press any key to boot from CD"), then record SHUTDOWN reasons until QEMU exits."""
    for _ in range(300):                                       # QEMU may take a while to start
        if qemu.poll() is not None:
            return
        try:
            s = socket.socket(socket.AF_UNIX)
            s.connect(str(sock))
            break
        except OSError:
            s.close()
            time.sleep(0.1)
    else:
        warn("could not reach the installer VM's QMP socket - press Enter in its window yourself")
        return
    f = s.makefile("rw", encoding="utf-8")

    def send(cmd: str, args: dict | None = None) -> None:
        f.write(json.dumps({"execute": cmd, **({"arguments": args} if args else {})}) + "\n")
        f.flush()

    try:
        f.readline()
        send("qmp_capabilities")
        end = time.time() + 12
        while time.time() < end:
            send("send-key", {"keys": [{"type": "qcode", "data": "ret"}]})
            time.sleep(0.5)
        for line in f:
            msg = json.loads(line)
            if msg.get("event") == "SHUTDOWN":
                reason.append(msg.get("data", {}).get("reason", ""))
    except (OSError, ValueError):
        pass


# ------------------------------------------------------------------ images built on images

def build_overlay(cfg: Config, name: str, gui=False) -> None:
    img, h, store = cfg.image(name), cfg.hash(name), Store(cfg)
    below = store.require(img["from"])
    steps = cfg.steps(name)
    for st in steps:                                           # fetch first: fail before booting
        if "download" in st:
            fetch_step(cfg, st)
    inst = Instance(cfg, f"_build-{name}", builder=True)
    inst.require_stopped()
    shutil.rmtree(inst.home, ignore_errors=True)
    inst.home.mkdir(parents=True)
    inst.create(name, below)
    try:
        log(f"building image '{name}' on {label(below)}: {len(steps)} step(s)")
        inst.start(gui=gui)
        call(inst, f"New-Item -ItemType Directory -Force {guest.quote(WORK)} | Out-Null")
        for i, st in enumerate(steps, 1):
            kind, arg = next(iter(st.items()))
            log(f"step {i}/{len(steps)}: {describe(st)}")
            if kind == "run":
                rc = guest.pipe(inst.ssh, guest.script(arg, cwd=WORK, strict=True), stdin=sp.DEVNULL)
                if rc:
                    die(f"image '{name}' step {i} failed (exit {rc})")
            elif kind == "download":
                scp(inst, fetch_step(cfg, st), f"{WORK}\\{arg['name']}")
            elif kind == "copy":
                src = Path(arg[0])
                dst = arg[1] if len(arg) > 1 else f"{WORK}\\{src.name}"
                # the parent must exist for scp (a drive root like C:\ always does)
                call(inst, f"$p = Split-Path {guest.quote(dst)}; if ($p -and -not (Test-Path -LiteralPath $p)) "
                           "{ New-Item -ItemType Directory -Force -Path $p | Out-Null }")
                scp(inst, src, dst)
            elif kind == "restart":
                stop_clean(inst)
                inst.start(gui=gui)
        # leave nothing behind, and hand freed blocks back so the layer stays small
        call(inst, f"Remove-Item -LiteralPath {guest.quote(WORK)} -Recurse -Force; "
                   "Optimize-Volume -DriveLetter C -ReTrim -ErrorAction SilentlyContinue | Out-Null")
        stop_clean(inst)
        log(f"writing image layer {name}@{h[:12]}")
        meta = store.meta(below)
        store.add(name, h, inst.disk, below, user=meta.get("user"), password=meta.get("password"),
                  source=str(cfg.sources[name]))
    finally:
        if inst.alive:
            inst.kill()
        shutil.rmtree(inst.home, ignore_errors=True)
        shutil.rmtree(inst.dir, ignore_errors=True)


def stop_clean(inst: Instance) -> None:
    """Shut the build VM down; a disk it had to be killed on is not worth keeping."""
    if not inst.stop():
        die(f"{inst.name} did not shut down by itself - build aborted")


def describe(st: dict) -> str:
    """One log line for a step."""
    kind, arg = next(iter(st.items()))
    if kind == "run":
        lines = arg.strip().splitlines()
        return "run " + lines[0][:90] + (" ..." if len(lines) > 1 or len(lines[0]) > 90 else "")
    if kind == "download":
        return f"download {arg['name']}"
    if kind == "copy":
        return f"copy {Path(arg[0]).name}" + (f" -> {arg[1]}" if len(arg) > 1 else "")
    return kind


def call(inst: Instance, ps: str) -> None:
    """winvm's own guest commands during a build; fail loudly."""
    r = guest.capture(inst.ssh, "$ErrorActionPreference = 'Stop'\n" + ps)
    if r.returncode:
        die(f"guest command failed: {ps[:100]}\n{r.stdout.strip()[:500]}")


def scp(inst: Instance, src: Path, dst: str) -> None:
    run("scp", "-F", inst.dir / "ssh_config", "-q", "-r", src, f"{inst.name}:{dst.replace(chr(92), '/')}")
