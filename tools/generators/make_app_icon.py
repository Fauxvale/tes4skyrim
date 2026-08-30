"""Rebuild the GUI app icon: Dwemer-brass lexicon cube with blue runes.

Usage:
    python tools/generators/make_app_icon.py [SRC.webp] [DST.ico]

Defaults: docs/lexicon.webp -> docs/favicon.ico (plus a full-size
docs/lexicon_dwemer.png preview beside the source, for eyeballing the tint).

The source is a near-black stone cube covered in saturated red glowing runes.
Two independent recolors, split by how saturated-and-red a pixel is:

  * rune pixels  -> hue rotated to a cold blue (216 deg)
  * stone pixels -> mapped to the Dwemer brass ramp measured from the vanilla
    Dwemer puzzle cube (hue ~26 deg, sat ~0.9), with the source luminance
    driving position along the ramp so the carved relief survives.
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from tools.generators import dwemer_palette  # noqa: E402

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/lexicon.webp")
DST = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/favicon.ico")

BLUE_HUE = dwemer_palette.RUNE_HUE_DEG / 360.0
RED_ARC = dwemer_palette.RUNE_RED_ARC_DEG / 360.0


def rgb_to_hsv(rgb):
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx, mn = rgb.max(-1), rgb.min(-1)
    d = mx - mn
    s = np.where(mx > 0, d / np.maximum(mx, 1e-6), 0.0)
    h = np.zeros_like(mx)
    nz = d > 1e-6
    for sel, expr in (
        (mx == r, lambda: ((g - b) / np.maximum(d, 1e-6)) % 6),
        (mx == g, lambda: (b - r) / np.maximum(d, 1e-6) + 2),
        (mx == b, lambda: (r - g) / np.maximum(d, 1e-6) + 4),
    ):
        i = nz & sel
        h[i] = expr()[i]
    return (h / 6.0) % 1.0, s, mx


def hsv_to_rgb(h, s, v):
    i = np.floor(h * 6.0)
    f = h * 6.0 - i
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    i = i.astype(int) % 6
    return np.select(
        [(i == k)[..., None] for k in range(6)],
        [np.stack(c, -1) for c in ((v, t, p), (q, v, p), (p, v, t),
                                   (p, q, v), (t, p, v), (v, p, q))],
    )


im = Image.open(SRC).convert("RGBA")
src = np.asarray(im)
rgb = src[..., :3].astype(np.float32) / 255.0
alpha = src[..., 3]

h, s, v = rgb_to_hsv(rgb)

# ── rune mask: saturated and close to red in hue ──────────────────────────────
dist = np.minimum(h, 1.0 - h)                       # angular distance from red
rune = np.clip(1.0 - dist / RED_ARC, 0.0, 1.0) * np.clip(s * 1.6, 0, 1)

# ── runes: rotate red -> blue, preserving the red-orange/red-magenta spread ───
signed = np.where(h > 0.5, h - 1.0, h)
h_rune = (BLUE_HUE + signed) % 1.0

# ── stone: map luminance onto the Dwemer brass ramp ───────────────────────────
# Stops sampled from Dwemer_puzzle_cube.webp: deep shadowed bronze, mid brass,
# lit gold, specular highlight. Interpolated in HSV so the hue stays coherent.
RAMP_V = np.array(dwemer_palette.RAMP_POS)
RAMP_H = np.array(dwemer_palette.RAMP_HUE_DEG) / 360.0
RAMP_S = np.array(dwemer_palette.RAMP_SAT)
RAMP_OUT_V = np.array(dwemer_palette.RAMP_VAL)

lum = (0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2])
# The source stone is crushed into the bottom of the range; stretch it so the
# relief reads at 16px instead of collapsing to one flat brown.
stone_lum = np.clip((lum - 0.015) / 0.28, 0.0, 1.0) ** 0.68

h_stone = np.interp(stone_lum, RAMP_V, RAMP_H)
s_stone = np.interp(stone_lum, RAMP_V, RAMP_S)
v_stone = np.interp(stone_lum, RAMP_V, RAMP_OUT_V)

# ── blend the two treatments by the rune mask ────────────────────────────────
w = rune
out_h_rune = hsv_to_rgb(h_rune, np.clip(s * 1.15, 0, 1), np.clip(v * 1.10, 0, 1))
out_stone = hsv_to_rgb(h_stone, s_stone, v_stone)
out = out_stone * (1 - w[..., None]) + out_h_rune * w[..., None]

out = (np.clip(out, 0, 1) * 255).round().astype(np.uint8)
out[alpha == 0] = 0          # keep fully-transparent pixels from being tinted
tinted = Image.fromarray(np.dstack([out, alpha]), "RGBA")
tinted.save(SRC.with_name("lexicon_dwemer.png"))

# Trim to the cube and pad square so small sizes downscale without cropping.
box = tinted.getchannel("A").getbbox()
cube = tinted.crop(box)
side = max(cube.size)
canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
canvas.paste(cube, ((side - cube.width) // 2, (side - cube.height) // 2))

sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]
canvas.save(DST, format="ICO", sizes=sizes)
print(f"wrote {DST} ({DST.stat().st_size} bytes)")
