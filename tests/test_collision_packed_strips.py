"""Converted collision must never ship bhkPackedNiTriStripsShape.

Skyrim does not support that shape: 0 of 17,216 vanilla Skyrim meshes contain
`bhkPackedNiTriStripsShape` or `hkPackedNiTriStripsData`.  When one reaches the
engine, the model loader mis-sizes its sub-part allocation and then memcpys the
payload with a garbage 32-bit length.  Measured live from a dump taken at the
fault: a 2.03 GB `memcpy`, a 2.25 GB tbbmalloc source block filled with
`01 00 00 00 01 00 00 00 ...`, a 36 GB block in the same allocator list, and
49.2 GB committed against a normal ~8 GB.  The resulting 0x0000000100000001
then surfaces as an access violation in whatever allocates next -- which is why
a single bad mesh produced crash logs blaming the shadow renderer, the audio
manager, a ScrapHeap, and tbbmalloc's own getTLS on different runs.

The regression that caused it: `_convert_shape` converted a
`bhkNiTriStripsShape` nested in a `bhkListShape` to a packed shape and returned
it DIRECTLY, never reaching the `bhkPackedNiTriStripsShape` branch below that
rebuilds it as MOPP + `bhkCompressedMeshShape`.

Confirmed in-game 2026-08-22 (anequina\\architecture\\huts\\domehut01.nif).
"""


import pytest

# The monkey patch must land BEFORE pyffi.formats.nif is imported: pyffi 2.2.3
# calls time.clock(), which no longer exists.
import asset_convert.pyffi_monkey_patch  # noqa: F401

NifFormat = pytest.importorskip('pyffi.formats.nif').NifFormat

from asset_convert import collision  # noqa: E402


def _tri_strips_shape(tris, material=0):
    """A minimal Oblivion bhkNiTriStripsShape covering `tris`."""
    verts = []
    index = {}
    faces = []
    for t in tris:
        row = []
        for v in t:
            key = tuple(round(c, 6) for c in v)
            if key not in index:
                index[key] = len(verts)
                verts.append(key)
            row.append(index[key])
        faces.append(row)

    # One 3-point strip per triangle.  `points` sizes itself from
    # strip_lengths once has_points is set -- it has no update_size() of its
    # own, which is why the rows must not be sized by hand.
    data = NifFormat.NiTriStripsData()
    data.num_vertices = len(verts)
    data.has_vertices = True
    data.vertices.update_size()
    for i, (x, y, z) in enumerate(verts):
        data.vertices[i].x = x
        data.vertices[i].y = y
        data.vertices[i].z = z
    data.num_strips = len(faces)
    data.strip_lengths.update_size()
    for i in range(len(faces)):
        data.strip_lengths[i] = 3
    data.has_points = True
    data.points.update_size()
    for i, row in enumerate(faces):
        for j in range(3):
            data.points[i][j] = row[j]

    shape = NifFormat.bhkNiTriStripsShape()
    shape.num_strips_data = 1
    shape.strips_data.update_size()
    shape.strips_data[0] = data
    shape.num_data_layers = 1
    shape.data_layers.update_size()
    try:
        collision._set_havok_material(shape.material, material)
    except Exception:
        pass
    shape.scale.x = shape.scale.y = shape.scale.z = 1.0
    return shape


def _box_tris(size=40.0):
    """Triangles for an axis-aligned box, in Oblivion units."""
    s = size
    c = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
         (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    quads = [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4),
             (2, 3, 7, 6), (1, 2, 6, 5), (0, 3, 7, 4)]
    tris = []
    for a, b, cc, d in quads:
        tris.append((c[a], c[b], c[cc]))
        tris.append((c[a], c[cc], c[d]))
    return tris


def _walk(shape, seen=None):
    """Yield every shape reachable from `shape`."""
    if seen is None:
        seen = set()
    if shape is None or id(shape) in seen:
        return
    seen.add(id(shape))
    yield shape
    for attr in ('shape',):
        yield from _walk(getattr(shape, attr, None), seen)
    for sub in getattr(shape, 'sub_shapes', []) or []:
        yield from _walk(sub, seen)


def test_nested_strips_are_rebuilt_as_mopp_cms():
    """A bhkNiTriStripsShape inside a bhkListShape must come out MOPP+CMS.

    This is the exact shape that crashed: returning the packed shape from the
    bhkNiTriStripsShape branch skipped the rebuild entirely.
    """
    inner = _tri_strips_shape(_box_tris())
    lst = NifFormat.bhkListShape()
    lst.num_sub_shapes = 1
    lst.sub_shapes.update_size()
    lst.sub_shapes[0] = inner

    out = collision._convert_shape(lst, None)

    kinds = {type(s).__name__ for s in _walk(out)}
    assert 'bhkPackedNiTriStripsShape' not in kinds, kinds
    assert 'bhkMoppBvTreeShape' in kinds, kinds
    assert 'bhkCompressedMeshShape' in kinds, kinds


def test_nested_strips_keep_their_triangles():
    """The rebuild must not silently lose collision geometry."""
    tris = _box_tris()
    inner = _tri_strips_shape(tris)
    lst = NifFormat.bhkListShape()
    lst.num_sub_shapes = 1
    lst.sub_shapes.update_size()
    lst.sub_shapes[0] = inner

    out = collision._convert_shape(lst, None)

    cms_data = [s.data for s in _walk(out)
                if type(s).__name__ == 'bhkCompressedMeshShape'
                and getattr(s, 'data', None) is not None]
    assert cms_data, 'no bhkCompressedMeshShapeData produced'
    d = cms_data[0]
    indices = sum(int(ch.num_indices) for ch in d.chunks)
    indices += int(d.num_big_tris) * 3
    assert indices >= len(tris) * 3 * 0.9, (indices, len(tris) * 3)


def test_standalone_strips_are_rebuilt_too():
    """The un-nested case must not regress either."""
    shape = _tri_strips_shape(_box_tris())

    out = collision._convert_shape(shape, None)

    kinds = {type(s).__name__ for s in _walk(out)}
    assert 'bhkPackedNiTriStripsShape' not in kinds, kinds
