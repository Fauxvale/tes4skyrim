"""Regression tests for the navmesh triangle-quality contract.

The quality pipeline (docs/navmesh_corridor_redesign.md, "The triangle-quality
contract") is a chain of guarded passes over synthetic-friendly pure functions
in corridor_clean/corridor_union.  Each test here encodes a measured failure
that must not come back:

* flips minting duplicate (non-manifold) edges — tore whole regions out of
  ChorrolFightersGuild and BarrenCave via _make_manifold;
* decimation pinching a thin neck (boundary pair joined by an interior edge)
  — shed vertex-attached scraps on BarrenCave;
* the sliver cull disconnecting what it trims;
* the de-stack removing genuine storeys instead of duplicates.
"""

import math

import pytest

pytest.importorskip("numpy")
pytest.importorskip("shapely")

from tes5_import.navmesh import corridor_clean as cc  # noqa: E402
from tes5_import.navmesh import corridor_union as cu  # noqa: E402
from tes5_import.navmesh import params  # noqa: E402


def _components(tris):
    return cc.components([list(map(int, t)) for t in tris])


def _edge_counts(tris):
    counts = {}
    for t in tris:
        for k in range(3):
            a, b = t[k], t[(k + 1) % 3]
            key = (a, b) if a < b else (b, a)
            counts[key] = counts.get(key, 0) + 1
    return counts


def _worst_ratio(verts, tris):
    worst = 1.0
    for t in tris:
        p, q, r = verts[t[0]], verts[t[1]], verts[t[2]]
        e = [math.dist(p[:2], q[:2]), math.dist(q[:2], r[:2]),
             math.dist(r[:2], p[:2])]
        lo = min(e)
        worst = max(worst, (max(e) / lo) if lo > 1e-9 else 1e9)
    return worst


# ---------------------------------------------------------------------------
# _flip_pass
# ---------------------------------------------------------------------------

def test_flip_improves_a_sliver_fan():
    """A kite split on its LONG diagonal flips to the short one."""
    # Kite: the 0-2 diagonal (120u) makes triangle (0,1,2) a 5u-high sliver;
    # the 1-3 diagonal (75u) yields two healthy triangles.
    verts = [[0, 0, 0], [60, 5, 0], [120, 0, 0], [60, 80, 0]]
    tris = [(0, 1, 2), (0, 2, 3)]
    before = _worst_ratio(verts, tris)
    out = cc._flip_pass(verts, [list(t) for t in tris], pin=set())
    assert out is not None
    assert _worst_ratio(verts, out) < before
    # Coverage unchanged: two triangles, same four vertices.
    assert len(out) == 2
    assert {v for t in out for v in t} == {0, 1, 2, 3}


def test_flip_never_mints_a_duplicate_edge():
    """Flipping onto a diagonal that already exists elsewhere is forbidden.

    Regression: folded storeys reuse vertices, so the new diagonal can already
    be an edge of an unrelated triangle pair; the flip then gives that edge
    3+ owners and _make_manifold rips the extras out with no connectivity
    guard (whole regions detached in ChorrolFightersGuild / BarrenCave).
    """
    # Quad 0-1-2-3 split on 0-2, PLUS two spectator triangles already using
    # edge (1, 3) — the diagonal a flip would create.
    verts = [[0, 0, 0], [100, 0, 0], [100, 30, 0], [0, 30, 0],
             [50, -80, 0], [50, 110, 0]]
    tris = [[0, 1, 2], [0, 2, 3], [1, 4, 3], [1, 3, 5]]
    out = cc._flip_pass(verts, [list(t) for t in tris], pin=set())
    final = out if out is not None else tris
    counts = _edge_counts(final)
    assert max(counts.values()) <= 2, "flip created a 3-owner edge"


def test_flip_leaves_pinned_door_triangle_alone():
    verts = [[0, 0, 0], [100, 0, 0], [100, 30, 0], [0, 30, 0]]
    tris = [[0, 1, 2], [0, 2, 3]]
    out = cc._flip_pass(verts, [list(t) for t in tris],
                        pin={0, 1, 2})       # first triangle fully pinned
    assert out is None or (0, 1, 2) in [tuple(t) for t in out]


# ---------------------------------------------------------------------------
# decimate: topology guards
# ---------------------------------------------------------------------------

def test_decimate_never_pinches_a_thin_neck():
    """Two boundary vertices joined by an INTERIOR edge may not collapse.

    Regression: collapsing across the neck of a thin peninsula fuses the
    outline at a point; the peninsula then hangs vertex-attached and the
    engine sees a separate component (BarrenCave went [1768, 6, 5, 3]).
    """
    # Two 'lobes' joined by a narrow two-triangle neck whose crossing edge
    # (4, 5) is interior and SHORT — the collapse candidate.
    verts = [
        [0, 0, 0], [120, 0, 0], [120, 100, 0], [0, 100, 0],      # lobe A
        [140, 40, 0], [140, 60, 0],                              # neck
        [260, 0, 0], [260, 100, 0], [380, 0, 0], [380, 100, 0],  # lobe B
    ]
    tris = [
        (0, 1, 2), (0, 2, 3),
        (1, 4, 2), (2, 4, 5),
        (4, 6, 5), (5, 6, 7),
        (6, 8, 7), (7, 8, 9),
    ]
    n0 = len(_components(tris))
    v2, t2 = cc.decimate([list(v) for v in verts], [tuple(t) for t in tris])
    assert len(_components(t2)) <= n0, "decimation disconnected the mesh"


# ---------------------------------------------------------------------------
# cull_boundary_slivers
# ---------------------------------------------------------------------------

def test_cull_removes_a_boundary_needle_but_keeps_connectivity():
    # A healthy quad with one thin flap atop its boundary edge (3, 2):
    # ratio ~3.3 (> CULL_SLIVER_RATIO), area 250 (< MIN_TRI_AREA).
    verts = [
        [0, 0, 0], [100, 0, 0], [100, 100, 0], [0, 100, 0],
        [70, 105, 0],
    ]
    tris = [(0, 1, 2), (0, 2, 3), (3, 2, 4)]
    out = cc.cull_boundary_slivers(verts, [tuple(t) for t in tris])
    assert (3, 2, 4) not in out, "the needle should be culled"
    assert len(_components(out)) == 1


def test_cull_spares_a_sliver_carrying_the_pathgrid():
    verts = [
        [0, 0, 0], [100, 0, 0], [100, 100, 0], [0, 100, 0],
        [70, 105, 0],
    ]
    tris = [(0, 1, 2), (0, 2, 3), (3, 2, 4)]
    out = cc.cull_boundary_slivers(verts, [tuple(t) for t in tris],
                                   pin_xy=[(70.0, 102.0)])
    assert (3, 2, 4) in out, "a pathgrid-carrying triangle must survive"


# ---------------------------------------------------------------------------
# _destack
# ---------------------------------------------------------------------------

def test_destack_removes_a_same_surface_duplicate():
    verts = [
        [0, 0, 0], [100, 0, 0], [100, 100, 0], [0, 100, 0],
        # duplicate patch a hair above, sharing no indices
        [10, 10, 5], [90, 10, 5], [50, 90, 5],
    ]
    tris = [(0, 1, 2), (0, 2, 3), (4, 5, 6)]
    out = cu._destack(verts, [tuple(t) for t in tris])
    assert (4, 5, 6) not in out
    assert (0, 1, 2) in out and (0, 2, 3) in out


def test_destack_keeps_genuine_storeys():
    verts = [
        [0, 0, 0], [100, 0, 0], [100, 100, 0], [0, 100, 0],
        [10, 10, 200], [90, 10, 200], [50, 90, 200],   # a real floor above
    ]
    tris = [(0, 1, 2), (0, 2, 3), (4, 5, 6)]
    out = cu._destack(verts, [tuple(t) for t in tris])
    assert (4, 5, 6) in out, "a storey %su above is not a duplicate" % 200


# ---------------------------------------------------------------------------
# The shape contract constant
# ---------------------------------------------------------------------------

def test_edge_ratio_contract_is_two():
    """The author-set contract: long side <= 2x the short side."""
    assert params.MAX_EDGE_RATIO == 2.0
    assert params.CULL_SLIVER_RATIO == 2.0
    assert params.MIN_TRI_AREA > 0
