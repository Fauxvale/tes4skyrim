"""Process-pool worker for parallel PGRD→NAVM navmesh generation.

This is a DELIBERATELY light module.  On Windows the process pool uses `spawn`,
so every worker re-imports the worker's module at start-up.  Importing the full
`import_main` (which pulls in the dialogue/creature/script/asset pipelines) in
each child cost several GB of RSS per process and exhausted RAM, killing workers
with BrokenProcessPool.  Depending only on `pgrd_to_navm` (which itself imports
just text_reader/writer, and loads scipy/shapely lazily) keeps each child small.

FormIDs are pre-assigned in the parent (job['navm_fid']) so no PluginWriter — an
unpicklable, shared-state object — has to cross the process boundary.

The Havok collision cache is a module global in `asset_convert.collision_extract`;
a spawned child does NOT inherit the parent's loaded copy, so each worker loads it
once from disk in the pool initializer.  Without this, every cell would voxelize
an empty world and emit no navmesh at all.
"""

from .pgrd_to_navm import cached_geometry, convert_PGRD, geom_equal

# Per-worker read-only carving context, populated by _init in each child.
_BASE_MODEL_BY_FID: dict = {}
_DOOR_FIDS: set = set()
_GEOM_CACHE: tuple = None


def init_worker(base_model_by_fid: dict, door_fids: set, collision_cache: str,
                formid_offset: int = 0, geom_cache: tuple = None,
                injected_formids: dict = None, disable_gc: bool = True,
                door_centers_cache: str = None):
    """ProcessPool initializer: stash context; load the collision cache.

    Runs once per worker process.  A spawned child does NOT inherit the parent's
    module-global state, so everything convert_PGRD relies on must be rebuilt
    here in each child:

      - `text_reader._formid_index_offset` — the load-order master-index shift
        get_formid() applies (e.g. +1 for Oblivion.esm behind Skyrim.esm).  If
        this is left at its default 0, every FormID convert_PGRD reads
        (PathingCell ParentCELL/ParentWRLD, door REFR links, ONAM base objects)
        keeps master index 0x00 instead of the plugin's real index.  The engine
        then can't resolve the navmesh's parent cell at load and null-derefs in
        Hook_NavMeshLoad.  MUST be set before any get_formid() call.
      - `collision_extract._COLLISION` — the per-mesh Havok collision soups the
        navmesh is voxelized from.  Without it every cell has no geometry and
        produces no navmesh at all.
    """
    # Join the parent's containment job so this worker cannot outlive a parent
    # that dies without cleanup (crash / external kill). Cheap; no-op off
    # Windows, and skipped harmlessly on the inline single-job path below.
    from process_job import join_pool_job
    join_pool_job()

    global _BASE_MODEL_BY_FID, _DOOR_FIDS, _GEOM_CACHE
    _BASE_MODEL_BY_FID = base_model_by_fid
    _DOOR_FIDS = door_fids
    _GEOM_CACHE = geom_cache
    from .text_reader import set_formid_index_offset, set_injected_formids
    set_formid_index_offset(formid_offset)
    set_injected_formids(injected_formids or {})
    if collision_cache:
        from asset_convert.collision_extract import load_collision
        load_collision(collision_cache, quiet=True)

    # Door panel centroids: the REFR position is the door's hinge, not the
    # doorway; _collect_doors offsets to the panel centre using these.  Module
    # global in pgrd_to_navm, so each spawned worker must load its own copy.
    if door_centers_cache:
        from .pgrd_to_navm import load_door_centroids
        load_door_centroids(door_centers_cache, quiet=True)

    # Generational GC is pure overhead in this worker and costs ~2x wall-clock.
    #
    # Voxelizing one cell allocates a Heightfield of ~22k empty column lists.
    # In isolation that takes 0.001s; inside the worker it took 0.356s, because
    # each burst of container allocations trips a generational collection that
    # then traverses every live object -- and by that point the child holds the
    # collision cache and the cell's records, ~1.16M tracked objects.  The
    # collector walks all of them to find nothing.
    #
    # Safe because this workload creates NO reference cycles: measured over 24
    # consecutive cell builds with the collector off, gc.collect() reported 0
    # uncollectable objects and tracemalloc stayed flat at 12.7 MB.  Refcounting
    # alone reclaims everything, so the collector has nothing to do.
    #
    # Scoped to the worker process on purpose.  import_main also calls this
    # initializer INLINE (single-job runs skip the pool), and the parent goes on
    # to convert every other record type -- so that path passes disable_gc=False
    # to keep the collector on in the parent.
    if disable_gc:
        import gc
        gc.disable()


def _verify_against_cache(job: dict, result: tuple) -> tuple:
    """Rebuild this cell and compare; on a mismatch RETURN THE FRESH BUILD.

    The tag hashes sources, so it cannot see a shapely/GEOS or `.pyd` change.
    This is the only check that catches a cache stale in a way the hash agrees
    with -- and keeping the fresh result means a bad cache costs accuracy in no
    cell at all, not even the ones sampled before the mismatch was known.

    See: docs/commentary/tes5_import_navmesh.md#verifying-a-cache-against-fresh-geometry
    """
    navm_bytes, meta = result
    stored = cached_geometry(_GEOM_CACHE, *job['key'])
    if stored is None:
        return result
    fresh_bytes, fresh_meta = convert_PGRD(
        job['pgrd_rec'],
        land_rec=job['land_rec'],
        cell_rec=job['cell_rec'],
        refr_recs=job['refr_recs'],
        base_model_by_fid=_BASE_MODEL_BY_FID,
        door_fids=_DOOR_FIDS,
        navm_fid=job['navm_fid'],
        geom_cache=None,
        extra_door_refrs=job.get('extra_door_refrs'),
    )
    meta['verified'] = True
    fresh = fresh_meta.get('geometry') if fresh_meta else None
    if fresh is None or geom_equal(stored, fresh):
        return result
    fresh_meta['verified'] = True
    fresh_meta['verify_mismatch'] = True
    return fresh_bytes, fresh_meta


def run_job(job: dict):
    """ProcessPool task: convert one PGRD to (navm_bytes, meta).

    A failing cell must not abort the whole ex.map batch, so exceptions are
    caught -- but the message is RETURNED, not printed: workers run under
    pythonw.exe, where stdout goes nowhere. The parent prints what comes back
    (see import_main._precompute_navmeshes).

    job['verify'] asks this cell to double-build and compare; the PARENT picks
    which cells carry it.
    """
    try:
        navm_bytes, meta = convert_PGRD(
            job['pgrd_rec'],
            land_rec=job['land_rec'],
            cell_rec=job['cell_rec'],
            refr_recs=job['refr_recs'],
            base_model_by_fid=_BASE_MODEL_BY_FID,
            door_fids=_DOOR_FIDS,
            navm_fid=job['navm_fid'],
            geom_cache=_GEOM_CACHE,
            extra_door_refrs=job.get('extra_door_refrs'),
        )
        if job.get('verify') and meta and meta.get('geom_cached'):
            return job['key'], _verify_against_cache(job, (navm_bytes, meta))
        return job['key'], (navm_bytes, meta)
    except Exception as e:  # noqa: BLE001 — must not kill the pool
        import traceback
        return job['key'], (None, {'error': f'{type(e).__name__}: {e}',
                                   'traceback': traceback.format_exc()})
