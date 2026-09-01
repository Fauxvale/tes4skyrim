"""WTHR and CLMT conversion: weather, climate and the image spaces they drive.

Split out of the former dialog_misc.py, which held SOUN and WTHR together and
no dialogue at all.

See: docs/commentary/tes5_import_weather.md
"""

import struct

from ..text_reader import get_hex_bytes
from .common import (
    _prefix_path,
    get_float,
    get_formid,
    get_int,
    get_str,
    pack_formid_subrecord,
    pack_record,
    pack_string_subrecord,
    pack_subrecord,
    pack_uint32_subrecord,
)
from .sound import _SNDR_FOR_SOUN, get_soun_identity


_DEFAULT_IMGS = 0x00000161

_TES4_EYE_ADAPT_DEFAULT = 0.7

#: IMGS.HNAM field names, in slot order; _IMGS_HNAM_RANGES is parallel to this.
_IMGS_HNAM_FIELDS = (
    'Eye Adapt Speed', 'Bloom Blur Radius', 'Bloom Threshold', 'Bloom Scale',
    'Receive Bloom Threshold', 'White', 'Sunlight Scale', 'Sky Scale',
    'Eye Adapt Strength',
)

#: Clamp range per _IMGS_HNAM_FIELDS slot, from the 213 weather-used vanilla imagespaces.
_IMGS_HNAM_RANGES = (
    (15.0, 50.0), (0.8, 8.0), (0.0, 0.80), (0.0, 7.0), (0.2, 1.0),
    (0.6, 1.075), (0.4, 3.85), (0.0, 0.45), (1.0, 30.0),
)

_IMGS_SLOT_NAMES = ('Dawn', 'Day', 'Dusk', 'Night')
_IMGS_SLOT_EYE_ADAPT_STRENGTH = (15.0, 5.0, 15.0, 20.0)
_IMGS_SLOT_EYE_ADAPT_BIAS = (0.925, 1.0, 0.925, 1.125)
_IMGS_SLOT_SUNLIGHT_BIAS = (0.974, 1.0, 0.974, 0.789)

#: TES4 DATA.Classification bits (xEdit wbDefinitionsTES4, WTHR).
_WTHR_CLASS_PLEASANT = 0x01
_WTHR_CLASS_CLOUDY   = 0x02
_WTHR_CLASS_RAINY    = 0x04
_WTHR_CLASS_SNOW     = 0x08
_WTHR_CLASSIFICATION_MASK = 0x0F

#: Sky Scale per (Dawn, Day, Dusk, Night), keyed by TES4 classification bit.
_IMGS_SKY_SCALE_BY_CLASS = {
    _WTHR_CLASS_PLEASANT: (0.080, 0.120, 0.100, 0.020),
    _WTHR_CLASS_CLOUDY:   (0.050, 0.100, 0.050, 0.000),
    _WTHR_CLASS_RAINY:    (0.090, 0.100, 0.100, 0.060),
    _WTHR_CLASS_SNOW:     (0.100, 0.050, 0.050, 0.050),
}
_IMGS_SKY_SCALE_UNCLASSIFIED = (0.050, 0.050, 0.050, 0.050)

#: tes4 field -> (t4_median, gain, per-slot medians, per-slot p10 lo, per-slot p90 hi).
_IMGS_ANCHORED_FIELDS = {
    'BrightClamp':   (0.30, 1.0, (0.375, 0.625, 0.475, 0.375),
                      (0.30, 0.30, 0.295, 0.25), (0.70, 0.715, 0.70, 0.70)),
    'BrightScale':   (2.00, 0.5, (3.0, 3.0, 3.0, 3.2),
                      (2.50, 2.35, 2.50, 2.50), (4.0, 4.0, 4.0, 4.0)),
    'TargetLum':     (1.20, 0.3, (0.55, 0.625, 0.60, 0.55),
                      (0.40, 0.50, 0.475, 0.40), (1.0, 1.0, 1.0, 1.0)),
    'UpperLumClamp': (1.00, 0.25, (0.925, 1.0, 0.90, 0.925),
                      (0.875, 0.875, 0.875, 0.875), (1.0, 1.05, 1.0, 1.0)),
}

_IMGS_FIELD_RESCALE = {
    'SunlightDimmer': (0.50, 2.00, 0.90, 2.70),
}

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
    """The IMGS Sky Scale for this weather and time slot.

    A LOOKUP on the authored TES4 classification bit, not a rescale of any
    TES4 field: Sky Scale feeds back into sky colour, so deriving it from the
    colour is a loop.

    See: docs/commentary/tes5_import_weather.md#the-actual-defect-sky-scale-is-derived-from-sky-colour-feedback-loop
    """
    cls = get_int(rec, 'DATA.Classification') & _WTHR_CLASSIFICATION_MASK
    for bit in (0x01, 0x02, 0x04, 0x08):
        if cls & bit:
            return _IMGS_SKY_SCALE_BY_CLASS[bit][time]
    return _IMGS_SKY_SCALE_UNCLASSIFIED[time]


def _wthr_imgs(rec: dict, imgs_fid: int, time: int) -> bytes:
    """Build one time-of-day IMGS carrying this weather's HDR tone mapping."""
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

    speed = _TES4_EYE_ADAPT_DEFAULT
    if authored:
        speed = get_float(rec, 'HNAM.EyeAdaptSpeed', _TES4_EYE_ADAPT_DEFAULT)
    speed = max(0.0, min(1.0, speed))
    lo, hi = _IMGS_HNAM_RANGES[0]
    eye_adapt = (lo + speed * (hi - lo)) * _IMGS_SLOT_EYE_ADAPT_BIAS[time]

    sunlight = 1.9
    if authored:
        sunlight = _rescale('SunlightDimmer',
                            get_float(rec, 'HNAM.SunlightDimmer', 1.3), 1.9)
    sunlight *= _IMGS_SLOT_SUNLIGHT_BIAS[time]

    sky_scale = _imgs_sky_scale(rec, time)

    values = [
        eye_adapt,
        _IMGS_BLOOM_BLUR_RADIUS,
        anchored('BrightClamp'),
        anchored('BrightScale'),
        anchored('TargetLum'),
        anchored('UpperLumClamp'),
        sunlight,
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
    subs += pack_subrecord('CNAM', struct.pack('<3f', 1.0, 1.0, 1.0))
    subs += pack_subrecord('TNAM', struct.pack('<4f', 0.0, 1.0, 1.0, 1.0))
    subs += pack_subrecord('DNAM', struct.pack(
        '<3f2xH', 0.5, 20000.0, 20000.0, 16816))
    return pack_record('IMGS', imgs_fid, 0, subs)


_WTHR_EDID_COLLISIONS = frozenset({'DefaultWeather'})



def _wthr_flags(rec: dict) -> int:
    """TES5 DATA weather-classification flags from the TES4 classification byte.

    Bits 0-3 (Pleasant/Cloudy/Rainy/Snow) are shared; bits 4-5 are TES5's
    aurora controls.  Weather with no classification at all is treated as
    Pleasant so the engine's weather-transition picker can still match it.
    """
    flags = get_int(rec, 'DATA.Classification') & _WTHR_CLASSIFICATION_MASK
    if flags == 0:
        flags = 0x01
    return flags


#: Vanilla precipitation particle systems: RainParticles (7 weathers), RainStorm, SnowParticlesMed.
_SPGD_RAIN       = 0x00023C48
_SPGD_RAIN_STORM = 0x0010780F
_SPGD_SNOW       = 0x00023C49


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


_T4_SKY_UPPER, _T4_FOG, _T4_CLOUDS_LOWER, _T4_AMBIENT = 0, 1, 2, 3
_T4_SUNLIGHT = 4
_T4_SUNLIGHT, _T4_SUN, _T4_STARS, _T4_SKY_LOWER = 4, 5, 6, 7
_T4_HORIZON, _T4_CLOUDS_UPPER = 8, 9

#: TES5 NAM0 slot names, from the decompiled TESWeather::ColorTypes enum.
_NAM0_SLOT_NAMES = (
    'Sky-Upper', 'Fog Near', 'Unused', 'Ambient', 'Sunlight', 'Sun', 'Stars',
    'Sky-Lower', 'Horizon', 'Effect Lighting', 'Cloud LOD Diffuse',
    'Cloud LOD Ambient', 'Fog Far', 'Sky Statics', 'Water Multiplier',
    'Sun Glare', 'Moon Glare',
)

#: TES4 colour index feeding each _NAM0_SLOT_NAMES slot; None means no TES4 source.
_NAM0_TES5_FROM_TES4 = [
    _T4_SKY_UPPER, _T4_FOG, None, _T4_AMBIENT, _T4_SUNLIGHT, _T4_SUN,
    _T4_STARS, _T4_SKY_LOWER, _T4_HORIZON, None, _T4_CLOUDS_UPPER,
    _T4_CLOUDS_LOWER, _T4_FOG, None, None, None, None,
]

_NAM0_SLOT_CLASS_DEFAULTS = {
    _WTHR_CLASS_PLEASANT: {
        13: ((174, 159, 159), (218, 222, 236), (176, 146, 133), (62, 93, 108)),
        14: ((143, 170, 186), (162, 225, 239), (79, 108, 120), (31, 63, 75)),
        15: ((74, 28, 0), (72, 58, 57), (74, 28, 0), (0, 0, 0)),
        16: ((102, 73, 49), (90, 61, 50), (96, 66, 49), (255, 173, 138)),
    },
    _WTHR_CLASS_CLOUDY: {
        13: ((151, 165, 176), (172, 174, 188), (149, 161, 162), (86, 106, 124)),
        14: ((180, 190, 191), (180, 190, 199), (152, 159, 167), (31, 63, 75)),
        15: ((0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0)),
        16: ((0, 0, 0), (0, 0, 0), (0, 0, 0), (57, 22, 0)),
    },
    _WTHR_CLASS_RAINY: {
        13: ((107, 105, 100), (142, 161, 173), (98, 122, 119), (73, 85, 100)),
        14: ((172, 177, 185), (164, 196, 210), (100, 135, 176), (31, 63, 75)),
        15: ((0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0)),
        16: ((0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0)),
    },
    _WTHR_CLASS_SNOW: {
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

#: TES5 NAM0 slot indices addressed by name; see _NAM0_SLOT_NAMES for the full map.
_T5_STARS = 6
_TES5_CLOUD_LAYERS = 32

#: Below _NAM0_KNEE an authored colour passes through; 255 maps to the ceiling.
_NAM0_KNEE = 160.0
_NAM0_KNEE_CEILING = 200.0
_NAM0_SUN_KNEE = 30.0
_NAM0_SUN_CEILING = 60.0
_T5_SUN = 5
_T5_AMBIENT = 3
_T5_SUNLIGHT = 4
_T5_SUN_GLARE = 15

_SUNLESS_SUN_TEXTURE = 'Black.dds'

#: TES4 sun sprites meaning "no sun"; matched case-insensitively on the basename.
_SUNLESS_SUN_MARKERS = ('void.dds',)

_SUNLESS_WEATHER_FIDS = set()


def _clmt_is_sunless(rec: dict) -> bool:
    r"""True when a TES4 climate authors no visible sun.

    Two authored idioms, both meaning the same thing:
      * FNAM names a void/black sun sprite, or
      * the record carries NO FNAM at all (Oblivion draws nothing).
    A climate with a real FNAM (Sky\Sun.dds) is never sunless.
    """
    sun = (get_str(rec, 'FNAM.SunTexture') or '').strip()
    if not sun:
        return True
    base = sun.replace('/', '\\').rsplit('\\', 1)[-1].lower()
    return base in _SUNLESS_SUN_MARKERS


def record_sunless_climate(rec: dict) -> None:
    """Register a sunless climate's weathers so convert_WTHR can zero their sun.

    Called for EVERY climate; only sunless ones contribute.  Idempotent.
    """
    if not _clmt_is_sunless(rec):
        return
    for i in range(get_int(rec, 'WeatherCount')):
        fid = get_formid(rec, f'Weather[{i}].FormID')
        if fid:
            _SUNLESS_WEATHER_FIDS.add(fid)


def reset_sunless_climates() -> None:
    """Clear the registry between plugins (and between tests)."""
    _SUNLESS_WEATHER_FIDS.clear()

#: Per-slot (p10, p90) luminance from vanilla; keys index _NAM0_SLOT_NAMES.
_NAM0_VANILLA_LUM = {
    0:  ((86.4, 84.3, 85.4, 21.3), (161.8, 155.8, 167.3, 114.8)),
    1:  ((85.2, 95.9, 80.5, 52.3), (137.6, 137.6, 138.4, 117.1)),
    3:  ((130.0, 172.2, 105.8, 76.4), (166.0, 217.3, 178.0, 100.8)),
    4:  ((131.6, 151.7, 116.2, 77.7), (197.3, 214.0, 189.2, 146.4)),
    5:  ((38.6, 43.0, 33.9, 0.0), (54.9, 121.1, 77.5, 0.0)),
    6:  ((0.0, 0.0, 0.0, 236.0), (255.0, 255.0, 255.0, 255.0)),
    7:  ((133.1, 122.6, 107.2, 47.7), (202.8, 136.0, 168.0, 89.4)),
    8:  ((90.3, 140.2, 89.6, 30.8), (155.7, 196.6, 123.4, 89.6)),
    12: ((113.7, 166.4, 103.7, 38.6), (168.6, 188.9, 170.8, 113.5)),
}

_NAM0_EFFECT_LIGHTING = ((150, 163, 158), (198, 193, 193),
                         (159, 129, 116), (84, 148, 166))

_PNAM_KNEE = _NAM0_KNEE
_PNAM_KNEE_CEILING = _NAM0_KNEE_CEILING


def _lum(r, g, b):
    """Rec.601 luminance of an RGB triple."""
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
    """Scale an RGB triple so its luminance matches `target`, preserving hue.

    See: docs/commentary/tes5_import_weather.md#colours-are-luminance-normalized-not-copied-the-real-bloom-source
    """
    if t5_slot == _T5_SUN:
        return _knee_rgb(r, g, b, _NAM0_SUN_KNEE, _NAM0_SUN_CEILING)
    return _knee_rgb(r, g, b, _NAM0_KNEE, _NAM0_KNEE_CEILING)

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

    for time, (r, g, b) in enumerate(_NAM0_EFFECT_LIGHTING):
        off = (9 * 4 + time) * 4
        out[off:off + 4] = bytes((r, g, b, 0))

    if not raw or len(raw) < 160:
        return bytes(out)

    for t5_slot, t4_slot in enumerate(_NAM0_TES5_FROM_TES4):
        if t4_slot is None:
            continue
        if t5_slot in (10, 11):
            continue
        for time in range(4):
            src = (t4_slot * 4 + time) * 4
            dst = (t5_slot * 4 + time) * 4
            r, g, b = raw[src], raw[src + 1], raw[src + 2]
            if t5_slot in _NAM0_VANILLA_LUM:
                r, g, b = _normalize_rgb(t5_slot, time, r, g, b)
            out[dst:dst + 3] = bytes((r, g, b))

    cls = get_int(rec, 'DATA.Classification') & _WTHR_CLASSIFICATION_MASK
    if cls & (0x04 | 0x08):
        for time in range(4):
            dst = (_T5_STARS * 4 + time) * 4
            out[dst:dst + 3] = b'\x00\x00\x00'

    if get_formid(rec, 'FormID') in _SUNLESS_WEATHER_FIDS:
        _nam0_kill_sun(out, raw)
    return bytes(out)


def _nam0_kill_sun(out: bytearray, raw: bytes) -> None:
    """Blank the sun-bearing NAM0 slots for a sunless (realm) weather.

    Zeroes slots 5 (Sun) and 15 (Sun Glare) and folds the sunlight slot into
    ambient so the scene keeps its overall level with no sun disc.

    See: docs/commentary/tes5_import_weather.md#sunless-skies--the-oblivion-realms-must-have-no-sun
    """
    for time in range(4):
        for slot in (_T5_SUN, _T5_SUN_GLARE):
            dst = (slot * 4 + time) * 4
            out[dst:dst + 3] = b'\x00\x00\x00'

    if not raw or len(raw) < 160:
        return

    for time in range(4):
        dst = (_T5_SUNLIGHT * 4 + time) * 4
        out[dst:dst + 3] = b'\x00\x00\x00'
        amb = (_T5_AMBIENT * 4 + time) * 4
        out[amb:amb + 3] = bytes(
            _fold_sunlight_into_ambient(raw, time,
                                        out[amb], out[amb + 1], out[amb + 2]))


def _fold_sunlight_into_ambient(raw: bytes, time: int,
                                ar: int, ag: int, ab: int) -> tuple:
    """Merge slot 4 (Sunlight) into slot 3 (Ambient) for a sunless weather.

    Skyrim lights the scene from the sun even when the disc is hidden, so a
    zeroed sunlight slot alone leaves the realm black.

    See: docs/commentary/tes5_import_weather.md#sunless-skies--the-oblivion-realms-must-have-no-sun
    """
    if not raw or len(raw) < 160:
        return (ar, ag, ab)
    src = (_T4_SUNLIGHT * 4 + time) * 4
    sr, sg, sb = raw[src], raw[src + 1], raw[src + 2]
    if not (sr or sg or sb):
        return (ar, ag, ab)
    return (min(255, ar + sr // 2), min(255, ag + sg // 2),
            min(255, ab + sb // 2))


def _cloud_speed_tes4_to_tes5(speed: int) -> int:
    """TES4 cloud-speed byte -> TES5 RNAM/QNAM (signed, 0x7F = 0, max 254).

    See: docs/commentary/tes5_import_weather.md#rnam--qnam-cloud-speed--a-real-unit-conversion
    """
    speed = max(0, min(255, speed))
    return min(254, 127 + round(speed / 255.0 * 127.0))


def _cloud_speed_signed_tes5(speed: float) -> int:
    """Encode a signed -0.1..+0.1 drift as a TES5 byte (0x7F = 0, max 254).
    """
    speed = max(-255.0, min(255.0, speed))
    return max(0, min(254, 127 + round(speed / 255.0 * 127.0)))


_WTHR_CLOUD_X_DRIFT = -0.35


#: TES5 cloud-layer slots the TES4 upper/lower layers map onto (09_CDTop, 14_CDLower).
_WTHR_UPPER_LAYER = 11
_WTHR_LOWER_LAYER = 27

_WTHR_LNAM_MIN = 1

_WTHR_FOG_MIN_RAMP = 9000.0

_WTHR_FOG_POWER = 1.0
_WTHR_FOG_MAX = 1.0

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
    """The RNAM/QNAM/PNAM/JNAM cloud arrays, one byte per TES5 layer.

    RNAM/QNAM are X/Y drift speeds, PNAM the per-layer colour index and JNAM
    the alpha. Layers not in `layer_plan` keep 0x7F (no drift).

    See: docs/commentary/tes5_import_weather.md#cloudsupdate--0x3c52e0--lnam-nam1-rnamqnam-pnamjnam
    """
    speed_lower = get_int(rec, 'DATA.CloudSpeedLower')
    speed_upper = get_int(rec, 'DATA.CloudSpeedUpper')
    lower_speed = _cloud_speed_tes4_to_tes5(speed_lower)
    upper_speed = _cloud_speed_tes4_to_tes5(speed_upper)

    upper_cloud = get_str(rec, 'DNAM.UpperCloudLayer')

    rnam = bytearray(b'\x7F' * _TES5_CLOUD_LAYERS)
    qnam = bytearray(b'\x7F' * _TES5_CLOUD_LAYERS)

    raw = get_hex_bytes(rec, 'NAM0.Data')
    pnam = bytearray(_TES5_CLOUD_LAYERS * 4 * 4)
    alphas = [0.0] * (_TES5_CLOUD_LAYERS * 4)

    def tint(t4_slot, time):
        """The TES4 cloud tint, highlight-compressed like the sky slots (no time axis)."""
        src = (t4_slot * 4 + time) * 4
        return _knee_rgb(raw[src], raw[src + 1], raw[src + 2],
                         _PNAM_KNEE, _PNAM_KNEE_CEILING)

    for layer, texture, layer_alphas in layer_plan:
        if not 0 <= layer < _TES5_CLOUD_LAYERS:
            continue
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


    jnam = struct.pack('<%df' % (_TES5_CLOUD_LAYERS * 4), *alphas)

    return (pack_subrecord('RNAM', bytes(rnam))
            + pack_subrecord('QNAM', bytes(qnam))
            + pack_subrecord('PNAM', bytes(pnam))
            + pack_subrecord('JNAM', jnam))


def _wthr_dalc(rec: dict) -> bytes:
    """The 32-byte DALC directional-ambient block for one time slot.

    Layout (wbAmbientColors, form version >= 34): 6 x RGBA directional +
    Specular RGBA + Fresnel Power f32 = 32 bytes. Fresnel is 1.0 in every
    vanilla record. TES4 has no source, so the directions are derived from
    the weather ambient by per-face weights.

    See: docs/commentary/tes5_import_weather.md#dalc-directional-ambient-4-x-32-bytes
    """
    raw = get_hex_bytes(rec, 'NAM0.Data')
    sunless = get_formid(rec, 'FormID') in _SUNLESS_WEATHER_FIDS
    out = b''
    for time in range(4):
        if raw and len(raw) >= 160:
            src = (_T4_AMBIENT * 4 + time) * 4
            r, g, b = _normalize_rgb(3, time,
                                     raw[src], raw[src + 1], raw[src + 2])
            if sunless:
                r, g, b = _fold_sunlight_into_ambient(raw, time, r, g, b)
        else:
            r = g = b = 0
        block = bytearray()
        for weight in _DALC_FACE_WEIGHTS:
            block += bytes((min(255, round(r * weight)),
                            min(255, round(g * weight)),
                            min(255, round(b * weight)), 0))
        block += b'\x00\x00\x00\x00'
        block += struct.pack('<f', 1.0)
        out += pack_subrecord('DALC', bytes(block))
    return out


_WTHR_SND_PRECIP, _WTHR_SND_WIND, _WTHR_SND_THUNDER = 1, 2, 3

_WTHR_SND_KEYWORDS = (
    (_WTHR_SND_THUNDER, ('thunder', 'lightning', 'donner', 'blitz')),
    (_WTHR_SND_PRECIP, ('rain', 'regen', 'snow', 'schnee', 'sleet', 'hail',
                        'storm', 'sturm', 'drizzle')),
    (_WTHR_SND_WIND, ('wind', 'gust', 'breeze', 'boe')),
)

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
    if any(k in hay for k in _WTHR_SND_NON_WEATHER):
        return None
    for stype, keys in _WTHR_SND_KEYWORDS:
        if any(k in hay for k in keys):
            return stype
    return None


_TES4_WTHR_PLEASANT = 0x01
_TES4_WTHR_CLOUDY = 0x02
_TES4_WTHR_RAINY = 0x04
_TES4_WTHR_SNOW = 0x08


def _wthr_is_fair(rec: dict) -> bool:
    """True when this weather should read as fair sky for sound classification.

    Classification bits win; an unclassified weather that authors thunder is
    a storm, not a fair sky.

    See: docs/commentary/tes5_import_weather.md#wthr-field-semantics-where-equivalence
    """
    flags = get_int(rec, 'DATA.Classification')
    if flags & (_TES4_WTHR_RAINY | _TES4_WTHR_SNOW):
        return False
    if flags & (_TES4_WTHR_PLEASANT | _TES4_WTHR_CLOUDY):
        return True
    return get_int(rec, 'DATA.ThunderFrequency', 255) >= 255


def _wthr_sounds(rec: dict) -> bytes:
    """Build the WTHR SNAM entries from the TES4 sound list.

    The FormID written is the TES4 SOUN id -- a placeholder resolved to the
    real SNDR by patch_weather_sounds after Phase 3.

    FAIR-WEATHER SKIES GET NO BED AT ALL: Skyrim's weather channel is global
    and continuous, so an Oblivion fair-weather wind loop becomes wind howling
    over the whole province in bright sunshine. Vanilla never does this.

    See: docs/commentary/tes5_import_weather.md#weather-sounds-ambience-loops-go-in-audiocategoryamb
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
        key = (sfid, stype)
        if key in seen:
            continue
        seen.add(key)
        out += pack_subrecord('SNAM', struct.pack('<II', sfid, stype))
    return out


def _patch_wthr_snam(blob: bytes, mapping: dict, own: set, bound: set) -> tuple:
    """Rewrite one WTHR's SNAM ids in place. Returns (bytearray, changed)."""
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
                out += chunk
                continue
            sndr = mapping.get(soun & 0x00FFFFFF, 0)
            if sndr != soun:
                changed = True
            if sndr:
                bound.add(sndr)
                out += chunk[:6] + struct.pack('<II', sndr, stype)
            continue
        out += chunk
    return out, changed


def patch_weather_sounds(writer, own_soun_ids=None) -> int:
    """Rewrite every WTHR SNAM from its TES4 SOUN id to the SNDR descriptor.

    TES5 weather sounds reference a sound DESCRIPTOR, not a SOUN, and our
    convert_SOUN emits `EDID + OBND + SDSC` only -- the audio data lives on
    the companion SNDR. WTHR is written in Phase 2, before Phase 3 creates
    those descriptors, so convert_WTHR stores the SOUN id and this resolves
    it afterwards.

    See: docs/commentary/tes5_import_weather.md#weather-sounds-ambience-loops-go-in-audiocategoryamb
    """
    mapping = _SNDR_FOR_SOUN
    if not mapping:
        return 0
    own = own_soun_ids if own_soun_ids is not None else set(mapping)
    records = writer._top_groups.get('WTHR') or []
    patched = 0
    bound = set()
    for i, blob in enumerate(records):
        if b'SNAM' not in blob:
            continue
        out, changed = _patch_wthr_snam(blob, mapping, own, bound)
        if not changed:
            continue
        struct.pack_into('<I', out, 4, len(out) - 24)
        records[i] = bytes(out)
        patched += 1
    _retune_weather_descriptors(writer, bound)
    return patched


_AUDIO_CAT_AMB = 0x0007F80B
_AUDIO_CAT_SFX = 0x000172A1


def _sndr_is_looping(blob: bytes) -> bool:
    """True when this SNDR's LNAM marks it a loop."""
    pos = 24
    while pos + 6 <= len(blob):
        sig = blob[pos:pos + 4]
        size = struct.unpack_from('<H', blob, pos + 4)[0]
        if sig == b'LNAM' and size == 4 and blob[pos + 7]:
            return True
        pos += 6 + size
    return False


def _sndr_gnam_to_ambience(blob: bytes) -> tuple:
    """Move an SFX-category GNAM to ambience. Returns (bytearray, changed)."""
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
    return out, changed


def _retune_weather_descriptors(writer, bound) -> int:
    """Route every descriptor a weather uses into the ambience category.

    convert_SOUN files a sound as ambience only when TES4 marked it 2D. A
    weather bed is different: the weather channel plays it across the whole
    sky for as long as the weather holds, so it IS ambience whatever its 2D
    flag says. Only non-looping beds (thunder cracks) stay on SFX.

    See: docs/commentary/tes5_import_weather.md#weather-sounds-ambience-loops-go-in-audiocategoryamb
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
        if not _sndr_is_looping(blob):
            continue
        out, changed = _sndr_gnam_to_ambience(blob)
        if not changed:
            continue
        struct.pack_into('<I', out, 4, len(out) - 24)
        records[i] = bytes(out)
        retuned += 1
    return retuned


def _wthr_cloud_textures(layer_plan) -> tuple:
    """Packed cloud-layer texture subrecords. Returns (bytes, [layer index])."""
    subs = b''
    used = []
    for layer, path, _alphas in layer_plan:
        path_bytes = _prefix_path(path).encode('utf-8') + b'\x00'
        subs += (_wthr_cloud_sig(layer)
                 + struct.pack('<H', len(path_bytes)) + path_bytes)
        used.append(layer)
    return subs, used


def _wthr_fnam(rec: dict) -> bytes:
    """The 32-byte TES5 fog struct; a far plane at or behind near gets a ramp."""
    day_near = max(0.0, get_float(rec, 'FNAM.FogDayNear', 100.0))
    day_far = get_float(rec, 'FNAM.FogDayFar', 100000.0)
    night_near = max(0.0, get_float(rec, 'FNAM.FogNightNear', 100.0))
    night_far = get_float(rec, 'FNAM.FogNightFar', 100000.0)
    if day_far <= day_near:
        day_far = day_near + _WTHR_FOG_MIN_RAMP
    if night_far <= night_near:
        night_far = night_near + _WTHR_FOG_MIN_RAMP
    return struct.pack('<ffffffff', day_near, day_far, night_near, night_far,
                       _WTHR_FOG_POWER, _WTHR_FOG_POWER,
                       _WTHR_FOG_MAX, _WTHR_FOG_MAX)


def convert_WTHR(rec: dict, writer=None) -> tuple:
    """WTHR — Weather conversion.  Returns (wthr_bytes, [imgs_bytes, ...]).

    TES5 subrecord order (from wbDefinitionsTES5.pas):
    EDID, DNAM/CNAM/ANAM/BNAM (old, unused), cloud textures (00TX..O0TX),
    LNAM, MNAM, NNAM, ONAM(unused), RNAM, QNAM, PNAM, JNAM, NAM0, FNAM,
    DATA, NAM1, SNAM(sounds), TNAM(sky statics), IMSP, HNAM(SSE volumetric),
    DALC x4, NAM2, NAM3, MODL/MODT(aurora), GNAM
    """
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
        if edid in _WTHR_EDID_COLLISIONS:
            edid = 'TES4' + edid
        subs += pack_string_subrecord('EDID', edid)

    layer_plan = _wthr_cloud_layer_plan(
        get_str(rec, 'CNAM.LowerCloudLayer'),
        get_str(rec, 'DNAM.UpperCloudLayer'))
    layer_subs, used_layers = _wthr_cloud_textures(layer_plan)
    subs += layer_subs

    subs += pack_uint32_subrecord(
        'LNAM', max(_WTHR_LNAM_MIN, (max(used_layers) + 1) if used_layers
                    else _WTHR_LNAM_MIN))

    subs += pack_formid_subrecord('MNAM', _wthr_precipitation(rec))
    subs += pack_formid_subrecord('NNAM', 0)

    subs += _wthr_cloud_arrays(rec, layer_plan)

    subs += pack_subrecord('NAM0', _wthr_nam0(rec))

    subs += pack_subrecord('FNAM', _wthr_fnam(rec))

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
        0,
        0,
        0,
        0,
    )
    subs += pack_subrecord('DATA', data)

    disabled = 0xFFFFFFFF
    for layer in used_layers:
        disabled &= ~(1 << layer)
    subs += pack_uint32_subrecord('NAM1', disabled & 0xFFFFFFFF)

    subs += _wthr_sounds(rec)

    subs += pack_subrecord('IMSP', struct.pack('<IIII', *imgs_fids))

    subs += _wthr_dalc(rec)

    wthr_bytes = pack_record('WTHR', get_formid(rec, 'FormID'),
                             get_int(rec, 'RecordFlags'), subs)
    return wthr_bytes, imgs_bytes


_DEFAULT_STARS_MODEL = 'Sky\\Stars.nif'


def convert_CLMT(rec: dict) -> bytes:
    """CLMT -- Climate.

    Near-identical between games: same WLST weather list, same FNAM/GNAM sun
    textures, same 6-byte TNAM timing struct. The only format change is that
    TES5's WLST entry gained a trailing Global FormID, widening it from 8 to
    12 bytes.

    Without this record the converted WTHR records are unreachable: weather
    is selected through WRLD -> CNAM -> CLMT -> WLST, never directly.

    See: docs/commentary/tes5_import_weather.md#clmt
    """
    subs = b''
    edid = get_str(rec, 'EditorID')
    if edid:
        subs += pack_string_subrecord('EDID', edid)

    wlst = b''
    for i in range(get_int(rec, 'WeatherCount')):
        wfid = get_formid(rec, f'Weather[{i}].FormID')
        if not wfid:
            continue
        chance = get_int(rec, f'Weather[{i}].Chance')
        wlst += struct.pack('<IiI', wfid, chance, 0)
    if wlst:
        subs += pack_subrecord('WLST', wlst)

    if _clmt_is_sunless(rec):
        subs += pack_string_subrecord('FNAM', _SUNLESS_SUN_TEXTURE)
        subs += pack_string_subrecord('GNAM', _SUNLESS_SUN_TEXTURE)
    else:
        sun = get_str(rec, 'FNAM.SunTexture')
        if sun:
            subs += pack_string_subrecord('FNAM', _prefix_path(sun))
        glare = get_str(rec, 'GNAM.GlareTexture')
        if glare:
            subs += pack_string_subrecord('GNAM', _prefix_path(glare))

    model = get_str(rec, 'Model.MODL') or _DEFAULT_STARS_MODEL
    subs += pack_string_subrecord('MODL', _prefix_path(model))
    subs += pack_subrecord('MODT', struct.pack('<III', 2, 0, 0))

    subs += pack_subrecord('TNAM', bytes((
        get_int(rec, 'TNAM.SunriseBegin'),
        get_int(rec, 'TNAM.SunriseEnd'),
        get_int(rec, 'TNAM.SunsetBegin'),
        get_int(rec, 'TNAM.SunsetEnd'),
        50,
        get_int(rec, 'TNAM.MoonsPhaseLength'),
    )))

    return pack_record('CLMT', get_formid(rec, 'FormID'), get_int(rec, 'RecordFlags'), subs)
