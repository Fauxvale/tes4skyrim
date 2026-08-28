r"""Do a converted mesh's bones agree with the skeleton the game will animate?

The retarget writes bone NiNodes into each worn mesh and then derives the bind
matrices FROM those nodes.  A wrong node therefore leaves the file perfectly
self-consistent -- pairs, rig, partitions, bone names and the `S @ B @ W = I`
invariant all check out -- while the game, which animates the ACTOR's skeleton
instead, tears the garment into spikes.  Nothing inside the NIF can detect
that; only a comparison against the skeleton can.

    census <tree>            how many meshes misplace a bone node
    mesh <file.nif> ...      per-bone detail for named meshes
    restpose <file.nif>      skin at rest, list the vertices that fly off

Reference skeleton: `asset_convert/generated/skeleton_bones_skyrim_*.json` --
what the retarget actually aims at.  Picked per file from the path (`\f\` ->
female), because male and female wearables retarget against DIFFERENT
skeletons and mixing them reports ~60% false failures.  Override with
--skeleton (a .json or a skeleton.nif).

🔴 Do NOT use the bundled reference NIFs as the default: they are different
variants with identical bone POSITIONS but finger and magic-node rotations
off by ~0.13, which trips a rotation tolerance on every mesh weighting a hand.

Examples:
    python tools/skin_skeleton_check.py census "output/Nehrim.esm/meshes/tes4/clothes"
    python tools/skin_skeleton_check.py census <tree> --only "\m\" --all
    python tools/skin_skeleton_check.py mesh output/.../shirt_0.nif
    python tools/skin_skeleton_check.py restpose output/.../shirt_0.nif --top 5

Exit code 2 when anything is misplaced, so it can gate a build.
"""
import argparse
import json
import os
import sys

import numpy as np

# tools/nif/ -> repo root is three levels up (matches the other tools/nif/
# scripts since the 2026-08-26 reorganisation).
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from asset_convert import pyffi_monkey_patch  # noqa: F401,E402
from pyffi.formats.nif import NifFormat       # noqa: E402

POS_TOL = 1.0
ROT_TOL = 0.02

# Derived from the repo ROOT, three levels up from tools/nif/ --
# the same depth sys.path uses above.  Two levels landed on
# tools/asset_convert/generated/, which does not exist.
_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
_GEN = os.path.join(_REPO_ROOT, 'asset_convert', 'generated')
SKEL_JSON = {
    'male': os.path.join(_GEN, 'skeleton_bones_skyrim_male.json'),
    'female': os.path.join(_GEN, 'skeleton_bones_skyrim_female.json'),
}

_cache = {}


def name_of(b):
    try:
        return bytes(b.name).rstrip(b'\x00').decode('latin-1', 'replace')
    except Exception:
        return '?'


def _mat(t):
    """pyffi NiTransform -> (3x3, translation, scale), row-vector convention."""
    m = t.rotation
    R = np.array([[m.m_11, m.m_12, m.m_13],
                  [m.m_21, m.m_22, m.m_23],
                  [m.m_31, m.m_32, m.m_33]], dtype=float)
    return (R,
            np.array([t.translation.x, t.translation.y, t.translation.z],
                     dtype=float),
            float(t.scale))


def _read(path):
    data = NifFormat.Data()
    with open(path, 'rb') as fh:
        data.read(fh)
    return data


def node_worlds(path):
    """{bone name: (R, t, scale)} in world space, from a NIF's node tree."""
    data = _read(path)
    out = {}

    def walk(node, R_p, t_p, s_p):
        r = node.rotation
        R_l = np.array([[r.m_11, r.m_12, r.m_13],
                        [r.m_21, r.m_22, r.m_23],
                        [r.m_31, r.m_32, r.m_33]], dtype=float)
        t_l = np.array([node.translation.x, node.translation.y,
                        node.translation.z], dtype=float)
        s_l = float(node.scale)
        R_w = R_l @ R_p                      # row-vector: world = local @ parent
        t_w = (t_l @ R_p) * s_p + t_p
        s_w = s_l * s_p
        out[name_of(node)] = (R_w, t_w, s_w)
        for c in getattr(node, 'children', []) or []:
            if isinstance(c, NifFormat.NiNode):
                walk(c, R_w, t_w, s_w)

    for root in data.roots:
        if isinstance(root, NifFormat.NiNode):
            walk(root, np.eye(3), np.zeros(3), 1.0)
    return out


def load_reference(spec):
    if spec in _cache:
        return _cache[spec]
    if spec.lower().endswith('.json'):
        with open(spec, 'r') as fh:
            raw = json.load(fh)
        ref = {}
        for n, m in raw.items():
            M = np.array(m, dtype=float)
            ref[n] = (M[:3, :3], M[3, :3], 1.0)
    else:
        ref = node_worlds(spec)
    _cache[spec] = ref
    return ref


def reference_for(path, override=None):
    if override:
        return load_reference(override)
    p = path.lower().replace('/', os.sep)
    female = f'{os.sep}f{os.sep}' in p or p.endswith('f.nif')
    return load_reference(SKEL_JSON['female' if female else 'male'])


def compare(path, override=None):
    """[(dpos, drot, bone)] for bones shared with the reference."""
    ref = reference_for(path, override)
    mine = node_worlds(path)
    rows = []
    for bn, (R, t, _s) in mine.items():
        if bn not in ref:
            continue
        Rt, tt, _st = ref[bn]
        rows.append((float(np.linalg.norm(t - tt)),
                     float(np.abs(R - Rt).max()), bn))
    rows.sort(reverse=True)
    return rows


def is_bad(dp, dr):
    return dp > POS_TOL or dr > ROT_TOL


def nif_files(target):
    if os.path.isfile(target):
        return [target]
    out = []
    for dirpath, _d, fs in os.walk(target):
        for f in fs:
            if f.lower().endswith('.nif'):
                out.append(os.path.join(dirpath, f))
    out.sort()
    return out


def cmd_census(args):
    files = nif_files(args.tree)
    if args.only:
        needle = args.only.lower().replace('/', os.sep)
        files = [f for f in files if needle in f.lower()]
    if args.max:
        files = files[:args.max]
    print(f'scanning {len(files)} meshes under {args.tree}')
    sys.stdout.flush()

    with_bones = clean = 0
    offenders = []
    for i, f in enumerate(files, 1):
        if i % 250 == 0:
            print(f'  {i}/{len(files)} ...')
            sys.stdout.flush()
        try:
            rows = compare(f, args.skeleton)
        except Exception:
            continue
        if not rows:
            continue
        with_bones += 1
        bad = [r for r in rows if is_bad(r[0], r[1])]
        if bad:
            offenders.append((bad[0][0], len(bad), len(rows),
                              os.path.relpath(f, args.tree)))
        else:
            clean += 1

    print(f'\nmeshes with skeleton bones : {with_bones}')
    print(f'  all nodes correct        : {clean} '
          f'({clean * 100.0 / max(1, with_bones):.1f}%)')
    print(f'  MISPLACED nodes          : {len(offenders)} '
          f'({len(offenders) * 100.0 / max(1, with_bones):.1f}%)')
    if offenders:
        offenders.sort(reverse=True)
        print('\nworst:')
        for worst, bad, tot, rel in (offenders if args.all
                                     else offenders[:20]):
            print(f'  dpos<={worst:8.1f}  {bad}/{tot} nodes  {rel}')
    return 2 if offenders else 0


def cmd_mesh(args):
    rc = 0
    for path in args.files:
        print(f'\n=== {os.path.basename(path)}')
        rows = compare(path, args.skeleton)
        bad = [r for r in rows if is_bad(r[0], r[1])]
        print(f'  bones shared with the reference: {len(rows)}')
        print(f'  MISPLACED                      : {len(bad)}')
        for dp, dr, bn in (rows if args.all else rows[:12]):
            flag = ' <-- MISPLACED' if is_bad(dp, dr) else ''
            print(f'    {bn:34s} dpos={dp:8.2f} drot={dr:.3f}{flag}')
        if bad:
            rc = 2
    return rc


def cmd_restpose(args):
    """Skin the mesh at rest and report the vertices that land far away.

    This is the one that shows what the PLAYER sees: it names the dominant
    bone of each outlier, which is the bone to go and look at.
    """
    rc = 0
    for path in args.files:
        ref = reference_for(path, args.skeleton)
        data = _read(path)
        print(f'\n=== {os.path.basename(path)}')
        for blk in data.get_global_iterator():
            if not isinstance(blk, (NifFormat.NiTriShape,
                                    NifFormat.NiTriStrips)):
                continue
            skin = getattr(blk, 'skin_instance', None)
            sd = getattr(skin, 'data', None) if skin else None
            if sd is None or blk.data is None:
                continue
            nv = blk.data.num_vertices
            verts = np.array([[v.x, v.y, v.z] for v in blk.data.vertices],
                             dtype=float)
            Rg, tg, sg = _mat(sd.skin_transform)
            p_skin = (verts @ Rg) * sg + tg

            acc = np.zeros((nv, 3))
            wsum = np.zeros(nv)
            best_w = np.zeros(nv)
            best_b = [''] * nv
            for bi, bd in enumerate(sd.bone_list):
                bone = skin.bones[bi] if bi < len(skin.bones) else None
                bn = name_of(bone) if bone is not None else '?'
                if bn not in ref:
                    continue
                Rb, tb, sb = _mat(bd.skin_transform)
                Rw, tw, sw = ref[bn]
                p_bone = (p_skin @ Rb) * sb + tb
                p_world = (p_bone @ Rw) * sw + tw
                idx = np.array([vw.index for vw in bd.vertex_weights], int)
                w = np.array([vw.weight for vw in bd.vertex_weights], float)
                if idx.size == 0:
                    continue
                acc[idx] += p_world[idx] * w[:, None]
                wsum[idx] += w
                for k, vi in enumerate(idx):
                    if w[k] > best_w[vi]:
                        best_w[vi], best_b[vi] = w[k], bn

            ok = wsum > 1e-6
            if not ok.any():
                continue
            pos = np.zeros_like(acc)
            pos[ok] = acc[ok] / wsum[ok, None]
            used = set()
            try:
                for t in blk.data.get_triangles():
                    used.update(int(x) for x in t)
            except Exception:
                pass
            mask = np.zeros(nv, bool)
            mask[sorted(used) if used else slice(None)] = True
            mask &= ok
            if not mask.any():
                continue
            P = pos[mask]
            centre = np.median(P, axis=0)
            dist = np.linalg.norm(P - centre, axis=1)
            med, mx = float(np.median(dist)), float(dist.max())
            spike = mx > 6 * max(med, 1.0)
            if spike:
                rc = 2
            print(f'  "{name_of(blk)}": p50={med:.1f} MAX={mx:.1f}'
                  f'{"  <-- SPIKE" if spike else ""}')
            idx_map = np.nonzero(mask)[0]
            for k in np.argsort(-dist)[:args.top]:
                gi = idx_map[k]
                print(f'      v[{gi}] d={dist[k]:8.1f} bone={best_b[gi]!r}')
    return rc


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--skeleton', help='override reference (.json or .nif)')
    sub = ap.add_subparsers(dest='cmd', required=True)

    c = sub.add_parser('census', help='scan a tree')
    c.add_argument('tree')
    c.add_argument('--only', help=r'substring filter, e.g. "\m\"')
    c.add_argument('--max', type=int)
    c.add_argument('--all', action='store_true')
    c.set_defaults(func=cmd_census)

    m = sub.add_parser('mesh', help='per-bone detail')
    m.add_argument('files', nargs='+')
    m.add_argument('--all', action='store_true')
    m.set_defaults(func=cmd_mesh)

    r = sub.add_parser('restpose', help='find the vertices that fly off')
    r.add_argument('files', nargs='+')
    r.add_argument('--top', type=int, default=5)
    r.set_defaults(func=cmd_restpose)

    args = ap.parse_args()
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
