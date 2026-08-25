"""_transform_verts: vectorised vertex transform for the winding oracle.

_visual_tri_soup transforms every render vertex under a node into Havok units
to serve as the orientation oracle for _repair_inverted_floors.  It used to do
that one vertex at a time through PyFFI's ``Vector3.__mul__``, which recurses
into get_matrix_33()/get_translation() and allocates several Vector3 objects
per vertex -- measured 3.42 s of 18.27 s (18.7%) on a 20-mesh sample, in 24,887
calls, ALL of them from this one function.

The replacement must be BIT-EXACT, not merely close: the soup feeds a
nearest-face search with a trust radius, so a 1e-7 shift can flip a DIFFERENT
triangle and change the collision that ships.  PyFFI's Float holds a plain
Python float, so the reference arithmetic is float64 -- computing this in
float32 left only 0.47% of 52,159 sample vertices bit-exact.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asset_convert import collision as C  # noqa: E402
from pyffi.formats.nif import NifFormat  # noqa: E402


def _matrix(vals):
    m = NifFormat.Matrix44()
    for i, row in enumerate(vals, 1):
        for j, val in enumerate(row, 1):
            setattr(m, 'm_%d%d' % (i, j), val)
    return m


def _verts(triples):
    out = []
    for x, y, z in triples:
        v = NifFormat.Vector3()
        v.x, v.y, v.z = x, y, z
        out.append(v)
    return out


def _scalar_reference(vertices, m, scale):
    """The loop _transform_verts replaced, verbatim."""
    verts = []
    for v in vertices:
        w = v * m
        verts.append((w.x * scale, w.y * scale, w.z * scale))
    return verts


IDENTITY = [[1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0]]

AFFINE = [[0.7071, 0.2, 0.13, 0.0],
          [-0.3, 0.9135, 0.41, 0.0],
          [0.517, 0.63, 0.811, 0.0],
          [10.5, -20.25, 30.125, 1.0]]


@pytest.mark.parametrize('matrix', [IDENTITY, AFFINE])
@pytest.mark.parametrize('n', [1, 5, 23, 24, 25, 200])
def test_bit_exact_against_scalar_loop(matrix, n):
    """Bulk path reproduces PyFFI's own arithmetic EXACTLY, at every size.

    n straddles _VECTOR_XFORM_MIN so both the numpy and fallback branches run.
    """
    m = _matrix(matrix)
    verts = _verts([(i * 1.1, -i * 2.7 + 0.3, i * 0.37) for i in range(n)])
    scale = C._HAVOK_SCALE / 7.0

    ref = _scalar_reference(verts, m, scale)
    fast = C._transform_verts(verts, m, scale)

    assert len(fast) == len(ref)
    for got, want in zip(fast, ref):
        # == not approx: a 1e-7 drift changes which triangle gets flipped
        assert got == want


def test_numpy_and_fallback_paths_agree():
    """The env toggle selects a different branch, not a different answer."""
    m = _matrix(AFFINE)
    verts = _verts([(i * 0.9, i * -1.3, i * 2.1) for i in range(64)])
    scale = C._HAVOK_SCALE / 7.0

    saved = C._NUMPY
    try:
        C._NUMPY = None
        os.environ['TESCONV_NO_FAST_VERT_XFORM'] = '1'
        scalar = C._transform_verts(verts, m, scale)
        assert C._numpy() is None, "toggle did not force the scalar path"
    finally:
        os.environ.pop('TESCONV_NO_FAST_VERT_XFORM', None)
        C._NUMPY = saved

    vector = C._transform_verts(verts, m, scale)
    assert vector == scalar


def test_translation_is_applied():
    """m_41..m_43 must reach the result (a pure-rotation bug would hide)."""
    m = _matrix([[1.0, 0.0, 0.0, 0.0],
                 [0.0, 1.0, 0.0, 0.0],
                 [0.0, 0.0, 1.0, 0.0],
                 [5.0, 7.0, 11.0, 1.0]])
    verts = _verts([(0.0, 0.0, 0.0)] * 30)
    got = C._transform_verts(verts, m, 1.0)
    assert got[0] == (5.0, 7.0, 11.0)


def test_row_vector_convention():
    """x' uses the m_11/m_21/m_31 COLUMN; transposing it must fail here."""
    m = _matrix([[0.0, 1.0, 0.0, 0.0],
                 [0.0, 0.0, 1.0, 0.0],
                 [1.0, 0.0, 0.0, 0.0],
                 [0.0, 0.0, 0.0, 1.0]])
    verts = _verts([(1.0, 2.0, 3.0)] * 30)
    got = C._transform_verts(verts, m, 1.0)
    # x' = 1*0 + 2*0 + 3*1 = 3 ; y' = 1*1 + 2*0 + 3*0 = 1 ; z' = 2
    assert got[0] == (3.0, 1.0, 2.0)
    assert got[0] == _scalar_reference(verts, m, 1.0)[0]


def test_empty_vertex_list():
    assert C._transform_verts([], _matrix(IDENTITY), 1.0) == []
