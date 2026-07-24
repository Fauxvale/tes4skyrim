"""Phase-2 corridor width-grow: march an actor slab out to walls / floor edge.

Phase 1 lays a fixed-width flat ribbon on every pathgrid edge.  Phase 2 keeps
the centerline sacred (never moved — principle 1) and the ribbon flat on the
centerline plane (principle 2), but replaces the single fixed half-width with a
per-cross-section, per-side GROWN half-width.

From each cross-section centre we step outward along the perpendicular.  Growth
stops one step before the FIRST of:

  (a) WALL — a thin vertical slab standing at the trial point intersects the
      blocking soup.  The slab is actor HEIGHT and (roughly) actor WIDTH along
      the edge tangent, but only a THIN sliver deep along the march direction,
      so the ribbon gets right up against the wall / doorway jamb instead of
      stopping an agent-radius short of it (that shortfall was narrowing every
      doorway).
  (b) FLOOR EDGE — the walkable surface under the trial point departs from the
      centerline floor Z by more than MAX_CLIMB, or there is no walkable surface
      there at all.  This is what stops the rail climbing onto a bed / table
      (its top is a step up) and what stops a ground-floor corridor widening
      sideways into the footprint of a different storey (the floor there is a
      whole storey away, so it reads as departed).
  (c) NEIGHBOUR — the midpoint toward the nearest roughly-PARALLEL other
      pathgrid edge's centerline, so two parallel corridors meet cleanly.
  (d) the hard cap RIBBON_GROW_MAX_HALF.

The march measures against FIXED geometry (the same blocking/walkable soups and
the same neighbour centerlines every time), never against other corridors'
already-grown width, so it is order-independent and the output stays
byte-reproducible.  The overlap the grow creates at junctions and between
parallel corridors is resolved by the boolean union in corridor_union.
"""

import math

import numpy as np

from . import params


# ---------------------------------------------------------------------------
# Shared multi-bucket triangle index
# ---------------------------------------------------------------------------

class _TriGrid:
    """Coarse XY bucket index over a triangle soup, with a 3x3-bucket query.

    The single-bucket lookups elsewhere miss a triangle whenever the query point
    sits near a bucket boundary — which for a wall test means growth walks
    straight through the wall.  This queries the point's bucket AND its eight
    neighbours, so a triangle within one bucket (>= the probe extent) is never
    missed.
    """

    __slots__ = ('B', 'cell', 'minx', 'miny', 'grid',
                 'x0', 'x1', 'y0', 'y1', 'z0', 'z1')

    def __init__(self, tris, cell=128.0):
        self.B = np.asarray(tris, dtype=float).reshape(-1, 3, 3)
        self.cell = cell
        self.grid = {}
        if not len(self.B):
            self.minx = self.miny = 0.0
            self.x0 = self.x1 = self.y0 = self.y1 = self.z0 = self.z1 = None
            return
        self.x0 = self.B[:, :, 0].min(axis=1)
        self.x1 = self.B[:, :, 0].max(axis=1)
        self.y0 = self.B[:, :, 1].min(axis=1)
        self.y1 = self.B[:, :, 1].max(axis=1)
        self.z0 = self.B[:, :, 2].min(axis=1)
        self.z1 = self.B[:, :, 2].max(axis=1)
        self.minx = float(self.x0.min())
        self.miny = float(self.y0.min())
        for i in range(len(self.B)):
            gx0 = int((self.x0[i] - self.minx) // cell)
            gx1 = int((self.x1[i] - self.minx) // cell)
            gy0 = int((self.y0[i] - self.miny) // cell)
            gy1 = int((self.y1[i] - self.miny) // cell)
            for gx in range(gx0, gx1 + 1):
                for gy in range(gy0, gy1 + 1):
                    self.grid.setdefault((gx, gy), []).append(i)

    def candidates(self, x, y):
        if not self.grid:
            return ()
        gx = int((x - self.minx) // self.cell)
        gy = int((y - self.miny) // self.cell)
        out = []
        seen = out.append
        got = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self.grid.get((gx + dx, gy + dy), ()):
                    if i not in got:
                        got.add(i)
                        seen(i)
        return out


# ---------------------------------------------------------------------------
# Walkable surface sampler (multi-bucket)
# ---------------------------------------------------------------------------

def walkable_sampler(walkable):
    """f(x, y, near_z) -> walkable Z at (x,y) nearest near_z, or None."""
    tg = _TriGrid(walkable)
    B = tg.B

    def sample(x, y, near_z):
        best = None
        for i in tg.candidates(x, y):
            a, b, c = B[i]
            d = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
            if abs(d) < 1e-6:
                continue
            l0 = ((b[1] - c[1]) * (x - c[0]) + (c[0] - b[0]) * (y - c[1])) / d
            l1 = ((c[1] - a[1]) * (x - c[0]) + (a[0] - c[0]) * (y - c[1])) / d
            l2 = 1.0 - l0 - l1
            if l0 < -0.02 or l1 < -0.02 or l2 < -0.02:
                continue
            z = l0 * a[2] + l1 * b[2] + l2 * c[2]
            if best is None or abs(z - near_z) < abs(best - near_z):
                best = z
        return best

    return sample


# ---------------------------------------------------------------------------
# Thin vertical slab vs blocking triangle
# ---------------------------------------------------------------------------

def _tri_hits_slab(tri, cx, cy, ux, uy, half_w, tx, ty, depth, z_lo, z_hi):
    """True if triangle `tri` intersects the thin oriented slab.

    The slab is an oriented box centred at (cx, cy): extent `half_w` along the
    edge-tangent (tx, ty) (~actor width), `depth` along the march direction
    (ux, uy) (thin), full Z span [z_lo, z_hi].  We test in the slab's own 2D
    frame (tangent = X', march = Y') by projecting the triangle's vertices and
    doing a cheap separating-axis check of the triangle vs the axis-aligned
    rectangle [-half_w, half_w] x [-depth, depth], gated by Z overlap.
    """
    # Z gate first (cheap): the triangle must reach into the column.
    tzs = (tri[0][2], tri[1][2], tri[2][2])
    if max(tzs) < z_lo or min(tzs) > z_hi:
        return False
    # Project the three vertices into the slab frame.
    px = []
    py = []
    for v in tri:
        ox, oy = v[0] - cx, v[1] - cy
        px.append(ox * tx + oy * ty)      # along tangent
        py.append(ox * ux + oy * uy)      # along march
    # SAT axis 1/2: slab's own axes (rectangle is axis-aligned in this frame).
    if min(px) > half_w or max(px) < -half_w:
        return False
    if min(py) > depth or max(py) < -depth:
        return False
    # SAT axis 3: the triangle's three edge normals.  Rectangle corners:
    rect = ((-half_w, -depth), (half_w, -depth), (half_w, depth), (-half_w, depth))
    tpts = ((px[0], py[0]), (px[1], py[1]), (px[2], py[2]))
    for k in range(3):
        ax, ay = tpts[k]
        bx, by = tpts[(k + 1) % 3]
        nx, ny = -(by - ay), (bx - ax)         # edge normal
        # project triangle
        tproj = [nx * p[0] + ny * p[1] for p in tpts]
        rproj = [nx * p[0] + ny * p[1] for p in rect]
        if min(tproj) > max(rproj) or max(tproj) < min(rproj):
            return False
    return True


def wall_slab_sampler(blocking):
    """Return f(cx, cy, ux, uy, tx, ty, z_lo, z_hi, depth=None) -> True if a wall
    stands in the actor slab there.  (ux,uy)=march dir, (tx,ty)=edge tangent.

    `depth` is the half-extent along the march direction.  Callers marching in
    steps pass HALF THE STEP (plus the sliver) and centre the slab on the step's
    midpoint, so consecutive probes sweep a continuous corridor and no wall can
    fall between two samples.
    """
    tg = _TriGrid(blocking)
    B = tg.B
    half_w = params.RIBBON_GROW_SLAB_HALF_WIDTH

    def hit(cx, cy, ux, uy, tx, ty, z_lo, z_hi, depth=None):
        if depth is None:
            depth = params.RIBBON_GROW_SLAB_DEPTH
        for i in tg.candidates(cx, cy):
            if tg.z1[i] < z_lo or tg.z0[i] > z_hi:
                continue
            if _tri_hits_slab(B[i], cx, cy, ux, uy, half_w, tx, ty,
                              depth, z_lo, z_hi):
                return True
        return False

    return hit


# ---------------------------------------------------------------------------
# Nearest roughly-PARALLEL other-edge centerline
# ---------------------------------------------------------------------------

class NeighbourField:
    """Nearest perpendicular distance to a roughly-parallel OTHER pathgrid edge.

    Only edges whose direction is within RIBBON_GROW_PARALLEL_DOT of THIS edge's
    direction count: a crossing or diverging edge is not an opposing corridor
    wall and must not cap this one's width (that pinched dense junctions).
    """

    def __init__(self, nodes, edges, node_z):
        self.segs = []          # (ax, ay, bx, by, dirx, diry, i, j, midz)
        for (i, j) in edges:
            if i >= len(nodes) or j >= len(nodes) or i == j:
                continue
            ax, ay = nodes[i][0], nodes[i][1]
            bx, by = nodes[j][0], nodes[j][1]
            dx, dy = bx - ax, by - ay
            ln = math.hypot(dx, dy)
            if ln < 1e-6:
                continue
            midz = 0.5 * (node_z[i] + node_z[j])
            self.segs.append((ax, ay, bx, by, dx / ln, dy / ln, i, j, midz))
        self.cell = 256.0
        self.grid = {}
        if not self.segs:
            self.minx = self.miny = 0.0
            return
        arr = np.asarray([[s[0], s[1], s[2], s[3]] for s in self.segs])
        self.minx = float(min(arr[:, 0].min(), arr[:, 2].min()))
        self.miny = float(min(arr[:, 1].min(), arr[:, 3].min()))
        for si, s in enumerate(self.segs):
            ax, ay, bx, by = s[0], s[1], s[2], s[3]
            gx0 = int((min(ax, bx) - self.minx) // self.cell)
            gx1 = int((max(ax, bx) - self.minx) // self.cell)
            gy0 = int((min(ay, by) - self.miny) // self.cell)
            gy1 = int((max(ay, by) - self.miny) // self.cell)
            for gx in range(gx0 - 1, gx1 + 2):
                for gy in range(gy0 - 1, gy1 + 2):
                    self.grid.setdefault((gx, gy), []).append(si)

    def nearest(self, x, y, z, exclude_nodes, dirx, diry):
        if not self.segs:
            return math.inf
        gx = int((x - self.minx) // self.cell)
        gy = int((y - self.miny) // self.cell)
        best = math.inf
        for si in self.grid.get((gx, gy), ()):
            ax, ay, bx, by, sdx, sdy, i, j, midz = self.segs[si]
            if i in exclude_nodes or j in exclude_nodes:
                continue
            if abs(midz - z) > params.RIBBON_GROW_NEIGHBOUR_ZTOL:
                continue
            if abs(sdx * dirx + sdy * diry) < params.RIBBON_GROW_PARALLEL_DOT:
                continue                    # not roughly parallel -> not a wall
            d = _seg_dist(x, y, ax, ay, bx, by)
            if d < best:
                best = d
        return best


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    d2 = dx * dx + dy * dy
    if d2 < 1e-9:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / d2))
    return math.hypot(px - (ax + dx * t), py - (ay + dy * t))


# ---------------------------------------------------------------------------
# Per-side outward march
# ---------------------------------------------------------------------------

def grow_node_disc(cx, cy, floor_z, exclude_nodes, wall_hit, walk_sample,
                   field, lo):
    """Radial fan around a pathgrid NODE -> a closed polygon (list of (x, y)).

    Ribbons grow only PERPENDICULAR to their own edge, so where two edges meet
    at an angle the outer corner is a notch no ribbon reaches — a right-angle
    junction leaves a square bite out of the mesh.  Growing the node itself
    radially fills exactly that corner: march outward on RIBBON_GROW_DISC_RAYS
    evenly-spaced bearings under the same stop rules as a rail, and close the
    ray ends into a polygon.  It joins the union like any other strip.
    """
    n = params.RIBBON_GROW_DISC_RAYS
    pts = []
    for k in range(n):
        ang = 2.0 * math.pi * k / n
        dx, dy = math.cos(ang), math.sin(ang)
        # tangent for the slab's width axis is perpendicular to the ray
        d = grow_half_width(cx, cy, floor_z, dx, dy, -dy, dx, exclude_nodes,
                            wall_hit, walk_sample, field, lo)
        pts.append((cx + dx * d, cy + dy * d))
    return pts


def grow_half_width(cx, cy, floor_z, dirx, diry, tanx, tany, exclude_nodes,
                    wall_hit, walk_sample, field, lo=None):
    """Grown half-width from centre (cx,cy) outward along the unit perpendicular
    (dirx,diry).  (tanx,tany) is the edge tangent (slab width axis).

    Stops at the first of: wall slab, walkable-floor departure (> MAX_CLIMB or
    no walkable there), neighbour midpoint, or the cap.  Never returns less than
    `lo` (the caller's per-station floor).
    """
    step = params.RIBBON_GROW_STEP
    cap = params.RIBBON_GROW_MAX_HALF
    if lo is None:
        lo = params.RIBBON_GROW_MIN_HALF

    nd = field.nearest(cx, cy, floor_z, exclude_nodes, tanx, tany)
    neighbour_cap = 0.5 * nd if math.isfinite(nd) else cap
    hard = min(cap, max(lo, neighbour_cap))

    z_lo = floor_z + params.RIBBON_GROW_SLAB_Z_BOTTOM
    z_hi = floor_z + params.AGENT_HEIGHT

    # `lo` is the caller's SOFT minimum (it keeps junctions overlapping).  A WALL
    # always overrides it: forcing the ribbon out to a connectivity floor drove
    # mesh straight through walls near every junction — the same defect the
    # Phase-1 unconditional width has.  So march the soft floor too, from zero,
    # and let the wall test cut it short.
    grown = 0.0
    d = 0.0
    while d < hard:
        nd_step = min(step, hard - d)
        prev = d
        d += nd_step
        # (a) WALL — test the SWEPT interval [prev, d], not the end point.  A
        # point probe with a thin slab steps straight OVER a wall whenever the
        # wall falls between two samples (measured: a 2u-deep slab on an 8u step
        # missed a wall by 2u on both sides and produced 124 through-wall
        # triangles in the Fighters Guild).  Centring the slab on the interval
        # midpoint with half the interval as depth (plus the slab's own sliver)
        # makes the sweep continuous, so a wall can never be skipped.
        mid = 0.5 * (prev + d)
        px = cx + dirx * mid
        py = cy + diry * mid
        sweep = 0.5 * nd_step + params.RIBBON_GROW_SLAB_DEPTH
        if wall_hit(px, py, dirx, diry, tanx, tany, z_lo, z_hi, sweep):
            # The wall is somewhere in (prev, d].  Bisect so the ribbon ends AT
            # the wall rather than up to a whole step short of it — the mesh is
            # supposed to hug collision (a step-short stop is what narrowed
            # doorways).
            lo_d, hi_d = prev, d
            for _ in range(params.RIBBON_GROW_BISECT):
                md = 0.5 * (lo_d + hi_d)
                mm = 0.5 * (lo_d + md)
                if wall_hit(cx + dirx * mm, cy + diry * mm, dirx, diry,
                            tanx, tany, z_lo, z_hi,
                            0.5 * (md - lo_d) + params.RIBBON_GROW_SLAB_DEPTH):
                    hi_d = md
                else:
                    lo_d = md
            grown = max(grown, lo_d)
            break
        # (b) walkable floor edge: the floor must stay near the centerline plane.
        # Only binds BEYOND the soft floor `lo` — inside it the pathgrid's own
        # assertion wins (a node sitting at a threshold or a ledge lip would
        # otherwise collapse its corridor to nothing), and no wall was found
        # there, so nothing can be on the far side of anything.
        if walk_sample is not None and d > lo:
            s = walk_sample(cx + dirx * d, cy + diry * d, floor_z)
            if s is None or abs(s - floor_z) > params.MAX_CLIMB:
                break
        grown = d
    return grown
