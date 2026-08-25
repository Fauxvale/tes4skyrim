"""Rotating per-run log files.

A conversion run's output used to exist only in the GUI's scrollback, so
closing the window -- or starting the next run, which clears the widget --
destroyed the record of the run the user had just played in game.  That is
exactly the evidence that costs a full build-and-play cycle to recreate.

Files are FIXED names, newest first::

    logs/run-1.log      most recent (the one being written, while a run is live)
    logs/run-2.log
    logs/run-3.log

Fixed rather than timestamped so "the last run" is always ``logs/run-1.log``
with no globbing; the wall-clock time lives in the header inside the file.
How many are kept comes from ``logRunsKept`` in conversion_config.json.

WHO ROTATES
-----------
The run's OWNER rotates, never each process: a GUI pipeline run is usually
several ``convert.py`` invocations (one per step, see gui.py's step loop), so
rotating per process would leave the "last 3 runs" holding the last 3 STEPS of
one run.  The GUI rotates once per run and writes every line through its own
log sink; it sets ``TESCONV_RUN_LOG`` in the child environment, which tells
``convert.py`` a run log already exists so it neither rotates nor writes.  A
bare ``python convert.py`` sees no such variable, so there the process IS the
run and it rotates for itself.

Nothing here may ever fail a conversion.  Every filesystem operation is
individually guarded and degrades to "no logging" rather than raising.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Set by a run owner (the GUI) to the absolute path of the run log it already
# opened.  Its presence means "a run log exists for this run" -- a child must
# not rotate, and must not write.
RUN_LOG_ENV_VAR = "TESCONV_RUN_LOG"

# Fallback when conversion_config.json has no `logRunsKept`.
DEFAULT_RUNS_KEPT = 3

# Guard rails for the configured value.  0 disables run logging entirely; the
# upper bound stops a typo (30 vs 3) from silently hoarding gigabytes.
MIN_RUNS_KEPT = 0
MAX_RUNS_KEPT = 99

_RULE = "-" * 60


def runs_kept(config: dict | None) -> int:
    """How many run logs to keep, from `logRunsKept`, clamped to sane bounds.

    A missing, malformed or out-of-range value falls back to the default
    rather than disabling logging -- a bad config entry should not silently
    cost the user their logs.
    """
    if not config:
        return DEFAULT_RUNS_KEPT
    raw = config.get("logRunsKept", DEFAULT_RUNS_KEPT)
    if isinstance(raw, bool):  # bools are ints; `true` is not a count
        return DEFAULT_RUNS_KEPT
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_RUNS_KEPT
    if n < MIN_RUNS_KEPT or n > MAX_RUNS_KEPT:
        return DEFAULT_RUNS_KEPT
    return n


def log_path(logs_dir, index: int = 1) -> Path:
    """Path of the `index`-th newest run log (1 = most recent)."""
    return Path(logs_dir) / f"run-{index}.log"


def rotate(logs_dir, keep: int = DEFAULT_RUNS_KEPT) -> bool:
    """Shift run-N.log down by one, freeing run-1.log for a new run.

    Renames DESCENDING (run-2 -> run-3 before run-1 -> run-2); ascending would
    clobber.  Renaming rather than copying is O(1) whatever the size, and on
    Windows a stale handle from a previous session blocks a delete-then-create
    where it does not block a rename.

    Returns True if run-1.log is believed free.  Never raises.
    """
    if keep <= 0:
        return False
    logs = Path(logs_dir)
    try:
        logs.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    # Drop everything at or past the retention limit.  A range rather than
    # just `keep` so lowering logRunsKept prunes the now-surplus files instead
    # of orphaning them forever.
    for i in range(keep, MAX_RUNS_KEPT + 1):
        try:
            log_path(logs, i).unlink()
        except OSError:
            pass

    for i in range(keep - 1, 0, -1):
        src, dst = log_path(logs, i), log_path(logs, i + 1)
        try:
            if src.exists():
                os.replace(src, dst)
        except OSError:
            # Locked by an editor, or a permission problem.  Later files are
            # independent, so keep going rather than abandoning the rotation.
            pass

    # If run-1.log survived the shift (its rename failed) the new run would
    # append to the previous one.  Truncation is handled by opening "w".
    return True


class RunLog:
    """A single run's log file: header, verbatim lines, footer.

    Line-buffered and flushed per line -- a log that only reaches disk on
    clean exit is empty exactly when it matters most (a crash or a hang).  The
    cost is nothing next to a stage that saturates every core for minutes.
    """

    def __init__(self, path, header: dict | None = None):
        self.path = Path(path)
        self._fh = None
        self._start = time.time()
        try:
            self._fh = open(self.path, "w", encoding="utf-8",
                            errors="replace", newline="\n")
        except OSError:
            self._fh = None
            return
        self._write_header(header or {})

    @property
    def active(self) -> bool:
        return self._fh is not None

    def _raw(self, text: str):
        if self._fh is None:
            return
        try:
            self._fh.write(text)
            self._fh.flush()
        except (OSError, ValueError):
            # The disk filled, or the handle died.  Stop trying; a conversion
            # must never fail because its log could not be written.
            try:
                self._fh.close()
            except (OSError, ValueError):
                pass
            self._fh = None

    def _write_header(self, header: dict):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._start))
        lines = ["# TESRACT run log", f"# Started:  {stamp}"]
        for key, value in header.items():
            if value in (None, ""):
                continue
            lines.append(f"# {key + ':':9} {value}")
        lines.append(_RULE)
        self._raw("\n".join(lines) + "\n")

    def write_line(self, line: str):
        """Append one line verbatim (no styling -- tags are presentation)."""
        self._raw(line.rstrip("\r\n") + "\n")

    def close(self, status: str | None = None):
        """Write the footer and close.

        A run killed mid-flight simply has no footer, which is itself the
        signal that it did not terminate cleanly.
        """
        if self._fh is None:
            return
        elapsed = time.time() - self._start
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        tail = f"# Finished: {stamp}  ({format_elapsed(elapsed)})"
        if status:
            tail += f"  {status}"
        self._raw(_RULE + "\n" + tail + "\n")
        try:
            self._fh.close()
        except (OSError, ValueError):
            pass
        self._fh = None

    # Context-manager sugar so a CLI run closes on any exit path.
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close("EXIT: OK" if exc_type is None else f"EXIT: {exc_type.__name__}")
        return False


def format_elapsed(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


class Tee:
    """Mirror a text stream into a RunLog, line by line.

    Wraps sys.stdout/sys.stderr for the CLI path, where there is no GUI sink
    to hook.  Partial writes are buffered until a newline so a progress line
    built from several `print(..., end="")` calls lands as one log line.
    """

    def __init__(self, stream, run_log: RunLog):
        self._stream = stream
        self._log = run_log
        self._buf = ""

    def write(self, text):
        try:
            n = self._stream.write(text)
        except (OSError, ValueError):
            n = len(text)
        if self._log is not None and text:
            self._buf += text
            if "\n" in self._buf:
                *lines, self._buf = self._buf.split("\n")
                for line in lines:
                    self._log.write_line(line)
        return n

    def flush(self):
        try:
            self._stream.flush()
        except (OSError, ValueError):
            pass

    def close_buffer(self):
        """Flush a trailing partial line (no newline) into the log."""
        if self._log is not None and self._buf:
            self._log.write_line(self._buf)
            self._buf = ""

    # Anything else -- isatty, encoding, fileno, buffer -- passes through, so
    # the wrapper stays transparent to code that inspects the stream.
    def __getattr__(self, name):
        return getattr(self._stream, name)


def start_cli_run(logs_dir, config: dict | None, header: dict | None = None):
    """Rotate and begin a run log for a standalone CLI run, teeing stdout.

    Returns the RunLog, or None when logging is disabled (`logRunsKept: 0`),
    unavailable, or when a run owner already opened one (the GUI case, flagged
    by TESCONV_RUN_LOG).  Callers must pair a non-None result with `finish`.
    """
    if os.environ.get(RUN_LOG_ENV_VAR):
        return None  # a parent owns this run's log
    keep = runs_kept(config)
    if keep <= 0:
        return None
    if not rotate(logs_dir, keep):
        return None
    run_log = RunLog(log_path(logs_dir, 1), header)
    if not run_log.active:
        return None
    sys.stdout = Tee(sys.stdout, run_log)
    sys.stderr = Tee(sys.stderr, run_log)
    return run_log


def finish_cli_run(run_log, status: str | None = None):
    """Unwrap the teed streams and close the run log.  Never raises."""
    if run_log is None:
        return
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if isinstance(stream, Tee):
            stream.close_buffer()
            setattr(sys, name, stream._stream)
    run_log.close(status)
