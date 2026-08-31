"""Machine-parseable progress lines for the GUI's per-phase progress bars.

The GUI runs the pipeline as `convert.py` subprocesses and reads their stdout
line by line.  There is no in-process callback to hand a progress fraction to,
so a phase reports progress by PRINTING a line the GUI knows how to parse::

    @@PROG <phase>\t<done>\t<total>

`gui.py` intercepts any line starting with `@@PROG ` (see `_set_progress`),
drives its two progress bars from it, and never renders it to the log.

Two properties keep this from costing the conversion anything:

  * OFF by default.  Nothing is emitted unless the environment sets
    `TESCONV_PROGRESS=1` -- which only the GUI does, in the child's env.  A
    plain CLI run and the test suite see no `@@PROG` lines at all.
  * Throttled.  At most one line every `_MIN_INTERVAL` seconds per phase (plus
    an unconditional final line via `force=True`), so a tight 20k-item loop
    prints a few lines a second, not 20k.  A worker process must never call
    this -- only the PARENT's aggregator loop does, so there is no interleaving
    of half-written lines across processes.

`report` writes only to stdout and changes no artifact, so it is safe against
the byte-reproducibility contract.
"""
from __future__ import annotations

import os
import sys
import time

# The prefix the GUI matches on.  A tab-separated payload follows so a phase
# label may contain spaces ("SpeedTrees", "Landscape") without ambiguity.
SENTINEL = "@@PROG"

# A phase made of several sub-phases (Import = Records + Navmesh + Landscape)
# emits ONE of these at its start, declaring every sub-phase's item count.  It
# lets the GUI seed the whole-phase denominator up front, so the bar sweeps a
# single 0->100% across all sub-phases instead of hitting 100% on the first and
# sticking.  Payload: "@@PLAN <phase>\t<label>=<n>\t<label>=<n>...".
PLAN_SENTINEL = "@@PLAN"

ENV_VAR = "TESCONV_PROGRESS"

# Per-phase throttle.  0.4s -> at most ~2-3 lines/sec even in the hottest loop.
_MIN_INTERVAL = 0.4

_enabled: bool | None = None
_last: dict[str, float] = {}


def _on() -> bool:
    global _enabled
    if _enabled is None:
        _enabled = os.environ.get(ENV_VAR) == "1"
    return _enabled


def report(phase: str, done: int, total: int, *, force: bool = False) -> None:
    """Emit one progress line for *phase*, if progress reporting is enabled.

    No-op unless `TESCONV_PROGRESS=1`.  Throttled to one line per
    `_MIN_INTERVAL` per phase; pass `force=True` for the terminal line so the
    bar always lands on 100% regardless of when the last throttled line fell.
    Call only from the parent process's aggregator loop, never from a worker.
    """
    if not _on() or total <= 0:
        return
    now = time.monotonic()
    if not force and done < total and now - _last.get(phase, 0.0) < _MIN_INTERVAL:
        return
    _last[phase] = now
    try:
        sys.stdout.write(f"{SENTINEL} {phase}\t{int(done)}\t{int(total)}\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        # A closed/broken stdout must never take the conversion down over a
        # cosmetic progress line.
        pass


def plan(phase: str, **sub_totals: int) -> None:
    """Declare a multi-sub-phase phase's sub-part item counts (see PLAN_SENTINEL).

    Emit ONCE at the phase's start, before any sub-phase reports.  No-op unless
    `TESCONV_PROGRESS=1`.  A zero-count sub-part is fine (it just contributes
    nothing to the denominator); omit ones that will never run.
    """
    if not _on():
        return
    parts = "\t".join(f"{k}={int(v)}" for k, v in sub_totals.items())
    try:
        sys.stdout.write(f"{PLAN_SENTINEL} {phase}\t{parts}\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def parse_plan(line: str) -> tuple[str, dict] | None:
    """Parse a `@@PLAN` line into `(phase, {label: total})`, or None."""
    if not line.startswith(PLAN_SENTINEL + " "):
        return None
    body = line[len(PLAN_SENTINEL) + 1:].rstrip("\r\n").split("\t")
    if not body:
        return None
    phase, subs = body[0], {}
    for kv in body[1:]:
        label, _, val = kv.partition("=")
        try:
            subs[label] = int(val)
        except ValueError:
            continue
    return phase, subs


def overall_fraction(phases_started: int, frac: float, steps_total: int,
                     prev: float) -> float:
    """Whole-run progress in [0, 1] for the GUI's second (Overall) bar.

    Phases are weighted equally by step: `phases_started - 1` phases are behind
    us and the current one contributes `frac` of its slice.  Clamped to `[0,1]`
    and never allowed below `prev`, so a multi-label phase (import emits
    Records/Landscape/Navmesh under one banner, each resetting `frac`) advances
    the bar without ever slipping it backward.

    Pure arithmetic, extracted here so it is unit-tested directly rather than
    trapped in a GUI closure.
    """
    steps_total = steps_total if steps_total > 0 else 1
    phases_started = phases_started if phases_started > 0 else 1
    ov = (phases_started - 1 + max(0.0, min(1.0, frac))) / steps_total
    return max(prev, min(1.0, ov))


def parse(line: str) -> tuple[str, int, int] | None:
    """Parse a `@@PROG` line into `(phase, done, total)`, or None.

    Lives here, beside the writer, so the format has exactly one definition.
    The GUI imports and calls this.
    """
    if not line.startswith(SENTINEL + " "):
        return None
    try:
        phase, done, total = line[len(SENTINEL) + 1:].rstrip("\r\n").split("\t")
        return phase, int(done), int(total)
    except ValueError:
        return None
