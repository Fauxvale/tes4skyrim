r"""FaceGen .egt texture-basis decoding and Oblivion skin-tone resolution.

Oblivion stores an actor's skin color in three authored layers:

  1. The RACE record's body/face part ICON paths -> the base diffuse texture.
  2. The RACE record's own FGTS vector.  A race either ships its own skin
     textures (FGTS all zero) or shares another race's textures and recolors
     them with a non-zero FGTS.  This is where High Elf gold, Redguard brown
     and Nord pale come from -- all five of those races point at
     Characters\Imperial\HeadHuman.dds and differ only by race FGTS.
  3. The NPC_ record's own FGTS vector.  Measured across all 2482 Oblivion
     NPCs this moves the color by a standard deviation of ~1/255 per channel,
     so it is deliberately ignored: within a race, Oblivion NPCs are
     effectively one skin tone.

The basis for (2) is the .egt file sitting next to the head mesh.  Format
(verified byte-exact against Oblivion's headhuman.egt, and matching pyffi's
egt.xml at references/pyffi_src/pyffi/formats/egt/egt.xml):

    char[8]  "FREGT003"
    int32    width          256 for faces, 32 for bodies/ears
    int32    height
    int64    num symmetric  50 -- matches the 50 floats in FGTS
    int64    num asymmetric
    byte[32] reserved
    then num_symmetric records of:
        byte[3]  unknown
        byte     flags
        int8[w*h] R plane      signed per-texel delta
        int8[w*h] G plane
        int8[w*h] B plane

The reconstruction is  base + SCALE * sum(FGTS[i] * mode_i)  per channel.
"""

import os
import struct

# See: docs/commentary/asset_convert_facegen.md#fgts-coefficient-scale
FGTS_SCALE = 0.25

_EGT_MAGIC = b'FREGT003'
_EGT_HEADER = 64

_egt_cache = {}
_texture_cache = {}


def load_egt_mode_means(path):
    """Return [(dR, dG, dB), ...] -- the mean per-channel delta of each of the
    50 symmetric texture modes.  A single averaged skin tone only needs each
    mode's mean, not its per-texel detail.  Returns None if unreadable."""
    key = os.path.normcase(os.path.abspath(path))
    if key in _egt_cache:
        return _egt_cache[key]
    modes = None
    try:
        with open(path, 'rb') as f:
            data = f.read()
        if data[:8] == _EGT_MAGIC and len(data) > _EGT_HEADER:
            w, h = struct.unpack_from('<ii', data, 8)
            nsym = struct.unpack_from('<q', data, 16)[0]
            n = w * h
            per = 4 + 3 * n
            if n > 0 and nsym > 0 and _EGT_HEADER + nsym * per <= len(data):
                modes = []
                for i in range(nsym):
                    o = _EGT_HEADER + i * per + 4
                    means = []
                    for c in range(3):
                        plane = data[o + c * n: o + (c + 1) * n]
                        total = 0
                        for v in plane:
                            total += v - 256 if v > 127 else v
                        means.append(total / n)
                    modes.append(tuple(means))
    except (OSError, struct.error):
        modes = None
    _egt_cache[key] = modes
    return modes


def sample_texture_rgb(path):
    """Mean RGB of a diffuse texture, ignoring near-black (transparent/edge)
    texels.  Returns None if the file cannot be read, or if the image is
    effectively greyscale -- Oblivion's per-age head files (headhumanm40.dds
    and friends) are detail/normal maps that sample to a flat (66,65,66) and
    must never be mistaken for a skin color."""
    key = os.path.normcase(os.path.abspath(path))
    if key in _texture_cache:
        return _texture_cache[key]
    rgb = None
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert('RGB')
            im.thumbnail((64, 64))
            px = [p for p in im.getdata() if sum(p) > 30]
        if px:
            n = len(px)
            rgb = tuple(sum(p[i] for p in px) / n for i in range(3))
            mx, mn = max(rgb), min(rgb)
            if mx - mn < 4.0:
                rgb = None          # greyscale -> a normal/detail map
    except Exception:
        rgb = None
    _texture_cache[key] = rgb
    return rgb


def reconstruct_skin_rgb(base_rgb, fgts, egt_modes, scale=FGTS_SCALE):
    """base + scale * sum(FGTS[i] * mode_i), clamped to 0..255."""
    if not base_rgb:
        return None
    out = list(base_rgb)
    if fgts and egt_modes:
        n = min(len(fgts), len(egt_modes))
        for c in range(3):
            out[c] += scale * sum(fgts[i] * egt_modes[i][c] for i in range(n))
    return tuple(int(round(max(0.0, min(255.0, v)))) for v in out)


def parse_fgts_hex(hex_str):
    """Decode a 200-byte FGTS hex string into 50 floats; None if malformed."""
    if not hex_str:
        return None
    try:
        raw = bytes.fromhex(hex_str.strip())
    except ValueError:
        return None
    if len(raw) != 200:
        return None
    vals = struct.unpack('<50f', raw)
    return vals if any(vals) else None
