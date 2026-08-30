# Quest script conversion: verified-correct behaviour

Findings from the quest-script audit rounds that are CORRECT and must not
be "fixed". Split out of the audit so they are not read as stale.

## Verified-correct behaviours from round 9 (do NOT "fix" these)

* **`SetForceRun 1` → `SetActorValue("SpeedMult", 150.0)`** (`MG07Script`).
  Skyrim keeps `SetForceRun` as a console/condition function (index 209) but
  exposes no Papyrus native — checked vanilla `Actor.psc` and SKSE. SpeedMult is
  the reachable approximation.
* **A property named `List`** (`Dark11Script`). `_PAPYRUS_RESERVED` covers the
  keywords plus every vanilla script name; `List` is neither, and the script
  compiles.
* **`GetInCell ArkvedsTower07` as a plain equality** (`DAVaerminaScript`). There
  are 8 `ArkvedsTower*` cells, but the round-1 rule matches the *argument* as a
  prefix and only `ArkvedsTower07` starts with `ArkvedsTower07`.
* **`GetPlayerInSEWorld` staying the literal 0.** This was investigated in depth
  and deliberately left alone; the reasoning is worth keeping because the
  "obvious" fix is a regression:
  * The **exterior** half is trivially reconstructible
    (`GetWorldSpace()` against the 13 `SE*` worldspaces). The **interior** half
    is not: an SI interior cell has no worldspace at all, carries no
    distinguishing climate or music (measured — SI interiors use the same music
    types as Cyrodiil's), and the **door graph does not separate the two
    worlds**, because the SI↔Cyrodiil gate is a legitimate edge. A flood fill
    from the SE worldspaces reaches **1,407 Cyrodiil interiors**; even depth-1
    leaks. There is no sound generic invariant, and an EditorID-prefix
    heuristic would be a fit-to-one-plugin patch.
  * Reconstructing only the exterior half would be **worse than the no-op**.
    Censused over the plugin, **11 of the 16 sites test `== 0`** — they are
    suppression guards (Lucien Lachance's sleep visit in `RufioDieScript`, the
    Gray Cowl's bounty transfer, `TutorialScript`'s jail hint), for which a
    constant 0 is the *right* answer everywhere in Cyrodiil. An exterior-only
    test would flip all 11 to false the moment the player entered an SI
    interior.
  * The 5 `== 1` sites do lose their behaviour, but 4 are in SI spell scripts
    that do not run at all (see the MGEF gap below).
* **71 unbound `Package` properties are declaration-only SCRO artifacts.** A
  sweep of all 242 scripts declaring a `Package` property found 73 unbound; 71
  are never referenced in the body (the round-8 `_preload_scro_refs` category).
  The 2 live ones are both explained by the gaps below.

## Verified-correct behaviours from round 8 (do NOT "fix" these)

* **The 4-argument `Say`'s speaker override, dropped — re-confirmed, and the
  earlier reasoning corrected.** Rounds 3 and 6 justified the drop as "the
  emitting ref is already the intended speaker". That justification is wrong:
  censused over SCPT/INFO/QUST there are **282** such sites, the emitter is an
  actor in exactly **1**, and the speaker argument is an `NPC_` in **281**. The
  emitters are shrines, statues, doors and Arena markers.

  The drop is nevertheless correct, for a different reason. Two facts settle it:
  1. **9 of 12** sampled speaker NPCs (`ArenaMouth`, `DAAzuraVoice`,
     `SEJyggalagVoice`, `DarkDoorSpeakNPC`, …) have **zero ACHR placements** —
     they are disembodied voice-carrier records that exist only to own a set of
     INFOs, so "make that actor speak" is not what the parameter can mean.
  2. Every topic used this way (`Announcer`, `SE13JyggalagSpeech`,
     `MQ15MankarRant`, `DABoethiaSpeech`, `SE01GateSpeech`, `DarkDoorSpeak`)
     carries INFOs with **no conditions at all**, so `emitter.Say(topic)`
     selects a line regardless of who the emitter is.

  A talking activator is normal in both games (Skyrim's own Daedric shrines are
  activators, and `ObjectReference.Say()` works on them), so keeping the emitter
  as the audio source reproduces Oblivion. Voice-file *routing* for these lines
  is a separate subsystem — see the voice-audit notes, not this file.

* **Calling a method on a `None` ref does not kill the polling loop**
  (`ArenaICGrandChampFights`). The script assigns up to three `combatant`
  refs and then unconditionally calls `setav`/`startcombat` on all three, so in
  every one- or two-creature fight the trailing calls run on `None`. Papyrus
  logs *"Cannot call X() on a None object, **aborting function call**"* and
  continues the frame — confirmed in a Papyrus log where the same script logged
  the error at 09:49:31 and again at 09:49:32, i.e. the `OnUpdate` re-registered
  in between. Only the offending call is skipped, which is exactly Oblivion's
  silent no-op on an unset ref. No guard needed.

* **A `TES4_<Script>`-typed property bound to a placed REFR.** 933 such sites
  (plus 2,252 ACHR and 253 ACRE). It looks unbindable, because the script is
  attached to the *base object* and the property names the *reference* — but
  Skyrim instantiates a base record's VMAD scripts on every placed reference,
  which is why `SCRIPTABLE_TYPES` in `tes5_import/object_scripts.py` includes
  the base signatures in the first place. `cross_ref.get_record_script_type`
  deliberately follows the `NAME` chain for this reason. Correct as written.

* **Dead `MagicEffect Property <CODE> Auto` declarations** (61, e.g. `FISH`,
  `REHE`, `BABO`). TES4's SCPT record carries a **SCRO** list of every form the
  compiled script referenced, and `_preload_scro_refs` declares a property for
  each. `PlayMagicEffectVisuals FISH` legitimately converts to the effect's
  EFSH (`effectFireShield.Play(...)`), so the MGEF property is left unreferenced
  — declared and bound, but never read. Harmless; 907 external-ref properties
  are unreferenced for the same reason (a converted call that no longer names
  the form). Not the R6-4 shape, which minted an *unbindable* quoted name.

---

## Verified-correct behaviours from round 7 (do NOT "fix" these)

* **`SetNoRumors` as `;NE`** (`MS13AltScript`). Skyrim keeps it as console /
  condition function 321 and the engine has `ExtraHasNoRumors`, but neither
  vanilla `.psc` nor SKSE exposes a Papyrus native — there is genuinely nothing
  to call.
* **`Activate X 1` dropping the trailing `1`** (`SE03AScript`). TES4's flag
  means "activate even if blocked"; the handler maps run-flag 1 to
  `Activate(x)` (full processing) and everything else to `Activate(x, true)`,
  which is what stops the OnActivate storm recorded in
  `project_activate_conversion`.
* **`PickIdle` → `Debug.SendAnimationEvent(ref, "IdleForceDefaultState")`**
  (`SE03AScript`). TES4 `PickIdle` (opcode `0x1064`, 0 params) forces an idle
  re-selection; the default-state reset is the standard Skyrim idiom.
* **`RemoveAllItems(dest, 1)`** (`SE38SCRIPT`). Both games put
  keep-ownership second — vanilla is
  `RemoveAllItems(akTransferTo, abKeepOwnership, abRemoveQuestItems)`.
* **`begin MenuMode 1034` preserved as a comment** (`GenericScript`). The
  persuasion minigame is a genuine menu-ID trigger — one of the 5 the R6-1 fix
  deliberately left dropped.
* **`GetSecondsPassed` → the fixed poll interval** (`SE04FinScript`). It makes
  the Felldew timer run 0.4s rather than ~20s, but it is the documented
  corpus-wide substitution, not a per-script defect.
* **A script-only `AddTopic` emitting `;NE: AddTopic (topic not gated)`**
  (`SE00Script`'s `SEFellmoorTopic`). R5-1's rule: a topic added only from a
  SCPT is never placed in `explicit_targets`, so it is ungated and already
  visible — there is no global to set.

---

## Verified-correct behaviours from round 6 (do NOT "fix" these)

* **`SetAV Aggression 40` → `SetActorValue("Aggression", 2)`** (`MG08`, `MG14`,
  `MG09`). TES4 stores aggression 0-100; TES5 defines it as a 0-3 enum
  (xEdit `wbAggressionEnum`) and rejects an out-of-range write outright. The
  bucketing mirrors the record-side thresholds in
  `tes5_import/record_types/actors.py`.
* **`If (X.GetCrimeGoldNonViolent() > 0) as Int == 1`** (`MGExpulsion02Script`,
  `DarkExiledScript`). The parenthesisation looks wrong but Papyrus is
  left-associative, so it reads `((expr) as Int) == 1` — the round-2 chained
  Bool/Int rule. Compiles clean under the CK compiler.
* **`if MenuMode == 0` → `If 0 == 0`** (`Dark18MotherScript`). Permanently true,
  and that is correct: Skyrim's `OnUpdate` never fires while a menu is open, so
  the guard's condition always holds where the body now runs.
* **`GetSecondsPassed` → the fixed poll interval** (`Dark18MotherScript`). Every
  timer increments by `0.1` and the loop re-registers at `0.1`, so
  `TraitorTimer > 48` really is ~48 seconds.
* **Dropping the 4-argument `Say`'s speaker override** (`Dark02WateryScript`'s
  `CapDoorRef.Say Dark02PirateDoor 1 DarkPirate6`). Round-3 rule — but see
  round 8, which re-derived *why*: the "emitting ref is already the intended
  speaker" reasoning given here is wrong (`CapDoorRef` is a door, `DarkPirate6`
  an NPC_). The drop is still correct, for the reasons recorded in round 8.
* **`GetQuestRunning X == 1` folding to `X.IsRunning()`** (`MG16Script`), and
  `GetIsCurrentPackage X == 1` to a bare equality (`Dark03AccidentsScript`).
* **`MG16Script`'s comment promising to "add Amulet to her inventory" while the
  code never does.** The omission is in the TES4 original; reproducing it is
  fidelity.
* **`FGD02Script`-style faithful reproduction of source oddities** — `MG09Script`
  has a stray backtick and an `if` block nested inside a branch it does not
  belong to, and `MGMageConversationFollowScript` has an unbalanced extra
  `endif`. Both are in the originals and both convert without changing meaning.
* **`GetSitState` keeping the raw 0/2/3/4 encoding** (`Dark03AccidentsScript`'s
  `>= 1`). Round-3 verified.

## Verified-correct behaviours from round 5 (do NOT "fix" these)

* **`GetRandomPercent` → `Utility.RandomInt(0, 99)`** (`FGPostQuest`). TES4
  returns 0-99 inclusive, which is exactly this range.
* **`FGRewardMod = ((FGRewards * 0.8)) as Int`** (`FGConversationScript`). The
  TES4 variable is a `short`, so the truncation *is* the original behaviour.
* **`Message "…" 7` dropping the trailing `7`** (`TG11HeistScript`). That is
  TES4's optional display time, which has no Papyrus equivalent — the R3-3 rule.
* **`SetQuestObject` as a no-op** (`TG11HeistScript`). Skyrim has no equivalent;
  quest-object status is a alias flag, not a runtime call.
* **`unlock` → `Lock(false)`** (`MG02Script`).
* **`GetInFaction X == 1` folding to a bare `IsInFaction(X)`** (TG09/TG10). The
  redundant `== 1` is absorbed, same rule as `GetIsCurrentPackage` in round 4.
* **A base form typed `ObjectReference` and passed to `GetItemCount`**
  (`MG02Script`'s `MG02RingofBurden`/`MG02BlackSoulGem`, a CLOT and an SLGM).
  The typing is loose but `ObjectReference` extends `Form`, so it compiles, and
  both bind to the right FormID — verified with `tools/script/vmad_probe.py`.

---

## Verified-correct behaviours from round 4 (do NOT "fix" these)

* **`FGD02Script`'s speaker-4/target-5 branch emitting `VantusPreliusREF.Say`.**
  It looks like a copy-paste slip that should say `WitseidutseiREF` — but the
  TES4 original has exactly the same bug on its own line 158. Reproducing it is
  fidelity, not a defect.
* **A `TES4_<Script>`-typed property used as a `Say()` receiver**
  (`FGD02Script`'s `WitseidutseiREF`, typed `TES4_PublicanFiveClawsWitseidutsei`).
  That script `extends Actor`, so `.Say()` resolves. Same round-2 rule.
* **A script-local named `fQuestDelayTime`** (`FGD02Script`, `FGExpulsionScript`).
  It shadows the TES4 built-in; the converter keeps it as an inert property and
  ticks at its own fixed interval, with `GetSecondsPassed` substituted to match.
  Round-1 verified-correct.
* **`IsPlayerExpelled() == 0`** as the negation. Same chained Bool/Int comparison
  verified in round 2 — Papyrus casts the Int literal to Bool, so `== 0` reads
  "not expelled".
* **`GetIsCurrentPackage X == 1` folding to a bare `(GetCurrentPackage() == X)`.**
  The redundant `== 1` is correctly absorbed rather than chained.

---

## Verified-correct behaviours from round 3 (do NOT "fix" these)

* **`setfactionreaction X Y 0` emitting `SetEnemy(Y, true, true)`.** It reads
  like the opposite of the intent ("make Mythic Dawn neutral so they won't attack
  anymore"), but both bools set is precisely how the Group Combat Reaction enum
  encodes **Neutral**. See the round-2 census behind `_faction_reaction_call`.
* **`GetSitting` → `GetSitState()` keeping the raw number.** Both games use the
  same 0/2/3/4 encoding (3 = Sitting), so `== 3` and `== 0` carry over directly.
* **`GetOpenState` keeping the raw number.** Same 0-4 encoding in both games
  (3 = Closed), so `BaurusScript`'s `< 3` is correct. Distinct from
  `SetDoorDefaultOpen` above, whose bug was dropping its argument entirely.
* **`SetOpen(0)` / `SetOpen(1)` passing an Int to a Bool parameter.** Papyrus
  coerces, and the 73 `SetOpenState` sites carry the right value.
* **`CreateFullActorCopy` → `PlaceAtMe(x.GetActorBase())`** (`MartinScript`'s
  player statue). Not an exact clone — Papyrus has no equivalent — but it places
  a real actor of the right base at the right spot.
* **`setav speed 0` → `SetActorValue("SpeedMult", 0)`.** The scales differ, but
  the call's purpose is immobilising the statue and SpeedMult 0 does that.
* **Dropping the 4-argument `Say`'s speaker override** (`MQ15Script`'s
  `MQ15ResurrectPad1.say MQ15MankarRant 1 MankarCamoran 1`). Correct to drop,
  but **not** for the reason originally recorded here ("the emitting ref is
  already the intended speaker") — `MQ15ResurrectPad1` is a marker, not Mankar.
  Round 8 censused all 282 sites and established the real justification; read
  that entry before touching this mapping.
* **MQ01's MenuMode tutorial blocks preserved as comments.** Every message inside
  them is commented out in the TES4 original too, so nothing executable is lost.
* **`set button to getbuttonpressed` → `button = -1`.** *(Superseded
  2026-08-03: button MessageBoxes now become authored MESG records and the
  poll reads a consume-once shadow of `Show()`'s return — see "Button
  MessageBoxes become authored MESG records" in papyrus_conversion_notes.md.
  The original rationale here — "no message box is ever shown" — was wrong
  for the door scripts whose box gated progression: CGSewerExitScript's
  dead `button == 3` branch was the only path to MQ01 stage 88, so the
  sewer exit could never be taken.)*

## Verified-correct behaviours from round 2 (do NOT "fix" these)

* **Chained `a == b == 0` / `== 1`** (`GetInWorldSpace`, `GetInSameCell`,
  single-cell `GetInCell`, `GetIsReference`). These look like a bug — a form
  comparison chained into another comparison — but they are correct. Compiled
  with the CK compiler and read back from the emitted assembly: Papyrus is
  left-associative and casts the Int literal to Bool, giving
  `COMPAREEQ t1, a, b` → `CAST t2, 0` → `COMPAREEQ t2, t1, t2`. So `== 0` means
  "not equal" and `== 1` means "equal", exactly reproducing TES4's 0/1 idiom.
  34 such sites.
* **A `TES4_<Script>`-typed property passed to `GetItemCount`** (`SQ06Script`'s
  `SQ06BearFang`). The type is derived from the item's own attached script
  (`MISC 0018AE3F` carries `SCRI=0018AE41` → `SQ06FangsScript`), so it names the
  right form, and Papyrus script types extend the base form type.
* **`SetWeather` / `ForceWeather` as a no-op.** Weather conversion is broken and
  currently skipped (`CONVERT_CLIMATE = False`), so the CLMT chain that is the
  only route to a weather is never written. Forcing an unreachable weather into
  Skyrim's sky system divides-by-zero and hard-crashes. Keep it a no-op until
  weather conversion itself is fixed. (The old code comment claiming "WTHR is in
  skipTypes" was wrong and has been corrected.)
* **`ModDisposition` with a value above -100** correctly emits `;NE`
  (`MS38Script`'s `+20`); only the full -100 hostility idiom maps to combat.
* **`EssentialDeathReload` as a no-op** (`MQ13Script`) — the "quest failed,
  reload" prompt has no Skyrim equivalent; the accompanying `scriptKill` still
  fires.
* **`StartConversation` → `Say(topic)`** is a deliberate, documented decision
  with recorded history (discarding the topic silenced every scripted NPC-NPC
  conversation). Not a defect.

## Verified-correct behaviours from round 1 (do NOT "fix" these)

* **`GameDaysPassed` → `.GetValue() as Int`.** Skyrim declares it float
  (`FNAM=102`, `Ord('f')` per xEdit's GLOB definition), which makes the cast
  look lossy — but **Oblivion declares it Short** (`GLOB 00000039`,
  `FNAM.Type=s`), so the source scripts only ever saw whole days and the
  truncation is what *reproduces* their behaviour. This matters beyond the
  day-of-week idiom: 72 lines across 28 scripts compare it against script
  floats, several by exact equality (`MS39Script`:
  `GameDaysPassed == (CurrentDay + 1)`), which only ever matched in Oblivion
  because both sides were whole numbers. The old code comment claimed the
  day-of-week idiom was its "only bare use" — that was wrong and is now fixed.
* **`fQuestDelayTime` as an inert property.** Oblivion's script-polling interval
  has no Skyrim equivalent; writes to it are correctly kept as harmless state.
* **`X.WearingArmor`** (`ArenaScript`) is a remote *script variable* read on the
  target's attached script, not an unconverted function.
* **`DisablePlayerControls` with no local re-enable** (`Dark17FollowingScript`).
  The matching `EnablePlayerControls` lives in the follow-up dialogue INFO, and
  converts fine there (58 scripts emit it). Not a soft-lock.
* **`Key` → `myKey`** and similar renames are the Papyrus reserved-word guard.

---
