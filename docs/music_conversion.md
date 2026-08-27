# Music conversion (TES4 → TES5)

Implemented 2026-08-25. Built and verified on Oblivion.esm and Nehrim.esm.

## The core fact: no record names a music file

TES4 has **no music records at all**. Oblivion's engine scans
`Data\Music\<Category>\` and shuffles whatever it finds; the plugin carries only
a 3-value enum (xEdit `wbMusicEnum`):

| value | meaning |
|---|---|
| 0 | Default |
| 1 | Public |
| 2 | Dungeon |

stored in `CELL.XCMT` (U8) and `WRLD.SNAM` (U32).

**So the FOLDER is the authored unit of meaning**, and a
"only extract files the plugin references" filter is wrong here. Measured:

| | referenced music paths | files on disk |
|---|---|---|
| Oblivion.esm | **0** | 32 |
| Nehrim.esm | 35 (all from SCPT/SOUN) | 76 |

For Nehrim, **49 of 76 files (64%) are referenced by no record** — all of
`Battle/` and `Public/`, most of `Explore/` and `Dungeon/`. Those are exactly
the files the engine's folder-scan uses. A reference filter would ship 0 files
for Oblivion and silence every town and fight in Nehrim.

Conversely **8 of Nehrim's 35 references do not exist on disk**
(`bardemusik01.wav`, `music\nehrim\theme_03.mp3`, `theme_06_part01/02.mp3`,
`schandmaulbacktrack.mp3`, `event_chase01.mp3`, and the `specialevent_05.mp3`
typo — a missing path separator). The reference list is not even a reliable
subset; those call sites keep an inert marker.

## Scoping: per plugin, not per worldspace

Output goes to `Music\tes4\<plugin>\<Category>\`. This is load-bearing:
Oblivion and Nehrim **both** ship `Explore/`, `Dungeon/`, `Public/`, `Battle/`
and `Special/`, so a shared `Music\Explore\` would have one overwrite the other.

Per-*worldspace* scoping was considered and rejected — there is no authored
source for it. Nehrim's 23 worldspaces with `SNAM` carry only the same 3-value
enum (18 Dungeon + 5 Public); `MQ08SanktumWorld` and `CubeWorldspace` both just
say `music=2`. Vanilla Skyrim agrees: **9 distinct MUSC across 27 worldspaces**,
with `MUSDungeonCave` reused 8× and `MUSTownTest` 6×.

Loose music is ingested **only for masterless plugins**
(`bsa_extract._is_masterless`, which delegates to `terrain_lod._master_names`).
Nehrim's `Data\` holds five plugins (Nehrim.esm, ORN.esp, Translation.esp, …)
beside one `Music\` tree; without the gate every dependent ESP would re-ship
329 MB.

## Format: xWMA, not PCM

Vanilla SSE ships loose music as `RIFF/XWMA, wFormatTag=0x161, 44.1 kHz stereo`.
All 240 vanilla `MUST.ANAM` strings say `.wav`, but `.xwm` loads too and we
write `.xwm` in the ANAM so no extension substitution is assumed.

Encoding is **stereo** — `audio_converter.convert_file_to_xwm` downmixes to mono,
which is right for dialogue and wrong for a soundtrack, hence the separate
`music_convert.convert_music_file`.

## Bitrate: native rates only, scaled to the source

🛑 **xWMAEncode accepts only 20000, 32000, 48000, 64000, 96000, 160000, 192000.**
Anything else fails outright with `XWMA_E_UNSUPPORTED_BITRATE` — 128000 looks
natural and is not selectable.

🛑 **Of those, only a subset is NATIVE per (sample rate, channels).** From
xWMAEncode's own usage text:

```
44100Hz mono:   32000, 48000
44100Hz stereo: 32000, 48000, 96000, 192000
48000Hz stereo: 48000, 64000, 96000, 160000, 192000

"Other combinations are supported by resampling the source data
 and/or using a bitrate of 48kbps as a fallback"
```

**64000 and 160000 are NOT native at 44.1 kHz.** Asking for either silently
resamples the output to 48 kHz. Verified by reading the `fmt` chunk of the
result rather than trusting the request:

| asked | actual | sample rate | size | |
|---|---|---|---|---|
| 48000 | 48 | 44100 | 0.68 MB | native |
| 96000 | 96 | 44100 | 1.35 MB | native |
| **160000** | 160 | **48000** | 2.25 MB | **RESAMPLED** |
| 192000 | 192 | 44100 | 2.69 MB | native |

We normalise every source to 44.1 kHz stereo before encoding, so only the
`44100Hz stereo` row applies. `pick_bitrate()` never returns anything outside
`NATIVE_44K_STEREO` / `NATIVE_44K_MONO`.

### Why the target tracks the source

Re-encoding lossy→lossy compounds artifacts: spending 192k on a 128k mp3
preserves that mp3's existing damage more faithfully without recovering
anything. Measured SNR of the decoded xWMA against the source PCM:

| source | @32k | @48k | @96k | @192k |
|---|---|---|---|---|
| 128 kb/s | 12.96 | 14.37 | **20.92** | 29.50 |
| 192 kb/s | 12.75 | 14.51 | **26.77** | 33.88 |
| 320 kb/s | 15.26 | 19.25 | **25.62** | 31.50 |

The 128k source at 96k (20.92 dB) is worse than the 320k source at the same
96k (25.62 dB) — the ceiling is the SOURCE, so the ladder scales with it:

| source kb/s | target |
|---|---|
| ≤ 64 | 32000 |
| ≤ 128 | 48000 |
| ≤ 224 | 96000 |
| > 224 | 192000 |

Mono clamps to `NATIVE_44K_MONO` (tops out at 48000).

For calibration, **vanilla Skyrim ships ALL its music at 48 kb/s** 44.1 kHz
stereo — measured on `mus_combat_01.xwm` / `mus_dungeon_01.xwm` in
`Skyrim - Sounds.bsa` and all 49 loose AE soundtrack files. Even the bottom
rung here is vanilla parity; the top is 4×.

### Measured source spread and result

| source kb/s | Nehrim | Oblivion | → target |
|---|---|---|---|
| 64 (mono) | 1 | — | 32k |
| 112–128 | 16 | 1 | 48k |
| 160–192 | 2 | 29 | 96k |
| 320 | 57 | 2 | 192k |

| plugin | source mp3 | output xwm |
|---|---|---|
| Nehrim.esm | 328.6 MB | **194 MB** |
| Oblivion.esm | 103.6 MB | **53 MB** |

All 108 output files verified at 44.1 kHz — none resampled.

## Record structure

Per `wbDefinitionsTES5.pas` and a real Skyrim.esm dump (258 MUST / 50 MUSC):

```
MUSC: EDID, FNAM flags(u32), PNAM {Priority u16, Ducking u16}, WNAM fade(f32),
      TNAM array of MUST FormIDs
MUST: EDID, CNAM track type(u32), FLTV duration(f32), DNAM fade-out(f32),
      ANAM track filename
```

`CNAM` values are **hashes, not an ordinal enum** — writing 0/1/2 gives a track
the engine ignores:

| constant | value | vanilla count |
|---|---|---|
| Single Track | `0x6ED7E048` | 240 |
| Palette | `0x23F678C3` | 13 |
| Silent Track | `0xA1A9C4D5` | 5 |

Vanilla census drove what we omit: only **1** record carries `LNAM` loop data,
**10** carry `FNAM` cue points and **10** a `BNAM` finale — all three are left
out rather than invented. 210 of 240 `ANAM`s start with a leading backslash;
we follow the majority.

`FLTV` is a real float in seconds the engine schedules against, so duration is
**probed with ffmpeg**, never guessed.

### Silence tracks

A stem containing `silence` becomes a Silent Track (no `ANAM`). Both games
author one: Nehrim's `Special\Silence.mp3`, and — at the music **root** with no
category folder — Oblivion's `5min-silence.mp3` (300 s). A root-level file is
given one-shot `special` treatment rather than joining `Explore`'s rotation,
where it would play as five minutes of dead air.

## Group order

MUSC/MUST go **after** CELL, matching vanilla. Measured on the real Skyrim.esm
top-level order: CELL 57, WRLD 58, LCTN 86, MUSC 91, DLBR 97, MUST 98.

Unlike a REFR base object, a cell's `XCMO` is **not** resolved when the CELL
group parses — vanilla's own 701 `XCMO` cells load with the music groups 34
slots later — so the base-object rule in `writer._group_order` does not apply.

## Script revival

`StreamMusic` was previously inert. It now resolves to `MusicType.Add()` on the
per-cue MUSC built from that exact path.

- **38 `StreamMusic` calls in Nehrim.esm** (35 by path, 3 by bare category);
  Oblivion.esm has **none**.
- **26 revived**; the remaining 12 call sites are the 5 distinct dead
  references above and `StreamMusic Random`.
- All **13** declared `MusicType` properties bind to a real MUSC record.

The EditorID is the contract between the two sides:
`script_convert.constants.music_cue_editor_id` / `music_type_editor_id` **must**
match `tes5_import.record_types.music.musc_cue_editor_id` / `musc_editor_id`.
If they drift, the property binds to nothing and the cue is silent.

Music cues ride `initargs` into the script workers — Windows **spawns** workers,
so a dict built only in the parent leaves every worker with an empty map.

`emc*` (Elys Music Control: `emcMusicStop`, `emcSetMusicHold`,
`emcIsBattleOverridden`, `emcGetPlaylist`) stays inert — those control the
playlist rather than naming a track, and Papyrus has no equivalent even with
MUSC authored.

## The one authored value

`Battle\` has **no record-level source** — Oblivion picks combat music by folder
name alone, and `XCMT` has no combat value. Its MUSC priority/flags are authored
in `CATEGORY_SPECS`. Every other category is a direct folder mapping.

## Verified output

| | Oblivion.esm | Nehrim.esm |
|---|---|---|
| MUST | 32 (31 single + 1 silent) | 76 (75 single + 1 silent) |
| MUSC | 9 (4 category + 5 cue) | 34 (4 category + 30 cue) |
| CELL `XCMO` | 1,871 | 613 |
| WRLD `ZNAM` | — | 23 of 34 |

Counts match the source census exactly (Oblivion 1104 Dungeon + 767 Public =
1871; Nehrim 386 + 227 = 613, and 18 + 5 = 23 worldspaces).

## Running the extract stage for a standalone conversion

`phase_extract` reads the single global `tes4DataPath`, and `find_game_path`
checks that config value BEFORE falling back to registry detection. So a
standalone total conversion (Nehrim ships its own game folder on another drive)
works by pointing `tes4DataPath` at that install — which is what the GUI's data
path selector writes (`gui.py`, `updated["tes4DataPath"]`).

With `tes4DataPath` empty, a bare CLI run registry-detects Oblivion and finds
none of Nehrim's BSAs or its loose `Music\`. That is a configuration state, not
a bug: set the path (or use the GUI) and both the BSA pass and the loose-music
ingest resolve correctly.
