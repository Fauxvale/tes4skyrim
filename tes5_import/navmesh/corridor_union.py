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

from . import params

from .union_mesh import (
    _destack as _destack,
    _drop_walls as _drop_walls,
    _merge_at_pathgrid_nodes as _merge_at_pathgrid_nodes,
    _split_t_junctions as _split_t_junctions,
    _stitch_shared_nodes as _stitch_shared_nodes,
    _tri_overlaps_mesh as _tri_overlaps_mesh,
    _weld_sheets as _weld_sheets,
)
from .union_sheets import (
    _overlap_height_gap as _overlap_height_gap,
    _same_surface_region as _same_surface_region,
    _split_plan_overlaps as _split_plan_overlaps,
    _storey_groups as _storey_groups,
    _subs_same_floor as _subs_same_floor,
)
from .union_cdt import (
    DOOR_SNAP_PERP as DOOR_SNAP_PERP,
    RIBBON_SEED_STEP as RIBBON_SEED_STEP,
    STEEP_REFINE_EDGE as STEEP_REFINE_EDGE,
    _door_edge_on_part as _door_edge_on_part,
    _earcut_fallback as _earcut_fallback,
    _flip2d as _flip2d,
    _hex_refine as _hex_refine,
    _recover_constraints as _recover_constraints,
    _refine_steep as _refine_steep,
    _ribbon_seeds as _ribbon_seeds,
    _snap_outline_to_door_lines as _snap_outline_to_door_lines,
    _triangulate as _triangulate,
)
from .union_geom import (
    FLAP_EDGE_DROP as FLAP_EDGE_DROP,
    REACH_TOL as REACH_TOL,
    SAME_SURFACE_Z as SAME_SURFACE_Z,
    STOREY_GAP_Z as STOREY_GAP_Z,
    WALL_SLOPE_COS as WALL_SLOPE_COS,
    _RIBBON_CACHE as _RIBBON_CACHE,
    _clip_strip_near as _clip_strip_near,
    _distance_to as _distance_to,
    _has_edge as _has_edge,
    _height_on as _height_on,
    _near as _near,
    _on_segment as _on_segment,
    _point_in_poly as _point_in_poly,
    _poly_strip as _poly_strip,
    _ribbon_cache_clear as _ribbon_cache_clear,
    _ribbon_polygon as _ribbon_polygon,
    _ribbon_polygon_uncached as _ribbon_polygon_uncached,
    _seg_dist as _seg_dist,
    _seg_intersect as _seg_intersect,
    _segment_cuts as _segment_cuts,
    _split_triangle as _split_triangle,
    _tri_area as _tri_area,
    _tri_components as _tri_components,
    _tri_edges as _tri_edges,
    _tri_span as _tri_span,
)


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
    from shapely.geometry import Polygon, box
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














# T-junction split tolerances (module-level so diagnostics can A/B them).
# TSPLIT_TOL: plan radius for INTERIOR hanging vertices.  TSPLIT_CRACK_TOL:
# plan radius when the hit vertex is itself on the boundary (crack zipper).
# TSPLIT_Z_TOL: the z window — 12u seals the measured 3-8u stair-fold cracks;
# MAX_CLIMB grabbed genuine fold vertices and minted overlaps.
TSPLIT_TOL = 2.0
TSPLIT_CRACK_TOL = 6.0
TSPLIT_Z_TOL = 12.0




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


