"""Mesh topology queries used by tools, acceptance and the ledge pass.

Edge adjacency and connected components over SHARED EDGES -- what the engine
actually walks -- plus the drop-down (ledge) link search.  Nothing here mutates
a mesh; the build half of `corridor_clean` imports from this module, never the
other way round.
"""

import math

from . import params


def edge_adjacency(tris):
    """(N,3) neighbour-triangle indices over shared edges, -1 for boundary.

    MUST match pgrd_to_navm._compute_adjacency: an edge shared by exactly two
    triangles links them; three or more links none.  This is the connectivity
    the ENGINE sees, distinct from vertex-union-find.
    """
    owners = {}
    for ti, t in enumerate(tris):
        for k in range(3):
            a, b = int(t[k]), int(t[(k + 1) % 3])
            key = (a, b) if a < b else (b, a)
            owners.setdefault(key, []).append((ti, k))
    adj = [[-1, -1, -1] for _ in range(len(tris))]
    for ent in owners.values():
        if len(ent) == 2:
            (ti, si), (tj, sj) = ent
            adj[ti][si] = tj
            adj[tj][sj] = ti
    return adj

def _boundary_edges(tris, comp):
    """Open edges (no neighbour) of one component, as (a, b) vertex indices."""
    owners = {}
    for ti in comp:
        t = tris[ti]
        for k in range(3):
            a, b = int(t[k]), int(t[(k + 1) % 3])
            key = (a, b) if a < b else (b, a)
            owners[key] = owners.get(key, 0) + 1
    return [e for e, n in owners.items() if n == 1]

def _centroid(verts, tri):
    """The triangle's 3D centroid."""
    a, b, c = verts[tri[0]], verts[tri[1]], verts[tri[2]]
    return ((a[0] + b[0] + c[0]) / 3.0,
            (a[1] + b[1] + c[1]) / 3.0,
            (a[2] + b[2] + c[2]) / 3.0)

def _resolve_ledges(verts, tris, marks, tol=1.0):
    """Map centroid-marked ledge pairs back to FINAL triangle indices.

    A pair is dropped when either of its triangles did not survive the cull —
    a link to a triangle that no longer exists is worse than no link.
    """
    if not marks or len(tris) == 0:
        return []
    cents = [_centroid(verts, t) for t in tris]
    tol2 = tol * tol

    def _find(mark):
        """Index of the triangle whose centroid matches, or None."""
        best = None
        for i, c in enumerate(cents):
            d = ((c[0] - mark[0]) ** 2 + (c[1] - mark[1]) ** 2
                 + (c[2] - mark[2]) ** 2)
            if d <= tol2 and (best is None or d < best[0]):
                best = (d, i)
        return best[1] if best else None

    out = []
    for (hi_mark, lo_mark, drop) in marks:
        hi = _find(hi_mark)
        lo = _find(lo_mark)
        if hi is not None and lo is not None and hi != lo:
            out.append((hi, lo, drop))
    return out

def _edge_tri(tris, ti_set, a, b):
    """The triangle in this component owning boundary edge (a, b)."""
    for ti in ti_set:
        t = tris[ti]
        for k in range(3):
            if {t[k], t[(k + 1) % 3]} == {a, b}:
                return ti
    return None


def _ledge_candidates(verts, bounds_i, bounds_j):
    """Boundary-edge pairs close enough in plan and separated by a legal drop.

    Endpoint pairing is tried both ways round: the two boundaries wind in
    opposite directions, so the matching endpoints are as often swapped as not.
    """
    cands = []
    for (a1, b1) in bounds_i:
        p1, q1 = verts[a1], verts[b1]
        for (a2, b2) in bounds_j:
            p2, q2 = verts[a2], verts[b2]
            dxy = min(
                max(math.hypot(p1[0] - p2[0], p1[1] - p2[1]),
                    math.hypot(q1[0] - q2[0], q1[1] - q2[1])),
                max(math.hypot(p1[0] - q2[0], p1[1] - q2[1]),
                    math.hypot(q1[0] - p2[0], q1[1] - p2[1])))
            if dxy > params.ISLAND_BRIDGE_XY:
                continue
            z1 = 0.5 * (p1[2] + q1[2])
            z2 = 0.5 * (p2[2] + q2[2])
            drop = abs(z1 - z2)
            if not (params.ISLAND_BRIDGE_MIN_DROP <= drop
                    <= params.ISLAND_BRIDGE_MAX_DROP):
                continue
            cands.append((dxy, (a1, b1), (a2, b2), z1, z2, drop))
    cands.sort(key=lambda c: (c[0], c[1], c[2]))
    return cands


def _link_pair(tris, comps, bounds, verts, i, j, out):
    """Append the ledge links between components `i` and `j` to `out`.

    Nearest pairs first, ONE link per triangle, so links spread along the lip
    instead of stacking on one edge.

    See: docs/commentary/tes5_import_navmesh.md#ledge-links-spread-along-the-lip
    """
    cands = _ledge_candidates(verts, bounds[i], bounds[j])
    used, made = set(), 0
    for (_dxy, (a1, b1), (a2, b2), z1, z2, drop) in cands:
        if made >= params.LEDGE_LINKS_PER_PAIR:
            break
        t1 = _edge_tri(tris, comps[i], a1, b1)
        t2 = _edge_tri(tris, comps[j], a2, b2)
        if t1 is None or t2 is None or t1 in used or t2 in used:
            continue
        used.add(t1)
        used.add(t2)
        made += 1
        out.append((t1, t2, drop) if z1 >= z2 else (t2, t1, drop))


def find_ledge_links(verts, tris):
    """Find DROP-DOWNs between components: [(tri_hi, tri_lo, drop), ...].

    Oblivion expressed a drop-down as two disconnected pathgrid islands, so the
    navmesh mirrors them as separate components with no route between.  Skyrim
    expresses it as an NVNM Edge Link (Ledge Down / Ledge Up), NOT geometry.

    This only DETECTS the pairs; `pgrd_to_navm` writes the links.  Both sides
    must ALREADY be separate components, which is what keeps stairs and ramps
    out of it.

    See: docs/commentary/tes5_import_navmesh.md#ledge-links-spread-along-the-lip
    """
    if len(tris) == 0:
        return []
    tris = [list(map(int, t)) for t in tris]
    comps = components(tris)
    if len(comps) < 2:
        return []
    bounds = [_boundary_edges(tris, c) for c in comps]
    out = []
    for i in range(len(comps)):
        for j in range(i + 1, len(comps)):
            _link_pair(tris, comps, bounds, verts, i, j, out)
    return out


def components(tris):
    """List of triangle-index lists connected over EDGE adjacency (engine view)."""
    adj = edge_adjacency(tris)
    n = len(tris)
    seen = [False] * n
    comps = []
    for s in range(n):
        if seen[s]:
            continue
        comp, stack = [], [s]
        seen[s] = True
        while stack:
            x = stack.pop()
            comp.append(x)
            for nb in adj[x]:
                if nb >= 0 and not seen[nb]:
                    seen[nb] = True
                    stack.append(nb)
        comps.append(comp)
    return comps
