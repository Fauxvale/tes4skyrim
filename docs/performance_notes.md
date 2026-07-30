# Performance & Parallelism Notes

Linked from [CLAUDE.md](../CLAUDE.md). Measured optimisation results and the
rules that keep the output byte-reproducible.

## Parallelism rules (learned 2026-07-16)

- **ThreadPoolExecutor is ONLY for I/O or subprocess work** (file reads,
  papyrus.exe, xWMAEncode). Pure-Python record conversion/parsing/formatting
  holds the GIL — threads pin one core AND (when converters allocate companion
  FormIDs) make output nondeterministic. Use ProcessPoolExecutor.
- **Worker state replay pattern**: converter functions depend on module globals
  set in Phase 0 (formid offset, cell locations, WORLD_NAMES, furniture origin
  shifts, mesh bounds). Process pools must replay them via an initializer — see
  `tes5_import/navm_worker.py` and `tes5_import/convert_worker.py`.
- **Determinism contract**: the output ESM must be byte-reproducible. Process
  results in submission order (`ex.map`, not `as_completed`) and keep any
  `writer.alloc_formid()` callers serial. Verify with `tools/esm_diff.py A.esm
  B.esm` (distinguishes real diffs from reorders).
- **Export format workers re-read from mmap**: `tes4_export` scans record
  offsets only (`read_file(..., parse_subs=False)`) and workers re-read/format
  from their own mmap — never pickle `Record` objects across process boundaries.
- **`unescape_value` fast path matters**: a `'\\' not in value` check made text
  parsing ~7x faster; keep C-speed scans in per-line hot paths.
- **Don't parallelize µs-level converters** (REFR/ACHR/CELL): the pickle
  round-trip costs more than the conversion. Only LAND (~0.9 ms/record) is worth
  a pool.
- **`bytes += big` is quadratic** — accumulate group contents in lists and
  `b''.join` at wrap points (CELL/WRLD builders).
- **Pool tools can exhaust memory**: some load the ~2.1 GB export index PER
  WORKER. Cap `--workers` or run single-process for those.

## Process containment — orphaned workers (learned 2026-07-29)

`process_job.py` puts the pipeline in a **Windows Job Object**. Two properties,
one mechanism:

- `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` — when the parent dies the kernel kills
  every process in the job. This covers the cases no Python cleanup can: a
  crash, Task Manager "End task", the console window closed. The GUI's
  `_kill_process_tree` only ever covered the deliberate Cancel button.
- `JobMemoryLimit` — a committed-memory ceiling across the whole job.
  **OFF by default**; opt in with `TESCONV_JOB_MEM_GB=<gb>`. See the measured
  trap below before enabling it.

**A job-wide memory cap divides by the worker count — do not "just set one".**
Measured 2026-07-29: a ceiling of available-RAM-minus-4GB (13.1 GB) sounded
generous but was shared across the import stage's **29 workers**, leaving
~0.45 GB each. A previously-healthy `--import-only` then failed 48 furniture
marker NIF reads. Two things made this nasty to diagnose:

- The failures surfaced as a mix of `MemoryError` **and a misleading numpy
  `ImportError`** ("you should not try to import numpy from its source
  directory"). numpy misreports an allocation failure during import as a
  source-tree problem — it is not a `sys.path`/CWD bug, it is OOM.
- `furniture_model_info_job` catches broad `Exception`, so each failure became a
  per-file "failed to read" line and the stage **silently degraded** to 37/85
  markers while still exiting 0.

A cap safe for 29 workers must sit near total RAM, where it protects nothing the
OS would not. Hence: off by default, and orphan containment does not depend on
it. Verified with the cap off: 0 MemoryErrors, all 85 markers resolved.

**Why orphans were expensive:** `subprocess_flags.configure_multiprocessing()`
points `multiprocessing` at `pythonw.exe`, so leaked workers are console-less
and near-invisible in Task Manager. They kept the multi-GB export index resident
and held handles on `output/`, so the *next* run had less RAM and hit file
permission errors — which is what made a reboot look necessary. It never was:
`taskkill /F /IM pythonw.exe` reclaimed it.

### Measured contracts (verified 2026-07-29, Win11 / Python 3.14)

- **A worker MUST close its own handle to the job after assigning itself.**
  `KILL_ON_JOB_CLOSE` fires only when the *last* handle closes. A worker holding
  one keeps the job — and every process in it — alive after the parent dies.
  Measured both ways: handle leaked → orphans survive a parent kill; handle
  closed → terminated immediately. This is the whole mechanism.
- **Job membership is inherited automatically.** A process spawned by a job
  member joins the same job, so `create_pool_job()` in `convert.py` covers all
  ~25 pool sites *and* the helper .exes (ffmpeg, hkxcmd, BSArch, LODGen) with no
  per-site changes. Verified with a pool that has no initializer at all. The
  explicit `join_pool_job()` calls in the initializers cover only the case of a
  worker whose parent chain was re-parented.
- **Nested jobs work.** GUI job → `convert.py` job → workers: killing the GUI
  still takes the entire tree down (Win8+ allows nesting).
- **`AssignProcessToJobObject` rejects the `GetCurrentProcess()` pseudo-handle**
  with ERROR_INVALID_HANDLE; a real handle from `OpenProcess` is required.
- **Handle argtypes must be pointer-sized.** ctypes defaults to `c_int`, which
  truncates handles on x64 — assignment then *reports success* against a garbage
  handle and silently contains nothing.
- The memory cap surfaces in a worker as a normal catchable `MemoryError`
  (measured: 960 MB allocated against a 1 GB cap), and the parent survives with
  the pool intact — not a `BrokenProcessPool`.

### BrokenProcessPool causes

It is always a symptom: a worker died without returning a result. Known causes
here, in the order worth checking:

1. **Orphan/kill timing** — fixed by the job object above.
2. **An initializer that raises.** An exception inside `initializer=` cannot be
   returned to the parent, so it surfaces as an opaque `BrokenProcessPool` with
   no traceback — and the worker's stderr is invisible when multiprocessing runs
   `pythonw.exe`. `asset_convert/book_inam.py` and
   `import_main._precompute_navmeshes` both guard this by running the
   initializer once in the parent first.
3. **A C++ exception escaping a native extension** (verified 2026-07-29, this is
   what broke the Nehrim import). See the section below.
4. **RAM exhaustion.** Historically real (see the `navm_worker` docstring: a
   heavy worker module cost several GB of RSS per child), which is why that
   module is deliberately light.

### A native extension that aborts (Nehrim import, fixed 2026-07-29)

**Symptom.** `--import-only` on Nehrim died at "Generating 2929 navmeshes" with a
bare `BrokenProcessPool` and no traceback anywhere. It reproduced at **4**
workers as readily as 29 and failed after **0** results, which rules out memory
pressure and worker count — the giveaway that one specific job kills its worker.

**How to find the culprit fast.** Do not bisect the pool. Run the jobs
*in-process* in dispatch order, flushing each job's identity BEFORE the call
(`temp/navm_find_killer.py`); a hard crash then leaves the culprit as the last
line printed. That named `job[27]`, cell `011E4FEC`, and — because the abort
happened in-process — Python printed the C-level frame:
`corridor_grow.py:93 in grow_batch` → `_navgrow_native`.

**Root cause, in two layers.**

- *The data.* Nehrim ships REFRs with uninitialised placement floats: 17 records
  across 10 base objects carry `PosY = 8.936455989415117e+17` (with
  `PosX = 1.68e-36`), e.g. REFR `001E57C4` in cell `001E4FEC`. Oblivion.esm has
  **zero** such records, which is why only Nehrim hit this. These are real
  exported values, not an export bug.
- *The code.* `TriGrid::build` in `native/src/grow.cpp` buckets triangles into a
  **dense** `nx*ny` array sized from the soup's XY *extent*, with no upper bound.
  One outlier vertex put the extent at 8.9e17 units → 5.4e14 buckets → a
  **4-billion-GB** allocation. The `std::bad_alloc` was thrown inside
  `Py_BEGIN_ALLOW_THREADS` with **no `try`/`catch` anywhere in the extension**, so
  it reached `std::terminate()` and the worker died by `abort()` (exit code 3).

**Why it was invisible.** Three failures compounded:
- A C++ throw with the GIL released cannot become a Python error, so there was no
  traceback to return.
- `navm_worker.run_job` caught per-cell errors and `print`ed them — but workers
  run under `pythonw.exe`, where stdout goes nowhere.
- The parent only counted results, so it never reported which cells produced no
  navmesh.

**The fixes (all four are load-bearing).**
- `grow.cpp`: reject non-finite coordinates, bound the grid at `kMaxBuckets`
  (16M; a 4096-unit exterior cell is 33x33 = 1,089, the largest real interior
  measured 77x83), widen the CSR accumulator to 64-bit (`int acc` could overflow
  negative and then `(size_t)acc` allocated huge and wrote out of bounds), and
  wrap **both** entry points' GIL-released blocks in `try`/`catch` that stashes
  the message and raises it as a Python `ValueError` after reacquiring the GIL.
  The same unbounded-bucket pattern existed in `levels_at`'s strip grid and
  `NeighbourField::build` (sparse maps, so no dense allocation trips first — they
  just grow until memory runs out); both are now bounded too.
- `navmesh/world.py`: drop refs whose placement is not finite or exceeds
  `_MAX_PLACEMENT` (1e7, ~50x the whole map) before any geometry is placed. Also
  guarded in `pgrd_to_navm._collect_doors` and `navmesh/build.py`
  (`float()` happily parses `nan` and `8.9e17`). The ref itself still converts
  normally — only its navmesh collision is skipped, and it is nowhere near the
  pathgrid anyway.
- `navm_worker.run_job`: **return** the error instead of printing it, and
  `_precompute_navmeshes`: print the returned failures in the parent.

**That reporting change immediately paid for itself.** With failures finally
visible, the next run surfaced two cells that had been silently shipping with no
navmesh at all: `012217C1` and `01193F44`, both `IndexError` in
`corridor_union._storey_groups`. `group_polys` is built once from `out`, but an
unmatched door strip does `out.append([s])` — so the next door's
`group_polys[gi]` ran off the end. `group_polys` now grows in step (and is
refreshed when a group gains a strip, since its cached footprint goes stale).
Both cells now build (8 KB and 80 KB of navmesh).

**Result:** 2,929/2,929 navmeshes, 0 failures, and the full Nehrim import
completes — 26,865 records, 197 MB.

## Navmesh generation (learned 2026-07-25)

Corridor navmesh generation was **9.8x** faster after four fixes, all
**byte-identical output** (13 cells A/B'd on (verts, tris) hashes; large
interiors 11-14x, small cells 1.3-2.5x). Profile with
`python tools/navmesh_profile.py --cells <A,B> --stages` (its stage timers now
wrap the CORRIDOR path; they used to wrap the deleted voxel/region/spanmesh and
so reported everything as "(other)"). Sub-stage rows are INDENTED because they
nest inside `build_union_mesh` — only top-level rows may be subtracted, or
"(other)" goes negative.

- **Never recompute a whole-mesh property inside a per-node loop.**
  `_merge_at_pathgrid_nodes` called `_tri_components` (full-mesh union-find) once
  per pathgrid node — 831 x ~4000 tris, ~60% of a large cell. It is only stale
  after an ACTUAL weld, and most nodes weld nothing, so memoise it and clear the
  memo only when a weld happens (61% -> 0.5% of build time).
- **All-pairs shapely predicates are the default hotspot.** `pa.intersects(pb)`
  over N ribbons was 7.4M scalar calls (~33%). `shapely.STRtree(polys)` +
  `tree.query(polys, predicate='intersects')` does the same box filter in bulk C.
  Same in `_same_surface_region`. **Sort the candidate pairs** — the union-find
  that consumes them must see the old nested-loop order or output shifts.
- **Memoise `_ribbon_polygon`** (pure function of the strip, called 379,250x for
  a few thousand strips; the invalid-outline buffer/union repair re-ran every
  time). Keyed on `id(strip)` while holding a reference to the strip, cleared per
  `build_union_mesh` so a worker converting thousands of cells cannot leak.
- **Set-membership beats rebuilding tuples in a loop:**
  `any((min(i,j),max(i,j)) in cset for j in sub)` over sub-sheet members was
  17.8M min/max calls; per-ribbon adjacency sets + `set.isdisjoint` replace it.
- **Batch scalar shapely into vectorised calls only where the work is Python
  overhead, not GEOS.** 9 grid samples per `_overlap_height_gap` built 274k
  `Point` objects; `shapely.points(list)` + `shapely.intersects` in one call won.
  But batching the pairwise `intersection`/`area` was **SLOWER** (17.0 -> 17.9s):
  GEOS clipping dominates and the bulk form materialises an intersection for
  every candidate instead of discarding most on a cheap area test. Measure.
- **C++ is NOT warranted here (measured).** After the above, 46% of remaining
  tottime is inside shapely/GEOS (robust boolean ops + triangulation) and only
  30% is our Python; a perfect union rewrite is Amdahl-capped at 5.3x and would
  mean reimplementing GEOS against a byte-exact output contract. `grow.cpp`
  (`_navgrow_native`) stays. `decimate.cpp`/`_navmesh_native` was DELETED — it
  served only spanmesh.
- **DELETED with the old generator:** `navmesh/voxel.py`, `region.py`,
  `spanmesh.py`, `native/src/decimate.cpp`, and their tests/fixtures in
  `tests/test_pgrd_navm.py` (which asserted collision-discovery rules the
  corridor model does not have — it derives the mesh from the pathgrid).
  Corridor geometry is verified against real cells by `tools/navmesh_check.py`,
  `navmesh_reach.py`, `navmesh_slope_check.py`.

## Measured throughput

- Export: ~8s to parse 1.17M records from Oblivion.esm, ~36s total with write.
- Import: ~28K records from Oblivion.esm, 413 MB output.
- NIF conversion: 8032 source NIFs; 7380 v20 converted (91.9%), 650 v10/v4
  copied as-is.
