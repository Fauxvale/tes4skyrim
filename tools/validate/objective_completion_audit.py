#!/usr/bin/env python
"""Which objective each converted quest stage FINISHES, and what a change moved.

An objective that stops closing, or starts closing EARLIER, is a silent journal
regression: the player ticks a step off before finishing it, or never does.

Take a snapshot BEFORE editing the completion rules, then diff AFTER:

    python -m tools.validate.objective_completion_audit --save temp/before.json
    ... edit script_convert/objective_completion.py ...
    python -m tools.validate.objective_completion_audit --diff temp/before.json

`--diff` exits non-zero if any objective regressed. The snapshot is a scratch
file, not a committed expectation — it is only meaningful against the edit it
brackets.

**The residue is a BIASED sample** — it holds only the objectives the rules give
up on, so it can never reveal one the rules close WRONGLY. That is why the diff
is over all objectives: a predicate reading only `GetStage` gates closed the six
order-independent `fbmwBMStones` rituals sequentially, and none of those
appeared in the residue.

    python -m tools.validate.objective_completion_audit --residue

See: docs/commentary/script_convert.md#journal-objective-completion
"""
import argparse
import json
import os
import sys

from script_convert.objective_completion import _closed_by, parallel_stages
from tes5_import.dialog_converter import _pc_stage_texts
from tes5_import.text_reader import get_int, get_str, parse_export_file


def _fragments(rec):
    """(stage, log, text, None, None, stage_arr, log_arr) as the pipeline builds."""
    out = []
    for i in range(get_int(rec, 'StageCount')):
        stage_idx = get_int(rec, f'Stage[{i}].Index')
        log_count = get_int(rec, f'Stage[{i}].LogCount')
        texts = (_pc_stage_texts([get_str(rec, f'Stage[{i}].Log[{j}].Text')
                                  for j in range(log_count)])
                 if log_count > 0 else [get_str(rec, f'Stage[{i}].LogEntry')])
        for j, text in enumerate(texts):
            out.append((stage_idx, j, text, None, None, i, j))
    return out


def sweep(export_root):
    """({"plugin|edid|stage": closing stage or None}, [residue triples])."""
    closes, residue = {}, []
    for plugin in sorted(os.listdir(export_root)):
        path = os.path.join(export_root, plugin, 'QUST.txt')
        if not os.path.isfile(path):
            continue
        for rec in parse_export_file(path):
            edid = get_str(rec, 'EditorID')
            if not edid:
                continue
            closed_by, unresolved = _closed_by(rec, _fragments(rec))
            for stage, end in closed_by.items():
                closes[f'{plugin}|{edid}|{stage}'] = end
            for stage in unresolved:
                closes[f'{plugin}|{edid}|{stage}'] = None
                residue.append((plugin, edid, stage))
    return closes, residue


def _report_residue(residue):
    """Print the residue grouped by quest, flagging the parallel-table ones."""
    by_quest = {}
    for plugin, edid, stage in residue:
        by_quest.setdefault((plugin, edid), []).append(stage)
    print(f'residue: {len(residue)} slots across {len(by_quest)} quests')
    for (plugin, edid), stages in sorted(by_quest.items(),
                                         key=lambda kv: -len(kv[1])):
        tag = '  [parallel-table]' if parallel_stages(edid) else ''
        print(f'  {len(stages):3d}  {plugin} {edid}{tag}')


def classify(before, after):
    """{kind: [(key, was, now)]} for every objective the change moved."""
    moved = {'regressed': [], 'now closes': [], 'closes later': [], 'new': []}
    for key, now in sorted(after.items()):
        if key not in before:
            moved['new'].append((key, None, now))
            continue
        was = before[key]
        if was == now:
            continue
        if was is None:
            moved['now closes'].append((key, was, now))
        elif now is None or now < was:
            moved['regressed'].append((key, was, now))
        else:
            moved['closes later'].append((key, was, now))
    for key in sorted(before):
        if key not in after:
            moved['regressed'].append((key, before[key], 'GONE'))
    return moved


def _print_moved(moved):
    """Show every objective that moved, regressions first."""
    for kind in ('regressed', 'closes later', 'now closes', 'new'):
        rows = moved[kind]
        if not rows:
            continue
        print(f'{kind}: {len(rows)}')
        for key, was, now in rows[:40]:
            print(f'  {key}: {was} -> {now}')
        if len(rows) > 40:
            print(f'  ... {len(rows) - 40} more')


def main(argv=None):
    """CLI entry point. Returns 1 when --diff finds a regressed objective."""
    ap = argparse.ArgumentParser()
    ap.add_argument('--export', default='export')
    ap.add_argument('--residue', action='store_true')
    ap.add_argument('--save', help='write a snapshot to diff against later')
    ap.add_argument('--diff', help='compare against a snapshot from --save')
    args = ap.parse_args(argv)

    closes, residue = sweep(args.export)
    print(f'objectives scored: {len(closes)}')
    if args.residue:
        _report_residue(residue)

    if args.save:
        with open(args.save, 'w', encoding='utf-8') as fh:
            json.dump(closes, fh, indent=0, sort_keys=True)
        print(f'snapshot written: {args.save}')

    if args.diff:
        with open(args.diff, encoding='utf-8') as fh:
            before = json.load(fh)
        moved = classify(before, closes)
        _print_moved(moved)
        if moved['regressed']:
            print(f'FAIL: {len(moved["regressed"])} objectives regressed')
            return 1
        print('no regressions')
    return 0


if __name__ == '__main__':
    sys.exit(main())
