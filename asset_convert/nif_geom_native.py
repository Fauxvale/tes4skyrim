"""Patch 13: native read/write for the NIF geometry-data blocks.

Timing ``StructBase.read`` per top-level block type across 60 Oblivion NIFs put
**NiTriStripsData at 81.7% of all read time** (267 blocks, 2.413 s) and
NiTriShapeData at a further 8.1% -- 89.8% between them, against ~1,700 blocks of
every other type sharing ~5%.  By bytes the picture matches: those two plus
NiBinaryExtraData are 92.9% of every block byte.

The cost is the object model, not the I/O.  Those 288 geometry blocks hold
124,587 vertices and PyFFI materialises each one as a ``Vector3`` -- a
``StructBase.__init__`` (building a ``set()`` and an ``_items`` list) plus three
``Float`` objects.  Counting normals/tangents/bitangents that is ~500,000
Python objects per 60 meshes, which is exactly why ``struct_.__init__``
(514,239 calls) and ``Float.__init__`` (1,337,405 calls) top the profile.

THE LAYOUT DECISION STAYS IN PYFFI
----------------------------------
A census over 300 NIFs found **16 distinct attribute sequences** for these two
block types across 15 (version, user_version, user_version_2) combinations --
fields appear and vanish with ``has_normals`` / ``has_vertex_colors`` /
``has_points`` / ``extra_vectors_flags`` and with several version gates.
Hardcoding that table in C++ would rot the first time a plugin ships a
combination nobody enumerated.

So this module never decides a layout.  It walks
``_get_filtered_attribute_list(data)`` -- the SAME call PyFFI's own read/write
uses, so conditions and version gates are evaluated by PyFFI itself -- and
lowers the result to a flat ``[(opcode, count), ...]`` plan the extension
executes.  Anything it does not recognise makes ``build_plan`` return None and
the block falls back to PyFFI unchanged: an unknown layout is a slow path,
never a wrong one.

That fallback is load-bearing for correctness in one subtle case.  PyFFI mutates
instance state *while* walking the attribute list during read (a later
attribute's presence can depend on an earlier one's just-read value -- this is
what defeated memoising ``_get_filtered_attribute_list``, see
performance_notes.md).  We therefore build the read plan INCREMENTALLY, re-
asking PyFFI for the filtered list after each count/flag field is assigned,
rather than resolving the whole layout up front.

Toggle with ``TESCONV_NO_NATIVE_GEOM=1``.  Byte-equality is the contract:
verify with ``python tools/nif_perf.py --baseline ...``.
"""

import os
import sys

_NATIVE = None
_NUMPY = None

# Opcodes -- keep in sync with native/src/nifgeom/geom.cpp.
OP_UINT8 = 1
OP_UINT16 = 2
OP_INT32 = 3
OP_UINT32 = 4
OP_FLOAT = 5
OP_VEC3 = 6
OP_VEC2 = 7
OP_VEC4 = 8
OP_U16 = 9
OP_U32 = 10
OP_SKIP = 11

_ARRAY_OPS = frozenset((OP_VEC3, OP_VEC2, OP_VEC4, OP_U16, OP_U32, OP_SKIP))

# Block types this module handles.  Deliberately short: these two are 89.8% of
# read time, and every extra type widens the correctness surface for a few
# tenths of a percent.
GEOM_BLOCKS = ('NiTriShapeData', 'NiTriStripsData')

# Element struct -> (opcode, floats-per-element).  Only flat all-scalar structs
# whose members are contiguous same-width fields can be bulk-copied.
_STRUCT_ARRAY_OPS = {
    'Vector3': OP_VEC3,
    'TexCoord': OP_VEC2,
    'Color4': OP_VEC4,
}

# Basic scalar type name -> opcode.  `bool` is resolved per-version by the
# caller (1 byte above 4.0.0.2, 4 below), so it is not in this table.
_BASIC_OPS = {
    'Int': OP_INT32,
    'UInt': OP_UINT32,
    'Ref': OP_INT32,
    'Ptr': OP_INT32,
    'UShort': OP_UINT16,
    'Short': OP_UINT16,
    'UByte': OP_UINT8,
    'Byte': OP_UINT8,
    'Float': OP_FLOAT,
}


def _numpy():
    global _NUMPY
    if _NUMPY is None:
        try:
            import numpy
        except ImportError:
            _NUMPY = False
        else:
            _NUMPY = numpy
    return _NUMPY or None


def native():
    """The compiled extension, or None if unavailable/disabled."""
    global _NATIVE
    if _NATIVE is None:
        if os.environ.get('TESCONV_NO_NATIVE_GEOM'):
            _NATIVE = False
            return None
        if _numpy() is None:
            _NATIVE = False
            return None
        try:
            import importlib.machinery
            import importlib.util
            import sysconfig
            mod = '_nifgeom_native'
            if mod in sys.modules:
                _NATIVE = sys.modules[mod]
                return _NATIVE
            dist = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'native', 'dist')
            suffix = sysconfig.get_config_var('EXT_SUFFIX') or '.pyd'
            path = os.path.join(dist, mod + suffix)
            if not os.path.exists(path):
                _NATIVE = False
                return None
            loader = importlib.machinery.ExtensionFileLoader(mod, path)
            spec = importlib.util.spec_from_loader(mod, loader, origin=path)
            m = importlib.util.module_from_spec(spec)
            sys.modules[mod] = m
            spec.loader.exec_module(m)
            _NATIVE = m
        except Exception:
            _NATIVE = False
    return _NATIVE or None


def _enum_op(basic):
    """Opcode for an enum/bitfield basic, from its declared storage width."""
    try:
        size = basic.get_size()
    except Exception:
        return None
    return {1: OP_UINT8, 2: OP_UINT16, 4: OP_UINT32}.get(size)


def _classify(value, attr, data, StructBase, Array, BasicBase):
    """(opcode, count) for one resolved attribute, or None if unsupported.

    `count` is the element count for array ops and 0 for scalars.
    """
    if isinstance(value, Array):
        et = getattr(value, '_elementType', None)
        if et is None:
            return None
        name = et.__name__
        if value._count2 is not None:
            # 2-D (uv_sets): only a single row is bulk-copyable as one run,
            # so leave the multi-row case to PyFFI.
            rows = len(value)
            if rows != 1:
                return None
            op = _STRUCT_ARRAY_OPS.get(name)
            if op is None:
                return None
            return (op, len(value[0]))
        op = _STRUCT_ARRAY_OPS.get(name)
        if op is not None:
            return (op, len(value))
        if name in ('UShort', 'Short'):
            return (OP_U16, len(value))
        if name in ('UInt', 'Int'):
            return (OP_U32, len(value))
        return None

    if isinstance(value, StructBase):
        # A nested struct of flat floats (center: Vector3) -- treat as a
        # 1-element array of that struct.
        op = _STRUCT_ARRAY_OPS.get(type(value).__name__)
        if op is not None:
            return (op, 1)
        return None

    name = type(value).__name__
    if name == 'bool':
        ver = getattr(data, 'version', -1)
        return (OP_UINT8 if ver > 0x04000002 else OP_UINT32, 0)
    op = _BASIC_OPS.get(name)
    if op is not None:
        return (op, 0)
    # Enums / bitfields (ConsistencyType, ExtraVectorsFlags) store as ints.
    if isinstance(value, BasicBase):
        op = _enum_op(value)
        if op is not None:
            return (op, 0)
    return None



# ---------------------------------------------------------------------------
# The hook: Array.read / Array.write for flat float element types.
#
# Measured, Array.read is 95% of ALL NIF read time (2.99 s of 3.17 s over 60
# meshes), and three element types are 2.32 s of that:
#
#     Vector3   1.259 s   225,591 elements
#     Color4    0.536 s    81,843 elements
#     TexCoord  0.521 s       297 elements (but 2-D, one row per uv set)
#
# PyFFI builds one element object per item and calls elem.read(), which runs a
# struct.unpack per COMPONENT.  The element objects still have to exist -- the
# whole converter manipulates them -- so this keeps construction in Python and
# replaces only the per-component unpack/store, handing the extension the flat
# list of value holders to fill in one call.
# ---------------------------------------------------------------------------

# element type name -> component count
_FLAT_FLOAT_ELEMS = {
    'Vector3': 3,
    'Color4': 4,
    'TexCoord': 2,
    'Vector4': 4,
    'Quaternion': 4,
}

_INSTALLED = False


def _holder_names(cls):
    names = _HOLDER_NAME_CACHE.get(cls)
    if names is None:
        seen = set()
        out = []
        for a in cls._get_attribute_list():
            if a.name in seen:
                continue
            seen.add(a.name)
            out.append('_%s_value_' % a.name)
        names = tuple(out)
        _HOLDER_NAME_CACHE[cls] = names
    return names


_HOLDER_NAME_CACHE = {}


def install():
    """Patch Array.read/Array.write.  Idempotent; returns True if active."""
    global _INSTALLED
    if _INSTALLED:
        return True
    mod = native()
    if mod is None:
        return False

    from pyffi.object_models.xml.array import Array, _ListWrap

    orig_read = Array.read
    orig_write = Array.write

    def _flat_spec(self):
        """(ncomp, holder_names) if this array is bulk-copyable, else None."""
        et = getattr(self, '_elementType', None)
        if et is None:
            return None
        ncomp = _FLAT_FLOAT_ELEMS.get(et.__name__)
        if ncomp is None:
            return None
        names = _holder_names(et)
        # The element must be EXACTLY ncomp unconditional float components in
        # declaration order; anything else falls back.
        if len(names) != ncomp:
            return None
        return (ncomp, names)

    def read(self, stream, data):
        spec = _flat_spec(self)
        if spec is None:
            return orig_read(self, stream, data)
        ncomp, names = spec
        self._elementTypeArgument = self.arg
        len1 = self._len1()
        if len1 > 0x10000000:
            raise ValueError('array too long (%i)' % len1)
        del self[0:self.__len__()]

        et = self._elementType
        tmpl = self._elementTypeTemplate
        argm = self._elementTypeArgument
        try:
            if self._count2 is None:
                total = len1
                rows = ((self, len1),)
            else:
                rows = []
                total = 0
                for i in range(len1):
                    len2i = self._len2(i)
                    if len2i > 0x10000000:
                        raise ValueError('array too long (%i)' % len2i)
                    row = _ListWrap(et, parent=self)
                    rows.append((row, len2i))
                    total += len2i
        except Exception:
            return orig_read(self, stream, data)

        nbytes = total * ncomp * 4
        buf = stream.read(nbytes)
        if len(buf) != nbytes:
            # Truncated: rewind and let PyFFI raise exactly as it would have.
            stream.seek(-len(buf), 1)
            del self[0:self.__len__()]
            return orig_read(self, stream, data)

        holders = []
        for row, count in rows:
            for _ in range(count):
                elem = et(template=tmpl, argument=argm, parent=row)
                for n in names:
                    holders.append(getattr(elem, n))
                row.append(elem)
        mod.fill_floats(buf, 0, holders)
        if self._count2 is not None:
            for row, _count in rows:
                self.append(row)
        return None

    def write(self, stream, data):
        spec = _flat_spec(self)
        if spec is None:
            return orig_write(self, stream, data)
        ncomp, names = spec
        self._elementTypeArgument = self.arg
        len1 = self._len1()
        if len1 != self.__len__():
            return orig_write(self, stream, data)
        if len1 > 0x10000000:
            raise ValueError('array too long (%i)' % len1)
        holders = []
        try:
            if self._count2 is None:
                for elem in list.__iter__(self):
                    for n in names:
                        holders.append(getattr(elem, n))
            else:
                for i, row in enumerate(list.__iter__(self)):
                    if len(row) != self._len2(i):
                        return orig_write(self, stream, data)
                    for elem in list.__iter__(row):
                        for n in names:
                            holders.append(getattr(elem, n))
        except AttributeError:
            return orig_write(self, stream, data)
        stream.write(mod.pack_floats(holders))
        return None

    Array.read = read
    Array.write = write
    _INSTALLED = True
    return True
