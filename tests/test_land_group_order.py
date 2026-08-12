"""LAND must be the FIRST record in a cell's temporary children group.

Census of real Skyrim.esm: 15,564 of 15,564 type-9 groups that contain a LAND
have it at index 0 -- no exceptions. Emitting it after the references instead
means the engine does not draw the terrain at all: Tamriel cell (-7,-32) had
its LAND at index 150 behind 150 REFRs and rendered as a hole in the world
with its clutter still floating in place, while cells that happened to have no
references got LAND at index 0 by accident and rendered fine. That is why the
defect looked like "a few isolated cells" rather than a systematic fault.
"""

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tes5_import.overrides import emit_nested_overrides


def _rec(sig, fid, payload=b''):
    return (sig + struct.pack('<II', len(payload), 0)
            + struct.pack('<I', fid) + b'\x00' * 8 + payload)


class _Writer:
    """Captures the raw group bytes emit_nested_overrides writes."""

    def __init__(self):
        self.groups = []

    def add_raw_group(self, label, body):
        self.groups.append((label, body))


def _order_in_group(body):
    """Signatures of the records inside the innermost type-9 group."""
    out, i = [], 0
    while i + 24 <= len(body):
        sig = body[i:i+4]
        size = struct.unpack_from('<I', body, i+4)[0]
        if sig == b'GRUP':
            gtype = struct.unpack_from('<i', body, i+12)[0]
            inner = _order_in_group(body[i+24:i+size])
            if gtype == 9:
                return inner
            out.extend(inner)
            i += size
            continue
        out.append(sig)
        i += 24 + size
    return out


class TestLandIsFirstInTemporaryGroup:

    CELL = 0x0100DAEA

    def _emit(self, records):
        writer = _Writer()
        path = ((0, b'WRLD'), (1, b'\x3c\x00\x00\x01'), (9,
                struct.pack('<I', self.CELL)))
        emit_nested_overrides(
            [(fid, blob, path) for fid, blob in records], writer, None)
        assert writer.groups, 'nothing was emitted'
        return _order_in_group(writer.groups[0][1])

    def test_land_moves_to_the_front(self):
        recs = [(1, _rec(b'REFR', 1)), (2, _rec(b'REFR', 2)),
                (3, _rec(b'LAND', 3))]
        assert _order_in_group.__name__  # sanity
        order = self._emit(recs)
        assert order[0] == b'LAND', f'LAND must be first, got {order}'

    def test_land_already_first_is_untouched(self):
        recs = [(3, _rec(b'LAND', 3)), (1, _rec(b'REFR', 1))]
        order = self._emit(recs)
        assert order[0] == b'LAND'
        assert order.count(b'LAND') == 1, 'LAND must not be duplicated'

    def test_other_records_keep_their_relative_order(self):
        """The sort is stable: only the LAND moves."""
        recs = [(1, _rec(b'REFR', 1)), (2, _rec(b'ACHR', 2)),
                (3, _rec(b'LAND', 3)), (4, _rec(b'REFR', 4))]
        order = self._emit(recs)
        assert order == [b'LAND', b'REFR', b'ACHR', b'REFR'], order

    def test_group_without_land_is_unchanged(self):
        recs = [(1, _rec(b'REFR', 1)), (2, _rec(b'ACHR', 2))]
        order = self._emit(recs)
        assert order == [b'REFR', b'ACHR'], order


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
