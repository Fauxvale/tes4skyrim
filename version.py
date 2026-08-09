"""Installed-version identity and upgrade-aware step selection.

Two questions this answers, both for a user who installs by pasting a new
source drop over the old folder:

  1. "What version am I running?"  -> `current_version()`
  2. "I just upgraded -- what do I have to re-run?"  -> `upgrade_plan()`

The version a user is *running* and the version they last *converted with* are
different facts, and only the second one tells you what is stale in `output/`.
Both are tracked:

  * `VERSION` (repo root, committed and stamped by the tag workflow) names the
    release the source tree IS.  A source drop carries it; git does not have to
    be present, and usually is not -- the release zip GitHub generates has no
    `.git` at all, so `git describe` is not a fallback that works where it
    matters.  In a dev checkout the file reads `0.0-dev`, and git tags take
    over so a developer still sees a real number.
  * `.conversion_state.json` (repo root, gitignored) records the version that
    last completed each pipeline step.  It is per-step and per-plugin because
    an upgrade that only touches meshes should not invalidate an Import the
    user ran an hour ago, and converting Nehrim should not mark Oblivion's
    steps fresh.

Pasting a new drop over the old install therefore leaves `VERSION` new and
`.conversion_state.json` old, and the difference between them is exactly the
set of commits whose changed paths decide which steps must re-run.  That path
-> step mapping is NOT reimplemented here: it is `tools/release_notes.py`,
which the tag workflow already uses to write the same answer into every
release's notes.  One mapping, two consumers.

Offline is the normal case, so the step list must not depend on the network.
Each release's notes embed their own step list, and the tag workflow also
writes `UPGRADE_STEPS.json` into the source tree: a cumulative
version -> steps table, so a drop knows what every prior version owes without
asking GitHub anything.

The table is maintained by CI, not by hand: tag-on-push.yml appends the new
release's entry via `tools/upgrade_table.py --add` and commits it with the
version stamp, and `--check` fails if the committed table has drifted from the
tag history.  A hole in it (a tag cut outside the workflow) makes
`steps_between` return None, which selects every step -- the feature degrades
to today's behaviour rather than under-selecting.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

VERSION_FILE = SCRIPT_DIR / "VERSION"
STEPS_FILE   = SCRIPT_DIR / "UPGRADE_STEPS.json"
STATE_FILE   = SCRIPT_DIR / ".conversion_state.json"

# What VERSION reads in a working checkout, where the tree sits between tags.
DEV_VERSION = "0.0-dev"


# ---------------------------------------------------------------------------
# Version identity
# ---------------------------------------------------------------------------

def _read_version_file() -> str | None:
    try:
        text = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _git(args: list[str]) -> str | None:
    """Run a git command, hidden.  None on any failure.

    POPEN_FLAGS is mandatory, not decoration: under `gui.pyw` the parent is
    console-less `pythonw.exe`, so every un-flagged spawn ALLOCATES ITS OWN
    CONSOLE WINDOW.  Omitting it here popped a terminal for each git call while
    the GUI was still building its window.
    """
    try:
        from subprocess_flags import POPEN_FLAGS
    except ImportError:
        POPEN_FLAGS = {}
    try:
        out = subprocess.run(["git", *args], cwd=SCRIPT_DIR,
                             capture_output=True, text=True, timeout=10,
                             **POPEN_FLAGS)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


_TAG_MATCH = ["--match", "[0-9]*.[0-9][0-9]", "--match", "[0-9]*.[0-9][0-9][0-9]"]


def _git_version() -> str | None:
    """What this checkout is, per git.  None when git is unavailable.

    Only a *fallback*: end users install by pasting a source drop, which has no
    `.git`, so this path exists so a DEVELOPER's title bar shows something true
    rather than the `0.0-dev` placeholder in VERSION.

    Reported verbatim, e.g. `0.58-3-g6c7a351-dirty` = three commits past 0.58
    with local edits.  Deliberately NOT reduced to something like "0.58+dev":
    that reads as a release number, and no such release exists.  Exactly on a
    tag, describe prints the bare tag and this returns it unchanged.

    ONE git call, not three.  `--dirty` plus describe's own `-<n>-g<sha>`
    suffix answers "has this moved past the tag" for free; the first version
    also ran `status --porcelain`, which walks the whole working tree (this
    repo carries a multi-GB gitignored `export/`).
    """
    return _git(["describe", "--tags", "--dirty", *_TAG_MATCH]) or None


# current_version() is called on every plugin selection and on each GUI
# refresh; the answer cannot change while the process runs, so resolve it once.
_CURRENT: list[str | None] = [None]


def current_version() -> str:
    """The release this source tree is, e.g. "0.583".

    `VERSION` wins because it is the only source that survives the way users
    actually install.  A dev checkout (VERSION absent or `0.0-dev`) falls back
    to the nearest git tag so the GUI shows a real number instead of "dev".
    """
    if _CURRENT[0] is not None:
        return _CURRENT[0]
    stamped = _read_version_file()
    if stamped and stamped != DEV_VERSION:
        resolved = stamped
    else:
        resolved = _git_version() or stamped or DEV_VERSION
    _CURRENT[0] = resolved
    return resolved


def is_dev_version(version: str | None = None) -> bool:
    """True when this tree is not exactly a release.

    A `git describe` string carries a `-<n>-g<sha>` and/or `-dirty` suffix
    whenever the checkout has moved past its tag, so anything beyond a bare
    `MAJOR.MINOR` is a development build.
    """
    v = current_version() if version is None else version
    return v == DEV_VERSION or v != _base_tag(v)


def _base_tag(version: str) -> str:
    """The release tag inside a version string.

    `0.58` -> `0.58`;  `0.58-3-g6c7a351-dirty` -> `0.58`.  Everything ranks and
    compares on the tag, so a developer three commits past 0.58 is treated as
    0.58 for "what does this upgrade owe" purposes.
    """
    return version.strip().split("-", 1)[0].split("+", 1)[0]


def version_key(tag: str) -> tuple[int, int] | None:
    """Sortable key for a release tag, in thousandths.

    Tags through 0.58 are MAJOR.MM (hundredths); 0.581 onward are MAJOR.MMM
    (thousandths).  Both must rank on ONE scale or 0.59 sorts above 0.580 --
    the same trap `tools/navmesh_cache.py` and the tag workflow each document.
    A 2-digit minor is worth ten of the new units: 0.58 IS 0.580.

    Accepts a raw `git describe` string so a dev checkout ranks as its tag.
    """
    base = _base_tag(tag)
    major, sep, minor = base.partition(".")
    if not sep or not major.isdigit() or not minor.isdigit():
        return None
    return (int(major), int(minor) * (10 if len(minor) == 2 else 1))


# ---------------------------------------------------------------------------
# Conversion state: which version last ran each step
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, STATE_FILE)
    except OSError:
        # Losing the record costs an over-broad re-run suggestion, never
        # correctness -- never let it take the conversion down with it.
        try:
            os.unlink(tmp)
        except OSError:
            pass


def record_step_run(step_key: str, plugin: str | None,
                    version: str | None = None) -> None:
    """Note that *step_key* completed for *plugin* at the current version.

    Called on a step's SUCCESS only.  A failed step must stay stale, or the
    next upgrade check would report clean and the user would ship half-built
    output.
    """
    version = current_version() if version is None else version
    state = _load_state()
    steps = state.setdefault("steps", {})
    entry  = steps.setdefault(_plugin_key(plugin), {})
    entry[step_key] = version
    _save_state(state)


def _plugin_key(plugin: str | None) -> str:
    # Case-insensitive: the GUI's combo and the CLI's -f differ in case for the
    # same file often enough that keying on the raw string splits one plugin's
    # history in two.
    return (plugin or "").strip().lower() or "*"


def steps_run_at(plugin: str | None) -> dict[str, str]:
    """{step_key: version} for every step recorded for *plugin*."""
    steps = _load_state().get("steps", {})
    merged: dict[str, str] = {}
    got = steps.get(_plugin_key(plugin))
    if isinstance(got, dict):
        merged.update({k: v for k, v in got.items() if isinstance(v, str)})
    return merged


def installed_version_for(plugin: str | None) -> str | None:
    """Oldest version among the steps recorded for *plugin*.

    OLDEST, not newest: the upgrade owed is whatever the most out-of-date step
    owes.  Taking the newest would silently forgive every step that has not run
    since.
    """
    versions = [v for v in steps_run_at(plugin).values() if version_key(v)]
    if not versions:
        return None
    return min(versions, key=lambda v: version_key(v) or (0, 0))


# ---------------------------------------------------------------------------
# Upgrade plan
# ---------------------------------------------------------------------------

_TABLE: list[dict[str, list[str]] | None] = [None]


def _load_steps_table() -> dict[str, list[str]]:
    """{version: [step labels]} written at tag time by the release workflow.

    Ships inside the source drop precisely so an offline install can answer
    "what changed between the version I ran and this one" with no network.

    Parsed once: the GUI asks for a plan on every plugin selection, and the
    file cannot change under a running process.
    """
    if _TABLE[0] is not None:
        return _TABLE[0]
    try:
        with open(STEPS_FILE, encoding="utf-8") as fh:
            table = json.load(fh)
    except (OSError, ValueError):
        _TABLE[0] = {}
        return {}
    if not isinstance(table, dict):
        _TABLE[0] = {}
        return {}
    out: dict[str, list[str]] = {}
    for version, steps in table.get("versions", table).items():
        if isinstance(steps, list):
            out[version] = [s for s in steps if isinstance(s, str)]
    _TABLE[0] = out
    return out


# The GUI's step keys, in run order, paired with the labels release_notes.py
# emits.  Kept here rather than imported from gui.py so convert.py and the CLI
# can use the same mapping without pulling in tkinter.
STEP_KEYS: list[tuple[str, str]] = [
    ("export",             "1. Export"),
    ("extract",            "2. Extract"),
    ("meshes",             "3. Meshes"),
    ("speedtrees",         "4. SpeedTrees"),
    ("creatures",          "5. Creatures"),
    ("import_",            "6. Import"),
    ("sounds",             "7. Sounds"),
    ("scripts",            "8. Scripts"),
    ("lod",                "9. LOD"),
    ("modify_body_meshes", "10. Patch Skyrim"),
    ("pack",               "11. Pack BSAs"),
    ("pack_zip",           "12. Pack Mod Zip"),
]

_LABEL_TO_KEY = {label: key for key, label in STEP_KEYS}


def label_to_key(label: str) -> str | None:
    return _LABEL_TO_KEY.get(label)


def steps_between(from_version: str, to_version: str) -> list[str] | None:
    """Step labels owed by upgrading from_version -> to_version.

    Unions every table entry in (from, to] -- an upgrade that skips four
    releases owes the union of all four, not just the newest one's steps.

    Returns None when the table cannot answer honestly: a missing table, or a
    gap where some intervening version has no entry.  None means "unknown",
    which the caller must render as "re-run everything" rather than "nothing" --
    a silent empty list would tell the user their stale output is current.
    """
    lo, hi = version_key(from_version), version_key(to_version)
    if not lo or not hi or lo >= hi:
        return []

    table = _load_steps_table()
    if not table:
        return None

    covered = {v: k for v in table if (k := version_key(v))}
    # Every release in the range must be present, or the union has a hole.
    in_range = {v: k for v, k in covered.items() if lo < k <= hi}
    if not in_range or max(in_range.values()) != hi:
        return None

    owed: set[str] = set()
    for version in in_range:
        owed.update(table[version])

    order = [label for _key, label in STEP_KEYS]
    return [label for label in order if label in owed]


def upgrade_plan(plugin: str | None) -> dict:
    """What the user must re-run for *plugin* after pasting in this version.

    Returns a dict the GUI and CLI both render:
      current      -- version of this source tree
      installed    -- oldest version any recorded step ran at (None if never)
      upgraded     -- True when installed < current
      steps        -- step KEYS to re-tick, run-ordered
      unknown      -- True when the range could not be resolved and `steps`
                      is a conservative everything-list rather than a
                      measured one
      never_run    -- True when nothing has ever been converted for `plugin`
    """
    current   = current_version()
    installed = installed_version_for(plugin)
    all_keys  = [key for key, _ in STEP_KEYS]

    if installed is None:
        # Nothing recorded: a first run, so everything is owed, but this is a
        # fresh install rather than an upgrade -- the GUI says so differently.
        return {"current": current, "installed": None, "upgraded": False,
                "steps": [], "unknown": False, "never_run": True}

    labels = steps_between(installed, current)
    if labels is None:
        return {"current": current, "installed": installed, "upgraded": True,
                "steps": all_keys, "unknown": True, "never_run": False}

    keys = [k for k in (label_to_key(l) for l in labels) if k]

    # A step that never ran at all is owed regardless of what changed since.
    ran = steps_run_at(plugin)
    for key in all_keys:
        if key not in ran and key not in keys:
            keys.append(key)
    keys = [k for k in all_keys if k in keys]

    return {"current": current,
            "installed": installed,
            "upgraded": version_key(installed) != version_key(current),
            "steps": keys,
            "unknown": False,
            "never_run": False}


def describe_plan(plan: dict) -> str:
    """One-line human summary of `upgrade_plan`, for the CLI and the GUI log.

    Plain ASCII: this is printed to a Windows console, which is cp1252 by
    default and raises UnicodeEncodeError on an em dash or an arrow.  The GUI's
    own banner is a Tk widget and can use nicer punctuation.
    """
    cur = plan["current"]
    if plan["never_run"]:
        return f"Version {cur} - no previous conversion recorded."
    if not plan["upgraded"] and not plan["steps"]:
        return f"Version {cur} - up to date, nothing needs re-running."

    label_of = dict(STEP_KEYS)
    names = ", ".join(label_of.get(k, k) for k in plan["steps"]) or "nothing"
    if plan["unknown"]:
        return (f"Version {cur} (was {plan['installed']}) - cannot tell which "
                f"steps changed, so all are selected.")
    if not plan["upgraded"]:
        return f"Version {cur} - steps never run: {names}."
    return (f"Version {cur} (was {plan['installed']}) - re-run: {names}")


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Report the installed version and "
                                             "which pipeline steps an upgrade "
                                             "requires re-running.")
    ap.add_argument("-f", "--file", default=None,
                    help="Plugin to report on (default: any)")
    ap.add_argument("--json", action="store_true", help="Machine-readable output")
    args = ap.parse_args()

    plan = upgrade_plan(args.file)
    if args.json:
        json.dump(plan, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(describe_plan(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
