"""Census ragdoll rigid-body MASS and capsule RADIUS across skeleton.hkx files.

Reads Havok packfiles of EITHER bitness (WIN32/AMD64) directly, without
hkxcmd — so it works on shipped 64-bit output AND on vanilla Skyrim
skeletons pulled from the SSE BSAs (which hkxcmd cannot read at all).

Why this exists: Oblivion authors ragdoll body masses on its own scale and
`hkx_ragdoll.extract_ragdoll` used to carry them through verbatim while
scaling every LENGTH by `_OB_TO_GAME`.  Vanilla Skyrim creature bodies are
2-8 units each (dog total 74); unconverted Oblivion sets reach 200 per body
and 11,320 total, which the ragdoll solver cannot hold — the corpse stays
rigid and tunnels through the floor.  This tool is how that was measured and
how the conversion is kept honest.

Method: `hkpRigidBody` stores `inertiaAndMassInv` as a float[4]
(1/Ix, 1/Iy, 1/Iz, 1/mass) and `hkpCapsuleShape` stores radius plus
vertexA/vertexB as float[4] each (w == radius).  Both are located by scanning
the data section for the class's distinctive float signature rather than by
walking the (bitness-dependent) pointer layout: a capsule is a quad pair
whose w components are equal and positive, and a body motion quad is a
positive 4-vector immediately preceded by the motion-state transform.

Usage:
  # vanilla census (fetches nothing — pass files you already have)
  python tools/ragdoll_mass_census.py path/to/skeleton.hkx [more.hkx ...]

  # ours, whole plugin
  python tools/ragdoll_mass_census.py --glob \
      "output/Oblivion.esm/meshes/actors/tes4/*/character assets/skeleton.hkx"

  # side-by-side summary only
  python tools/ragdoll_mass_census.py --summary <files...>
"""
import argparse
import glob as globmod
import os
import re
import struct
import sys


# A real ragdoll body's principal inertia is never below ~5 (vanilla's
# smallest is the dog's 10.68 toe cap), so 1/I < 0.2.  Quaternion and
# transform components routinely land in 0.2..1.0 and are the whole source of
# false positives — this bound is what separates them.
_MAX_INV_I = 0.2


def _floats(buf, off, n):
    return struct.unpack_from('<%df' % n, buf, off)


def _sane(v, lo, hi):
    return all(lo <= abs(x) <= hi or x == 0.0 for x in v)


def capsule_radii(buf):
    """Every plausible hkpCapsuleShape radius in the file.

    Layout (both bitnesses): ... radius(float) pad(3 floats or 3 ints) ...
    vertexA(float4) vertexB(float4), where vertexA.w == vertexB.w == radius.
    Locate by that triple-equality, which is unique enough in practice.
    """
    out = []
    n = len(buf) - 40
    off = 0
    while off < n:
        try:
            va = _floats(buf, off, 4)
            vb = _floats(buf, off + 16, 4)
        except struct.error:
            break
        r = va[3]
        if (r > 0.01 and abs(vb[3] - r) < 1e-6
                and _sane(va[:3], 0.0, 1e5) and _sane(vb[:3], 0.0, 1e5)):
            out.append((r, va[:3], vb[:3]))
            off += 32
            continue
        off += 4
    return out


def body_masses(buf):
    """Every plausible hkpRigidBody mass, from inertiaAndMassInv float[4].

    The quad is (1/Ix, 1/Iy, 1/Iz, 1/mass): all four strictly positive, the
    inverse inertias below `_MAX_INV_I` (a real ragdoll inertia is >= 5, and
    vanilla's smallest is 10.68) and the inverse mass in a believable band
    (mass 0.1 .. 5000).  It is always followed by linearVelocity and
    angularVelocity quads, which are ZERO in a rest-pose skeleton file — that
    trailing 8 zero floats is what removes the false positives that a plain
    magnitude test picks up from quaternion/transform data.

    Calibrated against the vanilla dog (`character assets dog/skeleton.hkx`),
    where this finds exactly the 22 real bodies with masses 2..6 / total 74,
    matching an hkxcmd XML decompile field for field.
    """
    out = []
    n = len(buf) - 64
    off = 0
    while off < n:
        try:
            q = _floats(buf, off, 4)
            tail = _floats(buf, off + 16, 8)
        except struct.error:
            break
        if (all(x > 0.0 for x in q)
                and all(x < _MAX_INV_I for x in q[:3])
                and 2e-4 < q[3] < 10.0
                and all(x == 0.0 for x in tail)):
            inv_i = q[:3]
            out.append((1.0 / q[3],
                        tuple(1.0 / x for x in inv_i)))
            off += 48
            continue
        off += 4
    return out


def ragdoll_names(buf):
    return [m.group(0)[:-1].decode('ascii', 'replace')
            for m in re.finditer(rb'Ragdoll_[\x20-\x7e]{1,48}?\x00', buf)]


def report(path, summary=False):
    buf = open(path, 'rb').read()
    bits = 64 if len(buf) > 0x11 and buf[0x10] == 8 else 32
    names = ragdoll_names(buf)
    masses = body_masses(buf)
    caps = capsule_radii(buf)
    label = os.path.basename(os.path.dirname(os.path.dirname(path))) \
        or os.path.basename(path)
    ms = [m for m, _i in masses]
    rs = [r for r, _a, _b in caps]
    print(f'=== {label}  ({os.path.basename(path)}, {bits}-bit)')
    print(f'    ragdoll bones: {len(names)}   bodies found: {len(ms)}'
          f'   capsules found: {len(rs)}')
    if ms:
        print(f'    mass   min={min(ms):8.2f} max={max(ms):8.2f} '
              f'total={sum(ms):9.1f} mean={sum(ms)/len(ms):7.2f}')
        allI = [v for _m, i in masses for v in i]
        print(f'    inertia min={min(allI):9.2f} max={max(allI):10.2f}')
    if rs:
        print(f'    radius min={min(rs):8.2f} max={max(rs):8.2f} '
              f'mean={sum(rs)/len(rs):7.2f}')
    if summary:
        return
    for i, nm in enumerate(names):
        m = f'{ms[i]:8.2f}' if i < len(ms) else '       -'
        r = f'{rs[i]:7.2f}' if i < len(rs) else '      -'
        print(f'      {nm:34s} mass={m}  r={r}')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('files', nargs='*', help='skeleton.hkx paths')
    ap.add_argument('--glob', action='append', default=[],
                    help='glob pattern of skeleton.hkx paths (repeatable)')
    ap.add_argument('--summary', action='store_true',
                    help='per-file totals only, no per-bone rows')
    ap.add_argument('--max', type=int, default=0,
                    help='stop after N files (0 = all)')
    a = ap.parse_args(argv)

    paths = list(a.files)
    for g in a.glob:
        paths.extend(sorted(globmod.glob(g)))
    if not paths:
        ap.error('no input files (pass paths or --glob)')
    if a.max:
        paths = paths[:a.max]
    for p in paths:
        try:
            report(p, a.summary)
        except Exception as e:                       # noqa: BLE001
            print(f'=== {p}: ERROR {type(e).__name__}: {e}')
        sys.stdout.flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
