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

def _height_grid(walkable, cell=128.0):
    """(triangles, grid, minx, miny) bucketing a walkable soup by plan cell."""
    W = np.asarray(walkable, dtype=float).reshape(-1, 3, 3)
    if not len(W):
        return None, None, 0.0, 0.0
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
    return W, grid, minx, miny


def _bucket_heights(W, grid, minx, miny, cell, x, y):
    """Every walkable height at (x, y) from the triangles in its bucket."""
    out = []
    for i in grid.get((int((x - minx) // cell), int((y - miny) // cell)), ()):
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


def _surface_sampler(walkable):
    """f(x, y, near_z) -> walkable height at (x,y) nearest near_z, or None.

    The returned callable carries a `.layers(x, y)` attribute listing every
    distinct walkable height there, ascending, deduped at 2u.
    """
    cell = 128.0
    W, grid, minx, miny = _height_grid(walkable, cell)
    if W is None:
        return None

    def sample(x, y, near_z):
        """The walkable height at (x, y) closest to near_z, or None."""
        best = None
        for z in _bucket_heights(W, grid, minx, miny, cell, x, y):
            if best is None or abs(z - near_z) < abs(best - near_z):
                best = z
        return best

    def layers(x, y):
        """Every distinct walkable height at (x, y), ascending (2u dedupe)."""
        out = []
        for z in sorted(_bucket_heights(W, grid, minx, miny, cell, x, y)):
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

def _edge_frame(nodes, node_z, i, j):
    """((ax,ay,az), (bx,by,bz), (ux,uy), length) for an edge, or None."""
    if i >= len(nodes) or j >= len(nodes) or i == j:
        return None
    ax, ay = nodes[i][0], nodes[i][1]
    bx, by = nodes[j][0], nodes[j][1]
    length = math.hypot(bx - ax, by - ay)
    if length < 1e-4:
        return None
    return ((ax, ay, node_z[i]), (bx, by, node_z[j]),
            ((bx - ax) / length, (by - ay) / length), length)


def _edge_march_rows(nodes, edges, node_z, degree):
    """(station rows, plan entries) for every edge flat enough to grow."""
    ext = params.RIBBON_END_EXTEND
    edge_index = {(i, j): e for e, (i, j) in enumerate(edges)}
    rows, plan = [], []
    for (i, j) in edges:
        got = _edge_frame(nodes, node_z, i, j)
        if got is None:
            continue
        (ax, ay, az), (bx, by, bz), (ux, uy), length = got
        wx, wy = -uy, ux
        dz = bz - az
        ea = ext if degree.get(i, 0) <= 1 else 0.0
        eb = ext if degree.get(j, 0) <= 1 else 0.0
        pa = (ax - ux * ea, ay - uy * ea, az - dz * (ea / length))
        pb = (bx + ux * eb, by + uy * eb, bz + dz * (eb / length))
        if (abs(pb[2] - pa[2]) / max(length, 1e-6)
                > params.RIBBON_GROW_MAX_SLOPE):
            continue
        total = length + ea + eb
        k = max(1, int(round(total / params.RIBBON_STEP)))
        ramp = params.RIBBON_HALF_WIDTH
        lo0, lo1 = params.RIBBON_HALF_WIDTH, params.RIBBON_GROW_MIN_HALF
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
            rows.append((cxs, cys, czs, wx, wy, ux, uy, floor_h, ei))
            rows.append((cxs, cys, czs, -wx, -wy, ux, uy, floor_h, ei))
        plan.append(('edge', (i, j), pa, pb, (ux, uy), (wx, wy),
                     length, k, base))
    return rows, plan


def _disc_march_rows(nodes, edges, node_z, degree, rows):
    """Append each node's radial fan to `rows`; returns (plan, extra edges).

    See: docs/commentary/tes5_import_navmesh.md#stair-nodes-get-discs-too
    """
    nrays = params.RIBBON_GROW_DISC_RAYS
    disc_self = {}
    plan = []
    for ni in sorted(degree):
        if ni >= len(nodes):
            continue
        nx, ny = nodes[ni][0], nodes[ni][1]
        nz = node_z[ni]
        base = len(rows)
        ei = len(edges) + disc_self.setdefault(ni, len(disc_self))
        for kk in range(nrays):
            ang = 2.0 * math.pi * kk / nrays
            ddx, ddy = math.cos(ang), math.sin(ang)
            rows.append((nx, ny, nz, ddx, ddy, -ddy, ddx, 0.0, ei))
        plan.append(('disc', ni, nx, ny, nz, base))
    extra_edges = [(ni, ni) for ni, _slot in
                   sorted(disc_self.items(), key=lambda kv: kv[1])]
    return plan, extra_edges


def _plan_stations(nodes, edges, node_z, degree, grow):
    """Every march station the grow needs, as a plan the native batch consumes.

    Returns (stations, plan, extra_edges): an (N, 9) float64 array of
    (cx, cy, cz, dirx, diry, tanx, tany, lo, edge_index), the reassembly plan
    (`edge` and `disc` entries), and the synthetic self-pairs the disc
    stations name as their exclusion.
    See: docs/commentary/tes5_import_navmesh.md#stations-are-planned-then-marched
    """
    if not grow:
        return np.zeros((0, 9), dtype=np.float64), [], []
    rows, plan = _edge_march_rows(nodes, edges, node_z, degree)
    disc_plan, extra_edges = _disc_march_rows(nodes, edges, node_z, degree,
                                              rows)
    plan.extend(disc_plan)
    st = (np.asarray(rows, dtype=np.float64) if rows
          else np.zeros((0, 9), dtype=np.float64))
    return st, plan, extra_edges


def _profile_stations(sample, pa, pb, n):
    """Per-station (x, y, candidate heights) along a steep edge's centreline."""
    layers = sample.layers
    ax, ay, az = pa
    bx, by, bz = pb
    lo = min(az, bz) - params.MAX_CLIMB
    hi = max(az, bz) + params.MAX_CLIMB
    stations = []
    for s in range(n + 1):
        t = s / n
        x = ax + (bx - ax) * t
        y = ay + (by - ay) * t
        cand = [z for z in layers(x, y) if lo <= z <= hi]
        if not cand:
            cand = [az + (bz - az) * t]
        stations.append((x, y, cand))
    return stations


def _cheapest_layer_path(stations, az, n):
    """(costs, backpointers) for the one-step-per-station layer walk."""
    inf = float('inf')
    step = params.MAX_CLIMB
    costs = [[(abs(z - az) if abs(z - az) <= step else inf)
              for z in stations[0][2]]]
    back = []
    for s in range(1, n + 1):
        cand = stations[s][2]
        prev_c, prev_z = costs[-1], stations[s - 1][2]
        row = [inf] * len(cand)
        bk = [0] * len(cand)
        for i, z in enumerate(cand):
            for j, zp in enumerate(prev_z):
                if prev_c[j] == inf or abs(z - zp) > step:
                    continue
                c = prev_c[j] + abs(z - zp)
                if c < row[i]:
                    row[i], bk[i] = c, j
        costs.append(row)
        back.append(bk)
    return costs, back


def _surface_profile(sample, pa, pb):
    """Height profile along a STEEP edge, following the real walkable surface.

    Returns None (caller keeps the chord) when no layer path reaches the far
    node's own height.
    See: docs/commentary/tes5_import_navmesh.md#steep-heights-follow-the-treads
    """
    if getattr(sample, 'layers', None) is None:
        return None
    ax, ay, az = pa
    bx, by, bz = pb
    run = math.hypot(bx - ax, by - ay)
    if run < 32.0:
        return None
    n = max(2, int(run // 16.0))
    stations = _profile_stations(sample, pa, pb, n)
    costs, back = _cheapest_layer_path(stations, az, n)

    inf = float('inf')
    best = None
    for i, z in enumerate(stations[n][2]):
        if costs[n][i] == inf or abs(z - bz) > params.MAX_CLIMB:
            continue
        key = (abs(z - bz), costs[n][i], i)
        if best is None or key < best:
            best = key
    if best is None:
        return None
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


def _node_degrees(edges):
    """How many edges touch each node."""
    degree = {}
    for (i, j) in edges:
        degree[i] = degree.get(i, 0) + 1
        degree[j] = degree.get(j, 0) + 1
    return degree


def _steep_counts(nodes, edges, node_z):
    """How many STEEP runs touch each node."""
    steep_count = {}
    for (i, j) in edges:
        got = _edge_frame(nodes, node_z, i, j)
        if got is None:
            continue
        (_a, _b, _u, run) = got
        if abs(node_z[j] - node_z[i]) / run > params.RIBBON_GROW_MAX_SLOPE:
            steep_count[i] = steep_count.get(i, 0) + 1
            steep_count[j] = steep_count.get(j, 0) + 1
    return steep_count


def _ungrown_strip(strip, steep, sample, pa, pb):
    """Finish a strip the march never planned: fixed width, real tread heights.

    See: docs/commentary/tes5_import_navmesh.md#a-steep-ribbon-is-never-grown
    """
    strip['half'] = (params.RIBBON_STAIR_HALF_WIDTH if steep
                     else params.RIBBON_HALF_WIDTH)
    if steep and sample is not None:
        prof = _surface_profile(sample, pa, pb)
        if prof:
            strip['prof'] = prof
    return strip


def _grown_outline(strip, entry, widths, w):
    """Finish a grown strip: simplified rails closed into an explicit outline.

    See: docs/commentary/tes5_import_navmesh.md#rails-are-simplified-before-triangulation
    """
    wx, wy = w
    _, _, ppa, ppb, _u, _w, _len, k, base = entry
    left, right = [], []
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
    left = _simplify(left, params.RIBBON_RAIL_SIMPLIFY)
    right = _simplify(right, params.RIBBON_RAIL_SIMPLIFY)
    strip['poly'] = left + right[::-1]
    strip['half'] = max_h
    return strip


def _edge_strip(nodes, node_z, i, j, degree, grown_edges, widths, sample):
    """The ribbon for one pathgrid edge, or None if the edge is unusable.

    See: docs/commentary/tes5_import_navmesh.md#only-dead-ends-extend
    """
    got = _edge_frame(nodes, node_z, i, j)
    if got is None:
        return None
    (ax, ay, az), (bx, by, bz), (ux, uy), length = got
    ext = params.RIBBON_END_EXTEND
    ea = ext if degree.get(i, 0) <= 1 else 0.0
    eb = ext if degree.get(j, 0) <= 1 else 0.0
    dz = bz - az
    pa = (ax - ux * ea, ay - uy * ea, az - dz * (ea / length))
    pb = (bx + ux * eb, by + uy * eb, bz + dz * (eb / length))
    strip = {
        'edge': (i, j),
        'na': (ax, ay, az), 'nb': (bx, by, bz),
        'a': pa, 'b': pb,
        'u': (ux, uy), 'w': (-uy, ux), 'len': length,
    }
    entry = grown_edges.get((i, j)) if widths is not None else None
    if entry is None:
        steep = abs(dz) / length > params.RIBBON_GROW_MAX_SLOPE
        return _ungrown_strip(strip, steep, sample, pa, pb)
    return _grown_outline(strip, entry, widths, (-uy, ux))


def _trim_disc_ray(layers, nx, ny, nz, ddx, ddy, d):
    """Shorten a disc ray where the real surface leaves the node's level.

    See: docs/commentary/tes5_import_navmesh.md#disc-rays-are-trimmed-at-stairs
    """
    zcur = nz
    good = params.RIBBON_HALF_WIDTH
    dd = good
    while dd < d - 1e-6:
        dd = min(d, dd + 8.0)
        cand = [z for z in layers(nx + ddx * dd, ny + ddy * dd)
                if abs(z - zcur) <= params.MAX_CLIMB]
        if not cand:
            good = dd
            continue
        zc = min(cand, key=lambda z: abs(z - zcur))
        if abs(zc - nz) > params.MAX_CLIMB:
            break
        zcur = zc
        good = dd
    return good


def _disc_strip(entry, widths, layers, steep_strips, trim):
    """The node-disc ribbon for one plan entry, or None if it degenerates."""
    _, ni, nx, ny, nz, base = entry
    nrays = params.RIBBON_GROW_DISC_RAYS
    disc = []
    for kk in range(nrays):
        ang = 2.0 * math.pi * kk / nrays
        ddx, ddy = math.cos(ang), math.sin(ang)
        d = float(widths[base + kk])
        if trim and d > params.RIBBON_HALF_WIDTH:
            d = _trim_disc_ray(layers, nx, ny, nz, ddx, ddy, d)
        disc.append((nx + ddx * d, ny + ddy * d))
    disc = _simplify(disc, params.RIBBON_RAIL_SIMPLIFY)
    if len(disc) < 3:
        return None
    disc = _clip_flat_poly_off_level(disc, nx, ny, nz, steep_strips)
    if len(disc) < 3:
        return None
    rmax = max(math.hypot(px - nx, py - ny) for (px, py) in disc)
    return {
        'edge': (ni, ni),
        'na': (nx, ny, nz), 'nb': (nx, ny, nz),
        'a': (nx, ny, nz), 'b': (nx, ny, nz),
        'u': (1.0, 0.0), 'w': (0.0, 1.0),
        'len': max(rmax, 1.0), 'half': max(rmax, 1.0),
        'poly': disc,
    }


def _steep_strips(strips):
    """Every ribbon steeper than RIBBON_GROW_MAX_SLOPE."""
    out = []
    for s in strips:
        if s.get('len', 0.0) < 1e-6:
            continue
        if (abs(s['nb'][2] - s['na'][2]) / s['len']
                > params.RIBBON_GROW_MAX_SLOPE):
            out.append(s)
    return out


def _build_corridor_strips(nodes, edges, node_z, wall_hit=None,
                           walk_probe=None, field=None,
                           blocking=None, walkable=None, sample=None):
    """One corridor ribbon per pathgrid edge, plus a disc at every node.

    Each strip carries its centreline ends (after dead-end extension), the
    along/perpendicular units, a MAX half-width for level lookups, and in
    Phase 2 an explicit grown outline.  They are NOT yet a shared mesh --
    corridor_union takes their boolean union and retriangulates it.
    See: docs/commentary/tes5_import_navmesh.md#ribbon-construction
    """
    grow = params.RIBBON_GROW and blocking is not None
    degree = _node_degrees(edges)
    steep_count = _steep_counts(nodes, edges, node_z)

    stations, plan, extra_edges = _plan_stations(nodes, edges, node_z,
                                                 degree, grow)
    widths = None
    if len(stations):
        widths = corridor_grow.grow_batch(
            blocking, walkable, nodes, list(edges) + extra_edges,
            node_z, stations)
    grown_edges = {p[1]: p for p in plan if p[0] == 'edge'}

    strips = []
    for (i, j) in edges:
        strip = _edge_strip(nodes, node_z, i, j, degree, grown_edges,
                            widths, sample)
        if strip is not None:
            strips.append(strip)

    if widths is None:
        return strips
    steep = _steep_strips(strips)
    layers = getattr(sample, 'layers', None) if sample is not None else None
    for entry in plan:
        if entry[0] != 'disc':
            continue
        trim = (DISC_RAY_TRIM and layers is not None
                and steep_count.get(entry[1], 0) >= 1)
        disc = _disc_strip(entry, widths, layers, steep, trim)
        if disc is not None:
            strips.append(disc)
    return strips


def _strip_z_at(strip, az, bz, t):
    """Height along a strip at parameter t, following its profile if it has one."""
    prof = strip.get('prof')
    if not prof:
        return az + (bz - az) * t
    f = t * (len(prof) - 1)
    k = min(len(prof) - 2, max(0, int(f)))
    return prof[k][2] + (prof[k + 1][2] - prof[k][2]) * (f - k)


def _off_level_mask(strip, az, bz, nz, ax, ay, bx, by, n):
    """Per-station True where the strip has left the flat surface's level."""
    if callable(nz):
        return [abs(_strip_z_at(strip, az, bz, k / n)
                    - nz(ax + (bx - ax) * (k / n),
                         ay + (by - ay) * (k / n))) > params.MAX_CLIMB
                for k in range(n + 1)]
    return [abs(_strip_z_at(strip, az, bz, k / n) - nz) > params.MAX_CLIMB
            for k in range(n + 1)]


def _anchor_stations(mask, ap, ax, ay, dx, dy, n):
    """On-level stations lying INSIDE the polygon, which anchor the flight.

    See: docs/commentary/tes5_import_navmesh.md#flat-polys-are-clipped-off-level
    """
    from shapely.geometry import Point
    anchored = set()
    for k in range(n + 1):
        if mask[k]:
            continue
        try:
            if ap.contains(Point(ax + dx * (k / n), ay + dy * (k / n))):
                anchored.add(k)
        except Exception:
            pass
    return anchored


def _cut_quads(strip, mask, anchored, frame, half, n):
    """The quads to subtract: off-level runs contiguous with an anchored mouth."""
    (ax, ay), (ux, uy), (wx, wy), run = frame
    hits = []
    k = 0
    while k <= n:
        if not mask[k]:
            k += 1
            continue
        k2 = k
        while k2 + 1 <= n and mask[k2 + 1]:
            k2 += 1
        if (k - 1) in anchored or (k2 + 1) in anchored:
            d0, d1 = run * k / n, run * k2 / n
            if d1 - d0 > 1.0:
                hits.append((
                    (ax + ux * d0 + wx * half, ay + uy * d0 + wy * half),
                    (ax + ux * d0 - wx * half, ay + uy * d0 - wy * half),
                    (ax + ux * d1 - wx * half, ay + uy * d1 - wy * half),
                    (ax + ux * d1 + wx * half, ay + uy * d1 + wy * half)))
        k = k2 + 1
    return hits


def _strip_cut_quads(strip, disc, nx, ny, nz, rmax, anchor):
    """Quads this steep strip contributes to the subtraction, possibly empty."""
    ax, ay, az = strip['a']
    bx, by, bz = strip['b']
    run = math.hypot(bx - ax, by - ay)
    if run < 1e-6:
        return []
    half = float(strip.get('half', params.RIBBON_STAIR_HALF_WIDTH))
    dx, dy = bx - ax, by - ay
    t0 = max(0.0, min(1.0, ((nx - ax) * dx + (ny - ay) * dy) / (run * run)))
    if math.hypot(nx - (ax + dx * t0),
                  ny - (ay + dy * t0)) > rmax + half + 8.0:
        return []
    ap = anchor()
    if ap is None:
        return []
    n = max(2, int(run // 8.0))
    mask = _off_level_mask(strip, az, bz, nz, ax, ay, bx, by, n)
    anchored = _anchor_stations(mask, ap, ax, ay, dx, dy, n)
    if not anchored:
        return []
    frame = ((ax, ay), (dx / run, dy / run), (-dy / run, dx / run), run)
    return _cut_quads(strip, mask, anchored, frame, half, n)


def _subtract_quads(disc, nx, ny, hits):
    """Cut `hits` out of the polygon, keeping the piece the node stands on."""
    try:
        from shapely.geometry import Point, Polygon
        from shapely.ops import unary_union
        dp = Polygon(disc)
        if not dp.is_valid:
            dp = dp.buffer(0)
        cut = dp.difference(unary_union([Polygon(q) for q in hits]))
        if cut.is_empty:
            return disc
        pieces = list(cut.geoms) if hasattr(cut, 'geoms') else [cut]
        pieces = [g for g in pieces if g.geom_type == 'Polygon' and g.area > 1.0]
        if not pieces:
            return disc
        best = min(pieces, key=lambda g: g.distance(Point(nx, ny)))
        ring = list(best.exterior.coords)
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring = ring[:-1]
        return ring
    except Exception:
        return disc


def _clip_flat_poly_off_level(disc, nx, ny, nz, steep_strips):
    """Remove from a FLAT polygon the ground where a steep ribbon that MEETS it
    has LEFT the polygon's level by more than a step.

    (nx, ny) anchors which piece survives a split; `nz` is the flat surface's
    height, either a constant (node disc) or a callable (sloped door quad).
    See: docs/commentary/tes5_import_navmesh.md#flat-polys-are-clipped-off-level
    """
    cache = []

    def anchor():
        """The buffered polygon, built lazily -- most callers never need it."""
        if not cache:
            try:
                from shapely.geometry import Polygon
                ap = Polygon(disc)
                if not ap.is_valid:
                    ap = ap.buffer(0)
                cache.append(ap.buffer(8.0))
            except Exception:
                cache.append(None)
        return cache[0]

    rmax = max(math.hypot(px - nx, py - ny) for (px, py) in disc)
    hits = []
    for s in steep_strips:
        hits.extend(_strip_cut_quads(s, disc, nx, ny, nz, rmax, anchor))
    if not hits:
        return disc
    return _subtract_quads(disc, nx, ny, hits)


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

def _cell_geometry(refr_recs, base_model_by_fid, get_collision, land_rec,
                   origin_x, origin_y, door_bases):
    """(walkable, blocking) for a cell, with LAND folded into the walkable set."""
    walkable, blocking, land_walk = world.gather_cell_geometry(
        refr_recs or [], base_model_by_fid or {}, get_collision,
        land_rec=land_rec, origin_x=origin_x, origin_y=origin_y,
        split_land=True, skip_bases=door_bases)
    if land_walk is not None and len(land_walk):
        walkable = (np.concatenate([walkable, land_walk])
                    if len(walkable) else land_walk)
    return walkable, blocking


def _quad_height_fn(poly, zb, zf, sweep, bm, fm):
    """A callable giving the door quad's ramped height at any (x, y)."""
    bmx, bmy = bm
    fmx, fmy = fm

    def _qz(px, py):
        """Height of the ramping quad at this point."""
        if sweep < 1e-6:
            return zb
        t = (((px - bmx) * (fmx - bmx) + (py - bmy) * (fmy - bmy))
             / (sweep * sweep))
        return zb + (zf - zb) * max(0.0, min(1.0, t))

    return _qz


def _ramped_strip(ps, zb, zf, sweep, bm, fm):
    """Point a door strip's height axis down its ramp, when it has one."""
    if abs(zf - zb) <= 1.0 or sweep <= 1e-6:
        return ps
    bmx, bmy = bm
    fmx, fmy = fm
    ux_, uy_ = (fmx - bmx) / sweep, (fmy - bmy) / sweep
    ps['a'] = (bmx, bmy, zb)
    ps['b'] = (fmx, fmy, zf)
    ps['na'], ps['nb'] = ps['a'], ps['b']
    ps['u'] = (ux_, uy_)
    ps['w'] = (-uy_, ux_)
    ps['len'] = sweep
    ps['half'] = max(float(ps['half']), sweep) + 8.0
    return ps


def _door_quad_strip(fp, steep_list):
    """(strip, base entry, pins) for one door footprint, or None if degenerate.

    See: docs/commentary/tes5_import_navmesh.md#the-door-quad-ramps-and-is-clipped
    """
    from . import corridor_union
    poly = fp['poly']
    zb = float(fp['z'])
    zf = float(fp.get('z_far', fp['z']))
    bmx = 0.5 * (poly[0][0] + poly[1][0])
    bmy = 0.5 * (poly[0][1] + poly[1][1])
    fmx = 0.5 * (poly[2][0] + poly[3][0])
    fmy = 0.5 * (poly[2][1] + poly[3][1])
    sweep = math.hypot(fmx - bmx, fmy - bmy)
    if abs(zf - zb) > 0.5 * max(sweep, 1.0):
        zf = zb
    qz = _quad_height_fn(poly, zb, zf, sweep, (bmx, bmy), (fmx, fmy))

    if steep_list and len(poly) >= 3:
        if fp['base'] is not None:
            ax_ = 0.5 * (fp['base'][0][0] + fp['base'][1][0])
            ay_ = 0.5 * (fp['base'][0][1] + fp['base'][1][1])
        else:
            ax_ = sum(p[0] for p in poly) / len(poly)
            ay_ = sum(p[1] for p in poly) / len(poly)
        poly = _clip_flat_poly_off_level(poly, ax_, ay_, qz, steep_list)
    if len(poly) < 3:
        return None

    ps = _ramped_strip(corridor_union._poly_strip(poly, zb),
                       zb, zf, sweep, (bmx, bmy), (fmx, fmy))
    if fp['base'] is None:
        return ps, None, []
    (b0, b1), apex, fz = fp['base'], fp['apex'], fp['z']
    pins = [(b0[0], b0[1], fz), (b1[0], b1[1], fz),
            (0.5 * (b0[0] + b1[0]), 0.5 * (b0[1] + b1[1]), fz),
            (apex[0], apex[1], fz)]
    return ps, (b0, b1, apex, fp['z']), pins


def _door_geometry(corridors, door_list, nodes, edges, wall_hit, cell_clip):
    """(door strips, door base edges, wedge pins) from a probe union.

    See: docs/commentary/tes5_import_navmesh.md#door-mesh-stays-in-the-union
    """
    from . import corridor_doors, corridor_union
    strips, edges_out, pins = [], [], []
    if not door_list:
        return strips, edges_out, pins
    rv, rt = corridor_union.build_union_mesh(corridors, cell_bounds=cell_clip,
                                             wall_cut=None, probe_only=True)
    if not rt:
        return strips, edges_out, pins
    steep_list = _steep_strips(corridors)
    for fp in corridor_doors.door_footprints(rv, rt, door_list,
                                             wall_hit=wall_hit, nodes=nodes,
                                             pg_edges=edges):
        got = _door_quad_strip(fp, steep_list)
        if got is None:
            continue
        ps, edge, quad_pins = got
        strips.append(ps)
        if edge is not None:
            edges_out.append(edge)
            pins.extend(quad_pins)
    return strips, edges_out, pins


def _centreline_samples(nodes, edges, node_z):
    """(x, y, z, ux, uy) along every pathgrid edge, at RIBBON_STEP spacing.

    See: docs/commentary/tes5_import_navmesh.md#every-centreline-is-sampled
    """
    out = []
    for (i, j) in edges:
        got = _edge_frame(nodes, node_z, i, j)
        if got is None:
            continue
        (_a, _b, (ux_, uy_), run) = got
        steps = max(2, int(run // params.RIBBON_STEP))
        for s in range(steps + 1):
            f = s / steps
            out.append((nodes[i][0] + (nodes[j][0] - nodes[i][0]) * f,
                        nodes[i][1] + (nodes[j][1] - nodes[i][1]) * f,
                        node_z[i] + (node_z[j] - node_z[i]) * f, ux_, uy_))
    return out


def _tri_carries_door(verts, tri, door_xy):
    """Does a door threshold stand on this triangle, within a storey?"""
    a, b, c = (verts[i] for i in tri)
    d = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
    if abs(d) < 1e-9:
        return False
    for (px, py, pz) in door_xy:
        l0 = ((b[1] - c[1]) * (px - c[0]) + (c[0] - b[0]) * (py - c[1])) / d
        l1 = ((c[1] - a[1]) * (px - c[0]) + (a[0] - c[0]) * (py - c[1])) / d
        l2 = 1.0 - l0 - l1
        if (l0 >= -0.05 and l1 >= -0.05 and l2 >= -0.05
                and abs(l0 * a[2] + l1 * b[2] + l2 * c[2] - pz) <= 128.0):
            return True
    return False


def _drop_attach_scraps(verts, tris, door_xy):
    """Drop 1-2 triangle specks the door attach orphaned, keeping door ground.

    See: docs/commentary/tes5_import_navmesh.md#attach-era-scraps-are-dropped
    """
    from . import corridor_clean
    comps = corridor_clean.components([list(map(int, t)) for t in tris])
    if len(comps) <= 1:
        return tris
    drop = set()
    for comp in comps:
        if len(comp) > 2:
            continue
        if not any(_tri_carries_door(verts, tris[ti], door_xy)
                   for ti in comp):
            drop.update(comp)
    if not drop:
        return tris
    return [t for ti, t in enumerate(tris) if ti not in drop]


def _ccw_in_plan(verts, tris):
    """Flip any triangle wound CW in plan; the mesh is a heightfield.

    See: docs/commentary/tes5_import_navmesh.md#winding-must-be-ccw-in-plan
    """
    return [((t[0], t[2], t[1])
             if ((verts[t[1]][0] - verts[t[0]][0])
                 * (verts[t[2]][1] - verts[t[0]][1])
                 - (verts[t[2]][0] - verts[t[0]][0])
                 * (verts[t[1]][1] - verts[t[0]][1])) < 0 else t)
            for t in tris]


def _lazy_wall_hit(blocking):
    """A wall-slab sampler that indexes the blocking soup on first use.

    See: docs/commentary/tes5_import_navmesh.md#the-wall-sampler-is-lazy
    """
    cache = []

    def wall_hit(*a, **kw):
        """True if a wall stands in the actor slab; builds the index once."""
        if not cache:
            cache.append(corridor_grow.wall_slab_sampler(blocking))
        return cache[0](*a, **kw)

    return wall_hit


def build_corridors(refr_recs, base_model_by_fid, get_collision, nodes, edges,
                    land_rec=None, origin_x=0.0, origin_y=0.0, doors=None,
                    door_bases=None):
    """Phase-1 corridor navmesh for one cell: (verts, tris, ledges) lists.

    doors: [(x, y, z, rot_z, is_teleport, width), ...] pivot-corrected door
    centres.  door_bases: low-24 DOOR base FormIDs contributing no collision.
    ledges: [(upper_tri, lower_tri, drop), ...] for NVNM Ledge Up/Down links.
    See: docs/commentary/tes5_import_navmesh.md#ribbon-construction
    """
    if not nodes or not edges:
        return [], [], []
    from . import corridor_clean, corridor_union

    walkable, blocking = _cell_geometry(
        refr_recs, base_model_by_fid, get_collision, land_rec,
        origin_x, origin_y, door_bases)
    sample = _surface_sampler(walkable)
    node_z = [_snap_node_z(sample, nodes[i][0], nodes[i][1], nodes[i][2])
              for i in range(len(nodes))]

    corridors = _build_corridor_strips(nodes, edges, node_z,
                                       blocking=blocking, walkable=walkable,
                                       sample=sample)
    cell_clip = None
    if land_rec is not None:
        cell_clip = (origin_x, origin_y, origin_x + 4096.0, origin_y + 4096.0)

    door_list = list(doors or ())
    door_strips, door_edges, door_pins = _door_geometry(
        corridors, door_list, nodes, edges, _lazy_wall_hit(blocking),
        cell_clip)

    verts, tris = corridor_union.build_union_mesh(
        corridors, extra_strips=door_strips, door_edges=door_edges,
        cell_bounds=cell_clip, wall_cut=None)
    if not tris:
        return [], [], []

    door_xy = [(x, y, z) for (x, y, z, r, tp, w) in door_list]
    pin_xy = (list(door_xy) + door_pins
              + _centreline_samples(nodes, edges, node_z))
    verts, tris, ledge_marks = corridor_clean.finalize(
        verts, tris, cs=(params.CS_EXTERIOR if land_rec is not None
                         else params.CS),
        doors=door_xy, cell_bounds=cell_clip, pin_xy=pin_xy,
        door_pins=door_pins,
        node_pins=[(nodes[i][0], nodes[i][1]) for i in range(len(nodes))])

    verts = [tuple(float(c) for c in v) for v in verts]
    tris = [tuple(int(i) for i in t) for t in tris]
    tris = _drop_attach_scraps(verts, tris, door_xy)
    tris = corridor_clean._drop_degenerate_guarded(verts, tris)
    tris = _ccw_in_plan(verts, tris)

    ledges = corridor_clean._resolve_ledges(verts, tris, ledge_marks)
    return (verts, tris,
            [(int(a), int(b), float(d)) for (a, b, d) in ledges])
