"""Find FormID references that resolve to NOTHING in a plugin's load order.

The engine builds its FormID table while PARSING a plugin, before any cell
loads. A reference naming a record that no loaded file defines is a classic
cause of a main-menu hang with no crash and no log: xEdit reports the file as
clean because a dangling reference is legal on disk, but the engine never
finishes linking it.

Every FormID-valued subrecord is checked against:

  * the records the plugin itself defines
  * each converted master's own records, routed BY INDEX BYTE (two masters'
    id spaces overlap almost completely, so a first-match scan lies)

References into a master we cannot read (Skyrim.esm and other vanilla files
are not in output/) are NOT judged -- they are reported separately as
unchecked so an absent master never invents thousands of fake findings.

Usage:
  python tools/dangling_ref_check.py TWMP_ValenwoodImproved.esp
  python tools/dangling_ref_check.py --max 40 Plugin.esp
"""
import argparse
import os
import struct
import sys
import zlib
from collections import Counter

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

_HEADER = 24

# Subrecords whose payload is exactly one FormID, per xEdit's definitions.
# Kept deliberately narrow: a mis-typed entry invents dangling references that
# do not exist, which is far worse than missing a real one.
FORMID_SUBS = {
    b'NAME', b'XEZN', b'XOWN', b'XGLB', b'XESP', b'XLCN', b'XLRT',
    b'XCWT', b'XCIM', b'XCMO', b'XLCM', b'XCCM', b'INAM', b'PNAM',
    b'ZNAM', b'CNAM', b'QNAM', b'RNAM', b'SNAM', b'VNAM', b'WNAM',
    b'YNAM', b'KNAM', b'LNAM', b'GNAM', b'HNAM', b'JNAM', b'TNAM',
    b'EITM', b'ETYP', b'SCRI', b'VTCK', b'TPLT', b'RCLR', b'ATKR',
}

# (record signature, subrecord) pairs from the set above whose payload is NOT
# a FormID on that record type. Anything not listed here is treated as a real
# reference, so this table is the accuracy-critical half of the tool.
NOT_FORMID = {
    (b'WRLD', b'DNAM'), (b'WRLD', b'MNAM'), (b'WRLD', b'ONAM'),
    (b'WRLD', b'PNAM'), (b'WRLD', b'TNAM'),
    (b'CELL', b'XCLC'),
    (b'LAND', b'DATA'),
    (b'CLMT', b'TNAM'), (b'CLMT', b'FNAM'), (b'CLMT', b'GNAM'),
    (b'WTHR', b'FNAM'), (b'WTHR', b'CNAM'), (b'WTHR', b'NNAM'),
    (b'NPC_', b'ANAM'), (b'RACE', b'ANAM'), (b'SOUN', b'FNAM'),
    (b'QUST', b'ANAM'), (b'INFO', b'ANAM'), (b'DIAL', b'TNAM'),
    (b'BOOK', b'CNAM'), (b'IDLE', b'ANAM'),
    (b'PACK', b'ANAM'), (b'PACK', b'PNAM'), (b'PACK', b'TNAM'),
    (b'PACK', b'UNAM'), (b'PACK', b'XNAM'),
    (b'FURN', b'MNAM'), (b'FURN', b'WNAM'), (b'FURN', b'ONAM'),
    (b'FURN', b'ANAM'),
    (b'SNDR', b'CNAM'), (b'SNDR', b'ANAM'),
    (b'SOPM', b'ANAM'), (b'SOPM', b'ONAM'), (b'SOPM', b'NAM1'),
    (b'REGN', b'RNAM'), (b'REGN', b'ICON'),
    (b'IMGS', b'TNAM'), (b'IMGS', b'CNAM'), (b'IMGS', b'ANAM'),
    (b'IMGS', b'HNAM'),
    (b'PROJ', b'VNAM'), (b'MOVT', b'INAM'),
    (b'LCTN', b'RNAM'), (b'LCTN', b'MNAM'), (b'LCTN', b'ANAM'),
    (b'FLOR', b'PNAM'), (b'ACTI', b'FNAM'), (b'VTYP', b'DNAM'),
    (b'BPTD', b'INAM'), (b'BPTD', b'NAM1'),
    (b'DOOR', b'FNAM'), (b'DOOR', b'SNAM'), (b'DOOR', b'ANAM'),
    (b'DOOR', b'BNAM'),
    (b'TREE', b'CNAM'), (b'DLBR', b'SNAM'), (b'DLVW', b'TNAM'),
    (b'MGEF', b'PNAM'), (b'ARMA', b'NAM0'), (b'ARMA', b'NAM1'),
}


def read_masters(data):
    hdr = struct.unpack_from('<I', data, 4)[0]
    o, end, out = _HEADER, _HEADER + hdr, []
    while o + 6 <= end:
        st = data[o:o + 4]
        ss = struct.unpack_from('<H', data, o + 4)[0]
        if st == b'MAST':
            out.append(data[o + 6:o + 6 + ss]
                       .rstrip(b'\0').decode('ascii', 'replace'))
        o += 6 + ss
    return out


def own_formids(data):
    """FormIDs this file's records carry."""
    hdr = struct.unpack_from('<I', data, 4)[0]
    ids = set()

    def walk(o, e):
        while o < e:
            sig = data[o:o + 4]
            size = struct.unpack_from('<I', data, o + 4)[0]
            if sig == b'GRUP':
                walk(o + _HEADER, o + size)
                o += size
            else:
                ids.add(struct.unpack_from('<I', data, o + 12)[0])
                o += _HEADER + size

    walk(_HEADER + hdr, len(data))
    return ids


def iter_refs(data):
    """(record_sig, record_fid, sub_sig, referenced_fid) across the file."""
    hdr = struct.unpack_from('<I', data, 4)[0]

    def subs(sig, body, flags):
        if flags & 0x00040000:
            try:
                body = zlib.decompress(body[4:])
            except zlib.error:
                return
        o = 0
        while o + 6 <= len(body):
            st = body[o:o + 4]
            ss = struct.unpack_from('<H', body, o + 4)[0]
            if (st in FORMID_SUBS and ss == 4
                    and (sig, st) not in NOT_FORMID):
                yield st, struct.unpack_from('<I', body, o + 6)[0]
            o += 6 + ss

    def walk(o, e):
        while o < e:
            sig = data[o:o + 4]
            size = struct.unpack_from('<I', data, o + 4)[0]
            if sig == b'GRUP':
                yield from walk(o + _HEADER, o + size)
                o += size
            else:
                flags = struct.unpack_from('<I', data, o + 8)[0]
                fid = struct.unpack_from('<I', data, o + 12)[0]
                body = data[o + _HEADER:o + _HEADER + size]
                for st, ref in subs(sig, body, flags):
                    yield sig, fid, st, ref
                o += _HEADER + size

    yield from walk(_HEADER + hdr, len(data))


def audit(name, output_dir, max_list):
    path = os.path.join(output_dir, name, name)
    if not os.path.isfile(path):
        print(f"--- {name}: NOT BUILT ({path})")
        return 0
    with open(path, 'rb') as f:
        data = f.read()
    masters = read_masters(data)
    own_index = len(masters)

    # slot -> ids that slot's file defines, restated in THIS plugin's space.
    by_slot = {}
    for slot, m in enumerate(masters):
        mp = os.path.join(output_dir, m, m)
        if not os.path.isfile(mp):
            by_slot[slot] = None            # unreadable: never judged
            continue
        with open(mp, 'rb') as f:
            mdata = f.read()
        m_own = len(read_masters(mdata))
        by_slot[slot] = {(slot << 24) | (i & 0xFFFFFF)
                         for i in own_formids(mdata) if (i >> 24) == m_own}

    mine = own_formids(data)
    dangling = Counter()
    checked = unchecked = 0

    for sig, _fid, st, ref in iter_refs(data):
        if not ref:
            continue
        slot = ref >> 24
        if slot == own_index:
            checked += 1
            if ref not in mine:
                dangling[(sig.decode(), st.decode(), f'{ref:08X}',
                          'this plugin')] += 1
            continue
        known = by_slot.get(slot)
        if known is None:
            unchecked += 1
            continue
        checked += 1
        if ref not in known:
            where = masters[slot] if slot < len(masters) else f'slot {slot}'
            dangling[(sig.decode(), st.decode(), f'{ref:08X}', where)] += 1

    n = sum(dangling.values())
    status = f"{n} DANGLING" if n else "CLEAN"
    print(f"--- {name}: {checked} references checked "
          f"({unchecked} into unreadable masters, not judged) -> {status}")
    for (rsig, ssig, target, where), count in dangling.most_common(max_list):
        print(f"    {rsig}.{ssig} -> {target} (expected in {where}) x{count}")
    return n


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('plugin', nargs='+')
    ap.add_argument('--output-dir', default=os.path.join(SCRIPT_DIR, 'output'))
    ap.add_argument('--max', type=int, default=25,
                    help='most-common dangling targets to list (default 25)')
    args = ap.parse_args()

    total = 0
    for name in args.plugin:
        total += audit(name, args.output_dir, args.max)
    print(f"\nTOTAL DANGLING: {total}")
    return total


if __name__ == '__main__':
    sys.exit(0 if main() == 0 else 1)
