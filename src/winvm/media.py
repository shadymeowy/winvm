"""ISO files in pure Python (pycdlib): the payload CD winvm hands to Windows Setup, and
reading which editions a Windows ISO holds - no 7z, xorriso or wimlib on the host."""
from __future__ import annotations

import re
import struct
from pathlib import Path
from xml.sax.saxutils import unescape

import pycdlib

from .util import die, log


# characters Windows (or Joliet) can't have in a file name
BAD_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*;]')
RESERVED = re.compile(r"(CON|PRN|AUX|NUL|COM\d|LPT\d)(\..*)?", re.I)


def write_iso(src: Path, out: Path, label: str) -> Path:
    """A data CD (ISO 9660 + Joliet) with the files under src. Windows reads the Joliet
    tree, which has the real names; the ISO 9660 tree beside it has made-up unique names,
    and folders deeper than ISO 9660 allows sit at its root instead."""
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, vol_ident=label)
    used: dict[str, set[str]] = {}

    def iso_name(parent: str, name: str, is_dir: bool) -> str:
        """A unique ISO 9660 name in `parent`."""
        stem, ext = (name, "") if is_dir else (Path(name).stem, Path(name).suffix[1:])

        def clean(s: str, n: int) -> str:
            return re.sub(r"[^A-Z0-9_]", "_", s.upper())[:n]

        base, ext = clean(stem, 8 if is_dir else 26), clean(ext, 3)
        taken = used.setdefault(parent, set())
        cand, n = base, 0
        while (cand + "." + ext if ext else cand) in taken:
            n += 1
            cand = f"{base[:8 - len(str(n)) - 1] if is_dir else base[:20]}_{n}"
        full = cand + ("." + ext if ext else "")
        taken.add(full)
        return full if is_dir else full + (";1" if ext else ".;1")

    def check(p: Path) -> None:
        """Names the CD or Windows can't hold fail here, on the host, not in the guest."""
        name, why = p.name, None
        if len(name.encode()) > 64:                            # pycdlib counts UTF-8 bytes
            why = "over 64 bytes long (Joliet's limit)"
        elif BAD_CHARS.search(name) or name.endswith((".", " ")) or RESERVED.fullmatch(name):
            why = "not a valid Windows file name"
        if why:
            die(f"can't put {p.relative_to(src)} on the install CD: its name is {why} - rename it")

    def add(folder: Path, iso_dir: str, joliet_dir: str) -> None:
        names: dict[str, Path] = {}
        for p in sorted(folder.iterdir()):
            check(p)
            if (twin := names.setdefault(p.name.lower(), p)) is not p:
                die(f"{twin.relative_to(src)} and {p.name} differ only in case: Windows can't hold both"
                    f" - rename one")
            jpath = f"{joliet_dir.rstrip('/')}/{p.name}"
            if p.is_dir():
                # ISO 9660 allows 7 levels, files included: deeper folders go to its root
                parent = iso_dir if iso_dir.count("/") < 6 else "/"
                ipath = f"{parent.rstrip('/')}/{iso_name(parent, p.name, True)}"
                iso.add_directory(ipath, joliet_path=jpath)
                add(p, ipath, jpath)
            else:
                iso.add_file(str(p), f"{iso_dir.rstrip('/')}/{iso_name(iso_dir, p.name, False)}",
                             joliet_path=jpath)

    add(src, "/", "/")
    tmp = out.with_suffix(".tmp")
    iso.write(str(tmp))
    iso.close()
    tmp.replace(out)
    return out


def editions(iso_path: Path) -> list[tuple[int, str]]:
    """(index, name) of the images in a Windows ISO's install.wim/esd/swm: the WIM header
    points to an uncompressed XML description, so only a few KB are read."""
    iso = pycdlib.PyCdlib()
    try:
        iso.open(str(iso_path))
    except (OSError, pycdlib.pycdlibexception.PyCdlibException) as e:
        die(f"{iso_path.name}: not a readable ISO ({e})")
    try:
        for name in ("install.wim", "install.esd", "install.swm"):
            # Microsoft's ISOs keep their files in UDF; others may have Joliet or ISO 9660 only
            where = [{"udf_path": f"/sources/{name}"}] if iso.has_udf() else []
            where += [{"joliet_path": f"/sources/{name}"}] if iso.has_joliet() else []
            where += [{"iso_path": f"/SOURCES/{name.upper()};1"}]
            for kw in where:
                try:
                    f = iso.open_file_from_iso(**kw)
                    break
                except pycdlib.pycdlibexception.PyCdlibException:
                    continue
            else:
                continue
            with f:
                hdr = f.read(208)
                if hdr[:8] != b"MSWIM\0\0\0":
                    die(f"{iso_path.name}: /sources/{name} is not a WIM file")
                size = int.from_bytes(hdr[0x48:0x4F], "little")   # XML resource: 7-byte size, offset
                f.seek(struct.unpack_from("<Q", hdr, 0x50)[0])
                xml = f.read(size).decode("utf-16-le", "replace")
            return [(int(i), unescape(n)) for i, n in
                    re.findall(r'<IMAGE INDEX="(\d+)">.*?<NAME>(.*?)</NAME>', xml, re.S)]
        die(f"{iso_path.name}: no sources/install.wim, .esd or .swm - a Windows 10/11 ISO?")
    finally:
        iso.close()


def check_edition(iso_path: Path, edition: str | None, index) -> None:
    """Fail early (on the host) if the ISO lacks the image's edition (pe.cmd matches the
    name ignoring case, and takes `index` over `edition`)."""
    eds = editions(iso_path)
    if index:
        if not any(str(i) == str(index) for i, _ in eds):
            die(f"{iso_path.name} has no image index {index!r} (winvm editions)")
    elif not edition:
        die("the image sets neither `edition` nor `index` (winvm editions)")
    elif not any(n.lower() == str(edition).lower() for _, n in eds):
        die(f"{iso_path.name} has no edition '{edition}'; it has: {', '.join(n for _, n in eds)}")
    else:
        log(f"edition '{edition}' found in {iso_path.name}")
