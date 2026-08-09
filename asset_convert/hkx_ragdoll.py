"""Ragdoll stage for creature skeleton.hkx.

Converts the Oblivion skeleton.nif ragdoll (bhkBlendCollisionObject rigid
bodies + bhkRagdollConstraint/bhkLimitedHingeConstraint on the bone nodes)
into the vanilla Skyrim skeleton.hkx ragdoll anatomy (layouts mirrored from
the hkxcmd XML dump of the vanilla deer skeleton.hkx):

  hkaSkeleton (ragdoll)     bones "Ragdoll_<bone>", subset of the anim
                            skeleton (bones that carry rigid bodies),
                            parent-before-child
  2x hkaSkeletonMapper      anim→ragdoll and ragdoll→anim (identity
                            aFromBTransform: our ragdoll bone frames are
                            DEFINED to coincide with the anim bone frames —
                            body translation offsets are folded into the
                            shape vertices / COM instead)
  hkpPhysicsData/System     shared hkpRigidBody set + one
                            hkpConstraintInstance per joint
  hkaRagdollInstance        second constraint-instance set (vanilla
                            duplicates the constraint data per owner)

Unit/convention notes (all verified against the vanilla deer dump):
  - skeleton.hkx works in GAME units (capsule radius ~24), NOT Havok metres.
    Oblivion nif bhk data is in Oblivion Havok units (game/7) → ×7.
  - Inertia scales by 7² = 49; hkpMotion stores inertiaAndMassInv =
    (1/I, 1/I, 1/I, 1/mass).
  - hkTransform XML prints the ROW-convention rotation matrix rows (same
    convention as NIF matrices) + translation; hkQuaternions equal
    _mat33_to_quat_xyzw of the NIF matrix.
  - Constraint transformA/B rows = (twist, plane, twist×plane) for ragdoll
    joints, (axle, perp1, perp2) for hinges, expressed in each entity's
    local frame; translation = pivot.
  - Constraint entities order = (child body, parent body) — the nif stores
    the constraint on the child body with entities[0] = itself.
  - THE BODY FRAME IS NOT THE BONE FRAME (2026-07-16, the mangled-ragdoll
    root cause): a blend-collision bhkRigidBody's rotation/translation hold
    the body's BIND-POSE WORLD transform (translation×7 == the bone's world
    position on every Oblivion skeleton; verified dog 26/26 — vanilla Skyrim
    skeleton.nif blend bodies use the same convention in metre units).
    Capsule vertices, COM, and constraint pivots/axes are authored in that
    body-local frame, so converting them to our bone-local ragdoll frames
    needs the full bone-from-body transform (R_body_world @ R_bone_world^T
    row-convention + the world offset), NOT translation-as-offset.  The old
    "fold body.translation in as an offset" displaced every capsule by the
    bone's world position and dropped the rotation entirely.
  - Vanilla creature ragdoll constraints have maxFrictionTorque 0.0 across
    the board (dog census) — Oblivion descriptor frictions (≈10) freeze
    joints into distorted poses in Skyrim's solver.  Synthetic rock joints
    keep 10.0 (vanilla atronachstorm census).
"""

import math
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from asset_convert import pyffi_monkey_patch  # noqa: F401
from asset_convert.hkx_xml import fmt_vec
from asset_convert import hkx_xml
from pyffi.formats.nif import NifFormat

hkx_xml.SIGNATURES.update({
    'hkaSkeletonMapper': '0x12df42a5',
    'hkpCapsuleShape': '0xdd0b1fd3',
    'hkpRigidBody': '0x75f8d805',
    'hkpRagdollConstraintData': '0x8fb5dd29',
    'hkpLimitedHingeConstraintData': '0x7c15bb6b',
    'hkpConstraintInstance': '0x34eba5f',
    'hkpPositionConstraintMotor': '0x748fb303',
    'hkaRagdollInstance': '0x154948e8',
    'hkpPhysicsSystem': '0xff724c17',
    'hkpPhysicsData': '0xc2a461e4',
    'hkMemoryResourceContainer': '0x4762f92a',
    'hkMemoryResourceHandle': '0xbffac086',
    'hkpShapeInfo': '0xea7f1d08',
})

_OB_TO_GAME = 7.0          # Oblivion Havok units → game units
_HUGE = '18446726481523507000.000000'
_MAX_IMPULSE = '340282001837565600000000000000000000000.000000'


# ---------------------------------------------------------------------------
# Extraction from the Oblivion skeleton.nif
# ---------------------------------------------------------------------------

def _quat_to_mat_row(q):
    """xyzw quat → row-convention 3x3 (inverse of _mat33_to_quat_xyzw)."""
    x, y, z, w = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w)],
        [2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w)],
        [2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y)],
    ])


def _mat_row_to_quat(m):
    """Row-convention 3x3 → xyzw quat (Shepperd)."""
    m00, m01, m02 = m[0]
    m10, m11, m12 = m[1]
    m20, m21, m22 = m[2]
    tr = m00 + m11 + m22
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        w = 0.25 * s
        x = (m12 - m21) / s
        y = (m20 - m02) / s
        z = (m01 - m10) / s
    elif m00 > m11 and m00 > m22:
        s = math.sqrt(1.0 + m00 - m11 - m22) * 2
        w = (m12 - m21) / s
        x = 0.25 * s
        y = (m10 + m01) / s
        z = (m20 + m02) / s
    elif m11 > m22:
        s = math.sqrt(1.0 + m11 - m00 - m22) * 2
        w = (m20 - m02) / s
        x = (m10 + m01) / s
        y = 0.25 * s
        z = (m21 + m12) / s
    else:
        s = math.sqrt(1.0 + m22 - m00 - m11) * 2
        w = (m01 - m10) / s
        x = (m20 + m02) / s
        y = (m21 + m12) / s
        z = 0.25 * s
    n = math.sqrt(w * w + x * x + y * y + z * z)
    return (x / n, y / n, z / n, w / n)


def _bone_worlds(bones):
    """World (rotation 3x3 row-convention, translation vec3) per anim bone."""
    worlds = []
    for b in bones:
        R = _quat_to_mat_row(b.quat_xyzw) * b.scale
        t = np.array(b.translation, dtype=float)
        if b.parent < 0:
            worlds.append((R, t))
        else:
            Rp, tp = worlds[b.parent]
            worlds.append((R @ Rp, t @ Rp + tp))
    return worlds


def _unit(v):
    v = np.asarray(v, dtype=float)
    return v / (np.linalg.norm(v) or 1.0)


def _v4(v, scale=1.0):
    return np.array([v.x * scale, v.y * scale, v.z * scale], dtype=float)


def _capsule_inertia(shape, mass):
    """Principal inertia diagonal (Ixx, Iyy, Izz) of a solid capsule of the
    given (radius, vertexA, vertexB), about its centre of mass.

    A capsule is a cylinder (length L along its segment axis, radius r) capped
    by two hemispheres.  This is the well-conditioned tensor Havok expects —
    replacing Oblivion's ill-conditioned authored diagonals, which diverge the
    ragdoll solver (rigid corpse).  The diagonal is expressed in the capsule's
    local frame with the segment along the discovered principal axis, then
    mapped back to (x,y,z) so the axis with the segment gets Iaxial and the
    other two get Iradial — vanilla stores exactly this axis-aligned form
    (thin limbs anisotropic ~5x, hubs near-isotropic), never worse than ~7x.
    """
    r, va, vb = (float(shape[0]), np.asarray(shape[1], float),
                 np.asarray(shape[2], float))
    seg = vb - va
    L = float(np.linalg.norm(seg))
    r = max(r, 1e-3)

    # masses split by volume between the cylinder and the two hemisphere caps
    v_cyl = math.pi * r * r * L
    v_cap = (4.0 / 3.0) * math.pi * r ** 3
    v_tot = v_cyl + v_cap or 1.0
    m_cyl = mass * v_cyl / v_tot
    m_cap = mass * v_cap / v_tot

    # axial (about the segment axis) and radial (perpendicular) moments
    i_axial = 0.5 * m_cyl * r * r + 2.0 * (0.4 * m_cap * r * r)
    i_radial = (m_cyl * (r * r / 4.0 + L * L / 12.0)
                + 2.0 * m_cap * (0.4 * r * r
                                 + 0.5 * (L / 2.0) ** 2 + 0.375 * r * L))

    # place i_axial on the axis most aligned with the segment; radial on the
    # other two.  A near-spherical capsule (L~0) comes out near-isotropic.
    axis = int(np.argmax(np.abs(seg))) if L > 1e-6 else 2
    diag = [i_radial, i_radial, i_radial]
    diag[axis] = i_axial

    # Clamp the principal-axis RATIO to what the ragdoll solver stays stable
    # under.  Even the exact capsule tensor of a long thin limb (forearm ~31x)
    # ill-conditions Havok's joint solver; vanilla Skyrim's WORST creature
    # body is 6.6x, so it evidently fattens the effective inertia ellipsoid.
    # Raise the small axes toward the largest until no axis exceeds MAX_ANISO
    # times the smallest — preserves the ellipsoid orientation (limbs still
    # resist axial spin least) while guaranteeing a well-conditioned tensor.
    MAX_ANISO = 6.5
    lo = min(diag)
    if lo > 0:
        diag = [min(x, lo * MAX_ANISO) for x in diag]
        diag = [max(x, max(diag) / MAX_ANISO) for x in diag]
    return tuple(max(x, 1e-6) for x in diag)


class RagdollPart:
    def __init__(self):
        self.anim_index = -1
        self.parent = -1            # ragdoll part index
        self.name = ''
        self.mass = 1.0
        self.inertia = (1.0, 1.0, 1.0)  # tensor diagonal, game units
        self.com = np.zeros(3)      # bone-local, game units
        self.shape = None           # (radius, vA, vB) capsule, bone-local
        self.constraint = None      # (kind, descriptor dict) joining to parent


def _capsule_from_shape(shape):
    """Any Oblivion bhk shape → (radius, vA, vB) capsule in BODY-local game
    units (the caller maps body space → bone space via the part's
    bone-from-body transform)."""
    name = shape.__class__.__name__
    if name == 'bhkCapsuleShape':
        r = float(shape.radius) * _OB_TO_GAME
        return (r, _v4(shape.first_point, _OB_TO_GAME),
                _v4(shape.second_point, _OB_TO_GAME))
    if name == 'bhkSphereShape':
        r = float(shape.radius) * _OB_TO_GAME
        eps = np.array([0.0, 0.0, max(0.1, r * 0.05)])
        return (r, -eps, eps)
    if name == 'bhkBoxShape':
        d = _v4(shape.dimensions, _OB_TO_GAME)     # half extents
        axis = int(np.argmax(d))
        seg = np.zeros(3)
        seg[axis] = d[axis]
        r = float(np.median(np.delete(d, axis)))
        return (max(r, 0.5), -seg, seg)
    if name in ('bhkTransformShape', 'bhkConvexTransformShape'):
        m = shape.transform
        sub = _capsule_from_shape(shape.shape)
        if sub is None:
            return None
        R = np.array([[m.m_11, m.m_12, m.m_13],
                      [m.m_21, m.m_22, m.m_23],
                      [m.m_31, m.m_32, m.m_33]])
        t = np.array([m.m_14, m.m_24, m.m_34]) * _OB_TO_GAME
        r, va, vb = sub
        # PyFFI m_ij is the transpose of the engine's column matrix →
        # row-convention: v' = v @ R.T ... use both orders? m_i4 column is
        # translation; rotate row-style like collision.py does.
        return (r, va @ R.T + t, vb @ R.T + t)
    if name == 'bhkListShape':
        for sub in shape.sub_shapes:
            got = _capsule_from_shape(sub)
            if got is not None:
                return got
    return None


def _descriptor(constraint):
    """(kind, descriptor) from a bhk constraint block; malleables demote to
    their inner type. Returns (None, None) for unsupported kinds."""
    cname = constraint.__class__.__name__
    if cname == 'bhkRagdollConstraint':
        return 'ragdoll', constraint.ragdoll
    if cname == 'bhkLimitedHingeConstraint':
        return 'hinge', constraint.limited_hinge
    if cname == 'bhkHingeConstraint':
        return 'plain_hinge', constraint.hinge
    if cname == 'bhkMalleableConstraint':
        sub = constraint.sub_constraint     # PyFFI 2.2.3 SubConstraint
        t = int(sub.type)
        if t == 7:      # ragdoll
            return 'ragdoll', sub.ragdoll
        if t == 2:      # limited hinge
            return 'hinge', sub.limited_hinge
        if t == 1:
            return 'plain_hinge', sub.hinge
    return None, None


# --- bind-pose limit legalization ------------------------------------------
# Oblivion authors many joints whose limit window EXCLUDES the bind pose
# (dog head hinge [23.8deg, 32.6deg] with the bind pose at 0; deer thigh
# cone axis 35deg off bind with a 15deg cone).  Oblivion's death flow
# tolerated that, but Skyrim's solver yanks every such limb to the nearest
# limit boundary the instant the ragdoll activates -> the mangled-corpse
# look.  Vanilla Skyrim keeps EVERY bind-pose joint angle inside its limit
# window (deer census: exactly 0.0 on all joints; dog: within a few
# degrees), so we widen each converted window just enough to contain the
# measured bind angle.  Widening (never shifting) preserves the authored
# range of motion — gravity still folds limbs toward the authored pose,
# but nothing snaps on death.
_BIND_EPS = 0.009        # ~0.5 deg margin inside the widened boundary


def _bind_twist(axis, ref_a, ref_b):
    """Signed rotation of ref_a relative to ref_b about axis (world)."""
    axis = np.asarray(axis, dtype=float)
    axis = axis / (np.linalg.norm(axis) or 1.0)

    def _proj(v):
        p = v - axis * float(np.dot(v, axis))
        n = np.linalg.norm(p)
        return p / n if n else p

    pa, pb = _proj(np.asarray(ref_a, float)), _proj(np.asarray(ref_b, float))
    return math.atan2(float(np.dot(np.cross(pb, pa), axis)),
                      float(np.dot(pa, pb)))


def _world_angle(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    a = a / (np.linalg.norm(a) or 1.0)
    b = b / (np.linalg.norm(b) or 1.0)
    return math.acos(max(-1.0, min(1.0, float(np.dot(a, b)))))


def _legalize_limits(kind, info, R_cw, R_pw):
    """Widen the constraint's limit windows so the bind pose is inside them
    (see block comment above).  rows/limits are mutated in place."""
    wa = [np.asarray(r, float) @ R_cw for r in info['rows_a']]
    wb = [np.asarray(r, float) @ R_pw for r in info['rows_b']]
    if kind == 'ragdoll':
        cone = _world_angle(wa[0], wb[0])
        dot = float(np.dot(wa[0] / (np.linalg.norm(wa[0]) or 1.0),
                           wb[1] / (np.linalg.norm(wb[1]) or 1.0)))
        plane = math.asin(max(-1.0, min(1.0, dot)))
        twist = _bind_twist(wa[0] + wb[0], wa[1], wb[1])
        info['cone'] = max(info['cone'], cone + _BIND_EPS)
        info['plane_min'] = min(info['plane_min'], plane - _BIND_EPS)
        info['plane_max'] = max(info['plane_max'], plane + _BIND_EPS)
        info['twist_min'] = min(info['twist_min'], twist - _BIND_EPS)
        info['twist_max'] = max(info['twist_max'], twist + _BIND_EPS)
    else:
        ang = _bind_twist(wa[0] + wb[0], wa[1], wb[1])
        info['min'] = min(info['min'], ang - _BIND_EPS)
        info['max'] = max(info['max'], ang + _BIND_EPS)


# --- vanilla rock-joint template (atronachstorm skeleton.nif census: every
# free orbiting rock is ragdoll-constrained to its nearest body-carrying
# ancestor with exactly these limits) ---
_SYNTH_CONE = 0.872665          # 50 deg
_SYNTH_PLANE = 1.570796         # +/- 90 deg
_SYNTH_TWIST = 0.087266         # +/- 5 deg
_SYNTH_FRICTION = 10.0


def _decode_name(node):
    return bytes(node.name).decode('latin-1').rstrip('\x00')


def plan_ragdoll_tree(data):
    """Plan the single constrained tree over EVERY collision body in a
    creature skeleton.nif (works on Oblivion source or mid-conversion data).

    ENGINE CONTRACT (2026-07-09 Storm Atronach / Skeleton Load3D crash): the
    SSE ragdoll attach walks constraints across ALL bhkBlendCollisionObject
    bodies; any body outside one connected constrained tree leaves the walk
    dereferencing an uninitialized hkpPositionConstraintMotor pointer ->
    EXCEPTION_ACCESS_VIOLATION.  Vanilla atronachstorm constrains all 26
    free orbiting rocks to their parent bones (27 bodies / 26 constraints);
    Oblivion ships those rocks UNCONSTRAINED, so joints must be synthesized.

    Returns None when there are fewer than 2 bodies, else a dict:
      body_nodes  [NiNode] every body-carrying node under the bone root, DFS
      edges       {id(child): parent NiNode} existing constraint links
                  (first valid constraint per body; cycles broken)
      synthetic   [(child NiNode, parent NiNode)] joints to ADD so the graph
                  becomes one tree (nearest body-carrying NIF ancestor,
                  fallback = the main tree root)
      worlds      {id(node): (R 3x3 row-conv, t vec3)} world transforms in
                  game units
      root        the main tree root NiNode
      node_of_id  {id(node): NiNode}
    """
    from asset_convert.hkx_skeleton import find_skeleton_root
    try:
        skel_root = find_skeleton_root(data)
    except ValueError:
        return None

    body_nodes = []
    node_parent = {}
    worlds = {}

    def _local(node):
        m = node.rotation
        R = np.array([[m.m_11, m.m_12, m.m_13],
                      [m.m_21, m.m_22, m.m_23],
                      [m.m_31, m.m_32, m.m_33]], dtype=float) \
            * float(node.scale)
        t = np.array([node.translation.x, node.translation.y,
                      node.translation.z], dtype=float)
        return R, t

    def visit(node, parent, R_p, t_p):
        R_l, t_l = _local(node)
        R_w = R_l @ R_p
        t_w = t_l @ R_p + t_p
        worlds[id(node)] = (R_w, t_w)
        node_parent[id(node)] = parent
        co = getattr(node, 'collision_object', None)
        if co is not None and getattr(co, 'body', None) is not None:
            body_nodes.append(node)
        for child in node.children:
            if isinstance(child, NifFormat.NiNode):
                visit(child, node, R_w, t_w)

    visit(skel_root, None, np.eye(3), np.zeros(3))

    if len(body_nodes) < 2:
        return None

    dfs_index = {id(n): i for i, n in enumerate(body_nodes)}
    body_of = {id(n): n.collision_object.body for n in body_nodes}
    node_of_body = {id(b): nid for nid, b in
                    ((id(n), body_of[id(n)]) for n in body_nodes)}
    node_of_id = {id(n): n for n in body_nodes}

    # existing constraint links: first valid constraint per body whose
    # entities are (self, another body)
    edges = {}
    for n in body_nodes:
        body = body_of[id(n)]
        for con in getattr(body, 'constraints', []):
            kind, d = _descriptor(con)
            if kind is None:
                continue
            ents = list(con.entities)
            if (len(ents) == 2 and ents[0] is body
                    and id(ents[1]) in node_of_body):
                edges[id(n)] = node_of_id[node_of_body[id(ents[1])]]
                break

    # break constraint cycles (defensive; each body has <= 1 outgoing edge)
    for n in list(body_nodes):
        seen = set()
        nid = id(n)
        while nid in edges and nid not in seen:
            seen.add(nid)
            nid = id(edges[nid])
        if nid in seen:
            del edges[nid]

    # connected components (union-find over edges)
    uf = {}

    def find(x):
        r = x
        while uf.get(r, r) != r:
            r = uf[r]
        while uf.get(x, x) != x:
            uf[x], x = r, uf[x]
        return r

    for cid, pnode in edges.items():
        uf[find(cid)] = find(id(pnode))

    comps = {}
    for n in body_nodes:
        comps.setdefault(find(id(n)), []).append(id(n))
    comp_roots = {}     # component key -> tree root id (no outgoing edge)
    for key, members in comps.items():
        roots = [m for m in members if m not in edges]
        comp_roots[key] = roots[0]

    # main root = root of the largest component (ties: earliest DFS)
    main_key = max(comps, key=lambda k: (len(comps[k]),
                                         -dfs_index[comp_roots[k]]))
    main_root = node_of_id[comp_roots[main_key]]

    # synthesize joints: link every other component root to its nearest
    # body-carrying NIF ancestor outside its own component (vanilla rock
    # pattern), fallback = the main tree root
    synthetic = []
    other_roots = sorted((comp_roots[k] for k in comps if k != main_key),
                         key=lambda nid: dfs_index[nid])
    for rid in other_roots:
        child = node_of_id[rid]
        target = None
        anc = node_parent.get(rid)
        while anc is not None:
            if id(anc) in body_of and find(id(anc)) != find(rid):
                target = anc
                break
            anc = node_parent.get(id(anc))
        if target is None and find(id(main_root)) != find(rid):
            target = main_root
        if target is None:
            continue
        synthetic.append((child, target))
        uf[find(rid)] = find(id(target))

    return {'body_nodes': body_nodes, 'edges': edges, 'synthetic': synthetic,
            'worlds': worlds, 'root': main_root, 'node_of_id': node_of_id}


def extract_ragdoll(skeleton_nif_path: str, bones: list):
    """Parse the Oblivion skeleton.nif into RagdollPart list (parent-before-
    child, constraints attached), or None when the skeleton has no ragdoll.

    EVERY blend-collision body becomes a ragdoll part of one connected
    constrained tree; unconstrained bodies (atronach rocks, detached
    skeleton-creature clusters) get synthetic vanilla-template joints to
    their nearest body-carrying ancestor (see plan_ragdoll_tree)."""
    data = NifFormat.Data()
    with open(skeleton_nif_path, 'rb') as f:
        data.read(f)

    plan = plan_ragdoll_tree(data)
    if plan is None:
        return None

    from asset_convert.hkx_skeleton import BONE_RENAMES
    bone_index = {b.name: i for i, b in enumerate(bones)}

    def anim_idx(node):
        name = _decode_name(node)
        return bone_index.get(BONE_RENAMES.get(name, name))

    if any(anim_idx(n) is None for n in plan['body_nodes']):
        return None     # body outside the anim skeleton — no usable ragdoll

    body_of = {id(n): n.collision_object.body for n in plan['body_nodes']}

    # bone-from-body transform per body node: the body's rotation/translation
    # are its BIND WORLD transform (see module docstring) while our ragdoll
    # bone frames are the anim bone frames — row convention
    # v_bone = v_body @ R_delta + t_delta.
    xf_of = {}
    for n in plan['body_nodes']:
        body = body_of[id(n)]
        q = body.rotation
        R_bw = _quat_to_mat_row((q.x, q.y, q.z, q.w))
        t_bw = _v4(body.translation, _OB_TO_GAME)
        R_bone, t_bone = plan['worlds'][id(n)]
        R_delta = R_bw @ R_bone.T
        t_delta = (t_bw - t_bone) @ R_bone.T
        xf_of[id(n)] = (R_delta, t_delta)

    def _to_bone(nid, v, is_point):
        R_delta, t_delta = xf_of[nid]
        out = np.asarray(v, dtype=float) @ R_delta
        return out + t_delta if is_point else out

    # per-child constraint info: real descriptors for planned edges,
    # synthetic vanilla-template ragdoll joints for the augmentation.
    # Everything is normalized here into bone-space game-unit dicts so the
    # XML emitters do no frame math.  Converted joints get friction 0.0
    # (vanilla creature census); synthetic rock joints keep the vanilla
    # atronach value.
    parent_of = {}          # id(child node) -> parent NiNode
    con_of = {}             # id(child node) -> (kind, info dict)
    for n in plan['body_nodes']:
        pnode = plan['edges'].get(id(n))
        if pnode is None:
            continue
        body, pbody = body_of[id(n)], body_of[id(pnode)]
        for con in getattr(body, 'constraints', []):
            kind, d = _descriptor(con)
            if kind is None:
                continue
            ents = list(con.entities)
            if not (len(ents) == 2 and ents[0] is body and ents[1] is pbody):
                continue
            cid, pid = id(n), id(pnode)
            if kind == 'ragdoll':
                info = {
                    'rows_a': _basis_rows(_to_bone(cid, _v4(d.twist_a), 0),
                                          _to_bone(cid, _v4(d.plane_a), 0)),
                    'rows_b': _basis_rows(_to_bone(pid, _v4(d.twist_b), 0),
                                          _to_bone(pid, _v4(d.plane_b), 0)),
                    'piv_a': _to_bone(cid, _v4(d.pivot_a, _OB_TO_GAME), 1),
                    'piv_b': _to_bone(pid, _v4(d.pivot_b, _OB_TO_GAME), 1),
                    'cone': float(d.cone_max_angle),
                    'plane_min': float(d.plane_min_angle),
                    'plane_max': float(d.plane_max_angle),
                    'twist_min': float(d.twist_min_angle),
                    'twist_max': float(d.twist_max_angle),
                    'friction': 0.0,
                }
            else:
                axle_a = _to_bone(cid, _v4(d.axle_a), 0)
                perp_a = getattr(d, 'perp_2_axle_in_a_1', None)
                rows_a = (_basis_rows(axle_a, _to_bone(cid, _v4(perp_a), 0))
                          if perp_a is not None
                          else _basis_rows(axle_a, np.array([0.0, 0.0, 1.0])))
                axle_b = _to_bone(pid, _v4(d.axle_b), 0)
                p2b = getattr(d, 'perp_2_axle_in_b_2', None)
                if p2b is not None:
                    # stored basis B = (axle, p1, p2); p1 = p2 × axle
                    p1b = np.cross(_unit(_v4(p2b)), _unit(_v4(d.axle_b)))
                    rows_b = _basis_rows(axle_b, _to_bone(pid, p1b, 0))
                else:
                    rows_b = _basis_rows(axle_b, np.array([0.0, 0.0, 1.0]))
                if kind == 'hinge':
                    min_a, max_a = float(d.min_angle), float(d.max_angle)
                else:
                    min_a, max_a = -math.pi, math.pi
                info = {
                    'rows_a': rows_a, 'rows_b': rows_b,
                    'piv_a': _to_bone(cid, _v4(d.pivot_a, _OB_TO_GAME), 1),
                    'piv_b': _to_bone(pid, _v4(d.pivot_b, _OB_TO_GAME), 1),
                    'min': min_a, 'max': max_a,
                    'friction': 0.0,
                }
            _legalize_limits(kind, info, plan['worlds'][cid][0],
                             plan['worlds'][pid][0])
            parent_of[id(n)] = pnode
            con_of[id(n)] = (kind, info)
            break

    for child, pnode in plan['synthetic']:
        body = body_of[id(child)]
        cid, pid = id(child), id(pnode)
        R_cw, t_cw = plan['worlds'][cid]
        R_pw, t_pw = plan['worlds'][pid]
        # pivot at the child body COM, expressed in each bone's frame
        com_child = _to_bone(cid, _v4(body.center, _OB_TO_GAME), 1)
        com_w = com_child @ R_cw + t_cw
        piv_parent = (com_w - t_pw) @ R_pw.T
        R_rel = R_cw @ R_pw.T           # child-frame vec -> parent frame
        parent_of[id(child)] = pnode
        con_of[id(child)] = ('ragdoll', {
            'rows_a': _basis_rows(np.array([1.0, 0.0, 0.0]),
                                  np.array([0.0, 1.0, 0.0])),
            'rows_b': _basis_rows(_unit(R_rel[0]), _unit(R_rel[1])),
            'piv_a': com_child,
            'piv_b': piv_parent,
            'cone': _SYNTH_CONE,
            'plane_min': -_SYNTH_PLANE, 'plane_max': _SYNTH_PLANE,
            'twist_min': -_SYNTH_TWIST, 'twist_max': _SYNTH_TWIST,
            'friction': _SYNTH_FRICTION,
        })

    # part order: DFS over the final tree (parent-before-child by
    # construction, required by hkaSkeleton parentIndices)
    dfs_index = {id(n): i for i, n in enumerate(plan['body_nodes'])}
    children = {}
    for cid, pnode in parent_of.items():
        children.setdefault(id(pnode), []).append(cid)
    for lst in children.values():
        lst.sort(key=dfs_index.__getitem__)

    node_of_id = plan['node_of_id']
    order = []
    stack = [id(plan['root'])]
    while stack:
        nid = stack.pop()
        order.append(nid)
        stack.extend(reversed(children.get(nid, [])))
    if len(order) != len(plan['body_nodes']):
        return None     # tree did not cover every body — bail to anim-only

    part_of_node = {}
    parts = []
    for nid in order:
        node = node_of_id[nid]
        body = body_of[nid]
        idx = anim_idx(node)
        p = RagdollPart()
        p.anim_index = idx
        p.name = 'Ragdoll_' + bones[idx].name
        pnode = parent_of.get(nid)
        p.parent = part_of_node[id(pnode)] if pnode is not None else -1
        p.constraint = con_of.get(nid)

        p.mass = float(body.mass) if body.mass > 0 else 1.0
        p.com = _to_bone(nid, _v4(body.center, _OB_TO_GAME), 1)
        shape = _capsule_from_shape(body.shape)
        if shape is not None:
            r, va, vb = shape
            p.shape = (r, _to_bone(nid, va, 1), _to_bone(nid, vb, 1))
        else:
            r = max(1.0, float(np.linalg.norm(p.com)))
            p.shape = (r, p.com - [0, 0, 0.5], p.com + [0, 0, 0.5])
        # Inertia is COMPUTED FROM THE CAPSULE, not carried from Oblivion
        # (2026-08-08, the rigid-ragdoll root cause).  Oblivion's authored
        # inertia diagonals are wildly ill-conditioned — our census of the
        # carried-through tensors hit 32x anisotropy on the forearm, 20x on
        # the thigh, vs vanilla Skyrim's WORST body at 6.6x — and a badly
        # ill-conditioned inertia on a constrained ragdoll body makes Havok's
        # joint solver diverge: the whole limp ragdoll stays RIGID, and the
        # destabilised island snaps to a fallback transform / drops out of
        # collision (teleport, fall-through-floor, can't be dragged, attached
        # parts lose their hitbox — every symptom, all on the frame physics
        # takes over).  Vanilla recomputes each body's tensor from its
        # capsule; so do we, giving the well-conditioned solid-capsule tensor
        # (analytic cylinder+hemisphere-caps approximation about the COM).
        # Computed for ALL parts after _widen_root_hub below, so the root hub's
        # tensor matches its final radius.

        part_of_node[nid] = len(parts)
        parts.append(p)

    for p in parts:
        p.inertia = _capsule_inertia(p.shape, p.mass)
    return parts


# ---------------------------------------------------------------------------
# DEAD END, do not rebuild: "re-root the ragdoll at anim bone 1"
#
# A `_ensure_root_at_bone1()` used to live here, on the theory that the ragdoll
# root must map to ANIM BONE 1 because vanilla dog's mapper #0121 maps ragdoll
# `A0 -> Canine_COM` (anim bone 1).  THREE variants were built and all three
# were user-confirmed BROKEN (corpses stopped ragdolling entirely and stood
# upright).  The theory itself is wrong:
#
#   vanilla dog bone 1 'Canine_COM'  world z = 59.3  <- UP IN THE BODY
#   scamp/rat  bone 1 'Bip01 NonAccum' world z = 71.7 / 27.3  (their NPC Root
#       carries the elevation, so NonAccum IS the trunk bone -> they work)
#   lion/boar/dog bone 1 'Bip01 NonAccum' world z = 0.0  <- AT THE FEET,
#       and 'Bip01 Spine0' (bone 2) is the trunk, 33-53 units up.
#
# The real invariant is "the ragdoll root is the TRUNK bone directly under
# NPC Root", NOT "bone index 1".  On lion/boar/dog `Spine0` ALREADY satisfies
# it, so `extract_ragdoll`'s natural root choice is correct and needs no
# adjustment; re-targeting moved the trunk body's FRAME to the ground while
# compensating the capsule offsets, and the engine's pose mapper /
# worldFromModel work off bone frames, not capsule offsets -> no simulation.
#
# The three variants, for the record: (1) hub inserted with a capsule spanning
# bone 1 -> old root = a 67-unit r=16.8 bar through the whole creature;
# (2) compact hub at the trunk = an exact duplicate of Spine0, doubling trunk
# mass; (3) re-target the existing root body onto bone 1 = trunk frame at the
# feet.  See docs/creature_conversion.md#ragdoll-root-bone1-dead-end.
# ---------------------------------------------------------------------------



# Bone-name WORDS identifying the chains vanilla leaves UNPINNED on a LIVE
# actor: the tail wags and the head/neck bob under physics while the
# locomotion body is keyframed to the animation.  Vanilla dog census
# (`KeyframeLowerBody`, 17 of 22 bones) omits exactly Tail1/2/3, Neck2 and
# Head — nothing else.
#
# Matched as whole WORDS, never as substrings: a plain `'ear' in name` test
# also matches "For<ear>m", which set every forearm (and its hand subtree)
# loose on a living creature.
_LOOSE_WHILE_ALIVE = ('tail', 'neck', 'head', 'skull', 'scull',
                      'ponytail', 'ear', 'jaw', 'tongue', 'wing')

# Trailing digits/side letters are part of the chain, not the word:
# 'Bip01 Tail3', 'Canine_Neck2', 'Bip01 L Ear01' all belong to their chain.
_WORD_RE = re.compile(r'[^a-z]+')

# The axial (trunk) chain: everything that is NOT a limb.  Used to find the
# limb ROOTS for the contact-listener set — vanilla lists limb roots plus the
# trunk's own links, never the toe/palm tips.
_AXIAL_WORDS = {'pelvis', 'spine', 'chest', 'ribcage', 'neck', 'head',
                'skull', 'scull', 'com', 'tail', 'nonaccum', 'torso',
                'body', 'abdomen', 'thorax'}


def _bone_words(name: str):
    """Lower-case word set of a bone name, digits stripped."""
    return {w for w in _WORD_RE.split(name.lower()) if w}


def _keyframe_bone_sets(parts):
    """The three vanilla `hkbBoneIndexArray` sets, in ragdoll indices.

    Returns (keyframe_full, keyframe_lower, contact).  **Never `range(n)`** —
    that was the 2026-08-08 root cause of the whole broken-corpse cluster
    (rigid limbs, teleport on death, sinking through the floor, dead pick
    geometry on the attached parts).  `hkbKeyframeBonesModifier` PINS each
    listed ragdoll body to the animation pose, and a pinned body is
    immovable by the solver and generates no contacts, so keyframing every
    body left nothing for gravity to act on and nothing for
    `BSRagdollContactListenerModifier` to fire on.

    Vanilla dogbehavior (22-body dog ragdoll) is the model:

    * `KeyframeFullRagdoll` — death state 3 `AnimateToRagdoll`, 18/22 bones:
      everything EXCEPT the deepest limb LEAVES (LBackLegToe, RFrontLeg2,
      L/RFrontLegPalm).  The extremities are already free the frame the
      ragdoll enters the world, so gravity gets a purchase and the corpse
      starts folding; the `Ragdoll` clip trigger then releases the rest.
    * `KeyframeLowerBody` — the LIVE root state, 17/22 bones: everything
      EXCEPT the tail chain, neck and head, which hang free so a living
      creature's tail wags and its head bobs under physics.
    * `CollisionListener.bones` — 8/22: the bones that actually touch the
      world (limb ROOTS, spine, neck, head), never the toe/palm tips.  These
      are what convert floor contact into the `Ragdoll` release event.
    """
    n = len(parts)
    children = {}
    for i, p in enumerate(parts):
        if p.parent >= 0:
            children.setdefault(p.parent, []).append(i)
    leaves = [i for i in range(n) if i not in children]

    def _depth(i):
        d = 0
        while parts[i].parent >= 0:
            i = parts[i].parent
            d += 1
        return d

    # --- KeyframeFullRagdoll: drop the deepest limb leaves.  Vanilla drops 4
    # of 22 (18%); scale that ratio, always at least one leaf, and never so
    # many that the trunk itself comes loose.
    n_free = max(1, round(n * 4.0 / 22.0))
    free_at_death = sorted(leaves, key=_depth, reverse=True)[:n_free]
    kf_full = [i for i in range(n) if i not in set(free_at_death)]

    # --- KeyframeLowerBody: drop the tail/neck/head chains entirely (a bone
    # is loose if it OR any ancestor is named as a loose chain, so the whole
    # sub-chain below Tail1 or Neck comes free, matching vanilla).
    def _loose(i):
        j = i
        while j >= 0:
            if _bone_words(parts[j].name) & set(_LOOSE_WHILE_ALIVE):
                return True
            j = parts[j].parent
        return False

    kf_lower = [i for i in range(n) if not _loose(i)]
    if not kf_lower:                       # all-loose rig (snake, tentacle)
        kf_lower = list(kf_full)

    # --- CollisionListener: only the bones that actually reach the world.
    # Vanilla dog is 8 of 22 (36%) — the four limb ROOTS (the first body of
    # each chain hanging off the trunk) plus the trunk's own spine/neck/head
    # links.  Deeper limb bodies and every leaf are excluded: a toe tip
    # scuffing the floor must not fire the `Ragdoll` release.
    # The TRUNK is the axial chain — the root plus every body whose name is a
    # spine/pelvis/neck/head link.  Walking "the single child that has
    # children" instead stops at the first spine node that also carries a
    # leg, which left the front limbs out of the contact set entirely.
    trunk = {0} | {i for i in range(n)
                   if _bone_words(parts[i].name) & _AXIAL_WORDS}
    # Limb roots: the first body of each chain hanging off the trunk that is
    # itself not a leaf (a lone leaf hanging off the spine is a fin/ear, not
    # a leg).
    limb_roots = {i for i in range(n)
                  if parts[i].parent in trunk and i not in trunk
                  and i in children}
    # The TAIL is excluded from contacts even though it is axial: vanilla's
    # 8-bone dog set is the four limb roots plus Spine1/Spine3/Neck2/Head,
    # with all three tail links absent.  A dragging tail must not fire the
    # `Ragdoll` release before the body has actually landed.
    spine_contacts = {i for i in trunk
                      if parts[i].parent >= 0
                      and not (_bone_words(parts[i].name) & {'tail'})}
    contact = sorted(limb_roots | spine_contacts)
    if not contact:
        contact = [i for i in range(n)
                   if i in children and parts[i].parent >= 0] or [0]
    return kf_full, kf_lower, contact


def ragdoll_info(skeleton_nif_path: str, bones: list):
    """Slim summary for the behavior generator (death/ragdoll states):

    pose_bones — pose-matching picks in RAGDOLL skeleton indices.  Vanilla
    dogbehavior uses (COM, RBackLegPalm, LBackLegPalm): the trunk root plus
    two SYMMETRIC low extremities, a wide well-conditioned triangle.  The
    old pick (root + the two deepest chain tips) could hand Havok a
    near-collinear tail-tip/toe triangle, and the pose matcher then derives
    a garbage worldFromModel — the corpse visibly teleported sideways on
    death and its collision no longer aligned with the rendered body
    (2026-08-07).  Generic rule: root part + the lowest opposite-side leaf
    pair, widest apart in X; depth picks only as a last resort.

    keyframe_full / keyframe_lower / contact_bones — the three
    `hkbBoneIndexArray` sets the behavior graph needs.  **None of them is
    `range(parts)`** (2026-08-08 root cause: all three were, which pinned
    every ragdoll body to the animation pose forever — see
    `_keyframe_bone_sets`).
    """
    try:
        parts = extract_ragdoll(skeleton_nif_path, bones)
    except Exception:
        return None
    if not parts:
        return None

    def _depth(i):
        d = 0
        while parts[i].parent >= 0:
            i = parts[i].parent
            d += 1
        return d

    worlds = _bone_worlds(bones)
    pos = [worlds[p.anim_index][1] for p in parts]
    parents = {p.parent for p in parts}
    leaves = [i for i in range(len(parts)) if i not in parents]
    zs = sorted(v[2] for v in pos)
    median_z = zs[len(zs) // 2]

    b1 = b2 = None
    low_leaves = [i for i in leaves if pos[i][2] <= median_z] or leaves
    best = -1.0
    for a in low_leaves:
        for b in low_leaves:
            if a >= b or pos[a][0] * pos[b][0] >= 0:
                continue  # need one left-side and one right-side extremity
            spread = abs(pos[a][0] - pos[b][0])
            if spread > best:
                best, b1, b2 = spread, a, b
    if b1 is None:  # no symmetric pair (snake-like chain): depth fallback
        order = sorted(range(len(parts)), key=_depth, reverse=True)
        b1 = order[0] if len(parts) > 1 else 0
        b2 = next((i for i in order if i not in (0, b1)), b1)

    kf_full, kf_lower, contact = _keyframe_bone_sets(parts)
    return {'parts': len(parts), 'pose_bones': (0, b1, b2),
            # the three vanilla hkbBoneIndexArray sets — see
            # _keyframe_bone_sets; NEVER range(parts)
            'keyframe_full': kf_full,
            'keyframe_lower': kf_lower,
            'contact_bones': contact,
            # part BONE names (the skeleton.nif NODE names, NOT the
            # 'Ragdoll_'-prefixed ragdoll-skeleton bone names), part order —
            # the import side builds the race's BPTD from these.  Vanilla
            # BPTD BPNN/BPNT name plain skeleton nodes ('Canine_Pelvis',
            # 'Scull'); the Ragdoll_ prefix shipped 2026-08-07 pointed every
            # body part at a nonexistent node.
            'part_bones': [bones[p.anim_index].name for p in parts]}


# ---------------------------------------------------------------------------
# XML emission
# ---------------------------------------------------------------------------

def _fmt_transform_rows(rows, t):
    return (fmt_vec(*rows[0]) + fmt_vec(*rows[1]) + fmt_vec(*rows[2])
            + fmt_vec(*t))


def _basis_rows(axis1, axis2):
    """Orthonormal (axis1, axis2', axis1×axis2') rows from two descriptor
    axes (Gram-Schmidt on axis2)."""
    a = np.asarray(axis1, dtype=float)
    a = a / (np.linalg.norm(a) or 1.0)
    b = np.asarray(axis2, dtype=float)
    b = b - a * float(a.dot(b))
    n = np.linalg.norm(b)
    if n < 1e-6:
        b = np.array([0.0, 0.0, 1.0]) if abs(a[2]) < 0.9 \
            else np.array([1.0, 0.0, 0.0])
        b = b - a * float(a.dot(b))
        n = np.linalg.norm(b)
    b = b / n
    return [a, b, np.cross(a, b)]


def _filter_info(part_index: int, parent_index: int) -> int:
    """Havok group-filter value for a ragdoll body (vanilla dog census):
    layer 0 (engine ORs the live layer in at attach), systemGroup 1, and
    the standard ragdoll subsystem chain — subSystemId = part+1,
    subSystemDontCollideWith = parent's subSystemId — so CONSTRAINED
    neighbours never collide while non-adjacent parts still do.  All-zero
    filter info lets every overlapping capsule collide with its neighbour
    and the ragdoll blasts itself apart on death (the 2026-07-16 mangled-
    ragdoll report, second root cause)."""
    sub = (part_index + 1) & 0x1F
    dont = ((parent_index + 1) & 0x1F) if parent_index >= 0 else 0
    return (1 << 16) | (dont << 10) | (sub << 5)


def _add_rigid_body(pf, part, world_R, world_t, filter_info=0):
    """hkpCapsuleShape + hkpRigidBody pair; returns (body, shape)."""
    shape = pf.add('hkpCapsuleShape')
    r, va, vb = part.shape
    shape.param('userData', 0)
    shape.param('radius', f'{r:.6f}')
    shape.param('vertexA', fmt_vec(va[0], va[1], va[2], r))
    shape.param('vertexB', fmt_vec(vb[0], vb[1], vb[2], r))

    com_w = part.com @ world_R + world_t
    quat = _mat_row_to_quat(world_R)
    r_obj = max(np.linalg.norm(va), np.linalg.norm(vb)) + r

    body = pf.add('hkpRigidBody')
    body.param('userData', 0)
    body.param_raw('collidable', f'''<hkobject>
\t<hkparam name="shape">{shape.ref}</hkparam>
\t<hkparam name="shapeKey">4294967295</hkparam>
\t<hkparam name="forceCollideOntoPpu">0</hkparam>
\t<hkparam name="broadPhaseHandle">
\t\t<hkobject>
\t\t\t<hkparam name="type">1</hkparam>
\t\t\t<hkparam name="objectQualityType">4</hkparam>
\t\t\t<hkparam name="collisionFilterInfo">{filter_info}</hkparam>
\t\t</hkobject>
\t</hkparam>
\t<hkparam name="allowedPenetrationDepth">0.100000</hkparam>
</hkobject>''')
    body.param_raw('multiThreadCheck', '<hkobject>\n</hkobject>')
    body.param('name', part.name)
    body.param_raw('properties', '', numelements=0)
    body.param_raw('material', '''<hkobject>
\t<hkparam name="responseType">RESPONSE_SIMPLE_CONTACT</hkparam>
\t<hkparam name="rollingFrictionMultiplier">0.000000</hkparam>
\t<hkparam name="friction">0.300000</hkparam>
\t<hkparam name="restitution">0.800000</hkparam>
</hkobject>''')
    body.param('damageMultiplier', '1.000000')
    body.param('storageIndex', 65535)
    body.param('contactPointCallbackDelay', 65535)
    body.param('autoRemoveLevel', 0)
    body.param('numShapeKeysInContactPointProperties', 0)
    body.param('responseModifierFlags', 0)
    body.param('uid', 4294967295)
    body.param_raw('spuCollisionCallback', '''<hkobject>
\t<hkparam name="eventFilter">3</hkparam>
\t<hkparam name="userFilter">1</hkparam>
</hkobject>''')
    ix, iy, iz = part.inertia
    inv_m = 1.0 / part.mass
    # Motion type — vanilla creature ragdolls are NOT uniformly BOX_INERTIA
    # (2026-08-08, the rigid-corpse root cause).  Dog census: 18 BOX + 4
    # SPHERE_INERTIA, and the 4 spheres are exactly the ROUND bodies — the
    # COM/torso hub, the mid-spine hub, and the tiny leg-tip caps — each with
    # an ISOTROPIC inertia (0.001,0.001,0.001 / 0.094,0.094,0.094).  A round,
    # heavily-CONSTRAINED hub carrying a strongly anisotropic box tensor
    # (ours shipped Spine3 invInertia (0.0002,0.0017,0.0002), 9x anisotropy)
    # ill-conditions Havok's ragdoll solver: the joint iteration cannot
    # converge, so the whole limp ragdoll stays RIGID and the destabilised
    # island snaps to a fallback transform and drops out of collision (the
    # teleport / fall-through-floor / can't-be-dragged / attached-parts-lose-
    # -collision cluster, all on the frame physics takes over).  A body is
    # "round" when its capsule segment is short relative to its radius; those
    # get SPHERE_INERTIA with the isotropic (min-axis) tensor a sphere has.
    # Long limb capsules keep BOX_INERTIA + their real anisotropic tensor
    # (an isotropic tensor there makes thin limbs tumble unnaturally).
    seg_len = float(np.linalg.norm(np.asarray(vb) - np.asarray(va)))
    is_round = seg_len < 0.8 * r
    if is_round:
        iso = min(ix, iy, iz)
        ix = iy = iz = iso
    motion_type = 'MOTION_SPHERE_INERTIA' if is_round else 'MOTION_BOX_INERTIA'
    body.param_raw('motion', f'''<hkobject>
\t<hkparam name="type">{motion_type}</hkparam>
\t<hkparam name="deactivationIntegrateCounter">15</hkparam>
\t<hkparam name="deactivationNumInactiveFrames">49152 49152</hkparam>
\t<hkparam name="motionState">
\t\t<hkobject>
\t\t\t<hkparam name="transform">{_fmt_transform_rows(world_R, world_t)}</hkparam>
\t\t\t<hkparam name="sweptTransform">
\t\t\t\t<hkobject>
\t\t\t\t\t<hkparam name="centerOfMass0">{fmt_vec(com_w[0], com_w[1], com_w[2], 0.0)}</hkparam>
\t\t\t\t\t<hkparam name="centerOfMass1">{fmt_vec(com_w[0], com_w[1], com_w[2], 0.0)}</hkparam>
\t\t\t\t\t<hkparam name="rotation0">{fmt_vec(*quat)}</hkparam>
\t\t\t\t\t<hkparam name="rotation1">{fmt_vec(*quat)}</hkparam>
\t\t\t\t\t<hkparam name="centerOfMassLocal">{fmt_vec(part.com[0], part.com[1], part.com[2], 0.0)}</hkparam>
\t\t\t\t</hkobject>
\t\t\t</hkparam>
\t\t\t<hkparam name="deltaAngle">(0.000000 0.000000 0.000000 0.000000)</hkparam>
\t\t\t<hkparam name="objectRadius">{r_obj:.6f}</hkparam>
\t\t\t<hkparam name="linearDamping">0.000000</hkparam>
\t\t\t<hkparam name="angularDamping">0.049805</hkparam>
\t\t\t<hkparam name="timeFactor">1.000000</hkparam>
\t\t\t<hkparam name="maxLinearVelocity">127</hkparam>
\t\t\t<hkparam name="maxAngularVelocity">127</hkparam>
\t\t\t<hkparam name="deactivationClass">2</hkparam>
\t\t</hkobject>
\t</hkparam>
\t<hkparam name="inertiaAndMassInv">{fmt_vec(1.0 / ix, 1.0 / iy, 1.0 / iz, inv_m)}</hkparam>
\t<hkparam name="linearVelocity">(0.000000 0.000000 0.000000 0.000000)</hkparam>
\t<hkparam name="angularVelocity">(0.000000 0.000000 0.000000 0.000000)</hkparam>
\t<hkparam name="deactivationRefPosition">(0.000000 0.000000 0.000000 0.000000) (0.000000 0.000000 0.000000 0.000000)</hkparam>
\t<hkparam name="deactivationRefOrientation">0 0</hkparam>
\t<hkparam name="savedMotion">null</hkparam>
\t<hkparam name="savedQualityTypeIndex">0</hkparam>
\t<hkparam name="gravityFactor">1.000000</hkparam>
</hkobject>''')
    body.param('localFrame', 'null')
    body.param('npData', 0)
    return body, shape


def _add_ragdoll_constraint_data(pf, info, motor_ref):
    """hkpRagdollConstraintData from a bone-space info dict (extract_ragdoll).

    motor_ref=None emits motors as null (the hkpPhysicsSystem copy);
    vanilla motorizes ONLY the hkaRagdollInstance constraint set."""
    motors = (f'{motor_ref} {motor_ref} {motor_ref}' if motor_ref
              else 'null null null')
    rows_a, rows_b = info['rows_a'], info['rows_b']
    piv_a, piv_b = info['piv_a'], info['piv_b']
    cone = info['cone']
    tgt = (fmt_vec(*rows_b[0]) + fmt_vec(*rows_b[1]) + fmt_vec(*rows_b[2]))

    data = pf.add('hkpRagdollConstraintData')
    data.param('userData', 0)
    data.param_raw('atoms', f'''<hkobject>
\t<hkparam name="transforms">
\t\t<hkobject>
\t\t\t<hkparam name="type">TYPE_SET_LOCAL_TRANSFORMS</hkparam>
\t\t\t<hkparam name="transformA">{_fmt_transform_rows(rows_a, piv_a)}</hkparam>
\t\t\t<hkparam name="transformB">{_fmt_transform_rows(rows_b, piv_b)}</hkparam>
\t\t</hkobject>
\t</hkparam>
\t<hkparam name="setupStabilization">
\t\t<hkobject>
\t\t\t<hkparam name="type">TYPE_SETUP_STABILIZATION</hkparam>
\t\t\t<hkparam name="enabled">false</hkparam>
\t\t\t<hkparam name="maxAngle">{_HUGE}</hkparam>
\t\t\t<hkparam name="padding">0 0 0 0 0 0 0 0</hkparam>
\t\t</hkobject>
\t</hkparam>
\t<hkparam name="ragdollMotors">
\t\t<hkobject>
\t\t\t<hkparam name="type">TYPE_RAGDOLL_MOTOR</hkparam>
\t\t\t<hkparam name="isEnabled">false</hkparam>
\t\t\t<hkparam name="initializedOffset">96</hkparam>
\t\t\t<hkparam name="previousTargetAnglesOffset">100</hkparam>
\t\t\t<hkparam name="target_bRca">{tgt}</hkparam>
\t\t\t<hkparam name="motors">{motors}</hkparam>
\t\t</hkobject>
\t</hkparam>
\t<hkparam name="angFriction">
\t\t<hkobject>
\t\t\t<hkparam name="type">TYPE_ANG_FRICTION</hkparam>
\t\t\t<hkparam name="isEnabled">1</hkparam>
\t\t\t<hkparam name="firstFrictionAxis">0</hkparam>
\t\t\t<hkparam name="numFrictionAxes">3</hkparam>
\t\t\t<hkparam name="maxFrictionTorque">{info['friction']:.6f}</hkparam>
\t\t</hkobject>
\t</hkparam>
\t<hkparam name="twistLimit">
\t\t<hkobject>
\t\t\t<hkparam name="type">TYPE_TWIST_LIMIT</hkparam>
\t\t\t<hkparam name="isEnabled">1</hkparam>
\t\t\t<hkparam name="twistAxis">0</hkparam>
\t\t\t<hkparam name="refAxis">1</hkparam>
\t\t\t<hkparam name="minAngle">{info['twist_min']:.6f}</hkparam>
\t\t\t<hkparam name="maxAngle">{info['twist_max']:.6f}</hkparam>
\t\t\t<hkparam name="angularLimitsTauFactor">0.800000</hkparam>
\t\t</hkobject>
\t</hkparam>
\t<hkparam name="coneLimit">
\t\t<hkobject>
\t\t\t<hkparam name="type">TYPE_CONE_LIMIT</hkparam>
\t\t\t<hkparam name="isEnabled">1</hkparam>
\t\t\t<hkparam name="twistAxisInA">0</hkparam>
\t\t\t<hkparam name="refAxisInB">0</hkparam>
\t\t\t<hkparam name="angleMeasurementMode">ZERO_WHEN_VECTORS_ALIGNED</hkparam>
\t\t\t<hkparam name="memOffsetToAngleOffset">56</hkparam>
\t\t\t<hkparam name="minAngle">-100.000000</hkparam>
\t\t\t<hkparam name="maxAngle">{cone:.6f}</hkparam>
\t\t\t<hkparam name="angularLimitsTauFactor">0.800000</hkparam>
\t\t</hkobject>
\t</hkparam>
\t<hkparam name="planesLimit">
\t\t<hkobject>
\t\t\t<hkparam name="type">TYPE_CONE_LIMIT</hkparam>
\t\t\t<hkparam name="isEnabled">1</hkparam>
\t\t\t<hkparam name="twistAxisInA">0</hkparam>
\t\t\t<hkparam name="refAxisInB">1</hkparam>
\t\t\t<hkparam name="angleMeasurementMode">ZERO_WHEN_VECTORS_PERPENDICULAR</hkparam>
\t\t\t<hkparam name="memOffsetToAngleOffset">0</hkparam>
\t\t\t<hkparam name="minAngle">{info['plane_min']:.6f}</hkparam>
\t\t\t<hkparam name="maxAngle">{info['plane_max']:.6f}</hkparam>
\t\t\t<hkparam name="angularLimitsTauFactor">0.800000</hkparam>
\t\t</hkobject>
\t</hkparam>
\t<hkparam name="ballSocket">
\t\t<hkobject>
\t\t\t<hkparam name="type">TYPE_BALL_SOCKET</hkparam>
\t\t\t<hkparam name="solvingMethod">METHOD_OLD</hkparam>
\t\t\t<hkparam name="bodiesToNotify">0</hkparam>
\t\t\t<hkparam name="velocityStabilizationFactor">48</hkparam>
\t\t\t<hkparam name="maxImpulse">{_MAX_IMPULSE}</hkparam>
\t\t\t<hkparam name="inertiaStabilizationFactor">0.000000</hkparam>
\t\t</hkobject>
\t</hkparam>
</hkobject>''')
    return data


def _add_hinge_constraint_data(pf, info, motor_ref=None):
    """hkpLimitedHingeConstraintData from a bone-space info dict
    (extract_ragdoll). Plain hinges get wide limits.

    motor_ref: hkpPositionConstraintMotor for the hkaRagdollInstance copy,
    None (null) for the hkpPhysicsSystem copy.  The engine's ragdoll attach
    dereferences the RAGDOLL set's angMotor.motor without a null check —
    hinge constraints with a null motor there crash SSE at actor Load3D
    (2026-07-09 Storm Atronach / Skeleton crash: every vanilla creature
    skeleton.hkx motorizes ALL ragdoll-instance constraints and nulls ALL
    physics-system copies)."""
    rows_a, rows_b = info['rows_a'], info['rows_b']
    piv_a, piv_b = info['piv_a'], info['piv_b']
    min_a, max_a = info['min'], info['max']
    friction = info['friction']

    data = pf.add('hkpLimitedHingeConstraintData')
    data.param('userData', 0)
    data.param_raw('atoms', f'''<hkobject>
\t<hkparam name="transforms">
\t\t<hkobject>
\t\t\t<hkparam name="type">TYPE_SET_LOCAL_TRANSFORMS</hkparam>
\t\t\t<hkparam name="transformA">{_fmt_transform_rows(rows_a, piv_a)}</hkparam>
\t\t\t<hkparam name="transformB">{_fmt_transform_rows(rows_b, piv_b)}</hkparam>
\t\t</hkobject>
\t</hkparam>
\t<hkparam name="setupStabilization">
\t\t<hkobject>
\t\t\t<hkparam name="type">TYPE_SETUP_STABILIZATION</hkparam>
\t\t\t<hkparam name="enabled">false</hkparam>
\t\t\t<hkparam name="maxAngle">{_HUGE}</hkparam>
\t\t\t<hkparam name="padding">0 0 0 0 0 0 0 0</hkparam>
\t\t</hkobject>
\t</hkparam>
\t<hkparam name="angMotor">
\t\t<hkobject>
\t\t\t<hkparam name="type">TYPE_ANG_MOTOR</hkparam>
\t\t\t<hkparam name="isEnabled">false</hkparam>
\t\t\t<hkparam name="motorAxis">0</hkparam>
\t\t\t<hkparam name="initializedOffset">64</hkparam>
\t\t\t<hkparam name="previousTargetAngleOffset">68</hkparam>
\t\t\t<hkparam name="correspondingAngLimitSolverResultOffset">16</hkparam>
\t\t\t<hkparam name="targetAngle">0.000000</hkparam>
\t\t\t<hkparam name="motor">{motor_ref or 'null'}</hkparam>
\t\t</hkobject>
\t</hkparam>
\t<hkparam name="angFriction">
\t\t<hkobject>
\t\t\t<hkparam name="type">TYPE_ANG_FRICTION</hkparam>
\t\t\t<hkparam name="isEnabled">1</hkparam>
\t\t\t<hkparam name="firstFrictionAxis">0</hkparam>
\t\t\t<hkparam name="numFrictionAxes">1</hkparam>
\t\t\t<hkparam name="maxFrictionTorque">{friction:.6f}</hkparam>
\t\t</hkobject>
\t</hkparam>
\t<hkparam name="angLimit">
\t\t<hkobject>
\t\t\t<hkparam name="type">TYPE_ANG_LIMIT</hkparam>
\t\t\t<hkparam name="isEnabled">1</hkparam>
\t\t\t<hkparam name="limitAxis">0</hkparam>
\t\t\t<hkparam name="minAngle">{min_a:.6f}</hkparam>
\t\t\t<hkparam name="maxAngle">{max_a:.6f}</hkparam>
\t\t\t<hkparam name="angularLimitsTauFactor">1.000000</hkparam>
\t\t</hkobject>
\t</hkparam>
\t<hkparam name="2dAng">
\t\t<hkobject>
\t\t\t<hkparam name="type">TYPE_2D_ANG</hkparam>
\t\t\t<hkparam name="freeRotationAxis">0</hkparam>
\t\t</hkobject>
\t</hkparam>
\t<hkparam name="ballSocket">
\t\t<hkobject>
\t\t\t<hkparam name="type">TYPE_BALL_SOCKET</hkparam>
\t\t\t<hkparam name="solvingMethod">METHOD_OLD</hkparam>
\t\t\t<hkparam name="bodiesToNotify">0</hkparam>
\t\t\t<hkparam name="velocityStabilizationFactor">48</hkparam>
\t\t\t<hkparam name="maxImpulse">{_MAX_IMPULSE}</hkparam>
\t\t\t<hkparam name="inertiaStabilizationFactor">0.000000</hkparam>
\t\t</hkobject>
\t</hkparam>
</hkobject>''')
    return data


def _add_constraint_instance(pf, data_ref, child_body_ref, parent_body_ref,
                             name):
    inst = pf.add('hkpConstraintInstance')
    inst.param('data', data_ref)
    inst.param('constraintModifiers', 'null')
    inst.param_raw('entities', f'{child_body_ref} {parent_body_ref}')
    inst.param('priority', 'PRIORITY_PSI')
    inst.param('wantRuntime', 'true')
    inst.param('destructionRemapInfo', 'ON_DESTRUCTION_REMAP')
    inst.param('name', name)
    inst.param('userData', 0)
    return inst


def emit_ragdoll(pf, bones, parts, anim_skel_ref):
    """Emit the full ragdoll object set; returns the extra namedVariants."""
    worlds = _bone_worlds(bones)

    # ragdoll hkaSkeleton — reference pose relative to the ragdoll parent
    rskel = pf.add('hkaSkeleton')
    rskel.param('name', parts[0].name)
    rskel.param_array('parentIndices', [p.parent for p in parts])
    rskel.param_structs('bones', [
        [('name', p.name), ('lockTranslation', p.parent != -1)]
        for p in parts])
    # The root's reference pose is its transform relative to the ACTOR ROOT
    # (anim bone 0).  Identical to writing world on every vanilla rig, whose
    # bone 0 sits at the origin; kept root-relative because Oblivion rigs
    # sometimes carry a bind transform on `Bip01` itself.
    #
    # NOTE: this is NOT what caused the teleport-on-death — that was
    # `lockTranslation` on the ragdoll root's mapped anim bone (see
    # hkx_skeleton.build_skeleton_xml).  Changing this alone measured as a
    # pure no-op on every broken creature.
    _R_actor, t_actor = worlds[0]
    pose_lines = []
    for p in parts:
        R, t = worlds[p.anim_index]
        if p.parent < 0:
            lt = (t - t_actor) @ _R_actor.T
            lq = _mat_row_to_quat(R @ _R_actor.T)
        else:
            Rp, tp = worlds[parts[p.parent].anim_index]
            inv = Rp.T
            lt = (t - tp) @ inv
            lq = _mat_row_to_quat(R @ inv)
        pose_lines.append(fmt_vec(*lt) + fmt_vec(*lq)
                          + fmt_vec(1.0, 1.0, 1.0))
    rskel.param_raw('referencePose', '\n'.join(pose_lines),
                    numelements=len(parts))
    rskel.param_array('referenceFloats', [])
    rskel.param_raw('floatSlots', '', numelements=0)
    rskel.param_raw('localFrames', '', numelements=0)

    # mappers (identity aFromB — ragdoll frames coincide with anim frames)
    ident = ('(0.000000 0.000000 0.000000)'
             '(0.000000 0.000000 0.000000 1.000000)'
             '(1.000000 1.000000 1.000000)')

    def _mapper(a_ref, b_ref, pairs, unmapped):
        m = pf.add('hkaSkeletonMapper')
        rows = '\n'.join(
            f'<hkobject>\n\t<hkparam name="boneA">{a}</hkparam>\n'
            f'\t<hkparam name="boneB">{b}</hkparam>\n'
            f'\t<hkparam name="aFromBTransform">{ident}</hkparam>\n'
            f'</hkobject>' for a, b in pairs)
        unmapped_s = ' '.join(str(u) for u in unmapped)
        m.param_raw('mapping', f'''<hkobject>
\t<hkparam name="skeletonA">{a_ref}</hkparam>
\t<hkparam name="skeletonB">{b_ref}</hkparam>
\t<hkparam name="simpleMappings" numelements="{len(pairs)}">
{rows}
\t</hkparam>
\t<hkparam name="chainMappings" numelements="0"></hkparam>
\t<hkparam name="unmappedBones" numelements="{len(unmapped)}">
\t\t{unmapped_s}
\t</hkparam>
\t<hkparam name="extractedMotionMapping">{ident}</hkparam>
\t<hkparam name="keepUnmappedLocal">true</hkparam>
\t<hkparam name="mappingType">HK_RAGDOLL_MAPPING</hkparam>
</hkobject>''')
        return m

    # unmappedBones are indices in skeleton B (vanilla dog census: the
    # ragdoll->anim mapper lists the 28 anim bones with no ragdoll part; the
    # anim->ragdoll mapper lists none).  Putting anim indices on the
    # anim->ragdoll mapper instead points past the end of the ragdoll
    # skeleton — out-of-range bone indices in the engine's pose mapper.
    mapped_anim = {p.anim_index for p in parts}
    unmapped_anim = [i for i in range(len(bones)) if i not in mapped_anim]
    map_r2a = _mapper(rskel.ref, anim_skel_ref,
                      [(ri, p.anim_index) for ri, p in enumerate(parts)],
                      unmapped_anim)
    map_a2r = _mapper(anim_skel_ref, rskel.ref,
                      [(p.anim_index, ri) for ri, p in enumerate(parts)],
                      [])

    # vanilla motor values (dog skeleton.hkx #0126) — the omitted-`type`
    # default is TYPE_INVALID, which the solver dispatches on; always emit it
    motor = pf.add('hkpPositionConstraintMotor')
    motor.param('type', 'TYPE_POSITION')
    motor.param('minForce', '-1000000.000000')
    motor.param('maxForce', '100.000000')
    motor.param('tau', '0.800000')
    motor.param('damping', '1.000000')
    motor.param('proportionalRecoveryVelocity', '5.000000')
    motor.param('constantRecoveryVelocity', '0.200000')

    body_shapes = [_add_rigid_body(pf, p, *worlds[p.anim_index],
                                   filter_info=_filter_info(i, p.parent))
                   for i, p in enumerate(parts)]
    bodies = [b for b, _s in body_shapes]
    shapes = [s for _b, s in body_shapes]

    def _constraints(motor_ref):
        insts = []
        for ri, p in enumerate(parts):
            if p.constraint is None or p.parent < 0:
                continue
            kind, info = p.constraint
            if kind == 'ragdoll':
                data = _add_ragdoll_constraint_data(pf, info, motor_ref)
            else:
                data = _add_hinge_constraint_data(pf, info, motor_ref)
            insts.append(_add_constraint_instance(
                pf, data.ref, bodies[ri].ref, bodies[p.parent].ref, p.name))
        return insts

    # vanilla duplicates the constraint graph: the hkaRagdollInstance set is
    # fully motored, the hkpPhysicsSystem set is fully null (bodies shared)
    con_ragdoll = _constraints(motor.ref)
    con_system = _constraints(None)

    ragdoll = pf.add('hkaRagdollInstance')
    ragdoll.param_array('rigidBodies', [b.ref for b in bodies])
    ragdoll.param_array('constraints', [c.ref for c in con_ragdoll])
    ragdoll.param_array('boneToRigidBodyMap', list(range(len(parts))))
    ragdoll.param('skeleton', rskel.ref)

    system = pf.add('hkpPhysicsSystem')
    system.param_array('rigidBodies', [b.ref for b in bodies])
    system.param_array('constraints', [c.ref for c in con_system])
    system.param_array('actions', [])
    system.param_array('phantoms', [])
    system.param('name', 'Default Physics System')
    system.param('userData', 0)
    system.param('active', True)

    pdata = pf.add('hkpPhysicsData')
    pdata.param('worldCinfo', 'null')
    pdata.param_array('systems', [system.ref])

    # 'Resource Data' tree (vanilla creature skeleton.hkx, dog census): one
    # hkMemoryResourceContainer PER RAGDOLL PART, named after the part,
    # nested along the ragdoll parent tree, each holding two
    # hkMemoryResourceHandles — 'hkRigidBody' -> the part's hkpRigidBody and
    # 'hkpShapeInfo' -> an hkpShapeInfo naming the part and carrying its
    # bind world transform.  Our old empty container was the last structural
    # delta against vanilla: this tree is the Bethesda-side registry of the
    # ragdoll parts (name -> body/shape), so ship it verbatim.
    ident_t = ('(1.000000 0.000000 0.000000)(0.000000 1.000000 0.000000)'
               '(0.000000 0.000000 1.000000)(0.000000 0.000000 0.000000)')
    kids = {}
    for i, p in enumerate(parts):
        if p.parent >= 0:
            kids.setdefault(p.parent, []).append(i)
    part_res = [None] * len(parts)
    for i in reversed(range(len(parts))):   # children before their parent:
        p = parts[i]                        # hkxcmd rejects forward refs
        R, t = worlds[p.anim_index]
        sinfo = pf.add('hkpShapeInfo')
        sinfo.param('shape', shapes[i].ref)
        sinfo.param('isHierarchicalCompound', False)
        sinfo.param('hkdShapesCollected', False)
        sinfo.param_raw('childShapeNames',
                        f'<hkcstring>{p.name}</hkcstring>', numelements=1)
        sinfo.param_raw('childTransforms', ident_t, numelements=1)
        sinfo.param('transform', _fmt_transform_rows(R, t))
        h_rb = pf.add('hkMemoryResourceHandle')
        h_rb.param('variant', bodies[i].ref)
        h_rb.param('name', 'hkRigidBody')
        h_rb.param_array('references', [])
        h_si = pf.add('hkMemoryResourceHandle')
        h_si.param('variant', sinfo.ref)
        h_si.param('name', 'hkpShapeInfo')
        h_si.param_array('references', [])
        cont = pf.add('hkMemoryResourceContainer')
        cont.param('name', p.name)
        cont.param_array('resourceHandles', [h_rb.ref, h_si.ref])
        cont.param_array('children',
                         [part_res[j].ref for j in kids.get(i, [])])
        part_res[i] = cont

    resource = pf.add('hkMemoryResourceContainer')
    resource.param('name', '')
    resource.param_array('resourceHandles', [])
    resource.param_array('children',
                         [part_res[i].ref for i, p in enumerate(parts)
                          if p.parent < 0])

    return rskel, [
        [('name', 'Resource Data'),
         ('className', 'hkMemoryResourceContainer'),
         ('variant', resource.ref)],
        [('name', 'Physics Data'), ('className', 'hkpPhysicsData'),
         ('variant', pdata.ref)],
        [('name', 'RagdollInstance'), ('className', 'hkaRagdollInstance'),
         ('variant', ragdoll.ref)],
        # namedVariants mapper order is a hard vanilla contract (census
        # 2026-07-20, 30/30 creature skeleton.hkx): the anim->ragdoll mapper
        # is listed FIRST, ragdoll->anim second.  Reversed order feeds the
        # engine's death-pose transfer the wrong mapping table.
        [('name', 'SkeletonMapper'), ('className', 'hkaSkeletonMapper'),
         ('variant', map_a2r.ref)],
        [('name', 'SkeletonMapper'), ('className', 'hkaSkeletonMapper'),
         ('variant', map_r2a.ref)],
    ]
