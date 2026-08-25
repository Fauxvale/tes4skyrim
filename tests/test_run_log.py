"""Tests for run_log: rotation ordering, config clamping, degradation."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_log  # noqa: E402


def _write(logs, index, text):
    run_log.log_path(logs, index).write_text(text, encoding="utf-8")


def _read(logs, index):
    p = run_log.log_path(logs, index)
    return p.read_text(encoding="utf-8") if p.exists() else None


# ── rotation ──────────────────────────────────────────────────────────────

def test_rotate_shifts_newest_to_oldest(tmp_path):
    _write(tmp_path, 1, "first")
    _write(tmp_path, 2, "second")
    run_log.rotate(tmp_path, keep=3)
    # run-1 freed for the new run; the others shifted down by one.
    assert _read(tmp_path, 2) == "first"
    assert _read(tmp_path, 3) == "second"


def test_rotate_descending_never_clobbers(tmp_path):
    """Five runs: each must evict the oldest, never overwrite a live file."""
    for run in range(1, 6):
        run_log.rotate(tmp_path, keep=3)
        _write(tmp_path, 1, f"run{run}")
        # Only `keep` files ever exist.
        assert sorted(p.name for p in tmp_path.glob("run-*.log")) == \
            sorted(f"run-{i}.log" for i in range(1, min(run, 3) + 1))
    # After 5 runs the retained set is the last 3, newest first.
    assert _read(tmp_path, 1) == "run5"
    assert _read(tmp_path, 2) == "run4"
    assert _read(tmp_path, 3) == "run3"


def test_rotate_prunes_when_keep_lowered(tmp_path):
    for i in range(1, 6):
        _write(tmp_path, i, f"old{i}")
    run_log.rotate(tmp_path, keep=2)
    assert run_log.log_path(tmp_path, 3).exists() is False
    assert run_log.log_path(tmp_path, 4).exists() is False
    assert _read(tmp_path, 2) == "old1"


def test_rotate_creates_missing_dir(tmp_path):
    logs = tmp_path / "logs"
    assert run_log.rotate(logs, keep=3) is True
    assert logs.is_dir()


def test_rotate_disabled_when_keep_zero(tmp_path):
    assert run_log.rotate(tmp_path, keep=0) is False


def test_rotate_tolerates_gaps(tmp_path):
    """A missing run-1 (deleted by hand) must not abort the shift."""
    _write(tmp_path, 2, "second")
    run_log.rotate(tmp_path, keep=3)
    assert _read(tmp_path, 3) == "second"


# ── configuration ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("cfg,expected", [
    (None, 3),
    ({}, 3),
    ({"logRunsKept": 5}, 5),
    ({"logRunsKept": 0}, 0),          # explicit opt-out is honoured
    ({"logRunsKept": "7"}, 7),        # JSON string coerces
    ({"logRunsKept": -1}, 3),         # out of range -> default
    ({"logRunsKept": 500}, 3),        # absurd -> default, never hoard
    ({"logRunsKept": "abc"}, 3),      # malformed -> default
    ({"logRunsKept": None}, 3),
    ({"logRunsKept": True}, 3),       # bool is not a count
])
def test_runs_kept(cfg, expected):
    assert run_log.runs_kept(cfg) == expected


# ── file contents ─────────────────────────────────────────────────────────

def test_header_footer_roundtrip(tmp_path):
    path = run_log.log_path(tmp_path, 1)
    log = run_log.RunLog(path, {"Command": "Pipeline run", "Steps": "export"})
    log.write_line("hello")
    log.write_line("world")
    log.close("EXIT: OK")

    text = path.read_text(encoding="utf-8")
    assert "# TESRACT run log" in text
    assert "# Started:" in text
    assert "Command:  Pipeline run" in text
    assert "Steps:    export" in text
    assert "\nhello\nworld\n" in text
    assert "# Finished:" in text and "EXIT: OK" in text


def test_lines_written_before_close(tmp_path):
    """A hung or killed run must still have its lines on disk."""
    path = run_log.log_path(tmp_path, 1)
    log = run_log.RunLog(path, {})
    log.write_line("partial progress")
    # Deliberately not closed -- simulates a kill.
    assert "partial progress" in path.read_text(encoding="utf-8")
    assert "# Finished:" not in path.read_text(encoding="utf-8")
    log.close()


def test_empty_header_values_skipped(tmp_path):
    path = run_log.log_path(tmp_path, 1)
    run_log.RunLog(path, {"Command": "x", "Output": "", "Version": None}).close()
    text = path.read_text(encoding="utf-8")
    assert "Output" not in text and "Version" not in text


def test_write_after_failure_is_silent(tmp_path):
    """A dead handle degrades to no-op; it must never raise into a run."""
    log = run_log.RunLog(run_log.log_path(tmp_path, 1), {})
    log._fh.close()          # simulate the handle dying mid-run
    log.write_line("still fine")   # must not raise
    log.close()
    assert log.active is False


def test_unwritable_path_degrades(tmp_path):
    """An undeletable/unopenable target yields an inactive log, not a crash."""
    target = tmp_path / "run-1.log"
    target.mkdir()           # a directory cannot be opened for writing
    log = run_log.RunLog(target, {})
    assert log.active is False
    log.write_line("ignored")
    log.close("EXIT: OK")


# ── tee ───────────────────────────────────────────────────────────────────

class _FakeStream:
    def __init__(self):
        self.text = ""

    def write(self, s):
        self.text += s
        return len(s)

    def flush(self):
        pass


def test_tee_mirrors_and_passes_through(tmp_path):
    path = run_log.log_path(tmp_path, 1)
    log = run_log.RunLog(path, {})
    stream = _FakeStream()
    tee = run_log.Tee(stream, log)

    tee.write("alpha\nbeta\n")
    assert stream.text == "alpha\nbeta\n"      # console unaffected
    body = path.read_text(encoding="utf-8")
    assert "alpha" in body and "beta" in body
    log.close()


def test_tee_buffers_partial_lines(tmp_path):
    """print(..., end="") fragments must land as ONE log line."""
    path = run_log.log_path(tmp_path, 1)
    log = run_log.RunLog(path, {})
    tee = run_log.Tee(_FakeStream(), log)

    tee.write("Converting ")
    tee.write("mesh 5/10")
    assert "Converting" not in path.read_text(encoding="utf-8")  # still buffered
    tee.write("\n")
    assert "Converting mesh 5/10" in path.read_text(encoding="utf-8")
    log.close()


def test_tee_flushes_trailing_partial_on_finish(tmp_path):
    path = run_log.log_path(tmp_path, 1)
    log = run_log.RunLog(path, {})
    real_out = sys.stdout
    sys.stdout = run_log.Tee(_FakeStream(), log)
    sys.stdout.write("no trailing newline")
    try:
        run_log.finish_cli_run(log, "EXIT: OK")
    finally:
        sys.stdout = real_out
    assert "no trailing newline" in path.read_text(encoding="utf-8")


def test_tee_forwards_unknown_attributes():
    stream = _FakeStream()
    stream.encoding = "utf-8"
    assert run_log.Tee(stream, None).encoding == "utf-8"


# ── ownership ─────────────────────────────────────────────────────────────

def test_child_process_does_not_open_its_own_log(tmp_path, monkeypatch):
    """TESCONV_RUN_LOG set => a parent owns the run; the child must not write."""
    monkeypatch.setenv(run_log.RUN_LOG_ENV_VAR, str(tmp_path / "run-1.log"))
    assert run_log.start_cli_run(tmp_path, {"logRunsKept": 3}) is None
    assert list(tmp_path.glob("run-*.log")) == []


def test_start_cli_run_rotates_and_tees(tmp_path, monkeypatch):
    monkeypatch.delenv(run_log.RUN_LOG_ENV_VAR, raising=False)
    _write(tmp_path, 1, "previous run")
    real_out, real_err = sys.stdout, sys.stderr
    try:
        log = run_log.start_cli_run(tmp_path, {"logRunsKept": 3},
                                    {"Command": "convert.py"})
        assert log is not None
        assert isinstance(sys.stdout, run_log.Tee)
        print("captured line")
        run_log.finish_cli_run(log, "EXIT: OK")
    finally:
        sys.stdout, sys.stderr = real_out, real_err

    assert sys.stdout is real_out                     # streams restored
    assert _read(tmp_path, 2) == "previous run"       # prior run preserved
    assert "captured line" in _read(tmp_path, 1)


def test_start_cli_run_disabled_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv(run_log.RUN_LOG_ENV_VAR, raising=False)
    assert run_log.start_cli_run(tmp_path, {"logRunsKept": 0}) is None
    assert not isinstance(sys.stdout, run_log.Tee)


def test_finish_cli_run_accepts_none():
    run_log.finish_cli_run(None)  # must not raise


# ── formatting ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("secs,expected", [
    (0, "0s"), (9, "9s"), (61, "1m 01s"), (3600, "1h 00m 00s"),
    (3661, "1h 01m 01s"), (-5, "0s"),
])
def test_format_elapsed(secs, expected):
    assert run_log.format_elapsed(secs) == expected


def test_format_size():
    assert run_log.format_size(512) == "512 B"
    assert run_log.format_size(2048) == "2.0 KB"
    assert run_log.format_size(5 * 1024 * 1024) == "5.0 MB"
