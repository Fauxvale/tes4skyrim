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

String Function MapActorValue(String avName) Global
  ; Attributes (removed in TES5 — map to closest equivalent)
  If avName == "Strength"
    Return "UnarmedDamage"
  ElseIf avName == "Intelligence"
    Return "Magicka"
  ElseIf avName == "Willpower"
    Return "MagickaRate"
  ElseIf avName == "Agility"
    Return "SpeedMult"
  ElseIf avName == "Speed"
    Return "SpeedMult"
  ElseIf avName == "Endurance"
    Return "HealRate"
  ElseIf avName == "Personality"
    Return "Speechcraft"
  ElseIf avName == "Luck"
    Return "Health"
  ; Skills (renamed in TES5)
  ElseIf avName == "Armorer"
    Return "Smithing"
  ElseIf avName == "Athletics"
    Return "Stamina"
  ElseIf avName == "Blade"
    Return "OneHanded"
  ElseIf avName == "Blunt"
    Return "TwoHanded"
  ElseIf avName == "HandToHand"
    Return "UnarmedDamage"
  ElseIf avName == "Mysticism"
    Return "Alteration"
  ElseIf avName == "Mercantile"
    Return "Speechcraft"
  ElseIf avName == "Security"
    Return "Lockpicking"
  ElseIf avName == "Acrobatics"
    Return "SpeedMult"
  ElseIf avName == "Fatigue"
    Return "Stamina"
  ElseIf avName == "Encumbrance"
    Return "CarryWeight"
  Else
    Return avName
  EndIf
EndFunction

Float Function GetTES4ActorValue(Actor akActor, String avName) Global
  Return akActor.GetActorValue(MapActorValue(avName))
EndFunction

Function SetTES4ActorValue(Actor akActor, String avName, Float afValue) Global
  akActor.SetActorValue(MapActorValue(avName), afValue)
EndFunction

Function ModTES4ActorValue(Actor akActor, String avName, Float afValue) Global
  akActor.ModActorValue(MapActorValue(avName), afValue)
EndFunction

Function ForceTES4ActorValue(Actor akActor, String avName, Float afValue) Global
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
