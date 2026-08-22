"""WRLD MNAM — the world map's pan rectangle.

The engine clamps world-map panning to a border polygon built from MNAM's
NW/SE CELL pair (SkyrimSE RVA 0x9213e0); NAM0/NAM9 is only the fallback used
when all four cell values are zero.  So the rectangle has to cover the land a
plugin actually adds, and it has to survive every path that emits a WRLD.

See docs/world_land_navmesh_notes.md.
"""
import struct

from tes5_import.record_types.world import (
    build_wrld_mnam,
    restamp_wrld_mnam,
    set_world_land_extents,
)

TAMRIEL = 0x0100003C

# Oblivion's authored rectangle: NW (-59, 47), SE (59, -58) -- 119x106 cells.
AUTHORED = {
    'FormID': '0100003C',
    'MNAM.UsableDimX': '2055',
    'MNAM.UsableDimY': '1830',
    'MNAM.NWCellX': '-59',
    'MNAM.NWCellY': '47',
    'MNAM.SECellX': '59',
    'MNAM.SECellY': '-58',
}

# Tamriel.esp's real terrain: grid X -192..191, Y -129..159.  _land_extents_by_wrld
# reports the FAR edge of the last cell, hence 192/160 rather than 191/159.
MEASURED = {TAMRIEL: (-192 * 4096.0, -129 * 4096.0, 192 * 4096.0, 160 * 4096.0)}


def _cells(payload):
    """(NW.X, NW.Y, SE.X, SE.Y) from a 28-byte MNAM payload."""
    return struct.unpack('<hhhh', payload[8:16])


def test_measured_grid_wins_over_authored_rectangle():
    set_world_land_extents(MEASURED)
    try:
        assert _cells(build_wrld_mnam(AUTHORED)) == (-192, 159, 191, -129)
    finally:
        set_world_land_extents({})


def test_authored_rectangle_is_the_fallback():
    """A worldspace whose cells we never measured keeps its authored framing."""
    set_world_land_extents({})
    assert _cells(build_wrld_mnam(AUTHORED)) == (-59, 47, 59, -58)


def test_usable_dimensions_written_zero():
    """All 3 Skyrim.esm WRLDs carrying MNAM write (0, 0); Oblivion's is stale."""
    set_world_land_extents({})
    assert struct.unpack('<ii', build_wrld_mnam(AUTHORED)[:8]) == (0, 0)


def test_no_mnam_and_no_extent_emits_nothing():
    set_world_land_extents({})
    assert build_wrld_mnam({'FormID': '0100003C'}) is None


def _wrld_record(nwx, nwy, sex, sey):
    """A minimal converted WRLD carrying EDID + MNAM, for the anchor paths."""
    mnam = struct.pack('<iihhhhfff', 0, 0, nwx, nwy, sex, sey,
                       50000.0, 80000.0, 50.0)
    body = (b'EDID' + struct.pack('<H', 8) + b'Tamriel\x00'
            + b'MNAM' + struct.pack('<H', len(mnam)) + mnam)
    # 24-byte record header: sig(4) size(4) flags(4) formid(4) + 8 trailing.
    return (b'WRLD' + struct.pack('<III', len(body), 0, TAMRIEL)
            + b'\x00' * 8 + body)


def test_restamp_widens_an_anchored_master_record():
    """The anchor paths copy the MASTER's bytes, which hold ITS rectangle.

    The last plugin to override a WRLD wins, so one verbatim anchor would undo
    every other plugin's widened MNAM.
    """
    rec = _wrld_record(-59, 47, 59, -58)
    set_world_land_extents(MEASURED)
    try:
        out = restamp_wrld_mnam(rec, TAMRIEL)
    finally:
        set_world_land_extents({})
    size = struct.unpack('<I', out[4:8])[0]
    body = out[24:24 + size]
    assert _cells(body[body.index(b'MNAM') + 6:]) == (-192, 159, 191, -129)
    # Rewritten in place: nothing else about the record may move.
    assert len(out) == len(rec)
    assert out[:24] == rec[:24]


def test_restamp_is_a_noop_without_a_registered_extent():
    rec = _wrld_record(-59, 47, 59, -58)
    set_world_land_extents({})
    assert restamp_wrld_mnam(rec, TAMRIEL) == rec


def test_restamp_leaves_compressed_records_alone():
    rec = _wrld_record(-59, 47, 59, -58)
    compressed = rec[:8] + struct.pack('<I', 0x00040000) + rec[12:]
    set_world_land_extents(MEASURED)
    try:
        assert restamp_wrld_mnam(compressed, TAMRIEL) == compressed
    finally:
        set_world_land_extents({})


def test_registering_extents_unions_rather_than_replaces():
    """Two calls per import; the second must never shrink the first.

    import_plugin registers over EVERY exterior cell before the override pass,
    then _build_world_groups registers again over just the own-hierarchy
    cells. A replacing register let that narrower second call clamp the map
    back down.
    """
    set_world_land_extents(MEASURED)
    try:
        # A later, narrower measurement of the same worldspace.
        set_world_land_extents(
            {TAMRIEL: (-64 * 4096.0, -69 * 4096.0, 70 * 4096.0, 60 * 4096.0)})
        assert _cells(build_wrld_mnam(AUTHORED)) == (-192, 159, 191, -129)
    finally:
        set_world_land_extents({})


def test_registering_extents_still_widens():
    set_world_land_extents(
        {TAMRIEL: (-64 * 4096.0, -69 * 4096.0, 70 * 4096.0, 60 * 4096.0)})
    try:
        set_world_land_extents(MEASURED)
        assert _cells(build_wrld_mnam(AUTHORED)) == (-192, 159, 191, -129)
    finally:
        set_world_land_extents({})
