"""Versioned pipeline artifacts: the JSON one stage writes and another reads.

The export/asset stages hand data to the import stage through JSON files on
disk.  Those files outlive the run that wrote them -- and, when a plugin has a
TES4 master, they outlive the PLUGIN that wrote them: a dependent plugin reads
its master's `creature_projects.json` and never rewrites it.  So a file on disk
can predate the code reading it by an arbitrary amount.

Add a required field to a producer and every existing file becomes unreadable.
Before this module that surfaced as a bare `KeyError` from deep inside a
builder, naming neither the file nor the plugin whose stage had to be re-run
(commit a4fdb47 added `behavior_hkx` to creature_projects.json; DLCFrostcrag
then died in creature_idles with `KeyError: 'behavior_hkx'`, pointing at
DLCFrostcrag when the stale file belonged to Oblivion.esm).

An artifact written here carries an envelope::

    {"version": 1, "plugin": "Oblivion.esm", "stage": "--creatures-only",
     "data": {...}}

`plugin` and `stage` exist to make the failure ACTIONABLE.  The consumer cannot
infer either one: the file may belong to a master, and only the producer knows
which `convert.py` flag rebuilds it.  Without them the error can name a problem
but not its fix.

Two independent checks, because a version is only as good as the discipline
that bumps it:

  * `version` mismatch -- the deliberate signal.  A file with no `version` key
    is v0 (every file written before this module existed), so old files are
    rejected cleanly instead of being half-read.
  * `required` keys missing from any entry -- the backstop for a required field
    added WITHOUT a version bump, which is the mistake that caused the original
    bug.  tests/test_artifact_schema.py catches the same slip at development
    time; this catches it on a user's machine.

Not every JSON file belongs here.  A pure CACHE (mesh bounds, extracted-BSA
bookkeeping, voice durations) is recomputable, so a stale one should be
discarded and rebuilt, never fatal -- `collision_extract.BOUNDS_SCHEMA_VERSION`
already does that with an in-band `__schema__` sentinel and is left alone.
Register an artifact here only when losing it means losing AUTHORED work the
consumer cannot rebuild by itself.

`master_manifest.py` predates this and carries its own equivalent envelope; its
remap logic is load-bearing (see the TWMP Valenwood note there) and is not
migrated.
"""

import json
import os


class StaleArtifactError(RuntimeError):
    """A pipeline artifact was written by an incompatible converter version."""


class Artifact:
    """One registered artifact: its current version and per-entry contract.

    `required` is the set of keys the CONSUMERS subscript without a default.
    It is the contract the version number stands for -- change it and the
    version must move, which tests/test_artifact_schema.py enforces.
    """

    __slots__ = ('version', 'stage', 'required', 'per_entry')

    def __init__(self, version, stage, required=(), per_entry=True):
        self.version = version
        self.stage = stage
        self.required = tuple(required)
        # True  -> `data` is {name: entry} and `required` applies to each entry
        # False -> `required` applies to `data` itself
        self.per_entry = per_entry


# The registry.  `stage` is the convert.py flag that REBUILDS the file -- it is
# quoted verbatim into the error, so it must stay correct.
ARTIFACTS = {
    'creature_projects.json': Artifact(
        version=1, stage='--creatures-only',
        # Read without a default by creature_idles.build_creature_idles
        # (behavior_hkx) and creature_races (_build_race / _build_arma /
        # _build_creature_bptd / _bodies_of).  A file missing any of these
        # kills the import.
        required=('behavior_hkx', 'body_dir', 'skeleton_nif', 'project_hkx',
                  'bodies')),
}


def artifact_name(path):
    """Registry key for a path (the basename)."""
    return os.path.basename(path)


def _describe(name, path, plugin, art, found_version, missing):
    """The user-facing failure: what is stale, and what to run to fix it."""
    who = plugin or '(unknown plugin)'
    detail = '; missing: ' + ', '.join(missing) if missing else ''
    lines = [
        name + ' is out of date - cannot convert.',
        '',
        '  ' + who + '/' + name + ' was written by an older version of the',
        '  converter (format v%s, need v%s%s).'
        % (found_version, art.version, detail),
        '',
        '  Read from: ' + path,
    ]
    if plugin:
        lines += [
            '',
            "  A plugin re-uses its MASTER's converted data and never rewrites",
            "  the master's files, so the file to refresh often belongs to a",
            '  different plugin than the one you are converting.',
            '',
            '  Re-run:',
            '    python convert.py -f %s %s' % (plugin, art.stage),
        ]
    else:
        lines += ['', '  Re-run:',
                  '    python convert.py -f <plugin> %s' % art.stage]
    return '\n'.join(lines)


def write_artifact(path, plugin, data):
    """Write `data` wrapped in the versioned envelope.

    `plugin` is the plugin whose stage produced it -- NOT necessarily the one
    that will read it.  It is what lets a consumer name the right file to
    rebuild.
    """
    art = ARTIFACTS[artifact_name(path)]
    payload = {'version': art.version, 'plugin': plugin, 'stage': art.stage,
               'data': data}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=1, sort_keys=True)


def _missing_keys(art, data):
    """Required keys absent from `data` (or from any of its entries)."""
    if not art.required:
        return []
    if not art.per_entry:
        if not isinstance(data, dict):
            return list(art.required)
        return [k for k in art.required if k not in data]
    missing = set()
    if isinstance(data, dict):
        for entry in data.values():
            if isinstance(entry, dict):
                missing.update(k for k in art.required if k not in entry)
    return sorted(missing)


def read_artifact(path, plugin_hint=None):
    """Return an artifact's `data`, or raise StaleArtifactError.

    `plugin_hint` names the plugin to blame when the file is too old to carry
    its own `plugin` field (v0 has no envelope at all), which is the case for
    every file written before this module existed.
    """
    art = ARTIFACTS[artifact_name(path)]
    with open(path, encoding='utf-8') as f:
        payload = json.load(f)

    # A pre-envelope file is a bare dict with no 'version' -> v0.
    if isinstance(payload, dict) and 'version' in payload:
        found = payload.get('version')
        plugin = payload.get('plugin') or plugin_hint
        data = payload.get('data')
    else:
        # v0: no envelope.  Some payloads carry their own plugin name
        # inside the data; prefer it over the hint so the error still names
        # the right file to rebuild.
        found, data = 0, payload
        plugin = plugin_hint
        if not plugin and isinstance(payload, dict):
            inner = payload.get('plugin')
            plugin = inner if isinstance(inner, str) else None

    if found != art.version:
        raise StaleArtifactError(
            _describe(artifact_name(path), path, plugin, art, found, []))

    # Same version, but a required field was added without a bump.  Catch it
    # here rather than letting a builder subscript it 900 lines later.
    missing = _missing_keys(art, data)
    if missing:
        raise StaleArtifactError(
            _describe(artifact_name(path), path, plugin, art, found, missing))
    return data


def _master_names(export_dir):
    """This plugin's TES4 master names, in load order, from its export header."""
    header = os.path.join(export_dir, '_HEADER.txt')
    if not os.path.isfile(header):
        return []
    names = []
    with open(header, 'r', encoding='utf-8') as f:
        for line in f:
            if line.startswith('Master['):
                _, _, val = line.partition('=')
                names.append(val.strip())
    return names


def preflight_artifacts(export_dir):
    """Validate every artifact this plugin will read, BEFORE any real work.

    The consumers sit deep in the import (creature projects are read ~15 steps
    into phase 0), so without this a stale file costs the user a minute of
    fid maps, cross-ref graphs and script plans before it says anything.  The
    fix is the same either way, so there is no reason to find out late.

    Checks this plugin's own artifacts AND its masters' -- a dependent plugin
    reads its master's creature projects and never rewrites them, so the stale
    file is often the master's.  Raises StaleArtifactError; a MISSING file is
    not an error (plenty of plugins ship no creatures).
    """
    from .overrides import _export_root, _master_export_dir

    seen = []
    # This plugin's artifacts, then each master's.  The hint names
    # the plugin whose stage rebuilds each file -- for the plugin's own export
    # that is the export dir's name, for a master's it is the master.
    own = os.path.basename(os.path.normpath(export_dir))
    roots = [(export_dir, own)]
    try:
        root = _export_root(export_dir)
        for name in _master_names(export_dir):
            roots.append((_master_export_dir(root, name), name))
    except Exception:
        # A layout we cannot resolve just means fewer preflight checks; the
        # real read still raises later.
        pass
    for base, plugin in roots:
        p = os.path.join(base, 'creature_projects.json')
        if os.path.isfile(p):
            seen.append((p, plugin))

    for path, plugin in seen:
        read_artifact(path, plugin)
