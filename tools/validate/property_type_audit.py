#!/usr/bin/env python3
"""Flag converted `Actor Property` declarations that can never bind.

A Papyrus property typed `Actor` binds only to a reference whose BASE record is
an NPC_ or CREA.  Point one at an XMarker (STAT), a shrine (ACTI) or a door and
the VM refuses the bind ("cannot be bound because <fid> is not the right type"):
the property comes back **None** and the FIRST call on it aborts the enclosing
function.  Nothing is logged at conversion time, so the failure only shows up
in-game as a dead script.

This is how the Imperial City Arena softlock was found: the announcer speaks
through four XMarker STATs, all of which the `Say` handler had promoted to
`Actor` -- see docs/notes/papyrus_conversion.md.

    # audit a converted plugin
    python tools/validate/property_type_audit.py -f Oblivion.esm

    # audit an arbitrary directory of .psc (e.g. a subset rebuild)
    python tools/validate/property_type_audit.py -f Oblivion.esm --src temp/arena_fix

    # also report which TES4 calls named each bad ref, to find the promoter
    python tools/validate/property_type_audit.py -f Oblivion.esm --blame

Exit status is 1 when anything is flagged, so it works as a CI gate.
"""
import argparse
import glob
import os
import re
import sys

ACTOR_BASES = {'NPC_', 'CREA'}
# LVLC spawns an actor at runtime, so an Actor Property on one is correct.
RUNTIME_ACTOR_BASES = {'LVLC'}

_PROP_RE = re.compile(r'^Actor Property (\w+) Auto', re.M)


def _iter_records(path):
    """Yield each record in an export .txt as a dict of its scalar fields."""
    cur = {}
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line = line.rstrip('\n')
            if line == '---RECORD_BEGIN---':
                cur = {}
            elif line == '---RECORD_END---':
                if cur:
                    yield cur
                cur = {}
            elif '=' in line:
                k, _, v = line.partition('=')
                cur.setdefault(k, v)


def build_ref_map(export_dir):
    """(EditorID -> base FormID, base FormID -> record signature)."""
    edid_to_base = {}
    for sig in ('REFR', 'ACHR', 'ACRE'):
        path = os.path.join(export_dir, f'{sig}.txt')
        if not os.path.exists(path):
            continue
        for rec in _iter_records(path):
            if 'EditorID' in rec and 'NAME' in rec:
                edid_to_base[rec['EditorID'].lower()] = rec['NAME']

    base_type = {}
    for path in glob.glob(os.path.join(export_dir, '*.txt')):
        sig = os.path.basename(path)[:-4]
        if sig in ('REFR', 'ACHR', 'ACRE', 'CELL', 'LAND',
                   'INFO', 'DIAL', 'SCPT', 'QUST'):
            continue
        for rec in _iter_records(path):
            fid = rec.get('FormID')
            if fid:
                base_type[fid] = sig
    return edid_to_base, base_type


def blame(export_dir, names):
    """Which TES4 calls name each ref -- i.e. which handler promoted it."""
    if not names:
        return {}
    pat = re.compile(
        r'\b(' + '|'.join(re.escape(n) for n in names) + r')\s*\.\s*(\w+)',
        re.I)
    found = {}
    for sig in ('SCPT', 'INFO', 'QUST'):
        path = os.path.join(export_dir, f'{sig}.txt')
        if not os.path.exists(path):
            continue
        with open(path, encoding='utf-8', errors='replace') as fh:
            for m in pat.finditer(fh.read()):
                found.setdefault(m.group(1).lower(), set()).add(m.group(2).lower())
    return found


def main():
    ap = argparse.ArgumentParser(
        description='Flag Actor Property declarations bound to non-actor refs.')
    ap.add_argument('-f', '--plugin', default='Oblivion.esm',
                    help='plugin name, for locating export/ and output/')
    ap.add_argument('--src',
                    help='directory of .psc to audit '
                         '(default: output/<plugin>/scripts/source)')
    ap.add_argument('--export',
                    help='export directory (default: export/<plugin>)')
    ap.add_argument('--blame', action='store_true',
                    help='also list the TES4 calls that named each bad ref')
    args = ap.parse_args()

    export_dir = args.export or os.path.join('export', args.plugin)
    src = args.src or os.path.join('output', args.plugin, 'scripts', 'source')
    if not os.path.isdir(export_dir):
        sys.exit(f'no export directory: {export_dir}')
    if not os.path.isdir(src):
        sys.exit(f'no script source directory: {src}')

    print(f'export: {export_dir}\nsource: {src}')
    edid_to_base, base_type = build_ref_map(export_dir)
    print(f'  {len(edid_to_base)} placed refs, {len(base_type)} base records')

    bad = {}
    scanned = 0
    for path in glob.glob(os.path.join(src, '*.psc')):
        scanned += 1
        with open(path, encoding='utf-8', errors='replace') as fh:
            for name in _PROP_RE.findall(fh.read()):
                base = edid_to_base.get(name.lower())
                if base is None:
                    continue          # not a placed ref -- nothing to check
                sig = base_type.get(base, '?')
                if sig in ACTOR_BASES or sig in RUNTIME_ACTOR_BASES:
                    continue
                bad.setdefault((name, sig), []).append(os.path.basename(path))

    print(f'  scanned {scanned} scripts\n')
    if not bad:
        print('CLEAN: no Actor Property bound to a non-actor reference.')
        return 0

    blamed = blame(export_dir, [n for n, _ in bad]) if args.blame else {}
    print(f'{len(bad)} Actor Property declaration(s) that cannot bind:\n')
    for (name, sig), files in sorted(bad.items(), key=lambda kv: -len(kv[1])):
        print(f'  {name:34s} base={sig:5s} in {len(files)} script(s)')
        for f in sorted(files)[:5]:
            print(f'      {f}')
        if len(files) > 5:
            print(f'      ... and {len(files) - 5} more')
        if args.blame:
            calls = sorted(blamed.get(name.lower(), ()))
            if calls:
                print(f'      TES4 calls: {", ".join(calls)}')
    return 1


if __name__ == '__main__':
    sys.exit(main())
