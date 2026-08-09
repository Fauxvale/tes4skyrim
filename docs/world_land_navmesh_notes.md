# World / LAND / PGRD→NAVM Conversion Notes

Linked from [CLAUDE.md](../CLAUDE.md). Covers pathgrid→navmesh conversion and
LAND/landscape-texture record structure. For terrain LOD generation see
[nif_conversion_notes.md](nif_conversion_notes.md#terrainlodland-adjacent-asset-notes).

## PGRD → NAVM/NAVI Conversion (PathGrid → NavMesh)

TES4 PGRD (per-cell pathgrid of nodes+edges) is converted to a TES5 NAVM per
cell PLUS a single top-level NAVI (Navmesh Info Map). Implemented in
`tes5_import/pgrd_to_navm.py` (`convert_PGRD`) and `tes5_import/navi_builder.py`
(`build_navi_record`), wired in `import_main.py` Phase 4 for both interior
(`_build_cell_groups`) and exterior (`_build_world_groups`) cells.

- **NAVI IS MANDATORY**: Skyrim only uses a NAVM for pathfinding when it is also
  indexed in a top-level NAVI record. NAVM records alone are ignored. NAVI goes
  in the top-level group order immediately BEFORE CELL (verified vs xEdit
  `wbAddGroupOrder`, and added to `writer._group_order`).
> ### ⚠ Surface-generation sections below are HISTORICAL (flagged 2026-07-26)
>
> The **collision-voxel** algorithm described in the next block — and every
> `voxel.*` / `region.*` / `spanmesh.*` / decimation rule that follows from it —
> **is no longer how the navmesh is built.** Those modules are deleted from
> `master`. The live generator is the **pathgrid corridor-ribbon** model:
> [navmesh_corridor_redesign.md](navmesh_corridor_redesign.md) (implemented),
> tuned per [performance_notes.md](performance_notes.md).
>
> **Still current and safe to rely on** in this file: NAVI-is-mandatory, the NVNM
> and NVMI binary layouts, door handling/links, the base-model index, triangle
> flags, LAND VHGT decode, REFR rotation transpose, world-space obstruction, the
> collision cache, and the iteration tools. Treat the voxel/region/spanmesh
> pipeline details as background on a superseded attempt.

- **Algorithm (collision-voxel — HISTORICAL, see the notice above; rewritten
  2026-07-12 to replace the pathgrid-buffering approach that could not represent
  walls)**: VOXELIZE the real Havok
  collision geometry of everything placed in the cell. The collision mesh is
  exactly what the engine uses to decide what an NPC stands on / is blocked by,
  so we use it directly instead of guessing from the pathgrid. Modules live in
  `tes5_import/navmesh/`:
  1. `world.gather_cell_geometry`: for every REFR, transform its base mesh's
     cached collision soup by the ref's FULL rotation + scale + position into
     cell space; split by surface normal into WALKABLE (|nz|≥cos46°) and
     BLOCKING. Exteriors also emit the LAND height field as walkable terrain.
  2. `voxel.build_heightfield` + `apply_filters`: rasterize into a column grid of
     Z-spans (CS=16u interior / 32u exterior, CH=8u), then Recast filters —
     low-hanging-obstacle merge, ledge (MAX_CLIMB=34u), min-headroom
     (AGENT_HEIGHT=128u) — plus agent-radius erosion (AGENT_RADIUS=24u) for a
     correct standoff from walls.
  2b. `voxel.stamp_pathgrid` — **the pathgrid goes in HERE, before any filter.**
     A band of PGRD_BAND (24u) either side of every pathgrid line is stamped as
     PROTECTED walkable spans, snapping onto real collision at that height where
     it exists and synthesizing a span where it does not. Protected spans are
     immune to every later stage: ledge filter, headroom filter, region cull and
     agent erosion all skip them. The stamp yields to NOTHING (an early version
     skipped columns with blocking collision, which silently refused to stamp
     staircases — a stair's own faces are steep, hence "blocking" — and left the
     storeys of a house as disconnected islands).
     **The sweep FOLLOWS THE WALKED SURFACE, not the edge's chord (2026-07-17).**
     Each step predicts `z + chord_slope` then locks onto the walkable surface
     nearest that prediction (window: PGRD_SNAP_Z=48 down, MAX_CLIMB up), so the
     ribbon walks down through gullies and up staircases like the NPC would; the
     chord is only the pacing fallback where geometry is absent. Snapping each
     sample independently against the raw chord had band columns alternating
     between terrain and chord height — a jagged lattice of near-vertical
     triangles down every hillside. Three self-contamination guards matter:
     (1) the follow ignores spans the sweep itself synthesized (`synth_tops`) —
     locking onto its own tail made climbing ribbons lag their chord and arrive
     a storey low (100+ broken edges in geometry-less cave cells); (2) a
     re-stamp keeps whichever pgz is CLOSER to the current sample — re-snapping
     onto a synth-merged span's walkable top ratcheted the mesh up furniture
     one MAX_CLIMB per pass; (3) post-sweep, a synthesized span within
     AGENT_HEIGHT of a SNAPPED protected span in the same column is dropped
     (two standable layers can't be that close; the chord fabricated air over
     real treads). Synth-vs-synth conflicts are kept — switchback flights both
     crossing a floor-less column are each load-bearing.
  3. `region.build_regions` + `seed_regions` + `keep_regions`: flood-fill spans
     into connected regions and KEEP only those a pathgrid node vouches for.
     Tabletops/roofs/ledges hold no node and are dropped. `keep_pathgrid_heights`
     then drops any span no pathgrid sample vouches for at its height — this is
     what stops navmesh appearing on the CEILING of a room a staircase passes over.
  4. `spanmesh.build_mesh`: mesh the SPAN GRAPH directly (see below). Then
     `_decimate` collapses edges, bounded by BOTH a plane error (MAX_SIMPLIFY_ERR)
     and a triangle-QUALITY test (aspect ratio ≤6, edge ≤TRI_TARGET_EDGE).
  5. `build.build_navmesh`: orchestrates the above, then `_drop_steep_triangles`
     (MAX_SLOPE_DEG is a HARD ceiling with no exceptions), `_cull_boundary_flaps`
     and `_prune_islands` (see below). Then this module computes adjacency,
     water flags, door triangles.

### Island pruning / boundary cleanup (2026-07-15 quality pass)

- **`_prune_islands` keep rules**: a disconnected component survives iff it has
  ≥ MIN_ISLAND_TRIS(5) triangles AND (it is ANCHORED — reaches a teleport door
  within ISLAND_DOOR_RADIUS, or in an exterior comes within ISLAND_EDGE_MARGIN
  of the cell border ("runs over into the next cell") — OR it is vouched by a
  pathgrid node and not merely SHADOWING a kept component in Z). The size gate
  applies to anchored components too: a 2-triangle doorstep scrap disconnected
  from the room is worse than no mesh at the door — it steals the Door Triangle
  from the main mesh and teleports NPCs onto an island they can't leave.
- **`_cull_boundary_flaps`** ("delete edge triangles that aren't up to snuff"):
  outline triangles with ≤1 neighbour (protruding flaps — provably never a
  bridge, so removal cannot disconnect anything) below EAR_MIN_AREA are deleted,
  EAR_ROUNDS(2) rounds. Exemption must be DISTANCE to the densified pathgrid
  line (EAR_PGRD_RADIUS), not node containment: containment-only let the cull
  eat ribbon ends and narrow cave ledges (2 wrong-floor nodes + broken edges in
  XPGloomstonePassage02 until fixed). Runs BEFORE `_prune_islands` so the size
  gate judges final component sizes.

### 🔴 Drop-down storeys arrive as separate components (found 2026-07-26)

**Symptom:** CharacterGen's Ambush A never fired. The Mythic Dawn assassins sit
in a holding cell that teleports (a door pair, both refs in the SAME cell) onto
a mezzanine they are *meant to step off* into the ambush room below. The
mezzanine and the room floor came out as two disconnected navmesh components, so
`CGAssassinsAmbushA4` could never complete, its `OnPackageEnd` never set stage
23, and A1/A2/A3 — gated `GetStage >= 23` — stayed parked in
`DefaultMasterPackage` forever. In game the assassin visibly walks *into* the
door instead of through it.

**Cause — and it is NOT a navmesh defect.** Oblivion has no pathgrid edge for a
DROP. A balcony and the floor beneath it are two disconnected pathgrid islands
and the actor simply steps off. Verified in the source data: cell 0001FBB9's
PGRD has **zero** edges between the pen (points 268–272, z=-594), the mezzanine
(z=-640) and the room floor (z=-832), and its single RefMap entry covers neither
door. Our navmesh reproduces the pathgrid faithfully, islands included — so the
faithfulness is what produced the break. Skyrim has no "step off here" construct
either; connectivity IS the mesh.

**Fix:** `corridor_clean.find_ledge_links` (params `ISLAND_BRIDGE_*`) detects
component pairs whose boundary edges nearly meet in plan (`ISLAND_BRIDGE_XY`,
two ribbon widths) but are separated by a drop of `MAX_CLIMB`..220u, and
`pgrd_to_navm._pack_nvnm` writes them as **Ledge Down / Ledge Up edge links**
(see "Drop-downs are EDGE LINKS" below).  Measured geometry in 0001FBB9: the
mezzanine/floor drop is 192u.  Both sides must ALREADY be separate components,
so stairs, ramps and genuinely-connected storeys never enter the candidate set.
A geometry-welding `_bridge_islands` variant was tried first and rejected —
bridging triangles let actors walk on air and bred downfacing triangles; the
edge link is Skyrim's own construct for this.

### 🔴 A door needs XNDP on the REFR, not just door triangles (found 2026-08-03)

**Symptom:** the *same* CharacterGen Ambush A stall as the drop-down bug above,
still present after that fix. The four Mythic Dawn assassins stayed in their
holding cell at stage 22+. Every layer checked out: `CGAssassinsAmbushA1-A4`
convert to Travel instances of the vanilla `Travel` template (00016FAA) with the
right `GetStage` CTDAs; all three packages per assassin sit on the actor's QUST
reference alias in TES4 order (ALPC verified in the written ESM); the aliases
are filled with the right ACHRs; `pack_validate.py` reports clean.

**Cause.** Three separate structures bind a door to the navmesh, and we wrote
only two:

| Structure | Direction | Written? |
|---|---|---|
| NVNM "Door Triangles" (in NAVM) | navmesh → door | yes |
| NAVI NVMI "Door Links" | navmesh → door | yes |
| **REFR `XNDP`** | **door → navmesh triangle** | **no** |

The engine builds its `BSPathingDoor` from the DOOR REFERENCE an actor is
heading for, so it needs the door→navmesh direction — and that is `XNDP` alone.
Without it a teleport door is not a pathing node: an actor whose destination
lies beyond it has no route, and simply never leaves the room even though its
package, alias and conditions are all correct.

**Vanilla census (the invariant):** 1,705 of 1,722 Skyrim.esm teleport-door
REFRs (99.0%) carry XNDP, and 1,705 of the 1,706 XNDP-bearing REFRs in the file
are teleport doors — the subrecord is essentially *the* teleport-door navmesh
binding. `XNDP` also appears as a literal in SkyrimSE.exe's REFR load switch.

**Layout** (xEdit `wbStruct(XNDP, 'Navmesh Door Link')`): `Navmesh FormID u32 +
Triangle s16 + 2 unused`, 8 bytes. The trailing 2 bytes are uninitialised CK
memory in vanilla (`DA08` x1262, but `0000` x107) — write zero.

**Ordering:** XNDP goes LAST, immediately before DATA — after XLOC/XOWN/XLRT/
XSCL. All 1,706 vanilla records agree.

**Fix:** `_convert_pgrd` already computes `door_tris` = `[(triangle, door_ref)]`
for NVNM; it now also exports `meta['door_xndp']` = `{door_ref: (navm_fid,
triangle)}`. `import_main` merges those across every navmesh right after
`build_edge_links` (triangle indices are final only then, and it must precede
the group builders that convert REFRs) and hands them to
`world.set_door_navmesh_links`; `convert_REFR` emits the subrecord.
`convert_worker.init_worker` replays the map into pool children — module state
like the location maps, and a worker missing it writes door REFRs with no
navmesh link at all.

Note `build_edge_links` only appends edge links to *exterior* meshes and never
reorders triangles, so indices captured before it stay valid.

### Door threshold axis comes from the COLLISION PANEL, never the bbox

Which local axis a door's threshold runs along decides the whole quad's
orientation. It is read from the door's **collision panel** — the body the
engine collides with — in `asset_convert.collision_extract.door_panel_axis_from_data`,
cached to `door_panel_axis_cache.json` by `tools/build_door_axis_cache.py`:

> A door panel is thin THROUGH the opening and wide ACROSS it. The panel's thin
> horizontal axis is the swing direction; the wide one is the threshold.

**The whole-NIF bounding box cannot answer this** — it includes the door
frame/arch, which routinely dwarfs the panel and inverts the result:

| model | bbox | panel | old (bbox) | correct |
|---|---|---|---|---|
| `AnvilDoorMC01` | 98 × 150 | 97.9 × 4.5 | Y | **X** |
| `chorrolfightersguildinteriordoorjam` | 188 × 32 | 34.5 × 186.5 | X | **Y** |
| `icbarreddoor01` | 152 × 14 | 15.2 × 136.8 | X | **Y** |

22 of 184 door models were wrong under the bbox rule, each laying its door quad
90° out (Anvil's exterior doors — Pinarus's house among them).

Read the **`output/`** meshes, not `export/`: the shipped collision is what the
navmesh and engine use, and its body transform is already baked into the shape,
so there is no `bhkRigidBodyT`-vs-`bhkRigidBody` rotation branch to get wrong.

The same measurement supplies the doorway **WIDTH**, and the quad must span it.
Door panels run **16u to 764u wide (median 121)**, so the old hardcoded
`DOOR_LINE_HALF = 45` (a 90u base line) was simply the wrong size for most
doors. On `impdundoor01` (115u) it left the **first 30u of the threshold with
no mesh under it**, and the Door Triangle came out a 571-unit scrap — smaller
than *every one* of 1,659 vanilla door triangles (min 992, median 9,614) and too
narrow for an actor to stand on. That is what stopped the CharacterGen assassins
dead at their cell door: they reached the door triangle and could not settle onto
it, so `OnPackageEnd` never fired, stage 23 never ran, and the other three
assassins never got a valid package at all.

Three traps, all of which silently dropped real doors:
* **CMS must be unwrapped.** Converted doors ship `bhkMoppBvTreeShape` →
  `bhkCompressedMeshShape`; 85 vanilla models (every Cheydinhal/Bravil/Leyawiin
  and castle-tower door) arrive that way. Decode with `asset_convert.cms.decode_cms`.
* **A zero-thickness collision sheet is legal.** `cathedraldoor02`,
  `priorydoor01`, `weynondoor01`, `skdoormiddle01`, `icwalldoor01` ship a flat
  plane where the ZERO axis *is* the swing direction. Rejecting `min(ex,ey)==0`
  dropped 10 real doors.
* **Thin-in-Z means no threshold at all.** Trapdoors, hatches, grates, manhole
  covers and display cases swing about a HORIZONTAL axis. They get no quad
  (`_DOOR_NO_THRESHOLD`); assigning one lays a quad across the floor in an
  arbitrary direction. Teleport doors are kept regardless — they still link two
  navmeshes.

#### 🔴 An unreadable door shape is NOT a trapdoor

`_DOOR_NO_THRESHOLD` (thin-in-Z) must only suppress the door QUAD — never the
door itself. Dropping such doors from `_collect_doors` deleted the Imperial
Prison cell gates, **including the player's own starting cell door**, because
`bhkListShape` (the gates ship as a list of bars) read as "no shape" and is
indistinguishable from a real trapdoor once it reaches the cache. Every door
must still receive a Door Triangle or the doorway is dead in the engine.

Collision shapes that must be unwrapped before measuring: `bhkMoppBvTreeShape`
and `bhkConvexTransformShape` (single child), `bhkListShape` (**several**
children).

#### 🔴 The debug tools were measuring doors the pipeline never builds

`navmesh_audit.py` cached `door_fids` as a **set**, while the pipeline
(`import_main._build_door_fid_set`) builds a **fid → model-key map**. With a set,
`_collect_doors` takes its legacy membership-only path: no panel centring, no
threshold axis, **width 0**. `navmesh_cell_check.py` additionally never called
`load_door_centroids` at all. So every generated-cell tool silently graded doors
with the default orientation and no width — the exact opposite of what shipped.
If a door metric from a debug tool disagrees with the ESM, check this first.
`tools/navmesh/index.py` (and therefore `tools/navmesh/render.py`) always loaded it.

### Drop-downs are EDGE LINKS, not bridging triangles

Oblivion expresses a drop-down as two disconnected pathgrid islands — the actor
steps off a ledge and there is no pathgrid edge for it. Skyrim's own mechanism
is an NVNM **Edge Link**, typed by `wbNavmeshEdgeLinkEnum`
(`xEdit/Core/wbDefinitionsCommon.pas:7272`):

| Type | Meaning |
|---|---|
| 0 | Portal (ordinary cross-mesh connection) |
| 1 | **Ledge Up** |
| 2 | **Ledge Down** |
| 3 | Enable/Disable Portal |

A drop-down is a **pair**: `Ledge Down` on the upper triangle, `Ledge Up` on the
lower. Vanilla census (Skyrim.esm, 3,000 navmeshes): 30,546 Portal, **467 Ledge
Up, 476 Ledge Down** — near-symmetric, exactly as pairing implies. Both links
may name the SAME navmesh when both triangles are in it (`0008FFE1` links to
itself), which is the usual case for us.

The linked triangle must also set the matching **per-edge link bit** in its
flags — `0x0001`/`0x0002`/`0x0004` for edge slot 0/1/2 (vanilla shows `0x0801`,
`0x0802`, `0x0804`). Pick the slot that is an OPEN edge (no neighbour) facing
the other side: that is the lip.

Two more parts of the contract, verified against real Skyrim.esm ledge links
(NAVM 0002FB4A/0002FB4B reciprocal pair, 001090A8 self-links) — the first
implementation got BOTH wrong and shipped dead links:

* **The carrier edge's neighbour field becomes the link INDEX.** When flag bit
  N is set, triangle edge-N no longer holds a neighbour-triangle index (or −1);
  it holds the index into the Edge Links array — the same `wbEdgeToStr` rule
  the Portal stitcher (`navm_edge_links.add_link`) already follows. Setting the
  bit but leaving the field at −1 makes the engine deref link −1.
* **The link's Triangle field names the TARGET triangle**, i.e. the one on the
  other side of the drop, in the navmesh the link's FormID names. Writing the
  carrier's own index makes every link point back at itself.

`corridor_clean.find_ledge_links` detects the pairs and `pgrd_to_navm._pack_nvnm`
writes them. It previously **stitched two triangles across the lip** instead,
which is wrong twice over: actors walk on air across the gap, and the near-
vertical quad breeds downfacing/opposite-normal triangles (ImperialDungeon01:
DOWNFACING 4 → 2 once the bridging was removed).

Triangles are identified by CENTROID between detection and packing — the cull
and compaction passes reorder both triangles and vertices, so an index captured
early is meaningless later.

### 🔴 The Door Triangle is RESERVED, not protected

Vanilla marks a door with **ONE** triangle whose long edge is the **full width
of the doorway**. The way to guarantee that is not to defend the triangle from
the passes that would damage it — it is to make sure they never see it:

1. `corridor_union._triangulate` computes the door triangle (base line +
   apex) and **cuts it out of the polygon** with `difference()` before
   Delaunay runs. The triangulator fills around a hole and cannot subdivide
   what is not there.
2. Every pass afterwards — the 3D weld, the T-junction split, the
   pathgrid-node merge, make-manifold, decimation, the island cull — sees the
   doorway as ordinary mesh boundary. Nothing there to split, weld or drop.
3. `corridor.build_corridors` calls `corridor_union.attach_door_triangles`
   **last**, after `finalize`, snapping the base endpoints tightly
   (`ATTACH_R_BASE = 2`) so the door line keeps its exact width and the apex
   loosely (`ATTACH_R_APEX = 8`) so it shares real edges with the mesh.

**Do not add per-pass protection instead.** That was tried across
`_weld_sheets`, `_split_t_junctions`, `_merge_at_pathgrid_nodes` and
`_make_manifold`; survival went 13/27 → 17/28 → 19/28 and never reached the
guarantee. Reservation reached **28/28 on the first try**, and every protective
branch was deleted afterwards.

Three rules the reservation itself must obey:

* **Never cut a hole that DISCONNECTS the sheet.** Where a door sits in a
  narrow passage the wedge can span the whole corridor: ImperialDungeon01's
  main surface stopped at x=2170 instead of 2293 and the door triangle became a
  lone island. Compare polygon part-counts before/after each `difference()` and
  skip any cut that raises it. A door triangle is worth nothing if it costs the
  corridor it serves.
* **Skip a triangle with nothing to attach to.** If all three corners mint new
  vertices, the pathgrid never reached that door; the triangle would land as an
  unreachable scrap.
* **Dedupe per STOREY, not per XY.** Two sheets bordering one threshold each
  reserve it (drop one), but the same door line at a different height is a
  different floor's doorway and keeps its own triangle (ChorrolCastleWallTowerSW
  has one at z=526 and one at z=-15).

Measured over 40 interior cells: 72 doorways, **every one with exactly one
full-width triangle**, none missing.

### Door reservation hardening (2026-08-02)

Rules added after the reservation model met ImperialDungeon01 end-to-end; each
was measured against a concrete failure in that cell and re-verified against
the four reference houses (all 1 component, CK-clean, every door ≥ vanilla
min area 992):

* **Frontal-strip candidate gate** (`corridor_doors`): a corridor edge only
  qualifies as a bridge target when it lies within the doorway's span across
  the facing (± a ribbon width).  The sweep extends along the facing, so a
  candidate displaced sideways is unreachable — accepting one laid a floating
  5-triangle patch beside the tower door whose only corridor runs 283u to the
  door's SIDE.  `DOOR_BRIDGE_RADIUS` is 384 (220 stranded that door's
  neighbours); the wall walk still vetoes blocked candidates.
* **Far-side quad for disconnected doorways** (`corridor_doors`): when a
  non-teleport door has walkable ground on both faces but the two sides'
  nearest pathgrid nodes are in DIFFERENT pathgrid components
  (`_sides_disconnected`), a second, constraint-free quad bridges the far
  side.  This is the prison-cell-gate case — Oblivion ships the cell interiors
  as pathgrid islands with no edge through the (openable) gate, and the
  player's own cell was an unreachable island an escorted Uriel could never
  enter.  The gate MUST be the pathgrid-component test: emitting far quads for
  ordinary doors (whose pathgrid crosses the doorway) severed the staircase
  sheets in Pinarus's and Arvena's houses.
* **De-stacking** (`attach_door_triangles`): the triangulator keeps any
  Delaunay triangle with ≥50% of its area inside the polygon, so ground
  overlapping the reserved wedge survives the cut.  If that gives a door-tri
  edge two users already, appending the door triangle 3-shares the edge — and
  `_compute_adjacency` links only 2-shared edges, so the doorway DISCONNECTS.
  The overlapping triangle is dropped and the door triangle takes its place.
* **Teleport apron rule** (`SPLIT_TINY_AREA`, corridor_union): every teleport
  door carries a thin apron of ribbon extension beyond its threshold, so the
  "never cut a hole that disconnects the sheet" guard read every wedge cut as
  a split and NO teleport door ever reserved — 158737's Door Triangle came
  out as the 534-unit apron sliver.  Pieces under 2,000 sq units are not
  counted as a disconnection and are dropped with the cut.
* **Stitching** (`_stitch_isolated_tri`): a door triangle whose corners all
  snapped to real mesh vertices can still share no EDGE (the Delaunay bridged
  the wedge corners through other vertices).  A short open-edge boundary chain
  from corner to corner is fan-filled; a COLLINEAR chain (mesh boundary
  running along a wedge side, a T-junction) instead splits the door
  triangle's SIDE edge at those vertices — the base line never splits.  The
  chain graph must exclude the door triangle's own edges or the BFS "reaches"
  the far corner through the door itself.
* **Island withdrawal**: if the stitch finds nothing, the reserved triangle is
  WITHDRAWN and `_build_door_links` falls back to the containing mesh
  triangle.  An unreachable 1-triangle island is strictly worse than a
  fallback door triangle.
* **Winding normalisation + scrap sweep** (`corridor.build_corridors` tail):
  every triangle is forced CCW in plan (decimation edge collapses can flip
  one → CK DOWNFACING), and 1-2 triangle components that carry no door
  threshold are dropped.

### Analytic door wedge (2026-08-03)

The reservation no longer *searches* for a door triangle — the wedge is a pure
function of the door, computed in `corridor_doors` and passed through
`door_edges` as `(base0, base1, apex, storey_z)`:

* **Base** = the doorway's exact measured width (collision panel, capped at
  `DOOR_LINE_HALF_MAX`), centred on the exact panel centre.  The old
  45u-minimum widening is gone — it pushed a narrow gate's base through both
  jambs.
* **Apex** = base midpoint + facing × `max(w/2, DOOR_TRI_MIN_DEPTH=64)` on the
  side the PATHGRID serves.  The old `_door_apex` ladder tried BOTH normals and
  five shrinking depths until something fit the polygon, so a cramped near side
  flipped the whole triangle to the far side of the door (three doors in
  ImperialDungeon01), and the area varied with the surrounding geometry.  Same
  door → same triangle, every build.
* **Exact centres**: `door_panel_axis_cache.json` now carries each model's
  collision-panel centre (`[axis, width, cx, cy]`, world units); `_door_threshold`
  prefers it over the legacy mesh-bbox `door_centers_cache.json`.  The bbox
  centres were 25–35u off along the threshold on the CharacterGen prison gates
  (`cgprisoncellgate01`, `idgate01`) — over half those gates' own width.  Double
  doors merge their per-leaf rigid bodies (parallel panel-shaped bodies of
  comparable size), so the width spans the whole doorway, not one leaf.
* **Storey-gated claiming** (`build_union_mesh`): parts are 2D, so where two
  floors stack, BOTH used to pass the containment test and iteration order
  decided which sheet cut the wedge — Arvena's upstairs door was reserved out
  of the sheet that only covers that spot downstairs.  The claim now requires
  the sheet to have a surface level within `STOREY_GAP_Z` of the door's own
  storey.  The claim test also uses `part.boundary` (holes included), not
  `part.exterior` — a mid-floor doorway lies on an interior ring.
* **Apron consumption**: when the wedge cut consumes its whole part (the
  sheet fragment was barely bigger than the doorway — Arvena's front door),
  the door triangle is still emitted (`PENDING_DOOR_TRIS` keeps it, with the
  door's storey_z since no local mesh exists to tag it from) and the remaining
  crumbs are triangulated regardless of `SPLIT_TINY_AREA`, because those
  crumbs are what the door triangle attaches to.
* **Door-storey level seeding** (`_apply_door_apex_levels`): a doorway can be
  wider than the ribbon crossing it, leaving a base corner on ground no strip
  covers — no level, dropped by `_emit_surfaces`, door triangle gone.  Base
  endpoints/apex/ring corners are seeded with the door's own storey height.
* **Corner pull** (`attach_door_triangles`): decimation collapses the wedge's
  hole-ring corners into nearby boundary vertices (7–10u inboard).  When no
  vertex sits within `ATTACH_R_BASE` of a base corner, the nearest vertex
  within `ATTACH_R_BASE_PULL=16` is MOVED to the exact corner — full width
  restored, and the survivor's shared edges to the apex come with it.
* **Base-edge far-face fan**: where the pathgrid runs THROUGH a doorway,
  ground exists on both faces; only the apex side is wedge-cut, so the far
  face can end up point-touching a base corner (Pinarus's bedroom door split
  the upstairs floor in two).  When the base edge has no second user after
  attach, the far face's open boundary is fan-filled onto it
  (`_stitch_isolated_tri(only_edges=[base])`).

### Door placement convention + closed-pose cache (2026-08-03)

* **Placement rotation is the TRANSPOSE.**  Bethesda applies the inverse of
  the stored REFR rotation when placing a mesh (`navmesh/world.py
  _rot_matrix`, measured on the AnvilFG floor shell).  `_door_threshold` and
  every door direction formula used the naive CCW form — wrong for any
  rotation off 0/180, which is why it survived every cardinal-rotation test:
  Arvena's upstairs door (raw 90°) had its centre one FULL door width from
  the real doorway.  Correct forms everywhere now: centre offset
  `(lx·c + ly·s, −lx·s + ly·c)`; threshold `(sin rz, cos rz)`; facing
  `(cos rz, −sin rz)`.
* **The door cache measures the ORIGINAL NIF at the CLOSED pose**
  (`asset_convert.collision_extract.door_closed_geometry`, built by
  `tools/build_door_axis_cache.py` from `export/<plugin>/meshes`; the
  converted-mesh scan no longer writes it).  The 'Close' controller
  sequence's FINAL key values override the animated nodes, and the union
  bbox of the KEYED shapes — the door leaf/leaves, never frames or static
  fence sections — gives `[axis, width, centre_x, centre_y, z_min]` per
  model.  This is the only correct source: idgate01's leaves are STORED
  mid-open (nowhere near the doorway; they swing 90° shut), its static side
  grates span 269u where the keyed leaves close to 133u, and the converted
  collision (the previous source) additionally baked the leaf transforms
  wrong — which is what rotated the CharacterGen pen gate's Door Triangle
  90° and put a corner at the door centre.  z_min (the closed slab's base)
  also replaces the whole-NIF bounds z-min as the pivot→floor drop.
* **Attach completion ladder** (in order, each a measured failure): stitch →
  T-junction split → apex bridges → **carve** (`_carve_door`: locally remove
  the same-storey triangles overlapping the wedge, retriangulate the region
  minus the wedge from their own vertices with the kept-mesh boundary edges
  and the wedge's edges forced back — the last resort when another sheet's
  uncut ground covers the doorway) → **door-to-door bridge** (a 2-triangle
  strip to the nearest other door triangle: a room with doors but no
  pathgrid, the CharacterGen pen, keeps nothing else to attach to) →
  withdraw.  The attach runs TWO passes so a door with nothing to attach to
  can succeed once its neighbours attached.
* **Known limitation**: pathgrid-less interiors (prison pens, closets) keep
  only door triangles plus their bridges — thin but traversable door to
  door; and a door no pathgrid approaches within 384u still gets no
  triangle.

### Door threshold quads (Door Triangles done right)

`spanmesh._stamp_door_quads`: every door REFR (teleport AND interior) gets an
exact oriented quad (DOOR_QUAD_HALF_WIDTH 48 × HALF_DEPTH 32, rotated by the
door's RotZ) stamped into the RAW voxel mesh — vertices inside the rect snap to
its 4 corners, which are then PINNED through decimation. Must happen
pre-decimation: afterwards triangles are bigger than the rect and there is
nothing to snap. `pgrd_to_navm._build_door_links` then links the triangle
CONTAINING the door point at the door's height (fallback: old nearest-centroid
cost). Result: two clean triangles precisely straddling every threshold.

#### 🔴 Snapping FOLDS triangles — restore winding (found 2026-07-22)

Snapping pulls several distinct vertices onto the 4 rect corners. A triangle
STRADDLING the rect boundary can have two of its corners pulled to *different*
corners, which **reverses its winding** — the remap preserved the original index
order and never rechecked. This was the ONLY source of downfacing triangles in
the entire generator, and (because a folded triangle is inverted relative to its
neighbours) the dominant source of CK `OPPOSITE_NORMALS` too.

Measured with `temp/wind_probe3.py`: the raw mesh is always clean
(`pre_stamp=0`) and the stamp injected 6 / 14 / 12 downfacing triangles into
XPAichan01 / SancreTor03 / ArkvedsTower04. Classification proved **zero** came
from the stamped quads themselves (that CCW emission is correct) — all were
pre-existing, previously up-facing triangles.

Fix: record each triangle's XY orientation BEFORE remapping and swap two indices
if the remap reversed it. `|nz|/2` is the XY-projected area, so the sign of the
2D cross product is the facing test. Triangle counts are unchanged (nothing is
dropped) — 943 DOWNFACING and most of 1,516 OPPOSITE_NORMALS went to zero.

Two smaller sources found alongside it:
- **Zero-XY-footprint slivers.** A triangle in an exactly vertical plane covers
  no ground (XY area 0.0000, `nz == 0` so invisible to a `nz < 0` test), yet a
  coplanar pair reads as OPPOSITE_NORMALS because their normals are antiparallel
  in XY. `_drop_steep_triangles` kept them: they are steep but their z-span is
  riser-sized, well under the `2.5 * MAX_CLIMB` gate. Now dropped by
  `MIN_XY_FOOTPRINT` (1.0u², far below one voxel quad, so only the genuinely
  degenerate-in-plan case goes). Example: Ondo tris 1445/1447, all six vertices
  at y=48.0 exactly.
- **Decimation drift.** The C++ collapse/flip/smooth guards were only
  RELATIVE (`new · old > 0`), so a triangle could rotate up to 90° per move and
  walk from up-facing to down-facing across passes without any single move
  tripping the guard. Added an absolute `nz >= 0` invariant to all three passes
  in `native/src/decimate.cpp` (rebuild with `python native/build.py`).

Verified on all 16 worst-offending cells from the shipped ESM (10 interior +
6 exterior): every one now reports CLEAN under `tools/navmesh/check.py`'s rules,
with coverage/steep/island metrics unchanged.

### Exterior coverage (the "discontinuities with no obstacles" fixes)

- **Reach**: `PGRD_XY_REACH_EXTERIOR` (8192) replaces the interior 384u gate
  outdoors — vanilla exterior navmeshes cover essentially the whole cell, and
  the tight gate carved open terrain into blobs around the road pathgrid.
  Geodesic flooding still can't climb >MAX_CLIMB per step or reach roofs.
- **Ledge spread test scales with cs**: `filter_ledge_spans`' steep-slope test
  `(max_drop - min_drop) > lim` must use `lim = max(MAX_CLIMB,
  2*cs*tan(MAX_SLOPE_DEG))`. With raw MAX_CLIMB at CS_EXTERIOR=32 it un-walked
  every hillside steeper than ~28° (2·32·tan28°≈34) — the mystery holes in open
  terrain. At CS=16 the scaled value equals MAX_CLIMB, so interiors unchanged.
- **Cell borders**: a neighbour column outside the exterior cell's LAND is
  unknown terrain (it continues in the next cell), NOT a cliff — treating it as
  a drop un-walked the border row and left a 2-column gap on every cell seam
  (`ext_rect` threading through `apply_filters`).

### Geometry cache (the import-time fix)

`pgrd_to_navm` caches built `(verts, tris)` per cell in
`export/<plugin>/navmesh_geom_cache/*.pkl` (float32/int32 arrays), keyed by a
sha1 of exactly what geometry consumes: pathgrid points/edges, per-REFR
(name, resolved model key, pos/rot/scale, XTEL), doors, LAND VHGT, origin, and
a TAG hashing the navmesh sources + collision-cache identity
(`import_main._navmesh_geom_cache`). Any code/param edit self-invalidates —
no version constant to forget (deliberate: stale caches must never explain a
bug). Warm hit ≈ 0.03s vs seconds; fresh builds round verts to float32 first so
cache hits are byte-identical to cold builds. FormID-dependent parts (NVNM
parent, door links, ONAM, water flags) are recomputed every run so load-order
changes can't bake in.

### Mesh the SPAN GRAPH, never contours (the decisive fix)

A contour is a **height map** — one Z per (cx,cy) column — and a building is not.
A staircase carries an NPC *over* the room below it, and a house stacks two
storeys in the same columns. The old contour mesher tried to slice the world into
height-map "layers" and contour each; every defect came from the seams:

- a staircase peeled into 5 layers, each contoured alone, each an island joined to
  the next only at a triangle **corner** (an NPC cannot cross that);
- a layer boundary falling between two floors let the triangulator bridge them —
  a wall of near-vertical triangles "connecting" storey 1 to storey 2;
- a short pathgrid stub became its own layer and was culled for being small,
  leaving a pathgrid line with **no navmesh under it**.

Tuning the slicer traded these defects for one another indefinitely. `spanmesh.py`
instead meshes the span graph: the unit is a **span**, not a column
(`node=(cx,cy,span_index)`, `adjacent = neighbouring column && |Δtop| ≤ MAX_CLIMB`),
one quad per span, and **adjacent spans share corner vertices**. Connectivity is
therefore structural — nothing to stitch, weld or repair — and two spans a storey
apart are simply never adjacent, so a cross-floor triangle is *unrepresentable*.
Result over 150 interior cells: **0 wrong-floor, 0 steep, 0.9% of pathgrid length
uncovered** (was 2.5% uncovered / 2452 broken pathgrid edges with contours).

- **Quality invariants** (`tools/navmesh/audit.py --interiors N` sweeps many cells
  in parallel; `tools/navmesh/tri_check.py --cell <id>` for one). The metric that matters is
  **BROKEN PATHGRID EDGES** — an edge whose two ends land on navmesh an NPC cannot
  cross between. A raw component count is NOT a bug metric: a cave with six
  chambers this cell's pathgrid never links is legitimately six components.
  Erosion uses a EUCLIDEAN distance transform (scipy `distance_transform_edt`),
  NOT a chamfer — a chamfer overestimates diagonal distance ~1.7x and left wide
  dead zones around obstacles.
- **Decimation must bound triangle QUALITY, not just planarity.** A vertex in the
  middle of a flat floor is coplanar with all its neighbours, so a purely planar
  collapse test drags it clear across the room and the floor degenerates into a
  fan of long thin slivers. Bound the aspect ratio and the edge length too.
  **And the EDGE RATIO (2026-07-17)**: aspect (`longest²/4·area`) alone passes a
  16u voxel edge with two ~100u edges (aspect ≈3, healthy area) — the "one side
  way shorter than the others" needles radiating from wall corners.
  `MAX_EDGE_RATIO` (4) bounds `longest/shortest` on every move, non-worsening
  (a move that improves an existing needle is still allowed, else voxel-scale
  needles freeze in place). The needles' SEED was outline notches: a boundary
  vertex whose boundary edge is shorter than ~1 cell is quantization noise, so
  it may absorb up to `0.9*cs` of outline error instead of MAX_SIMPLIFY_ERR
  (the true wall is within half a cell of either position). Together: RATIO
  defects 113-281/cell → 0 across the test set, and 20-40% fewer triangles.
- **Obstruction is decided in WORLD SPACE, never per-mesh.** An object obstructs
  iff it rises more than MAX_CLIMB above the floor beneath it — so rugs/pillows
  are walked over, tables/barrels are routed around, with NO size gate or rug
  list. Collision meshes are ORIGIN-CENTERED, so any per-mesh height rule is
  meaningless (a table's local extent says nothing about how high it stands).
- **Collision cache**: `asset_convert/collision_extract.py` reads the CONVERTED
  `output/.../meshes/tes4/**.nif` (collision is root-mounted there; the CMS is a
  flat triangle soup — no NiNode-transform walk needed). `scan_collision` →
  `export/<plugin>/collision_cache.bin` (binary, ~15MB, ~2 min one-time).
  Scales: CMS ×70, primitives (box/convex/capsule/sphere) ×10 — both measured
  exactly. Layer gate keeps only OL_STATIC/ANIM_STATIC/TERRAIN/GROUND/STAIRS.
- **REFR rotation is the TRANSPOSE** of the naive Rz@Ry@Rx product (the engine
  inverse-applies the stored rotation). The old code applied only RotZ and
  mis-oriented every ramp; the non-transposed full matrix put Anvil FG's floor
  shell ~180° backwards from its furniture. `world._rot_matrix`.
- **Door handling** (`_collect_doors`, `_build_door_links`): a door REFR is
  teleport (`XTEL.Door`) or interior-only (base in the DOOR set). BOTH get a Door
  Triangle linking the tri straddling the threshold line. The doorway is choked
  naturally now by the door frame's own collision — no jamb hack. Door CRC
  "PathingDoor" = `0xE48B73F3`. **Limitation**: cross-cell Portal Edge Links are
  not computed.
- **Base-model index**: `_build_base_model_index(by_type)` in import_main maps
  raw low-24 base FormID → `tes4/...nif` key, only for blocking base types. REFR
  exports position as `PosX/PosY/PosZ` + `RotX/RotY/RotZ` + `XSCL.Scale`, base as
  `NAME`.
- **Triangle flags** (wbDefinitionsTES5.pas): every generated tri sets
  `0x0800 Found`; water tris add `0x0200`, door-linked tris add `0x0400`. No Edge
  Links, empty Cover Triangles.
- **LAND VHGT decode** (`world.decode_vhgt`): offset float + 33×33 SIGNED int8
  gradients; BOTH the offset and the accumulated deltas scale by 8:
  `(cumsum(deltas) + offset) * 8`. The old code did `offset/8` in and `*8` out,
  which annihilated the offset and put exterior terrain ~16,700u below its own
  REFRs (Tamriel 47,6: terrain 829..3213 vs objects 18288..19776). This was the
  dominant coverage bug (pathgrid-on-floor 32%→92%).
- **Iteration tools**: `python tools/navmesh/render.py <cell> [--collision]`
  renders the generated navmesh (green) OVER the collision layer — walkable dim,
  BLOCKING/walls RED — plus pathgrid and door markers (cyan threshold lines;
  white core = teleport door). `--focus X,Y --span N` zooms a world-coord
  window; `--ids` labels triangle indices + vertex heights; `--quality`
  colours steep triangles red and needles magenta. Exterior cells can be
  addressed as `--cell grid:X:Y` (colon form survives comma-list splitting;
  Windows filenames can't hold `:` so outputs sanitize it).
  `tools/navmesh/tri_check.py --cell A,B,...` checks EVERY triangle of a
  cell's mesh (slope/zspan/edge-ratio/aspect/area + JUT/SINK = signed distance
  off the real collision surface at its own XY) and lists offenders — the way
  the furniture-hoist and needle defects were found and verified fixed.
  `tools/navmesh/probe.py --cell X` reports pathgrid-on-floor coverage and Z
  error; `--probe X,Y` dumps nearby REFRs/pathgrid plus the span column
  raw/stamped/filtered — the ground-truth view of any one spot.
  `tools/navmesh/audit.py --interiors N --exteriors M` sweeps both cell kinds
  and reports UNCOV%/BROKEN/STEEP/FLOOR/ISL/TINY/SLIV%/MICRO per cell (UNCOV
  measures the EDGE's z-range, not the chord — the generator follows the
  surface, and a long cave edge's chord cuts open air two storeys up).
  `tools/navmesh/perf.py --cell X` cProfiles one cell's build (how the
  shadowed()/plane_err hotspots were found).
- **NVNM binary layout** (validated byte-exact against Skyrim.esm via
  `tools/navmesh/dump.py`): all arrays use U32 count prefixes; CRC of
  "PathingCell" = `0xA5E9A03C`; parent union decided by (Parent Worldspace==0)
  → interior = FormID Parent Cell, exterior = `S16 Grid Y` then `S16 Grid X`;
  `Max X/Y Distance` = bbox span / divisor; NavMeshGrid = divisor² arrays each
  `U32 count + count×S16`. Door Triangle struct is **10 bytes** (S16+U32+FormID),
  NOT 12. NAVM record is written with the Compressed flag (0x00040000).
- **NVMI (in NAVI)**: validated byte-exact (57 bytes) vs Skyrim.esm NAVI
  0x00012FB4: `FormID, U32 Category(0=Edited), 3×float centroid, 4B PrefMerge,
  U32 EdgeLink count, U32 PrefEdgeLink count, U32 DoorLink count, U8 IsIsland,
  [island union empty when 0], PathingCell(U32 CRC, FormID WS, parent union)`.
  We emit 0 edge/door links (can't compute cross-navmesh portals from PGRD).
  NAVI has NO EDID; order is `NVER(=12), NVMI…, NVPP(empty: two 0 counts)`.
- **Exterior PGRD/REFR point coords are WORLD coords** (not cell-local) → LAND
  origin = `grid_x*4096, grid_y*4096`.
- **Dependencies**: `numpy` + `scipy` (Delaunay); `mapbox_earcut` used when
  present (fallback ear-clipper otherwise). `shapely` is no longer needed.
- **Performance**: geometry is cached across runs (see Geometry cache above),
  so repeat imports pay ~ms per cell. Cold builds: the 2026-07-15 pass cut
  per-cell CPU ~33% on a 65-cell mix (Wendir02 13.6s→6.1s) by vectorizing
  `_prune_islands.shadowed` (was 45% of the build) and caching per-vertex
  planes in `_collapse_pass` (`vertex_planes`/`plane_dev` with early-out —
  the old code recomputed a full `_tri_shape` per incident triangle per
  collapse candidate).
- **Tests**: `tests/test_pgrd_navm.py` (19 tests: region flood-fill (flat floor,
  two-storey separation, staircase), wall-doesn't-swallow-floor, rug walked over
  vs table routed around, walls contain the mesh, contour orientation,
  triangulation area/holes, VHGT offset, NVNM/NAVI layout).
- **Reusable tool**: `python tools/navmesh/dump.py <esm> [--navi|--navm]
  [--nvnm-decode] [--max N]` — decompresses + decodes real NAVI/NAVM/NVNM for
  format verification (this is how the layout was validated against Skyrim.esm).

### 🔴 Edge Links are MISSING — cross-cell pathing is dead (found 2026-07-20)

`_pack_nvnm` hard-codes the Edge Links count to 0 ("cross-cell links can't be
resolved from PGRD alone"). Measured against Skyrim.esm:

| | exterior NAVM | with edge links | total edge links |
|---|---|---|---|
| VANILLA | 14,440 | **12,145 (84%)** | **194,744** (Portal 190,779 / LedgeUp 1,978 / LedgeDown 1,987) |
| OURS | 5,825 | **0 (0%)** | **0** |

Edge Links stitch adjacent cell navmeshes together. With none, **every cell
navmesh is an isolated island**: an actor paths fine inside its current cell and
can never cross a cell boundary, so any AI package with an out-of-cell
destination starts (the actor stands up, plays its en-route dialogue) and then
never moves. This is game-wide AI breakage — it was found while chasing
"Pinarus/Arielle don't travel" after their PACK records were proven clean by
`tools/pack_validate.py`. Geometry is fine: the destination cell's mesh
(`AnvilWest02`, grid -48,-7) has 1,304 verts / 1,959 tris and **does** cover the
target marker point — it just connects to nothing.

**Binary contract (verified; Skyrim.esm now parses 15,949/15,949 clean):**
- **Edge Link = `Type(U32) + Navmesh(FormID U32) + Triangle(S16)` = 10 bytes.**
  NOT 12 — `navmesh_dump.py` had 12 and silently misparsed every navmesh that has
  links (12,229 vanilla misparses → 0 after the fix). Verified on NAVM 0x00101F28
  (63 links): `00000000 a61a1000 4500 | ... b200 | ... 2901` = three links to
  neighbour 0x00101AA6 at triangles 69/178/297.
- A triangle's **flag bits 0/1/2** = `Edge 0-1 / 1-2 / 2-0 Link`. When bit N is
  set, that triangle's edge-N field is an **INDEX into the Edge Links array**
  instead of a local neighbour-triangle index (xEdit `wbEdgeToStr`,
  wbDefinitionsCommon.pas:3457). Other triangle flags: 3 Deleted, 4 No Large
  Creatures, 5 Overlapping, 6 Preferred, 9 Water, 10 Door, 11 Found.
- **Edge Link Type enum: 0 Portal** (cell seam), 1 Ledge Up, 2 Ledge Down,
  3 Enable/Disable Portal.
- Links are **reciprocal** and go to the four orthogonal neighbours — vanilla
  NAVM 0x00101F29 grid (7,7) has 63 links: (6,7)x15, (8,7)x11, (7,6)x22,
  (7,8)x15, and each neighbour links back the identical count.

**Algorithm to implement** (post-pass, after all cell meshes exist, since it
needs neighbour NAVM FormIDs and final triangle indices — and must stay
deterministic, see the parallelism rules in CLAUDE.md):
1. for each pair of orthogonally adjacent exterior cells, take triangles with a
   border edge (edge field `-1`) lying on the shared seam;
2. match them across the seam by coinciding edge endpoints (with a tolerance);
3. emit reciprocal Portal links on both meshes; on each triangle set flag bit
   `1<<edgeIndex` and replace that edge field with the index into its own Edge
   Links array.

**Audit tool**: `python tools/navmesh/connectivity.py <esm> [--ref Skyrim.esm]
[--cell gx,gy]` — reports exterior link coverage vs the vanilla 84% baseline,
link-type mix, door-triangle counts, and internal consistency between
link-flagged triangle edges and Edge Link entries. Exits non-zero while coverage
is far below vanilla.

### 🔴 Corridor redesign regressed edge links — the ribbons never reach the seam (found 2026-07-23)

The pathgrid-corridor redesign (build.py/corridor*.py, "THE PATHGRID IS THE
MESH") builds one flat ribbon per pathgrid EDGE. But `build_edge_links` matches
triangle border edges lying within `SEAM_BAND` (24u) of the exact cell-boundary
plane, and a corridor ribbon stops at the last pathgrid NODE **inside** the cell
— it never reaches the seam. Result: only **182 edge links across 6,504
exterior meshes**, every exterior cell an island again. Pinarus could leave his
house (interior door works) but couldn't cross a single Anvil grid seam.

**The missing input is PGRI (InterCell).** TES4 PGRD carries, besides the
intra-cell `Point[i].Edge[j]` topology, a **PGRI array of cross-cell links**:
each entry names a LOCAL node and the world-space EXIT point it connects to in a
neighbouring cell. `convert_PGRD` built edges only from `Point.Edge` and ignored
PGRI, so no ribbon ever crossed a boundary.

**Fix (two parts):**
1. **Export bug — PGRI is 16 bytes, not 14, and LocalNode is U32, not U16**
   (UESP TES4 PGRD ref: `Local node number (long)`, then float X/Y/Z of the
   FOREIGN node). The old 14-byte/U16 reading misaligned every entry after the
   first into uninitialised CS memory (denormal floats ~1e-41, node indices like
   17306). Fixed in `tes4_export/record_types/world.py::export_PGRD`. This is a
   pure-dump correctness fix — it belongs in the export, per CLAUDE.md.
2. **Import — build a cross-seam ribbon per valid PGRI link.**
   `pgrd_to_navm._collect_intercell` parses PGRI, drops residual garbage
   (LocalNode out of range, `(0,0,~0)` padding, non-finite / far-away exits),
   and for each survivor appends a synthetic node at the exit point plus an edge
   LocalNode→exit. The ribbon then physically crosses the boundary plane. To keep
   each mesh inside its own cell, `corridor_union.build_union_mesh` takes a
   `cell_bounds` rectangle (exterior only) and **clips the unioned coverage to it
   with shapely** before triangulating — leaving a clean border edge exactly on
   the seam for `build_edge_links` to stitch. Chosen over extending geometry into
   the neighbour cell (the "clip at seam, links only" model).

**Verified** on the 8 Anvil cells around Pinarus (worldspace 0x0001C31A, grid
x −48..−46, y −9..−7) via `tools/navmesh/seam_probe.py`: before = 8 isolated
islands; after = **104 reciprocal Portal links, all 8 cells in ONE connected
component**. InterCell yield jumped with the export fix (e.g. grid (−47,−8):
30→42 of 58 kept; total portals in the patch 30→104). `tools/navmesh/seam_probe.py
--wrld <hex> --gx lo hi --gy lo hi` reports per-cell seam-edge counts, InterCell
kept/raw, reciprocity, and the connected-component structure for a cell range —
use it to spot-check a region without a full rebuild.

### 🔴 NAVI is a SINGLETON override + must mirror connectivity (found 2026-07-21)

The edge-link stitching above was necessary but NOT sufficient — Arielle
(MG04, destination in her OWN cell, mesh verified connected across the stairs
by `tools/navmesh/reach.py`) still never walked. Two more defects in the NAVI
record itself, both now fixed:

1. **NAVI must be written as an OVERRIDE of Skyrim.esm's `0x00012FB4`.** The
   Navmesh Info Map is a singleton the engine resolves by that fixed FormID;
   every DLC registers its navmeshes by overriding it with its own NVMI set
   (Update 251, Dawnguard 1873, HearthFires 132, Dragonborn 1732 entries) and
   the engine merges the per-file overrides. We allocated a FRESH FormID
   (0x011930C9), producing a NAVI the engine never consults — **none of our
   8,156 navmeshes were registered, so no converted NPC could pathfind
   anywhere, even inside a single connected mesh.** Loaded actors with a valid
   package just stood; the only movement left was the engine's off-screen
   teleport failsafe (exactly the reported symptom: Arielle occasionally
   "teleported" to her destination, Pinarus never moved even when console-
   teleported outdoors). `navi_builder.NAVI_SINGLETON_FID`.

2. **Every NVMI entry declared zero connectivity.** Contract verified against
   ALL 15,462 Skyrim.esm NVMI entries:
   - `Edge Links` ∪ `Preferred Edge Links` == the distinct neighbour meshes in
     that navmesh's own NVNM Edge Link array, **self-links excluded** (the 347
     non-matching entries differ only by a self-link). We emit all of them as
     plain Edge Links.
   - `Door Links` == the door REFRs of that navmesh's own NVNM Door Triangles
     (15,462/15,462 exact), CRC `"PathingDoor"` = 0xE48B73F3. Each side of a
     load door lists only its own door ref; the engine joins the two meshes via
     the doors' XTEL pairing — this is what carries an actor through ANY load
     door (interior→exterior, city gates between worldspaces).
   - The U32 after the FormID is **Flags** (0x20 = Is Island + island-data
     union, 0x40 = Not Edited), not a "category"; island data is OPTIONAL
     (305 vanilla entries have no links and no island data). We write 0.
   Plumbing: `pgrd_to_navm` puts `door_refs` on the meta;
   `navm_edge_links.build_edge_links` puts `edge_link_fids` on the meta (for
   every exterior view, dirty or not); `navi_builder._pack_nvmi` mirrors both.

**Also matched vanilla**: top-group order places NAVI *before* CELL/WRLD
(Skyrim.esm order `... REGN NAVI CELL WRLD DIAL QUST ...`) — the engine fixes
up NVMI's forward NAVM references lazily, unlike QUST ALFR. Vanilla NVMI is
NOT sorted on disk (7,790 out-of-order adjacent pairs in Skyrim.esm), so entry
order is free.

**NVPP must be carried forward.** Every vanilla master's 0x12FB4 override
ships a FULL 25,696-byte NVPP (Skyrim/Update/Dawnguard/HearthFires/Dragonborn
each carry their own edited copy of the same 100-path table). Our override is
the winning one, so an empty NVPP would replace the vanilla precomputed-path/
road network. `navi_builder.read_master_nvpp` re-ships the newest vanilla blob
from the registry-detected SSE install.

**FormID-allocation stability.** The old code allocated the NAVI's FormID from
`writer.alloc_formid()`; switching to the fixed singleton id removed that
allocation and SHIFTED every later-allocated FormID (all generated
DIAL/INFO/DLBR/DLVW/LCTN/SNDR records — thousands) relative to previously
shipped builds, which scrambles any existing save's script/dialogue state. The
import now burns one id at the same point to keep the layout stable. Never
add or remove an `alloc_formid()` call without accounting for this.

**Reachability tool**: `python tools/navmesh/reach.py <esm> --from-ref <fid>
--to-ref <fid> [--cell <fid> --components]` — decodes every NAVM, builds the
(mesh, component) graph over NVNM edge links + door-XTEL joins, locates both
endpoints, and answers REACHABLE yes/no with component/z-range detail. This is
what proved Arielle's cell mesh was fine and pushed the investigation to the
NAVI layer.

**Exterior door triangles need the worldspace's PERSISTENT doors (2026-07-21).**
Exterior teleport doors (house entrances, city gates) are persistent REFRs
parented to the worldspace's persistent *dummy* cell, not to the grid cell they
physically stand in — so the per-cell refr list never contained them and only
89/6,516 exterior meshes had door triangles (interiors: 1,612/1,640). Pinarus's
exit chain died on the Anvil street side of his own front door.
`_gather_navm_jobs` now buckets each worldspace's persistent door refs by the
grid square their POSITION falls in and passes them to that cell's job as
`extra_door_refrs` (convert_PGRD feeds them to the door threshold stamp +
door-triangle linking only). The doors are part of `_geom_hash`, so affected
exterior cells regenerate automatically.

## LAND Record Structure

Both TES4 and TES5 use `wbLandscapeLayers` from wbDefinitionsCommon.pas. The "Layers" array is a FLAT array of Layer entries where each is EITHER a Base Layer (BTXT) OR an Alpha Layer (ATXT+VTXT) — they are NOT nested.

### Export Format
```
LayerCount=N
Layer[i].Type=BASE|ALPHA
Layer[i].BTXT.Texture=FormID    # BASE only
Layer[i].BTXT.Quadrant=0-3      # BASE only
Layer[i].ATXT.Texture=FormID    # ALPHA only
Layer[i].ATXT.Quadrant=0-3      # ALPHA only
Layer[i].ATXT.Layer=N            # ALPHA only
Layer[i].VTXTCount=K             # ALPHA only
Layer[i].VT[k].Pos=posval        # ALPHA only
Layer[i].VT[k].Op=opval          # ALPHA only
VTEXCount=N
VTEX[i]=FormID
```

### Import Notes
- `ElementAssign(layers, HighInteger, nil, False)` creates a default Base Layer (BTXT)
- For Alpha Layers: remove BTXT via `RemoveElement`, then add ATXT + VTXT
- VTXT structured data only available when `wbSimpleRecords = False`; raw byte array otherwise
- **Alpha layer numbers must be per-quadrant sequential (0,1,2…), NOT the TES4 original values**
- **Skip alpha layers with Texture FormID = 0** — they cause visual artifacts in TES5
- **Max 8 alpha layers per quadrant** in TES5. Skyblivion uses 5 but engine supports 8.
- VTXT export field is `VT[k].Op` but import uses `VT[k].Opacity` — use Opacity in import
- Exterior cell block grouping: block = `floor(grid / 32)`, sub-block = `floor(grid / 8)`. Use Python `//` (floor division), NOT bitwise `>>` — the `>>` formula is wrong for exact negative multiples (e.g. -32 gives -2 instead of -1).
- Persistent worldspace cell classification: use `RecordFlags & 0x400`, NOT `XCLC.X == ''`. Persistent cells often have XCLC=(0,0) so the empty-string check mis-classifies them as exterior cells, putting them in the wrong block/sub-block structure and breaking all exterior cell loading.

### 🔴 The texture PRUNE must speak the importer's paths (2026-08-09)

Cause of "almost all landscape textures are missing" on Nehrim. An Oblivion
LTEX `ICON` is relative to `Textures\Landscape\`, and the importer prepends
`landscape\` (`record_types/world.py:111`). `texture_prune.refs_from_records`
did not: it kept `oblivion/terrainhd…dds` and `tes4/oblivion/terrainhd…dds`
while the plugin asks for `tes4/landscape/oblivion/terrainhd…dds`. Nothing
matched, so **every LTEX texture was pruned as unused and never packed.**

Measured on the shipped build: **252 of 484 referenced LTEX texture slots were
in no BSA, all of them still on disk.** The survivors were exactly the 116 the
MESH manifest happened to name — a texture a mesh also used survived, which is
why *some* terrain was textured and most was not. The LTEX records themselves
were fine (229/242 resolve, 0 dangling LAND layers), which is what makes this
so easy to misdiagnose: every record-level check passes.

`refs_from_records` now carries a per-signature prefix table
(`_RECORD_TEX_PREFIX`), keyed on the export filename since that is the only
place the record type is known. **Any record type whose texture field is
relative to a subfolder has to be listed there**, and it must mirror whatever
the importer prepends. Guarded by `tests/test_texture_prune.py`.

Diagnose with `python tools/ltex_check.py [plugin]` (LTEX → TXST → file, plus
dangling LAND layers). Note `refs_from_records` regex-scans **every** `.txt` in
the export — ~2 GB for Nehrim, so it takes minutes; that is not a hang.

### TXST for Landscape Textures
- No DNAM: vanilla Skyrim LTEX TXSTs omit DNAM. The 0x0001Fa "No Specular Map" flag only applies to the object (BSLightingShader) path, NOT the landscape shader. Writing it has no positive effect.
- TX00 = diffuse (`tes4\landscape\<icon>.dds`)
- TX01 = normal map (`tes4\landscape\<icon>_n.dds`)
- LTEX SNAM specular exponent: **pass through the TES4 value**. SNAM is a Phong exponent used directly by the landscape shader. Setting SNAM=0 gives `pow(NdotH, 0) = 1.0` everywhere → whole landscape appears blindingly bright white. TES4 landscape textures use ~30 (moderate gloss). Do NOT write SNAM=0.

## OBND (Object Bounds) defaults
- ESM records without OBND crash the engine. Import script generates per-type defaults:
  - MISC=(-5,-5,0,5,5,8), KEYM=(-3,-3,0,3,3,3), WEAP=(-5,-5,0,5,5,30), STAT=(-50,-50,0,50,50,80)
  - ARMO=(-15,-10,0,15,10,30), NPC_/CREA=(-12,-12,0,12,12,60), LIGH=(-6,-6,0,6,6,20)
  - Other types get (-5,-5,0,5,5,5) as fallback
