"""Audit which wearable meshes change biped slot, and the records that wear them.

The armour slot used to be guessed from the NIF filename stem; it now comes from
the wearing record's BMDT biped flags, with per-shape resolution from skin
weights for meshes that hold several pieces.  This reports where the two differ,
so a conversion change can be checked against real records in-game.

    python tools/wearable_slot_audit.py Oblivion.esm
    python tools/wearable_slot_audit.py Morrowind_ob.esm --formids
"""
import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asset_convert import wearable_plan as wp  # noqa: E402

BP_NAME = {32: 'body', 33: 'hands', 36: 'ring', 37: 'feet', 38: 'calves',
           40: 'amulet', 44: 'lowerbody', 131: 'head'}


def records_by_mesh(export_dir):
    """mesh path -> [(formid, name, biped_flags)] for every ARMO/CLOT wearing it."""
    from pathlib import Path
    out = defaultdict(list)
    for name in ('ARMO.txt', 'CLOT.txt'):
        path = Path(export_dir) / name
        if not path.is_file():
            continue
        for chunk in path.read_text(encoding='utf-8',
                                    errors='replace').split('---RECORD_BEGIN---')[1:]:
            rec = {}
            for line in chunk.split('---RECORD_END---')[0].splitlines():
                if '=' in line:
                    k, v = line.strip().split('=', 1)
                    rec[k] = v
            try:
                bf = int(rec.get('BMDT.BipedFlags', '0') or 0)
            except ValueError:
                bf = 0
            label = rec.get('FULL') or rec.get('EditorID') or '?'
            for key in ('Male.BipedModel.MODL', 'Female.BipedModel.MODL'):
                if rec.get(key, '').strip():
                    out[wp._norm(rec[key])].append((rec.get('FormID', '?'), label, bf))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('plugin', help='e.g. Oblivion.esm')
    ap.add_argument('--formids', action='store_true',
                    help='print one FormID per affected record')
    args = ap.parse_args()

    export_dir = os.path.join('export', args.plugin)
    if not os.path.isdir(export_dir):
        sys.exit(f'no export at {export_dir}')
    plan = wp.build_plan(export_dir)
    by_mesh = records_by_mesh(export_dir)

    rows = []
    for mesh, recs in sorted(by_mesh.items()):
        flags = wp.biped_flags_for(plan, 'meshes/' + mesh, 'meshes')
        if not flags:
            continue
        slots = wp.body_parts_for_flags(flags)
        if not slots:
            continue
        rows.append((mesh, slots, recs))

    print(f'{args.plugin}: {len(rows)} wearable meshes with an authored slot')
    multi = [r for r in rows if len(r[1]) > 1]
    print(f'  multi-slot (resolved per shape from skin weights): {len(multi)}')

    if args.formids:
        seen = set()
        print('\nFormID   slots                 name')
        for mesh, slots, recs in rows:
            names = '+'.join(BP_NAME.get(s, str(s)) for s in slots)
            for fid, label, _bf in recs:
                if fid in seen:
                    continue
                seen.add(fid)
                print(f'{fid}  {names:20}  {label}')


if __name__ == '__main__':
    main()
