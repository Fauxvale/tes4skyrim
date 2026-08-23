"""Lava surface mesh contract.

Each assertion here is a property that, if lost, makes the lava silently WRONG
in game with no crash and no warning — the exact failure mode that cost three
in-game test cycles:

  * BSXFlags Animated bit missing -> controllers never tick, texture frozen
  * winding reversed              -> plane backface-culled, invisible from above
  * controller target unset       -> animation never binds to the shader

They are cheap structural checks on a generated file, not a render test.
"""
import io

import pytest

pyffi = pytest.importorskip('pyffi')

from asset_convert import pyffi_monkey_patch  # noqa: E402,F401
from pyffi.formats.nif import NifFormat  # noqa: E402

from asset_convert.lava_surface import build_lava_nif  # noqa: E402

TEX = r'textures\tes4\water\oblivionlava06.dds'


def _load():
    blob = build_lava_nif(TEX, scroll_x=0.001, scroll_y=0.0001)
    data = NifFormat.Data()
    data.read(io.BytesIO(blob))
    return data


def _blocks(data, kind):
    return [b for b in data.blocks if isinstance(b, kind)]


def test_root_declares_animated():
    """Skyrim only ticks time controllers when the ROOT sets BSXFlags bit 0.

    Without it the scroll animation is present in the file and simply never
    runs.  Vanilla Dawnguard lava ships BSX=1.
    """
    root = _load().roots[0]
    bsx = [e for e in root.extra_data_list
           if isinstance(e, NifFormat.BSXFlags)]
    assert bsx, 'no BSXFlags on root — controllers will not tick'
    assert bsx[0].integer_data & 1, 'BSXFlags Animated bit (0) not set'


def test_surface_faces_up():
    """Every triangle must wind counter-clockwise seen from +Z.

    A downward normal backface-culls the plane from above, which is exactly
    where the player looks at it.
    """
    tsd = _blocks(_load(), NifFormat.NiTriShapeData)[0]
    verts = [(v.x, v.y, v.z) for v in tsd.vertices]
    for tri in tsd.triangles:
        a, b, c = verts[tri.v_1], verts[tri.v_2], verts[tri.v_3]
        z = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        assert z > 0, 'triangle faces downward — plane invisible from above'


def test_scroll_controllers_are_live():
    """The UV scroll must be fully wired, not merely present."""
    data = _load()
    ctrls = _blocks(data, NifFormat.BSEffectShaderPropertyFloatController)
    assert ctrls, 'no scroll controller'
    for c in ctrls:
        assert c.type_of_controlled_variable in (6, 8), \
            'controller does not drive U/V Offset'
        assert c.target is not None, \
            'controller has no target — animation never binds'
        assert (c.flags & 0x48) == 0x48, \
            'missing Active|ComputeScaledTime; curve does not advance'
        assert c.frequency != 0.0
        assert c.stop_time > c.start_time
        keys = [(k.time, k.value) for k in c.interpolator.data.data.keys]
        assert len(keys) >= 2
        assert len({round(v, 6) for _, v in keys}) > 1, \
            'key values never change — a static "animation"'


def test_shader_is_emissive_and_tiling():
    """The look: emissive over 1.0 so it blooms, and wrapping UVs so the
    scroll tiles instead of smearing the edge pixel."""
    shader = _blocks(_load(), NifFormat.BSEffectShaderProperty)[0]
    assert shader.source_texture.decode('latin1').lower() == TEX.lower()
    assert shader.emissive_multiple > 1.0
    assert (shader.emissive_color.r + shader.emissive_color.g
            + shader.emissive_color.b) > 0
    assert shader.texture_clamp_mode & 0x3 == 0x3, 'UVs do not wrap'


def test_no_greyscale_palette_without_a_palette():
    """Oblivion's lava texture is full colour, so the greyscale-to-palette bit
    must stay OFF — setting it would sample a gradient texture we never bind.
    """
    shader = _blocks(_load(), NifFormat.BSEffectShaderProperty)[0]
    assert not (int(shader.shader_flags_1) & 0x10)
    assert not shader.greyscale_texture
