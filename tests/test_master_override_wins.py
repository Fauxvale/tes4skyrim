"""An override's base is the LAST master that defines the record.

ChainedMasterIndex routes a FormID by its index byte, which names the master
that DEFINES the record. That is right for identity and group nesting, but the
record's CONTENT must come from the last file in load order that defines it --
a later master may override it, and the engine uses that version.

Tamriel.esp overrides Oblivion.esm's Tamriel worldspace 0100003C to WIDEN its
NAM0/NAM9 bounds (+/-262144 -> +/-786432) so the 99,946 exterior cells it adds
fit inside the worldspace. Routing to the owner alone handed back Oblivion's
ORIGINAL rectangle, so TWMP_ValenwoodImproved's WRLD override was spliced onto
the narrow bounds and shipped them -- putting 92,745 of Tamriel.esp's cells
outside the worldspace extent. The engine builds its cell grid from NAM0/NAM9
while PARSING the file, so the game hung on the main menu with no crash and no
log; deleting the WRLD record in xEdit made it load again.

Identity still follows the owner: `land()` must answer with the index byte of
the file that defines the LAND, never the overriding master's slot.
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tes5_import.override_merge import ChainedMasterIndex


class _FakeIndex:
    """Stands in for a MasterIndex over one converted master."""

    def __init__(self, own_index, records, masters=(), lands=None, name=''):
        self.own_index = own_index
        self.masters = list(masters)
        # _route identifies an overriding master by NAME, so the stub needs the
        # same output/<plugin>/<plugin> shape a real MasterIndex is loaded from.
        self.path = f'output/{name}/{name}' if name else ''
        self._records = dict(records)
        self._offsets = {f: (b[:4], 0, len(b)) for f, b in self._records.items()}
        self._lands = dict(lands or {})

    def __contains__(self, fid):
        return fid in self._records

    def record(self, fid):
        return self._records.get(fid, b'')

    def signature(self, fid):
        return self._records.get(fid, b'')[:4]

    def group_path(self, fid):
        return ((0, b'WRLD'),) if fid in self._records else ()

    def land(self, cell_fid):
        return self._lands.get(cell_fid, 0)


def _wrld(fid, min_x):
    """A WRLD record whose NAM0 carries `min_x`, for identifying the base."""
    body = b'NAM0' + struct.pack('<H', 8) + struct.pack('<ff', min_x, min_x)
    return (b'WRLD' + struct.pack('<II', len(body), 0)
            + struct.pack('<I', fid) + b'\x00' * 8 + body)


def _min_x(record_bytes):
    i = 24
    while i + 6 <= len(record_bytes):
        sig = record_bytes[i:i + 4]
        size = struct.unpack_from('<H', record_bytes, i + 4)[0]
        if sig == b'NAM0':
            return struct.unpack_from('<f', record_bytes, i + 6)[0]
        i += 6 + size
    return None


def _chain():
    """Oblivion.esm at slot 1, Tamriel.esp at slot 2 overriding 0100003C."""
    oblivion = _FakeIndex(
        own_index=1,
        records={0x0100003C: _wrld(0x0100003C, -262144.0)},
        masters=['Skyrim.esm'],
        # Oblivion defines the LAND inside its own cell.
        lands={0x0100AE18: 0x0100B015},
        name='Oblivion.esm',
    )
    tamriel = _FakeIndex(
        own_index=2,
        # Tamriel.esp OVERRIDES the worldspace, keeping Oblivion's index byte.
        records={0x0100003C: _wrld(0x0100003C, -786432.0)},
        masters=['Skyrim.esm', 'Oblivion.esm'],
        lands={0x0100AE18: 0x0100B015},
        name='Tamriel.esp',
    )
    return ChainedMasterIndex(
        [oblivion, tamriel], base_slot=1,
        child_masters=['Skyrim.esm', 'Oblivion.esm', 'Tamriel.esp'])


def test_later_master_supplies_the_base():
    """The override base is Tamriel.esp's widened rectangle, not Oblivion's."""
    chain = _chain()
    assert _min_x(chain.record(0x0100003C)) == -786432.0, (
        "an override must be built on the LAST master that defines the record")


def test_owner_only_record_is_untouched():
    """A record no later master overrides still comes from its owner."""
    oblivion = _FakeIndex(own_index=1,
                          records={0x0100AAAA: _wrld(0x0100AAAA, -1.0)},
                          masters=['Skyrim.esm'], name='Oblivion.esm')
    tamriel = _FakeIndex(own_index=2, records={},
                         masters=['Skyrim.esm', 'Oblivion.esm'],
                         name='Tamriel.esp')
    chain = ChainedMasterIndex(
        [oblivion, tamriel], base_slot=1,
        child_masters=['Skyrim.esm', 'Oblivion.esm', 'Tamriel.esp'])
    assert _min_x(chain.record(0x0100AAAA)) == -1.0


def test_land_keeps_the_defining_masters_index_byte():
    """land() answers in the slot of the file that DEFINES the terrain.

    Translating through the answering index moved every inherited LAND into
    the overriding master's slot, where no such record exists.
    """
    chain = _chain()
    assert chain.land(0x0100AE18) == 0x0100B015


def test_signature_still_resolves_through_the_override():
    chain = _chain()
    assert chain.signature(0x0100003C) == b'WRLD'
    assert chain.group_path(0x0100003C) == ((0, b'WRLD'),)


def test_same_id_in_an_unrelated_master_is_not_an_override():
    """Two masters using the same index byte must not be confused.

    Tamriel.esp and ElsweyrAnequina.esp are each the SECOND file in their own
    load order, so both number their own records 02xxxxxx and their id spaces
    overlap almost completely. 0202E438 is a CELL in Tamriel.esp and the WRLD
    ANQVerkarthHillsWorld in ElsweyrAnequina.esp.

    Treating "a later master contains this integer" as an override routed the
    Tamriel CELL to Anequina's worldspace: TWMP_Valenwood_Elsweyr then emitted
    that WRLD twice and anchored a cell's children group under it, producing
    389 duplicate FormIDs. A real override must come from a file that lists
    the owner among ITS masters AND carry the owner's signature.
    """
    tamriel = _FakeIndex(
        own_index=2,
        records={0x0202E438: (b'CELL' + struct.pack('<II', 0, 0)
                              + struct.pack('<I', 0x0202E438) + b'\x00' * 8)},
        masters=['Skyrim.esm', 'Oblivion.esm'], name='Tamriel.esp')
    anequina = _FakeIndex(
        own_index=2,
        records={0x0202E438: _wrld(0x0202E438, -1.0)},
        # Anequina does NOT list Tamriel.esp as a master.
        masters=['Skyrim.esm', 'Oblivion.esm'], name='ElsweyrAnequina.esp')
    chain = ChainedMasterIndex(
        [tamriel, anequina], base_slot=2,
        child_masters=['Skyrim.esm', 'Oblivion.esm', 'Tamriel.esp',
                       'ElsweyrAnequina.esp'])

    assert chain.signature(0x0202E438) == b'CELL', (
        "the id belongs to Tamriel.esp; Anequina's same-numbered WRLD is a "
        "different record entirely")
