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


_WALKED_CELL = 128.0


def _walked_lookup(pin_xy):
    """Bucket walked pathgrid samples for point-in-triangle tests."""
    grid = {}
    for p in (pin_xy or ()):
        grid.setdefault((int(p[0] // _WALKED_CELL), int(p[1] // _WALKED_CELL)),
                        []).append((p[0], p[1], p[2] if len(p) > 2 else None))
    return grid


def _covers_any_sample(verts, t, grid):
    """Does triangle t contain a walked sample, at its own height?"""
    if not grid:
        return False
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = (verts[i][:3] for i in t)
    d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if abs(d) < 1e-9:
        return False
    xs = (ax, bx, cx)
    ys = (ay, by, cy)
    for gx in range(int(min(xs) // _WALKED_CELL),
                    int(max(xs) // _WALKED_CELL) + 1):
        for gy in range(int(min(ys) // _WALKED_CELL),
                        int(max(ys) // _WALKED_CELL) + 1):
            for (px, py, pz) in grid.get((gx, gy), ()):
                l0 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / d
                l1 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / d
                l2 = 1.0 - l0 - l1
                if l0 < -0.01 or l1 < -0.01 or l2 < -0.01:
                    continue
                if pz is None or abs(l0 * az + l1 * bz + l2 * cz
                                     - pz) <= params.MAX_CLIMB + 8.0:
                    return True
    return False


def _make_manifold(verts, tris, pin_xy=None):
    """Drop the smallest triangle on any edge shared by 3+ triangles until every
    edge has at most two.  Keeps the larger (more load-bearing) triangle.

    pin_xy: walked pathgrid samples [(x, y, z, ...), ...].  A triangle that
    carries one of them OUTRANKS every other candidate on the edge, whatever
    its size or neighbour count.  The connectivity guard below cannot protect
    these: dropping the sole mesh in a doorway does not SPLIT the mesh (the
    two rooms still meet elsewhere), it just makes the doorway impassable, so
    the components() test waves it through.  Measured at ImperialDungeon01's
    prison door 0001FC1E, where three triangles fanned off one 17u edge of a
    near-collinear boundary chain and the victim chosen by area/neighbours was
    the only triangle spanning the threshold — a 24u hole across the doorway
    with a 12u height step, the "completely mangled" area in the report.
    """
    tris = [tuple(map(int, t)) for t in tris]

    def area(t):
        va, vb, vc = verts[t[0]], verts[t[1]], verts[t[2]]
        return abs((vb[0] - va[0]) * (vc[1] - va[1]) -
                   (vb[1] - va[1]) * (vc[0] - va[0])) * 0.5

    walked = _walked_lookup(pin_xy)

    def carries_line(t):
        return _covers_any_sample(verts, t, walked)

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
            # Carrying a walked line outranks everything; then connectivity,
            # then area.
            live.sort(key=lambda ti: (carries_line(tris[ti]), nbr[ti],
                                      area(tris[ti])), reverse=True)
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
             pin_xy=None, door_pins=None, node_pins=None):
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
    tris = _make_manifold(verts, tris, pin_xy=pin_xy)
    # Plan-degenerate triangles (three corners collinear in plan) are walls or
    # zero-width seam slivers — no actor can stand on them, and the CK flags
    # the coplanar-antiparallel case as OPPOSITE_NORMALS.  Drop them unless
    # removal would disconnect the mesh (a degenerate connector is still a
    # working NVNM adjacency, so severing it silently strands a region).
    tris = _drop_degenerate_guarded(verts, tris)
    # Only DOORS pin the decimator: a collapse at a threshold kills the Door
    # Triangle.  (pin_xy carries every pathgrid sample and is used by the island
    # pass below; pinning all of it would disable decimation everywhere.)
    # door_pins carries the reserved wedges' RING points (base corners, base
    # midpoint, apex) — decimation collapsing those left the attach nothing to
    # snap the door triangle to where the doorway outreaches its ribbon.
    _pins = ([(d[0], d[1], params.DECIMATE_PIN_CENTER_RADIUS)
              for d in (doors or ())]
             + [(p[0], p[1], params.DECIMATE_PIN_RADIUS)
                for p in (door_pins or ())]
             + [(n[0], n[1], params.DECIMATE_PIN_NODE_RADIUS)
                for n in (node_pins or ())])
    verts, tris = decimate(verts, tris, pinned_xy=_pins,
                           seam_bounds=cell_bounds)
    # The "little bits around the outside": whatever badly-shaped small
    # triangles remain after collapses and flips sit where the outline simply
    # does not admit a good triangle — remove them rather than ship needles.
    # Culling exposes a NEW boundary ring whose triangles were shaped for the
    # old one, so re-smooth (collapse + flip) and cull once more with a small
    # residual budget; one interleave converges (measured), more just churns.
    tris = cull_boundary_slivers(verts, tris, pinned_xy=_pins, pin_xy=pin_xy,
                                 seam_bounds=cell_bounds,
                                 budget_frac=params.CULL_SLIVER_AREA_FRAC)
    # The second round only RE-SMOOTHS the freshly exposed boundary ring:
    # two collapse/flip rounds, no splits (a full second decimate measured
    # ~10% of a dense cell's whole build for marginal further gain).
    verts, tris = decimate(verts, tris, pinned_xy=_pins,
                           seam_bounds=cell_bounds, rounds=2,
                           allow_split=False)
    tris = cull_boundary_slivers(verts, tris, pinned_xy=_pins, pin_xy=pin_xy,
                                 seam_bounds=cell_bounds,
                                 budget_frac=params.CULL_SLIVER_AREA_FRAC
                                 * 0.4)
    # Drop OPEN FLAPS — triangles hanging off a floor's rim into a stairwell.
    # This runs here, after decimation, because that is where they become
    # visible: emission fans a whole floor rim back to one stair vertex below,
    # and while every spoke of that fan is present each shares its long edges
    # with the next, so nothing upstream sees a free edge.  Decimation merges
    # the spokes into one triangle and the fan's two outer edges become the
    # silhouette — which is exactly what makes the flap identifiable.  It runs
    # BEFORE find_ledge_links so a flap is never mistaken for a mezzanine an
    # actor is meant to drop off.
    tris = cull_open_flaps(verts, tris, pin_xy)
    # Find drop-down storeys BEFORE the island cull: a mezzanine an actor is
    # meant to step off is a legitimate component and must not be culled as a
    # stray scrap.  These become NVNM Ledge Up/Down EDGE LINKS (Skyrim's own
    # drop-down mechanism), not bridging geometry — see find_ledge_links.
    ledge_pairs = find_ledge_links(verts, tris)
    # Identify each ledge triangle by its CENTROID, not its index: the passes
    # below drop and reorder both triangles and vertices, so an index captured
    # now is meaningless afterwards.  The centroid survives all of them.
    ledge_marks = [(_centroid(verts, tris[hi]), _centroid(verts, tris[lo]),
                    drop) for (hi, lo, drop) in ledge_pairs]
    tris = _make_manifold(verts, tris, pin_xy=pin_xy)
    # POST-CLEANUP JUNCTION REPAIR.  Decimation can land two components'
    # boundary vertices on the exact same position (a collapse target is
    # another vertex's position) — coincident, but different indices, so
    # nothing upstream can see the contact.  The stitch is re-run here: its
    # first act each round is to fuse coincident vertices, after which its
    # fan-open/bridge machinery (with all its guards) turns the contact into
    # shared edges.  Measured: Moranda02's doorstep quad ended 0.00u from the
    # main mesh, vertex-coincident and edge-disconnected.
    from .corridor_union import _stitch_shared_nodes
    verts = [list(map(float, v)) for v in verts]
    tris = _stitch_shared_nodes(verts, [tuple(t) for t in tris], [])
    tris = _make_manifold(verts, tris, pin_xy=pin_xy)
    tris = _drop_unreachable_islands(verts, tris, doors, cell_bounds, pin_xy)
    verts, tris = _compact(verts, tris)
    # Ledge MARKS (centroids), not indices: build_corridors still appends the
    # door triangles (attach_door_triangles) after this, and de-stacking there
    # can remove triangles, which would shift any index resolved here.  The
    # caller resolves marks to final indices with _resolve_ledges LAST.
    return verts, tris, ledge_marks


def cull_open_flaps(verts, tris, pin_xy=None):
    """Drop triangles that hang off a floor's rim into open space.

    A triangle with TWO UNSHARED edges that each fall a storey-ish drop is not
    ground.  Both of its long edges are the mesh's own silhouette, so nothing
    walks across them and nothing is connected through them; the only thing
    holding it on is the short edge along the floor rim it grew out of.  It
    reaches from that rim down to a single vertex on the stairs below, across a
    void — the shape the author saw jutting "from the upper left to the right"
    through a wall in Pinarus's upstairs.

    WHY SLOPE CANNOT FIND THESE.  `_drop_walls` already removes near-vertical
    triangles, but a flap is not vertical: the Pinarus one measures 30 degrees
    (corners at z 68.6, 68.6 and 18.8 spread over ~95u of plan), comfortably
    inside the walkable band.  Nor is it unsupported — every sample under it
    lands on real collision, because the stairwell it spans genuinely has
    floor at the bottom.  What distinguishes it is purely topological: real
    sloped ground is stitched to its neighbours.  A stair tread shares its
    edges with the treads above and below; a ramp shares its edges with the
    floors it joins.  Even a ledge an actor drops off has ONE free edge, at its
    lip — never two, and never two that both plunge.

    THE PATHGRID IS THE PRIME DIRECTIVE.  Topology alone is not enough: a ramp
    landing in an open room legitimately presents two free sloping edges, and
    the census caught one — LeyawiinCastleCountyHall's 105u/91u ramp spanning
    44u, a ~25 degree slope the authored pathgrid walks straight down.  Culling
    it cost three walked samples.  So a triangle carrying a pathgrid sample is
    never dropped, however it is shaped; the same guard `_drop_walls` applies
    for the same reason.  What remains is only ground the author never routed
    an actor across.

    Census over the reference cells: zero matches in ChorrolFightersGuild,
    ImperialDungeon01, ImperialDungeon05, AnvilFightersGuild and Moranda02, and
    in Pinarus exactly the triangle the author reported.

    The connectivity guard is kept as well: a flap is dropped only when its
    neighbours remain mutually reachable without it, so this can never sever a
    mesh even where the census did not look.
    """
    if not tris:
        return tris
    from .corridor_union import FLAP_EDGE_DROP

    # Pathgrid samples, bucketed for a cheap point-in-triangle test.
    PG_CELL = 128.0
    # How far inside a triangle a walked sample must fall to count as CROSSING
    # it.  A corner or edge contact is not evidence the pathgrid uses this
    # ground; every flap touches the rim it hangs from.  5% of the triangle is
    # a small margin in absolute terms (a few units on a 100u triangle) yet
    # excludes vertex and edge hits cleanly.
    PG_INSIDE_FRAC = 0.05
    pg = {}
    for p in (pin_xy or ()):
        pg.setdefault((int(p[0] // PG_CELL), int(p[1] // PG_CELL)),
                      []).append((p[0], p[1]))

    def _carries_pathgrid(t):
        """Does a walked sample land INSIDE this triangle, off its corners?

        A sample sitting exactly ON a corner proves only that the walked line
        reaches the rim the flap grew from — which it always does, since that
        is the floor edge.  The Pinarus flap's single "hit" was precisely its
        own corner (-269.9, 58.2), barycentric (0,1,0).  Requiring a strictly
        interior sample is what separates "the pathgrid crosses this ground"
        from "the pathgrid touches the vertex it hangs off".
        """
        (ax, ay) = verts[t[0]][0], verts[t[0]][1]
        (bx, by) = verts[t[1]][0], verts[t[1]][1]
        (cx, cy) = verts[t[2]][0], verts[t[2]][1]
        d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(d) < 1e-9:
            return False
        xs, ys = (ax, bx, cx), (ay, by, cy)
        for gx in range(int(min(xs) // PG_CELL), int(max(xs) // PG_CELL) + 1):
            for gy in range(int(min(ys) // PG_CELL),
                            int(max(ys) // PG_CELL) + 1):
                for (px, py) in pg.get((gx, gy), ()):
                    l0 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / d
                    l1 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / d
                    l2 = 1.0 - l0 - l1
                    if min(l0, l1, l2) >= PG_INSIDE_FRAC:
                        return True
        return False

    edge_tris = {}
    for ti, t in enumerate(tris):
        for k in range(3):
            a, b = t[k], t[(k + 1) % 3]
            edge_tris.setdefault((a, b) if a < b else (b, a), []).append(ti)

    cands = []
    for ti, t in enumerate(tris):
        free = 0
        for k in range(3):
            a, b = t[k], t[(k + 1) % 3]
            key = (a, b) if a < b else (b, a)
            if len(edge_tris.get(key, ())) != 1:
                continue
            if abs(verts[a][2] - verts[b][2]) >= FLAP_EDGE_DROP:
                free += 1
        if free >= 2 and not (pg and _carries_pathgrid(t)):
            cands.append(ti)
    if not cands:
        return tris

    alive = [True] * len(tris)

    def _neighbours(ti):
        out = []
        t = tris[ti]
        for k in range(3):
            a, b = t[k], t[(k + 1) % 3]
            for tj in edge_tris.get((a, b) if a < b else (b, a), ()):
                if tj != ti and alive[tj]:
                    out.append(tj)
        return out

    for ti in cands:
        nbrs = _neighbours(ti)
        if len(nbrs) > 1:
            # All neighbours must reach each other without crossing ti.
            target = set(nbrs[1:])
            seen = {nbrs[0], ti}
            queue = [nbrs[0]]
            budget = 128
            while queue and target and budget:
                budget -= 1
                cur = queue.pop()
                for tj in _neighbours(cur):
                    if tj in seen:
                        continue
                    seen.add(tj)
                    target.discard(tj)
                    queue.append(tj)
            if target:
                continue                # ti is the only link: keep it
        alive[ti] = False
    return [t for ti, t in enumerate(tris) if alive[ti]]


def _badness(verts, t):
    """Normalised shape badness; 1.0 = exactly at the contract boundary.

    max(edge_ratio / MAX_EDGE_RATIO, aspect / MAX_TRI_ASPECT): the ratio term
    catches needles (one short edge), the aspect term catches CAPS (all edges
    comparable, near-zero height) which the ratio cannot see.
    """
    p, q, r = verts[t[0]], verts[t[1]], verts[t[2]]
    e = [math.dist(p[:2], q[:2]), math.dist(q[:2], r[:2]),
         math.dist(r[:2], p[:2])]
    lo, hi = min(e), max(e)
    area = abs((q[0] - p[0]) * (r[1] - p[1]) -
               (q[1] - p[1]) * (r[0] - p[0])) * 0.5
    if lo <= 1e-9 or area <= 1e-9:
        return 1e9
    return max((hi / lo) / params.MAX_EDGE_RATIO,
               (hi * hi / (4.0 * area)) / params.MAX_TRI_ASPECT)


def _drop_degenerate_guarded(verts, tris):
    """Drop plan-degenerate (XY footprint < MIN_XY_FOOTPRINT) triangles, but
    never disconnect the mesh: a candidate that is the only route between its
    neighbours is kept.

    NOTE the one-at-a-time fallback re-derives candidate INDICES after every
    removal.  The first version remapped a pre-built index list while
    iterating it, and the stale indices deleted arbitrary HEALTHY triangles
    once the list had shifted — measured on Moranda02 as 122 walked-line
    samples losing their (7,000u^2!) covering triangles.
    """
    tris = [tuple(map(int, t)) for t in tris]

    def _deg(t):
        va, vb, vc = verts[t[0]], verts[t[1]], verts[t[2]]
        cross = ((vb[0] - va[0]) * (vc[1] - va[1]) -
                 (vb[1] - va[1]) * (vc[0] - va[0]))
        # NO SHAPE-BASED CULL HERE.  Dropping tiny high-badness "spikes" was
        # tried (5% of MIN_TRI_AREA and 4x the shape contract, with both the
        # local BFS guard and a walked-line exemption) and is a NET LOSS: it
        # removed the badness-28 needle at ImperialDungeon01's prison door but
        # cost 25 more defects across the reference cells, LeyawiinCastleCounty
        # Hall alone going from 10 crack edges to 21.  A spike is usually the
        # last adjacency between two sheets, and adjacency the engine can use
        # beats a tidy triangle list.  Shape is the decimator's and the sliver
        # cull's job, where coverage and width are checked first.
        return abs(cross) * 0.5 < params.MIN_XY_FOOTPRINT

    if not any(_deg(t) for t in tris):
        return tris
    # LOCAL connectivity guard (bounded BFS over shared-edge adjacency, the
    # same pattern the sliver cull uses).  The first version re-ran a whole-
    # mesh components() per removal trial — 2.6s on one cave cell.
    edge_tris = {}
    for ti, t in enumerate(tris):
        for k in range(3):
            a, b = t[k], t[(k + 1) % 3]
            edge_tris.setdefault((a, b) if a < b else (b, a), []).append(ti)
    alive = [True] * len(tris)

    def _neighbours(ti):
        out = []
        for k in range(3):
            a, b = tris[ti][k], tris[ti][(k + 1) % 3]
            for tj in edge_tris.get((a, b) if a < b else (b, a), ()):
                if tj != ti and alive[tj]:
                    out.append(tj)
        return out

    def _removable(ti):
        nbrs = _neighbours(ti)
        if len(nbrs) <= 1:
            return True
        target = set(nbrs[1:])
        seen = {nbrs[0], ti}
        queue = [nbrs[0]]
        fuel = 256
        while queue and target and fuel:
            fuel -= 1
            cur = queue.pop()
            for tj in _neighbours(cur):
                if tj in seen:
                    continue
                seen.add(tj)
                target.discard(tj)
                queue.append(tj)
        return not target

    for ti in range(len(tris)):
        if _deg(tris[ti]) and _removable(ti):
            alive[ti] = False
    tris = [t for ti, t in enumerate(tris) if alive[ti]]

    return tris




# Fraction of the ribbon standoff a CONCAVE outline cut may consume.  The
# standoff is what keeps the boundary clear of collision, so staying well
# inside it is what makes an outward cut safe.
CONCAVE_CUT_FRAC = 0.25


def decimate(verts, tris, pinned_xy=None, seam_bounds=None, rounds=None,
             allow_split=True):
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

    Additionally, SAWTOOTH boundary vertices are cut: a vertex that juts
    OUTWARD from its neighbours' chord (convex) may be removed with a larger
    deviation (DECIMATE_SAWTOOTH_DEV), because cutting it can only SHRINK the
    mesh — never push it through a wall — and the union outline's zigzag
    teeth are exactly such vertices.  The cuts are inward-only (a concave
    vertex is never removed: that would EXPAND the outline), bounded in total
    by DECIMATE_MAX_AREA_LOSS of the mesh's area, and every existing guard
    (pins, flips, edge-ratio, link condition) still applies, so a cut can
    never disconnect anything or open an interior hole.

    pinned_xy: [(x, y), ...] positions that must survive (door thresholds).
    seam_bounds: (minx, miny, maxx, maxy) — exterior cell rectangle.  Boundary
    vertices ON this rectangle are the cross-cell seam and only ever collapse
    under the strict collinear rule, so the seam line build_edge_links
    stitches against cannot be cut inward.
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
    # shape or _build_door_links finds nothing to flag.  Entries are (x, y) or
    # (x, y, radius); a bare pair takes the ring-point radius.
    pin = _pin_verts(verts, pinned_xy)

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
        return _badness(verts, t)

    def _on_seam(vi):
        if seam_bounds is None:
            return False
        x, y = verts[vi][0], verts[vi][1]
        minx, miny, maxx, maxy = seam_bounds
        return (abs(x - minx) <= 0.5 or abs(x - maxx) <= 0.5
                or abs(y - miny) <= 0.5 or abs(y - maxy) <= 0.5)

    def _area2(t):
        p, q, r = verts[t[0]], verts[t[1]], verts[t[2]]
        return ((q[0] - p[0]) * (r[1] - p[1]) -
                (q[1] - p[1]) * (r[0] - p[0]))

    # Sawtooth-cut budget: the inward boundary cuts may remove at most this
    # much plan area in total, so the periphery is straightened, never eaten.
    total_area = sum(abs((verts[t[1]][0] - verts[t[0]][0])
                         * (verts[t[2]][1] - verts[t[0]][1])
                         - (verts[t[1]][1] - verts[t[0]][1])
                         * (verts[t[2]][0] - verts[t[0]][0]))
                     for t in tris) * 0.5
    area_budget = params.DECIMATE_MAX_AREA_LOSS * total_area
    area_lost = 0.0

    for _ in range(rounds if rounds is not None else params.DECIMATE_ROUNDS):
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

        # Vertex -> triangle-index incidence, maintained through collapses.
        # The previous form scanned and REBUILT the whole triangle list per
        # committed collapse — O(T) twice per candidate, quadratic per cell —
        # which alone pushed a large cell's decimation into minutes.
        tris = [tuple(t) for t in tris]
        alive = [True] * len(tris)
        vmap = {}
        for ti, t in enumerate(tris):
            for i in t:
                vmap.setdefault(i, set()).add(ti)

        gone = set()
        remap = {}

        def _res(i):
            while i in remap:
                i = remap[i]
            return i

        def _outline_convex(vi):
            """Does boundary vertex vi jut OUTWARD (away from the interior)?

            Removal of a convex vertex cuts the corner INWARD — allowed; a
            concave vertex's removal would extend the mesh outward across
            ground it never covered — never allowed.
            """
            ns = bnbr.get(vi, ())
            if len(ns) != 2:
                return False
            a0, a1 = verts[ns[0]], verts[ns[1]]
            p = verts[vi]
            dx, dy = a1[0] - a0[0], a1[1] - a0[1]
            side_v = dx * (p[1] - a0[1]) - dy * (p[0] - a0[0])
            # Interior mass: mean centroid of the alive triangles at vi.
            cx = cy = 0.0
            cnt = 0
            for ti in vmap.get(vi, ()):
                if not alive[ti]:
                    continue
                t = tris[ti]
                cx += (verts[t[0]][0] + verts[t[1]][0] + verts[t[2]][0]) / 3.0
                cy += (verts[t[0]][1] + verts[t[1]][1] + verts[t[2]][1]) / 3.0
                cnt += 1
            if not cnt:
                return False
            side_c = (dx * (cy / cnt - a0[1]) - dy * (cx / cnt - a0[0]))
            if abs(side_v) < 1e-9 or abs(side_c) < 1e-9:
                return False
            return (side_v > 0) != (side_c > 0)

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
            # A PIN MAY NOT MOVE — but the vertex at the OTHER end of the edge
            # may still collapse INTO it.  The pin's position is what matters
            # (a door threshold corner, a pathgrid junction); nothing about it
            # requires its neighbour to stay.  Refusing the whole edge froze
            # the merge the author asked for: v464 sits 7.5u from pathgrid
            # node n124, so the 24u node pin vetoed collapsing the redundant
            # v598 into it and two 300u^2 slivers survived on the stairs.
            # Only both-pinned is a genuine stalemate.
            pin_a, pin_b = a in pin, b in pin
            if pin_a and pin_b:
                # Two pins may still fuse when they are REDUNDANT: a pin
                # protects a POSITION, and a vertex that lies (within
                # DECIMATE_OUTLINE_TOL) on the straight line between its two
                # boundary neighbours contributes no position of its own —
                # dropping it leaves the outline exactly where it was, pin
                # included.  A single door's 24u centre pin covers ~50u of
                # boundary, so at ImperialDungeon01's prison door FOUR
                # consecutive vertices of one near-collinear chain were all
                # pinned and could never collapse into each other; the chain
                # forced the fan of 8-360u^2 slivers through the doorway that
                # the author reported as "completely mangled".
                if not (a in boundary and b in boundary):
                    continue
                if b not in bnbr.get(a, ()):
                    continue
                if min(_outline_error(a, bnbr),
                       _outline_error(b, bnbr)) > params.DECIMATE_OUTLINE_TOL:
                    continue
            if a_b and b_b:
                # Two outline vertices may only collapse along an OUTLINE
                # edge.  If the edge between them is interior, they sit on
                # opposite sides of a thin neck; fusing them pinches the
                # sheet at a point and the far side comes off as a
                # vertex-attached scrap (BarrenCave: [1768, 6, 5, 3]).
                if b not in bnbr.get(a, ()):
                    continue
                # Both on the outline.  Removing one MOVES the silhouette, which
                # is how a collapse pushes mesh through a wall — so allow it only
                # when the vertex is nearly collinear with its two boundary
                # neighbours (the outline barely changes, DECIMATE_OUTLINE_TOL),
                # OR when it is a SAWTOOTH: a convex vertex whose removal cuts
                # the corner strictly INWARD (up to DECIMATE_SAWTOOTH_DEV,
                # within the area budget).  Straight runs decimate away, teeth
                # are cut off, concave corners never move.  A vertex on the
                # exterior cell seam only ever collapses collinearly, so the
                # seam line the neighbour cell stitches against stays put.
                ea, eb = _outline_error(a, bnbr), _outline_error(b, bnbr)
                tol = params.DECIMATE_OUTLINE_TOL
                dev = params.DECIMATE_SAWTOOTH_DEV
                budget_left = area_lost < area_budget
                # CONCAVE corners get a small allowance too.  The outline is a
                # wall STANDOFF, not the wall: a ribbon is laid at
                # RIBBON_HALF_WIDTH from its centreline, so the boundary
                # already sits clear of real collision (measured 11-17u at the
                # Pinarus corner).  A concave corner whose removal pushes the
                # silhouette out by less than that clearance therefore cannot
                # reach a wall — and refusing it is what left the sliver fans
                # nothing could repair: such a corner cannot be collapsed
                # (this rule), cannot be flipped (the quad around a concave
                # corner is non-convex) and cannot be split (its longest edge
                # is under the split floor).  A census of five reference cells
                # found 16-32 of them EACH, carrying triangles up to badness
                # 68.  The allowance is deliberately a fraction of the
                # standoff, so it can never cross the wall the standoff buys.
                cc_tol = max(tol, CONCAVE_CUT_FRAC * params.RIBBON_HALF_WIDTH)
                ok_a = ea <= tol or (budget_left and not _on_seam(a)
                                     and (ea <= dev if _outline_convex(a)
                                          else ea <= cc_tol))
                ok_b = eb <= tol or (budget_left and not _on_seam(b)
                                     and (eb <= dev if _outline_convex(b)
                                          else eb <= cc_tol))
                if not (ok_a or ok_b):
                    continue
                if pin_a and pin_b:
                    # Both redundant-pinned (checked above): drop whichever
                    # deviates least from the outline it sits on.
                    keep, drop = ((a, b) if _outline_error(b, bnbr)
                                  <= _outline_error(a, bnbr) else (b, a))
                elif pin_a or pin_b:
                    # One end is pinned: it survives, the other dissolves into
                    # it, so the pinned position is untouched.
                    if pin_a and not ok_b:
                        continue        # only `a` could go, and it may not
                    if pin_b and not ok_a:
                        continue
                    keep, drop = (a, b) if pin_a else (b, a)
                elif ok_a and (not ok_b or ea <= eb):
                    keep, drop = b, a        # drop the flatter/jutting one (a)
                else:
                    keep, drop = a, b
                target = verts[keep][:]
            elif a_b:                        # b interior -> merge b into a
                keep, drop = a, b
                target = verts[keep][:]
            elif b_b:                        # a interior -> merge a into b
                keep, drop = b, a
                target = verts[keep][:]
            elif pin_a or pin_b:
                # Both interior but one is pinned: keep the pin exactly where
                # it is instead of averaging it away to a midpoint.
                keep, drop = (a, b) if pin_a else (b, a)
                target = verts[keep][:]
            else:
                keep, drop = a, b            # both interior: midpoint
                target = [(verts[a][0] + verts[b][0]) * 0.5,
                          (verts[a][1] + verts[b][1]) * 0.5,
                          (verts[a][2] + verts[b][2]) * 0.5]

            # LINK CONDITION (topology guard).  A collapse is edge-topology
            # safe only when the two vertices' neighbourhoods meet EXACTLY at
            # the opposite corners of the triangles being collapsed.  If they
            # share any other vertex, the collapse pinches the surface into a
            # bowtie joined at a single vertex — edge adjacency (what the
            # engine walks) then reads it as TWO components (measured on
            # BarrenCave: decimation took one connected cave to [1771, 7]).
            la = {v for ti in vmap.get(a, ()) if alive[ti]
                  for v in tris[ti]} - {a, b}
            lb = {v for ti in vmap.get(b, ()) if alive[ti]
                  for v in tris[ti]} - {a, b}
            opp = {v for ti in vmap.get(a, ()) if alive[ti] and b in tris[ti]
                   for v in tris[ti]} - {a, b}
            extra = (la & lb) - opp
            if extra:
                # A shared vertex beyond the opposite corners normally means
                # the collapse would pinch a bowtie.  But when EVERY triangle
                # linking a (or b) to that vertex is one of the two being
                # collapsed away, there is no second path to pinch: the link
                # exists only inside the sliver pair the merge dissolves.
                #
                # This is the case the author drew: on ImperialDungeon01's
                # stairs, v600 and v601 sit 2.5u apart on the outline and each
                # reaches the V apex v382 through its own 300u^2 sliver.  The
                # strict test read that as a bowtie and refused, so the two
                # slivers survived every pass — neither dropped (they carry
                # adjacency) nor merged.  Allowing it produces exactly the two
                # fat triangles the author sketched: (394,382,465) and
                # (382,514,464), each a sliver absorbed into its neighbour.
                # The pinch the link condition guards against is a surface
                # closing on itself at a point.  That needs the collapsed edge
                # to be INTERIOR: with mesh on both sides, fusing its ends
                # joins two separate fans and the join is a single vertex.  On
                # a BOUNDARY edge (one owner) there is no second fan — one
                # side is open space — so the collapse simply shortens the
                # outline and cannot bowtie, whatever else the two vertices
                # happen to share.
                #
                # This is the case the author drew: on ImperialDungeon01's
                # stairs, v600/v601 sit 2.5u apart on the OUTLINE, joined by a
                # single 71u^2 sliver, and each reaches the V apex v382
                # through its own 300u^2 sliver.  The strict test saw v382 as
                # an extra shared vertex and refused, so the slivers survived
                # every pass — neither dropped (they carry adjacency) nor
                # merged.  Allowing it yields exactly the two fat triangles
                # the author sketched: (394,382,465) and (382,514,464).
                shared_tris = sum(1 for ti in vmap.get(a, ())
                                  if alive[ti] and b in tris[ti])
                if shared_tris != 1:
                    continue

            inc = (vmap.get(keep, set()) | vmap.get(drop, set()))
            aff_idx = [ti for ti in inc if alive[ti]
                       and not (keep in tris[ti] and drop in tris[ti])]
            affected = [tris[ti] for ti in aff_idx]
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
                # Shape bound: a collapse may not push any affected triangle
                # past the shape contract (badness 1.0), nor past the WORST
                # shape already present.  ("Strictly improve" was tried and
                # stalled: a sawtooth cut often worsens one neighbour a little
                # before the next collapse fixes it, so the outline never
                # cleaned.)
                if after > max(before, 1.0) + 1e-9:
                    ok = False
            # Charge a boundary cut's removed ground against the budget.  The
            # area of the triangles that vanish (they contained both keep and
            # drop) minus what the survivors regain is the ground the outline
            # gave up; interior collapses net to ~zero and charge nothing.
            loss = 0.0
            if ok and a_b and b_b:
                killed = [tris[ti] for ti in inc
                          if alive[ti] and keep in tris[ti]
                          and drop in tris[ti]]
                verts[keep] = old
                before_area = sum(abs(_area2(t))
                                  for t in affected + killed) * 0.5
                verts[keep] = target
                after_area = sum(abs(_area2(t)) for t in new_tris) * 0.5
                loss = max(0.0, before_area - after_area)
                if loss > 1.0 and area_lost + loss > area_budget:
                    ok = False
            if not ok:
                verts[keep] = old
                continue
            area_lost += loss
            # commit: remap the triangles around `drop` in place.
            remap[drop] = keep
            gone.add(drop)
            for ti in list(vmap.get(drop, ())):
                if not alive[ti]:
                    continue
                t = tris[ti]
                nt = tuple(keep if i == drop else i for i in t)
                for i in set(t):
                    vmap.get(i, set()).discard(ti)
                if len(set(nt)) < 3:
                    alive[ti] = False
                    continue
                tris[ti] = nt
                for i in set(nt):
                    vmap.setdefault(i, set()).add(ti)
            changed = True
        tris = [t for ti, t in enumerate(tris) if alive[ti]]
        # EDGE FLIPS.  Collapses cannot fix a fan of long slivers whose edges
        # are all above the collapse threshold — the classic boundary-driven
        # CDT artefact ("long thin triangles").  Flipping the shared diagonal
        # of a convex pair moves NO vertex, so the outline and coverage are
        # untouched by construction; a flip is taken only when it strictly
        # improves the pair's worst edge ratio.
        flipped = _flip_pass(verts, tris, pin)
        if flipped is not None:
            tris = flipped
        # LONG-EDGE BISECTION.  A needle whose edges are all LONG can be fixed
        # by neither collapse (nothing short) nor flip (the neighbour may be
        # fine already): bisect its longest edge instead.  The split moves no
        # vertex and changes no coverage — it only adds a vertex ON the edge —
        # and the next round's flips and collapses reshape the halves.
        split = (_split_needles(verts, tris, pin, _on_seam)
                 if allow_split else None)
        if split is not None:
            tris = split
        if not changed and flipped is None and split is None:
            break

    return verts, tris


def _split_needles(verts, tris, pin, on_seam):
    """Bisect the LONGEST edge of triangles violating MAX_EDGE_RATIO.

    Only edges long enough that both halves stay above DECIMATE_MIN_EDGE are
    split (>= 2 * DECIMATE_MIN_EDGE), so a split never creates collapse bait
    and the pass terminates.  An interior edge splits BOTH owners, so no
    hanging node is ever created; door-wedge triangles (all corners pinned)
    and exterior-seam edges are never touched.  Returns the new triangle list
    or None if nothing split.
    """
    from . import params
    min_len = 2.0 * params.DECIMATE_MIN_EDGE
    edge_tris = {}
    for ti, t in enumerate(tris):
        for k in range(3):
            a, b = t[k], t[(k + 1) % 3]
            edge_tris.setdefault((a, b) if a < b else (b, a),
                                 []).append(ti)

    def _elen(a, b):
        return math.dist(verts[a][:2], verts[b][:2])

    consumed = set()
    new_tris = []
    dead = set()
    for ti, t in enumerate(tris):
        if ti in consumed:
            continue
        if _badness(verts, t) <= 1.0:
            continue
        cand = [(_elen(t[k], t[(k + 1) % 3]), k) for k in range(3)]
        d, k = max(cand)
        if d < min_len:
            continue
        a, b = t[k], t[(k + 1) % 3]
        if on_seam(a) and on_seam(b):
            continue
        key = (a, b) if a < b else (b, a)
        owners = edge_tris.get(key, ())
        if len(owners) > 2:
            continue                        # non-manifold edge: leave alone
        if any(o in consumed for o in owners):
            continue
        if any(all(v in pin for v in tris[o]) for o in owners):
            continue
        # Split at the PROJECTION of the bad triangle's apex onto the long
        # edge (a cap's badness lives at its apex; the midpoint leaves two
        # smaller caps), clamped so both halves stay above DECIMATE_MIN_EDGE.
        apx = verts[t[(k + 2) % 3]]
        dx_, dy_ = verts[b][0] - verts[a][0], verts[b][1] - verts[a][1]
        tproj = (((apx[0] - verts[a][0]) * dx_
                  + (apx[1] - verts[a][1]) * dy_) / (d * d))
        tlo = params.DECIMATE_MIN_EDGE / d
        tproj = max(tlo, min(1.0 - tlo, tproj))
        mid = (verts[a][0] + dx_ * tproj,
               verts[a][1] + dy_ * tproj,
               verts[a][2] + (verts[b][2] - verts[a][2]) * tproj)

        # The split must IMPROVE the local worst shape, or it just mints two
        # thinner slivers (bisecting a triangle whose SHORT edge is tiny
        # halves the long side but keeps the tiny side — strictly worse;
        # measured: two r=11 slivers appeared where one r=4 had been).
        mv = [list(v_) for v_ in ([verts[a], verts[b], list(mid)])]

        def _b3(pa, pb, pc):
            return _badness([pa, pb, pc], (0, 1, 2))

        before = max(_badness(verts, tris[o]) for o in owners)
        after = 0.0
        halves = []
        ok = True
        for o in owners:
            to = tris[o]
            for kk in range(3):
                if {to[kk], to[(kk + 1) % 3]} == {a, b}:
                    cvi = to[(kk + 2) % 3]
                    after = max(after,
                                _b3(verts[to[kk]], list(mid), verts[cvi]),
                                _b3(list(mid), verts[to[(kk + 1) % 3]],
                                    verts[cvi]))
                    halves.append((o, kk, cvi))
                    break
            else:
                ok = False
        if not ok or after >= before - 1e-9:
            continue
        m = len(verts)
        verts.append([mid[0], mid[1], mid[2]])
        for (o, kk, cvi) in halves:
            to = tris[o]
            new_tris.append((to[kk], m, cvi))
            new_tris.append((m, to[(kk + 1) % 3], cvi))
            dead.add(o)
            consumed.add(o)
    if not new_tris:
        return None
    out = [t for ti, t in enumerate(tris) if ti not in dead]
    out.extend(new_tris)
    return out


def _pin_verts(verts, pinned_xy):
    """Vertex indices pinned by the (x, y[, radius]) door-pin list.

    Bucketed: the pin list now carries every pathgrid node, and the naive
    O(verts x pins) scan was 2.5s of a single cave cell's build.
    """
    pin = set()
    if not pinned_xy:
        return pin
    default_r = params.DECIMATE_PIN_RADIUS
    rmax = max((p[2] if len(p) > 2 else default_r) for p in pinned_xy)
    cell = max(float(rmax), 1.0)
    buckets = {}
    for p in pinned_xy:
        buckets.setdefault((int(p[0] // cell), int(p[1] // cell)),
                           []).append(p)
    for vi, v in enumerate(verts):
        gx, gy = int(v[0] // cell), int(v[1] // cell)
        hit = False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for p in buckets.get((gx + dx, gy + dy), ()):
                    r = p[2] if len(p) > 2 else default_r
                    if (v[0] - p[0]) ** 2 + (v[1] - p[1]) ** 2 <= r * r:
                        pin.add(vi)
                        hit = True
                        break
                if hit:
                    break
            if hit:
                break
    return pin


def cull_boundary_slivers(verts, tris, pinned_xy=None, pin_xy=None,
                          seam_bounds=None, budget_frac=None):
    """Remove small, badly-shaped triangles on the OUTLINE.

    After collapses and flips have done what they can, a residual needle on
    the boundary means the outline's shape does not admit a good triangle
    there — the corridor union's fringe, not usable ground.  Per the design
    brief those little bits are simply REMOVED: an actor loses a sliver of
    fringe it could not stand on anyway, and the mesh keeps only triangles
    honouring the shape contract.

    A boundary triangle is culled when (ratio > CULL_SLIVER_RATIO and area <
    CULL_SLIVER_MAX_AREA) or area < MIN_TRI_AREA, and ALL of:
      * it touches no door-pinned vertex (the Door Triangle region is a
        contract with the engine),
      * no pathgrid sample lies inside it (the walked line is the one input
        asserting an actor stands there),
      * none of its edges lies on the exterior cell seam (build_edge_links
        stitches the neighbour cell against those),
      * its neighbours stay mutually reachable without it (bounded BFS — a
        cull can never disconnect), and
      * the total culled area stays under CULL_SLIVER_AREA_FRAC of the mesh.
    """
    if not tris:
        return tris
    tris = [tuple(t) for t in tris]

    total_area = sum(abs((verts[t[1]][0] - verts[t[0]][0])
                         * (verts[t[2]][1] - verts[t[0]][1])
                         - (verts[t[1]][1] - verts[t[0]][1])
                         * (verts[t[2]][0] - verts[t[0]][0]))
                     for t in tris) * 0.5
    budget = ((budget_frac if budget_frac is not None
               else params.CULL_SLIVER_AREA_FRAC) * total_area)
    removed_area = 0.0

    pin = _pin_verts(verts, pinned_xy)

    # Pathgrid samples bucketed for the containment test (with z and the
    # line direction when known).
    pgrid = {}
    pcell = 128.0
    for p in (pin_xy or ()):
        px, py = p[0], p[1]
        pz = p[2] if len(p) > 2 else None
        pu = (p[3], p[4]) if len(p) > 4 else None
        pgrid.setdefault((int(px // pcell), int(py // pcell)),
                         []).append((px, py, pz, pu))

    def _pgrd_samples_in(t):
        """Pathgrid samples inside triangle t (as (px, py, pz-or-None) list)."""
        xs = [verts[i][0] for i in t]
        ys = [verts[i][1] for i in t]
        (ax, ay), (bx, by), (cx, cy) = ((verts[i][0], verts[i][1])
                                        for i in t)
        d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(d) < 1e-9:
            return []
        out = []
        for gx in range(int(min(xs) // pcell), int(max(xs) // pcell) + 1):
            for gy in range(int(min(ys) // pcell), int(max(ys) // pcell) + 1):
                for (px, py, pz, _pu) in pgrid.get((gx, gy), ()):
                    l0 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / d
                    l1 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / d
                    if l0 >= -0.02 and l1 >= -0.02 and (1 - l0 - l1) >= -0.02:
                        out.append((px, py, pz))
        return out

    def _on_seam(vi):
        if seam_bounds is None:
            return False
        x, y = verts[vi][0], verts[vi][1]
        minx, miny, maxx, maxy = seam_bounds
        return (abs(x - minx) <= 0.5 or abs(x - maxx) <= 0.5
                or abs(y - miny) <= 0.5 or abs(y - maxy) <= 0.5)

    def _shape(t):
        p, q, r = verts[t[0]], verts[t[1]], verts[t[2]]
        area = abs((q[0] - p[0]) * (r[1] - p[1]) -
                   (q[1] - p[1]) * (r[0] - p[0])) * 0.5
        return _badness(verts, t), area

    for _round in range(3):
        counts = {}
        for t in tris:
            for k in range(3):
                a, b = t[k], t[(k + 1) % 3]
                key = (a, b) if a < b else (b, a)
                counts[key] = counts.get(key, 0) + 1
        edge_tris = {}
        for ti, t in enumerate(tris):
            for k in range(3):
                a, b = t[k], t[(k + 1) % 3]
                edge_tris.setdefault((a, b) if a < b else (b, a),
                                     []).append(ti)
        alive = [True] * len(tris)

        def _neighbours(ti):
            out = []
            for k in range(3):
                a, b = tris[ti][k], tris[ti][(k + 1) % 3]
                for tj in edge_tris.get((a, b) if a < b else (b, a), ()):
                    if tj != ti and alive[tj]:
                        out.append(tj)
            return out

        cands = []
        for ti, t in enumerate(tris):
            nb_edges = sum(1 for k in range(3)
                           if counts.get((min(t[k], t[(k + 1) % 3]),
                                          max(t[k], t[(k + 1) % 3]))) == 1)
            if nb_edges == 0:
                continue                    # interior triangle: not fringe
            # ...and ONE boundary edge is not enough to make it fringe.  A
            # triangle with a single edge on the outline and its other two
            # deep in the interior is a WEDGE filling a concave pocket:
            # removing it does not trim the fringe, it bites a slim V into
            # walkable ground, apex inward, with open boundary down both new
            # sides.  Coverage and crack metrics are both blind to that (no
            # ground is uncovered, no walked line crosses the new boundary),
            # so it shipped while being plainly visible on the staircase —
            # measured on ImperialDungeon01, ONE cull round took the cell
            # from 1 open notch to 7, two of them bitten out of the tower
            # stairs the author reported twice.
            #
            # True fringe has TWO boundary edges (a corner of the outline
            # with one edge back into the mesh): removing it leaves the
            # remaining boundary running straight through, which is what
            # "remove the little bits around the outside" actually means.
            if nb_edges < 2:
                continue
            ratio, area = _shape(t)
            # ratio here is normalised badness: 1.0 == the shape contract
            # (CULL_SLIVER_RATIO / MAX_EDGE_RATIO for needles, MAX_TRI_ASPECT
            # for caps).
            bad = ((ratio > 1.0 and area < params.CULL_SLIVER_MAX_AREA)
                   or area < params.MIN_TRI_AREA)
            if not bad:
                continue
            if any(v in pin for v in t):
                continue
            if any(counts.get((min(t[k], t[(k + 1) % 3]),
                               max(t[k], t[(k + 1) % 3]))) == 1
                   and _on_seam(t[k]) and _on_seam(t[(k + 1) % 3])
                   for k in range(3)):
                continue                    # a border edge on the cell seam
            cands.append((-ratio, ti, area, _pgrd_samples_in(t)))
        cands.sort(key=lambda c: (c[0], c[1]))

        def _z_at(tj, x, y):
            (ax, ay, az) = verts[tris[tj][0]]
            (bx, by, bz) = verts[tris[tj][1]]
            (cx, cy, cz) = verts[tris[tj][2]]
            d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
            if abs(d) < 1e-9:
                return None
            l0 = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / d
            l1 = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / d
            l2 = 1.0 - l0 - l1
            if l0 < -0.02 or l1 < -0.02 or l2 < -0.02:
                return None
            return l0 * az + l1 * bz + l2 * cz

        # Triangle buckets for the coverage test below.
        tcell = 128.0
        tgrid = {}
        for tj, tt in enumerate(tris):
            xs = [verts[i][0] for i in tt]
            ys = [verts[i][1] for i in tt]
            for gx in range(int(min(xs) // tcell), int(max(xs) // tcell) + 1):
                for gy in range(int(min(ys) // tcell),
                                int(max(ys) // tcell) + 1):
                    tgrid.setdefault((gx, gy), []).append(tj)

        def _samples_still_covered(ti, samples):
            """Would every pathgrid sample in ti stay covered without it?

            The walked line is sacrosanct — but a sliver merely GRAZED by the
            line at a corner shares those samples with its neighbours, and
            removing it costs the line nothing.  Only a sliver that is the
            SOLE cover of some sample must stay.

            The replacement cover must be within a STEP of the sample's own
            height (or of this triangle's, when the pin carries no z).  An
            80u window was tried and let a stacked cave ledge 50-70u below
            count as cover — the cull then ate the real ledge and BarrenCave
            lost 39 walked-line samples.
            """
            for (px, py, pz) in samples:
                zref = pz if pz is not None else _z_at(ti, px, py)
                ok = False
                for tj in tgrid.get((int(px // tcell), int(py // tcell)), ()):
                    if tj == ti or not alive[tj]:
                        continue
                    z = _z_at(tj, px, py)
                    if z is not None and (zref is None
                                          or abs(z - zref)
                                          <= params.MAX_CLIMB + 8.0):
                        ok = True
                        break
                if not ok:
                    return False
            return True

        def _cov_z(x, y, zc, skip=None):
            best = None
            for tj in tgrid.get((int(x // tcell), int(y // tcell)), ()):
                if tj == skip or not alive[tj]:
                    continue
                z = _z_at(tj, x, y)
                if z is not None and abs(z - zc) <= 40.0:
                    if best is None or abs(z - zc) < abs(best - zc):
                        best = z
            return best

        def _width_at(px, py, pz, pu):
            """Continuous covered cross-width at a directed line sample."""
            wx_, wy_ = -pu[1], pu[0]
            width = 4.0
            for sgn in (1, -1):
                zc = pz
                for s in range(1, 16):
                    z = _cov_z(px + sgn * wx_ * s * 4.0,
                               py + sgn * wy_ * s * 4.0, zc)
                    if z is None:
                        break
                    zc = z
                    width += 4.0
            return width

        def _narrows_corridor(t):
            """Would culling t squeeze a nearby walked line under the
            walkable-width contract (~half a doorway)?

            Only samples whose corridor is ALREADY tight protect their
            fringe; in a wide room the same fringe triangle culls freely.
            The width is re-measured against the LIVE mesh, so a chain of
            culls stops the moment the contract is at risk.
            """
            r = 44.0
            pts = [verts[i] for i in t]
            cx = sum(p[0] for p in pts) / 3.0
            cy = sum(p[1] for p in pts) / 3.0
            cz = sum(p[2] for p in pts) / 3.0
            for (px_, py_, pz_) in (pts + [(cx, cy, cz)]):
                for gx in range(int((px_ - r) // pcell),
                                int((px_ + r) // pcell) + 1):
                    for gy in range(int((py_ - r) // pcell),
                                    int((py_ + r) // pcell) + 1):
                        for (qx, qy, qz, qu) in pgrid.get((gx, gy), ()):
                            if qu is None:
                                continue
                            if ((qx - px_) ** 2 + (qy - py_) ** 2 > r * r
                                    or (qz is not None
                                        and abs(qz - pz_) > 60.0)):
                                continue
                            if _width_at(qx, qy,
                                         qz if qz is not None else pz_,
                                         qu) < 56.0:
                                return True
            return False

        changed = False
        for (_r, ti, area, samples) in cands:
            if not alive[ti]:
                continue
            if removed_area + area > budget:
                continue
            if samples and not _samples_still_covered(ti, samples):
                continue                    # sole cover of the walked line
            if _narrows_corridor(tris[ti]):
                continue                    # corridor width is a contract
            nbrs = _neighbours(ti)
            if len(nbrs) > 1:
                target = set(nbrs[1:])
                seen_t = {nbrs[0], ti}
                queue = [nbrs[0]]
                fuel = 128
                while queue and target and fuel:
                    fuel -= 1
                    cur = queue.pop()
                    for tj in _neighbours(cur):
                        if tj in seen_t:
                            continue
                        seen_t.add(tj)
                        target.discard(tj)
                        queue.append(tj)
                if target:
                    continue                # sliver is a bridge: keep it
            alive[ti] = False
            removed_area += area
            changed = True
        tris = [t for ti, t in enumerate(tris) if alive[ti]]
        if not changed:
            break
    return tris


def _flip_pass(verts, tris, pin, rounds=3):
    """Lawson-style diagonal flips that strictly improve edge ratio.

    For an interior edge (a, b) shared by exactly two triangles (a,b,c) and
    (b,a,d): when the plan quad c-a-d-b is strictly convex, replacing the
    diagonal (a,b) with (c,d) re-meshes the same ground with the same four
    vertices.  Guards: the new diagonal must not span more height than the
    old one allowed (a flip across a fold would bridge two levels), triangles
    whose three corners are all door-pinned are never touched (the Door
    Triangle's shape is a contract), and the flip must strictly reduce the
    worst edge ratio of the pair.  Returns the new list, or None if nothing
    flipped.
    """
    from . import params

    def _ratio(a, b, c):
        return _badness(verts, (a, b, c))

    def _area2(a, b, c):
        p, q, r = verts[a], verts[b], verts[c]
        return ((q[0] - p[0]) * (r[1] - p[1]) -
                (q[1] - p[1]) * (r[0] - p[0]))

    any_flip = False
    for _ in range(rounds):
        edge_tris = {}
        for ti, t in enumerate(tris):
            for k in range(3):
                a, b = t[k], t[(k + 1) % 3]
                edge_tris.setdefault((a, b) if a < b else (b, a),
                                     []).append(ti)
        done = set()
        new_edges = set()
        changed = False
        for key in sorted(edge_tris):
            owners = edge_tris[key]
            if len(owners) != 2:
                continue
            ti, tj = owners
            if ti in done or tj in done:
                continue
            t1, t2 = tris[ti], tris[tj]
            a, b = key
            c = next((v for v in t1 if v != a and v != b), None)
            d = next((v for v in t2 if v != a and v != b), None)
            if c is None or d is None or c == d:
                continue
            # The new diagonal must not ALREADY be an edge somewhere else in
            # the mesh (possible where storeys fold and reuse vertices):
            # flipping onto it would give that edge 3+ owners, and
            # _make_manifold later rips the extra triangles out with no
            # connectivity guard — measured as whole regions detaching in
            # ChorrolFightersGuild and BarrenCave.
            ckey = (c, d) if c < d else (d, c)
            if ckey in edge_tris or ckey in new_edges:
                continue
            if (all(v in pin for v in t1) or all(v in pin for v in t2)):
                continue                    # door triangles keep their shape
            worst_old = max(_ratio(*t1), _ratio(*t2))
            worst_new = max(_ratio(c, d, a), _ratio(c, d, b))
            if worst_new >= worst_old - 1e-9:
                continue
            # The new diagonal may not climb more than the old one did (plus
            # a step): flipping across a fold bridges two walkable levels.
            dz_old = abs(verts[a][2] - verts[b][2])
            if abs(verts[c][2] - verts[d][2]) > dz_old + params.MAX_CLIMB:
                continue
            # Strict convexity: a and b must sit on OPPOSITE sides of the new
            # diagonal c-d, each a non-degenerate distance off it — on a
            # non-convex quad they land on the same side and the flip would
            # fold the quad over itself.  Winding follows from the side.
            s_a = _area2(c, d, a)
            s_b = _area2(c, d, b)
            if s_a * s_b >= 0 or abs(s_a) <= 1e-6 or abs(s_b) <= 1e-6:
                continue
            tris[ti] = (c, d, b) if s_b > 0 else (d, c, b)
            tris[tj] = (c, d, a) if s_a > 0 else (d, c, a)
            new_edges.add(ckey)
            done.add(ti)
            done.add(tj)
            changed = True
            any_flip = True
        if not changed:
            break
    return tris if any_flip else None


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


def find_ledge_links(verts, tris):
    """Find DROP-DOWNs between components: [(tri_hi, tri_lo, drop), ...].

    Oblivion expressed a drop-down as two disconnected pathgrid islands — the
    actor steps off a ledge, and there is no pathgrid edge for that.  The
    navmesh mirrors the pathgrid, so those storeys arrive as separate
    components and an NPC pathing across has no route.

    Skyrim's own mechanism for this is an NVNM **Edge Link** of type
    `Ledge Down` (2) from the upper triangle and `Ledge Up` (1) from the lower
    one — NOT geometry.  Vanilla Skyrim.esm carries 476 Ledge Down and 467
    Ledge Up links across 3,000 navmeshes (near-symmetric pairs), and zero
    bridging triangles: an actor DROPS through empty space, so inventing
    walkable ground across the lip both lets NPCs walk on air and, because the
    quad is near-vertical, breeds downfacing/opposite-normal triangles.

    This only DETECTS the pairs; `pgrd_to_navm` writes the links.  Both sides
    must ALREADY be separate components, which is what keeps it safe: stairs,
    ramps and genuinely-connected storeys are one component and never reach
    here.  A pair qualifies only when its boundaries come within
    ISLAND_BRIDGE_XY in plan AND are separated by a drop between
    ISLAND_BRIDGE_MIN_DROP and ISLAND_BRIDGE_MAX_DROP, so a fall the actor
    cannot survive is left alone.
    """
    if len(tris) == 0:
        return []
    tris = [list(map(int, t)) for t in tris]
    comps = components(tris)
    if len(comps) < 2:
        return []

    # triangle index -> component index, so a boundary edge names its triangle
    tri_comp = {}
    for ci, comp in enumerate(comps):
        for ti in comp:
            tri_comp[ti] = ci

    def _edge_tri(ti_set, a, b):
        """The triangle in this component owning boundary edge (a, b)."""
        for ti in ti_set:
            t = tris[ti]
            for k in range(3):
                if {t[k], t[(k + 1) % 3]} == {a, b}:
                    return ti
        return None

    bounds = [_boundary_edges(tris, c) for c in comps]
    out = []
    for i in range(len(comps)):
        for j in range(i + 1, len(comps)):
            # EVERY qualifying edge pair along the lip, not just the single
            # closest.  A vanilla balcony carries a ledge link on several
            # triangles along its edge (Skyrim.esm census: half of all
            # mesh->target ledge pairs have 2-12+ links, owning-triangle
            # median area 9,398, linked-edge median 135u), so an actor can
            # drop from wherever it stands.  With only one link on one small
            # triangle, actors whose path met the lip elsewhere never took
            # the drop — the CharacterGen assassins stood at the balcony edge
            # and stayed there.
            cands = []
            for (a1, b1) in bounds[i]:
                p1, q1 = verts[a1], verts[b1]
                for (a2, b2) in bounds[j]:
                    p2, q2 = verts[a2], verts[b2]
                    # Endpoint pairing tried both ways: the two boundaries
                    # wind in opposite directions, so the matching endpoints
                    # are as often swapped as not.
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
            # Nearest pairs first; ONE link per triangle so the links spread
            # along the lip instead of stacking on the same edge (each NVNM
            # triangle has three link slots, but one slot per lip triangle is
            # the vanilla shape).  Deterministic: sorted by (dxy, vertex ids).
            cands.sort(key=lambda c: (c[0], c[1], c[2]))
            used = set()
            made = 0
            for (_dxy, (a1, b1), (a2, b2), z1, z2, drop) in cands:
                if made >= params.LEDGE_LINKS_PER_PAIR:
                    break
                t1 = _edge_tri(comps[i], a1, b1)
                t2 = _edge_tri(comps[j], a2, b2)
                if t1 is None or t2 is None or t1 in used or t2 in used:
                    continue
                used.add(t1)
                used.add(t2)
                made += 1
                # (upper triangle, lower triangle, drop) — the caller writes
                # Ledge Down on the upper and Ledge Up on the lower.
                if z1 >= z2:
                    out.append((t1, t2, drop))
                else:
                    out.append((t2, t1, drop))
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
