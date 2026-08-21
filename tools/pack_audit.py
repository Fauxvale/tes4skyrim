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
from tes5_import.pack_indexes import (build_pack_indexes,          # noqa: E402
                                      PLACEABLE_BASE_SIGS)

T4_NAMES = {
    0: 'Find', 1: 'Follow', 2: 'Escort', 3: 'Eat', 4: 'Sleep', 5: 'Wander',
    6: 'Travel', 7: 'Accompany', 8: 'UseItemAt', 9: 'Ambush',
    10: 'FleeNotCombat', 11: 'CastMagic',
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--type', type=int, default=None)
    ap.add_argument('--export', default='export/Oblivion.esm')
    ap.add_argument('--detail', action='store_true',
                    help='for every bucket that drops its target, break the '
                         'dropped targets down by PTDT type and base signature')
    args = ap.parse_args()

    set_formid_index_offset(1)
    # The SAME indexes the import builds (tes5_import.pack_indexes), so this
    # census measures the production routing rather than a stand-in context.
    recs = parse_export_directory(
        args.export,
        type_filter={'PACK', 'REFR', 'ACHR', 'ACRE', 'CELL', 'NPC_', 'CREA'}
        | set(PLACEABLE_BASE_SIGS))
    bt = group_records_by_type(recs)
    ctx = pc.PackContext(**build_pack_indexes(bt))
    rows = collections.Counter()
    lost_target = collections.Counter()
    lost_loc = collections.Counter()
    samples = collections.defaultdict(list)
    detail = collections.defaultdict(collections.Counter)
    detail_samples = collections.defaultdict(dict)

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
        slot_t = any('target' in k for k in inp.t.slots)
        slot_l = any(k.endswith('location') for k in inp.t.slots)
        # A Find at an actor becomes a Travel whose LOCATION is the target
        # ref/alias (PLDT type 0 / 8): the target survived, in the other slot.
        if has_t and not slot_t and inp.t is pc.TRAVEL:
            loc = inp.values.get(inp.t.slot('location'))
            if isinstance(loc, bytes) and loc[0] in (0, 8):
                slot_t = True
        if has_t and not slot_t:
            lost_target[key] += 1
            if args.detail:
                t_type = get_int(rec, 'PTDT.Type', -1)
                tsig = (ctx.sig_of_base(get_formid(rec, 'PTDT.Target'))
                        if t_type == 1 else
                        ctx.base_sig_of(get_formid(rec, 'PTDT.Target'))
                        if t_type == 0 else
                        f'objtype{get_int(rec, "PTDT.Target", 0)}')
                detail[key][(t_type, tsig or '?')] += 1
                dsamp = detail_samples[key].setdefault((t_type, tsig or '?'), [])
                if len(dsamp) < 5:
                    dsamp.append(rec.get('EditorID', '?'))
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
        if args.detail and detail.get((ptype, tmpl)):
            for (tt, sig), n2 in detail[(ptype, tmpl)].most_common():
                print(f'{"":40} PTDT type {tt} {sig:8} x{n2}  '
                      f'{detail_samples[(ptype, tmpl)].get((tt, sig), [])}')


if __name__ == '__main__':
    main()
