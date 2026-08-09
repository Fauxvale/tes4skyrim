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


def test_git_describe_output_ranks_as_its_base_tag():
    """A dev checkout reports git's raw `describe`, e.g. 0.58-3-g6c7a351-dirty.

    It must rank as 0.58, or a developer's upgrade plan resolves against a
    version that does not exist and reports 'unknown' -> re-run everything.
    """
    for described in ("0.58-3-g6c7a351-dirty", "0.58-3-g6c7a351", "0.58-dirty"):
        assert v.version_key(described) == v.version_key("0.58"), described


def test_exact_tag_is_not_a_dev_version():
    assert v.is_dev_version("0.58") is False
    assert v.is_dev_version("0.581") is False


@pytest.mark.parametrize("described", [
    "0.58-3-g6c7a351-dirty", "0.58-dirty", "0.0-dev",
])
def test_moved_past_a_tag_is_a_dev_version(described):
    assert v.is_dev_version(described) is True


@pytest.mark.parametrize("bad", ["", "dev", "x.yy", "0", "0.", ".58"])
def test_unparseable_tags_are_rejected(bad):
    assert v.version_key(bad) is None


# ── steps_between: union, and the honest "unknown" ────────────────────────

def _table(monkeypatch, versions):
    monkeypatch.setattr(v, "_load_steps_table", lambda: versions)


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
