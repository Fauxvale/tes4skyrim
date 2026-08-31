"""Regenerate the GUI banner: TESRACT in the Oblivion font, Dwemer brass.

Usage:
    python tools/generators/make_banner.py [--svg docs/assets/banner.svg] [--png docs/assets/banner.png]

The title is traced from `references/oblivion-font.ttf` into real SVG outlines
rather than a `font-family` reference, so the banner renders identically on a
machine that has never seen the font. The letters carry the same brass ramp
measured from the vanilla Dwemer puzzle cube that `tools/generators/make_app_icon.py`
applies to the lexicon cube, so the banner and the app icon read as one set.

The PNG is rendered FROM the SVG, so the two can never drift.
"""
import argparse
import sys
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from tools.generators import dwemer_palette  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
FONT = ROOT / "references" / "oblivion-font.ttf"

TITLE = "TESRACT"
SUBTITLE = "OBLIVION \u2192 SKYRIM CONVERTER"

W, H = 960, 180

# The brass comes from the shared palette, so the banner and the app icon can
# never drift apart. See tools/generators/dwemer_palette.py for the measurements.
BRASS = dwemer_palette.BANNER_TITLE_STOPS


def glyph_paths(text, size, letter_spacing=0.0):
    """Trace `text` to (path_d, x_offset, advance) at `size` px em."""
    font = TTFont(FONT)
    cmap = font.getBestCmap()
    glyphs = font.getGlyphSet()
    upem = font["head"].unitsPerEm
    scale = size / upem

    out, x = [], 0.0
    for ch in text:
        gname = cmap[ord(ch)]
        pen = SVGPathPen(glyphs)
        glyphs[gname].draw(pen)
        d = pen.getCommands()
        adv = glyphs[gname].width * scale
        if d:
            out.append((d, x, scale))
        x += adv + letter_spacing
    return out, x


def gradient_stops(gid, stops):
    body = "".join(
        f'\n      <stop offset="{o}" stop-color="{c}"/>' for o, c in stops)
    return (f'    <linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
            f'{body}\n    </linearGradient>')


def build_svg():
    title_size = 116
    paths, title_w = glyph_paths(TITLE, title_size, letter_spacing=10)
    tx = (W - title_w) / 2
    baseline = 90

    # Each glyph is emitted twice: a dark offset copy that reads as the carved
    # edge, then the brass face over it.
    face, edge = [], []
    for d, x, scale in paths:
        t = (f'translate({tx + x:.2f} {baseline}) '
             f'scale({scale:.5f} {-scale:.5f})')
        edge.append(f'<path transform="{t}" d="{d}"/>')
        face.append(f'<path transform="{t}" d="{d}"/>')

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="{TITLE}">
  <defs>
{gradient_stops("brass", BRASS)}
    <linearGradient id="rule" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#a86f24" stop-opacity="0"/>
      <stop offset="0.5" stop-color="#d9a441" stop-opacity="0.85"/>
      <stop offset="1" stop-color="#a86f24" stop-opacity="0"/>
    </linearGradient>
    <filter id="engrave" x="-8%" y="-25%" width="116%" height="150%">
      <feDropShadow dx="0" dy="2" stdDeviation="1.6" flood-color="#000000" flood-opacity="0.65"/>
    </filter>
  </defs>

  <!-- No background rect: the banner is transparent so it sits on whatever
       panel color the GUI theme is using, instead of punching a dark hole. -->

  <!-- Title: Oblivion font traced to outlines, Dwemer brass ramp -->
  <g filter="url(#engrave)">
    <g fill="#2a1a08" opacity="0.85" transform="translate(0 2)">
      {"".join(edge)}
    </g>
    <g fill="url(#brass)">
      {"".join(face)}
    </g>
  </g>

  <!-- Divider rule -->
  <rect x="230" y="124" width="500" height="2" fill="url(#rule)"/>

  <!-- The GUI scales this 960px banner down to a 350px sidebar column, so the
       subtitle is sized for that ~0.36 factor: at the old 15px it rendered
       around 5px tall and was unreadable. -->
  <text x="{W // 2}" y="174" text-anchor="middle" font-family="Georgia, serif"
        font-size="27" letter-spacing="4" fill="#b9c3ce">{SUBTITLE}</text>
</svg>
'''


def render_png(svg_path: Path, png_path: Path, scale: int = 2) -> bool:
    """Rasterise the SVG so the PNG can never drift from its source.

    Headless Edge/Chrome rather than cairosvg: the machine has no libcairo, and
    a browser is the same engine that renders the SVG everywhere else, so the
    PNG matches what the vector actually looks like.
    """
    import base64
    import shutil
    import subprocess
    import tempfile

    browsers = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]
    exe = next((b for b in browsers if Path(b).exists()), None)
    if exe is None:
        return False

    # Wrap the SVG in a page sized to the exact output so the screenshot needs
    # no cropping: a bare --screenshot of an .svg letterboxes it in a viewport.
    svg_b64 = base64.b64encode(svg_path.read_bytes()).decode("ascii")
    html = (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "html,body{margin:0;padding:0;background:transparent}"
        f"img{{display:block;width:{W * scale}px;height:{H * scale}px}}"
        "</style></head><body>"
        f"<img src='data:image/svg+xml;base64,{svg_b64}'></body></html>"
    )

    tmp = Path(tempfile.mkdtemp(prefix="banner_"))
    try:
        page = tmp / "page.html"
        page.write_text(html, encoding="utf-8")
        shot = tmp / "shot.png"
        subprocess.run(
            [exe, "--headless", "--disable-gpu", "--hide-scrollbars",
             f"--screenshot={shot}",
             f"--window-size={W * scale},{H * scale}",
             "--default-background-color=00000000",
             page.as_uri()],
            check=True, capture_output=True, timeout=90)
        if not shot.exists():
            return False
        shutil.copyfile(shot, png_path)
        return True
    except (subprocess.SubprocessError, OSError):
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--svg", default=str(ROOT / "docs" / "banner.svg"))
    ap.add_argument("--png", default=str(ROOT / "docs" / "banner.png"))
    a = ap.parse_args()

    svg = build_svg()
    Path(a.svg).write_text(svg, encoding="utf-8")
    print(f"wrote {a.svg} ({len(svg)} bytes)")

    if render_png(Path(a.svg), Path(a.png)):
        print(f"wrote {a.png} (rendered from the SVG at 2x)")
    else:
        print("no SVG renderer found - SVG written, PNG NOT regenerated")


if __name__ == "__main__":
    main()
