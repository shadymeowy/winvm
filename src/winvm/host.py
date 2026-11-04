"""The host: QEMU, KVM, UEFI firmware, pinned downloads (with consent), the Windows ISO.

QEMU is the one program winvm can't fetch; everything else it needs either ships with
Python, is pure Python (pycdlib), or is downloaded once into the cache - pinned by
sha256, after asking (or with -y).
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess as sp
import sys
import tarfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__
from .config import Config
from .util import die, human, log, sha256_file

OVMF_PAIRS = [  # (code, vars) of non-secure-boot builds, per distro
    ("/usr/share/edk2/ovmf/OVMF_CODE.fd", "/usr/share/edk2/ovmf/OVMF_VARS.fd"),
    ("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"),
    ("/usr/share/OVMF/OVMF_CODE.fd", "/usr/share/OVMF/OVMF_VARS.fd"),
    ("/usr/share/edk2/x64/OVMF_CODE.4m.fd", "/usr/share/edk2/x64/OVMF_VARS.4m.fd"),
    ("/usr/share/edk2-ovmf/x64/OVMF_CODE.fd", "/usr/share/edk2-ovmf/x64/OVMF_VARS.fd"),
]
LICENSE_NOTE = ("Windows is Microsoft software under Microsoft's license terms. winvm installs it "
                "unactivated; activating needs a license of your own.")


# ------------------------------------------------------------------ programs

def program(cfg: Config, key: str) -> str | None:
    """settings.qemu / qemu_img: a name on PATH, or a path (with ~, ${VAR})."""
    v = str(cfg.s.get(key) or "")
    return shutil.which(str(cfg.path(v)) if "/" in v else v) if v else None


def qemu(cfg: Config) -> str:
    return program(cfg, "qemu") or die(f"QEMU not found ({cfg.s.get('qemu')}) - {install_hint('qemu')}")


def qemu_img(cfg: Config) -> str:
    if cfg.s.get("qemu_img"):
        return program(cfg, "qemu_img") or die(f"qemu-img not found ({cfg.s['qemu_img']})")
    for p in ("/usr/bin/qemu-img", shutil.which("qemu-img")):
        if p and Path(p).exists():
            return p
    die(f"qemu-img not found - {install_hint('qemu')}")


def kvm_problem() -> str | None:
    if not Path("/dev/kvm").exists():
        return "/dev/kvm is missing: enable virtualization (VT-x/AMD-V) in the firmware, load kvm_intel/kvm_amd"
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        return "no access to /dev/kvm: add yourself to group kvm (sudo usermod -aG kvm $USER), log in again"
    return None


# what to install, per distro family (ID / ID_LIKE in /etc/os-release)
PACKAGES = {
    "fedora": ("sudo dnf install", {"qemu": "qemu-kvm", "ssh": "openssh-clients", "samba": "samba",
                                    "gtk": "qemu-ui-gtk", "rdp": "freerdp", "vnc": "virt-viewer"}),
    "debian": ("sudo apt install", {"qemu": "qemu-system-x86 qemu-utils", "ssh": "openssh-client",
                                    "samba": "samba", "gtk": "qemu-system-gui", "rdp": "freerdp3-x11",
                                    "vnc": "virt-viewer"}),
    "arch": ("sudo pacman -S", {"qemu": "qemu-base", "ssh": "openssh", "samba": "samba",
                                "gtk": "qemu-ui-gtk", "rdp": "freerdp", "vnc": "virt-viewer"}),
    "suse": ("sudo zypper install", {"qemu": "qemu-x86 qemu-tools", "ssh": "openssh-clients",
                                     "samba": "samba", "gtk": "qemu-ui-gtk", "rdp": "freerdp",
                                     "vnc": "virt-viewer"}),
}
WHAT = {"qemu": "QEMU (qemu-system-x86_64, qemu-img)", "ssh": "the OpenSSH client (ssh, scp, ssh-keygen)",
        "samba": "Samba (smbd)", "gtk": "QEMU's GTK display", "rdp": "FreeRDP", "vnc": "a VNC viewer"}


def install_hint(*needs: str) -> str:
    """How to install what `needs` names ("qemu", "ssh", "samba", "gtk", "rdp", "vnc") here."""
    ids = []
    try:
        for ln in Path("/etc/os-release").read_text().splitlines():
            if ln.startswith(("ID=", "ID_LIKE=")):
                ids += ln.split("=", 1)[1].strip('"').split()
    except OSError:
        pass
    for i in ids:
        i = {"rhel": "fedora", "centos": "fedora", "ubuntu": "debian", "opensuse": "suse"}.get(i, i)
        if PACKAGES.get(i):
            cmd, pkgs = PACKAGES[i]
            return f"install it: {cmd} {' '.join(pkgs[n] for n in needs)}"
    return "install " + " and ".join(WHAT[n] for n in needs) + " from your distribution"


def ssh_missing() -> list[str]:
    return [t for t in ("ssh", "scp", "ssh-keygen") if not shutil.which(t)]


def has_gtk(cfg: Config) -> bool:
    r = sp.run([qemu(cfg), "-display", "help"], capture_output=True, text=True)
    return "gtk" in r.stdout.split()


def require(cfg: Config, gui=False) -> None:
    """Fail early - before downloading or booting anything - if the host can't run a VM
    the way winvm does: QEMU with KVM, and the OpenSSH client to talk to Windows."""
    qemu(cfg)
    qemu_img(cfg)
    if problem := kvm_problem():
        die(problem)
    if missing := ssh_missing():
        die(f"{', '.join(missing)} not found: winvm talks to Windows over SSH - {install_hint('ssh')}")
    if gui and not has_gtk(cfg):
        die(f"this QEMU has no GTK display (--gui) - {install_hint('gtk')}")


# ------------------------------------------------------------------ downloads

def pinned(cfg: Config, key: str) -> dict:
    """A pinned download from the settings (virtio, openssh, ovmf, windows) + its cache path."""
    pin = cfg.s.get(key) or die(f"settings.{key} is not set")
    name = urllib.parse.unquote(pin["url"].split("?")[0].rsplit("/", 1)[-1])
    dest = cfg.data / "iso" / name if key == "windows" else cfg.cache / "downloads" / name
    return {**pin, "key": key, "dest": dest}


def consent(items: list[dict], yes: bool) -> None:
    """Ask once before downloading `items` (title, url, size)."""
    if not items:
        return
    total = sum(i.get("size") or 0 for i in items)
    print(f"winvm needs to download ({human(total)}, once, checked against pinned sha256):", file=sys.stderr)
    for i in items:
        print(f"  {human(i.get('size') or 0):>6}  {i['title']}\n          {i['url']}", file=sys.stderr)
    windows = any(i["key"] == "windows" for i in items)
    if windows:
        print(f"  {LICENSE_NOTE}", file=sys.stderr)
    if yes:
        return
    iso_hint = " (or bring your own ISO: winvm up --iso PATH)" if windows else ""
    if not sys.stdin.isatty():
        die("downloads needed - rerun with -y to allow them" + iso_hint)
    print("Download now? [y/N] ", end="", file=sys.stderr, flush=True)
    try:
        answer = input()
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        die("not downloading" + iso_hint)


def fetch(url: str, dest: Path, sha256: str, size: int | None = None) -> Path:
    """Download url to dest (unless it's there with this sha256): resumes a partial download,
    shows progress with speed and ETA, checks the sha256."""
    if dest.exists() and (size is None or dest.stat().st_size == size) and sha256_file(dest) == sha256.lower():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if size and have >= size:                                  # complete or bogus: start over
        have = 0
    headers = {"User-Agent": f"winvm/{__version__}", **({"Range": f"bytes={have}-"} if have else {})}
    log(f"downloading {dest.name}" + (f" (resuming at {human(have)})" if have else ""))
    progress = Progress(dest.name)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
            if have and r.status != 206:                       # server ignored the range
                have = 0
            total = have + int(r.headers.get("Content-Length") or 0) or size or 0
            if size and total != size:                         # not the file that was pinned
                die(f"{url} is {total} bytes, the pinned download {size}")
            with part.open("ab" if have else "wb") as out:
                done = have
                while chunk := r.read(1 << 20):
                    out.write(chunk)
                    done += len(chunk)
                    progress.update(done, total)
    except OSError as e:
        die(f"download failed: {url}: {e} - run it again to resume")
    finally:
        progress.end()
    log(f"checking {dest.name} (sha256)")
    if (got := sha256_file(part)) != sha256.lower():
        part.unlink()
        die(f"checksum mismatch for {url}: got {got}, expected {sha256}")
    part.replace(dest)
    return dest


class Progress:
    """One self-updating line on a terminal (done/total, speed, ETA); elsewhere a line per 10%."""

    def __init__(self, name: str):
        self.name, self.tty = name, sys.stderr.isatty()
        self.samples: list[tuple[float, int]] = []             # (time, bytes) of the last seconds
        self.shown, self.decile, self.drawn = 0.0, -1, False

    def update(self, done: int, total: int) -> None:
        now = time.time()
        if now - self.shown < 0.5:
            return
        self.shown = now
        self.samples = [x for x in self.samples if now - x[0] < 10] + [(now, done)]
        t0, d0 = self.samples[0]
        rate = (done - d0) / (now - t0) if now > t0 else 0
        eta = f", {duration((total - done) / rate)} left" if total and rate > 0 else ""
        pct = f"{done * 100 // total:3d}%  " if total else ""
        line = f"  {pct}{human(done)} of {human(total) if total else '?'}, {human(rate)}/s{eta}"
        if self.tty:
            print(f"\r{line}\033[K", end="", file=sys.stderr, flush=True)
            self.drawn = True
        elif total and rate > 0 and (dec := done * 10 // total) > self.decile:
            self.decile = dec
            print(line, file=sys.stderr, flush=True)

    def end(self) -> None:
        if self.drawn:
            print(file=sys.stderr)


def duration(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


# ------------------------------------------------------------------ UEFI firmware

def ovmf(cfg: Config) -> tuple[str, str] | None:
    """(code, empty vars) of a non-secure-boot UEFI build: the settings, the host's, or the
    one downloaded earlier; None if a download is needed (see ensure_ovmf)."""
    code, vars_ = cfg.s.get("ovmf_code"), cfg.s.get("ovmf_vars")
    if code or vars_:
        if not (code and vars_):
            die("set both settings.ovmf_code and settings.ovmf_vars (or neither: autodetect)")
        pair = str(cfg.path(code)), str(cfg.path(vars_))
        for f in pair:
            Path(f).is_file() or die(f"UEFI firmware file not found: {f} (settings.ovmf_code/ovmf_vars)")
        return pair
    own = cfg.cache / "firmware"
    for pair in [*OVMF_PAIRS, (str(own / "OVMF_CODE_4M.fd"), str(own / "OVMF_VARS_4M.fd"))]:
        if all(Path(p).exists() for p in pair):
            return pair
    return None


def firmware(cfg: Config) -> tuple[str, str]:
    """The firmware for a VM; asks before downloading it (cfg.yes: don't ask)."""
    if pair := ovmf(cfg):
        return pair
    consent(downloads_for(cfg, []), cfg.yes)                   # the package, unless it is there
    return ensure_ovmf(cfg)


def ensure_ovmf(cfg: Config) -> tuple[str, str]:
    """The firmware; downloads Debian's package (pinned) and unpacks it if needed."""
    if pair := ovmf(cfg):
        return pair
    pin = pinned(cfg, "ovmf")
    deb = fetch(pin["url"], pin["dest"], pin["sha256"], pin.get("size"))
    unpack_ovmf(deb, cfg.cache / "firmware")
    return ovmf(cfg) or die(f"no OVMF_CODE_4M.fd/OVMF_VARS_4M.fd in {deb.name}")


def unpack_ovmf(deb: Path, out: Path) -> None:
    """OVMF_{CODE,VARS}_4M.fd from Debian's ovmf package: a .deb is an ar archive whose
    data.tar.* holds the files."""
    data = deb.read_bytes()
    if not data.startswith(b"!<arch>\n"):
        die(f"{deb.name} is not a .deb package")
    out.mkdir(parents=True, exist_ok=True)
    pos = 8
    while pos + 60 <= len(data):                               # 60-byte member headers
        name, size = data[pos:pos + 16].decode().strip().rstrip("/"), int(data[pos + 48:pos + 58])
        if name.startswith("data.tar"):
            try:
                with tarfile.open(fileobj=io.BytesIO(data[pos + 60:pos + 60 + size])) as t:
                    for m in t.getmembers():
                        if m.name.endswith(("/OVMF_CODE_4M.fd", "/OVMF_VARS_4M.fd")) and (f := t.extractfile(m)):
                            (out / Path(m.name).name).write_bytes(f.read())
            except tarfile.TarError as e:                      # e.g. zstd before Python 3.14
                die(f"can't unpack {name} of {deb.name}: {e}")
        pos += 60 + size + (size & 1)


# ------------------------------------------------------------------ Windows ISO

def needs_iso(cfg: Config, name: str) -> bool:
    """Does building ISO image `name` need the ISO download (none linked, none configured)?"""
    img = cfg.image(name)
    if img.get("iso"):
        return False                                           # its own path: no download for it
    return not cfg.linked_isos()


def downloads_for(cfg: Config, names: list[str]) -> list[dict]:
    """Pinned downloads that building ISO images `names` still needs."""
    keys = []
    if any("from" not in cfg.image(n) for n in names):
        keys += ["virtio", "openssh"]
        if ovmf(cfg) is None:
            keys.append("ovmf")
        if cfg.s.get("windows") and any("from" not in cfg.image(n) and needs_iso(cfg, n) for n in names):
            keys.append("windows")                             # (without the setting: --iso only)
    elif ovmf(cfg) is None:
        keys.append("ovmf")
    items = []
    for k in keys:
        pin = pinned(cfg, k)
        # there with the pinned size (its sha256 is checked where it is used)
        if not (pin["dest"].exists() and pin.get("size") in (None, pin["dest"].stat().st_size)):
            items.append({**pin, "title": pin.get("title") or TITLES.get(k, k)})
    return items


TITLES = {"virtio": "virtio-win drivers + guest tools (Fedora)",
          "openssh": "Win32-OpenSSH server (Microsoft, GitHub)",
          "ovmf": "OVMF UEFI firmware (Debian package)"}


def fetch_all(cfg: Config, items: list[dict]) -> None:
    for i in items:
        if i["key"] == "ovmf":
            ensure_ovmf(cfg)                                   # downloads and unpacks it
        else:
            fetch(i["url"], i["dest"], i["sha256"], i.get("size"))
