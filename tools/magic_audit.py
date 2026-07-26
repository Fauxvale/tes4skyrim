"""Audit TES4->TES5 magic conversion coverage.

Reports, for an export directory:
  * every MGEF the source defines, whether the converter maps it, and to what
  * which MGEF assets (model / effect shader / sounds / lights) are discarded
  * per-effect-record (SPEL/ENCH/ALCH/INGR/SGST/LVSP) fallout: how many effects
    are dropped, how many records lose ALL effects, how many go to filler
  * the AssocItem loss (summons/bound weapons whose target creature/item the
    flat code table cannot express)

Usage:
    python tools/magic_audit.py export/Oblivion.esm
    python tools/magic_audit.py export/Oblivion.esm --by-record
    python tools/magic_audit.py export/Nehrim.esm --unmapped-only
"""
import argparse
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Nehrim's strings are German; a cp1252 console cannot encode every character.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from tes5_import.text_reader import parse_export_file  # noqa: E402
from tes5_import.skyrim_overrides import (  # noqa: E402
    MGEF_AV_CODE_TO_SKYRIM,
    MGEF_CODE_TO_SKYRIM,
)
from tes5_import.vanilla_mgef_data import VANILLA_MGEF_DATA  # noqa: E402

EFFECT_RECORD_TYPES = ('SPEL', 'ENCH', 'ALCH', 'INGR', 'SGST', 'LVSP')

# MGEF DATA.Flags bits that carry behaviour the flat table cannot preserve.
MGEF_FLAGS = {
    0x00000001: 'Hostile',
    0x00000002: 'Recover',
    0x00000004: 'Detrimental',
    0x00000008: 'MagnitudePercent',
    0x00000010: 'Self',
    0x00000020: 'Touch',
    0x00000040: 'Target',
    0x00000080: 'NoDuration',
    0x00000100: 'NoMagnitude',
    0x00000200: 'NoArea',
    0x00000400: 'FXPersist',
    0x00000800: 'Spellmaking',
    0x00001000: 'Enchanting',
    0x00002000: 'NoIngredient',
    0x00010000: 'UseWeapon',
    0x00020000: 'UseArmor',
    0x00040000: 'UseCreature',
    0x00080000: 'UseSkill',
    0x00100000: 'UseAttribute',
    0x01000000: 'UseActorValue',
    0x02000000: 'SprayProjectile',
    0x04000000: 'BoltProjectile',
    0x08000000: 'NoHitEffect',
    0x10000000: 'PersistOnDeath',
    0x20000000: 'Unknown29',
    0x40000000: 'FogProjectile',
}

SCHOOLS = {0: 'Alteration', 1: 'Conjuration', 2: 'Destruction',
           3: 'Illusion', 4: 'Mysticism', 5: 'Restoration'}


def _load(export_dir, sig):
    path = os.path.join(export_dir, f'{sig}.txt')
    if not os.path.exists(path):
        return []
    return parse_export_file(path)


def _get(rec, key, default=''):
    return rec.get(key, default)


def _fid(rec, key):
    v = rec.get(key, '')
    try:
        return int(v, 16)
    except (ValueError, TypeError):
        return 0


def resolve(code, av=-1):
    """Mirror tes5_import.record_types.equipment._resolve_mgef."""
    per_av = MGEF_AV_CODE_TO_SKYRIM.get(code)
    if per_av is not None:
        fid = per_av.get(av)
        if fid:
            return fid
    return MGEF_CODE_TO_SKYRIM.get(code, 0)


def audit_mgef(export_dir):
    """Per-MGEF coverage + discarded-asset census."""
    mgefs = _load(export_dir, 'MGEF')
    rows = []
    for rec in mgefs:
        code = _get(rec, 'EditorID')
        flags = int(_get(rec, 'DATA.Flags', '0') or 0)
        rows.append({
            'code': code,
            'name': _get(rec, 'FULL'),
            'fid': _get(rec, 'FormID'),
            'school': SCHOOLS.get(int(_get(rec, 'DATA.School', '0') or 0), '?'),
            'flags': flags,
            'flag_names': [n for b, n in MGEF_FLAGS.items() if flags & b],
            'assoc': _fid(rec, 'DATA.AssocItem'),
            'model': _get(rec, 'Model.MODL'),
            'icon': _get(rec, 'ICON'),
            'shader': _fid(rec, 'DATA.EffectShader'),
            'ench_shader': _fid(rec, 'DATA.EnchantEffect'),
            'light': _fid(rec, 'DATA.Light'),
            'proj_speed': float(_get(rec, 'DATA.ProjectileSpeed', '0') or 0),
            'cast_snd': _fid(rec, 'DATA.CastingSound'),
            'bolt_snd': _fid(rec, 'DATA.BoltSound'),
            'hit_snd': _fid(rec, 'DATA.HitSound'),
            'area_snd': _fid(rec, 'DATA.AreaSound'),
            'counters': int(_get(rec, 'CounterEffects', '0') or 0),
            'base_cost': float(_get(rec, 'DATA.BaseCost', '0') or 0),
            'mapped': resolve(code),
            'per_av': code in MGEF_AV_CODE_TO_SKYRIM,
        })
    return rows


def audit_effect_records(export_dir):
    """Per-record effect fallout across all effect-bearing record types."""
    out = {}
    for sig in EFFECT_RECORD_TYPES:
        recs = _load(export_dir, sig)
        if not recs:
            continue
        stats = {'total': len(recs), 'effects': 0, 'dropped': 0,
                 'all_dropped': 0, 'partial': 0, 'clean': 0,
                 'dropped_codes': Counter(), 'examples': defaultdict(list)}
        for rec in recs:
            n = int(_get(rec, 'EffectCount', '0') or 0)
            if not n:
                continue
            kept = drop = 0
            for i in range(n):
                code = _get(rec, f'Effect[{i}].EFID')
                if not code:
                    continue
                av = int(_get(rec, f'Effect[{i}].ActorValue', '-1') or -1)
                stats['effects'] += 1
                if resolve(code, av):
                    kept += 1
                else:
                    drop += 1
                    stats['dropped'] += 1
                    stats['dropped_codes'][code] += 1
                    if len(stats['examples'][code]) < 3:
                        stats['examples'][code].append(
                            _get(rec, 'EditorID') or _get(rec, 'FormID'))
            if drop and not kept:
                stats['all_dropped'] += 1
            elif drop:
                stats['partial'] += 1
            else:
                stats['clean'] += 1
        out[sig] = stats
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('export_dir', help='e.g. export/Oblivion.esm')
    ap.add_argument('--by-record', action='store_true',
                    help='list every MGEF with its mapping')
    ap.add_argument('--unmapped-only', action='store_true',
                    help='only list MGEFs that resolve to nothing')
    ap.add_argument('--assets', action='store_true',
                    help='detail the per-MGEF assets that conversion discards')
    args = ap.parse_args()

    rows = audit_mgef(args.export_dir)
    if not rows:
        print(f'No MGEF.txt in {args.export_dir}')
        return 1

    mapped = [r for r in rows if r['mapped'] or r['per_av']]
    unmapped = [r for r in rows if not (r['mapped'] or r['per_av'])]
    distinct_targets = {r['mapped'] for r in rows if r['mapped']}

    print(f'=== MGEF coverage: {args.export_dir} ===')
    print(f'  source MGEF records          : {len(rows)}')
    print(f'  mapped to a vanilla Skyrim MGEF: {len(mapped)}')
    print(f'  unmapped (effect DROPPED)    : {len(unmapped)}')
    print(f'  distinct Skyrim targets used : {len(distinct_targets)}'
          f'  ({len(rows)} source effects collapse onto {len(distinct_targets)})')
    print(f'  vanilla DATA blobs available : {len(VANILLA_MGEF_DATA)}')

    # Assets lost: everything the source MGEF carries that has no destination.
    with_model = [r for r in rows if r['model']]
    with_shader = [r for r in rows if r['shader'] or r['ench_shader']]
    with_snd = [r for r in rows if r['cast_snd'] or r['bolt_snd']
                or r['hit_snd'] or r['area_snd']]
    with_assoc = [r for r in rows if r['assoc']]
    with_light = [r for r in rows if r['light']]
    with_counters = [r for r in rows if r['counters']]
    print('\n=== source data with no destination (per MGEF) ===')
    print(f'  Model.MODL (cast art)        : {len(with_model)}')
    print(f'  EffectShader/EnchantEffect   : {len(with_shader)}')
    print(f'  sounds (cast/bolt/hit/area)  : {len(with_snd)}')
    print(f'  AssocItem (summon/bound tgt) : {len(with_assoc)}')
    print(f'  Light                        : {len(with_light)}')
    print(f'  counter effects (ESCE)       : {len(with_counters)}')

    # Collapse pressure: which Skyrim MGEF absorbs the most source effects.
    collapse = Counter(r['mapped'] for r in rows if r['mapped'])
    print('\n=== worst collapses (source effects -> one Skyrim MGEF) ===')
    vanilla_name = {f: e[0] for f, e in VANILLA_MGEF_DATA.items()}
    for fid, n in collapse.most_common(10):
        if n < 2:
            continue
        codes = [r['code'] for r in rows if r['mapped'] == fid]
        print(f'  {vanilla_name.get(fid, hex(fid)):32s} <- {n:2d}  '
              f'{" ".join(sorted(codes)[:12])}')

    if unmapped:
        print(f'\n=== unmapped MGEFs ({len(unmapped)}) — effects silently dropped ===')
        for r in sorted(unmapped, key=lambda r: r['code']):
            assoc = f" assoc={r['assoc']:08X}" if r['assoc'] else ''
            print(f"  {r['code']:6s} {r['name'][:38]:38s} {r['school']:12s}{assoc}")

    if args.assets:
        print('\n=== per-MGEF discarded assets ===')
        for r in sorted(rows, key=lambda r: r['code']):
            bits = []
            if r['model']:
                bits.append(f"model={r['model']}")
            if r['shader']:
                bits.append(f"shader={r['shader']:08X}")
            if r['ench_shader']:
                bits.append(f"ench={r['ench_shader']:08X}")
            if r['light']:
                bits.append(f"light={r['light']:08X}")
            if r['assoc']:
                bits.append(f"assoc={r['assoc']:08X}")
            if r['counters']:
                bits.append(f"counters={r['counters']}")
            if bits:
                print(f"  {r['code']:6s} {r['name'][:30]:30s} {' '.join(bits)}")

    if args.by_record or args.unmapped_only:
        print('\n=== per-MGEF mapping ===')
        for r in sorted(rows, key=lambda r: (r['school'], r['code'])):
            if args.unmapped_only and (r['mapped'] or r['per_av']):
                continue
            tgt = ('per-ActorValue' if r['per_av']
                   else vanilla_name.get(r['mapped'], hex(r['mapped']))
                   if r['mapped'] else '*** DROPPED ***')
            print(f"  {r['school']:12s} {r['code']:6s} {r['name'][:34]:34s} -> {tgt}")

    print('\n=== effect-bearing records ===')
    per = audit_effect_records(args.export_dir)
    tot_all_dropped = 0
    for sig, s in per.items():
        pct = 100.0 * s['dropped'] / s['effects'] if s['effects'] else 0
        tot_all_dropped += s['all_dropped']
        print(f"  {sig}: {s['total']:5d} records, {s['effects']:5d} effects, "
              f"{s['dropped']:5d} dropped ({pct:4.1f}%), "
              f"{s['all_dropped']:4d} lost ALL effects -> filler, "
              f"{s['partial']:4d} partial, {s['clean']:5d} clean")
    print(f'  TOTAL records reduced to filler effects: {tot_all_dropped}')

    drops = Counter()
    ex = defaultdict(list)
    for s in per.values():
        drops.update(s['dropped_codes'])
        for c, names in s['examples'].items():
            ex[c].extend(names)
    if drops:
        print('\n=== most-dropped effect codes (by use count) ===')
        by_code = {r['code']: r for r in rows}
        for code, n in drops.most_common(25):
            name = by_code.get(code, {}).get('name', '?')
            print(f'  {code:6s} {name[:34]:34s} {n:5d} uses   '
                  f'e.g. {", ".join(ex[code][:3])}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
