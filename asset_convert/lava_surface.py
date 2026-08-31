"""Generate the emissive lava-surface meshes for Oblivion's realms.

WHY THIS EXISTS
---------------
Oblivion renders the Deadlands lava as engine WATER: a WATR record whose
`TNAM.Texture` is a bright, scrolling, self-illuminated lava image, tinted by
the record's shallow/deep colors.  14 worldspaces and 46 cells in Oblivion.esm
point at one of those records.

Skyrim's water shader cannot do that.  Its complete pixel-constant set
(`BSWaterShaderPixelConstants`, read out of SkyrimSE.exe at 0x1455789) is

    ShallowColor DeepColor ReflectionColor FresnelRI CameraData ProjData
    VarAmounts SunDir SunColor NumLights LightPos LightColor WaterParams
    DepthControl SSRParams

- no diffuse texture slot and no emissive term.  The three colors only tint a
reflection/refraction result.  All 93 NNAM entries across vanilla Skyrim's 34
WATR records name the same DefaultWater.dds, which is the normal/noise map,
not a color map.  So a WATR alone can never look like lava, however its colors
are set: Oblivion's authored values are DARK (79,12,2) precisely because they
were tinting a bright emissive texture that Skyrim has no way to sample.

Bethesda hit the same wall and solved it with geometry.  Dawnguard's Aetherium
Forge (cell DLC1Bthalft01) layers two things:

  * `LavaSettings` (WATR) reached through the cell's XCWT — the PHYSICS and
    the underwater tint, plus an INAM image space for being submerged.
  * `DweSpecialForgeLava01/02/03.nif` — the LOOK.  Each is an ordinary mesh
    carrying a BSEffectShaderProperty:
        source_texture     textures\\dlc01\\clutter\\WavyTurbulence01.dds
        greyscale_texture  textures\\effects\\gradients\\GradHotCoals.dds
        emissive_color     1,1,1   emissive_multiple 2.0
        controller         BSEffectShaderPropertyFloatController -> U Offset

That is the mechanism this module reproduces.  Bethesda needs the greyscale +
gradient pair because WavyTurbulence01.dds is a colorless turbulence pattern;
Oblivion's own lava texture is ALREADY full color (decoding oblivionlava06.dds
gives (230,97,49), (222,64,32), (205,52,24) — mean channel spread 151 of 255),
so it goes straight into source_texture and no gradient is needed.

The WATR conversion is left alone and still does its job — swim behaviour,
damage, fog and the underwater tint.  This module only adds the visible
surface on top of it.
"""

from __future__ import annotations

import io
import math
import os

from . import pyffi_monkey_patch as _patch  # noqa: F401  (time.clock, nif.xml)
from pyffi.formats.nif import NifFormat

# Skyrim NIF stream: version 20.2.0.7, user 12, user2 83 (LE).  The pipeline
# always writes LE; SSE loads it natively.
_SKYRIM_VER = 0x14020007
_NIF_USER_VERSION = 12
_NIF_USER_VERSION_2 = 83
_NIF_FLAGS = 14

# One exterior cell is 4096 world units square.  A lava plane is built per cell
# so the surface tessellates across a worldspace without one enormous mesh.
CELL_SIZE = 4096.0

# Subdivision of a single plane.  The shader scrolls the texture, so the mesh
# itself carries no animation and only needs enough vertices to avoid
# large-triangle depth artefacts against terrain.  4x4 quads keeps the file
# tiny (25 verts) while staying well-behaved.
_GRID = 4

# How many times the lava texture repeats across one cell.  Oblivion's water
# shader tiled its texture far denser than one repeat per cell; 8 keeps the
# turbulence detail readable at walking scale instead of smearing.
_UV_REPEATS = 8.0

# Measured from Dawnguard's DweSpecialForgeLava01/02/03: emissive white at
# multiple 2.0 (HDR overdrive, so the surface blooms).  Oblivion's texture is
# already the right hue, so the emissive stays neutral and lets it through.
_EMISSIVE_MULTIPLE = 2.0

# Scroll rate in UV units per second.  Oblivion authors ScrollXSpeed /
# ScrollYSpeed on the WATR (OblivionLavaTest01: 0.001 and 0.0001), but those
# are per-frame offsets in a different shader's units and read as motionless
# here.  Dawnguard's own lava scrolls its U Offset over a 20-second loop, so
# that period is used and the AUTHORED axis ratio decides the direction.
_SCROLL_PERIOD = 20.0


def _plane_geometry(size: float, z: float, uv_repeats: float):
    """A flat, upward-facing grid centred on the origin at height `z`."""
    verts, normals, uvs, tris = [], [], [], []
    step = size / _GRID
    half = size / 2.0
    for iy in range(_GRID + 1):
        for ix in range(_GRID + 1):
            x = -half + ix * step
            y = -half + iy * step
            verts.append((x, y, z))
            normals.append((0.0, 0.0, 1.0))
            uvs.append((ix / _GRID * uv_repeats, iy / _GRID * uv_repeats))
    row = _GRID + 1
    for iy in range(_GRID):
        for ix in range(_GRID):
            a = iy * row + ix
            b = a + 1
            c = a + row
            d = c + 1
            # Wind counter-clockwise seen from +Z so the face normal is +Z and
            # the surface is visible from ABOVE, where the player is.  With
            # a=(x,y), b=(x+1,y), c=(x,y+1), (a,c,b) gives a DOWNWARD normal
            # and the plane vanishes when viewed from above.
            tris.append((a, b, c))
            tris.append((b, d, c))
    return verts, normals, uvs, tris


def _build_shape(texture_rel: str, size: float, z: float,
                 scroll_u: float, scroll_v: float):
    """One lava NiTriShape with the Dawnguard effect-shader profile."""
    verts, normals, uvs, tris = _plane_geometry(size, z, _UV_REPEATS)

    tsd = NifFormat.NiTriShapeData()
    tsd.num_vertices = len(verts)
    tsd.has_vertices = True
    tsd.vertices.update_size()
    tsd.has_normals = True
    tsd.normals.update_size()
    tsd.num_uv_sets = 1
    tsd.bs_num_uv_sets = 1
    tsd.uv_sets.update_size()
    # Vertex colours must exist because the shader declares
    # slsf_2_vertex_colors (as Dawnguard's lava does); the engine reads them
    # as a per-vertex multiplier, so opaque white leaves the texture untouched.
    tsd.has_vertex_colors = True
    tsd.vertex_colors.update_size()
    for i in range(len(verts)):
        v = tsd.vertices[i]
        v.x, v.y, v.z = verts[i]
        n = tsd.normals[i]
        n.x, n.y, n.z = normals[i]
        uv = tsd.uv_sets[0][i]
        uv.u, uv.v = uvs[i]
        c = tsd.vertex_colors[i]
        c.r, c.g, c.b, c.a = 1.0, 1.0, 1.0, 1.0
    tsd.num_triangles = len(tris)
    tsd.num_triangle_points = len(tris) * 3
    tsd.has_triangles = True
    tsd.triangles.update_size()
    for i, (a, b, c) in enumerate(tris):
        t = tsd.triangles[i]
        t.v_1, t.v_2, t.v_3 = a, b, c
    tsd.center.x, tsd.center.y, tsd.center.z = 0.0, 0.0, z
    tsd.radius = float(math.hypot(size / 2.0, size / 2.0))
    tsd.consistency_flags = 0x4000  # CT_STATIC

    shader = NifFormat.BSEffectShaderProperty()
    shader.source_texture = texture_rel.encode('latin1')
    # No greyscale_texture: Oblivion's lava image is already full colour, so
    # there is nothing for a gradient palette to map.  Dawnguard needs one
    # only because its source is a colourless turbulence pattern.
    shader.greyscale_texture = b''
    # 0xFF03 = wrap S | wrap T in the low byte, with the high byte vanilla
    # always sets.  Both Dawnguard's lava and this pipeline's own converted
    # scrolling meshes write 0xFF03, not a bare 3.
    shader.texture_clamp_mode = 0xFF03
    shader.emissive_color.r = 1.0
    shader.emissive_color.g = 1.0
    shader.emissive_color.b = 1.0
    shader.emissive_color.a = 1.0
    shader.emissive_multiple = _EMISSIVE_MULTIPLE
    shader.falloff_start_angle = 1.0
    shader.falloff_stop_angle = 1.0
    shader.falloff_start_opacity = 1.0
    shader.falloff_stop_opacity = 1.0
    shader.soft_falloff_depth = 100.0
    shader.uv_scale.u = 1.0
    shader.uv_scale.v = 1.0
    # Flags copied from Dawnguard's DweSpecialForgeLava01 (flags1 0x80000010,
    # flags2 0x21) rather than guessed, MINUS the one bit that is specific to
    # its source art: slsf_1_greyscale_to_palette_color (0x10) tells the
    # shader to look the source texture's greyscale value up in
    # greyscale_texture.  Setting it with no palette bound would sample a
    # missing texture; our source is already full colour, so it stays off.
    shader.shader_flags_1.slsf_1_z_buffer_test = 1
    shader.shader_flags_2.slsf_2_z_buffer_write = 1
    shader.shader_flags_2.slsf_2_vertex_colors = 1
    # Double-sided: the winding faces up, but a lava plane is also seen from
    # BELOW while the player is standing in it (the WATR still governs the
    # swim), and a single-sided surface disappears entirely at that moment.
    shader.shader_flags_2.slsf_2_double_sided = 1

    _attach_scroll(shader, scroll_u, scroll_v)

    shape = NifFormat.NiTriShape()
    shape.name = b'LavaSurface:0'
    shape.flags = _NIF_FLAGS
    shape.data = tsd
    shape.num_properties = 0
    shape.bs_properties.update_size()
    shape.bs_properties[0] = shader
    return shape


def _float_ramp(period: float, span: float):
    """A 2-key linear NiFloatData ramping 0 -> span over `period` seconds."""
    fdata = NifFormat.NiFloatData()
    fdata.data.num_keys = 2
    fdata.data.interpolation = 1        # LINEAR_KEY
    fdata.data.keys.update_size()
    fdata.data.keys[0].time = 0.0
    fdata.data.keys[0].value = 0.0
    fdata.data.keys[1].time = period
    fdata.data.keys[1].value = span
    return fdata


def _attach_scroll(shader, scroll_u: float, scroll_v: float) -> None:
    """Chain U/V Offset float controllers, exactly as vanilla lava does.

    `type_of_controlled_variable` 6 = U Offset, 8 = V Offset
    (EffectShaderControlledVariable in nif.xml).  Vanilla chains one
    controller per animated channel through next_controller.
    """
    head = tail = None
    for var, span in ((6, scroll_u), (8, scroll_v)):
        if not span:
            continue
        ctrl = NifFormat.BSEffectShaderPropertyFloatController()
        ctrl.flags = 72                  # active | cycle-loop
        ctrl.frequency = 1.0
        ctrl.phase = 0.0
        ctrl.start_time = 0.0
        ctrl.stop_time = _SCROLL_PERIOD
        ctrl.type_of_controlled_variable = var
        # The back-pointer to the property being animated.  Vanilla sets it on
        # every one of these controllers, and WITHOUT IT THE ANIMATION NEVER
        # RUNS -- the controller is present in the file and the texture simply
        # sits still.  Same as every other controller the pipeline emits
        # (nif_converter sets .target at each site).
        ctrl.target = shader
        interp = NifFormat.NiFloatInterpolator()
        interp.data = _float_ramp(_SCROLL_PERIOD, span)
        ctrl.interpolator = interp
        if head is None:
            head = tail = ctrl
        else:
            tail.next_controller = ctrl
            tail = ctrl
    if head is not None:
        shader.controller = head


def _scroll_spans(scroll_x: float, scroll_y: float):
    """Turn TES4 scroll speeds into UV spans for one loop.

    Only the RATIO of the authored speeds is meaningful across engines (the
    magnitudes are per-frame offsets in Oblivion's own shader), so the faster
    axis is normalised to one full texture repeat per loop and the other is
    scaled against it.  A record that authors no scroll still creeps, because
    static lava reads as plastic.
    """
    ax, ay = abs(scroll_x), abs(scroll_y)
    peak = max(ax, ay)
    if peak <= 0.0:
        return 0.0, 1.0
    u = (ax / peak) * math.copysign(1.0, scroll_x or 1.0)
    v = (ay / peak) * math.copysign(1.0, scroll_y or 1.0)
    return u, v


def build_lava_nif(texture_rel: str, size: float = CELL_SIZE,
                   scroll_x: float = 0.0, scroll_y: float = 0.0) -> bytes:
    """Serialise a complete lava-surface NIF and return its bytes."""
    scroll_u, scroll_v = _scroll_spans(scroll_x, scroll_y)
    # z = 0: the plane sits at its own origin, and the placed REFR carries the
    # cell's water height, so one mesh serves every height.
    shape = _build_shape(texture_rel, size, 0.0, scroll_u, scroll_v)

    root = NifFormat.BSFadeNode()
    root.name = b'LavaSurface'
    root.flags = _NIF_FLAGS

    # BSXFlags bit 0 (Animated) — WITHOUT THIS THE SCROLL NEVER RUNS.  Skyrim
    # only ticks a mesh's time controllers when the ROOT carries the Animated
    # bit; the file is otherwise completely valid and the texture simply sits
    # still, with no crash and no warning.  Vanilla's own lava
    # (DweSpecialForgeLava01) ships BSX=1, which is the collisionless
    # "animated only" value this plane also wants.  Same trap as the
    # particle-mesh fire bug — see docs/commentary/asset_convert_nif.md.
    bsx = NifFormat.BSXFlags()
    bsx.name = b'BSX'
    bsx.integer_data = 1
    root.num_extra_data_list = 1
    root.extra_data_list.update_size()
    root.extra_data_list[0] = bsx

    root.num_children = 1
    root.children.update_size()
    root.children[0] = shape

    data = NifFormat.Data()
    data.version = _SKYRIM_VER
    data.user_version = _NIF_USER_VERSION
    data.user_version_2 = _NIF_USER_VERSION_2
    data.header.endian_type = 1
    data.roots = [root]

    buf = io.BytesIO()
    data.write(buf)
    return buf.getvalue()


def write_lava_nif(dst_path: str, texture_rel: str, size: float = CELL_SIZE,
                   scroll_x: float = 0.0, scroll_y: float = 0.0) -> bool:
    """Write the lava mesh to `dst_path`.  Returns True on success."""
    try:
        blob = build_lava_nif(texture_rel, size, scroll_x, scroll_y)
    except Exception as exc:                       # pragma: no cover
        print(f'    lava surface generation failed: '
              f'{type(exc).__name__}: {exc}')
        return False
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    with open(dst_path, 'wb') as fh:
        fh.write(blob)
    return True
