# docs/ — where documentation goes

Sorted by KIND, not by subject: the big files all span several subjects, but an
agent always knows which KIND it just produced.

## Where does my new document go?

1. Not prose (image, icon, data table)? → `assets/`
2. Describes a format or contract that exists independent of our code? → `reference/`
3. Describes work not yet done? → `plans/`, with `Status: PLAN` on line 2
4. A dated count over a corpus? → `audits/`
5. Explains code that EXISTS? → `commentary/`, named `<package>_<subsystem>.md`

**Naming:** `lower_snake_case.md`, named for its SUBJECT only. No ALLCAPS, no
`Title_Case`, no dates, and **no kind suffix** — the folder already states the
kind. `audits/aggression_faction.md`, not `..._audit.md`;
`commentary/asset_convert_nif.md`, not `..._notes.md`;
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
| [script_convert_architecture.md](reference/script_convert_architecture.md) | `script_convert/` architecture — read this BEFORE writing any code here |
| [skyrim_commands.md](reference/skyrim_commands.md) |  |
| [skyrim_mountable_actor.md](reference/skyrim_mountable_actor.md) | What makes a Skyrim actor mountable |
| [tes4_record_definitions.md](reference/tes4_record_definitions.md) | TES4 (Oblivion) Complete Binary Record Definitions |
| [tes5_binary_format.md](reference/tes5_binary_format.md) | Skyrim SE (TES5/SSE) Binary File Format — Exact Layout |
| [xedit_scripting.md](reference/xedit_scripting.md) | xEdit Scripting Reference (historical) |

## `commentary/`

Why the SHIPPED code is the way it is. Named after the code it explains; opens with `**Code:**`. The DEFAULT.

| Doc | Covers |
|---|---|
| [asset_convert_animation.md](commentary/asset_convert_animation.md) | asset_convert/hkx_anim.py — animation and behaviour graphs |
| [asset_convert_armor.md](commentary/asset_convert_armor.md) | asset_convert/body_wrap.py — worn armor, skin and fitting |
| [asset_convert_audio.md](commentary/asset_convert_audio.md) | asset_convert/audio_converter.py - sound and music |
| [asset_convert_collision.md](commentary/asset_convert_collision.md) | asset_convert/collision.py — Havok collision |
| [asset_convert_creature.md](commentary/asset_convert_creature.md) | asset_convert/creature_pipeline.py - creature conversion |
| [asset_convert_facegen.md](commentary/asset_convert_facegen.md) | asset_convert/facegen_tri.py - faces, skin tone and tints |
| [asset_convert_mod_ingest.md](commentary/asset_convert_mod_ingest.md) | asset_convert/mod_ingest.py - mod archive ingest |
| [asset_convert_nif.md](commentary/asset_convert_nif.md) | asset_convert/nif_converter.py — NIF conversion |
| [asset_convert_shader.md](commentary/asset_convert_shader.md) | asset_convert/nif_converter.py - shader values |
| [asset_convert_speedtree.md](commentary/asset_convert_speedtree.md) | asset_convert/spt_generator.py - SpeedTree conversion |
| [asset_convert_terrain.md](commentary/asset_convert_terrain.md) | asset_convert/terrain_lod.py — terrain, LOD and grass |
| [asset_convert_texture.md](commentary/asset_convert_texture.md) | asset_convert/parallax.py — textures, shaders and parallax |
| [asset_convert_ui.md](commentary/asset_convert_ui.md) | asset_convert/ui_menus.py - Oblivion UI in Skyrim |
| [ck_exe_disassembly.md](commentary/ck_exe_disassembly.md) | tools/disasm/ - CreationKit.exe as a source |
| [ck_navmesh_generation.md](commentary/ck_navmesh_generation.md) | tools/navmesh/ - how the CK generates navmesh |
| [ck_reference_init_hang.md](commentary/ck_reference_init_hang.md) | tes5_import/writer.py - the CK reference-init hang |
| [ck_vs_game_missing_objects.md](commentary/ck_vs_game_missing_objects.md) | tes5_import/ - objects in the CK, missing in game |
| [ck_warnings.md](commentary/ck_warnings.md) | tes5_import/ - what the CK complains about |
| [ingame_testing.md](commentary/ingame_testing.md) | tools/dialog/ - in-game test methodology |
| [performance.md](commentary/performance.md) | the whole pipeline - performance and parallelism |
| [script_convert.md](commentary/script_convert.md) | script_convert/ - TES4 script to Papyrus |
| [tes5_import_dialogue.md](commentary/tes5_import_dialogue.md) | tes5_import/dialog_converter.py - dialogue and voice |
| [tes5_import_magic.md](commentary/tes5_import_magic.md) | tes5_import/record_types/magic.py - magic conversion |
| [tes5_import_mod_merge.md](commentary/tes5_import_mod_merge.md) | tes5_import/master_manifest.py - merging a mod stack |
| [tes5_import_navmesh.md](commentary/tes5_import_navmesh.md) | tes5_import/navmesh/ - PGRD to NAVM, LAND and worldspace |
| [tes5_import_override.md](commentary/tes5_import_override.md) | tes5_import/overrides.py - plugins with masters |
| [tes5_import_package.md](commentary/tes5_import_package.md) | tes5_import/pack_converter.py - AI packages |
| [tes5_import_quest.md](commentary/tes5_import_quest.md) | tes5_import/object_scripts.py - quests and quest scripts |
| [tes5_import_weather.md](commentary/tes5_import_weather.md) | tes5_import/record_types/world.py - weather and climate |

## `plans/`

Designed, NOT yet built. Opens with `Status: PLAN`. Becomes commentary when built.

| Doc | Covers |
|---|---|
| [horse_rideability.md](plans/horse_rideability.md) | Rideable Horse Conversion: Oblivion CREA → Skyrim Mountable Actor |
| [in_app_update.md](plans/in_app_update.md) | In-app update: download only what changed — design plan |
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
