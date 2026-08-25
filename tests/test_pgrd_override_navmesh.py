"""A PGRD that edits a MASTER's cell must still produce a NAVM.

A pathgrid converts to a navmesh — a brand-new record with its own FormID —
so it can never be expressed as an override of the master's bytes. It used to
be listed in OVERRIDE_UNMAPPABLE_TYPES and was dropped in the override pass,
before the navmesh generator ever saw it: ElsweyrAnequina lost the navmesh of
all 863 Oblivion.esm Tamriel cells it edits (547 NAVM shipped where 1,351
pathgrids exist), so the landmass rendered fine and nothing in it could path.
"""

from collections import Counter


from tes5_import import overrides


class _FakeMasterIndex:
    def record(self, fid):
        return b''

    def group_path(self, fid):
        return ((0, b'WRLD'),)

    def land(self, fid):
        return 0


class _Ctx:
    """Minimal stand-in for OverrideContext (only what the routing touches)."""

    def __init__(self, master_export=None, navm_cache=None):
        self.master_export = master_export or {}
        self.master_manifest = {}
        self.master_index = _FakeMasterIndex()
        self.stats = Counter()
        self.navm_cache = navm_cache or {}
        self.navm_metas = []
        self.land_cache = None

    def master_record(self, rec):
        return self.master_export.get((rec.get('FormID') or '').upper())

    build = overrides.OverrideContext.build


def test_pgrd_is_not_an_unmappable_type():
    assert 'PGRD' not in overrides.OVERRIDE_UNMAPPABLE_TYPES
    # ROAD really does convert to nothing and stays unmappable.
    assert 'ROAD' in overrides.OVERRIDE_UNMAPPABLE_TYPES


def test_pgrd_nests_under_its_parent_cell():
    assert overrides._NEW_NESTED_PARENT.get('PGRD') == 'ParentCELL'


def test_overriding_pgrd_routes_as_a_new_record():
    """build() must return None (= "new record") even when the master has it."""
    ctx = _Ctx(master_export={'0000610A': {'FormID': '0000610A',
                                           'Signature': 'PGRD'}})
    rec = {'FormID': '0000610A', 'ParentCELL': '0000610B', 'Signature': 'PGRD'}

    assert ctx.build(rec, 'PGRD') is None
    # It must NOT be counted as inexpressible — that was the silent drop.
    assert ctx.stats['no-path'] == 0


def test_pgrd_geometry_comes_from_the_precompute_cache():
    """_navm_of keys by (ParentCELL, PGRD), matching _gather_navm_jobs."""
    navm_bytes = b'NAVM' + b'\x00' * 20
    meta = {'fid': 0x0500BEEF}
    ctx = _Ctx(navm_cache={(0x0000610B, 0x0000610A): (navm_bytes, meta)})
    rec = {'FormID': '0000610A', 'ParentCELL': '0000610B'}

    got_bytes, got_meta = overrides._navm_of(rec, ctx)
    assert got_bytes == navm_bytes
    assert got_meta is meta


def test_pgrd_with_no_generated_geometry_is_skipped_cleanly():
    """convert_PGRD declines a too-sparse pathgrid; that is not an error."""
    ctx = _Ctx(navm_cache={})
    rec = {'FormID': '0000610A', 'ParentCELL': '0000610B'}

    assert overrides._navm_of(rec, ctx) == (b'', {})
