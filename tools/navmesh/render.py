"""Render a cell's generated navmesh to a PNG for eyeball inspection.

Numbers tell you a cell has 3 uncovered samples; only a picture tells you they
form a slit across a staircase.  This is the tool for "there is a hole in the
top stairs" style reports.

Triangle fill = the shape contract: red badness>2, orange >1 (violates the
contract), yellow area<MIN_TRI_AREA, green healthy.  Blue = pathgrid (the
authored ground truth); magenta squares = doors.  With --cracks, boundary
edges that a walked pathgrid line crosses are drawn in bright red — those are
adjacency breaks the engine cannot path across even when the plan coverage
looks continuous (the "invisible hole in the stairs" signature).

    # whole cell
    python tools/navmesh/render.py ImperialDungeon01

    # ONE storey only, so a stacked building is actually readable
    python tools/navmesh/render.py AnvilPinarusInventiusHouse --z 300 700

    # zoom on the area around a placed reference the user complained about
    python tools/navmesh/render.py --ref 1A01FC1E --pad 400 --cracks

    # simplified: no pathgrid/door overlay, just the mesh
    python tools/navmesh/render.py ImperialDungeon01 --bare
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PIL import Image, ImageDraw  # noqa: E402

from tes5_import.navmesh import corridor_clean, params  # noqa: E402
from tools.navmesh.index import NavIndex, DEFAULT_EXPORT  # noqa: E402
from tools.navmesh.metrics import (  # noqa: E402
    crossed_boundary_edges, open_notches,
)


def render(verts, tris, nodes, edges, doors, out, bbox=None, width=1400,
           bare=False, cracks=None, title=None, collision=None, ids=False,
           notches=None):
    xs = [p[0] for p in verts]
    ys = [p[1] for p in verts]
    if bbox:
        minx, miny, maxx, maxy = bbox
    else:
        # Frame on the MESH, never on raw collision: one outlier REFR
        # (FelgageldtCave's is 70k units away) otherwise zooms the whole cell
        # down to a blob in the corner.  Collision is still drawn wherever it
        # falls; it just does not get to pick the framing.
        minx, maxx = min(xs) - 50, max(xs) + 50
        miny, maxy = min(ys) - 50, max(ys) + 50
    span = max(maxx - minx, 1.0)
    scale = width / span
    height = max(1, int((maxy - miny) * scale))

    def P(x, y):
        return ((x - minx) * scale, height - (y - miny) * scale)

    img = Image.new('RGB', (width, height), (16, 16, 16))
    dr = ImageDraw.Draw(img, 'RGBA')
    if collision:
        walk, block = collision
        for t in walk:
            dr.polygon([P(t[0][0], t[0][1]), P(t[1][0], t[1][1]),
                        P(t[2][0], t[2][1])], fill=(55, 70, 60, 70))
        for t in block:      # THE WALLS — without these, debugging is blind
            dr.polygon([P(t[0][0], t[0][1]), P(t[1][0], t[1][1]),
                        P(t[2][0], t[2][1])], fill=(190, 50, 45, 90))
    for ti, tri in enumerate(tris):
        p, q, r = (verts[k] for k in tri)
        bad = corridor_clean._badness(verts, tri)
        area = abs((q[0] - p[0]) * (r[1] - p[1])
                   - (q[1] - p[1]) * (r[0] - p[0])) * 0.5
        if bad > 2.0:
            fill = (220, 40, 40, 150)
        elif bad > 1.0:
            fill = (220, 130, 40, 150)
        elif area < params.MIN_TRI_AREA:
            fill = (210, 210, 40, 150)
        else:
            fill = (50, 160, 100, 130)
        pts = [P(p[0], p[1]), P(q[0], q[1]), P(r[0], r[1])]
        dr.polygon(pts, fill=fill, outline=(230, 230, 230, 255))
        if ids:
            cx = sum(pt[0] for pt in pts) / 3.0
            cy = sum(pt[1] for pt in pts) / 3.0
            dr.text((cx - 6, cy - 5), str(ti), fill=(255, 255, 255, 220))
    if cracks:
        for (a, b) in cracks:
            dr.line([P(verts[a][0], verts[a][1]), P(verts[b][0], verts[b][1])],
                    fill=(255, 30, 30, 255), width=5)
    if notches:
        # Ring the mouth of every V-bite: these are invisible to the crack and
        # coverage metrics but obvious (and unwalkable) on screen.
        for (apex, p, q, _d, _m) in notches:
            dr.line([P(verts[p][0], verts[p][1]),
                     P(verts[apex][0], verts[apex][1]),
                     P(verts[q][0], verts[q][1])],
                    fill=(255, 0, 255, 255), width=4)
            x, y = P(verts[apex][0], verts[apex][1])
            dr.ellipse([x - 7, y - 7, x + 7, y + 7],
                       outline=(255, 0, 255, 255), width=3)
    if not bare:
        for (a, b) in edges:
            pa, pb = nodes[a], nodes[b]
            dr.line([P(pa[0], pa[1]), P(pb[0], pb[1])],
                    fill=(40, 130, 255, 220), width=2)
        for n in nodes:
            x, y = P(n[0], n[1])
            dr.ellipse([x - 3, y - 3, x + 3, y + 3], fill=(60, 200, 255, 255))
        for (x, y, _z, _r, _f, _tp, _w) in doors:
            px, py = P(x, y)
            dr.rectangle([px - 4, py - 4, px + 4, py + 4],
                         fill=(240, 60, 240, 255))
    img.save(out)
    print('wrote %s (%d tris%s%s)%s'
          % (out, len(tris), ', %d crack edges' % len(cracks) if cracks else '',
             ', %d NOTCHES' % len(notches) if notches else '',
             ' [%s]' % title if title else ''))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cell', nargs='?', help='cell EditorID or FormID')
    ap.add_argument('--ref', help='centre on this placed reference FormID '
                                  '(finds its cell automatically)')
    ap.add_argument('--pad', type=float, default=512.0,
                    help='half-extent around --ref (default 512)')
    ap.add_argument('-o', '--out', help='output PNG (default temp/<cell>.png)')
    ap.add_argument('--z', nargs=2, type=float, metavar=('ZMIN', 'ZMAX'),
                    help='keep only triangles whose centroid z is in range '
                         '(isolate ONE storey)')
    ap.add_argument('--bbox', nargs=4, type=float,
                    metavar=('MINX', 'MINY', 'MAXX', 'MAXY'))
    ap.add_argument('--bare', action='store_true',
                    help='mesh only: no pathgrid, node or door overlay')
    ap.add_argument('--cracks', action='store_true',
                    help='highlight boundary edges a walked line crosses')
    ap.add_argument('--notches', action='store_true',
                    help='ring every open V-notch bitten into the surface')
    ap.add_argument('--collision', action='store_true',
                    help='draw the real collision underneath: dim green '
                         'walkable, red BLOCKING (the walls)')
    ap.add_argument('--ids', action='store_true',
                    help='label each triangle with its index')
    ap.add_argument('--width', type=int, default=1400)
    ap.add_argument('--export', default=DEFAULT_EXPORT)
    a = ap.parse_args()

    idx = NavIndex(a.export)
    bbox = tuple(a.bbox) if a.bbox else None
    if a.ref:
        cell, refr = idx.cell_of_ref(a.ref)
        if cell is None:
            print('reference %s not found in any cell' % a.ref)
            return 1
        from tes5_import.text_reader import get_float
        rx = get_float(refr, 'PosX', 0.0)
        ry = get_float(refr, 'PosY', 0.0)
        print('ref %s is in cell %s (%s) at (%.0f, %.0f)'
              % (a.ref, cell.name, cell.fid, rx, ry))
        if bbox is None:
            bbox = (rx - a.pad, ry - a.pad, rx + a.pad, ry + a.pad)
    else:
        if not a.cell:
            ap.error('give a cell name or --ref')
        cell = idx.cell(a.cell)
        if cell is None:
            print('cell %s not found' % a.cell)
            return 1
    if not cell.has_pathgrid:
        print('%s: no pathgrid' % cell.name)
        return 1

    verts, tris = cell.build()
    if a.z:
        tris = [tri for tri in tris
                if a.z[0] <= sum(verts[k][2] for k in tri) / 3.0 <= a.z[1]]
    if not tris:
        print('%s: no triangles in view' % cell.name)
        return 1
    cracks = None
    if a.cracks:
        cracks = crossed_boundary_edges(verts, tris, cell)
    notches = open_notches(verts, tris, cell) if a.notches else None
    coll = cell.collision() if a.collision else None
    out = a.out or ('temp/%s.png' % (cell.name or 'cell'))
    d = os.path.dirname(out)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    render(verts, tris, cell.nodes, cell.edges, cell.doors, out,
           bbox=bbox, width=a.width, bare=a.bare, cracks=cracks,
           title=cell.name, collision=coll, ids=a.ids, notches=notches)
    return 0


if __name__ == '__main__':
    sys.exit(main())
