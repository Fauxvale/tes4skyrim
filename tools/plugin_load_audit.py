"""Audit converted plugins for the errors xEdit reports when it loads them.

Answers "will the game load this?" without launching xEdit. Every check here
reproduces a real failure seen on TWMP Valenwood/Elsweyr, where the engine hung
forever on the main menu with no crash and no log — the FormID table is built
while the plugin is PARSED, so a malformed record deadlocks before any cell
loads.

Checks, per plugin:
  1. structural: every GRUP/record size nests exactly, compressed bodies inflate
     to their declared size
  2. duplicate (signature, FormID) within one file
     -> xEdit "Skipped Load: Duplicate FormID [...]"
  3. illegal top-level groups (REFR/ACHR/LAND/NAVM only live under a cell)
     -> xEdit "File contains top level group without known sort order"
  4. subrecords that cannot belong to the record's own type
     -> xEdit "record X contains unexpected (or out of order) subrecord Y"
  5. REGN Region Areas: RPLI/RPLD must alternate (a repeating STRUCT, not two
     independent runs)
  6. LAND must start at DATA — no FULL/DESC/EDID spliced in front
  7. duplicate owned-groups: one worldspace/cell/topic may own at most ONE
     children group per file
     -> xEdit "Found additional GRUP World Children of ... Merged N elements"
  8. an ACHR's base must be an actor, never a leveled list (LVLN) — the engine
     loads it as a Character*, dereferences a null base and CRASHES on startup

Exit status is the number of problems, so it can gate a build.

Usage:
  python tools/plugin_load_audit.py TWMP_Valenwood_Elsweyr.esp
  python tools/plugin_load_audit.py Tamriel.esp ElsweyrAnequina.esp
  python tools/plugin_load_audit.py --output-dir output Oblivion.esm
"""
import argparse
import os
import struct
import sys
import zlib
from collections import Counter

# Fields that must never appear on these record types.
FORBIDDEN = {
    b'REFR': {b'ACBS', b'AIDT', b'VTCK', b'CNTO', b'COCT', b'PKID', b'TINI',
              b'TINC', b'TINV', b'TIAS', b'HCLF', b'DPLT', b'RNAM', b'NAM5',
              b'NAM6', b'NAM7', b'NAM8', b'NAM9', b'NAMA', b'QNAM', b'ZNAM',
              b'VNML', b'VHGT', b'VCLR', b'BTXT', b'ATXT', b'VTXT', b'DESC'},
    b'ACHR': {b'VNML', b'VHGT', b'VCLR', b'BTXT', b'ATXT', b'VTXT', b'DESC',
              b'CNTO', b'COCT'},
    b'LAND': {b'FULL', b'DESC', b'EDID', b'NAME', b'ACBS', b'AIDT'},
    b'CELL': {b'VNML', b'VHGT', b'VCLR', b'ACBS', b'AIDT', b'VTCK'},
}
ILLEGAL_TOP = {'REFR', 'ACHR', 'LAND', 'NAVM', 'PGRE', 'PHZD', 'PMIS', 'PARW'}
# GRUP types labelled with the FormID of the record that owns them. At most one
# of each may exist per owner per file.
OWNED_GROUP_TYPES = (1, 6, 7)


def audit(path, label):
    d = open(path, 'rb').read()
    problems = Counter()
    examples = {}
    seen = {}
    tops = []
    owned_groups = Counter()
    achr_bases = {}          # ACHR formid -> its NAME target
    local_sigs = {}          # formid -> signature, for the ACHR base check

    def note(kind, detail):
        problems[kind] += 1
        examples.setdefault(kind, detail)

    def subs(body):
        j = 0
        while j + 6 <= len(body):
            ssig = body[j:j + 4]
            z = struct.unpack_from('<H', body, j + 4)[0]
            yield ssig, body[j + 6:j + 6 + z]
            j += 6 + z

    def walk(i, end, depth=0):
        while i < end:
            if i + 24 > end:
                note('trailing-bytes', f'{end - i} bytes at 0x{i:X}')
                return
            sig = d[i:i + 4]
            size = struct.unpack_from('<I', d, i + 4)[0]
            if sig == b'GRUP':
                if size < 24 or i + size > end:
                    note('bad-grup-size', f'0x{i:X}')
                    return
                gt = struct.unpack_from('<I', d, i + 12)[0]
                lbl = d[i + 8:i + 12]
                if depth == 0 and gt == 0:
                    tops.append(lbl.decode('latin1'))
                if gt in OWNED_GROUP_TYPES:
                    owned_groups[(gt, bytes(lbl))] += 1
                walk(i + 24, i + size, depth + 1)
                i += size
                continue
            if not all(48 <= c <= 90 or c == 95 for c in sig):
                note('bad-signature', f'{sig!r} at 0x{i:X}')
                return
            if i + 24 + size > end:
                note('record-overruns', f'{sig!r} at 0x{i:X}')
                return
            flags = struct.unpack_from('<I', d, i + 8)[0]
            fid = struct.unpack_from('<I', d, i + 12)[0]
            body = d[i + 24:i + 24 + size]
            if flags & 0x00040000:
                try:
                    declared = struct.unpack_from('<I', body, 0)[0]
                    body = zlib.decompress(body[4:])
                    if len(body) != declared:
                        note('bad-decompressed-size', f'{fid:08X}')
                except Exception:
                    note('zlib-fail', f'{fid:08X}')
                    body = b''
            key = (sig, fid)
            if key in seen:
                note('duplicate-formid', f'{sig.decode()} {fid:08X}')
            seen[key] = True

            names = [s for s, _v in subs(body)]
            bad = FORBIDDEN.get(sig, set()) & set(names)
            if bad:
                note('alien-subrecord',
                     f'{sig.decode()} {fid:08X}: '
                     f'{sorted(x.decode() for x in bad)}')
            if sig == b'LAND' and names and names[0] != b'DATA':
                note('land-not-starting-at-DATA',
                     f'{fid:08X}: {names[0].decode()}')
            if sig == b'REGN':
                seq = [s for s in names if s in (b'RPLI', b'RPLD')]
                ok = len(seq) % 2 == 0 and all(
                    seq[k] == (b'RPLI' if k % 2 == 0 else b'RPLD')
                    for k in range(len(seq)))
                if not ok:
                    note('regn-area-misordered', f'{fid:08X}')
            local_sigs[fid] = sig
            if sig == b'ACHR':
                for ssig, val in subs(body):
                    if ssig == b'NAME' and len(val) == 4:
                        achr_bases[fid] = struct.unpack('<I', val)[0]
                        break
            i += 24 + size

    hs = struct.unpack_from('<I', d, 4)[0]
    walk(24 + hs, len(d))
    for t in tops:
        if t in ILLEGAL_TOP:
            note('illegal-top-level-group', t)
    for key, n in owned_groups.items():
        if n > 1:
            gt, lbl = key
            note('duplicate-owned-group',
                 f'type {gt} for {struct.unpack("<I", lbl)[0]:08X} x{n}')
    # An ACHR's base must be an actor. Only bases THIS file defines can be
    # checked here; a master's is resolved by the caller's own audit of it.
    for fid, base in achr_bases.items():
        bsig = local_sigs.get(base)
        if bsig is not None and bsig not in (b'NPC_',):
            note('achr-base-is-not-an-actor',
                 f'ACHR {fid:08X} -> {bsig.decode()} {base:08X}')

    print(f'--- {label}: {len(seen)} records, {len(tops)} top groups')
    if not problems:
        print('    CLEAN')
    for k, n in problems.most_common():
        print(f'    {k}: {n}   e.g. {examples[k]}')
    return sum(problems.values())


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('plugins', nargs='+',
                    help='Plugin names under the output dir (e.g. Tamriel.esp)')
    ap.add_argument('--output-dir', default=os.path.join(root, 'output'),
                    help='Root holding <plugin>/<plugin> (default: output/)')
    args = ap.parse_args()

    total = 0
    missing = 0
    for name in args.plugins:
        path = os.path.join(args.output_dir, name, name)
        if not os.path.isfile(path):
            print(f'--- {name}: NOT BUILT ({path})')
            missing += 1
            continue
        total += audit(path, name)
    print(f'\nTOTAL PROBLEMS: {total}')
    return min(total + missing, 125)


if __name__ == '__main__':
    sys.exit(main())
