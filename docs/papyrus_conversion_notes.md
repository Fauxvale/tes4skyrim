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
- TES4 attributes (Strength, etc.) have no Papyrus equivalent — reads are
  stubbed open and writes dropped, never aliased onto a look-alike actor value
  (see "Skyrim has NO attributes" below)
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

- **`ResetFallDamageTimer` (2026-07-31)** was a `;NE:` no-op with no "on" half
  at all, so a levitation/flight effect converted to a spell that dropped the
  player to their death. Skyrim keeps the console command (opcode 4404) but
  binds no Papyrus equivalent, and `fJumpFallHeightMin` has readers but no
  vanilla writer. It now calls `TES4Polyfill.SuppressFallDamage()`, and the
  converter **injects the paired `RestoreFallDamage()` into the teardown
  event** — synthesizing an `OnEffectFinish` when the script has none, so the
  suppression can never outlive the effect. The injection is a post-pass run
  after the synthesized `OnInit`/`OnUpdate` are appended, because TES4 does not
  order its blocks and the teardown event must already be in the output for the
  restore to land inside it. `SetGhost`/`SetInvulnerable` were rejected as the
  mechanism: both suppress ALL damage, so the scroll would grant temporary
  immortality — a worse defect than the one being fixed.

## Skyrim has GMST readers but no GMST writer (2026-07-31)

`Game.GetGameSettingFloat/Int/String` are vanilla natives. **`Game.SetGameSetting*`
is SKSE-only and does NOT compile against the vanilla headers this pipeline
builds with** — verified directly against `papyrus.exe`: a script calling the
setter fails with "undefined function `SetGameSettingFloat`" on the same line
the getter resolves fine.

So OBSE's `SetNumericGameSetting` cannot convert literally. The settings that
have a per-actor equivalent go through `Actor.ForceActorValue` instead
(`_GMST_TO_ACTOR_VALUE` in `script_convert/converter.py`) — same observable
change, scoped to the actor rather than the world, which is what these scripts
actually want. Two rules fall out:

- **The READ must use the same channel as the WRITE.** These scripts all use the
  save/restore idiom ("remember the old value, set a new one, put it back"); if
  the getter still goes to the global GMST it reads back a number the write
  never changed, and the restore writes garbage. `GetGameSetting` is redirected
  to `GetActorValue` for exactly the settings in the table.
- **`fJumpHeightMax` does not exist in Skyrim** — only `fJumpHeightMin`.
  Confirmed against both Skyrim.esm's GMST records and the SkyrimSE.exe settings
  strings. A TES4 script that sets both is writing one real setting and one
  Oblivion had that Skyrim dropped; the second write is a harmless no-op.

Settings with no actor-value equivalent keep a `;TODO` marker — a call that
compiles and silently does nothing is the dangerous outcome, not the honest one.

## Silent mis-conversion — the unmarked loss

**A `;NE:`/`;TODO:` marker is the HEALTHY failure. The dangerous conversions are
the ones that emit a plausible call which compiles, runs, and does nothing.**
Audited output carries only 2 `;TODO:` markers across 18,566 scripts, so marker
counts measure honesty, not correctness — never treat a clean output scan as
evidence the conversion is complete.

### 🔴 An `as Actor` cast on a non-actor INVERTS every guard around it (2026-08-09)

**`(Self as Actor)` on an ObjectReference is `None` at runtime — and Papyrus
does not stop there. It aborts the call and substitutes `0` for the result**,
so a distance test built on it silently flips to always-true.

`MS48OblivionGateScript` rides on an ACTI (the Oblivion gate). TES4:

```
if getdistance player < 8000
    if MQ00.nearOblivionGate == 0 && getdistance player < 1000
        forceweather OblivionStormTamriel 1
```

emitted as `If (Self as Actor).GetDistance(Player) < 1000`. The gate is not an
actor, so the cast is None, the call aborts, the comparison reads `0 < 1000`,
and the gate hammered `OblivionStormTamriel.ForceActive()` **every 0.1s** while
the player crossed into the Plane of Oblivion. Papyrus.0.log shows the
signature 34x in the two seconds before the CTD:

```
error: Cannot call getDistance() on a None object, aborting function call
warning: Assigning None to a non-object variable named "::temp5"
```

Cause: `_ACTOR_ONLY_FUNCTIONS` and `_OBJREF_SHARED_FUNCTIONS` deliberately
overlap — 14 entries are declared on BOTH Actor and ObjectReference. The
ref'd-receiver site in `converter.py` subtracts the shared set; the
**implicit-Self site did not**, so any bare call to an overlapping function on
a non-actor script got the cast. Blast radius before the fix: **383 calls in
101 scripts** (137 GetDistance, 115 GetItemCount, 115 AddItem, 11 RemoveItem,
4 RemoveAllItems, 1 SetAlpha).

Rule: **every site that consults `_ACTOR_ONLY_FUNCTIONS` must subtract
`_OBJREF_SHARED_FUNCTIONS`.** Note `saa`/`setactoralpha` are deliberately NOT
in the shared set — their documented `(Self as Actor).SetAlpha()` degradation
is intentional and must survive. Guarded by
`tests/test_script_converter.py::TestObjRefSharedFunctionsNeverCastToActor`,
which asserts the invariant across the whole overlap, not just GetDistance.

This is the archetypal silent mis-conversion: it compiles, it is plausible, and
it does the opposite of the original.

### The two stages build DIFFERENT CrossRefGraphs (2026-07-31)

A conversion decision that depends on the graph can come out **differently in
the script stage than in the import stage**, because they do not share a graph:

| Stage | Writes | Builds its graph via |
|---|---|---|
| `--scripts-only` | the `.psc` / `.pex` | `CrossRefGraph.load_from_export()` (the parallel scan) |
| `--import-only` | the **VMAD property bindings** | hand-rolled loop over `all_records` in `import_main` |

`_resolve_props` re-runs the *whole converter* over the source to learn which
properties to bind. If the import's hand-built graph is missing a field the
scan collects, the converter takes a different branch there — and you get a
`.psc` that reads properties the VMAD never declares. They are `None` at
runtime: **as dead as whatever they replaced, while looking fixed in the
source.**

Found via R9-1 (`GetCurrentAIPackage`): the new `pack_type`/`actor_packages`
indexes existed only in the scan, so `TES4_MG17Script.psc` referenced six
`Package` properties and its VMAD declared 25 properties, none of them packages.

**Rule: anything added to `_scan_record_lines` must be mirrored into
`import_main`'s hand-built graph.** Verify with `tools/vmad_probe.py <esm>
<script> --props` — compare the bound set against the `.psc` declarations, and
never assume a correct-looking `.psc` means the binding happened.

### `extends` must be a base type EVERY attaching record can bind (2026-07-31)

The quietest failure in the pipeline so far. Papyrus binds a script to a form
only when the declared base type matches; a mismatch is rejected outright and
**nothing in the script runs** — no events, no poll, no properties:

```
error: Unable to bind script TES4_GoblinHeadScript to (1A08564B)
       because their base types do not match
```

It is invisible to every static check. `Actor extends ObjectReference`, so an
`extends Actor` script on a WEAP/ACTI/CONT/DOOR still **compiles cleanly** —
all 15,959 compiles passed while 67 scripts were dead in-game. The only place
it shows up is the Papyrus log.

Two independent sources of a wrong base type, both fixed in round 7 of the
quest-script audit ([quest_script_conversion_audit.md](quest_script_conversion_audit.md), R7-1):

* **`_infer_extends` overriding a correct answer.** `get_extends_class` derives
  the type from the attaching record's signature and is right; the bare-call
  pre-scan then upgraded 88 non-actor scripts to `Actor`. Its function set
  shared 14 entries with `_OBJREF_SHARED_FUNCTIONS` (`GetDistance` alone hit
  101 scripts), it matched inside comments and string literals, it matched
  locals *named* like functions, and it matched actor-only calls inside
  `OnEquipped`-style events whose subject is the passed-in actor, not the item.
* **A script shared between an actor and a non-actor record.**
  `NoActivationScript` sits on both a DOOR and an NPC_. The scan returned on
  the first actor attachment, so every DOOR copy was unbound — and its empty
  `OnActivate`, which exists purely to *consume* the activation, never ran.
  The base type must now be one all attachments can bind.

Rules that follow:

* **Never widen `extends` for convenience.** It is not a cosmetic type change;
  it decides whether the script exists at runtime.
* **`_ACTOR_ONLY_FUNCTIONS` is not a sound test for "is this an actor".** It
  lists methods `ObjectReference` also declares — that is why
  `_OBJREF_SHARED_FUNCTIONS` exists (see also R3-5's `PlaceAtMe` trap). Any
  new consumer of it must subtract that set.
* **Read the Papyrus log for bind errors after a script-side change.**
  `grep -c "Unable to bind script"` is a one-line health check that no amount
  of compiling or record inspection replaces.

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

### The chime latch is REAL seconds vs a GAME-hour window (2026-07-30)

The `as Int` fix above was necessary but **not sufficient** — the bell still rang
on repeat in Nehrim. The guard was fixed; the *latch* was not.

The idiom is a one-shot latch. The GameHour window sets `soundplaying = 1`, and a
countdown holds the latch until it passes a negative sentinel:

```
if ( soundplaying == 1 )
    set timer to ( timer - GetSecondsPassed )
    if ( timer <= -5 )              ; REAL seconds
        set soundplaying to 0
```

The window is measured in **game hours**; the sentinel in **real seconds**. The
two only stay in step at the TimeScale the author used:

| TimeScale | 1 game hour | 0.04gh window | 5s latch | rings/hour |
|---|---|---|---|---|
| 30 (Oblivion) | 120s | **4.8s** | outlasts it | 1 ✓ |
| 10 (**Nehrim**) | 360s | **14.4s** | expires 2× *inside* the window | 3 ✗ |
| 5 | 720s | 28.8s | expires 5× inside | 6 ✗ |

Nehrim ships `TimeScale = 10` (GLOB `0x3A`; Oblivion ships 30), so the latch
clears while `GameHour` is *still* inside the window and immediately re-fires.
**This is not a Papyrus artifact** — Oblivion's own interpreter rings 3× per hour
at TimeScale 10. The scripts were only ever correct at the author's TimeScale.

- `_scaled_debounce_seconds()` widens the sentinel to `window * 1.25` whenever
  the authored value no longer outlasts the window, and **returns it untouched
  when it already does** — so TimeScale-30 output is byte-identical (verified:
  `TES4_BellTowerScript.psc` diffs clean, 0 Oblivion scripts widened).
- Gated on `_uses_hour_window` (the `GameHour >= X.98` idiom) so ordinary timers
  keep their authored durations; `_LATCH_EXPIRY_RE` only matches a `<= -N`
  sentinel, never `<= 0` or a `>=` test.
- `CrossRefGraph.global_values` now carries GLOB `FLTV` values, so the converter
  can read the plugin's *own* TimeScale rather than assuming 30.
- Measured with `temp/bell_sim.py`: Nehrim 3.0 -> 1.0 rings/game-hour, Oblivion
  unchanged at 1.0.
- **General rule: a TES4 constant in real seconds that gates on game time is
  only valid at the author's TimeScale.** Scale it, don't copy it.
- **This was NOT the cause of the reported "bells on infinite repeat."** It is a
  real defect and the fix stands, but the chapel bell had a separate cause — see
  the next section. Do not re-litigate the latch when a bell repeats.

### `Begin OnTrigger` is PER-FRAME, not on-entry (2026-07-30)

The chapel bell that "tolls ~12 times, breaks briefly, then tolls again forever"
is **not** `SoundZoneKapelleGlockenScript` at all, and not a sound-record loop
(`SNDX.Flags=0`, emitted `LNAM=00000000` — verified in the built ESM).

Two facts have to land together:

1. **`fx\nehrim\kapelleglocke.wav` is 15.46 s long and contains a full peal of
   ~12 tolls.** "12 tolls" is ONE `Play()` call, not twelve. Measure the asset
   before treating a count of anything as a loop count.
2. The bell is rung by nine `Magieverbot*` (magic-ban) scripts, not the chapel
   script — `AAKapelleGlocken` is referenced by **10** Nehrim scripts. It is an
   *alarm* bell for casting inside Erothin's no-magic zone.

Those scripts are `Begin OnTrigger Player` blocks, and **TES4 runs an
`OnTrigger` block every frame the object is inside the volume.** The block's own
code proves it: it counts `frame >= 25` and `frame >= 100` *executions* as a
cooldown. Converting it to Papyrus `OnTriggerEnter` — which fires once, on entry
— froze the state machine on `counter == 1`, so the 100-frame cooldown never
ran and the alarm re-fired on every re-evaluation.

- Skyrim keeps the same three-way split, and all three are distinct engine
  events (`OnTrigger`, `OnTriggerEnter`, `OnTriggerLeave` each appear once,
  NUL-terminated, in `SkyrimSE.exe`). `ObjectReference.psc` documents
  `OnTrigger` as "a trigger is tripped" versus "volume is entered/left".
- `TES4_BLOCK_MAP` now sends `ontrigger`, `ontriggeractor` and `ontriggermob`
  to `Event OnTrigger`. The latter two differ only in *what* trips them, not in
  edge-vs-repeat; Skyrim has no actor/creature split, so that filter stays in
  the body.
- Scope: **504 blocks** (Nehrim 317, Oblivion 187); 79 of them keep a
  per-execution counter and were therefore hard-frozen. Verified no script
  declares two blocks that would now collide into a duplicate `Event OnTrigger`
  (0 in both plugins), and both plugins compile clean.
- **General rule: check whether a TES4 block is edge-triggered or per-frame
  before picking the Papyrus event.** A body that counts its own executions is
  proof of per-frame.
- This is a real defect and the fix stands, but it was **not** the cause of the
  looping chapel bell either. See below.

#### …but the ENTRY frame still has to fire — emit BOTH (2026-08-05, in-game confirmed)

Keeping the body on `OnTrigger` is right, but it is not sufficient: **Skyrim does
not deliver `OnTrigger` for a fast crossing**, which is exactly what walking over
a tripwire or a pressure plate is. Stepping on the Vilverin plate did nothing at
all — the body never ran once.

The vanilla census is unanimous and settles it: `Tripwire.pex`,
`PressurePlate.pex`, `TrapTriggerBase.pex` and `TrapTriggerHinge.pex` **all**
define `OnTriggerEnter`, and vanilla's own `Tripwire` does **not** define
`OnTrigger` at all. (Read them with `skyrim_assets.get_asset_bytes('scripts/
<Name>.pex')` and grep the string table for `On*` — the .psc sources are not
shipped.)

So a converted `begin OnTrigger` block now emits **both** events: the body stays
in `Event OnTrigger` (repeat semantics preserved, Magieverbot counters still
work) and a generated `Event OnTriggerEnter` delegates to it for the crossing
frame:

```papyrus
Event OnTriggerEnter(ObjectReference akActionRef)
  OnTrigger(akActionRef)
EndEvent
```

- Scope: **187 Oblivion scripts**. Skipped when the script authors its own
  `OnTriggerEnter` block (Papyrus allows one definition per event; 0 Oblivion
  scripts do, but a third-party plugin may).
- **Do not "simplify" this back to a single event.** Remapping to
  `OnTriggerEnter` alone re-freezes the per-frame counters above; leaving it on
  `OnTrigger` alone means trap triggers never fire. Both are required.

### Physical-trap damage: TES4's ENGINE read the script's variables (2026-08-09, in-game confirmed)

A converted swinging mace, swinging log, falling log or cave-in fired, swung
and connected — and dealt **zero damage**. Nothing in the TES4 script explains
it, because **the damage is not in the script**: Oblivion's engine dealt it.

When a Havok body on layer 14 (`OL_TRAP`) struck an actor, TES4 read three
magic variables off the striking object's script and applied
`fTrapDamage + fLevelledDamage × victimLevel` damage plus `fTrapPushBack`:

| TES4 script variable | Meaning |
|---|---|
| `fTrapDamage` | flat damage |
| `fLevelledDamage` | per-victim-level damage coefficient |
| `fTrapPushBack` | knockback impulse |
| `fTrapMinVelocity` | contact speed floor (NOT converted — see below) |
| `bTrapContinuous` | re-hit while in contact (NOT converted) |

The names are a **convention the engine keys on**, not ordinary locals — the
script body never assigns damage anywhere. `CTrapSwingMace01SCRIPT` sets
`fTrapDamage 20 / fLevelledDamage 1.5` on activation, which is exactly UESP's
documented "20 + 1.5 × level" for the swinging mace; the swinging log's 15 and
the falling logs' 30 match their scripts the same way. Census:
`fTrapDamage` appears in **226 Oblivion and 127 Nehrim** scripts.

**Skyrim keeps the detection but moves the damage into the script.** The
layer-14 contact still fires — it arrives as the `OnTrapHitStart` script event
— and vanilla answers it in `TrapHitBase.psc` with the native
`ObjectReference.ProcessTrapHit`. So the conversion mirrors vanilla's contract:
every converted `ObjectReference`/`Actor` script that DECLARES `fTrapDamage`
gets a synthesized handler.

```papyrus
Event OnTrapHitStart(ObjectReference akTarget, float afXVel, float afYVel, \
    float afZVel, float afXPos, float afYPos, float afZPos, int aeMaterial, \
    bool abInitialHit, int aeMotionType)
  Actor victim = akTarget as Actor
  If victim == None
    Return
  EndIf
  Float totalDamage = fTrapDamage + fLevelledDamage * victim.GetLevel()
  If totalDamage <= 0.0
    Return   ; not armed yet - TES4 variables start at 0
  EndIf
  akTarget.ProcessTrapHit(Self, totalDamage, fTrapPushBack, afXVel, afYVel, \
      afZVel, afXPos, afYPos, afZPos, aeMaterial, 0.0)
EndEvent
```

- **Read the variables LIVE, never bake the numbers in.** Doing so reproduces
  the whole authored lifecycle for free: the mace script leaves `fTrapDamage`
  at 0 while the trap is armed and held (so brushing it is harmless), sets 20
  on release, and drops it to 5 six seconds later. The `<= 0.0` guard is what
  makes the held phase safe, and it is why an un-triggered trap does nothing.
- Only `fLevelledDamage`/`fTrapPushBack` that the script actually declares are
  referenced; a script with `fTrapDamage` alone emits the flat term only.
- Scope: **64 Oblivion + 33 Nehrim** scripts (maces, swinging/falling logs,
  cave-ins, spike pits, blades, gas emitters).
- `fTrapMinVelocity` and `bTrapContinuous` are deliberately **not** converted.
  The event's velocity units are unverified, and gating on a wrong threshold
  silences all damage — the exact failure being fixed. `OnTrapHitStart` fires
  per contact-start rather than per frame, which already approximates the
  non-continuous case.
- The collision side needed no change: `_remap_world_filter` passes authored
  layer 14 straight through, and vanilla agrees — `trapmace01`'s striking mace
  head is layer 14 while its chain links are layer 10.
- **General rule: when a TES4 feature has no code behind it, suspect an engine
  convention keyed on variable names.** Grepping the script for "damage" finds
  nothing; the census of variable NAMES across all scripts is what exposes it.
- Pinned by `tests/test_script_converter.py::TestPhysicalTrapDamage` — including
  the two cases that would not compile if the emission were naive: a script
  declaring `fTrapDamage` alone must not reference the variables it lacks, and a
  Quest script must get no handler at all (`OnTrapHitStart` is an
  `ObjectReference` event).

### Engine globals must bind UNSHIFTED (2026-07-30) — the actual bell bug

**Root cause of the endlessly-looping chapel bell**, found in `Papyrus.0.log`
after two wrong theories (the TimeScale latch and `OnTrigger`, both above):

```
error: Property Gamehour on script TES4_SoundZoneKapelleGlockenScript
attached to (1A20DD0F) cannot be bound because <nullptr form> (1A000038)
is not the right type
error: Cannot call GetValue() on a None object, aborting function call
	[ (1A20DD0F)].TES4_SoundZoneKapelleGlockenScript.OnUpdate()
```

`convert_GLOB` deliberately drops the engine-owned globals (`GameHour`,
`TimeScale`, …) because Skyrim already ships them — and at the **same FormIDs
Oblivion uses** (`GameYear 0x35` … `TimeScale 0x3A`, verified in both GLOB
dumps). But the VMAD property binders still ran those FormIDs through the
load-order remap, producing `1A000038` — a form that does not exist. The
property bound to **None**, so `GameHour.GetValue()` returned **0.0 forever**,
which is permanently inside every `GameHour <= 0.02` hour-boundary window. The
bell re-fired on a continuous loop.

The `constants.py` comment claimed these references "are canonicalized to the
vanilla forms by script_convert (`_GLOBAL_CANONICAL`)". That was **false** —
`_GLOBAL_CANONICAL` only canonicalizes the *name*; nothing ever fixed the
FormID, and `_ENGINE_GLOBALS` had exactly one use (dropping the record). A
documented mechanism that does not exist in the source is worse than none.

- `constants.ENGINE_GLOBAL_FORMIDS` maps the six engine globals to their vanilla
  FormIDs. All three VMAD binders now bind them unshifted, exactly like Player
  (`0x14`): `object_scripts._resolve_props`,
  `dialog_converter._build_info_script_properties`, and
  `dialog_converter._collect_scro_properties` (the SCRO path shifted them too).
- Scope: **338 bindings** repaired (Nehrim 113, Oblivion 225); 0 remain shifted
  in either plugin.
- **Why "~12 chimes" is not a bug**: `fx\nehrim\kapelleglocke.wav` is a 15.46 s
  recording of exactly 12 evenly-spaced strikes (1.24 s apart, measured). Nehrim
  has only one bell asset and the script never counts hours — it plays the same
  12-strike file at every hour. That is vanilla Nehrim behaviour, not a
  conversion artifact. Making it strike the hour would be a redesign.
- **General rule: any FormID shared with the engine must skip the load-order
  remap.** Player was special-cased; the globals were not. When a property reads
  None in-game, check the Papyrus log for the binding error *first* — it names
  the exact FormID and costs one grep, versus days of modelling script logic.

### Synthesized records were unbound on object scripts (2026-07-31)

Same failure mode as the engine-globals bug above — a property binding to
**None** — from the opposite cause, and it survived that fix because it lives on
a different code path.

The converter mints properties for records that exist only in the OUTPUT:
`TES4Fame`, `TES4Infamy`, `TES4GoldFenced`, `TES4CyrodiilCrimeFaction`, and the
`TES4Unlock_*` topic gates. `object_scripts._resolve_props` binds properties
through `resolve_property_formid()` → `xref.edid_to_formid`, which is built
**from the TES4 export** and therefore can never contain a synthesized record.
Every one silently resolved to nothing.

Only the **object-script** binder was affected: `dialog_converter` already
injects the same registry as `well_known_props`, so `QF_*`/`TIF_*` fragments
bound correctly — which is why a verification counting the 4,762 *dialogue*
bindings reported all-clear while every object script was broken.

- `import_main.get_well_known_properties()` exposes the registry (an accessor,
  not a direct import, because `import_main` imports `object_scripts`).
  `_resolve_props` consults it before falling through to the EditorID lookup.
- Worst case found: `TGStolenGoodsScript`, the **Thieves Guild rank driver** —
  all ten of its gates read `TES4GoldFenced.GetValue()`, so a None property
  threw on the first tick and no TG rank ever advanced.
- **General rule: a record the importer synthesizes needs an explicit binding
  route in EVERY VMAD binder.** The export-derived EditorID map cannot see it.
  Check with `python tools/vmad_probe.py <esm> <script> --props` — a property the
  `.psc` declares but the probe does not list is unbound.

### An early `return` killed the OnUpdate poll (2026-07-31)

TES4 `return` ends only **this frame's** `GameMode` pass; the script runs again
next frame. The converted `OnUpdate` is one-shot and self-rescheduling, so a
`Return` that falls past the trailing `RegisterForSingleUpdate` stops the script
**for the rest of the game**.

`if GetStage X < N / return` is a standard Oblivion early-out, so this was
widespread: **115 Returns across 96 scripts**, including quest drivers
(`MG01`/`MG02`/`MG05`/`MG06`/`MG08`/`MG12`/`MG17`/`MG18`, `MQ16Script`,
`MS04`/`MS09`/`MS14`). `MG05RockScript` fires one shock bolt per tick and uses
`return` to serialize six — it fired exactly one bolt, ever.

The poll is armed at three places, each for a different reason (2026-08-16):

* **top of `OnUpdate`: `RegisterForSingleUpdate(5.0)` — abort insurance
  ONLY.** A runtime error mid-body aborts the event; without this the poll
  died for the rest of the game. 🛑 It must NOT be the real interval:
  `RegisterForSingleUpdate` counts from *now*, so a top arm at `interval`
  starts the next pass `interval` after this one **started**, and a pass whose
  body takes longer than that (MQ01Script's tutorial poll does ~15 latent
  natives per 0.1s tick) overlaps itself; each overlap slows the VM, and the
  pile grows without bound. Measured in game (Papyrus stack dump at the start
  of CharacterGen): **251 concurrent `TES4_MQ01Script.OnUpdate` stacks**, End
  fragments of 1–2s lines running 19–24s late, conversations with 10s+ gaps
  and repeated lines. That was the "excruciating" prison scene.
* **every TES4 `return` in the body**: `RegisterForSingleUpdate(<interval>)`
  spliced before `Return` (`_poll_return_prefix`), `Is3DLoaded()`-gated for
  object/actor scripts. A value-returning `Return <x>` (OBSE user function) is
  not touched; the `!IsRunning()` guard keeps the 5s insurance (a quest that
  is not running need not poll faster); the dialogue gate re-arms at 0.5s.
* **bottom of the body: `RegisterForSingleUpdate(<interval>)`** — the cadence,
  measured from the END of the pass, so passes never overlap.

### `begin MenuMode` — the BARE form is not the menu-ID form (2026-07-31)

The two spellings look alike and behave completely differently, and conflating
them costs real quest logic in one direction or a stage blowout in the other.

* **`begin MenuMode <id>`** fires only while that one menu is open (1014 =
  lockpicking, 1030 = class menu, 1002 = inventory). Skyrim has no per-menu
  hook, so these **must not run**: MQ01's id'd blocks `setstage MQ01 70`/`84`
  unconditionally, and merging them into the poll blew the tutorial's whole
  stage machine on the first tick, then hit stage 100's `stopquest MQ01`.
* **`begin MenuMode`** (bare) fires on *every* menu frame. Censused over
  Oblivion.esm, **not one of the 20 bare blocks is a menu-specific trigger** —
  they are all time-and-inventory bookkeeping that runs on the frames where
  GameMode does *not*, i.e. wait/sleep and the inventory screen. Several say so
  in their own comments (`ErthorScript`: *"contingency if player is
  waiting/resting"*; `SE02OrcCaptainScript` guards on `isTimePassing`).

Dropping the bare bodies deleted the **only** writer of two quest flags:
`MelisandeScript`'s `set MS40.cureready to 1` (so MS40's vampirism cure could
never be handed over) and `Dark09RetirementScript`'s `set GotFinger to 1`. Also
lost: the 7 innkeeper rent timers, 195 lines of SE37 item checks, and
`GandredhelScript`'s topic reveal.

The faithful conversion is to **merge a bare, non-sleep MenuMode body into the
GameMode poll** at its source position — in Oblivion the pair together covered
every frame, so one always-running pass reproduces the union rather than half of
it. `_has_gamemode` must account for it too, or a script whose only block is a
bare MenuMode (`SE42Script`, `DAOghmaInfiniumScript`) gets no loop at all.

Two exceptions keep their own routes: the `isPCSleeping` idiom becomes
`OnSleepStart`/`OnSleepStop`, and menu-ID blocks stay commented.

Before merging, check the bodies are safe on an ordinary frame. All 20 are
idempotent state machines gated by their own doonce/stage variables; a merged
body that reads a menu (`DAOghmaInfiniumScript`'s `getbuttonpressed`) is safe
because the read is consume-once (see the button-MessageBox section below):
until its own box has been shown and clicked it reads -1, so no branch
matches. Where a body is duplicated in both blocks (the `Publican*` rent
counter), running both in one pass still advances the hour once — the first
copy rewrites `renthour` to `GameHour`, so the second's
`(renthour + 1) < GameHour` is false.

### Button MessageBoxes become authored MESG records (2026-08-03)

TES4 builds every in-world choice menu as `MessageBox "text" "Btn1" "Btn2"`
plus a `GetButtonPressed` poll in GameMode. Skyrim has no dynamic boxes, so
for years both halves were stubbed — the box lost its buttons
(`Debug.MessageBox`) and the poll read a constant `-1` — which left ~289
menus dead across the plugins, including two that **soft-locked chargen**:
Oblivion's `CGSewerExitScript` ("Finished - Exit Sewers" is the dead
`button == 3` branch that sets MQ01 stage 88) and Morroblivion's
`mwCGCensusExitDootScript` (same shape, `fbmwChargen` stage 100).

The real conversion (`script_convert/message_menus.py` is the shared plan
both sides run):

* the **importer** writes one MESG per call site — EDID
  `TES4Msg_<Script>_<NN>`, DESC = text, one ITXT per trailing quoted string,
  DNAM bit 0 — and registers the EDIDs in `_WELL_KNOWN_PROPERTIES` so VMAD
  property binding resolves them;
* the **converter** emits `TES4_MsgButton = TES4_ShowMsg(TES4Msg_X_NN)` at
  the call site and `TES4_TakeMsgButton()` for `GetButtonPressed`.
  `TES4_ShowMsg` clears the state before `Show()` (TES4: displaying a box
  resets GetButtonPressed to -1), `Show()` parks its thread on the box, and
  the take helper returns the clicked index once, then -1 — TES4's own
  contract, which is what keeps `if button == N` polls from re-firing on a
  stale index.

Sites are matched by (text, buttons) content, not position — MenuMode merges
can reorder blocks. A `GetButtonPressed` in a script that never shows a
button box of its own (cross-script polling of TES4's global button state —
a handful of sites) still reads `-1`, explicitly dead rather than miswired.
Format specifiers inside a button-box's text (`"...%.0f Drakes?" cost "Yes"
"No"`) survive literally: MESG DESC is static text.

### A modal menu in a POLLED body must block the whole pass (2026-08-15)

`ShowBirthsignMenu` / `ShowClassMenu` convert to a `Message.Show()` chain
(`message_menus.build_chargen_menus`). TES4's chargen menus were modal to the
**entire GameMode pass**: the statement written after `ShowBirthsignMenu` did
not run until the player had chosen. Papyrus only parks *the thread that
called* `Show()`, so the poll's next tick — 0.1s later, on another thread —
re-enters the same body **while the menu is still open**.

A re-entrancy latch alone is not enough. The first form latched the menu but
let the latched-out pass **fall through to the authored tail**, which for
CharacterGen ran `setstage 44` mid-menu. Stage 44's fragment force-greets the
Emperor (`UrielSeptimRef.evp`) at a player still locked in the menu, so the
greet is evaluated and consumed with nobody able to receive it: the menu
closes onto an Emperor with nothing pending, and chargen soft-locks with the
player free-roaming mid-scene.

Verified live through the game bridge rather than by reading: driving
`setstage charactergen 43` advanced the stage to 44 **instantly** while
`TES4ChargenBirthsignChoice` was still 0 — the menu had not yet returned a
choice. That single readback is what separated this from the (superficially
identical) "menu shows twice" symptom the latch was originally added for.

So the emission is **context-dependent**:

* **Polled body** (`_current_event == 'Event OnUpdate()'`) — a latched-out
  pass `Return`s. The tick defers entirely; the pass that owns the menu runs
  the authored tail itself once `Show()` returns. Safe because the poll
  re-arms at the TOP of `OnUpdate`, so returning cannot kill the loop.
* **One-shot site** (quest-stage fragment, `OnActivate`) — keeps the
  fall-through `If !busy` form. Nothing repeating re-enters it, so the latch
  can only trip on a genuine race, and there a `Return` would **drop** the
  authored tail rather than defer it. CharacterGen stage 87 is exactly that
  shape: `MQ02.SetStage(20)`, the end-of-chargen topic unlocks and the
  autosave all follow its class menu.

The general rule: when a converted call blocks, ask whether its caller
repeats. A `Return` is only correct where something will call again.

### A "no equivalent → 0" fallback can shadow a working handler (2026-07-31)

`_convert_expression` keeps a list of argument-less commands that have no Skyrim
equivalent and returns the literal `'0'` for them. Two entries on that list
**also had real handlers** in `_emit_function` — and because those commands take
no arguments they are *always* read bare, so the fallback always won and the
handler was unreachable dead code.

* **`IsPCAMurderer`** → `If 0 == 1`. `DarkBrotherhoodScript`'s site is the sole
  trigger for the entire Dark Brotherhood questline, so Lucien Lachance never
  appeared and `Dark01Knife` never started.
* **`GetDetectionLevel`** → 56 dead threshold tests, including all 7 of
  `Dark04ExecutionScript`'s guard-aggro triggers and the Dark Sanctuary
  assassins' reaction to the player.

The lesson generalises: **before adding a command to a "no equivalent" list,
grep for an existing handler**, and before trusting one that is already there,
check whether the command's arity lets the handler be reached at all. A flat `0`
is invisible — it compiles, it never warns, and the call site quietly becomes a
constant.

When flattening *is* right, it must still be justified by how call sites read
the value. `GetDetectionLevel` was defensible only if scripts read the level
numerically; censused over the plugin, **not one of the 56 sites does** — every
one is `>= 2`, `>= 3` or `== 3`, i.e. "is the target detected", which
`IsDetectedBy` answers exactly.

### A compound `player.X` entry can shadow a handler too (2026-08-02)

Same family as above, different mechanism. `_emit_function` short-cuts any
`ref.func` whose **compound** key (`player.moveto`) exists in `FUNCTION_MAP`,
returning before the dedicated handler further down. `_COMPOUND_HAS_OWN_HANDLER`
exempts commands that need the handler; only `placeatme` was listed.

`moveto` needed the exemption for three reasons:

1. The compound path routes args through `_convert_args`, which **splits on
   commas only** — Oblivion writes the offsets space-separated
   (`MoveTo marker 0 100 0`), so they glued onto the target name.
2. It never registers the destination as a property. The call then emitted a
   bare identifier that nothing declared, and the compiler rejected the **whole
   script**.
3. Only the `Player.`-prefixed form took that path, so a plain `ref.MoveTo`
   looked correct — which is exactly what hid the bug.

MoveTo's destination is a placed reference, so the handler now types it
`ObjectReference` (and skips `player`, a converter keyword that is never a
property, and any already-converted expression).

**Morroblivion symptom:** `CATChargenAndTransport` failed on
`Player.MoveTo CGPlayerStartMarker1`. Note the mod's own typo — no such marker
exists; the SCRO table binds only `CGPlayerStartMarker`, because Oblivion's
compiler treated the trailing `1` as MoveTo's optional offset argument. Oblivion
silently no-ops an unresolved target; Papyrus will not compile an undefined
name, so **one dead line in the mod took down the whole start-menu script**, and
with it the Imperial City transport.

### A script that fails to COMPILE takes its dependents down with it (2026-08-07)

**Symptom:** Morroblivion's Fighters Guild handed out no quests after joining.
The Papyrus log named a *linking* failure, not a compile one:

```
Error: Unable to link type of variable "::fbmwFGAdvancement_var" on object
  "TES4_QF_fbmwFGKillBosses"
error: Unable to bind script TES4_QF_fbmwFGKillBosses to fbmwFGKillBosses (...)
  because their base types do not match
```

**The chain, and why the log points at the wrong file.** `TES4_QF_...KillBosses`
declares a property typed `TES4_fbmwFGAdvancementQuestScript`. That script
declares one typed `TES4_mwGetFactionWitnessesFunc` — which **failed to compile,
so no `.pex` was ever written**. A missing type cannot be linked, so the quest
script fails to load, so the QF fragment cannot bind, so **no stage fragment ever
runs**. Three files away from the error message.

The lesson generalises: **check `output/<plugin>/scripts/compile_errors.log`
before theorising about a dead quest.** One uncompilable script silently
disables every script that names its type, and the runtime error surfaces on the
dependent, never on the culprit.

`mwGetFactionWitnessesFunc` is called by **all nine** Morroblivion guild
advancement scripts, so a single unconvertible OBSE loop disabled every guild.

**What was actually wrong with it**, each fixed generically:

| Defect | Fix |
|---|---|
| OBSE `Label`/`Goto` ref-walk — not Papyrus keywords at all | `GetFirstRef 69`/`GetNextRef` → `Game.FindRandomActorFromRef` sampling; `Label` opens a `While`, `Goto` is a no-op (the header re-tests) |
| `GetIsGhost` / `GetUnconscious` unmapped (only the SETTERS were) | → `IsGhost()` / `IsUnconscious()` |
| `NextActor.IsCreature` — the dotted path resolves a name only if it is a `FUNCTION_MAP` key | added the `iscreature` alias beside `getiscreature` |
| `SetFunctionValue` with **no following `return`** | staged value inside a branch now returns where it stands — it was being dropped, making the function a constant `false` |
| `IsInFaction(Form)` — Papyrus wants a `Faction` | table-driven downcast at UDF call sites (`_UDF_ARG_DOWNCASTS`) |
| `_balance_if_endif` matched only a bare `function `, never `Int Function ...` | typed UDF bodies are now balanced too |

**Two parser bugs found alongside, both silent corruption rather than errors:**

- The arithmetic split ignored string literals, so `FileExists "Data\Morrowind_ob
  - Meshes.bsa"` split on the hyphen *inside the path* and leaked fragments out
  as code (`If 0 - Meshes.bsa(") == 0`).
- `_convert_args` split on whitespace regardless of quotes, so
  `IsModLoaded "Voice Overs V002.esp"` became three arguments and collapsed to a
  bare `If True` — firing every "deprecated plugin detected" warning
  unconditionally.

**Polarity matters when neutralising an install probe.** `FileExists` and
`GetModIndex` have no Papyrus answer, but every TES4 caller uses them to detect a
BROKEN install (`if FileExists ... == 0 → "ERROR: ... is missing"`). The paths
named are Oblivion-side BSAs and inis that do not exist after conversion *by
design*. Answering `0` fired every missing-file branch at once and greeted the
player with a bogus error box, so both answer the not-an-error side.

### `GetIsClass` / `GetPCIsClass` read the ActorBase (2026-08-02)

Both were **absent from `FUNCTION_MAP` entirely**, so the call survived
untranslated and Papyrus parsed `GetPCIsClass CharactergenClass` as a bare name
after a name — a syntax error that failed the whole script. Skyrim reads the
class off the ActorBase (`ActorBase.GetClass()`); `Actor` has no `GetClass()`.
The CLAS argument types as `Class`.

Site: Morroblivion's `fbmwChargenQuestScript` (the class quiz), which the
Chargen-and-Transport start menu imports — so the failure propagated to the
transport NPCs.

### A Bool cannot carry a multi-valued TES4 threshold (2026-07-31)

Mapping `GetDetectionLevel` onto `IsDetectedBy` is only half the fix, and the
missing half fails *silently*. Papyrus rejects a bare `Bool >= 2` outright
(*"cannot relatively compare variables of type bool"*), so the generic
`_BOOL_CMP_RE` pass wraps it as `(... as Int) >= 2` — and **`true as Int` is 1**.
That compiles cleanly and is permanently false, so a naive mapping trades one
dead form for another while looking like a fix.

TES4 detection levels run 0 (unnoticed) to 3 (fully detected), so the emission
scales the Bool to the source's own top value:

```papyrus
((target.IsDetectedBy(observer) as Int) * 3)
```

0 or 3 satisfies every threshold the plugin uses (`== 3`, `>= 2`, `>= 3`)
exactly when detected and never otherwise. **Whenever a TES4 function with a
range wider than 0/1 is mapped onto a Papyrus Bool, rescale to the source's
range** — do not let the generic `as Int` cast decide, because it collapses the
range to 0/1 and quietly kills every threshold above 1.

### Skyrim has NO attributes — the AV tables share nothing (2026-08-06)

**The two games' actor-value tables do not overlap at a single index.** TES4 0 is
Strength, TES5 0 is Aggression; TES4 5 is Endurance, TES5 5 is Assistance
(xEdit `wbActorValueEnum` in `wbDefinitionsTES4.pas` vs `wbDefinitionsTES5.pas`).
A CTDA `ptActorValue` param is a **raw index** into that table, so passing it
through unchanged reads a completely unrelated value.

Worse, Skyrim has no attributes at all. Strength, Intelligence, Willpower,
Agility, Speed, Endurance, Personality and Luck simply do not exist as actor
values, and no TES5 value is a faithful stand-in — every candidate (`SpeedMult`,
`HealRate`, `UnarmedDamage`, …) sits on a different scale, so a 0-100 attribute
threshold compared against one is arbitrary.

**This made every Morroblivion guild unjoinable.** Joining the Fighters Guild is
gated on `GetActorValue Strength >= 30 AND GetActorValue Endurance >= 30`
(INFO `013204F7`); converted verbatim that became `Aggression >= 30 AND
Assistance >= 30` — 0-3 enums that can never reach 30 at any level — so the
recruiter always fell through to *"The Fighters Guild can't just sign up anyone.
You don't meet our requirements."* The Thieves Guild (Agility/Personality →
Morality/One-Handed) failed identically, and the same defect hit ~600 conditions
across the exports. The script side had its own version: the polyfill aliased
`Strength → UnarmedDamage` (≈0, never passes) and `Agility → SpeedMult` (≈100,
always passes), so `fbmwFGAdvancementQuestScript`'s per-rank promotion gates were
equally dead.

The rule now, on **both** sides:

| TES4 AV | Conversion |
|---|---|
| The 8 attributes | **DROPPED** (CTDA) / stubbed to `100.0` (script read), writes discarded |
| Skills | Translated to the TES5 skill index / name |
| Shared derived + AI + magic values | Translated to the matching TES5 index |
| Everything else (Magicka Multiplier, Attack Bonus, Silence, Telekinesis, …) | Dropped — no TES5 equivalent |

Dropping an attribute gate **fails OPEN**, which is the faithful outcome: the
gate exists to keep an under-developed character out, and a Skyrim character has
no way to raise an attribute, so enforcing it would lock the content away
*permanently* rather than merely early.

Three places must agree, and a change to one needs the same change in the others:
`tes5_import/dialog_conditions.py` (`_TES4_AV_ATTRIBUTES` / `_TES4_AV_TO_TES5`,
applied in `convert_ctda` for functions 14 `GetActorValue` and 277
`GetBaseActorValue`), `script_convert/constants.py` (`TES4_ATTRIBUTES`,
`ATTRIBUTE_STUB_VALUE`, `ACTOR_VALUE_MAP`), and
`script_convert/static_scripts/TES4Polyfill.psc` (`IsTES4Attribute`).

Two AV names the map used to emit — `LuckModifier` and `MuteModifier` — are **not
names the engine knows** (verified against `SkyrimSE.exe`'s AV name table, which
runs `…Blindness, WeaponSpeedMult…` with no silence entry), so every read
returned 0 and every write was rejected. Skyrim's internal names for two skills
are also *not* the UI names: use `Speechcraft` (not Speech) and `Marksman` (not
Archery) in Papyrus strings; the CTDA side uses the numeric indices 17 and 8.

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

#### Aggression must not collapse 6..105 onto tier 2 (fixed 2026-07-31)

**TES4 aggression is only half of a PER-TARGET rule**; TES5's is a GLOBAL tier.
UESP `Oblivion:Aggression`: an actor attacks a target when
`disposition(actor→target) < aggression - 5`, so ≤5 never attacks and ≥106
attacks anyone regardless of disposition. TES5 instead names *which reaction
class* the actor attacks (UESP `Skyrim:NPCs#Aggression`): 0 nobody, 1 Enemies,
**2 Enemies AND NEUTRALS**, 3 everyone.

The old rule was `0 if raw <= 5 else (3 if raw >= 106 else 2)` — everything from
6 to 105 became tier 2. **The player is a Neutral to most factions**, so any
scripted "wake up and join this fight" turned the actor hostile to the player.

CharacterGen stage 22 is the case that exposed it: `GlenroyRef.setav aggression
10` exists so the Emperor's guards respond to the Mythic Dawn ambush. In Oblivion
10 only beats a disposition below 5, and the guards' disposition toward the
player is ≈47, so they never turn on you. Converted to tier 2 they attacked the
player from stage 22 onward — the exact failure UESP describes: *"a guard would
attack the whole town if their aggression were sufficiently raised."*

The threshold is `_ONSIGHT_AGGRESSION = 65`, matching the record path's margin
test (a default actor with disposition ≈ Personality 50 needs `(aggr-5) - 50 >=
10`, i.e. aggression ≥ 65 before it earns tier 2):

| TES4 `setav aggression` | tier | meaning |
|---|---|---|
| ≤ 5 | 0 | never initiates |
| 6 – 64 | **1** | attacks declared Enemies only — the faction graph picks the opponent |
| 65 – 105 | 2 | attacks Neutrals on sight too |
| ≥ 106 | 3 | Frenzied |

Census of the 227 scripted calls in Oblivion.esm: 38 → tier 0, **78 → tier 1**
(values 10/20/25/30/40/50, previously all tier 2), 111 → tier 2 (70/80/90/100,
the genuine "now attack anyone" beats). Keep this table in step with
`_npc_aidt` in `tes5_import/record_types/actors.py`, which applies the same rule
to base records but subtracts disposition explicitly.

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
  already-loaded actor never ticks. They start from an `OnInit` gated on
  `TES4Polyfill.ShouldRunGameMode(Self)`.
- **That gate is cell attachment, NOT `Is3DLoaded()`** (2026-08-01). A disabled
  reference has no 3D, so a 3D-gated poll can never start on one — and the poll
  body is routinely the only thing that ever calls `Enable()` on that same
  reference. See "The self-enable deadlock" below.

### Say() timers — `TES4Polyfill.SayLine` (2026-08-16)

**The single design fact.** TES4's `Say`/`SayTo` were **synchronous**: the
engine picked the INFO, started the audio and **returned its length before the
next script line ran**, so every scripted conversation is written as

```
if CharacterGen.speaker == 4 && CharacterGen.convTimer <= 0
    set CharacterGen.convTimer to SayTo player, CharGenMain 1   ; := line length
endif
```

and every other participant waits on that one countdown. Papyrus `Say()` is
fire-and-forget and returns nothing. Every previous conversion tried to
*estimate* the missing number (a topic-max charge at the call site, a "park"
sentinel released by the End fragment, an OnBegin re-charge, per-owner
property bindings, a decay-proof beat companion, a race-safe decrement, an
`If T <= 0` override guard, three End-fragment ordering constraints, quest-
scoped release …) and each estimate had an edge where a line was cut, repeated,
dropped or held. **The rewrite stops estimating: the length comes from the
engine.**

#### The mechanism

* **Every INFO carries a Begin+End fragment pair** (`TES4_TIF__<fid>`, VMAD
  flags 0x03; `build_vmad_info_fragment` and `_info_batch` are both
  unconditional so the two sides can never disagree). Their fixed job:
  `Fragment_1` (OnBegin) → `TES4Polyfill.LineBegan(akSpeakerRef, <measured
  length of THIS line>)`; `Fragment_0` (OnEnd) → the TES4 result script, then
  `TES4Polyfill.LineEnded(akSpeakerRef)` **last**. The hooks carry only the
  speaker — no owner analysis, no property binding, nothing to miss.
* State lives in four script Actor Values **on the speaker** (`Variable07`
  claim, `Variable08` claim deadline, `Variable09` playing line's length,
  `Variable10` speaking deadline; deadlines in game time, see the polyfill).
* A converted `set T to [ref.]Say[To] … topic [+ n]` becomes
  ```
  T = <topic max + 1>                    ; closes this poll's own guard for the ~2s a SayLine can take
  T = TES4Polyfill.SayLine(<speaker>, <topic>, <topic max>) [+ n]
  ```
  `SayLine` **blocks until the engine has begun the line** and returns that
  line's real length **+ SAY_TAIL (1.0s)**. The tail must cover the time
  between the measured audio length and the End fragment actually running
  (dispatch + the engine's trailing hold + inter-response gaps). **0.4 was
  tried in game and lines REPEATED** — so the End overhead is larger than
  0.4s and the "no repeat is possible" reasoning was wrong somewhere; the
  `TES4Say` traces (below) exist to measure it. Other levers that shorten the
  gap without touching the tail: a Say-driving script polls at **0.1s** (its
  `T <= 0` guard is what starts the next line; it was 0.5s), and SayLine's
  return lands at Begin, not at the call. Then the script continues at once, exactly
  as after TES4's `set T to Say`. A Say nothing qualifies for returns **0**
  after a 2s start timeout and the caller's own poll retries — Oblivion's
  behaviour too. Before Saying it waits while the speaker is in the player's
  dialogue menu (Oblivion froze GameMode in menus) or still speaking a tracked
  line (Skyrim silently drops a Say on a talking actor; Oblivion cut the line),
  and it keeps **one waiter per speaker** (a second SayLine returns 0.5 and the
  poll comes back). A `short` timer rounds UP (`Math.Ceiling`).
* **Fragments never write timers.** The owning script's countdown is a plain
  `T = T - dt` again; a fixed override right after the Say (`set convTimer to
  12`) replaces the length before any countdown, exactly as in Oblivion; a
  `set Q.convTimer to Q.convTimer + 2` in an End result lands on the live
  countdown's tail as an after-line pause; `convTimer - .4` "cut him off"
  trims it. None of that needs machinery any more.
* **An ACTOR script's poll skips the pass while the player is in a dialogue
  menu with anyone** — `Self.IsInDialogueWithPlayer() ||
  TES4Polyfill.PlayerIsInDialogue()` (Oblivion's GameMode never ran while a
  menu was open). Skyrim has no "is the player in dialogue" query, so
  `LineBegan` stamps the speaker of any line spoken inside the player's menu
  on the player (`Variable05/06` = FormID hi/lo) and `PlayerIsInDialogue`
  asks that actor. Two in-game failures drove it: the Emperor's
  `speaker == 4 && convTimer <= 0` poll fired during his stage-42 greeting
  (the greeting's End result is what sets `speaker = 0`), its SayLine waited
  for the menu and then spoke a stale "come closer" line exactly as stage
  44's force-greet arrived, which was consumed by a talking actor; and
  Baurus's stage-19 torch line fired INTO the player's conversation with the
  Emperor because a reply's result set stage 19 while the menu was open.
  Quest polls are NOT gated (the conversation countdown lives there; freezing
  it in 2026-08-14 shifted every beat).
* **Diagnostics are built in.** Every SayLine / LineBegan / LineEnded writes
  a `TES4Say …` `Debug.Trace` with real-time stamps; `python
  tools/say_trace_stats.py` turns the Papyrus log into Say→Begin latency,
  End overhead vs measured length (what SAY_TAIL must cover), pre-waits,
  drops and the dead-air gap between lines. Read those before touching the
  tail.
* Bare `Say`/`SayTo` (no assignment) stay plain fire-and-forget `Say()`
  (Nehrim's 727 hand-timed speech state machines). The measure-then-deliver
  pair (`set L to ref.Say T` / `ref.Say T`) collapses to the SayLine alone.
* The NPC-to-NPC driver (`tes5_import/npc_conversations.py`) uses the same
  primitive: `Utility.Wait(TES4Polyfill.SayLine(A, T, fallback) + 0.6)`.

#### Why results stay in the END fragment

Oblivion ran an INFO's result script when the line **finished**. The evidence
is the CS wiki's own scripted-conversation recipe (`How do you set up a
scripted conversation between two or more NPCs?`): it has each result write
`set <quest>.convTimer to <duration of sound file +/- a few seconds>` "to
further refine the timing between this dialogue and the next, and allowing for
momentary pauses" — an after-line pause, which is only meaningful if the
result runs at the end (at line start `set T to Say` would overwrite it). MQ04's
`set MQ04.convTimer to MQ04.convTimer + 2 / + 3 / + 10` beats and CharGen's
`convTimer - .4  ; cut him off` are the same idiom. So OnBegin only reports;
the sequence gate (`_sequence_gate`, applied only when the body itself steps
the counter it is conditioned on) still protects against a mid-line re-seed.

#### Measured in game (2026-08-16, CharacterGen 30-50, `TES4Say` traces)

* Say→Begin latency **0.14–0.26s** when the engine takes the line.
* End overhead (End fragment vs measured audio) **0.4–0.72s** for single
  lines — so a 0.4 tail was genuinely too small and 1.0 leaves ~0.3s.
* **The player can skip a menu line** (click through) and **exit the menu**
  mid-line; Skyrim then runs the skipped line's End and the next line's
  Begin in the SAME frame, End sometimes second. An unconditional clear in
  `LineEnded` wiped the flag of the line that had just started; the speaker's
  own poll saw him idle, and its `Say()` **INTERRUPTED the live line — a Say
  on a talking actor is not always dropped, it can cut the line, and the cut
  line's End result is lost** (`CGEmperor09`'s `setstage 43` → birthsign
  menu never opened). Hence the length-matched clear.
* A Goodbye reply keeps playing after the menu closes; `IsInDialogueWithPlayer`
  goes false at once. Hence `PlayerIsInDialogue()` also holds while the last
  dialogue speaker is still speaking, and QUEST polls are gated too (stage
  `45 → 50` fired from the quest poll mid-dialogue and sent Baurus in).
* **The Papyrus VM starves easily** (`Update budget: 1.2ms` per frame in the
  log). With the dialogue gate on every poll (~210 quest polls at 0.1s +
  every actor poll), from the START of CharacterGen the End fragments of 1–2s
  lines ran **11–17s late**, the VM dumped stacks, SayLine's 10s busy margin
  expired first and "Yessir" played twice. Only scripts that speak carry the
  gate now (153 in Oblivion), the busy deadline is length+30s (it only bounds
  a lost End), the pre-charge is capped at 3.5s (it is shared with the other
  participants' guards), and the start timeout is 1.5s nominal (each
  iteration is a VM turn, so it stretches with load). Same code from a stage-30
  save had 0.4–0.7s End overhead — load, not logic, was the difference.
* Right after an ambush, `Say(CharGenMain)` on Glenroy was refused for ~20s
  while his HELLO greeting bark did play (combat/search state — traces now log
  `inCombat`/`weaponDrawn`); the first Say after a busy wait was dropped
  because the End fragment was still returning — hence the 0.25s wait.

#### What was measured (Oblivion.esm)

397 timer-assigned Say sites in 207 scripts over 275 topics (+28 in QUST stage
results, 1 in an INFO result); 409 bare Say sites; Nehrim: 0 timer-assigned,
727 bare. Only 6 INFO results write a Say timer (MQ04's three beats, the
CharGen `- .4`, one `= 1`, one unrelated `timer = 0`).

#### Traps

* `Utility.GetCurrentRealTime()` restarts with the process, so a deadline
  stamped in one session is garbage in the next — deadlines are game-time
  days at the current TimeScale (`_GameDays`).
* A dead SayLine thread (mod update, script removed) would hold the per-speaker
  claim; the claim is renewed every 0.1s and expires 5s after the last
  renewal, so it can never strand a speaker.
* A lost End (actor killed or unloaded mid-line) expires the speaking state
  `length + tail + 2s` after Begin — a stale busy flag costs one line's length,
  never a stall.
* If OnBegin ever fired *before* the audio started by more than SAY_TAIL, the
  guard would reopen while the audio still played and the same speaker's next
  SayLine would wait on the busy flag (bounded) before Saying — no repeat is
  possible because the state has advanced by then, but the pause would show.
  Raise `SAY_TAIL` in `TES4Polyfill.psc` if that is ever observed.

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
- **`sv_Construct` is the ONE OBSE string command with an exact equivalent**: it
  builds a `string_var` from a literal, and a Papyrus `String` *is* that literal,
  so `set q to sv_Construct "text"` → `q = "text"`. It used to fall through to
  the inert `ar_`/`sv_` catch-all below, which left an undefined identifier and
  failed the whole script (2026-08-02). `sv_Destruct` stays a no-op — Papyrus
  strings are garbage-collected, so there is nothing to free.
- No Papyrus equivalent, emitted inert with `;NE:` — OBSE arrays/strings (`ar_*`,
  the rest of `sv_*`, `forEach`), path-based music (`StreamMusic` and Nehrim's bundled `emc*`
  plugin; Skyrim music is MusicType-based), `GetPlayerHasLastRiddenHorse`,
  `HasFlames`/`AddFlames`/`RemoveFlames`, `PositionCell` (Papyrus `MoveTo` takes
  a reference, not cell coordinates), `GetIgnoreFriendlyHits` (Skyrim exposes
  only the setter).

## Scripts on placed references

Reference events (`OnPackageEnd`, `OnActivate`) never fire on a base NPC_ VMAD —
they must be relocated to the placed ACHR. This was the CharacterGen stage-10
stall.

### Bare self-reference calls also force relocation (2026-08-01)

`_relocate_actor_scripts_to_refs` originally moved a script for two reasons: a
`GetVMScriptVariable` package gate, or a `begin <reference-event>` declaration.
There is a **third**: a script that calls a reference function on *itself* with
no `ref.` prefix — `enable`, `disable`, `moveto`, `startcombat`, `playgroup`,
`evp`, … An ActorBase is not a reference, so on the base record these calls have
nothing to act on and do nothing at all, whatever event drives them.

This matters because Oblivion's standard scripted-entrance idiom is an
**initially-disabled placement (record flag 0x800) whose OWN GameMode block
enables it on a cue**. `_script_uses_self_reference_call` now detects the bare
call (skipping comment lines, so a commented-out `;evp` does not trigger a move,
and requiring no `.` prefix so `CelebroRef.Disable` — someone *else's* method,
which works fine from the base — does not either).

### The self-enable deadlock (2026-08-01)

The same idiom hit a second, independent bug in the poll gate. The chain:

1. The ref is initially disabled → **no 3D**.
2. `OnLoad` / `OnCellAttach` need 3D or a cell *transition*; a ref already
   sitting in the player's starting cell gets neither.
3. The `OnInit` fallback was gated on `Is3DLoaded()` → **false while disabled**.
4. So the poll never starts, `Enable()` never runs, the ref never gets 3D.

The script that enables the reference only runs once the reference is enabled —
unbreakable. **200 placed refs in Nehrim** were stranded this way (Kim/MQ04,
Erik/NQ01, the MQ20 paladins, MQ31 batteries, MQ33 mirages, sound zones).

The fix is `TES4Polyfill.ShouldRunGameMode(akRef)`: 3D-loaded **or** parent cell
attached. Oblivion's own rule was cell-scoped, not 3D-scoped — GameMode ran for
every ref in an active cell, disabled ones included, which is precisely what
makes the self-enable idiom work. Cell attachment preserves the anti-storm
property the 3D gate was introduced for (refs in detached cells still never
tick); it only stops treating "invisible" as "not there".

**Nehrim intro symptom:** Celebro, the companion who is supposed to attack a
troll and then talk to the player, never appeared in the start cell
(`StartCelle`, 0x00000B9B). `MQ00CelebroScript` is nothing but
`begin GameMode / if ( GetStage MQ00 == 5 ) / enable / endif` — it declared no
reference event, so it stayed on the base NPC_ (bug 1), and its poll was
3D-gated, so it could not have run anyway (bug 2). Both had to be fixed for him
to spawn.

### A bare GameMode block also forces relocation (2026-08-02)

The two triggers above still missed a whole class: an actor script that is
**nothing but a `GameMode` block making explicit `Other.Method()` calls**. It
declares no reference event and makes no bare self-call, so neither reason
fired and it rode the base NPC_ — where it is dead code, because the converter
compiles `GameMode` into an OnUpdate poll whose only starters are
`OnCellAttach` / `OnCellDetach` / `OnLoad` / `OnInit`, all gated on
`TES4Polyfill.ShouldRunGameMode(Self)`. Every one of those is an
`ObjectReference` member; on a base VMAD `Self` is an `ActorBase`, so the events
never fire, the gate has no reference to answer for, and the poll never starts.

`gamemode` is therefore in `_TES4_REFERENCE_EVENTS` now. It is not an engine
reference event — it is there because *our own* lowering of it is
reference-only.

**Morroblivion symptom:** `CATDestinationSorter`, the script driving the
Cyrodiil↔Vvardenfell world transport, is pure GameMode polling a global
(`CATDestinationCode`) and calling `Player.MoveTo(marker)`. Attached to the
base NPC_ of both ferrymen (Kisimba in the Imperial City, Jo'Tesh in Seyda
Neen), it never ran: the player paid 1000 gold, the dialogue fragment set the
destination code, and nothing ever moved them.

### A script on the PLAYER base needs a quest alias (2026-08-01)

Oblivion let a plugin script the player by attaching a SCPT to the player's base
`NPC_ 0x00000007`. **Skyrim has no equivalent binding**, and the relocation above
cannot help: it walks ACHR/ACRE, and the player has no ACHR — `PlayerRef 0x14` is
engine-created and its record signature is **PLYR**, not ACHR, so a plugin cannot
author an override of it. PlayerRef's base is *Skyrim's own* `0x07`; our shifted
copy (`0x01000007`) is a record nothing ever instantiates, so a VMAD there is
inert.

Vanilla's mechanism for "code that runs on the player forever" is a
start-game-enabled quest holding a reference alias forced to `0x14`, with the
script on that **alias** — `JailQuestPlayerScript`, `TutorialPlayerScript`; 71
Skyrim.esm quests force an alias to `0x14`, and the vanilla `Player` NPC_ carries
no VMAD at all. The converter now mints `TES4PlayerScripts` for this
(`object_scripts.build_player_alias_plan` →
`dialog_converter._make_player_script_quest`), lists it in the `.seq`, and emits
the script as `extends ReferenceAlias`, routing every implicit-self call through
`GetReference()` / `GetActorReference()` (`Self` there is the alias, so
`Self as Actor` is a cast the compiler rejects).

Vanilla Oblivion attaches nothing to the player base, so this only ever surfaced
on Nehrim — where `GlobalplayerScript` holds the **entire** XP / level /
learning-point / gold economy *and* the only `SetStage MQ00 1`, which is what
starts the main quest. Without it the intro never began and no character levelled.

Two consequences worth remembering:

- **The player is never a script-typed property.** `player`/`playerref` is a
  converter keyword emitted as `Game.GetPlayer()`. Because the player base has
  EditorID `Player` *and* can carry a SCRI, both `_add_scro_ref` (which skipped
  only `0x14`, not `0x07`) and `get_record_script_type` typed it as the attached
  script — so 242 Nehrim scripts declared
  `TES4_GlobalplayerScript Property Player` and then failed to convert it to
  `ObjectReference` at every `X.GetDistance(Player)` / `MoveTo(Player)`.
- **A property typed as the attached script is not an Actor.** `_add_scro_ref`
  deliberately prefers the script type so cross-script variable reads work, so
  actor-only calls on such a property must be **cast at the call site**
  (`(KreoRef as Actor).EvaluatePackage()`) rather than retyped — and likewise for
  arguments of the four functions whose Papyrus signature declares an `Actor`
  (`_ACTOR_ARG_FUNCTIONS`: `StartCombat`, `IsHostileToActor`,
  `GetRelationshipRank`, `SetRelationshipRank`).

### Quoted EditorIDs — the `_MQ01Tate_` property (2026-08-01)

Oblivion's parser accepts quotes around any EditorID and Nehrim's authors use
them constantly (**173 sites**: `SetStage "MQ01Tate" 20`, `GetStage "NQ00Karick"`,
`StartQuest "NQ05"`, `AddScriptPackage "..."`). `_safe_property_name` maps
`[^\w]` to `_`, so `"MQ01Tate"` became the property `_MQ01Tate_` while the *same
script's* unquoted `GetStage MQ01Tate` became `MQ01Tate`. Only the unquoted
spelling matches an EditorID, so only it was bound in the VMAD — `_MQ01Tate_`
stayed **None** and every `_MQ01Tate_.SetStage(...)` threw at runtime.

The damage was structural, not cosmetic: MQ01Tate could never advance past stage
15, so it never reached stage 40 — the only thing that runs `SetStage MQ01 1` —
and MQ00's completion stage 65 (behind an INFO owned by MQ01) was unreachable
too. `_safe_property_name` now strips a wrapping quote pair, and
`_convert_line` unquotes the dotted member form (`"NQ16"."NQ16CountBooksVar"`,
which previously emitted un-parseable Papyrus because the assignment target and
its value took different code paths). Genuine string literals are untouched.

### `AdvancePCLevel` → the Level actor value (2026-08-01)

Vanilla `Game.psc` (from `Data/Scripts.zip`) has **no level setter** —
`Game.SetPlayerLevel` exists only in mod-supplied headers, so it will not
compile against the shipped set. `Game.GetPlayer().ModActorValue("Level", 1)` is
the equivalent the base game does offer. Nehrim drives its whole custom level-up
through `AdvancePCLevel` (`GlobaltagebuchScript`'s journal menu), so leaving it
unmapped pinned the player at level 1 forever.

> Check `Data/Scripts.zip`, not `Data/Scripts/Source/`, when asking whether a
> Papyrus native exists: the latter is where mods install their own headers, and
> in this install its `Game.psc` is 454 lines against vanilla's 266.
