"""Creature footstep/impact sound chain: IPCT -> IPDS -> FSTP -> FSTS -> ARMA.

WHY THIS EXISTS (root cause of "converted creatures make no sound at all"):

Oblivion keeps a creature's footstep sounds in CREA CSDT slots 0-3 (left foot,
right foot, left back foot, right back foot). Skyrim does not read anything
like that off the actor — footsteps come from the body ARMA's `SNDD` field,
which points at a Footstep Set, through this chain:

    ARMA.SNDD -> FSTS (per-gait footstep lists)
                   -> FSTP (one per footstep "tag")
                        -> IPDS (material -> impact table)
                             -> IPCT (one impact, carrying the sound)
                                  -> SNDR (the actual sound descriptor)

Every converted creature ARMA had `SNDD` absent, so no footstep ever played —
and this is the channel vanilla actually uses for creature locomotion audio.
Measured before the fix: 0 IPCT / 0 IPDS / 0 FSTP / 0 FSTS records in the whole
output, and all 63 creature ARMAs with SNDD=NONE.

Vanilla template followed exactly (Skyrim.esm NPCWolfFootFrontWalk* chain):
  * IPCT = EDID + DATA(24) + DODT(36) + SNAM->SNDR. No model; footstep impacts
    are sound-only.
  * IPDS = EDID + one PNAM(8) per MATERIAL, each mapping MATT -> our single
    IPCT. Wolf lists 60 materials all pointing at ONE impact, so a creature
    sounds the same on every surface; we reproduce that list verbatim
    (_FOOTSTEP_MATERIALS) rather than inventing per-material variants.
  * FSTP = EDID + DATA->IPDS + ANAM tag string.
  * FSTS = EDID + XCNT(20: walk/run/sprint/sneak/swim counts) + DATA, the
    FSTP FormID arrays. NOTE the DATA array order is the REVERSE of XCNT:
    swim, sneak, sprint, run, walk (xEdit wbDefinitionsTES5 line 7108).

Allocation order: this runs LAST, next to build_creature_voice_types, so it
cannot shift any pre-existing generated FormID (see
project_formid_allocation_order_contract). The ARMA records are already packed
by then, so their SNDD is applied by patching the packed bytes — but unlike
VTCK, ARMA has no SNDD placeholder to overwrite (it is a genuinely new
subrecord), so patch_creature_footsteps INSERTS it and fixes the record's
dataSize header.
"""

import struct

from .writer import (pack_record, pack_string_subrecord, pack_subrecord,
                     pack_formid_subrecord)

# --------------------------------------------------------------------------
# Vanilla-derived constants
# --------------------------------------------------------------------------

# IPCT DATA(24), copied byte-for-byte from NPCWolfFootFrontWalkImpact:
# duration 0.25, orientation 2 (Projectile Reflection), angleThreshold 15.0,
# placementRadius 16.0, soundLevel 1 (Normal), flags 0, result 0, unk 2.
_IPCT_DATA = bytes.fromhex(
    '0000803E0200000000007041000080410100000000000000')

# IPCT DODT(36) decal data, same source record.
_IPCT_DODT = bytes.fromhex(
    '0000004100000042000000410000004200000042000080400000803F'
    '04000000FFFFFF00')

# Every MATT material NPCWolfFootFrontWalkImpactSet lists (60 of them), all
# mapped to the one impact. Keeping the full list means a creature is audible
# on every surface type rather than only the handful we might guess at.
_FOOTSTEP_MATERIALS = (
    0x00012F3D, 0x00012F38, 0x000388FC, 0x0005178C, 0x00012F41, 0x0004D2B3,
    0x00050DBE, 0x000774C0, 0x000D309F, 0x0009150B, 0x00097786, 0x0005A28F,
    0x00084782, 0x000876B8, 0x00012F35, 0x00012F3C, 0x00012F44, 0x0009644C,
    0x00012F3A, 0x00021340, 0x000AF63D, 0x000774C1, 0x00012F40, 0x00012F39,
    0x00012F45, 0x00012F48, 0x00012F42, 0x00012F36, 0x0002FD39, 0x000C1AD7,
    0x000C8244, 0x00018401, 0x00070343, 0x00012F3F, 0x00028E99, 0x000363B0,
    0x0002EE2C, 0x0005ADD4, 0x00077251, 0x00052798, 0x00052ED0, 0x0001A353,
    0x00012F47, 0x0001EFF7, 0x00012F46, 0x000774C2, 0x000876B5, 0x00043DCC,
    0x00016978, 0x0002EE2D, 0x00012F37, 0x00012F3E, 0x00012F43, 0x00016979,
    0x00050AFA, 0x000388FB, 0x0001C151, 0x000774B6, 0x00012F34, 0x00012F3B,
)

# TES4 CSDT sound-type slots that are footsteps.
_FOOT_SLOTS = (0, 1, 2, 3)

# folder -> FSTS FormID, filled by build_creature_footsteps()
_CREA_FSTS_MAP = {}


def reset_creature_footsteps() -> None:
    _CREA_FSTS_MAP.clear()


def get_creature_footstep_set(folder: str) -> int:
    """FSTS FormID for a creature folder, or 0."""
    return _CREA_FSTS_MAP.get(folder, 0)


def _build_one(writer, folder: str, sndr_fids: list) -> int:
    """IPCT+IPDS+FSTP per distinct foot sound, then one FSTS. Returns FSTS."""
    base = folder.capitalize()
    fstp_fids = []
    for i, sndr in enumerate(sndr_fids):
        tag = f'TES4{base}Foot{i + 1}'

        ipct_fid = writer.alloc_formid()
        subs = pack_string_subrecord('EDID', f'{tag}Impact')
        subs += pack_subrecord('DATA', _IPCT_DATA)
        subs += pack_subrecord('DODT', _IPCT_DODT)
        subs += pack_formid_subrecord('SNAM', sndr)
        writer.add_record('IPCT', pack_record('IPCT', ipct_fid, 0, subs))

        ipds_fid = writer.alloc_formid()
        subs = pack_string_subrecord('EDID', f'{tag}ImpactSet')
        for mat in _FOOTSTEP_MATERIALS:
            subs += pack_subrecord('PNAM', struct.pack('<II', mat, ipct_fid))
        writer.add_record('IPDS', pack_record('IPDS', ipds_fid, 0, subs))

        fstp_fid = writer.alloc_formid()
        subs = pack_string_subrecord('EDID', f'{tag}Footstep')
        subs += pack_formid_subrecord('DATA', ipds_fid)
        subs += pack_string_subrecord('ANAM', tag)
        writer.add_record('FSTP', pack_record('FSTP', fstp_fid, 0, subs))
        fstp_fids.append(fstp_fid)

    # Same footstep list for every gait — vanilla creature sets reuse the same
    # FSTPs across walk/run/sprint/sneak and only trim the swim list.
    n = len(fstp_fids)
    fsts_fid = writer.alloc_formid()
    subs = pack_string_subrecord('EDID', f'TES4{base}FootstepSet')
    # XCNT order: walking, running, sprinting, sneaking, swimming
    subs += pack_subrecord('XCNT', struct.pack('<IIIII', n, n, n, n, 0))
    # DATA order is the REVERSE: swimming, sneaking, sprinting, running, walking
    data = b''
    for group in ([], fstp_fids, fstp_fids, fstp_fids, fstp_fids):
        for f in group:
            data += struct.pack('<I', f)
    subs += pack_subrecord('DATA', data)
    writer.add_record('FSTS', pack_record('FSTS', fsts_fid, 0, subs))
    return fsts_fid


def build_creature_footsteps(writer, sound_slots: dict,
                             sndr_for_soun) -> int:
    """Phase LAST: one footstep chain per creature folder that has foot sounds.

    sound_slots: {folder: {csdt_type: SOUN EditorID}} from the creature
                 pipeline (asset_convert.creature_pipeline).
    sndr_for_soun: callable EditorID -> SNDR FormID (0 when absent).

    Returns the number of folders wired.
    """
    _CREA_FSTS_MAP.clear()
    if not sound_slots:
        return 0
    for folder in sorted(sound_slots):
        slots = sound_slots[folder] or {}
        # Distinct foot sounds, in slot order; most creatures use one sound for
        # both front feet, quadrupeds a second for the back pair.
        seen, sndrs = set(), []
        for slot in _FOOT_SLOTS:
            edid = slots.get(slot)
            if not edid or edid in seen:
                continue
            fid = sndr_for_soun(edid)
            if fid:
                seen.add(edid)
                sndrs.append(fid)
        if not sndrs:
            continue
        _CREA_FSTS_MAP[folder] = _build_one(writer, folder, sndrs)
    return len(_CREA_FSTS_MAP)


def patch_creature_footsteps(writer, arma_folder: dict) -> int:
    """Insert ARMA.SNDD -> the folder's FSTS on every creature body ARMA.

    arma_folder: {arma_fid: folder}. ARMA has no SNDD placeholder (unlike
    VTCK), so this INSERTS the subrecord and rewrites the 24-byte record
    header's dataSize. Idempotent: an ARMA that already carries SNDD is left
    alone.

    Per the TES5 ARMA definition the field order is
    ... MODL[] (Additional Races), SNDD, ONAM — and our creature ARMAs write
    neither MODL nor ONAM, so appending at the end is correct.
    """
    if not _CREA_FSTS_MAP or not arma_folder:
        return 0
    records = writer._top_groups.get('ARMA') or []
    patched = 0
    for i, blob in enumerate(records):
        if len(blob) < 24:
            continue
        fid = struct.unpack_from('<I', blob, 12)[0]
        folder = arma_folder.get(fid)
        if not folder:
            continue
        fsts = _CREA_FSTS_MAP.get(folder)
        if not fsts or blob.find(b'SNDD', 24) >= 0:
            continue
        sub = pack_formid_subrecord('SNDD', fsts)
        new = blob + sub
        size = struct.unpack_from('<I', new, 4)[0] + len(sub)
        records[i] = new[:4] + struct.pack('<I', size) + new[8:]
        patched += 1
    return patched
