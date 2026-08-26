#!/usr/bin/env python3
r"""Placed references whose DATA rotation/position cannot be normalized.

WHY THIS EXISTS -- a HARD HANG with no crash and no log.

The engine normalizes a reference's angles into [0, 2*pi) with a bare loop
(SkyrimSE 1.6.1170 +0x2d8e43..+0x2d8e6f, recovered by attaching to a live
frozen process):

    while (a <  0    ) a += 2*pi;      # +0x2d8e50
    while (a >  2*pi ) a -= 2*pi;      # +0x2d8e66

There is no iteration cap and no range check. Both loops are FLOAT32. Once the
angle is large enough that `a - 2*pi == a` under float32 rounding (any |a| over
~2^24 * 2*pi, and instantly for 1e22-scale values), the subtraction changes
nothing and the loop spins forever at 100% CPU on one core with memory flat --
the exact signature of the Valenwood freeze: game unresponsive, no CTD, no
crash log, CrashLogger silent because nothing ever faults.

NaN is just as fatal: every comparison against NaN is false, so `jb`/`ja`
fall through unpredictably and the value is stored unnormalized, poisoning
whatever consumes it downstream.

So a single bad rotation on a single placed reference freezes the game for
anyone who walks into that cell. This checks every REFR/ACHR/ACRE DATA
subrecord (6 floats: X Y Z, RX RY RZ) for values the engine cannot handle.

Usage:
    python tools/validate/refr_rotation_check.py --plugin TWMP_Valenwood_Elsweyr.esp
    python tools/validate/refr_rotation_check.py --plugin X.esp --max 40
"""

import argparse
import math
import os
import struct
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TWO_PI = 2.0 * math.pi
# Above this an f32 `a -= 2*pi` stops making progress (2*pi is below the ULP),
# so the normalization loop cannot terminate. 2**24 steps is already ~1e8
# iterations, far past any sane authored angle.
ANGLE_STALL = (2 ** 24) * TWO_PI
# Skyrim worldspaces span roughly +/-2^22 units; beyond this a position is junk.
POS_LIMIT = 1.0e9


def _f32_stalls(a):
    """True if `a -= 2*pi` (or `+=`) cannot move `a` in float32."""
    if a != a or a in (float('inf'), float('-inf')):
        return True
    step = struct.unpack('<f', struct.pack('<f', TWO_PI))[0]
    cur = struct.unpack('<f', struct.pack('<f', a))[0]
    if cur > step:
        return struct.unpack('<f', struct.pack('<f', cur - step))[0] == cur
    if cur < 0.0:
        return struct.unpack('<f', struct.pack('<f', cur + step))[0] == cur
    return False


def _subs(body):
    out, q = {}, 0
    while q + 6 <= len(body):
        sig = body[q:q + 4]
        size = struct.unpack_from('<H', body, q + 4)[0]
        q += 6
        out.setdefault(sig, body[q:q + size])
        q += size
    return out


def scan(path):
    data = open(path, 'rb').read()
    bad = []
    total = 0
    cellpos = {}
    pending = []

    def walk(off, end, stack):
        nonlocal total
        p = off
        while p + 24 <= end:
            sig = data[p:p + 4]
            if sig == b'GRUP':
                gsize, label, gtype = struct.unpack_from('<IiI', data, p + 4)
                walk(p + 24, p + gsize, stack + [(gtype, label)])
                p += gsize
                continue
            size, flags, fid = struct.unpack_from('<III', data, p + 4)
            body = data[p + 24:p + 24 + size]
            if flags & 0x00040000:
                try:
                    body = zlib.decompress(body[4:])
                except zlib.error:
                    body = b''
            if sig == b'CELL':
                xclc = _subs(body).get(b'XCLC')
                if xclc and len(xclc) >= 8:
                    cellpos[fid] = struct.unpack_from('<ii', xclc, 0)
            elif sig in (b'REFR', b'ACHR', b'ACRE'):
                d = _subs(body).get(b'DATA')
                if d and len(d) >= 24:
                    total += 1
                    x, y, z, rx, ry, rz = struct.unpack_from('<6f', d, 0)
                    why = []
                    for nm, a in (('RX', rx), ('RY', ry), ('RZ', rz)):
                        if a != a:
                            why.append('%s=NaN' % nm)
                        elif a in (float('inf'), float('-inf')):
                            why.append('%s=Inf' % nm)
                        elif abs(a) > ANGLE_STALL:
                            why.append('%s=%g (normalize loop cannot '
                                       'terminate)' % (nm, a))
                    for nm, v in (('X', x), ('Y', y), ('Z', z)):
                        if v != v or abs(v) > POS_LIMIT:
                            why.append('%s=%g' % (nm, v))
                    if why:
                        cell = next((l for t, l in reversed(stack) if t == 6),
                                    None)
                        pending.append((fid, sig.decode('latin-1'), cell,
                                        (x, y, z), why))
            p += 24 + size

    walk(0, len(data), [])
    for fid, sig, cell, pos, why in pending:
        bad.append((fid, sig, cellpos.get(cell), pos, why))
    return total, bad


def main():
    ap = argparse.ArgumentParser(
        description='Find placed refs the engine cannot normalize (hard hang)')
    ap.add_argument('--plugin', required=True)
    ap.add_argument('--output-dir', default=os.path.join(ROOT, 'output'))
    ap.add_argument('--max', type=int, default=25)
    args = ap.parse_args()

    path = os.path.join(args.output_dir, args.plugin, args.plugin)
    if not os.path.isfile(path):
        print('ERROR: no converted plugin at %s' % path)
        return 1
    total, bad = scan(path)
    print('plugin: %s' % args.plugin)
    print('placed refs with DATA: %d' % total)
    print('UNNORMALIZABLE (engine hangs on these): %d' % len(bad))
    for fid, sig, cell, pos, why in bad[:args.max]:
        print('  %s %08X  cell %s  pos (%.1f, %.1f, %.1f)'
              % (sig, fid, cell, pos[0], pos[1], pos[2]))
        for w in why:
            print('      %s' % w)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
