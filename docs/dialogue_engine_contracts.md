# Dialogue contracts read out of SkyrimSE.exe

Everything here was recovered by disassembling the engine, not inferred from
xEdit's definitions or from what vanilla data happens to contain. Where the two
disagree, this document is the authority, and each claim below names the routine
it came from so it can be re-checked.

## The binary has to be the GOG / Anniversary build

The Steam `SkyrimSE.exe` is Steam-encrypted: its `.text` section has entropy
8.00, so no code in it can be read statically (this is what
`project_skyrimse_exe_drm_packed` recorded). The GOG / Anniversary Edition build
at `D:\Other Games\Skyrim Anniversary Edition\SkyrimSE.exe` is **not** packed —
`.text` entropy is 6.04 and the dialogue routines disassemble cleanly.

`tools/dialog_engine_extract.py` refuses to run against a packed build rather
than printing garbage.

    python tools/dialog_engine_extract.py --subtypes
    python tools/dialog_engine_extract.py --json tes5_import/dialog_engine_tables.json

RTTI type names survive in both builds, so `tools/skyrim_disasm.py --find` still
locates classes (`TESTopic`, `TESTopicInfo`, `MenuTopicManager`,
`BGSDialogueBranch`) either way.

## The engine's own subtype and category tables

Two static arrays in `.data`, found by cross-referencing the pointer to the
`"PlayerDialogue"` string literal:

| Table | RVA | Layout |
|---|---|---|
| Categories | `0x1e638d0` | 8 entries, 16 bytes: name pointer, first subtype index, category id |
| Subtypes | `0x1e63950` | 103 entries, 40 bytes: name pointer, category (+8), 4-char tag (+12), flag byte (+20) |

The subtype array ends at a `0xDEADBEEF` sentinel, which is how the extractor
knows where to stop.

The eight categories, in engine order, with the subtype index each range starts
at:

| id | Name | First subtype |
|---|---|---|
| 0 | PlayerDialogue | 0 |
| 1 | FavorDialogue | 3 |
| 2 | SceneDialogue | 14 |
| 3 | Combat | 26 |
| 4 | Favors | 15 |
| 5 | Detection | 55 |
| 6 | Service | 66 |
| 7 | Miscellaneous | 75 |

This is information xEdit does not carry at all: its subtype list is sorted
alphabetically and records no category membership and no ordering. The full
103-row table is checked in as `tes5_import/dialog_engine_tables.json`.

## SNAM decides the subtype and the category; DATA does not

This is the finding that most changes how the converter should be read.

In `TESTopic::LoadForm`, the SNAM handler at RVA `0x3a6fa8` does this:

```
mov   r8d, 0x67                            ; 103 = "not found" sentinel
cmp   edx, 0x67                            ; loop i over 0..102
lea   rcx, [rax + rax*4]                   ; i*5, so i*40 once scaled by 8
cmp   dword ptr [rdi + rcx*8 + 0xc], r9d   ; table[i].tag == the SNAM just read
cmove r8d, edx                             ; on a match, remember i
...
mov   word ptr [r15 + 0x32], ax            ; TESTopic+0x32 = subtype  := i
movzx eax, byte ptr [rdi + rcx*8 + 8]      ; table[i].category
mov   byte ptr [r15 + 0x31], al            ; TESTopic+0x31 = category := that
```

So the runtime subtype is **the index of the matching row in the engine's
table**, and the category is **that row's category field**. Both are derived
from SNAM's four characters. A tag with no matching row yields 103, the
sentinel.

The DATA handler at `0x3a6efc` writes the same two fields (`+0x31`, `+0x32`)
from the record, so which subrecord wins is purely a matter of parse order — and
in all 15,037 DIAL records in Skyrim.esm, SNAM is stored after DATA:

    8500 records: PNAM QNAM DATA SNAM TIFC
    6537 records: PNAM BNAM QNAM DATA SNAM TIFC

SNAM therefore always overwrites DATA. **The subtype and category bytes stored
in DIAL DATA have no effect on the running game.** They are stale values the
Creation Kit wrote and never refreshed, which is why vanilla is full of tags
carrying two different DATA subtypes — `HELO` is 73 in 288 records and 79 in 9,
`GBYE` is 72 in 126 and 78 in 3 — while the engine treats every one of them
identically.

DIAL DATA's own layout is `TopicFlags(u8) + Category(u8) + Subtype(u16)`,
matching xEdit — the engine reads the four bytes into `TESTopic+0x30`, so byte 1
lands on `+0x31` (category) and the u16 on `+0x32` (subtype), exactly the two
fields the SNAM handler above writes.

**A trap when checking this against `export/*.txt`:** the exporter prints DATA
as a big-endian `u32`, so vanilla Hello *displays* as `00490700` while its
on-disk bytes are `00 07 49 00`. Read the printed form as raw bytes and the
category and subtype look transposed, and the category byte appears to range up
to 96 rather than 0–7. Unpack it as a little-endian `u32` first.

What this means for the converter: SNAM is the field that has to be right, and
a wrong DATA subtype is harmless. Do not "fix" a converted DIAL by reasoning
about its DATA bytes, and do not trust DATA when reading vanilla records to
learn what a subtype number means.

`tools/dialog_emulator.py` now derives both values from SNAM through the engine
table, and reports 17 distinct tags across our Oblivion output with zero
unresolved.

## INFO DATA is 8 bytes: u16 flags, padding, and a scaled float

From `TESTopicInfo::LoadForm`, the DATA handler at RVA `0x3aa9a4`:

```
mov       r8d, 8                    ; the subrecord is read as 8 bytes
...
movzx     eax, word ptr [rbp-0x4f]
mov       word ptr [r12 + 0x3c], ax ; flags := first u16
movss     xmm0, dword ptr [rbp-0x4d]
mulss     xmm0, xmm6                ; xmm6 = 65535.0
cvttss2si eax, xmm0
mov       word ptr [r12 + 0x3e], ax ; reset := trunc(float * 65535)
```

The constant at `0x16689bc` is exactly `65535.0`.

All 924 INFO records in Skyrim.esm that carry DATA carry exactly 8 bytes, and
their float field holds small fractions — 0.021, 0.004, 0.013 — consistent with
a normalized value the engine expands into a `u16`, not with the count of days
that xEdit's field name ("Reset Days") suggests. xEdit is right about the *bit
meanings* of the flags word and about the layout; its name for the float encodes
an assumption about units that the engine does not support.

The emulator previously ignored INFO DATA entirely, so Say Once, Random, and
Goodbye were invisible to it. It now parses the subrecord with this layout and
exposes `say_once`, `is_random`, `is_goodbye`, and `reset_ticks`.

## DIAL.QNAM is a hard runtime gate; TES4's QSTI is not (2026-08-01)

Skyrim evaluates a topic's INFOs only while its **owning quest (QNAM) is
running**. Oblivion's QSTI list is an organisational grouping the engine never
gates on, so a TES4 topic filed under a quest that nothing ever starts still
worked — and converts into a permanently dead topic.

Nehrim ships exactly this: `MQ01Topic01` ("show Aratornias the letter") is filed
under the vestigial 3-stage `MQ01`, and its INFO holds the only
`SetStage MQ00 65` — MQ00's completion stage. Faithfully copying `Quest[0]` made
the intro quest uncompletable.

`build_dialog_groups` therefore computes the set of quests that can ever run
(start-game-enabled, or named by a `StartQuest`/`SetStage` anywhere in the
plugin's scripts, quest stage result scripts, or INFO result scripts) and owns a
topic by its original quest **only when that quest is in the set**; otherwise it
falls back to the always-running generic quest, exactly as a multi-quest topic
does. The analysis is skipped entirely when the plugin has no SCPT records — a
starter can only be found in a script, so with none every non-SGE quest would
look unstartable and the per-quest gating would be lost wholesale.

`Quest.SetStage` counts as a starter: vanilla `Quest.psc` documents it as
*"latent and will wait for the quest to start up before returning (if it needed
to be started)"*, so it starts a stopped quest. Censused on Skyrim.esm, 0 of
15,037 vanilla DIALs point QNAM at a non-existent quest.

## Re-deriving any of this

`tools/skyrim_disasm.py` locates classes and disassembles, and
`tools/dialog_engine_extract.py` pulls the tables. To find which code touches a
table, scan `.text` for RIP-relative `lea` instructions whose target is the
table's RVA — there are only four such references for the two dialogue tables,
which is what made `TESTopic::LoadForm` easy to isolate.

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
