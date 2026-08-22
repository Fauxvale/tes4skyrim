"""Offline preview renderer for SPT->geometry conversion.

Renders generated tree geometry (asset_convert.spt_generator) to PNG with
real leaf textures (DDS-decoded, composite-map UV quads honored), painter's-
algorithm depth sorting, and simple headlight shading.  Used to iterate on
generator semantics without launching the game.

Usage:
    python tools/spt_preview.py export/Oblivion.esm/trees/treeenglishoakforestsu.spt
    python tools/spt_preview.py export/Oblivion.esm/trees --max 12 --out temp/spt_preview
    python tools/spt_preview.py <spt> --views 0,90 --size 900 --icon TreeEnglishOakLeavesSU.dds

The composite leaf texture comes from the matching TREE record's ICON field
(read from <export>/TREE.txt) unless --icon overrides it.
"""

import argparse
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent.parent))

from asset_convert.spt_parser import parse_spt          # noqa: E402
from asset_convert.spt_generator import build_tree      # noqa: E402
from asset_convert.flipbook import decode_dds           # noqa: E402

_TEX_CACHE = {}


def load_tree_manifest(export_dir: Path) -> dict:
    """{spt_stem_lower: [(editorid, icon, seed), ...]} from TREE.txt."""
    out = {}
    tf = export_dir / 'TREE.txt'
    if not tf.exists():
        return out
    cur = {}
    for line in open(tf, encoding='utf-8', errors='replace'):
        line = line.strip()
        if line == '---RECORD_BEGIN---':
            cur = {}
        elif line == '---RECORD_END---':
            modl = cur.get('Model.MODL', '').replace('\\\\', '/').strip('/')
            stem = modl.rsplit('/', 1)[-1].lower().replace('.spt', '')
            if stem:
                out.setdefault(stem, []).append(
                    (cur.get('EditorID', ''), cur.get('ICON', ''),
                     int(cur.get('Seed[0]', '0') or 0)))
        elif '=' in line:
            k, v = line.split('=', 1)
            cur[k] = v
    return out


def _find_texture(basename: str, tex_root: Path) -> Path | None:
    stem = Path(basename.replace('\\', '/')).stem.lower()
    for sub in ('leaves', 'branches', ''):
        d = tex_root / sub if sub else tex_root
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.suffix.lower() == '.dds' and p.stem.lower() == stem:
                return p
    return None


def _load_texture(basename: str, tex_root: Path) -> Image.Image | None:
    if not basename:
        return None
    key = basename.lower()
    if key in _TEX_CACHE:
        return _TEX_CACHE[key]
    p = _find_texture(basename, tex_root)
    img = None
    if p is not None:
        try:
            w, h, bgra = decode_dds(p)
            img = Image.frombytes('RGBA', (w, h), bytes(bgra), 'raw', 'BGRA')
        except Exception as e:
            print(f'  [tex] {basename}: {e}')
    _TEX_CACHE[key] = img
    return img


def _mean_color(img: Image.Image | None, default=(110, 90, 70)):
    if img is None:
        return default
    small = img.resize((8, 8))
    px = np.asarray(small).astype(float)
    a = px[..., 3:4] / 255.0
    rgb = (px[..., :3] * a).sum(axis=(0, 1)) / max(a.sum(), 1e-3)
    return tuple(int(v) for v in rgb)


def _uv_affine(quad_xy: np.ndarray, uvs: np.ndarray):
    """Least-squares affine screen(x,y) -> uv for a planar textured quad."""
    a = np.hstack([quad_xy, np.ones((4, 1))])
    coef, *_ = np.linalg.lstsq(a, uvs, rcond=None)
    return coef        # (3,2): [x y 1] @ coef = (u,v)


def _load_alt_builder(path: str):
    """Import a build_tree() from ANOTHER copy of spt_generator.py.

    Used by --compare-rev to render a previous revision's geometry beside the
    current one (extract it with e.g.
    `git show HEAD:asset_convert/spt_generator.py > /tmp/old/asset_convert/spt_generator.py`).
    The alt module is loaded under its own name so it does not clobber the
    live asset_convert.spt_generator already imported above.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location('_spt_gen_alt', path)
    mod = importlib.util.module_from_spec(spec)
    # the alt file does `from .spt_parser import ...`; give it a package
    import types
    pkg = types.ModuleType('_spt_alt_pkg')
    pkg.__path__ = [str(Path(path).parent)]
    sys.modules['_spt_alt_pkg'] = pkg
    import asset_convert.spt_parser as _sp
    sys.modules['_spt_alt_pkg.spt_parser'] = _sp
    mod.__package__ = '_spt_alt_pkg'
    sys.modules['_spt_gen_alt'] = mod
    spec.loader.exec_module(mod)
    return mod.build_tree


def render_tree(spt_path: Path, tex_root: Path, views=(0.0,), size=800,
                seed=None, icon: str = '', export_dir: Path | None = None,
                compare_geo=None):
    """Render `spt_path`'s generated geometry.

    compare_geo: an optional second TreeGeometry (e.g. produced by a previous
    revision of spt_generator) rendered as an extra panel using the SAME
    framing, so an A/B against the current model is pixel-comparable.
    """
    tree = parse_spt(spt_path)

    if not icon and export_dir is not None:
        manifest = load_tree_manifest(export_dir)
        entries = manifest.get(spt_path.stem.lower(), [])
        if entries:
            icon = entries[0][1]
            if seed is None and entries[0][2]:
                seed = entries[0][2]

    geo = build_tree(tree, seed=seed)

    bark_img = _load_texture(Path(tree.bark_texture).name, tex_root)
    bark_rgb = _mean_color(bark_img)

    def _leaf_tex(g):
        # The SPT names the artist's .tga, which is often NOT what shipped
        # (alorange1 asks for TreeGreyPoplarLeavesSU, the pack ships
        # alorange2frut; reddeliciousappletree asks for
        # RedDeliciousAppleLeaves_1/_2, the pack ships reddeliciousapples).
        # spt_converter falls back to the ICON-resolved composite whenever the
        # per-map stem does not ship -- mirror that here, or the preview shows
        # untextured leaves for a tree that converts correctly.
        cands = []
        if g['texture'] != '__composite__':
            cands.append(g['texture'])
        cands += [icon, tree.composite_map,
                  tree.leaf_maps[0].texture if tree.leaf_maps else '']
        for cand in cands:
            if cand:
                t = _load_texture(Path(str(cand).replace('\\', '/')).name, tex_root)
                if t is not None:
                    return t
        return None

    pts = [geo.bark_verts] + [g['verts'] for g in geo.leaf_groups]
    if compare_geo is not None:
        pts += ([compare_geo.bark_verts]
                + [g['verts'] for g in compare_geo.leaf_groups])
    allpts = np.concatenate(pts)
    lo, hi = allpts.min(axis=0), allpts.max(axis=0)
    span = float(max(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])) * 1.06
    cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
    zmid = (lo[2] + hi[2]) / 2

    panels = []
    light = np.array([0.4, -0.8, 0.45])
    light /= np.linalg.norm(light)

    geos = [geo] if compare_geo is None else [geo, compare_geo]
    for gi, geo in enumerate(geos):
      for view_deg in views:
          th = math.radians(view_deg)
          rot = np.array([[math.cos(th), -math.sin(th), 0],
                          [math.sin(th), math.cos(th), 0],
                          [0, 0, 1.0]])

          def project(v):
              p = (v - [cx, cy, 0]) @ rot.T
              sx = (p[:, 0] / span + 0.5) * size
              sy = ((zmid - p[:, 2]) / span + 0.5) * size
              return np.stack([sx, sy], axis=1), p[:, 1]

          img = Image.new('RGBA', (size, size), (30, 34, 40, 255))
          draw = ImageDraw.Draw(img, 'RGBA')
          drawables = []

          bv2, bdepth = project(geo.bark_verts)
          bn = geo.bark_normals @ rot.T
          for t in geo.bark_tris:
              d = float(bdepth[t].mean())
              shade = 0.45 + 0.55 * max(float(np.dot(bn[t[0]], light)), 0.0)
              col = tuple(int(c * shade) for c in bark_rgb) + (255,)
              drawables.append((d, 'tri', ([tuple(p) for p in bv2[t]], col)))

          for g in geo.leaf_groups:
              tex = _leaf_tex(g)
              lv2, ldepth = project(g['verts'])
              for qi in range(0, len(g['verts']), 4):
                  quad = lv2[qi:qi + 4]
                  uvq = g['uvs'][qi:qi + 4]
                  d = float(ldepth[qi:qi + 4].mean())
                  col = g['colors'][qi]
                  drawables.append((d, 'leaf', (quad, uvq, tex, col)))

          drawables.sort(key=lambda x: -x[0])

          for d, kind, payload in drawables:
              if kind == 'tri':
                  poly, col = payload
                  draw.polygon(poly, fill=col)
                  continue
              quad, uvq, tex, col = payload
              if tex is None:
                  draw.polygon([tuple(p) for p in quad], fill=(60, 110, 45, 150))
                  continue
              x0, y0 = int(quad[:, 0].min()), int(quad[:, 1].min())
              x1, y1 = int(quad[:, 0].max()) + 1, int(quad[:, 1].max()) + 1
              w, hgt = x1 - x0, y1 - y0
              if w < 2 or hgt < 2 or w > size * 2 or hgt > size * 2:
                  continue
              tw, thh = tex.size
              coef = _uv_affine(quad, uvq)

              def tc(px, py):
                  u, v = np.array([px, py, 1.0]) @ coef
                  return (float(u * tw), float(v * thh))

              mesh = [((0, 0, w, hgt),
                       (*tc(x0, y0), *tc(x0, y1), *tc(x1, y1), *tc(x1, y0)))]
              try:
                  sprite = tex.transform((w, hgt), Image.MESH, mesh,
                                         resample=Image.BILINEAR)
              except Exception:
                  continue
              if tuple(col[:3]) != (1.0, 1.0, 1.0):
                  arr = np.asarray(sprite).astype(np.float32)
                  arr[..., 0] *= col[0]
                  arr[..., 1] *= col[1]
                  arr[..., 2] *= col[2]
                  sprite = Image.fromarray(arr.astype(np.uint8))
              img.alpha_composite(sprite, (x0, y0))

          gy = ((zmid - 0.0) / span + 0.5) * size
          draw.line([(0, gy), (size, gy)], fill=(90, 90, 90, 255))
          panels.append(img)

    # Oblivion's own billboard render (ground truth), if available
    bb = tex_root / 'billboards' / (Path(spt_path).stem.lower() + '.dds')
    if bb.exists():
        try:
            w, h, bgra = decode_dds(bb)
            bbimg = Image.frombytes('RGBA', (w, h), bytes(bgra), 'raw', 'BGRA')
            panel = Image.new('RGBA', (size, size), (30, 34, 40, 255))
            bbimg = bbimg.resize((size, size))
            panel.alpha_composite(bbimg)
            panels.append(panel)
        except Exception:
            pass

    # label the panels so an A/B is unambiguous
    n_gen = len(views) * (2 if compare_geo is not None else 1)
    labels = []
    for gi in range(2 if compare_geo is not None else 1):
        for v in views:
            tag = 'CURRENT' if gi == 0 else 'PREVIOUS'
            labels.append('%s  %d deg' % (tag, int(v)))
    while len(labels) < len(panels):
        labels.append('OBLIVION billboard (engine truth)')

    out = Image.new('RGBA', (size * len(panels), size))
    for i, p in enumerate(panels):
        out.paste(p, (i * size, 0))
    d = ImageDraw.Draw(out, 'RGBA')
    for i, lab in enumerate(labels[:len(panels)]):
        d.rectangle([i * size, 0, i * size + 8 * len(lab) + 12, 20],
                    fill=(0, 0, 0, 150))
        d.text((i * size + 6, 5), lab, fill=(235, 235, 235, 255))
    return out, geo, tree


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('paths', nargs='+')
    ap.add_argument('--out', default='temp/spt_preview')
    ap.add_argument('--size', type=int, default=700)
    ap.add_argument('--views', default='0')
    ap.add_argument('--max', type=int, default=0)
    ap.add_argument('--seed', type=int, default=None)
    ap.add_argument('--icon', default='', help='composite leaf texture override')
    ap.add_argument('--compare-rev', default='',
                    help='path to another spt_generator.py; renders its geometry '
                         'as an extra panel with identical framing (A/B a change)')
    ap.add_argument('--textures', default='export/Oblivion.esm/textures/trees')
    ap.add_argument('--export-dir', default='export/Oblivion.esm',
                    help='export dir with TREE.txt for ICON/seed lookup')
    args = ap.parse_args()

    files = []
    for p in args.paths:
        p = Path(p)
        files.extend(sorted(p.rglob('*.spt')) if p.is_dir() else [p])
    if args.max:
        files = files[:args.max]
    views = [float(v) for v in args.views.split(',')]
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    alt_build = _load_alt_builder(args.compare_rev) if args.compare_rev else None

    for f in files:
        try:
            cgeo = None
            if alt_build is not None:
                atree = parse_spt(f)
                aseed = args.seed
                if aseed is None and args.export_dir:
                    ents = load_tree_manifest(Path(args.export_dir)).get(f.stem.lower(), [])
                    if ents and ents[0][2]:
                        aseed = ents[0][2]
                cgeo = alt_build(atree, seed=aseed)
            img, geo, tree = render_tree(
                f, Path(args.textures), views=views, size=args.size,
                seed=args.seed, icon=args.icon, export_dir=Path(args.export_dir),
                compare_geo=cgeo)
            out = outdir / (f.stem + '.png')
            img.convert('RGB').save(out)
            print(f'{f.stem}: h={geo.height:.0f} r={geo.radius:.0f} '
                  f'stems={geo.n_stems} leaves={geo.n_leaves} '
                  f'bark_tris={len(geo.bark_tris)} -> {out}')
        except Exception as e:
            import traceback
            print(f'{f.stem}: FAIL {e}')
            traceback.print_exc()


if __name__ == '__main__':
    main()
