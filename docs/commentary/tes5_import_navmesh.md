# tes5_import/navmesh/ - PGRD to NAVM, LAND and worldspace

**Code:** `tes5_import/import_main.py`, `asset_convert/collision_extract.py`, `asset_convert/worldmap_clouds.py`, `tes4_export/record_types/world.py`

## Contents

- [World / LAND / PGRD→NAVM Conversion Notes](#world-land-pgrdnavm-conversion-notes)
- [PGRD → NAVM/NAVI Conversion (PathGrid → NavMesh)](#pgrd-navmnavi-conversion)
- [LAND Record Structure](#land-record-structure)
- [OBND (Object Bounds) defaults](#obnd-defaults)
- [The world-map camera clamp — MNAM's cell rectangle (verified by disassembly)](#world-map-camera-clamp-mnams)
- [World-map cloud banks (WRLD MODL) — sized to the LAND](#world-map-cloud-banks-sized)
- [The shared navmesh cache — design rationale](#shared-navmesh-cache-design-rationale)
- [Navmesh redesign: pathgrid corridor ribbons](#navmesh-redesign-pathgrid-corridor-ribbons)
- [Baseline before the rewrite (historical — verified 2026-07-23)](#baseline-before-rewrite)
- [Why replace it](#why-replace)
- [Author-set principles (do not violate)](#author-set-principles)
- [What stays exactly as-is](#what-stays-exactly-as)
- [Phase 1 — corridors + doors + links (a complete, narrow navmesh)](#phase-1-corridors-doors-links)
- [Phase 2 — grow width to walls (deferred, sketch only)](#phase-2-grow-width-walls)
- [Phase 3 — polish (deferred, sketch only)](#phase-3-polish)
- [Decisions made (author) and open questions](#decisions-made-open-questions)
- [Risk register](#risk-register)
- [Connectivity invariant: status 2026-07-25](#connectivity-invariant-status)
- [The triangle-quality contract (2026-08-04)](#triangle-quality-contract)

## World / LAND / PGRD→NAVM Conversion Notes
<a id="world-land-pgrdnavm-conversion-notes"></a>

Linked from [CLAUDE.md](../../CLAUDE.md). Covers pathgrid→navmesh conversion and
LAND/landscape-texture record structure. For terrain LOD generation see
[nif_conversion_notes.md](asset_convert_terrain.md#terrainlodland-adjacent-asset-notes).

## PGRD → NAVM/NAVI Conversion (PathGrid → NavMesh)
<a id="pgrd-navmnavi-conversion"></a>

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
> [navmesh_corridor_redesign.md](tes5_import_navmesh.md) (implemented),
> tuned per [performance_notes.md](performance.md).
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
cached to `door_panel_axis_cache.json` by `tools/generators/build_door_axis_cache.py`:

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
  `tools/generators/build_door_axis_cache.py` from `export/<plugin>/meshes`; the
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

<a id="storey-grouping-by-connectivity"></a>**Storey grouping is by CONNECTIVITY, not a Z threshold** (`corridor_union._storey_groups`). Flattening a cell into one 2D union bridges an upper and a lower floor that overlap in plan, producing triangles with corners on two floors at once. A Z threshold cannot separate them either, because a STAIRCASE legitimately spans two floors. So two ribbons join the same storey when they share a pathgrid NODE and their heights AT THAT NODE agree within `SAME_SURFACE_Z`: a stair joins the floor at its foot and the floor at its head, merging all three, while two floors that merely overlap in plan and share no node never merge.

### Door sheet matching: the area tie-break is GONE (2026-08-30)

**Code:** `corridor_union._storey_groups`.

The door-to-sheet match had an area tie-break -- "prefer the group the door
overlaps MOST" -- that NEVER ran once from commit 31c2594 (2026-07-24).
`unary_union` is imported locally inside functions throughout
`corridor_union.py`, never at module level, so both call sites raised
`NameError` inside a bare `except`: `group_polys` filled with `None`, every
`area` stayed `0.0`, and the key `(-area, hz)` collapsed to the HEIGHT
tie-break alone.  Ruff `--select F821` is what surfaced it.

**It is now DELETED**, along with `group_polys`: the code says what it does,
which is match each door to the overlapping group nearest in height.  Output
is byte-identical over 90 door-heavy cells, because that is what the engine
was already doing.

**Repairing it instead was tried and REJECTED.** Supplying the missing import
was measured over 190 door-heavy cells (1,529 doors): exactly ONE changed,
`Vilverin02` at 2026 -> 2021 tris, with identical coverage, one component and
CK-rule CLEAN -- but **crack edges rose 15 -> 16**.  A crack is a boundary edge
a walked pathgrid line crosses, an adjacency break the engine cannot path
across, so five fewer triangles does not pay for it.

The `else` branch (a door matching no group becomes its own) is NOT dead: it
fires in ChorrolFightersGuild, and deleting it cost 2 triangles there.

<a id="uniform-hex-lattice-triangulation"></a>**Uniform triangulation is a hex lattice + centroid-inside Delaunay** (`corridor_union._triangulate`). The old approach earcut the polygon after cutting it on an 8u grid and produced needles and slivers along every boundary -- **20% of triangles had an edge ratio > 3, some > 400**. Vanilla Skyrim navmeshes are near-uniform ~`target_edge` triangles, so: (1) sample interior Steiner points on a HEX lattice at `target_edge` spacing -- hex, not a square grid, so the Delaunay is near-equilateral rather than right-isoceles; (2) densify boundary rings at the same spacing so boundary triangles match interior scale; (3) Delaunay the whole point set and keep only triangles whose CENTROID lies inside the polygon, which honours the outline and every hole exactly without a constrained triangulator.

`steep_seeds` are points along STEEP ribbon centrelines (stairs, ramps). A uniform `target_edge` triangle on a staircase climbs more than one storey gap across its corners and is dropped by the per-surface emission -- the whole stair vanishes. The seeds are forced in at fine spacing so the stair keeps short, gently-climbing triangles.

<a id="door-triangle-is-reserved-as-a-hole"></a>**The door triangle is RESERVED as a hole, not coaxed out of the Delaunay** (`corridor_union._triangulate`). Vanilla marks a door with ONE triangle whose long edge is the whole doorway. Every attempt to get that from the triangulator failed the same way: the door line lies on the union BOUNDARY, so the ribbon's own outline corners land on it and split it into 3-4 pieces, and no amount of seeding, keep-out or constraint recovery can remove a corner already baked into the polygon. So the region is cut OUT of the polygon before triangulation -- the triangulator fills around it as a hole and cannot subdivide what it never sees. The wedge's shape is fixed by `corridor_doors` (full doorway base, deterministic depth, apex on the pathgrid's side); the reservation never moves, shrinks or flips it. A wedge that severs the sheet is allowed -- the MultiPolygon branch triangulates every significant piece. The old guard that SKIPPED reserving such a door instead demoted it to whatever sliver the fallback containing-triangle link happened to find.

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

  <a id="needle-split-must-improve-the-worst-shape"></a>**A needle split must
  IMPROVE the local worst shape** (`corridor_clean._split_needles`). Bisecting
  a triangle whose SHORT edge is tiny halves the long side but keeps the tiny
  side, so the split is strictly worse: measured, two r=11 slivers appeared
  where one r=4 had been. The pass therefore computes the worst badness over
  the edge's owners before and after and abandons the split unless it strictly
  drops — `after >= before` means leave the needle alone.
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
`tools/esm/pack_validate.py`. Geometry is fine: the destination cell's mesh
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

**The NAVI takes the fixed singleton id** `0x00012FB4`, not a generated one.
Generated ids are hashed from their source record, so removing the NAVI's own
allocation moves nothing else — an earlier burn-one-id workaround, needed when
ids came from a positional counter, is gone.

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
<a id="land-record-structure"></a>

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
- …but a NON-persistent cell with no XCLC is **not** a persistent cell: it is a real exterior cell at grid (0,0) whose coords Oblivion omitted. Stamp `XCLC=(0,0)` and leave it in the block tree — moving it out punches a null grid hole. See below.

### 🔴 A worldspace CELL with no XCLC is a real (0,0) cell — STAMP IT (2026-08-10)

Crash `crash-2026-08-09-23-15-19` / `-23-34-53` / `crash-2026-08-10-00-00-48`,
all byte-identical: `EXCEPTION_ACCESS_VIOLATION` at `SkyrimSE.exe+050E6AD`,
`mov rbx, [rax+rcx*8]` with `rax=0`, on a `BSJobs::JobThread`, streaming
`OblivionMQKvatchEntrance` in `Plane of Oblivion`.

**Oblivion omits XCLC when a cell sits at grid (0,0)** — an absent subrecord
already reads as 0, so the CS never wrote one. Skyrim does not tolerate the
omission: it builds its grid-cell array by walking the type-4/5 block tree and
reading each cell's XCLC, so a cell without one never occupies its slot. The
slot stays null while all four neighbours are live, and the streaming tick
indexes it **without a bounds check** — an allocated grid array is an assumed
invariant.

30 cells are affected: `OblivionMQKvatchBridge` (60 refs), `MQ14OblivionGate`
(34), `CheydinhalOblivion` (19), `DABoethiaStatue` (21), every IC district,
MQ16, DreamWorld. **100% of every one of their refs floors to grid (0,0)**
(`floor(pos / 4096)`), so stamping `XCLC=(0,0)` is faithful, not a patch.

Fix: `_ensure_cell_grid()` in `import_main.py` stamps the default on any
non-persistent exterior cell lacking XCLC, before both `convert_CELL` and the
block/sub-block bucketing — and `_gather_navm_jobs` calls it too, or the two
passes disagree about which block a cell belongs to.

**A dead end worth recording: REMOVING these cells from the block tree makes it
worse.** That was the first attempt here — it looked right (vanilla is 0 of
16,942 blocked cells without XCLC) but it *punched* the hole instead of filling
it. The diagnostic that settles it is counting **enclosed grid holes** (a
missing (x,y) whose four neighbours all exist):

| | enclosed holes |
|---|---|
| vanilla Skyrim.esm | 2, at arbitrary coords (WindhelmWorld, KatariahWorld) |
| ours, after removing the cells | 22, **every one at exactly (0,0)** |
| ours, after stamping XCLC | 0 |

Holes are legal in general; a hole at (0,0) is the signature of this bug.
Guarded by `tests/test_import.py::TestGridlessWorldspaceCellPlacement` and
checkable with `tools/validate/cell_grid_check.py --holes`.

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

Diagnose by walking LAND → LTEX → TNAM → TXST → TX00 → `.dds` (the old
`ltex_check.py` did this; removed 2026-08-25 as a one-plugin script) —
dangling LAND layers).

**Why this hid for so long:** the keep-set is applied by `bsa_pack` when the
textures archive is staged, and the mesh phase re-copies the whole texture tree
into `output/` on every run. A full pipeline run therefore always has the
textures back on disk by the time anyone looks, so a wrong keep-set only ever
showed up *inside the BSA* — never as a missing file. It bites hardest on
`--mesh-subdirs` runs, where the manifest names a fraction of the tree.
Corollary: **file mtimes in `output/textures/` prove nothing** about what the
keep-set did — `copy2` preserves the extract cache's timestamps.

`refs_from_records` used to regex-scan every `.txt` in the export (~2 GB for
Nehrim, minutes) because `_TEX_TEXT_RE` opens with a lazy star and expands at
every position on text with no match. `LAND.txt` alone is 1.47 GB on Oblivion
with zero `.dds` in it. A `'.dds' not in body` substring test skips those
outright: **4.2 s** for the whole export.

### TXST for Landscape Textures
- No DNAM: vanilla Skyrim LTEX TXSTs omit DNAM. The 0x0001Fa "No Specular Map" flag only applies to the object (BSLightingShader) path, NOT the landscape shader. Writing it has no positive effect.
- TX00 = diffuse (`tes4\landscape\<icon>.dds`)
- TX01 = normal map (`tes4\landscape\<icon>_n.dds`)
- LTEX SNAM specular exponent: **pass through the TES4 value**. SNAM is a Phong exponent used directly by the landscape shader. Setting SNAM=0 gives `pow(NdotH, 0) = 1.0` everywhere → whole landscape appears blindingly bright white. TES4 landscape textures use ~30 (moderate gloss). Do NOT write SNAM=0.

## OBND (Object Bounds) defaults
<a id="obnd-defaults"></a>
- ESM records without OBND crash the engine. Import script generates per-type defaults:
  - MISC=(-5,-5,0,5,5,8), KEYM=(-3,-3,0,3,3,3), WEAP=(-5,-5,0,5,5,30), STAT=(-50,-50,0,50,50,80)
  - ARMO=(-15,-10,0,15,10,30), NPC_/CREA=(-12,-12,0,12,12,60), LIGH=(-6,-6,0,6,6,20)
  - Other types get (-5,-5,0,5,5,5) as fallback

## The world-map camera clamp — MNAM's cell rectangle (verified by disassembly)
<a id="world-map-camera-clamp-mnams"></a>

How far the world map can SCROLL is set by WRLD `MNAM`'s NW/SE **cell**
rectangle — not by `NAM0`/`NAM9`, and not by any LOD, terrain, `.btr`/`.bto`,
or map-image input. Recovered from `SkyrimSE.exe` (GOG/AE):

`MapCameraStates::World::Update` at RVA **`0x9213e0`** (vtable
`.?AVWorld@MapCameraStates@@` @ `0x17b0fc0`, slot 3) branches at `0x9216ac` on
`MapCamera+0x68`, a border-polygon list, into two **mutually exclusive** clamps:

* **MNAM border polygon (wins whenever present).** Built in the state-enter
  handler at RVA `0x9219f0`. It calls `0x2c7d80`, which walks the parent chain
  (`WRLD+0x158` = WNAM) while `WRLD+0xa2` bit 2 (**PNAM "Use Map Data"**) is
  set and returns `owner+0x188`, the MNAM blob. **If all four MNAM cell int16s
  are zero it jumps to `0x921cb2` and leaves the polygon NULL**; otherwise
  `0x921b20`/`0x921b94`/`0x921c08`/`0x921c7c` build four vertices from
  `NW.X(+8) NW.Y(+0xa) SE.X(+0xc) SE.Y(+0xe)`, each `shl 12` (cells → world
  units). Clamping is a point-in-polygon push-back at `0x921f10`.
* **NAM0/NAM9 box — FALLBACK ONLY,** reached at `0x921717` solely when the
  polygon is NULL (`je 0x921717`). Clamps X into `[WRLD+0x1c0, +0x1c8]` and Y
  into `[+0x1c4, +0x1cc]`, filled by `TESWorldSpace::Load` (`0x2c5620`) via
  `minss` on NAM0 (`0x2c57a2`) and `maxss` on NAM9 (`0x2c591b`). MNAM lands in
  separate storage at `+0x188` and is never read by the box clamp.

`MNAM+0x10/+0x14/+0x18` are Min Height / Max Height / Initial Pitch, matching
xEdit's `wbWorldMapData` "Camera Data" — which validates the offset mapping.
`UsableDimX/Y` participates in nothing here; all 3 Skyrim.esm WRLDs that carry
MNAM write `(0, 0)`, Tamriel included, so we write 0 too.

### A worldspace's rectangle is SHARED, so it is unioned across plugins

🛑 **The last plugin to override a WRLD wins, so one plugin that never touched
the terrain can clamp the map back down.** Ten converted plugins override
Tamriel `0100003C`; only `Tamriel.esp` adds the outer land (99,946 cells, grid
X -192..191, Y -129..159). The other nine measure nothing there, fell back to
Oblivion's authored 119x106-cell rectangle, and three of them are plain ESPs
that therefore load after every ESM — so the widened map reverted to Cyrodiil.
`ElsweyrAnequina.esp` additionally reverted NAM0/NAM9 to cells -64..70.

So the extent is **unioned across every plugin built into an output tree** and
persisted in `output/world_extents.json`
(`tes5_import.import_main._merge_world_extents`), keyed by output-space FormID.
Every plugin emits the same widest rectangle and load order stops mattering.
A narrow measurement can only ever widen the stored box, never shrink it.

`set_world_land_extents` UNIONS for the same reason: it is called twice per
import -- once over every exterior cell before the override pass, and again
from `_build_world_groups` over just the own-hierarchy cells -- so replacing
would let the narrower second call shrink a rectangle the first measured
correctly.

## World-map cloud banks (WRLD MODL) — sized to the LAND
<a id="world-map-cloud-banks-sized"></a>

Skyrim's world map draws a bank of cloud sheets over the terrain. The mesh is
picked by a three-step fallback in the engine (`SkyrimSE.exe` RVA `0x2c7e00`,
the only cross-reference to the string): the PARENT worldspace's cloud model
when WRLD `DATA` bit 2 ("Use Map Data") is set, else this worldspace's own
`MODL` (xEdit's `wbRStruct('Cloud Model', [wbGenericModel])`, written between
DNAM and MNAM), else a HARDCODED `Meshes\Sky\SkyrimWorldMapCloudBank.nif`.

Oblivion has no world-map cloud layer and vanilla Skyrim authors no MODL
either (0 of 35 uncompressed Skyrim.esm WRLDs carry one), so without this every
converted worldspace inherits a bank sized for Skyrim's Tamriel.
`asset_convert/worldmap_clouds.py` emits one per worldspace and points MODL at
`meshes\tes4\worldmapclouds\<edid>.nif` (under `tes4\`, never `sky\`, so a
generated bank can never shadow the vanilla file the weather system loads by
name).

### Size and centre come from the exterior CELL GRID, not from MNAM or NAM0/NAM9

Per axis the deck is given **Bethesda's own deck-to-land ratio** — the stock
910,445 sheet against Skyrim's 487,424 x 385,024 land, i.e. **1.868x on X and
2.365x on Y** — and centred on the land rectangle's midpoint. Feeding Skyrim's
own land back in reproduces the stock scale of exactly **8.0 / 8.0**, which is
the control that validates the rule.

Both inputs are measured from the worldspace's non-persistent exterior cells
(`tes5_import.import_main._land_extents_by_wrld`; cell `(gx,gy)` spans
`gx*4096 .. (gx+1)*4096`). Persistent cells are excluded — they hold the
worldspace's persistent refs, are commonly parked at a dummy `(0,0)`, and drag
the extent toward the origin.

🛑 **MNAM is authored map-camera framing and a converted plugin's can simply be
WRONG about its own terrain.** NehrimWorldspace's MNAM rectangle is centred
26,624 units SOUTH of its land and its north edge clips 16,384 units of real
land off. Sizing or centring off it produces a deck that is both offset and
undersized. MNAM, then NAM0/NAM9, remain fallbacks only for a worldspace that
contributes no cells.

### The sheets must be stretched PER AXIS, and the UVs stretched with them

A NIF node `scale` is a single float, so it can only size the (square) stock
sheet off its longer side. Nehrim's land is portrait (92 x 101 cells) where
Skyrim's is landscape, so a uniform scale hangs far more cloud across the short
axis than the tall one needs. The stretch is therefore baked into the VERTICES
with each node's `scale` set to 1.0 — node scale and vertex scale multiply, so
leaving it at the stock 8.0 applies the factor twice.

🛑 **The clouds are a TEXTURE (`textures\sky\SkyrimCloudsMap01.dds`), not vertex
alpha.** Stretching vertices while leaving UVs alone keeps the cloud pattern
pinned to the same FRACTION of the sheet at any size, so the dense border band
lands wherever it likes relative to the terrain and no amount of resizing moves
it. UVs are scaled by the same world-span factor (`sx / 8.0`, since the
vertices already absorbed the stock node scale — scaling by `sx` directly
over-tiles by 8x). Verified by texel density: units-per-UV must come out
identical to stock (75858 / 94209 / 45791 / 192743 for High/Mid/Top/Low).

Only the horizontal axes are touched. Vertex Z (the sheet's own relief) and the
nodes' Z translations (cloud ALTITUDES: 0 / 1000 / 1500 / 12500) are preserved;
scaling those would sink the deck into terrain or launch it out of frame. The
bounding sphere is updated unconditionally, since X/Y always change.

### Sibling worldspaces: union the LAND, not the WRLD records

`sibling_lod.merge_cloud_bank` writes ONE bank covering every contributor, into
the merged LOD folder that installs last and wins the overwrite deliberately.

🛑 **The union must be measured from each plugin's cells** (`_wrld_land_bounds`
+ `_wrld_formid`, reading XCLC out of the built ESM). A dependent overrides the
master's WRLD record WITHOUT touching MNAM/NAM0/NAM9, so all five TES4Tamriel
contributors report the identical rectangle `X[-241664,245760]` and a
record-based union collapses to the master's 487,424 x 434,176 — against a real
combined land span of **1,572,864 x 1,183,744**, a deck 3.2x too small. That is
exactly the overwrite bug the function exists to prevent.

### Benign: two copies of each bank exist

`create_lod.py` calls `merge_cloud_bank` for every worldspace, including ones
with no contributors, so a solo plugin gets a second copy under
`output/AutoConvertLOD/`. Both resolve to the same Data-relative path and the
LOD copy wins. This is harmless — but note that **`--import-only` refreshes only
the per-plugin copy**, so after an import-side change the game still loads the
older AutoConvertLOD mesh until a LOD run regenerates it. Verify the copy that
actually wins before concluding a cloud-bank change had no effect.

## The shared navmesh cache — design rationale
<a id="shared-navmesh-cache-design-rationale"></a>

Navmesh generation is the slowest import stage. Results are cached per cell in
`export/<plugin>/navmesh_geom_cache/*.pkl` and published as a **GitHub Release
asset** so downloaders don't regenerate them. Not committed (git keeps every
version of a churning binary forever) and not Git LFS (free tier is 1 GB
bandwidth per *month* — about three clones).

Commands are in [CLAUDE.md](../../CLAUDE.md#shared-navmesh-cache).

### GAP (unfixed): the download path ignores `PUBLISHABLE_PLUGINS`

**Measured 2026-08-26.** `auto_install` makes its anonymous releases API call
for **any** plugin, including ones whose cache is never published. The allowlist
that should gate it already exists and is already correct:

```python
PUBLISHABLE_PLUGINS = ('Oblivion.esm', 'Nehrim.esm', 'Morrowind_ob.esm')
def is_publishable(plugin): ...   # case-insensitive
```
([navmesh_cache.py:149](../../tools/navmesh/navmesh_cache.py#L149))

It gates **publishing** — `discover_plugins`
([:171](../../tools/navmesh/navmesh_cache.py#L171)) and the `publish` command
([:1390](../../tools/navmesh/navmesh_cache.py#L1390)) both filter on it — but
**nothing on the download side consults it**. `auto_install`
([:1138](../../tools/navmesh/navmesh_cache.py#L1138)) walks
already-current → drop-ins → `allow_download` → `_api_releases()`
([:1222](../../tools/navmesh/navmesh_cache.py#L1222)) with no plugin-name check
anywhere.

Cost per non-cacheable plugin, per import run: **one wasted API call (~0.5 s
measured)** that can only ever end in "no matching asset". `auto_install` is
invoked once per plugin ([convert.py:662](../../convert.py#L662)), so a user
converting their own mods pays it every run for a lookup guaranteed to miss.

**The fix is a single early return** in `auto_install`, after the
already-up-to-date and drop-in checks but **before** `allow_download` /
`_api_releases()`. Ordering matters:

- Drop-ins must still work for *any* plugin — a user who builds and drops in
  their own zip is a supported path and must not be gated by an allowlist about
  what *we* host.
- Only the **network** step is restricted, so the gate belongs immediately
  before it.

**Verified safe — nothing is lost.** Two off-allowlist assets exist
(`navmesh-cache-DLCBattlehornCastle.zip`, `navmesh-cache-ElsweyrAnequina.zip`),
but they appear on exactly one historical release, `navmesh-cache-0.586-0.586`
(2026-08-11). That range covers 0.586 only; current builds report 0.616, so the
existing version-range gate already rejects them. Every current release
(`0.616+`, `0.609-0.615`, `0.600-0.608`, …) carries only the three allowlisted
plugins.

Keep the user-facing message honest when gating: for a non-hosted plugin the
truth is "no cache is published for this plugin", **not** the existing "could
not reach the releases API" wording, which would be a false diagnosis.

### GitHub anonymous rate limit — measured, not a practical risk

The download path is anonymous (`_api_releases`,
[navmesh_cache.py:307](../../tools/navmesh/navmesh_cache.py#L307)) and GitHub's
anonymous REST limit is **60 requests/hour, counted per source IP** — shared by
everyone behind that IP. Measured 2026-08-26, this is nonetheless fine:

- **A cache install costs exactly ONE API call.** `auto_install` is invoked once
  per plugin per import run ([convert.py:662](../../convert.py#L662)), and its only
  API call is the single `releases?per_page=100` request. Verified: remaining
  went 60 → 59.
- **The asset download itself costs ZERO.** `browser_download_url` redirects to
  `release-assets.githubusercontent.com`, which is outside the API. Verified by
  range-fetching 1 MB of the real 114 MB `navmesh-cache-Oblivion.zip` — API
  remaining was unchanged (56 → 56).
- So a user converting all three plugins spends **3 of 60**. Even a shared
  university/office NAT would need ~20 simultaneous first-time users in one hour
  to exhaust it.
- Today that is **1 call per plugin converted**, not per *cacheable* plugin —
  see the gap above. Gating on `is_publishable` caps it at 3 per run no matter
  how many plugins a user converts.

**On exhaustion it degrades safely, and this is already handled.** A 429 makes
`_api_releases` return `[]`, which the caller treats exactly like being offline:
it prints "could not reach the releases API (offline or blocked); generating
normally", names the manual drop-in route, and regenerates
([navmesh_cache.py:1223](../../tools/navmesh/navmesh_cache.py#L1223)). The cost is
slow generation, never wrong geometry or a failed run.

The one aggravating factor to keep in mind: **any future anonymous API caller
shares this same 60/hour budget** — notably the update check
([version.py:965](../../version.py#L965)) and the planned in-app updater
([in_app_update_plan.md](../plans/in_app_update.md)). That is why the updater's
launch check caches its result rather than polling every start.

**Never ship `collision_cache.bin`.** It maps Oblivion mesh *paths* to verbatim
Havok collision triangles lifted from Bethesda's NIFs — derived asset data keyed
by asset name. Only the generated `navmesh_geom_cache` pickles (hash + verts +
tris + ledges, our own output) go in the archive; the manifest carries a one-way
hash of the collision cache to prove a local build matches. For the same reason
archiving names the cache dir explicitly: globbing `export/**/*.pkl` would sweep
in the ~2.1 GB index pickles.

**Invalidation is per mesh, not per file.** Each cell's hash folds in the
collision digest of only the meshes *that cell places*
(`collision_extract.collision_digest`), so replacing a few meshes costs only the
cells that use them. It used to hash the whole collision file, where one changed
mesh invalidated all ~8,200 Oblivion entries.

**The cache tag must stay machine-independent.** It hashes the navmesh sources
only. It previously folded in `collision_cache.bin`'s *mtime*, which is
machine-local and survives neither git nor an unzip — every downloader computed
a different tag and the shared cache would have missed 100% of the time. Never
reintroduce mtime, absolute paths, or worker counts into any cache key.

**`CACHE_TAG` is written only by a real, failure-free generation pass.**
Computing the tag must never stamp it, or reading the tag would certify a stale
cache as fresh. A stale entry always regenerates, so a wrong cache is *slow*,
never *incorrect*.

**The pre-push gate only runs on direct pushes to master** (a PR merged in
GitHub's UI runs no local hook) — CI cannot validate a cache built from
gitignored `export/` data. Use `--run` for the PR case.


## Navmesh redesign: pathgrid corridor ribbons
<a id="navmesh-redesign-pathgrid-corridor-ribbons"></a>

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
<a id="baseline-before-rewrite"></a>

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
<a id="why-replace"></a>

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
<a id="author-set-principles"></a>

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
<a id="what-stays-exactly-as"></a>

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
<a id="phase-1-corridors-doors-links"></a>

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
<a id="phase-2-grow-width-walls"></a>

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
<a id="phase-3-polish"></a>

- Teleport-door far-side clipping (port `_interior_sign` from `test-navmesh-2`'s
  `door_carve.py`) so a teleport door does not trail ribbon into the decorative
  geometry beyond the cell shell.
- Junction mitering (Open Question B2) for cleaner intersections.
- Wider door thresholds / better-shaped Door Triangles if the stamped quad reads
  as too small in-game.

---

## Decisions made (author) and open questions
<a id="decisions-made-open-questions"></a>

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
<a id="risk-register"></a>

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
<a id="connectivity-invariant-status"></a>

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
<a id="triangle-quality-contract"></a>

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
