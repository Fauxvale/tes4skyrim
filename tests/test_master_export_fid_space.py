"""A master's record must be indexed by the id THIS plugin uses for it.

`load_master_export` parses each master's own export and re-keys every record
into the importing plugin's index space, because a master's `FormID` field is
in the MASTER's space -- its index byte counts that file's masters, not ours.
The dict KEY carries the correction; the record's `FormID` field does not.

Everything downstream (VMAD property values, placed-ref rebinding) feeds these
ids to `remap_formid`, which applies a blanket `+offset` valid ONLY for an id
already in our space. So indexing a master record on its raw `FormID` field
double-counts: the id lands on whatever file our offset happens to reach.

Measured 2026-08-12 on ElsweyrPelletine.esp (masters Oblivion.esm, Tamriel.esp,
ElsweyrAnequina.esp, TWMP_Valenwood_Elsweyr.esp):

    FACT ANQCORCorintheFaction is 010247E2 in ElsweyrAnequina.esp, which has
    ONE master -- so `01` is Anequina's own space. Pelletine has four masters
    and load_master_export re-keys it to 020247E2 (`02` = Anequina in
    Pelletine's TES4 list). Keying on the raw field registered 010247E2
    instead; +1 produced 020247E2 in TES5 space, which is **Tamriel.esp** --
    a LAND record.

The VM bound `Faction Property ANQCORCorintheFaction` to that LAND, so every
`GetCrimeGoldViolent()` aborted, and because the converted OnUpdate re-arms
`RegisterForSingleUpdate(0.5)` outside any success check it retried forever
(864 stack frames in one 3-minute session). Note NO "cannot be bound" line is
logged for this class: the id resolves to a REAL record, just the wrong one.

Audit an output plugin with:
    python tools/vmad_property_typecheck.py --plugin <p> --cross-master
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _write_export(root: Path, name: str, masters, records):
    """A minimal export directory: _HEADER.txt plus one record file."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    hdr = ['HEDR.Version=1.0', 'HEDR.NumRecords=%d' % len(records)]
    hdr += ['Master[%d]=%s' % (i, m) for i, m in enumerate(masters)]
    (d / '_HEADER.txt').write_text('\n'.join(hdr) + '\n', encoding='utf-8')

    by_sig = {}
    for sig, fid, edid in records:
        by_sig.setdefault(sig, []).append((fid, edid))
    for sig, rows in by_sig.items():
        blocks = []
        for fid, edid in rows:
            blocks.append('---RECORD_BEGIN---\nSignature=%s\nFormID=%s\n'
                          'EditorID=%s\n---RECORD_END---' % (sig, fid, edid))
        (d / ('%s.txt' % sig)).write_text('\n'.join(blocks) + '\n',
                                          encoding='utf-8')
    return d


def test_master_record_keyed_in_this_plugins_space(tmp_path):
    """The real ANQCORCorintheFaction shape: a master with FEWER masters.

    Anequina's own records sit at index 01 in its file; in the child's list
    Anequina is slot 02. The key must be 02..., not the raw 01... .
    """
    from tes5_import.overrides import load_master_export

    _write_export(tmp_path, 'Oblivion.esm', [],
                  [('FACT', '00012345', 'VanillaFaction')])
    _write_export(tmp_path, 'Tamriel.esp', ['Oblivion.esm'],
                  [('LAND', '010247E2', 'TamrielLand')])
    _write_export(tmp_path, 'ElsweyrAnequina.esp', ['Oblivion.esm'],
                  [('FACT', '010247E2', 'ANQCORCorintheFaction')])
    child = _write_export(
        tmp_path, 'ElsweyrPelletine.esp',
        ['Oblivion.esm', 'Tamriel.esp', 'ElsweyrAnequina.esp'],
        [('QUST', '030000AA', 'PELQuest')])

    me = load_master_export(str(child))

    # Anequina is slot 02 in the child's master list -> its own 01 becomes 02.
    assert me['020247E2']['EditorID'] == 'ANQCORCorintheFaction'
    assert me['020247E2']['Signature'] == 'FACT'
    # Tamriel is slot 01 -> its own 01 becomes 01 (unchanged, but distinct).
    assert me['010247E2']['EditorID'] == 'TamrielLand'
    # The two masters' identical local ids must NOT collide.
    assert me['010247E2'] is not me['020247E2']


def test_fid_to_edid_uses_the_rekeyed_id(tmp_path):
    """The map the VMAD builder reads must be keyed on the corrected id.

    This is the regression: building it from `rec['FormID']` registers the
    master's own-space id, and the later blanket +offset then resolves the
    property to a different FILE.
    """
    from tes5_import.overrides import load_master_export

    _write_export(tmp_path, 'Oblivion.esm', [],
                  [('FACT', '00012345', 'VanillaFaction')])
    _write_export(tmp_path, 'Tamriel.esp', ['Oblivion.esm'],
                  [('LAND', '010247E2', 'TamrielLand')])
    _write_export(tmp_path, 'ElsweyrAnequina.esp', ['Oblivion.esm'],
                  [('FACT', '010247E2', 'ANQCORCorintheFaction')])
    child = _write_export(
        tmp_path, 'ElsweyrPelletine.esp',
        ['Oblivion.esm', 'Tamriel.esp', 'ElsweyrAnequina.esp'],
        [('QUST', '030000AA', 'PELQuest')])

    me = load_master_export(str(child))

    # Mirror import_main's construction: iterate items(), not values().
    fid_to_edid = {}
    for fid_str, rec in me.items():
        edid = rec.get('EditorID', '')
        if fid_str and edid:
            fid_to_edid[int(fid_str, 16)] = edid

    assert fid_to_edid[0x020247E2] == 'ANQCORCorintheFaction'
    assert fid_to_edid[0x010247E2] == 'TamrielLand'

    # And the id the writer ultimately emits: +1 for the prepended Skyrim.esm.
    # 02 -> 03, which is ElsweyrAnequina.esp in the OUTPUT master list
    # (Skyrim, Oblivion, Tamriel, Anequina). Keying on the raw 01 would have
    # produced 02 = Tamriel.esp, the LAND -- the shipped defect.
    from tes5_import.text_reader import remap_formid
    assert remap_formid(0x020247E2, offset=1) == 0x030247E2


def test_reference_fields_are_rekeyed_with_their_record(tmp_path):
    """A master record's SCRI/NAME ids need the SAME correction as its key.

    The cross-ref graph chains them (record_scri[fid] -> script edid), so a
    value left in the master's space misses every key in the graph.
    """
    from tes5_import.overrides import load_master_export

    _write_export(tmp_path, 'Oblivion.esm', [], [])
    _write_export(tmp_path, 'Tamriel.esp', ['Oblivion.esm'], [])
    _write_export(tmp_path, 'ElsweyrAnequina.esp', ['Oblivion.esm'],
                  [('SCPT', '01005000', 'ANQSomeScript')])
    child = _write_export(
        tmp_path, 'ElsweyrPelletine.esp',
        ['Oblivion.esm', 'Tamriel.esp', 'ElsweyrAnequina.esp'], [])

    me = load_master_export(str(child))
    # The SCPT the master owns is reachable under the child's index byte.
    assert me['02005000']['EditorID'] == 'ANQSomeScript'

    # The shift applied to the key is what any id field on that record needs.
    own_raw, own_key = 0x01005000, 0x02005000
    shift = ((own_key >> 24) & 0xFF) - ((own_raw >> 24) & 0xFF)
    assert shift == 1
    ref = 0x01004000                       # a sibling in the master's space
    rekeyed = ((((ref >> 24) & 0xFF) + shift) << 24) | (ref & 0xFFFFFF)
    assert rekeyed == 0x02004000


# ---------------------------------------------------------------------------
# VMAD is not in _FORMID_FIELDS: its ids sit at positions that depend on the
# preceding variable-length names, so the override restater has to WALK it.
# Skipping it shipped the master's own property ids inside an override whose
# header had been restated -- 23 properties across 7 QF_ scripts on Pelletine.
# ---------------------------------------------------------------------------

import struct


def _vmad(scripts):
    """Build a VMAD payload: [(script_name, [(prop, type, value)])]."""
    out = struct.pack('<hhH', 5, 2, len(scripts))
    for sname, props in scripts:
        out += struct.pack('<H', len(sname)) + sname.encode()
        out += b'\x00'                                  # flags
        out += struct.pack('<H', len(props))
        for pname, ptype, val in props:
            out += struct.pack('<H', len(pname)) + pname.encode()
            out += bytes([ptype, 1])                    # type + status
            if ptype == 1:
                out += struct.pack('<II', 0, val)
            elif ptype == 2:
                out += struct.pack('<H', len(val)) + val.encode()
            else:
                out += struct.pack('<I', val)
    return out


def test_shift_vmad_moves_object_properties():
    from tes5_import.override_merge import _shift_vmad

    payload = _vmad([('QF_ANQMerchantsGuild04', [
        ('TES4Unlock_ANQMCG04Bookkeeper', 1, 0x020E0E24),
        ('ANQMerchantsGuild04', 1, 0x02061ABC),
    ])])
    out = _shift_vmad(payload, {2: 3})          # Anequina 02 -> child slot 03

    assert len(out) == len(payload)             # never resize a subrecord
    assert struct.pack('<I', 0x030E0E24) in out
    assert struct.pack('<I', 0x030247E2) not in out
    assert struct.pack('<I', 0x020E0E24) not in out
    assert struct.pack('<I', 0x03061ABC) in out


def test_shift_vmad_identity_map_is_byte_exact():
    """An index the map does not mention must be left strictly alone."""
    from tes5_import.override_merge import _shift_vmad

    payload = _vmad([
        ('QF_Thing', [('A', 1, 0x01001234), ('Name', 2, 'hello'),
                      ('Count', 3, 7), ('Flag', 5, 1)]),
        ('QF_Other', [('B', 1, 0x04005678)]),
    ])
    assert _shift_vmad(payload, {}) == payload
    # Only the mentioned byte moves; 01 and 04 stay put.
    out = _shift_vmad(payload, {2: 3})
    assert out == payload


def test_shift_vmad_returns_unparsable_payload_untouched():
    """A half-rewritten VMAD would corrupt a binding -- refuse instead."""
    from tes5_import.override_merge import _shift_vmad

    junk = b'\x05\x00\x02\x00\x01\x00' + b'\xff' * 3      # truncated
    assert _shift_vmad(junk, {2: 3}) == junk
    assert _shift_vmad(b'', {2: 3}) == b''
