"""Generated creature RACE / ARMA / ARMO(skin) records.

Every CREA whose model folder was converted by the creature pipeline
(asset_convert/creature_pipeline.py — see export/<plugin>/creature_projects.json)
gets a GENERATED race chain instead of the old Skyrim-race aliasing
(skyrim_overrides.resolve_creature_race, kept only as a fallback for
creatures without a converted project). Humanoid NPC_ records are NOT
affected — they keep the Skyrim playable-race override system.

Record layouts are mirrored from a real Skyrim.esm dump of DogRace
(000131EE) / SkinDog (0004B2C9) / NakedDogAA (0004B2CA):

  RACE: EDID FULL DESC WNAM BOD2 KSIZ KWDA DATA(164) MNAM ANAM MODT FNAM
        ANAM MODT MTNM*5 VTCK PNAM UNAM [ATKD ATKE]* NAM1 MNAM INDX MODL
        MODT FNAM INDX MODL MODT GNAM NAM3 MNAM MODL MODT FNAM MODL MODT
        NAM4 NAM5 ONAM LNAM NAME*32 VNAM QNAM UNES
  ARMA: EDID BOD2 RNAM DNAM(12) MOD2
  ARMO: EDID OBND BOD2 RNAM DESC MODL* DATA DNAM   (flags=4 non-playable)

DATA(164) template = DogRace values with per-creature patches at
offset 36 (starting health), 40 (magicka), 44 (stamina), 96 (unarmed
damage) and 100 (unarmed reach). One race is created per unique
(model folder, body-part set) so e.g. dog and wolf — which share one
folder/project but list different NIFZ parts — get separate races that
reference the same generated behavior project.

Deliberate v1 simplifications (documented in docs/creature_conversion.md):
GNAM points at the vanilla canine body-part data (bone names won't match
the Oblivion skeleton — hits still register, dismember targeting is off),
and ARMA has no footstep SNDD yet.
"""

import json
import os
import struct

from .writer import (pack_record, pack_subrecord, pack_string_subrecord,
                     pack_formid_subrecord, pack_obnd)
from .text_reader import get_formid, get_int, get_str

# GMST fNPCHealthLevelBonus (Skyrim.esm) — health the engine grants per level
# above 1. Defined here rather than imported from record_types.actors because
# actors.py imports this module, and a module-level import back would cycle.
TES5_HEALTH_LEVEL_BONUS = 5

# ---------------------------------------------------------------------------
# Vanilla template constants (Skyrim.esm DogRace chain, byte-verified)
# ---------------------------------------------------------------------------

_RACE_DATA_TEMPLATE = bytes.fromhex(
    '0f23ff00ff00ff00ff00ff00ff0000000000803f0000803f0000803f0000803f'
    '4889300000002041000000000000a041000048430000803f0000803f0000803f'
    '01000000ffffffffffffffff00000000ffffffff000000000000000000002041'
    '0000004100008042ffffffff00000000000000000000803e0000a04000000000'
    '7fea7dc20000000000000000000048c2000000000000824200000000000096c3'
    '00000000')
_MODT = bytes.fromhex('020000000000000000000000')
_ARMA_DNAM = bytes.fromhex('000000000000001100000000')
# MOVT SPED layout: 11 floats — leftWalk, leftRun, rightWalk, rightRun,
# forwardWalk, forwardRun, backWalk, backRun, rotateInPlaceWalk,
# rotateInPlaceRun, rotateWhileMovingRun (rotate in RADIANS/sec — vanilla
# dog: 0,0,0,0, 74.54, 500.14, 74.54, 74.54, π, 3π/2, 3π/2, both dog MOVTs
# byte-identical).  Per-creature forward/back speeds come from the clip
# root-motion endpoints (proj['speeds'], ck-cmd calculateMOVTs method):
# commanded speed must equal the animation's natural speed or actors slide/
# moonwalk (2026-07-09 "far too fast" report — every creature was shipping
# the vanilla dog's 500 u/s run).  No run clip → run = walk speed (the
# creature simply can't move faster than its only gait).
_DOG_WALK, _DOG_RUN = 74.54, 500.14
_ROT_WALK, _ROT_RUN = 3.14159265, 4.71238898
_MOVT_INAM = bytes.fromhex('FFFF7F7FFFFF7F7FFFFF7F7F')
# Oblivion creature ground speed comes from the Speed ATTRIBUTE, not the
# animation: walk = fMoveCreatureWalkMin + (Max-Min) x Speed/100, run =
# walk x fMoveRunMult (GMST values verified from the Oblivion.esm export:
# 5.0 / 300.0 / 3.0).  Clip-natural MOVT speeds made fast predators crawl
# ("mountain lion runs in slow motion", 2026-07-16): a Speed-50 lion ran
# 457 u/s in Oblivion vs its gallop clip's natural 200 u/s.  Commanded
# speed = max(natural, formula) capped at the parametric blend's top
# anchor (walk x1.4 / run x2.0 rate-scaled children), so creatures only
# speed UP toward Oblivion values and the animation rate always tracks.
_CREA_WALK_MIN, _CREA_WALK_MAX, _RUN_MULT = 5.0, 300.0, 3.0


def _movt_sped(speeds: dict, attr_speed: int = 0) -> bytes:
    walk_nat = speeds.get('walk') or _DOG_WALK
    run_nat = speeds.get('run') or walk_nat
    back = speeds.get('back') or walk_nat * 0.8
    walk, run = walk_nat, run_nat
    if attr_speed:
        f_walk = (_CREA_WALK_MIN
                  + (_CREA_WALK_MAX - _CREA_WALK_MIN) * attr_speed / 100.0)
        walk = min(max(walk_nat, f_walk), 1.4 * walk_nat)
        run = min(max(run_nat, f_walk * _RUN_MULT), 2.0 * run_nat)
    return struct.pack('<11f', 0.0, 0.0, 0.0, 0.0, walk, max(run, walk),
                       back, back, _ROT_WALK, _ROT_RUN, _ROT_RUN)
_MTNM_CODES = (b'WALK', b'RUN1', b'SNEK', b'BLDO', b'SWIM')
_EGT_MALE = 'Actors\\Character\\UpperBodyHumanMale.egt'
_EGT_FEMALE = 'Actors\\Character\\UpperBodyHumanFemale.egt'
_GNAM_BPTD = 0x0004FBF5      # canine body part data (see module docstring)
_NAM4_IMPACT_MAT = 0x0005A28F
_NAM5_IMPACT_SET = 0x000A956F
_ONAM_OPEN_SND = 0x000A5013
_LNAM_CLOSE_SND = 0x000A5014
_VNAM_EQUIP_FLAGS = 0xFFFFE001
_QNAM_UNARMED = 0x00013F42
_KW_ANIMAL = 0x00013798
_KW_UNDEAD = 0x00013796
_KW_DAEDRA = 0x00013797
_KW_CREATURE = 0x00013795

# TES4 creature folder → actor-type keyword set
_FOLDER_KEYWORDS = {
    'bear': [_KW_ANIMAL], 'boar': [_KW_ANIMAL], 'deer': [_KW_ANIMAL],
    'dog': [_KW_ANIMAL], 'horse': [_KW_ANIMAL], 'mountainlion': [_KW_ANIMAL],
    'mudcrab': [_KW_ANIMAL], 'rat': [_KW_ANIMAL], 'sheep': [_KW_ANIMAL],
    'slaughterfish': [_KW_ANIMAL],
    'skeleton': [_KW_UNDEAD, _KW_CREATURE],
    'zombie': [_KW_UNDEAD, _KW_CREATURE],
    'lich': [_KW_UNDEAD, _KW_CREATURE],
    'ghost': [_KW_UNDEAD, _KW_CREATURE],
    'wraith': [_KW_UNDEAD, _KW_CREATURE],
    'clannfear': [_KW_DAEDRA, _KW_CREATURE],
    'daedroth': [_KW_DAEDRA, _KW_CREATURE],
    'scamp': [_KW_DAEDRA, _KW_CREATURE],
    'spiderdaedra': [_KW_DAEDRA, _KW_CREATURE],
    'xivilai': [_KW_DAEDRA, _KW_CREATURE],
    'flameatronach': [_KW_DAEDRA, _KW_CREATURE],
    'frostatronach': [_KW_DAEDRA, _KW_CREATURE],
    'stormatronach': [_KW_DAEDRA, _KW_CREATURE],
    'mehrunesdagon': [_KW_DAEDRA, _KW_CREATURE],
}

# crea_fid_low24 → (race_fid, folder) — consumed by convert_CREA
_CREA_RACE_MAP = {}
# folder → project summary (attacks etc.) for anything else that needs it
_PROJECTS = {}


def get_creature_race(fid_low24: int):
    """Generated race FormID for a converted CREA, or None (→ fallback to
    resolve_creature_race aliasing)."""
    entry = _CREA_RACE_MAP.get(fid_low24)
    return entry[0] if entry else None


# A generated creature RACE is SHARED by every CREA that maps to the same mesh
# folder + body set (`made[key]` in build_creature_races), so it cannot carry any
# one creature's health. It therefore uses the same flat base every vanilla
# playable race uses, and each creature's whole pool lives in its own
# per-record ACBS.HealthOffset. An earlier version wrote the first creature's
# pool into the shared race and left the offset at 0, which gave every other
# creature sharing that race the wrong health (measured: 345 Nehrim actors, e.g.
# all three StartCelleTroll variants at 15 HP instead of 450).
CREATURE_RACE_BASE_HEALTH = 50.0


def creature_race_health(rec: dict) -> float:
    """Starting Health for a CREA's generated RACE — a flat shared base.

    See CREATURE_RACE_BASE_HEALTH: the race is shared, so per-creature health
    must not go here. `creature_health_offset` carries it instead.
    """
    return CREATURE_RACE_BASE_HEALTH


def creature_health_offset(rec: dict) -> int:
    """ACBS.HealthOffset for a CREA — carries the creature's whole TES4 pool.

    Solved so the engine's own sum,
        RACE.StartingHealth + HealthOffset + (Level-1)*fNPCHealthLevelBonus,
    lands exactly on TES4 DATA.Health. This is per-record, so creatures sharing a
    generated race still each get their own health.

    A TES4 pool of 0 is pinned dead: those are intentional corpse props (52 in
    Nehrim, 45 in Oblivion) that ship dead in Oblivion too, including the
    PC-Level-Mult ones whose runtime level term would otherwise revive them.
    """
    health = get_int(rec, 'DATA.Health', 50)
    if health <= 0:
        return -32768
    base = CREATURE_RACE_BASE_HEALTH
    if get_int(rec, 'ACBS.Flags') & 0x80:
        # PC Level Mult: the level term is a runtime multiplier, not authored.
        return max(-32768, min(int(health - base), 32767))
    level_term = (creature_capped_level(rec) - 1) * TES5_HEALTH_LEVEL_BONUS
    return max(-32768, min(int(health - base - level_term), 32767))


def creature_capped_level(rec: dict) -> int:
    """ACBS.Level for a CREA, lowered when its level term is unrepresentable.

    The engine's per-level bonus is what makes a very high level unusable: at
    level 32,767 the term is 163,830, which no int16 offset can cancel. Such a
    level is meaningless in Skyrim anyway (vanilla's highest NPC is level 100),
    so it is reduced to the highest value whose term the offset CAN cancel,
    keeping the resulting health pool exactly faithful.
    """
    level = max(1, min(get_int(rec, 'ACBS.Level', 1), 65535))
    if get_int(rec, 'ACBS.Flags') & 0x80:
        return 1000
    health = get_int(rec, 'DATA.Health', 50)
    surplus = health - CREATURE_RACE_BASE_HEALTH
    # Lower bound: the offset must be able to cancel the level term.
    max_level = int((surplus + 32768) // TES5_HEALTH_LEVEL_BONUS) + 1
    level = max(1, min(level, max_level))
    if surplus > 32767:
        # Too much health for the int16 offset alone (Nehrim ships 15 such
        # creatures, up to 65,200 HP). RAISE the level so its per-level bonus —
        # the engine's own mechanism — absorbs the surplus, exactly as
        # _health_and_level does for NPCs. Rounded up so the remainder still
        # fits, and capped at the U16 field.
        need = surplus - 32767
        level = max(level, min(65535, -(-need // TES5_HEALTH_LEVEL_BONUS) + 1))
    # ACBS.Level is a U16 struct field. CREATURE_RACE_BASE_HEALTH is a float
    # (it is packed as <f into RACE DATA), so surplus — and every level derived
    # from it above — is a float too, which struct.pack rejects with "required
    # argument is not an integer". Only creatures needing the raise-level branch
    # (health surplus > 32767) ever hit it, which is why it went unnoticed.
    return int(level)


def _race_data(rec: dict) -> bytes:
    """The 164-byte RACE DATA: DogRace template with CREA stat patches.

    Starting Health is the flat CREATURE_RACE_BASE_HEALTH, NOT this creature's
    pool: the race is shared by every CREA with the same mesh folder and body
    set, so per-creature health belongs in ACBS.HealthOffset instead (see
    creature_health_offset).
    """
    data = bytearray(_RACE_DATA_TEMPLATE)
    struct.pack_into('<f', data, 36, float(creature_race_health(rec)))
    struct.pack_into('<f', data, 40,
                     float(get_int(rec, 'ACBS.SpellPoints', 0)))
    struct.pack_into('<f', data, 44, float(get_int(rec, 'ACBS.Fatigue', 100)))
    struct.pack_into('<f', data, 96,
                     float(max(1, get_int(rec, 'DATA.AttackDamage', 5))))
    reach = get_int(rec, 'RNAM.AttackReach', 64) or 64
    struct.pack_into('<f', data, 100, float(reach))
    return bytes(data)


def _atkd(damage_mult: float = 1.0) -> bytes:
    """44-byte attack data: vanilla dog Attack1 values."""
    return struct.pack('<ffIIfffIfff',
                       damage_mult, 1.0, 0, 0, 0.0, 35.0, 0.75, 0, 0.0, 0.0,
                       1.0)


def _build_race(writer, rec, folder: str, bodies: list, proj: dict,
                race_fid: int, skin_fid: int, edid: str, full: str) -> None:
    subs = b''
    subs += pack_string_subrecord('EDID', edid)
    subs += pack_string_subrecord('FULL', full)
    subs += pack_subrecord('DESC', b'\x00')
    subs += pack_formid_subrecord('WNAM', skin_fid)
    subs += pack_subrecord('BOD2', struct.pack('<II', 0, 2))
    keywords = _FOLDER_KEYWORDS.get(folder, [_KW_CREATURE])
    subs += pack_subrecord('KSIZ', struct.pack('<I', len(keywords)))
    subs += pack_subrecord('KWDA',
                           b''.join(struct.pack('<I', k) for k in keywords))
    subs += pack_subrecord('DATA', _race_data(rec))

    skeleton = proj['skeleton_nif']
    for marker in ('MNAM', 'FNAM'):
        subs += pack_subrecord(marker, b'')
        subs += pack_string_subrecord('ANAM', skeleton)
        subs += pack_subrecord('MODT', _MODT)
    for code in _MTNM_CODES:
        subs += pack_subrecord('MTNM', code)
    # VTCK male+female — vanilla creature races always fill BOTH slots
    # (DogRace: CrDogVoice x2); a null slot draws a CK "Could not find
    # male/female voice type" warning per race. The actors carry their own
    # VTCK, so this is only the race-level fallback.
    from .record_types.actors import resolve_actor_voice
    subs += pack_subrecord('VTCK', struct.pack(
        '<II', resolve_actor_voice(rec, 'Male'),
        resolve_actor_voice(rec, 'Female')))
    subs += pack_subrecord('PNAM', struct.pack('<f', 5.0))
    subs += pack_subrecord('UNAM', struct.pack('<f', 3.0))

    for event, _clip in proj.get('attacks', []):
        subs += pack_subrecord('ATKD', _atkd())
        subs += pack_string_subrecord('ATKE', event)

    subs += pack_subrecord('NAM1', b'')
    for marker, egt in (('MNAM', _EGT_MALE), ('FNAM', _EGT_FEMALE)):
        subs += pack_subrecord(marker, b'')
        subs += pack_subrecord('INDX', struct.pack('<I', 0))
        subs += pack_string_subrecord('MODL', egt)
        subs += pack_subrecord('MODT', _MODT)
    subs += pack_formid_subrecord('GNAM', _GNAM_BPTD)

    subs += pack_subrecord('NAM3', b'')
    for marker in ('MNAM', 'FNAM'):
        subs += pack_subrecord(marker, b'')
        subs += pack_string_subrecord('MODL', proj['project_hkx'])
        subs += pack_subrecord('MODT', _MODT)

    subs += pack_formid_subrecord('NAM4', _NAM4_IMPACT_MAT)
    subs += pack_formid_subrecord('NAM5', _NAM5_IMPACT_SET)
    subs += pack_formid_subrecord('ONAM', _ONAM_OPEN_SND)
    subs += pack_formid_subrecord('LNAM', _LNAM_CLOSE_SND)
    # Biped object names: vanilla creatures name ONLY slot 2 ('BODY') and ship
    # the whole animal (body+head+eyes+tail) as a single skinned NIF on that
    # one BODY-slot ARMA (census: DogRace names 'BODY' and nothing else).  The
    # creature pipeline merges all Oblivion body parts into one <creature>.nif
    # for exactly this reason, so a single BODY slot is all that's needed.
    for slot in range(32):
        subs += pack_subrecord('NAME', b'BODY\x00' if slot == 2 else b'\x00')
    subs += pack_subrecord('VNAM', struct.pack('<I', _VNAM_EQUIP_FLAGS))
    subs += pack_formid_subrecord('QNAM', _QNAM_UNARMED)
    subs += pack_formid_subrecord('UNES', _QNAM_UNARMED)

    writer.add_record('RACE', pack_record('RACE', race_fid, 0, subs))


def _build_movts(writer, folder: str, proj: dict,
                 attr_speed: int = 0) -> None:
    """Generated MOVT records for one creature project (once per folder).

    The engine gives an actor movement types by matching the behavior
    graph's `iState_<X>` variables against MOVT records with MNAM == <X>
    (vanilla: dogbehavior iState_DogDefault/iState_DogRun ↔ Dog_Default_MT/
    Dog_Run_MT; ck-cmd's RetargetCreature clones MOVTs the same way). A
    graph whose iState_* names have no MOVT records — or a plugin whose
    MOVTs have no graph variables — leaves the actor unable to move AT ALL
    (no AI movement, no `tc` control, no locomotion events: the 2026-07-09
    stuck-in-idle root cause). The names come from the creature pipeline
    manifest so graph and records agree by construction (like ATKE)."""
    names = proj.get('movement_types') or [f'TES4{folder}Default',
                                           f'TES4{folder}Run']
    sped = _movt_sped(proj.get('speeds') or {}, attr_speed)
    for mnam in names:
        subs = pack_string_subrecord('EDID', f'{mnam}_MT')
        subs += pack_string_subrecord('MNAM', mnam)
        subs += pack_subrecord('SPED', sped)
        subs += pack_subrecord('INAM', _MOVT_INAM)
        writer.add_record('MOVT', pack_record('MOVT', writer.alloc_formid(),
                                              0, subs))


def _build_skin(writer, folder: str, bodies: list, race_fid: int,
                skin_fid: int, edid_base: str) -> None:
    """Single BODY-slot ARMA (the merged whole-animal NIF) + its skin ARMO.

    Vanilla creatures use ONE ARMA on slot BODY (0x4); the creature pipeline
    merges every Oblivion body part into one <creature>.nif so a single ARMA
    covers the whole animal (see merge_creature_body / DogRace census)."""
    body = bodies[0]
    arma_fid = writer.alloc_formid()
    stem = os.path.splitext(body)[0]
    subs = b''
    subs += pack_string_subrecord('EDID', f'TES4{edid_base}{stem}AA')
    subs += pack_subrecord('BOD2', struct.pack('<II', 0x4, 2))
    subs += pack_formid_subrecord('RNAM', race_fid)
    subs += pack_subrecord('DNAM', _ARMA_DNAM)
    subs += pack_string_subrecord('MOD2', f'Actors\\TES4\\{folder}\\{body}')
    writer.add_record('ARMA', pack_record('ARMA', arma_fid, 0, subs))

    subs = b''
    subs += pack_string_subrecord('EDID', f'TES4Skin{edid_base}')
    subs += pack_obnd(0, 0, 0, 0, 0, 0)
    subs += pack_subrecord('BOD2', struct.pack('<II', 0x4, 2))
    subs += pack_formid_subrecord('RNAM', race_fid)
    subs += pack_subrecord('DESC', b'\x00')
    subs += pack_formid_subrecord('MODL', arma_fid)
    subs += pack_subrecord('DATA', struct.pack('<If', 0, 0.0))
    subs += pack_subrecord('DNAM', struct.pack('<f', 0.0))
    # flags=4: non-playable (vanilla SkinDog)
    writer.add_record('ARMO', pack_record('ARMO', skin_fid, 4, subs))


def _load_projects(export_dir: str) -> dict:
    """This plugin's creature projects, with its MASTERS' projects merged in
    underneath.

    A plugin with a TES4 master re-uses the master's creature folders wholesale
    (Morrowind_ob.esm places 86 CREA records on Oblivion.esm's rat/skeleton/
    goblin/... meshes, which its own BSA never ships, so the creatures step
    extracts no folder for them and its creature_projects.json has no entry).
    Without the master's projects those CREA records fell through to
    resolve_creature_race and shipped as BASE SKYRIM creatures — a Skyrim
    frostbite spider or a Nord standing in for the converted Oblivion actor.

    The master's generated behavior project, skeleton and merged body NIFs all
    live under ITS output dir and are loaded by path at runtime, so pointing
    this plugin's generated RACE chain at them is correct: the assets exist and
    are shared, exactly as the source plugin intended. Own projects win on
    conflict — this plugin's own conversion of a folder is the authoritative
    one for the records it ships."""
    own_path = os.path.join(export_dir, 'creature_projects.json')
    own = {}
    if os.path.exists(own_path):
        with open(own_path, encoding='utf-8') as f:
            own = json.load(f)

    header = os.path.join(export_dir, '_HEADER.txt')
    if not os.path.isfile(header):
        return own
    names = []
    with open(header, 'r', encoding='utf-8') as f:
        for line in f:
            if line.startswith('Master['):
                _, _, val = line.partition('=')
                names.append(val.strip())

    root = os.path.dirname(os.path.normpath(export_dir))
    merged, inherited = {}, 0
    for name in names:
        mpath = os.path.join(root, name, 'creature_projects.json')
        if not os.path.exists(mpath):
            continue
        with open(mpath, encoding='utf-8') as f:
            for folder, proj in json.load(f).items():
                if folder not in own and folder not in merged:
                    merged[folder] = proj
                    inherited += 1
    if inherited:
        print(f'  Creature projects: inherited {inherited} from master(s) '
              f'{", ".join(names)} (own: {len(own)})')
    merged.update(own)
    return merged


def build_creature_races(by_type: dict, writer, export_dir: str) -> None:
    """Phase 0f: one generated RACE + skin ARMO/ARMA per unique
    (creature folder, body-part set) among CREA records with a converted
    project. Populates the crea→race map used by convert_CREA."""
    global _PROJECTS
    _CREA_RACE_MAP.clear()

    _PROJECTS = _load_projects(export_dir)
    if not _PROJECTS:
        print('  Creature projects: none (creature_projects.json missing — '
              'run the creatures step); CREA falls back to race aliasing')
        return

    def _folder_of(rec):
        model = (get_str(rec, 'Model.MODL') or '').replace('/', '\\')
        parts = [p for p in model.lower().split('\\') if p]
        # "Creatures\Dog\Skeleton.NIF" → folder "dog"
        return parts[-2] if len(parts) >= 2 else ''

    # per-folder Speed ATTRIBUTE for the MOVT formula (_movt_sped): the MAX
    # across the folder's records — the combat variants are the fast ones
    # and dead/quest-prop variants (Speed ~9-12) never move anyway.  One
    # value per folder because all races sharing a behavior project share
    # its iState_* movement-type names, hence the same MOVT records.
    folder_speed = {}
    for rec in by_type.get('CREA', []):
        f = _folder_of(rec)
        if f in _PROJECTS:
            folder_speed[f] = max(folder_speed.get(f, 0),
                                  get_int(rec, 'DATA.Speed', 0))

    made = {}
    movt_folders = set()
    n_races = 0
    for rec in by_type.get('CREA', []):
        folder = _folder_of(rec)
        proj = _PROJECTS.get(folder)
        if proj is None:
            continue
        fid = get_formid(rec, 'FormID') & 0x00FFFFFF

        # The creature pipeline merged each CREA's NIFZ part set into ONE
        # whole-animal NIF and ships the exact set→file mapping as body_map
        # ('|'.join(lowercase nifz) → merged filename), so dog/wolf/
        # skeletal-hound (same folder) each point at the right mesh without
        # re-deriving names here.
        nifz = [(get_str(rec, f'NIFZ[{i}]') or '').lower()
                for i in range(get_int(rec, 'NIFZCount', 0))]
        nifz = [p for p in nifz if p.endswith('.nif')]
        merged = (proj.get('body_map') or {}).get('|'.join(nifz))
        if merged:
            bodies = [merged]
        elif proj['bodies']:
            bodies = [proj['bodies'][0]]   # fallback: folder's first merged NIF
        else:
            continue

        if folder not in movt_folders:
            _build_movts(writer, folder, proj, folder_speed.get(folder, 0))
            # engine-action → graph-event routing (IDLE records) — without
            # these the engine never sends the graph ANY events and the
            # actor plays idle forever while sliding around
            from .creature_idles import build_creature_idles
            build_creature_idles(writer, folder, proj)
            movt_folders.add(folder)

        key = (folder, tuple(bodies))
        if key not in made:
            race_fid = writer.alloc_formid()
            skin_fid = writer.alloc_formid()
            edid = get_str(rec, 'EditorID') or folder
            edid_base = ''.join(c for c in edid if c.isalnum()) or folder
            full = get_str(rec, 'FULL') or edid
            _build_race(writer, rec, folder, bodies, proj,
                        race_fid, skin_fid, f'TES4{edid_base}Race', full)
            _build_skin(writer, folder, bodies, race_fid, skin_fid,
                        edid_base)
            made[key] = race_fid
            n_races += 1
        _CREA_RACE_MAP[fid] = (made[key], folder)

    print(f'  Creature races: {n_races} generated '
          f'({len(_CREA_RACE_MAP)} CREA records mapped, '
          f'{len(_PROJECTS)} converted projects)')
