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
# Bound once at module scope: `_overlap_height_gap` is called ~30k times on a
# large cell, and re-running `import shapely` per call is pure interpreter
# overhead on an already-loaded module.
from shapely import intersects as _sh_intersects, points as _sh_points

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

# How far a corner's own ground may be from a surface and still count as being
# ON that surface (_reaches, inside _emit_surfaces).  A STOREY-gap tolerance,
# deliberately: a stair triangle legitimately spans up to ~65u across one edge
# (a 128u edge on a 27-degree flight), so any step-sized tolerance here tears
# flights mid-air (measured: REACH_TOL=MAX_CLIMB opened a 127u hole in the
# middle of Pinarus's staircase).  The wall-like triangles that a wide reach
# admits are rejected by WALL_SLOPE_COS below instead — slope separates a
# stair from a wall cleanly, where no reach distance can.
REACH_TOL = STOREY_GAP_Z

# Steepest triangle the finished mesh should carry, as cos(slope) = plan area
# / 3D area.  Walkable ground tops out at MAX_SLOPE_DEG (46) and every real
# flight measures 27-40 degrees; a triangle steeper than 55 degrees is a WALL
# an actor cannot stand on — the near-vertical flaps that rendered as "a
# triangle sticking up vertically at the top of the stairs" (58-84 degrees
# measured).  Enforced by _drop_walls, which removes such a triangle ONLY when
# its neighbours stay connected without it: dropping walls at emission time
# instead tore ImperialDungeon04 and BarrenCave apart, because on jagged cave
# ground a steep triangle is sometimes the only link between two ledges — an
# ugly-but-connected mesh beats a clean-but-severed one.
WALL_SLOPE_COS = 0.574          # cos(55 deg)

# A free (unshared) triangle edge dropping at least this far is a SILHOUETTE
# over open space, not a join to adjoining ground.  Two of them on one triangle
# make it a flap hanging into a stairwell -- see _open_flap in _drop_walls.
# Sized above a stair's per-triangle rise (a 128u edge on a 27-degree flight
# climbs ~65u, but shares that edge with the next tread) and well under a
# storey, so a real ledge -- which has ONE free edge, at its bottom -- is never
# matched however deep the drop below it.
FLAP_EDGE_DROP = 40.0


def _ribbon_polygon(s):
    """The corridor's ribbon as a 2D polygon (a rectangle around its segment).

    A strip may instead carry an explicit 'poly' outline — the door triangles
    do — in which case that shape is used verbatim.

    MEMOISED on the strip's identity.  This is a pure function of the strip, but
    the nested ribbon-pair loops (_same_surface_region, _split_plan_overlaps)
    call it for the same strips over and over — 379,250 calls for a cell holding
    a few thousand strips, ~12.7s of a 33s build, and the invalid-outline repair
    path above re-ran its buffer/union work every single time.  Strips are plain
    dicts that live for the whole build and are never mutated after the polygon
    could first be asked for, so identity is a sound key; the cache is cleared
    per build by `_ribbon_cache_clear` so nothing leaks between cells (and a
    freed dict's id cannot be recycled onto a stale entry, because the cache
    holds a reference to every strip it keys).
    """
    cached = _RIBBON_CACHE.get(id(s))
    if cached is not None:
        return cached[1]
    p = _ribbon_polygon_uncached(s)
    _RIBBON_CACHE[id(s)] = (s, p)
    return p


_RIBBON_CACHE = {}


def _ribbon_cache_clear():
    _RIBBON_CACHE.clear()


def _clip_strip_near(s, nx, ny, r, piece):
    """A copy of strip `s` truncated to within `r` of (nx, ny).

    Used when a ribbon arriving from another sheet donates its ground at a
    junction: the owning sheet needs the arriving corridor's HEIGHT over the
    donated disc, and nothing beyond it.  The centreline is cut at r along
    the edge (heights interpolate on the original line, so the slope at the
    junction is preserved) and the footprint becomes the donated piece
    itself, so the strip can never answer a level lookup outside the ground
    that actually changed hands.
    """
    ax, ay, az = s['a']
    bx, by, bz = s['b']
    run = math.hypot(bx - ax, by - ay)
    out = dict(s)
    if run > 1e-6:
        # Cut r past the NODE'S OWN PROJECTION on the centreline, not r from
        # the segment end: a stair strip is extended up to 48u beyond its end
        # node (RIBBON_STAIR_END_EXTEND), so measuring from the endpoint left
        # only r-48u of covered disc and the piece's rim lost its levels —
        # which is what disconnected ChorrolFightersGuild.
        t_node = ((nx - ax) * (bx - ax) + (ny - ay) * (by - ay)) / (run * run)
        t_node = max(0.0, min(1.0, t_node))
        da = math.hypot(ax - nx, ay - ny)
        db = math.hypot(bx - nx, by - ny)
        if da <= db:
            f = min(1.0, t_node + r / run)
            out['b'] = (ax + (bx - ax) * f, ay + (by - ay) * f,
                        az + (bz - az) * f)
        else:
            f = max(0.0, t_node - r / run)
            out['a'] = (ax + (bx - ax) * f, ay + (by - ay) * f,
                        az + (bz - az) * f)
    try:
        best = None
        for g in getattr(piece, 'geoms', (piece,)):
            if g.geom_type == 'Polygon' and (best is None
                                             or g.area > best.area):
                best = g
        if best is not None:
            out['poly'] = list(best.exterior.coords)
    except Exception:
        pass
    return out


def _ribbon_polygon_uncached(s):
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

    A STEEP strip may instead carry a 'prof' polyline (corridor._surface_profile)
    whose endpoints ARE the node heights but whose interior follows the real
    treads — the chord of a long stair edge runs tens of units off the actual
    surface wherever the flight does not span the whole edge.  This projection
    must stay IDENTICAL to the native mirror in grow.cpp (py_levels_at), or the
    scalar and batch level lookups disagree.
    """
    prof = s.get('prof')
    if prof and len(prof) >= 2:
        best_d2 = None
        best_z = prof[0][2]
        for k in range(len(prof) - 1):
            qax, qay, qaz = prof[k]
            qbx, qby, qbz = prof[k + 1]
            dx, dy = qbx - qax, qby - qay
            d2 = dx * dx + dy * dy
            t = 0.0 if d2 < 1e-9 else max(0.0, min(1.0, (
                (px - qax) * dx + (py - qay) * dy) / d2))
            cx, cy = qax + dx * t, qay + dy * t
            dd = (px - cx) ** 2 + (py - cy) ** 2
            if best_d2 is None or dd < best_d2:
                best_d2 = dd
                best_z = qaz + (qbz - qaz) * t
        return best_z
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


def attach_door_triangles(verts, tris, pending):
    """Add the reserved door triangles to the FINISHED 3D mesh.

    Called once, after every cleanup pass, so nothing downstream can split or
    drop them.  Each corner snaps to the nearest existing vertex within
    ATTACH_R so the triangle shares real edges with the surrounding mesh
    (NVNM adjacency links only across shared edges); a corner with no
    neighbour mints a new vertex at the reserved position, lifted to the
    height of the mesh around it.
    """
    if not pending:
        return verts, tris
    # The BASE endpoints must land exactly on the door line, so they snap only
    # to a vertex practically on top of them; the apex may snap further, since
    # sharing an existing interior vertex is what gives the triangle real
    # shared edges with the mesh around it.
    ATTACH_R_BASE = 2.0
    # When no vertex sits exactly on a base corner, the corner usually STILL
    # exists — decimation collapsed the wedge's hole-ring corner into a nearby
    # boundary vertex (measured on Arvena's upstairs door: corners moved 7.8
    # and 10.1u inboard), and that survivor keeps the ring's edge structure to
    # the apex.  Pull it BACK to the exact corner instead of minting a
    # duplicate beside it: the door triangle regains its full width and
    # inherits the survivor's shared edges in one move.
    ATTACH_R_BASE_PULL = 16.0
    # 12, not 8: measured on ImperialDungeon01's upper-hall door, whose apex
    # landed 8.2u from the mesh vertex it belonged on and minted a duplicate
    # instead — the door triangle then shared no edge and hung as an island.
    ATTACH_R_APEX = 12.0
    verts = [tuple(float(c) for c in v) for v in verts]
    tris = [tuple(int(i) for i in t) for t in tris]

    cell = max(ATTACH_R_APEX, 1.0)
    grid = {}
    for i, v in enumerate(verts):
        grid.setdefault((int(v[0] // cell), int(v[1] // cell)), []).append(i)

    def _near(x, y, r, z=None):
        """Existing vertex within r of (x, y) AND on the same storey as z.

        The Z gate is what keeps a door corner on its own floor: matching in
        plan alone let a corner in a multi-storey building snap to the floor
        above or below, and the resulting triangle spanned the storeys.
        """
        best = None
        gx, gy = int(x // cell), int(y // cell)
        for ddx in (-1, 0, 1):
            for ddy in (-1, 0, 1):
                for i in grid.get((gx + ddx, gy + ddy), ()):
                    if z is not None and abs(verts[i][2] - z) > STOREY_GAP_Z:
                        continue
                    d = (verts[i][0] - x) ** 2 + (verts[i][1] - y) ** 2
                    if d <= r * r and (best is None or d < best[0]):
                        best = (d, i)
        return best[1] if best else None

    def _height(x, y, z=None):
        """Height of the mesh near (x, y) ON THIS STOREY."""
        if z is not None:
            return z
        best = None
        gx, gy = int(x // cell), int(y // cell)
        for ddx in (-2, -1, 0, 1, 2):
            for ddy in (-2, -1, 0, 1, 2):
                for i in grid.get((gx + ddx, gy + ddy), ()):
                    d = (verts[i][0] - x) ** 2 + (verts[i][1] - y) ** 2
                    if best is None or d < best[0]:
                        best = (d, verts[i][2])
        return best[1] if best else 0.0

    existing = {tuple(sorted(int(i) for i in t)) for t in tris}
    # Per-edge use counts over the CURRENT mesh, kept up to date as door
    # triangles and gap fills are appended.  A door triangle that shares no
    # EDGE with the mesh is a dead island however many vertices it shares —
    # NVNM adjacency links only across shared edges.
    edge_use = {}

    def _count_edges(t, delta=1):
        for k in range(3):
            p, q = int(t[k]), int(t[(k + 1) % 3])
            e = (p, q) if p < q else (q, p)
            edge_use[e] = edge_use.get(e, 0) + delta

    for t in tris:
        _count_edges(t)

    door_keys = []
    added = 0
    # ONE TRIANGLE PER DOOR LINE.  The same door can be reserved by two sheets
    # that both border the threshold; attaching both puts two triangles on the
    # same door line and breaks the guarantee.  Key on the base line.
    seen_lines = {}

    def _attach_one(entry, allow_bridge):
        """Attach one pending door triangle.

        Returns False when the door was withdrawn or had nothing to attach
        to — retriable: a second pass can succeed once NEIGHBOURING doors
        attached (the pen's teleport door bridges to the pen gate's carved
        triangle).  True means done (attached, or legitimately skipped).
        """
        nonlocal added
        p0, p1, apex = entry[0], entry[1], entry[2]
        storey_z = entry[3] if len(entry) > 3 else None
        idx = []
        minted = 0
        for (x, y), r, is_base in ((p0, ATTACH_R_BASE, True),
                                   (p1, ATTACH_R_BASE, True),
                                   (apex, ATTACH_R_APEX, False)):
            i = _near(x, y, r, storey_z)
            if i is None and is_base:
                # Pull a decimation-displaced ring corner back to the exact
                # base corner (see ATTACH_R_BASE_PULL above).
                i = _near(x, y, ATTACH_R_BASE_PULL, storey_z)
                if i is not None:
                    verts[i] = (float(x), float(y), verts[i][2])
                    grid.setdefault((int(x // cell), int(y // cell)),
                                    []).append(i)
            if i is None:
                i = len(verts)
                verts.append((float(x), float(y), _height(x, y, storey_z)))
                grid.setdefault((int(x // cell), int(y // cell)),
                                []).append(i)
                minted += 1
            idx.append(i)
        a, b, c = idx
        # A triangle whose corners are ALL new shares no vertex with the mesh,
        # so it lands as its own component and the doorway is unreachable
        # (ImperialDungeon01's right-hand door came out as a lone 1-triangle
        # island).  Retry those corners with the wide radius so the triangle
        # attaches to the surrounding ground.
        if minted == 3:
            wide = [_near(x, y, ATTACH_R_APEX * 4.0, storey_z)
                    for (x, y) in (p0, p1, apex)]
            if all(i is None for i in wide):
                # NOTHING to attach to: this door's corridor was never built
                # (the pathgrid does not reach it), so the triangle would land
                # as a lone island — an unreachable scrap, which is worse than
                # no door triangle.  Drop the vertices just minted and skip.
                del verts[len(verts) - 3:]
                return False
            idx = []
            for (x, y), i in zip((p0, p1, apex), wide):
                if i is None:
                    i = len(verts)
                    verts.append((float(x), float(y), _height(x, y, storey_z)))
                    grid.setdefault((int(x // cell), int(y // cell)),
                                    []).append(i)
                idx.append(i)
            a, b, c = idx
        if a == b or b == c or a == c:
            return True
        # The hole was cut in 2D but the mesh is welded in 3D, so a corner may
        # have snapped onto geometry that already covers this footprint.  A
        # duplicate triangle over the same ground reads as OPPOSITE_NORMALS to
        # the CK rules, so skip when the exact triangle is already present.
        key = tuple(sorted((a, b, c)))
        if key in existing:
            return True
        # ...and the same footprint can exist under DIFFERENT vertex indices:
        # the node stitch legitimately closes the wedge hole with a bridge
        # triangle whose corners are its own coincident vertices.  Appending
        # the pending copy on top then gives all three ring edges 3+ users —
        # adjacency links none of them and the doorway SEALS into a
        # 2-triangle island (the Sanctum pit gate).  The existing triangle
        # already carries the doorway (_build_door_links flags it by
        # containment), so skip by POSITION too, storey-gated.
        z_here = verts[a][2]
        wkey = tuple(sorted((round(verts[i][0], 1), round(verts[i][1], 1))
                            for i in (a, b, c)))
        for tt in tris:
            if tuple(sorted((round(verts[i][0], 1), round(verts[i][1], 1))
                            for i in tt)) != wkey:
                continue
            if all(abs(verts[i][2] - z_here) <= STOREY_GAP_Z for i in tt):
                return True
        existing.add(key)
        # Match the surrounding winding (CCW in plan); a backwards door
        # triangle reads as downfacing to the CK rules and to the engine.
        cross = ((p1[0] - p0[0]) * (apex[1] - p0[1])
                 - (apex[0] - p0[0]) * (p1[1] - p0[1]))
        # ONE TRIANGLE PER DOOR LINE PER STOREY.  Two sheets that both border
        # a threshold each reserve it, which would put two triangles on the
        # same line; but a door line repeated at a genuinely different HEIGHT
        # is a different storey's doorway and must keep its own triangle.
        line_key = (round(p0[0], 1), round(p0[1], 1),
                    round(p1[0], 1), round(p1[1], 1))
        prev_z = seen_lines.get(line_key)
        if prev_z is not None and abs(prev_z - z_here) <= STOREY_GAP_Z:
            return True
        seen_lines[line_key] = z_here
        # DE-STACK.  The triangulation keeps any Delaunay triangle with >=50%
        # of its area inside the polygon, so ground OVERLAPPING the reserved
        # wedge survives the cut.  If such a triangle already gives one of the
        # door triangle's edges two users, appending on top makes the edge
        # 3-shared — and _compute_adjacency links only edges shared by exactly
        # two triangles, so the whole doorway DISCONNECTS (measured on
        # ImperialDungeon01's upper-hall door: mesh tris 35+1489 both used the
        # wedge side, the door triangle made it 3-shared, and the door hung as
        # a 1-triangle island).  Drop, per saturated edge, the user that
        # overlaps the door wedge most; the door triangle takes its place.
        sat = [(p, q) for (p, q) in ((a, b), (b, c), (a, c))
               if edge_use.get((min(p, q), max(p, q)), 0) >= 2]
        if sat:
            from shapely.geometry import Polygon as _SP
            try:
                door_poly = _SP([(verts[i][0], verts[i][1])
                                 for i in (a, b, c)])
            except Exception:
                door_poly = None
            if door_poly is None or not door_poly.is_valid \
                    or door_poly.area < 1.0:
                return True
            drop = set()
            ok = True
            for (p, q) in sat:
                users = [ti for ti, tt in enumerate(tris)
                         if p in tt and q in tt and ti not in drop]
                best_drop, best_ov = None, 0.0
                for ti in users:
                    try:
                        tp = _SP([(verts[i][0], verts[i][1])
                                  for i in tris[ti]])
                        ov = tp.intersection(door_poly).area
                    except Exception:
                        ov = 0.0
                    if ov > best_ov:
                        best_ov, best_drop = ov, ti
                if best_drop is None or best_ov <= 1.0:
                    # No user genuinely overlaps the wedge: this saturated
                    # edge is real double-sided ground.  Adding the door
                    # triangle would still 3-share it, so skip this door —
                    # the doorway is already meshed and _build_door_links
                    # falls back to the containing triangle.
                    ok = False
                    break
                drop.add(best_drop)
            if not ok:
                return True
            for ti in sorted(drop, reverse=True):
                _count_edges(tris[ti], -1)
                existing.discard(tuple(sorted(int(i) for i in tris[ti])))
                del tris[ti]
        # Does the door triangle share an EDGE with the mesh as it stands?
        shared = any(edge_use.get((min(p, q), max(p, q)), 0) > 0
                     for (p, q) in ((a, b), (b, c), (a, c)))
        door_tri = (a, b, c) if cross > 0 else (a, c, b)
        tris.append(door_tri)
        _count_edges(door_tri)
        door_keys.append((a, b, c))
        added += 1
        if not shared:
            # EDGE-ISOLATED door triangle: all three corners snapped to real
            # mesh vertices (or minted beside them), yet no mesh triangle uses
            # any PAIR of them, so the door is its own component — measured on
            # ImperialDungeon01's ambush-room door, whose surrounding Delaunay
            # bridged the reserved wedge's corners through OTHER vertices and
            # left a void strip along the wedge sides.  Stitch it in: walk the
            # mesh's open-edge boundary from one corner of a door-tri edge to
            # the other and fan-fill that strip (or, when the boundary runs
            # collinearly along a side edge, split the door triangle at the
            # T-junction vertices), so real edges are shared on both sides.
            fills, repl = _stitch_isolated_tri(verts, tris, edge_use,
                                               door_tri, existing,
                                               base=(a, b))
            if repl is not None:
                _count_edges(door_tri, -1)
                existing.discard(tuple(sorted(door_tri)))
                tris.pop()
                for ftri in repl:
                    tris.append(ftri)
                    _count_edges(ftri)
                    existing.add(tuple(sorted(ftri)))
            elif not fills:
                # LAST RESORT — APEX BRIDGE.  The apex snapped onto a real
                # mesh vertex, but both base corners minted far from any mesh:
                # the corridor stops short of the doorway and the quad's thin
                # crumbs were eaten by the cleanup passes (measured on the
                # CharacterGen assassins' room door, whose room ribbon ends
                # 43u west of the threshold — the boundary chain is 75u+ from
                # the base corners, beyond the stitch's jump radius).  Bridge
                # each base corner to the mesh THROUGH the apex: a triangle
                # (apex, corner, w) per corner, where w is an open-edge
                # neighbour of the apex — it shares edge apex-w with the mesh
                # and edge apex-corner with the door triangle, so the doorway
                # is edge-connected on both sides.
                fills = _apex_bridges(verts, tris, edge_use, (a, b), c,
                                      existing)
                if not fills:
                    # CARVE: the doorway region is covered by ordinary mesh
                    # the reservation never cut (another sheet, or the wedge's
                    # ring slivers died at the Delaunay area filter).  Rebuild
                    # that patch around the wedge so the door triangle gets
                    # real shared edges (see _carve_door).
                    carved = _carve_door(verts, tris, edge_use, existing,
                                         door_tri, storey_z)
                    if carved is not None:
                        removed, repl_tris = carved
                        for ti in reversed(removed):
                            _count_edges(tris[ti], -1)
                            existing.discard(tuple(sorted(int(i)
                                                          for i in tris[ti])))
                            del tris[ti]
                        for ftri in repl_tris:
                            tris.append(ftri)
                            _count_edges(ftri)
                            existing.add(tuple(sorted(ftri)))
                        fills = []      # fall through to the base-edge fan
                    # DOOR-TO-DOOR BRIDGE (retry pass only): a room with
                    # doors but no pathgrid of its own — the CharacterGen
                    # pen — keeps nothing but its door triangles, so connect
                    # this one to the NEAREST other door triangle with a
                    # 2-triangle strip and the room is traversable door to
                    # door.
                    bridged = False
                    if allow_bridge:
                        bfs = _door_to_door_bridge(verts, tris, edge_use,
                                                   existing, door_keys,
                                                   (a, b, c), storey_z)
                        if bfs:
                            for ftri in bfs:
                                tris.append(ftri)
                                _count_edges(ftri)
                                existing.add(tuple(sorted(ftri)))
                            bridged = True
                            fills = []  # fall through to the base-edge fan
                    if not bridged:
                        # Nothing could connect it.  An unreachable 1-triangle
                        # island is worse than no reserved triangle at all —
                        # withdraw it and let _build_door_links fall back to
                        # the containing mesh triangle, which IS connected
                        # (measured: AnvilFG's and ChorrolFG's rear doors,
                        # whose thresholds sit on ground the boundary chain
                        # never reaches).
                        _count_edges(door_tri, -1)
                        existing.discard(tuple(sorted(door_tri)))
                        tris.pop()
                        door_keys.pop()
                        added -= 1
                        del seen_lines[line_key]
                        return False
            for fill in fills:
                tris.append(fill)
                _count_edges(fill)
                existing.add(tuple(sorted(fill)))

        # THE BASE EDGE MUST CONNECT BOTH FACES.  Where the pathgrid runs
        # THROUGH the doorway, walkable ground exists on both sides of the
        # base line; the wedge is only ever cut on the apex side, so the far
        # face's triangulation is not constrained to the base edge and can
        # end up merely point-touching a base corner — measured on Pinarus's
        # upstairs bedroom door, whose landing met the door triangle only at
        # one vertex and the upstairs floor fell into two components.  When
        # the base edge has no second user, fan the far face's open boundary
        # onto it so the doorway carries adjacency across.
        eb = (min(a, b), max(a, b))
        if edge_use.get(eb, 0) == 1:
            bfills, _ = _stitch_isolated_tri(verts, tris, edge_use, door_tri,
                                             existing, base=(a, b),
                                             only_edges=[(a, b, c)])
            for fill in bfills:
                tris.append(fill)
                _count_edges(fill)
                existing.add(tuple(sorted(fill)))
        return True

    # TWO PASSES: a door with nothing to attach to on the first pass can
    # succeed on the second once its neighbours attached.
    remaining = [e for e in pending if not _attach_one(e, False)]
    for e in remaining:
        _attach_one(e, True)

    # NOTE: no neighbour-splitting here.  An earlier version split mesh
    # triangles against the door corners to force shared edges, but it matched
    # in XY only and happily split a triangle on ANOTHER STOREY -- fanning 13
    # storey-spanning triangles across ChorrolFightersGuild's three floors
    # (worst dz 434u).  The reserved hole already leaves the door's own edges
    # on the boundary, so the surrounding mesh meets it without any splitting.
    return verts, tris


def _carve_door(verts, tris, edge_use, existing, door_tri, storey_z):
    """LAST-RESORT reservation on the FINISHED mesh: carve the wedge out.

    When the pipeline's reservation never reached the final mesh (the wedge's
    ring slivers died at the Delaunay area filter, or ANOTHER sheet's ordinary
    ground covers the doorway uncut), the appended door triangle floats over
    existing triangles sharing nothing.  Carve locally: remove every
    same-storey triangle genuinely overlapping the wedge, retriangulate the
    removed region MINUS the wedge from its own vertices (boundary edges to
    the kept mesh and the wedge's three edges forced back via
    _recover_constraints), and the door triangle ends up sharing real edges
    on every side.  Returns the replacement triangles, or None when nothing
    overlaps (a true no-mesh doorway — the caller withdraws as before).

    All replacement corners are EXISTING vertices (the removed triangles' own
    plus the door triangle's three), so heights and welds are inherited, and
    the storey gate keeps a stacked floor above/below untouched.
    """
    from shapely.geometry import Polygon as _SP
    from shapely.ops import unary_union as _uu

    a, b, c = door_tri[0], door_tri[1], door_tri[2]
    ids = sorted(set(int(i) for i in door_tri))
    try:
        wedge = _SP([(verts[i][0], verts[i][1]) for i in (a, b, c)])
    except Exception:
        return None
    if not wedge.is_valid or wedge.area < 1.0:
        return None
    wa = wedge.area
    minx, miny, maxx, maxy = wedge.bounds
    cand = []
    for ti, t in enumerate(tris):
        if sorted(set(int(i) for i in t)) == ids:
            continue                     # the door triangle itself
        pts = [verts[int(i)] for i in t]
        if storey_z is not None and any(
                abs(p[2] - storey_z) > STOREY_GAP_Z for p in pts):
            continue
        if (max(p[0] for p in pts) < minx or min(p[0] for p in pts) > maxx
                or max(p[1] for p in pts) < miny
                or min(p[1] for p in pts) > maxy):
            continue
        try:
            tp = _SP([(p[0], p[1]) for p in pts])
            ov = tp.intersection(wedge).area
        except Exception:
            continue
        if ov > max(1.0, 0.01 * wa) or ov > 0.4 * max(tp.area, 1e-6):
            cand.append((ti, tp))
    if not cand:
        return None
    try:
        region = _uu([tp for (_ti, tp) in cand])
        cut = region.difference(wedge)
    except Exception:
        return None

    # Vertex set: the removed triangles' corners + the door triangle's.
    vset = sorted({int(i) for (ti, _tp) in cand for i in tris[ti]} | set(ids))
    if len(vset) < 3:
        return None
    # Boundary edges to the KEPT mesh: every edge of a removed triangle whose
    # other user survives.  These must come back as edges or the carve tears
    # the surrounding mesh into T-junctions.
    removed = {ti for (ti, _tp) in cand}
    anchor = []
    for (ti, _tp) in cand:
        t = tris[ti]
        for k in range(3):
            p, q = int(t[k]), int(t[(k + 1) % 3])
            e = (min(p, q), max(p, q))
            if edge_use.get(e, 0) >= 2:
                # shared with something; with another removed tri?
                others = [tj for (tj, _t2) in cand if tj != ti
                          and p in tris[tj] and q in tris[tj]]
                if not others:
                    anchor.append((p, q))

    import numpy as _np
    from scipy.spatial import Delaunay as _DT
    arr = _np.asarray([[verts[i][0], verts[i][1]] for i in vset], float)
    try:
        dt = _DT(arr)
    except Exception:
        return None
    from shapely.geometry import Point as _Pt
    out = []
    for (i0, i1, i2) in dt.simplices:
        g0, g1, g2 = vset[i0], vset[i1], vset[i2]
        if sorted((g0, g1, g2)) == ids:
            continue
        cx = (arr[i0][0] + arr[i1][0] + arr[i2][0]) / 3.0
        cy = (arr[i0][1] + arr[i1][1] + arr[i2][1]) / 3.0
        if not cut.buffer(0.5).contains(_Pt(cx, cy)):
            continue
        if tuple(sorted((g0, g1, g2))) in existing:
            continue
        out.append((g0, g1, g2))
    # Force the kept-mesh boundary edges and the wedge's own edges.
    segs = [((verts[p][0], verts[p][1]), (verts[q][0], verts[q][1]))
            for (p, q) in anchor]
    segs += [((verts[p][0], verts[p][1]), (verts[q][0], verts[q][1]))
             for (p, q) in ((a, b), (b, c), (c, a))]
    if out and segs:
        sub_verts = [list(verts[i][:2]) for i in vset]
        remap = {g: k for k, g in enumerate(vset)}
        sub_tris = [(remap[g0], remap[g1], remap[g2])
                    for (g0, g1, g2) in out]
        rv, rt = _recover_constraints(sub_verts, sub_tris, segs)
        if len(rv) == len(sub_verts):    # no new vertices minted: safe map
            out = [(vset[i0], vset[i1], vset[i2]) for (i0, i1, i2) in rt]
    if not out:
        return None
    # CCW in plan.
    fixed = []
    for (g0, g1, g2) in out:
        v0, v1, v2 = verts[g0], verts[g1], verts[g2]
        cr = ((v1[0] - v0[0]) * (v2[1] - v0[1])
              - (v2[0] - v0[0]) * (v1[1] - v0[1]))
        if abs(cr) < 0.5:
            continue
        fixed.append((g0, g1, g2) if cr > 0 else (g0, g2, g1))
    if not fixed:
        return None
    return (sorted(removed), fixed)


def _door_to_door_bridge(verts, tris, edge_use, existing, door_keys,
                         door_tri, storey_z):
    """Strip of two triangles from a stranded door triangle to the nearest
    other DOOR triangle on the same storey.

    (c, a2, b2) shares the other door's base edge; (a, c, a2) shares this
    door's side edge a-c and the strip's own c-a2 — so the pair is fully
    edge-connected and a doorway-only room (the CharacterGen pen) becomes
    traversable door to door.  Returns [] when no other door triangle is
    within DOOR_BRIDGE_R.
    """
    DOOR_BRIDGE_R = 384.0
    a, b, c = door_tri
    cx = sum(verts[i][0] for i in door_tri) / 3.0
    cy = sum(verts[i][1] for i in door_tri) / 3.0
    best = None
    for other in door_keys:
        if set(other) == set(door_tri):
            continue
        if tuple(sorted(other)) not in existing:
            continue                     # replaced/withdrawn since
        ox = sum(verts[i][0] for i in other) / 3.0
        oy = sum(verts[i][1] for i in other) / 3.0
        oz = sum(verts[i][2] for i in other) / 3.0
        if storey_z is not None and abs(oz - storey_z) > STOREY_GAP_Z:
            continue
        d = math.hypot(ox - cx, oy - cy)
        if d > DOOR_BRIDGE_R:
            continue
        if best is None or d < best[0]:
            best = (d, other)
    if best is None:
        return []
    a2, b2, _c2 = best[1]
    out = []
    for tri in ((c, a2, b2), (a, c, a2)):
        v0, v1, v2 = verts[tri[0]], verts[tri[1]], verts[tri[2]]
        cr = ((v1[0] - v0[0]) * (v2[1] - v0[1])
              - (v2[0] - v0[0]) * (v1[1] - v0[1]))
        if abs(cr) < 1.0:
            return []
        t = tri if cr > 0 else (tri[0], tri[2], tri[1])
        if tuple(sorted(t)) in existing:
            return []
        out.append(t)
    return out


def _apex_bridges(verts, tris, edge_use, base_pair, apex, existing):
    """Bridge triangles (apex, corner, w) hooking a stranded door triangle in.

    Used when the door triangle's ONLY real mesh vertex is its apex: for each
    base corner, pick the apex's open-edge (use==1) neighbour nearest that
    corner and lay one triangle across.  Each bridge shares a real mesh edge
    (apex-w) and a door-triangle side edge (apex-corner).  Returns [] when the
    apex has no open edges on the door's storey — the caller withdraws then.
    """
    BRIDGE_MAX_XY = 192.0
    az = verts[apex][2]
    va = verts[apex]
    nbrs = []
    for (p, q), n in edge_use.items():
        if n != 1:
            continue
        w = q if p == apex else (p if q == apex else None)
        if w is None or w in base_pair:
            continue
        if abs(verts[w][2] - az) > STOREY_GAP_Z:
            continue
        if math.hypot(verts[w][0] - va[0], verts[w][1] - va[1]) \
                > BRIDGE_MAX_XY:
            continue
        nbrs.append(w)
    if not nbrs:
        return []
    out = []
    used = set()
    for corner in base_pair:
        vc = verts[corner]
        # Each bridge must take its OWN neighbour: two bridges sharing one w
        # both use edge (apex, w), driving it to 3 users — and a 3-shared
        # edge disconnects under _compute_adjacency, turning the door + both
        # bridges into a 3-triangle island (measured on Pinarus's basement
        # door).  One bridge alone already carries connectivity.
        cands = [i for i in nbrs if i not in used]
        if not cands:
            break
        w = min(cands, key=lambda i: (verts[i][0] - vc[0]) ** 2
                + (verts[i][1] - vc[1]) ** 2)
        vw = verts[w]
        cross = ((vc[0] - va[0]) * (vw[1] - va[1])
                 - (vw[0] - va[0]) * (vc[1] - va[1]))
        if abs(cross) < 1.0:
            continue
        t = (apex, corner, w) if cross > 0 else (apex, w, corner)
        if tuple(sorted(t)) in existing:
            continue
        used.add(w)
        out.append(t)
    return out


def _stitch_isolated_tri(verts, tris, edge_use, door_tri, existing,
                         base=None, only_edges=None):
    """Connect an edge-isolated door triangle to the surrounding mesh.

    Returns (fills, replacement):
      * fills — extra triangles filling the void strip between one of the door
        triangle's edges and the mesh's open boundary (empty when none found);
      * replacement — when the mesh boundary runs COLLINEARLY along a door-tri
        side edge (a T-junction), the door triangle itself must be split at
        those vertices; this is the list of sub-triangles to swap in for it
        (None otherwise).  The base line (`base`, the threshold edge) is never
        split — the sub-triangle carrying it keeps the full doorway width.

    For each edge (P, Q) of the door triangle, look for a short chain of OPEN
    mesh edges (used by exactly one triangle) leading from P to Q on the far
    side of that edge from the triangle's third corner.  Such a chain is the
    mesh boundary detouring around the reserved wedge; the area between it and
    the door edge is the void strip the Delaunay dropped.  Fan triangles
    (P, w_i, w_i+1) fill it: each shares a chain edge with the mesh, and the
    last shares the full edge with the door triangle.  Every check is
    conservative; an unfixable door stays as it was.
    """
    import math as _m
    a, b, c = door_tri
    z0 = sum(verts[i][2] for i in door_tri) / 3.0
    cx = sum(verts[i][0] for i in door_tri) / 3.0
    cy = sum(verts[i][1] for i in door_tri) / 3.0
    # The search box must contain the door triangle's own corners with room to
    # spare — a fixed 96 was smaller than half of a 194u-wide door's base, so
    # its corners fell outside the box and the BFS could never leave them.
    R = max(96.0, 64.0 + max(_m.hypot(verts[i][0] - cx, verts[i][1] - cy)
                             for i in door_tri))
    MAX_HOPS = 5

    # Open edges near the door triangle, on its storey.
    near_v = {}
    for i, v in enumerate(verts):
        if (abs(v[0] - cx) <= R and abs(v[1] - cy) <= R
                and abs(v[2] - z0) <= STOREY_GAP_Z):
            near_v[i] = v
    # The door triangle is already appended, so its OWN three edges are open
    # too — leave them out of the graph or the BFS "reaches" the far corner
    # through the door triangle itself and reports a trivial 1-hop chain.
    own = {(min(p, q), max(p, q))
           for (p, q) in ((a, b), (b, c), (a, c))}
    adjacency = {}
    for (p, q), n in edge_use.items():
        if n != 1 or (p, q) in own:
            continue
        if p not in near_v or q not in near_v:
            continue
        adjacency.setdefault(p, []).append(q)
        adjacency.setdefault(q, []).append(p)
    for vs in adjacency.values():
        vs.sort()                       # deterministic walk order

    def _side(px, py, qx, qy, x, y):
        return (qx - px) * (y - py) - (qy - py) * (x - px)

    fills = []
    JUMP_R = 64.0
    for (P, Q, O) in (only_edges or ((a, b, c), (b, c, a), (c, a, b))):
        vp, vq, vo = verts[P], verts[Q], verts[O]
        o_side = _side(vp[0], vp[1], vq[0], vq[1], vo[0], vo[1])
        if abs(o_side) < 1e-6:
            continue
        # BFS shortest open-edge chain P -> Q whose interior vertices all lie
        # on the OPPOSITE side of P-Q from O (outside the door triangle).
        # P itself may have NO open edges (a minted base corner whose ground
        # the wedge cut consumed) — seed the walk with every boundary vertex
        # within JUMP_R of P on the valid side, as a virtual first hop; the
        # fan triangle across that hop still shares its CHAIN edge with the
        # mesh, which is what carries connectivity.
        prev = {P: None}
        queue = [(0, P)]
        for w in adjacency:
            if w in (P, Q, O) or w in prev:
                continue
            vw = verts[w]
            if _m.hypot(vw[0] - vp[0], vw[1] - vp[1]) > JUMP_R:
                continue
            s = _side(vp[0], vp[1], vq[0], vq[1], vw[0], vw[1])
            if s * o_side > 0:
                continue
            prev[w] = P
            queue.append((1, w))
        found = False
        while queue:
            hops, u = queue.pop(0)
            if u == Q:
                found = True
                break
            if hops >= MAX_HOPS:
                continue
            for w in adjacency.get(u, ()):
                if w in prev:
                    continue
                if w != Q:
                    vw = verts[w]
                    s = _side(vp[0], vp[1], vq[0], vq[1], vw[0], vw[1])
                    if s * o_side > 0:
                        continue        # inside the door triangle's half-plane
                prev[w] = u
                queue.append((hops + 1, w))
        if not found:
            continue
        chain = []
        u = Q
        while u is not None:
            chain.append(u)
            u = prev[u]
        chain.reverse()                 # P ... Q
        if len(chain) < 3:
            continue                    # direct edge would already be shared

        # COLLINEAR chain: the mesh boundary runs ALONG this door-tri edge,
        # subdivided at T-junction vertices (measured on ImperialDungeon01's
        # east exit: boundary v197-v89-v87 lies exactly on the wedge side).
        # There is no strip to fill — instead split the door triangle's edge
        # at those vertices so both sides share real edges.  Never the base.
        interior = chain[1:-1]

        def _off_line(w):
            vw = verts[w]
            dx, dy = vq[0] - vp[0], vq[1] - vp[1]
            L = _m.hypot(dx, dy)
            if L < 1e-9:
                return 1e9
            return abs((vw[0] - vp[0]) * dy - (vw[1] - vp[1]) * dx) / L

        if all(_off_line(w) <= 2.0 for w in interior):
            if base is not None and {P, Q} == set(base):
                continue                # the threshold edge stays whole
            ws = sorted(interior,
                        key=lambda w: ((verts[w][0] - vp[0]) ** 2
                                       + (verts[w][1] - vp[1]) ** 2))
            pts_chain = [P] + ws + [Q]
            repl = []
            okr = True
            for u0, u1 in zip(pts_chain, pts_chain[1:]):
                vu, vw = verts[u0], verts[u1]
                cr = ((vw[0] - vu[0]) * (vo[1] - vu[1])
                      - (vo[0] - vu[0]) * (vw[1] - vu[1]))
                if abs(cr) < 1.0:
                    okr = False
                    break
                repl.append((u0, u1, O) if cr > 0 else (u0, O, u1))
            if okr and repl:
                return [], repl
            continue

        # Fan from P; require consistent (non-degenerate) winding throughout.
        cand = []
        ok = True
        for w0, w1 in zip(chain[1:-1], chain[2:]):
            v0, v1 = verts[w0], verts[w1]
            cross = ((v0[0] - vp[0]) * (v1[1] - vp[1])
                     - (v1[0] - vp[0]) * (v0[1] - vp[1]))
            if abs(cross) < 1.0:
                ok = False
                break
            t = (P, w0, w1) if cross > 0 else (P, w1, w0)
            if tuple(sorted(t)) in existing:
                ok = False
                break
            cand.append(t)
        if ok and cand:
            # SHAPE-RANK the fans instead of taking the first that connects.
            # This fill runs AFTER every quality pass, so whatever it emits
            # ships: an unranked first-match fan put an 8u^2 / badness-28
            # needle through ImperialDungeon01's prison door (0001FC1E) and
            # a cluster of 85-300u^2 scraps around it.  Connectivity still
            # wins over shape — a needle that connects a doorway beats an
            # unreachable door — so a bad fan is kept as a candidate and only
            # loses to a BETTER one, never to nothing.
            fills.append((_fan_cost(verts, cand), cand))
    if fills:
        return min(fills, key=lambda fc: fc[0])[1], None
    return [], None


def _fan_cost(verts, cand):
    """Worst badness in a candidate fan (lower is better)."""
    from . import corridor_clean
    return max(corridor_clean._badness(verts, t) for t in cand)




def _door_edge_on_part(edge, part, tol=2.0):
    """Does this door base line belong to `part` (inside OR on its outline)?

    The threshold edge of a door footprint is part of the union BOUNDARY, so a
    strict interior test silently drops it and the door never gets its forced
    edge.  Accept the edge when its midpoint is within the polygon or within
    `tol` of its boundary — the FULL boundary, holes included: a doorway in
    the middle of a floor plate lies on an INTERIOR ring (Arvena's upstairs
    door base sat on a hole of its sheet, 137u from the exterior ring, so the
    exterior-only test never claimed it and the door lost its reservation).
    """
    from shapely.geometry import Point
    mx = 0.5 * (edge[0][0] + edge[1][0])
    my = 0.5 * (edge[0][1] + edge[1][1])
    p = Point(mx, my)
    try:
        return part.contains(p) or part.boundary.distance(p) <= tol
    except Exception:
        return False


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


# How far off a door base line a polygon corner may sit and still be snapped
# onto it.  Well under a foot width, so only corners the union genuinely put ON
# the line move, and they move along it.
DOOR_SNAP_PERP = 4.0


def _snap_outline_to_door_lines(poly, fixed_edges):
    """Move ring corners lying ON a door base line onto its nearer endpoint.

    Leaves the ring's structure alone (same vertex count, same winding); only
    corners strictly BETWEEN a door line's endpoints move, and they move along
    that line, so the covered ground is unchanged to within DOOR_SNAP_PERP.
    Degenerate repeats collapse out naturally when shapely rebuilds the ring.
    """
    from shapely.geometry import Polygon as _SnapP
    lines = []
    for e in fixed_edges:
        p0, p1 = e[0], e[1]
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        dl = math.hypot(dx, dy)
        if dl > 1e-9:
            lines.append((p0, p1, dx / dl, dy / dl, dl))
    if not lines:
        return poly

    def _snap_ring(coords):
        out = []
        for (x, y) in coords:
            for (q0, q1, ux, uy, dl) in lines:
                vx, vy = x - q0[0], y - q0[1]
                if abs(-vx * uy + vy * ux) > DOOR_SNAP_PERP:
                    continue
                t = vx * ux + vy * uy
                if not (DOOR_SNAP_PERP < t < dl - DOOR_SNAP_PERP):
                    continue        # already at (or beyond) an endpoint
                x, y = (q0 if t < 0.5 * dl else q1)
                break
            if not out or (abs(out[-1][0] - x) > 1e-9
                           or abs(out[-1][1] - y) > 1e-9):
                out.append((x, y))
        while len(out) > 1 and out[0] == out[-1]:
            out.pop()
        return out

    try:
        shell = _snap_ring(list(poly.exterior.coords))
        if len(shell) < 3:
            return poly
        holes = []
        for r in poly.interiors:
            h = _snap_ring(list(r.coords))
            if len(h) >= 3:
                holes.append(h)
        out = _SnapP(shell, holes)
        if not out.is_valid:
            out = out.buffer(0)
        if (out.is_empty or not out.is_valid
                or not isinstance(out, _SnapP)
                or abs(out.area - poly.area) > 0.02 * max(poly.area, 1.0)):
            return poly
        return out
    except Exception:
        return poly


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

    `fixed_edges` is a list of (p0, p1, apex) door triangles — base line plus
    the analytically fixed apex (corridor_doors computes it: full doorway
    width, fixed depth, on the pathgrid's side).  Each is reserved out of the
    polygon before triangulation and stitched back in afterwards; the base
    endpoints are also inserted as Steiner points and no interior sample is
    placed near the base line, so the surrounding mesh meets the door triangle
    edge-to-edge.

    `steep_seeds` is a list of (x, y) points along STEEP ribbon centrelines
    (stairs, ramps).  A uniform target_edge triangle on a staircase climbs more
    than one storey gap across its corners and is dropped by the per-surface
    emission — the whole stair vanishes.  These seeds are forced in at a fine
    spacing so the stair keeps short, gently-climbing triangles that survive.
    """
    from shapely.geometry import Point, Polygon as _ShPoly
    from shapely.prepared import prep

    ext = list(poly.exterior.coords)[:-1]
    if len(ext) < 3:
        return [], []

    # RESERVE THE DOOR TRIANGLE.  Vanilla marks a door with ONE triangle whose
    # long edge is the whole doorway.  Every attempt to coax that out of the
    # Delaunay failed the same way: the door line is on the union BOUNDARY, so
    # the ribbon's own outline corners land on it and split it into 3-4 pieces,
    # and no amount of seeding, keep-out or constraint recovery can remove a
    # corner that is already baked into the polygon.
    #
    # So the region is CUT OUT of the polygon before triangulation — the
    # triangulator fills around it as if it were a hole, cannot subdivide what
    # it never sees — and the single door triangle is stitched back in
    # afterwards.  The wedge's shape is FIXED by corridor_doors (full doorway
    # base, deterministic depth, apex on the pathgrid's side): the reservation
    # never moves, shrinks or flips it.  A wedge that severs the sheet is
    # allowed — the MultiPolygon branch below triangulates every significant
    # piece, and the door triangle itself (base edge on one piece, apex on the
    # other, welded back in attach_door_triangles) is what reconnects them.
    # The old guard that SKIPPED reserving such a door instead demoted it to
    # whatever sliver the fallback containing-triangle link happened to find.
    # SNAP THE OUTLINE ONTO THE DOOR LINE'S ENDPOINTS FIRST.  The ribbons that
    # meet a threshold contribute their own corners ON the base line, and those
    # are baked into the polygon before anything here runs.  The wedge cut then
    # hands each piece a boundary that still carries them, so the doorway is
    # triangulated as several triangles instead of the one guaranteed one and
    # the leftovers ship as needles — measured on Pinarus's upstairs door
    # 113054, whose 99u base carried intruders at -359.4 and -277.3 and came
    # out as a 2930u^2 door triangle plus a 237u^2 badness-5.5 rogue.
    #
    # Snapping each such corner to the NEARER endpoint (rather than deleting
    # it) is what makes this safe: the ring keeps its vertex count and winding,
    # every edge that arrived at the corner still arrives at a point on the
    # same line, and no ground is added or removed — a deletion instead
    # stretched the mesh over ground no ribbon covered and cost 5 door-passage
    # samples on AnvilFightersGuild's teleport door.
    if fixed_edges:
        poly = _snap_outline_to_door_lines(poly, fixed_edges)


    door_tris_out = []
    reserved = []
    for e in (fixed_edges or ()):
        if len(e) < 3 or e[2] is None:
            continue
        p0, p1, apex = e[0], e[1], e[2]
        tri = _ShPoly([p0, p1, apex])
        if not tri.is_valid or tri.area < 1.0:
            continue
        reserved.append(tri)
        # Carry the door's storey height when known: a wedge that consumes
        # its whole part (below) emits no mesh of its own, so the pending
        # entry cannot be height-tagged from emitted vertices later.
        door_z = e[3] if len(e) > 3 and e[3] is not None else None
        entry = (tuple(p0), tuple(p1), tuple(apex))
        door_tris_out.append(entry if door_z is None
                             else entry + (float(door_z),))
    # Cut the wedges out, then triangulate every remaining piece AND the
    # wedges themselves into ONE shared vertex space below.  The wedge's ring
    # coordinates appear verbatim on the pieces' boundaries (the cut created
    # them), so after the shared re-index the door triangle SHARES its base
    # and side edges with the surrounding mesh by construction — there is
    # nothing to stitch back later, and no repair pass to go wrong.  (The
    # old shape — leave a hole, attach the triangle after all cleanup — was
    # never robust: the attach needed the ring to survive weld/decimation
    # exactly, and every drifted corner produced an island door.)
    parts = [poly]
    if reserved:
        try:
            cut = poly
            for r in reserved:
                cut = cut.difference(r)
            if cut.geom_type == 'GeometryCollection':
                from shapely.ops import unary_union as _uu
                gs = [g for g in cut.geoms if g.geom_type == 'Polygon']
                cut = _uu(gs) if gs else cut
            if cut.is_empty:
                # The wedges consumed the whole part: this part WAS the
                # doorway apron, and the door triangles replace its ground.
                parts = []
            elif cut.geom_type == 'Polygon':
                parts = [cut]
            elif cut.geom_type == 'MultiPolygon':
                # Every piece is real ground: the slivers on either side of
                # the wedge are the door triangle's edge-connection to the
                # corridor (an earlier SPLIT_TINY_AREA gate dropped the small
                # ones and doorways came out point-joined).  A piece that
                # genuinely leads nowhere is culled later by the island pass,
                # which knows about reachability; area is not a proxy for it.
                parts = [g for g in cut.geoms if g.geom_type == 'Polygon'
                         and g.area >= 1.0]
            else:
                parts = []
        except Exception:
            reserved, door_tris_out = [], []
            parts = [poly]

    # The triangulation is a TRUE constrained Delaunay (GEOS, via shapely's
    # constrained_delaunay_triangles): every ring edge is a constraint the
    # result must conform to, so no triangle can cross a hole or the outline,
    # the door base line survives as exactly one edge, no coverage is ever
    # lost to an in/out filter, and the whole part triangulates against ONE
    # consistent vertex set — the point-set-Delaunay predecessor guaranteed
    # none of these and each miss was a disconnection (giant triangles
    # spanning the door wedge, T-junction seams, missing slivers beside the
    # door triangle).
    pt_index = {}
    pts = []

    def _pid(x, y):
        """Index of the mesh vertex at (x, y), deduped at millimetre scale."""
        key = (round(float(x), 3), round(float(y), 3))
        i = pt_index.get(key)
        if i is None:
            i = len(pts)
            pts.append((float(x), float(y)))
            pt_index[key] = i
        return i

    tris_out = []

    # THE DOOR BASE LINE IS ON THE OUTLINE.  The door quad's threshold edge is
    # part of the union boundary, so the densify loop below would drop samples
    # ALONG it and chop the one big door triangle into pieces — measured on the
    # CharacterGen assassins' cell door, whose 115u base line came out as a
    # 26.8u + 21.6u pair and left a 571-unit scrap as the Door Triangle (every
    # vanilla door triangle is >= 992).  Densification is therefore suppressed
    # on any boundary segment lying along a door base line; the line keeps its
    # two endpoints and nothing in between, which is exactly what makes the
    # Delaunay span it with a single triangle.
    fixed_edges = fixed_edges or []
    door_guard = []
    for e in fixed_edges:
        p0, p1 = e[0], e[1]
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        dl = math.hypot(dx, dy)
        if dl > 1e-9:
            door_guard.append((p0, p1, dx / dl, dy / dl, dl))

    def _on_door_line(x0, y0, x1, y1):
        """True if this boundary segment runs ALONG a door base line."""
        for (q0, _q1, ux, uy, dl) in door_guard:
            ok = True
            for (px, py) in ((x0, y0), (x1, y1),
                             (0.5 * (x0 + x1), 0.5 * (y0 + y1))):
                vx, vy = px - q0[0], py - q0[1]
                t = vx * ux + vy * uy
                perp = abs(-vx * uy + vy * ux)
                if perp > 4.0 or not (-4.0 <= t <= dl + 4.0):
                    ok = False
                    break
            if ok:
                return True
        return False

    # 1. Densify the rings at target_edge so boundary triangles come out the
    #    same scale as interior ones — EXCEPT along a door base line, which
    #    keeps only its endpoints so the doorway spans one edge.  The
    #    densified rings ARE the CDT's vertex set: GEOS's constrained
    #    Delaunay uses exactly the polygon's vertices, so every vertex here
    #    appears in the mesh and no others.
    def _densify_ring(ring):
        coords = list(ring.coords)
        out = []
        for i in range(len(coords) - 1):
            x0, y0 = coords[i]
            x1, y1 = coords[i + 1]
            out.append((x0, y0))
            if _on_door_line(x0, y0, x1, y1):
                continue
            seg = math.hypot(x1 - x0, y1 - y0)
            n = int(seg // target_edge)
            for s in range(1, n + 1):
                f = s / (n + 1)
                out.append((x0 + (x1 - x0) * f, y0 + (y1 - y0) * f))
        return out

    from shapely.geometry import Polygon as _CPoly
    from shapely import constrained_delaunay_triangles as _cdt
    for part in parts:
        try:
            shell = _densify_ring(part.exterior)
            hole_rings = [_densify_ring(r) for r in part.interiors]
            dense = _CPoly(shell,
                           holes=[h for h in hole_rings if len(h) >= 3])
            if not dense.is_valid:
                dense = dense.buffer(0)
            cdt_out = _cdt(dense)
        except Exception:
            continue
        # Re-index the CDT triangles into the ONE shared vertex array.  GEOS
        # emits per-triangle coordinate rings; triangles that share a corner
        # repeat its exact coordinates, and the wedge cut stamped identical
        # coordinates on every piece it touched — so the coordinate-keyed
        # index rebuilds a single shared-vertex mesh across all pieces.
        part_tris = []
        for g in getattr(cdt_out, 'geoms', ()):
            if g.geom_type != 'Polygon':
                continue
            ring = list(g.exterior.coords)[:-1]
            if len(ring) != 3:
                continue
            ia, ib, ic = (_pid(x, y) for (x, y) in ring)
            if ia != ib and ib != ic and ia != ic:
                part_tris.append((ia, ib, ic))
        # INTERIOR LATTICE.  A boundary-only CDT triangulates every region
        # wider than one triangle as a FAN — long thin triangles are then a
        # mathematical certainty, not a tuning problem.  Near-equilateral
        # triangles ("the long side no more than twice the short one") need
        # interior vertices, so a hex lattice at target_edge spacing is
        # inserted into the CDT and the diagonals are flipped to shape.
        part_tris = _hex_refine(part, pts, _pid, part_tris, target_edge)
        tris_out.extend(part_tris)

    # 2. THE DOOR TRIANGLES, as ordinary mesh.  Their ring coordinates are
    #    already vertices of the neighbouring pieces, so each wedge lands
    #    edge-connected on every side it has a neighbour on — the vanilla
    #    "one big triangle spanning the doorway" with nothing to stitch.
    door_ring_edges = set()
    for d in door_tris_out:
        (b0, b1), apex = (d[0], d[1]), d[2]
        ia, ib, ic = _pid(*b0), _pid(*b1), _pid(*apex)
        if ia == ib or ib == ic or ia == ic:
            continue
        cross = ((b1[0] - b0[0]) * (apex[1] - b0[1])
                 - (apex[0] - b0[0]) * (b1[1] - b0[1]))
        tris_out.append((ia, ib, ic) if cross > 0 else (ia, ic, ib))
        for (u, v) in ((ia, ib), (ib, ic), (ia, ic)):
            door_ring_edges.add((u, v) if u < v else (v, u))

    verts = [(float(x), float(y)) for (x, y) in pts]
    tris = tris_out
    if not tris:
        return _earcut_fallback(poly)

    # 3. Conforming refinement over STEEP ground.  The CDT builds from ring
    #    vertices only, so a stair/ramp corridor comes out as a few large
    #    triangles whose corners span more than a storey step — the
    #    per-surface emission would drop them and the whole stair vanishes
    #    (this is what the old point-set pipeline used its fine steep seeds
    #    for).  Bisect any triangle a steep centreline seed lands in, at its
    #    longest edge, splitting the neighbour across that edge at the same
    #    midpoint so the mesh STAYS conforming, until the steep ground is
    #    finely meshed.  Door triangles and their ring edges are exempt: the
    #    doorway must stay ONE triangle.
    steep_pts = [(sx, sy) for (sx, sy, st) in (steep_seeds or ()) if st]
    if steep_pts:
        verts, tris = _refine_steep(verts, tris, steep_pts,
                                    protected=door_ring_edges)
    return verts, tris


def _hex_refine(part, pts, pid, tris, spacing):
    """Insert a hex lattice of interior vertices into a part's CDT and flip
    the diagonals to shape.

    GEOS's constrained Delaunay uses ONLY the polygon's own vertices, so any
    region wider than one triangle comes out as a fan of long slivers — no
    amount of post-collapse can make those near-equilateral, because the
    vertices to break them simply do not exist.  A hex lattice (offset rows,
    the dual of the honeycomb) at target-edge spacing is the arrangement
    whose Delaunay IS near-equilateral; each point splits its containing
    triangle 3 ways and the ratio flips below restore local Delaunay-ness.

    Points are kept 0.45*spacing clear of existing vertices and of the part
    boundary (erosion), so no insertion can itself mint a sliver.  The
    lattice is anchored on the part's own bounds — deterministic per part.
    """
    if not tris:
        return tris
    try:
        eroded = part.buffer(-0.45 * spacing)
        if eroded.is_empty:
            return _flip2d(pts, tris)
    except Exception:
        return _flip2d(pts, tris)
    minx, miny, maxx, maxy = part.bounds
    row_h = spacing * 0.8660254037844386
    cand = []
    row = 0
    y = miny + 0.5 * row_h
    while y < maxy:
        x = minx + (0.25 if row % 2 == 0 else 0.75) * spacing
        while x < maxx:
            cand.append((x, y))
            x += spacing
        y += row_h
        row += 1
    if cand:
        try:
            import shapely as _sh
            hits = _sh.contains_xy(eroded, [c[0] for c in cand],
                                   [c[1] for c in cand])
            cand = [c for c, k in zip(cand, hits.tolist()) if k]
        except Exception:
            from shapely.prepared import prep as _prep
            from shapely.geometry import Point as _Pt
            pe = _prep(eroded)
            cand = [c for c in cand if pe.contains(_Pt(c))]
    if not cand:
        return _flip2d(pts, tris)

    T = [tuple(t) for t in tris]
    alive = [True] * len(T)
    cell = spacing
    tgrid = {}

    def _addt(ti):
        xs = [pts[i][0] for i in T[ti]]
        ys = [pts[i][1] for i in T[ti]]
        for gx in range(int(min(xs) // cell), int(max(xs) // cell) + 1):
            for gy in range(int(min(ys) // cell), int(max(ys) // cell) + 1):
                tgrid.setdefault((gx, gy), []).append(ti)

    for ti in range(len(T)):
        _addt(ti)
    vgrid = {}

    def _addv(i):
        p = pts[i]
        vgrid.setdefault((int(p[0] // cell), int(p[1] // cell)), []).append(i)

    for i in sorted({i for t in T for i in t}):
        _addv(i)

    min_d2 = (0.45 * spacing) ** 2
    for (px, py) in cand:
        gx, gy = int(px // cell), int(py // cell)
        if any((pts[i][0] - px) ** 2 + (pts[i][1] - py) ** 2 < min_d2
               for ddx in (-1, 0, 1) for ddy in (-1, 0, 1)
               for i in vgrid.get((gx + ddx, gy + ddy), ())):
            continue
        hit = None
        for ti in tgrid.get((gx, gy), ()):
            if not alive[ti]:
                continue
            a, b, c = T[ti]
            ax, ay = pts[a]
            bx, by = pts[b]
            cx, cy = pts[c]
            d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
            if abs(d) < 1e-9:
                continue
            l0 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / d
            l1 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / d
            l2 = 1.0 - l0 - l1
            # Strictly interior: a point riding an edge would 3-fan into a
            # sliver pair; the lattice loses nothing by skipping it.
            if l0 >= 0.05 and l1 >= 0.05 and l2 >= 0.05:
                hit = ti
                break
        if hit is None:
            continue
        pi = pid(px, py)
        _addv(pi)
        a, b, c = T[hit]
        alive[hit] = False
        for nt in ((a, b, pi), (b, c, pi), (c, a, pi)):
            T.append(nt)
            alive.append(True)
            _addt(len(T) - 1)
    return _flip2d(pts, [T[ti] for ti in range(len(T)) if alive[ti]])


def _flip2d(pts, tris, rounds=4):
    """Ratio-improving diagonal flips on a 2D triangulation.

    Same shape rule as corridor_clean._flip_pass, but purely 2D (this runs
    before the surfaces are lifted).  Boundary and constraint edges have one
    owner and are structurally unflippable, so the outline, the holes and
    the door base lines cannot be disturbed.
    """
    tris = [tuple(t) for t in tris]

    def _ratio(a, b, c):
        pa, pb, pc = pts[a], pts[b], pts[c]
        e = [math.hypot(pa[0] - pb[0], pa[1] - pb[1]),
             math.hypot(pb[0] - pc[0], pb[1] - pc[1]),
             math.hypot(pc[0] - pa[0], pc[1] - pa[1])]
        lo = min(e)
        return (max(e) / lo) if lo > 1e-9 else 1e9

    def _area2(a, b, c):
        pa, pb, pc = pts[a], pts[b], pts[c]
        return ((pb[0] - pa[0]) * (pc[1] - pa[1]) -
                (pb[1] - pa[1]) * (pc[0] - pa[0]))

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
            # Never flip onto a diagonal that already exists as an edge (the
            # shared vertex space spans several cut pieces, so an edge can
            # recur): a 3-owner edge is non-manifold and gets torn out later.
            ckey = (c, d) if c < d else (d, c)
            if ckey in edge_tris or ckey in new_edges:
                continue
            worst_old = max(_ratio(*t1), _ratio(*t2))
            worst_new = max(_ratio(c, d, a), _ratio(c, d, b))
            if worst_new >= worst_old - 1e-9:
                continue
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
        if not changed:
            break
    return tris


STEEP_REFINE_EDGE = 64.0


def _refine_steep(verts, tris, steep_pts, protected=()):
    """Bisect triangles carrying steep centreline seeds until they are fine.

    Longest-edge bisection with the neighbour split at the same midpoint, so
    every split preserves a conforming (T-junction-free) triangulation.  A
    triangle is refined while a steep seed lies inside it (or within a seed
    radius of it) and its longest edge exceeds STEEP_REFINE_EDGE — the scale
    the old point-set pipeline seeded stairs at, fine enough that a
    ~0.4-slope ramp triangle spans well under a storey step.

    protected: edge keys (lo, hi) that must never be split — the door
    triangles' ring edges, so a doorway stays one big triangle.
    """
    verts = [tuple(v) for v in verts]
    tris = [tuple(t) for t in tris]
    if not steep_pts or not tris:
        return verts, tris
    max_e2 = STEEP_REFINE_EDGE * STEEP_REFINE_EDGE

    # Seeds bucketed on a grid so a triangle only tests the seeds its bbox
    # can contain — the all-pairs form was O(tris x seeds) per round and
    # timed out on seed-heavy cells.
    _cell = STEEP_REFINE_EDGE * 2.0
    seed_grid = {}
    for (px, py) in steep_pts:
        seed_grid.setdefault((int(px // _cell), int(py // _cell)),
                             []).append((px, py))

    def _longest(t):
        best = None
        for k in range(3):
            a, b = t[k], t[(k + 1) % 3]
            pa, pb = verts[a], verts[b]
            d2 = (pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2
            if best is None or d2 > best[0]:
                best = (d2, a, b)
        return best

    def _hit(t):
        ax, ay = verts[t[0]]
        bx, by = verts[t[1]]
        cx, cy = verts[t[2]]
        d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(d) < 1e-9:
            return False
        gx0 = int(min(ax, bx, cx) // _cell)
        gx1 = int(max(ax, bx, cx) // _cell)
        gy0 = int(min(ay, by, cy) // _cell)
        gy1 = int(max(ay, by, cy) // _cell)
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                for (px, py) in seed_grid.get((gx, gy), ()):
                    l0 = ((by - cy) * (px - cx)
                          + (cx - bx) * (py - cy)) / d
                    l1 = ((cy - ay) * (px - cx)
                          + (ax - cx) * (py - cy)) / d
                    l2 = 1.0 - l0 - l1
                    if l0 >= -0.02 and l1 >= -0.02 and l2 >= -0.02:
                        return True
        return False

    for _round in range(6):
        split_edges = {}
        for ti, t in enumerate(tris):
            if not _hit(t):
                continue
            d2, a, b = _longest(t)
            if d2 <= max_e2:
                continue
            key = (a, b) if a < b else (b, a)
            if key in protected:
                continue
            if key not in split_edges:
                pa, pb = verts[a], verts[b]
                mid = (0.5 * (pa[0] + pb[0]), 0.5 * (pa[1] + pb[1]))
                split_edges[key] = len(verts)
                verts.append(mid)
        if not split_edges:
            break
        out = []
        for t in tris:
            # Split this triangle at every one of its edges that was marked,
            # fanning from the ring of corners + midpoints — handles one,
            # two or three marked edges in a single conforming pass.
            ring = []
            for k in range(3):
                a, b = t[k], t[(k + 1) % 3]
                ring.append(a)
                m = split_edges.get((a, b) if a < b else (b, a))
                if m is not None:
                    ring.append(m)
            if len(ring) == 3:
                out.append(t)
                continue
            for i in range(1, len(ring) - 1):
                tri = (ring[0], ring[i], ring[i + 1])
                if len(set(tri)) == 3:
                    out.append(tri)
        tris = out
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

    # VECTORISED.  This runs once per storey per cell; the scalar version
    # buffered 2,317 LineStrings one at a time and cost 11s on a single cell.
    B = np.asarray(blocking, dtype=float).reshape(-1, 3, 3)
    if not len(B):
        return None
    zmax = B[:, :, 2].max(axis=1)
    zmin = B[:, :, 2].min(axis=1)
    keep = (zmax >= z_lo) & (zmin <= z_hi)
    B = B[keep]
    if not len(B):
        return None
    # Footprint of a wall triangle: its longest projected edge.
    d2 = np.empty((len(B), 3))
    for k in range(3):
        dx = B[:, (k + 1) % 3, 0] - B[:, k, 0]
        dy = B[:, (k + 1) % 3, 1] - B[:, k, 1]
        d2[:, k] = dx * dx + dy * dy
    kbest = d2.argmax(axis=1)
    rows = np.arange(len(B))
    long2 = d2[rows, kbest]
    ok = long2 >= 1.0
    if not ok.any():
        return None
    p = B[rows[ok], kbest[ok]]
    q = B[rows[ok], (kbest[ok] + 1) % 3]
    segs = [LineString([(float(a[0]), float(a[1])),
                        (float(b[0]), float(b[1]))])
            for a, b in zip(p, q)]
    if not segs:
        return None
    try:
        # Round joins are needed (MITRE spikes out at sharp wall corners and
        # shredded the mesh: ImperialDungeon01 went from 0 uncovered samples
        # to 50), but they need not be SMOOTH — this is a 1u slit.  quad_segs=1
        # keeps the join round-ish at a fraction of the cost.
        return unary_union(segs).buffer(WALL_CUT_WIDTH, cap_style=2,
                                        quad_segs=1)
    except Exception:
        return None


# Door triangles reserved out of the mesh, added back after ALL cleanup.
PENDING_DOOR_TRIS = []


# A sheet may only claim a door when it holds at least this much of the door
# triangle.  The base line sits on the union boundary, so every sheet meeting
# the threshold passes the on-outline test; without this the first one iterated
# won and cut a wedge that was almost entirely outside itself.
DOOR_CLAIM_MIN_FRAC = 0.5


def build_union_mesh(strips, extra_strips=None, door_edges=None,
                     cell_bounds=None, wall_cut=None, probe_only=False):
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

    # Bound the ribbon-polygon memo to one build: a worker converts thousands of
    # cells in a row and the cache pins a Polygon (and the strip) per entry.
    _ribbon_cache_clear()

    if not strips:
        return [], []

    # Door footprints participate as ordinary geometry: they contribute their
    # polygon to the union AND their (flat) height to the level lookup, so a
    # vertex standing on door-only ground still knows how high it is.
    strips = list(strips) + list(extra_strips or ())

    verts = []
    tris = []
    # Which _triangulate emission each vertex came from (parallel to `verts`).
    # The weld may only fuse vertices from DIFFERENT emissions: within one
    # part the CDT already connects everything, so a same-part weld can only
    # move a vertex sideways — measured at Pinarus's stair bottom, where the
    # steep refinement put a stair-copy vertex and a floor vertex 15.8u apart
    # in 3D and the weld dragged one onto the other, sweeping a triangle edge
    # across a neighbour it shared no vertex with (overlapping triangles).
    vert_src = []

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
    _door_claimed = set()        # door_edges indices already reserved
    PENDING_DOOR_TRIS.clear()
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
                #
                # CLIPPED to the junction disc, never the whole strip.  The
                # strip joins the owning sheet's LEVEL LOOKUP, and levels are
                # answered wherever a strip covers a point — so handing over
                # the full stair strip leaked its heights across everything it
                # passes under.  Measured on Pinarus: the upper-floor sheet
                # (whose polygon spans the whole house) received the stair
                # strip for a 64u junction at its top node, its corners above
                # the stair BOTTOM then answered levels [-199, 69], and the
                # sheet emitted a phantom duplicate of the ground floor there
                # — stacked, overlapping triangles at the foot of the stairs.
                junction_strips.setdefault(own, []).append(
                    _clip_strip_near(s, nx, ny, r, piece))
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
        # NOTE: pathgrid nodes are no longer appended as forced seeds.  Under
        # the point-set sampler the True flag forced a vertex at each node so
        # cross-sheet welds could fuse stair tops; the CDT takes vertices only
        # from the polygon rings, so a node seed cannot become a vertex — the
        # only thing the flag did was mark every node junction "steep" and
        # trigger 64u refinement around all of them, exploding a large cell
        # to ~50k triangles that emission then paid for (~85s of a 118s cell)
        # and decimation collapsed right back down.  Cross-sheet junctions
        # are joined by _merge_at_pathgrid_nodes and _stitch_shared_nodes.
        for part in gparts:
            if not isinstance(part, Polygon) or part.area < 1.0:
                continue
            # A door base line belongs to this part when it lies inside it OR
            # ON ITS OUTLINE — the threshold edge of a door quad IS part of the
            # union boundary, so a strict interior test rejected it and the
            # constraint never reached the triangulation at all.  That is what
            # left the CharacterGen assassins' 115u cell door as a 571-unit
            # scrap (every vanilla door triangle is >= 992): unprotected, the
            # boundary densify chopped its base line into 26.8u + 21.6u pieces.
            # Each door belongs to exactly ONE part.  The tolerant on-outline
            # test above can match a door line in several sheets that meet at
            # the threshold; reserving it more than once produces duplicate,
            # overlapping door triangles that then collide in the weld.
            fixed = []
            for ei, e in enumerate(door_edges):
                if ei in _door_claimed:
                    continue
                if not _door_edge_on_part(e, part):
                    continue
                # ...AND THE PART MUST ACTUALLY HOLD THE DOOR TRIANGLE.  The
                # base line lies on the union boundary, so EVERY sheet that
                # meets the threshold passes the on-outline test above and the
                # FIRST one iterated won the claim — even when the wedge sits
                # almost entirely in a different sheet.  The reservation then
                # cut a wedge that was 98.6% outside its own part, the cut
                # collapsed to nothing, and the door lost its guaranteed
                # triangle altogether: measured on ImperialDungeon01's 99.5u
                # prison gate (0001FC1E), claimed by the 3.49M sheet covering
                # 1.4% of the wedge while the sheet covering 98.6% was never
                # offered it.  What shipped instead was the fan of 8-360u^2
                # needles through the doorway.
                if len(e) > 2 and e[2] is not None:
                    try:
                        from shapely.geometry import Polygon as _DCP
                        wedge = _DCP([e[0], e[1], e[2]])
                        if (wedge.is_valid and wedge.area > 1.0
                                and part.intersection(wedge).area
                                < DOOR_CLAIM_MIN_FRAC * wedge.area):
                            continue        # another sheet owns this doorway
                    except Exception:
                        pass
                # STOREY GATE.  Parts are 2D, so where two floors stack in
                # plan BOTH pass the containment test and iteration order
                # decided the claim: Arvena's upstairs bedroom door was
                # claimed by the sheet that only covers that spot DOWNSTAIRS,
                # its wedge was cut from ground that does not span the
                # doorway at that height, and the door triangle came back
                # unattachable and was withdrawn.  The door knows its storey
                # (the corridor its quad bridges to); require this sheet to
                # actually have a surface at that height under the door.
                if len(e) > 3 and e[3] is not None:
                    mx = 0.5 * (e[0][0] + e[1][0])
                    my = 0.5 * (e[0][1] + e[1][1])
                    lv = _levels_at(group, mx, my)
                    if lv and not any(abs(q - e[3]) <= STOREY_GAP_Z
                                      for q in lv):
                        continue        # another storey's part
                _door_claimed.add(ei)
                fixed.append(e)
            _pending_mark = len(PENDING_DOOR_TRIS)
            v2, t2 = _triangulate(part, params.TRI_TARGET_EDGE,
                                  fixed_edges=fixed, steep_seeds=gseeds)
            if not t2:
                continue
            # Levels come from THIS storey's ribbons only, so a corner cannot
            # pick up the other floor's height.
            levels = _levels_batch(group, v2)
            # THE DOOR APEX HAS NO RIBBON UNDER IT.  Its triangle is reserved
            # out of the union (a hole), so no corridor covers that point and
            # _levels_at returns nothing for it.  _emit_surfaces then drops any
            # triangle whose corners do not all share a surface, which silently
            # deleted 4 of every 5 reserved door triangles — the protection
            # passes downstream never saw them because they never existed.
            #
            # The apex stands on the same ground as its own door line, so it
            # inherits the levels of the two base endpoints.
            _apply_door_apex_levels(v2, levels, fixed)
            v3, t3 = _emit_surfaces(v2, t2, levels)
            # Tag the door triangles THIS SHEET reserved with the height of the
            # ground they sit on.  attach_door_triangles must then snap their
            # corners to vertices on the SAME STOREY: matching in XY alone let
            # a corner grab a vertex on the floor above or below, fanning huge
            # vertical triangles between all three storeys of
            # ChorrolFightersGuild.
            if v3 and len(PENDING_DOOR_TRIS) > _pending_mark:
                for _n in range(_pending_mark, len(PENDING_DOOR_TRIS)):
                    _e = PENDING_DOOR_TRIS[_n]
                    if len(_e) != 3:
                        continue
                    _ax, _ay = _e[2]
                    _bz = min(v3, key=lambda q: (q[0] - _ax) ** 2
                              + (q[1] - _ay) ** 2)[2]
                    PENDING_DOOR_TRIS[_n] = _e + (float(_bz),)
            base = len(verts)
            verts.extend(v3)
            vert_src.extend([base] * len(v3))
            tris.extend((a + base, b + base, c + base) for (a, b, c) in t3)

    # Each sheet was triangulated on its own, so where two sheets meet on the
    # SAME surface their boundary vertices are coincident but carry different
    # indices — they share no edge, and the engine cannot walk between them.
    # Weld those together (a 3D weld, so two storeys stacked in plan are never
    # fused: they are hundreds of units apart in Z).
    verts, tris = _weld_sheets(verts, tris, src=vert_src)
    tris = _split_t_junctions(verts, tris)
    # THE GUARANTEE: corridors that meet at a pathgrid node are joined, EVERY
    # TIME — driven by the PATHGRID rather than by any property of the
    # geometry, so there is no case it can decline to handle.  It now runs
    # BEFORE the stitch: the merge makes each junction a shared POINT (one
    # weld per component), and the stitch is the machinery that turns shared
    # points into shared EDGES (fan-open + bridge, with the overlap guards).
    verts, tris = _merge_at_pathgrid_nodes(verts, tris, node_pts, node_half)
    tris = _destack(verts, tris)
    tris = _stitch_shared_nodes(verts, tris, stitch_nodes)
    # RE-SPLIT T-JUNCTIONS after the last vertex-moving pass.  The merge and
    # the stitch move and fuse vertices, which can land a vertex in the middle
    # of another triangle's border edge — a hanging node the first T-split ran
    # too early to see.  An edge left unshared reads as point-attached, and
    # _drop_point_attached then deletes REAL coverage: measured on
    # ImperialDungeon01, the junction triangle spanning pathgrid nodes
    # 137/138/139 was dropped and the walked line through the prison lost its
    # mesh (a hole an NPC cannot cross).
    tris = _split_t_junctions(verts, tris)
    tris = _drop_walls(verts, tris, strips)
    # ...AND AGAIN AFTER THE WALL CULL, which can itself OPEN a crack.  A
    # plan-degenerate triangle (three corners collinear in plan) reads as a
    # 90-degree wall and is dropped correctly — but where it was the only
    # thing bridging a hanging vertex to the edge it sits on, dropping it
    # UN-SPLITS that T-junction and the two sides of the seam stop sharing an
    # edge.  Measured on ImperialDungeon01's tower staircase: the zero-area
    # triangle (-288.5,183.2)/(-270.8,286.9)/(-279.6,235.0) was the bridge,
    # and losing it left a 105u boundary edge straight across the flight with
    # the pathgrid running through it — the mesh looks continuous in plan but
    # an NPC cannot cross ("a missing sliver that chokes the staircase by
    # half").  Re-zipping restores the shared edges without restoring the
    # wall.
    tris = _split_t_junctions(verts, tris)
    if probe_only:
        # STOP HERE for the door-footprint probe pass.  Everything ABOVE can
        # change which corridor edge is nearest a door -- the wall cull in
        # particular removes edges a door must never bridge across, and
        # dropping it moved a door in ImperialDungeon01.  What remains below
        # only ADDS ground (notch fill) or removes triangles that hang off a
        # point, neither of which is a bridge candidate the door search would
        # have picked: `cands` is filtered to edges within DOOR_BRIDGE_RADIUS
        # on the door's own storey, reached without crossing a wall.
        # The probe's mesh is discarded either way -- the second pass rebuilds
        # the union with the door quads unioned in.
        return verts, tris
    tris = _fill_boundary_notches(verts, tris, strips)
    tris = _drop_point_attached(tris)
    return verts, tris


def _destack(verts, tris):
    """Remove SAME-SURFACE stacked duplicates: two triangles covering the
    same plan area at walkable-step heights.

    The claim (_same_surface_region) surrenders most duplicated ground, but
    its per-ribbon-pair sampling can miss a sliver where two sheets disagree
    by a hair less than a storey — the leftovers are triangles lying ON each
    other (measured on Moranda02: 20 pairs at dz 0-33).  Overlapping
    triangles are never legal; the ground under a stacked pair stays covered
    by the surviving partner, so the smaller of the two is dropped whenever
    its neighbours stay mutually reachable without it.
    """
    if len(tris) < 2:
        return tris
    from shapely.geometry import Polygon as _DP
    from shapely import STRtree as _DT
    tris = [tuple(t) for t in tris]
    polys = [None] * len(tris)
    geoms = []
    gmap = []
    for ti, t in enumerate(tris):
        pa, pb, pc = verts[t[0]], verts[t[1]], verts[t[2]]
        try:
            pg = _DP([(pa[0], pa[1]), (pb[0], pb[1]), (pc[0], pc[1])])
        except Exception:
            continue
        if not pg.is_valid or pg.area < 4.0:
            continue
        polys[ti] = pg
        gmap.append(ti)
        geoms.append(pg)
    if not geoms:
        return tris
    tree = _DT(geoms)

    def _z_at(t, x, y):
        (ax, ay, az) = verts[t[0]]
        (bx, by, bz) = verts[t[1]]
        (cx, cy, cz) = verts[t[2]]
        d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(d) < 1e-9:
            return (az + bz + cz) / 3.0
        l0 = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / d
        l1 = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / d
        return l0 * az + l1 * bz + (1.0 - l0 - l1) * cz

    pairs = []
    for gi, ti in enumerate(gmap):
        cp = polys[ti]
        for gj in tree.query(cp).tolist():
            tj = gmap[gj]
            if tj <= ti:
                continue
            if set(tris[ti]) & set(tris[tj]):
                continue                    # adjacency, not stacking
            try:
                inter = cp.intersection(polys[tj])
                area = inter.area
            except Exception:
                continue
            if area <= 4.0:
                continue
            cx, cy = inter.centroid.x, inter.centroid.y
            # Same surface within 40u at the overlap itself — the tightest
            # gap two REAL storeys ever have is STOREY_GAP_Z (120), so
            # anything this close is a duplicate, not a floor above.
            if abs(_z_at(tris[ti], cx, cy)
                   - _z_at(tris[tj], cx, cy)) > 40.0:
                continue                    # genuine storeys stacked in plan
            pairs.append((-area, ti, tj))
    if not pairs:
        return tris

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
        fuel = 128
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

    pairs.sort()
    for (_na, ti, tj) in pairs:
        if not alive[ti] or not alive[tj]:
            continue
        small, big = ((ti, tj) if polys[ti].area <= polys[tj].area
                      else (tj, ti))
        for victim in (small, big):
            if _removable(victim):
                alive[victim] = False
                break
    return [t for ti, t in enumerate(tris) if alive[ti]]


def _drop_walls(verts, tris, strips=None):
    """Remove near-vertical triangles (steeper than WALL_SLOPE_COS) whose
    removal cannot disconnect anything.

    The per-surface emission can lay a wall-like triangle wherever a corner's
    covering ribbons genuinely disagree in height over a short plan distance
    (the landing overhanging the stairwell it grew across).  An actor cannot
    stand on such a triangle, so it is not walkable ground — but on jagged
    cave floors a steep triangle is occasionally the ONLY link between two
    ledges, and removing those tears the mesh (measured: emission-time slope
    filtering split ImperialDungeon04 in two and BarrenCave into 14).  So a
    wall is dropped only when its neighbours remain mutually reachable
    without it, checked by a bounded BFS over shared edges.

    PATHGRID GUARD: a triangle carrying a strip CENTRELINE sample is never
    dropped, however steep — the pathgrid asserts an actor walks there, and
    Oblivion's mountain trails genuinely exceed the wall threshold.  Without
    this, a cell whose whole mesh is one steep hillside ribbon was leaf-eaten
    to NOTHING (the connectivity BFS never fires on a chain's tips), and 113
    exterior cells shipped without a navmesh.
    """
    if not tris:
        return tris
    pg_grid = {}
    PG_CELL = 128.0
    if strips:
        for s in strips:
            ax, ay, az = s['a']
            bx, by, bz = s['b']
            run = math.hypot(bx - ax, by - ay)
            n = max(1, int(run // 32.0))
            for k in range(n + 1):
                t = k / n
                px, py = ax + (bx - ax) * t, ay + (by - ay) * t
                pz = _height_on(s, px, py)
                pg_grid.setdefault((int(px // PG_CELL), int(py // PG_CELL)),
                                   []).append((px, py, pz))

    def _carries_pathgrid(t):
        (ax, ay, az) = verts[t[0]]
        (bx, by, bz) = verts[t[1]]
        (cx, cy, cz) = verts[t[2]]
        d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(d) < 1e-9:
            return False
        xs = (ax, bx, cx)
        ys = (ay, by, cy)
        for gx in range(int(min(xs) // PG_CELL), int(max(xs) // PG_CELL) + 1):
            for gy in range(int(min(ys) // PG_CELL),
                            int(max(ys) // PG_CELL) + 1):
                for (px, py, pz) in pg_grid.get((gx, gy), ()):
                    l0 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / d
                    l1 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / d
                    l2 = 1.0 - l0 - l1
                    if l0 < -0.02 or l1 < -0.02 or l2 < -0.02:
                        continue
                    if abs(l0 * az + l1 * bz + l2 * cz - pz) <= 80.0:
                        return True
        return False

    steep = []
    for ti, t in enumerate(tris):
        pa, pb, pc = verts[t[0]], verts[t[1]], verts[t[2]]
        area2 = abs((pb[0] - pa[0]) * (pc[1] - pa[1]) -
                    (pb[1] - pa[1]) * (pc[0] - pa[0]))
        ux, uy, uz = (pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2])
        wx, wy, wz = (pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2])
        nx3 = uy * wz - uz * wy
        ny3 = uz * wx - ux * wz
        nz3 = ux * wy - uy * wx
        area3 = math.sqrt(nx3 * nx3 + ny3 * ny3 + nz3 * nz3)
        if area3 > 1e-9 and area2 / area3 < WALL_SLOPE_COS:
            if pg_grid and _carries_pathgrid(t):
                continue
            steep.append((area2 / area3, ti))
    if not steep:
        return tris
    edge_tris = {}
    for ti, t in enumerate(tris):
        for k in range(3):
            a, b = t[k], t[(k + 1) % 3]
            edge_tris.setdefault((a, b) if a < b else (b, a), []).append(ti)
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

    steep.sort()                            # steepest (smallest cos) first
    for (_cos, ti) in steep:
        nbrs = _neighbours(ti)
        if len(nbrs) > 1:
            # All neighbours must reach each other without crossing ti.
            target = set(nbrs[1:])
            seen_t = {nbrs[0], ti}
            queue = [nbrs[0]]
            budget = 128
            while queue and target and budget:
                budget -= 1
                cur = queue.pop()
                for tj in _neighbours(cur):
                    if tj in seen_t:
                        continue
                    seen_t.add(tj)
                    target.discard(tj)
                    queue.append(tj)
            if target:
                continue                    # ti is a bridge: keep the wall
        alive[ti] = False
    return [t for ti, t in enumerate(tris) if alive[ti]]


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

    # comp/vcomp describe the CURRENT triangle soup, so they only go stale when
    # a node actually welds something -- and most nodes weld nothing (they hit
    # the `continue`s below).  Rebuilding them per node regardless made this the
    # single hottest function in the whole navmesh build: full-mesh union-find
    # once per pathgrid node is O(nodes x tris), 831 x ~4000 on Moranda, ~60% of
    # a large cell's total time.  Computing them lazily and invalidating only on
    # a real weld is the same answer for a fraction of the work.
    #
    # `near` additionally needs a SPATIAL index: scanning every vertex per node
    # is the other half of the quadratic.  Vertices are bucketed once into a
    # grid whose cell equals the largest search radius, so a query touches only
    # the 3x3 neighbourhood around the node.
    cache = {}

    def _state():
        """(comp-per-tri, vertex -> set-of-comps, xy-bucket index), memoised."""
        if 'comp' not in cache:
            comp = _tri_components(tris)
            vcomp = {}
            for ti, t in enumerate(tris):
                for i in t:
                    vcomp.setdefault(resolve(i), set()).add(comp[ti])
            cell = max(GRID_R, 1.0)
            buckets = {}
            for i in vcomp:
                v = verts[i]
                buckets.setdefault((int(v[0] // cell), int(v[1] // cell)),
                                   []).append(i)
            cache['comp'] = (comp, vcomp, buckets, cell)
        return cache['comp']

    # The disc reaches one ribbon width past the node's own half-width: two
    # sheets meeting at a junction can each stop short of the node (a claim
    # seam leaves their boundaries 20-35u apart), and a disc that only just
    # covers the node's own corridor missed the neighbour sheet's nearest
    # vertex by a few units (BarrenCave's tunnel at node 337: 72u away
    # against a 64u disc — the junction was never seen at all).
    GRID_R = max([float(node_half.get(ni, 0.0)) for ni in node_pts]
                 + [params.RIBBON_HALF_WIDTH]) + params.RIBBON_HALF_WIDTH

    for ni, (nx, ny) in sorted(node_pts.items()):
        r = (max(float(node_half.get(ni, 0.0)), params.RIBBON_HALF_WIDTH)
             + params.RIBBON_HALF_WIDTH)
        comp, vcomp, buckets, cell = _state()
        # Candidates from the 3x3 bucket neighbourhood, then the exact radius
        # test.  Sorted so the banding below stays deterministic regardless of
        # bucket iteration order (byte-reproducibility contract).
        gx, gy = int(nx // cell), int(ny // cell)
        near = []
        for ddx in (-1, 0, 1):
            for ddy in (-1, 0, 1):
                for i in buckets.get((gx + ddx, gy + ddy), ()):
                    if math.hypot(verts[i][0] - nx, verts[i][1] - ny) <= r:
                        near.append(i)
        near.sort()
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
        welded_any = False
        for band in bands:
            comps_of = {}
            for i in band:
                for cmp in vcomp.get(i, ()):
                    comps_of.setdefault(cmp, []).append(i)
            if len(band) < 2 or len(comps_of) < 2:
                continue                # already one surface here
            keep = min(band, key=lambda i: (
                math.hypot(verts[i][0] - nx, verts[i][1] - ny), i))
            # Weld ONE vertex per foreign component — the closest — onto the
            # keeper.  Welding the WHOLE band (the old form) deleted every
            # triangle that fit inside the node disc: with the interior
            # lattice the mesh near a junction is exactly disc-sized
            # triangles, and a wide grown node radius (160u) swallowed real
            # ground whole (measured on ChorrolFightersGuild: two upstairs
            # floor triangles vanished and a door quad corner was dragged
            # 100u, leaving the quad an island).  One weld per component is
            # the minimal act that makes the junction a shared point; the
            # stitch that runs AFTER this pass turns shared points into
            # shared edges.
            keep_comps = vcomp.get(keep, set())
            anchor = [i for i in band
                      if vcomp.get(i, set()) & keep_comps]
            for cmp in sorted(comps_of):
                if cmp in keep_comps:
                    continue
                # Weld the CLOSEST cross pair (foreign vertex onto the
                # nearest anchor-component vertex), never everything onto
                # the node-nearest vertex: the pair across a claim seam is
                # 20-35u apart while the node-nearest vertex can be a full
                # disc away — welding onto IT dragged geometry ~100u.
                best = None
                for i in comps_of[cmp]:
                    for j in anchor:
                        d2 = ((verts[i][0] - verts[j][0]) ** 2
                              + (verts[i][1] - verts[j][1]) ** 2
                              + (verts[i][2] - verts[j][2]) ** 2)
                        if best is None or (d2, i, j) < best:
                            best = (d2, i, j)
                if best is None:
                    continue
                _d2, i, j = best
                # Bounded drag: a junction weld spans a claim seam (20-35u
                # measured), never half the disc.  An uncapped weld swept
                # edges across unrelated mesh exactly like the raw 16u weld
                # once did, and the overlaps came back (Moranda02: 24 pairs).
                if _d2 > params.RIBBON_HALF_WIDTH ** 2:
                    continue
                if i != j:
                    remap[i] = j
                    welded_any = True
        # Only a real weld changes the soup.  Skipping the rewrite (and the
        # cache drop) when nothing welded is what makes the memo above pay off:
        # the vertex roots, the components and the buckets are all still exactly
        # what _state() last computed.
        if welded_any:
            tris = [t for t in ((resolve(a), resolve(b), resolve(c))
                                for (a, b, c) in tris) if len(set(t)) == 3]
            cache.clear()

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

    # Run to CONVERGENCE, not a fixed small round count: a junction whose
    # border edges are all too long to bridge needs a split round per halving
    # before its bridge round (the Sanctum pit gate took split+split+bridge
    # on each side), and a busy cell spends early rounds on other junctions —
    # 3 and even 8 rounds left it disconnected.  Every round either bridges,
    # opens a fan, or halves an over-long border edge, all of which are
    # finite, so the loop terminates; the cap is a runaway backstop only.
    #
    # allow_overlap: bridges normally must land on EMPTY ground (the overlap
    # guard below).  When a full round makes no progress at all, one retry
    # round runs with the guard relaxed — a junction whose only possible
    # bridge slightly overlaps existing mesh still gets connected, because a
    # disconnected navmesh is strictly worse than a sliver of double cover
    # (ChorrolFightersGuild's stairwell junction needs exactly this).
    allow_overlap = False
    for _round in range(30):
        # A busy cell can spend every early round on OTHER junctions and
        # exhaust the loop without the stall-retry ever reaching a stubborn
        # one — the last rounds therefore run relaxed unconditionally.
        if _round >= 24:
            allow_overlap = True
        # FUSE COINCIDENT VERTICES first.  The passes before (and inside)
        # this loop mint midpoints independently on both sides of a seam, so
        # two components can touch at IDENTICAL positions under different
        # indices — invisible to the index-keyed junction scan below, and the
        # main weld has already run (measured: Chorrol and BarrenCave each
        # ended with a component pair 0.00u apart).  Same position + same
        # height is the same walkable point; fusing moves nothing and can
        # create nothing, it only lets the junction scan see the contact.
        pos = {}
        fuse = {}
        for i in sorted({i for t in tris for i in t}):
            key = (round(verts[i][0], 1), round(verts[i][1], 1),
                   round(verts[i][2], 1))
            j = pos.get(key)
            if j is None:
                pos[key] = i
            else:
                fuse[i] = j
        if fuse:
            tris = [t for t in (tuple(fuse.get(k, k) for k in t)
                                for t in tris) if len(set(t)) == 3]

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
        vtris = {}
        for ti, t in enumerate(tris):
            for i in t:
                vcomp.setdefault(i, set()).add(comp[ti])
                vtris.setdefault(i, []).append(ti)
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
                    cand_t = [ti for ti in vtris.get(i, ())
                              if comp[ti] == c and ti not in replaced]
                    # Try candidates LARGEST FIRST until one passes the guards
                    # below.  Trying only the largest gave up whenever it
                    # happened to fail a guard while a perfectly splittable
                    # fan triangle sat beside it (the Sanctum pit-gate seam:
                    # the 15,676u² candidate's opposite edge spans dz 36 — 2u
                    # over MAX_CLIMB — while the 2,936u² one is dead flat).
                    cand_t.sort(key=lambda x: -_tri_area(verts, tris[x]))
                    for ti in cand_t:
                        t = tris[ti]
                        k = t.index(i)
                        p, q = t[(k + 1) % 3], t[(k + 2) % 3]
                        okey = (p, q) if p < q else (q, p)
                        nb = [tj for tj, tt in enumerate(tris)
                              if tj != ti and tj not in replaced
                              and p in tt and q in tt]
                        if counts.get(okey, 0) > 1 and not nb:
                            continue      # cannot keep the opposite edge manifold
                        # The split must not manufacture a near-VERTICAL or
                        # degenerate triangle: the halves inherit the parent's
                        # corners plus the midpoint of (p,q), so a parent that
                        # already spans a big drop (a stairwell-edge triangle)
                        # would hand both halves that drop and the result reads
                        # as a wall, not floor.  Splitting those is what added
                        # OPPOSITE_NORMALS/DOWNFACING triangles to
                        # ImperialSewers03 and Bruma.  SLOPE-based, not a flat
                        # step: on a sloped cave floor a long edge legitimately
                        # spans several steps of height — what makes a wall is
                        # height WITHOUT plan run (a flat cap froze every
                        # junction on BarrenCave's ramped tunnels).
                        if (abs(verts[p][2] - verts[q][2])
                                > max(params.MAX_CLIMB,
                                      0.7 * math.hypot(
                                          verts[p][0] - verts[q][0],
                                          verts[p][1] - verts[q][1]))):
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
                                replaced[tj] = [(opp[0], p, mid),
                                                (opp[0], mid, q)]
                        key = (i, mid) if i < mid else (mid, i)
                        owner[key] = c
                        counts[key] = 1
                        by_comp.setdefault(c, []).append((i, key))
                        counts.pop(okey, None)
                        break
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
                        # actor would climb rather than walk.  SLOPE-based: a
                        # bridge on ramped ground may climb with its plan run
                        # (~35 degrees); only height without run is a wall.
                        zs = [verts[x][2] for x in tri]
                        plan_span = max(math.hypot(
                            verts[tri[k]][0] - verts[tri[(k + 1) % 3]][0],
                            verts[tri[k]][1] - verts[tri[(k + 1) % 3]][1])
                            for k in range(3))
                        if (max(zs) - min(zs)
                                > max(params.MAX_CLIMB, 0.7 * plan_span)):
                            continue
                        # MANIFOLD GUARD: every edge the bridge introduces must
                        # end with at most TWO owners, or adjacency links none of
                        # them and the bridge disconnects rather than joins.
                        if any(counts.get(e, 0) + extra.get(e, 0) >= 2
                               for e in _tri_edges(tri)):
                            continue
                        # OVERLAP GUARD: the bridge must land on empty ground.
                        # A wide, guard-passing bridge can lie across mesh it
                        # shares no vertex with — at Pinarus's stair top a
                        # 126u flat bridge at the landing height overlapped
                        # the flight's emerging top triangles (same surface,
                        # dz 19).  A stalled-retry round (allow_overlap)
                        # tolerates a SLIVER of overlap — the last resort for
                        # a junction nothing else could join — but a bridge
                        # lying broadly across other mesh is never accepted.
                        if _tri_overlaps_mesh(verts, tris, replaced, tri,
                                              eps=(250.0 if allow_overlap
                                                   else 2.0)):
                            continue
                        for e in _tri_edges(tri):
                            extra[e] = extra.get(e, 0) + 1
                        added.append(tri)
                        made = True
                        break
                if made:
                    continue
                # No bridge fit: every candidate spanned too far.  Decimation
                # merges boundary vertices into edges well past the 160u
                # bridge cap, so both sides offer only LONG border edges at
                # the junction and every bridge triangle reaches a far
                # endpoint (the Sanctum pit gate: comps touching at 0.00u,
                # shortest edges 104/173u, all bridges rejected).  Split the
                # shortest such edge of each side at its midpoint — a border
                # edge has exactly one owner, so the split is manifold by
                # construction — and the NEXT round bridges the halves.
                for c in (base_c, other_c):
                    best = None
                    for (i, key) in by_comp.get(c, ()):
                        far = key[0] if key[1] == i else key[1]
                        ln = math.hypot(verts[far][0] - verts[i][0],
                                        verts[far][1] - verts[i][1])
                        if ln > 80.0 and (best is None or ln < best[0]):
                            best = (ln, i, key)
                    if best is None:
                        continue
                    _ln, i, key = best
                    p, q = key
                    owner_t = None
                    for ti in vtris.get(p, ()):
                        if ti not in replaced and q in tris[ti]:
                            owner_t = ti
                            break
                    if owner_t is None:
                        continue
                    t = tris[owner_t]
                    for k in range(3):
                        if {t[k], t[(k + 1) % 3]} == {p, q}:
                            cc = t[(k + 2) % 3]
                            m = len(verts)
                            verts.append([
                                0.5 * (verts[p][0] + verts[q][0]),
                                0.5 * (verts[p][1] + verts[q][1]),
                                0.5 * (verts[p][2] + verts[q][2])])
                            replaced[owner_t] = [(t[k], m, cc),
                                                 (m, t[(k + 1) % 3], cc)]
                            counts.pop(key, None)
                            for nk in ((min(t[k], m), max(t[k], m)),
                                       (min(m, t[(k + 1) % 3]),
                                        max(m, t[(k + 1) % 3]))):
                                counts[nk] = 1
                                owner[nk] = c
                            break
        if not added and not replaced:
            if allow_overlap:
                break                   # even the relaxed round changed nothing
            allow_overlap = True        # stalled: retry once with the
            continue                    # overlap guard relaxed
        allow_overlap = False
        out = []
        for ti, t in enumerate(tris):
            out.extend(replaced.get(ti, [t]))
        tris = [t for t in out + added if len(set(t)) == 3]
    return tris


def _tri_overlaps_mesh(verts, tris, replaced, cand, eps=2.0):
    """Does candidate triangle `cand` overlap existing mesh in plan, at a
    height an actor could stand on both (within one storey step)?

    A correct bridge FILLS A GAP: it may share edges and vertices with both
    components it joins, but its interior must land on empty ground, so any
    real intersection area is a reject — vertex/edge contact contributes no
    area and passes on its own.  Small eps ignores exact-touch slivers from
    floating point.  (An earlier shared-vertex exemption let a 126u flat
    bridge lie across the whole stair-top fan it pivoted on.)
    """
    from shapely.geometry import Polygon as _OvP
    pa, pb, pc = verts[cand[0]], verts[cand[1]], verts[cand[2]]
    try:
        cp = _OvP([(pa[0], pa[1]), (pb[0], pb[1]), (pc[0], pc[1])])
    except Exception:
        return False
    if not cp.is_valid or cp.area <= eps:
        return False
    minx = min(pa[0], pb[0], pc[0]) - 1.0
    maxx = max(pa[0], pb[0], pc[0]) + 1.0
    miny = min(pa[1], pb[1], pc[1]) - 1.0
    maxy = max(pa[1], pb[1], pc[1]) + 1.0
    zlo = min(pa[2], pb[2], pc[2]) - 40.0
    zhi = max(pa[2], pb[2], pc[2]) + 40.0
    for ti, t in enumerate(tris):
        # A triangle in `replaced` (fan-opened this round) still counts: its
        # halves cover exactly the parent's footprint, so a bridge overlapping
        # the parent overlaps the halves.  Skipping them let 400-1600u^2
        # bridge overlaps through (Moranda02).
        qa, qb, qc = verts[t[0]], verts[t[1]], verts[t[2]]
        if (max(qa[0], qb[0], qc[0]) < minx or min(qa[0], qb[0], qc[0]) > maxx
                or max(qa[1], qb[1], qc[1]) < miny
                or min(qa[1], qb[1], qc[1]) > maxy
                or max(qa[2], qb[2], qc[2]) < zlo
                or min(qa[2], qb[2], qc[2]) > zhi):
            continue
        try:
            tp = _OvP([(qa[0], qa[1]), (qb[0], qb[1]), (qc[0], qc[1])])
            if tp.is_valid and cp.intersection(tp).area > eps:
                return True
        except Exception:
            continue
    return False


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


def _weld_sheets(verts, tris, src=None):
    """Fuse vertices that coincide in 3D, so independently-triangulated sheets
    share edges instead of merely touching.

    The split into plan-disjoint sheets (see _split_plan_overlaps) is what keeps
    a triangle from bridging two floors, but it also means two sheets that abut
    on ONE floor are meshed separately and their shared boundary is duplicated.
    Welding on the full 3D position repairs exactly that, and cannot fuse
    different storeys because it compares Z as well.

    src: per-vertex emission id (which _triangulate output the vertex belongs
    to).  A same-emission weld that moves a vertex sideways is PROVISIONAL:
    within one emission the CDT already connects everything, so such a weld is
    only ever needed to fuse a fold's stacked copies — but it can also drag a
    vertex across a neighbour's edge and create overlapping triangles
    (measured at Pinarus's stair bottom: two same-sheet vertices 15.8u apart
    fused, sweeping a 438u^2 overlap over triangles sharing no vertex).  Each
    provisional weld is therefore CHECKED after the mesh is formed and
    REVERTED if any triangle it touches overlaps other mesh — an outright
    XY-distance ban was tried first and broke the welds ImperialDungeon05's
    connectivity depends on (pathgrid=2 became navmesh=3).
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
    # Max XY drift a SAME-emission weld may cause (see the same-emission rule
    # below).  Big enough to fuse a fold's stacked copies (whose plan offsets
    # are float noise), far too small to sweep an edge across a neighbour.
    SAME_PART_WELD_XY = 4.0
    cell = WELD_R

    # A DOOR-RING CORNER (a pending wedge's base corner / apex) must never
    # DRIFT: the full-radius weld dragged the wedge base corner at
    # (2754,8192) onto the outline vertex 16u away at (2754,8176), after
    # which attach_door_triangles found no vertex at the corner, minted its
    # own, and the door triangle hung as an island (the Sanctum pit gate).
    # The rule is directional: any vertex may fuse ONTO a ring corner (the
    # corner's position survives), but a ring corner may fuse into another
    # vertex only when they are genuinely coincident.
    TIGHT_R = 2.0
    ring_grid = {}
    ring_cell = 32.0
    for e in PENDING_DOOR_TRIS:
        for p in e[:3]:
            if isinstance(p, tuple) and len(p) == 2:
                ring_grid.setdefault((int(p[0] // ring_cell),
                                      int(p[1] // ring_cell)), []).append(p)

    def _is_ring_corner(x, y):
        gx, gy = int(x // ring_cell), int(y // ring_cell)
        for ddx in (-1, 0, 1):
            for ddy in (-1, 0, 1):
                for (px, py) in ring_grid.get((gx + ddx, gy + ddy), ()):
                    if (px - x) ** 2 + (py - y) ** 2 <= 1.0:
                        return True
        return False

    grid = {}
    remap = [0] * len(verts)
    out = []
    provisional = {}                # rep index -> [original vertex index, ...]
    for i, v in enumerate(verts):
        gx, gy, gz = (int(v[0] // cell), int(v[1] // cell), int(v[2] // cell))
        isrc = src[i] if src is not None else None
        got = None
        risky = False
        for ddx in (-1, 0, 1):
            for ddy in (-1, 0, 1):
                for ddz in (-1, 0, 1):
                    for (j, p, jsrc) in grid.get((gx + ddx, gy + ddy,
                                                  gz + ddz), ()):
                        d2 = ((p[0] - v[0]) ** 2 + (p[1] - v[1]) ** 2 +
                              (p[2] - v[2]) ** 2)
                        if d2 > WELD_R * WELD_R:
                            continue
                        # A same-emission fuse that moves the vertex sideways
                        # is PROVISIONAL: usually it is the glue joining a
                        # fold's stacked copies (banning it outright split
                        # ImperialDungeon05), but occasionally it drags a
                        # vertex across a neighbour's edge instead.  Mark it
                        # and prove it harmless below.
                        r_flag = False
                        if src is not None and isrc == jsrc:
                            dxy2 = ((p[0] - v[0]) ** 2 + (p[1] - v[1]) ** 2)
                            if dxy2 > SAME_PART_WELD_XY * SAME_PART_WELD_XY:
                                r_flag = True
                        if (d2 > TIGHT_R * TIGHT_R
                                and _is_ring_corner(v[0], v[1])
                                and not _is_ring_corner(p[0], p[1])):
                            continue        # a corner may not drift
                        got = j
                        risky = r_flag
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
            grid.setdefault((gx, gy, gz), []).append((got, out[got], isrc))
        elif risky:
            provisional.setdefault(got, []).append(i)
        remap[i] = got

    def _form():
        w = []
        for (a, b, c) in tris:
            a2, b2, c2 = remap[a], remap[b], remap[c]
            if a2 != b2 and b2 != c2 and a2 != c2:
                w.append((a2, b2, c2))
        return w

    welded = _form()
    if provisional:
        # REVERT any provisional weld whose triangles now overlap other mesh.
        # Only triangles touching a provisional rep are suspects — but a cave
        # cell has hundreds of provisional fuses, so the suspects are tested
        # against an STRtree of the whole soup, not by scanning it per call
        # (the naive scan was 6 of Moranda02's 14 seconds).
        from shapely.geometry import Polygon as _WP
        from shapely import STRtree as _WTree
        from shapely import intersects as _wintersects
        vtris = {}
        for ti, t in enumerate(welded):
            for k in t:
                if k in provisional:
                    vtris.setdefault(k, []).append(ti)
        polys = [None] * len(welded)
        zr = [None] * len(welded)
        geoms = []
        gmap = []
        for ti, t in enumerate(welded):
            pa, pb, pc = out[t[0]], out[t[1]], out[t[2]]
            try:
                pg = _WP([(pa[0], pa[1]), (pb[0], pb[1]), (pc[0], pc[1])])
            except Exception:
                continue
            if not pg.is_valid or pg.area <= 1e-6:
                continue
            polys[ti] = pg
            zr[ti] = (min(pa[2], pb[2], pc[2]), max(pa[2], pb[2], pc[2]))
            gmap.append(ti)
            geoms.append(pg)
        tree = _WTree(geoms) if geoms else None
        checked = {}

        def _suspect_overlaps(ti):
            got = checked.get(ti)
            if got is not None:
                return got
            cp = polys[ti]
            res = False
            if cp is not None and cp.area > 2.0 and tree is not None:
                zlo, zhi = zr[ti]
                # The STRtree query is a BOX filter, so most candidates it
                # returns do not actually touch.  Ask the cheap PREDICATE
                # before paying for a clip: `intersection` builds a whole new
                # polygon just so we can read its area, and it was the single
                # hottest call in the build (23,553 of 41,473 GEOS clips
                # across two reference cells, ~14% of total time).  Triangles
                # that merely share an edge or a corner also pass the
                # predicate, but their intersection has no area — the area
                # test below still decides, so behaviour is unchanged.
                cand = []
                for gi in tree.query(cp).tolist():
                    tj = gmap[gi]
                    if tj == ti:
                        continue
                    if zr[tj][0] > zhi + 40.0 or zr[tj][1] < zlo - 40.0:
                        continue
                    cand.append(tj)
                if cand:
                    try:
                        hits = _wintersects(cp, [polys[tj] for tj in cand])
                    except Exception:
                        hits = None
                    if hits is not None:
                        cand = [tj for tj, h in zip(cand, hits.tolist()) if h]
                for tj in cand:
                    try:
                        if cp.intersection(polys[tj]).area > 2.0:
                            res = True
                            break
                    except Exception:
                        continue
            checked[ti] = res
            return res

        bad = set()
        for rep in sorted(provisional):
            for ti in vtris.get(rep, ()):
                if _suspect_overlaps(ti):
                    bad.add(rep)
                    break
        if bad:
            for rep in sorted(bad):
                for i in provisional[rep]:
                    ni = len(out)
                    out.append([float(verts[i][0]), float(verts[i][1]),
                                float(verts[i][2])])
                    remap[i] = ni
            welded = _form()
    return out, welded


# T-junction split tolerances (module-level so diagnostics can A/B them).
# TSPLIT_TOL: plan radius for INTERIOR hanging vertices.  TSPLIT_CRACK_TOL:
# plan radius when the hit vertex is itself on the boundary (crack zipper).
# TSPLIT_Z_TOL: the z window — 12u seals the measured 3-8u stair-fold cracks;
# MAX_CLIMB grabbed genuine fold vertices and minted overlaps.
TSPLIT_TOL = 2.0
TSPLIT_CRACK_TOL = 6.0
TSPLIT_Z_TOL = 12.0


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
    tol = TSPLIT_TOL
    # Wider PLAN tolerance for hits that are themselves BOUNDARY vertices: a
    # crack/lens hole has boundary on BOTH sides, so a boundary vertex up to
    # 6u off a border edge is the far lip of a crack and splitting seals it
    # (the fan spans the empty lens).  An INTERIOR vertex that close to the
    # boundary is dense healthy mesh — splitting toward it would lay the fan
    # over existing triangles, so those keep the tight 2u tolerance.
    tol_crack = TSPLIT_CRACK_TOL
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
        bverts = {i for e in border for i in e}
        cell = 64.0
        grid = {}
        for i in {i for t in tris for i in t}:
            grid.setdefault((int(verts[i][0] // cell),
                             int(verts[i][1] // cell)), []).append(i)

        splits = {}
        for (a, b) in border:
            pa, pb = verts[a], verts[b]
            dx, dy, dz = (pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2])
            L2 = dx * dx + dy * dy
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
                        # Projection in PLAN, with a separate z window.  The
                        # old spherical 2u test never sealed a stair fold:
                        # the hanging vertex sits ON the edge in plan but
                        # 3-8u off in z (two emissions of a flight disagree
                        # by part of a tread), and the unsealed crack reads
                        # as a zero-area hole NPCs cannot walk across (the
                        # ImperialDungeon01 staircase "holes").  The window
                        # is 12u — the measured cracks plus margin; a full
                        # MAX_CLIMB window grabbed genuine FOLD vertices a
                        # storey-step away and minted 18 overlapping /
                        # vertical fragments in the same cell.
                        s = ((p[0] - pa[0]) * dx + (p[1] - pa[1]) * dy) / L2
                        if not (0.02 < s < 0.98):
                            continue
                        qx = pa[0] + dx * s
                        qy = pa[1] + dy * s
                        qz = pa[2] + dz * s
                        r = tol_crack if i in bverts else tol
                        if ((p[0] - qx) ** 2 + (p[1] - qy) ** 2 <= r * r
                                and abs(p[2] - qz) <= TSPLIT_Z_TOL):
                            # The fan's new edges (a,i)/(i,b) must not give
                            # any edge a 3rd owner — _make_manifold would rip
                            # the extras out and delete real coverage
                            # (measured: 3-sample corridor losses in
                            # ImperialDungeon05 / LeyawiinCastleCountyHall).
                            ka = (a, i) if a < i else (i, a)
                            kb = (i, b) if i < b else (b, i)
                            if (counts.get(ka, 0) <= 1
                                    and counts.get(kb, 0) <= 1):
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

        # Candidate pairs from an R-tree instead of all-pairs.  A ribbon is a
        # short, local quad, so of the n(n-1)/2 pairs only a handful can touch --
        # but testing them all cost 7.4M scalar shapely `intersects` calls on
        # Moranda (~33% of the cell's build time).  STRtree.query does the same
        # box filter in bulk C, and `intersects` is then evaluated only on real
        # candidates.  Pairs are (a<b)-normalised and SORTED so the union-find
        # below sees them in the same order as the old nested loop, which the
        # byte-reproducibility contract requires.
        conflicts = set()
        polys = [p for (_s, p) in items]
        pairs = []
        if n > 1:
            from shapely import STRtree
            tree = STRtree(polys)
            qa, qb = tree.query(polys, predicate='intersects')
            for a, b in zip(qa.tolist(), qb.tolist()):
                if a < b:
                    pairs.append((a, b))
            pairs.sort()
        # NOTE: batching these intersections through shapely's vectorised
        # `intersection`/`area` was measured SLOWER (17.0s -> 17.9s over the
        # 6-cell set).  The cost here is GEOS clipping itself, not Python call
        # overhead, and the bulk form materialises an intersection for every
        # candidate pair whereas this loop discards most of them on the cheap
        # area test.  Left scalar deliberately.
        # Z-RANGE PREFILTER.  `_height_on` interpolates strictly between a
        # strip's own endpoint heights (or its profile's vertices), so a
        # strip's surface is bounded by that range.  Two strips whose ranges
        # are further apart than STOREY_GAP_Z therefore CANNOT disagree by
        # less than it anywhere, and the expensive path -- a GEOS clip plus a
        # 9-sample height probe -- can only return "conflict".  Answer it from
        # the two intervals instead: this is the same decision, reached
        # arithmetically.  Bonded pairs still take the real path, because a
        # bond outranks a plan conflict regardless of the gap.
        zrange = []
        for (s_, _p) in items:
            prof = s_.get('prof')
            if prof and len(prof) >= 2:
                zs = [q[2] for q in prof]
                zrange.append((min(zs), max(zs)))
            else:
                za, zb = s_['a'][2], s_['b'][2]
                zrange.append((za, zb) if za <= zb else (zb, za))

        for (a, b) in pairs:
            if (a, b) not in bonded:
                alo, ahi = zrange[a]
                blo, bhi = zrange[b]
                sep = blo - ahi if blo > ahi else alo - bhi
                if sep > STOREY_GAP_Z:
                    conflicts.add((a, b))
                    continue
            sa, pa = items[a]
            sb, pb = items[b]
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
            # Per-ribbon neighbour sets, so "does this ribbon conflict with (or
            # bond to) anything in that sub-sheet?" is a SET INTERSECTION instead
            # of a scan over the sub-sheet's members.  The scanning form rebuilt
            # a (min,max) tuple per member per candidate sub-sheet -- 17.8M
            # min/max calls and ~4.5s on Moranda02, the top cost once the union
            # and merge passes were indexed.
            cadj = {}
            for (a, b) in cset:
                cadj.setdefault(a, set()).add(b)
                cadj.setdefault(b, set()).add(a)
            badj = {}
            for (a, b) in bonded:
                if a in members and b in members:
                    badj.setdefault(a, set()).add(b)
                    badj.setdefault(b, set()).add(a)

            subs = []
            sub_sets = []                   # same membership, as sets
            sub_h = []                      # representative height per sub-sheet
            for i in sorted(members,
                            key=lambda k: -0.5 * (items[k][0]['na'][2] +
                                                  items[k][0]['nb'][2])):
                zi = 0.5 * (items[i][0]['na'][2] + items[i][0]['nb'][2])
                ci = cadj.get(i) or ()
                bi = badj.get(i) or ()
                best = None
                for si, sub in enumerate(subs):
                    if ci and not sub_sets[si].isdisjoint(ci):
                        continue
                    # A sub-sheet this ribbon is BONDED to (they meet at a
                    # pathgrid node and agree in height there) always wins: that
                    # is the junction, and splitting it is the defect.  Scoring on
                    # mean height alone loses it for the case it matters most --
                    # a STAIRCASE's mean sits midway between its two floors, so it
                    # is never near either sub-sheet and lands wherever the
                    # ordering happens to put it.
                    d = abs(sub_h[si] - zi)
                    rank = 0 if (bi and not sub_sets[si].isdisjoint(bi)) else 1
                    if best is None or (rank, d) < (best[0], best[1]):
                        best = (rank, d, si)
                if best is not None:
                    best = (best[1], best[2])
                if best is None:
                    subs.append([i])
                    sub_sets.append({i})
                    sub_h.append(zi)
                else:
                    si = best[1]
                    subs[si].append(i)
                    sub_sets[si].add(i)
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
                # Everything `sub` conflicts with, once -- then each candidate
                # merge target is one disjointness test rather than a nested
                # scan over both membership lists.
                sub_conf = set()
                for i in sub:
                    sub_conf |= cadj.get(i) or set()
                for mi, msub in enumerate(merged_subs):
                    if sub_conf and not sub_conf.isdisjoint(msub):
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
    from shapely import STRtree
    from shapely.ops import unary_union as _uu
    dup = []
    # R-tree over group_b, so each ribbon of group_a tests only the group_b
    # ribbons whose box actually meets it instead of all of them.  Candidates are
    # visited in ascending index order, which is the order the old nested loop
    # used, so `dup` is assembled identically.
    b_strips = list(group_b)
    b_polys = [_ribbon_polygon(sb) for sb in b_strips]
    if not b_strips:
        return None
    b_tree = STRtree(b_polys)
    for sa in group_a:
        pa = _ribbon_polygon(sa)
        if pa.is_empty or not pa.intersects(shared):
            continue
        for bi in sorted(b_tree.query(pa, predicate='intersects').tolist()):
            sb = b_strips[bi]
            pb = b_polys[bi]
            if pb.is_empty:
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
        # All 9 grid samples tested in ONE vectorised call.  Building a shapely
        # Point per sample and testing it scalar-wise cost 274k Point objects and
        # ~4.8s of a 17s cell; shapely.points + shapely.intersects do the same
        # work in bulk C.  Order is preserved, so the sample list -- and hence the
        # min below -- is identical to the scalar version.
        grid = [(minx + (maxx - minx) * fx, miny + (maxy - miny) * fy)
                for fx in (0.25, 0.5, 0.75) for fy in (0.25, 0.5, 0.75)]
        hits = _sh_intersects(inter, _sh_points(grid))
        pts.extend(g for g, hit in zip(grid, hits.tolist()) if hit)
    except Exception:
        pass
    if not pts:
        return float('inf')
    return min(abs(_height_on(sa, px, py) - _height_on(sb, px, py))
               for (px, py) in pts)


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
                # The group grew, so its cached footprint is now stale; a later
                # door must be matched against the union INCLUDING this one.
                try:
                    group_polys[best[1]] = unary_union(
                        [_ribbon_polygon(x) for x in out[best[1]]])
                except Exception:
                    group_polys[best[1]] = None
            else:
                # A door that matched nothing becomes its own group. group_polys
                # is indexed by the same `gi` as `out` in the loop above, so it
                # MUST grow in step -- without this the next door's
                # `group_polys[gi]` ran off the end (IndexError, which run_job
                # swallowed into a silently missing navmesh; measured on Nehrim
                # cells 012217C1 and 01193F44).
                out.append([s])
                try:
                    group_polys.append(_ribbon_polygon(s))
                except Exception:
                    group_polys.append(None)
    return out or [list(strips)]


def _apply_door_apex_levels(v2, levels, door_edges):
    """Give each reserved door triangle's APEX the levels of its base line.

    The apex sits inside the reserved hole, so nothing covers it and it has no
    level of its own; without this it is dropped by _emit_surfaces along with
    the whole door triangle.
    """
    if not door_edges:
        return
    idx = {}
    for i, p in enumerate(v2):
        idx.setdefault((round(p[0], 3), round(p[1], 3)), i)
    for e in door_edges:
        p0, p1 = e[0], e[1]
        door_z = e[3] if len(e) > 3 and e[3] is not None else None
        i0 = idx.get((round(p0[0], 3), round(p0[1], 3)))
        i1 = idx.get((round(p1[0], 3), round(p1[1], 3)))
        if i0 is None or i1 is None:
            continue
        # A doorway can be WIDER than the corridor ribbon crossing it, so a
        # base endpoint may stand on ground no strip covers (Arvena's
        # upstairs door: base corners at x=-368/-272, the crossing ribbon only
        # spans -360..-280) — it then has no level, is dropped by
        # _emit_surfaces, and the whole door triangle goes with it.  The door
        # knows its own storey; seed the endpoints with it.
        if door_z is not None:
            for ii in (i0, i1):
                if not any(abs(q - door_z) <= SAME_SURFACE_Z
                           for q in levels[ii]):
                    levels[ii] = sorted(set(list(levels[ii])
                                            + [float(door_z)]))
        base_lv = sorted(set(list(levels[i0]) + list(levels[i1])))
        if not base_lv:
            continue
        # The corners the door introduces must land on the DOOR's storey, not
        # on every storey the base endpoints touch (a base endpoint shared
        # with a stacked floor carries both heights).
        lv_door = [q for q in base_lv
                   if door_z is None or abs(q - door_z) <= STOREY_GAP_Z]
        lv_door = lv_door or base_lv
        # The apex is known exactly (the analytic wedge corner) — give it the
        # door's levels directly when nothing covers it.
        reach = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        if len(e) > 2 and e[2] is not None:
            ia = idx.get((round(e[2][0], 3), round(e[2][1], 3)))
            if ia is not None and not levels[ia]:
                levels[ia] = list(lv_door)
            reach = max(reach, math.hypot(
                e[2][0] - 0.5 * (p0[0] + p1[0]),
                e[2][1] - 0.5 * (p0[1] + p1[1])) + 4.0)
        # Any other level-less vertex near this base line was introduced by
        # the reservation (a hole-outline corner) and stands on the same
        # ground as the base.
        mx = 0.5 * (p0[0] + p1[0])
        my = 0.5 * (p0[1] + p1[1])
        for i, p in enumerate(v2):
            if levels[i]:
                continue
            if math.hypot(p[0] - mx, p[1] - my) <= reach:
                levels[i] = list(lv_door)


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
    # Only a corner's OWN levels count here.  (bare_clusters is derived from
    # tri_surfaces further down, so it cannot be consulted at this point;
    # corners with no levels take the fallback path below.)  Precomputed once
    # per corner — the per-triangle closure recomputed these min/max pairs
    # millions of times and was ~40% of a large cell's whole build.
    reps_all = [[(min(b), max(b)) for b in bands] for bands in corner_bands]

    def band_reps(k):
        return reps_all[k]

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
        for (lo, hi) in bands:
            if lo - tol <= z <= hi + tol:
                return True
        return False

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
            ok = True
            for bands in present:
                if not _reaches(bands, z):
                    ok = False
                    break
            # Collapse proposals that are the same storey, so one surface is
            # not emitted twice under slightly different names.
            if ok and (not surfaces or z - surfaces[-1] > STOREY_GAP_Z):
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
        best_i = 0
        best_d = None
        for i, band in enumerate(bands):
            for x in band:
                d = x - z
                if d < 0:
                    d = -d
                if best_d is None or d < best_d:
                    best_d = d
                    best_i = i
        return best_i

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


def _fill_boundary_notches(verts, tris, strips):
    """Close narrow V-shaped notches bitten out of the walkable surface.

    Where two sheets meet at an angle (a stair mouth meeting its floor), the
    boundaries can stop short of each other and leave a sliver-shaped bite in
    the surface, apex inward.  It is not a T-junction — no vertex lies on
    either edge, so the zipper cannot see it — and it is not a coverage hole
    either, because the sheet BELOW still covers the plan area, so the
    pathgrid-coverage test passes.  What it does is break adjacency across the
    mouth, and the notch mouth is exactly where the corridor is narrowest: the
    author's report was "a missing sliver near the bottom of the staircase
    that chokes the width by half" (measured on ImperialDungeon01 at the tower
    stair bottom, vertices (-64.9,134.8,31.3)/(-66.5,149.9,31.3) against
    (-113.1,211.8,98.5), a 4-edge open V straddling the walked line n124->n125).

    Fill only a notch that is:
      * TWO boundary edges sharing a vertex (the apex), with their far ends
        close enough to bridge (a wide V is a real room corner, not a bite);
      * near a walked pathgrid line — the pathgrid vouches that an actor
        crosses here, which is what makes the bite a defect rather than
        authored geometry;
      * fillable without giving any edge a third owner.
    """
    import math as _m
    if not strips:
        return tris
    tris = [tuple(t) for t in tris]
    # Walked-line lookup, bucketed.
    cellsz = NOTCH_NEAR_LINE
    lines = {}
    for s in strips:
        ax, ay, az = s['a'][0], s['a'][1], s['a'][2]
        bx, by, bz = s['b'][0], s['b'][1], s['b'][2]
        n = max(1, int(_m.hypot(bx - ax, by - ay) / cellsz) + 1)
        for i in range(n + 1):
            f = i / n
            x, y = ax + (bx - ax) * f, ay + (by - ay) * f
            lines.setdefault((int(x // cellsz), int(y // cellsz)),
                             []).append((x, y, az + (bz - az) * f))

    def _near_line(x, y, z):
        gx, gy = int(x // cellsz), int(y // cellsz)
        for ddx in (-1, 0, 1):
            for ddy in (-1, 0, 1):
                for (px, py, pz) in lines.get((gx + ddx, gy + ddy), ()):
                    if ((px - x) ** 2 + (py - y) ** 2 <= cellsz * cellsz
                            and abs(pz - z) <= STOREY_GAP_Z):
                        return True
        return False

    from shapely.geometry import Polygon as _NP
    from shapely import STRtree as _NT

    def _overlaps_existing(verts, tris, tri, index):
        tree, gmap, polys = index
        if tree is None:
            return False
        pa, pb, pc = verts[tri[0]], verts[tri[1]], verts[tri[2]]
        try:
            cand = _NP([(pa[0], pa[1]), (pb[0], pb[1]), (pc[0], pc[1])])
        except Exception:
            return True
        if not cand.is_valid or cand.area <= 1.0:
            return True
        zlo = min(pa[2], pb[2], pc[2])
        zhi = max(pa[2], pb[2], pc[2])
        for gi in tree.query(cand).tolist():
            ti = gmap[gi]
            if set(tris[ti]) & set(tri):
                continue
            tz = [verts[i][2] for i in tris[ti]]
            if min(tz) > zhi + STOREY_GAP_Z or max(tz) < zlo - STOREY_GAP_Z:
                continue
            try:
                if cand.intersection(polys[gi]).area > 4.0:
                    return True
            except Exception:
                return True
        return False

    for _round in range(2):
        counts = {}
        for t in tris:
            for k in range(3):
                a, b = t[k], t[(k + 1) % 3]
                key = (a, b) if a < b else (b, a)
                counts[key] = counts.get(key, 0) + 1
        border = [e for e, c in counts.items() if c == 1]
        if not border:
            break
        geoms = []
        gmap = []
        for ti, t in enumerate(tris):
            pa, pb, pc = verts[t[0]], verts[t[1]], verts[t[2]]
            try:
                pg = _NP([(pa[0], pa[1]), (pb[0], pb[1]), (pc[0], pc[1])])
            except Exception:
                continue
            if pg.is_valid and pg.area > 1.0:
                geoms.append(pg)
                gmap.append(ti)
        index = (_NT(geoms) if geoms else None, gmap, geoms)
        at = {}
        for (a, b) in border:
            at.setdefault(a, []).append(b)
            at.setdefault(b, []).append(a)
        added = []
        used = set()
        for apex, ends in at.items():
            if len(ends) != 2 or apex in used:
                continue
            p, q = ends
            if p in used or q in used:
                continue
            va, vp, vq = verts[apex], verts[p], verts[q]
            gap = _m.dist(vp[:2], vq[:2])
            if gap < 1e-6 or gap > NOTCH_MAX_MOUTH:
                continue
            # The notch must be DEEP relative to its mouth — a slim V bitten
            # into the surface, not an ordinary convex corner of a room.
            side = min(_m.dist(va[:2], vp[:2]), _m.dist(va[:2], vq[:2]))
            if side < 1e-6 or side < gap * NOTCH_MIN_DEPTH_RATIO:
                continue
            key = (min(p, q), max(p, q))
            if counts.get(key, 0) >= 1:
                continue                # bridging edge already exists
            mx = (va[0] + vp[0] + vq[0]) / 3.0
            my = (va[1] + vp[1] + vq[1]) / 3.0
            mz = (va[2] + vp[2] + vq[2]) / 3.0
            if not _near_line(mx, my, mz):
                continue
            cross = ((vp[0] - va[0]) * (vq[1] - va[1])
                     - (vq[0] - va[0]) * (vp[1] - va[1]))
            if abs(cross) < 1.0:
                continue
            tri = (apex, p, q) if cross > 0 else (apex, q, p)
            # The notch's plan area is usually still covered by the sheet
            # BELOW (which is why the coverage test never saw a hole), so a
            # blind fill stacks a second surface on it and the engine picks
            # one arbitrarily — ID01's same-surface overlaps went 2 -> 7.
            # Only fill where nothing already covers this ground at this
            # height.
            if _overlaps_existing(verts, tris, tri, index):
                continue
            added.append(tri)
            used.update((apex, p, q))
            counts[key] = counts.get(key, 0) + 1
        if not added:
            break
        tris.extend(added)
    return tris


# A notch mouth wider than this is a room corner, not a bite out of the mesh.
NOTCH_MAX_MOUTH = 160.0
# ...and it must be DEEP next to that mouth (a slim V, not a shallow wedge).
# NOTE the direction: side >= mouth * ratio.  The first version had this
# backwards (mouth > side * ratio) and so never fired on the very notches it
# was written for — the stair V-cracks, whose mouth is 15u against 65u sides.
NOTCH_MIN_DEPTH_RATIO = 1.2
# How close the fill must be to a walked pathgrid line to be justified.  64
# was under half a ribbon width, so a notch bitten out of the SIDE of a
# corridor (which is exactly where they appear) fell outside it and was never
# filled; 192 covers the full ribbon plus its grow margin.
NOTCH_NEAR_LINE = 192.0


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
    prof = []
    for s_ in strips:
        p = s_.get('poly')
        off, n = 0, 0
        if p is not None:
            off, n = len(poly), len(p)
            poly.extend(p)
        pr = s_.get('prof')
        proff, prn = 0, 0
        if pr is not None and len(pr) >= 2:
            proff, prn = len(prof), len(pr)
            prof.extend(pr)
        a_, b_ = s_['a'], s_['b']
        rows.append((a_[0], a_[1], a_[2], b_[0], b_[1], b_[2],
                     float(s_['half']), float(off), float(n),
                     float(proff), float(prn)))
    sarr = (np.asarray(rows, dtype=np.float64) if rows
            else np.zeros((0, 11), dtype=np.float64))
    parr = (np.asarray(poly, dtype=np.float64).reshape(-1, 2) if poly
            else np.zeros((0, 2), dtype=np.float64))
    farr = (np.asarray(prof, dtype=np.float64).reshape(-1, 3) if prof
            else np.zeros((0, 3), dtype=np.float64))
    qarr = (np.asarray(points, dtype=np.float64).reshape(-1, 2) if len(points)
            else np.zeros((0, 2), dtype=np.float64))
    return native.levels_at(sarr, parr, qarr, float(SAME_SURFACE_Z), farr)


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
