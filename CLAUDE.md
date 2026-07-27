# TES4-to-TES5 Conversion Project — AI Context

Convert TES4 (Oblivion) master/plugin files to TES5 (Skyrim) format.

| Stage | Package | Responsibility |
|---|---|---|
| Export | `tes4_export` | Reads TES4 binary, dumps every record to KEY=VALUE text. **Pure dump — no transformations.** |
| Import | `tes5_import` | Reads the text, writes a binary TES5 ESM/ESP. **All TES4→TES5 transformations live here.** |
| Assets | `asset_convert` | Meshes, textures, SpeedTree, collision, sound, LOD, BSA packing |
| Scripts | `script_convert` | TES4 script → Papyrus |

`convert.py` orchestrates all stages. Quick start:

```bash
python convert.py -f Oblivion.esm      # full pipeline for one file
python -m pytest tests/test_import.py -v
```

See [docs/pipeline_reference.md](docs/pipeline_reference.md) for all commands,
caching, skipped record types, the export text format, and the directory layout.

---

## Critical Rules

### Process

- **Do one bug at a time.** Make the edits before moving to the next. If you find
  another bug while investigating, fix it too.
- **Work in the order the prompt presents.** Highest priority first.
- **Never stop mid-task to report or ask.** Finish everything, verify, then reply.
- **All fixes must be generic.** Never patch to satisfy a single record or file.
  Oblivion and Nehrim are only the test files — we never know what plugin this
  runs on.
- **The goal is COMPLETE conversion.** Don't strip things out because the
  conversion would be complicated.
- **If you don't see the problem described, the test data is not stale** — there
  is always a REAL problem to find.
- **Census vanilla before calling something wrong.** If Skyrim.esm or the DLCs do
  the same thing at scale, it is legal and is not your bug — several docs record
  "verified vanilla-legal, don't fix this" for exactly the things that looked
  broken. Conversely, "all 3,740 vanilla records write 0 here" is the strongest
  possible evidence for what to write.
- **Prefer the engine's own mechanism over a Papyrus/script approximation.** Force
  greet is a package, not a function call; `SetAlert` is native, not
  `DrawWeapon()`. Check for a real equivalent before declaring one absent — the
  wikis under-document both games.
- **A symptom's cause is often several layers from the symptom.** Frozen NPCs have
  traced to navmesh, condition params, package data, and behavior graphs in turn.
  Confirm the mechanism before fixing; a plausible story that explains the symptom
  is not yet a diagnosis.
- Don't preserve backwards compatibility. Delete code that is no longer used.
- Keep files under ~1000 lines; split by responsibility when one grows.
- <a id="tools-first"></a>**CHECK `tools/` BEFORE BUILDING ANYTHING BESPOKE.**
  ~95 tools already exist and one probably answers your question — the full
  catalogue is [docs/python_tools_reference.md](docs/python_tools_reference.md).
  The order is:
  1. **Use** the existing tool.
  2. If it *almost* fits, **extend or fix it** — new flags, wider output. Never
     write a parallel script that duplicates a tool's job, and never leave a
     broken tool in place while working around it.
  3. Only if nothing is close, write a new one — and **add its entry to
     `python_tools_reference.md` in the same pass**, before you report back. An
     undocumented tool is one the next session will rebuild from scratch.
- Put throwaway files in `temp/`. Don't write one-off scripts with hardcoded
  output — `tools/` scripts take arguments and produce general output, so they are
  reusable next time.
- **Always record new learnings** in this file or, more likely, the relevant `docs/` file.
- Docs can be wrong: they sometimes describe fixes that were never implemented.
  Grep the source before claiming a mechanism exists, and fix the doc.
- If using subagents, ONLY use the lower tier Sonnet or Haiku models. NEVER Opus. Limit additional agents to only 2 at a time.
- When building test scripts, always output as they go so that if the time goes past the hard 120 second timeout limit you still get some output
- **LISTEN CAREFULLY to EXACTLY what the user's prompt says**. Seek to understand any implementation ideas instead of using your pre-conceived notions

### Verifying your work

**Always check theories against several of these** before acting:

1. The Skyrim executable at `D:\Other Games\Skyrim Anniversary Edition\SkyrimSE.exe`
   — this is the GOG/AE build and is **NOT DRM-packed**, so it disassembles
   statically. (Only the *Steam* copy is encrypted, `.text` entropy 8.00 — don't
   confuse the two and conclude the exe is unreadable.) Crash logs from the Steam
   build map across via the Address Library (stable ID → per-build RVA).
2. The Oblivion/Nehrim install at `D:\Other Games\Nehrim At Fate's Edge\Data`.
3. xEdit source at `references/xEdit` — `Core/` documents the binary structure of
   every record type. This is the first stop for any format question. Or if working with meshes, go to the Nifskope source at `references/Nifskope`
4. The Skyrim.esm dump at `references/Skyrim.esm`, real Skyrim.esm, and
   `references/Skyrim Meshes`. **Verify binary layout against BOTH the xEdit
   definition AND a real Skyrim.esm dump — never skip either.**
5. UESP / CK wiki via `python tools/uesp_lookup.py`. **Never WebSearch or
   WebFetch for these** (they 403). An empty result means fix the query.
6. A web search for other authoritative sources.
7. The Papyrus logs from the last in-game run — read them to diagnose a runtime
   symptom (see the directory-purpose table under Hard prohibitions).
8. Failing all the above, add comprehensive logging so the user's next run
   captures everything needed. Make it thorough — one wasted round trip costs the
   user a full build-and-play cycle, and their time is far more valuable than
   yours.

Never attribute a bug to LE-vs-SSE mesh format differences — verify engine
theories externally first.

### Hard prohibitions

- **NEVER `git stash` / `git stash pop`** in this repository.
- **NEVER `git commit` or `git push`.** The user commits after in-game testing.
- **NEVER `git add` / `git rm`** (staging, including staged deletions). Use plain
  `rm`. `git reset` destroys the user's own staging.
- **NEVER go snooping in the live, heavily-modded SSE install.** It is full of
  other mods' assets, so nothing you find there tells you anything about this
  converter. In particular: **never inspect it to check whether your changes were
  deployed or installed correctly** — trust the user's deployment statements, and
  never argue with an in-game result by reading their setup.
  Each external directory has ONE sanctioned purpose:
  | Path | Use it for | Not for |
  |---|---|---|
  | `D:\Other Games\Skyrim Anniversary Edition\` (GOG/AE) | exe decompilation | assets, deployment checks |
  | Oblivion / Nehrim LE install | BSA files and NIFs | anything Skyrim-side |
  | The modded SSE install | **Papyrus logs, and reading `Skyrim.esm`** | everything else, especially verifying deployment |
- **Never run the full pytest suite** — only the tests for files you changed.
- **KEEP EVERY TEST COMMAND / SCRIPT UNDER 120 SECONDS. Never set a long timeout.** If a command
  needs minutes, you have picked the wrong scope — narrow it instead: one cell,
  not a worldspace; 2-3 NIFs, not a tree; one record type, not the whole plugin;
  the per-cell tool (`navmesh_tri_check --cell X`) instead of the batch sweep.
  Most tools take `--cell` / `--max N` / `--workers` for exactly this. A long
  timeout burns the user's wall-clock and usually hides a scoping mistake; if
  something genuinely cannot be scoped down, say so instead of waiting on it.
- If file recovery is in progress, make zero writes to the affected drive; ask
  first before any bulk operation.
- **NEVER stop in the middle of an incomplete task to give a mid-session update**
  I don't want to know. I want you to complete the task you have been given

### Working with the user

- **NEVER STOP TO GIVE A MID-SESSION STATUS REPORT.** Not "here's where I am",
  not "should I continue?", not a summary of progress so far. Finish the whole task, then report once. A status update mid-task is a failure, not politeness.
- **Measure the invariant the user asked for, not a proxy for it.**
- **Trust the user's in-game test results as ground truth.** Never question
  whether they tested something, and never rebut a reported result with file
  timestamps or a reconstructed timeline. (Reading Papyrus logs to *diagnose* is
  encouraged — using them to dispute the user's report is not.)
- **Never assume `output/Oblivion.esm` reflects the latest code** from its
  timestamp. Ask, or rebuild first.
- **BUILD EVERY FILE YOUR CHANGES TOUCH, before reporting back.** The user should
  be able to launch the game and verify immediately — never leave them to work out
  which stage to re-run, and never hand back a change that only compiles in
  theory. Map the files you edited to stages and run each one into `output/`:

  | Changed | Run |
  |---|---|
  | `tes4_export/` | `python convert.py -f <plugin> --export-only` |
  | `tes5_import/` (records, navmesh, packages, dialogue) | `--import-only` |
  | `script_convert/` | `--scripts-only` (compiles .psc → .pex) |
  | `asset_convert/nif_converter.py`, collision, skin | `--meshes-only` |
  | `spt_*` | `--speedtrees-only` |
  | sound conversion | `--sounds-only` |
  | LOD | `--lod-only` |
  | BSA packing | `--pack-only` |

  Touching several areas means running several stages — import *and* scripts if
  you changed both. Other flags: `--creatures-only`, `--extract-only`,
  `--prune-textures-only`, `--pack-zip-only`. Report what you built and any
  failures verbatim; if a stage genuinely cannot be run, say which and why rather
  than staying silent.
- **While iterating on a repeated failure, don't write tests, update docs, or ANYTHING until
  the fix is CONFIRMED in-game.** Each round trip costs the user a full
  build-and-play cycle, so spend it on the diagnosis and the candidate fix only.
  Tests and docs written against an unconfirmed theory usually just encode the
  wrong theory and have to be rewritten. Once the user confirms, then add the
  regression test and the doc note.
- **When a fix doesn't work, don't continue to re-apply a variant of the same theory without new evidence.** Two
  failed attempts on one theory likely means the theory is wrong — go back to the
  sources in "Verifying your work" and find a *different* mechanism. Say plainly
  that the previous explanation was wrong rather than layering another guess on
  top of it.
- **Report honestly.** If something is untested, say so; if you skipped part of
  the scope, say which part and why. Never describe an unverified change as
  working.

### Assets and references

- **`references/` is for comparison/analysis ONLY — the pipeline must NEVER
  resolve runtime assets through it.** Vanilla Skyrim files are fetched via
  `asset_convert/skyrim_assets.py` (cache in `export/skyrim_assets/`, else
  auto-extracted from the SSE BSAs via registry-detected install).
- `references/` subfolders (`NIFConverter/`, `xEdit/`, `UESP/`, `nifskope`) are
  other projects — reference only.
- **LE assets are SSE-compatible.** Never dig through SSE-format assets/BSAs.
  BSA meshes are SSE-format; read them with `asset_convert/sse_nif.py`
  (`read_nif` converts BSTriShape graphs to LE NiTriShape graphs in-memory;
  pyffi Patch 8 supplies the SSE read layouts). Output is always written LE
  (uv2=83), which SSE loads natively.
- **The LE-compatibility rule above does NOT extend to `.hkx`: every hkx we ship
  is 64-bit.** `convert_hkx_to_amd64()` is the mandatory final step
- Use `references/nif [version].xml` for valid Skyrim NIF behavior — newer and
  more correct than pyffi 2.2.3's bundled version. Use pyffi with the clock
  monkey patch when analyzing.
- **Never batch-test many NIFs.** Test 2-3 specific to the bug. If a batch is
  genuinely required, use full workers (`cpu_count() - 1`) — single-threaded runs
  cap at 10 NIFs. Compare an `output/` mesh against the `export/` mesh and a few
  similar Skyrim meshes.

### Performance and memory

- Use multiprocessing, not threads, for pure-Python work; **ThreadPoolExecutor is
  only for I/O and subprocesses.** The output ESM must stay byte-reproducible.
  Rules and measured results: [docs/performance_notes.md](docs/performance_notes.md).
- **Never exhaust memory**: some pool tools load the ~2.1 GB export index per
  worker. Cap `--workers` or run single-process.

### Output paths

`output/Oblivion.esm` is a **FOLDER**, not a file — the .esm goes in
`output/Oblivion.esm/Oblivion.esm`. A write failure there means you are trying to
overwrite a folder with a file, not that a file is locked.

---

## Documentation Map

Deep reference material lives in `docs/` so this file stays short. Load the
relevant doc when working in that area.

### Pipeline & architecture
| Doc | Covers |
|---|---|
| [pipeline_reference.md](docs/pipeline_reference.md) | Orchestrator commands, stages, caching, SKIP_TYPES, export text format, directory layout, SSEEdit verification |
| [python_tools_reference.md](docs/python_tools_reference.md) | Per-module and `tools/` debug utility command reference |
| [performance_notes.md](docs/performance_notes.md) | Parallelism rules, determinism contract, navmesh optimisation results |
| [override_conversion.md](docs/override_conversion.md) | Converting plugins with TES4 masters: export-diff authorship, GRUP nesting, ONAM, cell buckets, injected records |
| [TES5_Binary_Format.md](docs/TES5_Binary_Format.md) | TES5 binary structure reference |
| [TES4_Record_Definitions.md](docs/TES4_Record_Definitions.md) | TES4 record structure reference |
| [xedit_scripting_reference.md](docs/xedit_scripting_reference.md) | xEdit Pascal API + globals (historical — the pipeline is pure Python now; kept for ad-hoc verification scripts) |

### Records & data
| Doc | Covers |
|---|---|
| [record_mapping_reference.md](docs/record_mapping_reference.md) | Full TES4→TES5 record type mapping, OBND/structural requirements, skipped/problem records, skill/weapon/biped-slot/enchantment tables, Skyblivion conversion rules |
| [magic_conversion_plan.md](docs/magic_conversion_plan.md) | SPEL/ENCH/MGEF: dropped effect families, phantom effect codes, archetype mapping, ARTO/PROJ/SEFF |
| [weather_climate_conversion.md](docs/weather_climate_conversion.md) | WTHR/CLMT: the WRLD→CNAM→CLMT→WLST chain, NAM0 slot remap, cloud-speed units, DALC weights |

### Actors, AI & dialogue
| Doc | Covers |
|---|---|
| [package_ai_contracts.md](docs/package_ai_contracts.md) | CTDA param remapping (the crash rule), PTDA Distance, Ambush→approach, force-greet packages, quest priority band |
| [package_conversion_plan.md](docs/package_conversion_plan.md) | PACK template model + vanilla census (implemented — the design behind `pack_converter.py`) |
| [dialogue_conversion_notes.md](docs/dialogue_conversion_notes.md) | DIAL/INFO/QUST/DLBR/DLVW implementation, voice type routing, AddTopic unlocks, GetIsID injection |
| [dialogue_engine_contracts.md](docs/dialogue_engine_contracts.md) | Verified engine rules for dialogue routing |
| [dialogue_transfer_gaps.md](docs/dialogue_transfer_gaps.md) | Measured gaps: what Oblivion dialogue does NOT survive conversion, with counts from both emulators |
| [ambient_dialogue_channel_plan.md](docs/ambient_dialogue_channel_plan.md) | Oblivion's 3 delivery channels vs Skyrim's 2; constant quipping, NPC-to-NPC topics in the player menu |
| [QUEST_AUDIT.md](docs/QUEST_AUDIT.md) | Quest completability audit via the walkthrough emulator (2026-07-17, all 390 QUSTs) |
| [creature_conversion.md](docs/creature_conversion.md) | CREA→actor: behavior graphs, HKX skeleton/animation/ragdoll, creature records |
| [horse_rideability_plan.md](docs/horse_rideability_plan.md) | Rideable horses: RACE Mount Data, horse/rider graph pair, rider-animation sourcing |

### Scripts
| Doc | Covers |
|---|---|
| [papyrus_conversion_notes.md](docs/papyrus_conversion_notes.md) | TES4→Papyrus mapping, paired on/off soft-lock trap, Say() timers and fragment release order, syntax traps, OBSE constructs |
| [Script_Conversion_Plan.md](docs/Script_Conversion_Plan.md) | Script conversion scope, counts, block/variable distributions |
| [skse_conversion_audit.md](docs/skse_conversion_audit.md) | SKSE/OBSE function coverage audit |
| [skyrim_commands.md](docs/skyrim_commands.md) | Raw table of Skyrim script command IDs, names, and argument types |
| [php_scriptconverter_analysis.md](docs/php_scriptconverter_analysis.md) | How Skyblivion's AST-based PHP converter works vs our regex approach — prior art, not a dependency |

### World, meshes & navmesh
| Doc | Covers |
|---|---|
| [nif_conversion_notes.md](docs/nif_conversion_notes.md) | NIF deep-dive: bhk collision/MOPP/CMS, particles, FlameNode grafting, worn armor/shields/furniture markers, skin retargeting, clutter physics, terrain LOD, SpeedTree |
| [world_land_navmesh_notes.md](docs/world_land_navmesh_notes.md) | PGRD→NAVM/NAVI algorithm, LAND record structure, landscape TXST |
| [navmesh_corridor_redesign.md](docs/navmesh_corridor_redesign.md) | The corridor-ribbon navmesh model |
| [ck_navmesh_generation.md](docs/ck_navmesh_generation.md) | How the CK generates navmesh (Recast), defaults, the voxel-vs-world units trap |

### Skills
| Skill | Covers |
|---|---|
| `oblivion-dialog-system` | Vanilla TES4 dialogue/voice/quest records |
| `skyrim-dialog-system` | Vanilla TES5 dialogue/voice/quest records |
| `oblivion-to-skyrim-dialog` | TES4→TES5 dialogue/quest/voice mapping |
