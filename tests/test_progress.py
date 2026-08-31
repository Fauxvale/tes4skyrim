"""Tests for the GUI progress emitter/parser and whole-run fraction.

The pipeline reports per-phase progress by printing `@@PROG` lines that the GUI
parses to drive two progress bars.  These cover the parts that are pure logic:
the env gate, the line format round-trip, and the monotonic whole-run fraction.
The GUI widget wiring itself is not unit-tested (it needs a display).
"""
import io
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import progress  # noqa: E402


def _emit(**env):
    """Run report() under a fresh env and return what it wrote to stdout."""
    progress._enabled = None            # re-read the env each time
    progress._last.clear()
    old = os.environ.get(progress.ENV_VAR)
    if env.get('on'):
        os.environ[progress.ENV_VAR] = '1'
    else:
        os.environ.pop(progress.ENV_VAR, None)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            progress.report('Meshes', 5, 10, force=True)
        return buf.getvalue()
    finally:
        if old is None:
            os.environ.pop(progress.ENV_VAR, None)
        else:
            os.environ[progress.ENV_VAR] = old
        progress._enabled = None


def test_disabled_by_default_emits_nothing():
    assert _emit(on=False) == ''


def test_enabled_emits_one_parseable_line():
    out = _emit(on=True)
    assert out == '@@PROG Meshes\t5\t10\n'
    assert progress.parse(out.strip()) == ('Meshes', 5, 10)


def test_throttle_lets_first_and_forced_through():
    progress._enabled = True
    progress._last.clear()
    buf = io.StringIO()
    with redirect_stdout(buf):
        progress.report('X', 1, 100)          # first: emitted
        progress.report('X', 2, 100)          # throttled away
        progress.report('X', 100, 100, force=True)   # forced: emitted
    lines = [l for l in buf.getvalue().splitlines() if l]
    assert len(lines) == 2
    assert lines[-1] == '@@PROG X\t100\t100'
    progress._enabled = None


def test_parse_rejects_non_sentinel_and_garbage():
    assert progress.parse('just a normal log line') is None
    assert progress.parse('@@PROG bad payload') is None
    assert progress.parse('@@PROG Phase\tNaN\t3') is None
    # A phase label may contain spaces; the tab split still recovers it.
    assert progress.parse('@@PROG Two Words\t3\t7') == ('Two Words', 3, 7)


def test_overall_fraction_is_monotonic_and_bounded():
    # Replays a 3-step run: Export(1 phase), Import(3 sub-labels under 1 banner),
    # Meshes(1 phase).  The whole-run bar must never slip backward and must end
    # at 1.0 even though each import sub-label resets `frac`.
    steps_total = 3
    prev = 0.0
    phases = 0
    stream = [
        ('banner', None),
        ('Export', 0.5), ('Export', 1.0),
        ('banner', None),
        ('Records', 0.5), ('Records', 1.0),
        ('Landscape', 0.25), ('Landscape', 1.0),
        ('Navmesh', 0.05), ('Navmesh', 1.0),
        ('banner', None),
        ('Meshes', 0.5), ('Meshes', 1.0),
    ]
    for label, frac in stream:
        if label == 'banner':
            phases += 1
            continue
        ov = progress.overall_fraction(phases, frac, steps_total, prev)
        assert 0.0 <= ov <= 1.0
        assert ov >= prev                       # monotonic
        prev = ov
    assert prev >= 0.999                          # reaches 100% by the end


def test_per_phase_bar_never_backward_within_a_step():
    """Replicates the GUI per-phase clamp: within ONE step (one banner) the bar
    is the running max of every reported fraction, so it never decreases even
    when sub-phases (Records/Landscape/Navmesh) or extra plugins restart the
    underlying count.  Only a new step (banner) resets it.
    """
    def run_step(reports):
        phase_max = 0.0
        seen = []
        for done, total in reports:
            frac = done / total if total else 0.0
            phase_max = max(phase_max, frac)      # the GUI's clamp
            seen.append(phase_max)
        return seen

    # Import step: Records 0..100, then Landscape restarts 0..4, then Navmesh
    # restarts 0..200 -- all under one banner.
    seq = run_step([(50, 100), (100, 100),
                    (1, 4), (4, 4),
                    (10, 200), (100, 200), (200, 200)])
    assert seq == sorted(seq), f"went backward within a step: {seq}"
    assert seq[-1] == 1.0

    # Meshes step across two plugins (same label, count restarts): still forward.
    seq2 = run_step([(9000, 10000), (10000, 10000), (500, 8000), (8000, 8000)])
    assert seq2 == sorted(seq2), f"went backward within a step: {seq2}"


def test_parse_plan_roundtrip_and_garbage():
    import io
    from contextlib import redirect_stdout
    progress._enabled = True
    buf = io.StringIO()
    with redirect_stdout(buf):
        progress.plan('Import', Records=1000, Navmesh=500, Landscape=200)
    line = buf.getvalue().strip()
    phase, subs = progress.parse_plan(line)
    assert phase == 'Import'
    assert subs == {'Records': 1000, 'Navmesh': 500, 'Landscape': 200}
    assert progress.parse_plan('@@PROG Records\t5\t10') is None
    assert progress.parse_plan('ordinary line') is None
    progress._enabled = None


def _sweep(plan_subs, reports):
    """Replicate the GUI's plan-combined phase bar and return its values."""
    plan = {k: int(v) for k, v in plan_subs.items() if int(v) > 0}
    done, phase_max, bars = {}, 0.0, []
    for label, d, t in reports:
        plan[label] = int(t)                      # exact total replaces estimate
        done[label] = max(done.get(label, 0), int(d))
        denom = sum(plan.values())
        num = sum(min(done.get(k, 0), tt) for k, tt in plan.items())
        frac = (num / denom) if denom else 0.0
        phase_max = max(phase_max, frac)          # clamp
        bars.append(phase_max)
    return bars


def test_multi_subphase_is_one_monotonic_sweep_to_100():
    # Import: Records (fast) then Navmesh (slow) then Landscape.  With the plan
    # seeded, Records must NOT reach 100%, the bar only climbs, and it ends at 1.
    bars = _sweep(
        {'Records': 1000, 'Navmesh': 500, 'Landscape': 200},
        [('Records', 250, 1000), ('Records', 1000, 1000),
         ('Navmesh', 240, 480), ('Navmesh', 480, 480),
         ('Landscape', 100, 200), ('Landscape', 200, 200)])
    assert bars == sorted(bars), f"went backward: {bars}"
    assert bars[1] < 0.65, f"Records reached the top alone: {bars[1]}"
    assert bars[-1] >= 0.999


def test_single_label_phase_without_plan_still_sweeps():
    # No @@PLAN: the first report seeds the (only) component; plain done/total.
    bars = _sweep({}, [('Meshes', 25, 100), ('Meshes', 100, 100)])
    assert bars == [0.25, 1.0]


def test_overall_fraction_defends_bad_inputs():
    assert progress.overall_fraction(0, 0.0, 0, 0.0) == 0.0       # no div-by-zero
    assert progress.overall_fraction(1, 5.0, 2, 0.0) == 0.5       # frac clamped to 1
    assert progress.overall_fraction(1, -1.0, 2, 0.3) == 0.3      # frac clamped to 0, held by prev
