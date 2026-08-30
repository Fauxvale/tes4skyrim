"""Pathgrid-corridor navmesh generation.

THE PATHGRID IS THE MESH: the navmesh is built directly on Bethesda's pathgrid
as a boolean union of fixed-width ribbons, one per pathgrid edge, rather than by
discovering walkable surface from collision.  Collision is consulted only to
sit the ribbons on the floor and to stop them at walls.

See corridor.py and docs/notes/navmesh_corridor.md.  (The earlier
voxelize -> regions -> contours generator this replaced has been deleted.)
"""

from .build import build_navmesh  # noqa: F401
