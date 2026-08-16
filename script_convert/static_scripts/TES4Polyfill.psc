ScriptName TES4Polyfill Hidden
{Utility functions for converted TES4 Oblivion scripts.
All functions are Global — no instance needed.
Provides equivalents for Oblivion functions with no direct Papyrus mapping.}

; ==========================================================================
; Random
; ==========================================================================

Int Function GetRandomPercent() Global
  Return Utility.RandomInt(0, 99)
EndFunction

; ==========================================================================
; Cell / Location
; ==========================================================================

Bool Function IsInCell(ObjectReference akRef, Cell akCell) Global
  Return akRef.GetParentCell() == akCell
EndFunction

Bool Function IsInSameCell(ObjectReference akRef1, ObjectReference akRef2) Global
  Return akRef1.GetParentCell() == akRef2.GetParentCell()
EndFunction

; True while a TES4 `begin GameMode` block on this placed reference would run.
;
; Is3DLoaded() alone is WRONG here: an initially-disabled reference (record flag
; 0x800) has no 3D, so an Is3DLoaded()-gated poll can never start — and the poll
; body is frequently the only thing that ever calls Enable() on that very
; reference.  That deadlock is unbreakable: the script that enables the ref only
; runs once the ref is enabled.  It stranded 200 placed refs in Nehrim, Celebro
; (the intro companion, MQ00CelebroScript `if GetStage MQ00 == 5 / enable`)
; among them, so the intro NPC never appeared at all.
;
; Oblivion's own rule is cell-scoped, not 3D-scoped: GameMode ran for every ref
; in an active cell, disabled ones included — which is exactly how the vanilla
; self-enable idiom works.  So test parent-cell attachment, which is true for a
; disabled ref and false for anything outside the active grid.  That preserves
; the anti-storm property the 3D gate was introduced for (references in detached
; cells still never tick); it only stops treating "invisible" as "not there".
Bool Function ShouldRunGameMode(ObjectReference akRef) Global
  If (akRef == None)
    Return False
  EndIf
  If (akRef.Is3DLoaded())
    Return True
  EndIf
  Cell parentCell = akRef.GetParentCell()
  Return parentCell && parentCell.IsAttached()
EndFunction

; ==========================================================================
; Actor Value Mapping (TES4 AV names → TES5 AV names)
; ==========================================================================

; SKYRIM HAS NO ATTRIBUTES. Strength, Intelligence, Willpower, Agility, Speed,
; Endurance, Personality and Luck do not exist as actor values, and no TES5
; actor value is a faithful stand-in — every candidate sits on a different
; scale, so comparing a 0-100 attribute threshold against one is arbitrary.
;
; These used to be aliased onto the nearest-looking AV (Strength->UnarmedDamage,
; Endurance->HealRate, Agility->SpeedMult, Personality->Speechcraft) and that
; silently broke every Morroblivion guild. The Fighters Guild advancement
; script gates each promotion on `Player.GetAV Strength >= 30 && Player.GetAV
; Endurance >= 30`; UnarmedDamage sits near 0 so the check could never pass at
; any level, while SpeedMult sits near 100 so the Thieves Guild's Agility gate
; passed unconditionally. Neither is the authored behaviour.
;
; IsTES4Attribute lets the readers below no-op instead: a read returns a value
; that satisfies any authored threshold (attribute gates cap at 100 in TES4 —
; the highest in the guild scripts is 35) so the gate falls open, and a write
; is discarded rather than corrupting a live Skyrim value. Falling open is the
; faithful outcome: an Oblivion attribute gate exists to keep an
; under-developed character out, and a Skyrim character has no way to raise an
; attribute at all, so enforcing it would lock the content away permanently
; rather than merely early.
Bool Function IsTES4Attribute(String avName) Global
  Return avName == "Strength" || avName == "Intelligence" || \
         avName == "Willpower" || avName == "Agility" || \
         avName == "Speed" || avName == "Endurance" || \
         avName == "Personality" || avName == "Luck"
EndFunction

; Value returned for a removed attribute. Above every authored TES4 attribute
; threshold (the ceiling is 100) so `>=` gates pass, and positive so the rarer
; `> 0` / `!= 0` forms behave the same way.
Float Function TES4AttributeStub() Global
  Return 100.0
EndFunction

String Function MapActorValue(String avName) Global
  ; Skills (renamed and/or merged in TES5). "Speechcraft" and "Marksman" are
  ; the engine's internal AV names for the skills Skyrim's UI calls Speech and
  ; Archery — both resolve; the UI names do not.
  If avName == "Armorer"
    Return "Smithing"
  ElseIf avName == "Athletics"
    Return "Stamina"
  ElseIf avName == "Blade"
    Return "OneHanded"
  ElseIf avName == "Blunt"
    Return "OneHanded"
  ElseIf avName == "HandToHand"
    Return "UnarmedDamage"
  ElseIf avName == "Mysticism"
    Return "Illusion"
  ElseIf avName == "Mercantile"
    Return "Speechcraft"
  ElseIf avName == "Security"
    Return "Lockpicking"
  ElseIf avName == "Acrobatics"
    Return "Stamina"
  ElseIf avName == "Fatigue"
    Return "Stamina"
  ElseIf avName == "Encumbrance"
    Return "CarryWeight"
  ElseIf avName == "Responsibility"
    Return "Morality"
  Else
    Return avName
  EndIf
EndFunction

Float Function GetTES4ActorValue(Actor akActor, String avName) Global
  If IsTES4Attribute(avName)
    Return TES4AttributeStub()
  EndIf
  Return akActor.GetActorValue(MapActorValue(avName))
EndFunction

Function SetTES4ActorValue(Actor akActor, String avName, Float afValue) Global
  If IsTES4Attribute(avName)
    Return
  EndIf
  akActor.SetActorValue(MapActorValue(avName), afValue)
EndFunction

Function ModTES4ActorValue(Actor akActor, String avName, Float afValue) Global
  If IsTES4Attribute(avName)
    Return
  EndIf
  akActor.ModActorValue(MapActorValue(avName), afValue)
EndFunction

Function ForceTES4ActorValue(Actor akActor, String avName, Float afValue) Global
  If IsTES4Attribute(avName)
    Return
  EndIf
  akActor.ForceActorValue(MapActorValue(avName), afValue)
EndFunction

; ==========================================================================
; Position / Angle Axis Helpers
; TES4: GetPos X → Papyrus: GetPositionX()
; ==========================================================================

Float Function GetPos(ObjectReference akRef, String axis) Global
  If axis == "X" || axis == "x"
    Return akRef.GetPositionX()
  ElseIf axis == "Y" || axis == "y"
    Return akRef.GetPositionY()
  ElseIf axis == "Z" || axis == "z"
    Return akRef.GetPositionZ()
  EndIf
  Return 0.0
EndFunction

Function SetPos(ObjectReference akRef, String axis, Float afValue) Global
  Float x = akRef.GetPositionX()
  Float y = akRef.GetPositionY()
  Float z = akRef.GetPositionZ()
  If axis == "X" || axis == "x"
    x = afValue
  ElseIf axis == "Y" || axis == "y"
    y = afValue
  ElseIf axis == "Z" || axis == "z"
    z = afValue
  EndIf
  akRef.SetPosition(x, y, z)
EndFunction

Float Function GetAngle(ObjectReference akRef, String axis) Global
  If axis == "X" || axis == "x"
    Return akRef.GetAngleX()
  ElseIf axis == "Y" || axis == "y"
    Return akRef.GetAngleY()
  ElseIf axis == "Z" || axis == "z"
    Return akRef.GetAngleZ()
  EndIf
  Return 0.0
EndFunction

Function SetAngle(ObjectReference akRef, String axis, Float afValue) Global
  Float x = akRef.GetAngleX()
  Float y = akRef.GetAngleY()
  Float z = akRef.GetAngleZ()
  If axis == "X" || axis == "x"
    x = afValue
  ElseIf axis == "Y" || axis == "y"
    y = afValue
  ElseIf axis == "Z" || axis == "z"
    z = afValue
  EndIf
  akRef.SetAngle(x, y, z)
EndFunction

; ==========================================================================
; Combat
; ==========================================================================

; TES4 StartCombat FORCES the fight: the actor attacks the target regardless
; of aggression, disposition or faction relations.  CharacterGen's finale
; depends on that -- the final assassin has base aggression 0, his only
; faction (MythicDawnCGAssassin) has no hostile relations, the Emperor's
; faction Friends it at +50 -- yet `CGAssassinFinal.startcombat
; UrielSeptimRef` must still cut the Emperor down.
;
; Skyrim's StartCombat is only a suggestion the combat AI re-evaluates at
; once: an actor with Aggression 0 exits combat immediately (vanilla's own
; turn-hostile fragment, MS08 "In My Time Of Need", pairs
; `MS08SaadiaFaction.SetEnemy(PlayerFaction)` with
; `SetAV("Aggression", 1)` for exactly this reason), and a target the actor
; has no hostile reaction to is dropped as invalid.
;
; So supply the two things the combat AI needs, then force it:
;   - floor Aggression at 1 ("attacks Enemies").  Tier 1 never widens to
;     neutrals, so this cannot make the actor attack bystanders.
;   - make the pair FACTION enemies through the two conversion-owned
;     factions the import writes for exactly this purpose
;     (TES4ForceCombatAttackers is record-side Enemy of
;     TES4ForceCombatVictims, both directions).  This is the vanilla
;     runtime-hostility idiom generalised to an arbitrary victim, and it
;     works for ANY actors.  The earlier attempt used relationship rank -4,
;     which only exists between UNIQUE actors — the CharacterGen final
;     assassin is non-unique, the rank write silently no-opped, and
;     StartCombat dropped the friendly Emperor as an invalid target.
;
; The memberships persist until death or an explicit stopcombat, matching
; TES4 StartCombat (fight until someone dies or a script stands them down).
; Cross-pair contamination (attacker A hostile to victim B forced in a
; different scene) is accepted: forced attackers are overwhelmingly scene
; actors that die in their scene, and TES4's own disposition damage from
; StartCombat leaked comparably.
Function ForceCombat(Actor akAttacker, Actor akTarget, Faction akAttackers, Faction akVictims) Global
  If akAttacker == None || akTarget == None
    Return
  EndIf
  If akAttackers != None && akVictims != None
    akAttacker.AddToFaction(akAttackers)
    akTarget.AddToFaction(akVictims)
  EndIf
  If akAttacker.GetActorValue("Aggression") < 1.0
    akAttacker.SetActorValue("Aggression", 1)
  EndIf
  akAttacker.StartCombat(akTarget)
EndFunction

; TES4 "PlayerFaction" converts to a plugin faction the RUNTIME player was
; never a member of — membership lives on Skyrim's own Player NPC (0x7),
; which the conversion does not touch.  So a scripted relation flip against
; the converted PlayerFaction reaches nobody.  Mirror those flips onto the
; vanilla PlayerFaction (Skyrim.esm 0x000DB1), the faction the real player
; actually belongs to.  aiMode: 1 = enemy, 0 = neutral, 2 = friend.
Function MirrorPlayerFactionRelation(Faction akOther, Int aiMode) Global
  If akOther == None
    Return
  EndIf
  Faction pf = Game.GetFormFromFile(0x000DB1, "Skyrim.esm") as Faction
  If pf == None
    Return
  EndIf
  If aiMode == 1
    akOther.SetEnemy(pf, false, false)
  ElseIf aiMode == 0
    ; Explicitly clearing a relation: SetEnemy with both "neutral" bools
    ; writes Neutral, the same idiom the faction-reaction conversion uses.
    akOther.SetEnemy(pf, true, true)
  Else
    akOther.SetAlly(pf, true, true)
  EndIf
EndFunction

; ==========================================================================
; Crime / Faction
; ==========================================================================

Function SetCrimeGold(Faction akFaction, Int aiGold) Global
  akFaction.SetCrimeGold(aiGold)
EndFunction

Int Function GetCrimeGold(Faction akFaction) Global
  Return akFaction.GetCrimeGold()
EndFunction

Function ModCrimeGold(Faction akFaction, Int aiGold) Global
  akFaction.ModCrimeGold(aiGold, false)
EndFunction

; ==========================================================================
; Sound Wrappers
; ==========================================================================

Function PlaySound3D(ObjectReference akSource, Sound akSound) Global
  akSound.Play(akSource)
EndFunction

; ==========================================================================
; Essential / Protected
; ==========================================================================

Function SetEssential(ActorBase akActorBase, Bool abEssential) Global
  akActorBase.SetEssential(abEssential)
EndFunction

Bool Function IsEssential(Actor akActor) Global
  Return akActor.GetActorBase().IsEssential()
EndFunction

; ==========================================================================
; Message Wrappers
; TES4 Message "text" → single-line notification
; TES4 MessageBox "text" "btn1" "btn2" → needs Message form (emit TODO)
; ==========================================================================

Function ShowNotification(String text) Global
  Debug.Notification(text)
EndFunction

Function ShowMessageBox(String text) Global
  Debug.MessageBox(text)
EndFunction

; ==========================================================================
; Lock Wrappers
; TES4: Lock 50 → Lock(true, 50)
; TES4: Unlock → Lock(false)
; ==========================================================================

Function LockAtLevel(ObjectReference akRef, Int aiLevel) Global
  akRef.Lock(true, aiLevel)
EndFunction

Function Unlock(ObjectReference akRef) Global
  akRef.Lock(false)
EndFunction

; ==========================================================================
; Ownership Wrappers
; ==========================================================================

Function SetOwnership(ObjectReference akRef, ActorBase akOwner) Global
  akRef.SetActorOwner(akOwner)
EndFunction

Function SetFactionOwnership(ObjectReference akRef, Faction akFaction) Global
  akRef.SetFactionOwner(akFaction)
EndFunction

; ==========================================================================
; AI Package Wrappers
; ==========================================================================

Function EvaluatePackage(Actor akActor) Global
  akActor.EvaluatePackage()
EndFunction

; ==========================================================================
; Container
; ==========================================================================

; TES4 `GetContainer` returns the container an item is inside (0 when it is
; lying in the world).  Papyrus has no way to walk from an item reference back
; to its container, but it does not need one to answer the question every
; caller actually asks: an item held in an inventory has no 3D placement, so
; its parent cell is None.  That is the same test, and it is exact.
Bool Function IsInContainer(ObjectReference akRef) Global
  Return akRef.GetParentCell() == None
EndFunction

; ==========================================================================
; Magic / Actor State
; ==========================================================================

; TES4 IsSpellTarget: "is this actor currently affected by spell X".  The
; converter resolves X to the Skyrim MGEF the imported spell actually carries
; and passes its Skyrim.esm FormID here.
Bool Function HasMagicEffectByID(Actor akActor, Int aiFormID) Global
  If akActor == None
    Return False
  EndIf
  MagicEffect fx = Game.GetFormFromFile(aiFormID, "Skyrim.esm") as MagicEffect
  If fx == None
    Return False
  EndIf
  Return akActor.HasMagicEffect(fx)
EndFunction

; TES4 GetIsCreature: Skyrim marks people with the ActorTypeNPC keyword
; (Skyrim.esm 0x00013794) on their race; converted creatures use generated
; races without it.
Bool Function GetIsCreature(Actor akActor) Global
  If akActor == None
    Return False
  EndIf
  Keyword npcKeyword = Game.GetFormFromFile(0x00013794, "Skyrim.esm") as Keyword
  If npcKeyword == None
    Return False
  EndIf
  Return !akActor.HasKeyword(npcKeyword)
EndFunction

; TES4 HasVampireFed: Skyrim's PlayerVampireQuest (Skyrim.esm 0x000EAFD5)
; tracks feeding — VampireStatus is 1 exactly while a vampire has recently fed
; (it climbs to 2..4 as the player goes hungry).
Bool Function HasVampireFed() Global
  Quest vq = Game.GetFormFromFile(0x000EAFD5, "Skyrim.esm") as Quest
  PlayerVampireQuestScript vs = vq as PlayerVampireQuestScript
  If vs == None
    Return False
  EndIf
  Return vs.VampireStatus == 1
EndFunction

; TES4 IsGuard: Skyrim guards are all members of GuardDialogueFaction
; (Skyrim.esm 0x0002BE3B).
Bool Function IsGuard(Actor akActor) Global
  If akActor == None
    Return False
  EndIf
  Faction guardFaction = Game.GetFormFromFile(0x0002BE3B, "Skyrim.esm") as Faction
  If guardFaction == None
    Return False
  EndIf
  Return akActor.IsInFaction(guardFaction)
EndFunction

; TES4 SetActorRefraction: no refraction control in Papyrus; a translucent
; alpha is the closest visual.  0 restores full opacity, anything else fades.
Function SetActorRefraction(Actor akActor, Float afValue) Global
  If akActor == None
    Return
  EndIf
  If afValue > 0.0
    akActor.SetAlpha(0.3, True)
  Else
    akActor.SetAlpha(1.0, True)
  EndIf
EndFunction

; TES4 (OBSE) ResetFallDamageTimer cleared the accumulated fall distance so the
; next landing did no damage.
;
; Skyrim has NO vanilla-Papyrus route to this.  The console command survives
; (opcode 4404) but is not bound to Papyrus; the GMST the fall-damage formula
; reads (fJumpFallHeightMin) has readers but no vanilla writer — SKSE's
; Game.SetGameSettingFloat does not compile against the vanilla headers this
; pipeline builds with, verified against the compiler; and the blunt
; alternatives (SetGhost, SetInvulnerable) suppress ALL damage, which would
; make a levitation scroll grant temporary immortality — a far worse defect
; than the one being fixed.
;
; So this keeps the ONE effect that is both faithful and scoped: heal the
; actor back up by the fall's cost.  DamageResist is applied for the window
; instead of invulnerability, so ordinary combat damage still lands.
;
; Callers are per-frame effect updates that stop when the effect ends, so the
; resistance is (re)applied on each call and RestoreFallDamage removes it —
; the paired on/off contract in docs/papyrus_conversion_notes.md.  The
; modifier is tracked so repeated calls cannot stack it without bound.
Function SuppressFallDamage(Actor akActor = None) Global
  If akActor == None
    akActor = Game.GetPlayer()
  EndIf
  If akActor == None
    Return
  EndIf
  ; ForceActorValue, not Mod: this runs every update tick, and a modifier
  ; would otherwise accumulate for as long as the effect lasts.
  akActor.ForceActorValue("DamageResist", 10000.0)
EndFunction

; Undo SuppressFallDamage.  Emitted by the effect-finish path of any script
; that called it; also safe to call blind.
Function RestoreFallDamage(Actor akActor = None) Global
  If akActor == None
    akActor = Game.GetPlayer()
  EndIf
  If akActor == None
    Return
  EndIf
  akActor.ForceActorValue("DamageResist", 0.0)
EndFunction

; ==========================================================================
; Day/Time Helpers
; ==========================================================================

; Every function here is Global, so none of them may touch a script property —
; a Global has no instance to read one from ("variable GameDaysPassed is
; undefined").  Fetch the vanilla GameDaysPassed global (Skyrim.esm 0x00000039)
; by form ID instead.
Int Function GetDayOfWeek() Global
  GlobalVariable daysPassed = Game.GetFormFromFile(0x00000039, "Skyrim.esm") as GlobalVariable
  If daysPassed == None
    Return 0
  EndIf
  Return ((daysPassed.GetValue() as Int) % 7)
EndFunction

Float Function GetCurrentTime() Global
  Return Utility.GetCurrentGameTime()
EndFunction

; ==========================================================================
; Math
; ==========================================================================

; OBSE's `exp`/`log` have no Papyrus native (Math.psc ships sin/cos/tan/asin/
; acos/atan/sqrt/pow/abs/Floor/Ceiling and nothing else), so they are built on
; Math.pow here.  Morrowind_ob's levitation code is the heavy user: its damping
; term is `set dampNorm to exp dampExp`, evaluated every frame.
Float Function Exp(Float afValue) Global
  Return Math.pow(2.718281828, afValue)
EndFunction

; Natural log via the change-of-base identity ln(x) = log2(x) / log2(e).
; Papyrus has no log of any base either, so log2 is computed by binary
; decomposition: pull out the integer power of two, then refine the fraction.
Float Function Log(Float afValue) Global
  If afValue <= 0.0
    Return 0.0  ; ln is undefined for x <= 0; callers treat 0 as "no contribution"
  EndIf
  Float x = afValue
  Float log2 = 0.0
  While x >= 2.0
    x /= 2.0
    log2 += 1.0
  EndWhile
  While x < 1.0
    x *= 2.0
    log2 -= 1.0
  EndWhile
  ; x is now in [1,2): refine 16 fractional bits of log2(x).
  Float frac = 0.5
  Int i = 0
  While i < 16
    x *= x
    If x >= 2.0
      x /= 2.0
      log2 += frac
    EndIf
    frac /= 2.0
    i += 1
  EndWhile
  Return log2 / 1.442695041  ; 1/ln(2)
EndFunction

; ==========================================================================
; 3D / Model refresh
; ==========================================================================

; OBSE `ref.Update3D` rebuilds a reference's 3D after its model changed —
; Morrowind_ob calls it through the fbmwUpdate3D helper after swapping the
; player's skeleton for the werewolf one.  Papyrus has no direct equivalent
; (QueueNiNodeUpdate is SKSE), but disable/enable tears the 3D down and
; rebuilds it, which is what the call is for.  The reference must be re-enabled
; even if it was already disabled — callers only ever use this on visible
; actors, and leaving one disabled would delete it from the world.
Function Update3D(ObjectReference akRef) Global
  If akRef == None
    Return
  EndIf
  akRef.Disable()
  akRef.Enable()
EndFunction

; ==========================================================================
; Plugin detection
; ==========================================================================

; OBSE `IsModLoaded "Foo.esp"` asks whether a plugin is in the load order.
; Vanilla Papyrus has no direct query, but Game.GetFormFromFile returns None
; for a file that is not loaded, so asking it for the plugin's own header
; record (0x00000000 in that file's local space) answers the same question.
Bool Function IsModLoaded(String asPlugin) Global
  Return Game.GetFormFromFile(0x00000000, asPlugin) != None
EndFunction

; ==========================================================================
; Breakaway props
; ==========================================================================

; Oblivion authors break-apart props (mwallplankbreakaway01's planks,
; IDCrumbleWall01's bricks) as KEYFRAMED bodies that carry real mass and
; `Unyielding = 1`.  The animation only creaks the pieces off their mounting --
; the planks rotate 15.19 degrees and have ZERO translation keys -- and the
; visible break is HAVOK taking over: the pieces detach and fall.
;
; Skyrim keyframed bodies never yield to gravity, so a straight conversion left
; the planks hanging in the half-broken pose forever.  Shipping them dynamic in
; the NIF instead was also wrong -- they dropped the moment the cell loaded,
; before the clip had played.  So the mesh keeps them keyframed (held, following
; the clip, exactly like Unyielding) and the release happens HERE, once the clip
; has run.
;
; The wait covers the clip.  Converted breakaway `Unequip` sequences run 0.033s
; to 3.8s (median 0.033; only 4 of 27 exceed 0.5s), and Papyrus cannot query a
; Gamebryo sequence's length -- PlayAnimationAndWait never returns for a
; BGSGamebryoSequenceGenerator state, and the graph declares no `end` event to
; wait on.  One second covers every clip but the 3.8s outlier while still
; reading as "it gave way, then it fell".
;
; Inert on anything that is not a breakaway piece: every other animated object
; converts to a mass-0 keyframed body, and a mass-0 body has infinite effective
; mass, so going dynamic cannot make it fall.  Doors, gates and portcullises
; driven by the same animation group are unaffected.
Function ReleaseBreakaway(ObjectReference akRef) Global
  If akRef == None
    Return
  EndIf
  Utility.Wait(1.0)
  ; Motion_Dynamic = 1.  abAllowActivate must be true or the body stays asleep
  ; and never starts simulating.
  akRef.SetMotionType(1, true)
EndFunction

; SetDestroyed(1) deferred until the clip that preceded it has finished.
;
; TES4 pairs `playgroup <grp>` with `setDestroyed 1` on the very next line
; (CTrigTripwire01SCRIPT, CTrapLogs01SCRIPT, CTrapCaveIn01SCRIPT,
; MPlanksBreakAway01Script).  In Oblivion that was harmless: with no
; destruction data on the record, setDestroyed only stopped the object being
; activated again.  Oblivion ships ZERO DEST subrecords, so nothing we convert
; has a destroyed state either -- but Skyrim's SetDestroyed still RESETS THE
; REFERENCE'S 3D, and doing that one line after PlayAnimation tore down the
; NiControllerSequence before a single frame of it had been drawn.  That is
; what stopped the tripwire visibly snapping when it was walked over.
;
; Waiting first preserves both halves of the original intent: the break
; animation plays to completion, and the object still ends up destroyed so it
; cannot fire a second time.  Same 1.0s budget as ReleaseBreakaway, chosen the
; same way -- Papyrus cannot query a Gamebryo sequence's length, and every
; converted break clip but one outlier finishes well inside it.
Function DestroyAfterAnimation(ObjectReference akRef) Global
  If akRef == None
    Return
  EndIf
  Utility.Wait(1.0)
  akRef.SetDestroyed(true)
EndFunction

; ==========================================================================
; Spoken lines: TES4 `set T to [ref.]Say[To] [target,] Topic`
; ==========================================================================
;
; TES4's Say/SayTo were SYNCHRONOUS: the engine picked the INFO, started the
; audio and RETURNED ITS LENGTH before the next script line ran, so a polled
; conversation is written as
;
;     if speaker == 4 && convTimer <= 0
;         set convTimer to SayTo player, CharGenMain     ; := line length
;     endif
;
; and every other participant waits on the same countdown.  Papyrus Say() is
; fire-and-forget and returns nothing, so the length has to come from the
; engine's OWN signal that the line is under way: the INFO's OnBegin fragment.
; Every converted INFO carries a Begin+End fragment whose fixed job is to call
; LineBegan / LineEnded here; the state lives in script Actor Values ON THE
; SPEAKER, so no property binding and no per-quest owner analysis is needed:
;
;     Variable06  real time the current line began (diagnostics)
;     Variable07  claim token while a SayLine is in progress for this speaker
;     Variable08  claim deadline (game time, days) - a stale claim expires
;     Variable09  length of the line now playing (0 = not speaking)
;     Variable10  speaking deadline (game time) - a lost End fragment expires
;   and on the PLAYER, Variable05/06 = hi/lo halves of the FormID of the last
;   actor to speak a line inside the player's dialogue menu (PlayerIsInDialogue).
;   Every SayLine / LineBegan / LineEnded writes a "TES4Say ..." Debug.Trace
;   with real-time stamps: the Papyrus log (or the bridge's vmlog) then gives
;   the engine's Say->Begin and Begin->End latencies against the measured
;   length, which is what SAY_TAIL must cover.
;
; SayLine restores the TES4 contract exactly: it BLOCKS until the engine has
; begun the line, then returns that line's real length (+ SAY_TAIL, which
; covers the End fragment's dispatch latency), and the caller's script goes on
; immediately - the countdown, the `speaker` handoff and any `set convTimer to
; convTimer + 2` pause the End result adds all behave as they did in Oblivion.
; A Say nothing under the topic qualifies for returns 0 after SAY_START_WAIT,
; and the caller's own poll simply retries - which is what Oblivion did too.
;
; Waits, in order:
;   * the speaker is in the player's dialogue menu -> wait (Oblivion froze
;     GameMode while any menu was open; a Say on an actor in dialogue is lost
;     or, per the CK wiki, can crash);
;   * the speaker is still speaking a tracked line -> wait for its End
;     (Oblivion cut the line; Skyrim silently DROPS the new Say instead, and
;     with it the result script that advances the scene);
;   * one waiter per speaker: a second SayLine while one is pending returns
;     a short backoff instead of queueing a duplicate.

; Seconds added to the returned line length.  This is most of the dead air
; between consecutive lines (the rest is the caller's poll tick and the
; engine's Say -> audio latency).  It must cover the time between the measured
; audio length and the End fragment actually running (the fragment's dispatch,
; the engine's trailing hold, inter-response gaps of a multi-response line);
; when it does not, the guard reopens before the End result has advanced the
; conversation state.  0.4 was tried in game (2026-08-16) and lines REPEATED,
; so the End overhead is evidently larger than that; the "TES4Say LineEnded
; ... measured= actual=" traces report the true overhead per line -- set this
; from those, not from a guess.
Float Function SAY_TAIL() Global
  Return 1.0
EndFunction

Float Function SayLine(ObjectReference akSpeaker, Topic akTopic, Float afFallbackLength) Global
  Actor a = akSpeaker as Actor
  If a == None || akTopic == None || (a as Form).GetFormID() == 0x14
    ; Not an actor we can track (a talking activator, the player): open loop.
    If akSpeaker != None && akTopic != None
      akSpeaker.Say(akTopic)
    EndIf
    Return afFallbackLength + SAY_TAIL()
  EndIf
  Float now = Utility.GetCurrentGameTime()
  If a.GetActorValue("Variable07") > 0.0 && now < a.GetActorValue("Variable08")
    Return 0.5   ; another SayLine already owns this speaker's next line; poll again shortly
  EndIf
  ; Claim the speaker.  SetActorValue lands on the game thread a frame later,
  ; so two callers arriving in the same frame both read "free" above; the
  ; token + re-read after a frame lets exactly one of them keep the claim.
  Float token = Utility.RandomFloat(1.0, 1000000.0)
  Float claimDays = _GameDays(5.0)   ; a claim not renewed for 5s is stale
  a.SetActorValue("Variable07", token)
  a.SetActorValue("Variable08", now + claimDays)
  Utility.Wait(0.05)
  If a.GetActorValue("Variable07") != token
    Return 0.5
  EndIf
  Debug.Trace("TES4Say request " + _Who(a) + " topic " + akTopic + " t=" + Utility.GetCurrentRealTime())
  ; Wait out the player's dialogue menu and any line still playing.
  Float waited = 0.0
  While waited < 600.0 && (a.IsInDialogueWithPlayer() || _IsSpeaking(a))
    Utility.Wait(0.15)
    waited += 0.15
    a.SetActorValue("Variable08", Utility.GetCurrentGameTime() + claimDays)
  EndWhile
  If waited > 0.0
    ; The line we waited for ends when its End fragment RETURNS; LineEnded is
    ; that fragment's last statement, so a Say issued the instant the flag
    ; clears reaches an actor the engine still counts as talking and is
    ; dropped (measured 2026-08-16: every post-wait Say dropped).  Let the
    ; fragment finish.
    Utility.Wait(0.25)
  EndIf
  ; Request the line and wait for the engine to begin it (LineBegan stores
  ; the length in Variable09).
  a.SetActorValue("Variable09", 0.0)
  Float t0 = Utility.GetCurrentRealTime()
  a.Say(akTopic)
  ; Nominal 1.5s: the engine begins a line it accepts within ~0.15-0.26s
  ; (measured), and each iteration is a VM turn, so under load the real
  ; wait stretches with everything else.
  Float t = 0.0
  While t < 1.5 && a.GetActorValue("Variable09") == 0.0
    Utility.Wait(0.05)
    t += 0.05
  EndWhile
  Float len = a.GetActorValue("Variable09")
  a.SetActorValue("Variable07", 0.0)
  If len <= 0.0
    Debug.Trace("TES4Say dropped " + _Who(a) + " topic " + akTopic + " waited=" + waited + " inCombat=" + a.IsInCombat() + " weaponDrawn=" + a.IsWeaponDrawn() + " alerted=" + a.IsAlerted() + " t=" + Utility.GetCurrentRealTime())
    Return 0.0   ; dropped: nothing under the topic qualified (or the engine refused it)
  EndIf
  If len < 0.02
    len = afFallbackLength   ; began, but the line has no measured voice file
  EndIf
  Debug.Trace("TES4Say began " + _Who(a) + " topic " + akTopic + " len=" + len + " startLatency=" + (Utility.GetCurrentRealTime() - t0) + " waited=" + waited + " t=" + Utility.GetCurrentRealTime())
  Return len + SAY_TAIL()
EndFunction

; OnBegin fragment hook: the engine has selected this INFO and started it.
Function LineBegan(ObjectReference akSpeakerRef, Float afLength) Global
  Actor a = akSpeakerRef as Actor
  If a == None || (a as Form).GetFormID() == 0x14
    Return
  EndIf
  Float len = afLength
  If len <= 0.0
    len = 0.01                 ; unknown length: still marks "speaking"
  EndIf
  a.SetActorValue("Variable09", len)
  ; Speaking deadline: VERY generous.  It only exists so a LOST End (actor
  ; killed or unloaded mid-line) cannot strand the speaker as busy forever;
  ; a late one must always hold a re-Say off.  Measured 2026-08-16 under a
  ; starved VM (start of CharacterGen): End fragments of 1-2s lines ran
  ; 11-17s late, a 10s margin expired first, and the speaker's own poll
  ; re-Said the line ("Yessir" twice).
  Float bound = afLength
  If bound <= 0.0
    bound = 10.0
  EndIf
  a.SetActorValue("Variable10", Utility.GetCurrentGameTime() + _GameDays(bound + 30.0))
  a.SetActorValue("Variable06", Utility.GetCurrentRealTime())
  ; A line spoken IN THE PLAYER'S DIALOGUE MENU: remember the speaker on the
  ; player, so PlayerIsInDialogue() can ask that actor whether the menu is
  ; still open (Skyrim has no direct "is the player in dialogue" query).
  If akSpeakerRef.IsInDialogueWithPlayer()
    Int fid = (akSpeakerRef as Form).GetFormID()
    Actor p = Game.GetPlayer()
    p.SetActorValue("Variable05", Math.Floor(fid / 65536) as Float)
    p.SetActorValue("Variable06", (fid - Math.Floor(fid / 65536) * 65536) as Float)
  EndIf
  Debug.Trace("TES4Say LineBegan " + _Who(a) + " len=" + afLength + " inDialogue=" + akSpeakerRef.IsInDialogueWithPlayer() + " t=" + Utility.GetCurrentRealTime())
EndFunction

; OnEnd fragment hook: the line (all of its responses) has finished -- or was
; cut.  Clears the speaking flag ONLY if it still belongs to THIS line.
;
; The player can skip a menu line (click through the greeting) or exit the
; menu; the skipped line's End fragment and the next line's Begin fragment
; then run in the same frame, and End can land SECOND.  An unconditional
; clear then wiped the flag of the line that had just started; the speaker's
; own poll saw him idle, its Say() INTERRUPTED the live line, and that line's
; End result -- CharGenEmperor09's `setstage 43` -- was lost (measured
; 2026-08-16, three of three runs showed the ordering, one soft-locked).  The
; fragment knows its own length, so match on it: a mismatch means a newer
; line owns the flag and it is left alone.
Function LineEnded(ObjectReference akSpeakerRef, Float afLength = -1.0) Global
  Actor a = akSpeakerRef as Actor
  If a == None || (a as Form).GetFormID() == 0x14
    Return
  EndIf
  Float began = a.GetActorValue("Variable06")
  Float cur = a.GetActorValue("Variable09")
  Bool mine = afLength < 0.0 || Math.abs(cur - afLength) < 0.006 || (afLength <= 0.0 && cur <= 0.02)
  If mine
    a.SetActorValue("Variable09", 0.0)
  EndIf
  Debug.Trace("TES4Say LineEnded " + _Who(a) + " measured=" + afLength + " playing=" + cur + " cleared=" + mine + " actual=" + (Utility.GetCurrentRealTime() - began) + " t=" + Utility.GetCurrentRealTime())
EndFunction

; True while the player is in a dialogue menu with anyone -- Oblivion's
; GameMode never ran then, so converted actor polls skip their pass.  Called
; every poll tick by every actor script, so it must be CHEAP: two AV reads
; when nobody has stamped a dialogue speaker, and the stamp is cleared as
; soon as that speaker reports the menu closed, so the GetForm +
; IsInDialogueWithPlayer pair only runs while a dialogue is actually open.
Bool Function PlayerIsInDialogue() Global
  Actor p = Game.GetPlayer()
  Float hi = p.GetActorValue("Variable05")
  Float lo = p.GetActorValue("Variable06")
  If hi <= 0.0 && lo <= 0.0
    Return False
  EndIf
  If hi >= 32768.0
    p.SetActorValue("Variable05", 0.0)   ; a runtime-created (FF) reference: cannot be rebuilt as an Int
    p.SetActorValue("Variable06", 0.0)
    Return False
  EndIf
  ObjectReference r = Game.GetForm((hi as Int) * 65536 + (lo as Int)) as ObjectReference
  If r != None && r.IsInDialogueWithPlayer()
    Return True
  EndIf
  ; A Goodbye reply keeps playing after the menu closes (and the player can
  ; leave mid-line): Oblivion's menu stayed up until the line was over, so
  ; hold the polls until the last dialogue speaker has finished it.
  Actor ra = r as Actor
  If ra != None && _IsSpeaking(ra)
    Return True
  EndIf
  p.SetActorValue("Variable05", 0.0)
  p.SetActorValue("Variable06", 0.0)
  Return False
EndFunction

Bool Function _IsSpeaking(Actor a) Global
  Return a.GetActorValue("Variable09") > 0.0 && Utility.GetCurrentGameTime() < a.GetActorValue("Variable10")
EndFunction

String Function _Who(Actor a) Global
  Return "actor " + (a as Form).GetFormID()   ; names need SKSE; the id maps through the manifest
EndFunction

; Real seconds -> game-time days at the current TimeScale.  Deadlines are kept
; in GAME time because GetCurrentRealTime restarts with the process: a stamp
; saved in one session compares against a different clock in the next.
Float Function _GameDays(Float afSeconds) Global
  GlobalVariable ts = Game.GetFormFromFile(0x0000003A, "Skyrim.esm") as GlobalVariable
  Float scale = 20.0
  If ts != None && ts.GetValue() > 0.0
    scale = ts.GetValue()
  EndIf
  Return afSeconds * scale / 86400.0
EndFunction
