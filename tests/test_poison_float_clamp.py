"""Poisoned placement floats must never reach the LODGen input.

Real plugins ship NaN and +/-FLT_MAX in REFR DATA rotations -- TWMP
Valenwood/Elsweyr has 505 of them. LODGen's C# parser rejects any line
carrying one and then bakes NO .bto tiles for the whole worldspace, so a
single bad reference costs every object LOD in it.

The two poisons fail through DIFFERENT parser errors, and FLT_MAX is finite,
so an isfinite() check alone is not enough. Both clamps are covered here:
get_float() on the import side and _finite() on the LODGen side (a MASTER's
record reaches the LODGen parser without passing through get_float).
"""

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asset_convert.lod_gen import _finite
from tes5_import.text_reader import get_float

# The exact bit patterns found in the shipped plugin.
NAN_BITS = 0x7FC00000
NEG_FLT_MAX_BITS = 0xFF7FFFFF
POS_FLT_MAX_BITS = 0x7F7FFFFF
INF_BITS = 0x7F800000


def _f(bits):
    return struct.unpack('<f', struct.pack('<I', bits))[0]


class TestGetFloatClamp:
    """Import side: the export text is a pure dump, so poison arrives verbatim."""

    @pytest.mark.parametrize('text', [
        'nan',
        '-3.4028234663852886e+38',
        '3.4028234663852886e+38',
        'inf',
        '-inf',
    ])
    def test_poison_falls_back_to_default(self, text):
        assert get_float({'RotZ': text}, 'RotZ') == 0.0

    def test_poison_respects_explicit_default(self):
        assert get_float({'XSCL': 'nan'}, 'XSCL', 1.0) == 1.0

    @pytest.mark.parametrize('text, expected', [
        ('1.5708', 1.5708),
        ('0.0', 0.0),
        ('-227696.0', -227696.0),   # a real Tamriel-scale coordinate
        ('-309294.5312', -309294.5312),
    ])
    def test_real_values_pass_through(self, text, expected):
        assert get_float({'PosX': text}, 'PosX') == pytest.approx(expected)


class TestFiniteClamp:
    """LODGen side: reads master bytes directly, bypassing get_float."""

    @pytest.mark.parametrize('bits', [
        NAN_BITS, NEG_FLT_MAX_BITS, POS_FLT_MAX_BITS, INF_BITS,
    ])
    def test_poison_bits_clamped(self, bits):
        assert _finite(_f(bits)) == 0.0

    def test_scale_clamps_to_one(self):
        assert _finite(_f(NAN_BITS), 1.0) == 1.0
        assert _finite(_f(NEG_FLT_MAX_BITS), 1.0) == 1.0

    @pytest.mark.parametrize('bits', [
        0x40B99E75,   # 5.80059  -- a real rotation
        0xC85E5C00,   # -227696.0 -- a real coordinate
    ])
    def test_real_bits_pass_through(self, bits):
        v = _f(bits)
        assert _finite(v) == v


class TestFormattedOutput:
    """The written line is what LODGen actually parses -- assert on that."""

    @pytest.mark.parametrize('bits', [NAN_BITS, NEG_FLT_MAX_BITS])
    def test_clamped_value_formats_as_parseable_single(self, bits):
        rendered = f'{_finite(_f(bits)):.4f}'
        assert rendered == '0.0000'
        # Must round-trip through Single's range, which is what the C#
        # parser rejected the unclamped literal for.
        assert abs(float(rendered)) < 3.4028234663852886e+38

    def test_unclamped_flt_max_would_have_broken_the_parser(self):
        # Guards the reason the magnitude bound exists: this value is finite,
        # so isfinite() alone lets it through, and "%.4f" then expands it to a
        # 40-digit literal outside Single's range.
        raw = _f(NEG_FLT_MAX_BITS)
        import math
        assert math.isfinite(raw)
        assert len(f'{raw:.4f}') > 40
