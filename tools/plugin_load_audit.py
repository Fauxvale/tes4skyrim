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
  9. at most ONE LAND per cell. Two landscapes in one cell is unresolvable
     while the engine builds its form table, so the game hangs on the main
     menu with no crash and no log — and xEdit still calls the file clean.
 10. records that OVERRIDE a vanilla Skyrim.esm form. Our converted content is
     brand-new, so a form whose own FormID carries master index 00 silently
     replaces a Bethesda record and breaks vanilla Skyrim for the player. The
     one legal case is the Navmesh Info Map singleton 0x00012FB4, which the
     engine resolves by that fixed id and which Dawnguard/HearthFires/
     Dragonborn each override the same way.
 11. exterior block/sub-block groups ascend by UNSIGNED (X, Y), X major, with
     no duplicate label. The engine walks this list to build a worldspace's
     cell grid while PARSING the file, so a run where X descends and
     re-ascends never terminates: main-menu hang, no crash, no log, and xEdit
     reports the file as clean. The label packs Y in the LOW word, so sorting
     on its own word order gives the TRANSPOSE — which is what shipped and
     what kept TWMP_ValenwoodImproved from loading. Authority is the real
     Skyrim.esm (168/168 blocks of 0000003C, all 37 worldspaces); never
     census our own output, which carried the same bug.

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
# Vanilla Skyrim.esm forms our plugins are ALLOWED to override. The Navmesh
# Info Map is a singleton the engine looks up by this fixed FormID, so every
# file that ships navmesh must override it -- verified: all three vanilla DLC
# ESMs do exactly this. Anything else with master index 00 is an accident.
LEGAL_VANILLA_OVERRIDES = {(b'NAVI', 0x00012FB4)}
# GRUP types labelled with the FormID of the record that owns them. At most one
# of each may exist per owner per file.
OWNED_GROUP_TYPES = (1, 6, 7)


def plugin_masters(d):
    """Master filenames from the TES4 header, in load-order index order."""
    size = struct.unpack_from('<I', d, 4)[0]
    out = []
    j = 24
    while j + 6 <= 24 + size:
        ssig = d[j:j + 4]
        z = struct.unpack_from('<H', d, j + 4)[0]
        if ssig == b'MAST':
            out.append(d[j + 6:j + 6 + z].rstrip(b'\0').decode('cp1252'))
        j += 6 + z
    return out


def audit(path, label):
    d = open(path, 'rb').read()
    problems = Counter()
    examples = {}
    seen = {}
    tops = []
    owned_groups = Counter()
    achr_bases = {}          # ACHR formid -> its NAME target
    local_sigs = {}          # formid -> signature, for the ACHR base check
    cell_lands = {}          # cell formid -> the LAND inside it (max one)
    # (enclosing group id) -> [block labels in file order], for the grid sort
    grid_siblings = {}
    # Index of Skyrim.esm in this plugin's master list. A record whose own
    # FormID carries this index byte is an override of a Bethesda record.
    masters = plugin_masters(d)
    vanilla_idx = next((k for k, m in enumerate(masters)
                        if m.lower() == 'skyrim.esm'), None)

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

    def check_grid_order(siblings):
        """Block/sub-block groups must ascend by unsigned (X, Y), X major.

        The label is `pack('<hh', Y, X)`, so the engine's key is the SECOND
        word then the first. A descent mid-list, or a repeated label, stalls
        the parse-time grid walk -> main-menu hang with no crash and no log.
        """
        if len(siblings) < 2:
            return
        keys = [struct.unpack('<HH', lbl)[::-1] for lbl in siblings]
        if keys != sorted(keys):
            runs = 1 + sum(1 for a, b in zip(keys, keys[1:]) if b < a)
            def sgn(k):
                return tuple(struct.unpack('<h', struct.pack('<H', v))[0]
                             for v in k)
            note('grid-groups-out-of-order',
                 f'{len(keys)} groups in {runs} ascending runs, '
                 f'e.g. {[sgn(k) for k in keys[:6]]}')
        dupes = [k for k, n in Counter(keys).items() if n > 1]
        if dupes:
            note('duplicate-grid-group',
                 f'{len(dupes)} repeated labels, e.g. '
                 f'{[tuple(struct.unpack("<h", struct.pack("<H", v))[0] for v in k) for k in dupes[:4]]}')

    def walk(i, end, depth=0, cell=None):
        siblings = []     # type-4/5 labels at THIS nesting level, in file order
        while i < end:
            if i + 24 > end:
                note('trailing-bytes', f'{end - i} bytes at 0x{i:X}')
                check_grid_order(siblings)
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
                if gt in (4, 5) and len(lbl) == 4:
                    siblings.append(bytes(lbl))
                inner_cell = cell
                if gt in (6, 8, 9) and len(lbl) == 4:
                    inner_cell = struct.unpack('<I', lbl)[0]
                walk(i + 24, i + size, depth + 1, inner_cell)
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
            if (vanilla_idx is not None and fid >> 24 == vanilla_idx
                    and key not in LEGAL_VANILLA_OVERRIDES):
                note('overrides-vanilla-skyrim',
                     f'{sig.decode()} {fid:08X}')

            names = [s for s, _v in subs(body)]
            bad = FORBIDDEN.get(sig, set()) & set(names)
            if bad:
                note('alien-subrecord',
                     f'{sig.decode()} {fid:08X}: '
                     f'{sorted(x.decode() for x in bad)}')
            if sig == b'LAND' and names and names[0] != b'DATA':
                note('land-not-starting-at-DATA',
                     f'{fid:08X}: {names[0].decode()}')
            if sig == b'LAND' and cell is not None:
                # A cell owns AT MOST ONE landscape. Two LAND records in one
                # cell is unresolvable while the engine parses the file: it
                # hangs on the main menu with no crash and no log, and xEdit
                # still reports the file as clean.
                if cell in cell_lands:
                    note('two-LAND-in-one-cell',
                         f'cell {cell:08X}: {cell_lands[cell]:08X} and '
                         f'{fid:08X}')
                else:
                    cell_lands[cell] = fid
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
        check_grid_order(siblings)

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
