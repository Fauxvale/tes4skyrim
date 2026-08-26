#!/usr/bin/env python
"""Extract every quest objective slot from a TES4 export as JSON.

Skyrim shows two different quest texts: the long retrospective log entry
(CNAM) in the journal, and a short imperative line (NNAM) on the objective
HUD.  Oblivion authored only ONE string per stage (`Stage[].Log[].Text`), so
NNAM currently receives the long paragraph -- measured against Skyrim.esm,
vanilla NNAM averages 31 chars while our converted objectives average 158.

There is no authored TES4 source for the short form, so it is supplied by a
curated table keyed on `(EditorID, stage_index)` -- both authored values, so
the table cannot cause FormID drift.  This tool dumps the exact set of slots
that table must cover.

The slot list is derived by calling `dialog_converter.quest_objective_texts`'s
own selection rules (first non-empty log text per stage, duplicate stage
indices skipped, gamepad variants dropped, control tokens expanded), so the
keys here cannot drift from the keys convert_QUST actually emits.

Usage:
    python tools/generators/objective_text_extract.py --plugin Oblivion.esm
    python tools/generators/objective_text_extract.py --plugin Oblivion.esm Nehrim.esm \
        --out temp/slots.json
    python tools/generators/objective_text_extract.py --all --stats
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tes5_import.text_reader import parse_export_file, get_int, get_str
from tes5_import.dialog_converter import _pc_stage_texts


def objective_slots(rec):
    """[(stage_index, long_text)] in the exact order convert_QUST emits NNAM.

    Mirrors dialog_converter.quest_objective_texts but keeps the stage index,
    which is the second half of the curated table's key.
    """
    out = []
    seen_stages = set()
    for i in range(get_int(rec, 'StageCount')):
        stage_idx = get_int(rec, f'Stage[{i}].Index')
        if stage_idx in seen_stages:
            continue
        log_count = get_int(rec, f'Stage[{i}].LogCount')
        texts = (_pc_stage_texts([get_str(rec, f'Stage[{i}].Log[{j}].Text')
                                  for j in range(log_count)])
                 if log_count > 0
                 else [get_str(rec, f'Stage[{i}].LogEntry')])
        txt = next((x for x in texts if x), None)
        if not txt:
            continue
        seen_stages.add(stage_idx)
        out.append((stage_idx, txt))
    return out


def extract(export_dir):
    """[{plugin, editor_id, formid, quest_name, stage, long}] for one export."""
    path = os.path.join(export_dir, 'QUST.txt')
    if not os.path.isfile(path):
        return []
    plugin = os.path.basename(export_dir.rstrip('/\\'))
    rows = []
    for rec in parse_export_file(path):
        edid = get_str(rec, 'EditorID')
        if not edid:
            continue
        full = get_str(rec, 'FULL')
        for stage_idx, txt in objective_slots(rec):
            rows.append({
                'plugin': plugin,
                'editor_id': edid,
                'formid': get_str(rec, 'FormID'),
                'quest_name': full,
                'stage': stage_idx,
                'long': txt,
            })
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--plugin', nargs='+', default=[],
                    help='Plugin name(s) under export/, e.g. Oblivion.esm')
    ap.add_argument('--all', action='store_true',
                    help='Every plugin under export/ that has a QUST.txt')
    ap.add_argument('--export-root', default='export')
    ap.add_argument('--out', help='Write JSON here (default: stdout)')
    ap.add_argument('--stats', action='store_true',
                    help='Print length statistics instead of the rows')
    args = ap.parse_args()

    plugins = list(args.plugin)
    if args.all:
        plugins = [d for d in sorted(os.listdir(args.export_root))
                   if os.path.isfile(os.path.join(args.export_root, d, 'QUST.txt'))]
    if not plugins:
        ap.error('give --plugin NAME... or --all')

    rows = []
    for p in plugins:
        got = extract(os.path.join(args.export_root, p))
        print(f'{p}: {len(got)} objective slots', file=sys.stderr)
        rows.extend(got)

    if args.stats:
        import statistics
        lens = [len(r['long']) for r in rows]
        if lens:
            print(f'slots={len(lens)} mean={statistics.mean(lens):.1f} '
                  f'median={statistics.median(lens)} max={max(lens)}')
            for thr in (48, 60, 80):
                over = sum(1 for x in lens if x > thr)
                print(f'  over {thr} chars: {over} ({100 * over / len(lens):.0f}%)')
        return

    text = json.dumps(rows, indent=1, ensure_ascii=False)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
        with open(args.out, 'w', encoding='utf-8') as fh:
            fh.write(text)
        print(f'wrote {args.out} ({len(rows)} slots)', file=sys.stderr)
    else:
        print(text)


if __name__ == '__main__':
    main()
