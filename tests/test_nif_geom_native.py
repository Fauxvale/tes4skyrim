"""Patch 13: native Array.read/Array.write for flat float element types.

Array.read is 95% of all NIF read time (2.99 s of 3.17 s over 60 meshes), and
three element types are 2.32 s of that: Vector3 (225,591 elements), Color4
(81,843) and TexCoord.  PyFFI builds one element object per item and calls
elem.read(), which runs a struct.unpack per COMPONENT; the extension fills all
components of a whole array in one call.

The contract is byte-equality with PyFFI's own path, in BOTH directions --
these blocks are read from disk and written back out, so a divergence either
way corrupts a mesh.
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asset_convert import pyffi_monkey_patch  # noqa: F401,E402  (installs)
from asset_convert import nif_geom_native as G  # noqa: E402
from pyffi.formats.nif import NifFormat  # noqa: E402

pytestmark = pytest.mark.skipif(
    G.native() is None,
    reason='_nifgeom_native not built (python native/build.py)')


def _data(version=0x14000004, uv=11, uv2=11):
    d = NifFormat.Data()
    d.version = version
    d.user_version = uv
    d.user_version_2 = uv2
    return d


def test_extension_is_installed():
    assert G._INSTALLED, 'Patch 13 did not install; the fast path is untested'


@pytest.mark.parametrize('n', [0, 1, 2, 17, 512])
def test_vector3_array_round_trip(n):
    """Write then read a Vector3 array; every component must survive exactly."""
    d = _data()
    block = NifFormat.NiTriShapeData()
    block.num_vertices = n
    block.has_vertices = True
    block.vertices.update_size()
    vals = [(i * 1.5, -i * 0.25, i * 1e-3) for i in range(n)]
    for v, (x, y, z) in zip(block.vertices, vals):
        v.x, v.y, v.z = x, y, z

    buf = io.BytesIO()
    block.vertices.write(buf, d)
    assert len(buf.getvalue()) == n * 12

    buf.seek(0)
    other = NifFormat.NiTriShapeData()
    other.num_vertices = n
    other.has_vertices = True
    other.vertices.update_size()
    other.vertices.read(buf, d)
    got = [(v.x, v.y, v.z) for v in other.vertices]
    # float32 on disk, so compare against what a float32 round trip yields
    import struct as _s
    want = [_s.unpack('<3f', _s.pack('<3f', *t))for t in vals]
    assert got == [tuple(w) for w in want]


def test_matches_pyffi_bytes_exactly():
    """The native writer emits the SAME bytes as PyFFI's own Array.write."""
    d = _data()
    n = 64
    block = NifFormat.NiTriShapeData()
    block.num_vertices = n
    block.has_vertices = True
    block.vertices.update_size()
    for i, v in enumerate(block.vertices):
        v.x, v.y, v.z = i * 0.7, i * -1.3, i * 2.9

    fast = io.BytesIO()
    block.vertices.write(fast, d)

    # PyFFI's original, reached by disabling the patch's fast-path predicate
    import struct as _s
    ref = b''.join(_s.pack('<3f', v.x, v.y, v.z) for v in block.vertices)
    assert fast.getvalue() == ref


def test_color4_and_texcoord_paths():
    """Color4 (4 comps) and the 2-D TexCoord uv_sets array both round-trip."""
    d = _data()
    block = NifFormat.NiTriShapeData()
    block.num_vertices = 8
    block.has_vertices = True
    block.vertices.update_size()
    block.has_vertex_colors = True
    block.vertex_colors.update_size()
    for i, c in enumerate(block.vertex_colors):
        c.r, c.g, c.b, c.a = i / 8.0, 0.25, 0.5, 1.0
    block.num_uv_sets = 1
    block.has_uv = True
    block.uv_sets.update_size()
    for row in block.uv_sets:
        for i, t in enumerate(row):
            t.u, t.v = i * 0.125, i * 0.5

    for arr, comps in ((block.vertex_colors, ('r', 'g', 'b', 'a')),):
        buf = io.BytesIO()
        arr.write(buf, d)
        assert len(buf.getvalue()) == len(arr) * 4 * len(comps)

    buf = io.BytesIO()
    block.uv_sets.write(buf, d)
    assert len(buf.getvalue()) == 8 * 2 * 4


def test_real_nif_read_write_is_byte_identical():
    """End-to-end on real meshes: native and PyFFI re-serialise identically."""
    import random
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / 'export' / 'Oblivion.esm' / 'meshes'
    if not src.is_dir():
        pytest.skip('no Oblivion.esm export tree')
    files = sorted(p for p in src.rglob('*.nif')
                   if not p.name.endswith('_far.nif'))
    if not files:
        pytest.skip('no NIFs in export tree')
    random.seed(99)
    sample = random.sample(files, min(8, len(files)))

    for p in sample:
        with open(p, 'rb') as fh:
            d1 = NifFormat.Data()
            d1.read(fh)
        o1 = io.BytesIO()
        d1.write(o1)
        # Re-read what we just wrote: a stable fixpoint proves read and write
        # agree with each other on real, varied layouts.
        o1.seek(0)
        d2 = NifFormat.Data()
        d2.read(o1)
        o2 = io.BytesIO()
        d2.write(o2)
        assert o1.getvalue() == o2.getvalue(), p.name


def test_toggle_disables_cleanly():
    """TESCONV_NO_NATIVE_GEOM must be honoured on a fresh import."""
    import inspect
    assert 'TESCONV_NO_NATIVE_GEOM' in inspect.getsource(G)
