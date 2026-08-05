"""Sweep the navmesh invariants over a list of cells, optionally vs an ESM.

This is the regression gate.  Run it over the reference cells before and after
any navmesh change: a fix that improves one room while quietly deleting a
corridor in another is a regression, and only a population sweep shows that.

Columns are defined in tools/navmesh/metrics.py.  The ones that mean a cell is
BROKEN (not merely ugly) are miss, crack, choke and dmiss — those are all
"an NPC cannot walk here".  bad/small are the shape contract.

    # measure the current code
    python tools/navmesh/sweep.py ImperialDungeon01 AnvilPinarusInventiusHouse

    # A/B the current code against the ESM already on disk (regression check)
    python tools/navmesh/sweep.py --esm output/Oblivion.esm/Oblivion.esm \
        ImperialDungeon01 ChorrolFightersGuild

    # the standard reference set, no arguments needed
    python tools/navmesh/sweep.py --reference
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.navmesh import metrics  # noqa: E402
from tools.navmesh.index import NavIndex, DEFAULT_EXPORT  # noqa: E402

# The cells every navmesh change is judged against.  Each earned its place by
# exposing a distinct failure: stacked storeys, cave ledges, multi-storey
# stairs, door chokepoints, inter-cell seams.
REFERENCE_CELLS = [
    'ImperialDungeon01',
    'ImperialDungeon05',
    'AnvilPinarusInventiusHouse',
    'ChorrolFightersGuild',
    'LeyawiinCastleCountyHall',
    'BarrenCave',
    'Moranda02',
    'AnvilFightersGuild',
    'SkingradCastleCourtyard',
    'MolagBalCavern01',
    'ICMarketDistrictTheMerchantsInn',
]


def esm_cells(esm_path, cache=True):
    """cell fid -> (verts, tris) merged across that cell's NAVMs.

    Cached on disk: scanning a 700MB ESM takes ~68s, which blows the 120s
    budget as soon as it is combined with an in-process rebuild.  The cache is
    keyed on the ESM's size+mtime, so a fresh build invalidates it by itself.
    """
    import pickle
    key = None
    if cache:
        st = os.stat(esm_path)
        key = os.path.join('temp', 'esm_navm_%d_%d.pkl'
                           % (st.st_size, int(st.st_mtime)))
        if os.path.exists(key):
            with open(key, 'rb') as fh:
                return pickle.load(fh)
    import tools.navmesh.check as nc
    meshes, _navi, _doors, _xndp, _mask = nc.scan(esm_path, want_doors=False)
    by_cell = {}
    for (nm, cell) in meshes:
        by_cell.setdefault(cell, []).append(nm)
    out = {}
    for cell, nms in by_cell.items():
        verts, tris = [], []
        for nm in nms:
            base = len(verts)
            vv = nm.verts
            if vv and isinstance(vv[0], (int, float)):
                vv = [tuple(vv[i:i + 3]) for i in range(0, len(vv), 3)]
            verts.extend(vv)
            tris.extend([(a + base, b + base, c + base)
                         for (a, b, c, *_r) in nm.tris])
        out[cell] = (verts, tris)
    if key:
        if not os.path.isdir('temp'):
            os.makedirs('temp')
        with open(key, 'wb') as fh:
            pickle.dump(out, fh, protocol=4)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cells', nargs='*', help='cell EditorIDs')
    ap.add_argument('--reference', action='store_true',
                    help='sweep the standard reference set')
    ap.add_argument('--esm', help='also measure this ESM for an A/B comparison')
    ap.add_argument('--no-overlaps', action='store_true',
                    help='skip the shapely overlap pass (much faster)')
    ap.add_argument('--export', default=DEFAULT_EXPORT)
    a = ap.parse_args()

    names = list(a.cells)
    if a.reference or not names:
        names = REFERENCE_CELLS + [n for n in names if n not in REFERENCE_CELLS]

    idx = NavIndex(a.export)
    baseline = esm_cells(a.esm) if a.esm else None

    for name in names:
        cell = idx.cell(name)
        if cell is None:
            print('%-28s NOT FOUND' % name)
            continue
        if not cell.has_pathgrid:
            print('%-28s no pathgrid' % name)
            continue
        if baseline is not None:
            key = int(cell.fid, 16) & 0xFFFFFF | 0x01000000
            got = baseline.get(key)
            if got and got[1]:
                m = metrics.measure(got[0], got[1], cell,
                                    want_overlaps=not a.no_overlaps)
                print(metrics.format_row(name + ' [ESM]', m))
            else:
                print('%-28s [ESM] absent' % name)
        t0 = time.time()
        verts, tris = cell.build()
        dt = time.time() - t0
        if not tris:
            print('%-28s EMPTY MESH' % name)
            continue
        m = metrics.measure(verts, tris, cell, want_overlaps=not a.no_overlaps)
        print(metrics.format_row(name + (' [now]' if baseline else ''), m, dt))
    return 0


if __name__ == '__main__':
    sys.exit(main())
