"""FO3/FNV weather colour tables: six times of day re-strided to four.

FalloutNV authors NAM0 with SIX times of day where TES4 and TES5 use four, so
a TES4-shaped reader indexes into the wrong colour entirely. Its ten slots
already carry the TES5 meanings, so only the time stride needs changing.

See: docs/commentary/tes5_import_weather.md#fo3fnv-nam0-six-times-of-day
"""

#: Colour slots in a NAM0 table; the first ten are common to TES4, FO3/FNV and TES5.
NAM0_SLOTS = 10

#: Bytes per colour entry: RGBA, alpha unused.
_RGBA = 4

#: TES4 and TES5 author four times of day; FO3/FNV appends High Noon and Midnight.
TES4_TIMES = 4
FALLOUT_TIMES = 6

#: A NAM0 blob of exactly this size uses the six-time FO3/FNV layout.
FALLOUT_NAM0_SIZE = NAM0_SLOTS * FALLOUT_TIMES * _RGBA


def is_fallout_nam0(raw: bytes) -> bool:
    """Whether this NAM0 blob uses the six-time FO3/FNV layout."""
    return len(raw) == FALLOUT_NAM0_SIZE


def to_four_times(raw: bytes) -> bytes:
    """Re-stride a six-time FO3/FNV NAM0 blob to four times, slots unchanged.

    Keeps Sunrise, Day, Sunset and Night; drops High Noon and Midnight, which
    TES5 has no slot for. Any other blob is returned unchanged.
    """
    if not is_fallout_nam0(raw):
        return raw
    out = bytearray(NAM0_SLOTS * TES4_TIMES * _RGBA)
    for slot in range(NAM0_SLOTS):
        for time in range(TES4_TIMES):
            src = (slot * FALLOUT_TIMES + time) * _RGBA
            dst = (slot * TES4_TIMES + time) * _RGBA
            out[dst:dst + _RGBA] = raw[src:src + _RGBA]
    return bytes(out)
