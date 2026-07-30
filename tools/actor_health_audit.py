"""Audit converted actor health against the TES4 source pool.

The engine derives an actor's max health as

    RACE.StartingHealth + ACBS.HealthOffset + (Level-1) * fNPCHealthLevelBonus

with fNPCHealthLevelBonus = 5.0. TES4 DATA.Health is a FINAL, fully-calculated
pool, so a faithful conversion makes that sum reproduce it exactly. This reads
the BUILT esm (not the converter's own functions, so it catches anything the
pipeline does after ACBS is packed) and reports every actor whose health does
not match its TES4 source.

Usage:
    python tools/actor_health_audit.py output/Nehrim.esm/Nehrim.esm export/Nehrim.esm
    python tools/actor_health_audit.py <built.esm> <export_dir> [--limit N] [--all]
"""
import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.tes5_esm_reader import read_tes5_file, _get  # noqa: E402

HEALTH_LEVEL_BONUS = 5.0
DEFAULT_RACE_BASE = 50.0


def _sub(rec, sig):
    s = _get(rec, sig)
    return s.data if s is not None else None


def parse_export(path):
    out = {}
    cur = None
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line = line.rstrip('\n')
            if line.startswith('---RECORD_BEGIN---'):
                if cur and cur.get('EditorID'):
                    out[cur['EditorID']] = cur
                cur = {}
            elif cur is not None and '=' in line:
                k, v = line.split('=', 1)
                cur[k.strip()] = v.split()[0].strip() if v.strip() else ''
    if cur and cur.get('EditorID'):
        out[cur['EditorID']] = cur
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('esm', help='built TES5 esm')
    ap.add_argument('export_dir', help='TES4 export dir (NPC_.txt / CREA.txt)')
    ap.add_argument('--limit', type=int, default=20, help='mismatches to print')
    ap.add_argument('--all', action='store_true', help='print every mismatch')
    args = ap.parse_args()

    src = {}
    for sig in ('NPC_', 'CREA'):
        p = os.path.join(args.export_dir, f'{sig}.txt')
        if os.path.isfile(p):
            src.update(parse_export(p))
    if not src:
        print(f'no NPC_/CREA exports under {args.export_dir}')
        return 1

    _hdr, recs, _loc = read_tes5_file(args.esm, parse_types={'RACE', 'NPC_'})
    races, npcs = {}, []
    for r in recs:
        if r.type == 'RACE':
            d = _sub(r, 'DATA')
            if d and len(d) >= 48:
                races[r.form_id] = struct.unpack_from('<f', d, 36)[0]
        elif r.type == 'NPC_':
            npcs.append(r)

    exact = mismatch = level_mult = corpse = unmatched = 0
    dead_bug = 0
    rows = []
    for r in npcs:
        e = _sub(r, 'EDID')
        edid = e.rstrip(b'\x00').decode('ascii', 'replace') if e else None
        s = src.get(edid)
        if not s:
            unmatched += 1
            continue
        acbs = _sub(r, 'ACBS')
        if not acbs or len(acbs) < 24:
            continue
        fl, _mo, _so, lvl, _cn, _cx, _sp, _dp, _tf, ho, _bo = struct.unpack(
            '<IhhHHHHhHhH', acbs[:24])
        rn = _sub(r, 'RNAM')
        base = races.get(struct.unpack('<I', rn)[0] if rn else 0, DEFAULT_RACE_BASE)
        t4h = int(s.get('DATA.Health', '50') or 50)

        if t4h <= 0:
            corpse += 1
            continue
        if fl & 0x80:      # PC Level Mult: level term is a runtime multiplier
            level_mult += 1
            continue
        final = base + ho + (lvl - 1) * HEALTH_LEVEL_BONUS
        if final <= 0:
            dead_bug += 1
        if abs(final - t4h) < 0.6:
            exact += 1
        else:
            mismatch += 1
            rows.append((edid, t4h, final, base, ho, lvl))

    total = exact + mismatch
    pct = 100.0 * exact / total if total else 0.0
    print(f'{os.path.basename(args.esm)}: fixed-level actors {total}')
    print(f'  exact      : {exact} ({pct:.1f}%)')
    print(f'  mismatch   : {mismatch}')
    print(f'  SPAWN-DEAD : {dead_bug}   <- positive TES4 health but <=0 in game')
    print(f'  skipped    : {level_mult} pc-level-mult, {corpse} zero-health corpse '
          f'props, {unmatched} not in export')
    if rows:
        print('\n  editorid                              tes4    built     base  '
              'offset  level')
        show = rows if args.all else rows[:args.limit]
        for edid, t4h, final, base, ho, lvl in show:
            print(f'  {edid[:36]:36s} {t4h:7d} {final:8.0f} {base:8.0f} '
                  f'{ho:7d} {lvl:6d}')
        if not args.all and len(rows) > len(show):
            print(f'  ... {len(rows) - len(show)} more (--all)')
    return 0 if mismatch == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
