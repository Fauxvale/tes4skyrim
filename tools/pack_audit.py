#!/usr/bin/env python3
"""Audit TES4->TES5 package conversion: which template each PKDT.Type picks,
how many packages take each path, and which ones DROP their target/location.

A package that silently loses its TES4 target or location is an actor that
stands still (UseItemAt->SitTarget on a switch) or walks to the wrong place
(an unset Location falls back to "near editor location").

Usage:
    python tools/pack_audit.py
    python tools/pack_audit.py --type 8      # only PKDT.Type 8
"""
import argparse
import collections
import sys

sys.path.insert(0, '.')
from tes5_import.text_reader import (parse_export_directory,       # noqa: E402
                                     group_records_by_type,
                                     set_formid_index_offset,
                                     get_int, get_formid)
from tes5_import import pack_converter as pc                       # noqa: E402

T4_NAMES = {
    0: 'Find', 1: 'Follow', 2: 'Escort', 3: 'Eat', 4: 'Sleep', 5: 'Wander',
    6: 'Travel', 7: 'Accompany', 8: 'UseItemAt', 9: 'Ambush',
    10: 'FleeNotCombat', 11: 'CastMagic',
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--type', type=int, default=None)
    ap.add_argument('--export', default='export/Oblivion.esm')
    args = ap.parse_args()

    set_formid_index_offset(1)
    recs = parse_export_directory(args.export,
                                  type_filter={'PACK', 'REFR', 'ACTI', 'FURN',
                                               'DOOR', 'CONT', 'STAT', 'MISC',
                                               'LIGH'})
    bt = group_records_by_type(recs)

    base_sig = {}
    for sig in ('ACTI', 'FURN', 'DOOR', 'CONT', 'STAT', 'MISC', 'LIGH'):
        for r in bt.get(sig, []):
            try:
                base_sig[int(r.get('FormID', '0'), 16) & 0xFFFFFF] = sig
            except ValueError:
                pass
    ref_base = {}
    for r in bt.get('REFR', []):
        b = r.get('NAME')
        if not b:
            continue
        try:
            s = base_sig.get(int(b, 16) & 0xFFFFFF)
            if s:
                ref_base[int(r.get('FormID', '0'), 16) & 0xFFFFFF] = s
        except ValueError:
            pass

    ctx = pc.PackContext(ref_base_sig=ref_base)
    rows = collections.Counter()
    lost_target = collections.Counter()
    lost_loc = collections.Counter()
    samples = collections.defaultdict(list)

    for rec in bt.get('PACK', []):
        ptype = get_int(rec, 'PKDT.Type', -1)
        if args.type is not None and ptype != args.type:
            continue
        try:
            inp = pc._choose(rec, ctx, get_formid(rec, 'FormID'))
        except Exception as exc:                     # noqa: BLE001
            rows[(ptype, f'ERROR {exc}')] += 1
            continue
        key = (ptype, inp.t.edid)
        rows[key] += 1
        if len(samples[key]) < 3:
            samples[key].append(rec.get('EditorID', '?'))
        # did the TES4 record HAVE a target/location the template dropped?
        has_t = get_int(rec, 'PTDT.Type', -1) >= 0
        has_l = 'PLDT.Type' in rec
        slot_t = 'target' in inp.t.slots or 'targets' in inp.t.slots
        slot_l = any(k.endswith('location') for k in inp.t.slots)
        if has_t and not slot_t:
            lost_target[key] += 1
        if has_l and not slot_l:
            lost_loc[key] += 1

    print(f'{"TES4 type":22} {"-> template":16} {"count":>6}  '
          f'{"lost tgt":>8} {"lost loc":>8}')
    for (ptype, tmpl), n in sorted(rows.items(),
                                   key=lambda kv: (kv[0][0], -kv[1])):
        name = f'{ptype} {T4_NAMES.get(ptype, "?")}'
        lt = lost_target.get((ptype, tmpl), 0)
        ll = lost_loc.get((ptype, tmpl), 0)
        flag = '  <-- DROPS DATA' if (lt or ll) else ''
        print(f'{name:22} {tmpl:16} {n:6}  {lt:8} {ll:8}{flag}')
        if lt or ll:
            print(f'{"":40} e.g. {samples[(ptype, tmpl)]}')


if __name__ == '__main__':
    main()
