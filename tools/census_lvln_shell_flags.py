"""Census ACBS Template Flags on NPC_ template shells in a Skyrim ESM/ESP.

A "shell" is an NPC_ whose TPLT points at an LVLN -- the indirection this
converter mints for every placed TES4 LVLC (tes5_import/leveled_actors.py).

Why this exists: a shell must only declare inheritance for categories the
template actually supplies.  Skyrim's template-copy routine null-checks the
*old* pointer but not the *new* one, so inheriting a category the template
leaves empty is a hard crash during initial data load (this is what "Use Attack
Data on a template with no ATKD" did -- main-menu CTD, GOG RVA 0x14037b4aa).
Vanilla is the authority on which combinations are safe.

Bit N of the U16 at ACBS+0x1A, verified in the GOG exe at 0x140197e20:
  0x0001 Traits    0x0020 AI Packages  0x0400 Def Pack List
  0x0002 Stats     0x0040 Model/Anim   0x0800 Attack Data
  0x0004 Factions  0x0080 Base Data    0x1000 Keywords
  0x0008 Spells    0x0100 Inventory
  0x0010 AI Data   0x0200 Script

Usage:
    python tools/census_lvln_shell_flags.py                 # vanilla Skyrim.esm
    python tools/census_lvln_shell_flags.py path/to/File.esm
    python tools/census_lvln_shell_flags.py output/Oblivion.esm/Oblivion.esm
"""

from __future__ import annotations

import struct
import sys
import zlib
from collections import Counter
from pathlib import Path

DEFAULT_ESM = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\Skyrim Special Edition"
    r"\Data\Skyrim.esm"
)

FLAG_NAMES = [
    (0x0001, "Traits"), (0x0002, "Stats"), (0x0004, "Factions"),
    (0x0008, "Spells"), (0x0010, "AI Data"), (0x0020, "AI Packages"),
    (0x0040, "Model"), (0x0080, "Base Data"), (0x0100, "Inventory"),
    (0x0200, "Script"), (0x0400, "Def Pack List"), (0x0800, "Attack Data"),
    (0x1000, "Keywords"),
]

COMPRESSED = 0x00040000


def _subrecords(data: bytes):
    o = 0
    while o + 6 <= len(data):
        sig = data[o:o + 4]
        size = struct.unpack_from("<H", data, o + 4)[0]
        o += 6
        yield sig, data[o:o + size]
        o += size


def scan(path: Path) -> tuple[dict[int, int], dict[int, int], set[int], set[int]]:
    """Return (npc_template_flags, npc_tplt, npcs_with_attack_data, lvln_fids)."""
    d = path.read_bytes()
    flags_by_fid: dict[int, int] = {}
    tplt_by_fid: dict[int, int] = {}
    with_attack: set[int] = set()
    lvln: set[int] = set()

    o = 0
    while o + 24 <= len(d):
        sig = d[o:o + 4]
        size = struct.unpack_from("<I", d, o + 4)[0]
        if sig == b"GRUP":
            o += 24
            continue
        rec_flags = struct.unpack_from("<I", d, o + 8)[0]
        fid = struct.unpack_from("<I", d, o + 12)[0]
        body = d[o + 24:o + 24 + size]
        if sig == b"LVLN":
            lvln.add(fid)
        elif sig == b"NPC_":
            if rec_flags & COMPRESSED:
                try:
                    body = zlib.decompress(body[4:])
                except zlib.error:
                    body = b""
            for ssig, sdata in _subrecords(body):
                if ssig == b"ACBS" and len(sdata) >= 24:
                    # ACBS: Flags(U32) MagickaOff(S16) StaminaOff(S16) Level(U16)
                    # CalcMin(U16) CalcMax(U16) Speed(U16) Disposition(S16)
                    # -> Template Flags (U16) is at byte 18, not 16. (Offset 16
                    # is Disposition, which is always 0 and silently reads as
                    # "inherits nothing".)
                    flags_by_fid[fid] = struct.unpack_from("<H", sdata, 18)[0]
                elif ssig == b"TPLT" and len(sdata) == 4:
                    tplt_by_fid[fid] = struct.unpack_from("<I", sdata, 0)[0]
                elif ssig in (b"ATKD", b"ATKR", b"ATKE"):
                    with_attack.add(fid)
        o += 24 + size

    return flags_by_fid, tplt_by_fid, with_attack, lvln


def describe(value: int) -> str:
    names = [n for bit, n in FLAG_NAMES if value & bit]
    return " | ".join(names) if names else "(none)"


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ESM
    if not path.is_file():
        print(f"not found: {path}")
        return 1

    flags_by_fid, tplt_by_fid, with_attack, lvln = scan(path)
    shells = [f for f, t in tplt_by_fid.items() if t in lvln]

    print(f"{path.name}")
    print(f"  NPC_ records ........ {len(flags_by_fid)}")
    print(f"  LVLN records ........ {len(lvln)}")
    print(f"  shells (TPLT->LVLN) . {len(shells)}")
    print(f"  NPC_ with ATKD/R/E .. {len(with_attack)}")

    if not shells:
        return 0

    counts = Counter(flags_by_fid.get(f, 0) for f in shells)
    print("\ntemplate-flag values among shells:")
    for value, n in counts.most_common():
        print(f"  {value:#06x}  n={n:<5} {describe(value)}")

    risky = [f for f in shells if flags_by_fid.get(f, 0) & 0x0800]
    print(f"\nshells inheriting Attack Data (0x0800): {len(risky)}/{len(shells)}")
    if risky:
        print("  WARNING: each of these crashes at load if its template has no ATKD.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
