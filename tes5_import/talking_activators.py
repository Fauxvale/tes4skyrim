r"""TES4's disembodied voices: the `Say` speak-as call sites and their topics.

WHY THIS EXISTS
---------------
TES4's `Say` takes a **speak-as actor** as its third argument:

    ArenaMatchPlayerRef.Say Announcer 1 ArenaMouth 1
    ^ emits the sound        ^ topic  ^ ^ WHO is speaking (a base NPC_)
                                      |                    ^ 1 = in the player's head
                                      force subtitles

The emitting reference is an XMarker, a shrine ACTI or a door; the *identity*
is a separate NPC_ record.  Oblivion resolves the speaker to that NPC, so the
INFO's `GetIsID <thatNPC>` condition passes and a line is selected.

Skyrim's `Say` has no such argument.  The subject stays the emitting marker, so
`GetIsID` can never pass -- **no INFO is selected and Say() plays nothing**,
while the calling script's timers run on regardless.  That is a silent line
with a chain that still advances: the Imperial City Arena announcer said
nothing yet the gates still opened on schedule.

Two further gates fail the same way, because a marker is not an actor:
the injected `GetIsVoiceType` (a STAT has no voice type) and any actor-shaped
QUST-level condition copied onto the INFO (ArenaAnnouncer carries
`GetIsPlayableRace`).

THE DISCRIMINATOR IS THE TOPIC, NOT THE NPC
-------------------------------------------
An NPC is not "a voice".  SEThadon is a real, placed actor who speaks his own
dialogue AND is named as the speak-as identity of a marker-spoken shout.
Keying this on the NPC dropped the authored `GetIsID(SEThadon)` from lines he
delivers himself, widening them to anyone: 53 INFOs name both a voice identity
and a real speaker.  "Never placed in the world" is no better -- that is a
property that happens to correlate, not the authored fact.

The authored fact is the TOPIC.  `se07athadonshout` is reachable only via
`marker.Say SE07AThadonShout 1 SEThadon 1`; Thadon's ordinary lines live in
other topics.  So every INFO inside a speak-as topic is marker-spoken by
construction, and the gates above are unsatisfiable there and only there.

Measured 2026-08-18 on Oblivion.esm: 61 call-site triples, 35 (emitter,
voice) pairs, 59 topics, and ZERO overlap with GREETING or any other shared
topic -- each is a dedicated topic the author created for that voice.

🛑 THE SCAN IS SAME-LINE AND VALIDATES THE SPEAK-AS TOKEN.  An earlier
version used `\s+` between arguments and did not check the third token, so
`SEGrommokRef.Say SE03GrommokChamberOneStart 1<newline>set triggered to 1`
matched with `set` as the "voice" and 27 of Grommok's/Lewin's/Syndel's OWN
topics were classed as speak-as -- their authored GetIsID gates were dropped.
Arguments of one TES4 command never span lines, and the speak-as slot must
name an NPC_/CREA base (or it is a flag).

This module finds the call sites; speaker_activators builds their delivery
records and dialog_conditions / dialog_converter consume the topic set.
"""

import re

from .text_reader import get_formid, get_str

# TES4 `Say` naming a speak-as actor, one line only:
#   Say <topic> <force-subtitles> <speak-as> [<in-players-head>]
_SAY_SPEAK_AS_RE = re.compile(
    r"([A-Za-z]\w*)[ \t]*\.[ \t]*Say[ \t]+([A-Za-z]\w*)[ \t]+(\d+)[ \t]+"
    r"([A-Za-z]\w*)(?:[ \t]+(\d+))?", re.IGNORECASE)


def _bodies(by_type: dict):
    """Every TES4 script body in the plugin: SCPT, INFO results, QUST stages."""
    sources = [("SCPT", "SCTX"), ("INFO", "ResultScript"), ("QUST", None)]
    for sig, field in sources:
        for rec in by_type.get(sig, []):
            if field:
                body = get_str(rec, field) or ""
                if body and ".say" in body.lower():
                    yield body
            else:
                for k, v in rec.items():
                    if (isinstance(v, str) and "ResultScript" in k
                            and ".say" in v.lower()):
                        yield v


def scan_speak_as_calls(by_type: dict) -> list:
    """Every speak-as `Say` call site, deduplicated and sorted.

    Returns tuples ``(emitter, voice, topic, in_head)`` -- EditorIDs lowercased,
    ``in_head`` True when the authored fourth argument is a non-zero integer.
    Only sites whose topic names a DIAL and whose speak-as token names an
    NPC_/CREA base are returned; anything else in that slot is a flag or a
    stray token, never a voice.
    """
    dials = {(get_str(d, "EditorID") or "").lower()
             for d in by_type.get("DIAL", [])}
    actors = set()
    for sig in ("NPC_", "CREA"):
        for rec in by_type.get(sig, []):
            e = (get_str(rec, "EditorID") or "").lower()
            if e:
                actors.add(e)
    found = set()
    for body in _bodies(by_type):
        for m in _SAY_SPEAK_AS_RE.finditer(body):
            emitter, topic, _force, voice, in_head = m.groups()
            topic = topic.lower()
            voice = voice.lower()
            if topic not in dials or voice not in actors:
                continue
            found.add((emitter.lower(), voice, topic,
                       bool(in_head) and int(in_head) != 0))
    # A site that appears both with and without the in-head flag: in-head
    # wins for the scene (the flag is per CALL and the converter passes it
    # through; the record set only needs the triple).
    return sorted(found)


def scan_speak_as_topics(by_type: dict) -> set:
    """DIAL FormIDs (24-bit) reachable ONLY through a speak-as `Say`."""
    edid_to_dial = {}
    for rec in by_type.get("DIAL", []):
        edid = (get_str(rec, "EditorID") or "").lower()
        fid = get_formid(rec, "FormID") & 0x00FFFFFF
        if edid and fid:
            edid_to_dial[edid] = fid
    found = set()
    for _emitter, _voice, topic, _in_head in scan_speak_as_calls(by_type):
        fid = edid_to_dial.get(topic)
        if fid:
            found.add(fid)
    return found
