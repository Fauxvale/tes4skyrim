#!/usr/bin/env python3
"""Instrument the converted CharacterGen scripts with exhaustive SKSE logging.

Writes ONE log the whole intro can be diagnosed from:
    Documents/My Games/Skyrim Special Edition/Logs/Script/User/TES4CharGen.log
(requires [Papyrus] bEnableLogging=1, bEnableTrace=1 in SkyrimCustom.ini)

Re-run after any `convert.py --scripts-only`, which regenerates the scripts and
drops the instrumentation. `--revert` restores clean output.

What it captures, so a single playthrough answers every question:
  QUEST tick     stage / convTimer / IsRunning, every change  (the quest script
                 owns the countdown AND every stage transition)
  QUEST STAGE    every SetStage the quest fragments run, with the stage number
  <SPEAKER>      per-actor view of speaker/target/convCount/convTimer
  <SPEAKER> FIRE every dispatch, with the count and target it dispatched on
  <SPEAKER> PKG  current package + AI state whenever it changes (force-greets
                 depend on the package winning, which no state dump shows)
  FRAG <fid>     every INFO End fragment, with the counter it saw and whether
                 its sequence gate accepted it
  GATE           stage-16 -> 17 exit test evaluated term by term

Usage:
    python tools/chargen_debug.py            # instrument + compile
    python tools/chargen_debug.py --revert
    python tools/chargen_debug.py --no-compile
"""
import argparse
import os
import re
import subprocess
import sys

SRC = os.path.join('output', 'Oblivion.esm', 'scripts', 'source')
OUT = os.path.join('output', 'Oblivion.esm', 'scripts')
LOG = 'TES4CharGen'
QUEST_SCRIPT = 'TES4_CharGenQuest'
QUEST_FRAGMENTS = 'TES4_QF_Charactergen'

# Stage 18: Renault must run CGRenoteOpenSecretDoor (an Activate package aimed
# at CGPrisonWallSwitchRef).  The switch's OWN script is what opens the door --
# it sets charactergen.secretDoor = 1, gated on `isActionRef RenoteRef`, and the
# quest script needs `stage 18 && secretDoor == 1 && convTimer <= 0` to reach
# stage 19.  So the door failing means the ACTIVATION never happened.
#
# Everything about the PACK record has been verified against vanilla
# (condition form, PTDA shape, template/UNAM/XNAM, interrupt flags), so the
# remaining unknowns are runtime-only.  These probes answer them directly.
SWITCH_SCRIPT = 'TES4_CGPrisonSecretWallSwitchSCRIPT'

# The four actor scripts that poll the shared conversation timer.
SPEAKERS = {
    'TES4_CGRenoteScript': 'RENAULT',
    'TES4_CGGlenroyScript': 'GLENROY',
    'TES4_BaurusScript': 'BAURUS',
    'TES4_CGEmperorScript': 'URIEL',
}

# Stage 22: the Ambush A assassins.  Their packages are gated GetStage >= 23,
# EXCEPT CGAssassinsAmbushA4 (>= 22) -- so A4 is the designed starter: stage 22
# wakes all four, only A4's package can pass, and A4's OnPackageEnd is what sets
# stage 23 and releases A1/A2/A3.  The whole ambush therefore hinges on A4
# completing its travel package.  These probes show, per assassin: whether the
# ref is enabled/3d-loaded at all (02/03/04 are Initially Disabled and rely on
# an XESP enable-parent chain off A1), which package it holds, and whether it
# ever reaches OnPackageEnd.
ASSASSINS = {
    'TES4_CGMythicDawnAmbushA1Script': 'ASSASSIN1',
    'TES4_CGMythicDawnAmbushA2Script': 'ASSASSIN2',
    'TES4_CGMythicDawnAmbushA3Script': 'ASSASSIN3',
    'TES4_CGMythicDawnAmbushA4Script': 'ASSASSIN4',
}

# The rest of CharacterGen's cast: the Ambush B / Ambush C assassins and the
# generic Mythic Dawn actors.  Same probe as ASSASSINS (enable + package +
# package events), so any actor that fails to wake is visible without having to
# extend this tool again.
OTHER_ACTORS = {
    'TES4_CGMythicDawnAmbushB1Script': 'ASSASSIN_B1',
    'TES4_CGMythicDawnAmbushB2Script': 'ASSASSIN_B2',
    'TES4_CGAssassinAmbushCScript': 'ASSASSIN_C',
    'TES4_CGAssassinScript': 'ASSASSIN_FINAL',
    'TES4_CGMythicDawnAssassinGenericScript': 'ASSASSIN_GEN',
}

# Trigger zones, doors and gates that gate stage progression.  Each gets an
# entry/activation trace so a stage that never advances can be traced to the
# trigger that never fired rather than guessed at.
TRIGGER_SCRIPTS = [
    'TES4_CGTrigZoneAmbushBSCRIPT',
    'TES4_CGTrigZoneEmperorBirthsignSCRIPT',
    'TES4_CGTriggerZoneCellScript',
    'TES4_CGTrigZone01SCRIPT',
    'TES4_CGTrigZoneACTORSCRIPT',
    'TES4_CGAmbushAGateScript',
    'TES4_CGAmbushCGateScript',
    'TES4_CGAmbushCBackGateScript',
]

# INFO fragments: the walk-down + cell-door lines (convCount 0..13), Uriel's
# CharGenVoice reaction (32B11) and the force-greet lines that follow it.
FRAGMENT_FIDS = [
    '00032B03', '00032B04', '00032B05', '00032B06', '00032B07',
    '00032B08', '00032B09', '00032B0A', '00032B0B', '00032B0C',
    '00032B0D', '00032B0E', '00032B0F', '00032B10', '00032B11',
    '00032B12', '00032B13', '0004D84C',
]

_STATE_DECL = '''
; --- TEMP DIAGNOSTIC state (tools/chargen_debug.py) ---
Bool _dbgOpen = False
String _dbgLast = ""
String _dbgPkg = ""
String _dbgR18 = ""
'''

# The switch reference the stage-18 Activate package targets
# (CGPrisonWallSwitchRef, TES4 0004E90D -> converted 0x0104E90D).  Renault's
# script does not reference it and a debug-only PROPERTY could not be filled
# (that needs a VMAD edit), so the probe resolves it by FormID at runtime.
SWITCH_FID = '0x0104E90D'
ESM_NAME = 'Oblivion.esm'


def _open_block(indent='  '):
    return (f'{indent}If !_dbgOpen\n'
            f'{indent}  _dbgOpen = Debug.OpenUserLog("{LOG}")\n'
            f'{indent}EndIf\n')


def _speaker_block(tag, quest):
    """State + package/AI dump for one polling actor script."""
    return f'''
  ; --- TEMP DIAGNOSTIC (tools/chargen_debug.py) ---
{_open_block()}  String _s = "st=" + {quest}.GetStage() + " spk=" + {quest}.speaker + " tgt=" + {quest}.target + " cnt=" + {quest}.convCount + " tmr=" + {quest}.convTimer
  If _s != _dbgLast
    _dbgLast = _s
    Debug.TraceUser("{LOG}", "{tag} " + _s)
  EndIf
  ; Package / AI state — a force-greet only happens if the package WINS.
  Package _cp = Self.GetCurrentPackage()
  String _p = ""
  If _cp
    _p = _cp as String
  EndIf
  String _pk = _p + " 3d=" + Self.Is3DLoaded() + " dead=" + Self.IsDead() + " combat=" + Self.IsInCombat() + " weap=" + Self.IsWeaponDrawn() + " dlg=" + Self.IsInDialogueWithPlayer() + " dist=" + (Self.GetDistance(Game.GetPlayer()) as Int)
  If _pk != _dbgPkg
    _dbgPkg = _pk
    Debug.TraceUser("{LOG}", "{tag} PKG " + _pk)
  EndIf
'''


def _renault_probe_block(quest):
    """Stage-18 probe: WHY the Activate package is not running.

    GetCurrentPackage() only names the WINNER, so it cannot distinguish
    "the engine never considered CGRenoteOpenSecretDoor" from "it considered
    it and rejected it".  These read the inputs the engine itself uses:

      OWNS   is the package even in this actor's list (property bound at all)?
      COND   does its GetStage condition pass RIGHT NOW (IsRunning + stage)?
      SWITCH where is the switch, and can she path to it?  An Activate package
             that cannot reach its target is dropped silently, which looks
             exactly like the package never being offered.
      DOOR   did the activation land?  secretDoor is the flag the switch script
             sets and the quest script waits on.
    """
    return f'''
  ; --- TEMP DIAGNOSTIC stage-18 probe (tools/chargen_debug.py) ---
  If {quest}.GetStage() == 18
    String _r18 = "st18 secretDoor=" + {quest}.secretDoor + " tmr=" + {quest}.convTimer
    If CGRenoteOpenSecretDoor
      _r18 = _r18 + " ownsPkg=YES"
    Else
      _r18 = _r18 + " ownsPkg=NO(unbound)"
    EndIf
    ObjectReference _sw = Game.GetFormFromFile({SWITCH_FID}, "{ESM_NAME}") as ObjectReference
    If _sw
      _r18 = _r18 + " switch3d=" + _sw.Is3DLoaded() + " switchDist=" + (Self.GetDistance(_sw) as Int) + " switchEnabled=" + _sw.IsEnabled()
    Else
      _r18 = _r18 + " switchRef=NULL"
    EndIf
    Package _cur = Self.GetCurrentPackage()
    If _cur
      _r18 = _r18 + " cur=" + _cur
    Else
      _r18 = _r18 + " cur=NONE"
    EndIf
    If _r18 != _dbgR18
      _dbgR18 = _r18
      Debug.TraceUser("{LOG}", "RENAULT " + _r18)
    EndIf
  EndIf
'''


# Every package the engine STARTS, not just the one GetCurrentPackage() reports.
# GetCurrentPackage() is sampled on our 0.1s poll, so a package the engine tries
# and abandons within one tick is invisible to it — exactly what would happen if
# CGRenoteOpenSecretDoor is selected and then immediately dropped (unreachable
# target, failed procedure).  OnPackageStart/Change/End are pushed by the engine
# and cannot be missed, so they separate:
#   start(04D84D) then end        -> the package RUNS but its procedure fails
#   no start(04D84D) ever         -> arbitration never picks it
_PKG_EVENT_BLOCK = '''
Event OnPackageStart(Package akNewPackage)
  ; chargen_debug.py
  If !_dbgOpen
    _dbgOpen = Debug.OpenUserLog("{LOG}")
  EndIf
  Debug.TraceUser("{LOG}", "{tag} PKGSTART " + akNewPackage)
EndEvent

Event OnPackageChange(Package akOldPackage)
  ; chargen_debug.py
  If !_dbgOpen
    _dbgOpen = Debug.OpenUserLog("{LOG}")
  EndIf
  Debug.TraceUser("{LOG}", "{tag} PKGCHANGE from " + akOldPackage + " to " + Self.GetCurrentPackage())
EndEvent
'''


_SWITCH_BLOCK = f'''
  ; --- TEMP DIAGNOSTIC (tools/chargen_debug.py) ---
  ; Fires on ANY activation, before the isActionRef gate, so a Renault
  ; activation that is rejected by the gate is still visible.
  If !_dbgOpen
    _dbgOpen = Debug.OpenUserLog("{LOG}")
  EndIf
  Debug.TraceUser("{LOG}", "SWITCH activated by " + akActionRef + " stage=" + charactergen.GetStage())
'''


def _quest_block():
    """Quest script: the countdown owner and every stage transition."""
    return f'''
  ; --- TEMP DIAGNOSTIC (tools/chargen_debug.py) ---
{_open_block()}  String _s = "QUEST tick st=" + GetStage() + " tmr=" + convTimer + " running=" + IsRunning() + " spk=" + speaker + " cnt=" + convCount
  If _s != _dbgLast
    _dbgLast = _s
    Debug.TraceUser("{LOG}", _s)
  EndIf
  ; The stage-16 -> 17 exit, term by term. This is the gate that must open for
  ; Uriel's force-greet package (CGEmperorGreetPlayerInCell, GetStage == 17).
  If GetStage() == 16
    Debug.TraceUser("{LOG}", "GATE st16 tmr=" + convTimer + " tmrOK=" + (convTimer <= 0) + " -> SetStage(17) " + (GetStage() == 16 && convTimer <= 0))
  EndIf
'''


def instrument_speaker(path, tag):
    src = open(path, encoding='utf-8').read()
    if 'chargen_debug.py' in src:
        return False
    # Take the quest property's EXACT spelling from the dispatcher guard -- the
    # converter preserves the TES4 source's casing, which differs per script
    # (`Charactergen` vs `CharacterGen`), and a mismatched name silently fails
    # to match (Uriel logged no FIRE lines because of this).
    m = re.search(r'If (\w+)\.speaker == \d+ && \1\.convTimer <= 0', src)
    if not m:
        return False
    quest = m.group(1)
    src = re.sub(r'(\{Converted from TES4:[^}]*\}\n)', r'\1' + _STATE_DECL,
                 src, count=1)
    block = _speaker_block(tag, quest)
    if tag == 'RENAULT':
        block += _renault_probe_block(quest)
    src = src.replace('Event OnUpdate()\n',
                      'Event OnUpdate()\n' + block, 1)
    # log every dispatch, right where the guard opens
    src = re.sub(
        r'(If ' + re.escape(quest) + r'\.speaker == \d+ && '
        + re.escape(quest) + r'\.convTimer <= 0\n\s*target = '
        + re.escape(quest) + r'\.target\n)',
        r'\1  Debug.TraceUser("' + LOG + '", "' + tag
        + ' FIRE cnt=" + ' + quest + '.convCount + " tgt=" + target)\n',
        src, count=1)
    # log package-end events (they drive the stage re-seeds)
    src = re.sub(
        r'(Event OnPackageEnd\(Package akOldPackage\)\n)',
        r'\1  Debug.TraceUser("' + LOG + '", "' + tag
        + ' PKGEND " + akOldPackage)\n', src, count=1)
    # Engine-pushed package events. Only add the ones the converted script does
    # not already define — a duplicate Event is a compile error.
    events = _PKG_EVENT_BLOCK.format(LOG=LOG, tag=tag)
    if 'Event OnPackageStart(' in src:
        events = re.sub(r'Event OnPackageStart\(.*?EndEvent\n', '', events,
                        flags=re.S)
    if 'Event OnPackageChange(' in src:
        events = re.sub(r'Event OnPackageChange\(.*?EndEvent\n', '', events,
                        flags=re.S)
    src += events
    open(path, 'w', encoding='utf-8').write(src)
    return True


def _assassin_block(tag, quest):
    """Per-tick state for one Ambush A assassin.

    Unlike the speaker scripts these have no OnUpdate loop of their own, so the
    probe adds one (OnInit -> RegisterForSingleUpdate, re-armed each tick).
    IsEnabled/Is3DLoaded are the first question: 02/03/04 ship Initially
    Disabled and are enabled only through the XESP parent chain off A1, so a
    disabled assassin never runs a package no matter what the package says.
    """
    return f'''
Event OnInit()
  If !_dbgOpen
    _dbgOpen = Debug.OpenUserLog("{LOG}")
  EndIf
  RegisterForSingleUpdate(1.0)
EndEvent

Event OnUpdate()
{_open_block()}  Package _cp = Self.GetCurrentPackage()
  String _p = ""
  If _cp
    _p = _cp as String
  EndIf
  String _s = "st=" + {quest}.GetStage() + " en=" + Self.IsEnabled() + " 3d=" + Self.Is3DLoaded() + " dead=" + Self.IsDead() + " combat=" + Self.IsInCombat() + " pkg=" + _p
  If _s != _dbgLast
    _dbgLast = _s
    Debug.TraceUser("{LOG}", "{tag} " + _s)
  EndIf
  ; MOVEMENT probe.  The state line above only prints on CHANGE, so an actor
  ; that holds one package forever prints once and looks identical whether it
  ; is walking, stuck against a wall, or standing still.  Position each tick is
  ; what distinguishes "travelling but slow" from "not moving at all", and
  ; distance-to-target says whether it is getting closer or has given up.
  ; Logged unconditionally while the quest is in the ambush window.
  Int _st = {quest}.GetStage()
  If _st >= 20 && _st < 30 && Self.Is3DLoaded()
    Float _x = Self.GetPositionX()
    Float _y = Self.GetPositionY()
    Float _z = Self.GetPositionZ()
    Debug.TraceUser("{LOG}", "{tag} POS " + (_x as Int) + "," + (_y as Int) + "," + (_z as Int) + " pkg=" + _p + " sitting=" + Self.GetSitState() + " move=" + Self.GetActorValue("SpeedMult") + " dist2tgt=" + (Self.GetDistance(Game.GetPlayer()) as Int))
  EndIf
  RegisterForSingleUpdate(1.0)
EndEvent

Event OnPackageStart(Package akNewPackage)
  Debug.TraceUser("{LOG}", "{tag} PKGSTART " + akNewPackage)
EndEvent

Event OnPackageChange(Package akOldPackage)
  Debug.TraceUser("{LOG}", "{tag} PKGCHANGE from " + akOldPackage + " to " + Self.GetCurrentPackage())
EndEvent
'''


def instrument_assassin(path, tag):
    """Add enable/package probes to an Ambush A assassin script.

    The A4 script's OnPackageEnd is the linchpin -- it sets stage 23, which is
    what releases the other three -- so that handler is logged unconditionally,
    before its `akOldPackage == CGAssassinsAmbushA4` guard, to distinguish "the
    event never fired" from "it fired but the guard rejected the package".
    """
    src = open(path, encoding='utf-8').read()
    if 'chargen_debug.py' in src:
        return False
    m = re.search(r'(\w+) Property (Charactergen|CharacterGen) Auto', src,
                  re.I)
    quest = m.group(2) if m else 'Charactergen'
    src = re.sub(r'(\{Converted from TES4:[^}]*\}\n)', r'\1' + _STATE_DECL,
                 src, count=1)
    # log the package-end event before its guard, so a mismatch is visible
    src = re.sub(
        r'(Event OnPackageEnd\(Package akOldPackage\)\n)',
        r'\1  Debug.TraceUser("' + LOG + '", "' + tag
        + ' PKGEND " + akOldPackage)\n', src, count=1)
    src += _assassin_block(tag, quest)
    open(path, 'w', encoding='utf-8').write(src)
    return True


def instrument_actor(path, tag):
    """Enable/package probe for a CharacterGen actor script.

    Same shape as instrument_assassin, but tolerant of scripts that already
    define OnUpdate / the package events (a duplicate Event is a compile
    error, and this set is more varied than the four Ambush A scripts).
    """
    src = open(path, encoding='utf-8').read()
    if 'chargen_debug.py' in src:
        return False
    if not re.match(r'ScriptName \w+ extends (Actor|ObjectReference)', src):
        return False
    m = re.search(r'(\w+) Property (Charactergen|CharacterGen) Auto', src,
                  re.I)
    quest = m.group(2) if m else None
    src = re.sub(r'(\{Converted from TES4:[^}]*\}\n)', r'\1' + _STATE_DECL,
                 src, count=1)
    src = re.sub(
        r'(Event OnPackageEnd\(Package akOldPackage\)\n)',
        r'\1  Debug.TraceUser("' + LOG + '", "' + tag
        + ' PKGEND " + akOldPackage)\n', src, count=1)
    block = _assassin_block(tag, quest) if quest else ''
    if not block:
        return False
    # Drop any handler the converted script already defines.
    for ev in ('OnInit', 'OnUpdate', 'OnPackageStart', 'OnPackageChange'):
        if re.search(r'Event ' + ev + r'\(', src):
            block = re.sub(r'Event ' + ev + r'\(.*?EndEvent\n', '', block,
                           flags=re.S)
    src += block
    open(path, 'w', encoding='utf-8').write(src)
    return True


def instrument_trigger(path, tag):
    """Trace every OnTrigger / OnActivate on a gating trigger zone or door.

    Logged before the script's own guards so a trigger that fires but is
    rejected by a stage check is distinguishable from one that never fires.
    """
    src = open(path, encoding='utf-8').read()
    if 'chargen_debug.py' in src:
        return False
    hit = False
    for ev, arg in (('OnTriggerEnter', 'akActionRef'),
                    ('OnTrigger', 'akActionRef'),
                    ('OnActivate', 'akActionRef')):
        pat = r'(Event ' + ev + r'\(ObjectReference ' + arg + r'\)\n)'
        if re.search(pat, src):
            src = re.sub(pat, r'\1  Debug.TraceUser("' + LOG + '", "'
                         + tag + ' ' + ev + ' by " + ' + arg + ')\n',
                         src, count=1)
            hit = True
    if not hit:
        return False
    open(path, 'w', encoding='utf-8').write(src)
    return True


def instrument_quest(path):
    src = open(path, encoding='utf-8').read()
    if 'chargen_debug.py' in src:
        return False
    src = re.sub(r'(\{Converted from TES4:[^}]*\}\n)', r'\1' + _STATE_DECL,
                 src, count=1)
    src = src.replace('Event OnUpdate()\n',
                      'Event OnUpdate()\n' + _quest_block(), 1)
    open(path, 'w', encoding='utf-8').write(src)
    return True


def instrument_switch(path):
    """Log EVERY activation of the secret-wall switch.

    The switch script's own `isActionRef RenoteRef` gate decides whether the
    door opens, so logging before it separates "Renault never activated it"
    from "she did, but the gate rejected her".
    """
    src = open(path, encoding='utf-8').read()
    if 'chargen_debug.py' in src:
        return False
    src = re.sub(r'(\{Converted from TES4:[^}]*\}\n)', r'\1' + _STATE_DECL,
                 src, count=1)
    src = src.replace(
        'Event OnActivate(ObjectReference akActionRef)\n',
        'Event OnActivate(ObjectReference akActionRef)\n' + _SWITCH_BLOCK, 1)
    open(path, 'w', encoding='utf-8').write(src)
    return True


def instrument_quest_fragments(path):
    """Log every quest-stage fragment: these re-seed the conversation."""
    src = open(path, encoding='utf-8').read()
    if 'chargen_debug.py' in src:
        return False
    def _add(m):
        stage = m.group(2)
        return (f'{m.group(0)}'
                f'  ; chargen_debug.py\n'
                f'  Debug.TraceUser("{LOG}", "QUEST STAGE {stage} frag")\n')
    src = re.sub(r'(Function Fragment_Stage_(\d+)_Item_\d+\(\)\n)', _add, src)
    open(path, 'w', encoding='utf-8').write(src)
    return True


def instrument_fragment(path, fid):
    src = open(path, encoding='utf-8').read()
    if 'chargen_debug.py' in src:
        return False
    quest = 'CharacterGen' if 'CharacterGen' in src else 'Charactergen'
    # Report the counter AND whether this fragment's sequence gate accepted it.
    gate = re.search(r'If (\w+\.\w+) == (\d+)  ; still this line', src)
    if gate:
        note = (f'  Debug.TraceUser("{LOG}", "FRAG {fid} cnt=" + {gate.group(1)}'
                f' + " needs {gate.group(2)} accepted=" + ({gate.group(1)} == {gate.group(2)}))')
    else:
        note = (f'  Debug.TraceUser("{LOG}", "FRAG {fid} cnt=" + '
                f'{quest}.convCount + " (ungated)")')
    src = src.replace(
        'Function Fragment_0(ObjectReference akSpeakerRef)',
        'Function Fragment_0(ObjectReference akSpeakerRef)\n'
        '  ; tools/chargen_debug.py\n' + note, 1)
    open(path, 'w', encoding='utf-8').write(src)
    return True


def compile_one(stem, headers):
    exe = os.path.join('external', 'papyrus-compiler', 'papyrus.exe')
    cmd = [exe, 'compile', '-nocache',
           '-i', os.path.join(SRC, stem + '.psc'),
           '-o', OUT, '-h', headers, '-h', SRC]
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = r.stdout + r.stderr
    ok = r.returncode == 0 and 'error' not in out.lower()
    if not ok:
        for line in out.splitlines():
            if 'error' in line.lower():
                print(f'    {line.strip()}')
    return ok


def find_headers():
    p = (r'C:\Program Files (x86)\Steam\steamapps\common'
         r'\Skyrim Special Edition\Data\Source\Scripts')
    return p if os.path.isdir(p) else ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-compile', action='store_true')
    ap.add_argument('--revert', action='store_true',
                    help='re-run convert.py --scripts-only for clean output')
    args = ap.parse_args()

    if args.revert:
        return subprocess.call([sys.executable, 'convert.py',
                                '--scripts-only', '-f', 'Oblivion.esm'])

    headers = find_headers()
    if not headers and not args.no_compile:
        print('ERROR: Skyrim Papyrus headers not found')
        return 1

    touched = []
    for stem, tag in SPEAKERS.items():
        path = os.path.join(SRC, stem + '.psc')
        if os.path.isfile(path) and instrument_speaker(path, tag):
            touched.append(stem)
            print(f'  instrumented {stem} [{tag}]')

    for stem, tag in ASSASSINS.items():
        path = os.path.join(SRC, stem + '.psc')
        if os.path.isfile(path) and instrument_assassin(path, tag):
            touched.append(stem)
            print(f'  instrumented {stem} [{tag}]')

    for stem, tag in OTHER_ACTORS.items():
        path = os.path.join(SRC, stem + '.psc')
        if os.path.isfile(path) and instrument_actor(path, tag):
            touched.append(stem)
            print(f'  instrumented {stem} [{tag}]')

    for stem in TRIGGER_SCRIPTS:
        path = os.path.join(SRC, stem + '.psc')
        tag = stem.replace('TES4_CG', '').replace('SCRIPT', '') \
                  .replace('Script', '').upper()
        if os.path.isfile(path) and instrument_trigger(path, tag):
            touched.append(stem)
            print(f'  instrumented {stem} [{tag}]')

    for stem, fn in ((QUEST_SCRIPT, instrument_quest),
                     (QUEST_FRAGMENTS, instrument_quest_fragments),
                     (SWITCH_SCRIPT, instrument_switch)):
        path = os.path.join(SRC, stem + '.psc')
        if os.path.isfile(path) and fn(path):
            touched.append(stem)
            print(f'  instrumented {stem}')

    for fid in FRAGMENT_FIDS:
        stem = f'TES4_TIF__{fid}'
        path = os.path.join(SRC, stem + '.psc')
        if os.path.isfile(path) and instrument_fragment(path, fid):
            touched.append(stem)

    print(f'\n{len(touched)} script(s) instrumented')
    if args.no_compile:
        return 0

    print('Compiling...')
    bad = [s for s in touched if not compile_one(s, headers)]
    print(f'  {len(touched) - len(bad)}/{len(touched)} compiled')
    if bad:
        print('  FAILED: ' + ', '.join(bad))
        return 1
    print(f'\nLog: Documents/My Games/Skyrim Special Edition/'
          f'Logs/Script/User/{LOG}.log')
    return 0


if __name__ == '__main__':
    sys.exit(main())
