"""Structural tests for the TESGameSelect starter plugin (tools/make_game_select_esp.py).

These lock the two contracts that are invisible in-game until they are already
broken: the MESG button/condition layout (a wrong one silently starts the wrong
game) and the .seq listing (without it the Start-Game-Enabled quest never runs
and the menu simply never appears).
"""
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.make_game_select_esp import (BUTTONS, FID_MESG, FID_QUST,
                                        FID_GLOB_OBLIVION, FID_GLOB_NEHRIM,
                                        FID_GLOB_MORROBLIVION, GLOBALS,
                                        SGE_FLAGS, QUEST_TYPE_NONE,
                                        FUNC_GET_GLOBAL_VALUE, SCRIPT_NAME,
                                        build_plugin)


def _parse(data):
    """Split a built plugin into {(type, formid): [(subtype, payload), ...]}."""
    records = {}
    pos = 0
    while pos + 24 <= len(data):
        tag = data[pos:pos + 4]
        size = struct.unpack_from('<I', data, pos + 4)[0]
        if tag == b'GRUP':
            pos += 24          # descend into the group's contents
            continue
        fid = struct.unpack_from('<I', data, pos + 12)[0]
        body = data[pos + 24:pos + 24 + size]
        subs = []
        p = 0
        while p + 6 <= len(body):
            stype = body[p:p + 4].decode('ascii')
            slen = struct.unpack_from('<H', body, p + 4)[0]
            subs.append((stype, body[p + 6:p + 6 + slen]))
            p += 6 + slen
        records[(tag.decode('ascii'), fid)] = subs
        pos += 24 + size
    return records


@pytest.fixture(scope='module')
def built():
    data, count = build_plugin()
    return data, count, _parse(data)


def test_header_declares_only_skyrim_master(built):
    """Every foreign form is resolved at runtime via GetFormFromFile, so the
    plugin must NOT master the converted games — mastering one would make the
    plugin fail to load for users who don't have that game installed."""
    data, _count, recs = built
    masters = [p.rstrip(b'\0').decode() for t, p in recs[('TES4', 0)]
               if t == 'MAST']
    assert masters == ['Skyrim.esm']


def test_hedr_count_matches_contents(built):
    """HEDR must count records + GRUPs. The engine walks the file by this
    number, so an undercount silently drops records."""
    data, count, recs = built
    hedr = dict(recs[('TES4', 0)])['HEDR']
    assert struct.unpack('<I', hedr[4:8])[0] == count
    # 4 GLOB + 1 MESG + 1 QUST + 3 top-level GRUPs
    assert count == 9


def test_quest_is_start_game_enabled_and_journal_invisible(built):
    """StartGameEnabled|StartsEnabled, and type 0 so a control quest with no
    stages never shows up in the player's journal."""
    _data, _count, recs = built
    dnam = dict(recs[('QUST', FID_QUST)])['DNAM']
    flags, priority, _formver, _unknown, qtype = struct.unpack('<HBBII', dnam)
    assert flags == SGE_FLAGS == 0x0011
    assert qtype == QUEST_TYPE_NONE
    assert priority == 0


def test_button_order_matches_game_ids(built):
    """Buttons must be declared in GAME_* order: a hidden button does not
    renumber the rest, so Show()'s return value IS the game id. Reordering
    these without editing the script starts the wrong game."""
    _data, _count, recs = built
    subs = recs[('MESG', FID_MESG)]
    texts = [p.rstrip(b'\0').decode() for t, p in subs if t == 'ITXT']
    assert texts[0].startswith('Skyrim')
    assert texts[1].startswith('Cyrodiil')
    assert texts[2].startswith('Nehrim')
    assert texts[3].startswith('Vvardenfell')
    assert len(texts) == len(BUTTONS) == 4


def test_skyrim_button_is_unconditional_others_are_gated(built):
    """Skyrim's button carries no condition so the menu can never be drawn
    empty (vanilla dunMiddenNamesMenuMSG does the same with its final button);
    each converted game's button is gated on its own global."""
    _data, _count, recs = built
    subs = recs[('MESG', FID_MESG)]

    # Walk ITXT/CTDA in order, attributing each CTDA to the ITXT before it.
    conds = {}
    idx = -1
    for stype, payload in subs:
        if stype == 'ITXT':
            idx += 1
        elif stype == 'CTDA':
            func = struct.unpack_from('<H', payload, 8)[0]
            param1 = struct.unpack_from('<I', payload, 12)[0]
            comp = struct.unpack_from('<f', payload, 4)[0]
            assert func == FUNC_GET_GLOBAL_VALUE
            assert comp == 1.0
            conds.setdefault(idx, []).append(param1)

    assert 0 not in conds, 'Skyrim button must stay unconditional'
    assert conds[1] == [FID_GLOB_OBLIVION]
    assert conds[2] == [FID_GLOB_NEHRIM]
    assert conds[3] == [FID_GLOB_MORROBLIVION]


def test_mesg_is_a_message_box(built):
    """DNAM bit 0 = Message Box. Without it the record renders as a corner
    notification with no buttons and Show() can never return a choice."""
    _data, _count, recs = built
    dnam = dict(recs[('MESG', FID_MESG)])['DNAM']
    assert struct.unpack('<I', dnam)[0] & 0x01


def test_vmad_binds_every_script_property(built):
    """The VMAD property names must match the .psc exactly — a typo binds
    silently to nothing and the menu never appears."""
    _data, _count, recs = built
    vmad = dict(recs[('QUST', FID_QUST)])['VMAD']
    version, obj_format, script_count = struct.unpack_from('<HHH', vmad, 0)
    assert (version, obj_format, script_count) == (5, 2, 1)

    pos = 6
    name_len = struct.unpack_from('<H', vmad, pos)[0]
    name = vmad[pos + 2:pos + 2 + name_len].decode()
    assert name == SCRIPT_NAME
    pos += 2 + name_len + 1                       # + flags byte
    prop_count = struct.unpack_from('<H', vmad, pos)[0]
    pos += 2

    props = {}
    for _ in range(prop_count):
        plen = struct.unpack_from('<H', vmad, pos)[0]
        pname = vmad[pos + 2:pos + 2 + plen].decode()
        pos += 2 + plen
        ptype = vmad[pos]
        pos += 2                                  # type + status
        assert ptype == 1, 'all properties are Object-typed'
        _unused, alias, fid = struct.unpack_from('<HhI', vmad, pos)
        pos += 8
        assert alias == -1
        props[pname] = fid

    assert props == {
        'GameSelectMenu': FID_MESG,
        'HasSkyrim': GLOBALS[0][0],
        'HasOblivion': FID_GLOB_OBLIVION,
        'HasNehrim': FID_GLOB_NEHRIM,
        'HasMorroblivion': FID_GLOB_MORROBLIVION,
    }
    assert pos == len(vmad), 'VMAD must be fully consumed'


def test_globals_are_short_typed_and_zeroed(built):
    """Short-typed, value 0: the script sets them during detection, and a
    stale 1 from authoring would offer a game that is not installed."""
    _data, _count, recs = built
    for fid, edid in GLOBALS:
        subs = dict(recs[('GLOB', fid)])
        assert subs['EDID'].rstrip(b'\0').decode() == edid
        assert subs['FNAM'] == b's'
        assert struct.unpack('<f', subs['FLTV'])[0] == 0.0


def test_script_source_declares_matching_game_constants():
    """The .psc GAME_* ids must line up with the MESG button order; they are
    the mapping from a clicked button to the game that gets started."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    psc = os.path.join(root, 'TESGameSelect', 'scripts', 'source',
                       SCRIPT_NAME + '.psc')
    text = open(psc, encoding='utf-8').read()
    for name, value in [('GAME_SKYRIM', 0), ('GAME_OBLIVION', 1),
                        ('GAME_NEHRIM', 2), ('GAME_MORROBLIVION', 3)]:
        assert f'Property {name}' in text
        line = next(ln for ln in text.splitlines() if f'Property {name}' in ln)
        assert f'= {value}' in line, f'{name} must equal {value}'
