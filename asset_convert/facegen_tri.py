"""FaceGen TRI codec — read Oblivion hair morphs, write Skyrim ones.

Both games store head-part morph targets in the SAME container format,
``FRTRI003`` (FaceGen's .tri).  What differs is the CONTENT each engine
expects to find inside it:

  Oblivion  meshes\\characters\\hair\\*.tri   carries one morph, ``HairMorph``
  Skyrim    .../character assets/hair/*.tri   carries one morph, ``SkinnyMorph``

Both engines reserve exactly one "Custom" morph slot for head parts.  Verified
against the engines themselves rather than inferred:

  * Oblivion.exe holds the literal ``HairMorph`` once (file 0x6640e8) with a
    single xref -- a strcmp at 0x55c48b inside the loader that copies 12-byte
    x/y/z vertex triples.  It sits adjacent to the RTTI names
    BSFaceGenMorphDataHair / BSFaceGenMorphDataHead, and the neighbouring
    diagnostics bucket it as a "Custom Morph" (separate messages exist for
    phoneme/modifier/expression).  There is no second code path applying a
    scale on top of it.
  * SkyrimSE.exe holds a morph-name pointer table in .data (file 0x1e756c0)
    whose category names are Phoneme / Expression / Modifier / Custom.
    ``SkinnyMorph`` occupies its own slot (index 7), fenced by NULLs from the
    expression block (8-24), the modifier block (26-42) and the phoneme block
    (44-59).  It is the sole member of ``Custom``.

WHAT THE OBLIVION MORPH MEANS
    NPC_.LNAM is a float, ``wbFloat(LNAM, 'Hair length')`` in xEdit's TES4
    definitions, authored per NPC.  ``HairMorph`` is the shape at LNAM=1.0 and
    the base mesh is the shape at LNAM=0.0; the engine blends between them.
    Measured across all 57 vanilla Oblivion hair .tri files: 50 extend
    DOWNWARD (z-min drops, median -2.7, extreme -15.9 on style01), 0 extend
    upward, and the 7 that do not move are exactly the short/spiky styles
    (argonianspikes, highelfmalepeak, orcmalestubs, woodelfmalepony ...) which
    have no length to add.  It is hair LENGTH.

    Skyrim has no per-NPC hair-length float, so length is BAKED at conversion
    time (see hair_pipeline.bake_hair_variant) and the emitted Skyrim .tri
    carries the SkinnyMorph slot the engine looks for.

    NOTE: Oblivion's .egm is a DIFFERENT system -- 50 symmetric + 30
    asymmetric FaceGen PCA morphs, the same basis behind FGGS.  Skyrim ships
    no .egm at all and its sliders are direct rather than PCA, so .egm does
    not convert (the same wall npc_face_mapper documents for FGGS->NAM9).
    LNAM does NOT drive the .egm, so hair length is unaffected by that gap.

Layout (little-endian), confirmed against PyNifly's reader and by round-trip
parsing all 57 Oblivion + 113 Skyrim vanilla hair .tri files with zero
trailing bytes on every one:

    0x00  char[8]  'FRTRI003'
    0x08  u32      vertexNum
    0x0c  u32      faceNum
    0x10  u32      quadNum          (0)
    0x14  u32      labelledVertNum  (0)
    0x18  u32      labelledSurfNum  (0)
    0x1c  u32      uvNum
    0x20  u32      extensionFlags   (1 = has UVs)
    0x24  u32      morphNum
    0x28  u32      addMorphNum      (modifier morphs)
    0x2c  u32      addVertexNum
    0x30  u32[4]   reserved
    0x40  f32[3]  * vertexNum       base vertices
          u32[3]  * faceNum         face indices
          f32[2]  * uvNum           UVs           (when extensionFlags & 1)
          u32[3]  * faceNum         UV face indices
          then morphNum morphs, each:
              u32      nameLen
              char[]   name (NUL-terminated, nameLen includes the NUL)
              f32      multiplier
              i16[3] * vertexNum    quantized deltas; delta = raw * multiplier
"""

import struct

MAGIC = b'FRTRI003'
HEADER_SIZE = 0x40

# The one custom-morph slot each engine reserves for head parts.
OBLIVION_HAIR_MORPH = 'HairMorph'
SKYRIM_HAIR_MORPH = 'SkinnyMorph'

# i16 quantization headroom.  Deltas are stored as raw*multiplier, so the
# multiplier must be large enough that max|delta| fits in a signed short.
_I16_MAX = 32767


class TriError(ValueError):
    """Malformed or unsupported .tri file."""


class TriFile:
    """A parsed FaceGen .tri.

    Attributes:
        vertices  list[(x, y, z)]      base mesh vertices
        faces     list[(a, b, c)]      triangle indices
        uvs       list[(u, v)]         texture coordinates (may be empty)
        uv_faces  list[(a, b, c)]      per-face UV indices (may be empty)
        morphs    dict[str, list]      name -> per-vertex (dx, dy, dz) deltas
    """

    __slots__ = ('vertices', 'faces', 'uvs', 'uv_faces', 'morphs')

    def __init__(self, vertices=None, faces=None, uvs=None, uv_faces=None,
                 morphs=None):
        self.vertices = vertices if vertices is not None else []
        self.faces = faces if faces is not None else []
        self.uvs = uvs if uvs is not None else []
        self.uv_faces = uv_faces if uv_faces is not None else []
        self.morphs = morphs if morphs is not None else {}

    # -- reading ----------------------------------------------------------

    @classmethod
    def from_bytes(cls, blob: bytes) -> 'TriFile':
        if len(blob) < HEADER_SIZE or blob[:8] != MAGIC:
            raise TriError('not a %s file' % MAGIC.decode())

        (nv, nf, _nquad, _nlv, _nls, nuv,
         ext, nmorph, naddmorph, _naddvert) = struct.unpack_from('<10I', blob, 8)

        if naddmorph:
            # Modifier morphs use a sparse per-vertex-index encoding.  No
            # vanilla hair .tri in either game uses them (verified across all
            # 170 files), so rather than half-support them, refuse.
            raise TriError('modifier morphs are not supported')

        off = HEADER_SIZE

        need = nv * 12
        if len(blob) < off + need:
            raise TriError('truncated vertex block')
        verts = list(_iter_tuples(blob, off, nv, '<3f', 12))
        off += need

        need = nf * 12
        if len(blob) < off + need:
            raise TriError('truncated face block')
        faces = list(_iter_tuples(blob, off, nf, '<3I', 12))
        off += need

        uvs, uv_faces = [], []
        if ext & 1:
            need = nuv * 8
            if len(blob) < off + need:
                raise TriError('truncated UV block')
            uvs = list(_iter_tuples(blob, off, nuv, '<2f', 8))
            off += need

            need = nf * 12
            if len(blob) < off + need:
                raise TriError('truncated UV face block')
            uv_faces = list(_iter_tuples(blob, off, nf, '<3I', 12))
            off += need

        morphs = {}
        for _ in range(nmorph):
            if len(blob) < off + 4:
                raise TriError('truncated morph name length')
            (name_len,) = struct.unpack_from('<I', blob, off)
            off += 4
            if len(blob) < off + name_len:
                raise TriError('truncated morph name')
            name = blob[off:off + name_len].split(b'\0')[0].decode(
                'iso-8859-15', 'replace')
            off += name_len

            if len(blob) < off + 4:
                raise TriError('truncated morph multiplier')
            (mult,) = struct.unpack_from('<f', blob, off)
            off += 4

            need = nv * 6
            if len(blob) < off + need:
                raise TriError('truncated morph data for %r' % name)
            deltas = [(x * mult, y * mult, z * mult)
                      for x, y, z in _iter_tuples(blob, off, nv, '<3h', 6)]
            off += need
            morphs[name] = deltas

        return cls(verts, faces, uvs, uv_faces, morphs)

    @classmethod
    def from_file(cls, path) -> 'TriFile':
        with open(path, 'rb') as fh:
            return cls.from_bytes(fh.read())

    # -- writing ----------------------------------------------------------

    def to_bytes(self) -> bytes:
        nv = len(self.vertices)
        nf = len(self.faces)
        has_uv = bool(self.uvs)

        # UV faces are mandatory alongside UVs; mirror the geometry faces when
        # a caller supplies UVs without their own index array.
        uv_faces = self.uv_faces if self.uv_faces else (
            self.faces if has_uv else [])

        out = bytearray()
        out += MAGIC
        out += struct.pack('<10I',
                           nv, nf, 0, 0, 0,
                           len(self.uvs),
                           1 if has_uv else 0,
                           len(self.morphs),
                           0, 0)
        out += b'\0' * 16  # reserved

        for x, y, z in self.vertices:
            out += struct.pack('<3f', x, y, z)
        for a, b, c in self.faces:
            out += struct.pack('<3I', a, b, c)
        if has_uv:
            for u, v in self.uvs:
                out += struct.pack('<2f', u, v)
            for a, b, c in uv_faces:
                out += struct.pack('<3I', a, b, c)

        for name, deltas in self.morphs.items():
            if len(deltas) != nv:
                raise TriError('morph %r has %d deltas, expected %d'
                               % (name, len(deltas), nv))
            raw = name.encode('iso-8859-15') + b'\0'
            # FaceGen pads the name field to a 4-byte boundary.
            if len(raw) % 4:
                raw += b'\0' * (4 - len(raw) % 4)
            out += struct.pack('<I', len(raw))
            out += raw

            peak = 0.0
            for d in deltas:
                for c in d:
                    a = abs(c)
                    if a > peak:
                        peak = a
            # A zero morph still needs a finite multiplier so the slot stays
            # editable; the engine never divides by it.
            mult = (peak / _I16_MAX) if peak > 0 else 1e-4
            out += struct.pack('<f', mult)

            inv = 1.0 / mult
            for dx, dy, dz in deltas:
                out += struct.pack('<3h',
                                   _clamp_i16(dx * inv),
                                   _clamp_i16(dy * inv),
                                   _clamp_i16(dz * inv))

        return bytes(out)

    def write(self, path):
        with open(path, 'wb') as fh:
            fh.write(self.to_bytes())

    # -- morph helpers ----------------------------------------------------

    def hair_morph(self):
        """The Oblivion hair-length morph deltas, or None.

        Vanilla capitalizes the name inconsistently across the 57 files
        ('HairMorph' x47, 'Hairmorph' x7, 'hairmorph' x3), so match
        case-insensitively rather than on the exact spelling.
        """
        want = OBLIVION_HAIR_MORPH.lower()
        for name, deltas in self.morphs.items():
            if name.lower() == want:
                return deltas
        return None

    def morphed_vertices(self, deltas, weight: float):
        """Base vertices blended `weight` of the way along `deltas`."""
        if not deltas or weight == 0.0:
            return list(self.vertices)
        return [(v[0] + d[0] * weight,
                 v[1] + d[1] * weight,
                 v[2] + d[2] * weight)
                for v, d in zip(self.vertices, deltas)]


def _iter_tuples(blob, off, count, fmt, stride):
    unpack = struct.Struct(fmt).unpack_from
    for i in range(count):
        yield unpack(blob, off + i * stride)


def _clamp_i16(value: float) -> int:
    i = int(round(value))
    if i > _I16_MAX:
        return _I16_MAX
    if i < -_I16_MAX - 1:
        return -_I16_MAX - 1
    return i


def build_skyrim_hair_tri(vertices, faces, uvs=None, uv_faces=None) -> bytes:
    """Serialise a Skyrim head-part .tri for already-baked hair geometry.

    Length is baked into `vertices`, so the SkinnyMorph slot the engine reads
    is emitted with zero deltas -- present and well-formed, deforming nothing.
    That mirrors vanilla, where SkinnyMorph is a mild build/weight tweak: it
    moves only 9-36% of vertices with mean |delta| 0.017-0.33, versus
    Oblivion's HairMorph at 82-100% of vertices and mean 0.38-2.06.
    """
    tri = TriFile(list(vertices), list(faces),
                  list(uvs or []), list(uv_faces or []))
    tri.morphs[SKYRIM_HAIR_MORPH] = [(0.0, 0.0, 0.0)] * len(tri.vertices)
    return tri.to_bytes()
