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
