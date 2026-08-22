# CK Warnings Audit — Oblivion.esm

Bucketed inventory of every warning the Creation Kit emits when loading our
converted `Oblivion.esm`, ordered **easiest to hardest to fix**.

Prior sweep (2026-07-16) is recorded at the bottom under
[Fixed in the 2026-07 sweep](#fixed-in-the-2026-07-sweep) — do not re-diagnose those.

---

## Capture method

The CK holds `ckpe.log` with an **exclusive write lock**. It is still readable
with `FileShare.Read` — plain `Copy-Item`, `cp`, and `FileShare.ReadWrite` all
fail with "used by another process", which is not the same as unreadable:

```powershell
$src = "C:\Program Files (x86)\Steam\steamapps\common\Skyrim Special Edition\ckpe.log"
$fs  = New-Object System.IO.FileStream($src,'Open','Read','Read')
$out = New-Object System.IO.FileStream($dst,'Create','Write')
$fs.CopyTo($out); $out.Close(); $fs.Close()
```

`Logs\CKPE\CreationKitPlatformExtended.log` is only the ~10 KB patch-init log —
it does **not** contain the warning stream. The repo's `CK_WARNINGS` file is a
stale manual capture; always re-pull the live log.

Warnings are tagged `[MASTERFILE]`, `[FORMS]`, `[EDITOR]`, `[SCRIPTS]`,
`[DEFAULT]`, `[PATHFINDING]`, `[MAGIC]`. Split ours from vanilla by FormID
prefix: `01......` is Oblivion.esm, `00......` is Skyrim.esm.

---

## Snapshot: 2026-08-22 16:46 (CK 1.6.1378.1, CKPE 0.6.267)

**45,437 warning lines.** 46,050 FormID mentions are `01` (ours), 1,320 are `00`.

| # | Bucket | Count | Tag | Effort | Status |
|---|---|---|---|---|---|
| 1 | Book has invalid character | 543 | FORMS | trivial | **WONTFIX** — font, not data |
| 2 | MGEF invalid primary Actor Value → HEALTH | 14 | MAGIC | trivial | **WONTFIX** — vanilla-legal |
| 3 | MGEF counter-effect invalid FormID | 25 | FORMS | trivial | **FIXED** 2026-08-22 |
| 4 | Shader effect sound `01800000` not found | 102 | MASTERFILE | easy | **FIXED** 2026-08-22 |
| 5 | Biped Object slot 44 invalid for DefaultRace | 596 | MASTERFILE | easy | open |
| 6 | One-way faction Friend/Ally relations | 105 | DEFAULT | easy | open |
| 7 | Duplicate EditorIDs → `…DUPLICATE001` | 270 | EDITOR | easy–moderate | open |
| 8 | "cannot be scripted, but has scripts attached" | 136 | SCRIPTS | moderate | open |
| 9 | Script property points at invalid object | 40 | SCRIPTS | moderate | open |
| 10 | Potentially invalid X/Y on reference | 62 | MASTERFILE | moderate | open |
| 11 | Ref should be persistent but is not | 18 | MASTERFILE | moderate | open |
| 12 | CTDA param init failures | 44 | MASTERFILE | moderate | open |
| 13 | Navmesh should be refinalized (bounds missing) | 19 | PATHFINDING | moderate–hard | open |
| 14 | Special Ref not in Special Ref data (map markers) | 1,002 | MASTERFILE | hard | open |
| 15 | Exterior cell no longer tagged to this location | 989 | MASTERFILE | hard | open |
| 16 | Ref uses location but not in unloaded-ref data | 15,333 | MASTERFILE | hard | open |
| 17 | Cell not in worldspace exterior cell data | 26,124 | MASTERFILE | hard | open |

Buckets **14–17 are one root cause** and account for 43,448 lines (**96%**).

---

## 1. Book has invalid character (543) — trivial

`[FORMS] Book 'X' (id) has invalid character <?>.`

Only **4 distinct books**, and the count is per bad byte, not per book:

| Book | Lines |
|---|---|
| `testFonts` | 483 |
| `testqabook` | 58 |
| `Broadsheet01Assassination` | 1 |
| `FGD06ViranusJournal` | 1 |

**WONTFIX — the encoding is already correct; the FONT is the limitation.**

Every offending character is ordinary accented Latin (`é É à è ù ü ï ñ œ`, 49
distinct) and all of them round-trip through cp1252 exactly as authored.
`pack_string_subrecord` already writes cp1252, matching xEdit's
`wbMBCSEncoding(1252)`.

The CK rejects them because the English Skyrim font has no glyph. Census:
**zero** of Skyrim.esm's text subrecords (DESC/FULL/CNAM/SHRT/RNAM/...) contain
any character above ASCII 126, so the glyphs were never authored on the Skyrim
side either.

Every available "fix" alters authored text — folding `naïve`->`naive` and
`protégé`->`protege` drops characters the author wrote. Not worth it for the
real content, which is **2 lines total**:

- `FGD06ViranusJournal` — "na**ï**ve" x1
- `Broadsheet01Assassination` — "prot**é**g**é**" x2

The remaining 541 lines are `testFonts` (483) and `testqabook` (58), Bethesda
font-test sheets that exist precisely to display accented glyphs. If the noise
ever needs silencing, skip those two records rather than rewriting text.

## 2. MGEF invalid primary Actor Value → HEALTH (14) — trivial

`[MAGIC] Effect Setting 'X' has an invalid primary Actor Value! HEALTH used.`

**WONTFIX — vanilla-legal. The CK warns about its own records.**

Census of Skyrim.esm's 950 MGEFs: **304 write Actor Value = -1**, including all
13 Turn Undead records (archetype 35) and the vanilla Rally record (archetype
41) — the very archetypes warned about here.

Our 14 are the AV-less **fallback** records that stand in when an effect's
actor value cannot be mapped; the per-AV variants (`_av_variants`, one MGEF per
(code, actor value) pair the plugin uses) carry the real work. All 14 have their
archetype set correctly and differ from vanilla only in writing -1 for the AV,
which is exactly what vanilla does 304 times.

Verified in the built ESM: Rally arch=38, Frenzy 8, Charm 6, Calm 6, Demoralize
7, Absorb Skill/Attribute 4, Damage/Restore Attribute 0, Drain/Fortify 34, Turn
Undead 24 — all AV = -1.

## 3. MGEF counter-effect invalid FormID (25) — trivial

`[FORMS] Invalid form ID for effect setting found while loading counter effects for XXXX.`

**FIXED 2026-08-22** — `_sort_mgef_by_counter_effects` in
[writer.py](../tes5_import/writer.py).

The original diagnosis ("targets no longer exist") was **wrong**. Every ESCE
target resolves to an MGEF that is present in the output, no ESCE dangles, and
the DATA counter-effect count matches the ESCE subrecord count on every record.

The real cause is **group ORDER**. The reported ids decode as real records —
`16783472` = `0x01001870` = DSPL, `16783458` = `0x01001862` = CUPO — and the CK
resolves ESCE *while reading the MGEF group*, looking the target up in what it
has loaded so far. A record naming a counter effect that appears later in the
group fails.

Measured on the pre-fix ESM: 41 ESCE references, **25 pointing forward and all
25 warned; 16 pointing backward and all 16 silent** — the forward set equals the
warning set exactly.

Vanilla Skyrim.esm ships **zero** ESCE subrecords across its 950 MGEFs, so there
is no vanilla ordering to copy; the engine's counter-effect load path is simply
order-dependent and untested by Bethesda's own data.

Fix: a stable topological sort of the MGEF group at serialization time, so every
target precedes its referrer. Records keep source order except where a dependency
forces one earlier, which preserves byte-reproducibility; a cycle (Oblivion has
none) degrades to source order rather than raising. Verified after rebuild: 340
records, 41 ESCE refs, **0 forward, 0 dangling**.

## 4. Shader effect sound `01800000` not found (102) — easy

`[MASTERFILE] Could not find shader effect sound (01800000) on object 'X'.`

**FIXED 2026-08-22** — `convert_EFSH` in
[world.py](../tes5_import/record_types/world.py).

Not a stray FormID at all: **the EFSH DATA tail was written 4 bytes off**.

Every field from offset 260 up sat one slot too high against the xEdit TES5
EFSH struct. That put the float `1.0` written for "Addon Models - Scale Out
Time" into **`Ambient Sound` at offset 308**. Float 1.0 is `0x3F800000`; the CK
reads it as an object id, takes the low three bytes `0x800000`, applies our
plugin index `01`, and reports `01800000` — the exact id in all 102 warnings.

Offsets verified field-by-field against the xEdit definition **and** 152 real
Skyrim.esm EFSH records:

| Offset | Field | Was written at |
|---|---|---|
| 260 | Holes - End Val | 264 |
| 264 | Edge Width (alpha units) | 268 |
| 268 | Edge Color | 272 |
| 272 | Explosion Wind Speed | *(skipped)* |
| 276/280 | Texture Count U/V | 280/284 |
| 284-304 | Addon Models Fade/Scale block | 288-308 |
| **308** | **Ambient Sound (SNDR FormID)** | *(never written — got Scale Out Time)* |
| 312/316 | Fill Color Key 2/3 | 316/320 |
| 320-340 | Color key scales and times | 324-344 |
| 344 | Color Scale | 348 |
| 376 | Frame Count | 372 |

Two values were also wrong on the merits and corrected against the vanilla
census: `Holes - End Val` is 0.0 on 143 of 152 vanilla records (we wrote 1.0),
and `Frame Count` is 0 on 108 of 152 (we wrote 1 — a TES4 shader has no frame
data to convert). `Ambient Sound` stays 0: TES4 EFSH has no sound field and 95
of 152 vanilla records leave it null.

Verified after rebuild: 102 EFSH records, **all 11 checked fields correct on
102/102**, Ambient Sound zero on every one.

## 5. Biped Object slot 44 invalid for DefaultRace (596) — easy

`[MASTERFILE] Armor 'X' used invalid Biped Object slot 44 for race 'DefaultRace'.`

298 ARMO + 298 ARMA, all slot 44, all `DefaultRace`.

**This is a RACE-side gap, not an armor-side bug.** Slot 44 is deliberate —
[constants.py:38](../tes5_import/constants.py#L38) maps TES4 Lower Body → 44 so
greaves get their own slot. But `DefaultRace` is vanilla Skyrim's record, which
we do not override, and it never declares slot 44 in its body-part data.

Affected items are not only greaves — `LowerPants`, `MiddlePants`,
`SE11CiirtaRobe`, `DBUpperShirt`, `MGRobeConjurer`, `SE06Mania/DementiaReward`
all appear, i.e. everything routed through Lower Body.

Fix: emit a `DefaultRace` override declaring slot 44 (and audit any other race
worn by converted actors). Do **not** remap slot 44 away — see
`project_body_skin_slot44`.

## 6. One-way faction Friend/Ally relations (105) — easy

`[DEFAULT] Faction 'A' is a Friend or Ally of Faction 'B', but 'B' is not a Friend or Ally of 'A'.`

Skyrim treats XNAM Friend/Ally as reciprocal by convention; Oblivion does not.
Fix: after building all FACT records, symmetrize Friend/Ally XNAM entries.

Entries naming `PlayerFactionDUPLICATE001` are bucket-7 damage, not real
asymmetry — they will disappear when the EditorID collision is fixed.

## 7. Duplicate EditorIDs → `…DUPLICATE001` (270) — easy to moderate

`[EDITOR] Editor ID 'X' for TYPE (01......) is not unique, previous object (00......) is type TYPE. Editor ID will be set to 'XDUPLICATE001'.`

Every one is one of ours colliding with a vanilla Skyrim form:

| Type | Count | Type | Count | Type | Count |
|---|---|---|---|---|---|
| SOUN | 71 | FACT | 9 | ARMO | 3 |
| BOOK | 68 | TACT | 7 | ACTI | 3 |
| STAT | 22 | REFR | 6 | WEAP | 2 |
| SPEL | 15 | MISC | 5 | DOOR | 2 |
| INGR | 14 | CLAS | 4 | WATR/PACK/MGEF | 1 each |
| QUST | 13 | DIAL | 3 | | |
| NPC_ | 13 | | | | |

Several collide **across types**, which is the CK matching on EditorID alone:
`Sleep` (our GLOB vs vanilla PACK), `Merchant` (our CLAS vs vanilla PERK),
`Vampire` (our QUST vs vanilla KYWD), `bed` (our DIAL vs vanilla IDLE),
`UpperChair01` (our STAT vs vanilla FURN).

The rename is silent and breaks anything resolving by EditorID — it is the
direct cause of the `PlayerFactionDUPLICATE001` and `CalmDUPLICATE` entries in
buckets 6 and 3.

Fix: prefix converted EditorIDs on collision with the vanilla namespace. Needs a
census of vanilla EditorIDs at write time. **Check for FormID drift before
shipping** — if any EditorID feeds `derive_formid`, changing it renumbers
records.

## 8. "cannot be scripted, but has scripts attached" (136) — moderate

`[SCRIPTS] X (01......) cannot be scripted, but has scripts attached to it.`

These are the **LVLN shell NPCs** from
[leveled_actors.py](../tes5_import/leveled_actors.py). An NPC_ whose `TPLT`
points at an LVLN is not scriptable in Skyrim, so **every VMAD we attach to a
shell is silently discarded.**

Names confirm the population: `SEZealot*`, `MG16Necromancer*`,
`SECreatureAtronachFlesh*`, `SE04FelldewElytra`, `SECreatureGnarl*`,
`MQ06MythicDawnAnteGuard*`, `SE32genericDefender*`. 40 of the 136 lines are
`Property` entries on those same shells.

This is a real behavioural loss, not cosmetic — the Oblivion scripts on those
actors do not run. Fix requires moving the script off the shell (onto the
placed ACHR, or onto a quest alias that fills the spawned actor).

## 9. Script property points at invalid object (40) — moderate

`[SCRIPTS] <name> on script <script> on <form> is pointing at an invalid object.`

Dominated by two scripts × 10 races each — a **race name bound where an object
is expected**:

- `TES4_DAHermaeusStaff` on `DAHermaeusThing` — Argonian, Breton, DarkElf,
  HighElf, IMPERIAL, Khajiit, Nord, Orc, Redguard, WoodElf
- `TES4_DABoethiaPortal` on `DABoethiaPortalFinal` — same 10

Plus 16 on `TES4_SE08AllyMainScript` (`GoldenSaint`/`DarkSeducer` properties on
`SE08GoldenSaint01-04Ref` / `SE08DarkSeducer01-04Ref`), one `Orc` on
`TES4_DAMalacathStatueScript`, one `Argonian` on
`TES4_MS40DaggerSpellEffect`, one `BurdTopic` on `TES4_QF_MQ07`.

The pattern is a property typed as ObjectReference but filled with a RACE (or
DIAL) form. One additional line:
`SE32ArrowSteelRef … is pointing at an object that can be picked up and has an
enable state parent` — separate, minor.

Cross-check `tools/property_type_audit.py` and
`project_objref_methods_never_promote_to_actor`.

## 10. Potentially invalid X/Y on reference (62) — moderate

`[MASTERFILE] Potentially Invalid X|Y value on reference: REFR Form to TYPE form in Cell …`

23 X + 39 Y. By base type: STAT 32, LIGH 12, NPC_ 5, MISC 5, ALCH 4, INGR 2,
DOOR 1, SOUN 1.

Coordinates the CK considers out of sane range. Related to the
`project_refr_angle_normalize_hang` family — the same class of oversized float
that hung reference init, here caught as a warning instead.

## 11. Ref should be persistent but is not (18) — moderate

`[MASTERFILE] Ref to base object X in cell Y should be persistent but is not.`

18 refs, incl. `MQMythicDawnShrineGuard` (LakeArriusShrineDagon, ×2),
`SEGoldenSaintGeneric` / `SEDarkSeducerGeneric` (SENSPalace, ×2 each),
`EvangelineBeanique` (ICPalaceOcatoChambers), `MQMythicDawnXivCatcher` and
`MQMythicDawnLowM` (TestParadiseCave), `CGMythicDawnAssassinAmbushC`
(ImperialDungeon), plus clutter in QASmoke/ChorrolHouseForSale.

⚠️ **Read [ck_vs_game_missing_objects.md](ck_vs_game_missing_objects.md) before
acting.** Force-persisting on the strength of this warning is exactly the fix
that was tried and refuted there — it made objects vanish in game.

## 12. CTDA param init failures (44) — moderate

Four related shapes:

| Shape | Count |
|---|---|
| `Unable to find Function Info TESForm … in TESConditionItem Parameter Init` | 33 |
| `Unable to find variable ::TES4NoSuchVariable_var on any VM scripts` | 11 |
| `Non-Persistent Function Info Reference … Initialization may fail in game` | 9 |
| `Package Location Reference on owner object … is not persistent` | 2 |

Mostly SE Fringe / Xeddefen dialogue INFOs and `SE08XedVictim0N_Flee` PACKs.
`TES4NoSuchVariable_var` is our own sentinel leaking into shipped conditions —
the condition is dead wherever it appears (e.g. `SKLxLightArmor4` on
`DaedraShrineTopic`, `SE08XeddefenNPC0NRef` on the flee packages).

Background: [dialogue_conversion_notes.md](dialogue_conversion_notes.md) on the
`Conditional` flag requirement, and `project_ctda_func_79_is_solved`.

## 13. Navmesh should be refinalized (19) — moderate to hard

`[PATHFINDING] NavMesh in cell X should be refinalized, there are navmesh bounds missing.`

All 19 are interiors: 11 Chorrol castle/wall towers
(`ChorrolCastleTowerBL/BL2/BR/FL/FR/R2`, `ChorrolCastleWallTowerNE/NW/SE/SW`,
`ChorrolFightersGuildTower`), `ChorrolGateBRandBL`, `ImperialDungeon01`,
`ImperialDungeon03`, `ICPalaceLibrary`, `ICWaterfrontLighthouse`,
`SkingradCastleSouthHall`, `OblivionRDCitadel04`, `OblivionMqKvatchCitadel`.

Missing NVNM bounds data on generated navmeshes. See
[world_land_navmesh_notes.md](world_land_navmesh_notes.md) and
[ck_navmesh_generation.md](ck_navmesh_generation.md).

---

## 14–17. The location cluster (43,448 lines, 96%) — hard

**All four are the same root cause.**
[locations.py](../tes5_import/locations.py) writes LCTN records carrying only
`FULL`, `MNAM`, `RNAM`, `PNAM`, `LCEC` — **none** of Skyrim's LCTN ref arrays.
Separately, there is no `OFST` emitter anywhere in `tes5_import/`.

### 17. Cell not in worldspace exterior cell data (26,124)

`[MASTERFILE] Cell (id) in world 'X' (id) is not in exterior cell data.`

The WRLD record has no `OFST` exterior cell-data table, so every exterior CELL
is unindexed. 59 worldspaces; the top of the distribution:

| Worldspace | Count | Worldspace | Count |
|---|---|---|---|
| TES4Tamriel | 11,777 | OblivionRD004 | 441 |
| SEWorld | 3,796 | MQ10BrumaOblivionGate | 441 |
| OblivionRD003 | 1,908 | OblivionRD005 | 420 |
| MS13CheydinhalOblivionWorld | 1,763 | CamoranParadise | 417 |
| DAPeryiteRealm | 704 | PalePassWorld | 371 |
| DABoethiaRealm | 576 | OblivionRD007 | 169 |
| MQ14OblivionWorld | 495 | MS14World | 169 |
| OblivionRD002 | 483 | *(45 more)* | ≤100 each |

### 16. Ref uses location but not in unloaded-ref data (15,333)

`[MASTERFILE] Ref 'X' (id) uses location but is not in the unloaded ref data.`

Refs carry `XLCN` but the target LCTN has no `ACPR`/`RCPR`/`RCUN`/`RCSR`/`RCEC`
arrays. **9,009 distinct refs.** Worst: `ICDoorInt01` 497, `NirnrootPlant` 271,
`XMarkerHeading` 230, `XMarker` 177, `ARDoor01` 157, `ICDoor04` 129,
`RFTrapDarts01` 125, `CTrapSwingMaceShort01` 115, `CDoor01` 104.

### 14. Special Ref not in Special Ref data (1,002)

`[MASTERFILE] Special Ref 'X' with type 'MapMarkerRefType' (0010F63C) is not in the Special Ref data.`

All 1,002 are `MapMarkerRefType`. We set `XLRT` on the map-marker REFR
([locations.py:17](../tes5_import/locations.py#L17)) but never add the marker to
the owning LCTN's special-ref array.

### 15. Exterior cell no longer tagged to this location (989)

`[MASTERFILE] Exterior cell (id) in world 'X' (id) is no longer tagged to this location.`

TES4Tamriel 888, SEWorld 50, then the city worlds (BrumaWorld 13, CheydinhalWorld
9, ChorrolWorld 7, AnvilWorld 7, LeyawiinWorld 6, BravilWorld 3, SkingradWorld 2,
SETheFringe/Ordered 2 each).

### Fix

One change closes all four: populate the LCTN ref arrays (`ACPR` actor-cell-
persistent, `RCPR` ref-cell-persistent, `RCUN`/`RCSR`/`RCEC` unique/static/cell
arrays) and emit WRLD `OFST`. Verify layout against **both** the xEdit LCTN/WRLD
definitions in `references/xEdit/Core/` and a real Skyrim.esm dump.

---

## Fixed in the 2026-07 sweep

Recorded here because the detail previously lived only in machine-local memory.
Do not re-diagnose these.

- **Persistence-location leak (CK hang)** — `locations.py` door-linking claimed
  door *destination* cells as interiors without checking. A city/Oblivion gate
  leads out to an exterior; XTEL destination doors are persistent and a
  worldspace stores all persistent refs in one dummy cell (Tamriel `00023777`),
  so one poisoned entry gave every persistent ref in Tamriel a single gate's
  location. Guard: only cells with `DATA.Flags & 1` (interior) may be
  door-claimed.
- **LCTN group order** — the CK resolves LCEC worldspace + MNAM marker when the
  LCTN top group loads. Vanilla order is `… NAVI CELL WRLD DIAL QUST … LCTN …
  DLBR DLVW`. LCTN before WRLD = 512× "Could not find worldspace in load" and
  undiscoverable markers.
- **PlayerRef remap** — `get_formid()` must not offset `0x14` (PlayerRef, in no
  data file). Nearly every *other* low FormID (Tamriel WRLD `0x3C`, gold `0xF`,
  Player NPC_ `0x7`, marker STATs, DIALs `0xAA+`) is a real Oblivion.esm record
  (~195 below `0x800`) and must remap. Pass-through set is exactly `{0x14}`
  (`_ENGINE_FIXED_FORMIDS` in `text_reader.py`).
- **Aimed magic needs projectiles** — an AIMED ENCH/SPEL whose effects have no
  projectile MGEF casts nothing. `magic_effects.py` synthesizes companion MGEFs
  (clone vanilla DATA from `vanilla_mgef_data.py`, regen via
  `tools/gen_vanilla_mgef_table.py`; patch cast=FF/delivery=aimed/projectile).
  MGEF DATA offsets: proj `0x48`, arch `0x40`, AV `0x44`, cast `0x50`,
  delivery `0x54`.
- **SPEL cast type** — `convert_SPEL` packed CastType=2 (Concentration) for every
  spell; FF is 1 (0=Constant 1=FF 2=Conc 3=Scroll; scrolls use 3).
- **SGST→SCRL had zero effects** (dead sigil stones); scrolls also need ETYP
  (EitherHand `0x13F44`).
- **TES4 negative inventory counts** = merchant restock semantics → `abs()` for
  CNTO/LVLO.
- **8-byte LVLO** — the TES4 LVLO Count+pad tail is optional (xEdit
  `wbStructExSK` optional-from-element-3); 8-byte = Level(2)+pad(2)+FormID(4),
  count=1.
- **Orphaned topics** — Oblivion.esm ships ~856 zero-INFO placeholder DIALs;
  emitting them = one "Orphaned topic" each. Skip, and drop TCLT choices into
  them (`_EMPTY_DIAL_FIDS`).
- **Vanilla PTDA null target** = type 6 (Self), never type-0-fid-0.
- **Footstep sets** — FSTArmorLight `0x21486`, FSTBarefoot `0x21468` (the older
  `0x24238`/`0x24237` do not exist).
- **Race VTCK** — vanilla creature races fill *both* voice slots (DogRace:
  CrDogVoice ×2); nulls produce per-race CK warnings.

Re-verify with `python tools/verify_ck_fixes.py <esm>`.
