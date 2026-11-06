"""The `winvm` command line."""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import select
import shutil
import signal
import stat
import subprocess as sp
import sys
import time
from pathlib import Path

import yaml

from . import __version__, desktop, guest, host
from .build import build, fetch_step
from .config import BUILTIN, DEFAULT_FILE, Config, is_path, sha
from .media import editions
from .store import Store, label
from .util import WAIT, Chan, Fatal, TimedOut, die, du, log, mib, mount_point, paint, seconds, start_timeout, stop_timeout, timing, valid, warn, win_path
from .vm import Instance, ports, running, smbd as vm_smbd

COMMANDS: dict[str, tuple] = {}
RAW = set()   # commands whose arguments are passed through verbatim (PowerShell, ssh, scp, ...)
OWN_TIMEOUT = ("stop", "restart", "wait")   # their --timeout or SECONDS: when to power off, how long to wait


def arg(*flags, **kw):
    """An argparse argument; names="instances"/"images"/"mounts": shell completion offers those."""
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


def confirm(a, question: str) -> None:
    """Ask before deleting something (in a terminal; -y, or no terminal: go ahead)."""
    if a.yes or not sys.stdin.isatty():
        return
    try:
        ok = input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        ok = False
    if not ok:
        die("nothing changed")


def allow_reboot(a, vm: Instance) -> None:
    """Rebooting a running VM to log on could interrupt what it is doing: only when asked,
    so never unnoticed from a script."""
    if a.yes:
        return
    if not sys.stdin.isatty():
        die(f"{vm.name} is running and not logged on, and logging on reboots it. Allow that with -y, "
            f"or `winvm -i {vm.name} up --desktop` while it is stopped")
    try:
        ok = input(f"{vm.name} is running and not logged on. Reboot it to log on? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        ok = False
    if not ok:
        die("nothing changed")


def hand_over(argv: list[str]) -> None:
    """Become this program; under --timeout stay around, so that the limit still holds."""
    if timing():
        sys.exit(sp.call(argv))
    os.execvp(argv[0], argv)


def own_limit(a) -> int | None:
    """stop, restart: what --timeout says, if it says anything."""
    try:
        return seconds(a.timeout) if a.timeout else None
    except ValueError as e:
        die(f"--timeout: {e}")


def instances_named(a, names: list[str]) -> list[Instance]:
    """Instances named on the command line, else the -i one."""
    return [Instance(a.cfg, valid(n)) for n in names] if names else [inst(a)]


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
        if run:                                                # (None: back to the image's value)
            own = {k: v for k, v in {**vm.meta.get("run", {}), **run}.items() if v is not None}
            vm.meta = {**vm.meta, "run": own}
        if image and image != vm.image:
            warn(f"{vm.name} runs on '{vm.image}' - winvm -i {vm.name} reset {image} to switch")
        if outdated(cfg, store, vm, store.parent(vm.disk)):
            warn(f"{vm.name} runs on an older version of '{vm.image}' - "
                 f"winvm -i {vm.name} reset to update it (discards its changes)")
        return
    host.require(cfg)                                          # before creating anything
    image = image or (vm.name if vm.name in cfg.images or store.versions(vm.name) else "base")
    if image in cfg.images:
        build(cfg, [image])
    vm.create(image, store.require(image), {k: v for k, v in (run or {}).items() if v is not None})


@command(arg("names", nargs="*", metavar="NAME", names="instances", help="instance, or an image file (instance named like it); "
                                                     "default: -i, else main"),
         arg("--image", metavar="IMAGE", names="images", help="image (name or file) for a new instance; default: the "
                                              "image named like the instance, else base"),
         arg("--ram", help="e.g. 8G, or 2G-8G: dynamic memory between the two; kept for this instance "
                           "(`default`: back to the image's)"),
         arg("--cpus", help="kept for this instance (`default`: back to the image's)"),
         arg("--all", action="store_true", help="every existing instance"),
         arg("--iso", help="Windows ISO for images without `iso:` (remembered in ${data_dir}/iso)"),
         arg("--gui", action="store_true", help="GTK window with clipboard instead of headless"),
         arg("--desktop", nargs="?", const="on", metavar="off|default", help="log on by itself at every boot, without a window; "
                                                     "kept for this instance (`default`: back to the image's)"),
         aliases=("start",))
def cmd_up(a):
    """create (building images as needed) and start instances; ready when SSH answers, in desktop mode once logged on"""
    cfg, store = a.cfg, Store(a.cfg)
    if a.iso:
        link_iso(cfg, a.iso)
    ram, cpus = a.ram, a.cpus
    if ram and "-" in ram:                                     # 2G-8G: dynamic memory
        lo, hi = ram.split("-", 1)
        ram = {"min": lo, "max": hi}
    for size in (ram.values() if isinstance(ram, dict) else [ram] if ram and ram != "default" else []):
        mib(size)                                              # fails early on a bad size
    if cpus and cpus != "default" and not cpus.isdigit():
        die(f"--cpus: a number, or `default` (got {cpus!r})")
    run = {k: None if v == "default" else int(v) if k == "cpus" else v
           for k, v in (("ram", ram), ("cpus", cpus)) if v}
    desk = a.desktop
    if desk not in (None, "on", "off", "default"):             # `up --desktop work`: an instance
        a.names, desk = [desk, *a.names], "on"
    if desk:
        run["desktop"] = {"on": True, "off": False, "default": None}[desk]
    image = cfg.ref(a.image) if a.image else None
    names = [d.parent.name for d in store.disks()] if a.all else \
        [cfg.ref(n) if is_path(n) else valid(n) for n in a.names or [a.instance]]
    if not all(Instance(cfg, n).alive for n in names):
        host.require(cfg, a.gui)                               # before building or creating
    for n in names:
        vm = Instance(cfg, n)
        if run.keys() - {"desktop"} and vm.alive:
            warn(f"{n} is running: the new settings apply from its next start (winvm -i {n} restart)")
        reboot = False
        if desk == "on" and vm.alive:                          # before the setting is kept
            vm.wait_ssh()
            if not any(desktop.status(vm)):
                allow_reboot(a, vm)
            reboot = True
        before = vm.meta
        ensure_disk(cfg, vm, image, run)
        try:
            vm.start(gui=a.gui, reboot=reboot)
        except (Fatal, TimedOut):
            if desk == "on":                                   # it couldn't log on: nothing changes
                vm.meta = before or {**vm.meta, "run": {k: v for k, v in vm.meta.get("run", {}).items() if k != "desktop"}}
            raise
        log(f"{n} is up - winvm{'' if n == 'main' else f' -i {n}'} sh")


@command(arg("names", nargs="*", metavar="INSTANCE", names="instances"),
         arg("--all", action="store_true", help="every running instance"),
         arg("--timeout", metavar="TIME", default=argparse.SUPPRESS, help="time before a hard power-off (default 2 min, emulated 10)"),
         aliases=("down",))
def cmd_stop(a):
    """graceful shutdown (ACPI), all at once; hard power-off after --timeout"""
    vms = [vm for vm in running(a.cfg) if not vm.builder] if a.all else instances_named(a, a.names)
    t0, limit = time.time(), own_limit(a)
    for vm in [vm for vm in vms if vm.power_button()]:
        vm.wait_stopped(limit or vm.power_off_after, since=t0)


@command(arg("names", nargs="*", metavar="INSTANCE", names="instances"),
         arg("--timeout", metavar="TIME", default=argparse.SUPPRESS, help="time before a hard power-off (default 2 min, emulated 10)"))
def cmd_restart(a):
    """stop (ACPI) and start again, e.g. for new --ram/--cpus or mounts"""
    vms = instances_named(a, a.names)
    for vm in vms:
        vm.disk.exists() or die(f"no instance '{vm.name}' (winvm ps)")
    t0, limit = time.time(), own_limit(a)
    accel = {vm.name: vm.state.get("accel") for vm in vms if vm.alive}   # (emulated stays emulated)
    asked = a.cfg.accel_arg
    for vm in [vm for vm in vms if vm.power_button()]:
        vm.wait_stopped(limit or vm.power_off_after, since=t0)
    for vm in vms:
        ensure_disk(a.cfg, vm)                                 # (tells if its image is outdated)
        a.cfg.accel_arg = asked or accel.get(vm.name)
        vm.start()
        log(f"{vm.name} is up")


@command(arg("names", nargs="*", metavar="INSTANCE", names="instances"))
def cmd_kill(a):
    """hard power-off (unsaved work in the guest is lost)"""
    for vm in instances_named(a, a.names):
        log(f"killed {vm.name}" if vm.kill() else f"{vm.name} not running")


@command(arg("-q", "--quiet", action="store_true", help="names only"),
         arg("--json", action="store_true", help="machine-readable"), aliases=("status",))
def cmd_ps(a):
    """list instances: state, ports, disk size, image"""
    cfg, store = a.cfg, Store(a.cfg)
    names = sorted({d.parent.name for d in store.disks()} | {vm.name for vm in running(cfg)})
    rows = []
    for n in names:
        vm = Instance(cfg, n, builder=n.startswith("_"))
        st = vm.state if vm.alive else {}
        on = store.parent(vm.disk) if vm.disk.exists() else None
        rows.append({"name": n, "state": "running" if st else "stopped",
                     **{k: st.get(k) for k in ("ssh", "rdp", "vnc")},
                     "disk": vm.disk.stat().st_blocks * 512 if vm.disk.exists() else 0,
                     "ram": ram_text(vm, st), "image": label(on) if on else None,
                     "outdated": bool(on and outdated(cfg, store, vm, on)),
                     "mounts": {m: mount_point(m, v) for m, v in vm.mounts.items()} if st else {}})
    if a.json:
        return print(json.dumps(rows, indent=1))
    if a.quiet:
        return print("\n".join(names)) if names else None
    if not rows:
        return print("no instances yet - winvm up (creates `main`)")
    print(f"{'INSTANCE':<14} {'STATE':<8} {'SSH':>5} {'RDP':>5} {'VNC':>5} {'DISK':>6} {'RAM':>9}  IMAGE")
    for r in rows:
        ssh, rdp, vnc = (str(r[k] or "-") for k in ("ssh", "rdp", "vnc"))
        image = (r["image"] or "-") + ("  (outdated)" if r["outdated"] else "")
        mounts = ", ".join(r["mounts"])
        print(f"{r['name']:<14} {r['state']:<8} {ssh:>5} {rdp:>5} {vnc:>5} {human_size(r['disk']):>6} "
              f"{r['ram']:>9}  {image}" + (f"   mounts: {mounts}" if mounts else ""))


@command()
def cmd_info(a):
    """one instance in detail: image, its own settings, what is in effect, ports, files"""
    cfg, store, vm = a.cfg, Store(a.cfg), inst(a)
    vm.disk.exists() or die(f"no instance '{vm.name}' (winvm ps)")
    on = store.parent(vm.disk)
    st = vm.state if vm.alive else {}
    eff = vm.settings
    print(f"instance  {vm.name}  ({'running' if st else 'stopped'})")
    print(f"image     {label(on)}" + ("  (outdated: winvm -i NAME reset updates it, discarding changes)".replace("NAME", vm.name)
                                       if outdated(cfg, store, vm, on) else "")
          + (f"  from {tilde(cfg.sources[vm.image])}" if vm.image in cfg.sources else ""))
    print(f"disk      {du(vm.disk)} of changes  ({vm.disk})")
    own = vm.meta.get("run") or {}
    print(f"ram       {ram_text(vm, st)}" + ("  (own setting; `winvm up --ram default` drops it)" if "ram" in own else ""))
    print(f"cpus      {eff.get('cpus')}" + ("  (own setting; `winvm up --cpus default` drops it)" if "cpus" in own else ""))
    if eff.get("desktop") or "desktop" in own:
        print(f"desktop   {'logs on at every boot' if eff.get('desktop') else 'off'}"
              + ("  (own setting; `winvm up --desktop default` drops it)" if "desktop" in own else ""))
    mounts = vm.mounts if st else eff["mounts"]
    for i, (n, m) in enumerate(mounts.items()):
        saved = "  (saved with this instance)" if n in (own.get("mounts") or {}) else ""
        print(f"{'mounts' if i == 0 else '':<9} {mount_point(n, m)} <- {m['path']}{'  (read-only)' if m['readonly'] else ''}{saved}")
    if st:
        print(f"ports     ssh 127.0.0.1:{st.get('ssh')}" + (f", rdp :{st['rdp']}" if st.get("rdp") else "")
              + (f", vnc :{st['vnc']}" if st.get("vnc") else ""))
    print(f"run dir   {vm.dir}  (winvm -i {vm.name} logs)")


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

@command(arg("-t", action="store_true", help="a terminal, for interactive programs"),
         arg("--desktop", action="store_true", help="run CMD in the logged-on desktop session"),
         arg("--timeout", metavar="TIME", default=argparse.SUPPRESS, help="give up after this long, starting the VM included (90s, 30m, 2h): exit 124"),
         arg("cmd", nargs="*", metavar="CMD", help="PowerShell command line (as typed at the prompt)"),
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
    winvm sh --desktop CMD            in the logged-on desktop session (COM, winget, DPAPI)
    winvm sh --timeout 5m CMD         give up on it after 5 minutes: exit 124

    The exit code is the command's. Run from inside a mounted host folder, the guest
    command runs in the matching C:\\mnt\\... folder.

    --desktop reboots a VM that isn't logged on, which it asks about in a terminal and refuses
    from a script, unless -y or the VM was stopped. Output is text, with stderr mixed in;
    stdin is not passed on. --timeout ends the command and what it started, but not what
    Windows started for it (Store apps, COM servers).
    """
    rest, tty, on_desktop, limit = list(a.rest), False, False, a.limit
    while rest:                                                # (only before the command)
        w = rest[0]
        if w == "-t":
            tty = True
        elif w == "--desktop":
            on_desktop = True
        elif w in ("-y", "--yes"):
            a.yes = a.cfg.yes = True
        elif w in ("-i", "--instance", "--timeout", "--accel") and len(rest) == 1:
            die(f"{w} needs a value")
        elif w in ("-i", "--instance"):
            a.instance = rest.pop(1)
        elif w == "--accel":
            a.cfg.accel_arg = rest.pop(1)
        elif w == "--timeout" or w.startswith("--timeout="):
            value = rest.pop(1) if w == "--timeout" else w.partition("=")[2]
            try:
                limit = seconds(value)
            except ValueError as e:
                die(f"--timeout: {e}")
        elif w == "--":
            rest.pop(0)
            break
        else:
            break
        rest.pop(0)
    words = rest
    if not words and (on_desktop or limit):
        die(f"{'--desktop' if on_desktop else '--timeout'} needs a command: winvm sh {'--desktop' if on_desktop else '--timeout 5m'} CMD"
            + (" (to work on the desktop yourself: winvm gui, or winvm rdp)" if on_desktop else ""))
    if tty and (on_desktop or limit):
        die(f"-t doesn't go with {'--desktop' if on_desktop else '--timeout'}")
    if on_desktop:
        check_no_stdin()
    if limit:
        start_timeout(limit)                                   # it covers starting the VM too
    vm = existing(a)
    fresh = not vm.alive
    vm.up()
    cwd = vm.guest_path(Path.cwd())
    if on_desktop:
        return sh_desktop(a, vm, guest.command_line(words), cwd, limit, fresh)
    left = stop_timeout() if limit else None                   # from here on it ends the command itself
    if not words:                                              # interactive shell
        # still a shell if the folder can't be entered: to find out why, for one
        cd = (f"try {{ Set-Location -LiteralPath {guest.quote(cwd)} -ErrorAction Stop }} catch {{ [Console]::Error.WriteLine("
              f"\"warn: can't enter {cwd.replace(chr(34), '').replace('$', '`$')}, starting in $((Get-Location).Path)\") }}\n"
              "powershell -NoLogo") if cwd else None
        return guest.terminal(vm.ssh("-t") + ([guest.encoded(cd)] if cd else []))
    pid_file = rf"C:\ProgramData\winvm\sh-{os.getpid()}.pid" if limit else None
    script = guest.script(guest.command_line(words), cwd, console=tty, pid_file=pid_file)
    if tty:
        return guest.tty(vm.ssh, script)
    rc = guest.pipe(vm.ssh, script, timeout=left)
    if rc == guest.TIMED_OUT:
        guest.end(vm.ssh, pid_file)
        print(paint("1;31", "error:"), f"gave up on the command after {limit} s and ended it", file=sys.stderr)
        return guest.EXIT_TIMED_OUT
    return rc


def check_no_stdin() -> None:
    """--desktop has no way to pass stdin on: better an error than a command that sees nothing."""
    try:
        st = os.fstat(0)
        if stat.S_ISREG(st.st_mode):
            waiting = st.st_size > os.lseek(0, 0, os.SEEK_CUR)
        elif stat.S_ISFIFO(st.st_mode) or stat.S_ISSOCK(st.st_mode):   # a pipe nobody writes to is fine
            waiting = bool(select.select([0], [], [], 0.1)[0] and os.read(0, 1))
        else:
            waiting = False
    except OSError:
        return
    if waiting:
        die("winvm sh --desktop can't pass stdin on to the command: copy the data in with winvm cp, "
            "or add </dev/null if the command doesn't need it")


def sh_desktop(a, vm: Instance, code: str, cwd: str | None, limit: int | None, fresh: bool) -> int:
    """`winvm sh --desktop`: see cmd_sh."""
    armed, on = desktop.status(vm)
    if not on:
        if armed:                                              # it is still booting
            desktop.wait_logon(vm, f"{vm.name} in desktop mode")
        else:
            if not fresh:
                allow_reboot(a, vm)
            before = vm.meta
            ensure_disk(a.cfg, vm, run={"desktop": True})
            try:
                desktop.sync(vm, may_reboot=True)
            except (Fatal, TimedOut):                          # it couldn't log on: nothing changes
                vm.meta = before
                raise
            log(f"desktop mode is on for {vm.name} from now on (`winvm -i {vm.name} up --desktop default` drops it)")

    def stopped(*_):                                           # `kill`, `timeout`: as Ctrl-C does
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stopped)
    rc, note = desktop.run(vm, code, stop_timeout() if limit else None, cwd=cwd)
    if rc == guest.TIMED_OUT:
        print(paint("1;31", "error:"), f"gave up on the command after {limit} s and ended it{note}", file=sys.stderr)
        return guest.EXIT_TIMED_OUT
    if rc == desktop.VANISHED:
        print(paint("1;31", "error:"), f"the command ended without an exit code: it probably rebooted the VM or "
              f"logged the user off{note}", file=sys.stderr)
        return 1
    if rc == desktop.LOST:
        print(paint("1;31", "error:"), "lost the connection to the VM: the command probably rebooted it", file=sys.stderr)
        return 255
    return rc


@command(arg("--timeout", metavar="TIME", default=argparse.SUPPRESS, help="give up after this long (90s, 30m, 2h): exit 124"), arg("args", nargs="*", metavar="ARG", help="ssh options, then an optional remote command"), raw=True)
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
    hand_over(["ssh", "-F", str(vm.dir / "ssh_config"), *opts, vm.name, *rest])


@command(arg("--timeout", metavar="TIME", default=argparse.SUPPRESS, help="give up after this long (90s, 30m, 2h): exit 124"), arg("paths", nargs="*", metavar="PATH", help="':' prefix = guest path"), raw=True)
def cmd_cp(a):
    """copy files with scp, e.g. cp a.txt :C:/Users/user/  |  cp :C:/x.log ."""
    vm = ready(a)
    paths = [f"{vm.name}{p}" if p.startswith(":") else p for p in a.rest]
    hand_over(["scp", "-F", str(vm.dir / "ssh_config"), "-r", *paths])


@command(arg("seconds", nargs="?", metavar="SECONDS", help="how long to wait (default 600)"),
         arg("--timeout", metavar="TIME", default=argparse.SUPPRESS, help="the same"))
def cmd_wait(a):
    """wait until SSH in the guest answers"""
    try:
        limit = seconds(a.seconds or a.timeout or WAIT)
    except ValueError as e:
        die(f"wait: {e}")
    inst(a).wait_ssh(limit)


@command()
def cmd_gui(a):
    """open a VNC viewer on a running instance, or start it with a GTK window"""
    vm = inst(a)
    if not vm.alive:
        host.require(a.cfg, gui=True)
        return existing(a).start(gui=True)
    vnc = vm.state.get("vnc") or die(f"{vm.name} runs headless without VNC - winvm rdp")
    for viewer in VNC_VIEWERS:
        if shutil.which(viewer):
            hand_over([viewer, f"vnc://127.0.0.1:{vnc}" if viewer == "remote-viewer" else f"127.0.0.1:{vnc}"])
    die(f"no VNC viewer found - connect one to 127.0.0.1:{vnc}, or {host.install_hint('vnc')}")


@command(arg("--timeout", metavar="TIME", default=argparse.SUPPRESS, help="give up after this long (90s, 30m, 2h): exit 124"), arg("args", nargs="*", metavar="FREERDP_ARG"), raw=True)
def cmd_rdp(a):
    """RDP session with FreeRDP: dynamic resolution, clipboard"""
    vm = ready(a)                                          # booted = SSH answers
    port = vm.state.get("rdp") or die("RDP is off (settings.ports.rdp)")
    for x in RDP_CLIENTS:
        if shutil.which(x):
            hand_over([x, f"/v:127.0.0.1:{port}", f"/u:{desktop.user_of(vm)}",
                          f"/p:{desktop.password_of(vm)}", "/cert:ignore", "/dynamic-resolution", "+clipboard",
                          *a.rest])
    die(f"no FreeRDP client found - {host.install_hint('rdp')}")


@command(arg("--timeout", metavar="TIME", default=argparse.SUPPRESS, help="give up after this long (90s, 30m, 2h): exit 124"), arg("args", nargs="*", metavar="ARG", help="agent command [json] | exec PROGRAM [ARGS]"), raw=True)
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


LOGS = {"serial": "serial.log", "balloon": "balloon.log", "smb": "smb/log.smbd", "qemu": "qemu.log"}


@command(arg("which", nargs="?", choices=list(LOGS), help="omit to list the logs"),
         arg("-f", "--follow", action="store_true", help="keep printing as it grows"))
def cmd_logs(a):
    """an instance's logs: serial (installer progress), balloon (dynamic memory), smb, qemu (--gui)"""
    vm = inst(a)
    if not a.which:
        found = [(k, vm.dir / f) for k, f in LOGS.items() if (vm.dir / f).exists()]
        if not found:
            die(f"no logs for {vm.name} in {vm.dir} (made while it runs)")
        for k, f in found:
            print(f"{k:<8} {du(f):>6}  {f}")
        return
    f = vm.dir / LOGS[a.which]
    f.exists() or die(f"no {a.which} log for {vm.name} ({f})")
    if a.follow:
        hand_over(["tail", "-n", "50", "-F", str(f)])
    sys.stdout.write(f.read_text(errors="replace"))


# ------------------------------------------------------------------ mounts

@command(arg("path", nargs="?", help="host folder; omit to list the mounts"),
         arg("where", nargs="?", metavar="NAME|WINPATH",
             help="NAME: at C:\\mnt\\NAME (default: the folder's name); or a Windows path, e.g. C:\\work"),
         arg("--ro", action="store_true", help="read-only"),
         arg("--save", action="store_true", help="keep it for this instance: set up at every start"))
def cmd_mount(a):
    """mount a host folder into the running instance, at C:\\mnt\\NAME or any Windows path

    winvm mount ~/code/proj                C:\\mnt\\proj
    winvm mount ~/code/proj src            C:\\mnt\\src
    winvm mount ~/code/proj 'C:\\work'      C:\\work (share name: work)
    winvm mount ~/code/proj --save         also at every later start (works when stopped)
    winvm mount                            list the mounts
    """
    vm = inst(a)
    if not a.path:                                             # stopped: what the next start sets up
        for n, m in (vm.mounts if vm.alive else vm.settings["mounts"]).items():
            print(f"{mount_point(n, m):<24} {m['path']}{'  (read-only)' if m['readonly'] else ''}")
        return
    path = Path(a.path).expanduser().resolve()
    if not path.is_dir():
        die(f"not a folder: {path}")
    at = None
    if a.where and re.match(r"[A-Za-z]:", a.where):            # a Windows path: the link goes there
        at = win_path(a.where) or die(f"not a Windows folder path: {a.where} (e.g. C:\\work)")
    if a.where and not at:
        name = valid(a.where)
    else:
        last = at.rsplit("\\", 1)[1] if at else path.name
        name = valid(re.sub(r"[^A-Za-z0-9._-]", "_", last).lstrip("._-") or "mount")
    m = {"path": str(path), "readonly": a.ro, **({"at": at} if at else {})}
    if a.save:
        vm.disk.exists() or die(f"no instance '{vm.name}' (winvm ps)")
    spot = vm.mount(name, path, a.ro, at) if vm.alive or not a.save else mount_point(name, m)
    if a.save:
        own = vm.meta.get("run") or {}
        vm.meta = {**vm.meta, "run": {**own, "mounts": {**(own.get("mounts") or {}), name: m}}}
    log(f"{path} -> {vm.name}:{spot}" + ("  (saved)" if a.save and vm.alive else
                                           "  from its next start" if a.save else ""))


@command(arg("which", metavar="NAME|WINPATH", names="mounts"),
         arg("--save", action="store_true", help="also not at later starts"))
def cmd_umount(a):
    """remove a mount from the running instance, by name or where it is (C:\\work)"""
    vm = inst(a)
    if a.save:
        vm.disk.exists() or die(f"no instance '{vm.name}' (winvm ps)")
        mounts = vm.mounts if vm.alive else vm.settings["mounts"]
        w = a.which.replace("/", "\\").rstrip("\\").lower()
        name = a.which if a.which in mounts else next((n for n, m in mounts.items() if mount_point(n, m).lower() == w), None)
        name or die(f"{vm.name} has no mount '{a.which}' (`winvm -i {vm.name} mount` lists them)")
        own = vm.meta.get("run") or {}
        theirs = vm.cfg.run_settings(vm.image)["mounts"]           # from the image / machine settings
        own_mounts = {k: v for k, v in (own.get("mounts") or {}).items() if k != name}
        if name in theirs:
            own_mounts[name] = None                            # drop the image's mount
        vm.meta = {**vm.meta, "run": {**own, "mounts": own_mounts}}
        if not vm.alive:
            return log(f"{name} is not mounted from the next start on")
        a.which = name
    log(f"unmounted {vm.umount(a.which)}" + ("  (saved)" if a.save else ""))


# ------------------------------------------------------------------ images

@command(arg("images", nargs="*", metavar="IMAGE", names="images", help="name or image file; default: every image winvm knows"),
         arg("--force", action="store_true", help="rebuild even if up to date (refused while in use)"),
         arg("--gui", action="store_true", help="show the build VM in a window"))
def cmd_build(a):
    """build images (and what they build on) whose definition changed or that are missing"""
    names = [a.cfg.ref(n) for n in a.images] or list(a.cfg.images)
    for n, why in a.cfg.broken.items():
        if not a.images:
            warn(f"skipping '{n}': {why}")
    chain = {x for n in names for x in a.cfg.chain(n)}
    for n in chain:
        try:
            a.cfg.hash(n)                                      # finds the stale ISO images
        except Fatal:
            pass
    if a.force:
        for n in names:
            confirm(a, f"build '{n}' again? The built version stays until the new one is done. "
                       f"{rebuild_note(a.cfg, n)}")
    if stale := sorted(chain & set(a.cfg.stale) - (set(names) if a.force else set())):
        confirm(a, f"{', '.join(stale)} changed since built (its file or winvm): install Windows again "
                   f"(up to an hour; instances keep the version they run on)?")
    build(a.cfg, names, force=a.force, gui=a.gui, stale=True)
    log("images up to date")


def rebuild_note(cfg: Config, name: str) -> str:
    """What building image `name` takes: said before deleting or replacing a built one."""
    img = cfg.images.get(name)
    if not img:
        return "It has no file, so it can't be built again."
    if "from" in img:
        return f"It builds from {tilde(cfg.sources[name])}."
    try:
        return f"Building it installs Windows from {cfg.iso(name).name} (up to an hour)."
    except Fatal:
        pin = cfg.s.get("windows")
        if img.get("iso") or not pin:
            return "NO ISO IS LEFT for it: you need one first (winvm up --iso PATH), then up to an hour."
        return (f"NO ISO IS LEFT for it: building it downloads {pin.get('title', 'Windows')} "
                f"({pin.get('size', 0) / (1 << 30):.1f} GB), maybe not the Windows you built it from, "
                f"then installs it (up to an hour).")


@command(arg("--json", action="store_true", help="machine-readable"))
def cmd_images(a):
    """list images: their file, built versions, size, what each builds on, who uses it"""
    cfg, store = a.cfg, Store(a.cfg)
    rows = [("IMAGE", "STATUS", "SIZE", "ON", "USED BY", "DEFINED IN")]
    data = []
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
            data.append({"image": name, "status": "not built", "file": str(src) if src else None})
        for f in store.versions(name):
            meta = store.meta(f)
            status = "saved" if meta.get("saved") else "current" if f == cur else "old"
            if f == cur and name in cfg.stale:
                status = "outdated"                            # in use until `winvm build NAME`
            rows.append((label(f), status, du(f), meta.get("from") or "iso", " ".join(store.users(f)),
                         where if f == cur or status == "saved" else ""))
            data.append({"image": label(f), "name": name, "status": status, "size": f.stat().st_blocks * 512,
                         "from": meta.get("from"), "used_by": store.users(f),
                         "file": str(src) if src and (f == cur or status == "saved") else None})
    if a.json:
        return print(json.dumps(data, indent=1))
    for n, why in cfg.broken.items():
        rows.append((n, "broken", "-", "-", "", why[:100]))
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r[:5], widths)) + "  " + r[5])
    if cfg.stale:
        print(f"\noutdated: changed since built (its file or winvm), still used - `winvm build "
              f"{' '.join(cfg.stale)}` installs again")


@command(arg("--cache", action="store_true", help="also the downloads and all slim images (re-fetched when needed)"),
         arg("--iso", action="store_true", help="also the ISOs in the data folder: built images keep working, "
                                                "building one again needs an ISO (links: only the link goes)"),
         arg("-n", "--dry-run", action="store_true", help="only show what would go"))
def cmd_prune(a):
    """delete image versions nothing uses, stale slim images, build leftovers"""
    cfg, store = a.cfg, Store(a.cfg)
    if a.iso and not a.dry_run and any(vm.builder for vm in running(cfg)):
        die("a build is running and may be using an ISO - run this when it is done")
    keep = set()
    for name in {store.meta(f)["name"] for f in store.all()} | set(cfg.images):
        try:
            if cur := store.current(name):                     # (an image without its file: newest)
                keep.add(cur)
        except Fatal as e:                                     # can't tell which version is current
            if newest := store.versions(name)[:1]:
                warn(f"{e} - keeping {label(newest[0])}")
                keep.update(newest)
    keep |= {f for f in store.all() if store.meta(f).get("saved")}
    gone: list[Path] = []
    removed = True
    while removed:                                             # removing a layer may free the one below
        removed = False
        for f in store.all():
            if f not in keep and f not in gone and not [u for u in store.users(f) if not any(label(g) == u for g in gone)]:
                gone.append(f)
                removed = True
    live = {vm.name for vm in running(cfg)}
    extra = [d for d in cfg.build.glob("*") if d.is_dir() and d.name not in live]
    if a.cache:
        extra += [cfg.cache / d for d in ("downloads", "slim", "firmware") if (cfg.cache / d).exists()]
    else:                                                      # slim images no image would use now
        try:
            want = {cfg.slim_key(n)[:16] for n in cfg.images if "from" not in cfg.images[n] and cfg.images[n].get("slim")}
            extra += [f for f in (cfg.cache / "slim").glob("*.qcow2") if f.stem not in want]
        except Fatal:                                          # no ISO to tell: keep them
            pass
    isos = sorted(p for p in (cfg.data / "iso").glob("*.iso")) if a.iso else []
    sizes = {p: du_bytes(p) for p in [*gone, *extra, *isos]}
    if not sizes:
        return log("nothing to prune")
    for p in gone:
        print(f"  image {label(p):<28} {human_size(sizes[p]):>7}")
    for p in extra:
        print(f"  {tilde(p):<34} {human_size(sizes[p]):>7}")
    for p in isos:
        print(f"  {tilde(p):<34} {human_size(sizes[p]):>7}  {iso_note(cfg, p)}", flush=True)
    total = human_size(sum(sizes.values()))
    if a.dry_run:
        return log(f"would free {total}")
    confirm(a, f"delete these ({total})?")
    for f in gone:
        store.remove(f)
    for p in [*extra, *isos]:
        shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
    log(f"pruned: {total} freed")


def iso_note(cfg: Config, iso: Path) -> str:
    """What deleting an ISO from the data folder costs."""
    if iso.is_symlink():
        return f"a link: your file stays ({tilde(iso.resolve())})"
    if cfg.iso_is_pinned(iso):
        return "winvm can download it again"
    return "your only copy here: winvm can't download this one"


def du_bytes(p: Path) -> int:
    """Disk space used by a file or folder (a link: the link's own)."""
    if p.is_symlink():
        return p.lstat().st_blocks * 512
    if p.is_file():
        return p.stat().st_blocks * 512
    return sum(f.stat().st_blocks * 512 for f in p.rglob("*") if f.is_file() and not f.is_symlink())


def human_size(n: int) -> str:
    return f"{n / (1 << 30):.1f}G" if n >= 1 << 30 else f"{n / (1 << 20):.0f}M"


@command()
def cmd_df(a):
    """disk space winvm uses: images, instances, ISOs, cache - and what is free"""
    cfg, store = a.cfg, Store(a.cfg)
    rows = [("images", sum(du_bytes(f) for f in store.all()), cfg.data / "images"),
            ("instances", sum(du_bytes(d.parent) for d in store.disks()), cfg.data / "instances"),
            ("ISOs", sum(du_bytes(f.resolve()) for f in cfg.linked_isos() if not f.is_symlink()), cfg.data / "iso"),
            ("downloads", du_bytes(cfg.cache / "downloads") if (cfg.cache / "downloads").exists() else 0,
             cfg.cache / "downloads"),
            ("slim images", du_bytes(cfg.cache / "slim") if (cfg.cache / "slim").exists() else 0, cfg.cache / "slim"),
            ("build scratch", du_bytes(cfg.cache / "build") if (cfg.cache / "build").exists() else 0,
             cfg.cache / "build")]
    for what, n, where in rows:
        print(f"  {what:<14} {human_size(n):>7}  {tilde(where)}")
    print(f"  {'total':<14} {human_size(sum(r[1] for r in rows)):>7}")
    seen = set()
    for d in (cfg.data, cfg.cache):
        p = d if d.exists() else Path.home()
        if os.stat(p).st_dev not in seen:
            seen.add(os.stat(p).st_dev)
            print(f"\n  {human_size(shutil.disk_usage(p).free)} free on the file system of {tilde(d)}")
    print("\n  winvm prune frees unused versions and stale slim images; --cache also the downloads "
          "and slim images, --iso the ISOs\n  (built images and instances keep working without them)")


@command(arg("name", names="images"))
def cmd_rmi(a):
    """delete all versions of an image (refused while anything builds on them)"""
    store = Store(a.cfg)
    versions = store.versions(valid(a.name)) or die(f"no built image '{a.name}'")
    for f in versions:
        if users := store.users(f):
            die(f"{label(f)} is used by {', '.join(users)} - rm/reset those first")
    confirm(a, f"delete image '{a.name}' ({human_size(sum(du_bytes(f) for f in versions))})? "
               f"{rebuild_note(a.cfg, a.name)}")
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


@command(arg("image", nargs="?", names="images", help="default: the instance's image (its current version)"))
def cmd_reset(a):
    """discard the instance's changes: fresh disk on the current version of its image"""
    cfg, vm, store = a.cfg, inst(a), Store(a.cfg)
    if a.image and not a.instance_given and a.image != vm.name and (cfg.data / "instances" / a.image).is_dir():
        die(f"'{a.image}' is an instance: to reset it, winvm -i {a.image} reset   (to put {vm.name} "
            f"on image '{a.image}': winvm -i {vm.name} reset {a.image})")
    vm.disk.exists() or die(f"no instance '{vm.name}' (winvm ps)")
    vm.require_stopped()
    image = cfg.ref(a.image) if a.image else vm.image
    onto = "" if image == vm.image else f" and put it on image '{image}'"
    confirm(a, f"discard {vm.name}'s changes ({du(vm.disk)}){onto}?")
    if image in cfg.images:
        build(cfg, [image])
    vm.create(image, store.require(image))


@command(arg("names", nargs="*", metavar="INSTANCE", names="instances"))
def cmd_rm(a):
    """delete instances: their disks (all changes in them!) and run folders"""
    vms = instances_named(a, a.names)
    for vm in vms:
        if not (vm.home.exists() or vm.dir.exists()):
            die(f"no instance '{vm.name}' (winvm ps)")
        vm.require_stopped()
    confirm(a, "delete " + ", ".join(f"{vm.name} ({du(vm.disk)} of changes)" for vm in vms) + "?")
    for vm in vms:
        shutil.rmtree(vm.home, ignore_errors=True)
        shutil.rmtree(vm.dir, ignore_errors=True)
        log(f"removed instance {vm.name}")


@command(arg("image", nargs="?", default="base", names="images", help="an image, or an ISO file"))
def cmd_editions(a):
    """list the editions inside an image's Windows ISO (or an ISO file's)"""
    if a.image.lower().endswith(".iso"):
        iso = Path(a.image).expanduser()
        iso.is_file() or die(f"no such ISO: {iso}")
    else:
        iso = a.cfg.iso(a.cfg.chain(a.cfg.ref(a.image))[0])   # the ISO image a `from` image is built on
    for idx, name in editions(iso):
        print(f"  {idx}: {name}")


@command(arg("images", nargs="*", metavar="IMAGE", names="images", help="default: every image winvm knows"))
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
    if cfg.accel == "tcg":
        row("KVM", None if kvm else True, f"{kvm or '/dev/kvm'} - not used: accel tcg, emulated and far slower")
    else:
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
         arg("image", nargs="?", names="images", help="an image (name or file) instead of the machine settings"))
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
        if cfg.prebuilt(name):
            meta = Store(cfg).meta(Store(cfg).versions(name)[0])
            print(f"# '{name}' is built, without a file (saved, or its file is gone): its metadata")
            return print(yaml.safe_dump(meta, sort_keys=False, width=120), end="")
        img = cfg.image(name)
        print(f"# {cfg.sources[name]}")
        print(yaml.safe_dump({**img, "run": cfg.run_settings(name)}, sort_keys=False, width=120), end="")
    elif a.what == "show":
        print(yaml.safe_dump({"settings": {**cfg.raw["settings"], "accel": cfg.accel}, "run": cfg.raw["run"]}, sort_keys=False, width=120), end="")
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
# (also the "names" kinds above: instances, images, mounts)


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
    # options and positionals taking instance or image names: `winvm __names KIND` lists them
    opt_kind = {o: act.names for _, sp_ in [(None, p), *subs] for act in sp_._actions
                if getattr(act, "names", None) for o in act.option_strings}
    pos_kind = {n: act.names for names, sp_ in subs for act in sp_._actions
                if getattr(act, "names", None) and not act.option_strings for n in names}
    if a.shell == "bash":
        cases = "".join(f"    {'|'.join(names)}) opts=\"{' '.join(o for act in sp_._actions for o in act.option_strings)}\";;\n"
                        for names, sp_ in subs)
        kinds = {k: [o for o, v in opt_kind.items() if v == k] for k in set(opt_kind.values())}
        prev = "".join(f"    {'|'.join(os_)}) kind={k};;\n" for k, os_ in kinds.items())
        pos = "".join(f"    {'|'.join(n for n, v in pos_kind.items() if v == k)}) kind={k};;\n"
                      for k in set(pos_kind.values()))
        sys.stdout.write(f"""# winvm {__version__} bash completion (winvm completion bash)
_winvm() {{
  local cur=${{COMP_WORDS[COMP_CWORD]}} prev=${{COMP_WORDS[COMP_CWORD-1]}} cmd="" opts="" kind="" inst="" i
  case $prev in
{prev}  esac
  if [[ -n $kind ]]; then
    COMPREPLY=($(compgen -W "$(winvm __names $kind 2>/dev/null)" -- "$cur"))
    return
  fi
  for ((i = 1; i < COMP_CWORD; i++)); do
    case ${{COMP_WORDS[i]}} in
      -i|--instance) inst=${{COMP_WORDS[i+1]}}; ((i++));;
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
    return
  fi
  case $cmd in
{pos}  esac
  [[ -n $kind ]] && COMPREPLY=($(compgen -W "$(winvm __names $kind $inst 2>/dev/null)" -- "$cur"))
  case $cmd in {'|'.join(n for names, _ in subs if names[0] in FILE_ARGS for n in names)}) COMPREPLY+=($(compgen -f -- "$cur"));; esac
}}
complete -o default -o bashdefault -F _winvm winvm
""")
        return
    q = desc
    # the command word: the first one that is neither an option nor an option's value
    out = [f"# winvm {__version__} fish completion (winvm completion fish)", f"""function __winvm_cmd
    set -l toks (commandline -opc)[2..]
    while set -q toks[1]
        switch $toks[1]
            case {' '.join(vals)}
                set -e toks[1]
            case '-*'
            case '*'
                echo $toks[1]
                return 0
        end
        set -e toks[1]
    end
    return 1
end""", """function __winvm_inst
    set -l toks (commandline -opc)[2..]
    while set -q toks[2]
        if contains -- $toks[1] -i --instance
            echo $toks[2]
            return 0
        end
        set -e toks[1]
    end
end""", "complete -c winvm -f"]
    top = "'not __winvm_cmd >/dev/null'"
    for act in p._actions:
        if act.option_strings and act.help != argparse.SUPPRESS:
            out.append(fish_opt(top, act, q))
    for names, sp_ in subs:
        out.append(f"complete -c winvm -n {top} -a {names[0]} -d '{q(sp_.description)}'")
        for alias in names[1:]:
            out.append(f"complete -c winvm -n {top} -a {alias} -d 'same as {names[0]}'")
        seen = f"'contains -- (__winvm_cmd) {' '.join(names)}'"
        if names[0] in FILE_ARGS:
            out.append(f"complete -c winvm -n {seen} -F")
        for act in sp_._actions:
            if act.option_strings and act.help != argparse.SUPPRESS and "-h" not in act.option_strings:
                out.append(fish_opt(seen, act, q))
            elif not act.option_strings and act.choices:
                out.append(f"complete -c winvm -n {seen} -a '{' '.join(act.choices)}'")
            elif not act.option_strings and getattr(act, "names", None):
                out.append(f"complete -c winvm -n {seen} -a '(winvm __names {act.names} (__winvm_inst))'")
    print("\n".join(out))


def desc(text: str | None) -> str:
    """First line of a help text, quoted for fish."""
    return text.splitlines()[0][:80].replace("'", "\\'") if text else ""


def fish_opt(cond: str, act, q) -> str:
    longs = [o[2:] for o in act.option_strings if o.startswith("--")]
    shorts = [o[1:] for o in act.option_strings if not o.startswith("--")]
    flags = " ".join([*(f"-l {x}" for x in longs), *(f"-s {x}" for x in shorts)])
    names = f" -a '(winvm __names {act.names})'" if getattr(act, "names", None) else ""
    takes = "" if act.nargs in (0, "?") else " -x" if getattr(act, "names", None) == "instances" else " -r"  # -x: no files
    return f"complete -c winvm -n {cond} {flags}{takes}{names} -d '{q(act.help)}'"


def complete_names(kind: str, instance: str | None = None) -> int:
    """`winvm __names instances|images|mounts [INSTANCE]`: for shell completion - quiet, never fails."""
    try:
        cfg = Config()
        store = Store(cfg)
        if kind == "mounts":                                   # of the running instance (-i, else main)
            names = list(Instance(cfg, valid(instance or "main")).mounts)
        elif kind == "instances":
            names = [d.parent.name for d in store.disks()]
        else:
            names = sorted(set(cfg.images) | {f.stem.split("@")[0] for f in store.all()})
        print("\n".join(names))
    except Exception:                                          # noqa: BLE001 - a broken config: no names
        pass
    return 0


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
    p.add_argument("-i", "--instance", metavar="NAME", help="instance to act on (default: main)").names = "instances"
    p.add_argument("--data-dir", metavar="DIR",
                   help="images and instances (default: $WINVM_DATA_DIR, settings.data_dir)")
    p.add_argument("--cache-dir", metavar="DIR",
                   help="downloads and build scratch (default: $WINVM_CACHE_DIR, settings.cache_dir)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask: download what's needed, delete what was asked for")
    p.add_argument("--timeout", metavar="TIME", help="give up on the whole command after this long (90s, 30m, 2h): exit 124")
    p.add_argument("--accel", choices=("kvm", "tcg"), help="tcg: emulate, without KVM; far slower (default: $WINVM_ACCEL, settings.accel)")
    p.add_argument("-V", "--version", action="version", version=f"winvm {__version__}")
    # -i and -y also work after the command (`winvm ps -i x` / `winvm rm -y`)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-i", "--instance", metavar="NAME", default=argparse.SUPPRESS,
                        help="instance to act on (default: main)").names = "instances"
    common.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS, help="don't ask")
    common.add_argument("--accel", choices=("kvm", "tcg"), default=argparse.SUPPRESS, help="tcg: emulate, without KVM; far slower")
    timed = argparse.ArgumentParser(add_help=False)
    timed.add_argument("--timeout", metavar="TIME", default=argparse.SUPPRESS, help="give up after this long (90s, 30m, 2h): exit 124")
    sub = p.add_subparsers(title="commands", metavar="<command>")
    subs = []
    for name, (fn, args, aliases) in COMMANDS.items():
        doc = fn.__doc__ or ""
        sp_ = sub.add_parser(name, aliases=aliases, help=doc.splitlines()[0], description=doc,
                             formatter_class=argparse.RawDescriptionHelpFormatter,
                             parents=[] if name in RAW or getattr(fn, "no_config", False) else
                             [common] if name in OWN_TIMEOUT else [common, timed])
        for flags, kw in args:
            kw = dict(kw)
            names = kw.pop("names", None)
            sp_.add_argument(*flags, **kw).names = names
        sp_.set_defaults(fn=fn)
        subs.append(([name, *aliases], sp_))
    return p, subs


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["__names"]:                                # (hidden) for shell completion
        return complete_names(*(argv[1:3] or [""]))
    p, _ = parser()

    # raw commands get everything after their name untouched (PowerShell, ssh, scp arguments)
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in ("-i", "--instance", "-f", "--file", "--data-dir", "--cache-dir", "--timeout", "--accel") else 1
    if i < len(argv) and argv[i] in RAW and argv[i + 1:] not in (["-h"], ["--help"]):
        a = p.parse_args(argv[: i + 1])
        a.rest = argv[i + 1:]
        # winvm's own options right after the command; what follows them is the program's. ssh and
        # scp have an -i of their own, so winvm's goes before the command there
        takes_i = argv[i] in ("sh", "shell", "rdp", "ga")
        while a.rest:
            w = a.rest[0]
            if takes_i and w in ("-y", "--yes"):
                a.yes, a.rest = True, a.rest[1:]
            elif takes_i and w in ("-i", "--instance") and len(a.rest) > 1:
                a.instance, a.rest = a.rest[1], a.rest[2:]
            elif w in ("--timeout", "--accel") and len(a.rest) > 1:
                setattr(a, w[2:], a.rest[1])
                a.rest = a.rest[2:]
            elif w.startswith(("--timeout=", "--accel=")):
                k, _, v = w[2:].partition("=")
                setattr(a, k, v)
                a.rest = a.rest[1:]
            else:
                break
    else:
        a = p.parse_args(argv)
    if not hasattr(a, "fn"):
        p.print_help()
        return 1
    if os.geteuid() == 0 and not os.environ.get("WINVM_ALLOW_ROOT"):
        print(paint("1;31", "error:"), "winvm runs as your own user, not as root (KVM needs group kvm: "
              "sudo usermod -aG kvm $USER, then log in again); WINVM_ALLOW_ROOT=1 to insist", file=sys.stderr)
        return 1
    try:
        if getattr(a.fn, "no_config", False):
            return a.fn(a) or 0
        a.cfg = Config(a.file, a.data_dir, a.cache_dir)
        a.cfg.yes = a.yes
        a.cfg.accel_arg = getattr(a, "accel", None)
        a.instance_given = bool(a.instance)
        a.instance = a.instance or "main"
        a.limit = None
        if a.timeout and a.fn.__name__.removeprefix("cmd_") not in OWN_TIMEOUT:
            try:
                a.limit = seconds(a.timeout)
            except ValueError as e:
                die(f"--timeout: {e}")
            start_timeout(a.limit)
        return a.fn(a) or 0
    except TimedOut as e:
        print(paint("1;31", "error:"), e, file=sys.stderr)
        return guest.EXIT_TIMED_OUT
    except Fatal as e:
        print(paint("1;31", "error:"), e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
