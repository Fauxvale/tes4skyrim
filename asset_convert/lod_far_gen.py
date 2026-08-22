"""Generate _far.nif LOD meshes by decimating full-resolution Skyrim NIFs.

Called from lod_gen.generate_lod() as a pre-pass to ensure all LOD-flagged
objects have a _far.nif before LODGenx64 is invoked.

Algorithm
---------
1. Read the converted Skyrim NIF (v20.2.0.7, BSStream 83).
2. Transform every solid (non-skinned) shape into a common root space and
   concatenate it into ONE vertex/triangle soup, tagging each triangle with
   the shape it came from.
3. Simplify that soup with quadric-error-metric (QEM) half-edge collapses:
   positions are welded for topology, edges are collapsed cheapest-first into
   a surviving original vertex, the moved corner's UV is interpolated onto the
   survivor, boundary edges carry constraint quadrics so open rims shrink last,
   and collapses that would flip a face normal are rejected.
4. Split the surviving triangles back out by material tag and write each group
   into its original shape, so every shape keeps its own texture and shader.
5. Recompute smooth per-vertex normals; recompute tangent/bitangent vectors
   from UV differentials (standard tangent-space method).
6. Strip collision, controllers, skin, vertex colors, and extra data from all
   nodes, and clear the VertexColors shader flag (SF2 bit 0x20).
7. Write to <model_base>_far.nif.

Decimating the whole model as one welded topology is what keeps it watertight.
Shapes used to be decimated INDEPENDENTLY, which left a rim shared by two
shapes as a free boundary to both: each side chose different survivors, the
rims drifted apart, and the gap was the hole.  Welding makes the shared rim one
graph node, so a collapse moves both sides at once.

BSLightingShaderProperty is COPIED from the source (correct flags, no
recreation) — this fixes the missing ZBufferTest flag that caused objects to
not render in-game.
"""

import io
import math
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from worker_budget import worker_count  # noqa: E402

from .game_paths import win_join
from . import pyffi_monkey_patch  # noqa — must apply before NifFormat import
from pyffi.formats.nif import NifFormat

_SKYRIM_VER = 0x14020007
_NIF_FLAGS  = 14

# Global target across all shapes combined.
#
# The budget below targets roughly vanilla Skyrim object-LOD density +25%.
# Vanilla Tamriel spends ~11,000 bytes of object LOD per CELL at level 4
# (measured from `Skyrim - Meshes1.bsa`); before this pass we spent ~410,000,
# i.e. 37x vanilla, because decimation was structurally broken and every
# constant here had been clamped down to hide the damage:
#   - shapes were decimated INDEPENDENTLY, so shared rims drifted apart and
#     tore holes; `_BOUNDARY_WEIGHT` froze both rims to limit the drift and
#     `_MIN_SRC_TRIS`/`_MIN_TRIS` deleted small shapes rather than risk them.
#   - collapses kept the ORIGINAL UV, so charts were squeezed into a third of
#     their proper footprint; `_MAX_DEV_FRAC` was clamped to 0.03 to limit the
#     shearing, which stalled reduction at ~40% of source verts against a
#     nominal 8% target.
# Both defects are fixed (one welded topology per model; UVs interpolated onto
# the survivor), so the defensive constants are gone and the real budget can
# stand on its own.
_DECIMATE_RATIO    = 0.05    # share of source verts to keep
_MIN_TOTAL_TARGET  = 24      # floor on a whole model's combined vertex budget:
                             # small props must still read as themselves
_NO_CAP            = 1 << 30  # the base tier is bounded by _DECIMATE_RATIO
                             # alone; only the _far8/_far16 tiers cap verts
_SF2_VERTEX_COLORS = 0x20    # SF2 bit to clear when removing vertex colors

# Tree models get a crossed-quad billboard _far.nif (vanilla-style flat tree
# LOD) instead of decimated geometry — decimating leaf cards shreds canopies
# and drops trunks, and the full geometry made .bto tiles enormous.
_TREE_MODEL_PREFIX = 'tes4\\speedtrees\\'
_BILLBOARD_TEX_DIR = 'tes4\\trees\\billboards'


# ---------------------------------------------------------------------------
# Decimation helpers
# ---------------------------------------------------------------------------


# Boundary-edge constraint quadric weight (× len²).  This used to be 8.0, to
# stop the two sides of a shared rim drifting apart — which it could never do,
# because each side was decimated separately and nothing made them AGREE.  Now
# that a model is one welded topology the seam cannot open, so this only has to
# do its real job: hold GENUINELY open rims (window frames, wall tops, leaf-card
# edges) a little longer than interior geometry.
_BOUNDARY_WEIGHT = 1.0
_WELD_EPS        = 1e-3    # position weld tolerance (game units)
_STITCH_MAX_EDGE_MULT = 1.0  # hard cap on the stitch radius, in multiples of
                             # the model's median edge length: never merge
                             # across more than the scale of the authored
                             # detail (see the stitch pass for the census)
_STITCH_FRAC     = 0.008   # proximity-stitch tolerance, as a fraction of the
                           # model diagonal: nodes this close are treated as
                           # one surface even when they only interpenetrate
                           # rather than share vertices (see the stitch pass).
_MAX_DEV_FRAC    = 0.25    # error floor: stop when a collapse would deviate
                           # more than this fraction of the model diagonal.
                           # 0.03 existed to limit UV shearing and cost ~5x the
                           # intended reduction; at LOD range (2+ km) a quarter
                           # of the diagonal is well under a pixel of silhouette.
_EDGE_LEN_REG    = 0.5     # edge-length regularization (× mean face area)
_TOPO_BOUNDARY_WEIGHT = 6.0  # budget multiplier per unit of boundary-vertex
                             # fraction: a 30%-rim building gets 2.8x the
                             # vertices of a closed rock at the same ratio

# Coarser variants for the far LOD rings.  The _far8/_far16 meshes are
# re-decimated FROM the _far.nif with a relaxed error floor — at level-8/16
# distances (2+ km) silhouette lumps are invisible but baked verts still
# cost disk/VRAM in every tile.
# A tier is only written when it comes back at least this much lighter than
# the mesh it would replace.  Below that it is the same geometry under a
# second filename: LODGen bakes identical triangles either way, so the file
# is pure cost.  When it is absent `_lod_meshes_for` lists the _far.nif for
# that level instead.
_TIER_MIN_GAIN = 0.90

_TIER8  = dict(ratio=0.5,  cap=250, dev=0.08, suffix='_far8')
_TIER16 = dict(ratio=0.25, cap=120, dev=0.12, suffix='_far16')


def is_boundary_fraction(verts: np.ndarray, tris: np.ndarray) -> float:
    """Share of welded vertices that sit on an open rim (0.0 - 1.0).

    Used to scale the decimation budget: rim vertices are pinned by the
    open-rim guard, so a model that is mostly rim has little collapsible
    interior and needs a larger budget to survive.
    """
    if len(verts) == 0 or len(tris) == 0:
        return 0.0
    keys = np.round(verts / _WELD_EPS).astype(np.int64)
    uq, wid = np.unique(keys, axis=0, return_inverse=True)
    F = wid[tris]
    ok = (F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2])
    F = F[ok]
    if not len(F):
        return 0.0
    e = np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
    ue, c = np.unique(np.sort(e, axis=1), axis=0, return_counts=True)
    if not (c == 1).any():
        return 0.0
    return len(np.unique(ue[c == 1])) / max(len(uq), 1)


def _qem_decimate(verts: np.ndarray, tris: np.ndarray,
                  uvs: Optional[np.ndarray],
                  target_verts: int,
                  max_dev_frac: float = _MAX_DEV_FRAC,
                  tri_mat: Optional[np.ndarray] = None) -> Tuple:
    """Quadric-error-metric half-edge-collapse simplification.

    Positions are welded (UV-seam duplicates share one topology node) so
    collapses can cross seams.  A collapse u→v moves u to v's exact original
    position, interpolates the moved corner's UV onto that position so the
    texture chart follows the geometry, is charged the combined quadric error
    at v, is rejected if it would flip an adjacent face's normal, and boundary
    edges carry perpendicular constraint quadrics so open rims shrink last.
    This preserves silhouettes and never punches holes the way grid
    vertex-clustering did.

    `tri_mat` optionally tags each input triangle with the material (source
    shape) it came from.  It is carried through the collapse unchanged and
    returned alongside the surviving triangles, which is what lets a whole
    model be welded into ONE topology and decimated together — shared rims
    between shapes become genuinely shared graph nodes, so a collapse moves
    both sides at once and the seam cannot tear — then split back out per
    material afterwards.  Decimating each shape separately instead let the two
    sides of a seam pick different survivors and drift apart: measured on
    `centrancerockmosslg01`, the shared boundary went from 32% welded to 9%
    and the gap opened from 3.8 to 93.4 units (6% of the object diagonal).

    Returns (new_verts, new_tris, new_uvs, new_tri_mat).
    """
    nV, nT = len(verts), len(tris)
    if nV == 0 or nT == 0:
        return verts, tris, uvs, tri_mat

    # ---- weld positions for topology -------------------------------------
    keys = np.round(verts / _WELD_EPS).astype(np.int64)
    _, first_idx, wid = np.unique(keys, axis=0, return_index=True,
                                  return_inverse=True)
    W = len(first_idx)
    P = verts[first_idx].astype(np.float64)          # position per weld node

    # ---- stitch touching pieces into ONE component ------------------------
    # An exact position weld only joins vertices that COINCIDE.  Game models
    # are not built that way: `piratecabin01` is 14 open sheets that overlap
    # and interpenetrate — visually one solid cabin, topologically 33 separate
    # components with only 20 of 91 shape pairs having any vertex within a
    # unit of each other.  Decimating that means dividing one budget among 33
    # pieces, which grinds each to nothing and takes whole planks with it.
    #
    # So merge nodes that are merely CLOSE, not identical.  The tolerance is
    # relative to the model, because "touching" means something different on a
    # 100-unit crate and an 8,000-unit fort.  This runs before quadrics are
    # built, so the collapse sees one connected surface and can simplify a
    # plank into its neighbour exactly as it simplifies one rock face into the
    # next.
    # The radius is capped by the model's own DETAIL scale, not just its
    # overall size.  A fraction of the diagonal is right for a building, whose
    # planks are large and genuinely overlap, but on thin repeated geometry it
    # exceeds the size of the parts themselves and fuses things that merely
    # pass near each other: `mainmast01` is rigging with a 2,968-unit diagonal
    # and a 9-unit median edge, so a 24-unit radius welded separate ropes into
    # one rope and the collapse then dragged them together.  Measured across
    # models, radius / median-edge cleanly separates the two cases — cabin
    # 0.38 and castle 0.79 (both want stitching) against IC wall 1.78 and mast
    # 2.62 (both do not) — so cap the radius at the median edge length.
    _f = wid[tris]
    _f = _f[(_f[:, 0] != _f[:, 1]) & (_f[:, 1] != _f[:, 2])
            & (_f[:, 0] != _f[:, 2])]
    if len(_f):
        _ue = np.unique(np.sort(np.concatenate(
            [_f[:, [0, 1]], _f[:, [1, 2]], _f[:, [2, 0]]]), axis=1), axis=0)
        _median_edge = float(np.median(
            np.linalg.norm(P[_ue[:, 0]] - P[_ue[:, 1]], axis=1)))
    else:
        _median_edge = 0.0
    stitch_eps = max(_WELD_EPS,
                     min(float(np.linalg.norm(P.max(axis=0) - P.min(axis=0)))
                         * _STITCH_FRAC,
                         _median_edge * _STITCH_MAX_EDGE_MULT))
    if W > 1 and stitch_eps > _WELD_EPS:
        try:
            from scipy.spatial import cKDTree
            pairs = cKDTree(P).query_pairs(stitch_eps, output_type='ndarray')
        except Exception:
            pairs = None
        if pairs is not None and len(pairs):
            sp = list(range(W))

            def _sfind(x):
                while sp[x] != x:
                    sp[x] = sp[sp[x]]
                    x = sp[x]
                return x

            for a, b in pairs:
                ra, rb = _sfind(int(a)), _sfind(int(b))
                if ra != rb:
                    sp[rb] = ra
            rep = np.array([_sfind(i) for i in range(W)], dtype=np.int64)
            uniq_rep, remap = np.unique(rep, return_inverse=True)
            if len(uniq_rep) < W:
                # Keep the representative's ORIGINAL position: averaging the
                # merged group would pull the surface off the silhouette.
                P = P[uniq_rep]
                wid = remap[wid]
                W = len(uniq_rep)

    F0 = wid[tris]                                    # faces in weld space
    ok = (F0[:, 0] != F0[:, 1]) & (F0[:, 1] != F0[:, 2]) & (F0[:, 0] != F0[:, 2])
    F0 = F0[ok]
    C0 = tris[ok]                                     # original corner ids (UVs)
    M0 = tri_mat[ok] if tri_mat is not None else None  # material tag per face
    if not len(F0):
        return verts, tris, uvs, tri_mat

    # ---- initial quadrics (area-weighted face planes) ---------------------
    v0, v1, v2 = P[F0[:, 0]], P[F0[:, 1]], P[F0[:, 2]]
    fn = np.cross(v1 - v0, v2 - v0)                   # |fn| = 2×area
    area2 = np.linalg.norm(fn, axis=1)
    nrm = fn / np.maximum(area2, 1e-12)[:, None]
    d = -np.einsum('ij,ij->i', nrm, v0)
    plane = np.concatenate([nrm, d[:, None]], axis=1)             # (nF,4)
    fq = plane[:, :, None] * plane[:, None, :] * area2[:, None, None]
    Q = np.zeros((W, 4, 4), np.float64)
    for k in range(3):
        np.add.at(Q, F0[:, k], fq)

    # ---- boundary constraint quadrics -------------------------------------
    edges = np.concatenate([F0[:, [0, 1]], F0[:, [1, 2]], F0[:, [2, 0]]])
    edges_s = np.sort(edges, axis=1)
    uniq_e, e_cnt = np.unique(edges_s, axis=0, return_counts=True)
    boundary = uniq_e[e_cnt == 1]
    # Which weld nodes sit on a genuinely open rim (an edge used by ONE face).
    # The collapse loop refuses to merge one of these into an interior vertex:
    # the constraint quadrics below only penalise moving a rim vertex ALONG
    # its edge line, and say nothing about it being absorbed upward into the
    # body, so a rock with a flat open bottom would have that bottom eaten
    # away.  Measured on `rockgreatforest1125rdm`, whose 106 rim verts all sit
    # at z=-241.2: without the guard only 12 of 178 rim nodes survived and the
    # rim rose 162 units — 34% of the model height — leaving the rock floating
    # above the terrain.
    is_boundary = np.zeros(W, dtype=bool)
    if len(boundary):
        is_boundary[boundary.ravel()] = True
    if len(boundary):
        # Constraint plane through each boundary edge: any plane containing the
        # edge penalizes moving its endpoints off the edge line, which is what
        # keeps open rims (window frames, wall tops, leaf-card edges) intact.
        # Use the plane spanned by the edge and the world axis least aligned
        # with it.
        ea, eb = boundary[:, 0], boundary[:, 1]
        edge_v = P[eb] - P[ea]
        ax = np.zeros_like(edge_v)
        ax[np.arange(len(edge_v)), np.argmin(np.abs(edge_v), axis=1)] = 1.0
        cn = np.cross(edge_v, ax)
        cl = np.linalg.norm(cn, axis=1)
        good = cl > 1e-12
        cn[good] /= cl[good][:, None]
        cd = -np.einsum('ij,ij->i', cn, P[ea])
        cplane = np.concatenate([cn, cd[:, None]], axis=1)
        w = _BOUNDARY_WEIGHT * np.einsum('ij,ij->i', edge_v, edge_v)
        cq = cplane[:, :, None] * cplane[:, None, :] * w[:, None, None]
        np.add.at(Q, ea, cq)
        np.add.at(Q, eb, cq)

    # ---- UV charts per weld node ------------------------------------------
    # A collapse u->v moves the corner's POSITION to v while the corner keeps
    # u's ORIGINAL UV.  The triangle then covers the geometry both vertices
    # used to span, but its UV footprint is unchanged — so the chart is
    # squeezed into less and less of the texture as collapses accumulate.
    # Measured on `rockgreatforest1500fgdrlichen`: the far mesh retains 96.3%
    # of the source's geometric area but only 32.3% of its UV area, leaving
    # 80% of triangles below half the source texel density (stretched).  On
    # `icexteriorwall02` the density spread reached 53,303x — a single-texel
    # streak, which reads in-game as a garbled or invisible texture.
    #
    # The fix is to carry a MUTABLE UV per corner and move it with the vertex:
    # when u collapses into v, the corner's UV becomes the point in u's chart
    # that corresponds to v's position, found by projecting v onto the edge
    # u->v in 3D and interpolating the UV by the same parameter.  UVs are
    # piecewise-linear over the surface, so this is exact along the edge and
    # keeps the chart's area in step with the geometry it covers.
    # A corner's UV is per-FACE once it starts moving (two faces sharing a
    # vertex can sit in different charts), so give each corner its own slot.
    # Plain (u, v) tuples, not NumPy rows: the collapse loop touches these
    # hundreds of thousands of times and scalar tuples are far cheaper.
    corner_uv = None
    if uvs is not None:
        uvl = [(float(a), float(b)) for a, b in uvs]
        corner_uv = [[uvl[int(C0[fi, k])] for k in range(3)]
                     for fi in range(len(F0))]

    def _uv_at(fi, i, u, v, cu):
        """UV for the corner of face `fi` that just moved from u to v.

        The face still holds its other two corners, whose UVs are known and
        whose positions are unchanged, so the face defines a local affine
        map from position to UV.  Solve it for v's position: that is exactly
        where v lands in this face's chart.  Falls back to the edge-parameter
        interpolation (and then to the unchanged UV) when the face is
        degenerate in position or UV and the map is not invertible.
        """
        f = faces[fi]
        j, k = (i + 1) % 3, (i + 2) % 3
        # Scalar arithmetic throughout: this runs once per collapsed corner
        # (~50k times per shape), where NumPy's per-call dispatch costs far
        # more than the handful of multiplies — same reason cost_of and the
        # flip guard are written out longhand.
        ax, ay, az = PL[f[j]]
        bx, by, bz = PL[f[k]]
        ux, uy, uz = PL[u]
        vx, vy, vz = PL[v]
        ua_ = corner_uv[fi][j]
        ub_ = corner_uv[fi][k]
        # Express (pv - pa) in the basis (pb - pa, pu - pa), then apply the
        # same weights to the UVs.  det == 0 covers the degenerate face, so
        # no separate normal test is needed.
        e1x, e1y, e1z = bx - ax, by - ay, bz - az
        e2x, e2y, e2z = ux - ax, uy - ay, uz - az
        d11 = e1x * e1x + e1y * e1y + e1z * e1z
        d12 = e1x * e2x + e1y * e2y + e1z * e2z
        d22 = e2x * e2x + e2y * e2y + e2z * e2z
        det = d11 * d22 - d12 * d12
        if det > 1e-20 or det < -1e-20:
            dx, dy, dz = vx - ax, vy - ay, vz - az
            b1 = dx * e1x + dy * e1y + dz * e1z
            b2 = dx * e2x + dy * e2y + dz * e2z
            s = (b1 * d22 - b2 * d12) / det
            t_ = (d11 * b2 - d12 * b1) / det
            au, av = ua_
            return (au + s * (ub_[0] - au) + t_ * (cu[0] - au),
                    av + s * (ub_[1] - av) + t_ * (cu[1] - av))
        # Degenerate face: the affine map is not invertible, so leave the UV.
        return cu

    # ---- mutable topology --------------------------------------------------
    import heapq
    faces = [[int(a), int(b), int(c)] for a, b, c in F0]
    corners = [[int(a), int(b), int(c)] for a, b, c in C0]
    face_alive = [True] * len(faces)
    vert_faces = [set() for _ in range(W)]
    for fi, f in enumerate(faces):
        for v in f:
            vert_faces[v].add(fi)

    version = [0] * W
    alive = sum(1 for s in vert_faces if s)

    # Per-vertex accumulated face area (for the error floor) + regularization.
    A = np.zeros(W, np.float64)
    for k in range(3):
        np.add.at(A, F0[:, k], area2 / 6.0)   # area2 = 2×area, /3 per corner
    mean_face_area = float(area2.mean()) / 2.0
    diag = float(np.linalg.norm(P.max(axis=0) - P.min(axis=0)))
    max_dev2 = (diag * max_dev_frac) ** 2

    hom = np.ones(4)

    # Python-list views of the per-node data the inner loop touches.  cost_of
    # and the flip guard run ~100k times per shape on 3-vectors, where NumPy's
    # per-call dispatch overhead dwarfs the arithmetic (np.cross alone spent
    # more time in normalize_axis_tuple/moveaxis than on the cross product).
    PL = [tuple(map(float, p)) for p in P]

    def cost_of(u, v):
        hom[:3] = P[v]
        q = Q[u] + Q[v]
        c = float(hom @ q @ hom)
        # edge-length regularization: discourage long-distance collapses that
        # stretch faces into "sails" even when the quadric error is small.
        # Scaled like "one average face displaced by the collapse distance".
        ux, uy, uz = PL[u]
        vx, vy, vz = PL[v]
        dx, dy, dz = ux - vx, uy - vy, uz - vz
        c += _EDGE_LEN_REG * mean_face_area * (dx * dx + dy * dy + dz * dz)
        return c

    def _flips(a, b, ox, oy, oz, nx_, ny_, nz_):
        """True if triangle (a, b, ·) flips when its third vertex moves from
        (ox,oy,oz) to (nx_,ny_,nz_).  Scalar cross products + dot."""
        ax, ay, az = PL[a]
        bx, by, bz = PL[b]
        a1x, a1y, a1z = ax - ox, ay - oy, az - oz
        b1x, b1y, b1z = bx - ox, by - oy, bz - oz
        c1x = a1y * b1z - a1z * b1y
        c1y = a1z * b1x - a1x * b1z
        c1z = a1x * b1y - a1y * b1x
        a2x, a2y, a2z = ax - nx_, ay - ny_, az - nz_
        b2x, b2y, b2z = bx - nx_, by - ny_, bz - nz_
        c2x = a2y * b2z - a2z * b2y
        c2y = a2z * b2x - a2x * b2z
        c2z = a2x * b2y - a2y * b2x
        return (c1x * c2x + c1y * c2y + c1z * c2z) <= 0.0

    def neighbors(u):
        out = set()
        for fi in vert_faces[u]:
            out.update(faces[fi])
        out.discard(u)
        return out

    heap = []
    for e in uniq_e:
        a, b = int(e[0]), int(e[1])
        heapq.heappush(heap, (cost_of(a, b), a, b, version[a], version[b]))
        heapq.heappush(heap, (cost_of(b, a), b, a, version[b], version[a]))

    # ---- per-component floor ----------------------------------------------
    # A model is often many DISCONNECTED pieces — `piratecabin01` is 33 planks,
    # beams and panels — and a global vertex budget says nothing about how it
    # should be split between them.  Asking for 54 vertices across 33 pieces is
    # ~1.6 each, far below the 4 a closed piece needs, so the loop ground whole
    # planks out of existence and the "holes" were missing parts, not torn
    # surface.  Decimation must never delete a piece: give every component its
    # own floor and refuse the collapse that would take it below.
    comp_of = list(range(W))

    def _find(x):
        while comp_of[x] != x:
            comp_of[x] = comp_of[comp_of[x]]
            x = comp_of[x]
        return x

    for f_ in faces:
        r0 = _find(f_[0])
        for k in (1, 2):
            rk = _find(f_[k])
            if rk != r0:
                comp_of[rk] = r0
    comp_alive: dict = {}
    for w_ in range(W):
        if vert_faces[w_]:
            r = _find(w_)
            comp_alive[r] = comp_alive.get(r, 0) + 1
    # 4 vertices is the minimum for a closed piece (a tetrahedron); a flat open
    # sheet still reads correctly at 3.  Use 4 — one wasted vertex on a plank
    # is nothing against the plank disappearing.
    _COMP_MIN = 4

    target = max(int(target_verts), 4)

    while alive > target and heap:
        cost, u, v, vu, vv = heapq.heappop(heap)
        if version[u] != vu or version[v] != vv:
            continue
        if not vert_faces[u] or not vert_faces[v]:
            continue
        if not math.isfinite(cost):
            continue
        # error floor: if even the cheapest remaining collapse would deviate
        # more than _MAX_DEV_FRAC of the diagonal, stop — a heavier LOD beats
        # a shredded one.  (cost ≈ local_area × deviation².)
        if cost > (A[u] + A[v]) * max_dev2 + 1e-12:
            continue
        # still adjacent?
        shared = [fi for fi in vert_faces[u] if v in faces[fi]]
        if not shared:
            continue

        # open-rim guard: a boundary vertex may only collapse INTO another
        # boundary vertex, so an open rim simplifies along itself and stays
        # where the author put it.  Collapsing it into an interior vertex is
        # what pulled flat rock bottoms upward (see is_boundary above); the
        # constraint quadrics cannot prevent that on their own, because a
        # half-edge collapse is charged the SURVIVOR's quadric.
        if is_boundary[u] and not is_boundary[v]:
            continue

        # per-component floor: never grind a disconnected piece out of the
        # model, however tight the global budget is (see _COMP_MIN above).
        cr = _find(u)
        if comp_alive.get(cr, 0) <= _COMP_MIN:
            continue

        # isolation guard: decimation must never leave a triangle floating on
        # its own.  A collapse removes the faces containing edge (u,v) and
        # rewrites the rest; if that would strand any surviving neighbour as a
        # triangle sharing no edge with another live face, refuse it.  Without
        # this a low budget shreds a surface into loose confetti rather than
        # simplifying it, which reads in-game as holes with stray triangles
        # floating in them.
        _dying = {fi for fi in vert_faces[u] if v in faces[fi]}
        _isolated = False
        for fi in vert_faces[u] | vert_faces[v]:
            if fi in _dying or not face_alive[fi]:
                continue
            f = faces[fi]
            # The face as it will look after the collapse.
            nf = [v if w_ == u else w_ for w_ in f]
            if nf[0] == nf[1] or nf[1] == nf[2] or nf[0] == nf[2]:
                continue                       # degenerates away, not stranded
            shares = False
            for a_, b_ in ((nf[0], nf[1]), (nf[1], nf[2]), (nf[2], nf[0])):
                for gi in vert_faces[a_]:
                    if gi == fi or gi in _dying or not face_alive[gi]:
                        continue
                    g = [v if w_ == u else w_ for w_ in faces[gi]]
                    if a_ in g and b_ in g:
                        shares = True
                        break
                if shares:
                    break
            if not shares:
                _isolated = True
                break
        if _isolated:
            continue

        # normal-flip guard: faces of u that survive (don't contain v)
        flip = False
        ux, uy, uz = PL[u]
        vx, vy, vz = PL[v]
        for fi in vert_faces[u]:
            f = faces[fi]
            if v in f:
                continue
            i = f.index(u)
            a, b = f[(i + 1) % 3], f[(i + 2) % 3]
            if _flips(a, b, ux, uy, uz, vx, vy, vz):
                flip = True
                break
        if flip:
            continue

        # ---- perform collapse u -> v ----
        # A degenerating face can strand a THIRD vertex — not just u or v — by
        # taking its last face away.  Those have to be counted, or `alive`
        # drifts above the real vertex count and the loop keeps collapsing long
        # after the budget is met: `piratecabin01` asked for 54 vertices and
        # was ground down to 14, losing 10 of its 14 shapes.
        stranded = 0
        for fi in list(vert_faces[u]):
            f = faces[fi]
            if v in f:
                # face degenerates: remove from all its vertices
                face_alive[fi] = False
                for w_ in f:
                    had = bool(vert_faces[w_])
                    vert_faces[w_].discard(fi)
                    if had and not vert_faces[w_] and w_ != u and w_ != v:
                        stranded += 1
            else:
                i = f.index(u)
                f[i] = v
                if corner_uv is not None:
                    # Move this corner's UV along u->v by the same parameter
                    # that moves its position, so the chart follows the
                    # geometry instead of being squeezed (see cur_uv above).
                    cu = corner_uv[fi][i]
                    # Other corners of this face name the surviving verts, so
                    # their UVs are already correct; only the moved one shifts.
                    ou = _uv_at(fi, i, u, v, cu)
                    corner_uv[fi][i] = ou
                vert_faces[v].add(fi)
        vert_faces[u].clear()
        Q[v] += Q[u]
        A[v] += A[u]
        version[u] += 1
        version[v] += 1
        alive -= 1 + stranded
        comp_alive[cr] = comp_alive.get(cr, 0) - (1 + stranded)
        if not vert_faces[v]:
            alive -= 1
            comp_alive[cr] = comp_alive.get(cr, 0) - 1
            continue

        for nb in neighbors(v):
            heapq.heappush(heap, (cost_of(nb, v), nb, v, version[nb], version[v]))
            heapq.heappush(heap, (cost_of(v, nb), v, nb, version[v], version[nb]))

    # NOTE: there is deliberately NO component-pruning fallback here.
    # An earlier version dropped whole connected components smallest-area-first
    # when collapses stalled above target.  That was written when each SHAPE was
    # decimated alone, so a "component" meant a disconnected island within one
    # shape.  Now that a model is decimated as ONE welded soup, every shape is
    # its own component, and the same code deleted entire shapes to meet the
    # budget: `piratecabin01` went from 14 shapes / 2,686 verts to 4 shapes /
    # 15 verts, and `ruinshallnxdeadenda01` lost most of its geometry the same
    # way.  Overshooting the budget is far better than deleting parts of the
    # model, so the shape is simply left heavier than target when the error
    # floor genuinely blocks further collapses.

    # ---- rebuild output arrays --------------------------------------------
    # Output vertex = (surviving weld node, corner's ORIGINAL UV, material):
    # faces keep their own texture chart, seams stay intact.  The material is
    # part of the key so the per-material split afterwards never has to merge
    # or reindex charts — two materials meeting at a welded seam share the
    # POSITION (the seam cannot open) while keeping separate output vertices.
    out_map = {}
    out_v: list = []
    out_uv: list = []
    out_t = []
    out_m = []
    for fi, f in enumerate(faces):
        if not face_alive[fi]:
            continue
        mat = int(M0[fi]) if M0 is not None else 0
        idx3 = []
        for k in range(3):
            wnode = f[k]
            if corner_uv is not None:
                cu = corner_uv[fi][k]
                key = (wnode, round(float(cu[0]) * 4096),
                       round(float(cu[1]) * 4096), mat)
            else:
                key = (wnode, mat)
            j = out_map.get(key)
            if j is None:
                j = len(out_v)
                out_map[key] = j
                out_v.append(P[wnode])
                if corner_uv is not None:
                    out_uv.append(cu)
            idx3.append(j)
        if idx3[0] != idx3[1] and idx3[1] != idx3[2] and idx3[0] != idx3[2]:
            out_t.append(idx3)
            out_m.append(mat)

    if not out_t:
        return (np.zeros((0, 3), np.float32), np.zeros((0, 3), np.int32),
                np.zeros((0, 2), np.float32) if uvs is not None else None,
                np.zeros((0,), np.int32) if tri_mat is not None else None)

    nv = np.asarray(out_v, dtype=np.float32)
    nt = np.asarray(out_t, dtype=np.int32)
    nuv = np.asarray(out_uv, dtype=np.float32) if uvs is not None else None
    nm = np.asarray(out_m, dtype=np.int32) if tri_mat is not None else None
    return nv, nt, nuv, nm


def _normals(verts: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Smooth per-vertex normals averaged from face normals."""
    n_out = np.zeros_like(verts)
    v0, v1, v2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    fn = np.cross(v1 - v0, v2 - v0)
    d = np.linalg.norm(fn, axis=1, keepdims=True)
    d[d < 1e-10] = 1.0
    fn /= d
    np.add.at(n_out, tris[:, 0], fn)
    np.add.at(n_out, tris[:, 1], fn)
    np.add.at(n_out, tris[:, 2], fn)
    d2 = np.linalg.norm(n_out, axis=1, keepdims=True)
    d2[d2 < 1e-10] = 1.0
    return (n_out / d2).astype(np.float32)


def _compute_tangents(verts: np.ndarray, tris: np.ndarray,
                      uvs: np.ndarray, normals: np.ndarray
                      ) -> Tuple[np.ndarray, np.ndarray]:
    """Per-vertex tangents and bitangents via UV differentials (Gram-Schmidt)."""
    tan1 = np.zeros_like(verts)
    tan2 = np.zeros_like(verts)

    v0 = verts[tris[:, 0]];  v1 = verts[tris[:, 1]];  v2 = verts[tris[:, 2]]
    uv0 = uvs[tris[:, 0]];   uv1 = uvs[tris[:, 1]];   uv2 = uvs[tris[:, 2]]

    dv1 = v1 - v0;    dv2 = v2 - v0
    duv1 = uv1 - uv0; duv2 = uv2 - uv0

    denom = duv1[:, 0] * duv2[:, 1] - duv2[:, 0] * duv1[:, 1]
    with np.errstate(divide='ignore', invalid='ignore'):
        r = np.where(np.abs(denom) > 1e-10, 1.0 / denom, 0.0)

    t_face = r[:, None] * (duv2[:, 1:2] * dv1 - duv1[:, 1:2] * dv2)
    b_face = r[:, None] * (duv1[:, 0:1] * dv2 - duv2[:, 0:1] * dv1)

    np.add.at(tan1, tris[:, 0], t_face); np.add.at(tan1, tris[:, 1], t_face); np.add.at(tan1, tris[:, 2], t_face)
    np.add.at(tan2, tris[:, 0], b_face); np.add.at(tan2, tris[:, 1], b_face); np.add.at(tan2, tris[:, 2], b_face)

    nT = np.einsum('ij,ij->i', normals, tan1)[:, None]
    t_ortho = tan1 - nT * normals
    d_t = np.linalg.norm(t_ortho, axis=1, keepdims=True)
    d_t[d_t < 1e-10] = 1.0
    tangents = (t_ortho / d_t).astype(np.float32)
    bitangents = np.cross(normals, tangents).astype(np.float32)
    return tangents, bitangents


# ---------------------------------------------------------------------------
# NIF in-place modification (per-shape)
# ---------------------------------------------------------------------------

def _strip_node(node) -> None:
    """Remove collision, controller, and extra data from a NIF node."""
    if hasattr(node, 'collision_object'):
        node.collision_object = None
    if hasattr(node, 'controller'):
        node.controller = None
    if hasattr(node, 'num_extra_data_list'):
        node.num_extra_data_list = 0
        if hasattr(node, 'extra_data_list'):
            node.extra_data_list.update_size()


def _write_shape_geometry(shape, d_v: np.ndarray, d_t: np.ndarray,
                          d_uv: Optional[np.ndarray]) -> bool:
    """Write decimated geometry into `shape`, compacting unused vertices.

    Recomputes normals and tangents, strips vertex colors and clears the
    VertexColors SF2 bit.  Shared by the per-shape path and the whole-model
    welded path, which differ only in how the geometry was produced.
    """
    d = getattr(shape, 'data', None)
    if d is None or not isinstance(d, NifFormat.NiTriShapeData):
        return False
    if len(d_t) == 0:
        return False

    used  = np.unique(d_t)
    v_map = np.full(len(d_v), -1, dtype=np.int32)
    v_map[used] = np.arange(len(used), dtype=np.int32)
    f_v  = d_v[used]
    f_t  = v_map[d_t]
    f_uv = d_uv[used] if d_uv is not None else None
    f_n  = _normals(f_v, f_t)

    nv = len(f_v)
    nt = len(f_t)

    # --- Write geometry ---
    d.num_vertices = nv
    d.has_vertices = True
    d.vertices.update_size()
    for i, (x, y, z) in enumerate(f_v):
        d.vertices[i].x = float(x)
        d.vertices[i].y = float(y)
        d.vertices[i].z = float(z)

    d.has_normals = True
    d.normals.update_size()
    for i, (nx, ny, nz) in enumerate(f_n):
        d.normals[i].x = float(nx)
        d.normals[i].y = float(ny)
        d.normals[i].z = float(nz)

    # UVs (_ListWrap has no update_size; resize via list primitives)
    if f_uv is not None:
        try:
            inner_uv = d.uv_sets[0]
            elem_type = inner_uv._elementType
            list.clear(inner_uv)
            list.extend(inner_uv, [elem_type() for _ in range(nv)])
            for i, (u, v) in enumerate(f_uv):
                d.uv_sets[0][i].u = float(u)
                d.uv_sets[0][i].v = float(v)
        except Exception:
            f_uv = None  # fall back: no UVs

    # Vertex colors — remove
    d.has_vertex_colors = False
    if hasattr(d, 'vertex_colors'):
        d.vertex_colors.update_size()

    # Tangents + bitangents
    has_tang = bool(getattr(d, 'extra_vectors_flags', 0) & 0x10)
    if has_tang:
        if f_uv is not None:
            try:
                f_tang, f_bita = _compute_tangents(f_v, f_t, f_uv, f_n)
                d.tangents.update_size()
                for i, (tx, ty, tz) in enumerate(f_tang):
                    d.tangents[i].x = float(tx)
                    d.tangents[i].y = float(ty)
                    d.tangents[i].z = float(tz)
                d.bitangents.update_size()
                for i, (bx, by, bz) in enumerate(f_bita):
                    d.bitangents[i].x = float(bx)
                    d.bitangents[i].y = float(by)
                    d.bitangents[i].z = float(bz)
            except Exception:
                d.extra_vectors_flags = getattr(d, 'extra_vectors_flags', 0) & ~0x10
                if hasattr(d, 'tangents'):   d.tangents.update_size()
                if hasattr(d, 'bitangents'): d.bitangents.update_size()
        else:
            # No UVs — resize to new vert count with zero vectors
            if hasattr(d, 'tangents'):   d.tangents.update_size()
            if hasattr(d, 'bitangents'): d.bitangents.update_size()

    d.num_triangles       = nt
    d.num_triangle_points = nt * 3
    d.has_triangles       = True
    d.triangles.update_size()
    for i, (a, b, c) in enumerate(f_t):
        d.triangles[i].v_1 = int(a)
        d.triangles[i].v_2 = int(b)
        d.triangles[i].v_3 = int(c)

    d.consistency_flags = 0x4000  # CT_STATIC
    d.unknown_int_2     = 0

    # Remove VertexColors bit from SF2 since vertex colors are stripped
    for prop in getattr(shape, 'bs_properties', []):
        if prop is None:
            continue
        sf2 = getattr(prop, 'shader_flags_2', None)
        if sf2 is None:
            continue
        # SkyrimShaderPropertyFlags2 has no integer setter; use the named bit
        try:
            sf2.slsf_2_vertex_colors = 0
        except Exception:
            pass

    return True


def _collect_shapes(node, out: list) -> None:
    """Recursively collect all NiTriShapes in the NIF tree."""
    if node is None:
        return
    for child in getattr(node, 'children', []):
        if child is None:
            continue
        if isinstance(child, NifFormat.NiTriShape):
            out.append(child)
        elif isinstance(child, NifFormat.NiNode):
            _collect_shapes(child, out)


def _shape_world_transform(root, shape):
    """4x4 world transform of `shape` within `root`, or None if not found."""
    def walk(node, m):
        t = np.eye(4, dtype=np.float64)
        r = getattr(node, 'rotation', None)
        if r is not None:
            t[:3, :3] = [[r.m_11, r.m_12, r.m_13],
                         [r.m_21, r.m_22, r.m_23],
                         [r.m_31, r.m_32, r.m_33]]
        s = float(getattr(node, 'scale', 1.0) or 1.0)
        t[:3, :3] *= s
        tr = getattr(node, 'translation', None)
        if tr is not None:
            t[:3, 3] = [tr.x, tr.y, tr.z]
        m2 = m @ t
        if node is shape:
            return m2
        for c in getattr(node, 'children', []) or []:
            if c is None:
                continue
            got = walk(c, m2)
            if got is not None:
                return got
        return None
    return walk(root, np.eye(4, dtype=np.float64))


def _decimate_nif_inplace(nif_data, ratio: float,
                          cap: int = _NO_CAP,
                          max_dev_frac: float = _MAX_DEV_FRAC) -> bool:
    """Decimate all geometry in the NIF in-place as ONE welded topology.

    Every shape's geometry is transformed to a common (root) space and
    concatenated into a single vertex/triangle soup, tagged per triangle with
    the shape it came from.  `_qem_decimate` then welds by position, so two
    shapes meeting at a shared rim become the SAME topology node: a collapse
    there moves both sides together and the seam cannot open.  Afterwards the
    surviving triangles are split back out by material tag and written into
    their original shapes, which keeps each one's own texture and shader.

    Decimating shapes independently is what produced the holes: each side of a
    seam chose different survivors, so the rims drifted apart.  Measured on
    `centrancerockmosslg01`, shared-boundary welding fell from 32% to 9% and
    the mean gap opened from 3.8 to 93.4 units (6% of the object's diagonal);
    across 28 multi-shape Oblivion rocks, 27 lost seam welding (mean 44%->20%).

    Returns True if at least one shape survived.
    """
    # ---- collect valid shapes ---------------------------------------------
    all_shapes: list = []
    for root in nif_data.roots:
        _collect_shapes(root, all_shapes)

    valid: List[tuple] = []          # (shape, data, verts, tris, uvs, world)
    for shape in all_shapes:
        d = getattr(shape, 'data', None)
        if (d is None
                or not isinstance(d, NifFormat.NiTriShapeData)
                or d.num_vertices < 3
                or d.num_triangles < 1
                or getattr(shape, 'skin_instance', None) is not None):
            continue
        v = np.array([(p.x, p.y, p.z) for p in d.vertices], dtype=np.float64)
        t = np.array([(x.v_1, x.v_2, x.v_3) for x in d.triangles],
                     dtype=np.int32)
        if not len(t):
            continue
        uv = None
        try:
            if len(d.uv_sets) > 0 and len(d.uv_sets[0]) == d.num_vertices:
                uv = np.array([(u.u, u.v) for u in d.uv_sets[0]],
                              dtype=np.float32)
        except Exception:
            pass
        world = None
        for root in nif_data.roots:
            world = _shape_world_transform(root, shape)
            if world is not None:
                break
        valid.append((shape, d, v, t, uv, world))

    if not valid:
        return False

    # ---- build one welded soup in root space ------------------------------
    # A shape with no UVs would otherwise force the whole model to lose UVs,
    # so give it zeros and let its own chart stay degenerate; every real LOD
    # mesh here is textured.
    any_uv = any(x[4] is not None for x in valid)
    V: list = []
    T: list = []
    U: list = []
    M: list = []
    base = 0
    for mi, (shape, d, v, t, uv, world) in enumerate(valid):
        if world is not None:
            vw = (world[:3, :3] @ v.T).T + world[:3, 3]
        else:
            vw = v
        V.append(vw)
        T.append(t + base)
        M.append(np.full(len(t), mi, dtype=np.int32))
        if any_uv:
            U.append(uv if uv is not None
                     else np.zeros((len(v), 2), dtype=np.float32))
        base += len(v)

    verts = np.concatenate(V).astype(np.float32)
    tris  = np.concatenate(T).astype(np.int32)
    mats  = np.concatenate(M).astype(np.int32)
    uvs   = np.concatenate(U).astype(np.float32) if any_uv else None

    # The budget must be expressed in WELDED nodes, because that is what the
    # collapse loop counts down.  A NIF's vertex array splits a position once
    # per UV/normal seam — measured across greatforest _far.nif, 314 stored
    # vertices for 62 distinct positions, 5.0x — so a ratio applied to the
    # stored count asks for far more than exists.  That is what made the
    # far-ring tiers inert: `_TIER16`'s ratio 0.25 of the stored count works
    # out to 1.26x the welded count, so `alive > target` was false on entry,
    # the loop never ran, and `_far16.nif` was written as a byte-for-byte copy
    # of `_far.nif`.
    weld_nodes = len(np.unique(np.round(verts / _WELD_EPS).astype(np.int64),
                               axis=0))

    # ---- topology-aware budget --------------------------------------------
    # A flat share of the vertex count assumes every model simplifies equally
    # well, and they do not.  A rock is one closed blob: 12% of its vertices
    # sit on an open rim, so almost every vertex is interior and free to
    # collapse.  A building is a pile of open sheets — `piratecabin01` is 30%
    # boundary, `ruinshallnxdeadenda01` 47% — and those rim vertices are held
    # in place by the open-rim guard.  Give both the same 5% and the rock
    # lands on a clean silhouette while the building runs out of collapsible
    # interior and tears itself apart: measured on the cabin, open edges went
    # 11.5% (source) -> 21% -> 43% as the target dropped 500 -> 300 -> 54.
    #
    # So scale the budget by how much of the model is rim.  A mostly-closed
    # model keeps the base ratio; a rim-heavy one gets proportionally more
    # vertices, which is exactly what it needs to still read as itself.
    b_frac = float(is_boundary_fraction(verts, tris))
    topo_scale = 1.0 + _TOPO_BOUNDARY_WEIGHT * b_frac
    total_target = min(max(_MIN_TOTAL_TARGET,
                           int(weld_nodes * ratio * topo_scale)), cap)

    d_v, d_t, d_uv, d_m = _qem_decimate(verts, tris, uvs, total_target,
                                        max_dev_frac, tri_mat=mats)
    if d_m is None or not len(d_t):
        return False

    # ---- split back out per material --------------------------------------
    survivors = 0
    kept_shapes = set()
    for mi, (shape, d, v, t, uv, world) in enumerate(valid):
        sel = d_t[d_m == mi]
        if len(sel) < 1:
            continue
        # Back to this shape's LOCAL space: the soup was welded in root space.
        s_v = d_v
        if world is not None:
            inv = np.linalg.inv(world)
            s_v = (inv[:3, :3] @ d_v.T.astype(np.float64)).T + inv[:3, 3]
            s_v = s_v.astype(np.float32)
        if _write_shape_geometry(shape, s_v, sel, d_uv):
            kept_shapes.add(id(shape))
            survivors += 1

    if not survivors:
        return False

    # ---- prune the tree to surviving shapes -------------------------------
    for root in nif_data.roots:
        if root is None:
            continue
        _strip_node(root)
        root.flags = _NIF_FLAGS
        _prune_to_kept(root, kept_shapes)
    return True


def _prune_to_kept(node, kept_shapes: set) -> int:
    """Keep only NiTriShapes in `kept_shapes`; drop empty NiNodes."""
    keep: list = []
    survivors = 0
    for child in getattr(node, 'children', []) or []:
        if child is None:
            continue
        if isinstance(child, NifFormat.NiTriShape):
            if id(child) in kept_shapes:
                keep.append(child)
                survivors += 1
        elif isinstance(child, NifFormat.NiNode):
            _strip_node(child)
            sub = _prune_to_kept(child, kept_shapes)
            if sub > 0:
                keep.append(child)
                survivors += sub
    node.num_children = len(keep)
    node.children.update_size()
    for i, c in enumerate(keep):
        node.children[i] = c
    return survivors


# ---------------------------------------------------------------------------
# Tree billboard LOD (vanilla-style flat crossed quads)
# ---------------------------------------------------------------------------

def _write_billboard_flat_normal(path: Path, size: int = 128) -> None:
    """Write a flat-normal uncompressed DDS so billboard LOD is lit evenly."""
    import struct as _struct
    path.parent.mkdir(parents=True, exist_ok=True)
    hdr = b'DDS ' + _struct.pack('<I', 124)
    hdr += _struct.pack('<I', 0x1 | 0x2 | 0x4 | 0x1000 | 0x8)
    hdr += _struct.pack('<II', size, size)
    hdr += _struct.pack('<I', size * 4)
    hdr += _struct.pack('<II', 0, 0)
    hdr += b'\x00' * 44
    hdr += _struct.pack('<II', 32, 0x41)              # RGB | ALPHAPIXELS
    hdr += _struct.pack('<I', 0)
    hdr += _struct.pack('<I', 32)
    hdr += _struct.pack('<IIII', 0x00ff0000, 0x0000ff00, 0x000000ff, 0xff000000)
    hdr += _struct.pack('<I', 0x1000)
    hdr += _struct.pack('<IIII', 0, 0, 0, 0)
    # BGRA (255,128,128,255) = flat +Z normal
    px = bytes((255, 128, 128, 255)) * (size * size)
    path.write_bytes(hdr + px)


def _billboard_geometry(width: float, z_bottom: float, z_top: float):
    """Crossed-quad card verts/normals/uvs/tris (two quads at 90°)."""
    hw = width / 2.0
    verts = np.array([
        (-hw, 0.0, z_bottom), (hw, 0.0, z_bottom),
        (hw, 0.0, z_top),     (-hw, 0.0, z_top),
        (0.0, -hw, z_bottom), (0.0, hw, z_bottom),
        (0.0, hw, z_top),     (0.0, -hw, z_top),
    ], dtype=np.float32)
    normals = np.array([(0, 1, 0)] * 4 + [(1, 0, 0)] * 4, dtype=np.float32)
    # DDS v=0 is the top of the rendered tree
    uvs = np.array([(0, 1), (1, 1), (1, 0), (0, 0)] * 2, dtype=np.float32)
    tris = np.array([(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7)],
                    dtype=np.int32)
    return verts, normals, uvs, tris


def generate_tree_billboard_far(dst_path: Path, obnd, model_rel: str,
                                tex_root: Path) -> bool:
    """Write a crossed-quad billboard _far.nif for a TREE model.

    Uses Oblivion's own shipped billboard render
    (textures\\tes4\\trees\\billboards\\<model stem>.dds — a full-tree render
    including the trunk).  Card size comes from OBND, which the importer
    derived from the billboard dimensions, so proportions match.  Returns
    False if the billboard texture doesn't exist (caller falls back to
    geometry decimation).
    """
    stem = os.path.splitext(os.path.basename(
        model_rel.replace('\\', '/')))[0].lower()
    # Some plugins prefix their tree MESHES with load-order digits that the
    # shipped billboard TEXTURES do not carry — TWMP Valenwood/Elsweyr ships
    # `00llltreevwelmforestmosssu.nif` against `llltreevwelmforestmosssu.dds`.
    # A miss here silently falls through to geometry decimation, which is how
    # 5,471-vertex trees ended up baked into every tile that places them:
    # those four Valenwood species alone accounted for 12 GB of one Tamriel
    # bake.  So retry without a leading digit run before giving up.
    candidates = [stem]
    _bare = stem.lstrip('0123456789')
    if _bare and _bare != stem:
        candidates.append(_bare)
    for _cand in candidates:
        if win_join(tex_root, f'{_BILLBOARD_TEX_DIR}\\{_cand}.dds').exists():
            stem = _cand
            break
    else:
        return False
    diffuse_rel = f'{_BILLBOARD_TEX_DIR}\\{stem}.dds'
    normal_rel = f'{_BILLBOARD_TEX_DIR}\\{stem}_n.dds'
    normal_path = win_join(tex_root, normal_rel)
    if not normal_path.exists():
        try:
            _write_billboard_flat_normal(normal_path)
        except Exception:
            return False

    width = height = z_min = 0.0
    if obnd:
        x1, y1, z1, x2, y2, z2 = obnd
        width  = float(max(x2 - x1, y2 - y1))
        height = float(z2 - z1)
        z_min  = float(z1)
    if width <= 0:
        width = 256.0
    if height <= 0:
        height, z_min = 384.0, 0.0
    # Sink the card slightly so it doesn't float on slopes (LODGen's own
    # flat-billboard code uses the same 5-unit sink).
    verts, normals, uvs, tris = _billboard_geometry(
        width, z_min - 5.0, z_min + height)

    tsd = NifFormat.NiTriShapeData()
    tsd.num_vertices = len(verts)
    tsd.has_vertices = True
    tsd.vertices.update_size()
    tsd.has_normals = True
    tsd.normals.update_size()
    tsd.num_uv_sets = 1
    tsd.uv_sets.update_size()
    for i in range(len(verts)):
        v = tsd.vertices[i]
        v.x, v.y, v.z = map(float, verts[i])
        n = tsd.normals[i]
        n.x, n.y, n.z = map(float, normals[i])
        uv = tsd.uv_sets[0][i]
        uv.u, uv.v = map(float, uvs[i])
    tsd.num_triangles = len(tris)
    tsd.num_triangle_points = len(tris) * 3
    tsd.has_triangles = True
    tsd.triangles.update_size()
    for i, (a, b, c) in enumerate(tris):
        t = tsd.triangles[i]
        t.v_1, t.v_2, t.v_3 = int(a), int(b), int(c)
    ctr = (verts.min(axis=0) + verts.max(axis=0)) / 2.0
    tsd.center.x, tsd.center.y, tsd.center.z = map(float, ctr)
    tsd.radius = float(np.linalg.norm(verts - ctr, axis=1).max())
    tsd.consistency_flags = 0x4000  # CT_STATIC

    texset = NifFormat.BSShaderTextureSet()
    texset.num_textures = 9
    texset.textures.update_size()
    texset.textures[0] = f'textures\\{diffuse_rel}'.encode()
    texset.textures[1] = f'textures\\{normal_rel}'.encode()

    shader = NifFormat.BSLightingShaderProperty()
    shader.texture_set = texset
    shader.uv_scale.u = 1.0
    shader.uv_scale.v = 1.0
    shader.glossiness = 1.0
    shader.specular_strength = 0.0
    shader.alpha = 1.0
    shader.emissive_multiple = 1.0
    shader.texture_clamp_mode = 3
    shader.shader_flags_1.slsf_1_z_buffer_test = 1
    shader.shader_flags_1.slsf_1_specular = 0
    shader.shader_flags_2.slsf_2_z_buffer_write = 1
    shader.shader_flags_2.slsf_2_double_sided = 1

    alpha = NifFormat.NiAlphaProperty()
    alpha.flags = 4844        # alpha testing, GREATER (LODGen's own value)
    alpha.threshold = 128

    shape = NifFormat.NiTriShape()
    shape.name = b'TreeBillboard'
    shape.flags = _NIF_FLAGS
    shape.data = tsd
    shape.bs_properties.update_size()
    shape.bs_properties[0] = shader
    shape.bs_properties[1] = alpha
    try:
        shape.update_tangent_space(as_extra=False)
    except Exception:
        pass

    root = NifFormat.BSFadeNode()
    root.name = (stem + '_far').encode('latin1')
    root.flags = _NIF_FLAGS
    root.num_children = 1
    root.children.update_size()
    root.children[0] = shape

    data = NifFormat.Data()
    data.version = _SKYRIM_VER
    data.user_version = 12
    data.user_version_2 = 83
    data.header.endian_type = 1
    data.roots = [root]
    buf = io.BytesIO()
    try:
        data.write(buf)
    except Exception:
        return False

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_path.write_bytes(buf.getvalue())
    marker = dst_path.with_suffix('.nif.generated')
    marker.write_text('generated by lod_far_gen (tree billboard)\n',
                      encoding='utf-8')
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Bounded per-process error visibility: _far.nif generation runs across many
# worker processes and thousands of models, so an unbounded print per failure
# would flood the log -- but printing NOTHING (the previous behaviour) left
# "generated 0 (77 failed)" with no way to diagnose it. Same cap pattern as
# bsa_extract.py's own error reporting.
_far_nif_errors_reported = 0


def _report_far_nif_error(what: str, exc: Exception) -> None:
    global _far_nif_errors_reported
    if _far_nif_errors_reported >= 5:
        return
    _far_nif_errors_reported += 1
    print(f'    _far.nif error ({what}): {type(exc).__name__}: {exc}')


def generate_far_nif(src_path: Path, dst_path: Path,
                     decimate_ratio: float = _DECIMATE_RATIO,
                     cap: int = _NO_CAP,
                     max_dev_frac: float = _MAX_DEV_FRAC) -> bool:
    """Generate dst_path (_far.nif) by decimating each shape in src_path.

    Only processes NIFs already in Skyrim format (v20.2.0.7).
    Each shape retains its original BSLightingShaderProperty (correct flags
    and textures).  Returns True on success, False on skip/failure.

    A marker file <dst_path>.generated is written alongside the NIF so the
    pipeline knows this file was auto-generated (and may be overwritten on
    subsequent runs) rather than being a hand-crafted LOD mesh.
    """
    if not src_path.exists():
        return False

    nif_data = _read_skyrim_nif(src_path)
    if nif_data is None:
        return False
    return _decimate_and_write(nif_data, src_path.stem, dst_path,
                               decimate_ratio, cap, max_dev_frac)


def _read_skyrim_nif(src_path: Path):
    """Parse a Skyrim-version NIF, or None if unreadable/wrong version.

    PyFFI's reader is ~65% of all _far.nif generation time (it builds a Python
    object per struct field), so callers that need several outputs from one
    source should read ONCE and reuse the parsed tree.
    """
    nif_data = NifFormat.Data()
    try:
        with open(src_path, 'rb') as fh:
            nif_data.inspect(fh)
            if nif_data.version != _SKYRIM_VER:
                return None
            nif_data.read(fh)
    except Exception as exc:
        _report_far_nif_error(f'read {src_path}', exc)
        return None
    return nif_data


def _strip_parallax(nif_data) -> int:
    """Clear the heightmap shader from a mesh about to be written as LOD.

    🔴 A distant-LOD mesh must never carry parallax, and this is the only place
    that can guarantee it. `_decimate_and_write` reduces the FULL model in
    place and copies its shader properties verbatim — so a parallax source
    hands its shader type 3, its `SLSF1_Parallax` flag and its slot-3 height
    map straight to the LOD tier, while the decimation rebuilds the geometry
    and drops the vertex colors that shader requires. The result renders
    unlit-black.

    Found by `parallax_check.py verify`: 60 malformed shapes, every one in a
    `_far`/`_far8`/`_far16` mesh, all reported "no vertex colors". Skipping
    parallax when CONVERTING a source `_far.nif` fixes only half of it; a tier
    DERIVED from a parallax full model needs this.

    It is also pointless work even when it renders: a per-pixel height offset
    at LOD distance resolves to nothing, and LODGen bakes these into `.bto`.
    """
    cleared = 0
    for block in nif_data.blocks:
        if not isinstance(block, NifFormat.BSLightingShaderProperty):
            continue
        touched = False
        if int(block.skyrim_shader_type) == 3:      # SHADER_TYPE_HEIGHTMAP
            block.skyrim_shader_type = 0            # back to Default
            touched = True
        if int(block.shader_flags_1.slsf_1_parallax):
            block.shader_flags_1.slsf_1_parallax = 0
            touched = True
        ts = block.texture_set
        if ts is not None and len(ts.textures) > 3 and bytes(ts.textures[3]):
            ts.textures[3] = b''
            touched = True
        cleared += bool(touched)
    return cleared


def _decimate_and_write(nif_data, src_stem: str, dst_path: Path,
                        decimate_ratio: float, cap: int,
                        max_dev_frac: float) -> bool:
    """Decimate an already-parsed NIF in place and write it to dst_path."""
    if not _decimate_nif_inplace(nif_data, decimate_ratio, cap, max_dev_frac):
        return False
    return _write_decimated(nif_data, src_stem, dst_path)



def _write_decimated(nif_data, src_stem: str, dst_path: Path) -> bool:
    """Write an already-decimated NIF to dst_path (+ its .generated marker)."""
    # Whatever this was derived from, it ships as LOD — never with parallax.
    # Here rather than in _decimate_and_write so BOTH callers are covered: the
    # coarser `_far8`/`_far16` tiers are written straight through this.
    _strip_parallax(nif_data)

    # Rename root to <stem>_far
    for root in nif_data.roots:
        if root is not None:
            root.name = ((src_stem + '_far').encode('latin1')
                         if not src_stem.endswith('_far')
                         else src_stem.encode('latin1'))

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    try:
        nif_data.write(buf)
    except Exception as exc:
        _report_far_nif_error(f'write {dst_path}', exc)
        return False

    with open(dst_path, 'wb') as fh:
        fh.write(buf.getvalue())

    # Write marker so regen passes know this is auto-generated
    marker = dst_path.with_suffix('.nif.generated')
    marker.write_text('generated by lod_far_gen\n', encoding='utf-8')
    return True


def _is_generated(far_path: Path) -> bool:
    """Return True if far_path was written by generate_far_nif (has marker)."""
    return far_path.with_suffix('.nif.generated').exists()


def is_tree_model(stat: dict) -> bool:
    """True if this stat should get billboard tree LOD."""
    if stat.get('sig') == 'TREE':
        return True
    rel = stat.get('model', '').lower().replace('/', '\\').lstrip('\\')
    if rel.startswith('meshes\\'):
        rel = rel[len('meshes\\'):]
    return rel.startswith(_TREE_MODEL_PREFIX)


def generate_missing_far_nifs(stats: dict, output_meshes_dir: Path,
                               referenced_models: 'set | None' = None,
                               workers: int = None,
                               force_regen_generated: bool = False,
                               tex_root: 'Path | None' = None) -> int:
    """Generate _far.nif files for all LOD-flagged stats that lack one.

    TREE-type stats get a crossed-quad billboard card (Oblivion's shipped
    billboard render); everything else is QEM-decimated from the full mesh.

    Args:
        stats:                  {form_id: {flags, model, ...}} from lod_gen._parse_esm()
        output_meshes_dir:      e.g. output/Oblivion.esm/meshes/
        referenced_models:      If provided, only generate for models in this set.
        workers:                Process count; defaults to cpu_count - 1.
        force_regen_generated:  If True, regenerate files that were previously
                                auto-generated (have a .nif.generated marker).
                                Hand-crafted _far.nif files (no marker) are
                                never overwritten.
        tex_root:               textures/ root (for billboard lookup); defaults
                                to <output_meshes_dir>/../textures.

    Returns the number of _far.nif files successfully created.
    """
    from .lod_gen import (_FLAG_DISTANT_LOD, _FLAG_WORLD_MAP, _far_nif_path,
                          _LOD8_MIN_SIZE, _obnd_max_dim)
    import multiprocessing as mp

    if workers is None:
        workers = worker_count()
    if tex_root is None:
        tex_root = output_meshes_dir.parent / 'textures'

    tasks: List[tuple] = []
    seen: set = set()

    for stat in stats.values():
        flags = stat.get('flags', 0)
        if not (flags & (_FLAG_DISTANT_LOD | _FLAG_WORLD_MAP)):
            continue
        model = stat.get('model', '')
        if not model or model in seen:
            continue
        seen.add(model)

        if referenced_models is not None and model not in referenced_models:
            continue

        # Resolve to filesystem paths
        rel = model.lower().replace('/', '\\').lstrip('\\')
        if rel.startswith('meshes\\'):
            rel = rel[len('meshes\\'):]
        src = win_join(output_meshes_dir, rel)

        far_rel = _far_nif_path(rel.replace('\\', '/')).replace('/', '\\')
        dst = win_join(output_meshes_dir, far_rel)

        far_exists = dst.exists()
        if far_exists:
            if not force_regen_generated:
                continue  # skip — we have a _far.nif and aren't forcing regen
            if not _is_generated(dst):
                continue  # skip — hand-crafted, never overwrite

        tree = is_tree_model(stat)
        if not src.exists() and not tree:
            continue  # source doesn't exist yet

        # Which far-ring tiers does this object need?  (Trees reuse their
        # billboard at every level, so they never need tier meshes.)
        need8  = (not tree) and _obnd_max_dim(stat) >= _LOD8_MIN_SIZE
        need16 = (not tree) and bool(flags & _FLAG_WORLD_MAP)

        tasks.append((src, dst, tree, stat.get('obnd'), rel, tex_root,
                      need8, need16))

    if not tasks:
        print(f'  LOD: all {len(seen)} unique models already have _far.nif')
        return 0

    print(f'  LOD: generating {len(tasks)} _far.nif files with {workers} workers...')
    success = failed = 0

    if workers <= 1:
        for task in tasks:
            if _far_nif_worker(task):
                success += 1
            else:
                failed += 1
    else:
        # Use multiprocessing.Pool for true CPU parallelism (PyFFI is GIL-bound)
        with mp.Pool(processes=workers) as pool:
            for ok in pool.imap_unordered(_far_nif_worker, tasks, chunksize=8):
                if ok:
                    success += 1
                else:
                    failed += 1

    print(f'  LOD: generated {success} _far.nif files ({failed} failed/skipped)')
    return success


def _tier_path(far_path: Path, suffix: str) -> Path:
    """foo_far.nif → foo<suffix>.nif (e.g. foo_far8.nif)."""
    stem = far_path.stem
    if stem.endswith('_far'):
        stem = stem[:-len('_far')]
    return far_path.with_name(stem + suffix + '.nif')


def _render_missing_billboard(src: Path, model_rel: str, tex_root: Path) -> bool:
    """Render `textures\\tes4\\trees\\billboards\\<stem>.dds` for a tree.

    Oblivion ships these; plugins often do not, and a tree without one used to
    be decimated as full geometry.  Rendering it here keeps the billboard path
    whole, so no tree ever reaches the simplifier.  Written next to the shipped
    ones so the normal lookup finds it on the very next call.
    """
    from .tree_billboard import (render_billboard, write_dds_rgba,
                                 BILLBOARD_DIR)
    if not Path(src).exists():
        return False
    stem = os.path.splitext(os.path.basename(
        str(model_rel).replace('\\', '/')))[0]
    dst = win_join(tex_root, BILLBOARD_DIR + '\\' + stem + '.dds')
    if dst.exists():
        return True
    try:
        # Sibling texture trees resolve a plugin's leaves against its master's.
        roots = [Path(tex_root)]
        parent = Path(tex_root).parent.parent
        if parent.is_dir():
            roots += [d / 'textures' for d in sorted(parent.iterdir())
                      if (d / 'textures').is_dir()
                      and (d / 'textures') != Path(tex_root)]
        img = render_billboard(Path(src), roots, 512)
        if img is None:
            return False
        write_dds_rgba(img, dst)
        return True
    except Exception as exc:
        _report_far_nif_error('billboard %s' % stem, exc)
        return False


def _far_nif_worker(args: tuple) -> bool:
    """Top-level worker for multiprocessing.Pool — must be picklable."""
    src, dst, tree, obnd, model_rel, tex_root, need8, need16 = args
    if tree:
        if generate_tree_billboard_far(dst, obnd, model_rel, tex_root):
            return True
        # No shipped billboard for this tree — RENDER one rather than falling
        # through to decimation.  Decimating a canopy is catastrophic at LOD
        # scale: the card is 8 verts, the decimated tree is 25-330 KB, and it
        # is baked once per placement.  Censused across the load order, 113
        # such trees accounted for 3.35 GB of baked geometry that becomes
        # 0.05 GB as cards — 63x lighter — and one of them
        # (`dementiatree10l`, 8,006 placements in a single level-16 tile)
        # drove that tile to 663 MB on its own.
        if _render_missing_billboard(src, model_rel, tex_root):
            if generate_tree_billboard_far(dst, obnd, model_rel, tex_root):
                return True
        # Still nothing to draw with — fall back to decimation.
    if not dst.exists() or _is_generated(dst):
        if not src.exists():
            return False
        if not generate_far_nif(src, dst):
            return False

    # Far-ring tiers are decimated FROM the _far.nif (also works for the
    # hand-crafted vanilla _far meshes, which are already low-poly).
    #
    # Decimation mutates the parsed tree in place, so each tier needs its own
    # parse — but they can all come from ONE disk read of the _far.nif we just
    # wrote, instead of re-reading (and re-stat'ing) the file per tier.  PyFFI
    # parsing is ~65% of this stage's runtime and 48% of all reads were these
    # tier re-reads.
    tiers = []
    if need8:
        p8 = _tier_path(dst, _TIER8['suffix'])
        if not p8.exists() or _is_generated(p8):
            tiers.append((p8, _TIER8))
    if need16:
        p16 = _tier_path(dst, _TIER16['suffix'])
        if not p16.exists() or _is_generated(p16):
            tiers.append((p16, _TIER16))
    if not tiers:
        return True

    try:
        far_bytes = dst.read_bytes()
    except OSError:
        return True

    # Vertex count of the parent _far.nif, to decide whether a tier is worth
    # shipping at all (see below).
    try:
        parent = NifFormat.Data()
        fh = io.BytesIO(far_bytes)
        parent.inspect(fh)
        parent.read(fh)
        parent_verts = _reachable_vert_count(parent)
    except Exception:
        parent_verts = 0

    for tier_path, tier in tiers:
        nif_data = NifFormat.Data()
        try:
            fh = io.BytesIO(far_bytes)
            nif_data.inspect(fh)
            if nif_data.version != _SKYRIM_VER:
                continue
            nif_data.read(fh)
        except Exception:
            continue
        if not _decimate_nif_inplace(nif_data, tier['ratio'], tier['cap'],
                                     tier['dev']):
            continue
        # A tier only earns its place if it is meaningfully lighter than the
        # mesh it would replace.  Many models are already at their floor after
        # the base pass — every component is down to its minimum and the
        # isolation guard refuses to go further — so asking for half of that
        # returns the same geometry.  Writing it anyway costs generation time
        # and ships a duplicate mesh for LODGen to bake; leaving it absent
        # makes `_lod_meshes_for` fall back to the _far.nif, which is the same
        # geometry by a shorter route.
        tier_verts = _reachable_vert_count(nif_data)
        if parent_verts and tier_verts >= parent_verts * _TIER_MIN_GAIN:
            if tier_path.exists() and _is_generated(tier_path):
                try:
                    tier_path.unlink()
                    tier_path.with_suffix('.nif.generated').unlink(
                        missing_ok=True)
                except OSError:
                    pass
            continue
        _write_decimated(nif_data, dst.stem, tier_path)
    return True


def _reachable_vert_count(nif_data) -> int:
    """Vertices actually reachable from the roots (what the writer keeps)."""
    shapes: list = []
    for root in nif_data.roots:
        if root is not None:
            _collect_shapes(root, shapes)
    total = 0
    for shape in shapes:
        d = getattr(shape, 'data', None)
        if d is not None:
            total += d.num_vertices
    return total
