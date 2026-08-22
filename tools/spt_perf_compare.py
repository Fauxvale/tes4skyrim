#!/usr/bin/env python3
"""Per-tree triangle/vertex budget: Python generator vs engine-branch path.

The two SpeedTree geometry paths write to the SAME output filenames, so a
build of one overwrites the other and they cannot be diffed after the fact.
This builds BOTH in-process from the same .spt + TREE record and reports the
render cost of each, split into bark and leaves, so the performance impact of
`--engine-branches` can be judged before shipping it.

Draw cost in Skyrim is dominated by triangle count and by the number of
alpha-tested leaf triangles (which are fill-rate heavy and cannot early-Z),
so both are reported separately along with the totals.

Usage:
    python tools/spt_perf_compare.py                       # 10 assorted trees
    python tools/spt_perf_compare.py --trees A B C         # named stems
    python tools/spt_perf_compare.py --all                 # every .spt
    python tools/spt_perf_compare.py --plugin Nehrim.esm
    python tools/spt_perf_compare.py --csv out.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asset_convert.spt_parser import parse_spt              # noqa: E402
from asset_convert.spt_generator import build_tree          # noqa: E402
from asset_convert.spt_converter import load_tree_manifest  # noqa: E402

# A spread of habits: tall canopy, small tree, shrub, conifer, weeper.
DEFAULT_TREES = [
    'treeenglishoakforest01su', 'treeginkgo', 'treedogwoodsu',
    'treecottonwoodsu', 'treemapleforest01su', 'treewillowsu',
    'shrubjuniper01', 'dbush03', 'treeaspensu', 'treepinesu',
]


def counts(geo):
    """(bark_tris, leaf_tris, bark_verts, leaf_verts) for a TreeGeometry."""
    bt = len(geo.bark_tris) if geo.bark_tris is not None else 0
    bv = len(geo.bark_verts) if geo.bark_verts is not None else 0
    lt = sum(len(g['tris']) for g in geo.leaf_groups)
    lv = sum(len(g['verts']) for g in geo.leaf_groups)
    return bt, lt, bv, lv


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--plugin', default='Oblivion.esm')
    ap.add_argument('--trees', nargs='*', default=None)
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--random', type=int, default=None, metavar='N',
                    help='Sample N trees at random from the export')
    ap.add_argument('--seed', type=int, default=0,
                    help='RNG seed for --random (default 0, so the sample is '
                         'reproducible)')
    ap.add_argument('--csv', type=Path, default=None)
    a = ap.parse_args()

    from asset_convert.spt_engine_geom import build_tree_engine, engine_available

    src = Path('export') / a.plugin / 'trees'
    if not src.is_dir():
        print(f'no trees dir: {src}')
        return 1
    if not engine_available():
        print('engine path unavailable (needs a configured Oblivion.exe and '
              'the native/dist harness) -- nothing to compare')
        return 1

    man = load_tree_manifest(Path('export') / a.plugin)
    if a.random:
        import random
        pool = sorted(p.stem for p in src.glob('*.spt'))
        rng = random.Random(a.seed)
        stems = sorted(rng.sample(pool, min(a.random, len(pool))))
        print(f'random sample of {len(stems)} of {len(pool)} trees '
              f'(seed {a.seed})')
    elif a.all:
        stems = sorted(p.stem for p in src.glob('*.spt'))
    elif a.trees:
        stems = [s.lower().replace('.spt', '') for s in a.trees]
    else:
        stems = [s for s in DEFAULT_TREES if (src / f'{s}.spt').is_file()]
        # top up from the export if some defaults are absent
        for p in sorted(src.glob('*.spt')):
            if len(stems) >= 10:
                break
            if p.stem not in stems:
                stems.append(p.stem)

    rows = []
    for stem in stems:
        f = src / f'{stem}.spt'
        if not f.is_file():
            print(f'  (skip {stem}: no .spt)')
            continue
        try:
            tree = parse_spt(f)
        except Exception as e:                  # noqa: BLE001
            print(f'  (skip {stem}: {e})')
            continue
        recs = man.get(stem.lower())
        seed = recs[0][2] if recs else None      # (editorid, icon, seed)
        try:
            py = build_tree(tree, seed=seed)
            en = build_tree_engine(tree, f, seed=seed)
        except Exception as e:                  # noqa: BLE001
            print(f'  (skip {stem}: {e})')
            continue
        pb, pl, pbv, plv = counts(py)
        eb, el, ebv, elv = counts(en)
        rows.append(dict(name=stem, py_bark=pb, py_leaf=pl, py_tot=pb + pl,
                         en_bark=eb, en_leaf=el, en_tot=eb + el,
                         py_v=pbv + plv, en_v=ebv + elv))

    if not rows:
        print('nothing compared')
        return 1

    def pct(o, n):
        return '     -' if not o else f'{(n - o) / o * 100:+6.1f}%'

    print(f'\n{"":30s}{"PYTHON (triangles)":>26s}{"ENGINE (triangles)":>26s}'
          f'{"total":>9s}')
    print(f'{"tree":30s}{"bark":>8s}{"leaf":>9s}{"total":>9s}'
          f'{"bark":>8s}{"leaf":>9s}{"total":>9s}{"delta":>9s}')
    print('-' * 91)
    for r in rows:
        print(f'{r["name"]:30s}{r["py_bark"]:8d}{r["py_leaf"]:9d}{r["py_tot"]:9d}'
              f'{r["en_bark"]:8d}{r["en_leaf"]:9d}{r["en_tot"]:9d}'
              f'{pct(r["py_tot"], r["en_tot"]):>9s}')
    print('-' * 91)
    s = {k: sum(r[k] for r in rows) for k in
         ('py_bark', 'py_leaf', 'py_tot', 'en_bark', 'en_leaf', 'en_tot',
          'py_v', 'en_v')}
    print(f'{"TOTAL":30s}{s["py_bark"]:8d}{s["py_leaf"]:9d}{s["py_tot"]:9d}'
          f'{s["en_bark"]:8d}{s["en_leaf"]:9d}{s["en_tot"]:9d}'
          f'{pct(s["py_tot"], s["en_tot"]):>9s}')
    print(f'{"  mean per tree":30s}'
          f'{s["py_bark"]//len(rows):8d}{s["py_leaf"]//len(rows):9d}'
          f'{s["py_tot"]//len(rows):9d}'
          f'{s["en_bark"]//len(rows):8d}{s["en_leaf"]//len(rows):9d}'
          f'{s["en_tot"]//len(rows):9d}')
    print(f'\nvertices: python {s["py_v"]:,}   engine {s["en_v"]:,}   '
          f'{pct(s["py_v"], s["en_v"]).strip()}')
    print(f'bark triangles : {pct(s["py_bark"], s["en_bark"]).strip()}')
    print(f'leaf triangles : {pct(s["py_leaf"], s["en_leaf"]).strip()}   '
          f'(alpha-tested -- the fill-rate cost)')
    print(f'\n{len(rows)} trees compared from {src}')

    if a.csv:
        import csv
        with open(a.csv, 'w', newline='', encoding='utf-8') as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f'wrote {a.csv}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
