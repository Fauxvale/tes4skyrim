"""A/B the Phase-2 corridor width-grow against the Phase-1 fixed width.

Builds one cell's corridor mesh twice — RIBBON_GROW off (fixed
RIBBON_HALF_WIDTH) then on (marched out to walls / neighbour centerlines) — and
reports triangles, covered XY area, and connectivity for each, so a width change
can be judged without a full import.  Also flags regressions the grow must not
cause: fewer connected components than the pathgrid, or lost area.

Usage:
    python tools/navmesh_width_ab.py --cell AnvilPinarusInventiusHouse
    python tools/navmesh_width_ab.py --cell grid:-48:-8
    python tools/navmesh_width_ab.py --cells AnvilFightersGuild,grid:-47:-8
"""

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tes5_import.navmesh import build, corridor_clean, params      # noqa: E402
from tools.navmesh_probe import load_cell                          # noqa: E402


def _area(verts, tris):
    if not tris:
        return 0.0
    v = np.asarray(verts, dtype=float)
    a = 0.0
    for (i, j, k) in tris:
        va, vb, vc = v[i], v[j], v[k]
        a += abs((vb[0] - va[0]) * (vc[1] - va[1]) -
                 (vb[1] - va[1]) * (vc[0] - va[0])) * 0.5
    return a


def _build(ctx):
    ox = ctx['grid_x'] * 4096.0
    oy = ctx['grid_y'] * 4096.0
    return build.build_navmesh(
        ctx['refrs'], ctx['base_model'],
        __import__('asset_convert.collision_extract',
                   fromlist=['get_collision']).get_collision,
        ctx['nodes'], ctx['edges'],
        land_rec=ctx['land'] if ctx['is_exterior'] else None,
        origin_x=ox, origin_y=oy, doors=ctx['doors'])


def _report(tag, verts, tris):
    comps = len(corridor_clean.components(
        [tuple(int(i) for i in t) for t in tris])) if tris else 0
    print(f"  {tag:6s}: {len(verts):5d} verts  {len(tris):5d} tris  "
          f"area={_area(verts, tris):11.0f}  components={comps}")
    return _area(verts, tris), len(tris)


def measure(export_dir, cell):
    ctx = load_cell(export_dir, cell)
    print(f"\n{cell}  (nodes={len(ctx['nodes'])} edges={len(ctx['edges'])} "
          f"{'exterior' if ctx['is_exterior'] else 'interior'})")

    old = params.RIBBON_GROW
    try:
        params.RIBBON_GROW = False
        v0, t0 = _build(ctx)
        a0, n0 = _report('FIXED', v0, t0)
        params.RIBBON_GROW = True
        v1, t1 = _build(ctx)
        a1, n1 = _report('GROW', v1, t1)
    finally:
        params.RIBBON_GROW = old

    if a0 > 0:
        print(f"  area x{a1 / a0:.2f}   tris x{(n1 / n0) if n0 else 0:.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--export', default='export/Oblivion.esm')
    ap.add_argument('--cell')
    ap.add_argument('--cells', help='comma-separated list')
    args = ap.parse_args()

    cells = []
    if args.cell:
        cells.append(args.cell)
    if args.cells:
        cells += [c for c in args.cells.split(',') if c]
    if not cells:
        ap.error('need --cell or --cells')
    for c in cells:
        measure(args.export, c)


if __name__ == '__main__':
    main()
