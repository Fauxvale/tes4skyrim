# Performance & Parallelism Notes

Linked from [CLAUDE.md](../CLAUDE.md). Measured optimisation results and the
rules that keep the output byte-reproducible.

## Low-core machines: where the time actually goes (measured 2026-08-09)

The pipeline takes ~30 min on a 32-core box. Profiling for 4-8 core machines
found that **the parallel stages are already mature** — the wins were all in
*serial* work and in *per-process-spawn* overhead, both of which a low-core
machine pays in full.

The default worker count is `cpu_total() - 3` (`worker_budget.py`), so a 4-core
machine runs **1 worker**. Anything not parallel therefore dominates there.

### 1. Papyrus compile — one process per script (the biggest low-core win)

`phase_compile` spawned **one `papyrus.exe` per .psc file**: 15,961 processes
for Oblivion.esm, each re-parsing the ~3,000 Skyrim `Data\Source\Scripts`
headers from scratch.

| | measured |
|---|---|
| per-file, serial | 82 ms/script -> **~1,316 s of CPU** |
| per-file @ 4 cores (1 worker) | **~22 minutes** |
| per-file @ 32 cores (29 workers) | ~45 s (why it was invisible here) |
| **batch `-i <dir>`, one process** | 200 scripts in 1.08 s; **2.37 ms/script marginal**, ~40 s for the whole plugin |

`papyrus.exe compile -i` accepts a **directory**. The catch — and the reason
the original code deliberately used per-file — is that **the compiler aborts
the whole batch on the first bad file and writes ZERO .pex** (measured: 1
broken script of 201 -> 0 .pex).

So `phase_compile` is now **batch-first with quarantine-and-retry**: compile the
directory; parse the failing filenames out of the error lines; copy everything
except those into a staging dir; retry. Error-reporting shape (measured):

- **Scanner/parser** errors surface **one file at a time**, each aborting.
- **Checker** (semantic) errors surface for **every** bad file in one pass
  (`failed to compile files, N errors`).
- Every error line is `<path>\Foo.psc:LINE:COL: <Kind> error: <msg>`, so the
  failing file is always recoverable from stdout.

Quarantined scripts are then re-tried individually (a file can be dragged into
a batch failure by a dependency), and if the batch cannot make progress at all
the original per-file path still runs as a fallback. A healthy build spawns
**one** compiler process instead of 15,961.

Measured end-to-end on Oblivion.esm with every `.pex` deleted first:

- **Clean build: 15,961/15,961 succeeded in 10.0 s** (one compiler process).
- **With 2 deliberately-broken scripts injected**: quarantined both over two
  retries, still compiled **15,961 of 15,963**, and reported each failure with
  its exact compiler error. Plain batch mode would have emitted **zero** .pex.

The staging directory is built **once and maintained incrementally** (each
retry only deletes the newly-quarantined file). Re-copying all ~16k scripts per
retry cost 129 s for the two-failure case — far more than the compiles.

### 2. `build_edge_links` — 60-93 s single-threaded (4.25x, byte-identical)

The largest serial block in `--import-only`: 93.2 s cold / 60.3 s warm, versus
**21.7 s** for generating all 8,228 navmeshes across 29 workers.

The cause was **not** algorithmic. Real seams are tiny — mean |A| = 5.7,
median 5, max 33; 262k total inner-loop iterations across every seam — so
`_match_seam`'s O(A x B) greedy pairing is only **1%** of the pass. The cost was
per-element `struct` work:

| | share |
|---|---|
| `NavMeshView.__init__` (per-vertex/per-tri `unpack_from`) | 39% |
| `_border_edges` (Python triangle scan, 24k calls) | 28% |
| `zlib.compress` | 11% |
| `pack()` (per-element `struct.pack`) | 6% |

numpy vectorisation of the decode, the pack, and the border-edge scan gives
**4.25x (35.9 s -> 8.4 s), byte-identical output and an identical link count**
(258,872). Two traps, both of which silently changed the link count:

- **Vertices must stay float64.** The original computed seam midpoints from
  Python floats (doubles). Doing it in float32 shifts midpoints by ~1e-2 units,
  which reorders near-ties in the greedy pairing — **4 extra links**.
- **Keep `verts`/`tris` as numpy arrays; do NOT `.tolist()` them.** `add_link`
  mutates a triangle in place, so a list form has to be re-converted with
  `np.asarray` on every seam scan: 103k calls costing **11.9 s**, more than the
  decode it was meant to save. That version measured only 1.15x. Arrays are
  mutated directly (`self.tris[i]` is a row view) and need no cache at all.
- `np.frombuffer` returns a **read-only** array; the `.astype()` that widens
  i2 -> i4 is what makes it writable (and restores the two unsigned columns
  with `&= 0xFFFF`).

Profile it in isolation without paying for a full import: set
`TESCONV_DUMP_NAVM_CACHE=<path>` on an import run to pickle the precomputed
navmesh cache, then profile `build_edge_links` against it.

### 0. NIF output was NON-DETERMINISTIC (fixed — read this first)

Converting the same source NIF twice produced **different bytes**, with no code
change between runs. It is stable *within* one process and varies *between*
processes — the signature of `PYTHONHASHSEED`. Pinning the seed makes three
separate runs agree exactly.

Cause, in PyFFI's `NifFormat.Data.write` (line ~1462):

```python
self._string_list = list(set(self._string_list))   # ensure unique elements
```

`set` over **bytes**, whose hash is randomised per process (PEP 456). Every
`NiStringRef` stores an *index* into that list, so the whole header string table
and its references shuffle. Measured on `dungeons/chargen/dobrick01.nif`: same
size, same 10 blocks, same geometry — **26 of 7,433 bytes differ**, all in the
string table and the indices into it.

**Patch 10** in `pyffi_monkey_patch.py` makes the dedupe insertion-ordered
(`dict.fromkeys`). It recompiles that one method from PyFFI's own source with
the single line rewritten, and declines to install (loudly) if the source does
not match, so it cannot silently drift from the installed PyFFI.

Why it matters beyond tidiness: **no mesh build was reproducible**, a BSA
differed from the previous one for no reason, and — the reason this surfaced —
every before/after byte-comparison of a mesh optimisation reported a spurious
mismatch, so it could not tell a real regression from seed noise. Any mesh
perf work done before this fix was measured with a broken instrument.

Check it with `python tools/nif_determinism.py` (converts a sample under
several `PYTHONHASHSEED` values and diffs the hashes; non-zero exit on failure).
Verified: 65 meshes across 5 seed pairs, 0 differences.

### 3. Mesh conversion is ~100% PyFFI object-model overhead

Per-mesh cost scales with file size (~95 ms per 100 KB; median 304 ms, worst
~9 s for a 2 MB architecture NIF). The profile is almost entirely PyFFI's
generic XML-driven object model — `struct_.__init__`, `get_basic_attribute`,
`getattr`, `_get_filtered_attribute_list` — **not** our conversion code. The
stage already runs one process per core, so the only lever is per-mesh CPU.

**Patch 9** (`asset_convert/pyffi_monkey_patch.py`): `StructBase._log_struct`
-> no-op. PyFFI calls it for every attribute of every struct on **both** read
and write, doing a `getattr`, an `isinstance`, a `get_value()` and a six-operand
`str.format()` before the logger discards the record. Nothing consumes it — the
pipeline's `_PyFFICapture` handler attaches at WARNING+. Measured 1,517,371
calls / 4.44 s cumulative on two heavy NIFs. **1.09x, byte-identical**; the
patch is skipped if DEBUG really is enabled for `pyffi.nif.data.struct`.

**Patch 11**: `NiTriBasedGeom.update_tangent_space` reimplemented in numpy. It
became the hottest single function once the logging patch was in (4.34 s
cumulative of ~11 s across 12 meshes, in only **57 calls**) because it runs a
per-**triangle** Python loop allocating several `Vector3` objects each, then a
per-**vertex** Gram-Schmidt loop. The numpy version reproduces the algorithm
exactly — the same quantised `(vertex, normal)` merge hash (so uv seams still
share a frame), degenerate-triangle skipping, per-triangle normalisation
*before* accumulation, the `r_sign` factor, the Gram-Schmidt order, and the
`x cross n` / `y cross n` fallback basis. Anything it cannot handle (no uvs, no
normals, no triangles, length mismatch) falls through to the original.

**1.39x on its own; 1.85x for the whole mesh stage with Patches 9+11**
(15.27 s -> 8.25 s on a 24-mesh sample). Used by the main mesh path (via
`SpellAddTangentSpace`), `lod_far_gen` and `spt_converter`, so all three gain.

Output is *not* byte-identical here, and that is expected: the original
accumulated in float32 `Vector3`s, this accumulates in float64. Measured
divergence across sample meshes is **1e-16 to 1e-5 per component** — rounding,
not a different answer. Toggle with `TESCONV_PYFFI_NO_FAST_TANGENTS=1` to A/B.

Still on the table, not done (riskier — it touches conversion correctness):
`NiTriBasedGeom.get_interchangeable_tri_shape` does a **double deepcopy** of all
geometry (verts, normals, UVs, colours) purely to change the container type —
8.4 s cumulative on two meshes.

**NOT DONE, and do not retry blindly:** caching `_get_filtered_attribute_list`
(1.98M calls, the obvious next target). Memoising it per
`(class, version, user_version)` — even restricted to classes where no attribute
carries a `cond` — **changed the output of 7 of 30 sample meshes**. The
filtering is not instance-independent: `arg`/`vercond` can reference instance
fields, and PyFFI mutates instance state *while* walking the list during read,
so a later attribute's inclusion can depend on an earlier one's just-read value.

### 4. Terrain LOD: the Python side is not the bottleneck

Object LOD (786 `_far.nif` across 29 workers) and the terrain tile pool are both
properly parallel. Two findings:

- **`LODGenx64.exe` dominates the stage.** It had burned **493 CPU-seconds**
  when sampled, running **6.4 cores across 122 threads** on 180,575 references.
  It is a third-party binary, already parallel — not our lever.
- **The serial LAND parse runs once PER WORLDSPACE**, and Oblivion.esm ships
  **18** of them, so the same ESM is re-scanned ~18 times before each
  worldspace's tile pool starts. Tamriel alone has **14,686 LAND records**.
  Two vectorisations took that parse from **5.9 s to 2.9 s (2.0x)**, i.e. about
  **54 s off the serial part of the stage**:
  - `_decode_land` (VHGT) ran a **33x33 nested Python loop per LAND record**.
    Replaced with integer prefix sums. The deltas are int8, so an integer
    `cumsum` is **exact** — there is no accumulation-order rounding to
    reproduce, unlike a float cumsum, which could not have matched the scalar
    loop's mixed float32/Python-float accumulator anyway. Worst height
    difference vs the old code: **0.002 game units (0.0014 cm)**.
  - `decode_land_layers` (VTXT) did one `struct.unpack_from` **per opacity
    entry** — 14.7M calls across Tamriel, since one layer carries up to
    17x17 = 289 entries per quadrant. A structured-dtype `np.frombuffer` reads
    each array in one go: **1.4x**, and verified **identical on 4,000 real LAND
    records** (`temp/vtxt_verify.py` pattern).
- Do NOT bother caching the plugin bytes across worldspaces: measured, the
  613 MB read is **0.10 s** (OS page cache) against a 2.9 s scan. Tried and
  reverted.

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

## FormID determinism — the save-game contract (audited 2026-08-09)

A save game stores **FormIDs**. If a rebuild gives a generated record a
different id, every save silently rebinds that object to whatever now holds the
id — so **generated FormIDs must be identical run to run, and must not move
when unrelated code changes.**

`PluginWriter.alloc_formid()` (`tes5_import/writer.py:252`) is a bare `+1`
counter. It carries no identity: an id is decided **purely by the position of
its allocation call in the global sequence**. Two consequences:

1. Same call sequence → same ids. Verified byte-for-byte.
2. **Insert or remove ONE earlier allocation and every later id shifts by one.**

### Audit result: currently deterministic (verified, not assumed)

Every `alloc_formid()` call site is reached through `sorted()` or list-order
iteration; there is no builtin `hash()` anywhere in the pipeline (only hashlib);
every `os.listdir`/`glob` in `tes5_import` is sorted; pools use `ex.map`
(submission order) and navmesh ids are pre-allocated serially *before* dispatch.
The allocator base, `max_formid + 0x1000` (`import_main.py:761-771`), is a
`max()` over all records — order-independent.

End-to-end proof: built each plugin twice under different `PYTHONHASHSEED`
values (`1` vs `424242`/`555555`/`99999`) — **byte-for-byte identical**:

| Plugin | Size | Path exercised |
|---|---|---|
| `Oblivion.esm` | 613,807,139 B | full record set, navmesh + LAND pools |
| `Morrowind_ob.esm` | 206,162,203 B | **masters** — overrides + injected records |
| `DLCBattlehornCastle.esp` | 7,899 B | small dependent plugin |

Guarded by `tests/test_formid_determinism.py` (static AST guards, verified to
fire on a deliberate canary — they are not vacuous).

### <a id="formid-fragility-map"></a>Where a code change WILL renumber FormIDs

These are ordering-sensitive by design. Editing them is legal, but it **breaks
existing saves**, so it belongs in a deliberate "saves reset" change, not a
drive-by refactor.

**A. Anything that adds/removes/reorders an allocation.** All ~50 sites shift
everything allocated after them:

| File | Generates |
|---|---|
| `record_types/equipment.py` (324, 447, 624, 804) | weapon STAT, **ARMA**, **PROJ**, book INAM |
| `record_types/actors.py` (273, 869, 929, 991, 1017, 1118, 1133) | **OTFT**, origin/vendor/trainer FACT, FLST, CLAS clone |
| `record_types/magic.py` (926, 1017, 1168) | AV / SEFF / bound-script MGEF variants |
| `record_types/dialog_misc.py` (121, 314) | SOPM, **SNDR** |
| `record_types/world.py` (106) | LTEX **TXST** |
| `creature_races.py` (232, 304, 728, 740, 922-923) | creature VTYP, BPTD, MOVT, skin ARMA, RACE |
| `creature_footsteps.py` (107-132) | IPCT / IPDS / FSTP / FSTS |
| `creature_idles.py` (112) | creature IDLE tree |
| `dialog_converter.py` (1847, 1884, 2006, 2434, 2617, 2924, 3013) | DIAL / INFO / **DLBR** / **DLVW** |
| `dialog_unlocks.py` (405) | unlock **GLOB** per gated topic |
| `locations.py` (189, 279) | **LCTN** |
| `magic_effects.py` (126) | aimed-MGEF variants |
| `leveled_actors.py` (205) | leveled-actor shells |
| `navm_split.py` (230), `pgrd_to_navm.py` (1058) | **NAVM** |
| `overrides.py` (166) | injected-record redirects |
| `import_main.py` (324-497) | fame/infamy/fenced/crime GLOBs, GMSTs |

**`import_main.py:1626` is a deliberate no-op allocation — never "clean it up".**
It burns one id where the old freshly-allocated NAVI FormID used to sit. NAVI is
now the fixed singleton `0x00012FB4`, so the alloc is functionally dead — but
thousands of generated DIAL/INFO/DLBR/DLVW/LCTN/SNDR records are allocated after
it, and removing it shifts every one of them by one relative to shipped builds,
scrambling any save's dialogue/Papyrus state. It is the clearest example of rule
A: an allocation's *existence* is load-bearing even when its *result* is unused.

**B. Phase order in `import_main.py`.** Phase 1 is a serial loop
(`import_main.py:1267`, "Serial on purpose") *specifically* to keep allocation
order stable — the comment there records that a thread pool once shuffled
companion FormIDs. Reordering phases, moving a `build_*` call, or making Phase 1
concurrent renumbers everything. The creature voice/footstep/BPTD builders are
allocated **last on purpose** (`import_main.py:1731` onward) so adding one
cannot move any earlier id — **put new generators there.**

**C. The sorted() calls that look redundant.** The `sorted()` wrapping a set at
`creature_races.py:231` / `:276`, `dialog_unlocks.py:404`, `magic.py:917` /
`:1011`, and the sorted-key loops in `locations.py` / `navm_split.py`, are all
load-bearing. "Simplifying" one to a bare set
reintroduces `PYTHONHASHSEED` dependence — the ids then differ **between runs on
the same machine**, the worst form of this bug.

**D. Conversion-order inputs.** `text_reader.parse_export_directory`
(`sorted(os.listdir)` + `ex.map`) fixes record order; `by_type[sig]` lists
inherit it. Changing export file naming, dedup (keep-last), or pool result
assembly reorders records and therefore ids.

**E. The allocator base.** `max_formid + 0x1000` — a new record with a higher
TES4 id, or changing the `0x1000` gap, moves the whole generated range.

### If a change WILL shift ids: tell the user, up front

Drift is the user's call, not a detail to absorb — the cost lands on players,
not on the build. Prefer the non-drifting route (add at the END, reuse an
existing record, leave the allocation alone). If drift is genuinely unavoidable,
finish the work, then **lead the final report with it**:

1. Say it explicitly and up front — never buried at the bottom, never omitted
   because the change is otherwise correct.
2. State the blast radius: which record types renumber, and roughly how many.
3. Say why it was unavoidable, and which non-drifting alternative you rejected.
4. Never accept drift for a refactor, tidy-up or "simplification". If the only
   benefit is cleanliness, the answer is no.

This is a reporting duty, not a licence to stop mid-task.

**Before shipping a change in these areas**, rebuild and diff with
`tools/esm_diff.py old.esm new.esm` (it separates real diffs from reorders). To
check pure reproducibility, build twice with different `PYTHONHASHSEED` and
`cmp` the output — it must be byte-identical.

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
`python tools/navmesh/perf.py --cells <A,B> --stages` (its stage timers now
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
  Corridor geometry is verified against real cells by `tools/navmesh/check.py`,
  `navmesh_reach.py`, `navmesh_slope_check.py`.

## Measured throughput

- Export: ~8s to parse 1.17M records from Oblivion.esm, ~36s total with write.
- Import: ~28K records from Oblivion.esm, 413 MB output.
- NIF conversion: 8032 source NIFs; 7380 v20 converted (91.9%), 650 v10/v4
  copied as-is.
