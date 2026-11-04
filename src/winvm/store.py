"""Image layers on disk.

  images/<name>@<hash12>.qcow2   read-only layer, backed by the layer below it
  images/<name>@<hash12>.json    {name, hash, from, built, saved}

A changed image definition gets a new hash and so a new file next to the old one:
instances on the old version keep working until they are reset, and `prune` deletes
versions nothing uses any more. Backing files are stored as relative paths, so
`qemu-img info --backing-chain` shows the same tree and the data dir can be moved.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .config import Config
from .util import die, output, run


class Store:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dir = cfg.data / "images"
        self._parents: dict[Path, tuple[int, Path | None]] = {}   # qemu-img info is slow-ish

    def file(self, name: str, h: str) -> Path:
        return self.dir / f"{name}@{h[:12]}.qcow2"

    @staticmethod
    def meta(f: Path) -> dict:
        try:
            return json.loads(f.with_suffix(".json").read_text())
        except (OSError, ValueError):
            return {"name": f.stem.split("@")[0], "hash": f.stem.split("@")[-1]}

    def all(self) -> list[Path]:
        return sorted(self.dir.glob("*@*.qcow2"))

    def versions(self, name: str) -> list[Path]:
        """All built versions of an image, newest first."""
        fs = [f for f in self.all() if f.stem.split("@")[0] == name]
        return sorted(fs, key=lambda f: self.meta(f).get("built", ""), reverse=True)

    def current(self, name: str) -> Path | None:
        """The version matching the config (images you `save`d have just one: the newest)."""
        if name in self.cfg.images:
            f = self.file(name, self.cfg.hash(name))
            return f if f.exists() else None
        return next(iter(self.versions(name)), None)

    def require(self, name: str) -> Path:
        if name not in self.cfg.images:
            return self.current(name) or die(f"no image '{name}' in the config or built (winvm images)")
        return self.current(name) or die(f"image '{name}' is not built - winvm build {name}")

    # --- backing chains
    def parent(self, img: Path) -> Path | None:
        """The layer below an image or disk (None for a bottom layer)."""
        mtime = img.stat().st_mtime_ns if img.exists() else die(f"missing: {img}")
        if self._parents.get(img, (None,))[0] != mtime:
            info = json.loads(output(self.cfg.s["qemu_img"], "info", "-U", "--output=json", img))
            b = info.get("backing-filename")
            self._parents[img] = (mtime, (img.parent / b).resolve() if b else None)
        return self._parents[img][1]

    def disks(self) -> list[Path]:
        """Instance disks (not builders)."""
        return sorted((self.cfg.data / "instances").glob("*/disk.qcow2"))

    def users(self, f: Path) -> list[str]:
        """Who is backed directly by layer f: other layers by label, instances as [name]."""
        out = [label(img) for img in self.all() if self.parent(img) == f]
        return out + [f"[{d.parent.name}]" for d in self.disks() if self.parent(d) == f]

    # --- writing
    def overlay(self, disk: Path, on: Path) -> None:
        """An empty qcow2 at `disk` on top of layer `on`."""
        disk.parent.mkdir(parents=True, exist_ok=True)
        disk.unlink(missing_ok=True)
        run(self.cfg.s["qemu_img"], "create", "-q", "-f", "qcow2", "-F", "qcow2",
            "-b", os.path.relpath(on, disk.parent), disk)

    def add(self, name: str, h: str, src: Path, below: Path | None, **meta) -> Path:
        """Freeze `src` as layer name@h: only src's own blocks (-B: the rest stays in the layer
        below), zstd-compressed, read-only."""
        dst = self.file(name, h)
        tmp = dst.with_suffix(".tmp")
        self.dir.mkdir(parents=True, exist_ok=True)
        zstd = ["-c", "-o", "compression_type=zstd"] if self.cfg.s.get("compress", True) else []
        back = ["-B", below.name, "-F", "qcow2"] if below else []   # relative: same folder
        try:
            run(self.cfg.s["qemu_img"], "convert", "-p", "-O", "qcow2", *zstd, *back, src, tmp)
            tmp.replace(dst)
        finally:
            tmp.unlink(missing_ok=True)                        # left only by a failed convert
        dst.chmod(0o444)
        dst.with_suffix(".json").write_text(json.dumps(
            {"name": name, "hash": h, "from": below and label(below),
             "built": time.strftime("%Y-%m-%dT%H:%M:%S"), **meta}, indent=1) + "\n")
        return dst

    def remove(self, f: Path) -> None:
        f.unlink(missing_ok=True)
        f.with_suffix(".json").unlink(missing_ok=True)


def label(f: Path) -> str:
    """images/dev@0123456789ab.qcow2 -> dev@0123456789ab"""
    return f.stem
