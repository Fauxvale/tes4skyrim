"""Report GRUP records whose owning record does not immediately precede them.

The engine binds a type-1 (world children), type-6 (cell children) or type-7
(topic children) GRUP to a record ONLY by physical adjacency: xEdit's
TwbGroupRecord.InformPrevMainRecord (wbImplementation.pas ~18023) attaches the
group to the previous record iff

    grsGroupType in [1, 6, 7] and aPrevMainRecord.FixedFormID = GroupLabel

A group of one of those types that is not preceded by its owner is attached to
nothing, so every record inside it is unreachable by the engine — as invisible
as if it had never been written. This is a silent failure: the file loads, the
records are present in xEdit, and nothing appears in-game.

Usage:
    python tools/esm_group_anchors.py output/<Plugin>/<Plugin>
    python tools/esm_group_anchors.py <plugin> --verbose
"""

import argparse
import struct
import sys

_HEADER_SIZE = 24
_OWNED = {1: 'world children', 6: 'cell children', 7: 'topic children'}


def scan(path):
    """Returns (anchored, orphans) where orphans is [(gtype, owner_fid), ...]."""
    with open(path, 'rb') as f:
        data = f.read()
    if data[:4] != b'TES4':
        raise SystemExit(f"Not a plugin file: {path}")

    anchored, orphans = [], []

    def walk(off, end):
        prev = None
        while off + _HEADER_SIZE <= end:
            sig = data[off:off + 4]
            size = struct.unpack_from('<I', data, off + 4)[0]
            if sig == b'GRUP':
                label = data[off + 8:off + 12]
                gtype = struct.unpack_from('<i', data, off + 12)[0]
                if gtype in _OWNED and len(label) == 4:
                    owner = struct.unpack('<I', label)[0]
                    entry = (gtype, owner)
                    (anchored if prev == owner else orphans).append(entry)
                walk(off + _HEADER_SIZE, off + size)
                off += size
                prev = None       # a group breaks the record/group adjacency
            else:
                prev = struct.unpack_from('<I', data, off + 12)[0]
                off += _HEADER_SIZE + size

    walk(_HEADER_SIZE + struct.unpack_from('<I', data, 4)[0], len(data))
    return anchored, orphans


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('plugin')
    ap.add_argument('--verbose', action='store_true',
                    help='list every orphaned group, not just a summary')
    args = ap.parse_args()

    anchored, orphans = scan(args.plugin)
    print(f"{args.plugin}")
    print(f"  owned groups correctly anchored: {len(anchored)}")
    print(f"  ORPHANED (unreachable in-engine): {len(orphans)}")
    if orphans:
        by_type = {}
        for gtype, owner in orphans:
            by_type.setdefault(gtype, []).append(owner)
        for gtype in sorted(by_type):
            fids = by_type[gtype]
            print(f"    type {gtype} ({_OWNED[gtype]}): {len(fids)}")
            shown = fids if args.verbose else fids[:10]
            for fid in shown:
                print(f"      owner {fid:08X} has no preceding record")
            if not args.verbose and len(fids) > len(shown):
                print(f"      ... and {len(fids) - len(shown)} more "
                      f"(use --verbose)")
    return 1 if orphans else 0


if __name__ == '__main__':
    sys.exit(main())
