"""A NEW LAND whose parent CELL belongs to a MASTER must be nested, not dropped.

A plugin can lay BRAND-NEW terrain into a cell a master already owns. The cell
exists, so the plugin ships no CELL of its own, but the LAND is a new record
with an id in the plugin's own space -- it overrides nothing, so the override
path hands it back as "unattached" for the normal group builders.

Those builders only construct worldspaces THIS plugin defines, and the
worldspace here is the master's, so every such LAND was silently discarded.
Measured on TWMP_ValenwoodImproved.esp (masters Oblivion.esm + Tamriel.esp):
1,754 of its 2,626 LAND records name Tamriel worldspace 0000003C with a parent
CELL owned by Tamriel.esp, and all 1,754 vanished -- the output shipped 1,819
LANDs instead of 2,626. In-game the terrain never draws while the cell's
placed references still render, which is exactly the "LOD objects appear but
the land does not" symptom.

The fix routes LAND through _attach_new_records like REFR/ACHR: nested at
(6, cell), (9, cell) under the master's cell, with the parent cell anchored
from the master by emit_nested_overrides.
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tes5_import import overrides as OV


class _Ctx:
    """The pieces of OverrideContext that _attach_new_records touches."""

    def __init__(self, parent_path, parent_out, master_land=0):
        self._parent_path = parent_path
        self._parent_out = parent_out
        self.master_manifest = None
        self.master_export = {}
        self.land_cache = None

        outer = self

        class _Index:
            def group_path(self, fid):
                return outer._parent_path if fid == outer._parent_out else ()

            def land(self, cell_fid):
                return master_land if cell_fid == outer._parent_out else 0

        self.master_index = _Index()


def _land_record(fid, payload=b'DATA'):
    """A packed LAND record carrying `fid` in its header."""
    return (b'LAND' + struct.pack('<II', len(payload), 0)
            + struct.pack('<I', fid) + b'\x00' * 8 + payload)


def _fid_of(record_bytes):
    return struct.unpack_from('<I', record_bytes, 12)[0]


def _install_stubs(monkeypatch, parent_out, land_fid=0x0302B026):
    """master_output_formid -> parent_out; convert LAND to a real record."""
    monkeypatch.setattr(OV, 'master_output_formid',
                        lambda src, manifest: parent_out)
    monkeypatch.setattr(OV, '_convert_land',
                        lambda rec, ctx: _land_record(land_fid))


def test_new_land_under_master_cell_is_attached(monkeypatch):
    """The LAND is nested under the master's cell instead of handed back."""
    parent_out = 0x0102E086
    parent_path = ((0, b'WRLD'), (1, struct.pack('<I', 0x0100003C)),
                   (4, b'\xff\xff\xfe\xff'), (5, b'\xfc\xff\xf8\xff'))
    _install_stubs(monkeypatch, parent_out)

    ctx = _Ctx(parent_path, parent_out)
    rec = {'Signature': 'LAND', 'FormID': '0202B026',
           'ParentCELL': '0102E086', 'ParentWRLD': '0000003C'}
    pending = []

    done, unattached = OV._attach_new_records([('LAND', rec)], ctx, pending)

    assert done == 1, "the LAND must be attached under the master's cell"
    assert unattached == [], "it must NOT be handed to the own-hierarchy builder"
    assert len(pending) == 1

    fid, body, path = pending[0]
    # The master's cell has NO terrain here, so our own id is kept.
    assert _fid_of(body) == 0x0302B026
    assert fid == 0x0302B026
    label = struct.pack('<I', parent_out)
    # Terrain is never persistent: type 6 children, type 9 temporary.
    assert path == parent_path + ((6, label), (9, label))


def test_land_replacing_master_terrain_takes_its_formid(monkeypatch):
    """A cell owns ONE land: replacing terrain reuses the master's FormID.

    Shipping our own new id instead leaves TWO LAND records in one cell, which
    the engine cannot resolve while parsing -- the main-menu hang. Measured on
    TWMP_ValenwoodImproved: 1,754 cells that Tamriel.esp already gives terrain
    each received a duplicate.
    """
    parent_out = 0x0202E086
    master_land = 0x0202E087
    parent_path = ((0, b'WRLD'), (1, struct.pack('<I', 0x0100003C)))
    _install_stubs(monkeypatch, parent_out, land_fid=0x0302B026)

    ctx = _Ctx(parent_path, parent_out, master_land=master_land)
    rec = {'Signature': 'LAND', 'FormID': '0202B026',
           'ParentCELL': '0102E086', 'ParentWRLD': '0000003C'}
    pending = []

    done, _unattached = OV._attach_new_records([('LAND', rec)], ctx, pending)

    assert done == 1
    fid, body, _path = pending[0]
    assert _fid_of(body) == master_land, (
        "the record must SHIP with the master's LAND id, replacing it")
    assert fid == master_land, "pending must agree with the shipped bytes"


def test_new_land_in_own_worldspace_stays_unattached(monkeypatch):
    """A LAND under this plugin's OWN cell still goes to the normal builders.

    Only a MASTER-owned parent has a group path to nest into; when the parent
    is the plugin's own the record must be handed back, or a plugin that ships
    an entire world of its own (Morroblivion) would lose all of its terrain.
    """
    _install_stubs(monkeypatch, 0x03019A67)
    # No path for this parent -> not the master's.
    ctx = _Ctx((), 0xFFFFFFFF)
    rec = {'Signature': 'LAND', 'FormID': '0202B100',
           'ParentCELL': '0202BCD5', 'ParentWRLD': '0202BCD5'}
    pending = []

    done, unattached = OV._attach_new_records([('LAND', rec)], ctx, pending)

    assert done == 0
    assert unattached == [('LAND', rec)]
    assert pending == []


def test_land_cache_is_reused(monkeypatch):
    """A precomputed LAND is taken from the cache, never re-converted."""
    parent_out = 0x0102E086
    parent_path = ((0, b'WRLD'), (1, struct.pack('<I', 0x0100003C)))
    monkeypatch.setattr(OV, 'master_output_formid',
                        lambda src, manifest: parent_out)

    calls = []

    def _boom(rec):
        calls.append(rec)
        return b'RECONVERTED'

    monkeypatch.setattr('tes5_import.record_types.world.convert_LAND', _boom)

    ctx = _Ctx(parent_path, parent_out)
    # text_reader.get_formid shifts the index byte by the module offset, so key
    # the cache with the same value _convert_land will look up.
    from tes5_import.text_reader import get_formid
    rec = {'Signature': 'LAND', 'FormID': '0202B026',
           'ParentCELL': '0102E086', 'ParentWRLD': '0000003C'}
    ctx.land_cache = {get_formid(rec, 'FormID'): (True, _land_record(0x0302B026))}

    pending = []
    done, _unattached = OV._attach_new_records([('LAND', rec)], ctx, pending)

    assert done == 1
    assert _fid_of(pending[0][1]) == 0x0302B026
    assert calls == [], "convert_LAND must not run when the cache has the record"
