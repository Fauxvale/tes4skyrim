"""Attach converted TES4 object scripts (SCPT via SCRI) to their records as VMAD.

TES4 object records (ACTI, FLOR, CONT, DOOR, FURN, MISC, KEYM, …) reference a
SCPT record through the ``SCRI`` field.  ``script_convert`` already converts each
such SCPT into a full ``TES4_<EditorID>.psc`` extending ObjectReference/Actor with
its OnActivate / OnLoad / GameMode(→OnUpdate) event handlers, and the pipeline
compiles those to ``.pex``.  What was missing is the binding: without a VMAD
subrecord naming that script on the object record, the engine never attaches it,
so activating an altar showed no message and gave no effect, a nirnroot never
stopped its sound, etc.

This module builds that binding once, up front:

  build_object_script_plan(by_type, xref, fid_to_edid) -> {record_fid_int: VMADinfo}

Each object record's plan carries the script name plus the FormID bindings for its
Object-typed properties (the SCPT's ``ref`` variables and every record the script
names by EditorID), resolved to the OUTPUT plugin's FormID space.  The record
converters then splice the VMAD in right after EDID (Skyrim order: EDID VMAD OBND …).
"""

import re

from script_convert.converter import ScriptConverter
from script_convert.constants import (_safe_property_name, papyrus_script_name,
                                      resolve_property_formid,
                                      PLAYER_ALIAS_EXTENDS)
from script_convert.pipeline import build_vmad_object_script
from .text_reader import (parse_export_file, get_formid_index_offset,
                          remap_formid, unescape_value)
from .constants import ENGINE_GLOBAL_FORMIDS

# Papyrus property types that are literal-valued (not bound to a FormID).
_VALUE_TYPES = {'Int', 'Float', 'Bool'}

_PLAYER_FORMID = 0x14

# Record types that carry a SCRI in TES4 and become plain object scripts
# in Skyrim.  NPC_/CREA are included: TES4 attaches actor scripts to the BASE
# record, and Skyrim instantiates a base record's VMAD scripts on every placed
# reference — without this, script-typed properties (e.g.
# TES4_FGC01PiranusScript) can never cast and read as None in-game.
# QUST/INFO have their own script pipelines (quest SCRI is attached by
# dialog_converter's QUST VMAD; INFO result scripts become TIF fragments).
SCRIPTABLE_TYPES = {
    'ACTI', 'FLOR', 'CONT', 'DOOR', 'FURN', 'MISC', 'KEYM', 'LIGH',
    'STAT', 'BOOK', 'WEAP', 'ARMO', 'CLOT', 'AMMO', 'INGR', 'ALCH',
    'APPA', 'SLGM', 'SGST', 'SBSP', 'NPC_', 'CREA',
}

# Output (TES5) signatures whose xEdit record definition actually lists a VMAD
# subrecord.  Attaching a VMAD to any other record makes xEdit flag it as an
# "unexpected (or out of order) subrecord" — e.g. ALCH/SLGM/STAT/AMMO have no
# VMAD in the Skyrim def, so a converted object script is dropped for those.
# Sourced from wbDefinitionsTES5.pas (records containing a plain `wbVMAD,`).
VMAD_SUPPORTED_OUTPUT_TYPES = {
    'ACTI', 'APPA', 'ARMO', 'BOOK', 'CONT', 'DOOR', 'EXPL', 'FLOR', 'FURN',
    'INGR', 'KEYM', 'LIGH', 'MGEF', 'MISC', 'NPC_', 'RACE', 'TACT', 'TREE',
    'WEAP',
}

# record FormID (int, output space) -> packed VMAD bytes.  Filled by
# build_object_script_plan(); read by the record converters via get_object_vmad().
_OBJECT_VMAD: dict[int, bytes] = {}

# QUST FormID (int, output space) -> (script_name, {prop: formid}).  Filled by
# build_quest_script_plan(); consumed by dialog_converter.convert_QUST, which
# splices the script into the quest's VMAD alongside the QF fragment script.
_QUEST_SCRIPT: dict[int, tuple] = {}

# [(script_name, {prop: formid}), ...] for TES4 scripts attached to the PLAYER
# BASE record (NPC_ 0x00000007).  Filled by build_player_alias_plan(); consumed
# by dialog_converter._make_player_script_quest, which hosts them on a
# start-game-enabled quest's PlayerRef reference alias.  See
# script_convert.constants.PLAYER_ALIAS_EXTENDS for why the base record itself
# cannot carry them.
_PLAYER_ALIAS_SCRIPTS: list[tuple] = []

# TES4 FormID of the player's base NPC_ record.  Oblivion and Skyrim both
# hardcode it; a plugin scripting the player attaches its SCPT here.
_PLAYER_BASE_FORMID = 0x07


def get_object_vmad(record_fid: int) -> bytes:
    """Packed VMAD subrecord for a record's attached object script (b'' if none)."""
    return _OBJECT_VMAD.get(record_fid, b'')


def get_quest_script(record_fid: int):
    """(script_name, props) for a QUST's converted TES4 quest script, or None."""
    return _QUEST_SCRIPT.get(record_fid)


def get_player_alias_scripts() -> list:
    """[(script_name, props)] to host on a quest's PlayerRef alias."""
    return list(_PLAYER_ALIAS_SCRIPTS)


def _remap(fid: int, offset: int) -> int:
    """Remap a TES4 FormID into the output plugin space.

    Mirrors text_reader.get_formid (engine-hardcoded Player 0x14 stays put);
    overrides keep their master's shifted index rather than becoming ours.
    """
    return remap_formid(fid, offset)


def _collect_scpts(by_type: dict, xref) -> dict:
    """SCPT FormID -> (EditorID, SCTX source, extends class)."""
    scpt_by_fid: dict[str, tuple] = {}
    for rec in by_type.get('SCPT', []):
        fid = rec.get('FormID', '')
        sctx = rec.get('SCTX', '')
        if not fid or not sctx or not sctx.strip():
            continue
        scpt_by_fid[fid] = (rec.get('EditorID', ''), sctx,
                            xref.get_extends_class(fid))
    return scpt_by_fid


def build_quest_script_plan(by_type: dict, xref, fid_to_edid: dict) -> int:
    """Resolve every QUST's attached TES4 quest script (SCRI) to a
    (script_name, bound-properties) plan for convert_QUST to splice into the
    quest VMAD.  Without this the converted TES4_<QuestScript>.pex is never
    attached, so its GameMode logic never runs and every property another
    script declares with that type (e.g. TES4_FGQuestTrack) fails to cast and
    reads None in-game.

    Returns the number of quests with a script plan.
    """
    _QUEST_SCRIPT.clear()
    offset = get_formid_index_offset()
    scpt_by_fid = _collect_scpts(by_type, xref)

    for rec in by_type.get('QUST', []):
        scri = rec.get('SCRI', '')
        if not scri or scri not in scpt_by_fid:
            continue
        rec_fid_str = rec.get('FormID', '')
        if not rec_fid_str:
            continue
        try:
            rec_fid = _remap(int(rec_fid_str, 16), offset)
        except ValueError:
            continue
        edid, sctx, extends = scpt_by_fid[scri]
        script_name = papyrus_script_name(edid or f'Script_{scri}')
        try:
            props = _resolve_props(sctx, edid, extends, xref, fid_to_edid, offset)
        except Exception:
            props = {}
        _QUEST_SCRIPT[rec_fid] = (script_name, props)

    return len(_QUEST_SCRIPT)


# TES4 SCPT FormID (raw hex string) -> packed VMAD for a Script-archetype MGEF.
# Filled by build_magic_effect_script_plan(); read by record_types/magic.py
# when it emits the per-script SEFF variants.
_MAGIC_EFFECT_VMAD: dict[str, bytes] = {}


def get_magic_effect_vmad(scpt_fid: str) -> bytes:
    """Packed VMAD for a TES4 magic-effect script (b'' when there is none)."""
    return _MAGIC_EFFECT_VMAD.get(scpt_fid, b'')


def build_magic_effect_script_plan(by_type: dict, xref, fid_to_edid: dict) -> int:
    """Resolve every magic-effect script (SCHR.Type 256) to a packed VMAD.

    A TES4 `SEFF` effect names its script per EFFECT — `ScriptEffect[i].FormID`
    on the owning SPEL/ENCH/ALCH — not on the MGEF, so the same SEFF record is
    a different script on every item that uses it.  Skyrim moved the script
    onto the MGEF (archetype 1 Script + a VMAD carrying an ActiveMagicEffect),
    so record_types/magic.py emits one MGEF per distinct script and needs the
    VMAD for each here, where the property-resolution machinery lives.

    Returns the number of scripts that produced a VMAD.
    """
    _MAGIC_EFFECT_VMAD.clear()
    offset = get_formid_index_offset()
    scpt_by_fid = _collect_scpts(by_type, xref)

    # Only the scripts actually referenced by an effect are worth resolving —
    # _resolve_props re-runs the whole converter per script.
    wanted = set()
    for sig in ('SPEL', 'ENCH', 'ALCH', 'INGR', 'SGST'):
        for rec in by_type.get(sig, []):
            for i in range(int(rec.get('EffectCount', 0) or 0)):
                fid = rec.get(f'ScriptEffect[{i}].FormID', '')
                if fid and fid in scpt_by_fid:
                    wanted.add(fid)

    from .writer import pack_subrecord
    for scpt_fid in sorted(wanted):
        edid, sctx, extends = scpt_by_fid[scpt_fid]
        script_name = papyrus_script_name(edid or f'Script_{scpt_fid}')
        try:
            props = _resolve_props(sctx, edid, extends, xref, fid_to_edid, offset)
        except Exception:
            props = {}
        _MAGIC_EFFECT_VMAD[scpt_fid] = pack_subrecord(
            'VMAD', build_vmad_object_script(script_name, props))

    return len(_MAGIC_EFFECT_VMAD)


def build_object_script_plan(by_type: dict, xref, fid_to_edid: dict) -> int:
    """Compute and cache the VMAD for every object record with an attached SCPT.

    by_type: {signature: [record dicts]} from the export.
    xref: CrossRefGraph (already populated with edid/formid/record_type +
          script_all_vars/ref_as_int via build_ref_as_int_map).
    fid_to_edid: {raw_formid_int: editor_id} for resolving property targets.

    Returns the number of records that received a script VMAD.
    """
    _OBJECT_VMAD.clear()
    offset = get_formid_index_offset()
    scpt_by_fid = _collect_scpts(by_type, xref)

    from .constants import TYPE_MAP

    # Property resolution runs a full ScriptConverter pass over the script
    # source — the dominant cost here — and depends only on the SCPT, not the
    # record it is attached to. Many records share one script (3297 scripted
    # records / 2090 unique scripts in Oblivion.esm), so memoise per SCRI.
    props_memo: dict[str, dict] = {}

    count = 0
    for sig in SCRIPTABLE_TYPES:
        # Skip types whose Skyrim output record has no VMAD field in its def;
        # binding a script there only produces an "unexpected subrecord" error
        # (ALCH, SLGM, STAT, AMMO, and SGST→SCRL / SBSP→STAT map here).
        out_sig = TYPE_MAP.get(sig, sig)
        if out_sig not in VMAD_SUPPORTED_OUTPUT_TYPES:
            continue
        for rec in by_type.get(sig, []):
            scri = rec.get('SCRI', '')
            if not scri or scri not in scpt_by_fid:
                continue
            rec_fid_str = rec.get('FormID', '')
            if not rec_fid_str:
                continue
            try:
                raw_fid = int(rec_fid_str, 16)
            except ValueError:
                continue
            # The PLAYER base carries no VMAD: our shifted copy of NPC_ 0x07 is
            # a record no actor ever instantiates (the acting player is
            # PlayerRef 0x14, whose base is Skyrim's own 0x07), so a script
            # bound here is inert.  It is rehosted on a quest's PlayerRef alias
            # by build_player_alias_plan below.
            if sig == 'NPC_' and (raw_fid & 0x00FFFFFF) == _PLAYER_BASE_FORMID:
                continue
            rec_fid = _remap(raw_fid, offset)

            edid, sctx, extends = scpt_by_fid[scri]
            script_name = papyrus_script_name(edid or f'Script_{scri}')

            obj_props = props_memo.get(scri)
            if obj_props is None:
                try:
                    obj_props = _resolve_props(sctx, edid, extends, xref,
                                               fid_to_edid, offset)
                except Exception:
                    obj_props = {}
                props_memo[scri] = obj_props

            from .writer import pack_subrecord
            _OBJECT_VMAD[rec_fid] = pack_subrecord(
                'VMAD', build_vmad_object_script(script_name, obj_props))
            count += 1

    n_player = build_player_alias_plan(by_type, xref, fid_to_edid)
    if n_player:
        print(f"  Player-base scripts rehosted on a PlayerRef quest alias: "
              f"{n_player}")

    n_moved = _relocate_actor_scripts_to_refs(by_type, offset)
    if n_moved:
        print(f"  Actor scripts relocated to placed refs (reference events / "
              f"self-ref calls / "
              f"GetVMScriptVariable package gates): {n_moved}")
    return count


def build_player_alias_plan(by_type: dict, xref, fid_to_edid: dict) -> int:
    """Plan the rehosting of PLAYER-BASE scripts onto a PlayerRef quest alias.

    Oblivion let a plugin script the player by attaching a SCPT to the player's
    base NPC_ record (0x00000007).  Nehrim relies on this completely: its
    GlobalplayerScript holds the whole XP/level/learning-point/gold economy AND
    the `SetStage MQ00 1` that is the ONLY thing that starts the main quest, so
    losing it means the intro never begins and no character ever levels.

    Skyrim cannot honour that attachment.  The acting player is PlayerRef 0x14,
    whose record signature is PLYR — not ACHR, so a plugin cannot author an
    override of it and there is no placed reference to relocate onto (which is
    why _relocate_actor_scripts_to_refs, walking ACHR/ACRE, never sees this
    case).  PlayerRef's base is Skyrim's OWN Player 0x07; the converted
    plugin's copy is shifted into our index (0x01000007) and is a dead record
    nothing instantiates.

    Vanilla's mechanism for "code that runs on the player forever" is a
    start-game-enabled quest holding a reference alias forced to 0x14 — 71
    Skyrim.esm quests do exactly this.  The script rides that alias, so it is
    emitted as `extends ReferenceAlias` (see PLAYER_ALIAS_EXTENDS) and every
    implicit-self call routes through GetReference()/GetActorReference().

    Returns the number of scripts planned.
    """
    _PLAYER_ALIAS_SCRIPTS.clear()
    offset = get_formid_index_offset()
    scpt_by_fid = _collect_scpts(by_type, xref)

    for rec in by_type.get('NPC_', []):
        try:
            if (int(rec.get('FormID', ''), 16)
                    & 0x00FFFFFF) != _PLAYER_BASE_FORMID:
                continue
        except ValueError:
            continue
        scri = rec.get('SCRI', '')
        if not scri or scri not in scpt_by_fid:
            continue
        edid, sctx, _extends = scpt_by_fid[scri]
        script_name = papyrus_script_name(edid or f'Script_{scri}')
        try:
            props = _resolve_props(sctx, edid, PLAYER_ALIAS_EXTENDS, xref,
                                   fid_to_edid, offset)
        except Exception:
            props = {}
        _PLAYER_ALIAS_SCRIPTS.append((script_name, props))

    return len(_PLAYER_ALIAS_SCRIPTS)


# Reference-only events, per the vanilla Papyrus base classes (Scripts.zip):
# Actor.psc defines OnPackageEnd/OnPackageStart/OnDeath; ObjectReference.psc
# defines OnActivate/OnCellAttach/OnLoad/OnHit.  A base NPC_ record is an
# ActorBase (a Form), NOT an Actor, so a VMAD attached there receives NONE of
# these — they are delivered only to the placed REFERENCE.
#
# TES4 spellings of the same events (the converter maps these onto the Papyrus
# events above); matched against the raw SCTX source.
_TES4_REFERENCE_EVENTS = frozenset({
    'onpackagedone', 'onpackagestart', 'onpackagechange',
    'onactivate', 'ondeath', 'onhit', 'onalarm', 'onstartcombat',
    'onload', 'onequip', 'onunequip', 'onadd', 'ondrop', 'onsell',
    # `gamemode` is not itself an engine event, but the converter compiles it
    # into an OnUpdate poll started by OnCellAttach/OnCellDetach/OnLoad and
    # gated on TES4Polyfill.ShouldRunGameMode(Self) — every one of which is an
    # ObjectReference member.  On a base NPC_ VMAD `Self` is an ActorBase, so
    # the events never fire, the gate's Is3DLoaded()/GetParentCell() have no
    # reference to answer for, and the poll never starts: the whole block is
    # dead.  Morroblivion's CATDestinationSorter (the Jo'Tesh/Kisimba world
    # transport) is pure GameMode and triggered neither of the other two
    # reasons, so it rode the base record and never ran.  Same root cause as
    # the OnPackageEnd case below — the poll just hides it one layer deeper.
    'gamemode',
})


_BEGIN_BLOCK_RE = re.compile(r'(?:^|[\r\n;])\s*begin\s+(\w+)', re.IGNORECASE)


# Functions that act on the CALLING REFERENCE when written bare (no `ref.`
# prefix).  On a base ActorBase there is no reference for them to act on, so a
# base-attached script calling these is inert no matter which event drives it.
#
# `enable` is the load-bearing one: Oblivion's standard idiom for a scripted
# entrance is an initially-disabled placement whose OWN GameMode block enables
# it on a cue (`if GetStage MQ00 == 5 / enable`).  Left on the base, the call
# has no target and the actor never appears — that is exactly why Celebro, the
# Nehrim intro companion, was missing from the start cell.
_TES4_SELF_REF_FUNCS = frozenset({
    'enable', 'disable', 'moveto', 'startcombat', 'stopcombat',
    'kill', 'resurrect', 'playgroup', 'setalert', 'evp',
    'addscriptpackage', 'removescriptpackage',
})

# A bare call: start of line (after optional whitespace) and NOT preceded by a
# `.`, which would make it someone else's method (`CelebroRef.Disable`).
_BARE_CALL_RE = re.compile(r'(?:^|\n)[^\S\n]*(\w+)\b', re.MULTILINE)


def _script_uses_self_reference_call(sctx: str) -> bool:
    """True when a TES4 script calls a reference function on ITSELF (bare, no
    ``ref.`` prefix).

    Such a script only functions when attached to a placed reference; on the
    base record the call has no reference to act on.  Comment lines are skipped
    so a commented-out ``;evp`` does not trigger a move.
    """
    text = unescape_value(sctx)
    for m in _BARE_CALL_RE.finditer(text):
        line = text[m.start():text.find('\n', m.start()) if
                    text.find('\n', m.start()) != -1 else len(text)]
        if line.lstrip().startswith(';'):
            continue
        if m.group(1).lower() in _TES4_SELF_REF_FUNCS:
            return True
    return False


def _script_uses_reference_event(sctx: str) -> bool:
    """True when a TES4 script DECLARES an event the engine delivers only to a
    placed reference.

    Must match the ``begin <event>`` declaration, not a bare substring: a
    comment mentioning an event name is not a handler, and relocating on that
    would move scripts that have no reason to leave the base record.
    """
    return any(m.group(1).lower() in _TES4_REFERENCE_EVENTS
               for m in _BEGIN_BLOCK_RE.finditer(sctx))


def _relocate_actor_scripts_to_refs(by_type: dict, offset: int) -> int:
    """Move an actor's script VMAD from the base NPC_/CREA to its placed ACHR.

    Three independent reasons an actor script MUST live on the reference:

    1. ``GetVMScriptVariable(ref, "::var_var")`` reads the property off a
       script attached to the *reference named in param1* (the ACHR), not off
       the base record — verified against Skyrim.esm, where 100% of vanilla
       func-630 package conditions name a REFR that carries its own VMAD
       holding the variable.  A base-attached script propagates to instances
       for property *access* (fragment writes work), but the condition *read*
       fails, so the quest package never wins its arbitration and the actor
       stays put (Pinarus/FGC01Rats, Arielle/MG04Restore, ~142 actors).

    2. REFERENCE EVENTS never fire on a base-attached script.  ``OnPackageEnd``
       and friends are declared on ``Actor``/``ObjectReference`` (vanilla
       Scripts.zip); ``NPC_`` is an ActorBase, so an event-driven script bound
       there is inert.  This silently killed every converted quest that
       sequences on package completion — CharacterGen sets stage 12 from
       Renote's ``OnPackageEnd``, so the chain stopped at stage 10 and the
       Emperor/guards had no ``GetStage ==`` package to select at all.

       A bare ``GameMode`` block counts here too, even though TES4 delivers it
       everywhere: the converter compiles it into an OnUpdate poll whose only
       starters are OnCellAttach/OnLoad/OnInit, gated on
       ``ShouldRunGameMode(Self)``.  Those are ObjectReference members, so on a
       base record the poll never starts and the block is dead code.

    3. SELF-REFERENCE CALLS have no target on a base record.  A bare ``enable``
       / ``moveto`` / ``startcombat`` acts on the calling REFERENCE; an
       ActorBase is not one, so the call does nothing.  Oblivion's standard
       scripted-entrance idiom is an initially-disabled placement whose own
       GameMode block enables it on a cue, which makes this the difference
       between the actor appearing and never existing — Celebro, the Nehrim
       intro companion, was absent from the start cell for exactly this reason
       (``MQ00CelebroScript``: ``if GetStage MQ00 == 5 / enable``).

    Vanilla does exactly this split: instance-identified logic lives on the
    ACHR (masterAmbushScript, 464 placements), while generic per-actor
    behaviour stays on the base (WIDeadBodyCleanupScript, defaultGhostScript).

    The script is moved (base entry removed) rather than duplicated so there is
    exactly ONE instance — both the fragment write (via the ACHR-typed self
    property) and the condition read resolve to it.
    """
    from .pack_aliases import _scriptvar_refs_from_conditions

    # Refs (raw low-24) whose script variables a package condition reads.
    wanted_low = set()
    for rec in by_type.get('PACK', []):
        for ref in _scriptvar_refs_from_conditions(rec):
            wanted_low.add(ref & 0x00FFFFFF)

    # Base actors (raw low-24) whose script handles a reference-only event.
    scpt_src = {r.get('FormID', ''): r.get('SCTX', '')
                for r in by_type.get('SCPT', [])}
    event_bases = set()
    for sig in ('NPC_', 'CREA'):
        for rec in by_type.get(sig, []):
            src = scpt_src.get(rec.get('SCRI', ''), '')
            if src and (_script_uses_reference_event(src)
                        or _script_uses_self_reference_call(src)):
                try:
                    event_bases.add(int(rec.get('FormID', ''), 16) & 0x00FFFFFF)
                except ValueError:
                    pass
    if not wanted_low and not event_bases:
        return 0

    # How many times each base actor is placed — a script may be moved off the
    # base only when that base has a single placement, else siblings would lose
    # it.  Shared bases (rare: SI victims, Sheogorath's sheep) keep the base
    # attachment and gain a per-ref one, matching Oblivion's per-instance vars.
    placements: dict[int, int] = {}
    for sig in ('ACHR', 'ACRE'):
        for rec in by_type.get(sig, []):
            base_str = rec.get('NAME', '')
            if base_str:
                try:
                    placements[int(base_str, 16) & 0x00FFFFFF] = \
                        placements.get(int(base_str, 16) & 0x00FFFFFF, 0) + 1
                except ValueError:
                    pass

    moved = 0
    for sig in ('ACHR', 'ACRE'):
        for rec in by_type.get(sig, []):
            fid_str = rec.get('FormID', '')
            if not fid_str:
                continue
            try:
                ref_raw = int(fid_str, 16)
            except ValueError:
                continue
            base_str = rec.get('NAME', '')
            if not base_str:
                continue
            try:
                base_raw = int(base_str, 16)
            except ValueError:
                continue
            # Qualify by EITHER trigger: a package condition reads this ref's
            # script vars, or the base's script handles a reference-only event.
            if ((ref_raw & 0x00FFFFFF) not in wanted_low
                    and (base_raw & 0x00FFFFFF) not in event_bases):
                continue
            base_out = _remap(base_raw, offset)
            vmad = _OBJECT_VMAD.get(base_out)
            if not vmad:
                continue          # base actor has no converted script
            ref_out = _remap(ref_raw, offset)
            _OBJECT_VMAD[ref_out] = vmad          # attach to the placed ref
            if placements.get(base_raw & 0x00FFFFFF, 0) <= 1:
                _OBJECT_VMAD.pop(base_out, None)  # single placement: move it
            moved += 1
    return moved


def _resolve_props(sctx: str, edid: str, extends: str, xref,
                   fid_to_edid: dict, offset: int) -> dict:
    """Run the converter to learn the script's property refs, then bind the
    Object-typed ones to their target record FormIDs (output space).

    Value-typed properties (Int/Float/Bool locals) are left unbound — the engine
    defaults them to zero, which matches the TES4 script's initial state.
    """
    conv = ScriptConverter(xref)
    name = _safe_property_name(edid or 'Script')
    conv.convert_standalone(name, sctx, extends, edid)

    # Lazy (circular: import_main imports this module).
    from .import_main import get_well_known_properties
    well_known = get_well_known_properties()

    obj_props: dict[str, int] = {}
    for pname, ptype in conv.get_property_refs().items():
        if ptype in _VALUE_TYPES:
            continue
        safe = _safe_property_name(pname)
        low = pname.lower()
        if low in ('player', 'playerref'):
            obj_props[safe] = _PLAYER_FORMID
            continue
        if low in ENGINE_GLOBAL_FORMIDS:
            obj_props[safe] = ENGINE_GLOBAL_FORMIDS[low]
            continue
        # SYNTHESIZED records (TES4Fame/TES4Infamy/TES4GoldFenced/
        # TES4CyrodiilCrimeFaction/TES4Unlock_*) stand in for TES4 concepts
        # Skyrim has no record for, so they exist only in the OUTPUT and are
        # absent from xref.edid_to_formid — which is built from the TES4
        # export. resolve_property_formid() therefore misses every one, and the
        # property was silently left unbound (None at runtime).
        #
        # The dialogue and quest VMAD builders already inject the same registry
        # (`well_known_props`), so QF_/TIF_ fragments bound correctly and only
        # OBJECT scripts were affected — which is why this survived the round-2
        # verification that counted the 4,762 dialogue bindings.
        #
        # It is not cosmetic: TGStolenGoodsScript is the Thieves Guild rank
        # driver and all ten of its gates read `TES4GoldFenced.GetValue()`, so
        # a None property threw on the first tick and no TG rank ever advanced.
        if pname in well_known:
            obj_props[safe] = well_known[pname]
            continue
        fid_hex = resolve_property_formid(xref, pname)
        if not fid_hex:
            continue
        try:
            raw = int(fid_hex, 16)
        except ValueError:
            continue
        if raw == 0:
            continue
        obj_props[safe] = _remap(raw, offset)
    return obj_props
