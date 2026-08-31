#!/usr/bin/env python
"""Merge the current branch into master locally, with the navmesh cache in step.

The navmesh cache gate is a PRE-PUSH hook, so it only ever sees a direct push
to master -- a merge performed on GitHub runs no local hook and would publish
nothing.  This does the merge here, where the hook can run.

    python tools/release/merge_to_master.py --dry-run   # what would happen
    python tools/release/merge_to_master.py             # merge, then push

Order matters.  The cache is adopted BEFORE the merge, because adoption hashes
the WORKING TREE's navmesh sources: adopting after checking out master would
key the cache to master's code rather than the code being merged.

Nothing here force-pushes, rebases, or touches the index beyond the merge
itself.  A dirty tree aborts: a merge is hard to unpick, and uncommitted work
in these files is exactly what would make it ambiguous.

See: docs/commentary/tes5_import_navmesh.md#the-shared-navmesh-cache--design-rationale
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

MASTER = 'master'


def git(*args, capture=True, check=False):
    """Run a git command in the repo root."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    return subprocess.run(['git', *args], cwd=root, check=check,
                          capture_output=capture, text=True)


def current_branch() -> str:
    """The branch HEAD is on, or '' when detached."""
    out = git('rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
    return '' if out == 'HEAD' else out


def is_dirty() -> bool:
    """True when the working tree or index has uncommitted changes."""
    return bool(git('status', '--porcelain').stdout.strip())


def cache_is_current() -> bool:
    """Does every publishable cache match the working tree's navmesh sources?"""
    from tools.navmesh import navmesh_cache as nc
    from tools.navmesh import navmesh_cache_hook as hook
    return all(hook.cache_matches_tag(p, nc.source_tag(p))
               for p in nc.discover_plugins())


def ensure_cache(sample: int, dry_run: bool) -> bool:
    """Adopt any stale cache; True when every cache ends up current."""
    if cache_is_current():
        print('navmesh cache: already current.')
        return True
    print('navmesh cache: stale -- attempting adoption '
          '(proves the geometry is unchanged, then re-keys).')
    if dry_run:
        print('  --dry-run: would run tools/navmesh/navmesh_adopt.py')
        return False
    rc = subprocess.run(
        [sys.executable, os.path.join('tools', 'navmesh', 'navmesh_adopt.py'),
         '--sample', str(sample)]).returncode
    if rc != 0 or not cache_is_current():
        print('')
        print('navmesh cache: adoption did not settle every cache.')
        print('A cache that will not adopt has REAL geometry changes; '
              'regenerate it:')
        print('  python convert.py -f "<plugin>" --import-only')
        return False
    return True


def do_merge(branch: str, dry_run: bool) -> int:
    """Fast-forward-or-merge *branch* into master and push."""
    steps = [
        ('checkout master', ('checkout', MASTER)),
        ('merge %s' % branch, ('merge', '--no-ff', branch,
                               '-m', 'Merge branch %r' % branch)),
        ('push master', ('push', 'origin', MASTER)),
    ]
    for label, args in steps:
        print('\n$ git %s' % ' '.join(args))
        if dry_run:
            continue
        res = git(*args, capture=False)
        if res.returncode != 0:
            print('\nFAILED at: %s' % label)
            if args[0] == 'merge':
                print('Resolve the conflict, then re-run; or `git merge '
                      '--abort` to back out.')
            elif args[0] == 'push':
                print('The merge is committed locally. Fix the push and run '
                      '`git push origin %s`.' % MASTER)
                print('The pre-push hook publishes the navmesh cache, so do '
                      'not bypass it with --no-verify.')
            return res.returncode
    return 0


def main(argv=None) -> int:
    """Entry point.  Returns 0 when the merge (or dry run) succeeded."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--branch', help='branch to merge (default: current)')
    ap.add_argument('--sample', type=int, default=40,
                    help='cells adoption rebuilds to prove the cache (40)')
    ap.add_argument('--dry-run', action='store_true',
                    help='print every step without running it')
    args = ap.parse_args(argv)

    branch = args.branch or current_branch()
    if not branch:
        print('HEAD is detached; check out a branch first.')
        return 1
    if branch == MASTER:
        print('Already on %s -- nothing to merge.' % MASTER)
        return 1
    if is_dirty():
        print('Working tree is dirty. Commit or set your changes aside first:')
        print(git('status', '--short').stdout)
        return 1

    print('Merging %r into %s.' % (branch, MASTER))
    if not ensure_cache(args.sample, args.dry_run) and not args.dry_run:
        return 1
    return do_merge(branch, args.dry_run)


if __name__ == '__main__':
    raise SystemExit(main())
