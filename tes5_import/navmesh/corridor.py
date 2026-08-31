"""Phase-1 corridor-ribbon navmesh generation.

THE MODEL, in one line:

    THE PATHGRID IS THE MESH.

Bethesda's pathgrid is the only part of the input that ASSERTS "an actor walks
here".  Instead of re-discovering walkable surface from collision (voxelize /
contour / region-flood) and then fighting to keep the result connected across
the seams that discovery introduces, we build the navmesh DIRECTLY on the
pathgrid: a flat, fixed-width ribbon of triangles centred on every pathgrid
edge.

Ribbons on a dense pathgrid overlap heavily (a node can carry 9 edges, and a
median edge is shorter than two ribbon widths), so they are not simply laid on
top of each other: corridor_union takes the boolean UNION of the ribbon polygons
and retriangulates it, per walkable surface.  The union is coverage-preserving
and non-overlapping by construction (see corridor_union), so the result is a
single connected sheet covering the pathgrid with zero stacked triangles.

The result is deliberately SPARSE — a corridor an actor can follow, not a
room-filling floor.  A completely functional, zero-bad-triangle navmesh that is
a bit narrow beats a dense one that is broken.  Width-grow (fill out to the
walls) is a later phase; this one gets the corridors + doors + links right.

Design principles (see docs/commentary/tes5_import_navmesh.md):
  1. The pathgrid CENTERLINE is sacred — never cut or moved, even where it
     clips a wall.  Only grown width (a later phase) may ever be clipped.
  2. Downward snap follows the pathgrid LINE'S OWN SLOPE.  A pathgrid edge
     A->B already IS the walk ramp (Oblivion places stair nodes at tread
     level).  We sit the ribbon on that straight line and only ever push a
     cross-section DOWN onto collision when the line floats above it — never
     let jagged treads push it up and reintroduce a sawtooth.  Slope stays
     slope.  Phase 1 keeps the corridor FLAT across its width.
  3. Conservative: when unsure, stop.  Doorways are assumed to already have
     pathgrid through them.

Output contract (identical to the old build_navmesh): a manifold (verts, tris)
where every edge is shared by <= 2 triangles — a 3+-shared edge silently
disconnects everything around it under _compute_adjacency.
"""

import math

import numpy as np

from . import corridor_grow, params, world

# Trim node-disc rays at stair nodes so the FLAT disc never rides out over a
# descending flight (see the disc loop in _build_corridor_strips).  Module
# flag so diagnostics can A/B it.
DISC_RAY_TRIM = True


# ---------------------------------------------------------------------------
# Walkable surface sampler (the only collision query Phase 1 needs)
# ---------------------------------------------------------------------------

def _surface_sampler(walkable):
    """f(x, y, near_z) -> walkable-collision height at (x,y) nearest near_z, or
    None.  Point-in-triangle over the walkable soup, bucketed into a coarse XY
    grid so each query only tests nearby triangles.
    """
    W = np.asarray(walkable, dtype=float).reshape(-1, 3, 3)
    if not len(W):
        return None
    cell = 128.0
    minx = float(W[:, :, 0].min())
    miny = float(W[:, :, 1].min())
    grid = {}
    for i, tri in enumerate(W):
        gx0 = int((tri[:, 0].min() - minx) // cell)
        gx1 = int((tri[:, 0].max() - minx) // cell)
        gy0 = int((tri[:, 1].min() - miny) // cell)
        gy1 = int((tri[:, 1].max() - miny) // cell)
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                grid.setdefault((gx, gy), []).append(i)

    def _heights(x, y):
        gx = int((x - minx) // cell)
        gy = int((y - miny) // cell)
        out = []
        for i in grid.get((gx, gy), ()):
            a, b, c = W[i]
            d = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
            if abs(d) < 1e-6:
                continue
            l0 = ((b[1] - c[1]) * (x - c[0]) + (c[0] - b[0]) * (y - c[1])) / d
            l1 = ((c[1] - a[1]) * (x - c[0]) + (a[0] - c[0]) * (y - c[1])) / d
            l2 = 1.0 - l0 - l1
            if l0 < -0.02 or l1 < -0.02 or l2 < -0.02:
                continue
            out.append(l0 * a[2] + l1 * b[2] + l2 * c[2])
        return out

    def sample(x, y, near_z):
        best = None
        for z in _heights(x, y):
            if best is None or abs(z - near_z) < abs(best - near_z):
                best = z
        return best

    def layers(x, y):
        """Every distinct walkable height at (x, y), ascending (2u dedupe)."""
        zs = sorted(_heights(x, y))
        out = []
        for z in zs:
            if not out or z - out[-1] > 2.0:
                out.append(z)
        return out

    sample.layers = layers
    return sample


def _snap_node_z(sample, x, y, z):
    """Node Z snapped DOWN onto walkable collision (principle 2).

    The pathgrid hovers above the walked surface, and the navmesh must sit ON
    it.  Snap toward the surface only within a plausible window; never teleport
    to a distant floor, never rise onto an object standing on the floor.
    """
    if sample is None:
        return z
    s = sample(x, y, z)
    if s is None:
        return z                                   # trust the pathgrid
    if s <= z + params.SEED_Z_TOLERANCE and s >= z - params.SEED_SNAP:
        return s                                   # within window: sit on it
    if s < z:
        return z - params.SEED_SNAP                # far below: clamp the drop
    return z                                       # surface above node: stay


# ---------------------------------------------------------------------------
# Ribbon generation
# ---------------------------------------------------------------------------

def _plan_stations(nodes, edges, node_z, degree, grow):
    """Every march station the grow needs, as a plan the native batch consumes.

    Returns (stations, plan) where `stations` is an (N, 9) float64 array
    (cx, cy, cz, dirx, diry, tanx, tany, lo, edge_index) and `plan` records how
    to reassemble the results:

        ('edge', (i, j), pa, pb, u, w, length, k, base)   k+1 stations per side
        ('disc', ni, nx, ny, nz, base)                    DISC_RAYS stations

    Splitting planning from marching is what lets the ~890k probes for a dense
    cell cross the Python/C boundary ONCE instead of once each.  The geometry
    each station measures against is fixed, so batching cannot change any
    result -- the march was already order-independent by design.
    """
    ext = params.RIBBON_END_EXTEND
    rows = []
    plan = []
    # edge -> index, so a station can name the endpoint pair to exclude from
    # the neighbour query without shipping node ids per row.
    edge_index = {}
    for e, (i, j) in enumerate(edges):
        edge_index[(i, j)] = e
    # node -> slot in the synthetic self-pair table appended after `edges`.
    disc_self = {}

    if not grow:
        return np.zeros((0, 9), dtype=np.float64), plan, []

    for (i, j) in edges:
        if i >= len(nodes) or j >= len(nodes) or i == j:
            continue
        ax, ay = nodes[i][0], nodes[i][1]
        bx, by = nodes[j][0], nodes[j][1]
        az, bz = node_z[i], node_z[j]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length < 1e-4:
            continue
        ux, uy = dx / length, dy / length
        wx, wy = -uy, ux
        dz = bz - az
        ea = ext if degree.get(i, 0) <= 1 else 0.0
        eb = ext if degree.get(j, 0) <= 1 else 0.0
        pa = (ax - ux * ea, ay - uy * ea, az - dz * (ea / length))
        pb = (bx + ux * eb, by + uy * eb, bz + dz * (eb / length))
        if abs(pb[2] - pa[2]) / max(length, 1e-6) > params.RIBBON_GROW_MAX_SLOPE:
            continue                      # steep: keeps Phase-1 width, no march

        total = length + ea + eb
        k = max(1, int(round(total / params.RIBBON_STEP)))
        ramp = params.RIBBON_HALF_WIDTH
        lo0 = params.RIBBON_HALF_WIDTH
        lo1 = params.RIBBON_GROW_MIN_HALF
        ei = edge_index.get((i, j), -1)
        base = len(rows)
        for s in range(k + 1):
            t = s / k
            cxs = pa[0] + (pb[0] - pa[0]) * t
            cys = pa[1] + (pb[1] - pa[1]) * t
            czs = pa[2] + (pb[2] - pa[2]) * t
            d_end = min(t, 1.0 - t) * total
            frac = min(1.0, d_end / ramp) if ramp > 1e-6 else 1.0
            floor_h = lo0 + (lo1 - lo0) * frac
            # left (+w) then right (-w), so results interleave predictably.
            rows.append((cxs, cys, czs, wx, wy, ux, uy, floor_h, ei))
            rows.append((cxs, cys, czs, -wx, -wy, ux, uy, floor_h, ei))
        plan.append(('edge', (i, j), pa, pb, (ux, uy), (wx, wy),
                     length, k, base))

    # NODE DISCS -- radial fan filling the outer corner at each junction.
    #
    # A node touching a STEEP edge used to be excluded from this fan.  That was a
    # blanket rule with no stated justification, and it is what left the corner at
    # the top of a staircase DEAD: measured on Pinarus, nodes 0 and 1 (the stair's
    # two endpoints) were the ONLY nodes in the cell without a disc — every other
    # node, 2 through 38, had one.  The upper floor's ribbon coverage therefore
    # stopped at y=146 with the corner beyond it unmeshed, and the union bridged
    # the gap with a single tilted triangle that flapped 38.6u under the landing —
    # the sole, unnavigable link between the two floors.
    #
    # The disc is a FLAT radial fan at the node's own height, which is precisely
    # what a stair top needs: the landing there IS flat.  It cannot spill over the
    # stairwell either, because each ray marches against real collision and stops
    # at the drop (verified: a ray heading out over the void reads the floor below
    # and terminates).  So there is no reason to exclude these nodes, and excluding
    # them removes the fan exactly where the geometry most needs it.
    nrays = params.RIBBON_GROW_DISC_RAYS
    for ni in sorted(degree):
        if ni >= len(nodes):
            continue
        nx, ny = nodes[ni][0], nodes[ni][1]
        nz = node_z[ni]
        base = len(rows)
        # The disc excludes only its OWN node.  There is no real edge (ni, ni)
        # to point at, so a synthetic self-pair is appended to the edge table
        # the native side receives (see `extra_edges` below) and indexed here.
        ei = len(edges) + disc_self.setdefault(ni, len(disc_self))
        for kk in range(nrays):
            ang = 2.0 * math.pi * kk / nrays
            ddx, ddy = math.cos(ang), math.sin(ang)
            # Floor 0: a wall must always beat any minimum here, or the disc
            # pushes mesh through a wall standing close to the node.
            rows.append((nx, ny, nz, ddx, ddy, -ddy, ddx, 0.0, ei))
        plan.append(('disc', ni, nx, ny, nz, base))

    st = (np.asarray(rows, dtype=np.float64) if rows
          else np.zeros((0, 9), dtype=np.float64))
    # Synthetic (ni, ni) rows appended to the edge table so a disc station can
    # exclude its own node.  They are only ever read as an exclusion pair; the
    # native NeighbourField skips zero-length segments, so they add no geometry.
    extra_edges = [(ni, ni) for ni, _slot in
                   sorted(disc_self.items(), key=lambda kv: kv[1])]
    return st, plan, extra_edges


def _surface_profile(sample, pa, pb):
    """Height profile along a STEEP edge's centreline, following the real
    walkable surface the way an actor walks it.

    The pathgrid draws a straight chord from node to node, but a real
    staircase rarely descends along the whole chord: Pinarus's flight starts
    ~90u east of its top node, so the chord ran 39u BELOW the actual landing
    there, the stair ribbon reported z=30 where the real floor is 68.6, and
    the union emitted a near-vertical triangle joining the two fictions.
    (tools/navmesh_tri_check measured the same chord error as +46/-49u float
    over the whole flight.)

    The path is found as a shortest path over the WALKABLE LAYERS along the
    line — at each 16u station the candidate heights are every walkable
    surface there (within the edge's own z range), a transition between
    stations may climb at most one step, and the path must START at the near
    node's height and END at the far node's.  The end constraint is what
    selects the treads: a greedy walk anchored on the previous height was
    tried first and simply followed the GROUND FLOOR that continues UNDER the
    flight (nearest-layer at every step), ending 260u below the far node with
    a cliff at the anchor — the flight is the only layer path that actually
    arrives at the far node.  Returns None (caller keeps the chord) when no
    such path exists.

    NOTE this is NOT the reverted "re-fit the line to collision" experiment
    (_height_on's docstring): that changed the flight's overall angle.  The
    profile keeps both endpoints and the plan line; it only replaces the
    straight-line INTERPOLATION between them with the measured surface.
    """
    layers = getattr(sample, 'layers', None)
    if layers is None:
        return None
    ax, ay, az = pa
    bx, by, bz = pb
    run = math.hypot(bx - ax, by - ay)
    if run < 32.0:
        return None
    n = max(2, int(run // 16.0))
    lo = min(az, bz) - params.MAX_CLIMB
    hi = max(az, bz) + params.MAX_CLIMB
    stations = []
    for s in range(n + 1):
        t = s / n
        x = ax + (bx - ax) * t
        y = ay + (by - ay) * t
        chord = az + (bz - az) * t
        cand = [z for z in layers(x, y) if lo <= z <= hi]
        if not cand:
            cand = [chord]      # collision gap: bridge on the chord
        stations.append((x, y, cand))

    INF = float('inf')
    step = params.MAX_CLIMB
    costs = [[(abs(z - az) if abs(z - az) <= step else INF)
              for z in stations[0][2]]]
    back = []
    for s in range(1, n + 1):
        cand = stations[s][2]
        prev_c = costs[-1]
        prev_z = stations[s - 1][2]
        row = [INF] * len(cand)
        bk = [0] * len(cand)
        for i, z in enumerate(cand):
            for j, zp in enumerate(prev_z):
                if prev_c[j] == INF:
                    continue
                d = abs(z - zp)
                if d > step:
                    continue
                c = prev_c[j] + d
                if c < row[i]:
                    row[i] = c
                    bk[i] = j
        costs.append(row)
        back.append(bk)

    best = None
    for i, z in enumerate(stations[n][2]):
        if costs[n][i] == INF or abs(z - bz) > step:
            continue
        key = (abs(z - bz), costs[n][i], i)
        if best is None or key < best:
            best = key
    if best is None:
        return None                         # no layer path reaches the node
    idx = best[2]
    zs = [0.0] * (n + 1)
    for s in range(n, -1, -1):
        zs[s] = stations[s][2][idx]
        if s > 0:
            idx = back[s - 1][idx]
    pts = [(stations[s][0], stations[s][1], zs[s]) for s in range(n + 1)]
    pts[0] = (ax, ay, az)
    pts[-1] = (bx, by, bz)
    return pts


def _build_corridor_strips(nodes, edges, node_z, wall_hit=None,
                           walk_probe=None, field=None,
                           blocking=None, walkable=None, sample=None):
    """One corridor per pathgrid edge.  Returns a list of dicts, each:

        {'edge': (i, j),
         'a': (ax, ay, az), 'b': (bx, by, bz),   # centerline ends (extended)
         'u': (ux, uy), 'w': (wx, wy),           # along / perpendicular units
         'half': half,                           # MAX half-width (for lookups)
         'poly': [(x, y), ...]}                  # explicit outline (Phase 2)

    'a'/'b' are the centerline endpoints AFTER dead-end extension, carrying the
    line's own slope (principle 2).  The corridor lies FLAT on the centerline
    plane.  In Phase 1 (params.RIBBON_GROW False) it is the fixed rectangle of
    half-width RIBBON_HALF_WIDTH.  In Phase 2 each side is GROWN per cross-section
    (corridor_grow.grow_half_width) so the outline is an explicit polygon whose
    two long sides need not be parallel — wider out to the walls, narrower where
    squeezed.  Corridors are NOT yet a shared mesh — corridor_union takes their
    boolean union and retriangulates it; keeping them as parametric strips lets
    it recover each vertex's height along the centerline.

    The Phase-2 march itself runs NATIVELY and in ONE batch (see
    corridor_grow.grow_batch): planning every station first, marching them all
    in C++, then reassembling turns ~890k Python/C crossings per dense cell
    into one.  The march was already order-independent (it measures against
    fixed geometry, never against another corridor's grown width), so batching
    cannot change any result.
    """
    half = params.RIBBON_HALF_WIDTH
    ext = params.RIBBON_END_EXTEND
    grow = params.RIBBON_GROW and blocking is not None

    # Degree of every node, so only DEAD ENDS get the end extension.  Extending
    # past a node that another corridor also uses puts this corridor's stub
    # entirely inside that corridor — guaranteed double coverage at every
    # junction, and the dominant residual overlap (collinear pairs sharing a
    # node overlapped for 22 triangles each).  At a dead end there is no other
    # corridor, so the stub is the only thing reaching the wall or door ahead
    # and it costs nothing.
    degree = {}
    for (i, j) in edges:
        degree[i] = degree.get(i, 0) + 1
        degree[j] = degree.get(j, 0) + 1

    # A node where TWO OR MORE steep runs meet is a mid-flight landing, not the
    # place a flight reaches a floor.  A steep ribbon must not extend flat through
    # such a node (see below): both runs would claim the same ground at different
    # heights and tear the mesh.
    steep_count = {}
    for (i, j) in edges:
        if i >= len(nodes) or j >= len(nodes) or i == j:
            continue
        run = math.hypot(nodes[j][0] - nodes[i][0], nodes[j][1] - nodes[i][1])
        if run < 1e-4:
            continue
        if abs(node_z[j] - node_z[i]) / run > params.RIBBON_GROW_MAX_SLOPE:
            steep_count[i] = steep_count.get(i, 0) + 1
            steep_count[j] = steep_count.get(j, 0) + 1

    # ---- Phase 2 march: plan every station, run them all natively, reassemble.
    # `widths` is indexed by the `base` offsets recorded in the plan.
    stations, plan, extra_edges = _plan_stations(nodes, edges, node_z,
                                                 degree, grow)
    widths = None
    if len(stations):
        widths = corridor_grow.grow_batch(
            blocking, walkable, nodes, list(edges) + extra_edges,
            node_z, stations)

    # Edges that were PLANNED (flat enough to grow) map to their plan entry;
    # every other edge keeps the Phase-1 fixed rectangle below.
    grown_edges = {p[1]: p for p in plan if p[0] == 'edge'}

    strips = []
    for (i, j) in edges:
        if i >= len(nodes) or j >= len(nodes) or i == j:
            continue
        ax, ay = nodes[i][0], nodes[i][1]
        bx, by = nodes[j][0], nodes[j][1]
        az, bz = node_z[i], node_z[j]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length < 1e-4:
            continue
        ux, uy = dx / length, dy / length
        wx, wy = -uy, ux
        dz = bz - az
        # Ribbons run node to node; at a DEAD END the ribbon extends past the
        # node, since nothing else reaches the wall or door ahead.  Overlap
        # between ribbons is resolved by the union, so a ribbon never needs to
        # stop short of a junction.
        ea = ext if degree.get(i, 0) <= 1 else 0.0
        eb = ext if degree.get(j, 0) <= 1 else 0.0

        # A STEEP edge (a flight of stairs) also extends past its END NODES, even
        # though they are junctions.  A steep ribbon is never width-grown (see
        # below), so it keeps the narrow Phase-1 width while the flat landing it
        # meets has grown to ~100u+.  The stair mouth is then far narrower than
        # the landing, and the two only meet at the landing's CORNER vertices:
        # measured at the top of Pinarus's stairs, the entire route from the
        # landing onto the flight ran through two 27-degree wedges (one with edge
        # ratio 6.1) hanging off those corners, each dropping 39-45u.  The mesh
        # was ONE component and still not walkable.
        #
        # Extending the flight a little onto the flat at each end gives the union
        # a real overlap to work with, so the stair mouth becomes a proper span of
        # shared edges instead of a pair of needles.  The extension carries the
        # line's own slope (principle 2), so it stays on the ramp plane rather
        # than lifting onto the landing.
        steep = abs(dz) / length > params.RIBBON_GROW_MAX_SLOPE

        # NOTE: a stair-end EXTENSION was tried here (both as a sloped projection
        # and as a footprint-only overhang) and both are wrong.  Sloped, it drives
        # the ramp plane past the node — up into the air above the landing at the
        # top (measured: ramp triangles at z=93 where the landing is z=69).
        # Footprint-only, the overhang keeps interpolating the ramp slope while the
        # landing is flat, so the flight's last row tilts UP off the landing edge:
        # measured a 38.9-degree joint whose ramp apex sat 14.8u above the shared
        # edge — a connection an actor cannot cross.  A stair ribbon therefore
        # runs node to node exactly, like any other edge.
        eza, ezb = dz * (ea / length), dz * (eb / length)
        pa = (ax - ux * ea, ay - uy * ea, az - eza)
        pb = (bx + ux * eb, by + uy * eb, bz + ezb)

        strip = {
            'edge': (i, j),
            'na': (ax, ay, az), 'nb': (bx, by, bz),
            'a': pa, 'b': pb,
            'u': (ux, uy), 'w': (wx, wy), 'len': length,
        }

        # A STEEP edge is a staircase/ramp and is never grown — the ribbon is a
        # tilted plane, so a perpendicular rail immediately leaves the treads
        # (measured: the Guild's stair edge grew to 82u and put mesh through the
        # wall beside it).  _plan_stations applies the same test, so an edge
        # absent from the plan is exactly a steep or ungrown one.
        entry = grown_edges.get((i, j)) if widths is not None else None
        if entry is None:
            # A steep flight keeps a FIXED width (it is never grown, since a
            # perpendicular rail would leave the treads), but a wider one than a
            # plain corridor: it has to present a mouth comparable to the landing
            # it joins, or the two meet only at the landing's corners.
            strip['half'] = (params.RIBBON_STAIR_HALF_WIDTH if steep else half)
            # A steep edge's heights follow the REAL treads, not the chord —
            # see _surface_profile.  Only steep edges need it: a flat edge's
            # chord IS its surface.
            if steep and sample is not None:
                prof = _surface_profile(sample, pa, pb)
                if prof:
                    strip['prof'] = prof
            strips.append(strip)
            continue

        _, _, ppa, ppb, _u, _w, _len, k, base = entry
        left = []                   # (x, y) along +w
        right = []                  # (x, y) along -w
        max_h = params.RIBBON_HALF_WIDTH
        for s in range(k + 1):
            t = s / k
            cxs = ppa[0] + (ppb[0] - ppa[0]) * t
            cys = ppa[1] + (ppb[1] - ppa[1]) * t
            hl = float(widths[base + 2 * s])
            hr = float(widths[base + 2 * s + 1])
            left.append((cxs + wx * hl, cys + wy * hl))
            right.append((cxs - wx * hr, cys - wy * hr))
            max_h = max(max_h, hl, hr)
        # SIMPLIFY each rail before it becomes an outline.  The march samples a
        # width every RIBBON_STEP (8u), so a raw rail carries a vertex every 8u —
        # and _triangulate FORCES every outline corner as a Steiner point, which
        # is precisely what turns a grown room into fans of 8u slivers.  A
        # Douglas-Peucker pass keeps the shape (a wall the rail followed stays
        # straight, a corner stays a corner) with a fraction of the vertices, so
        # the hex lattice governs the interior and triangles come out near
        # equilateral.
        left = _simplify(left, params.RIBBON_RAIL_SIMPLIFY)
        right = _simplify(right, params.RIBBON_RAIL_SIMPLIFY)
        # Outline: left rail a->b, then right rail b->a.  A poly strip makes
        # corridor_union._distance_to return 0 inside it, so 'half' only needs to
        # be a positive upper bound for the level-lookup admission test.
        strip['poly'] = left + right[::-1]
        strip['half'] = max_h
        strips.append(strip)

    # NODE DISCS.  A ribbon only grows perpendicular to its OWN edge, so the
    # outer corner where two edges meet at an angle is a notch no ribbon reaches
    # — a right-angle junction leaves a square bite out of the mesh.  The disc
    # rays were marched in the same batch; close each fan into a polygon here.
    if widths is not None:
        # STEEP ribbons, for the disc clip below.  A disc is FLAT at its node's
        # height, but nothing stops its rays marching out OVER a flight of
        # stairs: the first MAX_CLIMB of drop is legitimately walkable, and
        # beyond that the treads below are walkable collision, not a wall, so
        # the ray never terminates.  The flat disc then covers ground 40u+
        # above the real surface, the level lookup answers BOTH heights there,
        # and emission bridges them with a near-vertical triangle (measured at
        # the top of ImperialDungeon01's prison staircase: disc level 513.8
        # hanging over stair ground at 457-474).
        steep_strips = []
        for s in strips:
            if s.get('len', 0.0) < 1e-6:
                continue
            if (abs(s['nb'][2] - s['na'][2]) / s['len']
                    > params.RIBBON_GROW_MAX_SLOPE):
                steep_strips.append(s)
        nrays = params.RIBBON_GROW_DISC_RAYS
        layers = getattr(sample, 'layers', None) if sample is not None else None
        for entry in plan:
            if entry[0] != 'disc':
                continue
            _, ni, nx, ny, nz, base = entry
            # RAY TRIM at stair nodes.  The march stops at walls and at sudden
            # drops, but a surface that RAMPS away descends a legal step per
            # station, so a ray at a stair-top node happily marches the whole
            # flight and the FLAT disc then covers ground 40u+ below its own
            # height (the phantom second level that emits vertical triangles —
            # see _clip_disc_against_steep).  Walk the real surface outward and
            # stop the ray where the surface has left the node's level by more
            # than a step in total.
            trim = (DISC_RAY_TRIM and layers is not None
                    and steep_count.get(ni, 0) >= 1)
            disc = []
            for kk in range(nrays):
                ang = 2.0 * math.pi * kk / nrays
                ddx, ddy = math.cos(ang), math.sin(ang)
                d = float(widths[base + kk])
                if trim and d > params.RIBBON_HALF_WIDTH:
                    zcur = nz
                    good = params.RIBBON_HALF_WIDTH
                    dd = good
                    while dd < d - 1e-6:
                        dd = min(d, dd + 8.0)
                        cand = [z for z in layers(nx + ddx * dd, ny + ddy * dd)
                                if abs(z - zcur) <= params.MAX_CLIMB]
                        if not cand:
                            # collision gap: bridge it (the march itself saw
                            # ground here), only an OFF-LEVEL surface stops us
                            good = dd
                            continue
                        zc = min(cand, key=lambda z: abs(z - zcur))
                        if abs(zc - nz) > params.MAX_CLIMB:
                            break
                        zcur = zc
                        good = dd
                    d = good
                disc.append((nx + ddx * d, ny + ddy * d))
            disc = _simplify(disc, params.RIBBON_RAIL_SIMPLIFY)
            if len(disc) < 3:
                continue
            disc = _clip_flat_poly_off_level(disc, nx, ny, nz, steep_strips)
            if len(disc) < 3:
                continue
            rmax = max(math.hypot(px - nx, py - ny) for (px, py) in disc)
            strips.append({
                'edge': (ni, ni),
                'na': (nx, ny, nz), 'nb': (nx, ny, nz),
                'a': (nx, ny, nz), 'b': (nx, ny, nz),
                'u': (1.0, 0.0), 'w': (0.0, 1.0),
                'len': max(rmax, 1.0), 'half': max(rmax, 1.0),
                'poly': disc,
            })
    return strips


def _clip_flat_poly_off_level(disc, nx, ny, nz, steep_strips):
    """Remove from a FLAT polygon (node disc, door footprint) the ground where
    a steep ribbon that MEETS it has LEFT the polygon's level by more than a
    step.

    The polygon keeps the flight's mouth (the ribbon within MAX_CLIMB of its
    own height — legitimately shared ground where the two must weld) and
    gives up everything further down/up the flight, so a flat surface can
    never hang mesh over a stairwell.  (nx, ny) anchors which piece survives
    a split.

    ANCHORING.  |dz| alone cannot tell "my own flight ramping away" from "an
    unrelated flight on another storey passing under me in plan" — and cutting
    the latter opened holes on ChorrolFightersGuild's mid-floor corridors
    (37 pathgrid samples lost).  A cut interval is therefore taken only when
    it is CONTIGUOUS along the strip with a mouth station that lies INSIDE
    this polygon: the flight genuinely joins this surface here, so the ground
    beyond the mouth is the same flight descending — while a storey-below
    flight has its mouth somewhere else in plan and never anchors.
    """
    from shapely.geometry import Polygon as _AnchP, Point as _AnchPt
    _apoly_cache = []

    def _anchor_poly():
        """Built lazily: most discs/quads have no steep strip in range."""
        if not _apoly_cache:
            try:
                ap = _AnchP(disc)
                if not ap.is_valid:
                    ap = ap.buffer(0)
                _apoly_cache.append(ap.buffer(8.0))
            except Exception:
                _apoly_cache.append(None)
        return _apoly_cache[0]

    hit = []
    for s in steep_strips:
        ax, ay, az = s['a']
        bx, by, bz = s['b']
        run = math.hypot(bx - ax, by - ay)
        if run < 1e-6:
            continue
        # Quick reject: strip nowhere near the disc.
        rmax = max(math.hypot(px - nx, py - ny) for (px, py) in disc)
        half = float(s.get('half', params.RIBBON_STAIR_HALF_WIDTH))
        dx, dy = bx - ax, by - ay
        t0 = max(0.0, min(1.0, ((nx - ax) * dx + (ny - ay) * dy)
                          / (run * run)))
        cx, cy = ax + dx * t0, ay + dy * t0
        if math.hypot(nx - cx, ny - cy) > rmax + half + 8.0:
            continue
        prof = s.get('prof')

        def _zat(t):
            if not prof:
                return az + (bz - az) * t
            # piecewise: prof points are evenly spaced along the plan line
            f = t * (len(prof) - 1)
            k = min(len(prof) - 2, max(0, int(f)))
            fr = f - k
            return prof[k][2] + (prof[k + 1][2] - prof[k][2]) * fr

        n = max(2, int(run // 8.0))
        # nz may be a constant (node discs) or a callable (sloped door quads):
        # the off-level test always compares against the flat surface's OWN
        # height at the sampled point.
        if callable(nz):
            mask = [abs(_zat(k / n)
                        - nz(ax + (bx - ax) * (k / n),
                             ay + (by - ay) * (k / n)))
                    > params.MAX_CLIMB for k in range(n + 1)]
        else:
            mask = [abs(_zat(k / n) - nz) > params.MAX_CLIMB
                    for k in range(n + 1)]
        ux, uy = dx / run, dy / run
        wx, wy = -uy, ux
        # Mouth stations (on-level) that lie INSIDE this polygon anchor the
        # flight to this surface; without one the strip is another storey.
        anchored = set()
        ap = _anchor_poly()
        if ap is None:
            continue
        for k in range(n + 1):
            if mask[k]:
                continue
            px_ = ax + dx * (k / n)
            py_ = ay + dy * (k / n)
            try:
                if ap.contains(_AnchPt(px_, py_)):
                    anchored.add(k)
            except Exception:
                pass
        if not anchored:
            continue
        k = 0
        while k <= n:
            if not mask[k]:
                k += 1
                continue
            k2 = k
            while k2 + 1 <= n and mask[k2 + 1]:
                k2 += 1
            # Contiguity: the off-level run must border an anchored mouth
            # station, or it belongs to a flight that never joins this
            # surface here.
            if not ((k - 1) in anchored or (k2 + 1) in anchored):
                k = k2 + 1
                continue
            d0, d1 = run * k / n, run * k2 / n
            if d1 - d0 > 1.0:
                hit.append(((ax + ux * d0 + wx * half, ay + uy * d0 + wy * half),
                            (ax + ux * d0 - wx * half, ay + uy * d0 - wy * half),
                            (ax + ux * d1 - wx * half, ay + uy * d1 - wy * half),
                            (ax + ux * d1 + wx * half, ay + uy * d1 + wy * half)))
            k = k2 + 1
    if not hit:
        return disc
    try:
        from shapely.geometry import Polygon as _SP, Point as _SPt
        from shapely.ops import unary_union as _uu
        dp = _SP(disc)
        if not dp.is_valid:
            dp = dp.buffer(0)
        cut = dp.difference(_uu([_SP(q) for q in hit]))
        if cut.is_empty:
            return disc
        pieces = list(cut.geoms) if hasattr(cut, 'geoms') else [cut]
        pieces = [g for g in pieces if g.geom_type == 'Polygon'
                  and g.area > 1.0]
        if not pieces:
            return disc
        node = _SPt(nx, ny)
        # Keep the piece the node stands on (the node itself is never inside a
        # subtracted region: |z - nz| is ~0 there).
        best = min(pieces, key=lambda g: g.distance(node))
        ring = list(best.exterior.coords)
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring = ring[:-1]
        return ring
    except Exception:
        return disc


def _simplify(pts, tol):
    """Douglas-Peucker on a polyline, keeping both endpoints."""
    if tol <= 0.0 or len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 <= i0 + 1:
            continue
        ax, ay = pts[i0]
        bx, by = pts[i1]
        dx, dy = bx - ax, by - ay
        d2 = dx * dx + dy * dy
        worst = -1.0
        wi = -1
        for m in range(i0 + 1, i1):
            px, py = pts[m]
            if d2 < 1e-12:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / d2))
                d = math.hypot(px - (ax + dx * t), py - (ay + dy * t))
            if d > worst:
                worst, wi = d, m
        if worst > tol:
            keep[wi] = True
            stack.append((i0, wi))
            stack.append((wi, i1))
    return [p for p, k in zip(pts, keep) if k]


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def build_corridors(refr_recs, base_model_by_fid, get_collision, nodes, edges,
                    land_rec=None, origin_x=0.0, origin_y=0.0, doors=None,
                    door_bases=None):
    """Phase-1 corridor navmesh for one cell: (verts, tris, ledges) lists.

    doors: [(x, y, z, rot_z, is_teleport, width), ...] pivot-corrected door
        centres; width is the measured doorway span in world units.
    door_bases: low-24 DOOR base FormIDs whose refs contribute no collision
        (a panel is opened, never a wall).
    ledges: [(upper_tri, lower_tri, drop), ...] drop-down pairs between
        disconnected storeys, for NVNM Ledge Up/Down edge links.
    """
    if not nodes or not edges:
        return [], [], []

    walkable, blocking, land_walk = world.gather_cell_geometry(
        refr_recs or [], base_model_by_fid or {}, get_collision,
        land_rec=land_rec, origin_x=origin_x, origin_y=origin_y,
        split_land=True, skip_bases=door_bases)
    if land_walk is not None and len(land_walk):
        walkable = (np.concatenate([walkable, land_walk])
                    if len(walkable) else land_walk)

    sample = _surface_sampler(walkable)

    # Node heights: snap each node down onto walkable collision.
    node_z = [_snap_node_z(sample, nodes[i][0], nodes[i][1], nodes[i][2])
              for i in range(len(nodes))]

    # The Phase-2 grow builds its own indices natively (over the same fixed
    # geometry, so growth stays order-independent and byte-reproducible).  Only
    # the DOOR footprint still needs a Python-side wall test — it runs a few
    # probes per door, not the ~890k the width march does, so it is not worth
    # crossing into C++ for.
    #
    # Built LAZILY: indexing the blocking soup costs ~0.4s on a dense cell, and
    # a cell with no doors never asks a single question of it.  Once the grow
    # went native that build was the second-largest remaining cost, spent
    # entirely on an object most cells discard unused.
    _wall_hit_cache = []

    def wall_hit(*a, **kw):
        if not _wall_hit_cache:
            _wall_hit_cache.append(corridor_grow.wall_slab_sampler(blocking))
        return _wall_hit_cache[0](*a, **kw)

    from . import corridor_doors, corridor_clean, corridor_union

    # One corridor (a rectangular ribbon on the pathgrid line's own slope) per
    # edge, then a BOOLEAN UNION of those ribbons per storey, retriangulated.
    #
    # The union is coverage-preserving by construction: its area is exactly the
    # ground the ribbons cover, and a triangulation of it cannot self-overlap.
    # Cutting the ribbons pairwise instead (trim, weld, patch the junction) is an
    # approximation that has to handle every configuration — end-to-end,
    # crossing, wedge, collinear — and every case it got wrong appeared as lost
    # ground or stacked sheets.
    #
    # Storeys are grouped by SHARED NODES with agreeing heights, so a staircase
    # stays one storey with the floors it joins while two floors stacked in plan
    # view are unioned separately and never flattened together.
    corridors = _build_corridor_strips(nodes, edges, node_z,
                                       blocking=blocking, walkable=walkable,
                                       sample=sample)

    # Exterior meshes are clipped to their own cell rectangle so a cross-seam
    # ribbon (built from a PGRI InterCell link, which reaches into the neighbour
    # cell) stops exactly at the boundary plane — leaving a border edge on the
    # seam for build_edge_links to stitch, without importing neighbour geometry.
    cell_clip = None
    if land_rec is not None:
        cell_clip = (origin_x, origin_y, origin_x + 4096.0, origin_y + 4096.0)

    # Doors are computed FIRST, on the raw ribbon union: each door's footprint is
    # the RECTANGLE sweeping its base line to the nearest reachable corridor.
    #
    # The rectangle joins the union as ordinary ground, and its BASE LINE is
    # handed over as a triangulation CONSTRAINT.  The door mesh must stay part of
    # the one union — cutting the rectangle out and emitting its triangles
    # separately leaves them sharing no vertices with the surrounding mesh (the
    # union's own boundary around the hole is sampled independently), which is an
    # overlap-and-disconnect, not a fix.
    # NOTE: splitting the union along wall footprints was tried and reverted.
    # Walls are Z-dependent but the union is ONE 2D operation spanning every
    # storey, so cutting on all wall footprints fragmented the polygon against
    # walls belonging to other floors (Pinarus: 575 -> 908 triangles and MORE
    # wall crossings, not fewer).  Per-storey handling is needed instead.
    wall_cut = None

    door_list = [(x, y, z, r, tp, w) for (x, y, z, r, tp, w) in (doors or ())]
    door_strips = []
    door_edges = []
    door_pins = []
    if door_list:
        # probe_only: this mesh feeds door_footprints and is then DISCARDED --
        # the real union below rebuilds it with the door quads included.  The
        # probe needs coverage, heights and welded edges; it does not need the
        # connectivity repair passes (see build_union_mesh).
        rv, rt = corridor_union.build_union_mesh(corridors,
                                                 cell_bounds=cell_clip,
                                                 wall_cut=wall_cut,
                                                 probe_only=True)
        if rt:
            # STEEP ribbons, to clip flat door footprints against.  A door at
            # the top of a staircase sweeps its footprint toward the nearest
            # corridor mesh, which is the FLIGHT below it — the flat quad then
            # covers ramping ground 40u+ under its own height, the level
            # lookup answers both heights there, and emission bridges them
            # with a near-vertical triangle (measured at the top of
            # ImperialDungeon01's prison stairs: door quad at 513.8 hanging
            # over stair ground at 457-474).  The clip keeps the quad down to
            # where the flight is within a step of the door's level, which is
            # exactly where the two must weld.
            steep_list = [s for s in corridors
                          if s.get('len', 0.0) > 1e-6
                          and abs(s['nb'][2] - s['na'][2]) / s['len']
                          > params.RIBBON_GROW_MAX_SLOPE]
            for fp in corridor_doors.door_footprints(rv, rt, door_list,
                                                     wall_hit=wall_hit,
                                                     nodes=nodes,
                                                     pg_edges=edges):
                poly = fp['poly']
                # The quad RAMPS from the threshold (z, at the base line) to
                # the corridor mesh under its far edge (z_far) — see
                # corridor_doors._sweep.  Both the off-level clip and the
                # strip's height axis use that slope.
                zb = float(fp['z'])
                zf = float(fp.get('z_far', fp['z']))
                bmx = 0.5 * (poly[0][0] + poly[1][0])
                bmy = 0.5 * (poly[0][1] + poly[1][1])
                fmx = 0.5 * (poly[2][0] + poly[3][0])
                fmy = 0.5 * (poly[2][1] + poly[3][1])
                sweep = math.hypot(fmx - bmx, fmy - bmy)
                # The ramp may only slope as steeply as ground an actor can
                # walk (the steepest real stair at a door measures ~0.4).
                # z_far comes from a mesh probe with a storey-scale tolerance,
                # so a doorway over a stacked lower floor can grab the WRONG
                # storey — the quad then paints a 45-degree cliff across the
                # corridor and the degenerate/wall culls tear real coverage
                # out with it (measured on Moranda02 nodes 40/41/57).
                if abs(zf - zb) > 0.5 * max(sweep, 1.0):
                    zf = zb

                def _qz(px, py, bmx=bmx, bmy=bmy, fmx=fmx, fmy=fmy,
                        zb=zb, zf=zf, sweep=sweep):
                    if sweep < 1e-6:
                        return zb
                    t = (((px - bmx) * (fmx - bmx) + (py - bmy) * (fmy - bmy))
                         / (sweep * sweep))
                    return zb + (zf - zb) * max(0.0, min(1.0, t))

                if steep_list and len(poly) >= 3:
                    if fp['base'] is not None:
                        ax_ = 0.5 * (fp['base'][0][0] + fp['base'][1][0])
                        ay_ = 0.5 * (fp['base'][0][1] + fp['base'][1][1])
                    else:
                        ax_ = sum(p[0] for p in poly) / len(poly)
                        ay_ = sum(p[1] for p in poly) / len(poly)
                    poly = _clip_flat_poly_off_level(poly, ax_, ay_,
                                                     _qz, steep_list)
                if len(poly) < 3:
                    continue
                ps = corridor_union._poly_strip(poly, zb)
                if abs(zf - zb) > 1.0 and sweep > 1e-6:
                    ux_, uy_ = (fmx - bmx) / sweep, (fmy - bmy) / sweep
                    ps['a'] = (bmx, bmy, zb)
                    ps['b'] = (fmx, fmy, zf)
                    ps['na'], ps['nb'] = ps['a'], ps['b']
                    ps['u'] = (ux_, uy_)
                    ps['w'] = (-uy_, ux_)
                    ps['len'] = sweep
                    ps['half'] = max(float(ps['half']), sweep) + 8.0
                door_strips.append(ps)
                # Far-side quads (interior doors) carry no base constraint —
                # they are plain ground; ONE Door Triangle per door, on the
                # primary side.  The entry is (base0, base1, apex, storey_z):
                # the door triangle's exact shape, fixed by corridor_doors,
                # plus the height of the corridor it bridges to so the claim
                # in build_union_mesh can pick the right SHEET (two stacked
                # floors both pass a 2D containment test).
                if fp['base'] is not None:
                    door_edges.append((fp['base'][0], fp['base'][1],
                                       fp['apex'], fp['z']))
                    # PIN the wedge's ring through the cleanup passes: base
                    # corners, base midpoint and apex.  Where a door is wider
                    # than the ribbon crossing it, the ground beside the
                    # reserved wedge is thin crumb geometry that decimation
                    # eats — taking the hole-ring vertices with it, so the
                    # attach found nothing within snap range and withdrew the
                    # door triangle (measured on the CharacterGen pen gate).
                    (b0, b1), apex, fz = fp['base'], fp['apex'], fp['z']
                    door_pins.extend((
                        (b0[0], b0[1], fz), (b1[0], b1[1], fz),
                        (0.5 * (b0[0] + b1[0]), 0.5 * (b0[1] + b1[1]), fz),
                        (apex[0], apex[1], fz)))

    verts, tris = corridor_union.build_union_mesh(
        corridors, extra_strips=door_strips, door_edges=door_edges,
        cell_bounds=cell_clip, wall_cut=wall_cut)
    if not tris:
        return [], [], []

    cs = params.CS_EXTERIOR if land_rec is not None else params.CS
    # For dropping unreachable fringe scraps, a component is KEPT when it can
    # reach another cell — via a door, or (exterior) by touching the cell
    # border where a worldspace edge-link continues it.  Pass the door centres
    # and, for an exterior cell, its world-space bounds.
    door_xy = [(x, y, z) for (x, y, z, r, tp, w) in door_list]
    cell_bounds = None
    if land_rec is not None:
        cell_bounds = (origin_x, origin_y, origin_x + 4096.0, origin_y + 4096.0)
    # Pin the mesh over STEEP (stair/ramp) centrelines through decimation.  Such
    # a ribbon keeps only the narrow Phase-1 width, so an edge collapse can eat
    # it outright — measured on exterior grid(-48,-8), where all four steep
    # hillside edges lost their mesh entirely (4/4 midpoints covered before
    # decimation, 0/4 after) while every flat corridor was unaffected.
    # Every pathgrid centreline is sampled: the samples both PIN the mesh over a
    # steep ribbon through decimation and mark a component as pathgrid-carrying
    # so the island pass can never drop it (the pathgrid asserts an actor walks
    # there, so that ground is reachable by definition).
    pin_xy = list(door_xy) + door_pins
    for (i, j) in edges:
        if i >= len(nodes) or j >= len(nodes) or i == j:
            continue
        run = math.hypot(nodes[j][0] - nodes[i][0], nodes[j][1] - nodes[i][1])
        if run < 1e-6:
            continue
        ux_ = (nodes[j][0] - nodes[i][0]) / run
        uy_ = (nodes[j][1] - nodes[i][1]) / run
        steps = max(2, int(run // params.RIBBON_STEP))
        for s in range(steps + 1):
            f = s / steps
            # (x, y, z, ux, uy): the direction lets the sliver cull measure
            # the corridor's CROSS-WIDTH at this sample (the walkable-width
            # contract) — consumers that only read x/y/z are unaffected.
            pin_xy.append((nodes[i][0] + (nodes[j][0] - nodes[i][0]) * f,
                           nodes[i][1] + (nodes[j][1] - nodes[i][1]) * f,
                           node_z[i] + (node_z[j] - node_z[i]) * f,
                           ux_, uy_))

    verts, tris, ledge_marks = corridor_clean.finalize(
        verts, tris, cs=cs, doors=door_xy, cell_bounds=cell_bounds,
        pin_xy=pin_xy, door_pins=door_pins,
        node_pins=[(nodes[i][0], nodes[i][1]) for i in range(len(nodes))])

    verts = [tuple(float(c) for c in v) for v in verts]
    tris = [tuple(int(i) for i in t) for t in tris]


    # DROP ATTACH-ERA SCRAPS.  The island cull ran inside finalize, BEFORE the
    # door attach; de-stacking there can orphan a mesh triangle whose only
    # edge-neighbours were removed, leaving 1-2 triangle specks the engine can
    # never route onto (measured: one each in Pinarus, ChorrolFG, AnvilFG).
    # A speck that carries a door's threshold is kept — it IS that door's
    # triangle and the door link needs it.
    comps = corridor_clean.components([list(map(int, t)) for t in tris])
    if len(comps) > 1:
        drop = set()
        for comp in comps:
            if len(comp) > 2:
                continue
            has_door = False
            for ti in comp:
                a, b, c = (verts[i] for i in tris[ti])
                for (px, py, pz) in door_xy:
                    d = ((b[1] - c[1]) * (a[0] - c[0])
                         + (c[0] - b[0]) * (a[1] - c[1]))
                    if abs(d) < 1e-9:
                        continue
                    l0 = ((b[1] - c[1]) * (px - c[0])
                          + (c[0] - b[0]) * (py - c[1])) / d
                    l1 = ((c[1] - a[1]) * (px - c[0])
                          + (a[0] - c[0]) * (py - c[1])) / d
                    l2 = 1.0 - l0 - l1
                    if l0 >= -0.05 and l1 >= -0.05 and l2 >= -0.05 \
                            and abs(l0 * a[2] + l1 * b[2] + l2 * c[2]
                                    - pz) <= 128.0:
                        has_door = True
                        break
                if has_door:
                    break
            if not has_door:
                drop.update(comp)
        if drop:
            tris = [t for ti, t in enumerate(tris) if ti not in drop]

    # Attach can mint plan-degenerate seam slivers of its own (measured: a
    # zero-width 65u wall along ImperialDungeon01's prison-gate quad seam);
    # the finalize-era cull ran before attach, so run it once more.
    tris = corridor_clean._drop_degenerate_guarded(verts, tris)

    # NORMALISE WINDING.  The mesh is a heightfield, so every triangle must be
    # CCW in plan (Z-normal up); the engine and the CK's DOWNFACING rule both
    # read a CW triangle as a downward-facing surface.  Edge collapses in
    # decimation (and the weld) can flip a triangle's plan winding — measured
    # two CW triangles in ImperialDungeon01 once the far-side door quads
    # reshaped the local triangulation.  Orientation is a per-triangle
    # property; flipping is always safe here.
    tris = [((t[0], t[2], t[1])
             if ((verts[t[1]][0] - verts[t[0]][0])
                 * (verts[t[2]][1] - verts[t[0]][1])
                 - (verts[t[2]][0] - verts[t[0]][0])
                 * (verts[t[1]][1] - verts[t[0]][1])) < 0 else t)
            for t in tris]

    # Resolve drop-down pairs to FINAL triangle indices only now — the attach
    # can both append (door + stitch fills) and remove (de-stacked overlap)
    # triangles, so any index resolved earlier would be stale.
    ledges = corridor_clean._resolve_ledges(verts, tris, ledge_marks)

    return (verts, tris,
            [(int(a), int(b), float(d)) for (a, b, d) in ledges])
