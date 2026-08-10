#!/usr/bin/env python3
"""Verify worldspace CELL grid placement in a TES5 plugin.

Why this exists: the engine builds its grid-cell array by walking a
worldspace's type-4 block / type-5 sub-block GRUP tree and reading each cell's
coordinates from XCLC.  A cell filed inside that tree with NO XCLC leaves the
grid entry unallocated, and the cell-streaming tick then indexes a null array
(SkyrimSE.exe+050E6AD `mov rbx,[rax+rcx*8]` with rax=0 — the load is NOT
bounds-checked, because an allocated grid array is an assumed invariant).
That is a hard CTD as soon as the player streams near the cell.

The invariant, measured against vanilla: 0 of 16,942 exterior-block CELLs in
Skyrim.esm lack XCLC.  Any nonzero count here is a real defect.

Oblivion leaves the persistent bit (RecordFlags & 0x400) CLEAR on ~30 such
cells, so classifying on the flag alone is not enough — see
docs/world_land_navmesh_notes.md.

Usage:
    # check a converted plugin
    python tools/cell_grid_check.py output/Oblivion.esm/Oblivion.esm

    # confirm the invariant on vanilla (expects 0)
    python tools/cell_grid_check.py "<skyrim>/Data/Skyrim.esm"

    # also list duplicate grid squares within a worldspace
    python tools/cell_grid_check.py <esm> --duplicates

Exit code is 1 when any gridless block cell is found, so it can gate a build.
"""

import argparse
import struct
import sys
import zlib
from collections import defaultdict

COMPRESSED = 0x00040000


def _subs(buf):
    """First occurrence of each subrecord signature in a record body."""
    out, pos = {}, 0
    while pos + 6 <= len(buf):
        sig = buf[pos:pos + 4]
        size = struct.unpack_from('<H', buf, pos + 4)[0]
        pos += 6
        out.setdefault(sig, buf[pos:pos + size])
        pos += size
    return out


def scan(path):
    """Walk the plugin, tracking GRUP nesting so block membership is exact."""
    with open(path, 'rb') as fh:
        data = fh.read()

    worlds = {}                      # wrld fid -> editor id
    gridless = []                    # (fid, edid, wrld fid)
    grids = defaultdict(list)        # (wrld, gx, gy) -> [fid]
    n_block_cells = 0

    stack, pos = [], 0
    while pos + 24 <= len(data):
        while stack and pos >= stack[-1][0]:
            stack.pop()
        sig = data[pos:pos + 4]
        if sig == b'TES4':
            pos += 24 + struct.unpack_from('<I', data, pos + 4)[0]
            continue
        if sig == b'GRUP':
            gsize, label, gtype = struct.unpack_from('<IiI', data, pos + 4)[:3]
            stack.append((pos + gsize, gtype, label))
            pos += 24
            continue
        size, flags, fid = struct.unpack_from('<III', data, pos + 4)
        if sig in (b'WRLD', b'CELL'):
            body = data[pos + 24:pos + 24 + size]
            if flags & COMPRESSED:
                body = zlib.decompress(body[4:])
            d = _subs(body)
            edid = d.get(b'EDID', b'').split(b'\x00')[0].decode('ascii', 'replace')
            if sig == b'WRLD':
                worlds[fid] = edid
            elif any(g[1] in (4, 5) for g in stack):
                n_block_cells += 1
                wrld = next((g[2] for g in stack if g[1] == 1), None)
                xclc = d.get(b'XCLC')
                if xclc is None:
                    gridless.append((fid, edid, wrld))
                elif len(xclc) >= 8:
                    gx, gy = struct.unpack_from('<ii', xclc)
                    grids[(wrld, gx, gy)].append(fid)
        pos += 24 + size

    return worlds, gridless, grids, n_block_cells


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('plugin')
    ap.add_argument('--duplicates', action='store_true',
                    help='also report grid squares claimed by more than one cell')
    ap.add_argument('--holes', action='store_true',
                    help='report ENCLOSED grid holes (a missing (x,y) whose four '
                         'neighbours all exist). Vanilla Skyrim has 2, at arbitrary '
                         'coords; a hole at exactly (0,0) is the signature of a cell '
                         'whose TES4 record omitted XCLC')
    args = ap.parse_args()

    worlds, gridless, grids, n_block_cells = scan(args.plugin)

    print(f'exterior-block CELLs: {n_block_cells}')
    print(f'MISSING XCLC:         {len(gridless)}')
    for fid, edid, wrld in gridless:
        wname = worlds.get(wrld, '?')
        wtxt = f'{wname} ({wrld:08X})' if wrld is not None else '(no worldspace)'
        print(f'  {fid:08X}  {edid or "(no edid)":38s} world={wtxt}')

    if args.duplicates:
        dupes = {k: v for k, v in grids.items() if len(v) > 1}
        print(f'\nduplicate grid squares: {len(dupes)}')
        for (wrld, gx, gy), fids in sorted(dupes.items())[:40]:
            ids = ' '.join(f'{f:08X}' for f in fids)
            print(f'  {worlds.get(wrld, "?")} ({gx},{gy}): {ids}')

    n_holes = 0
    if args.holes:
        # Per-cell neighbour walk, NOT a bounding-box sweep: Tamriel's span makes
        # the bbox form O(range^2) and it never finishes.
        by_world = defaultdict(set)
        for (wrld, gx, gy) in grids:
            by_world[wrld].add((gx, gy))
        print()
        for wrld, cells in sorted(by_world.items(),
                                  key=lambda kv: -len(kv[1])):
            cand = set()
            for (x, y) in cells:
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    if (x + dx, y + dy) not in cells:
                        cand.add((x + dx, y + dy))
            holes = [p for p in cand
                     if all((p[0] + dx, p[1] + dy) in cells
                            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))]
            if holes:
                if (0, 0) in holes:
                    n_holes += 1
                flag = '  <-- (0,0): omitted XCLC' if (0, 0) in holes else ''
                print(f'  HOLE {worlds.get(wrld, "?"):32s} '
                      f'{len(holes):3d}  {sorted(holes)[:6]}{flag}')
        # Only a hole at (0,0) is diagnostic.  Vanilla ships 2 enclosed holes at
        # arbitrary coords (WindhelmWorld 29,11 and KatariahWorld -11,28), so
        # holes in general are legal and must not fail the gate.
        print(f'worldspaces holed at (0,0): {n_holes}  (vanilla: 0)')

    if gridless or n_holes:
        if gridless:
            print('\nFAIL: a gridless cell in a block/sub-block leaves the grid array '
                  'unallocated -> null-index CTD while streaming.')
        if n_holes:
            print('FAIL: a hole at (0,0) means a cell whose TES4 record omitted '
                  'XCLC never filled its slot -> null-index CTD while streaming.')
        return 1
    print('\nOK: every exterior-block cell carries XCLC.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
