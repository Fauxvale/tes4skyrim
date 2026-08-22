"""A shape that declares vertices and ships none must not reach the output.

`LeyawiinLowerDoor01` in `leyawiinhouselower01.nif` is the measured case:
`num_vertices = 16` with `has_vertices = False`, while normals, colors, UVs
and 6 triangles all still index 16 vertices that are not in the file.

Oblivion tolerates it — there is nothing to draw, so it draws nothing. LODGen
does not: `RemoveUnseenFaces` indexes straight into the empty list, throws
`ArgumentOutOfRangeException`, and the whole run ends with exit 548 and NO
`.bto` tiles. One shape like this cost NehrimWorldspace every bit of its
object LOD while the other 17 worldspaces baked fine.

Measured scope: 1 of Nehrim's 1552 shipped `_far.nif`.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def nif():
    if not hasattr(time, 'clock'):
        time.clock = time.perf_counter
    from asset_convert import pyffi_monkey_patch            # noqa: F401
    from pyffi.formats.nif import NifFormat
    return NifFormat


def _shape_with_no_vertex_array(NifFormat):
    """Rebuild the authored defect exactly as it comes off disk.

    Read back from the real file, `LeyawiinLowerDoor01` is
    ``num_vertices=16, has_vertices=False, len(vertices)=0`` with 6 triangles
    whose highest index is 15. `update_size()` alone will not produce that —
    it sizes the array from `num_vertices` regardless of the flag — so the
    list is sized while the count is still 0 and the count set afterwards.
    """
    shape = NifFormat.NiTriShape()
    data = NifFormat.NiTriShapeData()
    shape.data = data

    data.has_vertices = False              # <- the defect
    data.num_vertices = 0
    data.vertices.update_size()            # -> genuinely empty, as on read
    data.num_vertices = 16

    data.has_normals = True
    data.normals.update_size()
    data.has_vertex_colors = True
    data.vertex_colors.update_size()
    data.num_uv_sets = 1
    data.uv_sets.update_size()

    tris = [(0, 1, 2), (3, 4, 5), (6, 7, 8),
            (9, 10, 11), (12, 13, 14), (13, 14, 15)]
    data.num_triangles = len(tris)
    data.num_triangle_points = len(tris) * 3
    data.triangles.update_size()
    for t, (a, b, c) in zip(data.triangles, tris):
        t.v_1, t.v_2, t.v_3 = a, b, c
    return shape, data


class TestVertexlessShapeIsCleared:

    def test_the_fixture_reproduces_the_authored_defect(self, nif):
        """Guard on the guard: if this stops being reproducible the test
        below proves nothing."""
        _shape, data = _shape_with_no_vertex_array(nif)
        # Exactly what the real file reads back as.
        assert data.num_vertices == 16
        assert not data.has_vertices
        assert len(data.vertices) == 0, 'fixture is not vertex-less'
        assert len(data.get_triangles()) == 6
        assert max(max(t) for t in data.get_triangles()) == 15, \
            'triangles must index vertices that are not there'

    def test_it_is_cleared_to_an_empty_shape(self, nif):
        from asset_convert.nif_converter import _sanitize_geometry_data

        shape, data = _shape_with_no_vertex_array(nif)

        class _Doc:
            blocks = [data]

        assert _sanitize_geometry_data(_Doc()) >= 1
        assert data.num_vertices == 0
        assert len(data.get_triangles()) == 0, \
            'triangles left pointing at vertices that do not exist'
        assert not data.has_normals
        assert not data.has_vertex_colors

    def test_the_strips_layout_is_cleared_too(self, nif):
        """The measured case is STRIPS, not triangles.

        `LeyawiinLowerDoor01` is a `NiTriStrips`: its data block has no
        `triangles` array at all. Clearing only `triangles` left the strips
        untouched, the strips-to-triangles conversion downstream rebuilt them,
        and the shape shipped with 0 vertices and 6 faces indexing vertex 15.
        """
        from asset_convert.nif_converter import _sanitize_geometry_data

        data = nif.NiTriStripsData()
        data.has_vertices = False
        data.num_vertices = 0
        data.vertices.update_size()
        data.num_vertices = 16
        assert not hasattr(data, 'triangles'), \
            'fixture must exercise the strips layout, not triangles'

        data.num_strips = 1
        data.strip_lengths.update_size()
        data.strip_lengths[0] = 8
        data.has_points = True
        data.points.update_size()
        for i in range(8):
            data.points[0][i] = i + 8
        data.num_triangles = len(data.get_triangles())
        assert data.num_triangles > 0, 'fixture has no geometry to clear'

        class _Doc:
            blocks = [data]

        assert _sanitize_geometry_data(_Doc()) >= 1
        assert data.num_vertices == 0
        assert len(data.get_triangles()) == 0, 'strips survived the clear'
        assert data.num_strips == 0

    def test_a_healthy_shape_is_untouched(self, nif):
        """The sanitiser must not reach a normal mesh — it runs on every
        converted NIF."""
        from asset_convert.nif_converter import _sanitize_geometry_data

        shape = nif.NiTriShape()
        data = nif.NiTriShapeData()
        shape.data = data
        data.num_vertices = 3
        data.has_vertices = True
        data.vertices.update_size()
        for i, v in enumerate(data.vertices):
            v.x, v.y, v.z = float(i), 0.0, 0.0
        data.num_triangles = 1
        data.num_triangle_points = 3
        data.triangles.update_size()
        data.triangles[0].v_1 = 0
        data.triangles[0].v_2 = 1
        data.triangles[0].v_3 = 2

        class _Doc:
            blocks = [data]

        assert _sanitize_geometry_data(_Doc()) == 0
        assert data.num_vertices == 3
        assert len(data.get_triangles()) == 1
