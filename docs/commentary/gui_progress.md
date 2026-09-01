# progress.py — the GUI's progress bars

**Code:** `progress.py`, `gui.py`

## Contents

- [Why sentinels on stdout](#why-sentinels-on-stdout)
- [Only the parent process may emit](#only-the-parent-process-may-emit)
- [Monotonicity is the whole requirement](#monotonicity-is-the-whole-requirement)
- [Combining sub-phases into one sweep](#combining-sub-phases-into-one-sweep)
- [Where the plan counts come from](#where-the-plan-counts-come-from)
- [The whole-run bar](#the-whole-run-bar)
- [The throbber trap](#the-throbber-trap)
- [Where the bars live in the sidebar](#where-the-bars-live-in-the-sidebar)
- [Emitting from a call site without growing it](#emitting-from-a-call-site-without-growing-it)
- [Export: chunked formatting must not leap](#export-chunked-formatting-must-not-leap)
- [Where each label is emitted](#where-each-label-is-emitted)
- [Navmesh cache](#navmesh-cache)

## Why sentinels on stdout

The pipeline runs as one or more `convert.py` **subprocesses**. The GUI captures
their stdout line by line (reader thread → `queue.Queue` → `root.after`-drained
`_log` on the UI thread) and has no in-process callback a stage could hand a
fraction to. So a stage reports progress by *printing* a machine-parseable line
that the GUI parses and never renders:

```
@@PROG <label>\t<done>\t<total>\t<item>
@@PLAN <phase>\t<label>=<n>\t<label>=<n>...
```

Both are tab-separated; `<item>` (the file/record being worked on right now) is
optional and is shown inside the bar.

Everything is a no-op unless `TESCONV_PROGRESS=1`. Only the GUI sets it, in the
child process environment, so a plain `python convert.py` run and the whole test
suite emit nothing. That gating is what keeps the output ESM
byte-reproducible: progress touches stdout and nothing else.

`report()` is throttled to one line per 0.4 s per label, so a 20,000-item loop
prints a few lines a second rather than 20,000 lines. `force=True` bypasses the
throttle and is used for the first and last emission of every sub-phase.

## Only the parent process may emit

Pool workers run under `pythonw.exe`, where stdout goes nowhere, and dozens of
them writing at once would interleave half-written lines. Every emission
therefore sits in the parent's aggregator loop — the `imap_unordered` /
`as_completed` / `ex.map` consumer, or a serial loop — which already sees each
completion.

For the same reason the counter is the **number of completions**, not the item's
own index: `imap_unordered` and `as_completed` yield in wall-clock finish order,
so an index would jump around while a completion count only climbs.

## Monotonicity is the whole requirement

A bar that moves backwards reads as a bug even when the number behind it is
correct. The only places a bar may return to 0 are the ten top-level phase
boundaries. Three mechanisms enforce that:

1. `PhaseTracker.update()` keeps `done[label] = max(seen, new)`, so an
   out-of-order or repeated report cannot lower a count.
2. `phase_max = max(phase_max, frac)` clamps the per-phase bar, so refining a
   denominator can only hold the bar, never drop it.
3. `overall = max(prev_overall, …)` clamps the whole-run bar globally.

## Combining sub-phases into one sweep

A phase with several sub-phases — Import is Records + Navmesh + Landscape,
Sounds is Voices + Sounds + Music — must be ONE 0→100% sweep, not three.

The naive "fraction of the sub-phase currently reporting" makes the bar hit 100%
on sub-phase 1 and stick there, because the later sub-phases' totals are not
known while sub-phase 1 runs. `@@PLAN` fixes that: the phase declares every
sub-part's item count *before* the first `@@PROG`, so the denominator already
includes the work that has not started.

```
frac = sum(min(done[k], plan[k]) for k in plan) / sum(plan.values())
```

A `@@PROG` total **replaces** the planned total — the running stage's count is
exact and the plan's was an estimate — and a label absent from the plan appends
itself. Revising a later label's total downwards raises the fraction; revising
it upwards would lower it, and that is exactly what `phase_max` absorbs.

Only the FIRST `@@PLAN` of a phase is honoured. A second plugin running the same
phase under one banner emits its own plan, which must not reset the accumulated
sweep. `done` is cleared by the phase banner, never by a plan.

Single-label phases need no plan at all: their first `@@PROG` seeds the only
component and the bar is plain `done/total`.

### Where the plan counts come from

- **Import** — free from `by_type`: `Records=len(work_items)`,
  `Landscape=len(by_type['LAND'])`, `Navmesh=len(by_type['PGRD'])` (navmesh
  generates roughly one job per pathgrid). Import must NOT emit an early
  `@@PROG`: that would seed the plan and make the real `@@PLAN` be ignored.
- **Sounds** — a cheap pre-walk in `convert.py`'s `phase_sounds` of the plugin's
  export `sound/voice`, `sound` (minus voice) and `music` trees. Rough is fine;
  the per-sub-phase `@@PROG` totals supersede it.

## The whole-run bar

```
overall = max(prev, min(1.0, (max(phases_started, 1) - 1 + phase_frac) / steps_total))
```

`steps_total` is the number of selected pipeline steps, passed in when the run
starts. `phases_started` counts `Phase N:` banners seen. `max(phases_started, 1)`
keeps a global action — which prints no numbered banner at all — at or above 0
rather than negative.

## The throbber trap

A ttk **indeterminate** progress bar slides its block right and then cycles it
back to the left on each pass. That is visually identical to the bar jumping
backwards, and it was reported as exactly that during Export, where `read_file`
scans the whole ESM and the worker pool spins up (several seconds) before the
first `@@PROG` could possibly arrive.

The rule that removes it costs nothing and needs no extra emission: **a `Phase N:`
banner puts the phase bar into determinate mode at 0%.** Every counted phase
therefore holds a still 0% through its setup gap and climbs from there, and no
oscillation is ever visible inside a pipeline run.

Throbbers stay correct for work with **no way to count it**, and that is the only
place one is left: a global action (Convert to Master, Package Start Mod, Pack
LOD, Convert UI, Create LOD) prints no numbered banner, so its bar throbs until
it emits a `@@PROG` of its own, if it ever does. LOD's tile bake is countable and
is NOT yet wired up, so Create LOD throbs for its whole run.

The whole-run bar is always determinate: it knows `steps_total` from the start.

## Where the bars live in the sidebar

Both bars and the status line are pinned to the BOTTOM of the sidebar, OUTSIDE
the scroller: they report what the app is doing right now, so scrolling them out
of view would be worse than the clipping the scroller fixes. They sit in
`sidebar`'s own grid (rows 1 and 2) under the scroll viewport in row 0, which
takes all the slack — that keeps the two together with the spare space above
them, rather than pooling a void between the bar and "Ready". Grid, not pack:
`sidebar` is grid-managed, and the two geometry managers cannot share a parent.

The phase bar draws its current item INSIDE the trough, which a stock ttk
progressbar cannot do: `Item.Horizontal.TProgressbar` adds a `label` element to
the layout (`label` is a core ttk element, so it resolves under `clam`) and the
text is set with `style.configure(..., text=...)`.

## Emitting from a call site without growing it

`report()` is rarely called directly at a loop. `progress.track(label, results,
jobs, size, name)` is a generator that wraps an ORDERED consumer — `map`,
`ex.map`, or a plain sequence — yields each result untouched and reports one step
per job, so a call site turns into the same number of lines it already had.
`track_records()` is the Import variant: it emits the `@@PLAN` first, then tracks
the record loop. Neither may be used on `as_completed`, whose order does not
match `jobs`; those sites (Creatures, Voices, Music) count completions by hand.

## Export: chunked formatting must not leap

Export formats records in worker chunks of up to `_FORMAT_CHUNK_RECORDS = 4000`
that arrive at the parent whole, so counting in *chunks* makes the bar leap by
thousands of records at a time.

The parent builds those chunks itself, so it already knows each one's record
count — no change to the worker's return type is needed. `_write_format_results`
counts in **records** (total = sum of every type's record count) and walks the
bar up in fixed 25-record steps as each chunk lands, cumulatively across chunks
and types. Those emissions are `force=True`: throttled away, they would
reintroduce the leap.

## Where each label is emitted

Every one of these sits in the PARENT's aggregator loop.

| Label | Code |
|---|---|
| `Export` | `tes4_export/export.py` `_write_format_results` |
| `Extract` | `asset_convert/bsa_extract.py` `extract_bsas`, one step per BSA |
| `Meshes` | `asset_convert/nif_converter.py` `_batch_status` (both pool and serial) |
| `SpeedTrees` | `asset_convert/spt_converter.py` `_consume_spt` |
| `Creatures` | `asset_convert/creature_pipeline.py` `_collect_creatures` |
| `Voices` | `asset_convert/audio_converter.py` `_tally_voices` |
| `Sounds` | `asset_convert/audio_converter.py` `_tally_sounds` |
| `Music` | `asset_convert/music_convert.py` `_tally_music` |
| `Scripts` | `script_convert/pipeline.py`, `track` over the chunked jobs |
| `Records` | `tes5_import/import_main.py`, `track_records` over `work_items` |
| `Landscape` | `tes5_import/import_main.py` `_precompute_lands` |
| `Navmesh` | `tes5_import/import_main.py` `_precompute_navmeshes` |
| `Packing` | `asset_convert/bsa_pack.py` `pack_bsas`, one step per archive |

## Navmesh cache

`_precompute_navmeshes` is one of the six functions in
`NAVMESH_FUNCS` (`tools/navmesh/navmesh_cache_hook.py`), and a module-scope hunk
in `import_main.py` counts as a hit as well. Adding the `Navmesh` `@@PROG`
therefore flags the shared navmesh cache for republish on the next push to
master. It changes no navmesh output.
