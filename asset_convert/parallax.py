"""Carry Oblivion's parallax across to Skyrim.

Oblivion switches parallax on per shape with
``NiTexturingProperty.apply_mode == APPLY_HILIGHT2 (4)`` and keeps the height
field in the DIFFUSE's ALPHA channel.  Skyrim wants it as a separate greyscale
texture in slot 3 (``<name>_p.dds``) with shader type 3 (Heightmap) and
``SLSF1_Parallax`` set.

Two questions decide whether a shape is converted, and they need two different
sources:

  * the MESH FLAG answers "did the author want parallax here" — that is
    authored intent and is never guessed at;
  * the TEXTURE answers "is there any height data to carry" — measured over
    Nehrim's full 12,437-mesh set: 2359 flagged shapes on **130 distinct
    diffuse textures**, of which only **38** actually hold one.  67 are DXT1
    with no alpha channel at all (vanilla Oblivion architecture sets the flag
    and ships no data, so Oblivion itself renders no parallax there either),
    14 are flat, 6 are too coarsely quantised to be a surface, 1 is a
    soft-edged mask, 1 a transparency cutout, and 3 name a file that does not
    exist.

Both must say yes.  With the flag alone we would write an empty height map and
switch the shader for two thirds of the textures, and a parallax shape with no
usable height renders as a visibly swimming surface.

Per SHAPE the yield is much better than per texture — the textures that do
carry height are the ones used everywhere: **1495 of the 2359 flagged shapes
(63%) convert**, the rest are left flat.

**This never runs by default.**  Verified in game: a correctly built parallax
shape swims in vanilla SSE, and the SSE Parallax Shader Fix did not help.  It
renders correctly under Community Shaders and under ENB, so the output requires
one of those — which the converter cannot detect, hence the opt-in switch.

The alpha classification here follows the one in the author's own
TES4AutoParallaxer, whose thresholds were tuned against this very content.
"""

import math
import os
import struct

import numpy as np

# Oblivion's parallax switch on NiTexturingProperty.
APPLY_HILIGHT2 = 4

# Skyrim BSLightingShaderProperty shader type for the parallax path.
SHADER_TYPE_HEIGHTMAP = 3

# Texture slot the height map goes in (the 4th).
HEIGHT_SLOT = 3

_DDS_MAGIC = b'DDS '
_DDPF_FOURCC = 0x4
_FOURCC_DXT1 = b'DXT1'
_FOURCC_DXT3 = b'DXT3'
_FOURCC_DXT5 = b'DXT5'
_FOURCC_DX10 = b'DX10'
_DXGI_BC1 = (70, 71, 72)
_DXGI_BC2 = (73, 74, 75)
_DXGI_BC3 = (76, 77, 78)
_DXGI_BC4_UNORM = 80

# Classification thresholds.  Taken from TES4AutoParallaxer, where they were
# calibrated on Oblivion and Nehrim textures — the same content this converts.
_MIN_RANGE = 30        # max-min alpha below this is a flat channel
_MIN_MID_RATIO = 0.15  # share of texels in 16..239; low means a cutout mask
_MAX_EDGE_RATIO = 0.70 # share at the extremes; high means a soft-edged mask

# Distinct alpha values below which the channel is a staircase, not a surface.
#
# This is a STORAGE limit, not a tuning knob.  DXT3 keeps 4-bit explicit alpha,
# so it can hold at most 16 values however the artist authored them; DXT5
# interpolates and reaches 256.  Measured over the 44 textures this converter
# first accepted: every DXT3 one landed between 7 and 16 levels, every DXT5 one
# between 147 and 256 — two clusters with nothing in between, so the threshold
# is not fitted to the data.
#
# It matters because a parallax shader OFFSETS by the height.  RockBeach04 has
# 7 levels across a range of 102, i.e. ~15 units per step: the surface renders
# as visible terracing rather than depth, which is exactly what it looked like
# in game.  Counting levels rather than testing the FourCC keeps it honest — a
# DXT5 alpha that happens to be a 5-step staircase is just as unusable.
_MIN_LEVELS = 64


class AlphaInfo:
    """What a diffuse's alpha channel turned out to be.

    `kind` is one of: ``height``, ``quantised``, ``binary``, ``bimodal``,
    ``empty``, ``no_alpha``, ``unreadable``.  A category rather than a bool,
    because the build log has to be able to say WHY a shape was skipped —
    "skipped" alone sends the next person back to the texture with no lead.
    """

    __slots__ = ('kind', 'fmt', 'rng', 'mid_ratio', 'edge_ratio', 'mean',
                 'levels')

    def __init__(self, kind, fmt='unknown', rng=0, mid_ratio=0.0,
                 edge_ratio=0.0, mean=0.0, levels=0):
        self.kind = kind
        self.fmt = fmt
        self.rng = rng
        self.mid_ratio = mid_ratio
        self.edge_ratio = edge_ratio
        self.mean = mean
        self.levels = levels

    @property
    def usable(self) -> bool:
        return self.kind == 'height'

    def __repr__(self):
        return (f'<AlphaInfo {self.kind} {self.fmt} range={self.rng} '
                f'levels={self.levels} mid={self.mid_ratio:.2f} '
                f'edge={self.edge_ratio:.2f}>')


def _first_mip_bytes(data: bytes, block_size: int = 16) -> int:
    if len(data) < 20:
        return 0
    height = struct.unpack_from('<I', data, 12)[0]
    width = struct.unpack_from('<I', data, 16)[0]
    return ((max(1, width) + 3) // 4) * ((max(1, height) + 3) // 4) * block_size


def _stats(seen, amin, amax, mid, edge, total, ssum):
    return {
        'rng': amax - amin if total else 0,
        'mid_ratio': mid / total if total else 0.0,
        'edge_ratio': edge / total if total else 0.0,
        'mean': ssum / total if total else 0.0,
        'levels': sum(seen) if total else 0,
    }


def _scan_dxt5_alpha(data: bytes, offset: int, nbytes: int):
    """Decode DXT5 alpha blocks through the INTERPOLATED palette.

    Sampling only the two endpoints (a0/a1) misreads every smooth height field
    as binary, because the endpoints of a gentle gradient block sit far apart
    while everything between them is mid-tone.
    """
    amin, amax = 255, 0
    mid = edge = total = ssum = 0
    seen = bytearray(256)          # which of the 256 values actually occur
    pos = offset
    end = min(len(data), offset + nbytes)
    while pos + 16 <= end:
        a0, a1 = data[pos], data[pos + 1]
        if a0 > a1:
            pal = (a0, a1,
                   (6 * a0 + a1) // 7, (5 * a0 + 2 * a1) // 7,
                   (4 * a0 + 3 * a1) // 7, (3 * a0 + 4 * a1) // 7,
                   (2 * a0 + 5 * a1) // 7, (a0 + 6 * a1) // 7)
        else:
            pal = (a0, a1,
                   (4 * a0 + a1) // 5, (3 * a0 + 2 * a1) // 5,
                   (2 * a0 + 3 * a1) // 5, (a0 + 4 * a1) // 5,
                   0, 255)
        bits = int.from_bytes(data[pos + 2:pos + 8], 'little')
        for i in range(16):
            v = pal[(bits >> (i * 3)) & 0x7]
            if v < amin:
                amin = v
            if v > amax:
                amax = v
            ssum += v
            seen[v] = 1
            # Disjoint bands: a texel counts as mid OR edge, never both, so the
            # two ratios can be tuned independently.
            if 16 <= v <= 239:
                mid += 1
            else:
                edge += 1
            total += 1
        pos += 16
    return _stats(seen, amin, amax, mid, edge, total, ssum)


def _scan_dxt3_alpha(data: bytes, offset: int, nbytes: int):
    """DXT3 stores 4-bit explicit alpha — 16 nibbles per block.

    At most 16 distinct values exist in this format, which is why the level
    count below rejects every DXT3 source: see `_MIN_LEVELS`.
    """
    amin, amax = 255, 0
    mid = edge = total = ssum = 0
    seen = bytearray(256)
    pos = offset
    end = min(len(data), offset + nbytes)
    while pos + 16 <= end:
        for i in range(8):
            byte = data[pos + i]
            for nib in (byte & 0x0F, byte >> 4):
                v = nib * 17          # 4-bit -> 8-bit
                if v < amin:
                    amin = v
                if v > amax:
                    amax = v
                ssum += v
                seen[v] = 1
                if 16 <= v <= 239:
                    mid += 1
                else:
                    edge += 1
                total += 1
        pos += 16
    return _stats(seen, amin, amax, mid, edge, total, ssum)


def classify_alpha(data: bytes) -> AlphaInfo:
    """Decide what a diffuse's alpha channel is.

    Returns an :class:`AlphaInfo`; only ``kind == 'height'`` may be converted.
    """
    if len(data) < 128 or data[:4] != _DDS_MAGIC:
        return AlphaInfo('unreadable')

    pf_flags = struct.unpack_from('<I', data, 80)[0]
    fourcc = data[84:88]
    if not (pf_flags & _DDPF_FOURCC):
        return AlphaInfo('no_alpha', 'uncompressed')

    payload = 128
    if fourcc == _FOURCC_DX10:
        if len(data) < 148:
            return AlphaInfo('unreadable')
        dxgi = struct.unpack_from('<I', data, 128)[0]
        payload = 148
        if dxgi in _DXGI_BC1:
            return AlphaInfo('no_alpha', 'bc1')
        if dxgi in _DXGI_BC2:
            fourcc = _FOURCC_DXT3
        elif dxgi in _DXGI_BC3:
            fourcc = _FOURCC_DXT5
        else:
            return AlphaInfo('no_alpha', f'dxgi{dxgi}')

    if fourcc == _FOURCC_DXT1:
        # 1-bit punch-through at most — never a height field.
        return AlphaInfo('no_alpha', 'dxt1')
    if fourcc == _FOURCC_DXT3:
        s = _scan_dxt3_alpha(data, payload, _first_mip_bytes(data))
        fmt = 'dxt3'
    elif fourcc == _FOURCC_DXT5:
        s = _scan_dxt5_alpha(data, payload, _first_mip_bytes(data))
        fmt = 'dxt5'
    else:
        return AlphaInfo('no_alpha', fourcc.decode('latin-1', 'replace'))

    if s['rng'] < _MIN_RANGE:
        # Flat channel.  Measured on Nehrim: every flat one was fully WHITE
        # (mean 255), not black — do not assume an empty alpha reads as 0.
        return AlphaInfo('empty', fmt, **s)
    if s['mid_ratio'] < _MIN_MID_RATIO:
        return AlphaInfo('binary', fmt, **s)
    if s['edge_ratio'] >= _MAX_EDGE_RATIO:
        # Soft-edged cutout (vegetation, splatter): mostly extremes with a thin
        # transition. Enough mid-tones to pass the previous test, still a mask.
        return AlphaInfo('bimodal', fmt, **s)
    if s['levels'] < _MIN_LEVELS:
        # Right shape, not enough resolution to be a surface.  Checked LAST so
        # a coarse mask is still reported as the mask it is.
        return AlphaInfo('quantised', fmt, **s)
    return AlphaInfo('height', fmt, **s)


def decode_alpha_plane(data: bytes) -> 'tuple[int, int, bytearray] | None':
    """Top mip's alpha channel as (width, height, one byte per texel)."""
    if len(data) < 128 or data[:4] != _DDS_MAGIC:
        return None
    h = struct.unpack_from('<I', data, 12)[0]
    w = struct.unpack_from('<I', data, 16)[0]
    fourcc = data[84:88]
    payload = 128
    if fourcc == _FOURCC_DX10:
        dxgi = struct.unpack_from('<I', data, 128)[0]
        payload = 148
        fourcc = (_FOURCC_DXT3 if dxgi in _DXGI_BC2 else
                  _FOURCC_DXT5 if dxgi in _DXGI_BC3 else b'')
    if fourcc not in (_FOURCC_DXT3, _FOURCC_DXT5) or not w or not h:
        return None

    out = bytearray(w * h)
    bx, by = (w + 3) // 4, (h + 3) // 4
    pos = payload
    for byi in range(by):
        for bxi in range(bx):
            if pos + 16 > len(data):
                return None
            texels = [0] * 16
            if fourcc == _FOURCC_DXT5:
                a0, a1 = data[pos], data[pos + 1]
                if a0 > a1:
                    pal = (a0, a1,
                           (6 * a0 + a1) // 7, (5 * a0 + 2 * a1) // 7,
                           (4 * a0 + 3 * a1) // 7, (3 * a0 + 4 * a1) // 7,
                           (2 * a0 + 5 * a1) // 7, (a0 + 6 * a1) // 7)
                else:
                    pal = (a0, a1,
                           (4 * a0 + a1) // 5, (3 * a0 + 2 * a1) // 5,
                           (2 * a0 + 3 * a1) // 5, (a0 + 4 * a1) // 5,
                           0, 255)
                bits = int.from_bytes(data[pos + 2:pos + 8], 'little')
                for i in range(16):
                    texels[i] = pal[(bits >> (i * 3)) & 0x7]
            else:
                for i in range(8):
                    byte = data[pos + i]
                    texels[i * 2] = (byte & 0x0F) * 17
                    texels[i * 2 + 1] = (byte >> 4) * 17
            for ty in range(4):
                y = byi * 4 + ty
                if y >= h:
                    break
                for tx in range(4):
                    x = bxi * 4 + tx
                    if x < w:
                        out[y * w + x] = texels[ty * 4 + tx]
            pos += 16
    return w, h, out


def _encode_bc4_block(vals) -> bytes:
    """One 4x4 BC4 block.

    Byte-for-byte the same layout as a DXT5 ALPHA block: two 8-bit endpoints
    plus sixteen 3-bit indices.  That is why no external encoder is needed —
    the format is already understood from the decoding side.

    The palette index is COMPUTED, not searched.  With a0 = hi and a1 = lo the
    eight entries are hi, lo, then six evenly spaced steps from hi down to lo,
    so the nearest entry to v is found by quantising (hi - v) onto sevenths:
    step 0 is the endpoint hi (index 0), step 7 is the endpoint lo (index 1),
    and everything between is index step+1.  Searching all eight instead cost
    8x the inner-loop work — 0.34 s for one 512x512 texture, and this runs
    inside the mesh workers for every height map in the plugin.
    """
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return bytes((lo, lo, 0, 0, 0, 0, 0, 0))
    d = hi - lo
    half = d >> 1
    bits = 0
    shift = 0
    for v in vals:
        step = ((hi - v) * 7 + half) // d          # 0..7, nearest
        bits |= (0 if step == 0 else 1 if step == 7 else step + 1) << shift
        shift += 3
    return bytes((hi, lo)) + bits.to_bytes(6, 'little')


# --------------------------------------------------------------------------
# Output conditioning: half size, then blur, then the tone curve.
#
# Skyrim's parallax sampling is COARSER than Oblivion's -- verified in game by
# the author, who reports that an unsmoothed Oblivion height field reads as
# "comic" under Skyrim's stepping.  So every map is blurred, not just the ones
# a detector flags.
#
# The order matters and is the reason nothing needs recalibrating:
# `normalise_height` is not a fixed curve, it is a fit onto a MEASURED property
# of its input (the share of area within +/-FLAT_BAND of the median).  Run it
# LAST, on the pixels that actually ship, and it still lands on
# TARGET_FLAT_SHARE whatever the halving and the blur did to the field.
#
# Halving is also a straight win on the slowest step: `encode_bc4_dds` is pure
# Python, one 4x4 block at a time, inside the mesh workers -- a quarter of the
# pixels is a quarter of the blocks.
# --------------------------------------------------------------------------

# Blur radius in texels per 1000 texels of OUTPUT width, i.e. resolution
# relative.  A fixed pixel radius would hit a 512 map about eight times harder
# than a 4096 one, and this content ships both.  Radius is the kernel's
# half-width; sigma is a third of it, the usual truncation.
#
# 5.0 was the first in-game test and came back NOT ENOUGH -- Skyrim's parallax
# stepping still read as "comic".  The author asked for "at least 15, if not
# 20"; this takes the top of that range, because each retry costs them a full
# build-and-play cycle and an over-soft height field still reads as depth
# while an under-blurred one keeps the artifact.  Override per run with
# `tools/parallax_check.py regen --blur N` rather than editing this.
BLUR_RADIUS_PER_1000 = 20.0

# Linear size divisor applied before the blur.  A height field carries no fine
# detail worth keeping at diffuse resolution -- and see the encoder note above.
HEIGHT_DOWNSCALE = 2

# Mitchell-Netravali (B = C = 1/3) resampled for an exact 2x reduction: output
# texel i is centred on input 2i + 0.5, so the tap offsets are constant and the
# weights can be a literal.  Chosen over Lanczos deliberately -- Lanczos is too
# sharp for a height field that is already slightly soft, which is the whole
# point of the blur that follows.  The negative lobes are why the result is
# clipped back into 0..255.
_MITCHELL_TAPS = (-3, -2, -1, 0, 1, 2, 3)
_MITCHELL_WEIGHTS = np.array(
    [-5.0 / 288, 1.0 / 36, 77.0 / 288, 4.0 / 9, 77.0 / 288, 1.0 / 36,
     -5.0 / 288], dtype=np.float32)


def _as_array(w, h, plane):
    return np.frombuffer(bytes(plane), dtype=np.uint8).reshape(h, w)


def _to_plane(arr):
    return bytearray(np.clip(arr + 0.5, 0, 255).astype(np.uint8).tobytes())


def mitchell_halve(w, h, plane):
    """Halve a single-channel plane with a Mitchell filter.

    Separable and accumulated tap by tap rather than gathered into one big
    array: a 4096-square map would otherwise materialise a
    (4096, 2048, 7) float32 intermediate -- 234 MB, in each of nine workers.
    """
    nw, nh = max(1, w // 2), max(1, h // 2)
    if nw == w and nh == h:
        return w, h, plane
    a = _as_array(w, h, plane).astype(np.float32)

    tmp = np.zeros((h, nw), dtype=np.float32)
    xs = 2 * np.arange(nw)
    for d, wt in zip(_MITCHELL_TAPS, _MITCHELL_WEIGHTS):
        tmp += wt * a[:, np.clip(xs + d, 0, w - 1)]

    out = np.zeros((nh, nw), dtype=np.float32)
    ys = 2 * np.arange(nh)
    for d, wt in zip(_MITCHELL_TAPS, _MITCHELL_WEIGHTS):
        out += wt * tmp[np.clip(ys + d, 0, h - 1), :]

    return nw, nh, _to_plane(out)


def blur_radius_for(width: int, per_1000: float = None) -> float:
    """Resolution-relative blur radius for a map this wide."""
    if per_1000 is None:
        per_1000 = BLUR_RADIUS_PER_1000
    return per_1000 * width / 1000.0


def gaussian_blur(w, h, plane, radius: float):
    """Separable Gaussian with edge clamping.

    Padded slices rather than fancy indexing -- contiguous slicing is several
    times faster, and the kernel can reach 20 taps on a 4096 source.
    """
    if radius < 0.5 or w < 2 or h < 2:
        return plane
    r = int(math.ceil(radius))
    sigma = radius / 3.0
    xs = np.arange(-r, r + 1, dtype=np.float32)
    k = np.exp(-(xs * xs) / (2.0 * sigma * sigma))
    k /= k.sum()

    a = _as_array(w, h, plane).astype(np.float32)
    pad = np.pad(a, ((0, 0), (r, r)), mode='edge')
    tmp = np.zeros_like(a)
    for i, wt in enumerate(k):
        tmp += wt * pad[:, i:i + w]

    pad = np.pad(tmp, ((r, r), (0, 0)), mode='edge')
    out = np.zeros_like(tmp)
    for i, wt in enumerate(k):
        out += wt * pad[i:i + h, :]

    return _to_plane(out)


def _downsample(w, h, plane):
    """Box-filter to half size, for the mip chain."""
    nw, nh = max(1, w // 2), max(1, h // 2)
    out = bytearray(nw * nh)
    for y in range(nh):
        y0, y1 = min(2 * y, h - 1), min(2 * y + 1, h - 1)
        for x in range(nw):
            x0, x1 = min(2 * x, w - 1), min(2 * x + 1, w - 1)
            out[y * nw + x] = (plane[y0 * w + x0] + plane[y0 * w + x1] +
                               plane[y1 * w + x0] + plane[y1 * w + x1]) // 4
    return nw, nh, out


def encode_bc4_dds(w: int, h: int, plane, mipmaps: bool = True) -> bytes:
    """A single-channel BC4 DDS, with a full mip chain.

    BC4 is what Community Shaders recommends for height maps: one channel at
    the file size of BC1, without BC1's banding on grey gradients.  ENB reads
    it too.
    """
    levels = []
    cw, ch, cp = w, h, plane
    while True:
        blocks = bytearray()
        bx, by = (cw + 3) // 4, (ch + 3) // 4
        for byi in range(by):
            for bxi in range(bx):
                vals = []
                for ty in range(4):
                    y = min(byi * 4 + ty, ch - 1)
                    for tx in range(4):
                        x = min(bxi * 4 + tx, cw - 1)
                        vals.append(cp[y * cw + x])
                blocks += _encode_bc4_block(vals)
        levels.append(bytes(blocks))
        if not mipmaps or (cw == 1 and ch == 1):
            break
        cw, ch, cp = _downsample(cw, ch, cp)

    hdr = bytearray(128)
    hdr[0:4] = _DDS_MAGIC
    struct.pack_into('<I', hdr, 4, 124)                     # dwSize
    # CAPS | HEIGHT | WIDTH | PIXELFORMAT | LINEARSIZE | MIPMAPCOUNT
    struct.pack_into('<I', hdr, 8, 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000 |
                     (0x20000 if len(levels) > 1 else 0))
    struct.pack_into('<I', hdr, 12, h)
    struct.pack_into('<I', hdr, 16, w)
    struct.pack_into('<I', hdr, 20, len(levels[0]))         # linear size
    struct.pack_into('<I', hdr, 28, len(levels))            # mip count
    struct.pack_into('<I', hdr, 76, 32)                     # pf size
    struct.pack_into('<I', hdr, 80, _DDPF_FOURCC)
    hdr[84:88] = _FOURCC_DX10
    struct.pack_into('<I', hdr, 108, 0x1000 |               # TEXTURE
                     (0x400000 | 0x8 if len(levels) > 1 else 0))
    dx10 = struct.pack('<IIIII', _DXGI_BC4_UNORM, 3, 0, 1, 0)
    return bytes(hdr) + dx10 + b''.join(levels)


def _mip_chain(w, h, count):
    dims = []
    for _ in range(max(1, count)):
        dims.append((w, h))
        if w == 1 and h == 1:
            break
        w, h = max(1, w // 2), max(1, h // 2)
    return dims


def _bc1_repair_modes(color):
    """Make DXT3/DXT5 color blocks legal as DXT1.  `color` is (n, 8) uint8.

    DXT1 reads a block with ``c0 <= c1`` as three colors plus TRANSPARENT
    black; DXT3/DXT5 color blocks are always four opaque colors.  Both
    repairs below are exact -- no texel changes color:

      c0 <  c1  swap the endpoints and flip the low bit of every 2-bit index
                (0<->1, 2<->3).  The swapped palette names the same four
                colors in a different order, so the flip restores each texel.
      c0 == c1  every palette entry already equals c0, whatever the indices
                say, so zeroing them reproduces the block and steps around the
                transparent slot.
    """
    n = color.shape[0]
    u16 = color.view('<u2').reshape(n, 4)
    c0, c1 = u16[:, 0], u16[:, 1]
    idx = np.ascontiguousarray(color[:, 4:8]).view('<u4').reshape(n)

    less = c0 < c1
    equal = c0 == c1
    nc0 = np.where(less, c1, c0).astype('<u2')
    nc1 = np.where(less, c0, c1).astype('<u2')
    nidx = np.where(less, idx ^ np.uint32(0x55555555), idx).astype('<u4')
    nidx = np.where(equal, np.uint32(0), nidx).astype('<u4')

    out = np.empty((n, 8), dtype=np.uint8)
    out[:, 0:2] = nc0.view(np.uint8).reshape(n, 2)
    out[:, 2:4] = nc1.view(np.uint8).reshape(n, 2)
    out[:, 4:8] = nidx.view(np.uint8).reshape(n, 4)
    return out.tobytes()


def strip_alpha_to_bc1(data: bytes):
    """Re-container a DXT3/DXT5 diffuse as DXT1, dropping the alpha channel.

    This is NOT a recompression.  A DXT3/DXT5 block is 8 bytes of alpha
    followed by 8 bytes of color in exactly BC1's color-block layout, so the
    color half is copied verbatim and the endpoints keep the values the
    original encoder chose.  Dithering and perceptual error metrics have
    nothing to act on: nothing is being quantised, and decoding to RGB just to
    re-quantise would LOSE quality rather than gain it.

    Only ever called for a diffuse whose alpha was classified ``height`` and
    carried out to a `_p` map, so the channel being dropped is a height field,
    never transparency.

    Returns DDS bytes, or None if `data` is not DXT3/DXT5 (a DXT1 input is
    already stripped, which makes a re-run a no-op).
    """
    if len(data) < 128 or data[:4] != _DDS_MAGIC:
        return None
    if not (struct.unpack_from('<I', data, 80)[0] & _DDPF_FOURCC):
        return None
    fourcc = data[84:88]
    payload = 128
    if fourcc == _FOURCC_DX10:
        if len(data) < 148:
            return None
        dxgi = struct.unpack_from('<I', data, 128)[0]
        payload = 148
        if dxgi in _DXGI_BC2:
            fourcc = _FOURCC_DXT3
        elif dxgi in _DXGI_BC3:
            fourcc = _FOURCC_DXT5
        else:
            return None
    if fourcc not in (_FOURCC_DXT3, _FOURCC_DXT5):
        return None

    h = struct.unpack_from('<I', data, 12)[0]
    w = struct.unpack_from('<I', data, 16)[0]
    if not w or not h:
        return None

    levels = []
    off = payload
    for mw, mh in _mip_chain(w, h, struct.unpack_from('<I', data, 28)[0]):
        n = ((mw + 3) // 4) * ((mh + 3) // 4)
        end = off + n * 16
        if end > len(data):
            break
        blocks = np.frombuffer(data[off:end], dtype=np.uint8).reshape(n, 16)
        levels.append(_bc1_repair_modes(np.ascontiguousarray(blocks[:, 8:16])))
        off = end
    if not levels:
        return None

    hdr = bytearray(data[:128])
    flags = struct.unpack_from('<I', hdr, 8)[0] | 0x80000    # LINEARSIZE
    flags &= ~0x8                                            # not PITCH
    flags = (flags | 0x20000) if len(levels) > 1 else (flags & ~0x20000)
    struct.pack_into('<I', hdr, 8, flags)
    struct.pack_into('<I', hdr, 20, len(levels[0]))          # linear size
    struct.pack_into('<I', hdr, 28, len(levels))             # mip count
    struct.pack_into('<I', hdr, 76, 32)                      # pf size
    struct.pack_into('<I', hdr, 80, _DDPF_FOURCC)            # no alpha flags
    hdr[84:88] = _FOURCC_DXT1
    struct.pack_into('<I', hdr, 108, 0x1000 |
                     (0x400000 | 0x8 if len(levels) > 1 else 0))
    return bytes(hdr) + b''.join(levels)


def strip_diffuse_alpha(tex_root, keep=()) -> 'tuple[int, int, int, int]':
    """Drop the height-carrying alpha from every converted diffuse.

    The presence of `<name>_p.dds` beside `<name>.dds` IS the record that this
    texture's alpha was a height field — the mesh stage already decided that
    and wrote the map.  Keying off it needs no plumbing from the workers, and
    makes a non-parallax build a no-op by construction.

    `keep` is the set of texture paths some shape reads as OPACITY (collected
    by the mesh stage; paths relative to the plugin's texture root, lowercased
    with backslashes).  A shape blending or testing against the channel is
    evidence it is not a height field there, whatever the texture-level
    classifier said, so those are left as they are.

    Runs AFTER the texture copy, like the landscape-normal fix, so a re-copy
    cannot resurrect the DXT5 versions.

    Returns (converted, skipped, kept, bytes_saved).
    """
    keep = {k.replace('/', '\\').lower() for k in (keep or ())}
    root = os.path.abspath(str(tex_root))
    converted = skipped = kept = saved = 0
    for dirpath, _, files in os.walk(root):
        have = {f.lower() for f in files}
        for fn in files:
            low = fn.lower()
            if not low.endswith('.dds') or low.endswith('_p.dds'):
                continue
            if low[:-4] + '_p.dds' not in have:
                continue
            path = os.path.join(dirpath, fn)
            if keep:
                # The mesh stage names textures the way the NIFs do
                # (`textures\tes4\...`), so compare on that tail.
                rel = os.path.relpath(path, root).replace('/', '\\').lower()
                if ('textures\\tes4\\' + rel) in keep or rel in keep:
                    kept += 1
                    continue
            try:
                with open(path, 'rb') as f:
                    data = f.read()
            except OSError:
                continue
            blob = strip_alpha_to_bc1(data)
            if blob is None:
                skipped += 1
                continue
            tmp = f'{path}.{os.getpid()}.tmp'
            try:
                with open(tmp, 'wb') as f:
                    f.write(blob)
                os.replace(tmp, path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                continue
            saved += len(data) - len(blob)
            converted += 1
    return converted, skipped, kept, saved


def height_path(diffuse_rel: str) -> str:
    """`textures\\x\\stone.dds` -> `textures\\x\\stone_p.dds`."""
    if diffuse_rel.lower().endswith('.dds'):
        return diffuse_rel[:-4] + '_p.dds'
    return diffuse_rel + '_p.dds'


def _median(texels):
    srt = sorted(texels)
    return srt[len(srt) // 2]


# How much of a surface must sit within +/-`FLAT_BAND` levels of its own
# median: the shape difference between a hand-tuned height map and Nehrim's raw
# alpha, stated so that no outlier can move it.
#
# 🔴 THE MEASURE THIS REPLACED WAS WRONG, and it took the author pointing at a
# rendered map to see it.  "Share of texels in the bottom third of min..max"
# takes its threshold from the EXTREMES, so a couple of bright texels stretch
# the range and drag the whole surface into the deep band.
# `leyawiinmetalstrip03` — a flat plate with two rivets — scored 94.2% "deep",
# while 93.7% of its area sits within +/-20 of its median, p95 is 83, and the
# amplitude of 146 comes almost entirely from the rivets (p99 jumps to 132).
#
# Recomputed outlier-proof over the same 56 pairs (`temp/parallax_pairs.csv`):
#
#                                hand-tuned   Nehrim
#     deep third (min..max)            12.1     39.1  <- what was calibrated on
#     deep third (p05..p95)            25.8     37.9  <- honest, barely splits
#     share within +/-20 of median     63.2     36.6  <- clean AND robust
#
# The middle row is the finding: once the range is made robust, the deep-third
# figure stops separating the two populations at all.  Only the band measure
# does, and it says the same thing the eye does — the hand-tuned wall is a flat
# face with narrow mortar grooves, Nehrim's is restless everywhere.
#
# 🔴 Scaling and shifting cannot produce this.  Shifting is linear and moves
# the median with the body; scaling DOES change an absolute band, which is why
# `target_range` carries part of the correction, but reaching 63% by scaling
# alone would mean an amplitude far below the ~150 the hand-tuned maps ship at.
# The rest has to come from a curve.
#
# The target itself is the median of the WHOLE reference corpus, 3631 height
# fields rather than the 56 paired ones:
#
#     both folders   p05 34.7   p25 49.0   median 63.3   p75 73.4   p95 84.8
#       folder A     p05 49.9   p25 62.5   median 69.6   p75 76.1   p95 85.7
#       folder B     p05 29.3   p25 43.0   median 51.3   p75 66.6   p95 83.1
#
# 🔴 Read the two folder rows before touching this number.  The same author
# normalises to 69.6% in one set and 51.3% in the other — the SAME split that
# made the amplitude cap dangerous (medians 145 and 153 there).  63 is the
# pooled median and sits between them, which is the honest choice for a target;
# calibrating on either folder alone would land 6-12 points off.
#
# And the spread is wide on purpose: this is a central tendency, not a law.
# Half the reference corpus sits below 63%, so the target says "as flat as a
# typical hand-made map", not "flatter than every hand-made map".  It is a
# CURVE TARGET only — as a DETECTOR the same figure fails badly, see
# `DEFAULT_MAX_RANGE`.
#
# 🔵 Set to 0.68 rather than the pooled 63.3 on the author's in-game verdict:
# "the maps are OK, they could go a touch flatter".  0.68 sits between the
# pooled median and folder A's 69.6 — still inside the hand-tuned population,
# in the direction of the author's own newer set.
#
# What that buys is bounded, and measured: over the 38 maps the median flat
# share goes 63.4% -> 65.4%, and it does not move again at 0.70, 0.75 or 0.80.
# `_MIN_BODY_LEVELS` — not the target — is what stops the curve there.  So this
# dial saturates by design, which is the property that makes turning it safe.
FLAT_BAND = 20
TARGET_FLAT_SHARE = 0.68


def _cumulative(texels):
    """(cumulative 256-bin histogram, total) — one O(n) pass.

    Everything the fit needs is a count per level, so building this once turns
    every bisection step below into O(1) arithmetic instead of a pass over the
    texels.  It also replaces the sort `_median` used to do on this path.
    """
    hist = [0] * 256
    for v in texels:
        hist[v] += 1
    cum = [0] * 256
    run = 0
    for i, k in enumerate(hist):
        run += k
        cum[i] = run
    return cum, run


def _median_from(cum, total):
    """Same element `_median` picks: the (n//2)-th, zero-based."""
    want = total // 2 + 1
    for i, c in enumerate(cum):
        if c >= want:
            return i
    return 255


# Largest exponent the flattening curve may use, and where the ceiling comes
# from.  First, why the curve is shaped the way it is.
#
# 🔴 `x**g` — the curve this replaced — CANNOT do this job, which is worth
# recording so it is not tried again.  It compresses one END of the range, so
# the share inside a band around the median is not even monotone in g:
# measured over all 38 maps, 21 of them DIP before they rise as g falls, so a
# bisection has nothing to bisect on.  And inside the range its own
# posterisation floor allows (g >= 0.63 at amplitude 255) the median share only
# moves 53% -> 56%.  The 63% target is out of reach for that family entirely.
#
# This curve works on the DISTANCE from the median instead:
#
#     y = med + sign(d) * D * (|d| / D) ** p          d = v - med
#
# with D taken per side, so `lo` -> `lo` and `hi` -> `hi` exactly and the
# amplitude is untouched.  p > 1 presses the body together and steepens the
# tails — a flat face with narrow deep grooves.  Two properties `x**g` lacked:
#
#   * MONOTONE in p by construction.  Raising p moves every texel weakly
#     CLOSER to the median, so the share inside any band can only grow.  That
#     is what makes the bisection valid.
#   * It cannot punch holes.  Its steepest slope is p, at the ENDS, where
#     `x**g` had UNBOUNDED slope at 0 — that unbounded slope is what turned
#     cave04 into grey plateaus with black holes punched through it.  The
#     linear compression that follows scales even that down by `f`.
#
# The risk this family DOES carry is the opposite one: too large a p presses
# the body dead flat, and a face with no relief left is as wrong as a restless
# one.  So the ceiling is derived from the body rather than chosen: after the
# curve and the linear step, the +/-FLAT_BAND band must still span at least
# `_MIN_BODY_LEVELS` distinct output levels.
#
# 21 is the AUTHORED floor, not a round number.  Over all 3631 hand-tuned
# reference height fields, counting the distinct levels actually occupied
# inside each map's own +/-20 band:
#
#     min 21   p05 25   median 41   p95 41   max 41
#
# A +/-20 band spans 41 levels, so the median hand-tuned map uses EVERY one of
# them and not one map in the corpus drops below 21.  Holding a corrected map
# to that floor means it is never left with a poorer face than the poorest map
# the author shipped.  (The guard measures the band's WIDTH, which is an upper
# bound on occupied levels — taking the floor of the reference population
# rather than a comfortable round number is what pays for that optimism.)
#
# `_MAX_FLATTEN_P` is only the bisection's upper bracket, for the degenerate
# case where a field is narrower than the band and no compression is needed at
# all.  It is not a tuning knob: the level guard is what actually binds.
_MIN_BODY_LEVELS = 21
_MAX_FLATTEN_P = 4.0


def _flatten(texels, lo, hi, med, p):
    """Apply the curve above.  `lo`, `hi` and `med` all map to themselves."""
    dlo, dhi = med - lo, hi - med
    curve = [0.0] * 256
    for v in range(256):
        d = v - med
        if d < 0 and dlo > 0:
            curve[v] = med - dlo * ((-d / dlo) ** p)
        elif d > 0 and dhi > 0:
            curve[v] = med + dhi * ((d / dhi) ** p)
        else:
            curve[v] = float(med)
    return bytearray(int(round(curve[v])) for v in texels)


def _flat_share_at(cum, total, lo, hi, med, band, p):
    """Share within +/-band of the median AFTER the curve, without applying it.

    The curve is monotone and fixes the median, so the texels that land inside
    the band are exactly those between the band edges' pre-images — and those
    come from inverting the curve, which is the same expression with 1/p.
    """
    dlo, dhi = med - lo, hi - med
    q = 1.0 / p
    top = med + (dhi * (band / dhi) ** q if 0 < band < dhi else dhi)
    bot = med - (dlo * (band / dlo) ** q if 0 < band < dlo else dlo)
    a = max(0, int(math.ceil(bot)))
    b = min(255, int(math.floor(top)))
    if b < a or not total:
        return 0.0
    return (cum[b] - (cum[a - 1] if a else 0)) / total


def _p_ceiling(dlo, dhi, band, f):
    """Largest p that still leaves `_MIN_BODY_LEVELS` inside the band.

    On the output scale the band's half width becomes ``f * D * (band/D)**p``,
    so requiring that to stay above half the level budget pins p from above:

        f * D * (band/D)**p >= _MIN_BODY_LEVELS / 2
        p <= ln(H / (f*D)) / ln(band/D)        — both logs are negative

    A side narrower than the band is not compressed by the curve at all
    (``(band/D)**p`` is then >= 1), so it places no limit.  At p = 1 the band
    is always exactly `FLAT_BAND` wide on the output, i.e. 40 levels, so the
    guard can never forbid leaving the map alone.
    """
    half = _MIN_BODY_LEVELS / 2.0
    cap = _MAX_FLATTEN_P
    for d in (dlo, dhi):
        if d <= band or d <= 0:
            continue
        out = f * d
        if out <= half:
            return 1.0
        cap = min(cap, math.log(half / out) / math.log(band / d))
    return max(1.0, cap)


def _fit_flatten(cum, total, lo, hi, med, band, target, f):
    """Smallest p that puts `target` of the surface inside the band.

    A texture that simply IS restless cannot be dragged onto the target
    without pressing its face flat, so the curve goes as far as the level
    guard allows and no further.  A partial correction beats a destroyed map —
    the same trade the old gamma floor made, for the same reason.
    """
    if hi <= lo or not total:
        return 1.0
    if _flat_share_at(cum, total, lo, hi, med, band, 1.0) >= target:
        return 1.0                          # already flat enough: hands off
    pmax = _p_ceiling(med - lo, hi - med, band, f)
    if pmax <= 1.0:
        return 1.0
    if _flat_share_at(cum, total, lo, hi, med, band, pmax) < target:
        return pmax                         # unreachable: go as far as allowed
    p_lo, p_hi = 1.0, pmax
    for _ in range(24):
        p = (p_lo + p_hi) / 2.0
        if _flat_share_at(cum, total, lo, hi, med, band, p) >= target:
            p_hi = p
        else:
            p_lo = p
    return p_hi


# Amplitude a height field may span before it is compressed, and the ONE
# number in this module that was calibrated rather than chosen.
#
# A hand-made height map works in BOTH engines: the user's own maps are
# authored for Oblivion, rebuilt from the normal maps and hand-tuned, and they
# render correctly in Skyrim too.  So a mod shipping good maps must come
# through this conversion untouched, and only Oblivion/Nehrim's own raw alphas
# get corrected.  Calibrated on 56 PAIRS — the same texture, hand-tuned on one
# side and Nehrim's original on the other (`temp/parallax_pairs.csv`):
#
#     hand-tuned : min 130  p25 144  median 146  p75 147  max 148
#     Nehrim     : min  95  p25 159  median 203  p75 254  max 255
#
#     cap 148 -> 100% of the hand-tuned set untouched, 83% of Nehrim's fixed
#
# Then TWO whole reference folders were measured, not just the paired subset,
# and they do not agree with each other:
#
#     folder A  1738 height fields   median 145   max 148
#     folder B  1893 height fields   median 153   max 156
#
# The same author normalises to ~145 in one set and ~153 in the other, so
# "amplitude below X" is a CONVENTION test, not a law.  A cap of 150 — which
# folder A alone appeared to justify — would have compressed all 1893 maps in
# folder B.  That is exactly the damage this constant exists to prevent, and
# it was caught only because a second folder got checked.
#
# Nehrim's over-deep textures start at 159, and the ones that actually drew
# the complaint are 169 and up (`wandb` 171).  The two populations are barely
# fifteen steps apart, so the threshold goes in the MIDDLE of that gap: 163
# clears folder B's ceiling by seven and still catches everything from 169 up.
# No texture on either side falls between 156 and 169, so the exact value
# inside that window changes no behaviour — it only buys margin.
#
# 🔴 Treat this as FRAGILE.  It separates two populations ~15 steps apart, and
# a mod that normalises to 170 WOULD be compressed.
#
# But it is the BEST AVAILABLE, and that was measured rather than assumed.
# The obvious upgrade candidate is the tone-curve figure — whichever one is in
# use — and as a DETECTOR it is worse.  Measured with the deep-third share over
# 3631 good height fields vs Nehrim's 56:
#
#     amplitude   > 163      keeps 100% of good, catches 73% of bad
#     deep share  >  20%     keeps  88%,          catches 80%
#     deep share  >  40%     keeps  98%,          catches 48%
#     amp>163 OR deep> 25%   keeps  93%,          catches 92%
#     amp>163 OR deep> 50%   keeps  98%,          catches 80%
#
# Amplitude is the only rule that keeps ALL of them.  The reason is in the
# spread: the hand-tuned deep share ran 0..94% (median 6.3), so some of those
# maps look legitimately almost black and no threshold can tell them from
# Nehrim's.  Their amplitude, by contrast, stops dead at 156.
#
# That measurement is also the one the outlier problem hit hardest — a share
# taken off min..max is exactly what a pair of bright rivets distorts, so the
# 0..94% spread above overstates how dark the hand-tuned set really is (see
# `TARGET_FLAT_SHARE`).  The conclusion survives it: the band measure separates
# the two populations 63% against 37%, which is a clean split for a CURVE
# TARGET but nowhere near the "keeps 100% of good" a detector has to manage.
# Do not swap the detector for the curve's figure; it has been tried twice.
#
# What this is NOT: the median's distance from mid-grey.  That was the obvious
# theory and the same 56 pairs refute it — hand-tuned medians scatter 86..132
# and Nehrim's overlap them, so any tolerance that spares the good maps also
# spares half the bad ones (tol 40: 96% of theirs kept, only 53% of ours
# corrected).  Do not reintroduce a centring step without new evidence.
DEFAULT_MAX_RANGE = 163

# Where a corrected field's median is moved to.  Also measured on the 56
# pairs: the hand-tuned medians sit in a TIGHT band and Nehrim's do not.
#
#     hand-tuned : min 85  p25 102  median 117  p75 126  max 145
#     Nehrim     : min  0  p25  63  median  93  p75 138  max 216
#     in 100..135:  73% of the hand-tuned set, 23% of Nehrim's
#
# 117 is the middle of that band — just under mid-grey, not on it.
#
# 🔴 This is a CORRECTION, never a DETECTOR.  Whether a map needs work is
# decided by amplitude alone; the medians of the two sets overlap far too
# much to tell them apart (any tolerance sparing 96% of the good maps also
# spares 53% of Nehrim's).  So the shift is applied ONLY to a field the
# amplitude cap already condemned — a map that passes the cap is returned
# untouched, median and all.
TARGET_MEDIAN = 117

# How dark a field's median may be before it is corrected FOR THAT ALONE.
#
# This is the second detector, and it exists because the band measure answered
# the `durchgangD` question with a clear NO.  That texture — a practically
# black wall, median 17, amplitude 158 — reads as **89.2% flat** on the new
# measure, and correctly so: it IS flat, just parked entirely at the bottom.
# Restlessness and off-centredness are two different defects and the curve
# only fixes the first.
#
# The mechanism is why it matters.  A parallax shader offsets along the view
# vector by (height - neutral), so a field whose whole surface sits near 0 does
# not render as "deep" — it renders as a CONSTANT view-dependent UV shift, i.e.
# the texture slides across the wall as the camera moves.  That is the same
# swimming artefact an empty height map produces, which is exactly why an empty
# one is refused outright (see the module docstring).
#
# 🔴 Centring was rejected once, and reintroducing it needed new evidence.
# The old refutation stands for what it tested: a TOLERANCE AROUND MID-GREY,
# measured on the 56 pairs, where the medians overlap so badly that sparing 96%
# of the good maps also spares 53% of Nehrim's.  This is a different rule — a
# ONE-SIDED FLOOR — and the evidence is the full 3631-map reference corpus
# rather than 56 of them:
#
#     median level   hand-tuned : min  52  p05  94  median 125  p95 175
#                    Nehrim     : min  17  p05  31  median 105  p95 169
#
#     floor < 45   touches 0 of 3631 hand-tuned (0.00%), catches 4 Nehrim maps
#     floor < 60   touches 3 of 3631 (0.08%)
#
# NOTHING the author shipped is darker than 52.  The four maps this catches are
# `durchgangD` (17), `durchgangA` (31), `decked` (32) and `bodend` (36) — the
# exact set that sits just under the amplitude threshold at 153..159 and was
# left shipping untouched.  45 sits in the middle of the empty gap between 36
# and 52, the same way `DEFAULT_MAX_RANGE` sits in the gap between 156 and 169.
#
# What a map caught by THIS rule alone gets is deliberately minimal: the
# re-centring shift and nothing else.  No compression, no tone curve.  A pure
# translation cannot damage relief — every level, every gradient and every gap
# survives it — so the fix reaches the defect and stops there.  The amplitude
# detector remains the only thing that may compress a field.
MIN_MEDIAN = 45

# Rejected by measurement, so it is not tried again: clamping the tails.  The
# theory was that Nehrim's amplitude comes from a few extreme texels a p1..p99
# clamp could shave.  It does not — core(p5..p95)/full is 0.68 for Nehrim and
# 0.54 for the hand-tuned set, i.e. Nehrim's depth sits in the BODY of the
# surface and the hand-tuned maps have relatively MORE tail.  A p1..p99 clamp
# alone leaves Nehrim's amplitude at 83% and brings only 39% under the cap.


# How deep a CORRECTED field is taken, as opposed to which fields get
# corrected at all.  These must be two separate numbers.
#
# `max_range` is the DETECTOR and is pinned by the hand-tuned set (130..148):
# drop it below 148 and the conversion starts flattening maps a modder built
# on purpose, which is the one thing this must never do.
#
# `target_range` is how far a condemned map is taken, and it is free to go
# lower.  It exists because measurement failed to explain the eye: Nehrim's
# `wandb` corrected to amplitude 150 still reads as too strong in game while
# the hand-tuned version at 143 reads as right — and neither amplitude (5%
# apart), steepness (ours is 0.70x theirs in UV space) nor a material depth
# parameter (shader type 3 has none, and Community Shaders exposes only
# on/off switches) accounts for the difference.  So the depth of a corrected
# map is a dial set by eye, and keeping it separate means turning it does not
# cost anything a mod authored well.
DEFAULT_TARGET_RANGE = 0        # 0 = same as max_range


def normalise_height(texels, strength: float = 1.0,
                     max_range: int = DEFAULT_MAX_RANGE,
                     target_median: int = TARGET_MEDIAN,
                     target_range: int = DEFAULT_TARGET_RANGE,
                     flat_target: float = TARGET_FLAT_SHARE,
                     min_median: int = MIN_MEDIAN):
    """Compress an over-deep height field — and leave a good one ALONE.

    Both engines read the channel identically (white out, black in, mid-grey
    neutral), so nothing here reinterprets the data.  What differs is the
    DEPTH the shader gives it: Community Shaders computes
    ``maxHeight = 0.1 * scale`` from an engine parameter no texture can
    influence, and Oblivion's figure is unmeasurable from here (its parallax
    lives in compiled shader packages).  Same map, deeper result.

    `max_range`  the cap above.  A field already inside it is returned
                 unchanged, bit for bit — that is the property that protects
                 a mod's own maps.  0 disables the cap.
    `strength`   an extra uniform factor on top, 1.0 for none.  Reach for the
                 cap first: a uniform factor is set by the worst offender and
                 flattens the mild textures by the same amount, which throws
                 away authored relief (a plaster wall is meant to be flatter
                 than a cave wall).
    `target_median`  where a CORRECTED field's median is moved to; 0 leaves
                 it where it is.  Applies only when the cap or the strength
                 already decided this map needs work.
    `target_range`  how deep a CORRECTED field is taken; 0 means "to
                 max_range".  Separate from the detector on purpose — see the
                 constant — so the depth can be dialled down by eye without
                 ever reaching a map the detector let through.
    `flat_target`  how much of a CORRECTED field's surface the tone curve
                 aims to leave within +/-`FLAT_BAND` of its median; 0 disables
                 the curve.  This is the SHAPE correction, and it is the only
                 one that changes how restless a surface reads.
    `min_median`  the SECOND detector: a field whose median sits below this is
                 parked at the bottom of the channel and gets the re-centring
                 shift ALONE — no compression, no curve.  0 disables it.

    The tone curve runs first, then the compression around the field's own
    median so the relief keeps its shape, and the shift last — limited so
    nothing is pushed past 0 or 255, because clipping would flatten real
    relief into a plateau.
    """
    if not texels:
        return texels
    lo, hi = min(texels), max(texels)
    rng = hi - lo
    cum, total = _cumulative(texels)
    med = _median_from(cum, total)

    f = float(strength)
    if max_range and rng > max_range:
        # detected as over-deep -> take it down to target_range, not merely
        # to the detection threshold
        f = min(f, (target_range or max_range) / rng)
    # Sunk below anything the reference corpus contains: a DIFFERENT defect,
    # and it earns a strictly smaller correction.  See MIN_MEDIAN.
    off_centre = bool(min_median and target_median and med < min_median)
    if f >= 1.0 and not off_centre:
        return texels                      # already fine: do not touch it

    # Shape first: press the body of the surface together around its own
    # median so only the genuine grooves stay deep.  Done before the linear
    # steps because it is what actually changes how the surface reads; the cap
    # and the shift then place the reshaped field.
    #
    # The band is expressed on the OUTPUT scale.  `f` is a uniform factor
    # about the median, so a texel ends up within FLAT_BAND of the output
    # median exactly when it is within FLAT_BAND/f of the median here — no
    # approximation, which is why the fit can be done before the scaling.
    #
    # Skipped entirely when only the median floor fired: that field's SHAPE was
    # never in question, and the amplitude detector stays the only thing
    # allowed to decide a map needs reshaping.
    if flat_target and rng > 0 and f < 1.0:
        p = _fit_flatten(cum, total, lo, hi, med, FLAT_BAND / f, flat_target,
                         f)
        if p > 1.0:
            texels = _flatten(texels, lo, hi, med, p)
            # The curve fixes `lo`, `hi` and the median exactly and is
            # monotone, so none of the three needs recomputing.

    shift = 0.0
    if target_median:
        lo_s = med + (lo - med) * f
        hi_s = med + (hi - med) * f
        shift = target_median - med
        shift = max(shift, -lo_s)          # keep the darkest texel >= 0
        shift = min(shift, 255.0 - hi_s)   # keep the brightest <= 255

    out = bytearray(len(texels))
    for i, v in enumerate(texels):
        nv = int(round(med + (v - med) * f + shift))
        out[i] = 0 if nv < 0 else 255 if nv > 255 else nv
    return out


# --------------------------------------------------------------------------
# The global depth scale.
#
# Everything above this point is a CORRECTION: it detects a map that is wrong
# and leaves the rest bit-identical, which protects a mod author's own
# calibration.  This is different again -- it runs on EVERY map, and it is the
# SAME transform for all of them, because Oblivion's authored depth reads far
# too bumpy under Skyrim's parallax however well it was calibrated for
# Oblivion.
#
# GLOBAL is the whole point.  Normalising each map to its own target amplitude
# would make a plaster wall exactly as deep as a cave wall and throw away the
# relief the author actually authored (the same trap `normalise_height` warns
# about under `strength`).  One factor for every map keeps every relationship
# between two textures intact and only bounds how far the shader pushes.
#
# 128 is the pivot, and it is not a guess: Community Shaders pivots the height
# on 0.5 twice over -- `AdjustDisplacementNormalized` returns
# `(displacement - 0.5) * scale + 0.5 + offset`, and the POM ray starts at
# `minHeight = maxHeight * 0.5`.  Above 128 a surface pushes OUT, below it
# pushes IN, so compressing toward 128 reduces displacement in both directions
# without turning grooves into bumps.
#
# This is the same operation as the Output Levels in the author's own
# TES4N2HGenerator (Output Black 26 / Output White 165, clamp 26..179) -- and
# those maps measure 30..179 in the shipped pack, so the band is already there,
# just calibrated for Oblivion's much gentler offset mapping.
#
# The factor has NO vanilla anchor to census: our vanilla reference cache holds
# zero `_p.dds`, Bethesda having all but dropped parallax for SSE.  0.6 is the
# value the author confirmed in game (2026-08-19) after 0.5 read a touch flat;
# on the reference wall it takes the authored amplitude 145 down to about 76.
# Retune by eye with `tools/parallax_check.py regen --depth F`.
NEUTRAL_LEVEL = 128
DEPTH_SCALE = 0.6


def scale_depth(texels, factor: float = None, centre: int = NEUTRAL_LEVEL):
    """Compress every height field toward the shader's neutral plane.

    One affine map, identical for every texture:

        v' = centre + (v - centre) * factor

    so relative depth WITHIN a map and BETWEEN maps both survive exactly; only
    the absolute excursion shrinks.  `factor` 1.0 (or None -> DEPTH_SCALE)
    leaves the field alone; 0.5 halves how far the surface travels.
    """
    if not texels:
        return texels
    f = DEPTH_SCALE if factor is None else float(factor)
    if f == 1.0:
        return texels
    lut = bytes(max(0, min(255, int(round(centre + (v - centre) * f))))
                for v in range(256))
    return bytearray(lut[v] for v in texels)


def height_report(texels):
    """(median, amplitude, percent below mid-grey) — the three numbers the
    good/bad split is made on, so a build log can show its working."""
    if not texels:
        return 0, 0, 0.0
    n = len(texels)
    below = sum(1 for v in texels if v < 128) * 100.0 / n
    return _median(texels), max(texels) - min(texels), below


def build_height_map(src_dds: str, out_path: str, strength: float = 1.0,
                     max_range: int = DEFAULT_MAX_RANGE,
                     target_range: int = DEFAULT_TARGET_RANGE,
                     blur_per_1000: float = None,
                     depth: float = None) -> bool:
    """Write the diffuse's alpha channel out as a BC4 height map.

    Called from the mesh converter once per height texture.  Mesh conversion is
    a process POOL and several meshes share a diffuse, so two workers can reach
    the same output at once: the bytes go to a per-process temp name and are
    moved into place with :func:`os.replace`, which is atomic on NTFS.  A reader
    therefore never sees a half-written DDS, and the loser of the race simply
    overwrites identical content.
    """
    try:
        with open(src_dds, 'rb') as f:
            data = f.read()
    except OSError:
        return False
    plane = decode_alpha_plane(data)
    if plane is None:
        return False
    w, h, texels = plane
    if HEIGHT_DOWNSCALE == 2:
        w, h, texels = mitchell_halve(w, h, texels)
    texels = gaussian_blur(w, h, texels, blur_radius_for(w, blur_per_1000))
    # Tone curve before the band: it fits onto a MEASURED share of the
    # field, and fit_to_band is linear, so the calibrated TARGET_FLAT_SHARE
    # survives the rescale untouched.
    texels = normalise_height(texels, strength, max_range, TARGET_MEDIAN,
                              target_range)
    # Global depth scale, same factor on every map -- see scale_depth.
    texels = scale_depth(texels, depth)
    blob = encode_bc4_dds(w, h, texels)

    d = os.path.dirname(out_path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f'{out_path}.{os.getpid()}.tmp'
    try:
        with open(tmp, 'wb') as f:
            f.write(blob)
        os.replace(tmp, out_path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True
