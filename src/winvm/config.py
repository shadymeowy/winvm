"""Configuration: machine settings (winvm.yaml) and image definitions (one NAME.yaml per
image), their paths and content hashes (an image is rebuilt when its hash changes, like a
Docker layer).

Image files are found by name in `images_dir` (~/.config/winvm/images), then among the
built-in ones (data/images); any other file is named by its path (`winvm up ./x.yaml`,
`from: ../shared/base.yaml`) and remembered, so later commands find it from anywhere.
A file's folder is its context: relative paths in it are relative to the file.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import unquote

import yaml

from .util import Fatal, check_mount_points, content_hash, die, log, run, sha256_file, valid, warn, win_path, seconds

DATA = Path(__file__).parent / "data"
DEFAULT_FILE = DATA / "winvm.yaml"
BUILTIN = DATA / "images"
GUEST = DATA / "guest"
# image fields that change what an ISO install produces (`slim` only changes how fast)
BASE_KEYS = ("edition", "index", "hostname", "user", "password", "timezone", "locale", "keyboard",
             "disk_size", "debloat")
FROM_KEYS = {"from", "steps", "run", "desktop", "password"}
RUN_KEYS = {"ram", "cpus", "mounts"}


def merge(a, b):
    """b over a, recursively for mappings."""
    if isinstance(a, dict) and isinstance(b, dict):
        return {**a, **{k: merge(a[k], v) if k in a else v for k, v in b.items()}}
    return b


def read_yaml(p: Path) -> dict:
    try:
        d = yaml.safe_load(p.read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        die(f"{p}: {e}")
    return d if isinstance(d, dict) else die(f"{p}: expected a mapping at the top level")


def config_home() -> Path:
    """~/.config/winvm, or under $XDG_CONFIG_HOME."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "winvm"


def find_config(explicit: str | None) -> Path | None:
    """-f FILE, $WINVM_CONFIG, ~/.config/winvm/winvm.yaml - or None (built-in settings only)."""
    for c in (explicit, os.environ.get("WINVM_CONFIG")):
        if c:
            p = Path(c).expanduser()
            return p.resolve() if p.exists() else die(f"config file not found: {p}")
    p = config_home() / "winvm.yaml"
    return p.resolve() if p.exists() else None


def mapping(value, where: str) -> dict:
    return value if isinstance(value, dict) else die(f"{where}: expected a mapping, got {value!r}")


def check_keys(d: dict, known: set, where: str) -> None:
    for k in d:
        if k not in known:
            hint = difflib.get_close_matches(str(k), known, n=1)
            die(f"{where}: unknown key `{k}`" + (f" - did you mean `{hint[0]}`?" if hint else
                                                  f" (allowed: {', '.join(sorted(known))})"))


def is_path(ref: str) -> bool:
    """An image reference that is a file path rather than a name."""
    return "/" in ref or ref.endswith((".yaml", ".yml"))


def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


class Config:
    def __init__(self, file: str | None = None, data_dir: str | None = None, cache_dir: str | None = None):
        self.file = find_config(file)
        self.base = self.file.parent if self.file else Path.cwd()
        raw = read_yaml(DEFAULT_FILE)
        if self.file:
            user = read_yaml(self.file)
            if old := sorted({"images", "instances", "defaults"} & user.keys()):
                die(f"{self.file}: `{'`, `'.join(old)}` no longer go in winvm.yaml. Each image is "
                    f"its own file now, ~/.config/winvm/images/NAME.yaml (`winvm config dump base` "
                    f"shows one); instance settings go in an image's `run:` or on the command line "
                    f"(`winvm up --ram 8G`). See the README, section Images.")
            check_keys(user, set(raw), str(self.file))
            # an empty section (`settings:` with everything below commented out) changes nothing
            raw = merge(raw, {k: v for k, v in user.items() if v is not None})
        self.raw = raw
        self.s = mapping(raw.get("settings") or {}, "settings")
        self.run_defaults = mapping(raw.get("run") or {}, "run")
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
        self.yes = False                                        # -y: download without asking

        # image definitions: built-ins, then your images_dir (may replace a built-in), then
        # files named by path earlier (recorded in instance/image metadata)
        self.builtin = read_yaml(BUILTIN / "base.yaml")      # also the defaults of ISO images
        self.images: dict[str, dict] = {}
        self.sources: dict[str, Path] = {}
        self.replaced: set[str] = set()                         # built-ins your files replace
        self.broken: dict[str, str] = {}                        # name -> why its file can't be used
        self.stale: dict[str, str] = {}                         # ISO image -> the hash its file asks for
        self.fresh: set[str] = set()                            # ISO images to build as defined (build)
        self._hash: dict[str, str] = {}
        for f in sorted(BUILTIN.glob("*.yaml")):
            self.load(f)
        d = self.s.get("images_dir")
        self.images_dir = self.path(d) if d else config_home() / "images"
        for f in sorted(self.images_dir.glob("*.yaml")) + sorted(self.images_dir.glob("*.yml")):
            self.load_quietly(f)
        for meta in [*(self.data / "instances").glob("*/instance.json"), *(self.data / "images").glob("*.json")]:
            try:
                src = json.loads(meta.read_text()).get("source")
            except (OSError, ValueError):
                continue
            if src and Path(src).is_file() and Path(src).stem not in self.sources:
                self.load_quietly(Path(src))

    # --- paths
    def folder(self, cli: str | None, env: str, key: str) -> Path:
        """data/cache dir: command line, environment (both relative to the current folder),
        else the setting (relative to the config file)."""
        if v := cli or os.environ.get(env):
            return Path(v).expanduser().resolve()
        return self.path(self.s.get(key) or die(f"settings.{key} is not set"), plain=True).resolve()

    def path(self, value, base: Path | None = None, plain=False) -> Path:
        """~, ${ENV}, ${data_dir}, ${cache_dir}; relative to `base` (default: the config's folder)."""
        def var(m):
            k = m[1] or m[2]
            if not plain and k in ("data_dir", "cache_dir"):
                return str(self.data if k == "data_dir" else self.cache)
            return os.environ.get(k, "")
        p = Path(os.path.expanduser(re.sub(r"\$\{(\w+)\}|\$(\w+)", var, str(value))))
        return p if p.is_absolute() else ((base or self.base) / p).resolve()

    # --- image definitions
    def load(self, f: Path) -> str:
        """Read an image file; its name is the file name. Returns the name."""
        f = f.resolve()
        if not f.is_file():
            die(f"no such image file: {f}")
        name = valid(f.stem)
        if (have := self.sources.get(name)) and have != f:
            if have.parent != BUILTIN:
                die(f"two image files named '{name}': {have} and {f} - rename one")
            self.replaced.add(name)                            # your file replaces the built-in
        elif have:
            return name
        d = read_yaml(f)
        if d.get("from") is not None:
            if d.get("iso") is not None:
                die(f"{f}: `iso` or `from`, not both")
            check_keys(d, FROM_KEYS, str(f))
            if not isinstance(d.get("desktop", False), bool):
                die(f"{f}: `desktop` is true or false")
            img = {**d, "steps": d.get("steps") or []}
        else:   # an ISO image: the built-in base's values, then the file's
            check_keys(d, set(self.builtin) | {"index"}, str(f))
            img = merge({k: v for k, v in self.builtin.items() if k not in ("setup", "run")}, d)
        self.images[name], self.sources[name] = img, f
        self._hash.clear()
        if "from" in img and is_path(str(img["from"])):       # `from: ../shared/base.yaml`
            img["from"] = self.load(self.path(img["from"], f.parent))
        return name

    def load_quietly(self, f: Path) -> None:
        """Load an image file found by looking around; a broken one fails only the commands
        that use it (a typo in one file shouldn't stop `winvm ps`)."""
        try:
            self.load(f)
        except Fatal as e:
            self.broken.setdefault(f.stem, str(e))

    def ref(self, ref: str) -> str:
        """An image named on the command line: a name, or a path to an image file."""
        if is_path(ref):
            return self.load(Path(ref).expanduser())
        if ref in self.broken:
            die(self.broken[ref])
        return valid(ref)

    def image(self, name: str) -> dict:
        if name in self.images:
            return self.images[name]
        if name in self.broken:
            die(self.broken[name])
        if self.prebuilt(name):
            die(f"image '{name}' is built, but its file is gone (or it was `save`d): it can be "
                f"used, not rebuilt - give its file's path to rebuild it")
        die(f"no image '{name}': no {name}.yaml in {self.images_dir} or the built-ins, and nothing "
            f"built under that name (an image file elsewhere: give its path)")

    def prebuilt(self, name: str) -> bool:
        """A name with built versions but no definition (file moved/deleted, or `save`d):
        usable like a Docker image by name - as is, never rebuilt."""
        return name not in self.images and bool(self.built_hashes(name))

    def context(self, name: str) -> Path:
        """The folder of an image's file: its relative paths start there."""
        return self.sources[name].parent

    def parent(self, name: str) -> str | None:
        return None if self.prebuilt(name) else self.image(name).get("from")

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
            p = self.path(img["iso"], self.context(name))
        else:
            p = next(iter(self.linked_isos()), None)
        if not (p and p.exists()):
            die(f"image '{name}' needs a Windows ISO: `winvm up --iso PATH` (official ISO from "
                f"microsoft.com/software-download), or `iso:` in {self.sources[name]}")
        return p

    def linked_isos(self) -> list[Path]:
        """The ISOs in ${data_dir}/iso (linked by `--iso`, or downloaded), newest first;
        links to moved or deleted files don't count. lstat: a link counts from when it was
        made (or renewed by `--iso`)."""
        isos = [p for p in (self.data / "iso").glob("*.iso") if p.exists()]
        return sorted(isos, key=lambda q: q.lstat().st_mtime, reverse=True)

    def iso_hashes(self) -> dict[str, str]:
        """The sha256 of each ISO seen so far, by iso_key."""
        try:
            return json.loads((self.data / "iso" / ".sha256.json").read_text())
        except (OSError, ValueError):
            return {}

    @staticmethod
    def iso_key(iso: Path) -> str:
        st = iso.stat()
        return f"{iso.resolve()}|{st.st_size}|{int(st.st_mtime)}"

    def iso_is_pinned(self, iso: Path) -> bool:
        """Is this the ISO winvm can download again (settings.windows)? By its known sha256,
        else (never checksummed: 8 GB take a while) by name and size."""
        pin = self.s.get("windows") or {}
        if not pin or not iso.exists():
            return False
        if sha := self.iso_hashes().get(self.iso_key(iso)):
            return sha == pin.get("sha256")
        return iso.name == unquote(pin["url"].split("?")[0].rsplit("/", 1)[-1]) and iso.stat().st_size == pin.get("size")

    def iso_id(self, name: str) -> str:
        """The image's ISO by content: sha256, computed once per file (cached by path,
        size and mtime) - the same ISO under another name or path is the same input."""
        iso = self.iso(name)
        cache = self.data / "iso" / ".sha256.json"
        known = self.iso_hashes()
        key = self.iso_key(iso)
        if key not in known:
            log(f"checksumming {iso.name} (once)")
            known[key] = sha256_file(iso)
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_name(f"{cache.name}.{os.getpid()}")   # builds may run side by side
            tmp.write_text(json.dumps(known, indent=1) + "\n")
            tmp.replace(cache)
            if known[key] == (self.s.get("windows") or {}).get("sha256"):
                log(f"{iso.name}: matches Microsoft's published image")
            else:
                warn(f"{iso.name} is not an ISO winvm knows - make sure it is an official one from Microsoft")
        return known[key]

    def steps(self, name: str) -> list[dict]:
        """An image's steps (`setup` for ISO images, `steps` otherwise), checked and normalized."""
        img = self.image(name)
        offline = "from" not in img
        out = []
        for i, st in enumerate(img.get("setup" if offline else "steps") or [], 1):
            where = f"image '{name}' step {i}"
            timeout = None
            if isinstance(st, dict) and "timeout" in st:
                st = dict(st)
                if offline:
                    die(f"{where}: `timeout` is for `steps` of images built `from` another")
                if "run" not in st:
                    die(f"{where}: `timeout` goes with `run`")
                try:
                    timeout = seconds(st.pop("timeout"))
                except ValueError as e:
                    die(f"{where}: {e}")
            if not isinstance(st, dict) or len(st) != 1:
                die(f"{where}: expected one of run / download / copy" + ("" if offline else " / restart"))
            (kind, arg), = st.items()
            if kind == "run" and isinstance(arg, str):
                out.append({"run": arg, **({"timeout": timeout} if timeout else {})})
            elif kind == "download" and isinstance(arg, dict) and {"url", "sha256"} <= arg.keys():
                if not re.fullmatch(r"[0-9a-fA-F]{64}", str(arg["sha256"])):
                    die(f"{where}: sha256 must be 64 hex digits")
                fname = str(arg.get("name") or unquote(str(arg["url"]).split("?")[0].rstrip("/").rsplit("/", 1)[-1]))
                if fname in ("", ".", "..") or "/" in fname or "\\" in fname:
                    die(f"{where}: `name` must be a plain file name, got {fname!r}")
                out.append({"download": {"url": arg["url"], "sha256": str(arg["sha256"]).lower(), "name": fname}})
            elif kind == "copy" and isinstance(arg, list) and 1 <= len(arg) <= (1 if offline else 2):
                if not (src := self.path(arg[0], self.context(name))).exists():
                    die(f"{where}: no such file or folder: {src}")
                out.append({"copy": [str(src), *(str(a) for a in arg[1:])]})
            elif kind == "restart" and arg is True and not offline:
                out.append({"restart": True})
            else:
                die(f"{where}: bad `{kind}` (see `winvm config dump base` and the README)"
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

    def own_key(self) -> str:
        """winvm's own public key: what an image must trust for winvm to reach it (your
        `ssh_pubkeys` are brought up to date at every start instead)."""
        return self.ensure_key().with_suffix(".pub").read_text().strip()

    def authorized_keys(self) -> str:
        keys = [self.ensure_key().with_suffix(".pub"), *(self.path(p) for p in self.s.get("ssh_pubkeys") or [])]
        return "".join(k.read_text().strip() + "\n" for k in keys if k.exists())   # one per line

    def slim_key(self, name: str) -> str:
        """What the captured, debloated image depends on: ISO, edition, offline servicing,
        the virtio drivers injected into it."""
        img = self.image(name)
        return sha({"iso": self.iso_id(name), "edition": img.get("edition"), "index": img.get("index"),
                     "strip": (img.get("debloat") or {}).get("strip_payloads"),
                     "virtio": self.s["virtio"]["sha256"],
                     "pe": content_hash(GUEST / "pe.cmd"), "lists": content_hash(GUEST / "lists")})

    def hash(self, name: str) -> str:
        """Content hash of an image: its definition, the files it copies, and the image below.
        Not where its file is: moving a file rebuilds nothing."""
        if name not in self._hash and self.prebuilt(name):     # as built: its newest version
            self._hash[name] = self.built_hashes(name)[0]
        if name not in self._hash:
            img = self.image(name)
            steps = [dict(st, src=content_hash(Path(st["copy"][0]))) if "copy" in st else st
                     for st in self.steps(name)]
            if "from" in img:
                self.chain(name)                               # fails on cycles
                d = {"from": self.hash(img["from"]), "steps": steps,
                     **{k: img[k] for k in ("desktop", "password") if img.get(k) not in (None, False)}}
            else:
                try:
                    iso = self.iso_id(name)
                except Fatal:
                    # the ISO is gone (deleted to save space?): keep using the newest build
                    if built := self.built_hashes(name):
                        self._hash[name] = built[0]
                        return built[0]
                    raise
                d = {**{k: img.get(k) for k in BASE_KEYS}, "iso": iso,
                     "setup": steps, "guest": content_hash(GUEST), "key": self.own_key(),
                     "tools": [self.s["virtio"]["sha256"], self.s["openssh"]["sha256"]]}
            h = sha(d)
            # an ISO image whose file (or winvm) changed since it was built: reinstalling Windows
            # takes up to an hour, so the built version stays in use until it is built on purpose
            if "from" not in img and name not in self.fresh and (built := self.built_hashes(name)) \
                    and h not in built:
                self.stale[name] = h
                h = built[0]
            self._hash[name] = h
        return self._hash[name]

    def forget(self) -> None:
        """Drop cached hashes (after an ISO arrived, or an image was built)."""
        self._hash.clear()
        self.stale.clear()

    def built_hashes(self, name: str) -> list[str]:
        """Hashes of the built versions of an image, newest first (from images/*.json)."""
        metas = []
        for f in (self.data / "images").glob(f"{name}@*.json"):
            try:
                metas.append(json.loads(f.read_text()))
            except ValueError:
                pass
        return [m["hash"] for m in sorted(metas, key=lambda m: m.get("built", ""), reverse=True) if "hash" in m]

    # --- how instances run
    def run_settings(self, image: str | None, overrides: dict | None = None) -> dict:
        """ram/cpus/mounts of an instance: the machine's `run`, then the `run:` of its image and
        the images below it (bottom first), then the instance's own overrides. Mount paths are
        relative to the file that names them; `name: null` drops a mount."""
        layers = [(self.run_defaults, self.base, str(self.file or DEFAULT_FILE))]
        if image in self.images:
            layers += [(self.images[n].get("run") or {}, self.context(n), str(self.sources[n]))
                       for n in self.chain(image) if n in self.images]   # (a saved image has no file)
        layers.append((overrides or {}, Path.cwd(), "instance settings"))
        out: dict = {}
        mounts: dict[str, dict] = {}
        for settings, base, where in layers:
            check_keys(mapping(settings, f"{where}: run"), RUN_KEYS, f"{where}: run")
            for k, v in settings.items():
                if k == "ram" and isinstance(v, dict):         # dynamic memory: {min, max}
                    check_keys(v, {"min", "max"}, f"{where}: run.ram")
                if k != "mounts":
                    out[k] = v
                    continue
                for m, mv in mapping(v or {}, f"{where}: run.mounts").items():
                    if mv is None:
                        mounts.pop(m, None)
                        continue
                    mv = mv if isinstance(mv, dict) else {"path": mv}
                    check_keys(mv, {"path", "readonly", "at"}, f"{where}: run.mounts.{m}")
                    if not mv.get("path"):
                        die(f"{where}: mount '{m}' has no path")
                    at = mv.get("at") and (win_path(mv["at"]) or die(
                        f"{where}: mount '{m}': `at: {mv['at']}` is not a Windows folder path like C:\\work"))
                    mounts[valid(m)] = {"path": str(self.path(mv["path"], base)), "readonly": bool(mv.get("readonly")),
                                        **({"at": at} if at else {})}
        check_mount_points(mounts, f"{image or 'machine'}: run.mounts")
        return {**out, "mounts": mounts}
