#!/usr/bin/env python
"""Build UPGRADE_STEPS.json -- the per-release table of GUI steps a version
requires re-running.

The GUI reads this to pre-tick only the steps that actually changed between the
version a user last converted with and the one they just pasted in.  It ships
INSIDE the source drop because that is the only way the answer is available
offline: end users have no `.git` and often no network, so neither `git log`
nor the GitHub API can be on the path that decides what to re-run.

    python tools/upgrade_table.py                       # rebuild from all tags
    python tools/upgrade_table.py --add 0.584           # append one release
    python tools/upgrade_table.py --check               # verify, exit 1 if stale

Each entry maps a release tag to the steps its OWN commits require -- the delta
from the previous tag, not a cumulative set.  `version.steps_between` unions
the entries in range, so an upgrade that skips releases owes all of them.

The path -> step mapping is release_notes.py's, not a second copy: that module
is what the tag workflow already uses to write the same answer into the release
notes, and two mappings would drift the moment one is edited.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(REPO_ROOT))

import release_notes as rn  # noqa: E402
from version import STEPS_FILE, version_key  # noqa: E402

# Table format version.  Bump only on a breaking shape change; `version.py`
# tolerates both the wrapped {"versions": {...}} form and a bare mapping.
FORMAT = 1


def release_tags() -> list[str]:
    """Every release tag, oldest first, on the workflow's numbering scheme."""
    out = subprocess.run(
        ["git", "tag", "-l", "[0-9]*.[0-9][0-9]", "[0-9]*.[0-9][0-9][0-9]"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    tags = [t.strip() for t in out.splitlines() if t.strip()]
    keyed = [(k, t) for t in tags if (k := version_key(t))]
    return [t for _k, t in sorted(keyed)]


def steps_for_release(rev_from: str | None, rev_to: str) -> list[str]:
    """Step labels owed by rev_from..rev_to, via release_notes' mapping."""
    paths = rn.changed_files(rev_from, rev_to)
    steps, _unmatched, _gui_only = rn.steps_for_paths(
        paths, rn.convert_py_steps(rev_from, rev_to))
    return steps


def build_table(tags: list[str]) -> dict:
    versions: dict[str, list[str]] = {}
    previous: str | None = None
    for tag in tags:
        try:
            versions[tag] = steps_for_release(previous, tag)
        except subprocess.CalledProcessError:
            # A tag that no longer resolves (deleted, or a shallow clone).
            # Skipping it would leave a hole `steps_between` reads as
            # "unknown" -> select everything, which is the safe direction.
            print(f"warning: cannot diff {previous or '<root>'}..{tag}, skipping",
                  file=sys.stderr)
        previous = tag
    return {"format": FORMAT, "versions": versions}


def load_existing() -> dict:
    try:
        with open(STEPS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_table(table: dict) -> None:
    with open(STEPS_FILE, "w", encoding="utf-8") as fh:
        json.dump(table, fh, indent=2, sort_keys=True)
        fh.write("\n")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--add", metavar="TAG", default=None,
                    help="Append one release (diffed against the tag below it) "
                         "instead of rebuilding the whole table")
    ap.add_argument("--to", default="HEAD",
                    help="With --add: the revision the tag names (default HEAD)")
    ap.add_argument("--check", action="store_true",
                    help="Exit 1 if the committed table does not match the tags")
    args = ap.parse_args()

    if args.add:
        table = load_existing()
        versions = table.get("versions", {}) if isinstance(table, dict) else {}
        tags = release_tags()
        # The tag being added is usually not pushed yet, so find the newest
        # EXISTING tag below it rather than assuming it is in the list.
        key = version_key(args.add)
        if key is None:
            print(f"ERROR: {args.add} is not a release tag name.", file=sys.stderr)
            return 1
        below = [t for t in tags if (k := version_key(t)) and k < key]
        previous = below[-1] if below else None
        versions[args.add] = steps_for_release(previous, args.to)
        write_table({"format": FORMAT, "versions": versions})
        print(f"{args.add}: {', '.join(versions[args.add]) or '(no steps)'}")
        return 0

    fresh = build_table(release_tags())

    if args.check:
        existing = load_existing()
        if existing.get("versions") != fresh["versions"]:
            missing = set(fresh["versions"]) - set(existing.get("versions", {}))
            print("UPGRADE_STEPS.json is stale. Run: python tools/upgrade_table.py",
                  file=sys.stderr)
            if missing:
                print(f"  missing releases: {', '.join(sorted(missing))}",
                      file=sys.stderr)
            return 1
        print(f"UPGRADE_STEPS.json is current ({len(fresh['versions'])} releases).")
        return 0

    write_table(fresh)
    print(f"Wrote {STEPS_FILE.name}: {len(fresh['versions'])} releases.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
