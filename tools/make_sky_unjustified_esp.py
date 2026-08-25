"""A/B the UNJUSTIFIED values in the weather conversion, one at a time.

Every value in the converted WTHR/IMGS path was audited against "can I point
at engine code that requires this?".  The ones that survive are mechanism
(fog Power/Max = the linear-ramp identity, LNAM = the disassembled index
clamp, cloud speed = byte*0.2/254-0.1, the NAM0 slot map = the decompiled
enums).  The ones below are NOT: they are vanilla medians, percentile caps,
or numbers that were simply invented.  Statistics cannot tell you what a
value MEANS, so each one here is a candidate defect.

This builds one weather per unjustified value, each identical to our real
converted `Clear` EXCEPT that one value is reverted to what the mechanism
implies (usually: the authored TES4 data, unmodified).  Load the plugin,
force each weather in turn, and the one that looks right names the culprit.

ROUND 1 (reviewed in game):
    UJbase        our converted Clear, unchanged        (control)
    UJnorm        NAM0 luminance normalisation REMOVED
    UJp90         NAM0 vanilla-p90 colour caps removed
    UJraw         both removed -- authored TES4 colour
    UJpnam        PNAM cloud-tint p90 cap removed
    UJalpha       JNAM cloud alphas -> fully opaque
    UJtrans       DATA TransDelta x125/255 -> passthrough
    UJxdrift      QNAM invented -0.35 X drift -> none (0x7F)
    UJallraw      every one of the above reverted at once

  Result: norm+p90 together DO fix the sun/bloom blowout (perhaps too far),
  but night goes very blue and the day sky too dark.  With norm off the
  colours are right as authored and the blowout returns.  Nothing else was
  noticeable.  Stars blanking was accepted and is no longer under test.

ROUND 3 -- CLOUD LAYER PLACEMENT (built from Cloudy, not Clear):
    CLbase        our converted Cloudy as shipped   (control: NO CLOUDS)
    CLvanilla     the exact layer set SkyrimCloudy draws
    CLnofog       same without the 15_CDFog horizon wash
    CLdomeonly    dome body + 14_CDLower, no horizon strips
    CLupper       the upper-dome sheets instead of the dome+horizon body
    CLminimal     one full-dome shape per sheet, nothing else
    CLopaque      vanilla layout with every alpha 1.0 (coverage check)

ROUND 2 -- the palette and the blowout are DIFFERENT problems:
    UJknee        soft knee 160->200, everything below 160 as authored
    UJkneeSoft    gentler knee 170->220
    UJkneeHard    harder knee 150->190
    UJkneeSun     knee 160->200 + the Sun slot pulled down on its own
    UJsunonly     ONLY the Sun slot touched; every other slot authored

Each variant is a full override of OUR record, so only the named field
differs from UJbase -- the rest is byte-identical to what the pipeline ships.

Usage:
    python tools/make_sky_unjustified_esp.py
    python tools/make_sky_unjustified_esp.py --weather Cloudy
    python tools/make_sky_unjustified_esp.py --out SkyUJ2.esp
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

TIMES = ('Sunrise', 'Day', 'Sunset', 'Night')

# TES5 NAM0 slots the normalisation touches, and the TES4 slot each is fed
# from (mirrors _NAM0_NORM_SOURCES in the converter).
NORM_SLOTS = {0: 0, 1: 1, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8, 12: 1}
T5_SLOTS = 17


def lum(r, g, b):
    return 0.299 * r + 0.587 * g + 0.114 * b


def subrecords(data):
    out, i = [], 0
    while i + 6 <= len(data):
        sig = data[i:i + 4]
        size = struct.unpack('<H', data[i + 4:i + 6])[0]
        i += 6
        out.append((sig, data[i:i + size]))
        i += size
    return out


def read_records(path, want):
    """-> {edid or formid: (flags, formid, [(sig, bytes)])}"""
    buf = open(path, 'rb').read()
    out = {}
    i = struct.unpack('<I', buf[4:8])[0] + 24
    while i + 24 <= len(buf):
        if buf[i:i + 4] != b'GRUP':
            break
        gs = struct.unpack('<I', buf[i + 4:i + 8])[0]
        if gs < 24:
            break
        if (struct.unpack('<i', buf[i + 12:i + 16])[0] == 0
                and buf[i + 8:i + 12] == want):
            j, end = i + 24, i + gs
            while j + 24 <= end:
                size, flags = struct.unpack('<II', buf[j + 4:j + 12])
                fid = struct.unpack('<I', buf[j + 12:j + 16])[0]
                p = buf[j + 24:j + 24 + size]
                if flags & 0x00040000:
                    try:
                        p = zlib.decompress(p[4:])
                    except Exception:
                        j += 24 + size
                        continue
                subs = subrecords(p)
                ed = next((v.rstrip(b'\x00').decode('ascii', 'replace')
                           for s, v in subs if s == b'EDID'), None)
                out[ed if ed else fid] = (flags & ~0x00040000, fid, subs)
                j += 24 + size
        i += gs
    return out


def parse_t4(path):
    recs, cur = [], None
    for line in open(path, encoding='utf-8', errors='replace'):
        line = line.rstrip('\n')
        if line == '---RECORD_BEGIN---':
            cur = {}
        elif line == '---RECORD_END---':
            if cur:
                recs.append(cur)
            cur = None
        elif cur is not None and '=' in line:
            k, v = line.split('=', 1)
            cur[k] = v
    return {r.get('EditorID'): r for r in recs}


def t4_nam0(rec):
    raw = bytes.fromhex(rec['NAM0.Data'])
    return {(s, t): (raw[(s * 4 + t) * 4], raw[(s * 4 + t) * 4 + 1],
                     raw[(s * 4 + t) * 4 + 2])
            for s in range(10) for t in range(4)}


WRITE_ORDER = (
    [b'EDID'] + [bytes([c]) + b'0TX' for c in
                 list(range(0x30, 0x41)) + list(range(0x41, 0x4D))]
    + [b'LNAM', b'MNAM', b'NNAM', b'RNAM', b'QNAM', b'PNAM', b'JNAM',
       b'NAM0', b'FNAM', b'DATA', b'NAM1', b'SNAM', b'TNAM', b'IMSP',
       b'HNAM', b'DALC', b'NAM2', b'NAM3', b'GNAM']
)


def rebuild(edid, fid, flags, subs, overrides):
    """Emit a WTHR from `subs`, replacing signatures present in `overrides`.

    `overrides` maps sig -> list of payloads (empty list drops the field).
    """
    body = pack_subrecord('EDID', edid.encode('utf-8') + b'\x00')
    for sig in WRITE_ORDER:
        if sig == b'EDID':
            continue
        if sig in overrides:
            vals = overrides[sig]
        else:
            vals = [v for s, v in subs if s == sig]
        for v in vals:
            body += pack_subrecord(sig.decode('latin1'), v)
    return pack_record('WTHR', fid, flags, body)


# --------------------------------------------------------------------------
# One builder per unjustified value.  Each returns an `overrides` dict.
# --------------------------------------------------------------------------

def ov_denormalise_nam0(subs, t4, cap=True, scale=True):
    """Rewrite NAM0 straight from the TES4 table.

    The converter scales every colour so the PLUGIN's median luminance lands
    on vanilla's, then caps at vanilla's p90.  Both are census artifacts, and
    both engines were since shown to run the IDENTICAL 3-stop vertex blend
    (Oblivion vs 0x02767c, Skyrim Sky.hlsl), so the mechanism says these
    colours transfer 1:1.

    scale=False removes the median normalisation; cap=False removes the p90
    clamp; both False writes the authored TES4 colour untouched.
    """
    cur = next((v for s, v in subs if s == b'NAM0'), None)
    if cur is None or len(cur) < T5_SLOTS * 16:
        return {}
    out = bytearray(cur)
    src = t4_nam0(t4)
    from tes5_import.record_types.dialog_misc import (_NAM0_VANILLA_LUM,
                                                      _NAM0_K)
    for t5, t4slot in NORM_SLOTS.items():
        for t in range(4):
            r, g, b = src[(t4slot, t)]
            if scale:
                k = _NAM0_K.get((t5, t), 1.0)
                r, g, b = r * k, g * k, b * k
            if cap and t5 in _NAM0_VANILLA_LUM:
                lim = _NAM0_VANILLA_LUM[t5][1][t]
                L = lum(r, g, b)
                if L > lim and L > 0:
                    f = lim / L
                    r, g, b = r * f, g * f, b * f
            o = (t5 * 4 + t) * 4
            out[o:o + 3] = bytes((min(255, round(r)), min(255, round(g)),
                                  min(255, round(b))))
    return {b'NAM0': [bytes(out)]}


def ov_knee(subs, t4, kn=160.0, ce=200.0, sun_extra=None):
    """SOFT-KNEE highlight compression, authored colours otherwise untouched.

    The measured problem is not a broadly hot palette -- the two populations
    agree at the bottom (TES4 p50 76.9 vs vanilla 83.8) and only diverge at
    the top (p90 204.6 vs 168.0, p95 243.1 vs 193.5).  A uniform per-slot
    scale therefore darkens midtones to fix highlights, which is what makes
    the day sky dull; and a PER-TIME scale additionally wrecks the authored
    day/night curve (Oblivion authors night at 7-12% of day; the current code
    turns that into 22-55%).

    This leaves everything below `kn` exactly as authored and remaps
    kn..255 into kn..ce, preserving hue by scaling all three channels by the
    same factor.

    `sun_extra` optionally applies a harder ceiling to the Sun slot alone,
    which is the one genuine outlier (TES4 day median 193 vs vanilla 42.5).
    """
    cur = next((v for s, v in subs if s == b'NAM0'), None)
    if cur is None or len(cur) < T5_SLOTS * 16:
        return {}
    out = bytearray(cur)
    src = t4_nam0(t4)

    def squash(r, g, b, knee_, ceil_):
        L = lum(r, g, b)
        if L <= knee_ or L <= 0:
            return r, g, b
        target = knee_ + (L - knee_) * (ceil_ - knee_) / (255.0 - knee_)
        f = target / L
        return r * f, g * f, b * f

    for t5, t4slot in NORM_SLOTS.items():
        for t in range(4):
            r, g, b = src[(t4slot, t)]
            if t5 == 5 and sun_extra is not None:
                r, g, b = squash(r, g, b, sun_extra[0], sun_extra[1])
            else:
                r, g, b = squash(r, g, b, kn, ce)
            o = (t5 * 4 + t) * 4
            out[o:o + 3] = bytes((min(255, round(r)), min(255, round(g)),
                                  min(255, round(b))))
    return {b'NAM0': [bytes(out)]}


def ov_pnam_uncapped(subs, t4):
    """PNAM cloud tints straight from TES4, no vanilla-p90 cap."""
    cur = next((v for s, v in subs if s == b'PNAM'), None)
    if cur is None or len(cur) < 512:
        return {}
    out = bytearray(cur)
    src = t4_nam0(t4)
    T4_CLOUDS_LOWER, T4_CLOUDS_UPPER = 2, 9
    # layer 0 = upper sheet, layer 1 = lower sheet (the shipped plan)
    for layer, t4slot in ((0, T4_CLOUDS_UPPER), (1, T4_CLOUDS_LOWER)):
        for t in range(4):
            r, g, b = src[(t4slot, t)]
            o = (layer * 4 + t) * 4
            out[o:o + 3] = bytes((r, g, b))
    return {b'PNAM': [bytes(out)]}


def ov_alpha_opaque(subs):
    """JNAM -> 1.0 on every authored layer.

    The shipped alphas (0.60/0.50/0.60/0.50 and 1.00/1.00/0.75/1.00) are
    vanilla per-time medians.  TES4 authors no cloud alpha at all, so the
    mechanism-neutral reading is 'the sheet is drawn as authored' = opaque.
    """
    cur = next((v for s, v in subs if s == b'JNAM'), None)
    lnam = next((v for s, v in subs if s == b'LNAM'), None)
    if cur is None or len(cur) < 512:
        return {}
    n = struct.unpack('<I', lnam)[0] if lnam and len(lnam) == 4 else 2
    vals = list(struct.unpack('<128f', cur[:512]))
    for layer in range(min(n, 32)):
        for t in range(4):
            vals[layer * 4 + t] = 1.0
    return {b'JNAM': [struct.pack('<128f', *vals)]}


def ov_trans_passthrough(subs, t4):
    """DATA TransDelta: undo the x125/255 rescale."""
    cur = next((v for s, v in subs if s == b'DATA'), None)
    if cur is None or len(cur) < 19:
        return {}
    out = bytearray(cur)
    out[3] = max(0, min(255, int(t4.get('DATA.TransDelta', 0) or 0)))
    return {b'DATA': [bytes(out)]}


# --------------------------------------------------------------------------
# CLOUD LAYER PLACEMENT
#
# The engine loads the HARDCODED Meshes\\Sky\\Clouds.nif (string at SkyrimSE.exe
# 0x169a538) -- our converted tes4\\sky\\clouds.nif is never used, because CLMT
# has no cloud-mesh field at all (its MODL is the NIGHT SKY / stars mesh; see
# wbRecord(CLMT) in wbDefinitionsTES5.pas).  So layer N binds to the Nth shape
# of VANILLA's dome, and that mesh names its shapes:
#
#    0  01_CDUpper_04           26 verts   20.8..88.4 deg
#    1  02_CDUpper_04_E          9 verts    4.5..23.9
#    3  03_CDUpper_01           76 verts    4.1..71.0
#    6  05_CDUpper_02           64 verts    2.3..79.4
#    8  07_CDDome_Horizon       98 verts   -0.0..43.1
#    9  08_CDDome_Horizon_E     42 verts   -0.0..43.1
#   10  08_CDDome_Horizon_W     42 verts   -0.0..43.1
#   11  09_CDTop                53 verts   20.6..90.0
#   15..26  12_/13_CDHorizon_*  narrow HORIZON STRIPS, 0..~15 deg
#   27  14_CDLower              89 verts   -0.0..90.0   (full dome)
#   28  15_CDFog                62 verts   -0.9.. 2.9   (horizon wash)
#
# We currently texture ONLY layers 0 and 1 = 35 verts, a zenith cap plus one
# eastern sliver.  SkyrimCloudy draws 8,9,10,11,16,21,22,28 = 409 verts, and
# it explicitly DISABLES 0 and 1 via NAM1 -- i.e. we draw the two bands
# vanilla turns off for cloudy weather.  Hence "no clouds in the sky".
#
# Vanilla usage over the 162 weathers that texture any layer (drawn = NAM1
# bit clear):  layers 8/9/10/11 drawn by 108-111 each, layer 28 by 157,
# layers 0/1 by only 40/22.
#
# A previous revision spread the sheets over 0/3/8/15/21/27/28 and produced
# visibly discontinuous cloud banding.  The cause was NOT "layers with no
# geometry" (the diagnosis that led to the 0/1 collapse) -- it was ROLE
# MISMATCH: a full-dome Oblivion sheet was landing on horizon STRIPS (15,
# 19-26), whose UVs expect narrow banding.  These variants keep sheets on
# role-appropriate shapes.

# ROUND 3 RESULT + the UV measurement that explains it.
#
# In game: CLvanilla put all the clouds AROUND THE HORIZON instead of over the
# sky, CLupper looked the same and equally wrong, while CLdomeonly and
# CLminimal both looked correct (and near-identical to each other).
#
# The UV layout of the shipped Clouds.nif says why -- the shapes are NOT
# interchangeable, they carry different tiling:
#
#     L11 09_CDTop        U span 1.58  V span 1.58   ~1:1 projection
#     L27 14_CDLower      U span 2.27  V span 2.27   ~1:1 projection
#     L 8 07_CDDome_Horizon  U 6.00              tiles 6x around the horizon
#     L 9/10 (_E/_W)         U 2.40              same band, split
#     L15-26 12_/13_CDHorizon_*  V span 0.25     narrow V-sliced STRIPS
#     L28 15_CDFog           U 21.00             tiles 21x, horizon wash
#
# Oblivion authors its two sheets as SINGLE FULL-DOME projections
# (CloudDome:0 3.35x3.35, CloudDome:1 2.97x2.97, both 0..90 deg), so 11 and 27
# are the only structural matches.  Anything with heavy U tiling turns a
# full-sky sheet into a repeating horizon band -- exactly what was seen.
#
# Vanilla confirms the roles by using DEDICATED ART per layer:
#     L11  SkyrimClouds01 / SkyrimCloudsLower0*      (full-dome sheets)
#     L27  SkyrimClouds01 / SkyrimCloudsLower03      (full-dome sheets)
#     L16  SkyrimCloudsHorizon01  50 of 50 times     (purpose-made strip)
#     L28  SkyrimCloudsFill      156 of 157 times    (purpose-made wash)
# We have no strip or fill art, so those layers have nothing correct to put
# on them.
#
# Usage counts: 111 weathers draw L11, 66 draw L27, 64 draw both.
DOME_FULL = (8, 9, 10, 11)        # kept only to rebuild the rejected variant
DOME_UPPER = (3, 6, 11)           # rejected
HORIZON_STRIP = (16, 21, 22)      # rejected: needs strip art we do not have
FOG_WASH = 28                     # rejected: needs SkyrimCloudsFill-style art
SQUARE_UV = (11, 27)              # the only ~1:1 full-dome shapes


def _cloud_sig(layer):
    return (bytes([0x30 + layer]) + b'0TX' if layer <= 16
            else bytes([0x41 + layer - 17]) + b'0TX')


def ov_cloud_layers(subs, upper_layers, lower_layers, fog_layer=None,
                    upper_alpha=(0.60, 0.50, 0.60, 0.50),
                    lower_alpha=(1.00, 1.00, 0.75, 1.00),
                    fog_alpha=(0.50, 0.53, 0.40, 0.50)):
    """Re-place the two authored TES4 sheets onto role-appropriate shapes.

    Rewrites every per-layer array consistently: the <hex>0TX textures, NAM1
    (bit set = layer culled, verified at SkyrimSE.exe 0x3c5c56 where it sets
    APP_CULLED), JNAM alphas, PNAM tints, RNAM/QNAM drift, and LNAM -- which
    is an INDEX CLAMP into RNAM/QNAM/JNAM, so it must span the highest layer
    we author or those layers silently reuse layer 0's values.
    """
    # the two authored sheets, taken from whatever the record currently has
    tex = {}
    for s, v in subs:
        if len(s) == 4 and s[1:] == b'0TX':
            L = s[0] - 0x30 if s[0] <= 0x40 else 17 + (s[0] - 0x41)
            tex[L] = v
    if 0 not in tex:
        return {}
    upper_tex = tex[0]
    lower_tex = tex.get(1, tex[0])

    cur_j = next((v for s, v in subs if s == b'JNAM'), None)
    cur_p = next((v for s, v in subs if s == b'PNAM'), None)
    cur_r = next((v for s, v in subs if s == b'RNAM'), None)
    cur_q = next((v for s, v in subs if s == b'QNAM'), None)
    if not (cur_j and cur_p and cur_r and cur_q):
        return {}

    # the source per-layer values we are relocating (layer 0 = upper sheet,
    # layer 1 = lower sheet in the current shipped plan)
    jn = list(struct.unpack('<128f', cur_j[:512]))
    pn = bytearray(cur_p)
    rn = bytearray(cur_r)
    qn = bytearray(cur_q)
    src_p = {L: bytes(pn[(L * 4 + t) * 4:(L * 4 + t) * 4 + 3])
             for L in (0, 1) for t in range(4)}
    src_p = {}
    for L in (0, 1):
        src_p[L] = [bytes(cur_p[(L * 4 + t) * 4:(L * 4 + t) * 4 + 3])
                    for t in range(4)]
    src_r = {L: cur_r[L] for L in (0, 1)}
    src_q = {L: cur_q[L] for L in (0, 1)}

    out_tex = {}
    new_j = [0.0] * 128
    new_p = bytearray(512)
    new_r = bytearray(b'\x7F' * 32)
    new_q = bytearray(b'\x7F' * 32)

    def place(layer, which, alphas):
        out_tex[layer] = upper_tex if which == 0 else lower_tex
        for t in range(4):
            new_j[layer * 4 + t] = alphas[t]
            o = (layer * 4 + t) * 4
            new_p[o:o + 3] = src_p[which][t]
        new_r[layer] = src_r[which]
        new_q[layer] = src_q[which]

    for L in upper_layers:
        place(L, 0, upper_alpha)
    for L in lower_layers:
        place(L, 1, lower_alpha)
    if fog_layer is not None:
        place(fog_layer, 1, fog_alpha)

    used = sorted(out_tex)
    disabled = 0xFFFFFFFF
    for L in used:
        disabled &= ~(1 << L)

    ov = {}
    # clear every texture slot, then write only the ones we place
    for L in range(32):
        ov[_cloud_sig(L)] = []
    for L, v in out_tex.items():
        ov[_cloud_sig(L)] = [v]
    ov[b'JNAM'] = [struct.pack('<128f', *new_j)]
    ov[b'PNAM'] = [bytes(new_p)]
    ov[b'RNAM'] = [bytes(new_r)]
    ov[b'QNAM'] = [bytes(new_q)]
    ov[b'NAM1'] = [struct.pack('<I', disabled & 0xFFFFFFFF)]
    ov[b'LNAM'] = [struct.pack('<I', max(used) + 1)]
    return ov


def ov_no_xdrift(subs):
    """QNAM -> 0x7F everywhere (remove the invented -0.35 X drift)."""
    cur = next((v for s, v in subs if s == b'QNAM'), None)
    if cur is None:
        return {}
    return {b'QNAM': [b'\x7F' * len(cur)]}


def ov_stars_on(subs, t4):
    """Re-enable Stars from the authored TES4 value (rain/snow only)."""
    cur = next((v for s, v in subs if s == b'NAM0'), None)
    if cur is None or len(cur) < T5_SLOTS * 16:
        return {}
    out = bytearray(cur)
    src = t4_nam0(t4)
    for t in range(4):
        r, g, b = src[(6, t)]
        o = (6 * 4 + t) * 4
        out[o:o + 3] = bytes((r, g, b))
    return {b'NAM0': [bytes(out)]}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--source', default='output/Oblivion.esm/Oblivion.esm')
    ap.add_argument('--export', default='export/Oblivion.esm/WTHR.txt')
    ap.add_argument('--weather', default='Clear')
    ap.add_argument('--rain-weather', default='Rain',
                    help='weather used for the Stars variant')
    ap.add_argument('--cloud-weather', default='Cloudy',
                    help='weather used for the cloud-layer variants')
    ap.add_argument('--outdir', default='output/Oblivion.esm')
    ap.add_argument('--out', default='SkyUJ.esp')
    ap.add_argument('--base-fid', type=lambda s: int(s, 0), default=0x000E00)
    args = ap.parse_args()

    for p in (args.source, args.export):
        if not os.path.isfile(p):
            sys.exit(f'not found: {p}')

    wthr = read_records(args.source, b'WTHR')
    t4all = parse_t4(args.export)
    if args.weather not in wthr:
        sys.exit(f'{args.weather!r} not in {args.source}')
    if args.weather not in t4all:
        sys.exit(f'{args.weather!r} not in {args.export}')

    flags, _fid, subs = wthr[args.weather]
    t4 = t4all[args.weather]

    # ROUNDS 1 and 2 are DECIDED and shipped (highlight knee + Sun knee;
    # see the block comment above _NAM0_KNEE in dialog_misc.py).  Their
    # variants are gone -- keeping a control on the base weather is still
    # useful for comparing against the cloud variants below.
    plan = [
        ('UJbase', 'our converted %s, unchanged (CONTROL)' % args.weather,
         lambda: {}),
    ]

    records = b''
    listing = []
    n = 0
    for label, meaning, fn in plan:
        ov = fn()
        fid = (2 << 24) | (args.base_fid + n)
        records += rebuild('TES4' + label, fid, flags, subs, ov)
        listing.append((fid, 'TES4' + label, meaning,
                        sorted(s.decode('latin1') for s in ov)))
        n += 1

    # Stars blanking for rain/snow was reviewed and KEPT -- not under test.

    # ---- ROUND 3: CLOUD LAYER PLACEMENT, built from CLOUDY ----------------
    # Reported in game: forcing our Cloudy shows NO CLOUDS AT ALL.  The record
    # is fine (two textures, both layers enabled, nonzero alphas and tints) and
    # the .dds files exist -- the fault is WHICH dome shapes layers 0/1 are.
    # See the block comment above DOME_FULL.
    cw = args.cloud_weather
    if cw in wthr and cw in t4all:
        cflags, _cf, csubs = wthr[cw]
        cplan = [
            ('CLbase', f'our converted {cw}, unchanged (CONTROL: no clouds)',
             lambda: {}),
            # ROUND 3 winners, kept for reference against the refinements
            ('CLdomeonly', 'upper->8,9,10,11  lower->27  fog->28  (looked ok)',
             lambda: ov_cloud_layers(csubs, DOME_FULL, (27,), FOG_WASH)),
            ('CLminimal', 'upper->11  lower->27  (looked ok, ~same as above)',
             lambda: ov_cloud_layers(csubs, (11,), (27,), None)),

            # ---- ROUND 4: refine the 11/27 pairing -----------------------
            # 11 and 27 are the only ~1:1 shapes, so the open questions are
            # which sheet goes on which, and whether the current alphas are
            # right now that the sheets actually cover the sky.
            ('CLswap', 'SWAPPED: upper->27  lower->11',
             lambda: ov_cloud_layers(csubs, (27,), (11,), None)),
            ('CLopaque2', 'upper->11 lower->27, BOTH fully opaque',
             lambda: ov_cloud_layers(csubs, (11,), (27,), None,
                                     upper_alpha=(1.0,) * 4,
                                     lower_alpha=(1.0,) * 4)),
            ('CLupperonly', 'ONLY the upper sheet, on 11',
             lambda: ov_cloud_layers(csubs, (11,), (), None)),
            ('CLloweronly', 'ONLY the lower sheet, on 27',
             lambda: ov_cloud_layers(csubs, (), (27,), None)),
            # does the fog wash add anything with a correct dome underneath?
            ('CLdomefog', 'upper->11  lower->27  + fog wash on 28',
             lambda: ov_cloud_layers(csubs, (11,), (27,), FOG_WASH)),
        ]
        for label, meaning, fn in cplan:
            ov = fn()
            fid = (2 << 24) | (args.base_fid + n)
            records += rebuild('TES4' + label, fid, cflags, csubs, ov)
            listing.append((fid, 'TES4' + label, meaning,
                            sorted(s.decode('latin1') for s in ov
                                   if ov[s]) if ov else []))
            n += 1

    group = pack_top_group('WTHR', records)
    masters = ['Skyrim.esm', os.path.basename(args.source)]
    header = pack_tes4_header(
        masters=masters, num_records=_count_records_and_groups(group),
        next_object_id=0x2000, is_esm=False)

    os.makedirs(args.outdir, exist_ok=True)
    out_path = os.path.join(args.outdir, args.out)
    with open(out_path, 'wb') as fh:
        fh.write(header + group)

    print(f'Wrote {out_path}   ({len(listing)} test weathers)')
    print(f'  base: OUR {args.weather}   masters: {masters}')
    print()
    print(f'  {"FormID":>8s}  {"EditorID":18s} {"fields":22s} meaning')
    for fid, edid, meaning, fields in listing:
        f = ','.join(fields) if fields else '(none)'
        print(f'  {fid:08X}  {edid:18s} {f[:22]:22s} {meaning}')
    print()
    print('  Every variant differs from UJbase ONLY in the listed fields.')
    print('  If a variant looks BETTER than UJbase, that value was a defect.')


if __name__ == '__main__':
    main()
