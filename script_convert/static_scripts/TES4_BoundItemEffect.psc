ScriptName TES4_BoundItemEffect extends ActiveMagicEffect Hidden
{Conjures a TES4 bound weapon/armor piece onto the target for the effect's
duration, then takes it back — and restores what it displaced — when the
effect ends OR when the target dies.

WHY THIS EXISTS.  Skyrim's native archetype 17 covers strictly less ground than
Oblivion's bound-item family, in two independent ways:

1. SKYRIM HAS NO BOUND ARMOR.  Archetype 17 is a bound *weapon* implementation.
   xEdit types the Assoc. Item field as [WEAP, ARMO, NULL], but that is only
   what the field accepts — all seven vanilla archetype-17 effects name a WEAP
   and not one names an ARMO.  So Oblivion's whole bound-armor family (BACU
   cuirass, BAGR greaves, BAGA gauntlets, BAHE helmet, BABO boots, BASH shield,
   plus the Mythic Dawn set) is inert under the native path however it is cast.

2. ARCHETYPE 17 ONLY FIRES ON A CAST.  Oblivion also delivers bound gear via
   Abilities (SPIT.Type 4) and Lesser Powers (Type 3), which Skyrim applies
   passively and never casts — so even a bound WEAPON dies when delivered that
   way.  (Vanilla census: archetype 17 appears under Type 0 only.)

This script is the archetype-1 (Script) stand-in for both cases.  A bound
weapon on an ordinary castable spell keeps the engine's own implementation.

WHY TEARDOWN IS ON *TWO* HOOKS.  Bound gear is not real equipment: in Oblivion
it vanishes the moment the effect drops, and a corpse must never be lootable
for it.  OnEffectFinish alone does NOT achieve that here — an ABILITY is a
permanent, never-cast effect that simply never finishes, so on death the engine
stops processing the actor without ever running it (confirmed in-game: the
assassins wore the armor correctly but kept it after dying).  OnDying fires
while the actor is still alive, before the body can be searched, and is the
hook that actually runs.  Both call the same idempotent teardown.

WHY THE DISPLACED ARMOR IS TRACKED EXPLICITLY.  A bound cuirass covers the same
biped slots as whatever the actor already wore — the assassins' Mythic Dawn
robe occupies body/hands/feet, exactly what the bound armor takes — so equipping
it silently displaces that gear.  Removing the bound piece does not reliably
bring the original back: a dying or dead actor never re-evaluates its equipment.
So each slot the bound item claims is sampled BEFORE it is equipped, and those
items are re-equipped by name during teardown.}

; The conjured WEAP or ARMO.  Bound to the MGEF's Assoc. Item by the importer.
; Typed Form, not Armor/Weapon: one script serves both families, and AddItem /
; EquipItem take a base Form.  (An ObjectReference property would bind only to
; a placed REFR and would leave this None for every base-record item.)
Form Property BoundItem Auto
{Filled from the TES4 MGEF DATA.AssocItem (a WEAP, ARMO or converted CLOT).}

; Skyrim's biped slots are numbered 30-61.  The whole range is swept rather
; than a hand-picked few: Oblivion's bound pieces span more slots than the
; obvious four (the Mythic Dawn armor claims 32/33/37/44 and its helmet
; 30/31/41/42/43), and which slots a converted piece lands on depends entirely
; on the source plugin's biped flags — this must work for any plugin, not just
; the two test files.
int Property FirstBipedSlot = 30 AutoReadOnly
int Property LastBipedSlot  = 61 AutoReadOnly

; The actor holding the conjured item.  Cached at start so teardown still finds
; it when the effect's own target pointer has gone stale — which is exactly the
; case during death handling.  Cleared by teardown, which is also what makes
; teardown idempotent.
Actor akHolder

; What the bound item displaced, indexed by (slot - FirstBipedSlot).
Armor[] displaced

; The weapons the conjured item displaced, when it is itself a weapon.
Weapon displacedRight
Weapon displacedLeft

Event OnEffectStart(Actor akTarget, Actor akCaster)
  If akTarget == None || BoundItem == None
    Return
  EndIf

  akHolder = akTarget

  ; Sample what the conjured piece is about to take over, BEFORE equipping it,
  ; so it can be put back exactly as it was.  Both families are covered: this
  ; script serves bound ARMOR (which Skyrim has no engine support for at all)
  ; as well as bound weapons delivered by a never-cast spell.
  displaced = new Armor[32]
  int slot = FirstBipedSlot
  While slot <= LastBipedSlot
    displaced[slot - FirstBipedSlot] = akHolder.GetEquippedArmorInSlot(slot)
    slot += 1
  EndWhile
  displacedRight = akHolder.GetEquippedWeapon(false)
  displacedLeft = akHolder.GetEquippedWeapon(true)

  ; abSilent = true: no pickup sound and no "item added" HUD line, matching
  ; Oblivion's bound gear, which materialises with the spell's own effect.
  akHolder.AddItem(BoundItem, 1, true)
  ; abPreventRemoval = true keeps the engine's own equip logic (and the actor's
  ; combat AI re-scoring its inventory) from swapping the conjured piece back
  ; out for ordinary gear while the effect is running.  It does not block the
  ; scripted UnequipItem in teardown — vanilla pairs the two the same way
  ; (Serana's Elder Scroll: EquipItem(..., true) then UnequipItem).
  akHolder.EquipItem(BoundItem, true, true)
EndEvent

; Fires as the actor begins dying — still alive, before the corpse is lootable.
; This is the hook that actually runs for ability-delivered bound gear.
Event OnDying(Actor akKiller)
  ReclaimBoundItem()
EndEvent

; Belt and braces: a body that somehow reaches full death without OnDying still
; gets cleaned up before the player can search it.
Event OnDeath(Actor akKiller)
  ReclaimBoundItem()
EndEvent

; The normal path for a genuinely timed effect (a cast spell or a duration that
; runs out) — and for dispel.
Event OnEffectFinish(Actor akTarget, Actor akCaster)
  If akHolder == None
    akHolder = akTarget
  EndIf
  ReclaimBoundItem()
EndEvent

; Take the conjured item back and put the actor's own gear back on.  Idempotent:
; akHolder is cleared on the first call, so the second hook to arrive is a no-op.
Function ReclaimBoundItem()
  Actor holder = akHolder
  If holder == None || BoundItem == None
    Return
  EndIf
  ; Clear FIRST so a second hook arriving mid-call cannot double-remove.
  akHolder = None

  ; Default arguments, deliberately.  UnequipItem's second parameter is
  ; abPreventEquip, and passing true there tells the engine NOT to re-equip
  ; anything in the freed slot.  Every UnequipItem call in vanilla Skyrim's own
  ; scripts uses the defaults.
  holder.UnequipItem(BoundItem)
  holder.RemoveItem(BoundItem, holder.GetItemCount(BoundItem), true)

  ; Re-equip what the bound piece displaced.  The engine's own re-evaluation
  ; handles this for a living actor, but a dying/dead one never re-evaluates,
  ; so the original outfit is restored by name instead of by hope.
  If displaced != None
    int i = 0
    While i < displaced.Length
      Armor piece = displaced[i]
      ; A multi-slot garment is sampled once per slot it covers, so skip
      ; anything already back on rather than re-equipping the same robe.
      If piece != None && holder.GetItemCount(piece) > 0 && !holder.IsEquipped(piece)
        holder.EquipItem(piece, false, true)
      EndIf
      i += 1
    EndWhile
    displaced = None
  EndIf

  ; And the weapon the conjured one replaced, for a bound WEAPON effect.
  If displacedRight != None && holder.GetItemCount(displacedRight) > 0
    holder.EquipItem(displacedRight, false, true)
  EndIf
  If displacedLeft != None && holder.GetItemCount(displacedLeft) > 0
    holder.EquipItem(displacedLeft, false, true)
  EndIf
  displacedRight = None
  displacedLeft = None
EndFunction
