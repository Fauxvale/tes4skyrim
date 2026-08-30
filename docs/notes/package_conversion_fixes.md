# PACK conversion: the 2026-08-17 fix pass

What changed after the audit, including two findings the audit got wrong.

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
