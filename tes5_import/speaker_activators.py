r"""Speaker activators: give TES4's disembodied voices a real talking activator.

THE PROBLEM
-----------
TES4's `Say` names a speak-as actor separately from the reference that emits
the sound:

    ArenaMatchPlayerRef.Say Announcer 1 ArenaMouth 1
    ^ an XMarker (STAT)                 ^ WHO is speaking

Oblivion resolves the speaker to that NPC, so the line picks up the NPC's
voice folder and is delivered as a real exchange with the player.  Skyrim's
`Say` has no such argument: the speaker stays the XMarker, and **a STAT has no
voice type at all**, so the engine finds no voice folder.

THE FIX -- WHAT VANILLA ACTUALLY DOES
-------------------------------------
A TACT (Talking Activator) is an activator that carries a `VNAM` voice type
and that the engine accepts as a dialogue speaker.  Vanilla ships 25 and
**every single one has a VNAM** -- censused 2026-08-18 against
references/Skyrim.esm/TACT.txt, 25/25, no exceptions.
`DBNightMotherTalkingActivator` even uses `Markers\Marker_LinkMarker.nif`,
the same marker mesh our emitters already are.

So, per (emitter reference, speak-as NPC) pair:

  * mint a TACT carrying that NPC's converted VTYP, and
  * place a REFR of it at the emitter's exact position/cell.

Then the converted script speaks the line ON THAT REFERENCE:
`TES4Polyfill.SpeakAs` issues `Say(topic, None, abInHead)`, where abInHead is
TES4's fourth `Say` argument and Skyrim's own third one -- the voice comes
from inside the player's head at full volume, as in Oblivion.

🛑 **Two cleverer deliveries were tried and BOTH KILLED THE AUDIO** (worse
than the defect they targeted), 2026-08-19:

  * a one-action SCEN per call site, driven by `Scene.ForceStart()`;
  * `Activate()` on the talking activator -- which IS vanilla's own idiom
    (`DA08WhisperingDoorScript`, `DA05QuestingBeastGhostScript`, DA10's
    `TalkingMace.GetRef().Activate(...)`, and no vanilla script calls `Say()`
    on a TACT) -- but vanilla activates a TACT the PLAYER walked up to, which
    is not what a polled announcer line is.

🛑 **Never** emulate the in-head flag by `MoveTo`-ing the speaker onto the
player: that teleports the marker out of its authored position permanently
(nothing moves it back) and costs the line its audio.  Vanilla's one
repositioning case (DA05, following a ghost's head) uses `SetPosition`.

KNOWN OPEN DEFECT: a scripted `Say()` on a non-actor does not retire its
subtitle (the engine's countdown/KillSubtitles path is TESObjectREFR vtable
slot 0x40, which nothing drives for a plain reference).  No verified fix
exists; audio is the higher-value behaviour.  See
docs/dialogue_engine_contracts.md.

PER PAIR, NOT PER EMITTER
-------------------------
A REFR has one base, so it has one voice type.  Three emitters speak for more
than one voice, and the voices differ in gender -- `SE07ThadonSpeaks` covers
both SEThadon (Male) and SESyl (Female).  Keying on the emitter alone would
give one of them the wrong voice folder.  Measured on Oblivion.esm: 34
emitters, 35 (emitter, voice) pairs.

Layout verified against BOTH xEdit (`wbRecord(TACT ...)` in
Core/wbDefinitionsTES5.pas: EDID VMAD OBND FULL model DEST keywords PNAM SNAM
FNAM VNAM) and a real dump (references/Skyrim.esm/TACT.txt).  PNAM/FNAM are
`wbUnknown(..., cpIgnore, True)` -- required, and zero in every vanilla record.
"""

import struct

from .text_reader import get_formid, get_str
from .writer import (pack_record, pack_subrecord, pack_string_subrecord,
                     pack_formid_subrecord, pack_obnd)
from .talking_activators import scan_speak_as_calls

# The marker mesh vanilla's own Night Mother talking activator uses, with the
# MODT that ships beside it in Skyrim.esm (TACT 00022440).
_TACT_MODL = 'Markers\\Marker_LinkMarker.nif'
_TACT_MODT = bytes.fromhex('020000000000000000000000')

# Subrecords a cloned speaker must never inherit from the reference it was
# copied from: teleports, ownership, locks, enable-parents, scale, counts.
_CLONE_DROP_PREFIXES = (
    'XTEL', 'XOWN', 'XLOC', 'XESP', 'XPRM', 'XLIB', 'XLKR', 'XSCL',
    'XRDS', 'XEMI', 'XMBR', 'XCNT', 'XRNK', 'XACT', 'XTRG', 'XSED',
    'XCHG', 'XHLT', 'XPPA', 'XATO', 'XLRT', 'XLRL',
)

# (emitter EditorID lower, voice EditorID lower) -> generated speaker REFR fid.
_SPEAKER_REFS = {}


def reset() -> None:
    """Clear per-run state (the importer may build several plugins)."""
    _SPEAKER_REFS.clear()


def export_speaker_map() -> dict:
    """(emitter_edid, voice_edid) -> speaker REFR FormID."""
    return dict(_SPEAKER_REFS)


def speaker_property_name(emitter: str, voice: str) -> str:
    """The Papyrus property name script_convert emits for a speaker REFR."""
    return f'TES4Voice_{emitter.lower()}_{voice.lower()}'


def _pack_tact(fid: int, edid: str, vtyp_fid: int, name: str) -> bytes:
    """One TACT, in the vanilla subrecord order."""
    subs = pack_string_subrecord('EDID', edid)
    subs += pack_obnd(0, 0, 0, 0, 0, 0)
    if name:
        subs += pack_string_subrecord('FULL', name)
    subs += pack_string_subrecord('MODL', _TACT_MODL)
    subs += pack_subrecord('MODT', _TACT_MODT)
    subs += pack_subrecord('PNAM', struct.pack('<I', 0))
    subs += pack_subrecord('FNAM', struct.pack('<H', 0))
    subs += pack_formid_subrecord('VNAM', vtyp_fid)
    return pack_record('TACT', fid, 0, subs)


def build_speaker_activators(by_type: dict, writer, npc_to_vtyp: dict,
                             offset: int) -> int:
    """Mint a TACT + placed REFR for every (emitter, speak-as voice) pair.

    Mutates ``by_type``: appends the new REFRs so the CELL/WRLD builders place
    them.  Must run BEFORE those builders, like the leveled-actor shells.
    Returns the number of speakers built.
    """
    pairs = sorted({(e, v) for e, v, _t, _h in scan_speak_as_calls(by_type)})
    if not pairs:
        return 0

    refr_by_edid = {}
    for rec in by_type.get('REFR', []):
        e = (get_str(rec, 'EditorID') or '').lower()
        if e:
            refr_by_edid.setdefault(e, rec)
    npc_by_edid = {}
    for sig in ('NPC_', 'CREA'):
        for rec in by_type.get(sig, []):
            e = (get_str(rec, 'EditorID') or '').lower()
            if e:
                npc_by_edid.setdefault(e, rec)

    new_refrs = []
    for emitter, voice in pairs:
        src = refr_by_edid.get(emitter)
        npc = npc_by_edid.get(voice)
        if src is None or npc is None:
            continue
        vtyp = npc_to_vtyp.get(get_formid(npc, 'FormID'))
        if not vtyp:
            # No voice type means no folder to read from; a speaker here would
            # be as silent as the marker it replaces.
            continue

        key = f'{emitter}|{voice}'
        tact_fid = writer.derive_formid('SPEAKER_TACT', key)
        writer.add_record('TACT', _pack_tact(
            tact_fid, f'TES4Voice_{voice}', vtyp, get_str(npc, 'FULL') or ''))

        # Place it exactly where the author put the emitter, by cloning the
        # emitter's own REFR and repointing NAME at the new TACT.  Cloning
        # keeps cell/position/rotation without re-deriving any of it.
        refr_fid = writer.derive_formid('SPEAKER_REFR', key)
        clone = dict(src)
        clone['FormID'] = f'{refr_fid & 0x00FFFFFF:06X}'
        clone['EditorID'] = f'TES4VoiceRef_{emitter}_{voice}'
        high = ((tact_fid >> 24) - offset) & 0xFF
        clone['NAME'] = f'{high:02X}{tact_fid & 0x00FFFFFF:06X}'
        for k in list(clone):
            if k.startswith(_CLONE_DROP_PREFIXES):
                del clone[k]
        clone['RecordFlags'] = '1024'          # Persistent
        new_refrs.append(clone)
        _SPEAKER_REFS[(emitter, voice)] = refr_fid

    if new_refrs:
        by_type.setdefault('REFR', []).extend(new_refrs)
    return len(new_refrs)
