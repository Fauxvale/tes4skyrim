"""Oblivion's mouse cursor in Skyrim's menus.

Skyrim's `Interface/cursormenu.swf` draws the menu cursor as a SINGLE vector
shape -- character 1, a 42x42 shape placed at the movie origin. A cursor's
hotspot (the click point) is the movie's (0,0), and this shape's box is
x[0,42] y[0,42], so its TOP-LEFT CORNER is the hotspot -- exactly where a
pointer's tip belongs.

Oblivion draws its pointer from `textures/menus/misc/cursor.dds`: a 64x64
texture whose arrow art sits at the top-left with the tip in the corner (the
topmost opaque pixel is at x=1, the leftmost at y=2). Cropping to the art's
alpha bounds therefore puts the tip at the crop's top-left, and placing that
crop at the shape origin lands the tip on the hotspot.

So the port is the SAME one-shape swap the message-box frame uses (see
ui_menus / docs/ui_conversion.md): replace character 1 with a single
bitmap-filled shape of Oblivion's arrow, TOP-LEFT aligned rather than centered.
No ActionScript is touched, so the cursor tracks the mouse and hides in
gameplay exactly as vanilla -- the failure mode of a bad swap is "it looks
wrong", never "the cursor stopped moving".

This only affects the MENU cursor (inventory, map, message boxes, favorites);
there is no cursor during normal gameplay.
"""

from . import swf
from .ui_menus import to_image, premultiplied_argb

# Oblivion's pointer art.
CURSOR_TEXTURE_DIR = r'textures\menus\misc'
CURSOR_TEXTURE = 'cursor.dds'
CURSOR_TEXTURES = (CURSOR_TEXTURE,)

# The character the cursor shape is. Re-checked before it is touched, so a
# Skyrim update that renumbers it fails loudly rather than scrambling the movie.
CURSOR_SHAPE_ID = 1

# The box vanilla's cursor shape draws in, in pixels. The hotspot is its
# top-left corner (0, 0); the art extends down and right from there.
EXPECTED_BOX = (0.0, 0.0, 42.0, 42.0)
BOX_TOLERANCE = 2.0

# Height of the replacement art inside the cursor shape, in pixels.
#
# 🛑 This is NOT the on-screen size. cursormenu.swf's root places the cursor
# sprite at scale ~0.63 (measured), so whatever goes in the 42 px shape renders
# at ~0.63x. Skyrim's own 42 px arrow thus draws at ~26 px. Filling the whole
# 42 px box with Oblivion's chunkier pointer looked noticeably larger than
# Skyrim's thin one at the same box height, so the art is sized DOWN within the
# shape: 30 px here -> ~19 px on screen, which matches Oblivion's on-screen
# weight. Oblivion's aspect is preserved. Raise or lower this one number to
# taste; nothing else depends on it.
CURSOR_HEIGHT = 30

SHAPE_TAGS = (2, 22, 32, 83)


class CursorConvertError(RuntimeError):
    """cursormenu.swf is not the movie this reskin was written for."""


def _shape_box(tag: swf.Tag) -> tuple:
    """(x0, y0, x1, y1) of a DefineShape* in PIXELS."""
    if tag.code not in SHAPE_TAGS:
        raise CursorConvertError(f'character is a tag-{tag.code}, not a shape')
    reader = swf.BitReader(tag.data, 2)
    nbits = reader.ub(5)
    x0, x1, y0, y1 = (reader.sb(nbits) for _ in range(4))
    return (x0 / swf.TWIPS, y0 / swf.TWIPS,
            x1 / swf.TWIPS, y1 / swf.TWIPS)


def compose_cursor(texture: bytes, height: int = CURSOR_HEIGHT):
    """Oblivion's arrow, cropped to its art and scaled to `height`. -> image.

    The crop is to the ALPHA bounds so the tip sits at the image's top-left,
    which is what puts it on the hotspot when the image is placed at the shape
    origin. Oblivion's aspect is kept, so the pointer is not distorted.
    """
    from PIL import Image
    img = to_image(texture)
    bbox = img.getchannel('A').getbbox()          # the opaque arrow
    if bbox is None:
        raise CursorConvertError('cursor.dds is fully transparent')
    arrow = img.crop(bbox)
    width = max(1, round(height * arrow.width / arrow.height))
    return arrow.resize((width, height), Image.LANCZOS)


def patch_cursor(movie_bytes: bytes, texture: bytes) -> tuple:
    """Reskin Skyrim's menu cursor with Oblivion's. -> (bytes, report)

    `texture` is the DDS bytes of Oblivion's cursor.dds.
    """
    movie = swf.Swf.parse(movie_bytes)

    # -- refuse a movie that is not the one measured.
    try:
        index = movie.index_of_character(CURSOR_SHAPE_ID)
    except KeyError:
        raise CursorConvertError(
            f'cursormenu.swf has no character {CURSOR_SHAPE_ID}; this is not '
            f'the movie the cursor reskin was written against')
    got = _shape_box(movie.tags[index])
    drift = max(abs(a - b) for a, b in zip(got, EXPECTED_BOX))
    if drift > BOX_TOLERANCE:
        raise CursorConvertError(
            f'cursor shape draws in ({got[0]:.1f}, {got[1]:.1f})-'
            f'({got[2]:.1f}, {got[3]:.1f}), expected '
            f'({EXPECTED_BOX[0]:.0f}, {EXPECTED_BOX[1]:.0f})-'
            f'({EXPECTED_BOX[2]:.0f}, {EXPECTED_BOX[3]:.0f}) -- the cursor movie '
            f'has changed')

    arrow = compose_cursor(texture)
    next_id = movie.next_character_id()

    # One bitmap, one fill, TOP-LEFT aligned at the origin so the arrow tip lands
    # on the hotspot -- the only shape form this engine renders (ui_conversion).
    movie.tags[index:index] = [swf.define_bits_lossless2(
        next_id, arrow.width, arrow.height, premultiplied_argb(arrow))]
    movie.tags[index + 1] = swf.define_shape3_bitmap_rects(
        CURSOR_SHAPE_ID,
        [(next_id, 0, 0, arrow.width, arrow.height,
          (arrow.width, arrow.height))])

    report = {
        'shape': CURSOR_SHAPE_ID,
        'size': f'{arrow.width}x{arrow.height}',
        'note': 'tip at the hotspot (0, 0); no AS2 touched',
    }
    return movie.serialize(compress=True), report
