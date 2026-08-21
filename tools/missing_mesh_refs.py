#!/usr/bin/env python3
"""Placed references whose base record's MESH FILE is not on disk.

Answers "why does the game freeze/hitch when I walk into this area?" -- a REFR
pointing at a base whose MODL names a file we never wrote. The record data is
perfectly valid, so every structural audit (cell_grid_check, land_record_check,
plugin_load_audit) reports the plugin CLEAN; only comparing MODL against the
filesystem finds it.

Resolution mirrors the engine: a base FormID's index byte is looked up in THIS
plugin's MAST list by name, and the mesh is searched under that owner's own
output tree (plus this plugin's, since a dependent ships its own assets).

Usage:
    python tools/missing_mesh_refs.py --plugin TWMP_Valenwood_Elsweyr.esp
    python tools/missing_mesh_refs.py --plugin X.esp --region -60 -40 -60 -40
    python tools/missing_mesh_refs.py --plugin X.esp --max 40
"""

import argparse
import os
import struct
import sys
import zlib
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _masters(path):
    with open(path, 'rb') as fh:
        head = fh.read(24)
        if len(head) < 24 or head[:4] != b'TES4':
            return []
        body = fh.read(struct.unpack_from('<I', head, 4)[0])
    out, pos = [], 0
    while pos + 6 <= len(body):
        sig = body[pos:pos + 4]
        size = struct.unpack_from('<H', body, pos + 4)[0]
        pos += 6
        if sig == b'MAST':
            out.append(body[pos:pos + size].rstrip(b'\0').decode('latin-1'))
        pos += size
    return out


def _subs(body):
    """First value of each subrecord signature."""
    out, q = {}, 0
    while q + 6 <= len(body):
        sig = body[q:q + 4]
        size = struct.unpack_from('<H', body, q + 4)[0]
        q += 6
        out.setdefault(sig, body[q:q + size])
        q += size
    return out


def _records(path):
    """fid -> (signature, editorid, model path)."""
    data = open(path, 'rb').read()
    out = {}
    p, n = 0, len(data)
    while p + 24 <= n:
        sig = data[p:p + 4]
        if sig == b'GRUP':
            p += 24
            continue
        size, flags, fid = struct.unpack_from('<III', data, p + 4)
        body = data[p + 24:p + 24 + size]
        if flags & 0x00040000:
            try:
                body = zlib.decompress(body[4:])
            except zlib.error:
                body = b''
        s = _subs(body)
        edid = s.get(b'EDID', b'').split(b'\0')[0].decode('latin-1') or None
        modl = s.get(b'MODL', b'').split(b'\0')[0].decode('latin-1') or None
        out[fid] = (sig.decode('latin-1'), edid, modl)
        p += 24 + size
    return out


def _placements(path, wrld=None):
    """(cell fid -> [base fid]) and (cell fid -> (gx, gy))."""
    data = open(path, 'rb').read()
    cellpos, refs = {}, defaultdict(list)

    def walk(off, end, stack):
        p = off
        while p + 24 <= end:
            sig = data[p:p + 4]
            if sig == b'GRUP':
                gsize, label, gtype = struct.unpack_from('<IiI', data, p + 4)
                walk(p + 24, p + gsize, stack + [(gtype, label)])
                p += gsize
                continue
            size, flags, fid = struct.unpack_from('<III', data, p + 4)
            body = data[p + 24:p + 24 + size]
            if flags & 0x00040000:
                try:
                    body = zlib.decompress(body[4:])
                except zlib.error:
                    body = b''
            if sig == b'CELL' and any(t == 4 for t, _ in stack):
                xclc = _subs(body).get(b'XCLC')
                if xclc and len(xclc) >= 8:
                    cellpos[fid] = struct.unpack_from('<ii', xclc, 0)
            elif sig in (b'REFR', b'ACHR', b'ACRE'):
                cell = next((l for t, l in reversed(stack) if t == 6), None)
                name = _subs(body).get(b'NAME')
                if cell is not None and name and len(name) >= 4:
                    refs[cell].append(struct.unpack_from('<I', name, 0)[0])
            p += 24 + size

    walk(0, len(data), [])
    return cellpos, refs


def main():
    ap = argparse.ArgumentParser(
        description='Placed refs whose base mesh file is missing')
    ap.add_argument('--plugin', required=True)
    ap.add_argument('--output-dir', default=os.path.join(ROOT, 'output'))
    ap.add_argument('--region', nargs=4, type=int,
                    metavar=('XMIN', 'XMAX', 'YMIN', 'YMAX'),
                    help='only cells in this grid box')
    ap.add_argument('--max', type=int, default=25)
    args = ap.parse_args()

    # Through the resolver: an imported mod's plugins convert into their
    # MOD's folder, so `<out>/<plugin>/<plugin>` names nothing for them.
    from output_layout import paths as _paths
    root = args.output_dir
    path = str(_paths(args.plugin, out_root=root).esm)
    if not os.path.isfile(path):
        print('ERROR: no converted plugin at %s' % path)
        return 1

    masters = _masters(path)
    # Index byte -> (record table, mesh root). A plugin's own records sit at
    # index == len(masters); a vanilla master we never convert has no output
    # and is skipped (its assets ship in the game's own BSAs).
    owners = {}
    for slot, name in enumerate(masters):
        mp = str(_paths(name, out_root=root).esm)
        if os.path.isfile(mp):
            owners[slot] = (_records(mp),
                            str(_paths(name, out_root=root).out / 'meshes'))
    owners[len(masters)] = (
        _records(path),
        str(_paths(args.plugin, out_root=root).out / 'meshes'))

    cellpos, refs = _placements(path)
    missing = Counter()
    cells_of = defaultdict(set)
    checked = 0
    for cell, bases in refs.items():
        pos = cellpos.get(cell)
        if args.region and pos:
            x0, x1, y0, y1 = args.region
            if not (x0 <= pos[0] <= x1 and y0 <= pos[1] <= y1):
                continue
        elif args.region:
            continue
        for base in bases:
            owner = owners.get((base >> 24) & 0xFF)
            if not owner:
                continue
            table, mroot = owner
            rec = table.get(base)
            if not rec or not rec[2]:
                continue
            checked += 1
            rel = rec[2].replace('\\', os.sep).replace('/', os.sep)
            if not os.path.isfile(os.path.join(mroot, rel)):
                missing[(rec[2], rec[1], rec[0])] += 1
                if pos:
                    cells_of[rec[2]].add(pos)

    print('plugin: %s' % args.plugin)
    print('placements with a resolvable base model: %d' % checked)
    print('placements whose MESH FILE IS ABSENT   : %d' % sum(missing.values()))
    if missing:
        print()
        for (modl, edid, sig), n in missing.most_common(args.max):
            cs = cells_of.get(modl, set())
            box = ''
            if cs:
                xs = [c[0] for c in cs]
                ys = [c[1] for c in cs]
                box = '  cells %d  X %d..%d  Y %d..%d' % (
                    len(cs), min(xs), max(xs), min(ys), max(ys))
            print('  x%-6d %s %s%s' % (n, sig, edid, box))
            print('           %s' % modl)
    return 1 if missing else 0


if __name__ == '__main__':
    sys.exit(main())
