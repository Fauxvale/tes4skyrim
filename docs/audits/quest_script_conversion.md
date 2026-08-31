# Quest Script Conversion Audit

Record of converted **quest scripts** (`SCPT` with `SCHR.Type=1`) that have been
read line-by-line against their TES4 originals. The point of this file is so a
later session does not re-audit the same scripts — check here first, then pick
from the unaudited remainder.

Corpus: **265** quest scripts in `Oblivion.esm`.
Audited so far: **196** quest scripts + 4 chargen actor scripts
(rounds 1-9, 2026-07-31).

How to reproduce the pairing — use `tools/script/script_pair.py`, which unescapes the
SCTX field and prints the original beside the emitted `.psc`:

```bash
python tools/script/script_pair.py MQ02Script            # original + converted
python tools/script/script_pair.py MQ02Script --conv-only
python tools/script/script_pair.py --list-quest          # every SCHR.Type=1 EditorID
python tools/script/script_pair.py --file temp/names.txt # batch a sample
```

---

## Round 1 — 2026-07-31 (random sample of 20)

Sampled with `random.seed(4242)` over the 265 `SCHR.Type=1` records.

| # | Script | Verdict |
|---|---|---|
| 1 | `MQ02Script` | **Bug: GetInCell family** (fixed) |
| 2 | `MQ00Script` | Clean — declaration-only |
| 3 | `GenericDialogueScript` | Clean — declaration-only |
| 4 | `SQ08Script` | Clean |
| 5 | `MS37Script` | Clean |
| 6 | `SE34Script` | Clean |
| 7 | `Dark07MedicineScript` | **Bug: IsActorDetected self-compare** (fixed) + GetInCell |
| 8 | `DarkVampScript` | Clean — MenuMode→`OnSleepStart` routing correct |
| 9 | `MS02Script` | **Bug: WakeUpPC → ForceThirdPerson** (fixed) |
| 10 | `ICALLNQDScript` | Clean — declaration-only |
| 11 | `Dark17FollowingScript` | Clean — `Key`→`myKey` reserved-word rename correct; `DisablePlayerControls` pairing lives in the follow-up INFO, not a leak |
| 12 | `ArenaScript` | Clean — `X.WearingArmor` is a legitimate remote script-variable read |
| 13 | `SE41Script` | Clean |
| 14 | `MG19Script` | Clean — `GameDaysPassed as Int` is correct (see note below); comment corrected |
| 15 | `HouseServantsScript` | Clean — `Evp`→`EvaluatePackage()` |
| 16 | `TGQuestTalkScript` | Clean — declaration-only |
| 17 | `SE36QuestScript` | Clean — comment-only source |
| 18 | `EmfridDEMOScript` | Clean — declaration-only |
| 19 | `Dark08WhodunitScript` | Clean — negated `GetInCell == 0` → `!(...)` correct; case-variant families share one helper |
| 20 | `HouseBravilFurnScript` | Clean — `SetOwnership`→`SetActorOwner(...GetActorBase())` |

**16 clean / 3 with real defects / 1 with a wrong code comment.**

---

## Round 2 — 2026-07-31 (random sample of 20)

Sampled with `random.seed(9137)` over the 245 `SCHR.Type=1` records not in round 1.

| # | Script | Verdict |
|---|---|---|
| 1 | `ImperialExpressScript` | Clean — declaration-only |
| 2 | `MQ13Script` | Clean — Say beat (`+ .5`) and `EssentialDeathReload` no-op both correct |
| 3 | `SE07AScript` | **Bug: duplicate Say** (fixed) |
| 4 | `TGStolenGoodsScript` | **Bug: GetAmountSoldStolen → wrong stat** (fixed) |
| 5 | `HouseChorrolFurnScript` | Clean |
| 6 | `Dark19WhispersScript` | **Bug: SCAOnActor → StopCombat** (fixed) |
| 7 | `Dark06WandererScript` | Clean — round-1 GetInCell family fix verified working |
| 8 | `DAClavicusVileScript` | Clean |
| 9 | `DASheogorathScript` | Clean — Say timer park correct; SetWeather no-op is intended |
| 10 | `SQ06Script` | Clean — script-typed property on `GetItemCount` is correct (see below) |
| 11 | `HouseBrumaFurnScript` | Clean |
| 12 | `SkingradNQDScript` | Clean — declaration-only |
| 13 | `MS31Script` | Clean — MenuMode→`OnSleepStart` routing correct |
| 14 | `Dark16KissScript` | **Bug: ModDisposition direction inverted** (fixed) |
| 15 | `MS26Script` | Clean |
| 16 | `MS38Script` | Clean — `ModDisposition +20` correctly `;NE` |
| 17 | `MS48Script` | Clean — declaration-only |
| 18 | `MG00Script` | Clean |
| 19 | `SE05QuestScript` | Clean — chained `a == b == 0` verified correct (see below) |
| 20 | `SE13Script` | Clean |

**16 clean / 4 with real defects.** One cross-cutting artifact
(`Game_GetPlayer__`) was found while reading these and fixed separately.

---

## Round 3 — 2026-07-31 (chargen + main quest, 20 scripts)

Not a random draw: the whole unaudited MQ/chargen set (16 of the 19 `MQ*`/
`CharGen*` quest scripts) plus the 4 chargen **actor** scripts that drive the
prison-escape sequence, because that sequence has produced repeated soft-locks.

| # | Script | Verdict |
|---|---|---|
| 1 | `CharGenQuest` | **Bug: GetDetected direction** (fixed) |
| 2 | `MQ01Script` | **Bug: encumbrance AV split** (fixed) |
| 3 | `MQ04Script` | **Bug: `speaker > 0` null check destroyed** (fixed) |
| 4 | `MQ05Script` | **Bug: GetDetected direction** (fixed) |
| 5 | `MQ06Script` | Clean |
| 6 | `MQ07Script` | Clean — declaration-only |
| 7 | `MQ08Script` | Clean — declaration-only |
| 8 | `MQ09Script` | **Bug: `restrainedRef > 0` null check destroyed** (fixed) |
| 9 | `MQ10Script` | Clean |
| 10 | `MQ11Script` | Clean |
| 11 | `MQ12Script` | Clean |
| 12 | `MQ14Script` | **Bug: literal `%.0f` in Great Gate countdown** (fixed) |
| 13 | `MQ15Script` | Clean — cell-family helper + Say park correct |
| 14 | `MQ16Script` | **Bug: door inverted; marker cast to Actor** (both fixed) |
| 15 | `MQConversationsScript` | Clean — declaration-only |
| 16 | `MQDragonArmorQuestSCRIPT` | Clean |
| 17 | `CGEmperorScript` | **Bug: `combattarget > 0` null check destroyed** (fixed) |
| 18 | `CGGlenroyScript` | Clean — OnHit block merge order correct |
| 19 | `MartinScript` | Clean — package-event routing correct; `AddScriptPackage` gap |
| 20 | `BaurusScript` | Clean — SetOpenState/GetOpenState encodings match |

**13 clean / 7 with real defects.** Every defect was cross-cutting rather than
script-specific: the six fixes together touch 8 null checks, 38 detection calls,
86 message strings, 4 AV reads, 7 doors and 2 remote assignments.

---

## Round 4 — 2026-07-31 (Fighters + Thieves Guild, 20 scripts)

Sampled with `random.seed(5150)` over the 38 unaudited `FG*`/`TG*` quest
scripts (40 total, less `TGStolenGoodsScript` and `TGQuestTalkScript` from
rounds 1-2).

| # | Script | Verdict |
|---|---|---|
| 1 | `FGC01Script` | **Bug: 2-arg StartConversation dropped** (fixed) |
| 2 | `FGC03Script` | Clean |
| 3 | `FGC04Script` | Clean — declaration-only |
| 4 | `FGC05Script` | Clean |
| 5 | `FGC06Script` | Clean — `evaluatepackage` + 3-arg StartConversation both correct |
| 6 | `FGC07Script` | Clean |
| 7 | `FGC08Script` | Clean |
| 8 | `FGC09Script` | Clean |
| 9 | `FGD01DefaultScript` | Clean — quoted EditorID arg resolves |
| 10 | `FGD02Script` | Clean — see the speaker-4 note below |
| 11 | `FGD03ViranusScript` | Clean — cell-family helper correct |
| 12 | `FGD04DefectorScript` | Clean |
| 13 | `FGD05OreynScript` | Clean — `GetDeadCount`, `GetIsCurrentPackage` correct |
| 14 | `FGD07Script` | Clean |
| 15 | `TG02TaxesScript` | Clean — declaration-only |
| 16 | `TG03Main` | Clean — declaration-only |
| 17 | `TG07LexScript` | Clean — round-2 `TES4GoldFenced` fix verified working |
| 18 | `TG08BlindScript` | Clean — `GetStageDone` + cell family correct |
| 19 | `TGCastOut` | **Bug: murder/attack collision + crime-gold booleans** (fixed) |
| 20 | `TGInfoScript` | Clean — declaration-only |

**17 clean / 3 with real defects.** All three defects were cross-cutting, and
chasing the `TGCastOut` one uncovered two further defects in the *record*
pipeline (export and import) that no script-level reading would have shown.

---

## Round 5 — 2026-07-31 (rest of FG/TG + first Mages Guild, 20 scripts)

Not a random draw: **all 17 remaining unaudited `FG*`/`TG*` quest scripts**
(which completes both guild families), plus the first 3 `MG*` scripts.

| # | Script | Verdict |
|---|---|---|
| 1 | `FGC03FlagonScript` | Clean |
| 2 | `FGC02Script` | Clean |
| 3 | `FGC10Script` | Clean |
| 4 | `FGD06Script` | Clean |
| 5 | `FGD09Script` | Clean — single-cell `GetInCell` correctly a plain equality |
| 6 | `FGQuestTrack` | Clean |
| 7 | `FGPostQuest` | Clean — `GetRandomPercent`→`Utility.RandomInt(0, 99)` correct |
| 8 | `FGD08Script` | Clean — R4-5 `Say(GREETING)` + cell family both verified working |
| 9 | `FGConversationScript` | **Bug: `AddTopic` dropped** (fixed); short truncation of `FGRewards * .8` correct |
| 10 | `TG04MistakeScript` | Clean — declaration-only |
| 11 | `TG01BestThiefScript` | Clean — declaration-only |
| 12 | `TG06AtonementScript` | Clean — declaration-only |
| 13 | `TG05MisdirectionScript` | Clean — declaration-only |
| 14 | `TG09ArrowScript` | Clean — but depends on R5-2 |
| 15 | `TG10BootsScript` | Clean — but depends on R5-2 |
| 16 | `TG11HeistScript` | Clean — `Message` display-time dropped, cell family, `ModCrimeGold` all correct |
| 17 | `TG00FindThievesGuildScript` | **Bug: `IsPlayerInJail` → expulsion** (fixed) |
| 18 | `MG01Script` | **Bug: early `Return` kills the poll** (fixed) |
| 19 | `MG02Script` | Same early-`Return` defect; `unlock`→`Lock(false)` correct |
| 20 | `MG03Script` | Clean — quoted EditorID arg resolves |
| — | `MG09TestScript` | Read while chasing R5-1 (the `AddTopic` site) |

**16 clean / 4 with real defects.** As in round 4 every defect was
cross-cutting; two of them (R5-2, R5-3) were invisible at the script level and
only showed up when the *built ESM* and the whole corpus were measured.

---

## Round 6 — 2026-07-31 (rest of Mages Guild + Dark Brotherhood, 20 scripts)

Sampled with `random.seed(7311)` over the 20 unaudited `MG*` scripts and the 17
unaudited `Dark*` scripts. Two 3-line stubs drawn in the first pass
(`Dark01KnifeScript`, `Dark01KnifeFINScript` — declaration-only) were swapped
for `Dark02WateryScript` and `Dark03AccidentsScript`.

| # | Script | Verdict |
|---|---|---|
| 1 | `MG08Script` | Clean — `SetAV Aggression 40`→ enum tier 2 correct |
| 2 | `MG04Script` | Clean — `SetEssential` base-form arg, cell family incl. exterior |
| 3 | `MG14Script` | Clean — `cast` receiver/arg remap correct |
| 4 | `MGMageConversationFollowScript` | Clean — stray `endif` absorbed; R5-3 re-register present |
| 5 | `MG16Script` | Clean — `GetQuestRunning == 1` folds to `IsRunning()` |
| 6 | `MG15Script` | Clean |
| 7 | `MG12Script` | Clean |
| 8 | `MG09Script` | Clean — odd source nesting + stray backtick reproduced faithfully |
| 9 | `MGExpulsion02Script` | Clean — R4-1 crime split correct |
| 10 | `MG05Script` | Clean |
| 11 | `MG13Script` | **Bug: bare MenuMode dropped** (fixed) |
| 12 | `Dark18MotherScript` | Clean — `SetAlert`, `ClearLookAt`, timer/poll interval all correct |
| 13 | `Dark02WateryScript` | Clean — 4-arg Say override dropped, `ModDisposition -100`→StartCombat |
| 14 | `DarkBrotherhoodScript` | **Bug: `IsPCAMurderer` → literal 0** (fixed) |
| 15 | `DarkExiledScript` | Clean — R4-1 + sleep routing correct; redundant double `StartCombat` |
| 16 | `Dark03AccidentsScript` | **Bug: quoted `PlaySound` minted a dead property** (fixed) |
| 17 | `Dark14Script` | Clean |
| 18 | `Dark04ExecutionScript` | **Bug: `GetDetectionLevel` → literal 0** (fixed) |
| 19 | `Dark09RetirementScript` | Same MenuMode + detection defects; both fixed |
| 20 | `DarkConvoScript` | Clean — all 6 three-arg `StartConversation`→`Say(topic)` |

**16 clean / 4 with real defects.** As in rounds 4-5 every defect was
cross-cutting rather than script-specific, and three of the four were invisible
in the script text alone — they only showed up by censusing the whole corpus for
the shape.

---

## Round 7 — 2026-07-31 (random sample of 20)

Sampled with `random.seed(2718)` over the 124 `SCHR.Type=1` records not named in
rounds 1-6.

| # | Script | Verdict |
|---|---|---|
| 1 | `SE07BScript` | Clean — R2-4 Say dedup, the countdown RMW guard, `SetAlert`, `Look`→`SetLookAt` all correct; commented failsafes are commented in the original |
| 2 | `SEHaskillSummonQuestScript` | Clean — remote script-variable writes typed through the target's script |
| 3 | `MS13AltScript` | Clean — `SetNoRumors` genuinely has no Papyrus native (see below) |
| 4 | `SE11QuestScript` | Clean — `GetStageDone == 1/0` folding correct |
| 5 | `SE03AScript` | Clean — `Activate X 1` run-flag handled; `PickIdle`→`IdleForceDefaultState` |
| 6 | `SESacellumSpeechScript` | Clean — 2-cell family helper |
| 7 | `WeatherVARScript` | Clean — declaration-only, correctly `Conditional` |
| 8 | `GenericScript` | Clean — `MenuMode 1034` is a genuine menu-ID block (R6-1 exception) |
| 9 | `SE04FinScript` | Clean — `state`→`myState` rename propagates to the remote writer |
| 10 | `WabbajackQuestScript` | Clean — `GetDead == 0` → `!IsDead()` |
| 11 | `SE09QuestScript` | Clean — `SetWeather` no-op is the R2 rule; 5-cell family |
| 12 | `MS46Script` | **Bug: `extends Actor` on a WEAP** (fixed) — found via `GoblinHeadScript` |
| 13 | `QuestRefTest` | Clean — declaration-only |
| 14 | `BrumaNQDScript` | Clean — declaration-only |
| 15 | `SE04QuestScript` | Clean — whole withdrawal state machine; case-variant names resolve to one property |
| 16 | `MS23Script` | Clean — 2-cell family, `fQuestDelayTime` inert-property rule |
| 17 | `SE00Script` | Clean — script-only `AddTopic` correctly ungated (R5-1) |
| 18 | `SE38SCRIPT` | Clean — `RemoveAllItems(dest, keepOwnership)` argument order matches |
| 19 | `MS49Script` | Clean — R5-3 early-`Return`, R4-5 `Say(GREETING)` |
| 20 | `SENQDManiaScript` | Clean — declaration-only |

**19 clean / 1 with a real defect** — but that one defect was the largest of any
round so far: it is cross-cutting, has **four independent causes**, and was
confirmed in the user's own Papyrus log as breaking **67 scripts outright**.

---

## Round 8 — 2026-07-31 (random sample of 20)

Sampled with `random.seed(31415)` over the 104 `SCHR.Type=1` records not named
in rounds 1-7. The draw happened to cover the Arena chain and a spread of
`MS*`/`SE*`/`DA*` quests, which is what round 7 recommended as the next target.

| # | Script | Verdict |
|---|---|---|
| 1 | `DAPeryiteScript` | Clean — `soulsVAR` is written by the 5 soul scripts; `GetStageDone`/`GetDisabled` fold correctly |
| 2 | `MS45Script` | Clean — R1 cell family (Chorrol 86 + Hackdirt 13), R5-3 re-arm, `ModDisposition +20` → `;NE`, `forceav mercantile` → `Speechcraft` |
| 3 | `SQ07Script` | Clean |
| 4 | `ArenaICGrandChampFights` | Clean — the null-`combatant` calls are a faithful no-op (see below) |
| 5 | `SE45Script` | Clean |
| 6 | `MS29Script` | Clean — `True`→`myTrue` rename, `Evp`, paired Disable/EnablePlayerControls |
| 7 | `DASanguineScript` | Clean — `IsSpellTarget` → `TES4Polyfill.HasMagicEffectByID`, `Spell.Cast(caster, target)` |
| 8 | `LeyawiinNQDScript` | Clean — declaration-only |
| 9 | `SENQDDementiaScript` | Clean — declaration-only |
| 10 | `MS05Script` | Clean — R5-3 early-`Return` re-arm |
| 11 | `ArenaAnnouncerScript` | Clean — 4-arg `Say` speaker override correctly dropped (see below) |
| 12 | `MS18Script` | Clean — declaration-only |
| 13 | `ArenaSpectatorScript` | Clean — `Activate X` → `Activate(X, true)`, `Unlock` → `Lock(false)` |
| 14 | `E3QuestScript` | Clean — remote script-variable read `BaurusRef.sayPlayer` |
| 15 | `CheydinhalNQDScript` | Clean — declaration-only |
| 16 | `SE11bQuestScript` | Clean — declaration-only |
| 17 | `MS11Script` | Clean — single-cell `GetInCell` plain equality, `GameDaysPassed as Int`, commented blocks preserved |
| 18 | `RewardTest` | Clean — declaration-only |
| 19 | `MS40Script` | Clean — whole endgame state machine; the 9→10 gap is in the original (`RonaHassildorScript` closes it via `HasMagicEffect REFA`) |
| 20 | `SE44Script` | Clean |

**20 clean / 0 defects.** The first round with no new defect. Four mechanisms
were investigated in depth and all four turned out to be already-correct — they
are recorded below so a later session does not re-chase them.

---

## Round 9 — 2026-07-31 (random sample of 40)

Sampled with `random.seed(1618)` over the 108 `SCHR.Type=1` records not named in
rounds 1-8. Twice the usual size, because round 8 found nothing.

| # | Script | Verdict |
|---|---|---|
| 1 | `DAAzuraScript` | Clean |
| 2 | `DABoethiaScript` | Clean |
| 3 | `DAMalacathScript` | Clean — cell family + `IsInInterior` |
| 4 | `DAMeridiaScript` | Clean |
| 5 | `DASkullofCorruptionQuestScript` | Clean — declaration-only |
| 6 | `DAVaerminaScript` | Clean — `ArkvedsTower07` correctly a plain equality (the *prefix* matches one cell, though 8 siblings share the stem) |
| 7 | `Dark11Script` | Clean — a property named `List` is legal (not a keyword, not a vanilla script) |
| 8 | `HouseCheydinhalFurnScript` | Clean — R5-3 early-`Return` re-arm |
| 9 | `HouseSkingradFurnScript` | Clean — `StartQuest` → `Start()` |
| 10 | `MG07Script` | Clean — `SetForceRun` → SpeedMult; **a latent inverted map entry was removed** (see below) |
| 11 | `MG17Script` | **Bug: `GetCurrentAIPackage == <type>` → literal 0** (fixed) |
| 12 | `MG18Script` | **Bug: `GetPlayerControlsDisabled` → literal 0** (fixed) |
| 13 | `MGExpulsion01Script` | Clean — R4-1 crime split correct |
| 14 | `MGPostQuestScript` | Clean — `isxbox` → `False` |
| 15 | `MS06Script` | Clean — `GetCurrentTime` → `GameHour`, remote var write |
| 16 | `MS09Script` | Clean — R5-4 `IsArrested()` verified working |
| 17 | `MS13Script` | Clean — unbalanced source `endif` absorbed |
| 18 | `MS22Script` | Clean — declaration-only |
| 19 | `MS27Script` | Clean — `GetInSameCell == 0`, `IsInInterior == 0` |
| 20 | `MS39Script` | Clean — the whole 4-tier Nirnroot potion timer chain |
| 21 | `MS91Script` | `GetCurrentAIProcedure` is a genuine gap (see below) |
| 22 | `MS93Script` | Clean |
| 23 | `MSShadowscaleScript` | Clean |
| 24 | `NQDAnvilNPCScript` | Clean — declaration-only |
| 25 | `NQDChorrolScript` | Clean — vars declared *inside* `begin gamemode`, correctly hoisted |
| 26 | `NQDGuardScript` | Clean — declaration-only |
| 27 | `NecroAnchoriteQuestScript` | Clean — `GameDay` global, remote var write |
| 28 | `SE01DoorScript` | Clean — Say park, `PlayGroup`→`PlayAnimation`, R2 `SetWeather` no-op |
| 29 | `SE02QuestScript` | Clean — 3-cell family, `IsSpellTarget`→EffectShader |
| 30 | `SE06SCRIPT` | Clean — `SetAV Aggression` enum bucketing |
| 31 | `SE12Script` | Clean |
| 32 | `SE14ProtectionScript` | `GetPlayerInSEWorld` — verified NOT worth fixing (see below) |
| 33 | `SE14Script` | Clean |
| 34 | `SE30QuestScript` | Clean |
| 35 | `SE32Script` | Clean — the whole Vitharn ghost-siege machine; `Package` props are SCRO preloads |
| 36 | `SE42Script` | Clean — R6-1 bare-MenuMode merge verified working (its *only* block) |
| 37 | `SENQDWildernessScript` | Clean — declaration-only |
| 38 | `SEStartUpScript` | Clean |
| 39 | `SQ09Script` | Clean |
| 40 | `TutorialScript` | Clean — R6-1 menu-ID exception; `GetPlayerInSEWorld == 0` → `0 == 0` is the *right* answer |

**38 clean / 2 with real defects.** Both defects were the same shape as R6-2 and
R6-3 — a working special handler shadowed by a fallback list — and both were
invisible in the script text, because the flattening emits no `;NE` marker at
all when the call is read bare.

Chasing R9-1 also exposed a **cross-stage divergence** that no script-level
reading could have shown, and a pre-existing structural gap (below).

---

## Not yet audited

109 of 265 quest scripts. Pick the next sample from the `SCHR.Type=1` records
not listed in the round-1 to round-8 tables above — `tools/script/script_pair.py
--list-quest` prints the full set to diff against.

Fully covered families:

* **MQ / chargen** — all 19 `MQ*`/`CharGen*` quest scripts (16 in round 3, plus
  `MQ00Script`/`MQ02Script` in round 1 and `MQ13Script` in round 2).
* **Fighters Guild** — **all 24 `FG*` scripts.** 14 in round 4, 9 in round 5,
  and `FGExpulsionScript` read in full while fixing R4-1/R4-4.
* **Thieves Guild** — **all 16 `TG*` scripts.** 4 in round 4, 8 in round 5,
  `TGStolenGoodsScript`/`TGQuestTalkScript` in rounds 1-2, and `TGCastOut`/
  `TG07LexScript`/`TG08BlindScript`/`TG02TaxesScript`/`TG03Main` in round 4.

**Mages Guild — 16 of 26 `MG*` scripts.** `MG01`/`MG02`/`MG03` in round 5,
`MG19Script` in round 1, `MG00Script` in round 2, `MG09TestScript` read while
chasing R5-1, and 11 more in round 6. Remaining: `MG05AScript`, `MG06Script`,
`MG07Script`, `MG10Script`, `MG11Script`, `MG17Script`, `MG18Script`,
`MGExpulsion01Script`, `MGPostQuestScript`. (`MGExpulsion01Script` shares
`MGExpulsion02Script`'s exact R4-1 shape, already read.)

**Dark Brotherhood — 16 of 24 `Dark*` scripts.** `Dark06`/`Dark07`/`Dark08`/
`DarkVamp` in round 1, `Dark16`/`Dark17`/`Dark19` in round 2, and 9 in round 6.
Remaining: `Dark01KnifeScript` and `Dark01KnifeFINScript` (3-line stubs),
`Dark05AssassinatedScript`, `Dark09FINScript`, `Dark10SanctuaryScript`,
`Dark10SpecialScript`, `Dark11Script`, `Dark12Script`.

**Arena — the driver chain is now read.** `ArenaScript` (round 1),
`ArenaAnnouncerScript`, `ArenaSpectatorScript` and `ArenaICGrandChampFights`
(round 8). Remaining: `ArenaAggressionScript`,
`ArenaGrandChampionMatchScript`, `ArenaRaimentScript`,
`ArenaSpectatorCombatantsScript`.

Next obvious sample: the rest of the `MS*` miscellaneous quests (still the
largest unaudited family), the remaining `SE*` Shivering Isles set, and the
`DA*` Daedric shrine quests — note that 9 `SE*` guard scripts and
`SEMiriliUlvenSCRIPT` were touched by R6-1/R6-3 but never read line-by-line, so
their bodies are worth reading now that the detection tests and the
bare-MenuMode bodies actually run.
