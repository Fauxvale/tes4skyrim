"""Slot 1's alpha decides whether a shape gets specular.

The rule is deliberately blunt, and that is the point: unlike a diffuse's
alpha -- which may be transparency OR height, and needs a calibrated
classifier to tell them apart -- slot 1's alpha has no competing meaning.
Only three things disqualify it: no alpha channel at all, a flat channel, and
a two-value channel.

The one that must never regress is `no_alpha`. Skyrim reads a missing alpha as
1.0, i.e. FULL specular over the whole surface, and 39.6% of Nehrim's normal
maps are DXT1.
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asset_convert import spec_mask                              # noqa: E402


def _hdr(w, h, fourcc):
    hdr = bytearray(128)
    hdr[0:4] = b'DDS '
    struct.pack_into('<I', hdr, 4, 124)
    struct.pack_into('<I', hdr, 8, 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000)
    struct.pack_into('<I', hdr, 12, h)
    struct.pack_into('<I', hdr, 16, w)
    struct.pack_into('<I', hdr, 28, 1)
    struct.pack_into('<I', hdr, 76, 32)
    struct.pack_into('<I', hdr, 80, 0x4)
    hdr[84:88] = fourcc
    return bytes(hdr)


def _dxt5(w, h, a0, a1, indices):
    bits = 0
    for i, idx in enumerate(indices):
        bits |= (idx & 0x7) << (i * 3)
    block = bytes((a0, a1)) + bits.to_bytes(6, 'little') + b'\x00' * 8
    n = ((w + 3) // 4) * ((h + 3) // 4)
    return _hdr(w, h, b'DXT5') + block * n


def _dxt5_varied(w, h, endpoints):
    n = ((w + 3) // 4) * ((h + 3) // 4)
    bits = 0
    for i in range(16):
        bits |= (i % 8) << (i * 3)
    out = bytearray(_hdr(w, h, b'DXT5'))
    for b in range(n):
        a0, a1 = endpoints[b % len(endpoints)]
        out += bytes((a0, a1)) + bits.to_bytes(6, 'little') + b'\x00' * 8
    return bytes(out)


def _dxt3(w, h, nibbles):
    alpha = bytearray()
    for i in range(0, 16, 2):
        alpha.append((nibbles[i] & 0xF) | ((nibbles[i + 1] & 0xF) << 4))
    block = bytes(alpha) + b'\x00' * 8
    n = ((w + 3) // 4) * ((h + 3) // 4)
    return _hdr(w, h, b'DXT3') + block * n


class TestSpecularMaskDetection:

    def test_a_real_mask_is_accepted(self):
        blob = _dxt5_varied(32, 32, [(200 - i, 50 + i) for i in range(64)])
        assert spec_mask.classify_bytes(blob) == 'mask'
        assert spec_mask.has_mask(blob)

    def test_dxt1_has_no_alpha_and_must_stay_off(self):
        # the dangerous one: Skyrim reads a missing alpha as 1.0 = full
        # specular over the entire surface
        blob = _hdr(8, 8, b'DXT1') + b'\x00' * (4 * 8)
        assert spec_mask.classify_bytes(blob) == 'no_alpha'

    def test_a_flat_channel_is_rejected(self):
        # DXT5 saved by a tool that never needed alpha and filled it with 255
        blob = _dxt5(8, 8, 255, 255, [0] * 16)
        assert spec_mask.classify_bytes(blob) in ('flat', 'binary')
        assert not spec_mask.has_mask(blob)

    def test_a_two_value_channel_is_rejected(self):
        blob = _dxt5(8, 8, 255, 0, [0, 1] * 8)
        assert spec_mask.classify_bytes(blob) == 'binary'

    def test_dxt3_is_accepted_though_it_is_coarse(self):
        # 25% of Nehrim's normal maps are DXT3.  Sixteen levels of HEIGHT is
        # visible terracing -- which is why parallax rejects them -- but
        # sixteen levels of specular MASK is perfectly ordinary.
        blob = _dxt3(8, 8, [0, 2, 4, 6, 8, 10, 12, 14,
                            15, 13, 11, 9, 7, 5, 3, 1])
        assert spec_mask.classify_bytes(blob) == 'mask'

    def test_garbage_is_not_a_mask(self):
        assert spec_mask.classify_bytes(b'not a dds') == 'no_alpha'
        assert spec_mask.classify_bytes(b'') == 'no_alpha'

    def test_the_height_classifier_is_not_reused_wholesale(self):
        # parallax.classify_alpha calls the DXT3 sample above 'quantised' and
        # refuses it; spec_mask must not inherit that verdict
        from asset_convert import parallax
        blob = _dxt3(8, 8, [0, 2, 4, 6, 8, 10, 12, 14,
                            15, 13, 11, 9, 7, 5, 3, 1])
        assert parallax.classify_alpha(blob).kind != 'height'
        assert spec_mask.classify_bytes(blob) == 'mask'
