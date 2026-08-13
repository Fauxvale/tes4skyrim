"""Package the TESGameSelect starter mod as a distributable zip.

TESGameSelect ("Threads of Prophecy") is the new-game game selector: it
intercepts MQ101 so the player picks which converted world to start in. Unlike
every other mod in output/, it is not converted from a TES4 plugin — its Data
folder is COMMITTED, prebuilt, at TESGameSelect/dist/. Nothing needs generating;
it only needs wrapping so it installs like any other converted plugin.

The archive mirrors what `convert.py --pack-zip-only` produces for a converted
plugin — output/Finished Mods/<name>.zip, contents rooted as a Data folder — so
a user installs it exactly the same way and a mod manager sees the same shape.

Usage:
  python tools/package_start_mod.py     # -> output/Finished Mods/TESGameSelect.zip
  python tools/package_start_mod.py --output-dir PATH
"""

import argparse
import sys
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from output_layout import finished_dir

MOD_NAME = "TESGameSelect"

# The committed, prebuilt Data folder. Not output/TESGameSelect/ — that is
# where tools/make_game_select_esp.py REBUILDS the plugin from the installed
# Skyrim.esm, which needs the game present and is not what shipping requires.
DIST_DIR = SCRIPT_DIR / MOD_NAME / "dist"


def package(out_root: Path) -> int:
    if not DIST_DIR.is_dir():
        print(f"ERROR: {DIST_DIR} not found — the prebuilt starter mod is "
              f"missing from the repository.")
        return 1

    files = sorted(p for p in DIST_DIR.rglob('*') if p.is_file())
    if not files:
        print(f"ERROR: {DIST_DIR} is empty — nothing to package.")
        return 1

    zip_path = finished_dir(out_root) / f"{MOD_NAME}.zip"

    print("=" * 54)
    print("  PACKAGE START MOD")
    print("=" * 54)
    print(f"  Source: {DIST_DIR}")
    print(f"  Output: {zip_path}")
    print()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for src in files:
            # Paths are relative to dist/, so the archive root IS the Data
            # folder: TESGameSelect.esp, scripts\, seq\ all sit at top level.
            arc = src.relative_to(DIST_DIR)
            zf.write(src, arcname=str(arc))
            print(f"  + {arc}")

    size = zip_path.stat().st_size
    print()
    print(f"Packaged {len(files)} file(s) -> {zip_path} ({size:,} bytes)")
    print("Install it like any other converted mod: the archive root is the "
          "Data folder.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Package the TESGameSelect starter mod as a zip.")
    ap.add_argument("--output-dir", metavar="PATH",
                    help="Output directory (default: output/ in project root)")
    args = ap.parse_args()
    out_root = (Path(args.output_dir) if args.output_dir
                else SCRIPT_DIR / "output")
    return package(out_root)


if __name__ == "__main__":
    sys.exit(main())
