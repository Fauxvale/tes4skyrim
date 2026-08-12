"""DXT1 terrain-texture encoder: identical output, bounded memory.

`_encode_dxt1_quality` builds an (N,16,4,3) difference array to pick each
pixel's nearest palette entry. Whole-array, that is ~148 MB peak for a 1024²
tile — and `sum` promotes int32 to int64, so half of it is pure waste. One
worker survives it; 29 do not. The one-bake LOD model made every worker's
baseline much larger (a 110,674-cell worldspace instead of one plugin's share),
and every level-16 tile then died on

    numpy._core._exceptions._ArrayMemoryError: Unable to allocate 32.0 MiB
    for an array with shape (65536, 16, 4) and data type int64

Chunking bounds the peak per worker regardless of tile size. The encoder is a
pixel-exact format, so the output must not move by a single byte.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asset_convert import terrain_lod as T


def _reference(img):
    """The pre-chunking implementation, kept as the correctness oracle."""
    h, w = img.shape[:2]
    ph, pw = (h + 3) & ~3, (w + 3) & ~3
    padded = np.zeros((ph, pw, 3), dtype=np.uint8)
    padded[:h, :w] = img

    blocks = T._blocks_4x4(padded).astype(np.int32)
    c0 = T._rgb_to_565_vec(blocks.max(axis=1))
    c1 = T._rgb_to_565_vec(blocks.min(axis=1))
    swap = c0 < c1
    c0, c1 = np.where(swap, c1, c0), np.where(swap, c0, c1)
    eq = c0 == c1
    c1 = np.where(eq & (c0 != 0), c0 - 1, c1)
    c0 = np.where(eq & (c0 == 0), 1, c0)
    p0, p1 = T._565_to_rgb_vec(c0), T._565_to_rgb_vec(c1)
    palette = np.stack([p0, p1, (2 * p0 + p1) // 3, (p0 + 2 * p1) // 3], axis=1)

    diffs = blocks[:, :, None, :] - palette[:, None, :, :]
    codes = (diffs * diffs).sum(axis=3).argmin(axis=2)

    shifts = np.arange(16, dtype=np.uint32) * 2
    packed = (codes.astype(np.uint32) << shifts).sum(axis=1, dtype=np.uint32)
    out = np.empty(len(blocks),
                   dtype=np.dtype([('c0', '<u2'), ('c1', '<u2'), ('p', '<u4')]))
    out['c0'], out['c1'], out['p'] = c0, c1, packed
    return out.tobytes()


def _img(shape, seed=1234):
    return np.random.default_rng(seed).integers(0, 256, shape, dtype=np.uint8)


@pytest.mark.parametrize("name,img", [
    ("random small",     _img((64, 64, 3))),
    ("random mid",       _img((256, 256, 3))),
    # 512² = 16,384 blocks, so it crosses the 4,096-block chunk boundary four
    # times — the case a naive chunking would get wrong at the seams.
    ("crosses chunks",   _img((512, 512, 3))),
    ("flat grey",        np.full((64, 64, 3), 128, np.uint8)),
    ("black",            np.zeros((64, 64, 3), np.uint8)),
    ("white",            np.full((64, 64, 3), 255, np.uint8)),
    # Not a multiple of 4 in either axis: exercises the zero padding.
    ("ragged",           _img((37, 53, 3))),
    ("gradient",         np.tile(np.arange(256, dtype=np.uint8)[None, :, None],
                                 (256, 1, 3))),
])
def test_output_is_byte_identical(name, img):
    assert T._encode_dxt1_quality(img) == _reference(img), (
        f"{name}: chunking changed the encoded bytes")


def test_encodes_exactly_8_bytes_per_4x4_block():
    """DXT1 is 8 bytes per block; a short buffer is a corrupt .dds."""
    img = _img((64, 64, 3))
    assert len(T._encode_dxt1_quality(img)) == (64 // 4) * (64 // 4) * 8


def test_peak_memory_stays_bounded():
    """The actual regression: peak must not scale with the whole tile.

    Threshold is deliberately loose — this guards against reintroducing the
    whole-array form (~148 MB here), not against small allocator differences.
    """
    import tracemalloc
    img = _img((1024, 1024, 3))
    tracemalloc.start()
    try:
        T._encode_dxt1_quality(img)
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    mb = peak / 1024 / 1024
    assert mb < 80, (
        f"peak {mb:.1f} MB — the whole-array form is back; with one worker per "
        f"core this exhausts RAM and every level-16 tile fails")


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
