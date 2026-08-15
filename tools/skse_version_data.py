#!/usr/bin/env python3
"""Read an SKSE plugin's exported SKSEPlugin_Version straight out of the DLL file.

Why this exists
---------------
SKSE reads the version struct with ``LOAD_LIBRARY_AS_IMAGE_RESOURCE``, which maps
the DLL as raw data and runs **no CRT initializers**. So it sees whatever bytes
are sitting in ``.data`` on disk. A plugin that fills the struct in a dynamic
initializer (a lambda, a constructor, ``strncpy`` at startup) leaves those bytes
zero, and SKSE's first check is::

    if (!version.dataVersion) return "disabled, bad version data";

That failure is invisible at build time -- the code looks correct and compiles
clean -- and only shows up as one line in ``skse64.log``. This tool reproduces
SKSE's exact read so the struct can be verified statically, before launching.

Usage:
    python tools/skse_version_data.py game_bridge/TESGameBridge.dll
    python tools/skse_version_data.py <dll> --json
"""

from __future__ import annotations

import argparse
import json
import struct
import sys

# Layout from references/skse64-master/skse64/PluginAPI.h (SKSEPluginVersionData).
_NAME = 256
_AUTHOR = 256
_EMAIL = 252
_COMPAT = 16

VERSION_INDEPENDENCE_FLAGS = {
    1 << 0: "AddressLibraryPostAE",
    1 << 1: "Signatures",
    1 << 2: "StructsPost629",
}


class PEError(RuntimeError):
    pass


def _rva_to_offset(data: bytes, rva: int, sections: list[tuple[int, int, int, int]]) -> int:
    for _, vaddr, vsize, raw_ptr in sections:
        if vaddr <= rva < vaddr + max(vsize, 1):
            return raw_ptr + (rva - vaddr)
    raise PEError(f"RVA {rva:#x} is not inside any section")


def _parse_pe(data: bytes):
    """Return (export_dir_rva, sections). Minimal PE32+ parse, no dependencies."""
    if data[:2] != b"MZ":
        raise PEError("not a PE file (no MZ)")
    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_off : pe_off + 4] != b"PE\0\0":
        raise PEError("not a PE file (no PE signature)")

    coff = pe_off + 4
    machine, num_sections = struct.unpack_from("<HH", data, coff)
    if machine != 0x8664:
        raise PEError(f"not a 64-bit DLL (machine {machine:#x}) -- SKSE requires x64")
    opt_size = struct.unpack_from("<H", data, coff + 16)[0]

    opt = coff + 20
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic != 0x20B:
        raise PEError(f"not PE32+ (optional header magic {magic:#x})")

    # Data directories start at +112 in PE32+; entry 0 is the export table.
    export_rva, _export_size = struct.unpack_from("<II", data, opt + 112)

    sec_off = opt + opt_size
    sections = []
    for i in range(num_sections):
        s = sec_off + i * 40
        name = data[s : s + 8].rstrip(b"\0").decode("ascii", "replace")
        vsize, vaddr, _rawsize, raw_ptr = struct.unpack_from("<IIII", data, s + 8)
        sections.append((name, vaddr, vsize, raw_ptr))

    if not export_rva:
        raise PEError("DLL has no export directory")
    return export_rva, sections


def find_export(data: bytes, want: str) -> int:
    """Return the file offset of an exported symbol's data, like SKSE's GetProcAddress."""
    export_rva, sections = _parse_pe(data)
    ed = _rva_to_offset(data, export_rva, sections)

    num_names = struct.unpack_from("<I", data, ed + 24)[0]
    func_rva = struct.unpack_from("<I", data, ed + 28)[0]
    name_rva = struct.unpack_from("<I", data, ed + 32)[0]
    ord_rva = struct.unpack_from("<I", data, ed + 36)[0]

    names_off = _rva_to_offset(data, name_rva, sections)
    ords_off = _rva_to_offset(data, ord_rva, sections)
    funcs_off = _rva_to_offset(data, func_rva, sections)

    for i in range(num_names):
        n_rva = struct.unpack_from("<I", data, names_off + i * 4)[0]
        n_off = _rva_to_offset(data, n_rva, sections)
        end = data.index(b"\0", n_off)
        if data[n_off:end].decode("ascii", "replace") == want:
            ordinal = struct.unpack_from("<H", data, ords_off + i * 2)[0]
            sym_rva = struct.unpack_from("<I", data, funcs_off + ordinal * 4)[0]
            return _rva_to_offset(data, sym_rva, sections)
    raise PEError(f"export {want!r} not found")


def read_version_data(path: str) -> dict:
    with open(path, "rb") as fh:
        data = fh.read()

    off = find_export(data, "SKSEPlugin_Version")
    p = off

    def u32() -> int:
        nonlocal p
        v = struct.unpack_from("<I", data, p)[0]
        p += 4
        return v

    def s(n: int) -> str:
        nonlocal p
        raw = data[p : p + n]
        p += n
        return raw.split(b"\0", 1)[0].decode("ascii", "replace")

    out = {
        "dataVersion": u32(),
        "pluginVersion": u32(),
        "name": s(_NAME),
        "author": s(_AUTHOR),
        "supportEmail": s(_EMAIL),
        "versionIndependenceEx": u32(),
        "versionIndependence": u32(),
    }
    out["compatibleVersions"] = [u32() for _ in range(_COMPAT)]
    out["seVersionRequired"] = u32()

    flags = [n for bit, n in VERSION_INDEPENDENCE_FLAGS.items() if out["versionIndependence"] & bit]
    out["versionIndependence_flags"] = flags

    # SKSE's own checks, in PluginManager::CheckPluginCompatibility order.
    problems = []
    if not out["dataVersion"]:
        problems.append(
            "dataVersion is 0 -> SKSE reports 'disabled, bad version data'. "
            "The struct is almost certainly filled by a DYNAMIC initializer; "
            "SKSE maps the DLL as a raw image and runs no initializers."
        )
    if not out["name"]:
        problems.append("name is empty -> SKSE reports 'disabled, no name specified'")
    if not flags and not out["versionIndependenceEx"]:
        compat = [v for v in out["compatibleVersions"] if v]
        if not compat:
            problems.append(
                "no versionIndependence flags AND no compatibleVersions -> "
                "SKSE will reject this on any runtime"
            )
    out["problems"] = problems
    out["would_load"] = not problems
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("dll", help="path to the SKSE plugin DLL")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    args = ap.parse_args(argv)

    try:
        info = read_version_data(args.dll)
    except (PEError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(info, indent=2))
        return 0 if info["would_load"] else 1

    for key in (
        "dataVersion",
        "pluginVersion",
        "name",
        "author",
        "supportEmail",
        "versionIndependenceEx",
        "versionIndependence",
        "seVersionRequired",
    ):
        print(f"{key}: {info[key]!r}")
    print(f"versionIndependence_flags: {info['versionIndependence_flags']}")
    compat = [f"{v:#010x}" for v in info["compatibleVersions"] if v]
    print(f"compatibleVersions: {compat or '[] (any, via AddressLibraryPostAE)'}")

    print()
    if info["would_load"]:
        print("OK - SKSE's compatibility checks would accept this plugin")
    else:
        for prob in info["problems"]:
            print(f"FAIL - {prob}")
    return 0 if info["would_load"] else 1


if __name__ == "__main__":
    sys.exit(main())
