r"""Do the `_0`/`_1` weight-slider pairs share topology?

Skyrim lerps a wearable's vertices between `<name>_0.nif` and `<name>_1.nif`
per weight. That lerp is per-VERTEX-INDEX, so the two files must describe the
same mesh: same shape count, same vertex count per shape, same triangle count.
When they disagree the engine still lerps, pairing vertex 300 of one file with
an unrelated vertex 300 of the other -- which reads in game as the garment
tearing itself apart at intermediate slider values.

`nif_converter` guards this by never converting `_1` independently: it morphs
the finished `_0` (see `body_wrap.morph_converted_to_weight1`). This checks the
shipped result rather than trusting that.

Usage:
  python tools/weight_pair_check.py <meshes tree> [--workers N] [--all]
"""
import argparse
import os
import sys
from multiprocessing import Pool, cpu_count

# tools/nif/ -> repo root is three levels up (matches the other tools/nif/
# scripts since the 2026-08-26 reorganisation).
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def _shape_sig(path):
    """(count, [(vertices, triangles), ...]) or None if unreadable."""
    from asset_convert import pyffi_monkey_patch      # noqa: F401
    from pyffi.formats.nif import NifFormat
    try:
        d = NifFormat.Data()
        with open(path, 'rb') as f:
            d.read(f)
    except Exception:
        return None
    sig = []
    for b in d.blocks:
        if type(b).__name__ not in ('NiTriShape', 'NiTriStrips', 'BSTriShape'):
            continue
        data = getattr(b, 'data', None)
        if data is None:
            sig.append((int(getattr(b, 'num_vertices', 0) or 0), 0))
            continue
        sig.append((int(getattr(data, 'num_vertices', 0) or 0),
                    int(getattr(data, 'num_triangles', 0) or 0)))
    return sig


def check(pair):
    root, zero, one = pair
    a, b = _shape_sig(zero), _shape_sig(one)
    if a is None or b is None:
        return root, 'unreadable', a, b
    if len(a) != len(b):
        return root, 'shape count', a, b
    if a != b:
        return root, 'vertex/triangle count', a, b
    return root, 'ok', a, b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('root')
    ap.add_argument('--workers', type=int, default=max(1, cpu_count() - 1))
    ap.add_argument('--all', action='store_true')
    a = ap.parse_args()

    pairs = []
    for dp, _dirs, files in os.walk(a.root):
        names = {f.lower() for f in files}
        for f in files:
            low = f.lower()
            if not low.endswith('_0.nif'):
                continue
            mate = low[:-len('_0.nif')] + '_1.nif'
            if mate in names:
                pairs.append((os.path.relpath(os.path.join(dp, f), a.root),
                              os.path.join(dp, f),
                              os.path.join(dp, f[:-len('_0.nif')] + '_1.nif')))
    print(f'{len(pairs)} weight pairs under {a.root}', flush=True)
    if not pairs:
        return 0

    bad = []
    ok = 0
    with Pool(a.workers) as pool:
        for i, (root, verdict, sa, sb) in enumerate(
                pool.imap_unordered(check, pairs, 8), 1):
            if verdict == 'ok':
                ok += 1
            else:
                bad.append((root, verdict, sa, sb))
            if i % 200 == 0:
                print(f'  {i}/{len(pairs)} ...', flush=True)

    print(f'\n  matching  {ok}')
    print(f'  MISMATCH  {len(bad)}')
    for root, verdict, sa, sb in (bad if a.all else bad[:20]):
        print(f'\n  {root}   [{verdict}]')
        print(f'    _0: {sa}')
        print(f'    _1: {sb}')
    if not a.all and len(bad) > 20:
        print(f'\n  ... ({len(bad) - 20} more, pass --all)')
    return 0 if not bad else 2


if __name__ == '__main__':
    sys.exit(main())
