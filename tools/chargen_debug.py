#!/usr/bin/env python3
"""Instrument the converted CharacterGen conversation scripts with SKSE logging.

Writes to Documents/My Games/Skyrim Special Edition/Logs/Script/User/TES4CharGen.log
(Debug.OpenUserLog/TraceUser, SKSE not required for these two — they are vanilla
Papyrus). Re-run after any `convert.py --scripts-only`, which regenerates the
scripts and drops the instrumentation.

The CharacterGen prison-cell conversation is driven by ONE shared quest timer
(CharacterGen.convTimer) plus `speaker`/`target`/`convCount`, polled by four
separate actor scripts. This logs every state CHANGE plus every dispatch and
every End fragment, so a stalled exchange can be read off a single file.

Usage:
    python tools/chargen_debug.py                 # instrument + compile
    python tools/chargen_debug.py --revert        # restore from convert.py output
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

# The four actor scripts that poll the shared conversation timer.
SPEAKERS = {
    'TES4_CGRenoteScript': ('RENAULT', 'Charactergen'),
    'TES4_CGGlenroyScript': ('GLENROY', 'Charactergen'),
    'TES4_BaurusScript': ('BAURUS', 'Charactergen'),
    'TES4_CGEmperorScript': ('URIEL', 'Charactergen'),
}

# INFO fragments for the walk-down + cell-door lines (convCount 0..11).
FRAGMENT_FIDS = [
    '00032B03', '00032B04', '00032B05', '00032B06', '00032B07',
    '00032B08', '00032B09', '00032B0A', '00032B0B', '00032B0C',
    '00032B0D', '00032B0E',
]

_STATE_DECL = '''
; --- TEMP DIAGNOSTIC state (tools/chargen_debug.py) ---
Bool _dbgOpen = False
String _dbgLast = ""
'''


def _state_block(tag, quest):
    return f'''
  ; --- TEMP DIAGNOSTIC (tools/chargen_debug.py) ---
  If {quest}.GetStage() >= 6 && {quest}.GetStage() <= 16
    If !_dbgOpen
      _dbgOpen = Debug.OpenUserLog("{LOG}")
    EndIf
    String _s = "st=" + {quest}.GetStage() + " spk=" + {quest}.speaker + " tgt=" + {quest}.target + " cnt=" + {quest}.convCount + " tmr=" + {quest}.convTimer
    If _s != _dbgLast
      _dbgLast = _s
      Debug.TraceUser("{LOG}", "{tag} " + _s)
    EndIf
  EndIf
'''


def instrument_speaker(path, tag, quest):
    src = open(path, encoding='utf-8').read()
    if 'chargen_debug.py' in src:
        return False
    # Take the quest property's EXACT spelling from the dispatcher guard -- the
    # converter preserves the TES4 source's casing, which differs per script
    # (`Charactergen` vs `CharacterGen`), and a mismatched name silently fails
    # to match the regex below (Uriel logged no FIRE lines because of this).
    m = re.search(r'If (\w+)\.speaker == \d+ && \1\.convTimer <= 0', src)
    if m:
        quest = m.group(1)
    # state vars after the ScriptName/doc header
    src = re.sub(r'(\{Converted from TES4:[^}]*\}\n)', r'\1' + _STATE_DECL,
                 src, count=1)
    # state dump at the top of OnUpdate
    src = src.replace('Event OnUpdate()\n',
                      'Event OnUpdate()\n' + _state_block(tag, quest), 1)
    # log every dispatch, right where the guard opens
    src = re.sub(
        r'(If ' + re.escape(quest) + r'\.speaker == \d+ && '
        + re.escape(quest) + r'\.convTimer <= 0\n\s*target = '
        + re.escape(quest) + r'\.target\n)',
        r'\1  Debug.TraceUser("' + LOG + '", "' + tag
        + ' FIRE cnt=" + ' + quest + '.convCount + " tgt=" + target)\n',
        src, count=1)
    open(path, 'w', encoding='utf-8').write(src)
    return True


def instrument_fragment(path, fid):
    src = open(path, encoding='utf-8').read()
    if 'chargen_debug.py' in src:
        return False
    quest = 'CharacterGen' if 'CharacterGen' in src else 'Charactergen'
    src = src.replace(
        'Function Fragment_0(ObjectReference akSpeakerRef)',
        'Function Fragment_0(ObjectReference akSpeakerRef)\n'
        f'  ; tools/chargen_debug.py\n'
        f'  Debug.TraceUser("{LOG}", "FRAG {fid} cnt=" + {quest}.convCount'
        f' + " -> spk/tgt set below")', 1)
    open(path, 'w', encoding='utf-8').write(src)
    return True


def compile_one(stem, headers):
    exe = os.path.join('external', 'papyrus-compiler', 'papyrus.exe')
    cmd = [exe, 'compile', '-nocache',
           '-i', os.path.join(SRC, stem + '.psc'),
           '-o', OUT, '-h', headers, '-h', SRC]
    r = subprocess.run(cmd, capture_output=True, text=True)
    ok = r.returncode == 0 and 'error' not in (r.stdout + r.stderr).lower()
    if not ok:
        for line in (r.stdout + r.stderr).splitlines():
            if 'error' in line.lower():
                print(f'    {line.strip()}')
    return ok


def find_headers():
    for p in (r'C:\Program Files (x86)\Steam\steamapps\common'
              r'\Skyrim Special Edition\Data\Source\Scripts',):
        if os.path.isdir(p):
            return p
    return ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-compile', action='store_true')
    ap.add_argument('--revert', action='store_true',
                    help='re-run convert.py --scripts-only to restore clean output')
    args = ap.parse_args()

    if args.revert:
        return subprocess.call([sys.executable, 'convert.py',
                                '--scripts-only', '-f', 'Oblivion.esm'])

    headers = find_headers()
    if not headers and not args.no_compile:
        print('ERROR: Skyrim Papyrus headers not found')
        return 1

    touched = []
    for stem, (tag, quest) in SPEAKERS.items():
        path = os.path.join(SRC, stem + '.psc')
        if not os.path.isfile(path):
            print(f'  skip (missing): {stem}')
            continue
        if instrument_speaker(path, tag, quest):
            touched.append(stem)
            print(f'  instrumented {stem} [{tag}]')

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
    print('(enable Papyrus logging in SkyrimCustom.ini: '
          '[Papyrus] bEnableLogging=1, bEnableTrace=1)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
