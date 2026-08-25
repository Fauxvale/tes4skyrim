"""Which hair-length variants the import phase must emit HDPTs for.

The asset stage bakes one mesh per (HAIR record, quantized NPC_.LNAM) pair
that some actor actually wears; the import stage has to emit a matching HDPT
for each, and NPC_ has to point at the right one.  Both sides read the same
plan from here so they cannot disagree.

MASTER-EXPORT AWARENESS
    The wearers of a hair are frequently NOT in the plugin that defines it: an
    ESP adds actors who wear Oblivion.esm's hair, and Oblivion.esm's own NPCs
    wear Oblivion.esm's hair.  `load` therefore indexes the CURRENT plugin's
    NPC_ export AND every master export, or a dependent plugin would register
    no lengths at all and every one of its NPCs would silently fall back to
    the bucket-0 mesh regardless of the length its author set.
"""

import os

from asset_convert.hair_pipeline import (collect_hair_usage, hair_genders,
                                         quantize_length, source_tri_exists,
                                         _iter_records)

# hair FormID -> sorted tuple of length buckets needed
_BUCKETS: dict = {}
# hair FormID -> True when the source ships a .tri we can convert
_HAS_TRI: dict = {}
# hair FormID -> tuple of allowed genders as female-bools (authored
# NotMale/NotFemale flags; base gender first)
_GENDERS: dict = {}
# hair FormIDs defined by THIS plugin's own export.  Variant HDPT ids can only
# be derived for these: a master-owned hair's variants live in the MASTER's
# converted plugin under the MASTER's derived ids, so deriving one here mints
# an id that exists in no plugin — a dangling PNAM.  Master-owned hair falls
# back to the base FormID, which the load-order remap resolves correctly.
_OWN: set = set()
_EDIDS: dict = {}
_LOADED = False


def load(export_dir, master_dirs=()) -> dict:
    """Index every (hair, length) pair the converted plugin needs.

    `export_dir` is this plugin's export directory; `master_dirs` are the
    export directories of its masters (see the module docstring).
    """
    global _LOADED
    usage: dict = {}

    for d in [export_dir] + list(master_dirs or ()):
        if not d:
            continue
        npc_txt = os.path.join(str(d), 'NPC_.txt')
        for fid, buckets in collect_hair_usage(_iter_records(npc_txt)).items():
            usage.setdefault(fid, set()).update(buckets)

    _BUCKETS.clear()
    for fid, buckets in usage.items():
        _BUCKETS[_base(fid)] = tuple(sorted(buckets))

    # Which hairs ship a .tri (so an HDPT never names a NAM1 that was not
    # written) and which genders each allows (which HDPT variants exist).
    # Indexed from every export that DEFINES hair, not just this plugin's,
    # for the same master-blindness reason as the usage scan.
    _HAS_TRI.clear()
    _GENDERS.clear()
    _OWN.clear()
    _EDIDS.clear()
    for d in [export_dir] + list(master_dirs or ()):
        if not d:
            continue
        for rec in _iter_records(os.path.join(str(d), 'HAIR.txt')):
            raw = (rec.get('FormID') or '').strip()
            try:
                fid = int(raw, 16)
            except ValueError:
                continue
            _HAS_TRI[_base(fid)] = source_tri_exists(
                d, rec.get('Model.MODL') or '')
            try:
                flags = int((rec.get('DATA.Flags') or '0').strip() or '0')
            except ValueError:
                flags = 0
            _GENDERS[_base(fid)] = hair_genders(flags)
            _EDIDS[_base(fid)] = (rec.get('EditorID') or '').strip()
            if d == export_dir:
                _OWN.add(_base(fid))

    _LOADED = True
    return _BUCKETS


def hair_edid(hair_fid: int) -> str:
    """The hair record's EditorID ('' when unknown)."""
    return _EDIDS.get(_base(hair_fid), '')


def hair_has_tri(hair_fid: int) -> bool:
    """True when this hair's source shipped a .tri (so NAM1 is safe to write)."""
    return bool(_HAS_TRI.get(_base(hair_fid)))


def genders_for(hair_fid: int) -> tuple:
    """Allowed genders for a hair, as female-bools with the BASE gender first.

    The base gender's bucket-0 HDPT keeps the source FormID (see
    actors.hair_variant_formid).  Unknown hairs default to unisex.
    """
    return _GENDERS.get(_base(hair_fid)) or (False, True)


def is_own_hair(hair_fid: int) -> bool:
    """True when THIS plugin's export defines the hair (see _OWN)."""
    return _base(hair_fid) in _OWN


def _base(hair_fid: int) -> int:
    """Strip the load-order index byte from a FormID.

    The index is assigned per plugin at import time, so it is NOT part of a
    record's identity: the export dumps HAIR FormID 000C4821 while the record
    reaching convert_HAIR carries 010C4821 (index 01).  Indexing on the raw
    value makes every lookup miss silently and every NPC fall back to the
    bucket-0 mesh, which is exactly what it did before this mask existed.
    """
    return (hair_fid or 0) & 0x00FFFFFF


def hair_buckets_for(hair_fid: int) -> tuple:
    """Length buckets to emit for a HAIR record.

    Always includes 0 -- a hair no NPC currently wears still needs its base
    HDPT, because leveled actors and other plugins can name it and a missing
    HDPT is a dangling PNAM.
    """
    if not hair_fid:
        return (0,)
    got = _BUCKETS.get(_base(hair_fid))
    if not got:
        return (0,)
    return got if 0 in got else (0,) + got


def bucket_for_npc(rec) -> int:
    """The length bucket an NPC_ record's authored LNAM falls in."""
    from .text_reader import get_float
    return quantize_length(get_float(rec, 'LNAM.HairLength', 0.0))

