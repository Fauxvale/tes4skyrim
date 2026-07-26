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

# The four actor scripts that poll the shared conversation timer.
SPEAKERS = {
    'TES4_CGRenoteScript': 'RENAULT',
    'TES4_CGGlenroyScript': 'GLENROY',
    'TES4_BaurusScript': 'BAURUS',
    'TES4_CGEmperorScript': 'URIEL',
}

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
'''


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
    src = src.replace('Event OnUpdate()\n',
                      'Event OnUpdate()\n' + _speaker_block(tag, quest), 1)
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

    for stem, fn in ((QUEST_SCRIPT, instrument_quest),
                     (QUEST_FRAGMENTS, instrument_quest_fragments)):
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
