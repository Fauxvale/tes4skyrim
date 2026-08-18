"""Which meshes does a code change actually alter, and which stay identical?

The validation Bryant asked for: build the mesh stage twice — once with the
change active, once without — and byte-compare the two trees.  Everything that
comes out identical needs no inspection at all, which is what shrinks a
spot-check pool from twenty thousand meshes to the handful that really moved.

It is only meaningful because mesh conversion is byte-reproducible across
processes (`b2251cf perf: vectorise mesh tangent space and determinise NIF
string tables`).  Before that, set iteration order landed in the output and
every A/B was noise.

Usage:
  python tools/mesh_ab_diff.py <baseline_meshes_dir> <changed_meshes_dir>
                               [--expect SUBSTRING ...] [--all]

`--expect` marks which differing paths are INTENDED; anything differing that
matches none of them is reported separately as unexplained, which is the line
that actually matters.
"""
import hashlib
import os
import sys
from collections import Counter
from multiprocessing import Pool, cpu_count
from pathlib import Path


def _digest(path):
    h = hashlib.sha1()
    try:
        with open(path, 'rb') as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b''):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _pair(args):
    rel, a, b = args
    da, db = _digest(a), _digest(b)
    if da is None or db is None:
        return (rel, 'missing')
    return (rel, 'same' if da == db else 'differs')


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if len(args) < 2:
        print(__doc__)
        return 1
    base, changed = Path(args[0]), Path(args[1])
    show_all = '--all' in sys.argv
    expect = []
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == '--expect':
            expect = [x.lower() for x in argv[i + 1:]
                      if not x.startswith('--')]
            break

    for d in (base, changed):
        if not d.is_dir():
            print(f'no such tree: {d}')
            return 1

    print(f'baseline  {base}\nchanged   {changed}', flush=True)
    rel_base = {str(p.relative_to(base)).lower()
                for p in base.rglob('*') if p.is_file()}
    rel_chg = {str(p.relative_to(changed)).lower()
               for p in changed.rglob('*') if p.is_file()}

    only_base = sorted(rel_base - rel_chg)
    only_chg = sorted(rel_chg - rel_base)
    common = sorted(rel_base & rel_chg)
    print(f'{len(common)} files in both, {len(only_base)} only in baseline, '
          f'{len(only_chg)} only in changed\n', flush=True)

    jobs = [(r, base / r, changed / r) for r in common]
    verdict = {}
    with Pool(max(1, cpu_count() - 1)) as pool:
        for n, (rel, v) in enumerate(pool.imap_unordered(_pair, jobs, 64), 1):
            verdict[rel] = v
            if n % 5000 == 0:
                print(f'  {n}/{len(jobs)} ...', flush=True)

    same = [r for r, v in verdict.items() if v == 'same']
    diff = sorted(r for r, v in verdict.items() if v == 'differs')

    print(f'\n{"IDENTICAL":<12} {len(same):>6}')
    print(f'{"DIFFERENT":<12} {len(diff):>6}')
    if only_chg:
        print(f'{"NEW":<12} {len(only_chg):>6}')
    if only_base:
        print(f'{"REMOVED":<12} {len(only_base):>6}')
    tot = len(common) + len(only_chg) + len(only_base)
    if tot:
        print(f'\n{len(same) * 100.0 / tot:.2f}% of the tree is byte-identical')

    if expect:
        unexplained = [r for r in diff
                       if not any(e in r for e in expect)]
        print(f'\nexpected to differ (matching {expect}): '
              f'{len(diff) - len(unexplained)}')
        print(f'UNEXPLAINED differences: {len(unexplained)}')
        if unexplained:
            print('  these are the ones to look at:')
            for r in (unexplained if show_all else unexplained[:40]):
                print(f'    {r}')
            if not show_all and len(unexplained) > 40:
                print(f'    ... ({len(unexplained) - 40} more, pass --all)')
        else:
            print('  none — every changed file was accounted for')

    by_folder = Counter(r.split(os.sep)[0] if os.sep in r else '.'
                        for r in diff)
    if by_folder:
        print('\ndifferences by top folder:')
        for folder, c in by_folder.most_common(15):
            print(f'  {c:>6}  {folder}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
