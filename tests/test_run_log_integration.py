"""End-to-end run-log behaviour of the real convert.py process.

Unit tests cover rotation in isolation; these run the actual entry point,
because the defect this feature is most likely to grow is a wiring one -- the
log opening in the wrong place, or every step of a GUI run rotating the file
so the "last 3 runs" become the last 3 STEPS of one run.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run_log  # noqa: E402


# A plugin that cannot exist: the export stage fails immediately, so the run
# is fast, converts nothing, and still exercises the REAL logging path
# (informational flags like --list-mods deliberately skip run logging).
FAST_RUN = ("-f", "NoSuchPlugin.esm", "--export-only")


def _run(tmp_path, env_extra=None, args=FAST_RUN):
    """Run convert.py with logs/ redirected into tmp_path via a temp config."""
    env = dict(os.environ)
    env.pop(run_log.RUN_LOG_ENV_VAR, None)
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(ROOT / "convert.py"), *args],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=110,
    )


@pytest.fixture
def logs_dir(tmp_path, monkeypatch):
    """Point convert.py's logs/ at a temp dir by running it from a copy root.

    convert.py writes to SCRIPT_DIR/logs, so instead of relocating the whole
    project the real logs/ is snapshotted and restored around each test.
    """
    real = ROOT / "logs"
    backup = tmp_path / "backup"
    if real.exists():
        real.rename(backup)
    try:
        yield real
    finally:
        # Remove whatever the test produced, then put the user's logs back.
        if real.exists():
            for f in real.glob("*"):
                try:
                    f.unlink()
                except OSError:
                    pass
            try:
                real.rmdir()
            except OSError:
                pass
        if backup.exists():
            backup.rename(real)


def test_cli_run_writes_a_log(logs_dir):
    r = _run(logs_dir)
    path = run_log.log_path(logs_dir, 1)
    assert path.exists(), "a standalone convert.py run must leave a run log"
    text = path.read_text(encoding="utf-8")
    assert "# TESRACT run log" in text
    assert "NoSuchPlugin.esm" in text                # header records the argv
    assert "# Finished:" in text
    assert f"EXIT: {r.returncode}" in text           # the REAL exit status
    assert "Conversion Pipeline" in text             # console output captured


def test_informational_run_does_not_rotate(logs_dir):
    """--help/--list-mods convert nothing; they must not evict a real log."""
    _run(logs_dir)                                   # a real run
    before = run_log.log_path(logs_dir, 1).read_text(encoding="utf-8")
    for args in (("--help",), ("--list-mods",)):
        _run(logs_dir, args=args)
    assert not run_log.log_path(logs_dir, 2).exists()
    assert run_log.log_path(logs_dir, 1).read_text(encoding="utf-8") == before


def test_console_output_is_unchanged_by_teeing(logs_dir):
    """The Tee must not swallow or mangle stdout -- the GUI pipes it."""
    r = _run(logs_dir)
    body = run_log.log_path(logs_dir, 1).read_text(encoding="utf-8")
    for line in (l for l in r.stdout.splitlines() if l.strip()):
        assert line in body, f"console line missing from log: {line!r}"


def test_child_process_does_not_rotate(logs_dir):
    """A GUI child (TESCONV_RUN_LOG set) must not touch logs/ at all.

    This is the whole reason rotation lives with the run's owner: a 7-step GUI
    run would otherwise rotate seven times.
    """
    _run(logs_dir)                                   # run 1 -> creates run-1
    first = run_log.log_path(logs_dir, 1).read_text(encoding="utf-8")

    owned = str(logs_dir / "run-1.log")
    _run(logs_dir, {run_log.RUN_LOG_ENV_VAR: owned})
    assert not run_log.log_path(logs_dir, 2).exists(), "child rotated the logs"
    assert run_log.log_path(logs_dir, 1).read_text(encoding="utf-8") == first


def test_rotation_keeps_only_configured_count(logs_dir):
    """Four runs, keep=3 -> the oldest is evicted, newest is run-1."""
    for _ in range(4):
        _run(logs_dir)
    kept = sorted(p.name for p in logs_dir.glob("run-*.log"))
    assert kept == ["run-1.log", "run-2.log", "run-3.log"]


def test_log_runs_kept_zero_disables(logs_dir, tmp_path):
    """`logRunsKept: 0` is an explicit opt-out and must write nothing."""
    cfg_path = tmp_path / "cfg.json"
    base = json.loads((ROOT / "conversion_config.json").read_text(encoding="utf-8"))
    base["logRunsKept"] = 0
    cfg_path.write_text(json.dumps(base), encoding="utf-8")

    _run(logs_dir, args=("--config", str(cfg_path), *FAST_RUN))
    assert not run_log.log_path(logs_dir, 1).exists()


def test_log_runs_kept_honours_custom_count(logs_dir, tmp_path):
    cfg_path = tmp_path / "cfg.json"
    base = json.loads((ROOT / "conversion_config.json").read_text(encoding="utf-8"))
    base["logRunsKept"] = 2
    cfg_path.write_text(json.dumps(base), encoding="utf-8")

    for _ in range(4):
        _run(logs_dir, args=("--config", str(cfg_path), *FAST_RUN))
    kept = sorted(p.name for p in logs_dir.glob("run-*.log"))
    assert kept == ["run-1.log", "run-2.log"]


def test_shipped_config_declares_the_key():
    """The key must be discoverable in the config the user actually edits."""
    cfg = json.loads((ROOT / "conversion_config.json").read_text(encoding="utf-8"))
    assert cfg.get("logRunsKept") == run_log.DEFAULT_RUNS_KEPT
    assert any("logRunsKept" in k for k in cfg if k.startswith("//")), \
        "logRunsKept needs a // comment entry explaining it"
