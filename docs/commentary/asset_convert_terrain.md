# asset_convert/terrain_lod.py — terrain, LOD and grass

**Code:** `asset_convert/terrain_lod.py`, `asset_convert/lod_gen.py`, `asset_convert/lod_far_gen.py`, `asset_convert/grass_profile.py`, `asset_convert/lava_surface.py`

## Contents

- [Grass (GRAS) conversion — record invariants + shader profile (2026-07-09)](#grass-conversion-record-invariants-shader)
- [Terrain/LOD/LAND-adjacent asset notes](#terrainlodland-adjacent-asset-notes)
- [Lava surfaces — Oblivion realm water rendered as actual lava (2026-08-23)](#lava-surfaces-oblivion-realm-water)

## Grass (GRAS) conversion — record invariants + shader profile (2026-07-09)
<a id="grass-conversion-record-invariants-shader"></a>
- Skyrim grass spawns purely from LAND texture layers whose LTEX has GNAM — no REFR/region records involved. Chain verified in output ESM: GRAS DATA layout byte-matches the xEdit TES5 def; LTEX GNAM links and subrecord order (EDID/TNAM/MNAM/HNAM/SNAM/GNAM) match vanilla; all 419,902 LAND BTXT/ATXT layers resolve to real LTEX (~120k grass-linked); WRLD DATA bit 0x80 = "No Grass" must stay clear.
- **GRAS record invariants (the working-example pattern)**: every working GRAS record — vanilla Skyrim.esm, USSEP, Beyond Skyrim's BSHeartland.esm, Skyrim Extended Cut, Legacy Orsinium (all dumpable from the install with tools/esm/tes5_esm_reader.py) — has (1) **OBND all ZEROS**, never computed mesh bounds, (2) **MODT present** (BSHeartland proves a 12-byte version-2 stub `02000000 00000000 00000000` suffices), and (3) **the model under `meshes\landscape\grass\`** — 45/45 surveyed MODL paths contain `landscape\grass\`; nothing outside it is known to work (same kind of hardcoded naming contract as the `NPC Root [Root]` bone). convert_GRAS builds the record manually to honor all three (MODL via `grass_profile.grass_model_dest()` → `landscape\grass\tes4_<basename>.nif`, flattened — TES4 grass basenames are collision-free), and `grass_profile.run()` copies each converted grass NIF there (sources under tes4\ stay for FLOR/STAT sharing). BSHeartland also shows GRAS DATA density up to 80 and TES4-style values are fine; LTEX INAM (SSE "Is Snow" flag) is optional.
- **Grass NIFs with Vertex_Alpha + low VC alpha look invisible/ghosted in NifSkope — that is normal and matches vanilla** (vanilla grass VC alpha avg ~0.15; NifSkope multiplies it into the alpha test, the in-game grass shader uses it as wind weight instead). Don't diagnose grass visibility in NifSkope. Alpha-test thresholds are clamped to the vanilla grass envelope (≤100; Oblivion used up to 128).
- **GRAS DATA Density/PositionRange must be clamped to the working envelope** (Density ≤80, PositionRange ≤32 — vanilla uses Density 3-6/PosRange ≤32, BSHeartland up to Density 80). TES4 values (Density up to 100, PosRange up to 90) were tuned for Oblivion's 4x-coarser placement grid (iMinGrassSize 80 vs Skyrim 20); passed through raw they over-instance the grass planter and CTD **with no crash log** on cells where dense grass textures cover whole quadrants (found via `python tools/esm/cell_grass.py export/Oblivion.esm --wrld Tamriel --cell X,Y` — lists the grass types each cell can spawn; the two density-100 types were the outliers shared by the crashing cells).
- **Grass CTD root cause #2 — Oblivion meshes with NO triangle data (SOLVED 2026-07-10)**: several vanilla Oblivion grass meshes (GroundCoverMediumGrass01/LongGrass01, GroundCoverPineappleWeed*, GroundCoverWildPlant*, ms14longgrass01) ship NiTriShapeData with `has_triangles=False` — Num Triangles is set but the index array is ABSENT (only `Oblivion - Meshes.bsa` has them; no intact alternates exist). Oblivion's grass renderer tolerated it; Skyrim's planter dereferences the missing data → **region-specific CTD with no log** (Heartland/Cheydinhal cells). Several others (JMMediumGrass*, brmediumgrassyellow01, groundcoverfern01, oblivionmoldroots01) carry legacy vertex **match groups** (no vanilla Skyrim mesh has any) → same crash in Bruma cells. Fix: `asset_convert/tri_reconstruct.py` — blade triangles are reconstructed from the 3-UV-role pattern (base-left/base-right/tip; tip role = highest avg z; each blade pairs a tip with the base pair whose midpoint it tops; winding from stored normals), and match groups are cleared on every converted shape. Both run inside `_convert_strips_or_shape`, so FLOR/STAT meshes sharing these sources heal on full re-runs. Diagnosis method: `tools/esm/cell_grass.py` per-cell grass lists × working-vs-crashing region diff → the defective meshes were exactly the types unique to crashing regions. A mesh with `has_triangles=False` also renders as NOTHING in NifSkope (that was the real cause of the "invisible in NifSkope" report, not vertex alpha).
- **Grass CTD root cause #3 — intermediate NiNode wrapper on rotated sources (SOLVED 2026-07-10)**: Skyrim's grass instancer (`AddCellGrassTask` → `BSMultiStreamInstanceTriShape`) requires grass geometry as a **direct child of the BSFadeNode root** — every working grass NIF (vanilla + converted `gcgorsegrass`/`gclonggrass`) is flat `BSFadeNode → NiTriShape`. But the generic converter's Pass-6c wraps geometry in an inner NiNode whenever the **source root carries a non-identity rotation** (it bakes the rotation into a child NiNode because Skyrim honors child-NiNode rotation but ignores BSFadeNode root rotation for statics). The grass path never traverses that inner node → dereferences garbage (`rdi=0x0001000100010001`, `movzx ecx,[rdi+0x32]`) → **CTD** on any cell spawning the type. Hit TES4 **BWCattail01/02/03** (their source roots are rotated; the crash object was `BWCattail02` with a nested `NiNode "BWCattail02"`). Fix: `grass_profile._flatten_grass_root()` (runs inside `apply_grass_profile`) bakes each plain NiNode wrapper's transform into its geometry's verts+normals and re-parents the geometry onto the root, dropping the empty NiNode — world-space geometry preserved (verified: Z height extent unchanged). Only collapses bare NiNode wrappers holding pure geometry (no collision/controller/extra-data). Diagnosis: crash log named `tes4_bwcattail02.nif` + `BSFadeNode`/`NiNode` both named "BWCattail02"; block dump vs a working converted grass NIF showed the extra nesting; source root rotation identity=False (vs gcgorsegrass identity=True, which stayed flat).
- Known remaining grass-NIF oddities (not crash-related, grass renders): most grass tex[1] `_n.dds` normal maps don't exist (vanilla grass points tex[1] at `textures\effects\HighFrequencyNormals.dds` or the literal string `NOR`); bwcattail03 references BWCatTail02.dds which is absent from the extracted BSAs.
- **BSHeartland.esm is the best reference for "custom worldspace + custom grass records that provably work"** — compare against it before vanilla when a worldspace-scoped feature is dead.
- Grass NIFs additionally get the vanilla grass shader profile (`asset_convert/grass_profile.py`, run by `asset_pipeline.convert_meshes`; models identified from the export's `GRAS.txt`): NiAlphaProperty alpha-test only (blend bit clear), SLSF1 OwnEmit+VertexAlpha set / Specular clear (Specular + glossiness 0 = pow(NdotH,0)=1 white-out), emissive ×1.0, gloss 80, spec white/1.0, lighting effects 0.3/2.0, clamp 0 — matching every vanilla LE grass mesh in `references/Skyrim Meshes/meshes/landscape/grass/`. Geometry/UVs/vertex-color alpha (wind weight) preserved.
- 8 Shivering Isles grass models (Plants\Dementia\*, Plants\Mania\*) are absent from the extracted BSAs — their GRAS records exist but have no mesh until SI assets are extracted.

## Terrain/LOD/LAND-adjacent asset notes
<a id="terrainlodland-adjacent-asset-notes"></a>

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
Audit with `tools/validate/mttc_target_check.py`; guarded by
`tests/test_asset_convert.py::TestMTTCTargetsStayInSyncWithControlledBlocks`,
which drives the REAL converter (a hand-built MTTC tests pyffi's reference
arrays, not the converter).

### 🔴 A graph-bound mesh must ship NO empty text keys (2026-08-10)

Crashes `crash-2026-08-10-01-08-13` / `-01-39-02` / `-01-41-07` — the Spiddal
Stick and Harrada Root CTDs that survived both the MTTC fix (above) and every
Rest-state theory.  Same signature as the MTTC family (`movdqu xmm2,[rax]`,
rax=0, VCRUNTIME140, under `BGSGamebryoSequenceGenerator`) but a different
null.

**Mechanism, from disassembly** (`tools/disasm/address_lib.py --log` → GOG RVA
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

Audit: `tools/validate/gamebryo_seq_check.py` (check 3).  Guarded by
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
- Debug single tiles without a full run: `python tools/lod/terrain_lod_render.py` (rebuilds specific tiles in-process, reports water quads, dumps diffuse PNG). `python -m tools.lod.terrain_lod_tex_probe [--cell X Y]` audits LTEX→TXST→dds resolution and per-cell layers.
- Validate with `python tools/lod/terrain_lod_render.py --esm output/oblivion.esm/oblivion.esm --worldspace TES4Tamriel --cell X Y --radius R` → side-by-side hillshade + composited diffuse (the primary iteration tool; do NOT byte-match vanilla .btr). `tools/lod/lod_nif_inspect.py` dumps .btr/.bto geometry+shader.
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

## Lava surfaces — Oblivion realm water rendered as actual lava (2026-08-23)
<a id="lava-surfaces-oblivion-realm-water"></a>

**Skyrim's water shader physically cannot render lava.** The complete
`BSWaterShaderPixelConstants` table, read out of SkyrimSE.exe at `0x1455789`,
is: `ShallowColor DeepColor ReflectionColor FresnelRI CameraData ProjData
VarAmounts SunDir SunColor NumLights LightPos LightColor WaterParams
DepthControl SSRParams`. **No diffuse texture slot and no emissive term** — the
three colors only tint a reflection/refraction result. All 93 `NNAM` entries
across vanilla Skyrim's 34 WATR records name the same `DefaultWater.dds`, which
is the normal/noise map, not a color map. Oblivion's lava colors are DARK
(`shallow=(79,12,2)`) precisely because they tinted a bright emissive texture
Skyrim has no way to sample, so a faithful WATR port renders as dark water.
Chasing better WATR values is a dead end.

**Bethesda's own answer is geometry.** Dawnguard's Aetherium Forge
(`DLC1Bthalft01`) layers two things: `LavaSettings` (WATR, reached by the
cell's `XCWT`) for the PHYSICS — swim, damage, fog, plus an `INAM` image space
for being submerged — and `DweSpecialForgeLava01/02/03.nif` for the LOOK, an
ordinary mesh with a `BSEffectShaderProperty`:
`source_texture=WavyTurbulence01.dds`, `greyscale_texture=GradHotCoals.dds`,
`emissive 1,1,1 × 2.0`, and a `BSEffectShaderPropertyFloatController` on
U Offset. `Skyrim.esm`'s own `LavaWater` record is **dead data** — zero cell,
worldspace, or REFR references; the only "lava" strings in Skyrim.esm are
Olava the Feeble.

**Our implementation**: `asset_convert/lava_surface.py` generates the plane,
`tes5_import/lava_placement.py` places it. Oblivion's `oblivionlava06.dds` is
already full colour (DXT1 blocks decode to `(230,97,49)`, `(222,64,32)`; mean
channel spread 151/255), so it goes straight into `source_texture` and the
greyscale-to-palette path is NOT used — setting `slsf_1_greyscale_to_palette_color`
without binding a gradient samples a missing texture.

**Lava is identified from AUTHORED data only**: a WATR is lava when
`MNAM.MaterialID == "lava"` (Oblivion.esm: exactly `OblivionLavaTest01` and
`OblivionCitadelLavaPlane`; Nehrim: `LavaLow`, `LavaDurchsichtig`). A cell gets
a plane when its `XCWT.Water` is such a record, or — failing that — when it
inherits one via its worldspace's `NAM2.Water`, which is how the engine itself
resolves a cell's water. **Never infer from the worldspace being an "Oblivion
realm"**: that flag says nothing about whether the water is lava. Oblivion.esm
yields 7,765 planes (7,719 exterior on the 4096-unit cell grid, 46 interior at
their authored `XCLW` heights).

### Three bugs that each cost an in-game test cycle — check these FIRST

All three produce a mesh that is perfectly valid on disk, loads without crash
or warning, and is silently wrong in game:

1. **No `BSXFlags` on the root → the scroll never runs.** Skyrim only ticks a
   mesh's time controllers when the root sets BSXFlags **bit 0 (Animated)**.
   The controller sits in the file and the texture is frozen. Vanilla lava
   ships `BSX=1` (collisionless + animated). Identical to the fire-invisibility
   root cause above — the same trap, twice.
2. **Reversed winding → invisible from above.** With `a=(x,y)`, `b=(x+1,y)`,
   `c=(x,y+1)`, winding `(a,c,b)` yields a **−Z** normal and the plane
   backface-culls exactly where the player stands. Correct is `(a,b,c)` /
   `(b,d,c)`. Compute the cross product; do not trust the comment.
3. **Controller `target` unset → animation never binds.** Vanilla sets
   `.target` on every shader float controller, as does `nif_converter.py` at
   all six of its own sites.

Also match `texture_clamp_mode = 0xFF03` (both vanilla and our own working
converted meshes; a bare `3` differs) and controller `flags = 0x48`
(Active | Compute Scaled Time — without the scaled-time bit the curve does not
advance). `NiFloatInterpolator.float_value` may be `0.0`: our shipped,
in-game-confirmed scrolling meshes (streetlamps, flame atronach) use `0.0` and
animate fine, so it is **not** the blocker it first appears to be.

Guarded by `tests/test_lava_surface.py` (5 tests, each asserting one of the
silent-failure properties).
