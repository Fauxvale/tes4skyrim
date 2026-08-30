"""Navmesh build entry point.

CURRENT MODEL (Phase 1): pathgrid CORRIDOR RIBBONS — see corridor.py and
docs/notes/navmesh_corridor.md.  The navmesh is built directly on the
pathgrid as a flat, fixed-width ribbon of triangles per edge; edges meeting at
a node share the node vertex, so NVNM adjacency links by construction.  Doors
get a threshold quad welded into the ribbon; cross-cell edge links and NAVI are
downstream, unchanged.

`build_navmesh` keeps its historical signature and delegates to corridor.py.
(The old voxel/span-graph generator — voxel.py / region.py / spanmesh.py — came
off the build path when this model landed and has since been deleted.)

Returns (verts, tris) in world space.  The caller (pgrd_to_navm) owns the
NVNM/NAVM binary packing, validated byte-exact against Skyrim.esm — do not
change it.
"""

import logging
import math

from . import corridor
from . import world

_log = logging.getLogger(__name__)


def teleport_door_positions(refr_recs):
    """(x, y, z, rot_z, True) of every teleport-door REFR (XTEL) in the cell.

    Fallback door list when the caller cannot supply one (tools without a DOOR
    base-record set; interior-only doors are missed then).  A teleport door
    leads to ANOTHER cell, so the navmesh must end at its threshold — exactly
    as vanilla navmeshes do.  These positions become barriers for the
    pathgrid-reach flood (see region.keep_pathgrid_heights): without them, an
    interior cell's mesh escapes through the open doorway and spreads over the
    decorative street/porch geometry outside the shell.  They also ANCHOR
    island pruning: the doorstep component in front of each door is how an NPC
    enters the cell, so it is always kept.
    """
    out = []
    for refr in refr_recs or ():
        if refr.get('XTEL.Door'):
            try:
                x, y = float(refr.get('PosX')), float(refr.get('PosY'))
                z = float(refr.get('PosZ'))
                rz = float(refr.get('RotZ') or 0.0)
            except (TypeError, ValueError):
                continue
            # float() happily parses NaN and 8.9e17, so range-check as well --
            # see navmesh/world.py _MAX_PLACEMENT for why these exist.
            if not all(math.isfinite(v) for v in (x, y, z, rz)):
                continue
            if max(abs(x), abs(y), abs(z)) > world._MAX_PLACEMENT:
                continue
            # Width 0: no measured doorway span for a bare-XTEL fallback door;
            # corridor_doors falls back to its constant half-width.
            out.append((x, y, z, rz, True, 0.0))
    return out


def build_navmesh(refr_recs, base_model_by_fid, get_collision, nodes, edges,
                  land_rec=None, origin_x=0.0, origin_y=0.0, budget=None,
                  doors=None, ledges_out=None, door_bases=None):
    """Build a navmesh for one cell.  Returns (verts3d, tris) or ([], []).

    Phase-1 corridor model: delegates to corridor.build_corridors.  See
    corridor.py and docs/notes/navmesh_corridor.md.

    doors: [(x, y, z, rot_z, is_teleport, width), ...] door REFRs (teleport AND
    interior).  When None, teleport doors are recovered from XTEL alone.
    door_bases: low-24 DOOR base FormIDs — those refs' panel collision is
    EXCLUDED (a door is opened, not walked around; see world.gather_cell_geometry).
    When None, door refs are found via XTEL plus the doors list positions.
    budget is accepted for signature compatibility (the corridor build has no
    per-cell time risk) and ignored.
    """
    if not nodes:
        return [], []
    if doors is None:
        doors = teleport_door_positions(refr_recs)
    verts, tris, ledges = corridor.build_corridors(
        refr_recs, base_model_by_fid, get_collision, nodes, edges,
        land_rec=land_rec, origin_x=origin_x, origin_y=origin_y, doors=doors,
        door_bases=door_bases)
    # Drop-down (Ledge Up/Down) pairs are reported OUT-OF-BAND so the long-
    # standing (verts, tris) return stays intact for the many callers that
    # only want geometry.  pgrd_to_navm reads this to write the edge links.
    if ledges_out is not None:
        ledges_out.extend(ledges)
    return verts, tris
