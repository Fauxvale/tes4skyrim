"""Tests for version.py -- the installed-version stamp and the upgrade-aware
step selection the GUI uses after a user pastes a new build over the old one.

The failure this guards against is silent and expensive in both directions:
selecting too few steps ships stale output the user believes is current, and
selecting too many costs a multi-hour full reconversion for a 3-step change.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import version as v  # noqa: E402


# ── Tag numbering: two schemes on one scale ───────────────────────────────
# Tags through 0.58 are MAJOR.MM (hundredths); 0.581 onward are MAJOR.MMM.
# Ranking the minor field as a bare int sorts 0.59 below 0.581 and an upgrade
# would silently select nothing.

def test_two_digit_and_three_digit_minors_share_a_scale():
    assert v.version_key("0.58") == v.version_key("0.580")


def test_ordering_across_the_scheme_boundary():
    assert v.version_key("0.581") > v.version_key("0.58")
    assert v.version_key("0.59") > v.version_key("0.581")
    assert v.version_key("1.00") > v.version_key("0.999")


def test_dev_version_string_ranks_as_its_base_tag():
    """A dev checkout reports `<tag>+g<sha>`, e.g. 0.58+ge49ab44.

    It must rank as 0.58, or a developer's upgrade plan resolves against a
    version that does not exist and reports 'unknown' -> re-run everything.
    """
    for described in ("0.58+ge49ab44", "0.58+", "0.58-3-g6c7a351-dirty"):
        assert v.version_key(described) == v.version_key("0.58"), described


def test_exact_tag_is_not_a_dev_version():
    assert v.is_dev_version("0.58") is False
    assert v.is_dev_version("0.581") is False


@pytest.mark.parametrize("described", [
    "0.58+ge49ab44", "0.58+", "0.0-dev",
])
def test_moved_past_a_tag_is_a_dev_version(described):
    assert v.is_dev_version(described) is True


def test_resolving_the_version_never_spawns_a_subprocess():
    """The GUI resolves the version while building its window, and under
    gui.pyw the parent is console-less pythonw.exe -- every spawn there is a
    potential console window.  The GUI is ONE window."""
    import subprocess
    calls = []
    originals = {n: getattr(subprocess, n)
                 for n in ("run", "Popen", "call", "check_output")}
    for name in originals:
        setattr(subprocess, name,
                lambda *a, _n=name, **k: calls.append(_n))
    try:
        v._CURRENT[0] = None
        v.current_version()
    finally:
        for name, fn in originals.items():
            setattr(subprocess, name, fn)
        v._CURRENT[0] = None
    assert calls == [], f"version resolution spawned: {calls}"


# ── Update check ──────────────────────────────────────────────────────────

def test_update_check_ignores_non_release_tags(monkeypatch):
    """The repo also carries navmesh-cache-* tags; offering one as an update
    would send the user to a release that is not the converter."""
    monkeypatch.setattr(v, "latest_release", lambda timeout=8: None)
    monkeypatch.setattr(v, "current_version", lambda: "0.58")
    got = v.check_for_update()
    assert got["reachable"] is False and got["available"] is False


def test_update_available_only_when_strictly_newer(monkeypatch):
    monkeypatch.setattr(v, "current_version", lambda: "0.58")
    for latest, expect in (("0.59", True), ("0.58", False), ("0.57", False)):
        monkeypatch.setattr(v, "latest_release", lambda timeout=8, l=latest: l)
        assert v.check_for_update()["available"] is expect, latest


def test_dev_tree_on_the_newest_tag_is_not_behind(monkeypatch):
    """`0.58+ge49ab44` is ahead of 0.58, never behind it."""
    monkeypatch.setattr(v, "current_version", lambda: "0.58+ge49ab44")
    monkeypatch.setattr(v, "latest_release", lambda timeout=8: "0.58")
    assert v.check_for_update()["available"] is False


@pytest.mark.parametrize("bad", ["", "dev", "x.yy", "0", "0.", ".58"])
def test_unparseable_tags_are_rejected(bad):
    assert v.version_key(bad) is None


# ── steps_between: union, and the honest "unknown" ────────────────────────

def _table(monkeypatch, versions, reachable=True):
    """Stand in for the fetched table, bypassing the network entirely."""
    monkeypatch.setattr(v, "steps_table",
                        lambda timeout=8, refresh=False: (versions, reachable))


def test_skipping_releases_unions_every_entry_in_range(monkeypatch):
    """Upgrading 0.50 -> 0.53 owes all three releases' steps, not just 0.53's."""
    _table(monkeypatch, {
        "0.51": ["3. Meshes"],
        "0.52": ["6. Import"],
        "0.53": ["8. Scripts"],
    })
    got = v.steps_between("0.50", "0.53")
    assert got == ["3. Meshes", "6. Import", "8. Scripts"]


def test_result_is_in_run_order_not_table_order(monkeypatch):
    _table(monkeypatch, {"0.51": ["12. Pack Mod Zip", "1. Export", "6. Import"]})
    assert v.steps_between("0.50", "0.51") == [
        "1. Export", "6. Import", "12. Pack Mod Zip"]


def test_releases_outside_the_range_are_excluded(monkeypatch):
    _table(monkeypatch, {
        "0.50": ["1. Export"],      # at/below `from` -- already run
        "0.51": ["3. Meshes"],
        "0.52": ["6. Import"],      # above `to` -- not installed yet
    })
    assert v.steps_between("0.50", "0.51") == ["3. Meshes"]


def test_a_hole_in_the_range_reports_unknown_not_empty(monkeypatch):
    """A missing entry must NOT read as 'nothing changed'.

    None is the caller's signal to select everything.  Returning [] here would
    tell a user with stale output that they are up to date.
    """
    _table(monkeypatch, {"0.51": ["3. Meshes"]})   # 0.52 absent
    assert v.steps_between("0.50", "0.52") is None


def test_missing_table_reports_unknown(monkeypatch):
    _table(monkeypatch, {})
    assert v.steps_between("0.50", "0.52") is None


def test_no_upgrade_owes_nothing(monkeypatch):
    _table(monkeypatch, {"0.51": ["3. Meshes"]})
    assert v.steps_between("0.51", "0.51") == []
    # A downgrade owes nothing either -- there is no forward delta to apply.
    assert v.steps_between("0.52", "0.51") == []


# ── Step keys line up with the GUI and with release_notes ─────────────────

def test_step_keys_match_the_gui_step_table():
    """version.STEP_KEYS must mirror gui.STEPS, or auto-selection ticks nothing.

    gui.py is imported for its STEPS constant only; it must not need tkinter at
    import time for this to work.
    """
    import gui
    assert [k for k, *_ in gui.STEPS] == [k for k, _ in v.STEP_KEYS]
    assert [s[2] for s in gui.STEPS] == [lbl for _k, lbl in v.STEP_KEYS]


def test_labels_match_release_notes_step_order():
    """The labels version.py maps are exactly the ones release_notes emits."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
    import release_notes as rn
    assert [lbl for _k, lbl in v.STEP_KEYS] == rn.STEP_ORDER


# ── Conversion state ──────────────────────────────────────────────────────

@pytest.fixture
def state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(v, "STATE_FILE", path)
    return path


def test_records_and_reads_back_per_plugin(state):
    v.record_step_run("meshes", "Oblivion.esm", "0.55")
    v.record_step_run("import_", "Nehrim.esm", "0.57")
    assert v.steps_run_at("Oblivion.esm") == {"meshes": "0.55"}
    assert v.steps_run_at("Nehrim.esm") == {"import_": "0.57"}


def test_plugin_key_is_case_insensitive(state):
    """The GUI combo and the CLI's -f disagree on case for the same file; two
    spellings must not split one plugin's history in two."""
    v.record_step_run("meshes", "Oblivion.esm", "0.55")
    assert v.steps_run_at("OBLIVION.ESM") == {"meshes": "0.55"}


def test_installed_version_is_the_oldest_step_not_the_newest(state):
    """The upgrade owed is whatever the most out-of-date step owes."""
    v.record_step_run("meshes", "Oblivion.esm", "0.52")
    v.record_step_run("import_", "Oblivion.esm", "0.58")
    assert v.installed_version_for("Oblivion.esm") == "0.52"


def test_unknown_plugin_has_no_installed_version(state):
    assert v.installed_version_for("Never.esm") is None


# ── Source directory: plugins do not share one ────────────────────────────

def test_each_plugin_remembers_its_own_data_directory(state):
    """Nehrim and Morrowind_ob live in their own installs, so re-running a
    converted plugin must restore ITS directory, not whichever is configured."""
    v.record_step_run("import_", "Nehrim.esm", "0.55", data_path=r"D:\Nehrim\Data")
    v.record_step_run("import_", "Oblivion.esm", "0.55", data_path=r"D:\Obliv\Data")
    assert v.source_path_for("Nehrim.esm") == r"D:\Nehrim\Data"
    assert v.source_path_for("Oblivion.esm") == r"D:\Obliv\Data"


def test_source_path_is_case_insensitive(state):
    v.record_step_run("import_", "Nehrim.esm", "0.55", data_path=r"D:\Nehrim\Data")
    assert v.source_path_for("NEHRIM.ESM") == r"D:\Nehrim\Data"


def test_source_path_absent_when_never_recorded(state):
    v.record_step_run("import_", "Nehrim.esm", "0.55")   # no data_path
    assert v.source_path_for("Nehrim.esm") is None
    assert v.source_path_for("Unknown.esm") is None


def test_recording_a_source_does_not_disturb_step_versions(state):
    v.record_step_run("meshes", "Nehrim.esm", "0.55", data_path=r"D:\Nehrim\Data")
    assert v.steps_run_at("Nehrim.esm") == {"meshes": "0.55"}


def test_corrupt_state_file_is_survivable(state):
    state.write_text("{not json", encoding="utf-8")
    assert v.steps_run_at("Oblivion.esm") == {}
    assert v.installed_version_for("Oblivion.esm") is None


# ── upgrade_plan ──────────────────────────────────────────────────────────

def test_fresh_install_is_never_run_not_an_upgrade(state, monkeypatch):
    monkeypatch.setattr(v, "current_version", lambda: "0.58")
    plan = v.upgrade_plan("Oblivion.esm")
    assert plan["never_run"] is True
    assert plan["upgraded"] is False
    # Nothing pre-selected: the GUI's normal defaults are right for a first run.
    assert plan["steps"] == []


def test_upgrade_selects_only_the_changed_steps(state, monkeypatch):
    monkeypatch.setattr(v, "current_version", lambda: "0.58")
    _table(monkeypatch, {"0.58": ["6. Import", "11. Pack BSAs"]})
    for key, _ in v.STEP_KEYS:
        v.record_step_run(key, "Oblivion.esm", "0.57")

    plan = v.upgrade_plan("Oblivion.esm")
    assert plan["upgraded"] is True
    assert plan["unknown"] is False
    assert plan["steps"] == ["import_", "pack"]


def test_unresolvable_range_selects_everything(state, monkeypatch):
    """Unknown must fail toward re-running too much, never too little."""
    monkeypatch.setattr(v, "current_version", lambda: "0.58")
    _table(monkeypatch, {})
    for key, _ in v.STEP_KEYS:
        v.record_step_run(key, "Oblivion.esm", "0.55")

    plan = v.upgrade_plan("Oblivion.esm")
    assert plan["unknown"] is True
    assert plan["steps"] == [k for k, _ in v.STEP_KEYS]


def test_a_step_that_never_ran_is_owed_even_without_an_upgrade(state, monkeypatch):
    """Same version, but LOD was never run -- it is still outstanding."""
    monkeypatch.setattr(v, "current_version", lambda: "0.58")
    _table(monkeypatch, {})
    for key, _ in v.STEP_KEYS:
        if key != "lod":
            v.record_step_run(key, "Oblivion.esm", "0.58")

    plan = v.upgrade_plan("Oblivion.esm")
    assert plan["upgraded"] is False
    assert plan["steps"] == ["lod"]


def test_steps_are_returned_in_run_order(state, monkeypatch):
    monkeypatch.setattr(v, "current_version", lambda: "0.58")
    _table(monkeypatch, {"0.58": ["12. Pack Mod Zip", "1. Export", "6. Import"]})
    for key, _ in v.STEP_KEYS:
        v.record_step_run(key, "Oblivion.esm", "0.57")
    assert v.upgrade_plan("Oblivion.esm")["steps"] == [
        "export", "import_", "pack_zip"]


def test_describe_plan_is_console_safe(state, monkeypatch):
    """Printed to a cp1252 Windows console -- non-ASCII raises there."""
    monkeypatch.setattr(v, "current_version", lambda: "0.58")
    _table(monkeypatch, {"0.58": ["6. Import"]})
    for key, _ in v.STEP_KEYS:
        v.record_step_run(key, "Oblivion.esm", "0.57")

    for plugin in ("Oblivion.esm", "Never.esm"):
        text = v.describe_plan(v.upgrade_plan(plugin))
        text.encode("cp1252")  # raises UnicodeEncodeError on failure


# ── VERSION as an export-subst template ───────────────────────────────────
# VERSION holds a literal `$Format:%(describe:tags)$` on master and is marked
# `export-subst`, so `git archive` expands it while building the source zip a
# user downloads.  Nothing is ever committed, which is what lets a PR-merge
# release stamp a version at all -- the old scheme needed a commit on protected
# master and therefore only worked for a direct push.

def _version_file(tmp_path, monkeypatch, text):
    path = tmp_path / "VERSION"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(v, "VERSION_FILE", path)
    monkeypatch.setattr(v, "_CURRENT", [None])


def test_unexpanded_placeholder_is_not_a_version(tmp_path, monkeypatch):
    """A real checkout reads the raw template; it must never surface as-is.

    Returning it would put `$Format:...$` in the title bar and make every
    version comparison fail.
    """
    _version_file(tmp_path, monkeypatch, "$Format:%(describe:tags)$\n")
    assert v._read_version_file() is None


def test_expanded_tag_is_the_version(tmp_path, monkeypatch):
    """What the release zip for tag 0.581 actually contains."""
    _version_file(tmp_path, monkeypatch, "0.581\n")
    assert v._read_version_file() == "0.581"
    assert v.current_version() == "0.581"
    assert v.is_dev_version() is False


def test_archive_of_an_untagged_commit_is_a_dev_build(tmp_path, monkeypatch):
    """`%(describe:tags)` expands to `0.581-4-gaaef46e` off a tag.

    That archive is NOT release 0.581, so it must not claim to be one.  With no
    .git beside it the honest answer is a development build.
    """
    _version_file(tmp_path, monkeypatch, "0.581-4-gaaef46e\n")
    assert v._read_version_file() is None
    monkeypatch.setattr(v, "_git_version", lambda: None)
    monkeypatch.setattr(v, "_CURRENT", [None])
    assert v.current_version() == v.DEV_VERSION
    assert v.is_dev_version() is True


# ── The steps table over HTTPS ────────────────────────────────────────────

def _body(*steps, heading=True):
    """A release body shaped like the one release_notes.py writes."""
    lines = ["Release 0.581", "", "Changes since 0.58 (2 commits):", "",
             "  abc1234  did a thing", ""]
    if heading:
        lines += ["Steps to re-run in the GUI:", ""]
        lines += [f"  [x] {s}" for s in steps] or ["  (none -- no conversion "
                                                   "code changed)"]
    return "\n".join(lines) + "\n"


def _serve(monkeypatch, releases=None, fail=False, status=None):
    """Stand in for the `releases?per_page=100` fetch.

    `releases` is a list of (tag_name, body).  `fail` is a TRANSPORT failure
    (genuinely offline); `status` is an HTTP error response, which proves the
    server was reached.
    """
    import io
    import json as _json

    class _Resp:
        def __init__(self, body):
            self._body = body
        def __enter__(self):
            return io.BytesIO(self._body)
        def __exit__(self, *a):
            return False

    payload = [{"tag_name": t, "body": b} for t, b in (releases or [])]

    def _open(*a, **k):
        if fail:
            raise OSError("no network")
        if status:
            raise v.urllib.error.HTTPError(v._RELEASES_API, status, "err",
                                           {}, None)
        return _Resp(_json.dumps(payload).encode())

    monkeypatch.setattr(v.urllib.request, "urlopen", _open)
    monkeypatch.setattr(v, "_TABLE", [None])


def test_checklist_is_read_from_the_release_body(monkeypatch):
    """The release body IS the table -- no asset, nothing committed."""
    _serve(monkeypatch, [("0.581", _body("6. Import", "11. Pack BSAs"))])
    table, reachable = v.steps_table()
    assert reachable is True
    assert table == {"0.581": ["6. Import", "11. Pack BSAs"]}


def test_steps_come_back_in_run_order(monkeypatch):
    """A body listing steps out of order still yields GUI run order."""
    _serve(monkeypatch, [("0.581", _body("11. Pack BSAs", "1. Export"))])
    assert v.steps_table()[0]["0.581"] == ["1. Export", "11. Pack BSAs"]


def test_non_release_tags_are_ignored(monkeypatch):
    """navmesh-cache-* is a different series and must never rank as a version."""
    _serve(monkeypatch, [("navmesh-cache-0.57+", _body("6. Import")),
                         ("0.581", _body("3. Meshes"))])
    assert v.steps_table()[0] == {"0.581": ["3. Meshes"]}


def test_an_empty_checklist_is_an_entry_not_a_hole(monkeypatch):
    """A docs-only release genuinely owes nothing.

    Dropping it would leave a hole, and a hole selects all twelve steps
    forever -- so the heading, not the presence of ticks, decides.
    """
    _serve(monkeypatch, [("0.581", _body())])
    assert v.steps_table()[0] == {"0.581": []}


def test_a_body_with_no_checklist_is_absent(monkeypatch):
    """A release cut by hand has no checklist; it must read as a hole."""
    _serve(monkeypatch, [("0.581", _body("6. Import", heading=False))])
    assert v.steps_table()[0] == {}


def test_unreachable_table_is_reported_not_raised(monkeypatch):
    """A network failure on a plugin-selection handler must never propagate."""
    _serve(monkeypatch, fail=True)
    table, reachable = v.steps_table(timeout=1)
    assert table == {}
    assert reachable is False


def test_offline_selects_everything_never_nothing(state, monkeypatch):
    """No table means UNKNOWN, which owes every step.

    An empty list here would tell a user with stale output they are current --
    the exact silent failure the table exists to prevent.
    """
    _serve(monkeypatch, fail=True)
    monkeypatch.setattr(v, "current_version", lambda: "0.582")
    for key, _ in v.STEP_KEYS:
        v.record_step_run(key, "Oblivion.esm", "0.581")

    plan = v.upgrade_plan("Oblivion.esm")
    assert plan["unknown"] is True
    assert plan["offline"] is True
    assert plan["steps"] == [key for key, _ in v.STEP_KEYS]


def test_a_hole_in_a_reachable_table_is_not_offline(state, monkeypatch):
    """`offline` must distinguish 'could not ask' from 'asked, table has a gap'.

    Only the first is the user's to fix by reconnecting; the GUI disables the
    button for it and would be wrong to do so for the second.
    """
    _serve(monkeypatch, [("0.581", _body("6. Import"))])          # 0.582 absent
    monkeypatch.setattr(v, "current_version", lambda: "0.582")
    for key, _ in v.STEP_KEYS:
        v.record_step_run(key, "Oblivion.esm", "0.580")

    plan = v.upgrade_plan("Oblivion.esm")
    assert plan["unknown"] is True
    assert plan["offline"] is False


def test_a_failed_fetch_is_cached(monkeypatch):
    """An offline user must not pay the timeout on every plugin selection."""
    calls = []

    def _open(*a, **k):
        calls.append(1)
        raise OSError("no network")

    monkeypatch.setattr(v.urllib.request, "urlopen", _open)
    monkeypatch.setattr(v, "_TABLE", [None])
    for _ in range(5):
        v.steps_table(timeout=1)
    assert len(calls) == 1, f"re-fetched {len(calls)} times"


def test_a_404_is_not_offline(state, monkeypatch):
    """No data yet != the user has no internet.

    An HTTP response proves GitHub was reached, so it is a hole in our data,
    not a problem with the connection.  Reporting it as "offline" told users
    with working connections to go and fix their connection.
    """
    _serve(monkeypatch, status=404)
    table, reachable = v.steps_table()
    assert table == {}
    assert reachable is True, "a 404 means reached-but-empty, not offline"

    monkeypatch.setattr(v, "current_version", lambda: "0.582")
    for key, _ in v.STEP_KEYS:
        v.record_step_run(key, "Oblivion.esm", "0.581")
    plan = v.upgrade_plan("Oblivion.esm")
    # Still unknown -> still selects everything; only the EXPLANATION differs.
    assert plan["unknown"] is True
    assert plan["offline"] is False
    assert plan["steps"] == [key for key, _ in v.STEP_KEYS]


def test_only_a_transport_failure_is_offline(monkeypatch):
    _serve(monkeypatch, fail=True)
    assert v.steps_table(timeout=1)[1] is False


def test_never_converted_is_not_up_to_date(state, monkeypatch):
    """A plugin with no recorded run owes EVERYTHING, not nothing.

    `upgrade_plan` returns steps=[] here because the shortcut has nothing to
    narrow -- there is no previous version to diff against.  The GUI must not
    read that empty list as "up to date": it renders `never_run` as its own
    state ("Not converted"), because telling a user with no output at all that
    they are current is the exact inversion of the truth.
    """
    _table(monkeypatch, {"0.581": ["6. Import"]})
    monkeypatch.setattr(v, "current_version", lambda: "0.581")

    plan = v.upgrade_plan("NeverTouched.esm")
    assert plan["never_run"] is True
    assert plan["upgraded"] is False
    # The distinguishing flag: steps is empty for BOTH never_run and up-to-date,
    # so never_run is the only thing separating them.
    assert plan["steps"] == []
    assert "no previous conversion" in v.describe_plan(plan)


def test_parser_agrees_with_what_release_notes_actually_writes(monkeypatch):
    """The heading and checkbox shape are a CONTRACT between two modules.

    release_notes.py writes the tag message; version.py parses it back out of
    the release body.  Nothing else links them, so a reworded heading or a
    changed bullet would silently empty the table -- every upgrade would then
    read as "unknown" and select all twelve steps.  Build a real notes body and
    round-trip it.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
    import release_notes as rn

    steps = ["3. Meshes", "8. Scripts"]
    monkeypatch.setattr(rn, "commits_between", lambda a, b: [("abc1234", "x")])
    monkeypatch.setattr(rn, "changed_files", lambda a, b: [])
    monkeypatch.setattr(rn, "convert_py_steps", lambda a, b: None)
    monkeypatch.setattr(rn, "steps_for_paths", lambda *a, **k: (steps, [], False))

    notes = rn.build_notes("0.581", "0.58", "HEAD")
    assert v._CHECKLIST_HEADING in notes, "heading drifted from release_notes"
    assert v.steps_from_tag_message(notes) == steps
