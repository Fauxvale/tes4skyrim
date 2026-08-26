"""Head-gear / hair fit metrics: penetration of the head the game renders.

Grades a converted hair or helmet mesh against the Skyrim head it is worn on
and reports, per mesh:

  * tri-under    — hair TRIANGLES whose deepest interior point is inside the
    head.  This is the measurement that matters and the one a vertex-only
    check misses: a flat triangle spanning a domed skull dips between its
    corners, so a mesh whose every vertex clears the skin can still show
    scalp through the crown.  Sampled barycentrically (``--samples``, default
    8 -> 45 points per triangle).
  * worst        — the deepest such penetration, in Skyrim units.
  * standoff     — median/mean signed clearance over the on-head band, i.e.
    how far the mesh floats off the skin.  Vanilla Skyrim hair hugs; a large
    standoff reads in game as hair hovering.

MEASURE AGAINST THE REAL HEAD, NOT THE CAPPED ONE.  ``head_fit`` conforms
hair to an EAR-CAPPED skull (so ears never push hair outward) but penetration
must be judged against the surface the player actually sees.  Grading on the
capped head is what hid "hair under the skin" defects for several rounds:
one style measured 1 vertex under the capped head and 45 under the real one.
This tool always uses the real head (``sk_full_v`` / ``races_full``).

Usage:
  python -m tools.nif.head_fit_metrics <mesh.nif> [<mesh.nif> ...]
  python -m tools.nif.head_fit_metrics --shipped                 # sweep output/
  python -m tools.nif.head_fit_metrics --shipped --race khajiit
  python -m tools.nif.head_fit_metrics --shipped --group elves --max 12
  python -m tools.nif.head_fit_metrics <mesh.nif> --female --dump 6

A mesh's race/group is inferred from its filename suffix (``__ev`` elves,
``__or`` orc, ``__f`` female, a race token for beast packs) unless given
explicitly.
"""

import argparse
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scipy.spatial import cKDTree                                # noqa: E402

from asset_convert import pyffi_monkey_patch as _patch           # noqa: E402,F401
from pyffi.formats.nif import NifFormat                          # noqa: E402
from asset_convert import head_fit                               # noqa: E402

NIF_MAGIC = (b'Gamebryo', b'NetImmer')
SHIPPED_DIR = os.path.join('output', 'Oblivion.esm', 'meshes', 'tes4',
                           'characters', 'hair')
GROUP_SUFFIX = {'elves': '__ev', 'orc': '__or'}


def is_nif(path):
    try:
        with open(path, 'rb') as fh:
            return fh.read(8) in NIF_MAGIC
    except OSError:
        return False


def load_shapes(path):
    """(verts, tris) of every geometry block, concatenated."""
    data = NifFormat.Data()
    with open(path, 'rb') as fh:
        data.read(fh)
    vs, ts, off = [], [], 0
    for block in data.roots[0].tree():
        if not isinstance(block, (NifFormat.NiTriShape, NifFormat.NiTriStrips)):
            continue
        gd = block.data
        if gd is None or gd.num_vertices == 0:
            continue
        v = np.array([[q.x, q.y, q.z] for q in gd.vertices], dtype=np.float64)
        try:
            t = np.array([tuple(x) for x in gd.get_triangles()], dtype=np.int64)
        except Exception:
            t = np.zeros((0, 3), dtype=np.int64)
        vs.append(v)
        ts.append(t + off)
        off += len(v)
    if not vs:
        return None, None
    return np.vstack(vs), np.vstack(ts)


def barycentric_grid(n):
    """(S,3) barycentric weights sampling a triangle's interior."""
    return np.array([(i / n, j / n, (n - i - j) / n)
                     for i in range(n + 1) for j in range(n + 1 - i)],
                    dtype=np.float64)


def real_head(female, race=None, group=None):
    """The head the GAME RENDERS, in head-local coordinates: (verts, tris)."""
    fit = head_fit._get(female)
    if fit is None:
        return None, None
    if race is not None:
        rf = fit.races_full.get(race)
        return (rf[0], rf[1]) if rf is not None else (None, None)
    if group:
        g = fit.groups.get(group)
        if g is not None:
            return g[2], fit.sk_t
    return fit.sk_full_v, fit.sk_t


def infer(path):
    """(female, race, group) from a converted mesh's filename."""
    low = os.path.basename(path).lower()
    female = '__f' in low
    race = head_fit.fit_race_for_hair(low)
    group = None
    for g, suf in GROUP_SUFFIX.items():
        if suf in low:
            group = g
    return female, race, group


def measure(path, female=None, race=None, group=None, samples=8, dump=0):
    """Print one line of metrics; return the per-triangle min clearance."""
    label = os.path.basename(path)
    inf_f, inf_r, inf_g = infer(path)
    female = inf_f if female is None else female
    race = inf_r if race is None else race
    group = inf_g if group is None else group

    verts, tris = load_shapes(path)
    if verts is None or not len(tris):
        print('  %-34s no geometry' % label)
        return None
    fit = head_fit._get(female)
    if fit is None:
        print('  %-34s head-fit data not built' % label)
        return None
    head_v, head_t = real_head(female, race, group)
    if head_v is None:
        print('  %-34s no head pack for race=%s' % (label, race))
        return None

    # converted meshes are written in world space; head_fit works head-local
    local = verts - fit.o_sk if np.abs(verts[:, 2]).max() > 60 else verts
    tree = cKDTree(head_v[head_t].mean(axis=1))

    bw = barycentric_grid(samples)
    pts = (local[tris[:, 0]][:, None, :] * bw[None, :, 0, None]
           + local[tris[:, 1]][:, None, :] * bw[None, :, 1, None]
           + local[tris[:, 2]][:, None, :] * bw[None, :, 2, None])
    pts = pts.reshape(-1, 3)
    chunks = []
    for i in range(0, len(pts), 200000):          # bound peak memory
        chunks.append(head_fit._signed_clearance(pts[i:i + 200000],
                                                 head_v, head_t, tree))
    tmin = np.concatenate(chunks).reshape(len(tris), -1).min(axis=1)

    clear = head_fit._signed_clearance(local, head_v, head_t, tree)
    band = clear < 3.0                            # the on-head scalp band
    under = tmin < 0
    print('  %-34s tri-under %4d / %5d  worst %+.3f  standoff p50 %+.3f '
          'mean %+.3f'
          % (label, int(under.sum()), len(tris), float(tmin.min()),
             float(np.percentile(clear[band], 50)) if band.any() else float('nan'),
             float(clear[band].mean()) if band.any() else float('nan')))
    if dump and under.any():
        centers = local[tris[under]].mean(axis=1)
        for k in np.argsort(tmin[under])[:dump]:
            print('        depth %+.3f at x %+6.2f y %+6.2f z %+6.2f'
                  % (tmin[under][k], centers[k, 0], centers[k, 1],
                     centers[k, 2]))
    return tmin


def sweep(directory, race=None, group=None, limit=12, **kw):
    n = 0
    for name in sorted(os.listdir(directory)):
        low = name.lower()
        path = os.path.join(directory, name)
        if not is_nif(path):
            continue
        if race and race not in low:
            continue
        if group and GROUP_SUFFIX[group] not in low:
            continue
        if race is None and group is None:
            if '__' in low or head_fit.fit_race_for_hair(low):
                continue                          # plain human base variants
        measure(path, race=race, group=group, **kw)
        n += 1
        if n >= limit:
            break
    if not n:
        print('  (no matching meshes)')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('meshes', nargs='*', help='converted .nif files to grade')
    ap.add_argument('--shipped', action='store_true',
                    help='sweep the converted hair in output/')
    ap.add_argument('--dir', default=SHIPPED_DIR,
                    help='directory for --shipped (default: %(default)s)')
    ap.add_argument('--race', choices=sorted(head_fit._RACE_PACKS),
                    help='beast race pack (default: infer from filename)')
    ap.add_argument('--group', choices=sorted(GROUP_SUFFIX),
                    help='race group (default: infer from filename)')
    ap.add_argument('--female', action='store_true',
                    help='grade against the female head')
    ap.add_argument('--samples', type=int, default=8,
                    help='barycentric subdivision per triangle '
                         '(default: %(default)s -> 45 points)')
    ap.add_argument('--dump', type=int, default=0,
                    help='print the N deepest penetrations with positions')
    ap.add_argument('--max', type=int, default=12,
                    help='max meshes for --shipped (default: %(default)s)')
    args = ap.parse_args()

    kw = dict(samples=args.samples, dump=args.dump)
    if args.shipped:
        sweep(args.dir, race=args.race, group=args.group, limit=args.max, **kw)
    elif args.meshes:
        for path in args.meshes:
            measure(path, female=args.female or None, race=args.race,
                    group=args.group, **kw)
    else:
        ap.error('give mesh paths or --shipped')


if __name__ == '__main__':
    main()
