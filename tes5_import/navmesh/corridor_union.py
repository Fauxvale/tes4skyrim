"""Boolean polygon union of the corridor ribbons, then retriangulation.

WHY THIS IS THE RIGHT ALGORITHM

Corridor ribbons overlap wherever pathgrid lines converge.  Cutting them
pairwise (trim the ribbon, weld the seam, patch the junction) is an
approximation that has to get every case right — end-to-end, crossing, wedge,
collinear, dead end — and any case it gets wrong shows up as either lost ground
or stacked sheets.

The union does not approximate.  Each ribbon is a polygon; the geometric union
of the polygons is a region whose area is EXACTLY the measure of the ground the
corridors cover.  Retriangulating that region produces triangles that are
non-overlapping by definition.  So:

    coverage  == 100% by construction (the union contains every ribbon)
    overlap   ==   0% by construction (a triangulation does not self-overlap)

STOREYS — one union PER SHEET, never one flattened union

A single 2D union of every ribbon is WRONG for a multi-storey building, because
the floors overlap in plan view — measured in ChorrolFightersGuild, the three
floors overlap each other by 26-36% of the union's area.  Flattening them and
triangulating once lets a triangle take one corner from the upper floor and
another from the lower: a near-vertical sheet hanging in the stairwell, which is
what rendered as "triangles between floors".  Neither emitting those (mid-air
mesh) nor dropping them (they severed 24 shared edges and split one floor into
7 pieces) is a fix, because the flattened polygon was never the right region.

So the ribbons are partitioned into SHEETS first, and each sheet is unioned and
triangulated on its own:

  1. `_storey_groups` joins ribbons that share a pathgrid NODE where their
     heights agree.  A staircase therefore joins the floor at its foot AND the
     floor at its head, so a whole building comes back as one connected group —
     correct for connectivity, but still not a region we can union.
  2. `_split_plan_overlaps` cuts that group into sheets that do not overlap
     THEMSELVES in plan: two ribbons that overlap in plan and disagree in height
     by more than STOREY_GAP_Z cannot share a sheet.  Ribbons are then assigned
     to the sheet whose height they match BEST (a first-fit scattered one floor
     across several sheets, which overlapped at the same height and duplicated
     ground — 7% of triangles stacked).
  3. Each sheet is unioned, triangulated, and lifted independently, then
     `_weld_sheets` (3D radius weld) and `_split_t_junctions` rejoin sheets that
     abut on one floor, so the surface stays connected across a sheet boundary.

HEIGHT — the vertex, not the triangle, owns it

Every output vertex gets its Z from a corridor that covers it, along that
corridor's own centreline, so each triangle sits on the pathgrid line's own
slope (principle 2) and a staircase keeps its rise.

Crucially the height is a property of THE POINT AND ITS STOREY, never of
whichever triangle reached it first.  The original code took a triangle's height
as the MEAN of its three corners' levels and then bound each corner to any vertex
already within SAME_SURFACE_Z of that mean, so a corner's height depended on
triangle order: corner 22 of ICPrisonSewerExit01, carrying a single level at
395.3, minted one vertex at 395.3 for one neighbour and another at 356.2 for the
next.  Those two triangles then shared no EDGE, and the engine cannot walk
between triangles that share only a point — 28 of 582 shared edges were lost and
the mesh fell into 12 components (ICPrisonEntrance01: 28).  Stairs tore worst
because every consecutive triangle on a flight has a different mean.  No value of
SAME_SURFACE_Z fixes that; it is a first-match-wins race, not a tolerance.

`_emit_surfaces` instead keys each vertex on (corner, storey band), a stable
per-corner identity, so two triangles meeting on one surface ALWAYS resolve to
the same vertex and connectivity is structural.

DOORS

corridor_doors.door_footprints runs first, on the raw ribbon union; each door's
flat footprint (the quad bridging its base line to the nearest corridor edge) is
handed back as an `extra_strips` polygon and joins the union as ordinary ground,
and its BASE LINE is passed as a `door_edges` constraint so the retriangulation
forces one large triangle with its long side on the door line — the vanilla
Skyrim door triangle.  The union resolves any overlap with the corridor by
construction — the door coverage is preserved exactly, nothing is deleted.

HEIGHT

Every output vertex gets its Z from a corridor that covers it, along that
corridor's own centreline, so each triangle sits on the pathgrid line's own
slope (principle 2) and a staircase keeps its rise.  Heights are never discarded
and reconstructed — each ribbon already knows its Z everywhere along itself.
"""

import math

import numpy as np

from . import params

# Heights within this of each other at one point are treated as the same
# walkable surface when a vertex is placed and when it looks up its level.  Kept
# small so a genuine step between stacked sheets is never fused, but large enough
# to absorb the little disagreement where two ribbons cross on a slope.
SAME_SURFACE_Z = 36.0
# Half-width of the hairline gap opened along every wall when splitting the
# union (see wall_cuts).  Just wide enough to separate the two sides reliably in
# floating point; far below any real corridor width, so it costs no coverage.
WALL_CUT_WIDTH = 1.0

# Two levels at one point belong to DIFFERENT storeys only when they are at
# least this far apart.  Anything closer is one walkable surface — a stair step,
# a ramp, two ribbons meeting at a slight angle — and must produce ONE triangle;
# emitting both stacks them (measured: levels 39u apart on a Chorrol stair).
STOREY_GAP_Z = 120.0

# How far a corner's own ground may be from a surface and still count as being ON
# that surface (_reaches, inside _emit_surfaces).  This is a STEP tolerance, not a
# storey threshold — the two answer different questions, and using STOREY_GAP_Z
# here was a category error.  See _reaches for the measured consequence.
REACH_TOL = STOREY_GAP_Z


def _ribbon_polygon(s):
    """The corridor's ribbon as a 2D polygon (a rectangle around its segment).

    A strip may instead carry an explicit 'poly' outline — the door triangles
    do — in which case that shape is used verbatim.
    """
    from shapely.geometry import Polygon

    if s.get('poly') is not None:
        p = Polygon(s['poly'])
        # A grown corridor outline (corridor.py Phase 2) can self-intersect where
        # two cross-sections' rails cross at a sharp concavity.
        #
        # buffer(0) is NOT a safe repair on its own: on a bow-tie outline it
        # returns a MultiPolygon and shapely's own union then keeps the lobes as
        # separate pieces, so the part of the ribbon that bridged to a neighbour
        # is effectively lost.  Measured on ChorrolFightersGuild: exactly the 7
        # ribbons with invalid outlines — (22,23), (22,24), (26,43), (26,42),
        # (26,27), (41,42), (25,26) — were the ones whose sheet unioned into 5
        # disjoint parts, with ribbon (22,23) appearing in two parts without
        # joining them.  pathgrid=1 but navmesh=4.
        #
        # Repair by keeping EVERY lobe (union of the pieces) and, critically,
        # covering the CENTRELINE with a minimum-width band.  The centreline is
        # sacred (principle 1) — the pathgrid asserts an actor walks it — so the
        # ribbon must always contain it, which is also exactly what makes two
        # ribbons sharing a node overlap and union into one sheet.
        if not p.is_valid:
            from shapely.geometry import LineString
            from shapely.ops import unary_union as _uu
            fixed = p.buffer(0)
            pieces = []
            if not fixed.is_empty:
                if hasattr(fixed, 'geoms'):
                    pieces.extend(g for g in fixed.geoms
                                  if isinstance(g, Polygon) and g.area > 0.0)
                elif isinstance(fixed, Polygon) and fixed.area > 0.0:
                    pieces.append(fixed)
            spine = LineString([(s['a'][0], s['a'][1]),
                                (s['b'][0], s['b'][1])])
            pieces.append(spine.buffer(max(params.RIBBON_GROW_MIN_HALF, 1.0),
                                       cap_style=2))
            try:
                p = _uu(pieces)
            except Exception:
                p = pieces[-1]
            if not isinstance(p, Polygon) and not hasattr(p, 'geoms'):
                p = pieces[-1]
        return p

    ax, ay = s['a'][0], s['a'][1]
    bx, by = s['b'][0], s['b'][1]
    wx, wy = s['w']
    h = s['half']
    return Polygon([
        (ax + wx * h, ay + wy * h),
        (bx + wx * h, by + wy * h),
        (bx - wx * h, by - wy * h),
        (ax - wx * h, ay - wy * h),
    ])


def _poly_strip(poly2d, z):
    """A flat footprint polygon at a fixed height, as a strip for the union.

    The door footprint (base line bridged to the corridor edge) is handed in
    this way: it contributes its outline to the union and a constant height z to
    the level lookup, so the door ground knows how high it sits.  Its axis runs
    along the first polygon edge (only used to give the height lookup a gradient,
    which is flat here anyway).
    """
    a = (float(poly2d[0][0]), float(poly2d[0][1]), float(z))
    b = (float(poly2d[1][0]), float(poly2d[1][1]), float(z))
    length = math.hypot(b[0] - a[0], b[1] - a[1]) or 1.0
    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    return {
        'edge': (-1, -1),
        'na': a, 'nb': b, 'a': a, 'b': b,
        'u': (ux, uy), 'w': (-uy, ux),
        'half': 0.5 * length, 'len': length,
        'poly': [(float(p[0]), float(p[1])) for p in poly2d],
    }


def _height_on(s, px, py):
    """Height of corridor s's surface at (px, py), following its own slope.

    Strictly the straight A->B line (principle 2): the pathgrid edge IS the walk
    ramp, so the ribbon's angle is the LINE's angle.  Re-fitting it to sampled
    collision was tried and is wrong — it changes the staircase's angle away from
    the pathgrid line the designer drew, which is the one thing this model treats
    as ground truth.
    """
    ax, ay, az = s['a']
    bx, by, bz = s['b']
    dx, dy = bx - ax, by - ay
    d2 = dx * dx + dy * dy
    t = 0.0 if d2 < 1e-9 else max(0.0, min(1.0, ((px - ax) * dx +
                                                 (py - ay) * dy) / d2))
    return az + (bz - az) * t


def _distance_to(s, px, py):
    """Distance from (px, py) to the strip's centreline.

    For a strip with an explicit outline (a door triangle) the distance is 0
    inside that outline, so it only ever claims the ground it actually covers —
    a centreline measure would let it claim well outside its own shape.
    """
    if s.get('poly') is not None:
        if _point_in_poly(px, py, s['poly']):
            return 0.0
        return min(_seg_dist(px, py, s['poly'][i],
                             s['poly'][(i + 1) % len(s['poly'])])
                   for i in range(len(s['poly'])))

    ax, ay = s['a'][0], s['a'][1]
    bx, by = s['b'][0], s['b'][1]
    dx, dy = bx - ax, by - ay
    d2 = dx * dx + dy * dy
    t = 0.0 if d2 < 1e-9 else max(0.0, min(1.0, ((px - ax) * dx +
                                                 (py - ay) * dy) / d2))
    return math.hypot(px - (ax + dx * t), py - (ay + dy * t))


def _point_in_poly(px, py, poly):
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > py) != (y2 > py):
            xin = x1 + (py - y1) * (x2 - x1) / ((y2 - y1) or 1e-12)
            if px < xin:
                inside = not inside
    return inside


def _seg_dist(px, py, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    d2 = dx * dx + dy * dy
    t = 0.0 if d2 < 1e-9 else max(0.0, min(1.0, ((px - a[0]) * dx +
                                                 (py - a[1]) * dy) / d2))
    return math.hypot(px - (a[0] + dx * t), py - (a[1] + dy * t))


def _triangulate(poly, target_edge, fixed_edges=None, steep_seeds=None):
    """Triangulate a shapely polygon into UNIFORM, well-shaped triangles.

    Returns (verts2d, tris).  The old approach earcut'd the polygon after
    cutting it on an 8u grid, which produced a mesh full of needles and tiny
    slivers along every boundary (20% of triangles had an edge ratio > 3, some
    > 400).  Vanilla Skyrim navmeshes are near-uniform ~target_edge triangles,
    so we reproduce that:

      1. Sample interior Steiner points on a hex lattice at `target_edge`
         spacing — a hex lattice, not a square grid, so the Delaunay of the
         points is near-equilateral (the Voronoi-dual pattern the author asked
         for) instead of right-isoceles.
      2. Densify the boundary rings at the same spacing so boundary triangles
         are the same scale as interior ones.
      3. Delaunay-triangulate the whole point set, then keep only triangles
         whose CENTROID lies inside the polygon — this honours the outline and
         every hole exactly (a ring of corridors around an obstacle keeps its
         hole) without a constrained triangulator.

    `fixed_edges` is a list of (p0, p1) 2D segments that MUST appear as a
    triangle edge — the door base lines.  Their endpoints are inserted as
    Steiner points and no interior sample is placed near the segment, so the
    Delaunay naturally forms a triangle with that long edge (Skyrim's door
    triangle).

    `steep_seeds` is a list of (x, y) points along STEEP ribbon centrelines
    (stairs, ramps).  A uniform target_edge triangle on a staircase climbs more
    than one storey gap across its corners and is dropped by the per-surface
    emission — the whole stair vanishes.  These seeds are forced in at a fine
    spacing so the stair keeps short, gently-climbing triangles that survive.
    """
    from shapely.geometry import Point
    from shapely.prepared import prep

    ext = list(poly.exterior.coords)[:-1]
    if len(ext) < 3:
        return [], []

    # A coarse spatial hash of accepted points, so a candidate can be rejected
    # when it crowds an existing one — this Poisson-disk guard is what keeps the
    # Delaunay well-shaped: a boundary sample landing a few units from a lattice
    # point (or two boundary rings nearly touching) is exactly what breeds the
    # sliver needles, so we simply never place the second point.
    bin_size = max(1.0, target_edge * 0.5)
    hash_bins = {}

    def _too_close(x, y, r2):
        gx, gy = int(x // bin_size), int(y // bin_size)
        for ddx in (-1, 0, 1):
            for ddy in (-1, 0, 1):
                for (ex, ey) in hash_bins.get((gx + ddx, gy + ddy), ()):
                    if (ex - x) ** 2 + (ey - y) ** 2 < r2:
                        return True
        return False

    pts = []
    # Even a FORCED point (an outline corner, a door endpoint) is dropped when
    # it sits within this of an existing one: two union-outline corners landing
    # ~1u apart are the same corner and only breed a 1u-short needle.  Well
    # below the 40u ribbon width, so no real feature is welded away.
    weld2 = 3.0 ** 2

    def add(x, y, min_dist=0.0, force=False):
        x, y = float(x), float(y)
        if _too_close(x, y, weld2):
            return
        if not force and min_dist > 0.0 and _too_close(x, y, min_dist * min_dist):
            return
        hash_bins.setdefault((int(x // bin_size), int(y // bin_size)),
                             []).append((x, y))
        pts.append((x, y))

    # 1. boundary vertices + densified boundary samples.  Corners are FORCED
    #    (they define the outline); interpolated samples yield to spacing so a
    #    short edge does not seed a cluster of near-coincident points.
    for ring in [poly.exterior] + list(poly.interiors):
        coords = list(ring.coords)
        for i in range(len(coords) - 1):
            x0, y0 = coords[i]
            x1, y1 = coords[i + 1]
            add(x0, y0, force=True)
            seg = math.hypot(x1 - x0, y1 - y0)
            n = int(seg // target_edge)
            for s in range(1, n + 1):
                f = s / (n + 1)
                add(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f,
                    min_dist=target_edge * 0.5)

    # 2. door-base endpoints, forced (they must survive to make the door edge)
    fixed_edges = fixed_edges or []
    fixed_pts = []
    for (p0, p1) in fixed_edges:
        add(p0[0], p0[1], force=True)
        add(p1[0], p1[1], force=True)
        fixed_pts.append((p0, p1))

    # 3. ribbon centreline seeds.  Steep (stair) seeds are FORCED in at their
    #    fine spacing; flat centreline seeds YIELD to the Poisson spacing, so
    #    they only survive where a corridor is too narrow for the hex lattice
    #    (guaranteeing its bridge triangles stay inside) and are thinned out in
    #    open rooms that the lattice already fills.
    pp = prep(poly)
    for (sx, sy, steep) in (steep_seeds or ()):
        p = Point(sx, sy)
        if steep:
            # A FORCED seed is admitted on the BOUNDARY too, not just strictly
            # inside.  shapely's `contains` is false for a boundary point, and a
            # pathgrid node where two sheets meet lies exactly ON both sheets'
            # outlines — so the seed that was supposed to give them a shared
            # vertex was silently discarded, and Pinarus's stair top stayed 31u
            # from the upper floor with the house in two components.
            if not pp.intersects(p):
                continue
            add(sx, sy, force=True)
        else:
            if not pp.contains(p):
                continue
            add(sx, sy, min_dist=target_edge * 0.6)

    # 4. interior hex lattice at target_edge spacing.  A lattice point yields to
    #    the Poisson-disk spacing (never crowd a boundary, door, or steep seed),
    #    and to the door keep-out (leave the door triangle clean and large).
    minx, miny, maxx, maxy = poly.bounds
    dy = target_edge * math.sqrt(3.0) / 2.0
    row = 0
    y = miny + dy * 0.5
    keepout2 = (target_edge * 0.75) ** 2
    while y < maxy:
        off = 0.0 if (row % 2 == 0) else target_edge * 0.5
        x = minx + off + target_edge * 0.25
        while x < maxx:
            if pp.contains(Point(x, y)):
                near_fixed = any(_seg_dist2(x, y, p0, p1) < keepout2
                                 for (p0, p1) in fixed_pts)
                if not near_fixed:
                    add(x, y, min_dist=target_edge * 0.6)
            x += target_edge
        y += dy
        row += 1

    if len(pts) < 3:
        return [], []

    from scipy.spatial import Delaunay
    from shapely.geometry import Polygon as _Poly
    arr = np.asarray(pts, dtype=np.float64)
    try:
        dt = Delaunay(arr)
    except Exception:
        return _earcut_fallback(poly)

    # Keep a triangle when the MAJORITY of its area lies inside the union.  A
    # plain centroid-in-poly test dropped fringe bridge triangles whose centroid
    # fell a hair outside a concave notch, which both lost that ground and
    # severed a corner of the surface into a point-touching speck (Chorrol's top
    # floor and basement each shed a 3-triangle scrap the main mesh does NOT
    # cover, so it could not simply be deleted).  The area test keeps a triangle
    # that is mostly walkable ground and only discards one that mostly pokes past
    # the boundary — preserving coverage and connectivity together.
    tris = []
    for (a, b, c) in dt.simplices:
        pa, pb, pc = arr[a], arr[b], arr[c]
        cx = (pa[0] + pb[0] + pc[0]) / 3.0
        cy = (pa[1] + pb[1] + pc[1]) / 3.0
        if pp.contains(Point(cx, cy)):
            tris.append((int(a), int(b), int(c)))
            continue
        # centroid outside — keep only if most of the triangle is inside
        tri = _Poly([(pa[0], pa[1]), (pb[0], pb[1]), (pc[0], pc[1])])
        ta = tri.area
        if ta < 1e-6:
            continue
        try:
            inside = tri.intersection(poly).area
        except Exception:
            inside = 0.0
        if inside >= 0.5 * ta:
            tris.append((int(a), int(b), int(c)))
    verts = [(float(p[0]), float(p[1])) for p in arr]
    if not tris:
        return _earcut_fallback(poly)
    # Delaunay does not GUARANTEE a constraint edge: seeding its endpoints and
    # keeping other samples clear only makes it likely, and when the surrounding
    # geometry won, a door's base line came out split — the door then touched the
    # mesh at a single VERTEX with a rogue triangle hanging off it.  Recover each
    # constraint explicitly, which keeps the door inside the ONE triangulation
    # (so it still shares edges with its neighbours) while making its long side a
    # real edge.
    if fixed_pts:
        verts, tris = _recover_constraints(verts, tris, fixed_pts)
    return verts, tris


def _recover_constraints(verts, tris, segments):
    """Force each segment to appear as a triangle edge.

    Any triangle whose interior the segment crosses is split at the crossing
    points: the segment's intersections with that triangle's edges become
    vertices, and the triangle is re-fanned around them.  The result stays a
    valid triangulation of the same area — no triangle is dropped and no new
    ground is invented — but the segment now runs along triangle edges.
    """
    verts = [list(v) for v in verts]
    tris = [tuple(t) for t in tris]

    index = {}
    for i, v in enumerate(verts):
        index.setdefault((round(v[0], 3), round(v[1], 3)), i)

    def vid(x, y):
        key = (round(x, 3), round(y, 3))
        i = index.get(key)
        if i is None:
            i = len(verts)
            verts.append([float(x), float(y)])
            index[key] = i
        return i

    for (p0, p1) in segments:
        ax, ay = float(p0[0]), float(p0[1])
        bx, by = float(p1[0]), float(p1[1])
        if math.hypot(bx - ax, by - ay) < 1e-9:
            continue
        for _round in range(4):
            out = []
            changed = False
            for t in tris:
                pts = [verts[t[0]], verts[t[1]], verts[t[2]]]
                if _has_edge(verts, t, ax, ay, bx, by):
                    out.append(t)
                    continue
                cuts = _segment_cuts(pts, ax, ay, bx, by)
                if len(cuts) < 2:
                    out.append(t)
                    continue
                out.extend(_split_triangle(t, pts, cuts, vid, verts))
                changed = True
            tris = out
            if not changed:
                break
    return [tuple(v) for v in verts], tris


def _has_edge(verts, t, ax, ay, bx, by):
    """True if the triangle already has an edge lying along the segment."""
    for k in range(3):
        p = verts[t[k]]
        q = verts[t[(k + 1) % 3]]
        if (_near(p, ax, ay) and _near(q, bx, by)) or \
                (_near(p, bx, by) and _near(q, ax, ay)):
            return True
    return False


def _near(p, x, y):
    return abs(p[0] - x) < 1e-6 and abs(p[1] - y) < 1e-6


def _segment_cuts(pts, ax, ay, bx, by):
    """Points where the segment crosses this triangle's edges (deduped)."""
    cuts = []
    for k in range(3):
        p, q = pts[k], pts[(k + 1) % 3]
        hit = _seg_intersect(p[0], p[1], q[0], q[1], ax, ay, bx, by)
        if hit is None:
            continue
        if not any(abs(hit[0] - c[0]) < 1e-6 and abs(hit[1] - c[1]) < 1e-6
                   for c in cuts):
            cuts.append(hit)
    return cuts


def _seg_intersect(x1, y1, x2, y2, x3, y3, x4, y4):
    d = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3)
    if abs(d) < 1e-12:
        return None
    t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / d
    u = ((x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)) / d
    if t < -1e-9 or t > 1 + 1e-9 or u < -1e-9 or u > 1 + 1e-9:
        return None
    return (x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)


def _split_triangle(t, pts, cuts, vid, verts):
    """Re-triangulate one triangle around the two points the segment cuts."""
    c0 = vid(cuts[0][0], cuts[0][1])
    c1 = vid(cuts[1][0], cuts[1][1])
    if c0 == c1:
        return [t]
    ring = []
    for k in range(3):
        ring.append(t[k])
        p, q = pts[k], pts[(k + 1) % 3]
        on = [c for c in (c0, c1)
              if _on_segment(verts[c], p, q) and
              not _near(verts[c], p[0], p[1]) and
              not _near(verts[c], q[0], q[1])]
        on.sort(key=lambda c: (verts[c][0] - p[0]) ** 2 +
                (verts[c][1] - p[1]) ** 2)
        ring.extend(on)
    ring = [v for i, v in enumerate(ring) if v not in ring[:i]]
    if len(ring) < 3:
        return [t]
    out = []
    for i in range(1, len(ring) - 1):
        tri = (ring[0], ring[i], ring[i + 1])
        if len(set(tri)) == 3:
            out.append(tri)
    return out or [t]


def _on_segment(c, p, q):
    cross = ((q[0] - p[0]) * (c[1] - p[1]) - (q[1] - p[1]) * (c[0] - p[0]))
    if abs(cross) > 1e-6:
        return False
    dot = (c[0] - p[0]) * (q[0] - p[0]) + (c[1] - p[1]) * (q[1] - p[1])
    if dot < -1e-9:
        return False
    return dot <= (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2 + 1e-9


def _seg_dist2(px, py, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    d2 = dx * dx + dy * dy
    t = 0.0 if d2 < 1e-9 else max(0.0, min(1.0, ((px - a[0]) * dx +
                                                 (py - a[1]) * dy) / d2))
    ddx = px - (a[0] + dx * t)
    ddy = py - (a[1] + dy * t)
    return ddx * ddx + ddy * ddy


def _earcut_fallback(poly):
    """Plain earcut of a polygon — used only if Delaunay fails on a piece."""
    import mapbox_earcut as earcut

    rings = [list(poly.exterior.coords)[:-1]]
    for r in poly.interiors:
        rings.append(list(r.coords)[:-1])
    rings = [r for r in rings if len(r) >= 3]
    if not rings:
        return [], []
    flat = []
    ring_ends = []
    for r in rings:
        for (x, y) in r:
            flat.append([float(x), float(y)])
        ring_ends.append(len(flat))
    arr = np.asarray(flat, dtype=np.float64)
    try:
        idx = earcut.triangulate_float64(arr, np.asarray(ring_ends,
                                                         dtype=np.uint32))
    except Exception:
        return [], []
    verts = [(float(p[0]), float(p[1])) for p in arr]
    tris = [(int(idx[i]), int(idx[i + 1]), int(idx[i + 2]))
            for i in range(0, len(idx) - 2, 3)]
    return verts, tris


def _polygons_of(geom):
    from shapely.geometry import Polygon, MultiPolygon
    if isinstance(geom, Polygon):
        return [geom] if geom.area > 1e-6 else []
    if isinstance(geom, MultiPolygon):
        return [g for g in geom.geoms if g.area > 1e-6]
    if hasattr(geom, 'geoms'):
        out = []
        for g in geom.geoms:
            out.extend(_polygons_of(g))
        return out
    return []


def _ribbon_seeds(strips, target_edge):
    """Interior seed points down every ribbon centreline (stairs get more).

    Two jobs:

      * CONNECTIVITY.  A corridor is only ~one ribbon wide (80u); at a 128u
        target edge it gets no interior hex-lattice row, so a bend in it is
        triangulated by long triangles whose centroids fall outside the bend and
        are culled — silently snapping the corridor into disconnected pieces
        (ChorrolFightersGuild fell into 10 components).  A row of centreline
        points down every ribbon guarantees a triangle chain that stays inside.

      * STAIRS.  A ribbon that climbs more than half a storey gap over a
        target_edge run is a stair: one uniform triangle on it would span more
        than STOREY_GAP_Z across its corners and be dropped by the per-surface
        emission, and the whole stair vanishes (Pinarus's two floors, 268u
        apart, on a single 2-node edge).  Steep ribbons are sampled MUCH finer,
        along the centreline and both rails, so the stair keeps short,
        full-width, gently-climbing triangles.

    On flat open ground the Poisson guard rejects most of these in favour of the
    coarse hex lattice, so rooms stay large-triangled.
    """
    seeds = []
    for s in strips:
        ax, ay, az = s['a']
        bx, by, bz = s['b']
        run = math.hypot(bx - ax, by - ay)
        if run < 1e-3:
            continue
        wx, wy = s['w']
        h = s['half']
        rise = abs(bz - az)
        steep = rise / run * target_edge > STOREY_GAP_Z * 0.5
        if steep:
            # spacing so the climb per step is ~a third of the storey gap
            climb_step = STOREY_GAP_Z * 0.33
            step = max(RIBBON_SEED_STEP, climb_step * run / max(rise, 1e-6))
            offs = (-h * 0.6, 0.0, h * 0.6)
        else:
            step = target_edge * 0.9         # ~one triangle per along-corridor
            offs = (0.0,)                    # centreline only; Poisson thins it
        n = max(1, int(run / step))
        for k in range(n + 1):
            f = k / n
            cx, cy = ax + (bx - ax) * f, ay + (by - ay) * f
            for off in offs:
                seeds.append((cx + wx * off, cy + wy * off, steep))
    return seeds


# Along-ribbon spacing of steep-ribbon (stair) seeds.  RIBBON_STEP-scale so a
# stair keeps the fine cross-sections the old 8u grid gave it.
RIBBON_SEED_STEP = 24.0


def wall_cuts(blocking, z_lo, z_hi):
    """Thin 2D polygons for every wall standing between z_lo and z_hi.

    The union merges all the ribbons into ONE polygon and triangulates it, with
    no notion of collision — so a ribbon on each side of a wall becomes one
    region and the triangulation spans straight through the wall (measured on
    Pinarus's house: 438 of 575 triangles had an edge crossing a wall, doors not
    involved).  Subtracting these cuts SPLITS the polygon along every wall, so a
    triangle physically cannot bridge one.

    Each near-vertical blocking triangle contributes its footprint segment,
    buffered to a hairline so the subtraction actually separates the sides.
    """
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    B = np.asarray(blocking, dtype=float).reshape(-1, 3, 3)
    if not len(B):
        return None
    segs = []
    for tri in B:
        if tri[:, 2].max() < z_lo or tri[:, 2].min() > z_hi:
            continue
        # Footprint of a wall triangle: its longest projected edge.
        best = None
        for k in range(3):
            p, q = tri[k], tri[(k + 1) % 3]
            d2 = (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2
            if best is None or d2 > best[0]:
                best = (d2, p, q)
        if best is None or best[0] < 1.0:
            continue
        _d2, p, q = best
        segs.append(LineString([(p[0], p[1]), (q[0], q[1])]))
    if not segs:
        return None
    try:
        return unary_union(segs).buffer(WALL_CUT_WIDTH)
    except Exception:
        return None


def build_union_mesh(strips, extra_strips=None, door_edges=None,
                     cell_bounds=None, wall_cut=None):
    """Union the corridor ribbons per storey and retriangulate.

    Returns (verts, tris) with 3D vertices.  Coverage is the exact union of the
    ribbons and the triangles do not overlap — both by construction.

    extra_strips: door FOOTPRINT strips (from corridor_doors.door_footprints via
    _poly_strip) that join the union as ordinary ground — the flat connection
    quad from each door base to the nearest corridor edge.  Their COVERAGE is
    preserved exactly; the union resolves any overlap with the corridor.

    door_edges: [((x0,y0), (x1,y1)), ...] the door BASE lines.  Each is forced
    to appear as a triangle edge in the retriangulation, so every door gets one
    large triangle with its long side on the door line — the vanilla Skyrim door
    triangle — instead of whatever the generic mesh happens to lay there.

    cell_bounds: (minx, miny, maxx, maxy) — when given (exterior cells), the
    unioned coverage is CLIPPED to this rectangle before triangulation, so a
    cross-seam ribbon (built from a PGRI InterCell link that reaches into the
    neighbour cell) stops exactly on the boundary plane.  That leaves a border
    edge on the seam for build_edge_links to stitch, while each mesh stays
    strictly within its own cell.
    """
    from shapely.geometry import Polygon, MultiPolygon, box
    from shapely.ops import unary_union

    if not strips:
        return [], []

    # Door footprints participate as ordinary geometry: they contribute their
    # polygon to the union AND their (flat) height to the level lookup, so a
    # vertex standing on door-only ground still knows how high it is.
    strips = list(strips) + list(extra_strips or ())

    verts = []
    tris = []

    # ONE 2D union of every ribbon, retriangulated once.  No storey buckets: a
    # staircase has no single height, so any attempt to assign corridors to
    # floors forces one Z threshold to be both loose enough for a stair's slope
    # and tight enough for a 200u floor gap — which no value satisfies.
    polys = [p for p in (_ribbon_polygon(s) for s in strips)
             if p.is_valid and not p.is_empty]
    if not polys:
        return [], []
    merged = unary_union(polys)
    if merged.is_empty:
        return [], []
    # Clip to the cell rectangle (exterior only): a cross-seam ribbon is cut at
    # the boundary plane, and shapely re-polygonises the result cleanly.
    if cell_bounds is not None:
        minx, miny, maxx, maxy = cell_bounds
        merged = merged.intersection(box(minx, miny, maxx, maxy))
        if merged.is_empty:
            return [], []
    # Split the coverage along every wall, so no triangle can span one.
    if wall_cut is not None:
        try:
            cut = merged.difference(wall_cut)
            if not cut.is_empty:
                merged = cut
        except Exception:
            pass
    # The clip may turn one polygon into a MultiPolygon or drop degenerate
    # slivers to lines/points inside a GeometryCollection; keep only polygons.
    if hasattr(merged, 'geoms'):
        parts = [g for g in merged.geoms if isinstance(g, Polygon)]
    else:
        parts = [merged] if isinstance(merged, Polygon) else []

    # Steep-ribbon centreline seeds, computed once for all parts.  A ribbon is
    # "steep" when a target_edge-long triangle laid on it would climb more than
    # half a storey gap — a stair.  Such a triangle, spanning >STOREY_GAP_Z
    # across its corners, is split apart by the per-surface emission and the
    # whole stair vanishes (Pinarus's two floors, 268u apart, joined by a single
    # 2-node stair edge).  We seed the stair centreline finely so its triangles
    # stay short and climb little.
    steep_seeds = _ribbon_seeds(strips, params.TRI_TARGET_EDGE)

    # Each output vertex is emitted ONCE PER SURFACE that covers it: where two
    # storeys stack, the same (x, y) yields one vertex per storey, at each
    # storey's own height.  Surfaces are found by clustering the heights of the
    # corridors covering that point — heights within SAME_SURFACE_Z of each
    # other are one surface, a bigger jump is a different storey.  This is the
    # local, per-point version of the test; nothing is classified globally.
    door_edges = door_edges or []
    # PER-STOREY UNION.  A single flattened union merges floors that sit on top of
    # each other in plan view, and the triangulation then bridges them: measured
    # in ChorrolFightersGuild, 15 triangles had corners on the -302 floor AND the
    # -45 floor at once, 3-46u from a walked pathgrid line.  They are not
    # stairwell edges — they are the upper and lower ribbons overlapping in plan.
    # Emitting them stacks a near-vertical sheet between the storeys ("triangles
    # between floors"); dropping them severs 24 shared edges and splits the floor
    # into 7 pieces.  Neither is right, because the flattened polygon was never
    # the correct region to triangulate.
    #
    # So the ribbons are grouped into storeys FIRST and each storey is unioned and
    # triangulated on its own.  Within a storey there is exactly one surface, so a
    # triangle can no longer span two floors and every corner has an unambiguous
    # height.  Stairs are the reason this must group by CONNECTIVITY rather than
    # by a Z threshold: a flight has no single height, so it is walked from ribbon
    # to ribbon (see _storey_groups) and stays attached to both the floor it
    # leaves and the floor it reaches.
    sheets = _split_plan_overlaps(_storey_groups(strips))
    # SHARED NODE POINTS.  A pathgrid node where two sheets meet is the one place
    # they MUST connect — it is the top or bottom of a staircase.  Measured on
    # Pinarus: node 1 is the stair top, its stair ribbon (0,1) landed in one sheet
    # and the upper floor's ribbon (1,8) in another, and because each sheet is
    # triangulated independently the two nearest vertices came out 31u apart —
    # far beyond the weld radius, so the house stayed in two components with the
    # break exactly at the top of the stairs.
    #
    # Forcing the node's own XY into EVERY sheet that has a ribbon there makes
    # both sheets place a vertex at the same point, at the same height (the node's
    # ribbons agree on it by construction), so `_weld_sheets` fuses them and the
    # surfaces share real edges.
    node_pts = {}
    node_half = {}
    for s in strips:
        (i, j) = s.get('edge', (-1, -1))
        if i < 0:
            continue
        node_pts.setdefault(i, (s['na'][0], s['na'][1]))
        node_half[i] = max(node_half.get(i, 0.0), float(s['half']))
        if j != i:
            node_pts.setdefault(j, (s['nb'][0], s['nb'][1]))
            node_half[j] = max(node_half.get(j, 0.0), float(s['half']))

    # Which nodes are shared between two or more sheets?  Those, and only those,
    # are the stair tops/bottoms that must be stitched.
    node_sheets = {}
    for gi, group in enumerate(sheets):
        for s in group:
            (i, j) = s.get('edge', (-1, -1))
            if i < 0:
                continue
            node_sheets.setdefault(i, set()).add(gi)
            node_sheets.setdefault(j, set()).add(gi)

    sheet_nodes = []
    for gi, group in enumerate(sheets):
        ids = set()
        for s in group:
            (i, j) = s.get('edge', (-1, -1))
            if i < 0:
                continue
            ids.add(i)
            ids.add(j)
        sheet_nodes.append([node_pts[i] for i in sorted(ids) if i in node_pts])

    # Nodes shared by 2+ sheets are the stair tops/bottoms.  Seeding alone can
    # only ever give them a shared POINT — two independently triangulated polygons
    # meeting at one vertex form a fan around it and share no EDGE, so NVNM
    # adjacency cannot link them (measured on Pinarus: v152 at the stair top used
    # by both components, still 2 components).  They are stitched explicitly after
    # all sheets are meshed; see _stitch_shared_nodes.
    stitch_nodes = [(node_pts[i][0], node_pts[i][1])
                    for i, gset in node_sheets.items()
                    if len(gset) >= 2 and i in node_pts]

    # A ribbon that belongs to one sheet may still be COVERED by another sheet's
    # polygon where the two floors stack.  Each sheet is therefore triangulated
    # over its own ribbons only; the overlap is resolved in 3D by the Z of the
    # ribbons themselves, which is why per-sheet levels (below) are correct.
    # Ground already claimed by an earlier sheet, as (polygon, sheet index).  Two
    # sheets that meet at a shared floor level (Chorrol's sheet0 spans z -45..143
    # and sheet1 z -302..-40, so they meet around z=-45) otherwise BOTH mesh that
    # ground: each sheet alone measured ZERO overlap, while 12 overlapping pairs
    # existed across sheets.  Each piece of ground must have exactly one owner, so
    # a later sheet is clipped against the parts of earlier sheets that describe
    # the SAME surface height there.
    # THE JUNCTION UNION.
    #
    # Corridors that meet at a pathgrid node must come out as ONE merged surface.
    # Where both ribbons are in the same sheet the union does that already.  Where
    # the sheet split separated them (a staircase genuinely conflicts in plan with
    # the floor it passes UNDER, so no scoring can keep it with the landing it
    # arrives at) the junction has to be unioned explicitly — and it must be
    # unioned into exactly ONE sheet, never kept by both.  Keeping it in both is
    # not a union at all: each sheet triangulates that ground independently and
    # the result is stacked, overlapping triangles (measured on Pinarus: 16 pairs
    # of same-surface triangles overlapping by 5,582u^2).
    #
    # So each node is OWNED by the first sheet that reaches it, and that sheet
    # unions in the far ribbon of every edge arriving from another sheet, clipped
    # to the node's own corridor width.  The junction is then a single polygon in
    # a single sheet — one triangulation, no stacking — while the far sheet is
    # clipped against it by the normal `claimed` pass below, exactly as any other
    # shared ground is.
    node_owner = {}
    for gi, group in enumerate(sheets):
        for s in group:
            (i, j) = s.get('edge', (-1, -1))
            if i < 0:
                continue
            for nd in ((i,) if j == i else (i, j)):
                node_owner.setdefault(nd, gi)

    junction_extra = {}
    junction_strips = {}
    junction_drop = {}
    if node_pts:
        from shapely.geometry import Point as _Point
        sheet_of = {}
        for gi, group in enumerate(sheets):
            for s in group:
                sheet_of[s.get('edge', (-1, -1))] = gi
        for s in strips:
            (i, j) = s.get('edge', (-1, -1))
            if i < 0:
                continue
            gi = sheet_of.get((i, j))
            if gi is None:
                continue
            for nd in ((i,) if j == i else (i, j)):
                own = node_owner.get(nd)
                if own is None or own == gi or nd not in node_pts:
                    continue
                # This ribbon reaches a node owned by ANOTHER sheet: give that
                # sheet the ribbon's ground at the node so the two merge there.
                nx, ny = node_pts[nd]
                r = max(float(node_half.get(nd, 0.0)),
                        params.RIBBON_HALF_WIDTH)
                try:
                    piece = _ribbon_polygon(s).intersection(
                        _Point(nx, ny).buffer(r))
                except Exception:
                    continue
                if piece.is_empty or piece.area < 1.0:
                    continue
                junction_extra.setdefault(own, []).append(piece)
                # The far sheet keeps its centreline height there, so the merged
                # polygon still knows how high the arriving corridor is.
                junction_strips.setdefault(own, []).append(s)
                # ...and the sheet that does NOT own the node gives that ground
                # up.  Ownership has to be EXCLUSIVE or this is not a union at
                # all: both sheets would triangulate the junction independently
                # and the two results stack (measured before this subtraction:
                # Chorrol 135 same-surface triangle pairs overlapping by
                # 90,947u^2, Pinarus 20 pairs / 3,448u^2).
                junction_drop.setdefault(gi, []).append(piece)

    claimed = []
    for gi, group in enumerate(sheets):
        gpolys = [p for p in (_ribbon_polygon(s) for s in group)
                  if p.is_valid and not p.is_empty]
        gpolys.extend(junction_extra.get(gi, ()))
        if not gpolys:
            continue
        gmerged = unary_union(gpolys)
        drop = junction_drop.get(gi)
        if drop:
            try:
                cut = gmerged.difference(unary_union(drop))
                if not cut.is_empty:
                    gmerged = cut
            except Exception:
                pass
        if cell_bounds is not None:
            minx, miny, maxx, maxy = cell_bounds
            gmerged = gmerged.intersection(box(minx, miny, maxx, maxy))
        for (prev_poly, prev_group) in claimed:
            if gmerged.is_empty:
                break
            try:
                shared_area = gmerged.intersection(prev_poly)
            except Exception:
                continue
            if shared_area.is_empty or shared_area.area < 1.0:
                continue
            # Only surrender ground where the two sheets agree on the HEIGHT —
            # where they disagree they are different storeys stacked in plan and
            # both must keep their own mesh.
            dup = _same_surface_region(group, prev_group, shared_area)
            if dup is None or dup.is_empty:
                continue
            try:
                trimmed = gmerged.difference(dup)
            except Exception:
                continue
            if not trimmed.is_empty:
                gmerged = trimmed
        if gmerged.is_empty:
            continue
        claimed.append((gmerged, group))
        if wall_cut is not None:
            try:
                gcut = gmerged.difference(wall_cut)
                if not gcut.is_empty:
                    gmerged = gcut
            except Exception:
                pass
        if gmerged.is_empty:
            continue
        gparts = ([g for g in gmerged.geoms if isinstance(g, Polygon)]
                  if hasattr(gmerged, 'geoms')
                  else ([gmerged] if isinstance(gmerged, Polygon) else []))
        # The ribbons arriving from another sheet at a node THIS sheet owns are
        # part of this sheet's surface now (see the junction union above), so they
        # must contribute their centreline heights and their seeds — otherwise the
        # merged ground is triangulated here but takes its height only from the
        # local ribbons, and the arriving corridor's end is flattened onto this
        # floor instead of keeping its own slope.
        group = list(group) + junction_strips.get(gi, [])
        gseeds = _ribbon_seeds(group, params.TRI_TARGET_EDGE)
        # Every pathgrid node of this sheet is a FORCED seed (the True flag), so a
        # node shared with another sheet becomes a vertex in both and the weld can
        # fuse them.  These are the stair tops/bottoms.
        gseeds = list(gseeds) + [(nx, ny, True) for (nx, ny) in sheet_nodes[gi]]
        for part in gparts:
            if not isinstance(part, Polygon) or part.area < 1.0:
                continue
            fixed = [e for e in door_edges
                     if _point_in_poly(0.5 * (e[0][0] + e[1][0]),
                                       0.5 * (e[0][1] + e[1][1]),
                                       list(part.exterior.coords)[:-1])]
            v2, t2 = _triangulate(part, params.TRI_TARGET_EDGE,
                                  fixed_edges=fixed, steep_seeds=gseeds)
            if not t2:
                continue
            # Levels come from THIS storey's ribbons only, so a corner cannot
            # pick up the other floor's height.
            levels = _levels_batch(group, v2)
            v3, t3 = _emit_surfaces(v2, t2, levels)
            base = len(verts)
            verts.extend(v3)
            tris.extend((a + base, b + base, c + base) for (a, b, c) in t3)

    # Each sheet was triangulated on its own, so where two sheets meet on the
    # SAME surface their boundary vertices are coincident but carry different
    # indices — they share no edge, and the engine cannot walk between them.
    # Weld those together (a 3D weld, so two storeys stacked in plan are never
    # fused: they are hundreds of units apart in Z).
    verts, tris = _weld_sheets(verts, tris)
    tris = _split_t_junctions(verts, tris)
    tris = _stitch_shared_nodes(verts, tris, stitch_nodes)
    # THE GUARANTEE: corridors that meet at a pathgrid node are merged, EVERY
    # TIME.  Runs last, over the finished surface, and is driven by the PATHGRID
    # rather than by any property of the geometry — so there is no case it can
    # decline to handle.  See _merge_at_pathgrid_nodes.
    verts, tris = _merge_at_pathgrid_nodes(verts, tris, node_pts, node_half)
    tris = _drop_point_attached(tris)
    return verts, tris


def _merge_at_pathgrid_nodes(verts, tris, node_pts, node_half):
    """Guarantee that the corridors meeting at each pathgrid node are ONE
    connected surface.

    The pathgrid is the one input that asserts where an actor walks, so two
    edges meeting at a node describe a junction an actor walks through.  The
    navmesh must therefore be walkable across it — not merely present on both
    sides.

    Everything upstream tries to arrange this and can each fail for its own
    reason: the sheet split can put the two corridors in different sheets (a
    staircase genuinely conflicts with the floor it passes UNDER, so no scoring
    keeps it with the landing it arrives at); the clip can then take the
    junction ground as "duplicate"; the 3D weld only fuses vertices that already
    coincide; and a bridge triangle cannot be laid where the surrounding fan is
    already closed.  Measured on AnvilPinarusInventiusHouse the result was a
    flight whose top row sat ~32u BELOW its landing at nearly the same XY (v120
    (-255.8,132.9,36.8) vs v80 (-255.4,134.9,68.6)), joined only by skew edges
    hanging off a vertex 61u to the SIDE: 191.7u of joint at the top of the
    flight against 686.4u at its bottom.  One component, so no invariant caught
    it — and in game the navmesh at the top of the stairs did not connect.

    So this pass does not try to prevent the split; it repairs it afterwards,
    unconditionally, at every node:

      1. Collect the mesh vertices within the node's own corridor half-width.
      2. Group them by SURFACE (heights within one MAX_CLIMB step are the same
         walkable level, so a stair top and its landing are one group while a
         floor two storeys down is not).
      3. Where a group holds vertices from two or more edge-connected
         components, weld them onto the single vertex nearest the node.

    Welding — rather than adding triangles — is what makes this total: it needs
    no border edge, cannot raise an edge above two owners, and cannot invent
    ground.  Triangles that collapse to a degenerate are dropped, which is
    exactly the duplicate sliver at the seam.
    """
    if not tris or not node_pts:
        return verts, tris

    verts = [list(v) for v in verts]
    remap = list(range(len(verts)))

    def resolve(i):
        while remap[i] != i:
            remap[i] = remap[remap[i]]
            i = remap[i]
        return i

    for ni, (nx, ny) in sorted(node_pts.items()):
        r = max(float(node_half.get(ni, 0.0)), params.RIBBON_HALF_WIDTH)
        comp = _tri_components(tris)
        vcomp = {}
        for ti, t in enumerate(tris):
            for i in t:
                vcomp.setdefault(resolve(i), set()).add(comp[ti])
        near = [i for i in vcomp
                if math.hypot(verts[i][0] - nx, verts[i][1] - ny) <= r]
        if len(near) < 2:
            continue
        # Group by walkable surface: one step apart is the same surface, a
        # storey apart is not.  (Sorting makes the banding deterministic, which
        # the byte-reproducibility contract requires.)
        # Band on the STOREY gap, not on one step.  Two corridors meeting at a
        # node are the same junction even when the sheets left them a step or two
        # apart in Z — that disagreement is precisely the defect being repaired,
        # so a one-step band refuses to weld exactly the pair that needs it
        # (measured at Pinarus's stair top: v120 at z=36.8 and v80 at z=68.6, 2.0u
        # apart in plan but 31.8u in Z).  Only a genuine storey above or below the
        # junction must stay separate, and that is STOREY_GAP_Z away.
        near.sort(key=lambda i: (verts[i][2], i))
        bands = [[near[0]]]
        for i in near[1:]:
            if verts[i][2] - verts[bands[-1][-1]][2] <= STOREY_GAP_Z:
                bands[-1].append(i)
            else:
                bands.append([i])
        for band in bands:
            comps = set()
            for i in band:
                comps |= vcomp.get(i, set())
            if len(band) < 2 or len(comps) < 2:
                continue                # already one surface here
            keep = min(band, key=lambda i: (
                math.hypot(verts[i][0] - nx, verts[i][1] - ny), i))
            for i in band:
                if i != keep:
                    remap[i] = keep
        tris = [t for t in ((resolve(a), resolve(b), resolve(c))
                            for (a, b, c) in tris) if len(set(t)) == 3]

    tris = [t for t in ((resolve(a), resolve(b), resolve(c))
                        for (a, b, c) in tris) if len(set(t)) == 3]
    return verts, tris


def _stitch_shared_nodes(verts, tris, stitch_nodes):
    """Give two sheets meeting at a pathgrid node real SHARED EDGES.

    A pathgrid node shared by two sheets is a staircase top or bottom — the one
    place the two surfaces must connect.  Forcing the node in as a seed makes both
    sheets place a vertex at the same point, but a shared point is not enough: the
    triangles fan around it and share no edge, and NVNM adjacency links only
    across shared edges, so the mesh stays in two components with the break
    exactly at the top of the stairs (measured on Pinarus: v152, both components
    present, gap 0.000u, still disconnected).

    At each such node this bridges the two sides directly: take a border edge from
    each component incident to the node and emit the triangle joining them.  That
    single triangle shares one full edge with each side, so the two components
    become one.  Only border edges AT the node are used, and only between
    DIFFERENT components, so nothing already connected is touched and no
    triangle is created away from a node the pathgrid actually walks.
    """
    if not tris:
        return tris

    for _round in range(3):
        counts = {}
        for t in tris:
            for k in range(3):
                a, b = t[k], t[(k + 1) % 3]
                key = (a, b) if a < b else (b, a)
                counts[key] = counts.get(key, 0) + 1
        border = [e for e, c in counts.items() if c == 1]
        if not border:
            break
        comp = _tri_components(tris)
        vcomp = {}
        for ti, t in enumerate(tris):
            for i in t:
                vcomp.setdefault(i, set()).add(comp[ti])
        # border edges incident to each vertex, with the component that owns them
        inc = {}
        owner = {}
        for ti, t in enumerate(tris):
            for k in range(3):
                a, b = t[k], t[(k + 1) % 3]
                key = (a, b) if a < b else (b, a)
                if counts.get(key) == 1:
                    owner[key] = comp[ti]
                    inc.setdefault(a, []).append(key)
                    inc.setdefault(b, []).append(key)

        # Drive this from the GEOMETRY, not only from the pathgrid node list: a
        # junction shows up as a vertex used by two different components, and
        # Pinarus has two such points (the stair top at (-316.9, 134.9) AND a
        # second stair node at (-318.5, -88.0)), each also spawning small
        # point-attached scraps.  Stitching only the sheet-shared nodes fixed one
        # and left the other, so the house stayed in two pieces.
        junctions = sorted(i for i, cs in vcomp.items() if len(cs) > 1)
        added = []
        # Edges the bridges add this round, so two bridges cannot between them
        # push one edge past two owners.
        extra = {}
        # Triangle index -> replacement pair, for fans opened below.
        replaced = {}
        for i0v in junctions:
            cands = [i0v]
            # group the border edges at this junction by component
            by_comp = {}
            for i in cands:
                for key in inc.get(i, ()):
                    by_comp.setdefault(owner[key], []).append((i, key))
            # A component may USE the junction while presenting no BORDER edge
            # there: the other surface arrives into the MIDDLE of its fan, so the
            # fan is closed all the way round and every edge at the junction
            # already has two owners.  A bridge triangle cannot help then —
            # `_compute_adjacency` links an edge shared by 3+ triangles to
            # NOTHING, so laying a bridge on one SEVERS the fan it landed on
            # instead of joining it.  Measured at Pinarus's stair top: the landing
            # offered 8 triangles at the junction and 0 border slots, the flight 2
            # and 2; every candidate bridge died on the manifold guard and the
            # mesh stayed in two pieces.
            #
            # OPEN the fan instead — split one of that component's triangles at
            # the junction in two by inserting the midpoint of its opposite edge.
            # The split is area-preserving and purely internal (it re-meshes the
            # same ground) and leaves a fresh border edge at the junction for the
            # bridge below to use legitimately.  The neighbour across the split
            # edge is split to match, so that edge keeps exactly two owners.
            for i in cands:
                for c in sorted(vcomp.get(i, ())):
                    if c in by_comp:
                        continue
                    cand_t = [ti for ti, t in enumerate(tris)
                              if comp[ti] == c and i in t and ti not in replaced]
                    if not cand_t:
                        continue
                    ti = max(cand_t, key=lambda x: _tri_area(verts, tris[x]))
                    t = tris[ti]
                    k = t.index(i)
                    p, q = t[(k + 1) % 3], t[(k + 2) % 3]
                    okey = (p, q) if p < q else (q, p)
                    nb = [tj for tj, tt in enumerate(tris)
                          if tj != ti and tj not in replaced
                          and p in tt and q in tt]
                    if counts.get(okey, 0) > 1 and not nb:
                        continue          # cannot keep the opposite edge manifold
                    # The split must not manufacture a near-VERTICAL or degenerate
                    # triangle: the halves inherit the parent's corners plus the
                    # midpoint of (p,q), so a parent that already spans a big drop
                    # (a stairwell-edge triangle) would hand both halves that drop
                    # and the result reads as a wall, not floor.  Splitting those
                    # is what added OPPOSITE_NORMALS/DOWNFACING triangles to
                    # ImperialSewers03 and Bruma.
                    if abs(verts[p][2] - verts[q][2]) > params.MAX_CLIMB:
                        continue
                    if _tri_area(verts, t) < 4.0 * params.MIN_XY_FOOTPRINT:
                        continue
                    mid = len(verts)
                    verts.append([0.5 * (verts[p][0] + verts[q][0]),
                                  0.5 * (verts[p][1] + verts[q][1]),
                                  0.5 * (verts[p][2] + verts[q][2])])
                    replaced[ti] = [(i, p, mid), (i, mid, q)]
                    for tj in nb[:1]:
                        tt = tris[tj]
                        opp = [x for x in tt if x != p and x != q]
                        if len(opp) == 1:
                            replaced[tj] = [(opp[0], p, mid), (opp[0], mid, q)]
                    key = (i, mid) if i < mid else (mid, i)
                    owner[key] = c
                    counts[key] = 1
                    by_comp.setdefault(c, []).append((i, key))
                    counts.pop(okey, None)
            if len(by_comp) < 2:
                continue
            order = sorted(by_comp)
            base_c = order[0]
            for other_c in order[1:]:
                made = False
                for (i0, k0) in by_comp[base_c]:
                    if made:
                        break
                    a0 = k0[0] if k0[1] == i0 else k0[1]
                    for (i1, k1) in by_comp[other_c]:
                        a1 = k1[0] if k1[1] == i1 else k1[1]
                        tri = (a0, i0, a1)
                        if len(set(tri)) < 3:
                            tri = (a0, i0, i1)
                        if len(set(tri)) < 3:
                            continue
                        # Only accept a reasonably shaped, small bridge so this
                        # cannot sew two distant surfaces together.
                        if _tri_span(verts, tri) > 160.0:
                            continue
                        # ...and never a near-VERTICAL one.  A bridge whose
                        # corners differ by more than a step is the unnavigable
                        # flap this module already fought once — a triangle an
                        # actor would climb rather than walk.
                        zs = [verts[x][2] for x in tri]
                        if max(zs) - min(zs) > params.MAX_CLIMB:
                            continue
                        # MANIFOLD GUARD: every edge the bridge introduces must
                        # end with at most TWO owners, or adjacency links none of
                        # them and the bridge disconnects rather than joins.
                        if any(counts.get(e, 0) + extra.get(e, 0) >= 2
                               for e in _tri_edges(tri)):
                            continue
                        for e in _tri_edges(tri):
                            extra[e] = extra.get(e, 0) + 1
                        added.append(tri)
                        made = True
                        break
        if not added and not replaced:
            break
        out = []
        for ti, t in enumerate(tris):
            out.extend(replaced.get(ti, [t]))
        tris = [t for t in out + added if len(set(t)) == 3]
    return tris


def _tri_edges(tri):
    """The triangle's three edges as sorted (lo, hi) keys."""
    return [(tri[k], tri[(k + 1) % 3]) if tri[k] < tri[(k + 1) % 3]
            else (tri[(k + 1) % 3], tri[k]) for k in range(3)]


def _tri_area(verts, tri):
    """XY-projected area of a triangle."""
    a, b, c = verts[tri[0]], verts[tri[1]], verts[tri[2]]
    return abs((b[0] - a[0]) * (c[1] - a[1]) -
               (b[1] - a[1]) * (c[0] - a[0])) * 0.5


def _tri_span(verts, tri):
    """Longest edge length of a triangle (3D)."""
    best = 0.0
    for k in range(3):
        p = verts[tri[k]]
        q = verts[tri[(k + 1) % 3]]
        d = math.sqrt((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 +
                      (p[2] - q[2]) ** 2)
        best = max(best, d)
    return best


def _tri_components(tris):
    """Component id per triangle, over SHARED EDGES (what the engine walks)."""
    edges = {}
    for ti, t in enumerate(tris):
        for k in range(3):
            a, b = t[k], t[(k + 1) % 3]
            edges.setdefault((a, b) if a < b else (b, a), []).append(ti)
    parent = list(range(len(tris)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for ts in edges.values():
        for i in range(1, len(ts)):
            ra, rb = find(ts[0]), find(ts[i])
            if ra != rb:
                parent[ra] = rb
    return [find(i) for i in range(len(tris))]


def _weld_sheets(verts, tris):
    """Fuse vertices that coincide in 3D, so independently-triangulated sheets
    share edges instead of merely touching.

    The split into plan-disjoint sheets (see _split_plan_overlaps) is what keeps
    a triangle from bridging two floors, but it also means two sheets that abut
    on ONE floor are meshed separately and their shared boundary is duplicated.
    Welding on the full 3D position repairs exactly that, and cannot fuse
    different storeys because it compares Z as well.
    """
    if not verts:
        return verts, tris
    # DISTANCE-based, not grid-snapped.  Two sheets sample a shared boundary
    # independently, so their vertices land 1-3u apart (measured in Chorrol:
    # 310 border pairs under 25u, the closest at 1.2u, all at identical Z).
    # Rounding to a grid puts such a pair in different buckets as often as the
    # same one, so it welded almost nothing; a radius search fuses them reliably.
    # WELD_R is far below the ribbon width, so no distinct feature is merged.
    # Raised from 6u to 12u: where a later sheet is CLIPPED against an earlier
    # one, the two then meet along that cut boundary, and each triangulation
    # densifies it at its own offsets — ImperialSewers03's two sheets came out
    # 7.68u apart there, just outside a 6u weld, leaving pathgrid=1 / navmesh=2.
    # Raised again from 12u to 16u: where a STAIR flight meets its landing the
    # two sheets sample the shared pathgrid node at different Z — the flight's
    # last row sits on the chord, the landing's on the floor — and Pinarus's
    # stair top came out 12.66u apart, just outside a 12u weld (pathgrid=1 /
    # navmesh=2, the halves 150/148).  16u equals RIBBON_GROW_MIN_HALF, so the
    # radius still cannot span two distinct rails, and it stays far under
    # MAX_CLIMB (34) so nothing an actor could not step over is fused.
    WELD_R = 16.0
    cell = WELD_R
    grid = {}
    remap = [0] * len(verts)
    out = []
    for i, v in enumerate(verts):
        gx, gy, gz = (int(v[0] // cell), int(v[1] // cell), int(v[2] // cell))
        got = None
        for ddx in (-1, 0, 1):
            for ddy in (-1, 0, 1):
                for ddz in (-1, 0, 1):
                    for (j, p) in grid.get((gx + ddx, gy + ddy, gz + ddz), ()):
                        if ((p[0] - v[0]) ** 2 + (p[1] - v[1]) ** 2 +
                                (p[2] - v[2]) ** 2) <= WELD_R * WELD_R:
                            got = j
                            break
                    if got is not None:
                        break
                if got is not None:
                    break
            if got is not None:
                break
        if got is None:
            got = len(out)
            out.append([float(v[0]), float(v[1]), float(v[2])])
            grid.setdefault((gx, gy, gz), []).append((got, out[got]))
        remap[i] = got
    welded = []
    for (a, b, c) in tris:
        a, b, c = remap[a], remap[b], remap[c]
        if a != b and b != c and a != c:
            welded.append((a, b, c))
    return out, welded


def _split_t_junctions(verts, tris):
    """Split a border edge that another sheet's vertex lies ON.

    Welding fixes vertices that coincide exactly, but two independently
    triangulated sheets usually meet along a boundary that one side sampled more
    finely than the other.  The finer side's extra vertex then sits in the MIDDLE
    of the coarser side's edge: the two touch geometrically but share no edge, so
    NVNM adjacency does not link them and the surface reads as two components
    (measured in Chorrol: 11 such T-junctions).

    Splitting the coarse edge at that vertex turns the contact into two shared
    edges.  Only BORDER edges are considered — an interior edge already has two
    triangles and is not a seam — so this cannot disturb the interior of a sheet.
    """
    tol = 2.0
    for _round in range(3):
        counts = {}
        for t in tris:
            for k in range(3):
                a, b = t[k], t[(k + 1) % 3]
                key = (a, b) if a < b else (b, a)
                counts[key] = counts.get(key, 0) + 1
        border = {e for e, c in counts.items() if c == 1}
        if not border:
            break
        cell = 64.0
        grid = {}
        for i in {i for t in tris for i in t}:
            grid.setdefault((int(verts[i][0] // cell),
                             int(verts[i][1] // cell)), []).append(i)

        splits = {}
        for (a, b) in border:
            pa, pb = verts[a], verts[b]
            dx, dy, dz = (pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2])
            L2 = dx * dx + dy * dy + dz * dz
            if L2 < 1e-9:
                continue
            gx0 = int(min(pa[0], pb[0]) // cell)
            gx1 = int(max(pa[0], pb[0]) // cell)
            gy0 = int(min(pa[1], pb[1]) // cell)
            gy1 = int(max(pa[1], pb[1]) // cell)
            hits = []
            for gx in range(gx0, gx1 + 1):
                for gy in range(gy0, gy1 + 1):
                    for i in grid.get((gx, gy), ()):
                        if i == a or i == b:
                            continue
                        p = verts[i]
                        s = ((p[0] - pa[0]) * dx + (p[1] - pa[1]) * dy +
                             (p[2] - pa[2]) * dz) / L2
                        if not (0.02 < s < 0.98):
                            continue
                        qx = pa[0] + dx * s
                        qy = pa[1] + dy * s
                        qz = pa[2] + dz * s
                        if ((p[0] - qx) ** 2 + (p[1] - qy) ** 2 +
                                (p[2] - qz) ** 2) <= tol * tol:
                            hits.append((s, i))
            if hits:
                hits.sort()
                splits[(a, b)] = [i for (_s, i) in hits]

        if not splits:
            break

        out = []
        changed = False
        for t in tris:
            fan = None
            for k in range(3):
                a, b = t[k], t[(k + 1) % 3]
                key = (a, b) if a < b else (b, a)
                ins = splits.get(key)
                if not ins:
                    continue
                chain = list(ins) if (a, b) == key else list(reversed(ins))
                opp = t[(k + 2) % 3]
                seq = [a] + chain + [b]
                fan = [(seq[m], seq[m + 1], opp) for m in range(len(seq) - 1)]
                break
            if fan:
                out.extend(fan)
                changed = True
            else:
                out.append(t)
        tris = [t for t in out if len(set(t)) == 3]
        if not changed:
            break
    return tris


def _split_plan_overlaps(groups):
    """Split a connectivity group wherever it OVERLAPS ITSELF in plan view.

    _storey_groups deliberately chains a staircase to the floors at both its
    ends, so a multi-storey building comes back as ONE group (Chorrol: 107 strips
    spanning z -302..143).  That is the right answer for connectivity, but it is
    the wrong region to union in 2D: the upper and lower floors overlap in plan,
    and a single flattened union of them is exactly what let the triangulation
    bridge two floors.

    So each group is separated into sheets that do NOT overlap each other in
    plan.  A ribbon starts a new sheet when its footprint overlaps a sheet whose
    height there disagrees by more than a storey gap.  The staircase itself
    overlaps neither floor in plan (it occupies the gap between them), so it
    still lands in one of them and keeps the two joined through the shared node
    vertices its ribbon contributes.
    """
    out = []
    for group in groups:
        items = []
        for s in group:
            poly = _ribbon_polygon(s)
            if poly.is_valid and not poly.is_empty:
                items.append((s, poly))
        if not items:
            continue

        # Ribbons that OVERLAP in plan and AGREE in height there are the same
        # sheet; ribbons that overlap and disagree by more than a storey are
        # different sheets.  Grouping by the connected components of the
        # "agrees" relation keeps every same-floor neighbour in ONE sheet, so a
        # floor is triangulated whole and needs no stitching afterwards.
        #
        # A greedy first-fit was tried first and is wrong: a ribbon lands in the
        # first sheet that merely does not conflict, which scatters one floor
        # across several sheets and leaves seams between them.
        n = len(items)
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        # BONDED PAIRS -- two ribbons that meet at a pathgrid NODE where their
        # heights agree.  The pathgrid asserts an actor walks from one onto the
        # other there, so they describe ONE walkable junction and MUST end up in
        # the same sheet: the union then merges their ribbons and the junction
        # comes out as shared edges, with nothing left to repair afterwards.
        #
        # This bond is unconditional.  Scoping it (to steep ribbons, to a disc
        # around the node, to "stair mouths") was tried repeatedly and every
        # variant fails the same way -- whatever ground the two sheets both keep
        # gets meshed twice, once per sheet, and since each sheet is triangulated
        # independently the two copies share no edges and the mesh fragments.
        # Measured across 9 cells, the scoped variants cost 3-6 cells their
        # connectivity (Chorrol 1->4 components, ImperialSewers03 2->6, Skingrad
        # 500->261/236) while fixing one junction.  The only sound answer is to
        # not split the junction in the first place.
        bonded = set()
        node_h = {}
        for k in range(n):
            sk = items[k][0]
            (ni, nj) = sk.get('edge', (-1, -1))
            if ni < 0:
                continue
            node_h.setdefault(ni, []).append((k, sk['na'][2]))
            if nj != ni:
                node_h.setdefault(nj, []).append((k, sk['nb'][2]))
        for entries in node_h.values():
            for x in range(len(entries)):
                for y in range(x + 1, len(entries)):
                    ka, za = entries[x]
                    kb, zb = entries[y]
                    if abs(za - zb) <= SAME_SURFACE_Z:
                        bonded.add((min(ka, kb), max(ka, kb)))

        conflicts = set()
        for a in range(n):
            sa, pa = items[a]
            for b in range(a + 1, n):
                sb, pb = items[b]
                if not pa.intersects(pb):
                    continue
                inter = pa.intersection(pb)
                if inter.is_empty or inter.area < 1.0:
                    continue
                gap = _overlap_height_gap(sa, sb, inter)
                # A bond outranks a plan conflict.  Two ribbons meeting at a node
                # where they agree in height are one junction even if their
                # ribbons ALSO overlap somewhere else at a different storey --
                # which is exactly what a staircase does: Pinarus's flight (0,1)
                # meets the landing at node 1 (heights 68.6 vs 68.6) and passes
                # UNDER five upper-floor ribbons near its bottom end.  Judging it
                # on those overlaps alone put the flight in a different sheet from
                # its own landing, and the clip then took its top 51.2u as
                # duplicate ground.
                if gap > STOREY_GAP_Z and (a, b) not in bonded:
                    conflicts.add((a, b))
                else:
                    ra, rb = find(a), find(b)
                    if ra != rb:
                        parent[ra] = rb

        # Bonds also merge directly, so a junction survives even when the two
        # ribbons never overlap in plan at all.
        for (a, b) in bonded:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        buckets = {}
        for i in range(n):
            buckets.setdefault(find(i), []).append(i)

        # A merged bucket must not contain a conflicting pair (a stair can chain
        # a ribbon on each floor into one bucket).  Split any bucket that does,
        # by greedily seeding sub-sheets that stay conflict-free.
        for members in buckets.values():
            cset = {(a, b) for (a, b) in conflicts
                    if a in members and b in members}
            if not cset:
                out.append([items[i][0] for i in members])
                continue
            # Assign each ribbon to the sub-sheet it AGREES with best, not merely
            # to the first that does not conflict.  A first-fit scatters ribbons
            # from ONE floor across several sub-sheets, and those sub-sheets then
            # overlap in plan at the SAME height — duplicate ground, measured as
            # 7% of Chorrol's triangles stacked on another at the same height.
            subs = []
            sub_h = []                      # representative height per sub-sheet
            for i in sorted(members,
                            key=lambda k: -0.5 * (items[k][0]['na'][2] +
                                                  items[k][0]['nb'][2])):
                zi = 0.5 * (items[i][0]['na'][2] + items[i][0]['nb'][2])
                best = None
                for si, sub in enumerate(subs):
                    if any((min(i, j), max(i, j)) in cset for j in sub):
                        continue
                    # A sub-sheet this ribbon is BONDED to (they meet at a
                    # pathgrid node and agree in height there) always wins: that
                    # is the junction, and splitting it is the defect.  Scoring on
                    # mean height alone loses it for the case it matters most --
                    # a STAIRCASE's mean sits midway between its two floors, so it
                    # is never near either sub-sheet and lands wherever the
                    # ordering happens to put it.
                    d = abs(sub_h[si] - zi)
                    rank = 0 if any((min(i, j), max(i, j)) in bonded
                                    for j in sub) else 1
                    if best is None or (rank, d) < (best[0], best[1]):
                        best = (rank, d, si)
                if best is not None:
                    best = (best[1], best[2])
                if best is None:
                    subs.append([i])
                    sub_h.append(zi)
                else:
                    si = best[1]
                    subs[si].append(i)
                    # Track the sub-sheet's height as a running mean so a stair
                    # does not drag it away from the floor it belongs to.
                    sub_h[si] = (sub_h[si] * (len(subs[si]) - 1) + zi) / \
                        len(subs[si])
            # MERGE BACK any two sub-sheets that overlap in plan at the SAME
            # height.  The sub-split only has to separate ribbons that genuinely
            # conflict; anything else belonging to one floor must stay together,
            # or both sub-sheets mesh that ground independently and the triangles
            # STACK.  Measured on Chorrol: 11 overlapping pairs, each a point
            # where sheet0 and sheet1 both covered it at -44.8 vs -45.4 and
            # -31.6 vs -45.4 — the same floor, split in two.
            merged_subs = []
            for sub in subs:
                target = None
                for mi, msub in enumerate(merged_subs):
                    if any((min(i, j), max(i, j)) in cset
                           for i in sub for j in msub):
                        continue
                    if _subs_same_floor(items, sub, msub):
                        target = mi
                        break
                if target is None:
                    merged_subs.append(list(sub))
                else:
                    merged_subs[target].extend(sub)
            for sub in merged_subs:
                out.append([items[i][0] for i in sub])
    return out


def _is_steep(s):
    """Is this strip a STAIRCASE/ramp rather than a flat corridor?

    The same rise/run test corridor.py uses to decide a ribbon is not
    width-grown (params.RIBBON_GROW_MAX_SLOPE), so "steep" means the same thing
    on both sides of the pipeline.  Node discs and door footprints (a == b) are
    flat by construction and never steep.
    """
    ax, ay, az = s['a']
    bx, by, bz = s['b']
    run = math.hypot(bx - ax, by - ay)
    if run < 1e-4:
        return False
    return abs(bz - az) / run > params.RIBBON_GROW_MAX_SLOPE


def _same_surface_region(group_a, group_b, shared):
    """The part of `shared` where both sheets describe the SAME surface height.

    Returned as a polygon to subtract from the later sheet, so that ground is
    meshed once.  Where the two sheets disagree in height they are genuinely
    stacked storeys and both keep their mesh, so that ground is NOT returned.

    Worked per ribbon-pair rather than over the whole region, because a sheet
    containing a staircase spans many heights and a single test would either
    surrender a whole floor or nothing.
    """
    from shapely.ops import unary_union as _uu
    dup = []
    for sa in group_a:
        pa = _ribbon_polygon(sa)
        if pa.is_empty or not pa.intersects(shared):
            continue
        for sb in group_b:
            pb = _ribbon_polygon(sb)
            if pb.is_empty or not pb.intersects(pa):
                continue
            try:
                piece = pa.intersection(pb).intersection(shared)
            except Exception:
                continue
            if piece.is_empty or piece.area < 1.0:
                continue
            if _overlap_height_gap(sa, sb, piece) <= SAME_SURFACE_Z:
                dup.append(piece)
    if not dup:
        return None
    try:
        return _uu(dup)
    except Exception:
        return None


def _overlap_height_gap(sa, sb, inter):
    """Smallest height disagreement between two ribbons over the ground they share.

    Evaluating this at the intersection CENTROID alone is not enough: a long stair
    ribbon crossing a floor ribbon can agree at that single point while
    disagreeing over most of the overlap (and vice versa).  A stair also sweeps
    through every height between two floors, so a centroid test made it look like
    it belonged to whichever floor the centroid happened to land near — Chorrol's
    sheet0 came out spanning z -45..143, claiming ground-floor ribbons that
    sheet1 also meshed, and the two stacked 11 pairs of triangles at the same
    height.

    Sampling several points across the shared region and taking the MINIMUM
    disagreement answers the question that matters: is there anywhere these two
    ribbons describe the same walkable surface?  If so they belong together.
    """
    pts = []
    try:
        c = inter.centroid
        pts.append((c.x, c.y))
    except Exception:
        pass
    try:
        rp = inter.representative_point()
        pts.append((rp.x, rp.y))
    except Exception:
        pass
    try:
        minx, miny, maxx, maxy = inter.bounds
        for fx in (0.25, 0.5, 0.75):
            for fy in (0.25, 0.5, 0.75):
                px = minx + (maxx - minx) * fx
                py = miny + (maxy - miny) * fy
                if inter.intersects(_pt(px, py)):
                    pts.append((px, py))
    except Exception:
        pass
    if not pts:
        return float('inf')
    return min(abs(_height_on(sa, px, py) - _height_on(sb, px, py))
               for (px, py) in pts)


def _pt(x, y):
    from shapely.geometry import Point
    return Point(x, y)


def _subs_same_floor(items, sub_a, sub_b):
    """True when two sub-sheets share ground at (nearly) the same height.

    Two sub-sheets that overlap in plan and agree in height there are ONE floor
    that the conflict split happened to separate; keeping them apart makes each
    mesh that ground on its own and the results stack.
    """
    for i in sub_a:
        si, pi = items[i]
        for j in sub_b:
            sj, pj = items[j]
            if not pi.intersects(pj):
                continue
            try:
                inter = pi.intersection(pj)
            except Exception:
                continue
            if inter.is_empty or inter.area < 1.0:
                continue
            cx, cy = inter.centroid.x, inter.centroid.y
            if abs(_height_on(si, cx, cy) -
                   _height_on(sj, cx, cy)) <= SAME_SURFACE_Z:
                return True
    return False


def _storey_groups(strips):
    """Group the ribbons into STOREYS, so each can be unioned on its own.

    A cell's ribbons must not all be flattened into one 2D union: an upper floor
    and a lower floor overlap in plan view, so the triangulation bridges them and
    produces triangles whose corners are on two different floors at once (the
    near-vertical sheets that render as "triangles between floors").

    Grouping cannot be a Z threshold, because a STAIRCASE has no single height —
    it is exactly the thing that spans two floors legitimately.  So ribbons are
    grouped by CONNECTIVITY instead:

      * two ribbons join the same storey when they share a pathgrid NODE and
        their heights AT THAT SHARED NODE agree (within SAME_SURFACE_Z);
      * a stair therefore joins the floor at its foot (they agree at the bottom
        node) and the floor at its head (they agree at the top node), which
        merges all three into ONE group — the storeys stay connected exactly
        where the pathgrid says an actor walks between them;
      * two floors that merely overlap in PLAN, sharing no node, never merge.

    The result is a partition of the ribbons whose groups are each a single
    walkable sheet, connected the way the pathgrid asserts.  Returns a list of
    strip lists.
    """
    n = len(strips)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    # node -> [(strip index, height of that strip AT that node)]
    at_node = {}
    for si, s in enumerate(strips):
        (i, j) = s.get('edge', (-1, -1))
        if i < 0:
            continue                        # door quad: attached below by overlap
        at_node.setdefault(i, []).append((si, s['na'][2]))
        if j != i:
            at_node.setdefault(j, []).append((si, s['nb'][2]))

    for entries in at_node.values():
        for a in range(len(entries)):
            for b in range(a + 1, len(entries)):
                if abs(entries[a][1] - entries[b][1]) <= SAME_SURFACE_Z:
                    union(entries[a][0], entries[b][0])

    groups = {}
    for si in range(n):
        s = strips[si]
        if s.get('edge', (-1, -1))[0] < 0:
            continue                        # door quads handled after
        groups.setdefault(find(si), []).append(s)

    out = [g for g in groups.values() if g]

    # Door footprints (and any other node-less strip) carry no pathgrid edge, so
    # they cannot be grouped by node.  They must join the group whose ribbons the
    # footprint actually TOUCHES, judged in plan AND in height.
    #
    # Matching on height alone is wrong: a STAIR ribbon passes through every
    # height between two floors, so it always looked like the closest match and
    # swallowed door footprints from the far side of the house.  Those footprints
    # were then meshed with the stair sheet, where nothing else covers their
    # ground, and came out as isolated islands — measured on Pinarus, whose
    # corridor mesh alone is ONE component (289 tris) but became SEVEN once its 5
    # doors were added, with the visible break at the top of the stairs.
    door_strips = [s for s in strips if s.get('edge', (-1, -1))[0] < 0]
    if door_strips:
        polys = [_ribbon_polygon(s) for s in door_strips]
        group_polys = []
        for g in out:
            try:
                group_polys.append(unary_union([_ribbon_polygon(x)
                                                for x in g]))
            except Exception:
                group_polys.append(None)
        for s, dp in zip(door_strips, polys):
            z = s['a'][2]
            best = None
            for gi, g in enumerate(out):
                # Height agreement is required, but measured against the ribbons
                # whose footprint the door actually overlaps.
                hz = None
                for x in g:
                    xp = _ribbon_polygon(x)
                    if not xp.intersects(dp):
                        continue
                    d = min(abs(x['na'][2] - z), abs(x['nb'][2] - z))
                    if hz is None or d < hz:
                        hz = d
                if hz is None or hz > STOREY_GAP_Z:
                    continue
                gp = group_polys[gi]
                area = 0.0
                if gp is not None:
                    try:
                        area = dp.intersection(gp).area
                    except Exception:
                        area = 0.0
                # Prefer the group it overlaps MOST; break ties on height.
                key = (-area, hz)
                if best is None or key < best[0]:
                    best = (key, gi)
            if best is not None:
                out[best[1]].append(s)
            else:
                out.append([s])
    return out or [list(strips)]


def _emit_surfaces(v2, t2, levels):
    """Lift a 2D triangulation onto its walkable surfaces, WITHOUT tearing.

    THE DEFECT THIS REPLACES.  The old code chose a triangle's height as the
    MEAN of its three corners' levels, then bound each corner to whatever vertex
    already sat within SAME_SURFACE_Z of that mean.  A corner's height therefore
    depended on WHICH TRIANGLE ASKED FIRST, so two triangles sharing a corner on
    ONE surface routinely bound it to two different vertices:

        corner 22, a single level at 395.3, minted vertex 370 (z=395.3) for one
        neighbour and vertex 413 (z=356.2) for the next.

    They then share no EDGE, and the engine cannot walk between them
    (_compute_adjacency links only across shared edges).  On a STAIR every
    consecutive triangle has a different mean, so stairs tore worst: measured on
    ICPrisonSewerExit01, 28 of 582 shared 2D edges were lost and the mesh fell
    into 12 components; ICPrisonEntrance01 fell into 28.  No value of
    SAME_SURFACE_Z fixes it — widening fuses real storeys, narrowing tears more.
    It is a first-match-wins race, not a tolerance.

    THE FIX.  A point's height is a property of THE POINT AND ITS SURFACE, never
    of whichever triangle reached it first:

      1. Assign every 2D triangle to the surfaces beneath it (unchanged: cluster
         the corners' levels on STOREY_GAP_Z, emit once per storey).
      2. UNION-FIND the (corner, surface) pairs.  When a triangle is emitted on a
         surface, its three corners are joined into one class.  Two triangles
         meeting on one surface therefore ALWAYS resolve their shared corner to
         the same class — connectivity is structural, exactly as it is for the
         2D union itself, instead of being re-derived per triangle.
      3. Give each class ONE height: the level at that corner nearest the class's
         own surface.  Because the level came from the ribbon's own centreline
         (_height_on follows the pathgrid line A->B), the lifted surface is
         PARALLEL TO THE SEED LINE by construction — a stair comes out as one
         straight ramp rather than a sawtooth of per-triangle averages.

    Coverage is untouched: every 2D triangle is still emitted on every surface
    beneath it, and no triangle is ever dropped.
    """
    # --- 0. merge each corner's levels into STOREYS.
    #
    # _levels_at clusters a corner's covering ribbons on SAME_SURFACE_Z (36u), so
    # a staircase arrives already split into a level per tread-ish step: corner
    # 162 came back as [-302.3, -254.7], two entries 47u apart that are ONE
    # flight.  Emission then treated each as its own surface and stacked a second
    # triangle on the stair.  Re-cluster on the STOREY gap first, so "surface"
    # means the same thing to the level lookup and to the emission — a stair is
    # one surface, and only a genuine floor-above is a second.
    def storeys_of(lv):
        if not lv:
            return []
        out = [[lv[0]]]
        for z in lv[1:]:
            if z - out[-1][-1] <= STOREY_GAP_Z:
                out[-1].append(z)
            else:
                out.append([z])
        return out

    # Per corner: the storey bands, and the representative height of each.  The
    # height is the level NEAREST the triangle asking (see vert), not the band
    # mean — a stair's band spans its whole rise, and the mean would flatten it.
    corner_bands = [storeys_of(sorted(lv)) for lv in levels]

    # --- 1. which surfaces does each triangle live on?
    #
    # A surface is real for this triangle only when ALL THREE corners have ground
    # on it.  Pooling the three corners' levels and clustering the pool (the
    # previous rule) merges storeys TRANSITIVELY: a corner standing on a stair
    # carries heights between the two floors, chaining the -302 floor to the +127
    # floor into one band whose mean, -89, is in MID-AIR.  That is what put 225 of
    # 609 ChorrolFightersGuild triangles up to 213u from any walkable collision —
    # sheets hanging between the storeys.
    #
    # So each corner votes with its OWN storey bands, and a surface is kept only
    # where all three overlap.  A corner is then never dragged to a height it has
    # no ground at, and the triangle spanning a stairwell — whose corners really
    # are on different floors — is simply not emitted there, instead of being
    # emitted in between.
    def band_reps(k):
        # Only a corner's OWN levels count here.  (bare_clusters is derived from
        # tri_surfaces further down, so it cannot be consulted at this point;
        # corners with no levels take the fallback path below.)
        return [(min(b), max(b)) for b in corner_bands[k]]

    def _reaches(bands, z):
        """Does this corner have ground on the surface at z?

        A band is an interval [lo, hi]; on a stair it spans the whole rise, so a
        plain point test is right — the corner genuinely has ground everywhere
        between.

        The band is widened only by MAX_CLIMB, a single STEP.  Widening it by
        STOREY_GAP_Z (120u) instead let a corner vote for a surface it has no
        ground on at all, and that produced the flap that made Pinarus's only
        floor-to-floor link unnavigable:

            2D triangle (-242.7,132.5) (-316.9,134.9) (-317.5,173.3)
            corner 1 band [30.0, 30.0]    (stair ribbon only)
            corners 2,3 band [68.6, 68.6] (landing)

        With a 120u tolerance BOTH 30.0 and 68.6 passed for every corner, so the
        one triangle was emitted twice — once at 30.0 (tilted 27 degrees) and once
        at 68.6 (flat) — with identical 1425u^2 plan footprints, 38.6u apart,
        sharing edge (126,127).  That shared edge was the ONLY connection between
        the two floors, and an actor crossing it would have to step onto a surface
        directly beneath the one it is standing on.

        A step is the right tolerance: an actor can step up/down MAX_CLIMB onto an
        adjoining surface, so a corner one step off still legitimately belongs to
        that surface.  Anything further and it does not.
        """
        tol = REACH_TOL
        return any(lo - tol <= z <= hi + tol for (lo, hi) in bands)

    tri_surfaces = []
    for (a, b, c) in t2:
        ba, bb, bc = band_reps(a), band_reps(b), band_reps(c)
        present = [x for x in (ba, bb, bc) if x]
        if not present:
            tri_surfaces.append(())
            continue
        # EVERY corner proposes its own storeys — not just corner a, or a surface
        # that only the other two share is silently lost and the floor splits
        # laterally (measured: same-storey components 47-115u apart in Chorrol).
        # A proposal is kept when every corner that HAS ground reaches it; a
        # corner with no ground of its own abstains and takes the surface height
        # through `vert`'s fallback, so the triangle is still emitted.
        # Candidate surfaces are REAL band endpoints, never band midpoints: a
        # band that spans a flight of stairs has its midpoint in mid-air, and
        # proposing it emits a sheet hanging between the storeys.
        proposals = sorted({z for bands in present for (lo, hi) in bands
                            for z in (lo, hi)})
        surfaces = []
        for z in proposals:
            if all(_reaches(bands, z) for bands in present):
                # Collapse proposals that are the same storey, so one surface is
                # not emitted twice under slightly different names.
                if not surfaces or z - surfaces[-1] > STOREY_GAP_Z:
                    surfaces.append(z)
        # If no storey is shared by all corners, the triangle is NOT emitted.
        #
        # Such a triangle straddles a stairwell: measured in Chorrol, corners
        # with levels [-45], [-302] and [-302,-45] — two of them on floors 257u
        # apart, with no ground in between.  Forcing it onto one storey (a
        # majority vote) drags the odd corner down through the stairwell and
        # produces exactly the near-vertical sheets that render as "triangles
        # between floors".  That is a WALL, not walkable ground: an actor cannot
        # traverse it, so the correct mesh does not contain it.
        #
        # This costs no real coverage.  The ground itself is still covered — by
        # the upper floor's triangles at -45 and the lower floor's at -302; only
        # the impossible bridge between them is gone.  The stair proper is a
        # ribbon whose own levels are continuous, so its corners DO share a
        # storey band and it is emitted normally.
        tri_surfaces.append(tuple(surfaces))

    # --- 2. union-find over (corner, surface-slot) pairs.
    # A corner's surface slot is the index of ITS OWN level cluster nearest the
    # triangle's surface height: two triangles on one storey pick the same slot
    # at a shared corner, while a corner carrying two storeys keeps them apart.
    # A corner with NO level of its own still has to be distinguished per storey,
    # or every such corner in the cell collapses into one class and the mesh
    # flattens.  It CANNOT be keyed by quantising z into fixed bands: band edges
    # are arbitrary, so two neighbours a unit apart in Z straddle one and land in
    # different classes — that shattered ChorrolFightersGuild into 83 components
    # and lost two thirds of its triangles.
    #
    # Instead each level-less corner accumulates the surface heights that
    # actually reach it, and those are clustered on STOREY_GAP_Z the same way a
    # corner's own levels are.  The slot is then an index into ITS OWN clusters,
    # so it is stable, band-free, and separates real storeys only where a real
    # storey gap exists.
    bare = {}
    for ti, (a, b, c) in enumerate(t2):
        for z in tri_surfaces[ti]:
            for k in (a, b, c):
                if not levels[k]:
                    bare.setdefault(k, []).append(z)
    bare_clusters = {k: storeys_of(sorted(zs)) for k, zs in bare.items()}

    def slot_of(k, z):
        """Which STOREY of corner k this triangle's surface belongs to.

        Keyed on the storey band, not on the individual level: two triangles
        stepping along a stair ask with slightly different z but must land on the
        same band, or they mint different vertices and the stair tears.
        """
        bands = corner_bands[k] or bare_clusters.get(k)
        if not bands:
            return 0
        return min(range(len(bands)),
                   key=lambda i: min(abs(x - z) for x in bands[i]))

    # --- 2b. the vertex key is (corner, slot) DIRECTLY.
    #
    # `slot_of` already gives a corner a stable identity per walkable surface: it
    # indexes that corner's OWN clustered levels, which do not depend on which
    # triangle is asking.  So two triangles meeting at a corner on one surface
    # compute the same slot and therefore the SAME vertex — connectivity is
    # structural, which is the whole point of the rewrite.
    #
    # (An earlier attempt union-found the (corner, slot) pairs and keyed the
    # vertex on the resulting class root.  That was wrong twice over: the class
    # merges DIFFERENT corners, so the root is not a per-corner identity, and
    # keying on it produced a vertex per triangle — 355 of 609 triangles came out
    # as isolated singletons.  No union-find is needed at all.)
    keyed = []                              # per (tri, surface): the three keys
    for ti, (a, b, c) in enumerate(t2):
        for z in tri_surfaces[ti]:
            keyed.append(((a, slot_of(a, z)), (b, slot_of(b, z)),
                          (c, slot_of(c, z)), z))

    # --- 3. each vertex takes the corner's OWN level for that surface.
    #
    # That level was computed by _height_on along the covering ribbon's
    # centreline, i.e. along the pathgrid line A->B — so the lifted surface is
    # PARALLEL TO THE SEED LINE by construction and a stair keeps its exact rise.
    # The height is never a per-triangle average, which is what made the old
    # code's heights depend on which triangle asked first.
    vid = {}
    verts = []

    def vert(k, fallback):
        """The single vertex for corner k on storey k[1].

        The height depends ONLY on the key — never on which triangle asked, or
        the first caller would win and order-dependence (the original defect)
        would come straight back.  A band holds the heights of every ribbon
        covering this exact point on this storey; they were each computed by
        _height_on along that ribbon's centreline, so on a stair they agree to
        within the ribbons' own crossing error and their median is the point's
        height ON the pathgrid line.  A `fallback` is used only when the corner
        carries no level at all (the union covers it but no centreline claims it).
        """
        got = vid.get(k)
        if got is None:
            bands = corner_bands[k[0]] or bare_clusters.get(k[0]) or ()
            if 0 <= k[1] < len(bands):
                band = sorted(bands[k[1]])
                zz = band[len(band) // 2]
            else:
                zz = fallback
            got = len(verts)
            verts.append([float(v2[k[0]][0]), float(v2[k[0]][1]), float(zz)])
            vid[k] = got
        return got

    tris = []
    # A 2D triangle is emitted once per surface beneath it, and two of a corner's
    # storey bands can resolve to the SAME vertices — so the same triangle is
    # emitted twice (once per winding) or several times over.  Measured on
    # Pinarus: (167,178,152) and its reverse formed a 2-triangle "component", and
    # a collinear sliver (57,58,59) was emitted FOUR times as four 1-triangle
    # "components".  These are duplicates and degenerates, not islands, and they
    # are what made a house whose corridor mesh is ONE component report seven.
    seen = set()
    for (ka, kb, kc, z) in keyed:
        ia, ib, ic = vert(ka, z), vert(kb, z), vert(kc, z)
        if ia == ib or ib == ic or ia == ic:
            continue
        # Winding-independent identity: the same three vertices are the same
        # triangle however they are ordered.
        key = tuple(sorted((ia, ib, ic)))
        if key in seen:
            continue
        # Drop zero-area (collinear) triangles: they cover no ground, cannot be
        # stood on, and only ever attach to the mesh at a point.
        pa, pb, pc = verts[ia], verts[ib], verts[ic]
        area2 = abs((pb[0] - pa[0]) * (pc[1] - pa[1]) -
                    (pb[1] - pa[1]) * (pc[0] - pa[0]))
        if area2 * 0.5 < params.MIN_XY_FOOTPRINT:
            continue
        seen.add(key)
        tris.append((ia, ib, ic))
    return verts, tris


def _drop_point_attached(tris):
    """Drop triangles that touch the rest of the mesh at a single VERTEX only.

    One 2D triangle of the union is emitted once per SURFACE its corners' levels
    suggest.  Where a corridor and a nearby quad at a different height both cover
    a point, that produces a second copy at the other height — and because none
    of its edges is shared with anything at that height, it hangs off the mesh by
    a corner.  That is the rogue triangle climbing a staircase toward a door.

    A triangle that shares no full EDGE with any other triangle cannot be walked
    onto (NVNM adjacency links only across shared edges — see
    pgrd_to_navm._compute_adjacency), so it is never useful mesh; dropping it
    removes the artefact without touching anything reachable.  Iterated, because
    removing one can leave its neighbour edge-isolated in turn.
    """
    tris = [tuple(t) for t in tris]
    for _round in range(4):
        counts = {}
        for t in tris:
            for k in range(3):
                a, b = t[k], t[(k + 1) % 3]
                key = (a, b) if a < b else (b, a)
                counts[key] = counts.get(key, 0) + 1
        keep = []
        for t in tris:
            shared = False
            for k in range(3):
                a, b = t[k], t[(k + 1) % 3]
                key = (a, b) if a < b else (b, a)
                if counts.get(key, 0) >= 2:
                    shared = True
                    break
            if shared:
                keep.append(t)
        if len(keep) == len(tris):
            break
        tris = keep
    return tris


def _levels_batch(strips, points):
    """_levels_at for MANY points at once, natively.  Returns a list of lists.

    THE REASON THIS IS BATCHED: per point _levels_at scans every strip (~1,900
    in a dense cell), and a grown strip's admission test is a point-in-polygon
    plus a min-distance over its whole outline.  Measured on Wendir02 that was
    29.3s of a 31.9s build — 4.5ms per call over 6,491 calls — making it the
    single hottest thing left after the width-grow went native.

    The strips are flattened ONCE per union (not per point) and the native side
    buckets them by XY bounds, so each point tests only strips that could
    actually cover it.
    """
    from ._native_loader import load_native
    native = load_native('_navgrow_native')

    rows = []
    poly = []
    for s_ in strips:
        p = s_.get('poly')
        off, n = 0, 0
        if p is not None:
            off, n = len(poly), len(p)
            poly.extend(p)
        a_, b_ = s_['a'], s_['b']
        rows.append((a_[0], a_[1], a_[2], b_[0], b_[1], b_[2],
                     float(s_['half']), float(off), float(n), 0.0, 0.0))
    sarr = (np.asarray(rows, dtype=np.float64) if rows
            else np.zeros((0, 11), dtype=np.float64))
    parr = (np.asarray(poly, dtype=np.float64).reshape(-1, 2) if poly
            else np.zeros((0, 2), dtype=np.float64))
    qarr = (np.asarray(points, dtype=np.float64).reshape(-1, 2) if len(points)
            else np.zeros((0, 2), dtype=np.float64))
    return native.levels_at(sarr, parr, qarr, float(SAME_SURFACE_Z))


def _levels_at(strips, px, py):
    """Distinct surface heights at (px, py): one per storey covering it.

    The heights of every corridor whose ribbon covers the point are clustered;
    a gap larger than SAME_SURFACE_Z starts a new surface.  A stair ribbon and
    the floor it meets fall in one cluster (they differ by a few units there),
    while the floor it flies over is hundreds away and forms its own.
    """
    zs = []
    for s in strips:
        # A poly strip (door quad, or a Phase-2 GROWN corridor) owns exactly its
        # outline — admit only where the point is inside it.  Using the scalar
        # 'half' as the admission radius is only correct for a fixed-width
        # rectangle; for a grown ribbon 'half' is the MAX half-width (up to
        # RIBBON_GROW_MAX_HALF), so 'distance <= half' would claim the point far
        # OUTSIDE the actual ribbon and inject phantom surface levels that split
        # the triangulation (Pinarus fragmented into 11 components).
        if s.get('poly') is not None:
            hit = _distance_to(s, px, py) <= 1e-6      # 0 == inside the outline
        else:
            hit = _distance_to(s, px, py) <= s['half'] + 1e-6
        if hit:
            zs.append(_height_on(s, px, py))
    if not zs:
        return []
    zs.sort()
    out = [[zs[0]]]
    for z in zs[1:]:
        if z - out[-1][-1] <= SAME_SURFACE_Z:
            out[-1].append(z)
        else:
            out.append([z])
    return [sum(g) / len(g) for g in out]


def _closest_level(levels, z):
    """Index of the level nearest z, or None when none is within tolerance."""
    best = None
    for i, lz in enumerate(levels):
        d = abs(lz - z)
        if d <= SAME_SURFACE_Z and (best is None or d < best[0]):
            best = (d, i)
    return best[1] if best else None
