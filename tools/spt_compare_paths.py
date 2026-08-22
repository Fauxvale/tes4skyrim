#!/usr/bin/env python3
"""Compare SpeedTree NIFs produced by the two geometry paths, side by side.

The Python generator (`asset_convert/spt_generator.py`) and the engine path
(`asset_convert/spt_engine_geom.py`, opt-in via `--engine-branches`) write to
the SAME output filenames, so a build of one overwrites the other.  Point this
at two directories built separately and it reports, per tree, how the BARK
geometry differs -- vertex/triangle counts, height, crown radius -- plus
whether each NIF carries collision and its resolved textures.

Usage:
    python tools/spt_compare_paths.py <dir_a> <dir_b> [--label-a X --label-b Y]
                                      [--only STEM ...] [--sort-by DELTA]
                                      [--csv out.csv]

Example:
    python -m asset_convert.spt_converter export/Oblivion.esm/trees \
        temp/spt_compare/python --export-dir export/Oblivion.esm
    python convert.py -f Oblivion.esm --speedtrees-only --engine-branches
    python tools/spt_compare_paths.py temp/spt_compare/python \
        output/Oblivion.esm/meshes/tes4/speedtrees --label-a python --label-b engine
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from asset_convert import pyffi_monkey_patch as _patch  # noqa: F401,E402
from pyffi.formats.nif import NifFormat  # noqa: E402


def _shape_stats(shape):
    d = shape.data
    if d is None or not d.num_vertices:
        return None
    v = np.array([(x.x, x.y, x.z) for x in d.vertices], np.float64)
    return {
        'verts': int(d.num_vertices),
        'tris': int(d.num_triangles),
        'height': float(v[:, 2].max()),
        'zmin': float(v[:, 2].min()),
        'radius': float(np.hypot(v[:, 0], v[:, 1]).max()),
    }


def read_nif(path: Path) -> dict | None:
    """Bark/leaf geometry stats + collision presence for one tree NIF."""
    try:
        data = NifFormat.Data()
        with open(path, 'rb') as fh:
            data.read(fh)
    except Exception as e:                      # noqa: BLE001 - report, skip
        return {'error': str(e)}
    if not data.roots:
        return {'error': 'no roots'}
    root = data.roots[0]

    out = {'collision': root.collision_object is not None,
           'bark': None, 'leaves': [], 'textures': []}
    for ch in getattr(root, 'children', []) or []:
        if ch is None or not hasattr(ch, 'data'):
            continue
        st = _shape_stats(ch)
        if st is None:
            continue
        nm = ch.name.decode(errors='replace') if ch.name else ''
        tex = []
        for pr in (getattr(ch, 'bs_properties', None) or []):
            ts = getattr(pr, 'texture_set', None)
            if ts:
                tex += [t.decode(errors='replace') for t in ts.textures if t]
        out['textures'] += tex
        if nm.lower().endswith(':bark'):
            out['bark'] = st
        else:
            out['leaves'].append(st)
    return out


def _fmt_delta(a, b):
    if a is None or b is None:
        return '     n/a'
    if a == 0:
        return '       -'
    return f'{(b - a) / a * 100:+7.1f}%'


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('dir_a', type=Path)
    ap.add_argument('dir_b', type=Path)
    ap.add_argument('--label-a', default='A')
    ap.add_argument('--label-b', default='B')
    ap.add_argument('--only', nargs='*', default=None,
                    help='Only these NIF stems')
    ap.add_argument('--sort-by-delta', action='store_true',
                    help='Largest bark-triangle difference first')
    ap.add_argument('--csv', type=Path, default=None)
    a = ap.parse_args()

    names = sorted({p.name for p in a.dir_a.glob('*.nif')}
                   & {p.name for p in a.dir_b.glob('*.nif')})
    if a.only:
        want = {s.lower().replace('.nif', '') for s in a.only}
        names = [n for n in names if n.lower().replace('.nif', '') in want]
    if not names:
        print('No NIFs common to both directories.')
        print(f'  {a.dir_a}: {len(list(a.dir_a.glob("*.nif")))} nifs')
        print(f'  {a.dir_b}: {len(list(a.dir_b.glob("*.nif")))} nifs')
        return 1

    rows = []
    for n in names:
        ra, rb = read_nif(a.dir_a / n), read_nif(a.dir_b / n)
        if ra.get('error') or rb.get('error'):
            print(f'  !! {n}: {ra.get("error") or rb.get("error")}')
            continue
        ba, bb = ra['bark'], rb['bark']
        rows.append({
            'name': n.replace('.nif', ''),
            'a_v': ba['verts'] if ba else 0, 'b_v': bb['verts'] if bb else 0,
            'a_t': ba['tris'] if ba else 0, 'b_t': bb['tris'] if bb else 0,
            'a_h': ba['height'] if ba else 0.0,
            'b_h': bb['height'] if bb else 0.0,
            'a_r': ba['radius'] if ba else 0.0,
            'b_r': bb['radius'] if bb else 0.0,
            'a_lv': sum(s['verts'] for s in ra['leaves']),
            'b_lv': sum(s['verts'] for s in rb['leaves']),
            'a_col': ra['collision'], 'b_col': rb['collision'],
            'a_tex': len(ra['textures']), 'b_tex': len(rb['textures']),
        })

    if a.sort_by_delta:
        rows.sort(key=lambda r: -abs(r['b_t'] - r['a_t']))

    la, lb = a.label_a, a.label_b
    print(f'\n{"tree":32s} {"bark tris":>19s} {"delta":>8s} '
          f'{"height":>17s} {"radius":>17s}  col tex')
    print(f'{"":32s} {la[:8]:>9s}{lb[:8]:>10s} {"":>8s} '
          f'{la[:7]:>8s}{lb[:7]:>9s} {la[:7]:>8s}{lb[:7]:>9s}')
    print('-' * 110)
    for r in rows:
        col = ('Y' if r['a_col'] else 'n') + ('Y' if r['b_col'] else 'n')
        tex = f"{r['a_tex']}/{r['b_tex']}"
        print(f"{r['name']:32s} {r['a_t']:9d}{r['b_t']:10d} "
              f"{_fmt_delta(r['a_t'], r['b_t'])} "
              f"{r['a_h']:8.1f}{r['b_h']:9.1f} "
              f"{r['a_r']:8.1f}{r['b_r']:9.1f}  {col:3s} {tex}")

    ta = sum(r['a_t'] for r in rows)
    tb = sum(r['b_t'] for r in rows)
    print('-' * 110)
    print(f'{"TOTAL bark triangles":32s} {ta:9d}{tb:10d} {_fmt_delta(ta, tb)}')
    la_v = sum(r['a_lv'] for r in rows)
    lb_v = sum(r['b_lv'] for r in rows)
    print(f'{"TOTAL leaf verts":32s} {la_v:9d}{lb_v:10d} {_fmt_delta(la_v, lb_v)}')
    miss_a = sum(1 for r in rows if not r['a_col'])
    miss_b = sum(1 for r in rows if not r['b_col'])
    print(f'{"NIFs without collision":32s} {miss_a:9d}{miss_b:10d}')
    print(f'\n{len(rows)} trees compared.  {la} = {a.dir_a}   {lb} = {a.dir_b}')

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
