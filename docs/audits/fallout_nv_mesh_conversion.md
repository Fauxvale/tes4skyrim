# Fallout NV / FO3 Mesh Conversion Audit — 2026-08-31

Exploratory test: how much of the existing Oblivion→Skyrim asset pipeline already
works on Fallout 3 / New Vegas assets. **No FO3-specific conversion support was
built** — this measures where an FNV path would start. Two changes shipped: the
version gate now keys on `user_version_2`, and `0x14020007` joined
`_SUPPORTED_VERSIONS` so FO3/FNV meshes actually convert.

Corpus, from a Tale of Two Wastelands install at
`C:\Program Files (x86)\Steam\steamapps\common\Fallout New Vegas\Data`:

| Archive | Contents |
|---|---|
| `Fallout - Meshes.bsa` | 17,742 files / 13,067 NIFs |
| `Fallout3 - Meshes.bsa` | 5,572 files / 5,277 NIFs |
| `Fallout - Textures.bsa` | 5,393 |
| `Fallout - Textures2.bsa` | 10,728 |
| `Fallout3 - Textures.bsa` | 4,821 |

All five extracted with `bsa_extract.extract_bsa` into `export/FalloutNV.esm/`,
**0 errors** — 23,314 NIFs and 20,942 textures.

## Header identity — and the bug it exposed

| | Oblivion | FO3 / FNV | Skyrim SE |
|---|---|---|---|
| version | `0x14000004` | `0x14020007` | `0x14020007` |
| user_version | 11 | 11 | 12 |
| user_version_2 | 11 | **34** | **83** |

FO3/FNV share Skyrim's version word and are told apart **only** by
`user_version_2`. 250/250 sampled NIFs from each archive report `uv=11 uv2=34`;
the two archives are format-identical, so one code path covers both.

`nif_converter.convert_nif` gated on `data.version` alone, so every FNV mesh
matched `_SKYRIM_VERSIONS` and took the "already Skyrim — copy as-is" branch:
copied through unconverted, silently, with no error. Fixed by keying the set on
`(version, user_version_2)`.

Gate order matters and is load-bearing: the Skyrim `(version, 83)` check returns
*before* `_SUPPORTED_VERSIONS` is consulted, so adding the shared `0x14020007`
to that set cannot capture real Skyrim meshes — only uv2=34 reaches it.

## Real-stage results

`asset_pipeline.convert_meshes('FalloutNV.esm')` — the same entry point
`convert.py --meshes-only` calls, meshes *and* textures — over the full
17,818-NIF tree, 354.7 s:

| Outcome | Count | % |
|---|---|---|
| Converted | 17,442 | 97.9% |
| RD (read failure) | 371 | 2.1% |
| EXC | 5 | <0.1% |
| Copied / skipped | 0 | 0% |

Textures: **20,942 copied** to `output\FalloutNV.esm\textures\tes4`.

Output validation, 150 of 17,692 written meshes re-read with pyffi:

- **150/150** stamp `ver=14020007 uv=12 uv2=83` — correct Skyrim SE headers
- **146/150** carry a real `BSLightingShaderProperty`/`BSEffectShaderProperty`
- **0** unreadable outputs

This is far better than the block-vocabulary difference predicts. FO3/FNV use
`BSShaderPPLightingProperty` + `BSShaderTextureSet` (239/245 and 249/250 of
sampled source meshes) rather than Oblivion's `NiTexturingProperty` (13/245 and
4/250), yet the shader conversion still produces correct output — the FO3 shader
property is close enough to Skyrim's that the existing path lands on its feet.

Shader-slot gaps the stage papered over: 2,335 shapes had no normal map
(defaulted to `Textures\tes4\default_n.dds`), 518 had no spec alpha, 103 glow
maps were derived.

## Texture paths are WRONG — meshes render untextured

Converted meshes show purple in NifSkope. Of texture references in a 40-mesh
output sample, **3 resolve and 107 do not**; nearly every diffuse slot is
`Textures\white.dds`, the neutral fallback.

The cause is the property loop in `_process_geometry`
([nif_converter.py:1885](../../asset_convert/nif_converter.py#L1885)). It reads
`NiTexturingProperty`, `NiMaterialProperty`, `NiVertexColorProperty`,
`NiStencilProperty` and `NiAlphaProperty` — but **not**
`BSShaderPPLightingProperty`, where FO3/FNV keep their `BSShaderTextureSet`. So
`diffuse_path` stays empty and slot 0 takes the `white.dds` default.

The source data is intact: **149/149** sampled source shapes carry a real
diffuse path, 0 empty. The paths are present and simply never read.

Two further mismatches visible in those source paths, both unhandled:

- Some are `Data\Textures\...`-prefixed (LOD meshes especially), others plain
  `textures\...`. The output tree is `textures\tes4\...`, so paths need the same
  rewrite the Oblivion path applies.
- Case varies freely (`RoboBrain_Outcast.dds`); the extracted tree is lowercase.

This is the single change that would make FO3/FNV meshes actually render, and it
is confined to the property loop plus the existing path-rewrite helper.

## The one real defect: particle systems

Read failures are `KeyError: 16777217` during block-reference resolution. Across
all 376 failures of the real run, re-inspected for particle blocks:

| | Count |
|---|---|
| RD, **has** particle blocks | 339 |
| RD, no particle blocks | 32 |
| EXC, no particle blocks | 5 |

**90% of all failures are particle NIFs**, and in a separate 1,200-NIF sample
*no* particle NIF read successfully (34 failed, 0 succeeded). Failing folders:
`effects` (120), `mps` (50), `clutter` (38), `dlc04` (28), `dlc05` (23),
`dlc03` (20), `dungeons`/`dlcanch` (18 each), `dlcpitt` (11), then a tail.

The non-particle failures are a different, rarer bug — truncated struct reads
(`unpack requires a buffer of 4 bytes`), e.g.
`clutter\hiddenvalley\nv_hv_graffiti05.nif` and
`characters\head\headfemalefacegen.nif`.

## Legacy blocks surviving into output

Present in output because nothing maps them; Skyrim has no equivalent. Counts
from the 150-mesh output sample:

| Block | Count |
|---|---|
| `BSMultiBound` | 37 |
| `NiAdditionalGeometryData` | 34 |
| `BSSegmentedTriShape` | 5 |
| `BSDamageStage` | 1 |
| `BSValueNode` | 1 |

Unmeasured: whether Skyrim tolerates or rejects these at load.

## Other formats

- **BSA**: FNV ships v104. `bsa_extract` already handles 103/104/105 and read all
  five archives with no changes, 0 errors.
- **Textures**: DXT1/DXT3/DXT5 in standard DDS containers (40/40 sampled), which
  Skyrim reads natively. All 20,942 copied through without conversion.

## Record conversion — subrecord fidelity

`tes4_export.tes4_reader` returns **zero records** for FalloutNV.esm.
[tes4_reader.py:15](../../tes4_export/tes4_reader.py#L15) sets
`RECORD_HEADER_SIZE = 20` (Oblivion); FO3/FNV use **24**. The TES4 header then
consumes 20+30=50 but `GRUP` sits at byte 54, so the first top-level read misses
and parsing aborts silently. `GROUP_HEADER_SIZE` is 20 in both games and must
*not* change. With the record constant patched to 24 in-process: **465,054
records, 107 distinct types**.

Type coverage against `IMPORT_DISPATCH` (52 types): 29 of FNV's 49 common types
already have handlers — including every type named in a "worldspace + cells +
references" scope. 18 have none (`NAVM PACK SOUN TXST FLST TERM PERK EXPL MESG
NOTE CCRD CAMS CPTH IDLM IMAD LVLN MSTT PGRE`), 3 are skipped by design
(`GMST IDLE SCPT`).

**Type coverage overstates readiness.** Comparing actual subrecord signatures in
FalloutNV.esm against Nehrim.esm (a real TES4 binary — xEdit's Pascal was tried
first but its helper indirection, `wbGenericModel` and friends, measures helper
style rather than format drift), then weighting each field by how often it
actually occurs:

**342,914 of 2,085,715 subrecord instances (16.4%) use a field Oblivion never
emits.** By record type, share of instances unhandled:

| Clean (0%) | Light (<20%) | Moderate (20–50%) | Heavy (>50%) |
|---|---|---|---|
| `LAND` `GLOB` `GMST` `MGEF` `ANIO` `CLMT` `EYES` | `FACT` `REGN` `SPEL` `ACRE` `REFR` `ENCH` `LVLC` `DOOR` `ACHR` `CONT` `QUST` `LVLI` `INFO` `FURN` `MISC` | `CLASS` `LSCR` `LIGH` `DIAL` `BOOK` `ALCH` `NPC_` `CREA` `ACTI` `WATR` `STAT` `KEYM` `WRLD` | `ARMO` `CELL` `AMMO` `WEAP` |

The split that matters for your scope:

- **`LAND` is perfect** — 29,363 records, 252,015 subrecords, **0%** unhandled.
- **`REFR` is 6.3%** despite a 68% *field-count* drift, because its bulk is
  Oblivion-compatible: `NAME` (37.7%) + `DATA` (37.7%) + `XSCL` (14.0%) = 89% of
  all 816,313 REFR subrecords. The FNV-only fields are a long tail.
- **`CELL` is 52.9%** and is the real obstacle: `LTMP`, `LNAM`, `XNAM` and
  `XCLW` each fire on *every* one of the 30,497 cells, so the drift is
  structural, not a tail.
- `WEAP` (77.4%) and `ARMO` (50.9%) are Fallout-specific by nature (weapon mods,
  ammo, DEST destruction data) — expected, and outside a worldspace scope.

So a worldspace/cells/references conversion is **not** a matter of flipping a
constant. LAND and REFR would largely survive; CELL needs real per-field work
before its output would mean anything.

## What is NOT established

- Havok correctness. 137/245 sampled meshes carry collision; `_HAVOK_SCALE = 0.1`
  is an Oblivion→Skyrim factor and was **not** re-derived for FO3. MOPP data is
  version-sensitive and unverified here.
- In-game validity. Nothing was loaded in Skyrim; "converted" means the pipeline
  wrote a re-readable NIF, not that it renders.
- Whether the surviving legacy blocks cause load failures.
- Skeletons, animation, and skinned meshes were not separately assessed.
- Record **semantics**. The subrecord census compares signature presence and
  frequency, not field layout: a `DATA` that both games emit may still differ in
  size or meaning. A shared signature is necessary, not sufficient.
- Nehrim.esm stood in for Oblivion.esm as the TES4 baseline. It is a total
  conversion, so a field vanilla Oblivion uses but Nehrim never does would read
  here as FNV-only.
- The 24-byte header cannot be a global constant flip — it would break every
  Oblivion plugin. It needs to be per-file, keyed off the HEDR version (FNV
  reports 1.34, Oblivion 0.8/1.0).

## Reproducing

Extraction used `bsa_extract.extract_bsa(bsa, 'export', source_name='FalloutNV.esm')`
per archive; conversion used
`asset_pipeline.convert_meshes(source_file='FalloutNV.esm')`.

FalloutNV is **not** in the source registry — its export tree was created
directly, so `convert.py -f FalloutNV.esm --meshes-only` will not find it.
Registering it as an asset-only mod is the missing step for a normal CLI run.

Calling `nif_converter.batch_convert` directly converts meshes but **not**
textures; the stage entry point is `convert_meshes`. All harness scripts were
one-offs and were not kept.
