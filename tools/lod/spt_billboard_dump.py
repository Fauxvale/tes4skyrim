#!/usr/bin/env python3
"""Convert Oblivion's shipped tree BILLBOARDS to viewable PNGs.

`textures/trees/billboards/<stem>.dds` is Bethesda's own render of each tree,
made from the real SpeedTree geometry -- so it is the reference for judging a
converted mesh by eye: silhouette, crown density, branch habit, and whether a
tree is supposed to carry leaves at all.

It settled two questions directly: `dtree01`'s billboard is a bare dead tree
with no foliage whatsoever (confirming the leafless fix), and `maniatree01`'s
shows the authored crown shape.

The DDS is alpha-cut foliage, so it is composited over a mid grey rather than
saved with alpha -- on a white or transparent background the leaf edges are
invisible.

NOTE these are 2-D renders. They cannot reveal a 3-D error, and the generator
must never be fitted to them (see docs/speedtree_engine_decomp.md); they are
for human comparison only.

Usage:
    python tools/lod/spt_billboard_dump.py                     # every billboard
    python tools/lod/spt_billboard_dump.py --trees maniatree01 dtree01
    python tools/lod/spt_billboard_dump.py --plugin Nehrim.esm --out temp/bb
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

try:
    from PIL import Image
except ImportError:                              # pragma: no cover
    raise SystemExit('Pillow required: pip install Pillow')


def convert(src: Path, dst: Path, bg=(110, 110, 115)) -> tuple:
    im = Image.open(src).convert('RGBA')
    plate = Image.new('RGBA', im.size, bg + (255,))
    Image.alpha_composite(plate, im).convert('RGB').save(dst)
    return im.size


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--plugin', default='Oblivion.esm')
    ap.add_argument('--trees', nargs='*', default=None,
                    help='Billboard stems (default: all that ship)')
    ap.add_argument('--out', type=Path, default=Path('temp/billboards'))
    a = ap.parse_args()

    root = Path('export') / a.plugin / 'textures' / 'trees' / 'billboards'
    if not root.is_dir():
        print(f'no billboards dir: {root}')
        return 1
    a.out.mkdir(parents=True, exist_ok=True)

    if a.trees:
        stems = [s.lower().replace('.dds', '') for s in a.trees]
    else:
        stems = sorted(p.stem for p in root.glob('*.dds'))

    n = 0
    for stem in stems:
        src = root / f'{stem}.dds'
        if not src.is_file():
            print(f'  {stem:28s} (no billboard shipped)')
            continue
        dst = a.out / f'{stem}_billboard.png'
        try:
            size = convert(src, dst)
        except Exception as e:                   # noqa: BLE001
            print(f'  {stem:28s} FAILED: {e}')
            continue
        print(f'  {stem:28s} {size[0]}x{size[1]} -> {dst}')
        n += 1
    print(f'\n{n} billboards written to {a.out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
