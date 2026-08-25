"""Patch 12: single-hop get_interchangeable_tri_shape/_strips.

PyFFI converts a geometry block to the other container type with FOUR
deepcopies routed through the common base class; the patch does it with one
direct transfer plus a bulk element copy.  These tests pin the contract that
makes that safe: the result must equal what PyFFI's own two-hop path produces,
attribute for attribute, and the copy must be INDEPENDENT of the source (the
converter mutates the copy in place while still reading the original).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asset_convert import pyffi_monkey_patch  # noqa: F401,E402  (installs)
from pyffi.formats.nif import NifFormat  # noqa: E402


def _build(cls, datacls, nverts=6, colors=True, uvsets=1, normals=True):
    geom = cls()
    data = datacls()
    data.num_vertices = nverts
    data.has_vertices = True
    data.vertices.update_size()
    for i, v in enumerate(data.vertices):
        v.x, v.y, v.z = float(i), float(i * 2), float(i * 3)
    data.has_normals = normals
    if normals:
        data.normals.update_size()
        for i, v in enumerate(data.normals):
            v.x, v.y, v.z = 0.0, 0.0, 1.0
    data.has_vertex_colors = colors
    if colors:
        data.vertex_colors.update_size()
        for i, c in enumerate(data.vertex_colors):
            c.r, c.g, c.b, c.a = 0.1 * i, 0.2, 0.3, 1.0
    data.num_uv_sets = uvsets
    data.has_uv = bool(uvsets)
    data.uv_sets.update_size()
    for row in data.uv_sets:
        for i, t in enumerate(row):
            t.u, t.v = 0.5 * i, 0.25 * i
    data.center.x, data.center.y, data.center.z = 1.0, 2.0, 3.0
    data.radius = 9.0
    data.consistency_flags = 0x4000
    geom.data = data
    return geom


def _reference_tri_shape(self, triangles=None):
    """PyFFI's ORIGINAL two-hop implementation, verbatim."""
    shape = NifFormat.NiTriShape().deepcopy(
        NifFormat.NiTriBasedGeom().deepcopy(self))
    shapedata = NifFormat.NiTriShapeData().deepcopy(
        NifFormat.NiTriBasedGeomData().deepcopy(self.data))
    if triangles is None:
        shapedata.set_triangles(self.data.get_triangles())
    else:
        shapedata.set_triangles(triangles)
    shape.data = shapedata
    return shape


def _snapshot(shape):
    d = shape.data
    return {
        'type': type(shape).__name__,
        'datatype': type(d).__name__,
        'num_vertices': d.num_vertices,
        'vertices': [(v.x, v.y, v.z) for v in d.vertices],
        'has_normals': bool(d.has_normals),
        'normals': [(v.x, v.y, v.z) for v in d.normals],
        'has_vertex_colors': bool(d.has_vertex_colors),
        'colors': [(c.r, c.g, c.b, c.a) for c in d.vertex_colors],
        'num_uv_sets': d.num_uv_sets,
        'uv': [[(t.u, t.v) for t in row] for row in d.uv_sets],
        'center': (d.center.x, d.center.y, d.center.z),
        'radius': d.radius,
        'consistency_flags': d.consistency_flags,
        'triangles': d.get_triangles(),
    }


def test_patch_is_installed():
    assert getattr(NifFormat.NiTriBasedGeom,
                   '_tesconv_single_hop_copy', False), \
        "Patch 12 did not install; the fast path is not under test"


@pytest.mark.parametrize('colors,uvsets,normals', [
    (True, 1, True),
    (False, 1, True),     # no vertex colours
    (True, 0, True),      # no uv sets
    (False, 0, False),    # bare positions only
    (True, 2, True),      # two uv sets (2-D array path)
])
def test_matches_pyffi_two_hop(colors, uvsets, normals):
    """The single-hop result is identical to PyFFI's own four-deepcopy path."""
    strips = [[0, 1, 2, 3, 4, 5]]
    a = _build(NifFormat.NiTriStrips, NifFormat.NiTriStripsData,
               colors=colors, uvsets=uvsets, normals=normals)
    a.data.set_strips(strips)
    b = _build(NifFormat.NiTriStrips, NifFormat.NiTriStripsData,
               colors=colors, uvsets=uvsets, normals=normals)
    b.data.set_strips(strips)

    fast = _snapshot(a.get_interchangeable_tri_shape())
    ref = _snapshot(_reference_tri_shape(b))
    assert fast == ref


def test_copy_is_independent_of_source():
    """The converter mutates the copy while still reading the original."""
    geom = _build(NifFormat.NiTriStrips, NifFormat.NiTriStripsData)
    geom.data.set_strips([[0, 1, 2, 3, 4, 5]])
    shape = geom.get_interchangeable_tri_shape()

    shape.data.vertices[0].x = 999.0
    shape.data.normals[0].z = -5.0
    shape.data.uv_sets[0][0].u = 42.0
    shape.data.vertex_colors[0].r = 0.75
    shape.data.center.x = -1.0

    assert geom.data.vertices[0].x == 0.0
    assert geom.data.normals[0].z == 1.0
    assert geom.data.uv_sets[0][0].u == 0.0
    assert geom.data.vertex_colors[0].r == 0.0
    assert geom.data.center.x == 1.0


def test_explicit_triangles_argument_is_honoured():
    geom = _build(NifFormat.NiTriStrips, NifFormat.NiTriStripsData)
    geom.data.set_strips([[0, 1, 2, 3, 4, 5]])
    tris = [(0, 1, 2), (3, 4, 5)]
    shape = geom.get_interchangeable_tri_shape(triangles=tris)
    assert shape.data.get_triangles() == tris


def test_shape_to_strips_round_trip():
    """get_interchangeable_tri_strips takes the same path."""
    geom = _build(NifFormat.NiTriShape, NifFormat.NiTriShapeData)
    geom.data.set_triangles([(0, 1, 2), (2, 3, 4)])
    strips = geom.get_interchangeable_tri_strips()
    assert isinstance(strips, NifFormat.NiTriStrips)
    assert isinstance(strips.data, NifFormat.NiTriStripsData)
    assert [(v.x, v.y, v.z) for v in strips.data.vertices] == \
           [(v.x, v.y, v.z) for v in geom.data.vertices]
    # geometry survives the container change
    assert strips.data.get_triangles() == geom.data.get_triangles()


def test_env_toggle_documented():
    """The A/B escape hatch must keep working (used by tools/nif_perf.py)."""
    import inspect
    src = inspect.getsource(pyffi_monkey_patch)
    assert 'TESCONV_PYFFI_NO_SINGLE_HOP_COPY' in src
