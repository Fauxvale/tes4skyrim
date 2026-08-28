"""Tests for the menu-cursor reskin (asset_convert/ui_cursor.py).

Hermetic: the movie and the texture are synthesised here, so nothing needs
Oblivion or Skyrim installed. The synthetic movie carries the SAME character id
and box as vanilla cursormenu.swf, because that is what `patch_cursor` checks
before it writes anything.
"""

import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from asset_convert import swf
from asset_convert import ui_cursor


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _arrow_dds(width=64, height=64, art_w=33, art_h=34):
    """An uncompressed BGRA32 DDS with an opaque triangle in the TOP-LEFT.

    The opaque region's tip is at (0, 0) and it fills a right triangle down to
    (art_w, art_h), so `getbbox()` returns (0, 0, art_w, art_h) and the crop
    keeps the tip at the top-left -- exactly the shape of a pointer.
    """
    header = bytearray(128)
    header[0:4] = b'DDS '
    struct.pack_into('<I', header, 4, 124)
    struct.pack_into('<I', header, 8, 0x0002100F)
    struct.pack_into('<II', header, 12, height, width)
    struct.pack_into('<I', header, 20, width * 4)
    struct.pack_into('<I', header, 28, 1)
    struct.pack_into('<I', header, 76, 32)
    struct.pack_into('<I', header, 80, 0x41)
    struct.pack_into('<I', header, 88, 32)
    struct.pack_into('<IIII', header, 92, 0x00FF0000, 0x0000FF00,
                     0x000000FF, 0xFF000000)
    struct.pack_into('<I', header, 108, 0x1000)
    body = bytearray()
    for y in range(height):
        for x in range(width):
            opaque = x < art_w and y < art_h and x <= (art_w * (y + 1)) // art_h
            body += bytes([0, 210, 255, 255]) if opaque else bytes([0, 0, 0, 0])
    return bytes(header) + bytes(body)


@pytest.fixture
def texture():
    return _arrow_dds()


def _cursor_shape(character_id, box):
    """A solid shape occupying `box` = (x0, y0, x1, y1), as vanilla's char 1."""
    x0, y0, x1, y1 = box
    return swf.define_shape3_solid_rects(
        character_id, [(x0, y0, x1 - x0, y1 - y0)], (0, 0, 0, 204))


def _cursor_movie(box=ui_cursor.EXPECTED_BOX):
    """A movie shaped like cursormenu.swf: char 1 shape placed in a sprite,
    plus a DoAction so the AS2-untouched assertion has something to check."""
    tags = [
        _cursor_shape(ui_cursor.CURSOR_SHAPE_ID, box),
        swf.define_sprite(2, [swf.place_object2(1, ui_cursor.CURSOR_SHAPE_ID)]),
        swf.do_action(bytes([0x81, 0x02, 0x00, 0x00, 0x00])),   # GotoFrame 0
        swf.place_object2(1, 2),
        swf.Tag(swf.TAG_SHOW_FRAME, b''),
        swf.Tag(swf.TAG_END, b''),
    ]
    frame_size = swf.pack_rect(0, 42 * swf.TWIPS, 0, 42 * swf.TWIPS)
    return swf.Swf(15, frame_size, 24 * 256, 1, tags)


@pytest.fixture
def movie_bytes():
    return _cursor_movie().serialize()


# ---------------------------------------------------------------------------
# composing the cursor
# ---------------------------------------------------------------------------

def test_compose_crops_to_the_arrow_and_keeps_the_tip_at_top_left(texture):
    """🛑 The click hotspot is the shape origin, so the arrow tip has to land at
    the image's top-left. Cropping to the alpha bounds is what guarantees it."""
    cur = ui_cursor.compose_cursor(texture, height=42)
    assert cur.getpixel((0, 0))[3] > 0, 'the tip is not at the top-left'


def test_compose_preserves_aspect(texture):
    """The pointer must not be distorted."""
    cur = ui_cursor.compose_cursor(texture, height=42)
    assert cur.height == 42
    assert cur.width == max(1, round(42 * 33 / 34))     # 33x34 art


def test_compose_refuses_a_transparent_texture():
    blank = _arrow_dds(art_w=0, art_h=0)
    with pytest.raises(ui_cursor.CursorConvertError):
        ui_cursor.compose_cursor(blank)


# ---------------------------------------------------------------------------
# patching the movie
# ---------------------------------------------------------------------------

def test_patch_replaces_the_cursor_with_one_bitmap_fill(movie_bytes, texture):
    out, report = ui_cursor.patch_cursor(movie_bytes, texture)
    movie = swf.Swf.parse(out)
    tag = movie.tags[movie.index_of_character(ui_cursor.CURSOR_SHAPE_ID)]
    assert _bitmap_fill_count(tag.data) == 1
    assert report['shape'] == ui_cursor.CURSOR_SHAPE_ID


def test_patch_puts_the_hotspot_at_the_origin(movie_bytes, texture):
    """🛑 The replacement's box top-left is (0, 0) -- the movie origin, which is
    the cursor's click point. The vanilla shape's placement is untouched, so
    wherever char 1's origin is IS the hotspot."""
    out, _ = ui_cursor.patch_cursor(movie_bytes, texture)
    movie = swf.Swf.parse(out)
    box = ui_cursor._shape_box(movie.tags[movie.index_of_character(1)])
    assert abs(box[0]) < 0.05 and abs(box[1]) < 0.05, \
        f'hotspot moved off the origin: {box}'


def test_patch_touches_nothing_but_the_cursor_shape(movie_bytes, texture):
    """No AS2, no timeline, no other character -- the same discipline the
    message box holds, so the cursor still tracks and hides as vanilla."""
    out, _ = ui_cursor.patch_cursor(movie_bytes, texture)
    before = swf.Swf.parse(movie_bytes)
    after = swf.Swf.parse(out)
    before_as2 = [t.data for t in before.tags if t.code in (12, 59)]
    after_as2 = [t.data for t in after.tags if t.code in (12, 59)]
    assert before_as2 == after_as2, 'an AS2 block changed'
    assert (before.tags[before.index_of_character(2)].data
            == after.tags[after.index_of_character(2)].data)


def test_patch_adds_exactly_one_bitmap(movie_bytes, texture):
    out, _ = ui_cursor.patch_cursor(movie_bytes, texture)
    before = swf.Swf.parse(movie_bytes)
    after = swf.Swf.parse(out)
    before_chars = {t.character_id for t in before.tags
                    if t.character_id is not None}
    after_chars = {t.character_id for t in after.tags
                   if t.character_id is not None}
    assert len(after_chars) - len(before_chars) == 1


def test_refuses_a_movie_whose_cursor_is_the_wrong_size(texture):
    movie = _cursor_movie(box=(0, 0, 200, 200))
    with pytest.raises(ui_cursor.CursorConvertError):
        ui_cursor.patch_cursor(movie.serialize(), texture)


def test_refuses_a_movie_with_no_cursor_character(texture):
    movie = _cursor_movie()
    del movie.tags[movie.index_of_character(ui_cursor.CURSOR_SHAPE_ID)]
    with pytest.raises(ui_cursor.CursorConvertError):
        ui_cursor.patch_cursor(movie.serialize(), texture)


def test_round_trip_is_byte_identical(movie_bytes):
    once = swf.Swf.parse(movie_bytes).serialize(compress=False)
    assert swf.Swf.parse(once).serialize(compress=False) == once


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _Bits:
    def __init__(self, data, offset=0):
        self.data, self.offset, self.bit = data, offset, 0

    def u(self, n):
        value = 0
        for _ in range(n):
            value = (value << 1) | ((self.data[self.offset] >> (7 - self.bit)) & 1)
            self.bit += 1
            if self.bit == 8:
                self.bit, self.offset = 0, self.offset + 1
        return value

    def s(self, n):
        v = self.u(n)
        return v - (1 << n) if n and v & (1 << (n - 1)) else v

    def align(self):
        if self.bit:
            self.bit, self.offset = 0, self.offset + 1


def _bitmap_fill_count(shape_data):
    bits = _Bits(shape_data, 2)
    n = bits.u(5)
    for _ in range(4):
        bits.s(n)
    bits.align()
    at = bits.offset
    count = shape_data[at]
    at += 1
    found = 0
    for _ in range(count):
        if shape_data[at] in (0x40, 0x41, 0x42, 0x43):
            found += 1
        at += 1 + 2
        b = _Bits(shape_data, at)
        if b.u(1):
            nb = b.u(5); b.s(nb); b.s(nb)
        if b.u(1):
            nb = b.u(5); b.s(nb); b.s(nb)
        nb = b.u(5); b.s(nb); b.s(nb)
        b.align()
        at = b.offset
    return found
