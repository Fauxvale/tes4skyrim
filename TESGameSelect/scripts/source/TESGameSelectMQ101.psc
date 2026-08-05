ScriptName TESGameSelectMQ101 extends Quest Hidden
{Threads of Prophecy — the MQ101 takeover.

Attached to the VANILLA MQ101 record (0003372B), which this plugin overrides.
The build RETARGETS the stage-0 / log-entry-0 fragment — the one vanilla points
at QF_MQ101_0003372B.Fragment_2 and gates on GetGlobalValue(MQQuickstart) == 0,
i.e. the real new-game path — to this script's RunTakeover.

Why retargeting, not appending: an APPENDED unconditional entry runs IN
ADDITION to Fragment_2, whose whole body is `GameHour.SetValue(7);
SetStage(10)`. Stage 10 is the entire opening: it equips the prisoner outfit,
moves the player into the cart, plays the title sequence
(Game.ShowTitleSequenceMenu) and the cart-roll sound. That is exactly the
"credits play, cart still audible, player not transferred" failure. Replacing
the fragment means NOTHING of the opening runs until the player has chosen —
and choosing Skyrim just replays Fragment_2's two lines, after which stage 10
does everything itself (Fragment_4 even MoveTo's the player into position, so
no cart-position capture/restore is needed).

The menu is shown exactly once, after the initial load: stage 0 fires while
the main menu / load screen is still up, and a Message.Show() issued then is
rendered over the main menu, bashed by the load screen, and drawn again after
it — the "popup appears twice" failure. The Is3DLoaded/Wait gate below defers
the menu until the player is actually standing in the holding cell.}

; The selector quest that owns the menu and the per-game handoff.
TESGameSelectQuest Property Selector Auto

; Skyrim's own empty interior cell marker (WIDeadBodyCleanupCellMarker) — the
; player waits here while the menu is up, somewhere genuinely blank.
ObjectReference Property HoldingCellMarker Auto

; Skyrim.esm's GameHour global (0x38). Choosing Skyrim replays vanilla
; Fragment_2 verbatim, and its first line is GameHour.SetValue(7).
GlobalVariable Property GameHour Auto

; ---------------------------------------------------------------------------
; Fired from MQ101 stage 0, log entry 0 — the retargeted vanilla fragment.
; Runs exactly once per new game (the entry keeps its MQQuickstart == 0
; condition, so debug quickstarts bypass the takeover entirely).
; ---------------------------------------------------------------------------
Function RunTakeover()
  If Selector == None
    Debug.Trace("[TESGameSelect] selector quest unbound; vanilla start proceeds")
    VanillaStart()
    Return
  EndIf
  If Selector.HasRun
    Return
  EndIf

  ; Freeze the (not yet started) opening: no controls, no saving.
  Game.DisablePlayerControls()
  Game.SetInChargen(true, true, false)

  Actor player = Game.GetPlayer()
  If HoldingCellMarker != None
    player.MoveTo(HoldingCellMarker)
  EndIf

  ; Wait out the initial load. Utility.Wait only elapses while the game is
  ; unpaused, so the first tick already lands after the load screen; the
  ; Is3DLoaded check covers the MoveTo settling. Capped so a pathological
  ; load can never wedge the takeover.
  Int guard = 0
  While !player.Is3DLoaded() && guard < 200
    Utility.Wait(0.1)
    guard += 1
  EndWhile

  ; Ask the question. Only records the choice — no side effects yet.
  Selector.RunSelection()

  ; Hand the engine back its default state before either path starts: vanilla
  ; Fragment_2 ran with controls enabled and chargen-state clear, and the
  ; converted chargens disable/enable to suit their own intros AFTER this.
  Game.SetInChargen(false, false, false)
  Game.EnablePlayerControls()

  If Selector.ChoseSkyrim()
    VanillaStart()
  Else
    Selector.BeginChosenGame()
    If Selector.ChoseSkyrim()
      ; The handoff found the chosen game broken and fell back.
      VanillaStart()
    Else
      ; The other game owns the player now. MQ101 stays at stage 0 forever:
      ; no cart, no Helgen, and nothing downstream (MQ102 is only started by
      ; MQ101's own later stages) — the Skyrim main quest never advances.
      Stop()
    EndIf
  EndIf
EndFunction

; Vanilla QF_MQ101_0003372B.Fragment_2, verbatim: the whole normal-start
; fragment is these two lines, and stage 10 does everything else.
Function VanillaStart()
  If GameHour != None
    GameHour.SetValue(7)
  EndIf
  SetStage(10)
EndFunction
