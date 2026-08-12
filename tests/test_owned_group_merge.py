"""A worldspace/cell/topic may own only ONE children group in a file.

Two passes append to the same top-level group — the override pass writes a
worldspace's children, then the WRLD builder writes the cells this plugin adds
to it — so the file ended up with two `GRUP World Children of 0100003C` in a
row. xEdit: "Found additional GRUP World Children of TES4Tamriel ... Skipped
Load: Merged N elements from duplicate group". The engine indexes a
worldspace's children once, so the second group's cells may never load.
"""

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tes5_import.writer import _merge_owned_groups, pack_group, pack_record


def _grup(gtype, label, body):
    return pack_group(gtype, label, body)


def _rec(sig, fid, payload=b''):
    """Built by the writer's own packer so the header layout cannot drift."""
    return pack_record(sig.decode(), fid, 0, payload)


def _walk(blob):
    """[(kind, gtype, label, size)] for the TOP level of a blob.

    A GRUP's size INCLUDES its 24-byte header; a record's does NOT.
    """
    out, off = [], 0
    while off + 24 <= len(blob):
        sig = blob[off:off + 4]
        raw = struct.unpack_from('<I', blob, off + 4)[0]
        if sig == b'GRUP':
            gt = struct.unpack_from('<i', blob, off + 12)[0]
            out.append(('GRUP', gt, blob[off + 8:off + 12], raw))
            off += raw
        else:
            out.append((sig.decode(), None, None, 24 + raw))
            off += 24 + raw
    return out


WRLD = struct.pack('<I', 0x0100003C)


class TestMergeOwnedGroups:
    def test_two_world_children_groups_become_one(self):
        a = _grup(1, WRLD, _rec(b'CELL', 1))
        b = _grup(1, WRLD, _rec(b'CELL', 2))
        got = _merge_owned_groups(_rec(b'WRLD', 0x0100003C) + a + b)
        top = _walk(got)
        groups = [t for t in top if t[0] == 'GRUP']
        assert len(groups) == 1
        assert groups[0][1] == 1 and groups[0][2] == WRLD

    def test_both_groups_contents_survive(self):
        a = _grup(1, WRLD, _rec(b'CELL', 0xAA))
        b = _grup(1, WRLD, _rec(b'CELL', 0xBB))
        got = _merge_owned_groups(a + b)
        inner = got[24:]
        assert _rec(b'CELL', 0xAA) in inner
        assert _rec(b'CELL', 0xBB) in inner

    def test_merged_group_size_is_restamped(self):
        a = _grup(1, WRLD, _rec(b'CELL', 1))
        b = _grup(1, WRLD, _rec(b'CELL', 2))
        got = _merge_owned_groups(a + b)
        declared = struct.unpack_from('<I', got, 4)[0]
        assert declared == len(got)

    def test_different_labels_stay_separate(self):
        other = struct.pack('<I', 0x0100003D)
        got = _merge_owned_groups(
            _grup(1, WRLD, _rec(b'CELL', 1)) + _grup(1, other, _rec(b'CELL', 2)))
        assert len([t for t in _walk(got) if t[0] == 'GRUP']) == 2

    @pytest.mark.parametrize('gtype', [1, 6, 7])
    def test_every_owned_group_type_merges(self, gtype):
        got = _merge_owned_groups(
            _grup(gtype, WRLD, _rec(b'REFR', 1))
            + _grup(gtype, WRLD, _rec(b'REFR', 2)))
        assert len([t for t in _walk(got) if t[0] == 'GRUP']) == 1

    @pytest.mark.parametrize('gtype', [0, 2, 3, 4, 5, 8, 9, 10])
    def test_non_owned_group_types_are_left_alone(self, gtype):
        """Blocks/sub-blocks and the cell's own 8/9/10 keep their own identity
        here — they are merged by their PARENT's contents, not at this level."""
        got = _merge_owned_groups(
            _grup(gtype, WRLD, _rec(b'REFR', 1))
            + _grup(gtype, WRLD, _rec(b'REFR', 2)))
        assert len([t for t in _walk(got) if t[0] == 'GRUP']) == 2

    def test_records_between_groups_keep_their_order(self):
        got = _merge_owned_groups(
            _rec(b'WRLD', 0x0100003C)
            + _grup(1, WRLD, _rec(b'CELL', 1))
            + _rec(b'WRLD', 0x0100003D))
        top = _walk(got)
        assert [t[0] for t in top] == ['WRLD', 'GRUP', 'WRLD']

    def test_empty_input(self):
        assert _merge_owned_groups(b'') == b''

    def test_single_group_is_unchanged(self):
        one = _grup(1, WRLD, _rec(b'CELL', 1))
        assert _merge_owned_groups(one) == one

    def test_nested_contents_are_not_reparsed(self):
        """A group's body moves verbatim — inner duplicates are not touched."""
        inner = _grup(6, WRLD, _rec(b'REFR', 1)) + _grup(6, WRLD, _rec(b'REFR', 2))
        one = _grup(1, WRLD, inner)
        assert _merge_owned_groups(one) == one
