"""Audit the LAND (landscape) records a converted plugin ships for a worldspace.

Answers "why is the ground missing under this cell?" -- the symptom being
blank, missing terrain while the cell's placed references still render. Checks
the whole contract the engine needs, per cell:

  * the CELL exists in the worldspace and sits in the correct block/sub-block
  * a LAND record exists in its TEMPORARY (type-9) children group
  * LAND is the FIRST record in that group (vanilla: 15,564/15,564 at index 0)
  * VNML/VHGT/VCLR are the exact expected sizes and are not all-zero
  * BTXT/ATXT quadrants and layer indices are in range, no duplicate
    (quadrant, texture) pairs, alpha layers per quadrant within the 6 cap
  * VTXT alpha entries are in range, sorted, free of duplicate positions
  * every BTXT/ATXT texture FormID resolves to an LTEX in this plugin or in a
    master's converted output

Usage:
  python tools/land_record_check.py --plugin ElsweyrAnequina.esp
  python tools/land_record_check.py --plugin ElsweyrAnequina.esp --cell -7 -32
  python tools/land_record_check.py --plugin Tamriel.esp --max 50
  python tools/land_record_check.py --plugin X.esp --masters Oblivion.esm
"""

import argparse
import os
import struct
import sys
import zlib
from collections import Counter

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

VNML_SIZE = 3267        # 33*33*3
VHGT_SIZE = 1096        # float offset + 33*33 int8 + 3 pad
VCLR_SIZE = 3267
MAX_ALPHA_LAYERS = 6
MAX_VTXT_POS = 288      # 17*17 quadrant grid


def _master_names(path):
    """MAST entries of a plugin header, in load order."""
    with open(path, 'rb') as fh:
        head = fh.read(24)
        if len(head) < 24 or head[:4] != b'TES4':
            return []
        hdr = fh.read(struct.unpack_from('<I', head, 4)[0])
    out = []
    i = 0
    while i + 6 <= len(hdr):
        sig = hdr[i:i + 4]
        size = struct.unpack_from('<H', hdr, i + 4)[0]
        if sig == b'MAST':
            out.append(hdr[i + 6:i + 6 + size].rstrip(b'\0').decode('latin-1'))
        i += 6 + size
    return out


def _master_count(path):
    """Number of MAST entries in a plugin header.

    Also the index byte the file's own records carry, since a file's records
    sit immediately after its masters in load order.
    """
    return len(_master_names(path))


def _iter_records(data):
    """Flatten the file: (sig, fid, body, wrld, cell, gtype, order, blk, sub)."""
    out = []

    def walk(i, end, wrld=0, cell=None, gtype=None, blk=None, sub=None):
        order = 0
        while i + 24 <= end:
            sig = data[i:i + 4]
            size = struct.unpack_from('<I', data, i + 4)[0]
            if sig == b'GRUP':
                gt = struct.unpack_from('<I', data, i + 12)[0]
                label = data[i + 8:i + 12]
                w, c, b, s = wrld, cell, blk, sub
                if gt == 1:
                    w = struct.unpack('<I', label)[0]
                elif gt == 4:
                    b = label
                elif gt == 5:
                    s = label
                elif gt == 6:
                    c = struct.unpack('<I', label)[0]
                walk(i + 24, i + size, w, c,
                     gt if gt in (8, 9, 10) else gtype, b, s)
                i += size
                continue
            flags = struct.unpack_from('<I', data, i + 8)[0]
            fid = struct.unpack_from('<I', data, i + 12)[0]
            body = data[i + 24:i + 24 + size]
            if flags & 0x00040000:
                try:
                    body = zlib.decompress(body[4:])
                except zlib.error:
                    body = b''
            out.append((sig, fid, body, wrld, cell, gtype, order, blk, sub))
            order += 1
            i += 24 + size

    walk(24 + struct.unpack_from('<I', data, 4)[0], len(data))
    return out


def _subrecords(body):
    j = 0
    while j + 6 <= len(body):
        sig = body[j:j + 4]
        size = struct.unpack_from('<H', body, j + 4)[0]
        yield sig, body[j + 6:j + 6 + size]
        j += 6 + size


def _expected_block(x, y):
    """Skyrim exterior block labels: (Y div 32, X div 32) / (Y div 8, X div 8)."""
    return (struct.pack('<hh', y // 32, x // 32),
            struct.pack('<hh', y // 8, x // 8))


def load(path):
    """Index one converted plugin."""
    with open(path, 'rb') as fh:
        data = fh.read()
    cells = {}      # (wrld, x, y) -> (cell fid, block label, sub-block label)
    land = {}       # cell fid -> (body, index within its group, group type)
    ltex = set()
    for sig, fid, body, wrld, cell, gtype, order, blk, sub in _iter_records(data):
        if sig == b'LTEX':
            ltex.add(fid)
        elif sig == b'CELL':
            for s, v in _subrecords(body):
                if s == b'XCLC' and len(v) >= 8:
                    key = (wrld,) + struct.unpack_from('<ii', v, 0)
                    cells[key] = (fid, blk, sub)
        elif sig == b'LAND' and cell is not None:
            land[cell] = (body, order, gtype)
    return cells, land, ltex


def check_land(body, known_ltex):
    """Problem strings for one LAND body (empty list == healthy)."""
    problems = []
    counts = Counter()
    pairs = []
    per_quad = Counter()
    cur = None
    for sig, v in _subrecords(body):
        name = sig.decode('latin1')
        counts[name] += 1
        if sig == b'VNML' and len(v) != VNML_SIZE:
            problems.append('VNML size %d != %d' % (len(v), VNML_SIZE))
        if sig == b'VCLR' and len(v) != VCLR_SIZE:
            problems.append('VCLR size %d != %d' % (len(v), VCLR_SIZE))
        if sig == b'VHGT':
            if len(v) != VHGT_SIZE:
                problems.append('VHGT size %d != %d' % (len(v), VHGT_SIZE))
            elif not any(v[4:4 + 1089]):
                problems.append('VHGT all-zero deltas (flat/void terrain)')
        if sig in (b'BTXT', b'ATXT') and len(v) >= 8:
            tex, quad, _unused, layer = struct.unpack('<IbbH', v[:8])
            cur = (name, quad, layer)
            if not 0 <= quad <= 3:
                problems.append('%s quadrant %d out of range' % (name, quad))
            if sig == b'ATXT':
                per_quad[quad] += 1
                if layer > MAX_ALPHA_LAYERS - 1:
                    problems.append('ATXT layer %d exceeds cap' % layer)
            pairs.append((quad, tex))
            if tex and known_ltex is not None and tex not in known_ltex:
                problems.append('%s texture %08X unresolved' % (name, tex))
        if sig == b'VTXT':
            if len(v) % 8:
                problems.append('VTXT size %d not a multiple of 8' % len(v))
            positions = [struct.unpack_from('<H', v, k * 8)[0]
                         for k in range(len(v) // 8)]
            if any(p > MAX_VTXT_POS for p in positions):
                problems.append('VTXT position out of range (layer %s)' % (cur,))
            if positions != sorted(positions):
                problems.append('VTXT positions unsorted (layer %s)' % (cur,))
            if len(positions) != len(set(positions)):
                problems.append('VTXT duplicate positions (layer %s)' % (cur,))
            for k in range(len(v) // 8):
                op = struct.unpack_from('<f', v, k * 8 + 4)[0]
                if op != op or not (-0.001 <= op <= 1.001):
                    problems.append('VTXT opacity %r (layer %s)' % (op, cur))
                    break
        j = None
    for name in ('DATA', 'VHGT'):
        if counts.get(name, 0) != 1:
            problems.append('%s count %d != 1' % (name, counts.get(name, 0)))
    for quad, n in sorted(per_quad.items()):
        if n > MAX_ALPHA_LAYERS:
            problems.append('quadrant %d has %d alpha layers (> %d)'
                            % (quad, n, MAX_ALPHA_LAYERS))
    dupes = sum(c - 1 for c in Counter(pairs).values() if c > 1)
    if dupes:
        problems.append('%d duplicate (quadrant, texture) layer pair(s)' % dupes)
    return problems


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--plugin', required=True,
                    help='Plugin name under output/ (e.g. ElsweyrAnequina.esp)')
    ap.add_argument('--output-dir', default=os.path.join(SCRIPT_DIR, 'output'))
    ap.add_argument('--cell', nargs=2, type=int, metavar=('X', 'Y'),
                    help='Report one grid cell in full detail')
    ap.add_argument('--masters', nargs='*', default=None,
                    help="Converted masters to resolve LTEX against "
                         "(default: this plugin's own MAST list)")
    ap.add_argument('--max', type=int, default=25,
                    help='Max problem cells to list (default 25)')
    args = ap.parse_args()

    root = args.output_dir
    path = os.path.join(root, args.plugin, args.plugin)
    if not os.path.isfile(path):
        print('ERROR: no converted plugin at %s' % path)
        return 1

    cells, land, ltex = load(path)
    # Defaulting to a single hard-coded master mis-scoped every plugin with a
    # longer list: TWMP_Valenwood_Elsweyr has four (Skyrim, Oblivion, Tamriel,
    # Anequina), so LTEXes owned by Tamriel/Anequina were compared against
    # nothing and reported unresolved on thousands of cells. Vanilla masters
    # we never convert (Skyrim.esm) simply have no output and are skipped by
    # the isfile() check below.
    if args.masters is None:
        args.masters = _master_names(path)
    # A master's LTEX ids are in ITS OWN FormID space; this plugin names them
    # by the index byte of the master's slot in ITS master list. Merging them
    # verbatim compares ids from two different spaces and reports healthy
    # textures as dangling -- it claimed 1,734 unresolved layers in TWMP
    # Valenwood/Elsweyr, every one of them fine. `--masters` is given in load
    # order, so slot = (this plugin's master count - len(masters)) + position.
    # `--masters` is a trailing slice of the load order when given explicitly;
    # the default IS the whole list, so it starts at slot 0.
    base_slot = _master_count(path) - len(args.masters)
    for pos, m in enumerate(args.masters):
        mp = os.path.join(root, m, m)
        if os.path.isfile(mp):
            _c, _l, mltex = load(mp)
            own = _master_count(mp)
            slot = base_slot + pos
            ltex |= {((slot << 24) | (f & 0x00FFFFFF)) for f in mltex
                     if (f >> 24) & 0xFF == own}

    if args.cell:
        want = tuple(args.cell)
        hits = [(k, v) for k, v in cells.items() if k[1:] == want]
        if not hits:
            print('No CELL at %s in %s' % (want, args.plugin))
            return 1
        for (wrld, x, y), (fid, blk, sub) in hits:
            print('CELL %08X at (%d,%d) worldspace %08X' % (fid, x, y, wrld))
            eb, es = _expected_block(x, y)
            ok = (blk == eb and sub == es)
            print('  block/sub-block: %s/%s %s'
                  % (blk.hex() if blk else None, sub.hex() if sub else None,
                     'OK' if ok else 'WRONG (want %s/%s)' % (eb.hex(), es.hex())))
            entry = land.get(fid)
            if not entry:
                print('  LAND: MISSING -- this cell ships no landscape record')
                continue
            body, order, gtype = entry
            print('  LAND present, group type %s, index %d %s'
                  % (gtype, order,
                     'OK' if order == 0 else 'NOT FIRST (vanilla is always 0)'))
            probs = check_land(body, ltex)
            print('  problems: %s' % (probs if probs else 'NONE'))
        return 0

    total = notfirst = bad = 0
    listed = []
    for (wrld, x, y), (fid, blk, sub) in sorted(cells.items()):
        total += 1
        entry = land.get(fid)
        if not entry:
            continue    # no LAND is normal: a master may supply it
        body, order, gtype = entry
        issues = []
        if order != 0:
            notfirst += 1
            issues.append('LAND at index %d, not first' % order)
        probs = check_land(body, ltex)
        if probs:
            bad += 1
            issues.extend(probs)
        eb, es = _expected_block(x, y)
        if blk != eb or sub != es:
            issues.append('wrong block/sub-block')
        if issues and len(listed) < args.max:
            listed.append(((x, y), issues))
    print('%s: %d exterior cells, %d LAND records'
          % (args.plugin, total, len(land)))
    print('  LAND not first in its group : %d' % notfirst)
    print('  LAND with content problems  : %d' % bad)
    for xy, issues in listed:
        print('   %s: %s' % (xy, '; '.join(issues[:4])))
    if not listed:
        print('  no problems found')
    return 0


if __name__ == '__main__':
    sys.exit(main())
