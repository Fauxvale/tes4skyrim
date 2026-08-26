"""Package the generated AutoConvertLOD mod as a distributable zip.

`tools/release/create_lod.py` bakes every plugin's distant LOD ONCE into a single
standalone mod folder, output/AutoConvertLOD/. That folder is already a Data
folder — meshes/terrain/, textures/terrain/ — so shipping it needs no
generation, only wrapping.

The archive mirrors what `convert.py --pack-zip-only` produces for a converted
plugin and what `tools/release/package_start_mod.py` produces for the starter mod:
output/Finished Mods/<name>.zip with the contents rooted as a Data folder, so a
user installs it exactly the same way and a mod manager sees the same shape.
Unlike the plugin zip, this one packs the whole tree rather than a handful of
extensions — LOD is thousands of loose .bto/.btr/.dds tiles, not a plugin plus
a BSA.

Install it AFTER the plugins it covers.

Usage:
  python tools/release/pack_lod.py             # -> output/Finished Mods/AutoConvertLOD.zip
  python tools/release/pack_lod.py --output-dir PATH
"""

import argparse
import sys
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from asset_convert.sibling_lod import LOD_DIR_NAME
from output_layout import finished_dir


def package(out_root: Path) -> int:
    src_root = out_root / LOD_DIR_NAME
    if not src_root.is_dir():
        print(f"ERROR: {src_root} not found — run Create LOD first.")
        return 1

    files = sorted(p for p in src_root.rglob('*') if p.is_file())
    if not files:
        print(f"ERROR: {src_root} is empty — run Create LOD first.")
        return 1

    zip_path = finished_dir(out_root) / f"{LOD_DIR_NAME}.zip"

    print("=" * 54)
    print("  PACK LOD")
    print("=" * 54)
    print(f"  Source: {src_root}")
    print(f"  Output: {zip_path}")
    print(f"  Files:  {len(files):,}")
    print()

    # Written to a temp name and moved into place, so an interrupted run never
    # leaves a truncated archive sitting where the GUI reads "already packed".
    tmp_path = zip_path.with_suffix(".zip.part")
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, src in enumerate(files, 1):
                # Relative to AutoConvertLOD/, so the archive root IS the Data
                # folder: meshes\ and textures\ sit at top level.
                zf.write(src, arcname=str(src.relative_to(src_root)))
                # Thousands of tiles: a line each would bury the log, so report
                # progress periodically instead.
                if i % 500 == 0 or i == len(files):
                    print(f"  {i:,}/{len(files):,} packed", flush=True)
        zip_path.unlink(missing_ok=True)
        tmp_path.replace(zip_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    size = zip_path.stat().st_size
    print()
    print(f"Packaged {len(files):,} file(s) -> {zip_path} ({size:,} bytes)")
    print("Install it like any other converted mod: the archive root is the "
          "Data folder. Install it AFTER the plugins it covers.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Package the generated AutoConvertLOD mod as a zip.")
    ap.add_argument("--output-dir", metavar="PATH",
                    help="Output directory (default: output/ in project root)")
    args = ap.parse_args()
    out_root = (Path(args.output_dir) if args.output_dir
                else SCRIPT_DIR / "output")
    return package(out_root)


if __name__ == "__main__":
    sys.exit(main())
