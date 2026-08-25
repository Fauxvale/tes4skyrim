"""Test the TEMPLATE approach: vanilla weather + only Tier-1 Oblivion overrides.

The A/B rounds established that our converted weather blooms because its NAM0
colour table runs broadly hot, and that a record inheriting vanilla's colour
table, imagespaces, ambient cube and layer setup renders clean.  The template
design follows from that: start from the vanilla weather of the matching
class, then override ONLY the fields that carry Oblivion's visual identity.

Tier 1, measured over Oblivion.esm's 37 weathers (distinct values / 37):

    cloud textures   12-13   the single most recognisable thing
    NAM0 SkyUpper    19      \\
    NAM0 SkyLower    19       |  the sky gradient -- highest-variance
    NAM0 Horizon     18       |  authored colours (stdev 77-81)
    NAM0 Fog         22      /   also carries the Deadlands red
    DATA class        6      picks the template and drives precipitation

Everything else is inherited from the template: IMSP (all 148 of ours are
synthesized), DALC, PNAM/JNAM/LNAM/NAM1 (layer setup belongs to Skyrim's
29-shape dome), NAM0 slots 13-16 (no TES4 source), and TNAM -- 48 sky statics
vanilla ships and our converter never writes at all.

This plugin builds one weather per override so a regression can be attributed:

    TPLbase      pure vanilla template + our EDID   (control, expect clean)
    TPLtex       template + Oblivion cloud TEXTURES only
    TPLsky       template + Oblivion SkyUpper/SkyLower only
    TPLhorizon   template + Oblivion Horizon only
    TPLfog       template + Oblivion Fog (near+far colour) only
    TPLfnam      template + Oblivion FNAM fog DISTANCES only
    TPLall       template + ALL Tier-1 overrides   <-- the candidate
    TPLallraw    same as TPLall but colours copied RAW (no luminance match),
                 to show whether the luminance matching is load-bearing

Colour overrides are luminance-matched to the template per slot and time,
hue preserved, DARKEN-ONLY -- so Oblivion's hue and its relative
between-weather variation survive, but nothing exceeds what Skyrim's renderer
expects.  TPLallraw is the same data without that step, which is what the
current converter effectively does.

Usage:
  python tools/make_sky_template_esp.py
  python tools/make_sky_template_esp.py --weather Cloudy --template SkyrimCloudy
  python tools/make_sky_template_esp.py --out SkyTPL.esp
"""
import argparse
import json
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tes5_import.writer import (pack_record, pack_subrecord,
                                pack_tes4_header, pack_top_group,
                                _count_records_and_groups)

# TES5 NAM0 slot indices
SKY_UPPER, FOG_NEAR, AMBIENT, SUNLIGHT = 0, 1, 3, 4
SUN, STARS, SKY_LOWER, HORIZON = 5, 6, 7, 8
FOG_FAR = 12

# TES4 NAM0 slot indices (wbDefinitionsTES4 wbWeatherColors)
T4_SKY_UPPER, T4_FOG, T4_CLOUDS_LOWER, T4_AMBIENT = 0, 1, 2, 3
T4_SUNLIGHT, T4_SUN, T4_STARS, T4_SKY_LOWER = 4, 5, 6, 7
T4_HORIZON, T4_CLOUDS_UPPER = 8, 9

# Tier-1 colour overrides: TES5 slot <- TES4 slot.  TES4's single Fog feeds
# both of TES5's fog slots, exactly as the main converter does.
SKY_GRADIENT = {SKY_UPPER: T4_SKY_UPPER, SKY_LOWER: T4_SKY_LOWER}
HORIZON_MAP = {HORIZON: T4_HORIZON}
FOG_MAP = {FOG_NEAR: T4_FOG, FOG_FAR: T4_FOG}


def subrecords(data):
    out, i = [], 0
    while i + 6 <= len(data):
        sig = data[i:i + 4]
        size = struct.unpack('<H', data[i + 4:i + 6])[0]
        i += 6
        out.append((sig, data[i:i + size]))
        i += size
    return out


def read_tes5(path, want):
    buf = open(path, 'rb').read()
    hs = struct.unpack('<I', buf[4:8])[0]
    out = {s: {} for s in want}
    i = hs + 24
    while i + 24 <= len(buf):
        if buf[i:i + 4] != b'GRUP':
            break
        gs = struct.unpack('<I', buf[i + 4:i + 8])[0]
        lbl = buf[i + 8:i + 12]
        if struct.unpack('<i', buf[i + 12:i + 16])[0] == 0 and lbl in out:
            j, end = i + 24, i + gs
            while j + 24 <= end:
                size, flags = struct.unpack('<II', buf[j + 4:j + 12])
                fid = struct.unpack('<I', buf[j + 12:j + 16])[0]
                p = buf[j + 24:j + 24 + size]
                if flags & 0x00040000:
                    p = zlib.decompress(p[4:])
                out[lbl][fid] = (flags & ~0x00040000, subrecords(p))
                j += 24 + size
        i += gs
    return out


def read_export(path):
    """Parse the TES4 export dump into {EditorID: {key: value}}."""
    txt = open(path, encoding='utf-8', errors='replace').read()
    out = {}
    for chunk in txt.split('---RECORD_BEGIN---'):
        if 'Signature=WTHR' not in chunk:
            continue
        d = {}
        for line in chunk.splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                d[k.strip()] = v.strip()
        if d.get('EditorID'):
            out[d['EditorID']] = d
    return out


def ed(subs):
    return next((v.rstrip(b'\x00').decode('ascii', 'replace')
                 for k, v in subs if k == b'EDID'), None)


def get(subs, sig):
    return next((v for k, v in subs if k == sig), None)


def lum(r, g, b):
    return 0.299 * r + 0.587 * g + 0.114 * b


def prefix(path):
    """Our converted textures live under the tes4\\ namespace."""
    p = path.replace('\\\\', '\\').replace('/', '\\')
    if p.lower().startswith('textures\\'):
        p = p[9:]
    return p if p.lower().startswith('tes4\\') else 'tes4\\' + p


def apply_colours(nam0, t4raw, mapping, template, match):
    """Copy TES4 colours into the TES5 table for the given slot mapping.

    match=True luminance-matches each colour to the TEMPLATE's value for that
    slot and time (hue preserved, darken only).  match=False copies raw, which
    is what the current converter effectively does.
    """
    out = bytearray(nam0)
    for t5, t4 in mapping.items():
        for t in range(4):
            src = (t4 * 4 + t) * 4
            dst = (t5 * 4 + t) * 4
            r, g, b = t4raw[src], t4raw[src + 1], t4raw[src + 2]
            if match:
                cur = lum(r, g, b)
                tgt = lum(template[dst], template[dst + 1], template[dst + 2])
                if cur > 1.0 and tgt < cur:
                    k = tgt / cur
                    r, g, b = (min(255, round(r * k)), min(255, round(g * k)),
                               min(255, round(b * k)))
            out[dst:dst + 3] = bytes((r, g, b))
    return bytes(out)


# Dome layers whose geometry is a narrow BAND with strip UVs rather than a
# full-dome sheet.  Measured from the 84 vanilla weathers: every weather that
# enables one of these points it at a *Horizon* or *Fill* texture, never at a
# dome sheet.  Oblivion authors no equivalent art, so these keep the
# template's own texture.
HORIZON_STRIP_LAYERS = frozenset({1} | set(range(15, 27)))
FILL_LAYER = 28
ROLE_SPECIFIC_LAYERS = HORIZON_STRIP_LAYERS | {FILL_LAYER}


def cloud_sig(layer):
    return (bytes([0x30 + layer]) if layer <= 16
            else bytes([0x41 + (layer - 17)])) + b'0TX'


def build(edid, fid, flags, tpl_subs, t4, *, textures=False, colours=None,
          fnam=False, data_class=False, match=True):
    """One test weather: the template, with the requested Tier-1 overrides."""
    t4raw = bytes.fromhex(t4['NAM0.Data']) if t4.get('NAM0.Data') else None
    tpl_nam0 = get(tpl_subs, b'NAM0')
    nam0 = tpl_nam0
    if colours and t4raw:
        mapping = {}
        for m in colours:
            mapping.update(m)
        nam0 = apply_colours(tpl_nam0, t4raw, mapping, tpl_nam0, match)

    upper = t4.get('DNAM.UpperCloudLayer', '')
    lower = t4.get('CNAM.LowerCloudLayer', '')

    body = b''
    for sig, v in tpl_subs:
        if sig == b'EDID':
            body += pack_subrecord('EDID', edid.encode('utf-8') + b'\x00')
        elif len(sig) == 4 and sig[1:] == b'0TX':
            if textures:
                layer = (sig[0] - 0x30 if sig[0] <= 0x40
                         else 17 + (sig[0] - 0x41))
                # ROLE-SPECIFIC LAYERS KEEP THE TEMPLATE'S OWN ART.
                #
                # Vanilla's dome bands are not interchangeable: layers 1 and
                # 15-26 are HORIZON STRIPS and 28 is the FILL wash, each a
                # narrow band whose UVs expect strip art.  An Oblivion
                # full-dome sheet mapped onto a strip is stretched across a
                # band it was never drawn for, which reads as clouds stopping
                # at a hard line -- the discontinuity seen in-game.
                #
                # Only the FULL-DOME layers take Oblivion's sheets.
                if layer in ROLE_SPECIFIC_LAYERS:
                    body += pack_subrecord(sig.decode('ascii'), v)
                    continue
                pick = upper if layer < 8 else lower
                if pick:
                    body += pack_subrecord(sig.decode('ascii'),
                                           prefix(pick).encode('utf-8') + b'\x00')
                    continue
            body += pack_subrecord(sig.decode('ascii'), v)
        elif sig == b'NAM0':
            body += pack_subrecord('NAM0', nam0)
        elif sig == b'FNAM' and fnam:
            f = list(struct.unpack('<8f', v[:32]))
            # Distances are genuinely authored per weather; power/max are not
            # in TES4 at all, so they stay the template's.  Negative near
            # planes are clamped -- Oblivion authors them, vanilla never does,
            # and they make fog full-density everywhere.
            f[0] = max(0.0, float(t4.get('FNAM.FogDayNear', f[0])))
            f[1] = float(t4.get('FNAM.FogDayFar', f[1]))
            f[2] = max(0.0, float(t4.get('FNAM.FogNightNear', f[2])))
            f[3] = float(t4.get('FNAM.FogNightFar', f[3]))
            if f[1] <= f[0]:
                f[1] = f[0] + 9000.0
            if f[3] <= f[2]:
                f[3] = f[2] + 9000.0
            body += pack_subrecord('FNAM', struct.pack('<8f', *f))
        elif sig == b'DATA' and data_class:
            d = bytearray(v)
            cls = int(t4.get('DATA.Classification', '0') or 0) & 0x0F
            if cls == 0:
                cls = 0x01
            d[11] = cls
            d[0] = int(t4.get('DATA.WindSpeed', d[0]) or d[0]) & 0xFF
            body += pack_subrecord('DATA', bytes(d))
        else:
            body += pack_subrecord(sig.decode('ascii'), v)
    return pack_record('WTHR', fid, flags, body)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--weather', default='Clear',
                    help='Oblivion weather EditorID to convert')
    ap.add_argument('--template', default='SkyrimClear',
                    help='vanilla weather to use as the template')
    ap.add_argument('--export',
                    default='export/Oblivion.esm/WTHR.txt')
    ap.add_argument('--source', default='output/Oblivion.esm/Oblivion.esm',
                    help='converted plugin (used only as a master name)')
    ap.add_argument('--skyrim-esm', default=None)
    ap.add_argument('--outdir', default='output/Oblivion.esm')
    ap.add_argument('--out', default='SkyTPL.esp')
    args = ap.parse_args()

    skyrim_esm = args.skyrim_esm
    if not skyrim_esm:
        cfg = json.load(open('conversion_config.json', encoding='utf-8'))
        skyrim_esm = os.path.join(cfg['tes5DataPath'], 'Skyrim.esm')
    for p in (skyrim_esm, args.export):
        if not os.path.isfile(p):
            sys.exit(f'not found: {p}')

    V = read_tes5(skyrim_esm, (b'WTHR',))
    tpl = next(((f, s) for f, s in V[b'WTHR'].values()
                if ed(s) == args.template), None)
    if not tpl:
        sys.exit(f'template {args.template!r} not found')
    flags, tpl_subs = tpl

    t4all = read_export(args.export)
    if args.weather not in t4all:
        sys.exit(f'{args.weather!r} not in {args.export}')
    t4 = t4all[args.weather]

    plan = [
        ('TPLbase',    dict()),
        ('TPLtex',     dict(textures=True)),
        ('TPLsky',     dict(colours=[SKY_GRADIENT])),
        ('TPLhorizon', dict(colours=[HORIZON_MAP])),
        ('TPLfog',     dict(colours=[FOG_MAP])),
        ('TPLfnam',    dict(fnam=True)),
        ('TPLall',     dict(textures=True, fnam=True, data_class=True,
                            colours=[SKY_GRADIENT, HORIZON_MAP, FOG_MAP])),
        ('TPLallraw',  dict(textures=True, fnam=True, data_class=True,
                            colours=[SKY_GRADIENT, HORIZON_MAP, FOG_MAP],
                            match=False)),
    ]

    records = b''
    listing = []
    for n, (tag, kw) in enumerate(plan):
        fid = (2 << 24) | (0x000C00 + n)
        edid = 'TES4' + tag
        records += build(edid, fid, flags, tpl_subs, t4, **kw)
        listing.append((edid, fid, tag, kw))

    group = pack_top_group('WTHR', records)
    masters = ['Skyrim.esm', os.path.basename(args.source)]
    header = pack_tes4_header(
        masters=masters, num_records=_count_records_and_groups(group),
        next_object_id=0x1000, is_esm=False)

    os.makedirs(args.outdir, exist_ok=True)
    out_path = os.path.join(args.outdir, args.out)
    with open(out_path, 'wb') as fh:
        fh.write(header + group)

    print(f'Wrote {out_path}')
    print(f'  template : vanilla {args.template}')
    print(f'  overrides: Oblivion {args.weather}')
    print(f'  masters  : {masters}')
    print()
    for edid, fid, tag, kw in listing:
        what = ', '.join(k for k in ('textures', 'colours', 'fnam',
                                     'data_class') if kw.get(k)) or 'none'
        if tag == 'TPLallraw':
            what += ' (RAW, no luminance match)'
        print(f'  {fid:08X}  {edid:16s} {what}')


if __name__ == '__main__':
    main()
