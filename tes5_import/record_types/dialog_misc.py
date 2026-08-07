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
    pack_uint8_subrecord,
    pack_uint32_subrecord,
)


# TES4 SNDX/SNDD flag bits (xEdit wbDefinitionsTES4, SOUN)
_TES4_SND_RANDOM_FREQ_SHIFT = 0x0001
_TES4_SND_LOOP              = 0x0010
_TES4_SND_MENU_SOUND        = 0x0020
_TES4_SND_2D                = 0x0040

# Root of the extracted TES4 assets for the plugin being imported, set by
# set_sound_source_dir() at import start. Used only to enumerate the .wav files
# inside a directory-valued SOUN.FNAM.
_SOUND_SOURCE_DIR = None


def set_sound_source_dir(export_dir: str) -> None:
    """Point the SOUN converter at this plugin's extracted assets.

    Only needed to expand directory-valued FNAMs; a missing/None dir simply
    means such sounds fall back to the single literal path.
    """
    global _SOUND_SOURCE_DIR
    _SOUND_SOURCE_DIR = export_dir


def get_sound_source_dir() -> str:
    return _SOUND_SOURCE_DIR


# TES4 SOUN FormID (low 24 bits) → the SNDR FormID convert_SOUN gave its
# companion. Filled DURING Phase 3, and read afterwards to patch the records
# that reference it (see items.patch_door_sounds).
#
# An earlier version reserved these ids in a Phase 0 pre-pass so records could
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
    """TES4 SOUN id (low 24 bits) → companion SNDR FormID, for slot patching."""
    return _SNDR_FOR_SOUN


def get_sndr_for_soun(soun_fid: int) -> int:
    """The SNDR FormID for a TES4 SOUN, or 0 if it has no companion.

    Accepts a FormID in either raw or load-order-offset form; the map is keyed
    on the low 24 bits (same convention as outfits.load_item_index).
    """
    return _SNDR_FOR_SOUN.get(soun_fid & 0x00FFFFFF, 0)


# Vanilla Skyrim SOPM constants (verified against references/Skyrim.esm SOPM dump)
_SOPM_2D = 0x000B5183            # SOMDialogue2D — non-attenuating, for menu/2D sounds
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

    fid = writer.alloc_formid()
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


def set_sound_source_dir(export_dir: str) -> None:
    """Point the SOUN converter at this plugin's extracted assets.

    Only needed to expand directory-valued FNAMs; a missing/None dir simply
    means such sounds fall back to the single literal path (see
    _sound_anam_paths).
    """
    global _SOUND_SOURCE_DIR
    _SOUND_SOURCE_DIR = export_dir


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

        is_2d = bool(tes4_flags & (_TES4_SND_2D | _TES4_SND_MENU_SOUND))

        sndr_fid = writer.alloc_formid()
        # Actors reference this descriptor by TES4 SOUN id; they are already
        # written, so the CSDI placeholders get patched afterwards (see
        # actors.patch_actor_sounds).
        record_sndr_for_soun(get_formid(rec, 'FormID'), sndr_fid)
        sndr_subs = b''
        sndr_edid = f"TES4_{edid}_SNDR" if edid else f"TES4_SOUN_{get_formid(rec, 'FormID'):08X}_SNDR"
        sndr_subs += pack_string_subrecord('EDID', sndr_edid)
        # CNAM = Descriptor Type constant (0x1EEF540A — matches all vanilla SNDR records)
        sndr_subs += pack_uint32_subrecord('CNAM', 0x1EEF540A)
        # GNAM = Category: AudioCategorySFX (FormID 0x000172A1 in Skyrim.esm)
        sndr_subs += pack_formid_subrecord('GNAM', 0x000172A1)
        # ANAM = Sound file path, one per variant (a directory-valued TES4 FNAM
        # expands to the files it holds — see _sound_anam_paths).
        for anam in _sound_anam_paths(filename):
            sndr_subs += pack_string_subrecord('ANAM', anam)
        # ONAM = Sound Output Model. Required — CK reports 'Sound Output Model
        # missing' if absent.  2D/menu sounds are not positional, so they take
        # the vanilla non-attenuating model; everything else gets a SOPM built
        # from this sound's own TES4 falloff distances.
        if is_2d or max_dist <= 0:
            onam_fid = _SOPM_2D
        else:
            onam_fid = _build_sopm(writer, min_dist, max_dist, stereo=False)
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


# Skyrim.esm's default imagespace, used by every vanilla WTHR's IMSP slots.
# TES4 has no IMGS records (its tone mapping lives in the weather's own HNAM),
# so converted weathers inherit Skyrim's default rather than a null pointer.
_DEFAULT_IMGS = 0x00000161

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


# --- NAM0 weather colour tables -------------------------------------------
#
# TES4 NAM0 is 10 colour types x 4 times-of-day x RGBA = 160 bytes.
# TES5 NAM0 is 17 colour types x 4 times-of-day x RGBA = 272 bytes (verified
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
    _T4_FOG,            # 12 Fog Far — TES4 had a single fog colour
    None,               # 13 Sky Statics — see _NAM0_SLOT_DEFAULTS
    None,               # 14 Water Multiplier
    None,               # 15 Sun Glare
    None,               # 16 Moon Glare
]

# Documented per-slot defaults for the TES5-only slots (UESP
# 'Skyrim Mod:Mod File Format/WTHR', corroborated by a census of the 84 vanilla
# Skyrim.esm WTHR records).
#
# These are NOT free to guess: slots 13/15/16 tint additive sky passes, so a
# wrong value blows the scene out rather than merely looking off.
#   13 Sky Statics    — UESP "defaults to black"; vanilla mode (0,0,0).
#                       This tints the CLMT stars/moon mesh; WHITE HERE MAKES
#                       THE NIGHT SKY BLINDING.
#   14 Water Multiplier — UESP "defaults to white"; vanilla mode (255,255,255).
#                       Multiplies water reflection, so black would flatten it.
#   15 Sun Glare      — UESP says defaults to white, but 35 of 84 vanilla
#                       records ship BLACK and the rest are dark browns
#                       (e.g. SkyrimClear = 35,21,7). Copying the TES4 Sun
#                       colour here produced a blazing glare.
#   16 Moon Glare     — same story; vanilla mode is black.
#
# TES4 has no source colour for any of them, so use the vanilla-mode value.
_NAM0_SLOT_DEFAULTS = {
    13: (0, 0, 0),
    14: (255, 255, 255),
    15: (0, 0, 0),
    16: (0, 0, 0),
}

_TES5_NAM0_SLOTS = 17
_TES5_CLOUD_LAYERS = 32

# DALC face brightness relative to NAM0's Ambient colour, in xEdit's field
# order (X+, X-, Y+, Y-, Z+, Z-).  Medians measured over all 84 vanilla
# Skyrim.esm weather records; see _wthr_dalc.
_DALC_FACE_WEIGHTS = (0.98, 0.94, 0.96, 0.95, 0.67, 1.28)


def _wthr_nam0(rec: dict) -> bytes:
    """Remap the TES4 160-byte weather colour table into TES5's 272-byte one.

    Returns 272 bytes: 17 colour types x 4 times-of-day x RGBA.
    """
    raw = get_hex_bytes(rec, 'NAM0.Data')
    out = bytearray(_TES5_NAM0_SLOTS * 4 * 4)

    for slot, (r, g, b) in _NAM0_SLOT_DEFAULTS.items():
        for time in range(4):
            off = (slot * 4 + time) * 4
            out[off:off + 4] = bytes((r, g, b, 0))

    if not raw or len(raw) < 160:
        return bytes(out)

    for t5_slot, t4_slot in enumerate(_NAM0_TES5_FROM_TES4):
        if t4_slot is None:
            continue
        for time in range(4):
            src = (t4_slot * 4 + time) * 4
            dst = (t5_slot * 4 + time) * 4
            # TES4 stores RGBA with the 4th byte unused; TES5 is identical.
            out[dst:dst + 3] = raw[src:src + 3]
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


def _wthr_cloud_arrays(rec: dict, used_layers) -> bytes:
    """Build TES5's per-cloud-layer RNAM/QNAM/PNAM/JNAM arrays.

    TES4 has no per-layer data — it has exactly two cloud layers and a single
    speed byte for each.  Layers 0/1 therefore take the TES4 lower/upper cloud
    speed and colour; every other layer gets the vanilla neutral value.

    Sizes verified against references/Skyrim.esm (83/84 vanilla records):
      RNAM 32B  — cloud speed Y, u8 per layer, 0x7F = neutral (no drift)
      QNAM 32B  — cloud speed X, u8 per layer, 0x7F = neutral
      PNAM 512B — cloud colours, 32 layers x 4 times x RGBA
      JNAM 512B — cloud alphas, 32 layers x 4 times x f32
    """
    speed_lower = get_int(rec, 'DATA.CloudSpeedLower')
    speed_upper = get_int(rec, 'DATA.CloudSpeedUpper')

    rnam = bytearray(b'\x7F' * _TES5_CLOUD_LAYERS)
    qnam = bytearray(b'\x7F' * _TES5_CLOUD_LAYERS)
    rnam[0] = _cloud_speed_tes4_to_tes5(speed_lower)
    rnam[1] = _cloud_speed_tes4_to_tes5(speed_upper)

    # Cloud tints come from the TES4 colour table's Clouds-Lower/Clouds-Upper
    # rows so the layers match the sky they are drawn against.
    raw = get_hex_bytes(rec, 'NAM0.Data')
    pnam = bytearray(_TES5_CLOUD_LAYERS * 4 * 4)
    if raw and len(raw) >= 160:
        for layer, t4_slot in ((0, _T4_CLOUDS_LOWER), (1, _T4_CLOUDS_UPPER)):
            for time in range(4):
                src = (t4_slot * 4 + time) * 4
                dst = (layer * 4 + time) * 4
                pnam[dst:dst + 3] = raw[src:src + 3]

    # Cloud alpha, per layer per time-of-day.  Only the layers that actually
    # carry a texture may be opaque: a blanket 1.0 across all 32 layers asks
    # the engine to draw 30 fully-opaque empty layers on top of the sky.
    # TES4 has no per-layer alpha curve, so the two real layers are opaque and
    # the rest are transparent.
    alphas = [0.0] * (_TES5_CLOUD_LAYERS * 4)
    for layer in used_layers:
        for time in range(4):
            alphas[layer * 4 + time] = 1.0
    jnam = struct.pack('<%df' % (_TES5_CLOUD_LAYERS * 4), *alphas)

    return (pack_subrecord('RNAM', bytes(rnam))
            + pack_subrecord('QNAM', bytes(qnam))
            + pack_subrecord('PNAM', bytes(pnam))
            + pack_subrecord('JNAM', jnam))


def _wthr_dalc(rec: dict) -> bytes:
    """Build the four DALC directional-ambient blocks (sunrise/day/sunset/night).

    TES5 lights the world with a 6-direction ambient cube (X+/X-/Y+/Y-/Z+/Z-)
    that TES4 has no equivalent for, so it is derived from the TES4 Ambient
    colour for the same time of day.

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
            r, g, b = raw[src], raw[src + 1], raw[src + 2]
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


def convert_WTHR(rec: dict) -> bytes:
    """WTHR — Weather conversion.

    TES5 subrecord order (from wbDefinitionsTES5.pas):
    EDID, DNAM/CNAM/ANAM/BNAM (old, unused), cloud textures (00TX..O0TX),
    LNAM, MNAM, NNAM, ONAM(unused), RNAM, QNAM, PNAM, JNAM, NAM0, FNAM,
    DATA, NAM1, SNAM(sounds), TNAM(sky statics), IMSP, HNAM(SSE volumetric),
    DALC x4, NAM2, NAM3, MODL/MODT(aurora), GNAM
    """
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

    # Cloud layer textures — TES4's two layers become TES5 layers 0 and 1.
    lower_cloud = get_str(rec, 'CNAM.LowerCloudLayer')
    upper_cloud = get_str(rec, 'DNAM.UpperCloudLayer')
    used_layers = []
    for layer, path in ((0, lower_cloud), (1, upper_cloud)):
        if not path:
            continue
        sig = _wthr_cloud_sig(layer)
        path_bytes = _prefix_path(path).encode('utf-8') + b'\x00'
        subs += sig + struct.pack('<H', len(path_bytes)) + path_bytes
        used_layers.append(layer)

    # LNAM — Max Cloud Layers.  Vanilla is always 29 (0x1D); xEdit marks the
    # field required with that default.  0 made the engine allocate no layers.
    subs += pack_uint32_subrecord('LNAM', 29)

    # MNAM (Precipitation Type -> SPGD) and NNAM (Visual Effect -> RFCT) are
    # .SetRequired in xEdit.  TES4 drives precipitation from the weather's own
    # particle textures and has no record to map here, so emit the explicit
    # NULL that vanilla records use rather than omitting the subrecord.
    subs += pack_formid_subrecord('MNAM', 0)
    subs += pack_formid_subrecord('NNAM', 0)

    # RNAM/QNAM/PNAM/JNAM — per-cloud-layer speed, colour and alpha.
    subs += _wthr_cloud_arrays(rec, used_layers)

    # NAM0 — weather colours, remapped from TES4's 10 types to TES5's 17.
    subs += pack_subrecord('NAM0', _wthr_nam0(rec))

    # FNAM — Fog distances (TES5: 32 bytes — 8 floats)
    fog_day_near = get_float(rec, 'FNAM.FogDayNear', 100.0)
    fog_day_far = get_float(rec, 'FNAM.FogDayFar', 100000.0)
    fog_night_near = get_float(rec, 'FNAM.FogNightNear', 100.0)
    fog_night_far = get_float(rec, 'FNAM.FogNightFar', 100000.0)
    fnam = struct.pack('<ffffffff',
                        fog_day_near, fog_day_far,
                        fog_night_near, fog_night_far,
                        1.0, 1.0,    # Day/Night power
                        1.0, 1.0)    # Day/Night max
    subs += pack_subrecord('FNAM', fnam)

    # DATA — Weather Data (19 bytes in TES5).
    #
    # TES5 reuses TES4's field order but replaces TES4's two cloud-speed bytes
    # (offsets 1-2) with padding, having moved per-layer speed into RNAM/QNAM,
    # and appends four fields TES4 has no source for.
    data = struct.pack(
        '<B2xBBBBBBBBBBBBBBBB',
        get_int(rec, 'DATA.WindSpeed'),
        get_int(rec, 'DATA.TransDelta'),
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

    # Sounds — SNAM (after NAM1 per xEdit)
    sc = get_int(rec, 'SoundCount')
    for i in range(sc):
        sfid = get_formid(rec, f'Sound[{i}].FormID')
        stype = get_int(rec, f'Sound[{i}].Type')
        if sfid:
            subs += pack_subrecord('SNAM', struct.pack('<II', sfid, stype))

    # IMSP — Image Spaces (sunrise/day/sunset/night).  TES4 drives tone
    # mapping from the weather's own HNAM HDR block, which TES5 moved into
    # IMGS records; point at Skyrim's default imagespace (0x161) as every
    # vanilla record does rather than leaving four null FormIDs.
    subs += pack_subrecord('IMSP', struct.pack('<IIII', *([_DEFAULT_IMGS] * 4)))

    # DALC — Directional Ambient Lighting Colors (4 x 32 bytes)
    subs += _wthr_dalc(rec)

    return pack_record('WTHR', get_formid(rec, 'FormID'), get_int(rec, 'RecordFlags'), subs)


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

    # TNAM — 6-byte timing struct, identical in both games:
    # sunrise begin/end, sunset begin/end (units of 10 minutes), volatility,
    # and a packed moons/phase-length byte.
    subs += pack_subrecord('TNAM', bytes((
        get_int(rec, 'TNAM.SunriseBegin'),
        get_int(rec, 'TNAM.SunriseEnd'),
        get_int(rec, 'TNAM.SunsetBegin'),
        get_int(rec, 'TNAM.SunsetEnd'),
        get_int(rec, 'TNAM.Volatility'),
        get_int(rec, 'TNAM.MoonsPhaseLength'),
    )))

    return pack_record('CLMT', get_formid(rec, 'FormID'), get_int(rec, 'RecordFlags'), subs)
