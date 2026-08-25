"""PyFFI 2.2.3 monkey-patches for correct Skyrim NIF (BSStream 83) output.

PyFFI 2.2.3 ships nif.xml version 0.7.1.1.  This module corrects several
field-condition bugs discovered by comparing against the newer nif.xml 0.9.x
used by NifSkope.  All patches must be applied *before* any NIF read or write
operation.

Usage
-----
Import this module at the very top of any file that uses PyFFI, before
importing NifFormat::

    import asset_convert.pyffi_monkey_patch  # apply patches
    from pyffi.formats.nif import NifFormat

The module is idempotent – importing it multiple times is safe.

Summary of patches
------------------
1. time.clock compatibility
   Python 3.8 removed time.clock().  PyFFI uses it internally.  Replaced with
   time.perf_counter().

2. NiPSysGrowFadeModifier.base_scale  (v0.7.1.1 bug)
   PyFFI: userver="11" — field present only when user_version == 11 (Oblivion).
   v0.9 spec: vercond="User Version 2 >= 34" — present when UV2 >= 34 (Skyrim).
   Effect: without the patch, writing a Skyrim NIF (UV1=12) silently omits the
   4-byte base_scale field, shifting all subsequent bytes by 4.  Particles
   appear invisible (size = 0) or crash the engine.
   Fix: clear the userver constraint so the field is always written for
   version >= 20.2.0.7.

3. NiPSysData.unknown_short_1 / unknown_short_2  (v0.7.1.1 bug)
   PyFFI nif.xml line ~2995:
     vercond="!((Version >= 20.2.0.7) && (User Version == 11))"
   This makes the two shorts ABSENT for FO3 (UV1=11) but PRESENT for Skyrim
   (UV1=12), producing 4 extra bytes in Skyrim NiPSysData binary output.
   v0.9 spec: these are "Num Added Particles" and "Added Particles Base" with
     vercond="!((Version == 20.2.0.7) && (User Version 2 > 0))"
   — absent for ALL Bethesda 20.2 formats including Skyrim.
   Fix: change condition to user_version >= 11 so they are absent in both FO3
   and Skyrim when version == 20.2.0.7.

9. StructBase._log_struct -> no-op  (PERFORMANCE ONLY)
   PyFFI calls a debug-logging helper for every attribute of every struct on
   both read and write, doing getattr/isinstance/get_value/str.format work
   that the (never-enabled) logger then throws away.  ~11.5% of NIF conversion
   time.  Replaced with a no-op unless DEBUG is actually enabled for the
   "pyffi.nif.data.struct" logger.  Cannot affect output bytes.
   Set TESCONV_PYFFI_NO_PERF_PATCH=1 to disable (for A/B measurement).

10. Deterministic header string table  (CORRECTNESS)
   Data.write() deduplicated the string table with `list(set(...))` over BYTES,
   whose hash is randomised per process, so the same source NIF produced
   different output bytes on every run — identical blocks and geometry, but a
   reordered string table and therefore different NiStringRef indices.  Made
   insertion-ordered.  Verify with `python tools/nif_determinism.py`.
"""

import os
import sys
import time as _time

# ---------------------------------------------------------------------------
# Patch 1: time.clock (removed in Python 3.8)
# ---------------------------------------------------------------------------
if not hasattr(_time, 'clock'):
    _time.clock = _time.perf_counter


# ---------------------------------------------------------------------------
# Patches requiring NifFormat (applied lazily on first import)
# ---------------------------------------------------------------------------
_PYFFI_PATCHED = False


def _apply_nifformat_patches(NifFormat):
    """Apply patches to a loaded NifFormat.  Called once after import."""
    from pyffi.object_models.xml.expression import Expression

    # ------------------------------------------------------------------
    # Patch 2: NiPSysGrowFadeModifier.base_scale
    # ------------------------------------------------------------------
    # The field is defined with userver="11" (exact match on UV1=11, Oblivion).
    # For Skyrim (UV1=12) PyFFI omits it entirely, creating a 4-byte hole that
    # the engine misreads.  v0.9 specifies UV2>=34 (i.e. always present for
    # any modern Bethesda NIF).  Clearing userver removes the restriction so
    # the field is written whenever ver1 (20.2.0.7) is satisfied.
    for _attr in NifFormat.NiPSysGrowFadeModifier._attrs:
        if _attr.name == 'base_scale':
            _attr.userver = None
            break

    # ------------------------------------------------------------------
    # Patch 3: NiPSysData.unknown_short_1 / unknown_short_2
    # ------------------------------------------------------------------
    # PyFFI condition: "!((Version >= 20.2.0.7) && (User Version == 11))"
    #   → absent for FO3 (UV1=11), PRESENT for Skyrim (UV1=12).  WRONG.
    # Correct (per v0.9): absent for ALL Bethesda 20.2 (UV2 > 0).
    # We approximate this as user_version >= 11 (excludes both FO3 & Skyrim)
    # which matches the v0.9 semantics for all platforms we care about.
    # Parenthesization is CRITICAL: Expression parses the unparenthesized
    # '! version >= X && ...' as '((!version) >= X) && ...' = always False,
    # which drops the two shorts from OBLIVION reads as well — every source
    # NIF containing NiPSysData then misaligns by 4 bytes and fails to read
    # (the entire fire/effects/magiceffects [RD] failure list).
    _psy_fixed_expr = Expression(
        '!((version >= 335675399) && (user_version >= 11))'
    )
    for _attr in NifFormat.NiPSysData._attrs:
        if _attr.name in ('unknown_short_1', 'unknown_short_2'):
            _attr.vercond = _psy_fixed_expr

    # ------------------------------------------------------------------
    # Patch 4: hand-rolled NiPSysData layout for Skyrim (BSStream 83)
    # ------------------------------------------------------------------
    _install_skyrim_psysdata_serializer(NifFormat)

    # ------------------------------------------------------------------
    # Patch 5-7: early-Oblivion (10.0.1.x / 10.1.0.106) layout support
    # ------------------------------------------------------------------
    _install_early_oblivion_layouts(NifFormat)

    # ------------------------------------------------------------------
    # Patch 8: Skyrim SE (BSStream 100) read support
    # ------------------------------------------------------------------
    _install_sse_layouts(NifFormat)


# ---------------------------------------------------------------------------
# Patches 5-7: early-Oblivion NIF layout support
# ---------------------------------------------------------------------------
# Oblivion.esm's BSAs contain a handful of development-era meshes saved with
# NIF versions 10.0.1.0 / 10.0.1.2 / 10.1.0.106 instead of the shipping
# 10.2.0.0 (e.g. clutter\floorplane01.nif, clutter\farm\oar01.nif,
# architecture\castle\kvatch\..., oblivion\...\scampswitch01.nif).  PyFFI
# 2.2.3 lacks the version guards these layouts need (verified against
# references/nif 0.10.0.0.xml):
#
# 5. Field-presence guards:
#    - bhkWorldObject: extra "Unknown Int" (4B) after Shape, until 10.0.1.2.
#    - HavokMaterial: extra "Unknown Int" (4B) before the material enum,
#      until 10.0.1.2.  (Affects every bhk shape.)
#    - bhkRigidBody CInfo: the 16-byte header (unknown_2_shorts,
#      havok_col_filter_copy, unknown_6_shorts[0:4]) and the 12-byte
#      max_linear/max_angular/penetration_depth group exist only since
#      10.1.0.0; before that only a 4-byte unused field sits between the
#      contact-callback delay and Translation.
#    - bhkMoppBvTreeShape: the hkpMoppCode Offset vector (pyffi origin+scale,
#      16B) exists only since 10.1.0.0.
# 6. NiSingleInterpController.Interpolator: introduced 10.1.0.104 per the
#    newer nif.xml; pyffi guards it at 10.2.0.0, breaking every controller
#    in 10.1.0.106 files.
# 7. bhkConvexSweepShape: block type used by 10.0.1.0-era clutter
#    (handscythe01, oar01), missing from pyffi entirely.  Registered here;
#    the converter unwraps it to its inner shape (Skyrim never ships it).

_V10_0_1_2 = 0x0A000102
_V10_1_0_0 = 0x0A010000
_V10_1_0_104 = 0x0A010068
_V10_2_0_0 = 0x0A020000
_V20_1_0_2 = 0x14010002


def _make_attr(NifFormat, xml_attrs, ver1=None, ver2=None, template=None):
    """Build a resolved StructAttribute from an xml-style attrs dict."""
    from pyffi.object_models.xml import StructAttribute
    attr = StructAttribute(NifFormat, xml_attrs)
    attr.ver1 = ver1
    attr.ver2 = ver2
    if template is not None:
        attr.template = template
    return attr


def _refresh_attribute_caches(NifFormat, changed_classes):
    """Recompute the flattened _attribute_list cache for every NifFormat
    class that inherits from one of changed_classes (the caches are built
    once at class-creation time and hold stale copies after _attrs edits)."""
    for name in dir(NifFormat):
        cls = getattr(NifFormat, name, None)
        if not isinstance(cls, type):
            continue
        if not any(issubclass(cls, c) for c in changed_classes):
            continue
        if hasattr(cls, '_get_attribute_list'):
            cls._attribute_list = cls._get_attribute_list()


def _install_early_oblivion_layouts(NifFormat):
    from pyffi.object_models.xml.expression import Expression

    # --- Patch 5a: bhkWorldObject extra int (until 10.0.1.2) --------------
    wo = NifFormat.bhkWorldObject
    if not any(a.name == 'unknown_int_early' for a in wo._attrs):
        extra = _make_attr(NifFormat,
                           {'name': 'Unknown Int Early', 'type': 'uint'},
                           ver2=_V10_0_1_2)
        # Reference order: Shape, Unknown Int (old), Havok Filter.
        wo._attrs.insert(1, extra)

    # --- Patch 5b: HavokMaterial extra int (until 10.0.1.2) ---------------
    hm = NifFormat.HavokMaterial
    if not any(a.name == 'unknown_int_early' for a in hm._attrs):
        extra = _make_attr(NifFormat,
                           {'name': 'Unknown Int Early', 'type': 'uint'},
                           ver2=_V10_0_1_2)
        hm._attrs.insert(0, extra)

    # --- Patch 5c: bhkRigidBody CInfo fields introduced at 10.1.0.0 -------
    rb = NifFormat.bhkRigidBody
    gate_at_10_1 = ('unknown_2_shorts', 'havok_col_filter_copy',
                    'unknown_6_shorts', 'max_linear_velocity',
                    'max_angular_velocity', 'penetration_depth')
    for a in rb._attrs:
        if a.name in gate_at_10_1 and a.ver1 is None:
            a.ver1 = _V10_1_0_0
    if not any(a.name == 'unused_early' for a in rb._attrs):
        # The 4-byte unused field old files DO have where the 16-byte header
        # would sit (reference bhkRigidBodyCInfo550_660 "Unused 04").
        unused = _make_attr(NifFormat,
                            {'name': 'Unused Early', 'type': 'ushort',
                             'arr1': '2'},
                            ver2=_V10_0_1_2)
        idx = next(i for i, a in enumerate(rb._attrs)
                   if a.name == 'unknown_6_shorts')
        rb._attrs.insert(idx + 1, unused)

    # --- Patch 5d: bhkMoppBvTreeShape offset vector since 10.1.0.0 --------
    for a in NifFormat.bhkMoppBvTreeShape._attrs:
        if a.name in ('origin', 'scale') and a.ver1 is None:
            a.ver1 = _V10_1_0_0
        # pyffi reads "mopp_data_size - 1" bytes for files <= 10.0.1.0, but
        # Bethesda 10.0.1.0 meshes store the FULL size (verified byte-by-byte
        # on ungrdltraphingedoor.nif: the +1 shift lands response=1,
        # delay=0xFFFF and a unit quaternion in the following rigid body).
        # Push the old convention below Bethesda's version range.
        if a.name == 'old_mopp_data':
            a.ver2 = 0x0A000100 - 1
        if a.name == 'mopp_data':
            a.ver1 = 0x0A000100

    # --- Patch 5e: bhkNiTriStripsShape scale vector since 10.1.0.0 --------
    # Reference: "Scale" Vector4 since="10.1.0.0" (pyffi splits it into
    # scale Vector3 + unknown_int_3).  Absent in 10.0.1.x (kvatch castle
    # int hallway01, stonepedastellarge01).
    for a in NifFormat.bhkNiTriStripsShape._attrs:
        if a.name in ('scale', 'unknown_int_3') and a.ver1 is None:
            a.ver1 = _V10_1_0_0

    # --- Patch 6: NiSingleInterpController.interpolator since 10.1.0.104 --
    for a in NifFormat.NiSingleInterpController._attrs:
        if a.name == 'interpolator':
            a.ver1 = _V10_1_0_104

    # --- Patch 6a2: NiPSysEmitterCtlr.visibility_interpolator --------------
    # since="10.1.0.104" per reference nif.xml; pyffi gates it at 10.2.0.0.
    for a in NifFormat.NiPSysEmitterCtlr._attrs:
        if a.name == 'visibility_interpolator':
            a.ver1 = _V10_1_0_104

    # --- Patch 6b: NiInterpController "Manager Controlled" byte -----------
    # Exists only in 10.1.0.104..10.1.0.108 (reference nif.xml); sits between
    # NiTimeController.target and NiSingleInterpController.interpolator.
    # Verified on scampswitch01.nif: bytes ... target=0, 01 (this byte),
    # interpolator=5 (the controller's own blend interpolator).
    ic = NifFormat.NiInterpController
    if not any(a.name == 'manager_controlled' for a in ic._attrs):
        mc = _make_attr(NifFormat,
                        {'name': 'Manager Controlled', 'type': 'byte'},
                        ver1=_V10_1_0_104, ver2=0x0A01006C)
        ic._attrs.insert(0, mc)

    # --- Patch 6c: NiBlendInterpolator pre-10.1.0.108 layout --------------
    # 10.1.0.106 blend interpolators store a full runtime blend-item array:
    #   ArraySize(u16) ArrayGrowBy(u16) Items[ArraySize]{ref,f,f,i32,f}
    #   ManagerControlled(u8) WeightThreshold(f) OnlyUseHighestWeight(u8)
    #   InterpCount(u16) SingleIndex(u16) HighPriority(i32) NextHighPriority(i32)
    # pyffi only knows the 10.1.0.112+ 6-byte layout, so every block after a
    # blend interpolator misparses.  We consume the old layout and leave the
    # pyffi attrs at defaults (Skyrim output writes a fresh blend state; the
    # sub-interpolators stay reachable through the controller sequences).
    # Item refs are deliberately NOT pushed on the link stack — the class has
    # no Ref attrs for fix_links to pop, so pushing would desync all links.
    import struct as _struct2
    _bi = NifFormat.NiBlendInterpolator
    _orig_bi_read = _bi.read

    def _bi_read(self, stream, data=None):
        ver = getattr(data, 'version', 0) if data is not None else 0
        if not (_V10_1_0_104 <= ver <= 0x0A01006B):
            _orig_bi_read(self, stream, data=data)
            return
        n, _grow = _struct2.unpack('<HH', stream.read(4))
        stream.read(n * 20)      # InterpBlendItem[n]: ref,weight,normWeight,priority,easeSpinner
        stream.read(1 + 4 + 1)   # managerControlled, weightThreshold, onlyUseHighest
        stream.read(2 + 2)       # interpCount, singleIndex
        stream.read(4 + 4)       # highPriority, nextHighPriority
        # Subclass value snapshot (byte-verified on scampswitch01.nif — each
        # block ends exactly at the next block's zero tag):
        #   Transform: translation(3f) + rotation quat(4f) + scale(1f) = 32B
        #              + 3 valid-flag bytes = 35
        #   Point3:    value(3f) = 12
        #   Float:     value(1f) = 4      (by the same pattern)
        #   Bool:      value(1B) = 1
        extra = {'NiBlendTransformInterpolator': 35,
                 'NiBlendPoint3Interpolator': 12,
                 'NiBlendFloatInterpolator': 4,
                 'NiBlendBoolInterpolator': 1}.get(type(self).__name__, 0)
        if extra:
            stream.read(extra)

    _bi.read = _bi_read

    # --- Patch 6d: NiGeomMorpherController phantom byte at 10.1.0.106 -----
    # pyffi's old nif.xml carries an "Unknown 2" UByte gated to EXACTLY
    # 10.1.0.106 between Extra Flags and Data.  The reference nif.xml has no
    # such field in ANY version (Morpher Flags → Data → Always Update →
    # Num Interpolators), and real 10.1.0.106 files (mountainlion head/paws,
    # minotaur head/eyelids — dev-era creature morph meshes) prove it: the
    # extra byte desyncs the stream one byte late, so num_interpolators
    # reads 0x05000000-style garbage and the whole file fails [RD].
    gm = NifFormat.NiGeomMorpherController
    gm._attrs[:] = [a for a in gm._attrs if a.name != 'unknown_2']

    # ...and its Unknown Ints array starts at 10.2.0.0, not 20.0.0.4.  The
    # reference gates the pair
    #   since="10.2.0.0" until="20.0.0.5" vercond="#BSVER# #GT# 9"
    # while pyffi floors both at 20.0.0.4, so on a 10.2.0.0 / bsver 11 file it
    # skips them and the block ends 24 bytes short -- the stream then reads
    # NiMorphData from the middle of the controller.  Measured on
    # mudcrab\mud crbeye l00.nif: the controller ends at 0x2A3, NiMorphData
    # truly begins at 0x2BB, and 0x2A3 holds num_unknown_ints=5 followed by
    # five zero ints = 4 + 5*4 = 24 bytes, landing exactly on 0x2BB.
    # pyffi's 'user_version >= 10' vercond is also the wrong field; BSVER is
    # user_version_2.
    _gm_unknown_vercond = Expression('user_version_2 > 9')
    for _attr in gm._attrs:
        if _attr.name in ('num_unknown_ints', 'unknown_ints'):
            _attr.ver1 = _V10_2_0_0
            _attr.vercond = _gm_unknown_vercond

    # --- Patch 6e: Morph "Legacy Weight" is gated on BSVER, not version ---
    # pyffi models the reference nif.xml's Morph."Legacy Weight" float as an
    # 'unknown_int' gated ver1=10.1.0.106 ver2=10.2.0.0 -- a pure version
    # range.  The reference gates it
    #   since="10.1.0.104" until="20.1.0.2" vercond="#BSVER# #LT# 10"
    # so on a 10.2.0.0 file the field's presence depends on the BS version,
    # which pyffi never consults.  Both of pyffi's entries are also literally
    # named 'unknown_int', and _get_filtered_attribute_list skips duplicate
    # names, so the second can never be read at all.
    #
    # Census of every creature NIF containing a NiMorphData (Oblivion.esm):
    #   ver 10.1.0.106 bsver 5   -> present  (goblinhead, 7 files)
    #   ver 10.2.0.0   bsver 9   -> present  (doghead, 2 files)
    #   ver 10.2.0.0   bsver 11  -> ABSENT   (minotaur eyelidslord, mudcrab
    #                                         eyes -- the 3 files that failed)
    # Byte-walking mud crbeye l00.nif settles it: NiMorphData at 0x2BB reads
    # num_morphs=5 num_vertices=90 relative_targets=1, and only the no-field
    # layout parses all five morph names (Base, Mud Crbeye L01..L04); with the
    # field the second morph's name length is garbage and the file fails [RD].
    # Note ver2 is INCLUSIVE in pyffi (`version > attr.ver2` skips), so the
    # old range covered 10.2.0.0 exactly.
    #
    # Restoring the reference gating verbatim -- widen the version range and
    # add the BSVER vercond -- keeps the bsver 5/9 files reading as they do
    # today while dropping the phantom 4 bytes per morph on bsver >= 10.
    _legacy_weight_vercond = Expression('user_version_2 < 10')
    for _attr in NifFormat.Morph._attrs:
        if _attr.name != 'unknown_int':
            continue
        _attr.ver1 = _V10_1_0_104
        _attr.ver2 = _V20_1_0_2
        _attr.userver = None
        _attr.vercond = _legacy_weight_vercond

    # --- Patch 7: register bhkConvexSweepShape ----------------------------
    if not hasattr(NifFormat, 'bhkConvexSweepShape'):
        sweep_attrs = [
            _make_attr(NifFormat, {'name': 'Shape', 'type': 'Ref',
                                   'template': 'bhkShape'},
                       template=NifFormat.bhkShape),
            _make_attr(NifFormat, {'name': 'Material',
                                   'type': 'HavokMaterial'}),
            _make_attr(NifFormat, {'name': 'Radius', 'type': 'float'}),
            _make_attr(NifFormat, {'name': 'Unknown', 'type': 'Vector3'}),
        ]

        # Inherit from bhkShape (no _attrs): bhkConvexShape would inject its
        # inherited material+radius BEFORE our fields and shadow them by name,
        # scrambling the read order.
        class bhkConvexSweepShape(NifFormat.bhkShape):
            _attrs = sweep_attrs
            _is_template = False
            _is_abstract = False

        bhkConvexSweepShape.__name__ = 'bhkConvexSweepShape'
        NifFormat.bhkConvexSweepShape = bhkConvexSweepShape

    # --- Refresh flattened attribute caches -------------------------------
    _refresh_attribute_caches(NifFormat, (NifFormat.bhkWorldObject,
                                          NifFormat.HavokMaterial,
                                          NifFormat.bhkRigidBody,
                                          NifFormat.bhkMoppBvTreeShape,
                                          NifFormat.bhkNiTriStripsShape,
                                          NifFormat.NiSingleInterpController,
                                          NifFormat.NiInterpController,
                                          NifFormat.NiGeomMorpherController,
                                          NifFormat.NiPSysEmitterCtlr))


# ---------------------------------------------------------------------------
# Patch 8: Skyrim SE (BSStream / User Version 2 == 100) READ support
# ---------------------------------------------------------------------------
# The SSE "Skyrim - Meshes*.bsa" archives ship optimized meshes whose geometry
# lives in BSTriShape blocks (and, for skinned shapes, in the SSE-layout
# NiSkinPartition shared vertex buffer).  pyffi 2.2.3 predates SSE entirely.
# This patch registers read-only support so vanilla SSE meshes (body/hands/
# feet, book reading rigs, ...) can be loaded when no LE reference tree is
# available.  Layouts verified against references/nif 0.10.0.0.xml and a
# byte-walk of vanilla malebody_0.nif (SSE Meshes0.bsa).
#
# We deliberately support READ only: converted output is always written as
# LE-format (User Version 2 = 83), which SSE loads natively.  Use
# asset_convert.sse_nif.sse_to_le() to rebuild BSTriShape graphs into
# NiTriShape graphs before writing.
#
# Decoded geometry is exposed on the block instances as numpy arrays:
#   sse_verts (N,3) f32   sse_uvs (N,2) f32       sse_normals (N,3) f32
#   sse_tangents (N,3)    sse_bitangents (N,3)    sse_colors (N,4) u8
#   sse_bone_weights (N,4) f32   sse_bone_indices (N,4) u8
#   sse_triangles (M,3) u16  (BSTriShape only; skinned shapes keep geometry
#                             in their NiSkinPartition — see sse_partitions)
# NiSkinPartition additionally gets:
#   sse_partitions: list of dicts with 'bones' (tuple of skin-instance bone
#   indices), 'vertex_map', 'weights', 'bone_indices', 'triangles' (partition-
#   local), 'triangles_copy' (global shape vertex indices).

_SSE_UV2 = 100


def _is_sse(data):
    return (data is not None
            and getattr(data, 'version', 0) == _SKYRIM_VER
            and getattr(data, 'user_version_2', 0) == _SSE_UV2)


def _decode_sse_vertex_block(raw, num_vertices, vdesc):
    """Decode an SSE packed vertex buffer into numpy arrays.

    raw: bytes of num_vertices records; vdesc: the uint64 BSVertexDesc.
    Returns dict of arrays (keys matching the sse_* attribute names above);
    absent attributes map to None.
    """
    import numpy as np
    out = {'sse_verts': None, 'sse_uvs': None, 'sse_normals': None,
           'sse_tangents': None, 'sse_bitangents': None, 'sse_colors': None,
           'sse_bone_weights': None, 'sse_bone_indices': None}
    if num_vertices == 0:
        return out
    vsize = (vdesc & 0xF) * 4
    attrs = (vdesc >> 44) & 0xFFF
    rec = np.frombuffer(raw, dtype=np.uint8).reshape(num_vertices, vsize)

    def _f32(col_off, n):
        return rec[:, col_off:col_off + 4 * n].copy().view('<f4')

    def _half(col_off, n):
        return rec[:, col_off:col_off + 2 * n].copy().view('<f2').astype(
            np.float32)

    def _nbyte(col_off, n):
        # ByteVector: [0,255] -> [-1,1]
        return rec[:, col_off:col_off + n].astype(np.float32) / 255.0 * 2.0 - 1.0

    off = 0
    bit_x = None
    if attrs & 0x1:                     # Vertex (full precision in SSE)
        out['sse_verts'] = _f32(0, 3)
        if attrs & 0x10:
            bit_x = _f32(12, 1)[:, 0]   # Bitangent X shares the W slot
    if attrs & 0x2:                     # UV (half2)
        uv_off = ((vdesc >> 8) & 0xF) * 4
        out['sse_uvs'] = _half(uv_off, 2)
    bit_y = bit_z = None
    if attrs & 0x8:                     # Normal (byte3) + Bitangent Y
        n_off = ((vdesc >> 16) & 0xF) * 4
        out['sse_normals'] = _nbyte(n_off, 3)
        bit_y = _nbyte(n_off + 3, 1)[:, 0]
    if (attrs & 0x18) == 0x18:          # Tangent (byte3) + Bitangent Z
        t_off = ((vdesc >> 20) & 0xF) * 4
        out['sse_tangents'] = _nbyte(t_off, 3)
        bit_z = _nbyte(t_off + 3, 1)[:, 0]
    if attrs & 0x20:                    # Vertex Colors (byte4)
        c_off = ((vdesc >> 24) & 0xF) * 4
        out['sse_colors'] = rec[:, c_off:c_off + 4].copy()
    if attrs & 0x40:                    # Bone Weights (half4) + Indices (byte4)
        s_off = ((vdesc >> 28) & 0xF) * 4
        out['sse_bone_weights'] = _half(s_off, 4)
        out['sse_bone_indices'] = rec[:, s_off + 8:s_off + 12].copy()
    if bit_x is not None and bit_y is not None and bit_z is not None:
        out['sse_bitangents'] = np.stack([bit_x, bit_y, bit_z], axis=1)
    return out


def _install_sse_layouts(NifFormat):
    import struct as _struct
    import numpy as np

    # --- BSTriShape / BSDynamicTriShape ----------------------------------
    # Fixed prefix (after the inherited NiAVObject fields) is declared as
    # normal pyffi attrs so the generic machinery handles the name string and
    # the skin/shader/alpha Refs (link stack + fix_links).  The variable
    # vertex/triangle/particle payload is consumed by a read override.
    if not hasattr(NifFormat, 'BSTriShape'):
        ts_attrs = [
            _make_attr(NifFormat, {'name': 'Center', 'type': 'Vector3'}),
            _make_attr(NifFormat, {'name': 'Radius', 'type': 'float'}),
            _make_attr(NifFormat, {'name': 'Skin', 'type': 'Ref',
                                   'template': 'NiObject'},
                       template=NifFormat.NiObject),
            _make_attr(NifFormat, {'name': 'Shader Property', 'type': 'Ref',
                                   'template': 'NiProperty'},
                       template=NifFormat.NiProperty),
            _make_attr(NifFormat, {'name': 'Alpha Property', 'type': 'Ref',
                                   'template': 'NiProperty'},
                       template=NifFormat.NiProperty),
            _make_attr(NifFormat, {'name': 'Vertex Desc Lo', 'type': 'uint'}),
            _make_attr(NifFormat, {'name': 'Vertex Desc Hi', 'type': 'uint'}),
            _make_attr(NifFormat, {'name': 'Num Triangles', 'type': 'ushort'}),
            _make_attr(NifFormat, {'name': 'Num Vertices', 'type': 'ushort'}),
            _make_attr(NifFormat, {'name': 'Data Size', 'type': 'uint'}),
        ]

        class BSTriShape(NifFormat.NiAVObject):
            _attrs = ts_attrs
            _is_template = False
            _is_abstract = False

            def read(self, stream, data=None):
                start = stream.tell()
                super(BSTriShape, self).read(stream, data)
                vdesc = (int(self.vertex_desc_lo)
                         | (int(self.vertex_desc_hi) << 32))
                self.sse_vertex_desc = vdesc
                nv = int(self.num_vertices)
                nt = int(self.num_triangles)
                self.sse_triangles = None
                for k, v in _decode_sse_vertex_block(b'', 0, vdesc).items():
                    setattr(self, k, v)
                if int(self.data_size) > 0:
                    vsize = (vdesc & 0xF) * 4
                    dec = _decode_sse_vertex_block(
                        stream.read(nv * vsize), nv, vdesc)
                    for k, v in dec.items():
                        setattr(self, k, v)
                    self.sse_triangles = np.frombuffer(
                        stream.read(nt * 6), dtype='<u2').reshape(nt, 3).copy()
                # SSE-only trailing particle copy of the mesh
                psize, = _struct.unpack('<I', stream.read(4))
                self.sse_particle_raw = (
                    stream.read(nv * 12 + nt * 6) if psize > 0 else b'')
                self._read_dynamic(stream, nv)
                self._sse_size = stream.tell() - start

            def _read_dynamic(self, stream, nv):
                pass

            def get_size(self, data=None):
                return getattr(self, '_sse_size',
                               super(BSTriShape, self).get_size(data=data))

            def write(self, stream, data=None):
                raise NifFormat.NifError(
                    'BSTriShape is read-only: convert SSE graphs with '
                    'asset_convert.sse_nif.sse_to_le() before writing')

        BSTriShape.__name__ = 'BSTriShape'
        NifFormat.BSTriShape = BSTriShape

        class BSDynamicTriShape(BSTriShape):
            # Dynamic (morphable) variant: positions live in a trailing
            # full-precision Vector4 array instead of the packed buffer.
            def _read_dynamic(self, stream, nv):
                dsize, = _struct.unpack('<I', stream.read(4))
                n = dsize // 16
                dyn = np.frombuffer(stream.read(dsize),
                                    dtype='<f4').reshape(n, 4)
                self.sse_verts = dyn[:, :3].copy()

        BSDynamicTriShape.__name__ = 'BSDynamicTriShape'
        NifFormat.BSDynamicTriShape = BSDynamicTriShape

    # --- NiSkinPartition: SSE layout -------------------------------------
    # NumPartitions, DataSize, VertexSize, VertexDesc(u64), shared vertex
    # buffer, then per-partition: the classic 20.2.0.7 SkinPartition struct
    # + LOD byte + GlobalVB bool + VertexDesc(u64) + TrianglesCopy (global
    # shape vertex indices).  Byte-walk verified end-to-end on malebody_0.
    _skp = NifFormat.NiSkinPartition
    _orig_skp_read = _skp.read
    _orig_skp_write = _skp.write
    _orig_skp_get_size = _skp.get_size

    def _skp_read(self, stream, data=None):
        if not _is_sse(data):
            _orig_skp_read(self, stream, data=data)
            return
        start = stream.tell()
        nparts, dsize, vsize = _struct.unpack('<III', stream.read(12))
        vdesc, = _struct.unpack('<Q', stream.read(8))
        self.sse_vertex_desc = vdesc
        nv_total = dsize // vsize if vsize else 0
        dec = _decode_sse_vertex_block(stream.read(dsize), nv_total, vdesc)
        for k, v in dec.items():
            setattr(self, k, v)
        self.sse_num_vertices = nv_total
        parts = []
        for _p in range(nparts):
            nv, ntri, nbones, nstrips, wpv = _struct.unpack(
                '<5H', stream.read(10))
            bones = _struct.unpack('<%dH' % nbones, stream.read(2 * nbones))
            part = {'bones': bones, 'num_weights_per_vertex': wpv,
                    'vertex_map': None, 'weights': None,
                    'bone_indices': None, 'triangles': None,
                    'triangles_copy': None}
            if stream.read(1)[0]:      # Has Vertex Map
                part['vertex_map'] = np.frombuffer(
                    stream.read(2 * nv), dtype='<u2').copy()
            if stream.read(1)[0]:      # Has Vertex Weights
                part['weights'] = np.frombuffer(
                    stream.read(4 * nv * wpv),
                    dtype='<f4').reshape(nv, wpv).copy()
            strip_lens = _struct.unpack('<%dH' % nstrips,
                                        stream.read(2 * nstrips))
            has_faces = stream.read(1)[0]
            if has_faces:
                if nstrips:
                    for sl in strip_lens:
                        stream.read(2 * sl)
                else:
                    part['triangles'] = np.frombuffer(
                        stream.read(6 * ntri),
                        dtype='<u2').reshape(ntri, 3).copy()
            if stream.read(1)[0]:      # Has Bone Indices
                part['bone_indices'] = np.frombuffer(
                    stream.read(nv * wpv),
                    dtype=np.uint8).reshape(nv, wpv).copy()
            stream.read(2)             # LOD Level + Global VB
            stream.read(8)             # per-partition VertexDesc
            part['triangles_copy'] = np.frombuffer(
                stream.read(6 * ntri), dtype='<u2').reshape(ntri, 3).copy()
            parts.append(part)
        self.sse_partitions = parts
        self._sse_size = stream.tell() - start

    def _skp_get_size(self, data=None):
        if hasattr(self, '_sse_size'):
            return self._sse_size
        return _orig_skp_get_size(self, data=data)

    def _skp_write(self, stream, data=None):
        if hasattr(self, '_sse_size'):
            raise NifFormat.NifError(
                'SSE-read NiSkinPartition is read-only: regenerate the '
                'partition (skin_retarget._regen_skin_partition) before '
                'writing')
        _orig_skp_write(self, stream, data=data)

    _skp.read = _skp_read
    _skp.get_size = _skp_get_size
    _skp.write = _skp_write

    _refresh_attribute_caches(NifFormat, (NifFormat.BSTriShape,))


# ---------------------------------------------------------------------------
# Patch 4 implementation: correct Skyrim NiPSysData binary layout
# ---------------------------------------------------------------------------
# PyFFI 2.2.3's NiPSysData attribute list is the WRONG (older Bethesda) field
# arrangement for Skyrim: it is missing Material CRC (4), Consistency Flags (2),
# Additional Data ref (4), Has Texture Indices (1) and Aspect Flags (2), and
# invents spurious unknown_byte_1/unknown_link/unknown_short_3/unknown_byte_4
# fields.  The net size is 66 bytes for an empty block where real Skyrim is 70,
# and the field ORDER is wrong regardless of size — so the SSE engine misreads
# every following block (BSEffectShaderMaterial buffer-overrun CTD).
#
# We cannot reorder PyFFI's cached attribute list at runtime, so we override
# NiPSysData.get_size / read / write to emit the authoritative BSStream-83
# layout (derived from nif.xml 0.10 #BS202# path, verified == 70 bytes on the
# vanilla census).  Only the num_vertices==0 (empty particle pool) case that
# our converter produces is hand-rolled; anything with real per-particle arrays
# falls back to PyFFI (Oblivion source reads still use PyFFI's Oblivion layout,
# which is separately correct because Oblivion isn't #BS202#).

_SKYRIM_VER = 0x14020007


def _install_skyrim_psysdata_serializer(NifFormat):
    import struct as _struct

    PSysData = NifFormat.NiPSysData

    def _is_skyrim(data):
        return data is not None and getattr(data, 'version', 0) == _SKYRIM_VER

    def _use_handroll(self, data):
        """Hand-roll the NiPSysData layout whenever writing a Skyrim NIF.

        Our converter only ever emits NiPSysData with an EMPTY inline particle
        pool (Skyrim generates particles at runtime from bs_max_vertices), so
        the hand-rolled 70-byte #BS202# layout is always the correct output.
        PyFFI's own NiPSysData layout is structurally wrong for Skyrim (missing
        Material CRC / Consistency Flags / Additional Data / Has Texture
        Indices / Aspect Flags), so we never defer to it for Skyrim output."""
        return _is_skyrim(data)

    def _sk_fields(self):
        """Return the ordered list of (value, struct_fmt) for the Skyrim
        BSStream-83 NiPSysData layout, num_vertices==0 (empty pool)."""
        # BS Data Flags: low 6 bits = num UV sets, bit 12 (0x1000) = has tangents.
        # Particle data has neither → 0.  PyFFI stores these as num_uv_sets +
        # extra_vectors_flags bytes; recombine defensively.
        bs_data_flags = int(getattr(self, 'num_uv_sets', 0)) & 0x3F
        c = self.center
        # BS Max Vertices: the particle-pool size.  num_vertices and
        # bs_max_vertices alias the same slot; take whichever is set, min 75.
        pool = max(int(getattr(self, 'num_vertices', 0)),
                   int(getattr(self, 'bs_max_vertices', 0)), 75)
        return [
            (0, '<i'),                                   # Group ID
            (pool, '<H'),                                # BS Max Vertices
            (int(getattr(self, 'keep_flags', 0)), '<B'), # Keep Flags
            (int(getattr(self, 'compress_flags', 0)), '<B'),  # Compress Flags
            (1, '<B'),                                   # Has Vertices (always)
            (bs_data_flags, '<H'),                       # BS Data Flags
            (0, '<I'),                                   # Material CRC
            (0, '<B'),                                   # Has Normals (particles: no)
            (float(c.x), '<f'), (float(c.y), '<f'), (float(c.z), '<f'),  # Bound center
            (float(self.radius), '<f'),                  # Bound radius
            (1, '<B'),                                   # Has Vertex Colors (810/837 vanilla)
            (0, '<H'),                                   # Consistency Flags (0=MUTABLE)
            (-1, '<i'),                                  # Additional Data (NULL ref; 837/837 vanilla = -1.
                                                         #  0 would REF BLOCK 0 = the root node!)
            (1, '<B'),                                   # Has Radii (837/837 vanilla)
            (int(getattr(self, 'num_active', 0)), '<H'), # Num Active
            (1 if getattr(self, 'has_sizes', True) else 0, '<B'),          # Has Sizes
            (0, '<B'),                                   # Has Rotations
            (1 if getattr(self, 'has_rotation_angles', True) else 0, '<B'),  # Has Rotation Angles
            (0, '<B'),                                   # Has Rotation Axes
            (0, '<B'),                                   # Has Texture Indices — MUST be 0 when
                                                         #  Num Subtexture Offsets is 0: the engine does
                                                         #  rand % count for atlas frame selection →
                                                         #  EXCEPTION_INT_DIVIDE_BY_ZERO in the emitter
                                                         #  update.  0/837 vanilla blocks pair 1 with
                                                         #  count=0 (atlas blocks have count 1..128).
            (0, '<I'),                                   # Num Subtexture Offsets
            (1.0, '<f'),                                 # Aspect Ratio
            (0, '<H'),                                   # Aspect Flags
            (0.0, '<f'),                                 # Speed to Aspect Aspect 2
            (0.0, '<f'),                                 # Speed to Aspect Speed 1
            (0.0, '<f'),                                 # Speed to Aspect Speed 2
            (0, '<B'),                                   # Has Rotation Speeds
        ]

    _orig_get_size = PSysData.get_size
    _orig_write = PSysData.write
    _orig_read = PSysData.read

    def get_size(self, data=None):
        if _use_handroll(self, data):
            return sum(_struct.calcsize(fmt) for _v, fmt in _sk_fields(self))
        return _orig_get_size(self, data=data)

    def write(self, stream, data=None):
        if _use_handroll(self, data):
            for v, fmt in _sk_fields(self):
                stream.write(_struct.pack(fmt, v))
            return
        _orig_write(self, stream, data=data)

    # Field names in _sk_fields order (parallel to the packed tuples), used by
    # the Skyrim reader to restore attribute values.
    _SK_NAMES = [
        'group_id', 'bs_max_vertices', 'keep_flags', 'compress_flags',
        'has_vertices', 'bs_data_flags', 'material_crc', 'has_normals',
        'cx', 'cy', 'cz', 'radius', 'has_vertex_colors',
        'consistency_flags', 'additional_data', 'has_radii', 'num_active',
        'has_sizes', 'has_rotations', 'has_rotation_angles', 'has_rotation_axes',
        'has_texture_indices', 'num_subtexture_offsets', 'aspect_ratio',
        'aspect_flags', 's2a_a2', 's2a_s1', 's2a_s2', 'has_rotation_speeds',
    ]

    def read(self, stream, data=None):
        """Read the authoritative Skyrim #BS202# NiPSysData layout.

        Handles the variable Subtexture Offsets Vector4 array (vanilla fire has
        real atlas offsets) so vanilla particle NIFs parse for analysis.  The
        Additional Data ref is pushed onto the link stack so PyFFI's fix_links
        pass stays consistent."""
        if not _is_skyrim(data):
            _orig_read(self, stream, data=data)
            return
        fmts = [fmt for _v, fmt in _sk_fields(self)]
        vals = {}
        for name, fmt in zip(_SK_NAMES, fmts):
            n = _struct.calcsize(fmt)
            vals[name] = _struct.unpack(fmt, stream.read(n))[0]
        # NOTE: we deliberately read ONLY the fixed 70-byte prefix (matching
        # get_size).  Vanilla files may carry a Subtexture Offsets array after
        # it; PyFFI's loader compares get_size (70) to the declared block_size
        # and seeks past the remainder, so we must NOT consume it here or the
        # next block starts 16*n bytes early.
        nsub = int(vals['num_subtexture_offsets'])
        subs = []
        # Restore the attributes PyFFI/our code reads back.
        self.num_vertices = 0
        self.bs_max_vertices = vals['bs_max_vertices']
        self.keep_flags = vals['keep_flags']
        self.compress_flags = vals['compress_flags']
        self.has_vertices = True
        self.num_uv_sets = vals['bs_data_flags'] & 0x3F
        self.center.x, self.center.y, self.center.z = vals['cx'], vals['cy'], vals['cz']
        self.radius = vals['radius']
        self.num_active = vals['num_active']
        self.has_sizes = bool(vals['has_sizes'])
        self.has_rotation_angles = bool(vals['has_rotation_angles'])
        if hasattr(self, 'has_subtexture_offset_u_vs'):
            self.has_subtexture_offset_u_vs = bool(vals['has_texture_indices'])
            self.num_subtexture_offset_u_vs = nsub
        # Additional Data ref → link stack (block index or -1).
        add = vals['additional_data']
        if hasattr(data, '_link_stack') and data._link_stack is not None:
            data._link_stack.append(add if add >= 0 else -1)
        # Stash decoded subtex for analysis tooling.
        self._sk_subtex_offsets = subs

    PSysData.get_size = get_size
    PSysData.write = write
    PSysData.read = read


# ---------------------------------------------------------------------------
# Patch 9: kill PyFFI's per-attribute debug logging (PURE SPEED, no behaviour)
# ---------------------------------------------------------------------------
#
# StructBase.read/write call self._log_struct(stream, attr) for EVERY attribute
# of EVERY struct.  _log_struct does real work before the logger ever gets to
# discard the record: a getattr for the value object, an isinstance check, a
# get_value() call, and on the else-branch a full "".format() of six operands
# including hex().  Nothing reads the output — the pipeline never enables DEBUG
# on the "pyffi.nif.data.struct" logger.
#
# Measured on the two heaviest architecture NIFs (38.5 s of conversion):
# 1,517,371 _log_struct calls costing 4.44 s cumulative (~11.5%), plus the
# str.format hits inside it.  Replacing it with a no-op is a strict subset of
# "logging is disabled", so it cannot change any output byte.
#
# Kept honest: if someone really does turn DEBUG on for that logger, the
# original is restored, so the debugging path still exists.
def _install_no_op_struct_logging():
    import logging
    from pyffi.object_models.xml.struct_ import StructBase

    if getattr(StructBase, '_tesconv_nolog', False):
        return
    if os.environ.get('TESCONV_PYFFI_NO_PERF_PATCH'):
        return                      # A/B escape hatch (tools/nif_perf.py)
    if logging.getLogger("pyffi.nif.data.struct").isEnabledFor(logging.DEBUG):
        return                      # someone wants the debug output; leave it

    def _log_struct(self, stream, attr):
        return

    StructBase._log_struct = _log_struct
    StructBase._tesconv_nolog = True


# ---------------------------------------------------------------------------
# Patch 10: DETERMINISTIC header string table  (CORRECTNESS, not speed)
# ---------------------------------------------------------------------------
#
# NifFormat.Data.write() deduplicates the header string table with
#
#     self._string_list = list(set(self._string_list))   # ensure unique elements
#
# `set` of BYTES objects, whose hash is randomised per process (PEP 456), so
# the ORDER of that list changes on every run.  Every NiStringRef stores an
# INDEX into it, so the same source NIF converts to different bytes each time:
# measured on dungeons/chargen/dobrick01.nif, 26 of 7,433 bytes differ between
# two runs, all of them in the string table and the indices pointing at it.
# Same size, same blocks, same geometry — only the ordering moves.
#
# Consequences beyond tidiness: no build is reproducible, a BSA differs from
# the last one for no reason, and — the reason this was found — every
# before/after byte-comparison of a converter optimisation reports a spurious
# mismatch, so it cannot distinguish a real regression from seed noise.
#
# Fix: dedupe preserving FIRST APPEARANCE, which is a pure function of the
# block tree.  `dict.fromkeys` is the stable-unique idiom and is O(n) like the
# set version.  This does not change WHICH strings are written, only their
# order, and PyFFI itself resolves every reference through
# `_string_list.index(...)`, so the indices follow automatically.
#
# The dedupe sits in the MIDDLE of Data.write, so it cannot be wrapped from
# outside.  It is patched by recompiling that one method from PyFFI's own
# source with the single line rewritten — no behaviour is reimplemented here,
# so the method cannot drift from the installed PyFFI version.  If the source
# ever stops matching (different PyFFI release), the patch declines to install
# rather than silently applying to something it does not understand.
_STRING_DEDUPE_SRC = "self._string_list = list(set(self._string_list))"
_STRING_DEDUPE_FIX = "self._string_list = list(dict.fromkeys(self._string_list))"


def _install_deterministic_string_table():
    import inspect
    import textwrap
    from pyffi.formats.nif import NifFormat

    if getattr(NifFormat.Data, '_tesconv_stable_strings', False):
        return False

    try:
        src = inspect.getsource(NifFormat.Data.write)
    except (OSError, TypeError):
        return False
    if src.count(_STRING_DEDUPE_SRC) != 1:
        return False                     # unrecognised PyFFI; leave it alone

    src = textwrap.dedent(src).replace(_STRING_DEDUPE_SRC, _STRING_DEDUPE_FIX)

    # Compile against the defining module's globals so every name the method
    # uses (logging, NifFormat, struct, ...) resolves exactly as before.
    mod = sys.modules[NifFormat.Data.write.__module__]
    ns: dict = {}
    exec(compile(src, mod.__file__, 'exec'), mod.__dict__, ns)
    new_write = ns.get('write')
    if new_write is None:
        return False

    NifFormat.Data.write = new_write
    NifFormat.Data._tesconv_stable_strings = True
    return True


# ---------------------------------------------------------------------------
# Patch 11: vectorised update_tangent_space  (PERFORMANCE ONLY)
# ---------------------------------------------------------------------------
#
# NiTriBasedGeom.update_tangent_space is the single hottest function left in
# mesh conversion once the logging patch is in (measured 4.34 s cumulative of
# ~11 s across 12 meshes, in only 57 calls).  It runs a per-TRIANGLE Python
# loop that allocates several NifFormat.Vector3 objects per triangle, then a
# per-VERTEX Gram-Schmidt loop, on meshes with thousands of each.
#
# The algorithm is reproduced here exactly, in numpy:
#   * vertices are merged by the SAME quantised (vertex, normal) hash, so uv
#     seams still share a tangent frame,
#   * degenerate triangles (two hashes equal) are skipped,
#   * each triangle's sdir/tdir are NORMALISED BEFORE accumulation (pyffi does
#     this; skipping it changes the weighting) and a triangle whose sdir or
#     tdir is zero-length is skipped entirely,
#   * the r_sign factor, the Gram-Schmidt order (bitangent first, then tangent
#     against both n and the new bitangent), and the fallback basis for
#     degenerate frames all match.
#
# It is used by the main mesh path (via SpellAddTangentSpace), lod_far_gen and
# spt_converter, so all three benefit.  Verify with
# `python tools/nif_perf.py --baseline ...` — byte-equality is the contract.
def _install_vectorised_tangent_space():
    try:
        import numpy as np
    except ImportError:
        return False
    from pyffi.formats.nif import NifFormat

    geom = NifFormat.NiTriBasedGeom
    if getattr(geom, '_tesconv_fast_tangents', False):
        return False
    if os.environ.get('TESCONV_PYFFI_NO_PERF_PATCH') \
            or os.environ.get('TESCONV_PYFFI_NO_FAST_TANGENTS'):
        return False

    original = geom.update_tangent_space

    def _quantise(a, factor):
        """pyffi float_to_int semantics: round-half-away-from-zero, NaN -> 0."""
        scaled = a * factor
        out = np.where(np.isfinite(scaled),
                       np.trunc(np.abs(scaled) + 0.5) * np.sign(scaled), 0.0)
        return out.astype(np.int64)

    def update_tangent_space(self, as_extra=None, vertexprecision=3,
                             normalprecision=3):
        data = self.data
        if not isinstance(data, NifFormat.NiTriBasedGeomData):
            return original(self, as_extra=as_extra,
                            vertexprecision=vertexprecision,
                            normalprecision=normalprecision)
        if not data.uv_sets or not data.has_normals or not data.num_vertices:
            return original(self, as_extra=as_extra,
                            vertexprecision=vertexprecision,
                            normalprecision=normalprecision)

        n_v = data.num_vertices
        verts = np.array([(v.x, v.y, v.z) for v in data.vertices],
                         dtype=np.float64)
        norms = np.array([(v.x, v.y, v.z) for v in data.normals],
                         dtype=np.float64)
        uvs = np.array([(v.u, v.v) for v in data.uv_sets[0]], dtype=np.float64)
        if len(verts) != n_v or len(norms) != n_v or len(uvs) != n_v:
            return original(self, as_extra=as_extra,
                            vertexprecision=vertexprecision,
                            normalprecision=normalprecision)

        tris = np.array(data.get_triangles(), dtype=np.int64)
        if tris.size == 0:
            return original(self, as_extra=as_extra,
                            vertexprecision=vertexprecision,
                            normalprecision=normalprecision)

        # Merge key: quantised vertex + normal (uv/vcol excluded, as pyffi
        # passes uvprecision=-2 / vcolprecision=-2 for this call).
        key = np.concatenate([_quantise(verts, 10 ** vertexprecision),
                              _quantise(norms, 10 ** normalprecision)], axis=1)
        _uniq, h_index = np.unique(key, axis=0, return_inverse=True)
        h_index = h_index.reshape(-1)
        n_h = int(h_index.max()) + 1 if len(h_index) else 0

        i1, i2, i3 = tris[:, 0], tris[:, 1], tris[:, 2]
        h1, h2, h3 = h_index[i1], h_index[i2], h_index[i3]
        live = (h1 != h2) & (h2 != h3) & (h3 != h1)
        if not live.any():
            return original(self, as_extra=as_extra,
                            vertexprecision=vertexprecision,
                            normalprecision=normalprecision)
        i1, i2, i3 = i1[live], i2[live], i3[live]
        h1, h2, h3 = h1[live], h2[live], h3[live]

        d_v2 = verts[i2] - verts[i1]
        d_v3 = verts[i3] - verts[i1]
        d_w2 = uvs[i2] - uvs[i1]
        d_w3 = uvs[i3] - uvs[i1]

        r = d_w2[:, 0] * d_w3[:, 1] - d_w3[:, 0] * d_w2[:, 1]
        r_sign = np.where(r >= 0, 1.0, -1.0)[:, None]

        sdir = (d_w3[:, 1:2] * d_v2 - d_w2[:, 1:2] * d_v3) * r_sign
        tdir = (d_w2[:, 0:1] * d_v3 - d_w3[:, 0:1] * d_v2) * r_sign

        with np.errstate(invalid='ignore', divide='ignore'):
            s_len = np.linalg.norm(sdir, axis=1)
            t_len = np.linalg.norm(tdir, axis=1)
        ok = (s_len > 0) & (t_len > 0) & np.isfinite(s_len) & np.isfinite(t_len)
        if not ok.any():
            return original(self, as_extra=as_extra,
                            vertexprecision=vertexprecision,
                            normalprecision=normalprecision)
        sdir = sdir[ok] / s_len[ok][:, None]
        tdir = tdir[ok] / t_len[ok][:, None]
        hh = np.stack([h1[ok], h2[ok], h3[ok]], axis=1).reshape(-1)

        bins = np.zeros((n_h, 3))
        tans = np.zeros((n_h, 3))
        np.add.at(bins, hh, np.repeat(sdir, 3, axis=0))
        np.add.at(tans, hh, np.repeat(tdir, 3, axis=0))

        # Per-vertex Gram-Schmidt against the (normalised) normal.
        nrm = norms.copy()
        with np.errstate(invalid='ignore', divide='ignore'):
            n_len = np.linalg.norm(nrm, axis=1)
        bad_n = ~np.isfinite(n_len) | (n_len == 0)
        nrm[bad_n] = (0.0, 1.0, 0.0)          # pyffi's yvec fallback
        n_len = np.where(bad_n, 1.0, n_len)
        nrm /= n_len[:, None]

        b_v = bins[h_index]
        t_v = tans[h_index]

        b_v = b_v - nrm * np.einsum('ij,ij->i', nrm, b_v)[:, None]
        with np.errstate(invalid='ignore', divide='ignore'):
            b_len = np.linalg.norm(b_v, axis=1)
        t_v = t_v - nrm * np.einsum('ij,ij->i', nrm, t_v)[:, None]

        safe_b = np.where((b_len > 0) & np.isfinite(b_len), b_len, 1.0)[:, None]
        b_unit = b_v / safe_b
        t_v = t_v - b_unit * np.einsum('ij,ij->i', b_unit, t_v)[:, None]
        with np.errstate(invalid='ignore', divide='ignore'):
            t_len2 = np.linalg.norm(t_v, axis=1)

        degenerate = (~np.isfinite(b_len) | (b_len == 0)
                      | ~np.isfinite(t_len2) | (t_len2 == 0))
        b_out = b_unit
        t_out = t_v / np.where((t_len2 > 0) & np.isfinite(t_len2),
                               t_len2, 1.0)[:, None]

        if degenerate.any():
            # pyffi: bin = x cross n (fall back to y cross n), tan = n cross bin
            idx = np.nonzero(degenerate)[0]
            n_d = nrm[idx]
            b_d = np.cross(np.array((1.0, 0.0, 0.0)), n_d)
            l_d = np.linalg.norm(b_d, axis=1)
            fallback = (l_d == 0) | ~np.isfinite(l_d)
            if fallback.any():
                b_d[fallback] = np.cross(np.array((0.0, 1.0, 0.0)),
                                         n_d[fallback])
                l_d = np.linalg.norm(b_d, axis=1)
            b_d = b_d / np.where(l_d > 0, l_d, 1.0)[:, None]
            b_out = b_out.copy()
            t_out = t_out.copy()
            b_out[idx] = b_d
            t_out[idx] = np.cross(n_d, b_d)

        b_out = np.nan_to_num(b_out, nan=0.0, posinf=0.0, neginf=0.0)
        t_out = np.nan_to_num(t_out, nan=0.0, posinf=0.0, neginf=0.0)

        # ---- write back exactly as pyffi does -------------------------------
        extra = None
        for ed in self.get_extra_datas():
            if isinstance(ed, NifFormat.NiBinaryExtraData):
                if ed.name == b'Tangent space (binormal & tangent vectors)':
                    extra = ed
                    break
        if as_extra is None:
            as_extra = bool(extra)

        t32 = t_out.astype('<f4')
        b32 = b_out.astype('<f4')
        if as_extra:
            if not extra:
                extra = NifFormat.NiBinaryExtraData()
                extra.name = b'Tangent space (binormal & tangent vectors)'
                self.add_extra_data(extra)
            extra.binary_data = t32.tobytes() + b32.tobytes()
        else:
            data.extra_vectors_flags = 16
            data.tangents.update_size()
            data.bitangents.update_size()
            for vec, row in zip(data.tangents, t_out):
                vec.x, vec.y, vec.z = float(row[0]), float(row[1]), float(row[2])
            for vec, row in zip(data.bitangents, b_out):
                vec.x, vec.y, vec.z = float(row[0]), float(row[1]), float(row[2])

    geom.update_tangent_space = update_tangent_space
    geom._tesconv_fast_tangents = True
    return True


# ---------------------------------------------------------------------------
# Patch 12: single-hop get_interchangeable_tri_shape/_strips  (PERFORMANCE)
# ---------------------------------------------------------------------------
#
# PyFFI converts a geometry block to the other container type with FOUR full
# deepcopies, routed through the common base class:
#
#     shape     = NiTriShape().deepcopy(NiTriBasedGeom().deepcopy(self))
#     shapedata = NiTriShapeData().deepcopy(NiTriBasedGeomData().deepcopy(self.data))
#
# The intermediate exists only because NiTriShapeData and NiTriStripsData are
# SIBLINGS -- deepcopy refuses unrelated classes, so a strips->shape copy has
# no legal direct form.  But the round trip through the base copies every
# vertex, normal, uv and vertex colour TWICE, and the base class is exactly
# what both hops walk: measured, hop1 and hop2 select the SAME attribute list
# (19 names for the data blocks, 29 for the shape blocks), and the triangle /
# strip fields are never among them -- set_triangles/set_strips supplies those
# immediately afterwards.  So the second copy transfers nothing the first did
# not already produce, and the intermediate object is pure overhead.
#
# Measured at 2.01 s cumulative of 6.47 s (31.1%) across a 10-mesh sample --
# the single largest item in mesh conversion, and 90,364 deepcopy calls.
#
# This patch copies the base attributes ONCE, straight from source to target,
# using deepcopy's own per-attribute semantics (recurse into StructBase,
# update_size() then recurse into Array, plain setattr otherwise).
#
# CRITICAL -- the attribute list is taken from the SOURCE, never the target.
# _get_filtered_attribute_list is condition-dependent: `has_normals`,
# `has_vertex_colors` and the uv flags gate whether `normals` / `tangents` /
# `bitangents` / `vertex_colors` appear at all, and a freshly constructed
# target has them all False.  Filtering against the target would silently drop
# every normal and vertex colour.  This mirrors what deepcopy does on hop 1
# (`isinstance(mid, src.__class__)` is False, so it filters on `mid`... which
# is why we filter on the BASE view of the source -- see _base_attr_names).
#
# Falls back to the original for anything unexpected (unrelated classes, a
# source missing an attribute the base declares).  Toggle with
# TESCONV_PYFFI_NO_SINGLE_HOP_COPY=1 to A/B.  Byte-equality is the contract:
# verify with `python tools/nif_perf.py --baseline ...`.
def _install_single_hop_interchangeable():
    from pyffi.formats.nif import NifFormat
    from pyffi.object_models.xml.struct_ import StructBase
    from pyffi.object_models.xml.array import Array, _ListWrap
    from pyffi.object_models.xml.basic import BasicBase

    geom = NifFormat.NiTriBasedGeom
    if getattr(geom, '_tesconv_single_hop_copy', False):
        return False
    if os.environ.get('TESCONV_PYFFI_NO_PERF_PATCH') \
            or os.environ.get('TESCONV_PYFFI_NO_SINGLE_HOP_COPY'):
        return False

    orig_shape = geom.get_interchangeable_tri_shape
    orig_strips = geom.get_interchangeable_tri_strips

    # Element types worth bulk-copying: flat structs whose every attribute is
    # an unconditional scalar (no nested structs, no arrays, no `cond`).  A
    # 10-mesh sample copies 22,494 Vector3 and 11,237 Color4 elements -- that
    # is the whole remaining cost of the surviving copy.
    _bulk_cache = {}

    def _bulk_fields(element_type):
        """['_x_value_', ...] if element_type is bulk-copyable, else None."""
        try:
            return _bulk_cache[element_type]
        except KeyError:
            pass
        fields = None
        if isinstance(element_type, type) and issubclass(element_type,
                                                         StructBase):
            attrs = element_type._get_attribute_list()
            names = []
            seen = set()
            ok = bool(attrs)
            for a in attrs:
                if a.name in seen:
                    continue          # pyffi lists duplicates; __init__ skips
                seen.add(a.name)
                if (a.arr1 is not None or a.arr2 is not None
                        or a.cond is not None or a.vercond is not None
                        or not isinstance(a.type_, type)
                        or not issubclass(a.type_, BasicBase)):
                    ok = False
                    break
                names.append("_%s_value_" % a.name)
            if ok:
                fields = tuple(names)
        _bulk_cache[element_type] = fields
        return fields

    def _bulk_array_copy(dst_arr, src_arr):
        """Fill an EMPTY dst_arr with copies of src_arr's elements.

        Replaces update_size() + deepcopy() for flat scalar element types.
        update_size builds each element through StructBase.__init__ -- a set(),
        an _items list and one holder object per component, walking the
        attribute list -- purely so deepcopy can overwrite every component one
        getattr/get_value/set_value at a time.  Here each element is built once
        and its holders' `_value` fields are assigned straight across.

        Elements are NEW objects, never shared with the source: the caller
        mutates the copy in place (_set_tangents, _clamp_uv_sets,
        fix_missing_triangles) while still reading the original.
        """
        fields = _bulk_fields(getattr(dst_arr, '_elementType', None))
        if fields is None:
            return False
        if len(dst_arr):                       # only ever fill a fresh array
            return False
        element_type = dst_arr._elementType
        template = dst_arr._elementTypeTemplate
        argument = dst_arr._elementTypeArgument

        def fill(dst_list, src_list):
            append = dst_list.append
            for src_elem in list.__iter__(src_list):
                elem = element_type(template=template, argument=argument)
                for f in fields:
                    getattr(elem, f)._value = getattr(src_elem, f)._value
                append(elem)

        try:
            if dst_arr._count2 is None:
                fill(dst_arr, src_arr)
            else:
                # 2-D (uv_sets): rows are _ListWrap, elements live one level
                # down.  Row COUNT comes from the source, exactly as
                # update_size would derive it from the already-copied
                # num_uv_sets / num_vertices.
                for src_row in list.__iter__(src_arr):
                    row = _ListWrap(element_type)
                    fill(row, src_row)
                    dst_arr.append(row)
        except AttributeError:
            del dst_arr[0:len(dst_arr)]
            return False
        return True

    def _transfer(dst, src, base_cls):
        """Copy base_cls's attributes from src into dst, deepcopy semantics.

        Returns False (having touched nothing) if the source does not carry
        every attribute the base declares, so the caller can fall back.
        """
        # The attribute list must come from the SOURCE instance -- its flags
        # decide which conditional attributes (normals, vertex colours, uvs)
        # actually exist.  Restricting to base_cls's names keeps the copy to
        # exactly what the two-hop path transferred.
        base_names = frozenset(a.name for a in base_cls._get_attribute_list())
        attrlist = [a for a in src._get_filtered_attribute_list()
                    if a.name in base_names]
        pending = []
        for attr in attrlist:
            try:
                srcvalue = getattr(src, attr.name)
                dstvalue = getattr(dst, attr.name)
            except AttributeError:
                return False
            pending.append((attr.name, dstvalue, srcvalue))
        # Only mutate once every attribute resolved, so a fallback is clean.
        for name, dstvalue, srcvalue in pending:
            if isinstance(dstvalue, StructBase):
                dstvalue.deepcopy(srcvalue)
            elif isinstance(dstvalue, Array):
                if not _bulk_array_copy(dstvalue, srcvalue):
                    dstvalue.update_size()
                    dstvalue.deepcopy(srcvalue)
            else:
                setattr(dst, name, srcvalue)
        return True

    def _make(self, target_cls, target_data_cls, base_cls, base_data_cls,
              setter, geometry, orig):
        data = self.data
        if data is None:
            return orig(self, geometry)
        shape = target_cls()
        if not _transfer(shape, self, base_cls):
            return orig(self, geometry)
        shapedata = target_data_cls()
        if not _transfer(shapedata, data, base_data_cls):
            return orig(self, geometry)
        setter(shapedata, geometry)
        shape.data = shapedata
        return shape

    def get_interchangeable_tri_shape(self, triangles=None):
        if triangles is None:
            if self.data is None:
                return orig_shape(self, triangles)
            triangles = self.data.get_triangles()
        return _make(self, NifFormat.NiTriShape, NifFormat.NiTriShapeData,
                     NifFormat.NiTriBasedGeom, NifFormat.NiTriBasedGeomData,
                     lambda d, g: d.set_triangles(g), triangles, orig_shape)

    def get_interchangeable_tri_strips(self, strips=None):
        if strips is None:
            if self.data is None:
                return orig_strips(self, strips)
            strips = self.data.get_strips()
        return _make(self, NifFormat.NiTriStrips, NifFormat.NiTriStripsData,
                     NifFormat.NiTriBasedGeom, NifFormat.NiTriBasedGeomData,
                     lambda d, g: d.set_strips(g), strips, orig_strips)

    geom.get_interchangeable_tri_shape = get_interchangeable_tri_shape
    geom.get_interchangeable_tri_strips = get_interchangeable_tri_strips
    geom._tesconv_single_hop_copy = True
    return True


# ---------------------------------------------------------------------------
# NOT DONE: caching _get_filtered_attribute_list.  It looks like the obvious
# next win (5.1M calls in a two-mesh conversion, re-deriving a per-class
# constant), and it is WRONG.  Memoising it per (class, version, user_version)
# — even restricted to classes where no attribute carries a `cond` — changed
# the output of 7 of 30 sample meshes.  The filtering is not instance-
# independent: `arg`/`vercond` can reference instance fields, and PyFFI mutates
# instance state *while* walking the list during read, so a later attribute's
# inclusion can depend on an earlier one's just-read value.  Verify any retry
# with `python tools/nif_perf.py --baseline ...`, which is how this was caught.
# ---------------------------------------------------------------------------


def apply_patches():
    """Import NifFormat and apply all patches.  Safe to call multiple times."""
    global _PYFFI_PATCHED
    if _PYFFI_PATCHED:
        return True
    try:
        from pyffi.formats.nif import NifFormat
        _apply_nifformat_patches(NifFormat)
        _install_no_op_struct_logging()
        _install_vectorised_tangent_space()
        _install_single_hop_interchangeable()
        # Patch 13: native Array.read/write for flat float element types
        # (Vector3/Color4/TexCoord).  Array.read is 95% of all NIF read time.
        # Lives in its own module because it owns a compiled extension; a
        # missing .pyd just leaves PyFFI's own path in place.
        try:
            from . import nif_geom_native
            nif_geom_native.install()
        except Exception:
            pass
        # Patch 14: numpy-backed Array storage (asset_convert/nif_geom_array.py).
        # Supersedes Patch 13's fill/pack for the element types it backs; the
        # native hook above still serves anything it declines.
        try:
            from . import nif_geom_array
            nif_geom_array.install()
        except Exception:
            pass
        if not _install_deterministic_string_table():
            # Loud on purpose: without it every mesh build differs from the
            # last one for no reason, and byte-comparisons become meaningless.
            print("WARNING: PyFFI string-table determinism patch did not "
                  "install; converted NIFs will vary between runs.",
                  file=sys.stderr)
        _PYFFI_PATCHED = True
        return True
    except ImportError:
        return False


# Apply automatically when this module is imported.
apply_patches()
