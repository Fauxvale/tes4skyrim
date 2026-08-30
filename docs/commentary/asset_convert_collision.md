# asset_convert/collision.py — Havok collision

**Code:** `asset_convert/collision.py`, `asset_convert/collision_extract.py`, `asset_convert/mopp.py`

## Contents

- [NIF bhkRigidBody field mapping (PyFFI ↔ newer nif.xml)](#nif-bhkrigidbody-field-mapping)
- [NIF dynamic clutter physics (Havok)](#nif-dynamic-clutter-physics)
- [MO_SYS_FIXED (7) statics simulated as clutter — "floating / spinning / on its side" (SOLVED 2026-07-28)](#mosysfixed-statics-simulated-as-clutter)
- [Skyrim APPLIES rotation/translation on non-T bhkRigidBody (THE fundamental havok bug, found 2026-07-15)](#skyrim-applies-rotationtranslation-non-t)
- [Hoisted collision dropped the child node's ROTATION (SOLVED 2026-08-27)](#hoisted-collision-dropped-child-nodes)
- [Inverted collision winding — "I fall through the floor" (SOLVED 2026-07-20; **rewritten 2026-08-20, see round 3 below**)](#inverted-collision-winding-i-fall)
- [bhkPackedNiTriStripsShape sub-shapes MOVED between formats — load CTD (SOLVED 2026-07-28)](#bhkpackednitristripsshape-sub-shapes-moved-between)
- [Constrained objects: chains, swinging traps, gates, trigger phantoms (2026-07-15)](#constrained-objects-chains-swinging-traps)
- [Activation pick region (HUD rollover "too big" on clutter) — SOLVED 2026-07](#activation-pick-region)
- [NIF bhkMultiSphereShape (dead in Skyrim, fixed 2026-07-05)](#nif-bhkmultisphereshape)

## NIF bhkRigidBody field mapping (PyFFI ↔ newer nif.xml)
<a id="nif-bhkrigidbody-field-mapping"></a>
- `unknown_int_1` → bhkWorldObjCInfo.Unused01 (4 bytes binary padding) — **zero for safety**
- `unknown_int_2` → BroadPhaseType(1B) + Unused02(3B) — set to 1 (BROAD_PHASE_ENTITY)
- `unknown_3_ints` → bhkWorldObjCInfoProperty (Data=0, Size=0, CapFlags=0x80000000)
- `unknown_byte` → bhkEntityCInfo.Unused01 — set to 116 (matching external NIFConverter)
- `unknown_2_shorts` → bhkRBCInfo padding — set to [29541, 23659]
- `unknown_6_shorts[2:4]` → bhkRBCInfo2010.UnknownInt1 — **MUST be 0** (Skyrim interprets as pointer)
- Static objects: quality_type=1 (MO_QUAL_FIXED), motion_system=5 (SYS_BOX_STABILIZED)
- Dynamic/clutter: quality_type=4 (MO_QUAL_MOVING), motion_system=3 (MO_SYS_SPHERE_INERTIA)
- Animated: quality_type=1 (MO_QUAL_FIXED), motion_system=4 (MO_SYS_KEYFRAMED)

## NIF dynamic clutter physics (Havok)
<a id="nif-dynamic-clutter-physics"></a>
- **Mass**: Keep Oblivion mass as-is. Oblivion clutter (0.1–8.0) is already in Skyrim's range (0.5–100). The legacy converter's `mass *= 6` is WRONG — makes items too heavy and causes them to "hang in the air."
- **Inertia tensor**: Must scale by `HAVOK_SCALE² = 0.01`. Oblivion inertia (2.3–8.8) is ~100× Skyrim (0.02–0.32) because inertia ∝ mass × distance² and collision shapes are scaled 0.1× for Skyrim Havok units.
  - The full ×0.01 is applied EXACTLY ONCE, in `_convert_collision` (dynamic + keyframed branches) and `_convert_blend_collision`. `scale_constraint_pivots` must NOT rescale again — a leftover ×0.1 there had every constrained body's inertia 10× too small (fixed 2026-07-15).
- **Skyrim clutter standard values**: friction=0.50, restitution=0.40, linear_damping=0.0996, angular_damping=0.0498, max_linear_velocity=104.4, max_angular_velocity=31.57, deactivator_type=1, solver_deactivation=2

## MO_SYS_FIXED (7) statics simulated as clutter — "floating / spinning / on its side" (SOLVED 2026-07-28)
<a id="mosysfixed-statics-simulated-as-clutter"></a>
Third-party plugin statics (streetlights, beds, tables, shrines, chests, torches) tipped onto their sides, drifted, or spun off through the air on cell load.
- **Cause**: `_convert_collision`'s static-vs-dynamic branch dispatched on **mass alone** (`elif rb.mass == 0:` → static, `else:` → dynamic). Oblivion `MO_SYS_FIXED` (7) — nif.xml: *"used for the static elements of a game scene, e.g. the landscape"* — was never consulted, so any fixed body with a non-zero mass field became a fully-simulated Skyrim prop with a mesh collision shape.
- **Why base Oblivion never showed it**: a 300-NIF census of `Oblivion.esm` found **198 ms=7 bodies, 0 with mass>0** — Bethesda always zeroes mass on fixed bodies, so mass alone happened to classify every one correctly. The inference was wrong but indistinguishable from correct on vanilla data.
- **Why Morroblivion did**: the same census over `Morrowind_ob.esm` found **186 ms=7 bodies, 157 with mass>0** (its idiom is `mass=1000` + `layer=1 OL_STATIC` for "static"). The majority of its statics were being converted into 1000 kg dynamic clutter.
- **Fix**: before the mass dispatch, `rb.motion_system == 7 and rb.num_constraints == 0` → `rb.mass = 0.0`, falling into the existing static branch (ms=5 BOX_STABILIZED, quality 0, mass 0). Constraint-owning fixed bodies are left alone — they are real trap/chain parts handled by the constraint branches.
- Measured: 125/227 sampled Morroblivion meshes corrected, 0 left dynamic; base-Oblivion clutter (ms=1/2/4, real masses) verified unchanged and still dynamic.
- **General lesson**: the source's declared motion type is the authoritative statement of static intent — never re-derive it from mass. A heuristic that is *accidentally* total on vanilla data will silently misclassify third-party content.

## Skyrim APPLIES rotation/translation on non-T bhkRigidBody (THE fundamental havok bug, found 2026-07-15)
<a id="skyrim-applies-rotationtranslation-non-t"></a>
The single most important havok-conversion fact, and the root cause of both "constrained objects act completely rigid" AND the longstanding "havok interactions feel weird on normal misc items":
- **Oblivion ignores the translation/rotation fields on plain (non-T) `bhkRigidBody`**, so Oblivion NIFs ship arbitrary leftover values there (chain links carried rotations up to ~115°).
- **Skyrim applies BOTH fields on BOTH body classes.** Proof: vanilla `trapmace01.nif` Base01 — the node is rotated +0.5° about X and the plain bhkRigidBody carries exactly the inverse quaternion (-0.0044,0,0,1) so its root-space MOPP stays aligned; every other vanilla non-T body is exactly identity/zero, unlike Bethesda's genuinely-garbage padding fields.
- Consequence of passing them through: every constraint frame and collision shape is rotated out from under the solver → constraint assemblies act welded solid; ordinary clutter collision sits askew from the visual mesh.
- Fix in `_convert_collision`: non-T bodies get translation=(0,0,0,0) AND rotation=(0,0,0,1). bhkRigidBodyT keeps its (scaled) transform. NOTE: field-level dumps looked "fine" for months because everyone (and the docs) believed the non-T fields were dead — when a converted mesh matches vanilla on every OTHER field, byte-diff the remaining "ignored" ones.

## Hoisted collision dropped the child node's ROTATION (SOLVED 2026-08-27)
<a id="hoisted-collision-dropped-child-nodes"></a>
"citadelballconystandardendleft02.nif has no collision" — but the output carried a
complete `bhkCollisionObject` + MOPP + CMS with 373 non-degenerate triangles. The
collision was **present and correctly formed, just parked in the wrong half of the
world**, so nothing the player walks on ever touched it.

Measured (game units): render Y `-956.8..-494.2`, collision Y `+508.7..+1035.6`,
Z off by ~286. X matched exactly — the tell that this is a transform bug, not a
geometry one.

Cause: Skyrim requires collision on the root, so `hoist_collision` moves it up from
a child NiNode — but it read **only `child.translation`**, and only for the two
strips shape types via `_offset_collision_shape_verts`. This mesh hangs its
collision on `collisionCitadelBallconyStandardEndRight04`, whose rotation is
`diag(1,-1,-1)` (180° about X). That flip was silently discarded, negating Y and Z.

**The A/B that isolates it**: sibling `citadelballconystandardendleft.nif` — same
author, same geometry, same 373-tri hull, same code path — converts correctly, and
its collision node's rotation is identity. The node rotation is the only
discriminator; the shape type, mesh and plugin are all constants across the pair.

Fix: `hoist_collision` now composes the child's FULL `(R, T, s)`:
- **`bhkRigidBody(T)`** → `bake_node_transform_into_body` (promotes to
  `bhkRigidBodyT`). Shape-agnostic, so it also fixes convex hulls, list shapes and
  primitives — none of which have a vertex array the old path could rewrite, so
  they had been dropping the translation too.
- **`bhkSimpleShapePhantom`** (trap-damage volumes, trigger zones) has no body
  transform field and cannot be promoted → `_wrap_shape_in_node_transform` composes
  `L` into a `bhkTransformShape` wrapper instead (composing into an existing
  transform shape rather than double-wrapping).

Safe against the bodyT+CMS CTD above: mesh collision folds any bodyT back into the
triangles in `_bake_body_transform_into_tris` and demotes the body to plain
identity, so no shipped CMS mesh gains a `bhkRigidBodyT`.

**Blast radius, measured over 28,470 exported NIFs**: 645 reach the hoist path; 55
have a non-identity rotation/scale there; 19 are creature assets (`creature=True`
skips the hoist) and 15 more take the `wrapped` path (which already baked rotation
correctly) — leaving **14 meshes that actually changed**, across Oblivion.esm and
Nehrim.esm. Verified: collision/render overlap ≥0.97 on the balcony pair, 0 issues
from `collision_sanity.py`, and the phantom's composed transform matches a hand
computation to 4 decimals.

The `wrapped` gate in `nif_converter.py` still skips hoisting — not because
rotation is unsupported any more, but because that case would have to compose the
WRAPPER's transform too.

## Inverted collision winding — "I fall through the floor" (SOLVED 2026-07-20; **rewritten 2026-08-20, see round 3 below**)
<a id="inverted-collision-winding-i-fall"></a>
Falling through floors in Nehrim (worst in caves) that are solid in Oblivion. **Source-data corruption, not a conversion bug** — the converter faithfully reproduced broken input.

> **⚠ Read [round 3](#rewritten-2026-08-20-round-3--the-winding-is-authored-stop-inferring-it) before changing anything here.** Two claims in the sections below are now superseded: the repair is **no longer Nehrim-specific** (vanilla Oblivion has the same damage — `seisland.nif` ships 1480 inverted triangles, `meshes/rocks` measures 14.5%), and **"vanilla Oblivion is the control test for any detector" is FALSE**. The winding is recorded per-triangle in the NIF itself, so the primary repair no longer infers anything; the inferred steps described below still exist but are now gated to Morroblivion alone.
- **Symptom shape matters**: you fall through *half* a floor, not all of it. Collision is present, MOPP is clean, layer/material/orientation all correct.
- **Cause**: Nehrim re-exported collision as `bhkPackedNiTriStripsShape` (Oblivion ships `bhkNiTriStripsShape`). Flattening strips → triangle lists dropped the parity flip on odd-indexed triangles, so one half of every floor quad has reversed winding. **Havok mesh collision is single-sided** → a down-facing triangle is walked straight through from above.
  - `priorychapelinterior.nif` floor: Oblivion `(37,35,34)`+`(35,36,34)` both UP; Nehrim `(37,35,34)` UP + `(35,34,36)` **DOWN** (last two indices swapped). Floor area 719k → 359k, exactly half.
  - `rfrmfloor.nif` (fort/cave tileset, used in hundreds of cells): Oblivion `(0,1,2)`+`(1,3,2)` UP; Nehrim `(0,1,2)` UP + `(1,2,3)` DOWN.
- **Scale**: 1065/4485 Nehrim meshes vs 10/4199 vanilla Oblivion (~100×). Vanilla-Oblivion cleanliness is the control test for any detector here — if a scanner flags lots of Oblivion meshes, the scanner is wrong.
- **Fix**: `_repair_inverted_floors()` in `asset_convert/collision.py`, called from `_rebuild_mesh_collision` after the body-transform bake (so triangles are in final orientation). Counter via `inverted_floor_flip_count()`.
- **Verify**: A/B a mesh with `_repair_inverted_floors` monkeypatched to a no-op and compare output hashes; count up/down near-horizontal faces in the decoded CMS (`asset_convert.cms.decode_cms`) before/after.

### Rewritten 2026-07-28 (round 2) — it is a STRIP-PARITY bug, so solve it structurally
The first rewrite (coplanar contradiction + co-located visual face) scored 35.8% recall against ground truth and left `priorychapelinterior`, `skbridgesmall` and `rockgreatforest645lichen` unwalkable. Both it and the original z-band rule were **geometric guesses at a structural defect**.

- **What the data actually says.** Score a Nehrim mesh triangle-by-triangle against its Oblivion original (same relative path, identical vertices — the vanilla winding is ground truth) and print the correct/reversed flag in *packed triangle order*:

  ```
  priorychapelinterior  ++++++++++++++++++++-+-+-+-+-+-+-+-+-+-...-+-+-+-..+-+-+-
  skbridgesmall         ++++++-+-+-+-+-+-+-+-+-++-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
  ```

  It **strictly alternates**. 97.2% of reversed triangles are explained purely by position within a flattened strip, and of 301 decided strip runs, **301 have the same phase** (first triangle correct, alternating after). This is exactly the dropped `bhkPackedNiTriStripsShape` parity flip the section above describes — no geometry required to find it.
- **New design — two steps, the first exact.**
  1. **Relative orientation (`_orient_components`).** Two triangles sharing an edge are consistently wound **iff they traverse that shared edge in opposite directions** — the standard manifold-orientation test. Weld coincident vertices, BFS the shared-edge graph, flip whatever disagrees. No thresholds, no normals, no flatness. It undoes the dropped parity exactly and is **completely inert on correctly wound input**.
  2. **Absolute sign (only where step 1 cannot help).** Step 1 makes a component self-consistent but cannot tell outward from inside-out, because flipping *every* triangle of a component is also self-consistent. So per component: a **closed** component must enclose positive volume; otherwise the **render mesh** decides (artist winding is correct by construction). Undecided ⇒ leave alone.
- **Weld per geometry group** (`shape_tri_groups`). A shape can hold several independent pieces — one `NiTriStripsData` block each, or one packed sub-shape each — that merely touch in space. Welding across that seam fuses them into one component and forces a single orientation on both.
- **The visual vote needs a quorum, not more geometry.** Every false positive measured on already-correct collision had `cov == 1`: a single stray facet (the far skin of a slab, or a decorative mesh passing nearby) condemning a whole component. Requiring **half the component to have seen evidence**, with trust radius `0.30` hu, removes them and still fixes uniformly-reversed floors. Attempts to separate the cases by *geometry* instead (signed-volume floor, area-normalised "solidity") both failed — volume is meaningless on the open sheets that dominate here.
- **Result** (shipped code, scored against the Oblivion originals):

  | Tree | recall | broken |
  |---|---|---|
  | dungeons (114 meshes, 33k tris) | **99.9%** (13,123/13,135) | **0** |
  | architecture (155 meshes, 53k tris) | **99.7%** (17,310/17,354) | 112 (0.32%) |
  | rocks (147 meshes, 51k tris) | 99.1% (24,467/24,693) | 268 (1.00%) |
  | the 3 reported failures | **99.8%** (494/495) | **0** |

  Previous code scored **35.8%** on the same corpus. Floor-regression sweep (`--floor-regress`): **0** walkable floors turned into fall-throughs across 300 vanilla Oblivion and 300 Nehrim architecture meshes. `inuhlaaluuroomuside` still converts walkable (the sign step flips its 4-triangle floor component).
- **"Vanilla is clean" is FALSE for the SI bridges.** `dementiabridge01` has **242 of 324 shared edges inconsistently wound** in vanilla Oblivion. A safety harness that counts "triangles the repair changed" on vanilla will report ~1.4% false positives that are actually genuine repairs. Measure the invariant that matters instead — *does a walkable floor become a fall-through one* — which is what `--floor-regress` does.
- **Do not weld across strip-data blocks when auditing.** An early safety test flattened `bhkNiTriStripsShape` into one soup and reported 272 vanilla "false positives"; evaluating each `NiTriStripsData` separately (as the shipped code does) gave **0**. The artifact was the harness, not the algorithm.
- **Known limit — 3 Morrowind_ob meshes lose a floor** (`morro/f/actubmurootu01`, `morro/i/actusothautenderizer`, `morro/i/actusothauoilbridge`; the same sweep *fixes* 2 others). Cause is the **relative pass**, not the sign step: `--floor-regress` still reports them with the visual oracle disabled entirely, and the visual radius makes no difference (swept 0.10→0.30, identical outcome). The BFS anchors each component on whichever triangle it reaches first, so when the floor is the minority of a large component the whole component comes out inverted. For `actubmurootu01` this is arguably correct anyway — its render skin at distance 0.000 faces **down** (`agree = -1.00`), i.e. the source mesh is self-inconsistent.
- **Two tie-breaks were tried against this and BOTH were reverted** — record them so they are not re-attempted:
  1. *Minority-flip in the relative pass* (choose whichever sign flips fewer triangles, since orientation is only defined up to a global sign). Did **not** fix the Morrowind meshes and cost Nehrim recall: 99.7%→98.6%, broken 112→273.
  2. *Floor-preserving tie-break in the sign step* (when volume and visual both abstain, pick the sign that keeps the lowest surface up-facing). Also did not fix them — the visual vote **does** reach quorum on these components (`cov=80/80`) so it never abstains and the tie-break never runs — and it hurt Nehrim badly: 99.7%→97.5%, broken 112→818.

  The lesson both times: these components are decided by *confident* evidence, so a fallback cannot reach them, and any rule strong enough to override the evidence damages the 99.7% case. Fixing this properly needs a better component seed, not another tie-break.
- **Morrowind_ob scale check** (400 `morro/` meshes, `--floor-regress`): 1 fixed, 3 regressed — all three in the `morro/f/` flora family (`actubmurootu01`, `floraurootuwgu01`, `floraurootuwgu05`), i.e. root/plant props rather than walkable architecture. For context `--floor-orientation` shows **58 of those meshes already fall-through in the source before any repair**, so this tree is broadly broken upstream and is not the case the repair is tuned for. Nehrim and vanilla Oblivion — the trees with real ground truth — regress **0** floors across 600 architecture meshes.
- **Tooling**: `tools/nif/collision_winding.py --ab <ref_tree>` (exact recall/breakage vs ground truth) and `--floor-regress` (the in-game invariant). Run both before shipping a change here.

### Rewritten 2026-08-20 (round 3) — the winding is AUTHORED; stop inferring it

Round 2 solved Nehrim but was gated per-plugin and off for vanilla, on the
premise that "vanilla Oblivion is authored correctly". **That premise is
false**, and the gate hid a signal that was in the file the whole time.

- **Vanilla Oblivion falls through its own floors.** `rocks/seisland/seisland.nif`
  (the Shivering Isles island, `STAT 00078C2C`, placed at scale 1.0 in two
  worldspaces) ships **1480 of 3590** collision triangles wound against their
  own recorded normal. Downward-raycast over its top surface: **538 walkable /
  432 fall-through**. Across `meshes/rocks`, **14.5%** of decidable floor faces
  are inverted in vanilla. The converter reproduced this faithfully — it was
  never a conversion bug, and the per-plugin gate meant it was never repaired.

- **THE AUTHORED INDICATOR: every collision triangle records the direction it is
  meant to face, independently of the winding that produces that facing.**

  | format | field |
  |---|---|
  | `hkPackedNiTriStripsData` | `triangles[i].normal` — one per triangle |
  | `NiTriStripsData` | `normals[]` — per **vertex**, averaged per face |

  A strip flatten reverses the winding and carries the stored normal through
  **unchanged**, so `dot(face_normal(tri), stored_normal) < 0` is the file
  stating which of its own triangles are damaged. No adjacency walk, no oracle
  mesh, no thresholds, and inert wherever the two already agree. On seIsland it
  identifies 1480 triangles — matching the 1487 the round-2 heuristic flips —
  and rewinding to it alone gives **970 walkable / 0 fall-through**.

  Cross-checked against the render-skin oracle (`collision_winding_truth.py`,
  which is independent of both winding and normals):

  | tree | authored normal agrees with render truth |
  |---|---|
  | Oblivion architecture | **98.6%** |
  | Nehrim architecture | **97.9%** |
  | Morroblivion `morro/i` | **60.0%** |

- **This is now STEP 0 and it is UNGATED.** `_shape_tri_normals()` extracts the
  normals index-aligned with `_shape_tri_soup` (**same degenerate-triangle
  filtering — edit the two together or normals bind to the wrong faces**), and
  `_repair_inverted_floors` applies them before consulting the toggle at all.

- **Steps 1-3 are INFERENCE and stay gated, because inference costs false
  positives.** Step 1 seeds each welded component from an arbitrary triangle and
  propagates that choice, so a component seeded inward inverts wholesale:
  `architecture/castle/leyawiin/leyawiincastle02.nif` is ONE `NiTriStripsData`
  block of 1849 triangles that welds into **40 disconnected components**; step 1
  flipped **274 of 284** triangles in one of them (96% — the tell that the seed
  was in the 4%) and cost **806 walkable raycast cells on a vanilla mesh**.
  Step 2 nearly caught it (visual vote 11:1 against, well past `_VIS_MARGIN`)
  but abstained: the component is not closed, and coverage was **137/284**,
  five short of the quorum. Measured over Oblivion architecture, step 1 alone
  takes 43 inverted faces to **103**.

- **Nehrim no longer needs the gate.** Its exporter left the normals intact, so
  step 0 alone does the job and the inference risk is not worth taking:

  | tree | raw | step 0 only (SHIPPED) |
  |---|---|---|
  | Nehrim dungeons | 46.07% inverted | **0.09%** |
  | Nehrim architecture | 24.17% | **0.70%** |

- **Morroblivion still needs it, and is the ONLY default member.** Its exporter
  rewrote each normal to match the winding it emitted, so both agree while both
  are wrong and step 0 has nothing to detect. `morro/i/inuhlaaluuroomuside.nif`:
  all 10 triangles score `dot = +1.0` over a floor (`z = -123`, under a ceiling
  at `z = +97`) you fall straight through. Across `morro/i` the authored normals
  change **0 of 5496** inverted faces; steps 1-3 take it to **20**.

- **⚠ MEASURING AN ENCLOSED MESH: a downward raycast sees the CEILING.** This
  cost two wrong conclusions in one session — that `inuhlaaluuroomuside` was
  unrepaired (it is repaired; its floor is simply under a roof) and that the
  round-2 docstring was stale (it is accurate). For a room, score the **lowest**
  surface, or use the render-skin oracle. Never read a top-down "fall-through"
  count on interior geometry.

- **Result, scored on the SHIPPED output against the source render skin:**

  | tree | raw | shipped |
  |---|---|---|
  | Oblivion rocks | 17.8% | **0.10%** |
  | Oblivion architecture | 0.81% | **0.43%** |
  | Oblivion dungeons | 0.10% | **0.10%** (inert) |
  | Nehrim dungeons | 46.07% | **0.09%** |
  | Nehrim architecture | 24.17% | **0.70%** |

  `leyawiincastle02` ships **847 walkable / 0 fall-through**, identical to its
  source — zero false positives.

- **Caveat on the numbers above.** The render-skin oracle shares its signal with
  step 2's `_component_visual_vote` (same coincidence rule, same constants), so
  scores for variants *containing step 2* are partly self-graded and read high.
  The step-0 figures do not depend on it — they come from reading normals
  directly out of the files. The oracle also has a documented thin-slab trap
  (chairs, benches, stairs), which is why Oblivion clutter/furniture measure
  ~9-12% "inverted" raw while `lowerclasschair01` and `lowerclassbench01` in
  fact have **0** triangles disagreeing with their normals.

## `bhkPackedNiTriStripsShape` sub-shapes MOVED between formats — load CTD (SOLVED 2026-07-28)
<a id="bhkpackednitristripsshape-sub-shapes-moved-between"></a>
Three crashes in converted Morrowind_ob traced to one mesh, named directly in the crash log's stack strings (`inputFilePath: "data\MESHES\tes4\morro\i\inucaveuplant00.nif"`). Exception was `vmovntdq [rcx+0x40], ymm3` in VCRUNTIME140 (a `memcpy`) writing off the end of a heap page, with `bhkPackedNiTriStripsShape` + `bhkRigidBody` + `BSResource::LooseFileStream` on the stack — i.e. **a crash while reading the NIF, before anything renders**.
- **Root cause — the sub-shape list changed owner between the two NIF versions** (`references/nif 0.10.0.0.xml`):
  - `bhkPackedNiTriStripsShape.Num Sub Shapes` — `until="20.0.0.5"` (Oblivion)
  - `hkPackedNiTriStripsData.Num Sub Shapes` — `since="20.2.0.7"` (Skyrim)

  Our builders wrote the count onto the **shape** (the Oblivion field, not even serialised at Skyrim's 20.2.0.7), so the **data** block shipped `num_sub_shapes = 0` while carrying real geometry. Skyrim sizes its sub-part allocation from that count, then memcpys the vertex/triangle payload into the undersized buffer → access violation on load.
- **Fix**: `_set_packed_sub_shape()` in `asset_convert/collision.py` writes the covering sub-shape to **both** fields (correct at either version). Called from `_packed_from_tris`, `_ni_strips_to_packed`, and the `bhkListShape`-child path.
- **Three independent defects, same symptom** — all had to be fixed:
  1. `_packed_from_tris` / `_ni_strips_to_packed` set only the Oblivion-side count.
  2. `_convert_shape` returned a `bhkPackedNiTriStripsShape` **completely unconverted** (`return shape`) when it appeared as a `bhkListShape` child — no rescale, no sub-shape migration. This is how `crescentblade.nif` and 4 Oblivion.esm meshes were hit; the standalone case never reached it because `_rebuild_mesh_collision` handles that first.
  3. Degenerate hulls (below).
- **Degenerate collision hulls crash the MOPP bridge**: the bridge access-violates (`rc 3221225477`) inside "computing two-sided welding" on very small hulls — Havok divides by near-zero edge lengths. Verified by scale sweep on `inucaveuplant00`: ×1 crashes, ×10/×100/×1000 all succeed. The duplicated reversed triangle in that mesh is *not* the trigger (A/B tested — crashes with and without it). **This is not Morroblivion-specific**: vanilla Oblivion `paintbrush01/02/03` (0.034 hu) hit it too.
  - **Fix is a scaled rebuild, not a drop** (`cms_builder.build_cms_collision`): MOPP encodes geometry as `(v - origin) * scale`, so building the bytecode over vertices scaled by `k` and storing `origin/k` with `scale*k` is an **exact** restatement for the original geometry — no approximation, and the CMS chunk data stays at native scale. Retries k=10/100/1000. Verified on paintbrush01: MOPP origin reproduces the source AABB minimum on all 3 axes, decoded CMS AABB matches the source, `walk_mopp` returns 0 errors and its terminal key set equals the CMS key set.
  - **Dropping is now only for sub-viable hulls**: `_MIN_HULL_EXTENT = 0.01` hu (≈0.07 game units, sub-millimetre) — far below the smallest vanilla Skyrim hull (**0.179 hu**, censused from vanilla clutter CMS). Morroblivion's `inucaveuplant00` is 0.0098 hu against a 73.5-game-unit visual mesh (~1000× too small), so its collision is meaningless and the collision object is removed (`collision_object = None`). Counter: `degenerate_hull_drop_count()`. Do **not** raise this to catch things like the paintbrush — that clutter must stay grabbable, and the scaled retry already gives it real MOPP.
- **Vanilla control**: **0 of 17,216** vanilla Skyrim meshes contain `bhkPackedNiTriStripsShape` at all — Skyrim always ships MOPP+CMS. That was read here as "our fallback must at least be structurally valid"; **2026-08-22 corrected it to "never emit it"** — a structurally-valid shape of a type the engine does not support still corrupts the heap on load.
- **Scale of the bug**: 3 meshes in Morrowind_ob, 4 in Oblivion.esm; 0 in Nehrim/SI. After the fix the paintbrushes and `crescentblade` get real MOPP+CMS (collision preserved), the two `inucaveuplant` hulls are dropped, and the only remaining packed shape is `gnarlspawner.nif`'s **`bhkSPCollisionObject` trigger phantom** — a deliberately-preserved type (see the constrained-objects section) that goes through `_convert_shape`, not the rebuilder. It now ships `data.num_sub_shapes = 1`, so it is structurally safe even though it keeps the fallback shape. **SUPERSEDED 2026-08-22:** keeping the fallback shape at all was wrong -- Skyrim never loads that type, and it caused the 2 GB-memcpy heap corruption documented below. `gnarlspawner`'s phantom now carries MOPP+CMS (the `bhkSPCollisionObject` / `bhkSimpleShapePhantom` types are still preserved; only the shape inside changed, geometry exact at 120->120 and 124->124 triangles), and the packed shape is no longer emitted anywhere.
- **Verify**: `python tools/nif/nif_block_scan.py output/<plugin>/meshes --has bhkPackedNiTriStripsShape`; any hit must have `data.num_sub_shapes >= 1` covering all vertices. Note the scanner reads the header block-type *table*, so it flags a file whose table still lists the type — confirm with a block walk before concluding a real shape is present.

## Constrained objects: chains, swinging traps, gates, trigger phantoms (2026-07-15)
<a id="constrained-objects-chains-swinging-traps"></a>
Besides the non-T rotation root cause above, the "chains/traps look right but never move when touched/grabbed" cluster had four more independent causes, all in `asset_convert/collision.py`:
1. **Constraint max_friction**: Oblivion ships 3.0 (limited hinge) / 10.0 (ragdoll); in Skyrim that much joint friction locks the joint solid. Vanilla Skyrim prop constraints use **0.01** (desecratedimperial.nif ragdolls, spitpot hinges, tavern signs). Clamp >0.5 → 0.01 in BOTH `_fix_limited_hinge` AND `_fix_ragdoll` (the sign fix originally only covered limited hinge — chains use ragdoll constraints).
2. **Collision-filter layer remap** (`_remap_world_filter`): Oblivion layers 0-18 equal Skyrim's, but 19+ diverge and Oblivion authored world props on ragdoll bone layers (cellchain01 anchor = 42 OL_L_FOOT → Skyrim PATHPICK = raycast-only, NO collision). Body-part layers 33-57 → 10 SKYL_PROPS (vanilla trapmace links' layer); pick layers shift +15; stairs 19→31, char controller 20→30, avoid box 21→34.
3. **Filter flags/part byte + group must be zeroed** on world objects: Oblivion chains ship 0x80|partnum ("Linked Group" bit 7 + biped part); vanilla Skyrim constrained props ship 0 and rely on runtime per-reference group assignment. Biped part numbers only mean anything on layer 8/32/33.
4. **Oblivion MO_SYS_KEYFRAMED (6) is a THREE-WAY split** (this took three in-game test rounds to get right — both simpler rules made things worse):
   - node driven by animation (`_node_is_animated`: sequence controlled-blocks, NiMultiTargetTransformController extra targets, or a transform controller on the node) → Skyrim KEYFRAMED (ms 4, quality 1, collObj 137, node flag |0x80, **mass forced to 0**). Gate leaves (mass=100, no constraints, Open/Close sequences) — treating gates as dynamic made them "fall off their hinges" (they have NO hinge constraints; they swing by animation). **The mass-0 forcing is mandatory**: a keyframed body with non-zero mass is an active simulated island, so physics overwrites the animated transform every step and the object never visibly moves even though the sequence plays normally (vanilla: 11/11 keyframed bodies are mass 0). See "A keyframed body MUST have mass 0" above.
   - mass>0 AND owns a constraint → DYNAMIC. Oblivion marks entire swinging traps keyframed ("Unyielding=1" links) because ITS engine holds traps rigid until the trap script enables havok; Skyrim's trapmace01 ships the same links DYNAMIC (ms 3, quality 4). Keyframing them = trap welded solid.
   - everything else (constrained-island anchors cellchain01/cellChainMiddle mass=100, unyielding props) → STATIC with mass forced to 0. Vanilla chain/noose/trap anchors are ALWAYS static mass-0 bodies (NooseRopePiece01 root, trapmace Base01), NEVER keyframed — a keyframed body with anim flags on a non-animated object flips the engine into the baked path and the whole compound (all its dynamic children included) acts welded solid.
- **Trigger phantoms are SUPPORTED by Skyrim** — do NOT strip `bhkSPCollisionObject`/`bhkSimpleShapePhantom`: vanilla ships 31 under meshes/traps alone (tripwire, pressure plates, bear trap), always collObj flags=129 + layer 12 TRIGGER. Convert the inner shape (×0.1 + material) and keep. Stripping them killed every Oblivion trigger volume (tripwire never fired).
- Vanilla reference meshes: `traps/macetrap/trapmace01.nif` (swinging mace analogue), `traps/tripwire/traptripwire01.nif`, `clutter/woodfires/spitpot*.nif` (hinge), `clutter/deadsoldiers/desecratedimperial.nif` (prop ragdoll constraints).
- Debug tool: `python tools/nif/havok_constraint_dump.py <nif|dir>` prints per-body filter (layer/flags/group), inertia, motion/quality, damping, and full constraint descriptors (pivots/axes/limits/friction) — the scene-tree analyzers hide all of this.

## Activation pick region (HUD rollover "too big" on clutter) — SOLVED 2026-07
<a id="activation-pick-region"></a>
- Skyrim's crosshair activation is a PRECISE raycast against the Havok collision shape (user-verified: vanilla prompts appear only when the cursor is exactly on the mesh; `fActivatePickRadius` INI had no effect). An earlier theory blaming engine INI slop was WRONG.
- Root cause: Oblivion clutter ships ONE bhkConvexVerticesShape hull per object. A convex hull FILLS EVERY CONCAVITY — a goblet's hull fills the waist around the thin stem (collision radius 2.7-3.0 vs visual 1.6), a pitcher's hull fills the entire handle gap (y ±4.7 where the visual handle is ±0.53). AABB comparisons hide this (hull AABB == visual AABB exactly); compare CROSS-SECTIONS at concave features instead. Vanilla authors compound shapes instead (glazedgoblet01 = bhkListShape of cup box + stem box).
- Fix (`_decompose_clutter_hull` in collision.py): dynamic (mass>0) plain-bhkRigidBody single-convex-hull clutter is rebuilt as a bhkListShape of per-piece hulls: recursive binary split of the VISUAL vertices along the axis-aligned cut minimising total hull volume (scipy ConvexHull; accept cut if ≥10% volume gain, depth ≤3 → ≤8 pieces). Each half extends past the first vertex ring on the far side of the cut, or sparse vertex rows leave unfilled collision bands between pieces. Piece planes = scipy hull equations deduped, w = d − radius (vanilla stores planes pushed out by the convex radius). bhkRigidBodyT excluded (shape frame ≠ node frame). Frame sanity check vs the original hull AABB bails out when collision was authored differently from visuals. Result: goblet stem 2.7-3.0 → 1.8-2.4 (tighter than vanilla's box corners), pitcher handle strip y ±0.6.
- **Havok material conversion (was missing entirely)**: Oblivion materials are a 0-31 enum; Skyrim materials are CRC32 hashes (SkyrimHavokMaterial, values in references/nif 0.10.0.0.xml). `_convert_materials()` in collision.py maps them (`_OB_TO_SK_MATERIAL`); unmapped values leave the engine with an unknown material (no impact sounds/decals/stair-walk flag). **PyFFI trap: EnumBase.set_value() only LOGS "invalid enum value" and returns** for values outside its old enum list — must write `item._value` directly. PyFFI instantiates ONE material item per read context (typed OblivionHavokMaterial even when reading Skyrim CRC files — repr shows `<INVALID (...)>`, harmless; read/write via `_get_havok_material`/`_set_havok_material`).
- **Inertia scale regression**: collision.py had drifted to `_INERTIA_SCALE = 0.1` with a bogus justification comment ("Havok normalises by body scale internally"). Correct value is `_HAVOK_SCALE**2 = 0.01` (inertia ∝ mass·length², lengths scale 0.1) — verified: vanilla silverjug01 stores I_x=0.031 = m(3r²+h²)/12 exactly in SI/Havok metres. The 0.1 scale left inertia ~10× too large → sluggish rotation / "too much inertia" feel when grabbing or knocking clutter. (tests/test_asset_convert.py `_INERTIA_SCALE = 0.1` still asserts the old value and needs updating.)
- Note on masses: Oblivion authored masses differ per-item from Skyrim equivalents with no consistent ratio (OB silver pitcher 8.0 vs vanilla silver jug 0.8, but OB ceramic goblet 0.4 ≈ vanilla goblets 0.5-0.8) — masses stay unconverted.
- tes4/tes5_nif_analyzer print `BoundSphere` (NiTriShapeData center/radius) and bhkConvexVerticesShape vertex `extents` for this kind of investigation.

## NIF bhkMultiSphereShape (dead in Skyrim, fixed 2026-07-05)
<a id="nif-bhkmultisphereshape"></a>
- **0 of 17,216 vanilla Skyrim meshes ship bhkMultiSphereShape** (deprecated Havok path). The only Oblivion source that has one is `clutter\magesguild\apparatusalembicnovice.nif`, and shipping it converted CRASHES SSE at cell load (Anvil Mages Guild) with no crash log. Vanilla expresses the same thing as ConvexTransform+Sphere children in a list shape (`clutter\kitchen\woodenladle01.nif`).
- `_expand_multisphere()` in collision.py expands it: each sphere → a `bhkSphereShape` (radius ×0.1) wrapped in a `bhkConvexTransformShape` (identity rotation, sphere center ×0.1 in the 4th column, 4th matrix row all zeros incl. m_44 — matches vanilla). 1 sphere → bare wrapper, N → bhkListShape. `_convert_shape`'s bhkListShape branch now FLATTENS a nested list produced by the expansion (a list shape has no transform of its own so flattening is safe; vanilla never nests list shapes).
