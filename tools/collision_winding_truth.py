"""Measure the REAL inverted-collision rate of a mesh tree, against the render mesh.

This is the ground-truth instrument the other winding tools lack.  It does not
run the repair and does not ask what the repair would change; it asks the prior
question:

    how much of this tree's collision is actually wound wrong?

Method: for every near-horizontal collision face, find the render face
COINCIDENT with it (same plane, same XY spot -- tolerances matched to
`_FLOOR_PLANE_DZ` / `_FLOOR_XY` in asset_convert.collision).  The artist's
visual winding is correct by construction, so a collision face pointing
opposite its own render skin is genuinely inverted -- you fall through a
surface you can see.  Faces with no coincident skin are not counted either way,
because nothing decides them.

THE THIN-SLAB TRAP (why "nearest skin" is not good enough).  A stair tread, a
shelf, a table top or a well rim is a thin slab, and BOTH of its skins fall
inside any tolerance loose enough to match at all.  Deciding on raw proximity
therefore picks the wrong skin routinely, in both directions:

  * an UP-facing collision face matched against the tread's UNDERSIDE skin --
    measured on bravilstairs01/02/04 + anvilwell01, ALL 48 "inverted"
    verdicts were decided by a skin below the face (mean dz -0.04, none from
    above).  Every one was a correctly wound stair.
  * a DOWN-facing collision face (a shelf's own correct underside) matched
    against the shelf TOP one thickness above it -- this is what made
    Oblivion furniture read as 16% inverted (upperbookshelf01, upperdesk01,
    mageguilddesk01 are all this).

So a skin may only decide a face it could actually BE: an up-facing face is
decided by a skin at or above it, a down-facing face by a skin at or below it
(_SLAB_EPS slack for float noise).  Anything on the far side of the slab is a
different surface and does not vote.  Without both halves of that rule this
tool manufactures defects out of ordinary furniture and staircases.

Use this BEFORE enabling the winding repair for a plugin.  A tree that measures
near-zero does not need the repair, and turning it on there can only cost
false positives; a tree with a real defect rate is a candidate.  Measured:

    Oblivion architecture/imperialcity : 1 of 5156  (0.02%)

so the "vanilla Oblivion is authored correctly" claim in collision_options.py
is now measured rather than assumed.

Usage:
    python tools/collision_winding_truth.py <nif_or_dir> [--max N]
                                            [--workers N] [--all]
"""
import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TESCONV_COLLISION_WINDING_FIX", "0")

_FLAT = 0.85
_DZ = 0.05
_XY = 0.05
# A skin more than this far BELOW the collision face is the far side of a thin
# slab, not this surface.  0.005 hu ~= 0.35 game units: tighter than any real
# tread thickness (measured 0.016-0.043) and looser than float noise on a
# genuinely co-planar skin.
_SLAB_EPS = 0.005


def _scan(path):
    from asset_convert import pyffi_monkey_patch  # noqa: F401
    from pyffi.formats.nif import NifFormat
    from asset_convert import collision as C

    try:
        data = NifFormat.Data()
        with open(path, 'rb') as f:
            data.read(f)
    except Exception as exc:
        return (path, None, repr(exc)[:70])

    try:
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
                         if isinstance(sh, NifFormat.bhkMoppBvTreeShape)
                         else sh)
                soup = C._shape_tri_soup(inner)
                if soup is None:
                    continue
                tris, _mat = soup
                if isinstance(rb, NifFormat.bhkRigidBodyT):
                    rb.translation.x *= C._HAVOK_SCALE
                    rb.translation.y *= C._HAVOK_SCALE
                    rb.translation.z *= C._HAVOK_SCALE
                tris = C._bake_body_transform_into_tris(rb, tris)
                # Oracle from the NIF ROOT, as the pipeline does: collision
                # frequently hangs off a dedicated collision node with no
                # render geometry of its own.
                vis = C._visual_tri_soup(root)
                if not vis:
                    return (path, (0, 0, 0), None)

                grid = {}
                for vt in vis:
                    vn = C._face_normal(vt)
                    if abs(vn[2]) < _FLAT:
                        continue
                    vc = C._tri_centroid(vt)
                    key = (int(vc[0] // _XY), int(vc[1] // _XY))
                    grid.setdefault(key, []).append((vc, vn[2]))

                agree = dis = 0
                for t in tris:
                    n = C._face_normal(t)
                    if abs(n[2]) < _FLAT:
                        continue
                    c = C._tri_centroid(t)
                    gx, gy = int(c[0] // _XY), int(c[1] // _XY)
                    best = None
                    for ox in (-1, 0, 1):
                        for oy in (-1, 0, 1):
                            for vc, vnz in grid.get((gx + ox, gy + oy), ()):
                                dz = vc[2] - c[2]
                                if abs(dz) > _DZ:
                                    continue
                                d2 = (vc[0] - c[0])**2 + (vc[1] - c[1])**2
                                if d2 > _XY * _XY:
                                    continue
                                # A slab is thin: BOTH its skins fall inside
                                # _DZ, so "nearest" picks the wrong one in
                                # either direction.  An up-facing collision
                                # face is only decided by a skin at/above it;
                                # a down-facing one only by a skin at/below.
                                # Anything on the far side of the slab is a
                                # different surface and must not vote.
                                #   up-face + skin below  -> tread underside
                                #                            (48/48 fake
                                #                             stair defects)
                                #   down-face + skin above -> shelf top over
                                #                            its own correct
                                #                            underside
                                if n[2] > 0 and dz < -_SLAB_EPS:
                                    continue
                                if n[2] < 0 and dz > _SLAB_EPS:
                                    continue
                                if best is None or d2 < best[0]:
                                    best = (d2, vnz)
                    if best is None:
                        continue
                    if (n[2] > 0) == (best[1] > 0):  # best[1] = skin normal z
                        agree += 1
                    else:
                        dis += 1
                return (path, (agree + dis, agree, dis), None)
    except Exception as exc:
        return (path, None, repr(exc)[:70])
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()

    if os.path.isdir(a.root):
        files = []
        for dp, _dn, fn in os.walk(a.root):
            for f in fn:
                if f.lower().endswith('.nif') and '_far' not in f.lower():
                    files.append(os.path.join(dp, f))
        files.sort()
    else:
        files = [a.root]
    if a.max:
        files = files[:a.max]
    print(f"measuring {len(files)} NIFs against their render meshes")

    workers = a.workers or max(1, (os.cpu_count() or 2) - 1)
    if len(files) == 1 or workers == 1:
        results = [_scan(f) for f in files]
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_scan, files, chunksize=4))

    tot = tot_ag = tot_dis = errs = 0
    rows = []
    for r in results:
        if r is None:
            continue
        path, vals, err = r
        if err:
            errs += 1
            continue
        checked, ag, dis = vals
        tot += checked
        tot_ag += ag
        tot_dis += dis
        if dis or a.all:
            rows.append((-dis, path, ag, dis))

    rows.sort()
    for _k, path, ag, dis in rows:
        print(f"  {dis:5d} inverted / {ag:5d} correct   {os.path.basename(path)}")

    pct = (100.0 * tot_dis / tot) if tot else 0.0
    print(f"\nhorizontal collision faces with a coincident render skin: {tot}")
    print(f"  agree with render (correct) : {tot_ag}")
    print(f"  DISAGREE (truly inverted)   : {tot_dis}  ({pct:.2f}%)")
    print(f"unreadable: {errs}")


if __name__ == "__main__":
    main()
