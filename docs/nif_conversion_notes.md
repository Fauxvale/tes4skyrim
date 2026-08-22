# NIF / Asset Conversion Notes

Linked from [CLAUDE.md](../CLAUDE.md). Deep narrative notes from debugging the
Oblivion→Skyrim mesh/collision/particle/animation pipeline. For creature-specific
(behavior graphs, HKX, ragdoll) notes see [creature_conversion.md](creature_conversion.md).
For record-level mapping tables see [record_mapping_reference.md](record_mapping_reference.md).

## Asset Conversion Notes

- **NIF meshes**: Oblivion uses NIF version 20.0.0.4/20.0.0.5 (NetImmerse). Skyrim uses 20.2.0.7 (Gamebryo/BSTriShape). The external NIFConverter subfolder has reference tools.
- **NIF full conversion** (`mesh_convert` package): Performs complete Oblivion→Skyrim NIF conversion:
  1. NiTriStrips → NiTriShape (SE can't render strips)
  2. NiTexturingProperty + NiMaterialProperty → BSLightingShaderProperty + BSShaderTextureSet (Skyrim shader system)
  3. Texture path rewriting (prepend `tes4\` to keep separate from Skyrim assets)
  4. Bone name remapping (Oblivion Bip01 → Skyrim NPC skeleton)
  5. NiNode root → BSFadeNode root (Skyrim's standard root type)
  6. Geometry data finalization (`unknown_int_2 = 8`)
  7. NIF version upgrade (20.0.0.4 → 20.2.0.7, BSStream 83)
  8. bhk block format conversion (Oblivion UV2=11 → Skyrim UV2=83):
     - bhkRigidBody/T: +14 bytes (UnknownInt2 field swap at [44:52], TimeFactor, GravityFactor, RollingFrictionMult, UnknownBytes2, BodyFlags u32→u16)
     - bhkMoppBvTreeShape: +1 byte (BuildType insertion at offset 40)
  9. Orphan block removal (NiMaterialProperty, NiTexturingProperty, etc.)
  10. Oblivion-only block types force-removed (NiVertexColorProperty, NiSpecularProperty, etc.)
  Run: `python -m asset_convert.nif_converter <src_dir> <dst_dir>` (worker pool is automatic: cpu_count-3; there is NO --workers flag).
- **NIF conversion stats**: 8032 source NIFs from Oblivion BSAs. 7380 v20 files converted (91.9%). 650 v10/v4 files copied as-is. 2 remaining parse errors (magic effect particle NIFs).
- **NIF bhk conversion details** (Session 19+):
  - bhkRigidBody/T: Oblivion=236+n*4, Skyrim=250+n*4. Key: two `Unknown Int 2` fields with different vercond (UV2>34 vs UV2≤34). Bytes [44:52] need rearrangement, not just passthrough. Translation/Mass/Friction are at fixed offsets (52, 180, 192 in Oblivion, 52, 180, 200 in Skyrim)
  - Crash signature: SkyrimSE.exe+0A882E6 reading from 0xFFFF* addresses = corrupted bhkRigidBody pointers from misaligned fields
  - bhkNiTriStripsShape: Collision NiTriStripsData must NOT be renamed to NiTriShapeData (template type mismatch). Writer must write strips format, not triangulated.
  - Constraint descriptors (RagdollDescriptor, LimitedHingeDescriptor, HingeDescriptor, MalleableDescriptor): UV2≤16 vs UV2>16 field REORDERING is handled by PyFFI's ver1/ver2-guarded duplicate attrs (same attr names in both layouts, so values carry over automatically on read-Oblivion/write-Skyrim).
  - **Constraint conversion (rewritten 2026-07-04, `collision.py::scale_constraint_pivots`)**: the old code only fixed bhkLimitedHingeConstraint; every other descriptor shipped UNSCALED pivots (10× too far, e.g. UpperScales01 ragdoll pivot 3.57 vs vanilla-range 0.36) and zeroed Skyrim-only basis fields. Now for ALL descriptor types: pivot_a/pivot_b ×0.1 (stiff-spring `length` and prismatic min/max_distance too — they're lengths); RagdollDescriptor `motor_a/motor_b` = twist × plane (they are the 3rd column of the constraint's orthonormal basis, NOT motor params — zero = singular basis; handedness verified on vanilla desecratedimperial.nif); HingeDescriptor Skyrim-only `axle_a` = perp_a1 × perp_a2 and `perp_2_axle_in_b_1/2` = Gram-Schmidt complement of axle_b (plain hinge has no limits so any orthonormal complement is valid); inertia ×0.1 rescale deduped per body (the scales crossbar sits in 3 constraints — was being triple-scaled). Vanilla Skyrim constraint census (17,216 meshes): LimitedHinge 158, Ragdoll 59, Hinge 3, StiffSpring 2, **Malleable 0, Prismatic 0** → bhkMalleableConstraint is demoted to a plain constraint of its inner SubConstraint type (`_demote_malleable_constraints`; strength/tau/damping dropped); bhkPrismaticConstraint (Oblivion arrows only) is kept best-effort with a note that vanilla never ships it. Oblivion source census: LimitedHinge 278, Ragdoll 60, Malleable 21, Prismatic 10, Hinge 4, StiffSpring 3.
  - **KNOWN REMAINING bhkRigidBodyT+CMS violations (2026-07-04)**: 5 converted ANIMATED meshes still ship the forbidden pair (dungeons\ayleidruins\interior\traps\artrapspikepit01, dungeons\caves\cdoor03, dungeons\sewers\sewertunneldoor01, oblivion\clutter\traps\citadelhall3wayspiketrapbroken, oblivion\gate\obliviongate_simple) — keyframed child-node collision can't be demoted by the static bake pass; needs its own fix. ~100 speedtrees/ shrub+tree NIFs also contain the pair but are pre-made Skyblivion assets copied verbatim (not produced by our converter). Find them with `python tools/nif_block_scan.py <dir> --has bhkRigidBodyT --has bhkCompressedMeshShape`.
  - `asset_convert/mopp.py::walk_mopp()` is a full MOPP VM symbolic walker (PyFFI's parse_mopp opcode table + Skyrim-era opcodes: 0x52 TERM24, 0x29-0x2B DOUBLE_CUT24, 0x70 CHUNK_JUMP32), validated clean against 400 vanilla meshes. CLI: `python tools/mopp_validator.py <nif_or_dir> [--verbose|--summary|--histogram|--workers N]` (validates walk cleanliness AND exact terminal-key-set == shape-key decode). Vanilla opcode set observed: 0x01-0x06, 0x09-0x0B, 0x10-0x1C, 0x20-0x28 (0x29-0x2B rare), 0x30-0x53 — never 0x07/0x08/0x70; emit only these.
  - **MOPP_RL.exe is GONE (2026-07-03): all mesh collision is built by `asset_convert/cms_builder.py`**. History: MOPP_RL's chunked bytecode (0x70 chunk jumps, PC engine mis-executes) was first dechunked (`mopp.py::dechunk_mopp`), then its bytecode was replaced wholesale with Havok-bridge output — and the intermittent CTD STILL persisted (crash `SkyrimSE.exe+07D4C4B` fn 43870, runaway `hkpAllCdPointTempCollector` scan → EXCEPTION_STACK_OVERFLOW; Collision Sentinel: `CULPRIT ... key=0xFFFFFFFF` on the same meshes). Root cause was never the bytecode (see bhkRigidBodyT bullet below). MOPP_RL, its template.nif, and the dechunk fallback are all removed from the pipeline; `dechunk_mopp` remains in mopp.py for forensics only.
  - **CMS collision is built in pure Python + real Havok (2026-07-03)**: `cms_builder.py::build_cms_collision(tris_hu, sk_material_crc, NifFormat)` builds the whole bhkMoppBvTreeShape→bhkCompressedMeshShape→bhkCompressedMeshShapeData chain from a triangle soup: bpi=17/bpw=18, error=0.001, one identity bhkCMSDTransform, chunk = spatial bucket (split until extent <60 hu, ≤2000 tris), chunk translation = bucket min corner, u16 offsets = (v−min)×1000, triples-only indices (num_strips=0 — engine key decode identical to strips, `key=(ci+1)<<18|offset`), tris larger than the u16 span → big tris. MOPP bytecode + TWO_SIDED welding come from `external/mopp_bridge/dovah_hkp_mesh_mopp_bridge.exe` (Havok's real `hkpMoppUtility::buildCode`, chunk subdivision off, terminal keys self-validated by Havok's find-all-keys VM) — bridge input is `decode_cms()` of the freshly built block so MOPP/welding are computed over the exact quantized geometry the engine will decode. Welding u16 goes at the tri's first-index slot in chunk `indices_2` (= key offset); big-tri welding in `unknown_short_1`. Output re-verified in Python (walk clean + keys == `predict_keys`). Constants mirrored from vanilla: CMS radius=0.005, unknown_float_1=0.005, scale vec (1,1,1,0), data unknown_int_3=1, chunk unknown_short_1=0xFFFF, material layer=1. Wired in `collision.py::_rebuild_mesh_collision` (handles strips/packed/stale-Oblivion-MOPP sources; strips verts are GAME units → ÷70 to Skyrim hu; packed verts ×0.1). Fallback when the bridge fails: bare `_packed_from_tris` (no MOPP; packed data verts are stored ×10 hu = 1/7 game scale). NaN-vert tris are filtered before building.
  - **The MOPP bridge exe** came from inside `tools/DovahNifWorkbench_v6_47.exe` (PyInstaller onefile; payload `backend_exact_mopp\dovah_hkp_mesh_mopp_bridge.exe` + full C++ source `native_hkp_mesh_mopp_bridge/`, re-extractable by parsing the CArchive TOC at the `MEI\014\013\012\013\016` cookie). CLI: `--input in.json [--output report.json] [--no-stdout]`; input JSON `{"vertices":[x,y,z,...], "triangles":[a,b,c,...], "shape_keys":[k,...]}` (keys optional, must be unique); report has `mopp_origin`, `mopp_scale`, `mopp_data_hex`, `welding_info` (TWO_SIDED, per source tri), `mopp_keys_match_shape_keys`. GUI batch mode is NOT needed — the exe is called per-shape by cms_builder.py (`run_mopp_bridge`).
  - **CMS shape-key encoding (validated 200/200 vanilla meshes: walked MOPP key set == predicted set — `asset_convert/cms.py::decode_cms/predict_keys`)**: chunk tri key = `(chunk_idx+1) << bitsPerWIndex | winding << bitsPerIndex | first_index_offset` where the offset is the tri's first index position in the chunk's indices array; strips yield sliding-window tris (winding = window ordinal parity within the strip), then remaining indices are independent triples (winding 0, stride 3); big tris = part 0, key = big-tri index. Chunk vertex = chunk.translation + transform.translation + u16/1000 (rotate by transform quat if non-identity). PyFFI 2.2.3 field quirks: chunk welding array = `indices_2`, big-tri welding = `unknown_short_1`, big-tri fields `triangle_1/2/3` index into `big_verts`.
  - **PyFFI parse_mopp 0x0B (TERM_REOFFSET32) is WRONG** ("unsure about first two arguments" — reads only operand bytes 3-4): the operand is a full 32-bit big-endian value that SETS the terminal offset, and Skyrim CMS keys carry the chunk part in the HIGH bytes (0x00040000 = chunk 0). With the 2-byte read, every terminal after a 0x0B loses its chunk part — this made valid keys look like out-of-range "big tri" keys (a red herring chased for hours; vanilla showed the identical false pattern, which is what exposed the walker bug). Fixed in `walk_mopp`. Welding values legitimately span the full u16 range incl. ≥0x8000 and 0xffff — NOT a corruption signal (vanilla does the same).
  - `bhkCompressedMeshShape.target` must point to the BSFadeNode root (identity transform). Static collision MUST be on the root BSFadeNode — having bhkCollisionObject on a child NiNode causes STACK_OVERFLOW in Skyrim's `hkpCollisionDispatcher`.
  - **bhkRigidBodyT + CMS/MOPP = intermittent CTD — THE AnvilCastleGreatHall root cause (2026-07-03)**: vanilla Skyrim NEVER pairs a transformed rigid body with CompressedMesh collision — **0 of 6,341 vanilla CMS meshes contain bhkRigidBodyT** (checked by binary grep — block type names are plaintext in NIF headers). Shipping one exercises an engine path Bethesda never tested: queries intermittently resolve to HK_INVALID_SHAPE_KEY (Collision Sentinel `key=0xFFFFFFFF`) → runaway `hkpAllCdPointTempCollector` scan (Sentinel EVENT `b=129` vs the 128-slot stack collector) → EXCEPTION_STACK_OVERFLOW at `SkyrimSE.exe+07D4C4B`. Every Sentinel CULPRIT was a rotated-root mesh whose wrap pass produced bhkRigidBodyT+CMS ("diagonal/curved architecture" pattern). This explains all earlier observations: identity-body configs never crashed (only had rotated collision); transformed-body configs (bodyT OR collision on rotated child node) crashed ~50%. Replacing the MOPP bytecode alone did NOT fix it — the bytecode was never the problem.
  - **Root rotation wrap + collision (final design 2026-07-03)**: when the wrap pass zeroes the root transform L=(R,T), `bake_node_transform_into_body()` still composes bodyT' = L ∘ bodyT (in Oblivion hu; PyFFI `m_ij` names are the TRANSPOSE of the engine's column-vector matrix; rotation is QuaternionXYZW; ×0.1 rescale happens in `_convert_collision`). But for MESH collision the transform never reaches the file: `_bake_body_transform_into_tris()` applies the final bodyT to the triangle soup and DEMOTES the body back to a plain identity bhkRigidBody (class swap) before `build_cms_collision` runs — the output matches vanilla exactly (identity plain body, geometry in the world frame). Collision stays on the root BSFadeNode; CMS target = root. Regression test: `TestCollisionTargetPointsToRoot::test_static_collision_stays_on_root_when_wrapped` (asserts plain identity body + decoded CMS centroids match the source collision in the L∘bodyT frame within quantization — catches conjugate/transpose convention errors). Primitive shapes (convex/box/capsule, incl. constrained sign bodies) legitimately keep bhkRigidBodyT — vanilla does too.
  - **NaN geometry = silent cell-load CTD, NO crash log (2026-07-04, the AnvilMagesGuild/AnvilCastlePrivateQuarters root cause)**: some Oblivion source meshes ship non-finite floats in RENDER geometry (anvildooruc02.nif: 9 NaN UVs; middlecandlestickfloor03fake.nif: 2 NaN UVs — exactly one such mesh in each crashing cell, found by intersecting `tools/cell_meshes.py` output with a `tools/collision_sanity.py --geometry` sweep). Oblivion's renderer tolerated them; SSE dies at cell load WITHOUT writing a crash log (fail-fast, not a loggable exception) — collision was never involved. Fixed by `_sanitize_geometry_data()` in nif_converter.py (runs right after `_resolve_palette_strings`, BEFORE tangent computation/skin retarget so NaNs can't propagate): NaN UVs→0, NaN verts→finite centroid (+ bound-sphere recompute), NaN normals/tangents→+Z, NaN vertex colors→1. NOTE: the PyFFI warning summary from a full conversion run showed `nan_in_vertices: 155` — other meshes in the tree carry NaN too and previously shipped unsanitized; a full mesh reconversion (pipeline now sanitizes) or a `collision_sanity.py --geometry` sweep of output finds/fixes the rest.
  - NiParticleSystem: NiGeometry body needs format conversion (MaterialData→NumMaterials, Properties removed for UV2>34, FarBegin/End added for UV2≥83). IMPLEMENTED — `_convert_particle_system()` creates fresh NiPSysData with `bs_max_vertices = max(old_num_vertices, 75)`, keeps all modifiers, sets `base_scale=1.0` on NiPSysGrowFadeModifier.
- **PyFFI 2.2.3 version-condition bugs**: PyFFI's nif.xml has WRONG version conditions for some fields. Must monkey-patch at import time:
  - `NiPSysGrowFadeModifier.base_scale`: PyFFI has `userver="11"` (exact match on user_version=11). Correct condition per newer nif.xml: `User Version 2 >= 34`. Since we write `user_version=12` (Skyrim), PyFFI silently skips the field. Fix: set `_attrs[base_scale].userver = None` in monkey-patch.
  - Without the fix, `base_scale` defaults to 0.0 → particles invisible (scale = 0 × grow = 0).
  - The `bhkMoppBvTreeShape.build_type` field's vercond (`user_version >= 12`) is correct and does NOT need patching.
- **NIF reference docs**: NifSkope nif.xml at `external/NifSkope Built/nif.xml`, NifSkope HTML docs at `external/NifSkope Built/doc/`, NifSkope source at `external/nifskope-2.0.dev7/src/`
- **NIF BSStream versions**: 83 = Skyrim LE, 100 = Skyrim SE optimized. SE can load BSStream 83 files with NiTriShape geometry.
- **DDS textures**: Oblivion uses DXT1/DXT3/DXT5. Skyrim SE uses BC7/BC5/BC1 compression. May need re-export.
- **BSA archives**: Oblivion BSA format differs from Skyrim BSA. Need re-packing.
- **File paths**: The export prepends `tes4\` to all asset paths to avoid conflicts with Skyrim's own assets.

## Asset Pipeline

The `-ExtractAssets` flag triggers BSA extraction and mesh conversion:

1. **BSA Extraction** — Uses `bsab.exe` (from external/fnv-to-fo4/bin/bsab/) to extract meshes and textures from Oblivion BSA archives
2. **Mesh Conversion** — Uses PyFFI-based NIFConverter (from external/NIFConverter/) to convert Oblivion NIF 20.0.0.4/5 → Skyrim NIF 20.2.0.7
3. **Texture Copy** — DXT textures from Oblivion are compatible with Skyrim; copied as-is under `tes4\` namespace
   - **Path rewriting (`_rewrite_tex_path`) must normalise separators FIRST** (fixed 2026-07-27). Oblivion NIFs mix `/` and `\`, sometimes in one file. Testing only for a backslash `'textures\'` prefix let `textures/lowres/foo.dds` fall through and come out as `Textures\tes4\textures/lowres/foo.dds` — a path resolving to nothing, so the mesh renders untextured and the LOD tiles built from it reference 100 nonexistent textures. 96 Morrowind_ob source NIFs hit this; **zero Oblivion.esm ones**, which is why it stayed hidden.
   - `textures\lowres\` is an Oblivion **_far.nif authoring convention** for low-res LOD copies (pyffi ships a `modify_texturepathlowres` spell writing exactly this prefix, documented "used mainly for making _far.nifs"). We ship no lowres tree — converted textures live at the normal path — so the segment is **dropped**, resolving the reference to the real texture. The rewrite is idempotent on already-correct `Textures\tes4\…` paths.
4. **BSA Repacking** — Not yet automated. Use BSArch.exe or Skyrim CK Archive tool.

### Prerequisites for mesh conversion
- Python 3.x
- PyFFI (`pip install PyFFI`)
- `external/mopp_bridge/dovah_hkp_mesh_mopp_bridge.exe` (checked in — Havok MOPP/welding compiler)

### BSA naming conventions (Oblivion)
- `Oblivion - Meshes.bsa`, `Oblivion - Textures - Compressed.bsa`
- `DLCShiveringIsles - Meshes.bsa`, `DLCShiveringIsles - Textures.bsa`
- `Knights.bsa` (single BSA for smaller DLCs)

## DOOR conversion notes
- TES4 FNAM bit 0 = "Oblivion gate" — **clear this bit** when writing TES5 FNAM (no TES5 equivalent, may corrupt flags)
- TES4 bits 1-3 (Automatic, Hidden, Minimal Use) map directly to TES5 bits 1-3
- XTEL Door FormID is remapped via get_formid() — both sides of a teleport pair must be in the output
- TES4 XTEL = 28 bytes (no flags field); TES5 XTEL = 32 bytes — must append 4 bytes of flags (0x00000000 = default) when writing TES5 XTEL
- Doors without XTEL are correctly treated as open/close doors

## NIF mesh rotation
- Some Oblivion architecture/static NIFs have a non-identity rotation on their root NiNode (from 3ds Max exporter)
- Skyrim's BSFadeNode ignores the root node's local rotation matrix for static placement (Oblivion's NiNode applied it); this means statics appear rotated in Skyrim
- **Fix (in nif_converter.py Pass 6c)**: For non-skinned NIFs, bake the root rotation into each direct child's local transform (R_child = R_root × R_child, T_child = R_root × T_child), then zero the root rotation. Skinned meshes excluded (need skeleton bone alignment).
- Simple zero-only reset (prior approach) does NOT fix the issue — the geometry is still in the rotated coordinate space; baking into children is required.

## NIF animated mesh conversion
- Oblivion animated doors/activators use keyframed collision (motion_system=6 in Oblivion format)
- Key differences from static collision in Skyrim:
  - bhkCollisionObject.flags = 137 (0x89 = ACTIVE | D_ANIMATED | bit 7)
  - bhkRigidBody.motion_system = 4 (MO_SYS_KEYFRAMED)
  - **bhkRigidBody.mass = 0 AND filter layer = 2 SKYL_ANIMSTATIC** (see below)
  - bhkRigidBody.quality_type = 1 (MO_QUAL_FIXED)
  - bhkRigidBody.unknown_byte = 10 (broadphase type for animated)
  - NiNode flags |= 0x80 (selective update sync for physics)

### Keyframed bodies: layer 2 ANIMSTATIC + mass 0 (2026-08-02, PENDING in-game confirmation)

**History, kept honest.** The earlier note here ("mass 0 is MANDATORY,
implemented") was wrong twice over: (a) the mass write was never in the shipped
code — the attempted `rb.mass = 0.0` inside the keyframed branch changed which
downstream shape path ran (hull decomposition keys on `mass > 0`), collapsed
the collision compound, and was reverted, so **the in-game test that "mass
changed nothing" tested a broken build, not the theory**; (b) the two
"real causes" it then blamed (adjacent `PlayGroup`s cancelling; one-sequence
hold-state dead end) were both fixed and the planks still failed in-game
(2026-08-02: "begins to animate, stops suddenly, doesn't finish" — so the
event DOES reach the graph and the sequence starts; something reclaims the
nodes mid-clip). Do not cite either as the mechanism again.

**The discriminator the earlier session missed is the collision filter
LAYER.** Census of every vanilla motion_system=4 body found
(`farmhouseanimdoor01`, `farmbtrapdoor01`, `rtirongate01`, `orcdoor01`,
`riftenkeepdoor01` ×2, `mrkmarketstalldoor01`, `rifrmsmbasewallgrate01`,
`rifrmsmsecretcabinetdoor01` ×2, `sldjailwallcollapse01`): **layer 2
SKYL_ANIMSTATIC and mass exactly 0.0, no exceptions.** Our one in-game-working
animated object (`prisonSecretWall01`, source-authored OL_ANIM_STATIC + mass 0)
also ships layer 2 / mass 0. The broken ones shipped **layer 10 PROPS**
(Oblivion authored the bricks/planks on OL_PROPS, which the 0-18 identity
remap passes through) with mass 40/100:
- `mwallplankbreakaway01` (Oblivion + Nehrim) — 8 planks × mass 40, layer 10
- `IDCrumbleWall01` (ImperialDungeon01) — 13 bricks × mass 100, layer 10

Fix in `_convert_collision`: the keyframed branch forces layer 2 on both
filters; mass is zeroed at the very END of the function (after the mass-keyed
decompose gate), so the only bytes that change are the two fields themselves —
verified by structural diff (block graph identical; prisonSecretWall01
unchanged in every field). Multiple keyframed bodies per NIF is vanilla-legal
(`riftenkeepdoor01` ships two).

Note `sldjailwallcollapse01`'s own pattern for multi-piece collapses: ONE
keyframed mass-0 helper body (`ColHelper01`) and NO collision on the 22
animated pieces — vanilla never gives each piece its own body. We keep
per-piece keyframed bodies (faithful to the source collision), normalized to
the vanilla per-body contract.

### Constrained trap islands are HELD, not dynamic (2026-08-05, in-game confirmed)

A swinging trap (`ctrapswingmacelong01`'s chain links + mace head,
`ctraplogs01`, `cprollingrock01`) is authored exactly like a breakaway piece:
`ms=6` KEYFRAMED bodies with **real mass** and `Unyielding = 1`, wired together
by constraints. Oblivion's own script states the contract in its header:

> `; On activation havok will turn on and logs will roll` — `CTrapLogs01SCRIPT`

The old rule sent any `ms=6` body with *mass + a constraint* to **DYNAMIC**
(case 2), which is why **every swinging trap swung freely the instant the cell
loaded**, before anything tripped it. The opposite error (mass-0 keyframed)
welds the trap solid forever. Both were wrong for the same reason: the island is
**held rigid until the trap script fires**.

Fix (`_node_is_held_trap`): a constrained island member ships **KEYFRAMED but
keeps its authored mass**, and the converted script releases it with
`SetMotionType(Motion_Dynamic)`. Membership is checked **island-wide, not
per-body** — a chain link routinely carries mass with `num_constraints == 0` and
hangs off a neighbour's constraint (same reason `collision_extract` checks
constraints file-wide).

Vanilla `trapmace01` ships its links dynamic because a *Skyrim* trap has no
script-held phase; ours must reproduce Oblivion's held phase instead. Do not
"correct" ours to match vanilla here.

**But the MOTION TYPE is the only thing the held phase changes — quality_type
and solver_deactivation must be the POST-RELEASE values (2026-08-10, in-game
confirmed hang).** `SetMotionType(Motion_Dynamic)` swaps the motion type and
nothing else, so whatever the NIF ships for collision quality is what the body
simulates with *after* it is let go. The keyframed branch used to give every
animated body `quality_type=1` (MO_QUAL_FIXED, "static body") with
`solver_deactivation=1` (OFF) — right for a door, whose position really is
deterministic and which is never released, but wrong for a held trap. On
release that handed Havok a ring of mass-bearing bodies inside a live
constraint island all still claiming to be static with deactivation disabled;
the solver has no consistent state to converge on and the simulation step stops
completing. The game keeps running and never renders another frame — it reads
as a **freeze on a black loading screen**, not a crash, and nothing appears in
the Papyrus log.

Symptom that isolated it: walking onto the tripwire in Natural Caverns
(`ImperialDungeon05`, `CGTrigTripwire01` → three chained `CTrapSwingMaceLong01`)
hung the game, while the Vilverin tripwire was fine. Vilverin's trap is
`CTrapSwingMaceShort01`, and the two differ in exactly one way that matters:
the long mace hangs on **7 `chainLink` bodies with `bhkRagdollConstraint`**,
the short one had no chain at the point it was compared. Same mesh family,
same script, same `playgroup` — the constraint island was the whole difference.

Vanilla is the reference for the released state: `trapmace01.nif` ships every
`Link01..11` **and** `Mace01` at `quality_type=4` (MO_QUAL_MOVING) with
`solver_deactivation=2` (LOW). So `_convert_collision` now branches on
`breakaway_body`: held/breakaway pieces get 4/2, plain animated bodies keep
1/1. Plain animated doors (`cdoor03`, `ricketyfencegate01`) stay
**byte-identical**, and creature skeletons are untouched because they route
through `_convert_blend_collision` before this branch. Regression test:
`test_held_trap_ships_post_release_quality`.

**The release is keyed on the MESH, never the animation-group name.**
`physics_flags_from_data` bit 1 = "ships a keyframed body that kept a non-zero
mass", which `_convert_collision` writes for held pieces only (36 meshes).
Keying off the group name cannot work: `forward` is **491 of Oblivion's 850**
`playgroup` calls and is overwhelmingly gates, doors and portcullises that must
keep following their clip exactly — yet it is *also* the tripwire's break group.
The mesh knows which is which; the name does not.

**The bounds cache is SCHEMA-VERSIONED — bump the version when you add a field**
(2026-08-14). `mesh_bounds_cache.json` carries a `"__schema__": [N]` entry, and
`collision_extract.bounds_cache_is_current()` treats any cache written at a
lower version — or with no stamp at all — as missing, so it regenerates instead
of being trusted. Bump `BOUNDS_SCHEMA_VERSION` in the same commit as any change
to an entry's fields.

This is not hypothetical tidiness; skipping it shipped a bug. Entries are plain
lists, so a cache written before a field existed parses cleanly and reads as
**zero** for that field, which is indistinguishable from a computed zero. The
scan used to run only when the file was **absent**, so when bit 1 (HELD) shipped
on 2026-08-05 Nehrim simply kept its 2026-08-02 cache: 0 of its 11,946 meshes
carried the bit, `needs_havok_release` answered False for every one, and **no**
converted `playgroup` emitted `TES4Polyfill.ReleaseBreakaway`.
`mwallplankbreakaway01`'s planks stopped falling. Oblivion's cache happened to
be rebuilt an hour after that commit, so the *same mesh* still worked there —
which made it look like a Nehrim mesh bug rather than a stale cache. Diagnostic
that settles it in one step: compare the entry length for the same path key
across two plugins' caches (`[…, 2]` vs a bare 6-element list).

`--scripts-only` cannot rebuild the cache (it runs with no mesh scan), so
`script_convert/pipeline.py` prints a loud warning when the cache is stale
rather than silently emitting scripts with no release. The script stage also
has to load that cache in **both** the parent and the spawned workers (Windows
spawn does not inherit module state), or every lookup silently answers 0.

**Layer 14 (`OL_TRAP`) on the striking body is LOAD-BEARING — never remap it.**
`_remap_world_filter` passes 14 through unchanged and must keep doing so: it is
the layer whose contact raises Skyrim's `OnTrapHitStart`, which is the ONLY
thing that makes a converted trap deal damage (see the trap-damage section of
[papyrus_conversion_notes.md](papyrus_conversion_notes.md) — the damage lives
in the script's `fTrapDamage` variables, not in the mesh). Oblivion and Skyrim
agree on the idiom: `ctrapswingmacelong01`'s mace-end link is layer 14 with its
chain on 10, and vanilla `trapmace01` is identical (Mace01 = 14, Link01-11 =
10). Flattening 14 → 10 "for consistency" would silently disarm every trap.

### The Rest state is CORRECT — the animobject crash is elsewhere (2026-08-10)

Recorded so the next session does not re-tread this. `pSequence=''` on
`GamebryoSequenceGeneratorRest` is **right** and must not be changed: nothing
should play on cell load, for doors, rubble AND plants alike (the Spiddal
plant animates when the player approaches, driven by its script, not by the
graph starting in Forward).

Two "fixes" were tried and both were wrong:

| change | crash | door |
|---|---|---|
| `pSequence=sequences[0]`, `fPercent=0` | stopped | **opened on cell load** |
| non-empty sentinel naming no sequence | **still crashed** (in Generator00) | ok |

The decisive evidence: the FIRST crash of this family
(crash-2026-08-10-00-42-35) was already on **`GamebryoSequenceGenerator00`** —
the generator that plays `Forward` — not on the Rest generator. So the empty
Rest name was never the cause, and every Rest-state change merely moved the
symptom.

Also ruled out by census (700 vanilla meshes, 149 sequences): a **dataless
interpolator is legal**. Vanilla ships 72 dataless `NiFloatInterpolator`, 248
`NiBoolInterpolator`, 259 `NiBoolTimelineInterpolator` and even 4 dataless
`NiTransformInterpolator` (fxbatgroup, fxpoisongaswithonoff,
sprigganfxtestunified). Both crashing meshes carry dataless blocks, but so does
working vanilla content — it is not the discriminator.

RESOLVED (same day): the crash was **empty text key values** in the activated
sequence — see "🔴 A graph-bound mesh must ship NO empty text keys" below.
The secret door's `Forward` plays fine because its keys are only
`start`/`end`, both non-empty; the plants shipped Oblivion-authored empty
keys.

### Every state needs a real transitions array — including at ONE sequence (2026-08-01)

`_transitions(exclude_state=i)` gives each motion state "every OTHER sequence",
so a repeated event cannot restart a sequence mid-play. For a **one-sequence**
object that set is EMPTY and the emitter writes `transitions=null` — the exact
dead end the Rest-state comment warns about. `IDCrumbleWall01`'s only sequence
is `Unequip`, so once it played it could never be re-entered and `OnReset` was
inert. Fix: when the exclusion would empty the array, keep the self-transition.
2- and 3-sequence graphs are byte-identical to before, so the working
`prisonSecretWall01` is unaffected.

The regression test now runs at 1, 2 and 3 sequences — it only covered the
2-sequence case, which is why this shipped.

### Sequence controlled-block ID strings are the ENGINE'S LOOKUP KEY (2026-08-02)

The engine resolves each controlled block at sequence activation BY STRING: on
node `<node_name>` find the property whose class is `<property_type>`, then its
controller of class `<controller_type>`, disambiguated by `<variable_1>`.  Our
shader-controller rewrites swapped the controller block + `controller_type` but
left Oblivion's strings — `property_type='NiTexturingProperty'` (a class that
no longer exists in the file) and `variable_1='0-0-TT_TRANSLATE_V'` — so the
lookup failed silently and the interpolator never drove the shader:
palacefont01's fountain shipped a correct V-offset curve that never played.
Vanilla convention (beehive01, blackpool, dweastrolabehub01, every entry
sampled): `property_type` = shader class name, `variable_1` =
`str(type_of_controlled_variable/color)` (`'8'`, `'11'`, …), `variable_2` = `''`.
Fixed generically in `_normalize_shader_cb_strings` (runs at the end of
`_match_seq_shader_types`).  PSys controlled-block strings were already
vanilla-identical (`var1='NiPSysBoxEmitter:0'`, `var2='BirthRate'`) — leave.

**Shared Oblivion properties → one entry per shape (2026-08-18, the Font of
Madness's upper tier).** Oblivion shares one `NiTexturingProperty` /
`NiMaterialProperty` block between several shapes and a sequence entry names
only ONE of them: palacefont01's `Water` entry drives texturing property #71,
which `Water03`, `PalaceWaterL2` and `PalaceWaterR02` also wear, so in TES4 one
entry scrolls all four. Skyrim gives every converted shape its own
`BS*ShaderProperty`, so only the named shape animated (lower tier moving, upper
frozen). `_process_controller_manager` now indexes property controllers →
wearing shapes once per manager (`_property_ctrl_index`) and, for each
retargeted texture-transform / alpha / material-colour entry, appends one entry
per sibling with a cloned controller + interpolator (`_fan_out_shared_entries`,
key data shared; `_attach_seq_shader_controllers` then hangs each off its own
shader). 33 Oblivion.esm meshes / 703 entries (oblivionwargateani02 168,
citadeldeadralordscenterring 106, obeliskenergybox01 102, se01waitingroomwalls
36).

<a id="morph-emulation"></a>
### NiGeomMorpherController does not exist in Skyrim — emulate as a shape swap (⚠ SCALE version REVERTED 2026-08-10, see notice below)

The SSE exe has NO `NiGeomMorpherController` RTTI class (only the orphaned
`NiMorphData` remains) and vanilla ships 0 uses, so morph entries HAD to be
dropped — but the morph IS the visible effect for 18 Oblivion meshes
(ctrigtripwire01's wire snap, se01waitingroomwalls, obliviongate_forming,
gnarlspawner…).  `_emulate_morphs` (fed by a harvest at the drop site in
`_process_controller_manager`) bakes each animated morph target into a sibling
copy of the shape (relative_targets → base verts + deltas) and CUTS from base
to copy where the weight curve crosses 0.5.  A smooth crossfade degrades to a
cut — the closest this engine gets.

**The cut is animated as wrapper-node SCALE, never as a NiVisController on the
geometry.**  Each shape — the base and every baked target — is wrapped in its
own `NiNode` (`"<shape> Swap"`), and the sequence drives that node's scale
1 ↔ 0 through an ordinary `NiTransformController` entry bound to the manager's
`NiMultiTargetTransformController`.  Clone wrappers rest at scale 0 (so the
authored rest pose shows only the base shape) and the clone geometry itself
ships VISIBLE; wrappers are added to the MTC's `extra_targets` and to the
manager's `NiDefaultAVObjectPalette`.  Scale keys are `LINEAR` (1) floats with
a hold key one frame (1/30 s) before each transition, which expresses the step
without touching the bool-key machinery.

> ## 🛑 REVERTED 2026-08-10 — THE SCALE SWAP FREEZES THE GAME
>
> **Everything described above and below about the wrapper-node SCALE swap is
> the state of `90d04a3`, which is NOT what the tree currently builds.**
> `_emulate_morphs` has been reverted to the pre-`90d04a3` **NiVisController**
> implementation because the scale swap hard-freezes Skyrim.
>
> ### The symptom
> Walking onto the tripwire in **Natural Caverns / `ImperialDungeon05`**
> (ref `00051AC9`, base `CGTrigTripwire01` `000CD4CC`) freezes the game: no
> crash, no crash log, nothing in the Papyrus log — the process stays alive
> and never renders another frame.  The **same mesh file** in **Vilverin**
> (ref `0006BF50`, base `CTrigTripwire01` `0004CAD9`) works perfectly, wire
> snap and all.  One `ctrigtripwire01.nif` serves both cells, so the mesh
> alone cannot explain the difference — that contradiction was never resolved.
>
> ### How it was isolated (in-game bisection, user-run)
> Each removed in turn from `output/`, one at a time:
> * long mace `ctrapswingmacelong01.nif` removed → **still froze**
> * tripwire `ctrigtripwire01.nif` removed → **no freeze**
> * tripwire restored, `ctrigtripwire01_behavior/` removed → **froze**
>   (so the animobject graph is innocent)
> * tripwire rebuilt with `_emulate_morphs` disabled → **no freeze**
>
> That last step is the definitive result: **morph emulation ON = freeze,
> OFF = no freeze.**  The trap-damage `OnTrapHitStart` scripts were also
> stripped and rebuilt separately — the freeze persisted, so the scripts are
> innocent too.
>
> ### Four fixes attempted, all failed in-game
> 1. **Move the wrappers off the MTC** (own `NiTransformController` each) —
>    still froze, and the wire stopped breaking.
> 2. **Give the entries full translation+rotation+scale key channels** —
>    still froze.
> 3. **Replace scale with a shader-ALPHA cross-fade**
>    (`BSLightingShaderPropertyFloatController`, variable 12) — no freeze
>    reported, but the wire **did not visually break**, so it is not a fix.
> 4. **Add a constant rotation key channel** so the entry reads `r=3 s=3`
>    like vanilla's `t=0 r=1 s=0` shape — still froze.
>
> ### Verified facts — do NOT re-derive these
> From the **GOG/AE** `SkyrimSE.exe` (the Steam copy is DRM-packed and
> disassembles to garbage — `tools/skyrim_disasm.py` still defaults to the
> Steam path, pass `--exe` explicitly):
> * `NiMultiTargetTransformController`: interpolator slots at `+0x48`, sized
>   `count * 0x48`, allocated at `0xd0d857`; target pointers at `+0x50`,
>   `count * 8`, zero-filled at `0xd0d91f`; `num_extra_targets` is a **ushort**
>   at `+0x58`.  Both arrays are walked **strictly by index** (`0xd0ca20`,
>   bounded by `cmp bx, word ptr [rdi+0x58]`).
> * Blend bookkeeping at `0xd0b640` walks `0x20`-byte `NiBlendInterpolator`
>   records, reading each slot's interpolator pointer and **priority byte** at
>   `+0x10` to track highest / second-highest contributor.
> * `NiTransformInterpolator`: `+0x18` translation, `+0x24` rotation quat,
>   `+0x34` scale, `+0x38` data pointer.
> * `NiTransformData`: `+0x10`/`+0x18` translation count/keys, `+0x20`/`+0x28`
>   rotation, `+0x30` scale keys, `+0x14`/`+0x24` key types.
> * `NiControllerSequence`: controlled blocks are a 32-byte stride array at
>   `+0x20`, count at `+0x18`, with a priority-ordered insertion pass at
>   `0xd08890`.  Its constructor seeds float fields with `0xff7fffff`
>   (**-FLT_MAX**) at `0xd04549`–`0xd04589`, so that sentinel is
>   **engine-native and correct** — writing real values there is wrong.
>
> Ruled out by measurement, all dead ends:
> * MTC target **count** — vanilla `alduin.nif` ships **246** targets.
> * Targets with **no driving block** — vanilla `fxnocturnalbirdl.nif` has
>   10 targets and 1 block, 9 of them NULL.
> * Missing `NiBlendTransformInterpolator` blocks — vanilla ships **0**; the
>   engine allocates them at runtime.
> * MTC identity / manager-chain shape — ours matches vanilla exactly (one
>   MTC in the chain, all blocks binding to it).
> * Degenerate `scale = 0.0` — replacing it with `1e-4` did not help.
> * Orphaned manager-chain `NiTransformController` — present **identically**
>   in the pre-`90d04a3` build that works, so it is not the cause.
> * `-FLT_MAX` statics, node/child array consistency, palette registration,
>   scene-graph reachability, clone geometry flags, controlled-block
>   priorities, the two maces' NIFs and behaviour graphs (8 bodies + 7
>   constraints each, identical graph file sizes) — all verified equal.
>
> ### The one lead never chased to a conclusion
> The two placements differ in exactly two authored ways: `XSCL` (0.75 in
> ImperialDungeon05 vs 0.71 in Vilverin) and the **persistent** record flag
> (Vilverin's ref is `0x400` persistent, ImperialDungeon05's is not).  A
> non-persistent ref whose 3D unloads/reloads while a sequence holds MTC
> interpolator slots is the only mechanism found that is consistent with
> "same file, different cell, freezes *sometimes*".  Untested.
>
> ### What "reverted" means concretely
> `_emulate_morphs` is the pre-`90d04a3` body (`git show 90d04a3^:asset_convert/nif_converter.py`),
> plus its `_init_blend_interpolator` helper which `90d04a3` had deleted.
> Output for `ctrigtripwire01.nif` is block-for-block identical to that build
> (the only delta is `BSBehaviorGraphExtraData`, added by a later, unrelated
> commit).  **Consequence: the wire does not visibly snap.**  That is the
> accepted trade — a cosmetic loss instead of a hard freeze.  The two tests
> named below still assert the SCALE design and will fail against the
> reverted code; fix them together with the real fix.
>
> **When returning to this:** the scale swap itself is not obviously illegal,
> and it demonstrably works in Vilverin.  Start from the persistence /
> ref-scale difference above, not from the mesh — the mesh has been
> exhaustively compared and is identical in both cells.

**Do NOT "restore" the NiVisController version.**  *(Superseded — see the
revert notice above; the NiVisController version is what currently ships.)*
The first implementation
toggled `NiVisController` entries aimed at the NiTriShapes themselves; it
produced NO visible swap in-game across three rounds of fixes, and the vanilla
census explains why it was never trustworthy: sequence-driven NiVisController
controlled blocks target **NiNode / NiBillboardNode / particle systems in
1852/1852 cases and a NiTriShape in ZERO**.  Meanwhile transform entries on
plain NiNodes carrying scale keys are routine (406 in a 130-file sample), and
converted transform sequences are the one animation path already confirmed
working in-game (CharacterGen's secret wall).  The scale swap therefore reuses
only proven machinery and generates no `NiVisController` /
`NiBlendBoolInterpolator` at all — which also retires both Vilverin CTDs below
for this path.  Two tests in
`tests/test_asset_convert.py::TestAnimationBlockLayout` pin it:
`test_morph_emulation_never_targets_geometry` (no vis entries are synthesized)
and `test_tripwire_morph_ships_a_scale_swap` (converts the real
ctrigtripwire01 and asserts inverse scale curves, wrapper rest scales, MTC
extra-target + palette registration, and zero surviving NiVisController).

> **Note (2026-08-10):** the "406 scale-key transform entries in a 130-file
> sample" claim above **does not reproduce**.  A 250-mesh re-census found 36
> `NiTransformData` total, only 8 with scale keys, and every one of those is
> on `skeleton.nif` at a constant `1.0` — vanilla never animates a node's
> scale, and ZERO sequence entries are scale-only.  Vanilla makes geometry
> appear/disappear mid-sequence with shader float controllers instead:
> `BSEffectShaderPropertyFloatController` (25),
> `BSLightingShaderPropertyFloatController` (17),
> `BSNiAlphaPropertyTestRefController` (4); `NiTransformController` accounts
> for 4 and none drive scale.  Treat the original census as unreliable.

Historical note — **the two CTDs the vis-swap path caused**, kept because
`_normalize_blend_interpolators` still repairs blocks COPIED from Oblivion:

1. **NiBoolData keys must be `CONST_KEY` (5), never `LINEAR` (1).**  Writing 1
   CTD'd on entering Vilverin — an access violation at `0x0` inside
   `NiBoolData::Load`, `RSI/R14 = NiBoolData*`,
   `inputFilePath: ctrigtripwire01.nif`.  Census: **3449/3449 vanilla Skyrim
   and 1296/1296 Oblivion source NiBoolData store 5**; `nif [version].xml`
   documents type 5 as "Step function.  Used for visibility keys in
   NiBoolData".  The two types are byte-identical on disk
   (`{float time, byte value}`), so the file round-trips through PyFFI and
   NifSkope cleanly and nothing but the engine notices — hence check 3 in
   `tools/nif_block_type_audit.py`.
2. The Manager-Controlled flag defect below, which the same crash hunt found.

Both still apply to any NiBoolData / blend interpolator the converter copies
through; they are simply no longer reachable from morph emulation.

### NiBlendInterpolator must be Manager Controlled (2026-08-02)

Fixing the key type above moved the Vilverin CTD one block later, to
`lock inc [rax+0x08]` — an AddRef — with `RDI = NiVisController*` and
`rax = 0xBF800000421BED50`.  That high half is `-1.0f`, i.e. **float data being
dereferenced as a pointer**, which is the signature of a block read at the wrong
length.

`NiBlendInterpolator.Flags` bit 0 is **Manager Controlled**.  nif.xml makes the
next SEVEN fields (Interp Count, Single Index, High Priority, Next High
Priority, Single Time, High Weights Sum, Next High Weights Sum) conditional on
that bit being **clear** — so a manager-driven block is 7 bytes and a
free-standing one is 15.  We were writing `Flags=0` into a 7-byte block, so the
engine read 15 bytes, ran into the following block, and AddRef'd whatever it
found.  `Single Time` defaults to `-1.0f`, which is precisely the `0xBF800000`
in the faulting address.

Vanilla is unanimous: **8779/8779 `NiBlend*Interpolator` blocks store Flags=1,
Array Size=2** (2688 bool, 5520 float, 571 point3).

The underlying cause is a **PyFFI 2.2.3 broken layout** (cf. NiPSysData): it
models this block as `unknown_short` + `unknown_int` + `bool_value` instead of
`byte Flags, byte Array Size, float Weight Threshold, byte Value`.  So
`unknown_short = 0x0201` IS `Flags=1, ArraySize=2`.  These are **not padding** —
the usual "never touch unknown_*" rule does not apply, because they are real
named fields PyFFI failed to describe.  Critically this hits blocks **copied**
from Oblivion as well as synthesized ones: PyFFI reads them under the old
version's layout and rewrites them under Skyrim's, and the flags do not survive.
`_normalize_blend_interpolators` therefore stamps the header onto every blend
interpolator in the tree after all controller passes, and
`tools/nif_block_type_audit.py` checks it (check 4).  261 blocks across 26
Oblivion meshes were affected — gates, magic effects, creatures and the enemy
health bar, not just the morph-swap meshes.

### Oblivion `sound:` text keys are NATIVE in Skyrim — never rewrite them (2026-08-05)

**This section previously said the opposite.  The rewrite it described silenced
244 Oblivion meshes** — every animated gate, portcullis and prison door — and
was reverted after the user reported StoneWallGateDoor01 losing its iron creak.
Confirmed fixed in-game 2026-08-05.

SkyrimSE keeps Gamebryo's own text-key sound handler.  At `0x1401db723` (GOG
build) it compares the key against the literal **`"Sound: "`** (`r8d = 7`) with
**`_strnicmp`, which is CASE-INSENSITIVE**, so Oblivion's lowercase `sound: X`
matches, and it plays whatever follows those 7 characters (`lea rcx, [rbx + 7]`
at `0x1401db890`).  The same handler also accepts `"Enum: StopSounds "`.  Both
literals sit at file offsets `0x1635f50` / `0x168d0ec`.

**The trap:** the earlier pass searched the exe for lowercase `sound:`, found
nothing, and concluded the keyword did not exist.  The string is capitalised.
Case-fold before concluding a string is absent from the exe.

`SoundPlay.<SNDR EDID>` is a DIFFERENT, non-interchangeable channel: it is
matched against a behaviour graph's declared event-name table, so it only works
on meshes that have one (38 of the 39 vanilla meshes using it carry
`BSBehaviorGraphExtraData`).  Converted doors deliberately have **no** graph —
attaching one to an Open/Close door CTDs it on cell load — so the rewritten key
matched nothing and was dropped.  Creature/actor sounds still correctly use
`SoundPlay.` because they DO go through a graph (`hkx_behavior.py`).

`_convert_sound_text_keys` is therefore a documented no-op returning 0.

### DOOR sound records: SNAM/ANAM must name an SNDR (2026-08-05)

Separate defect found in the same investigation.  TES5 `DOOR` SNAM (open) /
ANAM (close) / BNAM (loop) reference a sound **descriptor**, not a SOUN — xEdit
declares `wbFormIDCk(SNAM, 'Sound - Open', [SNDR])`, and all 90 sounded vanilla
Skyrim DOORs agree (WRDragonSideDoor01's SNAM `0005AFC9` is the SNDR
`DRSWoodImperialDouble01OpenSD`).  The converter was writing the TES4 SOUN id,
so all 417 sounded Oblivion doors held a wrong-typed reference.

DOORs are written in import Phase 1, before Phase 3 mints the descriptors, so
`convert_DOOR` stores the SOUN id as a placeholder and
`items.patch_door_sounds` resolves it afterwards — the same approach
`actors.patch_actor_sounds` uses for CSDI.  Allocating descriptor ids earlier
would shift every other generated FormID.

Oblivion also lets a door's sound live ONLY in the mesh (the record has no
SNAM/ANAM at all — StoneWallGateDoor01 and 57 other doors).  Skyrim's record
channel is what vanilla relies on, so `asset_convert/door_sounds.py` reads the
model's `Open`/`Close` sequence text keys and `items.load_door_model_sounds`
lifts those names onto SNAM/ANAM.  The sequence NAME decides the slot, so the
NIF is parsed rather than byte-scanned.

### PlayGroup chains: NEVER convert to PlayAnimationAndWait (2026-08-02)

`PlayAnimationAndWait("<seq>", "<event>")` waits on a BEHAVIOR-GRAPH event.  A
BGSGamebryoSequenceGenerator state has no completion event and NIF text keys
are not delivered as anim events — vanilla proof: every gamebryo-sequence
object script (norsarcophagustopanim01script, dunsolitudejailopencelldoor, the
Solitude jail-wall scene) uses plain `PlayAnimation` with a state debounce and
never waits; the scripts that DO wait (`sarcophagusskulllock01script`
"alldone", `dunlabyanimateontrig` "done") drive native-hkx objects whose
events are havok annotations.  The wait blocks its thread forever.  Consecutive
same-frame PlayGroups therefore stay plain `PlayAnimation` calls (last event
wins — which also matches Oblivion's own queue-depth-1 PlayGroup semantics).

- BSXFlags must have bit 0 set (ANIMATED) → value 139 (0x8B) for animated meshes. Detect via NiControllerManager on root.
- Animation data: NiControllerSequence StringPalette offsets MUST be resolved BEFORE version upgrade (UV2=11→83). After upgrade, PyFFI switches to direct-string mode and offsets are ignored → empty node_name → crash.
- **EVERY `NiTimeController` needs "Compute Scaled Time" (flags bit 6, 0x40) or a `PlayAnimation()`d sequence NEVER MOVES — the CharacterGen secret-wall fix (2026-07-26, `_fix_controller_flags`)**: `nif.xml` `TimeControllerFlags` declares bit 6 `default="true"`, and Oblivion's engine computed scaled time unconditionally without ever writing the bit — every controller in the Chargen secret-wall/switch NIFs stores 12 / 40 / 44, always 0x40 **clear**. Skyrim reads the flag: the sequence binds its targets, `ObjectReference.PlayAnimation("Forward")` returns success and logs **no Papyrus error**, but scaled time never advances so the object sits on frame 0 forever. Symptom was maximally misleading — the quest stage said the wall had opened (the switch fired, `secretDoor` flipped 0→1, the timer ran; all visible in the `TES4CharGen` user log) while the wall physically stayed shut. Census: across 62 vanilla animated door/activator meshes (Windhelm animated secret doors, Nordic animated doors, Dwemer doors, Labyrinthian panel, Winterhold anim door) **157/157 `NiMultiTargetTransformController` have flags=108 (0x6C)** and every other controller — `NiTransformController`, `NiControllerManager`, `NiVisController`, `NiFloatExtraDataController` — has **76 (0x4C)**; both set 0x40, and 108 vs our 44 differs *only* in this bit. Fix ORs 0x40 into every `NiTimeController` in the tree before the version upgrade, so activators/doors/traps/levers are all covered. This generalizes the emitter-controller rule below (which had it only for `NiPSysEmitterCtlr`/`NiPSysUpdateCtlr`) and the `0x48` already hardcoded on the flip-book `BSEffectShaderPropertyFloatController`.
  - **Things that were NOT the cause** (all verified fine, don't re-investigate): the Papyrus conversion (`PlayGroup` correctly routed to `PlayAnimation` via base-signature lookup); the dropped `prisonSecretWall01`/`... NonAccum` controlled blocks (genuinely empty — `data=None`, zero translation — the real motion is on the `bed`/`wall` transform tracks, which survive with all 111/21 keys); the missing `NiStringPalette` (correct — Skyrim uses direct strings); sequence names `Forward`/`Backward` (vanilla `VolunruudLeftDoor`/`RightDoor` use exactly these); and the absent ACTI `PNAM`/`FNAM` (marker colour + flags, cosmetic — 1739/1753 vanilla write FNAM=0).
  - **CORRECTION (2026-07-26): the "needs no BGED" claim previously recorded here was WRONG.** The earlier note reasoned that because 227 ACTI + 196 DOOR vanilla records ship `NiControllerManager` meshes, the in-NIF sequence was sufficient. That census is real but does not support the conclusion: `ObjectReference` exposes **two different animation paths** — `PlayGamebryoAnimation` drives an in-NIF `NiControllerSequence`, while **`PlayAnimation`/`PlayAnimationAndWait` drive the BEHAVIOUR GRAPH and require an animation graph manager**, which exists only when the root carries a `BSBehaviorGraphExtraData` naming an hkx project. `PlayGroup` converts to `PlayAnimation`, so without a BGED the call is accepted, returns immediately, logs no Papyrus error, and nothing moves. Fixed by generating the graph (below).

## Animated-object behaviour graphs (`asset_convert/hkx_animobject.py`)

**ONLY meshes whose sequences carry SCRIPT-DRIVEN group names get a GENERATED graph** — `Forward`, `Backward`, `FastForward`, `FastBackward`, `Left`, `Right`, `Equip`, `Unequip`, `SpecialIdle`, `Stagger` (`_SCRIPT_DRIVEN_SEQUENCES` in `nif_converter.py`; 161 trees on Oblivion.esm). Ambient `AutoPlay`/`AutoLoop` meshes point at vanilla's shared `GenericBehaviors\Autoplay.hkx` instead (next section). Generated by `collect_sequence_names` + `_add_animobject_bged`. Layout, sibling to the mesh so two animated NIFs in one folder never collide:

    <model>_behavior/<model>.hkx            project    (this is what BGED names)
    <model>_behavior/Characters/Character01.hkx
    <model>_behavior/CharacterAssets/Skeleton.hkx      1-bone; transforms live in the NIF
    <model>_behavior/Behaviors/Behavior00.hkx          state machine + Gamebryo generators

The bridge is **`BGSGamebryoSequenceGenerator`**, whose `pSequence` names a NIF `NiControllerSequence`. Each sequence becomes one state AND one same-named event, so `PlayAnimation("Forward")` sends `Forward` and lands on the generator bound to the `Forward` sequence, plus a synthetic **`Rest`** start state. BSX bit 0 (Animated) must also be set or the graph loads and never ticks.

### 🛑 A NIF CANNOT BE HOT-RELOADED — the engine caches it for the whole process

Skyrim parses each NIF once and keeps it in its model cache for the lifetime of
the process. `coc` out and back unloads the CELL, not the model: the reload
hands back the cached copy, so **overwriting the mesh on disk while the game is
running changes nothing.** There is no console command that drops it (`pcb`
purges the cell buffer, not the model cache).

Consequence for debugging: **every in-game observation describes the build that
was on disk when the game LAUNCHED.** Rebuilding a mesh mid-session and
re-entering the cell tests the OLD file and silently produces a "the fix did
nothing" result. On 2026-08-18 this invalidated several rounds of ambient-mesh
testing before it was noticed.

So: deploy the mesh, THEN relaunch, then test. One build per launch. Verify the
DEPLOYED file (`Data\meshes\...`, not just `output/`) before asking for a
relaunch — a wasted launch costs a full play cycle.

Note the deploy is hardlinked here (Vortex): writing `output/...` updates
`Data/...` in place, same inode. That makes deployment instant and is easy to
mistake for a working hot reload. It is not one.

### Ambient (self-playing) meshes: the vanilla AutoPlay/AutoLoop pair (2026-08-18, mechanism read out of the live engine)

Oblivion authors ambient scenery (arena spectator crowds, `palacefont01`'s
fountain, `watersurf01`, candle flames) as a STAT whose NIF holds a self-playing
`Idle` sequence. TES4 starts `Idle` on load; Skyrim starts nothing. What vanilla
does instead, and what `_autoplay_ambient_sequences` now emits:

- BGED → **`GenericBehaviors\Autoplay.hkx`**, the shared graph every one of the
  63 vanilla self-playing meshes points at (`hkx_animobject` returns it for
  meshes whose sequences are only ambient; anything a script drives by name
  keeps its generated project). Its state machine STARTS on `AutoplayState`
  (sequence **`AutoPlay`**), and on that sequence's `End` event hands off to
  `AutoLoopState` (sequence **`AutoLoop`**). Its events are `End`, `StopEffect`,
  `AutoOneOff`, `Reset`, `AutoReset` — `AutoPlay`/`AutoLoop` are sequence names,
  never events.
- The authored `Idle` becomes **`AutoLoop` and keeps its authored cycle type**;
  a full-length **`AutoPlay` clone with cycle type CLAMP** is added for the
  start state (shares the interpolators — the engine binds the same pointers
  from both and plays).
- **CycleType is 0 = LOOP, 1 = REVERSE, 2 = CLAMP.** All 116 Oblivion `Idle`
  sequences are 0. Vanilla: `AutoPlay` CLAMP 53/54, `AutoLoop` LOOP 39/53.
  Looping is the SEQUENCE's — `BGSGamebryoSequenceGenerator` has no looping
  field and `AutoLoopState` has no self-transition.

**Why it took ten builds** (all read back out of the running game with
`tools/nif_live.py sequences|nodes` on a loaded arena spectator, then patched
in memory with `set-cycle` / `set-pose` + `sae AutoReset` to prove each fix
before rebuilding):

1. *"Plays one cycle then freezes"* — the converter had `_CYCLE_LOOP = 2`, i.e.
   it wrote CLAMP into `AutoLoop`. Live: `Autoplay` state INACTIVE (finished,
   `End` fired), `AutoLoop` state ANIMATING with lastTime far past its end —
   frozen on the last frame. Flipping the loaded sequence's `cycleType` to 0 and
   `sae AutoReset` made it loop indefinitely.
2. *"Rotated ~90°"* — Bethesda's exporter writes the sequence's **accum root**
   (`Bip01`, `DoorLowerINT01`, `MetalGate`…) as an IDENTITY pose and moves the
   node's real transform onto the **`<accum> NonAccum`** child (crowd:
   NonAccum key 0 = Bip01's authored (−0.34,−1.64,64.07) / 82.5° Z; census of
   464 Oblivion NIFs: 853 accum-root entries, 815 identity poses, 0 that
   move). Both engines apply the identity and NonAccum restores the world
   pose. Our data-less sentinel rule (rotation/scale → −FLT_MAX) left Bip01's
   authored 82.5° in place while NonAccum re-applied its own 82.5° → the crowd
   faced 165° off whenever the sequence actually played, and was correct only
   in builds where nothing played. `_accum_root_mode` now leaves a
   'transferred' accum-root entry exactly as authored (identity applied) and
   sentinels every channel only for an 'orphan' (nothing carries the
   transform). Live: Bip01 → identity, NonAccum → (−0.34,−1.64,~64) at 82.5°,
   crowd at the authored pose, looping.

Ruled out along the way, do not retry: STAT→MSTT promotion (crashed on save
load — 94 vanilla STATs carry a BGED, the record type is not the gate);
`selfTransitionMode` FORCE_TRANSITION_TO_START_STATE (looping is the
sequence's); a generated per-mesh graph instead of the shared one (works, but
the shared one is what vanilla ships and what is verified live).

**`sae <event>` is the fastest first diagnostic** for any "animated object does
nothing": empty reply = the graph is bound and knows the event; *"not processed
by the graph"* = no such event or no graph. Then `python tools/nif_live.py
sequences <ref>` (state, cycleType, lastTime per sequence) and `nodes <ref>
--names ... --samples N` (is the bone moving, and where is it) instead of
theorising.

### The four defects that had to be fixed before it worked in-game (2026-07-26, all CONFIRMED)

Every one was invisible to structural inspection **and to NifSkope, which renders and animates the NIF perfectly while never loading the hkx at all** — so "it's fine in NifSkope" tells you nothing about any of these. In symptom order:

1. **BGED must NOT carry a `meshes\` prefix — the object is otherwise NEVER RENDERED.** The engine prepends `Meshes\%s` itself, so `meshes\tes4\…` resolves to `Meshes\meshes\tes4\…`, the project is never found, and the object silently gets no graph and never draws. Vanilla stores `Clutter\BlackPool\BlackPoolSecretDoor\NocturnalsSecretDoor01.hkx`; our own working bow rig stores `Weapons\Bow\BowProject.hkx`. **The path is relative to `meshes\`, not to `data\`.**
2. **The skeleton's bone must be the fixed dummy name `x_SingleBone`, never the model stem.** The rig is a placeholder (the real motion is in the NIF's sequences), and vanilla's `SingleBoneSkeleton.hkx` uses that reserved name precisely so it can never collide with a NIF node. Naming it after the model made the engine bind the graph's identity bind pose onto the object and place it **far from its authored worldspace position**.
3. **`startStateId` must point at a state that plays NOTHING.** Vanilla starts on an idle (`BlackPoolSecretDoor` `startStateId=3` = `AnimIdle01`) and reaches the motion only by event. Oblivion sources have no idle sequence — a converted wall has only `Forward`/`Backward` — so starting on state 0 made the wall **swing open by itself the instant the cell loaded**. Fix: synthesise a `Rest` state whose `pSequence` is empty (it holds the NIF's authored rest pose = closed) and start there. It is the LAST state, so the event→stateId mapping of the real sequences is untouched.
4. **Transitions must live ON EACH STATE, not only in the machine's `wildcardTransitions`.** Vanilla's Gamebryo state machine sets `wildcardTransitions=null` and gives every state its own `hkbStateMachineTransitionInfoArray` (`State00` carries event 0 → state 4). Leaving `Rest.transitions = null` made the start state a **DEAD END**: nothing could open the wall again, from the quest *or* from console `activate`. Each state now reaches every *other* sequence (self-transitions excluded, or a repeated event restarts the sequence mid-play); the global wildcard array is kept as a harmless second route.

**`Open`/`Close` MUST NOT get a graph — attaching one is a CTD (2026-07-26).** They are the engine's own DOOR group names, driven natively through the NIF's `NiControllerManager`; no converted script ever names them (census of 18,566 output scripts: Forward 418, Backward 192, Unequip 45, Equip 27, SpecialIdle 10, FastForward 8, Left 6, FastBackward 6, Right 5, Stagger 1 — **zero Open/Close**). `prisonCellGate01` animated perfectly before the graph existed; giving it one made the engine bind the sequence through the graph instead of natively and crash on cell load (`EXCEPTION_ACCESS_VIOLATION`, `movdqu xmm2,[rax]` with `rax=0`, relevant objects `BGSGamebryoSequenceGenerator "GamebryoSequenceGenerator00"` + `hkbBehaviorGraph "prisoncellgate01"`). Vanilla agrees: the graph-driven `NocturnalsSecretDoor01` uses `AnimIdle01`/`AnimPlay01`, never Open/Close. **A mesh that already animates is not a mesh that needs a graph — check whether a script actually drives it first.**

**Template: vanilla `NocturnalsSecretDoor01`** — `Behaviors/Behavior00.hkx` ships loose at `references/Skyrim Animations/meshes/clutter/blackpool/blackpoolsecretdoor/`, the NIF at `references/Skyrim Meshes/meshes/clutter/blackpool/blackpoolsecretdoor/nocturnalssecretdoor01.nif` (BGED + BSX 0x0B + a 12-object `NiDefaultAVObjectPalette`). Decompile with `hkx_xml.decompile_hkx` and match field-for-field. Traps found the hard way:

- **hkxcmd fails SILENTLY on a malformed packfile**: it prints `Converting '...'`, exits non-zero, and writes **no file and no error text**. A missing or extra param is indistinguishable from any other failure, so bisect against the decompiled vanilla file rather than guessing.
- **`BGSGamebryoSequenceGenerator` takes exactly `pSequence`, `eBlendModeFunction`, `fPercent`.** The class also declares `bLooping`/`bDelayedActivate`/`fTime`/`events`, and they appear in hkxcmd's own field-name table in the exe — but vanilla marks them **`SERIALIZE_IGNORED`** and emitting them breaks the compile. *The exe's class definition lists fields that must not be written; only the decompiled vanilla file distinguishes them.*
- **`hkbBehaviorGraphData` needs `wordMinVariableValues` + `wordMaxVariableValues`** between `eventInfos` and `variableInitialValues`.
- **`eventToSendWhenStateOrTransitionChanges` is a nested `hkobject` (`{id:-1, payload:null}`), not `null`**; likewise `triggerInterval`/`initiateInterval` are nested `hkbStateMachineTimeInterval` structs (all `-1`/`0.0`), not tuple literals — `param_structs` renders values inline and cannot express either, so they are built with `param_raw`.
- **`hkbBlendingTransitionEffect.flags` is the integer `0`**, and `selfTransitionMode` is the full `SELF_TRANSITION_MODE_CONTINUE_IF_CYCLIC_BLEND_IF_ACYCLIC`.
- Class signatures for all of these are registered in `hkx_xml.SIGNATURES`, read off the vanilla file.
- Sequences that `_process_controller_manager` stripped to zero controlled blocks are **excluded** — a state for a dead sequence makes `PlayAnimation()` succeed while animating nothing, reintroducing the original silent failure.
- **The skeleton needs exactly ONE `referencePose` entry per bone, emitted ONCE.** `HkxPackfile` happily writes a duplicate `hkparam` and hkxcmd keeps the **FIRST**, so an empty `referencePose` emitted before the real one yields a skeleton with 1 bone and 0 poses; binding a sequence then indexes past the end and null-derefs (this was the second half of the prisonCellGate01 CTD).
- **hkxcmd compiles the identity pose into a ZERO QUATERNION — patch the bytes (`_fix_identity_quat`).** The XML text `(0 0 0)(0 0 0 1)(1 1 1)` is exactly what every shipped creature skeleton uses, but for this file hkxcmd writes the rotation slot as all zeros. A zero quaternion is not a rotation, so the single bone the graph drives has no valid bind pose and **the entire object renders nothing** — while the graph loads without error and no Papyrus message appears. Havok's **binary** quaternion is **w-first** `(1,0,0,0)`, unlike the XML's xyzw, so the fix rewrites the 48-byte pose block (trans/quat/scale hkVector4 slots) in the compiled WIN32 file, before the AMD64 step. Verified **byte-identical to vanilla `clutter\beehive\characterassets\SingleBoneSkeleton.hkx`** (1104 bytes, 0 diffs) — that file is the reference for any single-bone animated object.
- **`hkbCharacterData`'s field list is not what the name suggests** — copy `clutter\beehive\characters\Character00.hkx`: `characterControllerInfo, modelUpMS, modelForwardMS, modelRightMS, characterPropertyInfos, numBonesPerLod, characterPropertyValues (this is where the hkbVariableValueSet hangs), footIkDriverInfo (null POINTER, not an array), handIkDriverInfo (null), stringData, mirroredSkeletonInfo, scale`. There is **no `variableInitialValues` and no `aiControlDriverInfo`**. Getting it wrong made hkxcmd silently drop the `hkbVariableValueSet` — detectable by diffing the packfile's `__classnames__` string table against vanilla's, which is a fast sanity check for any generated hkx.
- Vanilla lays these files out in the mesh's OWN folder (`clutter\beehive\{behaviors,characters,characterassets}\`), not a `<stem>_behavior\` subfolder; ours nests them so two animated NIFs in one directory cannot collide on `Character01.hkx`. Both work — the paths inside the character file resolve relative to the project file's folder. Our project hkx is byte-identical to vanilla's (880 bytes).
- Final step is `convert_hkx_to_amd64` on every file: SSE loads only 64-bit packfiles (verified pointer-size byte 8 on all 161×4 outputs).

## NIF particle system conversion
- **🛑 `NiFlipController` INSIDE A SEQUENCE was the actual `oblivionarchgate01` red triangle (fixed 2026-08-09, THIRD red-triangle cause)**: the property-side flip handler only sees a `NiFlipController` hanging off a geometry's `NiTexturingProperty`; one referenced from a **`NiControllerSequence` controlled block** never reaches it, so the block stayed in the file and kept **121 `NiSourceTexture` frames** alive with it. A sequence stores its controller type as a **string the engine instantiates BY NAME**, so an Oblivion-only type there rejects the whole NIF. Vanilla census (~8,300 meshes): `NiFlipController` and `NiSourceTexture` appear **ZERO** times. Affected exactly 9 of 11,693 output meshes — all four Oblivion gates, three magiceffects, `creatures/endgame/battle`, `health_bar01`. **DROP the entry, don't retarget it**: the flip-book is already fully converted geometry-side into a `*_flip.dds` frame-strip atlas driven by a `BSEffectShaderPropertyFloatController` stepping U Offset (verified: all 5 flip nodes keep their atlas + 16/75/30 stepped keys), so the sequence entry is a pure duplicate. Also added `_VANILLA_SEQ_CONTROLLERS`, a whitelist backstop dropping **any** controller type vanilla never puts in a sequence — every other handler there is type-by-type, so the next Oblivion-only controller would have shipped broken the same way. **Diagnosis method that actually worked**: pyffi parses these files happily (it is far more tolerant than the engine) — use the header/block-size verifier (`_verify_block_structure` in `tests/test_asset_convert.py`) or `tools/nif_block_scan.py --has NiSourceTexture`, which showed 121 structural errors where pyffi showed none. **Caveat when sweeping with that verifier: the 112-byte `BSLightingShaderProperty` variant is VANILLA-LEGAL** (1,876 occurrences in `references/Skyrim Meshes`) and is a gap in the verifier, not a defect — exclude it or you get 340 false positives.
- **A lighting shader over UV-LESS geometry (fixed 2026-08-09, same session — real defect, but NOT the red-triangle cause)**: this was fixed first on the theory that it caused the red triangle; it did not (the user re-tested and the triangle remained — the mesh was failing to LOAD, which is the `NiFlipController` bug above, and a malformed shader would garble a shape rather than replace it with the placeholder). Keep the fix — the state is still vanilla-impossible — but do not credit it with the red triangle. 12 shapes shipped a `BSLightingShaderProperty` over geometry with `num_uv_sets == 0` and no tangents. That shader *always* samples a diffuse texcoord and reads the tangent basis for its normal map, so with neither stream present it reads past the vertex buffer and renders as an untextured red shard. These are Oblivion **helper volumes** (particle emitter sources, spawn/effect proxies) that Oblivion hides with **bit 0 of the node flags**, but `_process_geometry` did `ts.flags = NIF_FLAGS`, clobbering the authored hidden bit and un-hiding every one of them. Two earlier narrow workarounds existed for this same clobber — the `EditorMarker` name-prefix strip and the `NiPSysMeshEmitter.emitter_meshes` hiding pass — and **neither reaches a helper the emitter does not link**, which is why these survived. Three fixes: (1) preserve the authored hidden bit in `_process_geometry`; (2) `_apply_rest_visibility` only read *keyframed* interpolators and so skipped every **data-less `NiBoolInterpolator`**, whose constant `bool_value` IS the rest state — this mesh drives all 30+ vis-controlled nodes that way, so the meteors/tendrils rendered from cell load; (3) a final safety net that strips the shader and hides any shape still left lit-but-UV-less. **Vanilla census (373 shapes, `references/Skyrim Meshes`): ZERO pair a lighting shader with 0 UV sets** — the 54 UV-less vanilla shapes are either `BSEffectShaderProperty` (45; that shader needs no tangents) or carry no shader at all (9, 8 of them hidden). Detect with a converted-output scan for `num_uv_sets == 0` + `BSLightingShaderProperty`; 0 after the fix. The same fix also cleared `obliviongate_simple` (10), `obliviongate_forming` (9) and `oblivionwargateani02` (6) untargeted, so treat any lit UV-less shape as this bug rather than patching the mesh. **Lesson for the next session: "renders as a red triangle" means the engine REJECTED THE FILE AT LOAD — go straight to block-level structural validation, not to shader/geometry inspection.** A shader or vertex-stream defect garbles a shape; only a load failure substitutes the placeholder.
- **`NiPSysMeshEmitter.emitter_meshes` is a SECOND link to geometry and must be remapped when NiTriStrips→NiTriShape (fixed 2026-07-27, the RED-TRIANGLE bug)**: `se11sheopooffx.nif`, `se01waitingroomwalls.nif` and `palacefont01.nif` rendered as Skyrim's red missing-mesh placeholder — the engine failed the whole NIF at load. `_walk_node` converts strips by writing the replacement back into the **parent's `children` array**, but a mesh emitter references its source geometry through `emitter_meshes`, which is not a children array and was never rewritten. The orphaned `NiTriStrips` stayed reachable through that link, so PyFFI happily re-serialized it, leaving raw Oblivion strips in a Skyrim file. **Skyrim has no NiTriStrips renderer** (vanilla census: **107/107** emitter meshes across all 256 `NiPSysMeshEmitter` blocks are `NiTriShape`; the 21 stray NiTriStrips in the whole vanilla tree are all `bhkNiTriStripsShape` collision), so the load fails outright. Fix: extend the existing `_block_map` fixup (which already repaired `NiDefaultAVObjectPalette`) to walk every `NiPSysMeshEmitter` and remap each `emitter_meshes[i]`. Detect it with a converted-output scan for surviving `NiTriStrips`, or for emitter meshes whose class is not NiTriShape — both are 0 after the fix. **The particle CONVERSION itself was never at fault here** — fire and other psys meshes were always fine; this is purely a dangling-reference bug in the strips rewrite, so look for the second link rather than re-auditing the modifier chain.
- **BSXFlags bit 0 (Animated) is REQUIRED or particles NEVER TICK — THE final fire-invisibility root cause (fixed 2026-07-05)**: without BSX bit 0x01 on the root, the engine never updates the mesh's time controllers, so emitters never fire — the file is perfectly valid but the fire is invisible. Census: **399/400 vanilla particle meshes set bit 0** (sole exception: a trailer camera rig); collisionless particle meshes use plain BSX=1 (also 0x201/0x221 with external-emit/editor bits). Two converter gaps caused this: `_add_bsx_flags` (a) early-returned when the root had NO collision (fireopensmall loses its collision → no BSXFlags at all), and (b) detected "animated" only via NiControllerManager on the ROOT — particle controllers live on the NiParticleSystem, so even collision-bearing fire got 0x82 static. Fix: `_tree_is_animated()` (any NiParticleSystem, or any block with a controller, anywhere in the tree) → collisionless+animated gets BSX=1; collision values get bit 0 OR'd in (0x82→0x83, 0xC2→0xC3 — both appear in vanilla census); `_convert_flame_nodes` now CREATES a BSXFlags(=0x10) when the root has none (fake candles without collision previously lost the AddonNode bit). All the fixes below were necessary too, but this was the last blocker: the earlier gravity_object fix repaired the SIM, this makes the engine RUN it. (fixed 2026-07-05, `_skyrimize_modifiers`)**: the SSE particle engine does NOT drive Oblivion-era `NiPSysGrowFadeModifier` (scale) or `NiPSysColorModifier` (color) even though they're valid block types — particles spawn at scale 0 / alpha 0 = invisible. Convert them to the BS* equivalents the engine actually processes, matching a working vanilla fire (`references\Skyrim Meshes\...\slighthousefire.nif` Fireball): **NiPSysGrowFadeModifier → BSPSysScaleModifier** (60-entry scale ramp, grow-in/hold/fade-out, peak ~1.0 taper to 0.1); **NiPSysColorModifier → BSPSysSimpleColorModifier** (fade_in/out % + 3 Color4s). **Inject BSPSysLODModifier** — it's in 498/498 vanilla particle meshes (LOD begin/end/emit-scale/size = 0.033/0.233/0.2/1.0); without it the system culls at all distances. Keep emitter/spawn/rotation/gravity/position/bound-update/age-death as-is. Set NiPSysModifier `order` to vanilla bands: AgeDeath=0, LOD=1, Emitter/Spawn=1000, SimpleColor/Rotation/SubTex/Scale=3000, Gravity=4000, Position=6000, BoundUpdate=7000 (engine processes in ascending order). Set Name/Target(=the NiParticleSystem)/Active on every modifier.
- **Particle shader** (BSEffectShaderProperty): flags1 = `z_buffer_test` + `soft_effect` (see the FX brightness/soft-fade entry below — the earlier "NOT soft_effect" note was drawn from fire meshes only and did not hold for the blended FX population), flags2 = `vertex_colors` ONLY, `emissive_multiple`=**1.0** with emissive_color taken from the source `NiMaterialProperty` (was a blanket 1.5, which over-brightened every non-fire system), texture_clamp_mode=**0xFF03** (u32 packs clamp 3 in the low byte + Lighting Influence 0xFF in byte 1 — every vanilla fire uses 65283, not 3). Always attach a NiAlphaProperty flags=0x100d (additive SRC_ALPHA/ONE) — vanilla particles always have one (campfire01burning uses 0x10ed/threshold 128 standard blending; source alpha is passed through when present).
- **NiBillboardNode root scrambles particle emission → invisible (fixed 2026-07-05; quad re-billboarding added same day)**: Oblivion fire/effect NIFs have a `NiBillboardNode` ROOT (to face the 2D fire quads at the camera) with the particle-system emitters nested UNDER it. A NiBillboardNode re-orients its entire subtree to face the camera every frame; a world-space emitter under it emits into a spinning frame → particles fly off-screen / the system renders nowhere. Vanilla Skyrim keeps particle emitters under a PLAIN NiNode (`slighthousefire.nif`: BSFadeNode→NiNode "Fireball-Emitter"→NiParticleSystem). Fix in nif_converter Pass (root handling): if a NiBillboardNode root's subtree contains any NiParticleSystem, DEMOTE the root to a plain NiNode (copy name/transform/children/extradata/controller) — **but wrap each direct GEOMETRY child (the flat fire quads) in a fresh child NiBillboardNode carrying the source root's billboard_mode** (vanilla campfire01burning pattern: BSFadeNode → NiBillboardNode "Plane05" → NiTriShape). A plain demote leaves the quads fixed-facing = edge-on/backfacing from most in-game angles = fires look invisible (while NifSkope's default camera happens to face them). Emitter/marker child nodes stay unwrapped. Non-particle billboard roots keep the whole-root wrap. **BILLBOARD AXIS CONVENTION (the final fire-quad invisibility fix, 2026-07-05)**: Oblivion mode-1 (ROTATE_ABOUT_UP) keeps local **+Y up / +Z at camera**; Skyrim mode-1 keeps local **+Z up / ±Y at camera**. Fire quads are authored flat in local XY (height along +Y) with IDENTITY transforms — correct under Oblivion's convention, but under Skyrim's an identity-rotation billboard leaves the quad LYING FLAT spinning about Z (edge-on from every standing viewpoint = invisible). The wrapper NiBillboardNode must carry vanilla's **−90°-about-X** static rotation `[[1,0,0],[0,0,1],[0,−1,0]]` (maps local Y→world Z; byte-identical to vanilla campfire01burning "Plane05"). Diagnosed by comparing vanilla billboard-node rotations (non-identity!) vs quad vert planes (both games author quads flat in XY).
- **EditorMarker geometry must be STRIPPED** (`_walk_node`): Oblivion hides its editor-marker meshes (the pyramid in fire NIFs) via the node hidden flag, which our conversion clobbers with NIF_FLAGS (visible) — the marker then renders in game as an untextured BLACK PYRAMID (this was the mysterious "black pyramid" at placeatme'd fires; at world-placed fires it sat underground). Vanilla Skyrim ships no editor-marker geometry in these objects.
- **NiAlphaProperty must NOT be shared between particle systems**: Oblivion sources share one alpha block across several PS; vanilla Skyrim always pairs each PS with its own shader+alpha. `_convert_particle_system` clones the source alpha per PS.
- **NifSkope's "animate" option is NOT a valid diagnostic for Skyrim particle chains**: NifSkope 2.0.dev7 only registers the OLD `NiParticleSystemController`/`NiBSPArrayController` for particles (glparticles.cpp) and `BSEffectShaderPropertyFloat/ColorController` for effect shaders — it completely ignores `NiPSysEmitterCtlr`/`NiPSysUpdateCtlr`. A perfectly-authored Skyrim PSys NIF shows "No Animations in this NIF"; vanilla campfire only gets an animate option from its shader controllers on the glow quads.
- **UV SCALE (0,0) = INVISIBLE — THE fire-invisibility ENDGAME bug (fixed 2026-07-05)**: PyFFI's fresh `BSEffectShaderProperty` defaults `uv_scale` to **(0,0)** (vanilla: offset (0,0), scale **(1,1)**). Scale 0 collapses EVERY UV to the texture's top-left texel — transparent on flame textures — so all effect-shader geometry (particles AND quads) rendered fully transparent while being structurally perfect: sim ran (crash proved it), every block census-clean, texture valid. Diagnosed via A/B matrix: vanilla-structure+our-texture visible, our-structure+vanilla-texture invisible → field-by-field shader diff caught the one field never printed. ALWAYS set uv_offset(0,0)+uv_scale(1,1) on any PyFFI-created shader property; regression test asserts non-zero scale on every effect shader.
- **NiFlipController is dead in Skyrim (0/17,216 vanilla) — converted to atlas + float controller (2026-07-05, `asset_convert/flipbook.py`)**: Oblivion animates fire quads by flipping the diffuse per frame. Conversion: decode the N frame DDSes (DXT1/3/5 → BGRA), compose a horizontal strip atlas padded to POT frame count (uncompressed BGRA32 DDS, written into the output textures tree beside `\meshes\`), set `uv_scale.u = 1/N_pad`, and drive `BSEffectShaderPropertyFloatController` (flags 0x48, var **6 = U Offset**, `NiFloatData` keys mode **5 = CONST** at k·delta → k/N_pad; delta from `NiFlipController.delta`, fallback cycle/N or 1/15s). Planned in `_process_geometry` (validates source frames via `_resolve_source_texture` — maps the rewritten tes4 path back to the export textures tree), built in `convert_nif` (knows dst tree). NifSkope animates it too (its EffectFloatController is supported — NifSkope "no animate option" on PSys-only NIFs is normal, but flip-book quads DO animate there now). Fallback on unresolvable frames: static first frame.
- **NiTextureTransformController → BS*ShaderPropertyFloatController (2026-07-21, `_collect_tex_transform_ctrls`/`_attach_tex_transform_ctrls`)**: Oblivion scrolls/scales UVs (waterfalls, lava, Oblivion gates, sunbeams — 61 Nehrim meshes, 127 controllers) with a `NiTextureTransformController` hosted on `NiTexturingProperty`. Conversion DELETES `NiTexturingProperty`, so the animation was silently lost and e.g. `landscapewaterfall02.nif` rendered as a frozen texture. Skyrim's equivalent is a shader float controller on the UV offset/scale, chained via `next_controller` — vanilla `fxwaterfallbodytall.nif` drives **V Offset with the same 2-key ramp**, and `fxwaterfallthin512x128.nif` chains U Scale + V Offset + U Offset on one shader. Harvest BEFORE properties are cleared in both `_process_geometry` and `_convert_particle_system`, then re-attach to whichever shader was built (Lighting *or* Effect), preserving any flip-book controller already on it at the tail of the chain. The `NiFloatData` is reused as-is — both engines read the curve as a UV offset/scale over time. Mapping (`TransformMember` → Lighting/Effect enum): TRANSLATE_U 0→**20**/**6**, TRANSLATE_V 1→**22**/**8**, SCALE_U 3→**21**/**7**, SCALE_V 4→**23**/**9**. Flags: OR in **0x48** and keep the source cycle bits (0x06) — Oblivion's 0x08 lacks Compute-Scaled-Time so the curve never advances. **Dropped, not faked**: `TT_ROTATE` (2) — neither Skyrim shader exposes a UV rotation float, so 50/127 Nehrim controllers have no equivalent; `NiBlendFloatInterpolator` (46/127, all skull/fireball meshes) — driven by a `NiControllerManager` sequence, no inline keys to translate; and single-key curves (constants, not animation).
- **`BS*ShaderProperty*Controller.Target` must name its shader block — a NULL target is a CTD on cell load (2026-08-01, `_bind_shader_ctrl_target` / `_drop_unbound_shader_controllers`)**: `NiTimeController.Target` is a non-optional back-pointer for this controller family. Census: **15/15 vanilla `BS*ShaderProperty{Color,Float}Controller`s name their own shader block** (Lighting controllers → `BSLightingShaderProperty`, Effect → `BSEffectShaderProperty`); **0 nulls in 150 meshes sampled**. The Oblivion sources we rebuild these from (`NiMaterialColorController`, `NiAlphaController`, `NiTextureTransformController`) target the NiTriShape's *property list*, which has no Skyrim counterpart, so the rebuilt controller was written with `target = None`. Skyrim dereferences it while loading the shader property → `EXCEPTION_ACCESS_VIOLATION`. Crash signature: the faulting frame is the engine's own `BSLightingShaderProperty::LoadBinary` (reached via `LooseFileStream` → `BSResourceNiBinaryStream` → `NiStream`); with Community Shaders installed the return address lands in its `TruePBR.cpp` `BSLightingShaderProperty_LoadBinary` hook, which is a **red herring** — the hook simply calls the original, and the fault is inside it. Fix binds the target in `_match_seq_shader_types` (which already resolves each node's real shader for the Lighting-vs-Effect re-stamp) and drops any entry that still cannot be bound.
- **`"<node>:<index>"` in a sequence string palette means GEOMETRY, not a missing node (2026-08-01, `_retarget_geometry_suffix_entries`)** — the reason a shader-controller target can look unbindable. Oblivion's exporter names a node's geometry children two different ways, and `morroblivionchandilier01.nif`'s `Idle` sequence uses **both at once**:
  ```
  node='CandleSkinny01:0'          NiMaterialColorController   (emissive flicker)
  node='CandleSkinny01'            NiTransformController
  node='CandleSkinny01 NonAccum'   NiTransformController
  ```
  The last two name real `NiNode`s, so **the palette is not stale** — only the `:0` form needs translating. It means "geometry child 0 of `CandleSkinny01`", which after conversion is the shape carrying the `BSLightingShaderProperty` (block-named `Tri Tri Light_Com_Chandelier_01 2 0` under the other convention, `Tri <parent> <index>`). Resolve it by walking the named node's subtree, collecting shader-bearing geometry in tree order, and taking the Nth; then rewrite the entry's `node_name` to that real block name so the engine can re-bind at run time. The source `node_name` bytes are empty — the name lives only in the palette — so none of this is visible unless you resolve the offsets.
  **Do NOT "fix" this by deleting the entry.** That was tried first and is wrong twice over: it silently drops the chandelier's emissive flicker (a faithful conversion must keep it — the curve is 5 keys, 0→3s, and survives byte-identical), and emptying the sequence strands its `NiControllerManager` with **0 sequences**, which the engine dereferences exactly the same way — crash log named `RCX/RDI = NiControllerManager*`, `RAX = 0`, on `BSFadeNode "CandleSkinny01"`. Vanilla census: **0/8 managers have 0 sequences; 0/17 sequences are empty.** This was the Seyda Neen Census & Excise Office CTD — the chandelier is placed **7×** in that one room. *(Pre-existing and NOT part of this fix: 24 empty `Forward`/`Backward` sequences on `morroblivion\flora\*anim.nif` — separate issue, no manager involved.)*
- **Skyrim reads ONE UV set — a second one overruns the engine's vertex buffer (2026-08-01, `_clamp_uv_sets`)**: this was the **Seyda Neen Census & Excise Office CTD**. On disk the u16 **`BS Data Flags`** is a bitfield (`references/nif [version].xml` → `BSGeometryDataFlags`): **low 6 bits = UV-set COUNT** (mask 0x003F), bits 6–11 = Havok Material, **bit 12 (0x1000) = Has Tangents**. PyFFI splits that one field into `num_uv_sets` + `extra_vectors_flags`, which is why `extra_vectors_flags = 16` writes bit 12 — the converter's comment calling it an enum ("0=none, 16=has binormal+tangent") is wrong, it is a bitfield. The count is the **only** thing telling the engine how many `TexCoord` arrays follow the vertex colours, so a mesh that stores **2** sets while `BSLightingShaderProperty` binds 1 leaves the vertex buffer a whole array short: the copy runs past the end of the allocation and faults on a **non-temporal store** — `vmovntdq [rcx+N], ymm` where `rcx` is 32-byte aligned and `rcx+N` is exactly the first byte past a 64 KB page. That alignment signature (`memcpy` ≥4 KB, destination landing precisely on the page boundary) is the tell for a short destination buffer, **not** a bad pointer. Oblivion authors the extra set for detail/overlay passes Skyrim has no slot for; set 0 is the diffuse UVs every shader samples, so the surplus is dropped. Census: **2,233 vanilla shapes carry 0 or 1 UV sets, NEVER 2**; we shipped 2 on 5 meshes, including `morro\f\furnucomutableu05.nif` — the file the crash log named in its `inputFilePath`. Also note `bhkCompressedMeshShapeData` blocks legitimately dwarf these (500 KB+), so "big block" alone is not a signal.
- **A block type with no RTTI in SkyrimSE.exe is a hard CTD — audit with `tools/nif_block_type_audit.py` (2026-08-01)**: `NiStream` constructs each block by looking its type NAME up in a factory registry. If the engine has no such class the slot is never built, and a link to it hands `NiPointer::operator=` a non-NiObject pointer; the engine runs `lock cmpxchg [ptr-0x10]` on the "refcount", which lands in **read-only `.rdata`** → `EXCEPTION_ACCESS_VIOLATION` while loading the mesh. No Papyrus trace, and **invisible to PyFFI**, which reads and writes the dead block happily. Diagnosis route (all three tools were essential): `tools/address_lib.py --log <crash>` to translate the Steam-build stack into GOG RVAs, `tools/skyrim_disasm.py --disasm` to read the faulting function, and `--find <ClassName>` to check RTTI. **`NiUVController` was the only such type** across 3000 converted meshes — searching RTTI for `NiUV` returns *only* `NiUVData`. It hit 8 Ghostfence meshes (`morro\x\exuggufence*`, `morroblivion\architecture\ghostgate\fence01*`). Note the whole Oblivion controller family is likewise absent from the exe (`NiFlipController`, `NiMaterialColorController`, `NiTextureTransformController`, `NiAlphaController` — all already converted elsewhere); `NiUVController` was simply missed. Run the audit after any converter change that can emit a new block type.
- **`NiUVController` → `BS*ShaderPropertyFloatController` (2026-08-01, `_collect_uv_ctrls`)**: it is Oblivion's UV-scroll animation carried on the **geometry** controller chain rather than on `NiTexturingProperty`. `NiUVData.uv_groups` is a fixed 4-entry array — **[U offset, V offset, U scale, V scale]** — holding the same curves a `NiTextureTransformController` would, so each populated group (≥2 keys; a single key is a constant) becomes one shader float controller through the existing `_attach_tex_transform_ctrls` path and `_TEX_TRANSFORM_VARS` mapping. Harvest must run **before** `_strip_dead_geometry_controllers`, which now also unlinks `NiUVController`. Ghostfence emits 6 controllers per mesh (U Offset 20 + V Offset 22 × 3 shapes). Shapes used purely as `NiPSysMeshEmitter` sources (`fence01.nif`'s `ForceField2`) legitimately end up with no shader and therefore no controller — that is correct, not a regression.
- **Emitter controller flags** (`NiPSysEmitterCtlr`/`NiPSysUpdateCtlr`/`NiPSysModifierActiveCtlr`): Oblivion ships flags=0x08 (Active only); **OR in 0x48** (Active | Compute-Scaled-Time, bit 0x40 default-true in Skyrim) — do NOT overwrite, because Oblivion's NiPSysUpdateCtlr carries CLAMP cycle bits (0x0c) that vanilla keeps (campfire01burning UpdateCtlr = 0x4c, EmitterCtlr = 0x48). Without Compute-Scaled-Time the birth-rate interpolator can evaluate to 0 (no particles).
- **Dangling gravity_object → broken particle sim → invisible (fixed 2026-07-05; necessary but NOT sufficient — the BSX Animated bit above was the final blocker)**: `collision.py::remove_empty_collision_nodes` deletes EVERY bare empty NiNode child of the root (0 children, no collision). Oblivion fire NIFs have empty marker nodes named `Gravity`/`SparkGravity` that the `NiPSysGravityModifier.gravity_object` points at — deleting them dangles the reference (PyFFI writes "NiNode block is missing from the nif tree: omitting reference"), and the engine's particle physics then fails → particles never render. Vanilla campfire01burning.nif KEEPS its `Gravity` node (block [2], referenced by the gravity modifier). Fix: `remove_empty_collision_nodes` now protects nodes whose id() is in `_collect_psys_referenced_nodes(root)` (gravity_object + every *Emitter.emitter_object). Detect the symptom: convert with pyffi logging at WARNING and grep for "missing from the nif tree", or check `id(gravity_object) in tree` after conversion.
- **NiParticleSystem block size sanity**: at BSStream 83 an empty-modifier-list particle system is ~142 bytes, +8 per extra modifier band; vanilla fire particle systems are 150 (10 modifiers). Compare header block_size across many vanilla meshes — a size that's LOWER than the vanilla floor for the same modifier count means a dropped field/ref. The 4 Far/Near Begin/End ushorts (PyFFI `unknown_short_2`/`unknown_short_3`/`unknown_int_1`, only when user_version≥12) are all 0 in vanilla fire — not a culprit.
- Diagnosing invisibility: read a WORKING vanilla particle mesh and diff the modifier chain (needs `NiPSysData.read` from pyffi_monkey_patch Patch 4 — stock PyFFI can't read Skyrim NiPSysData). The reference NIFConverter (`references/NIFConverter/copyover_legacy_nif_animations.py:915`) just DELETES NiParticleSystem (`replace_global_node(node, None)`) — do NOT copy that; convert to the visible BS* vocabulary instead.
- NiPSysGrowFadeModifier base_scale patch (Patch 2) still needed for any GrowFade that survives; makes the block 29 bytes = correct Skyrim size (NiPSysModifier parent 13 + own 16).
- NiPSysData: preserve original max particle count (`max(num_vertices, 75)` → bs_max_vertices). num_vertices and bs_max_vertices ALIAS the same PyFFI field slot.
- **CRITICAL — PyFFI 2.2.3 NiPSysData layout is STRUCTURALLY WRONG for Skyrim; hand-rolled in `pyffi_monkey_patch.py` Patch 4 (fixed 2026-07-05, the AnvilCastleGreatHall CTD)**: PyFFI's NiPSysData attribute list is the wrong (older Bethesda) field arrangement — it is MISSING Material CRC (4), Consistency Flags (2), Additional Data ref (4), Has Texture Indices (1), Aspect Flags (2), and invents spurious unknown_byte_1/unknown_link/unknown_short_3/unknown_byte_4. Net: an empty block writes 66 bytes where real Skyrim is **70**, and the FIELD ORDER is wrong regardless of size, so the SSE engine (which trusts the header block_size to seek to the next block) misaligns EVERY following block → it builds a BSEffectShaderMaterial from garbage → `vmovntdq [rcx+0xA0/0xC0], ymm` non-temporal store past a page end → CTD (crash logs named `BSEffectShaderProperty "DamageSphere"/"CandleFat02Fake"`). The correct 70-byte #BS202# layout (from `references/nif 0.10.0.0.xml`, verified == 70 on a census of 27 vanilla empty NiPSysData blocks) is emitted by overriding `NiPSysData.get_size`/`write` to pack the bytes directly: GroupID(i) BSMaxVertices(H) KeepFlags(B) CompressFlags(B) HasVertices(B) BSDataFlags(H) MaterialCRC(I) HasNormals(B) BoundCenter(3f) BoundRadius(f) HasVColors(B) ConsistencyFlags(H) AdditionalData(i) HasRadii(B) NumActive(H) HasSizes(B) HasRotations(B) HasRotAngles(B) HasRotAxes(B) HasTexIndices(B) NumSubtexOffsets(I) AspectRatio(f) AspectFlags(H) SpeedToAspect×3(f) HasRotSpeeds(B). **Field values (raw-byte census of ALL 837 NiPSysData blocks in 400 vanilla particle meshes, 2026-07-05 — supersedes the earlier 27-block census which was read through PyFFI's MISALIGNED layout and got the flags wrong)**: HasVertices=1, BSDataFlags=0, MaterialCRC=0, HasNormals=0, **HasVColors=1** (810/837), Consistency=0, **AdditionalData=-1** (837/837 — NULL ref; writing 0 references BLOCK 0 = the root!), **HasRadii=1** (837/837), NumActive=0, HasSizes=1, HasRots=0, HasRotAngles=1|0, HasRotAxes=0, **HasTexIndices=0 whenever NumSubtexOffsets=0** — the engine does `rand % NumSubtexOffsets` for atlas frame selection when the flag is set, so flag=1+count=0 = **EXCEPTION_INT_DIVIDE_BY_ZERO in the emitter update** (`div [rsp+...]`, crash names NiPSysCylinderEmitter+NiPSysData+NiPSysEmitterCtlr; 0/837 vanilla blocks pair flag=1 with count=0; atlas blocks have count 1..128 and block size 70+16×count — all 837 satisfy that size equation, fully validating the layout). AspectRatio=1.0 for non-atlas (0.0 on atlas blocks), AspectFlags=0, s2a floats=0, HasRotSpeeds=0. This crash only SURFACED once the BSX Animated bit made emitters actually run. `read` is NOT overridden for Oblivion sources — the converter only reads Oblivion-version sources (PyFFI's Oblivion layout is separately correct); our Skyrim output is never re-read by the pipeline. **PyFFI can no longer parse our Skyrim particle output — verify via the HEADER block_size table (inspect-only), NOT a PyFFI struct re-read.** Sweep: `NiPSysData` block_size must be 70 for empty pools.
- **Diagnostic method for "which field is wrong" (data-driven, per user directive — never compare against a single mesh)**: census MANY vanilla meshes (`references\Skyrim Meshes`, ~400 particle NIFs) reading only the header block_size table + field values; the value that is uniform across all vanilla but differs in ours is the bug (e.g. `has_subtexture_offset_u_vs`=True in 27/27 vanilla). When PyFFI can't even READ vanilla (`Skipping -4092 bytes`), that itself proves PyFFI's layout ≠ the real engine layout → hand-roll from nif.xml.
- The self-consistency trap: `block.get_size()` (fills header block_size) and `block.write()` can DISAGREE for a mis-conditioned PyFFI struct (get_size=66, write=70) → header says 66 but 70 bytes are written → engine seeks 4 short. A read→write round-trip inside a test masks this (re-read reconstructs arrays). Check `get_size()==len(write())` on the freshly-converted in-memory block, or the deployed file's header block_size vs vanilla census.
- **CRITICAL — `pyffi_monkey_patch.py` NiPSysData vercond precedence bug (fixed 2026-07-05)**: the added-particles shorts vercond was written as `'! version >= X && user_version >= 11'`. PyFFI's Expression parser binds `!` to `version` FIRST → `((!version) >= X) && ...` = ALWAYS FALSE → the two shorts were dropped from OBLIVION reads too, misaligning every source NIF containing NiPSysData by 4 bytes → read abort. This is why the ENTIRE `fire\`, `effects\`, `magiceffects\`, `dungeons\misc\fx\`, `landscape\waterfall*` etc. list in TODO.txt §7 failed with [RD] (123 of 151 recovered by the one-line fix). MUST parenthesize: `'!((version >= 335675399) && (user_version >= 11))'`. Verify with `Expression(expr).eval(ctx)` against Oblivion (v=0x14000004,uv=11 → present=True) and Skyrim (v=0x14020007,uv=12 → present=False). The "Skipping N bytes in NiPSysData/NiPSysGrowFadeModifier" messages when a converted file is re-read by STOCK (unpatched) PyFFI are expected — stock PyFFI has the buggy layout; the game engine follows the real nif.xml (matches our output). Confirm real correctness via a patched-reader round-trip, not stock-PyFFI block-size checks.
- **Fire/effect QUAD emissive (`_process_geometry`, flip_ctrl path)**: BSEffectShaderProperty.emissive_multiple defaults to 0.0 → the flame quad renders BLACK. Fire is self-illuminated: set emissive_multiple=1.0. emissive_color is taken from the source `NiMaterialProperty`, falling back to (1,1,1) only when the source declares no emissive at all (see the next entry).
- **FX BRIGHTNESS + THE RECTANGULAR BOUNDING BOX (2026-08-07, `_apply_fx_soft_effect` + the `is_additive_fx` route)** — user report: "smoke effects such as in Vilverin are incredibly bright… way brighter than in Oblivion and difficult to see through, and many transparent effects have what appears to be a rectangular bounding box around them". Three separate defects, all in the FX shader path:
  1. **Authored emissive was discarded.** Both the quad and particle paths hardcoded `emissive_color=(1,1,1,1)`, throwing away Oblivion's own `NiMaterialProperty.emissive_color` — which is precisely how Oblivion dims an FX surface. `dungeons/misc/fx/fxmist01` ships (0.47,0.47,0.47) and `fxmistgroundeffect01` ships (0.13,0.16,0.17); both were being promoted to full white. Under **additive** blending (dst=ONE) the excess accumulates per overlapping layer, so a multi-plane mist reads as blinding and opaque instead of translucent. Now carried across verbatim; white only when the source emissive is pure black. `NiMaterialProperty.alpha` (previously dropped on the effect path entirely) goes to `emissive_color.a`.
  2. **`emissive_multiple` was a blanket 1.5 on every particle system.** That is a *fire* value, but the same code path converts smoke, mist, steam and dust. Vanilla census of 1,164 blended FX shapes: **1.0 in 852**; the brighter values are authored per-effect, never applied wholesale. Now 1.0, with the authored colour doing the dimming.
  3. **`slsf_1_soft_effect` was never set anywhere.** Without it a blended FX quad intersecting solid geometry is hard-cut along the intersection line, so the billboard shows **its own quad edge** — the reported rectangle. Vanilla census (1,198 BSEffectShaderProperty shapes across meshes/effects + meshes/dungeons): additive `0x100d` → soft_effect=1 in **417/470**, blended `0x10ed` → **224/362**, *no* NiAlphaProperty → soft_effect=0 in **322/332**. So the rule is **blended FX gets the fade, unblended does not**; `soft_falloff_depth` = **100.0** (the commonest value, 250/521 on mist/smoke/fog geometry, and what vanilla uses for ambient room fog).
- **`lighting_mode == 0` is NOT the only unlit indicator — ADDITIVE BLENDING IS THE SECOND (same fix)**: the FX/lit discriminator was `NiVertexColorProperty.lighting_mode == LIGHTING_E`, but **many Oblivion FX meshes ship no `NiVertexColorProperty` at all**, so the mode defaulted to "lit" and genuine FX geometry took `BSLightingShaderProperty` — lit, normal-mapped, no soft fade. `fxmistgroundeffect01` (the Ayleid-ruin ground mist the user saw in Vilverin) is exactly this: additively-blended AtmosphereCloud01 planes with no vertex-colour property, so **all 30 shapes** were misrouted. Across Oblivion's own FX directories **76 of 179** blended shapes declare no lighting_mode. A surface whose NiAlphaProperty sets **dst=ONE** adds its colour to the framebuffer and therefore cannot be lit geometry (lighting it double-counts the light it already contributes). Vanilla agrees without exception: of 64 additively-blended shapes sampled, **64/64 use BSEffectShaderProperty, 0 use the lighting shader**. **Plain alpha blending is deliberately excluded** — the same census shows 3 legitimate BSLightingShaderProperty cases (glass/ice), so widening the rule to all blending would misroute real lit geometry. Blast radius measured before shipping: across a 250-mesh sample of architecture/clutter/dungeons only 10 shapes newly reroute, all `textures\effects\` blood decals and FlameTower quads.

## NIF FlameNode → grafted converted flame (rewritten 2026-07-05, replaces the MPS/AddonNode substitution)
- Oblivion marks where a flame burns with an empty `FlameNode*` NiNode (a bare marker: name + transform, no children) and attaches a flame NIF there at RUNTIME (`fire\firecandleflame.nif` for candles/sconces/lamps/etc., torch flame for torches). 108 Oblivion meshes have them.
- **Conversion (`_convert_flame_nodes` + `_load_converted_flame` in nif_converter.py)**: the flame NIF for each marker's socket (see the FlameNode STAT table below) is run through the FULL converter once per worker (cached as serialized bytes; deep copies by re-reading — requires the patched-PyFFI NiPSysData `read`), and the converted root's children are grafted under each empty FlameNode marker. Marker keeps TRANSLATION, SCALE **and ROTATION** — all three are authored. The rotation is the hook-up between two model frames: the flame NIFs are +Y-up, and a +Z-up host carries the −90°X correction on its marker (`uppersilverplatecandles01`'s FlameNode0 is `[1,0,0][0,0,1][0,-1,0]`, i.e. `_BB_AXIS_FIX` itself — that host is a flat plate, extent X=23 Y=23 Z=2, and all 121 of its REFRs use RotX=0, so nothing else would stand the flame up). Zeroing it laid the candle flames on their side; +Y-up hosts author an identity marker and are unaffected. Host root gets BSX bit 0 OR'd in (grafted controllers must tick); the flame's flip-book atlas jobs are merged into the host stats so `convert_nif` writes the atlas into every output tree that needs it. Graft runs in `convert_nif` BEFORE the atlas build step.
- **The earlier "embedding crashes the engine" lesson is OBSOLETE**: that crash (`vmovntdq` past page end, `BSEffectShaderProperty "CandleFat02Fake"`) was actually the PyFFI NiPSysData 66-vs-70-byte misalignment (+ uv_scale=(0,0)) — both long fixed. The interim `BSValueNode`/`AddOnNode` MPS substitution (`_ADDN_CANDLE_FLAME`=49 / `_ADDN_TORCH_FIRE`=46 / BSX bit 0x10) is deleted per user directive: convert, don't substitute.
- **Billboard handling is now GENERAL (any tree depth, `_skyrimize_billboard`)**: firecandleflame.nif nests its particle emitter under TWO levels of NiBillboardNode, so root-only handling was insufficient. Every non-root NiBillboardNode on the walk (and root's direct children — they use a separate loop in `_convert_nif` that needs the same hook): contains a NiParticleSystem anywhere in its subtree → DEMOTE to plain NiNode + wrap its direct geometry children via `_wrap_in_billboard` (fresh NiBillboardNode, source mode, `_BB_AXIS_FIX` −90°X rotation); pure-geometry billboard → keep but COMPOSE the axis fix into its rotation (Oblivion billboards are authored identity over flat-XY quads). **When demoting, remap `emitter_object`/`gravity_object` refs that pointed at the old billboard node to the replacement** — else they dangle ("block is missing from the nif tree") and the particle sim breaks.

- **FLAME QUADS STAY IN THE MODEL FRAME — no axis fix on the wrapper (fixed 2026-08-20)**: `_wrap_in_billboard` used to compose `_BB_AXIS_FIX` (−90°X) into every wrapper it built. That is wrong for these meshes: they are authored **+Y-up and their PLACED REFERENCES carry the stand-up rotation** — censused across `Oblivion.esm`, **494 REFRs** of the `Fire\*.nif` lights use `RotX = ±90°` (10/10 for `FireTorchLargeSmoke`, 188+51 of 395 for `FireOpenSmall`). The whole model — quads AND emitter markers — shares that one frame and the REFR rotates all of it together. Pre-rotating only the quad made it the sole part in a different frame, so the REFR's −90° then laid it flat: reported in game as "a third flame component on its side" beside a correct-looking flame and smoke. `_wrap_in_billboard` now applies NO fix and only tags `bb._axis_fixed = True`, so the later `_skyrimize_billboard` pass leaves its wrappers alone (that guard still fires — measured 27 times over 81 billboard meshes — and without it the pure-geometry branch would compose the fix back in). `_compose_axis_fix` remains live for genuinely Oblivion-authored pure-geometry billboards (249 calls over the same 81 meshes). Guarded by `test_flame_keeps_the_authored_model_frame`.
- **A DEMOTED BILLBOARD INHERITS IDENTITY — except emitter markers**: a `NiBillboardNode` DISCARDS its own rotation at runtime and substitutes identity in view space (NifSkope `BillboardNode::viewTrans`, glnode.cpp: `t = parent->viewTrans() * local; t.rotation = Matrix();`). Copying that dead rotation onto the plain replacement resurrects a value the engine never used. **But a `NiPSysEmitter` reads its `emitter_object` node's orientation as the emission DIRECTION**, which is live data — `firecandleflame` authors quad and emitter in one +Y-up frame (quad identity, local extent `[1.3, 2.6, 0.0]`; emitter `[1,0,0][0,0,-1][0,1,0]`, local +Z → model +Y), and zeroing the emitter made it +Z-up while the quad stayed +Y-up: an upright flame with a second, sideways particle jet, most visible once a FlameNode marker rotated the mismatched pair into a +Z-up host. `_is_emitter_marker()` keeps the rotation for nodes referenced as `emitter_object`/`gravity_object`; every other demoted billboard still gets identity. Guarded by `test_emitter_and_quad_agree_on_up`.
- **WHICH FLAME BURNS AT A SOCKET IS AUTHORED — read the FlameNode STATs**: Oblivion ships one STAT per socket (WorldObjects/Static, EditorID `FlameNode<N>`) whose MODL is the flame to attach: `FlameNode0` `0x1E` FireCandleFlame, `1` `0x1F` FireTorchSmall, `2` `0x20` FireTorchLarge, `3` `0x21` FireTorchLargeSmoke, `4` `0x22` FireOpenSmall, `5` `0x23` FireOpenSmallSmoke, `6` `0x24` FireOpenMedium, `7` `0x25` FireOpenMediumSmoke, `8` `0x26` FireOpenLarge, `9` `0x27` FireOpenLargeSmoke. Those FormIDs are the keys `Oblivion.exe` hardcodes — the socket-name table at `0xB06818` is walked in lockstep with `0xB067C0` holding `0x1E..0x32`, looked up in the form map at `0xB0613C` — so the **plugin owns the mapping and a mod may repoint it**; `_flame_socket_map()` parses it from the export's `STAT.txt` (cached per export root). Keying on the host FILENAME instead ('torch' in the name) put the 1.3×2.6-unit candle flame on every lamp in the game: `castlelight02` is a 105-unit fixture on socket 2, i.e. FireTorchLarge (32×64). Resolution is **per marker** — `lecternworkstation1` mixes FlameNode0 candles with a FlameNode1 torch. Guarded by `test_flame_comes_from_the_flamenode_stat`.
- **A ZERO-PADDED SOCKET BURNS NOTHING**: the engine matches socket names EXACTLY, and its table holds only unpadded `FlameNode<N>` — `Oblivion.exe` contains `FlameNode7` and `FlameNode1` but neither `FlameNode07` nor `FlameNode01`, and the STATs are likewise unpadded. Two vanilla meshes are authored with padded markers and show **no flame in the original game**: `clutter/metalsmith/forgeopen01.nif` (`FlameNode07`) and `clutter/lecternworkstation1.nif` (`FlameNode01`). Matching them loosely put a 468-unit FireOpenMediumSmoke on the forge. `_FLAME_SOCKET_RE` is `^FlameNode(0|[1-9][0-9]*)(?![0-9])` and an unmatched socket grafts NOTHING — there is no default-flame fallback. Guarded by `test_zero_padded_socket_burns_nothing`.
- **FLAMES ARE NOT SOFT-FADED, AND CARRY THE FIRE EMISSIVE BOOST**: vanilla's mounted fires — our torch/sconce case — ship `soft_effect=0` with a boosted multiple (`torchsconce01` pFireballCore04 1.50, `slighthousefire` Fireball/Flames 1.50, `campfire01burning` Glow:2/Glow:3 2.50/1.60), while only the free-standing smoke and glow planes take the fade (smoke02, Glow02, Hot_Center: soft=1, mult=1.00). A soft-particle fade on a flame pinned to its sconce attenuates it against the very surface it is mounted on, so the flame reads dim and see-through. `_is_fire_fx()` classifies off the authored diffuse path (smoke/mist/fog/dust/steam/cloud win over fire/flame/torch); flames get `soft=0, mult=1.5`, smoke keeps `soft=1, mult=1.0`. Note `miscfirefly*.dds` classifies as fire and SHOULD — the source authors it emissive white with additive `0x100D`, the same signature as a flame. Guarded by `test_flames_are_bright_and_not_soft_faded`.
- **NifSkope striping on flip-book quads is COSMETIC**: NifSkope's GLSL path (`sk_effectshader.frag`) applies `uvScale`, but its fixed-function fallback maps raw UVs — the whole N-frame atlas strip shows across the quad ("texture, blank, texture"). Vanilla meshes use scale (1,1) so the fallback looks right for them; in-game the engine always applies the scale.

## NIF NiGeomMorpherController (dead in Skyrim, fixed 2026-07-05)
- **0 of 17,216 vanilla Skyrim meshes use NiGeomMorpherController** — it's Oblivion's bow flex/morph system; Skyrim bows are `*skinned.nif` and flex via skeletal animation. Strip it (and NiMaterialColorController) from geometry controller chains: `_strip_dead_geometry_controllers()` walks `geom.controller.next_controller` and unlinks them. This also lets NiTriStrips that were only kept as strips (because of the morpher) convert to NiTriShape.
- Why it mattered: PyFFI mis-serializes NiGeomMorpherController across the 20.0→20.2 bump — `interpolator_weights` is populated under the Oblivion layout but EMPTY under the Skyrim layout, so `data.write` aborts with `array size (0) different from field describing number of elements (N)`. This was the entire `weapons\*\bow.nif` [WR] failure list in TODO.txt §7.

## NIF bhkMultiSphereShape (dead in Skyrim, fixed 2026-07-05)
- **0 of 17,216 vanilla Skyrim meshes ship bhkMultiSphereShape** (deprecated Havok path). The only Oblivion source that has one is `clutter\magesguild\apparatusalembicnovice.nif`, and shipping it converted CRASHES SSE at cell load (Anvil Mages Guild) with no crash log. Vanilla expresses the same thing as ConvexTransform+Sphere children in a list shape (`clutter\kitchen\woodenladle01.nif`).
- `_expand_multisphere()` in collision.py expands it: each sphere → a `bhkSphereShape` (radius ×0.1) wrapped in a `bhkConvexTransformShape` (identity rotation, sphere center ×0.1 in the 4th column, 4th matrix row all zeros incl. m_44 — matches vanilla). 1 sphere → bare wrapper, N → bhkListShape. `_convert_shape`'s bhkListShape branch now FLATTENS a nested list produced by the expansion (a list shape has no transform of its own so flattening is safe; vanilla never nests list shapes).

## NIF embedded Ni*Light blocks (dead in Skyrim, fixed 2026-07-18)
- **0 vanilla Skyrim meshes contain any NiAmbientLight/NiDirectionalLight/NiPointLight/NiSpotLight block** (nif_block_scan). They are 3ds Max export leftovers in a handful of Oblivion assets (11 meshes: statuegodszenithar01, sanguine statue/shrine, priory doors/cabinets, vine01/02, countess clothes _gnd). SSE fails to load a static carrying one — statuegodszenithar01.nif (NiAmbientLight child of the root) rendered as the missing-model red triangle (TODO §26).
- Skyrim lighting comes from placed LIGH references, never from mesh-embedded light nodes, so there is nothing to convert them into. `_walk_node`'s NiDynamicEffect branch now strips ALL dynamic-effect subtypes (it previously kept Ambient/Point/Spot believing them valid; NiNode `effects` arrays were already cleared, but a light in the `children` array survived).

## Oblivion parallax → Skyrim height maps (`asset_convert/parallax.py`, opt-in, 2026-08-15)

`NiTexturingProperty.apply_mode == APPLY_HILIGHT2 (4)` is **Oblivion's parallax
switch**, and the height field lives in the **diffuse's ALPHA channel**. It was
long read here as a "detail-overlay blend weight"; the *action* that reading
produced (drop the NiAlphaProperty) is right either way, but the reason was
wrong and the height was being thrown away. Parallax and glow map are mutually
exclusive in Oblivion.

Skyrim's side: shader type **3** (Heightmap), `SLSF1_Parallax` in Shader Flags
1, the height map in **texture slot 3** as `<name>_p.dds`, height read from the
RED channel, **vertex colours required** (all-white is fine), incompatible with
glow map and env map, compatible with specular and shadow. Type 3 adds **no
conditional fields** to the record (`references/nif 0.10.0.0.xml`: only types
1/5/6/7/11/14/16 do), so setting it changes the enum and nothing else.

### 🔴 This must never be the default
Measured in game on a hand-built parallax shape (`temp/parallax_testbuild.py`,
one shape of `skingradbridgemain01.nif`, the other six as an in-frame control):

| Environment | Result |
|---|---|
| Vanilla SSE | the shape **swims** — visibly broken, not merely flat |
| + SSE Parallax Shader Fix | **identical**, no improvement |
| + Community Shaders | works, effect is good |

So the switch is `--parallax` (CLI), a GUI checkbox, and off everywhere else.
The converter cannot detect the player's shader setup, which is the only thing
that decides whether the output is correct. ENB also handles it (user's
report, not measured here). Do **not** rely on the SSE Parallax Shader Fix: it
did not carry the one test we ran.

### Two conditions, both required
1. **The mesh flag** answers "did the author want parallax here" — authored
   intent, never guessed at.
2. **The texture** answers "is there any height data to carry" — measured over
   Nehrim's full 12,437-mesh set with `tools/parallax_check.py census`:
   **2359 flagged shapes** in 1267 meshes, on **130 distinct diffuse
   textures**, of which only **44** actually hold a height field.

| verdict | textures | shapes |
|---|---|---|
| **height** (converted) | **38** | **1495** |
| no alpha at all (DXT1) | 67 | 508 |
| flat/empty alpha | 14 | 279 |
| **too coarsely quantised** | **6** | **56** |
| soft-edged mask (bimodal) | 1 | 11 |
| transparency cutout (binary) | 1 | 1 |
| names a file that does not exist | 3 | 9 |

### 🔴 Count the LEVELS, not just the distribution (found in game, 2026-08-15)
The first version of the classifier tested range, mid-tone share and edge
share — and accepted six **DXT3** textures that pass all three. DXT3 stores
4-bit explicit alpha: **at most 16 distinct values, whatever the artist
painted.** Measured over the 44 it first accepted, the two clusters do not
overlap at all:

```
DXT3   7-16 levels     (RockBeach04: 7 levels over a range of 102)
DXT5 147-256 levels
```

A parallax shader OFFSETS by the height, so seven levels across a range of 102
is a ~15-unit step per level — the surface renders as **visible spikes and
terracing**, which is exactly how Oblivion's beach rocks looked in game.
`_MIN_LEVELS = 64` rejects them as `quantised`. The threshold sits in the empty
gap between the clusters, so it is not fitted to the data, and the check counts
LEVELS rather than testing the FourCC — a DXT5 alpha that happens to be an
eight-step staircase is just as unusable.

The flag on those meshes is genuine (`rockbeachshell045.nif` carries HILIGHT2
in Nehrim's own BSA and has no loose override at all). What was wrong was
assuming a flag plus a plausible-looking histogram implies usable data.

**Per shape the yield is 63%, not 29%** — the textures that carry height are
the ones used everywhere (cave and fort-ruin walls, the Lazeon interior set).
Flag alone would write an empty height map and switch the shader for the other
864 — producing exactly the swimming surface above. Skipping those is the
**faithful** conversion: with no alpha channel to read, Oblivion renders no
parallax there either. Nothing is invented; the categories are counted and
printed per build. (Whoever wants more should use the TES4N2HGenerator, which
reconstructs height from the normal map properly.)

🔴 **Count textures by LOWERCASED path, not by spelling.** Oblivion meshes
spell the same file several ways — Nehrim's Lazeon walls appear as both
`Lazeon\` and `lazeon\`. Keying on the verbatim string turns these 130
textures into 163 and the 44 height maps into 74, and an earlier measurement
(`temp/parallax_yield.py`, `sorted(set(diffuses))`) reported exactly those
inflated figures. `census` prints both counts side by side so the two can be
reconciled instead of re-investigated. The remaining 74-vs-75 difference is
the one soft-edged mask: the author's own `dds_is_parallax` lacks the
`edge_ratio >= 0.70` guard that `_is_clear_parallax` beside it applies, and we
apply it.

All three unresolvable textures are defects in the source, not path bugs here:
`lazeon\static\wanda2.dds` exists nowhere in the BSAs;
`lowres\architecture\leyawiin\SkingradTrim05.dds` names the Leyawiin folder for
a texture that lives in `architecture\skingrad\`; and one mesh
(`leyawiinhouselower02_far.nif`) drops the separator entirely and writes
`textureslowres\…`. That last one is **one reference in one mesh out of
12,437** — a single-record typo, deliberately not special-cased.

Distribution is worth knowing: **63% of flagged shapes are vanilla Oblivion**
(dungeons 808, rocks 340, architecture 338), not mod content — so the gain
starts at the base conversion. Architecture is where the DXT1 no-data cases
cluster; a 6,000-mesh sample that happened to exclude it put DXT1 at 12%
instead of 37%.

### 🔴 Amplitude: cap at 150, and NEVER touch a map that is already good
The raw Oblivion/Nehrim alpha renders far too deep in Skyrim. Both engines
read the channel identically (white out, black in, mid-grey neutral) — what
differs is the depth the shader gives it: Community Shaders computes
`maxHeight = 0.1 * scale` from an engine parameter no texture can influence,
and Oblivion's figure is unmeasurable from here (compiled shader packages).

The rule must not damage a mod that ships GOOD maps. A hand-made height map
works in both engines — the user's own set is authored for Oblivion, rebuilt
from the normal maps and hand-tuned, and renders correctly in Skyrim too.
Calibrated on **56 pairs of the same texture**, hand-tuned vs Nehrim's
original (`temp/parallax_pairs.csv`):

| | min | p25 | median | p75 | max |
|---|---|---|---|---|---|
| hand-tuned (good) | 130 | 144 | **146** | 147 | **148** |
| Nehrim (raw) | 95 | 159 | **203** | 254 | **255** |

```
cap 148 -> 100% of the hand-tuned set untouched, 83% of Nehrim's corrected
cap 150 -> same, with margin        <- DEFAULT_MAX_RANGE
```

A field already inside the cap is returned **bit for bit unchanged**; only the
over-deep ones are compressed, and around their own MEDIAN so the body of the
distribution stays where the author put it. On Nehrim's 38 shipped maps: 33
compressed, 5 untouched.

### 🔴 The thing that actually mattered: how FLAT the face is (rebuilt 2026-08-16)
A hand-made height map is a flat face with narrow grooves; Nehrim's raw alpha
undulates everywhere. Stated so no outlier can distort it — the share of the
surface within ±20 levels of its own median:

| | hand-tuned | Nehrim |
|---|---|---|
| **share within ±20 of the median** | **63.2** | **36.6** |

That is a SHAPE difference. **Shifting cannot touch it** (linear, moves the
median with the body), which is why matching amplitude (150 vs 143) changed
nothing about the impression. Only a tone curve does.

#### The measure this replaced was outlier-confounded — do not go back to it
The first version used the share of texels in the bottom third of `min..max`.
That threshold comes from the EXTREMES, so a couple of bright texels stretch
the range and drag the whole surface into the "deep" band.
`leyawiinmetalstrip03.dds` — a flat plate with **two rivets** — scored
**94.2% deep** while 93.7% of it lies within ±20 of the median (p95 83, p99
jumps to 132). Recomputed on a robust p05..p95 range the separation collapses:

| | hand-tuned | Nehrim |
|---|---|---|
| deep third, full range | 12.1 | 39.1 |
| deep third, robust range | 25.8 | 37.9 |
| **share within ±20 of the median** | **63.2** | **36.6** |

The middle row is the finding: once the range is made robust, the deep-third
figure stops separating the two populations at all. `temp/dark_reference_maps.csv`
was generated with the broken figure and lists flat textures with bright specks.

#### The curve: `x**g` cannot do this job, and the reason is worth keeping
`x**g` compresses one END of the range, so the share inside a band around the
median **is not even monotone in g** — measured over all 38 maps, 21 DIP before
they rise as g falls, so a bisection has nothing to bisect on. Inside the range
its own posterisation floor allowed (g ≥ 0.63 at amplitude 255) the median
share only moved 53% → 56%. The 63% target is out of reach for that family.

The replacement works on the DISTANCE from the median:

```
y = med + sign(d) * D * (|d| / D) ** p          d = v - med
```

`D` per side, so `lo`→`lo` and `hi`→`hi` exactly and the amplitude is
untouched. `p > 1` presses the body together and steepens the tails. Two
properties `x**g` lacked:

- **Monotone in p by construction** — raising `p` moves every texel weakly
  closer to the median, so the share inside any band can only grow. That is
  what makes the bisection valid.
- **It cannot punch holes.** Steepest slope is `p`, at the ENDS, where `x**g`
  had *unbounded* slope at 0 — the thing that turned `cave04` into grey
  plateaus with black holes. The linear step then scales even that down by `f`.

The fit is O(1) per bisection step: the curve is monotone and fixes the median,
so the texels landing inside the band are those between the band edges'
pre-images, read straight out of a cumulative histogram (`_flat_share_at`).

**Ceiling on `p` is the authored floor, not a round number.** Counting the
distinct levels actually occupied inside each reference map's own ±20 band,
over all 3631: `min 21, p05 25, median 41, max 41`. A ±20 band spans 41 levels,
so the median hand-tuned map uses every one and none drops below 21 — hence
`_MIN_BODY_LEVELS = 21`. A texture that simply IS restless goes as far as that
allows and no further; a partial correction beats a destroyed map.

#### Calibration — check BOTH reference folders, again
The target is the median of the whole corpus, not of the 56 pairs:

| | p05 | median | p95 |
|---|---|---|---|
| both folders (3631) | 34.7 | **63.3** | 84.8 |
| folder A (1738) | 49.9 | 69.6 | 85.7 |
| folder B (1893) | 29.3 | 51.3 | 83.1 |

🔴 The same author normalises to 69.6% in one set and 51.3% in the other — the
**same split that made the amplitude cap dangerous** (medians 145 and 153).
The pooled median 63.3 is the natural target and sits between them;
calibrating on either folder alone lands 6–12 points off. The spread is wide on
purpose: half the corpus sits below 63%, so this says "as flat as a typical
hand-made map", not "flatter than every one". It is a CURVE TARGET only — as a
detector the same figure fails badly (see `DEFAULT_MAX_RANGE`).

#### The target saturates, which is what makes it safe to turn
`TARGET_FLAT_SHARE = 0.68`, not the pooled 63.3, on the author's in-game
verdict: *"the maps are OK, they could go a touch flatter"*. 0.68 stays inside
the hand-tuned population, in the direction of folder A's own 69.6.

Swept through the shipped module over the 38 maps, the median flat share is:

```
target   63%    66%    68%    70%    75%    80%
median  63.4   65.4   65.4   65.4   65.4   65.4
```

It stops at 65.4 and does not move again. **`_MIN_BODY_LEVELS`, not the target,
is what ends the curve** — beyond that point the fit would have to press the
face flat, and the guard refuses. So this dial cannot be over-turned, which is
the property that makes tuning it by eye safe.

Three safety properties hold by construction, not by observation:

- **bit-identical maps are decided by the amplitude detector alone** — 6 of 38
  stay bit-identical across the entire target sweep; the curve never runs on a
  map the detector let through (`f < 1.0` gates it);
- **posterisation is bounded** — steepest slope is `p·f` with `p ≤ 4` and
  `f ≤ 1`, so at most ~4 levels. Measured largest output gap is 10 and it sits
  in a map that was only *shifted*, i.e. it is authored, not introduced;
- **the body keeps ≥ 21 levels**, the reference population's own floor.

At 0.68: flat share median **40.4% → 65.4%**, poorest corrected body 34 levels.

#### ✅ Validated against a third-party pack nobody calibrated on
The detector was tuned on two folders by ONE author, which is a real weakness —
so it was then run against QTP3 (Qarl's Texture Pack 3), a well-known Oblivion
replacer with no connection to this project. `dungeons\caves`, via
`tools/parallax_check.py pack`:

| | |
|---|---|
| 38 DDS | 19 diffuse, 19 `_n` normal maps (never read) |
| of the 19 diffuse | **9** hold a usable height field, 8 have no alpha channel at all, 2 are cutout masks |
| of the 9 | **5 bit-identical**, 4 corrected |

The split is clean and lands where it should:

```
untouched   amplitude  82 .. 137      cave08, cave03, cave06, cave04, cave01
corrected   amplitude 207 .. 255      cave07, cave11, cave10, cave12
```

**Nothing sits between 137 and 207.** All four corrections fired on amplitude;
the median floor did not fire once (no QTP3 median is below 45). Largest output
gap 2 — no posterisation. On two of the four the tone curve contributed exactly
+0.0, i.e. the amplitude cap alone had already brought them inside the target.

The author's own verdict on that split: the five left alone are the ones that
"would already look good", the four changed are the ones that are "extreme to
very extreme". That is an independent confirmation of the threshold, on content
it was not fitted to.

Worth stating plainly: those four ARE authored work being changed — `cave07`
loses a third of its depth. The justification is the same premise the whole
correction rests on (Community Shaders renders the same field deeper than
Oblivion does), and `--max-range 0` turns it off for anyone who disagrees.

### 🔴 `durchgangD`: a SECOND defect, and the band measure does not cover it
`lazeon\static\durchgangD` — a practically black wall, median 17, amplitude 158
— reads as **89.2% flat** on the band measure, and correctly so: it *is* flat,
just parked entirely at the bottom of the channel. Restlessness and
off-centredness are two different defects and the curve only fixes the first.

The mechanism is why it matters: a parallax shader offsets along the view
vector by `(height − neutral)`, so a surface sitting near 0 renders not as depth
but as a **constant view-dependent UV shift** — the texture slides across the
wall as the camera moves. Same swimming artefact an empty height map produces.

**Centring was rejected once and came back only on new evidence.** The old
refutation stands for what it tested: a *tolerance around mid-grey*, measured on
56 pairs, where the medians overlap so badly that sparing 96% of the good maps
also spares 53% of Nehrim's. `MIN_MEDIAN` is a different rule — a **one-sided
floor** — and the evidence is the full 3631-map corpus:

| median level | min | p05 | median | p95 |
|---|---|---|---|---|
| hand-tuned | **52** | 94 | 125 | 175 |
| Nehrim | 17 | 31 | 105 | 169 |

```
floor < 45   touches 0 of 3631 hand-tuned (0.00%), catches 4 Nehrim maps
floor < 60   touches 3 of 3631 (0.08%)
```

Nothing the author shipped is darker than 52. The four it catches are
`durchgangD` (17), `durchgangA` (31), `decked` (32) and `bodend` (36) — exactly
the set sitting just under the amplitude threshold at 153–159 that used to ship
untouched. 45 sits in the middle of the empty gap between 36 and 52, the same
way `DEFAULT_MAX_RANGE` sits in the gap between 156 and 169.

A map caught by this rule ALONE gets the re-centring shift and nothing else —
no compression, no tone curve. A pure translation cannot damage relief: every
level, gradient and gap survives. Verified on `durchgangD`: median 17 → 113,
amplitude 158 → 158, levels 149 → 149, gap 5 → 5, and the render goes from a
black slab to a legible stone wall with the mortar joints as the deep parts.
The amplitude detector remains the only thing that may compress a field.

Consequence worth knowing: the amplitude target had been dialled down to 80 by
eye, but that was compensating for the wrong SHAPE. With the curve in place it
went back up to 140 — next to the hand-tuned set's own 143.

**Detection and correction depth are two separate numbers** — `max_range`
decides WHICH maps are touched and is pinned at 150 by the hand-tuned set
(130..148); `target_range` decides HOW FAR a condemned map is taken and is
free to go lower. That split exists because the eye and the measurements
disagree: corrected to amplitude 150, Nehrim's `wandb` still read as too
strong in game while the hand-tuned version at **143** read as right. Three
explanations were measured and all three failed —

* amplitude: 150 vs 143, five percent apart;
* steepness: ours is **0.70x** theirs in UV space (mean |dh| per texel x width
  — resolution cancels, so 1024² and 2048² are comparable), i.e. ours is the
  FLATTER one;
* a material depth parameter: shader type 3 has none (only type 7,
  ParallaxOcc, carries `Scale`), Community Shaders' Extended Materials exposes
  on/off switches and nothing numeric, and `HeightScale *= PBRParams1.y`
  belongs to the TruePBR path, not to vanilla parallax shading.

So what drives the perceived strength is still unexplained, and the depth of a
corrected map is a dial set by eye. Keeping it separate from the detector
means turning that dial can never cost a mod anything it authored well —
pinned by `test_lowering_the_target_never_reaches_a_good_map`.

**The refuted theory, so it is not retried:** re-centring on mid-grey. It is
the obvious idea — Nehrim's `wandb` sits at median 63 with 92% below mid-grey
while the hand-tuned version sits at 126 — but the same 56 pairs kill it. The
hand-tuned medians scatter 86..132 and overlap Nehrim's, so any tolerance that
spares the good maps also spares half the bad ones (tolerance 40: 96% of the
good set kept, only 53% of Nehrim's corrected). Amplitude separates cleanly;
centring does not.

Tuning does NOT need a mesh rebuild — the meshes never change, only the
`_p.dds`. Use `python tools/parallax_check.py regen [--max-range N]
[--strength F] [--only SUBSTRING]`, which rewrites the maps in seconds and
reports per texture whether the cap bit or the map was left alone.

### Output conditioning: halve → blur → curve → BC4 (added 2026-08-19)

**Skyrim's parallax sampling is coarser than Oblivion's.** Verified in game by
the author: an unsmoothed Oblivion height field reads as "comic" under Skyrim's
stepping. So *every* map is smoothed, not just the ones a detector flags.

The chain in `build_height_map` is, in order:

1. **`mitchell_halve`** — half linear size, Mitchell-Netravali (B = C = 1/3),
   resampled for an exact 2× reduction so the tap offsets are constant and the
   seven weights are a literal (`-5/288, 1/36, 77/288, 4/9, …`, summing to 1).
   **Lanczos was ruled out deliberately** — too sharp for a field that is
   already slightly soft, which is the whole point of the blur that follows.
2. **`gaussian_blur`** — radius `BLUR_RADIUS_PER_1000 = 5.0` texels per 1000
   texels of *output* width, i.e. resolution relative; σ = radius/3. A fixed
   pixel radius would hit a 512 map about eight times harder than a 4096 one
   and this content ships both. Below ~100 px output width the radius falls
   under 0.5 and the blur is skipped — small maps are left alone by design.
3. **`normalise_height`** — the tone curve, **last**.
4. **`encode_bc4_dds`**.

#### 🔴 The order is why nothing needed recalibrating

`normalise_height` is not a fixed curve, it is a **fit onto a measured property
of its input** (share of area within ±`FLAT_BAND` of the median, target
`TARGET_FLAT_SHARE`). Run it LAST, on the texels that actually ship, and it
still lands on the calibrated target whatever the halving and the blur did to
the field. Putting the blur *after* the curve would silently give back part of
the in-game-approved depth.

Halving is also a straight **speed win** on the slowest step: `encode_bc4_dds`
is pure Python, one 4×4 block at a time, inside the mesh workers — a quarter of
the pixels is a quarter of the blocks. Both new passes are numpy and accumulate
tap by tap rather than gathering, because a 4096-square map would otherwise
materialise a 234 MB intermediate in each of nine workers.

Measured on `anvilcastledoor01.dds` (4096×8192, 42 MB), `temp/bench_chain.py`:

| step | s |
|---|---|
| `decode_alpha_plane` | 8.45 |
| `mitchell_halve` | 1.78 |
| `gaussian_blur` (r = 10.2) | 0.47 |
| `normalise_height` | 0.38 |
| `encode_bc4_dds` at half | 4.60 |
| **new chain** | **15.68** |
| `encode_bc4_dds` at full — what the old chain paid | 18.00 |
| **old chain** | **26.45** |

So the conditioning is **41% cheaper per texture**, not more expensive: the
encoder saves 13.4 s and the two new passes cost 2.25 s. `decode_alpha_plane`
is now the single biggest cost and is still pure Python — the next place to
look if this ever needs to be faster.

### Diffuse → BC1: a block strip, not a recompression (added 2026-08-19)

Once the height is out in a `_p` map the diffuse has no use for its alpha, and
DXT1 is half the size. **This is not a re-encode.** Every height-carrying
diffuse is DXT5 — `classify_alpha` rejects DXT1 and uncompressed outright, and
`_MIN_LEVELS` rejects every DXT3 source — and a DXT5 block is 8 bytes of alpha
followed by 8 bytes of colour **in exactly BC1's colour-block layout**. So the
colour half is copied verbatim, keeping the endpoints the original encoder
chose.

**Dithering and perceptual error metrics therefore have nothing to act on**:
nothing is being quantised. Decoding to RGB to re-compress with dithering would
*lose* quality, not gain it.

The one real difference is DXT1's 3-colour mode. Two exact repairs, neither
changing a texel's colour (`_bc1_repair_modes`):

| source block | repair |
|---|---|
| `c0 > c1` | copy verbatim — already a legal 4-colour DXT1 block |
| `c0 < c1` | swap the endpoints, XOR the index word with `0x55555555` (0↔1, 2↔3) — the swapped palette names the same four colours |
| `c0 == c1` | zero the indices. Every palette entry already equals `c0`, and DXT1 index 3 would be **transparent black** |

Verified on real Nehrim textures: first 64 blocks decode identically, file
exactly halved (170 KB → 85 KB).

#### The gate: a shape that BLENDS with the alpha vetoes the strip

`strip_diffuse_alpha` runs after the texture copy (same reason as the
landscape-normal fix) and keys on the presence of `<name>_p.dds` beside
`<name>.dds` — the mesh stage already decided that texture carried height, so
no plumbing is needed and a non-parallax build is a no-op by construction.

But a texture-level classification is not the whole answer. If some *other*
shape reads that diffuse's alpha as opacity, that is evidence the channel is
not a height field there, whatever the classifier said. `_process_geometry`
records those diffuses in `alpha_opacity_diffuse` (a set, carried separately
from the `parallax` Counter) and the strip skips them.

Measured on the author's NTATU/Qarl parallax mod: 39,201 shapes, 134 textures
classified `height`, of which **1** — `architecture\chorrol\interior\
forgeembers01.dds` — is read as opacity by a non-parallax shape. One in forty
thousand, but the converter runs on plugins nobody has measured, so the gate is
generic rather than a bet on that number.

### The global depth scale (added 2026-08-19)

Oblivion's authored depth reads far too bumpy under Skyrim whatever the map was
calibrated for, so **every** map is compressed toward the neutral plane:

    v' = 128 + (v - 128) * DEPTH_SCALE          # 0.6, confirmed in game

**128 is not a guess.** Community Shaders pivots the height on 0.5 twice over —
`AdjustDisplacementNormalized` returns `(displacement - 0.5) * scale + 0.5 +
offset`, and the POM ray starts at `minHeight = maxHeight * 0.5`. Above 128 a
surface pushes OUT, below it pushes IN, so compressing toward 128 reduces
displacement in both directions and a groove never flips into a bump.

#### 🔴 GLOBAL, not per-map — the trap that was nearly built

The first cut normalised every map to a fixed target amplitude. That is wrong:
it makes a plaster wall exactly as deep as a cave wall and throws away the
relief the author actually authored — the same trap `normalise_height` already
warns about under `strength`. One factor for every texture keeps every
relationship between two surfaces intact and only bounds the excursion.

Prior art confirms the shape of the fix. The author's own `TES4N2HGenerator`
ends its pipeline with Output Levels (Output Black 26 / Output White 165, clamp
26..179) — the same global band operation — and the shipped NTATU/Qarl pack
measures 30..179, so `clamp_max` is visible in the data. Those values are
calibrated for Oblivion's much gentler offset mapping, which is why Skyrim needs
a further factor on top rather than a different band.

Not taken from that tool: its Contrast (150) and Balance. Shape correction is
already done by `TARGET_FLAT_SHARE`, calibrated in game; two S-curves stacked
would fight each other.

#### Why this one has no detector

Everything above the halve/blur/depth block is a CORRECTION — it decides a map
is defective and leaves everything else bit-identical, which is what protects a
mod author's own calibration. These three are a SYSTEM ADAPTATION and run
unconditionally, from any source, because the target engine samples differently:

| step | when |
|---|---|
| amplitude cap (163) | outliers only |
| median floor (45) | sunk maps only |
| tone curve | only if the cap fired |
| **halve, blur, `scale_depth`** | **always, every map, every source** |

`build_height_map` is the single funnel — the mesh converter and
`parallax_check.py regen` are its only two callers, and it is the only thing
that calls `encode_bc4_dds`. There is no second route by which a `_p.dds` can
come into being.

### `--textures-only`: hand the mesh side to PGPatcher (added 2026-08-19)

`convert.py -f <plugin> --meshes-only --parallax --textures-only` reads and
analyses every NIF and writes **none** of them; only the textures ship, height
maps included.

The reason is that there is a better mesh patcher than us for this job.
**PGPatcher** (ParallaxGen) runs over the player's finished load order, so it
sees every plugin at once, and it can also upgrade a shape to ENB's
complex-material system — which Community Shaders reads too. Neither is
knowable from inside a single-plugin conversion. What PGPatcher cannot do is
recover a height field out of Oblivion's diffuse alpha, and that is exactly
what we keep.

The meshes still have to be READ: whether a diffuse carries height is only
knowable from the shape's own `APPLY_HILIGHT2` flag, the authored intent. So
the analysis is unchanged and only the emit is dropped — `convert_nif` returns
right after `_harvest_textures`, through the same `_finish_result` the normal
path uses, so `batch_convert`'s accounting does not silently read zero.
Animation-object projects, grass models and book inventory art are skipped too,
being mesh products.

### Implementation notes
- `classify_alpha` returns a **category** (`height` / `binary` / `bimodal` /
  `empty` / `no_alpha` / `unreadable`), not a bool, so the build log can say
  WHY a shape was skipped. Thresholds come from the user's own
  TES4AutoParallaxer, tuned on this content.
- DXT5 alpha must be decoded through the **interpolated palette**. Sampling
  only the two endpoints misreads every smooth height field as binary — a
  gentle block's endpoints sit far apart with all six mid-tones between them.
- **Empty alpha reads WHITE (mean 255), not black.** Every flat channel
  measured on Nehrim was 255. Code that assumes an unused channel is 0 gets
  these exactly backwards.
- Output format is **BC4**: one channel, BC1's file size, no banding on grey
  gradients. Written without texconv — a BC4 block is byte-for-byte a DXT5
  ALPHA block, so `encode_bc4_dds` reuses the decoder's own understanding of
  the format. The palette index is **computed, not searched** (quantise
  `hi - v` onto sevenths); the 8-way search cost 4x as much and this runs
  inside the mesh workers. Cost per 512x512 texture: 0.23 s end to end.
  Beyond-Skyrim's BC1 recommendation is for the vanilla path we do not serve.
- Normal maps in both games are **DirectX convention** (green/Y inverted).
  Anything reconstructing height FROM a normal map must flip before taking
  gradients — omitting it produced garbage on the first test build. (Not used
  by the converter, which reads the authored alpha; relevant to the tooling.)
- The prune keeps the maps by itself: the shape names `_p.dds` in slot 3, so
  `_harvest_textures` puts it in the mesh manifest. `_p` is also in
  `texture_prune._MAP_SUFFIXES` and `_companions` derives it from any kept
  diffuse — two independent reasons, no extra handling.
- Alpha-blended flagged shapes need no separate exclusion: the HILIGHT2 branch
  already drops a blend-enabled NiAlphaProperty (below), so by the time the
  shape ships it is not blended. A surviving alpha there is test-only
  (`0x12EC`), and alpha-tested cutout + parallax is legal in Skyrim.
- Audit either side with `python tools/parallax_check.py census|verify`.

## NIF worn armor conversion
- Worn armor (has_skin AND not _gnd AND in armor/clothes dir) must use **NiNode** root, NOT BSFadeNode
- BSFadeNode is for world objects only — worn armor is attached to the character skeleton
- BSDismemberSkinInstance is required for Skyrim biped slot assignment (upgrade from NiSkinInstance)
- Ground models (_gnd) with cloth-physics bones must have skin stripped (bones don't exist in Skyrim skeleton)
- **Material CRC (unknown_int_2)**: ALL vanilla Skyrim NiTriShapeData has `unknown_int_2=0`. This field is the Material CRC in Skyrim BSStream 83. Setting it to 8 (confused with the tangent flags) causes rendering issues. Always set to 0.
- **PRN rigid armor (helmets etc.)**: Oblivion attaches via `Prn` NiStringExtraData on root. Converted to BSDismemberSkinInstance with single bone at weight 1.0. Vanilla Skyrim structure has bone NiNode as FIRST child of root (before geometry blocks). bodyPart=131 (SBP_131_HAIR) is correct for helmets (they replace hair).
- **PRN piece verts are in an upright bone-pivot frame** (same convention as vanilla Skyrim head-local helmet verts), NOT in the rotated Bip01 bone frame — no rotation correction needed; the retarget places them exactly at the SK bone. Therefore the FK-tuned `ARMOR_PIECE_OFFSETS` (helmet dz=+7, tuned on genuinely-skinned helms like TownguardCho) must NOT apply to them — that floated iron/legion helms on top of the head. PRN blocks are collected via `retarget_skin_to_skyrim(prn_out=...)` and get `ARMOR_PIECE_OFFSETS_PRN` instead (helmet dz=-2.1: OB head pivot sits deeper in the skull — OB headhuman.nif top = pivot+13.6 vs SK malehead.nif +11.5).
- **Body part assignment (BSDismemberSkinInstance)**: Oblivion cuirass NIFs have geometry named 'Arms' and 'UpperBody'. The 'arm' keyword in ARMOR_GEOMETRY_BODY_PARTS maps to SBP_32_BODY (not SBP_34_FOREARMS) because gauntlet NIFs use 'Hand' geometry names — 'Arms' only appears in cuirass/shirt meshes. This prevents cuirass arm geometry from being hidden when gauntlets are equipped.
- **Clothing vs armor ARMA body coverage**: Clothing ARMA should NOT add ForeArms(34) extra coverage — shirt sleeves (SBP_32_BODY) should remain visible when gloves are equipped. Armor cuirasses DO add ForeArms(34) because the separate ARMA system allows gauntlets to properly overlay.
- **Shoes vs boots calves slot**: Shoes (clogs, sandals) should NOT claim Calves(38) in ARMA. Only boots get calves. Detection: `'boot' in model_path`. Clothing foot items without 'boot' are shoes.
- **Oblivion alpha-BLENDS surfaces it also alpha-TESTS; Skyrim must not (fixed 2026-07-27, `_skyrim_alpha_property`)**: Oblivion ships cutout geometry as `NiAlphaProperty` flags **0x12ED** (blend bit 0 SET + test bit 9 set). Skyrim reads the blend bit as "draw in the transparent pass", so an opaque diffuse authored that way renders wrong — this is why the **Shivering Isles Dark Seducer body armor was invisible when worn while its ground model was fine** (SI armor is one all-in-one ARMO covering slots 32/33/37/44; its `armor.dds` is DXT3 but **99.5% fully opaque**, so it should be a plain cutout). Vanilla uses **0x12EC** — the identical value with blending CLEAR — on **188/193** surveyed shapes that enable alpha testing (`references/Skyrim Meshes` armor + landscape + architecture + clutter). So whenever the test bit is on, the blend bit is dropped. This is a GENERAL TES4→TES5 rule, not an SI quirk; `grass_profile.py` had already learned the same thing for grass (`0x12ED`→blend clear) and this generalises it to every converted mesh.
- **Blend-on/test-OFF is real transparency EXCEPT under `APPLY_HILIGHT2` (fixed 2026-07-27)**: see-through SI mania/dementia rocks ship `0x00ED` (blend on, test off) — but so do gems, bottles, curtains, posters and potion liquids, which must keep blending, and a flawed emerald has *byte-identical* alpha flags to a rock overlay. The discriminator is **`NiTexturingProperty.apply_mode`**: the rocks use **APPLY_HILIGHT2 (4)**, which is Oblivion's **parallax** switch — that alpha is a HEIGHT FIELD, not transparency and not a blend weight (see the parallax section; the mid-tone-dominant profile the census measured is exactly a height map's) (SI `DMRockSideRoot01.dds`: 0% opaque / 99% partial). Skyrim has no equivalent mode, so it blends the mask across the whole surface → you see through the rock. Census over ~1,000 source meshes (rocks, clutter, architecture, dungeons, plants): **all 5** blend-on HILIGHT2 shapes are the SI rock overlays; **none** of the other 142 blend-on shapes use HILIGHT2 (they are MODULATE=2 or HILIGHT=3). Everything else is left exactly as authored. Do NOT try to classify these by texture alpha percentages — potion liquids measure 100% partial alpha and would be wrongly turned opaque.
  - **The remedy is to DROP the NiAlphaProperty (`hilight2_alpha_dropped`), not to reinterpret it.** Two earlier attempts are recorded because neither is in the code and both are worth not repeating: the first version of this fix turned HILIGHT2+blend+no-test into a threshold-128 cutout, which replaced see-through rock with **completely invisible sections** — these overlay masks are soft gradients with NO fully-opaque texels at all, so a cutout deletes a quarter to a half of the surface outright (`DMRockSideRoot01` peaks at alpha **221** with 29% of texels below 128; `DMRockSideMudBase01` peaks at 238, 26% below; `mrock01worn` 47% below). The second attempt was `slsf_1_decal` + `slsf_1_dynamic_decal` with blending KEPT. 🛑 **Neither shipped** — grep for `slsf_1_decal` or `0x10ED` in `nif_converter.py` and you will find nothing; `_process_geometry` drops a blend-enabled NiAlphaProperty under HILIGHT2 outright and the rock renders solid. Vanilla census (400 random meshes, all block types): blend-on/test-off is a perfectly legal Skyrim mode (370 shapes), and **142/206 blend-on shapes carrying the decal pair all ship alpha flags exactly `0x10ED`** — Oblivion's own `0x00ED` plus the no-sort bit `0x1000`. Vanilla agrees with the drop: across 600 landscape/clutter meshes, 1088/1313 shapes ship no NiAlphaProperty at all and the commonest value on the rest is `0x12EC` (test, blend OFF). Vanilla rock does not alpha-blend.
  - **The same overlay breaks OBJECT LOD even with NO alpha property at all (fixed 2026-08-20)**: the two fixes above both hang off `alpha_prop is not None`, so a HILIGHT2 shape that ships no `NiAlphaProperty` was untouched — correct up close (nothing samples the channel) but see-through at LOD range. `RockGreatForest645` is the reference case: `apply_mode=4`, no alpha property, and its diffuses `GreatForestRock03/01.dds` measure alpha mean 101.6/157.2 with only 0.5%/0.0% of texels fully opaque — a blend WEIGHT, not a mask. Cause: LODGen stamps `slsf_2_lod_objects` on every shape it bakes (`num2 = 5U` in `LODApp.cs`), and the LOD object shader reads diffuse alpha as opacity. Confirmed in the artifacts — `TES4Tamriel.4.4.-12.bto` has 13 shapes and **zero** `NiAlphaProperty` blocks, and 154 shapes across 75 sampled VANILLA `.bto` tiles carry zero between them: vanilla object LOD is opaque, always.
    - LODGen cannot be told otherwise — it writes the shader itself, and it only harvests `NiAlphaProperty` in its `fo4`/`merge5` modes (`ShapeDesc.cs:369`), never the `tes5`/`sse` mode we run, so `isAlpha` stays false and the emit path writes `SetBSProperty(1, -1)`.
    - The texture cannot be flattened in place either — **unless `--parallax` carried the height out to a `_p.dds` first**, which is the one case where the full mesh no longer needs that channel (the height then lives in slot 3, where Skyrim's shader actually reads it). Without `--parallax`, or for a texture the height classifier rejects (92 of Nehrim's 130 flagged diffuses hold no usable height), the full-size mesh still needs the alpha. So mesh conversion records every HILIGHT2 diffuse to `export/<plugin>/overlay_diffuses.txt` (`texture_prune.OVERLAY_MANIFEST_NAME`) and `lod_gen._force_opaque_lod_diffuses` writes an alpha-flattened COPY into the LOD mod's texture tree at the same relative path the tiles reference. Data holds one file per path and that tree shadows the plugins', so tiles get the opaque copy and full meshes keep theirs.
    - **The discriminator is the AUTHORED `apply_mode`, never the pixels.** A genuine cutout mask ships MODULATE (2), is absent from the manifest, and is left alone. Measuring alpha instead flattens tree billboards and cobwebs into solid rectangles — DXT5 leaves a billboard with ~0% of texels at exactly 255 even though it is unambiguously a mask. (Same trap the bullet above warns about for potion liquids.) Measured: 94 overlay diffuses across the 4 Tamriel contributors; `cobweb01.nif` (MODULATE) correctly reports none.
- **Body skin splice section_bboxes coordinate space**: OB body skin sections are in OB skeleton space; SK body NIF verts are in SK skeleton space. These are DIFFERENT frames. The OB arm area (z≈98–105) is at SK z≈72–92 after retarget. **Always use POST-RETARGET section_bboxes** from `collect_skin_info()` — these are in SK world space and correctly localise both arm openings and neck. Pre-retarget bboxes (source OB verts) only work for neck/collar (small-x geometry that happens to be at the same world z in both skeletons) but MISS the arms (which are displaced ~20 Z units by skeleton frame differences). SK male body max arm reach (|x|>20) sits at z=75–97 world, exactly within the post-retarget 'Arms' bbox z=72–92. Use `bbox_pad=1.0` to stay under 25% of total body verts spliced.

## Body-wrap armor fitting (2026-07-10/11, `asset_convert/body_wrap.py`)
- **Architecture: FK base + measured-error correction field.** FK (animation DQS) is locally smooth but lands armor 0.5-2.5 units off the SK body (the in-game clipping). The wrap field measures FK's error EXACTLY by running the actual OB body meshes (upperbody/lowerbody/hand/foot) through the very same FK retarget, then fitting them onto the real Skyrim body NIFs via iterative closest-point projection with topology-aware delta smoothing (never bleeds between the legs) + limb-segment length prescaling. Fits BOTH weight-slider targets (`malebody_0` AND `malebody_1` etc.); cached per gender in `generated/body_wrap_{male,female}.npz` (src/fkp/dst0/dst1/tris/vert_bc/part). Runtime: FK first, then each armor vertex gets `delta = dst[w] - fkp` interpolated from the K=40 nearest body triangles (Gaussian distance + skin-weight bone-centroid gating + wrong-side penalty), then a clearance-enforcement push. Rebuild with `python -m asset_convert.body_wrap` (uses `allow_wrap=False` internally -- the field must never bootstrap from a previous field).
- **_0/_1 weight variants (2026-07-11)**: `convert_nif` writes `<name>_0.nif`/`<name>_1.nif` for every biped wearable (any non-`_gnd` mesh the wearable plan names — see the folder-vs-plugin note below). **The _1 file is NEVER a second independent conversion** — the engine lerps the pair per-vertex, so the pair must be topology-identical; a reconversion clips the body splice differently and mid-slider values vertex-explode (observed in game). Instead `body_wrap.morph_converted_to_weight1` post-morphs the finished _0 mesh with the fitted `dst1 - dst0` body morph (built from the REFERENCE Skyrim bodies — the modified output bodies have bugs and are never used for weights); spliced fill lies on the _0 surface so it gets the exact body morph, rigid PRN blocks are untouched. tes5_import ARMA enables the weight slider + `<name>_1.nif` path ONLY for gear covering TES4 biped bits 2-5 (upper/lower body, hand, foot) — vanilla helmets (IronHelmetAA) and shields (IronShieldAA) have the slider DISABLED and a plain path, and slider-on shields misbehaved in game.
- **What counts as worn gear is the PLUGIN's call, not the folder's (2026-08-08)**: `_convert_nif` used to decide with `'armor' in src_path or 'clothes' in src_path`. That holds for vanilla Oblivion, which files every wearable under `meshes\armor` or `meshes\clothes`, but it is a guess about a naming convention. Nehrim files 88 worn meshes under its own folders (`eyren/`, `spinat/`, `nehrim/`, `skeletonk/`, `dwemertechnology/`, `ttbeards/`, `mr_siika/`, `suedland_set/`) and every one of them was converted as a **world object**: BSFadeNode root instead of NiNode, plain NiSkinInstance instead of BSDismemberSkinInstance, no retarget onto the Skyrim skeleton — and, because the same substring gated the variant writer, no `_0`/`_1` pair, so 52 of the 61 unresolvable ARMA paths were simply never written and the engine drew nothing (guards with a head and hands but no torso). The authored answer is the plugin's own biped model references: `wearable_plan` now sets a `WORN` bit on every path an ARMO/CLOT names as a biped model, and `wearable_plan.is_worn` answers the question. The folder test survives only as the fallback for meshes no record references. Verified byte-identical output for `armor/` and `clothes/` controls (mesh conversion is **not reproducible across processes** unless `PYTHONHASHSEED` is fixed — set/dict iteration order leaks into the written bytes, so any A/B of NIF output must pin it). The remaining 9 misses are dead references: those meshes exist in no Nehrim BSA and no loose file, i.e. they were broken in the original game too.
- **🔴 Body-skin identity comes from the BONES, not the texture name (2026-08-09)**: Oblivion bakes the wearer's skin into a wearable; the converter strips it and splices Skyrim body geometry back, choosing which body NIF by a keyword in the texture path (`_SKIN_TEX_TO_BODY_NIF`). That is the author's *label*, not what the geometry *is*. Nehrim ships 18 wearables whose torso skin carries a foot or hand texture — the Silverlight cuirass (`Foot:Body`, 3321 verts, weighted to Spine/Spine1/Spine2/Clavicle/Neck/Pelvis, textured `characters\imperial\female\footfemale.dds`) and the entire female Eyren set (four battledresses at 3321 verts plus four greaves). The keyword picked `femalefeet_0.nif`, which contains no torso, so the stripped chest was never spliced back: the armour renders as plates with see-through gaps and the actor looks half-invisible rather than naked. `collect_skin_info` now overrides a hands/feet classification when the skin instance is weighted to **spine, clavicle or neck**. Those three are deliberately the only test — a gauntlet legitimately reaches the forearm and a boot the calf, so including those bones produced 9 false positives on correctly-named vanilla gauntlets; spine/clavicle/neck produced zero. Survey any plugin with `python tools/body_skin_audit.py [plugin] [--all]`. **Diagnostic trap:** the symptom reads as a texture or alpha problem — the source NIF genuinely does have `NiAlphaProperty flags=0x00ed blend=True` on several shapes — so it invites an alpha investigation. Compare the source's shape list against the converted one first; a missing body shape is instantly visible and the alpha is a red herring.
- **Cross-block solve is mandatory**: `deform_geoms_wrap` concatenates ALL non-PRN blocks into ONE weld/correction/diffusion system. Per-block solving gave coincident seam verts across blocks (cuirass/pauldron boundary) different corrections — visible seam splits. `weld_groups` is true distance welding (KDTree pairs + union-find), not grid rounding (rounding-boundary twins split).
- **Head gear**: the field has no head surface, so a geometry whose skin weights are >50% `Bip01 Head` keeps plain FK + the legacy `ARMOR_PIECE_OFFSETS['helmet']` offset (nif_converter applies it for `_piece_type == 'helmet'` even when the wrap is active). Decision is PER-GEOMETRY, not per-vertex — shirt collars carry partial head weights and must keep full correction/enforcement. PRN helms: `ARMOR_PIECE_OFFSETS_PRN['helmet']` = measured OB-head→SK-head bbox affine (dy=-1.6, dz=-2.1, sx=1.12, sy=1.165 — the SK skull reaches 2.2 further back with only 0.27 front slack, so translation alone cannot stop back-of-head clipping).
- **When the field exists, `ARMOR_PIECE_OFFSETS` are SKIPPED** (nif_converter checks `body_wrap.wrap_available`) -- the field's far-range constant extrapolation replaces that hand-tuned drift table. PRN offsets (`ARMOR_PIECE_OFFSETS_PRN`) still apply, and helmet pieces keep both (see above).
- **Approaches that FAILED before landing here**: (1) pure surface-relative wrap (offset from body surface, rigid transport through triangle frames) -- preserves clearance perfectly but imprints the fitted map's tangential bunching onto every body-hugging vertex (gauntlets 31% edge failures vs FK's 2%); (2) tangential isometry relaxation of the fitted body -- the scale field is a fixed point of the current state (no-op) and heavy diffusion made fingers hop between surfaces; (3) restoring the normal component after correction-field smoothing -- the normal component of the noise IS the noise (gauntlets 24%). Correction-field smoothing (12 Jacobi passes at load, `DELTA_SMOOTH_PASSES`) is the tuned tradeoff: fewer passes = crisper fit, more = smoother mesh but clipping slowly returns past ~16.
- **Clearance enforcement** (`CLEAR_MARGIN=1.0`, 2 iterations): authored clearance (T-pose vert vs OB body) + outward margin is enforced against the fitted surface. The deficit is DIFFUSED over the (global) armor mesh graph before pushing (raw per-vertex pushes crumple meshes: gauntlets went to 62%), but `PUSH_RAW_KEEP=0.6` of the raw deficit survives as a floor — diffusion alone diluted genuine isolated deficits (shirt-collar rings 0.9-1.9 deep) into surrounding slack. Gates: authored proximity (`CLEAR_PROX=4.0`; 2.5 faded enforcement exactly where collars authored 2-3 off the neck clipped), fit-reliability (local stretch, floored at `REL_FLOOR=0.4` on body triangles — otherwise enforcement dies at wrist/neck seam rings), and **body-part-only** (`part` array; hand/foot fits are noisy at fingers, and gauntlets/boots replace body hands/feet in Skyrim — EXCEPT the wrist/ankle seam region: hand/foot verts within 3 units of the body surface count as body, which is where clothing shoe tops and shirt cuffs clip). In the reliability blend, zero-rel hand/foot triangles ABSTAIN (weighted mean over voters within d_best+2) instead of vetoing — a cuff half-surrounded by hand triangles keeps the forearm's reliability, but boot-shaft verts must not inherit reliability from calf triangles 8+ units away. Verts authored INSIDE the OB body (collar necklines, c0 ~ -0.6..-1.5) get depth-preservation (target = c0, no margin — `CLEAR_INNER_FADE`); excluding them entirely let the field drag collars 2+ units deeper.
- **Metrics tool**: `python -m tools.armor_fit_metrics <src.nif> <converted.nif> [--weight 0|1]` -- edge-failure %, high-frequency distortion (per-tri stretch spread = crumple signal vs smooth reshaping), clearance preservation vs the wrap surfaces, penetration. Distortion is deliberately traded for anti-clipping (user: clipping is visible, distortion is not): iron cuirass ~25% edges>15%, boots ~19%, gauntlets ~7.6%, but flagged penetrating verts are near zero everywhere visible (male shirt collar 73→5, female shirt collar 67→2, gauntlets/boots vs their replaced body parts don't count). Known remaining warts: crotch-cavity hems (cuirass front fauld, robe center panel) where signed distance itself is ill-defined -- hidden in game.

## NIF weapon Prn (attach node) contract
- The draw animation looks for the weapon at the skeleton node matching its WEAP AnimationType; the mesh's `Prn` decides where the engine actually parents it. A mismatch = weapon stays sheathed / hands look empty when drawn (seen THREE times: axes with Prn=WeaponAxe but WEAP type Mace; bows with Prn=WeaponBack; shortswords with Prn=WeaponDagger but WEAP type Sword → "invisible while held", fixed 2026-07-15).
- **Shortswords stay on WeaponSword** (they're Sword-type records); **daggers get Prn=WeaponDagger AND the WEAP record refined to AnimationType Dagger (2)** — the filename-keyword refinement runs on BOTH sides (`_remap_prn` in nif_converter.py and `convert_WEAP` in tes5_import/record_types/equipment.py) keyed on the model basename so they can never diverge.
- **Bows must get Prn='WeaponBow'** (vanilla ironbow.nif), NOT 'WeaponBack' — Oblivion uses 'BackWeapon' for both 2H weapons and bows, so `_remap_prn` refines by filename ('bow' in basename).
- **Bows are exempt from the blanket weapon 180° Y-flip** (the war-axe orientation fix applied to every `_WEAPON_PRN_VALUES` mesh): Oblivion bows already match the Skyrim WeaponBow frame — string plane at x≈-15.7 vs vanilla string bones at x≈-13.7, limbs along ±Y. The flip held them backwards (curve toward the archer).
- **Bow bend rig (`asset_convert/bow_rig.py`)**: converted bows get the exact vanilla 7-bone chain (Bow_MidBone → Lo/Up chains → StringBones; locals lifted from vanilla steelbow.nif — the rig is the animation contract, BowProject.hkx clips store absolute local bone transforms) + BGED `Weapons\Bow\BowProject.hkx` + BSXFlags Animated bit (0x08). Geometry is skinned with plain NiSkinInstance (vanilla bows never use BSDismember) using the measured vanilla weight profile (Mid→B1 crossfade |y| 4-16, B1→B2 20-36, tips ~58/42 B2/StringBone; string = SB1↔SB2 lerp). String verts are identified from the Oblivion NiGeomMorpherController draw morph (string moves ~28 units vs limb ~7-10; capture BEFORE controllers are stripped) — verified on all 8 vanilla Oblivion bows.
- **SLSF1_Skinned (shader_flags_1 bit 0x02) is mandatory on the bow shape's BSLightingShaderProperty** — without it the renderer never applies bone deforms: the bow renders frozen in bind pose while the graph animates the bones (string never draws, limbs never bend). Shader conversion runs before the rig exists, so `add_bow_rig` sets the flag itself after skinning (vanilla steelbow SF1=0x82400383 has it set).

## NIF torch Prn — Skyrim carries the torch on the SHIELD node (SOLVED 2026-08-01)
- Oblivion `Prn='Torch'` → **`'SHIELD'`**, not `'NPC L MagicNode [LMag]'`.
- Skyrim holds the torch in the off-hand: vanilla `meshes\weapons\torch\torch.nif`
  ships `Prn='SHIELD'` (and lives under `weapons\`, not `lights\`). The static
  sconce torches under `clutter\common\` carry **no Prn at all** — they are
  placed world objects, so they are not evidence for the carried case.
- `NPC L MagicNode [LMag]` is the spell-**cast** node; its axes point outward
  from the open palm, so a torch parented there renders rotated ~90° with the
  flame sticking out to the left. Weapons were unaffected because they route
  through the `Weapon*` nodes, which is why this looked torch-specific.
- **Sharing the SHIELD node does NOT make it a shield.** The `remapped ==
  'SHIELD'` branch must be split on the ORIGINAL `prn_val`: a torch takes
  **no shield attach transform**. That transform exists only because Oblivion
  straps a shield to `Bip01 L ForearmTwist` while Skyrim glues the root to the
  SHIELD bone at the grip — a torch is authored at the grip in both games, so
  remapping frames throws it ~65° off with a -20.5 forearm-strap offset. This
  was the second, separate cause of "torch orientation completely wrong": the
  Prn was right, the geometry transform was not.
- Vanilla `torch.nif` is identity rotation, zero translation, geometry at
  identity; flame at +Y (`TorchFire` y≈28.96), matching the converted
  `FlameNode1` y=27 / `AttachLight` y=35.
- Torch **does** still need its own BSInvMarker: `SHIELD` is in
  `_EQUIPPED_PRN_VALUES`, so the per-mesh inventory pass skips it and it would
  otherwise ship none. Vanilla values rot (4712, 0, 0) **zoom 0.82** (shield is
  the same rotation but zoom 1.0) → `TORCH_INV_MARKER_*` in `skyrim_overrides.py`.
- Verified against `references/Skyrim Meshes` — **not** the SSE BSAs, which are
  off-limits (see CLAUDE.md); `asset_convert/skyrim_assets.py` is for the
  runtime pipeline only, never for "what does vanilla do here?" debugging.

## NIF shield conversion
- Shields use BSFadeNode root + Prn='SHIELD' (same as weapons, NOT NiNode like worn armor)
- **Orientation fix**: Oblivion shields are modeled with thin (face-normal) axis along Y. Skyrim's SHIELD bone expects it along Z. A +90° rotation around X is applied to the BSFadeNode root. Root rotation baking wraps this in an inner NiNode.
- **BSInvMarker**: Shields need BSInvMarker for inventory display: rot=(4712,0,0), zoom=1.0 (from vanilla ironshield.nif). Without BSInvMarker, shield is invisible in inventory. Shields keep this constant — they are exempt from the per-mesh inventory-orientation pass (see BSInvMarker section below).
- Oblivion Prn values for shields: 'Shield' or 'Bip01 L ForearmTwist' → remapped to 'SHIELD'
- Oblivion shield geometry names: 'Shield:0', 'Shield:2' (single geometry block, no skin)

## NIF armor ground model (_gnd) conversion
- Armor/clothing _gnd files need **BSInvMarker** for inventory display. Without it, items are invisible in the inventory 3D viewer.
- BSInvMarker is added during NiNode→BSFadeNode conversion when `_is_gnd and _in_armor_dir` (constant rot=(1570,0,0) as an initial value), then **recomputed per-mesh by the inventory-orientation finalize pass** (see below).
- BSXFlags: vanilla gnd files use 194 (0xC2); our converted use 130 (0x82). Both load fine.
- **Ground-model detection must match the bare `gnd` suffix, NOT `_gnd` (fixed 2026-07-28, `_is_ground_model`)**: Bethesda's convention is `<item>_gnd.nif`, but assets that came through a Morrowind→Oblivion conversion lost the separator. Morroblivion rewrites `_` as `u`, so the same files arrive as `<item>ugnd.nif` (`cumurobeucommonu02ugnd.nif`, cf. `TXUCUclothwrap01`); others drop the separator after a body-part word (`...shoegnd`, `...shirtgnd`, `...pgnd`, `amuletcommon1gnd`). Census of `export/`: **1,120 files use `_gnd`, 195 do not** — every one of the 195 was misread as *worn armor*. Three failures follow from that single flag, and they are exactly the reported symptoms:
  1. `_is_worn_armor` becomes true → root stays **NiNode** instead of BSFadeNode, so the item **has no collision and floats where it was dropped / can't be picked up**;
  2. the `_is_gnd and _in_armor_dir` guard skips **BSInvMarker** → invisible/unoriented in the inventory viewer;
  3. worst, the `not _is_gnd ... and has_skin` guard lets a *static* ground model be **FK-retargeted onto the Skyrim biped** — this **mangles the mesh**. 48 of the 195 are skinned and were being deformed this way.
  The mangling looks like a mesh-orientation bug (the robe's source bbox is X ±47, Z −86→32, i.e. apparently "on its side") but the source geometry and its bind data are correct and upright — the wide/negative extents are just sleeves and the down-the-bone bind offsets. Nothing needs to be "stood up" first; the mesh only had to skip the retarget. Worn variants of the same items (`cumupantsugucommon010.nif`) always looked fine because they take the worn path legitimately.
  Matching bare `gnd.nif` covers all three spellings. A worn mesh would have to genuinely end in the letters "gnd" to false-positive, which no body slot or equipment word does (verified: the char preceding `gnd.nif` across all assets is only `_`, `u`, or a body-part/index character, plus files named exactly `gnd.nif`).

## BSInvMarker inventory orientation (learned 2026-07-18)
- **Engine convention** (derived empirically with `tools/inv_marker_survey.py` across ~500 vanilla meshes, mean alignment 0.97+): stored ushort angles are milliradians; the inventory view rotates the model by `M = Rx(-rx/1000) @ Ry(-ry/1000) @ Rz(-rz/1000)` (column-vector, XYZ order, negated angles) and the camera looks along **+Y** with **+Z as screen-up** (screen-right = X). Reproduces vanilla exactly: ironshield (4712,0,0) = −Z face toward camera; cuirassgnd (1570,0,0) = +Z face toward camera with model −Y at screen-up; iron weapons (4712,6283,0) ≈ pure Rx.
- **Per-mesh computation** (`asset_convert/inv_marker.py`): the finalize pass at the end of `_convert_nif` orients each mesh so the side with the greatest front-facing projected area (of the six area-weighted PCA axis directions of the triangle soup) faces the camera. Screen roll keeps model +Z at screen-up (upright items stay upright); when the view normal is ±Z (items modeled lying flat: books, pelts, plates, gnd armor) it follows the vanilla cuirassgnd rule (−Y up for face-up items, +Y for face-down). Hidden geometry (flags & 1), `Blood*` decal shapes and EditorMarkers are excluded from the analysis.
- **Scope**: applied to every non-creature, non-skinned BSFadeNode root — a marker is inert on meshes never shown in inventory, and clutter/books/ingredients/keys/soul gems have no reliable path signature. Existing markers (gnd) are recomputed; missing ones are added (zoom 1.0).
- **Weapons/shields/quivers are exempt** (`_EQUIPPED_PRN_VALUES`, matched on the post-remap Prn): conversion normalizes them into vanilla attachment frames (Prn node convention / SHIELD attach transform), so the vanilla-derived constants are already exact. The computed value would also flip shields: a shield's concave strap side genuinely has more visible area than its display face.
- Geometry math uses PyFFI's row-vector transform convention throughout — the survey validated stored-marker ↔ pyffi-space relationships end-to-end, so the generator must use the same gather code (`_gather_area_normals`).

## NIF skin retargeting (Oblivion → Skyrim skeleton)
- **Critical**: Oblivion skeleton uses X-up coordinates (spine along X axis). Skyrim uses Z-up (spine along Z)
- **Current approach: Corpus search + L-BFGS-B continuous optimization**:
  1. `tools/kf_animation_explorer.py --build-cache` searches 453 .kf animation files with parallel parsing (ThreadPoolExecutor, 31 workers, ~36s)
  2. Per-bone transform library: 65 bones, 336K candidates from entire animation corpus
  3. Chain-level softmax blend (T=1.0, effectively argmin) over ~25K coherent frames per chain (left/right arm, left/right leg — body chain excluded)
  4. **Multi-start L-BFGS-B refinement**: 50 starting frames per chain, axis-angle rotation perturbation bounded to ±0.35 rad (~20°), parallelized with ThreadPoolExecutor. Discovers poses NOT in the .kf corpus.
  5. L/R mirroring for symmetry
  6. Pre-computes delta matrices `inv(rest_world) @ anim_world` per bone, saved to `asset_convert/generated/best_animation_pose.json`
  7. `skin_retarget.py` Phase B.1: loads pre-computed deltas, applies standard LBS using OB skin weights: `v' = Σ w_i * (v @ delta_i)`
  8. Phase A: repositions bones to Skyrim skeleton positions
  9. Phase C+D: recomputes bind matrices (`_manual_update_bind_position`) and skin partitions
  10. **FK+Gaussian double-deformation MUST be avoided** — Gaussian spatial blend only runs when FK was NOT applied.
- **FK results**: Post-mirror RMSD 9.64 (was 9.73 corpus-only). Legs: 2.8/1.8→1.08/1.08 (62% improvement). Arms: 4.4→4.0 (10%). 37/37 tests pass, 396 armor NIFs 0 errors.
  - **`_mat3_to_quat` NIF convention**: This function expects a column-vector convention matrix. PyFFI Matrix33 / NIF matrices use row-vector convention so `_mat3_to_quat(NIF_Matrix)` returns the CONJUGATE. In `skin_retarget.py` the delta matrices are numpy column-convention, so pass `_mat3_to_quat(delta[:3,:3].T)` (transpose, no sign flip). For collision baking this is moot — **do not apply _mat3_to_quat to bhkRigidBodyT at all**.
- **Spatial blend residual was wrong direction**: `v_spatial` (spatial blend from OB rest ≈ 50% to SK) minus `v_fk` (FK ≈ 90% to SK) = vector pointing BACKWARD toward the LESS-transformed position. DQS inherently handles joint boundaries — no separate residual needed.
- **ProcessPoolExecutor causes issues on Windows**: Exit code 1 + slightly worse results. Reverted to sequential `for` loop for L-BFGS-B multi-start. Module-level `_lbfgsb_trial_worker` kept (clean, no harm). ThreadPoolExecutor for first kf-parsing step is fine (I/O-bound).
- **Geometric limit**: Arm RMSD ~4.0 is the minimum achievable with rotation-only optimization. UpperArmTwist (err=13.4) and ForearmTwist (err=9.4) contribute 56% of arm cost from bone LENGTH differences between OB/SK skeletons. Excluding twist bones from cost made mesh quality WORSE (larger main-bone rotations).
- **Body chain**: Including spine in optimization gives spine RMSD 5.59 but BREAKS cuirass edges (18.8% fail) — spine deltas distort LBS. Spine gets identity delta; Phase A handles repositioning.
- **Gaussian spatial blend (fallback)**: Only runs when best_animation_pose.json is absent. Uses distance-based Gaussian-weighted bone blending with σ=20.
- Vertices in OB armor NIFs are in standard world-space coordinates (Z-up), NOT in the OB convention-rotated space.
- NiSkinData B_bone = inv(W_sk_bone) when M_mesh = identity (standard for skinned armor)
- Skeleton data: `asset_convert/generated/skeleton_bones_skyrim_{male,female}.json` and `skeleton_bones_oblivion.json`
- Female armor detected via `/f/` in path → uses female skeleton data
- PRN meshes (single bone, identity B) are NOT reposed — they're rigidly attached to one bone
- **Critical**: ALWAYS use `_manual_update_bind_position()` instead of PyFFI's `update_bind_position()`. PyFFI's version computes wrong B values when geometry has a non-identity local transform. The manual numpy version handles this correctly.
- **Test suite**: `tests/test_skin_retarget.py` — 37 tests covering skeleton loading, bone mapping, MBW=I, vertex deformation, bone position accuracy, edge length preservation (<10% failure for cuirass, <5% for boots), full converter integration, skin partitions, PRN handling, BSDismemberSkin. All 37 pass.
- **Previous approaches that FAILED** (16+ attempts):
  - v2 bind-matrix-only (no vertex deformation): Arms stuck in A-pose at rest.
  - Skin-weight-based DQS/LBS: Sharp weight boundaries → 24-82% edge failure
  - Gaussian spatial blend alone: 40-50% displacement dilution on arms (normalized weight averaging)
  - FK LBS + Gaussian together: Double deformation → 13.9% edge failure
  - Laplacian-smoothed mesh weights: Created discontinuities. REVERTED.
  - Global/per-mesh inverse filter, RBF interpolation, 2x global overshoot: All failed (see repo memory for full list)

## Creature skin render crash — >80 skin bones per shape (SOLVED 2026-07-10)
- Symptom: render-thread `EXCEPTION_ACCESS_VIOLATION` in a VCRUNTIME140 memcpy
  (`vmovdqa [rcx+…]`) inside BSBatchRenderer pass setup (BSUtilityShader =
  shadow-depth pass); crash objects show NiSkinInstance/NiSkinPartition +
  BSTriShape named `"(<armo fid>)[0]/(<arma fid>) [50%]"`. FormIDs resolve
  (via `tools/tes5_esm_reader.py <esm> --formid <fid>`) to the generated
  creature skin ARMO/ARMA.
- **Root cause (proven by crash-log registers)**: SSE memcpys one 3x4 matrix
  (48 B) per NiSkinInstance bone into a fixed **80-matrix (3840 B) buffer**.
  Imp = 85 bones → copy size RBP=4080=85×48, fault at dest offset 3840=80×48,
  R8=464 remaining. Per-partition bone counts are irrelevant; the per-shape
  TOTAL is the limit. Vanilla max: dragon, 77. The crash needs the shadow
  path, so >80-bone actors can *appear* fine where no shadow-casting light
  hits them (mehrunesdagon/spiderdaedra initially seemed unaffected).
- **Fix (in-game verified)**: `skin_retarget.merge_oversized_skin_bones()` —
  merge the lowest-total-weight LEAF bones into their parents until ≤78
  (SSE_MAX_SKIN_BONES; vanilla max is 77 and splitting at exactly 80 froze the
  game, so stay clearly under). Bind pose is exact (B·W=I at rest); only tip
  articulation (fingertips/ear tips/eyebrows) is lost. Weights renormalized;
  partitions regenerated afterwards.
- **Where it runs matters**: called from `merge_creature_body()` AFTER rig
  grafting — Oblivion body-part NIFs store their skin bones FLAT (no
  parent/child links), so leaf detection only works on the merged rig. The
  hierarchy lookup is BY NAME (bone pointers aren't tree members at part
  stage). Affected creatures: imp 85→78, spiderdaedra 88→78, mehrunesdagon
  98→78; all other 151 merged bodies were already ≤80.
- **Shape-SPLITTING does NOT work** (tested in-game: game froze) — don't
  split skinned shapes to duck the cap; merge bones instead.
- Diagnostics: `tools/skin_partition_dump.py <nif>` (per-shape/partition
  bone/vert/tri counts, flags >80-bone shapes + index-range problems).

## NIF bhkRigidBody field mapping (PyFFI ↔ newer nif.xml)
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
- **Mass**: Keep Oblivion mass as-is. Oblivion clutter (0.1–8.0) is already in Skyrim's range (0.5–100). The legacy converter's `mass *= 6` is WRONG — makes items too heavy and causes them to "hang in the air."
- **Inertia tensor**: Must scale by `HAVOK_SCALE² = 0.01`. Oblivion inertia (2.3–8.8) is ~100× Skyrim (0.02–0.32) because inertia ∝ mass × distance² and collision shapes are scaled 0.1× for Skyrim Havok units.
  - The full ×0.01 is applied EXACTLY ONCE, in `_convert_collision` (dynamic + keyframed branches) and `_convert_blend_collision`. `scale_constraint_pivots` must NOT rescale again — a leftover ×0.1 there had every constrained body's inertia 10× too small (fixed 2026-07-15).
- **Skyrim clutter standard values**: friction=0.50, restitution=0.40, linear_damping=0.0996, angular_damping=0.0498, max_linear_velocity=104.4, max_angular_velocity=31.57, deactivator_type=1, solver_deactivation=2

## MO_SYS_FIXED (7) statics simulated as clutter — "floating / spinning / on its side" (SOLVED 2026-07-28)
Third-party plugin statics (streetlights, beds, tables, shrines, chests, torches) tipped onto their sides, drifted, or spun off through the air on cell load.
- **Cause**: `_convert_collision`'s static-vs-dynamic branch dispatched on **mass alone** (`elif rb.mass == 0:` → static, `else:` → dynamic). Oblivion `MO_SYS_FIXED` (7) — nif.xml: *"used for the static elements of a game scene, e.g. the landscape"* — was never consulted, so any fixed body with a non-zero mass field became a fully-simulated Skyrim prop with a mesh collision shape.
- **Why base Oblivion never showed it**: a 300-NIF census of `Oblivion.esm` found **198 ms=7 bodies, 0 with mass>0** — Bethesda always zeroes mass on fixed bodies, so mass alone happened to classify every one correctly. The inference was wrong but indistinguishable from correct on vanilla data.
- **Why Morroblivion did**: the same census over `Morrowind_ob.esm` found **186 ms=7 bodies, 157 with mass>0** (its idiom is `mass=1000` + `layer=1 OL_STATIC` for "static"). The majority of its statics were being converted into 1000 kg dynamic clutter.
- **Fix**: before the mass dispatch, `rb.motion_system == 7 and rb.num_constraints == 0` → `rb.mass = 0.0`, falling into the existing static branch (ms=5 BOX_STABILIZED, quality 0, mass 0). Constraint-owning fixed bodies are left alone — they are real trap/chain parts handled by the constraint branches.
- Measured: 125/227 sampled Morroblivion meshes corrected, 0 left dynamic; base-Oblivion clutter (ms=1/2/4, real masses) verified unchanged and still dynamic.
- **General lesson**: the source's declared motion type is the authoritative statement of static intent — never re-derive it from mass. A heuristic that is *accidentally* total on vanilla data will silently misclassify third-party content.

## Skyrim APPLIES rotation/translation on non-T bhkRigidBody (THE fundamental havok bug, found 2026-07-15)
The single most important havok-conversion fact, and the root cause of both "constrained objects act completely rigid" AND the longstanding "havok interactions feel weird on normal misc items":
- **Oblivion ignores the translation/rotation fields on plain (non-T) `bhkRigidBody`**, so Oblivion NIFs ship arbitrary leftover values there (chain links carried rotations up to ~115°).
- **Skyrim applies BOTH fields on BOTH body classes.** Proof: vanilla `trapmace01.nif` Base01 — the node is rotated +0.5° about X and the plain bhkRigidBody carries exactly the inverse quaternion (-0.0044,0,0,1) so its root-space MOPP stays aligned; every other vanilla non-T body is exactly identity/zero, unlike Bethesda's genuinely-garbage padding fields.
- Consequence of passing them through: every constraint frame and collision shape is rotated out from under the solver → constraint assemblies act welded solid; ordinary clutter collision sits askew from the visual mesh.
- Fix in `_convert_collision`: non-T bodies get translation=(0,0,0,0) AND rotation=(0,0,0,1). bhkRigidBodyT keeps its (scaled) transform. NOTE: field-level dumps looked "fine" for months because everyone (and the docs) believed the non-T fields were dead — when a converted mesh matches vanilla on every OTHER field, byte-diff the remaining "ignored" ones.

## Inverted collision winding — "I fall through the floor" (SOLVED 2026-07-20; **rewritten 2026-08-20, see round 3 below**)
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
- **Tooling**: `tools/collision_winding.py --ab <ref_tree>` (exact recall/breakage vs ground truth) and `--floor-regress` (the in-game invariant). Run both before shipping a change here.

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
- **Verify**: `python tools/nif_block_scan.py output/<plugin>/meshes --has bhkPackedNiTriStripsShape`; any hit must have `data.num_sub_shapes >= 1` covering all vertices. Note the scanner reads the header block-type *table*, so it flags a file whose table still lists the type — confirm with a block walk before concluding a real shape is present.

## Constrained objects: chains, swinging traps, gates, trigger phantoms (2026-07-15)
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
- Debug tool: `python tools/havok_constraint_dump.py <nif|dir>` prints per-body filter (layer/flags/group), inertia, motion/quality, damping, and full constraint descriptors (pivots/axes/limits/friction) — the scene-tree analyzers hide all of this.

## Early-Oblivion NIF versions (10.0.1.0 / 10.0.1.2 / 10.1.0.106) — the [RD] read failures (SOLVED 2026-07-15)
Oblivion's BSAs contain dev-era leftovers in older NIF versions that PyFFI 2.2.3 can't parse (floorplane01, handscythe01, oar01, stonepedastellarge01, ungrdltraphingedoor, kvatch castle int hallway01, arwelkydclusterfx01, scampswitch01). Fixed with monkey patches 5-7 in `asset_convert/pyffi_monkey_patch.py` (field-presence guards verified against `references/nif 0.10.0.0.xml` + byte-level decode):
- ≤10.0.1.2: extra uint after bhkWorldObject.Shape and at the start of HavokMaterial; bhkRigidBody CInfo lacks the 16-byte filter-copy header and max-velocity trio; bhkMoppBvTreeShape lacks the offset vector; bhkNiTriStripsShape lacks the scale Vector4; 10.0.1.0 mopp data is FULL size (pyffi's "size-1" convention is pre-Bethesda).
- 10.1.0.106: NiSingleInterpController.Interpolator exists since 10.1.0.104 (pyffi said 10.2); NiInterpController has a Manager Controlled byte (10.1.0.104-108); NiPSysEmitterCtlr.VisibilityInterpolator since 10.1.0.104; NiBlendInterpolator uses the full runtime-state layout (item array + per-subclass value snapshot: Transform 35B, Point3 12B) — hand-rolled consume-only reader.
- `bhkConvexSweepShape` (10.0.1.0 clutter) registered as a class at runtime; `_convert_shape` unwraps it to its inner shape (Skyrim never ships it).

## Orphaned blocks in `data.roots` — the [EXC] `'<block>' object has no attribute 'controller'` failures (SOLVED 2026-07-20)
PyFFI reports **every unreferenced block** as a root, not just scene-graph roots. Many Nehrim meshes (all of `castle\*_far.nif`, `artilleryduell\flamecannonballnew.nif`, the `nehrim\zahnrad*` gear set, ~60 files) were authored by tools that leave dangling `NiTriShapeData` / `NiTriStripsData` / `NiBinaryExtraData` / `bhkCollisionObject` / `Ni*Property` blocks behind, so `data.roots` comes back as `[NiNode, NiTriShapeData, ...]`. Every pass in `_convert_nif` assumes a root is a node and reads `root.controller` / `root.children` → `AttributeError` (the varying class name in the error is just whichever orphan landed in the list).
- `_prune_orphan_roots(data)` runs first in `_convert_nif`: keeps `NiAVObject` roots, plus any non-node root still reachable from them (never drop something a kept root references). No-ops when there are <2 roots or no node root at all, so it can't empty `roots`.
- The orphans are unreachable from the real root — dead weight, so dropping them also shrinks output. PyFFI's "block is missing from the nif tree: omitting reference" notice on write is the expected, benign confirmation.
- **Files whose ONLY root is a non-node** are standalone animation files (`creatures/*/idleanims/*.nif` → a lone `NiControllerSequence`). There is no geometry to convert; `convert_nif` returns `error='NOGEO'` and skips instead of crashing.
- Related trap: **never trust `num_vertices`/`has_normals` over the actual array length.** `leyawiinhouselower01_far.nif` has a shape with `num_vertices=16` but an empty `vertices` array (stale count, `has_vertices` unset), which made `np.array([...])` a `(0,)` array and blew up the matmul in `inv_marker._gather_area_normals`. Guard with `len(gd.vertices)` and `len(gd.normals) == len(gd.vertices)`.

## NIF NiDefaultAVObjectPalette fixup
- After converting NiTriStrips→NiTriShape, NiDefaultAVObjectPalette entries still reference old blocks. Must update `av_object` references using a block_map (old id → new block). Without this fix, PyFFI writes "NiTriStrips block is missing from the nif tree" warnings and the animation palette has stale references.

## Skinned shape = red triangle: NiSkinPartition still in STRIP format (SOLVED 2026-08-01)
**This is the actual cause of the `ropebucket01.nif` red triangle.** (The
`skeleton_root` fix below is a real defect and was fixed in the same pass, but
it did NOT fix the red triangle — don't stop there again.)

- A `NiSkinPartition` stores geometry as **either strips or triangles**.
  Oblivion writes strips. Skyrim's renderer draws a skinned shape from the
  **partition**, not from `NiTriShapeData` — a strip-format partition hands it
  no triangles and the shape renders as the red missing-geometry marker.
- **Census: 678/678 vanilla skin partitions across 350 sampled meshes store
  TRIANGLES. Zero store strips.**
- The strips→triangles pass in `_walk_node` rebuilds `NiTriShapeData` but
  **does not touch the partition**. The two `_regen_skin_partition` passes that
  would fix it are gated on mesh **category**: `creature and has_skin`, and
  worn armor (`_in_armor_dir`). Anything else that happens to be skinned kept
  its Oblivion strip partition — self-skinned clutter (rope, chain, banner,
  hanging bucket), effect meshes, creature parts outside the creature path.
- **Not one file:** a sweep of 500 converted meshes found **93 strip-format
  partitions across 6+ unrelated meshes** (`roothavok05`, `parachuteclosed`,
  `refractioneffect`, `thornelemental`, `sloftarantulafuzzyredknee`,
  `handrberskir`).
- **Fix:** a category-independent safety net after all the category passes —
  regenerate any partition still reporting `num_strips > 0`. Existing passes
  are untouched (they already emit triangles). Counter:
  `stats['skin_partitions_destripified']`.
- **Diagnostic:** `pb.num_strips > 0` / `len(pb.triangles) == 0` on any
  `skin_partition_block`. Checking the shape's `NiTriShapeData` is NOT enough —
  it looks perfectly healthy while the partition is broken.

## Dangling back-references to `old_root` after NiNode→BSFadeNode (skin case SOLVED 2026-08-01)
The root swap in `nif_converter.py` builds a **new** BSFadeNode and drops the
original NiNode out of the tree. Every block still pointing at `old_root` is
then unreachable, and PyFFI silently writes that link as null (-1). The fixup
block after the swap must retarget *all* of them — it already handled
`NiTimeController.target`, `.extra_targets`, and `NiDefaultAVObjectPalette`, but
**not `NiSkinInstance.skeleton_root`**.

- **Symptom:** none observed in-game on its own. This was initially blamed for
  the ropebucket red triangle; fixing it changed nothing, and the real cause was
  the strip-format skin partition above. It is still a genuine broken link
  (source `skeleton_root = RopeBucket01`, output `None`) and worth fixing, but
  do not treat a dangling `skeleton_root` as an explanation for a red triangle.
- **Who it hits:** self-skinned *clutter*, i.e. a mesh whose bones live in its
  own tree rather than on the character skeleton — rope, chain, banner, hanging
  bucket. Found on `dungeons\chargen\ropebucket01.nif`, whose two `BucketRope:*`
  shapes are skinned to the internal `c_BucketBone00..07` chain with
  `skeleton_root` = the root node. Worn armor is immune because it keeps a
  NiNode root (no swap happens).
- **Detection:** dump source vs output and compare — source has
  `skeleton_root = RopeBucket01`, broken output has `None`.
- Note the shapes here are already `NiTriShape` in the source, so
  `get_interchangeable_tri_shape()` is *not* involved. (That method does
  `deepcopy` the skin instance, which would orphan the same links for a skinned
  *NiTriStrips* — no such mesh has been observed yet, but it is the same trap.)

## NIF furniture marker conversion (rewritten 2026-07 — fixed backwards/floating NPCs)
- Oblivion: `BSFurnitureMarker` (NiExtraData) with FurniturePosition using `orientation` (ushort, milliradians), `position_ref_1`/`position_ref_2` (byte, always equal in practice)
- Skyrim: `BSFurnitureMarkerNode` (inherits BSFurnitureMarker) with FurniturePosition using `heading` (float, radians), `animation_type` (ushort: 1=Sit, 2=Sleep, 4=Lean), `entry_properties` (bitflags: front, behind, right, left, up)
- **CRITICAL SEMANTIC DIFFERENCE**: Oblivion positions are ENTRY POINTS — where the NPC stands on the floor ~51-106 units AWAY from the furniture, one marker per approach direction (a single chair has 3-4). Skyrim positions are the actual SIT/SLEEP spots (hip position), one per physical seat. A 1:1 position copy produces N duplicate seats with inconsistent headings (NPCs sit sideways/backwards) at the wrong place.
- **Conversion** (`_convert_furniture_markers` in nif_converter.py): compute a seat candidate per entry, cluster candidates within 20 units, emit ONE Skyrim position per cluster. Verified to reproduce vanilla marker topology exactly (chair→1 pos front|right|left; bench→3 pos; bed→1 sleep pos right|left).
- **Seat candidate**: sit entries stand a FIXED distance from their seat — 51.5 (side refs 11/12) / 55.0 (front/behind refs 13/14) — walk that far along the approach direction (handles curved benches like anviltreebenchseat01; a bench's side entry is 51.5 from the END seat so it clusters correctly). Sleep entry distances vary per bed (67-106), so instead project the geometry-bbox centre onto the approach ray (entries always point across the hip line).
- **Heading** (= direction occupant faces; for sleep = head→feet direction): `heading = orientation/1000 + offset[ref]` where offset = {1: −π/2, 2: +π/2, 3: −π/2, 4: 0, 11: −π/2, 12: +π/2, 13: 0, 14: +π}. 100% consistent across all 48 marker-bearing Oblivion.esm furniture NIFs. The old blanket `+π` rule was only right for ref 14. Ref semantics: 1/11 = occupant's left side, 2/12 = right side, 13 = behind occupant (step over / sit without turning), 14 = in front (approach facing seat, turn, sit), 3 = mat side entry, 4 = mat head-end crawl entry (3/4 verified against sleepingmat01's pillow bump; pillow end = taller z bump, calibrated on Skyrim bedroll01 where the marker proves head=+Y).
- **Z**: entry markers stand ON THE FLOOR in mesh coords (Oblivion furniture origins are at mid-height, so entry z is negative). Skyrim marker z = entry_z + 34.0 (sit) or + 37.0931 (sleep) — the vanilla floor-relative hip heights. All 24 Oblivion bed mattress surfaces lie 36.5-42 above their entry z, so floor+37.09 lands on the mattress. The old `z = -src.z` rule floated NPCs ~34 units in the air (it looked right on chairs only because origin-at-mid-height makes |−z| ≈ seat height by coincidence).
- **Entry flags** are relative to the final heading: flag = side of the seat the entry point lies on (front if (entry−seat)·facing > 0.5, etc.) — NOT a fixed per-ref mapping.
- Oblivion double beds get ONE centered sleep pos (entries converge mid-bed; single and double beds have identical entry spacing ~±91-94 so they cannot be distinguished, and Oblivion's fixed-travel sleep anim landed center-ish too).
- Marker-bearing NIFs live outside meshes/furniture too: clutter/castleinterior (castle beds/thrones), architecture (cathedral pews, tents/sleepingmat, ships/sibed, anvil tree bench), dungeons (benches, thrones, sacrifice altar), oblivion/architecture/citadel. Find them with a binary grep for the ASCII string `BSFurnitureMarker` (block type names are plaintext in NIF headers).
- BSFurnitureMarker lives in root NiNode's extra_data_list. During NiNode→BSFadeNode conversion, it must be explicitly converted and transferred (bulk extra_data_list copy breaks animated objects). Marker offsets are model-space and stay valid under the root-rotation wrap pass.
- **FURN record linkage (CRITICAL)**: TES5 FURN `MNAM` bits 0-23 enable NIF marker POSITION index 0-23. TES4 MNAM bits indexed the Oblivion NIF's ENTRY list — passing the bitmask through after seat clustering leaves dangling bits and the engine seats NPCs at garbage positions FAR from the mesh. The shared algorithm lives in `asset_convert/furniture_markers.py`; `tes5_import` (items.py `load_furniture_seats`, called in import Phase 0e) recomputes the same seat list from the source NIF and writes MNAM=(1<<n_seats)−1 + preserved high bits (0x40000000 sit-type / 0x80000000 bed-type, same in both games; beds add 0x08000000 MustExitToTalk like all vanilla beds) + WBDT(0,-1) + one FNPR per seat.
- **Oblivion entry-restriction variants**: many TES4 FURN records share one NIF and enable different entry-marker subsets (SEChair01F/R/L, 19 LCBench01* variants like `Fall`=front row only, `RL`=ends only). Conversion carries this into per-seat FNPR entry flags: only the entry directions whose TES4 entry bit was enabled are allowed (seats with no enabled entries fall back to all their entries). Verified vs vanilla: converted bench = 0x40000007 + 3×FNPR like CommonBench01; converted bed = 0x88000001 + FNPR 0x000C0002 byte-identical to CommonBed01; LCBed02L keeps right-entry-only (FNPR 0x00040002).
- FURN models whose NIF is missing from the export (SI furniture, palace thrones) get a conservative fallback: MNAM bit 0 + high flags, FNPR all entries. NIFs with NO markers get MNAM high flags only (no active positions — never enable bits beyond the NIF's position count).

## Activation pick region (HUD rollover "too big" on clutter) — SOLVED 2026-07
- Skyrim's crosshair activation is a PRECISE raycast against the Havok collision shape (user-verified: vanilla prompts appear only when the cursor is exactly on the mesh; `fActivatePickRadius` INI had no effect). An earlier theory blaming engine INI slop was WRONG.
- Root cause: Oblivion clutter ships ONE bhkConvexVerticesShape hull per object. A convex hull FILLS EVERY CONCAVITY — a goblet's hull fills the waist around the thin stem (collision radius 2.7-3.0 vs visual 1.6), a pitcher's hull fills the entire handle gap (y ±4.7 where the visual handle is ±0.53). AABB comparisons hide this (hull AABB == visual AABB exactly); compare CROSS-SECTIONS at concave features instead. Vanilla authors compound shapes instead (glazedgoblet01 = bhkListShape of cup box + stem box).
- Fix (`_decompose_clutter_hull` in collision.py): dynamic (mass>0) plain-bhkRigidBody single-convex-hull clutter is rebuilt as a bhkListShape of per-piece hulls: recursive binary split of the VISUAL vertices along the axis-aligned cut minimising total hull volume (scipy ConvexHull; accept cut if ≥10% volume gain, depth ≤3 → ≤8 pieces). Each half extends past the first vertex ring on the far side of the cut, or sparse vertex rows leave unfilled collision bands between pieces. Piece planes = scipy hull equations deduped, w = d − radius (vanilla stores planes pushed out by the convex radius). bhkRigidBodyT excluded (shape frame ≠ node frame). Frame sanity check vs the original hull AABB bails out when collision was authored differently from visuals. Result: goblet stem 2.7-3.0 → 1.8-2.4 (tighter than vanilla's box corners), pitcher handle strip y ±0.6.
- **Havok material conversion (was missing entirely)**: Oblivion materials are a 0-31 enum; Skyrim materials are CRC32 hashes (SkyrimHavokMaterial, values in references/nif 0.10.0.0.xml). `_convert_materials()` in collision.py maps them (`_OB_TO_SK_MATERIAL`); unmapped values leave the engine with an unknown material (no impact sounds/decals/stair-walk flag). **PyFFI trap: EnumBase.set_value() only LOGS "invalid enum value" and returns** for values outside its old enum list — must write `item._value` directly. PyFFI instantiates ONE material item per read context (typed OblivionHavokMaterial even when reading Skyrim CRC files — repr shows `<INVALID (...)>`, harmless; read/write via `_get_havok_material`/`_set_havok_material`).
- **Inertia scale regression**: collision.py had drifted to `_INERTIA_SCALE = 0.1` with a bogus justification comment ("Havok normalises by body scale internally"). Correct value is `_HAVOK_SCALE**2 = 0.01` (inertia ∝ mass·length², lengths scale 0.1) — verified: vanilla silverjug01 stores I_x=0.031 = m(3r²+h²)/12 exactly in SI/Havok metres. The 0.1 scale left inertia ~10× too large → sluggish rotation / "too much inertia" feel when grabbing or knocking clutter. (tests/test_asset_convert.py `_INERTIA_SCALE = 0.1` still asserts the old value and needs updating.)
- Note on masses: Oblivion authored masses differ per-item from Skyrim equivalents with no consistent ratio (OB silver pitcher 8.0 vs vanilla silver jug 0.8, but OB ceramic goblet 0.4 ≈ vanilla goblets 0.5-0.8) — masses stay unconverted.
- tes4/tes5_nif_analyzer print `BoundSphere` (NiTriShapeData center/radius) and bhkConvexVerticesShape vertex `extents` for this kind of investigation.

## Grass (GRAS) conversion — record invariants + shader profile (2026-07-09)
- Skyrim grass spawns purely from LAND texture layers whose LTEX has GNAM — no REFR/region records involved. Chain verified in output ESM: GRAS DATA layout byte-matches the xEdit TES5 def; LTEX GNAM links and subrecord order (EDID/TNAM/MNAM/HNAM/SNAM/GNAM) match vanilla; all 419,902 LAND BTXT/ATXT layers resolve to real LTEX (~120k grass-linked); WRLD DATA bit 0x80 = "No Grass" must stay clear.
- **GRAS record invariants (the working-example pattern)**: every working GRAS record — vanilla Skyrim.esm, USSEP, Beyond Skyrim's BSHeartland.esm, Skyrim Extended Cut, Legacy Orsinium (all dumpable from the install with tools/tes5_esm_reader.py) — has (1) **OBND all ZEROS**, never computed mesh bounds, (2) **MODT present** (BSHeartland proves a 12-byte version-2 stub `02000000 00000000 00000000` suffices), and (3) **the model under `meshes\landscape\grass\`** — 45/45 surveyed MODL paths contain `landscape\grass\`; nothing outside it is known to work (same kind of hardcoded naming contract as the `NPC Root [Root]` bone). convert_GRAS builds the record manually to honor all three (MODL via `grass_profile.grass_model_dest()` → `landscape\grass\tes4_<basename>.nif`, flattened — TES4 grass basenames are collision-free), and `grass_profile.run()` copies each converted grass NIF there (sources under tes4\ stay for FLOR/STAT sharing). BSHeartland also shows GRAS DATA density up to 80 and TES4-style values are fine; LTEX INAM (SSE "Is Snow" flag) is optional.
- **Grass NIFs with Vertex_Alpha + low VC alpha look invisible/ghosted in NifSkope — that is normal and matches vanilla** (vanilla grass VC alpha avg ~0.15; NifSkope multiplies it into the alpha test, the in-game grass shader uses it as wind weight instead). Don't diagnose grass visibility in NifSkope. Alpha-test thresholds are clamped to the vanilla grass envelope (≤100; Oblivion used up to 128).
- **GRAS DATA Density/PositionRange must be clamped to the working envelope** (Density ≤80, PositionRange ≤32 — vanilla uses Density 3-6/PosRange ≤32, BSHeartland up to Density 80). TES4 values (Density up to 100, PosRange up to 90) were tuned for Oblivion's 4x-coarser placement grid (iMinGrassSize 80 vs Skyrim 20); passed through raw they over-instance the grass planter and CTD **with no crash log** on cells where dense grass textures cover whole quadrants (found via `python tools/cell_grass.py export/Oblivion.esm --wrld Tamriel --cell X,Y` — lists the grass types each cell can spawn; the two density-100 types were the outliers shared by the crashing cells).
- **Grass CTD root cause #2 — Oblivion meshes with NO triangle data (SOLVED 2026-07-10)**: several vanilla Oblivion grass meshes (GroundCoverMediumGrass01/LongGrass01, GroundCoverPineappleWeed*, GroundCoverWildPlant*, ms14longgrass01) ship NiTriShapeData with `has_triangles=False` — Num Triangles is set but the index array is ABSENT (only `Oblivion - Meshes.bsa` has them; no intact alternates exist). Oblivion's grass renderer tolerated it; Skyrim's planter dereferences the missing data → **region-specific CTD with no log** (Heartland/Cheydinhal cells). Several others (JMMediumGrass*, brmediumgrassyellow01, groundcoverfern01, oblivionmoldroots01) carry legacy vertex **match groups** (no vanilla Skyrim mesh has any) → same crash in Bruma cells. Fix: `asset_convert/tri_reconstruct.py` — blade triangles are reconstructed from the 3-UV-role pattern (base-left/base-right/tip; tip role = highest avg z; each blade pairs a tip with the base pair whose midpoint it tops; winding from stored normals), and match groups are cleared on every converted shape. Both run inside `_convert_strips_or_shape`, so FLOR/STAT meshes sharing these sources heal on full re-runs. Diagnosis method: `tools/cell_grass.py` per-cell grass lists × working-vs-crashing region diff → the defective meshes were exactly the types unique to crashing regions. A mesh with `has_triangles=False` also renders as NOTHING in NifSkope (that was the real cause of the "invisible in NifSkope" report, not vertex alpha).
- **Grass CTD root cause #3 — intermediate NiNode wrapper on rotated sources (SOLVED 2026-07-10)**: Skyrim's grass instancer (`AddCellGrassTask` → `BSMultiStreamInstanceTriShape`) requires grass geometry as a **direct child of the BSFadeNode root** — every working grass NIF (vanilla + converted `gcgorsegrass`/`gclonggrass`) is flat `BSFadeNode → NiTriShape`. But the generic converter's Pass-6c wraps geometry in an inner NiNode whenever the **source root carries a non-identity rotation** (it bakes the rotation into a child NiNode because Skyrim honors child-NiNode rotation but ignores BSFadeNode root rotation for statics). The grass path never traverses that inner node → dereferences garbage (`rdi=0x0001000100010001`, `movzx ecx,[rdi+0x32]`) → **CTD** on any cell spawning the type. Hit TES4 **BWCattail01/02/03** (their source roots are rotated; the crash object was `BWCattail02` with a nested `NiNode "BWCattail02"`). Fix: `grass_profile._flatten_grass_root()` (runs inside `apply_grass_profile`) bakes each plain NiNode wrapper's transform into its geometry's verts+normals and re-parents the geometry onto the root, dropping the empty NiNode — world-space geometry preserved (verified: Z height extent unchanged). Only collapses bare NiNode wrappers holding pure geometry (no collision/controller/extra-data). Diagnosis: crash log named `tes4_bwcattail02.nif` + `BSFadeNode`/`NiNode` both named "BWCattail02"; block dump vs a working converted grass NIF showed the extra nesting; source root rotation identity=False (vs gcgorsegrass identity=True, which stayed flat).
- Known remaining grass-NIF oddities (not crash-related, grass renders): most grass tex[1] `_n.dds` normal maps don't exist (vanilla grass points tex[1] at `textures\effects\HighFrequencyNormals.dds` or the literal string `NOR`); bwcattail03 references BWCatTail02.dds which is absent from the extracted BSAs.
- **BSHeartland.esm is the best reference for "custom worldspace + custom grass records that provably work"** — compare against it before vanilla when a worldspace-scoped feature is dead.
- Grass NIFs additionally get the vanilla grass shader profile (`asset_convert/grass_profile.py`, run by `asset_pipeline.convert_meshes`; models identified from the export's `GRAS.txt`): NiAlphaProperty alpha-test only (blend bit clear), SLSF1 OwnEmit+VertexAlpha set / Specular clear (Specular + glossiness 0 = pow(NdotH,0)=1 white-out), emissive ×1.0, gloss 80, spec white/1.0, lighting effects 0.3/2.0, clamp 0 — matching every vanilla LE grass mesh in `references/Skyrim Meshes/meshes/landscape/grass/`. Geometry/UVs/vertex-color alpha (wind weight) preserved.
- 8 Shivering Isles grass models (Plants\Dementia\*, Plants\Mania\*) are absent from the extracted BSAs — their GRAS records exist but have no mesh until SI assets are extracted.

## Landscape normal maps: DXT1 = shiny ground (2026-07-09)
- Skyrim's landscape shader reads the normal map ALPHA channel as the specular mask. Oblivion's terrain shader never used it, so most Oblivion landscape `*_n.dds` are DXT1 (no alpha) → sampled alpha = 1.0 → full-strength specular over the whole terrain (user-visible "very shiny ground"). Oblivion normals that are already DXT5 carry a real mask (avg ~77/255) and are correct as-is.
- Fix: `asset_convert/landscape_normals.py` (pipeline step after the texture copy, so re-copies can't resurrect DXT1) re-containers DXT1 → DXT5 with constant dark alpha 32/255. DXT1 and DXT5 share the 8-byte color block format, so RGB is preserved losslessly; DXT1 3-color blocks (c0<=c1, ~0.05%) get endpoints swapped + indices 0↔1 remapped since DXT5 color blocks are always 4-color mode.
- Related: LTEX SNAM is a Phong exponent (never write 0 — see convert_LTEX comment); the alpha mask is what actually controls specular *amount*.

## NIF analyzer tools
- `python tools/tes4_nif_analyzer.py <nif_or_dir> [--outdir temp/analysis] [--max N]` — Dumps NIF structure to human-readable text (includes furniture marker positions/refs/orientations)
- `python tools/tes4_nif_analyzer.py <nif_or_dir> --bbox` — Prints world-space geometry bounding boxes (per-block + total, all transforms applied) to stdout; use to find mesh origins, floor levels, pillow bumps, etc.
- `tools/tes5_nif_analyzer.py` re-exports from tes4 version (PyFFI handles both versions)
- Useful for diff-based comparison between Oblivion, converted, and Skyrim reference NIFs

## Terrain/LOD/LAND-adjacent asset notes

### WRLD World Bounds (NAM0/NAM9)
- NAM0 (bounds min) and NAM9 (bounds max) store X, Y as raw float world-unit values (same scale as TES4)
- xEdit **displays** them scaled by `1/4096` (cell units) but the raw file value is NOT divided
- TES4 exports `NAM0.MinX=-262144.0` → write exactly -262144.0 to TES5 file (do NOT divide by 4096)
- If divided: NAM0=-64.0 looks like valid cell coords but is actually 64 times smaller than needed → SSELodGen won't generate world map correctly

### 🔴 MTTC targets and sequence blocks must be dropped TOGETHER (2026-08-10)

Crashes `crash-2026-08-10-00-42-35` / `-00-51-26` / `-00-53-49`:
`EXCEPTION_ACCESS_VIOLATION` at `VCRUNTIME140.dll+0019BCF`,
`movdqu xmm2, [rax]` with `rax=0`. This one names its own cause in the object
list: `NiControllerSequence`, `BGSGamebryoSequenceGenerator`, behavior graph
`spiddalcloudplant`, `BSFadeNode "spiddalplant"
("tes4\Oblivion\Plants\SpiddalCloudPlant.NIF")`. It fires **when the plant
animates** (walking up to a Spiddal Stick as it spews its cloud), not on cell
load.

`NiMultiTargetTransformController.extra_targets` is a **POSITIONAL** list: the
engine pairs slot N with the `NiControllerSequence` controlled-block that
drives it. Break the pairing either way and the slot resolves to a null
interpolator, which the sequence generator dereferences.

**Both directions were shipped and both crashed identically** — worth
recording, because each looked correct in isolation:

1. The original code dropped any controlled block whose node name equalled the
   **root node's** name, leaving `num_extra_targets` unchanged. Oblivion
   routinely names the root and its animated node the same thing
   (`spiddalcloudplant.nif`'s root IS `spiddalplant`, also extra-target #1),
   so the target lost its driver. Source 10 blocks / 3 targets → ours 9 / 3.
2. "Keep the root-named block so the target matches" — this restored 10/3 and
   **still crashed**, because a root-targeting entry is itself illegal.

Census of 141 sequences across 43 animated vanilla meshes, and **both numbers
are zero**:

| invariant | vanilla |
|---|---|
| controlled blocks targeting their own root | **0** |
| MTTC extra targets with no controlled block | **0** |

Vanilla satisfies both by never listing the root as a target at all. So the
correct fix is to drop the block **and** remove that node from every MTTC
target list, decrementing `num_extra_targets` with it (`_drop_mttc_target`).
Result on the crashing mesh: 9 blocks / **2** targets, root absent, every
target driven.

Blast radius: **413 meshes**, including `obcloud01.nif` and
`oblivionsmokeemitter01.nif` — the two REFRs that appeared in the earlier
`SkyrimSE+050E6AD` logs, where a separate LOD fault happened to crash first.
Audit with `tools/mttc_target_check.py`; guarded by
`tests/test_asset_convert.py::TestMTTCTargetsStayInSyncWithControlledBlocks`,
which drives the REAL converter (a hand-built MTTC tests pyffi's reference
arrays, not the converter).

### 🔴 A graph-bound mesh must ship NO empty text keys (2026-08-10)

Crashes `crash-2026-08-10-01-08-13` / `-01-39-02` / `-01-41-07` — the Spiddal
Stick and Harrada Root CTDs that survived both the MTTC fix (above) and every
Rest-state theory.  Same signature as the MTTC family (`movdqu xmm2,[rax]`,
rax=0, VCRUNTIME140, under `BGSGamebryoSequenceGenerator`) but a different
null.

**Mechanism, from disassembly** (`tools/address_lib.py --log` → GOG RVA
`0x505130`, Address Library ID 32774; crash site is the call returning to
`+0x1B0`): on state activation the generator walks the activated
`NiControllerSequence`'s `NiTextKeyExtraData` translating keys into behavior
events.  Each value is first matched WHOLE against the project's registered
event table (hash map on `BShkbHkxDB::ProjectDBData+0xC8`); on a miss the
engine calls `strchr(value, '.')` to split an `Event.Payload` key
(`mov edx, 0x2e` right before the crashing call — and the crash log's
`R9=0x2E2E` is strchr's broadcast needle).  The strchr runs on the RAW string
pointer: an **empty NiString loads as a NULL BSFixedString**, and the read of
address 0 is the CTD.  So the crash fires the moment the object first
animates — the Spiddal Stick spewing its cloud as the player walks up.

The sources really do ship empty keys — Oblivion authored them freely and its
engine ignored them: `spiddalcloudplant.nif`'s Forward has `t=0.1 ''`,
`harradauprightattack.nif` has SEVEN of them.

**Vanilla census draws the exact legality line:**

| where | empty text keys |
|---|---|
| graph-carrying meshes (animobjects, traps, furniture) | **0** |
| graph-less meshes | 2 (`impjaildoor01`, `ruinscanopicjar02` — plain Open/Close) |
| keys with trailing whitespace (`'Sound: X\r\n'`) | 107 in dungeons alone — **legal, do not trim** |

Empty keys are tolerated on the graph-less path (vanilla ships them and those
doors work), and lethal on the graph path (vanilla ships none).  So the fix
(`_strip_empty_text_keys`) drops whitespace-only keys **only for meshes that
get an animobject graph** — converted graph-less doors stay byte-identical to
what already works.  Verified: `idsecretwall01` and `doceilingcollapse01`
(the ImperialDungeon01 hidden door and falling rubble) re-convert
byte-identical; the plants lose exactly their empty keys and keep
`start`/`end`/`sound:` verbatim, including the vanilla-legal trailing `\r\n`.

Audit: `tools/gamebryo_seq_check.py` (check 3).  Guarded by
`tests/test_asset_convert.py::TestGraphMeshesShipNoEmptyTextKeys`, driven
through the real converter on both crashing meshes.

### 🔴 LODSettings must COVER the terrain, or the worldspace CTDs on entry (2026-08-10)

Crashes `crash-2026-08-09-23-15-19` through `crash-2026-08-10-00-16-34`, all
byte-identical: `EXCEPTION_ACCESS_VIOLATION` at `SkyrimSE.exe+050E6AD`,
`mov rbx, [rax+rcx*8]` with `rax=0`, on a `BSJobs::JobThread`, in
`Plane of Oblivion`. **Reproducible with `coc OblivionMQKvatchEntrance`** —
which is what proved it is the worldspace's own load path, not the Oblivion
gate, the transition, or any script.

The engine builds its terrain-LOD quadtree from `LODSettings/<WRLD>.lod`: root
at SW, `size` cells across, recursively subdivided into 4 children
(`0x4d1020` recurses over `[node+0x30]` with exactly 4 children, stride 0x50).
A `.btr` tile outside that square has **no node**, and the per-frame walk
indexes the node array with **no bounds check**.

Cause: `write_lod_settings` took its extents from `WRLD.MNAM` alone, and **57
of 84 TES4 worldspaces author no usable MNAM** — so `sw == ne == 0` arrived,
and the old `size = 1 << ceil(log2(ne - sw))` plus `eff_sw = -(size // 2)`
produced a **1×1 grid** (`SWx=0 SWy=1`) for every converted worldspace, while
LODGen still emitted tiles out to (-32,-32).

Fix, in two parts:
- Extents are measured from the **CELLS** (always carry XCLC), unioned with
  MNAM only when MNAM is populated.
- `size` grows from 4 until the square covers `[sw, ne)`; **SW is the literal
  terrain corner, NOT snapped or centred**, and `maxLOD` tracks `size`
  (capped at 32) rather than being hardcoded to 32.

Ground truth — vanilla `.lod` files extracted from `Skyrim - Meshes0.bsa`
(layout `<hhIII` = SWx i16, SWy i16, size u32, minLOD u32, maxLOD u32):

| worldspace | SW | size | minLOD | maxLOD |
|---|---|---|---|---|
| japhetsfollyworld | (-9, -6) | 16 | 4 | **16** |
| dlc01falmervalley | (-16, -13) | 32 | 4 | 32 |
| skuldafnworld | (0, -21) | 64 | 4 | 32 |

The new formula reproduces the first two **exactly** from their cell extents,
which is what confirms it rather than merely being self-consistent. Note SW is
unaligned in all three — an earlier attempt that snapped SW down to a multiple
of `size` never converges for a span crossing the origin (the gap grows as
fast as the size). Guarded by
`tests/test_asset_convert.py::TestLODSettingsCoversTheTerrain`.

### Terrain LOD (SSELodGen) — data chain
- LAND BTXT/ATXT subrecords contain direct LTEX FormIDs (NOT indices into VTEX array)
- SSELodGen uses: BTXT.Texture(LTEX) → LTEX.TNAM(TXST) → TXST.TX00(path) → Data\Textures\{path}
- VTEX subrecord is a supplementary lookup array; most Oblivion LAND records don't have it (29/31823)
  - TES5 LAND VTEX format: packed array of uint32 LTEX FormIDs, one subrecord total (not per-quadrant)
  - Null slots (zero FormID) are valid and common — NOT a bug
- Landscape textures extracted from BSA are DXT1 BC1 512x512 — fully supported by SSELodGen
- If terrain LOD appears purple after correct data install: ensure OLD LOD tiles are deleted before regenerating

### Distant LOD generation (one-click, rebuilt 2026-07-06) — `convert.py` Phase 8 `phase_lod`
Two pieces, both native, both re-enabled in the pipeline (`generate_lod` + `generate_terrain_lod`).

**Terrain LOD** (`asset_convert/terrain_lod.py` + `terrain_lod_textures.py`): per-tile `.btr` heightmap NIF + composited diffuse `.dds` + heightmap-derived BC5 normal `.dds`, LOD levels 4/8/16/32. TES4Tamriel = 1301 tiles.
- **The old diffuse was the bug**: it upscaled raw LAND VCLR vertex colors → a blurry color grid (why distant terrain looked wrong). FIX: `terrain_lod_textures.composite_cell()` composites the REAL landscape textures — resolve LTEX FormID→diffuse via `build_ltex_texture_map` (LAND BTXT/ATXT → LTEX.TNAM → TXST.TX00 = `tes4\landscape\*.dds`), then per quadrant blend base + alpha layers using the ATXT/VTXT opacity grid (17×17, pos=row*17+col, sorted by ATXT layer index), ×VCLR shading at 0.4 strength (full x2 caused hard cell seams). Landscape UV repeats every 2 cells.
- **Compositor orientation contract (fixed 2026-07-09 — the "large single color areas" bug was three separate defects):**
  1. **Quadrants with no BTXT base layer** (22.6% of Tamriel quadrants, whole sea floor) rendered flat grey-128. The engine's default for unpainted land is `Landscape\Default.dds` → `DEFAULT_LAND_TEXTURE = tes4\landscape\default.dds`. Cells with NO LAND record now also composite (default texture) instead of a flat fill.
  2. **V ran the wrong way**: the diffuse tile is written image-row-0 = NORTH, so world V must DECREASE as the image row grows. Sampling with ascending V mirrored every quadrant and broke ground-texture continuity at every quadrant boundary (horizontal banding every half cell). Same flip applies to VTXT opacity grids and VCLR (LAND row 0 = SOUTH → `np.flipud` to image space), and to the heightmap-derived normal map (`_heightmap_normal_rgb` flips + negates the row gradient so the `_n.dds` matches the diffuse orientation — it was N/S-mirrored vs the diffuse before).
  3. **No underwater murk**: vanilla/xLODGen LOD diffuse bakes submerged terrain toward a flat murky colour; without it the sea floor reads as bright land. `composite_cell(heights=, water_height=)` blends toward `MURK_COLOR` by depth (`MURK_FULL_DEPTH=512`, cap `MURK_MAX=0.9`).
  - Ground truth for row-0=north: xLODGen's own `tamriel.32.0.32.dds` (northern Sea of Ghosts at the TOP).
- `.btr` structure = `BSMultiBoundNode` "chunk" → child[0] `NiTriShape` "land" (scale=level, local 0..4096 verts, shader type 18 LODLandscapeNoise, no normals/vcol), child[1] optional WATER node → `BSMultiBound`/`BSMultiBoundAABB`. Loads in-game; AABB magnitude matches vanilla LOD4. (xLODGen source only READS .btr for object-face culling — terrain .btr generation is entirely ours.)
- **Land UVs are REQUIRED and meaningful** (fixed 2026-07-09): vanilla maps the tile texture across the tile with `u = x/4096`, `v = 1 − y/4096` (v=0 = NORTH edge = DDS row 0). "UVs are irrelevant for terrain .btr" was only true of how xLODGen *reads* them — the ENGINE samples them. All-zero UVs make every triangle sample one texel → each tile renders as a single flat colour → the in-game/world-map "hard-edged checkerboard, one colour per tile" symptom. Water shape has NO UVs (num_uv_sets=0), matching vanilla.
- **LOD water (added 2026-07-09, vanilla-exact)**: child[1] = `BSMultiBoundNode` named `WATER` (scale 1) → one shape with an independent flat quad per water cell (4 verts/2 tris each, cell-local size 4096/level, Z = water height / level, NO shader/UV/normals — the engine textures it from WRLD NAM3). LOD4 uses `BSSegmentedTriShape` with EXACTLY 16 segments (fixed 4×4 grid, column-major sx*4+sy, so the engine can hide quads over loaded cells); LOD8/16/32 use plain `NiTriShape`. Segment binary layout (nif.xml `BSGeometrySegmentData`): `flags:byte=0, start_index:uint (tri-POINTS, 0 when segment empty), num_primitives:uint`; PyFFI's `BSSegment` fields are misaligned over the same 9 bytes — write `internal_index = start<<8` and `flags.bsseg_water = 1` (== num_prims 2 << 8). WATER AABB: XY = quad bbox in world units rel. tile origin; Z spans [min water height, max(max height, 0)]. Water cells = CELL HasWater (DATA bit 0x02) AND terrain dips below the cell water height (XCLW override valid only in ±1e9, else WRLD DNAM default).
  - **The old CTD** (BSMultiBoundNode "Water" → null deref): the engine's LOD-water path derefs the worldspace's WATR via **WRLD NAM3** — the fix is NOT to avoid the node, it's to write NAM2/NAM3 = Skyrim.esm DefaultWater (0x18) + NAM4 (LOD water height, 0 for Oblivion) in `convert_WRLD`. Also: TES4 CELL XCLW `-2147483648.0` = "use default" sentinel — must be OMITTED on conversion, not written as a literal height.
- Normal map derived from the heightmap gradient (`_heightmap_normal_rgb` + real BC5 via `_encode_bc4_block`), replacing the old flat normal so distant terrain is lit.
- Debug single tiles without a full run: `python -m tools.terrain_lod_tile_debug --tiles 4,0,0 16,0,0 --png-dir temp` (rebuilds specific tiles in-process, reports water quads, dumps diffuse PNG). `python -m tools.terrain_lod_tex_probe [--cell X Y]` audits LTEX→TXST→dds resolution and per-cell layers.
- Validate with `python tools/terrain_lod_render.py --esm output/oblivion.esm/oblivion.esm --worldspace TES4Tamriel --cell X Y --radius R` → side-by-side hillshade + composited diffuse (the primary iteration tool; do NOT byte-match vanilla .btr). `tools/lod_nif_inspect.py` dumps .btr/.bto geometry+shader.
- **🔴 A worldspace a MASTER defines is ALWAYS sourced from that master, with the plugin as an OVERLAY — never from the plugin alone, however much terrain it adds** (2026-08-11, Tamriel.esp). `convert.py::_records_esm` used to hand record ownership to whichever file held the *bulk* of the LAND records. That silently inverts for a plugin which **extends** a master's worldspace rather than patching it: Tamriel.esp adds a landmass around Cyrodiil (99,910 LAND vs Oblivion.esm's 31,823), won ownership, and every tile was then built from the plugin ALONE — all of the master's own terrain was missing from the heightmap and `_fill_missing` edge-extended it into flat plateaus. Symptom: tile-sized discontinuities along the vanilla border, **worst at level 32** where one tile spans 32×32 cells (tile `32.0.-32` had 86 of 1024 cells and encoded world Z `4096..16416` instead of `-4576..20152`; tile `32.0.0` had ZERO cells and rendered dead flat). The tell is that the only level-32 tiles that looked *correct* were the two the plugin never regenerated, so the master's copy survived. Record COUNT never distinguished "patches a worldspace" from "extends a worldspace" and must not decide ownership — the overlay path already expresses "master's terrain + this plugin's edits" correctly and is what the DLC/override case always used. Verify with `Parsing LAND records from <master>, <plugin>` in the run log and a LAND count ABOVE the plugin's own (110,095 vs 99,910 here); a single-file parse line means the bug is back.

**Object LOD + tree billboards** (`asset_convert/lod_gen.py` via `external/lodgen/LODGenx64.exe`): STAT/etc. flagged `0x8000` (Distant LOD)/`0x10000000` (World Map) by size in the importer get baked into `.bto`. TREE refs render as **crossed-quad billboard cards** via LODGen's FlatTextures mechanism (`_tree_billboard` points the LOD "model" at `tes4\trees\billboards\<sptstem>.dds` — Oblivion ships 118 billboard renders; `_write_flat_textures` emits the descriptor + a normals file so cards are lit + `_ensure_white_dds`). 91 flat textures, 716/734 LOD4 .bto contain tree billboards. `.btt`/`.lst` vanilla tree-LOD format was deliberately NOT reverse-engineered (risky, unvalidatable) — flat cards in .bto is the reliable Skyblivion path.
- **GOTCHA**: `LODGenx64.exe` runs with cwd=external/lodgen/ → `PathData=` MUST be absolute (`Path(output_dir).resolve()`) or it fails its Data-dir check (exit -1, log "No Data directory"). LODGen's "Oh crap N = N = ..." stderr spam is a harmless degenerate-triangle notice, not an error.
- **GOTCHA — a geometry-rooted NIF kills the ENTIRE worldspace's object LOD** (found 2026-07-27, Morrowind_ob.esm): `LODGenx64` casts every LOD mesh's root block to `NiNode` unchecked, so a root that is a bare `NiTriShape`/`NiTriStrips` throws `System.InvalidCastException: Unable to cast … NiTriShape to … NiNode` **on a worker thread, unhandled** → the process dies, writes NO log (the stale log is the previous worldspace's, so it looks like LOD "just didn't run"), and the worldspace ends up with a single junk `.bto`. Two 4-triangle `bcscum02/03.nif` scum patches cost Morrowind_ob all 75,316 of its LOD references. Two independent guards now exist:
  1. `nif_converter` wraps a geometry root in a `NiNode` before the usual `NiNode→BSFadeNode` step (vanilla census: 400/400 sampled Skyrim meshes have a NiNode-derived root — `BSFadeNode` 340, `NiNode` 55, `BSMasterParticleSystem` 2, `BSLeafAnimNode` 3; **zero** geometry roots, so this is invalid for Skyrim regardless of LODGen).
  2. `lod_gen._lod_mesh_is_safe()` screens every listed mesh's root block and drops (with a warning) anything unreadable or non-NiNode, so one bad mesh can never again cost a whole worldspace.
  Note `lod_far_gen` legitimately refuses to build a `_far.nif` for shapes under `_MIN_SRC_TRIS` (20 tris) — such models simply get no LOD entry; a **stale** `_far.nif` left from an older run is what got listed here.
- **`external/lodgen/LODGenx64.exe` is 3.0.36.0 — it REPLACED the 2.2.0.0 build of the same name** (2026-08-09; the 2.2 exe was deleted, so an old checkout's `LODGenx64.exe` is a different program). The guards above screen the *root block*, but a mesh can still fault LODGen deeper in the walk — and 2.2 handles **no** exceptions anywhere, so any such throw on a ThreadPool worker kills the process and loses every tile not yet written. Nehrim: 28 of 418 tiles, twice in a row, from ONE model (`LeyawiinHouseLower01`, 5 refs game-wide) throwing `ArgumentOutOfRangeException` in `LODApp.TransformShape` → `IterateNodes` → `ParseNif`. 3.0.36.0 catches the same fault **per object**, prints `Error processing <EditorID>`, and finishes the worldspace, so one bad model costs only its own distant LOD (it pops in at load distance).
  - Verified equivalent before switching: on identical input both versions emit the same tile set with the same block structure (39 `BSSegmentedTriShape`/`BSMultiBoundNode` per tile, NIF 20.2.0.7); 3.x only reduces slightly tighter (48,993 vs 49,527 tris on the sampled tile).
  - **3.x's exit code is NOT a success signal**: 0 on a clean run, but nonzero when any object failed *even though the bake completed and every tile was written*. `run_lodgen` therefore judges success by `.bto` files present in `PathOutput`, and logs the skipped EditorIDs as a warning.
  - The mesh is not at fault — repairing `LeyawiinHouseLower01`'s tangent flag and recomputing its missing normals each made 2.2 crash *earlier*. Don't chase the mesh; the bug is inside LODGen.
- **A plugin's LOD bakes its MASTERS' models too**, and those textures were only converted into the master's output (Morrowind_ob places Oblivion architecture in its own worldspace → 117 `.bto`-referenced textures missing). `_fill_missing_lod_textures(master_tex_roots=…)` copies them in, and the normal-map synthesis prefers a master's real `_n.dds` over a flat fallback. Note `master_dirs` is set only when a master owns the WORLDSPACE, so the texture fallback uses its own `master_texture_dirs` argument (always this plugin's masters) — do not conflate the two.
- `_promote_lod_textures` copies .bto-referenced textures to the textures root AND synthesizes missing NORMAL maps (`_a_n.dds` atlas normals + any missing `_n.dds`) from the source normal or a flat normal — LODGen writes atlas diffuse but not atlas normals, so object LOD would render unlit without this.

**World map** uses terrain LOD (all levels incl. LOD32) + object LOD. WRLD NAM0/NAM9 bounds are RAW float world units (NOT /4096) and MNAM map dims must be correct — both verified in the output ESM.

## SpeedTree (.spt) conversion

> 🛑 **GROUND TRUTH IS `Oblivion.exe`, NOT the billboards.** The game statically
> links SpeedTreeRT 4.x with symbols intact — the RNG, the child-placement
> rules, the spline evaluator and the level struct are all decompiled in
> **[speedtree_engine_decomp.md](speedtree_engine_decomp.md)**. Read that
> before changing `spt_generator.py`. The "compare against the billboards"
> advice below is SUPERSEDED for anything structural: the generator was already
> fitted to those images, so an A/B can never reveal a 3D error.
> Known-wrong today: golden-angle azimuth (engine uses `uniform(-180,180)`),
> the `MAX_STEMS_PER_LEVEL` caps (engine uses a smooth per-level density
> falloff), and the crown-shell culls.

**Real procedural, rewritten 2026-07-05 — replaces the asset-matching hack**: `asset_convert/spt_parser.py` + `spt_generator.py` + `spt_converter.py` decode the Oblivion SpeedTreeCAD-4.x `.spt` binary and bake procedural tree geometry directly into a Skyrim NIF that matches the Oblivion tree's silhouette. `python -m asset_convert.spt_converter <trees_src> <nif_dst> [--export-dir <dir>]`. The old `assets/speedtrees/` asset-matching + `_spt_to_skyblivion` is GONE (those were custom Skyblivion creations, not real conversions).

- **`.spt` format** is documented in `references/spttools-master/FORMAT` (GPL sptparser reference). It's a flat stream of `<int32 section_id><payload>` chunks. `spt_parser.py::parse_spt` consumes EVERY section (strict — unknown id raises) into an `SptTree`: levels (trunk=0, branch levels, leaves=last; count in section 1014), shape curves as ASCII "BezierSpline" strings (section 6000-6017), leaf maps (4003 texture / 4005 size / 4004 origin), composite-map UV quads (section 10002), collision primitives (12002/3/4), floor, flares, roughness. Parses 113/113 Oblivion.esm SPTs byte-exact, and 547/547 across every exported plugin (see the newer-CAD note below).
- **BezierSpline** (`spt_parser.BezierSpline`): header `lo hi variance`, then control points `x y tan_u tan_v tan_weight`. `eval(x)` = `lo + curve_y(x)*(hi-lo)` where x∈[0,1] is position along the parent. Constant params have lo==hi. `eval_var` adds the stored ±variance.
- **Scale**: world_units = `stored_value * Size * 10` (`WORLD_SCALE=10`). Verified against the TREE records' billboard heights (`textures/trees/billboards/<stem>.dds` are the ENGINE'S OWN renders — the definitive ground truth; decode them for A/B comparison) — median generated/actual height ratio ≈ 1.0.
- **Generation model** (`spt_generator.build_tree`): recursive stems. Child count per parent = `parent.child_freq * parent.stored_length` (250*0.05=12 on deadbush, 80*0.6=48 on oak). Children spawn in the `[child_first, child_last]` window; SHAPE curves (length/radius/start-angle/gravity/flexibility) evaluate at `x_rel` = position WITHIN the window (NOT absolute parent position — cottonwood forks its whole fan inside the trunk's [0,0.1] window). Start angle = degrees from parent axis. Azimuth = golden-angle spiral + jitter.
- **Gravity semantics** (revised 2026-07-10 after in-game feedback — an earlier "target pitch = 90°−|g−1|·90°" model bent cottonwood's fork limbs DOWN toward horizontal into a wide "wing" the billboard doesn't show): the value sets a bend DIRECTION and RATE — **0<g≤1 bends toward straight UP at rate g** (limbs spread at their start angle near the base then grow back vertical — cottonwood forks g 0.2-0.4, dogwood g 0.25-0.6; every normal trunk stores g=1 = stay vertical), **g>1 wraps over and bends toward the GROUND at rate g−1** (forsythia canes g=3 flop; willow branches store 2..4), g=0 = no influence (redwood, Camoran-paradise trunks — they wander on disturbance alone). The rate is scaled by the FLEXIBILITY value (6002) × GRAVITY PROFILE (6017 — starts at 0.5 at the base, so limbs curve from the moment they fork). Do NOT gate it by the flexibility PROFILE (6003): that ramp is 0 at the base, which left cottonwood's 60°-spread forks lying on their sides for their whole lower half. Willow branches (gravity 2-4, flex 0) HOLD their start angle — the weeping look is the leaf curtains, not the branches.
- **Weeping willow drape**: leaf-LEVEL gravity (section 6001 on the last level) = 90 means leaves hang straight down as long curtains. Modelled as vertical STRANDS of 4 stacked leaf cards reaching ~32% of tree height below each attachment — the only way to reproduce the solid teardrop crown that hangs far below the branches. Ordinary leaves (leaf gravity 0) get one card.
- **Leaf cards**: size = section 4005 * Size (NOT section 4006 — that's the pre-multiplied product but it's STALE in ~15 shrubs, e.g. buckthorn stores 0.08 where 4005*Size=3.6). Two crossed quads. UVs come from the composite-map quad (section 10002) cropping the shipped composite leaf DDS — which is the TREE record's ICON field, resolved at convert time (`_resolve_leaf_tex`).
- **EVERY leaf texture reference must be resolved through `tex_idx` — the SPT names the artist's .tga, not the shipped .dds (fixed 2026-07-27, `dementiatree10` missing leaves)**: `build_tree_nif` had two paths. The composite path (`g['texture'] == '__composite__'`) went through `_resolve_leaf_tex`, which validates `stem in tex_idx` and so can only ever emit a real file. The **per-map else-branch built `LEAF_TEX_DIR + stem + '.dds'` straight from the SPT string with no validation**, happily writing a path to a file that does not exist → leaves render untextured. Measured scope on the converted tree set: **137/143 leaf refs resolved, 6 broken across 4 NIFs** (`dementiatree01/04/10` + `treems14canvasfreesu`) — small, but invisible until you look, because the composite path masks it everywhere else. Two renamings account for all 6, both handled by the shared `_match_tex_stem` (literal stem → trailing composite `c` → leaf-map variant number): `MTreeLeaves02c.tga` → `mtreeleaves02.dds`, and `TreeMS14CanvasLeaves01SU.tga` → `treems14canvasleavessu.dds` (three per-map variants collapse onto ONE shipped atlas). Anchor the variant-number strip on `leaves|needles` and not on "first 2-digit run", or `TreeMS14…` loses its model number instead. Audit it with a scan that checks each converted tree NIF's `textures[0]` against the filesystem — the count should be 0 missing.
- **Newer-CAD trees: the roots twist pair and the 50000 texture-coordinate block (fixed 2026-08-20, Tamriel Landscape Pack)**: 183 of 547 exported SPTs (all TamRes / Tamriel Landscape Pack; **no vanilla Oblivion tree is affected**) were authored by a later SpeedTreeCAD that writes two things the parser did not handle. Both are now supported, and the fix is provably additive — all 364 previously-parsing trees are value-identical and their generated geometry is bit-identical.
  1. **The roots block carries its own 15003/15002 pair, bare and REVERSED.** Outside the roots block the sections come in `15000`-opened `15002,15003` pairs, one per level. Inside `40000..40001` the pair appears once with no opener and in the order `15003,15002`, and it belongs to the roots level. The handler ignored `in_roots`, so `twist_idx` ran one past the last level, `_level_by_seq` returned `None`, and the parse died with `'NoneType' object has no attribute 'random_v_offset'`. Guard it exactly like the `16002` flare and `26002` roughness groups: when `in_roots`, target `tree.roots_level` and **do not advance the counter**.
  2. **Section 50000 is the per-layer texture-coordinate block** (`FORMAT` lines 367-387 + `sptparser.c` case 50004-50018). It holds `50002..50003` groups, each one texture layer: `50004` U tile, `50005` V tile, `50006/50007` U/V absolute, `50008` twist, `50009` random V offset, `50010` V offset, `50011/50012` clamp, `50013-50016` left/right/bottom/top crop, `50017` U offset, `50018` sync-to-diffuse. There are **7 layers per level** (diffuse, detail, normal, height, specular, user1, user2 — the same seven filenames as `70002..70008`), and the block covers **trunk + branch levels + leaves + roots**, so the group count is always **`(num_levels + 1) * 7`** — verified 28/35/42 for 3/4/5 levels across all 183 files, 0 exceptions, matching the FORMAT note "count occurrences of 50002: 28 35 42 49, the difference is 7". Layer 0 is diffuse and duplicates the older per-level `6013-6016`/`15002`/`15003` values. Stored on `SptTree.tex_layers` as `TexLayer`; empty for older-CAD trees, which omit the block entirely. `70000/70001` also had to become markers (their `70002..70008` payloads were already in `_PATTERNS`).
- **Leaf textures: the ICON is the AUTHORED source; the stem-collapse rule is a narrow patch, do NOT widen it** (audited 2026-08-20, `tools/spt_leaf_tex_audit.py`). The SPT's `4003` leaf-map string and the `70002` diffuse filename under `60003` both store the ARTIST'S path (`C:\Hope\IDV\GreyPoplar\TreeGreyPoplarLeavesSU.tga`) — they agree with each other and neither is what shipped. The authored answer is the **TREE record's ICON**, which resolves **literally in 354/354 records** across Oblivion + Nehrim; `_match_tex_stem`'s collapse regex is needed for **0** of them. (The ICON is the *composite* texture and the per-group fallback — a per-map leaf group whose OWN stem ships still wins, e.g. Nehrim's `treecottonwoodsu` groups keep `treecottonwoodleavessu` rather than the record's `Nehrim_Southshrub_SU01`. Verified 2026-08-20: modelling `build_tree_nif`'s exact per-group selection reproduces every shipped NIF's texture set, 146 variants sampled across Oblivion/Nehrim/TamRes, 0 mismatches.) Measured over all 662 tree variants, dropping the collapse rule changes the shipped texture for only **8** — exactly the documented `dementiatree01/04/10`, `treems14canvasfreesu`, and 4 Nehrim stems. So the rule earns its place at that width and nothing more: **widening it (trailing letter `leaves01a`, underscore `leaves_1`) buys only trees that have no TREE record at all** and is pure heuristic — CLAUDE.md "look for the AUTHORED indicator".
- **A resource pack's SPTs have NO TREE records and are never placed.** TamRes/Tamriel Landscape Pack ship 69 SPTs with **0** TREE records (Oblivion: 139/139 have one). Their trees are raw art for other plugins to reference, so an unresolved leaf texture there is invisible in-game. Of the 42 trees that resolve to no leaf texture, only **7 are placed**, and all 7 are legitimately leafless — `shrubdeadbush`, `treekvatchburnt`, `dtree02` (no leaf maps), and Anequina's two cacti, whose leaf maps are literally `FileLoadError.tga`. **No in-game tree is missing foliage art**; do not "fix" this by inventing a name-matching rule.
- **Auditing leaf textures per-plugin is MASTER-BLIND.** `_tex_index` over one plugin's `textures/trees` reported Valenwood as 0/58 resolved and Oblivion as 67/139; merging the masters' tree-texture dirs (as `convert_spt_directory` already does) gives 58/58 and 136/139. Any tree-texture audit must merge master dirs or every dependent plugin looks catastrophically broken.
- **Spline variance is a MAGNITUDE — take `abs()`** (fixed 2026-08-20). `BezierSpline.eval_var` called `rng.uniform(-variance, variance)`, which raises `ValueError: high - low < 0` on a negative stored variance. `reddeliciousappletree.spt` level 3 stores `length` as `lo=-0.03 hi=0.08 variance=-0.007` — 3 occurrences across all 547 trees (one tree × 3 plugin copies). The sign carries no meaning; the flare code already used `abs()` on its `*_var` fields, so this just makes the spline path agree.
- **Composite quad convention (2026-07-10)**: section 10002 quads are 4 corner pairs in order **TC0..TC3 = TR, TL, BL, BR in TGA space where v runs UP** (corner layout per the FORMAT doc's embedded-texcoords dialog). Sampling the shipped DDS requires **v_dds = 1 − v_tga** — the SpeedTreeRT texture flip that ck-cmd enables (`SetTextureFlip(true)` in `references/ck-cmd-master/src/spt/sptconvert.cpp`). Using quad v directly as DDS v swaps vertically-stacked atlas crops: dogwood rendered ONLY flowers because its leaves crop (TGA bottom half) sampled the DDS top half where the flowers live. Leaf-map 4004 origin (card pivot) is in the same TGA v-up space.
- **Blossom rules** (sections 3000/3002 + per-map 4000 flag): maps flagged blossom (dogwood flowers, azalea/hydrangea/rhododendron blooms — 6 SPTs total) are placed only at branch positions x ≥ blossom_distance (3000) and take blossom_weight (3002, e.g. dogwood 0.23) of the eligible picks; ordinary leaf maps share the rest uniformly.
- **Bark UVs** (sections 6013-6016 + 15002/15003, semantics per `references/spttools-master/speedtreecadnotesv4`): U = u_tile repeats around the circumference plus a Twist (15003) spiral along the length; V = v_tile repeats where the **v_abs flag (6016) means the count is exact; otherwise it scales with the stem's STORED length** (dogwood trunk 12 × 0.8 = 9.6 repeats — lands square texels against its U density on every sampled tree); random_v_offset (15002) de-syncs bark phase per stem. The tube seam column must be DUPLICATED (n_az+1 columns, last u = u_tile) — a modulo wrap swept the whole texture backwards across one face of every trunk (the "bad trunk UV" stripe).
- **Branch curvature**: gravity bend is a **linear-rate arc** (constant curvature, `min(gap, step)` per ring toward straight up/down per the gravity semantics above), `GRAVITY_RESPONSE = 8.0` rad capacity at rate×flex = 1. An exponential approach (rotate by gap×frac) slows near the target and left every branch a straight stick. Ring caps must stay near the STORED segment counts (`_RING_CAP` 16/10/6 — oak trunk stores 18, cottonwood limbs 13); crushing them to 3-6 rings flattens every curve. **Disturbance** (6000, variance 15-50° in real trees) is a **ZERO-MEAN snake**: the bend direction oscillates along the stem (sine, random phase, 1.2-2.6 turns) about a slightly-drifting azimuth, so stems curve in-out-in with no net flop. Two failed models: a fresh random direction per ring averages into fuzz (stem reads straight); a persistent one-way azimuth accumulates the variance as NET drift and lays branches over on their sides.
- **Fork limb sizing**: the radius curve over the spawn window IS the limb-size variation (cottonwood forks store 0.03→0.01, a 3× spread; its "trunk" is a 72-unit stub — the level-1 limbs are the visible trunks). Cap child radius only at the parent's radius at the attach point; capping at 0.85×prad flattened the forks to near-identical thickness. Start-angle curves over the window matter the same way (cottonwood: 60°→0° — early limbs spread, later ones vertical).
- **Tube winding**: front faces MUST wind so the geometric normal aligns with the radial vertex normals (>80% positive dot vs vanilla), else the trunk renders visible only from INSIDE (the "U-shaped view inside the tree" bug).
- **NIF structure** = vanilla flora (verified vs `references/Skyrim Meshes/meshes/plants/florasnowberry01.nif` and `landscape/trees/wrtempletree01.nif` Gildergreen): `BSLeafAnimNode` root (flags 14) + `BSXFlags=130` + one bark `NiTriShape` (BSLightingShaderProperty, vertex colors) + leaf `NiTriShape`s (composite texture, `NiAlphaProperty` flags 0x92EC thr 128, shader SLSF2_Tree_Anim + Double_Sided + Vertex_Colors, SLSF1_Vertex_Alpha). ALWAYS set `uv_scale=(1,1)` on PyFFI-created shaders (defaults to (0,0)=invisible).
- **Collision = EXACT trunk mesh** (not a fat capsule): the generator collects the trunk + thick-limb (base radius ≥ `COLLISION_MIN_RADIUS`=5hu) tube triangles into a soup; `spt_converter._make_collision` builds `bhkMoppBvTreeShape→bhkCompressedMeshShape` from it via `cms_builder.build_cms_collision` (the real Havok MOPP bridge). Plain identity static bhkRigidBody, CMS target = root BSLeafAnimNode, wood material, layer 1. Matches Gildergreen exactly. Falls back to a trunk capsule only if the bridge fails. A capsule sized to the whole trunk AABB is ~2× too fat — use the mesh.
- **One NIF PER TREE RECORD** (named `<editorid>.nif`): Oblivion resolves each TREE record's leaf composite texture from its ICON field and seeds the generator from its SNAM seed, so records sharing one `.spt` (e.g. ShrubVineMapleSU + TestToddTree03) genuinely differ. Manifest read from `<export>/TREE.txt` by `load_tree_manifest`.
- **TREE record import** (`tes5_import/record_types/items.py::convert_TREE`): MODL → `tes4\speedtrees\<editorid>.nif`; OBND derived from the TES4 billboard dims (real world size); adds PFPC (0) + CNAM (12 wind floats — the BSLeafAnimNode params TES4 has no source for).
- **Preview/iteration tool**: `python tools/spt_preview.py <spt_or_dir> [--views 0,90] [--out dir]` renders the generated geometry to PNG with real leaf textures AND pastes Oblivion's own billboard render beside it for A/B comparison. This is how the generator semantics were validated — ALWAYS compare against the billboards, never guess.
- Stats: 113 Oblivion.esm SPTs → 116 tree-record NIFs, 0 fail, all 116 collision-sane + MOPP-clean. Tests: `tests/test_spt_convert.py` (19 tests: parser, generator, NIF builder, TREE import).

## Book inventory art (INAM reading rigs) — books were invisible with no text when opened (SOLVED 2026-07-18)

- **Why books failed**: Skyrim's BookMenu renders the BOOK record's INAM inventory-art mesh, never the world MODL. The vanilla INAM meshes (`clutter\books\book02\character assets\bookskyrim01.nif`, `clutter\books\note01\note02.nif`) are rigged: skinned page-turn bone chains ("Book CoverPage Turn1-6", "Book TurnPage1-10", "Note Fold1-3"), a `BSBehaviorGraphExtraData` pointing at `Clutter\Books\Book01\Book01Project.hkx` that drives the open/page-turn animation, and a 4-vert `PageText` NiTriShape (with `NiStringExtraData 'Keep' = "NiHide"`) the engine swaps for the rendered page text. A static (converted Oblivion) mesh as INAM opens invisible with no text. INAM must always be present — BookMenu null-derefs without it.
- **Solution** (`asset_convert/book_inam.py`): keep the vanilla rig byte-for-byte (UVs, skin, BGED untouched — animation guaranteed) and instead **bake the Oblivion book's textures into the template's texture layout**, then point the template's cover `BSShaderTextureSet` at the baked atlas. One NIF+DDS pair per distinct TES4 book model (38 for Oblivion.esm) → `meshes\tes4\clutter\books\inv\<model basename>.nif` + `textures\tes4\clutter\books\inv\<base>.dds`/`_n.dds`.
- **Calibration is per-mesh UV-island fitting, not hardcoded rects**: both sides decompose into the same semantic regions — front cover (largest flat +Z island above the midplane), spine (tall |n_x| island on the same texture), page-edge strips (side-facing islands spanning the page block; Oblivion maps these to `bookpages01.dds`). A least-squares affine fit (normalized in-plane coords → uv) per region on each side, composed dst-uv → coords → src-uv, handles every Oblivion layout automatically: Octavo has the spine on the left edge of the texture, Quarto/Folio have it in the middle with separate front/back art. The Skyrim cover uses ONE art rect for both covers (u∈[0.24,0.96], spine u<0.22, page strips v>0.97, with wrapped UVs at u±1/v+1 reusing regions), so the Oblivion FRONT cover art is used for both sides — same limitation as vanilla.
- **Flat sheets (notes/parchment/posters/broadsheets) bake as a plain UV-space rect copy**, NOT through mesh coords: sheet art is authored upright in texture space while the world mesh may lie in any orientation (a flat-lying broadsheet arrived rotated 90° on the portrait Note02 template until this was changed). Unfittable sources (rolled scrolls, crumpled paper — UV not affine in position, fit rms > 0.08) fall back to an identity full-texture copy; scroll textures are actually flat sealed-parchment art, so they read fine on the note rig.
- **Atlas output**: 512² uncompressed BGRA DDS with a full box-filtered mip chain (writer in book_inam, decode via PIL). Normal maps baked the same way from the `_n` siblings (flat normal fallback). Pages/paper shapes keep the vanilla `LargeBookPaper01.dds` (loaded from Skyrim's own BSAs — nothing redistributed; templates come from the user's `Skyrim - Meshes*.bsa` via `skyrim_assets` auto-extraction, read through `sse_nif`).
- **pyffi round-trips Skyrim rigs safely**: re-writing bookskyrim01.nif only reorders the header string table with indices remapped consistently (verified byte-level); skin partitions/BGED survive.
- **Record side** (`tes5_import/record_types/equipment.py::convert_BOOK`): one `InvArt_<base>` STAT per distinct model (cached on the writer — BOOKs convert serially), INAM → that STAT; vanilla `HighPolySkyrimBook` only for model-less books. DATA.Type is ALWAYS 0: the CK lists 255 = Note/Scroll but vanilla Skyrim.esm types all 821 BOOKs (notes included) as 0, so 255 is engine-untested; TES4 scroll-ness survives via the vendor keyword + note-rig INAM.
- Pipeline: runs inside `convert.py phase_assets` after `convert_meshes`; standalone CLI `python -m asset_convert.book_inam Oblivion.esm [--extract-dir export] [--output-dir output] [--templates-dir <explicit meshes tree>] [--skyrim-data <SSE Data>] [--workers N]`. Tests: `tests/test_book_inam.py`.
- **Basename uniqueness (fixed 2026-07-28)**: `inv_basename()` keyed on the MODL leaf filename only, so plugins that merge several asset trees collided — Morroblivion ships both `Clutter\Books\Note01.NIF` and `Morroblivion\Clutter\Paper\Note01.nif`, and the guard `raise ValueError('INAM basename collision')` **aborted the entire asset stage** (book INAM + everything after it) for the whole plugin. Now names outside the conventional `clutter\books` tree are qualified by their parent directory (`paper_note01`), leaving vanilla-layout names untouched so existing generated assets stay stable; a residual clash logs and skips that one model instead of killing the stage. `equipment.py::convert_BOOK` **imports `inv_basename` instead of re-deriving it** — the rule had been duplicated in both files, which is exactly how the STAT target and the generated mesh could drift apart.
- **`i.shape in page_shapes` was a numpy trap (fixed 2026-07-28)**: shapes are dicts holding numpy arrays, so `in` runs dict `__eq__` → element-wise array comparison, raising `ValueError: operands could not be broadcast together` the moment two shapes have different vertex counts. Books whose shapes happened to share a vertex count worked; mixed ones failed to bake (35 failures on Morroblivion, now 0). Compare islands to shapes by `id()`, never by `in`/`==`.

## SSE-format NIF read support + BSA auto-extraction (2026-07-19)

- **Rule: the pipeline never resolves runtime assets through `references/`** (that tree is comparison-only and may not exist). Vanilla Skyrim files are fetched via `asset_convert/skyrim_assets.py`: `export/skyrim_assets/` cache first, else extracted on demand from the registry-detected SSE install's BSAs (atomic cache writes — pool workers race). `set_skyrim_data()` overrides detection.
- **SSE meshes (BSTriShape) are readable via pyffi Patch 8** (`pyffi_monkey_patch._install_sse_layouts`): registers `BSTriShape`/`BSDynamicTriShape` (fixed prefix as declared pyffi attrs so name/Refs use the generic link machinery; variable vertex/triangle/particle payload hand-read into numpy `sse_*` arrays) and an SSE-layout `NiSkinPartition` read (`sse_partitions` dicts + shared vertex buffer). READ-ONLY by design — writes raise.
- **`asset_convert/sse_nif.read_nif(path_or_bytes)`** converts any SSE graph to LE in-memory: BSTriShape → NiTriShape+NiTriShapeData (verts/normals/uvs/colors/tangents; skinned shapes pull geometry from the partition's shared vertex buffer), skin partitions rebuilt faithfully (preserving the vanilla body's semantic 32/34/38 dismember split — do NOT regenerate from scratch), `user_version_2` set to 83 so writes are LE. Validated field-identical against the LE reference body.
- **SSE partition gotchas**: BOTH triangle arrays in an SSE NiSkinPartition ("Triangles" and "Triangles Copy") hold GLOBAL shape-vertex indices — LE wants partition-LOCAL indices into the vertex map, so remap via inverted vertex_map. Vertex-data bone indices are partition-local. Vanilla SSE `NiSkinData` still carries LE-style per-bone weights (`has_vertex_weights=1`), so binds/weights come straight from it; `_ensure_skin_weights` rebuilds them from partition data only if absent.
- **Consumers**: `skin_replacement.load_body_geom` (modified body in `output/` → BSA body), `book_inam.load_templates` (BSA book/note rigs; emit re-writes them as LE), `modify_body_meshes` (BSA body → split → LE output), `body_wrap._load_sk_surface`, `extract_skeleton_bones`. A missing body source now prints a loud `[skin_replacement] WARNING` instead of silently skipping the splice, and `generate_book_inams` validates templates in the parent before spawning workers (a worker-initializer crash surfaces only as an opaque BrokenProcessPool, with stderr hidden under pythonw).


## Two vanilla divergences investigated and DELIBERATELY NOT FIXED (2026-08-22)

Both were found while chasing the ElsweyrAnequina load crash, both were briefly
believed to be its cause, and both turned out not to be: that crash was an
unsupported collision shape (next section), confirmed fixed in-game.

Fixes for both were written, measured, tested — and then **reverted and not
shipped**, because neither has a demonstrated in-game benefit and both carry
real downside. Recorded here so the measurements are not re-derived, and so a
future session does not "fix" them again without new evidence.

**Do not re-fix either one on the strength of the census alone.** Ship only if
an actual in-game symptom is traced to it.

### 1. Object LOD carries `slsf_2_double_sided`; vanilla never does

Oblivion marks foliage and other cutout geometry two-sided with a
`NiStencilProperty` (`draw_mode` 3 = DRAW_BOTH). `nif_converter` carries that
across as `slsf_2_double_sided` — correct for the full-size mesh — and LODGen
then copies the flag into the shader it writes for each baked tile.

Measured:

| population | tiles | shader props | `double_sided` |
|---|---|---|---|
| vanilla `meshes/terrain` (mixed) | 120 | 141 | **0** |
| vanilla `terrain/tamriel/objects` | 40 | 74 | **0** |
| our output | 2,582 | — | **21,899** |

Vanilla object LOD is uniformly `f2=0x00000005` on Tamriel tree LOD. So the
flag is a genuine divergence.

**Why it was not shipped:** no in-game symptom was ever traced to it, and no
back-face rendering cost was measured. The fix had to be a byte patch (parsing
~15,700 tiles with pyffi costs ~2.3s each, i.e. ten hours), and an in-place
byte patch on shipped artifacts is a standing corruption risk if a future
LODGen output shifts the anchored layout — a bad trade for an unmeasured gain.

For the record the patch did validate cleanly: anchoring on the 32-byte window
(controller/extra-data refs `-1,0,-1` before the flag pair; UV offset/scale
`0,0,1,1` after) matched the true `BSLightingShaderProperty` count **exactly on
453 of 453 tiles**, was idempotent over repeated passes, and ran the full
output in 31s.

### 2. One `NiAlphaProperty` shared between shapes; vanilla never shares

Measured: **10 of 400** Oblivion source meshes share one block between shapes,
up to **14 shapes on the single block** in `benirusdoor01.nif`, and the sharing
survives conversion (5 of 300 converted architecture meshes). Vanilla Skyrim:
**300 meshes, 250 carrying an alpha property, 0 sharing one.**

The particle path in `nif_converter` already clones for exactly this reason;
the geometry path does not.

**Why it was not shipped:** the mechanism originally claimed for it — that
these properties are refcounted per render pass, so a block reached through N
shapes is released N times — was **never measured**. It was a theory invented
while this looked like the crash cause. With that removed, what remains is
"vanilla does not do this", with no observed misbehaviour to fix.

## `bhkPackedNiTriStripsShape` reaching the output — the 2 GB memcpy / heap-wide `0x100000001` (SOLVED 2026-08-22, confirmed in-game)

**Symptom.** Reproducible crash-or-freeze near `tes4tamriel 0 -30` in
ElsweyrAnequina.  The access violation moved around between runs -- the shadow
renderer, `BSXAudio2GameSound`, a `ScrapHeap` path, `bhkListShape` during a mesh
load, and finally inside tbbmalloc's own
`rml::internal::MemoryPool::getTLS` -- but the faulting value was **always
`0x0000000100000001`**.  Sometimes CrashLoggerSSE itself deadlocked in its
handler (`MSVCP140!_Mtx_lock` -> `RtlpAcquireSRWLockExclusiveContended` ->
`NtWaitForAlertByThreadId`), so the game "hung" with no crash log written at
all.

**Same family as the Seyda Neen UV-set CTD** (see the `_clamp_uv_sets` entry
above): both are a short destination buffer, both fault inside a `memcpy`
on a non-temporal store, and in both the crash log blames whatever the
corrupted memory reached next rather than the mesh that caused it.

**Do not chase the subsystem in the log.**  Four different crash logs blamed
four different subsystems and all four were victims, not causes.

**Diagnosis (from a live dump).**  Attach cdb, `sxe av`, `.dump /ma` at the
fault (`tools/hang_capture.py` does exactly this).  In the captured dump:

* the faulting thread was inside `VCRUNTIME140!memcpy` with **`r8 =
  0x7EF225F0` -- a 2.03 GB copy length** (a second crash log showed
  `0x7F436BE0`, the same thing);
* the memcpy SOURCE was a **2.25 GB** committed block whose tbbmalloc header
  read `totalSize=0x90010000`, `objectSize=0x90000000`, owner pointer into
  `EngineFixes.dll`, and whose contents were **entirely
  `01 00 00 00 01 00 00 00 ...`**;
* the same allocator list held a **36 GB** block; total commit was **49.2 GB**
  against a normal ~8 GB;
* the loader stack carried the asset path as a plain string --
  `data\MESHES\tes4\anequina\architecture\huts\domehut01.nif`.

So the `(1,1)` fill is not something corrupting memory: it is uninitialised
content being **copied around by the gigabyte**, landing in whatever allocates
next.  That is why one bad mesh looks like four unrelated crashes.

Finding the path on the stack is the step that matters -- search the loader
thread's stack for `"nif"` (`s -a <stack range> "nif"`) and read the string
back.

**Cause.** `_convert_shape` had:

```python
if isinstance(shape, NifFormat.bhkNiTriStripsShape):
    packed = _ni_strips_to_packed(shape)
    return packed if packed is not None else shape      # <-- returns too early
```

A `bhkNiTriStripsShape` nested inside a `bhkListShape` was converted to a
`bhkPackedNiTriStripsShape` and returned **directly**, never reaching the
`bhkPackedNiTriStripsShape` branch a few lines below that rebuilds it as
MOPP + `bhkCompressedMeshShape`.

Skyrim does not support that shape.  Census: **0 of 17,216 vanilla Skyrim
meshes** contain `bhkPackedNiTriStripsShape` or `hkPackedNiTriStripsData`.  The
engine mis-sizes its sub-part allocation and then memcpys the payload with a
garbage 32-bit length -- the loader's grow step is
`imul edx, r15d` / `imul esi, r15d`, both 32-bit, feeding the allocator and the
memcpy.

**Scope.** Exactly **10 meshes** in the whole output still shipped the type:
1 in ElsweyrAnequina (`domehut01.nif` -- the only one of 1,837 in that plugin,
and the one the dump named), 1 in Oblivion.esm
(`dungeons\root\interior\misc\gnarlspawner.nif`), 8 in Tamriel Resource
Pack Full 2.0.

**Fix.** Route the converted shape back through `_convert_shape` so the
existing MOPP/CMS rebuild runs.  The rebuild succeeds for these meshes -- it was
simply never attempted.  Verified on `domehut01.nif`: before, `bhkListShape` ->
`bhkPackedNiTriStripsShape` + `hkPackedNiTriStripsData`; after, `bhkListShape`
-> `bhkMoppBvTreeShape` -> `bhkCompressedMeshShape` with one chunk of 1,935
verts / 3,711 indices = **1,237 triangles, exactly the source count**, and a
10,587-byte MOPP tree.

**Note on a red herring:** pyffi prints the Skyrim stone material
(`3741512247` = `SKY_HAV_MAT_STONE`) as `<INVALID>` because its enum table is
incomplete.  That value is correct output, not a corruption sentinel.

**Also note:** `bhkPackedNiTriStripsShape.Num Sub Shapes` is `until="20.0.0.5"`
and `hkPackedNiTriStripsData.Num Sub Shapes` is `since="20.2.0.7"` (nif.xml
3195/3968), so at our output version the shape-side count is not serialised and
a `0` there is cosmetic.  It is not the bug -- the unsupported *shape type* is.

### Three defects, not one (2026-08-22)

The unsupported shape reached the output by three separate routes.  All three
are fixed; the first is the one confirmed in-game.

1. **Nested strips never rebuilt.**  `_convert_shape`'s
   `bhkNiTriStripsShape` branch converted to a packed shape and returned it
   directly, skipping the `bhkPackedNiTriStripsShape` branch below that
   rebuilds as MOPP+CMS.  Fixed by recursing:
   `return _convert_shape(packed, root_node)`.
   (`anequina/architecture/huts/domehut01.nif` — confirmed fixed in-game.)

2. **Collision on non-NiNode geometry never converted at all.**
   `convert_all_collisions` opened with
   `if node is None or not isinstance(node, NifFormat.NiNode): return`, so a
   `bhkCollisionObject` hanging off a **NiTriShape** was skipped *and* its
   subtree was never walked.  Oblivion does exactly that:
   `obmkmeadhallmaindoor.nif` puts a `bhkPackedNiTriStripsShape` on the
   NiTriShape `'Scene Root:5'`.  Those objects passed through completely
   unconverted — Oblivion-format shape, Oblivion-format filter values and all.
   Fixed by converting whatever any node owns and always continuing the walk.

3. **Unsafe fallbacks when MOPP failed.**  Two sites shipped a packed shape
   when `build_cms_collision` returned None (`_rebuild_mesh_collision`, and the
   `bhkPackedNiTriStripsShape` branch of `_convert_shape`, which also had a
   "repair the sub-shape count and return `shape`" path).  An unsupported shape
   is never safer than no collision, so both now drop instead.
   `_packed_from_tris` had no callers left and was deleted.

   In practice MOPP only fails on geometry that is not a surface:
   `romanhanginglamp01.nif`'s collision is 8 vertices with X=Y=0 — a bare line
   segment on the Z axis, zero area, quantising to two distinct points.

### Measured blast radius (2026-08-22)

762 Oblivion.esm **source** meshes contain a packed shape, but the converter
already handled 761 of them: scanning all **44,856 converted meshes** across
every plugin found only **5** carrying `bhkPackedNiTriStripsShape` /
`hkPackedNiTriStripsData` after the first fix —

| plugin | leaked / scanned |
|---|---|
| Oblivion.esm | 1 / 11,575 (`dungeons/root/interior/misc/gnarlspawner.nif`) |
| Tamriel Resource Pack Full 2.0 | 4 / 6,129 (3 oblivimonk architecture + romanhanginglamp01) |
| ElsweyrAnequina.esp | 0 / 1,837 (was `domehut01.nif`) |
| Nehrim.esm | 0 / 14,609 |
| Morrowind_ob.esm | 0 / 8,444 |
| everything else | 0 |

So the crash needed a rare combination, which is why it reproduced at one spot
rather than everywhere.  Re-run the census with:

```bash
python tools/nif_block_scan.py output/<plugin>/meshes \
    --any bhkPackedNiTriStripsShape hkPackedNiTriStripsData
```

Guarded by `tests/test_collision_packed_strips.py`.
