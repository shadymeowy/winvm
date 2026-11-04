"""winvm.yaml: lookup, merging over the packaged defaults, paths, image definitions and their
content hashes (an image is rebuilt when its hash changes, like a Docker layer)."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import yaml

from .util import Fatal, content_hash, die, log, run, valid

DATA = Path(__file__).parent / "data"
DEFAULT_FILE = DATA / "winvm.yaml"
GUEST = DATA / "guest"
# image fields that change what an ISO install produces (`slim` only changes how fast)
BASE_KEYS = ("edition", "index", "hostname", "user", "password", "timezone", "locale", "keyboard",
             "disk_size", "debloat")


def merge(a, b):
    """b over a, recursively for mappings."""
    if isinstance(a, dict) and isinstance(b, dict):
        return {**a, **{k: merge(a[k], v) if k in a else v for k, v in b.items()}}
    return b


def read_yaml(p: Path) -> dict:
    try:
        d = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        die(f"{p}: {e}")
    return d if isinstance(d, dict) else die(f"{p}: expected a mapping at the top level")


def find_config(explicit: str | None) -> Path | None:
    """-f FILE, $WINVM_CONFIG, ./winvm.yaml, ~/.config/winvm/winvm.yaml - or None (defaults only)."""
    for c in (explicit, os.environ.get("WINVM_CONFIG")):
        if c:
            p = Path(c).expanduser()
            return p.resolve() if p.exists() else die(f"config file not found: {p}")
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser()
    for p in (Path.cwd() / "winvm.yaml", xdg / "winvm/winvm.yaml"):
        if p.exists():
            return p.resolve()
    return None


def mapping(value, where: str) -> dict:
    return value if isinstance(value, dict) else die(f"{where}: expected a mapping, got {value!r}")


def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


class Config:
    def __init__(self, file: str | None = None, data_dir: str | None = None, cache_dir: str | None = None):
        self.file = find_config(file)
        self.base = self.file.parent if self.file else Path.cwd()
        raw = read_yaml(DEFAULT_FILE)
        if self.file:
            # an empty section (`settings:` with everything below commented out) changes nothing
            raw = merge(raw, {k: v for k, v in read_yaml(self.file).items() if v is not None})
        self.raw = raw
        self.s = raw.get("settings") or {}
        # resolved: layers are compared with the (resolved) backing files qemu-img reports
        self.data = self.folder(data_dir, "WINVM_DATA_DIR", "data_dir")
        self.cache = self.folder(cache_dir, "WINVM_CACHE_DIR", "cache_dir")
        # sockets, pids, ports, smbd state: short paths (unix sockets max out at 108 bytes),
        # gone after a reboot; keyed by data dir so two data dirs never mix their instances
        rt = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/winvm-{os.getuid()}"
        self.runtime = Path(rt) / f"winvm-{hashlib.sha256(str(self.data).encode()).hexdigest()[:8]}"
        # build scratch, keyed like the runtime dir: data dirs may share one cache
        self.build = self.cache / "build" / self.runtime.name
        self.key = self.data / "keys/id_ed25519"

        defaults = raw.get("defaults") or {}
        self.image_defaults = defaults.get("image") or {}
        self.instance_defaults = defaults.get("instance") or {}
        self.images: dict[str, dict] = {}
        for name, img in (raw.get("images") or {}).items():
            if img is None:                                    # `name: null` removes a default
                continue
            name, img = valid(name), mapping(img, f"images.{name}")
            if img.get("from") is not None:
                if img.get("iso") is not None:
                    die(f"image '{name}': `iso` or `from`, not both")
                img = {k: v for k, v in img.items() if k != "iso"}   # a default `iso: null`
                known = {"from", "steps"}
            else:
                img = merge(self.image_defaults, img)
                known = {"iso", *self.image_defaults}
            if unknown := set(img) - known:                   # e.g. `steps` on an ISO image
                die(f"image '{name}': unknown key(s) {', '.join(map(str, unknown))} "
                    f"(allowed: {', '.join(sorted(known))})")
            self.images[name] = img
        self.instances: dict[str, dict] = {}
        for name, i in (raw.get("instances") or {}).items():
            if i is not None:
                name, i = valid(name), mapping(i, f"instances.{name}")
                if unknown := set(i) - set(self.instance_defaults):
                    die(f"instance '{name}': unknown key(s) {', '.join(map(str, unknown))} "
                        f"(allowed: {', '.join(sorted(self.instance_defaults))})")
                self.instances[name] = merge(self.instance_defaults, i)
        self._hash: dict[str, str] = {}

    # --- paths
    def folder(self, cli: str | None, env: str, key: str) -> Path:
        """data/cache dir: command line, environment (both relative to the current folder),
        else the setting (relative to the config file)."""
        if v := cli or os.environ.get(env):
            return Path(v).expanduser().resolve()
        return self.path(self.s.get(key) or die(f"settings.{key} is not set"), plain=True).resolve()

    def path(self, value, plain=False) -> Path:
        """~, ${ENV}, ${data_dir}, ${cache_dir}; relative to the config file's folder."""
        def var(m):
            k = m[1] or m[2]
            if not plain and k in ("data_dir", "cache_dir"):
                return str(self.data if k == "data_dir" else self.cache)
            return os.environ.get(k, "")
        p = Path(os.path.expanduser(re.sub(r"\$\{(\w+)\}|\$(\w+)", var, str(value))))
        return p if p.is_absolute() else (self.base / p).resolve()

    # --- images
    def image(self, name: str) -> dict:
        return self.images.get(name) or die(f"no image '{name}' in the config (winvm images)")

    def parent(self, name: str) -> str | None:
        return self.image(name).get("from")

    def chain(self, name: str) -> list[str]:
        """The image and everything below it, bottom first."""
        out: list[str] = []
        while name:
            if name in out:
                die(f"images build on each other in a circle: {' -> '.join(out + [name])}")
            out.append(name)
            name = self.parent(name)
        return out[::-1]

    def iso(self, name: str) -> Path:
        """The image's Windows ISO: `iso:`, else the newest *.iso in ${data_dir}/iso."""
        img = self.image(name)
        if img.get("iso"):
            p = self.path(img["iso"])
        else:
            # newest first; lstat: a link made by `--iso` counts from when it was made
            p = max((self.data / "iso").glob("*.iso"), key=lambda q: q.lstat().st_mtime, default=None)
        if not (p and p.exists()):
            die(f"image '{name}' needs a Windows ISO: `winvm up --iso PATH` (official ISO from "
                f"microsoft.com/software-download), or set images.{name}.iso")
        return p

    def steps(self, name: str) -> list[dict]:
        """An image's steps (`setup` for ISO images, `steps` otherwise), checked and normalized."""
        img = self.image(name)
        offline = "from" not in img
        out = []
        for i, st in enumerate(img.get("setup" if offline else "steps") or [], 1):
            where = f"image '{name}' step {i}"
            if not isinstance(st, dict) or len(st) != 1:
                die(f"{where}: expected one of run / download / copy / restart")
            (kind, arg), = st.items()
            if kind == "run" and isinstance(arg, str):
                out.append({"run": arg})
            elif kind == "download" and isinstance(arg, dict) and {"url", "sha256"} <= arg.keys():
                if not re.fullmatch(r"[0-9a-fA-F]{64}", str(arg["sha256"])):
                    die(f"{where}: sha256 must be 64 hex digits")
                fname = arg.get("name") or str(arg["url"]).split("?")[0].rstrip("/").rsplit("/", 1)[-1]
                out.append({"download": {"url": arg["url"], "sha256": str(arg["sha256"]).lower(), "name": fname}})
            elif kind == "copy" and isinstance(arg, list) and 1 <= len(arg) <= (1 if offline else 2):
                if not (src := self.path(arg[0])).exists():
                    die(f"{where}: no such file or folder: {src}")
                out.append({"copy": [str(src), *(str(a) for a in arg[1:])]})
            elif kind == "restart" and arg is True and not offline:
                out.append({"restart": True})
            else:
                die(f"{where}: bad `{kind}` (see `winvm config dump`)"
                    + (" - ISO images take only run / download / copy [HOST_PATH]" if offline else ""))
        return out

    def ensure_key(self) -> Path:
        """winvm's own SSH key; every image built here trusts it."""
        if not self.key.exists():
            self.key.parent.mkdir(parents=True, exist_ok=True)
            self.key.parent.chmod(0o700)
            run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "winvm", "-f", self.key)
            log(f"created SSH key {self.key}")
        return self.key

    def authorized_keys(self) -> str:
        keys = [self.ensure_key().with_suffix(".pub"), *(self.path(p) for p in self.s.get("ssh_pubkeys") or [])]
        return "".join(k.read_text().strip() + "\n" for k in keys if k.exists())   # one per line

    def slim_key(self, name: str) -> str:
        """What the captured, debloated image depends on: ISO, edition, offline servicing."""
        img, iso = self.image(name), self.iso(name)
        return sha({"iso": [iso.name, iso.stat().st_size], "edition": img.get("edition"), "index": img.get("index"),
                     "strip": (img.get("debloat") or {}).get("strip_payloads"),
                     "pe": content_hash(GUEST / "pe.cmd"), "lists": content_hash(GUEST / "lists")})

    def hash(self, name: str) -> str:
        """Content hash of an image: its definition, the files it copies, and the image below."""
        if name not in self._hash:
            img = self.image(name)
            steps = [dict(st, src=content_hash(Path(st["copy"][0]))) if "copy" in st else st
                     for st in self.steps(name)]
            if "from" in img:
                self.chain(name)                               # fails on cycles
                d = {"from": self.hash(img["from"]), "steps": steps}
            else:
                try:
                    iso = self.iso(name)
                except Fatal:
                    # the ISO is gone (deleted to save space?): keep using the newest build
                    if built := self.built_hashes(name):
                        self._hash[name] = built[0]
                        return built[0]
                    raise
                d = {**{k: img.get(k) for k in BASE_KEYS}, "iso": [iso.name, iso.stat().st_size],
                     "setup": steps, "guest": content_hash(GUEST), "keys": self.authorized_keys(),
                     "tools": [self.s["virtio"]["sha256"], self.s["openssh"]["sha256"]]}
            self._hash[name] = sha(d)
        return self._hash[name]

    def built_hashes(self, name: str) -> list[str]:
        """Hashes of the built versions of an image, newest first (from images/*.json)."""
        metas = []
        for f in (self.data / "images").glob(f"{name}@*.json"):
            try:
                metas.append(json.loads(f.read_text()))
            except ValueError:
                pass
        return [m["hash"] for m in sorted(metas, key=lambda m: m.get("built", ""), reverse=True) if "hash" in m]

    # --- instances
    def instance(self, name: str) -> dict:
        """An instance's settings: from the config, or the defaults for ad-hoc ones (-i NAME)."""
        return self.instances.get(name) or dict(self.instance_defaults)

    def mounts(self, name: str) -> dict[str, dict]:
        """Configured mounts of an instance: {name: {path, readonly}}."""
        out = {}
        for m, v in (self.instance(name).get("mounts") or {}).items():
            if v is None:                                      # `share: null` drops a default mount
                continue
            v = v if isinstance(v, dict) else {"path": v}
            if not v.get("path"):
                die(f"instance '{name}' mount '{m}': no path")
            out[valid(m)] = {"path": str(self.path(v["path"])), "readonly": bool(v.get("readonly"))}
        return out
