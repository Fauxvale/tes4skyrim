"""Resample both domes' vertex ALPHA and gradient MASKS against ELEVATION ANGLE.

The dome is drawn centred on the camera, so the ring at elevation 0 is what the
player sees at the horizon.  Comparing the two meshes by mesh-space z is
misleading because the two domes have different z origins (Oblivion's lowest
ring is BELOW the horizon at -3.65 deg, Skyrim's starts exactly at 0.00 deg).

This interpolates both domes onto a common elevation axis and reports:
  * alpha(elev)      -- how opaque the sky dome is at that view angle
  * R/G/B mask(elev) -- which NAM0 gradient stop dominates there
so the horizon-band difference can be stated as a number instead of an
impression.
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


def rings(path):
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

    out = {}
    for x, y, z, R, G, B, A in rows:
        rr = math.hypot(x, y)
        elev = math.degrees(math.atan2(z, rr)) if rr > 1e-6 else 90.0
        key = round(elev, 2)
        acc = out.setdefault(key, [0, 0.0, 0.0, 0.0, 0.0])
        acc[0] += 1
        acc[1] += R
        acc[2] += G
        acc[3] += B
        acc[4] += A
    return sorted((k, v[1] / v[0], v[2] / v[0], v[3] / v[0], v[4] / v[0])
                  for k, v in out.items())


def sample(curve, elev):
    """Linear interpolation of (R,G,B,A) at an elevation."""
    if elev <= curve[0][0]:
        return curve[0][1:]
    if elev >= curve[-1][0]:
        return curve[-1][1:]
    for i in range(len(curve) - 1):
        a, b = curve[i], curve[i + 1]
        if a[0] <= elev <= b[0]:
            t = (elev - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
            return tuple(a[1 + k] + t * (b[1 + k] - a[1 + k]) for k in range(4))
    return curve[-1][1:]


curves = {}
for label, path in DOMES:
    try:
        curves[label] = rings(path)
    except Exception as e:
        print(label, 'FAILED', e)

print('=' * 84)
print('DOME ALPHA vs ELEVATION ANGLE  (what the player sees looking up/down)')
print('=' * 84)
print(f'{"elev°":>7s} | {"OB alpha":>9s} {"SK alpha":>9s} {"diff":>7s} | '
      f'{"OB mask RGB":>20s} | {"SK mask RGB":>20s}')
axis = [-4, -3, -2, -1, -0.5, 0, 0.5, 1, 1.5, 2, 3, 4, 5, 7.5, 10, 15, 20,
        30, 45, 60, 90]
for e in axis:
    ob = sample(curves['OBLIVION'], e)
    sk = sample(curves['SKYRIM'], e)
    print(f'{e:7.2f} | {ob[3]:9.3f} {sk[3]:9.3f} {ob[3]-sk[3]:+7.3f} | '
          f'{ob[0]:6.3f}{ob[1]:7.3f}{ob[2]:7.3f} | '
          f'{sk[0]:6.3f}{sk[1]:7.3f}{sk[2]:7.3f}')

print()
print('=' * 84)
print('WHERE EACH DOME REACHES FULL OPACITY')
print('=' * 84)
for label in ('OBLIVION', 'SKYRIM'):
    c = curves[label]
    first_full = next((k for k, R, G, B, A in c if A >= 0.999), None)
    first_any = next((k for k, R, G, B, A in c if A > 0.001), None)
    lowest = c[0][0]
    print(f'  {label:9s} lowest ring {lowest:+6.2f}°   alpha>0 at '
          f'{first_any:+6.2f}°   alpha=1 at {first_full:+6.2f}°')

print()
print('=' * 84)
print('THE HORIZON BAND: integrated "missing sky" below the full-opacity angle')
print('=' * 84)
print('A dome that is still translucent at low elevation lets the FOG/terrain')
print('show through; one that is already opaque paints sky colour there.')
for label in ('OBLIVION', 'SKYRIM'):
    c = curves[label]
    lo = c[0][0]
    hi = next((k for k, R, G, B, A in c if A >= 0.999), c[-1][0])
    # integrate alpha over the band, and the band width
    n = 200
    tot = 0.0
    for i in range(n):
        e = lo + (hi - lo) * (i + 0.5) / n
        tot += sample(c, e)[3]
    tot /= n
    print(f'  {label:9s} band {lo:+.2f}°..{hi:+.2f}° '
          f'(width {hi-lo:5.2f}°)  mean alpha in band {tot:.3f}')
