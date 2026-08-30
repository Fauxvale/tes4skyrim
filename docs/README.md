# docs/ — where documentation goes

Sorted by KIND, not by subject: the big files all span several subjects, but an
agent always knows which KIND it just produced.

## Where does my new document go?

1. Not prose (image, icon, data table)? → `assets/`
2. Describes a format or contract that exists independent of our code? → `reference/`
3. Describes work not yet done? → `plans/`, with `Status: PLAN, unimplemented.` on line 2
4. A dated count over a corpus? → `audits/`
5. Otherwise → `notes/`

**Naming:** `lower_snake_case.md`, named for its SUBJECT only. No ALLCAPS, no
`Title_Case`, no dates, and **no kind suffix** — the folder already states the
kind. `audits/aggression_faction.md`, not `..._audit.md`;
`notes/nif_conversion.md`, not `..._notes.md`;
`reference/pipeline.md`, not `..._reference.md`.

**One kind per file.** A dated fix narrative inside a reference doc, or a
"verified correct, do NOT fix" finding inside an audit, belongs in `notes/`.
Split it out rather than appending to whichever file looked closest.

`python tools/validate/doc_links.py --index` checks that every link resolves
and that every doc below appears in this table.


## `reference/`

What a format or contract IS. Stable; no dates, no status.

| Doc | Covers |
|---|---|
| [creature_race_equivalence.md](reference/creature_race_equivalence.md) | Skyrim ↔ Oblivion Creature Equivalence Map |
| [dialogue_engine_contracts.md](reference/dialogue_engine_contracts.md) | Dialogue contracts read out of SkyrimSE.exe |
| [item_swap_table.md](reference/item_swap_table.md) | Oblivion → Skyrim Item Swap Table (MISC + Ingredients/Food) |
| [package_ai_contracts.md](reference/package_ai_contracts.md) | PACK / AI Package & CTDA Engine Contracts |
| [pipeline.md](reference/pipeline.md) | Pipeline Reference — orchestration, caching, layout, export format |
| [prior_art_php_scriptconverter.md](reference/prior_art_php_scriptconverter.md) | PHP ScriptConverter (Skyblivion) — Comprehensive Analysis |
| [python_tools.md](reference/python_tools.md) | Python Tools Reference |
| [record_mapping.md](reference/record_mapping.md) | TES4 → TES5 Record Mapping Reference |
| [script_convert_architecture.md](reference/script_convert_architecture.md) | script_convert/ architecture — read this BEFORE writing any code here |
| [skyrim_commands.md](reference/skyrim_commands.md) | skyrim commands |
| [skyrim_mountable_actor.md](reference/skyrim_mountable_actor.md) | What makes a Skyrim actor mountable |
| [tes4_record_definitions.md](reference/tes4_record_definitions.md) | TES4 (Oblivion) Complete Binary Record Definitions |
| [tes5_binary_format.md](reference/tes5_binary_format.md) | Skyrim SE (TES5/SSE) Binary File Format — Exact Layout |
| [xedit_scripting.md](reference/xedit_scripting.md) | xEdit Scripting Reference (historical) |

## `notes/`

What we LEARNED building it: measurements, engine behaviour, reverted attempts. The DEFAULT.

| Doc | Covers |
|---|---|
| [ambient_dialogue_channel.md](notes/ambient_dialogue_channel.md) | Ambient dialogue channels: diagnosis and plan of attack |
| [ck_exe_as_a_source.md](notes/ck_exe_as_a_source.md) | CreationKit.exe as a disassembly source |
| [ck_log_capture.md](notes/ck_log_capture.md) | Reading the CK log while the CK holds it open |
| [ck_navmesh_generation.md](notes/ck_navmesh_generation.md) | CreationKit.exe NavMesh Generation — Static Analysis |
| [ck_reference_init_hang.md](notes/ck_reference_init_hang.md) | CK "Initializing References" hang — investigation log (2026-08-22) |
| [ck_vs_game_missing_objects.md](notes/ck_vs_game_missing_objects.md) | Objects present in the Creation Kit, missing in game |
| [ck_warnings_fixes.md](notes/ck_warnings_fixes.md) | CK warnings: the 2026-07 fix sweep |
| [ck_warnings_verdicts.md](notes/ck_warnings_verdicts.md) | CK warnings: WONTFIX verdicts and dead ends |
| [creature_conversion.md](notes/creature_conversion.md) | Creature Conversion: Oblivion CREA → Skyrim Actor (Fully Automated, No Donors) |
| [dialogue_conversion.md](notes/dialogue_conversion.md) | Dialogue / Quest Conversion Notes (DIAL / INFO / QUST / DLBR / DLVW) |
| [dialogue_transfer_gaps.md](notes/dialogue_transfer_gaps.md) | What Oblivion dialogue does not transfer to Skyrim, and what to do about it |
| [ingame_test_methodology.md](notes/ingame_test_methodology.md) | In-Game Test Methodology (clean-room quest/dialogue/script testing) |
| [magic_conversion.md](notes/magic_conversion.md) | Magic Conversion: Analysis and Path to Completion |
| [mod_archive_ingest.md](notes/mod_archive_ingest.md) | Mod Archive Ingest — Drag-and-Drop a Mod Archive |
| [mod_merge_and_base_resolution.md](notes/mod_merge_and_base_resolution.md) | Converting a mod STACK, and seeing past your own tree |
| [music_conversion.md](notes/music_conversion.md) | Music conversion (TES4 → TES5) |
| [navmesh_corridor.md](notes/navmesh_corridor.md) | Navmesh redesign: pathgrid corridor ribbons |
| [nif_conversion.md](notes/nif_conversion.md) | NIF / Asset Conversion Notes |
| [npc_skin_tone.md](notes/npc_skin_tone.md) | NPC skin tone conversion (TES4 → TES5) |
| [override_conversion.md](notes/override_conversion.md) | Override Conversion (plugins with masters) |
| [package_conversion.md](notes/package_conversion.md) | PACK Conversion Plan (TES4 → TES5) |
| [package_conversion_fixes.md](notes/package_conversion_fixes.md) | PACK conversion: the 2026-08-17 fix pass |
| [package_verified_behaviour.md](notes/package_verified_behaviour.md) | PACK conversion: verified-correct behaviour |
| [papyrus_conversion.md](notes/papyrus_conversion.md) | Papyrus / Script Conversion Notes |
| [performance.md](notes/performance.md) | Performance & Parallelism Notes |
| [quest_fixes.md](notes/quest_fixes.md) | Quest conversion: bugs found and fixed |
| [quest_script_fixes.md](notes/quest_script_fixes.md) | Quest script conversion: defects found and fixed |
| [quest_script_gaps.md](notes/quest_script_gaps.md) | Quest script conversion: known gaps |
| [quest_script_verified_behaviour.md](notes/quest_script_verified_behaviour.md) | Quest script conversion: verified-correct behaviour |
| [script_conversion.md](notes/script_conversion.md) | TES4 Script → Papyrus Conversion Plan |
| [script_conversion_bugs.md](notes/script_conversion_bugs.md) | Script conversion: known defects found during the parse-tree rewrite |
| [script_convert_findings.md](notes/script_convert_findings.md) | script_convert: measurements and failure modes |
| [shader_value_mapping.md](notes/shader_value_mapping.md) | Shader values: what the converter writes, and why |
| [speak_as_lines.md](notes/speak_as_lines.md) | Speak-as lines: what works and what was reverted |
| [speedtree_engine_decomp.md](notes/speedtree_engine_decomp.md) | SpeedTree engine decompilation — replicating Oblivion's tree rendering |
| [ui_conversion.md](notes/ui_conversion.md) | UI conversion — Oblivion's menus in Skyrim |
| [weather_climate.md](notes/weather_climate.md) | Weather / Climate Conversion (WTHR, CLMT, REGN weather, sky meshes) |
| [world_land_navmesh.md](notes/world_land_navmesh.md) | World / LAND / PGRD→NAVM Conversion Notes |

## `plans/`

Designed, NOT yet built. Opens with `Status: PLAN, unimplemented.` Moves to `notes/` when built.

| Doc | Covers |
|---|---|
| [horse_rideability.md](plans/horse_rideability.md) | Rideable Horse Conversion: Oblivion CREA → Skyrim Mountable Actor |
| [in_app_update.md](plans/in_app_update.md) | In-app update: download only what changed — design plan |
| [quest_conversion_gaps.md](plans/quest_conversion_gaps.md) | Quest conversion: gaps needing design |
| [vanilla_creature_swap.md](plans/vanilla_creature_swap.md) | Plan — "Vanilla Creature Swap" ESP generator + GUI |
| [vanilla_item_swap.md](plans/vanilla_item_swap.md) | Plan — "Vanilla Item Swap" (ingredients, food, clutter) + preview renderer |

## `audits/`

A dated sweep over a corpus, with counts. Frozen once written; a re-audit is a NEW file.

| Doc | Covers |
|---|---|
| [aggression_faction.md](audits/aggression_faction.md) | Aggression / Ally / Enemy Conversion Audit |
| [ck_warnings.md](audits/ck_warnings.md) | CK Warnings Audit — Oblivion.esm |
| [package_conversion.md](audits/package_conversion.md) | PACK Conversion Audit — 2026-08-17 |
| [quest.md](audits/quest.md) | Quest Completability Audit — Oblivion.esm conversion |
| [quest_script_conversion.md](audits/quest_script_conversion.md) | Quest Script Conversion Audit |
| [skse_conversion.md](audits/skse_conversion.md) | SKSE / OBSE Convertibility Audit — Grounded in the Original Nehrim Scripts |

## `assets/`

Non-prose files. `banner.png` and `favicon.ico` are loaded at RUNTIME by `gui.py`.

