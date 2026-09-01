"""Tests for the progress sentinels and the bars' monotonic arithmetic.

The one hard requirement is that no bar ever moves backwards inside a unit of
work, so most of these assert on a whole SEQUENCE of values rather than on one.
See: docs/commentary/gui_progress.md#monotonicity-is-the-whole-requirement
"""
import pytest

import progress
from progress import PhaseTracker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def on(monkeypatch):
    """Turn sentinels on and clear the throttle, as a fresh process would be."""
    monkeypatch.setenv(progress.PROGRESS_ENV_VAR, "1")
    progress._last_emit.clear()


@pytest.fixture
def off(monkeypatch):
    """The default state: no env var, so nothing may reach stdout."""
    monkeypatch.delenv(progress.PROGRESS_ENV_VAR, raising=False)
    progress._last_emit.clear()


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def test_silent_without_the_env_var(off, capsys):
    """A plain run emits nothing, so no output artifact can change."""
    progress.report("Export", 1, 10, "x", force=True)
    progress.plan("Import", Records=5)
    assert capsys.readouterr().out == ""


def test_enabled_only_on_exactly_one(monkeypatch):
    """Only "1" enables; a stale empty or "0" value does not."""
    for value, want in (("1", True), ("0", False), ("", False), (" 1 ", True)):
        monkeypatch.setenv(progress.PROGRESS_ENV_VAR, value)
        assert progress.enabled() is want


# ---------------------------------------------------------------------------
# Line format
# ---------------------------------------------------------------------------

def test_report_round_trips(on, capsys):
    """Every field survives the trip through the printed line."""
    progress.report("Meshes", 7, 20, "clutter/barrel01.nif", force=True)
    line = capsys.readouterr().out.strip()
    assert progress.parse(line) == ("Meshes", 7, 20, "clutter/barrel01.nif")


def test_report_without_an_item(on, capsys):
    """An absent item parses back as the empty string, not as None."""
    progress.report("Records", 3, 4, force=True)
    assert progress.parse(capsys.readouterr().out.strip())[3] == ""


def test_item_is_one_line_and_bounded(on, capsys):
    """Tabs and newlines cannot forge extra fields, and the tail is kept."""
    progress.report("Meshes", 1, 2, "a\tb\nc" + "x" * 200, force=True)
    label, done, total, item = progress.parse(capsys.readouterr().out.strip())
    assert (label, done, total) == ("Meshes", 1, 2)
    assert "\t" not in item and "\n" not in item
    assert item == "x" * 60


def test_plan_round_trips(on, capsys):
    """A plan carries every sub-part's count under its phase name."""
    progress.plan("Import", Records=10, Landscape=2, Navmesh=5)
    got = progress.parse_plan(capsys.readouterr().out.strip())
    assert got == ("Import", {"Records": 10, "Landscape": 2, "Navmesh": 5})


@pytest.mark.parametrize("line", [
    "", "hello", "@@PROG", "@@PROG Meshes", "@@PROG Meshes\t1",
    "@@PROG Meshes\tx\t2", "@@PROG \t1\t2", "@@PLAN Import\tRecords=10",
])
def test_parse_rejects_garbage(line):
    """Anything that is not a well-formed @@PROG is not a @@PROG."""
    assert progress.parse(line) is None


@pytest.mark.parametrize("line", [
    "", "hello", "@@PLAN", "@@PLAN Import", "@@PLAN Import\tRecords",
    "@@PLAN Import\tRecords=x", "@@PLAN \tRecords=1",
    "@@PROG Meshes\t1\t2\t",
])
def test_parse_plan_rejects_garbage(line):
    """Anything that is not a well-formed @@PLAN is not a @@PLAN."""
    assert progress.parse_plan(line) is None


def test_the_two_sentinels_do_not_parse_as_each_other(on, capsys):
    """A plan is never read as progress, nor progress as a plan."""
    progress.report("Records", 1, 2, force=True)
    progress.plan("Import", Records=2)
    prog_line, plan_line = capsys.readouterr().out.strip().split("\n")
    assert progress.parse_plan(prog_line) is None
    assert progress.parse(plan_line) is None


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------

def test_throttle_drops_the_flood_but_never_a_forced_line(on, capsys):
    """A 20k-item loop prints a few lines a second, not 20,000."""
    progress.report("Meshes", 1, 20000)
    for n in range(2, 20000):
        progress.report("Meshes", n, 20000)
    progress.report("Meshes", 20000, 20000, force=True)
    lines = capsys.readouterr().out.strip().split("\n")
    assert len(lines) == 2
    assert progress.parse(lines[-1])[1] == 20000


def test_throttle_is_per_label(on, capsys):
    """One label's flood must not silence another label's first report."""
    progress.report("Voices", 1, 10)
    progress.report("Music", 1, 10)
    assert len(capsys.readouterr().out.strip().split("\n")) == 2


# ---------------------------------------------------------------------------
# track
# ---------------------------------------------------------------------------

def test_track_counts_items_not_chunks(on, capsys):
    """A chunked job advances the bar by its own size, and lands on the total."""
    jobs = [("scpt", [1, 2, 3]), ("info", [4, 5])]
    assert list(progress.track("Scripts", ["a", "b"], jobs,
                               lambda j: len(j[1]))) == ["a", "b"]
    seen = [progress.parse(x)
            for x in capsys.readouterr().out.strip().split("\n")]
    assert [s[1] for s in seen] == sorted(s[1] for s in seen)
    assert seen[-1][1:3] == (5, 5)


def test_track_records_plans_before_it_reports(on, capsys):
    """The Import plan must be on stdout BEFORE the first Records line."""
    list(progress.track_records([("STAT", "STAT", {})], lands=4, pgrds=9))
    out = capsys.readouterr().out.strip().split("\n")
    assert progress.parse_plan(out[0]) == (
        "Import", {"Records": 1, "Landscape": 4, "Navmesh": 9})
    assert progress.parse(out[1])[0] == "Records"


# ---------------------------------------------------------------------------
# PhaseTracker
# ---------------------------------------------------------------------------

def _sweep(tracker, reports, plan=None):
    """Phase fractions after a banner, one plan and a list of (label, n, total)."""
    tracker.banner()
    if plan:
        tracker.set_plan(plan)
    out = [tracker.phase()]
    for label, done, total in reports:
        tracker.update(label, done, total)
        out.append(tracker.phase())
    return out


def test_a_planned_phase_is_one_sweep(on):
    """Import's three sub-phases combine into a single 0 -> 100% climb."""
    tracker = PhaseTracker()
    plan = {"Records": 100, "Landscape": 20, "Navmesh": 80}
    reports = ([("Records", n, 100) for n in range(0, 101, 25)]
               + [("Navmesh", n, 80) for n in range(0, 81, 20)]
               + [("Landscape", n, 20) for n in range(0, 21, 5)])
    seen = _sweep(tracker, reports, plan)
    assert seen == sorted(seen)
    assert seen[5] < 1.0
    assert seen[-1] == pytest.approx(1.0)


def test_records_alone_cannot_finish_the_phase(on):
    """The plan's denominator already holds the work that has not started."""
    tracker = PhaseTracker()
    tracker.banner()
    tracker.set_plan({"Records": 100, "Landscape": 20, "Navmesh": 80})
    tracker.update("Records", 100, 100)
    assert tracker.phase() == pytest.approx(0.5)


def test_a_single_label_phase_needs_no_plan(on):
    """The first report seeds the only component; the bar is done/total."""
    tracker = PhaseTracker()
    seen = _sweep(tracker, [("Meshes", n, 4) for n in range(5)])
    assert seen == [0.0, 0.0, 0.25, 0.5, 0.75, 1.0]


def test_a_refined_total_never_drops_the_bar(on):
    """A @@PROG total supersedes the plan's estimate without moving backwards."""
    tracker = PhaseTracker()
    tracker.banner()
    tracker.set_plan({"Records": 100, "Navmesh": 10})
    tracker.update("Records", 100, 100)
    before = tracker.phase()
    tracker.update("Navmesh", 0, 900)
    assert tracker.phase() >= before


def test_a_second_plugin_under_one_banner_never_resets(on):
    """A second plugin's plan and restarted counts must not undo the sweep."""
    tracker = PhaseTracker()
    tracker.banner()
    tracker.set_plan({"Records": 10})
    seen = []
    for done in range(11):
        tracker.update("Records", done, 10)
        seen.append(tracker.phase())
    tracker.set_plan({"Records": 999})
    for done in range(6):
        tracker.update("Records", done, 5)
        seen.append(tracker.phase())
    assert seen == sorted(seen)
    assert tracker.plan == {"Records": 5}


def test_out_of_order_reports_cannot_lower_a_count(on):
    """A late line from a slower worker must not walk the bar back."""
    tracker = PhaseTracker()
    tracker.banner()
    tracker.update("Meshes", 900, 1000)
    tracker.update("Meshes", 400, 1000)
    assert tracker.phase() == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# The whole-run bar
# ---------------------------------------------------------------------------

def test_overall_is_monotonic_bounded_and_reaches_one(on):
    """Ten phases of a ten-step run walk the run bar from 0 to exactly 1."""
    tracker = PhaseTracker()
    seen = []
    for _phase in range(10):
        tracker.banner()
        for done in range(0, 11):
            tracker.update("X", done, 10)
            seen.append(tracker.overall(10))
    assert seen == sorted(seen)
    assert all(0.0 <= v <= 1.0 for v in seen)
    assert seen[-1] == pytest.approx(1.0)


def test_overall_never_drops_at_a_phase_boundary(on):
    """The bar holds across the banner that resets the per-phase bar."""
    tracker = PhaseTracker()
    tracker.banner()
    tracker.update("X", 10, 10)
    peak = tracker.overall(4)
    tracker.banner()
    assert tracker.phase() == 0.0
    assert tracker.overall(4) >= peak


def test_a_global_action_with_no_banner_stays_in_range(on):
    """A run that prints no numbered banner still reports a sane fraction."""
    tracker = PhaseTracker()
    tracker.update("LOD", 1, 4)
    assert tracker.overall(1) == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# The widget
# ---------------------------------------------------------------------------

def _bars(colors):
    """A real ProgressBars on a real (withdrawn) Tk root, plus that root."""
    tk = pytest.importorskip("tkinter")
    pytest.importorskip("_tkinter")
    from tkinter import ttk
    from gui_progress import ProgressBars
    root = tk.Tk()
    root.withdraw()
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("Panel.TFrame", background=colors["panel"])
    style.configure("PanelSub.TLabel", background=colors["panel"])
    frame = ttk.Frame(root)
    frame.grid(row=0, column=0)
    return root, ProgressBars(frame, style, colors, row=1)


def test_the_bars_build_and_never_go_backwards(on):
    """The text-in-trough style resolves, and a whole run only ever climbs."""
    import gui
    root, bars = _bars(gui.CLR)
    try:
        bars.show(10)
        seen = []
        for line in ["  Phase 1: EXPORT TES4 RECORDS",
                     "@@PROG Export\t100\t1000\tSTAT",
                     "@@PROG Export\t900\t1000\tWEAP",
                     "  Phase 6: BUILD TES5 PLUGIN",
                     "@@PLAN Import\tRecords=100\tLandscape=20\tNavmesh=80",
                     "@@PROG Records\t100\t100\t",
                     "@@PROG Navmesh\t80\t80\tcell 12,4",
                     "@@PROG Landscape\t20\t20\t"]:
            bars.line(line)
            seen.append(bars._tracker.overall(10))
        assert seen == sorted(seen)
        assert bars._tracker.phase() == pytest.approx(1.0)
        bars.hide()
    finally:
        root.destroy()


def test_a_sentinel_is_eaten_and_a_banner_is_not(on):
    """Sentinels never reach the log; a phase banner still renders."""
    import gui
    root, bars = _bars(gui.CLR)
    try:
        bars.show(2)
        assert bars.line("@@PROG Meshes\t1\t2\tx") is True
        assert bars.line("@@PLAN Import\tRecords=1") is True
        assert bars.line("  Phase 3: CONVERT MESHES AND TEXTURES") is False
        assert bars.line("Found 20000 NIF files") is False
    finally:
        root.destroy()


def test_the_banner_pattern_matches_every_numbered_phase():
    """Every "Phase N:" banner convert.py prints starts a new sweep."""
    from gui_progress import BANNER
    for text in ("  Phase 1: EXPORT TES4 RECORDS",
                 "  Phase 12: PACK ZIP ARCHIVES",
                 "phase 6:"):
        assert BANNER.match(text)
    for text in ("  GENERATE LOD", "Phase one:", "  Phases 1: x", ""):
        assert not BANNER.match(text)
