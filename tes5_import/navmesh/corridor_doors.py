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

DOOR_BRIDGE_RADIUS = 220.0
DOOR_LINE_HALF = 45.0
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


def door_footprints(verts, tris, doors, wall_hit=None):
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

    for (dx, dy, dz, rz, _is_tp) in doors:
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
            cands.append((d2, mx, my, a, b))
        cands.sort(key=lambda c: c[0])

        best = None
        for (d2, mx, my, a, b) in cands:
            if wall_hit is not None and _blocked_between(
                    wall_hit, dx, dy, dz, mx, my):
                continue
            best = (d2, a, b)
            break
        if best is None:
            continue

        _d2, ea, eb = best
        # Sit the footprint on the CORRIDOR's height: the quad has to meet the
        # ribbon it bridges to, and the door REFR's own z is only approximate.
        storey_z = 0.5 * (verts[ea][2] + verts[eb][2])
        tx, ty = math.cos(rz), math.sin(rz)
        blx, bly = dx + tx * DOOR_LINE_HALF, dy + ty * DOOR_LINE_HALF
        brx, bry = dx - tx * DOOR_LINE_HALF, dy - ty * DOOR_LINE_HALF

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
        mx = 0.5 * (verts[ea][0] + verts[eb][0])
        my = 0.5 * (verts[ea][1] + verts[eb][1])
        # Depth = how far the corridor is, measured along the door's facing
        # (perpendicular to the base line), so the sweep is square to the door.
        #
        # The nearest corridor edge is usually RIGHT AT the threshold, so this
        # projection alone gave depths of 1-20u: a 90x1.3u sliver that cannot
        # connect to anything and shows up as a rogue scrap.  The quad must be
        # deep enough to actually overlap the corridor it is bridging to, so the
        # depth is floored at DOOR_MIN_DEPTH and pushed PAST the corridor edge by
        # DOOR_OVERLAP, guaranteeing the union merges the two.
        fx, fy = -ty, tx
        depth = (mx - dx) * fx + (my - dy) * fy
        sign = -1.0 if depth < 0 else 1.0
        depth = sign * max(abs(depth) + DOOR_OVERLAP, DOOR_MIN_DEPTH)
        flx, fly = blx + fx * depth, bly + fy * depth
        frx, fry = brx + fx * depth, bry + fy * depth

        poly = [(blx, bly), (brx, bry), (frx, fry), (flx, fly)]
        out.append({'base': ((blx, bly), (brx, bry)),
                    'poly': poly, 'z': storey_z})
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
