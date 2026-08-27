"""Reskin Skyrim's `interface/startmenu.swf` into Oblivion's main menu.

Same rule as `ui_menus.patch_message_box`: the vanilla movie's AS2 classes,
timelines and `GameDelegate` plumbing are kept byte-for-byte, so every button
(Continue/New/Load/Settings/Add-Ons/Credits/Quit) keeps its Skyrim function.
We only ADD Oblivion's two animated backdrops behind the existing UI:

  * a parchment/map backdrop (`loading_background.dds`) that drifts slowly, and
  * the "The Elder Scrolls IV / OBLIVION" logo lockup (`tes_oblivion_logo_final.dds`)
    that flies in and fades up, then holds.

Both are self-contained sprites with their OWN internal timelines, so they
animate without any ActionScript -- Flash keeps a same-character/same-depth
instance across the root's frame loop, so their playheads run freely.

Injection (see `docs/ui_conversion.md`): Skyrim's root places `MenuHolder` at
depth 1 and nothing else. We insert the two backdrops at depths 1-2 and move
`MenuHolder` to depth 3, so the menu widgets render in front of the backdrop.
The 3D menu scene (logo NIFs + fog) is blanked separately by `convert_ui`.

No Bethesda asset is redistributed: the DDS inputs are read off the user's own
Oblivion install by the caller.
"""
import struct

from . import swf
from .ui_menus import to_image, premultiplied_argb

# Oblivion's own main-menu art (read from the user's install by the caller).
OB_BACKGROUND = r'textures\menus\loading\loading_background.dds'
OB_LOGO = r'textures\menus\loading\tes_oblivion_logo_final.dds'

# The Skyrim movie we reskin.
STARTMENU_SWF = r'interface\startmenu.swf'

# Stage of startmenu.swf, in its own coordinate space.
STAGE_W, STAGE_H = 1280, 720

# Skyrim places its whole menu (`MenuHolder`) at this depth and nothing else.
MENUHOLDER_CHARACTER = 604

# Backdrop: a square parchment drawn larger than the stage so the drift never
# reveals an edge, centred on the stage with overscan.
BG_DISPLAY = 1500                     # px, square
BG_PAN_FRAMES = 480                   # 16 s at 30 fps, seamless ping-pong loop
BG_AMP_X, BG_AMP_Y = 28, 16           # drift amplitude, px
BG_MAX_TEX = 512                      # downscale the 1024² parchment to this

# Logo lockup, at Oblivion's own on-screen size, upper-centre.
LOGO_W, LOGO_H = 748, 159
LOGO_FLYIN_FRAMES = 48                # 1.6 s fly-in, then stop()
LOGO_RISE = 30                        # px it rises into place
LOGO_MAX_TEX_W = 768                  # downscale width for the lockup


class MainMenuError(RuntimeError):
    pass


# Skyrim's 3D main-menu scene: the SKYRIM logo model (+ its AE variant) and the
# drifting fog particles. Overwriting these with an empty node blanks the scene
# so only our SWF backdrop shows. Paths confirmed by the user.
MENU_SCENE_MESHES = (
    r'meshes\interface\logo\logo.nif',
    r'meshes\interface\logo\logo01ae.nif',
    r'meshes\interface\intmenufogparticles.nif',
    r'meshes\interface\intmenufogparticles_.nif',
)


def blank_nif_bytes() -> bytes:
    """A minimal valid Skyrim NIF: one empty root NiNode, so it renders nothing.

    Built from scratch (no Bethesda asset read) at 20.2.0.7 / user 12 / stream 83
    -- the LE stream SSE loads natively, matching everything else the pipeline
    writes. The header's endian field must be set explicitly on a fresh Data(),
    or PyFFI writes big-endian and nothing can read it back.
    """
    import io
    import time
    if not hasattr(time, 'clock'):
        time.clock = time.perf_counter        # PyFFI 2.2.3 wants time.clock
    from . import pyffi_monkey_patch as _patch  # noqa: F401
    from pyffi.formats.nif import NifFormat

    data = NifFormat.Data()
    data.header.endian_type = NifFormat.EndianType.ENDIANLITTLE
    data.version = 0x14020007
    data.user_version = 12
    data.user_version_2 = 83
    root = NifFormat.NiNode()
    root.name = b'Scene Root'
    data.roots = [root]
    buf = io.BytesIO()
    data.write(buf)
    return buf.getvalue()


def _fit(img, max_w, max_h=None):
    """Downscale (never up) an image to fit, preserving aspect."""
    max_h = max_h or max_w
    w, h = img.size
    if w <= max_w and h <= max_h:
        return img
    scale = min(max_w / w, max_h / h)
    return img.resize((max(1, round(w * scale)), max(1, round(h * scale))))


def _bitmap_tag(char_id, img):
    """DefineBitsLossless2 (premultiplied ARGB) from a Pillow RGBA image."""
    w, h = img.size
    return swf.define_bits_lossless2(char_id, w, h, premultiplied_argb(img)), (w, h)


def _bg_offset(i):
    """Seamless drift offset at frame i (Lissajous; equal at i=0 and i=N)."""
    import math
    t = 2 * math.pi * i / BG_PAN_FRAMES
    return BG_AMP_X * math.sin(t), BG_AMP_Y * math.sin(t + math.pi / 2)


def _build_backdrop_sprite(sprite_id, shape_id):
    """A sprite that places the backdrop shape and drifts it, looping."""
    base_x = STAGE_W / 2 - BG_DISPLAY / 2      # overscan-centred top-left
    base_y = STAGE_H / 2 - BG_DISPLAY / 2
    frames = []
    for i in range(BG_PAN_FRAMES):
        ox, oy = _bg_offset(i)
        pos = (base_x + ox, base_y + oy)
        if i == 0:
            frames.append([swf.place_object2(1, shape_id, translate=pos)])
        else:
            frames.append([swf.place_move(1, translate=pos)])
    return swf.define_sprite_frames(sprite_id, frames)


def _ease_out_cubic(t):
    return 1 - (1 - t) ** 3


def _build_logo_sprite(sprite_id, shape_id):
    """A sprite that flies the logo up while fading in, then stops."""
    final_x = (STAGE_W - LOGO_W) / 2
    final_y = STAGE_H / 4 + 22                  # Oblivion's screen()/4 + 22.5
    frames = []
    for i in range(LOGO_FLYIN_FRAMES):
        t = i / (LOGO_FLYIN_FRAMES - 1)
        e = _ease_out_cubic(t)
        pos = (final_x, final_y + LOGO_RISE * (1 - e))
        alpha = round(255 * e)
        if i == 0:
            # place the character, then set its start alpha in the same frame
            frames.append([swf.place_object2(1, shape_id, translate=pos),
                           swf.place_move(1, translate=pos, alpha=alpha)])
        elif i == LOGO_FLYIN_FRAMES - 1:
            frames.append([swf.place_move(1, translate=pos, alpha=alpha),
                           swf.do_action(swf.STOP_ACTION)])
        else:
            frames.append([swf.place_move(1, translate=pos, alpha=alpha)])
    return swf.define_sprite_frames(sprite_id, frames)


def _find_menuholder(movie):
    """(index, depth) of the root PlaceObject2 that places MenuHolder."""
    for i, tag in enumerate(movie.tags):
        if tag.code != swf.TAG_PLACE_OBJECT_2:
            continue
        flags = tag.data[0]
        if not (flags & 0x02):                 # HasCharacter
            continue
        depth = struct.unpack_from('<H', tag.data, 1)[0]
        char = struct.unpack_from('<H', tag.data, 3)[0]
        if char == MENUHOLDER_CHARACTER:
            return i, depth
    raise MainMenuError('MenuHolder placement (char 604) not found -- '
                        'is this vanilla startmenu.swf?')


def _redepth(tag, new_depth):
    """A copy of a PlaceObject2 tag with its depth field changed."""
    data = bytearray(tag.data)
    struct.pack_into('<H', data, 1, new_depth)
    return swf.Tag(tag.code, bytes(data), force_long=tag.force_long)


def patch_main_menu(movie_bytes: bytes, background_dds: bytes,
                    logo_dds: bytes) -> tuple:
    """Return (patched_swf_bytes, report). Inputs are Oblivion DDS bytes."""
    movie = swf.Swf.parse(movie_bytes)
    report = {}

    # -- decode + size the two textures
    bg_img = _fit(to_image(background_dds), BG_MAX_TEX)
    logo_img = _fit(to_image(logo_dds), LOGO_MAX_TEX_W, LOGO_MAX_TEX_W)
    report['bg_tex'] = bg_img.size
    report['logo_tex'] = logo_img.size

    # -- character ids: six contiguous, all above vanilla's max
    base = movie.next_character_id()
    bg_bmp_id, bg_shape_id, logo_bmp_id, logo_shape_id, bg_sprite_id, \
        logo_sprite_id = range(base, base + 6)

    bg_bmp, (bgw, bgh) = _bitmap_tag(bg_bmp_id, bg_img)
    # backdrop shape: fill a BG_DISPLAY square, scaling the bitmap up to it
    bg_shape = swf.define_shape3_bitmap_rects(
        bg_shape_id, [(bg_bmp_id, 0, 0, BG_DISPLAY, BG_DISPLAY, (bgw, bgh))])
    logo_bmp, (lw, lh) = _bitmap_tag(logo_bmp_id, logo_img)
    logo_shape = swf.define_shape3_bitmap_rects(
        logo_shape_id, [(logo_bmp_id, 0, 0, LOGO_W, LOGO_H, (lw, lh))])
    bg_sprite = _build_backdrop_sprite(bg_sprite_id, bg_shape_id)
    logo_sprite = _build_logo_sprite(logo_sprite_id, logo_shape_id)

    all_defs = [bg_bmp, bg_shape, logo_bmp, logo_shape, bg_sprite, logo_sprite]
    report['new_char_ids'] = [bg_bmp_id, bg_shape_id, logo_bmp_id,
                              logo_shape_id, bg_sprite_id, logo_sprite_id]

    # -- inject: defs + two placements before MenuHolder, then re-depth it
    idx, depth = _find_menuholder(movie)
    if depth != 1:
        report['menuholder_depth_was'] = depth
    placements = [swf.place_object2(1, bg_sprite_id, name='OblivionBackdrop'),
                  swf.place_object2(2, logo_sprite_id, name='OblivionLogo')]
    movie.tags[idx] = _redepth(movie.tags[idx], 3)
    movie.tags[idx:idx] = all_defs + placements

    out = movie.serialize(compress=True)
    report['output_bytes'] = len(out)
    return out, report
