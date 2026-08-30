# Speak-as lines: what works and what was reverted

Measured in-game. Two alternatives were built and both killed the audio.

## Speak-as lines: `Say()` on a voiced stand-in, with the in-head flag (2026-08-19)

TES4's `Say <topic> <force-subtitles> <speak-as NPC> <in-head>` speaks a line
THROUGH a marker/shrine/door AS some NPC. Skyrim's `Say` has no speak-as
argument and keys voice lookup on the speaker, and a bare XMarker STAT has no
voice type -- so the engine finds no voice folder and plays nothing.

**What works, measured in-game:** mint a TACT carrying that NPC's converted
VTYP, place it at the emitter's authored position, and call a plain
`Say(topic, None, abInHead)` on it. `abSpeakInPlayersHead` is Skyrim's own
third parameter and is the faithful conversion of TES4's fourth argument --
the voice comes from inside the player's head at full volume, which is how
Oblivion delivered the Arena announcer, the Daedric princes and Mankar
Camoran. See `tes5_import/speaker_activators.py`, `TES4Polyfill.SayScene`.

🛑 **Do not get clever with the delivery.** Two alternatives were built and
both KILLED THE AUDIO outright, which is strictly worse than any subtitle
defect:

* **A one-action SCEN per call site.** Scenes do tick a non-actor's line
  (`BGSSceneActionDialogue`), so the reasoning looked sound -- delivered
  through a scene, the lines produced no audio at all.
* **`Activate()` on the talking activator.** This *is* vanilla's idiom
  (`DA08WhisperingDoorScript`, `DA05QuestingBeastGhostScript`, DA10's
  `TalkingMace.GetRef().Activate(...)` -- and no vanilla script calls `Say()`
  on a TACT). But vanilla activates a TACT the PLAYER has walked up to, which
  is not what a polled announcer line is; converted call sites got no audio.

🛑 **Never emulate the in-head flag with `MoveTo(player)`.** That was an
invention: it teleports the marker out of its authored position permanently
(nothing moves it back) and costs the line its audio. Vanilla's one
repositioning case (DA05, following a ghost's head) uses `SetPosition`.

**Known open defect:** a scripted `Say()` on a NON-ACTOR does not retire its
subtitle -- measured live, a direct `Say` on the announcer's TACT played its
audio and left the subtitle onscreen indefinitely. The engine's countdown /
`SubtitleManager::KillSubtitles` path is `TESObjectREFR` vtable slot 0x40
(rva 0x2d9d80, SkyrimSE 1.6.1170), which nothing drives for a plain
reference. **No verified fix exists**; the two attempts above cost the audio
and were reverted. Audio is the higher-value behaviour, so the stuck subtitle
stands until a fix is demonstrated in-game rather than reasoned about.

### Speak-as INFOs silently lost their quest-inherited conditions (fixed 2026-08-25)

A speak-as INFO whose owning QUST carries conditions runs its inherited
condition block through `_drop_non_actor_speaker_ctdas` (dialog_converter),
which strips subject-run actor-identity CTDAs -- the speaker is a TACT, not an
actor, so `GetIsID`-family conditions can never pass.

That function walks the PACKED buffer, so its `sig` is a 4-byte `bytes` slice,
but `pack_subrecord` takes a `str` and calls `sig.encode('ascii')`. Every call
that kept at least one condition therefore raised
`AttributeError: 'bytes' object has no attribute 'encode'`.

The failure was invisible because `_convert_topic_infos`' per-INFO handler is a
blanket `except Exception` that prints `ERROR info under <topic>` and moves on
-- so the INFO was **dropped from the output entirely**.
Fix: `pack_subrecord(sig.decode('ascii'), data)`.

**Blast radius, measured 2026-08-25** by instrumenting the call site and running
both plugins through `--import-only`. An INFO is lost only when the drop leaves
at least one condition standing; when it strips them ALL, the function returns
`b''` at its `if not kept` early exit and never reaches the broken line.

* **Morrowind_ob.esm: 35 INFOs across 7 Daedric-prince topics** --
  mwDAMolagBalSpeech 8, mwDAMalacathSpeech 6, mwDAAzuraSpeech 6,
  mwDAMehrunesDagonSpeech 5, mwDASheogorathSpeech 4, mwDABoethiahSpeech 4,
  mwDAMephalaSpeech 2. Every one had `KEPT38` (one surviving condition).
* **Oblivion.esm: ZERO.** 390 speak-as INFOs reach the call site and 320 carry
  quest conditions, but all 320 measured `KEPT0` -- Oblivion's speak-as quests
  condition exclusively on funcs in `_NON_ACTOR_SPEAKER_DROP` (`{72, 254}`), so
  the block always empties and the bug is a near-miss there. This includes
  ArenaAnnouncer/Announcer (30), ICAnnouncer (34), DASheogorathSpeech (38) and
  the 13 other Daedric shrines.

🛑 **Do not estimate this statically from the export.** A first pass replaying
the drop against the QUST's RAW TES4 conditions predicted 353 lost INFOs in
Oblivion.esm; the real input is the CONVERTED block, whose TES4->TES5 function
remap changes which funcs fall in the drop set. Instrument the call site.

**Lesson:** that handler converts any bug in the INFO path into silent data
loss. When a topic reports `ERROR info under ...`, the INFOs under it are gone,
not degraded -- get the traceback before theorising.
