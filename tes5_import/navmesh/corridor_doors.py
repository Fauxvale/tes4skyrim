r"""Compute a Door footprint at every door, to feed the corridor union.

Vanilla Skyrim marks a door with a triangle whose LONG EDGE runs PARALLEL to the
door line.  We reproduce that: for each door we find the base line (BL-BR, on the
door line) and the FOOTPRINT quad bridging it to the nearest corridor edge:

    BL ----------- BR      BL,BR = the door base, on the door line,
     |             |       DOOR_LINE_HALF either side of the (panel-centred)
     |             |       threshold.  BL-BR is the LONG SIDE handed to the
     |             |       triangulation as a forced edge.
     E0 ----------- E1     E0,E1 = the two ends of the nearest corridor edge,
                           ordered so E0 pairs with BL and E1 with BR.

`door_footprints` returns, per reachable door, the base line and the quad
BL-BR-E1-E0.  corridor.py feeds the quad into the boolean union as ordinary
ground and passes the base line as a union `door_edges` constraint, so the
retriangulation forms ONE large triangle with its long side on the door line —
the union owns and de-overlaps the door geometry, no separate stitching.

Conservative: a door whose nearest corridor edge midpoint is beyond
DOOR_BRIDGE_RADIUS is walled off from the pathgrid and yields nothing.
"""

import math

from . import params

# How far the nearest corridor edge may be for a door to get a footprint at
# all.  220 stranded real doors: ImperialDungeon01's upper walkway door sits
# 287u from the nearest pathgrid line and got NO Door Triangle, leaving the
# doorway dead in-engine.  The wall walk (_blocked_between) still rejects any
# candidate behind geometry, so a larger reach cannot bridge through a wall.
DOOR_BRIDGE_RADIUS = 384.0
# Fallback half-width, used only when a door model has no measured panel width
# (see pgrd_to_navm._DOOR_WIDTH).  Real widths come from the collision panel.
DOOR_LINE_HALF = 45.0
# A door wider than this is a city gate/portcullis whose full span would
# swallow the room; the base line is capped there.  A MEASURED width is
# otherwise used EXACTLY — widening a narrow gate to a minimum pushed the base
# line into the jambs on both sides (cgprisoncellgate01's old 40u single-bar
# measurement got a 90u base through the wall), and the door triangle must be
# the door's own width, no more and no less.
DOOR_LINE_HALF_MAX = 220.0
# Depth of the door triangle: apex on the perpendicular bisector of the base,
# on the side the pathgrid serves, at half the base length (near-equilateral,
# the vanilla shape) but never shallower than DOOR_MIN_DEPTH.  The triangle's
# shape is a pure function of the door — same width, same depth, same area,
# every build — instead of whatever apex the triangulation could fit.
DOOR_TRI_MIN_DEPTH = 64.0
# How far from each end the bridge collision walk is skipped.  A door REFR is a
# placed mesh standing ON the threshold, so a walk starting at the door hits the
# door panel itself and rejects every candidate; the corridor end is skipped by
# the same amount so the ribbon's own geometry cannot reject it either.  Wider
# than a door panel is thick, far narrower than a room.
DOOR_SELF_CLEARANCE = 48.0
# The footprint must be deep enough to genuinely overlap the corridor it bridges
# to.  The nearest corridor edge usually sits right at the threshold, so the raw
# projection gave 1-20u deep quads — slivers that connect to nothing.
DOOR_MIN_DEPTH = 64.0
# Extra depth pushed PAST the corridor edge so the union certainly merges them.
DOOR_OVERLAP = 32.0


def _sides_disconnected(nodes, pg_edges, dx, dy, dz, fcx, fcy, ztol):
    """True when the pathgrid on the door's two faces is DISCONNECTED.

    The far-side bridge exists for exactly one situation: walkable ground on
    both faces of a doorway with no pathgrid route between them (the prison
    cell gates — nodes inside the cell and out in the corridor are separate
    pathgrid components, so the ribbons never join).  Where the pathgrid IS
    connected across the door, the ribbon already runs through or around the
    doorway, and an extra quad only adds overlapping ground — measured, it
    severed the staircase sheets in Pinarus's and Arvena's houses.
    """
    if not nodes or not pg_edges:
        return False
    parent = list(range(len(nodes)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for (i, j) in pg_edges:
        if i < len(nodes) and j < len(nodes):
            parent[find(i)] = find(j)
    best = {}                        # side -> (d2, node)
    for ni, n in enumerate(nodes):
        if abs(n[2] - dz) > ztol:
            continue
        proj = (n[0] - dx) * fcx + (n[1] - dy) * fcy
        if abs(proj) < 1.0:
            continue
        side = 1 if proj > 0 else -1
        d2 = (n[0] - dx) ** 2 + (n[1] - dy) ** 2
        if side not in best or d2 < best[side][0]:
            best[side] = (d2, ni)
    if len(best) < 2:
        return False
    return find(best[1][1]) != find(best[-1][1])


def door_footprints(verts, tris, doors, wall_hit=None, nodes=None,
                    pg_edges=None):
    """Per door, the base line + connecting footprint to feed the union.

    Returns a list of dicts, one per door that has a reachable corridor edge:

        {'base':  ((blx, bly), (brx, bry)),      # long side, on the door line
         'poly':  [(x, y), ...],                  # footprint to union in as ground
         'z':     storey_z}                        # height of that ground

    The footprint is the quad BL-BR-E1-E0 bridging the door base to the nearest
    corridor edge, handed to the boolean union as an ordinary polygon so the
    union owns its triangles, while the base line is forced to be a triangle
    edge.  This produces the vanilla Skyrim door triangle: one big triangle whose
    long side lies on the door line.

    Conservative: a door whose nearest corridor edge is beyond
    DOOR_BRIDGE_RADIUS is walled off from the pathgrid and yields nothing.
    """
    verts = [list(map(float, v)) for v in verts]
    tris = [tuple(map(int, t)) for t in tris]
    out = []
    if not doors or not tris:
        return out

    ztol = params.DOOR_QUAD_ZTOL
    br2 = DOOR_BRIDGE_RADIUS ** 2

    # Corridor edges once (this reads the RAW ribbon union, unmodified).
    edges = set()
    for t in tris:
        for k in range(3):
            a, b = t[k], t[(k + 1) % 3]
            edges.add((a, b) if a < b else (b, a))

    for (dx, dy, dz, rz, _is_tp, door_w) in doors:
        # Rank every candidate corridor edge by distance, then take the NEAREST
        # one the door can reach WITHOUT crossing a wall.  A blocked candidate is
        # never used — it is skipped and the search continues outward to the next
        # one.  (Checking only candidates nearer than the current best let a
        # near-but-blocked edge shadow a slightly farther clear one, and the door
        # then produced no footprint at all.)
        # Rank candidates by DISTANCE and take the nearest one the door can reach
        # without crossing a wall, restricted to corridor on THIS door's storey.
        # The height gate matters: without it a door bridges to whatever ribbon
        # is nearest in plan view, which in Pinarus's house meant reaching up the
        # staircase and laying a triangle across the floor below it.  (The gate
        # is not what made doors fail to connect — that was the walk hitting the
        # door's OWN panel collision; see _blocked_between.)
        # The quad sweeps the base line along the door's FACING, so it can only
        # ever reach a corridor edge lying in the frontal strip: within the
        # doorway's span across the facing (plus a ribbon width of slack).  A
        # candidate displaced mostly ALONG the threshold axis is unreachable by
        # the sweep — accepting one laid a floating 5-triangle patch beside
        # ImperialDungeon01's tower door, whose only corridor runs 283u to the
        # door's SIDE (the pathgrid never approaches that door frontally).
        w_half = 0.5 * door_w if door_w else DOOR_LINE_HALF
        w_half = min(w_half, DOOR_LINE_HALF_MAX)
        # Directions under the TRANSPOSE placement convention (see
        # navmesh/world.py _rot_matrix): threshold = (sin rz, cos rz),
        # facing = (cos rz, -sin rz).  The old CCW forms drew/swept doors
        # mirrored for any rotation off 0/180.
        ttx, tty = math.sin(rz), math.cos(rz)       # threshold direction
        strip_half = w_half + params.RIBBON_HALF_WIDTH
        cands = []
        for (a, b) in edges:
            va, vb = verts[a], verts[b]
            mz = 0.5 * (va[2] + vb[2])
            if abs(mz - dz) > ztol:
                continue
            mx = 0.5 * (va[0] + vb[0])
            my = 0.5 * (va[1] + vb[1])
            d2 = (mx - dx) ** 2 + (my - dy) ** 2
            if d2 > br2:
                continue
            if abs((mx - dx) * ttx + (my - dy) * tty) > strip_half:
                continue
            cands.append((d2, mx, my, a, b))
        cands.sort(key=lambda c: c[0])

        # Which side the actor walks in from, decided by the PATHGRID -- the
        # only input that asserts "an actor walks here".  Derived ribbon edges
        # run past BOTH faces of most doorways, so nearest-edge / majority /
        # distance-weighted votes all disagreed with the pathgrid on ~47% of
        # doors; the nearest NODE is unambiguous.
        want_side = 0
        if nodes:
            ftx, fty = math.cos(rz), -math.sin(rz)  # == fx,fy below
            bn = None
            for n in nodes:
                proj = (n[0] - dx) * ftx + (n[1] - dy) * fty
                if abs(proj) < 1.0:
                    continue
                d2n = (n[0] - dx) ** 2 + (n[1] - dy) ** 2
                if bn is None or d2n < bn[0]:
                    bn = (d2n, proj)
            if bn is not None:
                want_side = 1 if bn[1] > 0 else -1

        # Nearest CLEAR candidate on each FACE of the door.  A door has two
        # faces 180 degrees apart; an interior door joins the walkable ground
        # on BOTH of them (the player's prison-cell gate has pathgrid inside
        # the cell and out in the corridor with no pathgrid edge across, so a
        # single-sided quad left the cell interior a disconnected island an
        # actor could never leave).  A teleport door's far side is another
        # cell — it gets the primary side only.
        fcx, fcy = math.cos(rz), -math.sin(rz)     # facing (transpose conv.)
        best_by_side = {}
        for (d2, mx, my, a, b) in cands:
            side = 1 if (mx - dx) * fcx + (my - dy) * fcy >= 0.0 else -1
            if side in best_by_side:
                continue
            if wall_hit is not None and _blocked_between(
                    wall_hit, dx, dy, dz, mx, my):
                continue
            best_by_side[side] = (d2, a, b, mx, my)
            if len(best_by_side) == 2:
                break
        if not best_by_side:
            continue

        # The PRIMARY side (the one that carries the base-line constraint and
        # so the Door Triangle) is the side the pathgrid says the door serves;
        # when that side has no clear corridor, whichever side does.
        if want_side in best_by_side:
            primary = want_side
        else:
            primary = sorted(best_by_side)[0]
        # Each footprint sits on ITS corridor's height (see _sweep): the quad
        # has to meet the ribbon it bridges to, and the door REFR's own z is
        # only approximate.
        # A door mesh's LOCAL +X points THROUGH the opening and local +Y runs
        # ALONG the threshold: measured on impdundoor01.nif, whose panel is
        # 5.6u thick in X and 115.3u wide in Y — a panel is thin through the
        # doorway and wide across it.  `_door_threshold` agrees: it rotates the
        # hinge->doorway-centre offset (which lies along local X) by the same
        # standard matrix, so local +X == (cos rz, sin rz) is the FACING.
        #
        # Using the facing as the base line laid the threshold across the axis
        # the door actually opens along — every door quad rotated 90 degrees
        # from its real opening, visible as a sideways door line in
        # navmesh_preview.  The base line is local +Y.
        tx, ty = math.sin(rz), math.cos(rz)
        # Span the REAL doorway.  Door panels run from 16u to 764u wide (median
        # 121), measured off each model's collision panel, so the old constant
        # 90u base line was simply the wrong size for most doors: on
        # impdundoor01 (115u) it left the first 30u of the threshold with no
        # mesh under it, and the Door Triangle came out a 571-unit scrap — below
        # the smallest of 1,659 vanilla door triangles (min 992, median 9,614),
        # too narrow for an actor to stand on.  That is what stopped the
        # CharacterGen assassins dead at their cell door.
        half = w_half                    # computed with the candidate gate
        blx, bly = dx + tx * half, dy + ty * half
        brx, bry = dx - tx * half, dy - ty * half

        # The footprint is a RECTANGLE: the door base line, swept to the corridor
        # along the door's facing.  Using the corridor edge's own two endpoints as
        # the far side (the previous shape) made the quad's width arbitrary — when
        # those endpoints projected close together the quad pinched to a wedge and
        # the door ended up joined to the mesh AT A POINT, with a long thin
        # triangle reaching off to whatever the other end was.  A rectangle
        # guarantees instead that
        #   * the base line BL-BR is one FULL edge (the vanilla door triangle's
        #     long side, and the union is told to keep it via `base`), and
        #   * the two triangles the rectangle splits into share the full diagonal,
        #     so the second is attached along an EDGE, never at a corner.
        # Depth = how far the corridor is, measured along the door's facing
        # (perpendicular to the base line), so the sweep is square to the door.
        #
        # The nearest corridor edge is usually RIGHT AT the threshold, so this
        # projection alone gave depths of 1-20u: a 90x1.3u sliver that cannot
        # connect to anything and shows up as a rogue scrap.  The quad must be
        # deep enough to actually overlap the corridor it is bridging to, so the
        # depth is floored at DOOR_MIN_DEPTH and pushed PAST the corridor edge by
        # DOOR_OVERLAP, guaranteeing the union merges the two.
        # Facing = local +X = perpendicular to the base line (tx, ty).
        # (fcx, fcy) above is the same vector; (fx, fy) keeps the historical
        # name for the sweep math below.
        fx, fy = ty, -tx

        # The door TRIANGLE is fixed analytically here, not discovered by the
        # triangulation: base = the full doorway, apex on the perpendicular
        # bisector at a depth that is a pure function of the width, on the
        # side the pathgrid serves.  The old apex search
        # (corridor_union._door_apex) tried BOTH normals and a ladder of
        # shrinking depths until a candidate fit inside the walkable polygon —
        # so a cramped near side flipped the whole triangle to the FAR side of
        # the door (three doors in ImperialDungeon01 had their reserved
        # triangle on the opposite side from the pathgrid), and the area
        # varied build to build with the surrounding geometry.
        apex_depth = max(w_half, DOOR_TRI_MIN_DEPTH)

        def _sweep(side, entry, with_base):
            """Footprint quad sweeping the base line toward `side`."""
            _sd2, sea, seb, semx, semy = entry
            s_z = 0.5 * (verts[sea][2] + verts[seb][2])
            depth = abs((semx - dx) * fx + (semy - dy) * fy)
            # The quad must reach past the corridor edge it bridges to
            # (DOOR_OVERLAP) AND past the door triangle's apex, so the wedge
            # reserved out of the union always sits on ground the quad itself
            # contributed.
            depth = float(side) * max(depth + DOOR_OVERLAP, DOOR_MIN_DEPTH,
                                      apex_depth + DOOR_OVERLAP)
            sflx, sfly = blx + fx * depth, bly + fy * depth
            sfrx, sfry = brx + fx * depth, bry + fy * depth
            # The quad spans EXACTLY the doorway — never wider.  Widening it
            # past the door line pushes the footprint through the wall on
            # either side of the frame.  The door triangle is protected by
            # reserving it out of the triangulation; see
            # corridor_union._triangulate.
            apex = (dx + fx * float(side) * apex_depth,
                    dy + fy * float(side) * apex_depth)
            return {'base': ((blx, bly), (brx, bry)) if with_base else None,
                    'apex': apex if with_base else None,
                    'poly': [(blx, bly), (brx, bry), (sfrx, sfry),
                             (sflx, sfly)],
                    'z': s_z}

        # PRIMARY quad: sweeps toward the side the PATHGRID says the door
        # serves (nearest node — derived ribbon edges run past BOTH faces of
        # most doorways, so nearest-edge/majority votes disagreed with the
        # pathgrid on 14 of 30 doors; keying on the node cut that to 2), and
        # carries the base-line constraint that becomes the Door Triangle.
        out.append(_sweep(primary, best_by_side[primary], True))

        # FAR-SIDE quad: bridges a doorway whose two faces have walkable
        # ground but NO pathgrid route between them (see _sides_disconnected)
        # — the prison-cell gates, whose interiors were unreachable islands.
        # No base constraint: one Door Triangle per door, on the primary side.
        other = -primary
        if (not _is_tp and other in best_by_side
                and _sides_disconnected(nodes, pg_edges, dx, dy, dz,
                                        fcx, fcy, ztol)):
            out.append(_sweep(other, best_by_side[other], False))
    return out


def _d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _blocked_between(wall_hit, x0, y0, z0, x1, y1):
    """True if blocking collision stands between the door and a corridor edge.

    Walks the bridge in RIBBON_GROW_STEP steps with the same thin actor slab the
    width-grow uses, starting just above the door's own floor so the threshold
    lip and the floor itself are not read as a wall.

    THE DOOR'S OWN COLLISION IS SKIPPED.  A door REFR is a placed mesh standing
    exactly on the threshold, so a walk that begins at the door position hits the
    door panel on its very first step and every candidate looks blocked — which
    left doors with wide open floor in front of them unconnected.  The walk
    therefore starts DOOR_SELF_CLEARANCE away from the door and stops the same
    distance short of the corridor edge; only genuine geometry BETWEEN them can
    reject the bridge.
    """
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy)
    if dist < 1e-6:
        return False
    ux, uy = dx / dist, dy / dist
    tx, ty = -uy, ux
    z_lo = z0 + params.RIBBON_GROW_SLAB_Z_BOTTOM
    z_hi = z0 + params.AGENT_HEIGHT
    step = params.RIBBON_GROW_STEP
    start = DOOR_SELF_CLEARANCE
    end = dist - DOOR_SELF_CLEARANCE
    if end <= start:
        return False                 # too short to contain anything but the door
    q = start
    while q < end:
        seg = min(step, end - q)
        mid = q + 0.5 * seg
        if wall_hit(x0 + ux * mid, y0 + uy * mid, ux, uy, tx, ty, z_lo, z_hi,
                    0.5 * seg + params.RIBBON_GROW_SLAB_DEPTH):
            return True
        q += seg
    return False
