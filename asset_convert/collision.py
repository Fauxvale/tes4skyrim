import math
import sys
from collections import deque
from pathlib import Path

# Apply all PyFFI patches (time.clock fix, nif.xml condition fixes) before import
from . import pyffi_monkey_patch as _patch  # noqa: F401

from pyffi.formats.nif import NifFormat

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collision_options import winding_fix_enabled  # noqa: E402

from .cms_builder import build_cms_collision

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HAVOK_SCALE = 0.1
_GAME_UNITS_PER_HAVOK = 69.9904  # Skyrim: 1 Havok unit = 69.9904 game units
# Oblivion->game unit scale, used to divide authored ragdoll MASS on creature
# blend bodies.  Must stay equal to hkx_ragdoll._OB_MASS_DIV: skeleton.nif and
# skeleton.hkx describe the SAME bodies and vanilla ships identical masses in
# both (dog total 74.00 either side).  See _convert_blend_collision.
_OB_MASS_DIV = 7.0
NIF_FLAGS = 14  # Standard Skyrim NiAVObject flags (SelectiveUpdate bits 1-3)

# ---------------------------------------------------------------------------
# Havok material conversion
# ---------------------------------------------------------------------------
# Oblivion stores materials as a small sequential enum (OblivionHavokMaterial,
# 0-31).  Skyrim stores them as CRC32 hashes of the Creation Kit material name
# (SkyrimHavokMaterial).  Passing the Oblivion int through unmapped leaves the
# engine with an unknown material (no impact sounds/decals, no stair-walk flag).
_OB_TO_SK_MATERIAL = {
    0:  3741512247,  # Stone            → SKY_HAV_MAT_STONE
    1:  3839073443,  # Cloth            → SKY_HAV_MAT_CLOTH
    2:  3106094762,  # Dirt             → SKY_HAV_MAT_DIRT
    3:  3739830338,  # Glass            → SKY_HAV_MAT_GLASS
    4:  1848600814,  # Grass            → SKY_HAV_MAT_GRASS
    5:  1288358971,  # Metal            → SKY_HAV_MAT_SOLID_METAL
    6:  2974920155,  # Organic          → SKY_HAV_MAT_ORGANIC
    7:  591247106,   # Skin             → SKY_HAV_MAT_SKIN
    8:  1024582599,  # Water            → SKY_HAV_MAT_WATER
    9:  500811281,   # Wood             → SKY_HAV_MAT_WOOD
    10: 1570821952,  # Heavy Stone      → SKY_HAV_MAT_HEAVY_STONE
    11: 2229413539,  # Heavy Metal      → SKY_HAV_MAT_HEAVY_METAL
    12: 3070783559,  # Heavy Wood       → SKY_HAV_MAT_HEAVY_WOOD
    13: 3074114406,  # Chain            → SKY_HAV_MAT_MATERIAL_CHAIN
    14: 398949039,   # Snow             → SKY_HAV_MAT_SNOW
    15: 899511101,   # Stone Stairs     → SKY_HAV_MAT_STAIRS_STONE
    16: 1461712277,  # Cloth Stairs     → SKY_HAV_MAT_STAIRS_WOOD (carpeted)
    17: 899511101,   # Dirt Stairs      → SKY_HAV_MAT_STAIRS_STONE
    18: 880200008,   # Glass Stairs     → SKY_HAV_MAT_STAIRS_GLASS
    19: 899511101,   # Grass Stairs     → SKY_HAV_MAT_STAIRS_STONE
    20: 899511101,   # Metal Stairs     → SKY_HAV_MAT_STAIRS_STONE (no metal stairs)
    21: 1461712277,  # Organic Stairs   → SKY_HAV_MAT_STAIRS_WOOD
    22: 1461712277,  # Skin Stairs      → SKY_HAV_MAT_STAIRS_WOOD
    23: 899511101,   # Water Stairs     → SKY_HAV_MAT_STAIRS_STONE
    24: 1461712277,  # Wood Stairs      → SKY_HAV_MAT_STAIRS_WOOD
    25: 899511101,   # Heavy Stone Strs → SKY_HAV_MAT_STAIRS_STONE
    26: 899511101,   # Heavy Metal Strs → SKY_HAV_MAT_STAIRS_STONE
    27: 1461712277,  # Heavy Wood Strs  → SKY_HAV_MAT_STAIRS_WOOD
    28: 899511101,   # Chain Stairs     → SKY_HAV_MAT_STAIRS_STONE
    29: 1560365355,  # Snow Stairs      → SKY_HAV_MAT_STAIRS_SNOW
    30: 1288358971,  # Elevator         → SKY_HAV_MAT_SOLID_METAL
    31: 2974920155,  # Rubber           → SKY_HAV_MAT_ORGANIC
}


def _set_havok_material(hm, value):
    """Set every material item inside a HavokMaterial struct to *value*.

    PyFFI instantiates one enum item per read context (typed as whichever
    variant matched the source version); CRC values are outside the old
    Oblivion enum's range so bypass enum validation when needed.
    """
    for it in getattr(hm, '_items', []):
        if it.__class__.__name__.endswith('HavokMaterial'):
            # NOT set_value(): PyFFI's EnumBase.set_value only logs a warning
            # and returns when the value isn't in its (old, Oblivion-era) enum
            # list.  Skyrim CRC values must be written raw.
            it._value = int(value)


def _get_havok_material(hm):
    """Return the raw material int stored in a HavokMaterial struct."""
    for it in getattr(hm, '_items', []):
        if it.__class__.__name__.endswith('HavokMaterial'):
            return int(it.get_value())
    return 0


def _convert_materials(shape, _seen=None):
    """Recursively map Oblivion havok material enums to Skyrim CRC values.

    Values ≤ 31 are Oblivion enum indices; anything larger is already a
    Skyrim CRC (idempotent — safe to call on partially converted trees).
    """
    if shape is None:
        return
    if _seen is None:
        _seen = set()
    if id(shape) in _seen:
        return
    _seen.add(id(shape))

    hm = getattr(shape, 'material', None)
    if hm is not None and hasattr(hm, '_items'):
        cur = _get_havok_material(hm)
        if 0 <= cur <= 31:
            _set_havok_material(hm, _OB_TO_SK_MATERIAL.get(cur, 3741512247))

    # Recurse into child shapes / sub-shape material carriers
    for attr in ('shape',):
        _convert_materials(getattr(shape, attr, None), _seen)
    for list_attr in ('sub_shapes',):
        subs = getattr(shape, list_attr, None)
        if subs is not None:
            for s in subs:
                _convert_materials(s, _seen)
    data = getattr(shape, 'data', None)
    if data is not None:
        subs = getattr(data, 'sub_shapes', None)
        if subs is not None:
            for s in subs:
                _convert_materials(s, _seen)

# ---------------------------------------------------------------------------
# Triangle extraction from NiTriStripsData
# ---------------------------------------------------------------------------

def _triangulate_strips(strips_data):
    """Convert NiTriStripsData strip indices to a list of (a, b, c) triangles."""
    triangles = []
    for strip in strips_data.points:
        pts = list(strip)
        flip = False
        for i in range(2, len(pts)):
            a, b, c = pts[i-2], pts[i-1], pts[i]
            if a != b and b != c and c != a:
                if not flip:
                    triangles.append((a, b, c))
                else:
                    triangles.append((a, c, b))
            flip = not flip
    return triangles


def _find_normal(verts, a, b, c):
    """Return normalised face normal for triangle (a,b,c) in a vertex list."""
    va, vb, vc = verts[a], verts[b], verts[c]
    ux, uy, uz = vb[0]-va[0], vb[1]-va[1], vb[2]-va[2]
    vx, vy, vz = vc[0]-va[0], vc[1]-va[1], vc[2]-va[2]
    nx = uy*vz - uz*vy
    ny = uz*vx - ux*vz
    nz = ux*vy - uy*vx
    mag = math.sqrt(nx*nx + ny*ny + nz*nz)
    if mag > 0:
        nx /= mag; ny /= mag; nz /= mag
    return nx, ny, nz


# ---------------------------------------------------------------------------
# Shape conversion
# ---------------------------------------------------------------------------

def _face_normal(tri):
    """Normalised face normal for a triangle given as three xyz tuples."""
    (v0, v1, v2) = tri
    ux, uy, uz = v1[0]-v0[0], v1[1]-v0[1], v1[2]-v0[2]
    vx, vy, vz = v2[0]-v0[0], v2[1]-v0[1], v2[2]-v0[2]
    nx = uy*vz - uz*vy
    ny = uz*vx - ux*vz
    nz = ux*vy - uy*vx
    mag = math.sqrt(nx*nx + ny*ny + nz*nz)
    if mag > 0:
        nx /= mag; ny /= mag; nz /= mag
    return nx, ny, nz


# Count of triangles rewound by _repair_inverted_floors (list so process
# workers can mutate it; read via inverted_floor_flip_count()).
_INVERTED_FLOOR_FLIPS = [0]


def inverted_floor_flip_count():
    """Triangles rewound by the inverted-floor repair since process start."""
    return _INVERTED_FLOOR_FLIPS[0]


# Absolute-sign tuning for _repair_inverted_floors (step 2).  Distances are in
# Skyrim havok units (1 hu = 69.9904 game units).
_VIS_RADIUS = 0.30      # trust radius for a co-located visual face (~21 gu)
_VIS_PARALLEL = 0.95    # visual face must be this aligned to count as a vote
_VIS_MARGIN = 3.0       # winning side must outweigh the other this much
_SIGN_MIN_TRIS = 4      # components smaller than this are never sign-flipped


def _tri_centroid(t):
    return ((t[0][0] + t[1][0] + t[2][0]) / 3.0,
            (t[0][1] + t[1][1] + t[2][1]) / 3.0,
            (t[0][2] + t[1][2] + t[2][2]) / 3.0)


def _orient_components(idx):
    """Make every triangle agree with its edge-neighbours.

    Two triangles sharing an edge are consistently wound if and only if they
    traverse that shared edge in OPPOSITE directions — the standard manifold
    orientation test.  A breadth-first walk of the shared-edge graph therefore
    settles the whole connected component from whichever triangle it starts
    on, with no thresholds, no geometry and no external reference.

    Returns (flip:set, comps:list[list[int]]).
    """
    edge_map = {}
    for k, t in enumerate(idx):
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            edge_map.setdefault((a, b) if a < b else (b, a), []).append(k)

    flip = set()
    seen = [False] * len(idx)
    comps = []
    for start in range(len(idx)):
        if seen[start]:
            continue
        seen[start] = True
        comp = [start]
        queue = deque([start])
        while queue:
            k = queue.popleft()
            t = idx[k]
            if k in flip:
                t = (t[0], t[2], t[1])
            for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
                for nb in edge_map.get((a, b) if a < b else (b, a), ()):
                    if nb == k or seen[nb]:
                        continue
                    nt = idx[nb]
                    edges = ((nt[0], nt[1]), (nt[1], nt[2]), (nt[2], nt[0]))
                    if (b, a) in edges:
                        pass                 # opposite direction → agrees
                    elif (a, b) in edges:
                        flip.add(nb)         # same direction → reversed
                    else:
                        continue
                    seen[nb] = True
                    comp.append(nb)
                    queue.append(nb)
        comps.append(comp)
    return flip, comps


def _component_is_closed(comp, idx):
    """True when every edge of the component is shared by exactly 2 faces."""
    cnt = {}
    for k in comp:
        t = idx[k]
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            e = (a, b) if a < b else (b, a)
            cnt[e] = cnt.get(e, 0) + 1
    return all(c == 2 for c in cnt.values())


def _component_volume(comp, idx, verts, flip):
    """Signed volume of the component (positive when wound outward)."""
    v = 0.0
    for k in comp:
        a, b, c = idx[k]
        p, q, r = verts[a], verts[b], verts[c]
        if k in flip:
            q, r = r, q
        v += (p[0] * (q[1]*r[2] - q[2]*r[1])
              - p[1] * (q[0]*r[2] - q[2]*r[0])
              + p[2] * (q[0]*r[1] - q[1]*r[0]))
    return v / 6.0


# Step-3 tuning: a near-horizontal collision face is a floor candidate.
#
# The test is COINCIDENCE, not proximity: the collision face and the render
# face must be the same surface, so the tolerances are tight.  Measured on
# exUdeUship's foredeck the walkable skin sits at dxy 0.003-0.006 / dz 0.002
# from its collision face, while the nearest DOWN skin is 0.2-1.6 away — three
# orders of magnitude of separation, so this is a wide margin, not a knife edge.
_FLOOR_FLAT = 0.85      # |nz| above this is "near-horizontal"
_FLOOR_PLANE_DZ = 0.05  # visual skin must be this co-planar in Z (~3.5 gu)
_FLOOR_XY = 0.05        # ...and this coincident in XY (~3.5 gu)


def _floor_skin_index(vdata):
    """Bucket near-horizontal visual faces into an XY grid for lookup.

    Returns {(gx, gy): [(cx, cy, cz, nz), ...]} at _FLOOR_XY cell size, so a
    collision face only tests the handful of visual faces above/below it
    instead of all of them (the whole-mesh scan is O(coll x visual) and this
    runs inside the per-mesh worker).
    """
    grid = {}
    for vc, vn in vdata:
        if abs(vn[2]) < _FLOOR_FLAT:
            continue
        gx = int(vc[0] // _FLOOR_XY)
        gy = int(vc[1] // _FLOOR_XY)
        grid.setdefault((gx, gy), []).append((vc[0], vc[1], vc[2], vn[2]))
    return grid


def _repair_inverted_walkables(tris, flip, verts, idx, vdata):
    """Flip individual down-facing floor faces the render mesh says are up.

    Step 2 decides one sign for a WHOLE component, which is right for a
    uniformly reversed surface but blind to a small patch inside a large
    component: exUdeUship's raised foredeck is 12-22 triangles inside hull
    components of 384-610, so the hull's correct faces outvote it ~1000:1 and
    the deck stays inverted (you fall through the front of the chargen ship).

    It is also blind for a different reason — the deck is a ZERO-THICKNESS
    double-sided sheet in the render mesh, so its up skin and down skin share
    a plane (measured mean z 3.6533 vs 3.6787).  Step 2 weights votes by
    1/distance, so the coincident down skin scores ~200000 against the real
    walkable skin's ~33 and "nearest face wins" picks the wrong one.

    So this step asks the question that actually matters for a floor, and asks
    it PER TRIANGLE: of the render faces COINCIDENT with this down-facing
    collision face, which way does the nearest one point?  Coincident, not
    merely nearby — the render face has to BE this surface for its normal to
    settle the question, which is what makes the double-sided sheet decidable
    (the true skin sits ~0.004 away, any other surface is 0.2+).  A face with
    no coincident render skin is a genuine underside/overhang and is left
    alone.  On exUdeUship this flips the 6 foredeck faces the player falls
    through and leaves the other 357 down-facing faces untouched.

    Measured on exUdeUship by downward raycast (the engine's own test):
    fall-through cells 10 -> 0, walkable 92 -> 102.  Vanilla control sweeps
    (400 architecture + 400 dungeon + 61 ship meshes) report 0 regressions.

    Only near-horizontal faces are considered: a wall's sidedness is not
    decidable this way and Havok single-sidedness only strands the player on
    floors.
    """
    if not vdata:
        return 0
    grid = _floor_skin_index(vdata)
    if not grid:
        return 0

    flipped = 0
    for k in range(len(idx)):
        a, b, c = idx[k]
        p, q, r = verts[a], verts[b], verts[c]
        if k in flip:
            q, r = r, q
        n = _face_normal((p, q, r))
        if n[2] > -_FLOOR_FLAT:          # only currently DOWN-facing floors
            continue
        cx = (p[0] + q[0] + r[0]) / 3.0
        cy = (p[1] + q[1] + r[1]) / 3.0
        cz = (p[2] + q[2] + r[2]) / 3.0
        gx = int(cx // _FLOOR_XY)
        gy = int(cy // _FLOOR_XY)
        best = None
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                for vx, vy, vz, vnz in grid.get((gx + ox, gy + oy), ()):
                    if abs(vz - cz) > _FLOOR_PLANE_DZ:
                        continue
                    d2 = (vx - cx)**2 + (vy - cy)**2
                    if d2 > _FLOOR_XY * _FLOOR_XY:
                        continue
                    if best is None or d2 < best[0]:
                        best = (d2, vnz)
        # The coincident render skin IS this surface, so its normal is the
        # artist's statement of which way the surface faces.  Up means the
        # collision contradicts a walkable floor; no coincident skin at all
        # means a real underside/overhang, which is left alone.
        if best is not None and best[1] > 0:
            if k in flip:
                flip.discard(k)
            else:
                flip.add(k)
            flipped += 1
    return flipped


def _component_visual_vote(comp, idx, verts, flip, vdata):
    """(agree, oppose, covered) for the component against the render mesh."""
    agree = oppose = 0.0
    covered = 0
    for k in comp:
        a, b, c = idx[k]
        p, q, r = verts[a], verts[b], verts[c]
        if k in flip:
            q, r = r, q
        n = _face_normal((p, q, r))
        cx = (p[0] + q[0] + r[0]) / 3.0
        cy = (p[1] + q[1] + r[1]) / 3.0
        cz = (p[2] + q[2] + r[2]) / 3.0
        hit = False
        for vc, vn in vdata:
            algn = n[0]*vn[0] + n[1]*vn[1] + n[2]*vn[2]
            if abs(algn) < _VIS_PARALLEL:
                continue
            dd = ((cx - vc[0])**2 + (cy - vc[1])**2 + (cz - vc[2])**2)
            if dd > _VIS_RADIUS * _VIS_RADIUS:
                continue
            hit = True
            w = 1.0 / (dd + 1e-9)
            if algn > 0:
                agree += w
            else:
                oppose += w
        if hit:
            covered += 1
    return agree, oppose, covered


def _repair_inverted_floors(tris, visual_tris=None, groups=None,
                            authored_normals=None):
    """Rewind collision triangles whose winding was reversed at the source.

    Havok mesh collision is single-sided: a face only blocks from the side its
    normal points, so a surface wound backwards is walked straight through —
    the classic "I fall through the floor" symptom.  Both Nehrim and
    Morrowind_ob re-export collision as bhkPackedNiTriStripsShape triangle
    lists, and that flatten drops the strip's alternating parity.

    STEP 0 — the AUTHORED normal (always on, no toggle).
        Every collision triangle stores the direction it is meant to face,
        independently of the winding that produces that facing.  A flatten
        reverses the winding but carries the stored normal through unchanged,
        so a triangle whose winding contradicts its own normal is damaged BY
        ITS OWN RECORD -- read off the file, not inferred.  This needs no
        adjacency, no oracle mesh and no thresholds, it is inert wherever the
        two already agree, and it is therefore safe on every plugin.

        It is also the only step that repairs VANILLA Oblivion, which is not
        clean: seIsland.nif ships 1480 of 3590 collision triangles contradicting
        their own normals, and the rocks tree measures 14.5% of decidable floor
        faces inverted.  You fall through the Shivering Isles island in vanilla
        because of it.

        Its limit is corruption that is SELF-CONSISTENT: where an exporter
        rewrote the normals to match the winding it emitted, both sources agree
        while both are wrong and nothing is left to detect.  That is the
        Morroblivion case (inuhlaaluuroomuside's 10 triangles all score dot
        +1.0 over an inverted floor), and it is what the gated steps below are
        for.

    Measured against the Oblivion originals of the same meshes (Nehrim
    re-exports assets Oblivion also ships, so the vanilla file is exact ground
    truth for what the winding should be), the damage is not scattered noise:
    reversal strictly alternates along the packed triangle order, and 97% of
    reversed triangles are explained by position within a flattened strip.
    That makes the repair a structural problem, not a guessing problem.

    STEP 1 — relative orientation (exact, no thresholds).
        Triangles sharing an edge must traverse it in opposite directions.
        BFS the shared-edge graph and flip whatever disagrees.  This undoes
        the dropped parity exactly and is completely inert on correctly wound
        input, so vanilla strip meshes pass through untouched.

    STEP 2 — absolute sign (only where step 1 cannot help).
        Step 1 makes a component self-consistent but cannot tell an outward
        surface from an inside-out one, because flipping every triangle of a
        component is also self-consistent.  A uniformly reversed floor
        (Morrowind_ob's inuhlaaluuroomuside) needs an absolute reference:

          * a CLOSED component must enclose positive volume;
          * otherwise the render mesh decides — the artist's visual winding is
            correct by construction, so collision facing opposite a co-located
            visual face is reversed.

        The visual vote requires a quorum: at least half the component's
        triangles must have seen a qualifying visual face.  Every false
        positive measured on already-correct vanilla collision came from a
        single stray facet (typically the far skin of a thin slab — an altar
        top, a shelf, a step tread) condemning a whole component; the quorum
        removes those while still deciding genuinely reversed surfaces, whose
        own render skin covers every triangle they have.

        Where neither test is decisive the component is left alone.  A lone
        down-facing surface is a perfectly valid ceiling or overhang, and
        flipping it would punch a new hole.

    Measured against the Oblivion originals (dungeons + architecture, 265
    meshes / 64k matched triangles): 99.8% of reversed triangles repaired,
    0.08% of already-correct triangles disturbed.  The previous heuristic
    (near-horizontal + coplanar-conflict + nearest-visual-face arbitration)
    scored 35.8% recall on the same corpus and left priorychapelinterior,
    skbridgesmall and rockgreatforest645lichen unwalkable.

    STEPS 1-3 ARE GATED (see collision_options); STEP 0 IS NOT.  Step 0 reads
    a fact the file states about itself, so it is always correct to apply.
    Steps 1-3 INFER the answer from adjacency, enclosed volume and the render
    mesh, and inference costs false positives: step 1 seeds each welded
    component from an arbitrary triangle and propagates that choice, so a
    component whose seed happens to be inward inverts wholesale
    (leyawiincastle02: 274 of 284 triangles flipped, 806 walkable raycast
    cells lost on a vanilla mesh).  They are worth that risk only where the
    authored normals have been destroyed and nothing else can recover the
    orientation.

    Returns (repaired_tris, n_flipped).
    """
    if not tris:
        return tris, 0

    # ---- Step 0: the authored normal.  Ungated -- see the docstring.
    authored_flip = set()
    if authored_normals and len(authored_normals) == len(tris):
        for i, (t, an) in enumerate(zip(tris, authored_normals)):
            if an is None:
                continue
            alen = math.sqrt(an[0]**2 + an[1]**2 + an[2]**2)
            if alen < 1e-6:
                continue
            n = _face_normal(t)
            if (n[0]*an[0] + n[1]*an[1] + n[2]*an[2]) / alen \
                    < _AUTHORED_NORMAL_DOT:
                authored_flip.add(i)

    if not winding_fix_enabled():
        if not authored_flip:
            return tris, 0
        out = [(t[0], t[2], t[1]) if i in authored_flip else t
               for i, t in enumerate(tris)]
        return out, len(authored_flip)

    # Weld to shared vertex indices so adjacency is discoverable.  The packed
    # list stores each triangle's corners independently, so without welding no
    # two triangles ever "share" an edge and step 1 would be a no-op.
    #
    # Welding is scoped PER GROUP (see shape_tri_groups).  Independent pieces
    # of a shape frequently touch — a bridge deck resting on its posts, a
    # stair block against a landing — and welding across that seam would fuse
    # them into one component, forcing a single orientation on both.
    if not groups or sum(groups) != len(tris):
        groups = [len(tris)]

    vmap = {}
    verts = []
    idx = []
    base = 0
    for gsize in groups:
        vmap.clear()
        for t in tris[base:base + gsize]:
            tri_i = []
            for v in t:
                k = (round(v[0], 4), round(v[1], 4), round(v[2], 4))
                i = vmap.get(k)
                if i is None:
                    i = len(verts)
                    vmap[k] = i
                    verts.append(v)
                tri_i.append(i)
            idx.append(tuple(tri_i))
        base += gsize

    flip, comps = _orient_components(idx)

    # ---- Step 2: absolute sign per component.
    vdata = []
    if visual_tris:
        for t in visual_tris:
            n = _face_normal(t)
            if n[0] or n[1] or n[2]:
                vdata.append((_tri_centroid(t), n))

    for comp in comps:
        if len(comp) < _SIGN_MIN_TRIS:
            continue
        decided = None
        if _component_is_closed(comp, idx):
            v = _component_volume(comp, idx, verts, flip)
            if abs(v) > 1e-6:
                decided = v < 0          # negative volume → inside-out
        if decided is None and vdata:
            ag, op, cov = _component_visual_vote(comp, idx, verts, flip,
                                                 vdata)
            if cov * 2 >= len(comp):     # quorum
                if op > ag * _VIS_MARGIN:
                    decided = True
                elif ag > op * _VIS_MARGIN:
                    decided = False
        if decided:
            for k in comp:
                if k in flip:
                    flip.discard(k)
                else:
                    flip.add(k)

    # ---- Step 3: per-triangle walkable repair (localised damage).
    # Steps 1-2 settle whole components; a small inverted patch inside a large
    # correctly-wound component survives both.  See _repair_inverted_walkables.
    _repair_inverted_walkables(tris, flip, verts, idx, vdata)

    if not flip:
        return tris, 0

    out = [(t[0], t[2], t[1]) if i in flip else t
           for i, t in enumerate(tris)]
    return out, len(flip)


def _set_packed_sub_shape(packed, num_vertices, sk_material, layer=1):
    """Write the single covering sub-shape onto a bhkPackedNiTriStripsShape.

    The sub-shape list MOVED between the two formats (nif.xml):

        bhkPackedNiTriStripsShape.Num Sub Shapes   until="20.0.0.5"  (Oblivion)
        hkPackedNiTriStripsData.Num Sub Shapes     since="20.2.0.7"  (Skyrim)

    So at Skyrim's 20.2.0.7 the count written on the *shape* is not even
    serialised, and the engine reads the one on the *data* block.  Writing
    only the Oblivion-side field left every fallback shape claiming zero
    sub-shapes while carrying real geometry; Skyrim's reader sizes its
    sub-part allocation from that count, then memcpys the vertex/triangle
    payload into the undersized buffer — an access violation inside
    VCRUNTIME140 on load (crash-2026-07-27/28, inucaveuplant00.nif).

    Both fields are set so the shape is correct at either version.
    """
    packed.num_sub_shapes = 1
    packed.sub_shapes.update_size()
    packed.sub_shapes[0].layer = layer
    packed.sub_shapes[0].num_vertices = num_vertices
    _set_havok_material(packed.sub_shapes[0].material, sk_material)

    data = packed.data
    if data is not None and hasattr(data, 'num_sub_shapes'):
        data.num_sub_shapes = 1
        data.sub_shapes.update_size()
        data.sub_shapes[0].layer = layer
        data.sub_shapes[0].num_vertices = num_vertices
        _set_havok_material(data.sub_shapes[0].material, sk_material)


def _ni_strips_to_packed(bhk_strips):
    """Convert bhkNiTriStripsShape → bhkPackedNiTriStripsShape.

    Combines ALL NiTriStripsData blocks (Oblivion often has multiple per shape)
    and scales vertices by 1/7 (Oblivion stores them at 7× Havok unit scale).
    Returns a bhkPackedNiTriStripsShape, or None on failure.
    """
    try:
        strips_list = list(bhk_strips.strips_data)
        if not strips_list:
            return None

        # Combine vertices and triangles from ALL NiTriStripsData blocks.
        # Oblivion bhkNiTriStripsShape can have multiple data blocks (each is a
        # separate collision piece), but bhkPackedNiTriStripsShape stores them
        # merged with a single sub-shape covering all vertices.
        all_verts = []
        all_triangles = []
        for sd in strips_list:
            offset = len(all_verts)
            block_verts = [(v.x / 7.0, v.y / 7.0, v.z / 7.0) for v in sd.vertices]
            all_verts.extend(block_verts)
            block_tris = _triangulate_strips(sd)
            all_triangles.extend(
                (a + offset, b + offset, c + offset) for a, b, c in block_tris
            )

        if not all_triangles:
            return None

        hkdata = NifFormat.hkPackedNiTriStripsData()
        hkdata.num_vertices = len(all_verts)
        hkdata.vertices.update_size()
        for i, (x, y, z) in enumerate(all_verts):
            hkdata.vertices[i].x = x
            hkdata.vertices[i].y = y
            hkdata.vertices[i].z = z

        hkdata.num_triangles = len(all_triangles)
        hkdata.triangles.update_size()
        for i, (a, b, c) in enumerate(all_triangles):
            hkdata.triangles[i].triangle.v_1 = a
            hkdata.triangles[i].triangle.v_2 = b
            hkdata.triangles[i].triangle.v_3 = c
            hkdata.triangles[i].welding_info = 0
            nx, ny, nz = _find_normal(all_verts, a, b, c)
            hkdata.triangles[i].normal.x = nx
            hkdata.triangles[i].normal.y = ny
            hkdata.triangles[i].normal.z = nz

        packed = NifFormat.bhkPackedNiTriStripsShape()
        packed.data = hkdata
        _set_packed_sub_shape(packed, len(all_verts),
                              _get_havok_material(bhk_strips.material))
        packed.scale.x = 1.0
        packed.scale.y = 1.0
        packed.scale.z = 1.0
        packed.unknown_float_1 = 0.1
        packed.unknown_float_3 = 0.1
        return packed
    except Exception:
        return None



# ---------------------------------------------------------------------------
# Mesh collision rebuild (strips/packed → vanilla-style MOPP + CMS)
# ---------------------------------------------------------------------------

def shape_tri_groups(shape):
    """Triangle-count of each independent geometry group inside `shape`.

    A mesh shape can hold several self-contained pieces — one NiTriStripsData
    block per piece, or one packed sub-shape per piece — that happen to touch
    in space without being one surface.  _shape_tri_soup concatenates them in
    this order, so these counts partition its output.

    _repair_inverted_floors needs the partition: it discovers surfaces by
    welding coincident vertices, and welding ACROSS a group boundary fuses
    two independent pieces into one component.  A single orientation is then
    forced on both, which is how the sign step inverted half of vanilla's
    SI bridges (dementiabridge01: 2 sub-shapes, 130/258 triangles flipped).
    """
    if isinstance(shape, NifFormat.bhkNiTriStripsShape):
        return [len(list(_triangulate_strips(sd)))
                for sd in shape.strips_data if sd is not None]

    if isinstance(shape, NifFormat.bhkPackedNiTriStripsShape):
        data = getattr(shape, 'data', None)
        if data is None:
            return []
        subs = getattr(shape, 'sub_shapes', None) or []
        if len(subs) < 2:
            return []
        # Sub-shapes partition the VERTEX array; a triangle belongs to the
        # sub-shape owning its vertices.  Walk the triangles in order and cut
        # whenever the owning sub-shape changes.
        bounds, run = [], 0
        for s in subs:
            run += s.num_vertices
            bounds.append(run)

        def owner(vi):
            for gi, hi in enumerate(bounds):
                if vi < hi:
                    return gi
            return len(bounds) - 1

        groups, cur, prev = [], 0, None
        for t in data.triangles:
            a, b, c = t.triangle.v_1, t.triangle.v_2, t.triangle.v_3
            if a == b or b == c or a == c:
                continue
            g = owner(a)
            if prev is not None and g != prev:
                groups.append(cur)
                cur = 0
            prev = g
            cur += 1
        if cur:
            groups.append(cur)
        return groups

    return []


def _shape_tri_soup(shape):
    """Extract (triangles_hu, sk_material) from a mesh collision shape.

    bhkNiTriStripsShape data is at game-unit scale (÷7 → Oblivion havok,
    ×_HAVOK_SCALE → Skyrim havok).  hkPackedNiTriStripsData is at 1/7
    game-unit scale already (×_HAVOK_SCALE only).  Returns None for
    non-mesh shapes (caller uses the primitive conversion path).
    """
    if isinstance(shape, NifFormat.bhkNiTriStripsShape):
        scale = _HAVOK_SCALE / 7.0
        tris = []
        for sd in shape.strips_data:
            if sd is None:
                continue
            verts = [(v.x * scale, v.y * scale, v.z * scale)
                     for v in sd.vertices]
            tris.extend((verts[a], verts[b], verts[c])
                        for a, b, c in _triangulate_strips(sd))
        if not tris:
            return None
        material = _get_havok_material(shape.material)
        if 0 <= material <= 31:
            material = _OB_TO_SK_MATERIAL.get(material, 3741512247)
        return tris, material

    if isinstance(shape, NifFormat.bhkPackedNiTriStripsShape):
        data = getattr(shape, 'data', None)
        if data is None or data.num_triangles == 0:
            return None
        verts = [(v.x * _HAVOK_SCALE, v.y * _HAVOK_SCALE, v.z * _HAVOK_SCALE)
                 for v in data.vertices]
        tris = []
        for t in data.triangles:
            a, b, c = t.triangle.v_1, t.triangle.v_2, t.triangle.v_3
            if a == b or b == c or a == c:
                continue
            tris.append((verts[a], verts[b], verts[c]))
        if not tris:
            return None
        material = 3741512247  # stone default
        if shape.num_sub_shapes > 0:
            material = _get_havok_material(shape.sub_shapes[0].material)
            if 0 <= material <= 31:
                material = _OB_TO_SK_MATERIAL.get(material, 3741512247)
        return tris, material

    return None


# A face normal and its triangle's winding must point the same way; the dot
# product of the two is +1 when they agree and -1 when the winding is
# reversed.  -0.3 is deliberately slack: it only has to separate "same
# hemisphere" from "opposite hemisphere", and a normal that is merely
# imprecise (a coarse export, a quantised value) still lands nowhere near it.
_AUTHORED_NORMAL_DOT = -0.3

# Minimum length of an averaged per-vertex normal for it to describe THIS
# triangle.  bhkNiTriStripsShape stores normals per VERTEX, and a vertex on a
# smoothed edge carries the average of every face meeting there -- averaging
# three of those yields a short vector pointing at none of them
# (mageguilddesk01's desk edge: |n| = 0.31, and trusting it rewound 9 correct
# faces).  Near unit length is the proof that all three vertices agreed, i.e.
# the face is flat-shaded and its normal really is the face normal.
_AUTHORED_NORMAL_MIN_LEN = 0.95


def _shape_tri_normals(shape):
    """Authored per-triangle normals for `shape`, parallel to _shape_tri_soup.

    A collision triangle records the direction it is supposed to face
    INDEPENDENTLY of the winding that actually produces that facing:

        hkPackedNiTriStripsData.triangles[i].normal   -- one per triangle
        NiTriStripsData.normals[]                     -- one per vertex

    When an exporter flattens triangle strips and drops the alternating
    parity, the winding is reversed but this stored normal is carried through
    untouched -- so the two disagree, and the disagreement is the AUTHORED
    record of exactly which triangles were damaged.  No adjacency walk, no
    thresholds, no oracle mesh: the file says which faces are wrong.

    Entries are None where nothing can be trusted (no normals block, a
    zero-length normal, or a per-vertex average that did not survive the
    length test), and the caller leaves those triangles alone.  The order and
    the degenerate-triangle filtering MUST match _shape_tri_soup exactly or
    normals bind to the wrong faces; the two are edited together.

    Returns None when the shape carries no usable normals at all.
    """
    if isinstance(shape, NifFormat.bhkNiTriStripsShape):
        out = []
        any_real = False
        for sd in shape.strips_data:
            if sd is None:
                continue
            tri_iter = list(_triangulate_strips(sd))
            if not getattr(sd, 'has_normals', False) or not sd.normals:
                out.extend([None] * len(tri_iter))
                continue
            norms = [(n.x, n.y, n.z) for n in sd.normals]
            for a, b, c in tri_iter:
                try:
                    na, nb, nc = norms[a], norms[b], norms[c]
                except IndexError:
                    out.append(None)
                    continue
                avg = ((na[0] + nb[0] + nc[0]) / 3.0,
                       (na[1] + nb[1] + nc[1]) / 3.0,
                       (na[2] + nb[2] + nc[2]) / 3.0)
                if (math.sqrt(avg[0]**2 + avg[1]**2 + avg[2]**2)
                        < _AUTHORED_NORMAL_MIN_LEN):
                    out.append(None)      # smoothed edge -- not this face
                    continue
                out.append(avg)
                any_real = True
        return out if any_real else None

    if isinstance(shape, NifFormat.bhkPackedNiTriStripsShape):
        data = getattr(shape, 'data', None)
        if data is None or data.num_triangles == 0:
            return None
        out = []
        any_real = False
        for t in data.triangles:
            a, b, c = t.triangle.v_1, t.triangle.v_2, t.triangle.v_3
            if a == b or b == c or a == c:
                continue              # dropped by _shape_tri_soup too
            n = (t.normal.x, t.normal.y, t.normal.z)
            if n[0] == 0.0 and n[1] == 0.0 and n[2] == 0.0:
                out.append(None)
                continue
            out.append(n)
            any_real = True
        return out if any_real else None

    return None


def _visual_tri_soup(root, max_tris=20000):
    """Render-mesh triangles under `root`, in Havok units to match collision.

    Used as the orientation oracle by _repair_inverted_floors: the artist's
    visual winding is correct by construction, so a collision face pointing
    opposite a co-located visual face is reversed.  Returns [] when the node
    has no render geometry (collision-only markers), which makes the repair
    fall back to Signal A alone.

    The scale MUST match _shape_tri_soup's output or the oracle silently does
    nothing: render vertices are in game units, while the triangle soup is in
    Skyrim havok units built from data already at 1/7 game-unit scale — hence
    _HAVOK_SCALE / 7, not _HAVOK_SCALE.  (Getting this wrong put the visual
    geometry 7× too large, so no face ever fell inside the trust radius and
    every mesh came through unrepaired.)

    Capped at max_tris because the oracle is a nearest-face search: highly
    tessellated meshes would dominate conversion time for no extra accuracy.
    """
    scale = _HAVOK_SCALE / 7.0
    if root is None:
        return []
    out = []
    try:
        for blk in root.tree():
            if not isinstance(blk, NifFormat.NiTriBasedGeom):
                continue
            data = blk.data
            if data is None:
                continue
            try:
                m = blk.get_transform(root)
                verts = []
                for v in data.vertices:
                    w = v * m
                    verts.append((w.x * scale, w.y * scale, w.z * scale))
                for (a, b, c) in data.get_triangles():
                    if a != b and b != c and a != c:
                        out.append((verts[a], verts[b], verts[c]))
            except Exception:
                continue
            if len(out) > max_tris:
                return []
    except Exception:
        return []
    return out


def _bake_body_transform_into_tris(rb, tris):
    """Fold a bhkRigidBodyT transform into the triangle soup (Skyrim hu).

    Vanilla Skyrim never pairs a transformed rigid body with MOPP/mesh
    collision (0 of 6341 vanilla CMS meshes contain bhkRigidBodyT): the
    engine's CMS/MOPP query path intermittently produces invalid shape keys
    (HK_INVALID_SHAPE_KEY → runaway hit scan → CTD) when one is present —
    every Collision Sentinel CULPRIT was a rotated-root mesh whose wrap
    pass produced bhkRigidBodyT + CMS.  So the body transform is applied to
    the vertices here and the body is demoted to a plain identity
    bhkRigidBody, exactly like vanilla static collision.

    rb.translation must already be in Skyrim havok units (the caller scales
    it before shape conversion).  Returns the transformed triangle list.
    """
    if not isinstance(rb, NifFormat.bhkRigidBodyT):
        return tris
    q = rb.rotation
    R = _m3_from_quat_xyzw(q.x, q.y, q.z, q.w)  # column-vector convention
    t = (rb.translation.x, rb.translation.y, rb.translation.z)

    def xf(v):
        return (
            R[0][0] * v[0] + R[0][1] * v[1] + R[0][2] * v[2] + t[0],
            R[1][0] * v[0] + R[1][1] * v[1] + R[1][2] * v[2] + t[1],
            R[2][0] * v[0] + R[2][1] * v[1] + R[2][2] * v[2] + t[2],
        )

    tris = [(xf(a), xf(b), xf(c)) for a, b, c in tris]
    rb.__class__ = NifFormat.bhkRigidBody
    rb.rotation.x = rb.rotation.y = rb.rotation.z = 0.0
    rb.rotation.w = 1.0
    rb.translation.x = rb.translation.y = rb.translation.z = 0.0
    return tris


def _packed_from_tris(tris, sk_material):
    """Fallback: bare bhkPackedNiTriStripsShape (no MOPP) from a hu soup.

    Only used when the Havok bridge is unavailable or rejects the mesh.
    Packed data vertices are stored at 10× havok units (1/7 game scale).
    """
    vert_index = {}
    verts = []
    idx_tris = []
    for tri in tris:
        idx = []
        for v in tri:
            key = (round(v[0], 6), round(v[1], 6), round(v[2], 6))
            i = vert_index.get(key)
            if i is None:
                i = len(verts)
                vert_index[key] = i
                verts.append(key)
            idx.append(i)
        if idx[0] == idx[1] or idx[1] == idx[2] or idx[0] == idx[2]:
            continue
        idx_tris.append(idx)
    if not idx_tris:
        return None

    packed_verts = [(x * 10.0, y * 10.0, z * 10.0) for x, y, z in verts]
    hkdata = NifFormat.hkPackedNiTriStripsData()
    hkdata.num_vertices = len(packed_verts)
    hkdata.vertices.update_size()
    for i, (x, y, z) in enumerate(packed_verts):
        hkdata.vertices[i].x = x
        hkdata.vertices[i].y = y
        hkdata.vertices[i].z = z
    hkdata.num_triangles = len(idx_tris)
    hkdata.triangles.update_size()
    for i, (a, b, c) in enumerate(idx_tris):
        hkdata.triangles[i].triangle.v_1 = a
        hkdata.triangles[i].triangle.v_2 = b
        hkdata.triangles[i].triangle.v_3 = c
        hkdata.triangles[i].welding_info = 0
        nx, ny, nz = _find_normal(packed_verts, a, b, c)
        hkdata.triangles[i].normal.x = nx
        hkdata.triangles[i].normal.y = ny
        hkdata.triangles[i].normal.z = nz

    packed = NifFormat.bhkPackedNiTriStripsShape()
    packed.data = hkdata
    _set_packed_sub_shape(packed, len(packed_verts), sk_material)
    packed.scale.x = 1.0
    packed.scale.y = 1.0
    packed.scale.z = 1.0
    packed.unknown_float_1 = 0.1
    packed.unknown_float_3 = 0.1
    return packed


# Smallest AABB extent (havok units) a mesh collision hull may have.
#
# Havok's MOPP builder access-violates on hulls a few hundredths of a havok
# unit across; cms_builder now recovers those by building the MOPP in a
# scaled-up frame (exact, see the degenerate-scale retry there), so a small
# hull is NOT by itself a reason to discard collision -- vanilla Oblivion
# clutter genuinely ships them (paintbrush01 = 0.034 hu) and it must stay
# grabbable.
#
# This threshold therefore only catches hulls that are sub-viable in the
# engine regardless of MOPP: 0.01 hu is 0.07 game units, i.e. sub-millimetre,
# far below the smallest vanilla Skyrim hull (0.179 hu) and small enough that
# nothing can ever collide with it.  Morroblivion's inucaveuplant00 (0.0098 hu
# against a 73.5-game-unit visual mesh, ~1000x too small) is the case in
# point: its collision is meaningless, so it is dropped rather than shipped.
_MIN_HULL_EXTENT = 0.01

# Mesh collisions dropped as degenerate (list so process workers can mutate).
_DEGENERATE_HULLS_DROPPED = [0]


def degenerate_hull_drop_count():
    """Mesh collision hulls dropped as sub-viable since process start."""
    return _DEGENERATE_HULLS_DROPPED[0]


def _rebuild_mesh_collision(rb, target_node):
    """Rebuild strips/packed mesh collision as vanilla MOPP+CMS (in place).

    Handles rb.shape being bhkNiTriStripsShape, bhkPackedNiTriStripsShape,
    or a stale Oblivion bhkMoppBvTreeShape wrapping either.  Bakes any
    bhkRigidBodyT transform into the geometry (body becomes plain identity
    bhkRigidBody).

    Returns True when handled, False → caller uses the primitive-shape
    conversion path, or 'drop' → caller removes the collision object
    entirely (degenerate sub-viable hull).
    """
    shape = rb.shape
    inner = shape.shape if isinstance(shape, NifFormat.bhkMoppBvTreeShape) else shape
    soup = _shape_tri_soup(inner)
    if soup is None:
        return False
    tris, sk_material = soup
    # Independent geometry groups, kept in step with the filtering below so
    # _repair_inverted_floors never welds two separate pieces together.
    groups = shape_tri_groups(inner)
    # The winding each triangle was AUTHORED to have (step 0 of the repair).
    # Filtered alongside `tris` below so the two stay index-aligned; a
    # mismatched length makes _repair_inverted_floors ignore them entirely.
    authored_normals = _shape_tri_normals(inner)
    if authored_normals is not None and len(authored_normals) != len(tris):
        authored_normals = None
    keep = [all(math.isfinite(c) for v in t for c in v) for t in tris]
    if groups and sum(groups) == len(tris) and not all(keep):
        adj, base = [], 0
        for g in groups:
            adj.append(sum(1 for i in range(base, base + g) if keep[i]))
            base += g
        groups = adj
    if authored_normals is not None and not all(keep):
        authored_normals = [n for n, k in zip(authored_normals, keep) if k]
    tris = [t for t, k in zip(tris, keep) if k]
    if not tris:
        return False

    # Degenerate hull: too small for Havok to build a MOPP over, and far too
    # small to collide with anything.  Drop it instead of shipping a shape
    # that crashes the MOPP builder and lands on the packed fallback.
    lo = [min(v[i] for t in tris for v in t) for i in range(3)]
    hi = [max(v[i] for t in tris for v in t) for i in range(3)]
    if max(hi[i] - lo[i] for i in range(3)) < _MIN_HULL_EXTENT:
        _DEGENERATE_HULLS_DROPPED[0] += 1
        return 'drop'

    # The authored normals live in the SHAPE's frame; _bake_body_transform
    # rotates the triangles into the node's.  Rotate the normals by the same
    # matrix first or every comparison is made across two different frames --
    # a 180-degree body would then read every face as reversed.  (The rotation
    # comes from a quaternion, so it never mirrors and never itself changes
    # which way a triangle winds.)
    if authored_normals and isinstance(rb, NifFormat.bhkRigidBodyT):
        q = rb.rotation
        R = _m3_from_quat_xyzw(q.x, q.y, q.z, q.w)
        authored_normals = [
            None if n is None else
            (R[0][0]*n[0] + R[0][1]*n[1] + R[0][2]*n[2],
             R[1][0]*n[0] + R[1][1]*n[1] + R[1][2]*n[2],
             R[2][0]*n[0] + R[2][1]*n[1] + R[2][2]*n[2])
            for n in authored_normals
        ]
    tris = _bake_body_transform_into_tris(rb, tris)
    tris, n_flipped = _repair_inverted_floors(
        tris, _visual_tri_soup(target_node), groups, authored_normals)
    if n_flipped:
        _INVERTED_FLOOR_FLIPS[0] += n_flipped
    mopp = build_cms_collision(tris, sk_material, NifFormat)
    if mopp is not None:
        mopp.shape.target = target_node
        rb.shape = mopp
        return True
    packed = _packed_from_tris(tris, sk_material)
    if packed is not None:
        rb.shape = packed
        return True
    return False


def demote_t_body_on_mesh_collision(data):
    """Demote bhkRigidBodyT bodies that own MOPP/CMS collision (in-place).

    For pre-made Skyrim-format assets (the Skyblivion speedtree pack pairs
    bhkRigidBodyT with bhkCompressedMeshShape — a combination vanilla Skyrim
    never ships, 0 of 6341 vanilla CMS meshes, and the engine path that
    intermittently produces invalid shape keys / CTDs).

    Pure-translation bodies (the speedtree case): the CMS chunk translations,
    big verts, bounds and the MOPP origin are shifted by t — MOPP bytecode is
    origin-relative, so no recompile is needed.  Rotated bodies fall back to
    a full decode + rebuild through the Havok bridge.  Returns the number of
    bodies demoted.
    """
    from .cms import decode_cms

    n = 0
    for blk in list(data.blocks):
        if blk.__class__ is not NifFormat.bhkRigidBodyT:
            continue
        mopp = blk.shape
        if not isinstance(mopp, NifFormat.bhkMoppBvTreeShape):
            continue
        cms = getattr(mopp, 'shape', None)
        cms_data = getattr(cms, 'data', None)
        if cms_data is None or type(cms_data).__name__ != 'bhkCompressedMeshShapeData':
            continue

        q = blk.rotation
        t = (blk.translation.x, blk.translation.y, blk.translation.z)
        rot_identity = max(abs(q.x), abs(q.y), abs(q.z),
                           abs(abs(q.w) - 1.0)) < 1e-5
        xforms_identity = all(
            max(abs(x.rotation.x), abs(x.rotation.y), abs(x.rotation.z),
                abs(abs(x.rotation.w) - 1.0)) < 1e-5
            for x in cms_data.chunk_transforms
        )

        if rot_identity and xforms_identity:
            for ch in cms_data.chunks:
                ch.translation.x += t[0]
                ch.translation.y += t[1]
                ch.translation.z += t[2]
            for bv in cms_data.big_verts:
                bv.x += t[0]
                bv.y += t[1]
                bv.z += t[2]
            for bound in (cms_data.bounds_min, cms_data.bounds_max):
                bound.x += t[0]
                bound.y += t[1]
                bound.z += t[2]
            mopp.origin.x += t[0]
            mopp.origin.y += t[1]
            mopp.origin.z += t[2]
        else:
            # Rotated body — rebuild the whole chain over transformed tris.
            R = _m3_from_quat_xyzw(q.x, q.y, q.z, q.w)
            tris = [
                tuple(
                    tuple(sum(R[i][k] * v[k] for k in range(3)) + t[i]
                          for i in range(3))
                    for v in tri
                )
                for _key, tri in decode_cms(cms_data)
            ]
            material = 3741512247
            if cms_data.num_materials > 0:
                material = int(cms_data.chunk_materials[0].material)
            new_mopp = build_cms_collision(tris, material, NifFormat)
            if new_mopp is None:
                continue  # keep the T body rather than lose collision
            new_mopp.shape.target = cms.target
            blk.shape = new_mopp

        blk.rotation.x = blk.rotation.y = blk.rotation.z = 0.0
        blk.rotation.w = 1.0
        blk.translation.x = blk.translation.y = blk.translation.z = 0.0
        blk.__class__ = NifFormat.bhkRigidBody
        n += 1
    return n


# ---------------------------------------------------------------------------
# Rigid body conversion
# ---------------------------------------------------------------------------

# Oblivion layer → Skyrim layer for the values whose meaning diverges.
# Layers 0-18 are identical in both enums (STATIC..PORTAL) and pass through.
# 19+ diverge: Oblivion 19-31 are stairs/pick layers that moved, 32 is
# OL_OTHER, and 33-56 are per-bone ragdoll layers (OL_HEAD..OL_WING) that
# Oblivion authors also used on constrained world props (cellchain01 anchor
# = OL_L_FOOT).  Skyrim reads those raw values as pick/zone layers with NO
# physical collision (42 = PATHPICK), so touching the object does nothing.
# Vanilla Skyrim constrained props (trapmace01 links) use 10 = PROPS.
_OB_TO_SKY_LAYER: dict[int, int] = {
    19: 31,   # OL_STAIRS          → SKYL_STAIRHELPER
    20: 30,   # OL_CHAR_CONTROLLER → SKYL_CHARCONTROLLER
    21: 34,   # OL_AVOID_BOX       → SKYL_AVOIDBOX
    22: 1,    # OL_UNKNOWN1        → STATIC (no equivalent)
    23: 1,    # OL_UNKNOWN2        → STATIC (no equivalent)
    24: 39,   # OL_CAMERA_PICK     → SKYL_CAMERAPICK
    25: 40,   # OL_ITEM_PICK       → SKYL_ITEMPICK
    26: 41,   # OL_LINE_OF_SIGHT   → SKYL_LINEOFSIGHT
    27: 42,   # OL_PATH_PICK       → SKYL_PATHPICK
    28: 43,   # OL_CUSTOM_PICK_1   → SKYL_CUSTOMPICK1
    29: 44,   # OL_CUSTOM_PICK_2   → SKYL_CUSTOMPICK2
    30: 45,   # OL_SPELL_EXPLOSION → SKYL_SPELLEXPLOSION
    31: 46,   # OL_DROPPING_PICK   → SKYL_DROPPINGPICK
    32: 4,    # OL_OTHER           → SKYL_CLUTTER
}
for _l in range(33, 58):   # OL_HEAD..OL_NULL (ragdoll bone layers)
    _OB_TO_SKY_LAYER[_l] = 10  # SKYL_PROPS (vanilla constrained-prop layer)


def _remap_world_filter(rb):
    """Convert the Oblivion collision filter of a world-object body (in-place).

    Remaps diverging layer values (see _OB_TO_SKY_LAYER) and zeroes the
    flags/part byte and group: bits 0-4 are biped part numbers (meaningless
    off the BIPED layers) and bit 7 is Skyrim's "Linked Group" flag —
    Oblivion chains ship 0x80|part here, vanilla Skyrim constrained props
    ship 0 and rely on the engine's per-reference group assignment.
    Creature-skeleton blend bodies do NOT go through this (their layer is
    forced to 8 BIPED in _convert_blend_collision and part numbers matter).
    """
    for hf in (getattr(rb, 'havok_col_filter', None),
               getattr(rb, 'havok_col_filter_copy', None)):
        if hf is None:
            continue
        hf.layer = _OB_TO_SKY_LAYER.get(int(hf.layer), int(hf.layer))
        hf.flags_and_part_number = 0
        hf.unknown_short = 0   # Group


def _convert_rigid_body(rb):
    """Set Skyrim-compatible rigid body flags (in-place).

    Field mapping (PyFFI ↔ newer nif.xml):
      unknown_int_1  → bhkWorldObjCInfo.Unused01     (4 bytes binary padding)
      unknown_int_2  → BroadPhaseType(1B) + Unused02 (3B padding)
      unknown_3_ints → bhkWorldObjCInfoProperty       (Data,Size,CapacityFlags)
      unknown_byte   → bhkEntityCInfo.Unused01         (padding byte)
      unknown_2_shorts → bhkRBCInfo2010.Unused01       (padding)
      havok_col_filter_copy → bhkRBCInfo2010.HavokFilter (copy of entity filter)
      unknown_6_shorts[0:2] → bhkRBCInfo2010.Unused02  (padding)
      unknown_6_shorts[2:4] → bhkRBCInfo2010.UnknownInt1 (MUST be 0!)
      unknown_6_shorts[4:6] → bhkRBCInfo2010.CollisionResponse+ProcessContactDelay
    """
    # Zero padding fields that carried Oblivion-specific data.
    # unknown_int_1 = WorldObjCInfo.Unused01 — padding, zero is safest
    rb.unknown_int_1 = 0
    # unknown_int_2 byte 0 = BroadPhaseType, bytes 1-3 = Unused02 padding.
    # BroadPhaseType 1 = BROAD_PHASE_ENTITY (standard for all shapes).
    rb.unknown_int_2 = 1  # BroadPhaseType=1, padding=0
    # unknown_3_ints = WorldObjCInfoProperty (Data=0, Size=0, CapacityFlags=0x80000000)
    rb.unknown_3_ints[0] = 0
    rb.unknown_3_ints[1] = 0
    rb.unknown_3_ints[2] = -2147483648  # 0x80000000

    # unknown_byte: bhkEntityCInfo.Unused01 — the external NIFConverter
    # sets this to 116, vanilla Skyrim NIFs also show 116.
    rb.unknown_byte = 116
    # Gravity/time factors must be 1.0 or Havok ignores the body
    rb.unknown_time_factor_or_gravity_factor_1 = 1.0
    rb.unknown_time_factor_or_gravity_factor_2 = 1.0
    rb.unknown_int_6 = 196608
    rb.unknown_int_7 = 0
    rb.unknown_int_8 = 0
    rb.unknown_int_81 = 0
    rb.unknown_int_91 = 0
    # unknown_2_shorts: static Skyrim values (per external NIFConverter)
    rb.unknown_2_shorts[0] = 29541
    rb.unknown_2_shorts[1] = 23659
    # unknown_6_shorts: Skyrim-specific values required for correct physics.
    # Elements [2] and [3] map to bhkRBCInfo2010.UnknownInt1 — a 32-bit field
    # that Oblivion values corrupt into invalid pointer 0xFFFF1301.
    # Must be zero.  Elements [0:2] and [4:6] are padding/duplicates.
    rb.unknown_6_shorts[0] = 20704
    rb.unknown_6_shorts[1] = 9444
    rb.unknown_6_shorts[2] = 0       # MUST be 0 — Skyrim interprets as pointer
    rb.unknown_6_shorts[3] = 0       # MUST be 0 — Skyrim interprets as pointer
    rb.unknown_6_shorts[4] = 60417
    rb.unknown_6_shorts[5] = 65535


# ---------------------------------------------------------------------------
# Recursive shape conversion
# ---------------------------------------------------------------------------

def _expand_multisphere(ms):
    """Expand bhkMultiSphereShape into per-sphere bhkConvexTransformShape-
    wrapped bhkSphereShapes.

    hkpMultiSphereShape is deprecated in Skyrim's Havok generation: 0 of
    17,216 vanilla meshes ship the block, and files that do (Oblivion's
    alchemy apparatus clutter) crash SSE at cell load with no crash log.
    Vanilla expresses the same thing as ConvexTransform+Sphere children in a
    list shape (e.g. clutter\\kitchen\\woodenladle01.nif).

    Sphere data arrives in Oblivion Havok units — the ×0.1 rescale happens
    here.  Returns a single wrapper for 1 sphere, a bhkListShape for several,
    or None for an empty multisphere.
    """
    mat = _get_havok_material(ms.material)
    wrappers = []
    for s in ms.spheres:
        sph = NifFormat.bhkSphereShape()
        _set_havok_material(sph.material, mat)
        sph.radius = s.radius * _HAVOK_SCALE

        cts = NifFormat.bhkConvexTransformShape()
        _set_havok_material(cts.material, mat)
        cts.unknown_float_1 = sph.radius
        for i in range(8):
            cts.unknown_8_bytes[i] = 0
        t = cts.transform
        # Identity rotation, translation in the 4th column, 4th row all
        # zeros (incl. m_44) — matches vanilla bhkConvexTransformShape.
        t.m_11 = 1.0; t.m_12 = 0.0; t.m_13 = 0.0; t.m_14 = s.center.x * _HAVOK_SCALE
        t.m_21 = 0.0; t.m_22 = 1.0; t.m_23 = 0.0; t.m_24 = s.center.y * _HAVOK_SCALE
        t.m_31 = 0.0; t.m_32 = 0.0; t.m_33 = 1.0; t.m_34 = s.center.z * _HAVOK_SCALE
        t.m_41 = 0.0; t.m_42 = 0.0; t.m_43 = 0.0; t.m_44 = 0.0
        cts.shape = sph
        wrappers.append(cts)

    if not wrappers:
        return None
    if len(wrappers) == 1:
        return wrappers[0]
    ls = NifFormat.bhkListShape()
    _set_havok_material(ls.material, mat)
    ls.num_sub_shapes = len(wrappers)
    ls.sub_shapes.update_size()
    for i, w in enumerate(wrappers):
        ls.sub_shapes[i] = w
    ls.num_unknown_ints = len(wrappers)
    ls.unknown_ints.update_size()
    for i in range(len(wrappers)):
        ls.unknown_ints[i] = 0
    return ls


def _convert_shape(shape, root_node):
    """Recursively convert an Oblivion Havok shape to Skyrim format.

    Scales all geometry/dimensions by _HAVOK_SCALE (0.1).  Top-level mesh
    collision (strips/packed/MOPP) is rebuilt in _rebuild_mesh_collision
    before this runs; the mesh branches here only serve nested occurrences
    (e.g. a strips shape inside a bhkListShape) and produce a bare packed
    shape without MOPP.
    Returns the (possibly replaced) shape.
    """
    if shape is None:
        return None

    if isinstance(shape, NifFormat.bhkBoxShape):
        shape.dimensions.x *= _HAVOK_SCALE
        shape.dimensions.y *= _HAVOK_SCALE
        shape.dimensions.z *= _HAVOK_SCALE
        shape.radius *= _HAVOK_SCALE
        shape.minimum_size = min(shape.dimensions.x, shape.dimensions.y, shape.dimensions.z)
        return shape

    if isinstance(shape, NifFormat.bhkSphereShape):
        shape.radius *= _HAVOK_SCALE
        return shape

    if isinstance(shape, NifFormat.bhkCapsuleShape):
        shape.radius   *= _HAVOK_SCALE
        shape.radius_1 *= _HAVOK_SCALE
        shape.radius_2 *= _HAVOK_SCALE
        shape.first_point.x  *= _HAVOK_SCALE
        shape.first_point.y  *= _HAVOK_SCALE
        shape.first_point.z  *= _HAVOK_SCALE
        shape.second_point.x *= _HAVOK_SCALE
        shape.second_point.y *= _HAVOK_SCALE
        shape.second_point.z *= _HAVOK_SCALE
        return shape

    if isinstance(shape, NifFormat.bhkMultiSphereShape):
        return _expand_multisphere(shape)

    # bhkConvexSweepShape: early-Oblivion (10.0.1.0) wrapper for a swept
    # convex shape (handscythe01, oar01).  Skyrim never ships it — unwrap to
    # the inner shape, which then converts normally.
    if shape.__class__.__name__ == 'bhkConvexSweepShape':
        return _convert_shape(shape.shape, root_node)

    if isinstance(shape, (NifFormat.bhkConvexTransformShape,
                           NifFormat.bhkTransformShape)):
        shape.transform.m_14 *= _HAVOK_SCALE
        shape.transform.m_24 *= _HAVOK_SCALE
        shape.transform.m_34 *= _HAVOK_SCALE
        shape.shape = _convert_shape(shape.shape, root_node)
        return shape

    if isinstance(shape, NifFormat.bhkConvexVerticesShape):
        for i in range(len(shape.vertices)):
            shape.vertices[i].x *= _HAVOK_SCALE
            shape.vertices[i].y *= _HAVOK_SCALE
            shape.vertices[i].z *= _HAVOK_SCALE
        for i in range(len(shape.normals)):
            shape.normals[i].w *= _HAVOK_SCALE
        shape.radius *= _HAVOK_SCALE
        return shape

    if isinstance(shape, NifFormat.bhkListShape):
        # Convert children; flatten any nested bhkListShape produced by child
        # conversion (e.g. multisphere expansion) — a list shape carries no
        # transform of its own so flattening is semantics-preserving, and
        # vanilla never nests list shapes.
        children = []
        for i in range(len(shape.sub_shapes)):
            c = _convert_shape(shape.sub_shapes[i], root_node)
            if isinstance(c, NifFormat.bhkListShape):
                children.extend(list(c.sub_shapes))
            elif c is not None:
                children.append(c)
        if len(children) != shape.num_sub_shapes:
            shape.num_sub_shapes = len(children)
            shape.sub_shapes.update_size()
            shape.num_unknown_ints = len(children)
            shape.unknown_ints.update_size()
            for i in range(len(children)):
                shape.unknown_ints[i] = 0
        for i, c in enumerate(children):
            shape.sub_shapes[i] = c
        return shape

    if isinstance(shape, NifFormat.bhkNiTriStripsShape):
        # Nested strips (inside a list shape) → bare packed triangle shape.
        packed = _ni_strips_to_packed(shape)
        return packed if packed is not None else shape

    if isinstance(shape, NifFormat.bhkMoppBvTreeShape):
        # Never keep the outer bhkMoppBvTreeShape with stale Oblivion MOPP
        # data: Skyrim can't load Oblivion MOPP and will silently drop the
        # collision, while the incompatible blob causes undefined behaviour.
        return _convert_shape(shape.shape, root_node)

    if isinstance(shape, NifFormat.bhkPackedNiTriStripsShape):
        # Reached only as a bhkListShape child (the standalone case is rebuilt
        # as MOPP+CMS by _rebuild_mesh_collision).  Rebuild it the same way so
        # a list child gets real Skyrim collision; if the MOPP bridge rejects
        # the geometry, at least migrate the sub-shape count to the field
        # Skyrim actually reads (see _set_packed_sub_shape) — leaving the
        # Oblivion-side count made the engine allocate zero sub-parts and then
        # memcpy the payload over unmapped memory (crash on load).
        soup = _shape_tri_soup(shape)
        if soup is not None:
            tris, sk_material = soup
            tris = [t for t in tris
                    if all(math.isfinite(c) for v in t for c in v)]
            if tris:
                lo = [min(v[i] for t in tris for v in t) for i in range(3)]
                hi = [max(v[i] for t in tris for v in t) for i in range(3)]
                if max(hi[i] - lo[i] for i in range(3)) < _MIN_HULL_EXTENT:
                    _DEGENERATE_HULLS_DROPPED[0] += 1
                    return None
                mopp = build_cms_collision(tris, sk_material, NifFormat)
                if mopp is not None:
                    mopp.shape.target = root_node
                    return mopp
                rebuilt = _packed_from_tris(tris, sk_material)
                if rebuilt is not None:
                    return rebuilt
        # Could not rebuild — repair the sub-shape count in place.
        data = getattr(shape, 'data', None)
        if (data is not None and getattr(data, 'num_sub_shapes', 0) == 0
                and getattr(data, 'num_vertices', 0) > 0):
            material = 3741512247
            if shape.num_sub_shapes > 0:
                material = _get_havok_material(shape.sub_shapes[0].material)
                if 0 <= material <= 31:
                    material = _OB_TO_SK_MATERIAL.get(material, 3741512247)
            _set_packed_sub_shape(shape, data.num_vertices, material)
        return shape

    # Unknown shape — return as-is
    return shape


# ---------------------------------------------------------------------------
# Concave clutter hull decomposition
# ---------------------------------------------------------------------------
# Oblivion clutter ships ONE convex hull per object.  A convex hull fills
# every concavity: a goblet's hull spans rim→base (the thin stem gets a fat
# cylinder of phantom collision), a pitcher's hull fills the handle gap.
# Skyrim's crosshair/activation raycast tests the Havok shape, so the pick
# region extends 2-5× beyond the visible mesh around such features.  Vanilla
# Skyrim authors compound shapes instead (glazedgoblet01 = bhkListShape of a
# cup box + stem box).  We reproduce that: recursively split the VISUAL
# vertices along the axis-aligned cut that minimises total hull volume, and
# emit a bhkListShape of per-piece convex hulls when this removes enough
# phantom volume.
# NOTE: Testing this ingame seemd to make no difference

_DECOMP_MAX_DEPTH = 3          # binary split tree → ≤ 8 pieces
_DECOMP_SPLIT_GAIN = 0.90      # accept a cut only if it removes ≥10% volume
_DECOMP_MIN_PIECE_VERTS = 8
_DECOMP_MAX_HULL_VERTS = 64


def _hull_volume(pts):
    from scipy.spatial import ConvexHull
    try:
        return ConvexHull(pts).volume
    except Exception:
        return None


def _recursive_hull_split(pts, depth):
    """Split point cloud into pieces whose hulls waste less volume.

    Returns a list of point arrays (≥1 entries).  Points near the cut plane
    are shared by both halves so piece hulls overlap slightly (no gaps).
    """
    import numpy as np
    vol = _hull_volume(pts)
    if vol is None or vol <= 0 or depth <= 0:
        return [pts]

    best = None
    for axis in range(3):
        lo, hi = pts[:, axis].min(), pts[:, axis].max()
        extent = hi - lo
        if extent * _GAME_UNITS_PER_HAVOK < 3.0:  # too thin to split
            continue
        eps = 0.02 * extent
        for frac in (0.3, 0.4, 0.5, 0.6, 0.7):
            cut = lo + frac * extent
            coords = pts[:, axis]
            # Each half must reach past the first vertex "ring" on the far
            # side of the cut, otherwise sparse vertex rows leave an unfilled
            # band of collision between the two piece hulls.
            above = coords[coords > cut]
            below = coords[coords < cut]
            reach_a = (above.min() if len(above) else cut) + eps
            reach_b = (below.max() if len(below) else cut) - eps
            a = pts[coords <= reach_a]
            b = pts[coords >= reach_b]
            if len(a) < _DECOMP_MIN_PIECE_VERTS or len(b) < _DECOMP_MIN_PIECE_VERTS:
                continue
            va = _hull_volume(a)
            vb = _hull_volume(b)
            if va is None or vb is None:
                continue
            if best is None or va + vb < best[0]:
                best = (va + vb, a, b)

    if best is None or best[0] > vol * _DECOMP_SPLIT_GAIN:
        return [pts]
    return (_recursive_hull_split(best[1], depth - 1)
            + _recursive_hull_split(best[2], depth - 1))


def _build_piece_convex_shape(pts, radius, sk_material):
    """Build a bhkConvexVerticesShape from a piece point cloud (Havok units)."""
    import numpy as np
    from scipy.spatial import ConvexHull

    hull = None
    hull_pts = None
    # Quantise to a grid to keep hull vertex counts in the vanilla range
    # (grid steps in Havok units: 0.28 / 0.56 / 1.05 game units).
    for grid in (0.004, 0.008, 0.015):
        q = np.unique(np.round(pts / grid) * grid, axis=0)
        if len(q) < 4:
            continue
        try:
            h = ConvexHull(q)
        except Exception:
            continue
        hull, hull_pts = h, q[h.vertices]
        if len(hull_pts) <= _DECOMP_MAX_HULL_VERTS:
            break
    if hull is None or len(hull_pts) < 4:
        return None

    # scipy facet equations are triangulated → dedupe coplanar planes.
    # Equation convention: n·x + d <= 0 inside (n outward unit normal).
    eqs = np.unique(np.round(hull.equations, 5), axis=0)

    shape = NifFormat.bhkConvexVerticesShape()
    _set_havok_material(shape.material, sk_material)
    shape.radius = radius
    shape.num_vertices = len(hull_pts)
    shape.vertices.update_size()
    for i, (x, y, z) in enumerate(hull_pts):
        shape.vertices[i].x = float(x)
        shape.vertices[i].y = float(y)
        shape.vertices[i].z = float(z)
        shape.vertices[i].w = 0.0
    shape.num_normals = len(eqs)
    shape.normals.update_size()
    for i, eq in enumerate(eqs):
        shape.normals[i].x = float(eq[0])
        shape.normals[i].y = float(eq[1])
        shape.normals[i].z = float(eq[2])
        # Face plane sits at n·x = -w.  Vanilla stores planes pushed out by
        # the convex radius (face dist = vertex dist + radius).
        shape.normals[i].w = float(eq[3]) - radius
    return shape


def _collect_visual_vertices(node):
    """Gather all visual mesh vertices under *node* in node-frame game units."""
    import numpy as np
    out = []

    def walk(n, M):
        if n is None:
            return
        L = np.eye(4)
        if hasattr(n, 'translation') and hasattr(n.translation, 'x'):
            r = n.rotation
            L[0, :3] = [r.m_11, r.m_12, r.m_13]
            L[1, :3] = [r.m_21, r.m_22, r.m_23]
            L[2, :3] = [r.m_31, r.m_32, r.m_33]
            L[:3, :3] *= n.scale
            L[3, :3] = [n.translation.x, n.translation.y, n.translation.z]
        M2 = L @ M
        if isinstance(n, (NifFormat.NiTriShape, NifFormat.NiTriStrips)):
            d = n.data
            if d is not None and getattr(d, 'num_vertices', 0) > 0:
                verts = np.array([[v.x, v.y, v.z] for v in d.vertices])
                out.append(verts @ M2[:3, :3] + M2[3, :3])
        elif isinstance(n, NifFormat.NiNode):
            for c in n.children:
                walk(c, M2)

    if isinstance(node, NifFormat.NiNode):
        for c in node.children:
            walk(c, np.eye(4))
    if not out:
        return None
    return np.vstack(out)


def _decompose_clutter_hull(node, hull_shape):
    """Replace a concave-filling single convex hull with a bhkListShape of
    tighter per-piece hulls rebuilt from the visual geometry.

    Returns the new bhkListShape, or None to keep the original shape.
    Only called for dynamic (mass>0) plain bhkRigidBody clutter, where the
    shape frame equals the node frame.
    """
    try:
        import numpy as np
        from scipy.spatial import ConvexHull  # noqa: F401 — availability check
    except ImportError:
        return None

    pts = _collect_visual_vertices(node)
    if pts is None or len(pts) < 24 or len(pts) > 60000:
        return None
    pts_hk = pts / _GAME_UNITS_PER_HAVOK

    # Frame/coverage sanity: the existing (already scaled) hull must roughly
    # match the visual AABB, otherwise the collision was authored to cover
    # something else (or sits in a different frame) — keep it.
    hull_pts = np.array([[v.x, v.y, v.z] for v in hull_shape.vertices])
    if len(hull_pts) < 4:
        return None
    for axis in range(3):
        v_lo, v_hi = pts_hk[:, axis].min(), pts_hk[:, axis].max()
        h_lo, h_hi = hull_pts[:, axis].min(), hull_pts[:, axis].max()
        v_ext, h_ext = v_hi - v_lo, h_hi - h_lo
        max_ext = max(v_ext, h_ext, 1e-4)
        if abs((v_lo + v_hi) - (h_lo + h_hi)) / 2 > 0.35 * max_ext:
            return None
        if not (0.6 <= (v_ext + 1e-4) / (h_ext + 1e-4) <= 1.67):
            return None

    single_vol = _hull_volume(pts_hk)
    if single_vol is None or single_vol <= 0:
        return None

    pieces = _recursive_hull_split(pts_hk, _DECOMP_MAX_DEPTH)
    if len(pieces) < 2:
        return None

    radius = max(hull_shape.radius, 0.005)
    sk_material = _get_havok_material(hull_shape.material)
    piece_shapes = []
    for piece in pieces:
        s = _build_piece_convex_shape(piece, radius, sk_material)
        if s is None:
            return None
        piece_shapes.append(s)

    list_shape = NifFormat.bhkListShape()
    _set_havok_material(list_shape.material, sk_material)
    list_shape.num_sub_shapes = len(piece_shapes)
    list_shape.sub_shapes.update_size()
    for i, s in enumerate(piece_shapes):
        list_shape.sub_shapes[i] = s
    list_shape.num_unknown_ints = len(piece_shapes)
    list_shape.unknown_ints.update_size()
    for i in range(len(piece_shapes)):
        list_shape.unknown_ints[i] = 0
    return list_shape


# ---------------------------------------------------------------------------
# Full collision conversion per-node
# ---------------------------------------------------------------------------

def _node_is_animated(node, actual_root):
    """True if this node's transform is driven by animation in this NIF.

    Sources checked (walking from the file root):
      - NiControllerSequence controlled blocks (node-name entries),
      - NiMultiTargetTransformController extra targets,
      - a NiTransformController/NiKeyframeController attached to the node.
    Used to decide whether an Oblivion MO_SYS_KEYFRAMED body stays keyframed
    in Skyrim (gate leaves, animated lids) or is an unyielding anchor/held
    trap part instead (see the motion-system comment in _convert_collision).
    """
    root = actual_root if actual_root is not None else node

    def _name_of(b):
        nm = getattr(b, 'name', b'')
        return nm.decode('latin-1') if isinstance(nm, (bytes, bytearray)) else str(nm)

    names = set()

    def _walk(n):
        if not isinstance(n, NifFormat.NiAVObject):
            return
        ctrl = getattr(n, 'controller', None)
        while ctrl is not None:
            cls = ctrl.__class__.__name__
            if cls == 'NiControllerManager':
                for seq in getattr(ctrl, 'controller_sequences', []) or []:
                    if seq is None:
                        continue
                    for cb in getattr(seq, 'controlled_blocks', []) or []:
                        try:
                            nm = cb.get_node_name()
                        except Exception:
                            nm = None
                        if nm:
                            names.add(nm.decode('latin-1')
                                      if isinstance(nm, (bytes, bytearray)) else str(nm))
            elif cls == 'NiMultiTargetTransformController':
                for t in getattr(ctrl, 'extra_targets', []) or []:
                    if t is not None:
                        names.add(_name_of(t))
            elif 'TransformController' in cls or 'KeyframeController' in cls:
                names.add(_name_of(n))
            ctrl = getattr(ctrl, 'next_controller', None)
        for c in getattr(n, 'children', []) or []:
            if c is not None:
                _walk(c)

    _walk(root)
    return _name_of(node) in names


# Oblivion collision layer 10 = OL_PROPS, the DYNAMIC clutter layer (barrels,
# cups -- everything Havok simulates and lets fall).  It is the authored
# indicator that separates a piece meant to break off and drop from one whose
# animation performs the whole motion: the artist picks the layer in the
# exporter, so this is a statement of intent, not a measurement.
#
# Census of every ms=6 + animated + mass>0 body across both plugins (44 meshes):
#   layer 10 OL_PROPS   -> falls: mwallplankbreakaway01, idcrumblewall01,
#                          cpbrick01-15, cpgenericbrick01-03, cplog01/02,
#                          artrapbridgecrumble, roperock01, rfpitbridgetrap
#   layer 2/3           -> self-actuating, MUST stay keyframed: prisoncellgate01,
#                          cgprisoncellgate01, icbastioncellgate01, argate01,
#                          rfwportcullis01(+door), arpitstairs01/03,
#                          arenclosedcircle01, dreamstairs01
#   layer 14 OL_TRAP    -> swinging traps, keyframed: ctrapcavein01,
#                          ctraplogs01, cprollingrock01, artrapchannelspikes01
# Gates swing and portcullises slide precisely because they are NOT on the
# props layer; nothing on layer 10 is expected to hold a pose against gravity.
_OL_PROPS = 10


def _node_is_breakaway(node, actual_root, rb):
    """True if this animated node is a piece that breaks off and falls.

    Oblivion authors breakaway props (mwallplankbreakaway01's planks,
    IDCrumbleWall01's bricks) as ms=6 bodies with real mass on OL_PROPS: the
    sequence only creaks them off their mounting and Havok does the rest.
    Converting them to keyframed/mass-0 pins them in the half-broken pose
    forever.  Gates and portcullises are also ms=6 with mass, but they sit on
    the anim-static/clutter layers and must keep following their clip exactly.
    """
    if not _node_is_animated(node, actual_root):
        return False
    for attr in ('havok_col_filter', 'havok_col_filter_copy'):
        hf = getattr(rb, attr, None)
        if hf is not None and int(getattr(hf, 'layer', -1)) == _OL_PROPS:
            return True
    return False


def _node_is_held_trap(node, actual_root, rb):
    """True if this body is part of a trap island Oblivion holds until scripted.

    Oblivion authors a whole swinging trap (ctrapswingmacelong01's chain links
    + mace head, ctraplogs01's logs) as ms=6 KEYFRAMED bodies with real mass
    and `Unyielding = 1`, wired together by constraints.  ITS engine keeps the
    island rigid until the trap script runs `playgroup` -- the script header
    says so outright: "On activation havok will turn on and logs will roll".

    Shipping them dynamic (the old "mass>0 and owns a constraint" rule) made
    every trap swing freely on cell load.  Shipping them mass-0 keyframed
    welds them solid forever.  They are breakaway pieces in the exact sense
    the breakaway path already models: HELD, keeping their mass, released to
    Motion_Dynamic by the converted trap script.

    Membership is island-wide, not per-body: a chain link routinely carries
    mass with num_constraints == 0 and hangs off a neighbour's constraint
    (the same reason collision_extract checks constraints file-wide).
    """
    if rb.num_constraints > 0:
        return True
    root = actual_root if actual_root is not None else node
    for blk in root.tree():
        co = getattr(blk, 'collision_object', None)
        body = getattr(co, 'body', None) if co is not None else None
        if body is not None and getattr(body, 'num_constraints', 0) > 0:
            return True
    return False


def _convert_blend_collision(node, coll_obj):
    """Convert a bhkBlendCollisionObject on a creature-skeleton bone.

    Vanilla Skyrim creature skeletons KEEP blend collision objects (dog:
    flags=137, plain bhkRigidBody with a NON-zero bone-relative translation
    in Havok units, capsule shapes, motion_system=4 KEYFRAMED,
    quality_type=1 FIXED, layer=8 BIPED).  Shape/material/2010-format fixups
    are shared with the standard path.  Inertia gets the full ×0.01
    (mass·length², lengths scale ×0.1) here, same as the standard dynamic
    path in _convert_collision.
    """
    coll_obj.flags = 137
    rb = getattr(coll_obj, 'body', None)
    if rb is None:
        return
    # Blend bodies USE their translation (bone-relative placement) — scale,
    # never zero, even for plain bhkRigidBody.
    rb.translation.x *= _HAVOK_SCALE
    rb.translation.y *= _HAVOK_SCALE
    rb.translation.z *= _HAVOK_SCALE
    rb.center.x *= _HAVOK_SCALE
    rb.center.y *= _HAVOK_SCALE
    rb.center.z *= _HAVOK_SCALE
    _convert_rigid_body(rb)
    # bhkEntityCInfo byte: prop bodies use 116/10, but the vanilla creature
    # blend-body census is 0 (COM root occasionally 3)
    rb.unknown_byte = 0
    # MASS: divide by the Oblivion->game unit scale, exactly as
    # hkx_ragdoll._OB_MASS_DIV does for the skeleton.hkx ragdoll bodies.
    #
    # **These are the SAME physical bodies described twice** — vanilla ships
    # identical masses in skeleton.nif and skeleton.hkx (dog: total 74.00 in
    # both, per-body 2-6).  The 2026-08-08 "dead creatures weigh a million
    # pounds, I can only move limbs a little" report survived fixing the hkx
    # side alone because the ENGINE WEIGHS THE NIF BLEND BODIES: they still
    # shipped Oblivion's raw values (dog total 262, per-body 25-50) while the
    # hkx said 37.4, so the two representations disagreed and the heavy one won.
    #
    # Whenever mass handling changes, it MUST change in both places or they
    # desync.  Verify with: vanilla dog nif == 74.0 total, and our output nif
    # total == our output hkx total.
    rb.mass = float(rb.mass) / _OB_MASS_DIV
    for attr in ('m_11', 'm_12', 'm_13', 'm_21', 'm_22', 'm_23',
                 'm_31', 'm_32', 'm_33'):
        setattr(rb.inertia, attr,
                getattr(rb.inertia, attr) * _HAVOK_SCALE * _HAVOK_SCALE
                / _OB_MASS_DIV)
    rb.motion_system = 4        # MO_SYS_KEYFRAMED (bone follows animation)
    rb.quality_type = 1         # MO_QUAL_FIXED
    rb.deactivator_type = 1
    # Dynamics contract (vanilla census: wolf/dog/bear/deer/sabrecat/skeever,
    # 136/136 blend bodies identical).  Skyrim APPLIES these on blend bodies
    # — same lesson as the chains/signs saga — and Oblivion ships garbage:
    # damping 5.0/5.0 froze every corpse solid mid-air (no gravity reaction,
    # immovable by havok grab), maxLinearVelocity up to 10000, restitution
    # 0.3, solverDeactivation 1, and junk in translation.w.
    rb.linear_damping = 0.0996
    rb.angular_damping = 0.0498
    rb.max_linear_velocity = 104.4
    rb.max_angular_velocity = 31.57
    rb.solver_deactivation = 2
    rb.friction = 0.3
    rb.restitution = 0.8
    rb.translation.w = 0.0
    rb.havok_col_filter.layer = 8   # SKYL_BIPED
    # Oblivion's filter byte (0x40 | part) means nothing to Skyrim — the
    # vanilla creature-skeleton census is plain small part numbers with NO
    # flag bits (wolf: all 0 across 22 bodies; bear: 0-2). Carrying the
    # Oblivion byte through left every blend body flagged.
    rb.havok_col_filter.flags_and_part_number = 0
    if hasattr(rb, 'havok_col_filter_copy'):
        rb.havok_col_filter_copy.layer = 8
        rb.havok_col_filter_copy.flags_and_part_number = 0
    rb.shape = _convert_shape(rb.shape, node)
    _convert_materials(rb.shape)


def _convert_collision(node, actual_root=None, keep_blend=False):
    """Convert all collision on a NiNode from Oblivion to Skyrim Havok format.

    Modifies node.collision_object in-place.
    actual_root: the NIF's top-level root node, used as target for
    bhkCompressedMeshShape so Skyrim reads the correct world transform.
    keep_blend: creature skeletons — convert bhkBlendCollisionObject
    (ragdoll bone collision, fully supported by Skyrim) instead of
    stripping it.
    """
    if not hasattr(node, 'collision_object') or node.collision_object is None:
        return

    # bhkBlendCollisionObject is stripped on world objects, but on creature
    # skeletons (keep_blend) it is the vanilla ragdoll-bone type.
    cls_name = node.collision_object.__class__.__name__
    if cls_name == 'bhkBlendCollisionObject':
        if keep_blend:
            _convert_blend_collision(node, node.collision_object)
        else:
            node.collision_object = None
        return
    if cls_name == 'bhkSPCollisionObject':
        # Trigger-volume phantom (tripwire triggers, gas/fire damage zones).
        # Skyrim fully supports bhkSPCollisionObject + bhkSimpleShapePhantom —
        # vanilla ships 31 of them under meshes/traps alone (traptripwire01,
        # pressure plates, bear trap...), always with collision-object
        # flags=129 and layer 12 (TRIGGER, same enum value as Oblivion).
        # Convert the inner shape (×0.1 scale + material remap) and keep it.
        body = getattr(node.collision_object, 'body', None)
        if isinstance(body, NifFormat.bhkSimpleShapePhantom):
            node.collision_object.flags = 129
            _remap_world_filter(body)
            body.shape = _convert_shape(body.shape, node)
            _convert_materials(body.shape)
        else:
            node.collision_object = None
        return
    if cls_name == 'bhkNPCollisionObject':
        node.collision_object = None
        return

    coll_obj = node.collision_object
    # Default: standard Skyrim collision flags.  Animated collision (keyframed)
    # has flags overridden below after rigid body analysis.
    coll_obj.flags = 129

    rb = coll_obj.body if hasattr(coll_obj, 'body') else None
    if rb is None:
        return

    if isinstance(rb, NifFormat.bhkSimpleShapePhantom):
        _remap_world_filter(rb)
        rb.shape = _convert_shape(rb.shape, node)
        _convert_materials(rb.shape)
        return

    # Scale rigid body translation.
    # bhkRigidBodyT uses translation/rotation for the Havok body offset; scale
    # the translation and keep the rotation.
    # bhkRigidBody (non-T): OBLIVION ignores both fields, so its files carry
    # arbitrary leftover values there.  SKYRIM APPLIES BOTH EVEN ON NON-T
    # BODIES — proven by vanilla trapmace01.nif Base01: node rotated +0.5°
    # about X, body rotation = the exact inverse quaternion (-0.0044,0,0,1)
    # so its root-space MOPP stays aligned; every other vanilla non-T body is
    # exactly identity/zero, unlike the genuinely-garbage padding fields.
    # Leftover Oblivion rotations (up to ~115° on chain links) rotated every
    # constraint frame and collision shape out from under the solver: chains/
    # swinging traps acted welded solid, and ordinary clutter collision sat
    # askew from the visual mesh ("havok interactions feel weird").
    if isinstance(rb, NifFormat.bhkRigidBodyT):
        rb.translation.x *= _HAVOK_SCALE
        rb.translation.y *= _HAVOK_SCALE
        rb.translation.z *= _HAVOK_SCALE
    else:
        rb.translation.x = 0.0
        rb.translation.y = 0.0
        rb.translation.z = 0.0
        rb.translation.w = 0.0
        rb.rotation.x = 0.0
        rb.rotation.y = 0.0
        rb.rotation.z = 0.0
        rb.rotation.w = 1.0
    rb.center.x *= _HAVOK_SCALE
    rb.center.y *= _HAVOK_SCALE
    rb.center.z *= _HAVOK_SCALE

    _convert_rigid_body(rb)
    _remap_world_filter(rb)

    # Penetration depth is a LENGTH (max allowed overlap): Oblivion ships
    # 0.15 in its Havok units; vanilla Skyrim bodies carry ~0.005-0.012.
    # Unscaled it lets contacts sink an entire chain-link deep.
    rb.penetration_depth *= _HAVOK_SCALE

    # Oblivion MO_SYS_KEYFRAMED (6) semantics are context-dependent.  Three
    # cases, discriminated per body (vanilla Skyrim census):
    #  1. Node driven by animation (gate leaves targeted by Open/Close
    #     sequences, animated display-case lids) → Skyrim KEYFRAMED, like
    #     vanilla farmhouseanimdoor01.  Keyframed is ONLY valid for animated
    #     nodes: a keyframed body with anim flags (137/142) on a non-animated
    #     object flips the engine into the baked/anim-static path and the
    #     whole compound acts welded solid.
    #  2. mass>0 AND owns a constraint (mace-trap chain links: Oblivion holds
    #     whole traps keyframed until the trap script enables havok) →
    #     DYNAMIC, like vanilla trapmace01's links (ms=3, quality 4).
    #  3. Everything else (constrained-island anchors: cellchain01 root,
    #     cellChainMiddle, mass=100 "Unyielding"; unyielding props) →
    #     STATIC with mass 0.  Vanilla chain/noose/trap anchors are ALWAYS
    #     static mass-0 bodies (NooseRopePiece01 root, trapmace Base01),
    #     never keyframed.
    #  4. BREAKAWAY pieces (mwallplankbreakaway01's 8 planks, IDCrumbleWall01's
    #     bricks): ms=6 bodies with real mass whose clip only creaks them off
    #     their mounting -- 15.19 deg and ZERO translation keys for the planks
    #     -- because the visible break is HAVOK taking over and letting the
    #     pieces detach and FALL.  Forcing those onto the plain keyframed path
    #     (which also zeroes the mass) pinned them forever: the clip played, the
    #     planks tilted, and then hung in the half-broken pose as a solid wall.
    #     Gates/portcullises are also ms=6 with mass but sit on the anim-static
    #     layers, so the authored OL_PROPS layer separates the two.
    #
    #     A breakaway piece still ships KEYFRAMED, exactly like Oblivion's
    #     `Unyielding = 1` (all 8 planks; the root is Unyielding 0 / mass 0):
    #     the body is HELD, following the clip, and only becomes dynamic when
    #     the animation ends.  Shipping it dynamic instead made the planks drop
    #     the instant the cell loaded, before the clip ever played.  What the
    #     breakaway flag changes is that the piece KEEPS ITS MASS, so the
    #     script-side release (ObjectReference.SetMotionType(Motion_Dynamic))
    #     hands Havok a body that can actually fall -- a mass-0 body would just
    #     hang there.  See script_convert PlayGroup handling.
    #  5. HELD TRAP islands (ctrapswingmacelong01's chain + mace,
    #     ctraplogs01's logs, cprollingrock01): ms=6 bodies with real mass
    #     that belong to a CONSTRAINED island.  Case 2 shipped these DYNAMIC,
    #     which is what made every swinging trap swing freely the moment the
    #     cell loaded, before anything tripped it.  Oblivion's own trap script
    #     states the contract in its header comment -- "On activation havok
    #     will turn on and logs will roll" (CTrapLogs01SCRIPT) -- and authors
    #     the whole island `Unyielding = 1`: the trap is HELD rigid until the
    #     trap script fires, exactly like a breakaway piece.
    #
    #     So a constrained trap island is a breakaway: ship it KEYFRAMED (held,
    #     not simulating) but KEEP the authored mass, and let the script-side
    #     SetMotionType(Motion_Dynamic) release start the swing.  Skyrim's own
    #     trapmace01 ships its links dynamic because a Skyrim trap has no
    #     script-held phase; ours must reproduce Oblivion's held phase instead.
    breakaway_body = (rb.motion_system == 6 and rb.mass > 0
                      and (_node_is_breakaway(node, actual_root, rb)
                           or _node_is_held_trap(node, actual_root, rb)))
    keyframed_body = (rb.motion_system == 6
                      and (_node_is_animated(node, actual_root)
                           or breakaway_body))
    if rb.motion_system == 6 and not keyframed_body:
        rb.mass = 0.0               # case 3: falls into the static branch

    # Oblivion MO_SYS_FIXED (7) is the explicit "static element of the scene"
    # motion type (nif.xml: landscape/architecture).  The static-vs-dynamic
    # branch below dispatches on mass alone, which silently misreads any fixed
    # body whose mass field is non-zero as clutter: Skyrim then simulates a
    # 1000 kg mesh-collision prop, and it tips onto its side, sinks, or spins
    # off through the air on cell load.
    #
    # Base Oblivion hid the bug — a 300-NIF census found 198 ms=7 bodies with
    # mass EXACTLY 0 (0 non-zero), so mass alone happened to classify all of
    # them right.  Third-party content does not follow that convention: the
    # same census over Morroblivion found 186 ms=7 bodies of which 157 carry
    # a junk mass (1000.0 is its idiom for "static"), i.e. the majority of its
    # statics were being converted into dynamic clutter.
    #
    # A fixed body that owns a constraint is left alone: it is a real
    # trap/chain part whose island the constraint branches handle.
    if rb.motion_system == 7 and rb.num_constraints == 0:
        rb.mass = 0.0               # falls into the static branch
    if keyframed_body:
        # Skyrim animated doors/activators: the collision body follows the
        # NiNode animation exactly (keyframed).  Values sourced from vanilla
        # Skyrim farmhouseanimdoor01.nif.
        coll_obj.flags      = 137  # 0x89 = ACTIVE | D_ANIMATED | bit 7
        rb.motion_system    = 4    # MO_SYS_KEYFRAMED
        rb.deactivator_type = 1
        # quality_type / solver_deactivation are NOT changed by the runtime
        # release: `SetMotionType(Motion_Dynamic)` swaps only the motion type,
        # so whatever ships in the NIF is what the body simulates with AFTER a
        # breakaway/held trap is let go.
        #
        #  * A plain animated body (door, gate, portcullis) is never released
        #    and its position is fully deterministic -> MO_QUAL_FIXED with
        #    solver deactivation OFF, sourced from vanilla
        #    farmhouseanimdoor01.nif.
        #  * A BREAKAWAY / HELD-TRAP body becomes a real moving object the
        #    instant the script releases it, and it does so while carrying
        #    mass inside a live constraint island.  MO_QUAL_FIXED there tells
        #    Havok the body is static, so the released chain is a ring of
        #    mass-bearing constrained bodies all claiming to be static with
        #    deactivation disabled -- the solver has no consistent state to
        #    converge on and the simulation step stops completing (the
        #    Natural Caverns / CGTrigTripwire01 hang: the game keeps running
        #    but never renders another frame).  Vanilla's own chain trap is
        #    the reference for the released state: trapmace01.nif ships every
        #    Link01..11 at quality_type=4 (MO_QUAL_MOVING) with
        #    solver_deactivation=2 (LOW), and its Mace01 head likewise.
        #    Vilverin's ctrapswingmaceshort01 never hit this because it has
        #    NO chain links and so no constraint island at all.
        if breakaway_body:
            rb.quality_type        = 4  # MO_QUAL_MOVING (post-release)
            rb.solver_deactivation = 2  # SOLVER_DEACTIVATION_LOW
        else:
            rb.quality_type        = 1  # MO_QUAL_FIXED (deterministic)
            rb.solver_deactivation = 1  # OFF
        rb.unknown_byte     = 10   # Skyrim broadphase type for animated
        # Set bit 7 (0x80) on the animated NiNode — tells Skyrim to
        # synchronise the node's transform updates with physics.
        if hasattr(node, 'flags'):
            node.flags = NIF_FLAGS | 0x80  # 0x008E = 142
        # Layer MUST be 2 SKYL_ANIMSTATIC.  Census: every vanilla keyframed
        # body is layer 2 (farmhouseanimdoor01, rtirongate01, orcdoor01,
        # riftenkeepdoor01 ×2, mrkmarketstalldoor01, rifrmsmbasewallgrate01,
        # rifrmsmsecretcabinetdoor01 ×2, farmbtrapdoor01,
        # sldjailwallcollapse01 — 11/11, zero exceptions), and our one
        # in-game-confirmed working animated object (prisonSecretWall01,
        # source-authored OL_ANIM_STATIC) also ships layer 2.  Oblivion
        # authored crumble-wall bricks / breakaway planks on OL_PROPS (10),
        # which _remap_world_filter passes through unchanged — those were the
        # meshes whose sequences played without any visible motion.
        # A breakaway piece keeps SKYL_PROPS (10): once the clip ends the script
        # releases it to Motion_Dynamic and it has to collide and settle as
        # ordinary debris, which layer 2 ANIMSTATIC does not do.  Vanilla
        # breakableboard01 ships its falling Piece01/Piece02 on layer 10 for
        # exactly this reason (its fixed Anchors sit on layer 0).
        if not breakaway_body:
            for _hf in (getattr(rb, 'havok_col_filter', None),
                        getattr(rb, 'havok_col_filter_copy', None)):
                if _hf is not None:
                    _hf.layer = 2  # SKYL_ANIMSTATIC
        rb.friction         = 0.50
        rb.restitution      = 0.40
        rb.linear_damping   = 0.0996
        rb.angular_damping  = 0.0498
        rb.max_linear_velocity  = 104.4
        rb.max_angular_velocity = 31.57
        # Scripts can switch keyframed trap bodies to dynamic at runtime
        # (swinging traps activate that way), so inertia must be in Skyrim
        # units even though keyframed motion ignores it.  ×0.01, same as the
        # dynamic branch.
        _s2 = _HAVOK_SCALE * _HAVOK_SCALE
        for _attr in ('m_11', 'm_12', 'm_13', 'm_21', 'm_22', 'm_23',
                      'm_31', 'm_32', 'm_33'):
            setattr(rb.inertia, _attr, getattr(rb.inertia, _attr) * _s2)
    elif rb.mass == 0:
        # Static object — vanilla Skyrim static NIFs (farmhouse01.nif etc.)
        # use quality_type=0 (MO_QUAL_INVALID = auto-detect), not 1.
        # The working pre-refactor code also used 0.
        rb.motion_system    = 5  # SYS_BOX_STABILIZED
        rb.deactivator_type = 1
        rb.quality_type     = 0  # MO_QUAL_INVALID (auto-detect, vanilla standard)
        rb.solver_deactivation = 1
        rb.friction         = 0.50
        rb.restitution      = 0.40
        rb.linear_damping   = 0.0996
        rb.angular_damping  = 0.0498
        rb.max_linear_velocity  = 104.4
        rb.max_angular_velocity = 31.57
    else:
        # Dynamic/clutter objects.
        #
        # Mass: keep Oblivion mass as-is.  Oblivion masses (0.1–35) are in the
        # same SI-kilogram range as Skyrim clutter (0.2–100) — no scaling needed.
        # Skyrim designers set masses independently; there is no consistent
        # object-to-object multiplier between the two games.
        #
        # Inertia tensor: inertia ∝ mass × length², and lengths scale by
        # _HAVOK_SCALE (0.1) going from Oblivion Havok units (game/7) to Skyrim
        # Havok units (game/70) — so inertia scales by _HAVOK_SCALE² = 0.01.
        # Verified against vanilla: silverjug01 (mass 0.8, r≈0.19 hk, h≈0.6 hk)
        # stores I_x=0.031 = m(3r²+h²)/12 exactly (SI physics in Havok metres).
        # Scaling by only 0.1 leaves inertia ~10× too large → objects resist
        # rotation, feel sluggish/heavy when grabbed or knocked.
        _INERTIA_SCALE = _HAVOK_SCALE ** 2  # 0.01
        rb.inertia.m_11 *= _INERTIA_SCALE
        rb.inertia.m_12 *= _INERTIA_SCALE
        rb.inertia.m_13 *= _INERTIA_SCALE
        rb.inertia.m_21 *= _INERTIA_SCALE
        rb.inertia.m_22 *= _INERTIA_SCALE
        rb.inertia.m_23 *= _INERTIA_SCALE
        rb.inertia.m_31 *= _INERTIA_SCALE
        rb.inertia.m_32 *= _INERTIA_SCALE
        rb.inertia.m_33 *= _INERTIA_SCALE
        # motion_system: preserve SPHERE (2) for round objects; map all others
        # (Oblivion used BOX=4) to SPHERE_INERTIA (3), which Skyrim uses for
        # asymmetric clutter (keys, bottles, boxes, etc.).
        if rb.motion_system == 2:
            pass  # keep SPHERE
        else:
            rb.motion_system = 3  # MO_SYS_SPHERE_INERTIA
        rb.quality_type    = 4  # MO_QUAL_MOVING
        rb.deactivator_type = 1
        rb.solver_deactivation = 2
        rb.rolling_friction_multiplier = 0
        rb.linear_damping  = 0.0996
        rb.angular_damping = 0.0498
        rb.friction        = 0.50
        rb.restitution     = 0.40
        rb.max_linear_velocity  = 104.4
        rb.max_angular_velocity = 31.57

    # Mesh collision (strips/packed, possibly under a stale Oblivion MOPP) is
    # rebuilt from scratch as vanilla-style MOPP + bhkCompressedMeshShape with
    # any bhkRigidBodyT transform baked into the geometry (plain identity
    # body, like all 6341 vanilla CMS meshes).  The CMS target is the root
    # BSFadeNode — static collision must live on the root.
    target_node = actual_root if actual_root is not None else node
    rebuilt = _rebuild_mesh_collision(rb, target_node)
    if rebuilt == 'drop':
        # Sub-viable hull (see _MIN_HULL_EXTENT) — ship no collision at all
        # rather than a shape that crashes Skyrim's loader.
        node.collision_object = None
        return
    if not rebuilt:
        rb.shape = _convert_shape(rb.shape, target_node)
    _convert_materials(rb.shape)

    # Dynamic clutter with a single full-object convex hull: rebuild concave
    # objects (goblets, pitchers, ewers…) as a compound of tighter hulls so
    # the activation raycast and contacts match the visible mesh.
    # bhkRigidBodyT excluded — its shape frame is offset from the node frame.
    if (rb.mass > 0 and rb.__class__ is NifFormat.bhkRigidBody
            and isinstance(rb.shape, NifFormat.bhkConvexVerticesShape)):
        decomposed = _decompose_clutter_hull(node, rb.shape)
        if decomposed is not None:
            rb.shape = decomposed

    # Keyframed bodies carry mass 0 — vanilla census 11/11, zero exceptions
    # (Havok keyframed motion has infinite effective mass; the field is
    # convention, but it is the one remaining field where broken converted
    # animated objects diverged from both vanilla and the in-game-confirmed
    # prisonSecretWall01).  This write MUST stay at the very end of the
    # function: an earlier attempt assigned it inside the keyframed branch,
    # which flipped the mass-keyed decompose gate above and silently rebuilt
    # the collision compound (see nif_conversion_notes.md, 2026-08-01).  Down
    # here nothing dispatches on mass any more, so the only bytes that change
    # are the mass field itself.
    # ...EXCEPT a breakaway piece, which is keyframed only until its clip ends.
    # It must keep the authored mass so the script-side
    # SetMotionType(Motion_Dynamic) release drops a body Havok can simulate.
    if keyframed_body and not breakaway_body:
        rb.mass = 0.0

def convert_all_collisions(node, actual_root=None, keep_blend=False):
    """Recursively convert collision objects on every node in the entire tree.

    Skyrim requires ALL bhkCollisionObject instances in a NIF to use Skyrim
    Havok format.  Our main conversion only processes the root node's collision,
    but objects like animated display cases have additional collision objects on
    child NiNodes (e.g. the moving lid).  These child collisions also contain
    Oblivion-format unknown_6_shorts values that cause a crash when Skyrim reads
    them as pointers.  This function walks the full tree to convert every one.

    actual_root: the NIF's top-level root node (BSFadeNode).  Passed through
    to _convert_collision → _rebuild_mesh_collision so that
    bhkCompressedMeshShape.target always points to the root, not an inner wrapper.
    keep_blend: creature skeleton mode — see _convert_collision.
    """
    if node is None or not isinstance(node, NifFormat.NiNode):
        return
    if actual_root is None:
        actual_root = node
    _convert_collision(node, actual_root, keep_blend=keep_blend)
    if hasattr(node, 'children'):
        for child in node.children:
            convert_all_collisions(child, actual_root, keep_blend=keep_blend)



def _vec_cross(a, b):
    """Cross product of two PyFFI Vector4s (xyz), returned as a tuple."""
    return (a.y * b.z - a.z * b.y,
            a.z * b.x - a.x * b.z,
            a.x * b.y - a.y * b.x)


def _vec_set_unit(dst, xyz, w=0.0):
    """Normalise xyz and store into a PyFFI Vector4."""
    x, y, z = xyz
    mag = math.sqrt(x * x + y * y + z * z)
    if mag > 1e-6:
        x /= mag; y /= mag; z /= mag
    dst.x = x
    dst.y = y
    dst.z = z
    dst.w = w


def _copy_struct(src, dst):
    """Copy a PyFFI compound field-by-field (Vector4s by component)."""
    done = set()
    for a in dst._attrs:
        name = a.name
        if name in done:
            continue
        done.add(name)
        try:
            sv = getattr(src, name)
            dv = getattr(dst, name)
        except Exception:
            continue
        if hasattr(dv, 'x') and hasattr(dv, 'w'):
            dv.x = sv.x; dv.y = sv.y; dv.z = sv.z; dv.w = sv.w
        elif hasattr(dv, '_attrs'):
            _copy_struct(sv, dv)
        elif isinstance(dv, (int, float, bool)):
            try:
                setattr(dst, name, sv)
            except Exception:
                pass


# SubConstraint.type (hkConstraintType) → (plain constraint block class name,
# descriptor attribute name on both SubConstraint and the plain block).
_MALLEABLE_INNER = {
    0: ('bhkBallAndSocketConstraint', 'ball_and_socket'),
    1: ('bhkHingeConstraint', 'hinge'),
    2: ('bhkLimitedHingeConstraint', 'limited_hinge'),
    6: ('bhkPrismaticConstraint', 'prismatic'),
    7: ('bhkRagdollConstraint', 'ragdoll'),
    8: ('bhkStiffSpringConstraint', 'stiff_spring'),
}


def _demote_malleable_constraints(data):
    """Replace every bhkMalleableConstraint with a plain constraint of its inner type.

    Vanilla Skyrim ships ZERO bhkMalleableConstraint meshes (0 of 17,216 —
    binary block-type grep), so the engine path for them is untested; the
    inner descriptor as a plain constraint is the vanilla-conformant form.
    The malleable strength/tau/damping wrapper data is dropped.

    Returns the list of newly created constraint blocks (they are referenced
    from the rigid bodies but not yet present in data.blocks).
    """
    new_blocks = []
    replacements = {}
    for block in data.blocks:
        if not isinstance(block, NifFormat.bhkMalleableConstraint):
            continue
        sub = block.sub_constraint
        inner = _MALLEABLE_INNER.get(sub.type)
        if inner is None:
            continue
        cls_name, desc_attr = inner
        new_block = getattr(NifFormat, cls_name)()
        # bhkConstraint header: entities + priority come from the outer block
        # (SubConstraint's own entity list is "usually NONE").
        new_block.num_entities = block.num_entities
        new_block.entities.update_size()
        for i in range(block.num_entities):
            new_block.entities[i] = block.entities[i]
        new_block.priority = block.priority
        _copy_struct(getattr(sub, desc_attr), getattr(new_block, desc_attr))
        replacements[block] = new_block
        new_blocks.append(new_block)

    if replacements:
        # Swap references in every rigid body's constraints array.
        for block in data.blocks:
            constraints = getattr(block, 'constraints', None)
            if constraints is None:
                continue
            for i, c in enumerate(constraints):
                if c in replacements:
                    constraints[i] = replacements[c]
    return new_blocks


def _fix_limited_hinge(d, clamp_friction=True, friction_target=0.01):
    """Skyrim-format fixes for a LimitedHingeDescriptor (pivots already scaled).

    1. Missing perp_2_axle_in_b_1: Oblivion's LimitedHingeDescriptor does not
       have perp_2_axle_in_b_1; Skyrim does.  Leaving it zero causes the sign
       to spawn at a wrong tilt.  Derived as: perp_b2 × axle_b (normalised).
       Vanilla Skyrim stores w=-1 on perp_2_axle_in_a_1 and perp_2_axle_in_b_1.

    2. Clamp max_friction to Skyrim range.
       Oblivion stores max_friction=3.0; Skyrim signs use 0.01.
       At 3.0 the hinge has enough rotational friction to lock the sign
       at any angle against gravity, so it stops at a wrong tilt instead
       of swinging freely back to vertical.
    """
    perp_b1 = getattr(d, 'perp_2_axle_in_b_1', None)
    if perp_b1 is not None:
        _vec_set_unit(perp_b1, _vec_cross(d.perp_2_axle_in_b_2, d.axle_b), w=-1.0)

    perp_a1 = getattr(d, 'perp_2_axle_in_a_1', None)
    if perp_a1 is not None:
        perp_a1.w = -1.0

    if clamp_friction and d.max_friction > friction_target:
        d.max_friction = friction_target


def _fix_ragdoll(d, clamp_friction=True, friction_target=0.01):
    """Derive the Skyrim-only RagdollDescriptor motor axes and clamp friction.

    In the Skyrim (Havok 2010) layout twist/plane/motor are the three columns
    of an orthonormal basis — motor = twist × plane (verified on vanilla
    desecratedimperial.nif: twist=(1,0,0), plane=(0,1,0), motor=(0,0,1)).
    Oblivion's layout has no motor fields, so PyFFI leaves them zero, which
    ships a singular constraint basis.

    max_friction: Oblivion chain/trap ragdoll constraints store 10.0; at that
    value the joint has enough rotational friction to lock solid — chains and
    swinging traps LOOK fine but never move when touched.  Vanilla Skyrim prop
    ragdoll constraints use 0.01 (desecratedimperial.nif), the same value the
    limited-hinge clamp already uses (the tavern-sign fix).

    `friction_target` selects which contract applies: 0.01 for props, and
    **0.0 for creature blend joints** — vanilla creature skeleton.nifs are
    89/89 constraints at exactly 0.0 (dog/wolf/sabrecat/skeever census
    2026-08-08), matching their skeleton.hkx ragdolls.  The previous code
    exempted creature joints from the clamp entirely on the false premise that
    "the vanilla creature census mixes 10.0/0.5/0.01"; carrying Oblivion's 10
    through is what stopped corpses falling over.
    """
    if clamp_friction and d.max_friction > friction_target:
        d.max_friction = friction_target
    for twist_name, plane_name, motor_name in (('twist_a', 'plane_a', 'motor_a'),
                                               ('twist_b', 'plane_b', 'motor_b')):
        motor = getattr(d, motor_name, None)
        if motor is None:
            continue
        if math.sqrt(motor.x ** 2 + motor.y ** 2 + motor.z ** 2) > 1e-6:
            continue  # already populated
        _vec_set_unit(motor, _vec_cross(getattr(d, twist_name),
                                        getattr(d, plane_name)))


def _fix_hinge(d):
    """Derive the Skyrim-only HingeDescriptor fields.

    Oblivion stores only pivot_a, perp_a1, perp_a2, pivot_b, axle_b.  Skyrim
    additionally needs axle_a and perp_2_axle_in_b_1/2; left zero the hinge
    axis is degenerate.  Frame convention (per nif.xml): perp2 = axle × perp1,
    so axle_a = perp_a1 × perp_a2.  For the B side only axle_b is known; any
    orthonormal complement works because a plain hinge has no angle limits —
    build perp_b1 by Gram-Schmidt from perp_a1, then perp_b2 = axle_b × perp_b1.
    """
    axle_a = getattr(d, 'axle_a', None)
    if axle_a is not None:
        L = math.sqrt(axle_a.x ** 2 + axle_a.y ** 2 + axle_a.z ** 2)
        if L < 1e-6:
            _vec_set_unit(axle_a, _vec_cross(d.perp_2_axle_in_a_1,
                                             d.perp_2_axle_in_a_2))

    perp_b1 = getattr(d, 'perp_2_axle_in_b_1', None)
    perp_b2 = getattr(d, 'perp_2_axle_in_b_2', None)
    if perp_b1 is None or perp_b2 is None:
        return
    L1 = math.sqrt(perp_b1.x ** 2 + perp_b1.y ** 2 + perp_b1.z ** 2)
    L2 = math.sqrt(perp_b2.x ** 2 + perp_b2.y ** 2 + perp_b2.z ** 2)
    if L1 > 1e-6 and L2 > 1e-6:
        return  # already populated
    ab = d.axle_b
    # Reference vector not parallel to axle_b
    ref = d.perp_2_axle_in_a_1
    rx, ry, rz = ref.x, ref.y, ref.z
    dot = rx * ab.x + ry * ab.y + rz * ab.z
    px, py, pz = rx - dot * ab.x, ry - dot * ab.y, rz - dot * ab.z
    if px * px + py * py + pz * pz < 1e-9:
        # perp_a1 parallel to axle_b — fall back to whichever world axis isn't
        for rx, ry, rz in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)):
            dot = rx * ab.x + ry * ab.y + rz * ab.z
            px, py, pz = rx - dot * ab.x, ry - dot * ab.y, rz - dot * ab.z
            if px * px + py * py + pz * pz > 1e-9:
                break
    _vec_set_unit(perp_b1, (px, py, pz))
    _vec_set_unit(perp_b2, _vec_cross(ab, perp_b1))


def _fix_prismatic(d):
    """Best-effort Skyrim fields for a PrismaticDescriptor.

    Oblivion stores pivot_a, pivot_b, sliding_b, plane_b (+ a rotation).
    Skyrim additionally wants sliding_a/plane_a (the same axes expressed in
    body A's frame).  Without the body world transforms at this point, copy
    the B-frame axes — constrained prop pairs sit at near-identity relative
    rotation in practice.  Sliding distances are lengths → scale by 0.1.
    NOTE: vanilla Skyrim ships zero bhkPrismaticConstraint meshes, so this
    path is inherently untested by Bethesda.
    """
    for src_name, dst_name in (('sliding_b', 'sliding_a'), ('plane_b', 'plane_a')):
        src = getattr(d, src_name, None)
        dst = getattr(d, dst_name, None)
        if src is None or dst is None:
            continue
        if math.sqrt(dst.x ** 2 + dst.y ** 2 + dst.z ** 2) < 1e-6:
            dst.x = src.x; dst.y = src.y; dst.z = src.z; dst.w = src.w
    for attr in ('min_distance', 'max_distance'):
        v = getattr(d, attr, None)
        if v is not None:
            setattr(d, attr, v * _HAVOK_SCALE)


def _constraint_descriptors(block):
    """Yield (kind, descriptor) for a plain bhkConstraint block."""
    for kind in ('limited_hinge', 'ragdoll', 'hinge', 'prismatic',
                 'stiff_spring', 'ball_and_socket'):
        d = getattr(block, kind, None)
        if d is not None:
            yield kind, d


def add_missing_creature_constraints(data, root):
    """Give every unconstrained creature blend body a joint to its nearest
    body-carrying ancestor, so the NIF ragdoll is a CONNECTED tree.

    **This is the 2026-08-08 "corpse never falls over" root cause.**  Vanilla
    creature skeleton.nifs ship exactly `bodies - 1` constraints — every body
    except the ragdoll root is constrained, no orphans (dog 22/21, wolf 22/21,
    sabrecat 28/27, skeever 21/20).  A `bhkRigidBody` with NO constraint is not
    part of the ragdoll's constraint island, so the chain through it cannot
    collapse and the corpse stays standing.

    Oblivion leaves some bodies unconstrained (its animators only needed them
    as collision, not as joints).  `hkx_ragdoll` already synthesizes joints for
    these on the skeleton.**hkx** side — but the engine also reads the NIF, and
    nothing was closing the tree there.  Census of our output before this fix,
    which matched the in-game reports exactly (4/4, no false positives):

        stormatronach  70 bodies, 16 constraints (53 missing)  never fell
        mehrunesdagon  23 bodies,  0 constraints (22 missing)  never fell
        shambles       17 bodies, 13 constraints ( 3 missing)  never fell
        skeleton       17 bodies, 13 constraints ( 3 missing)  never fell
        ...all 39 other creatures complete                     all fell

    The synthesized joint is a `bhkRagdollConstraint` on the vanilla atronach
    rock template (cone 50 deg, plane +-90, twist +-5) with friction 0, pivoted
    at the child body's centre — the same template and pivot rule
    `hkx_ragdoll._SYNTH_*` uses, so the two files stay consistent.
    """
    # body -> owning node, in tree order, plus each node's parent
    node_of_body = {}
    parent_of = {}

    def walk(n, parent):
        parent_of[id(n)] = parent
        co = getattr(n, 'collision_object', None)
        body = getattr(co, 'body', None) if co is not None else None
        if body is not None and isinstance(body, NifFormat.bhkRigidBody):
            node_of_body[id(body)] = n
        for c in getattr(n, 'children', []) or []:
            if isinstance(c, NifFormat.NiNode):
                walk(c, n)

    walk(root, None)
    if not node_of_body:
        return 0

    # id(node) -> body object
    body_of_node = {}
    for _bid, n in node_of_body.items():
        body_of_node[id(n)] = n.collision_object.body

    added = 0
    for bid, node in list(node_of_body.items()):
        body = body_of_node[id(node)]
        if len(list(getattr(body, 'constraints', []) or [])) > 0:
            continue
        # nearest ancestor that carries a body = this body's ragdoll parent
        anc = parent_of.get(id(node))
        target = None
        while anc is not None:
            if id(anc) in body_of_node:
                target = body_of_node[id(anc)]
                break
            anc = parent_of.get(id(anc))
        if target is None:
            continue        # genuinely the ragdoll ROOT — vanilla leaves it bare
        _add_synth_ragdoll_constraint(data, body, target)
        added += 1
    return added


# Vanilla atronach rock-joint template, shared with hkx_ragdoll._SYNTH_*:
# cone 50 deg, plane +-90 deg, twist +-5 deg, friction 0.
_SYNTH_CONE = 0.872665
_SYNTH_PLANE = 1.570796
_SYNTH_TWIST = 0.087266


def _add_synth_ragdoll_constraint(data, child_body, parent_body):
    """Append a bhkRagdollConstraint joining child_body to parent_body.

    Pivots at the child body's own centre expressed in each body's local space
    (both bodies' `center` fields are already in Skyrim Havok units at this
    point, so the pivot needs no further scaling).  Frames are axis-aligned:
    twist = X, plane = Y, motor = Z — the orthonormal basis Skyrim's 2010
    layout requires (a zero motor ships a singular basis).
    """
    con = NifFormat.bhkRagdollConstraint()
    con.num_entities = 2
    con.entities.update_size()
    con.entities[0] = child_body
    con.entities[1] = parent_body
    con.priority = 1

    d = con.ragdoll
    for name, (x, y, z) in (('twist_a', (1.0, 0.0, 0.0)),
                            ('plane_a', (0.0, 1.0, 0.0)),
                            ('motor_a', (0.0, 0.0, 1.0)),
                            ('twist_b', (1.0, 0.0, 0.0)),
                            ('plane_b', (0.0, 1.0, 0.0)),
                            ('motor_b', (0.0, 0.0, 1.0))):
        v = getattr(d, name, None)
        if v is not None:
            v.x, v.y, v.z = x, y, z
            if hasattr(v, 'w'):
                v.w = 0.0
    for name, src in (('pivot_a', child_body.center),
                      ('pivot_b', child_body.center)):
        v = getattr(d, name, None)
        if v is not None:
            v.x, v.y, v.z = src.x, src.y, src.z
            if hasattr(v, 'w'):
                v.w = 0.0
    d.cone_max_angle = _SYNTH_CONE
    d.plane_min_angle = -_SYNTH_PLANE
    d.plane_max_angle = _SYNTH_PLANE
    d.twist_min_angle = -_SYNTH_TWIST
    d.twist_max_angle = _SYNTH_TWIST
    d.max_friction = 0.0

    # register on the child body (Havok convention: the constraint lives on
    # entity A) and in the block list
    n = child_body.num_constraints
    child_body.num_constraints = n + 1
    child_body.constraints.update_size()
    child_body.constraints[n] = con
    data.blocks.append(con)
    return con


def scale_constraint_pivots(data):
    """Fix Havok constraint data for Oblivion → Skyrim conversion.

    Applies to EVERY constraint descriptor type (limited hinge, ragdoll,
    hinge, prismatic, stiff spring, ball-and-socket):

    1. Malleable demotion: bhkMalleableConstraint (never shipped by vanilla
       Skyrim) is replaced by a plain constraint of its inner type.
    2. Pivot scale: pivot_a/pivot_b are Oblivion Havok-space positions and
       must be scaled by _HAVOK_SCALE (0.1).  Axis vectors are unit vectors
       and must NOT be scaled.  Stiff-spring length and prismatic sliding
       distances are lengths and scale too.
    3. Skyrim-only fields Oblivion has no source for are derived
       (limited hinge perp_b1, hinge axle_a/perp_b1/perp_b2).
    4. broadphaseType=10 for dynamic constrained bodies.  (Inertia is NOT
       rescaled here — _convert_collision and _convert_blend_collision both
       already apply the full ×0.01; an extra ×0.1 here left every
       constrained body's inertia 10× too small.)

    bhkRigidBodyT is kept as-is: Skyrim uses bhkRigidBodyT for constrained
    sign bodies (confirmed in vanilla signfourshieldstavern01.nif).  The T
    offset is body-local relative to the owning NiNode, which is already
    correct after _convert_collision scales it by _HAVOK_SCALE.
    """
    constraint_blocks = [b for b in data.blocks
                         if isinstance(b, NifFormat.bhkConstraint)]
    constraint_blocks += _demote_malleable_constraints(data)

    # Creature-skeleton blend bodies follow the VANILLA CREATURE contract, not
    # the prop contract: the broadphase byte stays 0, not the dynamic-prop 10.
    #
    # max_friction is FORCED TO 0 on them (2026-08-08).  The old comment here
    # claimed "vanilla skeleton.nif joints mix 10.0/0.5/0.01 so keep the
    # authored value" — that is measurably WRONG: a census of the vanilla
    # dog/wolf/sabrecat/skeever creature skeleton.nifs is **89/89 constraints
    # at exactly 0.000000**, matching their skeleton.hkx ragdolls (dog 42/42
    # `maxFrictionTorque` = 0).  Oblivion ships 10.0/12.0, and
    # `max_friction` is an ANGULAR FRICTION TORQUE: at that magnitude the joint
    # resists rotation hard enough that the chain through it cannot collapse
    # under gravity, so corpses never fall over.  Fixing only the hkx side left
    # this in place and the symptom survived — **the engine reads the NIF blend
    # bodies**, so nif and hkx must agree.
    _force_blend_friction_zero = True
    blend_ids = {id(b.body) for b in data.blocks
                 if isinstance(b, NifFormat.bhkBlendCollisionObject)
                 and b.body is not None}

    for block in constraint_blocks:
        if isinstance(block, NifFormat.bhkMalleableConstraint):
            continue  # replaced by its demoted inner constraint
        is_blend = any(e is not None and id(e) in blend_ids
                       for e in block.entities)
        for kind, d in _constraint_descriptors(block):
            # Scale pivot positions (xyz only; w is unused padding).
            for pivot_attr in ('pivot_a', 'pivot_b'):
                pivot = getattr(d, pivot_attr, None)
                if pivot is not None:
                    pivot.x *= _HAVOK_SCALE
                    pivot.y *= _HAVOK_SCALE
                    pivot.z *= _HAVOK_SCALE
            # Blend (creature) joints clamp to 0.0, props to 0.01 — both are
            # clamped now; only the TARGET differs.  See the census note above.
            tgt = 0.0 if is_blend else 0.01
            if kind == 'limited_hinge':
                _fix_limited_hinge(d, friction_target=tgt)
            elif kind == 'ragdoll':
                _fix_ragdoll(d, friction_target=tgt)
            elif kind == 'hinge':
                _fix_hinge(d)
            elif kind == 'prismatic':
                _fix_prismatic(d)
            elif kind == 'stiff_spring':
                length = getattr(d, 'length', None)
                if length is not None:
                    d.length = length * _HAVOK_SCALE

        for e in block.entities:
            if e is not None and e.mass > 0.0 and id(e) not in blend_ids:
                e.unknown_byte = 10


# ---------------------------------------------------------------------------
# Collision hoisting (child → root)
# ---------------------------------------------------------------------------

def _offset_collision_shape_verts(co, ox, oy, oz):
    """Add (ox, oy, oz) game-unit offset to all collision shape vertices.

    Bakes a child NiNode's world-space translation into the shape so the
    collision stays in the correct position after the node is hoisted to the
    root (which sits at the origin).

    BOTH mesh shape types must be handled, and they store vertices at
    DIFFERENT scales:

      * bhkNiTriStripsShape      — game units (×7 Havok units) → add as-is.
      * bhkPackedNiTriStripsShape — 1/7 game units (i.e. Oblivion Havok
        units) → the offset must be divided by 7 first.

    Handling only the strips case silently dropped the offset for every
    packed-shape mesh, leaving its collision centred on the origin while the
    visual mesh sat elsewhere.  Battlehorn's stackstairsmid02b is the case in
    point: a `collisionStackBalconyMid02b` node at Z=+394.5 whose collision
    came through at z[-332.8..332.8] instead of z[61.7..727.3] — the shape
    ends up half a storey low, which on a stair/balcony wedge reads in-game
    as the collision being flipped upside-down.  Its sibling
    stackbalconymid02.nif has the identical node offset but ships a
    bhkNiTriStripsShape, so it was always converted correctly — the pair is
    the A/B that isolates the shape type as the discriminator.

    Traverses: bhkCollisionObject → body → shape → (bhkMoppBvTreeShape →)
    bhkNiTriStripsShape | bhkPackedNiTriStripsShape
    """
    rb = getattr(co, 'body', None)
    if rb is None:
        return
    shape = getattr(rb, 'shape', None)
    # Unwrap bhkMoppBvTreeShape to get at the inner shape
    if shape is not None and isinstance(shape, NifFormat.bhkMoppBvTreeShape):
        shape = shape.shape
    if shape is None:
        return

    if isinstance(shape, NifFormat.bhkNiTriStripsShape):
        for sd in shape.strips_data:
            if sd is None:
                continue
            for v in sd.vertices:
                v.x += ox
                v.y += oy
                v.z += oz
        return

    if isinstance(shape, NifFormat.bhkPackedNiTriStripsShape):
        data = getattr(shape, 'data', None)
        if data is None:
            return
        sx = ox / _OB_GAME_UNITS_PER_HAVOK
        sy = oy / _OB_GAME_UNITS_PER_HAVOK
        sz = oz / _OB_GAME_UNITS_PER_HAVOK
        for v in data.vertices:
            v.x += sx
            v.y += sy
            v.z += sz


_OB_GAME_UNITS_PER_HAVOK = 7.0  # Oblivion: 1 Havok unit = 7 game units


def _m3_from_quat_xyzw(x, y, z, w):
    """Unit quaternion (x,y,z,w) → 3x3 column-vector rotation matrix.

    Same formula as NifSkope's Matrix::fromQuat, which is how the engine
    interprets bhkRigidBodyT.rotation.
    """
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z),     2 * (x * z + w * y)],
        [2 * (x * y + w * z),     1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y),     2 * (y * z + w * x),     1 - 2 * (x * x + y * y)],
    ]


def _quat_xyzw_from_m3(m):
    """3x3 column-vector rotation matrix → unit quaternion (x,y,z,w).

    Shoemake branches handle 180° rotations (trace = -1, w = 0), which are
    common on Oblivion architecture roots.
    """
    tr = m[0][0] + m[1][1] + m[2][2]
    if tr > 0.0:
        s = math.sqrt(tr + 1.0) * 2.0
        w = 0.25 * s
        x = (m[2][1] - m[1][2]) / s
        y = (m[0][2] - m[2][0]) / s
        z = (m[1][0] - m[0][1]) / s
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2.0
        w = (m[2][1] - m[1][2]) / s
        x = 0.25 * s
        y = (m[0][1] + m[1][0]) / s
        z = (m[0][2] + m[2][0]) / s
    elif m[1][1] > m[2][2]:
        s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2.0
        w = (m[0][2] - m[2][0]) / s
        x = (m[0][1] + m[1][0]) / s
        y = 0.25 * s
        z = (m[1][2] + m[2][1]) / s
    else:
        s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2.0
        w = (m[1][0] - m[0][1]) / s
        x = (m[0][2] + m[2][0]) / s
        y = (m[1][2] + m[2][1]) / s
        z = 0.25 * s
    return x, y, z, w


def bake_node_transform_into_body(coll_obj, node, extra_z=0.0):
    """Compose a node's local transform L=(R,T,s) into its collision body.

    extra_z: additional model-space z translation carried by the wrapper but
    not present on the node itself (the furniture origin shift — the importer
    lowers the REFRs by the same amount, so the collision must rise with the
    geometry or it ends up sunk by the shift).

    Used when the root's transform is about to be zeroed (rotation-wrap pass):
    the engine places a root collision body at REFR ∘ bodyT, so the vanishing
    root transform must be absorbed into bodyT or the collision ends up
    rotated/offset relative to the mesh (stackhallentrance01: 90° off).
    Collision must stay on the ROOT node — attaching it to the inner wrapper
    aligns it too, but intermittently crashes hkpCollisionDispatcher.

    NIF matrices act on row vectors under PyFFI's m_ij naming; the engine
    (and NifSkope) reads the same file bytes as a column-vector matrix, so the
    column matrix is the m_ij-named transpose.  bhkRigidBody(T) translation is
    in Oblivion Havok units at this stage (game / 7); the Skyrim rescale
    (× _HAVOK_SCALE) happens later in _convert_collision.

    bhkRigidBody (non-T) carries no transform of its own, so it is promoted to
    bhkRigidBodyT (identical field layout — PyFFI class swap) to hold L.
    Returns True if the body was modified.
    """
    body = getattr(coll_obj, 'body', None)
    if body is None or not isinstance(body, NifFormat.bhkRigidBody):
        return False

    r = node.rotation
    R = [[r.m_11, r.m_21, r.m_31],
         [r.m_12, r.m_22, r.m_32],
         [r.m_13, r.m_23, r.m_33]]  # column-vector convention
    s = node.scale
    T = (node.translation.x / _OB_GAME_UNITS_PER_HAVOK,
         node.translation.y / _OB_GAME_UNITS_PER_HAVOK,
         (node.translation.z + extra_z) / _OB_GAME_UNITS_PER_HAVOK)

    if isinstance(body, NifFormat.bhkRigidBodyT):
        q = body.rotation
        M_old = _m3_from_quat_xyzw(q.x, q.y, q.z, q.w)
        t_old = (body.translation.x, body.translation.y, body.translation.z)
    else:
        body.__class__ = NifFormat.bhkRigidBodyT
        M_old = _m3_from_quat_xyzw(0.0, 0.0, 0.0, 1.0)
        t_old = (0.0, 0.0, 0.0)

    # bodyT' = L ∘ bodyT:  M' = R·M,  t' = R·(t·s) + T
    M_new = [[sum(R[i][k] * M_old[k][j] for k in range(3)) for j in range(3)]
             for i in range(3)]
    t_new = [sum(R[i][k] * t_old[k] * s for k in range(3)) + T[i]
             for i in range(3)]
    x, y, z, w = _quat_xyzw_from_m3(M_new)

    body.rotation.x = x
    body.rotation.y = y
    body.rotation.z = z
    body.rotation.w = w
    body.translation.x = t_new[0]
    body.translation.y = t_new[1]
    body.translation.z = t_new[2]
    return True


def hoist_collision(root):
    """Find a collision object on any descendant NiNode and move it to root.

    Skyrim requires bhkCollisionObject to be on the root BSFadeNode.
    Oblivion meshes sometimes put it on a child NiNode (e.g. 'CollisionXxx').
    We take the first one found, assign it to root, and null it on the child.

    If the source child NiNode has a non-zero translation, that offset is baked
    into the collision shape vertices so the collision stays in the correct world
    position after being moved to the root (which is at the world origin).

    Returns True if a collision was hoisted.
    """
    def _find_and_clear(node):
        """Return (collision_object, child_node_translation_xyz) or None."""
        if not isinstance(node, NifFormat.NiNode):
            return None
        for child in node.children:
            if child is None:
                continue
            if (hasattr(child, 'collision_object') and
                    child.collision_object is not None):
                co = child.collision_object
                child.collision_object = None
                t = child.translation
                return co, (t.x, t.y, t.z)
            result = _find_and_clear(child)
            if result is not None:
                return result
        return None

    found = _find_and_clear(root)
    if found is not None:
        co, (ox, oy, oz) = found
        root.collision_object = co
        co.target = root
        # If the source NiNode was not at the origin, bake its world-space
        # translation into the bhkNiTriStripsShape vertices so the collision
        # lands in the correct position after being placed on the root node.
        if ox != 0.0 or oy != 0.0 or oz != 0.0:
            _offset_collision_shape_verts(co, ox, oy, oz)
        return True
    return False


def _collect_psys_referenced_nodes(root):
    """Return the set of id()s of nodes referenced by particle-system modifiers
    (NiPSysGravityModifier.gravity_object, *Emitter.emitter_object).

    These are empty marker NiNodes (e.g. 'Gravity', 'SparkGravity') that the
    particle physics point at; removing them dangles the reference and breaks
    the simulation (invisible particles)."""
    refs = set()
    for block in getattr(root, 'tree', lambda: [])():
        tn = type(block).__name__
        if tn == 'NiPSysGravityModifier':
            go = getattr(block, 'gravity_object', None)
            if go is not None:
                refs.add(id(go))
        elif tn.endswith('Emitter') and 'Ctlr' not in tn:
            eo = getattr(block, 'emitter_object', None)
            if eo is not None:
                refs.add(id(eo))
    return refs


def remove_empty_collision_nodes(root):
    """Remove empty NiNode children that were collision containers.

    After hoisting collision to root, the original NiNode child (e.g.
    'Collision045') is left empty: no children, no collision_object.  Skyrim
    processes every child of BSFadeNode and an unexpected empty NiNode can
    trigger crashes.  This function compacts the children array in-place.

    Nodes referenced by particle-system modifiers (Gravity/emitter objects)
    are PRESERVED even when empty — dropping them dangles the reference and the
    particle system stops rendering.
    """
    if not hasattr(root, 'children') or not isinstance(root, NifFormat.NiNode):
        return
    protected = _collect_psys_referenced_nodes(root)
    keep = []
    for child in root.children:
        if child is None:
            continue
        # Remove bare NiNodes with no children and no collision — unless a
        # particle modifier references them.
        if (type(child).__name__ in ('NiNode',) and
                child.num_children == 0 and
                getattr(child, 'collision_object', None) is None and
                id(child) not in protected):
            continue
        keep.append(child)
    if len(keep) < root.num_children:
        root.num_children = len(keep)
        root.children.update_size()
        for i, c in enumerate(keep):
            root.children[i] = c
