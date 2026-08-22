# CreationKit.exe as a disassembly source

**Verified 2026-08-22** against
`C:\Program Files (x86)\Steam\steamapps\common\Skyrim Special Edition\CreationKit.exe`
(CK 1.6.1378.1, 47,954,944 bytes, mtime 2026-03-31).

The project's "Verifying your work" list sends every engine question to
`SkyrimSE.exe`. That is right for **runtime** behaviour and wrong for
**data-validation** behaviour. The CK is the better source for the second class,
and it is not DRM-packed.

---

## 1. The Steam CK is NOT DRM-wrapped

This is the opposite of the retail Steam exe. Measured PE facts:

| Binary | `.text` entropy | Entry point section | `.bind`? | Static disasm |
|---|---|---|---|---|
| Steam `SkyrimSE.exe` | **8.000** | `.bind` | yes (0x32918) | **impossible** |
| GOG/AE `SkyrimSE.exe` | 6.043 | `.text` | no | works |
| **Steam `CreationKit.exe`** | **5.593** | **`.text`** | **no** | **works** |

Entropy 8.000 is a fully encrypted section; 5.593 is ordinary x86-64 code. The
CK also imports no `steam_api*` and contains no Steam DRM strings. **Use the
Steam CK directly — there is no GOG-copy dance needed** (contrast
`project_skyrimse_exe_drm_packed`).

Reproduce with `tools/ck_srcpaths.py --pe-check`.

---

## 2. It carries Bethesda's original source tree

**1,114 compiled-in source file paths** under `e:\_skyrimhd\code\gamesln\`
(retail exe: 113, nearly all Havok). This is the real internal module layout:

| Files | Module | Files | Module |
|---|---|---|---|
| 335 | `shared\` | 106 | `bshavok\` |
| 92 | `construction set\` | 86 | `bsshader\` |
| 67 | `nimain\` | 66 | `nianimation\` |
| 65 | `bsmain\` | **60** | **`bspathfinding\`** |
| 58 | `niparticle\` | 40 | `bscore\` |

Directly relevant subtrees:

- `shared\tesforms\{character,gameplay,objects,world}` — **134 files**, one per
  record type (`tesland.cpp`, `testopicinfo.cpp`, `tesworldspace.cpp`,
  `tesobjectrefr*.cpp`, `tesweather.cpp`, `bgsdialoguebranch.cpp`, …).
- `construction set\tesforms\` — 45 `*_editor.cpp` files (the authoring side).
- `construction set\pathfinding\navmeshgeneration.cpp` + `_util.cpp` — **the
  navmesh generator we reimplement**.
- `shared\pathfinding\` — 5 navmesh + 7 pathing files, including
  `teleportdoorsearch.cpp` and `teleportpath.cpp` (cf.
  `project_door_xndp_pathing`).
- `shared\distantterrain\` — 8 files incl. `bgsterrainmeshbuilder.cpp` (LOD).
- `shared\speedtree\`, `shared\los\`, `shared\regions\`, `shared\facegen\`.

List them with `tools/ck_srcpaths.py --tree`.

## 3. Asserts carry FILE AND LINE

Assert sites pass the source path and line straight into the handler. Measured
example at RVA `0x0282af5a`:

```
lea  rcx, [rip + 0x73f717]   ; 'e:\_skyrimhd\code\gamesln\bspathfinding\bsnavmeshtriangle.h'
mov  edx, 0x1e3              ; line 483
call 0x140f91481             ; assert handler
```

The CK log prints the same pair — the CKPE blacklist ships real captures:

```
[ASSERTION] Tried to set a character outside the range of the string. (e:\_skyrimhd\code\gamesln\bscore\bsstring.h line 731)
[ASSERTION] This functor is only inteded to be called offline. (e:\_skyrimhd\code\gamesln\bsmain\bsninodeutils.cpp line 28)
```

So a CK assertion **already names the validating file and line** — no
disassembly needed to locate the check, only to read its condition.

## 4. 17,465 CK-only diagnostic strings

Full function names and printf parameter lists, e.g. (all verbatim):

```
BSNavmesh::GetMatchingEdge() for (Navmesh 0x%08x, Tri %d, Edge %d) - Edge has extra info flag, but no actual extra info
Bad portal navmesh ID in navmesh 0x%08x, Tri %d, Edge %d in Cell %s to invalid navmesh ID %08x, Tri %d, the cell needs to be refinalized
Attempted to create NavMeshInfo for orphaned NavMesh %0X.
Bad index in CheckNavMesh
---Land ( %i, %i ) has bad height delta. Height diff = %.4f.
Package Location Reference (%08X) on owner object "%s" is not persistent. Initialization may fail in game.
Unable to find location ref type %08X. Ref type data will be removed.
```

Counts by area: **408** navmesh, **180** LAND/landscape, **146** topic/dialogue.

`Bad portal navmesh ID … needs to be refinalized` resolves to **3 code sites**
via `tools/ck_strref.py`, and is the same check behind bucket 13 of
[ck_warnings_audit.md](ck_warnings_audit.md).

## 5. RTTI: fewer classes, but the RIGHT extra ones

**Correction to a natural assumption:** the CK has **fewer** RTTI type
descriptors than the game (4,731 vs 7,883) — it omits most runtime combat/AI
machinery. Overlap is 3,677 shared, 1,054 CK-only, 4,206 game-only.

The CK-only set is the *authoring and generation* code that exists nowhere in
the game:

```
RecastJob@BGSRecastModule            NavGenMeshRecastImport@NavGenUtil
NavMeshEditObject / …Triangle / …Vertex / …Edge
BGSRenderWindowNavMeshEditModule     WarningsSink@BGSWarningsHandler
BGSWarningsDialog                    BGSLOSGenParallelTask
```

`--find`/`--vtable` in `tools/skyrim_disasm.py` accept `--exe`, so they work
against the CK unchanged.

## 6. 433 DIALOG resources = the per-record field lists

`.rsrc` holds 433 dialog templates and 50 menus — the record editor forms, i.e.
exactly which fields the CK exposes per record type. Useful when deciding
whether a subrecord is authored or derived. Titles include `NavMesh Generation`,
`Advanced NavMesh Generation`, `Recast Navmesh Generation`, `World LOD`,
`Landscape Edit Settings`, `Quest Voice Assets`, `Invalid References`.

Dump with `tools/ck_srcpaths.py --dialogs`.

---

## When to use which binary

| Question | Source |
|---|---|
| Is my record structurally acceptable? Why was it rejected? | **CK** |
| What does this CK warning/assert actually check? | **CK** (file+line, §3) |
| How does the CK generate navmesh / LOD / lip? | **CK** |
| Which fields are authored vs derived for record X? | **CK** dialogs (§6) |
| Does the AI package run? Does the behaviour graph step? | GOG `SkyrimSE.exe` |
| Combat, dialogue scheduling, ragdoll, rendering at runtime | GOG `SkyrimSE.exe` |
| Crash-log address mapping (Address Library) | retail / `--live` |
| A hang with the game up | `--live` attach |

**The CK can disagree with the game** — that is the whole subject of
[ck_vs_game_missing_objects.md](ck_vs_game_missing_objects.md). A CK-only
finding is a lead about *data acceptance*, never proof of *runtime* behaviour.

## Tooling

- `tools/ck_srcpaths.py` — PE/DRM check, source-tree listing, CK-only string
  and RTTI diffs, dialog dump.
- `tools/ck_strref.py --pattern <regex>` — string → referencing code RVAs.
- `tools/skyrim_disasm.py --exe <CK> --find/--vtable/--disasm` — works on the CK.
