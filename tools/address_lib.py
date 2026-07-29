"""Address Library (versionlib) reader: map SKSE stable IDs <-> per-build RVAs.

Why this exists: crash logs come from the user's *Steam* SkyrimSE.exe (1.6.1170),
which is DRM-packed and cannot be disassembled.  The only statically readable
build is the GOG/AE copy at 1.6.659.  A crash frame like

    SkyrimSE.exe+14E0460 -> 107327+0x3A0

names an Address Library *stable ID* (107327) plus an offset, so the same code can
be located in the GOG exe by translating 107327 through both builds' versionlib
databases.  Without that translation the raw RVA lands in unrelated code and any
conclusion drawn from it is fiction.

Format: SKSE64 Address Library v2 ("database version 2"), little-endian.

Usage:
    # translate a crash-log frame to the GOG (disassemblable) build
    python tools/address_lib.py --id 107327 --to 1.6.659 --offset 0x3A0

    # which stable ID owns a raw RVA in a given build?
    python tools/address_lib.py --rva 0x14E0460 --from 1.6.1170

    # RVA of an ID in one build
    python tools/address_lib.py --id 107327 --from 1.6.1170
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

DEFAULT_DIRS = [
    Path(r"C:\Program Files (x86)\Steam\steamapps\common\Skyrim Special Edition")
    / "Data"
    / "SKSE"
    / "Plugins",
    Path(r"D:\Other Games\Skyrim Anniversary Edition") / "Data" / "SKSE" / "Plugins",
]


def find_versionlib(version: str, extra_dir: str | None = None) -> Path:
    """Locate versionlib-<a>-<b>-<c>-0.bin for a dotted version string."""
    stem = "versionlib-" + version.replace(".", "-") + "-0"
    dirs = [Path(extra_dir)] if extra_dir else []
    dirs += DEFAULT_DIRS
    for d in dirs:
        cand = d / (stem + ".bin")
        if cand.is_file():
            return cand
    searched = ", ".join(str(d) for d in dirs)
    raise SystemExit(f"no {stem}.bin found in: {searched}")


class Reader:
    def __init__(self, data: bytes):
        self.d = data
        self.p = 0

    def u8(self) -> int:
        v = self.d[self.p]
        self.p += 1
        return v

    def u16(self) -> int:
        v = struct.unpack_from("<H", self.d, self.p)[0]
        self.p += 2
        return v

    def i32(self) -> int:
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def u32(self) -> int:
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def u64(self) -> int:
        v = struct.unpack_from("<Q", self.d, self.p)[0]
        self.p += 8
        return v

    def string(self, n: int) -> str:
        v = self.d[self.p : self.p + n].decode("utf-8", "replace")
        self.p += n
        return v


def _read_value(r: Reader, width: int) -> int:
    """Read a value whose byte-width is encoded in the control nibble."""
    if width == 0:
        return r.u8()
    if width == 1:
        return r.u16()
    if width == 2:
        return r.u32()
    raise ValueError(f"bad value width {width}")


def load(path: Path) -> dict[int, int]:
    """Parse a versionlib v2 database into {stable_id: rva}."""
    r = Reader(path.read_bytes())
    fmt = r.i32()
    if fmt != 2:
        raise SystemExit(f"{path.name}: unsupported database format {fmt} (expected 2)")

    ver = [r.i32() for _ in range(4)]
    name_len = r.i32()
    r.string(name_len)
    ptr_size = r.i32()
    count = r.i32()

    out: dict[int, int] = {}
    prev_id = 0
    prev_off = 0

    for _ in range(count):
        ctl = r.u8()
        id_kind = ctl & 0x07  # low 3 bits: how the stable id is encoded
        off_kind = (ctl >> 3) & 0x07  # next 3 bits: how the offset is encoded
        # bit 6 (0x40): offset value is pre-divided by ptr_size

        if id_kind == 0:
            cur_id = r.u64()
        elif id_kind == 1:
            cur_id = prev_id + 1
        elif id_kind == 2:
            cur_id = prev_id + r.u8()
        elif id_kind == 3:
            cur_id = prev_id - r.u8()
        elif id_kind == 4:
            cur_id = prev_id + r.u16()
        elif id_kind == 5:
            cur_id = prev_id - r.u16()
        elif id_kind == 6:
            cur_id = r.u32()
        elif id_kind == 7:
            cur_id = r.u64()
        else:
            raise ValueError("unreachable")

        # Bit 6 means the *delta* forms are expressed in pointer-size units.
        # Absolute forms (0, 6, 7) are always raw byte offsets.
        scale = ptr_size if (ctl & 0x40) else 1

        if off_kind == 0:
            cur_off = r.u64()
        elif off_kind == 1:
            cur_off = prev_off + scale
        elif off_kind == 2:
            cur_off = prev_off + r.u8() * scale
        elif off_kind == 3:
            cur_off = prev_off - r.u8() * scale
        elif off_kind == 4:
            cur_off = prev_off + r.u16() * scale
        elif off_kind == 5:
            cur_off = prev_off - r.u16() * scale
        elif off_kind == 6:
            cur_off = r.u32()
        elif off_kind == 7:
            cur_off = r.u64()
        else:
            raise ValueError("unreachable")

        out[cur_id] = cur_off
        prev_id = cur_id
        prev_off = cur_off

    return out


def owning_id(db: dict[int, int], rva: int) -> tuple[int, int] | None:
    """Return (stable_id, delta) for the entry whose RVA is the closest <= rva."""
    best = None
    for sid, off in db.items():
        if off <= rva and (best is None or off > best[1]):
            best = (sid, off)
    if best is None:
        return None
    return best[0], rva - best[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--id", type=lambda s: int(s, 0), help="stable ID to look up")
    ap.add_argument("--rva", type=lambda s: int(s, 0), help="RVA to reverse-look-up")
    ap.add_argument(
        "--from",
        dest="src",
        default="1.6.1170",
        help="build the ID/RVA comes from (default 1.6.1170, the Steam crash-log build)",
    )
    ap.add_argument(
        "--to",
        dest="dst",
        default="1.6.659",
        help="build to translate into (default 1.6.659, the disassemblable GOG build)",
    )
    ap.add_argument(
        "--offset",
        type=lambda s: int(s, 0),
        default=0,
        help="byte offset inside the function (from a crash frame's '+0xNNN')",
    )
    ap.add_argument("--dir", help="extra directory to search for versionlib bins")
    args = ap.parse_args()

    if args.id is None and args.rva is None:
        ap.error("need --id or --rva")

    src_db = load(find_versionlib(args.src, args.dir))
    sid = args.id

    if sid is None:
        hit = owning_id(src_db, args.rva)
        if hit is None:
            print(f"no entry <= {args.rva:#x} in {args.src}")
            return 1
        sid, delta = hit
        print(f"{args.src}  RVA {args.rva:#x}  ->  ID {sid} + {delta:#x}")
        args.offset = args.offset or delta
    else:
        if sid not in src_db:
            print(f"ID {sid} not present in {args.src}")
            return 1
        print(f"{args.src}  ID {sid} -> RVA {src_db[sid]:#x} (+{args.offset:#x} = "
              f"{src_db[sid] + args.offset:#x})")

    if args.dst and args.dst != args.src:
        dst_db = load(find_versionlib(args.dst, args.dir))
        if sid not in dst_db:
            print(f"ID {sid} not present in {args.dst} (signature changed between builds)")
            return 1
        base = dst_db[sid]
        print(f"{args.dst}  ID {sid} -> RVA {base:#x} (+{args.offset:#x} = "
              f"{base + args.offset:#x})")
        print(f"\ndisassemble with:\n  python tools/skyrim_disasm.py "
              f'--exe "D:\\Other Games\\Skyrim Anniversary Edition\\SkyrimSE.exe" '
              f"--disasm {base:#x} --count 200")

    return 0


if __name__ == "__main__":
    sys.exit(main())
