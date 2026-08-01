# Python Tools Reference

Linked from [CLAUDE.md](../CLAUDE.md). Command reference for the pipeline's
Python modules and `tools/` debug utilities.

## tes4_export (export pipeline)
- **Export**: `python -m tes4_export.export "path/to/Oblivion.esm"` — Pure TES4 binary dump to KEY=VALUE text
- **Pipeline**: `python convert.py` — Full export→import pipeline
- **List types**: `python -m tes4_export.export "path/to/Oblivion.esm" --list-types`
- Export performance: ~8s to parse 1.17M records from Oblivion.esm, ~36s total with write

## tes5_import (import pipeline)
- **Import**: `python -m tes5_import export/Oblivion.esm -o output/Oblivion.esm -m Skyrim.esm` — TES4 text → TES5 binary ESM/ESP
- **Tests**: `python -m pytest tests/ -v`
- Import performance: ~28K records converted from Oblivion.esm, 413MB output, 0 errors

## asset_convert (asset pipeline)
- **NIF conversion**: `python -m asset_convert.nif_converter <src_dir> <dst_dir> [--workers N]` — Full Oblivion→Skyrim NIF conversion (strips, textures, bones, collision, skin retarget). See [nif_conversion_notes.md](nif_conversion_notes.md) for the deep implementation notes.
- **SKIP_PATHS**: `asset_convert/nif_converter.py::SKIP_PATHS` — frozenset of path segments to skip during batch conversion (default: `menus`, `creatures`, `trees`). Trees are skipped because TREE records map model paths to `speedtrees/` via spt_converter — the original `trees/` geometry NIFs are not referenced at all.
- NIF conversion stats: 8032 source NIFs from Oblivion BSAs. 7380 v20 files converted (91.9%). 650 v10/v4 files copied as-is.
- **Book inventory art (INAM)**: `python -m asset_convert.book_inam <plugin> [--extract-dir export] [--output-dir output] [--templates-dir "references/Skyrim Meshes"] [--skyrim-data <SSE Data dir>] [--workers N]` — bakes each distinct BOOK model's Oblivion textures onto the vanilla Skyrim reading rigs and emits `meshes/tes4/clutter/books/inv/<base>.nif` + baked DDS. Runs automatically in `convert.py` phase_assets. See [nif_conversion_notes.md](nif_conversion_notes.md#book-inventory-art-inam-reading-rigs--books-were-invisible-with-no-text-when-opened-solved-2026-07-18).
- **SpeedTree (.spt) conversion**: `python -m asset_convert.spt_converter <trees_src> <nif_dst> [--export-dir <dir>]` — see [nif_conversion_notes.md](nif_conversion_notes.md#speedtree-spt-conversion) for the full algorithm.
- **Preview/iteration tool**: `python tools/spt_preview.py <spt_or_dir> [--views 0,90] [--out dir]` renders generated tree geometry to PNG with real leaf textures beside Oblivion's own billboard render for A/B comparison.
- **BSA packing**: `python -m asset_convert.bsa_pack <plugin.esm> [--output-dir output] [--compress-textures] [--size-limit BYTES]` — packs `output/<plugin>/` into Skyrim SE BSAs via `external/bsarch/BSArch.exe`. Normally produces `Oblivion.bsa` (meshes + every other non-texture dir, incl. `sound/` and `scripts/`) and `<stem> - Textures.bsa`.
  - **2 GiB limit + overflow ESLs**: BSA file-data offsets are 32-bit, so an archive cannot exceed 2,147,483,648 bytes (`BSA_HARD_LIMIT`). Files are binned in path order under `BSA_SIZE_LIMIT` (the hard limit minus a 64 MiB budget for the BSA header/folder/file/name tables, which are *not* counted in the raw payload sum). Overflow spills into extra archives.
  - Skyrim only auto-mounts `<PluginStem>.bsa` and `<PluginStem> - Textures.bsa` for plugins in the load order, so each overflow archive is paired with a generated record-free dummy **ESL** loader whose stem matches it: `oblivion_loader.esl` mounts `oblivion_loader.bsa` *and* `oblivion_loader - Textures.bsa`; then `oblivion_loader_1.esl`, etc. The ESL flag (`0x0200`) keeps loaders out of the 255-plugin limit. **The loaders must be enabled in the load order** or their assets are missing in-game.
  - Stale overflow `.bsa`/`.esl` files from a previous, larger run are swept on each run — otherwise a later run could re-create `oblivion_loader.esl` on top of a stale `oblivion_loader.bsa` and silently serve assets from the old conversion.

## tools/ (debug & analysis)

**Before you build anything bespoke, look here first** — the repo already ships
~95 tools and one probably covers your question. Extend the existing tool rather than writing a
parallel script; when you add a new one, add its entry to this file in the same
pass (see the workflow rule in [CLAUDE.md](../CLAUDE.md#tools-first)).

**Rule: `tools/` is NOT for one-off scripts.** Only multi-use tools that take
arguments belong here — several functions per file, general output. A throwaway
with hardcoded paths goes in `temp/` instead.

Every tool in `tools/` is listed below; run any with `--help` for the full flag
set. Entries marked **★** are the primary entry point for their area.

Four files here are **support modules, not CLI tools**, and correctly have no
argparse — don't "fix" them: `navmesh_probe.py` (shared cell loading for 14
navmesh tools), `papyrus_emulator_load.py` and `papyrus_interp.py` (loader +
interpreter for `papyrus_emulator.py`), and `quest_walkthrough_tes5.py` (the TES5
half of `quest_walkthrough.py`).

### Plugin / record inspection
- **★ Plugin verify**: `python tools/verify_plugin.py <plugin.esp> [--check] [--dump --verbose] [--type NPC_] [--formid 00012345] [--edid <edid>]` — record counts + version info; `--check` finds missing OBND, wrong form version, bad CELL DATA size, NPC_ race/ACBS problems.
- **TES5 ESM reader**: `python tools/tes5_esm_reader.py <esm> [--outdir dir] [--types TYPE ...]` — TES5 binary to per-type KEY=VALUE text, same format as the TES4 export.
- **★ Semantic ESM diff**: `python tools/esm_diff.py A.esm B.esm` — compares group by group, then record by record; distinguishes real differences from mere reordering. **The determinism gate** for the byte-reproducibility contract.
- **GRUP tree dump**: `python tools/esm_group_tree.py <esm> --formid <fid>` — prints the group nesting around a record, including sibling groups of its children group. Use when a record lands in the wrong bucket or nesting level.
- **★ Owned-group anchors**: `python tools/esm_group_anchors.py <plugin> [--verbose]` — every type-1/6/7 GRUP whose owning record does not immediately precede it. Those groups are attached to nothing, so the engine indexes none of their contents while the file still loads and still looks right in xEdit. Exit code 1 if any orphan is found, so it works as a gate. **Run this on any plugin with masters**: it is how the lost DLCBattlehornCastle worldspace overrides (13 orphans) and Translation.esp's 958 were found. Converted masters should report 0 (Oblivion.esm 37,250 anchored, Nehrim.esm 9,519).
- **VMAD probe**: `python tools/vmad_probe.py <esm> <script> [--types SIG,SIG] [--props]` — which records carry a given Papyrus script, and its bound property values. The script name is **positional** (not `--script`). Answers "is this on the base NPC_ or the placed ACHR?" and "is this property actually bound, or None at runtime?" — a declared property missing from `--props` output is unbound.
- **CK warning fixes**: `python tools/verify_ck_fixes.py <esm>` — re-checks the 2026-07 CK_WARNINGS buckets directly in the binary (index-00 overrides, LCTN order, aimed MGEF clones, SPEL cast type, 8-byte LVLO, negative counts).
- **Override audit**: `python tools/override_audit.py export/<Plugin>` — per record type, what the override path does with every record and every authored field with no output mapping. Runs without a conversion.
- **TES4 CTDA dump**: `python tools/tes4_ctda_dump.py --dial <edid_or_fid> [--export export/Oblivion.esm]` — decoded TES4 conditions straight from the text export.
- **CTDA param-type table generator**: `python tools/gen_ctda_param_types.py <path>/wbDefinitionsTES5.pas -o tes5_import/ctda_param_types.py [--func N]` — **generates** the FormID-vs-value param table from xEdit's `wbConditionFunctions`. Never hand-edit the output; see [package_ai_contracts.md](package_ai_contracts.md).
- **Vanilla MGEF table generator**: `python tools/gen_vanilla_mgef_table.py` — regenerates `tes5_import/vanilla_mgef_data.py` from the Skyrim.esm MGEF dump (for synthesized "aimed variant" effects).
- **Alias-fill test plugin**: `python tools/make_alias_test_esp.py` — builds a minimal ESP (+ .seq) of five StartGameEnabled quests to factorize quest-alias fill failures.
- **Body-slot patch**: `python tools/patch_body_slots.py <plugin>` — generates a patch making another plugin's body items compatible with biped slot 44 and the split body meshes.

### Magic, packages, outfits
- **★ Magic audit**: `python tools/magic_audit.py <export_dir> [--by-record] [--unmapped-only] [--assets]` — MGEF coverage: how many source effects convert as our OWN MGEF vs fall back to a vanilla alias vs are DROPPED, the per-archetype breakdown, which per-MGEF source data still has no destination, and records that lose ALL effects. Also reports **phantom table keys** — entries matching no MGEF in ANY export, i.e. coverage that can never fire (17 of the old alias table's 100 were exactly that). Run on **both** `export/Oblivion.esm` and `export/Nehrim.esm` (Nehrim exercises 16 mod-authored codes). See [magic_conversion_plan.md](magic_conversion_plan.md).
- **★ PACK validate**: `python tools/pack_validate.py <esm> [--ref "<SSE>/Data/Skyrim.esm"] [--summary]` — the engine load/run contract: PKDT type/size, PKCU size, count-vs-ANAM agreement, UNAM presence/order, PLDT/PTDA type legality. Flags the known type-9 bug on a synthetic record, so a clean run is meaningful.
- **PACK audit**: `python tools/pack_audit.py <export_dir>` — which template each `PKDT.Type` picks, how many take each path, and which packages DROP their target/location.
- **PACK template dump**: `python tools/pack_template_dump.py` — vanilla template roots (`PKDT.Type=19`) and their public data-input signatures.
- **Outfit trace**: `python -m tools.trace_outfit <export_dir> <edid_or_fid> [--contains] [--no-masters]` — how a TES4 inventory splits into OTFT (worn) + CNTO (carried), using the real `tes5_import.outfits` logic; shows the biped slot each wearable claims and which ones lost a slot conflict and were demoted to loot. For a plugin with masters it indexes the masters' exports automatically (a dependent plugin wears its master's wardrobe — without them every entry looks like carried loot); `--no-masters` gives the plugin-only view.

### Dialogue & quests
- **★ Quest walkthrough emulator**: `python tools/quest_walkthrough.py --export export/Oblivion.esm --esm output/Oblivion.esm/Oblivion.esm --scripts output/Oblivion.esm/scripts --seq output/Oblivion.esm/seq/Oblivion.seq [--quest EDID] [--md report.md]` — symbolically plays every quest to a fixpoint over the CONVERTED data and diffs stage reachability against the TES4 export, naming the record/script that breaks each lost stage. `quest_walkthrough_tes5.py` is its TES5 half. See [QUEST_AUDIT.md](QUEST_AUDIT.md).
- **Quest runtime trace**: `python tools/quest_runtime_trace.py --quest <edid> --stage N` — what actually FIRES while a quest *sits* at a stage (packages, topics, fragments). Complements the walkthrough's "can the graph advance?" with "why is nothing happening?"
- **★ Skyrim dialog emulator**: `python tools/dialog_emulator.py <esm>` — evaluates which topics/responses each NPC would see under Skyrim's real condition rules.
- **★ Oblivion dialog emulator**: `python tools/oblivion_dialog_emulator.py <export_dir>` — the TES4 counterpart, read from the export (never the converted output). Pair the two to measure what a conversion loses.
- **Dialog walkthrough / chains**: `python tools/dialog_walkthrough.py <esm> [--check-chains]` — traces dialog trees and TCLT chain integrity; verifies NPC dialog access.
- **Dialog validator**: `python tools/dialog_validator.py <esm>` — DIAL/INFO/QUST/DLBR/DLVW structural integrity and CTDA patterns against Skyrim.esm conventions.
- **Ambient bark audit**: `python tools/ambient_bark_audit.py <esm>` — how much an NPC can say *unprompted*, vs vanilla. The tool behind [ambient_dialogue_channel_plan.md](ambient_dialogue_channel_plan.md).
- **Say/SayTo topic A/B**: `python tools/say_topic_ab.py --topic <edid>` — which INFO each engine picks for a script-driven topic. Both games select ONE response by walking the list; this shows where they diverge.
- **Bark choice promotion trace**: `python tools/trace_bark_choice_promotion.py <esm>` — which conversation topics a greeting/bark `Choice[]` promotes to a top-level menu topic.
- **Say-timer race sim**: `python -m tools.say_timer_race_sim [--lines N] [--model naive|fixed] [--sweep] [--drop-at CC] [--line-len S] [--pollers N]` — replays a converted Say()-driven conversation over every interleaving of fragment-vs-countdown timing. `--sweep` A/Bs the pre-fix read-modify-write countdown against the guarded one (4/10 vs 10/10). Regresses the 2026-07-25 park/release races.
- **★ Voice audit (folder-level)**: `python tools/voice_audit.py [--esm ..] [--voice-dir ..] [--source-dir ..] [--csv out]` — recomputes every INFO's expected voice files from the BUILT ESM, diffs against disk, classifies misses (NO_SOURCE_AUDIO / PREFIX_MISMATCH / MISSING_IN_VTYP / NOT_ORGANIZED).
- **Voice line table (per-NPC)**: `python tools/voice_line_table.py [--npc <edid>] [--status MISSING INVALID] [--csv out]` — one row per (INFO, response, speaker), using the WRITTEN VTCK folder the engine actually looks up, with structural validation (FUZE/lip/RIFF).
- **CharacterGen debug**: `python tools/chargen_debug.py [--no-compile] [--revert]` — instruments the converted CharacterGen scripts with exhaustive SKSE logging into a single diagnosable log (`Logs/Script/User/TES4CharGen.log`), then compiles them. Covers the whole quest, so it should not need extending again: the 4 polling speakers (Renault/Glenroy/Baurus/Uriel) with conversation state + package dumps, the stage-18 switch probe, the quest script's stage/timer ticks, every QUST stage fragment, 18 INFO fragments, the Ambush A/B/C + generic assassins (`enable`/`3d`/`dead`/`combat`/package per tick, plus PKGSTART/PKGCHANGE/PKGEND), and the trigger zones, gates and doors that gate stage progression (traced *before* their own guards, so "fired but rejected" is distinguishable from "never fired"). `--revert` re-runs `convert.py --scripts-only` for clean output.

### Papyrus / script conversion
- **★ Compile Papyrus**: `python tools/compile_papyrus.py [--src DIR] [--out DIR] [--headers DIR] [--errors-detail]` — compiles the converted `.psc` and reports categorized error statistics. The pass-rate gate.
- **CK compiler check**: `python tools/ck_compile_check.py` — compiles with Skyrim's **bundled CK** `PapyrusCompiler.exe`, which is stricter than the MIT compiler in `external/` and surfaces errors it misses.
- **★ Papyrus conversation emulator**: `python tools/papyrus_emulator.py` — runs the CONVERTED Papyrus against a model of Skyrim's runtime, so a scripted conversation can be proven to advance before loading the game. `papyrus_emulator_load.py` wires in the real output; `papyrus_interp.py` is the statement interpreter.
- **Reserved-word list**: `python tools/gen_papyrus_reserved.py` — regenerates `script_convert/papyrus_reserved.txt` from Skyrim's `Scripts.zip` (the compiler rejects any identifier matching a visible script name).
- **OBSE convertibility audit**: `python tools/obse_convertibility_audit.py <export_dir>` — which OBSE/Oblivion functions the original source actually uses, and whether each converts.
- **Script command census**: `python tools/nehrim_script_command_census.py <export_dir>` — tokenises every `SCPT.SCTX` + `INFO.ResultScript` statement and ranks the leading commands. Use to prioritise `FUNCTION_MAP` work.
- **★ Original vs converted pairing**: `python tools/script_pair.py <EditorID> [...]` — prints a SCPT's TES4 source (unescaping the `\r\n`/`\t` in the export's `SCTX` field) beside the emitted `output/.../TES4_<EditorID>.psc`. `--orig-only` / `--conv-only` to print one side, `--file PATH` to batch a sample, `--list-quest` / `--list-all` to enumerate EditorIDs (`--list-quest` = the `SCHR.Type=1` quest scripts), `-f` to pick a plugin other than `Oblivion.esm`. This is the workhorse for [quest_script_conversion_audit.md](quest_script_conversion_audit.md) — use it instead of hand-rolling a `split('---RECORD_BEGIN---')` one-liner, which silently leaves `\t` escapes in the source and misparses the body.
- **Enum actor-value check**: `python tools/check_enum_actor_values.py <scripts/source dir> [...]` — flags any `Set/ForceActorValue` writing a value outside the TES5 enum range for Aggression (0-3), Confidence (0-4), Assistance (0-2), Mood (0-8) or Morality (0-3). TES4 stores these on 0-100; an out-of-range write is rejected by the engine ("attempt made to set illegal value") and silently leaves the trait unchanged. Exits non-zero if any are found.

### Engine forensics (exe analysis)
- **★ Skyrim disassembler**: `python tools/skyrim_disasm.py [--rtti <class>] [--rva 0x...]` — RTTI class/vtable lookup + disassembly of the unpacked **GOG** `SkyrimSE.exe`.
- **★ Skyrim xrefs**: `python tools/skyrim_xref.py --pattern '<regex>'` — starts from the other end: which code references a given string/address. Pair with `skyrim_disasm.py`.
- **★ Address Library**: `python tools/address_lib.py --log <crash.log>` — **start every CTD here.** Crash logs come from the user's *Steam* build (1.6.1170); only the *GOG* build (1.6.659) disassembles. `--log` translates every `-> <id>+0x<off>` frame in the log into GOG RVAs in one shot, ready to paste into `skyrim_disasm.py`. Also does single lookups: `--id N [--offset 0x..]`, or `--rva 0x.. --from 1.6.1170` to find the owning function. The versionlib v2 decoder is delta-coded — kinds 6/7 are u16/u32 (**not** u64) and the 0x08 high-nibble flag divides the previous offset by pointer size before the delta and multiplies back after; the loader now hard-fails if the stream doesn't consume the file exactly, since a desync silently yields plausible-looking wrong addresses.
- **Skyrim dialogue tables**: `python tools/dialog_engine_extract.py` — the engine's authoritative dialogue tables, rather than xEdit's description of them. Refuses to run against a packed (Steam) build.
- **Oblivion dialogue tables**: `python tools/oblivion_engine_extract.py` — the TES4 counterpart.
- **Oblivion exe probe**: `python tools/oblivion_exe_probe.py` — reusable read-only probe of the unpacked 32-bit `Oblivion.exe` (engine behavior, not record layout).
- **CK setting defaults**: `python tools/ck_settings_dump.py [--filter NavMeshGeneration]` — recovers `CreationKit.exe`'s compiled-in INI defaults by decoding each dynamic-initializer thunk. Works for any `Name:Section` setting.
- **CK string xrefs**: `python tools/ck_strref.py --pattern '<regex>'` — indexes every rip-relative `.text` reference to matching strings in one pass. Log strings come out in execution order, which recovers pass ORDERING from a stripped binary.
- **UESP lookup**: `python tools/uesp_lookup.py --find <title-regex> | --page <exact title> | --grep <body-regex> [--title-filter RE] [--context N] [--limit N]` — streams the 1.1 GB / 463k-page UESP+CK wiki dump in `references/UESP`. **Use this instead of WebSearch/WebFetch**, which 403. Takes FLAGS, not a positional query. `--grep` over the function tables is the quickest way to settle an opcode's arity and parameter types (`--grep GetDetectionLevel --title-filter "Oblivion Mod"` gives opcode, param count and receiver kind). Output is forced to UTF-8; before that a single arrow or accented name in a matched line raised `UnicodeEncodeError` on the Windows console and killed the search mid-result.

### NIF / mesh
- **★ TES4 NIF analyzer**: `python tools/tes4_nif_analyzer.py <nif_or_dir> [--outdir dir] [--max N] [--bbox]` — dumps Oblivion NIF structure to text; `--bbox` gives world-space geometry bounds.
- **★ TES5 NIF analyzer**: `python tools/tes5_nif_analyzer.py <nif_or_dir> [--outdir dir] [--max N]` — same format for Skyrim NIFs.
- **NIF block scanner**: `python tools/nif_block_scan.py <dir> [--has TYPE]... [--any TYPE...] [--histogram] [--workers N]` — header-only binary block-type search (ripgrep skips binaries). `--has X --has Y` is the "0 vanilla files pair X with Y" diagnostic.
- **Texture path swapper**: `python tools/nif_retex.py <nif> --map old=new` — raw-byte texture path swap that never round-trips through PyFFI, so it is safe on files PyFFI cannot fully parse.
- **Skin partition dump**: `python tools/skin_partition_dump.py <nif>` — NiSkinInstance/NiSkinData/NiSkinPartition per-partition bone/vertex/triangle counts plus the consistency checks the renderer assumes.
- **Particle chain dump**: `python tools/psys_dump.py <nif> [...] [--convert]` — everything determining particle visibility (BSXFlags, controller chain, modifiers, emitter params, NiPSysData, shader/alpha). `--convert` dumps the converter's in-memory RESULT.
- **Inventory-marker survey**: `python tools/inv_marker_survey.py <vanilla_meshes_dir> [--max N] [--multiaxis] [--filter substr] [--detail CONV]` — scores every BSInvMarker convention (Euler order x sign x camera axis) against vanilla. How `asset_convert/inv_marker.py`'s convention was derived.
- **Armor fit metrics**: `python tools/armor_fit_metrics.py <source_nif> <converted_nif>` — per-block edge distortion, body clearance, and explosion detection for armor retargeting.
- **SpeedTree preview**: `python tools/spt_preview.py <spt_or_dir> [--views 0,90] [--out dir]` — renders generated tree geometry to PNG with real leaf textures, beside Oblivion's own billboard render.
- **NIF LOD preview**: `python tools/nif_lod_preview.py <nif>` — flat-shaded two-view PNG render for validating LOD decimation.

### Collision & Havok
- **★ Collision sanity**: `python tools/collision_sanity.py <nif_or_dir_or_listfile.txt> [--constraints] [--geometry] [--quiet]` — walks all bhk blocks: NaN/Inf sweep, degenerate hulls/lists, non-unit constraint axes, hinge limit ordering.
- **★ MOPP validator**: `python tools/mopp_validator.py <nif_or_dir> [--verbose|--summary|--histogram|--workers N]` — MOPP walk cleanliness **and** exact terminal-key-set == shape-key decode.
- **CMS inspector**: `python tools/cms_inspector.py <nif>` — every scalar field of bhkCompressedMeshShape(Data) + per-chunk headers, for field-by-field diffs against vanilla.
- **Collision winding**: `python tools/collision_winding.py <nif_or_dir> [--converted] [--floor-orientation] [--floor-regress] [--ab REF_TREE] [--max N] [--top N] [--workers N]` — audits inverted collision winding (the "fall through the floor" bug). **`--ab <ref_tree>` is the one that gives a real number**: it scores the shipped repair triangle-by-triangle against the same meshes in another tree (e.g. `--ab export/Oblivion.esm/meshes` for a Nehrim scan), where the vanilla winding is ground truth, and reports recall plus how many already-correct triangles the repair breaks. `--floor-regress` measures the in-game invariant — walkable floors turned into fall-throughs — and is the right safety check on a vanilla tree, because counting "triangles changed" there is misleading (vanilla’s SI bridges really are inconsistently wound: `dementiabridge01` has 242 of 324 shared edges disagreeing). `--floor-orientation` reports whether each mesh’s lowest near-horizontal surface faces up or down, catching uniformly reversed floors. The default scan only detects a MIXED up/down edge pair and misses those entirely.
- **Havok constraint dump**: `python tools/havok_constraint_dump.py <nif>` — rigid-body filter/solver fields and full constraint descriptors that the NIF analyzers hide.
- **Ragdoll validate**: `python tools/ragdoll_validate.py <skeleton_nif>` — runs the real `hkx_ragdoll.extract_ragdoll` and checks the ragdoll at bind pose (constraint tree, limits, motors).
- **Creature vanilla A/B**: `python tools/creature_vanilla_ab.py` — builds test ESPs repointing a generated creature RACE at vanilla asset layers (behavior project / skeleton / body NIF) to bisect which generated layer breaks.
- **CREA project gap**: `python tools/crea_project_gap.py <plugin> [master ...]` — audits how many CREA records actually map to a converted creature project. Misses ship as BASE SKYRIM creatures (`resolve_creature_race` aliasing), and the report splits them into "folder owned by a MASTER's project" vs "in neither" (never converted — usually a creature folder nested outside `meshes\creatures`). Use this whenever converted creatures appear in-game as vanilla Skyrim ones.
- **Animation cache validate**: `python tools/animcache_validate.py` — validates merged `animationdatasinglefile.txt` / `animationsetdatasinglefile.txt` against ck-cmd's exact grammar.
- **KF animation explorer**: `python tools/kf_animation_explorer.py --build-cache` / `--skeleton` — explores the Oblivion `.kf` corpus and skeleton for FK retargeting.

### Navmesh
Correctness first, then quality, then geometry probes. See
[navmesh_corridor_redesign.md](navmesh_corridor_redesign.md) and
[performance_notes.md](performance_notes.md).

- **★ CheckNavMesh (CK rule port)**: `python tools/navmesh_check.py <esm> [--verbose] [--rule NAME] [--csv out.csv] [--no-doors] [--all-portals]` — the CK's own validation rules against WRITTEN NVNM: bad indices, degenerate tris, duplicate/asymmetric edges, downfacing normals, portals to missing meshes/worldspaces, NAVI registration, doors with no Door Triangle, 3500/5000 tri warnings. **The correctness gate**; exit 1 on any non-`_WARN` defect. Baseline: Skyrim.esm 225 findings / 2.67M tris, Dawnguard 46 — far more than that on vanilla means the TOOL is wrong.
- **★ CheckNavMesh on generated cells**: `python tools/navmesh_cell_check.py <EditorID> [...]` — same rules against fresh `build_navmesh` output, no ESM needed. Seconds per cell, so generator fixes iterate directly.
- **★ Why did THIS cell produce no navmesh**: `python tools/navmesh_job_trace.py --plugin <name> --cell <FormID> [--all] [--uncached] [--start N] [--max N]` — runs the real `convert_PGRD` job **in-process** so you get the traceback. `navm_worker.run_job` catches per-cell errors (one bad cell must not abort a 2,900-job pool) and pool workers run under `pythonw.exe` where stdout goes nowhere, so this is the only way to see the actual exception behind an import's `WARNING: N cells produced no navmesh`. Takes the raw FormID from the error message — no audit index needed, unlike `navmesh_cell_check.py`. `--all` flushes each job's identity BEFORE the call, so a **hard crash** (a C++ exception escaping the native extension aborts the process with no traceback, appearing in the parent only as `BrokenProcessPool`) leaves the culprit as the last line printed — this is how Nehrim's cell `011E4FEC` was found. `--uncached` limits the run to jobs with no geometry-cache entry, i.e. the ones that actually recompute.
- **★ Reachability**: `python tools/navmesh_reach.py <esm> --from-ref <fid> --to-ref <fid> [--cell <fid> --components]` — builds the (mesh, component) graph over NVNM edge links + door XTEL joins and answers "can an actor path A to B?" **Use FIRST when a quest NPC will not travel** — it separates package bugs from navmesh bugs.
- **★ Preview renderer**: `python tools/navmesh_preview.py --cell <FormID_or_EditorID> [--out png] [--size N]` — top-down render over the collision layer, drawing the real WALLS. The primary iteration tool. (`navmesh_probe.py` holds the shared cell-loading helpers these tools agree on.)
- **Quality audit (many cells)**: `python tools/navmesh_audit.py [--interiors N]` — sweeps a batch and reports the defects that matter. The quality metric, vs `navmesh_check`'s correctness.
- **Raw record dump**: `python tools/navmesh_dump.py <esm> [--navi|--navm] [--nvnm-decode] [--max N]` — decompresses and decodes real NAVI/NAVM/NVNM for format verification.
- **Connectivity (cross-cell)**: `python tools/navmesh_connectivity.py <esm>` — are cell navmeshes actually stitched? Without edge links each cell is an island.
- **Component invariant**: `python tools/navmesh_component_audit.py` — checks "one connected pathgrid implies one connected navmesh component".
- **Bottlenecks**: `python tools/navmesh_bottleneck.py --cell <id>` — narrow joints where removing a few shared edges disconnects the surface (passes the one-component test yet is unnavigable).
- **Walk fitness test**: `python tools/navmesh_walk_test.py --cell <id>` — can an NPC actually walk from the top floor out the front door? Component counts are necessary but not sufficient.
- **Coverage**: `python tools/navmesh_coverage.py --cell <id>` — every point of the uncut corridor union must be covered by EXACTLY ONE triangle at that point's height.
- **Slope / continuity**: `python tools/navmesh_slope_check.py --cell <id>` — the defect that breaks stairs: right ground, wrong HEIGHTS.
- **Surface residual**: `python tools/navmesh_surface_residual.py --cell <id>` — how far the mesh floats above/sinks below real collision (the pathgrid hovers).
- **Triangle checks**: `python tools/navmesh_tri_check.py --cell <id> [--all] [--quiet] [--csv out]` — per-triangle slope, vertical extent, edge lengths/ratio, aspect, area, and FLOAT above collision, plus a **WRONG-FLOOR** summary (pathgrid nodes whose nearest mesh vertex is a storey off — the stacked-storey sampling failure). `python tools/navmesh_triquality.py --cell <id>` gives distribution stats only.
  - *Consolidated 2026-07-26*: absorbed `navmesh_diag.py` (pre-corridor, no argparse, hardcoded default cell). Its STEEP/ISLANDS checks were already covered here and by `navmesh_audit`/`navmesh_component_audit`; its unique WRONG-FLOOR check moved into `wrong_floor()` here and the duplicate was deleted.
- **Seam probe**: `python tools/navmesh_seam_probe.py` — cross-cell seam coverage for exterior PGRDs, straight from the export, before a full rebuild.
- **Width grow**: `python tools/navmesh_width_ab.py --cell <id>` (Phase-2 grow vs Phase-1 fixed width), `navmesh_grow_check.py` (spatial defects grow can introduce, against real collision), `navmesh_grow_verify.py` (**native `grow.cpp` must match the pure-Python original** — behaviour parity, not just speed).
- **★ Profiler**: `python tools/navmesh_profile.py --cells <A,B> --stages [--top N]` — stage wall-clock + cProfile over the whole CORRIDOR path: `world.gather_cell_geometry`, the surface samplers (`corridor._surface_sampler`, `corridor_grow.wall_slab_sampler`), strip building (`_build_corridor_strips`, with nested `grow_batch(native)` and `_plan_stations`), `corridor_doors.door_footprints`, `corridor_union.build_union_mesh` (+ its 7 nested stages) and `corridor_clean.finalize`/`decimate`. Also prints the Amdahl ceiling for optimising the union. Sub-stage rows are INDENTED because they nest — only top-level rows may be subtracted, or "(other)" goes negative.
  - *Consolidated 2026-07-26*: absorbed `navmesh_corridor_profile.py`, which existed only because this tool once wrapped the deleted voxel/region/spanmesh stages and reported 100% "(other)". Its four unique wraps were folded in; the duplicate was deleted.

### Terrain / LOD / world
- **Terrain LOD renderer**: `python tools/terrain_lod_render.py --esm <esm> --worldspace <name> --cell X Y --radius R` — side-by-side hillshade + composited diffuse (incl. water murk).
- **LOD NIF inspector**: `python tools/lod_nif_inspect.py <btr_or_bto>` — geometry + shader facts for `.btr`/`.bto`, for field-by-field vanilla comparison.
- **LOD tile debugger**: `python -m tools.terrain_lod_tile_debug --tiles LEVEL,TX,TY ... [--png-dir temp]` — regenerates specific tiles in-process, reports water quad counts, dumps PNGs.
- **LOD texture probe**: `python -m tools.terrain_lod_tex_probe [--cell X Y]` — audits LTEX to TXST to dds resolution (ok/missing/loadfail) and per-cell BTXT/ATXT layers.
- **Cell mesh lister**: `python tools/cell_meshes.py <export_dir> --cell <FormID_or_EditorID> [--cell ...] [--meshes-only]` — placed base objects + model paths; multiple `--cell` prints the mesh-set intersection. Finds the suspect mesh when one cell crashes.
- **Cell grass lister**: `python tools/cell_grass.py <export_dir> --cell <id>` — walks CELL to LAND to LTEX to GRAS to list the grass types a cell can spawn.
- **NPC skin census**: `python -m tools.census_npc_skin [--dump references/Skyrim.esm] [--race NordRace]` — joins RACE tint-mask definitions with NPC_ tint layers to report the colors/TINV/QNAM vanilla NPCs actually use. Source of `_RACE_SKIN_TONES`.
- **Actor health audit**: `python tools/actor_health_audit.py <built.esm> <export_dir> [--limit N] [--all]` — recomputes each converted actor's in-game health the way the engine does (`RACE.StartingHealth + ACBS.HealthOffset + (Level-1)*fNPCHealthLevelBonus`, bonus 5.0) and compares it with the TES4 `DATA.Health` pool, which is already final. Reads the **built** esm, so it catches anything the pipeline does after ACBS is packed. Reports exact/mismatch counts plus a **SPAWN-DEAD** count (positive TES4 health that lands ≤0 in game — the symptom of a broken race-base/offset pairing). Exits non-zero on any mismatch, so it works as a regression gate. Current: Oblivion 100.0% exact, Nehrim 99.9% (the 3 outliers are 1M–10M HP invulnerability sentinels that exceed the int16-offset + U16-level ceiling of ~360k, still ~59x the toughest real enemy).

- **LVLN shell template flags**: `python tools/census_lvln_shell_flags.py [esm]` — censuses `ACBS.Template Flags` on every NPC_ whose `TPLT` is an LVLN (the shells `leveled_actors.py` mints for placed LVLCs), decoding each bit by name and flagging any shell that inherits **Attack Data (0x0800)**. Defaults to vanilla Skyrim.esm; pass a built esm to audit our own output. Vanilla baseline: 502/508 shells are `0x0023` (Traits|Stats|AI Packages), 6 are `0x0000`, **none** inherit Attack Data. That bit is a load-time CTD when the template has no ATKD — Skyrim's template-copy null-checks the old pointer but not the new one (GOG `0x14037b4aa`).

### Release / repo
- **Release notes**: `python tools/release_notes.py [--from <rev>] [--to <rev>] [--tag N.NN] [--output FILE]` — commits since the previous `MAJOR.MM` tag plus the GUI pipeline steps those changes require re-running. Defaults to *latest tag → HEAD*. Used by `.github/workflows/tag-on-push.yml` to write the annotated tag message. The path→step map (`RULES`) mirrors `gui.py`'s `STEPS` table; **add a rule whenever a new top-level package lands**, otherwise unmapped paths conservatively select every step.

## verify_plugin.py

Also usable directly as a module entry point (same tool as **Plugin verify** above):
- **Summary**: `python tools/verify_plugin.py <plugin.esp>` — record counts, version info
- **Integrity checks**: `--check` — missing OBND, wrong form version, CELL DATA size, NPC_ race/ACBS
- **Record dump**: `--dump --verbose` — hex dump of all subrecords
- **Filter**: `--type NPC_`, `--formid 00012345`, `--edid SomeEditor`
