"""Does the dome-fade difference explain "the horizon is overbright"?

Mechanism established:
  * Oblivion's dome cross-fades SKY -> FOG over 13.81 deg (-3.65..+10.16),
    reaching only alpha 0.623 at the true horizon (0 deg).
  * Skyrim's dome cross-fades over 2.09 deg (0.00..+2.09) and is fully opaque
    by +2.09 deg.  Below 0 deg it has NO geometry at all.
  * Neither dome has a NiAlphaProperty; both sky pixel shaders pass vertex
    alpha straight through, so the fade is the vertex alpha.

Consequence: in Oblivion the authored Horizon colour is NEVER shown at full
strength near the horizon -- it is always diluted toward the FOG colour.  In
Skyrim the same authored value IS shown at full strength just 2 degrees up.

So the correct normalisation is not a blanket scale: it is
    effective_horizon_oblivion(e) = Horizon*a_ob(e) + Fog*(1 - a_ob(e))
and Skyrim should be given a Horizon value that reproduces that, i.e. the
Oblivion composite evaluated where Skyrim is already opaque.

This runs the whole converted weather set to see how large the correction is
and whether it depends on the Horizon-vs-Fog contrast (it should: when they are
equal, the fade is invisible and no correction is needed).
"""
import math
import os
import statistics
import struct
import sys
import zlib

sys.path.insert(0, r"c:\Users\Bryant\Documents\Code\TESConversion")
HERE = os.path.dirname(os.path.abspath(__file__))
OURS = r"c:\Users\Bryant\Documents\Code\TESConversion\output\Oblivion.esm\Oblivion.esm"
OB = r"c:\Users\Bryant\Documents\Code\TESConversion\export\Oblivion.esm\meshes\sky\atmosphere.nif"
SK = os.path.join(HERE, 'sse_atmosphere.nif')
TIMES = ['Sunrise', 'Day', 'Sunset', 'Night']


def curve(path):
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
                rr = math.hypot(v.x, v.y)
                e = math.degrees(math.atan2(v.z, rr)) if rr > 1e-6 else 90.0
                rows.append((e, c.r, c.g, c.b, c.a))
    for r in (d.roots or []):
        walk(r)
    agg = {}
    for e, R, G, B, A in rows:
        k = round(e, 2)
        a = agg.setdefault(k, [0, 0, 0, 0, 0])
        a[0] += 1; a[1] += R; a[2] += G; a[3] += B; a[4] += A
    return sorted((k, v[1]/v[0], v[2]/v[0], v[3]/v[0], v[4]/v[0])
                  for k, v in agg.items())


def at(c, e):
    if e <= c[0][0]:
        return c[0][1:]
    if e >= c[-1][0]:
        return c[-1][1:]
    for i in range(len(c)-1):
        a, b = c[i], c[i+1]
        if a[0] <= e <= b[0]:
            t = (e-a[0])/(b[0]-a[0]) if b[0] > a[0] else 0.0
            return tuple(a[1+k]+t*(b[1+k]-a[1+k]) for k in range(4))
    return c[-1][1:]


def subrecords(data):
    out, i = [], 0
    while i + 6 <= len(data):
        sig = data[i:i+4]
        size = struct.unpack('<H', data[i+4:i+6])[0]
        i += 6
        out.append((sig, data[i:i+size]))
        i += size
    return out


def read_wthr(path):
    buf = open(path, 'rb').read()
    out = {}
    i = struct.unpack('<I', buf[4:8])[0] + 24
    while i + 24 <= len(buf):
        if buf[i:i+4] != b'GRUP':
            break
        gs = struct.unpack('<I', buf[i+4:i+8])[0]
        if gs < 24:
            break
        if (struct.unpack('<i', buf[i+12:i+16])[0] == 0
                and buf[i+8:i+12] == b'WTHR'):
            j, end = i + 24, i + gs
            while j + 24 <= end:
                size, flags = struct.unpack('<II', buf[j+4:j+12])
                p = buf[j+24:j+24+size]
                if flags & 0x00040000:
                    p = zlib.decompress(p[4:])
                subs = subrecords(p)
                ed = next((v.rstrip(b'\x00').decode('ascii', 'replace')
                           for s, v in subs if s == b'EDID'), None)
                n0 = next((v for s, v in subs if s == b'NAM0'), None)
                if ed and n0 and len(n0) >= 272:
                    out[ed] = n0
                j += 24 + size
        i += gs
    return out


def lum(c):
    return 0.299*c[0]+0.587*c[1]+0.114*c[2]


cob, csk = curve(OB), curve(SK)
w = read_wthr(OURS)
HOR, SKYLO, SKYUP, FOGNEAR = 8, 7, 0, 1

# The elevation at which Skyrim first shows the Horizon colour undiluted
SK_OPAQUE = next(k for k, R, G, B, A in csk if A >= 0.999)
print(f'Skyrim dome becomes opaque at {SK_OPAQUE:+.2f} deg')
print(f'Oblivion alpha there = {at(cob, SK_OPAQUE)[3]:.3f}')
print()
print('=' * 96)
print('PER-WEATHER: what Oblivion actually SHOWS at the horizon vs what we WRITE')
print('=' * 96)
print(f'{"weather":22s} {"time":8s} {"Horiz":>6s} {"Fog":>6s} {"contrast":>9s} '
      f'{"OB shown":>9s} {"SK shown":>9s} {"SK/OB":>7s}')

rows = []
for ed in sorted(w):
    n0 = w[ed]
    for t, tn in enumerate(TIMES):
        def slot(s):
            o = (s*4+t)*4
            return (n0[o], n0[o+1], n0[o+2])
        Lh, Ll, Lu = lum(slot(HOR)), lum(slot(SKYLO)), lum(slot(SKYUP))
        Lf = lum(slot(FOGNEAR))
        # what each engine shows across the visible horizon band 0..+3 deg
        def shown(c):
            tot = 0.0
            n = 0
            e = 0.0
            while e <= 3.0001:
                R, G, B, A = at(c, e)
                sky = R*Lh + G*Ll + B*Lu
                tot += sky*A + Lf*(1-A)
                n += 1
                e += 0.25
            return tot/n
        so, ss = shown(cob), shown(csk)
        rows.append((ed, tn, Lh, Lf, Lh-Lf, so, ss,
                     ss/so if so > 1e-6 else 0.0))

for r in rows[:14]:
    print(f'{r[0][:22]:22s} {r[1]:8s} {r[2]:6.1f} {r[3]:6.1f} {r[4]:+9.1f} '
          f'{r[5]:9.1f} {r[6]:9.1f} {r[7]:7.3f}')

print()
print('=' * 96)
print('SUMMARY OVER ALL %d WEATHER/TIME PAIRS' % len(rows))
print('=' * 96)
ratios = [r[7] for r in rows if r[7] > 0]
contrasts = [r[4] for r in rows]
print(f'  SK/OB horizon brightness ratio: min {min(ratios):.3f} '
      f'median {statistics.median(ratios):.3f} max {max(ratios):.3f}')
print(f'  Horizon-minus-Fog contrast:     min {min(contrasts):+.1f} '
      f'median {statistics.median(contrasts):+.1f} max {max(contrasts):+.1f}')

# correlation between contrast and the error
import math as _m


def pearson(p):
    p = [(a, b) for a, b in p if b > 0]
    xs = [a for a, _ in p]
    ys = [b for _, b in p]
    mx, my = sum(xs)/len(xs), sum(ys)/len(ys)
    num = sum((a-mx)*(b-my) for a, b in p)
    dx = _m.sqrt(sum((a-mx)**2 for a in xs))
    dy = _m.sqrt(sum((b-my)**2 for b in ys))
    return num/(dx*dy) if dx and dy else 0.0


print(f'  corr(contrast, SK/OB ratio) = '
      f'{pearson([(r[4], r[7]) for r in rows]):+.3f}')
print()
print('  A strong NEGATIVE correlation confirms the mechanism: the bigger the')
print('  gap between the authored Horizon and Fog colours, the more Skyrim')
print('  over-shows the Horizon colour relative to Oblivion.')

# how many weathers have horizon brighter than fog (the overbright case)
over = sum(1 for r in rows if r[4] > 0)
print(f'\n  {over} of {len(rows)} weather/time pairs have Horizon BRIGHTER than Fog')
print('  -> for those, Skyrim renders a brighter horizon band than Oblivion.')
