"""Tunable parameters for navmesh generation.

All distances are GAME UNITS.  Values are derived from Skyrim's actual actor
dimensions rather than guessed, so they should only be changed with a reason.
"""

# --- Voxel grid ---------------------------------------------------------------
# XY size of a heightfield column.  A Skyrim humanoid's path radius is ~20-35u,
# so 16u gives sub-radius precision without exploding the grid.  Interiors are
# tight (doorways, furniture gaps) and need it.
CS = 16.0
# Exteriors are a full 4096u cell: at 16u that is a 256x256+ column grid, and the
# rasterize/region passes are O(columns).  Terrain has no doorway-scale detail,
# so a coarser grid costs nothing there and is ~4x less work.
CS_EXTERIOR = 32.0
# Z resolution of a span.  Must be well under MAX_CLIMB or stairs blur together.
CH = 8.0

# --- Agent ---------------------------------------------------------------------
# Radius to erode the walkable set by, so the mesh keeps a correct standoff from
# every wall.  This replaces the old hand-tuned EXCLUSION_MARGIN fudge.
AGENT_RADIUS = 24.0
# Required headroom.  Kills crawlspaces under stairs and low shelves.
AGENT_HEIGHT = 128.0
# Step height.  THE key constant: it decides stairs (connected) vs ledges
# (not connected), and it is what lets an NPC walk over a rug or a low sack
# instead of pathing around it.
MAX_CLIMB = 34.0

# Minimum XY-projected area (game units^2) for a triangle to be kept at all.
# A triangle standing in a near-vertical plane covers no ground, so no actor can
# stand on it however tall it is — it is a wall-hugging sliver, not a stair
# riser (a riser is steep but still has real footprint).  Deliberately TINY: this
# only removes the genuinely degenerate-in-plan case, never a real surface.
# 1.0u^2 is far below one voxel quad (CS^2 = 256u^2 at CS 16).
MIN_XY_FOOTPRINT = 1.0

# --- Surfaces -------------------------------------------------------------------
# Walkable if the surface normal is within this of straight up.  Mirrors
# asset_convert.collision_extract.MAX_SLOPE_DEG (which bakes the classification
# into the cache); kept here for the LAND terrain, which is classified live.
MAX_SLOPE_DEG = 46.0

# --- Contour / polygon ----------------------------------------------------------
# Max deviation when simplifying a region contour.  This is what turns the raw
# 16u voxel staircase into straight edges that follow the real wall.
MAX_SIMPLIFY_ERR = 12.0
# Contours shorter than this many voxels are noise (specks behind furniture).
MIN_REGION_VOXELS = 8
# Target navmesh triangle edge length (game units).  Simplification never makes
# an edge longer than this, so triangles come out roughly uniform in size rather
# than as fans of long thin slivers.  ~vanilla interior tri scale.  Scaled by the
# heightfield's cell size, so an exterior (CS 32) allows 2x longer edges.
TRI_TARGET_EDGE = 128.0
# Triangle shape bound during simplification: longest_edge^2 / (4 * area).  An
# equilateral triangle scores 0.58; slivers score high.  A collapse or a smooth
# move may not create a triangle worse than this.  (The old bound of 6 let
# decimation fill rooms with visible near-degenerate fans.)
MAX_ASPECT = 4.0
# Edge-ratio bound (longest/shortest edge) during simplification.  The aspect
# metric alone lets through "one side way shorter than the others" triangles:
# a 16u voxel edge with two ~100u edges scores aspect ~3 (its area is healthy)
# yet reads as an obvious needle radiating from a wall corner.  No move may
# create a triangle whose edges differ by more than this factor.
MAX_EDGE_RATIO = 4.0
# Simplification rounds (collapse + flip + smooth per round).  Converges fast;
# rounds after the third change little.
SIMPLIFY_PASSES = 4

# --- Pathgrid coupling -----------------------------------------------------------
# A pathgrid node associates with a walkable span within this XY distance.  The
# pathgrid is Bethesda's own annotation of where NPCs walk, so it selects which
# of the many physically-standable surfaces (floor vs tabletop vs roof) we keep.
SEED_SNAP = 64.0
# When a node's column has several spans (multi-floor), take the span whose Z is
# within this of the node's Z.  Node Z states which floor the designer meant.
SEED_Z_TOLERANCE = 96.0
# How far a walkable span may be from the pathgrid — measured as WALKED
# (geodesic) distance over the span graph, not straight-line XY — and still be
# kept (region.keep_pathgrid_heights).  The pathgrid is SPARSE: Bethesda ran a
# line down the middle of a room, not around it, and the whole point of
# voxelizing real collision is to EXPAND from that line and fill the walkable
# floor.  Geodesic distance wraps around furniture but cannot pass through
# walls, so a big reach fills the room without painting the street outside the
# shell.  (160u straight-line, the old gate, trimmed real floor in large rooms.)
PGRD_XY_REACH = 384.0
# Exterior reach.  An exterior cell is open terrain: Bethesda's own exterior
# navmeshes cover essentially the WHOLE cell, while the pathgrid is just the
# roads.  A tight reach gate carved the open ground into arbitrary blobs around
# the pathgrid — mesh missing over half a cell with no obstacle in sight — so
# outdoors the flood may reach the entire cell (geodesic walking still cannot
# climb cliffs steeper than MAX_CLIMB per step or cross water gaps, and roofs
# remain unreachable, so the wrong-surface protection is intact).
PGRD_XY_REACH_EXTERIOR = 8192.0
# Radius of the flood barrier stamped over each TELEPORT door of an interior
# cell.  The reach flood may arrive at these columns (the doorstep keeps its
# mesh, so the Door Triangle exists) but never expands from them, so the mesh
# ends at the threshold — like vanilla — instead of escaping through the open
# doorway onto the decorative street outside the shell.  Must comfortably
# exceed a doorway's half-width so the flood cannot slip around the corners.
DOOR_BARRIER_RADIUS = 64.0
# Half-width of the band stamped along every pathgrid line (voxel.stamp_pathgrid).
# This band is UNCONDITIONAL navmesh: the pathgrid is the only part of the input
# we know to be correct, so a strip of this width around every pathgrid line is
# always in the final mesh, whatever the collision says.  Nothing culls it — no
# ledge/headroom filter, no region cull, no agent erosion — and the stamp yields
# to nothing.  (Making it yield to blocking collision silently refused to stamp
# staircases, whose own faces are steep enough to be classed blocking, and left
# the storeys of a house as disconnected islands.)
#
# Sized to the agent so a staircase or a doorway comes out genuinely walkable
# rather than a sliver.
PGRD_BAND = 24.0
# How far the stamp reaches in Z to SNAP a pathgrid sample onto real walkable
# collision.  A pathgrid is coarse on stairs (the Anvil Fighters Guild runs a whole
# flight on two nodes ~100u apart in Z), so a sample interpolated along such an
# edge can float above the tread it is meant to stand on and must reach down for
# it.  But reaching too far is worse than not reaching at all: at 128u the band
# starts latching onto whatever surface happens to lie under a balcony, and the
# layer count goes UP.  A step height plus a stair riser is the right order.
PGRD_SNAP_Z = 48.0
# The UPWARD half of the snap window is MAX_CLIMB (see voxel.surface_near):
# reaching DOWN is what stairs and gullies need (a chord-paced sample floats
# above the surface that dips under it); reaching UP more than a single step
# could only latch onto something standing ON the walked surface (chest and
# counter tops hoisted the mesh onto the furniture), while less than a step
# loses a climbing cave passage and stamps the ribbon inside the hill.

# --- Door threshold quads -----------------------------------------------------------
# Every door REFR (teleport or interior) gets an exact oriented quad stamped
# into the mesh at its threshold: the two triangles the Door Triangle link can
# land on.  Half-extent along the door's width (local X) and depth (local Y,
# the walk-through direction).  96u total width stays inside a standard ~110u
# Oblivion doorway so the quad never pokes into the jambs; 64u total depth
# straddles the threshold line the way vanilla door triangles do.
DOOR_QUAD_HALF_WIDTH = 48.0
DOOR_QUAD_HALF_DEPTH = 32.0
# Z window for claiming mesh vertices into the quad — a door only restructures
# the floor it stands on, never a storey above/below.
DOOR_QUAD_ZTOL = 128.0

# --- Boundary cleanup ---------------------------------------------------------------
# A triangle on the outline with at most one neighbour (a protruding flap/ear)
# is deleted when smaller than this (game units^2, interior scale; scales with
# the voxel size squared).  These flaps are voxel-quantization noise at wall
# corners — too small to route through, ugly, and they read as "small triangles
# around corners" in-game.  192 = 1.5 voxel-scale triangles at CS 16.
# Removal is inherently safe: a triangle with <=1 neighbour cannot be a bridge,
# so deleting it can never disconnect the mesh.  Triangles near the pathgrid
# or a door threshold are exempt.
EAR_MIN_AREA = 192.0
# A flap is exempt when any densified pathgrid sample lies within this XY
# distance of it.  Containment-only exemption still let the cull eat ribbon
# ends and narrow cave ledges the pathgrid walks (2 wrong-floor nodes in
# XPGloomstonePassage02); a distance buffer protects the walked line and its
# fringe while still cleaning wall corners elsewhere.
EAR_PGRD_RADIUS = 64.0
# Cull rounds.  Each round exposes new boundary edges; more rounds chain-eat
# the outline (a cave boundary is legitimately jagged).
EAR_ROUNDS = 2

# --- Island pruning ---------------------------------------------------------------
# A disconnected component smaller than this is noise (a scrap behind a shelf, a
# ribbon fragment on a wall top) unless a door anchors it.  NPCs cannot use a
# 1-4 triangle island for anything.
MIN_ISLAND_TRIS = 5
# A component counts as door-anchored when a mesh vertex lies within this XY
# distance of a teleport-door REFR (and within door Z tolerance).  The doorstep
# strip in front of a door must always survive so its Door Triangle exists.
ISLAND_DOOR_RADIUS = 150.0
# Z window for the door-anchor test: a door's PosZ sits at its threshold, so the
# doorstep mesh is within a step or two of it.  Wide enough for tall thresholds,
# narrow enough that a door on a balcony never anchors the floor below it.
ISLAND_DOOR_ZTOL = 128.0
# Exterior: a component that comes within this of the cell border "runs over
# into the next cell" and is kept — its continuation lives in the neighbour
# cell's navmesh.  ~1.5 exterior cells.
ISLAND_EDGE_MARGIN = 48.0
# A component with a mesh vertex within this of a PATHGRID sample carries the
# walked line and is never dropped as an island — the pathgrid is the one input
# that asserts an actor walks there.  Sized to the ribbon half-width so a
# ribbon's own vertices always qualify.
ISLAND_PGRD_RADIUS = 48.0

# --- Island bridging (drop-downs) -------------------------------------------------
# Oblivion had no pathgrid edge for a DROP: a balcony and the floor below it are
# two disconnected pathgrid islands, and the actor simply steps off.  The
# navmesh reproduces the pathgrid faithfully, islands included, so those two
# storeys arrive as separate components and an NPC pathing between them has no
# route -- it walks into the wall/door and stops.  (CharacterGen's Ambush A: the
# assassins' holding cell teleports onto a mezzanine they are meant to DROP from
# into the ambush room; mezzanine and room floor were separate components, so
# CGAssassinsAmbushA4 could never complete and the ambush never fired.)
#
# The fix bridges two components that all but touch in plan but are separated in
# Z.  Both sides must ALREADY be separate components, so genuinely-connected
# storeys (stairs, ramps) are never candidates -- they are one component and
# never enter this pass.
#
# Horizontal reach, the drop window, and the bridge constants themselves are
# defined AFTER RIBBON_HALF_WIDTH (below), because the reach is derived from
# the ribbon width rather than hand-fitted.

# --- Corridor ribbons (Phase 1, corridor.py) --------------------------------------
# Half-width of the flat ribbon laid down each pathgrid edge.  ~80u total sits
# inside a standard ~110u Oblivion doorway with clearance for the jambs; wide
# enough for a Skyrim NPC's path radius.  Phase 2 will grow this out to walls.
RIBBON_HALF_WIDTH = 40.0

# --- Island bridging reach (see the ISLAND_BRIDGE block above) --------------------
# Matched between BOUNDARY EDGE PAIRS, not single vertices, so the threshold has
# to cover the length of a ribbon edge: two components can sit directly above one
# another and still have their nearest edge ENDPOINTS a full edge apart, purely
# because of where the triangulation put vertices.
#
# One ribbon WIDTH, not a hand-fitted constant.  A lip that overhangs the floor
# below is within a corridor's width of it by construction, while "across the
# room" is many widths away.  An earlier value fitted to a single measured cell
# (33u) silently stopped bridging the moment the door-quad fix reshaped that
# cell's mezzanine to 58u -- which is exactly why this is derived from the
# ribbon geometry instead.
ISLAND_BRIDGE_XY = 2.0 * RIBBON_HALF_WIDTH
# Vertical drop the bridge may span.  A one-storey fall; Skyrim NPCs take this
# routinely and Oblivion's design relies on it (the Ambush A mezzanine is 192u
# above the room floor).  More than this would weld a tower's storeys together.
ISLAND_BRIDGE_MAX_DROP = 220.0
# Below MAX_CLIMB the two sides are a step apart, not a storey -- ordinary
# adjacency, not a drop, and not this pass's business.
ISLAND_BRIDGE_MIN_DROP = MAX_CLIMB

# Spacing of cross-sections along an edge, so a long edge is several quads and
# the ribbon can follow the pathgrid line's slope in Z rather than one flat
# quad bridging the whole span.
#
# This ALSO sets how finely the overlap cut is resolved.  A quad between two
# cross-sections is a straight sheet, but the boundary between two corridors'
# owned ground bends at every junction, so a long quad paints outside the region
# it owns.  Measured on AnvilFightersGuild (coverage / double-covered ground):
#   32u -> 85.7% / 14.9%     16u -> 94.7% / 6.0%     8u -> 97.8% / 4.1%
# 8u costs more triangles but is the difference between a corridor network that
# is visibly stacked and one that is not.
RIBBON_STEP = 8.0
# How close a door-quad corner must be to a ribbon vertex to weld onto it
# (share the index, hence a shared edge -> adjacency).  Half a ribbon width:
# tight enough not to fuse across a real gap, wide enough to catch the strip.
RIBBON_WELD_EPS = 24.0
# A dead-end pathgrid node (degree 1) has its ribbon extended this far past the
# node along the edge direction: the pathgrid ends before the room does, so a
# corridor that stopped at the last node would leave a gap in front of the wall
# or door it was heading for.  ~one ribbon half-width reaches the threshold.
RIBBON_END_EXTEND = 40.0
# A STEEP (stair/ramp) ribbon extends this far past BOTH its end nodes, junction
# or not.  A steep edge is never width-grown, so it keeps the narrow Phase-1
# width while the flat landing it meets grows to 100u+; the stair mouth is then
# much narrower than the landing and the two meet only at the landing's CORNER
# vertices.  At the top of Pinarus's stairs that left the whole descent routed
# through two 27-degree wedges (one of edge ratio 6.1) hanging off those corners
# — one connected component, but not walkable.  Extending the flight onto the
# flat at each end gives the boolean union a real overlap, so the mouth becomes a
# span of shared edges.  Kept modest: the extension carries the LINE's slope, so
# too much would push ramp mesh out across the landing floor.
RIBBON_STAIR_END_EXTEND = 48.0
# Half-width of a STEEP ribbon.  Wider than RIBBON_HALF_WIDTH because a flight
# has to present a mouth comparable to the landing it joins, and because a
# staircase in Oblivion is a full-width architectural feature, not a corridor.
# Still well inside a standard flight so the ribbon does not overhang the
# stringers. Only used for edges the width-grow refuses (see RIBBON_GROW_MAX_SLOPE).
RIBBON_STAIR_HALF_WIDTH = 64.0

# --- Corridor width-grow (Phase 2, corridor.py) -----------------------------------
# Phase 2 replaces the fixed RIBBON_HALF_WIDTH with a per-cross-section, per-side
# grown half-width: an actor-sized box marches outward perpendicular to the
# centerline (in the centerline's own flat plane, principle 2) until it either
# (a) hits blocking collision, (b) reaches the midpoint toward the nearest OTHER
# pathgrid edge's centerline (so two parallel corridors meet cleanly instead of
# overrunning), or (c) hits the hard cap.  Overlap that results is resolved by
# the boolean union (corridor_union), exactly like the fixed-width overlap.
#
# Enable/disable the grow.  When False, corridor.py lays the Phase-1 fixed-width
# rectangle (RIBBON_HALF_WIDTH) — kept as a fallback and for A/B testing.
RIBBON_GROW = True
# Step size of the outward march (game units).  Finer = tighter fit to a wall at
# more cost.  Half the agent radius resolves a doorway jamb without over-sampling.
RIBBON_GROW_STEP = 8.0

# The probe is a THIN vertical slab, not an agent-radius box: actor HEIGHT and
# ~actor WIDTH along the edge tangent, but only a sliver DEEP along the march
# direction.  A fat box stops an agent-radius short of every wall, which
# narrowed doorways by 24u a side — the navmesh must instead come right up to
# the collision.  Depth is a sliver so "hit" means the slab genuinely touches
# the wall at this step, not that a wall is somewhere nearby.
RIBBON_GROW_SLAB_HALF_WIDTH = 20.0
# Depth is a sliver so "hit" means the slab genuinely touches collision at this
# step, not that something is vaguely nearby — but not razor-thin: a few units
# of depth give the ribbon a small standoff from furniture instead of pressing
# right against the bed frames, and cost nothing at a wall.
RIBBON_GROW_SLAB_DEPTH = 6.0
# The slab starts above the floor so collision the actor simply STEPS ONTO is
# not read as a wall, and rises to AGENT_HEIGHT so anything the actor's body
# would hit stops the growth.  The floor is MAX_CLIMB: a step, curb, or stair
# tread whose top is within one climb of the floor is walkable, so its riser
# face must be ignored — otherwise a 20u curb across a passable route (the Anvil
# main-gate ramp, the center-circle steps) walls the corridor shut and splits
# the navmesh into disconnected islands.  A real wall extends far above
# MAX_CLIMB and is still caught by the band above it.
RIBBON_GROW_SLAB_Z_BOTTOM = MAX_CLIMB
# Bisection rounds used to place the stop exactly at the wall once the swept
# step has detected one.  4 rounds resolve an 8u step to 0.5u.
RIBBON_GROW_BISECT = 4
# Two edges count as opposing parallel corridors only when their directions
# agree to at least this |dot|.  A crossing/diverging edge is NOT a corridor
# wall and must not cap width (that pinched every dense junction).
RIBBON_GROW_PARALLEL_DOT = 0.70
# An edge steeper than this (rise/run) is a STAIRCASE or ramp and is NOT grown:
# its ribbon is a tilted plane, so a perpendicular rail leaves the treads at
# once — off the side of the flight, or through the stairwell wall.  Stairs keep
# the Phase-1 width, which is what the pathgrid asserts.  0.20 ~ 11 degrees:
# well above a floor's slop, well below any real flight.
RIBBON_GROW_MAX_SLOPE = 0.20
# Douglas-Peucker tolerance applied to each grown RAIL before it becomes the
# ribbon outline.  The march samples a width every RIBBON_STEP, and the
# triangulator FORCES every outline corner as a Steiner point — an un-simplified
# rail therefore seeds a vertex every 8u and fills rooms with sliver fans.  At
# 12u the rail still hugs a wall it followed, but a straight run collapses to
# two points and the hex lattice governs the interior.
RIBBON_RAIL_SIMPLIFY = 12.0
# Rays in the radial fan grown around each pathgrid NODE.  Ribbons grow only
# perpendicular to their own edge, so the outer corner where two edges meet at
# an angle is a notch no ribbon reaches (a right-angle junction leaves a square
# bite out of the mesh).  16 rays = one every 22.5 deg, enough to resolve a
# square corner without a fan of near-duplicate boundary points.
RIBBON_GROW_DISC_RAYS = 16

# --- Decimation (corridor_clean.decimate) ------------------------------------
# Collapse edges shorter than this, turning the needle fans that outline corners
# breed into near-equilateral triangles.  A collapse is only taken when it keeps
# the outline, flips nothing, and does not worsen the local edge ratio, so this
# can only improve shape and never changes coverage.  ~0.4 * TRI_TARGET_EDGE:
# short enough that a real feature edge survives, long enough to eat the slivers.
DECIMATE_MIN_EDGE = 0.0
# Passes.  Each round re-derives the boundary and re-sorts candidates; the mesh
# converges in a couple of rounds.
DECIMATE_ROUNDS = 3
# How far the OUTLINE may move when a boundary vertex is decimated away: the
# vertex's distance from the chord between its two boundary neighbours.  The
# outline is the wall standoff, so this is deliberately small — straight runs of
# boundary samples collapse freely, a real corner never moves and the mesh
# cannot cut through a wall.  Freezing the outline entirely (tol 0) left 17-21%
# sliver triangles; this recovers the decimation without the corner-cutting.
DECIMATE_OUTLINE_TOL = 6.0
# Vertices within this of a door threshold are PINNED — never collapsed.  A
# decimated door corner destroys the Door Triangle and the doorway goes dead in
# the engine (no cross-cell/room pathing through it).  Comfortably covers the
# door quad (DOOR_QUAD_HALF_WIDTH 48 / HALF_DEPTH 32).
DECIMATE_PIN_RADIUS = 80.0
# Lower bound: a rail never grows NARROWER than this half-width even if a wall or
# a neighbour centerline is closer, so a corridor squeezed between two close
# obstacles still carries a walkable strip (the Phase-1 width was unconditional).
RIBBON_GROW_MIN_HALF = 16.0
# Hard cap on grown half-width.  A corridor in open space (no wall, no neighbour)
# stops here rather than ballooning across a whole exterior cell.  ~1.5 doorways;
# wide enough for room coverage, bounded enough that a doorway leak is a nub.
RIBBON_GROW_MAX_HALF = 160.0
# When measuring the distance to the nearest OTHER edge's centerline, ignore
# edges that share a node with this one (they meet AT the junction, they are not
# an opposing wall of corridor) and edges whose centerline Z is more than this
# from the trial point's Z (a corridor on the storey above must not stop growth
# on the floor below).
RIBBON_GROW_NEIGHBOUR_ZTOL = 96.0

# --- Limits ----------------------------------------------------------------------
# Hard cap on grid dimension per cell; beyond this CS is coarsened.  Guards
# memory on huge exterior cells.
MAX_GRID_DIM = 512
# Per-cell wall-clock budget.  On overrun the cell is abandoned (the caller
# falls back), so one pathological cell can never stall the whole run.
CELL_TIME_BUDGET = 20.0
