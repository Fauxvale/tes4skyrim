# PACK Conversion Audit — 2026-08-17

Full audit of the TES4→TES5 AI-package conversion: gaps, incorrect mappings, and
same-name/different-meaning traps. Scope: `tes5_import/pack_converter.py`,
`pack_templates.py`, `pack_aliases.py`, `packages.py`,
`tes4_export/record_types/dialog_misc.py::export_PACK`, and the CTDA path in
`dialog_conditions.py`.

Every number below was **measured 2026-08-17** against `export/Oblivion.esm/`,
`export/Nehrim.esm/`, `references/Skyrim.esm/PACK.txt`, `references/xEdit`, and
the built `output/Oblivion.esm/Oblivion.esm`. Nothing here is inferred.

Companion docs: [package_ai_contracts.md](package_ai_contracts.md) (the verified
engine contracts) and [package_conversion_plan.md](package_conversion_plan.md)
(the original design).

---

## Verified correct — do NOT "fix" these

Recorded so a later session does not re-litigate them.

| Area | Verification |
|---|---|
| PKDT flag re-derivation | Matches xEdit `wbPackageFlags` (`wbDefinitionsCommon.pas:7635`) bit for bit. Both collisions handled: TES4 bit 3 `Lock Doors At Package Start` vs TES5 `Maintain Speed At Goal`; TES4 bit 20 `Armor Unequipped` vs TES5 `Ignore Combat`. |
| PKDT dual format | `export_PACK` emits `PKDT.Format`, matching `wbPACKPKDTDecider` (4-byte subrecord = U16 flags + U8 type; 8-byte = U32 + U8). Measured: **561** old-format Oblivion packages, **zero** with any flag bit above 16 — so no flag is misread. Nehrim is 100% new-format. |
| PSDT layout | 12 bytes `<bbBbb3xi>`, Duration hours→minutes, `minute=-1`. Confirmed against all **5,961** vanilla PSDTs. The nonzero bytes at `[5:8]` in some vanilla records are uninitialised garbage (`ababab`, ASCII fragments), not a field. |
| PSDT DayOfWeek | `wbPackageScheduleDayOfWeekEnum` is a **shared** enum — identical 0..10 values incl. `Weekdays (MTWTF)`, `Weekends (SS)`, `Monday, Wednesday, Friday`. Oblivion's 306 day-scheduled packages copy through correctly. |
| PSDT Date | Non-issue: 7,134/7,209 Oblivion and 1,900/1,900 Nehrim packages write 0, and all 5,961 vanilla records write 0. |
| PTDA slot 3 = 0 | Re-confirmed: all **3,740** vanilla PTDA records write 0 across every target type. |
| Speed byte | Vanilla honours `PKDT` speed only when flag `0x2000` (Preferred Speed) is set — **4,386** vanilla records carry an inert `speed=2` with the flag clear. Writing walk-unflagged is inert, not a defect. |
| PKDT byte layout | `<IBBBBHH>` confirmed: `[4]`=Type (18 ×5,857 / 19 ×104), `[5]`=interrupt override, `[6]`=speed, `[10:12]`=interrupt flags. |
| Reused CTDA indices | `_FUNC_DROP` correctly catches the index collisions, incl. **365 = `GetPlayerInSEWorld` (TES4) → `IsChild` (TES5)**, 249 `GetPCFame` → `IsInDialogueWithPlayer`, 224, 227, 258, 259, 264. |
| Structural contract | `tools/esm/pack_validate.py output/Oblivion.esm/Oblivion.esm` → **clean, 7,209 records**. Every defect below is *semantic*, which is exactly why the structural validator passes. |

---

## Gaps found

### 1. `PTDT.Type=1` (Object ID) is unhandled — 388 packages silently sandbox

**Highest-value fix, and mechanical.**

`pack_converter._choose()` tests `PTDT.Type == 0` only, in both the UseItemAt
branch and the Find branch. `_operate_target()` likewise returns `False` for any
non-zero PTDT type, so a type-1 target never even reaches the Activate test.
Everything else falls into the Sandbox fallback with its target discarded.

Measured (Oblivion):

| Case | Count | Target base records |
|---|---|---|
| UseItemAt (type 8) → Sandbox | 318 | MISC 232, BOOK 36, APPA 8, ALCH 7, CONT 2, STAT 2, ACTI 1, WEAP 1, unresolved 29 |
| Find (type 0) → Sandbox | 70 | NPC_ 20, WEAP 11, INGR 11, FURN 11, CONT 6, MISC 6, CREA 6, ALCH 3, … |

TES4 `PTDT.Type=1` means **Object ID** — "use/find *a* thing of this base form
nearby", not a specific placed ref. Examples: `SEZoeMelenePaintsOutside`
(MISC easel), `SEAtrabhiManiaRead` (BOOK), `SE11AlchemyStation6x4` (APPA),
`SQ01SjirraDrinks` (ALCH), `SE12GnarlFindContainers2/3` (CONT).

Skyrim has a direct expression for this and **the converter already uses it
elsewhere**: `PTDA type 2` ("Object Type") is what every vanilla Eat/Sleep
instance supplies via `build_object_type_target()`. A type-1 TES4 target maps
1:1 into `build_target()` (which already accepts types 0..2) — the branches just
never call it.

### 2. `_operate_target()` misses real object classes

`OPERABLE_SIGS = {ACTI, DOOR, CONT}`. Measured UseItemAt targets with
`PTDT.Type=0`:

```
ACTI 78   (unresolved) 8   STAT 7   FURN 2   DOOR 2
```

The **7 STAT** (`SE09RelmynaStudyAtronach`, `SE09RelmynaStudyAtronachNoVictims`,
`SE03GrummiteGnarlDancePKG`) and **8 unresolved** refs
(`MS39SinderionMakesElixir02/03/04`) fall through to `SIT_TARGET` — i.e. the
actor is told to sit on a statue, which is the same failure mode as the Renault
wall-switch bug that `_operate_target` was written to fix.

The unresolved 8 are a second-order symptom of gap 3: `_ref_base_sig` is built
only from `by_type`, so a ref whose base is not in the indexed signature set
resolves to empty and the branch cannot classify it.

Separately, `FURNITURE_SIGS = {FURN, CHAI, BED }` mixes vocabularies:
`CHAI` and `BED ` are **TES5 `wbObjectTypeEnum` names, not TES4 record
signatures**. No TES4 record ever carries them, so only `FURN` can ever match.
Harmless today (the fallthrough is Activate, which is usually right) but the set
is misleading and should be `{FURN}` with a comment, or extended properly.

### 3. `PackagePlan.build()` is master-blind

`import_main.py` Phase 0g builds the plan from **`by_type` alone**:

```python
pack_plan.build(by_type,
                {get_formid(r, 'FormID') for r in by_type.get('QUST', [])},
                _sv_owner)
```

…while `load_package_types(by_type, _master_export)` and
`build_script_var_map(by_type, _master_export)` on the *adjacent lines* both take
the master export. `_base_sig` / `_ref_base_sig` in the same phase are likewise
built from `by_type` only.

Consequences for any plugin with masters (Morrowind_ob and the ESPs; Nehrim and
Oblivion are standalone):

* a master-owned QUST is absent from `quest_fids`, so a package gated on it gets
  no `owner_quest` → no QNAM, no ALPC, no alias routing;
* a master-owned ACHR/ACRE is absent from `base_to_ref`, so the actor gets no
  alias and its quest packages fall back to the standing schedule;
* a master-owned base record is absent from `_base_sig`, so `_operate_target`
  and the actor-Find branch silently take the Sandbox fallback.

Confirmed on `Morrowind_ob - Chargen and Transport Mod.esp`: 12 own PACK records
but **14 AIPackage references**, 2 of them master-owned (`0002C81C`).
`load_package_types` already covers those 2; the *plan* does not.

This is the recurring defect named in [CLAUDE.md](../CLAUDE.md#master-blindness).

### 4. 48 packages lose their entire condition gate

Measured through the production path
(`convert_ctda_list_with_strings` with a real `script_vars` map):

| Plugin | Packages w/ conditions | CTDAs | Kept | Dropped | Lost ALL conditions |
|---|---|---|---|---|---|
| Oblivion.esm | 3,874 | 6,172 | 6,054 | 118 (1.9%) | **48** |
| Nehrim.esm | 812 | 1,141 | 1,141 | 0 (0.0%) | 0 |

Dropping a condition **fails open**. That is defensible for dialogue (Oblivion's
call site already chose speaker+topic) but it is *inverted* for a package: an
ungated package runs whenever it reaches the top of the actor's list.

The 48 split cleanly by cause:

* **34 × func 53 `GetScriptVariable`** whose variable name could not be resolved
  through REFR → base → SCRI → SCPT. `SE08XedVictim01..05GreetPlayer` and
  `...Flee` now force-greet / flee unconditionally.
* **14 × func 171 `IsPlayerInJail`** — Oblivion-only, no TES5 equivalent, so it
  is correctly in `_FUNC_DROP`. But every city's `ChorrolJailorWanderPlayerInJail`,
  `AnvilJailorWanderPlayerInJail`, `ICPrisonJailorNightPatrol`,
  `BravilJailorNightPickpocket` etc. now runs **always** instead of only while
  the player is jailed.

For func 171 there is a real TES5 expression available (the jail/prison state is
reachable via faction/quest state or a `GetInCell` on the prison cell); for func
53 the fix is to widen the script-var resolution rather than to drop.

A package that loses its last condition should arguably be given a
never-passing gate instead of an always-passing one — the Oblivion behaviour
"only under condition X" is closer to "never" than to "always" when X is
untranslatable.

### 5. Escort/Follow discard the authored `PLDT.Radius`

`build_location()` preserves radius correctly, but both escort paths —
Follow-with-location → `ESCORT` and plain `T4_ESCORT` — set only
`target`/`location`/`ride_horse` and leave the template's default spacing
(`120.0` / `256.0` / `512.0` from `ESCORT.defaults`).

TES4's `PLDT.Radius` on an escort is the **arrival radius**, and arrival is what
ENDS the package — `OnPackageEnd` is where these quests advance.
`CGEmperorToMarkerB` authors radius 70. This is the same class of bug as the
original Follow→Escort routing fix (which was made because Follow "never
ARRIVES"), one level down.

---

## Scale

Template distribution in the built ESM (`output/Oblivion.esm/Oblivion.esm`,
7,209 PKCU records):

```
Sandbox   2878      Follow       231      FleeTo     11
Travel    2156      ForceGreet   153      SitTarget  10
Eat        829      Activate     113      UseMagic    5
Sleep      725      Escort        98
```

**2,878 packages (40%) land on Sandbox**, of which ~1,167 are Find/UseItemAt
fallbacks that discarded a target (513 Find + 654 UseItemAt per
`tools/esm/pack_audit.py`). Gap 1 alone accounts for 388 of them.

## Reproducing

```bash
python tools/esm/pack_audit.py --export export/Oblivion.esm      # template routing + data loss
python tools/esm/pack_validate.py output/Oblivion.esm/Oblivion.esm --summary
python tools/esm/pack_template_dump.py --list                    # vanilla template roots
```

The PTDT/PLDT censuses and the condition-drop measurement were ad-hoc reads of
`export/<plugin>/PACK.txt` plus `dialog_conditions.convert_ctda_list_with_strings`;
if they need to be repeated often they belong in `pack_audit.py` as new flags
rather than in a fresh script.

---

## Status after the 2026-08-17 fix pass

Everything below was measured with `python tools/esm/pack_audit.py --detail`
(which now builds the import's own context via `tes5_import.pack_indexes`).
Oblivion routing before → after, Find/UseItemAt only:

```
Find      Sandbox 513 → 194   Travel 236 → 337   Acquire 0 → 72   Sit 0 → 16
          SitTarget 0 → 17    ForceGreet 79      Activate 26
UseItemAt Sandbox 662 → 656   Activate 87        Sit 0 → 6        SitTarget 2
```

### Fixed

* **Gap 1 (Object-ID / Object-Type targets).**
  `pack_converter._find_object_criteria` + `object_criteria_kind`:
  * actor base with **one** placement → the Object ID *is* that ref → Travel
    near it (alias-routed like any specific ref);
  * actor base with several placements (FGC06Goblin ×9, FGD08Goblin ×11,
    FGC01 lions/rats) → a **chain of Follow packages, one per placed target,
    nearest the hunter first**, each gated on the source's conditions +
    `GetInSameCell(target)`, `GetDisabled==0` and `GetDead==0` on the target,
    ahead of the source in the alias ALPC / PKID list; the source itself
    (a wander-only in-cell Sandbox) is the tail. FormIDs are derived from
    (source PACK, target ref). **Measured live 2026-08-18** (game bridge, FGC06
    at stage 30): with the Sandbox alone all three fighters were RUNNING their
    hunt package (`getiscurrentpackage`) yet stayed within ~300 units of spawn
    — the Sandbox wander is local; a PLDT type-4 "Object ID" location patched
    into the live package left the fighter standing (type 4/5 are dead in the
    engine); the mine's navmesh is one component (`tools/navmesh/reach.py`).
    Oblivion: 6 hunts → 46 seek links;
  * item base/type (WEAP/INGR/MISC/ALCH/BOOK/… incl. `SEBruscusDannusFind*`,
    `HeroLoot`, the goblins' totem staffs) → **Acquire** with the same
    criteria and `PTDT.Count` as num-to-acquire (vanilla
    `MQ101RalofGetDoorKey` type 1, `MS09Stage25JonAcquireNote` type 0);
  * furniture base/type → **Sit** with the criteria (vanilla
    `MG06Stage99MirabelleGetIntoFurniture` type 1, `DA14StartSamSit` type 2),
    also for UseItemAt "any furniture" (6);
  * a specific STAT/other ref (`SEBlackrootFindPrisonerNNTarget`, 101) →
    Travel near it; a specific FURN ref → SitTarget; a specific item ref →
    Acquire.
  * **The TES4 and TES5 object-TYPE enums differ** (Apparatus/Clothing/
    NPCs/Creatures/Soul Gems exist only in TES4, everything after Activators is
    shifted). `TES4_TO_TES5_OBJECT_TYPE` translates every PTDT type-2 target
    and PLDT type-5 location; before, the value was parsed as hex and
    load-order-shifted (TES4 "Furniture" 12 → written as 0x0100000C).
  * A PLDT type-4 (Object ID) location whose base has one placement is
    written as the type-0 reference (4,048 vanilla uses vs 0 for type 4).
* **Gap 2.** `_base_sig` now covers every placeable signature
  (`pack_indexes.PLACEABLE_BASE_SIGS`), so MS39's APPA mortar resolves;
  UseItemAt at a carriable item ref falls to the sandbox (Activate would pick
  it up), STAT stays Activate. `FURNITURE_SIGS = {FURN}`.
* **Gap 4, func 53.** `build_script_var_map` was blind to scripted
  WEAP/MISC/… bases (21 PACK + 13 INFO conditions, e.g. the goblin
  `CreatureGoblinLeaderFindHead*` totem gates). Beyond that, an unresolvable
  script variable is now emitted against `::TES4NoSuchVariable_var` — reads
  0, exactly TES4's value for a missing variable — instead of being dropped
  (fail-open). SE08's five Xedilian victims no longer force-greet/flee
  unconditionally. Func 171 `IsPlayerInJail` stays dropped: Skyrim has no
  equivalent function (`GetArrestedState` is the arrest, not the sentence).
* **AddScriptPackage** (not in the original audit): packages forced on by
  script and gated on a quest are attached to the actor's alias as ALPCs
  (`pack_aliases.build_script_assigned_packages`; 15 Oblivion / 4 Nehrim,
  incl. `MQ12MartinPlaceWelkyndStone`, `MQ14MartinPlaceSigilStone`,
  `MQ15MartinOpenPortal`, `MQ00CalebroPackage04`). Unconditioned forced
  packages (52 / 20) are NOT attached — with no gate they would run whenever
  the quest runs — and remain the known `AddScriptPackage → EvaluatePackage`
  gap.

### The audit was wrong about

* **Gap 3** — `PackagePlan.build` and the base-signature indexes already take
  `master_export` (checked in HEAD before this pass).
* **Gap 5** — Escort's arrival radius IS the destination location's radius,
  which `build_location` preserves (CGEmperorToMarkerB writes PLDT type 8
  radius 70 in the built ESM). Slots 3/4/5 are follower spacing, which TES4
  does not author.

### Still open (measured)

* UseItemAt with an object-type/token criteria — 232 `aaaObeisanceToken` /
  `aaaPreachToken` / Hoe / Rake / PaintBrush MISC tokens, 79 "read any book",
  55 "any melee weapon" drills, 142 "None" — keep the sandbox at the location.
  Oblivion drives these through IDLE records conditioned on the used item;
  Skyrim's equivalent is a placed IdleMarker/furniture + UseIdleMarker/
  UseWeapon-with-a-dummy, i.e. synthesised references, not a template choice.
* Find at a container/door/activator *type* (SE12 gnarl chests ×6, obelisk,
  cathedral doors) and at "any NPC" (ImpEx couriers ×35): sandbox in the
  authored cell. `ActivateAfterFinding` exists as a root but has 0 instances.
* Unconditioned script-forced packages (above).
