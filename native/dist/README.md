# Prebuilt navmesh extensions

Two compiled modules, both **committed on purpose** — the navmesh build
requires them and most machines running the conversion have no C++ compiler:

| Module | Source | Used by |
|---|---|---|
| `_navmesh_native.<abi>.pyd` | `../src/decimate.cpp` | `navmesh.spanmesh` — mesh decimation |
| `_navgrow_native.<abi>.pyd` | `../src/grow.cpp` | `navmesh.corridor_grow` (Phase-2 width march) and `navmesh.corridor_union` (per-vertex surface levels) |

`grow.cpp` exists because those two kernels dominated generation: a single
dense interior cell (Wendir02, 938 edges) spent ~150s in the width march alone
(~890k wall probes at ~170us each) and 29.3s of a 31.9s build in the level
lookup. Both are batched so the Python/C boundary is crossed once per CELL
rather than once per probe, which took that cell to ~2.6s.

Only the `.pyd` belongs here. The `.lib` / `.exp` MSVC emits are link-time
artifacts for callers that link against the DLL — nothing does — so they are
written to `../build/` and gitignored.

## The ABI tag matters

The filename carries a Python ABI tag (`cp314-win_amd64` = CPython 3.14,
64-bit Windows). A `.pyd` is only importable by a matching interpreter, so the
committed binary works for whoever runs the same Python version and platform.
On anything else the loader raises with the expected filename and what it found
instead — rebuild with:

    python native/build.py

That needs "Build Tools for Visual Studio" with the C++ workload (a full Visual
Studio install is not required); the build script locates MSVC through
`vswhere`.

## Why it is not optional

The extensions are imported unconditionally. A silent fallback to a Python
implementation would make navmesh output depend on whether a build artifact
happened to be present, and the pipeline's output must be byte-reproducible.

The native kernels are verified against the Python originals they replaced —
`python tools/navmesh_grow_verify.py` marches both implementations over
synthetic geometry chosen to exercise every stop rule (wall + bisect, floor
edge, neighbour cap, hard cap) and reports the worst disagreement.
