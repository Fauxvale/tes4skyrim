#!/usr/bin/env python
"""Build release-tag notes: commits since the previous tag plus the GUI
pipeline steps those commits require the user to re-run.

Used by .github/workflows/tag-on-push.yml to annotate each auto-tag, but it
runs standalone against any two revisions:

    python tools/release_notes.py                     # last tag -> HEAD
    python tools/release_notes.py --from 1.07 --to HEAD
    python tools/release_notes.py --tag 1.08          # title the notes

The step mapping mirrors gui.py's STEPS table (the numbered checkboxes) and
the phase_* functions in convert.py that each one invokes.  Anything that
changes the plugin body (tes5_import) implies Import; mesh/creature/sound/LOD
work implies its own asset step; and because Pack BSAs / Pack Mod Zip consume
whatever the earlier steps wrote, they are appended whenever any step that
*produces* packaged output is triggered.

Keeping the answer honest means being narrow where the code is narrow:

  * convert.py is attributed per phase_* function via git's hunk headers, so a
    diff confined to phase_lod costs only the LOD step (see PHASE_STEPS).  Only
    module-scope/main()/shared-helper hunks fall back to every step.
  * Paths that are never pipeline input (docs, tests, tools, vendored binaries
    under external/, the standalone TESGameSelect/ plugin) map to no steps.
    Genuinely unrecognised paths select nothing -- they are listed verbatim so
    the reader can judge them (and add a rule), rather than blanket-ticking
    every step over one stray file.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent

# Ordered exactly as the GUI lists them, so output reads as a run order.
STEP_ORDER = [
    "1. Export",
    "2. Extract",
    "3. Meshes",
    "4. SpeedTrees",
    "5. Creatures",
    "6. Import",
    "7. Sounds",
    "8. Scripts",
    "9. LOD",
    "10. Patch Skyrim",
    "11. Pack BSAs",
    "12. Pack Mod Zip",
]

# Steps that only repackage what earlier steps produced.  Added automatically
# whenever any producing step fires, never a reason to run on their own.
PACKAGING_STEPS = ["11. Pack BSAs", "12. Pack Mod Zip"]

# (regex over the repo-relative path, steps it forces).  First match wins per
# rule list order, but every matching rule contributes -- a path may need
# several steps.  Patterns are matched with re.search against forward-slash
# paths.
RULES: list[tuple[str, list[str]]] = [
    # ── Stage packages ────────────────────────────────────────────────────
    (r"^tes4_export/",            ["1. Export", "6. Import"]),
    (r"^tes5_import/",            ["6. Import"]),
    (r"^script_convert/",         ["8. Scripts"]),

    # ── asset_convert: split by which phase_* function pulls the module ───
    (r"^asset_convert/bsa_extract\.py",        ["2. Extract"]),
    (r"^asset_convert/(spt_\w+|flipbook)\.py", ["4. SpeedTrees"]),
    (r"^asset_convert/(creature_pipeline|hkx_\w+|animation_data|"
     r"extract_skeleton_bones|kf_decode|kf_writer)\.py",
                                               ["5. Creatures"]),
    # Pre-built behavior/skeleton assets shipped with the converter.
    (r"^asset_convert/generated/",             ["5. Creatures"]),
    (r"^asset_convert/(audio_converter)\.py",  ["7. Sounds"]),
    (r"^asset_convert/(lod_gen|lod_far_gen|terrain_lod|terrain_lod_textures|"
     r"landscape_normals)\.py",                ["9. LOD"]),
    (r"^asset_convert/modify_body_meshes\.py", ["10. Patch Skyrim"]),
    # skin_replacement is also imported by nif_converter, so it is a mesh
    # change as well as a body-patch one.
    (r"^asset_convert/skin_replacement\.py",   ["3. Meshes", "10. Patch Skyrim"]),
    (r"^asset_convert/(bsa_pack)\.py",         ["11. Pack BSAs"]),
    (r"^asset_convert/texture_prune\.py",      ["3. Meshes"]),
    # Vanilla-asset provider: mesh conversion, creature skeletons
    # (extract_skeleton_bones) and the slot-44 body patch all pull from it.
    (r"^asset_convert/skyrim_assets\.py",
                                               ["3. Meshes", "5. Creatures",
                                                "10. Patch Skyrim"]),
    # Everything else under asset_convert is mesh conversion (nif_converter,
    # collision, cms, mopp, skin_retarget, body_wrap, book_inam, bow_rig,
    # furniture_markers, inv_marker, sse_nif, pyffi_monkey_patch, ...).
    (r"^asset_convert/",                       ["3. Meshes"]),

    # ── Native / shared code: conservatively wide ─────────────────────────
    # Docs and build notes shipped alongside the extension are not inputs to
    # anything.  Listed BEFORE the blanket native/ rule (first match wins), or
    # a README edit costs the user a mesh, creature AND LOD rebuild -- which is
    # exactly what 0.57 charged for `native/dist/README.md`.
    (r"^native/.*\.(md|txt)$",    []),
    (r"^native/",                 ["3. Meshes", "5. Creatures", "9. LOD"]),
    # convert.py is resolved per-phase-function instead (see PHASE_STEPS);
    # "ALL" here is only the fallback when the hunks can't be attributed.
    (r"^convert\.py$",            ["CONVERT"]),
    (r"^gui\.py$|^gui\.pyw$",     ["GUI"]),
    (r"^collision_options\.py$",  ["3. Meshes"]),
    # Process-pool plumbing: every worker-based stage runs through these.
    (r"^worker_budget\.py$|^subprocess_flags\.py$|^process_job\.py$", ["ALL"]),

    # ── Non-pipeline: never a reason to re-run anything ───────────────────
    (r"^docs/",                   []),
    (r"^tests/",                  []),
    (r"^tools/",                  []),
    (r"^references/",             []),
    (r"^external/",               []),
    (r"^\.github/",               []),
    (r"^\.claude/|^\.vscode/",    []),
    # Standalone starter plugin, built by tools/make_game_select_esp.py and
    # shipped as-is -- none of the 12 pipeline steps read or write it.
    (r"^TESGameSelect/",          []),
    # Dependency preflight: gates the run before any phase starts and produces
    # no conversion output of its own, so a change here re-runs nothing.
    (r"^preflight\.py$",          []),
    # Version identity and the upgrade shortcut itself.  It reports what is
    # stale; it never converts anything, so it cannot make output stale.  VERSION
    # is an export-subst template expanded at archive time -- its content is the
    # release number, which likewise changes no output.
    (r"^version\.py$|^VERSION$",  []),
    (r"^CLAUDE\.md$|^README\.md$|^TODO\.txt$|^CK_WARNINGS", []),
    (r"^conversion_config\.json$|^pyproject\.toml$|^\.git\w+$", []),
    (r"^[^/]+\.code-workspace$", []),
]

# convert.py hosts one phase_* function per GUI step.  A change inside exactly
# one of them implies only that step -- historically the blanket "ALL" here was
# the single biggest source of "re-run everything" noise (e.g. 0.40, whose
# convert.py diff was entirely inside phase_lod).
# convert.py functions that ORCHESTRATE phases without producing output.
# A hunk in one of these narrows to nothing rather than falling back to every
# step: main() parses arguments and dispatches, so editing it changes which
# phases the user can ask for, never what a phase writes.  `_mark` just records
# that a step completed (version.record_step_run) and is likewise output-inert.
ORCHESTRATION_FUNCS = frozenset({"main", "_mark"})

PHASE_STEPS: dict[str, list[str]] = {
    "phase_export":             ["1. Export"],
    "phase_extract":            ["2. Extract"],
    "phase_assets":             ["3. Meshes"],
    "phase_prune_textures":     ["3. Meshes"],
    "phase_speedtrees":         ["4. SpeedTrees"],
    "phase_creatures":          ["5. Creatures"],
    "phase_import":             ["6. Import"],
    "phase_sounds":             ["7. Sounds"],
    "phase_scripts":            ["8. Scripts"],
    "phase_compile":            ["8. Scripts"],
    "phase_lod":                ["9. LOD"],
    "phase_modify_body_meshes": ["10. Patch Skyrim"],
    "phase_pack":               ["11. Pack BSAs"],
    "phase_pack_zip":           ["12. Pack Mod Zip"],
}


def _run(args: list[str]) -> str:
    return subprocess.run(
        ["git", *args], cwd=SCRIPT_DIR, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def previous_tag(before: str = "HEAD") -> str | None:
    """Latest release tag reachable from `before`, matching the workflow's own
    tag scheme.  None when the repo has no release tag yet.

    Tags through 0.58 are MAJOR.MM (hundredths); 0.581 onward are MAJOR.MMM
    (thousandths).  Both forms must be globbed AND ranked on a common scale:
    comparing the minor fields as bare ints would sort 0.59 above 0.580, and
    globbing only two digits would pin the notes to 0.58 forever.
    """
    try:
        tags = _run(["tag", "-l", "[0-9]*.[0-9][0-9]",
                     "[0-9]*.[0-9][0-9][0-9]"]).splitlines()
    except subprocess.CalledProcessError:
        return None
    tags = [t.strip() for t in tags if t.strip()]
    if not tags:
        return None

    def key(t: str) -> tuple[int, int]:
        major, _, minor = t.partition(".")
        try:
            # Scale by width so both schemes compare in thousandths.
            scale = 10 if len(minor) == 2 else 1
            return (int(major), int(minor) * scale)
        except ValueError:
            return (-1, -1)

    return sorted(tags, key=key)[-1]


def commits_between(rev_from: str | None, rev_to: str) -> list[tuple[str, str]]:
    """[(short_sha, subject)] oldest-first for rev_from..rev_to."""
    rng = f"{rev_from}..{rev_to}" if rev_from else rev_to
    out = _run(["log", "--reverse", "--no-merges", "--format=%h%x1f%s", rng])
    rows = []
    for line in out.splitlines():
        if "\x1f" in line:
            sha, _, subject = line.partition("\x1f")
            rows.append((sha, subject))
    return rows


def changed_files(rev_from: str | None, rev_to: str) -> list[str]:
    if rev_from:
        out = _run(["diff", "--name-only", f"{rev_from}..{rev_to}"])
    else:
        out = _run(["ls-tree", "-r", "--name-only", rev_to])
    return [p for p in out.splitlines() if p.strip()]


_HUNK_FUNC = re.compile(r"^@@ .*? @@\s*(?:def\s+)?([A-Za-z_]\w*)")


def convert_py_steps(rev_from: str | None, rev_to: str) -> list[str] | None:
    """Steps implied by a convert.py change, resolved per phase_* function.

    Git's hunk headers name the enclosing function, so a diff confined to
    phase_lod costs only the LOD step.  Returns None when the change can't be
    attributed -- shared helpers, main(), module scope, or a brand-new file --
    in which case the caller falls back to every step.
    """
    if not rev_from:
        return None
    try:
        diff = _run(["diff", "-U0", f"{rev_from}..{rev_to}", "--", "convert.py"])
    except subprocess.CalledProcessError:
        return None

    steps: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("@@"):
            continue
        m = _HUNK_FUNC.match(line)
        if not m:
            # Hunk outside any function (imports, constants) -- affects
            # everything, so don't narrow.
            return None
        func = m.group(1)
        if func in ORCHESTRATION_FUNCS:
            # main() is argument parsing and phase dispatch.  It decides WHICH
            # phases run, never what any of them produces, so a change confined
            # to it costs no re-conversion.
            #
            # It used to fall through to "cannot attribute" -> every step, which
            # made any CLI-plumbing commit (a new flag, the per-step state
            # recording) demand a full multi-hour reconversion.  Measured on
            # 0.58..0.581: all 16 hunks were in main(), and the release asked
            # for all twelve steps when only Meshes and Scripts had changed.
            continue
        mapped = PHASE_STEPS.get(func)
        if mapped is None:
            # A shared helper or an unknown function -- genuinely unattributable.
            return None
        steps.update(mapped)

    # An EMPTY set is a real answer here, not a failure: it means every hunk
    # was orchestration, which costs nothing to re-run.  Returning None for it
    # would resurrect the all-twelve-steps bug this function exists to avoid.
    return sorted(steps)


def steps_for_paths(paths: list[str],
                    convert_steps: list[str] | None = None,
                    ) -> tuple[list[str], list[str], bool]:
    """→ (ordered steps to re-run, paths no rule matched, gui_only_change).

    `convert_steps` is the per-phase attribution of a convert.py change from
    `convert_py_steps`; None means "couldn't narrow it", i.e. every step.

    `gui_only_change` is True when the GUI itself changed but nothing that
    alters conversion output did -- the user needs a fresh GUI, not a re-run.
    """
    steps: set[str] = set()
    unmatched: list[str] = []
    gui_touched = False
    run_all = False

    for path in paths:
        p = path.replace("\\", "/")
        matched = False
        for pattern, mapped in RULES:
            if re.search(pattern, p):
                matched = True
                if "ALL" in mapped:
                    run_all = True
                elif "GUI" in mapped:
                    gui_touched = True
                elif "CONVERT" in mapped:
                    if convert_steps is None:
                        run_all = True
                    else:
                        steps.update(convert_steps)
                else:
                    steps.update(mapped)
                break
        if not matched:
            unmatched.append(p)

    if run_all:
        steps.update(STEP_ORDER)

    # Unrecognised paths select no steps.  They are reported separately so the
    # reader decides what (if anything) they imply -- ticking all twelve boxes
    # over one unmapped file made the checklist useless.

    # Packaging only matters once something it packages was rebuilt.  "Patch
    # Skyrim" writes a standalone ARMA patch that the BSA/zip steps never read,
    # so it must not drag packaging in on its own.
    if steps - {"10. Patch Skyrim"}:
        steps.update(PACKAGING_STEPS)

    ordered = [s for s in STEP_ORDER if s in steps]
    return ordered, unmatched, (gui_touched and not steps)


def build_notes(tag: str | None, rev_from: str | None, rev_to: str) -> str:
    commits = commits_between(rev_from, rev_to)
    paths = changed_files(rev_from, rev_to)
    steps, unmatched, gui_only = steps_for_paths(
        paths, convert_py_steps(rev_from, rev_to))

    lines: list[str] = []
    lines.append(f"Release {tag}" if tag else "Release notes")
    lines.append("")

    if rev_from:
        lines.append(f"Changes since {rev_from} ({len(commits)} commit"
                     f"{'' if len(commits) == 1 else 's'}):")
    else:
        lines.append(f"Initial release ({len(commits)} commits):")
    lines.append("")
    for sha, subject in commits:
        lines.append(f"  {sha}  {subject}")
    if not commits:
        lines.append("  (no commits)")
    lines.append("")

    lines.append("Steps to re-run in the GUI:")
    lines.append("")
    if steps:
        for step in steps:
            lines.append(f"  [x] {step}")
    elif gui_only:
        lines.append("  (none -- GUI-only change; relaunch the GUI, no re-run needed)")
    elif unmatched:
        lines.append("  (none matched -- see the unmapped paths below)")
    else:
        lines.append("  (none -- no conversion code changed)")

    if unmatched:
        uniq = sorted(set(unmatched))
        lines.append("")
        lines.append("Unmapped paths (no step selected -- judge for yourself, and "
                     "add a rule in tools/release_notes.py):")
        for p in uniq[:20]:
            lines.append(f"  {p}")
        if len(uniq) > 20:
            lines.append(f"  ... and {len(uniq) - 20} more")

    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="rev_from", default=None,
                    help="Start revision (default: latest MAJOR.MM tag)")
    ap.add_argument("--to", dest="rev_to", default="HEAD",
                    help="End revision (default: HEAD)")
    ap.add_argument("--tag", default=None,
                    help="Tag name to title the notes with")
    ap.add_argument("--output", default=None,
                    help="Write notes to this file instead of stdout")
    args = ap.parse_args()

    rev_from = args.rev_from if args.rev_from is not None else previous_tag()
    notes = build_notes(args.tag, rev_from, args.rev_to)

    if args.output:
        Path(args.output).write_text(notes, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
