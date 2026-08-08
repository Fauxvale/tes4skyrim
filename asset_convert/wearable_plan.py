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
