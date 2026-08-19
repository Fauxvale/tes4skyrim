"""Which wearable NIF variants the converted plugin actually references.

A converted wearable can exist on disk in three forms:

    armor/iron/m/cuirass.nif      the plain converted mesh
    armor/iron/m/cuirass_0.nif    weight-0 variant
    armor/iron/m/cuirass_1.nif    weight-1 variant (body-morphed)

but the plugin never references all three for the same mesh, so writing all
three always wastes space:

  * ARMA sets the weight slider ONLY for gear covering body/hands/feet
    (tes5_import.record_types.equipment._build_arma).  With the slider on it
    references <name>_1.nif and the engine derives its partner _0 — the plain
    <name>.nif is dead.  With the slider off it references the plain
    <name>.nif — both _0 and _1 are dead.
  * ARMO's ground (dropped-item) model is the WorldModel when there is one and
    otherwise falls back to the biped path, which keeps the plain <name>.nif
    alive for the ~76 shields and odds and ends that ship no _gnd mesh.

This module derives that same set of decisions straight from the export, so the
converter writes exactly the files the plugin asks for.  The rules here MUST
track equipment.convert_ARMO / _build_arma — if the slider condition or the
ground-model fallback changes there, change it here too.
"""

import os
from pathlib import Path

# TES4 BMDT biped bits 2=UpperBody 3=LowerBody 4=Hand 5=Foot — the gear the
# vanilla weight slider applies to.  Mirrors _build_arma's `use_slider`.
_SLIDER_BIPED_MASK = 0b111100

# Variant flags
BASE = 1        # <name>.nif
W0 = 2          # <name>_0.nif
W1 = 4          # <name>_1.nif
WORN = 8        # named as an ARMA worn (biped) model by some ARMO/CLOT record

# TES4 BMDT biped bit -> the Skyrim body part the geometry belongs in.  This is
# the plugin's OWN statement of what the item is, so it replaces guessing the
# slot from the filename ('helm' in the stem) or from the geometry name.
# Bit 0 Head, 1 Hair, 2 UpperBody, 3 LowerBody, 4 Hand, 5 Foot.
_SBP_131_HAIR = 131
_SBP_32_BODY = 32
_SBP_44_LOWERBODY = 44
_SBP_33_HANDS = 33
_SBP_37_FEET = 37
_SBP_36_RING = 36
_SBP_40_NECK = 40
_BIPED_BIT_BODY_PART = [
    (0, _SBP_131_HAIR),        # Head  -> helmets ride Skyrim's hair slot
    (1, _SBP_131_HAIR),        # Hair
    (2, _SBP_32_BODY),
    (3, _SBP_44_LOWERBODY),
    (4, _SBP_33_HANDS),
    (5, _SBP_37_FEET),
    # Jewellery.  Bit meanings per xEdit wbBipedFlags (wbDefinitionsTES4.pas):
    # 6 Right Ring, 7 Left Ring, 8 Amulet -- NOT 7=amulet/8=tail (tail is 15).
    # Slots measured from vanilla Skyrim: goldring_1.nif partitions as 36,
    # amulet.nif as 40.  Listed last so a ring that also claims a body slot is
    # still slotted by the body one.
    (6, _SBP_36_RING),         # Right Ring
    (7, _SBP_36_RING),         # Left Ring
    (8, _SBP_40_NECK),         # Amulet
]


def body_part_for_flags(biped_flags: int):
    """Skyrim body part implied by a record's BMDT biped flags, or None.

    Head/Hair win when set: a helmet that also claims UpperBody is still
    headgear.  None means the flags say nothing useful and the caller should
    fall back to inspecting the mesh.
    """
    for bit, bp in _BIPED_BIT_BODY_PART:
        if biped_flags & (1 << bit):
            return bp
    return None


def _norm(path: str) -> str:
    """Normalise an export model path to a lowercase mesh-relative key.

    The export escapes backslashes, so a model path arrives as
    'armor\\\\fur\\\\m\\\\gauntlets.nif' — collapse the doubling, or every key
    ends up with '//' separators and never matches a real relative path.
    """
    p = path.strip().lower().replace('\\\\', '\\').replace('\\', '/')
    while '//' in p:
        p = p.replace('//', '/')
    return p.lstrip('/')


def _iter_records(txt: Path):
    if not txt.is_file():
        return
    body = txt.read_text(encoding='utf-8', errors='replace')
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


def build_biped_flags(export_dir) -> dict:
    """Map mesh-relative NIF path -> the BMDT biped flags of the record wearing it.

    The authored answer to 'what slot is this?', which the converter previously
    guessed from the filename stem.  A mesh worn by several records ORs their
    flags together; in practice they agree.
    """
    export_dir = Path(export_dir)
    flags: dict = {}
    for name in ('ARMO.txt', 'CLOT.txt'):
        for rec in _iter_records(export_dir / name):
            try:
                bf = int(rec.get('BMDT.BipedFlags', '0') or 0)
            except ValueError:
                continue
            if not bf:
                continue
            for key in ('Male.BipedModel.MODL', 'Female.BipedModel.MODL'):
                mp = rec.get(key, '').strip()
                if mp:
                    k = _norm(mp)
                    flags[k] = flags.get(k, 0) | bf
    return flags


# Key under which build_plan stashes the biped-flag map inside the plan dict.
# _norm() lowercases and strips leading '/', so it can never emit this string —
# the sub-map cannot collide with a real mesh entry.
BIPED_FLAGS_KEY = '*biped_flags*'


def biped_flags_for(plan: dict, src_path, meshes_root) -> int:
    """BMDT biped flags for a source NIF, or 0 when no record wears it.

    Accepts the plan dict returned by build_plan (the flag map rides along
    under BIPED_FLAGS_KEY) or a bare flag map.
    """
    if not plan:
        return 0
    flags = plan.get(BIPED_FLAGS_KEY)
    if not isinstance(flags, dict):
        flags = plan
    try:
        rel = os.path.relpath(str(src_path), str(meshes_root))
    except (ValueError, TypeError):
        return 0
    val = flags.get(_norm(rel), 0)
    return val if isinstance(val, int) else 0


def body_parts_for_flags(biped_flags: int) -> list:
    """EVERY Skyrim body part a record's BMDT flags claim.

    body_part_for_flags returns the single most head-ward one; this returns the
    whole set, which is what a mesh holding several shapes needs to be resolved
    against (see skin_retarget._body_part_from_skin_bones).
    """
    out = []
    for bit, bp in _BIPED_BIT_BODY_PART:
        if biped_flags & (1 << bit) and bp not in out:
            out.append(bp)
    return out


def build_plan(export_dir) -> dict:
    """Map mesh-relative NIF path -> bitmask of the variants the plugin uses.

    *export_dir* is the per-plugin export directory (e.g. export/Oblivion.esm).
    Paths absent from the result are referenced by no ARMO/CLOT record.
    """
    export_dir = Path(export_dir)
    plan: dict = {}

    def want(path: str, flags: int):
        if path:
            key = _norm(path)
            plan[key] = plan.get(key, 0) | flags

    for name in ('ARMO.txt', 'CLOT.txt'):
        for rec in _iter_records(export_dir / name):
            male_biped = rec.get('Male.BipedModel.MODL', '').strip()
            female_biped = rec.get('Female.BipedModel.MODL', '').strip()
            male_world = rec.get('Male.WorldModel.MODL', '').strip()
            female_world = rec.get('Female.WorldModel.MODL', '').strip()
            try:
                biped_flags = int(rec.get('BMDT.BipedFlags', '0') or 0)
            except ValueError:
                biped_flags = 0

            # ARMA worn models (MOD2/MOD3): _1 + engine-derived _0 when the
            # slider is on, otherwise the plain mesh.  WORN rides along on every
            # biped reference — it is what marks the mesh as body-worn gear, a
            # fact only the plugin knows (see is_worn).
            worn_flags = WORN | (
                (W0 | W1) if (biped_flags & _SLIDER_BIPED_MASK) else BASE)
            want(male_biped, worn_flags)
            want(female_biped or male_biped, worn_flags)

            # ARMO ground models (MOD2/MOD4): always the plain mesh, and the
            # biped mesh stands in when the record ships no world model.
            # A female-only wearable has no male field at all, so the fallback
            # has to reach across genders or its dropped item has no mesh —
            # this must track convert_ARMO's ground_model expression exactly.
            want(male_world or male_biped or female_world or female_biped, BASE)
            want(female_world, BASE)

    # Carry the authored slot data alongside, so callers that need to know what
    # a mesh IS (not just which variants to write) do not re-parse the export.
    plan[BIPED_FLAGS_KEY] = build_biped_flags(export_dir)
    return plan


def variants_for(plan: dict, src_path, meshes_root) -> int:
    """Variant bitmask for a source NIF, or BASE if the plugin never names it.

    Meshes no ARMO/CLOT references (loose test assets, unused BSA content) keep
    their plain conversion and gain no weight variants.
    """
    try:
        rel = os.path.relpath(str(src_path), str(meshes_root))
    except ValueError:
        return BASE
    return plan.get(_norm(rel), BASE)


def is_worn(plan: dict, src_path, meshes_root) -> bool:
    """True if some ARMO/CLOT record wears this NIF on the body.

    The converter used to answer this by looking for 'armor' or 'clothes' in the
    source path.  That holds for vanilla Oblivion, which files every wearable
    under meshes\\armor or meshes\\clothes, but it is a guess about a naming
    convention rather than a fact about the plugin — Nehrim ships 88 worn meshes
    under its own folders (eyren/, spinat/, nehrim/, skeletonk/, ...) and every
    one of them was converted as a world object: BSFadeNode root instead of
    NiNode, plain NiSkinInstance instead of BSDismemberSkinInstance, and no
    retarget onto the Skyrim skeleton, so the engine draws nothing where the
    body should be.  The plugin's own biped model references are the authored
    answer, so ask them.
    """
    if not plan:
        return False
    try:
        rel = os.path.relpath(str(src_path), str(meshes_root))
    except (ValueError, TypeError):
        return False
    return bool(plan.get(_norm(rel), 0) & WORN)
