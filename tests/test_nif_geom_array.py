"""Patch 14: numpy-backed storage for flat-float geometry arrays.

Each test here pins a bug that actually shipped a wrong mesh during
development.  They are cheap; the byte-equality runs that found them are not.
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asset_convert import pyffi_monkey_patch  # noqa: F401,E402  (installs)
from asset_convert import nif_geom_array as GA  # noqa: E402
from pyffi.formats.nif import NifFormat  # noqa: E402

pytestmark = pytest.mark.skipif(not GA._INSTALLED,
                                reason='numpy-backed arrays not installed')


def _data(version=0x14000004, uv=11, uv2=11):
    d = NifFormat.Data()
    d.version, d.user_version, d.user_version_2 = version, uv, uv2
    return d


def _shape(n=4, normals=True):
    b = NifFormat.NiTriShapeData()
    b.num_vertices = n
    b.has_vertices = True
    b.vertices.update_size()
    for i, v in enumerate(b.vertices):
        v.x, v.y, v.z = float(i + 1), float(i + 2), float(i + 3)
    if normals:
        b.has_normals = True
        b.normals.update_size()
        for v in b.normals:
            v.x, v.y, v.z = 0.0, 0.0, 1.0
    return b


def test_backing_is_float64_not_float32():
    """float32 storage truncates every intermediate the converter computes.

    PyFFI's Float holds a Python float (a double) and only the ON-DISK format
    is 32-bit.  Backing with float32 broke 5 of 60 sample meshes: skin
    retargeting and tangent generation write computed doubles back through
    these arrays, and each store rounded.
    """
    for _name, (_comps, dtype, _w) in GA._BACKED.items():
        assert dtype == 'float64', 'geometry backing must be float64'
    b = _shape()
    assert b.vertices._np.dtype.name == 'float64'


def test_view_is_a_real_element_subclass():
    """A view must do EVERYTHING the element it replaces could.

    PyFFI's own update_tangent_space does `v_2 - v_1`.  A view without
    __sub__ raised TypeError inside SpellAddTangentSpace, whose caller
    swallows exceptions -- tangent generation silently stopped for 42 of 51
    shapes in one mesh and shipped zeroed tangents with no error at all.
    """
    b = _shape()
    v = b.vertices[0]
    assert isinstance(v, NifFormat.Vector3)
    for op in ('__sub__', '__add__', '__mul__', '__neg__',
               'crossproduct', 'norm', 'normalize', 'as_list'):
        assert hasattr(v, op), 'view lost %s' % op


def test_view_arithmetic_matches_real_vector3():
    b = _shape()
    a, c = b.vertices[0], b.vertices[1]
    ra, rc = NifFormat.Vector3(), NifFormat.Vector3()
    ra.x, ra.y, ra.z = a.x, a.y, a.z
    rc.x, rc.y, rc.z = c.x, c.y, c.z

    assert (c - a).as_list() == (rc - ra).as_list()
    assert (a + c).as_list() == (ra + rc).as_list()
    assert a * c == ra * rc                       # dot product
    assert (-a).as_list() == (-ra).as_list()
    assert a.norm() == ra.norm()
    assert a.crossproduct(c).as_list() == ra.crossproduct(rc).as_list()


def test_component_properties_win_over_pyffi_descriptors():
    """StructBase re-creates a property per declared attribute on subclassing.

    Passing the component property in the namespace dict is NOT enough -- it
    gets overwritten by PyFFI's partial(set_basic_attribute), which then looks
    for the `_x_value_` holder we deliberately never create and raises
    AttributeError on the first write.  They must be installed after the class
    exists.
    """
    b = _shape()
    v = b.vertices[0]
    v.x = 42.5
    assert v.x == 42.5
    assert b.vertices._np[0, 0] == 42.5


def test_writes_are_visible_through_the_array_and_other_views():
    b = _shape()
    b.vertices[2].y = -7.25
    assert b.vertices[2].y == -7.25
    assert [v.y for v in b.vertices][2] == -7.25


def test_view_stays_valid_after_binding():
    """32 of 346 call sites bind an element and use it later."""
    b = _shape()
    held = b.vertices[1]
    b.vertices[1].x = 100.0
    assert held.x == 100.0, 'view must alias the row, not copy it'


def test_conditionally_absent_array_stays_empty():
    """tangents must not materialise merely because num_vertices is set."""
    b = _shape()
    assert len(b.tangents) == 0
    assert len(b.bitangents) == 0


def test_round_trip_write_read_is_exact():
    d = _data()
    b = _shape(n=32)
    buf = io.BytesIO()
    b.vertices.write(buf, d)
    assert len(buf.getvalue()) == 32 * 12

    buf.seek(0)
    other = _shape(n=32)
    other.vertices.read(buf, d)
    import struct as _s
    want = [_s.unpack('<3f', _s.pack('<3f', float(i + 1), float(i + 2),
                                     float(i + 3))) for i in range(32)]
    got = [(v.x, v.y, v.z) for v in other.vertices]
    assert got == [tuple(w) for w in want]


def test_get_size_matches_pyffi():
    d = _data()
    b = _shape(n=9)
    assert b.vertices.get_size(d) == 9 * 12
    assert b.normals.get_size(d) == 9 * 12
    assert b.tangents.get_size(d) == 0


def test_clone_does_not_alias_the_source():
    """_copy_block_fields clones a shape; _emulate_morphs then does v.x += d.x.

    If the clone aliases the source array those += land on the ORIGINAL
    vertices and accumulate across morph targets.
    """
    from asset_convert import nif_converter as nc
    src = _shape(n=5)
    dst = src.__class__()
    nc._copy_block_fields(src, dst)
    assert [(v.x, v.y, v.z) for v in dst.vertices] == \
           [(v.x, v.y, v.z) for v in src.vertices]
    dst.vertices[0].x = 999.0
    assert src.vertices[0].x != 999.0, 'clone aliases the source array'


def test_non_backed_arrays_are_untouched():
    """Only flat-float element types are backed; the rest keep PyFFI storage."""
    node = NifFormat.NiNode()
    child = NifFormat.NiTriShape()
    node.add_child(child)
    assert len(node.children) == 1
    assert node.children[0] is child
    assert getattr(node.children, '_np', None) is None
