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

from . import __version__, guest
from .build import build, export_iso, fetch_step, fetch_tools
from .config import DEFAULT_FILE, Config, sha
from .store import Store, label
from .util import Chan, Fatal, die, du, log, mib, need, output, run, valid, warn
from .vm import Instance, ports, running

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
    builder = name.startswith("_") and (a.cfg.cache / "build" / name).is_dir()
    return Instance(a.cfg, name if builder else valid(name), builder=builder)


# ------------------------------------------------------------------ run instances

def link_iso(cfg: Config, iso: str) -> None:
    """--iso: link the ISO into ${data_dir}/iso, where images without `iso:` find the newest one."""
    src = Path(iso).expanduser().resolve()
    if not src.is_file():
        die(f"no such ISO: {src}")
    dst = cfg.data / "iso" / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.resolve() != src:
        dst.unlink(missing_ok=True)
        dst.symlink_to(src)
        log(f"using {src} (linked as {dst})")


def ensure_disk(cfg: Config, vm: Instance, image: str | None = None) -> None:
    """A new instance gets a disk on its image (built first if needed); an existing one is
    only told when its image has a newer version."""
    store = Store(cfg)
    if vm.disk.exists():
        if image and image != vm.image:
            warn(f"{vm.name} runs on '{vm.image}' - winvm -i {vm.name} reset {image} to switch")
        cur = store.current(vm.image) if vm.image in cfg.images else None
        if vm.image in cfg.images and cur != store.parent(vm.disk):
            warn(f"{vm.name} runs on an older version of '{vm.image}' - "
                 f"winvm -i {vm.name} reset to update it (discards its changes)")
        return
    image = image or vm.settings["image"]
    if image in cfg.images:
        build(cfg, [image])
    vm.create(image, store.require(image))


@command(arg("names", nargs="*", metavar="INSTANCE", help="default: -i / the first configured instance"),
         arg("--all", action="store_true", help="every instance in the config"),
         arg("--image", help="image for a new instance (default: from the config)"),
         arg("--iso", help="Windows ISO for images without `iso:` (remembered in ${data_dir}/iso)"),
         arg("--gui", action="store_true", help="GTK window with clipboard instead of headless"),
         aliases=("start",))
def cmd_up(a):
    """create (building images as needed) and start instances; ready when SSH answers"""
    cfg = a.cfg
    if a.iso:
        link_iso(cfg, a.iso)
    names = list(cfg.instances) if a.all else [valid(n) for n in a.names] or [a.instance]
    for n in names:
        vm = Instance(cfg, n)
        ensure_disk(cfg, vm, a.image)
        vm.start(gui=a.gui)
        log(f"{n} is up - winvm{'' if n == default_instance(cfg) else f' -i {n}'} sh")


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
    inst(a).kill()
    log("killed")


@command(aliases=("status",))
def cmd_ps(a):
    """list instances: state, ports, disk size, image"""
    cfg, store = a.cfg, Store(a.cfg)
    names = sorted({d.parent.name for d in store.disks()} | {vm.name for vm in running(cfg)})
    print(f"{'INSTANCE':<14} {'STATE':<8} {'SSH':>5} {'RDP':>5} {'VNC':>5} {'DISK':>6}  IMAGE")
    for n in names:
        vm = Instance(cfg, n, builder=n.startswith("_"))
        st = vm.state if vm.alive else {}
        col = lambda k: str(st[k]) if st.get(k) else "-"
        on = store.parent(vm.disk) if vm.disk.exists() else None
        image = (label(on) if on else "-") + ("  (outdated)" if on and outdated(cfg, store, vm, on) else "")
        mounts = ", ".join(vm.mounts) if st else ""
        print(f"{n:<14} {'running' if st else 'stopped':<8} {col('ssh'):>5} {col('rdp'):>5} {col('vnc'):>5} "
              f"{du(vm.disk):>6}  {image}" + (f"   mounts: {mounts}" if mounts else ""))


def outdated(cfg: Config, store: Store, vm: Instance, on: Path) -> bool:
    try:
        return vm.image in cfg.images and store.current(vm.image) != on
    except Fatal:
        return False


def default_instance(cfg: Config) -> str:
    return next(iter(cfg.instances), "main")


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
    vm = inst(a).up()
    tty = a.rest[:1] == ["-t"]
    words = a.rest[tty:]
    cwd = vm.guest_path(Path.cwd())
    if not words:                                              # interactive shell
        cd = f"Set-Location -LiteralPath {guest.quote(cwd)}\npowershell -NoLogo" if cwd else None
        os.execvp("ssh", vm.ssh("-t") + ([guest.encoded(cd)] if cd else []))
    script = guest.script(guest.command_line(words), cwd)
    return guest.tty(vm.ssh, script) if tty else guest.pipe(vm.ssh, script)


@command(arg("args", nargs="*", metavar="SSH_ARG", help="extra ssh options, e.g. -L 8080:localhost:80"), raw=True)
def cmd_ssh(a):
    """plain ssh to the instance (for port forwards and other ssh options)"""
    vm = inst(a).up()
    os.execvp("ssh", ["ssh", "-F", str(vm.dir / "ssh_config"), *a.rest, vm.name])


@command(arg("paths", nargs="*", metavar="PATH", help="':' prefix = guest path"), raw=True)
def cmd_cp(a):
    """copy files with scp, e.g. cp a.txt :C:/Users/user/  |  cp :C:/x.log ."""
    vm = inst(a).up()
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
        ensure_disk(a.cfg, vm)
        return vm.start(gui=True, wait=False)
    vnc = vm.state.get("vnc") or die(f"{vm.name} runs headless without VNC - winvm rdp")
    for viewer in ("remote-viewer", "vncviewer"):
        if shutil.which(viewer):
            os.execvp(viewer, [viewer, f"vnc://127.0.0.1:{vnc}" if viewer == "remote-viewer" else f"127.0.0.1:{vnc}"])
    die(f"no VNC viewer found - connect one to 127.0.0.1:{vnc}")


@command(arg("args", nargs="*", metavar="FREERDP_ARG"), raw=True)
def cmd_rdp(a):
    """RDP session with FreeRDP: dynamic resolution, clipboard"""
    vm = inst(a).up()                                          # booted = SSH answers
    port = vm.state.get("rdp") or die("RDP is off (settings.ports.rdp)")
    meta = vm.layer_meta()
    for x in ("xfreerdp3", "xfreerdp", "sdl-freerdp3"):
        if shutil.which(x):
            os.execvp(x, [x, f"/v:127.0.0.1:{port}", f"/u:{meta.get('user', 'user')}",
                          f"/p:{meta.get('password', '')}", "/cert:ignore", "/dynamic-resolution", "+clipboard",
                          *a.rest])
    die("no FreeRDP client found (dnf install freerdp)")


@command(arg("args", nargs="*", metavar="ARG", help="agent command [json] | exec PROGRAM [ARGS]"), raw=True)
def cmd_ga(a):
    """qemu-guest-agent (works without network), e.g. ga guest-info | ga exec ipconfig /all"""
    agent = Chan(inst(a).dir / "qga.sock", qmp=False)
    cmd, *rest = a.rest or ["guest-ping"]
    if cmd != "exec":
        return print(json.dumps(agent(cmd, json.loads(rest[0]) if rest else None), indent=1))
    rest or die("usage: winvm ga exec PROGRAM [ARGS...]")
    pid = agent("guest-exec", {"path": rest[0], "arg": rest[1:], "capture-output": True})["pid"]
    while not (st := agent("guest-exec-status", {"pid": pid})).get("exited"):
        time.sleep(0.2)
    sys.stdout.write(base64.b64decode(st.get("out-data", "")).decode("utf-8", "replace"))
    sys.stderr.write(base64.b64decode(st.get("err-data", "")).decode("utf-8", "replace"))
    return st.get("exitcode", 0)


@command(arg("size", nargs="?", help="e.g. 2G; omit to show"))
def cmd_balloon(a):
    """show or set guest RAM live via virtio-balloon (returns memory to the host)"""
    qmp = Chan(inst(a).dir / "qmp.sock", qmp=True)
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
    st = vm.state if vm.alive else {"ram": mib(vm.settings["ram"]), **ports(a.cfg, vm.state.get("slot", 0))}
    print(shlex.join(vm.qemu_argv(gui=a.gui, st=st)))


# ------------------------------------------------------------------ mounts

@command(arg("path", nargs="?", help="host folder; omit to list the mounts"),
         arg("name", nargs="?", help="share name, guest C:\\mnt\\NAME (default: the folder name)"),
         arg("--ro", action="store_true", help="read-only"))
def cmd_mount(a):
    """mount a host folder into the running instance, at C:\\mnt\\NAME"""
    vm = inst(a)
    if not a.path:
        for n, m in vm.mounts.items():
            print(f"C:\\mnt\\{n:<14} {m['path']}{'  (read-only)' if m['readonly'] else ''}")
        return
    path = Path(a.path).expanduser().resolve()
    path.is_dir() or die(f"not a folder: {path}")
    name = valid(a.name or re.sub(r"[^A-Za-z0-9._-]", "_", path.name))
    vm.mount(name, path, a.ro)
    log(f"{path} -> {vm.name}:C:\\mnt\\{name}")


@command(arg("name"))
def cmd_umount(a):
    """remove a mount from the running instance"""
    inst(a).umount(valid(a.name))
    log(f"unmounted {a.name}")


# ------------------------------------------------------------------ images

@command(arg("images", nargs="*", metavar="IMAGE", help="default: every image in the config"),
         arg("--force", action="store_true", help="rebuild even if up to date (refused while in use)"),
         arg("--gui", action="store_true", help="show the build VM in a window"))
def cmd_build(a):
    """build images (and what they build on) whose definition changed or that are missing"""
    names = [valid(n) for n in a.images] or list(a.cfg.images)
    build(a.cfg, names, force=a.force, gui=a.gui)
    log("images up to date")


@command()
def cmd_images(a):
    """list images: config + built versions, size, what each builds on, who uses it"""
    cfg, store = a.cfg, Store(a.cfg)
    print(f"{'IMAGE':<22} {'STATUS':<10} {'SIZE':>6}  {'ON':<22} USED BY")
    shown = set()
    for name in list(cfg.images) + sorted({store.meta(f)["name"] for f in store.all()} - set(cfg.images)):
        try:
            cur = store.current(name)
        except Fatal:
            cur = None
        versions = store.versions(name)
        if name in cfg.images and not cur:
            on = cfg.parent(name) or "iso"
            print(f"{name:<22} {'not built':<10} {'-':>6}  {on:<22}")
        for f in versions:
            meta = store.meta(f)
            status = "saved" if meta.get("saved") else "current" if f == cur else "old"
            print(f"{label(f):<22} {status:<10} {du(f):>6}  {meta.get('from') or 'iso':<22} "
                  f"{' '.join(store.users(f))}")
            shown.add(f)


@command(arg("--cache", action="store_true", help="also the download and slim-image cache"))
def cmd_prune(a):
    """delete image versions nothing uses (old or no longer in the config), build leftovers"""
    cfg, store = a.cfg, Store(a.cfg)
    keep = set()
    for name in cfg.images:
        try:
            if f := store.current(name):
                keep.add(f)
        except Fatal:
            pass
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
    for d in (cfg.cache / "build").glob("*/"):
        if d.name not in live:
            shutil.rmtree(d)
    if a.cache:
        for d in ("downloads", "slim"):
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
    log(f"removed image '{a.name}'" + (" (still in the config: rebuilt when needed)" if a.name in a.cfg.images else ""))


@command(arg("name", help="new image name"))
def cmd_save(a):
    """freeze the instance's disk as image NAME (compressed, read-only) and continue on it"""
    cfg, store, vm, name = a.cfg, Store(a.cfg), inst(a), valid(a.name)
    vm.require_stopped()
    vm.disk.exists() or die(f"{vm.name} has no disk")
    if name in cfg.images:
        die(f"'{name}' is defined in the config - pick another name for a saved image")
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
    image = valid(a.image) if a.image else vm.image
    if image in cfg.images:
        build(cfg, [image])
    vm.create(image, Store(cfg).require(image))


@command()
def cmd_rm(a):
    """delete the instance: its disk (unsaved changes!) and run folder"""
    vm = inst(a)
    vm.require_stopped()
    shutil.rmtree(vm.home, ignore_errors=True)
    shutil.rmtree(vm.dir, ignore_errors=True)
    log(f"removed instance {vm.name}")


@command(arg("image", nargs="?", default="base"), arg("-o", "--output", help="default: ./IMAGE.iso"))
def cmd_iso(a):
    """write a standalone installer ISO of an ISO image (its slim image + payload)"""
    out = Path(a.output or f"{a.image}.iso").resolve()
    export_iso(a.cfg, valid(a.image), out)
    log(f"{out} ({du(out)}) - it wipes disk 0 without asking and refuses to run outside QEMU")


@command(arg("image", nargs="?", default="base"))
def cmd_editions(a):
    """list the editions inside an image's Windows ISO"""
    need("7z", "wimlib-imagex")
    iso, tmp = a.cfg.iso(valid(a.image)), a.cfg.cache / "build/editions"
    shutil.rmtree(tmp, ignore_errors=True)
    image = "sources/install.wim" if "install.wim" in output("7z", "l", iso, "sources/install.*") \
        else "sources/install.esd"
    run("7z", "x", "-y", f"-o{tmp}", iso, image, quiet=True)
    info = output("wimlib-imagex", "info", tmp / image)
    shutil.rmtree(tmp)
    for idx, name in re.findall(r"^Index:\s+(\d+).*?^Name:\s+(.+?)$", info, re.M | re.S):
        print(f"  {idx}: {name}")


@command()
def cmd_fetch(a):
    """download everything pinned: virtio-win, OpenSSH, the images' download steps"""
    for f in fetch_tools(a.cfg).values():
        log(f"ok: {f.name}")
    for name in a.cfg.images:
        for st in a.cfg.steps(name):
            if "download" in st:
                log(f"ok: {fetch_step(a.cfg, st).name}")


# ------------------------------------------------------------------ config

@command(arg("what", choices=["dump", "show", "path"], nargs="?", default="path",
             help="dump: the default config with comments; show: the merged config; path: files in use"))
def cmd_config(a):
    """the configuration: `config dump > winvm.yaml` to start your own"""
    cfg = a.cfg
    if a.what == "dump":
        sys.stdout.write(DEFAULT_FILE.read_text())
    elif a.what == "show":
        import yaml
        print(yaml.safe_dump(cfg.raw, sort_keys=False, width=120), end="")
    else:
        print(f"config:  {cfg.file or '(none - built-in defaults: winvm config dump)'}")
        print(f"data:    {cfg.data}\ncache:   {cfg.cache}\nruntime: {cfg.runtime}")


# ------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(
        prog="winvm", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Build and run slim, unattended Windows VMs on QEMU/KVM, from one YAML file.",
        epilog="quick start:  winvm up --iso ~/Downloads/Win11_*.iso   then   winvm sh")
    p.add_argument("-f", "--file", metavar="YAML", help="config file (default: ./winvm.yaml, ~/.config/winvm/winvm.yaml)")
    p.add_argument("-i", "--instance", metavar="NAME", help="instance to act on (default: the first configured)")
    p.add_argument("--data-dir", metavar="DIR", help="images and instances (default: ~/.local/share/winvm)")
    p.add_argument("--cache-dir", metavar="DIR", help="downloads and build scratch (default: ~/.cache/winvm)")
    p.add_argument("-V", "--version", action="version", version=f"winvm {__version__}")
    sub = p.add_subparsers(title="commands", metavar="<command>")
    for name, (fn, args, aliases) in COMMANDS.items():
        doc = fn.__doc__ or ""
        sp_ = sub.add_parser(name, aliases=aliases, help=doc.splitlines()[0], description=doc,
                             formatter_class=argparse.RawDescriptionHelpFormatter)
        for flags, kw in args:
            sp_.add_argument(*flags, **kw)
        sp_.set_defaults(fn=fn)

    # raw commands get everything after their name untouched (PowerShell, ssh, scp arguments)
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in ("-i", "--instance", "-f", "--file", "--data-dir", "--cache-dir") else 1
    if i < len(argv) and argv[i] in RAW:
        a = p.parse_args(argv[: i + 1])
        a.rest = argv[i + 1:]
    else:
        a = p.parse_args(argv)
    if not hasattr(a, "fn"):
        p.print_help()
        return 1
    try:
        a.cfg = Config(a.file, a.data_dir, a.cache_dir)
        a.instance = a.instance or default_instance(a.cfg)
        return a.fn(a) or 0
    except Fatal as e:
        print(f"\033[1;31merror:\033[0m {e}" if sys.stderr.isatty() else f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
