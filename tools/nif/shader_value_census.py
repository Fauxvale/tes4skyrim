r"""What do BSLightingShaderProperty material values actually hold?

Our converter sets `slsf_1_specular` but assigns neither `glossiness` nor
`specular_color` nor `specular_strength`, so they ship at pyffi's defaults.
Before picking replacement defaults, measure what vanilla Skyrim really writes
-- an exporter's documented default is not evidence about shipped content.

Usage:
  python tools/shader_value_census.py <mesh tree> [--sample N] [--by-folder]
                                      [--workers N] [--seed N]

Samples RANDOMLY across the whole tree by default: walking the first N files
lands in one folder and measures that folder, not the game.
"""
import argparse
import os
import random
import sys
from collections import Counter, defaultdict
from multiprocessing import Pool, cpu_count

# tools/nif/ -> repo root is three levels up (matches the other tools/nif/
# scripts since the 2026-08-26 reorganisation).
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def scan(path):
    """-> (rel_top_folder, [(glossiness, spec_rgb, strength, emissive), ...])"""
    from asset_convert import pyffi_monkey_patch      # noqa: F401
    from pyffi.formats.nif import NifFormat
    out = []
    try:
        d = NifFormat.Data()
        with open(path, 'rb') as f:
            d.read(f)
    except Exception:
        return path, out
    for b in d.blocks:
        # Oblivion source side: the values live on NiMaterialProperty,
        # which is what a 'use the original if present' rule would read.
        if type(b).__name__ == 'NiMaterialProperty':
            sc = b.specular_color
            out.append((round(float(b.glossiness), 1),
                        (round(sc.r, 2), round(sc.g, 2), round(sc.b, 2)),
                        1.0, 0.0, -1))
            continue
        if type(b).__name__ != 'BSLightingShaderProperty':
            continue
        sc = b.specular_color
        out.append((round(float(b.glossiness), 1),
                    (round(sc.r, 2), round(sc.g, 2), round(sc.b, 2)),
                    round(float(b.specular_strength), 2),
                    round(float(b.emissive_multiple), 2),
                    int(b.skyrim_shader_type),
                    (int(getattr(b.shader_flags_2, 'slsf_2_soft_lighting', 0)),
                     int(getattr(b.shader_flags_2, 'slsf_2_tree_anim', 0)),
                     int(getattr(b.shader_flags_1, 'slsf_1_vertex_alpha', 0)),
                     round(float(getattr(b, 'lighting_effect_1', 0.0)), 2))))
    return path, out


def top_folder(path, root):
    rel = os.path.relpath(path, root).replace('\\', '/').lower()
    parts = rel.split('/')
    if parts and parts[0] == 'meshes':
        parts = parts[1:]
    return parts[0] if len(parts) > 1 else '.'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('root')
    ap.add_argument('--sample', type=int, default=1500)
    ap.add_argument('--by-folder', action='store_true')
    ap.add_argument('--workers', type=int, default=max(1, cpu_count() - 1))
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--source-signal', action='store_true',
                    help='TES4 side: which properties/slots exist')
    ap.add_argument('--texture-signal', action='store_true',
                    help='what the texture set knows (suffixes, normal-map alpha)')
    a = ap.parse_args()

    if a.source_signal:
        return source_signal(a.root, a.sample, a.workers, a.seed)
    if a.texture_signal:
        return texture_signal(a.root, a.sample, a.seed)

    files = []
    for dp, _, fs in os.walk(a.root):
        for fn in fs:
            if fn.lower().endswith('.nif'):
                files.append(os.path.join(dp, fn))
    print(f'{len(files)} NIFs under {a.root}')
    if a.sample and a.sample < len(files):
        random.Random(a.seed).shuffle(files)
        files = files[:a.sample]
        print(f'random sample of {len(files)} (seed {a.seed})')

    gloss, spec, strength, emis = Counter(), Counter(), Counter(), Counter()
    per_folder = defaultdict(Counter)
    per_type = defaultdict(Counter)
    soft = defaultdict(Counter)
    le1 = Counter()
    treeanim = Counter()
    n_shaders = n_meshes = 0
    with Pool(a.workers) as pool:
        for i, (path, rows) in enumerate(
                pool.imap_unordered(scan, files, 16), 1):
            if rows:
                n_meshes += 1
            for g, s, st, e, ty, fl in rows:
                soft[top_folder(path, a.root)][fl[0]] += 1
                if fl[0]:
                    le1[fl[3]] += 1
                if fl[1]:
                    treeanim[top_folder(path, a.root)] += 1
                gloss[g] += 1
                spec[s] += 1
                strength[st] += 1
                emis[e] += 1
                per_folder[top_folder(path, a.root)][g] += 1
                per_type[ty][g] += 1
                n_shaders += 1
            if i % 500 == 0:
                print(f'  {i}/{len(files)} ...', flush=True)

    print(f'\n{n_meshes} meshes carried a lighting shader, '
          f'{n_shaders} shaders total\n')
    if not n_shaders:
        return 1

    def show(name, c, top=6):
        print(f'  {name}')
        for v, cnt in c.most_common(top):
            print(f'    {str(v):<22} {cnt:>6}  {cnt * 100.0 / n_shaders:5.1f}%')

    show('glossiness', gloss)
    show('specular colour', spec)
    show('specular strength', strength)
    show('emissive multiple', emis)

    white = sum(c for s, c in spec.items() if s == (1.0, 1.0, 1.0))
    black = sum(c for s, c in spec.items() if s == (0.0, 0.0, 0.0))
    print(f'\n  specular WHITE {white * 100.0 / n_shaders:.1f}%   '
          f'BLACK {black * 100.0 / n_shaders:.1f}%')
    med = sorted(gloss.elements())[n_shaders // 2]
    print(f'  median glossiness {med}')

    print('\n  glossiness per SKYRIM SHADER TYPE:')
    _names = {0: 'Default', 1: 'EnvMap', 2: 'GlowMap', 3: 'Heightmap',
              4: 'FaceTint', 5: 'SkinTint', 6: 'HairTint',
              7: 'ParallaxOcc', 8: 'MultiTexLand', 9: 'LODLand',
              10: 'Snow', 11: 'MultiLayerParallax', 12: 'TreeAnim',
              13: 'LODObjects', 14: 'SparkleSnow', 15: 'LODObjectsHD',
              16: 'EyeEnvmap', 17: 'Cloud', 18: 'LODLandNoise',
              19: 'MultiIndexTriShapeSnow', -1: 'TES4 NiMaterial'}
    for ty, c in sorted(per_type.items(), key=lambda kv: -sum(kv[1].values())):
        tot = sum(c.values())
        top3 = ', '.join(f'{v}({n})' for v, n in c.most_common(3))
        med_t = sorted(c.elements())[tot // 2]
        print(f'    {ty:>2} {_names.get(ty, "?"):<20} {tot:>6}  '
              f'median {med_t:<7} {top3}')

    if a.by_folder:
        print('\n  most common glossiness per top folder:')
        for folder, c in sorted(per_folder.items(),
                                key=lambda kv: -sum(kv[1].values()))[:15]:
            tot = sum(c.values())
            top3 = ', '.join(f'{v}({n})' for v, n in c.most_common(3))
            print(f'    {folder:<16} {tot:>6} shaders   {top3}')
    return 0



# ---------------------------------------------------------------------------
# --source-signal: what does the TES4 side actually CARRY?
#
# The conversion can only map signal that exists.  Before designing a shader
# mapping, count what the source meshes really hold -- which properties, which
# texture slots, which modes -- rather than what the format permits.
# ---------------------------------------------------------------------------

_TEX_SLOTS = ('base', 'dark', 'detail', 'gloss', 'glow', 'bump_map',
              'normal', 'decal_0', 'decal_1', 'decal_2', 'decal_3')


def scan_source_signal(path):
    from asset_convert import pyffi_monkey_patch      # noqa: F401
    from pyffi.formats.nif import NifFormat
    props, slots, modes, misc = Counter(), Counter(), Counter(), Counter()
    try:
        d = NifFormat.Data()
        with open(path, 'rb') as f:
            d.read(f)
    except Exception:
        return props, slots, modes, misc
    for b in d.blocks:
        n = type(b).__name__
        # BS*Property too: Oblivion ships BSShaderPPLightingProperty and
        # friends, and filtering on 'Ni' alone silently hides them.
        if n.endswith('Property'):
            props[n] += 1
        if n.startswith('BSShader'):
            sf = getattr(b, 'shader_type', None)
            if sf is not None:
                misc[f'{n} shader_type={int(sf)}'] += 1
            for attr in ('texture_set', 'refraction_strength',
                         'environment_map_scale'):
                if getattr(b, attr, None):
                    misc[f'{n}.{attr} set'] += 1
        if n == 'NiTexturingProperty':
            modes[f'apply_mode={int(b.apply_mode)}'] += 1
            for s in _TEX_SLOTS:
                if getattr(b, f'has_{s}_texture', False):
                    slots[s] += 1
        elif n == 'NiMaterialProperty':
            ec = b.emissive_color
            if (ec.r, ec.g, ec.b) != (0.0, 0.0, 0.0):
                misc['emissive_color non-black'] += 1
            if float(b.alpha) < 1.0:
                misc['material alpha < 1'] += 1
        elif n == 'NiVertexColorProperty':
            misc[f'vcol lighting_mode={int(b.lighting_mode)}'] += 1
        elif n == 'NiStencilProperty':
            misc['stencil (two-sided)'] += 1
        elif n == 'NiSpecularProperty':
            misc[f'NiSpecularProperty flags={int(b.flags)}'] += 1
    return props, slots, modes, misc


def source_signal(root, sample, workers, seed):
    files = []
    for dp, _, fs in os.walk(root):
        for fn in fs:
            if fn.lower().endswith('.nif'):
                files.append(os.path.join(dp, fn))
    if sample and sample < len(files):
        random.Random(seed).shuffle(files)
        files = files[:sample]
    print(f'{len(files)} source NIFs sampled from {root}\n', flush=True)

    props, slots, modes, misc = Counter(), Counter(), Counter(), Counter()
    with Pool(workers) as pool:
        for i, (p, s, m, x) in enumerate(
                pool.imap_unordered(scan_source_signal, files, 16), 1):
            props += p
            slots += s
            modes += m
            misc += x
            if i % 400 == 0:
                print(f'  {i}/{len(files)} ...', flush=True)

    def show(title, c):
        print(f'\n  {title}')
        for k, v in c.most_common(14):
            print(f'    {str(k):<34} {v:>7}')

    show('properties present', props)
    show('NiTexturingProperty slots in use', slots)
    show('apply_mode', modes)
    show('other authored signal', misc)
    return 0


# ---------------------------------------------------------------------------
# --texture-signal: what the TEXTURE SET knows that the mesh does not.
#
# Both engines read the NORMAL MAP's alpha as the specular mask, so a `_n.dds`
# shipped as DXT5 with a non-flat alpha IS an authored specular map -- and the
# suffix convention (`_g` glow, `_hl` etc.) carries more of the same.  The mesh
# property census says only 4.2% of shapes enable specular; this measures
# whether the textures disagree.
# ---------------------------------------------------------------------------

_SUFFIXES = ('_n', '_g', '_hl', '_e', '_em', '_m', '_s', '_sk', '_b', '_d',
             '_p', '_ref', '_msn')


def _fourcc_and_alpha(path):
    """(fourcc, alpha_kind) without decoding more than the first mip."""
    from asset_convert import parallax as px
    try:
        with open(path, 'rb') as f:
            blob = f.read()
    except OSError:
        return None, None
    if len(blob) < 128 or blob[:4] != b'DDS ':
        return None, None
    fourcc = blob[84:88].decode('latin-1', 'replace')
    info = px.classify_alpha(blob)
    return fourcc, info


def texture_signal(root, sample, seed):
    by_suffix = Counter()
    n_fourcc = Counter()
    n_alpha = Counter()
    rng_buckets = Counter()
    files = []
    for dp, _, fs in os.walk(root):
        for fn in fs:
            if fn.lower().endswith('.dds'):
                files.append(os.path.join(dp, fn))
    for f in files:
        stem = os.path.splitext(os.path.basename(f))[0].lower()
        hit = next((s for s in _SUFFIXES if stem.endswith(s)), 'diffuse')
        by_suffix[hit] += 1
    print(f'{len(files)} DDS under {root}\n')
    print('  by suffix')
    for s, c in by_suffix.most_common(12):
        print(f'    {s:<10} {c:>6}')

    normals = [f for f in files
               if os.path.splitext(os.path.basename(f))[0].lower()
               .endswith('_n')]
    if sample and sample < len(normals):
        random.Random(seed).shuffle(normals)
        normals = normals[:sample]
    print(f'\n  scanning {len(normals)} normal maps for a specular mask',
          flush=True)
    for i, f in enumerate(normals, 1):
        fourcc, info = _fourcc_and_alpha(f)
        if fourcc is None:
            n_fourcc['unreadable'] += 1
            continue
        n_fourcc[fourcc] += 1
        if info is None:
            continue
        n_alpha[info.kind] += 1
        if info.kind != 'no_alpha':
            r = info.rng
            b = ('flat (<8)' if r < 8 else 'weak (8-31)' if r < 32
                 else 'real (32-127)' if r < 128 else 'strong (128+)')
            rng_buckets[b] += 1
        if i % 500 == 0:
            print(f'    {i}/{len(normals)} ...', flush=True)

    print('\n  normal-map format')
    for k, c in n_fourcc.most_common():
        print(f'    {k:<12} {c:>6}  {c * 100.0 / max(1, sum(n_fourcc.values())):5.1f}%')
    print('\n  alpha content of those normal maps')
    for k, c in n_alpha.most_common():
        print(f'    {k:<12} {c:>6}')
    if rng_buckets:
        print('\n  alpha amplitude (how much of a mask it really is)')
        tot = sum(rng_buckets.values())
        for k, c in rng_buckets.most_common():
            print(f'    {k:<16} {c:>6}  {c * 100.0 / tot:5.1f}%')
    return 0

if __name__ == '__main__':
    sys.exit(main())
