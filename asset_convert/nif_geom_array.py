"""Patch 14: numpy-backed storage for the flat-float geometry arrays.

WHY
---
Patch 13 made reading and writing those arrays native, and only bought 1.20x on
mesh conversion, because it did not touch the thing that actually costs:
PyFFI materialises one element OBJECT per item.  Profiling after Patch 13:

    struct_.__init__     514,239 calls   2.32 s   <- unchanged by Patch 13
    Float.__init__     1,337,405 calls   0.78 s   <- unchanged by Patch 13

Measured directly, constructing 225,591 ``Vector3`` objects costs **0.80 s** all
by itself, and that is a hard floor for any design that keeps handing real
Vector3 objects to consumers.  Object-model overhead was still ~44% of mesh
conversion (6.33 s of 14.3 s) after Patches 11-13.

WHAT THIS DOES
--------------
Backs those arrays with ONE numpy array instead of N element objects.
Indexing returns a lightweight ``__slots__`` view whose ``.x``/``.y``/``.z``
read and write straight into a row, so existing consumer code keeps working:

    for v in data.vertices:      # still iterates
        v.x, v.y, v.z            # still reads
    data.vertices[i].x = 1.0     # still writes

Measured against 225,591 elements:

    construct                    pyffi 0.797 s  ->  numpy ~0 s
    iterate + read all components pyffi 0.203 s  ->  views 0.109 s  (1.86x)
    bulk (.sum())                pyffi 0.203 s  ->  ~0 s

A census of all 346 geometry-array references across 37 files found **309 (91%)
work unchanged** under this model; 32 bind an element to a name for later use
and need the view to stay valid, which it does (a view holds the backing array
and a row index, not a copy).

WHY THE PROTOCOL SURFACE IS SMALL
---------------------------------
``Vector3``/``Color4``/``TexCoord``/``Triangle`` all report
``_has_links = False`` and ``_has_strings = False``, so PyFFI's ``fix_links``,
``get_links``, ``get_strings``, ``get_refs`` and ``replace_global_node`` all
short-circuit to no-ops, and ``get_size`` is pure arithmetic on a fixed element
width.  That leaves only read/write/deepcopy/update_size/get_hash to implement.

SAFETY
------
Only element types that are exactly N unconditional same-width components are
backed this way; every other array keeps PyFFI's own storage untouched.  The
contract is byte-equality, verified with ``tools/nif_perf.py --baseline`` and
``tools/nif_determinism.py``.  Toggle with ``TESCONV_NO_GEOM_ARRAY=1``.
"""

import operator
import os

_INSTALLED = False

# element type name -> (component names, in-memory dtype, bytes on disk)
#
# 🛑 The in-memory dtype is float64, NOT float32, even though the on-disk
# format is 32-bit.  PyFFI's Float holds a plain PYTHON FLOAT (a double), so
# every intermediate the converter computes -- skin retargeting, tangent
# generation, transforms -- is a double until it is finally narrowed on write.
# Backing the array with float32 truncates each intermediate as it is stored,
# and the error compounds: it moved tangents/bitangents/vertices by ~1e-7 and
# changed the output of 5 of 60 sample meshes.  Store wide, narrow only in
# write().  (Same trap as the winding-oracle transform, see notes 3a.)
_BACKED = {
    'Vector3': (('x', 'y', 'z'), 'float64', 4),
    'Color4': (('r', 'g', 'b', 'a'), 'float64', 4),
    'TexCoord': (('u', 'v'), 'float64', 4),
    'Vector4': (('x', 'y', 'z', 'w'), 'float64', 4),
}


def _make_view_class(name, comps, np, elem_cls):
    """A __slots__ view onto one row of the backing array.

    Properties, not attributes: the value must live in the array, so that a
    write through the view is seen by the array (and by any other view of the
    same row), and so that resizing the array does not strand a copy.
    """
    # 🛑 Carry the ORIGINAL element class's _attrs and identity.
    #
    # Generic PyFFI-shaped copiers detect a compound by `hasattr(x, '_attrs')`
    # and then walk `_all_attr_names(type(x))` over the MRO.  Without these a
    # view is not recognised as a compound, so such a copier ASSIGNS THE VIEW
    # OBJECT itself into the destination instead of copying its components --
    # which aliases the clone to the source array.  That is exactly what broke
    # nif_converter._copy_block_fields: _emulate_morphs clones a shape, then
    # does `v.x += d.x` per morph target, and the += landed on the ORIGINAL
    # vertices, accumulating across targets (measured: the same 54-vertex block
    # recomputed its bounds three times with sums 389 -> 40 -> 237, and shipped
    # collapsed vertices with all-zero normals).
    ns = {'__slots__': ('_a', '_i'), '_comps': comps, '_typename': name,
          '_attrs': elem_cls._attrs, '_elem_cls': elem_cls,
          '_has_links': False, '_has_strings': False}

    def __init__(self, a, i):
        self._a = a
        self._i = i
    ns['__init__'] = __init__

    def __repr__(self):
        vals = ', '.join('%s=%r' % (c, getattr(self, c)) for c in comps)
        return '<%s %s>' % (name, vals)
    ns['__repr__'] = __repr__

    # Value equality, so `a == b` compares components rather than identity --
    # PyFFI's element objects compare by identity, but nothing in the pipeline
    # relies on that and value semantics are what callers expect from a vector.
    def __eq__(self, other):
        if other is None:
            return False
        try:
            return all(getattr(self, c) == getattr(other, c) for c in comps)
        except AttributeError:
            return NotImplemented
    ns['__eq__'] = __eq__

    def __ne__(self, other):
        r = self.__eq__(other)
        return r if r is NotImplemented else not r
    ns['__ne__'] = __ne__

    # get_size/get_hash so a view can stand in for the element in PyFFI's own
    # generic walks.
    def get_size(self, data=None):
        return len(comps) * 4
    ns['get_size'] = get_size

    def get_hash(self, data=None):
        return tuple(getattr(self, c) for c in comps)
    ns['get_hash'] = get_hash

    def get_links(self, data=None):
        return []
    ns['get_links'] = get_links
    ns['get_refs'] = get_links

    def get_strings(self, data=None):
        return []
    ns['get_strings'] = get_strings

    def fix_links(self, data=None):
        return None
    ns['fix_links'] = fix_links

    def replace_global_node(self, oldbranch, newbranch, **kwargs):
        return None
    ns['replace_global_node'] = replace_global_node

    def deepcopy(self, block):
        for c in comps:
            setattr(self, c, getattr(block, c))
        return self
    ns['deepcopy'] = deepcopy

    # 🛑 INHERIT FROM THE REAL ELEMENT CLASS.
    #
    # A view must be able to do EVERYTHING the object it replaces could, not
    # just carry its components.  PyFFI's Vector3 defines __sub__, __add__,
    # __mul__, __neg__, crossproduct, norm, normalize, as_list ... and its own
    # tangent-space code does `v_2 - v_1`.  A view without __sub__ raised
    # TypeError inside SpellAddTangentSpace, whose caller swallows exceptions
    # (`except Exception: pass`) -- so tangent generation silently stopped for
    # 42 of 51 shapes in explodingrootpod.nif and shipped zeroed tangents.
    # Nothing reported an error; only a byte-diff caught it.
    #
    # Subclassing the element type means every such method is inherited and
    # operates through our component properties, so the view is behaviourally
    # complete by construction rather than by enumeration.  The properties
    # below shadow the class's own value-holder descriptors, and __slots__
    # keeps the view at two words.
    view = type(name + 'View', (elem_cls,), ns)

    # 🛑 Install the component properties AFTER the class exists.
    #
    # StructBase's class machinery re-creates a property for every declared
    # attribute when a subclass is built, so a component property passed in the
    # namespace dict is OVERWRITTEN by PyFFI's own
    # `partial(set_basic_attribute, name='x')` -- which then looks for the
    # `_x_value_` holder we deliberately do not create, and raises
    # AttributeError on the first write.  Assigning afterwards wins.
    for k, comp in enumerate(comps):
        def getter(self, _k=k):
            return float(self._a[self._i, _k])

        def setter(self, value, _k=k):
            self._a[self._i, _k] = value
        setattr(view, comp, property(getter, setter))

    return view




def install():
    """Replace Array storage for flat-float element types.  Idempotent."""
    global _INSTALLED
    if _INSTALLED:
        return True
    if os.environ.get('TESCONV_NO_GEOM_ARRAY'):
        return False
    try:
        import numpy as np
    except ImportError:
        return False
    try:
        from pyffi.formats.nif import NifFormat
        from pyffi.object_models.xml.array import Array, _ListWrap
    except ImportError:
        return False

    from . import nif_geom_native as GN
    mod = GN.native()

    views = {}
    for name, (comps, dtype, _w) in _BACKED.items():
        cls = getattr(NifFormat, name, None)
        if cls is None:
            continue
        # Only back a type whose declared attributes are EXACTLY the components
        # we expect, in order -- anything else means the XML changed under us.
        seen, names = set(), []
        for a in cls._get_attribute_list():
            if a.name in seen:
                continue
            seen.add(a.name)
            names.append(a.name)
        if tuple(names) != tuple(comps):
            continue
        if cls._has_links or cls._has_strings:
            continue
        views[cls] = (_make_view_class(name, comps, np, cls), comps, dtype)

    if not views:
        return False

    orig_read = Array.read
    orig_write = Array.write
    orig_update = Array.update_size
    orig_deepcopy = Array.deepcopy
    orig_getitem = Array.__getitem__
    orig_iter = Array.__iter__
    orig_len = Array.__len__ if hasattr(Array, '__len__') else None
    orig_get_size = Array.get_size
    orig_get_hash = Array.get_hash
    orig_elemlist = Array._elementList

    def backing(self):
        """(view_cls, comps, dtype) if this array is numpy-backed, else None."""
        return views.get(getattr(self, '_elementType', None))

    def _alloc(self, n, ncomp, dtype):
        arr = getattr(self, '_np', None)
        if arr is None or len(arr) != n:
            new = np.zeros((n, ncomp), dtype=dtype)
            if arr is not None:
                keep = min(len(arr), n)
                if keep:
                    new[:keep] = arr[:keep]
            self._np = new
        return self._np

    # --- storage ---------------------------------------------------------
    def update_size(self):
        spec = backing(self)
        if spec is None or self._count2 is not None:
            return orig_update(self)
        view_cls, comps, dtype = spec
        n = self._len1()
        _alloc(self, n, len(comps), dtype)
        # Keep list length in sync: PyFFI and our own code both call len().
        cur = list.__len__(self)
        if n < cur:
            list.__delitem__(self, slice(n, cur))
        elif n > cur:
            for _ in range(n - cur):
                list.append(self, None)
        return None

    def __getitem__(self, index):
        spec = backing(self)
        if spec is None or self._count2 is not None:
            return orig_getitem(self, index)
        view_cls, comps, dtype = spec
        arr = getattr(self, '_np', None)
        n = list.__len__(self)
        if arr is None:
            return orig_getitem(self, index)
        # SLICES MUST STILL WORK.  The accelerated path assumed an integer
        # index, so any `vertices[:n]` raised "'<' not supported between
        # instances of 'slice' and 'int'" -- pyffi's own list semantics
        # allow slicing, and the skin-retarget tests read `data.vertices[:nv]`
        # exactly that way.  A slice returns the list of element views, which
        # is what the unaccelerated list would have returned.
        if isinstance(index, slice):
            return [view_cls(arr, i)
                    for i in range(*index.indices(min(n, len(arr))))]
        index = operator.index(index)
        if index < 0:
            index += n
        if index < 0 or index >= min(n, len(arr)):
            raise IndexError('array index out of range')
        return view_cls(arr, index)

    def __iter__(self):
        spec = backing(self)
        if spec is None or self._count2 is not None:
            return orig_iter(self)
        view_cls, comps, dtype = spec
        arr = getattr(self, '_np', None)
        n = list.__len__(self)
        if arr is None:
            return orig_iter(self)
        # LIST length wins over the backing array: PyFFI keeps a conditionally
        # absent array EMPTY even when its count field says otherwise (tangents
        # with has_normals False), and materialising rows the original would
        # not have made this converter compute tangents PyFFI leaves at zero.
        return iter([view_cls(arr, i) for i in range(min(n, len(arr)))])

    def _elementList(self, **kwargs):
        spec = backing(self)
        if spec is None or self._count2 is not None:
            return orig_elemlist(self, **kwargs)
        view_cls, comps, dtype = spec
        arr = getattr(self, '_np', None)
        n = list.__len__(self)
        if arr is None:
            return orig_elemlist(self, **kwargs)
        return iter([view_cls(arr, i) for i in range(min(n, len(arr)))])

    # --- serialisation ---------------------------------------------------
    def read(self, stream, data):
        spec = backing(self)
        if spec is None or self._count2 is not None:
            return orig_read(self, stream, data)
        view_cls, comps, dtype = spec
        self._elementTypeArgument = self.arg
        n = self._len1()
        if n > 0x10000000:
            raise ValueError('array too long (%i)' % n)
        ncomp = len(comps)
        nbytes = n * ncomp * 4
        buf = stream.read(nbytes)
        if len(buf) != nbytes:
            stream.seek(-len(buf), 1)
            return orig_read(self, stream, data)
        arr = np.frombuffer(buf, dtype=np.dtype('<f4'), count=n * ncomp)
        self._np = arr.reshape(n, ncomp).astype(dtype, copy=True)
        cur = list.__len__(self)
        if n < cur:
            list.__delitem__(self, slice(n, cur))
        elif n > cur:
            for _ in range(n - cur):
                list.append(self, None)
        return None

    def write(self, stream, data):
        spec = backing(self)
        if spec is None or self._count2 is not None:
            return orig_write(self, stream, data)
        view_cls, comps, dtype = spec
        arr = getattr(self, '_np', None)
        if arr is None:
            return orig_write(self, stream, data)
        self._elementTypeArgument = self.arg
        n = self._len1()
        if n != len(arr):
            raise ValueError(
                'array size (%i) different from to field describing number '
                'of elements (%i)' % (len(arr), n))
        stream.write(np.ascontiguousarray(arr, dtype='<f4').tobytes())
        return None

    def get_size(self, data=None):
        spec = backing(self)
        if spec is None or self._count2 is not None:
            return orig_get_size(self, data)
        view_cls, comps, dtype = spec
        arr = getattr(self, '_np', None)
        if arr is None:
            return orig_get_size(self, data)
        return min(list.__len__(self), len(arr)) * len(comps) * 4

    def get_hash(self, data=None):
        spec = backing(self)
        if spec is None or self._count2 is not None:
            return orig_get_hash(self, data)
        arr = getattr(self, '_np', None)
        if arr is None:
            return orig_get_hash(self, data)
        return tuple(tuple(row) for row in arr.tolist())

    def deepcopy(self, block):
        spec = backing(self)
        other = getattr(block, '_np', None)
        if spec is None or self._count2 is not None or other is None:
            return orig_deepcopy(self, block)
        view_cls, comps, dtype = spec
        n = min(self._len1(), len(other))
        arr = _alloc(self, self._len1(), len(comps), dtype)
        if n:
            arr[:n] = other[:n]
        cur = list.__len__(self)
        want = self._len1()
        if want < cur:
            list.__delitem__(self, slice(want, cur))
        elif want > cur:
            for _ in range(want - cur):
                list.append(self, None)
        return None

    Array.update_size = update_size
    Array.__getitem__ = __getitem__
    Array.__iter__ = __iter__
    Array._elementList = _elementList
    Array.read = read
    Array.write = write
    Array.get_size = get_size
    Array.get_hash = get_hash
    Array.deepcopy = deepcopy

    _INSTALLED = True
    return True
