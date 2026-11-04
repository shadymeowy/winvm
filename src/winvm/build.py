"""Building images.

ISO images (offline, unattended):
  1. stage an installer tree: WinPE from the Windows ISO with its shell swapped for
     winslim (pe.cmd), virtio drivers, unattend.xml, first-boot payload, `setup` steps
  2. boot it in a VM without network: WinPE applies Windows to the disk, debloats it
     offline (data/guest/lists), Windows Setup runs, the payload installs guest tools and
     OpenSSH, runs your setup steps, cleans up and powers off
  3. freeze the disk as a compressed read-only layer
  With `slim: true` the debloated image is captured once (cache/slim) and later builds
  apply it directly: ~10 min instead of ~22.

Images `from` another image: boot a scratch VM on the image below (with network), run
the steps over SSH, shut it down and keep its changes as a compressed layer.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess as sp
import time
from pathlib import Path
from xml.sax.saxutils import escape

from . import __version__, guest
from .config import GUEST, Config
from .store import Store, label
from .util import die, dos, download, log, mib, need, run, warn
from .vm import Instance, need_mem, running

# virtio driver folders injected into Windows (w11/amd64 flavour)
VIRTIO_DRIVERS = "vioscsi viostor NetKVM Balloon vioserial viorng vioinput pvpanic fwcfg viogpudo".split()
WORK = r"C:\winvm-build"        # work folder of `from` builds: downloads, copies, cwd of run steps


def build(cfg: Config, names: list[str], force=False, gui=False) -> None:
    """Build whatever `names` need, bottom-up; up-to-date images are skipped."""
    store = Store(cfg)
    order: list[str] = []
    for n in names:
        order += [x for x in cfg.chain(n) if x not in order]
    for n in order:
        cur = store.current(n)
        if cur and not (force and n in names):
            continue
        if cur and (users := store.users(cur)):
            die(f"{label(cur)} is in use by {', '.join(users)} - rm/reset those to rebuild it")
        t0 = time.time()
        (build_overlay if "from" in cfg.image(n) else build_iso)(cfg, n, gui)
        log(f"built {label(store.require(n))} in {(time.time() - t0) / 60:.0f} min")


def fetch_tools(cfg: Config) -> dict[str, Path]:
    """The pinned virtio-win ISO and OpenSSH MSI."""
    return {k: download(cfg.s[k]["url"], cfg.cache / "downloads" / cfg.s[k]["url"].rsplit("/", 1)[-1],
                        cfg.s[k]["sha256"]) for k in ("virtio", "openssh")}


def fetch_step(cfg: Config, st: dict) -> Path:
    d = st["download"]
    return download(d["url"], cfg.cache / "downloads" / d["sha256"][:16] / d["name"], d["sha256"])


# ------------------------------------------------------------------ ISO images

def build_iso(cfg: Config, name: str, gui=False) -> None:
    need(cfg.s["qemu"], cfg.s["qemu_img"], "7z", "wimlib-imagex", "xorriso")
    img, h, store = cfg.image(name), cfg.hash(name), Store(cfg)
    inst = Instance(cfg, f"_install-{name}", builder=True)
    inst.require_stopped()
    shutil.rmtree(inst.home, ignore_errors=True)
    inst.home.mkdir(parents=True)
    tree, tools = inst.home / "tree", fetch_tools(cfg)

    if img.get("slim"):
        esd = slim_image(cfg, name, inst, tools)
        stage(cfg, name, tree, tools, "install")
        log(f"applying the cached slim image {esd.name}")
        (tree / "sources").mkdir(exist_ok=True)
        link_or_copy(esd, tree / "sources/install.esd")
        write_config_cmd(cfg, name, tree, "install", preserviced=True)
        cdroms = [make_iso(tree, inst.home / "install.iso", "WINSLIM_FULL")]
    else:
        stage(cfg, name, tree, tools, "install")
        cdroms = [make_iso(tree, inst.home / "boot.iso", "WINSLIM"), cfg.iso(name)]   # pe.cmd reads CD 2
    shutil.rmtree(tree)

    run(cfg.s["qemu_img"], "create", "-q", "-f", "qcow2", inst.disk, img["disk_size"])
    log(f"installing Windows for image '{name}' (unattended, offline)")
    serial = run_installer(inst, cdroms, gui)
    if "WINSLIM-FAIL" in serial:
        die(f"install failed - see {inst.dir}/serial.log (also C:\\Windows\\Setup on {inst.disk})")
    if "WINSLIM-DONE" not in serial:
        die(f"install did not report WINSLIM-DONE; disk kept at {inst.disk}")

    if (img.get("debloat") or {}).get("remove_webview") and shutil.which("guestfish"):
        # firstlogon deletes WebView2, but the shell keeps a few files locked; finish offline
        log("removing WebView2 leftovers offline (libguestfs)")
        r = run("guestfish", "--rw", "-a", inst.disk, "-i", check=False, text=True, stdout=sp.DEVNULL,
                stderr=sp.DEVNULL, input='-rm-rf "/Program Files (x86)/Microsoft/EdgeWebView"\n-fstrim /\n')
        if r.returncode:
            warn("guestfish failed - leftovers kept")
    log(f"writing image layer {name}@{h[:12]}")
    store.add(name, h, inst.disk, None, user=img["user"], password=img["password"])
    shutil.rmtree(inst.home)
    shutil.rmtree(inst.dir, ignore_errors=True)


def slim_image(cfg: Config, name: str, inst: Instance, tools: dict) -> Path:
    """The captured, debloated install image for this ISO/edition/debloat set, built once:
    capture VM (~30 min) -> install.wim -> solid LZMS install.esd (~30 min, low RAM)."""
    key = cfg.slim_key(name)[:16]
    esd = cfg.cache / "slim" / f"{key}.esd"
    if esd.exists():
        return esd
    esd.parent.mkdir(parents=True, exist_ok=True)
    wim = esd.with_suffix(".wim")
    if not wim.exists():                                       # kept if only the export was interrupted
        need("guestfish")
        tree = inst.home / "capture-tree"
        stage(cfg, name, tree, tools, "capture")
        iso = make_iso(tree, inst.home / "capture.iso", "WINSLIM")
        shutil.rmtree(tree)
        cap = inst.home / "capture.qcow2"
        run(cfg.s["qemu_img"], "create", "-q", "-f", "qcow2", cap, "80G")
        log("slim image: apply + offline debloat + capture (~30 min, once)")
        cap_inst = Instance(cfg, inst.name, builder=True)
        cap_inst.disk = cap
        run_installer(cap_inst, [iso, cfg.iso(name)])
        log("pulling the captured image off the capture disk")
        r = run("guestfish", "--ro", "-a", cap, "run", ":", "mount", "/dev/sda4", "/", ":",
                "download", "/WINSLIM-CAPTURE-DONE", "/dev/null", ":",
                "download", "/install.wim", wim.with_suffix(".part"), check=False, stderr=sp.DEVNULL)
        if r.returncode:
            die(f"capture failed - see {cap_inst.dir}/serial.log; disk kept at {cap}")
        wim.with_suffix(".part").replace(wim)
        cap.unlink()
        iso.unlink()
    if running(cfg):
        die("a VM is running - stop it before the slim export (both need lots of RAM)")
    threads = int(cfg.s.get("wimlib_threads") or 2)
    need_mem(cfg, threads * 1024, f"solid LZMS export with {threads} threads")
    log(f"compressing the slim image (wimlib, solid LZMS, {threads} threads; slow but light)")
    tmp = esd.with_suffix(".part")
    run("wimlib-imagex", "export", wim, "1", tmp, "--solid", f"--threads={threads}")
    if tmp.stat().st_size >= 4 << 30:
        die("the slim image is over 4 GiB (ISO9660 limit) - debloat more")
    tmp.replace(esd)
    wim.unlink()
    return esd


def export_iso(cfg: Config, name: str, out: Path) -> None:
    """A standalone installer ISO of an ISO image: its slim image + payload. Booting it wipes
    disk 0 without asking (it refuses to run outside QEMU)."""
    need("7z", "wimlib-imagex", "xorriso")
    inst = Instance(cfg, f"_install-{name}", builder=True)
    inst.require_stopped()
    inst.home.mkdir(parents=True, exist_ok=True)
    tools = fetch_tools(cfg)
    esd = slim_image(cfg, name, inst, tools)
    tree = inst.home / "tree"
    stage(cfg, name, tree, tools, "install")
    link_or_copy(esd, tree / "sources/install.esd")
    write_config_cmd(cfg, name, tree, "install", preserviced=True)
    make_iso(tree, out, "WINSLIM_FULL")
    shutil.rmtree(inst.home)


def link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy(src, dst)


def list_file(name: str) -> str:
    """A data/guest/lists/*.txt file without comments and blank lines."""
    lines = (ln.rstrip() for ln in (GUEST / "lists" / f"{name}.txt").read_text().splitlines())
    return "".join(f"{ln}\n" for ln in lines if ln.strip() and not ln.lstrip().startswith("#"))


def write_config_cmd(cfg: Config, name: str, tree: Path, mode: str, preserviced=False) -> None:
    """winslim/config.cmd: the knobs pe.cmd reads."""
    img = cfg.image(name)
    flag = lambda v: int(bool(v))
    knobs = {"WIN_EDITION": img.get("edition") or "",
             "WIN_INDEX": 1 if preserviced else img.get("index") or "",
             "WIN_STRIP_PAYLOADS": flag(img["debloat"].get("strip_payloads")),
             "WIN_COMPACT": flag(img["debloat"].get("compact_os")),
             "WINSLIM_MODE": mode, "WIN_PRESERVICED": flag(preserviced),
             "WINSLIM_DEBUG": flag(os.environ.get("WINVM_DEBUG") == "1"),
             "WINSLIM_BUILD": f"winvm-{__version__}-{name}-{time.strftime('%Y%m%d')}"}
    (tree / "winslim/config.cmd").write_bytes(dos("".join(f'set "{k}={v}"\n' for k, v in knobs.items())))


def stage(cfg: Config, name: str, tree: Path, tools: dict, mode: str) -> None:
    """Everything of an installer ISO except install.wim: patched WinPE, drivers, winslim/ payload."""
    img, iso = cfg.image(name), cfg.iso(name)
    shutil.rmtree(tree, ignore_errors=True)
    tree.mkdir(parents=True)
    scratch = tree.parent / "scratch"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir()

    log(f"extracting boot files from {iso.name}")
    run("7z", "x", "-y", f"-o{tree}", iso, "bootmgr", "bootmgr.efi", "boot", "efi", "sources/boot.wim", quiet=True)
    if not (tree / "efi/microsoft/boot/efisys_noprompt.bin").exists():
        die(f"unexpected ISO layout in {iso.name} (no efisys_noprompt.bin) - an official Windows 10/11 ISO?")

    log("patching boot.wim (WinPE shell -> winslim installer)")
    boot, new = tree / "sources/boot.wim", tree / "sources/boot.new.wim"
    run("wimlib-imagex", "export", boot, "2", new, "--boot", f"--threads={cfg.s.get('wimlib_threads') or 2}",
        quiet=True)
    for f in ("winpeshl.ini", "winslim-start.cmd"):
        (scratch / f).write_bytes(dos((GUEST / f).read_text()))
    run("wimlib-imagex", "update", new, "1", quiet=True, text=True,
        input=f"add '{scratch}/winpeshl.ini' /Windows/System32/winpeshl.ini\n"
              f"add '{scratch}/winslim-start.cmd' /Windows/System32/winslim-start.cmd\n")
    new.replace(boot)

    log("extracting virtio drivers")
    vio = scratch / "virtio"
    run("7z", "x", "-y", f"-o{vio}", tools["virtio"], *(f"{d}/w11/amd64" for d in VIRTIO_DRIVERS),
        "virtio-win-guest-tools.exe", "cert/Virtio_Win_Red_Hat_CA.cer", quiet=True)
    for d in VIRTIO_DRIVERS:
        dst = tree / "drivers" / d.lower()
        dst.mkdir(parents=True)
        for f in (vio / d / "w11/amd64").iterdir():
            if f.is_file() and f.suffix != ".pdb":
                shutil.copy(f, dst)

    log("staging winslim scripts and payload")
    ws, pl = tree / "winslim", tree / "winslim/payload"
    pl.mkdir(parents=True)
    (ws / "pe.cmd").write_bytes(dos((GUEST / "pe.cmd").read_text()))
    for f in ("appx-keep", "capabilities-remove", "features-remove", "features-keep-payload",
              "services-disable", "offline-delete"):
        (ws / f"{f}.txt").write_bytes(dos(list_file(f)))
    (ws / "offline-reg.txt").write_bytes(dos((GUEST / "lists/offline-reg.txt").read_text()))
    write_config_cmd(cfg, name, tree, mode)

    xml = {"WIN_USER": img["user"], "WIN_PASSWORD": img["password"], "WIN_HOSTNAME": img["hostname"],
           "WIN_TIMEZONE": img["timezone"], "WIN_LOCALE": img["locale"], "WIN_KEYBOARD": img["keyboard"]}
    unattend = (GUEST / "unattend.xml.in").read_text()
    for k, v in xml.items():
        unattend = unattend.replace(f"@{k}@", escape(str(v), {'"': "&quot;", "'": "&apos;"}))
    (ws / "unattend.xml").write_bytes(dos(unattend))

    for f in ("SetupComplete.cmd", "setupcomplete.ps1", "firstlogon.ps1", "common.ps1"):
        (pl / f).write_bytes(dos((GUEST / "payload" / f).read_text()))
    for f in ("appx-keep", "services-disable"):
        (pl / f"{f}.txt").write_bytes(dos(list_file(f)))
    ps_bool = lambda v: "$true" if v else "$false"
    (pl / "settings.ps1").write_bytes(dos(
        f"$WinUser = {guest.quote(img['user'])}\n$WinCompact = {ps_bool(img['debloat'].get('compact_os'))}\n"
        f"$RemoveWebView = {ps_bool(img['debloat'].get('remove_webview'))}\n"))
    shutil.copy(vio / "virtio-win-guest-tools.exe", pl)
    shutil.copy(vio / "cert/Virtio_Win_Red_Hat_CA.cer", pl / "virtio-ca.cer")
    shutil.copy(tools["openssh"], pl / "OpenSSH.msi")
    (pl / "authorized_keys").write_bytes(dos(cfg.authorized_keys()))
    shutil.rmtree(scratch)
    stage_setup(cfg, name, pl / "extra")


def stage_setup(cfg: Config, name: str, extra: Path) -> None:
    """`setup` steps -> payload/extra: their files plus setup.ps1, which setupcomplete.ps1 runs
    as SYSTEM after the debloat (a failing step fails the build)."""
    steps = cfg.steps(name)
    if not steps:
        return
    extra.mkdir(parents=True)
    ps = ["$ErrorActionPreference = 'Stop'"]
    for i, st in enumerate(steps, 1):
        if "download" in st:
            shutil.copy(fetch_step(cfg, st), extra / st["download"]["name"])
        elif "copy" in st:
            src = Path(st["copy"][0])
            (shutil.copytree if src.is_dir() else shutil.copy)(src, extra / src.name)
        else:
            ps += [f"# step {i}", "$global:LASTEXITCODE = 0", st["run"],
                   f"if ($LASTEXITCODE) {{ throw \"setup step {i} exited with $LASTEXITCODE\" }}"]
    (extra / "setup.ps1").write_bytes(dos("\n".join(ps)))
    log(f"setup: {len(steps)} step(s) staged")


def make_iso(tree: Path, out: Path, vol: str) -> Path:
    """Hybrid BIOS/UEFI ISO; efisys_noprompt skips 'press any key to boot from CD'."""
    log(f"writing {out.name}")
    tmp = out.with_suffix(".tmp")                              # replaced at the end: never half-written
    run("xorriso", "-as", "mkisofs", "-quiet", "-iso-level", "3", "-J", "-joliet-long", "-relaxed-filenames",
        "-V", vol, "-b", "boot/etfsboot.com", "-no-emul-boot", "-boot-load-size", "8", "-hide", "boot.catalog",
        "-eltorito-alt-boot", "-e", "efi/microsoft/boot/efisys_noprompt.bin", "-no-emul-boot", "-o", tmp, tree)
    tmp.replace(out)
    return out


def run_installer(inst: Instance, cdroms: list[Path], gui=False) -> str:
    """Boot an offline installer VM, stream its [PE]/[SC]/[FL]/[WIN] progress lines from the
    serial log until QEMU exits, and return the whole log."""
    from .vm import ovmf
    shutil.copyfile(ovmf(inst.cfg)[1], inst.vars)
    inst.claim(mib(inst.settings["ram"]))
    serial = inst.dir / "serial.log"
    serial.write_bytes(b"")
    if not gui and inst.state.get("vnc"):
        log(f"watch it: vncviewer 127.0.0.1:{inst.state['vnc']}  (serial log: {serial})")
    t0 = time.time()
    qemu = sp.Popen(inst.qemu_argv(install=True, gui=gui, cdroms=cdroms) + ["-pidfile", str(inst.pidfile)])
    buf = b""
    try:
        with serial.open("rb") as f:
            while True:
                done = qemu.poll() is not None                 # read once more after QEMU exits
                buf += f.read()
                *lines, buf = buf.split(b"\n")
                for ln in lines:
                    if re.match(rb"\[(PE|SC|FL|WIN)", ln):
                        print(ln.decode(errors="replace").rstrip("\r"), flush=True)
                if done:
                    break
                time.sleep(0.5)
    finally:
        if qemu.poll() is None:
            qemu.terminate()
        inst.pidfile.unlink(missing_ok=True)
    log(f"installer VM finished after {(time.time() - t0) / 60:.0f} min")
    return serial.read_text(errors="replace")


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
                call(inst, f"New-Item -ItemType Directory -Force (Split-Path {guest.quote(dst)}) | Out-Null")
                scp(inst, src, dst)
            elif kind == "restart":
                inst.stop()
                inst.start(gui=gui)
        # leave nothing behind, and hand freed blocks back so the layer stays small
        call(inst, f"Remove-Item -LiteralPath {guest.quote(WORK)} -Recurse -Force; "
                   "Optimize-Volume -DriveLetter C -ReTrim -ErrorAction SilentlyContinue | Out-Null")
        inst.stop()
        log(f"writing image layer {name}@{h[:12]}")
        meta = store.meta(below)
        store.add(name, h, inst.disk, below, user=meta.get("user"), password=meta.get("password"))
    finally:
        if inst.alive:
            inst.kill()
        shutil.rmtree(inst.home, ignore_errors=True)
        shutil.rmtree(inst.dir, ignore_errors=True)


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
