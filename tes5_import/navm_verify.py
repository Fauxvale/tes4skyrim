"""Pick which navmesh cells get re-built and compared against the cache.

The cache tag hashes the navmesh sources, so it cannot see a shapely/GEOS or
`.pyd` change -- and once a cache may be ADOPTED (re-keyed after proving the
geometry is unchanged) a matching tag no longer proves the entries were produced
by the current code.  Re-building a sample of cache hits on every import is what
restores the "a wrong cache is slow, never incorrect" guarantee.

See: docs/commentary/tes5_import_navmesh.md#verifying-a-cache-against-fresh-geometry
"""

from collections import defaultdict

#: Cells re-built and compared against the cache on every import, by default.
VERIFY_DEFAULT = 40

#: Env var overriding VERIFY_DEFAULT; 0 disables verification entirely.
VERIFY_ENV_VAR = 'TESCONV_NAVMESH_VERIFY'


def verify_budget(explicit: int = None) -> int:
    """How many cells to verify: explicit value, else the env var, else default."""
    if explicit is not None:
        return max(0, explicit)
    import os
    raw = os.environ.get(VERIFY_ENV_VAR, '').strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return VERIFY_DEFAULT


def _stratum(job: dict) -> str:
    """Which sampling bucket this navmesh job belongs to."""
    if job.get('extra_door_refrs'):
        return 'door'
    if job.get('land_rec') is not None:
        return 'exterior'
    return 'crowded' if len(job.get('refr_recs') or ()) >= 100 else 'interior'


def mark_jobs(jobs: list, budget: int) -> int:
    """Flag up to *budget* jobs with job['verify'], spread across strata.

    Returns how many were flagged.  The PARENT owns this choice: `initargs` are
    copied into every worker, so a per-worker budget would multiply by the
    worker count and rebuild a large fraction of the cache instead of a sample.
    """
    if budget <= 0 or not jobs:
        return 0
    buckets = defaultdict(list)
    for i, job in enumerate(jobs):
        buckets[_stratum(job)].append(i)
    chosen = []
    names = sorted(buckets)
    per = max(1, budget // len(names))
    for name in names:
        idx = buckets[name]
        step = max(1, len(idx) // per)
        chosen.extend(idx[::step][:per])
    picked = sorted(set(chosen))[:budget]
    for i in picked:
        jobs[i]['verify'] = True
    return len(picked)


def report(cache: dict) -> list:
    """Cells whose cached geometry did not reproduce, as [(key, meta), ...]."""
    return [(key, m) for key, (_b, m) in cache.items()
            if m and m.get('verify_mismatch')]


def verified_count(cache: dict) -> int:
    """How many cells actually ran a verification build."""
    return sum(1 for (_b, m) in cache.values() if m and m.get('verified'))


def uncertify(geom_cache) -> bool:
    """Drop CACHE_TAG so the next run does not trust a cache that failed.

    Only the stamp goes.  THIS run has already read most of the entries, and
    each verified cell that mismatched kept its own fresh build, so deleting
    them mid-run would strand the reads still to come while fixing nothing.  An
    unstamped cache is what `verify` and the pre-push gate call stale -- exactly
    the state a cache that failed to reproduce belongs in.
    """
    if not geom_cache:
        return False
    import os
    try:
        os.remove(os.path.join(geom_cache[0], 'CACHE_TAG'))
        return True
    except OSError:
        return False


def report_failures(cache: dict) -> bool:
    """Print cells that produced no navmesh.  True if any did.

    Reported in the PARENT: workers run under pythonw.exe where stdout goes
    nowhere, so a failed cell would otherwise vanish silently and leave the
    plugin a navmesh short with nothing in the log.  A run with failures must
    not stamp the cache -- entries are missing, and stamping would advertise a
    partial cache as a full one to anyone who downloads it.
    """
    failures = [(key, m) for key, (b, m) in cache.items()
                if b is None and m and m.get('error')]
    if not failures:
        return False
    print('    WARNING: %d cells produced no navmesh:' % len(failures))
    for (cell_fid, pgrd_fid), m in failures[:20]:
        print('      cell %08X pgrd %08X: %s'
              % (cell_fid, pgrd_fid, m['error']))
    if len(failures) > 20:
        print('      ... and %d more' % (len(failures) - 20))
    return True


def report_verification(cache: dict, geom_cache) -> bool:
    """Print the verification result; unstamp a cache that did not reproduce.

    True when a mismatch was found.  The plugin is still correct either way:
    a mismatching cell keeps the fresh build made to test it.

    A mismatch means the cached geometry is not what this code builds -- which
    the tag cannot detect, since it hashes sources rather than output.
    """
    checked = verified_count(cache)
    if not checked:
        return False
    bad = report(cache)
    if not bad:
        print('    Navmesh cache verified: %d cells rebuilt, all identical.'
              % checked)
        return False
    print('    WARNING: navmesh cache FAILED verification -- %d/%d rebuilt '
          'cells differ from the cached geometry.' % (len(bad), checked))
    for (cell_fid, pgrd_fid), _m in bad[:10]:
        print('      cell %08X pgrd %08X' % (cell_fid, pgrd_fid))
    uncertify(geom_cache)
    print('      Mismatched cells used their FRESH build, so this plugin is '
          'correct; the cache is now marked stale.')
    return True
