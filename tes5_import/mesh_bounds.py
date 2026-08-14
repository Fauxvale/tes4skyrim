"""
Axis-aligned bounding boxes (AABB) for converted NIF meshes.

Used by the import pipeline to set accurate OBND values on records instead of
type-based defaults.

This module is the READER half only.  The cache is produced by
`asset_convert.collision_extract.scan_mesh_data`, which computes bounds and
collision from a SINGLE NIF parse — parsing dominates both analyses, so the
bounds scan used to re-read every mesh the collision scan had just read.  See
that function for the details.

    scan_mesh_data(mesh_dir, collision_cache, bounds_cache)  — after mesh
                                                               conversion
    load_mesh_bounds(cache_path)                             — import_main.py

Path keys are normalised: lowercase, forward slashes, relative to the mesh
output directory root.  Example: "tes4/furniture/chairnoble01.nif".

Records store raw TES4 model paths like "Furniture\\ChairNoble01.NIF"; after
_prefix_path() and normalisation these map to the same key.
"""

import json
import os
from typing import Dict, Optional, Tuple

OBNDTuple = Tuple[int, int, int, int, int, int]

# Module-level caches populated by load_mesh_bounds().
_MESH_BOUNDS: Dict[str, OBNDTuple] = {}
# Optional 7th element of a cache entry: physics flags from
# asset_convert.collision_extract.physics_flags_from_data (bit 0 =
# constrained dynamic island -> the record must be MSTT, not STAT).
_MESH_PHYSICS: Dict[str, int] = {}


def load_mesh_bounds(cache_path: str, quiet: bool = False) -> int:
    """Load previously computed bounds from *cache_path* into the module cache.

    Per-key lookup: if a key exists in the JSON it is used; missing keys fall
    back to type defaults (no recompute).  Returns the number of entries loaded.

    quiet=True skips the status prints — used by navmesh worker processes, which
    each call this once in their pool initializer and would otherwise spam one
    line per worker.
    """
    global _MESH_BOUNDS, _MESH_PHYSICS
    if not os.path.exists(cache_path):
        if not quiet:
            print(f"  Mesh bounds: cache not found ({cache_path}), using type defaults")
        return 0
    try:
        with open(cache_path, encoding='utf-8') as fh:
            raw = json.load(fh)
        # '__schema__' carries the cache version, not a mesh — see
        # collision_extract.BOUNDS_SCHEMA_VERSION.  It is not a path key, so it
        # can never be looked up, but it must not become a bounds entry either.
        raw = {k: v for k, v in raw.items() if k != '__schema__'}
        _MESH_BOUNDS = {k: tuple(v[:6]) for k, v in raw.items()}
        _MESH_PHYSICS = {k: int(v[6]) for k, v in raw.items() if len(v) > 6}
        if not quiet:
            print(f"  Mesh bounds: loaded {len(_MESH_BOUNDS)} entries from cache")
        return len(_MESH_BOUNDS)
    except (OSError, json.JSONDecodeError) as exc:
        if not quiet:
            print(f"  Mesh bounds: could not load cache ({exc}), using type defaults")
        return 0


def get_mesh_obnd(path_key: str) -> Optional[OBNDTuple]:
    """Return cached OBND tuple for *path_key*, or ``None`` if not found.

    *path_key* must be lowercase with forward slashes, relative to the mesh
    output directory root (e.g. ``"tes4/furniture/chairnoble01.nif"``).
    """
    return _MESH_BOUNDS.get(path_key)


def get_mesh_physics_flags(path_key: str) -> int:
    """Physics flags for *path_key* (0 if unknown).

    Bit 0: the converted NIF is a constrained dynamic havok island (swinging
    chains/signs).  Skyrim never simulates those on a STAT reference — the
    base record must be written as MSTT (see items.convert_STAT).
    """
    return _MESH_PHYSICS.get(path_key, 0)
