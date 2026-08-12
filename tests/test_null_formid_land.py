"""Records shipped with a null FormID must survive parsing and get an id.

ElsweyrAnequina.esp ships 7 LAND records whose FormID is literally
0x00000000 (verified against the original Oblivion .esp — the mod's own data).
Oblivion reaches a cell's landscape through the cell's children group rather
than by id, so it never noticed. Two places in our import did:

  1. parse_export_directory deduplicates by FormID, and all 7 collapsed onto
     the single key '00000000' -- 6 of 7 silently discarded.
  2. the LAND conversion cache is keyed by FormID and popped by it, so the
     survivors would still have fought over one entry.

The visible result was blank, missing terrain with the cell's placed
references still rendering (reported in-game at cell -7,-32).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tes5_import.import_main import _repair_null_land_formids


class TestNullFormIdDedup:
    """parse_export_directory must not treat 00000000 as an identity."""

    def _dedup(self, records):
        """The dedup step from parse_export_directory, in isolation."""
        seen = {}
        for i, rec in enumerate(records):
            fid = rec.get('FormID')
            if fid and fid.strip('0'):
                seen[fid] = i
            else:
                seen[('\0null', i)] = i
        if len(seen) < len(records):
            keep = set(seen.values())
            records = [r for i, r in enumerate(records) if i in keep]
        return records

    def test_null_formids_are_all_kept(self):
        recs = [{'FormID': '00000000', 'ParentCELL': '0101C27A'},
                {'FormID': '00000000', 'ParentCELL': '0101C27B'},
                {'FormID': '00000000', 'ParentCELL': '0101C2A8'}]
        out = self._dedup(list(recs))
        assert len(out) == 3, 'null FormIDs are not identities; none may collapse'
        assert [r['ParentCELL'] for r in out] == \
            ['0101C27A', '0101C27B', '0101C2A8']

    def test_real_duplicate_formids_still_collapse(self):
        """The dedup must keep doing its actual job."""
        recs = [{'FormID': '0001C27A', 'v': 'first'},
                {'FormID': '0001C27A', 'v': 'last'}]
        out = self._dedup(list(recs))
        assert len(out) == 1 and out[0]['v'] == 'last', \
            'a genuine duplicate FormID still keeps the last occurrence'

    def test_mixed_null_and_real(self):
        recs = [{'FormID': '00000000', 'ParentCELL': 'A'},
                {'FormID': '0001C27A', 'v': 'x'},
                {'FormID': '00000000', 'ParentCELL': 'B'},
                {'FormID': '0001C27A', 'v': 'y'}]
        out = self._dedup(list(recs))
        assert len(out) == 3
        assert sum(1 for r in out if r['FormID'] == '00000000') == 2


class TestRepairNullLandFormIds:
    """Each null-id LAND gets an id unique across the WHOLE plugin.

    Skyrim's form table is keyed by FormID with no per-signature namespace, so
    "unique among LAND records" is not enough -- see
    test_id_never_equals_the_parent_cell.
    """

    OWN = 0x01          # this plugin's load-order index in the export
    MAXFID = 0x0DFE10   # highest own-space id, as measured on ElsweyrAnequina

    def _repair(self, by_type):
        _repair_null_land_formids(by_type, self.OWN, self.MAXFID)
        return by_type

    def test_each_land_gets_a_unique_id(self):
        by_type = self._repair({'LAND': [
            {'FormID': '00000000', 'ParentCELL': '0101C27A'},
            {'FormID': '00000000', 'ParentCELL': '0101C27B'},
            {'FormID': '00000000', 'ParentCELL': '0101C2A8'},
        ]})
        ids = [r['FormID'] for r in by_type['LAND']]
        assert len(set(ids)) == 3, 'ids must be unique or the cache collides'
        assert all(i.strip('0') for i in ids), 'no id may remain null'

    def test_id_never_equals_the_parent_cell(self):
        """THE SHIPPED BUG. A cell owns at most one LAND, so reusing the
        cell's own id looked unique -- but the CELL already holds that id.
        Only one form per id loads, the CELL won, and the landscape never
        became a form: blank terrain with the cell's references still drawn.
        """
        cells = ['0101C27A', '0101C27B', '0101C2A8', '0101C2B8']
        by_type = self._repair({
            'LAND': [{'FormID': '00000000', 'ParentCELL': c} for c in cells],
            'CELL': [{'FormID': c} for c in cells],
        })
        for rec in by_type['LAND']:
            assert rec['FormID'] != rec['ParentCELL'], \
                f"LAND reused its CELL's id {rec['FormID']}"

    def test_id_collides_with_no_record_of_any_signature(self):
        """Uniqueness is plugin-wide, not per-type."""
        by_type = self._repair({
            'LAND': [{'FormID': '00000000', 'ParentCELL': '0101C2A8'},
                     {'FormID': '00000000', 'ParentCELL': '0101C2B8'}],
            'CELL': [{'FormID': '0101C2A8'}, {'FormID': '0101C2B8'}],
            'REFR': [{'FormID': '010DFE0F'}, {'FormID': '010DFE10'}],
            'STAT': [{'FormID': '0000ABCD'}],
        })
        synthesized = [r['FormID'] for r in by_type['LAND']]
        others = [r['FormID'] for sig, recs in by_type.items() if sig != 'LAND'
                  for r in recs]
        assert not (set(synthesized) & set(others)), 'synthesized id collided'
        assert len(set(synthesized)) == 2

    def test_ids_sit_in_the_reserved_gap(self):
        """Above every real record, below alloc_formid()'s base.

        import_plugin sets the allocator to max_formid + 0x1000, so the gap
        (max_formid, max_formid + 0x1000) belongs to nothing else. Landing
        below it would hit a real record; landing on or above it would collide
        with a companion record minted later.
        """
        by_type = self._repair({'LAND': [
            {'FormID': '00000000', 'ParentCELL': '0101C2A8'},
            {'FormID': '00000000', 'ParentCELL': '0101C2B8'},
        ]})
        for rec in by_type['LAND']:
            low = int(rec['FormID'], 16) & 0xFFFFFF
            assert self.MAXFID < low < self.MAXFID + 0x1000, \
                f'{rec["FormID"]} escaped the reserved gap'

    def test_id_is_derived_not_allocated(self):
        """Stable across runs; alloc_formid would cause FormID drift."""
        make = lambda: {'LAND': [{'FormID': '00000000',
                                  'ParentCELL': '0101C2A8'}]}
        a, b = self._repair(make()), self._repair(make())
        assert a['LAND'][0]['FormID'] == b['LAND'][0]['FormID']

    def test_id_carries_our_own_load_order_index(self):
        """The index byte says WHICH PLUGIN owns the record.

        An early attempt OR'd in 0x0F000000 to avoid collisions, putting the
        records at index 0x10 where no plugin is loaded -- the engine could
        not resolve them and the land stayed missing in-game.
        """
        by_type = self._repair({'LAND': [
            {'FormID': '00000000', 'ParentCELL': '0101C2A8'}]})
        assert by_type['LAND'][0]['FormID'][:2] == '01'

    def test_real_formids_untouched(self):
        by_type = self._repair(
            {'LAND': [{'FormID': '00034A9D', 'ParentCELL': '00008334'}]})
        assert by_type['LAND'][0]['FormID'] == '00034A9D'

    def test_null_parent_is_left_alone(self):
        """No parent cell -- the land has no home, so an id would not help."""
        by_type = self._repair(
            {'LAND': [{'FormID': '00000000', 'ParentCELL': '00000000'}]})
        assert by_type['LAND'][0]['FormID'] == '00000000'


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
