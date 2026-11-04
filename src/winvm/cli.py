"""The `winvm` command line."""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path

import yaml

from . import __version__, guest, host
from .build import build, fetch_step
from .config import BUILTIN, DEFAULT_FILE, Config, is_path, sha
from .media import editions
from .store import Store, label
from .util import Chan, Fatal, die, du, log, mib, paint, valid, warn
from .vm import Instance, ports, running, smbd as vm_smbd

COMMANDS: dict[str, tuple] = {}
RAW = set()   # commands whose arguments are passed through verbatim (PowerShell, ssh, scp, ...)


def arg(*flags, **kw):
    return flags, kw


def command(*args, raw=False, aliases=()):
    """Register cmd_<name> as subcommand <name>; its docstring's first line is the help text."""
    def deco(fn):
        name = fn.__name__.removeprefix("cmd_").replace("_", "-")
        COMMANDS[name] = (fn, args, aliases)
        if raw:
            RAW.update((name, *aliases))
        return fn
    return deco


def inst(a) -> Instance:
    """The -i instance; build VMs (_install-X, _build-X) can be named for debugging."""
    name = a.instance
    builder = name.startswith("_") and (a.cfg.build / name).is_dir()
    return Instance(a.cfg, name if builder else valid(name), builder=builder)


def existing(a) -> Instance:
    """The -i instance, which must exist - except the default one, main, which is created
    on first use as `winvm up` would (a mistyped name shouldn't start a download and build)."""
    vm = inst(a)
    if not vm.builder and not vm.disk.exists():
        if vm.name != "main":
            die(f"{vm.name} does not exist yet - winvm up {vm.name}")
        ensure_disk(a.cfg, vm)
    return vm


def ready(a) -> Instance:
    """The -i instance, running with SSH answering (see existing)."""
    return existing(a).up()


RDP_CLIENTS = ("xfreerdp3", "xfreerdp", "sdl-freerdp3")
VNC_VIEWERS = ("remote-viewer", "vncviewer")


# ------------------------------------------------------------------ run instances

def link_iso(cfg: Config, iso: str) -> None:
    """--iso: link the ISO into ${data_dir}/iso, where images without `iso:` find the newest one."""
    src = Path(iso).expanduser().resolve()
    if not src.is_file():
        die(f"no such ISO: {src}")
    dst = cfg.data / "iso" / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.resolve() == src:                                   # linked before: make it the newest again
        os.utime(dst, follow_symlinks=False)
        return
    if dst.exists() and not dst.is_symlink():
        die(f"{dst} is another file with that name - move it away or rename the ISO")
    dst.unlink(missing_ok=True)
    dst.symlink_to(src)
    log(f"using {src} (linked as {dst})")


def ensure_disk(cfg: Config, vm: Instance, image: str | None = None, run: dict | None = None) -> None:
    """A new instance gets a disk on its image (built first if needed): `image`, else the
    image named like the instance, else base. An existing one keeps its disk; new `run`
    settings are added to its own, and it is told when its image has a newer version."""
    store = Store(cfg)
    if vm.disk.exists():
        if run:
            vm.meta = {**vm.meta, "run": {**vm.meta.get("run", {}), **run}}
        if image and image != vm.image:
            warn(f"{vm.name} runs on '{vm.image}' - winvm -i {vm.name} reset {image} to switch")
        if outdated(cfg, store, vm, store.parent(vm.disk)):
            warn(f"{vm.name} runs on an older version of '{vm.image}' - "
                 f"winvm -i {vm.name} reset to update it (discards its changes)")
        return
    host.require(cfg)                                          # before creating anything
    image = image or (vm.name if vm.name in cfg.images else "base")
    if image in cfg.images:
        build(cfg, [image])
    vm.create(image, store.require(image), run or {})


@command(arg("names", nargs="*", metavar="NAME", help="instance, or an image file (instance named like it); "
                                                     "default: -i, else main"),
         arg("--image", metavar="IMAGE", help="image (name or file) for a new instance; default: the "
                                              "image named like the instance, else base"),
         arg("--ram", help="e.g. 8G, or 2G-8G: dynamic memory between the two; kept for this instance"),
         arg("--cpus", type=int, help="kept for this instance"),
         arg("--all", action="store_true", help="every existing instance"),
         arg("--iso", help="Windows ISO for images without `iso:` (remembered in ${data_dir}/iso)"),
         arg("--gui", action="store_true", help="GTK window with clipboard instead of headless"),
         aliases=("start",))
def cmd_up(a):
    """create (building images as needed) and start instances; ready when SSH answers"""
    cfg, store = a.cfg, Store(a.cfg)
    if a.iso:
        link_iso(cfg, a.iso)
    ram = a.ram
    if ram and "-" in ram:                                     # 2G-8G: dynamic memory
        lo, hi = ram.split("-", 1)
        ram = {"min": lo, "max": hi}
    for size in (ram.values() if isinstance(ram, dict) else [ram] if ram else []):
        mib(size)                                              # fails early on a bad size
    run = {k: v for k, v in (("ram", ram), ("cpus", a.cpus)) if v}
    image = cfg.ref(a.image) if a.image else None
    names = [d.parent.name for d in store.disks()] if a.all else \
        [cfg.ref(n) if is_path(n) else valid(n) for n in a.names or [a.instance]]
    host.require(cfg, a.gui)                                   # before building or creating
    for n in names:
        vm = Instance(cfg, n)
        ensure_disk(cfg, vm, image, run)
        vm.start(gui=a.gui)
        log(f"{n} is up - winvm{'' if n == 'main' else f' -i {n}'} sh")


@command(arg("names", nargs="*", metavar="INSTANCE"),
         arg("--all", action="store_true", help="every running instance"), aliases=("down",))
def cmd_stop(a):
    """graceful shutdown (ACPI), hard kill after 120 s"""
    vms = [vm for vm in running(a.cfg) if not vm.builder] if a.all else \
        [Instance(a.cfg, valid(n)) for n in a.names or [a.instance]]
    for vm in vms:
        vm.stop()


@command()
def cmd_kill(a):
    """hard power-off"""
    vm = inst(a)
    log(f"killed {vm.name}" if vm.kill() else f"{vm.name} not running")


@command(aliases=("status",))
def cmd_ps(a):
    """list instances: state, ports, disk size, image"""
    cfg, store = a.cfg, Store(a.cfg)
    names = sorted({d.parent.name for d in store.disks()} | {vm.name for vm in running(cfg)})
    print(f"{'INSTANCE':<14} {'STATE':<8} {'SSH':>5} {'RDP':>5} {'VNC':>5} {'DISK':>6} {'RAM':>9}  IMAGE")
    for n in names:
        vm = Instance(cfg, n, builder=n.startswith("_"))
        st = vm.state if vm.alive else {}
        ssh, rdp, vnc = (str(st.get(k) or "-") for k in ("ssh", "rdp", "vnc"))
        on = store.parent(vm.disk) if vm.disk.exists() else None
        image = (label(on) if on else "-") + ("  (outdated)" if on and outdated(cfg, store, vm, on) else "")
        mounts = ", ".join(vm.mounts) if st else ""
        print(f"{n:<14} {'running' if st else 'stopped':<8} {ssh:>5} {rdp:>5} {vnc:>5} "
              f"{du(vm.disk):>6} {ram_text(vm, st):>9}  {image}" + (f"   mounts: {mounts}" if mounts else ""))


def ram_text(vm: Instance, st: dict) -> str:
    """4G, or for dynamic memory what the guest has now / its max (2.1/8G)."""
    rng = vm.auto_ram
    if not rng:
        return f"{vm.ram / 1024:g}G"
    try:
        now = json.loads((vm.dir / "balloon.json").read_text())["actual"] if st and vm.balloon_pid else None
    except (OSError, ValueError, KeyError):
        now = None
    return f"{now / 1024:.1f}/{rng[1] / 1024:g}G" if now else f"{rng[0] / 1024:g}-{rng[1] / 1024:g}G"


def outdated(cfg: Config, store: Store, vm: Instance, on: Path | None) -> bool:
    """Is the instance's disk on an older version of its (config) image than the current one?"""
    if vm.builder or vm.image not in cfg.images:
        return False
    try:
        return store.current(vm.image) != on
    except Fatal:                                              # e.g. a `copy` source is gone
        return False


# ------------------------------------------------------------------ guest access

@command(arg("cmd", nargs="*", metavar="CMD", help="PowerShell command line (as typed at the prompt)"),
         raw=True, aliases=("shell",))
def cmd_sh(a):
    """PowerShell in the guest: interactive, or run CMD and return its exit code

    winvm sh                          interactive shell
    winvm sh ipconfig /all            run a command line; PowerShell parses it
    winvm sh 'gci C:\\ | select Name'  one quoted argument = PowerShell code as is
    winvm sh notepad "a b.txt"        words with spaces stay one argument
    winvm sh -t python                -t: a terminal, for interactive programs
    cat x | winvm sh findstr foo      stdin reaches native programs; PowerShell code reads `stdin`
    winvm sh ipconfig | grep IPv4     output has Unix line endings

    The exit code is the command's. Run from inside a mounted host folder, the guest
    command runs in the matching C:\\mnt\\... folder.
    """
    vm = ready(a)
    tty = a.rest[:1] == ["-t"]
    words = a.rest[tty:]
    cwd = vm.guest_path(Path.cwd())
    if not words:                                              # interactive shell
        cd = f"Set-Location -LiteralPath {guest.quote(cwd)}\npowershell -NoLogo" if cwd else None
        return guest.terminal(vm.ssh("-t") + ([guest.encoded(cd)] if cd else []))
    script = guest.script(guest.command_line(words), cwd)
    return guest.tty(vm.ssh, script) if tty else guest.pipe(vm.ssh, script)


@command(arg("args", nargs="*", metavar="ARG", help="ssh options, then an optional remote command"), raw=True)
def cmd_ssh(a):
    """plain ssh to the instance, e.g. ssh -L 8080:localhost:80 -N  |  ssh hostname"""
    vm = ready(a)
    opts, rest = [], list(a.rest)
    while rest and rest[0].startswith("-"):                    # options go before the host name
        o = rest.pop(0)
        if o == "--":
            break
        opts.append(o)
        if len(o) == 2 and o[1] in "BbcDEeFIiJLlmOoPpRSWw" and rest:   # ssh options taking a value
            opts.append(rest.pop(0))
    os.execvp("ssh", ["ssh", "-F", str(vm.dir / "ssh_config"), *opts, vm.name, *rest])


@command(arg("paths", nargs="*", metavar="PATH", help="':' prefix = guest path"), raw=True)
def cmd_cp(a):
    """copy files with scp, e.g. cp a.txt :C:/Users/user/  |  cp :C:/x.log ."""
    vm = ready(a)
    paths = [f"{vm.name}{p}" if p.startswith(":") else p for p in a.rest]
    os.execvp("scp", ["scp", "-F", str(vm.dir / "ssh_config"), "-r", *paths])


@command(arg("timeout", nargs="?", type=int, default=600))
def cmd_wait(a):
    """wait until SSH in the guest answers"""
    inst(a).wait_ssh(a.timeout)


@command()
def cmd_gui(a):
    """open a VNC viewer on a running instance, or start it with a GTK window"""
    vm = inst(a)
    if not vm.alive:
        host.require(a.cfg, gui=True)
        return existing(a).start(gui=True, wait=False)
    vnc = vm.state.get("vnc") or die(f"{vm.name} runs headless without VNC - winvm rdp")
    for viewer in VNC_VIEWERS:
        if shutil.which(viewer):
            os.execvp(viewer, [viewer, f"vnc://127.0.0.1:{vnc}" if viewer == "remote-viewer" else f"127.0.0.1:{vnc}"])
    die(f"no VNC viewer found - connect one to 127.0.0.1:{vnc}, or {host.install_hint('vnc')}")


@command(arg("args", nargs="*", metavar="FREERDP_ARG"), raw=True)
def cmd_rdp(a):
    """RDP session with FreeRDP: dynamic resolution, clipboard"""
    vm = ready(a)                                          # booted = SSH answers
    port = vm.state.get("rdp") or die("RDP is off (settings.ports.rdp)")
    meta = vm.layer_meta()
    for x in RDP_CLIENTS:
        if shutil.which(x):
            os.execvp(x, [x, f"/v:127.0.0.1:{port}", f"/u:{meta.get('user', 'user')}",
                          f"/p:{meta.get('password', '')}", "/cert:ignore", "/dynamic-resolution", "+clipboard",
                          *a.rest])
    die(f"no FreeRDP client found - {host.install_hint('rdp')}")


@command(arg("args", nargs="*", metavar="ARG", help="agent command [json] | exec PROGRAM [ARGS]"), raw=True)
def cmd_ga(a):
    """qemu-guest-agent (works without network), e.g. ga guest-info | ga exec ipconfig /all"""
    agent = Chan(inst(a).dir / "qga.sock", qmp=False)
    cmd, *rest = a.rest or ["guest-ping"]
    if cmd != "exec":
        try:
            args = json.loads(rest[0]) if rest else None
        except ValueError as e:
            die(f"arguments must be JSON, e.g. '{{\"path\": \"C:/x.txt\"}}': {e}")
        return print(json.dumps(agent(cmd, args), indent=1))
    if not rest:
        die("usage: winvm ga exec PROGRAM [ARGS...]")
    pid = agent("guest-exec", {"path": rest[0], "arg": rest[1:], "capture-output": True})["pid"]
    while not (st := agent("guest-exec-status", {"pid": pid})).get("exited"):
        time.sleep(0.2)
    sys.stdout.write(base64.b64decode(st.get("out-data", "")).decode("utf-8", "replace"))
    sys.stderr.write(base64.b64decode(st.get("err-data", "")).decode("utf-8", "replace"))
    return st.get("exitcode", 0)


@command(arg("size", nargs="?", help="e.g. 2G; omit to show"))
def cmd_balloon(a):
    """show or set guest RAM live via virtio-balloon (returns memory to the host)"""
    vm = inst(a)
    qmp = Chan(vm.dir / "qmp.sock", qmp=True)
    if a.size and vm.balloon_pid:
        warn(f"{vm.name} has dynamic memory: winvm will move this again as the guest needs")
    if a.size:
        qmp("balloon", {"value": mib(a.size) << 20})
        time.sleep(2)
    print(f"guest RAM: {qmp('query-balloon')['actual'] >> 20} MiB")


@command(arg("png", nargs="?", help="output file (default: in the run folder)"))
def cmd_screenshot(a):
    """save a PNG of the guest screen"""
    vm = inst(a)
    out = Path(a.png or vm.dir / "screen.png").resolve()
    Chan(vm.dir / "qmp.sock", qmp=True)("screendump", {"filename": str(out), "format": "png"})
    print(out)


@command(arg("--gui", action="store_true"))
def cmd_cmdline(a):
    """print the QEMU command line `up` uses"""
    vm = inst(a)
    st = vm.state if vm.alive else {"ram": vm.ram, **ports(a.cfg, vm.state.get("slot", 0))}
    print(shlex.join(vm.qemu_argv(gui=a.gui, st=st)))


# ------------------------------------------------------------------ mounts

@command(arg("path", nargs="?", help="host folder; omit to list the mounts"),
         arg("name", nargs="?", help="share name, guest C:\\mnt\\NAME (default: the folder name)"),
         arg("--ro", action="store_true", help="read-only"))
def cmd_mount(a):
    """mount a host folder into the running instance, at C:\\mnt\\NAME"""
    vm = inst(a)
    if not a.path:                                             # stopped: what the next start sets up
        for n, m in (vm.mounts if vm.alive else vm.settings["mounts"]).items():
            print(f"C:\\mnt\\{n:<14} {m['path']}{'  (read-only)' if m['readonly'] else ''}")
        return
    path = Path(a.path).expanduser().resolve()
    if not path.is_dir():
        die(f"not a folder: {path}")
    name = valid(a.name or re.sub(r"[^A-Za-z0-9._-]", "_", path.name))
    vm.mount(name, path, a.ro)
    log(f"{path} -> {vm.name}:C:\\mnt\\{name}")


@command(arg("name"))
def cmd_umount(a):
    """remove a mount from the running instance"""
    inst(a).umount(valid(a.name))
    log(f"unmounted {a.name}")


# ------------------------------------------------------------------ images

@command(arg("images", nargs="*", metavar="IMAGE", help="name or image file; default: every image winvm knows"),
         arg("--force", action="store_true", help="rebuild even if up to date (refused while in use)"),
         arg("--gui", action="store_true", help="show the build VM in a window"))
def cmd_build(a):
    """build images (and what they build on) whose definition changed or that are missing"""
    names = [a.cfg.ref(n) for n in a.images] or list(a.cfg.images)
    build(a.cfg, names, force=a.force, gui=a.gui)
    log("images up to date")


@command()
def cmd_images(a):
    """list images: their file, built versions, size, what each builds on, who uses it"""
    cfg, store = a.cfg, Store(a.cfg)
    rows = [("IMAGE", "STATUS", "SIZE", "ON", "USED BY", "DEFINED IN")]
    for name in list(cfg.images) + sorted({store.meta(f)["name"] for f in store.all()} - set(cfg.images)):
        try:
            cur = store.current(name)
        except Fatal as e:                                     # no ISO, a `copy` source gone, ...
            warn(str(e))
            cur = None
        src = cfg.sources.get(name)
        where = ("built-in" if src.parent == BUILTIN else tilde(src)) if src else "-"
        where += "  (replaces the built-in)" if name in cfg.replaced else ""
        if name in cfg.images and not cur:
            rows.append((name, "not built", "-", cfg.parent(name) or "iso", "", where))
        for f in store.versions(name):
            meta = store.meta(f)
            status = "saved" if meta.get("saved") else "current" if f == cur else "old"
            rows.append((label(f), status, du(f), meta.get("from") or "iso", " ".join(store.users(f)),
                         where if f == cur or status == "saved" else ""))
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r[:5], widths)) + "  " + r[5])


@command(arg("--cache", action="store_true", help="also the download and slim-image cache"))
def cmd_prune(a):
    """delete image versions nothing uses (old, or whose file is gone), build leftovers"""
    cfg, store = a.cfg, Store(a.cfg)
    keep = set()
    for name in cfg.images:
        try:
            keep.add(store.current(name))
        except Fatal as e:                                     # can't tell which version is current
            if newest := store.versions(name)[:1]:
                warn(f"{e} - keeping {label(newest[0])}")
                keep.update(newest)
    keep |= {f for f in store.all() if store.meta(f).get("saved")}
    removed = True
    while removed:                                             # removing a layer may free the one below
        removed = False
        for f in store.all():
            if f not in keep and not store.users(f):
                log(f"removing {label(f)} ({du(f)})")
                store.remove(f)
                removed = True
    live = {vm.name for vm in running(cfg)}
    for d in cfg.build.glob("*"):
        if d.is_dir() and d.name not in live:
            shutil.rmtree(d)
    if a.cache:
        for d in ("downloads", "slim", "firmware"):
            shutil.rmtree(cfg.cache / d, ignore_errors=True)
    log("pruned")


@command(arg("name"))
def cmd_rmi(a):
    """delete all versions of an image (refused while anything builds on them)"""
    store = Store(a.cfg)
    versions = store.versions(valid(a.name)) or die(f"no built image '{a.name}'")
    for f in versions:
        if users := store.users(f):
            die(f"{label(f)} is used by {', '.join(users)} - rm/reset those first")
    for f in versions:
        store.remove(f)
    log(f"removed image '{a.name}'" + (" (its file is still there: rebuilt when needed)" if a.name in a.cfg.images else ""))


@command(arg("name", help="new image name"))
def cmd_save(a):
    """freeze the instance's disk as image NAME (compressed, read-only) and continue on it"""
    cfg, store, vm, name = a.cfg, Store(a.cfg), inst(a), valid(a.name)
    vm.require_stopped()
    vm.disk.exists() or die(f"{vm.name} has no disk")
    if name in cfg.images:
        die(f"'{name}' is an image file ({tilde(cfg.sources[name])}) - pick another name for a saved image")
    if store.versions(name):
        die(f"image '{name}' exists (winvm rmi {name} first)")
    below = store.parent(vm.disk)
    meta = store.meta(below)
    log(f"saving {vm.name} as image '{name}' on {label(below)}")
    f = store.add(name, sha([label(below), time.time()]), vm.disk, below, saved=True,
                  user=meta.get("user"), password=meta.get("password"))
    vm.create(name, f)
    log(f"saved {label(f)} ({du(f)}); {vm.name} continues on it")


@command(arg("image", nargs="?", help="default: the instance's image (its current version)"))
def cmd_reset(a):
    """discard the instance's changes: fresh disk on the current version of its image"""
    cfg, vm = a.cfg, inst(a)
    vm.require_stopped()
    image = a.cfg.ref(a.image) if a.image else vm.image
    if image in cfg.images:
        build(cfg, [image])
    vm.create(image, Store(cfg).require(image))


@command()
def cmd_rm(a):
    """delete the instance: its disk (unsaved changes!) and run folder"""
    vm = inst(a)
    vm.require_stopped()
    if not (vm.home.exists() or vm.dir.exists()):
        die(f"no instance '{vm.name}' (winvm ps)")
    shutil.rmtree(vm.home, ignore_errors=True)
    shutil.rmtree(vm.dir, ignore_errors=True)
    log(f"removed instance {vm.name}")


@command(arg("image", nargs="?", default="base"))
def cmd_editions(a):
    """list the editions inside an image's Windows ISO"""
    root = a.cfg.chain(a.cfg.ref(a.image))[0]                 # the ISO image a `from` image is built on
    for idx, name in editions(a.cfg.iso(root)):
        print(f"  {idx}: {name}")


@command(arg("images", nargs="*", metavar="IMAGE", help="default: every image winvm knows"))
def cmd_fetch(a):
    """download now what building the images needs (ISO, drivers, firmware, download steps)"""
    cfg = a.cfg
    names = [cfg.ref(n) for n in a.images] or list(cfg.images)
    chain = [x for n in names for x in cfg.chain(n)]
    items = host.downloads_for(cfg, chain)
    host.consent(items, cfg.yes)
    host.fetch_all(cfg, items)
    for name in dict.fromkeys(chain):
        for st in cfg.steps(name):
            if "download" in st:
                fetch_step(cfg, st)
    log("everything downloaded")


@command()
def cmd_check(a):
    """what this host has and lacks for winvm, and how to get it"""
    cfg, bad = a.cfg, 0

    def row(what: str, ok, detail: str) -> None:
        nonlocal bad
        word, color = {True: ("ok", "1;32"), False: ("missing", "1;31"), None: ("later", "1;33")}[ok]
        bad += ok is False
        print(f"  {what:<13} {paint(color, f'{word:<7}')}  {detail}")   # pad before coloring

    print("required")
    kvm = host.kvm_problem()
    row("KVM", not kvm, kvm or "/dev/kvm")
    qemu = host.program(cfg, "qemu")
    row("QEMU", bool(qemu), qemu or host.install_hint("qemu"))
    try:
        row("qemu-img", True, host.qemu_img(cfg))
    except Fatal:
        row("qemu-img", False, host.install_hint("qemu"))
    missing = host.ssh_missing()
    row("OpenSSH", not missing, f"{', '.join(missing)} missing - {host.install_hint('ssh')}" if missing
        else "ssh, scp, ssh-keygen")
    fw = host.ovmf(cfg)
    row("UEFI (OVMF)", True if fw else None, fw[0] if fw else "downloaded from Debian when needed (6 MB)")
    print("optional")
    smbd = vm_smbd()
    row("Samba", True if smbd else None, smbd or f"for mounts (C:\\mnt): {host.install_hint('samba')}")
    gtk = bool(qemu) and host.has_gtk(cfg)
    row("GTK display", True if gtk else None, "QEMU window (--gui, winvm gui)" if gtk else
        f"for --gui / winvm gui on a stopped instance: {host.install_hint('gtk')}")
    rdp = next(filter(shutil.which, RDP_CLIENTS), None)
    row("FreeRDP", True if rdp else None, rdp if rdp else f"for winvm rdp: {host.install_hint('rdp')}")
    vnc = next(filter(shutil.which, VNC_VIEWERS), None)
    row("VNC viewer", True if vnc else None, vnc if vnc else
        f"for winvm gui on a running instance: {host.install_hint('vnc')}")
    ksm = Path("/sys/kernel/mm/ksm/run")
    on = ksm.exists() and ksm.read_text().strip() == "1"
    row("KSM", True if on else None, "shares identical memory between VMs" if on else
        "off - to share identical memory between VMs: echo 1 | sudo tee /sys/kernel/mm/ksm/run")
    print("downloads")
    isos, win = cfg.linked_isos(), cfg.s.get("windows")
    row("Windows ISO", True if isos else None,
        isos[0].name if isos else
        f"{win.get('title', 'Windows')}: downloaded when needed (asks first)" if win else
        "none yet: winvm up --iso PATH")
    for k in ("virtio", "openssh"):
        pin = host.pinned(cfg, k)
        row(k, True if pin["dest"].exists() else None, pin["dest"].name)
    free = shutil.disk_usage(cfg.data if cfg.data.exists() else Path.home()).free
    print(f"\n  {du_free(free)} free for {cfg.data} (a first build needs ~25 GB)")
    if bad:
        die(f"{bad} requirement(s) missing")


def du_free(n: int) -> str:
    return f"{n / (1 << 30):.0f} GB"


# ------------------------------------------------------------------ config

@command(arg("what", choices=["dump", "show", "path"], nargs="?", default="path"),
         arg("image", nargs="?", help="an image (name or file) instead of the machine settings"))
def cmd_config(a):
    """machine settings and image definitions

    winvm config dump                 built-in machine settings, with comments
    winvm config dump base            built-in image `base`: the template for image files
    winvm config show [IMAGE]         what is in effect: merged settings, or a resolved image
    winvm config path                 files and folders in use
    """
    cfg = a.cfg
    if a.what == "dump":
        f = DEFAULT_FILE if not a.image else BUILTIN / f"{valid(a.image)}.yaml"
        sys.stdout.write(f.read_text() if f.exists() else die(f"no built-in image '{a.image}'"))
    elif a.what == "show" and a.image:
        name = cfg.ref(a.image)
        print(f"# {cfg.sources[name]}")
        print(yaml.safe_dump({**cfg.image(name), "run": cfg.run_settings(name)}, sort_keys=False, width=120), end="")
    elif a.what == "show":
        print(yaml.safe_dump({k: cfg.raw[k] for k in ("settings", "run")}, sort_keys=False, width=120), end="")
    else:
        print(f"settings: {cfg.file or '(built-in only: winvm config dump)'}")
        print(f"images:   {cfg.images_dir}  (NAME.yaml, then the built-ins)")
        print(f"data:     {cfg.data}\ncache:    {cfg.cache}\nruntime:  {cfg.runtime}")


def tilde(p: Path) -> str:
    home = str(Path.home())
    return "~" + str(p)[len(home):] if str(p).startswith(home + "/") else str(p)


# ------------------------------------------------------------------ main

# ------------------------------------------------------------------ shell completion

FILE_ARGS = {"up", "start", "build", "fetch", "editions", "reset", "config", "cp", "mount", "screenshot"}


@command(arg("shell", choices=["bash", "fish"]))
def cmd_completion(a):
    """print a shell completion script, generated from winvm's own commands

    winvm completion bash > ~/.local/share/bash-completion/completions/winvm
    winvm completion fish > ~/.config/fish/completions/winvm.fish
    zsh: autoload -U bashcompinit && bashcompinit && source <(winvm completion bash)
    """
    p, subs = parser()
    glob = [o for act in p._actions for o in act.option_strings]
    vals = [o for act in p._actions if act.nargs != 0 and act.option_strings for o in act.option_strings]
    if a.shell == "bash":
        cases = "".join(f"    {'|'.join(names)}) opts=\"{' '.join(o for act in sp_._actions for o in act.option_strings)}\";;\n"
                        for names, sp_ in subs)
        sys.stdout.write(f"""# winvm {__version__} bash completion (winvm completion bash)
_winvm() {{
  local cur=${{COMP_WORDS[COMP_CWORD]}} cmd="" opts="" i
  for ((i = 1; i < COMP_CWORD; i++)); do
    case ${{COMP_WORDS[i]}} in
      {'|'.join(vals)}) ((i++));;
      -*) ;;
      *) cmd=${{COMP_WORDS[i]}}; break;;
    esac
  done
  if [[ -z $cmd ]]; then
    COMPREPLY=($(compgen -W "{' '.join(n for names, _ in subs for n in names)} {' '.join(glob)}" -- "$cur"))
    return
  fi
  case $cmd in
{cases}  esac
  if [[ $cur == -* ]]; then
    COMPREPLY=($(compgen -W "$opts" -- "$cur"))
  else
    case $cmd in {'|'.join(n for names, _ in subs if names[0] in FILE_ARGS for n in names)}) COMPREPLY=($(compgen -f -- "$cur"));; esac
  fi
}}
complete -o default -o bashdefault -F _winvm winvm
""")
        return
    q = desc
    out = [f"# winvm {__version__} fish completion (winvm completion fish)", "complete -c winvm -f"]
    for act in p._actions:
        if act.option_strings and act.help != argparse.SUPPRESS:
            out.append(fish_opt("__fish_use_subcommand", act, q))
    for names, sp_ in subs:
        out.append(f"complete -c winvm -n __fish_use_subcommand -a {names[0]} -d '{q(sp_.description)}'")
        for alias in names[1:]:
            out.append(f"complete -c winvm -n __fish_use_subcommand -a {alias} -d 'same as {names[0]}'")
        seen = f"'__fish_seen_subcommand_from {' '.join(names)}'"
        if names[0] in FILE_ARGS:
            out.append(f"complete -c winvm -n {seen} -F")
        for act in sp_._actions:
            if act.option_strings and act.help != argparse.SUPPRESS and "-h" not in act.option_strings:
                out.append(fish_opt(seen, act, q))
            elif not act.option_strings and act.choices:
                out.append(f"complete -c winvm -n {seen} -a '{' '.join(act.choices)}'")
    print("\n".join(out))


def desc(text: str | None) -> str:
    """First line of a help text, quoted for fish."""
    return text.splitlines()[0][:80].replace("'", "\\'") if text else ""


def fish_opt(cond: str, act, q) -> str:
    longs = [o[2:] for o in act.option_strings if o.startswith("--")]
    shorts = [o[1:] for o in act.option_strings if not o.startswith("--")]
    flags = " ".join([*(f"-l {x}" for x in longs), *(f"-s {x}" for x in shorts)])
    return f"complete -c winvm -n {cond} {flags}{' -r' if act.nargs != 0 else ''} -d '{q(act.help)}'"


cmd_completion.no_config = True


# ------------------------------------------------------------------ main

def parser() -> tuple[argparse.ArgumentParser, list[tuple[list[str], argparse.ArgumentParser]]]:
    """The command line, from COMMANDS; also returns each command's names and parser."""
    p = argparse.ArgumentParser(
        prog="winvm", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Build and run slim, unattended Windows VMs on QEMU/KVM; images defined in YAML files.",
        epilog="quick start:  winvm up   then   winvm sh      (winvm check: what the host has)")
    p.add_argument("-f", "--file", metavar="YAML",
                   help="machine settings (default: $WINVM_CONFIG, ~/.config/winvm/winvm.yaml)")
    p.add_argument("-i", "--instance", metavar="NAME", help="instance to act on (default: main)")
    p.add_argument("--data-dir", metavar="DIR",
                   help="images and instances (default: $WINVM_DATA_DIR, settings.data_dir)")
    p.add_argument("--cache-dir", metavar="DIR",
                   help="downloads and build scratch (default: $WINVM_CACHE_DIR, settings.cache_dir)")
    p.add_argument("-y", "--yes", action="store_true", help="download what's needed without asking")
    p.add_argument("-V", "--version", action="version", version=f"winvm {__version__}")
    sub = p.add_subparsers(title="commands", metavar="<command>")
    subs = []
    for name, (fn, args, aliases) in COMMANDS.items():
        doc = fn.__doc__ or ""
        sp_ = sub.add_parser(name, aliases=aliases, help=doc.splitlines()[0], description=doc,
                             formatter_class=argparse.RawDescriptionHelpFormatter)
        for flags, kw in args:
            sp_.add_argument(*flags, **kw)
        sp_.set_defaults(fn=fn)
        subs.append(([name, *aliases], sp_))
    return p, subs


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p, _ = parser()

    # raw commands get everything after their name untouched (PowerShell, ssh, scp arguments)
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in ("-i", "--instance", "-f", "--file", "--data-dir", "--cache-dir") else 1
    if i < len(argv) and argv[i] in RAW and argv[i + 1:] not in (["-h"], ["--help"]):
        a = p.parse_args(argv[: i + 1])
        a.rest = argv[i + 1:]
    else:
        a = p.parse_args(argv)
    if not hasattr(a, "fn"):
        p.print_help()
        return 1
    try:
        if getattr(a.fn, "no_config", False):
            return a.fn(a) or 0
        a.cfg = Config(a.file, a.data_dir, a.cache_dir)
        a.cfg.yes = a.yes
        a.instance = a.instance or "main"
        return a.fn(a) or 0
    except Fatal as e:
        print(paint("1;31", "error:"), e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
