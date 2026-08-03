ScriptName TESGameSelectQuest extends Quest Conditional
{Threads of Prophecy — new-game game selector.

Runs once at the start of a new game (Start Game Enabled + listed in
TESGameSelect.seq).  Detects which converted TES games are present in the
current load order, offers the player a choice, and hands control to that
game's own character generation.

Every foreign form is resolved at runtime with Game.GetFormFromFile(), so this
plugin declares only Skyrim.esm as a master and ships to users with any subset
of the converted games installed, in any load order.  A game whose plugin is
absent never appears in the menu.}

; ---------------------------------------------------------------------------
; Plugin file names.  Properties rather than literals so a repack for a renamed
; or translated plugin needs no recompile — just an xEdit property edit.
; ---------------------------------------------------------------------------
String Property OblivionPlugin     = "Oblivion.esm"     Auto
String Property NehrimPlugin       = "Nehrim.esm"       Auto
String Property MorroblivionPlugin = "Morrowind_ob.esm" Auto

; ---------------------------------------------------------------------------
; Per-game entry points.  GetFormFromFile takes a form's ID *within its own
; file*, so only the low 24 bits matter and the load order can be anything.
;
;   Oblivion      Charactergen 0002466E — stage 5 is the whole start: sets
;                 in-chargen, starts MQ01 at stage 5, and moves the player to
;                 CGPlayerStartMarker 00032AB5 in the Imperial Prison.
;   Nehrim        Charactergen 0002466E — stage 5 moves the player to
;                 PlayerMarkerStartCell 00000D33.  MQ00 00000811 is the real
;                 intro driver and stops Charactergen once controls return.
;   Morroblivion  fbmwChargen 01F0A28C — stage 1 opens the prison-ship
;                 sequence; mwCGPlayerStartMarker 01F0A278 is the wake-up spot.
; ---------------------------------------------------------------------------
Int Property OblivionChargenID     = 0x0002466E Auto
Int Property OblivionStartMarkerID = 0x00032AB5 Auto
Int Property OblivionChargenStage  = 5          Auto

Int Property NehrimChargenID       = 0x0002466E Auto
Int Property NehrimStartMarkerID   = 0x00000D33 Auto
Int Property NehrimMainQuestID     = 0x00000811 Auto
Int Property NehrimChargenStage    = 5          Auto

Int Property MorroChargenID        = 0x00F0A28C Auto
Int Property MorroStartMarkerID    = 0x00F0A278 Auto
Int Property MorroChargenStage     = 1          Auto

; MQ101 "Unbound" — Skyrim's own opening, which owns the cart ride and holds
; player controls disabled until Helgen is over.
Int Property SkyrimOpeningID       = 0x0003372B Auto

; ---------------------------------------------------------------------------
; The menu.
;
; Message.Show() renders a MESG record whose buttons are fixed at authoring
; time, but the set of installed games is only known at runtime.  The vanilla
; mechanism for that is a CONDITION on each button (dunMiddenNamesMenuMSG does
; exactly this): a button whose condition fails is simply not drawn.
;
; Crucially, hidden buttons do NOT renumber the rest — Show() returns the
; button's ORIGINAL index, not its position among the visible ones.  Vanilla's
; dunMiddenHandSculptureSCRIPT relies on this, testing `i == 0`..`i == 4`
; against fixed ring identities while its conditions hide arbitrary subsets.
; So the returned index maps directly onto the GAME_* constants below and no
; remapping table is needed (an earlier version of this script compacted the
; indices and would have misrouted every choice whenever a game was absent).
;
; The MESG therefore ships all four buttons in GAME_* order, each gated on
; GetGlobalValue(<its global>) == 1, and this script sets those globals from
; the detection pass before showing the menu.
; ---------------------------------------------------------------------------
Message Property GameSelectMenu Auto

GlobalVariable Property HasSkyrim       Auto
GlobalVariable Property HasOblivion     Auto
GlobalVariable Property HasNehrim       Auto
GlobalVariable Property HasMorroblivion Auto

; Seconds to let the vanilla opening settle before taking over.  MQ101 places
; the player in the cart and disables controls during its first moments; opening
; the menu before that has finished lets it re-disable controls underneath us.
Float Property StartupDelay = 3.0 Auto

Bool Property HasRun = false Auto Conditional

; Game identifiers, in the button order the MESG declares.
Int Property GAME_SKYRIM       = 0 AutoReadOnly
Int Property GAME_OBLIVION     = 1 AutoReadOnly
Int Property GAME_NEHRIM       = 2 AutoReadOnly
Int Property GAME_MORROBLIVION = 3 AutoReadOnly

; Number of games offered, counting Skyrim. 1 means "Skyrim only" — no menu.
Int gameCount

Event OnInit()
  ; A Start-Game-Enabled quest runs OnInit at new-game start, and also when the
  ; plugin is first added to an existing save.  HasRun keeps the offer to one
  ; per character either way.
  StartSelection()
EndEvent

Function StartSelection()
  If HasRun
    Return
  EndIf
  HasRun = true

  DetectInstalledGames()

  ; Only Skyrim present — nothing to choose, leave the vanilla start alone.
  If gameCount <= 1
    Return
  EndIf

  Utility.Wait(StartupDelay)

  ; The returned index is the button's own index in the MESG, unaffected by
  ; which buttons the conditions hid — so it IS the game id.
  Int game = GameSelectMenu.Show()

  If game == GAME_OBLIVION
    BeginOblivion()
  ElseIf game == GAME_NEHRIM
    BeginNehrim()
  ElseIf game == GAME_MORROBLIVION
    BeginMorroblivion()
  EndIf
  ; GAME_SKYRIM (and any unexpected index) falls through: MQ101 keeps running
  ; exactly as vanilla.
EndFunction

; ---------------------------------------------------------------------------
; Detection
; ---------------------------------------------------------------------------

Function DetectInstalledGames()
  gameCount = 0

  ; Skyrim is always available — it is the game we are running inside.
  SetGate(HasSkyrim, true)
  SetGate(HasOblivion, IsPluginPresent(OblivionPlugin, OblivionChargenID))
  SetGate(HasNehrim, IsPluginPresent(NehrimPlugin, NehrimChargenID))
  SetGate(HasMorroblivion, \
          IsPluginPresent(MorroblivionPlugin, MorroChargenID))
EndFunction

Function SetGate(GlobalVariable gate, Bool present)
  ; The global drives that button's MESG condition: 1 shows it, 0 hides it.
  If gate != None
    If present
      gate.SetValue(1.0)
    Else
      gate.SetValue(0.0)
    EndIf
  EndIf

  If present
    gameCount += 1
  EndIf
EndFunction

Bool Function IsPluginPresent(String plugin, Int probeID)
  ; GetFormFromFile returns None when the file is not in the load order, so a
  ; successful lookup of a form we know that file defines proves it is loaded.
  Return Game.GetFormFromFile(probeID, plugin) != None
EndFunction

; ---------------------------------------------------------------------------
; Per-game handoff
;
; Each converted game's chargen quest owns its own opening: its stage result
; script moves the player, starts the companion quests, and sets the in-chargen
; state.  We only stop Skyrim's opening, start the right quest, and set the
; stage its own author wrote as "the game begins here" — then get out of the
; way.
; ---------------------------------------------------------------------------

Function BeginOblivion()
  HandOff(GetQuestFrom(OblivionChargenID, OblivionPlugin), \
          OblivionChargenStage, \
          GetRefFrom(OblivionStartMarkerID, OblivionPlugin))
EndFunction

Function BeginNehrim()
  HandOff(GetQuestFrom(NehrimChargenID, NehrimPlugin), \
          NehrimChargenStage, \
          GetRefFrom(NehrimStartMarkerID, NehrimPlugin))

  ; Nehrim's intro is driven by MQ00, not by Charactergen — Charactergen only
  ; places the player, and MQ00 stage 2 is what stops it and hands controls
  ; back.  In Nehrim itself MQ00 is Start-Game-Enabled, but that flag only
  ; fires for Nehrim's OWN new game, so start it explicitly here.
  ;
  ; Its stage is deliberately NOT set: Nehrim's player script (GlobalplayerScript)
  ; polls `If StartQuest == 0 -> MQ00.SetStage(1)` and drives the opening from
  ; there, so forcing a stage would race that script rather than help it.
  Quest mq00 = GetQuestFrom(NehrimMainQuestID, NehrimPlugin)
  If mq00 != None && !mq00.IsRunning()
    mq00.Start()
  EndIf
EndFunction

Function BeginMorroblivion()
  HandOff(GetQuestFrom(MorroChargenID, MorroblivionPlugin), \
          MorroChargenStage, \
          GetRefFrom(MorroStartMarkerID, MorroblivionPlugin))
EndFunction

Quest Function GetQuestFrom(Int formID, String plugin)
  Return Game.GetFormFromFile(formID, plugin) as Quest
EndFunction

ObjectReference Function GetRefFrom(Int formID, String plugin)
  Return Game.GetFormFromFile(formID, plugin) as ObjectReference
EndFunction

Function HandOff(Quest chargen, Int stage, ObjectReference marker)
  If chargen == None
    Debug.Trace("[TESGameSelect] chargen quest missing; staying in Skyrim")
    Return
  EndIf

  StopSkyrimOpening()

  ; Move first so the destination cell loads while the quest spins up.  The
  ; chargen stage script moves the player to the same marker itself; a
  ; redundant MoveTo is harmless, while a missing one would strand the player
  ; in Helgen watching a quest run somewhere else.
  If marker != None
    Game.GetPlayer().MoveTo(marker)
  EndIf

  If !chargen.IsRunning()
    chargen.Start()
  EndIf
  chargen.SetStage(stage)
EndFunction

Function StopSkyrimOpening()
  ; Leaving MQ101 running while the player stands in another game's start cell
  ; keeps its packages chasing the player and its controls-disabled state
  ; latched on.
  Quest mq101 = Game.GetFormFromFile(SkyrimOpeningID, "Skyrim.esm") as Quest
  If mq101 != None && mq101.IsRunning()
    mq101.Stop()
  EndIf

  ; Clear the opening's latched state so the destination game starts from a
  ; clean slate.  This runs BEFORE the chargen stage is set, which matters:
  ; Oblivion's stage 5 and Morroblivion's stage 1 both call
  ; DisablePlayerControls themselves as part of their own scripted intro, so
  ; enabling afterwards would undo the intro the player just chose.
  Game.EnablePlayerControls()
  Game.SetInChargen(false, false, false)
EndFunction
