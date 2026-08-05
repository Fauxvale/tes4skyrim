"""FITNESS TEST: can an NPC actually walk from the top floor out the front door?

Component counts are necessary but not sufficient.  A mesh can be one component
and still be untraversable: the route may only exist through a fold, a needle, or
a joint whose shared edge does not really carry the surface.  This walks the mesh
the way the ENGINE does and reports whether the trip is possible.

The traversal mirrors pgrd_to_navm._compute_adjacency: an actor moves between two
triangles only across a SHARED EDGE, and only when the step across that edge is
climbable (the two surfaces meet within MAX_CLIMB at the edge itself).  A fold —
a triangle bonded to its neighbour from above rather than edge-on — fails that
test exactly as it does in game.

    python tools/navmesh/walk_test.py --cell AnvilPinarusInventiusHouse
    python tools/navmesh/walk_test.py --cell X --from-highest --to-door
"""

import argparse
import heapq
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from asset_convert import collision_extract as ce  # noqa: E402
from tes5_import.navmesh import build, params  # noqa: E402
from tools.navmesh.probe import load_cell  # noqa: E402


def _tri_centre(v, t):
    return (sum(v[i][0] for i in t) / 3.0,
            sum(v[i][1] for i in t) / 3.0,
            sum(v[i][2] for i in t) / 3.0)


def _edge_z(v, a, b, mid):
    """Height of the segment a-b at the point closest to `mid` (its midpoint)."""
    return 0.5 * (v[a][2] + v[b][2])


def build_graph(v, tris, climb=None):
    """Adjacency over SHARED EDGES, rejecting steps that are not climbable.

    Returns (adj, rejected) where adj[ti] = [(tj, cost), ...].
    """
    if climb is None:
        climb = params.MAX_CLIMB
    owners = defaultdict(list)
    for ti, t in enumerate(tris):
        for k in range(3):
            a, b = t[k], t[(k + 1) % 3]
            owners[(a, b) if a < b else (b, a)].append(ti)

    adj = defaultdict(list)
    rejected = []
    for (a, b), ts in owners.items():
        if len(ts) != 2:
            continue
        t0, t1 = ts
        c0 = _tri_centre(v, tris[t0])
        c1 = _tri_centre(v, tris[t1])
        # The step is taken ACROSS the shared edge, so compare each triangle's
        # own height AT that edge — a fold shows up here as a large jump even
        # though the two triangles share the edge's vertices.
        ez = _edge_z(v, a, b, None)
        d0 = abs(c0[2] - ez)
        d1 = abs(c1[2] - ez)
        # A legitimate ramp triangle's centre sits within half its own rise of
        # the edge; a folded one is offset by the whole fold height.
        if max(d0, d1) > max(climb, 0.5 * abs(c0[2] - c1[2]) + climb):
            rejected.append(((a, b), t0, t1, round(max(d0, d1), 1)))
            continue
        cost = math.dist(c0, c1)
        adj[t0].append((t1, cost))
        adj[t1].append((t0, cost))
    return adj, rejected


def astar(adj, v, tris, start, goal):
    if start == goal:
        return [start]
    gc = _tri_centre(v, tris[goal])

    def h(ti):
        c = _tri_centre(v, tris[ti])
        return math.dist(c, gc)

    openq = [(h(start), 0.0, start, None)]
    came = {}
    best = {start: 0.0}
    while openq:
        _f, g, cur, prev = heapq.heappop(openq)
        if cur in came:
            continue
        came[cur] = prev
        if cur == goal:
            path = [cur]
            while came[path[-1]] is not None:
                path.append(came[path[-1]])
            return list(reversed(path))
        for (nb, w) in adj.get(cur, ()):
            ng = g + w
            if ng < best.get(nb, float('inf')):
                best[nb] = ng
                heapq.heappush(openq, (ng + h(nb), ng, nb, cur))
    return None


def nearest_tri(v, tris, pt, zw=1.0):
    best = None
    for ti, t in enumerate(tris):
        c = _tri_centre(v, tris[ti])
        d = ((c[0] - pt[0]) ** 2 + (c[1] - pt[1]) ** 2 +
             (zw * (c[2] - pt[2])) ** 2)
        if best is None or d < best[0]:
            best = (d, ti)
    return best[1] if best else None


def run(export_dir, cell_arg, climb=None):
    ctx = load_cell(export_dir, cell_arg)
    land = ctx['land'] if ctx['is_exterior'] else None
    v, tris = build.build_navmesh(
        ctx['refrs'], ctx['base_model'], ce.get_collision, ctx['nodes'],
        ctx['edges'], land_rec=land, origin_x=ctx['grid_x'] * 4096.0,
        origin_y=ctx['grid_y'] * 4096.0, doors=ctx.get('doors'))
    if not tris:
        print('%-34s NO MESH' % cell_arg)
        return False
    v = [tuple(map(float, p)) for p in v]

    adj, rejected = build_graph(v, tris, climb=climb)

    # START: the highest triangle (top floor).  GOAL: each teleport door.
    highest = max(range(len(tris)), key=lambda ti: _tri_centre(v, tris[ti])[2])
    hz = _tri_centre(v, tris[highest])[2]
    doors = [d for d in (ctx.get('doors') or ()) if len(d) < 5 or d[4]]
    if not doors:
        doors = list(ctx.get('doors') or ())

    print('%-34s %d tris, highest z=%.0f, %d doors, %d unwalkable joints'
          % (cell_arg, len(tris), hz, len(doors), len(rejected)))
    for (e, t0, t1, off) in rejected[:6]:
        print('     REJECTED joint tris %d/%d across edge %s (offset %.1fu)'
              % (t0, t1, e, off))

    ok = True
    for (dx, dy, dz) in [(d[0], d[1], d[2]) for d in doors]:
        goal = nearest_tri(v, tris, (dx, dy, dz))
        if goal is None:
            continue
        path = astar(adj, v, tris, highest, goal)
        gz = _tri_centre(v, tris[goal])[2]
        if path:
            print('     REACHABLE  top(z=%.0f) -> door(%.0f,%.0f,z=%.0f)  '
                  '%d triangles' % (hz, dx, dy, gz, len(path)))
        else:
            ok = False
            print('     BLOCKED    top(z=%.0f) -> door(%.0f,%.0f,z=%.0f)  '
                  'NO PATH' % (hz, dx, dy, gz))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--export', default='export/Oblivion.esm')
    ap.add_argument('--cell')
    ap.add_argument('--cells')
    ap.add_argument('--climb', type=float, default=None)
    a = ap.parse_args()
    cells = ([c.strip() for c in a.cells.split(',')] if a.cells else [a.cell])
    bad = 0
    for c in cells:
        try:
            if not run(a.export, c, climb=a.climb):
                bad += 1
        except Exception as e:
            bad += 1
            print('%-34s ERROR %s' % (c, e))
        sys.stdout.flush()
    if len(cells) > 1:
        print('\n%d cells, %d with a BLOCKED route' % (len(cells), bad))


if __name__ == '__main__':
    main()
