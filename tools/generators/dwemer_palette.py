"""The Dwemer brass palette — the single source of truth for TESRACT's metal.

Measured from the vanilla Dwemer puzzle cube (`Dwemer_puzzle_cube.webp`): over
the opaque pixels, hue runs 13-44 deg with a median of 26, saturation sits
around 0.92, and value covers the full range. Everything that renders Dwemer
metal in this project reads its numbers from here, so the app icon and the GUI
banner stay one visual set instead of drifting apart.

Consumers:
    tools/generators/make_app_icon.py  - recolors the lexicon cube's stone
    tools/generators/make_banner.py    - the banner title's SVG gradient
"""

# ── The ramp, as measured ────────────────────────────────────────────────────
# Position along the ramp (0 = deepest shadow, 1 = specular highlight), then the
# hue/saturation/value each position maps to. Hue climbs from a red-brown
# shadow toward a pale gold highlight, while saturation falls off as the
# highlight blows out -- that pairing is what reads as metal rather than paint.
RAMP_POS = (0.00, 0.30, 0.62, 0.85, 1.00)
RAMP_HUE_DEG = (18.0, 25.0, 32.0, 40.0, 46.0)
RAMP_SAT = (0.96, 0.95, 0.88, 0.70, 0.40)
RAMP_VAL = (0.12, 0.50, 0.78, 0.94, 1.00)

# The rune glow that sits on top of the metal: a cold arcane blue, deliberately
# opposite the brass so it reads as light rather than as more metal.
RUNE_HUE_DEG = 216.0
RUNE_RED_ARC_DEG = 60.0   # how far from pure red still counts as a rune


def _hsv_to_hex(h_deg: float, s: float, v: float) -> str:
    """HSV (hue in degrees) -> '#rrggbb'."""
    h = (h_deg % 360.0) / 60.0
    i = int(h) % 6
    f = h - int(h)
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    r, g, b = ((v, t, p), (q, v, p), (p, v, t),
               (p, q, v), (t, p, v), (v, p, q))[i]
    return "#{:02x}{:02x}{:02x}".format(
        *(round(c * 255) for c in (r, g, b)))


def ramp_hex():
    """The ramp as [(position, '#rrggbb')], shadow-first."""
    return [(pos, _hsv_to_hex(h, s, v)) for pos, h, s, v
            in zip(RAMP_POS, RAMP_HUE_DEG, RAMP_SAT, RAMP_VAL, strict=True)]


# The banner title's gradient, shadow-first. These are hand-tuned rather than
# derived from the ramp above: the glyph transform flips Y, so the sweep runs
# dark at the top bevel, through a lit brass face, back to bronze at the foot.
# A more elaborate "top-lit metal" version with a specular turn and bounce
# light was tried and REVERTED -- it read as busier, not more metallic, and
# this is the version that was signed off. Don't re-derive it.
BANNER_TITLE_STOPS = [
    (0.00, "#4a2c0d"),   # shadowed bronze under the top bevel
    (0.16, "#8a5a1c"),
    (0.42, "#c8912f"),
    (0.62, "#f0cf7c"),   # lit brass face
    (0.80, "#d9a441"),
    (1.00, "#8a5a1c"),   # falls back to bronze at the foot
]


if __name__ == "__main__":
    print("Dwemer brass ramp (measured from the vanilla puzzle cube):")
    for pos, hexv in ramp_hex():
        print(f"  {pos:.2f}  {hexv}")
    print("\nBanner title gradient stops:")
    for pos, hexv in BANNER_TITLE_STOPS:
        print(f"  {pos:.2f}  {hexv}")
