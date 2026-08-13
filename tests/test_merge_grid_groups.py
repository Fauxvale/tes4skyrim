"""Folding two world-children groups must leave ONE ascending block run.

The override pass and the WRLD builder each write a `GRUP World Children`
for the same worldspace, and each sorts its own exterior blocks. Merging the
two by concatenating their bodies therefore produces one group holding TWO
ascending runs, plus a duplicate type-4 group for every block both passes
touched.

The engine walks that block list to build the worldspace's cell grid while
PARSING the file, so a descent mid-list hangs the game on the main menu with
no crash and no log -- and xEdit still reports the file as clean. Measured
before the fix: Tamriel.esp shipped 121 block groups in 15 ascending runs with
14 duplicate labels; ElsweyrAnequina.esp 8 blocks in 2 runs with 3 duplicates.
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tes5_import.writer import (GROUP_HEADER_SIZE, _merge_owned_groups,
                                pack_group, pack_record)


def _label(x, y):
    """An exterior block GRUP label: Y in the low word, then X."""
    return struct.pack('<hh', y, x)


def _cell(fid):
    return pack_record('CELL', fid, 0, b'')


def _block(x, y, *cells):
    return pack_group(4, _label(x, y), b''.join(cells))


def _world_children(wrld_fid, body):
    return pack_group(1, struct.pack('<I', wrld_fid), body)


def _walk(blob):
    """Top-level (signature, group-type, label) triples of a group body."""
    out, off = [], 0
    while off + GROUP_HEADER_SIZE <= len(blob):
        sig = blob[off:off + 4]
        raw = struct.unpack_from('<I', blob, off + 4)[0]
        if sig != b'GRUP':
            out.append(('REC', None, struct.unpack_from('<I', blob, off + 12)[0]))
            off += GROUP_HEADER_SIZE + raw
            continue
        gtype = struct.unpack_from('<i', blob, off + 12)[0]
        out.append(('GRUP', gtype, bytes(blob[off + 8:off + 12])))
        off += raw
    return out


def _blocks_of(world_children_group):
    body = world_children_group[GROUP_HEADER_SIZE:]
    return [struct.unpack('<hh', lbl)[::-1]
            for kind, gt, lbl in _walk(body) if kind == 'GRUP' and gt == 4]


def _sizes_consistent(blob):
    """Every GRUP's declared size must span exactly its contents."""
    off = 0
    while off + GROUP_HEADER_SIZE <= len(blob):
        sig = blob[off:off + 4]
        raw = struct.unpack_from('<I', blob, off + 4)[0]
        if sig != b'GRUP':
            off += GROUP_HEADER_SIZE + raw
            continue
        if off + raw > len(blob):
            return False
        if not _sizes_consistent(blob[off + GROUP_HEADER_SIZE:off + raw]):
            return False
        off += raw
    return off == len(blob)


def test_two_runs_become_one_ascending_run():
    """The seam between the two passes must not leave X descending."""
    first = _world_children(0x0100003C, _block(0, -1) + _block(-1, -1))
    second = _world_children(0x0100003C, _block(1, 0) + _block(-2, 0))

    out = _merge_owned_groups(first + second)

    groups = [g for g in _walk(out) if g[0] == 'GRUP' and g[1] == 1]
    assert len(groups) == 1, "the two world-children groups must fold into one"

    blocks = _blocks_of(out)
    keys = [(x & 0xFFFF, y & 0xFFFF) for x, y in blocks]
    assert keys == sorted(keys), f"blocks must ascend by unsigned (X, Y): {blocks}"
    # Unsigned X major: 0 < 1 < -2 (65534) < -1 (65535).
    assert blocks == [(0, -1), (1, 0), (-2, 0), (-1, -1)]


def test_duplicate_blocks_are_folded_and_keep_their_cells():
    """A block both passes wrote becomes ONE group holding all its cells."""
    first = _world_children(0x0100003C, _block(-1, -1, _cell(0xAA)))
    second = _world_children(0x0100003C, _block(-1, -1, _cell(0xBB)))

    out = _merge_owned_groups(first + second)

    assert _blocks_of(out) == [(-1, -1)], "the duplicate block must collapse"
    # Both passes' cells survive inside the single surviving block.
    body = out[GROUP_HEADER_SIZE:]
    block_body = body[GROUP_HEADER_SIZE:]
    assert [f for k, _t, f in _walk(block_body) if k == 'REC'] == [0xAA, 0xBB]


def test_group_sizes_stay_consistent_after_merging():
    """A merged group's header size must still span exactly its contents."""
    first = _world_children(0x0100003C, _block(0, 0, _cell(1)) + _block(-1, 0))
    second = _world_children(0x0100003C, _block(0, 0, _cell(2)) + _block(2, 0))

    out = _merge_owned_groups(first + second)

    assert _sizes_consistent(out), "GRUP sizes must be re-stamped after merging"


def test_untouched_group_is_moved_verbatim():
    """With no duplicate to fold, the bytes must pass through unchanged.

    Re-sorting a group that was never merged would be pointless work on every
    plugin, and would risk reordering a hierarchy no pass duplicated.
    """
    only = _world_children(0x0100003C, _block(-1, -1) + _block(0, 0))
    assert _merge_owned_groups(only) == only


def test_persistent_cell_group_keeps_its_place_at_the_front():
    """Only the grid groups are sorted; the type-6 group must stay first.

    Vanilla opens a worldspace's children with the persistent CELL and its
    type-6 group, then the exterior blocks.
    """
    pers = pack_group(6, struct.pack('<I', 0x01023777), _cell(0x01023777))
    first = _world_children(0x0100003C, pers + _block(-1, -1))
    second = _world_children(0x0100003C, _block(0, 0))

    out = _merge_owned_groups(first + second)

    body = out[GROUP_HEADER_SIZE:]
    kinds = [(k, gt) for k, gt, _l in _walk(body)]
    assert kinds[0] == ('GRUP', 6), f"persistent group must lead: {kinds}"
    assert _blocks_of(out) == [(0, 0), (-1, -1)]
