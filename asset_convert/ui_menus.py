"""Oblivion menu art -> Skyrim Scaleform menus. Message boxes only, for now.

Oblivion draws its UI from XML "tiles" (`Data/Menus/*.xml`) over DDS art;
Skyrim drives Scaleform GFx movies (`Interface/*.swf`) whose ActionScript 2
classes are called from C++ by name.  There is no translation between the two
technologies, so nothing here converts a menu in the sense the rest of the
pipeline converts a record.  What it does instead is narrower and safe:

    RESKIN the vanilla movie in place, changing only character definitions.

Every AS2 class, every GameDelegate callback and the whole timeline are copied
through byte-for-byte (`asset_convert.swf` round-trips an untouched movie
byte-identically, which `tests/test_ui_convert.py` asserts).  The engine's
interface contract with the movie therefore cannot drift -- the failure mode of
a bad reskin is "it looks wrong", never "the menu stopped responding".

WHY THE MESSAGE BOX IS THE ONE THAT PORTS CLEANLY
-------------------------------------------------
Both engines build this menu the same way: a 9-slice frame around a text block
with a stack of choices under it.

  * Oblivion (`menus/message_menu.xml` + `menus/prefabs/generic_background.xml`)
    draws a `_border_thickness`-thick border from three textures -- corners,
    a horizontal edge, a vertical edge -- around a stretched center, and the
    box is `user0` wide.  Its buttons are PLAIN CENTERED TEXT: the button image
    is `shared_empty.dds` at `alpha 0`, so there is no button art to port.
  * Skyrim's `messagebox.swf` has a `Background_mc` clip (character 11, holding
    shape 10) that `MessageBox.PositionElements` sizes to fit the text and
    buttons.

So the port is: compose Oblivion's whole frame -- center and border -- into ONE
bitmap on `Background_mc`'s shape, which the AS2 already sizes to the panel,
and set the layout margins from Oblivion's authored insets.  The selection
arrows become Oblivion's focus box and the text takes Oblivion's authored
color.

🛑 ONE BITMAP, ONE FILL.  Five in-game rounds narrowed the render path to a
single rule: this engine draws a shape's FIRST bitmap fill across the whole
shape and ignores the rest, and it did not draw characters this conversion
ADDS at all.  So the whole frame -- center and border -- is composed offline
into ONE bitmap on the shape vanilla already sizes, rather than nine rects or
separate clips.

The border is then 9-SLICED: `Background_mc` already carries a
`DefineScalingGrid`, and re-cutting that grid to our 44 px border keeps the
corners fixed while the middle stretches, so the carving holds its density at
any panel size.  (An earlier rule here claimed the engine will not 9-slice a
bitmap fill "0 of 353 shapes"; that census compared scaling grids, which name a
SPRITE, against fills, which live on a SHAPE -- disjoint kinds, so the zero was
arithmetic, not evidence.  Vanilla 9-slices bitmap art in magic/container/craft
menus.)  Full evidence, including what that costs: docs/ui_conversion.md.

WHAT IS NOT PORTED (and why)
----------------------------
* **Fonts.** Skyrim's message text imports `$EverywhereMediumFont` from
  `gfxfontlib.swf`, which is shared by EVERY menu -- replacing it is a
  whole-UI change, not a message-box one.  Oblivion's own faces are `.fnt`
  bitmap fonts, which do not scale and would have to be revectorised.  The
  text therefore stays in Skyrim's face; the frame is the dominant visual
  signature and it does port.
* **The `60` fallback margin** in `PositionElements` is patched (see
  `_LITERAL_PATCHES`), but any other unnamed literal in the AS2 is left alone.
* **Oblivion's fixed 700 px box width.** Skyrim sizes the panel to its content;
  pinning a minimum would mean recompiling AS2, not patching a literal.
"""

import re
import struct

from . import swf

# Oblivion's message box, as authored. These are FALLBACKS only -- the real
# values are read from the plugin's own menu XML by `read_oblivion_layout` and
# these are what a missing/edited file degrades to. Recorded here so a
# hand-inspection of the output has something to check against.
VANILLA_LAYOUT = {
    'border': 44,          # generic_background _border_thickness
    # INFORMATIONAL, not applied: Oblivion fixes the box at this width
    # whatever the text says, and Skyrim's panel grows to fit it instead.
    # Reported so the difference is visible; see ui_conversion.md.
    'box_width': 700,      # message_menu background user0
    'text_inset': 12,      # half of the wrapwidth <sub> (24)
    'text_top': 15,        # message_text y
    'button_spacer': 30,   # button_1 y offset from the message text
}

# Oblivion menu files this reads. Data-relative, as stored in the BSAs.
MESSAGE_MENU_XML = r'menus\message_menu.xml'
GENERIC_BACKGROUND_XML = r'menus\prefabs\generic_background.xml'

# The generic-background art, and which part of each texture each slice is.
# `crop` is (x, y, w, h) in SOURCE pixels; Oblivion packs the four corners as a
# 2x2 grid of 64px tiles and both edge textures as two 64px-wide strips, which
# is exactly what generic_background.xml's cropx/cropy values select.
BACKGROUND_TEXTURE_DIR = r'textures\menus\genericbackground'
_CORNERS = 'edge_corners.dds'
_HORIZONTAL = 'edge_horizontal.dds'
_VERTICAL = 'edge_vertical.dds'
_CENTER = 'center_background.dds'
BACKGROUND_TEXTURES = (_CORNERS, _HORIZONTAL, _VERTICAL, _CENTER)

# The focus box Oblivion rings the selected choice with
# (menus/prefabs/focus_box.xml, included by message_menu.xml as
# `message_focus_box`). Nine pieces around a tiled center, and -- like the
# generic background -- each is drawn at its AUTHORED size as a 1:1 crop from
# the top-left of its texture, which the alpha bounds confirm exactly
# (focus_top's art is 9 px of a 16 px texture, focus_right's 12 of 16, ...).
FOCUS_TEXTURE_DIR = r'textures\menus\focus'
FOCUS_TEXTURES = ('focus_center_x4.dds', 'focus_top.dds', 'focus_bottom.dds',
                  'focus_left.dds', 'focus_right.dds', 'focus_top_left.dds',
                  'focus_top_right.dds', 'focus_bottom_left.dds',
                  'focus_bottom_right.dds')

# focus_box.xml's authored geometry, as (x, y, w, h) relative to the CONTENT
# box, where `W`/`H` stand for the content's width and height. The borders are
# deliberately asymmetric -- Oblivion hangs a soft shadow below and to the
# right -- so the content is what gets centered, not the image.
FOCUS_EDGE_LEFT, FOCUS_EDGE_TOP = 8, 9
FOCUS_EDGE_RIGHT, FOCUS_EDGE_BOTTOM = 12, 14

# Content size of the composed box. The AS2 sets only `_width`, to
# `ButtonText._width + SELECTION_INDICATOR_WIDTH`, so the authored HEIGHT
# survives while the width scales -- and the scale factor is what thickens the
# vertical edges. A content width near real label widths keeps that near 1.
FOCUS_CONTENT_W = 180
FOCUS_CONTENT_H = 17

# The composed frame's base size, as the CENTER's dimensions -- the full shape
# is this plus a border on each side.
#
# Since the scaling grid is re-cut to the border (see patch_message_box), the
# frame NO LONGER rides Background_mc's uniform scale -- the border draws at a
# constant 44 px whatever the panel size, so it can no longer outgrow the
# margins, and the base size is now a quality choice (blur/aspect) rather than
# the layout constraint it used to be. The note below is the history that set
# these numbers.
#
# 🛑 (HISTORICAL, pre-scale9) THE BASE MUST BE BIG ENOUGH THAT THE BORDER NEVER
# OUTGROWS THE MARGINS. The frame rode Background_mc's uniform scale, so a panel
# taller than the base drew the border THICKER than its authored 44 px, while
# the margins that keep text clear of it are fixed pixel values. Measured in
# game: a ten-choice menu is 762 px tall, which against a 488 px base scaled the
# border to 69 px against a HEIGHT_MARGIN of 59 -- and the last choice rendered
# on top
# of the border. A four-choice box scaled to 42 px and was fine, which is why
# it only ever went wrong with many options.
#
# The invariant is therefore
#
#     border * panel / base  <  margin        on each axis
#
# so base > border/margin * panel: 0.75x the tallest panel and 0.79x the
# widest. These values clear a 900x700 panel, well past anything observed.
#
# Supersampling was the obvious alternative and is the wrong trade: doubling
# the pixels quadruples the file to fix blur, while sizing the base fixes blur,
# aspect AND this overlap for a fraction of that.
# The panel sizes the base is sized to cover. Ten choices measured 762 px tall
# in game before the MessageText fix and about 707 after, so this is generous
# headroom rather than a guess -- and `tests/test_ui_convert.py` asserts the
# invariant holds across it.
MAX_PANEL_W = 700
MAX_PANEL_H = 1040

CENTER_BASE_W = 472         # -> 560 px base, covers 700 px wide
CENTER_BASE_H = 712         # -> 800 px base, covers 1072 px tall

# The center is composed at 1/N of the frame's resolution and scaled back up.
#
# It is the only slice with photographic detail, so it dominates the file --
# and it is also the slice Oblivion itself stretches hardest (a 1024x1024
# texture across the whole box), so softening it is faithful rather than a
# compromise. The border, which is what the eye reads, keeps full detail.
# Measured on the 560x800 base: 550 KB at 1/6, against ~1 MB at full detail.
CENTER_DETAIL_DIVISOR = 6

# Character IDs in vanilla messagebox.swf. Verified against the shipped file;
# `patch_message_box` re-checks each one before touching it and refuses to
# write if the movie does not match, so a future Skyrim patch that renumbers
# them fails loudly instead of producing a broken menu.
BACKGROUND_SHAPE_ID = 10        # the panel art, inside...
BACKGROUND_SPRITE_ID = 11       # ...this clip, which is named Background_mc
DIVIDER_SHAPE_ID = 12           # the hairline between message and buttons
SELECTION_ARROW_SHAPE_ID = 4    # the >< arrows flanking the focused choice
# `DiamondMarker` (character 1) is EXPORTED but never placed in this movie --
# the marker actually on screen is the vector shape above, reached through
# MessageBoxButton -> character 6 (`SelectionIndicator`) -> 5 -> 4. Blanking
# the bitmap therefore changed nothing, which is what the second in-game round
# showed.
DIAMOND_MARKER_ID = 1

# The message and button text fields. Color lives in each DefineEditText's
# TextColor, and the AS2 reads its DefaultTextFormat back off the field
# (`getTextFormat`), so recoloring the record recolors everything drawn from
# it. Vanilla is white: (255,255,255) at alpha 255 for the message and 204 for
# the choices.
MESSAGE_TEXT_ID = 14
BUTTON_TEXT_ID = 8

# What the message field's authored height is shrunk to (see
# set_edit_text_height). Vanilla authors 121 px for a single 22 px line, and
# every message box pays for it as dead space between the header and the
# choices. Three lines of headroom, and WordWrap/Multiline still let a longer
# message grow past it.
MESSAGE_TEXT_HEIGHT = 66

# Oblivion's menu text color, and not a guess: (117, 59, 33) is the single
# most-authored RGB triple across Oblivion's own menu XML, appearing 226 times
# -- more than five times the next most common. message_menu.xml itself sets no
# color, so this is the house default it inherits.
OBLIVION_TEXT_RGB = (117, 59, 33)

# `color="#rrggbb"` inside a DefineEditText's initial HTML. See
# recolor_edit_text for why that has to change as well as TextColor.
_HTML_COLOR_RE = re.compile(r'(color\s*=\s*")(#[0-9a-fA-F]{6})(")')

# AS2 layout constants in __Packages.MessageBox, and how each is derived from
# Oblivion's authored layout. Skyrim measures every one of these from the PANEL
# EDGE inward, and Oblivion's insets are measured from inside its border, so
# each margin is the border thickness plus Oblivion's own inset.
#
# Verified against the disassembled PositionElements:
#     Background_mc._width  = max(widestLine + 60, buttons._width + WIDTH_MARGIN*2)
#     Background_mc._height = Message._height + buttons._height
#                             + HEIGHT_MARGIN*2 + MESSAGE_TO_BUTTON_SPACER
#     Message._y            = -height/2 + HEIGHT_MARGIN
def _derived_constants(layout: dict) -> dict:
    border = layout['border']
    width_margin = border + layout['text_inset']
    return {
        'WIDTH_MARGIN': width_margin,
        # border + Oblivion's inset cleared the border for the TEXT. It does
        # not clear it for the SELECTED option's FOCUS BOX. The indicator is
        # placed at the button center and only its _width is set, so its native
        # bottom edge hangs `FOCUS_CONTENT_H/2 + FOCUS_EDGE_BOTTOM` px below the
        # center -- past the glyphs it frames. Once scale9 made the border a
        # constant 44 px (no longer scaling thin on small panels), that overhang
        # started landing on the border for the LAST option.
        #
        # 🛑 Worst case: the last button's center sits right at the container's
        # bottom edge, so the WHOLE overhang hangs below the buttons. Sizing the
        # margin to clear that -- border + overhang + a gap -- holds at any panel
        # size and option count, because under scale9 the border is constant, so
        # this is one number rather than the old panel-dependent invariant. The
        # gap reuses Oblivion's own text inset. (Overhang derivation matches the
        # offset compose_focus_box returns.)
        'HEIGHT_MARGIN': (border + FOCUS_CONTENT_H // 2 + FOCUS_EDGE_BOTTOM
                          + layout['text_top']),
        # 🛑 NOT Oblivion's authored 30. That value assumes Oblivion's message
        # tile, which is exactly as tall as its text. Skyrim's MessageText
        # reports ~120 px at runtime for a single 22 px line whatever its
        # authored bounds say (changing them 121 -> 66 left the panel height
        # identical at 762 px), so the field already contributes ~95 px of
        # empty space below the header. Adding Oblivion's spacer on top of
        # that only widens a gap Oblivion never had.
        'MESSAGE_TO_BUTTON_SPACER': 0,
    }


class UiConvertError(RuntimeError):
    """The input movie or menu art is not what the patch was written for."""


# ---------------------------------------------------------------------------
# Reading Oblivion's authored layout
# ---------------------------------------------------------------------------

def _trait(source: str, name: str):
    """The literal number in `<name> 44 </name>`, or None.

    Only a bare literal counts. A trait whose body is a `<copy>`/`<add>` chain
    is computed by Oblivion's tile engine from other traits, and guessing at
    the result is exactly the heuristic this should not do -- the caller falls
    back to the vanilla value and says so.
    """
    m = re.search(rf'<{re.escape(name)}>\s*(-?\d+(?:\.\d+)?)\s*</{re.escape(name)}>',
                  source, re.IGNORECASE)
    return float(m.group(1)) if m else None


def _block(source: str, tag: str, name: str):
    """The text of the `<tag name="name"> ... </tag>` element, or None.

    Oblivion's menu XML is not well-formed (undeclared entities like
    `&true;`, prefab fragments with no root), so a real parser cannot open it.
    This walks tag depth over the raw text instead, which is enough for the
    flat, hand-authored nesting these files use.
    """
    open_re = re.compile(rf'<{tag}\s+name\s*=\s*"{re.escape(name)}"\s*>',
                         re.IGNORECASE)
    m = open_re.search(source)
    if not m:
        return None
    depth = 1
    pos = m.end()
    step = re.compile(rf'<(/?){tag}\b[^>]*>', re.IGNORECASE)
    while depth:
        m2 = step.search(source, pos)
        if not m2:
            return None
        depth += -1 if m2.group(1) else 1
        pos = m2.end()
    return source[m.end():pos]


def read_oblivion_layout(message_menu_xml: str,
                         generic_background_xml: str) -> tuple:
    """(layout dict, [warnings]) from Oblivion's own menu XML.

    Every value that can be read is read; anything absent falls back to
    VANILLA_LAYOUT with a warning naming it, so a user running this against an
    edited UI (DarNified and friends rewrite these files wholesale) gets their
    numbers where they are legible and vanilla's where they are not.
    """
    layout = dict(VANILLA_LAYOUT)
    warnings = []

    def take(key, value, what):
        if value is None:
            warnings.append(f'{what}: not a literal, using {layout[key]}')
        else:
            layout[key] = value

    take('border', _trait(generic_background_xml, '_border_thickness'),
         'generic_background _border_thickness')

    background = _block(message_menu_xml, 'rect', 'background') or ''
    # user0 is the box width; it sits before the first nested element, so read
    # it from the head of the block rather than the whole thing (every button
    # has traits of its own further down).
    head = background.split('<text', 1)[0]
    take('box_width', _trait(head, 'user0'), 'message_menu background user0')

    text = _block(background, 'text', 'message_text') or ''
    take('text_top', _trait(text, 'y'), 'message_text y')

    # wrapwidth is `<copy src="background" trait="user0"/><sub> 24 </sub>` --
    # the SUB is the TOTAL horizontal inset, so half of it is the per-side
    # margin the text actually sits at.
    wrap = re.search(r'<wrapwidth>(.*?)</wrapwidth>', text, re.S | re.I)
    sub = _trait(wrap.group(1), 'sub') if wrap else None
    if sub is None:
        warnings.append('message_text wrapwidth <sub>: not a literal, '
                        f"using {layout['text_inset']}")
    else:
        layout['text_inset'] = sub / 2

    # button_1's y is `<copy src="message_text" trait="height"/><add> 30 </add>`
    # -- the ADD is the gap between the message and the first choice.
    button1 = _block(background, 'image', 'button_1') or ''
    y_block = re.search(r'<y>(.*?)</y>', button1, re.S | re.I)
    take('button_spacer', _trait(y_block.group(1), 'add') if y_block else None,
         'button_1 y <add>')

    return layout, warnings


# ---------------------------------------------------------------------------
# Building the frame bitmaps
# ---------------------------------------------------------------------------

def to_image(dds_bytes: bytes):
    """A Pillow RGBA image from DDS bytes (DXT1/3/5 or uncompressed BGRA32)."""
    from PIL import Image
    from .flipbook import _read_dds_header, _decode_dxt

    w, h, pf_flags, fourcc, rgb_bits = _read_dds_header(dds_bytes)
    payload = dds_bytes[128:]
    if pf_flags & 0x4:
        bgra = _decode_dxt(payload, w, h, fourcc.decode())
    elif pf_flags & 0x40 and rgb_bits == 32:
        bgra = bytearray(payload[:w * h * 4])
    else:
        raise UiConvertError(f'unsupported DDS pixel format (fourcc {fourcc!r})')
    # _decode_dxt writes B,G,R,A; Pillow's 'RGBA' wants R,G,B,A.
    img = Image.frombytes('RGBA', (w, h), bytes(bgra))
    b, g, r, a = img.split()
    return Image.merge('RGBA', (r, g, b, a))


def _slice(img, crop, size):
    """Crop `img` to (x, y, w, h) and resample to (w, h) `size`."""
    from PIL import Image
    x, y, w, h = crop
    piece = img.crop((x, y, x + w, y + h))
    if piece.size != tuple(size):
        piece = piece.resize(tuple(size), Image.LANCZOS)
    return piece


def premultiplied_argb(img) -> bytes:
    """Pillow RGBA -> the premultiplied ARGB byte order DefineBitsLossless2
    format 5 requires (see swf.define_bits_lossless2)."""
    import numpy as np
    a = np.asarray(img, dtype=np.uint16)          # H x W x 4, RGBA
    alpha = a[:, :, 3:4]
    rgb = (a[:, :, :3] * alpha + 127) // 255
    out = np.concatenate([alpha, rgb], axis=2).astype(np.uint8)  # ARGB
    return out.tobytes()


def build_frame_slices(textures: dict, border: int) -> list:
    """The nine frame slices as (name, PIL image), in shape order.

    `textures` maps the BACKGROUND_TEXTURES filenames to their DDS bytes.

    Crops mirror generic_background.xml, where `cropx`/`cropy` name a SOURCE
    PIXEL OFFSET and the region taken is `_border_thickness` px across --
    **1:1, not the whole tile resampled**.  The distinction is the difference
    between a tight frame and a broken one, and the textures settle it: the
    art inside each 64 px tile is anchored at the tile origin and runs 45-52
    px, i.e. the border thickness, with the remainder transparent padding.
    Reading the tile as 64 px and resampling to 44 shrinks the art to ~31 px
    and opens a visible gap between the border and the parchment on all four
    sides (rendered both ways to confirm).

    Only the axis a piece is STRETCHED along is resampled: the edges run the
    length of the box, and the center fills it.
    """
    corners = to_image(textures[_CORNERS])
    horizontal = to_image(textures[_HORIZONTAL])
    vertical = to_image(textures[_VERTICAL])
    center = to_image(textures[_CENTER])

    # Second-tile offsets. Each of these textures is exactly two tiles, so the
    # stride is half the packed dimension -- which is also the ceiling on how
    # thick a border can be cut before it runs into its neighbour.
    ctile = corners.width // 2                 # 64 in vanilla
    hstrip = horizontal.height // 2            # 64
    vstrip = vertical.width // 2               # 64

    b = min(int(border), ctile, hstrip, vstrip)
    cw, ch = CENTER_BASE_W, CENTER_BASE_H
    hw, vh = horizontal.width, vertical.height
    # 🛑 THE EDGES KEEP THEIR NATIVE LENGTH; compose_frame TILES them.
    #
    # generic_background.xml stretches its edges (no `<tile>` trait anywhere in
    # it), and copying that here squashes the carving far harder than Oblivion
    # ever does. Oblivion can afford to stretch because its box is a FIXED
    # 700 px wide, so a 1024 px motif compresses to 0.68x and that mild squeeze
    # IS the look. Ours compresses twice -- once composing 1024 into the base,
    # then again when the base scales to the panel -- which measured 0.35x on a
    # 422 px panel, about half Oblivion's density, and reads as the border
    # being squished between the corners.
    #
    # Returning the edges at source length and tiling them into the band keeps
    # the motif at its own scale, so the only squeeze left is the panel's. This
    # deliberately matches Oblivion's APPEARANCE rather than its mechanism,
    # because our geometry is not its geometry.
    #
    # The center still stretches: it is a soft parchment wash and Oblivion
    # `zoom`s it across the whole box, so stretching is right there.
    return [
        ('top_left', _slice(corners, (0, 0, b, b), (b, b))),
        ('top', _slice(horizontal, (0, 0, hw, b), (hw, b))),
        ('top_right', _slice(corners, (ctile, 0, b, b), (b, b))),
        ('left', _slice(vertical, (0, 0, b, vh), (b, vh))),
        ('center', _slice(center, (0, 0, center.width, center.height), (cw, ch))),
        ('right', _slice(vertical, (vstrip, 0, b, vh), (b, vh))),
        ('bottom_left', _slice(corners, (0, ctile, b, b), (b, b))),
        ('bottom', _slice(horizontal, (0, hstrip, hw, b), (hw, b))),
        ('bottom_right', _slice(corners, (ctile, ctile, b, b), (b, b))),
    ]


# ---------------------------------------------------------------------------
# AS2 literal patching
# ---------------------------------------------------------------------------
#
# The layout constants are initialised in __Packages.MessageBox as a
# three-item ActionPush -- (register, constant-pool name, INTEGER literal) --
# immediately before a SetMember. Push type 7 is a fixed 4-byte signed int, so
# the replacement is written OVER the original: the action keeps its length,
# every jump offset in the block stays valid, and nothing else can shift.
#
# Anything less surgical than this would mean recompiling AS2, which this
# module deliberately cannot do.

_PUSH = 0x96
_PUSH_INT = 7
_PUSH_CONST8 = 8
_PUSH_CONST16 = 9
_PUSH_REGISTER = 4
_PUSH_BOOLEAN = 5
_CONSTANT_POOL = 0x88


def _constant_pool(block: bytes) -> list:
    """The first ConstantPool string list in an action block."""
    p = 0
    while p < len(block):
        code = block[p]
        if code < 0x80:
            p += 1
            continue
        if p + 3 > len(block):
            break
        (length,) = struct.unpack_from('<H', block, p + 1)
        body = p + 3
        if code == _CONSTANT_POOL:
            (count,) = struct.unpack_from('<H', block, body)
            q = body + 2
            out = []
            for _ in range(count):
                end = block.index(b'\x00', q)
                out.append(block[q:end].decode('latin1'))
                q = end + 1
            return out
        p = body + length
    return []


def _iter_pushes(block: bytes):
    """(offset, length, [(type, value_offset, value)]) for each ActionPush."""
    p = 0
    while p < len(block):
        code = block[p]
        if code < 0x80:
            p += 1
            continue
        if p + 3 > len(block):
            break
        (length,) = struct.unpack_from('<H', block, p + 1)
        body = p + 3
        end = body + length
        if code == _PUSH:
            items = []
            q = body
            while q < end:
                t = block[q]
                q += 1
                if t == 0:
                    e = block.index(b'\x00', q)
                    items.append((t, q, block[q:e].decode('latin1')))
                    q = e + 1
                elif t == 1:
                    items.append((t, q, struct.unpack_from('<f', block, q)[0]))
                    q += 4
                elif t in (2, 3):
                    items.append((t, q, None))
                elif t in (4, 5, _PUSH_CONST8):
                    items.append((t, q, block[q]))
                    q += 1
                elif t == 6:
                    items.append((t, q, struct.unpack_from('<d', block, q)[0]))
                    q += 8
                elif t == _PUSH_INT:
                    items.append((t, q, struct.unpack_from('<i', block, q)[0]))
                    q += 4
                elif t == _PUSH_CONST16:
                    items.append((t, q,
                                  struct.unpack_from('<H', block, q)[0]))
                    q += 2
                else:
                    break           # unknown push type: stop reading THIS push
            yield p, length, items
        p = end


def patch_as2_constants(block: bytes, values: dict) -> tuple:
    """Rewrite named integer constants in an action block.

    values: {constant_pool_name: new_int}. Returns (new_block, {name: (old,
    new)}). A name that is not found, or is found more than once, raises --
    silently skipping one would ship a half-applied layout that is harder to
    diagnose than a hard failure.
    """
    pool = _constant_pool(block)
    index = {name: i for i, name in enumerate(pool)}
    out = bytearray(block)
    applied = {}

    for name, new_value in values.items():
        if name not in index:
            raise UiConvertError(f'{name!r} is not in the AS2 constant pool')
        want = index[name]
        hits = []
        for _off, _len, items in _iter_pushes(block):
            # The initialiser shape: a constant-pool reference to the name,
            # immediately followed by the integer literal being assigned.
            for a, b in zip(items, items[1:]):
                if (a[0] in (_PUSH_CONST8, _PUSH_CONST16) and a[2] == want
                        and b[0] == _PUSH_INT):
                    hits.append(b)
        if len(hits) != 1:
            raise UiConvertError(
                f'{name!r}: expected exactly one integer initialiser, '
                f'found {len(hits)}')
        _t, value_off, old = hits[0]
        struct.pack_into('<i', out, value_off, int(new_value))
        applied[name] = (old, int(new_value))
    return bytes(out), applied


def force_as2_boolean(block: bytes, name: str, value: bool) -> tuple:
    """Rewrite `this.<name> = <register>` into `this.<name> = <value>`.

    Used to pin `IsVertical`. Skyrim stacks a message box's choices only when
    its C++ side says so -- `SetIsVertical` is a `GameDelegate` callback whose
    whole body is `this.IsVertical = aIsVertical`, and for the chargen menus
    the engine says false, which laid ten birthsigns out in a row across the
    entire screen. Oblivion ALWAYS stacks (message_menu.xml runs button_1..10
    down the box), so the setter is pinned rather than the default, which the
    engine would otherwise overwrite.

    A register push and a boolean push are both two bytes, so this is an
    in-place edit and the block keeps its length.
    """
    pool = _constant_pool(block)
    if name not in pool:
        raise UiConvertError(f'{name!r} is not in the AS2 constant pool')
    want = pool.index(name)
    hits = []
    for _off, _len, items in _iter_pushes(block):
        # The setter's shape: (this, <name>, the argument register).
        for a, b in zip(items, items[1:]):
            if (a[0] in (_PUSH_CONST8, _PUSH_CONST16) and a[2] == want
                    and b[0] == _PUSH_REGISTER):
                hits.append(b)
    if len(hits) != 1:
        raise UiConvertError(
            f'{name!r}: expected exactly one register assignment, '
            f'found {len(hits)}')
    _t, value_off, old_register = hits[0]
    out = bytearray(block)
    out[value_off - 1] = _PUSH_BOOLEAN
    out[value_off] = 1 if value else 0
    return bytes(out), (f'reg{old_register}', value)


def patch_as2_literal(block: bytes, register: int, old_value: int,
                      new_value: int) -> tuple:
    """Rewrite a `Push [regN, <int old_value>]` literal.

    This is the unnamed sibling of the named margins: `PositionElements`
    computes the panel's text-driven width as `widestLine + 60`, where 60 is a
    bare literal with no constant-pool name to anchor on. The anchor used
    instead is the exact push shape (that register, that value), and finding
    anything other than exactly one match raises -- which is what makes it safe
    to patch something identified only by its value.
    """
    hits = []
    for _off, _len, items in _iter_pushes(block):
        for a, b in zip(items, items[1:]):
            if (a[0] == _PUSH_REGISTER and a[2] == register
                    and b[0] == _PUSH_INT and b[2] == old_value):
                hits.append(b)
    if len(hits) != 1:
        raise UiConvertError(
            f'push [reg{register}, {old_value}]: expected exactly one, '
            f'found {len(hits)}')
    out = bytearray(block)
    struct.pack_into('<i', out, hits[0][1], int(new_value))
    return bytes(out), (old_value, int(new_value))


# Where the AS2 layout class lives.
AS2_CLASS_EXPORT = '__Packages.MessageBox'

# Unnamed literals in that class worth carrying across, as
# (register, vanilla value, how to derive the new one from the derived
# constants). Only PositionElements' text-side margin qualifies so far: it is
# the horizontal sibling of WIDTH_MARGIN that Bethesda wrote as a bare 60.
_LITERAL_PATCHES = (
    (3, 60, lambda c: c['WIDTH_MARGIN'] * 2),
)


# ---------------------------------------------------------------------------
# The frame
# ---------------------------------------------------------------------------

FRAME_SUPERSAMPLE = 1       # bitmap pixels per shape pixel (see compose_frame)


def compose_frame(slices: dict, border: int, supersample: int = 1):
    """The WHOLE frame -- center and all eight border pieces -- as ONE image.

    🛑 One bitmap, one fill, one rect. Skyrim's Scaleform does not render a
    shape whose paths select DIFFERENT bitmap fills: across vanilla, 202 of the
    207 bitmap-filled shapes use exactly one fill, and the five exceptions are
    a single continuous path switching fill along its edges, never disjoint
    filled rectangles. Handed nine disjoint rects, the engine drew the FIRST
    fill across the whole shape -- which is why round 1 showed a magnified
    corner (its first rect was `top_left`) and round 5 showed bare parchment
    (its first rect was the center). Both screenshots are the same bug.

    `supersample` renders the image at N pixels per shape pixel. It is 1: the
    shape rides `Background_mc`'s uniform scale, and moving CENTER_BASE_* to
    match a real panel fixes both blur and aspect far more cheaply than extra
    pixels would (see the note there).
    """
    from PIL import Image
    width = (2 * border + CENTER_BASE_W) * supersample
    height = (2 * border + CENTER_BASE_H) * supersample
    edge = border * supersample
    inner_w, inner_h = width - 2 * edge, height - 2 * edge

    out = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    # The center goes down and back up (see CENTER_DETAIL_DIVISOR); the border
    # slices below are composited at full detail.
    coarse = max(1, CENTER_DETAIL_DIVISOR)
    center = slices['center'].resize(
        (max(1, inner_w // coarse), max(1, inner_h // coarse)), Image.LANCZOS)
    out.alpha_composite(center.resize((inner_w, inner_h), Image.LANCZOS),
                        (edge, edge))
    # Corners are placed at their own size; edges are TILED along their length
    # so the carving keeps its native density (see build_frame_slices).
    for name, (x, y, w, h) in {
            'top_left': (0, 0, edge, edge),
            'top_right': (width - edge, 0, edge, edge),
            'bottom_left': (0, height - edge, edge, edge),
            'bottom_right': (width - edge, height - edge, edge, edge),
    }.items():
        out.alpha_composite(slices[name].resize((w, h), Image.LANCZOS), (x, y))
    for name, (x, y, w, h) in {
            'top': (edge, 0, inner_w, edge),
            'bottom': (edge, height - edge, inner_w, edge),
            'left': (0, edge, edge, inner_h),
            'right': (width - edge, edge, edge, inner_h),
    }.items():
        out.alpha_composite(tile_image(slices[name], w, h), (x, y))
    return out


# The eight border slices, in the order build_frame_slices returns them
# around the center. compose_frame places each one.
_BORDER_ORDER = ('top_left', 'top', 'top_right',
                 'left', 'right',
                 'bottom_left', 'bottom', 'bottom_right')



def tile_image(image, width, height):
    """`image` repeated to fill (width, height).

    focus_box.xml marks its edges `<tile> &true; </tile>`, so they repeat
    rather than stretch -- which for a 1024 px edge texture on a 200 px box is
    simply a crop, but for the 4x4 center is a real tiling.
    """
    from PIL import Image
    out = Image.new('RGBA', (max(1, width), max(1, height)), (0, 0, 0, 0))
    for y in range(0, out.height, image.height):
        for x in range(0, out.width, image.width):
            out.alpha_composite(image, (x, y))
    return out


def compose_focus_box(textures: dict, content_w: int = FOCUS_CONTENT_W,
                      content_h: int = FOCUS_CONTENT_H):
    """Oblivion's focus box as ONE image, plus the origin to draw it about.

    Returns (image, offset_x, offset_y) where the offsets place the image so
    that its CONTENT area is centered on the origin. That matters because
    Skyrim positions the indicator by its own origin
    (`SelectionIndicator._y = ButtonText._y + ButtonText._height / 2`), and
    Oblivion's border is asymmetric -- 9 px above the content, 14 below --
    so centering the image would sit the box low.

    One image for the same reason the frame is one image: this engine draws
    only a shape's first bitmap fill (see compose_frame).
    """
    from PIL import Image
    art = {}
    for name in FOCUS_TEXTURES:
        art[name.replace('focus_', '').replace('.dds', '')] = \
            to_image(textures[name])

    left, top = FOCUS_EDGE_LEFT, FOCUS_EDGE_TOP
    right, bottom = FOCUS_EDGE_RIGHT, FOCUS_EDGE_BOTTOM
    width = left + content_w + right
    height = top + content_h + bottom
    out = Image.new('RGBA', (width, height), (0, 0, 0, 0))

    def crop(key, w, h):
        """The authored 1:1 crop from the top-left of a texture."""
        piece = art[key]
        return piece.crop((0, 0, min(w, piece.width), min(h, piece.height)))

    # The content box's top-left inside the composed image.
    ox, oy = left, top
    # Center first (tiled, and inset by 1 all round exactly as authored).
    out.alpha_composite(tile_image(art['center_x4'], content_w + 2, content_h + 2),
                        (ox - 1, oy - 1))
    # Edges, tiled along their length.
    out.alpha_composite(tile_image(crop('top', 1024, top), content_w + 2, top),
                        (ox - 1, oy - 8))
    out.alpha_composite(tile_image(crop('bottom', 1024, bottom),
                              content_w + 2, bottom), (ox - 1, oy + content_h))
    out.alpha_composite(tile_image(crop('left', left, 256), left, content_h + 2),
                        (ox - 8, oy - 1))
    out.alpha_composite(tile_image(crop('right', right, 256), right, content_h + 2),
                        (ox + content_w, oy - 1))
    # Corners sit on top (focus_box.xml gives them depth 1).
    out.alpha_composite(crop('top_left', 17, 18), (ox - 8, oy - 9))
    out.alpha_composite(crop('top_right', 22, 21), (ox + content_w - 8, oy - 8))
    out.alpha_composite(crop('bottom_left', 17, 22),
                        (ox - 8, oy + content_h - 8))
    out.alpha_composite(crop('bottom_right', 12, 14),
                        (ox + content_w, oy + content_h))

    return out, -(left + content_w / 2), -(top + content_h / 2)


def transparent_shape(character_id: int) -> swf.Tag:
    """A shape that draws nothing visible.

    Used to retire the Divider hairline, which Oblivion's message box does not
    have. A fully transparent fill rather than an empty shape: the AS2 still
    writes `Divider._width`, and a clip with real geometry takes that
    assignment normally, where a geometry-less one can yield NaN.
    """
    return swf.define_shape3_solid_rects(character_id, [(0, 0, 1, 1)],
                                         (0, 0, 0, 0))


def set_edit_text_height(tag: swf.Tag, height: int) -> tuple:
    """Shrink a DefineEditText's authored bounds to `height` px. -> (tag, old)

    🛑 This is where the dead space between the header and the choices comes
    from. `MessageText` is authored 121 px tall for a single 22 px line, and
    `PositionElements` sizes the panel as

        height = Message._height + buttons._height + HEIGHT_MARGIN*2 + SPACER

    so ~100 px of empty field is baked into every message box, however many
    choices it has -- which is exactly why the gap measured the same (~130 px)
    on a four-choice box and a ten-choice one.

    The field keeps WordWrap and Multiline, so a longer message still grows;
    what changes is only how much empty space a SHORT one reserves.
    """
    data = bytearray(tag.data)
    reader = swf.BitReader(data, 2)
    nbits = reader.ub(5)
    x_min, x_max, y_min, y_max = (reader.sb(nbits) for _ in range(4))
    reader.align()
    rest = bytes(data[reader.offset:])
    old = (y_max - y_min) / swf.TWIPS
    bounds = swf.pack_rect(x_min, x_max, y_min,
                           y_min + int(round(height * swf.TWIPS)))
    return swf.Tag(tag.code, bytes(data[:2]) + bounds + rest,
                   tag.force_long), old


def recolor_edit_text(tag: swf.Tag, rgb: tuple) -> tuple:
    """A copy of a DefineEditText with its TextColor's RGB replaced.

    ALPHA IS KEPT: vanilla runs the message at 255 and the unfocused choices
    at 204, and that difference is what dims an unselected option. Replacing
    all four bytes would flatten the two apart.

    Returns (tag, (old_rgba, new_rgba, html_edits)); raises when the field
    carries no color of its own, because then the color is coming from
    somewhere this does not patch and a silent no-op would be misleading.
    """
    data = bytearray(tag.data)
    offset = 2
    nbits = data[offset] >> 3
    offset += (5 + nbits * 4 + 7) // 8          # skip the bounds RECT
    flags = struct.unpack_from('<H', data, offset)[0]
    offset += 2
    if not flags & 0x0004:                      # HasTextColor
        raise UiConvertError(
            f'DefineEditText {struct.unpack_from("<H", data, 0)[0]} has no '
            f'TextColor to recolor')
    if flags & 0x0001:                          # HasFont
        offset += 4                             # FontID + FontHeight
    old = tuple(data[offset:offset + 4])
    data[offset:offset + 3] = bytes(rgb)        # RGBA, alpha untouched

    # 🛑 AND THE COLOR BAKED INTO THE FIELD'S INITIAL HTML.
    #
    # Both fields ship an authoring placeholder as HTML with an explicit
    # `color="#ffffff"`, and the class captures it:
    #
    #     DefaultTextFormat = Message.getTextFormat()      (constructor)
    #     Message.setTextFormat(DefaultTextFormat)         (SetMessage)
    #
    # `getTextFormat()` reports the format of the text CURRENTLY in the field,
    # which is that placeholder -- so the captured format is white whatever
    # TextColor says, and SetMessage paints it back over every message. The
    # header stayed white in game for exactly this reason while the choices,
    # which assign `.text` and pick up TextColor normally, went brown.
    #
    # Six hex digits replace six, so the record does not change length.
    html_color = '#%02x%02x%02x' % tuple(rgb)
    patched, count = _HTML_COLOR_RE.subn(
        lambda m: m.group(1) + html_color + m.group(3),
        bytes(data).decode('latin1'))
    if count:
        data = bytearray(patched.encode('latin1'))
    return (swf.Tag(tag.code, bytes(data), tag.force_long),
            (old, tuple(rgb) + (old[3],), count))


# Filter ids that carry a `strength` field, and how many bytes of filter body
# precede it. Both are the shadow/glow family, which is all this needs to mute.
_FILTER_STRENGTH_OFFSET = {
    0: 4 + 16,      # DropShadow: RGBA, blurX/Y, angle, distance
    2: 4 + 8,       # Glow: RGBA, blurX/Y
}


def _iter_sprite_tags(data: bytes, start: int = 4):
    """(code, payload_offset, payload_length) over a sprite body's tags."""
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
            return


def _placement_name_offset(data: bytes, offset: int, is_place3: int):
    """(name, offset just past it) for a PlaceObject2/3 payload, or (None, _)."""
    flags = data[offset]
    flags2 = data[offset + 1] if is_place3 else 0
    at = offset + (2 if is_place3 else 1) + 2      # flags(+2), depth
    if is_place3 and flags2 & 0x10:                # HasClassName
        at = data.index(b'\x00', at) + 1
    if flags & 0x02:
        at += 2                                    # character id
    reader = swf.BitReader(data, at)
    if flags & 0x04:
        reader.skip_matrix()
    if flags & 0x08:                               # color transform
        has_add, has_mult = reader.ub(1), reader.ub(1)
        nbits = reader.ub(4)
        for _ in range(4 if has_mult else 0):
            reader.sb(nbits)
        for _ in range(4 if has_add else 0):
            reader.sb(nbits)
        reader.align()
    at = reader.offset
    if flags & 0x10:
        at += 2                                    # ratio
    if not flags & 0x20:
        return None, at
    end = data.index(b'\x00', at)
    return data[at:end].decode('latin1'), end + 1


def find_named_child(sprite_data: bytes, name: str):
    """(tag_code, payload_offset, payload_length) of a sprite's named child."""
    for code, offset, length in _iter_sprite_tags(sprite_data):
        if code not in (swf.TAG_PLACE_OBJECT_2, 70):
            continue
        found, _after = _placement_name_offset(sprite_data, offset, code == 70)
        if found == name:
            return code, offset, length
    raise UiConvertError(f'{name!r} is not a named child of this sprite')


def make_child_opaque(sprite_data: bytes, name: str) -> tuple:
    """Raise a named child's color-transform ALPHA multiplier to opaque.

    🛑 This is what makes the panel see-through, not the artwork. Vanilla
    places `Background_mc` with a color transform whose alpha multiplier is
    205/256 -- Skyrim's message panel is deliberately translucent -- and that
    multiplies the whole clip regardless of how opaque the bitmap inside it is.

    The multiplier is rewritten IN PLACE at its existing bit width, so the tag
    keeps its length. That width is what caps the result: a 9-bit signed field
    holds 255, i.e. 99.6%, which is not distinguishable from 256.
    """
    _code, offset, length = find_named_child(sprite_data, name)
    found = swf.find_cxform_alpha(sprite_data, offset, length)
    if found is None:
        return bytes(sprite_data), None          # nothing dimming it
    position, width, old = found
    new = min(256, (1 << (width - 1)) - 1)
    data = bytearray(sprite_data)
    swf.poke_bits(data, position, width, new)
    return bytes(data), (old, new)


def mute_child_shadow(sprite_data: bytes, name: str) -> tuple:
    """Zero the strength of every shadow/glow filter on a named child.

    Skyrim hangs a DropShadow on `MessageText` (black, 2 px blur, 45 degrees,
    strength 1.0) which reads as an outline against Oblivion's parchment.
    Strength is a 2-byte 8.8 fixed value, so zeroing it removes the shadow
    without touching the tag's length or the filter count -- safer than
    dropping the filter list, which would mean re-headering the tag.
    """
    code, offset, length = find_named_child(sprite_data, name)
    if code != 70:                               # only PlaceObject3 has filters
        return bytes(sprite_data), 0
    flags2 = sprite_data[offset + 1]
    if not flags2 & 0x01:                        # HasFilterList
        return bytes(sprite_data), 0
    _name, at = _placement_name_offset(sprite_data, offset, True)
    if sprite_data[offset] & 0x40:               # HasClipDepth
        at += 2
    data = bytearray(sprite_data)
    count = data[at]
    at += 1
    muted = 0
    for _ in range(count):
        filter_id = data[at]
        at += 1
        skip = _FILTER_STRENGTH_OFFSET.get(filter_id)
        if skip is None:
            break                                # an unknown filter ends the walk
        struct.pack_into('<h', data, at + skip, 0)
        muted += 1
        at += skip + 2 + 1                       # ...strength, flags
    return bytes(data), muted


def patch_message_box(movie_bytes: bytes, textures: dict, layout: dict,
                      hide_divider: bool = True,
                      hide_marker: bool = True,
                      force_vertical: bool = True,
                      text_rgb: tuple = OBLIVION_TEXT_RGB,
                      opaque: bool = True,
                      mute_shadow: bool = True,
                      scale9: bool = True,
                      message_height: int = MESSAGE_TEXT_HEIGHT) -> tuple:
    """Reskin Skyrim's messagebox.swf with Oblivion's frame. -> (bytes, report)

    `textures` maps BACKGROUND_TEXTURES names to DDS bytes; `layout` is
    `read_oblivion_layout`'s dict.
    """
    movie = swf.Swf.parse(movie_bytes)
    report = {'layout': dict(layout), 'constants': {}, 'literals': {}}

    exports = movie.exports()
    for name, expect in (('MessageBox', 15), ('MessageBoxButton', 9),
                         ('DiamondMarker', DIAMOND_MARKER_ID)):
        if exports.get(name) != expect:
            raise UiConvertError(
                f'messagebox.swf does not match: export {name!r} is '
                f'{exports.get(name)}, expected {expect}')

    border = int(round(layout['border']))
    slices = dict(build_frame_slices(textures, border))

    # -- The CENTER goes on Background_mc itself.
    #
    # Skyrim's AS2 already sizes that clip to the whole panel, and a uniformly
    # scaled parchment is exactly what Oblivion draws -- generic_background
    # stretches `center_background.dds` across the box with no tiling. So the
    # center needs no help, and putting it here means a failure in the border
    # script below degrades to "a plain parchment panel" rather than to an
    # invisible one.
    shape_index = movie.index_of_character(BACKGROUND_SHAPE_ID)
    next_id = movie.next_character_id()

    # The whole frame as ONE bitmap on shape 10 -- one fill, one rect, which
    # is vanilla's overwhelming idiom and the only thing observed to render
    # here. See compose_frame for why nine rects could never work, and
    # ui_conversion.md for what else was ruled out along the way.
    width = 2 * border + CENTER_BASE_W
    height = 2 * border + CENTER_BASE_H
    frame = compose_frame(slices, border, FRAME_SUPERSAMPLE)
    movie.tags[shape_index:shape_index] = [swf.define_bits_lossless2(
        next_id, frame.width, frame.height, premultiplied_argb(frame))]
    shape_index += 1
    movie.tags[shape_index] = swf.define_shape3_bitmap_rects(
        BACKGROUND_SHAPE_ID,
        [(next_id, -width / 2, -height / 2, width, height,
          (frame.width, frame.height))])
    next_id += 1

    # 🛑 RE-CUT THE SCALING GRID TO OUR BORDER.
    #
    # Background_mc already carries a DefineScalingGrid -- vanilla 9-slices it.
    # We were leaving it untouched on the belief that this engine will not
    # 9-slice bitmap art, but the census behind that belief compared scaling
    # grids (which name a SPRITE) against bitmap fills (which live on a SHAPE).
    # Those are disjoint character kinds, so an intersection of zero was
    # guaranteed by construction and proved nothing. Asking the question one
    # level down -- does any grid's sprite CONTAIN bitmap art? -- finds vanilla
    # doing it in magicmenu, containermenu and craftingmenu.
    #
    # The grid was still there, but its splitter was authored for vanilla's
    # much smaller panel art. Against our 560x800 shape its fixed rows come to
    # 354 px top and bottom, more than a whole 379 px panel, so the slice
    # degenerates and the engine falls back to a plain stretch -- which is
    # exactly the artifact: the frame scaled ~0.99 wide by ~0.47 tall, leaving
    # the side borders twice the thickness of the top and bottom and the
    # carving on them compressed to under half density.
    #
    # Re-cut to OUR border, the corners hold their size and only the middle
    # stretches, which is what the art was drawn for.
    grid_index = movie.scaling_grid_index(BACKGROUND_SPRITE_ID)
    if scale9 and grid_index is not None:
        movie.tags[grid_index] = swf.define_scaling_grid(
            BACKGROUND_SPRITE_ID, border, border, border, border,
            width, height)
        report['scale9'] = (f'grid re-cut to a {border}px border on '
                            f'{width}x{height}')
    elif not scale9 and grid_index is not None:
        report['scale9'] = 'left at vanilla (border rides the uniform scale)'

    report['frame'] = {
        'border': border,
        'base': f'{width}x{height}',
        'bitmap': f'{frame.width}x{frame.height} '
                  f'({FRAME_SUPERSAMPLE}x supersampled)',
        'note': ('single fill on Background_mc, 9-sliced at the border'
                 if scale9 and grid_index is not None
                 else 'single fill on Background_mc; rides its uniform scale'),
    }

    if hide_divider:
        movie.tags[movie.index_of_character(DIVIDER_SHAPE_ID)] = \
            transparent_shape(DIVIDER_SHAPE_ID)
        report['divider'] = 'hidden'

    if hide_marker:
        # Skyrim's arrows become Oblivion's own focus box, composed from
        # Oblivion's own art. One bitmap, one fill, for the same reason the
        # frame is one bitmap.
        box, offset_x, offset_y = compose_focus_box(textures)
        arrow_index = movie.index_of_character(SELECTION_ARROW_SHAPE_ID)
        movie.tags[arrow_index:arrow_index] = [swf.define_bits_lossless2(
            next_id, box.width, box.height, premultiplied_argb(box))]
        movie.tags[arrow_index + 1] = swf.define_shape3_bitmap_rects(
            SELECTION_ARROW_SHAPE_ID,
            [(next_id, offset_x, offset_y, box.width, box.height)])
        report['marker'] = (f'shape {SELECTION_ARROW_SHAPE_ID}: Skyrim arrows '
                            f'-> Oblivion focus box, {box.width}x{box.height} '
                            f'from its own art (content '
                            f'{FOCUS_CONTENT_W}x{FOCUS_CONTENT_H})')
        next_id += 1

    if text_rgb:
        recolored = {}
        for character_id, label in ((MESSAGE_TEXT_ID, 'message'),
                                    (BUTTON_TEXT_ID, 'choices')):
            index = movie.index_of_character(character_id)
            movie.tags[index], change = recolor_edit_text(
                movie.tags[index], text_rgb)
            recolored[label] = change
        report['text_color'] = recolored

    if message_height:
        index = movie.index_of_character(MESSAGE_TEXT_ID)
        movie.tags[index], old = set_edit_text_height(
            movie.tags[index], message_height)
        report['message_field'] = f'{old:g}px tall -> {message_height}px'

    # -- The panel's own translucency and the header's drop shadow both live
    # on MessageBox's child PLACEMENTS, not on any artwork, so both are edited
    # in the sprite's tag body.
    message_box_index = movie.index_of_character(exports['MessageBox'])
    sprite = movie.tags[message_box_index].data
    if opaque:
        sprite, change = make_child_opaque(sprite, 'Background_mc')
        if change:
            report['opacity'] = (f'Background_mc alpha {change[0]}/256 -> '
                                 f'{change[1]}/256')
    if mute_shadow:
        sprite, muted = mute_child_shadow(sprite, 'MessageText')
        if muted:
            report['shadow'] = f'{muted} filter(s) on MessageText muted'
    movie.tags[message_box_index] = swf.Tag(
        movie.tags[message_box_index].code, sprite,
        movie.tags[message_box_index].force_long)

    # -- AS2 layout constants.
    constants = _derived_constants(layout)
    target_sprite = exports.get(AS2_CLASS_EXPORT)
    if target_sprite is None:
        raise UiConvertError(f'{AS2_CLASS_EXPORT} is not exported')

    for index, tag in enumerate(movie.tags):
        if tag.code != swf.TAG_DO_INIT_ACTION:
            continue
        (sprite_id,) = struct.unpack_from('<H', tag.data, 0)
        if sprite_id != target_sprite:
            continue
        block = tag.data[2:]
        block, report['constants'] = patch_as2_constants(
            block, {k: int(round(v)) for k, v in constants.items()})
        for register, old, derive in _LITERAL_PATCHES:
            block, change = patch_as2_literal(
                block, register, old, int(round(derive(constants))))
            report['literals'][f'reg{register}:{old}'] = change
        if force_vertical:
            block, change = force_as2_boolean(block, 'IsVertical', True)
            report['is_vertical'] = change
        movie.tags[index] = swf.Tag(swf.TAG_DO_INIT_ACTION,
                                    tag.data[:2] + block, tag.force_long)
        break
    else:
        raise UiConvertError(f'no DoInitAction for {AS2_CLASS_EXPORT}')

    return movie.serialize(compress=True), report
