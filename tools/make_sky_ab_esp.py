"""Comprehensive one-load A/B: find WHICH field of our weather causes the bloom.

Established by an earlier arm: overriding our `Clear` so every value came from
vanilla `SkyrimClear`, keeping OUR cloud textures, renders BLOOM-FREE.  So the
bloom is in our WTHR record data, not our .dds files.

That earlier sweep could not localise it because it was built LEAVE-ONE-IN:
each variant swapped one group to vanilla and left nine others ours, so every
variant kept most of the defect and they all bloomed.  It also never tested
TNAM at all.

This build tests every differing field group TWICE, in opposite directions, so
the culprit is confirmed by two independent readings:

  ISO<Group>   all vanilla EXCEPT this group, which stays OURS
               -> blooms only if this group is SUFFICIENT to cause it
  FIX<Group>   all ours EXCEPT this group, which comes from VANILLA
               -> stops blooming only if this group is NECESSARY

A single cause shows up as: ISO<G> blooms AND FIX<G> is clean, with every
other ISO clean and every other FIX bloomy.  An additive cause shows up as
several ISOs blooming a little and no single FIX clearing it -- which is why
both directions are needed.

Groups (every subrecord that differs between our Clear and vanilla
SkyrimClear, so nothing is left untested):

  Colour   NAM0            weather colour table
  Cloud    PNAM            per-layer cloud tints
  Alpha    JNAM            per-layer cloud alphas
  Layers   LNAM NAM1       layer allocation + disabled bitfield
  Speed    RNAM QNAM       per-layer cloud drift
  Fog      FNAM            fog distances/power/max
  Data     DATA            wind, glare, trans delta, classification
  Tone     IMSP            HDR / bloom tone mapping (our minted IMGS)
  Ambient  DALC            directional ambient cube
  Statics  TNAM            SKY STATICS -- 48 volumetric cloud STATs in
                           vanilla, ZERO in ours (the converter never writes
                           this field)
  Tex      *0TX            cloud texture paths + which layers exist

Controls: ALLVanilla (expect clean) and ALLOurs (expect bloom).  If those two
do not bracket, the harness is wrong, not the converter -- stop and say so.

Usage:
  python tools/make_sky_ab_esp.py
  python tools/make_sky_ab_esp.py --weather Cloudy --vanilla SkyrimCloudy
  python tools/make_sky_ab_esp.py --our-textures   # ISO/FIX keep our .dds
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

# Ordered so the console listing groups related things together.  Every
# subrecord that can differ between the two records appears in exactly one
# group; TEXTURE_SIGS is handled specially because the SET of layers differs,
# not just the values.
GROUPS = (
    ('Colour',  (b'NAM0',)),
    ('Cloud',   (b'PNAM',)),
    ('Alpha',   (b'JNAM',)),
    ('Layers',  (b'LNAM', b'NAM1')),
    ('Speed',   (b'RNAM', b'QNAM')),
    ('Fog',     (b'FNAM',)),
    ('Data',    (b'DATA',)),
    ('Tone',    (b'IMSP',)),
    ('Ambient', (b'DALC',)),
    ('Statics', (b'TNAM',)),
    ('Tex',     ()),            # texture subrecords, see is_texture
)
GROUP_NAMES = [n for n, _ in GROUPS]


def is_texture(sig):
    return len(sig) == 4 and sig[1:] == b'0TX'


def layer_of(sig):
    return sig[0] - 0x30 if sig[0] <= 0x40 else 17 + (sig[0] - 0x41)


def subrecords(data):
    out, i = [], 0
    while i + 6 <= len(data):
        sig = data[i:i + 4]
        size = struct.unpack('<H', data[i + 4:i + 6])[0]
        i += 6
        out.append((sig, data[i:i + size]))
        i += size
    return out


def read_weathers(path):
    buf = open(path, 'rb').read()
    out = {}
    i = struct.unpack('<I', buf[4:8])[0] + 24
    while i + 24 <= len(buf):
        if buf[i:i + 4] != b'GRUP':
            break
        gs = struct.unpack('<I', buf[i + 4:i + 8])[0]
        if (struct.unpack('<i', buf[i + 12:i + 16])[0] == 0
                and buf[i + 8:i + 12] == b'WTHR'):
            j, end = i + 24, i + gs
            while j + 24 <= end:
                size, flags = struct.unpack('<II', buf[j + 4:j + 12])
                p = buf[j + 24:j + 24 + size]
                if flags & 0x00040000:
                    p = zlib.decompress(p[4:])
                s = subrecords(p)
                ed = next((v.rstrip(b'\x00').decode('ascii', 'replace')
                           for sig, v in s if sig == b'EDID'), None)
                if ed:
                    out[ed] = (flags & ~0x00040000, s)
                j += 24 + size
        i += gs
    return out


# Subrecord write order, from wbDefinitionsTES5 wbRecord(WTHR, ...).  Building
# from an explicit order lets a variant mix subrecords from both donors while
# still producing a record the engine parses.
WRITE_ORDER = (
    [b'EDID'] + [bytes([c]) + b'0TX' for c in
                 list(range(0x30, 0x41)) + list(range(0x41, 0x4D))]
    + [b'LNAM', b'MNAM', b'NNAM', b'RNAM', b'QNAM', b'PNAM', b'JNAM',
       b'NAM0', b'FNAM', b'DATA', b'NAM1', b'SNAM', b'TNAM', b'IMSP',
       b'DALC', b'NAM2', b'NAM3', b'GNAM']
)


def build(edid, fid, flags, our_subs, van_subs, ours_groups, keep_our_tex):
    """One test weather.

    `ours_groups` is the set of group names taken from OUR record; every other
    group comes from vanilla.  Textures follow the 'Tex' group unless
    keep_our_tex forces ours everywhere (so the .dds files stay constant and
    only record data varies).
    """
    def pick(group):
        return our_subs if group in ours_groups else van_subs

    by_group = {}
    for name, sigs in GROUPS:
        for sig in sigs:
            by_group[sig] = name

    src_of = {}
    for sig in WRITE_ORDER:
        if sig == b'EDID':
            continue
        if is_texture(sig):
            grp = 'Tex'
        else:
            grp = by_group.get(sig)
        if grp is None:
            # A subrecord neither record differs on (MNAM/NNAM/SNAM/...) --
            # take vanilla's, which equals ours by definition.
            src_of[sig] = van_subs
        elif grp == 'Tex' and keep_our_tex:
            src_of[sig] = our_subs
        else:
            src_of[sig] = pick(grp)

    out = pack_subrecord('EDID', edid.encode('utf-8') + b'\x00')
    our_tex = {layer_of(s): v for s, v in our_subs if is_texture(s)}
    our_layers = sorted(our_tex)
    for sig in WRITE_ORDER:
        if sig == b'EDID':
            continue
        src = src_of[sig]
        values = [v for s, v in src if s == sig]
        if is_texture(sig) and keep_our_tex and not values:
            # Vanilla enables layers we have no sheet for; fill by BAND so the
            # sky still has clouds everywhere vanilla does.
            if any(s == sig for s, _ in van_subs) and our_layers:
                layer = layer_of(sig)
                p = 0 if layer < 8 else min(1, our_layers[-1])
                values = [our_tex.get(p, our_tex[our_layers[0]])]
        for v in values:
            out += pack_subrecord(sig.decode('ascii'), v)
    return pack_record('WTHR', fid, flags, out)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--source', default='output/Oblivion.esm/Oblivion.esm')
    ap.add_argument('--weather', default='Clear')
    ap.add_argument('--vanilla', default='SkyrimClear')
    ap.add_argument('--skyrim-esm', default=None)
    ap.add_argument('--our-textures', action='store_true',
                    help='hold OUR .dds constant in every ISO/FIX variant '
                         '(drops the Tex group from the sweep)')
    ap.add_argument('--outdir', default='output/Oblivion.esm')
    ap.add_argument('--out', default='SkyAB.esp')
    args = ap.parse_args()

    skyrim_esm = args.skyrim_esm
    if not skyrim_esm:
        cfg = json.load(open('conversion_config.json', encoding='utf-8'))
        skyrim_esm = os.path.join(cfg['tes5DataPath'], 'Skyrim.esm')
    for p in (args.source, skyrim_esm):
        if not os.path.isfile(p):
            sys.exit(f'not found: {p}')

    ours = read_weathers(args.source)
    vanilla = read_weathers(skyrim_esm)
    if args.weather not in ours:
        sys.exit(f'{args.weather!r} not in {args.source}')
    if args.vanilla not in vanilla:
        sys.exit(f'{args.vanilla!r} not in {skyrim_esm}')
    flags, our_subs = ours[args.weather]
    _vflags, van_subs = vanilla[args.vanilla]

    # Only sweep groups that actually differ; a group with identical data on
    # both sides would produce two identical variants and waste a test slot.
    def differs(sigs):
        for sig in sigs:
            if ([v for s, v in our_subs if s == sig]
                    != [v for s, v in van_subs if s == sig]):
                return True
        return False

    active = []
    for name, sigs in GROUPS:
        if name == 'Tex':
            if not args.our_textures and differs(
                    tuple(s for s, _ in our_subs if is_texture(s))
                    + tuple(s for s, _ in van_subs if is_texture(s))):
                active.append(name)
        elif differs(sigs):
            active.append(name)

    keep = args.our_textures
    plan = [('ALLVanilla', set())]
    for name in active:
        plan.append((f'ISO{name}', {name}))
    for name in active:
        plan.append((f'FIX{name}', set(active) - {name}))
    plan.append(('ALLOurs', set(active)))

    records = b''
    listing = []
    for n, (label, ours_groups) in enumerate(plan):
        edid = f'TES4AB{label}'
        fid = (2 << 24) | (0x000D00 + n)
        records += build(edid, fid, flags, our_subs, van_subs,
                         ours_groups, keep)
        listing.append((edid, fid, label, ours_groups))

    group = pack_top_group('WTHR', records)
    masters = ['Skyrim.esm', os.path.basename(args.source)]
    header = pack_tes4_header(
        masters=masters, num_records=_count_records_and_groups(group),
        next_object_id=0x2000, is_esm=False)

    os.makedirs(args.outdir, exist_ok=True)
    out_path = os.path.join(args.outdir, args.out)
    with open(out_path, 'wb') as fh:
        fh.write(header + group)

    print(f'Wrote {out_path}   ({len(plan)} test weathers)')
    print(f'  base: OUR {args.weather}    donor: vanilla {args.vanilla}')
    print(f'  masters: {masters}')
    print(f'  groups swept: {active}')
    if keep:
        print('  OUR textures held constant in every variant')
    print()
    print(f'  {"FormID":>8s}  {"EditorID":24s} meaning')
    for edid, fid, label, og in listing:
        if label == 'ALLVanilla':
            meaning = 'CONTROL - all vanilla (expect NO bloom)'
        elif label == 'ALLOurs':
            meaning = 'CONTROL - all ours (expect BLOOM)'
        elif label.startswith('ISO'):
            meaning = f'only {label[3:]} is ours -> blooms if SUFFICIENT'
        else:
            meaning = f'only {label[3:]} is vanilla -> clean if NECESSARY'
        print(f'  {fid:08X}  {edid:24s} {meaning}')
    print()
    print('  Read the result:')
    print('   * exactly one ISO blooms and the matching FIX is clean')
    print('       -> that group is the culprit, confirmed both ways')
    print('   * several ISOs bloom, no FIX is clean')
    print('       -> additive; the blooming ISOs are the contributors')
    print('   * controls do not bracket -> harness bug, not converter')


if __name__ == '__main__':
    main()
