"""Profile what the collision winding repair WOULD do to a plugin's meshes.

`collision_walkable_ab.py` A/Bs step 3 against a repair that is already
running.  This answers the prior question: for a plugin where the repair is
currently OFF, what would turning it ON change?

For each mesh it runs the full pipeline collision path twice -- fix disabled,
then enabled -- and reports, per mesh:

  flips        triangles rewound by the repair
  up/down      near-horizontal collision faces before -> after

A rewind that turns a DOWN-facing floor face UP is a repair (you could not
stand there; now you can).  A rewind in the other direction is damage.  The
summary separates the two, because a raw "triangles changed" count cannot
distinguish them -- and on a corpus assumed to be correctly authored, the
up->down number is the one that matters.

The walkability delta is NOT measured by downward raycast here: that metric
misattributes rewound non-horizontal faces (stair risers, wall panels) as
floor changes.  Horizontal-face polarity is the honest per-mesh signal.

Usage:
    python tools/collision_winding_profile.py <nif_or_dir> [--max N]
                                              [--workers N] [--all]
                                              [--csv PATH]

    # Would enabling the fix help vanilla Oblivion architecture?
    python tools/collision_winding_profile.py export/Oblivion.esm/meshes/architecture

Meshes the repair does not touch are omitted unless --all.
"""
import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_FLAT = 0.85


def _run(path, enabled):
    """(tris, flips) after the full repair with the toggle forced."""
    os.environ["TESCONV_COLLISION_WINDING_FIX"] = "1" if enabled else "0"
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
            if isinstance(rb, NifFormat.bhkRigidBodyT):
                rb.translation.x *= C._HAVOK_SCALE
                rb.translation.y *= C._HAVOK_SCALE
                rb.translation.z *= C._HAVOK_SCALE
            tris = C._bake_body_transform_into_tris(rb, tris)
            # The oracle must come from the NIF ROOT, matching the pipeline
            # (_convert_collision passes actual_root): a mesh whose collision
            # hangs off a dedicated collision node has no render geometry in
            # its own subtree and would otherwise score a silent zero.
            vis = C._visual_tri_soup(root)
            tris, n = C._repair_inverted_floors(tris, vis, groups)
            return tris, n
    return None, 0


def _polarity(tris):
    from asset_convert import collision as C
    up = dn = 0
    for t in tris:
        n = C._face_normal(t)
        if abs(n[2]) < _FLAT:
            continue
        if n[2] > 0:
            up += 1
        else:
            dn += 1
    return up, dn


def _scan(path):
    try:
        off, _ = _run(path, False)
        on, flips = _run(path, True)
    except Exception as exc:
        return (path, None, repr(exc)[:70])
    if off is None or on is None:
        return None
    try:
        # Count the rewinds by direction, per triangle.
        from asset_convert import collision as C
        d2u = u2d = 0
        for a, b in zip(off, on):
            if a == b:
                continue
            na = C._face_normal(a)[2]
            nb = C._face_normal(b)[2]
            if na < -_FLAT and nb > _FLAT:
                d2u += 1
            elif na > _FLAT and nb < -_FLAT:
                u2d += 1
        return (path, (flips, _polarity(off), _polarity(on), d2u, u2d), None)
    except Exception as exc:
        return (path, None, repr(exc)[:70])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--csv")
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
    print(f"profiling {len(files)} NIFs (repair OFF vs ON)")

    workers = a.workers or max(1, (os.cpu_count() or 2) - 1)
    if len(files) == 1 or workers == 1:
        results = [_scan(f) for f in files]
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_scan, files, chunksize=4))

    touched = tot_flips = tot_d2u = tot_u2d = errs = 0
    withcoll = 0
    rows = []
    for r in results:
        if r is None:
            continue
        path, vals, err = r
        if err:
            errs += 1
            continue
        withcoll += 1
        flips, (u0, d0), (u1, d1), d2u, u2d = vals
        tot_flips += flips
        tot_d2u += d2u
        tot_u2d += u2d
        if flips:
            touched += 1
        if flips or a.all:
            rows.append((-(d2u - u2d), path, flips, u0, d0, u1, d1, d2u, u2d))

    rows.sort()
    print(f"  {'flips':>6} {'horiz before':>13} {'horiz after':>12} "
          f"{'dn->up':>7} {'up->dn':>7}  mesh")
    for _k, path, flips, u0, d0, u1, d1, d2u, u2d in rows:
        print(f"  {flips:6d} {u0:5d}u/{d0:<5d}d {u1:5d}u/{d1:<5d}d "
              f"{d2u:7d} {u2d:7d}  {os.path.basename(path)}")

    if a.csv:
        with open(a.csv, 'w', encoding='utf-8') as fh:
            fh.write("mesh,flips,up_before,down_before,up_after,down_after,"
                     "down_to_up,up_to_down\n")
            for _k, path, flips, u0, d0, u1, d1, d2u, u2d in rows:
                fh.write(f"{path},{flips},{u0},{d0},{u1},{d1},{d2u},{u2d}\n")
        print(f"\nwrote {a.csv}")

    print(f"\nmeshes with mesh collision : {withcoll}")
    print(f"meshes the repair touches  : {touched}")
    print(f"triangles rewound          : {tot_flips}")
    print(f"  floor faces DOWN -> UP (repairs): {tot_d2u}")
    print(f"  floor faces UP -> DOWN (damage) : {tot_u2d}")
    print(f"unreadable: {errs}")


if __name__ == "__main__":
    main()
