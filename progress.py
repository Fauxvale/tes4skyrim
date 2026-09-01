"""Progress sentinels a pipeline stage prints and the GUI parses off stdout.

Every function is a no-op unless ``TESCONV_PROGRESS=1``.  Only the GUI sets it,
in the child process environment, so a plain run and the test suite emit nothing
and no output byte changes.  Emit ONLY from a parent process, and only from a
loop that counts COMPLETIONS.
See: docs/commentary/gui_progress.md#why-sentinels-on-stdout
"""
import os
import time

__all__ = ["PROGRESS_ENV_VAR", "PROG_PREFIX", "PLAN_PREFIX", "enabled",
           "report", "plan", "parse", "parse_plan", "track", "track_records",
           "PhaseTracker"]

PROGRESS_ENV_VAR = "TESCONV_PROGRESS"

#: A log line starting with either prefix is a sentinel and is never rendered.
PROG_PREFIX = "@@PROG "
PLAN_PREFIX = "@@PLAN "

#: Seconds between two unforced reports of one label.
_THROTTLE = 0.4

#: Longest item text carried in a bar; the TAIL is kept, so a path shows its file.
_ITEM_CHARS = 60

_last_emit = {}


def enabled() -> bool:
    """True when the parent asked this process to print progress sentinels."""
    return os.environ.get(PROGRESS_ENV_VAR, "").strip() == "1"


def _clean(item) -> str:
    """`item` reduced to one short single-line field."""
    text = str(item or "")
    for bad in ("\t", "\r", "\n"):
        text = text.replace(bad, " ")
    return text.strip()[-_ITEM_CHARS:]


def report(label: str, done: int, total: int, item="", *, force=False) -> None:
    """Print one sub-phase's progress, throttled to one line per 0.4 s.

    `force` bypasses the throttle; use it for a sub-phase's first and last
    report so the bar starts held and lands exactly on its total.
    See: docs/commentary/gui_progress.md#only-the-parent-process-may-emit
    """
    if not enabled():
        return
    now = time.monotonic()
    if not force and now - _last_emit.get(label, 0.0) < _THROTTLE:
        return
    _last_emit[label] = now
    print("%s%s\t%d\t%d\t%s"
          % (PROG_PREFIX, label, done, total, _clean(item)), flush=True)


def plan(phase: str, **sub_totals: int) -> None:
    """Declare every sub-part's item count, BEFORE the phase's first report.

    See: docs/commentary/gui_progress.md#combining-sub-phases-into-one-sweep
    """
    if not enabled() or not sub_totals:
        return
    parts = "\t".join("%s=%d" % (k, v) for k, v in sub_totals.items())
    print("%s%s\t%s" % (PLAN_PREFIX, phase, parts), flush=True)


def track(label: str, results, jobs, size=None, name=None):
    """Yield each result, reporting one `label` step per job as it lands.

    `jobs` parallels `results` in submission order, so this belongs on an
    ORDERED consumer (`map` / `ex.map` / a serial loop), never on
    `as_completed`.  `size(job)` is how many items a job stands for (default
    1); `name(job)` labels it inside the bar.
    See: docs/commentary/gui_progress.md#only-the-parent-process-may-emit
    """
    total = sum(size(j) for j in jobs) if size else len(jobs)
    done = 0
    for job, result in zip(jobs, results):
        yield result
        done += size(job) if size else 1
        report(label, done, total, name(job) if name else "")
    report(label, total, total, force=True)


def track_records(work_items: list, lands: int, pgrds: int):
    """Yield each record work item, having first planned the Import phase.

    See: docs/commentary/gui_progress.md#where-the-plan-counts-come-from
    """
    plan('Import', Records=len(work_items), Landscape=lands, Navmesh=pgrds)
    yield from track('Records', work_items, work_items)


def parse(line: str):
    """(label, done, total, item) for a `@@PROG` line, else None."""
    if not line.startswith(PROG_PREFIX):
        return None
    parts = line[len(PROG_PREFIX):].split("\t")
    if len(parts) < 3 or not parts[0]:
        return None
    try:
        done, total = int(parts[1]), int(parts[2])
    except ValueError:
        return None
    return parts[0], done, total, parts[3] if len(parts) > 3 else ""


def parse_plan(line: str):
    """(phase, {label: total}) for a `@@PLAN` line, else None."""
    if not line.startswith(PLAN_PREFIX):
        return None
    parts = line[len(PLAN_PREFIX):].split("\t")
    totals = {}
    for part in parts[1:]:
        label, sep, count = part.partition("=")
        if not sep or not label or not count.isdigit():
            return None
        totals[label] = int(count)
    return (parts[0], totals) if parts[0] and totals else None


class PhaseTracker:
    """Monotonic per-phase and whole-run fractions, driven by the sentinels.

    The phase fraction returns to 0 at a phase banner and nowhere else; the
    whole-run fraction never decreases at all.
    See: docs/commentary/gui_progress.md#monotonicity-is-the-whole-requirement
    """

    def __init__(self):
        """A tracker sitting before the first banner, with both bars at zero."""
        self.phases_started = 0
        self.overall_max = 0.0
        self._new_phase()

    def _new_phase(self) -> None:
        """Clear everything scoped to ONE phase."""
        self.plan = {}
        self.done = {}
        self.phase_max = 0.0
        self.item = ""

    def reset(self) -> None:
        """Forget the whole run; both bars go back to zero."""
        self.phases_started = 0
        self.overall_max = 0.0
        self._new_phase()

    def banner(self) -> None:
        """Begin a phase: the only place the phase bar may return to zero."""
        self.phases_started += 1
        self._new_phase()

    def set_plan(self, totals: dict) -> None:
        """Seed this phase's denominators; a second plan is ignored.

        See: docs/commentary/gui_progress.md#combining-sub-phases-into-one-sweep
        """
        if self.plan or not totals:
            return
        self.plan = {k: max(0, int(v)) for k, v in totals.items()}

    def update(self, label: str, done: int, total: int, item="") -> None:
        """Fold one `@@PROG` in; the phase fraction can only hold or climb."""
        self.plan[label] = max(0, int(total))
        self.done[label] = max(self.done.get(label, 0), max(0, int(done)))
        self.item = item
        span = sum(self.plan.values())
        if span > 0:
            frac = sum(min(self.done.get(k, 0), v)
                       for k, v in self.plan.items()) / span
            self.phase_max = max(self.phase_max, min(1.0, frac))

    def counted(self) -> bool:
        """True once this phase has reported a count, so its bar can fill."""
        return bool(self.done)

    def phase(self) -> float:
        """The per-phase fraction, 0..1."""
        return self.phase_max

    def overall(self, steps_total: int) -> float:
        """The whole-run fraction, 0..1, across `steps_total` pipeline steps.

        See: docs/commentary/gui_progress.md#the-whole-run-bar
        """
        steps = max(1, int(steps_total or 1))
        started = max(1, self.phases_started)
        frac = (started - 1 + self.phase_max) / steps
        self.overall_max = max(self.overall_max, min(1.0, max(0.0, frac)))
        return self.overall_max
