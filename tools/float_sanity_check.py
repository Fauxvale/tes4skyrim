"""Find NaN / Infinity / absurd floats in a converted plugin.

A poisoned float is invisible to xEdit -- it is four legal bytes and every
size/offset still nests, so the file reports as clean -- but the engine folds
these values into arithmetic while it PARSES the file, before any cell loads:
worldspace extents, cell grid bounds and the object LOD/quadtree are all built
at load time from placement data. A NaN propagates through every comparison as
false, so a bounds loop never terminates and the game sits on the main menu
forever with no crash and no log.

Checked per record type, on the subrecords whose payload is float:

  * REFR/ACHR/ACRE  DATA  position XYZ + rotation XYZ
  * CELL            XCLW  water height
  * WRLD            NAM0/NAM9 object bounds, DNAM land/water level
  * LAND            VHGT  height offset

`--max-abs` bounds the "absurd" test (default 1e9): a coordinate that large is
not a real placement, and Skyrim's world is ~2e6 units across.

Usage:
  python tools/float_sanity_check.py TWMP_ValenwoodImproved.esp
  python tools/float_sanity_check.py --max 40 Plugin.esp Other.esp
"""
import argparse
import math
import os
import struct
import sys
import zlib
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
from output_layout import paths  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HEADER = 24

# (record sig, subrecord) -> (offset, count) of floats to test in the payload.
# Only fields the engine reads geometrically at load are listed; a wrong entry
# here invents findings, so each one is tied to a known binary layout.
FLOAT_FIELDS = {
    (b'REFR', b'DATA'): (0, 6),
    (b'ACHR', b'DATA'): (0, 6),
    (b'ACRE', b'DATA'): (0, 6),
    (b'PGRE', b'DATA'): (0, 6),
    (b'PHZD', b'DATA'): (0, 6),
    (b'PMIS', b'DATA'): (0, 6),
    (b'PARW', b'DATA'): (0, 6),
    (b'CELL', b'XCLW'): (0, 1),
    (b'WRLD', b'NAM0'): (0, 2),
    (b'WRLD', b'NAM9'): (0, 2),
    (b'LAND', b'VHGT'): (0, 1),
}


def _subrecords(body):
    o = 0
    while o + 6 <= len(body):
        st = body[o:o + 4]
        ss = struct.unpack_from('<H', body, o + 4)[0]
        yield st, body[o + 6:o + 6 + ss]
        o += 6 + ss


def audit(path, label, max_list, max_abs):
    with open(path, 'rb') as f:
        d = f.read()
    hdr = struct.unpack_from('<I', d, 4)[0]
    bad = Counter()
    examples = {}
    checked = 0

    def walk(o, e):
        nonlocal checked
        while o < e:
            sig = d[o:o + 4]
            size = struct.unpack_from('<I', d, o + 4)[0]
            if sig == b'GRUP':
                walk(o + _HEADER, o + size)
                o += size
                continue
            flags = struct.unpack_from('<I', d, o + 8)[0]
            fid = struct.unpack_from('<I', d, o + 12)[0]
            body = d[o + _HEADER:o + _HEADER + size]
            if flags & 0x00040000:
                try:
                    body = zlib.decompress(body[4:])
                except zlib.error:
                    body = b''
            for st, payload in _subrecords(body):
                spec = FLOAT_FIELDS.get((sig, st))
                if not spec:
                    continue
                off, count = spec
                if len(payload) < off + 4 * count:
                    continue
                vals = struct.unpack_from('<%df' % count, payload, off)
                for i, v in enumerate(vals):
                    checked += 1
                    if math.isnan(v):
                        kind = 'NaN'
                    elif math.isinf(v):
                        kind = 'Infinity'
                    elif abs(v) > max_abs:
                        kind = 'absurd'
                    else:
                        continue
                    key = (sig.decode(), st.decode(), i, kind)
                    bad[key] += 1
                    examples.setdefault(key, (fid, v))
            o += _HEADER + size

    walk(_HEADER + hdr, len(d))
    n = sum(bad.values())
    print(f"--- {label}: {checked:,} floats checked -> "
          + (f"{n} POISONED" if n else "CLEAN"))
    for (rsig, ssig, i, kind), count in bad.most_common(max_list):
        fid, v = examples[(rsig, ssig, i, kind)]
        print(f"    {rsig}.{ssig}[{i}] {kind} x{count}"
              f"  e.g. {fid:08X} = {v!r}")
    return n


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('plugin', nargs='+')
    ap.add_argument('--output-dir', default=os.path.join(SCRIPT_DIR, 'output'))
    ap.add_argument('--max', type=int, default=25)
    ap.add_argument('--max-abs', type=float, default=1e9,
                    help='magnitude above which a float is "absurd"')
    args = ap.parse_args()

    total = 0
    for name in args.plugin:
        path = str(paths(name, out_root=args.output_dir).esm)
        if not os.path.isfile(path):
            print(f"--- {name}: NOT BUILT ({path})")
            continue
        total += audit(path, name, args.max, args.max_abs)
    print(f"\nTOTAL POISONED FLOATS: {total}")
    return total


if __name__ == '__main__':
    sys.exit(0 if main() == 0 else 1)
