"""Actor/NPC converters: NPC_, CREA, FACT, EYES, HAIR, CLAS, GLOB, GMST, leveled lists."""

import re
import struct

from ..constants import DEFAULT_RACE, RACE_MAP, TES4_SKILL_TO_TES5, TES5_SKILL_ORDER
from ..npc_face_mapper import build_face_tail_subs, build_pnam_subs
from ..outfits import split_inventory
from ..packages import (
    CLAS_CREATURE_CASTER,
    CLAS_CREATURE_PREDATOR,
    CSTY_ANIMAL,
    CSTY_DEFAULT,
    DPLT_CREATURE_LIST,
    DPLT_NPC_LIST,
    PKID_CREATURE_MASTER,
    npc_packages,
)
from ..skyrim_overrides import (
    ATTRIBUTE_SKILL_MAP,
    TES4_RACE_FID_TO_EDID,
    VOICE_TYPE_MAP,
    map_hair_color,
    resolve_creature_race,
)
from .common import (
    _prefix_path,
    get_float,
    get_formid,
    get_int,
    get_str,
    pack_float_subrecord,
    pack_formid_subrecord,
    pack_obnd,
    pack_record,
    pack_string_subrecord,
    pack_subrecord,
    pack_uint8_subrecord,
    pack_uint32_subrecord,
)


def _read_items(rec: dict) -> list:
    """Actor's TES4 CNTO inventory as [(fid, count)], in export order.

    TES4 merchant inventories use NEGATIVE counts for restocking stock;
    Skyrim restocks via respawn and treats count < 1 as adding nothing
    (a CK warning per entry), so counts are normalized to at least 1.
    """
    items = []
    for i in range(get_int(rec, 'ItemCount')):
        fid = get_formid(rec, f'Item[{i}].FormID')
        if fid:
            count = abs(get_int(rec, f'Item[{i}].Count', 1)) or 1
            items.append((fid, count))
    return items


# TES4 CREA sound types written to the TES5 record. Both games use the SAME
# enum for 0-9 (xEdit wbSoundTypeSounds is one shared struct), but the census
# of all 5118 vanilla Skyrim NPC_ records is the contract for which slots the
# TES5 ENGINE actually reads from the record: 31 Hit, 4 Attack, 1 Left Foot,
# and ZERO of everything else. So only Hit rides the record. The other slots
# are delivered through the channels vanilla uses for them:
#   0-3 feet  -> ARMA.SNDD footstep chain (creature_footsteps) + FootFront/
#                FootBack animation events
#   4 Idle / 5 Aware -> single-play vocal states entered via ActionIdle/
#                ActionIdleWarn IDLE records (hkx_behavior vocal states +
#                creature_idles) — NEVER per-loop clip annotations, and never
#                chance-100 record slots: both made the creature vocalize
#                non-stop, continuing after death
#   6 Attack -> SoundPlay annotation at the swing frame of each attack clip
#   8 Death  -> annotation on the death clip when one exists (writing the
#                slot vanilla never writes risks untested engine paths in
#                the kill flow)
# (Attack is annotation-driven here even though vanilla has 4 record
# entries — writing both channels would double the bark.)
_RECORD_SOUND_TYPES = (7,)          # Hit

# NPC_ NAM8 'Sound Level' (wbSoundLevelEnum: 0 Loud, 1 Normal, 2 Silent).
# Vanilla writes Normal on 5116 of 5118 NPC_ records.
_SOUND_LEVEL_NORMAL = 1

# NPC_ NAM5 is wbUnknown in xEdit, so its width comes from the binary: every
# NPC_ in the real Skyrim.esm writes exactly 2 bytes, FF 00 (verified over 60
# decompressed records). Writing it as a u32 would desync the subrecord stream.
_NAM5_UNKNOWN = b'\xff\x00'


def _actor_sound_subs(rec: dict) -> bytes:
    """CSDT/CSDI/CSDC array for a TES4 CREA, in TES5 NPC_ order.

    Only the slots in _RECORD_SOUND_TYPES are written (the vanilla-census
    channel split — see the table above); the rest of the creature's voice
    travels through animation annotations, vocal idle states and the
    footstep chain. CSDC is the AUTHORED TES4 play-chance (the same shared
    wbSoundTypeSounds struct in both games), not a hardcoded 100.

    CSDI is written as the TES4 SOUN FormID and patched to the real SNDR after
    Phase 3 (patch_actor_sounds). The descriptor does not exist yet, and
    pre-allocating its id here would shift every other generated FormID — which
    is what broke the Slot44 patch and left NPCs unarmoured. See
    dialog_misc._SNDR_FOR_SOUN.

    Returns b'' when the creature has no own sounds; the caller then falls back
    to CSCR inheritance, exactly as both games do (817 of 909 Oblivion CREA and
    725 of Skyrim's NPC_ records inherit rather than define).
    """
    subs = b''
    for i in range(get_int(rec, 'SoundTypeCount')):
        stype = get_int(rec, f'SoundType[{i}].Type', -1)
        if stype not in _RECORD_SOUND_TYPES:
            continue
        # first sound keeps the un-indexed key; a type may list several
        pairs, j = [], 0
        while True:
            key = (f'SoundType[{i}].Sound' if j == 0
                   else f'SoundType[{i}].Sound[{j}]')
            soun = get_formid(rec, key)
            if not soun:
                break
            pairs.append((soun, get_int(rec, f'{key}.Chance', 100)))
            j += 1
        if not pairs:
            continue
        subs += pack_uint32_subrecord('CSDT', stype)
        for soun, chance in pairs:
            subs += pack_formid_subrecord('CSDI', soun)   # placeholder
            subs += pack_uint8_subrecord('CSDC', max(0, min(100, chance)))
    return subs


def patch_actor_sounds(writer) -> int:
    """Rewrite every actor CSDI from its TES4 SOUN id to the real SNDR id.

    Actors are written long before Phase 3 creates the sound descriptors, so
    conversion stores the SOUN FormID and this resolves it — the same
    placeholder-then-patch approach pack_converter.patch_forcegreet_topics uses
    for PDTO, and the reason no FormID is allocated early (which would shift
    every other generated id).

    A CSDI whose SOUN produced no descriptor is dropped along with its CSDT/
    CSDC, rather than left pointing at a record that does not exist.

    An OVERRIDE of a master's actor is different: its bytes come from the
    master's already-converted record, so the CSDI already holds a real SNDR
    in the MASTER's id space.  `sndr_map` is keyed on the low 24 bits only, so
    masking such an id looks it up in the wrong space, finds nothing, and the
    whole CSDT/CSDI/CSDC group is dropped -- which silently stripped the sound
    block from all six creature-derived NPC_ overrides in Knights.esp
    (CreatureWolf's `CSDI 016D9202` is `TES4_NPCWolfInjured_SNDR`).  A CSDI
    whose index byte names a MASTER is already resolved and is left alone.
    """
    from .dialog_misc import sndr_map
    mapping = sndr_map()
    records = writer._top_groups.get('NPC_') or []
    if not mapping or not records:
        return _flatten_cscr(records) if records else 0
    # An already-resolved CSDI must survive a second pass untouched, so treat
    # every descriptor id as mapping to itself.
    resolved = {v for v in mapping.values()}
    # Index bytes below our own slot belong to a master; anything there was
    # resolved when that master was converted.
    own_index = writer.own_index
    patched = 0
    for i, blob in enumerate(records):
        if b'CSDI' not in blob:
            continue
        out = bytearray(blob[:24])
        pos = 24
        changed = False
        # Walk the subrecord stream. A group is one CSDT followed by its
        # CSDI/CSDC pairs (a type may list several sounds); the CSDT is only
        # emitted if at least one of its sounds resolved.
        cur_type = b''          # pending CSDT chunk
        cur_pairs = b''         # resolved CSDI/CSDC pairs for it
        drop_csdc = False       # current CSDI was dropped -> drop its CSDC

        def _flush():
            nonlocal cur_type, cur_pairs
            if cur_type and cur_pairs:
                out.extend(cur_type + cur_pairs)
            cur_type, cur_pairs = b'', b''

        while pos + 6 <= len(blob):
            sig = blob[pos:pos + 4]
            size = struct.unpack_from('<H', blob, pos + 4)[0]
            chunk = blob[pos:pos + 6 + size]
            pos += 6 + size
            if sig == b'CSDT':
                _flush()
                cur_type = chunk
                drop_csdc = False
            elif sig == b'CSDI' and cur_type:
                soun = struct.unpack_from('<I', chunk, 6)[0]
                if soun and (soun >> 24) < own_index:
                    sndr = soun          # master-owned, already an SNDR
                else:
                    sndr = (soun if soun in resolved
                            else mapping.get(soun & 0x00FFFFFF, 0))
                if sndr:
                    cur_pairs += chunk[:6] + struct.pack('<I', sndr)
                    drop_csdc = False
                    changed = changed or sndr != soun
                else:
                    drop_csdc = True
                    changed = True
            elif sig == b'CSDC' and cur_type:
                if not drop_csdc:
                    cur_pairs += chunk
                drop_csdc = False
            else:
                _flush()
                out += chunk
        _flush()
        if not changed:
            continue
        struct.pack_into('<I', out, 4, len(out) - 24)   # data size
        records[i] = bytes(out)
        patched += 1
    patched += _flatten_cscr(records)
    return patched


def _sound_chunks(blob: bytes) -> bytes:
    """The record's CSDT/CSDI/CSDC subrecord bytes, contiguous, or b''."""
    out, pos = b'', 24
    while pos + 6 <= len(blob):
        sig = blob[pos:pos + 4]
        size = struct.unpack_from('<H', blob, pos + 4)[0]
        if sig in (b'CSDT', b'CSDI', b'CSDC'):
            out += blob[pos:pos + 6 + size]
        pos += 6 + size
    return out


def _flatten_cscr(records: list) -> int:
    """Replace every actor CSCR with the target's own resolved CSDT array.

    TES4 lets inheritance CHAIN (rat variant -> base rat -> ...); vanilla
    Skyrim CSCR always points ONE hop to an actor with a direct array, and
    the sound-commit regression window is exactly when non-vanilla record
    shapes entered the kill path — so instead of shipping chains (or CSCR
    targets that themselves were skipped records), resolve the chain here
    and write the sounds directly. Runs after the CSDI->SNDR patch so the
    inlined bytes already carry real descriptor ids.
    """
    by_fid = {struct.unpack_from('<I', blob, 12)[0]: i
              for i, blob in enumerate(records)}

    def find_cscr(blob):
        """(offset, size) of the CSCR subrecord via a proper subrecord walk
        (a raw .find could match 'CSCR' inside another subrecord's data)."""
        pos = 24
        while pos + 6 <= len(blob):
            sig = blob[pos:pos + 4]
            size = struct.unpack_from('<H', blob, pos + 4)[0]
            if sig == b'CSCR':
                return pos, size
            pos += 6 + size
        return None

    def resolved_sounds(blob, depth=0):
        own = _sound_chunks(blob)
        if own:
            return own
        hit = find_cscr(blob)
        if hit is None or depth >= 4:
            return b''
        target = struct.unpack_from('<I', blob, hit[0] + 6)[0]
        k = by_fid.get(target)
        return resolved_sounds(records[k], depth + 1) if k is not None else b''

    patched = 0
    for i, blob in enumerate(records):
        hit = find_cscr(blob)
        if hit is None:
            continue
        j, size = hit
        inline = resolved_sounds(blob)
        out = bytearray(blob[:j]) + inline + blob[j + 6 + size:]
        struct.pack_into('<I', out, 4, len(out) - 24)   # data size
        records[i] = bytes(out)
        patched += 1
    return patched


def _build_outfit(writer, edid: str, outfit_fids: list,
                  source_fid: int = 0) -> int:
    """Emit the OTFT companion record for an actor and return its FormID."""
    otft_fid = writer.derive_formid('OTFT', source_fid or edid)
    subs = pack_string_subrecord('EDID', edid)
    # INAM — item FormIDs packed as consecutive 4-byte LE uint32
    subs += pack_subrecord(
        'INAM', b''.join(struct.pack('<I', fid) for fid in outfit_fids))
    writer.add_record('OTFT', pack_record('OTFT', otft_fid, 0, subs))
    return otft_fid


def _npc_skills_dnam(rec: dict) -> bytes:
    """Build TES5 NPC_ DNAM subrecord (52 bytes, skills + stats)."""
    dnam = bytearray(52)
    skill_vals = {}
    skill_names_tes4 = [
        "Armorer", "Athletics", "Blade", "Block", "Blunt",
        "HandToHand", "HeavyArmor", "Alchemy", "Alteration",
        "Conjuration", "Destruction", "Illusion", "Mysticism",
        "Restoration", "Acrobatics", "LightArmor", "Marksman",
        "Mercantile", "Security", "Sneak", "Speechcraft"
    ]
    tes4_to_tes5_skill = {
        "Armorer": "Smithing", "Blade": "OneHanded", "Block": "Block",
        "Blunt": "OneHanded", "HandToHand": "OneHanded",
        "HeavyArmor": "HeavyArmor", "Alchemy": "Alchemy",
        "Alteration": "Alteration", "Conjuration": "Conjuration",
        "Destruction": "Destruction", "Illusion": "Illusion",
        "Mysticism": "Illusion", "Restoration": "Restoration",
        "LightArmor": "LightArmor", "Marksman": "Marksman",
        "Mercantile": "Pickpocket", "Security": "Lockpicking",
        "Sneak": "Sneak", "Speechcraft": "Speechcraft",
    }
    for tes4_name in skill_names_tes4:
        val = get_int(rec, f'DATA.{tes4_name}')
        tes5_name = tes4_to_tes5_skill.get(tes4_name)
        if tes5_name and val:
            skill_vals[tes5_name] = max(skill_vals.get(tes5_name, 0), val)
    for i, skill_name in enumerate(TES5_SKILL_ORDER):
        dnam[i] = min(skill_vals.get(skill_name, 15), 255)
    # DNAM offsets 36/38/40 are the engine's calculated Health/Magicka/Stamina
    # CACHE, not authored stats — the real controls are ACBS.HealthOffset and
    # ACBS.Magicka/StaminaOffset. Write the TES4 totals so the cache agrees with
    # what the engine will compute from those offsets (it recomputes on load
    # regardless). Magicka is Oblivion's SpellPoints; stamina is Fatigue.
    health = get_int(rec, 'DATA.Health', 50)
    struct.pack_into('<H', dnam, 36, max(0, min(health, 65535)))
    magicka = get_int(rec, 'ACBS.SpellPoints', 0)
    struct.pack_into('<H', dnam, 38, max(0, min(magicka, 65535)))
    stamina = get_int(rec, 'ACBS.Fatigue', 100)
    struct.pack_into('<H', dnam, 40, max(0, min(stamina, 65535)))
    return bytes(dnam)


# Every playable/Dremora RACE that RACE_MAP targets ships Starting Health 50.0
# (verified: all 11 target races in Skyrim.esm decode to 50.0/50.0/50.0), and the
# engine derives an actor's max health as
#     StartingHealth + HealthOffset + (Level - 1) * fNPCHealthLevelBonus
# with fNPCHealthLevelBonus = 5.0 (Skyrim.esm GMST). ACBS.HealthOffset (int16 at
# byte 20) is the AUTHORED control; DNAM.Health is only a cache the engine
# recomputes — vanilla proves it is not a function of the record at all (52 groups
# of NPCs with identical race/class/level/offset carry different DNAM.Health, e.g.
# 55 / 51 / 0 / 20971), matching UESP's "otherwise seems to be random".
#
# TES4 DATA.Health is the actor's FINAL hit-point pool, already fully calculated.
# So a faithful conversion pins the engine's result to that exact number by
# solving for the offset rather than copying the pool into the cache field.
TES5_RACE_BASE_HEALTH = 50
# Single definition lives in creature_races (see the note there on import order).
from ..creature_races import TES5_HEALTH_LEVEL_BONUS  # noqa: E402


def _health_and_level(tes4_health: int, tes4_level: int, is_pc_level_mult: bool
                      ) -> tuple:
    """TES4 final health pool → (ACBS.HealthOffset, ACBS.Level) for an NPC.

    Chosen so the engine's own calculation,
        50 (race base) + HealthOffset + (Level-1) * fNPCHealthLevelBonus
    reproduces the TES4 total exactly.

    HealthOffset is int16, but a handful of TES4 actors (dev test dummies, story
    bosses made effectively unkillable, the Player record) carry pools far past
    32767. Rather than clamp — which would silently make an intended-invulnerable
    actor killable — the surplus is spent through the Level term, whose per-level
    bonus is the engine's own mechanism for exactly this. Level stays within the
    U16 field, so the result is still a faithful total.

    For PC-Level-Mult actors the level term tracks the player and is unknown at
    author time, so only the fixed race base is removed; the actor then scales
    with the player the way its TES4 PCLevelOffset counterpart did.
    """
    if is_pc_level_mult:
        offset = tes4_health - TES5_RACE_BASE_HEALTH
        return max(-32768, min(offset, 32767)), 1000

    level = max(1, min(tes4_level, 65535))
    offset = tes4_health - TES5_RACE_BASE_HEALTH - (level - 1) * TES5_HEALTH_LEVEL_BONUS
    if offset > 32767:
        # Raise Level so its bonus absorbs the surplus, then re-solve the
        # remainder. Capped at the U16 field limit.
        need = tes4_health - TES5_RACE_BASE_HEALTH - 32767
        # Round the division UP: too few levels leaves the remainder above the
        # int16 cap and the final clamp would silently lose it.
        level = max(level, min(65535, -(-need // TES5_HEALTH_LEVEL_BONUS) + 1))
        offset = tes4_health - TES5_RACE_BASE_HEALTH - (level - 1) * TES5_HEALTH_LEVEL_BONUS
    return max(-32768, min(offset, 32767)), level


def _npc_acbs(rec: dict) -> bytes:
    """Build TES5 NPC_ ACBS payload (24 bytes) from a TES4 NPC_ record.

    Shared by convert_NPC_ and the override path (override_builder), so an
    authored ACBS/attribute change patches the exact bytes conversion writes.
    """
    tes4_flags = get_int(rec, 'ACBS.Flags')
    level = get_int(rec, 'ACBS.Level', 1)
    calc_min = get_int(rec, 'ACBS.CalcMin', 1)
    calc_max = get_int(rec, 'ACBS.CalcMax', 100)
    # Keep compatible bits + preserve Female flag (bit 0)
    tes5_acbs_flags = tes4_flags & 0x4C9B
    is_pc_level = bool(tes4_flags & 0x80)
    # TES4 PCLevelOffset: level is an additive offset from the player's level.
    # TES5 PCLevelMult: level is a fixed-point multiplier (1000 = 1.0×).
    # We can't map an offset directly to a multiplier so default to 1.0×.
    # Faithful health: solve ACBS.HealthOffset so the engine reproduces the exact
    # TES4 pool. See _health_and_level. Previously the offset slot wrote a
    # hardcoded 0 and the raw pool went into the DNAM cache, so the authored
    # control was empty and the level was copied across unmapped.
    health_offset, tes5_level = _health_and_level(
        get_int(rec, 'DATA.Health', 50), level, is_pc_level)
    # Magicka/Stamina offsets are likewise deltas from the 50.0 race base. These
    # two slots previously received raw Intelligence and Strength ATTRIBUTES,
    # which are not pools at all; Oblivion's actual pools are SpellPoints (magicka)
    # and Fatigue (stamina).
    magicka_offset = max(-32768, min(
        get_int(rec, 'ACBS.SpellPoints', 0) - TES5_RACE_BASE_HEALTH, 32767))
    stamina_offset = max(-32768, min(
        get_int(rec, 'ACBS.Fatigue', 100) - TES5_RACE_BASE_HEALTH, 32767))
    # ACBS: Flags(I) MagickaOff(h) StaminaOff(h) Level(H) CalcMin(H) CalcMax(H)
    #       SpeedMult(H) Disposition(h) TemplateFlags(H) HealthOffset(h)
    #       BleedoutOverride(H)   — layout per xEdit wbDefinitionsTES5.
    # CalcMin/CalcMax are a plain level band in BOTH games; the old `* 2` here
    # doubled every NPC's level range and disagreed with the creature path.
    return struct.pack('<IhhHHHHhHhH',
                       tes5_acbs_flags, magicka_offset, stamina_offset, tes5_level,
                       min(calc_min, 65535), min(calc_max, 65535),
                       100, 0, 0, health_offset, 0)


# TES4 CREA ACBS flag bits. These are NOT the TES4 NPC_ bits and NOT the TES5
# bits — the same bit number means something different in all three (xEdit
# wbDefinitionsTES4 CREA ACBS vs wbDefinitionsTES5 NPC_ ACBS). A raw
# `flags & mask` therefore silently reinterprets creature data as unrelated NPC
# behaviour, which is exactly what the old `& 0x4C9B | 0x10` did:
#     bit 0  Biped         -> TES5 0x01 Female          (every biped went female)
#     bit 4  Swims         -> TES5 0x10 Auto-calc stats (swimmers got autocalc)
#     bit 11 No Blood Spray-> TES5 0x800 Protected      (unkillable by NPCs)
# and it dropped bit 9 "No Low Level Processing" entirely.
_T4C_BIPED           = 0x000001
_T4C_ESSENTIAL       = 0x000002
_T4C_RESPAWN         = 0x000008
_T4C_SWIMS           = 0x000010
_T4C_FLIES           = 0x000020
_T4C_WALKS           = 0x000040
_T4C_PC_LEVEL_OFFSET = 0x000080
_T4C_NO_LOW_LEVEL    = 0x000200
_T4C_NO_BLOOD_SPRAY  = 0x000800
_T4C_NO_BLOOD_DECAL  = 0x001000

# TES5 NPC_ ACBS flags (xEdit + UESP Skyrim Mod:Mod File Format/NPC).
_T5_FEMALE        = 0x00000001
_T5_ESSENTIAL     = 0x00000002
_T5_RESPAWN       = 0x00000008
_T5_AUTOCALC      = 0x00000010
_T5_PC_LEVEL_MULT = 0x00000080
_T5_PROTECTED     = 0x00000800
_T5_SUMMONABLE    = 0x00004000
_T5_DOESNT_BLEED  = 0x00010000
_T5_SIMPLE_ACTOR  = 0x00100000


def _crea_flags(tes4_flags: int) -> int:
    """TES4 CREA ACBS flags → TES5 NPC_ ACBS flags, translated by MEANING.

    Only bits whose meaning genuinely carries over are set; TES4 bits with no
    TES5 counterpart (Biped/Swims/Flies/Walks — movement is a behaviour-graph
    and RACE concern in Skyrim) are dropped rather than smuggled into whatever
    TES5 happens to use that bit number for.
    """
    out = 0
    if tes4_flags & _T4C_ESSENTIAL:
        out |= _T5_ESSENTIAL
    if tes4_flags & _T4C_RESPAWN:
        out |= _T5_RESPAWN
    if tes4_flags & _T4C_PC_LEVEL_OFFSET:
        out |= _T5_PC_LEVEL_MULT
    # "No Low Level Processing" is TES4 bit 9 (0x200). Skyrim's equivalent for
    # an actor that must keep running full AI/stat processing when unloaded is
    # Simple Actor being OFF plus the actor not being culled; the closest real
    # counterpart is 0x100000 Simple Actor, which is the INVERSE (a simple actor
    # gets reduced processing). So the flag is honoured by explicitly NOT
    # marking such creatures Simple Actor, and it is never mapped onto an
    # unrelated bit. Dropping it silently was the previous behaviour.
    if tes4_flags & _T4C_NO_BLOOD_SPRAY and tes4_flags & _T4C_NO_BLOOD_DECAL:
        out |= _T5_DOESNT_BLEED
    # Creatures have no TES4 attribute block to derive stats from, so Skyrim must
    # autocalc from race + level + the offsets _crea_acbs writes.
    out |= _T5_AUTOCALC
    return out


def _crea_nam6(rec: dict) -> bytes:
    """TES5 NPC_ NAM6 'Height' from the TES4 CREA BNAM 'Base Scale'.

    Shared by convert_CREA and the override path.

    TES4 sizes a creature with a PER-RECORD BNAM base scale; only humanoids
    take their height from the RACE (Male/Female Height in RACE DATA). The
    creature converter used the NPC_ default of 1.0 here, so every creature
    shipped at 1.0x no matter what it was authored at: Anequina's bull
    elephant (BNAM 3.5) rendered a third of its size and Nehrim's chickens
    (0.4) two and a half times theirs. 754 of the 1903 CREA records across
    Oblivion/Nehrim/Anequina carry a non-1.0 BNAM.

    NAM6 is the direct equivalent — both engines multiply the base height by
    the placed ref's XSCL, so the per-ref scale (already preserved by
    convert_ACHR) keeps layering exactly as it did in TES4. Vanilla uses the
    field the same way, up to 3.3 on a giant and down to 0.6.

    The scale must live HERE and not on the generated creature RACE: one race
    is shared by every CREA with the same mesh folder, so a race-level height
    would collapse the elephant bull/cow/calf to a single size.
    """
    scale = get_float(rec, 'BNAM.BaseScale', 1.0)
    # A missing BNAM, or a nonsense authored value, means "no scaling". Every
    # CREA in the four test plugins carries one and none is <= 0, but the
    # converter never assumes a plugin is well-formed.
    if scale <= 0.0:
        scale = 1.0
    return struct.pack('<f', scale)


def _crea_acbs(rec: dict) -> bytes:
    """Build TES5 ACBS payload (24 bytes) from a TES4 CREA record.

    Shared by convert_CREA and the override path. Creatures auto-calc their
    stats (flag 0x10), so attributes stay zero.
    """
    tes4_flags = get_int(rec, 'ACBS.Flags')
    level = get_int(rec, 'ACBS.Level', 1)
    calc_min = get_int(rec, 'ACBS.CalcMin', 1)
    calc_max = get_int(rec, 'ACBS.CalcMax', 100)
    tes5_flags = _crea_flags(tes4_flags)
    # TES4 flag 0x80 = "PC Level Offset" (Level is an additive offset from the
    # player's level). TES5 reuses the same bit as "PC Level Mult", where Level
    # is a fixed-point multiplier (1000 = 1.0x). A raw TES4 offset (e.g. 0..5)
    # reinterpreted as a multiplier is 0.000x..0.005x, which the CK clamps to
    # the 0.10 minimum. Since an offset can't be mapped to a multiplier, default
    # to 1.0x when the flag is set.  See _npc_acbs for the same handling.
    is_pc_level = bool(tes4_flags & 0x80)
    from ..creature_races import creature_capped_level
    tes5_level = creature_capped_level(rec)
    # A creature's generated RACE is SHARED across every CREA with the same mesh
    # folder, so it carries only a flat base and this per-record offset carries
    # the creature's whole TES4 pool. See creature_health_offset.
    from ..creature_races import creature_health_offset
    health_offset = creature_health_offset(rec)
    # Magicka: the generated race's starting magicka is 0 (shared race), so
    # the actor's whole TES4 SpellPoints pool rides here. Without it a
    # spell-knowing creature has 0 magicka, cannot pay any cast cost, and
    # never casts — vanilla's atronach carries the same split (race base +
    # ACBS.MagickaOffset 50).
    magicka_offset = min(get_int(rec, 'ACBS.SpellPoints', 0), 32767)
    return struct.pack('<IhhHHHHhHhH',
                       tes5_flags, magicka_offset, 0, tes5_level,
                       min(calc_min, 65535), min(calc_max, 65535),
                       100, 0, 0, health_offset, 0)


# fid_low24 → that faction's Relation disposition toward PlayerFaction.
# Populated by load_faction_player_reactions() in Phase 0, before any actor
# converter runs.  Empty is a safe default: _player_disposition() then falls
# back to Personality alone, which is the TES4 base disposition.
_FACTION_PLAYER_DISP = {}

# TES4 PlayerFaction. Same FormID in Oblivion.esm and Nehrim.esm (a Nehrim
# record, being a plugin over the same master layout, keeps the id).
_PLAYER_FACTION_FID = 0x0001DBCD


# Lowercased EditorIDs of every faction this plugin's scripts treat as a crime
# faction — i.e. that appears as the argument of a Get/SetPCFaction{Murder,
# Attack,Steal} call.  Populated by _load_crime_factions() in Phase 0.
TES4_CRIME_FACTIONS: set = set()

# Oblivion's crime functions read per-faction flags the engine maintains for any
# faction the player can offend; there is no TES4 "this is a crime faction" flag
# to carry across.  Skyrim instead requires Track Crime (DATA bit 6) plus
# nonzero CRVA amounts before it will accumulate crime gold at all, so the set
# has to be derived.  Scanning the scripts is the generic way to do it: whatever
# faction a plugin actually tests is by definition one whose crimes it tracks.
_CRIME_FN_RE = re.compile(
    r'\b(?:get|set)pcfaction(?:murder|attack|steal)\s+([A-Za-z0-9_]+)', re.I)


def _load_crime_factions(by_type: dict) -> None:
    """Collect faction EditorIDs used by the plugin's crime-flag script calls."""
    TES4_CRIME_FACTIONS.clear()
    for sig in ('SCPT', 'INFO', 'QUST'):
        for rec in by_type.get(sig, []):
            for key, val in rec.items():
                if not isinstance(val, str) or 'pcfaction' not in val.lower():
                    continue
                # SCTX arrives with \r\n still escaped; the regex only needs the
                # function name and its first argument, so no unescaping needed.
                for m in _CRIME_FN_RE.finditer(val):
                    TES4_CRIME_FACTIONS.add(m.group(1).lower())


def load_faction_player_reactions(by_type: dict) -> None:
    """Index each FACT's disposition modifier toward the player faction.

    TES4 starting disposition is NOT just Personality — UESP Oblivion:Disposition
    lists the terms as "base disposition is equal to the NPC's Personality
    score", then "faction reactions further modify disposition", and notes that
    "enemies are programmed to have negative dispositions towards you".  The
    faction term is the one that actually separates a wolf from a horse, so it
    has to be read from the data rather than guessed.

    Also indexes which factions the plugin's scripts treat as crime factions
    (see _load_crime_factions).
    """
    _FACTION_PLAYER_DISP.clear()
    _PREY_FACTIONS.clear()
    _load_crime_factions(by_type)
    for rec in by_type.get('FACT', []):
        fid = get_formid(rec, 'FormID') & 0xFFFFFF
        # Match on EditorID, not FormID: a plugin that defines its own prey
        # faction gets the same treatment as vanilla's 0005D556.
        edid = (get_str(rec, 'EditorID') or '').lower()
        if 'prey' in edid:
            _PREY_FACTIONS.add(fid)
        n = get_int(rec, 'RelationCount')
        for i in range(n):
            other = get_formid(rec, f'Relation[{i}].Faction') & 0xFFFFFF
            if other == (_PLAYER_FACTION_FID & 0xFFFFFF):
                _FACTION_PLAYER_DISP[fid] = get_int(
                    rec, f'Relation[{i}].Disposition')
                break


# fid_low24 of every faction whose EditorID marks its members as prey.
# Populated alongside _FACTION_PLAYER_DISP.
_PREY_FACTIONS = set()

# Player's starting Personality.  UESP Oblivion:Disposition: base disposition is
# the actor's Personality, then "for every 4 points of Personality that the
# player has above the NPC's, disposition increases by 1 point".  40 is the
# mid-range starting value across races/classes.
_PLAYER_PERSONALITY = 40

# How decisively an actor must want to attack before it earns tier 2.
# margin = (aggression - 5) - disposition.  Measured over Oblivion's creatures:
# known predators cluster at a median margin of 48, while tame animals sit at
# -47.  Nehrim's Benno — a marauder-template pet dog — scores just 8, which is
# what separates "hostile in principle" from "attacks you on sight".
_ONSIGHT_MARGIN = 10


def _is_prey(rec: dict) -> bool:
    """True when this actor belongs to a prey faction.

    Prey is the vanilla marker for "harmless": Oblivion's 43 Prey members are
    all horses, deer and sheep, and several carry aggression 100 — the same
    value as the nastiest predators — so aggression alone cannot exclude them.
    UESP Oblivion:Animals confirms deer "are not aggressive" despite that.
    """
    for i in range(get_int(rec, 'FactionCount')):
        if (get_formid(rec, f'Faction[{i}].FormID') & 0xFFFFFF) in _PREY_FACTIONS:
            return True
    return False


def _player_disposition(rec: dict, pers: int) -> int:
    """Estimate this actor's TES4 starting disposition toward the player.

    Personality is the base; every faction the actor belongs to contributes its
    own reaction toward PlayerFaction.  TES4 sums the faction modifiers, so a
    creature in two hostile factions is more hostile than one in a single
    faction — matching the in-game behaviour the formula describes.
    """
    disp = pers
    for i in range(get_int(rec, 'FactionCount')):
        fid = get_formid(rec, f'Faction[{i}].FormID') & 0xFFFFFF
        disp += _FACTION_PLAYER_DISP.get(fid, 0)
    # Player-Personality term: +1 disposition per 4 points the player's
    # Personality exceeds the actor's (UESP Oblivion:Disposition).
    disp += (_PLAYER_PERSONALITY - pers) // 4
    return disp


def _npc_aidt(rec: dict, is_creature: bool = False) -> bytes:
    """Build TES5 AIDT subrecord (20 bytes).

    TES5 layout:
      00: Aggression U8  (0=Unaggressive,1=Aggressive,2=VeryAggressive,3=Frenzied)
      01: Confidence U8  (0=Cowardly,1=Cautious,2=Average,3=Brave,4=Foolhardy)
      02: Energy U8
      03: Morality U8    (0=AnyCrime,1=ViolenceAgainstEnemies,2=PropertyCrimeOnly,3=NoCrime)
      04: Mood U8        (0=Neutral)
      05: Assistance U8  (0=HelpsNobody,1=HelpsAllies,2=HelpsFriendsAndAllies)
      06: AggroRadiusBehavior U8
      07: Unused U8
      08: Warn U32
      0C: Warn/Attack U32
      10: Attack U32
    """
    aggr = get_int(rec, 'AIDT.Aggression')
    conf = get_int(rec, 'AIDT.Confidence')
    energy = get_int(rec, 'AIDT.EnergyLevel', 50)
    resp = get_int(rec, 'AIDT.Responsibility')
    pers = get_int(rec, 'DATA.Personality', 50)
    # TES4 aggression → TES5 tier. Both games gate combat on the SAME two axes,
    # only renamed, so the mapping models the TES4 rule directly rather than
    # bucketing aggression alone.
    #
    #   TES4 (UESP Oblivion:Aggression / Oblivion:Disposition): an actor attacks
    #   a target when disposition(actor→target) < aggression - 5. Starting
    #   disposition toward the player ≈ the actor's Personality, shifted by
    #   race/faction reactions; "enemies are programmed to have NEGATIVE
    #   dispositions towards you." aggression <= 5 never attacks; >= 106 attacks
    #   anyone regardless of disposition.
    #
    #   TES5 replaces the 0-100 disposition scalar with a discrete combat
    #   reaction (Enemy/Neutral/Friend/Ally) and makes aggression a TIER that
    #   says which reactions it will attack: 0 attacks nobody unprovoked, 1
    #   "Aggressive" attacks only Enemies (a neutral player is not attacked — the
    #   actor merely RETALIATES when hit), 2 "VeryAggressive" attacks Neutrals on
    #   sight too, 3 "Frenzied" attacks everyone incl. allies.
    #
    # THE KEY POINT: aggression is not "how hostile is this actor", it is "WHICH
    # REACTION TIER does it attack".  Who it is hostile TO lives in the faction
    # graph, in BOTH games.  UESP Skyrim:NPCs#Aggression states it directly:
    # "Together with the FACTION RELATIONSHIP COMBAT MODIFIER this governs
    # whether the NPC initiates combat", and defines
    #   0 Unaggressive  attacks nobody unless provoked
    #   1 Aggressive    attacks ENEMIES on sight
    #   2 VeryAggressive attacks enemies AND NEUTRAL on sight
    #   3 Frenzied      attacks anybody on sight
    # The player is a NEUTRAL to anyone with no relation to PlayerFaction, so
    # tier 2 is the line between "hunts its faction enemies" and "hunts you".
    #
    # TES4 expresses the same thing per-target: attack when
    # disposition(actor→target) < aggression - 5, where disposition is
    # Personality plus the FACTION reactions toward that specific target
    # (UESP Oblivion:Disposition).  Worked through for Nehrim's Benno, a dog in
    # MarauderFaction + BanditFaction with aggr=30, Personality=10:
    #
    #   Benno → a bandit : 10 + (-100) = -90 < 25   → attacks   (Enemy)
    #   Benno → player   : 10 +     0  =  10        → no faction relation at all
    #
    # MarauderFaction and BanditFaction relate ONLY to each other (-100) and to
    # CreatureFaction (+20); NEITHER has any relation to PlayerFaction.  Benno
    # is aggressive toward MARAUDERS, not toward you — and Oblivion's own
    # CreatureDog carries byte-identical data, which is why UESP lists Dog under
    # "Aggressive Animals" while the dog still never mauls the player on sight.
    #
    # That is the whole bug: collapsing a per-target rule onto one global tier
    # and then resolving it against the player.  Any actor whose hostility is
    # expressed purely as faction relations must land on tier 1 — the faction
    # graph (converted faithfully into XNAM Group Combat Reaction) then does the
    # targeting exactly as it does in vanilla Skyrim, whose own horses, deer,
    # elk, cows, goats, foxes and sabre cats are ALL Aggression 0 for the same
    # reason.
    #
    # NOTE: gating tier 2 on "does a faction make the player an enemy" was tried
    # and is WRONG.  Oblivion's wolves/bears/trolls/mountain lions sit in
    # CreatureFaction, which has no PlayerFaction relation either, so that rule
    # dropped every predator to tier 1 and made the wilderness passive.  Skyrim
    # separates these two cases with AIDT's Aggro Radius fields (EncWolf is
    # Aggression 0 but carries aggroRadiusBehavior=1, attack radius 1500) —
    # fields TES4's AIDT does not have at all (xEdit wbDefinitionsTES4: AIDT is
    # Aggression/Confidence/Energy/Responsibility/Services/Teaches/MaxTraining
    # only).  The discriminator therefore has to be reconstructed; see
    # _predator_attack_radius below.
    # The rule below is calibrated, not guessed.  UESP Oblivion:Animals draws
    # the exact distinction we need for the dogs: randomly generated dogs "are
    # Bandit or marauder dogs ... that are hostile towards you, ALTHOUGH THEY
    # WILL NOT NECESSARILY ATTACK ON SIGHT", while "the other dogs in the game
    # are all pets of townspeople and are friendly".  "Hostile but not on sight"
    # is precisely TES5 tier 1; "on sight" is tier 2.
    #
    # Two terms decide it:
    #   * Prey membership — vanilla's marker for harmless.  Its 43 members are
    #     horses, deer and sheep, several at aggression 100, so no aggression
    #     threshold can exclude them (this is what broke earlier attempts).
    #   * The attack MARGIN, (aggr-5) - disposition: how decisively the TES4
    #     rule fires.  Measured across Oblivion's creatures, known predators
    #     have a median margin of 48 and tame animals -47, while Benno scores
    #     just 8 — hostile in principle, not a threat on sight.
    disp = _player_disposition(rec, pers)
    if aggr <= 5:
        tes5_aggr = 0   # never initiates (0 will not even defend itself)
    elif aggr >= 106:
        tes5_aggr = 3   # Frenzied — attacks everyone, even allies
    elif not _is_prey(rec) and (aggr - 5) - disp >= _ONSIGHT_MARGIN:
        tes5_aggr = 2   # decisively hostile: attacks the player on sight
    else:
        tes5_aggr = 1   # attacks its faction enemies; not the player on sight
    # TES4 confidence (0-100 scalar) → TES5 confidence TIER.
    #
    # Both engines feed confidence into fAIFleeConfBase/fAIFleeConfMult to score
    # "should I run away", but TES4 supplies a 0-100 number while TES5 supplies
    # one of five tiers (xEdit wbConfidenceEnum: 0 Cowardly, 1 Cautious,
    # 2 Average, 3 Brave, 4 Foolhardy).  Only tier 4 never flees.
    #
    # The old mapping was `<30 → 0, >=70 → 3, else 2`, which never emitted tier
    # 1 or tier 4 at all and capped the whole top of the range at Brave.  That
    # is why converted actors fled constantly: Oblivion's "fearless" value 100
    # is by far the most common setting (1,567 of 3,396 exported actors) and it
    # was landing on Brave, which still has a nonzero flee score, instead of
    # Foolhardy.  Vanilla Skyrim leans the opposite way — of 5,118 NPC_ records
    # the distribution is 292/90/1730/393/2613, i.e. Foolhardy is the single
    # most common tier and more than half of all NPCs sit at 3 or 4.
    #
    # Anchoring on the values Oblivion actually uses (100 = fearless, 75-95 =
    # brave, 50 = the engine default "average", 5-25 = timid, 0 = flees on
    # sight) reproduces a vanilla-shaped spread.  Must stay in sync with
    # _scale_enum_av in script_convert/converter.py, which buckets scripted
    # `setav confidence N` onto the same tiers.
    if conf >= 100:
        tes5_conf = 4   # Foolhardy — never flees
    elif conf >= 70:
        tes5_conf = 3   # Brave
    elif conf >= 40:
        tes5_conf = 2   # Average
    elif conf >= 15:
        tes5_conf = 1   # Cautious
    else:
        tes5_conf = 0   # Cowardly
    # TES4 responsibility → TES5 morality (inverted: high resp = no crime)
    tes5_moral = 3 if resp >= 80 else (2 if resp >= 50 else (1 if resp >= 30 else 0))
    # Assistance: low responsibility → helps nobody
    tes5_assist = 1 if resp >= 30 else 0

    return struct.pack('<BBBBBB BB III',
                       tes5_aggr, tes5_conf, energy,
                       tes5_moral, 0, tes5_assist,  # mood=0 (Neutral)
                       0, 0,                          # aggro radius, unused
                       0, 0, 0)                       # warn, warn/attack, attack


# ---------------------------------------------------------------------------
#   Vendor Faction System
# ---------------------------------------------------------------------------

# Skyrim.esm Gold001 (index 0 in the output load order)
GOLD001_FID = 0x0000000F

# TES4 AIDT.Services bitmask → Skyrim VendorItem KYWD FormIDs.
# Training (bit 14), Recharge (bit 16), Repair (bit 17) have no vendor keyword
# equivalent — Training is handled by CLAS, the others are TES4-only.
# MUST stay in sync with the keywords the item converters emit (VENDOR_KYWD in
# record_types/common.py): a vendor only trades items whose keywords appear in
# its faction's VEND formlist.
_TES4_SERVICE_BIT_TO_SKYRIM_KEYWORDS = {
    0:  [0x0008F958, 0x000917E7],             # Weapons → VendorItemWeapon + Arrow
    1:  [0x0008F959],                         # Armor   → VendorItemArmor
    2:  [0x0008F95B, 0x0008F95A],             # Clothing → VendorItemClothing + Jewelry
    3:  [0x000937A2, 0x000A0E57],             # Books → VendorItemBook + Scroll
    4:  [0x0008CDEB, 0x000A0E56,
         0x0008CDEA],                         # Ingredients → Ingredient + FoodRaw + Food (TES4 food = ingredients)
    7:  [0x000914E9],                         # Lights → VendorItemClutter
    8:  [0x000914E9],                         # Apparatus → VendorItemClutter (no TES5 apparatus)
    10: [0x000914ED, 0x000914EA, 0x000914EC,
         0x000914EE, 0x000914E9],             # Misc → Gem + AnimalHide + OreIngot + Tool + Clutter
    11: [0x000937A5, 0x000A0E57,
         0x000937A4],                         # Spells → SpellTome + Scroll + Staff
    12: [0x000937A3, 0x000937A4],             # MagicItems → SoulGem + Staff
    13: [0x0008CDEC, 0x0008CDED,
         0x0008CDEA],                         # Potions → Potion + Poison + Food
}

# Module-level cache: service_bitmask → shared vendor FACT FormID (Phase 0c).
# Used for merchants that have no dedicated merchant chest — they trade from
# their carried inventory only.
_vendor_faction_cache: dict[int, int] = {}

# The one faction EVERY merchant joins, purely so the Barter topic can be gated
# with a SINGLE GetInFaction condition.
#
# Why this exists: the barter gate used to OR over every per-service vendor
# faction (25 of them), which pushed each Barter INFO to 25-30 CTDAs. Vanilla
# Skyrim never exceeds 22 conditions on an INFO (max OR-run 20), and past that
# the engine silently drops the line — every Barter INFO failed, so merchants
# lost the topic entirely while Training (a 1-condition gate) kept working.
# Membership here is what the dialogue asks about; the per-service factions
# still do the actual vending (VEND keyword filter / VENC chest).
_merchant_marker_faction_fid = 0

# Per-merchant vendor factions: (remapped) actor FormID → FACT FormID. These
# carry a VENC (Merchant Container) pointing at the actor's own converted
# Oblivion merchant chest, so the barter menu stocks the chest's full
# merchandise instead of just the NPC's carried items.
_merchant_faction_by_npc: dict[int, int] = {}

# "Belongs to this converted plugin" marker faction. Every actor this file
# defines joins it, and dialogue that states no plugin-scoped audience of its
# own is gated on it (see dialog_conditions.needs_origin_gate).
#
# Without it, two converted plugins loaded together cross-talk: Oblivion's
# guard/crime/directions/rumour lines are scoped only by GetIsRace (or by a
# NEGATIVE GetIsID), and conversion rewrites GetIsRace to a VANILLA Skyrim race
# every plugin shares, so Nehrim NPCs passed them. Race was Oblivion's plugin
# boundary only because Oblivion was the only file loaded.
#
# ONLY a root master (no TES4 masters of its own) creates and applies this.
# A DLC/plugin must stay ungated so it can extend and override its master's
# dialogue exactly as it does in Oblivion — its own actors are already members
# via the master's NPC records it inherits or overrides.
_origin_faction_fid = 0


def get_origin_faction_fid() -> int:
    """The plugin-origin marker FACT, or 0 when this file isn't gated."""
    return _origin_faction_fid


def create_origin_faction(writer) -> int:
    """Create the plugin-origin marker faction. Root masters only.

    A plain membership marker: no flags, no relations, no vendor data, so it
    can never affect crime, combat reaction, or the barter menu.
    """
    global _origin_faction_fid
    _origin_faction_fid = writer.derive_formid('FACT', 'TES4PluginOriginFaction')
    subs = pack_string_subrecord('EDID', 'TES4PluginOriginFaction')
    subs += pack_subrecord('DATA', struct.pack('<I', 0))
    writer.add_record('FACT', pack_record('FACT', _origin_faction_fid, 0, subs))
    return _origin_faction_fid


def reset_origin_faction() -> None:
    """Clear origin-faction state (per-run isolation for tests/batch runs)."""
    global _origin_faction_fid
    _origin_faction_fid = 0


def _keywords_for_services(services: int) -> list[int]:
    """Return unique sorted Skyrim KYWD FormIDs for a TES4 services bitmask."""
    kw_set = set()
    for bit, kwds in _TES4_SERVICE_BIT_TO_SKYRIM_KEYWORDS.items():
        if services & (1 << bit):
            kw_set.update(kwds)
    return sorted(kw_set)


def _vendor_bits(services: int) -> int:
    """Services bitmask with the non-vendor (training/recharge/repair) bits cleared."""
    return services & ~((1 << 14) | (1 << 16) | (1 << 17))


def _build_merchant_chest_map(by_type: dict) -> dict[int, int]:
    """(remapped) base-actor FormID → (remapped) merchant-chest REFR FormID.

    In Oblivion a merchant's sale stock lives in a CONT placed in the world and
    linked to the NPC through the placed reference's XMRC (Merchant Container),
    not in the NPC's carried inventory. Skyrim expresses the same thing with a
    VENC on the vendor faction, so we resolve ACHR.NAME (base actor) → the chest
    REFR here and hand it to the faction builder.
    """
    chest_by_npc: dict[int, int] = {}
    for rec in by_type.get('ACHR', []):
        chest = get_formid(rec, 'XMRC.MerchantContainer')
        if not chest:
            continue
        base = get_formid(rec, 'NAME')
        if base:
            chest_by_npc.setdefault(base, chest)
    return chest_by_npc


def _vendor_flst_subs(svc_mask: int) -> bytes:
    """FLST subrecords for a service bitmask's VendorItem keyword filter."""
    kwds = _keywords_for_services(svc_mask)
    # Always include VendorNoSale (0x000FF9FB) — prevents selling quest items.
    kwds = kwds + [0x000FF9FB]
    subs = pack_string_subrecord('EDID', f'TES4VendorList_{svc_mask:06X}')
    for kw_fid in kwds:
        subs += pack_formid_subrecord('LNAM', kw_fid)
    return subs


def _write_vendor_faction(writer, edid: str, flst_fid: int, venc_fid: int = 0) -> int:
    """Create a vendor FACT (VEND → flst, optional VENC → chest) and return its FormID."""
    # `edid` is unique per faction (TES4VendorFaction_<mask> or
    # TES4Merchant_<actor fid>), both derived from authored data.
    fact_fid = writer.derive_formid('VENDOR_FACT', edid)
    subs = pack_string_subrecord('EDID', edid)
    subs += pack_string_subrecord('FULL', 'Merchant')
    # DATA: Vendor (0x4000) only — matches vanilla service factions
    # (e.g. ServicesWhiterunEorlund). CanBeOwner is not set on vendor factions.
    subs += pack_subrecord('DATA', struct.pack('<I', 0x4000))
    # CRVA — Crime values (20 bytes of mostly zeros, like vanilla)
    subs += pack_subrecord('CRVA', b'\x01\x01' + b'\x00' * 18)
    # VEND — Vendor buy/sell list → FLST
    subs += pack_formid_subrecord('VEND', flst_fid)
    # VENC — Merchant Container → the actor's Oblivion merchant chest REFR. When
    # present the barter menu stocks this container; order is VEND, VENC, VENV.
    if venc_fid:
        subs += pack_formid_subrecord('VENC', venc_fid)
    # VENV — Vendor values (matches vanilla ServicesWhiterunEorlund): available
    # 0..23h, radius 700, no stolen-only, sell+buy. StartHour(U16) + EndHour(U16)
    # + Radius(U16) + Unused(2B) + OnlyBuyStolen(U8) + NotSellBuy(U8) + Unused(2B).
    subs += pack_subrecord('VENV', struct.pack('<HHH BB BB BB',
                                               0, 23, 700, 0, 0, 0, 0, 0, 0))
    writer.add_record('FACT', pack_record('FACT', fact_fid, 0, subs))
    return fact_fid


def create_vendor_factions(by_type: dict, writer) -> None:
    """Phase 0c: Pre-scan NPC_/CREA for services and create vendor FACTs + FLSTs.

    Two kinds of vendor faction are produced:

    * A shared per-service-bitmask faction (VEND only) for merchants that have
      no merchant chest — they trade from their carried CNTO inventory.
    * A dedicated per-merchant faction (VEND + VENC) for each merchant whose
      placed reference links a merchant chest, so the barter menu stocks that
      chest's full stock.

    Both share one FLST per service bitmask. The NPC/CREA converters call
    get_vendor_faction_fid(actor_fid, services) to pick the right one.
    """
    _vendor_faction_cache.clear()
    _merchant_faction_by_npc.clear()

    chest_by_npc = _build_merchant_chest_map(by_type)

    # Collect vendor actors: (remapped actor fid, vendor_bits). Training-only
    # actors (bit 14 with no vendor bits) are handled by CLAS, not here.
    vendor_actors: list[tuple[int, int]] = []
    unique_services: set[int] = set()
    for sig in ('NPC_', 'CREA'):
        for rec in by_type.get(sig, []):
            bits = _vendor_bits(get_int(rec, 'AIDT.Services'))
            if not bits:
                continue
            unique_services.add(bits)
            vendor_actors.append((get_formid(rec, 'FormID'), bits))

    if not unique_services:
        return

    # One shared FLST per service bitmask, reused by both faction kinds.
    flst_by_svc: dict[int, int] = {}
    for svc_mask in sorted(unique_services):
        if not _keywords_for_services(svc_mask):
            continue
        flst_fid = writer.derive_formid('VENDOR_FLST', svc_mask)
        writer.add_record('FLST', pack_record('FLST', flst_fid, 0,
                                              _vendor_flst_subs(svc_mask)))
        flst_by_svc[svc_mask] = flst_fid

    # Shared (chest-less) faction per service bitmask.
    for svc_mask, flst_fid in flst_by_svc.items():
        _vendor_faction_cache[svc_mask] = _write_vendor_faction(
            writer, f'TES4VendorFaction_{svc_mask:06X}', flst_fid)

    # Dedicated faction per merchant that owns a chest.
    n_chest = 0
    for actor_fid, bits in vendor_actors:
        chest = chest_by_npc.get(actor_fid)
        flst_fid = flst_by_svc.get(bits)
        if not chest or not flst_fid:
            continue
        _merchant_faction_by_npc[actor_fid] = _write_vendor_faction(
            writer, f'TES4Merchant_{actor_fid & 0xFFFFFF:06X}', flst_fid, chest)
        n_chest += 1

    # The single "is a merchant" faction the Barter topic gates on. Not a vendor
    # faction (no Vendor flag / VEND / VENV) — it exists only as a membership
    # marker, so it can never compete with the real vendor faction the engine
    # resolves for the barter menu.
    global _merchant_marker_faction_fid
    _merchant_marker_faction_fid = writer.derive_formid('FACT', 'TES4MerchantFaction')
    marker = pack_string_subrecord('EDID', 'TES4MerchantFaction')
    marker += pack_string_subrecord('FULL', 'Merchant')
    marker += pack_subrecord('DATA', struct.pack('<I', 0))
    marker += pack_subrecord('CRVA', b'\x01\x01' + b'\x00' * 18)
    writer.add_record('FACT', pack_record('FACT', _merchant_marker_faction_fid,
                                          0, marker))

    print(f"  Creating vendor factions: {len(flst_by_svc)} shared service combos, "
          f"{n_chest} chest-backed merchants, 1 merchant marker faction...")


def get_vendor_faction_fids_for_actor(actor_fid: int, services: int) -> list[int]:
    """Vendor FACT FormIDs this actor should belong to (SNAM memberships).

    A chest-backed merchant gets its dedicated faction (VENC → its own Oblivion
    merchant chest); everyone else gets the shared per-service faction. Both
    kinds carry the VEND keyword list that filters what the actor trades.

    Every merchant additionally joins the marker faction, which is what the
    Barter topic actually gates on — one condition instead of an OR-chain over
    all the vendor factions.
    """
    fids = []
    shared = _vendor_faction_cache.get(_vendor_bits(services), 0)
    if shared:
        fids.append(shared)
    dedicated = _merchant_faction_by_npc.get(actor_fid)
    if dedicated:
        fids.append(dedicated)
    if fids and _merchant_marker_faction_fid:
        fids.append(_merchant_marker_faction_fid)
    return fids


def get_merchant_faction_fid() -> int:
    """The single FACT the Barter topic gates on (0 if no merchants exist).

    One GetInFaction against this replaces the old OR-chain over every vendor
    faction. That chain put 25-30 CTDAs on each Barter INFO — beyond anything
    vanilla ships (max 22 conditions, max OR-run 20) — and the engine dropped
    every one of those lines, taking the whole topic with it.
    """
    return _merchant_marker_faction_fid


# ---------------------------------------------------------------------------
#   Trainer System
# ---------------------------------------------------------------------------
# Skyrim's training menu reads the trainer's skill and level cap from the
# NPC's CLASS (CLAS DATA Teaches/MaxTrainingLevel), but Oblivion stores them
# per-NPC in AIDT (the class fields are just CS defaults — 92 of 114 vanilla
# trainers disagree with their class). So each trainer NPC gets a clone of its
# own class with Teaches/MaxTraining replaced, plus membership in a synthetic
# trainer faction that gates the generated Training dialogue topic.

_trainer_faction_fid = 0
_trainer_class_by_npc: dict[int, int] = {}   # remapped NPC fid -> CLAS clone fid


def _npc_trainer_params(rec: dict):
    """(teaches_tes5_index, max_level) for a trainer NPC, or None.

    A trainer offers the Training service (AIDT bit 14) with a level cap > 0
    and a skill that still exists in Skyrim (Athletics/Acrobatics don't).
    """
    svc = get_int(rec, 'AIDT.Services')
    if not (svc & (1 << 14)):
        return None
    max_train = get_int(rec, 'AIDT.MaxTraining')
    if max_train <= 0:
        return None
    teaches_name = TES4_SKILL_TO_TES5.get(get_int(rec, 'AIDT.Teaches') + 12)
    if not teaches_name or teaches_name not in TES5_SKILL_ORDER:
        return None
    return TES5_SKILL_ORDER.index(teaches_name), min(255, max_train)


def create_trainer_records(by_type: dict, writer) -> None:
    """Phase 0c2: trainer FACT + per-trainer CLAS clones for NPC_ trainers."""
    global _trainer_faction_fid
    _trainer_faction_fid = 0
    _trainer_class_by_npc.clear()

    clas_by_fid = {get_formid(r, 'FormID'): r for r in by_type.get('CLAS', [])
                   if get_formid(r, 'FormID')}

    trainers = []   # (npc_fid, clas_rec_or_None, teaches_idx, max_level)
    for rec in by_type.get('NPC_', []):
        params = _npc_trainer_params(rec)
        if not params:
            continue
        clas_rec = clas_by_fid.get(get_formid(rec, 'CNAM.Class'))
        trainers.append((get_formid(rec, 'FormID'), clas_rec, *params))

    if not trainers:
        return
    print(f"  Creating trainer records for {len(trainers)} trainer NPCs...")

    # One faction marks every trainer; the generated Training topic is gated
    # on GetInFaction(this). Flags 0 like vanilla JobTrainerFaction.
    _trainer_faction_fid = writer.derive_formid('FACT', 'TES4JobTrainerFaction')
    f = pack_string_subrecord('EDID', 'TES4JobTrainerFaction')
    f += pack_string_subrecord('FULL', 'Trainer')
    f += pack_subrecord('DATA', struct.pack('<I', 0))
    f += pack_subrecord('CRVA', b'\x01\x01' + b'\x00' * 18)
    writer.add_record('FACT', pack_record('FACT', _trainer_faction_fid, 0, f))

    # CLAS clones, deduped per (source class, skill, cap). NPCs without a
    # resolvable class get a minimal default class carrying the trainer data.
    clone_cache: dict[tuple, int] = {}
    for npc_fid, clas_rec, teaches_idx, max_level in trainers:
        src_fid = get_formid(clas_rec, 'FormID') if clas_rec else 0
        key = (src_fid, teaches_idx, max_level)
        clone_fid = clone_cache.get(key)
        if not clone_fid:
            clone_fid = writer.derive_formid('TRAINER_CLAS', key)
            edid = (f'TES4Trainer{TES5_SKILL_ORDER[teaches_idx]}'
                    f'{max_level}_{src_fid & 0xFFFFFF:06X}')
            writer.add_record('CLAS', convert_CLAS(
                clas_rec or {}, override_fid=clone_fid, override_edid=edid,
                override_teaches=teaches_idx, override_maxtrain=max_level))
            clone_cache[key] = clone_fid
        _trainer_class_by_npc[npc_fid] = clone_fid


def get_trainer_faction_fid() -> int:
    """The synthetic trainer FACT FormID (0 when no trainers exist)."""
    return _trainer_faction_fid


def get_trainer_class_fid(npc_fid: int) -> int:
    """The trainer CLAS clone for a (remapped) NPC FormID, or 0."""
    return _trainer_class_by_npc.get(npc_fid, 0)


# (remapped) NPC/CREA FormID -> VTYP FormID, from build_npc_to_vtyp_map —
# the VNAM-resolved voice the actor actually used in Oblivion. Set by
# import_main (Phase 0) so VTCK matches the GetIsVoiceType gates and audio
# folders the dialogue pass emits; the (race, gender) computation below is
# only the fallback when the map has no entry.
_npc_voice_map: dict = {}


def set_npc_voice_map(m: dict):
    """Register the NPC->VTYP map (called by import_main before conversion)."""
    global _npc_voice_map
    _npc_voice_map = m or {}


def _resolve_npc_race(rec: dict):
    """Resolve TES4 race FormID to (race_edid, skyrim_race_fid, gender_str)."""
    tes4_race_fid = get_formid(rec, 'RNAM.Race')
    # Mask off load-order high byte — TES4_RACE_FID_TO_EDID uses base FormIDs
    race_edid = TES4_RACE_FID_TO_EDID.get(tes4_race_fid & 0x00FFFFFF, 'Imperial')
    skyrim_race = RACE_MAP.get(race_edid, DEFAULT_RACE)
    tes4_flags = get_int(rec, 'ACBS.Flags')
    gender = 'Female' if (tes4_flags & 1) else 'Male'
    return race_edid, skyrim_race, gender


def resolve_actor_voice(rec: dict, gender: str) -> int:
    """VTYP a converted actor gets: dialogue-pass voice first, then race/gender.

    Same chain as convert_NPC_/convert_CREA VTCK. Also used to fill the male/
    female VTCK slots on generated creature RACE records — vanilla creature
    races always fill both (DogRace: CrDogVoice x2), and a null slot makes the
    CK log "Could not find male/female voice type" per race.
    """
    tes4_race_fid = get_formid(rec, 'RNAM.Race')
    race_edid = TES4_RACE_FID_TO_EDID.get(tes4_race_fid & 0x00FFFFFF, 'Imperial')
    return (_npc_voice_map.get(get_formid(rec, 'FormID'))
            or VOICE_TYPE_MAP.get((race_edid, gender))
            or VOICE_TYPE_MAP.get(('Imperial', gender), 0))


def convert_NPC_(rec: dict, writer=None) -> bytes:
    """NPC_ → NPC_ with TES5 restructuring.

    Correct TES5 subrecord order (from wbDefinitionsTES5.pas):
    EDID VMAD OBND ACBS SNAM[] INAM VTCK TPLT RNAM SPCT SPLO[]
    DEST WNAM ANAM ATKR ATKD/ATKE SPOR OCOR GWOR ECOR PRKZ PRKR[]
    COCT CNTO[] AIDT PKID[] KSIZ KWDA CNAM FULL SHRT DATA DNAM
    PNAM[] HCLF ZNAM GNAM NAM5 NAM6 NAM7 NAM8 DOFT SOFT ...
    """
    race_edid, skyrim_race, gender = _resolve_npc_race(rec)

    subs = b''
    edid = get_str(rec, 'EditorID')
    if edid:
        subs += pack_string_subrecord('EDID', edid)

    # VMAD — converted TES4 actor script (SCRI), attached to the base so every
    # placed reference gets an instance (mirrors TES4 semantics).
    from ..object_scripts import get_object_vmad
    subs += get_object_vmad(get_formid(rec, 'FormID'))

    # OBND (NPC_ default bounds)
    subs += pack_obnd(-12, -12, 0, 12, 12, 60)

    # ACBS — 24 bytes in TES5 (shared with the override path)
    subs += pack_subrecord('ACBS', _npc_acbs(rec))

    # SNAM — Factions
    fc = get_int(rec, 'FactionCount')
    for i in range(fc):
        fid = get_formid(rec, f'Faction[{i}].FormID')
        rank = get_int(rec, f'Faction[{i}].Rank')
        subs += pack_subrecord('SNAM', struct.pack('<IbBBB', fid, rank, 0, 0, 0))

    # SNAM — Vendor factions (if this NPC sells anything): the shared per-service
    # faction (barter dialogue gate) plus, for chest-backed merchants, the
    # dedicated faction whose VENC stocks the barter menu from its chest.
    services = get_int(rec, 'AIDT.Services')
    vendor_fids = get_vendor_faction_fids_for_actor(get_formid(rec, 'FormID'),
                                                    services)
    for vfid in vendor_fids:
        subs += pack_subrecord('SNAM', struct.pack('<IbBBB', vfid, 0, 0, 0, 0))
    vendor_fid = vendor_fids[0] if vendor_fids else 0

    # SNAM — Trainer faction (gates the generated Training dialogue topic)
    trainer_clas_fid = get_trainer_class_fid(get_formid(rec, 'FormID'))
    if trainer_clas_fid and _trainer_faction_fid:
        subs += pack_subrecord('SNAM', struct.pack('<IbBBB',
                                                   _trainer_faction_fid, 0, 0, 0, 0))

    # SNAM — Plugin-origin marker (root masters only). Keeps this file's
    # unscoped dialogue off another converted plugin's actors.
    if _origin_faction_fid:
        subs += pack_subrecord('SNAM', struct.pack('<IbBBB',
                                                   _origin_faction_fid, 0, 0, 0, 0))

    # INAM — Death item
    inam = get_formid(rec, 'INAM.DeathItem')
    if inam:
        subs += pack_formid_subrecord('INAM', inam)

    # VTCK — Voice type (custom VTYP created in Phase 0). Primary source is
    # the VNAM-resolved per-NPC map (matches dialogue voice gates + audio
    # folders); fall back to literal race, then Imperial.
    voice = (_npc_voice_map.get(get_formid(rec, 'FormID'))
             or VOICE_TYPE_MAP.get((race_edid, gender))
             or VOICE_TYPE_MAP.get(('Imperial', gender), 0))
    if voice:
        subs += pack_formid_subrecord('VTCK', voice)

    # RNAM — Race (mapped to Skyrim equivalent)
    subs += pack_formid_subrecord('RNAM', skyrim_race)

    # SPCT + SPLO — Spells
    sc = get_int(rec, 'SpellCount')
    if sc > 0:
        spell_fids = [get_formid(rec, f'Spell[{i}]') for i in range(sc)]
        spell_fids = [s for s in spell_fids if s]
        if spell_fids:
            subs += pack_subrecord('SPCT', struct.pack('<I', len(spell_fids)))
            for sfid in spell_fids:
                subs += pack_formid_subrecord('SPLO', sfid)

    # COCT + CNTO — carried inventory, and the wearables that become the outfit.
    # The TES4 inventory holds both; Skyrim needs them SPLIT (see outfits.py):
    # the outfit is added on top of CNTO at load, so an item in both is carried
    # twice, and only wearables may appear in an outfit at all.
    outfit_fids, carried = split_inventory(_read_items(rec))
    coct = 0
    item_data = b''
    for fid, count in carried:
        item_data += pack_subrecord('CNTO', struct.pack('<Ii', fid, count))
        coct += 1
    # Vendor buying power: TES5 has no barter-gold field — a chest-less vendor
    # trades from its own inventory, so ACBS.BarterGold becomes carried gold.
    barter_gold = get_int(rec, 'ACBS.BarterGold') if vendor_fid else 0
    if barter_gold > 0:
        item_data += pack_subrecord('CNTO', struct.pack('<Ii', GOLD001_FID,
                                                        barter_gold))
        coct += 1
    if coct:
        subs += pack_uint32_subrecord('COCT', coct)
        subs += item_data

    # AIDT — AI data
    subs += pack_subrecord('AIDT', _npc_aidt(rec))

    # PKID — the actor's own AI packages, converted (pack_converter.py) and kept
    # in TES4 ORDER: Skyrim, like Oblivion, runs the first package whose
    # conditions pass, so the order IS the behaviour.  Quest packages are
    # excluded — they reach the actor through a QUST reference alias (ALPC),
    # which is what lets them outrank this standing schedule.
    pc = get_int(rec, 'AIPackageCount')
    pack_fids = [get_formid(rec, f'AIPackage[{i}]') for i in range(pc)]
    for pfid in npc_packages(pack_fids):
        subs += pack_formid_subrecord('PKID', pfid)

    # CNAM — Class. Trainer NPCs get their synthesized class clone (carries
    # the AIDT Teaches/MaxTraining data Skyrim's training menu reads).
    cnam = trainer_clas_fid or get_formid(rec, 'CNAM.Class')
    if cnam:
        subs += pack_formid_subrecord('CNAM', cnam)

    # FULL — Name (comes after CNAM in TES5!)
    full = get_str(rec, 'FULL')
    if full:
        subs += pack_string_subrecord('FULL', full)

    # DATA — empty marker (0 bytes)
    subs += pack_subrecord('DATA', b'')

    # DNAM — Skills + stats (52 bytes)
    subs += pack_subrecord('DNAM', _npc_skills_dnam(rec))

    # PNAM[] — Head parts: hair HDPT + eyes HDPT
    subs += build_pnam_subs(rec, race_edid, gender, writer)

    # HCLF — Hair color.  Oblivion authors a FREE RGB per NPC (2,482 actors,
    # 571 distinct colors spanning the whole cube), while Skyrim's HCLF is a
    # FormID into CLFM and vanilla ships only 15 swatches, all dark and
    # desaturated.  Snapping to the nearest vanilla swatch loses the authored
    # color badly (measured over Oblivion.esm: mean RGB error 26.9, max 274.5),
    # so the authored color gets its own generated CLFM instead and the vanilla
    # table is only the fallback when no writer is available.
    hclr_r = get_int(rec, 'HCLR.R', 100)
    hclr_g = get_int(rec, 'HCLR.G', 80)
    hclr_b = get_int(rec, 'HCLR.B', 60)
    if writer is not None:
        subs += pack_formid_subrecord(
            'HCLF', hair_color_formid(writer, hclr_r, hclr_g, hclr_b))
    else:
        subs += pack_formid_subrecord(
            'HCLF', map_hair_color(hclr_r, hclr_g, hclr_b))

    # ZNAM — Combat style. CSTY is skipped, so the TES4 reference would
    # dangle; the vanilla default combat style keeps combat AI functional.
    if get_formid(rec, 'ZNAM.CombatStyle'):
        subs += pack_formid_subrecord('ZNAM', CSTY_DEFAULT)

    # NAM5 / NAM6 / NAM7 / NAM8 — all four are SetRequired in the TES5 NPC_
    # definition and present on every one of the 5118 vanilla NPC_ records.
    # NAM5 and NAM8 were missing entirely; NAM8 'Sound Level' is what lets the
    # engine voice an actor at all, so without it an NPC is silent.
    subs += pack_subrecord('NAM5', _NAM5_UNKNOWN)
    # NAM6 / NAM7 — Height / Weight (TES4 has these on RACR records, not NPC;
    # use neutral defaults so the race's own scale applies)
    subs += pack_subrecord('NAM6', struct.pack('<f', 1.0))
    subs += pack_subrecord('NAM7', struct.pack('<f', 1.0))
    subs += pack_uint32_subrecord('NAM8', _SOUND_LEVEL_NORMAL)

    # DOFT — Default outfit (requires OTFT companion record)
    # TES4 NPCs equip out of CNTO; TES5 wears exactly what the outfit lists.
    if writer is not None and outfit_fids:
        subs += pack_formid_subrecord(
            'DOFT', _build_outfit(writer, (edid or 'NPC') + '_Outfit',
                                  outfit_fids, get_formid(rec, 'FormID')))

    # DPLT — default package list: the vanilla fallback AI most NPCs carry
    subs += pack_formid_subrecord('DPLT', DPLT_NPC_LIST)

    # Trailing face data: FTST, QNAM, NAM9, NAMA
    subs += build_face_tail_subs(rec, race_edid, gender)

    return pack_record('NPC_', get_formid(rec, 'FormID'), get_int(rec, 'RecordFlags'), subs)


def convert_CREA(rec: dict, writer=None) -> bytes:
    """CREA → NPC_ (creatures become NPCs in TES5).

    Same subrecord order as NPC_: EDID OBND ACBS SNAM INAM VTCK RNAM
    SPCT SPLO[] COCT/CNTO AIDT PKID FULL DATA DNAM ZNAM DOFT DPLT
    """
    subs = b''
    edid = get_str(rec, 'EditorID')
    if edid:
        subs += pack_string_subrecord('EDID', edid)

    # VMAD — converted TES4 creature script (SCRI), attached to the base so
    # every placed reference gets an instance (mirrors TES4 semantics).
    from ..object_scripts import get_object_vmad
    subs += get_object_vmad(get_formid(rec, 'FormID'))

    subs += pack_obnd(-12, -12, 0, 12, 12, 60)  # NPC_ default bounds

    # ACBS — auto-calc stats for creatures (shared with the override path)
    subs += pack_subrecord('ACBS', _crea_acbs(rec))

    # Factions
    fc = get_int(rec, 'FactionCount')
    for i in range(fc):
        fid = get_formid(rec, f'Faction[{i}].FormID')
        rank = get_int(rec, f'Faction[{i}].Rank')
        subs += pack_subrecord('SNAM', struct.pack('<IbBBB', fid, rank, 0, 0, 0))

    # Vendor factions (if this creature sells anything) — see convert_NPC_.
    crea_services = get_int(rec, 'AIDT.Services')
    crea_vendor_fids = get_vendor_faction_fids_for_actor(
        get_formid(rec, 'FormID'), crea_services)
    for vfid in crea_vendor_fids:
        subs += pack_subrecord('SNAM', struct.pack('<IbBBB', vfid, 0, 0, 0, 0))
    crea_vendor_fid = crea_vendor_fids[0] if crea_vendor_fids else 0

    # Plugin-origin marker (root masters only) — see convert_NPC_.
    if _origin_faction_fid:
        subs += pack_subrecord('SNAM', struct.pack('<IbBBB',
                                                   _origin_faction_fid, 0, 0, 0, 0))

    # Death item
    inam = get_formid(rec, 'INAM.DeathItem')
    if inam:
        subs += pack_formid_subrecord('INAM', inam)

    # VTCK — Voice type (must come before RNAM per TES5 NPC_ definition)
    # Race: generated creature race (creature pipeline project) when
    # available; else the legacy Skyrim-race aliasing fallback.
    from ..creature_races import get_creature_race
    full = get_str(rec, 'FULL')
    _src = None
    crea_race_fid = get_creature_race(get_formid(rec, 'FormID') & 0x00FFFFFF)
    if crea_race_fid is None:
        crea_race_fid, _src, _alt = resolve_creature_race(edid, full)
    tes4_flags = get_int(rec, 'ACBS.Flags')
    gender = 'Female' if (tes4_flags & 1) else 'Male'
    tes4_race_fid = get_formid(rec, 'RNAM.Race')
    race_edid = TES4_RACE_FID_TO_EDID.get(tes4_race_fid & 0x00FFFFFF, '')
    if not race_edid:
        race_edid = _src if _src else 'Imperial'
    voice = (_npc_voice_map.get(get_formid(rec, 'FormID'))
             or VOICE_TYPE_MAP.get((race_edid, gender))
             or VOICE_TYPE_MAP.get(('Imperial', gender), 0))
    # ALWAYS emit VTCK, even when the humanoid chain yields nothing (which is
    # the normal case for a creature — no TES4 creature race is in
    # VOICE_TYPE_MAP). Two reasons, and skipping it broke both:
    #   * Vanilla never ships an actor without one: all 3,887 Skyrim.esm NPC_
    #     records carry a non-zero VTCK.
    #   * creature_races.patch_creature_voices rewrites this slot in the packed
    #     bytes once the creature VTYPs exist, and it can only patch a
    #     subrecord that is already THERE — it finds VTCK or skips the record.
    #     Omitting it left all 442 converted creatures with no voice type at
    #     all, so every creature sound channel was dead no matter how correct
    #     the descriptors, triggers and audio files were.
    subs += pack_formid_subrecord('VTCK', voice)

    # RNAM — Race (after VTCK per TES5 NPC_ definition)
    subs += pack_formid_subrecord('RNAM', crea_race_fid)

    # SPCT + SPLO — Spells. Creatures carry their magic through SPLO exactly
    # like NPCs do (the stunted scamp's fireball, a summoner's summon, an
    # atronach's touch attack); convert_CREA simply never emitted them, so
    # ALL 600 of the 914 Oblivion CREA that know a spell shipped with none
    # and could not cast whatever the behavior graph offered them.
    #
    # Order is RNAM -> SPCT -> SPLO[] -> COCT -> CNTO, verified against BOTH
    # the xEdit TES5 definition (wbDefinitionsTES5.pas: wbSPCT, wbSPLOs
    # directly after RNAM) and real Skyrim.esm records.
    #
    # The target may be a SPEL, a SHOU or an LVSP — xEdit types SPLO as
    # [SPEL, SHOU, LVSP], so a TES4 leveled spell (LVSP survives conversion
    # via convert_LVSP) is referenced directly rather than unrolled. The
    # scamp above is exactly that case: its fireball arrives through
    # LL2CreatureScampStunted100.
    crea_spell_count = get_int(rec, 'SpellCount')
    if crea_spell_count > 0:
        crea_spells = [get_formid(rec, f'Spell[{i}]')
                       for i in range(crea_spell_count)]
        crea_spells = [s for s in crea_spells if s]
        if crea_spells:
            subs += pack_subrecord('SPCT',
                                   struct.pack('<I', len(crea_spells)))
            for sfid in crea_spells:
                subs += pack_formid_subrecord('SPLO', sfid)

    # Items — carried inventory and outfit are disjoint (see convert_NPC_).
    # Creature inventories are mostly loot leveled-lists, which belong in CNTO;
    # only the armed/armored ones (skeletons, dremora) yield an outfit at all.
    outfit_fids, carried = split_inventory(_read_items(rec))
    coct = 0
    item_data = b''
    for fid, count in carried:
        item_data += pack_subrecord('CNTO', struct.pack('<Ii', fid, count))
        coct += 1
    # Vendor buying power (see convert_NPC_): barter gold -> carried gold.
    crea_barter_gold = get_int(rec, 'ACBS.BarterGold') if crea_vendor_fid else 0
    if crea_barter_gold > 0:
        item_data += pack_subrecord('CNTO', struct.pack('<Ii', GOLD001_FID,
                                                        crea_barter_gold))
        coct += 1
    if coct:
        subs += pack_uint32_subrecord('COCT', coct)
        subs += item_data

    # AIDT — 20 bytes (TES5 format, shared with NPC_). Creatures use the
    # predator threshold: any TES4 aggression above the passive default (5)
    # means "attack on sight", which in TES5 requires VeryAggressive.
    subs += pack_subrecord('AIDT', _npc_aidt(rec, is_creature=True))

    # PKID — TES4 PACK records are skipped (SKIP_TYPES), so a raw pass-through
    # gave creatures NO working packages → the AI layer made no decisions and
    # the engine never sent the graph movement/attack events (the stuck-in-idle
    # root cause). Every vanilla creature carries exactly ONE package,
    # DefaultMasterPackageCreature — give converted creatures the same hookup.
    subs += pack_formid_subrecord('PKID', PKID_CREATURE_MASTER)

    # CNAM — Class. 5118/5118 vanilla NPC_ carry one; a creature never did.
    # Under ACBS AutoCalc (set above) the engine derives the actor's skills
    # and attribute growth from the CLASS weights, so a classless actor is a
    # skill-less one — and the class is where a vanilla caster's magic
    # profile lives: EncAtronachFlame uses EncClassBanditWizard (Magicka
    # weight 3, Destruction 3), EncHagraven its own mage class, while the
    # wolf/sabrecat/skeever/spriggan/wisp share EncClassAnimalPredator
    # (Magicka weight 0). A creature that knows an offensive spell gets the
    # atronach's class; everything else the predators'.
    from ..creature_races import creature_has_offensive_spell
    subs += pack_formid_subrecord(
        'CNAM', CLAS_CREATURE_CASTER if creature_has_offensive_spell([rec])
        else CLAS_CREATURE_PREDATOR)

    # FULL — Name (after CNAM in TES5 NPC_ order)
    if full:
        subs += pack_string_subrecord('FULL', full)

    # DATA — empty marker
    subs += pack_subrecord('DATA', b'')

    # DNAM — Skills from creature aggregate stats
    dnam = bytearray(52)
    combat = get_int(rec, 'DATA.CombatSkill', 30)
    magic = get_int(rec, 'DATA.MagicSkill', 30)
    stealth = get_int(rec, 'DATA.StealthSkill', 30)

    skill_defaults = {
        'OneHanded': combat, 'TwoHanded': combat, 'Block': combat, 'Smithing': combat,
        'HeavyArmor': combat, 'LightArmor': stealth,
        'Marksman': stealth, 'Sneak': stealth, 'Lockpicking': stealth, 'Pickpocket': stealth,
        'Destruction': magic, 'Conjuration': magic, 'Alteration': magic,
        'Illusion': magic, 'Restoration': magic,
        'Alchemy': magic, 'Speechcraft': stealth, 'Enchanting': magic // 3,
    }
    for i, skill_name in enumerate(TES5_SKILL_ORDER):
        dnam[i] = min(skill_defaults.get(skill_name, 15), 255)

    # DNAM 36/38/40 is the engine's calculated Health/Magicka/Stamina CACHE (see
    # _npc_skills_dnam). Slots 38/40 previously took raw Intelligence/Strength
    # attributes; the real pools are SpellPoints and Fatigue.
    health = get_int(rec, 'DATA.Health', 50)
    struct.pack_into('<H', dnam, 36, max(0, min(health, 65535)))
    magicka = get_int(rec, 'ACBS.SpellPoints', 0)
    struct.pack_into('<H', dnam, 38, max(0, min(magicka, 65535)))
    stamina = get_int(rec, 'ACBS.Fatigue', 100)
    struct.pack_into('<H', dnam, 40, max(0, min(stamina, 65535)))
    subs += pack_subrecord('DNAM', bytes(dnam))

    # ZNAM — combat style (CSTY is skipped; use vanilla styles).
    # TES4 DATA.Type: 0=Creature, 1=Daedra, 2=Undead, 3=Humanoid, 4=Horse.
    crea_type = get_int(rec, 'DATA.Type')
    subs += pack_formid_subrecord(
        'ZNAM', CSTY_ANIMAL if crea_type in (0, 4) else CSTY_DEFAULT)

    # NAM5/6/7/8 — required by the TES5 NPC_ definition and present on ALL 5118
    # vanilla NPC_ records (xEdit marks each SetRequired). convert_CREA wrote
    # none of them. NAM8 'Sound Level' is the one that matters here: it gates
    # how loudly the actor is heard, and every vanilla actor carrying a CSDT
    # sound array (31/31) also carries NAM8 — a creature without it has sound
    # descriptors the engine never voices.
    subs += pack_subrecord('NAM5', _NAM5_UNKNOWN)
    subs += pack_subrecord('NAM6', _crea_nam6(rec))          # height
    subs += pack_subrecord('NAM7', struct.pack('<f', 50.0))  # weight
    subs += pack_uint32_subrecord('NAM8', _SOUND_LEVEL_NORMAL)

    # Creature sounds — the record channel carries the Hit slot only (see
    # _RECORD_SOUND_TYPES for the full vanilla-census channel split); a
    # creature with no own sounds gets a PLACEHOLDER CSCR here which
    # patch_actor_sounds/_flatten_cscr later replaces with the target's
    # resolved array — TES4 inheritance CHAINS (and targets that were
    # skipped records) are shapes vanilla CSCR never has, so no CSCR
    # survives into the output. Order per wbDefinitionsTES5 NPC_: CSDT[]
    # then CSCR, both after ZNAM and before DOFT.
    sound_subs = _actor_sound_subs(rec)
    if sound_subs:
        subs += sound_subs
    else:
        inherit = get_formid(rec, 'CSCR.InheritSound')
        if inherit:
            subs += pack_formid_subrecord('CSCR', inherit)

    # DOFT — Default outfit
    if writer is not None and outfit_fids:
        subs += pack_formid_subrecord(
            'DOFT', _build_outfit(writer, (edid or 'CREA') + '_Outfit',
                                  outfit_fids, get_formid(rec, 'FormID')))

    # DPLT — default package list, like every vanilla creature (EncWolf etc.)
    subs += pack_formid_subrecord('DPLT', DPLT_CREATURE_LIST)

    return pack_record('NPC_', get_formid(rec, 'FormID'), get_int(rec, 'RecordFlags'), subs)


def convert_FACT(rec: dict) -> bytes:
    subs = b''
    edid = get_str(rec, 'EditorID')
    if edid:
        subs += pack_string_subrecord('EDID', edid)
    full = get_str(rec, 'FULL')
    if full:
        subs += pack_string_subrecord('FULL', full)

    # Relations → XNAM: Faction(FormID) Modifier(S32) GroupCombatReaction(U32)
    #
    # The enum is xEdit wbFactionRelations (wbDefinitionsCommon):
    #   0 Neutral, 1 Enemy, 2 ALLY, 3 FRIEND
    # Ally and Friend were previously swapped here (3 written for Ally, 2 for
    # Friend).  Confirmed two ways: the xEdit definition above, and a census of
    # Skyrim.esm where 160 of 200 faction SELF-relations use 2 — a faction is
    # Ally to itself, never merely Friend.
    #
    # Why the swap was a live bug and not cosmetic: Ally is the tier that makes
    # members ASSIST each other into combat (UESP Skyrim:Factions — reaction
    # combines with aggression and ASSISTANCE to decide who joins a fight).
    # Oblivion's CG data has BladesCG → MythicDawnCG at +100, which in TES4 is
    # only a disposition bonus ("these people like each other"); TES4 starts the
    # intro fight with StartCombat, not with the faction graph.  Converted with
    # the swap, that +100 became Ally, so the Emperor's guards assisted the
    # Mythic Dawn and turned on the player — who is a Neutral — instead of on
    # the assassins.
    #
    # Reaction thresholds also tightened: TES4 dispositions are a 0-100 SCALAR
    # that shifts how much an actor likes a target, whereas TES5's Ally is a
    # hard "fight alongside them" contract.  Reserving Ally for a faction's
    # relation to ITSELF (Oblivion's universal idiom for "we are one group",
    # 147 of 660 relations) keeps the assist graph vanilla-shaped; a positive
    # relation to a DIFFERENT faction means "friendly", which is Friend.  This
    # is what stops any converted plugin from silently wiring bystanders into
    # someone else's fight.
    self_fid = get_formid(rec, 'FormID')
    rc = get_int(rec, 'RelationCount')
    for i in range(rc):
        fid = get_formid(rec, f'Relation[{i}].Faction')
        disp = get_int(rec, f'Relation[{i}].Disposition')
        if disp <= -50:
            reaction = 1                        # Enemy
        elif disp >= 50:
            # Ally only for the self-relation; cross-faction goodwill is Friend.
            reaction = 2 if fid == self_fid else 3
        else:
            reaction = 0                        # Neutral
        # Modifier is 0 in 1035 of 1036 vanilla Skyrim relations — the TES4
        # disposition scalar has no meaning in TES5 and the reaction enum
        # carries the whole signal, so writing it here was noise.
        subs += pack_subrecord('XNAM', struct.pack('<IiI', fid, 0, reaction))

    # DATA — Flags.  The two games number these differently, so the old
    # straight passthrough mis-landed every bit: TES4 bit 1 is "Evil" but TES5
    # bit 1 is "Special Combat", and TES4 bit 2 (Special Combat) became TES5
    # bit 2 (unused).  Bit meanings per xEdit wbDefinitionsTES4/TES5:
    #   TES4 (U8):  0 Hidden from Player, 1 Evil, 2 Special Combat
    #   TES5 (U32): 0 Hidden From NPC, 1 Special Combat, 6 Track Crime,
    #               7-11/13/16 Ignore Crimes: *, 12 Crime Gold Use Defaults,
    #               14 Vendor, 15 Can Be Owner
    tes4_flags = get_int(rec, 'DATA.Flags')
    tes5_flags = 0x8000                       # Can Be Owner — always set
    if tes4_flags & 0x01:                     # Hidden from Player
        tes5_flags |= 0x0001                  #   → Hidden From NPC
    if tes4_flags & 0x04:                     # Special Combat
        tes5_flags |= 0x0002                  #   → Special Combat

    # Crime tracking.  The old code set the *Ignore* Crimes bits (7-11, 13, 16)
    # off the Evil flag, which is the exact opposite of the intent — it told the
    # engine to ignore murder, assault, stealing, trespass and pickpocket — and
    # it never set Track Crime (bit 6) on anything.  Oblivion has no per-faction
    # crime-tracking flag: its crime system is driven by the GetPCFaction*
    # family, which the engine maintains for any faction the player can offend.
    # A faction is a crime faction here when converted scripts actually test it.
    if (edid or '').lower() in TES4_CRIME_FACTIONS:
        tes5_flags |= 0x0040                  # Track Crime
    subs += pack_subrecord('DATA', struct.pack('<I', tes5_flags))

    # CNAM → CRVA (Crime Values).  Layout per xEdit wbDefinitionsTES5 and
    # verified byte-for-byte against Skyrim.esm's WERoad12HorsemanFaction
    # (0101 E803 2800 0500 1900 0000 0000003F 6400 E803):
    #   Arrest U8, Attack On Sight U8, Murder U16, Assault U16, Trespass U16,
    #   Pickpocket U16, Unknown U16, Steal Multiplier Float, Escape U16,
    #   Werewolf U16.  The old '<HHHHIfI' packing was the same 20 bytes but
    #   misaligned every field: the leading U16 swallowed both U8 booleans, so
    #   no converted crime faction ever arrested, and murder/assault/trespass/
    #   pickpocket were all left at 0 — meaning GetCrimeGoldViolent() and
    #   GetCrimeGoldNonViolent() returned 0 forever and every converted
    #   crime-flag test was permanently false.
    #
    # Amounts follow the vanilla census: all 14 real Skyrim crime factions use
    # exactly murder=1000, assault=40, trespass=5, pickpocket=25, escape=100.
    # The 25x murder/assault gap is what lets converted scripts tell the two
    # apart (see TES4_HasFactionMurder in the script converter).  Werewolf is
    # left 0: a converted Oblivion plugin has no werewolf crime.
    crime_gold = get_float(rec, 'CNAM.CrimeGold', 1.0)
    is_crime_faction = bool(tes5_flags & 0x0040)   # Track Crime (xEdit bit 6)
    if is_crime_faction:
        crva = struct.pack('<BBHHHHHfHH', 1, 1, 1000, 40, 5, 25, 0,
                           crime_gold, 100, 0)
    else:
        crva = struct.pack('<BBHHHHHfHH', 1, 1, 0, 0, 0, 0, 0,
                           crime_gold, 0, 0)
    subs += pack_subrecord('CRVA', crva)

    return pack_record('FACT', get_formid(rec, 'FormID'), get_int(rec, 'RecordFlags'), subs)


def convert_EYES(rec: dict) -> bytes:
    # Map to TES5 record formIDs instead
    pass


# HDPT.PNAM 'Type' enum (xEdit wbDefinitionsTES5): 3 = Hair.
HDPT_TYPE_HAIR = 3

# HDPT.DATA flag bits: 0 Playable, 1 Male, 2 Female, 3 IsExtraPart,
# 4 UseSolidTint.  Vanilla hair is overwhelmingly Playable+Male (3, x93) or
# Playable+Female (5, x76) out of 204 hair HDPTs, so gender-restrict when the
# TES4 record says so and leave both flags set when it does not.
HDPT_FLAG_PLAYABLE = 0x01
HDPT_FLAG_MALE = 0x02
HDPT_FLAG_FEMALE = 0x04

# HDPT.NAM0 'Part Type': 0 Race Morph, 1 Tri, 2 Chargen Morph.  All 123
# vanilla hair HDPTs that carry a part use NAM0=1.  A NAM0=0 races tri was
# tried on converted hair and THE ENGINE DOES NOT APPLY IT to type-3 (Hair)
# parts (vanilla only ships one on heads, type 1, and beards, type 4; in
# game the hair rendered unmorphed).  Per-race conformance is instead BAKED:
# one mesh per race GROUP, gated by RNAM race lists — vanilla hair's own
# architecture.
HDPT_PART_TRI = 1

# RNAM — Valid Races FLST.  Skyrim gates which races may wear a head part on
# this list, and getting it wrong makes the hair INVISIBLE in the race menu for
# every race not on it.
#
# 000A8023 is HeadPartsHumansandVampires -- HUMANS ONLY, despite the name this
# constant used to carry.  Pointing every converted hair at it is why only Nords
# and the other human races saw the new hairstyles and Argonian/Khajiit/Orc/Elf
# saw none of theirs.  The FLST names below are read out of Skyrim.esm.
HDPT_RNAM_HUMANS = 0x000A8023          # HeadPartsHumansandVampires
HDPT_RNAM_ELVES = 0x000A8024           # HeadPartsElvesandVampires
HDPT_RNAM_ORC = 0x000A8032             # HeadPartsOrcandVampire
HDPT_RNAM_ARGONIAN = 0x000A8039        # HeadPartsArgonianandVampire
HDPT_RNAM_KHAJIIT = 0x000A8036         # HeadPartsKhajiitandVampire
HDPT_RNAM_REDGUARD = 0x000A803B        # HeadPartsRedguardandVampire
HDPT_RNAM_DREMORA = 0x000A8027         # HeadPartsDremora
HDPT_RNAM_ALL_MINUS_BEAST = 0x000A803F  # HeadPartsAllRacesMinusBeast (19 races)

# Which list an Oblivion hair belongs on, matched on its EditorID.  Oblivion
# names every hair for the race it was authored for, and the mesh filename
# agrees with the EditorID on all 57 records (checked), so this is the
# plugin's own statement rather than a guess.  Order matters: 'DarkElf' and
# 'HighElf'/'WoodElf' must be tested before the bare 'Elf' substring.
#
# Vanilla routes its own hair exactly this way -- censused over Skyrim.esm's
# hair HDPTs: every Khajiit hair uses 000A8036 (x21), every Orc 000A8032
# (x43), every Elf 000A8024 (x36), every Argonian 000A8039, Dremora 000A8027.
_HDPT_RNAM_BY_EDID = (
    ('argonian', HDPT_RNAM_ARGONIAN),
    ('khajiit', HDPT_RNAM_KHAJIIT),
    ('orc', HDPT_RNAM_ORC),
    ('dremora', HDPT_RNAM_DREMORA),
    ('darkelf', HDPT_RNAM_ELVES),
    ('highelf', HDPT_RNAM_ELVES),
    ('woodelf', HDPT_RNAM_ELVES),
    ('elf', HDPT_RNAM_ELVES),
    ('redguard', HDPT_RNAM_REDGUARD),
)


def _hdpt_valid_races(edid: str) -> int:
    """The Valid Races FLST for a converted Oblivion hair.

    Race-specific hair goes on that race's list; anything Oblivion did not name
    for a race (Cropped, Ponytail, MediumLength, Blindfold, the styleNN set)
    is generic and goes on HeadPartsAllRacesMinusBeast, matching how Oblivion
    offered those styles to every non-beast race.
    """
    low = (edid or '').lower()
    for token, flst in _HDPT_RNAM_BY_EDID:
        if token in low:
            return flst
    # Explicitly human-named styles stay on the human list.
    for token in ('nord', 'imperial', 'breton'):
        if token in low:
            return HDPT_RNAM_HUMANS
    return HDPT_RNAM_ALL_MINUS_BEAST

# GENERIC hair (no race in its EDID) is emitted once per race GROUP: the
# in-game head = base mesh + the wearer race's races-tri morph, and the
# scalp measurements split cleanly (head_fit.GROUP_MORPHS): all five human
# races + Dremora wear the BASE scalp (morphs <= 0.15 there), the three elf
# races share one shape (2.6 off base), Orc its own (1.5).  Each group entry:
# (group key for formids/naming, hair_pipeline group name, RNAM FLST).
# The human mesh serves two HDPTs — the humans+vampires list and the
# one-race Dremora list (same base scalp).
HDPT_GROUPS = (
    ('',  None,     HDPT_RNAM_HUMANS),      # base mesh, keeps existing ids
    ('D', None,     HDPT_RNAM_DREMORA),     # base mesh, Dremora list
    ('E', 'elves',  HDPT_RNAM_ELVES),
    ('O', 'orc',    HDPT_RNAM_ORC),
)

# CLFM.FNAM 'Playable' — vanilla hair colors are all playable.
_CLFM_PLAYABLE = 1


def hair_color_formid(writer, r: int, g: int, b: int) -> int:
    """CLFM FormID for an authored Oblivion hair color, generating it once.

    Skyrim reads an NPC's hair color from HCLF -> CLFM.CNAM, so carrying
    Oblivion's authored RGB across only needs a CLFM holding that exact color.
    The record is tiny (EDID + CNAM + FNAM), and NPCs share colors heavily
    (2,482 actors use 571 distinct values in Oblivion.esm), so one record per
    DISTINCT color is far cheaper than one per actor.

    Keyed on the authored RGB itself, which is authored TES4 data -- the same
    color always lands on the same id, on every machine and in every build.
    """
    r = max(0, min(255, int(r)))
    g = max(0, min(255, int(g)))
    b = max(0, min(255, int(b)))
    key = (r, g, b)
    fid = writer.derive_formid('CLFM_HAIR', key)

    cache = getattr(writer, '_tes4_hair_colors', None)
    if cache is None:
        cache = set()
        writer._tes4_hair_colors = cache
    if key in cache:
        return fid
    cache.add(key)

    subs = pack_string_subrecord('EDID', 'TES4HairColor%02X%02X%02X' % key)
    subs += pack_string_subrecord('FULL', 'Hair %02X%02X%02X' % key)
    # CNAM is a byte RGBA; vanilla hair colors all carry alpha 0.
    subs += pack_subrecord('CNAM', struct.pack('<4B', r, g, b, 0))
    subs += pack_subrecord('FNAM', struct.pack('<I', _CLFM_PLAYABLE))
    writer.add_record('CLFM', pack_record('CLFM', fid, 0, subs))
    return fid


def hair_variant_formid(writer, source_fid: int, bucket: int,
                        female: bool, base_female: bool,
                        group: str = '') -> int:
    """The HDPT FormID for one (hair, bucket, gender, race group) variant.

    The BASE variant -- bucket 0 of the hair's base gender in the human
    group -- keeps the SOURCE FormID, so an NPC whose LNAM is 0 resolves
    straight through its HNAM.  Every other variant derives from authored
    data only: the masked source id, the LNAM bucket, the gender ('F') and
    the race group tag ('D'/'E'/'O') -- see HDPT_GROUPS.
    """
    if bucket <= 0 and female == base_female and not group:
        return source_fid
    key = (source_fid & 0x00FFFFFF, bucket)
    if female:
        key = key + ('F',)
    if group:
        key = key + (group,)
    return writer.derive_formid('HDPT_HAIR', key)


def convert_HAIR(rec: dict, *, writer=None) -> bytes:
    """HAIR -> HDPT (Type 3 / Hair), one per (length, gender) variant.

    Returns the base record (unmorphed mesh, base gender, source FormID) and
    side-emits an HDPT for every other variant the plugin's NPCs ask for:

    LENGTH   Skyrim has no per-NPC hair-length field, so NPC_.LNAM is baked
             into the mesh per quantized bucket (asset_convert.hair_pipeline).
    GENDER   every mesh is FITTED to the Skyrim head (asset_convert.head_fit)
             and the male and female Skyrim skulls differ by up to 1.23 units
             over the scalp, so each allowed gender gets its own mesh + HDPT,
             exactly as vanilla genders every hairstyle.  TES4's NotMale /
             NotFemale restriction picks which genders exist at all.
    """
    from asset_convert.hair_pipeline import (_fit_group_lock, hair_genders,
                                             output_model_path,
                                             output_tri_path, variant_edid)
    from asset_convert.head_fit import fit_race_for_hair
    from ..hair_variants import hair_buckets_for, hair_has_tri

    model = get_str(rec, 'Model.MODL')
    source_fid = get_formid(rec, 'FormID')
    edid = get_str(rec, 'EditorID')
    # A few Oblivion hairs ship no .tri; naming a NAM1 we never wrote makes
    # the CK report a missing file for each one.
    want_tri = bool(model) and hair_has_tri(source_fid)
    # Generic hair (not race-named, not a beast head) is emitted once per
    # race GROUP — see HDPT_GROUPS; race-named hair keeps its single HDPT.
    generic = (fit_race_for_hair(edid) is None
               and _fit_group_lock(edid) is None)
    groups = HDPT_GROUPS if generic else (('', None, 0),)

    genders = hair_genders(get_int(rec, 'DATA.Flags'))
    base_female = genders[0]

    def build(bucket, female, tag, name_grp, rnam, fid_override=0):
        edid_grp = {'E': 'elves', 'O': 'orc', 'D': 'dremora'}.get(tag)
        return _build_hdpt(
            rec,
            model_override=output_model_path(model, bucket, female, name_grp)
            if model else '',
            tri_path=output_tri_path(model, bucket, female, name_grp)
            if want_tri else '',
            edid_override=variant_edid(edid, bucket, female, edid_grp),
            fid_override=fid_override,
            female=female,
            rnam_override=rnam)

    base = build(0, base_female, '', None, groups[0][2])
    if writer is None:
        return base

    for female in genders:
        for bucket in hair_buckets_for(source_fid):
            for tag, name_grp, rnam in groups:
                if bucket <= 0 and female == base_female and not tag:
                    continue                      # that is `base`
                vid = hair_variant_formid(writer, source_fid, bucket,
                                          female, base_female, tag)
                writer.add_record('HDPT', build(bucket, female, tag,
                                                name_grp, rnam, vid))
    return base


def _build_hdpt(rec: dict, *, model_override: str = '',
                tri_path: str = '', edid_override: str = '',
                fid_override: int = 0, female: bool = False,
                rnam_override: int = 0) -> bytes:
    """Pack one HDPT (Type 3 / Hair).

    Oblivion's HAIR is a head part in all but name: a model, an icon and a
    playable/gender flag byte.  Skyrim's HDPT wants the same information plus
    an explicit Type and a valid-races list.

    `tri_path` names the emitted Skyrim .tri (the SkinnyMorph slot the engine
    reads for head parts).  123 of the 204 vanilla hair HDPTs carry one.
    """
    subs = b''

    edid = edid_override or get_str(rec, 'EditorID')
    if edid:
        subs += pack_string_subrecord('EDID', edid)

    full = get_str(rec, 'FULL')
    if full:
        subs += pack_string_subrecord('FULL', full)

    model = model_override or get_str(rec, 'Model.MODL')
    if model:
        subs += pack_string_subrecord('MODL', _prefix_path(model))
        # MODT stub (version 2, no texture hashes) — the same form the GRAS
        # and CLMT converters emit.
        subs += pack_subrecord('MODT', struct.pack('<III', 2, 0, 0))

    # DATA — flags.  Each variant's mesh is fitted to ONE gender's head, so
    # the record is single-gender by construction (vanilla hair HDPTs are
    # Playable+Male x93 / Playable+Female x76 out of 204 for the same reason).
    flags = HDPT_FLAG_PLAYABLE | (
        HDPT_FLAG_FEMALE if female else HDPT_FLAG_MALE)
    subs += pack_uint8_subrecord('DATA', flags)

    # PNAM — Type.  Required; without it the CK rejects the record.
    subs += pack_uint32_subrecord('PNAM', HDPT_TYPE_HAIR)

    # NAM0/NAM1 — the .tri part.
    if tri_path:
        subs += pack_uint32_subrecord('NAM0', HDPT_PART_TRI)
        subs += pack_string_subrecord('NAM1', _prefix_path(tri_path))

    # RNAM — Valid Races.  Group variants carry their group's list; race-
    # named hair keys off the SOURCE record's EditorID as before.
    subs += pack_formid_subrecord(
        'RNAM', rnam_override or _hdpt_valid_races(get_str(rec, 'EditorID')))

    fid = fid_override or get_formid(rec, 'FormID')
    return pack_record('HDPT', fid, get_int(rec, 'RecordFlags'), subs)


def convert_CLAS(rec: dict, *, override_fid: int = 0, override_edid: str = '',
                 override_teaches: int = -1, override_maxtrain: int = -1) -> bytes:
    """CLAS — TES5 DATA is 36 bytes with Skyblivion skill-weight algorithm.

    The override_* parameters support Phase 0c trainer-class synthesis: Skyrim
    reads a trainer's skill/cap from the NPC's CLASS, but Oblivion stores them
    per-NPC in AIDT, so trainer NPCs get a clone of their own class with just
    Teaches/MaxTraining replaced (override_teaches is a TES5_SKILL_ORDER index).
    """
    subs = b''
    edid = override_edid or get_str(rec, 'EditorID')
    if edid:
        subs += pack_string_subrecord('EDID', edid)
    full = get_str(rec, 'FULL')
    if full:
        subs += pack_string_subrecord('FULL', full)
    desc = get_str(rec, 'DESC', '')
    subs += pack_string_subrecord('DESC', desc)

    # --- Skill weight algorithm (from Skyblivion) ---
    weights = {s: 0 for s in TES5_SKILL_ORDER}

    # 1) Specialization adds +2 to its skill group
    spec = get_int(rec, 'DATA.Specialization')
    SPEC_SKILLS = {
        0: ['OneHanded', 'TwoHanded', 'Block', 'Smithing', 'HeavyArmor', 'Marksman'],   # Combat
        1: ['Alteration', 'Conjuration', 'Destruction', 'Illusion', 'Restoration', 'Enchanting'],  # Magic
        2: ['Sneak', 'LightArmor', 'Lockpicking', 'Pickpocket', 'Speechcraft', 'Alchemy'],  # Stealth
    }
    for skill in SPEC_SKILLS.get(spec, []):
        weights[skill] = weights.get(skill, 0) + 2

    # 2) Two primary attributes: each attribute's associated skills get +1
    TES4_ATTR_NAMES = ['Strength', 'Intelligence', 'Willpower', 'Agility',
                       'Speed', 'Endurance', 'Personality', 'Luck']
    for attr_idx_key in ('DATA.PrimaryAttribute1', 'DATA.PrimaryAttribute2'):
        attr_idx = get_int(rec, attr_idx_key)
        if 0 <= attr_idx < len(TES4_ATTR_NAMES):
            attr_name = TES4_ATTR_NAMES[attr_idx]
            if attr_name == 'Luck':
                for s in TES5_SKILL_ORDER:
                    weights[s] = weights.get(s, 0) + 1
            else:
                for skill in ATTRIBUTE_SKILL_MAP.get(attr_name, []):
                    weights[skill] = weights.get(skill, 0) + 1

    # 3) Seven major skills: mapped to TES5 equivalents, each gets +3
    for i in range(7):
        tes4_skill = get_int(rec, f'DATA.MajorSkill[{i}]')
        tes5_name = TES4_SKILL_TO_TES5.get(tes4_skill)
        if tes5_name:
            weights[tes5_name] = weights.get(tes5_name, 0) + 3

    # Clamp to 0-255
    skill_weights = bytes(min(255, max(0, weights.get(s, 0))) for s in TES5_SKILL_ORDER)

    # Teaches: TES4 stores a 0-based index (0=Armorer, 1=Athletics, 2=Blade...).
    # TES4 actor values for skills start at 12, so add 12 to get the actor value
    # that TES4_SKILL_TO_TES5 uses as keys.
    teaches_tes4 = get_int(rec, 'DATA.Teaches') + 12
    teaches_tes5_name = TES4_SKILL_TO_TES5.get(teaches_tes4)
    if teaches_tes5_name and teaches_tes5_name in TES5_SKILL_ORDER:
        teaches = TES5_SKILL_ORDER.index(teaches_tes5_name)
    else:
        teaches = 0

    max_train = get_int(rec, 'DATA.MaxTraining')
    if override_teaches >= 0:
        teaches = override_teaches
    if override_maxtrain >= 0:
        max_train = override_maxtrain
    max_train = min(255, max(0, max_train))

    # TES5 CLAS DATA: Unknown(4) + Teaches(S8,1) + MaxTraining(U8,1) + SkillWeights(18) + Bleedout(float,4) + VoicePoints(U32,4) + AttrWeights(4×U8) = 36 bytes
    data = struct.pack('<I', 0xFFFC0000)  # Flags (vanilla default)
    data += struct.pack('<bB', teaches, max_train)
    data += skill_weights                  # 18 bytes
    data += struct.pack('<f', 0.1)        # Bleedout default (vanilla=0.1)
    data += struct.pack('<I', 0)          # Voice points (vanilla default)
    data += struct.pack('<4B', 1, 1, 1, 0)  # Attr weights: Health, Magicka, Stamina, Unknown
    subs += pack_subrecord('DATA', data)

    fid = override_fid or get_formid(rec, 'FormID')
    flags = 0 if override_fid else get_int(rec, 'RecordFlags')
    return pack_record('CLAS', fid, flags, subs)


# TES4 globals whose names collide with Skyrim engine globals. Script
# Skyrim already ships these at the SAME FormIDs Oblivion uses, so emitting our
# own copies would only create duplicate EditorIDs.  VMAD properties that name
# them are bound UNSHIFTED via constants.ENGINE_GLOBAL_FORMIDS — dropping the
# record is only half the contract, and for a long time it was the only half
# that existed, which left every such property bound to a nonexistent form.
# (An earlier comment here credited script_convert._GLOBAL_CANONICAL with
# fixing the references; it does not — it only canonicalizes the NAME.)
_ENGINE_GLOBALS = {'gamehour', 'gamedayspassed', 'gameday', 'gamemonth',
                   'gameyear', 'timescale'}


def convert_GLOB(rec: dict) -> bytes:
    subs = b''
    edid = get_str(rec, 'EditorID')
    if edid and edid.lower() in _ENGINE_GLOBALS:
        return b''
    if edid:
        subs += pack_string_subrecord('EDID', edid)
    type_char = get_str(rec, 'FNAM.Type', 'f')
    subs += pack_uint8_subrecord('FNAM', ord(type_char[0]) if type_char else ord('f'))
    value = get_float(rec, 'FLTV.Value')
    subs += pack_float_subrecord('FLTV', value)
    return pack_record('GLOB', get_formid(rec, 'FormID'), get_int(rec, 'RecordFlags'), subs)


def convert_GMST(rec: dict) -> bytes:
    subs = b''
    edid = get_str(rec, 'EditorID')
    if edid:
        subs += pack_string_subrecord('EDID', edid)
    value_str = get_str(rec, 'DATA.Value')
    if edid and edid.startswith('s'):
        subs += pack_string_subrecord('DATA', value_str)
    elif edid and edid.startswith('f'):
        try:
            subs += pack_float_subrecord('DATA', float(value_str))
        except ValueError:
            subs += pack_uint32_subrecord('DATA', 0)
    else:
        try:
            subs += pack_uint32_subrecord('DATA', int(value_str))
        except ValueError:
            subs += pack_uint32_subrecord('DATA', 0)
    return pack_record('GMST', get_formid(rec, 'FormID'), get_int(rec, 'RecordFlags'), subs)


# ---------------------------------------------------------------------------
# Leveled list converters
# ---------------------------------------------------------------------------


def _convert_leveled_list(rec: dict, tes5_sig: str) -> bytes:
    subs = b''
    edid = get_str(rec, 'EditorID')
    if edid:
        subs += pack_string_subrecord('EDID', edid)
    subs += pack_obnd()

    chance = get_int(rec, 'LVLD.ChanceNone')
    subs += pack_uint8_subrecord('LVLD', chance)
    flags = get_int(rec, 'LVLF.Flags')
    subs += pack_uint8_subrecord('LVLF', flags)

    # Entries. A null FormID must never be written (CK: "Unable to find
    # Leveled Object Form (00000000)"), so build the list first and derive
    # LLCT (U8 in TES5) from what survives. Negative TES4 counts (restock
    # semantics) are normalized like inventories.
    entries = []
    for i in range(get_int(rec, 'EntryCount')):
        fid = get_formid(rec, f'Entry[{i}].FormID')
        if not fid:
            continue
        level = get_int(rec, f'Entry[{i}].Level', 1)
        count = abs(get_int(rec, f'Entry[{i}].Count', 1)) or 1
        entries.append((max(1, level), fid, count))
    if entries:
        subs += pack_subrecord('LLCT', struct.pack('<B', min(len(entries), 255)))
    for level, fid, count in entries:
        # LVLO: Level(U16) + pad(U16) + FormID(U32) + Count(U16) + pad(U16) = 12 bytes
        subs += pack_subrecord('LVLO', struct.pack('<HxxIHxx', level, fid, count))

    return pack_record(tes5_sig, get_formid(rec, 'FormID'), get_int(rec, 'RecordFlags'), subs)


def convert_LVLI(rec: dict) -> bytes:
    return _convert_leveled_list(rec, 'LVLI')


def convert_LVLC(rec: dict) -> bytes:
    """LVLC → LVLN (Leveled NPC)."""
    return _convert_leveled_list(rec, 'LVLN')


def convert_LVSP(rec: dict) -> bytes:
    return _convert_leveled_list(rec, 'LVSP')


# ---------------------------------------------------------------------------
# World / Placement converters
# ---------------------------------------------------------------------------
