"""Tests for the UI conversion (asset_convert/swf.py + ui_menus.py).

Hermetic: every fixture is synthesised here, so nothing needs Oblivion or
Skyrim installed. The synthetic movie mirrors the SHAPE of vanilla
messagebox.swf -- the same character ids, exports, scaling grid and AS2
initialiser pattern -- because that shape is the contract `patch_message_box`
is written against, and a test that used a simpler movie would not catch a
patch that only works by accident.
"""

import os
import struct
import sys
import zlib

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from asset_convert import swf
from asset_convert import ui_menus


# ---------------------------------------------------------------------------
# synthetic fixtures
# ---------------------------------------------------------------------------

def _dds_bgra(width, height, pixel):
    """An uncompressed BGRA32 DDS filled with `pixel` (an (r,g,b,a) tuple).

    Uncompressed rather than DXT so the test asserts on EXACT pixel values --
    a block-compressed fixture would make "the slice is a 1:1 crop" untestable,
    which is the one thing these crops most need to prove.
    """
    header = bytearray(128)
    header[0:4] = b'DDS '
    struct.pack_into('<I', header, 4, 124)
    struct.pack_into('<I', header, 8, 0x0002100F)
    struct.pack_into('<II', header, 12, height, width)
    struct.pack_into('<I', header, 20, width * 4)
    struct.pack_into('<I', header, 28, 1)
    struct.pack_into('<I', header, 76, 32)
    struct.pack_into('<I', header, 80, 0x41)          # RGB | ALPHAPIXELS
    struct.pack_into('<I', header, 88, 32)
    struct.pack_into('<IIII', header, 92, 0x00FF0000, 0x0000FF00,
                     0x000000FF, 0xFF000000)
    struct.pack_into('<I', header, 108, 0x1000)
    r, g, b, a = pixel
    return bytes(header) + bytes([b, g, r, a]) * (width * height)


def _dds_gradient(width, height):
    """A BGRA32 DDS whose red channel encodes x and green encodes y, so a crop
    can be identified by its pixel values alone."""
    header = _dds_bgra(1, 1, (0, 0, 0, 0))[:128]
    header = bytearray(header)
    struct.pack_into('<II', header, 12, height, width)
    struct.pack_into('<I', header, 20, width * 4)
    body = bytearray()
    for y in range(height):
        for x in range(width):
            body += bytes([y % 256, 0, x % 256, 255])   # B,G,R,A
    return bytes(header) + bytes(body)


_AS2_NAMES = ('WIDTH_MARGIN', 'HEIGHT_MARGIN', 'MESSAGE_TO_BUTTON_SPACER',
              'IsVertical')


def _action_block(values=(20, 30, 10), extra_literal=60):
    """An AVM1 block shaped like __Packages.MessageBox's initialiser.

    ConstantPool, then one `Push [reg1, <name>, <int>]` per margin, then the
    unnamed `Push [reg3, 60]` PositionElements uses as its text margin, then
    the `SetIsVertical` setter body: `Push [reg1, 'IsVertical', reg2]`.
    """
    pool = bytearray(struct.pack('<H', len(_AS2_NAMES)))
    for name in _AS2_NAMES:
        pool += name.encode('latin1') + b'\x00'
    block = bytearray()
    block.append(0x88)
    block += struct.pack('<H', len(pool)) + pool

    def emit_push(payload):
        block.append(0x96)
        block.extend(struct.pack('<H', len(payload)) + payload)

    for i, value in enumerate(values):
        emit_push(bytes([4, 1]) + bytes([8, i])
                  + bytes([7]) + struct.pack('<i', value))
        block.append(0x4F)                             # SetMember

    emit_push(bytes([4, 3]) + bytes([7]) + struct.pack('<i', extra_literal))

    # The setter the engine calls: this.IsVertical = <argument register>.
    emit_push(bytes([4, 1]) + bytes([8, _AS2_NAMES.index('IsVertical')])
              + bytes([4, 2]))
    block.append(0x4F)                                 # SetMember
    block.append(0)                                    # End
    return bytes(block)


def _edit_text(character_id, alpha):
    """A DefineEditText with HasFont + HasTextColor, as vanilla's are."""
    data = bytearray(struct.pack('<H', character_id))
    data += swf.pack_rect(0, 100 * swf.TWIPS, 0, 20 * swf.TWIPS)
    flags = (0x0001 | 0x0004 | 0x0080            # HasFont|HasTextColor|HasText
             | 0x0100 | 0x0200)                  # UseOutlines | HTML
    data += struct.pack('<H', flags)
    data += struct.pack('<HH', 7, 440)          # FontID, FontHeight
    data += bytes([255, 255, 255, alpha])       # TextColor RGBA
    data += b'\x00'                             # VariableName (empty)
    # Vanilla ships an authoring placeholder whose HTML names a color, and
    # that is what the class captures as its DefaultTextFormat.
    data += (b'<p align="center"><font face="$EverywhereMediumFont" size="22"'
             b' color="#ffffff">placeholder</font></p>\x00')
    return swf.Tag(37, bytes(data))


def _solid_shape(character_id, width, height):
    """A DefineShape3 rectangle centered on the origin, as vanilla's shape 10
    is."""
    return ui_menus.transparent_shape(character_id) if width == 0 else \
        swf.define_shape3_bitmap_rects(character_id,
                                       [(999, -width / 2, -height / 2,
                                         width, height)])


def _place_with_cxform(depth, character_id, name, alpha):
    """PlaceObject2 shaped like vanilla's: char + matrix + cxform + name.

    Vanilla dims its panel here (alpha 205/256), which is what makes the
    message box see-through, so the fixture has to carry one.
    """
    body = bytearray([0x02 | 0x04 | 0x08 | 0x20])
    body += struct.pack('<H', depth)
    body += struct.pack('<H', character_id)
    body += swf.pack_matrix(1.0, 1.0, 0, 0)
    w = swf.BitWriter()
    w.ub(0, 1)                                  # HasAddTerms
    w.ub(1, 1)                                  # HasMultTerms
    w.ub(10, 4)                                 # nbits
    for value in (256, 256, 256, alpha):        # R, G, B, A
        w.sb(value, 10)
    body += w.bytes()
    body += name.encode('latin1') + b'\x00'
    return swf.Tag(swf.TAG_PLACE_OBJECT_2, bytes(body))


def _place3_with_shadow(depth, character_id, name, strength=1.0):
    """PlaceObject3 carrying a DropShadow, as vanilla hangs on MessageText."""
    body = bytearray([0x02 | 0x04 | 0x20, 0x01])   # flags1, flags2 HasFilterList
    body += struct.pack('<H', depth)
    body += struct.pack('<H', character_id)
    body += swf.pack_matrix(1.0, 1.0, 0, 0)
    body += name.encode('latin1') + b'\x00'
    body += bytes([1])                              # one filter
    body += bytes([0])                              # DropShadow
    body += bytes([0, 0, 0, 255])                   # color RGBA
    body += struct.pack('<iiii', 2 << 16, 2 << 16, 51471, 2 << 16)
    body += struct.pack('<h', int(strength * 256))
    body += bytes([0x21])                           # filter flags
    return swf.Tag(70, bytes(body))


def _synthetic_movie():
    """A movie with vanilla messagebox.swf's structure."""
    tags = []
    # DiamondMarker: a 20x30 bitmap, format 5.
    tags.append(swf.define_bits_lossless2(
        ui_menus.DIAMOND_MARKER_ID, 20, 30, b'\xff' * (20 * 30 * 4)))
    tags.append(swf.Tag(swf.TAG_EXPORT_ASSETS,
                        struct.pack('<HH', 1, ui_menus.DIAMOND_MARKER_ID)
                        + b'DiamondMarker\x00'))
    # Background art + the clip holding it + its 9-slice.
    tags.append(_solid_shape(ui_menus.BACKGROUND_SHAPE_ID, 432, 155))
    tags.append(swf.define_sprite(
        ui_menus.BACKGROUND_SPRITE_ID,
        [swf.place_object2(1, ui_menus.BACKGROUND_SHAPE_ID)]))
    tags.append(swf.define_scaling_grid(ui_menus.BACKGROUND_SPRITE_ID,
                                        28, 31, 29, 29, 432, 155))
    tags.append(_solid_shape(ui_menus.DIVIDER_SHAPE_ID, 395, 2))
    # The selection arrows the focus box replaces, and the two text fields.
    tags.append(_solid_shape(ui_menus.SELECTION_ARROW_SHAPE_ID, 28, 14))
    for cid, alpha in ((ui_menus.MESSAGE_TEXT_ID, 255),
                       (ui_menus.BUTTON_TEXT_ID, 204)):
        tags.append(_edit_text(cid, alpha))
    tags.append(swf.define_sprite(
        13, [swf.place_object2(1, ui_menus.DIVIDER_SHAPE_ID)]))
    tags.append(swf.define_sprite(9, []))
    tags.append(swf.Tag(swf.TAG_EXPORT_ASSETS,
                        struct.pack('<HH', 1, 9) + b'MessageBoxButton\x00'))
    # MessageBox holds Background_mc at depth 1 and Divider at depth 3, which
    # is the depth ordering the frame has to slot into.
    tags.append(swf.define_sprite(15, [
        _place_with_cxform(1, ui_menus.BACKGROUND_SPRITE_ID,
                           'Background_mc', 205),
        _place_with_cxform(3, 13, 'Divider', 179),
        _place3_with_shadow(5, ui_menus.MESSAGE_TEXT_ID, 'MessageText'),
    ]))
    tags.append(swf.Tag(swf.TAG_EXPORT_ASSETS,
                        struct.pack('<HH', 1, 15) + b'MessageBox\x00'))
    # The AS2 class, as a sprite + export + DoInitAction.
    tags.append(swf.define_sprite(18, []))
    tags.append(swf.Tag(swf.TAG_EXPORT_ASSETS,
                        struct.pack('<HH', 1, 18)
                        + b'__Packages.MessageBox\x00'))
    tags.append(swf.Tag(swf.TAG_DO_INIT_ACTION,
                        struct.pack('<H', 18) + _action_block()))
    tags.append(swf.Tag(swf.TAG_SHOW_FRAME, b''))
    tags.append(swf.Tag(swf.TAG_END, b''))

    frame_size = swf.pack_rect(0, 640 * swf.TWIPS, 0, 480 * swf.TWIPS)
    return swf.Swf(15, frame_size, 24 * 256, 1, tags)


@pytest.fixture
def movie_bytes():
    return _synthetic_movie().serialize()


@pytest.fixture
def textures():
    """Frame and focus art whose slices are identifiable by pixel value."""
    art = {
        'edge_corners.dds': _dds_gradient(128, 128),
        'edge_horizontal.dds': _dds_gradient(1024, 128),
        'edge_vertical.dds': _dds_gradient(128, 1024),
        'center_background.dds': _dds_bgra(64, 64, (10, 20, 30, 255)),
    }
    # The focus box's nine pieces, at the sizes Oblivion ships them.
    for name, size, pixel in (
            ('focus_center_x4.dds', (4, 4), (240, 232, 210, 255)),
            ('focus_top.dds', (1024, 16), (90, 60, 40, 255)),
            ('focus_bottom.dds', (1024, 16), (90, 60, 40, 255)),
            ('focus_left.dds', (8, 256), (90, 60, 40, 255)),
            ('focus_right.dds', (16, 256), (90, 60, 40, 255)),
            ('focus_top_left.dds', (32, 32), (70, 45, 30, 255)),
            ('focus_top_right.dds', (32, 16), (70, 45, 30, 255)),
            ('focus_bottom_left.dds', (8, 32), (70, 45, 30, 255)),
            ('focus_bottom_right.dds', (16, 16), (70, 45, 30, 255))):
        art[name] = _dds_bgra(size[0], size[1], pixel)
    return art


# ---------------------------------------------------------------------------
# container
# ---------------------------------------------------------------------------

def test_round_trip_is_byte_identical(movie_bytes):
    """The safety property the whole reskin rests on: a movie that is parsed
    and re-serialised without being touched comes back EXACTLY, so any diff in
    the output is a change the patch made on purpose."""
    once = swf.Swf.parse(movie_bytes).serialize(compress=False)
    twice = swf.Swf.parse(once).serialize(compress=False)
    assert once == twice


def test_long_form_tag_headers_survive():
    """Bethesda's exporter writes the 6-byte header for some short tags; both
    forms are legal, so re-encoding them shorter would make an untouched movie
    differ for no reason."""
    body = swf.pack_rect(0, 20, 0, 20) + struct.pack('<HH', 24 * 256, 1)
    body += struct.pack('<HI', (swf.TAG_SHOW_FRAME << 6) | 0x3F, 0)
    body += struct.pack('<H', swf.TAG_END << 6)
    raw = b'FWS' + bytes([15]) + struct.pack('<I', 8 + len(body)) + body
    parsed = swf.Swf.parse(raw)
    assert parsed.tags[0].force_long is True
    assert parsed.serialize(compress=False) == raw


def test_parse_rejects_non_swf():
    with pytest.raises(ValueError):
        swf.Swf.parse(b'NOPE' + b'\x00' * 32)


def test_index_of_missing_character_raises(movie_bytes):
    with pytest.raises(KeyError):
        swf.Swf.parse(movie_bytes).index_of_character(4242)


# ---------------------------------------------------------------------------
# tag builders
# ---------------------------------------------------------------------------

def test_scaling_grid_inner_rect_is_centered():
    tag = swf.define_scaling_grid(11, 44, 44, 44, 44, 344, 248)
    assert struct.unpack_from('<H', tag.data, 0)[0] == 11
    assert _read_rect(tag.data[2:]) == [-128.0, 128.0, -80.0, 80.0]


def test_shape_bounds_span_every_piece():
    tag = swf.define_shape3_bitmap_rects(
        10, [(101, -50, -20, 100, 40), (102, 50, -20, 10, 40)])
    assert _read_rect(tag.data[2:]) == [-50.0, 60.0, -20.0, 20.0]


def test_shape_fill_matrix_maps_pixels_to_twips():
    """A bitmap fill covers its rect 1:1 only if the matrix scale is exactly
    TWIPS; anything else renders the art at the wrong size."""
    tag = swf.define_shape3_bitmap_rects(10, [(101, -22, -22, 44, 44)])
    fills = _read_fill_styles(tag.data)
    assert len(fills) == 1
    kind, bitmap_id, scale, translate = fills[0]
    assert kind == 0x41              # clipped, so a stretched border cannot wrap
    assert bitmap_id == 101
    assert scale == (swf.TWIPS, swf.TWIPS)
    assert translate == (-22 * swf.TWIPS, -22 * swf.TWIPS)


def test_place_object_always_writes_a_matrix():
    """🛑 The matrix is optional per spec and MANDATORY in practice.

    Every PlaceObject2 in vanilla Skyrim's interface movies sets HasMatrix and
    spends the one byte an identity matrix costs. Omitting it produced a tag
    this module and every desktop SWF parser read back correctly while the game
    rendered nothing — the frame's eight border clips never appeared, which is
    what made the first in-game build show a bare parchment panel.

    Vanilla's own inner placement, for comparison: 06 0100 0400 00.
    """
    tag = swf.place_object2(1, 4)
    assert tag.data == bytes([0x06, 0x01, 0x00, 0x04, 0x00, 0x00])
    assert tag.data[0] & 0x04, 'HasMatrix is not set'


def test_identity_matrix_is_one_byte():
    """A scale of exactly 1 and no translation take no bits at all; storing
    0x00010000 twice instead is what made the placement byte-heavier than
    vanilla's."""
    assert swf.pack_matrix(1.0, 1.0, 0, 0) == b'\x00'


def test_shape_rejects_empty_piece_list():
    with pytest.raises(ValueError):
        swf.define_shape3_bitmap_rects(10, [])


def test_bitmap_rejects_wrong_sized_data():
    with pytest.raises(ValueError):
        swf.define_bits_lossless2(1, 4, 4, b'\x00' * 10)


def test_bitmap_payload_is_argb(movie_bytes):
    tag = swf.define_bits_lossless2(7, 2, 1, bytes([255, 10, 20, 30,
                                                   128, 1, 2, 3]))
    cid, fmt, w, h = struct.unpack_from('<HBHH', tag.data, 0)
    assert (cid, fmt, w, h) == (7, 5, 2, 1)
    assert zlib.decompress(tag.data[7:]) == bytes([255, 10, 20, 30,
                                                   128, 1, 2, 3])


# ---------------------------------------------------------------------------
# Oblivion layout
# ---------------------------------------------------------------------------

_MENU_XML = """
<menu name="Messagemenu">
  <rect name="background">
    <user0> 700 </user0>
    <text name="message_text">
      <wrapwidth><copy src="background" trait="user0"/><sub> 24 </sub></wrapwidth>
      <y> 15 </y>
    </text>
    <image name="button_1">
      <y><copy src="message_text" trait="height"/><add> 30 </add></y>
    </image>
  </rect>
</menu>
"""

_PREFAB_XML = '<_border_thickness> 44 </_border_thickness>'


def test_reads_vanilla_layout_from_xml():
    layout, warnings = ui_menus.read_oblivion_layout(_MENU_XML, _PREFAB_XML)
    assert warnings == []
    assert layout == {'border': 44, 'box_width': 700, 'text_inset': 12,
                      'text_top': 15, 'button_spacer': 30}


def test_missing_traits_fall_back_and_warn():
    layout, warnings = ui_menus.read_oblivion_layout('<menu/>', '')
    assert layout == ui_menus.VANILLA_LAYOUT
    assert len(warnings) == 5


def test_derived_constants_add_the_border_to_oblivions_insets():
    """Skyrim measures its margins from the panel EDGE and Oblivion measures
    its insets from inside the border, so each margin is border + inset.

    WIDTH is a plain border + inset. HEIGHT additionally carries the SELECTED
    focus box's downward overhang, so the last option's box clears the constant
    scale9 border (see _derived_constants).

    The spacer is the exception and is deliberately NOT Oblivion's authored 30:
    Skyrim's MessageText reports ~120 px at runtime for a single 22 px line
    whatever its authored bounds say, so the field already supplies the gap
    Oblivion used the spacer for.
    """
    constants = ui_menus._derived_constants(
        {'border': 44, 'text_inset': 12, 'text_top': 15, 'button_spacer': 30})
    overhang = ui_menus.FOCUS_CONTENT_H // 2 + ui_menus.FOCUS_EDGE_BOTTOM
    assert constants == {'WIDTH_MARGIN': 56,
                         'HEIGHT_MARGIN': 44 + 15 + overhang,
                         'MESSAGE_TO_BUTTON_SPACER': 0}


# ---------------------------------------------------------------------------
# frame slicing
# ---------------------------------------------------------------------------

def test_slices_are_one_to_one_crops_not_scaled_tiles(textures):
    """cropx/cropy is a SOURCE OFFSET and the region taken is border px across,
    1:1. Reading it as "the whole 64px tile, resampled to 44" shrinks the art
    to ~31px and opens a gap between the border and the parchment on all four
    sides. The gradient fixture makes the two readings distinguishable: a 1:1
    crop of the top-left corner has red running 0..43, a resampled tile has it
    running 0..63.
    """
    slices = dict(ui_menus.build_frame_slices(textures, 44))
    top_left = slices['top_left']
    assert top_left.size == (44, 44)
    assert top_left.getpixel((0, 0))[:3] == (0, 0, 0)
    assert top_left.getpixel((43, 0))[0] == 43       # NOT 63
    assert top_left.getpixel((0, 43))[2] == 43


def test_second_tile_slices_start_at_the_tile_stride(textures):
    """The right-hand and bottom pieces come from the second tile, so their
    first column/row is the stride, not zero."""
    slices = dict(ui_menus.build_frame_slices(textures, 44))
    assert slices['top_right'].getpixel((0, 0))[0] == 64
    assert slices['bottom_left'].getpixel((0, 0))[2] == 64
    assert slices['right'].getpixel((0, 0))[0] == 64


def test_edges_keep_their_native_length_for_tiling(textures):
    """🛑 The edges are NOT resized along their length.

    generic_background.xml stretches its edges, and copying that squashes the
    carving far harder than Oblivion does: Oblivion's box is a FIXED 700 px
    wide so a 1024 px motif compresses to 0.68x, whereas composing 1024 into
    the base and then scaling the base to the panel measured 0.35x on a 422 px
    panel -- which reads as the border being squished between the corners.
    Kept at source length and tiled, the only squeeze left is the panel's.
    """
    slices = dict(ui_menus.build_frame_slices(textures, 44))
    assert [n for n, _ in ui_menus.build_frame_slices(textures, 44)] == [
        'top_left', 'top', 'top_right',
        'left', 'center', 'right',
        'bottom_left', 'bottom', 'bottom_right']
    # Corners: a 1:1 crop at the border size.
    for name in ('top_left', 'top_right', 'bottom_left', 'bottom_right'):
        assert slices[name].size == (44, 44)
    # Edges: border thick, SOURCE length.
    source_w = ui_menus.to_image(textures['edge_horizontal.dds']).width
    source_h = ui_menus.to_image(textures['edge_vertical.dds']).height
    assert slices['top'].size == slices['bottom'].size == (source_w, 44)
    assert slices['left'].size == slices['right'].size == (44, source_h)
    # The center still stretches -- Oblivion `zoom`s it across the whole box.
    assert slices['center'].size == (ui_menus.CENTER_BASE_W,
                                     ui_menus.CENTER_BASE_H)


def test_composed_edges_are_tiled_not_stretched(textures):
    """A band shorter than the source is a crop of it, so the motif lands at
    exactly the same pixels it has in the texture."""
    slices = dict(ui_menus.build_frame_slices(textures, 44))
    frame = ui_menus.compose_frame(slices, 44)
    top = slices['top']
    # A few px into the top band, the frame must match the source 1:1.
    for x in (0, 5, 40):
        assert frame.getpixel((44 + x, 3)) == top.getpixel((x, 3)),             'the top edge was resampled instead of tiled'


def test_tiling_fills_a_band_longer_than_the_source(textures):
    """A band longer than the source repeats rather than leaving a gap."""
    strip = ui_menus.to_image(textures['edge_corners.dds']).crop((0, 0, 8, 4))
    filled = ui_menus.tile_image(strip, 20, 4)
    assert filled.size == (20, 4)
    assert filled.getpixel((0, 0)) == filled.getpixel((8, 0))         == filled.getpixel((16, 0))


def test_border_thicker_than_the_tile_is_clamped(textures):
    """An edited UI could author a border wider than the art tile; cropping
    past the stride would pull in the NEXT tile rather than fail."""
    slices = dict(ui_menus.build_frame_slices(textures, 200))
    assert slices['top_left'].size == (64, 64)


def test_premultiplied_argb_never_exceeds_alpha():
    from PIL import Image
    img = Image.new('RGBA', (4, 4), (255, 200, 100, 128))
    data = ui_menus.premultiplied_argb(img)
    for i in range(0, len(data), 4):
        a, r, g, b = data[i:i + 4]
        assert r <= a and g <= a and b <= a


# ---------------------------------------------------------------------------
# AS2 patching
# ---------------------------------------------------------------------------

def test_constants_are_rewritten_in_place():
    """The replacement must not change the block's LENGTH: every jump offset
    in AVM1 is relative, so a resized action would silently corrupt control
    flow rather than fail to parse."""
    block = _action_block()
    patched, applied = ui_menus.patch_as2_constants(
        block, {'WIDTH_MARGIN': 56, 'HEIGHT_MARGIN': 59})
    assert len(patched) == len(block)
    assert applied == {'WIDTH_MARGIN': (20, 56), 'HEIGHT_MARGIN': (30, 59)}
    again, _ = ui_menus.patch_as2_constants(patched, {'WIDTH_MARGIN': 56})
    assert again == patched


def test_unknown_constant_raises():
    with pytest.raises(ui_menus.UiConvertError):
        ui_menus.patch_as2_constants(_action_block(), {'NOT_A_MARGIN': 1})


def test_ambiguous_constant_raises():
    """Two initialisers for one name means the anchor is not unique, and
    guessing which to rewrite would ship a half-applied layout."""
    block = _action_block() + _action_block()
    with pytest.raises(ui_menus.UiConvertError):
        ui_menus.patch_as2_constants(block, {'WIDTH_MARGIN': 56})


def test_unnamed_literal_is_rewritten_by_its_push_shape():
    block = _action_block()
    patched, change = ui_menus.patch_as2_literal(block, 3, 60, 112)
    assert len(patched) == len(block)
    assert change == (60, 112)
    with pytest.raises(ui_menus.UiConvertError):
        ui_menus.patch_as2_literal(patched, 3, 60, 112)   # no longer present


# ---------------------------------------------------------------------------
# the whole patch
# ---------------------------------------------------------------------------

def test_patch_changes_only_the_intended_characters(movie_bytes, textures):
    layout = dict(ui_menus.VANILLA_LAYOUT)
    out, report = ui_menus.patch_message_box(movie_bytes, textures, layout)

    before = swf.Swf.parse(movie_bytes)
    after = swf.Swf.parse(out)
    old = {t.character_id: t.data for t in before.tags
           if t.character_id is not None}
    new = {t.character_id: t.data for t in after.tags
           if t.character_id is not None}

    changed = {cid for cid in old if new.get(cid) != old[cid]}
    assert changed == {ui_menus.BACKGROUND_SHAPE_ID,       # the whole frame
                       ui_menus.DIVIDER_SHAPE_ID,
                       ui_menus.SELECTION_ARROW_SHAPE_ID,  # -> focus box
                       ui_menus.MESSAGE_TEXT_ID,           # recolored
                       ui_menus.BUTTON_TEXT_ID,
                       ui_menus.SELECTION_ARROW_SHAPE_ID,
                       15}          # + the opacity and shadow edits
    # The exported DiamondMarker bitmap is NOT touched: it is never placed in
    # this movie, so blanking it changes nothing on screen.
    assert new[ui_menus.DIAMOND_MARKER_ID] == old[ui_menus.DIAMOND_MARKER_ID]
    # Two additions: the composed frame bitmap and the composed focus box.
    assert len(new) - len(old) == 2

    # Every AS2 block keeps its length.
    assert ([len(t.data) for t in before.tags
             if t.code == swf.TAG_DO_INIT_ACTION]
            == [len(t.data) for t in after.tags
                if t.code == swf.TAG_DO_INIT_ACTION])

    assert report['constants']['WIDTH_MARGIN'] == (20, 56)
    assert report['literals']['reg3:60'] == (60, 112)


def test_scaling_grid_is_recut_to_our_border(movie_bytes, textures):
    """🛑 The grid must be RE-CUT, not inherited and not removed.

    Background_mc ships with a DefineScalingGrid -- vanilla 9-slices it. The
    splitter is authored for vanilla's 432x155 panel art, roughly a 30 px
    border. Our shape is 560x800, so that same splitter leaves fixed rows
    ~354 px tall top and bottom: more than a whole 379 px panel, which
    degenerates the slice and drops the engine back to a plain stretch. That
    is the artifact -- side borders at twice the thickness of top and bottom,
    with the carving on them compressed to under half density.

    Re-cut to OUR border, the corners hold their size at any panel size.
    """
    out, report = ui_menus.patch_message_box(movie_bytes, textures,
                                             dict(ui_menus.VANILLA_LAYOUT))
    after = swf.Swf.parse(out)
    j = after.scaling_grid_index(ui_menus.BACKGROUND_SPRITE_ID)
    assert j is not None, 'the grid was removed rather than re-cut'
    inner = _read_rect(after.tags[j].data[2:])
    box = _read_rect(
        after.tags[after.index_of_character(
            ui_menus.BACKGROUND_SHAPE_ID)].data[2:])
    border = report['frame']['border']
    # _read_rect gives (x_min, x_max, y_min, y_max) in pixels.
    for label, got in (('left', inner[0] - box[0]),
                       ('right', box[1] - inner[1]),
                       ('top', inner[2] - box[2]),
                       ('bottom', box[3] - inner[3])):
        assert abs(got - border) < 0.5, (
            f'{label} fixed edge is {got:.1f}px, expected the {border}px border')
    assert 'scale9' in report


def test_scale9_can_be_turned_off(movie_bytes, textures):
    """The engine has refused things that looked this safe before, so the
    previous behaviour stays reachable without editing code."""
    out, report = ui_menus.patch_message_box(movie_bytes, textures,
                                             dict(ui_menus.VANILLA_LAYOUT),
                                             scale9=False)
    before = swf.Swf.parse(movie_bytes)
    after = swf.Swf.parse(out)
    i = before.scaling_grid_index(ui_menus.BACKGROUND_SPRITE_ID)
    j = after.scaling_grid_index(ui_menus.BACKGROUND_SPRITE_ID)
    assert before.tags[i].data == after.tags[j].data
    assert 'vanilla' in report['scale9']


def test_background_shape_carries_exactly_one_bitmap_fill(movie_bytes,
                                                          textures):
    """🛑 ONE fill. Skyrim draws a shape's FIRST bitmap fill across the whole
    shape and ignores the rest.

    Vanilla agrees: 202 of its 207 bitmap-filled shapes declare exactly one
    fill, and the five exceptions are a single continuous path switching fill
    along its edges, never disjoint filled rectangles. Handed nine rects the
    engine drew only the first -- as a magnified corner in round 1 (first rect
    `top_left`) and as bare parchment in round 5 (first rect the center).
    """
    out, _ = ui_menus.patch_message_box(movie_bytes, textures,
                                        dict(ui_menus.VANILLA_LAYOUT))
    movie = swf.Swf.parse(out)
    tag = movie.tags[movie.index_of_character(ui_menus.BACKGROUND_SHAPE_ID)]
    fills = _read_bitmap_fill_ids(tag.data)
    assert len(fills) == 1, 'more than one fill will not render'
    width = 2 * 44 + ui_menus.CENTER_BASE_W
    height = 2 * 44 + ui_menus.CENTER_BASE_H
    assert _read_rect(tag.data[2:]) == [-width / 2, width / 2,
                                        -height / 2, height / 2]


def test_margins_clear_the_constant_border_and_the_focus_box(textures):
    """🛑 The invariant that stops a choice rendering on top of the border.

    With the scaling grid re-cut to the border, the frame no longer rides
    Background_mc's uniform scale: the border is a CONSTANT `border` px at any
    panel size. So the old panel-dependent check (`border*panel/base < margin`)
    is void, and the invariant is now a plain constant one, the same for every
    box.

    Two things must clear that constant border:
      * the message/choice TEXT -- WIDTH_MARGIN and the text part of
        HEIGHT_MARGIN handle this, exactly as before;
      * the SELECTED option's FOCUS BOX, which hangs FOCUS_EDGE_BOTTOM px past
        the glyphs it frames. This is what put the last choice back on the
        border once the border stopped scaling thin on small panels, and it is
        why HEIGHT_MARGIN now carries FOCUS_EDGE_BOTTOM on top of the text
        clearance.
    """
    border = ui_menus.VANILLA_LAYOUT['border']
    constants = ui_menus._derived_constants(ui_menus.VANILLA_LAYOUT)

    # Room for the border AND the focus box's full downward overhang below the
    # button center (the worst case: last button center at the container edge).
    focus_overhang = ui_menus.FOCUS_CONTENT_H // 2 + ui_menus.FOCUS_EDGE_BOTTOM
    assert constants['HEIGHT_MARGIN'] >= border + focus_overhang, (
        f"HEIGHT_MARGIN {constants['HEIGHT_MARGIN']} leaves no room for the "
        f"focus box ({focus_overhang}px overhang) past the {border}px border")
    # And still room for the text on the width axis.
    assert constants['WIDTH_MARGIN'] > border, (
        f"WIDTH_MARGIN {constants['WIDTH_MARGIN']} does not clear the "
        f"{border}px border")


def test_message_field_is_shrunk_to_cut_the_dead_space(movie_bytes, textures):
    """Vanilla authors MessageText 121 px tall for a single 22 px line, and
    `PositionElements` folds that straight into the panel height -- so every
    box reserved ~100 px of empty field between the header and the choices,
    the same on a four-choice box as on a ten-choice one."""
    out, report = ui_menus.patch_message_box(movie_bytes, textures,
                                             dict(ui_menus.VANILLA_LAYOUT))
    movie = swf.Swf.parse(out)
    tag = movie.tags[movie.index_of_character(ui_menus.MESSAGE_TEXT_ID)]
    bounds = _read_rect(tag.data[2:])
    assert bounds[3] - bounds[2] == ui_menus.MESSAGE_TEXT_HEIGHT
    assert 'message_field' in report
    # Still room for several lines, since the field may not auto-grow.
    assert ui_menus.MESSAGE_TEXT_HEIGHT >= 3 * 22


def test_message_field_height_can_be_left_alone(movie_bytes, textures):
    _out, report = ui_menus.patch_message_box(
        movie_bytes, textures, dict(ui_menus.VANILLA_LAYOUT),
        message_height=0)
    assert 'message_field' not in report


def test_only_the_center_is_softened(textures):
    """The center is the one slice with photographic detail and the one
    Oblivion itself stretches hardest, so it is composed coarse to keep the
    file down. The BORDER, which is what the eye reads, stays full detail."""
    slices = dict(ui_menus.build_frame_slices(textures, 44))
    frame = ui_menus.compose_frame(slices, 44)
    corner = slices['top_left']
    for x in (0, corner.width - 1):
        for y in (0, corner.height - 1):
            assert frame.getpixel((x, y)) == corner.getpixel((x, y)),                 'a corner pixel was resampled'


def test_composed_frame_places_every_slice(textures):
    """The frame is assembled offline, so the nine-slice geometry has to be
    right in the IMAGE rather than in the shape."""
    slices = dict(ui_menus.build_frame_slices(textures, 44))
    frame = ui_menus.compose_frame(slices, 44)
    assert frame.size == (2 * 44 + ui_menus.CENTER_BASE_W,
                          2 * 44 + ui_menus.CENTER_BASE_H)
    # Corners come from the corner tiles, which the gradient fixture makes
    # identifiable: the top-left tile starts at red 0, the right-hand tiles at
    # the 64px stride.
    assert frame.getpixel((0, 0))[3] > 0, 'top-left corner is empty'
    assert frame.getpixel((frame.width - 1, 0))[3] > 0, 'top-right is empty'
    assert frame.getpixel((0, frame.height - 1))[3] > 0, 'bottom-left is empty'
    center = frame.getpixel((frame.width // 2, frame.height // 2))
    assert center[:3] == (10, 20, 30), 'center is not the parchment fixture'


def test_supersample_multiplies_the_bitmap_but_not_the_geometry(textures):
    slices = dict(ui_menus.build_frame_slices(textures, 44))
    one = ui_menus.compose_frame(slices, 44, 1)
    two = ui_menus.compose_frame(slices, 44, 2)
    assert two.size == (one.width * 2, one.height * 2)


def test_buttons_are_forced_vertical(movie_bytes, textures):
    """Oblivion always stacks its choices; Skyrim's engine asked for a row and
    laid ten birthsigns across the whole screen."""
    out, report = ui_menus.patch_message_box(
        movie_bytes, textures, dict(ui_menus.VANILLA_LAYOUT))
    assert report['is_vertical'] == ('reg2', True)
    before = swf.Swf.parse(movie_bytes)
    after = swf.Swf.parse(out)
    assert ([len(t.data) for t in before.tags
             if t.code == swf.TAG_DO_INIT_ACTION]
            == [len(t.data) for t in after.tags
                if t.code == swf.TAG_DO_INIT_ACTION])

    out2, report2 = ui_menus.patch_message_box(
        movie_bytes, textures, dict(ui_menus.VANILLA_LAYOUT),
        force_vertical=False)
    assert 'is_vertical' not in report2


def test_marker_and_divider_are_kept_on_request(movie_bytes, textures):
    out, report = ui_menus.patch_message_box(
        movie_bytes, textures, dict(ui_menus.VANILLA_LAYOUT),
        hide_divider=False, hide_marker=False)
    before = swf.Swf.parse(movie_bytes)
    after = swf.Swf.parse(out)
    for cid in (ui_menus.DIVIDER_SHAPE_ID, ui_menus.DIAMOND_MARKER_ID):
        assert (before.tags[before.index_of_character(cid)].data
                == after.tags[after.index_of_character(cid)].data)
    assert 'divider' not in report and 'marker' not in report


def test_selection_arrows_become_an_oblivion_focus_box(movie_bytes, textures):
    """Oblivion rings the focused choice with a box built from its own art
    (menus/prefabs/focus_box.xml); Skyrim flanks it with arrows.

    Only the SHAPE is swapped, so the AS2's focus handling is untouched -- it
    still sizes the indicator to `ButtonText._width + SELECTION_INDICATOR_WIDTH`
    and still centers it on the label.
    """
    out, _ = ui_menus.patch_message_box(movie_bytes, textures,
                                        dict(ui_menus.VANILLA_LAYOUT))
    movie = swf.Swf.parse(out)
    tag = movie.tags[movie.index_of_character(
        ui_menus.SELECTION_ARROW_SHAPE_ID)]
    fills = _read_bitmap_fill_ids(tag.data)
    assert len(fills) == 1, 'more than one fill will not render'


def test_focus_box_centers_its_CONTENT_not_its_image(textures):
    """Oblivion's border is asymmetric -- 9 px above the content, 14 below --
    and Skyrim positions the indicator by its own origin, so centering the
    image would sit the box low on the label."""
    box, offset_x, offset_y = ui_menus.compose_focus_box(textures)
    assert box.size == (ui_menus.FOCUS_EDGE_LEFT + ui_menus.FOCUS_CONTENT_W
                        + ui_menus.FOCUS_EDGE_RIGHT,
                        ui_menus.FOCUS_EDGE_TOP + ui_menus.FOCUS_CONTENT_H
                        + ui_menus.FOCUS_EDGE_BOTTOM)
    # The content box, placed at these offsets, straddles the origin evenly.
    content_left = offset_x + ui_menus.FOCUS_EDGE_LEFT
    content_top = offset_y + ui_menus.FOCUS_EDGE_TOP
    assert content_left == -ui_menus.FOCUS_CONTENT_W / 2
    assert content_top == -ui_menus.FOCUS_CONTENT_H / 2
    # ...whereas centering the image would not.
    assert offset_y != -box.height / 2


def test_focus_box_is_wide_enough_to_survive_the_stretch():
    """The AS2 sets only `_width`, so the horizontal scale is
    `(textWidth + 25) / natural` and that same factor thickens the box's SIDES.
    Vanilla's 28.5 px arrow would scale a typical label ~6x and turn the sides
    into slabs; the natural width has to stay near real label widths.
    """
    natural = (ui_menus.FOCUS_EDGE_LEFT + ui_menus.FOCUS_CONTENT_W
               + ui_menus.FOCUS_EDGE_RIGHT)
    assert natural >= 150
    for text_width in (40, 150, 300):
        scale = (text_width + 25) / natural
        assert 0.2 < scale < 2.0, f'{text_width}px label scales {scale:.2f}x'


def test_panel_is_made_fully_opaque(movie_bytes, textures):
    """🛑 The see-through panel is the PLACEMENT, not the artwork.

    Vanilla places Background_mc with a color transform whose alpha
    multiplier is 205/256, and that multiplies the whole clip however opaque
    the bitmap inside it is. The rewrite is in place at the field's existing
    bit width, so the tag keeps its length.
    """
    out, report = ui_menus.patch_message_box(movie_bytes, textures,
                                             dict(ui_menus.VANILLA_LAYOUT))
    before = swf.Swf.parse(movie_bytes)
    after = swf.Swf.parse(out)
    old = before.tags[before.index_of_character(15)].data
    new = after.tags[after.index_of_character(15)].data
    assert len(new) == len(old), 'the sprite body changed length'

    code, offset, length = ui_menus.find_named_child(new, 'Background_mc')
    _position, _width, alpha = swf.find_cxform_alpha(new, offset, length)
    assert alpha == 256
    assert report['opacity'].endswith('205/256 -> 256/256')


def test_opacity_can_be_left_alone(movie_bytes, textures):
    out, report = ui_menus.patch_message_box(
        movie_bytes, textures, dict(ui_menus.VANILLA_LAYOUT), opaque=False)
    movie = swf.Swf.parse(out)
    body = movie.tags[movie.index_of_character(15)].data
    _c, offset, length = ui_menus.find_named_child(body, 'Background_mc')
    assert swf.find_cxform_alpha(body, offset, length)[2] == 205
    assert 'opacity' not in report


def test_header_drop_shadow_is_muted(movie_bytes, textures):
    """Skyrim hangs a DropShadow on MessageText, which reads as an outline
    against parchment. Zeroing STRENGTH keeps the tag's length and filter
    count, where dropping the filter list would mean re-headering the tag."""
    out, report = ui_menus.patch_message_box(movie_bytes, textures,
                                             dict(ui_menus.VANILLA_LAYOUT))
    movie = swf.Swf.parse(out)
    body = movie.tags[movie.index_of_character(15)].data
    code, offset, length = ui_menus.find_named_child(body, 'MessageText')
    assert code == 70
    raw = body[offset:offset + length]
    at = raw.index(b'MessageText\x00') + len('MessageText') + 1
    assert raw[at] == 1, 'the filter count changed'
    assert raw[at + 1] == 0, 'the filter is no longer a DropShadow'
    strength = struct.unpack_from('<h', raw, at + 2 + 4 + 16)[0]
    assert strength == 0
    assert report['shadow'] == '1 filter(s) on MessageText muted'


def test_shadow_can_be_left_alone(movie_bytes, textures):
    _out, report = ui_menus.patch_message_box(
        movie_bytes, textures, dict(ui_menus.VANILLA_LAYOUT),
        mute_shadow=False)
    assert 'shadow' not in report


def test_text_is_recolored_but_keeps_its_alpha(movie_bytes, textures):
    """Vanilla runs the message at alpha 255 and the unfocused choices at 204,
    and that difference is what dims an unselected option."""
    out, report = ui_menus.patch_message_box(movie_bytes, textures,
                                             dict(ui_menus.VANILLA_LAYOUT))
    brown = tuple(ui_menus.OBLIVION_TEXT_RGB)
    assert report['text_color']['message'][:2] == (
        (255, 255, 255, 255), brown + (255,))
    assert report['text_color']['choices'][:2] == (
        (255, 255, 255, 204), brown + (204,))


def test_the_html_color_baked_into_the_field_is_recolored_too(movie_bytes,
                                                                textures):
    """🛑 TextColor alone is not enough for the message.

    Both fields ship an authoring placeholder as HTML carrying an explicit
    `color="#ffffff"`, and the class captures it --
    `DefaultTextFormat = Message.getTextFormat()` reports the format of the
    text CURRENTLY in the field -- then paints it back over every message in
    `SetMessage`. The header stayed white in game for exactly that reason while
    the choices, which assign `.text` and pick up TextColor normally, went
    brown. Six hex digits replace six, so the record keeps its length.
    """
    out, report = ui_menus.patch_message_box(movie_bytes, textures,
                                             dict(ui_menus.VANILLA_LAYOUT))
    expected = '#%02x%02x%02x' % tuple(ui_menus.OBLIVION_TEXT_RGB)
    before = swf.Swf.parse(movie_bytes)
    after = swf.Swf.parse(out)
    for character_id in (ui_menus.MESSAGE_TEXT_ID, ui_menus.BUTTON_TEXT_ID):
        old = before.tags[before.index_of_character(character_id)].data
        new = after.tags[after.index_of_character(character_id)].data
        assert len(new) == len(old), 'the record changed length'
        assert b'#ffffff' not in new
        assert expected.encode() in new
    assert report['text_color']['message'][2] == 1      # one html edit
    assert report['text_color']['choices'][2] == 1


def test_recolor_refuses_a_field_with_no_color_of_its_own():
    data = bytearray(struct.pack('<H', 99))
    data += swf.pack_rect(0, 20, 0, 20)
    data += struct.pack('<H', 0x0000)          # no HasTextColor
    with pytest.raises(ui_menus.UiConvertError):
        ui_menus.recolor_edit_text(swf.Tag(37, bytes(data)), (1, 2, 3))


def test_patch_refuses_a_movie_it_does_not_recognise(textures):
    """A future Skyrim patch that renumbers characters must fail loudly rather
    than emit a movie whose menu no longer works."""
    movie = _synthetic_movie()
    movie.tags = [t for t in movie.tags
                  if not (t.code == swf.TAG_EXPORT_ASSETS
                          and b'MessageBox\x00' in t.data)]
    with pytest.raises(ui_menus.UiConvertError):
        ui_menus.patch_message_box(movie.serialize(), textures,
                                   dict(ui_menus.VANILLA_LAYOUT))


def test_patch_refuses_a_movie_whose_as2_has_no_margins(textures):
    movie = _synthetic_movie()
    for i, tag in enumerate(movie.tags):
        if tag.code == swf.TAG_DO_INIT_ACTION:
            movie.tags[i] = swf.Tag(tag.code, tag.data[:2] + b'\x00')
    with pytest.raises(ui_menus.UiConvertError):
        ui_menus.patch_message_box(movie.serialize(), textures,
                                   dict(ui_menus.VANILLA_LAYOUT))


# ---------------------------------------------------------------------------
# helpers: read back what the builders wrote
# ---------------------------------------------------------------------------

def _read_bitmap_fill_ids(shape_data):
    """[(fill_type, bitmap_id)] of a DefineShape3's bitmap fill styles."""
    bits = _Bits(shape_data, 2)
    n = bits.u(5)
    for _ in range(4):
        bits.s(n)
    bits.align()
    count = shape_data[bits.offset]
    bits.offset += 1
    out = []
    for _ in range(count):
        kind = shape_data[bits.offset]
        bits.offset += 1
        out.append((kind, struct.unpack_from('<H', shape_data, bits.offset)[0]))
        bits.offset += 2
        if bits.u(1):
            nb = bits.u(5); bits.s(nb); bits.s(nb)
        if bits.u(1):
            nb = bits.u(5); bits.s(nb); bits.s(nb)
        nb = bits.u(5); bits.s(nb); bits.s(nb)
        bits.align()
    return out


def _read_solid_fills(shape_data):
    """[(r, g, b, a)] of a DefineShape3's solid fill styles."""
    bits = _Bits(shape_data, 2)
    n = bits.u(5)
    for _ in range(4):
        bits.s(n)
    bits.align()
    count = shape_data[bits.offset]
    at = bits.offset + 1
    out = []
    for _ in range(count):
        kind = shape_data[at]
        at += 1
        assert kind == 0x00, f'fill style {hex(kind)} is not solid'
        out.append(tuple(shape_data[at:at + 4]))
        at += 4
    return out


def _sprite_children(movie, sprite_id, named_only=True, with_translate=False):
    """A sprite's children.

    {name: (depth, character_id)} by default; `with_translate` adds the
    placement's translation in pixels, and `named_only=False` returns an
    ordered list including unnamed children.
    """
    from asset_convert.swf import TAG_PLACE_OBJECT_2
    tag = movie.tags[movie.index_of_character(sprite_id)]
    out = {} if named_only else []
    for code, offset, length in _iter_sprite_tags(tag.data):
        if code != TAG_PLACE_OBJECT_2:
            continue
        flags = tag.data[offset]
        at = offset + 1
        depth = struct.unpack_from('<H', tag.data, at)[0]
        at += 2
        character = None
        if flags & 0x02:
            character = struct.unpack_from('<H', tag.data, at)[0]
            at += 2
        translate = (0.0, 0.0)
        if flags & 0x04:
            at, translate = _read_matrix(tag.data, at)
        if flags & 0x08:
            at = _skip_cxform(tag.data, at)
        if flags & 0x10:
            at += 2
        if not named_only:
            out.append((depth, character))
        elif flags & 0x20:
            end = tag.data.index(b'\x00', at)
            name = tag.data[at:end].decode('latin1')
            out[name] = ((depth, character, translate) if with_translate
                         else (depth, character))
    return out


def _iter_sprite_tags(data, start=4):
    offset = start
    while offset < len(data):
        (packed,) = struct.unpack_from('<H', data, offset)
        offset += 2
        code = packed >> 6
        length = packed & 0x3F
        if length == 0x3F:
            (length,) = struct.unpack_from('<I', data, offset)
            offset += 4
        yield code, offset, length
        offset += length
        if code == 0:
            break


def _read_matrix(data, offset):
    """(offset past the matrix, (translate_x, translate_y) in pixels)."""
    bits = _Bits(data, offset)
    if bits.u(1):
        n = bits.u(5); bits.s(n); bits.s(n)
    if bits.u(1):
        n = bits.u(5); bits.s(n); bits.s(n)
    n = bits.u(5)
    translate = (bits.s(n) / swf.TWIPS, bits.s(n) / swf.TWIPS)
    bits.align()
    return bits.offset, translate


def _skip_matrix(data, offset):
    return _read_matrix(data, offset)[0]


def _skip_cxform(data, offset):
    bits = _Bits(data, offset)
    has_add = bits.u(1)
    has_mult = bits.u(1)
    n = bits.u(4)
    for _ in range(4 if has_mult else 0):
        bits.s(n)
    for _ in range(4 if has_add else 0):
        bits.s(n)
    bits.align()
    return bits.offset


# ---------------------------------------------------------------------------
# bit readers
# ---------------------------------------------------------------------------

class _Bits:
    def __init__(self, data, offset=0):
        self.data = data
        self.offset = offset
        self.bit = 0

    def u(self, n):
        value = 0
        for _ in range(n):
            value = (value << 1) | ((self.data[self.offset] >> (7 - self.bit)) & 1)
            self.bit += 1
            if self.bit == 8:
                self.bit = 0
                self.offset += 1
        return value

    def s(self, n):
        value = self.u(n)
        return value - (1 << n) if n and value & (1 << (n - 1)) else value

    def align(self):
        if self.bit:
            self.bit = 0
            self.offset += 1


def _read_rect(data):
    bits = _Bits(data)
    n = bits.u(5)
    return [bits.s(n) / swf.TWIPS for _ in range(4)]


def _read_fill_styles(shape_data):
    """[(type, bitmap_id, (scale_x, scale_y), (tx, ty))] from a DefineShape3."""
    bits = _Bits(shape_data, 2)
    n = bits.u(5)
    for _ in range(4):
        bits.s(n)
    bits.align()
    count = shape_data[bits.offset]
    bits.offset += 1
    out = []
    for _ in range(count):
        kind = shape_data[bits.offset]
        bits.offset += 1
        bitmap_id = struct.unpack_from('<H', shape_data, bits.offset)[0]
        bits.offset += 2
        scale = (1.0, 1.0)
        if bits.u(1):
            nb = bits.u(5)
            scale = (bits.s(nb) / 65536, bits.s(nb) / 65536)
        if bits.u(1):
            nb = bits.u(5)
            bits.s(nb)
            bits.s(nb)
        nb = bits.u(5)
        translate = (bits.s(nb), bits.s(nb))
        bits.align()
        out.append((kind, bitmap_id, scale, translate))
    return out


# ---------------------------------------------------------------------------
# main menu (asset_convert/ui_main_menu.py)
# ---------------------------------------------------------------------------

def _mini_startmenu():
    """A movie in the shape patch_main_menu needs: a character with the
    MenuHolder id (604) placed at depth 1 at the root, nothing else there."""
    from asset_convert.ui_main_menu import MENUHOLDER_CHARACTER, STAGE_W, STAGE_H
    empty_sprite = swf.Tag(
        swf.TAG_DEFINE_SPRITE,
        struct.pack('<HH', MENUHOLDER_CHARACTER, 1)
        + struct.pack('<H', swf.TAG_SHOW_FRAME << 6)
        + struct.pack('<H', swf.TAG_END << 6))
    tags = [empty_sprite,
            swf.place_object2(1, MENUHOLDER_CHARACTER, name='MenuHolder'),
            swf.Tag(swf.TAG_SHOW_FRAME, b''),
            swf.Tag(swf.TAG_END, b'')]
    movie = swf.Swf(15, swf.pack_rect(0, STAGE_W * 20, 0, STAGE_H * 20),
                    256 * 30, 1, tags)
    return movie.serialize(compress=False)


def test_main_menu_injection_keeps_the_movie_and_moves_menuholder():
    from asset_convert import ui_main_menu
    movie = _mini_startmenu()
    out, report = ui_main_menu.patch_main_menu(
        movie, _dds_bgra(8, 8, (200, 180, 140, 255)),
        _dds_bgra(16, 4, (40, 30, 20, 255)))
    patched = swf.Swf.parse(out)

    ids = set(report['new_char_ids'])
    assert len(ids) == 6
    assert ids <= {t.character_id for t in patched.tags}

    depths = {struct.unpack_from('<H', t.data, 3)[0]:
              struct.unpack_from('<H', t.data, 1)[0]
              for t in patched.tags
              if t.code == swf.TAG_PLACE_OBJECT_2 and t.data[0] & 0x02}
    assert depths[ui_main_menu.MENUHOLDER_CHARACTER] == 3   # bumped behind ours
    bg_sprite, logo_sprite = report['new_char_ids'][4], report['new_char_ids'][5]
    assert depths[bg_sprite] == 1 and depths[logo_sprite] == 2

    # every original tag but the MenuHolder placement (depth) survives verbatim
    kept = {(t.code, bytes(t.data)) for t in patched.tags}
    orig = swf.Swf.parse(movie).tags
    survivors = sum((t.code, bytes(t.data)) in kept for t in orig)
    assert survivors == len(orig) - 1

    once = patched.serialize(compress=False)
    assert swf.Swf.parse(once).serialize(compress=False) == once


def test_place_move_flags_and_multiframe_sprite():
    move = swf.place_move(2, (5, 7), alpha=100)
    assert move.code == swf.TAG_PLACE_OBJECT_2
    assert move.data[0] & 0x01 and move.data[0] & 0x04 and move.data[0] & 0x08
    plain = swf.place_move(2, (5, 7))
    assert not plain.data[0] & 0x08                         # no cxform without alpha

    sp = swf.define_sprite_frames(700, [
        [swf.place_object2(1, 10)],
        [swf.place_move(1, (2, 3))],
        [swf.place_move(1, (0, 0), alpha=255), swf.do_action(swf.STOP_ACTION)]])
    cid, framecount = struct.unpack_from('<HH', sp.data, 0)
    assert cid == 700 and framecount == 3
