ScriptName TES4_GhostDissolve extends Actor Hidden
{Dissolves a TES4 ghost/wraith corpse into the pile of ectoplasm Oblivion
leaves behind, using Skyrim's own ash-pile mechanism.

WHY THIS EXISTS.  An Oblivion ghost's death is VISIBILITY, not motion.
`ghost/death.kf` never lowers the body: `Bip01 NonAccum` stays at standing
height for the whole 1.17s clip.  What makes the ghost disappear is 47
non-transform controlled blocks — NiVisController toggles that hide
SkinAttachment / AttachmentsHead / the hand attachments while REVEALING
AttachmentsBip (the ectoplasm) and AttachmentsShrink, plus
NiGeomMorpherController (Base -> Shrunk), NiAlphaController fades and ten
particle emitters.  A Havok clip carries bone transforms ONLY, so every one of
those channels is dropped in conversion.  With nothing left to hide the body,
no ragdoll to collapse it (ghost and wraith have no extractable ragdoll) and a
Death state that holds its last frame, the corpse stood upright in mid-air.

THE PILE IS OBLIVION'S OWN.  `AttachAshPile` takes any base object, so the
importer passes a STAT built from the ectoplasm geometry lifted straight out
of the creature's own skeleton.nif (`Bip01 ectoplasm:0` for the ghost,
`Cloak06:0` for the wraith — the node its death clip reveals), baked to the
position that clip leaves it at.  Skyrim's DefaultAshPileGhost is only the
fallback when a creature has no authored pile of its own.

WHY THE BODY IS SCALED AWAY AND NOT DISABLED.  An attached ash pile is an
ENABLE CHILD of the actor: `Disable(true)` on the corpse takes the pile with
it, which is exactly what happened in game — the ectoplasm appeared and then
faded away with the body.  (The CK wiki says as much under Disable: "If this
is an enable parent the children will not be faded.")  Shrinking the actor
instead removes the body from view while leaving the pile — a separate
reference — untouched and lootable.  The corpse is never deleted or disabled,
so its inventory, quest aliases and the pile's activation forwarding all keep
working: activating the pile passes the activation to the actor, which is how
the ghost stays lootable for its Ectoplasm exactly as in Oblivion.}

; The pile to drop.  Bound by the importer to a STAT built from this
; creature's OWN authored ectoplasm, or to vanilla DefaultAshPileGhost when it
; has none.  Typed Form because AttachAshPile takes a base object OR a
; leveled item list.
Form Property AshPile Auto
{This creature's extracted ectoplasm STAT (fallback: DefaultAshPileGhost).}

; How long the converted death animation runs, in seconds.  Filled per
; creature from the decoded death.kf duration so the body is not yanked away
; mid-clip: the ghost plays its authored death, THEN the pile takes over.
float Property DeathAnimSeconds = 1.2 Auto
{Decoded duration of this creature's death.kf.}

; What the corpse is shrunk to.  Not 0.0: SetScale rejects zero, and a very
; small positive scale collapses the body below a pixel at any camera
; distance, which is what "invisible" means here.
float Property HiddenScale = 0.01 AutoReadOnly

; Guard: OnDying and OnDeath can both arrive, and a reanimated corpse can die
; twice.  Without this the actor would collect a second pile each time.
bool dissolved

Event OnDying(Actor akKiller)
  Dissolve()
EndEvent

; Belt and braces — a corpse that somehow reaches full death without OnDying
; (reanimation, a script-driven kill) still dissolves.
Event OnDeath(Actor akKiller)
  Dissolve()
EndEvent

Function Dissolve()
  If dissolved
    Return
  EndIf
  dissolved = true

  ; Drop the ectoplasm FIRST, while the body is still visible and the death
  ; animation is playing, so there is never a frame with neither on screen.
  If AshPile != None
    AttachAshPile(AshPile)
  Else
    ; None uses the engine's own default pile, which beats dropping nothing
    ; if the property ever fails to bind.
    AttachAshPile()
  EndIf

  ; Let the authored death animation finish before taking the body away.
  Utility.Wait(DeathAnimSeconds)

  ; Shrink the corpse out of view.  NOT Disable(): the pile is an enable child
  ; of this actor and would fade out with it (in-game 2026-08-26 — "the
  ; ectoplasm pile appears but then it also fades away").  The reference stays
  ; enabled and lootable through the pile.
  SetScale(HiddenScale)
EndFunction
