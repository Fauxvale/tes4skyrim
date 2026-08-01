"""Print a TES4 script's original source beside its converted Papyrus.

The quest-script conversion audit (docs/quest_script_conversion_audit.md) needs
the SCTX source of a SCPT record and the emitted .psc side by side. The export
stores SCTX with \r\n escaped, so it is unreadable without unescaping.

Usage:
    python tools/script_pair.py MQ02Script
    python tools/script_pair.py MQ02Script SQ08Script --orig-only
    python tools/script_pair.py --list-quest          # every SCHR.Type=1 EditorID
    python tools/script_pair.py --file temp/names.txt --conv-only

Options:
    -f/--plugin NAME   plugin directory under export/ (default Oblivion.esm)
    --orig-only        print only the TES4 source
    --conv-only        print only the converted Papyrus
    --list-quest       list quest-script EditorIDs (SCHR.Type=1) and exit
    --list-all         list every SCPT EditorID and exit
    --file PATH        read newline-separated EditorIDs from PATH
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _index(plugin: str) -> dict[str, dict[str, str]]:
    """Map EditorID -> {'sctx': source, 'type': SCHR.Type} for every SCPT."""
    path = REPO / "export" / plugin / "SCPT.txt"
    if not path.exists():
        sys.exit(f"no export at {path} — run: python convert.py -f {plugin} --export-only")

    out: dict[str, dict[str, str]] = {}
    for rec in path.read_text(encoding="utf-8", errors="replace").split("---RECORD_BEGIN---"):
        edid = re.search(r"^EditorID=(.*)$", rec, re.M)
        if not edid:
            continue
        sctx = re.search(r"^SCTX=(.*)$", rec, re.M)
        stype = re.search(r"^SCHR\.Type=(\d+)$", rec, re.M)
        out[edid.group(1).strip()] = {
            "sctx": sctx.group(1) if sctx else "",
            "type": stype.group(1) if stype else "?",
        }
    return out


def _unescape(sctx: str) -> str:
    return sctx.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n").replace("\\t", "\t")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", help="script EditorIDs")
    ap.add_argument("-f", "--plugin", default="Oblivion.esm")
    ap.add_argument("--orig-only", action="store_true")
    ap.add_argument("--conv-only", action="store_true")
    ap.add_argument("--list-quest", action="store_true")
    ap.add_argument("--list-all", action="store_true")
    ap.add_argument("--file", help="file of newline-separated EditorIDs")
    args = ap.parse_args()

    idx = _index(args.plugin)

    if args.list_quest or args.list_all:
        for name, rec in sorted(idx.items()):
            if args.list_all or rec["type"] == "1":
                print(name)
        return 0

    names = list(args.names)
    if args.file:
        names += Path(args.file).read_text().split()
    if not names:
        ap.error("give at least one EditorID, --file, or a --list-* flag")

    src_dir = REPO / "output" / args.plugin / "scripts" / "Source"

    for name in names:
        rec = idx.get(name)
        print(f"\n{'=' * 78}\n=== {name}   (SCHR.Type={rec['type'] if rec else '?'})\n{'=' * 78}")

        if not args.conv_only:
            print(f"\n--- TES4 source ({args.plugin}/SCPT.txt SCTX) " + "-" * 30)
            if rec is None:
                print(f"!! no SCPT record named {name}")
            elif not rec["sctx"]:
                print("!! record has no SCTX field")
            else:
                print(_unescape(rec["sctx"]))

        if not args.orig_only:
            psc = src_dir / f"TES4_{name}.psc"
            print(f"\n--- converted ({psc.relative_to(REPO)}) " + "-" * 30)
            if psc.exists():
                print(psc.read_text(encoding="utf-8", errors="replace"))
            else:
                print(f"!! not built — run: python convert.py -f {args.plugin} --scripts-only")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
