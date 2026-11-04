"""Shared helpers: output, external tools, sizes, hashing, downloads, QMP/QGA sockets."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import socket
import subprocess as sp
import sys
from pathlib import Path

NAME_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_COLOR = sys.stderr.isatty()


def _paint(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def log(msg: str) -> None:
    print(_paint("1;32", "==>"), msg, file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    print(_paint("1;33", "warn:"), msg, file=sys.stderr, flush=True)


class Fatal(Exception):
    """A user-facing error; the CLI prints it and exits 1."""


def die(msg: str):
    raise Fatal(msg)


def run(*cmd, check=True, quiet=False, **kw) -> sp.CompletedProcess:
    """Run an external tool; fail naming it if it fails."""
    cmd = [str(c) for c in cmd]
    if quiet:
        kw.setdefault("stdout", sp.DEVNULL)
    r = sp.run(cmd, **kw)
    if check and r.returncode:
        die(f"{Path(cmd[0]).name} failed (exit {r.returncode}): {' '.join(cmd[1:])[:300]}")
    return r


def output(*cmd, **kw) -> str:
    return run(*cmd, stdout=sp.PIPE, text=True, **kw).stdout


def need(*tools: str) -> None:
    missing = [t for t in tools if not shutil.which(t)]
    if missing:
        die(f"missing tools: {' '.join(missing)} (see README: host packages)")


def valid(name: str) -> str:
    return name if NAME_RX.fullmatch(name or "") else die(f"bad name '{name}' (letters, digits, . _ -)")


# ------------------------------------------------------------------ sizes

def mib(size) -> int:
    """4G / 4096M / 4096 (MiB) -> MiB"""
    s = str(size).strip().upper().removesuffix("B").removesuffix("I")
    if s.endswith("G"):
        return int(float(s[:-1]) * 1024)
    return int(float(s.removesuffix("M")))


def human(n: float) -> str:
    for unit in "BKMG":
        if n < 1024:
            break
        n /= 1024
    return f"{n:.1f}{unit}" if unit == "G" else f"{n:.0f}{unit}"


def du(path: Path) -> str:
    """Allocated size on the host (like du -h)."""
    return human(path.stat().st_blocks * 512) if path.exists() else "-"


def meminfo(key: str) -> int:
    """/proc/meminfo value in MiB"""
    m = re.search(rf"^{key}:\s+(\d+)", Path("/proc/meminfo").read_text(), re.M)
    return int(m[1]) // 1024


# ------------------------------------------------------------------ hashing / downloads

def sha256_file(f: Path) -> str:
    h = hashlib.sha256()
    with f.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def content_hash(path: Path) -> str:
    """Hash of a file, or of a folder's names and contents (for `copy` sources)."""
    if path.is_file():
        return sha256_file(path)
    if not path.is_dir():
        die(f"no such file or folder: {path}")
    h = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file()):
        h.update(f"{f.relative_to(path).as_posix()}\0{sha256_file(f)}\0".encode())
    return h.hexdigest()


def download(url: str, f: Path, sha256: str) -> Path:
    """Fetch url to f unless f is already there with this sha256; a mismatch is fatal."""
    if f.exists() and sha256_file(f) == sha256.lower():
        return f
    log(f"downloading {f.name}")
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".part")
    if shutil.which("aria2c"):
        run("aria2c", "-q", "-x8", "-s8", "--allow-overwrite=true", "--auto-file-renaming=false",
            "-d", tmp.parent, "-o", tmp.name, url)
    else:
        run("curl", "-fL", "--progress-bar", "-o", tmp, url)
    if (got := sha256_file(tmp)) != sha256.lower():
        tmp.unlink()
        die(f"checksum mismatch for {url}: got {got}, expected {sha256}")
    tmp.replace(f)
    return f


def dos(text: str) -> bytes:
    """Unix -> Windows line endings."""
    return re.sub(r"\r*\n", "\r\n", text if text.endswith("\n") else text + "\n").encode()


# ------------------------------------------------------------------ QMP / guest agent

class Chan:
    """Line-delimited JSON over a unix socket: QMP (QEMU monitor) or QGA (guest agent)."""

    def __init__(self, path: Path, qmp: bool):
        if not path.is_socket():
            die(f"{path.name} missing - is the instance running?")
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(30)
        self.sock.connect(str(path))
        self.f = self.sock.makefile("rw", encoding="utf-8")
        if qmp:
            self.f.readline()                                  # greeting
            self("qmp_capabilities")

    def __call__(self, cmd: str, args: dict | None = None):
        self.f.write(json.dumps({"execute": cmd, **({"arguments": args} if args else {})}) + "\n")
        self.f.flush()
        while True:                                            # skip async events
            line = self.f.readline() or die(f"{cmd}: connection closed")
            msg = json.loads(line)
            if "error" in msg:
                die(f"{cmd}: {msg['error'].get('desc', msg['error'])}")
            if "return" in msg:
                return msg["return"]
