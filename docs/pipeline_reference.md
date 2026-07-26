# Pipeline Reference — orchestration, caching, layout, export format

Linked from [CLAUDE.md](../CLAUDE.md). Everything about *running* the
conversion and the shape of the data that moves between stages. For per-tool
command lines see [python_tools_reference.md](python_tools_reference.md).

## Orchestrator

`convert.py` at the repo root drives every stage, reading the file list from
`conversion_config.json` in dependency order. Masters are auto-detected from
the TES4 binary headers; game data paths come from the Windows registry.

```bash
python convert.py                          # full pipeline
python convert.py -f Oblivion.esm          # single file, all stages
python convert.py -f Oblivion.esm --export-only
python convert.py -f Oblivion.esm --no-cache      # force re-export
python convert.py -f Oblivion.esm --scripts-only  # Papyrus only
python gui.py                              # GUI front-end
```

Each stage has a `--<step>-only` flag. The steps are: `export`, `import`,
`extract`, `meshes`, `speedtrees`, `sounds`, `scripts`, `lod`,
`modify-body-meshes`, `pack`, `pack-zip`. Read `convert.py`'s module docstring
for the authoritative list — it changes more often than this doc.

Direct module entry points:

```bash
python -m tes4_export.export "C:/path/to/Oblivion.esm" --outdir export/Oblivion.esm
python -m tes4_export.export "C:/path/to/Oblivion.esm" --list-types
python -m tes5_import export/Oblivion.esm -o output/Oblivion.esm -m Skyrim.esm
python -m pytest tests/test_import.py -v          # targeted tests only
```

### Configuration

```json
{ "files": ["Oblivion.esm", "Knights.esp"] }
```

### Caching

- Export text is cached per record type in `export/<filename>/` (`ACTI.txt`,
  `NPC_.txt`, …).
- FormID mappings live in `export/mappings/<filename>.FormID_Mapping.txt`.
- Processing `Knights.esp` reuses the cached `Oblivion.esm` export + mappings.
- `--no-cache` forces a re-export.

## Stages

1. **Export** (`tes4_export`) — reads the TES4 binary, writes KEY=VALUE text,
   one file per record type. Pure dump.
2. **Import** (`tes5_import`) — reads the text, applies every TES4→TES5
   transformation, writes the binary ESM/ESP. Type mapping (CREA→NPC_,
   CLOT→ARMO, LVLC→LVLN), FormID remapping, GRUP hierarchy (CELL/WRLD/DIAL),
   companion records (TXST for LTEX, SNDR for SOUN), LAND binary data.
3. **Assets** (`asset_convert`) — BSA extraction, NIF/texture/SpeedTree/sound
   conversion, LOD generation, BSA packing.

`import_main.py` runs a long phase sequence (Phase 0 pre-scans through Phase 5
dialogue). Ordering matters — e.g. PACK is written in its own Phase 3b2 after
QUST because quest packages need the aliases to exist first, and the ForceGreet
topic binding is patched in after Phase 5. Read the phase comments in
`import_main.py` rather than trusting a copy of the list here.

## Skipped record types

`SKIP_TYPES` in [tes5_import/constants.py](../tes5_import/constants.py) is the
single source of truth. Currently skipped: ROAD, SCPT, SKIL, BSGN, RACE, MGEF,
CSTY, IDLE, GMST, REGN, EYES, HAIR.

Notably **converted** (do not assume otherwise): GLOB, CLAS, CLMT, WATR, PACK.
PACK is converted in its own phase (3b2, after QUST) rather than via the generic
dispatch.

**WTHR is NOT converted on `master` as of 2026-07-26.** A `convert_WTHR` dispatch
entry exists in `constants.py`, but the work that actually enables weather
conversion lives on **another branch and is not merged** — don't read the
dispatch entry as proof the feature is live, and don't "fix" docs that describe
weather as unconverted (e.g. the WTHR row in
[skse_conversion_audit.md](skse_conversion_audit.md)). CLMT stays converted
regardless: weather is only reachable via WRLD → CNAM → CLMT → WLST, so CLMT is
the chain that the branch's WTHR records will hang from.
GMST is skipped wholesale *except* the four ambient-dialogue pacing settings in
`AMBIENT_GMST_OVERRIDES`, which exist in both engines with identical meaning.

Conditions whose params reference a skipped type must be translated (RACE →
Skyrim race via `RACE_MAP` in `dialog_conditions`) or dropped. A dangling param
can never pass, and the CK warns "Unable to find … TESForm in
TESConditionItem Parameter Init".

## Text export format

Records are delimited by `---RECORD_BEGIN---` / `---RECORD_END---`. Each line is
`KEY=VALUE`. Escapes: `\\`, `\"`, `\n`, `\r`, `\t`. `#` starts a comment.
FormIDs are 8-digit hex in load-order form. Arrays use indexed keys
(`Item[0].FormID`, `Item[0].Count`). `Signature=` carries the original TES4
record type; there are no derived or transformation fields.

```
---RECORD_BEGIN---
Signature=CREA
FormID=00000E35
EditorID=TestDeerDoe
RecordFlags=0
ParentCELL=00012345
ParentWRLD=0000003C
FULL=Deer
Model.MODL=Creatures\\Deer\\Skeleton.NIF
---RECORD_END---
```

## Directory layout

```
TESConversion/
  convert.py              # pipeline orchestrator (all stages)
  gui.py / gui.pyw        # GUI front-end
  conversion_config.json  # file list and settings

  tes4_export/            # TES4 binary -> KEY=VALUE text (pure dump)
    tes4_reader.py        # mmap-based binary reader
    export.py             # export CLI
    text_reader.py        # parse text exports back to dicts
    record_types/         # per-type field emitters
      common.py items.py equipment.py actors.py world.py dialog_misc.py

  tes5_import/            # text -> TES5 binary (all transformations)
    constants.py          # lookup tables, dispatch maps, SKIP_TYPES
    writer.py             # TES5 binary packing (records, groups, headers)
    import_main.py        # phase orchestrator
    pack_converter.py     # PACK -> TES5 template instances
    pgrd_to_navm.py       # PGRD -> NAVM
    navi_builder.py       # top-level NAVI
    navmesh/              # corridor navmesh generator
    overrides.py          # override conversion (plugins with masters)
    export_diff.py master_manifest.py override_builder.py override_merge.py
    record_types/         # per-type converters
      common.py items.py equipment.py actors.py world.py dialog_misc.py

  script_convert/         # TES4 script -> Papyrus
    converter.py pipeline.py cross_ref.py say_durations.py static_scripts/

  asset_convert/          # asset pipeline
    nif_converter.py      # NIF conversion (strips, textures, bones, collision)
    collision.py          # Havok rigid bodies, shapes, materials
    cms.py cms_builder.py mopp.py     # compressed mesh shapes + MOPP
    skin_retarget.py      # Oblivion Bip01 -> Skyrim NPC bones
    skyrim_overrides.py   # bone mapping, BSX flags, biped slots
    skyrim_assets.py      # vanilla asset fetch (cache / BSA auto-extract)
    sse_nif.py            # SSE NIF read -> LE graph bridge
    bsa_extract.py bsa_pack.py asset_pipeline.py
    spt_parser.py spt_generator.py spt_converter.py   # SpeedTree

  native/                 # C++ extensions (grow.cpp -> _navgrow_native)
  external/               # third-party binaries (see README license table)
  tests/ tools/ docs/ references/ temp/
  export/                 # cached exports (gitignored)
  output/                 # converted plugins (gitignored)
```

## Verifying output in SSEEdit

```powershell
if (Test-Path SSEEdit_log.txt) { Remove-Item SSEEdit_log.txt -Force }
$tes5 = "C:\Program Files (x86)\Steam\steamapps\common\Skyrim Special Edition\Data"
$tp = "temp_plugins.txt"
"*Skyrim.esm`n*Oblivion.esm" | Set-Content $tp -Encoding UTF8
$args = "-P:`"$tp`" -D:`"$tes5`" -autoload -IKnowWhatImDoing `"Oblivion.esm`""
Start-Process -FilePath ".\sseEdit.exe" -ArgumentList $args -WorkingDirectory (Get-Location).Path
```
