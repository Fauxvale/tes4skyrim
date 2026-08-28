r"""What does a TES4 mesh say about ITSELF, beyond its shader properties?

Three authored carriers the converter does not consult when choosing material
values:

  * the Havok MATERIAL on the collision body -- stone, wood, metal, glass,
    cloth, skin.  `asset_convert/collision.py` already translates these to
    Skyrim, but only for physics; nothing asks what the surface is MADE of.
  * the ROOT NODE type -- static body, skinned mesh, or plant.
  * BSXFlags -- the mesh declaring its own capabilities (havok, ragdoll,
    animated, external emittance).

Usage:
  python tools/mesh_identity_census.py <mesh tree> [--sample N] [--workers N]

Must live in a FILE, not a heredoc: Windows multiprocessing re-imports the
main module in every spawned worker, and a stdin script cannot be re-imported
-- the pool then respawns forever and fills the disk.
"""
import argparse
import os
import random
import sys
from collections import Counter
from multiprocessing import Pool, cpu_count

# tools/nif/ -> repo root is three levels up (matches the other tools/nif/
# scripts since the 2026-08-26 reorganisation).
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

# asset_convert/collision.py carries the same table for the physics side.
MATS = {0: 'Stone', 1: 'Cloth', 2: 'Dirt', 3: 'Glass', 4: 'Grass',
        5: 'Metal', 6: 'Organic', 7: 'Skin', 8: 'Water', 9: 'Wood',
        10: 'HeavyStone', 11: 'HeavyMetal', 12: 'HeavyWood', 13: 'Chain',
        14: 'Snow', 15: 'StairsStone', 16: 'StairsWood', 17: 'StairsSnow',
        18: 'Elevator', 19: 'Rubber'}

# BSXFlags bits (Oblivion).
BSX = {0: 'animated', 1: 'havok', 2: 'ragdoll', 3: 'complex', 4: 'addon',
       5: 'editor marker', 6: 'dynamic', 7: 'articulated',
       8: 'needs transform updates', 9: 'external emittance'}


def scan(path):
    from asset_convert import pyffi_monkey_patch      # noqa: F401
    from pyffi.formats.nif import NifFormat
    mats, roots, bsx = Counter(), Counter(), Counter()
    try:
        d = NifFormat.Data()
        with open(path, 'rb') as f:
            d.read(f)
    except Exception:
        return mats, roots, bsx
    for r in d.roots:
        roots[type(r).__name__] += 1
    for b in d.blocks:
        if type(b).__name__ == 'BSXFlags':
            v = int(b.integer_data)
            for bit, name in BSX.items():
                if v & (1 << bit):
                    bsx[name] += 1
            continue
        m = getattr(b, 'material', None)
        if m is None:
            continue
        v = getattr(m, 'material', m)          # SkyrimHavokMaterial wrapper
        try:
            mats[int(v)] += 1
        except (TypeError, ValueError):
            pass
    return mats, roots, bsx


def top_folder(path, root):
    rel = os.path.relpath(path, root).replace(os.sep, '/').lower()
    parts = rel.split('/')
    return parts[0] if len(parts) > 1 else '.'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('root')
    ap.add_argument('--sample', type=int, default=500)
    ap.add_argument('--workers', type=int, default=max(1, cpu_count() - 1))
    ap.add_argument('--seed', type=int, default=1)
    a = ap.parse_args()

    files = [os.path.join(dp, fn)
             for dp, _, fs in os.walk(a.root) for fn in fs
             if fn.lower().endswith('.nif')]
    total = len(files)
    if a.sample and a.sample < total:
        random.Random(a.seed).shuffle(files)
        files = files[:a.sample]
    print(f'{total} NIFs, sampling {len(files)} ({a.workers} workers)',
          flush=True)

    mats, roots, bsx = Counter(), Counter(), Counter()
    with Pool(a.workers) as pool:
        for i, (m, r, x) in enumerate(pool.imap_unordered(scan, files, 8), 1):
            mats += m
            roots += r
            bsx += x
            if i % 100 == 0:
                print(f'  {i}/{len(files)} ...', flush=True)

    n = sum(mats.values())
    print(f'\n  Havok material ({n} bodies)')
    for k, v in mats.most_common(14):
        print(f'    {MATS.get(k, k):<14} {v:>6}  {v * 100.0 / max(1, n):5.1f}%')
    print(f'\n  root node ({sum(roots.values())} roots)')
    for k, v in roots.most_common(8):
        print(f'    {k:<24} {v:>6}')
    print(f'\n  BSXFlags bits set')
    for k, v in bsx.most_common(10):
        print(f'    {k:<26} {v:>6}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
