"""Cleanup + validation for the corridor navmesh.

Two jobs, both mandatory for a mesh the engine can actually walk:

  * DROP DEGENERATE triangles (no XY footprint) — a vertical sliver covers no
    ground, so no actor stands on it, and a coplanar antiparallel pair reads as
    CK OPPOSITE_NORMALS.
  * MAKE MANIFOLD — NVNM adjacency (pgrd_to_navm._compute_adjacency) links an
    edge only when EXACTLY two triangles share it; an edge shared by three or
    more links NONE of them, silently disconnecting everything around it.  The
    ribbon body is manifold by construction, but the door-quad connection can
    lay a triangle over one already there, so this is the backstop.

The corridor model needs NOTHING else: no welding (ribbon vertices are shared
by construction), no stitching, no island cull (there are no stray scraps).
"""

import math

import numpy as np

from . import params

def _compact(verts, tris):
    """Drop vertices no triangle references; reindex.  Lists in, arrays out."""
    if not tris:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int32)
    used = sorted({int(i) for t in tris for i in t})
    remap = {old: new for new, old in enumerate(used)}
    nv = [list(verts[i]) for i in used]
    nt = [(remap[int(a)], remap[int(b)], remap[int(c)]) for (a, b, c) in tris]
    return (np.asarray(nv, dtype=float),
            np.asarray(nt, dtype=np.int32))


def _drop_degenerate(verts, tris):
    """Remove triangles whose XY footprint is below MIN_XY_FOOTPRINT."""
    kept = []
    for (a, b, c) in tris:
        va, vb, vc = verts[a], verts[b], verts[c]
        cross = ((vb[0] - va[0]) * (vc[1] - va[1]) -
                 (vb[1] - va[1]) * (vc[0] - va[0]))
        if abs(cross) * 0.5 >= params.MIN_XY_FOOTPRINT:
            kept.append((a, b, c))
    return kept


def _make_manifold(verts, tris):
    """Drop the smallest triangle on any edge shared by 3+ triangles until every
    edge has at most two.  Keeps the larger (more load-bearing) triangle.
    """
    tris = [tuple(map(int, t)) for t in tris]

    def area(t):
        va, vb, vc = verts[t[0]], verts[t[1]], verts[t[2]]
        return abs((vb[0] - va[0]) * (vc[1] - va[1]) -
                   (vb[1] - va[1]) * (vc[0] - va[0])) * 0.5

    for _ in range(6):
        owners = {}
        for ti, t in enumerate(tris):
            for k in range(3):
                a, b = t[k], t[(k + 1) % 3]
                key = (a, b) if a < b else (b, a)
                owners.setdefault(key, []).append(ti)

        # How many DIFFERENT triangles each triangle touches: a triangle that is
        # the only link between two parts of the mesh has high connectivity value
        # and must survive even if it is small.  Dropping purely by area cut the
        # node-disc bridges — the smallest triangles at every junction — and split
        # LeyawiinCastleCountyHall's mesh into 8170 + 4275 after it had been built
        # as a single connected sheet.
        nbr = [0] * len(tris)
        for ent in owners.values():
            if len(ent) == 2:
                nbr[ent[0]] += 1
                nbr[ent[1]] += 1

        drop = set()
        for ent in owners.values():
            if len(ent) <= 2:
                continue
            live = [ti for ti in ent if ti not in drop]
            if len(live) <= 2:
                continue
            # Keep the best-connected triangles first, breaking ties by area.
            live.sort(key=lambda ti: (nbr[ti], area(tris[ti])), reverse=True)
            drop.update(live[2:])
        if not drop:
            break
        # A triangle may only be dropped if the mesh stays as connected without
        # it.  Dropping purely to satisfy the manifold rule repeatedly split
        # meshes that had been built as one sheet (5 components in the Guild),
        # which is far worse than one over-shared edge: the engine ignores the
        # extra edge, but an island is unreachable.
        kept = [t for i, t in enumerate(tris) if i not in drop]
        if len(components(kept)) <= len(components(tris)):
            tris = kept
        else:
            # Re-try dropping only those that do not break connectivity.
            safe = set()
            base = len(components(tris))
            for ti in sorted(drop):
                trial = [t for i, t in enumerate(tris)
                         if i != ti and i not in safe]
                if len(components(trial)) <= base:
                    safe.add(ti)
            if not safe:
                break
            tris = [t for i, t in enumerate(tris) if i not in safe]
    return tris


def finalize(verts, tris, cs=None, pinned=None, doors=None, cell_bounds=None,
             pin_xy=None):
    """V1 cleanup: drop degenerate triangles, guarantee manifold, compact.

    corridor_union already produces ONE connected, non-overlapping surface (the
    boolean union of the ribbons, retriangulated) — manifold by construction.
    So this welds coincident vertices (the grid-split pieces share boundary
    coords), runs a make-manifold backstop, and drops any UNREACHABLE stray
    component; no decimation.  `cs`/`pinned` accepted for signature stability
    and unused.

    doors: [(x, y, z), ...] door centres — a component reaching a door leads to
    another cell and is kept.  cell_bounds: (minx, miny, maxx, maxy) worldspace
    bounds for an exterior cell — a component touching the border continues into
    the neighbour cell via a worldspace edge-link and is kept.

    Returns (verts, tris) as numpy arrays (float verts, int32 tris).
    """
    verts, tris = _weld_coincident(verts, tris)
    tris = _make_manifold(verts, tris)
    # Only DOORS pin the decimator: a collapse at a threshold kills the Door
    # Triangle.  (pin_xy carries every pathgrid sample and is used by the island
    # pass below; pinning all of it would disable decimation everywhere.)
    verts, tris = decimate(verts, tris,
                           pinned_xy=[(d[0], d[1]) for d in (doors or ())])
    tris = _drop_unreachable_islands(verts, tris, doors, cell_bounds, pin_xy)
    return _compact(verts, tris)


def decimate(verts, tris, pinned_xy=None):
    """Collapse sliver-producing SHORT edges into near-equilateral triangles.

    The grown ribbon outlines (Phase 2) contribute many boundary corners, and a
    corner landing a few units from a lattice point yields a needle however good
    the point sampling was.  Rather than tune the sampler for every case, remove
    the needles directly: repeatedly collapse the shortest edge whose length is
    below DECIMATE_MIN_EDGE, provided the collapse

      * NEVER moves a BOUNDARY vertex.  The outline is the wall standoff: any
        boundary motion — even sliding one boundary vertex onto another, which
        cuts the corner between them — pushes mesh through walls.  So only an
        INTERIOR vertex may be collapsed, and it collapses INTO its neighbour.
      * never touches a PINNED vertex (door threshold corners): collapsing those
        destroys the Door Triangle and the doorway goes dead in the engine,
      * does not flip or degenerate any triangle around it, and
      * does not make the worst edge ratio of the affected triangles worse than
        it already was (so a collapse can only improve shape).

    This is the "minor decimation" that turns fans of slivers into big
    well-shaped triangles without touching coverage.

    pinned_xy: [(x, y), ...] positions that must survive (door thresholds).
    """
    from . import params
    tris = [tuple(int(i) for i in t) for t in tris]
    verts = [list(map(float, v)) for v in verts]
    if not tris:
        return verts, tris

    min_edge = params.DECIMATE_MIN_EDGE
    if min_edge <= 0.0:
        return verts, tris

    # Vertices near a door threshold are pinned: the Door Triangle must keep its
    # shape or _build_door_links finds nothing to flag.
    pin = set()
    if pinned_xy:
        pr2 = params.DECIMATE_PIN_RADIUS ** 2
        for vi, v in enumerate(verts):
            for (px, py) in pinned_xy:
                if (v[0] - px) ** 2 + (v[1] - py) ** 2 <= pr2:
                    pin.add(vi)
                    break

    def _boundary_verts(tl):
        """(boundary vertex set, {vertex: [boundary neighbours]})."""
        cnt = {}
        for t in tl:
            for k in range(3):
                a, b = t[k], t[(k + 1) % 3]
                key = (a, b) if a < b else (b, a)
                cnt[key] = cnt.get(key, 0) + 1
        bset = set()
        nbr = {}
        for (a, b), c in cnt.items():
            if c == 1:
                bset.add(a)
                bset.add(b)
                nbr.setdefault(a, []).append(b)
                nbr.setdefault(b, []).append(a)
        return bset, nbr

    def _outline_error(vi, nbr):
        """How far the outline would move if boundary vertex vi were removed:
        vi's distance from the chord between its two boundary neighbours."""
        ns = nbr.get(vi, ())
        if len(ns) != 2:
            return 1e9                  # a junction/fork on the outline: keep
        p = verts[vi]
        a, b = verts[ns[0]], verts[ns[1]]
        dx, dy = b[0] - a[0], b[1] - a[1]
        d2 = dx * dx + dy * dy
        if d2 < 1e-12:
            return math.hypot(p[0] - a[0], p[1] - a[1])
        t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / d2))
        return math.hypot(p[0] - (a[0] + dx * t), p[1] - (a[1] + dy * t))

    def _edge_ratio(t):
        p, q, r = verts[t[0]], verts[t[1]], verts[t[2]]
        e = [math.dist(p[:2], q[:2]), math.dist(q[:2], r[:2]),
             math.dist(r[:2], p[:2])]
        lo = min(e)
        return (max(e) / lo) if lo > 1e-9 else 1e9

    def _area2(t):
        p, q, r = verts[t[0]], verts[t[1]], verts[t[2]]
        return ((q[0] - p[0]) * (r[1] - p[1]) -
                (q[1] - p[1]) * (r[0] - p[0]))

    for _ in range(params.DECIMATE_ROUNDS):
        boundary, bnbr = _boundary_verts(tris)
        # candidate edges, shortest first
        cands = []
        seen = set()
        for t in tris:
            for k in range(3):
                a, b = t[k], t[(k + 1) % 3]
                key = (a, b) if a < b else (b, a)
                if key in seen:
                    continue
                seen.add(key)
                d = math.dist(verts[a][:2], verts[b][:2])
                if d < min_edge:
                    cands.append((d, key))
        if not cands:
            break
        cands.sort()

        gone = set()
        remap = {}

        def _res(i):
            while i in remap:
                i = remap[i]
            return i

        changed = False
        for _d, (a, b) in cands:
            a, b = _res(a), _res(b)
            if a == b or a in gone or b in gone:
                continue
            a_b, b_b = a in boundary, b in boundary
            # The OUTLINE MAY NOT MOVE.  A boundary vertex is the wall standoff;
            # sliding one boundary vertex onto another cuts the corner between
            # them and pushes the mesh through the wall.  So a boundary vertex is
            # never dropped and never moved — only an INTERIOR vertex collapses,
            # into its neighbour.  Pinned (door) vertices never move either.
            if a in pin or b in pin:
                continue
            if a_b and b_b:
                # Both on the outline.  Removing one MOVES the silhouette, which
                # is how a collapse pushes mesh through a wall — so allow it only
                # when the vertex is nearly collinear with its two boundary
                # neighbours, i.e. the outline barely changes (within
                # DECIMATE_OUTLINE_TOL).  This is the happy medium: straight runs
                # of boundary samples decimate away, real corners never move.
                ea, eb = _outline_error(a, bnbr), _outline_error(b, bnbr)
                if min(ea, eb) > params.DECIMATE_OUTLINE_TOL:
                    continue
                if ea <= eb:
                    keep, drop = b, a        # drop the flatter one (a)
                else:
                    keep, drop = a, b
                target = verts[keep][:]
            elif a_b:                        # b interior -> merge b into a
                keep, drop = a, b
                target = verts[keep][:]
            elif b_b:                        # a interior -> merge a into b
                keep, drop = b, a
                target = verts[keep][:]
            else:
                keep, drop = a, b            # both interior: midpoint
                target = [(verts[a][0] + verts[b][0]) * 0.5,
                          (verts[a][1] + verts[b][1]) * 0.5,
                          (verts[a][2] + verts[b][2]) * 0.5]

            affected = [t for t in tris
                        if (keep in t or drop in t) and not (
                            keep in t and drop in t)]
            before = max([_edge_ratio(t) for t in affected], default=1.0)
            old = verts[keep][:]
            old_signs = [_area2(t) > 0 for t in affected]
            verts[keep] = target
            ok = True
            new_tris = [tuple(keep if i == drop else i for i in t)
                        for t in affected]
            for t, s0 in zip(new_tris, old_signs):
                if len(set(t)) < 3:
                    ok = False
                    break
                ar = _area2(t)
                if abs(ar) < 1e-6 or ((ar > 0) != s0):
                    ok = False             # degenerate or flipped
                    break
            if ok:
                after = max([_edge_ratio(t) for t in new_tris], default=1.0)
                if after > before + 1e-9:
                    ok = False             # only ever improve shape
            if not ok:
                verts[keep] = old
                continue
            # commit
            remap[drop] = keep
            gone.add(drop)
            out = []
            for t in tris:
                nt = tuple(keep if i == drop else i for i in t)
                if len(set(nt)) == 3:
                    out.append(nt)
            tris = out
            changed = True
        if not changed:
            break

    return verts, tris


# TODO(navmesh): revisit — some of these dropped fringe islands are REAL
# coverage the main mesh does not cover (verified on Chorrol: their centroids
# are inside no main-component triangle).  They arise where the retriangulation
# pinched a surface to a single-vertex bowtie, leaving a corner edge-detached.
# The proper fix is to seed the triangulation so the neck stays edge-connected
# (or split the bowtie vertex), NOT to drop.  For now an island is dropped only
# when it is unreachable — connected to NO cell door and NO worldspace border —
# so a doorstep or a border-crossing scrap is always preserved even if tiny.


def _drop_unreachable_islands(verts, tris, doors=None, cell_bounds=None,
                              pin_xy=None):
    """Drop disconnected components that lead nowhere.

    A component is KEPT when it can reach another cell:
      * it comes within ISLAND_DOOR_RADIUS of a door (leads through the door),
      * or (exterior) it touches the cell border, where a worldspace edge-link
        continues it into the neighbour cell,
      * or it carries a PATHGRID line (pin_xy).  The pathgrid is the one part of
        the input that asserts "an actor walks here", so a component covering it
        is reachable BY DEFINITION however isolated this cell's mesh makes it
        look.  Without this, a steep hillside ribbon — kept narrow because steep
        edges are not width-grown — was dropped wholesale on exterior grid
        (-48,-8): all four of its pathgrid edges lost their mesh (4/4 midpoints
        covered before the island pass, 0/4 after).
    Everything still connected to the main body is kept as one component.  Only
    a component that is BOTH disconnected from the main mesh AND reaches no cell
    exit is unreachable noise, and only those are dropped — never by size.
    """
    comps = components(tris)
    if len(comps) <= 1:
        return tris
    comps.sort(key=len, reverse=True)

    doors = doors or []
    dr2 = params.ISLAND_DOOR_RADIUS ** 2
    dz = params.ISLAND_DOOR_ZTOL
    margin = params.ISLAND_EDGE_MARGIN

    pins = list(pin_xy or ())
    pr2 = params.ISLAND_PGRD_RADIUS ** 2

    def reaches_exit(comp):
        for ci in comp:
            for i in tris[ci]:
                vx, vy, vz = verts[i][0], verts[i][1], verts[i][2]
                for (dxp, dyp, dzp) in doors:
                    if ((vx - dxp) ** 2 + (vy - dyp) ** 2 <= dr2 and
                            abs(vz - dzp) <= dz):
                        return True
                for p in pins:
                    if ((vx - p[0]) ** 2 + (vy - p[1]) ** 2 <= pr2 and
                            (len(p) < 3 or abs(vz - p[2]) <= dz)):
                        return True        # carries a pathgrid line
                if cell_bounds is not None:
                    minx, miny, maxx, maxy = cell_bounds
                    if (vx - minx <= margin or maxx - vx <= margin or
                            vy - miny <= margin or maxy - vy <= margin):
                        return True
        return False

    keep = set(comps[0])                     # the main body always stays
    for c in comps[1:]:
        if reaches_exit(c):
            keep.update(c)
    return [t for i, t in enumerate(tris) if i in keep]


def _weld_coincident(verts, tris):
    """Fuse vertices at the SAME rounded coordinate to one index.

    The node stitch snaps the coincident cross-section rails of corridors
    meeting at a node onto identical coordinates; this fuses those into shared
    indices so the corridors share EDGES.  Only exact (rounded) coincidence is
    fused — this never pulls distinct geometry together.
    """
    key_to_vid = {}
    remap = [0] * len(verts)
    out_verts = []
    for i, v in enumerate(verts):
        k = (round(v[0], 1), round(v[1], 1), round(v[2], 1))
        vi = key_to_vid.get(k)
        if vi is None:
            vi = len(out_verts)
            out_verts.append([v[0], v[1], v[2]])
            key_to_vid[k] = vi
        remap[i] = vi
    out_tris = []
    for (a, b, c) in tris:
        a, b, c = remap[a], remap[b], remap[c]
        if a != b and b != c and a != c:
            out_tris.append((a, b, c))
    return out_verts, out_tris


# ---------------------------------------------------------------------------
# Validation (used by tools / acceptance, not by the build)
# ---------------------------------------------------------------------------

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
