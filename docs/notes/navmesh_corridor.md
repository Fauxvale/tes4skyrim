# Navmesh redesign: pathgrid corridor ribbons

> **Status: IMPLEMENTED and live on `master`** (design approved 2026-07-23;
> status corrected 2026-07-26 — this header previously read "design, not yet
> implemented"). `build.py::build_navmesh` keeps its historical signature and
> **delegates to `corridor.build_corridors`**. The corridor modules are
> `corridor.py`, `corridor_clean.py`, `corridor_doors.py`, `corridor_grow.py`,
> `corridor_union.py`, plus `params.py` and `world.py`.
>
> The superseded voxel/span-graph generator is **DELETED** from `master`:
> `voxel.py`, `region.py`, `spanmesh.py` and `native/src/decimate.cpp` no longer
> exist. The Recast-era generator remains on branch **`test-navmesh-2`**.
> Performance work on the corridor path is recorded in
> [performance_notes.md](performance.md); geometry is verified by
> `tools/navmesh/check.py`, `navmesh_reach.py`, `navmesh_slope_check.py`.
>
> Read the rest of this document as the design rationale for what was built.

## Baseline before the rewrite (historical — verified 2026-07-23)

The pre-corridor `master` was **not** the Recast pipeline — it was a **voxel /
span-graph** generator: `voxel.py` (heightfield + `stamp_pathgrid` + filters +
erosion), `region.py` (region flood + pathgrid seeding), `spanmesh.py` (mesh the
span graph directly). **All three are now deleted.** `build_navmesh`'s signature
was, and still is:

```
build_navmesh(refr_recs, base_model_by_fid, get_collision, nodes, edges,
              land_rec=None, origin_x=0.0, origin_y=0.0, budget=None, doors=None)
    -> (verts, tris)   # world-space; [] , [] on failure
```

(`budget` is now accepted only for signature compatibility — the corridor build
has no budget knob.)

There was **no `door_carve.py`** — doors were stamped into the voxel grid and
passed to `spanmesh.build_mesh(doors=door_rects)`. The Recast-era `door_carve.py`
(shapely cut-and-earcut) lives on `test-navmesh-2`; the corridor model handles
doors in `corridor_doors.py`.

That voxel pipeline was cleaner than the Recast one (pathgrid stamped first,
span-graph meshing so adjacency is structural), but still heavy: voxel grid,
filters, region flood, erosion, span meshing, steep-tri drop, flap cull, island
prune. The corridor model replaced the whole surface generator with a direct
ribbon build.

---

## Why replace it

The pathgrid is already the "an actor walks here" graph. Every voxel/Recast
generator spends its complexity RE-DISCOVERING walkable surface from collision
and then fighting to keep the mesh connected across the seams that discovery
introduces (the Recast version needed ~900 lines of weld/stitch/clip to undo
its own per-sheet fragmentation; the voxel version needs region flood +
seeding + geodesic pathgrid-reach culling to keep the pathgrid's surface and
throw away the ceiling a staircase flood-merged into).

The corridor model builds the mesh **directly on the pathgrid**, so:
- connectivity is structural (edges meeting at a node share the node vertex);
- there is no surface to re-discover, so no filters/flood/erosion;
- the result is exactly what the pathgrid asserts and nothing more.

It removes the problem at the source rather than repairing it downstream.

### The core idea

The pathgrid **is** the "an actor walks here" graph. Build the navmesh directly
on it:

> Emit a fixed-width ribbon of triangles centred on every pathgrid edge. Edges
> that meet at a shared node **share that node's vertices by construction**, so
> triangle adjacency links automatically. No independent sheets, so nothing to
> weld or stitch.

Connectivity becomes a property of the construction, not a post-process. The
entire 900-line stitch/clip/dedup/manifold apparatus is deleted.

The trade the author accepted explicitly: **a completely functional navmesh
with zero bad triangles, even if it is a bit sparse, beats a dense but broken
one.** Sparse-but-correct is the Phase 1 target.

---

## Author-set principles (do not violate)

These came from direct decisions on 2026-07-23. They constrain every phase.

1. **The pathgrid centerline is sacred.** The pathgrid asserts an actor walks
   the line; we trust it. We never cut, clip, or move the centerline — not even
   where it clips a wall (Oblivion authors cut corners constantly). Only *grown
   width* may ever be clipped (Phase 2+), never the ribbon spine.

2. **Downward snap follows the pathgrid line's own slope — it is NOT a per-tread
   re-fit.** A pathgrid edge already has a slope: node A at `z_a`, node B at
   `z_b`. That straight line **is** the walk ramp. A staircase comes out as one
   clean ramp because the Oblivion nodes are placed at tread level and the A→B
   line is already the ramp. "Snap down" means: sit the ribbon on that line, and
   only push a cross-section *down* onto walkable collision when the line floats
   above it — never let jagged tread collision push samples up and reintroduce a
   sawtooth. A slope stays a slope. (This is the single biggest simplification
   over the current `EDGE_SEG_TOL`/`STAIR_TRACK_TOL` per-sample piecewise fit.)

3. **Be conservative; stop when unsure.** Doorways are *assumed* to already have
   pathgrid running through them, so lateral growth never has to "find" a
   doorway — it only has to avoid leaking through one. When growth is uncertain,
   stop. We can always widen later. A missing sliver of floor is recoverable; a
   through-wall triangle is a bug.

4. **Never put navmesh on the wrong side of a wall.** The current code "often
   puts navmesh on the other side of walls." The corridor model must not
   reproduce this. Because the centerline is sacred (principle 1), through-wall
   mesh can only arise from *grown width* leaking across a wall — so all wall
   handling lives in the width-grow phase, and defaults to stopping early.

5. **Phase it. Phase 1 is corridors + doors + links, and must be completely
   right before any width-grow or polish is added.** A navmesh with a perfect
   surface but no door links and no cell links is DEAD in the engine — an actor
   cannot cross a doorway or a cell boundary. So Phase 1 is not "surface only";
   it is "a *complete, functional* navmesh, just narrow." Door carve and the
   link passes are in scope for Phase 1 (author, 2026-07-23).

---

## What stays exactly as-is

The corridor generator replaces the surface generator inside `build_navmesh`.
The record packing and the link passes are downstream, mesh-agnostic, and
already verified byte-exact — they are REUSED, not rewritten. Phase 1's job is
to feed them a mesh that presents the anchors they need.

| Component | Role | Change |
|---|---|---|
| `world.gather_cell_geometry` | REFR + LAND collision → `walkable`/`blocking` (N,3,3) soups | **none** — Phase 1 uses `walkable` for the downward snap; Phase 2 uses `blocking` for lateral stop |
| `pgrd_to_navm.convert_PGRD` | reads PGRD, builds NVNM/NAVM bytes, water flags, ONAM, calls `_build_door_links` | **none** — still calls `build_navmesh(...)` → `(verts3d, tris)` and links doors on the result |
| `pgrd_to_navm._compute_adjacency` | writes the NVNM neighbour fields the engine walks | **none** — the corridor mesh MUST satisfy the same manifold rule (≤2 tris/edge) |
| `pgrd_to_navm._build_door_links` | finds the tri CONTAINING each door threshold; falls back to nearest-on-threshold-line | **none** — but Phase 1's door carve must guarantee a triangle actually sits under each door, else this silently falls back or drops the link |
| `navm_edge_links.build_edge_links` | reciprocal Portal links across exterior cell seams; needs border edges near the seam plane | **none** — decodes NVNM bytes and matches border edges; works on ANY mesh. Phase 1 must ensure ribbons reach the cell boundary so border edges exist there |
| `navi_builder` NAVI singleton + NVMI mirror | registers every mesh engine-wide (no NAVI ⇒ zero pathfinding anywhere) + mirrors door/edge links | **none** |
| geometry cache (`_geom_hash`, `_GEOM_BUILD_VERSION`) | disk cache keyed on inputs | bump `_GEOM_BUILD_VERSION`; the corridor build is a new pipeline |

The **contract** `build_navmesh` must keep: return `(verts3d, tris)`, a list of
`(x,y,z)` float tuples and a list of `(i,j,k)` int tuples, forming a
**manifold** mesh (every edge shared by ≤2 triangles — a 3+ edge silently
disconnects everything around it under `_compute_adjacency`).

### The two link systems, and what the corridor mesh owes each

**Door links** (interior passages AND cross-cell teleport doors). Built in
`pgrd_to_navm._build_door_links(verts, tris, doors)`: for each door it finds the
triangle whose 2D footprint CONTAINS the (pivot-corrected) threshold point at
the door's storey Z; failing that, the nearest triangle centred on the threshold
line within `DOOR_LINK_MAX_DIST`. That triangle is flagged `_TRI_FLAG_DOOR` and
emitted as a Door Triangle, and its ref FormID goes into the NVMI door mirror.
**What the corridor mesh owes it:** a well-shaped, connected triangle sitting
exactly on each door threshold. In the sparse ribbon model this only happens for
free if a pathgrid edge runs through the door — and even then the pivot→panel
offset can nudge the threshold just off the ribbon. So **Phase 1 includes a door
carve** (below) whose whole job is to place that triangle and connect it to the
corridor mass.

**Cell links** (exterior cross-cell Portals). Built in
`navm_edge_links.build_edge_links` as a post-pass over the whole navmesh cache:
it finds border edges (neighbour field −1) lying within `SEAM_BAND` of a shared
cell-boundary plane and pairs them reciprocally across the seam. **What the
corridor mesh owes it:** ribbon triangles with border edges at the cell boundary
plane. An exterior pathgrid edge that crosses (or ends at) the cell boundary
produces exactly such border edges — so this is satisfied by construction as
long as the ribbon is emitted out to the node, and no clamp pulls it inside the
seam band. Phase 1 verifies this; it writes no new code for cell links.

---

## Phase 1 — corridors + doors + links (a complete, narrow navmesh)

**Goal:** for every cell, a connected, manifold, zero-bad-triangle ribbon mesh
following the pathgrid graph, sitting on walkable collision, with a Door
Triangle under every door and border edges at cell seams so the existing door-
link and cell-link passes produce a fully functional (if narrow) navmesh.

### Inputs (already available inside `build_navmesh`)
- `nodes`: pathgrid nodes `[(x,y,z), ...]` (world coords: cell-local interior,
  world exterior — same frame as collision).
- `edges`: `[(i,j), ...]` node-index pairs.
- `walkable`: `(N,3,3)` float array of walkable collision (floors, treads,
  terrain), from `gather_cell_geometry`.
- `doors`: `[(x, y, z, rot_z, is_teleport), ...]` pivot-corrected door centres
  (already assembled by `pgrd_to_navm._collect_doors` and passed through).

### Algorithm

**Step 0 — walkable surface sampler.**
Reuse the existing `_walkable_surface_sampler(walkable)` from `build.py`
verbatim (it is already independent of the rest). It returns
`sample(x, y, near_z) -> z | None`: the walkable-collision height at `(x,y)`
nearest `near_z`, bucketed to a coarse XY grid. This is the only collision query
Phase 1 needs.

**Step 1 — a vertex per node.**
For each pathgrid node `i`, its ribbon spine point is the node XY at the node's
own Z, snapped down onto walkable collision:

```
z_i = snap_down(node_i.x, node_i.y, node_i.z)
```

where `snap_down(x, y, z)`:
- `s = sample(x, y, z)`
- if `s is None`: keep `z` (no collision known here — trust the pathgrid; a
  missing sample must never delete the spine, principle 1).
- else if `s <= z + SEED_SNAP_UP` and `s >= z - SEED_SNAP_DOWN`: use `s`
  (the surface is within the plausible window; sit on it).
- else if `s < z`: the surface is far below (node floats over a pit/upper
  storey) — clamp the drop to `z - SEED_SNAP_DOWN` rather than teleporting to a
  distant floor. **Conservative.**
- else (`s > z + SEED_SNAP_UP`): surface is above the node (an object sitting on
  the floor, or the node is under geometry) — keep `z`, do **not** rise onto it.

Reuse `SEED_SNAP_DOWN` (96) and `SEED_SNAP_UP` (=MAX_CLIMB, 34) from `params`.

**Step 2 — ribbon each edge, following the line's slope.**
For edge `(i, j)` with snapped endpoints `A=(ax,ay,az)`, `B=(bx,by,bz)`:

- Width direction `w = normalize(perp(B-A in XY))`; half-width `HALF`
  (Phase 1 constant, below).
- Densify the edge into `k = max(1, round(len_xy(A,B) / RIBBON_STEP))` segments
  so a long edge is several quads (needed so the ribbon can *follow* a curved
  or bumpy floor in Z; a single quad would bridge straight over dips).
- For each cross-section parameter `t` in `{0, 1/k, ..., 1}`:
  - centre `C(t) = lerp(A, B, t)` — **Z comes from the straight A→B line**, not
    re-sampled per cross-section (principle 2: the line's slope is the ramp).
  - left `L(t) = C(t) + HALF * w`, right `R(t) = C(t) - HALF * w`, **both at
    `C(t).z`** — the corridor is FLAT across its width (author decision
    2026-07-23: "just keep the corridors of navmesh flat"). No per-rail snap.
    The whole cross-section lies on the centerline plane, so a rail can never
    drape down a ledge and no side-collision query is needed in Phase 1.
- Emit two triangles per segment (quad `L(t),R(t),R(t+1),L(t+1)`), CCW.

**Step 3 — shared vertices at nodes = free connectivity.**
Key detail that makes the whole model work: **the two cross-section vertices at
a node are minted ONCE per node and reused by every edge incident to that node.**
Maintain `node_ribbon_verts[i]` — but a node has one spine point and *many*
incident edges leaving at different angles, so the left/right rails of different
edges do **not** coincide. Two options, decide in Open Question B:

- **B1 (Phase 1 default — simplest, guaranteed manifold):** every edge is an
  independent quad strip that shares **only the single spine vertex** at each
  node (mint one shared vertex per node at `(node.x, node.y, z_i)`, and have
  every incident edge's strip include a triangle fan back to it). Ribbons then
  overlap slightly at junctions but always share the node vertex, so adjacency
  links through the node. Overlap at a junction is coplanar and small; the
  manifold pass (Step 4) resolves any 3+-shared edge.
- **B2 (nicer, more work — deferred):** compute a proper junction polygon at
  each node (miter the incident ribbons) so rails meet cleanly. This is
  Phase 2+ polish, not Phase 1.

Phase 1 uses **B1**: correctness first, junction beauty later.

**Step 4 — door carve (connect every door to the corridor mass).**
A door with no triangle under its threshold gets no Door Triangle, so the engine
cannot path through it — the mesh is dead at that doorway. Because the pathgrid
is assumed to run through every doorway (principle 3), a ribbon usually already
passes near each door; the carve's job is to guarantee a well-shaped triangle
sits *exactly* on the (pivot-corrected) threshold and is *connected* to the
ribbon. The ribbon model makes this far simpler than the shapely cut-and-earcut
`door_carve.py` on `test-navmesh-2`:

For each door `(dx, dy, dz, rz, is_tp)`:
1. **Find the storey Z** = the ribbon Z nearest `dz` within `DOOR_QUAD_ZTOL`
   (the door REFR z only picks the storey). If no ribbon triangle is within
   `DOOR_BRIDGE_RADIUS` of `(dx,dy)` at that storey, the door is genuinely walled
   off from the pathgrid — skip it (conservative; do not invent a floating
   patch).
2. **Stamp a small threshold quad** on the door line: an oriented rect centred at
   `(dx,dy,storey_z)`, width `2·DOOR_QUAD_HALF_WIDTH` along the door axis, depth
   `2·DOOR_QUAD_HALF_DEPTH` across it, flat at `storey_z`. Two triangles. Its long
   edge lies ON the door line — exactly what `_build_door_links` wants to flag.
3. **Connect it to the ribbon** by welding the quad's corners to the nearest
   ribbon vertices within a small weld epsilon, and — where a quad corner lands
   in a ribbon triangle's interior rather than on a vertex — splitting that
   ribbon edge so both sides share indices (a minimal, LOCAL T-junction split, not
   the general stitch machinery). If the quad and the ribbon overlap, drop the
   quad triangles that fall inside the ribbon and keep only the part that extends
   coverage to the threshold. The manifold pass (Step 5) cleans any residue.
4. Interior doors: done. Teleport doors: same, and Phase 1 does NOT clip the far
   side (deferred — see Phase 3). The ribbon simply ends where the pathgrid ends.

This is a self-contained `corridor_doors.py` (or a function in the new build
module), NOT the `test-navmesh-2` `door_carve.py`. It reuses `DOOR_QUAD_*` and
`DOOR_BRIDGE_RADIUS`-style constants from `params`.

**Step 5 — make manifold + drop degenerate.**
Run the existing `_make_manifold` and `_drop_degenerate` (generic, no sheet
assumptions). This guarantees the ≤2-tris-per-edge invariant
`_compute_adjacency` requires. Nothing else — no welding of the ribbon body
(vertices are already shared by construction), no stitching, no clipping.

**Step 6 — return `(verts, tris)`.** `pgrd_to_navm.convert_PGRD` then runs
`_build_door_links` (finds the Door Triangle we stamped) and packs the NVNM;
`navm_edge_links` + `navi_builder` run as post-passes over the whole cache.

### Phase 1 parameters (new, in `params.py`)
```
RIBBON_HALF_WIDTH = 40.0     # half of ~door width (80u), fits Oblivion ~110u doors
RIBBON_STEP       = 32.0     # cross-section spacing along an edge (follow Z)
RIBBON_WELD_EPS   = 8.0      # weld door-quad corners to nearby ribbon vertices
```
Reuse `SEED_SNAP_DOWN`, `SEED_SNAP_UP`, `MAX_CLIMB`, `MIN_XY_FOOTPRINT`,
`DOOR_QUAD_HALF_WIDTH`, `DOOR_QUAD_HALF_DEPTH`, `DOOR_QUAD_ZTOL`.

### What Phase 1 deliberately does NOT do
- No lateral width-grow (fixed `RIBBON_HALF_WIDTH`) — Phase 2.
- No `blocking`/wall collision use at all. It cannot leak through a wall because
  it never grows into one; it CAN still ribbon *along* a wall-hugging pathgrid
  line — accepted (principle 1).
- No teleport-door far-side clipping (`_interior_sign`) — Phase 3.
- No junction mitering (Open Question B2) — Phase 2+.
- Likely no unreachable-cull / sliver-prune: the corridor mesh has no stray
  scraps to cull. Leave them out; add back only if real output needs it (Q C).
- No exterior special-casing beyond the terrain already in `walkable`.

### Phase 1 acceptance (get it *completely* right)
A cell is done only when it is a *complete, functional* navmesh — surface AND
links. Verify on the canonical problem cells:
- **Pinarus' house (interior, stairs + upper floor + door):** one connected
  component; staircase is a single clean ramp (not a sawtooth); upstairs
  reachable from downstairs; the exterior door has a Door Triangle and
  `_build_door_links` attaches it. `tools/navmesh/reach.py` shows the quest
  start→goal reachable *through* the door.
- **A cave interior:** floor followed in Z, no bad triangles.
- **An exterior grid cell with terrain + a road pathgrid:** ribbon follows the
  road, sits on LAND terrain, and `navm_edge_links` reports Portals created at
  the shared seams with its neighbours (border edges present at the boundary
  plane).
- **A house with a load door, both sides:** the interior mesh and the exterior
  mesh each carry the door's Door Triangle, and the NVMI door mirror lists the
  same ref both sides (the vanilla rule already in `convert_PGRD`).
- **Global invariants (all cells):** zero degenerate/zero-area triangles; every
  edge shared by ≤2 triangles (manifold); `_components` count equals the pathgrid
  connected-component count (no splits, no false merges); every door with a
  pathgrid edge through it gets a Door Triangle; byte-reproducible
  (`tools/esm/esm_diff.py`).

Tools: `tools/navmesh/probe.py`, `tools/navmesh/reach.py`, `tools/navmesh/check.py`
(validate against Skyrim.esm first — it has known findings, don't chase those).

---

## Phase 2 — grow width to walls (deferred, sketch only)

Once Phase 1 is solid: replace the fixed `RIBBON_HALF_WIDTH` with a per
cross-section width that grows outward until it *conservatively* hits a wall.

- Use `blocking` collision. Grow each rail outward in steps; stop the rail when
  the vertical column from the ribbon floor up to `AGENT_HEIGHT` at the trial
  point intersects `blocking`, **or** the walkable surface under the trial point
  departs from the centerline Z by more than `MAX_CLIMB`, **or** a hard
  `RIBBON_MAX_HALF_WIDTH` cap (~128–192u) is reached.
- **The centerline never moves** (principle 1). Only rails grow.
- **Conservative stop** (principle 3): if a growth step is ambiguous (sample
  returns `None`, or the column is marginal), stop there. Under-growing is fine.
- The max-width cap means even a doorway leak becomes a small nub reaching into
  the next room, never a whole extra floor — the specific failure the author
  flagged. Combined with "doorways already have pathgrid through them," growth
  rarely needs to reach a doorway at all.

This is where wall-side correctness is won or lost; it gets its own design pass
and its own acceptance run before it ships.

## Phase 3 — polish (deferred, sketch only)

- Teleport-door far-side clipping (port `_interior_sign` from `test-navmesh-2`'s
  `door_carve.py`) so a teleport door does not trail ribbon into the decorative
  geometry beyond the cell shell.
- Junction mitering (Open Question B2) for cleaner intersections.
- Wider door thresholds / better-shaped Door Triangles if the stamped quad reads
  as too small in-game.

---

## Decisions made (author) and open questions

Resolved 2026-07-23:
- **Rails are FLAT** on the centerline plane (Step 2). No per-rail snap. Closed.
- **Junctions use B1** (shared spine vertex). Mitering deferred to Phase 2+.
- **Door carve + door links + cell links are IN Phase 1.** A navmesh without
  them is dead in-engine.
- **Work on `master`;** the Recast generator is preserved on `test-navmesh-2`.

Still open, to resolve during the Phase 1 build:
- **C. Do we need any island cull / sliver prune at all?** Hypothesis: no — the
  corridor mesh has no stray scraps. Leave them out; add back only if output
  demands. The pathgrid-component-count invariant (acceptance) will catch a
  regression.
- **D. Door-quad → ribbon connection robustness.** Step 4's weld+split must not
  create a non-manifold edge or an island threshold. Validate the Door Triangle
  is in the SAME component as the ribbon it serves (not just spatially near it) —
  reuse `_components` to assert it during the acceptance run.
- **E. `_GEOM_BUILD_VERSION` bump** and the geometry cache key: the corridor
  build consumes the same inputs (`points`, `edges`, refrs, land), so the
  existing `_geom_hash` covers it; just bump the version constant so old cached
  meshes self-invalidate.

---

## Risk register

| Risk | Mitigation |
|---|---|
| Sparse mesh: NPCs path single-file, don't use room area | Accepted for Phase 1 (author). Phase 2 width-grow restores room coverage. |
| Pathgrid edge clips a wall → ribbon straddles wall | Accepted (principle 1); the fixed narrow width limits how far it protrudes. Phase 2 must not *widen* it through the wall. |
| Junction overlap creates non-manifold edges | `_make_manifold` (Step 5) resolves; keep the largest tris. |
| Node floats far above the floor (pit/upper storey) | `snap_down` clamps the drop to `SEED_SNAP_DOWN`; never teleports to a distant surface. |
| Door with no pathgrid edge through it → no Door Triangle → dead doorway | Step 4 skips only genuinely walled-off doors; author asserts doorways have pathgrid. Acceptance counts doors that got a Door Triangle vs. total; a shortfall is a real bug to chase. |
| Exterior sparse pathgrid → spiderweb over open terrain | Accepted for Phase 1; Phase 2 width-grow + terrain already in `walkable`. |
| Cross-cell connectivity | Unchanged — NAVI/NVMI + edge-link passes already handle it and consume `(verts,tris)`; Phase 1 only owes them border edges at the seam. |

---

## Connectivity invariant: status 2026-07-25

Acceptance test is `tools/navmesh/component_audit.py` (SINGLE-PROCESS — see its
docstring): **one connected pathgrid component must produce one connected
navmesh component.** Anything more means the engine cannot make a walk the
pathgrid asserts, however good the mesh looks in the preview.

Measured over `--all --limit 60`: **18 bad (32.7%) -> 15 bad (27.3%)** after
raising `_weld_sheets`' `WELD_R` 12.0 -> 16.0. The four reference houses
(Pinarus / Arvena / ChorrolFG / AnvilFG) are all `pathgrid=1 navmesh=1`.

### Fixed: sheet weld radius (the stair-top class)

Where a stair FLIGHT meets its LANDING the two sheets both seed a vertex at the
shared pathgrid node, but at different Z — the flight's last row sits on the
ribbon CHORD, the landing's on the floor. Pinarus's stair top came out
**12.66u** apart (identical XY, pure Z gap), just outside a 12u weld, so the
house shipped as 150/148 triangles with no shared edge across the joint.

`WELD_R = 16.0` closes it. The value is bounded on both sides and must stay
there: it equals `RIBBON_GROW_MIN_HALF`, so the radius cannot span two distinct
rails, and it is far under `MAX_CLIMB` (34) so it cannot fuse a step an actor is
supposed to climb. A trial at 20.0 scored marginally better (14 bad) but eroded
triangle counts everywhere and pushed `XPGloomstonePassage02` from 16 to 17
components — a tolerance past its justification, not a fix. **Do not raise it
further to chase a component count.**

The weld must stay **3D**. Measured in plan alone, Pinarus `B v56` is 0.00u from
a main-mesh edge and **267u** below it — a different storey. A plan-only or
grid-snapped weld fuses floors.

### Remaining failures — diagnosed, NOT fixed

Diagnose with `temp/_edgecheck.py <cell>`, which reports every pathgrid edge
whose endpoints land in different navmesh components together with that edge's
slope; that slope separates the classes below. `temp/_gap2.py <cell> <ci> <cj>`
measures the closest approach between two named components.

1. **Vertical drops (NOT a geometry bug — needs an edge link).**
   `VeyondCave02` `n35->n36`: run **24.7u**, dz **308u**, slope **12.49**. The
   two components sit at the same XY (`341.5, 606.9`) 308u apart in Z — a shaft
   an NPC FALLS down. Oblivion pathgrids legitimately connect across such
   ledges. No continuous surface can represent it, and forcing triangles here
   reproduces exactly the unnavigable "fold" rejected at Pinarus's stair top.
   Skyrim's representation is a NAVM edge/portal link, not geometry. The audit
   should classify a crossing edge steeper than ~1.5 as link-only and stop
   counting it as a component violation.

2. **Real holes: the ribbon never got built (the big splits).**
   `VeyondCave02` `n47->n48`: run **329.5u**, dz 44u, slope **0.13** — nearly
   flat, so it SHOULD be one surface, yet comp1<->comp2 are **97.3u** apart at
   the closest point. A gentle ramp was severed outright; no weld radius can or
   should bridge 97u. This is the cause of the large multi-way cave splits
   (`XPAichan01` 23-24 comps, `XPGloomstonePassage02/03`, `XPMilchar02a`,
   `Elenglynn`, `SENSGreenmoteSilo`, `XPXeddefen03spire`) and the 121.86u gap in
   `XPGloomstonePassage03`. Find why the width-grow/clip drops these ribbons
   before touching tolerances again.

3. **1-2 triangle specks** (`KvatchChapelUndercroft` [419,2,2],
   `GoblinJimsCave` [1633,2], `BrumaJGhastasHouse` [415,2],
   `BramblePointCave03` [2540,2], `Piukanda02` [5294,1]).
   Vertex-only contact: the speck shares VERTEX ids with the main mesh but zero
   common EDGES, so `_drop_point_attached` / `_split_t_junctions` do not fire.
   Note `_split_t_junctions` tests candidate vertices in **3D** with `tol=2.0`;
   a speck lying on a main-mesh edge in plan but offset in Z is never split in.
   Cheapest correct fix is to DROP a component under a few triangles that has no
   shared edge, rather than to stitch it.

### The real Pinarus defect: triangles that exceed MAX_CLIMB (2026-07-25)

"One component" is NOT sufficient. Pinarus passed the component audit and was
still unnavigable in game — the upper floor hung off a single vertex through a
fan of triangles each climbing 44-54u, and the pathfinder will not traverse a
triangle whose rise exceeds `MAX_CLIMB` (34). Measure it with
`tools/navmesh/bottleneck.py`, which reports single-edge BRIDGES (a shared edge
whose removal splits the mesh) and the total shared-edge width across each Z
level. Pinarus's stair throat showed **1 shared edge / 106.7u** where every
other level had 6 edges / ~520u.

**The pathgrid was NOT the problem.** `tools/navmesh/surface_residual.py`
measures mesh_z minus real collision_z per vertex: Pinarus's upper floor is
**100% of vertices at exactly 0.00u** (flush on collision), and 86.5% of the
whole cell is within +/-2u. The hover theory is disproved for this cell —
lowering the upper floor would sink it INTO the floor. No pathgrid edge in any
of the four houses is steeper than **0.91** (Arvena's worst is 0.53), so the
input lines are all ordinary staircases.

**Cause: two thresholds in `_ribbon_seeds` were set from `STOREY_GAP_Z` when the
walkability question is `MAX_CLIMB`.**

- the steep DETECTION test was `rise/run*target_edge > STOREY_GAP_Z * 0.5` (60u),
  so any ribbon climbing 34-60u per triangle was treated as flat ground and kept
  128u triangles.
- the steep SPACING aimed at `STOREY_GAP_Z * 0.33` = **39.6u of climb per step,
  above MAX_CLIMB**. Its stated goal was only to keep a triangle under
  `STOREY_GAP_Z` so the per-surface emission would not DROP it; whether an actor
  could walk it was never considered.

Both now key off `MAX_CLIMB` (detection at `> MAX_CLIMB`, spacing at
`MAX_CLIMB * 0.6`). On Pinarus's 515u/264u stair that is **13 segments at 20.56u
climb** instead of 6 at 44.55u. Over-climb triangles per cell:

| cell | before | after |
|---|---|---|
| Pinarus | 33 | **17** |
| Arvena | 34 | **15** |
| ChorrolFG | 65 | **45** |
| AnvilFG | 20 | 28 |

Ramp fidelity is untouched (slopes identical to HEAD, `ramp_miss=0/38`), the
component invariant is unchanged over `--all --limit 60` (15 bad, same as the
weld fix alone), Chorrol's Z-seam went 1 -> 0, and the 28 targeted tests pass.

**STILL OPEN — the remaining over-climb triangles, and Pinarus's sliver joint.**
The joint is still a fan around ONE vertex (v14, z=68.6): `edge(14,116)` spans
z 68.6 -> 15.1. The seeding fix demonstrably reaches the area (a new intermediate
vertex appears at z=48.0, dz=-20.6) but the triangulator still draws the long
diagonal PAST those seeds, so the top step remains 41-53u. Suspect the Poisson
keepout in `_triangulate` (`min_dist=target_edge*0.6`, `keepout2`) thinning the
fine stair seeds, and/or the plan-space Delaunay preferring the long diagonal
because the stair polygon is a narrow band in plan. Fixing this needs work in
`_triangulate`, not another threshold.

**DO NOT "fix" this by dropping over-climb triangles.** Measured: dropping every
triangle spanning > MAX_CLIMB shatters ChorrolFG into `[153,124,123,123,12]` and
Pinarus into `[128,107,57]`. Those triangles ARE the only floor-to-floor
connection — they must be SUBDIVIDED into walkable steps, never removed.

---

## The triangle-quality contract (2026-08-04)

The author's explicit brief: **triangles MUST be close to equilateral** — the
long side no more than 2x the short side, plus a hard MINIMUM triangle area,
with the sawtooth/decimation machinery existing precisely so the mesh can be
broken into LARGE well-formed triangles and the little bits around the outside
simply removed.

**Shape metric — `corridor_clean._badness`.** Edge ratio alone cannot see a
CAP (obtuse, near-zero height, all edges comparable — visually the worst
sliver there is).  Badness = max(edge_ratio / MAX_EDGE_RATIO (2.0),
aspect / MAX_TRI_ASPECT (2.5)) with aspect = longest^2/(4*area); 1.0 is the
contract boundary.  Every cleanup pass (collapse bound, flip objective, cull
candidacy, split candidacy) uses this one metric.

**The passes, in order** (decimate -> cull -> decimate -> cull inside
`finalize`, budget split 100%/40%):

* collapses (edges < DECIMATE_MIN_EDGE 64) with link-condition +
  outline/sawtooth rules; a collapse may not push shape past the contract;
* Lawson flips (`_flip_pass`), duplicate-edge-guarded;
* long-edge bisection (`_split_needles`): a needle whose edges are all LONG
  can be fixed by neither collapse nor flip — bisect its longest edge at the
  apex projection, only when both halves beat the parent's badness (a naive
  midpoint bisect minted two r=11 slivers where one r=4 stood);
* boundary sliver cull: badness > 1 and area < 3000, or area < MIN_TRI_AREA
  (1000 — a Skyrim actor's footprint; vanilla door triangles bottom out at
  992).

**The walkability contract (added same day, author's rule).** Connectivity is
not the metric — CHOKEPOINTS are: two areas joined by a strip narrower than
half a doorway (~48u) are UNWALKABLE for NPCs.  Enforcement in the cull:

* a candidate whose pathgrid samples remain covered by neighbours may go
  (sole-cover slivers never go; replacement cover must be within a STEP of
  the sample's own z — an 80u window let a stacked cave ledge below count);
* `_narrows_corridor`: pin_xy samples carry the line DIRECTION; the cull
  measures the corridor's live cross-width at any nearby sample and refuses
  the cull when it is already under 56u.  Wide-room fringe still culls.
* pathgrid NODES are pinned (DECIMATE_PIN_NODE_RADIUS 24): outline collapses
  and culls had no node awareness and shaved the boundary across junctions
  (single-sample holes exactly at nodes in ImperialDungeon01/BarrenCave).

**Doors are never walls.** A door is a thing an actor OPENS: vanilla navmesh
runs under every door.  `gather_cell_geometry(skip_bases=door bases)` keeps a
door ref's placed FLAT faces (a trapdoor/platform door IS the floor — the
ImperialDungeon01 nodes 243-248 junction stands on one, and gates are
authored upright then laid flat by rotation, so the local-space class cannot
be trusted) and drops everything steep (the panel).  Measured defect: the
Pinarus upstairs animated door's at-rest panel sits 47u from its threshold
ACROSS the passage and pinched the doorway to nothing.

**Flat surfaces over flights.**  Three mechanisms keep a FLAT surface (node
disc, door quad) from hanging mesh over a staircase:

* disc RAY TRIM at stair nodes (`DISC_RAY_TRIM`): the march stops at walls
  and sudden drops but happily follows a RAMP down a legal step per station;
  the trim walks the real surface and stops the ray where it has left the
  node's level by more than a step in total;
* `_clip_flat_poly_off_level`: discs and door quads give up the parts of a
  steep ribbon's footprint that are off their level — but ONLY intervals
  contiguous with a mouth station INSIDE the polygon (anchoring).  |dz| alone
  cannot tell "my own flight ramping away" from "another storey's flight
  passing under me in plan": the unanchored version opened 37 walked-line
  holes on ChorrolFightersGuild's mid floors;
* door quads are RAMPS, not shelves: `door_footprints` probes the corridor
  mesh under the quad's far edge (`z_far`) and the strip slopes to meet it,
  clamped to slope 0.5 (the probe's storey-scale tolerance could grab the
  WRONG floor and paint a 45-degree cliff across a corridor — Moranda02).

**The crack zipper.** Two emissions of a flight can meet along a zero-area
lens: coincident in plan, 3-8u apart in z — no shared edge, so the engine
cannot path across, and it renders as a hairline hole ON the staircase (the
ImperialDungeon01 "holes in the highest stairs").  `_split_t_junctions` seals
them: hits project in PLAN with a separate z window (TSPLIT_Z_TOL 12 — a full
MAX_CLIMB window grabbed genuine fold vertices and minted 18 overlaps), a hit
that is itself a BOUNDARY vertex may be up to TSPLIT_CRACK_TOL 6u off the
edge (both sides of a crack are boundary; an interior vertex that close is
dense healthy mesh and keeps the 2u radius), and a hit is refused when the
fan's new edges would give any edge a 3rd owner (_make_manifold would rip the
extras and delete real corridor — measured 3-sample losses in two cells).

**Repair-pass ordering.** `_split_t_junctions` re-runs after the last
vertex-moving pass (merge/stitch): a hanging node minted late reads as
point-attached and `_drop_point_attached` deletes REAL coverage (the
ImperialDungeon01 prison junction triangle).  Plan-degenerate triangles are
culled by `_drop_degenerate_guarded` (never disconnecting; load-bearing
degenerate connectors survive) — in `finalize` AND once more after
`attach_door_triangles`, which mints seam slivers of its own.

**Measured state (in-process harness vs the prior user-approved build)**:
badness p90 1.15-1.6 vs 1.4-2.1; contract violations down 6-11 points per
cell; sub-1000u^2 triangles roughly halved; walked-line coverage and
chokepoints at parity (residual: 1-3 single 16u samples per cave cell and
+1 choke edge on two cells, all borderline z-drift on jagged cave floors).
Verify with `temp/sweep.py` / `temp/esm_shape_cmp.py` (miss / choke / ovl /
badness per cell, current build vs the ESM on disk).
long side no more than ~2x the short side, plus a minimum triangle area — with
sawtooth outlines simplified inward and the leftover "little bits around the
outside simply removed."  Implemented as a pipeline of guarded passes; every
one preserves the two hard invariants (no overlapping same-surface triangles,
no disconnection the pathgrid contradicts).

### Where the shape comes from

1. **Interior hex lattice** (`corridor_union._hex_refine`).  GEOS's
   constrained Delaunay uses only the polygon's own vertices, so any region
   wider than one triangle triangulates as a fan of slivers — no post-collapse
   can fix that, because the vertices to break the fans do not exist.  A hex
   lattice at `TRI_TARGET_EDGE` spacing is inserted point-by-point into the
   CDT (containing-triangle 3-fan split; each point kept 0.45×spacing clear of
   existing vertices and of the boundary), then `_flip2d` restores local
   shape.  Lattice anchored on the part's own bounds — deterministic.
2. **Ratio-improving diagonal flips** (`corridor_union._flip2d` in 2D at
   triangulation time, `corridor_clean._flip_pass` in 3D during decimation).
   A flip moves no vertex, so outline and coverage cannot change.  Guards:
   strict ratio improvement, quad convexity via signed areas, z-span of the
   new diagonal, door triangles (all corners pinned) untouched, and — learned
   the hard way — **never flip onto a diagonal that already exists as an edge
   elsewhere**: folded storeys reuse vertices, the duplicate edge is
   non-manifold, and `_make_manifold` later rips whole regions out.
3. **Decimation shape ceiling** `MAX_EDGE_RATIO = 2.0`: no collapse may push
   any triangle past the 2× contract (or past the worst ratio already
   present).
4. **Sawtooth cuts** (decimate boundary rule): a *convex* outline vertex —
   one whose removal can only SHRINK the mesh — may be cut with deviation up
   to `DECIMATE_SAWTOOTH_DEV` (32u), budgeted by `DECIMATE_MAX_AREA_LOSS`
   (10%).  Concave vertices never move (their removal would extend the mesh
   outward, i.e. through a wall).  Exterior-seam vertices only ever collapse
   collinearly, so cross-cell stitching is untouched.
5. **Peripheral sliver cull** (`corridor_clean.cull_boundary_slivers`): a
   boundary triangle with ratio > `CULL_SLIVER_RATIO` and area <
   `CULL_SLIVER_MAX_AREA`, or below `MIN_TRI_AREA`, is removed outright —
   unless it touches a door pin, contains a pathgrid sample, lies on the
   cell seam, or its neighbours would lose each other (bounded BFS).
6. **Door pins are TIGHT** (`DECIMATE_PIN_RADIUS` 8u around the wedge ring
   points, 24u around door centres).  The old 80u blanket froze every sliver
   near a doorway beyond repair (area-3 MICRO triangles parked forever).

Measured on the reference cells: edge-ratio p50 1.56–1.85, p90 2.3–3.1;
needles (>3.0) 3–12% (they are the protected minority: pathgrid-carrying
strips, connectivity bridges, genuinely thin corridors).

### The overlap/connectivity repairs that made it safe

The quality passes exposed a series of latent defects; the fixes are load
bearing and each encodes a measured failure:

* **Same-emission weld = provisional, checked, reverted** (`_weld_sheets`):
  a sideways weld that creates any overlap is undone (an outright ban broke
  ImperialDungeon05's connectivity; the unchecked weld created Pinarus's
  stair-bottom overlaps).
* **Junction strips are clipped to the junction disc** (`_clip_strip_near`),
  and the clip measures from the NODE's projection, not the segment end
  (stair ribbons extend 48u past their nodes).  Handing over the whole strip
  leaked its heights across everything it passes under → phantom duplicate
  floors.
* **Steep edges carry a tread-following height profile**
  (`corridor._surface_profile`, mirrored natively in `grow.cpp
  py_levels_at`): a DP over walkable collision layers along the line,
  constrained to start/end at the node heights.  The end constraint is what
  selects the treads over the floor that continues under the flight.
* **`_merge_at_pathgrid_nodes` welds ONE closest cross-component pair per
  junction, capped at `RIBBON_HALF_WIDTH`**, in a disc widened by one ribbon
  width, and runs BEFORE the stitch.  The old whole-band weld deleted every
  triangle that fit inside the node disc (lattice-sized triangles all do).
* **`_stitch_shared_nodes`** fuses coincident vertices each round (post-weld
  passes mint identical positions under different indices), bridges with an
  overlap guard (relaxed to a 250u² sliver tolerance only for junctions
  nothing else could join), and slope-based dz guards (a bridge may climb
  with its plan run; only height without run is a wall).  It is re-run at
  the END of `finalize` — decimation can land two components' vertices on
  the same position, invisible to everything upstream.
* **`_destack`**: same-surface stacked duplicates (two triangles covering
  the same plan area within 40u of height) that survive the claim are
  removed, smaller first, connectivity-guarded.
* **`_drop_walls`**: triangles steeper than `WALL_SLOPE_COS` (55°) removed
  when their neighbours stay connected without them — never at emission
  time, which tore caves apart.
* **Decimation topology guards**: the standard link condition, plus
  boundary-pair collapses only along an OUTLINE edge (collapsing across a
  thin neck pinches the sheet and sheds vertex-attached scraps).

State on the 10 reference cells (2026-08-04): component invariant 9/10 OK
(Moranda02 at 2 components — its historical defect, previously 3–4 — with an
85u genuine hole in one tunnel), overlaps 0 everywhere except two mutually
load-bearing bridge pairs in a BarrenCave throat.

### XXXX-oversized NVNMs and the edge linker (2026-08-04)

The lattice pushed ~119 exterior meshes past the 65,535-byte subrecord limit,
so their NVNMs are written under the XXXX size-override protocol.
`navm_edge_links._extract_nvnm` did not speak XXXX and read those NVNMs as
EMPTY — every oversized mesh silently skipped cross-cell edge linking (the
"6504 → 6385 exterior navmeshes" drop in the build log; the meshes themselves
were present and fine).  The walker now honours XXXX; `pack_subrecord`
re-emits it on write.  Any new consumer that walks raw NAVM subrecords MUST
handle XXXX — `navm_split._decode_record` and `navi_builder` already do.

Known pre-existing gap (not from this work): 53 exterior cells whose pathgrid
is a single node with only PGRI (cross-seam) links get no navmesh job at all
(`_gather_navm_jobs` requires in-cell edges); `build_navmesh` produces a valid
ribbon for them when invoked directly, so the fix is to gate jobs on
"edges OR PGRI links".
