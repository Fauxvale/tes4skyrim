"""Profile the CORRIDOR navmesh build: which stage and which function is hot?

The older tools/navmesh_profile.py wraps voxel/region/spanmesh, which the
pathgrid-seeded corridor build no longer calls -- it reports 100% "(other)".
This one wraps the stages build_corridors actually runs:

    geometry   world.gather_cell_geometry (NIF collision soup assembly)
    samplers   _surface_sampler / wall_slab_sampler / walkable_sampler /
               NeighbourField construction
    strips     _build_corridor_strips (per-edge ribbon + width grow)
    doors      corridor_doors.door_footprints (+ its probe union)
    union      corridor_union.build_union_mesh (shapely boolean + triangulate)
    finalize   corridor_clean.finalize (decimate / island prune / validate)

STAGE view decides what is worth optimising (Amdahl); FUNC view (cProfile)
finds the kernel inside the indicted stage.  Runs serially -- fractions carry
to the parallel run because every worker runs this same code.

    python tools/navmesh_corridor_profile.py --cell Wendir02
    python tools/navmesh_corridor_profile.py --cells A,B,grid:12:-8 --top 30
    python tools/navmesh_corridor_profile.py --cells A,B --no-func
"""

import argparse
import cProfile
import os
import pstats
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asset_convert import collision_extract as ce  # noqa: E402
from tes5_import.navmesh import (  # noqa: E402
    corridor, corridor_clean, corridor_doors, corridor_grow, corridor_union,
    world,
)
from tools.navmesh_probe import load_cell  # noqa: E402

_ACC = {}
_LIVE = []          # stack of (stage_name, start_time) currently executing


def _timed(name, fn):
    """Wrap fn so its inclusive wall-clock accumulates under `name`.

    The live stack is maintained so a run that gets KILLED mid-stage can still
    say which stage it died in -- with a build this slow, "stuck in union" is
    more useful than a complete table we never get to see.
    """
    def wrapper(*a, **kw):
        t0 = time.perf_counter()
        _LIVE.append((name, t0))
        try:
            return fn(*a, **kw)
        finally:
            _LIVE.pop()
            _ACC[name] = _ACC.get(name, 0.0) + (time.perf_counter() - t0)
            print('    [stage] %-10s +%.2fs' % (name, time.perf_counter() - t0),
                  flush=True)
    return wrapper


def _patch_stages():
    """Wrap each stage where build_corridors looks it up.

    corridor.py does `from . import corridor_grow, world` and imports the
    union/doors/clean modules INSIDE build_corridors, so patching the module
    attribute is what the call actually resolves through in every case.
    """
    world.gather_cell_geometry = _timed('geometry', world.gather_cell_geometry)

    corridor._surface_sampler = _timed('samplers', corridor._surface_sampler)
    corridor_grow.wall_slab_sampler = _timed(
        'samplers', corridor_grow.wall_slab_sampler)

    corridor._build_corridor_strips = _timed(
        'strips', corridor._build_corridor_strips)
    # Inside `strips`: the native march vs the Python planning/reassembly.
    corridor_grow.grow_batch = _timed('  grow(native)', corridor_grow.grow_batch)
    corridor._plan_stations = _timed('  plan', corridor._plan_stations)
    corridor_doors.door_footprints = _timed(
        'doors', corridor_doors.door_footprints)
    corridor_union.build_union_mesh = _timed(
        'union', corridor_union.build_union_mesh)
    corridor_clean.finalize = _timed('finalize', corridor_clean.finalize)


def _build_one(cell):
    """Run the real build for one loaded cell; returns (verts, tris)."""
    from tes5_import.navmesh import build

    # Exterior cells build in world space with the cell's SW corner as origin;
    # interiors are already world-space and use (0, 0).
    ox = cell['grid_x'] * 4096.0 if cell['is_exterior'] else 0.0
    oy = cell['grid_y'] * 4096.0 if cell['is_exterior'] else 0.0
    return build.build_navmesh(
        cell['refrs'], cell['base_model'], ce.get_collision,
        cell['nodes'], cell['edges'], land_rec=cell.get('land'),
        origin_x=ox, origin_y=oy, doors=cell['doors'])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--export', default='export/Oblivion.esm')
    ap.add_argument('--cell')
    ap.add_argument('--cells', help='comma-separated list')
    ap.add_argument('--top', type=int, default=25)
    ap.add_argument('--no-func', action='store_true',
                    help='stage timings only (cProfile adds ~2x overhead)')
    ap.add_argument('--out', default='temp/navmesh_corridor_profile.txt',
                    help='append per-cell stage timings here as they finish')
    args = ap.parse_args()
    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)

    names = []
    if args.cell:
        names.append(args.cell)
    if args.cells:
        names += [c for c in args.cells.split(',') if c]
    if not names:
        ap.error('need --cell or --cells')

    _patch_stages()

    prof = cProfile.Profile() if not args.no_func else None
    totals = {}

    # Load and build ONE cell at a time, printing as we go.  Loading is NOT
    # part of the pipeline cost being measured -- load_cell rescans the whole
    # ~1M-record REFR list per cell, which the real run does once in
    # _gather_navm_jobs -- so it is timed separately and never profiled.
    for n in names:
        t0 = time.perf_counter()
        c = load_cell(args.export, n)
        load_s = time.perf_counter() - t0
        print('%-28s loaded %.2fs (%d nodes, %d edges, %d refrs) ... building'
              % (n, load_s, len(c['nodes']), len(c['edges']), len(c['refrs'])),
              flush=True)

        _ACC.clear()
        t0 = time.perf_counter()
        if prof:
            prof.enable()
        try:
            verts, tris = _build_one(c)
        finally:
            if prof:
                prof.disable()
            # Dump stage timings even if the build raised or the run is about
            # to be killed: an interrupted profile still answers "which stage
            # was it stuck in", which is the whole point of running it.
            el = time.perf_counter() - t0
            with open(args.out, 'a') as fh:
                fh.write('%s elapsed=%.3f %s\n'
                         % (n, el, ' '.join('%s=%.3f' % kv
                                            for kv in sorted(_ACC.items()))))
        totals[n] = (el, dict(_ACC), len(verts), len(tris))
        print('%-28s BUILD %.2fs -> %d verts / %d tris'
              % (n, el, len(verts), len(tris)), flush=True)

    print('\n=== STAGE (wall-clock, inclusive) ===')
    grand = {}
    for n, (el, acc, nv, nt) in totals.items():
        print('\n%s   %.2fs total  -> %d verts / %d tris' % (n, el, nv, nt))
        acc = dict(acc)
        # Names indented with two spaces are NESTED inside another stage, so
        # they must not count toward the top-level sum or "(other)" goes
        # negative and the percentages stop meaning anything.
        top = sum(v for k, v in acc.items() if not k.startswith('  '))
        acc['(other)'] = max(0.0, el - top)
        for k, v in sorted(acc.items(), key=lambda kv: -kv[1]):
            print('    %-10s %7.3fs  %5.1f%%' % (k, v, 100.0 * v / el if el else 0))
            grand[k] = grand.get(k, 0.0) + v

    gt = sum(grand.values())
    print('\n--- ALL CELLS (%.2fs) ---' % gt)
    for k, v in sorted(grand.items(), key=lambda kv: -kv[1]):
        print('    %-10s %7.3fs  %5.1f%%' % (k, v, 100.0 * v / gt if gt else 0))

    if prof:
        print('\n=== FUNC (cProfile, cumulative) ===')
        pstats.Stats(prof).sort_stats('cumulative').print_stats(args.top)
        print('\n=== FUNC (tottime) ===')
        pstats.Stats(prof).sort_stats('tottime').print_stats(args.top)


if __name__ == '__main__':
    main()
