"""A/B the per-triangle walkable repair (collision winding step 3) over a tree.

`tools/collision_winding.py --floor-regress` only inspects each mesh's LOWEST
near-horizontal surface, so it cannot see a raised deck, an upper storey or a
balcony -- precisely the class of localised inversion that step 3
(`asset_convert.collision._repair_inverted_walkables`) exists to repair.  That
blind spot is why exUdeUship's inverted foredeck scored clean there.

This tool measures the invariant the engine actually enforces: drop a ray
straight down over an XY grid and ask whether the FIRST collision face it hits
points up (you stand on it) or down (you fall through).  It runs the full
repair twice per mesh -- once with step 3 disabled, once enabled -- and reports
the delta, so a regression and an improvement are distinguishable rather than
both showing up as "changed".

Usage:
    python tools/collision_walkable_ab.py <nif_or_dir> [--max N] [--workers N]
                                          [--grid N] [--all]

    # The plugins with the winding fix on by default:
    python tools/collision_walkable_ab.py export/Nehrim.esm/meshes/architecture
    python tools/collision_walkable_ab.py export/Morrowind_ob.esm/meshes/morro/x

Only meshes whose walkable/fall-through counts CHANGE are listed by default;
--all lists every mesh scanned.  Exit status is non-zero when any regression
(walkable -> fall-through) is found, so this is usable as a gate.
"""
import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TESCONV_COLLISION_WINDING_FIX", "1")


def _tris_for(path, step3):
    """Collision triangles after the repair, with step 3 on or off."""
    from asset_convert import pyffi_monkey_patch  # noqa: F401
    from pyffi.formats.nif import NifFormat
    from asset_convert import collision as C

    data = NifFormat.Data()
    with open(path, 'rb') as f:
        data.read(f)

    for root in data.roots:
        for node in root.tree():
            co = getattr(node, 'collision_object', None)
            if co is None:
                continue
            rb = getattr(co, 'body', None)
            if rb is None or getattr(rb, 'shape', None) is None:
                continue
            sh = rb.shape
            inner = (sh.shape
                     if isinstance(sh, NifFormat.bhkMoppBvTreeShape) else sh)
            soup = C._shape_tri_soup(inner)
            if soup is None:
                continue
            tris, _mat = soup
            groups = C.shape_tri_groups(inner)
            # Match the pipeline: the body translation is scaled to Skyrim
            # havok units BEFORE the bake, or the visual oracle lands in the
            # wrong frame and silently never votes.
            if isinstance(rb, NifFormat.bhkRigidBodyT):
                rb.translation.x *= C._HAVOK_SCALE
                rb.translation.y *= C._HAVOK_SCALE
                rb.translation.z *= C._HAVOK_SCALE
            tris = C._bake_body_transform_into_tris(rb, tris)
            vis = C._visual_tri_soup(node)

            orig = C._repair_inverted_walkables
            if not step3:
                C._repair_inverted_walkables = lambda *a, **k: 0
            try:
                tris, _n = C._repair_inverted_floors(tris, vis, groups)
            finally:
                C._repair_inverted_walkables = orig
            return tris
    return None


def _walkability(tris, grid):
    """(walkable, fall_through) over a downward raycast grid."""
    from asset_convert import collision as C

    lo = [min(v[i] for t in tris for v in t) for i in range(3)]
    hi = [max(v[i] for t in tris for v in t) for i in range(3)]
    ztop = hi[2] + 0.1
    good = bad = 0
    ny = grid
    nx = max(4, int(grid * (hi[0] - lo[0]) / max(hi[1] - lo[1], 1e-9)))
    nx = min(nx, grid)
    for iy in range(ny):
        py = lo[1] + (hi[1] - lo[1]) * (iy + 0.5) / ny
        for ix in range(nx):
            px = lo[0] + (hi[0] - lo[0]) * (ix + 0.5) / nx
            best = None
            for t in tris:
                (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = t
                d = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
                if abs(d) < 1e-12:
                    continue
                a = ((y2 - y3) * (px - x3) + (x3 - x2) * (py - y3)) / d
                b = ((y3 - y1) * (px - x3) + (x1 - x3) * (py - y3)) / d
                c = 1.0 - a - b
                if a < 0 or b < 0 or c < 0:
                    continue
                z = a * z1 + b * z2 + c * z3
                if z > ztop:
                    continue
                if best is None or z > best[0]:
                    best = (z, C._face_normal(t)[2])
            if best is None:
                continue
            if best[1] > 0:
                good += 1
            else:
                bad += 1
    return good, bad


def _scan(args):
    path, grid = args
    try:
        off = _tris_for(path, False)
        on = _tris_for(path, True)
    except Exception as exc:
        return (path, None, None, repr(exc)[:70])
    if not off or not on:
        return None
    try:
        return (path, _walkability(off, grid), _walkability(on, grid), None)
    except Exception as exc:
        return (path, None, None, repr(exc)[:70])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--grid", type=int, default=16,
                    help="raycast rows (default 16; higher = finer, slower)")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()

    if os.path.isdir(a.root):
        files = []
        for dp, _dn, fn in os.walk(a.root):
            for f in fn:
                if f.lower().endswith('.nif'):
                    files.append(os.path.join(dp, f))
        files.sort()
    else:
        files = [a.root]
    if a.max:
        files = files[:a.max]
    print(f"scanning {len(files)} NIFs (step3 off vs on)")

    workers = a.workers or max(1, (os.cpu_count() or 2) - 1)
    results = []
    if len(files) == 1 or workers == 1:
        results = [_scan((f, a.grid)) for f in files]
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_scan, [(f, a.grid) for f in files],
                                  chunksize=4))

    fixed = regressed = unchanged = errors = 0
    rows = []
    for r in results:
        if r is None:
            continue
        path, off, on, err = r
        if err:
            errors += 1
            continue
        d_bad = on[1] - off[1]
        if d_bad < 0:
            fixed += 1
        elif d_bad > 0:
            regressed += 1
        else:
            unchanged += 1
        if d_bad or a.all:
            rows.append((d_bad, path, off, on))

    rows.sort()
    for d_bad, path, off, on in rows:
        tag = "IMPROVED" if d_bad < 0 else ("REGRESSION" if d_bad > 0 else "")
        print(f"  {off[0]:5d}/{off[1]:<5d} -> {on[0]:5d}/{on[1]:<5d} "
              f"({d_bad:+d})  {tag:11s} {path}")

    print(f"\nmeshes improved   (fall-through reduced): {fixed}")
    print(f"meshes REGRESSED  (fall-through increased): {regressed}")
    print(f"meshes unchanged: {unchanged}   unreadable: {errors}")
    return 1 if regressed else 0


if __name__ == "__main__":
    sys.exit(main())
