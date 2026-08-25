"""Oblivion hair -> Skyrim head-part (HDPT) meshes, with hair length baked in.

WHY THIS IS A SEPARATE STAGE
    `meshes\\characters\\` is in nif_converter.SKIP_PATHS, so hair never enters
    the batch mesh conversion.  It cannot simply be un-skipped: a converted
    hair mesh is not a 1:1 file copy.  Oblivion's per-NPC hair LENGTH
    (NPC_.LNAM, a float) selects a blend between the base mesh and the
    ``HairMorph`` target in the sibling .tri, and Skyrim has no per-NPC
    hair-length field to carry that with.  So one Oblivion hair record becomes
    SEVERAL Skyrim meshes -- one per distinct length actually used by NPCs --
    and each needs its own HDPT record to point at it.

    See facegen_tri for the evidence that HairMorph is length (50 of 57 vanilla
    hairs extend downward under it, 0 upward) and for both engines' custom
    morph slots.

WHAT GETS EMITTED
    For each (HAIR record, quantized LNAM, gender) triple the plugin needs:

        meshes\\tes4\\characters\\hair\\<stem>[__f][__l<NN>].nif   baked geometry
        meshes\\tes4\\characters\\hair\\<stem>[__f][__l<NN>].tri   SkinnyMorph slot

    The male length-0 variant keeps the bare stem (it IS the unmorphed base
    mesh); female variants carry `__f`.

WHY PER GENDER (2026-08-23)
    Every mesh is FITTED to the Skyrim head through asset_convert.head_fit:
    each vertex keeps its authored signed distance from the Oblivion skin, so
    hair authored flush on the scalp stays flush on the Skyrim scalp.  The
    Skyrim male and female skulls differ by up to 1.23 units over the scalp
    (mean 0.46 -- measured malehead vs femalehead), far more than the fit's
    own error, so one unisex mesh cannot be flush on both; vanilla Skyrim
    genders every hairstyle for the same reason.  A TES4 hair restricted to
    one gender (DATA.Flags NotMale/NotFemale) only emits that gender.

FORMID CONTRACT
    Male variant HDPTs are keyed `derive_formid('HDPT_HAIR', (hair_fid,
    bucket))`, female ones `('HDPT_HAIR', (hair_fid, bucket, 'F'))`.  Every
    component is authored TES4 data -- the source HAIR FormID, a quantization
    of the authored LNAM float, and the authored gender restriction -- never
    anything the conversion computes, so ids are stable across machines and
    builds.  Changing LENGTH_BUCKETS renumbers every non-zero variant, which
    is FormID drift.
"""

import os
import sys
from concurrent.futures import ProcessPoolExecutor

from .facegen_tri import TriFile, TriError, build_skyrim_hair_tri

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker_budget import worker_count  # noqa: E402
from output_layout import assets_for  # noqa: E402

_WORKERS = worker_count()

# How finely the authored LNAM float is quantized.  The distribution in
# Oblivion.esm is strongly bimodal -- 654 NPCs at exactly 0.0 and 371 at
# exactly 1.0, then a long tail (0.58, 0.55, 0.47, 0.37 ...) -- so the two
# endpoint buckets carry most actors and cost nothing extra: bucket 0 is the
# base mesh and bucket N is the fully-applied morph.
#
# 🛑 CHANGING THIS RENUMBERS EVERY NON-ZERO HAIR VARIANT (FormID drift).
LENGTH_BUCKETS = 8

# Oblivion hair lives here; converted output is namespaced under tes4\ like
# every other converted mesh so it can never collide with a vanilla path.
OUT_REL_DIR = os.path.join('tes4', 'characters', 'hair')


def quantize_length(value: float) -> int:
    """Authored LNAM float -> bucket index in [0, LENGTH_BUCKETS].

    Clamped rather than wrapped: a plugin is free to author a length outside
    [0, 1] and the morph is only defined across that span.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0
    if v != v:            # NaN
        return 0
    if v <= 0.0:
        return 0
    if v >= 1.0:
        return LENGTH_BUCKETS
    return int(round(v * LENGTH_BUCKETS))


def bucket_weight(bucket: int) -> float:
    """Bucket index -> the morph blend weight it represents."""
    if bucket <= 0:
        return 0.0
    if bucket >= LENGTH_BUCKETS:
        return 1.0
    return bucket / float(LENGTH_BUCKETS)


# Race-group mesh suffixes.  The human-group mesh keeps the bare stem (it
# IS the fit every file carried before groups existed); the elf and orc
# groups get their own meshes because their in-game heads differ from the
# base scalp by up to 2.6 / 1.5 units (see head_fit.GROUP_MORPHS).
GROUP_SUFFIX = {'elves': '__ev', 'orc': '__or'}


def variant_stem(stem: str, bucket: int, female: bool = False,
                 group=None) -> str:
    """Output filename stem for a hair mesh variant.

    The male bucket-0 human-group variant keeps the bare stem -- the file a
    conversion without any length handling would already have written.
    Female meshes (fitted to femalehead) carry `__f`; elf/orc group meshes
    carry `__ev` / `__or`.
    """
    if female:
        stem = stem + '__f'
    stem += GROUP_SUFFIX.get(group, '')
    if bucket <= 0:
        return stem
    return '%s__l%02d' % (stem, bucket)


def variant_edid(edid: str, bucket: int, female: bool = False,
                 group=None) -> str:
    """EditorID for the HDPT of a hair variant."""
    base = edid or 'Hair'
    out = 'TES4Hair%s' % base
    if female:
        out += 'F'
    out += {'elves': 'Elf', 'orc': 'Orc', 'dremora': 'Dre'}.get(group, '')
    if bucket > 0:
        out += '_L%02d' % bucket
    return out


# ---------------------------------------------------------------------------
# Geometry baking
# ---------------------------------------------------------------------------

def bake_hair_variant(nif_bytes: bytes, tri_bytes, weight: float,
                      female: bool = False, race=None, group=None):
    """Bake one hair variant: length morph + Skyrim head fit.

    Applies `weight` of the .tri HairMorph to the hair NIF's vertices, then
    fits the result to the Skyrim head of the given gender through
    asset_convert.head_fit (each vertex keeps its authored signed distance
    from the Oblivion skin, so scalp-flush hair stays flush).

    Returns (new_nif_bytes, baked_tri_bytes).  `tri_bytes` may be None (a
    few Oblivion hairs ship no .tri at all) in which case the geometry
    passes through untouched and the emitted .tri is built from the NIF's
    own mesh.  `group` selects the race-group target head ('elves'/'orc');
    None fits the base (human) head.

    The morph is matched to geometry by VERTEX COUNT, not by name or order:
    an Oblivion hair NIF is a single NiTriShape whose vertex count equals the
    .tri's, and pairing on that is what makes the bake safe when a mesh
    carries extra non-morphed shapes (jewellery on khajiitjeweled, etc.).
    The head fit runs on EVERY shape as one system, so those extra shapes
    follow the hair they decorate.
    """
    from . import pyffi_monkey_patch as _patch  # noqa: F401
    from pyffi.formats.nif import NifFormat
    import io

    tri = None
    deltas = None
    if tri_bytes:
        try:
            tri = TriFile.from_bytes(tri_bytes)
            deltas = tri.hair_morph()
        except TriError:
            tri = None
            deltas = None

    data = NifFormat.Data()
    data.read(io.BytesIO(nif_bytes))

    # ---- pass 1: length morph -------------------------------------------
    blocks = []
    for root in data.roots:
        if root is None:
            continue
        for block in root.tree():
            if not isinstance(block, (NifFormat.NiTriShape,
                                      NifFormat.NiTriStrips)):
                continue
            gd = block.data
            if gd is None or gd.num_vertices == 0:
                continue
            if (deltas is not None and weight != 0.0
                    and gd.num_vertices == len(deltas)):
                for i, v in enumerate(gd.vertices):
                    dx, dy, dz = deltas[i]
                    v.x += dx * weight
                    v.y += dy * weight
                    v.z += dz * weight
            blocks.append(block)

    # ---- pass 2: fit onto the Skyrim head -------------------------------
    _fit_blocks_to_head(blocks, female, race, group)

    # ---- pass 3: the largest shape defines the .tri we emit -------------
    baked_verts = None
    baked_faces = None
    baked_uvs = None
    for block in blocks:
        gd = block.data
        try:
            gd.update_center_radius()
        except Exception:
            pass
        if baked_verts is None or gd.num_vertices > len(baked_verts):
            baked_verts = [(v.x, v.y, v.z) for v in gd.vertices]
            baked_faces = _shape_triangles(block, gd)
            baked_uvs = _shape_uvs(gd)

    out = io.BytesIO()
    data.write(out)

    if baked_verts is None:
        return out.getvalue(), None

    # Prefer the source .tri's topology when the counts agree -- it is the
    # authored mesh the morph was built against.
    if tri is not None and len(tri.vertices) == len(baked_verts):
        faces = tri.faces
        uvs = tri.uvs
        uv_faces = tri.uv_faces
    else:
        faces = baked_faces
        uvs = baked_uvs
        uv_faces = None

    tri_out = build_skyrim_hair_tri(baked_verts, faces, uvs, uv_faces)
    return out.getvalue(), tri_out


def _fit_group_lock(edid: str):
    """The single race group a race-NAMED hair belongs to, or None (generic).

    Oblivion names race hair for its race; an elf-named hair is only ever
    worn by elves so it bakes ONE mesh fitted to the elf head; likewise orc.
    Generic hair bakes all three group meshes.
    """
    low = (edid or '').lower()
    if 'elf' in low:
        return 'elves'
    if 'orc' in low:
        return 'orc'
    if any(t in low for t in ('nord', 'imperial', 'breton', 'redguard',
                              'dremora')):
        return 'humans'
    return None


def _fit_race(edid: str):
    """Race pack for a hair record (khajiit/argonian/orc), or None (human).

    Oblivion authors race-specific hair against that race's own head mesh,
    and names the record for the race — the same EDID token match the HDPT
    RNAM routing uses.
    """
    try:
        from .head_fit import fit_race_for_hair
    except ImportError:
        return None
    return fit_race_for_hair(edid)


def _fit_blocks_to_head(blocks, female: bool, race=None, group=None) -> bool:
    """Fit hair geometry blocks onto the Skyrim head (asset_convert.head_fit).

    Oblivion hair is authored in face space (origin = head attach point) and
    the conversion glues it rigidly to Skyrim's head bone, so without this the
    OBLIVION skull's shape survives around a SKYRIM skull.  All blocks are
    solved as one system (cross-shape welds stay together).  Returns
    whether the fit ran; when the fit data is unavailable the geometry
    passes through untouched, the old unfitted behavior.
    """
    if not blocks:
        return False
    try:
        import numpy as np
        from . import head_fit
    except ImportError:
        return False
    if not head_fit.fit_available(female):
        return False

    shapes = []
    for block in blocks:
        gd = block.data
        verts = np.array([[v.x, v.y, v.z] for v in gd.vertices],
                         dtype=np.float64)
        tris = np.array(_shape_triangles(block, gd), dtype=np.int64)
        if tris.size == 0:
            tris = np.zeros((0, 3), dtype=np.int64)
        shapes.append((verts, tris))

    fitted = head_fit.fit_head_gear(shapes, female, race=race, group=group,
                                    cover_ears=True, hug=True)
    if fitted is None:
        return False
    for block, new_v in zip(blocks, fitted):
        gd = block.data
        for i, v in enumerate(gd.vertices):
            v.x = float(new_v[i, 0])
            v.y = float(new_v[i, 1])
            v.z = float(new_v[i, 2])
    return True


# BSLightingShaderProperty.skyrim_shader_type 6 = "Hair Tint".  nif.xml is
# explicit that the Hair Tint Color field is read ONLY when Shader Type == 6
# ("Enables Hair Tint Color" / cond="Shader Type == 6"), and the engine carries
# a matching BSLightingShaderMaterialHairTint RTTI class plus a "HairTint"
# shader technique.  Converted hair shipped as type 0 (Default), which is why
# it rendered as the raw grey source texture no matter what the NPC's HCLF
# said: with the wrong material class the tint is never sampled.
#
# Vanilla census (references/Skyrim Meshes, 214 hair shaders): type 6 on 196,
# type 5 on the remaining 18 (the beast-race horn meshes, which are genuinely
# untinted).  Flags on the type-6 majority are 0x82440303 / 0x80a1.
SHADER_TYPE_HAIR_TINT = 6

# The mesh's own tint is a PLACEHOLDER: nif.xml notes it is "Overridden by game
# settings", and the engine substitutes the wearer's HCLF -> CLFM color at
# runtime.  Vanilla's most common value (92 of 214 shaders) is
# (0.5176, 0.4706, 0.3922) = RGB (132, 120, 100); matching it keeps a converted
# mesh looking right in NifSkope and in the CK preview.
_VANILLA_HAIR_TINT = (0.5176470875740051, 0.4705882966518402, 0.3921569287776947)

# Vanilla hair sets these three on top of what the generic converter already
# writes.  Soft lighting is what gives hair its through-strand falloff;
# own_emit and assume_shadowmask come with it in every vanilla type-6 hair.
# Alpha TEST threshold for hair.  Oblivion authored hair with alpha BLEND and
# threshold 0 (60 of its 61 hair meshes), which its renderer tolerated.  Skyrim
# does not: with threshold 0 the test rejects nothing, so every semi-transparent
# strand pixel goes through the blend path and gets depth-sorted per frame --
# which reads in game as the whole hairstyle smearing/blurring as the camera
# rotates, and as broken transparency generally.
#
# Vanilla Skyrim hair alpha-tests (dominant flag word 0x12EC -- test ON,
# blend OFF), with thresholds 128 (x92), 100 (x57), 120 (x12) and a low tail
# (hairlonghumanm ships 0x12EC at 35).  128 is right for VANILLA textures,
# whose alpha is a strand mask -- but OBLIVION hair diffuses were authored
# for blend-at-0, and several put large VISIBLE regions at mid alpha:
# measured, threshold 128 deletes 18% of grey.dds (the blindfold band tore
# into holes in game, 2026-08-24 -- no triangles were lost, the test cut the
# texels), 18% of mane.dds, 17% of dremora.dds, 12% of khajiit.dds.  At 35
# the loss drops to 2-5% (texels under ~14% opacity, near-invisible under
# Oblivion's own blending); the blindfold band still showed residual holes
# at 35 (its 16-35 band is 3% of visible texels), so the shipped threshold
# is 16 — loss 1.3%, texels under ~6% opacity.  Keeps the vanilla no-blur
# flags while cutting essentially nothing Oblivion showed.
HAIR_ALPHA_THRESHOLD = 16

# NiAlphaProperty flag word used by 126 of 211 vanilla hair meshes: alpha test
# enabled (bit 9), alpha blend disabled (bit 0), src/dst blend modes left at
# the vanilla SRC_ALPHA / INV_SRC_ALPHA pair.
HAIR_ALPHA_FLAGS = 0x12EC


def _texture_bias(dds_path):
    """Per-channel colour bias of a hair diffuse, normalized to its own mean.

    Returns (br, bg, bb) where 1.0 on every channel means a neutral texture.
    Alpha-weighted, because a hair diffuse's transparent margin is not part of
    the visible strand colour.
    """
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return None
    try:
        im = Image.open(dds_path)
        im.load()
        arr = np.asarray(im.convert('RGBA')).astype(float)
    except Exception:
        return None
    rgb = arr[..., :3]
    alpha = arr[..., 3:4] / 255.0
    if alpha.sum() < 1.0:
        alpha = np.ones_like(alpha)
    mean = (rgb * alpha).sum(axis=(0, 1)) / alpha.sum()
    grey = mean.mean()
    if grey <= 1e-6:
        return None
    return tuple(float(c / grey) for c in mean)


def hair_tint_for_texture(dds_path):
    """The mesh tint to write so an authored hair colour reads true.

    Skyrim MULTIPLIES the hair tint by the diffuse, so the texture's own colour
    cast survives into the result.  Oblivion's hair diffuses are nowhere near
    uniform -- measured alpha-weighted channel ratios (r:g:b, normalized):

        grey     1.07 : 0.99 : 0.94      near neutral
        dremora  1.06 : 1.00 : 0.95      near neutral
        short    1.09 : 0.99 : 0.92      near neutral
        khajiit  1.11 : 1.00 : 0.89      mild warm
        mane     1.43 : 1.03 : 0.54      strongly orange-brown
        argonian 1.40 : 0.97 : 0.63      strongly orange-brown

    so the same authored HCLF renders very differently depending on which
    texture the hairstyle happens to use -- a Khajiit mane comes out orange no
    matter what colour the NPC authored.  Dividing the bias out of the mesh
    tint cancels the texture's cast, leaving the wearer's own colour to do the
    work.  Falls back to the plain vanilla tint when the texture cannot be read.
    """
    bias = _texture_bias(dds_path) if dds_path else None
    if not bias:
        return _VANILLA_HAIR_TINT
    out = []
    for base, b in zip(_VANILLA_HAIR_TINT, bias):
        out.append(min(1.0, max(0.0, base / b)) if b > 1e-6 else base)
    return tuple(out)


def apply_hair_shader(data, tint=None, spec_strength=None):
    """Retype a converted hair NIF's shaders to Hair Tint (type 6).

    Also switches the NiAlphaProperty from Oblivion's blend-with-no-threshold
    to Skyrim's alpha-test form (see HAIR_ALPHA_THRESHOLD).

    Returns the number of shader properties updated.
    """
    from . import pyffi_monkey_patch as _patch  # noqa: F401
    from pyffi.formats.nif import NifFormat

    for block in data.blocks:
        if isinstance(block, NifFormat.NiAlphaProperty):
            block.flags = HAIR_ALPHA_FLAGS
            block.threshold = HAIR_ALPHA_THRESHOLD

    n = 0
    for block in data.blocks:
        if not isinstance(block, NifFormat.BSLightingShaderProperty):
            continue
        block.skyrim_shader_type = SHADER_TYPE_HAIR_TINT
        block.shader_flags_1.slsf_1_hair_soft_lighting = 1
        block.shader_flags_1.slsf_1_own_emit = 1
        block.shader_flags_2.slsf_2_assume_shadowmask = 1
        # ANISOTROPIC LIGHTING is what makes the specular read as hair
        # strands instead of a round plastic highlight — vanilla human hair
        # (hairshorthumanm, malehumanoldhair01) sets it alongside soft
        # lighting params 0.3/2.0; without them converted hair looked
        # "really shiny and not hair-like" in game (2026-08-24).
        block.shader_flags_2.slsf_2_anisotropic_lighting = 1
        block.lighting_effect_1 = 0.30000001192092896
        block.lighting_effect_2 = 2.0
        tint_col = getattr(block, 'hair_tint_color', None)
        if tint_col is not None:
            tint_col.r, tint_col.g, tint_col.b = tint or _VANILLA_HAIR_TINT
        # Vanilla hair is specular with a tight highlight; the generic path
        # leaves these at zero, which reads as flat matte under the tint.
        block.glossiness = 10.0
        block.specular_strength = (_SPEC_STRENGTH_BASE
                                   if spec_strength is None
                                   else float(spec_strength))
        spec = getattr(block, 'specular_color', None)
        if spec is not None:
            spec.r = spec.g = spec.b = 1.0
        n += 1
    return n


def _shape_triangles(block, gd):
    """Triangle list for either a NiTriShape or a NiTriStrips."""
    try:
        return [tuple(t) for t in gd.get_triangles()]
    except Exception:
        pass
    tris = getattr(gd, 'triangles', None)
    if not tris:
        return []
    return [(t.v_1, t.v_2, t.v_3) for t in tris]


def _shape_uvs(gd):
    sets = getattr(gd, 'uv_sets', None)
    if not sets or len(sets) == 0:
        return []
    return [(uv.u, uv.v) for uv in sets[0]]


# ---------------------------------------------------------------------------
# Which (hair, length) pairs the plugin actually uses
# ---------------------------------------------------------------------------

def collect_hair_usage(npc_records) -> dict:
    """Map HAIR FormID -> set of length buckets its wearers ask for.

    `npc_records` is any iterable of parsed NPC_ record dicts.  Callers MUST
    include master-owned NPCs (ctx.master_export) as well as the current
    plugin's: an ESP whose actors are defined in Oblivion.esm would otherwise
    register no lengths at all and every one of its NPCs would get the
    bucket-0 mesh regardless of its authored LNAM.
    """
    usage: dict = {}
    for rec in npc_records:
        hair_fid = _rec_formid(rec, 'HNAM.Hair')
        if not hair_fid:
            continue
        bucket = quantize_length(_rec_float(rec, 'LNAM.HairLength'))
        usage.setdefault(hair_fid, set()).add(bucket)
    return usage


# ---------------------------------------------------------------------------
# Export-text helpers (this module runs in the ASSET stage, which has no
# tes5_import context, so it parses the same KEY=VALUE dump directly).
# ---------------------------------------------------------------------------

def _iter_records(txt):
    if not os.path.isfile(txt):
        return
    with open(txt, 'r', encoding='utf-8', errors='replace') as fh:
        body = fh.read()
    for chunk in body.split('---RECORD_BEGIN---')[1:]:
        rec = {}
        for line in chunk.split('---RECORD_END---')[0].splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            rec[k] = v
        if rec:
            yield rec


def _rec_formid(rec, key) -> int:
    raw = (rec.get(key) or '').strip()
    if not raw:
        return 0
    raw = raw.split()[0]
    try:
        return int(raw, 16)
    except ValueError:
        return 0


def _rec_float(rec, key) -> float:
    raw = (rec.get(key) or '').strip()
    if not raw:
        return 0.0
    try:
        return float(raw.split()[0])
    except ValueError:
        return 0.0


def _norm_model(path: str) -> str:
    """MODL path -> lowercase mesh-relative path with OS separators."""
    p = (path or '').strip().replace('\\\\', '\\').replace('\\', '/')
    return p.lstrip('/').lower()


# TES4 HAIR DATA flags: bit 0 Playable, bit 1 NotMale, bit 2 NotFemale.
TES4_HAIR_NOT_MALE = 0x02
TES4_HAIR_NOT_FEMALE = 0x04


def hair_genders(data_flags: int) -> tuple:
    """Which genders a TES4 HAIR record allows, as a tuple of female-bools.

    A record excluding BOTH genders is authored nonsense; treat it as unisex
    rather than emitting nothing (a missing HDPT is a dangling PNAM).
    """
    out = []
    if not data_flags & TES4_HAIR_NOT_MALE:
        out.append(False)
    if not data_flags & TES4_HAIR_NOT_FEMALE:
        out.append(True)
    return tuple(out) or (False, True)


def build_hair_plan(export_dir) -> dict:
    """Which (HAIR record, length bucket, gender) variants this plugin needs.

    Returns {hair_fid: {'edid', 'model', 'buckets': set(), 'genders': tuple}}.

    Every HAIR record gets at least bucket 0 so a hair no NPC currently wears
    still converts -- leveled actors and other plugins reference hair we can
    not see from NPC_ alone, and a missing HDPT is a dangling PNAM.
    """
    export_dir = str(export_dir)
    plan: dict = {}

    for rec in _iter_records(os.path.join(export_dir, 'HAIR.txt')):
        fid = _rec_formid(rec, 'FormID')
        if not fid:
            continue
        try:
            flags = int((rec.get('DATA.Flags') or '0').strip() or '0')
        except ValueError:
            flags = 0
        plan[fid] = {
            'edid': (rec.get('EditorID') or '').strip(),
            'model': (rec.get('Model.MODL') or '').strip(),
            'buckets': {0},
            'genders': hair_genders(flags),
        }

    usage = collect_hair_usage(
        _iter_records(os.path.join(export_dir, 'NPC_.txt')))
    for fid, buckets in usage.items():
        entry = plan.get(fid)
        if entry is None:
            # An NPC naming a hair this plugin does not define (master-owned).
            # Nothing to convert here -- the master's own run emits it.
            continue
        entry['buckets'] |= buckets

    return plan


def _bake_one(job):
    """Bake + convert ONE hair variant.  Runs in a pool worker process.

    Returns (out_stem, written, tinted, error) -- errors come back as data
    rather than raising so one bad mesh cannot kill the pool, matching the
    old loop's per-variant try/except.
    """
    import tempfile
    from .nif_converter import convert_nif

    (rel, src_nif_path, src_tri_path, weight, female, race, group, out_stem,
     out_dir, src_root, src_tex_root) = job

    # THE JOB CARRIES PATHS, NOT BYTES.  One HAIR record becomes many
    # variants (bucket x gender x race group), and embedding the mesh bytes
    # in every job tuple duplicated 3.5 MB of unique NIFs into 102.9 MB
    # spread over ~1605 tuples -- all of which pool.map pickles into the
    # queue up front, on top of 29 spawned interpreters.  The worker reads
    # the file itself instead; the OS page cache makes the re-read free.
    try:
        with open(src_nif_path, 'rb') as fh:
            nif_bytes = fh.read()
        tri_bytes = None
        if src_tri_path:
            with open(src_tri_path, 'rb') as fh:
                tri_bytes = fh.read()
    except OSError as exc:
        return out_stem, False, 0, 'source unreadable: %s' % exc

    try:
        baked_nif, baked_tri = bake_hair_variant(
            nif_bytes, tri_bytes, weight, female, race=race, group=group)
    except Exception as exc:                # noqa: BLE001 - report and go on
        return out_stem, False, 0, 'bake failed: %s' % exc

    # convert_nif works on paths, so stage the baked mesh.  It is written
    # under the source tree's basename so the converter's own path-derived
    # decisions (texture namespacing) see a hair path.
    tmp_dir = tempfile.mkdtemp(prefix='tes4hair_')
    try:
        staged = os.path.join(tmp_dir, os.path.basename(rel))
        with open(staged, 'wb') as fh:
            fh.write(baked_nif)
        dst_nif = os.path.join(out_dir, out_stem + '.nif')
        result = convert_nif(staged, dst_nif, src_meshes_dir=src_root,
                             hair=True)
        if result.get('error'):
            return out_stem, False, 0, 'convert failed: %s' % result['error']
        if not os.path.isfile(dst_nif):
            return out_stem, False, 0, 'convert produced no file'
    finally:
        _rmtree_quiet(tmp_dir)

    # Retype to the Hair Tint shader.  Runs on the CONVERTED file so it lands
    # after the generic property conversion has built the
    # BSLightingShaderProperty; doing it earlier would just be overwritten.
    tinted = _retype_hair_shader(dst_nif, src_tex_root)

    if baked_tri:
        with open(os.path.join(out_dir, out_stem + '.tri'), 'wb') as fh:
            fh.write(baked_tri)
    return out_stem, True, tinted, None


def run(export_dir, out_meshes_dir, *, verbose: bool = True) -> dict:
    """Convert every hair variant this plugin needs.

    Reads the extracted Oblivion hair NIF/.tri pairs, bakes each required
    length, converts the result to Skyrim format through the normal NIF
    converter, and writes the paired Skyrim .tri beside it.
    """

    export_dir = str(export_dir)
    # HAIR.txt/NPC_.txt are RECORDS (export_dir), but the NIF/.tri
    # and textures are SHARED assets, which for an imported mod sit
    # one level up. Joining them onto the record dir finds nothing
    # and every hair silently converts to 'missing'.
    asset_root = str(assets_for(export_dir))
    src_root = os.path.join(asset_root, 'meshes')
    src_tex_root = os.path.join(asset_root, 'textures')
    out_root = str(out_meshes_dir)

    stats = {'hairs': 0, 'variants': 0, 'written': 0, 'missing': 0,
             'errors': 0, 'tinted': 0}

    plan = build_hair_plan(export_dir)
    if not plan:
        return stats

    # Build every variant's job first, then bake them in a process pool:
    # each variant reads immutable source bytes and writes one uniquely
    # named output pair, so they are fully independent.
    jobs = []
    for fid in sorted(plan):
        entry = plan[fid]
        model = entry['model']
        if not model:
            continue
        rel = _norm_model(model)
        src_nif = os.path.join(src_root, *rel.split('/'))
        if not os.path.isfile(src_nif):
            stats['missing'] += 1
            if verbose:
                print('    hair: missing source mesh %s' % rel)
            continue

        stats['hairs'] += 1
        stem = os.path.splitext(os.path.basename(rel))[0]
        src_tri = os.path.splitext(src_nif)[0] + '.tri'
        if not os.path.isfile(src_tri):
            src_tri = None

        out_dir = os.path.join(out_root, *OUT_REL_DIR.split(os.sep))
        os.makedirs(out_dir, exist_ok=True)

        race = _fit_race(entry['edid'])
        if race is not None:
            group_plan = [(None, None)]          # beast pack: its own head
        else:
            lock = _fit_group_lock(entry['edid'])
            if lock is not None:
                # race-named hair: ONE mesh fitted to its group's head; the
                # bare stem is kept (its RNAM already restricts the races)
                group_plan = [(None if lock == 'humans' else lock, None)]
            else:
                # generic hair: one mesh per race group
                group_plan = [(None, None), ('elves', 'elves'),
                              ('orc', 'orc')]

        for bucket, female, (group, name_grp) in [
                (b, f, g) for b in sorted(entry['buckets'])
                for f in entry.get('genders', (False, True))
                for g in group_plan]:
            stats['variants'] += 1
            jobs.append((rel, src_nif, src_tri, bucket_weight(bucket),
                         female, race, group,
                         variant_stem(stem, bucket, female, name_grp),
                         out_dir, src_root, src_tex_root))

    if not jobs:
        return stats

    # ProcessPoolExecutor, not threads: the per-variant work is CPU-bound
    # pure Python (pyffi parse/write plus the numpy head fit), which the GIL
    # serializes.  One worker is spawned in-process to keep small runs (and
    # the tests) free of pool overhead.
    workers = min(_WORKERS, len(jobs))
    if verbose and workers > 1:
        print('  Hair: baking %d variants (%d workers)...'
              % (len(jobs), workers))

    # STREAM the results instead of materialising all of them.  `list(...)`
    # around pool.map holds every finished result alongside every pending
    # job, and a worker being killed there surfaces only as an opaque
    # BrokenProcessPool with no indication of which mesh was in flight.
    # Consuming the iterator lets each result be folded into the stats and
    # dropped, and `chunksize` keeps the queue from being filled with all
    # 721 tuples at once.
    def _iter_results():
        if workers <= 1:
            for job in jobs:
                yield _bake_one(job)
            return
        with ProcessPoolExecutor(max_workers=workers) as pool:
            yield from pool.map(_bake_one, jobs, chunksize=4)

    for out_stem, written, tinted, error in _iter_results():
        if error:
            stats['errors'] += 1
            if verbose:
                print('    hair: %s for %s' % (error, out_stem))
            continue
        stats['written'] += written
        stats['tinted'] += tinted

    if verbose:
        print('  Hair: %d records, %d group/length variants, %d written, '
              '%d hair-tint shaders'
              % (stats['hairs'], stats['variants'], stats['written'],
                 stats['tinted'])
              + (', %d missing' % stats['missing'] if stats['missing'] else '')
              + (', %d errors' % stats['errors'] if stats['errors'] else ''))
    return stats


# Hair diffuses two source meshes name but Oblivion never shipped.  Both are
# authored typos, verified against the extracted texture tree:
#   Grey_Mane.dds  -> named by 3 meshes (khajiitdreds/khajiitheadband/...);
#                     only Mane.dds exists, which is the mane texture they mean.
#   Grey.dds       -> 5 meshes name it with NO folder at all, so it resolves to
#                     textures	es4\Grey.dds instead of the hair folder.
# Left alone the shape renders untextured (Skyrim draws it black/white), which
# is the "some hairs are missing their textures" symptom.
_BS = chr(92)          # backslash, the separator NIF texture paths use
_HAIR_TEX_DIR = 'characters/hair'
_HAIR_TEX_FIXUPS = {
    'grey_mane.dds': 'characters/hair/mane.dds',
    'grey.dds': 'characters/hair/grey.dds',
}


def _resolve_hair_texture(rel: str, textures_root):
    """Repair a hair diffuse path that names a file Oblivion never shipped.

    Returns the corrected mesh-relative path (tes4-prefixed, backslashes) or
    None when the original is fine.
    """
    if not rel:
        return None
    norm = rel.replace(_BS, '/').lower().lstrip('/')
    if norm.startswith('textures/'):
        norm = norm[len('textures/'):]
    if norm.startswith('tes4/'):
        norm = norm[len('tes4/'):]

    if textures_root and os.path.isfile(
            os.path.join(str(textures_root), *norm.split('/'))):
        return None                      # resolves already

    fixed = _HAIR_TEX_FIXUPS.get(os.path.basename(norm))
    if fixed is None:
        # Anything else that lost its folder: put it back in the hair folder.
        if '/' not in norm:
            fixed = '%s/%s' % (_HAIR_TEX_DIR, norm)
        else:
            return None
    if textures_root and not os.path.isfile(
            os.path.join(str(textures_root), *fixed.split('/'))):
        return None                      # the repair target is missing too
    return 'tes4' + _BS + fixed.replace('/', _BS)


# Vanilla hair specular masks (the normal map's alpha) average mean-alpha
# ~17 (HairLong_Old_n 14.2, HairShort_Old_n 17.2, hairlong_n 18.7); the
# converted Oblivion ones average 94 (khajiit_n 197, grey_n 110, short_n 37)
# — 5x hotter, which read in game as "hair still overly shiny" even with the
# vanilla flags/gloss (2026-08-24).  specular_strength is scaled per texture
# so mask*strength lands at the vanilla level.
_VANILLA_SPEC_MASK_MEAN = 17.0
_SPEC_STRENGTH_BASE = 0.8999999761581421
_spec_strength_cache: dict = {}


def _spec_strength_for_normal(path):
    """specular_strength scaled by the normal map's alpha mass, cached."""
    key = os.path.normcase(str(path))
    if key in _spec_strength_cache:
        return _spec_strength_cache[key]
    strength = _SPEC_STRENGTH_BASE
    try:
        from PIL import Image
        import numpy as np
        a = np.array(Image.open(path).convert('RGBA'))[:, :, 3]
        mean = max(float(a.mean()), 1.0)
        strength = float(min(_SPEC_STRENGTH_BASE, max(
            0.05, _SPEC_STRENGTH_BASE * _VANILLA_SPEC_MASK_MEAN / mean)))
    except Exception:
        pass
    _spec_strength_cache[key] = strength
    return strength


def _fix_hair_textures(data, textures_root):
    """Repair broken diffuse paths; return the tint for the texture in use."""
    from . import pyffi_monkey_patch as _patch  # noqa: F401
    from pyffi.formats.nif import NifFormat

    diffuse_rel = None
    for block in data.blocks:
        if not isinstance(block, NifFormat.BSLightingShaderProperty):
            continue
        ts = getattr(block, 'texture_set', None)
        if ts is None:
            continue
        raw = ts.textures[0]
        rel = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw)
        fixed = _resolve_hair_texture(rel, textures_root)
        if fixed:
            ts.textures[0] = fixed.encode('utf-8')
            stem = fixed.rsplit('.', 1)[0]
            ts.textures[1] = (stem + '_n.dds').encode('utf-8')
            rel = fixed
        diffuse_rel = diffuse_rel or rel

    if not diffuse_rel or not textures_root:
        return None, _SPEC_STRENGTH_BASE
    norm = diffuse_rel.replace(_BS, '/').lower().lstrip('/')
    for prefix in ('textures/', 'tes4/'):
        if norm.startswith(prefix):
            norm = norm[len(prefix):]
    path = os.path.join(str(textures_root), *norm.split('/'))
    if not os.path.isfile(path):
        return None, _SPEC_STRENGTH_BASE
    spec = _SPEC_STRENGTH_BASE
    npath = os.path.splitext(path)[0] + '_n.dds'
    if os.path.isfile(npath):
        spec = _spec_strength_for_normal(npath)
    return hair_tint_for_texture(path), spec


def _retype_hair_shader(path, textures_root=None) -> int:
    """Apply the Hair Tint shader to a converted hair NIF on disk.

    Also repairs the diffuse path when the source names a texture that does not
    exist (see _resolve_hair_texture) and derives the tint from the texture the
    mesh actually ends up using.
    """
    from . import pyffi_monkey_patch as _patch  # noqa: F401
    from pyffi.formats.nif import NifFormat
    try:
        data = NifFormat.Data()
        with open(path, 'rb') as fh:
            data.read(fh)
        tint, spec = _fix_hair_textures(data, textures_root)
        n = apply_hair_shader(data, tint=tint, spec_strength=spec)
        if n:
            with open(path, 'wb') as fh:
                data.write(fh)
        return n
    except Exception:
        # A hair that will not re-open is already reported by the convert
        # step; never let the tint pass turn a written mesh into a failure.
        return 0


def _rmtree_quiet(path):
    import shutil
    try:
        shutil.rmtree(path)
    except OSError:
        pass


def output_model_path(model: str, bucket: int, female: bool = False,
                      group=None) -> str:
    """The MODL path an HDPT variant should carry (mesh-relative, TES4 style).

    Mirrors what `run` writes, so the record and the asset stages cannot
    disagree about where a variant lives.
    """
    stem = os.path.splitext(os.path.basename(_norm_model(model)))[0]
    return '%s\\%s.nif' % (OUT_REL_DIR.replace(os.sep, '\\').replace('tes4\\', '', 1),
                           variant_stem(stem, bucket, female, group))


def output_tri_path(model: str, bucket: int, female: bool = False,
                    group=None) -> str:
    """The NAM1 (.tri) path an HDPT variant should carry."""
    return os.path.splitext(
        output_model_path(model, bucket, female, group))[0] + '.tri'


def source_tri_exists(export_dir, model: str) -> bool:
    """True when the Oblivion hair ships the .tri we build the Skyrim one from.

    A handful of hairs have no sibling .tri (4 of Oblivion's 61 meshes).  Those
    still convert -- the geometry is all the engine needs -- but the HDPT must
    not name a NAM1 the asset stage never wrote, or the CK reports a missing
    file for every one of them.
    """
    rel = _norm_model(model)
    if not rel:
        return False
    src = os.path.join(str(assets_for(export_dir)), 'meshes',
                       *rel.split('/'))
    return os.path.isfile(os.path.splitext(src)[0] + '.tri')
