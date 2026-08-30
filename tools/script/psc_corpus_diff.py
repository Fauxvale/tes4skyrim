#!/usr/bin/env python3
"""Snapshot the generated Papyrus corpus and diff a later run against it.

Why: `script_convert/` is being rewritten around a parse tree, and the contract
is that output stays NEARLY IDENTICAL -- the rewrite reproduces current
behaviour, bugs included.  The ~40,000 .psc files under `output/` are the only
evidence that holds, but they are gitignored AND `build_script_context` wipes
the output directory at the start of every `--scripts-only` run.  So the
baseline has to be copied somewhere the pipeline does not write, BEFORE the
first change, or it is gone.

    python tools/script/psc_corpus_diff.py snapshot --all
    ... edit script_convert/, run convert.py --scripts-only ...
    python tools/script/psc_corpus_diff.py compare --all --show 20

`compare` is a REVIEW QUEUE, not a pass/fail gate: the baseline is known to
contain conversion bugs, so a diff is something to read and classify, not
automatically a failure.  What it does enforce is VOLUME -- `--budget N` fails
when more than N files changed, because a transform that rewrites thousands of
scripts is wrong by construction and should be reverted rather than triaged.

`--gate` marks files whose diff blocks regardless of the budget (the heavily
play-tested CharacterGen scripts); any change there is presumed a regression.

Snapshots store the file TEXT, not just hashes: after a regeneration the old
bytes are gone, so a hash-only baseline could say THAT a file changed but never
HOW.  Text lives in a zip next to the manifest, so a snapshot of the full
corpus is a few tens of MB.

    snapshot   hash + archive output/<plugin>/scripts/source/*.psc
    compare    re-hash, report changed/added/removed, print unified diffs
    promote    replace the baseline with current output (after review)

🛑 SNAPSHOT WHAT IS ON DISK, NOT WHAT HEAD PRODUCES.  `output/` is whatever the
last run left there, which may be many commits old -- measured 2026-08-28: a
snapshot of the existing tree reported 266 changed Nehrim scripts for a change
that actually altered nothing, because the tree predated `TES4Polyfill.SetDestroyed`
(already in HEAD).  Before trusting a baseline, BUILD IT: check out the
reference commit, run `convert.py -f <plugin> --scripts-only`, then `promote`.
Otherwise every stale file reads as a regression and buries the real diff.
"""

import argparse
import difflib
import fnmatch
import hashlib
import json
import os
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

OUTPUT_DIR = ROOT / 'output'
DEFAULT_BASELINE = ROOT / 'temp' / 'psc_baseline'

# Scripts whose conversion has been validated in game across many playthroughs.
# A diff in one of these is presumed a regression until proven equivalent, so
# it blocks even when the overall file count is within budget.
DEFAULT_GATE = [
    'TES4_CharGenQuest.psc',
    'TES4_CGEmperorScript.psc',
    'TES4_BaurusScript.psc',
    'TES4_ValenDrethScript.psc',
    'TES4_Dark04ValenDrethScript.psc',
    'TES4_Dark04ValenDrethGate.psc',
]


def plugin_dirs(names=None, use_all=False):
    """[(plugin_name, scripts_source_dir)] for plugins with generated .psc."""
    if not OUTPUT_DIR.is_dir():
        raise SystemExit(f'no output directory: {OUTPUT_DIR}')
    out = []
    for child in sorted(OUTPUT_DIR.iterdir()):
        if not child.is_dir():
            continue
        src = child / 'scripts' / 'source'
        if not src.is_dir():
            continue
        if use_all or not names or child.name in names:
            out.append((child.name, src))
    if names and not use_all:
        missing = set(names) - {n for n, _ in out}
        if missing:
            raise SystemExit(f'no generated scripts for: {", ".join(sorted(missing))}')
    if not out:
        raise SystemExit('no plugins with generated scripts under output/')
    return out


def _hash_file(path):
    with open(path, 'rb') as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def scan_plugin(src_dir, workers):
    """{relative_name: sha256} for every .psc under src_dir.

    Hashing 16,519 files single-threaded measured 75.7s, which would put a
    full-corpus scan over the 120s budget for a check command.  This is pure
    I/O, so threads are the right tool (see docs/performance_notes.md).
    """
    files = sorted(p for p in src_dir.rglob('*.psc'))
    if not files:
        return {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        digests = list(ex.map(_hash_file, files))
    return {str(p.relative_to(src_dir)).replace('\\', '/'): d
            for p, d in zip(files, digests, strict=True)}


def baseline_paths(baseline, plugin):
    safe = plugin.replace(os.sep, '_')
    return (Path(baseline) / f'{safe}.json', Path(baseline) / f'{safe}.zip')


def cmd_snapshot(args):
    base = Path(args.baseline)
    base.mkdir(parents=True, exist_ok=True)
    total = 0
    for plugin, src in plugin_dirs(args.plugin, args.all):
        digests = scan_plugin(src, args.workers)
        manifest, archive = baseline_paths(base, plugin)
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zf:
            for rel in digests:
                zf.write(src / rel, rel)
        manifest.write_text(json.dumps(
            {'plugin': plugin, 'count': len(digests), 'files': digests},
            indent=1), encoding='utf-8')
        print(f'  {plugin:32} {len(digests):6} files -> {archive.name}')
        total += len(digests)
    print(f'\nsnapshot: {total} files in {base}')
    return 0


def _load_baseline(base, plugin):
    manifest, archive = baseline_paths(base, plugin)
    if not manifest.is_file():
        raise SystemExit(f'no baseline for {plugin!r} -- run `snapshot` first')
    return json.loads(manifest.read_text(encoding='utf-8'))['files'], archive


def _is_gated(name, patterns):
    base = os.path.basename(name)
    return any(fnmatch.fnmatch(base, p) for p in patterns)


def cmd_compare(args):
    base = Path(args.baseline)
    gate = args.gate if args.gate is not None else DEFAULT_GATE
    shown = 0
    tot_changed = tot_added = tot_removed = 0
    gated_hits = []

    for plugin, src in plugin_dirs(args.plugin, args.all):
        old, archive = _load_baseline(base, plugin)
        new = scan_plugin(src, args.workers)

        changed = sorted(n for n in old.keys() & new.keys() if old[n] != new[n])
        added = sorted(new.keys() - old.keys())
        removed = sorted(old.keys() - new.keys())
        tot_changed += len(changed)
        tot_added += len(added)
        tot_removed += len(removed)

        flag = '' if not (changed or added or removed) else '  <-- differs'
        print(f'  {plugin:32} {len(new):6} files   '
              f'changed {len(changed):5}  added {len(added):5}  '
              f'removed {len(removed):5}{flag}')

        gated_hits += [f'{plugin}/{n}' for n in changed + added + removed
                       if _is_gated(n, gate)]

        if changed and shown < args.show:
            with zipfile.ZipFile(archive) as zf:
                for name in changed:
                    if shown >= args.show:
                        break
                    before = zf.read(name).decode('utf-8', 'replace').splitlines()
                    after = (src / name).read_text(
                        encoding='utf-8', errors='replace').splitlines()
                    diff = difflib.unified_diff(
                        before, after, f'baseline/{name}', f'output/{name}',
                        lineterm='', n=2)
                    print('\n'.join(diff))
                    print()
                    shown += 1

    total = tot_changed + tot_added + tot_removed
    print(f'\ntotal: {tot_changed} changed, {tot_added} added, '
          f'{tot_removed} removed')
    if shown < tot_changed:
        print(f'  ({tot_changed - shown} more changed files not shown; '
              f'raise --show)')

    rc = 0
    if gated_hits:
        print(f'\nGATED FILES CHANGED ({len(gated_hits)}) -- these are '
              f'play-tested, treat every diff as a regression:')
        for g in gated_hits:
            print(f'    {g}')
        rc = 1
    if args.budget is not None and total > args.budget:
        print(f'\nOVER BUDGET: {total} files changed, limit {args.budget}. '
              f'A transform this broad is wrong -- revert, do not triage.')
        rc = 1
    return rc


def cmd_promote(args):
    print('promoting current output to the baseline (review first!)')
    return cmd_snapshot(args)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    def common(p):
        p.add_argument('-f', '--plugin', action='append',
                       help='plugin name (repeatable); default all')
        p.add_argument('--all', action='store_true',
                       help='every plugin with generated scripts')
        p.add_argument('--baseline', default=str(DEFAULT_BASELINE),
                       help=f'baseline directory (default {DEFAULT_BASELINE})')
        p.add_argument('--workers', type=int,
                       default=max(1, (os.cpu_count() or 2) - 1),
                       help='parallel hash workers')

    common(sub.add_parser('snapshot', help='record the current corpus'))

    c = sub.add_parser('compare', help='diff current output against the baseline')
    common(c)
    c.add_argument('--show', type=int, default=3,
                   help='print unified diffs for the first N changed files')
    c.add_argument('--budget', type=int,
                   help='fail if more than N files differ')
    c.add_argument('--gate', action='append',
                   help='glob of files whose diff always fails (repeatable); '
                        'defaults to the CharacterGen scripts')

    common(sub.add_parser('promote', help='overwrite the baseline with current output'))

    args = ap.parse_args(argv)
    return {'snapshot': cmd_snapshot,
            'compare': cmd_compare,
            'promote': cmd_promote}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
