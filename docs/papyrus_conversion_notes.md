# Papyrus / Script Conversion Notes

Linked from [CLAUDE.md](../CLAUDE.md). TES4 script → Papyrus conversion
learnings. Implemented in `script_convert/`. For the original scope analysis and
record counts see [Script_Conversion_Plan.md](Script_Conversion_Plan.md).

## Language mapping basics

TES4 uses an imperative scripting language with event blocks (GameMode,
OnActivate, …). TES5 uses Papyrus, an object-oriented language.

- Variables become Properties: `short myVar` → `Int Property myVar Auto`
- Event blocks change: `begin OnActivate` → `Event OnActivate(ObjectReference akActionRef)`
- Functions change: `Message "text"` → `Debug.Notification("text")`
- TES4 `set x to y` → `x = y`
- Player reference: `player.` → `Game.GetPlayer().`
- No direct equivalent for: GetInCell (→IsInLocation), ShowMap, CloseOblivionGate, SetQuestObject
- TES4 attributes (Strength, etc.) have no Papyrus equivalent
- Vanilla forms with no TES4 counterpart are reached via
  `Game.GetFormFromFile(0x..., "Skyrim.esm")` in TES4Polyfill (ActorTypeNPC
  keyword for GetIsCreature, GuardDialogueFaction for IsGuard,
  PlayerVampireQuestScript.VampireStatus for HasVampireFed) — no property
  binding needed.

**Vanilla Papyrus has more than the wikis suggest** — check the game's
`Data/Source/Scripts/*.psc` headers before declaring something unconvertible.
`Faction.SetReaction/ModReaction`, `Actor.GetCurrentPackage()` (→
GetIsCurrentPackage/GetCurrentAIPackage-vs-form),
`ObjectReference.PushActorAway` and
`ObjectReference.GetAnimationVariableBool("bAnimPlaying")` (→ IsAnimPlaying) all
exist and are used by the converter.

## Paired on/off commands — the asymmetric-map trap

**A `;NE:` (no-equivalent) comment on ONE HALF of a paired on/off command is a
latent soft-lock, not a cosmetic gap.** When the "on" half converts to a
state-changing call and the "off" half is a no-op, the actor can never return to
the original state. Audit the partner call before accepting either.

- **`SetAlert` is NATIVE in both games (`Actor.SetAlert(bool)`) — never
  approximate it with `DrawWeapon()`.** Oblivion's SetAlert sets the AI
  combat-READINESS flag; the engine clears it and it does NOT suppress dialogue.
  The old mapping sent `SetAlert 1`→`DrawWeapon()` while `SetAlert 0` was a
  silent no-op, so any actor alerted for a scripted ambush drew a weapon and
  NEVER stood down. CharacterGen alerts Uriel for the prison-cell ambush (stage
  15) and clears it at stages 17/24 to run the conversation — converted Uriel
  stood weapon-drawn, could not force-greet, and the intro SOFT-LOCKED with
  player controls disabled. 97 scripts across the game use SetAlert, most in
  talking scenes (MQ13/MQ14 Bruma, SE06 battle, MS13), not fights.

## Silent mis-conversion — the unmarked loss

**A `;NE:`/`;TODO:` marker is the HEALTHY failure. The dangerous conversions are
the ones that emit a plausible call which compiles, runs, and does nothing.**
Audited output carries only 2 `;TODO:` markers across 18,566 scripts, so marker
counts measure honesty, not correctness — never treat a clean output scan as
evidence the conversion is complete.

### GameHour is FLOAT — never truncate a global read (2026-07-28)

`GameHour` is FormID `0x00000038` in **both** games, and Skyrim declares it
**float** (`GLOB.FNAM=102`), so `GetValue()` returns fractional hours — 23.9847,
not 23. Oblivion mislabels it `short` in its own GLOB record but the engine still
reports the fraction, which is why the bell/chime idiom works there:

```
if ( GameHour >= 23.98 ) || ( GameHour <= 0.02 )   ; the top of the hour
```

Emitting `GameHour.GetValue() as Int` truncated that, and every such window
collapsed into an **always-true whole-hour test** (`23 >= 23.98` is false, but
`0 <= 0.02` is true for all of hour 0). The guarded body then ran every frame:
the Erodans-Kapelle chapel bell and Oblivion's `BellTowerScript` rang
continuously instead of once on the hour. 157 comparisons across 7 scripts in
both plugins.

- `_global_read()` decides the cast from the GLOB's real `FNAM.Type` (now carried
  by `CrossRefGraph.global_types`), plus an explicit `_FRACTIONAL_ENGINE_GLOBALS`
  set for engine globals Oblivion mislabels. `TimeScale` really is short
  (Skyrim `FNAM=115`) — do NOT add it.
- Assignments into Int variables still get their cast from
  `_coerce_float_to_int` (`GetValue` is in `_FLOAT_RETURNING_FUNCS`), so
  `currenthour = GameHour` remains correct.
- **General rule: a blanket cast on a global read is a silent behaviour change.**
  Type the cast from the record, not from the call site.

### Aggression/Confidence are ENUMS in TES5, not 0-100 (2026-07-28)

TES4 stores the AI traits on a 0-100 scale; TES5 defines them as small enums
(xEdit `wbDefinitionsCommon.pas`: `wbAggressionEnum` 0-3, `wbConfidenceEnum` 0-4,
`wbAssistanceEnum` 0-2, Morality 0-3). `SetActorValue("Aggression", 100)` is
**rejected outright** — the engine logs *"attempt made to set illegal value"* and
leaves the trait **unchanged**, so every scripted "now turn hostile" beat
silently did nothing. 160 such writes in Nehrim alone (94 of them `Aggression
100`), 509 enum-AV writes across both plugins.

`_scale_enum_av()` buckets literals onto the same thresholds the record-side
converter uses (`tes5_import/record_types/actors.py`), so a scripted change lands
on the tier the NPC's AIDT was converted to. Values already inside the enum range
pass through untouched; non-literals are left alone. `ModActorValue` is
deliberately NOT scaled — a delta on a 0-100 scale has no enum equivalent, and no
such call exists in the source. Verify with
`python tools/check_enum_actor_values.py <scripts/source>`.

Two recurring shapes, both found in the animation handlers:

- **Wrong target vocabulary.** The emitted call is valid Papyrus but the string
  argument comes from TES4's namespace, which the engine silently drops.
  `PlayIdle`/`PickIdle` passes the raw TES4 IDLE EditorID straight into
  `Debug.SendAnimationEvent(ref, "<edid>")`; Skyrim defines no such event, so the
  idle never plays and nothing is logged (`"fastforward"` survives into output
  this way, next to correctly-mapped events like `moveStart`).
- **Unconditional target-type assumption.** *The correct API depends on WHAT THE
  TARGET IS, not on whether the call names a reference.* `PlayGroup` routed every
  explicit-ref call to `Debug.SendAnimationEvent` (behavior-graph actors only), so
  `CGPrisonSecretWallRef.playgroup forward 1` — an ACTI whose NIF carries a
  `Forward` NiControllerSequence — did nothing and Renault's switch never moved
  the wall, while the SELF-call on the very next line converted correctly. Fix:
  resolve the base record via `CrossRefGraph.get_base_signature()` and treat only
  `NPC_`/`CREA`/`ACHR`/`ACRE` as actors; unknown targets keep the behavior event,
  which is inert on an object but never corrupts an actor's graph. `PlayIdle`
  still uses the old `actor_func=True` assumption and needs the same treatment.

**Census the no-op lists against the real API, not against intuition.** Six
entries in `_NO_OP_FUNCS`/`_BARE_NO_EQUIV_COMMANDS` exist natively in Skyrim:
`AddAchievement` (59 call sites), `PlayBink` (5), `SendTrespassAlarm` (2),
`SetPublic` (1), `AttachAshPile`, and `GetCurrentPackage` (already special-cased
for the PACK-comparison form; the residual sites compare TES4 package-TYPE codes,
which genuinely have no equivalent). `SetCellPublicFlag` (100 sites) sets the same
Cell flag as `SetPublic` and should route there rather than no-op. The
authoritative list is the vanilla Papyrus sources at
`references/skse64-master/scripts/vanilla` — extract every `Function` declaration
and diff it against the no-op sets before assuming a command was dropped for a
good reason.

Losses that ARE correct and should not be re-litigated: `AddTopic` (223 `;NE:`)
is deliberate — `tes5_import/dialog_unlocks.py` re-expresses topic visibility as
`TES4Unlock_*` GLOB gates and scans SCPT sources *because* script_convert leaves
an inert comment. `ModDisposition` (414) is a genuine engine removal, with the
`<= -100` hostility case already converting to `StartCombat`.

## Event / timer conversion

- `begin OnAlarm` → `OnCombatStateChanged` guarded `aeCombatState != 0`;
  `OnStartCombat` bodies are guarded `== 1` (the event also fires on combat END).
- Bare `begin MenuMode` + `isPCSleeping` (Oblivion's sleep-detection idiom) →
  `RegisterForSleep()` + OnSleepStart/OnSleepStop running the body twice with a
  `TES4_PCSleeping` flag (11 quests incl. MG04 inn ambush, Rufio murder,
  vampirism relied on it). Menu-ID MenuMode blocks stay commented out.
- `GetSecondsPassed` substitutes `_get_update_interval()` (must equal the
  RegisterForSingleUpdate arg or timers run off-rate).
- Converted GameMode loops must not only start on cell attach — an
  already-loaded actor never ticks. They start from an `Is3DLoaded`-gated
  `OnInit`.

### Say() timers

**A converted `Say`/`SayTo` timer holds the line's MEASURED length**
(`say_durations`, else `SAY_LINE_SECONDS`). Papyrus `Say()` is fire-and-forget —
it does not block or queue, and silently does nothing when no INFO under the
topic qualifies ([CK wiki, Say - ObjectReference](https://ck.uesp.net/wiki/Say_-_ObjectReference)).
The owning script polls and re-issues `Say()` while its guard reads
`timer <= 0`, but the INFO's End FRAGMENT — which advances the conversation —
only runs when the line FINISHES. The timer's one job is to cover that window.

Both extremes have been tested IN GAME and both fail: **zero** makes the poller
re-Say every tick, restarting the line so its fragment never runs (Valen Dreth
repeats taunt 1 forever); a large **"park" that only the End fragment can
clear** strands whenever a line is dropped, halting the scene (CharacterGen's
prison-cell ~20s gaps and stalls). The line's own length is the smallest value
covering the window, and the fragment clears it the moment the line really ends
— so it adds no silence, and a dropped line drains via the owning script's
countdown instead of stopping the quest.

#### Release the timer LAST, after the body advances the state

**A Say() INFO End fragment must RELEASE the conversation timer LAST — after the
body advances the sequence state.** The release re-opens the owning script's poll
guard (`If <quest>.speaker == 2 && <quest>.convTimer <= 0`, or Valen Dreth's
`ElseIf talk == 1`); the BODY (`convCount + 1`, `speaker = 0`, `SetStage(18)`) is
what that guard reads. Releasing first opens the guard while the OLD state still
stands, so the poller re-fires the SAME line — a runtime trace shows
`RENAULT FIRE cnt=15` → fragment accepted → `RENAULT FIRE cnt=15`.

Two symptoms, one cause:

1. **Renault never presses the secret wall switch at CharacterGen stage 18** —
   the duplicate Say re-armed `convTimer` to 8.33, holding stage 18 open past the
   `EvaluatePackage()` the stage fragment had just issued, so
   `CGRenoteOpenSecretDoor` was never selected. The door is actually opened by
   the SWITCH's script (`CGPrisonSecretWallSwitchSCRIPT`, gated
   `isActionRef RenoteRef`) setting `charactergen.secretDoor = 1`, which the
   quest script's `stage 18 && secretDoor == 1 && convTimer <= 0` needs to reach
   19.
2. **Valen Dreth repeats a taunt** — his `talk` flag never advances (TES4's
   `tauntStage` is declared but never incremented), so only the re-armed timer
   stopped him looping.

Fixed in `script_convert/pipeline.py` `_info_batch` (6,432 fragments); guarded by
`TestSayTimerRelease::test_release_comes_after_the_body_advances_the_sequence`.

**The retime case needs no special handling.** `CharGenMain 0x32B0C` does
`convTimer = convTimer - .4` ("cut him off"). Oblivion ran that while the line was
STILL PLAYING, shortening a live countdown; our fragment runs when the line has
already ENDED, so the base is 0 and `0 - .4` is negative — which every `<= 0`
guard reads as "release now", the same outcome. Do not add machinery to preserve
it.

#### Keep it a countdown

**The `convTimer`-style timer is an ordinary self-clearing COUNTDOWN in TES4**
(`if convTimer > 0 : convTimer = convTimer - getSecondsPassed`; every consumer
just tests `<= 0`). Keep it that way. Never convert it into something whose
default state is "stuck until an event fires".

Comments in this area are unreliable sediment from earlier designs — trust the
code and the in-game results, not the surrounding prose.

## Magic / condition helpers

- `pme`/`sme` (PlayMagicEffectVisuals) take a MGEF code, not a shader: resolve
  code → TES4 MGEF → its `DATA.EffectShader` (else EnchantEffect, else school
  enchant glow) → converted EFSH, and emit `<shader>.Play(ref, dur)`. EFSH
  records are converted, so the property binds.
- `IsSpellTarget X` → `TES4Polyfill.HasMagicEffectByID(ref, <Skyrim MGEF fid>)`
  where the MGEF is the spell's first effect surviving import (same mapping as
  `_pack_effects`); pure script-effect spells are detected via the importer's
  first filler effect, which keeps the dropped effect's duration for exactly
  this reason.

## Reaching 100% compile (2026-07-28, 42 → 0 failures)

Nehrim 2620/2620 and Oblivion 15959/15959 now compile. The failures clustered
into a few generic causes, all fixed in the converter rather than per-script:

- **Comma-form RECEIVER on a zero-arg command.** `StopCombat, Player` /
  `IsInCombat, Player == 1` name the *receiver*, not an argument — the comma
  spelling of `Player.StopCombat`. Treating it as an argument gave `IsInCombat(Player)`
  ("function takes 0 parameters not 1") or dropped the token and acted on the
  WRONG ACTOR. `_ZERO_ARG_REF_FUNCTIONS` (derived from the empty-argument `ref.`
  rows of `docs/skyrim_commands.md`) drives the promotion. **When widening the
  bool-comparison regex, keep the `\b` and the mandatory separator** — without
  them `GetDead` matched the prefix of `GetDeadCount` and split off `Count` as an
  argument across 28 scripts.
- **A local variable may shadow `player`.** `StartCelleAufzugTriggerZone01Script`
  declares `Short Player` as its own trigger flag; substituting the keyword gave
  the un-assignable `Game.GetPlayer() = 1`. Locals win in a VALUE position but
  never as a **receiver** (a Short has no methods) and never inside
  `IsActionRef`, whose operand is always a reference — hence
  `_convert_ref(..., as_receiver=True)`.
- **Property names must key on the CANONICAL EditorID.** TES4 lookup is
  case-insensitive, so `SetEssential Kornderbraumeister` refers to
  `KornderBraumeister`; keying on the local spelling created a second
  `_property_refs` entry differing only in case, and since Papyrus is also
  case-insensitive the two declarations collided and the typed one lost.
  Where the EditorID collides with one of the script's own variables (MQ19Script
  has an `Int narel` beside the NPC_ `Narel`), `_actor_base_property()` mints a
  `<Name>Base` property and `resolve_property_formid` strips the suffix to bind it.
- **A mapped GLOBAL call takes no receiver.** `Player.DisablePlayerControls`
  emitted `Game.GetPlayer().Game.DisablePlayerControls()`. Any `FUNCTION_MAP`
  target starting `Game.`/`Utility.`/`Debug.`/`Math.` drops the TES4 receiver.
- **`as` binds tighter than arithmetic.** A trailing `as Int` only types the whole
  expression when no bare operator precedes it; `A - B.GetValue() as Int` is
  `Float - Int`. Also: a plain Float→Int copy (`ihour = vtime`) needs the cast
  just as much as an expression does, and OBSE `let` needs the same coercion
  `set` already had.
- **No-equivalent handlers must return a BARE literal.** These sit inside larger
  conditions, where a trailing `;` comment swallows the rest of the line
  (`If True  ;(False ;NE: ...)`). Push the note to `_line_comments` instead.
- **Match no-equivalent FAMILIES by pattern, not by name.** Enumerating OBSE
  commands one per build is how `disableKey` and `setMenuFloatValue` each survived
  to fail alone. `con_*`, `get/setMenu*`, and the input family are prefix-matched,
  and the bare-read router honours those prefixes without a `FUNCTION_MAP` entry.

New native equivalents found (always check before declaring one absent):

| TES4 / OBSE | Papyrus | Note |
|---|---|---|
| `GetDeadCount <base>` | `ActorBase.GetDeadCount()` | Exact match. Previously emitted a literal `0`, silently disabling **152 quest gates** (126 of them `== 1` checks that became `0 == 1`). |
| `SetEssential` | `ActorBase.SetEssential(bool)` | On ActorBase, not Actor. |
| `PositionWorld x y z ang ws` | `SetPosition` + `SetAngle` | No worldspace param; dropped. |
| `ForceFlee` / `Flee` | `SetActorValue("Confidence", 0)` + `EvaluatePackage()` | Skyrim drives fleeing off Confidence — the engine's own mechanism. |
| `GetAttacked` | `Actor.IsAlarmed() as Int` | |
| `IsInAir` | `Actor.IsFlying() as Int` | |
| `con_Save` | `Game.RequestSave()` | |
| `DispelSpell` | `Actor.DispelSpell(Spell)` | Actor-only — must NOT sit in `_OBJREF_SHARED_FUNCTIONS`. |
| `$var` (OBSE) | `(var as String)` | `$` is not even a legal Papyrus character. |
| `string_var` / `array_var` | `String` | Missing from `TYPE_MAP`, so the variable got **no declaration at all**. |

Genuinely absent (inert `;NE:`): OBSE UI/menu (`get/setMenu*`), raw input
(`isKeyPressed*`, `disableKey`, `getControl`), console commands (`con_*`),
INI access (`Set/GetNumericINISetting`), `getCrosshairRef`, `getObjectType`
(Skyrim's form-type numbering differs entirely), `GetStringGameSetting` (Papyrus
has only the numeric getters), `SkipAnim`, `getPackageTarget`, and
`UnlockAchievement`. An OBSE `forEach … loop` suppresses its **whole body** — the
body reads an iterator that cannot exist.

A cross-script write to a variable the owner never declares
(`AutoSaveQuest.ReadyForAutosave`, 3 scripts) is **dangling in the original mod**.
Oblivion ignored it; Papyrus fails the whole file, so it is commented out.

## Syntax traps found via Nehrim (2026-07-20, 50.5% → 98.4% compile rate)

- **`;/` opens a Papyrus BLOCK comment** (closed by `/;`). Oblivion scripts use
  `;//////...` banner rules constantly and TES4 had no block-comment syntax, so
  every banner swallowed the rest of the file. The compiler only reports this as
  `unexpected end of file` at the LAST line, and one unterminated banner in a
  widely-extended base script cascaded into ~300 downstream failures.
  `_postprocess_lines` pads a space after the `;`.
- **Oblivion accepted a comma between a command and its first argument**
  (`IsActionRef, Player`, `MessageBox, "text"`, `SetPCExpelled Fac, 1`).
  `_emit_function` strips a leading comma once for all handlers; the expression
  router also matches `^(\w+)(?:\s*,\s*|\s+)(.+)$`. Handlers that
  `split(None, 1)` must still `rstrip(',')` the token.
- **TES4 EditorIDs may start with a digit** (`1Feuerball`, `01SetBonus...`);
  Papyrus identifiers may not. Regexes anchored on `^[a-zA-Z_]` silently skipped
  these, leaving the raw name in the output. Use `^\w+` and exclude pure digits /
  `(?!\d+\.)` so float literals still parse. `_safe_property_name` strips the
  leading digit for the declaration, so call sites must go through the same
  lookup or the two disagree.
- **`"EditorID".Function` (quoted ref)** is valid TES4 and appears in 143 Nehrim
  scripts. Unquote before the ref patterns run, or the call is emitted as a
  property access on a string.
- **Anything unparseable must be emitted COMMENTED**, never as bare code — TES4
  uses `-----` separator rules, which parse as a prefix expression.
- A `FUNCTION_MAP` entry with a `None` Papyrus name normally falls through to the
  EditorID lookup on purpose (bare `getSecondsPassed` etc. are rewritten by later
  passes; routing them early TODO's them mid-expression and leaves
  `timer = timer - `). Bare-read commands that have no such pass belong in
  `_BARE_NO_EQUIV_COMMANDS`.
- `Activate` conversions: bare `Activate` → `(akActionRef/self, true)`. Passing
  `Game.GetPlayer()` produced door/lockpick/teleport storms.

## OBSE constructs (Nehrim depends on these heavily)

- **User-defined functions**: `begin Function{ a, b }` + `Call <ScriptName> arg1,
  arg2` (first arg space-separated, rest comma-separated; param list may use
  EITHER separator). Converted to a Papyrus method named `TES4Call` on the callee
  script, reached through a property typed as that script. NOT `Global` — the
  bodies read the script's own object properties.
  - Params must NOT also be emitted as auto-properties; the parameter would
    shadow the property while callers write neither, so the body reads a
    permanent 0.
  - A TES4 `ref` param is an untyped handle: type it from USAGE (convert the body
    first, then read `_property_refs`), else `Form`. Typing it
    `ObjectReference` — the literal translation — rejected all 170 call sites
    that pass a Spell.
  - `SetFunctionValue X` + `return` → `Return X`, and the function needs a return
    type plus a trailing `Return 0` for fall-through paths.
- `eval <expr>` is a pure pass-through wrapper (Nehrim uses it only around
  `Call`) — drop it. Beware over-broad stripping: an earlier pass ate a variable
  named `Eval`.
- `Let X := Y` and the compound forms `+= -= *= /=` → `X = X op Y` (Papyrus has
  no compound assignment).
- **OBSE `IsCasting` maps NATIVELY** — `GetAnimationVariableBool("bIsCastingRight"
  /"bIsCastingLeft")`, no SKSE needed. Check for a native equivalent before
  declaring a function unconvertible.
- No Papyrus equivalent, emitted inert with `;NE:` — OBSE arrays/strings (`ar_*`,
  `sv_*`, `forEach`), path-based music (`StreamMusic` and Nehrim's bundled `emc*`
  plugin; Skyrim music is MusicType-based), `GetPlayerHasLastRiddenHorse`,
  `HasFlames`/`AddFlames`/`RemoveFlames`, `PositionCell` (Papyrus `MoveTo` takes
  a reference, not cell coordinates), `GetIgnoreFriendlyHits` (Skyrim exposes
  only the setter).

## Scripts on placed references

Reference events (`OnPackageEnd`, `OnActivate`) never fire on a base NPC_ VMAD —
they must be relocated to the placed ACHR. This was the CharacterGen stage-10
stall.
