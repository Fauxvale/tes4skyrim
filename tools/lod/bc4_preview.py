r"""Render a BC4 height map (`*_p.dds`) as a viewable greyscale PNG.

The shipped height maps are single-channel BC4, which no image viewer on a
phone opens and which nothing in the tree could show before.  A BC4 block is
byte-for-byte a DXT5 ALPHA block -- two 8-bit endpoints plus sixteen 3-bit
indices -- so decoding needs no external tool, exactly as `encode_bc4_dds`
needs none to write them.

Usage:
  python tools/lod/bc4_preview.py <file_p.dds> [-o OUT.png] [--max N] [--stats]

`--max` caps the long edge (default 1024) so the result is small enough to
send; `--stats` prints the height field's range, median and flat share, the
numbers the tone curve is calibrated against.
"""
import argparse
import os
import struct
import sys


def decode_bc4(blob):
    """Top mip of a BC4 DDS -> (w, h, bytearray), one byte per texel."""
    if blob[:4] != b'DDS ':
        raise ValueError('not a DDS')
    h = struct.unpack_from('<I', blob, 12)[0]
    w = struct.unpack_from('<I', blob, 16)[0]
    payload = 148 if blob[84:88] == b'DX10' else 128
    out = bytearray(w * h)
    pos = payload
    for by in range((h + 3) // 4):
        for bx in range((w + 3) // 4):
            a0, a1 = blob[pos], blob[pos + 1]
            if a0 > a1:
                pal = (a0, a1,
                       (6 * a0 + a1) // 7, (5 * a0 + 2 * a1) // 7,
                       (4 * a0 + 3 * a1) // 7, (3 * a0 + 4 * a1) // 7,
                       (2 * a0 + 5 * a1) // 7, (a0 + 6 * a1) // 7)
            else:
                pal = (a0, a1,
                       (4 * a0 + a1) // 5, (3 * a0 + 2 * a1) // 5,
                       (2 * a0 + 3 * a1) // 5, (a0 + 4 * a1) // 5, 0, 255)
            bits = int.from_bytes(blob[pos + 2:pos + 8], 'little')
            for ty in range(4):
                y = by * 4 + ty
                if y >= h:
                    break
                row = y * w
                for tx in range(4):
                    x = bx * 4 + tx
                    if x < w:
                        out[row + x] = pal[(bits >> ((ty * 4 + tx) * 3)) & 7]
            pos += 8
    return w, h, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dds')
    ap.add_argument('-o', '--out')
    ap.add_argument('--max', type=int, default=1024)
    ap.add_argument('--stats', action='store_true')
    args = ap.parse_args()

    with open(args.dds, 'rb') as f:
        blob = f.read()
    w, h, plane = decode_bc4(blob)

    if args.stats:
        lo, hi = min(plane), max(plane)
        srt = sorted(plane)
        med = srt[len(srt) // 2]
        band = sum(1 for v in plane if abs(v - med) <= 20) * 100.0 / len(plane)
        print(f'{w}x{h}  range {lo}..{hi} (amplitude {hi - lo})  '
              f'median {med}  within +/-20 of median: {band:.1f}%')

    from PIL import Image
    img = Image.frombytes('L', (w, h), bytes(plane))
    if max(w, h) > args.max:
        s = args.max / max(w, h)
        img = img.resize((max(1, int(w * s)), max(1, int(h * s))),
                         Image.LANCZOS)
    out = args.out or (os.path.splitext(args.dds)[0] + '.png')
    img.save(out)
    print(f'wrote {out}  ({img.width}x{img.height})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
