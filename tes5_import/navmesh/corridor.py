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

Design principles (see docs/navmesh_corridor_redesign.md):
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

    def sample(x, y, near_z):
        gx = int((x - minx) // cell)
        gy = int((y - miny) // cell)
        best = None
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
            z = l0 * a[2] + l1 * b[2] + l2 * c[2]
            if best is None or abs(z - near_z) < abs(best - near_z):
                best = z
        return best

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

def _build_corridor_strips(nodes, edges, node_z, wall_hit=None,
                           walk_probe=None, field=None):
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
    """
    half = params.RIBBON_HALF_WIDTH
    ext = params.RIBBON_END_EXTEND
    grow = params.RIBBON_GROW and wall_hit is not None and field is not None \
        and walk_probe is not None

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
        pa = (ax - ux * ea, ay - uy * ea, az - dz * (ea / length))
        pb = (bx + ux * eb, by + uy * eb, bz + dz * (eb / length))

        strip = {
            'edge': (i, j),
            'na': (ax, ay, az), 'nb': (bx, by, bz),
            'a': pa, 'b': pb,
            'u': (ux, uy), 'w': (wx, wy), 'len': length,
        }

        # A STEEP edge is a staircase/ramp.  Growing it sideways is meaningless
        # and actively harmful: the ribbon is a tilted plane, so a perpendicular
        # rail immediately leaves the treads — it either floats off the side of
        # the flight or drives through the stairwell wall (measured: the Guild's
        # stair edge grew to 82u and put mesh through the wall beside it).  A
        # staircase is exactly as wide as the pathgrid says, so keep Phase-1
        # width there and only grow genuinely FLAT corridors.
        steep = abs(pb[2] - pa[2]) / max(length, 1e-6) > params.RIBBON_GROW_MAX_SLOPE

        if not grow or steep:
            strip['half'] = half
            strips.append(strip)
            continue

        # Phase 2: march the width out per cross-section on each side.  Sample
        # at RIBBON_STEP stations from a-end to b-end; the flat plane means the
        # floor Z under the box is the centerline Z at that station.
        #
        # Connectivity guard: near a NODE the width must not fall below the
        # Phase-1 half-width, or two corridors meeting there stop overlapping and
        # the union splits the mesh (Pinarus's 73 dense edges fragmented into 11
        # components when the grow was allowed to pinch to the global minimum at a
        # junction).  So the per-station FLOOR ramps from RIBBON_HALF_WIDTH at
        # each endpoint down to RIBBON_GROW_MIN_HALF over a RIBBON_HALF_WIDTH-long
        # zone: the overlapping core around every shared node is preserved, while
        # the middle of a long edge is still free to pinch to a wall.
        total = length + ea + eb
        k = max(1, int(round(total / params.RIBBON_STEP)))
        ramp = params.RIBBON_HALF_WIDTH          # length of the endpoint zone
        lo0 = params.RIBBON_HALF_WIDTH
        lo1 = params.RIBBON_GROW_MIN_HALF
        left = []                   # (x, y) along +w
        right = []                  # (x, y) along -w
        max_h = lo0
        for s in range(k + 1):
            t = s / k
            cxs = pa[0] + (pb[0] - pa[0]) * t
            cys = pa[1] + (pb[1] - pa[1]) * t
            czs = pa[2] + (pb[2] - pa[2]) * t
            # distance from this station to the nearer endpoint, along the edge
            d_end = min(t, 1.0 - t) * total
            frac = min(1.0, d_end / ramp) if ramp > 1e-6 else 1.0
            floor_h = lo0 + (lo1 - lo0) * frac    # lo0 at the ends, lo1 mid-edge
            hl = corridor_grow.grow_half_width(
                cxs, cys, czs, wx, wy, ux, uy, (i, j),
                wall_hit, walk_probe, field, floor_h)
            hr = corridor_grow.grow_half_width(
                cxs, cys, czs, -wx, -wy, ux, uy, (i, j),
                wall_hit, walk_probe, field, floor_h)
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
    # — a right-angle junction leaves a square bite out of the mesh.  Grow each
    # node radially under the same stop rules and add the resulting polygon to
    # the union, which fills exactly those corners.
    if grow:
        # A node on a STEEP edge (stair/ramp landing) is not grown, for the same
        # reason the steep edge itself is not: a radial fan there leaves the
        # treads at once and reaches over the stairwell wall.
        steep_nodes = set()
        for (i, j) in edges:
            if i >= len(nodes) or j >= len(nodes) or i == j:
                continue
            run = math.hypot(nodes[j][0] - nodes[i][0], nodes[j][1] - nodes[i][1])
            if run > 1e-6 and abs(node_z[j] - node_z[i]) / run > \
                    params.RIBBON_GROW_MAX_SLOPE:
                steep_nodes.add(i)
                steep_nodes.add(j)
        for ni in sorted(degree):
            if ni >= len(nodes) or ni in steep_nodes:
                continue
            nx, ny = nodes[ni][0], nodes[ni][1]
            nz = node_z[ni]
            # Floor of 0: a wall must always win over any minimum, or the disc
            # pushes mesh through a wall standing close to the node (the same
            # defect the rails' connectivity floor caused).  The node's own
            # ribbons already guarantee the corridor width here.
            disc = corridor_grow.grow_node_disc(
                nx, ny, nz, (ni,), wall_hit, walk_probe, field, 0.0)
            disc = _simplify(disc, params.RIBBON_RAIL_SIMPLIFY)
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
                    land_rec=None, origin_x=0.0, origin_y=0.0, doors=None):
    """Phase-1 corridor navmesh for one cell.  Returns (verts, tris) lists.

    doors: [(x, y, z, rot_z, is_teleport), ...] pivot-corrected door centres.
    """
    if not nodes or not edges:
        return [], []

    walkable, blocking, land_walk = world.gather_cell_geometry(
        refr_recs or [], base_model_by_fid or {}, get_collision,
        land_rec=land_rec, origin_x=origin_x, origin_y=origin_y,
        split_land=True)
    if land_walk is not None and len(land_walk):
        walkable = (np.concatenate([walkable, land_walk])
                    if len(walkable) else land_walk)

    sample = _surface_sampler(walkable)

    # Node heights: snap each node down onto walkable collision.
    node_z = [_snap_node_z(sample, nodes[i][0], nodes[i][1], nodes[i][2])
              for i in range(len(nodes))]

    # Phase-2 width-grow inputs.  All are built once per cell over FIXED
    # geometry, so growth is order-independent and the output byte-reproducible:
    #   wall_hit    thin actor slab vs blocking  -> stop AT the wall/jamb
    #   walk_probe  walkable height (multi-bucket) -> stop at a floor edge, so a
    #               rail never climbs a bed/table or widen into another storey
    #   field       nearest roughly-PARALLEL other centerline -> meet halfway
    # wall_hit is built unconditionally: the DOOR footprint needs it to refuse a
    # bridge that crosses a wall, whether or not width-grow is enabled.
    wall_hit = corridor_grow.wall_slab_sampler(blocking)
    walk_probe = field = None
    if params.RIBBON_GROW:
        walk_probe = corridor_grow.walkable_sampler(walkable)
        field = corridor_grow.NeighbourField(nodes, edges, node_z)

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
                                       wall_hit, walk_probe, field)

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

    door_list = [(x, y, z, r, tp) for (x, y, z, r, tp) in (doors or ())]
    door_strips = []
    door_edges = []
    if door_list:
        rv, rt = corridor_union.build_union_mesh(corridors,
                                                 cell_bounds=cell_clip,
                                                 wall_cut=wall_cut)
        if rt:
            for fp in corridor_doors.door_footprints(rv, rt, door_list,
                                                     wall_hit=wall_hit):
                door_strips.append(corridor_union._poly_strip(fp['poly'],
                                                              fp['z']))
                door_edges.append(fp['base'])

    verts, tris = corridor_union.build_union_mesh(
        corridors, extra_strips=door_strips, door_edges=door_edges,
        cell_bounds=cell_clip, wall_cut=wall_cut)
    if not tris:
        return [], []

    cs = params.CS_EXTERIOR if land_rec is not None else params.CS
    # For dropping unreachable fringe scraps, a component is KEPT when it can
    # reach another cell — via a door, or (exterior) by touching the cell
    # border where a worldspace edge-link continues it.  Pass the door centres
    # and, for an exterior cell, its world-space bounds.
    door_xy = [(x, y, z) for (x, y, z, r, tp) in door_list]
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
    pin_xy = list(door_xy)
    for (i, j) in edges:
        if i >= len(nodes) or j >= len(nodes) or i == j:
            continue
        run = math.hypot(nodes[j][0] - nodes[i][0], nodes[j][1] - nodes[i][1])
        if run < 1e-6:
            continue
        steps = max(2, int(run // params.RIBBON_STEP))
        for s in range(steps + 1):
            f = s / steps
            pin_xy.append((nodes[i][0] + (nodes[j][0] - nodes[i][0]) * f,
                           nodes[i][1] + (nodes[j][1] - nodes[i][1]) * f,
                           node_z[i] + (node_z[j] - node_z[i]) * f))

    verts, tris = corridor_clean.finalize(verts, tris, cs=cs,
                                          doors=door_xy, cell_bounds=cell_bounds,
                                          pin_xy=pin_xy)

    return ([tuple(float(c) for c in v) for v in verts],
            [tuple(int(i) for i in t) for t in tris])
