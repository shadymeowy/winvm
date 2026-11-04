"""Shared helpers: output, external tools, sizes, hashing, QMP/QGA sockets."""
from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess as sp
import sys
from pathlib import Path
from typing import NoReturn

NAME_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_COLOR = sys.stderr.isatty()


def paint(code: str, s: str) -> str:
    """ANSI color for stderr, if it is a terminal."""
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def log(msg: str) -> None:
    print(paint("1;32", "==>"), msg, file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    print(paint("1;33", "warn:"), msg, file=sys.stderr, flush=True)


class Fatal(Exception):
    """A user-facing error; the CLI prints it and exits 1."""


def die(msg: str) -> NoReturn:
    raise Fatal(msg)


def run(*cmd, check=True, quiet=False, **kw) -> sp.CompletedProcess:
    """Run an external tool; fail naming it if it fails."""
    cmd = [str(c) for c in cmd]
    if quiet:
        kw.setdefault("stdout", sp.DEVNULL)
    try:
        r = sp.run(cmd, **kw)
    except FileNotFoundError:
        die(f"{cmd[0]} not found (winvm check)")
    if check and r.returncode:
        die(f"{Path(cmd[0]).name} failed (exit {r.returncode}): {' '.join(cmd[1:])[:300]}")
    return r


def output(*cmd, **kw) -> str:
    return run(*cmd, stdout=sp.PIPE, text=True, **kw).stdout


def valid(name) -> str:
    """An image / instance / mount name, safe in file names (YAML keys may be numbers)."""
    name = "" if name is None else str(name)
    return name if NAME_RX.fullmatch(name) else die(f"bad name '{name}' (letters, digits, . _ -)")


# ------------------------------------------------------------------ sizes

def mib(size) -> int:
    """4G / 4096M / 4096 (MiB) -> MiB"""
    s = str(size).strip().upper().removesuffix("B").removesuffix("I")
    try:
        if s.endswith("G"):
            return int(float(s[:-1]) * 1024)
        return int(float(s.removesuffix("M")))
    except ValueError:
        die(f"bad size '{size}' (e.g. 4G, 512M)")


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


# ------------------------------------------------------------------ hashing

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
        try:
            self.sock.connect(str(path))
        except OSError as e:                                   # a socket left by a QEMU that died
            die(f"{path.name}: {e} - is the instance running?")
        self.f = self.sock.makefile("rw", encoding="utf-8")
        if qmp:
            self._read("greeting")
            self("qmp_capabilities")

    def _read(self, cmd: str) -> dict:
        try:
            line = self.f.readline()
        except OSError as e:                                   # e.g. no guest agent: timeout
            die(f"{cmd}: {e}")
        return json.loads(line) if line else die(f"{cmd}: connection closed")

    def __call__(self, cmd: str, args: dict | None = None):
        self.f.write(json.dumps({"execute": cmd, **({"arguments": args} if args else {})}) + "\n")
        self.f.flush()
        while True:                                            # skip async events
            msg = self._read(cmd)
            if "error" in msg:
                die(f"{cmd}: {msg['error'].get('desc', msg['error'])}")
            if "return" in msg:
                return msg["return"]
