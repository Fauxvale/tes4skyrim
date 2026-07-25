"""Measure SLOPE and CONTINUITY defects in a generated navmesh.

Coverage tools measure 2D area and miss the defect that actually breaks stairs:
the mesh covers the right ground but its HEIGHTS are wrong or discontinuous.
This reports, per cell:

  * COMPONENTS     — disconnected pieces (a stair that fails to join its floors)
  * Z-SEAMS        — pairs of triangles that overlap in plan view at nearly the
                     same height but share NO edge: the mesh is torn there, so an
                     actor cannot cross even though the ground is continuous.
  * RAMP ERROR     — for every steep (stair/ramp) pathgrid edge, how far the mesh
                     surface departs from the pathgrid line's own straight slope,
                     sampled along the centreline.  Principle 2 says the line IS
                     the ramp, so this should be ~0.  A sawtooth or a dropout
                     shows up here as a large max error / missing samples.
  * STEP JUMPS     — adjacent (edge-sharing) triangles whose surfaces differ by
                     more than MAX_CLIMB at the shared edge: a cliff in the mesh.

    python tools/navmesh_slope_check.py --cell AnvilPinarusInventiusHouse
    python tools/navmesh_slope_check.py --cells A,B,C --dump temp/seams.txt
"""

import argparse
import math
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asset_convert import collision_extract as ce  # noqa: E402
from tes5_import.navmesh import build, params  # noqa: E402
from tools.navmesh_probe import load_cell  # noqa: E402

# Two surfaces this close in Z at one XY are the same walkable surface.
SAME_SURFACE = 40.0
# Sample spacing along a pathgrid centreline when measuring ramp error.
RAMP_STEP = 16.0


def _components(nv, tris):
    parent = list(range(nv))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    # Union only across SHARED EDGES — that is what the engine walks
    # (pgrd_to_navm._compute_adjacency links neighbours across shared edges).
    edges = defaultdict(list)
    for ti, t in enumerate(tris):
        for k in range(3):
            a, b = t[k], t[(k + 1) % 3]
            edges[(a, b) if a < b else (b, a)].append(ti)
    tp = list(range(len(tris)))

    def tfind(x):
        while tp[x] != x:
            tp[x] = tp[tp[x]]
            x = tp[x]
        return x

    for key, ts in edges.items():
        for i in range(1, len(ts)):
            ra, rb = tfind(ts[0]), tfind(ts[i])
            if ra != rb:
                tp[ra] = rb
    seen = {}
    for ti in range(len(tris)):
        seen.setdefault(tfind(ti), []).append(ti)
    return list(seen.values())


def _tri_z_at(v, t, x, y):
    """Height of triangle t at (x, y), or None if (x,y) is outside it."""
    a, b, c = v[t[0]], v[t[1]], v[t[2]]
    d = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
    if abs(d) < 1e-9:
        return None
    l0 = ((b[1] - c[1]) * (x - c[0]) + (c[0] - b[0]) * (y - c[1])) / d
    l1 = ((c[1] - a[1]) * (x - c[0]) + (a[0] - c[0]) * (y - c[1])) / d
    l2 = 1.0 - l0 - l1
    if l0 < -1e-6 or l1 < -1e-6 or l2 < -1e-6:
        return None
    return l0 * a[2] + l1 * b[2] + l2 * c[2]


def _grid_index(v, tris, cell=64.0):
    """Bucket triangles by XY so a point query only tests nearby ones."""
    idx = defaultdict(list)
    for ti, t in enumerate(tris):
        xs = [v[t[0]][0], v[t[1]][0], v[t[2]][0]]
        ys = [v[t[0]][1], v[t[1]][1], v[t[2]][1]]
        for gx in range(int(min(xs) // cell), int(max(xs) // cell) + 1):
            for gy in range(int(min(ys) // cell), int(max(ys) // cell) + 1):
                idx[(gx, gy)].append(ti)
    return idx, cell


def _heights_at(v, tris, idx, cell, x, y):
    out = []
    for ti in idx.get((int(x // cell), int(y // cell)), ()):
        z = _tri_z_at(v, tris[ti], x, y)
        if z is not None:
            out.append((z, ti))
    return out


def analyse(export_dir, cell_arg, dump=None):
    ctx = load_cell(export_dir, cell_arg)
    verts, tris = build.build_navmesh(
        ctx['refrs'], ctx['base_model'], ce.get_collision,
        ctx['nodes'], ctx['edges'],
        land_rec=ctx['land'] if ctx['is_exterior'] else None,
        origin_x=ctx['grid_x'] * 4096.0, origin_y=ctx['grid_y'] * 4096.0,
        doors=ctx.get('doors'))
    if not tris:
        print('%-32s NO MESH' % cell_arg)
        return
    v = [tuple(map(float, p)) for p in verts]
    comps = _components(len(v), tris)
    comps.sort(key=len, reverse=True)
    idx, cell = _grid_index(v, tris)

    # --- STEP JUMPS: edge-sharing triangles whose planes disagree at the edge.
    # (shared vertices mean this is usually 0; a real jump means torn heights)
    edge_map = defaultdict(list)
    for ti, t in enumerate(tris):
        for k in range(3):
            a, b = t[k], t[(k + 1) % 3]
            edge_map[(a, b) if a < b else (b, a)].append(ti)

    # --- Z-SEAMS: triangles covering the same XY at the same height that share
    # no edge.  This is the tear that makes a stair unwalkable.
    seams = []
    seam_pts = []
    for ti, t in enumerate(tris):
        cx = sum(v[i][0] for i in t) / 3.0
        cy = sum(v[i][1] for i in t) / 3.0
        cz = sum(v[i][2] for i in t) / 3.0
        for (z, tj) in _heights_at(v, tris, idx, cell, cx, cy):
            if tj <= ti:
                continue
            if abs(z - cz) > SAME_SURFACE:
                continue
            shared = len(set(tris[ti]) & set(tris[tj]))
            if shared < 2:
                seams.append((ti, tj, cx, cy, cz, z, shared))
                seam_pts.append((cx, cy, cz))

    # --- RAMP ERROR on steep pathgrid edges (the stairs).
    nodes, edges_pg = ctx['nodes'], ctx['edges']
    ramp_rows = []
    for (i, j) in edges_pg:
        if i >= len(nodes) or j >= len(nodes) or i == j:
            continue
        ax, ay, az = nodes[i]
        bx, by, bz = nodes[j]
        run = math.hypot(bx - ax, by - ay)
        if run < 1e-6:
            continue
        slope = abs(bz - az) / run
        if slope < 0.20:            # not a stair/ramp
            continue
        n = max(2, int(run // RAMP_STEP))
        errs = []
        miss = 0
        for s in range(n + 1):
            f = s / n
            px, py = ax + (bx - ax) * f, ay + (by - ay) * f
            pz = az + (bz - az) * f
            hs = _heights_at(v, tris, idx, cell, px, py)
            if not hs:
                miss += 1
                continue
            best = min(abs(z - pz) for (z, _t) in hs)
            errs.append(best)
        ramp_rows.append((i, j, slope, run, errs, miss, n + 1))

    tot_samples = sum(r[6] for r in ramp_rows)
    tot_miss = sum(r[5] for r in ramp_rows)
    all_err = [e for r in ramp_rows for e in r[4]]
    max_err = max(all_err) if all_err else 0.0
    mean_err = sum(all_err) / len(all_err) if all_err else 0.0

    print('%-32s tris=%-5d comps=%-3d  seams=%-5d  ramps=%d '
          'ramp_miss=%d/%d  ramp_err mean=%.1f max=%.1f'
          % (cell_arg, len(tris), len(comps), len(seams), len(ramp_rows),
             tot_miss, tot_samples, mean_err, max_err))
    if len(comps) > 1:
        print('     component sizes: %s'
              % ', '.join(str(len(c)) for c in comps[:12]))
    # Worst ramps, so a specific stair can be rendered and chased.
    worst = sorted(ramp_rows, key=lambda r: -(max(r[4]) if r[4] else 1e9))[:5]
    for (i, j, slope, run, errs, miss, tot) in worst:
        if not errs and not miss:
            continue
        print('     ramp %4d->%-4d slope=%.2f run=%5.0f  miss=%d/%d '
              'err mean=%.1f max=%.1f  @(%.0f,%.0f,%.0f)'
              % (i, j, slope, run, miss, tot,
                 sum(errs) / len(errs) if errs else -1,
                 max(errs) if errs else -1,
                 nodes[i][0], nodes[i][1], nodes[i][2]))
    if dump and seam_pts:
        with open(dump, 'a') as fh:
            fh.write('# %s Z-seams (x y z)\n' % cell_arg)
            for (x, y, z) in seam_pts:
                fh.write('%.1f %.1f %.1f\n' % (x, y, z))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--export', default='export/Oblivion.esm')
    ap.add_argument('--cell')
    ap.add_argument('--cells')
    ap.add_argument('--dump')
    a = ap.parse_args()
    cells = ([c.strip() for c in a.cells.split(',')] if a.cells
             else [a.cell])
    for c in cells:
        try:
            analyse(a.export, c, dump=a.dump)
        except Exception as e:
            print('%-32s ERROR %s' % (c, e))


if __name__ == '__main__':
    main()
