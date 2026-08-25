"""Misc converters: SOUN, PACK, WTHR.

All dialog/quest/DIAL/INFO/DLBR/DLVW logic has been moved to
tes5_import.dialog_converter.
"""

import os
import struct

from ..text_reader import get_hex_bytes
from .common import (
    _prefix_path,
    get_float,
    get_formid,
    get_int,
    get_str,
    pack_formid_subrecord,
    pack_obnd,
    pack_record,
    pack_string_subrecord,
    pack_subrecord,
    pack_uint32_subrecord,
)


# TES4 SNDX/SNDD flag bits (xEdit wbDefinitionsTES4, SOUN)
_TES4_SND_RANDOM_FREQ_SHIFT = 0x0001
_TES4_SND_LOOP              = 0x0010
_TES4_SND_MENU_SOUND        = 0x0020
_TES4_SND_2D                = 0x0040


# Vanilla Skyrim SOPM constants (verified against references/Skyrim.esm SOPM dump)
_SOPM_2D = 0x000B5183            # SOMDialogue2D — non-attenuating, for menu/2D sounds

# Falloff for a 3D sound whose TES4 record authored no max attenuation
# distance.  1800 units is vanilla Skyrim's most common mono model
# (SOMMono01800, 285 SNDRs).  Never use _SOPM_2D here — see the ONAM note in
# convert_SOUN for the worldspace-wide siege-engine thump that caused.
_DEFAULT_3D_MAX_DIST = 1800.0
_SOPM_ONAM_CHANNELS = bytes.fromhex(
    '646400003232323264000000640064000064000000640064')
# ANAM: unknown[4] minDistance(f32) maxDistance(f32) curve[5] unknown[3]
_SOPM_ANAM_LEAD = bytes.fromhex('809dfa00')   # most common in vanilla (24/69)
_SOPM_ANAM_TAIL = b'\x00\x00\x00'             # most common in vanilla (56/69)
# Standard vanilla falloff curve, shared by every SOMMono*/SOMStereoRad* model
_SOPM_CURVE = bytes((100, 50, 20, 5, 0))


def _build_sopm(writer, min_dist: float, max_dist: float, stereo: bool) -> int:
    """Get-or-create a Sound Output Model with the given attenuation distances.

    Skyrim does not store falloff distances on the sound itself — they live in
    the SOPM the SNDR's ONAM points at (vanilla ships one per distance:
    SOMMono00400, SOMMono03000, SOMMono10000, ...).  Oblivion instead stores the
    distances per-SOUN in SNDX, so we mint a SOPM per distinct distance pair and
    cache it, rather than pinning every sound to a single model.

    Returns the SOPM FormID.
    """
    cache = getattr(writer, '_sopm_cache', None)
    if cache is None:
        cache = writer._sopm_cache = {}
    key = (round(min_dist), round(max_dist), stereo)
    if key in cache:
        return cache[key]

    # SHARED across every SOUN with the same attenuation, so it cannot key off
    # one source record. The key is the rounded TES4 min/max distances, which
    # are authored values carried straight from the export.
    fid = writer.derive_formid('SOPM', key)
    kind = 'Stereo' if stereo else 'Mono'
    subs = pack_string_subrecord(
        'EDID', f'TES4_SOM{kind}{round(max_dist):05d}_{round(min_dist):05d}')
    # NAM1: Flags(u8) unknown[2] ReverbSend%(u8).  Flag 0x01 = Attenuates With
    # Distance — required, or the sound plays at full volume everywhere.
    #
    # DO NOT "fix" this to (30, 0, 0x01). The Skyrim.esm dump prints NAM1 as a
    # little-endian u32, so its `NAM1=1E000001` is the bytes 01 00 00 1E on
    # disk — identical to what this line writes. Reversing it on 2026-08-05
    # made every sound in the game play far too loud (confirmed in-game),
    # because it moved the reverb percentage into the flags byte.
    subs += pack_subrecord('NAM1', struct.pack('<BHB', 0x01, 0, 30))
    # MNAM: 0 = Uses HRTF (mono), 1 = Defined Speaker Output (stereo)
    subs += pack_uint32_subrecord('MNAM', 1 if stereo else 0)
    if stereo:
        subs += pack_subrecord('ONAM', _SOPM_ONAM_CHANNELS)
    subs += pack_subrecord('ANAM', _SOPM_ANAM_LEAD
                           + struct.pack('<ff', min_dist, max_dist)
                           + _SOPM_CURVE + _SOPM_ANAM_TAIL)
    writer.add_record('SOPM', pack_record('SOPM', fid, 0, subs))
    cache[key] = fid
    return fid


# Root of the extracted TES4 assets for the plugin being imported, set by
# set_sound_source_dir() at import start. Used only to enumerate the .wav files
# inside a directory-valued SOUN.FNAM (see _sound_anam_paths).
_SOUND_SOURCE_DIR = None

# Audio containers Skyrim SE plays natively, in the order convert_sounds copies
# them through to output/<plugin>/sound/tes4/ (it copies non-voice sounds as-is).
_AUDIO_EXTS = ('.wav', '.xwm', '.mp3')


def set_sound_source_dir(asset_root: str) -> None:
    """Point the SOUN converter at this plugin's extracted assets.

    🛑 The ASSET root (the folder holding `sound/`), not the record dir. For an
    imported mod those are two different folders, and the record dir has no
    `sound/` -- every directory-valued FNAM then fell back to its bare literal.
    Callers hold a record dir, so wrap it: `assets_for(export_dir)`.

    Only needed to expand directory-valued FNAMs; a missing/None dir simply
    means such sounds fall back to the single literal path (see
    _sound_anam_paths).
    """
    global _SOUND_SOURCE_DIR
    _SOUND_SOURCE_DIR = asset_root


# TES4 SOUN FormID (low 24 bits) → the SNDR FormID convert_SOUN gave its
# companion. Filled DURING Phase 3, and read afterwards to patch the actor
# records that reference it (see actors._actor_sound_subs / patch_actor_sounds).
#
# An earlier version reserved these ids in a Phase 0 pre-pass so actors could
# embed them directly. That allocated ~1100 FormIDs before anything else and
# so SHIFTED every other generated id (OTFT, ARMA, TXST, ...) — which silently
# invalidated the separately-built 'Slot44 Patch.esp', whose 818 ARMO/233 ARMA
# overrides are matched to the master BY FORMID. NPCs lost their armor. The
# allocation ORDER is therefore a compatibility contract with anything built
# against a previous run: never insert an allocating pass ahead of existing
# ones.
_SNDR_FOR_SOUN = {}


def reset_sound_descriptors() -> None:
    """Clear the SOUN→SNDR map at the start of an import run."""
    _SNDR_FOR_SOUN.clear()


def record_sndr_for_soun(soun_fid: int, sndr_fid: int) -> None:
    """Note the companion SNDR convert_SOUN just built for a SOUN."""
    _SNDR_FOR_SOUN[soun_fid & 0x00FFFFFF] = sndr_fid


def sndr_map() -> dict:
    """TES4 SOUN id (low 24 bits) → companion SNDR FormID, for CSDI patching."""
    return _SNDR_FOR_SOUN


def get_sndr_for_soun(soun_fid: int) -> int:
    """The SNDR FormID for a TES4 SOUN, or 0 if it has no companion.

    Accepts a FormID in either raw or load-order-offset form; the reservation
    map is keyed on the low 24 bits (same convention as outfits.load_item_index).
    """
    return _SNDR_FOR_SOUN.get(soun_fid & 0x00FFFFFF, 0)


# TES4 SOUN id (low 24 bits) -> (EditorID, FNAM filename).  WTHR converts
# before the SOUN phase, so the weather sound classifier cannot read the SOUN
# record itself; this index is loaded up front from the export (and from the
# MASTER export, so a plugin whose weathers cite master-owned sounds still
# classifies them — see CLAUDE.md 'master-export blindness').
_SOUN_IDENTITY = {}


def reset_soun_identity() -> None:
    """Clear the SOUN identity index at the start of an import run."""
    _SOUN_IDENTITY.clear()


def load_soun_identity(records) -> None:
    """Index (EditorID, filename) for every SOUN record in *records*.

    Safe to call more than once; later calls add without clobbering, so the
    plugin's own SOUNs are loaded after the master's and win on collision.
    """
    for rec in records:
        fid = get_formid(rec, 'FormID')
        if not fid:
            continue
        _SOUN_IDENTITY[fid & 0x00FFFFFF] = (
            get_str(rec, 'EditorID') or '',
            get_str(rec, 'FNAM.Filename') or '',
        )


def get_soun_identity(soun_fid: int) -> tuple:
    """(EditorID, filename) for a TES4 SOUN id, or ('', '') when unknown."""
    return _SOUN_IDENTITY.get(soun_fid & 0x00FFFFFF, ('', ''))


def _shipped_name(name: str) -> str:
    """The on-disk name convert_sounds writes for a source audio file.

    Non-voice audio keeps its extension; only .mp3 is transcoded (to PCM .wav)
    because the SSE exe has no mp3 support. Keep this in lockstep with
    asset_convert.audio_converter.convert_sounds — an ANAM naming an extension
    the sound stage does not produce is a reference to a file that isn't there,
    and the sound is silently dropped.
    """
    stem, ext = os.path.splitext(name)
    return stem + '.wav' if ext.lower() == '.mp3' else name


def _sound_path(name: str) -> str:
    """One SNDR ANAM value: `tes4\\<path>`, relative to `Sound\\`.

    Both forms are legal and both work: 3512 vanilla ANAM values are rooted at
    the data folder (`Data\\Sound\\FX\\...`) and 707 are relative like ours
    (`fx\\npc\\dragon\\npc_dragon_breathe_lp.wav`). Prefixing `Data\\Sound\\`
    was tried on 2026-08-05 and changed nothing in-game, so the relative form
    stands — it is the one this pipeline has always written.
    """
    return _prefix_path(_shipped_name(name))


def _sound_anam_paths(filename: str) -> list:
    """The ANAM values for one TES4 SOUN.FNAM, as a list of TES5 sound paths.

    Oblivion lets a SOUN name a DIRECTORY instead of a file, and the engine
    picks one of its files at random per play — 6 of the goblin's 7 sound slots
    are authored this way, and it is how every creature gets varied vocal
    lines. Skyrim has the same feature but expresses it differently: the
    variants are listed explicitly, one ANAM per file, and the engine
    randomises across them (vanilla NPCWolfHowl lists 5 wav ANAMs).

    A single ANAM naming a bare directory is not a sound Skyrim can open, so
    every such SOUN was silent — the CREA sound channel converted to records
    that could never play. Enumerating the extracted folder reproduces
    Oblivion's random-variant behaviour with Skyrim's own mechanism.

    Falls back to the literal path when the source folder is unavailable or
    empty, which is also the correct handling for an ordinary file-valued FNAM.

    Every ANAM must name the file the sound stage actually writes. Non-voice
    audio is copied through with its extension intact (PCM .wav, exactly as
    vanilla ships it — see audio_converter.convert_sounds), so the only
    rewrite needed is .mp3 -> .wav, which that stage transcodes because the
    SSE exe has no mp3 support.
    """
    literal = [_sound_path(filename)]
    # A file-valued FNAM already names a playable asset — nothing to expand.
    if os.path.splitext(filename)[1]:
        return literal
    if not _SOUND_SOURCE_DIR:
        return literal

    rel = filename.replace('/', '\\').strip('\\')
    src = os.path.join(_SOUND_SOURCE_DIR, 'sound', *rel.split('\\'))
    try:
        entries = sorted(os.listdir(src))
    except OSError:
        return literal

    # Sorted so the ANAM order — and therefore the output ESM — is
    # byte-reproducible across runs (the determinism contract).
    variants = [_sound_path(f'{rel}\\{e}') for e in entries
                if e.lower().endswith(_AUDIO_EXTS)]
    return variants or literal


def convert_SOUN(rec: dict, writer=None) -> tuple:
    """SOUN — needs companion SNDR record in TES5.
    Returns (soun_bytes, sndr_bytes_or_None, sndr_formid).

    SOUN order: EDID OBND SDSC
    SNDR order: EDID CNAM GNAM SNAM ANAM[] ONAM LNAM BNAM

    Volume in Skyrim comes from two places, and both must be carried over from
    TES4 or every sound plays far louder than vanilla:
      * SNDR BNAM 'Static Attenuation (db)' — a per-sound volume trim.  Oblivion
        stores the same value in SNDX bytes 8-9 (95% of Oblivion.esm SOUNs set
        it; median 6.6 dB).
      * The SOPM's min/max attenuation distance — how fast the sound falls off
        with distance.  Oblivion stores these in SNDX bytes 0-1.
    """
    subs = b''
    edid = get_str(rec, 'EditorID')
    if edid:
        subs += pack_string_subrecord('EDID', edid)
    subs += pack_obnd()

    # SDSC → link to SNDR
    sndr_fid = 0
    sndr_bytes = None
    filename = get_str(rec, 'FNAM.Filename')
    if filename and writer:
        # SNDX and SNDD hold the same struct; whichever is present wins.
        pfx = 'SNDD' if rec.get('SNDD.MaxAttDist') is not None else 'SNDX'
        tes4_flags = get_int(rec, f'{pfx}.Flags') or 0
        # TES4 stores the distances scaled down: min x5, max x100 (xEdit wbMul).
        min_dist = (get_int(rec, f'{pfx}.MinAttDist') or 0) * 5.0
        max_dist = (get_int(rec, f'{pfx}.MaxAttDist') or 0) * 100.0
        # Static attenuation is a u16 of hundredths of a dB in both games, so it
        # transfers as a raw value with no rescaling.
        static_atten = get_int(rec, f'{pfx}.StaticAttenuation') or 0

        # Only the 2D bit means "not positional".  Menu Sound (bit 5) is a UI
        # ROUTING flag and says nothing about spatialization — see the category
        # note below for what conflating the two did.
        is_2d = bool(tes4_flags & _TES4_SND_2D)

        sndr_fid = writer.derive_formid('SNDR', get_formid(rec, 'FormID'))
        # Actors reference this descriptor by TES4 SOUN id; they are already
        # written, so the CSDI placeholders get patched afterwards (see
        # actors.patch_actor_sounds).
        record_sndr_for_soun(get_formid(rec, 'FormID'), sndr_fid)
        sndr_subs = b''
        sndr_edid = f"TES4_{edid}_SNDR" if edid else f"TES4_SOUN_{get_formid(rec, 'FormID'):08X}_SNDR"
        sndr_subs += pack_string_subrecord('EDID', sndr_edid)
        # CNAM = Descriptor Type constant (0x1EEF540A — matches all vanilla SNDR records)
        sndr_subs += pack_uint32_subrecord('CNAM', 0x1EEF540A)
        # GNAM = audio category.  A 2D LOOPING TES4 sound is an ambience bed
        # (weather winds, interior drones) and must land in AudioCategoryAMB
        # (0x7F80B) like every vanilla AMBWeather* descriptor — filed under
        # AudioCategorySFX it sits in the wrong mix bus, ignores the ambience
        # slider/ducking, and Oblivion's weather winds played LOUD over
        # everything.  One-shots and 3D sounds stay SFX (0x172A1).
        #
        # THE 2D TEST IS BIT 6 ALONE.  It used to accept Menu Sound (bit 5)
        # as well, but xEdit (wbDefinitionsTES4 'Flags') lists them as
        # SEPARATE flags — bit 5 'Menu Sound' is a UI routing hint, bit 6 '2D'
        # is what actually means non-positional.  Three Nehrim sounds are
        # LOOP|MENU without 2D — AMBFireSmallLP, AMBFireMediumLP and a forest
        # birds loop — all genuinely POSITIONAL world sounds attached to fire
        # pits.  Promoting them to the global ambience bus detached them from
        # their emitter, so a fire pit crackled (and birds chirped) across the
        # entire worldspace at constant volume, in clear weather, nowhere near
        # any fire.  Confirmed by attaching to the live game: the loaded
        # descriptor TES4_AMBFireSmallLP_SNDR carried GNAM 0x0007F80B
        # (AudioCategoryAMB) with LNAM loop 0x08.
        is_loop = bool(tes4_flags & _TES4_SND_LOOP)
        if is_loop and is_2d:
            sndr_subs += pack_formid_subrecord('GNAM', 0x0007F80B)
        else:
            sndr_subs += pack_formid_subrecord('GNAM', 0x000172A1)
        # ANAM = Sound file path, one per variant (a directory-valued TES4 FNAM
        # expands to the files it holds — see _sound_anam_paths).
        for anam in _sound_anam_paths(filename):
            sndr_subs += pack_string_subrecord('ANAM', anam)
        # ONAM = Sound Output Model. Required — CK reports 'Sound Output Model
        # missing' if absent.  Only a genuinely 2D sound takes the vanilla
        # non-attenuating model; everything else gets a SOPM built from this
        # sound's own TES4 falloff distances.
        #
        # A 3D SOUND WITH max=0 MUST NOT FALL BACK TO THE 2D MODEL.  In
        # Oblivion an unset max distance means "use the engine's default
        # falloff", NOT "audible everywhere" — but _SOPM_2D is
        # SOMDialogue2D, which does not attenuate at all, so such a sound
        # played at full volume across the whole worldspace.  Nehrim's
        # siege-engine set-piece is authored exactly this way
        # (ambsiegeenginestep / _idle_lp / _foward_lp: 3D, max=0), which put a
        # repeating mechanical THUMP over open countryside far from any siege
        # engine.  Vanilla Skyrim never ships a positional loop on a
        # non-attenuating model: its SNDRs overwhelmingly use finite mono
        # falloffs (SOMMono01400 x428 incl. Player1st, 01800 x285, 02000 x225),
        # and the non-attenuating models are reserved for UI and 2D dialogue.
        # So an absent distance takes _DEFAULT_3D_MAX_DIST, matching vanilla's
        # most common falloff rather than disabling attenuation.
        if is_2d:
            onam_fid = _SOPM_2D
        else:
            onam_fid = _build_sopm(
                writer, min_dist,
                max_dist if max_dist > 0 else _DEFAULT_3D_MAX_DIST,
                stereo=False)
        sndr_subs += pack_formid_subrecord('ONAM', onam_fid)
        # LNAM = Loop Data struct (4 bytes): byte[0]=Unknown, byte[1]=Looping enum,
        # byte[2]=Unknown, byte[3]=Rumble.  Looping enum: 0x00=None, 0x08=Loop.
        lnam_value = 0x00000800 if (tes4_flags & _TES4_SND_LOOP) else 0
        sndr_subs += pack_subrecord('LNAM', struct.pack('<I', lnam_value))
        # BNAM = Values: FreqShift(S8) FreqVariance(S8) Priority(U8) dbVariance(U8) StaticAttenuation(U16)
        freq_adj = get_int(rec, f'{pfx}.FreqAdj') or 0
        freq_var = 0 if not (tes4_flags & _TES4_SND_RANDOM_FREQ_SHIFT) else 10
        sndr_subs += pack_subrecord(
            'BNAM', struct.pack('<bbBBH', max(-128, min(127, freq_adj)),
                                freq_var, 128, 0, min(65535, static_atten)))
        sndr_bytes = pack_record('SNDR', sndr_fid, 0, sndr_subs)

    if sndr_fid:
        subs += pack_formid_subrecord('SDSC', sndr_fid)
        # Records written BEFORE Phase 3 (DOOR sound slots) hold this
        # SOUN's id as a placeholder; register the descriptor so their
        # patch pass can resolve it.
        record_sndr_for_soun(get_formid(rec, 'FormID'), sndr_fid)

    soun_bytes = pack_record('SOUN', get_formid(rec, 'FormID'), get_int(rec, 'RecordFlags'), subs)
    return soun_bytes, sndr_bytes, sndr_fid


# Skyrim.esm 0x161 'DefaultImageSpaceExterior'.  ONLY a last-resort fallback
# for a weather whose TES4 record carries no HNAM — see _wthr_imgs for why it
# must not be used as the general target.
_DEFAULT_IMGS = 0x00000161

# --- HDR tone mapping: TES4 WTHR.HNAM -> TES5 IMGS.HNAM --------------------
#
# This is the field that decides overall scene exposure, and the two games put
# it in DIFFERENT RECORDS: Oblivion stores HDR per WEATHER (WTHR.HNAM, 14
# floats), Skyrim stores it in an IMAGESPACE that the weather points at
# (WTHR.IMSP -> IMGS.HNAM, 9 floats).  There is no TES5 WTHR field for it.
#
# Pointing every converted weather at the stock 0x161 is NOT a valid
# conversion: 0x161 is one of only two vanilla imagespaces that ship ENAM and
# NO HNAM (the other is 0x160 Interior), so the HDR block is left undefined and
# the scene renders blown-out at every hour.  332 of the 336 IMSP references in
# Skyrim.esm point at imagespaces that DO have an HNAM.  So convert the TES4
# HDR data into a real IMGS per weather.
#
# Field correspondence (xEdit wbDefinitionsTES5 IMGS/HNAM + UESP
# 'Skyrim Mod:Mod File Format/IMGS', whose note names slots 5/6 the
# "target luminance" pair — i.e. exactly TES4's TargetLum/UpperLumClamp):
#
#   TES5 slot            <- TES4 WTHR.HNAM field       treatment
#   0 Eye Adapt Speed    <- EyeAdaptSpeed              UNIT change (see below)
#   1 Bloom Blur Radius  <- (engine constant)          7.0 in all 213 vanilla
#   2 Bloom Threshold    <- BrightClamp                blend toward slot median
#   3 Bloom Scale        <- BrightScale                rescale 1..3 -> 2.5..4
#   4 Recv Bloom Thresh  <- TargetLum                  rescale (see table)
#   5 White              <- UpperLumClamp              rescale (see table)
#   6 Sunlight Scale     <- SunlightDimmer             rescale + per-slot bias
#   7 Sky Scale          <- (no TES4 source)           from own sky luminance
#   8 Eye Adapt Strength <- (no TES4 source)           vanilla per-slot values
#
# EyeAdaptSpeed is the one genuine UNIT change.  Oblivion's is a 0..1 rate
# (`fEyeAdaptSpeed:BlurShaderHDR`, engine default 0.7 — the setting is in
# Oblivion.ini and named in Oblivion.exe at 0xA3E965); Skyrim's weather-used
# span is 15..50.  Copying 0.7 into a field the engine reads on that scale all
# but freezes eye adaptation, so the TES4 0..1 rate is mapped onto the range.
_TES4_EYE_ADAPT_DEFAULT = 0.7

# Per-field (min, max) observed across the 213 vanilla imagespaces that a
# Skyrim.esm WEATHER actually references.  Interior/dungeon imagespaces are
# excluded: they are half the 268 total and pull the envelope somewhere no
# outdoor weather ever sits.
#
# Every converted value is clamped into this envelope, because a TES4 value
# outside it is not merely unusual — it is degenerate for Skyrim's tonemapper.
# `DefaultWeather`, the weather the engine falls back to for the 57
# worldspaces with no CNAM (Tamriel and every city), ships an ALL-ZERO HNAM in
# Oblivion.esm; copied verbatim that gives White=0 and SunlightScale=0, i.e. a
# zero white point, and the whole scene renders blown out at every hour.
_IMGS_HNAM_RANGES = (
    (15.0, 50.0),    # 0 Eye Adapt Speed
    (0.8, 8.0),      # 1 Bloom Blur Radius
    (0.0, 0.80),     # 2 Bloom Threshold
    (0.0, 7.0),      # 3 Bloom Scale
    (0.2, 1.0),      # 4 Receive Bloom Threshold
    (0.6, 1.075),    # 5 White
    (0.4, 3.85),     # 6 Sunlight Scale
    (0.0, 0.45),     # 7 Sky Scale
    (1.0, 30.0),     # 8 Eye Adapt Strength
)

# TES5 gives a weather FOUR imagespaces, one per time of day, and 59 of the 84
# vanilla weathers (70%) really do use distinct ones — day and night differ a
# lot (SkyScale 0.235 day vs 0.02 night on SkyrimClear).  TES4 has a single
# HDR block per weather with no time axis, so the fields TES4 cannot supply
# are taken from the vanilla PER-SLOT medians (measured over all 84 weathers
# x their 4 slots):
#
#                    Sunrise   Day   Sunset  Night
#   Bloom Threshold    0.375  0.625   0.475  0.375
#   Eye Adapt Speed   37      40     37     45
#   Eye Adapt Strength 15      5      15     20
#   Sky Scale          0.175  0.210   0.200  0.035
#
# Collapsing all four slots onto one imagespace gives day and night identical
# tone mapping.
_IMGS_SLOT_NAMES = ('Dawn', 'Day', 'Dusk', 'Night')
_IMGS_SLOT_EYE_ADAPT_STRENGTH = (15.0, 5.0, 15.0, 20.0)
# Eye-adapt speed multiplier relative to the weather's own TES4 rate, so an
# authored TES4 value still drives the result but keeps vanilla's day/night
# shape (medians 37/40/37/45 -> normalised against the 40 day value).
_IMGS_SLOT_EYE_ADAPT_BIAS = (0.925, 1.0, 0.925, 1.125)
# Vanilla also dims Sunlight Scale after dark (per-slot medians
# 1.85/1.90/1.85/1.50); TES4 has one value for the whole day.
_IMGS_SLOT_SUNLIGHT_BIAS = (0.974, 1.0, 0.974, 0.789)

# SKY SCALE MUST NOT BE DERIVED FROM SKY COLOUR.
#
# Sky Scale is a tonemapper term applied to the sky, and in Skyrim's sky pixel
# shader it lands as an ADDITIVE floor:
#
#     psout.Color.xyz = input.Color.xyz * baseColor.xyz + skyScale
#
# (Community Shaders `Sky.hlsl`; the constant is PParams.y, set in
# BSSkyShader.cpp as fInvFrameBufferRange * [sky+0xE4] and forced to 0 for
# moons/sunglare.)  Oblivion's sky shader has NO additive term at all — its
# dome pixel shader is literally `mov r0, v0` — so nothing in a TES4 weather
# should drive this.
#
# The previous revision ramped Sky Scale off the weather's own sky luminance.
# That scales an ADDITIVE floor in proportion to the MULTIPLICATIVE colour,
# i.e. a feedback loop: brighter sky -> bigger floor -> brighter sky.  It is
# invisible to any per-field range check, which is why every clamping pass
# missed it.  Measured on the shipped output, it made our Sky Scale a near
# deterministic function of sky brightness where vanilla's is only loosely
# related:
#
#     SkyUpper vs SkyScale   vanilla r = +0.407   ours r = +0.885
#     Horizon  vs SkyScale   vanilla r = +0.374   ours r = +0.819
#
# What vanilla actually keys it off, over the 332 weather/time rows that
# resolve an imagespace: classification x time explains R2 = 0.434, sky
# luminance only R2 = 0.166.  So this is a LOOKUP on two authored TES4
# fields (DATA.Classification and the time slot), with no colour dependence —
# which also makes it generalise to plugins we have never seen.
#
# Medians measured over all 177 vanilla WTHR in Skyrim.esm + Update.esm +
# Dawnguard.esm + Dragonborn.esm (tools/skyscale_authoring.py).
# Index: [classification bit][time], times = Sunrise, Day, Sunset, Night.
_IMGS_SKY_SCALE_BY_CLASS = {
    0x01: (0.080, 0.120, 0.100, 0.020),   # Pleasant
    0x02: (0.050, 0.100, 0.050, 0.000),   # Cloudy
    0x04: (0.090, 0.100, 0.100, 0.060),   # Rainy
    0x08: (0.100, 0.050, 0.050, 0.050),   # Snow
}
# Weathers with no classification bit set (10 of Oblivion's 37, including all
# the Deadlands skies).  Vanilla's own unclassified weathers sit flat at 0.05.
_IMGS_SKY_SCALE_UNCLASSIFIED = (0.050, 0.050, 0.050, 0.050)

# --- Median-anchored bloom/exposure mapping -------------------------------
#
# The first calibration rescaled each TES4 field's OBSERVED SPAN onto the
# vanilla span.  In-game that overexposed everything, because the TES4
# medians sit at the EDGE of their spans, not the middle: Oblivion's median
# UpperLumClamp is 1.0 (the bottom of its 1.0..1.3 span), so nearly every
# weather got White = 0.88 — the bottom DECILE of vanilla — a lowered white
# point, with bloom threshold and receive-bloom simultaneously mapped
# bloom-heavy.  The result read as an overexposed camera.
#
# The fix anchors on medians instead: a median TES4 input lands EXACTLY on
# the vanilla per-slot median (measured over the 213 imagespaces vanilla
# weathers reference), and authored deviation from the TES4 median moves the
# output away from the vanilla median by `gain`, clamped to the vanilla
# p10..p90 band for that slot.  "Base-Skyrim by default, authored variation
# on top."
#
#   value = slot_median + (tes4 - tes4_median) * gain, clamped to slot band
#
# TES4 medians measured over Oblivion.esm's 36 authored HNAMs (Nehrim's agree
# on every median; its outliers — SunlightDimmer 50, TargetLum 5.2 — are
# exactly why the p10..p90 clamp exists).
_IMGS_ANCHORED_FIELDS = {
    # tes4 field     (t4_median, gain, per-slot medians,
    #                 per-slot lo (p10),            per-slot hi (p90))
    'BrightClamp':   (0.30, 1.0, (0.375, 0.625, 0.475, 0.375),
                      (0.30, 0.30, 0.295, 0.25), (0.70, 0.715, 0.70, 0.70)),
    'BrightScale':   (2.00, 0.5, (3.0, 3.0, 3.0, 3.2),
                      (2.50, 2.35, 2.50, 2.50), (4.0, 4.0, 4.0, 4.0)),
    'TargetLum':     (1.20, 0.3, (0.55, 0.625, 0.60, 0.55),
                      (0.40, 0.50, 0.475, 0.40), (1.0, 1.0, 1.0, 1.0)),
    'UpperLumClamp': (1.00, 0.25, (0.925, 1.0, 0.90, 0.925),
                      (0.875, 0.875, 0.875, 0.875), (1.0, 1.05, 1.0, 1.0)),
}

# SunlightDimmer keeps a span rescale — TES4's 0.5..2.0 maps onto vanilla's
# 0.9..2.7 with the medians already aligned (1.3 -> 1.86 vs vanilla 1.9).
_IMGS_FIELD_RESCALE = {
    'SunlightDimmer': (0.50, 2.00, 0.90, 2.70),
}

# Bloom Blur Radius is 7.0 in ALL 213 vanilla weather-used imagespaces — it is
# an engine constant, not authored data.  TES4's BlurRadius belongs to
# Oblivion's own blur pass (a different quantity that happens to share a name)
# and ranges 4..8; feeding it through made the bloom kernel too tight.
_IMGS_BLOOM_BLUR_RADIUS = 7.0


def _rescale(name: str, value: float, default: float) -> float:
    """Map a TES4 HDR value from its own range onto the vanilla TES5 range."""
    span = _IMGS_FIELD_RESCALE.get(name)
    if span is None:
        return value
    lo_in, hi_in, lo_out, hi_out = span
    if hi_in <= lo_in:
        return default
    t = (value - lo_in) / (hi_in - lo_in)
    t = min(max(t, 0.0), 1.0)
    return lo_out + t * (hi_out - lo_out)


def _imgs_sky_scale(rec: dict, time: int) -> float:
    """IMGS Sky Scale for a time of day, from the weather's classification.

    Keyed on AUTHORED TES4 data only (DATA.Classification + the time slot), so
    it never feeds sky colour back into an additive shader term, and it works
    for plugins whose weathers we have never seen.

    Unclassified weathers keep vanilla's flat unclassified value rather than
    being promoted to Pleasant the way _wthr_flags does — 10 of Oblivion's 37
    weathers are unclassified, including the Deadlands skies, and giving them
    Pleasant's daytime scale lights them like a clear Skyrim afternoon.
    """
    cls = get_int(rec, 'DATA.Classification') & _WTHR_CLASSIFICATION_MASK
    for bit in (0x01, 0x02, 0x04, 0x08):
        if cls & bit:
            return _IMGS_SKY_SCALE_BY_CLASS[bit][time]
    return _IMGS_SKY_SCALE_UNCLASSIFIED[time]


def _wthr_imgs(rec: dict, imgs_fid: int, time: int) -> bytes:
    """Build one time-of-day IMGS carrying this weather's HDR tone mapping."""
    # A TES4 weather whose whole HNAM block is zero has no authored HDR at all
    # (Oblivion's CS supplied the defaults, so the record was never filled in).
    # DefaultWeather is one, and it is the weather the 57 CNAM-less
    # worldspaces fall back to.  Clamping those zeros to the range MINIMUM
    # pins every field at its darkest/flattest legal value; fall back to the
    # vanilla per-slot defaults instead, which is what Oblivion itself did.
    authored = any(
        get_float(rec, 'HNAM.' + f, 0.0) != 0.0
        for f in ('EyeAdaptSpeed', 'BlurRadius', 'TargetLum', 'UpperLumClamp',
                  'BrightScale', 'BrightClamp', 'SunlightDimmer'))

    def anchored(field):
        """Vanilla slot median, moved by the weather's authored deviation."""
        t4_med, gain, meds, los, his = _IMGS_ANCHORED_FIELDS[field]
        v = meds[time]
        if authored:
            v += (get_float(rec, 'HNAM.' + field, t4_med) - t4_med) * gain
        return min(max(v, los[time]), his[time])

    # Oblivion 0..1 adaptation rate -> Skyrim's scale, then biased per slot.
    speed = _TES4_EYE_ADAPT_DEFAULT
    if authored:
        speed = get_float(rec, 'HNAM.EyeAdaptSpeed', _TES4_EYE_ADAPT_DEFAULT)
    speed = max(0.0, min(1.0, speed))
    lo, hi = _IMGS_HNAM_RANGES[0]
    eye_adapt = (lo + speed * (hi - lo)) * _IMGS_SLOT_EYE_ADAPT_BIAS[time]

    # Sunlight Scale: span rescale (medians already aligned), then the
    # vanilla day/night shape on top.
    sunlight = 1.9
    if authored:
        sunlight = _rescale('SunlightDimmer',
                            get_float(rec, 'HNAM.SunlightDimmer', 1.3), 1.9)
    sunlight *= _IMGS_SLOT_SUNLIGHT_BIAS[time]

    # Sky Scale: a LOOKUP on authored classification + time.  See
    # _IMGS_SKY_SCALE_BY_CLASS — deriving this from sky luminance (as an
    # earlier revision did) drives an additive shader term from the
    # multiplicative colour and is the bloom feedback loop.
    sky_scale = _imgs_sky_scale(rec, time)

    # Defaults are the vanilla weather-used per-slot medians, so a weather
    # with no authored TES4 HDR renders like a normal Skyrim exterior.
    values = [
        eye_adapt,
        _IMGS_BLOOM_BLUR_RADIUS,       # Bloom Blur Radius (vanilla constant)
        anchored('BrightClamp'),       # Bloom Threshold
        anchored('BrightScale'),       # Bloom Scale
        anchored('TargetLum'),         # Receive Bloom Threshold
        anchored('UpperLumClamp'),     # White
        sunlight,                      # Sunlight Scale
        sky_scale,
        _IMGS_SLOT_EYE_ADAPT_STRENGTH[time],
    ]
    values = [min(max(v, lo), hi)
              for v, (lo, hi) in zip(values, _IMGS_HNAM_RANGES)]
    hnam = struct.pack('<9f', *values)

    edid = get_str(rec, 'EditorID') or f"WTHR{get_formid(rec, 'FormID'):08X}"
    subs = pack_string_subrecord(
        'EDID', f'TES4_{edid}_IMGS{_IMGS_SLOT_NAMES[time]}')
    subs += pack_subrecord('HNAM', hnam)
    # Neutral cinematic/tint: TES4 has no equivalent, and every vanilla IMGS
    # with an HNAM also ships CNAM+TNAM.
    subs += pack_subrecord('CNAM', struct.pack('<3f', 1.0, 1.0, 1.0))
    subs += pack_subrecord('TNAM', struct.pack('<4f', 0.0, 1.0, 1.0, 1.0))
    # DNAM — depth of field + Sky/Blur Radius.  213 of the 214 weather-used
    # vanilla imagespaces ship it; omitting it leaves DoF and sky blur
    # UNDEFINED.  Use the MODAL COMPLETE vanilla tuple (19 records ship
    # exactly this), not per-field medians, so the combination is coherent:
    # Strength 0.5, Distance 20000, Range 20000, Sky/Blur 16816 =
    # "No Sky, Radius 2" — distant-only DoF and the sky EXCLUDED from blur.
    subs += pack_subrecord('DNAM', struct.pack(
        '<3f2xH', 0.5, 20000.0, 20000.0, 16816))
    return pack_record('IMGS', imgs_fid, 0, subs)


# EditorIDs Oblivion and Skyrim both use.  Ours is remapped into index 01 and
# would otherwise make the CK rename it to '<name>DUPLICATE001'.
_WTHR_EDID_COLLISIONS = frozenset({'DefaultWeather'})

# TES4 DATA 'Flags' classification bits.  TES5 defines the same four in the
# same positions and adds two aurora bits TES4 has no source for, so the low
# nibble passes straight through.
_WTHR_CLASSIFICATION_MASK = 0x0F


def _wthr_flags(rec: dict) -> int:
    """TES5 DATA weather-classification flags from the TES4 classification byte.

    Bits 0-3 (Pleasant/Cloudy/Rainy/Snow) are shared; bits 4-5 are TES5's
    aurora controls.  Weather with no classification at all is treated as
    Pleasant so the engine's weather-transition picker can still match it.
    """
    flags = get_int(rec, 'DATA.Classification') & _WTHR_CLASSIFICATION_MASK
    if flags == 0:
        flags = 0x01  # Weather - Pleasant
    return flags


# Vanilla Skyrim.esm shader-particle systems (SPGD) for precipitation.
# Skyrim.esm is always master index 0 of the output, so the raw ids resolve.
_SPGD_RAIN       = 0x00023C48   # RainParticles (used by 7 vanilla weathers)
_SPGD_RAIN_STORM = 0x0010780F   # RainStormParticles
_SPGD_SNOW       = 0x00023C49   # SnowParticlesMed

# TES4/TES5 shared classification bits (low nibble of DATA flags).
_WTHR_CLASS_RAINY = 0x04
_WTHR_CLASS_SNOW  = 0x08

# ThunderFrequency is inverted in both games: 255 = never.
_WTHR_THUNDER_NEVER = 255


def _wthr_precipitation(rec: dict) -> int:
    """Pick the vanilla SPGD matching this weather's authored classification.

    Snow wins over rain if both bits are set (visually dominant); rain splits
    on authored thunder into the storm variant, mirroring vanilla usage where
    SkyrimStormRain uses RainStormParticles and plain rain uses RainParticles.
    """
    flags = get_int(rec, 'DATA.Classification') & _WTHR_CLASSIFICATION_MASK
    if flags & _WTHR_CLASS_SNOW:
        return _SPGD_SNOW
    if flags & _WTHR_CLASS_RAINY:
        # The rainy bit only exists when DATA was authored, so a frequency of
        # 0 here is authored "constant thunder", not a missing field.
        if get_int(rec, 'DATA.ThunderFrequency') < _WTHR_THUNDER_NEVER:
            return _SPGD_RAIN_STORM
        return _SPGD_RAIN
    return 0


def _wthr_cloud_sig(layer: int) -> bytes:
    """Build the 4-byte cloud texture signature for a given layer index (0-28).

    Layer 0-16:  first byte is chr(0x30 + layer), rest is '0TX'
                 e.g. layer 0 = '00TX', layer 1 = '10TX', layer 10 = ':0TX'
    Layer 17-28: first byte is chr(0x41 + (layer - 17)), rest is '0TX'
                 e.g. layer 17 = 'A0TX', layer 18 = 'B0TX'
    """
    if layer <= 16:
        return bytes([0x30 + layer]) + b'0TX'
    else:
        return bytes([0x41 + (layer - 17)]) + b'0TX'


# --- NAM0 weather color tables -------------------------------------------
#
# TES4 NAM0 is 10 color types x 4 times-of-day x RGBA = 160 bytes.
# TES5 NAM0 is 17 color types x 4 times-of-day x RGBA = 272 bytes (verified
# against references/Skyrim.esm: 72 of 84 vanilla WTHR use the full 272; the
# short 224/208 variants are older form versions).
#
# Times-of-day are in the same order in both games (Sunrise, Day, Sunset,
# Night), so only the TYPE axis needs remapping.  Index = TES5 slot, value =
# the TES4 slot it is sourced from, or None for a TES5-only slot.
#
# TES4 order (wbDefinitionsTES4 wbWeatherColors):
#   0 Sky-Upper, 1 Fog, 2 Clouds-Lower, 3 Ambient, 4 Sunlight, 5 Sun,
#   6 Stars, 7 Sky-Lower, 8 Horizon, 9 Clouds-Upper
_T4_SKY_UPPER, _T4_FOG, _T4_CLOUDS_LOWER, _T4_AMBIENT = 0, 1, 2, 3
_T4_SUNLIGHT, _T4_SUN, _T4_STARS, _T4_SKY_LOWER = 4, 5, 6, 7
_T4_HORIZON, _T4_CLOUDS_UPPER = 8, 9

# TES5 order (wbDefinitionsCommon wbWeatherColors, gmTES5 branch):
#   0 Sky-Upper, 1 Fog Near, 2 Unused, 3 Ambient, 4 Sunlight, 5 Sun, 6 Stars,
#   7 Sky-Lower, 8 Horizon, 9 Effect Lighting, 10 Cloud LOD Diffuse,
#   11 Cloud LOD Ambient, 12 Fog Far, 13 Sky Statics, 14 Water Multiplier,
#   15 Sun Glare, 16 Moon Glare
_NAM0_TES5_FROM_TES4 = [
    _T4_SKY_UPPER,      # 0  Sky-Upper
    _T4_FOG,            # 1  Fog Near
    None,               # 2  Unused (TES4 had Clouds-Lower here)
    _T4_AMBIENT,        # 3  Ambient
    _T4_SUNLIGHT,       # 4  Sunlight
    _T4_SUN,            # 5  Sun
    _T4_STARS,          # 6  Stars
    _T4_SKY_LOWER,      # 7  Sky-Lower
    _T4_HORIZON,        # 8  Horizon
    None,               # 9  Effect Lighting — no TES4 source
    _T4_CLOUDS_UPPER,   # 10 Cloud LOD Diffuse  <- TES4 upper cloud tint
    _T4_CLOUDS_LOWER,   # 11 Cloud LOD Ambient  <- TES4 lower cloud tint
    _T4_FOG,            # 12 Fog Far — TES4 had a single fog color
    None,               # 13 Sky Statics — see _NAM0_SLOT_DEFAULTS
    None,               # 14 Water Multiplier
    None,               # 15 Sun Glare
    None,               # 16 Moon Glare
]

# TES5-only slot defaults, keyed by weather CLASSIFICATION and time of day.
#
# The earlier flat defaults (13/15/16 black, 14 white) came from a census
# that collapsed the time axis and mixed dungeon weathers in — the "mode is
# black" was an artifact.  Re-censused from the REAL Skyrim.esm (the
# references dump truncates NAM0 hex before slot 13), per classification and
# per time (2026-08-09):
#
#   13 Sky Statics    — NEVER black in vanilla.  It tints the moon discs and
#                       CLMT statics: forcing black rendered the MOONS
#                       black-on-black — stars were fine (slot 6) but no
#                       moon was ever visible.  SkyrimClear night ships a
#                       bright blue (45,137,208).
#   14 Water Multiplier — flat white was also wrong: every vanilla class
#                       ships dark teal (31,63,75) at night; white there
#                       over-brightens night water reflections.
#   15 Sun Glare      — dark browns by day on clear weathers, black at night
#                       and black all day under cloud/rain/snow.
#   16 Moon Glare     — the inverse: clear weathers ship a warm bright halo
#                       at NIGHT (255,173,138) and near-black by day; weather
#                       classes whose clouds hide the moons ship black.
#
# TES4 has no source color for any of them, so take the vanilla per-class,
# per-time MEDIAN — the classification bits are shared between the games, so
# an Oblivion thunderstorm gets Skyrim's storm treatment and a clear day gets
# Skyrim's clear treatment.  Values are (sunrise, day, sunset, night) RGB.
_NAM0_SLOT_CLASS_DEFAULTS = {
    # Pleasant (0x01) — also the fallback for unclassified weathers
    0x01: {
        13: ((174, 159, 159), (218, 222, 236), (176, 146, 133), (62, 93, 108)),
        14: ((143, 170, 186), (162, 225, 239), (79, 108, 120), (31, 63, 75)),
        15: ((74, 28, 0), (72, 58, 57), (74, 28, 0), (0, 0, 0)),
        16: ((102, 73, 49), (90, 61, 50), (96, 66, 49), (255, 173, 138)),
    },
    # Cloudy (0x02)
    0x02: {
        13: ((151, 165, 176), (172, 174, 188), (149, 161, 162), (86, 106, 124)),
        14: ((180, 190, 191), (180, 190, 199), (152, 159, 167), (31, 63, 75)),
        15: ((0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0)),
        16: ((0, 0, 0), (0, 0, 0), (0, 0, 0), (57, 22, 0)),
    },
    # Rainy (0x04)
    0x04: {
        13: ((107, 105, 100), (142, 161, 173), (98, 122, 119), (73, 85, 100)),
        14: ((172, 177, 185), (164, 196, 210), (100, 135, 176), (31, 63, 75)),
        15: ((0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0)),
        16: ((0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0)),
    },
    # Snow (0x08)
    0x08: {
        13: ((146, 140, 128), (159, 159, 162), (118, 112, 120), (75, 84, 88)),
        14: ((176, 166, 184), (188, 188, 188), (176, 166, 184), (31, 63, 75)),
        15: ((0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0)),
        16: ((0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0)),
    },
}


def _nam0_class_defaults(rec: dict) -> dict:
    """Pick the TES5-only slot table for this weather's classification.

    Snow wins over rain, rain over cloud, mirroring _wthr_precipitation."""
    flags = _wthr_flags(rec)
    for bit in (0x08, 0x04, 0x02, 0x01):
        if flags & bit:
            return _NAM0_SLOT_CLASS_DEFAULTS[bit]
    return _NAM0_SLOT_CLASS_DEFAULTS[0x01]

_TES5_NAM0_SLOTS = 17

# TES5 NAM0 slot indices we address by name (see _NAM0_TES5_FROM_TES4 for the
# full map).  Named from the decompiled TESWeather::ColorTypes enum.
_T5_STARS = 6
_TES5_CLOUD_LAYERS = 32

# --- NAM0 highlight compression (soft knee) --------------------------------
#
# THE PALETTE AND THE BLOOM ARE TWO DIFFERENT PROBLEMS.  Getting that wrong
# is what the previous revision did, and it cost a lot of in-game rounds.
#
# What the old code did: compute the PLUGIN's median luminance per slot and
# per TIME, scale every colour so that median lands on vanilla's, then cap at
# vanilla's p90.  It did stop the bloom — but by crushing the whole palette,
# and it produced two clearly wrong results in game:
#
#   * NIGHTS WENT BRIGHT BLUE.  Oblivion authors night at 7-12% of day
#     luminance.  A PER-TIME factor scaled night up 1.9-3.1x while scaling day
#     down 0.62-0.70x, so the authored day/night ratio became 22-55% — the
#     night sky stopped being dark and became a saturated blue.
#   * THE DAY SKY WENT DULL.  Authored SkyUpper day median 136.5 was pushed to
#     84.3, because the vanilla median it aimed at is pulled down by storms,
#     dungeons and Sovngarde rather than describing a clear afternoon.
#
# The measurement that settles it: the two populations AGREE at the bottom and
# only diverge at the top (all sky/light slots x times, TES4 vs the 177
# vanilla weathers):
#
#     percentile     p50    p75    p90    p95    p99
#     TES4          76.9  154.6  204.6  243.1  255.0
#     vanilla       83.8  125.8  168.0  193.5  220.3
#
# So Oblivion's colours are NOT broadly hot.  Only the top ~20% is, which is
# exactly the part that crosses the tonemapper's bright-pass threshold
# (`max(scene - BloomThreshold, 0) * BloomScale`, verified against Oblivion's
# own HDR005.pso and Skyrim's ISHDR.hlsl).  A uniform scale therefore darkens
# midtones to fix highlights.
#
# What we do instead: leave everything below the knee EXACTLY AS AUTHORED and
# remap knee..255 into knee..ceiling, scaling all three channels by the same
# factor so hue is untouched.  Because the curve is a pure function of one
# colour's luminance, it has no time axis and no plugin-population term — the
# authored day/night ratio survives bit-for-bit (measured: 0.073 authored ->
# 0.073 through the knee, against 0.253 through the old code), and a weather
# converts the same way regardless of what else is in the plugin.
#
# THE SUN SLOT IS A GENUINE OUTLIER and gets its own, much harder knee.  Its
# TES4 day median is 193.4 against vanilla's 42.5 — 4.55x, where no other slot
# exceeds 1.7x.  In Skyrim the sun's apparent brightness comes from the glare
# pass and the imagespace, not from this colour, so a near-white disc here is
# a bloom source with nothing to justify it.
#
# Chosen in game from tools/make_sky_unjustified_esp.py (variant UJkneeSun)
# against UJbase/UJraw/UJknee/UJkneeSoft/UJkneeHard/UJsunonly.
_NAM0_KNEE = 160.0            # below this, authored colour passes through
_NAM0_KNEE_CEILING = 200.0    # 255 maps here
_NAM0_SUN_KNEE = 30.0
_NAM0_SUN_CEILING = 60.0
_T5_SUN = 5

# Vanilla targets: (per-time medians, per-time p90 luminance), keyed by TES5
# NAM0 slot, measured over the 84 Skyrim.esm weathers.  RETAINED ONLY as the
# reference figures quoted above; nothing reads them any more.
_NAM0_VANILLA_LUM = {
    0:  ((86.4, 84.3, 85.4, 21.3), (161.8, 155.8, 167.3, 114.8)),   # Sky-Upper
    1:  ((85.2, 95.9, 80.5, 52.3), (137.6, 137.6, 138.4, 117.1)),   # Fog Near
    3:  ((130.0, 172.2, 105.8, 76.4), (166.0, 217.3, 178.0, 100.8)),  # Ambient
    4:  ((131.6, 151.7, 116.2, 77.7), (197.3, 214.0, 189.2, 146.4)),  # Sunlight
    5:  ((38.6, 43.0, 33.9, 0.0), (54.9, 121.1, 77.5, 0.0)),        # Sun
    6:  ((0.0, 0.0, 0.0, 236.0), (255.0, 255.0, 255.0, 255.0)),     # Stars
    7:  ((133.1, 122.6, 107.2, 47.7), (202.8, 136.0, 168.0, 89.4)),   # Sky-Lower
    8:  ((90.3, 140.2, 89.6, 30.8), (155.7, 196.6, 123.4, 89.6)),   # Horizon
    12: ((113.7, 166.4, 103.7, 38.6), (168.6, 188.9, 170.8, 113.5)),  # Fog Far
}

# Effect Lighting (slot 9) has no TES4 source and was previously written
# black — vanilla authors it BRIGHT (per-channel medians per time); black
# effect lighting renders spell/effect shaders unlit.
_NAM0_EFFECT_LIGHTING = ((150, 163, 158), (198, 193, 193),
                         (159, 129, 116), (84, 148, 166))

# Cloud layer tints (PNAM) get the same knee as the sky slots.  The old p90
# cap here was the same percentile fit as the NAM0 one and had the same flaw:
# it clamped by TIME, so a layer's authored day/night relationship moved.
_PNAM_KNEE = _NAM0_KNEE
_PNAM_KNEE_CEILING = _NAM0_KNEE_CEILING


def _lum(r, g, b):
    return 0.299 * r + 0.587 * g + 0.114 * b


def _knee_rgb(r, g, b, knee: float, ceiling: float) -> tuple:
    """Compress luminance above `knee` into `knee..ceiling`, hue preserved.

    Below the knee the colour is returned EXACTLY as authored — no scaling,
    no rounding drift.  Above it, all three channels are scaled by the same
    factor, so only brightness changes.

    This is a pure function of one colour: no time-of-day term and no
    dependence on the rest of the plugin, which is what keeps the authored
    day/night curve intact.
    """
    lum = _lum(r, g, b)
    if lum <= knee or lum <= 0.0:
        return (r, g, b)
    target = knee + (lum - knee) * (ceiling - knee) / (255.0 - knee)
    s = target / lum
    return (min(255, round(r * s)), min(255, round(g * s)),
            min(255, round(b * s)))


def _normalize_rgb(t5_slot: int, time: int, r: int, g: int, b: int) -> tuple:
    """Apply the highlight knee for a NAM0 slot.

    `time` is unused and kept only so callers read symmetrically with the
    slot/time addressing everywhere else — the whole point of the knee is
    that it has no time axis (see the block comment above).
    """
    if t5_slot == _T5_SUN:
        return _knee_rgb(r, g, b, _NAM0_SUN_KNEE, _NAM0_SUN_CEILING)
    return _knee_rgb(r, g, b, _NAM0_KNEE, _NAM0_KNEE_CEILING)

# DALC face brightness relative to NAM0's Ambient color, in xEdit's field
# order (X+, X-, Y+, Y-, Z+, Z-).  Medians measured over all 84 vanilla
# Skyrim.esm weather records; see _wthr_dalc.
_DALC_FACE_WEIGHTS = (0.98, 0.94, 0.96, 0.95, 0.67, 1.28)


def _wthr_nam0(rec: dict) -> bytes:
    """Remap the TES4 160-byte weather color table into TES5's 272-byte one.

    Returns 272 bytes: 17 color types x 4 times-of-day x RGBA.
    """
    raw = get_hex_bytes(rec, 'NAM0.Data')
    out = bytearray(_TES5_NAM0_SLOTS * 4 * 4)

    for slot, per_time in _nam0_class_defaults(rec).items():
        for time, (r, g, b) in enumerate(per_time):
            off = (slot * 4 + time) * 4
            out[off:off + 4] = bytes((r, g, b, 0))

    # Effect Lighting (slot 9): no TES4 source; vanilla authors it bright and
    # black leaves effect shaders unlit.
    for time, (r, g, b) in enumerate(_NAM0_EFFECT_LIGHTING):
        off = (9 * 4 + time) * 4
        out[off:off + 4] = bytes((r, g, b, 0))

    if not raw or len(raw) < 160:
        return bytes(out)

    for t5_slot, t4_slot in enumerate(_NAM0_TES5_FROM_TES4):
        if t4_slot is None:
            continue
        if t5_slot in (10, 11):
            # Cloud LOD Diffuse/Ambient: vanilla ships these BLACK (median
            # and p90 are 0 in all four slots); Oblivion's cloud tints here
            # lit the distant cloud LOD pass white.
            continue
        for time in range(4):
            src = (t4_slot * 4 + time) * 4
            dst = (t5_slot * 4 + time) * 4
            r, g, b = raw[src], raw[src + 1], raw[src + 2]
            if t5_slot in _NAM0_VANILLA_LUM:
                r, g, b = _normalize_rgb(t5_slot, time, r, g, b)
            out[dst:dst + 3] = bytes((r, g, b))

    # Stars (slot 6) is a VISIBILITY SWITCH in vanilla, not a continuous tint.
    # Censused over the 165 vanilla weathers that carry a full NAM0:
    #
    #   Pleasant  91.4% pure white   3.7% black
    #   Cloudy    17.8% white       60.0% black   (genuinely mixed)
    #   Rainy      4.3% white       95.7% black
    #   Snow      22.2% white       77.8% black
    #
    # Oblivion has no such convention and authors a continuous value on every
    # weather, so a converted rainstorm keeps a starfield burning through the
    # overcast.  Rain and snow are unambiguous, so blank the stars there;
    # Cloudy is left alone because vanilla itself is split on it.
    cls = get_int(rec, 'DATA.Classification') & _WTHR_CLASSIFICATION_MASK
    if cls & (0x04 | 0x08):
        for time in range(4):
            dst = (_T5_STARS * 4 + time) * 4
            out[dst:dst + 3] = b'\x00\x00\x00'
    return bytes(out)


def _cloud_speed_tes4_to_tes5(speed: int) -> int:
    """Convert a TES4 cloud-speed byte to TES5's RNAM/QNAM encoding.

    BOTH GAMES USE THE SAME PHYSICAL SCALE, so this is a real unit conversion
    rather than a passthrough:

    * TES4 stores an UNSIGNED 0..255 byte scaled against the engine setting
      `fWeatherCloudSpeedMax`, whose default is 0.1 — read straight out of
      Oblivion.exe, where the settings constructor at 0x9E5BF0 does
      `fld dword ptr [0xA2FAAC]` (= 0.1) before pushing the name string at
      0xA56C88.  So byte b means b/255 * 0.1, always forwards.
    * TES5 stores a SIGNED drift over the same -0.1..+0.1 range, encoded
      0x00 = -0.1, 0x7F = 0.0, 0xFE = +0.1 (UESP 'Skyrim Mod:Mod File
      Format/WTHR'; xEdit's wbWeatherCloudSpeedToStr computes
      (value-127)/127/10, and wbWeatherCloudSpeedToInt clamps to 254).

    Mapping TES4's 0..0.1 onto the positive half gives 0x7F..0xFE.  The old
    `0x7F + speed//2` treated the byte as if it were already TES5-encoded and
    ran clouds at up to ~10x their intended speed.
    """
    speed = max(0, min(255, speed))
    return min(254, 127 + round(speed / 255.0 * 127.0))


def _cloud_speed_signed_tes5(speed: float) -> int:
    """Encode a SIGNED TES4-scale cloud speed into TES5's RNAM/QNAM byte.

    Same scale as _cloud_speed_tes4_to_tes5, but accepts negatives so a layer
    can drift backwards.  The engine decodes this as
    `byte * 0.2/254 - 0.1` (Clouds::Update, SkyrimSE.exe 0x3c54ad-0x3c54de:
    xmm4=0.1, xmm3=0.1 XOR -0.0 = -0.1, xmm10=1/254), so 0x7F is exactly
    stationary and the usable range is 0x00..0xFE.
    """
    speed = max(-255.0, min(255.0, speed))
    return max(0, min(254, 127 + round(speed / 255.0 * 127.0)))


# Fraction of a layer's authored drift that goes on the X axis.  TES4 authors
# only a magnitude, so the axis split is ours; this keeps the motion clearly
# two-dimensional (vanilla drives both axes) without changing the overall
# rate much.  Sign is negative so X and Y differ, which is what makes the
# motion read as drift rather than a straight scroll.
_WTHR_CLOUD_X_DRIFT = -0.35


# --- Cloud dome layer plan -------------------------------------------------
#
# LAYER INDICES BIND TO SHAPES IN *VANILLA'S* DOME, AND THE SHAPES ARE NOT
# INTERCHANGEABLE -- each carries its own UV tiling.
#
# The engine loads the HARDCODED `Meshes\Sky\Clouds.nif` (string at
# SkyrimSE.exe 0x169a538).  Our converted `tes4\sky\clouds.nif` is NEVER
# used: CLMT has no cloud-mesh field at all -- its MODL is the NIGHT SKY
# (stars) mesh, per wbRecord(CLMT) in wbDefinitionsTES5.pas and
# TESClimate::nightSky in CommonLibSSE-NG.  An earlier revision assumed we
# shipped the dome and capped the plan at two layers on that basis; that was
# wrong, and it put both sheets on the two smallest shapes in the mesh.
#
# Measured UV spans of the shipped 29-shape dome:
#
#     L11 09_CDTop            U 1.58  V 1.58    ~1:1 projection
#     L27 14_CDLower          U 2.27  V 2.27    ~1:1 projection
#     L 8 07_CDDome_Horizon   U 6.00            tiles 6x around the horizon
#     L 9/10 (_E/_W)          U 2.40            same band, split
#     L15-26 12_/13_CDHorizon_*  V 0.25         narrow V-sliced STRIPS
#     L28 15_CDFog            U 21.00           tiles 21x, horizon wash
#
# Oblivion authors its two sheets as SINGLE FULL-DOME PROJECTIONS
# (`CloudDome:0` 3.35x3.35 and `CloudDome:1` 2.97x2.97, both spanning 0..90
# degrees elevation), so **11 and 27 are the only structural matches**.  Any
# heavily U-tiled shape turns a full-sky sheet into a repeating horizon band,
# which is exactly what was observed in game: a variant using 8/9/10 put all
# the cloud around the horizon instead of over the sky.
#
# Vanilla confirms the roles by shipping DEDICATED ART per layer:
#     L11  SkyrimClouds01 / SkyrimCloudsLower0*   (full-dome sheets)
#     L27  SkyrimClouds01 / SkyrimCloudsLower03   (full-dome sheets)
#     L16  SkyrimCloudsHorizon01   50 of 50 times (purpose-made strip)
#     L28  SkyrimCloudsFill       156 of 157      (purpose-made wash)
# We have no strip or wash art, so those layers have nothing correct to put
# on them and stay empty.
#
# Confirmed in game against variants placing the sheets on 8/9/10/11 +
# strips + fog wash, on 3/6/11, on 11/27 swapped, and on 11/27 alone:
# 11 (upper) + 27 (lower) is correct.  See tools/make_sky_unjustified_esp.py.
_WTHR_UPPER_LAYER = 11        # 09_CDTop
_WTHR_LOWER_LAYER = 27        # 14_CDLower

# LNAM IS NOT A LAYER COUNT — it is an ARRAY INDEX CLAMP.
#
# Read out of SkyrimSE.exe rather than inferred.  Every read of
# TESWeather+0x7D0 in .text was enumerated (122 candidate dword hits, three
# real readers) and they all use the identical idiom:
#
#     mov   ecx, [r12 + 0x7d0]        ; LNAM
#     test  ecx, ecx
#     jle   .default                  ; LNAM <= 0 -> speed 0x33, alpha 1.0
#     cmp   ebx, ecx                  ; layerIndex vs LNAM
#     cmovl eax, ebx                  ; idx = (layer < LNAM) ? layer : 0
#     movzx edx, byte [rax+r12+0x220] ; cloudLayerSpeedY[idx]   RNAM
#
#   Clouds::Update  0x3c5485 (current weather) and 0x3c54ff (last weather)
#   JNAM alpha getter 0x2c1eb0  ([rcx+rdx*4+0x460])
#
# The PNAM colour getter (0x2c1e99) has no clamp at all.  The draw loops are
# bounded by Clouds::numLayers (the runtime object, +0x510) and a hard 32
# (`cmp bx, 0x20`), NOT by LNAM.  Layer visibility is owned solely by NAM1,
# which sets APP_CULLED on the layer geometry (0x3c5c56).
#
# So a too-small LNAM does not hide layers — it makes every layer at or above
# it silently reuse layer 0's speed and layer 0's alpha.  It must therefore
# cover every layer we actually author an RNAM/QNAM/JNAM entry for.
_WTHR_LNAM_MIN = 1

# Minimum width of the fog ramp, in world units, when a weather authors a far
# plane at or inside its near plane.  A zero-width ramp makes fog snap to full
# density instead of blending; vanilla's tightest real ramp is
# RiftenOvercastFog at 9000, and its 13 degenerate records are all 0/0
# no-fog FX weathers rather than authored fog.
_WTHR_FOG_MIN_RAMP = 9000.0

# Fog curve shape.  1.0/1.0 makes Skyrim's min(Max, pow(t, Power)) identical
# to Oblivion's fixed-function linear fog — see the FNAM block in convert_WTHR
# for the derivation.  These are NOT tuning knobs; changing them makes the
# converted fog stop matching the source engine.
_WTHR_FOG_POWER = 1.0
_WTHR_FOG_MAX = 1.0

# ALPHA IS THE SCALAR THAT WAS MISSING (and it is still missing without this).
#
# The original converter wrote 1.0 on both layers, so layer 0 was an opaque
# plane painted over the sky gradient at full strength and layer 1 was
# completely occluded behind it -- the cloud sheet BECAME the sky, which is
# what bleached the horizon.  Vanilla composites translucent sheets instead:
# over every vanilla weather that enables the layer, the per-time medians are
# 0.50-0.60 on layer 0 and 0.75-1.00 on layer 1.  Those are the values here.
#
# (layer, alpha per time-of-day: sunrise, day, sunset, night)
_WTHR_UPPER_PLAN = (
    (_WTHR_UPPER_LAYER, (0.60, 0.50, 0.60, 0.50)),
)
_WTHR_LOWER_PLAN = (
    (_WTHR_LOWER_LAYER, (1.00, 1.00, 0.75, 1.00)),
)


def _wthr_cloud_layer_plan(lower_cloud: str, upper_cloud: str) -> list:
    """Distribute TES4's two cloud textures across the TES5 dome bands.

    Returns [(layer, texture, alpha_per_time), ...] — see the block comment.
    A weather that authored only one of the two textures still fills the
    bands that texture is responsible for; the other band stays empty rather
    than being padded with a texture the weather never authored.
    """
    plan = []
    if upper_cloud:
        for layer, alphas in _WTHR_UPPER_PLAN:
            plan.append((layer, upper_cloud, alphas))
    if lower_cloud:
        for layer, alphas in _WTHR_LOWER_PLAN:
            plan.append((layer, lower_cloud, alphas))
    return plan


def _wthr_cloud_arrays(rec: dict, layer_plan) -> bytes:
    """Build TES5's per-cloud-layer RNAM/QNAM/PNAM/JNAM arrays.

    `layer_plan` is [(layer, texture, alpha_per_time), ...] from
    _wthr_cloud_layer_plan(), plus the synthesized fill layer — see the block
    comment above it for why TES4's two textures are spread over the dome.

    Sizes verified against references/Skyrim.esm (83/84 vanilla records):
      RNAM 32B  — cloud speed Y, u8 per layer, 0x7F = neutral (no drift)
      QNAM 32B  — cloud speed X, u8 per layer, 0x7F = neutral
      PNAM 512B — cloud colors, 32 layers x 4 times x RGBA
      JNAM 512B — cloud alphas, 32 layers x 4 times x f32
    """
    speed_lower = get_int(rec, 'DATA.CloudSpeedLower')
    speed_upper = get_int(rec, 'DATA.CloudSpeedUpper')
    lower_speed = _cloud_speed_tes4_to_tes5(speed_lower)
    upper_speed = _cloud_speed_tes4_to_tes5(speed_upper)

    lower_cloud = get_str(rec, 'CNAM.LowerCloudLayer')
    upper_cloud = get_str(rec, 'DNAM.UpperCloudLayer')

    rnam = bytearray(b'\x7F' * _TES5_CLOUD_LAYERS)
    qnam = bytearray(b'\x7F' * _TES5_CLOUD_LAYERS)

    raw = get_hex_bytes(rec, 'NAM0.Data')
    pnam = bytearray(_TES5_CLOUD_LAYERS * 4 * 4)
    alphas = [0.0] * (_TES5_CLOUD_LAYERS * 4)

    def tint(t4_slot, time):
        """The TES4 cloud tint, highlight-compressed like the sky slots.

        The cloud sheet is MULTIPLIED by this tint (verified in both engines'
        pixel shaders), so a near-white tint hands the tonemapper the texture
        at full strength.  Same knee as NAM0: authored below it, compressed
        above it, and with no time axis so a layer keeps its authored
        day/night relationship.
        """
        src = (t4_slot * 4 + time) * 4
        return _knee_rgb(raw[src], raw[src + 1], raw[src + 2],
                         _PNAM_KNEE, _PNAM_KNEE_CEILING)

    # Wind direction, for the X (QNAM) axis.  TES4 has no per-weather wind
    # direction, so the drift axis is derived from the same authored speed:
    # the engine reads BOTH arrays every frame (Clouds::Update 0x3c549b and
    # 0x3c54a4 in SkyrimSE.exe), and leaving QNAM at the neutral 0x7F means
    # the clouds never move horizontally at all.  Vanilla authors nonzero X
    # drift on 557 of 2656 layer entries and NEGATIVE drift on 77 of them, so
    # a single-axis, single-sign copy is visibly wrong motion.
    #
    # Split the authored speed across the two axes so the total drift rate is
    # preserved: Y keeps the full authored magnitude, X gets a fixed fraction
    # of it.  Both encode through _cloud_speed_tes4_to_tes5, which is signed
    # about 0x7F (speed = byte*0.2/254 - 0.1, verified from the machine code).
    for layer, texture, layer_alphas in layer_plan:
        if not 0 <= layer < _TES5_CLOUD_LAYERS:
            continue
        # Each dome layer drifts with the TES4 speed authored for the sheet it
        # came from, so the upper and lower bands keep their relative motion.
        t4_speed = speed_upper if texture == upper_cloud else speed_lower
        rnam[layer] = upper_speed if texture == upper_cloud else lower_speed
        qnam[layer] = _cloud_speed_signed_tes5(
            t4_speed * _WTHR_CLOUD_X_DRIFT)
        for time in range(4):
            alphas[layer * 4 + time] = layer_alphas[time]
        if not raw or len(raw) < 160:
            continue
        t4_slot = (_T4_CLOUDS_UPPER if texture == upper_cloud
                   else _T4_CLOUDS_LOWER)
        for time in range(4):
            dst = (layer * 4 + time) * 4
            pnam[dst:dst + 3] = bytes(tint(t4_slot, time))

    # NO SYNTHESIZED FILL LAYER.  A previous revision added one on layer 28
    # (`Sky\SkyrimCloudsFill.dds`, which 68 of 84 vanilla weathers use as a
    # dark horizon wash).  That is real vanilla behaviour but it is not
    # portable: it depends on Skyrim's 29-shape dome having geometry at layer
    # 28, and the dome WE ship is Oblivion's two-shape CloudDome.  Enabling a
    # layer the dome cannot draw is what produced the stitched-together sky.
    # Holding the horizon down has to come from the fog and colour tables,
    # which apply to the dome we actually have.

    jnam = struct.pack('<%df' % (_TES5_CLOUD_LAYERS * 4), *alphas)

    return (pack_subrecord('RNAM', bytes(rnam))
            + pack_subrecord('QNAM', bytes(qnam))
            + pack_subrecord('PNAM', bytes(pnam))
            + pack_subrecord('JNAM', jnam))


def _wthr_dalc(rec: dict) -> bytes:
    """Build the four DALC directional-ambient blocks (sunrise/day/sunset/night).

    TES5 lights the world with a 6-direction ambient cube (X+/X-/Y+/Y-/Z+/Z-)
    that TES4 has no equivalent for, so it is derived from the TES4 Ambient
    color for the same time of day.

    The per-face WEIGHTS are measured from the 84 vanilla Skyrim.esm weathers
    (median of face/NAM0-Ambient over every record, time and channel):

        X+ 0.98   X- 0.94   Y+ 0.96   Y- 0.95   Z+ 0.67   Z- 1.28

    Z+ is the DARKEST face and Z- the brightest — the opposite of the
    intuition that the sky-facing side should be brighter.  Writing Ambient
    verbatim into all six faces and then brightening Z+ (the previous
    behaviour) overdrove every face and washed the scene out.

    Layout (wbAmbientColors, form version >= 34): 6 x RGBA + Specular RGBA +
    Fresnel Power f32 = 32 bytes.  Fresnel is 1.0 in every vanilla record.
    """
    raw = get_hex_bytes(rec, 'NAM0.Data')
    out = b''
    for time in range(4):
        if raw and len(raw) >= 160:
            src = (_T4_AMBIENT * 4 + time) * 4
            # Same normalization as NAM0's Ambient slot: Oblivion authors
            # ambient at roughly HALF vanilla's luminance (92 vs 172 midday
            # median), which left shadows black under blown highlights.
            r, g, b = _normalize_rgb(3, time,
                                     raw[src], raw[src + 1], raw[src + 2])
        else:
            r = g = b = 0
        block = bytearray()
        for weight in _DALC_FACE_WEIGHTS:
            block += bytes((min(255, round(r * weight)),
                            min(255, round(g * weight)),
                            min(255, round(b * weight)), 0))
        block += b'\x00\x00\x00\x00'          # Specular
        block += struct.pack('<f', 1.0)       # Fresnel Power
        out += pack_subrecord('DALC', bytes(block))
    return out


# WTHR SNAM sound-type enum, identical in both games (xEdit wbWeatherSounds,
# shared by wbDefinitionsTES4/TES5): 0=Default 1=Precipitation 2=Wind 3=Thunder.
_WTHR_SND_PRECIP, _WTHR_SND_WIND, _WTHR_SND_THUNDER = 1, 2, 3

# TES4 SOUN EditorID substrings that identify what a weather bed actually is.
# Oblivion weather sound entries are overwhelmingly authored as Type 0
# ('Default') regardless of content — 33 of Nehrim's 53 entries — because
# Oblivion's Default channel is just "play this while the weather is active".
# Skyrim's Default channel means the same thing, which is exactly the problem:
# it plays UNCONDITIONALLY and UNDUCKED for as long as the weather holds.
# Skyrim expects rain on Precipitation (which the engine gates on the
# precipitation fade envelope) and wind on Wind, so the authored intent has to
# be recovered from the sound itself.
_WTHR_SND_KEYWORDS = (
    (_WTHR_SND_THUNDER, ('thunder', 'lightning', 'donner', 'blitz')),
    (_WTHR_SND_PRECIP, ('rain', 'regen', 'snow', 'schnee', 'sleet', 'hail',
                        'storm', 'sturm', 'drizzle')),
    (_WTHR_SND_WIND, ('wind', 'gust', 'breeze', 'boe')),
)

# Sounds that are not weather at all.  Oblivion routinely parks a location's
# ambience bed on the WEATHER record — fire pits, dungeon drones, creature and
# machinery loops — because in Oblivion a weather sound only plays while that
# weather is active in that region, which makes it a cheap area-ambience hook.
# Skyrim has a dedicated mechanism for this (ASPC acoustic spaces / ambient
# REFR loops), and its weather channel is global to the whole sky: anything
# left here plays everywhere the weather does.  That is what put fire
# crackling, dungeon drones and animal noises into open Nehrim countryside.
#
# Verified against the real Skyrim.esm (SSE, 84 weathers, 30 SNAM entries):
# EVERY vanilla weather sound is rain, snow, wind, thunder, or a deliberate
# 2D set-piece bed.  There is not one fire, creature or dungeon loop among
# them.  Anything matching here is dropped rather than converted.
_WTHR_SND_NON_WEATHER = (
    'fire', 'feuer', 'flame', 'flamme', 'lava', 'magma', 'torch', 'fackel',
    'dungeon', 'hoehle', 'cave', 'crypt', 'sewer', 'kanal',
    'water', 'wasser', 'river', 'fluss', 'waterfall', 'sea', 'meer', 'ocean',
    'wave', 'welle', 'brook', 'bach', 'drip', 'tropf',
    'creature', 'animal', 'tier', 'pig', 'schwein', 'bird', 'vogel',
    'insect', 'cricket', 'grille', 'wolf', 'howl', 'heul',
    'machine', 'maschine', 'mill', 'muehle', 'forge', 'schmiede',
    'crowd', 'menge', 'market', 'markt', 'tavern', 'taverne',
    'void', 'universum', 'space', 'portal', 'magic', 'magie',
)


def _wthr_sound_class(edid: str, filename: str):
    """Classify a TES4 weather sound entry.

    Returns the TES5 SNAM type to write, or None when the sound is not
    weather at all and must be dropped.  Both the EditorID and the sound's
    file path are searched, because Nehrim names several beds only in the
    path (``fx\\nehrim\\windhoehle01.wav`` is a cave wind, not weather).
    """
    hay = f'{edid} {filename}'.lower().replace('\\', '/')
    # Non-weather wins over everything: 'windhoehle' is a cave, not wind.
    if any(k in hay for k in _WTHR_SND_NON_WEATHER):
        return None
    for stype, keys in _WTHR_SND_KEYWORDS:
        if any(k in hay for k in keys):
            return stype
    # Unrecognised. Oblivion's own weather beds are all named for what they
    # are, so an unmatched sound is far more likely to be another borrowed
    # ambience loop than a weather bed we failed to name — and a wrong sound
    # here plays across the entire sky.  Drop it: vanilla ships 63 of 84
    # weathers with NO sound at all, so silence is the vanilla-normal state.
    return None


_TES4_WTHR_PLEASANT = 0x01
_TES4_WTHR_CLOUDY = 0x02
_TES4_WTHR_RAINY = 0x04
_TES4_WTHR_SNOW = 0x08


def _wthr_is_fair(rec: dict) -> bool:
    """True when this TES4 weather is a fair-weather sky (clear/cloudy/fog).

    Unlike Skyrim — where 78 of 84 weathers set all four classification bits,
    making the field meaningless — Oblivion authors the bits precisely, so the
    Pleasant/Cloudy bits alone identify a fair sky.

    Classification 0 is NOT fair: it means UNCLASSIFIED, and Oblivion uses it
    for the Oblivion-plane storm skies (OblivionStormTamriel, OblivionSigil,
    OblivionElectrical, SE09SummoningWeather...), which are thunderstorms with
    authored thunder.  Treating 0 as fair silenced all nine of them.  Fall back
    to the authored thunder frequency, which is the same signal _wthr_precipitation
    already trusts to pick a storm particle system: it is inverted in both
    games (255 = never), and vanilla Oblivion thunderstorms ship 188/132/100/24.
    """
    flags = get_int(rec, 'DATA.Classification')
    if flags & (_TES4_WTHR_RAINY | _TES4_WTHR_SNOW):
        return False
    if flags & (_TES4_WTHR_PLEASANT | _TES4_WTHR_CLOUDY):
        return True
    # Unclassified: a sky that authors thunder is a storm, not a fair sky.
    return get_int(rec, 'DATA.ThunderFrequency', 255) >= 255


def _wthr_sounds(rec: dict) -> bytes:
    """Build the WTHR SNAM entries from the TES4 sound list.

    The FormID written is the TES4 SOUN id — a placeholder resolved to the
    real SNDR descriptor by patch_weather_sounds after Phase 3.

    FAIR-WEATHER SKIES GET NO BED AT ALL.  Oblivion hangs a looping wind on
    Clear, Cloudy, Fog, Snow and DefaultWeather alike, and because Oblivion's
    weather sound is region-scoped that reads as local color there.  Skyrim's
    weather channel is global and continuous, so the same entry becomes wind
    howling over the entire province in bright sunshine that never once stops
    — the 'incessant wind' symptom.

    Vanilla Skyrim never does this.  Census of the real Skyrim.esm (SSE): of
    the 21 weathers carrying any sound, EVERY one is a rain, storm, snow or
    overcast sky, or a scripted set-piece (Sovngarde, Blackreach, Helgen,
    WorldMap).  Not a single plain fair-weather sky has an ambient bed; 62 of
    84 weathers are silent outright.  Skyrim's fair-weather wind comes from
    region ambience and ASPC acoustic spaces instead, which are positional and
    intermittent — exactly the 'occasional and environmental' behaviour that
    is wanted, and which the weather channel cannot produce.
    """
    if _wthr_is_fair(rec):
        return b''
    out = b''
    seen = set()
    for i in range(get_int(rec, 'SoundCount')):
        sfid = get_formid(rec, f'Sound[{i}].FormID')
        if not sfid:
            continue
        edid, fname = get_soun_identity(sfid)
        stype = _wthr_sound_class(edid, fname)
        if stype is None:
            continue
        # Oblivion lists each thunder variant separately and relies on its own
        # random-variant picker; in Skyrim one descriptor already holds every
        # variant as repeated ANAMs, so duplicate entries would just stack the
        # same bed on itself and play it N times louder.
        key = (sfid, stype)
        if key in seen:
            continue
        seen.add(key)
        out += pack_subrecord('SNAM', struct.pack('<II', sfid, stype))
    return out


def patch_weather_sounds(writer, own_soun_ids=None) -> int:
    """Rewrite every WTHR SNAM from its TES4 SOUN id to the SNDR descriptor.

    TES5 weather sounds reference a sound DESCRIPTOR, not a SOUN: xEdit's
    shared wbWeatherSounds is `wbFormIDCK('Sound', [SNDR, SOUN, NULL])`, and a
    census of the real Skyrim.esm (SSE) shows 11 of the 12 distinct weather
    sound targets are SNDR.  The single SOUN target (AMBWindLightLP, 0x12EA2)
    is a LEGACY record that still carries its own FNAM + SNDD payload, so it
    is self-describing.

    Ours are not.  convert_SOUN emits `EDID + OBND + SDSC` only — all the
    audio data lives on the companion SNDR — so a weather pointing at one
    hands the engine a descriptor-shaped read with no descriptor behind it.
    That is what produced the wrong-sound symptom (fire crackling and animal
    noises in open Nehrim countryside): the weather bed is not the sound the
    plugin authored, it is whatever the mis-typed dereference lands on.

    WTHR is written in Phase 2, before Phase 3 creates the descriptors, so
    convert_WTHR stores the SOUN id and this resolves it — the same
    placeholder-then-patch approach actors.patch_actor_sounds uses for CSDI
    and items.patch_door_sounds for the DOOR slots.  Allocating during Phase 2
    instead would shift every later generated FormID.

    *own_soun_ids* is the low-24 id set of the SOUN records THIS plugin
    converts; anything outside it (an override build's master-owned records,
    already holding real SNDR ids) is left untouched, exactly as
    patch_door_sounds does.

    A slot whose SOUN produced no descriptor is DROPPED rather than left
    pointing at the wrong record type — 63 of the 84 vanilla weathers ship no
    sound at all, so an empty sound list is the vanilla-normal state.
    """
    mapping = _SNDR_FOR_SOUN
    if not mapping:
        return 0
    own = own_soun_ids if own_soun_ids is not None else set(mapping)
    records = writer._top_groups.get('WTHR') or []
    patched = 0
    bound = set()          # descriptors a weather actually uses (retuned below)
    for i, blob in enumerate(records):
        if b'SNAM' not in blob:
            continue
        out = bytearray(blob[:24])
        pos = 24
        changed = False
        while pos + 6 <= len(blob):
            sig = blob[pos:pos + 4]
            size = struct.unpack_from('<H', blob, pos + 4)[0]
            chunk = blob[pos:pos + 6 + size]
            pos += 6 + size
            if sig == b'SNAM' and size == 8:
                soun, stype = struct.unpack_from('<II', chunk, 6)
                if (soun & 0x00FFFFFF) not in own:
                    out += chunk          # not ours to resolve — leave alone
                    continue
                sndr = mapping.get(soun & 0x00FFFFFF, 0)
                if sndr != soun:
                    changed = True
                if sndr:
                    bound.add(sndr)
                    out += chunk[:6] + struct.pack('<II', sndr, stype)
                continue           # no descriptor -> drop the slot entirely
            out += chunk
        if not changed:
            continue
        struct.pack_into('<I', out, 4, len(out) - 24)   # data size
        records[i] = bytes(out)
        patched += 1
    _retune_weather_descriptors(writer, bound)
    return patched


# AudioCategoryAMB / AudioCategorySFX (Skyrim.esm SNCT 0x0007F80B / 0x000172A1).
_AUDIO_CAT_AMB = 0x0007F80B
_AUDIO_CAT_SFX = 0x000172A1


def _retune_weather_descriptors(writer, bound) -> int:
    """Route every descriptor a weather uses into the ambience category.

    convert_SOUN files a sound as ambience only when TES4 marked it 2D, which
    is the right default for a sound in isolation.  A weather bed is different:
    the weather channel plays it across the whole sky for as long as the
    weather holds, so it IS ambience whatever its 2D flag says.  Oblivion's
    rain and region-wind loops are authored 3D (AMBRainLP flags=0x10 is LOOP
    with no 2D bit), so they were landing in AudioCategorySFX — the wrong mix
    bus, which ignores the ambience slider and does not duck under dialogue or
    combat.  That is the 'incessant, never-ducking, far too loud' half of the
    symptom; the wrong-sound half is the SOUN/SNDR mis-typing above.

    Census of the real Skyrim.esm (SSE): of the 12 descriptors its weathers
    reference, every looping bed is AudioCategoryAMB, AudioCategoryAMBr or
    AudioCategoryMuteSubmerged.  Only the one-shot thunder cracks
    (AMBWeatherThunderDistant/Extra, LNAM loop byte 0) stay on SFX, so a
    non-looping descriptor is left exactly as convert_SOUN filed it.
    """
    if not bound:
        return 0
    records = writer._top_groups.get('SNDR') or []
    retuned = 0
    for i, blob in enumerate(records):
        if len(blob) < 24:
            continue
        if struct.unpack_from('<I', blob, 12)[0] not in bound:
            continue
        # Only looping beds move; one-shots (thunder) are SFX in vanilla too.
        looping = False
        pos = 24
        while pos + 6 <= len(blob):
            sig = blob[pos:pos + 4]
            size = struct.unpack_from('<H', blob, pos + 4)[0]
            if sig == b'LNAM' and size == 4 and blob[pos + 7]:
                looping = True
                break
            pos += 6 + size
        if not looping:
            continue
        out = bytearray(blob[:24])
        pos = 24
        changed = False
        while pos + 6 <= len(blob):
            sig = blob[pos:pos + 4]
            size = struct.unpack_from('<H', blob, pos + 4)[0]
            chunk = blob[pos:pos + 6 + size]
            pos += 6 + size
            if (sig == b'GNAM' and size == 4
                    and struct.unpack_from('<I', chunk, 6)[0] == _AUDIO_CAT_SFX):
                out += chunk[:6] + struct.pack('<I', _AUDIO_CAT_AMB)
                changed = True
                continue
            out += chunk
        if not changed:
            continue
        struct.pack_into('<I', out, 4, len(out) - 24)
        records[i] = bytes(out)
        retuned += 1
    return retuned


def convert_WTHR(rec: dict, writer=None) -> tuple:
    """WTHR — Weather conversion.  Returns (wthr_bytes, [imgs_bytes, ...]).

    TES5 subrecord order (from wbDefinitionsTES5.pas):
    EDID, DNAM/CNAM/ANAM/BNAM (old, unused), cloud textures (00TX..O0TX),
    LNAM, MNAM, NNAM, ONAM(unused), RNAM, QNAM, PNAM, JNAM, NAM0, FNAM,
    DATA, NAM1, SNAM(sounds), TNAM(sky statics), IMSP, HNAM(SSE volumetric),
    DALC x4, NAM2, NAM3, MODL/MODT(aurora), GNAM
    """
    # HDR tone mapping lives in companion IMGS records in TES5 — see
    # _wthr_imgs.  FOUR of them, one per time of day, because day and night
    # tone mapping differ substantially and 70% of vanilla weathers ship
    # distinct imagespaces per slot.  Every weather gets them, including those
    # whose TES4 HNAM is all zeros (DefaultWeather): _wthr_imgs turns those
    # into the vanilla per-slot defaults rather than a degenerate zero white
    # point.  A caller with no writer (unit tests exercising the WTHR bytes
    # alone) falls back to stock.
    imgs_bytes = []
    imgs_fids = [_DEFAULT_IMGS] * 4
    if writer is not None:
        imgs_fids = []
        for time in range(4):
            fid = writer.derive_formid('WTHR_IMGS',
                                       (get_formid(rec, 'FormID'), time))
            imgs_fids.append(fid)
            imgs_bytes.append(_wthr_imgs(rec, fid, time))

    subs = b''
    edid = get_str(rec, 'EditorID')
    if edid:
        # Oblivion and Skyrim both ship a 'DefaultWeather'.  After load-order
        # remapping ours collides with Skyrim's 0x0000015E and the CK renames
        # it to 'DefaultWeatherDUPLICATE001'; prefix instead so the converted
        # record keeps a stable, meaningful EditorID.
        if edid in _WTHR_EDID_COLLISIONS:
            edid = 'TES4' + edid
        subs += pack_string_subrecord('EDID', edid)

    # Cloud layer textures.  TES4 authors exactly two sheets; Skyrim's dome is
    # a stack whose vanilla weathers populate a median of EIGHT layers, split
    # into an upper dome, a mid band and a HORIZON band.  Putting the two TES4
    # sheets on layers 0/1 alone left the horizon band empty, so the horizon
    # showed bare sky gradient under an opaque cloud plane seen edge-on.
    # _wthr_cloud_layer_plan spreads them across the bands the engine expects.
    lower_cloud = get_str(rec, 'CNAM.LowerCloudLayer')
    upper_cloud = get_str(rec, 'DNAM.UpperCloudLayer')
    layer_plan = _wthr_cloud_layer_plan(lower_cloud, upper_cloud)
    used_layers = []
    for layer, path, _alphas in layer_plan:
        sig = _wthr_cloud_sig(layer)
        path_bytes = _prefix_path(path).encode('utf-8') + b'\x00'
        subs += sig + struct.pack('<H', len(path_bytes)) + path_bytes
        used_layers.append(layer)

    # LNAM — see _WTHR_LNAM_MIN.  Disassembly of every reader in SkyrimSE.exe
    # shows this is an INDEX CLAMP into RNAM/QNAM/JNAM, not an allocation
    # count: layers >= LNAM keep drawing but silently reuse layer 0's speed
    # and layer 0's alpha.  So it must span the layers we author, and it must
    # be > 0 (LNAM <= 0 takes the `jle` default of speed 0x33 / alpha 1.0,
    # which throws away the authored JNAM entirely).
    subs += pack_uint32_subrecord(
        'LNAM', max(_WTHR_LNAM_MIN, (max(used_layers) + 1) if used_layers
                    else _WTHR_LNAM_MIN))

    # MNAM (Precipitation Type -> SPGD) and NNAM (Visual Effect -> RFCT) are
    # .SetRequired in xEdit.
    #
    # Oblivion draws rain/snow through hardcoded Sky\ meshes picked by the
    # weather's classification bits; Skyrim draws them through the SPGD this
    # field names, so a NULL here means an authored rainstorm produces thunder
    # and a dark sky BUT NO RAIN.  Map the authored classification onto the
    # vanilla Skyrim.esm particle systems (18 of 84 vanilla weathers do
    # exactly this): Rainy -> RainParticles, Rainy with authored thunder ->
    # RainStormParticles, Snow -> SnowParticlesMed.  Thunder presence is
    # authored via DATA.ThunderFrequency, which is inverted in both games
    # (255 = never); vanilla Oblivion thunderstorms ship 188/132/100/24.
    #
    # NNAM stays NULL — 82 of 84 vanilla weathers ship NULL there.
    subs += pack_formid_subrecord('MNAM', _wthr_precipitation(rec))
    subs += pack_formid_subrecord('NNAM', 0)

    # RNAM/QNAM/PNAM/JNAM — per-cloud-layer speed, color and alpha.
    subs += _wthr_cloud_arrays(rec, layer_plan)

    # NAM0 — weather colors, remapped from TES4's 10 types to TES5's 17.
    subs += pack_subrecord('NAM0', _wthr_nam0(rec))

    # FNAM — Fog distances (TES5: 32 bytes — 8 floats).  Near/far distances
    # pass through; power and max have no TES4 source and take the vanilla
    # medians (power 0.4/0.4, max 0.9/0.925 over all 84 weathers).  The
    # earlier 1.0/1.0 was xEdit's field default, not what vanilla ships:
    # max 1.0 lets fog reach FULL opacity at the horizon, painting it with
    # Oblivion's pale fog color — a big part of the blown-white horizon.
    #
    # THE NEAR PLANE MUST NOT BE NEGATIVE.  Both games share the same
    # `wbWeatherFogDistance` struct (xEdit wbDefinitionsCommon, used by
    # wbDefinitionsTES4 and TES5 alike), so the field is a straight
    # passthrough by layout -- but Oblivion AUTHORS negative near planes and
    # Skyrim never does: 18 of Oblivion.esm's 37 weathers ship one (SETestAsh
    # -750, SEThunderstorm night -6500, SE09SummoningWeather -2000) against
    # 0 of the 84 vanilla Skyrim records.
    #
    # A negative near plane means the fog ramp starts BEHIND the camera, so
    # every visible pixel is already past the ramp's start and fog renders at
    # full density with no gradient.  That is worst along the LONGEST view
    # rays -- the horizon -- and it is view-dependent, which is why the sky
    # looks right when facing the ground and the horizon flares when you look
    # up into it.  Clamp to the vanilla floor of 0.
    fog_day_near = max(0.0, get_float(rec, 'FNAM.FogDayNear', 100.0))
    fog_day_far = get_float(rec, 'FNAM.FogDayFar', 100000.0)
    fog_night_near = max(0.0, get_float(rec, 'FNAM.FogNightNear', 100.0))
    fog_night_far = get_float(rec, 'FNAM.FogNightFar', 100000.0)
    # A near plane at or beyond the far plane is degenerate the same way: the
    # ramp has no width, so fog snaps to full density instead of blending.
    if fog_day_far <= fog_day_near:
        fog_day_far = fog_day_near + _WTHR_FOG_MIN_RAMP
    if fog_night_far <= fog_night_near:
        fog_night_far = fog_night_near + _WTHR_FOG_MIN_RAMP
    # Power and Max: TES4 has NO source for either, and the correct values are
    # fixed by the two engines' fog EQUATIONS, not by what vanilla happens to
    # author.
    #
    #   Skyrim   (Lighting.hlsl:271, identical in Effect.hlsl/DistantTree.hlsl)
    #     f = min(Max, exp2(Power * log2(saturate((d-Near)/(Far-Near)))))
    #       = min(Max, pow(t, Power))
    #   Oblivion  fixed-function D3DFOG_LINEAR.  Established by disassembling
    #     all 123 vertex shaders in Data/Shaders/shaderpackage001.sdp and
    #     finding that NONE writes oFog (RASTOUT#1); the weather's near/far go
    #     straight into a BSFogProperty (Atmosphere::Update 0x53b318..0x53b34c).
    #     D3D linear fog is (End-d)/(End-Start), whose complement — the fog
    #     weight, which is what Skyrim's fogColorParam holds — is
    #     (d-Start)/(End-Start).
    #
    # With Power=1 and Max=1, min(1, pow(t,1)) == t == Oblivion's ramp
    # exactly, at every distance.  The previous 0.4/0.9 were vanilla medians
    # and are wrong by up to +0.32 fog mid-ramp and -0.10 at the far plane
    # (where Max=0.9 leaves a tenth of the scene permanently unfogged).
    # 15 of 177 vanilla weathers ship Power=1.0 and 29 ship Max=1.0, so these
    # are ordinary values for the engine — but the reason to write them is the
    # identity above, not their frequency.
    fnam = struct.pack('<ffffffff',
                        fog_day_near, fog_day_far,
                        fog_night_near, fog_night_far,
                        _WTHR_FOG_POWER, _WTHR_FOG_POWER,
                        _WTHR_FOG_MAX, _WTHR_FOG_MAX)
    subs += pack_subrecord('FNAM', fnam)

    # DATA — Weather Data (19 bytes in TES5).
    #
    # TES5 reuses TES4's field order but replaces TES4's two cloud-speed bytes
    # (offsets 1-2) with padding, having moved per-layer speed into RNAM/QNAM,
    # and appends four fields TES4 has no source for.
    #
    # Trans Delta is a real UNIT conversion, not a fitted one.  xEdit
    # annotates the TES5 byte `scaled 0..0,25` (wbDefinitionsTES5.pas:10650)
    # against TES4's plain 0..255, and the authored data shows the ceiling
    # exactly: over all 177 vanilla WTHR the byte NEVER exceeds 125 (134 of
    # them sit precisely at 125), whereas 19 of Oblivion's 37 author 255.
    # 125/255 maps TES4's full range onto the range the engine accepts.
    #
    # Sun Glare passes through UNSCALED.  A previous revision multiplied by
    # 0.6 to land TES4's 255 on the vanilla p90 of 153 — a percentile fit with
    # no mechanism behind it.  The census does not support a ceiling here the
    # way it does for Trans Delta: vanilla reaches 204, and both games
    # document the byte identically (`Sun Glare (0-1)` in the TES5 format
    # notes; TES4 has the same field with the same width and no scale
    # annotation).  Since the field means the same fraction in both engines,
    # scaling it would dim an authored value for no reason.
    data = struct.pack(
        '<B2xBBBBBBBBBBBBBBBB',
        get_int(rec, 'DATA.WindSpeed'),
        round(get_int(rec, 'DATA.TransDelta') * 125 / 255),
        get_int(rec, 'DATA.SunGlare'),
        get_int(rec, 'DATA.SunDamage'),
        get_int(rec, 'DATA.PrecipBeginFadeIn'),
        get_int(rec, 'DATA.PrecipEndFadeOut'),
        get_int(rec, 'DATA.ThunderBeginFadeIn'),
        get_int(rec, 'DATA.ThunderEndFadeOut'),
        get_int(rec, 'DATA.ThunderFrequency'),
        _wthr_flags(rec),
        get_int(rec, 'DATA.LightningR'),
        get_int(rec, 'DATA.LightningG'),
        get_int(rec, 'DATA.LightningB'),
        0,    # Visual Effect - Begin (no TES4 source)
        0,    # Visual Effect - End
        0,    # Wind Direction
        0,    # Wind Direction Range
    )
    subs += pack_subrecord('DATA', data)

    # NAM1 — Disabled Cloud Layers bitfield.  This must disable only the
    # layers we did NOT write a texture for; the old blanket 0xFFFFFFFF also
    # disabled layers 0/1 and blanked every converted sky.
    disabled = 0xFFFFFFFF
    for layer in used_layers:
        disabled &= ~(1 << layer)
    subs += pack_uint32_subrecord('NAM1', disabled & 0xFFFFFFFF)

    # Sounds — SNAM (after NAM1 per xEdit).  The FormID written here is this
    # weather's TES4 SOUN id: WTHR converts in Phase 2, before Phase 3 mints
    # the SNDR descriptors, so it is a PLACEHOLDER that patch_weather_sounds
    # resolves later (the same approach actors.patch_actor_sounds uses for
    # CSDI and items.patch_door_sounds for the DOOR slots).  Allocating here
    # instead would shift every later generated FormID.
    subs += _wthr_sounds(rec)

    # IMSP — Image Spaces (sunrise/day/sunset/night), each pointing at the
    # matching time-of-day IMGS built from this weather's TES4 HDR block.
    subs += pack_subrecord('IMSP', struct.pack('<IIII', *imgs_fids))

    # DALC — Directional Ambient Lighting Colors (4 x 32 bytes)
    subs += _wthr_dalc(rec)

    wthr_bytes = pack_record('WTHR', get_formid(rec, 'FormID'),
                             get_int(rec, 'RecordFlags'), subs)
    return wthr_bytes, imgs_bytes


# Skyrim's night-sky mesh.  TES4 climates point MODL at their own stars mesh
# (Sky\Stars.nif), which the asset pipeline converts under the tes4\ namespace;
# this is only the fallback for a climate that authored no model at all.
_DEFAULT_STARS_MODEL = 'Sky\\Stars.nif'


def convert_CLMT(rec: dict) -> bytes:
    """CLMT — Climate.

    TES4 and TES5 climates are near-identical: the same WLST weather list, the
    same FNAM/GNAM sun textures, the same 6-byte TNAM timing struct.  The only
    format change is that TES5's WLST entry gained a trailing Global FormID,
    widening it from 8 to 12 bytes (verified against references/Skyrim.esm,
    whose entries are all 12 bytes with a null Global).

    Without this record the converted WTHR records are unreachable: weather is
    selected through WRLD -> CNAM -> CLMT -> WLST, never referenced directly.

    Subrecord order (wbDefinitionsTES5.pas:4444):
    EDID, WLST, FNAM, GNAM, MODL/MODT, TNAM
    """
    subs = b''
    edid = get_str(rec, 'EditorID')
    if edid:
        subs += pack_string_subrecord('EDID', edid)

    # WLST — weather list.  TES5 entry is (Weather, Chance, Global).
    wlst = b''
    for i in range(get_int(rec, 'WeatherCount')):
        wfid = get_formid(rec, f'Weather[{i}].FormID')
        if not wfid:
            continue
        chance = get_int(rec, f'Weather[{i}].Chance')
        wlst += struct.pack('<IiI', wfid, chance, 0)
    if wlst:
        subs += pack_subrecord('WLST', wlst)

    # Sun and sun-glare textures.
    sun = get_str(rec, 'FNAM.SunTexture')
    if sun:
        subs += pack_string_subrecord('FNAM', _prefix_path(sun))
    glare = get_str(rec, 'GNAM.GlareTexture')
    if glare:
        subs += pack_string_subrecord('GNAM', _prefix_path(glare))

    # MODL — the night-sky/stars mesh.  Every vanilla Skyrim climate has one;
    # without it the engine draws no stars at night.
    model = get_str(rec, 'Model.MODL') or _DEFAULT_STARS_MODEL
    subs += pack_string_subrecord('MODL', _prefix_path(model))
    # MODT stub (version 2, no texture hashes) — same form the GRAS converter
    # uses; vanilla climates ship a 12-byte MODT.
    subs += pack_subrecord('MODT', struct.pack('<III', 2, 0, 0))

    # TNAM — 6-byte timing struct: sunrise begin/end, sunset begin/end (units
    # of 10 minutes), volatility, and a packed moons/phase-length byte.  The
    # moons byte is a true passthrough — TES4's 0xC3 (Masser+Secunda, phase 3)
    # is byte-identical to vanilla SkyrimClimate's.
    #
    # Volatility is NOT a passthrough: the TES4 byte spans 0..255 and Oblivion
    # re-rolls weather regardless of it (TamrielClimate 174, DefaultClimate 0,
    # both cycle in-game), while in Skyrim it is the re-roll chance and the
    # census is bimodal — SkyrimClimate (the ONE variable outdoor climate)
    # ships exactly 50, and 0 appears only on locked single-weather skies
    # (Sovngarde, Blackreach).  Passing TES4's 0 through froze every converted
    # sky on its first weather forever.  Write vanilla's 50; single-weather
    # climates re-roll onto the same weather, so nothing is lost there.
    subs += pack_subrecord('TNAM', bytes((
        get_int(rec, 'TNAM.SunriseBegin'),
        get_int(rec, 'TNAM.SunriseEnd'),
        get_int(rec, 'TNAM.SunsetBegin'),
        get_int(rec, 'TNAM.SunsetEnd'),
        50,
        get_int(rec, 'TNAM.MoonsPhaseLength'),
    )))

    return pack_record('CLMT', get_formid(rec, 'FormID'), get_int(rec, 'RecordFlags'), subs)
