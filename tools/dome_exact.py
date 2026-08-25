"""Exact geometry + vertex data of both games' Atmosphere.nif.

The dome's vertex ALPHA is what fades the sky out at the horizon.  The earlier
8-bucket summary showed Oblivion fading over ~85 units from z=-32 and Skyrim
over ~18 from z=0, but bucket means hide the actual ring structure.  This
prints every distinct RING (vertices sharing a z), with its radius, elevation
angle from the camera at the dome centre, and its exact R/G/B/A.

Elevation angle is the thing that actually matters: the dome is drawn around
the camera, so what the player sees at "the horizon" is the ring at ~0 degrees
elevation, NOT the ring at z=0 in mesh space.
"""
import math
import os
import sys

sys.path.insert(0, r"c:\Users\Bryant\Documents\Code\TESConversion")

HERE = os.path.dirname(os.path.abspath(__file__))
DOMES = [
    ('OBLIVION',
     r"c:\Users\Bryant\Documents\Code\TESConversion\export\Oblivion.esm\meshes\sky\atmosphere.nif"),
    ('SKYRIM',
     os.path.join(HERE, 'sse_atmosphere.nif')),
]


def load_rows(path):
    from asset_convert.sse_nif import read_nif
    d = read_nif(path)
    seen, rows = set(), []

    def walk(b):
        if b is None or id(b) in seen:
            return
        seen.add(id(b))
        for a in ('children', 'properties', 'extra_data_list'):
            for c in (getattr(b, a, None) or []):
                walk(c)
        for a in ('data', 'shader_property', 'alpha_property', 'controller'):
            c = getattr(b, a, None)
            if c is not None:
                walk(c)
        nv = getattr(b, 'num_vertices', None)
        cols = getattr(b, 'vertex_colors', None)
        verts = getattr(b, 'vertices', None)
        if nv and cols and verts:
            for i in range(min(nv, len(cols), len(verts))):
                v, c = verts[i], cols[i]
                rows.append((v.x, v.y, v.z, c.r, c.g, c.b, c.a))
    for r in (d.roots or []):
        walk(r)
    return rows


for label, path in DOMES:
    print('=' * 78)
    print(label, os.path.basename(path))
    print('=' * 78)
    try:
        rows = load_rows(path)
    except Exception as e:
        print('  FAILED:', type(e).__name__, e)
        continue
    if not rows:
        print('  no vertex colours')
        continue

    zs = sorted({round(r[2], 2) for r in rows})
    print(f'  {len(rows)} verts, {len(zs)} distinct z, '
          f'z range {min(zs):.2f}..{max(zs):.2f}')

    # bounding radius, to characterise the dome shape
    rad = [math.hypot(r[0], r[1]) for r in rows]
    print(f'  xy radius {min(rad):.2f}..{max(rad):.2f}')
    print()
    print(f'  {"z":>9s} {"radius":>9s} {"elev°":>7s} {"n":>4s}  '
          f'{"R":>5s} {"G":>5s} {"B":>5s} {"A":>6s}')
    for z in zs:
        grp = [r for r in rows if round(r[2], 2) == z]
        n = len(grp)
        rr = sum(math.hypot(g[0], g[1]) for g in grp) / n
        R = sum(g[3] for g in grp) / n
        G = sum(g[4] for g in grp) / n
        B = sum(g[5] for g in grp) / n
        A = sum(g[6] for g in grp) / n
        elev = math.degrees(math.atan2(z, rr)) if rr > 1e-6 else 90.0
        print(f'  {z:9.2f} {rr:9.2f} {elev:7.2f} {n:4d}  '
              f'{R:5.3f} {G:5.3f} {B:5.3f} {A:6.3f}')
