"""Verify the NATIVE corridor width-grow against the pure-Python original.

The native march (native/src/grow.cpp) replaced the per-station Python loop for
speed, not for behaviour: every stop rule must still fire at the same distance.
This drives both implementations over the same synthetic geometry and reports
the worst disagreement.

Synthetic rather than a real cell on purpose -- it needs no 2GB export index,
so it runs in seconds and can cover shapes chosen to exercise each stop rule
(a wall at a known offset, a floor that drops away, a parallel neighbour).

    python tools/navmesh/grow_verify.py
    python tools/navmesh/grow_verify.py --cases 400 --tol 1e-9
"""

import argparse
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tes5_import.navmesh import corridor_grow as cg, params  # noqa: E402


def make_world(rng):
    """A blocking soup with real walls, plus a walkable floor with a hole.

    The walls are axis-aligned quads at known offsets so the bisect has
    something exact to converge on; the floor covers most of the area but stops
    short on one side, which is what exercises the floor-edge rule.
    """
    blocking = []
    # Four walls of a 1200u room, 300u tall, as two triangles each.
    for (x0, y0, x1, y1) in ((0, 0, 1200, 0), (0, 1200, 1200, 1200),
                             (0, 0, 0, 1200), (1200, 0, 1200, 1200)):
        blocking.append([[x0, y0, 0], [x1, y1, 0], [x1, y1, 300]])
        blocking.append([[x0, y0, 0], [x1, y1, 300], [x0, y0, 300]])
    # An interior pillar, to give the march something off-axis to stop on.
    for k in range(4):
        a = 2 * math.pi * k / 4
        b = 2 * math.pi * (k + 1) / 4
        p = (600 + 40 * math.cos(a), 700 + 40 * math.sin(a))
        q = (600 + 40 * math.cos(b), 700 + 40 * math.sin(b))
        blocking.append([[p[0], p[1], 0], [q[0], q[1], 0], [q[0], q[1], 200]])
        blocking.append([[p[0], p[1], 0], [q[0], q[1], 200], [p[0], p[1], 200]])

    # Walkable floor over x in [0, 900] only -- beyond that the floor is gone,
    # so a rail marching east must stop at the floor edge rather than a wall.
    walkable = []
    step = 100.0
    x = 0.0
    while x < 900.0:
        y = 0.0
        while y < 1200.0:
            walkable.append([[x, y, 0], [x + step, y, 0], [x + step, y + step, 0]])
            walkable.append([[x, y, 0], [x + step, y + step, 0], [x, y + step, 0]])
            y += step
        x += step
    return (np.asarray(blocking, dtype=float),
            np.asarray(walkable, dtype=float))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cases', type=int, default=300)
    ap.add_argument('--tol', type=float, default=1e-6)
    args = ap.parse_args()

    rng = np.random.default_rng(1234)
    blocking, walkable = make_world(rng)

    # A pathgrid through the room, so the neighbour cap has parallel edges.
    nodes = [(200.0, 200.0, 0.0), (1000.0, 200.0, 0.0),
             (200.0, 400.0, 0.0), (1000.0, 400.0, 0.0),
             (200.0, 800.0, 0.0), (1000.0, 800.0, 0.0)]
    edges = [(0, 1), (2, 3), (4, 5), (0, 2), (2, 4)]
    node_z = [0.0] * len(nodes)

    wall_hit = cg.wall_slab_sampler(blocking)
    walk_probe = cg.walkable_sampler(walkable)
    field = cg.NeighbourField(nodes, edges, node_z)

    # Random stations across the room, on both perpendiculars of a random edge.
    rows = []
    meta = []
    for _ in range(args.cases):
        ei = int(rng.integers(0, len(edges)))
        i, j = edges[ei]
        t = float(rng.random())
        cx = nodes[i][0] + (nodes[j][0] - nodes[i][0]) * t
        cy = nodes[i][1] + (nodes[j][1] - nodes[i][1]) * t
        dx = nodes[j][0] - nodes[i][0]
        dy = nodes[j][1] - nodes[i][1]
        ln = math.hypot(dx, dy)
        ux, uy = dx / ln, dy / ln
        wx, wy = -uy, ux
        if rng.random() < 0.5:
            wx, wy = -wx, -wy
        lo = float(rng.choice([0.0, params.RIBBON_GROW_MIN_HALF,
                               params.RIBBON_HALF_WIDTH]))
        rows.append((cx, cy, 0.0, wx, wy, ux, uy, lo, ei))
        meta.append((cx, cy, wx, wy, ux, uy, lo, (i, j)))

    st = np.asarray(rows, dtype=np.float64)
    got = cg.grow_batch(blocking, walkable, nodes, edges, node_z, st)

    worst = 0.0
    worst_at = None
    bad = 0
    for n, (cx, cy, wx, wy, ux, uy, lo, ij) in enumerate(meta):
        ref = cg.grow_half_width(cx, cy, 0.0, wx, wy, ux, uy, ij,
                                 wall_hit, walk_probe, field, lo)
        d = abs(ref - float(got[n]))
        if d > worst:
            worst, worst_at = d, (n, ref, float(got[n]), cx, cy, lo)
        if d > args.tol:
            bad += 1

    print('cases            : %d' % args.cases)
    print('max |native-py|  : %.3e' % worst)
    print('over tolerance   : %d (tol %.1e)' % (bad, args.tol))
    if worst_at:
        n, ref, gotv, cx, cy, lo = worst_at
        print('worst case #%d at (%.1f, %.1f) lo=%.1f: py=%.6f native=%.6f'
              % (n, cx, cy, lo, ref, gotv))
    print('RESULT: %s' % ('PASS' if bad == 0 else 'FAIL'))
    return 0 if bad == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
