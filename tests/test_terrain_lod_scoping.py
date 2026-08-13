"""An overlay must never import its OWN worldspaces into the target one.

`_scan_land_file` historically treated "this file has no such WRLD record" as
"no filter — take every LAND record". For the file a worldspace is sourced FROM
that fallback is a reasonable last resort. For an OVERLAY it is data corruption:
an override plugin routinely edits a master's worldspace through the master's
GRUPs while shipping no WRLD record of its own, so the fallback fires and sweeps
in every unrelated worldspace the plugin carries.

Measured on the real output: Morrowind_ob.esm ships no TES4Tamriel WRLD, so all
5,796 of its Vvardenfell cells were collected into Cyrodiil's heightmap, and
5,787 of them landed on coordinates Oblivion.esm legitimately owns — Vvardenfell
stamped across central Cyrodiil's distant terrain.

The fix scopes an overlay by the FormID resolved from the plugin that DEFINES
the worldspace, which is exactly the label on the type-1 GRUP its edits live
under.
"""

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asset_convert import terrain_lod
from asset_convert.terrain_lod import _scan_land_file


def _rec(sig: bytes, fid: int, body: bytes) -> bytes:
    """A TES5 record: 24-byte header + body."""
    return sig + struct.pack('<IIIII', len(body), 0, fid, 0, 0) + body


def _sub(sig: bytes, payload: bytes) -> bytes:
    return sig + struct.pack('<H', len(payload)) + payload


def _grup(label: bytes, gtype: int, payload: bytes) -> bytes:
    return (b'GRUP' + struct.pack('<I', 24 + len(payload)) + label
            + struct.pack('<III', gtype, 0, 0) + payload)


def _cell(fid: int, gx: int, gy: int, with_land: bool = True) -> bytes:
    """A CELL plus its type-6 children group holding one LAND."""
    cell = _rec(b'CELL', fid, _sub(b'XCLC', struct.pack('<ii', gx, gy)))
    if not with_land:
        return cell
    # VHGT: offset float + 33*33 deltas + 3 pad = 4 + 1089 + 3.
    vhgt = struct.pack('<f', float(gx)) + bytes(1089) + bytes(3)
    land = _rec(b'LAND', fid + 0x1000, _sub(b'VHGT', vhgt))
    return cell + _grup(struct.pack('<I', fid), 6, land)


def _plugin(worldspaces, masters=('Skyrim.esm', 'Oblivion.esm')) -> bytes:
    """A minimal ESM. `worldspaces` is [(wrld_fid, edid_or_None, [cells])].

    A MAST list is declared because FormIDs are now resolved through it: the
    index byte is an offset into THIS file's masters, so a file claiming none
    would own every id it mentions and `0100003C` would stop meaning
    Oblivion.esm's Tamriel. The default matches every real converted plugin.
    """
    hdr = b''.join(_sub(b'MAST', m.encode() + b'\0') + _sub(b'DATA', bytes(8))
                   for m in masters)
    out = _rec(b'TES4', 0, hdr)
    body = b''
    for wfid, edid, cells in worldspaces:
        if edid is not None:
            body += _rec(b'WRLD', wfid, _sub(b'EDID', edid.encode() + b'\0'))
        body += _grup(struct.pack('<I', wfid), 1, b''.join(cells))
    return out + _grup(b'WRLD', 0, body)


TARGET_FID = 0x0100003C

# `known_wrld_fid` is resolved from the plugin that DEFINES the worldspace and
# handed to scans of OTHER files, so it travels in the normalised space (see
# lod_gen._formid_remap_table). With the standard master list above, index byte
# 01 is Oblivion.esm; `_norm` states that mapping explicitly rather than
# hard-coding whatever integer the global table happens to assign.
def _norm(fid: int, masters=('Skyrim.esm', 'Oblivion.esm')) -> int:
    from asset_convert.lod_gen import _global_file_index
    owner = (masters[fid >> 24] if (fid >> 24) < len(masters) else None)
    assert owner is not None, 'test ids should name a declared master'
    return _global_file_index(owner.lower()) << 24 | (fid & 0x00FFFFFF)


@pytest.fixture
def plugin_file(tmp_path):
    def _make(name: str, worldspaces) -> Path:
        p = tmp_path / name
        p.write_bytes(_plugin(worldspaces))
        return p
    return _make


class TestOverlayScoping:
    def test_overlay_without_the_wrld_contributes_nothing_unscoped(
            self, plugin_file):
        """The Morrowind_ob case: no target WRLD, only its own worldspace."""
        esm = plugin_file('Overlay.esm', [
            (0x02380000, 'WrldMorrowind',
             [_cell(0x02380100, 5, 5), _cell(0x02380200, 6, 6)]),
        ])
        lands = {}
        _scan_land_file(esm, 'TES4Tamriel', lands, {}, {'default': None}, {},
                        allow_unscoped=False,
                        known_wrld_fid=_norm(TARGET_FID))
        assert lands == {}, (
            "an overlay's own worldspace must never be imported into the "
            "target worldspace")

    def test_the_old_wildcard_would_have_imported_them(self, plugin_file):
        """Guards the fix itself: the unscoped path still behaves as before,
        so this test fails loudly if the two branches are ever collapsed."""
        esm = plugin_file('Overlay.esm', [
            (0x02380000, 'WrldMorrowind',
             [_cell(0x02380100, 5, 5), _cell(0x02380200, 6, 6)]),
        ])
        lands = {}
        _scan_land_file(esm, 'TES4Tamriel', lands, {}, {'default': None}, {},
                        allow_unscoped=True)
        assert set(lands) == {(5, 5), (6, 6)}, (
            'the unscoped fallback is what the overlay path must NOT use')

    def test_overlay_editing_the_masters_worldspace_is_still_collected(
            self, plugin_file):
        """The edits that MUST survive: DLCBattlehornCastle regrades Tamriel
        cells through the master's GRUP while shipping no WRLD of its own.
        Scoping must not throw those away along with the foreign ones."""
        esm = plugin_file('Battlehorn.esp', [
            # Edits under the MASTER's worldspace FormID, no WRLD record.
            (TARGET_FID, None, [_cell(0x01000500, 1, 2)]),
            # ...and its own unrelated worldspace, which must be excluded.
            (0x02380000, 'SomeOtherWorld', [_cell(0x02380100, 5, 5)]),
        ])
        lands = {}
        _scan_land_file(esm, 'TES4Tamriel', lands, {}, {'default': None}, {},
                        allow_unscoped=False,
                        known_wrld_fid=_norm(TARGET_FID))
        assert set(lands) == {(1, 2)}, (
            'an override of the target worldspace must still be collected, '
            'and only that')

    def test_a_plugin_defining_the_worldspace_scopes_on_its_own_lookup(
            self, plugin_file):
        esm = plugin_file('Owner.esm', [
            (TARGET_FID, 'TES4Tamriel', [_cell(0x01000500, 1, 2)]),
            (0x02380000, 'Other', [_cell(0x02380100, 5, 5)]),
        ])
        lands = {}
        _scan_land_file(esm, 'TES4Tamriel', lands, {}, {'default': None}, {})
        assert set(lands) == {(1, 2)}

    def test_no_formid_and_no_fallback_takes_nothing(self, plugin_file):
        esm = plugin_file('Stray.esp', [
            (0x02380000, 'Other', [_cell(0x02380100, 5, 5)]),
        ])
        lands = {}
        _scan_land_file(esm, 'TES4Tamriel', lands, {}, {'default': None}, {},
                        allow_unscoped=False, known_wrld_fid=None)
        assert lands == {}


class TestParseLandRecords:
    def test_only_the_base_file_may_fall_back_to_unscoped(self, plugin_file,
                                                          monkeypatch):
        """End to end: the base keeps the fallback, overlays never get it."""
        base = plugin_file('Base.esm', [
            (TARGET_FID, 'TES4Tamriel', [_cell(0x01000500, 1, 2)]),
        ])
        overlay = plugin_file('Foreign.esm', [
            (0x02380000, 'WrldMorrowind', [_cell(0x02380100, 5, 5)]),
        ])
        lands, _water, _wh = terrain_lod._parse_land_records(
            base, 'TES4Tamriel', [overlay])
        assert set(lands) == {(1, 2)}, (
            'the overlay\'s own worldspace leaked into the merged heightmap')


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
