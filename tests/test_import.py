"""
Tests for TES5 import - verifies binary output is correctly structured.

Tests the writer, record converters, and group hierarchy.
"""

import os
import struct
import tempfile

import pytest

from tes5_import.record_types.actors import (
    convert_CREA,
    convert_GLOB,
    convert_LVLC,
    convert_LVLI,
    convert_NPC_,
)
from tes5_import.record_types.common import _convert_biped_flags
from tes5_import.record_types.equipment import (
    convert_ARMO,
    convert_BOOK,
    convert_CLOT,
    convert_ENCH,
    convert_WEAP,
)
from tes5_import.record_types.items import (
    convert_CONT,
    convert_DOOR,
    convert_LIGH,
    convert_MISC,
    convert_STAT,
)
from tes5_import.record_types.world import (
    convert_CELL,
    convert_LAND,
    convert_REFR,
)
from tes5_import.text_reader import (
    get_formid,
    get_int,
    parse_record_block,
    unescape_value,
)
from tes5_import.writer import (
    FORM_VERSION_SSE,
    GROUP_HEADER_SIZE,
    RECORD_HEADER_SIZE,
    PluginWriter,
    pack_obnd,
    pack_record,
    pack_string_subrecord,
    pack_subrecord,
    pack_tes4_header,
    pack_top_group,
)

# ---------------------------------------------------------------------------
# Writer tests
# ---------------------------------------------------------------------------

class TestWriter:
    def test_subrecord_packing(self):
        data = pack_subrecord('EDID', b'TestEdid\x00')
        assert data[:4] == b'EDID'
        size = struct.unpack_from('<H', data, 4)[0]
        assert size == 9
        assert data[6:] == b'TestEdid\x00'

    def test_string_subrecord(self):
        data = pack_string_subrecord('FULL', 'Iron Sword')
        assert data[:4] == b'FULL'
        size = struct.unpack_from('<H', data, 4)[0]
        assert data[6:6 + size] == b'Iron Sword\x00'

    def test_record_packing(self):
        subs = pack_string_subrecord('EDID', 'Test')
        rec = pack_record('STAT', 0x12345, 0, subs)
        assert rec[:4] == b'STAT'
        data_size = struct.unpack_from('<I', rec, 4)[0]
        assert data_size == len(subs)
        flags = struct.unpack_from('<I', rec, 8)[0]
        assert flags == 0
        form_id = struct.unpack_from('<I', rec, 12)[0]
        assert form_id == 0x12345
        form_ver = struct.unpack_from('<H', rec, 20)[0]
        assert form_ver == FORM_VERSION_SSE  # 44

    def test_record_header_size(self):
        subs = b''
        rec = pack_record('GLOB', 1, 0, subs)
        assert len(rec) == RECORD_HEADER_SIZE  # 24 bytes for empty record

    def test_group_packing(self):
        content = pack_record('STAT', 1, 0, b'')
        group = pack_top_group('STAT', content)
        assert group[:4] == b'GRUP'
        group_size = struct.unpack_from('<I', group, 4)[0]
        assert group_size == GROUP_HEADER_SIZE + len(content)
        label = group[8:12]
        assert label == b'STAT'
        group_type = struct.unpack_from('<i', group, 12)[0]
        assert group_type == 0

    def test_obnd(self):
        data = pack_obnd()
        assert data[:4] == b'OBND'
        size = struct.unpack_from('<H', data, 4)[0]
        assert size == 12
        # All zeros
        for i in range(6):
            val = struct.unpack_from('<h', data, 6 + i * 2)[0]
            assert val == 0

    def test_obnd_custom_bounds(self):
        data = pack_obnd(-50, -50, 0, 50, 50, 80)
        vals = struct.unpack_from('<6h', data, 6)
        assert vals == (-50, -50, 0, 50, 50, 80)

    def test_obnd_size_is_always_12(self):
        data = pack_obnd(-3, -3, 0, 3, 3, 3)
        assert struct.unpack_from('<H', data, 4)[0] == 12

    def test_tes4_header(self):
        header = pack_tes4_header(['Skyrim.esm'], num_records=100, next_object_id=0x800)
        assert header[:4] == b'TES4'
        form_ver = struct.unpack_from('<H', header, 20)[0]
        assert form_ver == 44
        # Data should contain HEDR
        assert b'HEDR' in header
        assert b'MAST' in header
        # Check HEDR version
        hedr_pos = header.index(b'HEDR')
        ver = struct.unpack_from('<f', header, hedr_pos + 6)[0]
        assert abs(ver - 1.71) < 0.01

    def test_plugin_writer(self):
        w = PluginWriter(masters=['Skyrim.esm'])
        subs = pack_string_subrecord('EDID', 'TestStat')
        rec = pack_record('STAT', 0x800, 0, subs)
        w.add_record('STAT', rec)

        with tempfile.NamedTemporaryFile(suffix='.esm', delete=False) as f:
            tmp = f.name
        try:
            w.write(tmp)
            assert os.path.getsize(tmp) > 0

            # Read back and verify structure
            with open(tmp, 'rb') as f:
                data = f.read()
            assert data[:4] == b'TES4'  # File header
            # Find GRUP
            grup_pos = data.index(b'GRUP')
            assert grup_pos > 0
            label = data[grup_pos + 8:grup_pos + 12]
            assert label == b'STAT'
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# Text reader tests
# ---------------------------------------------------------------------------

class TestTextReader:
    def test_unescape(self):
        assert unescape_value('Hello\\nWorld') == 'Hello\nWorld'
        assert unescape_value('Path\\\\File') == 'Path\\File'
        assert unescape_value('Tab\\there') == 'Tab\there'

    def test_parse_record_block(self):
        lines = [
            'Signature=STAT',
            'FormID=00012345',
            'EditorID=TestRock',
            'RecordFlags=0',
            'Model.MODL=Rocks\\\\Rock01.nif',  # Double-escaped in export format
        ]
        rec = parse_record_block(lines)
        assert rec['Signature'] == 'STAT'
        assert rec['FormID'] == '00012345'
        assert rec['EditorID'] == 'TestRock'
        assert rec['Model.MODL'] == 'Rocks\\Rock01.nif'

    def test_get_formid(self):
        rec = {'FormID': '0001A2B3'}
        assert get_formid(rec, 'FormID') == 0x0001A2B3

    def test_get_int(self):
        rec = {'DATA.Value': '42'}
        assert get_int(rec, 'DATA.Value') == 42
        assert get_int(rec, 'Missing', 0) == 0


# ---------------------------------------------------------------------------
# Record converter tests
# ---------------------------------------------------------------------------

class TestConverters:
    """Test individual record converters produce valid binary."""

    def _check_record(self, rec_bytes: bytes, expected_sig: str):
        """Basic validation of a packed record."""
        assert len(rec_bytes) >= RECORD_HEADER_SIZE
        sig = rec_bytes[:4].decode('ascii')
        assert sig == expected_sig
        data_size = struct.unpack_from('<I', rec_bytes, 4)[0]
        assert len(rec_bytes) == RECORD_HEADER_SIZE + data_size
        form_ver = struct.unpack_from('<H', rec_bytes, 20)[0]
        assert form_ver == FORM_VERSION_SSE

    def _has_subrecord(self, rec_bytes: bytes, sub_sig: str) -> bool:
        """Check if a subrecord signature exists in the record."""
        data = rec_bytes[RECORD_HEADER_SIZE:]
        target = sub_sig.encode('ascii')
        return target in data

    def _get_subrecord_data(self, rec_bytes: bytes, sub_sig: str) -> bytes:
        """Extract a subrecord's data from a packed record."""
        data = rec_bytes[RECORD_HEADER_SIZE:]
        target = sub_sig.encode('ascii')
        pos = 0
        while pos + 6 <= len(data):
            sig = data[pos:pos + 4]
            size = struct.unpack_from('<H', data, pos + 4)[0]
            if sig == target:
                return data[pos + 6:pos + 6 + size]
            pos += 6 + size
        return None

    def test_stat(self):
        rec = {'Signature': 'STAT', 'FormID': '00012345', 'RecordFlags': '0',
               'EditorID': 'TestRock', 'Model.MODL': 'Rocks\\Rock01.nif'}
        result = convert_STAT(rec)
        self._check_record(result, 'STAT')
        assert self._has_subrecord(result, 'EDID')
        assert self._has_subrecord(result, 'OBND')
        obnd = self._get_subrecord_data(result, 'OBND')
        assert len(obnd) == 12
        # STAT should use STAT-specific bounds (-50,-50,0,50,50,80)
        vals = struct.unpack_from('<6h', obnd, 0)
        assert vals == (-50, -50, 0, 50, 50, 80)

    def test_misc(self):
        rec = {'Signature': 'MISC', 'FormID': '00000100', 'RecordFlags': '0',
               'EditorID': 'Gold001', 'FULL': 'Gold', 'DATA.Value': '1',
               'DATA.Weight': '0.0', 'Model.MODL': 'Gold\\Gold.nif'}
        result = convert_MISC(rec)
        self._check_record(result, 'MISC')
        assert self._has_subrecord(result, 'DATA')
        data = self._get_subrecord_data(result, 'DATA')
        assert len(data) == 8  # int32 + float32
        value = struct.unpack_from('<I', data, 0)[0]
        assert value == 1
        # MISC should use MISC-specific OBND (-5,-5,0,5,5,8)
        obnd = self._get_subrecord_data(result, 'OBND')
        assert struct.unpack_from('<6h', obnd, 0) == (-5, -5, 0, 5, 5, 8)

    def test_weap(self):
        rec = {'Signature': 'WEAP', 'FormID': '00000200', 'RecordFlags': '0',
               'EditorID': 'IronSword', 'FULL': 'Iron Sword',
               'DATA.Type': '0', 'DATA.Speed': '1.0', 'DATA.Reach': '1.0',
               'DATA.Value': '25', 'DATA.Health': '100', 'DATA.Weight': '12.0',
               'DATA.Damage': '8', 'Model.MODL': 'Weapons\\Iron\\Sword.nif'}
        result = convert_WEAP(rec)
        self._check_record(result, 'WEAP')
        assert self._has_subrecord(result, 'DNAM')
        assert self._has_subrecord(result, 'CRDT')
        data = self._get_subrecord_data(result, 'DATA')
        # TES5 WEAP DATA: Value(4) + Weight(4) + Damage(2) = 10 bytes
        assert len(data) == 10
        damage = struct.unpack_from('<H', data, 8)[0]
        assert damage == 8
        # WEAP should use WEAP-specific OBND (-5,-5,0,5,5,30)
        obnd = self._get_subrecord_data(result, 'OBND')
        assert struct.unpack_from('<6h', obnd, 0) == (-5, -5, 0, 5, 5, 30)

    def test_armo(self):
        rec = {'Signature': 'ARMO', 'FormID': '00000300', 'RecordFlags': '0',
               'EditorID': 'IronArmor', 'FULL': 'Iron Armor',
               'BMDT.BipedFlags': '4', 'BMDT.GeneralFlags': '0',
               'DATA.ArmorRating': '20', 'DATA.Value': '100',
               'DATA.Weight': '30.0',
               'Male.BipedModel.MODL': 'Armor\\Iron\\M.nif'}
        result = convert_ARMO(rec)
        self._check_record(result, 'ARMO')
        assert self._has_subrecord(result, 'BOD2')
        assert self._has_subrecord(result, 'RNAM')
        bod2 = self._get_subrecord_data(result, 'BOD2')
        assert len(bod2) == 8

    def test_clot_becomes_armo(self):
        rec = {'Signature': 'CLOT', 'FormID': '00000400', 'RecordFlags': '0',
               'EditorID': 'FineShirt', 'FULL': 'Fine Shirt',
               'BMDT.BipedFlags': '4', 'BMDT.GeneralFlags': '0',
               'DATA.Value': '10', 'DATA.Weight': '1.0'}
        result = convert_CLOT(rec)
        # Should be ARMO record
        sig = result[:4].decode('ascii')
        assert sig == 'ARMO'

    def test_biped_slot_mapping(self):
        # _convert_biped_flags returns PRIMARY equip slots plus equipment-conflict
        # extras (helmets block Circlet/Ears so they can't be worn simultaneously).
        # Body-coverage extras (ForeArms, Calves, etc.) go on ARMA, not ARMO.
        # Head (bit 0) â†’ 30 (bit 0) + Hair(1) + LongHair(11) + Circlet(12) + Ears(13)
        # - full-face helm.  LongHair(41) also covered so the hairline headpart
        # (partition 141) is hidden, not just swapped in (see BIPED_SLOT_EXTRA).
        assert _convert_biped_flags(0x01) == (1 | (1 << 1) | (1 << 11) | (1 << 12) | (1 << 13))
        # Hair (bit 1) â†’ 31 (bit 1) + LongHair(11) + Circlet(12) - open-face helm
        assert _convert_biped_flags(0x02) == ((1 << 1) | (1 << 11) | (1 << 12))
        # Upper body (bit 2) â†’ 32 (bit 2) - no extra equipment conflicts
        assert _convert_biped_flags(0x04) == 0x04
        # Lower body (bit 3) â†’ 44-LowerBody (bit 14)
        assert _convert_biped_flags(0x08) == (1 << 14)
        # Hand (bit 4) â†’ 33-Hands (bit 3)
        assert _convert_biped_flags(0x10) == (1 << 3)
        # Foot (bit 5) â†’ 37 (bit 7)
        assert _convert_biped_flags(0x20) == 0x80
        # Amulet (bit 8) â†’ 35 (bit 5)
        assert _convert_biped_flags(0x100) == 0x20
        # Shield (bit 13) â†’ 39 (bit 9)
        assert _convert_biped_flags(0x2000) == 0x200
        # Upper+Lower body combined
        assert _convert_biped_flags(0x0C) == 0x04 | (1 << 14)

    def test_armo_armor_type_enum(self):
        """ArmorType: 0=Light, 1=Heavy, 2=Clothing per wbArmorTypeEnum."""
        rec = {'Signature': 'ARMO', 'FormID': '00000300', 'RecordFlags': '0',
               'EditorID': 'TestArmor', 'FULL': 'Test',
               'BMDT.BipedFlags': '4', 'BMDT.GeneralFlags': '0',
               'DATA.ArmorRating': '10', 'DATA.Value': '1', 'DATA.Weight': '1.0',
               'Male.BipedModel.MODL': 'Armor\\Test\\M.nif'}
        # Light armor (gen_flags=0, no heavy bit)
        result = convert_ARMO(rec)
        bod2 = self._get_subrecord_data(result, 'BOD2')
        armor_type = struct.unpack_from('<I', bod2, 4)[0]
        assert armor_type == 0, f"Light armor type should be 0, got {armor_type}"
        # Heavy armor (gen_flags bit 7 = 0x80 set, per wbDefinitionsTES4.pas)
        rec['BMDT.GeneralFlags'] = '128'
        result = convert_ARMO(rec)
        bod2 = self._get_subrecord_data(result, 'BOD2')
        armor_type = struct.unpack_from('<I', bod2, 4)[0]
        assert armor_type == 1, f"Heavy armor type should be 1, got {armor_type}"
        # Clothing
        result = convert_ARMO(rec, is_clothing=True)
        bod2 = self._get_subrecord_data(result, 'BOD2')
        armor_type = struct.unpack_from('<I', bod2, 4)[0]
        assert armor_type == 2, f"Clothing type should be 2, got {armor_type}"

    def test_armo_dnam_is_int(self):
        """ARMO DNAM should be S32 = rating."""
        rec = {'Signature': 'ARMO', 'FormID': '00000300', 'RecordFlags': '0',
               'EditorID': 'TestArmor', 'FULL': 'Test',
               'BMDT.BipedFlags': '4', 'BMDT.GeneralFlags': '0',
               'DATA.ArmorRating': '20', 'DATA.Value': '100', 'DATA.Weight': '30.0',
               'Male.BipedModel.MODL': 'Armor\\Test\\M.nif'}
        result = convert_ARMO(rec)
        dnam = self._get_subrecord_data(result, 'DNAM')
        assert len(dnam) == 4
        val = struct.unpack('<i', dnam)[0]
        assert val == 20, f"Expected 20, got {val}"

    def test_weap_crdt_sse_size(self):
        """WEAP CRDT must be 24 bytes for SSE (form version 44)."""
        rec = {'Signature': 'WEAP', 'FormID': '00000200', 'RecordFlags': '0',
               'EditorID': 'TestSword', 'FULL': 'Test',
               'DATA.Type': '0', 'DATA.Speed': '1.0', 'DATA.Reach': '1.0',
               'DATA.Value': '25', 'DATA.Weight': '12.0', 'DATA.Damage': '8',
               'Model.MODL': 'Weapons\\Test\\Sword.nif'}
        result = convert_WEAP(rec)
        crdt = self._get_subrecord_data(result, 'CRDT')
        assert len(crdt) == 24, f"SSE CRDT should be 24 bytes, got {len(crdt)}"

    def test_weap_dnam_defaults(self):
        """WEAP DNAM should have sensible defaults for VATS hit chance and attack mult."""
        rec = {'Signature': 'WEAP', 'FormID': '00000200', 'RecordFlags': '0',
               'EditorID': 'TestSword', 'FULL': 'Test',
               'DATA.Type': '0', 'DATA.Speed': '1.0', 'DATA.Reach': '1.0',
               'DATA.Value': '25', 'DATA.Weight': '12.0', 'DATA.Damage': '8',
               'Model.MODL': 'Weapons\\Test\\Sword.nif'}
        result = convert_WEAP(rec)
        dnam = self._get_subrecord_data(result, 'DNAM')
        assert len(dnam) == 100
        vats_chance = dnam[24]  # Base VATS To-Hit Chance at offset 24
        assert vats_chance == 0  # Vanilla Skyrim 1H melee weapons have 0 here
        attack_mult = struct.unpack_from('<f', dnam, 44)[0]  # Animation Attack Mult
        assert attack_mult == 1.0

    def test_arma_generation(self):
        """ARMO with writer should generate a companion ARMA record."""
        from tes5_import.writer import PluginWriter
        rec = {'Signature': 'ARMO', 'FormID': '00000300', 'RecordFlags': '0',
               'EditorID': 'IronArmor', 'FULL': 'Iron Armor',
               'BMDT.BipedFlags': '4', 'BMDT.GeneralFlags': '0',
               'DATA.ArmorRating': '20', 'DATA.Value': '100', 'DATA.Weight': '30.0',
               'Male.BipedModel.MODL': 'Armor\\Iron\\M.nif'}
        writer = PluginWriter(masters=['Skyrim.esm'])
        writer.next_object_id = 0x01002000
        result = convert_ARMO(rec, writer=writer)
        # ARMO should have MODL subrecord referencing the ARMA
        assert self._has_subrecord(result, 'MODL')
        modl_data = self._get_subrecord_data(result, 'MODL')
        arma_ref = struct.unpack('<I', modl_data)[0]
        assert arma_ref == 0x01002000
        # Writer should have an ARMA record
        assert 'ARMA' in writer._top_groups
        assert len(writer._top_groups['ARMA']) == 1

    def test_arma_generation_female_only(self):
        """A wearable authored for one gender only must still get an armature.

        Nehrim ships 5 (IrlandaRobe, the Silverlight set): the male fields are
        empty, the ARMA build was gated on the male model, and an ARMO with no
        ARMA equips but draws nothing — Goddess Irlanda and the MQ34 Eliath
        embodiment stood there naked. Vanilla Skyrim.esm has 4 ARMA carrying
        MOD3 only and 0 carrying neither, so female-only is legal and empty is
        not: MOD3 must be written and MOD2 must NOT be invented.
        """
        from tes5_import.writer import PluginWriter
        rec = {'Signature': 'ARMO', 'FormID': '00000301', 'RecordFlags': '0',
               'EditorID': 'IrlandaRobe', 'FULL': 'Robe',
               'BMDT.BipedFlags': '12', 'BMDT.GeneralFlags': '0',
               'DATA.ArmorRating': '0', 'DATA.Value': '10', 'DATA.Weight': '2.0',
               'Female.BipedModel.MODL': 'OE\\Armor\\Robes\\RobeBanshee2.nif'}
        writer = PluginWriter(masters=['Skyrim.esm'])
        writer.next_object_id = 0x01002000
        result = convert_ARMO(rec, writer=writer)

        assert self._has_subrecord(result, 'MODL'), 'no armature generated'
        assert len(writer._top_groups.get('ARMA', [])) == 1
        arma = writer._top_groups['ARMA'][0]
        assert b'MOD3' in arma, 'female model missing from the armature'
        assert b'MOD2' not in arma, 'invented a male model vanilla does not have'
        # The dropped-item model has to fall back across genders too, or the
        # item is invisible on the ground and in the inventory viewer.
        assert self._has_subrecord(result, 'MOD2')

    def test_npc(self):
        rec = {'Signature': 'NPC_', 'FormID': '00000500', 'RecordFlags': '0',
               'EditorID': 'TestNPC', 'FULL': 'Test Guard',
               'ACBS.Flags': '0', 'ACBS.Level': '5', 'ACBS.CalcMin': '5',
               'ACBS.CalcMax': '10', 'ACBS.BarterGold': '0',
               'ACBS.SpellPoints': '0', 'ACBS.Fatigue': '0',
               'FactionCount': '0', 'SpellCount': '0', 'ItemCount': '0',
               'AIPackageCount': '0',
               'AIDT.Aggression': '5', 'AIDT.Confidence': '50',
               'AIDT.Responsibility': '50', 'AIDT.Services': '0',
               'DATA.Health': '50', 'DATA.Intelligence': '40',
               'DATA.Strength': '60', 'DATA.Endurance': '45',
               'HCLR.R': '100', 'HCLR.G': '80', 'HCLR.B': '60'}
        result = convert_NPC_(rec)
        self._check_record(result, 'NPC_')
        assert self._has_subrecord(result, 'ACBS')
        assert self._has_subrecord(result, 'RNAM')
        assert self._has_subrecord(result, 'DATA')
        assert self._has_subrecord(result, 'DNAM')
        # NPC_ DATA should be empty (0 bytes) in TES5
        data = self._get_subrecord_data(result, 'DATA')
        assert len(data) == 0
        # DNAM should be 52 bytes
        dnam = self._get_subrecord_data(result, 'DNAM')
        assert len(dnam) == 52

    def test_npc_skin_tone_tint_layer(self):
        """NPCs must carry a skin-tone tint layer (TINI/TINC/TINV/TIAS) and a
        matching QNAM — without them the engine renders the body pale white
        regardless of race."""
        base = {'Signature': 'NPC_', 'FormID': '00000500', 'RecordFlags': '0',
                'EditorID': 'TestNPC', 'ACBS.Flags': '0', 'ACBS.Level': '5',
                'FactionCount': '0', 'SpellCount': '0', 'ItemCount': '0',
                'AIPackageCount': '0', 'AIDT.Services': '0',
                'HCLR.R': '100', 'HCLR.G': '80', 'HCLR.B': '60'}
        # Redguard male (TES4 race fid 0x00000D43)
        rec = dict(base, **{'RNAM.Race': '00000D43'})
        result = convert_NPC_(rec)
        for sig in ('TINI', 'TINC', 'TINV', 'TIAS'):
            assert self._has_subrecord(result, sig), f'missing {sig}'
        tini = struct.unpack('<H', self._get_subrecord_data(result, 'TINI'))[0]
        assert tini == 1  # Redguard male skin-tone index in Skyrim.esm
        r, g, b, a = struct.unpack('<4B', self._get_subrecord_data(result, 'TINC'))
        # Must be one of the census colors — all dark Redguard tones, never white
        assert (r, g, b) in {(45, 33, 30), (53, 39, 34), (79, 69, 64)}
        assert a == 0
        tinv = struct.unpack('<I', self._get_subrecord_data(result, 'TINV'))[0]
        assert tinv == 100
        tias = struct.unpack('<h', self._get_subrecord_data(result, 'TIAS'))[0]
        assert tias == -1
        # QNAM must agree with the tint (tinv=100 → exactly color/255)
        qnam = struct.unpack('<3f', self._get_subrecord_data(result, 'QNAM'))
        for got, want in zip(qnam, (r / 255.0, g / 255.0, b / 255.0)):
            assert abs(got - want) < 1e-6
        # Female Nord uses the FEMALE tint list index (24, not male 1)
        rec_f = dict(base, **{'RNAM.Race': '000224FD', 'ACBS.Flags': '1'})
        result_f = convert_NPC_(rec_f)
        tini_f = struct.unpack('<H', self._get_subrecord_data(result_f, 'TINI'))[0]
        assert tini_f == 24
        # Deterministic: same FormID → same pick
        assert (self._get_subrecord_data(result_f, 'TINC')
                == self._get_subrecord_data(convert_NPC_(rec_f), 'TINC'))

    def test_crea_becomes_npc(self):
        rec = {'Signature': 'CREA', 'FormID': '00000600', 'RecordFlags': '0',
               'EditorID': 'TestDeer', 'FULL': 'Deer',
               'ACBS.Flags': '0', 'ACBS.Level': '3', 'ACBS.CalcMin': '1',
               'ACBS.CalcMax': '10', 'ACBS.BarterGold': '0',
               'FactionCount': '0', 'ItemCount': '0', 'AIPackageCount': '0',
               'AIDT.Aggression': '0', 'AIDT.Confidence': '30',
               'AIDT.Services': '0',
               'DATA.CombatSkill': '15', 'DATA.MagicSkill': '0',
               'DATA.StealthSkill': '20', 'DATA.Health': '20',
               'DATA.Strength': '30', 'DATA.Intelligence': '10'}
        result = convert_CREA(rec)
        sig = result[:4].decode('ascii')
        assert sig == 'NPC_'

    def test_ench(self):
        rec = {'Signature': 'ENCH', 'FormID': '00000700', 'RecordFlags': '0',
               'EditorID': 'TestEnch', 'ENIT.Type': '2', 'ENIT.Charge': '100',
               'ENIT.Cost': '50', 'ENIT.Flags': '0', 'EffectCount': '1',
               'Effect[0].Magnitude': '10', 'Effect[0].Area': '0',
               'Effect[0].Duration': '60', 'Effect[0].Type': 'Target'}
        result = convert_ENCH(rec)
        self._check_record(result, 'ENCH')
        enit = self._get_subrecord_data(result, 'ENIT')
        assert len(enit) == 36
        cast_type, = struct.unpack_from('<I', enit, 8)
        enchant_type, = struct.unpack_from('<I', enit, 20)
        # TES4 ENIT.Type=2 (Weapon) -> TES5 CastType=1 (Fire and Forget),
        # not 2 (Concentration) -- wbCastEnum in wbDefinitionsTES5.pas.
        assert cast_type == 1
        assert enchant_type == 6  # Enchantment

    def test_ench_staff_cast_type(self):
        rec = {'Signature': 'ENCH', 'FormID': '00000701', 'RecordFlags': '0',
               'EditorID': 'TestStaffEnch', 'ENIT.Type': '1', 'ENIT.Charge': '100',
               'ENIT.Cost': '50', 'ENIT.Flags': '0', 'EffectCount': '1',
               'Effect[0].Magnitude': '10', 'Effect[0].Area': '0',
               'Effect[0].Duration': '60', 'Effect[0].Type': 'Target'}
        result = convert_ENCH(rec)
        enit = self._get_subrecord_data(result, 'ENIT')
        cast_type, = struct.unpack_from('<I', enit, 8)
        enchant_type, = struct.unpack_from('<I', enit, 20)
        assert cast_type == 1  # Fire and Forget
        assert enchant_type == 12  # Staff Enchantment

    def test_ench_apparel_cast_type(self):
        rec = {'Signature': 'ENCH', 'FormID': '00000702', 'RecordFlags': '0',
               'EditorID': 'TestApparelEnch', 'ENIT.Type': '3', 'ENIT.Charge': '100',
               'ENIT.Cost': '50', 'ENIT.Flags': '0', 'EffectCount': '1',
               'Effect[0].Magnitude': '10', 'Effect[0].Area': '0',
               'Effect[0].Duration': '60', 'Effect[0].Type': 'Self'}
        result = convert_ENCH(rec)
        enit = self._get_subrecord_data(result, 'ENIT')
        cast_type, = struct.unpack_from('<I', enit, 8)
        assert cast_type == 0  # Constant Effect

    def test_cell(self):
        rec = {'Signature': 'CELL', 'FormID': '00000800', 'RecordFlags': '0',
               'EditorID': 'TestCell', 'FULL': 'Test Interior',
               'DATA.Flags': '1'}  # Is Interior
        result = convert_CELL(rec)
        self._check_record(result, 'CELL')
        data = self._get_subrecord_data(result, 'DATA')
        assert len(data) == 2  # uint16 in TES5

    def test_refr(self):
        rec = {'Signature': 'REFR', 'FormID': '00001000', 'RecordFlags': '0',
               'NAME': '00012345',
               'PosX': '100.0', 'PosY': '200.0', 'PosZ': '0.0',
               'RotX': '0.0', 'RotY': '0.0', 'RotZ': '1.57'}
        result = convert_REFR(rec)
        self._check_record(result, 'REFR')
        assert self._has_subrecord(result, 'NAME')
        assert self._has_subrecord(result, 'DATA')
        data = self._get_subrecord_data(result, 'DATA')
        assert len(data) == 24  # 6 floats

    def test_land(self):
        # Minimal LAND record
        vhgt = b'\x00' * 1093  # Standard VHGT size
        rec = {'Signature': 'LAND', 'FormID': '00002000', 'RecordFlags': '0',
               'DATA.Flags': '1',
               'VHGT': vhgt.hex().upper(),
               'LayerCount': '0', 'VTEXCount': '0'}
        result = convert_LAND(rec)
        self._check_record(result, 'LAND')
        assert self._has_subrecord(result, 'VHGT')

    def test_glob(self):
        rec = {'Signature': 'GLOB', 'FormID': '00003000', 'RecordFlags': '0',
               'EditorID': 'TES4Fame', 'FNAM.Type': 'f', 'FLTV.Value': '12.0'}
        result = convert_GLOB(rec)
        self._check_record(result, 'GLOB')
        fnam = self._get_subrecord_data(result, 'FNAM')
        assert fnam == bytes([ord('f')])

    def test_glob_engine_time_global_dropped(self):
        """GameHour etc. collide with Skyrim engine globals; script references
        are canonicalized to the vanilla forms, so our copy must not be
        emitted."""
        rec = {'Signature': 'GLOB', 'FormID': '00003000', 'RecordFlags': '0',
               'EditorID': 'GameHour', 'FNAM.Type': 'f', 'FLTV.Value': '12.0'}
        assert convert_GLOB(rec) == b''

    def test_lvli(self):
        rec = {'Signature': 'LVLI', 'FormID': '00004000', 'RecordFlags': '0',
               'EditorID': 'TestLvlList', 'LVLD.ChanceNone': '0', 'LVLF.Flags': '1',
               'EntryCount': '2',
               'Entry[0].Level': '1', 'Entry[0].FormID': '00000100', 'Entry[0].Count': '1',
               'Entry[1].Level': '5', 'Entry[1].FormID': '00000200', 'Entry[1].Count': '2'}
        result = convert_LVLI(rec)
        self._check_record(result, 'LVLI')
        assert self._has_subrecord(result, 'LLCT')
        assert self._has_subrecord(result, 'LVLO')

    def test_lvlc_becomes_lvln(self):
        rec = {'Signature': 'LVLC', 'FormID': '00005000', 'RecordFlags': '0',
               'EditorID': 'TestLvlCrea', 'LVLD.ChanceNone': '0', 'LVLF.Flags': '0',
               'EntryCount': '1',
               'Entry[0].Level': '1', 'Entry[0].FormID': '00000600', 'Entry[0].Count': '1'}
        result = convert_LVLC(rec)
        sig = result[:4].decode('ascii')
        assert sig == 'LVLN'

    def test_book(self):
        rec = {'Signature': 'BOOK', 'FormID': '00006000', 'RecordFlags': '0',
               'EditorID': 'TestBook', 'FULL': 'A Test Book',
               'DESC': 'Once upon a time...', 'DATA.Flags': '0',
               'DATA.Teaches': '255', 'DATA.Value': '5', 'DATA.Weight': '1.0',
               'Model.MODL': 'Books\\TestBook.nif'}
        result = convert_BOOK(rec)
        self._check_record(result, 'BOOK')
        assert self._has_subrecord(result, 'DESC')
        # INAM (Inventory Art STAT) must always be present — BookMenu
        # null-derefs (in-game crash) when a book without INAM is read.
        assert self._has_subrecord(result, 'INAM'), "BOOK must have INAM"
        inam_data = self._get_subrecord_data(result, 'INAM')
        assert len(inam_data) == 4, "INAM must be a 4-byte FormID"
        assert struct.unpack_from('<I', inam_data)[0] != 0, "INAM must not be null"
        assert self._has_subrecord(result, 'CNAM'), "BOOK must have CNAM"
        cnam_data = self._get_subrecord_data(result, 'CNAM')
        assert cnam_data == b'\x00', "CNAM must be an empty description string"

    def test_book_inventory_art_uses_generated_rig(self):
        """With a writer, INAM must reference a companion STAT pointing at the
        generated reading-rig mesh (meshes\\tes4\\clutter\\books\\inv\\
        <model basename>.nif, built by asset_convert/book_inam.py), and books
        sharing the same TES4 model must share one STAT."""
        class _FakeWriter:
            def __init__(self):
                self.records = []
                self._next = 0x01000000
            def alloc_formid(self):
                self._next += 1
                return self._next
            def add_record(self, sig, data):
                self.records.append((sig, data))
        w = _FakeWriter()
        rec = {'Signature': 'BOOK', 'FormID': '00006000', 'RecordFlags': '0',
               'EditorID': 'TestBook', 'FULL': 'A Test Book',
               'DESC': 'Once upon a time...', 'DATA.Flags': '0',
               'DATA.Teaches': '255', 'DATA.Value': '5', 'DATA.Weight': '1.0',
               'Model.MODL': 'Books\\TestBook.nif'}
        result = convert_BOOK(rec, writer=w)
        inam_fid = struct.unpack_from('<I', self._get_subrecord_data(result, 'INAM'))[0]
        assert inam_fid == 0x01000001, "INAM must point at the companion STAT"
        assert len(w.records) == 1 and w.records[0][0] == 'STAT'
        assert b'tes4\\clutter\\books\\inv\\testbook.nif' in w.records[0][1]
        # a second book with the same model reuses the STAT
        rec2 = dict(rec, FormID='00006002', EditorID='TestBookCopy')
        result2 = convert_BOOK(rec2, writer=w)
        inam2 = struct.unpack_from('<I', self._get_subrecord_data(result2, 'INAM'))[0]
        assert inam2 == inam_fid, "same model must reuse the same INAM STAT"
        assert len(w.records) == 1, "no duplicate STAT for a shared model"

    def test_book_scroll_flag_keeps_book_type(self):
        """TES4 Scroll flag (0x01) must still produce Type 0: vanilla
        Skyrim.esm types every one of its 821 BOOKs (notes included) as 0, so
        255 is an engine-untested value.  Scroll-ness survives via the vendor
        keyword and the note-rig inventory art."""
        rec = {'Signature': 'BOOK', 'FormID': '00006001', 'RecordFlags': '0',
               'EditorID': 'TestNote', 'FULL': 'A Test Note',
               'DESC': 'note text', 'DATA.Flags': '1',
               'DATA.Teaches': '255', 'DATA.Value': '5', 'DATA.Weight': '0.1'}
        result = convert_BOOK(rec)
        data = self._get_subrecord_data(result, 'DATA')
        assert data[1] == 0, "books must use Type=0 like all vanilla BOOKs"

    def test_book_html_font_face_remapped(self):
        """Numeric Oblivion <font face=N> must become Skyrim named fonts."""
        from tes5_import.record_types.equipment import _fix_book_html
        assert "<font face='$HandwrittenFont'>" in _fix_book_html('<font face=5>text')
        assert "<font face='$SkyrimBooks'>" in _fix_book_html('<FONT face=1>text')
        assert "<font face='$DaedricFont'>" in _fix_book_html('<font face=4>text')
        assert '</font>' in _fix_book_html('text</font>')
        assert 'face=5' not in _fix_book_html('<font face=5>text')

    def test_book_html_img_prefixed(self):
        """IMG src paths must use img:// with the converted texture path."""
        from tes5_import.record_types.equipment import _fix_book_html
        result = _fix_book_html('<IMG src="Book/fancy_font/h_62x62.dds" width=62 height=62>')
        assert "src='img://textures/tes4/menus/Book/fancy_font/h_62x62.dds'" in result
        # Already-converted paths must not be double-prefixed
        result2 = _fix_book_html("<IMG src='img://textures/tes4/menus/book/h.dds'>")
        assert result2.count('img://') == 1

    def test_book_html_no_double_br(self):
        """\\r\\n after <br> must not be converted to <br><br>."""
        from tes5_import.record_types.equipment import _fix_book_html
        result = _fix_book_html('<font face=5>\r\nline1<br>\r\nline2')
        assert '<br><br>' not in result

    def test_ligh(self):
        rec = {'Signature': 'LIGH', 'FormID': '00007000', 'RecordFlags': '0',
               'EditorID': 'TestLight', 'FULL': 'Torch',
               'DATA.Time': '-1', 'DATA.Radius': '300',
               'DATA.Color.R': '255', 'DATA.Color.G': '200', 'DATA.Color.B': '100',
               'DATA.Flags': '1', 'DATA.FalloffExponent': '1.0', 'DATA.FOV': '90.0',
               'DATA.Value': '2', 'DATA.Weight': '1.0'}
        result = convert_LIGH(rec)
        self._check_record(result, 'LIGH')
        data = self._get_subrecord_data(result, 'DATA')
        assert len(data) == 48

    def test_cont(self):
        rec = {'Signature': 'CONT', 'FormID': '00008000', 'RecordFlags': '0',
               'EditorID': 'TestChest', 'FULL': 'Chest',
               'Item[0].FormID': '00000100', 'Item[0].Count': '5',
               'DATA.Flags': '0', 'DATA.Weight': '25.0',
               'Model.MODL': 'Containers\\Chest.nif'}
        result = convert_CONT(rec)
        self._check_record(result, 'CONT')
        assert self._has_subrecord(result, 'CNTO')

    def test_door(self):
        rec = {'Signature': 'DOOR', 'FormID': '00009000', 'RecordFlags': '0',
               'EditorID': 'TestDoor', 'FULL': 'Wooden Door',
               'Model.MODL': 'Doors\\WoodDoor.nif'}
        result = convert_DOOR(rec)
        self._check_record(result, 'DOOR')


class TestDoorSounds:
    """DOOR SNAM/ANAM/BNAM must name an SNDR, not the TES4 SOUN.

    xEdit: wbFormIDCk(SNAM, 'Sound - Open', [SNDR]); Skyrim.esm agrees —
    WRDragonSideDoor01's SNAM 0005AFC9 is the SNDR DRSWoodImperialDouble01OpenSD.
    Doors are written in Phase 1, before the descriptors exist, so the SOUN id
    is a placeholder that patch_door_sounds resolves.
    """

    @staticmethod
    def _slots(blob):
        out = {}
        pos = 24
        assert struct.unpack_from('<I', blob, 4)[0] == len(blob) - 24
        while pos + 6 <= len(blob):
            sig = blob[pos:pos + 4]
            size = struct.unpack_from('<H', blob, pos + 4)[0]
            if sig in (b'SNAM', b'ANAM', b'BNAM') and size == 4:
                out[sig.decode()] = struct.unpack_from('<I', blob, pos + 6)[0]
            pos += 6 + size
        return out

    @staticmethod
    def _door(edid, **slots):
        from tes5_import.record_types.common import (pack_formid_subrecord,
                                                     pack_string_subrecord,
                                                     pack_uint8_subrecord)
        subs = pack_string_subrecord('EDID', edid)
        for sig in ('SNAM', 'ANAM', 'BNAM'):
            if slots.get(sig):
                subs += pack_formid_subrecord(sig, slots[sig])
        subs += pack_uint8_subrecord('FNAM', 0)
        return pack_record('DOOR', 0x01000001, 0, subs)

    class _Writer:
        def __init__(self, records):
            self._top_groups = {'DOOR': records}

    def test_slots_resolve_to_descriptors(self):
        from tes5_import.record_types import dialog_misc
        from tes5_import.record_types.items import patch_door_sounds
        dialog_misc.reset_sound_descriptors()
        dialog_misc.record_sndr_for_soun(0x0105C423, 0x01190F00)
        dialog_misc.record_sndr_for_soun(0x0105C424, 0x01190F01)
        w = self._Writer([self._door('D', SNAM=0x0105C423, ANAM=0x0105C424)])
        assert patch_door_sounds(w, {0x05C423, 0x05C424}) == 1
        assert self._slots(w._top_groups['DOOR'][0]) == {
            'SNAM': 0x01190F00, 'ANAM': 0x01190F01}

    def test_slot_without_descriptor_is_dropped(self):
        """A SOUN with no FNAM mints no SNDR; a slot pointing at the SOUN
        would be a wrong-typed reference, so it goes."""
        from tes5_import.record_types import dialog_misc
        from tes5_import.record_types.items import patch_door_sounds
        dialog_misc.reset_sound_descriptors()
        dialog_misc.record_sndr_for_soun(0x0105C423, 0x01190F00)
        w = self._Writer([self._door('D', SNAM=0x0105C423, BNAM=0x0105C425)])
        assert patch_door_sounds(w, {0x05C423, 0x05C425}) == 1
        assert self._slots(w._top_groups['DOOR'][0]) == {'SNAM': 0x01190F00}

    def test_master_override_slots_untouched(self):
        """An override build's DOOR group also holds the master's records,
        whose slots already name the MASTER's SNDRs. Rewriting or dropping
        those would strip door sound out of every dependent plugin."""
        from tes5_import.record_types import dialog_misc
        from tes5_import.record_types.items import patch_door_sounds
        dialog_misc.reset_sound_descriptors()
        dialog_misc.record_sndr_for_soun(0x0105C423, 0x01190F00)
        w = self._Writer([self._door('M', SNAM=0x00190AAA, ANAM=0x00190AAB)])
        assert patch_door_sounds(w, {0x05C423}) == 0
        assert self._slots(w._top_groups['DOOR'][0]) == {
            'SNAM': 0x00190AAA, 'ANAM': 0x00190AAB}

    def test_patch_is_idempotent(self):
        from tes5_import.record_types import dialog_misc
        from tes5_import.record_types.items import patch_door_sounds
        dialog_misc.reset_sound_descriptors()
        dialog_misc.record_sndr_for_soun(0x0105C423, 0x01190F00)
        w = self._Writer([self._door('D', SNAM=0x0105C423)])
        assert patch_door_sounds(w, {0x05C423}) == 1
        first = w._top_groups['DOOR'][0]
        assert patch_door_sounds(w, {0x05C423}) == 0
        assert w._top_groups['DOOR'][0] == first

    def test_mesh_authored_sound_fills_empty_slots(self):
        """Oblivion accepts a door's sound on the record OR as `sound:` text
        keys in the model; Skyrim has only the record. StoneWallGateDoor01's
        DOOR records carry no SNAM/ANAM at all — the gate creak lives entirely
        in the NIF — so without the mesh fallback they convert silent."""
        from tes5_import.record_types import items
        rec = {'Signature': 'DOOR', 'FormID': '0002595F', 'RecordFlags': '0',
               'EditorID': 'StoneWallGateDoor01', 'FULL': 'Wooden Gate',
               'Model.MODL': 'Architecture\\StoneWall\\StoneWallGateDoor01.NIF'}
        items._DOOR_MODEL_SOUNDS.clear()
        items._DOOR_MODEL_SOUNDS[
            'architecture/stonewall/stonewallgatedoor01.nif'] = {
                'open': 0x0105C423, 'close': 0x0105C424}
        try:
            slots = self._slots(items.convert_DOOR(rec))
        finally:
            items._DOOR_MODEL_SOUNDS.clear()
        assert slots == {'SNAM': 0x0105C423, 'ANAM': 0x0105C424}

    def test_record_sound_wins_over_mesh(self):
        """The record is the authored value where both exist."""
        from tes5_import.record_types import items
        rec = {'Signature': 'DOOR', 'FormID': '0002595F', 'RecordFlags': '0',
               'EditorID': 'D', 'SNAM.Open': '0105C400',
               'Model.MODL': 'Architecture\\StoneWall\\StoneWallGateDoor01.NIF'}
        items._DOOR_MODEL_SOUNDS.clear()
        items._DOOR_MODEL_SOUNDS[
            'architecture/stonewall/stonewallgatedoor01.nif'] = {
                'open': 0x0105C423, 'close': 0x0105C424}
        try:
            slots = self._slots(items.convert_DOOR(rec))
        finally:
            items._DOOR_MODEL_SOUNDS.clear()
        assert slots['SNAM'] == 0x0105C400
        assert slots['ANAM'] == 0x0105C424


# ---------------------------------------------------------------------------
# Integration test: Full pipeline on synthetic data
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_minimal_plugin(self):
        """Test creating a minimal valid plugin with just a header."""
        w = PluginWriter(masters=['Skyrim.esm'])
        with tempfile.NamedTemporaryFile(suffix='.esm', delete=False) as f:
            tmp = f.name
        try:
            w.write(tmp)
            with open(tmp, 'rb') as f:
                data = f.read()
            # Should at least have a TES4 header
            assert data[:4] == b'TES4'
            # Check header version
            hedr_pos = data.index(b'HEDR')
            ver = struct.unpack_from('<f', data, hedr_pos + 6)[0]
            assert abs(ver - 1.71) < 0.01
        finally:
            os.unlink(tmp)

    def test_plugin_with_records(self):
        """Test creating a plugin with a few records."""
        w = PluginWriter(masters=['Skyrim.esm'])

        # Add a STAT
        stat_rec = {'Signature': 'STAT', 'FormID': '00012345', 'RecordFlags': '0',
                    'EditorID': 'TestRock'}
        from tes5_import.record_types.items import convert_STAT
        stat_bytes = convert_STAT(stat_rec)
        w.add_record('STAT', stat_bytes)

        # Add a GLOB
        glob_rec = {'Signature': 'GLOB', 'FormID': '00012346', 'RecordFlags': '0',
                    'EditorID': 'TestGlobal', 'FNAM.Type': 'f', 'FLTV.Value': '1.0'}
        from tes5_import.record_types.actors import convert_GLOB
        glob_bytes = convert_GLOB(glob_rec)
        w.add_record('GLOB', glob_bytes)

        with tempfile.NamedTemporaryFile(suffix='.esm', delete=False) as f:
            tmp = f.name
        try:
            w.write(tmp)
            size = os.path.getsize(tmp)
            assert size > RECORD_HEADER_SIZE + GROUP_HEADER_SIZE

            with open(tmp, 'rb') as f:
                data = f.read()
            assert b'GRUP' in data
            assert b'STAT' in data
            assert b'GLOB' in data
        finally:
            os.unlink(tmp)


class TestSkyrimRecordFormat:
    """Tests that verify our understanding of Skyrim's record format
    by looking at actual Skyrim.esm data (if available)."""

    SKYRIM_ESM = r"C:\Program Files (x86)\Steam\steamapps\common\Skyrim Special Edition\Data\Skyrim.esm"

    @pytest.fixture(autouse=True)
    def _skip_if_no_skyrim(self):
        if not os.path.exists(self.SKYRIM_ESM):
            pytest.skip("Skyrim.esm not found")

    def _read_record(self, fid):
        """Read a record by FormID from Skyrim.esm."""
        import mmap
        with open(self.SKYRIM_ESM, 'rb') as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            pos = 0
            while pos < len(mm) - 24:
                sig = mm[pos:pos+4]
                if sig == b'GRUP':
                    pos += 24
                    continue
                sz = struct.unpack_from('<I', mm, pos+4)[0]
                rec_fid = struct.unpack_from('<I', mm, pos+12)[0]
                if rec_fid == fid:
                    rec = bytes(mm[pos:pos+24+sz])
                    mm.close()
                    return rec
                pos += 24 + sz
            mm.close()
        return None

    def test_skyrim_dialogue_generic_qust(self):
        """Skyrim's DialogueGeneric (0x00013EB3) must NOT have HasDialogueData."""
        rec = self._read_record(0x00013EB3)
        assert rec is not None, "DialogueGeneric not found"
        dnam = _find_subrecord(rec, b'DNAM')
        assert dnam is not None
        flags = struct.unpack_from('<H', dnam, 0)[0]
        assert not (flags & 0x8000), "Skyrim DialogueGeneric must not have HasDialogueData"
        assert flags & 0x0001, "Must have StartGameEnabled"
        assert flags & 0x0010, "Must have StartsEnabled"
        formver = dnam[3]
        assert formver == 0, "Skyrim QUST DNAM FormVer must be 0"

    def test_skyrim_vtyp_has_allow_default(self):
        """Skyrim MaleNord VTYP (0x00013AE6) should have AllowDefaultDialogue."""
        rec = self._read_record(0x00013AE6)
        if rec is None:
            pytest.skip("MaleNord VTYP not found")
        dnam = _find_subrecord(rec, b'DNAM')
        assert dnam is not None
        assert dnam[0] & 0x01, "AllowDefaultDialogue flag must be set"

    def test_skyrim_ctda_32_bytes(self):
        """Skyrim INFO conditions must be 32 bytes."""
        # We'll check DialogueGeneric's quest-level conditions
        rec = self._read_record(0x00013EB3)
        if rec is None:
            pytest.skip("DialogueGeneric not found")
        ctdas = _find_all_subrecords(rec, b'CTDA')
        for ctda in ctdas:
            assert len(ctda) == 32, f"CTDA should be 32 bytes, got {len(ctda)}"


# ---------------------------------------------------------------------------
# Voice file naming tests
# ---------------------------------------------------------------------------

import re
from asset_convert.audio_converter import _VOICE_FILENAME_RE, _TES4_VOICE_TYPE_MAP


class TestVoiceFileNaming:
    """Tests for the voice file regex and naming conventions."""

    def test_regex_captures_prefix(self):
        """Regex must capture quest_topic prefix as group(1)."""
        m = _VOICE_FILENAME_RE.match('arenaannouncer_announcer_0004216e_1.mp3')
        assert m is not None
        assert m.group(1) == 'arenaannouncer_announcer'
        assert m.group(2) == '0004216e'
        assert m.group(3) == '1'
        assert m.group(4) == 'mp3'

    def test_regex_complex_prefix(self):
        """Regex handles multi-underscore prefixes correctly."""
        m = _VOICE_FILENAME_RE.match('ms45_dar_ma_00012345_2.wav')
        assert m is not None
        assert m.group(1) == 'ms45_dar_ma'
        assert m.group(2) == '00012345'
        assert m.group(3) == '2'

    def test_regex_uppercase_formid(self):
        """Regex is case-insensitive for FormID hex digits."""
        m = _VOICE_FILENAME_RE.match('questname_topicname_0004ABCD_1.xwm')
        assert m is not None
        assert m.group(2) == '0004ABCD'

    def test_regex_fuz_extension(self):
        """Regex matches .fuz files."""
        m = _VOICE_FILENAME_RE.match('quest_topic_00001234_1.fuz')
        assert m is not None
        assert m.group(4) == 'fuz'

    def test_regex_rejects_short_formid(self):
        """Regex requires exactly 8 hex chars for FormID."""
        m = _VOICE_FILENAME_RE.match('quest_topic_01234_1.mp3')
        assert m is None

    def test_regex_rejects_no_prefix(self):
        """Regex requires at least one underscore-separated prefix."""
        m = _VOICE_FILENAME_RE.match('00012345_1.mp3')
        assert m is None

    def test_voice_type_map_coverage(self):
        """Voice type map covers all 10 playable races Ã— 2 genders."""
        playable_races = [
            'Argonian', 'Breton', 'DarkElf', 'HighElf', 'Imperial',
            'Khajiit', 'Nord', 'Orc', 'Redguard', 'WoodElf',
        ]
        for race in playable_races:
            for gender in ('M', 'F'):
                assert (race, gender) in _TES4_VOICE_TYPE_MAP, \
                    f"Missing voice type for ({race}, {gender})"

    def test_voice_type_map_sheogorath(self):
        """Sheogorath has Male voice type only."""
        assert ('Sheogorath', 'M') in _TES4_VOICE_TYPE_MAP

    def test_voice_type_naming_convention(self):
        """All voice types follow TES4{Male|Female}Race naming."""
        for (race, gender), vtype in _TES4_VOICE_TYPE_MAP.items():
            sex = 'Male' if gender == 'M' else 'Female'
            assert vtype.startswith(f'TES4{sex}'), \
                f"Voice type {vtype} for ({race}, {gender}) has wrong prefix"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_subrecord(record_bytes: bytes, sig: bytes) -> bytes | None:
    """Find the first subrecord with the given signature in a record."""
    # Skip the 24-byte record header
    pos = 24
    while pos < len(record_bytes) - 6:
        sub_sig = record_bytes[pos:pos+4]
        sub_size = struct.unpack_from('<H', record_bytes, pos+4)[0]
        if sub_sig == sig:
            return record_bytes[pos+6:pos+6+sub_size]
        pos += 6 + sub_size
    return None


def _find_all_subrecords(record_bytes: bytes, sig: bytes) -> list[bytes]:
    """Find all subrecords with the given signature in a record."""
    results = []
    pos = 24
    while pos < len(record_bytes) - 6:
        sub_sig = record_bytes[pos:pos+4]
        sub_size = struct.unpack_from('<H', record_bytes, pos+4)[0]
        if sub_sig == sig:
            results.append(record_bytes[pos+6:pos+6+sub_size])
        pos += 6 + sub_size
    return results


def _make_info_rec(data_flags=0, conditions=None, responses=None):
    """Create a minimal INFO test record dict."""
    rec = {
        'Signature': 'INFO', 'FormID': '00012345', 'RecordFlags': '0',
        'EditorID': 'TestInfo', 'DATA.Flags': str(data_flags),
        'ConditionCount': '0', 'ResponseCount': '0',
        'ChoiceCount': '0',
    }
    if conditions:
        rec['ConditionCount'] = str(len(conditions))
        for i, (raw_hex,) in enumerate(conditions):
            rec[f'Condition[{i}].Raw'] = raw_hex
    if responses:
        rec['ResponseCount'] = str(len(responses))
        for i, (text, emotion, value) in enumerate(responses):
            rec[f'Response[{i}].ResponseText'] = text
            rec[f'Response[{i}].EmotionType'] = str(emotion)
            rec[f'Response[{i}].EmotionValue'] = str(value)
            rec[f'Response[{i}].ResponseNumber'] = str(i + 1)
    return rec


class TestFurnConversion:
    """FURN MNAM/FNPR must index the converted NIF's clustered seat positions.

    The seat list is derived from the source NIF with the shared algorithm in
    asset_convert/furniture_markers.py; MNAM bit i enables NIF position i, so
    dangling bits would seat NPCs at garbage positions in-game.
    """

    MESHES = os.path.join('export', 'Oblivion.esm', 'meshes')

    def _unpack(self, data):
        i = data.find(b'MNAM')
        mnam = struct.unpack_from('<I', data, i + 6)[0]
        fnprs = []
        j = 0
        while True:
            j = data.find(b'FNPR', j)
            if j < 0:
                break
            fnprs.append(struct.unpack_from('<HH', data, j + 6))
            j += 10
        return mnam, fnprs

    def _furn_rec(self, modl, mnam_flags):
        return {
            'Signature': 'FURN', 'FormID': '00012345', 'RecordFlags': '0',
            'EditorID': 'TestFurn', 'Model.MODL': modl,
            'MNAM.Flags': str(mnam_flags),
        }

    @pytest.fixture(autouse=True)
    def _seats(self):
        if not os.path.isdir(self.MESHES):
            pytest.skip('Export meshes not available')
        from tes5_import.record_types.items import load_furniture_models
        recs = [
            self._furn_rec('Furniture\\LowerClass\\LowerClassBench01.NIF', 0),
            self._furn_rec('Furniture\\LowerClass\\LowerClassBed01.NIF', 0),
        ]
        load_furniture_models(self.MESHES, {'FURN': recs})

    def test_bench_mnam_matches_seat_count(self):
        """3-seat bench: MNAM bits 0-2 + preserved sit-type flag, 3 FNPR."""
        from tes5_import.record_types.items import convert_FURN
        # TES4: bits for all 8 entry markers + sit-type bit 30
        rec = self._furn_rec('Furniture\\LowerClass\\LowerClassBench01.NIF',
                             0x400000FF)
        mnam, fnprs = self._unpack(convert_FURN(rec))
        assert mnam == 0x40000007, hex(mnam)
        assert len(fnprs) == 3
        assert all(t == 1 for t, _ in fnprs)  # Sit

    def test_bench_entry_restriction(self):
        """TES4 record enabling only the front-entry row (bits 5-7 = ref 14
        entries) must yield front-only FNPR entry points on every seat."""
        from tes5_import.record_types.items import convert_FURN
        rec = self._furn_rec('Furniture\\LowerClass\\LowerClassBench01.NIF',
                             0x400000E0)
        mnam, fnprs = self._unpack(convert_FURN(rec))
        assert mnam == 0x40000007
        assert fnprs == [(1, 0x01), (1, 0x01), (1, 0x01)]

    def test_bed_single_sleep_seat(self):
        """Bed entries converge to ONE sleep seat: MNAM bit 0 + bed-type +
        Must Exit to Talk, FNPR Sleep with left|right entries."""
        from tes5_import.record_types.items import convert_FURN
        rec = self._furn_rec('Furniture\\LowerClass\\LowerClassBed01.NIF',
                             0x80000003)
        mnam, fnprs = self._unpack(convert_FURN(rec))
        assert mnam == 0x88000001, hex(mnam)
        assert fnprs == [(2, 0x04 | 0x08)]

    def test_missing_nif_fallback(self):
        """Unresolvable model: single conservative seat, all entry points.

        Must be a path that exists in NO extracted BSA (SEfurniture used to
        qualify until the Shivering Isles archives joined the extraction)."""
        from tes5_import.record_types.items import convert_FURN
        rec = self._furn_rec('Clutter\\NoSuchDir\\NoSuchChair01.NIF',
                             0x40000004)
        mnam, fnprs = self._unpack(convert_FURN(rec))
        assert mnam == 0x40000001, hex(mnam)
        assert fnprs == [(1, 0x0F)]

    def test_seat_count_matches_converted_nif(self):
        """The NIF converter must emit exactly the positions MNAM enables."""
        import time
        if not hasattr(time, 'clock'):
            time.clock = time.perf_counter
        from pyffi.formats.nif import NifFormat as NF
        from asset_convert.nif_converter import convert_nif
        from tes5_import.record_types.items import _FURN_SEATS, _furn_model_key

        key = _furn_model_key('Furniture\\LowerClass\\LowerClassBench01.NIF')
        seats = _FURN_SEATS[key]
        src = os.path.join(self.MESHES, key.replace('/', os.sep))
        with tempfile.TemporaryDirectory() as td:
            dst = os.path.join(td, 'out.nif')
            convert_nif(src, dst)
            data = NF.Data()
            with open(dst, 'rb') as f:
                data.read(f)
            for block in data.blocks:
                if isinstance(block, NF.BSFurnitureMarkerNode):
                    assert block.num_positions == len(seats)
                    for i, seat in enumerate(seats):
                        p = block.positions[i]
                        assert abs(p.offset.x - seat['x']) < 0.01
                        assert abs(p.offset.y - seat['y']) < 0.01
                        assert abs(p.offset.z - seat['z']) < 0.01
                    break
            else:
                pytest.fail('No BSFurnitureMarkerNode in converted NIF')


class TestServiceConversion:
    """Barter/training services: trainer CLAS clones, vendor gold, dialogue."""

    def _subrecords(self, rec_bytes: bytes) -> dict:
        data = rec_bytes[RECORD_HEADER_SIZE:]
        subs = {}
        pos = 0
        while pos + 6 <= len(data):
            sig = data[pos:pos + 4].decode('ascii')
            size = struct.unpack_from('<H', data, pos + 4)[0]
            subs.setdefault(sig, []).append(data[pos + 6:pos + 6 + size])
            pos += 6 + size
        return subs

    def _trainer_npc(self, teaches='7', maxtrain='70', services='16385'):
        # services 16385 = 0x4001 (weapons vendor + training)
        return {'Signature': 'NPC_', 'FormID': '00000500', 'RecordFlags': '0',
                'EditorID': 'TestTrainer', 'FULL': 'Trainer',
                'ACBS.Flags': '0', 'ACBS.Level': '5', 'ACBS.CalcMin': '5',
                'ACBS.CalcMax': '10', 'ACBS.BarterGold': '800',
                'FactionCount': '0', 'SpellCount': '0', 'ItemCount': '0',
                'AIPackageCount': '0',
                'AIDT.Aggression': '5', 'AIDT.Confidence': '50',
                'AIDT.Responsibility': '50', 'AIDT.Services': services,
                'AIDT.Teaches': teaches, 'AIDT.MaxTraining': maxtrain,
                'CNAM.Class': '00000900',
                'DATA.Health': '50', 'DATA.Intelligence': '40',
                'DATA.Strength': '60', 'DATA.Endurance': '45',
                'HCLR.R': '100', 'HCLR.G': '80', 'HCLR.B': '60'}

    def _clas_rec(self):
        return {'Signature': 'CLAS', 'FormID': '00000900', 'RecordFlags': '0',
                'EditorID': 'TestClass', 'FULL': 'Warrior', 'DESC': '',
                'DATA.Specialization': '0', 'DATA.Teaches': '0',
                'DATA.MaxTraining': '0'}

    def test_trainer_class_and_faction(self):
        from tes5_import.record_types.actors import (
            create_trainer_records, create_vendor_factions,
            get_trainer_class_fid, get_trainer_faction_fid)
        from tes5_import.constants import TES5_SKILL_ORDER
        writer = PluginWriter(masters=['Skyrim.esm'])
        npc = self._trainer_npc()   # Teaches=7 (Alchemy), max 70
        by_type = {'NPC_': [npc], 'CLAS': [self._clas_rec()]}
        create_vendor_factions(by_type, writer)
        create_trainer_records(by_type, writer)

        assert get_trainer_faction_fid() != 0
        clone_fid = get_trainer_class_fid(0x00000500)
        assert clone_fid != 0
        # The CLAS clone must carry Teaches=Alchemy + MaxTraining=70
        clas_bytes = writer._top_groups['CLAS'][-1]
        subs = self._subrecords(clas_bytes)
        data = subs['DATA'][0]
        teaches, maxtrain = struct.unpack_from('<bB', data, 4)
        assert teaches == TES5_SKILL_ORDER.index('Alchemy')
        assert maxtrain == 70

        # NPC gets the clone as CNAM, trainer faction SNAM, and vendor gold
        npc_bytes = convert_NPC_(npc)
        nsubs = self._subrecords(npc_bytes)
        assert struct.unpack('<I', nsubs['CNAM'][0])[0] == clone_fid
        snam_fids = {struct.unpack_from('<I', s)[0] for s in nsubs['SNAM']}
        assert get_trainer_faction_fid() in snam_fids
        cnto = [struct.unpack('<Ii', c) for c in nsubs['CNTO']]
        assert (0x0000000F, 800) in cnto   # Gold001 x barter gold

    def _merchant_npc(self, fid='00000501', services='132227'):
        npc = self._trainer_npc(services=services)
        npc['FormID'] = fid
        npc['EditorID'] = 'TestMerchant'
        return npc

    def test_every_merchant_joins_one_marker_faction(self):
        """The Barter topic gates on ONE faction, so every merchant — whatever
        its service bitmask, chest-backed or not — must be a member of it."""
        from tes5_import.record_types.actors import (
            create_vendor_factions, get_merchant_faction_fid,
            get_vendor_faction_fids_for_actor)
        writer = PluginWriter(masters=['Skyrim.esm'])
        a = self._merchant_npc(fid='00000501', services='132227')
        b = self._merchant_npc(fid='00000502', services='9216')
        create_vendor_factions({'NPC_': [a, b], 'CREA': []}, writer)

        marker = get_merchant_faction_fid()
        assert marker != 0
        for fid, svc in ((0x00000501, 132227), (0x00000502, 9216)):
            assert marker in get_vendor_faction_fids_for_actor(fid, svc)

    def test_marker_faction_is_not_a_vendor_faction(self):
        """The marker is a membership tag only. Giving it the Vendor flag would
        make it compete with the real vendor faction the engine resolves for the
        barter menu (VEND keyword filter / VENC chest)."""
        from tes5_import.record_types.actors import (create_vendor_factions,
                                                     get_merchant_faction_fid)
        writer = PluginWriter(masters=['Skyrim.esm'])
        create_vendor_factions({'NPC_': [self._merchant_npc()]}, writer)
        marker = get_merchant_faction_fid()
        fact = next(f for f in writer._top_groups['FACT']
                    if struct.unpack_from('<I', f, 12)[0] == marker)
        subs = self._subrecords(fact)
        flags = struct.unpack('<I', subs['DATA'][0])[0]
        assert not (flags & 0x4000), 'marker must not carry the Vendor flag'
        assert 'VEND' not in subs and 'VENV' not in subs

    def test_barter_gate_is_a_single_condition(self):
        """Regression: the Barter gate used to OR over every vendor faction,
        putting 25-30 CTDAs on each Barter INFO. Vanilla Skyrim never exceeds 22
        conditions on an INFO (longest OR-run is 20); past that the engine drops
        the line, so every Barter INFO failed and merchants lost the topic
        entirely — while Training, a 1-condition gate, kept working."""
        from tes5_import.dialog_conditions import (FUNC_GET_IN_FACTION,
                                                   build_ctda)
        from tes5_import.dialog_converter import _build_service_fallback_info
        from tes5_import.record_types.actors import (create_vendor_factions,
                                                     get_merchant_faction_fid)
        writer = PluginWriter(masters=['Skyrim.esm'])
        # Many distinct service bitmasks => many vendor factions. The gate must
        # stay at one condition regardless of how many exist.
        npcs = [self._merchant_npc(fid=f'0000{0x600 + i:03X}', services=str(s))
                for i, s in enumerate((3, 4, 8, 16, 1155, 2048, 4103, 5124))]
        create_vendor_factions({'NPC_': npcs}, writer)

        gate = pack_subrecord('CTDA', build_ctda(
            FUNC_GET_IN_FACTION, param1=get_merchant_faction_fid()))
        info = _build_service_fallback_info(writer, 'barter', gate)
        n_ctda = self._subrecords(info).get('CTDA', [])
        assert len(n_ctda) == 1, f'barter gate must be 1 CTDA, got {len(n_ctda)}'
        assert struct.unpack_from('<I', n_ctda[0], 12)[0] == get_merchant_faction_fid()

    def test_trainer_unmappable_skill_skipped(self):
        from tes5_import.record_types.actors import (
            create_trainer_records, get_trainer_class_fid)
        writer = PluginWriter(masters=['Skyrim.esm'])
        # Teaches=1 (Athletics) has no Skyrim skill -> not a trainer
        npc = self._trainer_npc(teaches='1')
        create_trainer_records({'NPC_': [npc], 'CLAS': []}, writer)
        assert get_trainer_class_fid(0x00000500) == 0

    def test_service_menu_kind(self):
        from tes5_import.dialog_converter import (service_menu_kind,
                                                  should_skip_dial)
        barter = {'Signature': 'DIAL', 'FormID': '0000010F',
                  'EditorID': 'Barter', 'DATA.Type': '5'}
        refusal = {'Signature': 'DIAL', 'FormID': '0000010E',
                   'EditorID': 'ServiceRefusal', 'DATA.Type': '5'}
        assert service_menu_kind(barter) == 'barter'
        assert not should_skip_dial(barter)
        assert service_menu_kind(refusal) == ''
        assert should_skip_dial(refusal)

    def test_convert_info_service_vmad(self):
        from tes5_import.dialog_converter import convert_INFO
        rec = {'Signature': 'INFO', 'FormID': '00062116', 'RecordFlags': '0',
               'ParentDIAL': '0000010F', 'DATA.Flags': '2',
               'ResponseCount': '1', 'Response[0].EmotionType': '0',
               'Response[0].EmotionValue': '50',
               'Response[0].ResponseNumber': '1',
               'Response[0].ResponseText': 'Take a look.'}
        result = convert_INFO(rec, service_menu='barter')
        assert b'TES4_ShowBarterMenu' in result
        result = convert_INFO(rec, service_menu='training')
        assert b'TES4_ShowTrainingMenu' in result
        # Without a service menu there is no VMAD at all
        assert b'VMAD' not in convert_INFO(rec)

    def test_vendor_item_keywords(self):
        """Sellable items carry the VendorItem* keyword the vendor factions
        filter on (no keyword = item invisible in the barter menu)."""
        from tes5_import.record_types.common import VENDOR_KYWD

        def kwda_fids(rec_bytes):
            data = self._subrecords(rec_bytes).get('KWDA')
            if not data:
                return set()
            return {struct.unpack_from('<I', data[0], i)[0]
                    for i in range(0, len(data[0]), 4)}

        weap = convert_WEAP({'Signature': 'WEAP', 'FormID': '00001000',
                             'RecordFlags': '0', 'EditorID': 'TestSword',
                             'DATA.Type': '0', 'DATA.Value': '10',
                             'DATA.Weight': '5', 'DATA.Damage': '8'})
        assert VENDOR_KYWD['Weapon'] in kwda_fids(weap)

        staff = convert_WEAP({'Signature': 'WEAP', 'FormID': '00001001',
                              'RecordFlags': '0', 'EditorID': 'TestStaff',
                              'DATA.Type': '4', 'DATA.Value': '10',
                              'DATA.Weight': '5', 'DATA.Damage': '8'})
        assert VENDOR_KYWD['Staff'] in kwda_fids(staff)

        # Ring (TES4 biped bit 6 = RightRing) -> Jewelry, not Clothing
        ring = convert_CLOT({'Signature': 'CLOT', 'FormID': '00001002',
                             'RecordFlags': '0', 'EditorID': 'TestRing',
                             'BMDT.BipedFlags': str(1 << 6),
                             'DATA.Value': '50', 'DATA.Weight': '0.1'})
        assert VENDOR_KYWD['Jewelry'] in kwda_fids(ring)

        shirt = convert_CLOT({'Signature': 'CLOT', 'FormID': '00001003',
                              'RecordFlags': '0', 'EditorID': 'TestShirt',
                              'BMDT.BipedFlags': str(1 << 2),
                              'DATA.Value': '5', 'DATA.Weight': '1'})
        assert VENDOR_KYWD['Clothing'] in kwda_fids(shirt)

        misc = convert_MISC({'Signature': 'MISC', 'FormID': '00001004',
                             'RecordFlags': '0', 'EditorID': 'TestPlate',
                             'DATA.Value': '2', 'DATA.Weight': '1'})
        assert VENDOR_KYWD['Clutter'] in kwda_fids(misc)

        # Every emitted keyword must be tradable at the matching TES4 vendor
        from tes5_import.record_types.actors import _keywords_for_services
        assert VENDOR_KYWD['Arrow'] in _keywords_for_services(1 << 0)
        assert VENDOR_KYWD['Clutter'] in _keywords_for_services(1 << 10)

    def test_barter_topic_dialogue(self):
        """End-to-end: Barter topic converts with prompt, gate and fragment."""
        from tes5_import.dialog_converter import build_dialog_groups
        from tes5_import.record_types.actors import (
            create_trainer_records, create_vendor_factions)
        writer = PluginWriter(masters=['Skyrim.esm'])
        npc = self._trainer_npc()
        qust = {'Signature': 'QUST', 'FormID': '00010602',
                'EditorID': 'Generic', 'DATA.Flags': '5',
                'DATA.Priority': '30', 'StageCount': '0'}
        dial = {'Signature': 'DIAL', 'FormID': '0000010F',
                'EditorID': 'Barter', 'FULL': 'Barter', 'DATA.Type': '5',
                'QuestCount': '1', 'Quest[0]': '00010602'}
        info = {'Signature': 'INFO', 'FormID': '00062116', 'RecordFlags': '0',
                'ParentDIAL': '0000010F', 'DATA.Flags': '2',
                'QSTI.Quest': '00010602', 'ResponseCount': '1',
                'Response[0].EmotionType': '0',
                'Response[0].EmotionValue': '50',
                'Response[0].ResponseNumber': '1',
                'Response[0].ResponseText': 'I have much to offer.'}
        by_type = {'NPC_': [npc], 'CLAS': [self._clas_rec()],
                   'QUST': [qust], 'DIAL': [dial], 'INFO': [info]}
        create_vendor_factions(by_type, writer)
        create_trainer_records(by_type, writer)
        build_dialog_groups(by_type, writer, npc_to_vtyp={})

        dial_group = b''.join(writer._top_groups['DIAL'])
        # Player prompt replaces the raw 'Barter' FULL
        assert b'What have you got for sale?' in dial_group
        # Both the original line and the synthetic fallback carry the shared
        # barter fragment (the script name appears 3x per VMAD: attached
        # script, fragment FileName, fragment ScriptName)
        assert dial_group.count(b'TES4_ShowBarterMenu') == 6
        assert b'Take a look.' in dial_group

    def test_greeting_choice_reaches_response_topic(self):
        """A greeting bark whose INFO carries a Choice must keep that TCLT and
        the response topic must get a TOP-LEVEL branch — otherwise the NPC
        greets the player but the player cannot select the response (FGC01Rats:
        Arvena asks what happened in the basement, player can't answer). Also
        verifies a greeting Choice pointing at ANOTHER bark is dropped (it would
        dangle after the bark pass splits/merges topics)."""
        from tes5_import.dialog_converter import build_dialog_groups
        from tes5_import.text_reader import set_formid_index_offset
        set_formid_index_offset(0)
        writer = PluginWriter(masters=['Skyrim.esm'])
        qust = {'Signature': 'QUST', 'FormID': '00035713',
                'EditorID': 'FGC01Rats', 'DATA.Flags': '1',
                'DATA.Priority': '30', 'StageCount': '0'}
        # GREETING (bark). Its INFO offers a Choice -> the response topic, plus a
        # Choice -> another greeting sub-topic (must be dropped).
        greeting = {'Signature': 'DIAL', 'FormID': '000000C8',
                    'EditorID': 'GREETING', 'FULL': 'GREETING',
                    'DATA.Type': '0',  # classified as bark by reserved EDID
                    'QuestCount': '1', 'Quest[0]': '00035713'}
        greet_info = {'Signature': 'INFO', 'FormID': '00036622',
                      'RecordFlags': '0', 'ParentDIAL': '000000C8',
                      'DATA.Flags': '0', 'QSTI.Quest': '00035713',
                      'ResponseCount': '1', 'Response[0].EmotionType': '0',
                      'Response[0].EmotionValue': '50',
                      'Response[0].ResponseNumber': '1',
                      'Response[0].ResponseText': 'What did you find?',
                      # GetStage(FGC01Rats)==30 — the greeting only fires after
                      # the lion is dealt with. The response must inherit this.
                      'ConditionCount': '1',
                      'Condition[0].Raw':
                          '000000000000f0413a000000135703000000000000000000',
                      'ChoiceCount': '2',
                      'Choice[0]': '00036613',   # -> FGC01Choice1 (conversation)
                      'Choice[1]': '000000C9'}   # -> another GREETING (dropped)
        greeting2 = {'Signature': 'DIAL', 'FormID': '000000C9',
                     'EditorID': 'GREETING', 'FULL': 'GREETING',
                     'DATA.Type': '0', 'QuestCount': '1',
                     'Quest[0]': '00035713'}
        greet2_info = {'Signature': 'INFO', 'FormID': '000000CA',
                       'RecordFlags': '0', 'ParentDIAL': '000000C9',
                       'DATA.Flags': '0', 'QSTI.Quest': '00035713',
                       'ResponseCount': '1', 'Response[0].EmotionType': '0',
                       'Response[0].EmotionValue': '50',
                       'Response[0].ResponseNumber': '1',
                       'Response[0].ResponseText': 'Hello again.'}
        # The player response topic (conversation), reached only via the greeting.
        choice1 = {'Signature': 'DIAL', 'FormID': '00036613',
                   'EditorID': 'FGC01Choice1', 'FULL': 'It was a mountain lion.',
                   'DATA.Type': '0', 'QuestCount': '1', 'Quest[0]': '00035713'}
        choice1_info = {'Signature': 'INFO', 'FormID': '0003662A',
                        'RecordFlags': '0', 'ParentDIAL': '00036613',
                        'DATA.Flags': '0', 'QSTI.Quest': '00035713',
                        'ResponseCount': '1', 'Response[0].EmotionType': '0',
                        'Response[0].EmotionValue': '70',
                        'Response[0].ResponseNumber': '1',
                        'Response[0].ResponseText': 'A mountain lion? How?'}
        by_type = {'QUST': [qust],
                   'DIAL': [greeting, greeting2, choice1],
                   'INFO': [greet_info, greet2_info, choice1_info]}
        build_dialog_groups(by_type, writer, npc_to_vtyp={})

        dial_group = b''.join(writer._top_groups.get('DIAL', []))
        dlbr_group = b''.join(writer._top_groups.get('DLBR', []))

        # The greeting INFO keeps its TCLT to the conversation response topic...
        greet_rec = self._find_record(dial_group, b'INFO', 0x00036622)
        assert greet_rec is not None, 'greeting INFO missing'
        tclts = {struct.unpack('<I', d[:4])[0]
                 for d in self._subrecords(greet_rec).get('TCLT', [])}
        assert 0x00036613 in tclts, 'greeting lost its Choice to the response'
        # ...but drops the Choice that points at another bark.
        assert 0x000000C9 not in tclts, 'bark->bark choice should be dropped'

        # The response topic's branch is TOP-LEVEL (DNAM=1), so it is selectable.
        branch = self._find_record(dlbr_group, b'DLBR', None,
                                   snam=0x00036613)
        assert branch is not None, 'no branch for the response topic'
        dnam = self._subrecords(branch)['DNAM'][0]
        assert struct.unpack('<I', dnam[:4])[0] == 1, \
            'greeting-reached response topic must be a top-level branch'

        # The response INFO inherits the greeting's quest-TIMING gate
        # (GetStage==30), so it only appears after the lion is dealt with —
        # NOT from the first conversation. Without this the top-level topic
        # leaks into the menu whenever GetIsID passes.
        resp = self._find_record(dial_group, b'INFO', 0x0003662A)
        assert resp is not None, 'response INFO missing'
        funcs = [struct.unpack_from('<H', c, 8)[0]
                 for c in self._subrecords(resp).get('CTDA', [])]
        assert 58 in funcs, \
            'response topic must inherit the greeting GetStage(58) timing gate'

    def test_bark_prose_mention_does_not_ungate_topic(self):
        """A gated topic whose FULL name merely appears in a GREETING's prose
        must STAY gated. Only an explicit AddTopic/Choice from a bark reveals a
        topic on first contact; a prose mention rides that bark line's own
        (stage) conditions. Azzan's 'Advancement' was showing before joining
        the guild because 6 late-game greetings say the word 'advancement'."""
        from tes5_import.dialog_unlocks import build_unlock_plan
        # A conversation topic 'Advancement', AddTopic'd by a normal join line.
        topic = {'Signature': 'DIAL', 'FormID': '0003568F',
                 'EditorID': 'advancementFG', 'FULL': 'Advancement',
                 'DATA.Type': '0'}
        join_info = {'Signature': 'INFO', 'FormID': '0002427B',
                     'ParentDIAL': '00024279',   # a conversation topic (FGJoin1)
                     'AddTopicCount': '1', 'AddTopic[0]': '0003568F'}
        join_topic = {'Signature': 'DIAL', 'FormID': '00024279',
                      'EditorID': 'FGJoin1', 'DATA.Type': '0'}
        # A GREETING (bark) whose response text mentions 'Advancement'.
        greeting = {'Signature': 'DIAL', 'FormID': '000000C8',
                    'EditorID': 'GREETING', 'DATA.Type': '0'}
        greet_info = {'Signature': 'INFO', 'FormID': '00023F7B',
                      'ParentDIAL': '000000C8',
                      'ResponseCount': '1',
                      'Response[0].ResponseText':
                          'You are ready for advancement.'}
        by_type = {'DIAL': [topic, join_topic, greeting],
                   'INFO': [join_info, greet_info], 'QUST': []}
        plan = build_unlock_plan(by_type)
        assert 0x03568F in plan['gated'], \
            'topic mentioned only in bark prose must stay gated'

    def test_convert_qust_writes_boosted_priority(self):
        """The bug this whole class exists to catch: a fix that only changes
        the DERIVED DIAL PNAM (used for merged bark topics) but not the QUST
        record's OWN DNAM.Priority byte has NO EFFECT in-game, because the
        engine arbitrates dialogue on the quest's own priority. convert_QUST
        must write the EFFECTIVE (boosted) priority, not the raw TES4 value."""
        from tes5_import.dialog_converter import compute_quest_priorities, convert_QUST
        staged = {'Signature': 'QUST', 'FormID': '00035713',
                 'EditorID': 'RealQuest', 'DATA.Flags': '0',
                 'DATA.Priority': '60', 'StageCount': '1',
                 'Stage[0].Index': '10'}
        container = {'Signature': 'QUST', 'FormID': '00035714',
                    'EditorID': 'ContainerQuest', 'DATA.Flags': '1',
                    'DATA.Priority': '61', 'StageCount': '0'}
        by_type = {'QUST': [staged, container]}
        compute_quest_priorities(by_type)

        staged_bytes = convert_QUST(staged)
        container_bytes = convert_QUST(container)
        staged_dnam = self._subrecords(staged_bytes)['DNAM'][0]
        container_dnam = self._subrecords(container_bytes)['DNAM'][0]
        staged_priority = staged_dnam[2]     # Priority is byte offset 2 in DNAM
        container_priority = container_dnam[2]
        assert container_priority < staged_priority, \
            (f'container QUST.DNAM.Priority={container_priority} still '
             f'>= staged QUST.DNAM.Priority={staged_priority}: the engine '
             'reads this field directly, so the fix must land here')
        # The raw authored value (61) must not survive unboosted-vs-boosted —
        # confirms the write path actually consulted the override table.
        assert container_priority != 61 or staged_priority > 60

    def test_quest_priority_never_exceeds_engine_max(self):
        """DNAM.Priority must stay in the engine's 0-100 band.

        Vanilla Skyrim.esm's 391 quests top out at EXACTLY 100 with none above
        it (the CK field is 0-100). The byte does not only order dialogue — it
        arbitrates a quest ALIAS PACKAGE against the actor's standing schedule,
        so an out-of-band value breaks AI too. The old additive boost pushed
        TES4 priority 60 to 161 and put 265 of 391 quests (68%) over 100, which
        is why converted escort/travel packages could pass their condition and
        start (the actor stands up) yet never actually travel.
        """
        from tes5_import.dialog_converter import (
            QUEST_PRIORITY_MAX, compute_quest_priorities, convert_QUST)
        # Authored priorities spanning TES4's range, staged and zero-stage.
        quests = []
        for i, (prio, stages) in enumerate(
                [(0, 1), (11, 1), (60, 1), (90, 1), (100, 1),
                 (0, 0), (50, 0), (61, 0), (85, 0)]):
            q = {'Signature': 'QUST', 'FormID': f'000357{i:02X}',
                 'EditorID': f'Q{i}', 'DATA.Flags': '0',
                 'DATA.Priority': str(prio), 'StageCount': str(stages)}
            if stages:
                q['Stage[0].Index'] = '10'
            quests.append(q)
        by_type = {'QUST': quests}
        pri = compute_quest_priorities(by_type)

        assert max(pri.values()) <= QUEST_PRIORITY_MAX, \
            f'priority {max(pri.values())} exceeds engine max {QUEST_PRIORITY_MAX}'
        assert min(pri.values()) >= 0
        # ...and the written byte agrees with the table.
        for q in quests:
            dnam = self._subrecords(convert_QUST(q))['DNAM'][0]
            assert 0 <= dnam[2] <= QUEST_PRIORITY_MAX

        # Staged quests keep their AUTHORED priority — the correction is a
        # downward clamp on containers only, never a shift of staged values.
        for q in quests:
            if int(q['StageCount']):
                assert pri[int(q['FormID'], 16)] == int(q['DATA.Priority']), \
                    'a staged quest must keep the priority its author wrote'

        # No container may sit above the ceiling, so a normally-authored staged
        # quest outranks every container. (Three vanilla staged quests are
        # authored at 0, so this is NOT a universal min(staged) > max(zero) —
        # clamping to that would flatten all 125 containers onto one value.)
        from tes5_import.dialog_converter import ZERO_STAGE_TOP
        zero = [pri[int(q['FormID'], 16)] for q in quests
                if not int(q['StageCount'])]
        assert max(zero) <= ZERO_STAGE_TOP, \
            'a zero-stage container must never exceed the container ceiling'

    def test_zero_stage_quest_never_outranks_staged_greeting(self):
        """A zero-stage 'conversation container' quest (MG00General-style,
        priority 61 in vanilla) must never outrank a REAL staged quest's
        GREETING (priority 60) in the merged HELO bark group. Oblivion ran
        GREETING and HELLO as separate channels, so a container quest's
        authored priority never had to compete with a staged quest's
        on-activate briefing; merged into one Skyrim HELO topic per quest, the
        higher-priority container's cover line won and the staged quest's
        SetStage-advancing briefing never played (symptom: NPC only gives a
        generic greeting, journal stage correct, every record field verified
        individually correct — MG00General/MG04Restore/Arielle Jurard,
        2026-07-20)."""
        from tes5_import.dialog_converter import build_dialog_groups
        from tes5_import.text_reader import set_formid_index_offset
        set_formid_index_offset(0)
        writer = PluginWriter(masters=['Skyrim.esm'])

        staged = {'Signature': 'QUST', 'FormID': '00035713',
                 'EditorID': 'RealQuest', 'DATA.Flags': '0',
                 'DATA.Priority': '60', 'StageCount': '1',
                 'Stage[0].Index': '10'}
        container = {'Signature': 'QUST', 'FormID': '00035714',
                    'EditorID': 'ContainerQuest', 'DATA.Flags': '1',
                    'DATA.Priority': '61', 'StageCount': '0'}

        greeting = {'Signature': 'DIAL', 'FormID': '000000C8',
                   'EditorID': 'GREETING', 'FULL': 'GREETING',
                   'DATA.Type': '0', 'QuestCount': '1',
                   'Quest[0]': '00035713'}
        staged_info = {'Signature': 'INFO', 'FormID': '00036622',
                      'RecordFlags': '0', 'ParentDIAL': '000000C8',
                      'DATA.Flags': '0', 'QSTI.Quest': '00035713',
                      'ResponseCount': '1', 'Response[0].EmotionType': '0',
                      'Response[0].EmotionValue': '50',
                      'Response[0].ResponseNumber': '1',
                      'Response[0].ResponseText': 'The staged briefing line.'}

        hello = {'Signature': 'DIAL', 'FormID': '000000D2',
                'EditorID': 'HELLO', 'FULL': 'HELLO',
                'DATA.Type': '1', 'QuestCount': '1',
                'Quest[0]': '00035714'}
        container_info = {'Signature': 'INFO', 'FormID': '00036623',
                          'RecordFlags': '0', 'ParentDIAL': '000000D2',
                          'DATA.Flags': '0', 'QSTI.Quest': '00035714',
                          'ResponseCount': '1', 'Response[0].EmotionType': '0',
                          'Response[0].EmotionValue': '50',
                          'Response[0].ResponseNumber': '1',
                          'Response[0].ResponseText': 'Generic cover line.'}

        by_type = {'QUST': [staged, container],
                  'DIAL': [greeting, hello],
                  'INFO': [staged_info, container_info]}
        build_dialog_groups(by_type, writer, npc_to_vtyp={})

        # Arbitration lives on QUST.DNAM.Priority — NOT on the topic's PNAM.
        from tes5_import.dialog_converter import compute_quest_priorities
        pri = compute_quest_priorities(by_type)
        staged_prio = pri[0x00035713]
        container_prio = pri[0x00035714]
        assert container_prio < staged_prio, \
            (f'container quest priority {container_prio} still outranks '
             f'the staged quest priority {staged_prio}')

        # ...and every generated bark topic keeps the vanilla 50.0 PNAM
        # default. Writing quest priority here instead is what put FGC01Rats'
        # GREETING at 161 against its own player topics' 50.0 and cost Pinarus
        # every topic he owned (mountain-lion AND training).
        dial_group = b''.join(writer._top_groups.get('DIAL', []))
        for info_fid in (0x00036622, 0x00036623):
            topic = self._topic_owning_info(dial_group, info_fid)
            assert topic is not None, f'HELO topic for {info_fid:08X} not found'
            pnam = struct.unpack(
                '<f', self._subrecords(topic)['PNAM'][0][:4])[0]
            assert pnam == 50.0, \
                f'bark topic PNAM must stay at the vanilla default, got {pnam}'

    def test_zero_stage_quests_keep_relative_priority_order(self):
        """Boosting staged quests above the zero-stage ceiling must be a
        uniform shift, not a clamp that collapses zero-stage quests together —
        125 vanilla zero-stage quests (Dark00General=50, MQConversations=85,
        ...) arbitrate AMONG EACH OTHER too (two factions' idle chatter
        competing for the same generic NPC); losing that ordering would hand
        the decision to file order instead."""
        from tes5_import.dialog_converter import build_dialog_groups
        from tes5_import.text_reader import set_formid_index_offset
        set_formid_index_offset(0)
        writer = PluginWriter(masters=['Skyrim.esm'])

        staged = {'Signature': 'QUST', 'FormID': '00035713',
                 'EditorID': 'RealQuest', 'DATA.Flags': '0',
                 'DATA.Priority': '60', 'StageCount': '1',
                 'Stage[0].Index': '10'}
        # Two zero-stage containers whose ORIGINAL priorities (85 vs 50) must
        # stay ordered the same way after both get squeezed below 60.
        high_container = {'Signature': 'QUST', 'FormID': '00035715',
                          'EditorID': 'HighContainer', 'DATA.Flags': '1',
                          'DATA.Priority': '85', 'StageCount': '0'}
        low_container = {'Signature': 'QUST', 'FormID': '00035716',
                         'EditorID': 'LowContainer', 'DATA.Flags': '1',
                         'DATA.Priority': '50', 'StageCount': '0'}

        greeting = {'Signature': 'DIAL', 'FormID': '000000C8',
                   'EditorID': 'GREETING', 'FULL': 'GREETING',
                   'DATA.Type': '0', 'QuestCount': '1',
                   'Quest[0]': '00035713'}
        staged_info = {'Signature': 'INFO', 'FormID': '00036622',
                      'RecordFlags': '0', 'ParentDIAL': '000000C8',
                      'DATA.Flags': '0', 'QSTI.Quest': '00035713',
                      'ResponseCount': '1', 'Response[0].EmotionType': '0',
                      'Response[0].EmotionValue': '50',
                      'Response[0].ResponseNumber': '1',
                      'Response[0].ResponseText': 'The staged briefing line.'}
        hello = {'Signature': 'DIAL', 'FormID': '000000D2',
                'EditorID': 'HELLO', 'FULL': 'HELLO',
                'DATA.Type': '1', 'QuestCount': '1',
                'Quest[0]': '00035715'}
        high_info = {'Signature': 'INFO', 'FormID': '00036624',
                    'RecordFlags': '0', 'ParentDIAL': '000000D2',
                    'DATA.Flags': '0', 'QSTI.Quest': '00035715',
                    'ResponseCount': '1', 'Response[0].EmotionType': '0',
                    'Response[0].EmotionValue': '50',
                    'Response[0].ResponseNumber': '1',
                    'Response[0].ResponseText': 'High-priority container line.'}
        goodbye = {'Signature': 'DIAL', 'FormID': '000000D4',
                  'EditorID': 'GOODBYE', 'FULL': 'GOODBYE',
                  'DATA.Type': '1', 'QuestCount': '1',
                  'Quest[0]': '00035716'}
        low_info = {'Signature': 'INFO', 'FormID': '00036625',
                   'RecordFlags': '0', 'ParentDIAL': '000000D4',
                   'DATA.Flags': '0', 'QSTI.Quest': '00035716',
                   'ResponseCount': '1', 'Response[0].EmotionType': '0',
                   'Response[0].EmotionValue': '50',
                   'Response[0].ResponseNumber': '1',
                   'Response[0].ResponseText': 'Low-priority container line.'}

        by_type = {'QUST': [staged, high_container, low_container],
                  'DIAL': [greeting, hello, goodbye],
                  'INFO': [staged_info, high_info, low_info]}
        build_dialog_groups(by_type, writer, npc_to_vtyp={})

        # Measured on QUST.DNAM.Priority — the byte the engine arbitrates on.
        # (The topics' PNAM all stay at the vanilla 50.0 default, so ordering
        # cannot be read there; see
        # test_zero_stage_quest_never_outranks_staged_greeting.)
        from tes5_import.dialog_converter import compute_quest_priorities
        pri = compute_quest_priorities(by_type)
        staged_prio = pri[0x00035713]
        high_prio = pri[0x00035715]
        low_prio = pri[0x00035716]
        assert high_prio > low_prio, \
            ('zero-stage quests lost their relative order: '
             f'HighContainer(was 85)={high_prio} <= LowContainer(was 50)={low_prio}')
        assert high_prio < staged_prio and low_prio < staged_prio, \
            'both containers must still sit below the staged quest'

    def _topic_owning_info(self, dial_group_bytes, info_fid):
        """Find the DIAL record whose group directly contains an INFO with
        this FormID (walks GRUP boundaries the way real ESM nesting does)."""
        pos = 0
        n = len(dial_group_bytes)
        current_dial = None
        while pos + RECORD_HEADER_SIZE <= n:
            rsig = dial_group_bytes[pos:pos + 4]
            size = struct.unpack_from('<I', dial_group_bytes, pos + 4)[0]
            if rsig == b'GRUP':
                pos += RECORD_HEADER_SIZE
                continue
            rfid = struct.unpack_from('<I', dial_group_bytes, pos + 12)[0]
            rec = dial_group_bytes[pos:pos + RECORD_HEADER_SIZE + size]
            pos += RECORD_HEADER_SIZE + size
            if rsig == b'DIAL':
                current_dial = rec
            elif rsig == b'INFO' and rfid == info_fid:
                return current_dial
        return None

    def _find_record(self, group_bytes, sig, formid, snam=None):
        """Find a record by (sig, formid) or by (sig, SNAM value) in a group."""
        pos = 0
        n = len(group_bytes)
        while pos + RECORD_HEADER_SIZE <= n:
            rsig = group_bytes[pos:pos + 4]
            size = struct.unpack_from('<I', group_bytes, pos + 4)[0]
            if rsig == b'GRUP':
                pos += RECORD_HEADER_SIZE   # descend into group contents
                continue
            rfid = struct.unpack_from('<I', group_bytes, pos + 12)[0]
            rec = group_bytes[pos:pos + RECORD_HEADER_SIZE + size]
            pos += RECORD_HEADER_SIZE + size
            if rsig != sig:
                continue
            if formid is not None and rfid != formid:
                continue
            if snam is not None:
                sn = self._subrecords(rec).get('SNAM')
                if not sn or struct.unpack('<I', sn[0][:4])[0] != snam:
                    continue
            return rec
        return None


class TestOutfitSplit:
    """TES4 inventory → TES5 outfit (OTFT) + carried inventory (CNTO).

    Skyrim wears exactly what the outfit lists and ADDS it on top of CNTO, so
    the split must be disjoint, wearable-only, and free of biped-slot ties.
    """

    # TES4 BMDT biped bits: 2=UpperBody 3=LowerBody 5=Foot
    BODY = 1 << 2
    LEGS = 1 << 3
    FEET = 1 << 5

    def _index(self, **types):
        """Install a fresh item index from {sig: [rec, ...]}."""
        from tes5_import.outfits import load_item_index
        from tes5_import.text_reader import set_formid_index_offset
        set_formid_index_offset(0)
        load_item_index(types)

    def _armo(self, fid, edid, slots, value=100):
        return {'Signature': 'ARMO', 'FormID': fid, 'EditorID': edid,
                'BMDT.BipedFlags': str(slots), 'DATA.Value': str(value)}

    def _clot(self, fid, edid, slots, value=5):
        return {'Signature': 'CLOT', 'FormID': fid, 'EditorID': edid,
                'BMDT.BipedFlags': str(slots), 'DATA.Value': str(value)}

    def _lvli(self, fid, edid, entries, chance_none=0):
        rec = {'Signature': 'LVLI', 'FormID': fid, 'EditorID': edid,
               'LVLD.ChanceNone': str(chance_none),
               'EntryCount': str(len(entries))}
        for i, e in enumerate(entries):
            rec[f'Entry[{i}].FormID'] = e
        return rec

    def test_only_wearables_reach_the_outfit(self):
        """Loot/keys/potions in an outfit are what the CK rejects with
        'contains non-armor objects' — they must stay in CNTO. Weapons must
        too: a survey of every vanilla Skyrim.esm OTFT found none containing a
        weapon — Skyrim's combat AI equips weapons from CNTO at runtime."""
        from tes5_import.outfits import split_inventory
        self._index(
            ARMO=[self._armo('00000001', 'Cuirass', self.BODY)],
            WEAP=[{'Signature': 'WEAP', 'FormID': '00000002', 'EditorID': 'Axe'}],
            KEYM=[{'Signature': 'KEYM', 'FormID': '00000003', 'EditorID': 'Key'}],
            ALCH=[{'Signature': 'ALCH', 'FormID': '00000004', 'EditorID': 'Potion'}],
            INGR=[{'Signature': 'INGR', 'FormID': '00000005', 'EditorID': 'Herb'}],
        )
        outfit, carried = split_inventory([(i, 1) for i in range(1, 6)])
        assert outfit == [1]                            # armor only
        assert [f for f, _ in carried] == [2, 3, 4, 5]  # weapon, key, potion, ingredient

    def test_outfit_and_inventory_are_disjoint(self):
        """Skyrim adds the outfit ON TOP of CNTO, so an item in both is
        carried twice — the duplicate-inventory bug."""
        from tes5_import.outfits import split_inventory
        self._index(
            ARMO=[self._armo('00000001', 'Cuirass', self.BODY)],
            KEYM=[{'Signature': 'KEYM', 'FormID': '00000002', 'EditorID': 'Key'}],
        )
        outfit, carried = split_inventory([(1, 1), (2, 1)])
        assert not set(outfit) & {f for f, _ in carried}

    def test_armor_beats_clothing_for_a_contested_slot(self):
        """An NPC issued both armor and clothes was meant to wear the armor."""
        from tes5_import.outfits import split_inventory
        self._index(
            ARMO=[self._armo('00000001', 'SteelCuirass', self.BODY, value=180)],
            CLOT=[self._clot('00000002', 'Shirt', self.BODY, value=5)],
        )
        outfit, carried = split_inventory([(2, 1), (1, 1)])  # shirt listed first
        assert outfit == [1]
        assert [f for f, _ in carried] == [2]  # loser is carried, not dropped

    def test_leveled_clothing_list_loses_to_armor(self):
        """Azzan: his steel competed with LL0NPCClothingShirt/Pants/ShoesMiddle,
        not with plain CLOT records. A leveled list must claim the union of its
        leaves' slots or it silently wins the slot and the NPC wears the shirt.
        """
        from tes5_import.outfits import split_inventory
        self._index(
            ARMO=[self._armo('00000001', 'SteelCuirass', self.BODY, value=180)],
            CLOT=[self._clot('00000010', 'MiddleShirt', self.BODY | self.LEGS)],
            LVLI=[self._lvli('00000002', 'LL0NPCClothingShirtMiddle',
                             ['00000010'])],
        )
        outfit, carried = split_inventory([(1, 1), (2, 1)])
        assert outfit == [1], 'armor must win the body slot over a clothing list'
        assert [f for f, _ in carried] == [2]

    def test_multislot_loser_cannot_win_on_a_second_slot(self):
        """A garment spanning body+legs that loses the body slot to a cuirass
        must not survive by winning legs — Skyrim would equip it and it would
        cover the chest again (the LL0VampireShirt case)."""
        from tes5_import.outfits import split_inventory
        self._index(
            ARMO=[self._armo('00000001', 'Cuirass', self.BODY, value=4800)],
            CLOT=[self._clot('00000002', 'Shirt', self.BODY | self.LEGS)],
        )
        outfit, carried = split_inventory([(1, 1), (2, 1)])
        assert outfit == [1]
        assert [f for f, _ in carried] == [2]

    def test_repeated_sublist_is_not_a_cycle(self):
        """Oblivion weights an entry by naming it twice (LL2NPCStaff25 lists
        LL1NPCStaff1Normal100 twice). A visited-set shared across siblings reads
        the repeat as a cycle and rejects the whole list — which left every
        leveled-weapon actor unarmed. A weapon-only list is never outfit
        material (weapons are carried, not worn), so it must resolve as
        non-wearable rather than erroring out from the false cycle."""
        from tes5_import.outfits import is_outfit_eligible, split_inventory
        self._index(
            WEAP=[{'Signature': 'WEAP', 'FormID': '00000010', 'EditorID': 'Staff'}],
            LVLI=[
                self._lvli('00000011', 'LL1NPCStaffNormal', ['00000010']),
                # names the same sublist twice, to weight it
                self._lvli('00000001', 'LL2NPCStaff25',
                           ['00000011', '00000011']),
            ],
        )
        assert is_outfit_eligible(0x01) is False
        outfit, carried = split_inventory([(1, 1)])
        assert outfit == [], 'a weapon list is carried, never worn'
        assert carried == [(1, 1)]

    def test_mixed_leveled_list_stays_in_inventory(self):
        """A list that can roll gold/ingredients is not a valid outfit form."""
        from tes5_import.outfits import is_outfit_eligible
        self._index(
            ARMO=[self._armo('00000010', 'Cuirass', self.BODY)],
            MISC=[{'Signature': 'MISC', 'FormID': '00000011', 'EditorID': 'Gold'}],
            LVLI=[self._lvli('00000001', 'LL0Loot', ['00000010', '00000011'])],
        )
        assert is_outfit_eligible(0x01) is False

    def test_empty_leveled_list_is_not_outfit_eligible(self):
        """An outfit entry that resolves to nothing is the CK's
        'Unable to find valid outfit form'."""
        from tes5_import.outfits import is_outfit_eligible
        self._index(LVLI=[self._lvli('00000001', 'LL0Empty', [])])
        assert is_outfit_eligible(0x01) is False

    def test_jewelry_does_not_contend(self):
        """Rings/amulets never conflict with armor, and an NPC wears two rings."""
        from tes5_import.outfits import split_inventory
        ring_r, ring_l, amulet = 1 << 6, 1 << 7, 1 << 8
        self._index(CLOT=[
            self._clot('00000001', 'Ring1', ring_r),
            self._clot('00000002', 'Ring2', ring_l),
            self._clot('00000003', 'Amulet', amulet),
        ])
        outfit, carried = split_inventory([(1, 1), (2, 1), (3, 1)])
        assert sorted(outfit) == [1, 2, 3]
        assert carried == []

    def test_formid_offset_is_tolerated(self):
        """Callers pass FormIDs from get_formid(), which has already applied the
        load-order offset (0x00xxxxxx → 0x01xxxxxx). An unmasked index lookup
        misses every record and the actor gets no outfit at all."""
        from tes5_import.outfits import is_outfit_eligible
        self._index(ARMO=[self._armo('00000001', 'Cuirass', self.BODY)])
        assert is_outfit_eligible(0x01000001) is True

    def test_chance_armor_keeps_guaranteed_clothing_fallback(self):
        """The bandit-with-no-pants bug. A bandit pairs a guaranteed clothing
        base (LL0NPCClothingPantsLower, ChanceNone 0) under chance-based armor
        (LL0NPCArmorLightGreaves25, ChanceNone 75 — the "25" being 25% odds).
        Both claim LowerBody. The probabilistic greaves must NOT evict the
        guaranteed pants: Skyrim resolves the outfit once and, ~75% of the time
        the greaves roll nothing, so evicting the pants leaves the actor
        bare-legged. Keeping both lets the engine wear greaves when they roll
        and the pants otherwise."""
        from tes5_import.outfits import split_inventory
        self._index(
            ARMO=[self._armo('00000010', 'IronGreaves', self.LEGS, value=1000)],
            CLOT=[self._clot('00000011', 'Pants', self.LEGS, value=1)],
            LVLI=[
                self._lvli('00000001', 'LL0NPCArmorLightGreaves25',
                           ['00000010'], chance_none=75),
                self._lvli('00000002', 'LL0NPCClothingPantsLower',
                           ['00000011'], chance_none=0),
            ],
        )
        outfit, carried = split_inventory([(1, 1), (2, 1)])
        assert set(outfit) == {1, 2}, \
            'guaranteed pants must stay when the greaves that outrank it can roll none'
        assert carried == []

    def test_guaranteed_armor_still_evicts_guaranteed_clothing(self):
        """The fallback rule must not weaken Azzan: a GUARANTEED armor list
        (ChanceNone 0) still evicts the guaranteed clothing under it — that
        slot will always be filled by the armor, so the clothes would only
        double up. Only a *probabilistic* winner keeps the fallback."""
        from tes5_import.outfits import split_inventory
        self._index(
            ARMO=[self._armo('00000010', 'SteelGreaves', self.LEGS, value=1000)],
            CLOT=[self._clot('00000011', 'Pants', self.LEGS, value=1)],
            LVLI=[
                self._lvli('00000001', 'LL0NPCArmorGreaves100',
                           ['00000010'], chance_none=0),
                self._lvli('00000002', 'LL0NPCClothingPantsLower',
                           ['00000011'], chance_none=0),
            ],
        )
        outfit, carried = split_inventory([(1, 1), (2, 1)])
        assert outfit == [1], 'guaranteed armor closes the slot to the clothing'
        assert [f for f, _ in carried] == [2]

    def test_nested_chance_none_breaks_guarantee(self):
        """A guarantee must hold all the way down. An outer list with
        ChanceNone 0 whose entry is a ChanceNone-75 sublist is NOT guaranteed,
        so it cannot evict a guaranteed peer sharing the slot."""
        from tes5_import.outfits import split_inventory
        self._index(
            ARMO=[self._armo('00000010', 'Greaves', self.LEGS, value=1000)],
            CLOT=[self._clot('00000011', 'Pants', self.LEGS, value=1)],
            LVLI=[
                self._lvli('00000003', 'InnerChance', ['00000010'],
                           chance_none=75),
                self._lvli('00000001', 'OuterSure', ['00000003'],
                           chance_none=0),
                self._lvli('00000002', 'GuaranteedPants', ['00000011'],
                           chance_none=0),
            ],
        )
        outfit, carried = split_inventory([(1, 1), (2, 1)])
        assert set(outfit) == {1, 2}, \
            'a chance-none anywhere on the path breaks the guarantee'
        assert carried == []

    def test_nothing_is_lost(self):
        """Every source item must reach the actor via exactly one channel."""
        from tes5_import.outfits import split_inventory
        self._index(
            ARMO=[self._armo('00000001', 'Cuirass', self.BODY),
                  self._armo('00000002', 'Boots', self.FEET)],
            CLOT=[self._clot('00000003', 'Shirt', self.BODY)],
            KEYM=[{'Signature': 'KEYM', 'FormID': '00000004', 'EditorID': 'Key'}],
        )
        items = [(1, 1), (2, 1), (3, 1), (4, 2)]
        outfit, carried = split_inventory(items)
        assert len(outfit) + len(carried) == len(items)
        assert set(outfit) | {f for f, _ in carried} == {1, 2, 3, 4}
        # counts on carried items survive the split
        assert dict(carried)[4] == 2


class TestCKWarningFixes:
    """Regressions for the 2026-07 CK_WARNINGS sweep."""

    def test_engine_formids_not_remapped(self):
        # PlayerRef 0x14 exists in NO data file (engine-hardcoded, same id in
        # Skyrim) — remapping it to 0x01000014 dangles every package/alias
        # reference to the player. Other low ids (Tamriel 0x3C!) are REAL
        # Oblivion.esm records and must keep remapping.
        from tes5_import.text_reader import set_formid_index_offset
        set_formid_index_offset(1)
        try:
            rec = {'A': '00000014', 'B': '00000100', 'C': '0000003C'}
            assert get_formid(rec, 'A') == 0x14           # PlayerRef stays put
            assert get_formid(rec, 'B') == 0x01000100     # real ids remap
            assert get_formid(rec, 'C') == 0x0100003C     # Tamriel IS remapped
        finally:
            set_formid_index_offset(0)

    def test_null_package_target_is_self(self):
        # A type-0 "Specific Reference" with FormID 0 is the CK's "Unable to
        # find Package Target Reference (00000000)"; vanilla's filler is
        # type 6 = Self.
        from tes5_import.pack_converter import _null_target
        assert struct.unpack('<iIi', _null_target())[0] == 6

    def test_player_ambush_becomes_forcegreet(self):
        """A player-targeted TES4 Ambush is a FORCE GREET.

        Skyrim has no Papyrus "walk over and talk to the player" call — the
        force greet is the PACKAGE. Every field below was wrong at some point
        and each one on its own stops the conversation:
          * template must be ForceGreet (0003C1C4), not Follow/HoldPosition
          * the Topic input (PDTO) names the dialogue to open
          * Location slots must NOT default to type 3 "near editor location",
            or the actor walks back to his CK spot (Uriel up the stairs)
          * the leash/forcegreet distances anchor on the PLAYER (type 0, 0x14)
          * PKDT interrupt flags must AUTHORISE speaking (vanilla 0xFEFF); the
            0x0000 default denies every interrupt and no greet can fire
        """
        from tes5_import.pack_converter import (_choose, PackContext,
                                                convert_flags, build_pkdt,
                                                SPEED_RUN, T4_AMBUSH)
        from tes5_import.pack_templates import FORCE_GREET
        rec = {'Signature': 'PACK', 'FormID': '0002C2F0',
               'EditorID': 'CGEmperorGreetPlayerInCell',
               'PKDT.Type': str(T4_AMBUSH), 'PKDT.Flags': '5124',
               'PLDT.Type': '2', 'PLDT.Location': '0', 'PLDT.Radius': '500',
               'PTDT.Type': '0', 'PTDT.Target': '00000014',
               'PTDT.Count': '100'}
        inp = _choose(rec, PackContext(), 0x0102C2F0)
        assert inp.t is FORCE_GREET
        blob = inp.emit()
        assert b'Topic' in blob and b'PDTO' in blob
        # no Location slot may be left as the "near editor location" filler
        for slot in (1, 2, 3, 7):
            assert slot in inp.values, f'Location slot {slot} unset'
        def _pldt(v):
            return v if isinstance(v, tuple) else struct.unpack('<iIi', v)
        assert _pldt(inp.values[3])[:2] == (0, 0x14)
        assert _pldt(inp.values[7])[:2] == (0, 0x14)
        # the weapon-drawn/sneak ambush flags must NOT be applied
        flags, _speed = convert_flags(5124, T4_AMBUSH, hostile_ambush=False)
        assert not (flags & 0x00800000), 'weapon drawn on a force greet'
        # interrupts must be authorised, exactly as vanilla force-greets are
        pkdt = build_pkdt(0, SPEED_RUN, interrupt=0xFEFF)
        assert struct.unpack('<IBBBBHH', pkdt)[5] == 0xFEFF

    def test_find_at_operable_object_becomes_activate(self):
        """A TES4 "Find" aimed at a lever/switch/door means GO OPERATE IT.

        Oblivion's Find is a seek-*then-use* procedure. Skyrim has no
        standalone equivalent, so these were falling through to Sandbox — and
        because this idiom carries no PLDT, the Sandbox had an EMPTY location:
        the actor stood inert beside the object forever and the object's
        OnActivate script never ran.

        CharacterGen is the visible case. `CGRatAmbushAPushBricks` is a Find
        at CGCrumbleWall01REF (base ACTI). The rat is supposed to push the
        bricks down, which fires CGCrumbleWall01SCRIPT.OnActivate ->
        `setstage MQ01 24` -> the rats turn hostile. With the Sandbox the wall
        never crumbled and the rats never became hostile, no matter how long
        the player waited, and the tutorial dead-ended.

        Skyrim expresses exactly this with the Activate template (00019B2D):
        24 vanilla instances, including MQ101HadvarOpenGate2,
        MS02BorkulOpenDoorPackage and TG08AKarliahOpenGatePackage.

        24 Oblivion Find packages target an ACTI/DOOR/CONT ref this way.
        Find at an NPC (230 packages) is a greet, not an operate, and must
        keep sandboxing.
        """
        from tes5_import.pack_converter import _choose, PackContext, T4_FIND
        from tes5_import.pack_templates import ACTIVATE, TRAVEL

        rec = {'Signature': 'PACK', 'FormID': '0007303D',
               'EditorID': 'CGRatAmbushAPushBricks',
               'PKDT.Type': str(T4_FIND), 'PKDT.Flags': '8196',
               'PTDT.Type': '0', 'PTDT.Target': '0003E31C',
               'PTDT.Count': '200'}
        ctx = PackContext(ref_base_sig={0x03E31C: 'ACTI'})
        inp = _choose(rec, ctx, 0x0007303D)
        assert inp.t is ACTIVATE, \
            'rat sandboxes instead of pushing the wall; MQ01 never reaches 24'
        # the wall ref must actually reach the Target slot
        target = inp.values[ACTIVATE.slot('target')]
        assert struct.unpack('<iIi', target)[1] == 0x0003E31C

        # a door is the same idiom (SE06GSWardenUnlockMainDoor)
        door = dict(rec, **{'PTDT.Target': '0001AAAA'})
        assert _choose(door, PackContext(ref_base_sig={0x01AAAA: 'DOOR'}),
                       0x0007303E).t is ACTIVATE

        # ...but Find at an ACTOR is a SEEK, not an operate: it becomes Travel
        # to a "near reference" location, with PTDT.Count as the radius.
        # (CGAssassinsAmbushAToGlenroy et al: the package that carries the
        # assassins out of the ambush room; the old sandbox fallback left them
        # standing there forever.)
        npc = dict(rec, **{'PTDT.Target': '00023E71'})
        seek = _choose(npc, PackContext(ref_base_sig={0x023E71: 'NPC_'}),
                       0x0007303F)
        assert seek.t is TRAVEL, 'Find at an actor must not become Activate'
        loc = seek.values[TRAVEL.slot('location')]
        ltype, lfid, lradius = struct.unpack('<iIi', loc)
        assert (ltype, lfid) == (0, 0x00023E71)
        assert lradius == 200, 'PTDT.Count is the approach distance'

    def test_defensive_combat_is_not_ignore_combat(self):
        """TES4 Defensive Combat must NOT become TES5 Ignore Combat.

        Both sit on bit 20 but mean opposite things:
          TES4 Defensive Combat -> do not START fights, but DO fight back
          TES5 Ignore Combat    -> take no part in combat at all

        Mapping one onto the other told every Oblivion bodyguard to stand
        still and be killed. CharacterGen's `CGGlenroyDefendEmperorAmbushA` —
        the package whose whole job is defending the Emperor — carries the TES4
        flag, so the converted Blades drew their swords and then watched the
        assassins kill Renault unopposed.

        Skyrim has no Defensive Combat equivalent and needs none: the
        aggression tier decides whether an actor initiates, and everyone
        retaliates when attacked. TES5's default IS TES4 Defensive Combat, so
        the bit is dropped. 388 of 7,209 TES4 packages set it.
        """
        from tes5_import.pack_converter import (convert_flags,
                                                T4_DEFENSIVE_COMBAT,
                                                T5_IGNORE_COMBAT, T4_ALWAYS_RUN)
        flags, _ = convert_flags(T4_DEFENSIVE_COMBAT, 6, True)
        assert not (flags & T5_IGNORE_COMBAT), \
            'Defensive Combat became Ignore Combat — bodyguards will not fight'
        # The real CGGlenroyDefendEmperorAmbushA value (AlwaysRun|Defensive).
        flags, speed = convert_flags(0x00402000, 6, True)
        assert not (flags & T5_IGNORE_COMBAT)
        assert flags & 0x00002000, 'AlwaysRun must still map to Preferred Speed'
        # Unrelated flags on the same package are unaffected.
        flags, _ = convert_flags(T4_ALWAYS_RUN, 6, True)
        assert not (flags & T5_IGNORE_COMBAT)

    def test_spel_cast_type_fire_and_forget(self):
        from tes5_import.record_types.equipment import convert_SPEL
        rec = {'Signature': 'SPEL', 'FormID': '00001234', 'RecordFlags': '0',
               'EditorID': 'TestSpell', 'FULL': 'Test', 'SPIT.Cost': '10',
               'SPIT.Flags': '0', 'SPIT.Type': '0', 'EffectCount': '1',
               'Effect[0].EFID': 'FIDG', 'Effect[0].Type': 'Target',
               'Effect[0].Magnitude': '10', 'Effect[0].Area': '0',
               'Effect[0].Duration': '0'}
        spit = _find_subrecord(convert_SPEL(rec), b'SPIT')
        cast_type = struct.unpack_from('<I', spit, 16)[0]
        assert cast_type == 1  # Fire and Forget (2 = Concentration)

    def test_sgst_scroll_has_effects_and_etyp(self):
        # Sigil stones -> SCRL used to carry ZERO effects ("Magic Item has no
        # effects defined", one per stone) and no equip type.
        from tes5_import.record_types.equipment import convert_SGST
        rec = {'Signature': 'SGST', 'FormID': '00001234', 'RecordFlags': '0',
               'EditorID': 'TestSigil', 'FULL': 'Sigil Stone',
               'DATA.Value': '100', 'DATA.Weight': '1.0', 'EffectCount': '1',
               'Effect[0].EFID': 'SHLD', 'Effect[0].Type': 'Self',
               'Effect[0].Magnitude': '10', 'Effect[0].Area': '0',
               'Effect[0].Duration': '120'}
        out = convert_SGST(rec)
        assert _find_subrecord(out, b'EFID') is not None
        assert struct.unpack('<I', _find_subrecord(out, b'ETYP'))[0] == 0x00013F44
        spit = _find_subrecord(out, b'SPIT')
        assert struct.unpack_from('<I', spit, 16)[0] == 3  # CastType Scroll

    def test_aimed_ench_gets_projectile_mgef(self):
        # An AIMED enchantment whose effects all map to projectile-less
        # Alch* MGEFs fires NOTHING in game; the converter must synthesize an
        # aimed MGEF clone with a projectile and swap it in.
        from tes5_import import magic_effects
        from tes5_import.record_types.equipment import convert_ENCH
        magic_effects.set_tes4_effect_names([])   # reset cache
        writer = PluginWriter(masters=['Skyrim.esm'])
        writer.next_object_id = 0x01100000
        rec = {'Signature': 'ENCH', 'FormID': '00001234', 'RecordFlags': '0',
               'EditorID': 'TestStaffEnch', 'FULL': 'Drain Staff',
               'ENIT.Type': '1', 'ENIT.Charge': '100', 'ENIT.Cost': '10',
               'ENIT.Flags': '0', 'EffectCount': '1',
               'Effect[0].EFID': 'DRHE', 'Effect[0].Type': 'Target',
               'Effect[0].Magnitude': '20', 'Effect[0].Area': '0',
               'Effect[0].Duration': '0'}
        out = convert_ENCH(rec, writer=writer)
        efid = struct.unpack('<I', _find_subrecord(out, b'EFID'))[0]
        assert efid != 0x0003EB42          # not plain AlchDamageHealth
        mgefs = writer._top_groups.get('MGEF')
        assert mgefs and len(mgefs) == 1
        data = _find_subrecord(mgefs[0], b'DATA')
        assert struct.unpack_from('<I', data, 0x48)[0] != 0   # projectile
        assert struct.unpack_from('<I', data, 0x50)[0] == 1   # fire&forget
        assert struct.unpack_from('<I', data, 0x54)[0] == 2   # aimed
        # second conversion reuses the cached clone
        convert_ENCH(rec, writer=writer)
        assert len(writer._top_groups['MGEF']) == 1

    def test_aimed_ench_using_converted_mgef_reaches_a_projectile(self):
        # The crash this pins: an Aimed ENCH whose effects all have a null
        # MGEF Projectile is an UNCONDITIONAL null-deref, not just a dud cast.
        # MagicItem::GetCostliestEffectItem skips every projectile-less effect
        # when delivery == Aimed and returns null; the combat-AI item rating
        # function then does `mov rdi,[rax+0xC8]` with no null check.
        # (Nehrim "Stab des Frosts" / EnStaffFrostDamage + FRDG, 2026-08-01.)
        #
        # The older test above exercises the VANILLA-alias fallback because it
        # never registers converted MGEFs.  This one registers them, which is
        # the normal path since convert_MGEF landed, and is what shipped broken.
        from tes5_import import magic_effects
        from tes5_import.record_types.equipment import convert_ENCH
        from tes5_import.record_types.magic import (convert_MGEF,
                                                    register_mgef_formids)
        magic_effects.set_tes4_effect_names([])
        # FRDG exactly as Nehrim/Oblivion export it: Destruction, Frost resist,
        # Hostile|Detrimental|Target.
        mgef = {'Signature': 'MGEF', 'FormID': '0000187B', 'RecordFlags': '0',
                'EditorID': 'FRDG', 'FULL': 'Frost Damage',
                'DATA.Flags': str(0x00000001 | 0x00000004 | 0x00000040),
                'DATA.BaseCost': '1.0', 'DATA.School': '2',
                'DATA.ResistValue': '62'}
        register_mgef_formids([mgef])

        writer = PluginWriter(masters=['Skyrim.esm'])
        writer.next_object_id = 0x01100000
        rec = {'Signature': 'ENCH', 'FormID': '0000C211', 'RecordFlags': '0',
               'EditorID': 'EnStaffFrostDamage', 'ENIT.Type': '1',
               'ENIT.Charge': '62', 'ENIT.Cost': '62', 'ENIT.Flags': '1',
               'EffectCount': '1', 'Effect[0].EFID': 'FRDG',
               'Effect[0].Type': 'Target', 'Effect[0].Magnitude': '40',
               'Effect[0].Area': '0', 'Effect[0].Duration': '0',
               'Effect[0].ActorValue': '8'}
        out = convert_ENCH(rec, writer=writer)

        enit = _find_subrecord(out, b'ENIT')
        assert struct.unpack_from('<I', enit, 16)[0] == 2, 'expected Aimed'
        efid = struct.unpack('<I', _find_subrecord(out, b'EFID'))[0]

        # The effect must resolve to our converted MGEF, and that MGEF's DATA
        # must carry a projectile.
        assert magic_effects.has_projectile(efid), \
            'aimed ENCH reaches no projectile — engine null-derefs on it'
        data = _find_subrecord(convert_MGEF(mgef), b'DATA')
        assert struct.unpack_from('<I', data, 84)[0] == 2      # delivery Aimed
        # Frost + Destruction + fire-and-forget -> FrostIcicleProjectile01,
        # the projectile vanilla uses for that exact combination.
        assert struct.unpack_from('<I', data, 72)[0] == 0x0002F774

    def test_non_aimed_mgef_keeps_null_projectile(self):
        # Only Aimed/Target-Location deliveries fly a projectile; vanilla
        # leaves Self and Contact null far more often than not, so the fix
        # must not blanket-assign one.
        from tes5_import.record_types.magic import convert_MGEF
        rec = {'Signature': 'MGEF', 'FormID': '00001234', 'RecordFlags': '0',
               'EditorID': 'REHE', 'FULL': 'Restore Health',
               'DATA.Flags': str(0x00000010),   # Self only
               'DATA.BaseCost': '1.0', 'DATA.School': '5'}
        data = _find_subrecord(convert_MGEF(rec), b'DATA')
        assert struct.unpack_from('<I', data, 84)[0] == 0      # delivery Self
        assert struct.unpack_from('<I', data, 72)[0] == 0      # no projectile

    def test_leveled_list_drops_null_entries(self):
        rec = {'Signature': 'LVLI', 'FormID': '00001234', 'RecordFlags': '0',
               'EditorID': 'TestList', 'LVLD.ChanceNone': '0',
               'EntryCount': '3',
               'Entry[0].Level': '1', 'Entry[0].FormID': '00000F00',
               'Entry[0].Count': '-100',    # TES4 restock semantics
               'Entry[1].Level': '5',       # missing FormID -> dropped
               'Entry[2].Level': '10', 'Entry[2].FormID': '00000F01',
               'Entry[2].Count': '2'}
        out = convert_LVLI(rec)
        llct = _find_subrecord(out, b'LLCT')
        assert llct is not None and llct[0] == 2
        lvlos = _find_all_subrecords(out, b'LVLO')
        assert len(lvlos) == 2
        for lvlo in lvlos:
            level, fid, count = struct.unpack('<HxxIHxx', lvlo)
            assert fid != 0
            assert count >= 1

    def test_container_negative_counts_normalized(self):
        rec = {'Signature': 'CONT', 'FormID': '00001234', 'RecordFlags': '0',
               'EditorID': 'TestChest', 'FULL': 'Chest',
               'Item[0].FormID': '00000F00', 'Item[0].Count': '-100',
               'Item[1].FormID': '00000F01', 'Item[1].Count': '0',
               'DATA.Flags': '2', 'DATA.Weight': '0.0'}
        out = convert_CONT(rec)
        counts = [struct.unpack('<Ii', c)[1]
                  for c in _find_all_subrecords(out, b'CNTO')]
        assert counts == [100, 1]

    def test_footstep_sets_exist_in_skyrim(self):
        # The old Light/Clothing constants (0x24238/0x24237) were FormIDs
        # that do not exist in Skyrim.esm at all.
        from tes5_import.skyrim_overrides import (
            CLOTHING_FOOTSTEP_SET, HEAVY_ARMOR_FOOTSTEP_SET,
            LIGHT_ARMOR_FOOTSTEP_SET)
        assert HEAVY_ARMOR_FOOTSTEP_SET == 0x00021487
        assert LIGHT_ARMOR_FOOTSTEP_SET == 0x00021486
        assert CLOTHING_FOOTSTEP_SET == 0x00021468

    def test_dataless_mapmarker_ref_grounded_to_xmarker(self):
        rec = {'Signature': 'REFR', 'FormID': '00001234', 'RecordFlags': '1024',
               'NAME': '00000010', 'ParentWRLD': '0000003C',
               'ParentCELL': '00023777',
               'PosX': '0.0', 'PosY': '0.0', 'PosZ': '0.0',
               'RotX': '0.0', 'RotY': '0.0', 'RotZ': '0.0'}
        out = convert_REFR(rec)
        name = struct.unpack('<I', _find_subrecord(out, b'NAME'))[0]
        assert name == 0x0000003B   # XMarker, not a marker-data-less MapMarker

    def test_doors_to_exteriors_never_claim_location(self):
        # A city-gate/Oblivion-gate door leads OUT to an exterior; claiming
        # the destination cell poisoned the worldspace's shared persistent
        # dummy cell, giving EVERY persistent ref in Tamriel one gate's
        # location ("Ref is not in its persistence location ..." x13, where
        # the CK then hangs).
        from tes5_import.locations import build_marker_locations
        writer = PluginWriter(masters=['Skyrim.esm'])
        writer.next_object_id = 0x01100000
        interior = {'Signature': 'CELL', 'FormID': '00000C01',
                    'EditorID': 'TestInterior', 'DATA.Flags': '1'}
        exterior = {'Signature': 'CELL', 'FormID': '00000C02',
                    'EditorID': 'TestExterior', 'DATA.Flags': '2',
                    'ParentWRLD': '00000A01'}
        wrld = {'Signature': 'WRLD', 'FormID': '00000A01',
                'EditorID': 'TestWorld', 'FULL': 'Test World'}
        marker = {'Signature': 'REFR', 'FormID': '00000E01',
                  'MapMarker': '1', 'MapMarker.FULL': 'Fort Test',
                  'ParentWRLD': '00000A01', 'PosX': '100.0', 'PosY': '100.0'}
        dest_int = {'Signature': 'REFR', 'FormID': '00000E02',
                    'ParentCELL': '00000C01'}
        dest_ext = {'Signature': 'REFR', 'FormID': '00000E03',
                    'ParentCELL': '00000C02'}
        door_to_int = {'Signature': 'REFR', 'FormID': '00000E04',
                       'ParentWRLD': '00000A01', 'PosX': '150.0',
                       'PosY': '100.0', 'XTEL.Door': '00000E02'}
        door_to_ext = {'Signature': 'REFR', 'FormID': '00000E05',
                       'ParentWRLD': '00000A01', 'PosX': '90.0',
                       'PosY': '100.0', 'XTEL.Door': '00000E03'}
        by_type = {'WRLD': [wrld], 'CELL': [interior, exterior],
                   'REFR': [marker, dest_int, dest_ext,
                            door_to_int, door_to_ext]}
        cell_to_location, _grid, _world = build_marker_locations(
            by_type, writer)
        assert get_formid(interior, 'FormID') in cell_to_location
        assert get_formid(exterior, 'FormID') not in cell_to_location


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])


class TestSayTopicRetarget:
    """Run-on-Target conditions in Say-driven topics (2026-07-19)."""

    def _raw_ctda(self, run_on_target=True, func=47):
        import struct
        # func defaults to GetItemCount(47) — a STATE query, which is what the
        # retarget exists for. Identity functions are exempt (see below).
        type_byte = 0x02 if run_on_target else 0x00   # CTDA_RUN_ON_TARGET
        return struct.pack('<B3xfHHII4x', type_byte, 1.0, func, 0, 0x00012345, 0)

    def test_retarget_to_reference(self):
        import struct
        from tes5_import.dialog_conditions import convert_ctda
        out = convert_ctda(self._raw_ctda(), offset=1,
                           run_on_target_ref=0x14)
        assert out is not None
        run_on, reference = struct.unpack_from('<II', out, 20)
        assert run_on == 2          # Reference
        assert reference == 0x14    # PlayerRef
        assert out[0] & 0x02 == 0   # flag bit cleared

    def test_drop_run_on_target(self):
        from tes5_import.dialog_conditions import convert_ctda
        assert convert_ctda(self._raw_ctda(), offset=1,
                            drop_run_on_target=True) is None

    def test_base_form_identity_conditions_are_never_retargeted(self):
        """GetIsID/GetIsClass answer by comparing a BASE FORM, so they must
        stay RunOn=Target and never be pinned to PlayerRef.

        PlayerRef's base is vanilla Skyrim's 0x00000007, never the converted
        TES4 player NPC_ 0x01000007, so retargeting makes them UNPASSABLE.
        That silently killed 667 GREETING/bark INFOs across 101 topics — every
        affected NPC lost their whole topic list because the greeting that
        opens it could not pass (Pinarus Inventius kept only 'rumors').

        Vanilla Skyrim.esm corroborates the split: GetIsID appears with
        RunOn=Reference ZERO times.
        """
        import struct
        from tes5_import.dialog_conditions import convert_ctda
        for func in (72, 68):
            raw = self._raw_ctda(func=func)
            # ...neither the retarget...
            out = convert_ctda(raw, offset=1, run_on_target_ref=0x14)
            assert out is not None, f'func {func} must not be dropped'
            run_on, reference = struct.unpack_from('<II', out, 20)
            assert (run_on, reference) == (1, 0), \
                f'identity func {func} was retargeted to {run_on}/{reference:#x}'
            # ...nor the drop applies to them.
            out = convert_ctda(raw, offset=1, drop_run_on_target=True)
            assert out is not None, f'identity func {func} must not be dropped'
            run_on, _ = struct.unpack_from('<II', out, 20)
            assert run_on == 1

    def test_actor_state_conditions_are_retargeted(self):
        """GetIsRace/GetIsSex/GetInFaction/GetFactionRank read live actor STATE,
        not a base-form identity, so a Say()-driven topic MUST pin them to the
        resolved target reference — Skyrim's Say has no target at all
        (ObjectReference.psc `Say(Topic, Actor akActorToSpeakAs, bool)`: arg 2
        is the SPEAKER), so RunOn=Target evaluates against nothing and the
        wrong response wins.

        Vanilla Skyrim.esm uses RunOn=Reference->PlayerRef for exactly these:
        GetIsSex 125x, GetInFaction 136x, GetIsRace 29x, GetFactionRank 22x.

        Left on RunOn=Target, Valen Dreth's per-race CharacterGen taunts all
        failed and only the single race-less line could play, so the tutorial
        never advanced past its first taunt.
        """
        import struct
        from tes5_import.dialog_conditions import convert_ctda
        # GetIsRace(69) is exercised separately: its PARAM is race-mapped and
        # this fixture's dummy FormID is not a real TES4 race.
        for func in (70, 71, 73):
            raw = self._raw_ctda(func=func)
            out = convert_ctda(raw, offset=1, run_on_target_ref=0x14)
            assert out is not None, f'func {func} must not be dropped'
            run_on, reference = struct.unpack_from('<II', out, 20)
            assert (run_on, reference) == (2, 0x14), \
                f'state func {func} not retargeted: {run_on}/{reference:#x}'

    def test_default_still_target(self):
        import struct
        from tes5_import.dialog_conditions import convert_ctda
        out = convert_ctda(self._raw_ctda(), offset=1)
        run_on, reference = struct.unpack_from('<II', out, 20)
        assert run_on == 1 and reference == 0

    def test_subject_condition_untouched(self):
        import struct
        from tes5_import.dialog_conditions import convert_ctda
        out = convert_ctda(self._raw_ctda(run_on_target=False), offset=1,
                           run_on_target_ref=0x14)
        run_on, reference = struct.unpack_from('<II', out, 20)
        assert run_on == 0 and reference == 0

    def test_engine_fixed_param_never_remapped(self):
        """GetIsID(Player 0x00000007) [Target] means "am I addressing the
        player" — the runtime player's base form is vanilla Skyrim's
        0x00000007, so the param must NOT be load-order shifted to the
        converted TES4 player copy (0x01000007 can never pass; 3,761 INFOs
        incl. every stage-gated reveal greeting died and their AddTopic-unlock
        fragments never ran — Pinarus lost his whole topic list, second cause
        after the identity-retarget bug)."""
        import struct
        from tes5_import.dialog_conditions import convert_ctda
        type_byte = 0x02                              # run-on-target
        raw = struct.pack('<B3xfHHII4x', type_byte, 1.0, 72, 0, 0x00000007, 0)
        out = convert_ctda(raw, offset=1)
        assert out is not None
        param1 = struct.unpack_from('<I', out, 12)[0]
        assert param1 == 0x00000007, \
            f'engine-fixed Player id was remapped to {param1:#010x}'


class TestAmbientChatterPacing:
    """NPCs quipped every few seconds anywhere, even mid-scripted-sequence
    (2026-07-25).

    Two causes, both from Skyrim-only mechanisms having no TES4 source:

    1. PKDT Interrupt Flags authorise an actor to INTERRUPT its package to
       speak. Oblivion has no such field -- its package flags cover doors,
       speed, sneak, equipment and combat only (xEdit wbPackageFlags; UESP
       "Oblivion Mod:Mod File Format/PACK"), and "No Idle Anims" is idle
       ANIMATIONS. The converter wrote 0xFFFF on all 7,209 packages, which per
       UESP is exactly what the CK's "Set all interrupt flags" button writes.
    2. Oblivion paces ambient dialogue GLOBALLY via GMSTs, which were skipped.
    """

    def test_interrupt_flags_not_force_enabled(self):
        from tes5_import.pack_converter import DEFAULT_INTERRUPT
        assert DEFAULT_INTERRUPT != 0xFFFF, \
            "0xFFFF is the CK's 'set all interrupt flags'; it forces every " \
            "NPC to be allowed to break off any activity to chatter"
        # Bit 0 is "Hellos to player"; vanilla Skyrim leaves it clear on 39.7%
        # of packages and TES4 provides nothing to derive it from.
        assert not (DEFAULT_INTERRUPT & 0x01)

    def test_pkdt_writes_the_interrupt_field(self):
        import struct
        from tes5_import.pack_converter import build_pkdt, DEFAULT_INTERRUPT
        b = build_pkdt(0, 2)
        assert len(b) == 12
        assert struct.unpack_from('<H', b, 8)[0] == DEFAULT_INTERRUPT

    def test_oblivion_pacing_gmsts_carried(self):
        """Oblivion is far slower than Skyrim on both ambient clocks; without
        these the converted game runs Skyrim's pacing over Oblivion's much
        larger line pool."""
        from tes5_import.constants import AMBIENT_GMST_OVERRIDES
        # Skyrim.esm ships 5.0 / 10.0 for these two.
        assert AMBIENT_GMST_OVERRIDES['fAIGreetingTimer'][0] == 20.0
        assert AMBIENT_GMST_OVERRIDES['fIdleChatterCommentTimer'][0] == 100.0
        for _edid, (value, is_float) in AMBIENT_GMST_OVERRIDES.items():
            assert is_float and isinstance(value, float)


class TestBarkResetTimer:
    """Ambient bark lines need a non-zero ENAM reset or the NPC repeats them
    forever (2026-07-25).

    The reset field is the engine's per-line lockout: once spoken, that INFO is
    ineligible until it expires, and when every greeting is on a timer the
    actor falls silent. TES4 has no such field, so the converter wrote 0 --
    meaning NO lockout, every line permanently re-playable, NPCs quipping on
    repeat. Vanilla Skyrim sets a real value on 65% of its HELO lines.
    """

    def test_reset_ticks_match_vanilla_half_hour(self):
        from tes5_import.dialog_converter import (_BARK_RESET_TICKS,
                                                  _BARK_RESET_HOURS)
        # The engine stores this as trunc(days * 65535); 1365 is the exact
        # value on 2809 of vanilla Skyrim.esm's 5287 HELO lines (53%).
        assert _BARK_RESET_TICKS == 1365
        assert abs(_BARK_RESET_TICKS / 65535 * 24 - _BARK_RESET_HOURS) < 0.001

    def _enam(self, rec, *, is_bark):
        import struct
        from tes5_import.dialog_converter import convert_INFO
        out = convert_INFO(rec, bark_dial_fids=set() if is_bark else None)
        pos = 24          # skip the record header; subrecords follow
        while pos + 6 <= len(out):
            sig = out[pos:pos + 4]
            size = struct.unpack_from('<H', out, pos + 4)[0]
            if sig == b'ENAM':
                return struct.unpack_from('<HH', out, pos + 6)
            pos += 6 + size
        raise AssertionError('no ENAM emitted')

    def test_bark_line_gets_reset(self):
        rec = {'FormID': '00001234', 'DATA.Flags': '0', 'ResponseCount': '0'}
        _flags, reset = self._enam(rec, is_bark=True)
        assert reset == 1365, 'ambient bark must carry a repeat lockout'

    def test_conversation_line_keeps_zero(self):
        """A player-selected topic is not ambient; the player controls when it
        plays, so a lockout would wrongly make it unavailable."""
        rec = {'FormID': '00001234', 'DATA.Flags': '0', 'ResponseCount': '0'}
        _flags, reset = self._enam(rec, is_bark=False)
        assert reset == 0

    def test_say_once_bark_keeps_zero(self):
        """Say-once (0x04) already locks permanently after one play; a reset
        would only weaken it."""
        rec = {'FormID': '00001234', 'DATA.Flags': '4', 'ResponseCount': '0'}
        flags, reset = self._enam(rec, is_bark=True)
        assert flags & 0x04, 'say-once flag must survive'
        assert reset == 0


class TestNpcToNpcConversationDrop:
    """Oblivion Type-1 Conversation topics are NPC-to-NPC chatter with no
    Skyrim equivalent short of a SCEN scene, so they are dropped rather than
    converted into player-menu entries labelled with their EditorID
    (2026-07-25). See docs/ambient_dialogue_channel_plan.md Step 1."""

    def _dial(self, edid, dtype=1, fid='00001234'):
        return {'EditorID': edid, 'DATA.Type': str(dtype), 'FormID': fid}

    def test_npc_to_npc_topic_is_dropped(self):
        import tes5_import.dialog_converter as dc
        dc._SAY_TOPIC_DISPOSITIONS.clear()
        dc._SAY_TOPIC_DISPOSITIONS[0x0000AAAA] = ('drop', None)   # unrelated
        try:
            assert dc.should_skip_dial(self._dial('SkingradNQDResponses'))
        finally:
            dc._SAY_TOPIC_DISPOSITIONS.clear()

    def test_script_driven_topic_is_kept(self):
        """CharGen's lines are spoken by an explicit Say/SayTo in a quest
        script — a real Skyrim Actor.Say(). Dropping Type-1 by DATA.Type alone
        would delete the whole tutorial conversation."""
        import tes5_import.dialog_converter as dc
        dc._SAY_TOPIC_DISPOSITIONS.clear()
        dc._SAY_TOPIC_DISPOSITIONS[0x00001234] = ('drop', None)
        try:
            assert not dc.should_skip_dial(
                self._dial('CharGenMain', fid='00001234'))
        finally:
            dc._SAY_TOPIC_DISPOSITIONS.clear()

    def test_named_keeps_survive(self):
        """INFOGENERAL is Oblivion's Rumors channel (a real Skyrim player
        topic); HELLO/GOODBYE are engine bark channels that happen to carry
        DIAL Type 1."""
        import tes5_import.dialog_converter as dc
        dc._SAY_TOPIC_DISPOSITIONS.clear()
        dc._SAY_TOPIC_DISPOSITIONS[0x0000BBBB] = ('drop', None)
        try:
            for name in ('INFOGENERAL', 'HELLO', 'GOODBYE'):
                assert not dc.should_skip_dial(self._dial(name)), name
        finally:
            dc._SAY_TOPIC_DISPOSITIONS.clear()

    def test_empty_say_map_drops_nothing(self):
        """FAIL-SAFE: an empty map means the say-driven scan has not run yet
        (dialog_unlocks.build_unlock_plan calls should_skip_dial long before
        build_dialog_groups populates it). Treating that as 'nothing is
        script-driven' would drop all 293 scripted topics including CharGen."""
        import tes5_import.dialog_converter as dc
        dc._SAY_TOPIC_DISPOSITIONS.clear()
        assert not dc.should_skip_dial(self._dial('SkingradNQDResponses'))

    def test_other_dial_types_unaffected(self):
        """Only Type 1 is NPC-to-NPC; Type 0 player topics must be untouched."""
        import tes5_import.dialog_converter as dc
        dc._SAY_TOPIC_DISPOSITIONS.clear()
        dc._SAY_TOPIC_DISPOSITIONS[0x0000CCCC] = ('drop', None)
        try:
            assert not dc.should_skip_dial(
                self._dial('SomePlayerTopic', dtype=0))
        finally:
            dc._SAY_TOPIC_DISPOSITIONS.clear()


class TestQuestJournalPlatformText:
    """MQ01's journal shipped a gamepad AND a keyboard variant per tutorial
    stage; Oblivion picked one at runtime, Skyrim renders both (2026-07-24)."""

    def test_gamepad_variant_dropped_when_pc_variant_exists(self):
        from tes5_import.dialog_converter import _pc_stage_texts
        out = _pc_stage_texts([
            '',
            'Use the left stick to move around. The right stick turns you.',
            'To move forward, &sUActnForward;. The mouse turns you.',
        ])
        assert out[1] is None, 'gamepad journal text must be dropped'
        assert 'left stick' not in (out[2] or '')
        assert out[2].startswith('To move forward, press W')

    def test_control_tokens_expanded(self):
        """&sUActnX; is an Oblivion UI token; Skyrim prints it verbatim."""
        from tes5_import.dialog_converter import _pc_stage_texts
        out = _pc_stage_texts(
            ['To ready your weapon, &sUActnRdyitem;. To block, '
             '&sUActnBlock;.'])
        assert '&sUActn' not in out[0]
        assert 'press R' in out[0] and 'right mouse button' in out[0]

    def test_ordinary_multi_entry_stage_untouched(self):
        """Only a clean gamepad/PC split may drop anything — a quest with two
        unrelated journal entries must keep both."""
        from tes5_import.dialog_converter import _pc_stage_texts
        texts = ['Kill the bandit leader.', 'Return to Jauffre.']
        assert _pc_stage_texts(texts) == texts


class TestSayLineDurations:
    """A converted Say() timer is charged the line's own MEASURED length.

    Papyrus Say() is fire-and-forget: it does not block or queue, and does
    nothing when no INFO under the topic qualifies. The owning script polls and
    re-issues Say() while its guard reads `timer <= 0`, but the INFO's End
    fragment — which advances the conversation — only runs when the line
    FINISHES. The timer's one job is to cover that window.

    Both extremes have been tried in game and each fails:
      * ZERO — the poller re-Says every tick, restarting the line so its
        fragment never runs (Valen Dreth repeats taunt 1 forever).
      * A large PARK only the fragment can clear — no line means no fragment, so
        it strands and the scene HALTS (CharacterGen's prison-cell gap/stall).
    The line's own length is the smallest value that covers the window, and the
    fragment clears it the moment the line really ends, so it adds no silence.
    """

    def test_call_site_charges_the_measured_line_length(self):
        from script_convert.converter import (ScriptConverter,
                                              SAY_LINE_SECONDS)
        from script_convert.cross_ref import CrossRefGraph
        conv = ScriptConverter(CrossRefGraph())
        saved = ScriptConverter.say_durations
        try:
            ScriptConverter.say_durations = {'chargentaunt2': 14.63}
            # Never zero — that re-Says the line every tick.
            assert conv._say_seconds('Self.Say(CharGenTaunt2)') == 14.63
            # Unmeasured topics fall back to the generic stand-in, still > 0.
            assert conv._say_seconds('Self.Say(X)') == SAY_LINE_SECONDS
            assert SAY_LINE_SECONDS > 0
        finally:
            ScriptConverter.say_durations = saved

    def test_parked_timer_read_back_does_not_leak_the_sentinel(self):
        """`convTimer = timer - .5` must not propagate the park value."""
        from script_convert.converter import ScriptConverter
        from script_convert.cross_ref import CrossRefGraph
        conv = ScriptConverter(CrossRefGraph())
        conv._parked_timers.add('timer')
        # a subtraction means "no extra wait"
        assert conv._resolve_parked_timer_expr('timer - 0.5') == '0'
        # a bare read likewise
        assert conv._resolve_parked_timer_expr('timer') == '0'
        # an ADD is a deliberate beat: it must be REDIRECTED out of the timer
        # (which decays while the line plays) into the pending-beat companion,
        # signalled by the __BEAT__ marker the assignment emitter rewrites
        assert conv._resolve_parked_timer_expr('timer + 10') == '__BEAT__10'
        # unrelated expressions are untouched
        assert conv._resolve_parked_timer_expr('foo + 1') == 'foo + 1'

    def test_mp3_duration_reads_frame_headers(self, tmp_path):
        """A silent MPEG-1 Layer III CBR stream of known length."""
        import struct
        from script_convert.say_durations import mp3_duration
        # 128kbps, 44100Hz, no padding -> 417-byte frames of 1152 samples
        frame = bytes([0xFF, 0xFB, 0x90, 0x00]) + b'\x00' * 413
        n = 38  # 38 * 1152 / 44100 ~= 0.9927s
        p = tmp_path / 'x.mp3'
        p.write_bytes(frame * n)
        assert abs(mp3_duration(str(p)) - n * 1152 / 44100) < 0.01


class TestSayTimerRaceFree:
    """Say pacing handshake: park at the call site, release in the End
    fragment, beats carried in a decay-proof companion (2026-07-25).

    Three defects drove this design, in order:
      1. A flat/measured wait ADDED to the engine's own wait (the engine
         already runs the End fragment when the line finishes).
      2. A relative release raced the other scripts polling the same timer —
         four actors on independent 0.5s updates plus the quest script
         decrementing every 0.1s — so a different handoff stalled each run.
      3. A `>= park` guard never fired, because the owning loop counts the
         timer DOWN while the line plays: a 60s park is ~50 after a 10s line.
         Valen Dreth said his first line and then stalled forever.
    """

    PARK = 60.0
    THRESH = 20.0

    def _run(self, line_len, beat=0.0, dt=0.1):
        """Simulate call site -> countdown -> End fragment."""
        timer, pending = self.PARK, beat      # park, Say(), stage the beat
        elapsed = 0.0
        while elapsed < line_len:             # loop decays the timer
            if timer > 0:
                timer -= dt
            elapsed += dt
        if timer > self.THRESH:               # End fragment releases
            timer, pending = pending, 0.0
        return timer

    def test_releases_for_every_real_line_length(self):
        """The guard must survive decay: Oblivion lines run 0.65s to ~30s."""
        for line_len in (0.65, 3.0, 10.29, 14.6, 30.0):
            assert abs(self._run(line_len)) < 1e-9,                 f'{line_len}s line did not release the timer'

    def test_beat_survives_any_line_length(self):
        """A staged pause must arrive intact however long the line ran."""
        for line_len in (0.65, 10.29, 30.0):
            assert abs(self._run(line_len, beat=2.5) - 2.5) < 1e-9
            assert abs(self._run(line_len, beat=10.0) - 10.0) < 1e-9

    def test_release_is_idempotent(self):
        """A second fire must not re-arm the timer or go negative."""
        released = self._run(3.0)
        assert released <= self.THRESH   # guard no longer fires
        assert released == 0.0

    def test_beat_property_name_is_shared_by_both_emitters(self):
        """Call site and fragment must derive the identical companion name."""
        from script_convert.converter import ScriptConverter
        assert ScriptConverter.beat_property('timer') == 'timerPendingBeat'
        assert (ScriptConverter.beat_property('CharacterGen.convTimer')
                == 'CharacterGen.convTimerPendingBeat')


class TestSayTimerRaces:
    """The setstage-CharacterGen intermittency (2026-07-25).

    "Sometimes actors play their lines and sometimes they do not" had three
    independent causes, all in the park/release handshake around Say().
    """

    def _owners(self, sctx, extra=None):
        from script_convert.pipeline import build_say_timer_owners
        by_type = {'SCPT': [{'EditorID': 'CharGenQuest', 'FormID': '0002480B',
                             'SCTX': sctx}],
                   'QUST': [{'EditorID': 'Charactergen', 'FormID': '0002466E',
                             'SCRI': '0002480B'}]}
        if extra:
            for sig, recs in extra.items():
                by_type.setdefault(sig, []).extend(recs)
        return build_say_timer_owners(by_type)

    def test_say_with_a_trailing_flag_keeps_its_topic(self):
        """`Say <topic> 1`'s topic must not be eaten by the target group.

        SayTo takes a TARGET before the topic and Say does not, so an optional
        `(?:\\w+[\\s,]+)?` target group greedily consumed the topic of every
        `Say <topic> 1` and captured the trailing flag as the topic ("1").
        120+ topics were left with no release owner — every Daedric shrine
        speech, the Boethia champions, the SE quest chatter — so their parked
        timers were never cleared and each scene died after ONE line.
        """
        ow = self._owners('set CharacterGen.convTimer to Say CharGenMain 1')
        assert '1' not in ow, 'the trailing Say flag was captured as the topic'
        assert 'chargenmain' in ow

    def test_sayto_still_skips_its_target_argument(self):
        ow = self._owners(
            'set CharacterGen.convTimer to SayTo BaurusRef, CharGenMain 1')
        assert 'chargenmain' in ow
        assert 'baurusref' not in ow

    def test_a_match_never_runs_past_the_end_of_its_line(self):
        """SCTX is one escaped blob; `\\s` let a match swallow the next lines."""
        ow = self._owners('set CharacterGen.convTimer to Say CharGenMain 1\n'
                          'else\n'
                          'setstage CharacterGen 20\n'
                          'endif')
        for junk in ('else', 'endif', 'setstage', 'charactergen'):
            assert junk not in ow, f'{junk!r} was captured as a topic'

    def test_a_non_quest_prefix_is_not_bound_as_a_quest(self):
        """`set <REFR>.timer to Say <topic>` has no quest to bind.

        MS27CarvingWall is a placed REFR, so emitting `Quest Property
        MS27CarvingWall` made the fragment fail to compile ("field or property
        `timer` not found"). A dotted target is only quest-scoped when the
        prefix really is a QUST EditorID.
        """
        ow = self._owners('set MS27CarvingWall.timer to Say MS27Voice')
        assert 'ms27voice' not in ow

    def test_countdown_of_a_parked_timer_cannot_resurrect_the_park(self):
        """The decrement is a read-modify-write on an async-cleared variable.

        The owning script counts the timer down on its own update while the
        line's End fragment clears it to 0 from the engine's dialogue thread.
        Papyrus has no atomicity, so a release landing between the read and the
        write stored `park - dt` and RE-PARKED a timer that was already
        released. Every INFO here is gated on an exact convCount, so the chain
        did not resume late — it stopped dead.
        """
        from script_convert.converter import ScriptConverter
        from script_convert.cross_ref import CrossRefGraph
        src = ('scn CharGenQuest\n'
               'float convTimer\n'
               'short speaker\n'
               'begin gamemode\n'
               '\tif convTimer > 0\n'
               '\t\tset convTimer to convTimer - getSecondsPassed\n'
               '\tendif\n'
               '\tif speaker == 1 && convTimer <= 0\n'
               '\t\tset convTimer to BaurusRef.SayTo player CharGenVoice 1\n'
               '\tendif\n'
               'end\n')
        out = ScriptConverter(CrossRefGraph()).convert_standalone(
            'CharGenQuest', src, 'Quest', editor_id='CharGenQuest')
        # the naive read-modify-write must be gone
        assert 'convTimer = convTimer - 0.1' not in out
        # ...replaced by a snapshot whose write-back is abandoned if the value
        # moved while the statement ran
        assert 'convTimer == _tes4TickconvTimer' in out
        assert 'convTimer = _tes4TickconvTimer - 0.1' in out

    def test_countdown_snapshot_matches_an_int_timer_type(self):
        """TES4 let a `short` hold a Say duration; Float locals break that."""
        from script_convert.converter import ScriptConverter
        from script_convert.cross_ref import CrossRefGraph
        src = ('scn X\n'
               'short sayLen\n'
               'begin gamemode\n'
               '\tif sayLen > 0\n'
               '\t\tset sayLen to sayLen - getSecondsPassed\n'
               '\telse\n'
               '\t\tset sayLen to say SomeTopic\n'
               '\tendif\n'
               'end\n')
        out = ScriptConverter(CrossRefGraph()).convert_standalone(
            'X', src, 'ObjectReference', editor_id='X')
        assert 'Float _tes4TicksayLen' not in out
        assert 'Int _tes4TicksayLen' in out

    def test_park_never_blocks_the_polling_loop(self):
        """No Utility.Wait at a park site.

        An earlier watchdog polled for the line to start, which blocked the very
        OnUpdate that owns the countdown (and deferred the actor scripts' next
        RegisterForSingleUpdate), stalling the timer it was meant to protect.
        """
        from script_convert.converter import ScriptConverter
        from script_convert.cross_ref import CrossRefGraph
        src = ('scn CGGlenroyScript\n'
               'short target\n'
               'begin gamemode\n'
               'if CharacterGen.speaker == 3 && CharacterGen.convTimer <= 0\n'
               '\tset CharacterGen.convTimer to Say CharGenMain 1\n'
               'endif\n'
               'end\n')
        out = ScriptConverter(CrossRefGraph()).convert_standalone(
            'CGGlenroyScript', src, 'Actor', editor_id='CGGlenroyScript')
        assert 'Utility.Wait' not in out


class TestActorScriptOnPlacedRef:
    """A converted actor script must live on the placed reference, not the base.

    Reference events (OnPackageEnd/OnActivate/OnDeath/OnHit) are declared on
    Actor/ObjectReference in vanilla Papyrus (Scripts.zip).  A base NPC_ record
    is an ActorBase (a Form), so a VMAD bound there receives NONE of them.

    This silently broke every converted quest that sequences on package
    completion: CharacterGen advances 10 -> 12 from Renote's OnPackageEnd, and
    with the script stranded on the base NPC_ the chain stopped dead at stage
    10, leaving the Emperor and guards with no `GetStage ==` package to select.
    """

    def test_reference_event_declaration_is_detected(self):
        from tes5_import.object_scripts import _script_uses_reference_event
        assert _script_uses_reference_event('begin OnPackageDone CGRenoteToMarkerA')
        assert _script_uses_reference_event('scn X\r\nbegin gamemode\r\nend\r\nbegin onhit\r\nend')
        assert _script_uses_reference_event('begin OnActivate\nActivate')

    def test_non_reference_scripts_are_left_on_the_base(self):
        """Only a `begin <event>` DECLARATION counts.

        A bare substring match relocated scripts that merely mentioned an event
        name in a comment, moving records that had no reason to leave the base.
        """
        from tes5_import.object_scripts import _script_uses_reference_event
        assert not _script_uses_reference_event('; comment mentioning onhit behaviour')
        assert not _script_uses_reference_event('begin MenuMode 1027')
        # not an actor event, and must not match on the 'onhit' substring
        assert not _script_uses_reference_event('begin OnMagicEffectHit')

    def test_bare_gamemode_does_not_force_relocation(self):
        """A bare GameMode block alone does NOT relocate. Known open gap.

        `gamemode` was briefly a member of _TES4_REFERENCE_EVENTS: TES4
        delivers GameMode everywhere, but the converter compiles it into an
        OnUpdate poll whose starters are all ObjectReference members, so on a
        base NPC_ the poll never starts and the block is dead code.
        Morroblivion's CATDestinationSorter (the Jo'Tesh/Kisimba world
        transport) is pure GameMode with no bare self-reference call, so it
        triggered neither other relocation reason and never ran.

        It was REVERTED alongside the ShouldRunGameMode gate (commit 4a802e2)
        when that pair regressed CharacterGen.  A GameMode block still
        relocates whenever it ALSO makes a bare self-reference call — which is
        the common case (Celebro's `enable`) — so only pure-GameMode scripts
        are affected.

        This test pins the current behaviour so the revert cannot be undone
        silently; re-test CharacterGen in-game before restoring `gamemode`.
        See test_self_enable_deadlock_is_a_known_open_regression in
        tests/test_script_converter.py for the other half of the same revert.
        """
        from tes5_import.object_scripts import (
            _script_uses_reference_event, _script_uses_self_reference_call)
        assert not _script_uses_reference_event('begin GameMode\nset x to 1\nend')
        assert not _script_uses_reference_event('scn X\r\nbegin gamemode\r\nend')
        # ...but a GameMode block that calls a self-reference function still
        # relocates, via reason 3.
        assert _script_uses_self_reference_call(
            'scn X\nbegin GameMode\nenable\nend')

    def test_bare_self_reference_call_forces_relocation(self):
        """A bare `enable`/`moveto`/... acts on the calling REFERENCE.

        An ActorBase is not a reference, so on the base record the call does
        nothing at all.  Oblivion's scripted-entrance idiom is an
        initially-disabled placement whose own GameMode block enables it, which
        makes this the difference between the actor appearing and never
        existing — Celebro, the Nehrim intro companion, was absent from the
        start cell because MQ00CelebroScript (`if GetStage MQ00 == 5 / enable`)
        declares no reference event and so was left on the base NPC_.
        """
        from tes5_import.object_scripts import _script_uses_self_reference_call
        assert _script_uses_self_reference_call(
            'scn X\\r\\nbegin GameMode\\r\\nif ( GetStage MQ00 == 5 )'
            '\\r\\n\\tenable\\r\\nendif\\r\\nend')
        assert _script_uses_self_reference_call(
            'begin GameMode\\r\\n\\tMoveTo MQ00TrollMarker\\r\\nend')

    def test_other_refs_calls_do_not_force_relocation(self):
        """Only BARE calls count.  `CelebroRef.Disable` targets someone else and
        works fine from the base; a commented-out `;evp` is not a call at all.
        """
        from tes5_import.object_scripts import _script_uses_self_reference_call
        assert not _script_uses_self_reference_call(
            'begin OnActivate\\r\\n\\tCelebroRef.Disable\\r\\n\\tActivate\\r\\nend')
        assert not _script_uses_self_reference_call(
            'scn X\\r\\nbegin GameMode\\r\\n;evp\\r\\nend')
        assert not _script_uses_self_reference_call(
            'begin GameMode\\r\\n\\tset x to 1\\r\\nend')


class TestLoadGatedPollStart:
    """A load-gated update loop must start from OnLoad, not OnInit alone.

    OnInit on a PLACED REFERENCE runs at load BEFORE the actor's 3D exists, so
    an `If Is3DLoaded()` guard there is false and the poll never starts.
    OnCellAttach cannot cover it either — it fires only when a cell BECOMES
    attached, never for an actor already standing in the player's current cell.

    This silenced Valen Dreth the moment actor scripts moved to the ACHR (which
    reference events like OnPackageEnd require). Vanilla starts update loops
    from OnLoad in 27 scripts vs 2 that gate OnInit on Is3DLoaded; per
    ObjectReference.psc OnLoad is "fired every time this object is loaded".
    """

    def _emit(self, src):
        from script_convert.converter import ScriptConverter
        from script_convert.cross_ref import CrossRefGraph
        conv = ScriptConverter(CrossRefGraph())
        return conv.convert_standalone('T', src, 'Actor', 'T')

    def test_gamemode_actor_script_registers_from_onload(self):
        out = self._emit('scn T\nbegin gamemode\nset x to 1\nend\n')
        assert 'Event OnLoad()' in out
        onload = out.split('Event OnLoad()', 1)[1].split('EndEvent', 1)[0]
        assert 'RegisterForSingleUpdate' in onload

    def test_oncellattach_and_onload_both_present(self):
        """OnCellAttach still handles streaming in; OnLoad covers the rest."""
        out = self._emit('scn T\nbegin gamemode\nset x to 1\nend\n')
        assert 'Event OnCellAttach()' in out
        assert 'Event OnCellDetach()' in out


# ---------------------------------------------------------------------------
# Weather / climate conversion
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason='WTHR imagespace conversion lives on the '
                         'weather-conversion branch; master convert_WTHR '
                         'returns bytes, not (wthr, imgs)')
class TestWeatherConversion:
    """WTHR conversion.

    Field SEMANTICS come from UESP 'Skyrim Mod:Mod File Format/WTHR' and
    xEdit wbDefinitionsCommon; the defaults come from a census of the 84
    vanilla WTHR records in Skyrim.esm.  Several of these fields tint additive
    sky passes, so a guessed value blows the scene out rather than merely
    looking a bit off -- hence a test per field.
    """

    def _nam0(self):
        """TES4 NAM0: 10 types x 4 times x RGBA, one recognisable colour each."""
        raw = bytearray(160)
        for t in range(10):
            for time in range(4):
                o = (t * 4 + time) * 4
                raw[o:o + 4] = bytes((10 * t, 10 * t + 1, 10 * t + 2, 0))
        return bytes(raw).hex().upper()

    def _convert(self, rec):
        """convert_WTHR returns (wthr_bytes, imgs_bytes); most tests want the WTHR."""
        from tes5_import.record_types.dialog_misc import convert_WTHR
        wthr, _imgs = convert_WTHR(rec)
        return wthr

    def _rec(self, **over):
        rec = {
            'Signature': 'WTHR', 'FormID': '00000200', 'RecordFlags': '0',
            'EditorID': 'TestWeather',
            'CNAM.LowerCloudLayer': 'Sky\\Lower.dds',
            'DNAM.UpperCloudLayer': 'Sky\\Upper.dds',
            'NAM0.Size': '160', 'NAM0.Data': self._nam0(),
            'DATA.WindSpeed': '25', 'DATA.CloudSpeedLower': '42',
            'DATA.CloudSpeedUpper': '19', 'DATA.TransDelta': '3',
            'DATA.SunGlare': '255', 'DATA.SunDamage': '200',
            'DATA.PrecipBeginFadeIn': '5', 'DATA.PrecipEndFadeOut': '6',
            'DATA.ThunderBeginFadeIn': '7', 'DATA.ThunderEndFadeOut': '8',
            'DATA.ThunderFrequency': '188', 'DATA.Classification': '4',
            'DATA.LightningR': '11', 'DATA.LightningG': '12',
            'DATA.LightningB': '13',
        }
        rec.update(over)
        return rec

    def test_subrecord_sizes_match_vanilla(self):
        rec = self._convert(self._rec())
        for sig, size in ((b'NAM0', 272), (b'DATA', 19), (b'FNAM', 32),
                          (b'RNAM', 32), (b'QNAM', 32), (b'PNAM', 512),
                          (b'JNAM', 512), (b'IMSP', 16)):
            assert len(_find_subrecord(rec, sig)) == size, sig
        dalc = _find_all_subrecords(rec, b'DALC')
        assert len(dalc) == 4 and all(len(d) == 32 for d in dalc)

    def test_nam0_colours_are_remapped_not_stubbed(self):
        """The first version wrote flat 128-grey over the whole table."""
        nam0 = _find_subrecord(self._convert(self._rec()), b'NAM0')

        def rgb(slot, time=1):
            o = (slot * 4 + time) * 4
            return tuple(nam0[o:o + 3])

        assert rgb(0) == (0, 1, 2)        # Sky-Upper
        assert rgb(1) == (10, 11, 12)     # Fog -> Fog Near
        assert rgb(3) == (30, 31, 32)     # Ambient
        assert rgb(6) == (60, 61, 62)     # Stars
        assert rgb(8) == (80, 81, 82)     # Horizon
        # TES4's two cloud tints move to TES5's Cloud LOD slots
        assert rgb(10) == (90, 91, 92)    # Clouds-Upper -> Cloud LOD Diffuse
        assert rgb(11) == (20, 21, 22)    # Clouds-Lower -> Cloud LOD Ambient
        assert rgb(12) == (10, 11, 12)    # Fog Far reuses the single TES4 fog

    def test_glare_and_sky_static_slots_default_dark(self):
        """Slots 13/15/16 tint ADDITIVE passes.

        Copying the TES4 Sun/Stars colours into Sun Glare / Moon Glare, and
        forcing Sky Statics white, produced a blinding sky.  Vanilla ships
        black in all three (Sky Statics 7/84 exact-black and never white,
        Sun Glare 35/84 black, Moon Glare 27/84 black).
        """
        nam0 = _find_subrecord(self._convert(self._rec()), b'NAM0')

        def rgb(slot, time=1):
            o = (slot * 4 + time) * 4
            return tuple(nam0[o:o + 3])

        assert rgb(13) == (0, 0, 0), 'Sky Statics must not be white'
        assert rgb(15) == (0, 0, 0), 'Sun Glare must not copy the Sun colour'
        assert rgb(16) == (0, 0, 0), 'Moon Glare must not copy the Stars colour'
        # Water Multiplier is the one slot that genuinely defaults to white
        assert rgb(14) == (255, 255, 255)

    def test_data_carries_every_tes4_field(self):
        """Offsets 6-14 were dropped entirely by the original converter."""
        d = _find_subrecord(self._convert(self._rec()), b'DATA')
        assert d[0] == 25                        # wind speed
        assert (d[1], d[2]) == (0, 0)            # TES5 padding (was cloud speed)
        assert d[3] == 3 and d[4] == 255         # trans delta, sun glare
        assert d[5] == 200                       # sun damage
        assert (d[6], d[7]) == (5, 6)            # precipitation fades
        assert (d[8], d[9]) == (7, 8)            # thunder fades
        assert d[10] == 188                      # thunder frequency
        assert d[11] == 4                        # classification (Rainy)
        assert (d[12], d[13], d[14]) == (11, 12, 13)   # lightning colour

    def test_thunder_frequency_keeps_inverted_scale(self):
        """Both games use 255=never .. 15=constant, so it is a passthrough.

        Vanilla Oblivion agrees: every clear weather is 255 and the
        thunderstorms are 188/132/100/24.  Inverting it here would make clear
        skies thunder constantly.
        """
        from tes5_import.record_types.dialog_misc import convert_WTHR
        clear = self._convert(self._rec(**{'DATA.ThunderFrequency': '255'}))
        assert _find_subrecord(clear, b'DATA')[10] == 255

    def test_classification_defaults_to_pleasant(self):
        out = self._convert(self._rec(**{'DATA.Classification': '0'}))
        assert _find_subrecord(out, b'DATA')[11] == 0x01

    def test_only_unused_cloud_layers_are_disabled(self):
        """NAM1=0xFFFFFFFF disabled layers 0/1 too, blanking every sky."""
        nam1 = struct.unpack('<I', _find_subrecord(self._convert(self._rec()), b'NAM1'))[0]
        assert nam1 & 0b11 == 0
        assert nam1 == 0xFFFFFFFC

    def test_weather_with_no_cloud_textures_disables_all_layers(self):
        rec = self._rec(**{'CNAM.LowerCloudLayer': '', 'DNAM.UpperCloudLayer': ''})
        out = self._convert(rec)
        assert struct.unpack('<I', _find_subrecord(out, b'NAM1'))[0] == 0xFFFFFFFF

    def test_cloud_speed_uses_the_shared_physical_scale(self):
        """Both engines cap cloud drift at 0.1 units.

        Oblivion scales its unsigned 0..255 byte by fWeatherCloudSpeedMax
        (0.1, read from Oblivion.exe at 0xA2FAAC); Skyrim encodes a SIGNED
        -0.1..+0.1 as 0x00..0xFE with 0x7F = 0.  So the byte must be rescaled,
        not copied.  `0x7F + speed//2` ran clouds ~10x too fast.
        """
        from tes5_import.record_types.dialog_misc import _cloud_speed_tes4_to_tes5 as conv

        def to_float(b):
            return (b - 127) / 127 / 10

        assert conv(0) == 0x7F                      # still stays still
        assert conv(255) == 0xFE                    # TES4 max == TES5 max
        for tes4 in (19, 25, 42, 50, 101, 200):
            assert abs(to_float(conv(tes4)) - tes4 / 255 * 0.1) < 0.001
        assert all(0x7F <= conv(v) <= 0xFE for v in range(256))

    def test_cloud_speed_lands_in_rnam_layers(self):
        from tes5_import.record_types.dialog_misc import convert_WTHR
        rnam = _find_subrecord(self._convert(self._rec()), b'RNAM')
        assert rnam[0] == 0x94 and rnam[1] == 0x88   # TES4 42 / 19
        assert set(rnam[2:]) == {0x7F}               # untouched layers neutral

    def test_only_textured_layers_are_opaque(self):
        """A blanket alpha 1.0 draws 30 opaque empty layers over the sky."""
        jnam = struct.unpack('<128f', _find_subrecord(self._convert(self._rec()), b'JNAM'))
        assert jnam[0:4] == (1.0,) * 4      # layer 0 has a texture
        assert jnam[4:8] == (1.0,) * 4      # layer 1 has a texture
        assert set(jnam[8:]) == {0.0}       # layers 2..31 do not

    def test_required_subrecords_present(self):
        """LNAM/MNAM/NNAM are .SetRequired in xEdit; LNAM=0 allocated no layers."""
        rec = self._convert(self._rec())
        assert struct.unpack('<I', _find_subrecord(rec, b'LNAM'))[0] == 29
        assert _find_subrecord(rec, b'MNAM') == b'\x00\x00\x00\x00'
        assert _find_subrecord(rec, b'NNAM') == b'\x00\x00\x00\x00'

    def test_dalc_follows_vanilla_face_weights(self):
        """Z+ is the DARKEST face and Z- the brightest.

        Medians over all 84 vanilla weathers: X+ .98 X- .94 Y+ .96 Y- .95
        Z+ .67 Z- 1.28.  Writing Ambient verbatim into all six faces and then
        BRIGHTENING Z+ (the first version) overdrove the whole cube.
        """
        dalc = _find_all_subrecords(self._convert(self._rec()), b'DALC')[1]
        faces = [tuple(dalc[i * 4:i * 4 + 3]) for i in range(6)]
        amb = (30, 31, 32)                       # NAM0 Ambient, day
        assert faces[4][0] < amb[0], 'Z+ must be darker than Ambient'
        assert faces[5][0] > amb[0], 'Z- must be brighter than Ambient'
        assert faces[4][0] < faces[5][0]
        for f in faces[:4]:                      # horizontals sit near Ambient
            assert abs(f[0] - amb[0]) <= 3
        assert struct.unpack_from('<f', dalc, 28)[0] == 1.0    # Fresnel

    def test_default_weather_editorid_avoids_skyrim_collision(self):
        out = self._convert(self._rec(EditorID='DefaultWeather'))
        assert _find_subrecord(out, b'EDID') == b'TES4DefaultWeather\x00'

    def test_missing_nam0_does_not_crash(self):
        rec = self._rec()
        del rec['NAM0.Data']
        assert len(_find_subrecord(self._convert(rec), b'NAM0')) == 272


class TestClimateConversion:
    def _rec(self, **over):
        rec = {
            'Signature': 'CLMT', 'FormID': '0000015F', 'RecordFlags': '0',
            'EditorID': 'TestClimate',
            'WeatherCount': '2',
            'Weather[0].FormID': '00000200', 'Weather[0].Chance': '70',
            'Weather[1].FormID': '00000201', 'Weather[1].Chance': '30',
            'FNAM.SunTexture': 'Sky\\Sun.dds',
            'GNAM.GlareTexture': 'Sky\\SunGlare.dds',
            'Model.MODL': 'Sky\\Stars.nif',
            'TNAM.SunriseBegin': '36', 'TNAM.SunriseEnd': '60',
            'TNAM.SunsetBegin': '96', 'TNAM.SunsetEnd': '120',
            'TNAM.Volatility': '0', 'TNAM.MoonsPhaseLength': '195',
        }
        rec.update(over)
        return rec

    def test_wlst_entries_are_widened_to_12_bytes(self):
        """TES4's entry is 8 bytes; TES5 appends a Global FormID."""
        from tes5_import.record_types.dialog_misc import convert_CLMT
        wlst = _find_subrecord(convert_CLMT(self._rec()), b'WLST')
        assert len(wlst) == 24
        w0, c0, g0 = struct.unpack_from('<IiI', wlst, 0)
        w1, c1, g1 = struct.unpack_from('<IiI', wlst, 12)
        assert (c0, c1) == (70, 30)
        assert (g0, g1) == (0, 0)
        assert w0 != 0 and w1 != 0

    def test_stars_model_and_sun_textures_are_namespaced(self):
        from tes5_import.record_types.dialog_misc import convert_CLMT
        out = convert_CLMT(self._rec())
        assert _find_subrecord(out, b'MODL') == b'tes4\\Sky\\Stars.nif\x00'
        assert _find_subrecord(out, b'FNAM') == b'tes4\\Sky\\Sun.dds\x00'
        assert _find_subrecord(out, b'GNAM') == b'tes4\\Sky\\SunGlare.dds\x00'
        assert _find_subrecord(out, b'MODT') is not None

    def test_climate_without_model_still_gets_stars(self):
        """Every vanilla Skyrim climate has a MODL; without one, no stars."""
        from tes5_import.record_types.dialog_misc import convert_CLMT
        rec = self._rec()
        del rec['Model.MODL']
        assert _find_subrecord(convert_CLMT(rec), b'MODL') == b'tes4\\Sky\\Stars.nif\x00'

    def test_tnam_timing_is_six_bytes_verbatim(self):
        from tes5_import.record_types.dialog_misc import convert_CLMT
        tnam = _find_subrecord(convert_CLMT(self._rec()), b'TNAM')
        assert tnam == bytes((36, 60, 96, 120, 0, 195))

    def test_climate_dispatch_follows_the_feature_flag(self):
        """Climate conversion is gated on CONVERT_CLIMATE; the converter above
        stays tested either way so the flag can be flipped back on."""
        from tes5_import.constants import (
            CONVERT_CLIMATE, IMPORT_DISPATCH, SKIP_TYPES)
        if CONVERT_CLIMATE:
            assert 'CLMT' not in SKIP_TYPES
            assert 'CLMT' in IMPORT_DISPATCH
        else:
            assert 'CLMT' in SKIP_TYPES
            assert 'CLMT' not in IMPORT_DISPATCH


class TestWorldspaceClimate:
    def _rec(self, **over):
        rec = {'Signature': 'WRLD', 'FormID': '0000003C', 'RecordFlags': '0',
               'EditorID': 'Tamriel'}
        rec.update(over)
        return rec

    def test_authored_climate_is_kept(self):
        from tes5_import.constants import CONVERT_CLIMATE
        from tes5_import.record_types.world import convert_WRLD
        if not CONVERT_CLIMATE:
            pytest.skip('climate conversion disabled (CONVERT_CLIMATE)')
        out = convert_WRLD(self._rec(**{'CNAM.Climate': '00097C60'}))
        cnam = struct.unpack('<I', _find_subrecord(out, b'CNAM'))[0]
        assert cnam & 0x00FFFFFF == 0x97C60

    def test_worldspace_without_climate_falls_back_to_default(self):
        """Oblivion.exe resolves a null worldspace climate to DefaultClimate
        (0x15F) at runtime -- the sky setup at 0x667688 falls through to
        0x543200, which does LookupForm(0x15F).  Skyrim has no such fallback,
        and 57 of 84 TES4 worldspaces (incl. Tamriel and every city) author no
        CNAM, so it must be written explicitly."""
        from tes5_import.constants import CONVERT_CLIMATE
        from tes5_import.record_types.world import convert_WRLD
        if not CONVERT_CLIMATE:
            pytest.skip('climate conversion disabled (CONVERT_CLIMATE)')
        cnam = _find_subrecord(convert_WRLD(self._rec()), b'CNAM')
        assert cnam is not None, 'a CNAM-less worldspace would use Skyrim weather'
        assert struct.unpack('<I', cnam)[0] & 0x00FFFFFF == 0x15F

    def test_no_dangling_climate_reference_when_disabled(self):
        """With CLMT in SKIP_TYPES, a CNAM would point at a record that was
        never written -- omit it rather than ship a dangling FormID."""
        from tes5_import.constants import CONVERT_CLIMATE
        from tes5_import.record_types.world import convert_WRLD
        if CONVERT_CLIMATE:
            pytest.skip('climate conversion enabled (CONVERT_CLIMATE)')
        out = convert_WRLD(self._rec(**{'CNAM.Climate': '00097C60'}))
        assert _find_subrecord(out, b'CNAM') is None


class TestSkyMeshShaders:
    """Sky geometry needs BSSkyShaderProperty, not the lighting shader.

    Skyrim draws sky through a dedicated pass keyed on Sky Object Type; a sky
    mesh carrying BSLightingShaderProperty is treated as ordinary world
    geometry, which made converted stars draw on top of the landscape.
    """

    def test_sky_meshes_are_classified_by_type(self):
        from asset_convert.nif_converter import (
            sky_object_type_for, SKY_STARS, SKY_CLOUDS, SKY_BASE)
        assert sky_object_type_for('export/x/meshes/sky/stars.nif') == SKY_STARS
        assert sky_object_type_for('export/x/meshes/sky/clouds.nif') == SKY_CLOUDS
        assert sky_object_type_for('export/x/meshes/sky/atmosphere.nif') == SKY_BASE
        assert sky_object_type_for(r'export\x\meshes\Sky\Stars.NIF') == SKY_STARS

    def test_non_sky_meshes_are_not_misclassified(self):
        from asset_convert.nif_converter import sky_object_type_for
        assert sky_object_type_for('meshes/clutter/barrel01.nif') is None
        # must key on the sky/ DIRECTORY, not just the basename
        assert sky_object_type_for('meshes/architecture/sky/wall.nif') is None
        assert sky_object_type_for('meshes/dungeons/clouds.nif') is None
        assert sky_object_type_for('') is None


class TestSayTimerRelease:
    """Every line under a PARKED topic must be able to release the timer.

    A converted Say() parks its conversation timer (the engine already blocks
    for the audio, so charging a duration would make the two waits ADD). The
    topic's End fragment clears it when the line really finishes. Two bugs made
    that release unreachable and stalled CharacterGen for good:

      1. A QUEST script writing its own timer with no prefix (`set convTimer to
         BaurusRef.SayTo ...` in CharGenQuest) was classified as SPEAKER-scoped
         purely because the name had no dot, emitting
         `(akSpeakerRef as TES4_CharGenQuest)` — casting an actor to a Quest
         script always yields None, so the write silently did nothing.
      2. The fragment was only generated/attached for an INFO that HAD a result
         script, but 87% of the INFOs under a parked topic (5,450 of 6,248)
         have none. Those lines parked the timer forever.

    The emitted order (script_convert/pipeline.py, `_write_info_fragments`)
    satisfies three constraints at once, each traced to an in-game failure:
    counter step, then release, then the rest of the body — all inside the
    sequence gate — plus an unconditional release after the gate for the
    rejected path. The release is idempotent, so the accepted path running it
    twice is harmless.
    """

    def _owners(self):
        from script_convert.pipeline import build_say_timer_owners
        scpt = {'EditorID': 'CharGenQuest', 'FormID': '0002480B',
                'SCTX': 'begin gamemode\n'
                        'set convTimer to BaurusRef.SayTo player CharGenVoice 1\n'
                        'end'}
        qust = {'EditorID': 'Charactergen', 'FormID': '0002466E',
                'SCRI': '0002480B'}
        return build_say_timer_owners({'SCPT': [scpt], 'QUST': [qust]})

    def test_quest_script_bare_timer_is_quest_scoped(self):
        """The owner must resolve to the QUEST, not the speaker."""
        owner = self._owners().get('chargenvoice')
        assert owner is not None
        assert owner[0] == 'quest', (
            'a quest script\'s own timer must bind a quest property; casting '
            'akSpeakerRef to a Quest script yields None and drops the write')
        assert owner[1].split('.', 1)[0].lower() == 'charactergen'

    def test_speaker_local_timer_still_casts_the_speaker(self):
        """An actor script's own timer must NOT be redirected to a quest."""
        from script_convert.pipeline import build_say_timer_owners
        scpt = {'EditorID': 'ValenDrethScript', 'FormID': '0001FC44',
                'SCTX': 'begin gamemode\n'
                        'set timer to SayTo player, CharGenTaunt2 1\n'
                        'end'}
        owner = build_say_timer_owners({'SCPT': [scpt], 'QUST': []})
        assert owner['chargentaunt2'][0] == 'speaker'

    def _fragments(self):
        import os
        src = os.path.join('output', 'Oblivion.esm', 'scripts', 'source')
        if not os.path.isdir(src):
            pytest.skip('no generated scripts to check')
        for fn in sorted(os.listdir(src)):
            if not fn.startswith('TES4_TIF__'):
                continue
            lines = open(os.path.join(src, fn),
                         encoding='utf-8', errors='replace').read().splitlines()
            if any('; line ended' in ln for ln in lines):
                yield fn, lines

    @staticmethod
    def _gate_depth(lines):
        """Yield (line, depth) with depth>0 inside a sequence gate."""
        import re
        depth = 0
        for ln in lines:
            if "still this line's turn" in ln:
                yield ln, depth
                depth += 1
            elif re.match(r'\s*EndIf\b', ln, re.IGNORECASE) and depth:
                depth -= 1
                yield ln, depth
            else:
                yield ln, depth

    def test_release_is_reachable_on_the_rejected_path(self):
        """A gated fragment must ALSO release OUTSIDE the gate.

        A line whose turn has passed still has to free the timer. With the
        release only inside the `still this line's turn` gate, CharacterGen
        stopped dead at `FRAG 00032B0A cnt=8 needs 7 accepted=False`: quest
        stage 12 re-seeded convCount while line 7 was still playing, the
        fragment was rejected, and nothing ever cleared convTimer.

        The gated copy is NOT a defect — constraint 3 requires a release before
        the body's SetStage, and the write is idempotent. What matters is that
        an unconditional one exists too.
        """
        offenders = []
        checked = 0
        for fn, lines in self._fragments():
            if not any("still this line's turn" in ln for ln in lines):
                continue
            checked += 1
            if not any('; line ended' in ln and d == 0
                       for ln, d in self._gate_depth(lines)):
                offenders.append(fn)
        assert checked, 'expected some gated fragments to release a say timer'
        assert not offenders, (
            'these gated fragments release the timer ONLY inside the sequence '
            'gate, so a rejected line never frees it and the conversation '
            'stalls:\n' + '\n'.join(offenders[:10]))

    def test_counter_step_precedes_the_release(self):
        """Inside the gate, the counter closes before the timer frees.

        The owning script's poll guard is `speaker == N && convTimer <= 0` and
        the gate is `convCount == K`. Releasing the timer while the COUNTER
        still reads K lets the owner re-Say the same line (`RENAULT FIRE
        cnt=15` twice), which re-arms the timer. Stepping the counter first
        closes the gate against that re-fire.
        """
        import re
        # The step the gate tests: `<c> = <c> + n`, not any convCount write.
        step_pat = re.compile(
            r'^\s*(\S*convCount)\s*=\s*\1\s*[-+]', re.IGNORECASE)
        offenders = []
        checked = 0
        for fn, lines in self._fragments():
            gated = [ln for ln, d in self._gate_depth(lines) if d > 0]
            rel = next((i for i, ln in enumerate(gated)
                        if '; line ended' in ln), None)
            step = next((i for i, ln in enumerate(gated)
                         if step_pat.match(ln)), None)
            if rel is None or step is None:
                continue
            checked += 1
            if step > rel:
                offenders.append(f'{fn}: {gated[step].strip()}')
        assert checked, 'expected some gated fragments to step a counter'
        assert not offenders, (
            'these fragments step the sequence counter AFTER releasing the '
            'timer, so the owning script re-fires the same line:\n'
            + '\n'.join(offenders[:10]))

    def test_release_precedes_setstage_in_the_body(self):
        """The timer must already be free when the body's SetStage runs.

        SetStage executes that stage's fragment INLINE and those call
        `EvaluatePackage()`, which arbitrates against whatever is committed at
        that instant. With convTimer still 7.63 the engine picked
        CGRenoteOpenSecretDoor and kicked it back to CGRenoteWalkToMarkerB in
        the same second — a race on engine latency that worked one run and
        failed the next.

        A SetStage on some OTHER quest shares no state with the timer's owner,
        so only same-owner calls are constrained (see `_setstage_on_owner`).
        """
        import re
        offenders = []
        checked = 0
        for fn, lines in self._fragments():
            rel = next((i for i, ln in enumerate(lines)
                        if '; line ended' in ln), None)
            if rel is None:
                continue
            # `<owner>.convTimer = ...` or `(x as Script).convtimer = ...`
            m = re.search(r'([\w.)]+)\.convtimer\s*=', lines[rel],
                          re.IGNORECASE)
            if not m:
                continue
            owner = m.group(1).lower()
            same = re.compile(r'^\s*' + re.escape(owner) + r'\.SetStage\s*\(',
                              re.IGNORECASE)
            first = next((i for i, ln in enumerate(lines)
                          if same.match(ln)), None)
            if first is None:
                continue
            checked += 1
            if rel > first:
                offenders.append(f'{fn}: {lines[first].strip()}')
        assert checked, (
            'expected some fragments to SetStage on the timer\'s own quest')
        assert not offenders, (
            'these fragments call SetStage before releasing the timer, so the '
            'stage fragment\'s EvaluatePackage() arbitrates against an armed '
            'convTimer:\n' + '\n'.join(offenders[:10]))

    def test_state_writes_precede_setstage_within_the_body(self):
        """Inside the body, speaker/target/counter land before any SetStage.

        SetStage runs that stage's fragment INLINE and those call
        `EvaluatePackage()`, which arbitrates against whatever is committed at
        that instant. This is ordering WITHIN the gate only — the release lands
        earlier still, and is repeated unconditionally after the gate (see the
        three tests above).
        """
        import re
        setstage = re.compile(r'^\s*(\w[\w.]*)\.SetStage\s*\(', re.IGNORECASE)
        offenders = []
        checked = 0
        for fn, lines in self._fragments():
            first = next((i for i, ln in enumerate(lines)
                          if setstage.match(ln)), None)
            if first is None:
                continue
            checked += 1
            quest = setstage.match(lines[first]).group(1).lower()
            book = re.compile(
                r'^\s*' + re.escape(quest) + r'\.(convCount|speaker|target)\s*=',
                re.IGNORECASE)
            after = [ln for ln in lines[first + 1:] if book.match(ln)]
            if after:
                offenders.append(f'{fn}: {after[0].strip()}')
        assert checked, 'expected some fragments to call SetStage'
        assert not offenders, (
            'these fragments write conversation state AFTER a SetStage on the '
            'same quest, so the stage fragment\'s EvaluatePackage() arbitrates '
            'against stale state:\n' + '\n'.join(offenders[:10]))


@pytest.mark.skip(reason='WTHR imagespace conversion lives on the '
                         'weather-conversion branch; master convert_WTHR '
                         'returns bytes, not (wthr, imgs)')
class TestWeatherImageSpace:
    """WTHR HDR tone mapping -> companion IMGS records.

    Oblivion stores HDR per WEATHER (WTHR.HNAM, 14 floats); Skyrim has NO
    per-weather HDR field -- it lives in imagespaces the weather points at
    (WTHR.IMSP -> IMGS.HNAM, 9 floats), FOUR of them, one per time of day.
    Pointing every weather at the stock 0x161 left HDR undefined: 0x161 is one
    of only two vanilla imagespaces that ship ENAM and no HNAM.
    """

    class _FakeWriter:
        def __init__(self):
            self.next_fid = 0x01001000

        def alloc_formid(self):
            self.next_fid += 1
            return self.next_fid

    # (min, max) per field across the 213 vanilla imagespaces a Skyrim.esm
    # WEATHER actually references (interior/dungeon ones excluded).
    VANILLA_RANGES = [
        (15.0, 50.0), (0.8, 8.0), (0.0, 0.80), (0.0, 7.0), (0.2, 1.0),
        (0.6, 1.075), (0.4, 3.85), (0.0, 0.45), (1.0, 30.0),
    ]
    DAWN, DAY, DUSK, NIGHT = 0, 1, 2, 3

    def _nam0(self, day_rgb=(100, 141, 191), night_rgb=(6, 8, 14)):
        """TES4 NAM0 with a bright day sky and a dark night sky."""
        raw = bytearray(160)
        for t, rgb in ((0, day_rgb), (1, day_rgb), (2, day_rgb), (3, night_rgb)):
            o = (0 * 4 + t) * 4          # slot 0 = Sky-Upper
            raw[o:o + 3] = bytes(rgb)
        return bytes(raw).hex().upper()

    def _rec(self, **over):
        rec = {
            'Signature': 'WTHR', 'FormID': '00000200', 'RecordFlags': '0',
            'EditorID': 'TestWeather',
            'NAM0.Size': '160', 'NAM0.Data': self._nam0(),
            'HNAM.EyeAdaptSpeed': '0.7', 'HNAM.BlurRadius': '4.0',
            'HNAM.BlurPasses': '2.0', 'HNAM.EmissiveMult': '1.0',
            'HNAM.TargetLum': '1.2', 'HNAM.UpperLumClamp': '1.0',
            'HNAM.BrightScale': '1.75', 'HNAM.BrightClamp': '0.3',
            'HNAM.SunlightDimmer': '1.3', 'HNAM.GrassDimmer': '1.3',
            'HNAM.TreeDimmer': '1.2',
        }
        rec.update(over)
        return rec

    def _convert(self, rec):
        from tes5_import.record_types.dialog_misc import convert_WTHR
        return convert_WTHR(rec, self._FakeWriter())

    def _hnam(self, imgs_bytes):
        return struct.unpack('<9f', _find_subrecord(imgs_bytes, b'HNAM')[:36])

    def test_weather_mints_four_imagespaces_with_hdr(self):
        """70% of vanilla weathers use DISTINCT imagespaces per time of day;
        collapsing to one gives day and night identical tone mapping."""
        _wthr, imgs = self._convert(self._rec())
        assert len(imgs) == 4
        for b in imgs:
            assert _find_subrecord(b, b'HNAM') is not None, (
                'an ENAM-only stub leaves HDR undefined')
            assert len(_find_subrecord(b, b'HNAM')) == 36

    def test_imsp_points_at_the_generated_imagespaces_in_order(self):
        wthr, imgs = self._convert(self._rec())
        fids = [struct.unpack_from('<I', b, 12)[0] for b in imgs]
        slots = list(struct.unpack('<4I', _find_subrecord(wthr, b'IMSP')))
        assert slots == fids
        assert 0x161 not in slots, 'must not use the HNAM-less stock imagespace'
        assert len(set(slots)) == 4

    def test_hdr_fields_come_from_the_tes4_block(self):
        _w, imgs = self._convert(self._rec())
        h = self._hnam(imgs[self.DAY])
        # BrightScale 1.75 rescaled from TES4 1..3 onto vanilla 2.5..4
        assert 2.5 <= h[3] <= 4.0
        # SunlightDimmer 1.3 rescaled from TES4 0.5..2 onto vanilla 0.9..2.7
        assert 1.5 <= h[6] <= 2.4

    def test_bloom_blur_radius_is_the_engine_constant(self):
        """All 213 vanilla weather-used imagespaces use exactly 7.0.  TES4's
        BlurRadius is a different quantity (Oblivion's own blur pass)."""
        _w, imgs = self._convert(self._rec(**{'HNAM.BlurRadius': '4.0'}))
        for b in imgs:
            assert self._hnam(b)[1] == 7.0

    def test_luminance_fields_are_rescaled_off_the_tes5_ceiling(self):
        """TES4 TargetLum spans 0.75..1.2 but TES5 Receive Bloom Threshold
        spans 0.2..1.0; copying raw pinned it at 1.0, so the WHOLE frame
        bloomed.  Same for UpperLumClamp (1.0..1.3) vs White (0.6..1.075)."""
        _w, imgs = self._convert(self._rec(**{'HNAM.TargetLum': '1.2',
                                              'HNAM.UpperLumClamp': '1.3'}))
        h = self._hnam(imgs[self.DAY])
        assert h[4] < 0.8, 'Receive Bloom Threshold must not sit at the ceiling'
        assert h[5] < 1.05, 'White must not sit at the ceiling'

    def test_sky_scale_tracks_sky_brightness(self):
        """Sky Scale is the sky's contribution to exposure and TES4 has no
        equivalent.  Vanilla: ~0.025 for a dark night sky, ~0.20 for a lit
        one.  A flat value washes the day sky out to near-white."""
        _w, imgs = self._convert(self._rec())
        day = self._hnam(imgs[self.DAY])[7]
        night = self._hnam(imgs[self.NIGHT])[7]
        assert night < 0.06, 'night sky scale must be near zero'
        assert day > 0.15, 'day sky scale must be the lit value'
        assert day > night

    def test_eye_adapt_varies_by_time_of_day(self):
        """Vanilla per-slot medians: speed 37/40/37/45, strength 15/5/15/20."""
        _w, imgs = self._convert(self._rec())
        strength = [self._hnam(b)[8] for b in imgs]
        assert strength == [15.0, 5.0, 15.0, 20.0]
        speed = [self._hnam(b)[0] for b in imgs]
        assert speed[self.NIGHT] > speed[self.DAY] > speed[self.DAWN]

    def test_eye_adapt_speed_is_rescaled_not_copied(self):
        """Oblivion's rate is 0..1 (Oblivion.ini fEyeAdaptSpeed=0.7); Skyrim's
        weather-used range is 15..50.  Copying 0.7 freezes adaptation."""
        _w, imgs = self._convert(self._rec(**{'HNAM.EyeAdaptSpeed': '0.7'}))
        speed = self._hnam(imgs[self.DAY])[0]
        assert speed > 15.0, 'a raw 0.7 would sit far below the vanilla floor'
        assert 15.0 <= speed <= 50.0

    def test_unauthored_tes4_hdr_uses_vanilla_defaults(self):
        """DefaultWeather ships an ALL-ZERO HNAM -- and it is exactly the
        weather the 57 CNAM-less worldspaces (Tamriel, every city) fall back
        to.  Copied verbatim that gives White=0 / SunlightScale=0 (a zero
        white point); clamping to the range MINIMUM instead pins every field
        at its flattest legal value.  Neither is right: use vanilla defaults."""
        zero = {k: '0.0' for k in self._rec() if k.startswith('HNAM.')}
        _w, imgs = self._convert(self._rec(**zero))
        h = self._hnam(imgs[self.DAY])
        assert h[1] == 7.0                   # Bloom Blur Radius
        assert abs(h[3] - 3.0) < 1e-6        # Bloom Scale, vanilla median
        assert abs(h[6] - 1.9) < 1e-3        # Sunlight Scale, vanilla median
        assert h[5] >= 0.6, 'White must never be 0 (degenerate white point)'
        for v, (lo, hi) in zip(h, self.VANILLA_RANGES):
            assert lo - 1e-6 <= v <= hi + 1e-6

    def test_every_field_stays_inside_the_vanilla_envelope(self):
        wild = {k: '999.0' for k in self._rec() if k.startswith('HNAM.')}
        for rec in (self._rec(**wild), self._rec()):
            _w, imgs = self._convert(rec)
            for b in imgs:
                for v, (lo, hi) in zip(self._hnam(b), self.VANILLA_RANGES):
                    assert lo - 1e-6 <= v <= hi + 1e-6

    def test_imagespace_ships_cinematic_and_tint(self):
        """Every vanilla IMGS with an HNAM also ships CNAM+TNAM."""
        _w, imgs = self._convert(self._rec())
        cnam = struct.unpack('<3f', _find_subrecord(imgs[0], b'CNAM'))
        tnam = struct.unpack('<4f', _find_subrecord(imgs[0], b'TNAM'))
        assert cnam == (1.0, 1.0, 1.0)
        assert tnam[0] == 0.0

    def test_imagespace_is_written_before_the_weather(self):
        """IMGS must precede WTHR in the group order: the weather's IMSP
        resolves against it, and CLMT then resolves against the weather."""
        import inspect

        from tes5_import.writer import PluginWriter
        src = inspect.getsource(PluginWriter)
        assert src.index("'IMGS'") < src.index("'WTHR'")
        assert src.index("'WTHR'") < src.index("'CLMT'")


class TestVanillaMgefDataSize:
    """The vanilla MGEF DATA table must hold FULL 152-byte structs.

    tools/gen_vanilla_mgef_table.py used to read the Skyrim.esm dump with
    `line.split('...')[0]`, and the dump truncated hex at 96 bytes — so every
    committed blob was 96 bytes and every synthesized aimed-variant MGEF
    shipped a DATA missing its last 14 fields (HitEffectArt, ImpactData,
    DualCasting, EnchantArt, HitVisuals, EquipAbility, IMAD, PerkToApply,
    CastingSoundLevel, ScriptEffect AI score/delay).
    """

    def test_every_committed_blob_is_a_full_struct(self):
        from tes5_import.vanilla_mgef_data import (
            MGEF_DATA_SIZE,
            VANILLA_MGEF_DATA,
        )
        assert MGEF_DATA_SIZE == 152
        assert VANILLA_MGEF_DATA, 'table is empty'
        for fid, (edid, data_hex) in VANILLA_MGEF_DATA.items():
            n = len(bytes.fromhex(data_hex))
            assert n == MGEF_DATA_SIZE, (
                f'{edid} ({fid:08X}) DATA is {n} bytes, expected '
                f'{MGEF_DATA_SIZE} — regenerate the table')

    def test_aimed_variant_writes_a_full_length_data(self):
        """The synthesized clone must be a complete, correctly-patched MGEF."""
        from tes5_import import magic_effects
        from tes5_import.vanilla_mgef_data import MGEF_DATA_SIZE

        class _Writer:
            def __init__(self):
                self.fid = 0x01000000
                self.records = []

            def alloc_formid(self):
                self.fid += 1
                return self.fid

            def add_record(self, rec_type, data):
                self.records.append((rec_type, data))

        magic_effects.set_tes4_effect_names(
            [{'EditorID': 'FIDG', 'FULL': 'Fire Damage'}])
        writer = _Writer()
        # FireDamageFFAimed — a vanilla effect that already has a projectile.
        assert magic_effects.aimed_variant(0x00012F03, 'FIDG', writer)

        rec_type, blob = writer.records[0]
        assert rec_type == 'MGEF'
        data = _find_subrecord(blob, b'DATA')
        assert len(data) == MGEF_DATA_SIZE

        # The patched fields: Fire and Forget + Aimed + a real projectile, and
        # no counter-effect slots (the clone carries no ESCE subrecords).
        assert struct.unpack_from('<I', data, 80)[0] == 1   # Casting Type
        assert struct.unpack_from('<I', data, 84)[0] == 2   # Delivery = Aimed
        assert struct.unpack_from('<I', data, 72)[0] != 0   # Projectile
        assert struct.unpack_from('<I', data, 20)[0] == 0   # Counter count

        # A field that only exists past the old 96-byte cut, proving the tail
        # is present: vanilla FireDamageFFAimed has DualCastScale == 1.0.
        assert struct.unpack_from('<f', data, 112)[0] == pytest.approx(1.0)

    def test_short_blob_is_rejected_rather_than_written(self):
        from tes5_import import magic_effects
        original = magic_effects.VANILLA_MGEF_DATA.get(0x00012F03)
        magic_effects.VANILLA_MGEF_DATA[0x00012F03] = ('Truncated', '00' * 96)
        magic_effects._cache.clear()
        try:
            with pytest.raises(ValueError, match='expected 152'):
                magic_effects.aimed_variant(0x00012F03, 'FIDG', object())
        finally:
            magic_effects.VANILLA_MGEF_DATA[0x00012F03] = original
            magic_effects._cache.clear()


class TestMgefConversion:
    """MGEF is a CONVERTED record type, not an alias to a vanilla effect.

    It used to be in SKIP_TYPES, with every effect on every SPEL/ENCH/ALCH/
    INGR/SGST re-pointed at a vanilla Skyrim MGEF through a flat 4-char code
    table.  A flat table cannot express an effect PARAMETERISED BY A FORMID
    the source record carries, so all 33 summons and every bound weapon/armor
    were dropped: 796 effects lost and 382 records gutted to zero-magnitude
    filler across Oblivion.esm alone.
    """

    def test_mgef_is_dispatched_not_skipped(self):
        from tes5_import.constants import IMPORT_DISPATCH, SKIP_TYPES
        assert 'MGEF' not in SKIP_TYPES
        assert 'MGEF' in IMPORT_DISPATCH

    def test_every_source_effect_code_has_an_archetype(self):
        """A code with no table entry silently becomes a Value Modifier.

        Validated against the EXPORT, never against a plausible-looking name:
        17 of the old alias table's 100 keys were codes no Oblivion or Nehrim
        record uses, so the summons looked mapped and contributed nothing.
        """
        import glob
        import re
        from tes5_import.record_types.magic import EFFECT_ARCHETYPES

        root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'export')
        codes = set()
        for path in glob.glob(os.path.join(root, '*', 'MGEF.txt')):
            with open(path, encoding='utf-8', errors='replace') as f:
                codes |= set(re.findall(r'^EditorID=(\S+)', f.read(), re.M))
        if not codes:
            pytest.skip('no MGEF export available')

        missing = sorted(codes - set(EFFECT_ARCHETYPES))
        assert not missing, (
            f'{len(missing)} source effect codes have no archetype and would '
            f'default to Value Modifier: {" ".join(missing)}')

        phantom = sorted(set(EFFECT_ARCHETYPES) - codes)
        assert not phantom, (
            f'{len(phantom)} table keys match no MGEF in any export — dead '
            f'coverage that can never fire: {" ".join(phantom)}')

    def test_data_is_a_full_152_byte_struct(self):
        from tes5_import.record_types.magic import MGEF_DATA_SIZE, convert_MGEF
        rec = {'FormID': '00001857', 'EditorID': 'BWSW', 'FULL': 'Bound Sword',
               'DATA.Flags': '76562', 'DATA.School': '1',
               'DATA.ResistValue': '4294967295', 'DATA.BaseCost': '1.0'}
        data = _find_subrecord(convert_MGEF(rec), b'DATA')
        assert len(data) == MGEF_DATA_SIZE

    def test_assoc_item_only_written_for_archetypes_that_read_it(self):
        """`wbMGEFAssocItemDecider` reads Assoc. Item for 10 archetypes only.

        A creature FormID under a value-modifier archetype is meaningless, and
        xEdit flags it — so it must be dropped rather than written blind.
        """
        from tes5_import.record_types import magic

        magic.set_assoc_item_index({}, {0x01001234: 'CREA'})
        # Summon Creature (18) reads it...
        assert magic._resolve_assoc_item(
            0x01001234, magic.A_SUMMON_CREATURE, magic.T4_USE_CREATURE) == 0x01001234
        # ...a plain Value Modifier does not.
        assert magic._resolve_assoc_item(
            0x01001234, magic.A_VALUE_MODIFIER, magic.T4_USE_CREATURE) == 0

    def test_summon_of_a_leveled_list_resolves_to_a_concrete_actor(self):
        """Skyrim's Summon Creature takes an NPC_, never an LVLN.

        Two Oblivion summons (Z004 Flesh Atronach, Z011 Wabba) name an LVLC,
        which converts to an LVLN — a type the archetype rejects, so the
        list's first entry stands in or the spell summons nothing.
        """
        from tes5_import.record_types import magic

        magic.set_assoc_item_index({0x0100AAAA: 0x0100BBBB},
                                   {0x0100AAAA: 'LVLC', 0x0100BBBB: 'CREA'})
        assert magic._resolve_assoc_item(
            0x0100AAAA, magic.A_SUMMON_CREATURE,
            magic.T4_USE_CREATURE) == 0x0100BBBB

    def test_every_spell_gets_an_equip_type(self):
        """ETYP is mandatory: without it a spell never reaches the magic menu.

        ETYP names the slot the menu files a spell under. A converted spell
        that omitted it could be added by console but was invisible and
        uncastable (user-confirmed on the Bound Dagger/Mace spells). Oblivion
        has no equivalent field, so it is derived from the spell type.

        Census of references/Skyrim.esm: 827/827 spells carry ETYP with no
        exceptions in any type. Vanilla's majority choice is EitherHand for
        ordinary spells and abilities, Voice for powers.
        """
        import struct as _s
        from tes5_import.record_types import equipment
        from tes5_import.skyrim_overrides import (SPELL_EQUIP_EITHER_HAND,
                                                  SPELL_EQUIP_VOICE)

        def _etyp(spit_type):
            rec = {'FormID': '000A97BD', 'EditorID': 'S',
                   'SPIT.Type': str(spit_type), 'SPIT.Cost': '50',
                   'SPIT.Flags': '0', 'EffectCount': '1',
                   'Effect[0].EFID': 'DGHE', 'Effect[0].Magnitude': '1',
                   'Effect[0].Area': '0', 'Effect[0].Duration': '5',
                   'Effect[0].Type': 'Target'}
            out = equipment.convert_SPEL(rec)
            assert out.find(b'ETYP') < out.find(b'SPIT'), 'ETYP must precede SPIT'
            return _s.unpack('<I', _find_subrecord(out, b'ETYP'))[0]

        assert _etyp(0) == SPELL_EQUIP_EITHER_HAND    # Spell
        assert _etyp(1) == SPELL_EQUIP_EITHER_HAND    # Disease
        assert _etyp(4) == SPELL_EQUIP_EITHER_HAND    # Ability
        assert _etyp(2) == SPELL_EQUIP_VOICE          # Power
        assert _etyp(3) == SPELL_EQUIP_VOICE          # Lesser Power

    def test_bound_items_use_the_script_only_where_the_engine_cannot(self):
        """Archetype 17 covers strictly less than Oblivion's bound-item family.

        Two independent gaps, both verified against references/Skyrim.esm:

        * **No bound armor at all.** All seven vanilla archetype-17 effects
          name a WEAP; not one names an ARMO. xEdit types the field as
          [WEAP, ARMO, NULL], but that is what it ACCEPTS, not proof the
          engine equips armor — user-confirmed in-game, casting the converted
          Bound Greaves spell did nothing.
        * **Only fires on a cast.** Archetype 17 appears under SPIT.Type 0
          only, never Type 3/4, so an Ability or Lesser Power (which Skyrim
          applies passively) never reaches BoundItemEffect even for a weapon.

        So: armor is always scripted; a weapon is scripted only when the
        spell cannot cast, and otherwise keeps the engine's own path.
        """
        import struct as _s
        from tes5_import.record_types import equipment, magic

        class _W:
            def __init__(self):
                self.n = 0x800
                self.recs = []

            def alloc_formid(self):
                self.n += 1
                return self.n

            def add_record(self, sig, b):
                self.recs.append((sig, b))

        def _mgef(fid, code, assoc):
            return {'FormID': fid, 'EditorID': code, 'DATA.Flags': '139538',
                    'DATA.BaseCost': '1.0', 'DATA.AssocItem': assoc,
                    'DATA.School': '1', 'DATA.ResistValue': '4294967295'}

        # BAGR (bound greaves) -> ARMO; BWSW (bound sword) -> WEAP.
        magic.set_assoc_item_index({}, {0x00026270: 'ARMO', 0x0002627C: 'WEAP'})
        magic.register_mgef_formids([
            _mgef('0000184F', 'BAGR', '00026270'),
            _mgef('00001857', 'BWSW', '0002627C'),
        ])

        def _spell(code, spit_type):
            return {'FormID': '000A97B9', 'EditorID': 'BoundSpell',
                    'SPIT.Type': str(spit_type), 'SPIT.Cost': '0',
                    'SPIT.Flags': '0', 'EffectCount': '1',
                    'Effect[0].EFID': code, 'Effect[0].Magnitude': '0',
                    'Effect[0].Area': '0', 'Effect[0].Duration': '45',
                    'Effect[0].Type': 'Self'}

        def _first_efid(record_bytes):
            return _s.unpack('<I', _find_subrecord(record_bytes, b'EFID'))[0]

        armor_base = magic.get_mgef_formid('BAGR')
        weapon_base = magic.get_mgef_formid('BWSW')

        # A bound WEAPON on a castable spell keeps the engine's own archetype.
        w = _W()
        assert _first_efid(
            equipment.convert_SPEL(_spell('BWSW', 0), writer=w)) == weapon_base
        assert w.recs == []

        # A bound weapon on an ABILITY cannot cast, so it gets the script.
        w = _W()
        assert _first_efid(
            equipment.convert_SPEL(_spell('BWSW', 4), writer=w)) != weapon_base
        assert len(w.recs) == 1

        # Bound ARMOR is scripted even on a perfectly castable spell — Skyrim
        # has no bound-armor implementation for it to fall back on.
        w = _W()
        scripted = _first_efid(
            equipment.convert_SPEL(_spell('BAGR', 0), writer=w))
        assert scripted != armor_base
        assert len(w.recs) == 1
        sig, clone = w.recs[0]
        assert sig == 'MGEF'
        data = _find_subrecord(clone, b'DATA')
        assert _s.unpack_from('<I', data, magic._O_ARCHETYPE)[0] == magic.A_SCRIPT
        # Assoc. Item is "Unused" under archetype 1 — the item travels as the
        # script's BoundItem property instead.
        assert _s.unpack_from('<I', data, magic._O_ASSOC_ITEM)[0] == 0
        assert magic.BOUND_ITEM_SCRIPT.encode() in clone
        assert b'BoundItem' in clone

        # A lesser power is equally uncastable, and shares the cached clone.
        w2 = _W()
        assert _first_efid(
            equipment.convert_SPEL(_spell('BAGR', 3), writer=w2)) == scripted
        assert w2.recs == []

    def test_counter_effect_count_matches_the_esce_array(self):
        """DATA offset 20 must equal the ESCE count or the CK reads garbage."""
        import struct as _s
        from tes5_import.record_types import magic

        magic.register_mgef_formids([
            {'EditorID': 'DSPL', 'FormID': '00001234'},
            {'EditorID': 'CUDI', 'FormID': '00001235'},
        ])
        rec = {'FormID': '00001863', 'EditorID': 'CALM', 'DATA.School': '3',
               'DATA.Flags': '0', 'CounterEffects': '2',
               'ESCE[0]': 'DSPL', 'ESCE[1]': 'CUDI'}
        blob = magic.convert_MGEF(rec)
        data = _find_subrecord(blob, b'DATA')
        assert _s.unpack_from('<H', data, 20)[0] == blob.count(b'ESCE') == 2

    def test_attribute_effects_get_a_per_actor_value_variant(self):
        """One TES4 DGAT is Damage Strength on one spell, Damage Endurance on
        the next — the AV lives in the ITEM's EFIT.  Skyrim moved it onto the
        MGEF, so a single converted DGAT could only ever damage one stat.
        """
        import struct as _s
        from tes5_import.record_types import magic

        class _Writer:
            def __init__(self):
                self.fid = 0x01000000
                self.records = []

            def alloc_formid(self):
                self.fid += 1
                return self.fid

            def add_record(self, rec_type, data):
                self.records.append((rec_type, data))

        writer = _Writer()
        mgefs = [{'EditorID': 'DGAT', 'FormID': '00001863',
                  'FULL': 'Damage Attribute', 'DATA.School': '2',
                  'DATA.Flags': '0'}]
        effects = [
            {'EffectCount': '1', 'Effect[0].EFID': 'DGAT',
             'Effect[0].ActorValue': '0'},    # Strength
            {'EffectCount': '1', 'Effect[0].EFID': 'DGAT',
             'Effect[0].ActorValue': '5'},    # Endurance
        ]
        assert magic.build_av_variants(mgefs, effects, writer) == 2

        strength = magic.get_mgef_formid('DGAT', 0)
        endurance = magic.get_mgef_formid('DGAT', 5)
        assert strength and endurance and strength != endurance

        avs = {}
        for _, blob in writer.records:
            data = _find_subrecord(blob, b'DATA')
            avs[_s.unpack_from('<I', blob, 12)[0]] = \
                _s.unpack_from('<i', data, 68)[0]
        assert avs[strength] == magic.AV_CARRY_WEIGHT
        assert avs[endurance] == magic.AV_HEALTH


class TestEnchantedBookIsAScroll:
    """A TES4 BOOK carrying an ENAM is a SCROLL, and Skyrim's BOOK has NO
    field for an object effect.

    Converting one to a BOOK produced a blank page that could never be cast —
    503 of them across Oblivion, Nehrim and Morrowind_ob, the Scroll of
    Icarian Flight among them.  Skyrim's record for this is SCRL, which
    carries its effects directly rather than through an enchantment link.
    """

    def test_enchanted_book_converts_to_scrl_with_the_ench_effects(self):
        from tes5_import.record_types import equipment, magic

        magic.register_mgef_formids([{'EditorID': 'REHE',
                                      'FormID': '00001234'}])
        equipment.set_ench_index([{
            'FormID': '000402B1', 'Signature': 'ENCH', 'ENIT.Cost': '350',
            'EffectCount': '1', 'Effect[0].EFID': 'REHE',
            'Effect[0].Type': 'Self', 'Effect[0].Magnitude': '25',
            'Effect[0].Duration': '7', 'Effect[0].ActorValue': '-1',
        }])
        blob = equipment.convert_BOOK({
            'FormID': '0018022C', 'EditorID': 'ScrollOfHealing',
            'FULL': 'Scroll of Healing', 'ENAM': '000402B1',
            'DATA.Flags': '1', 'DATA.Value': '119', 'DATA.Weight': '0.2',
        })
        assert blob[:4] == b'SCRL', 'an enchanted book must become a scroll'
        # CastType 3 = Scroll, matching every vanilla SCRL.
        assert struct.unpack_from('<I', _find_subrecord(blob, b'SPIT'), 16)[0] == 3
        # The payload came from the ENCH, not from the (effect-less) book.
        assert b'EFID' in blob

    def test_plain_book_stays_a_book(self):
        from tes5_import.record_types import equipment

        equipment.set_ench_index([])
        blob = equipment.convert_BOOK({
            'FormID': '0018022D', 'EditorID': 'PlainBook',
            'FULL': 'A Book', 'DATA.Value': '10', 'DATA.Weight': '1.0',
        })
        assert blob[:4] == b'BOOK'


class TestEfitFieldOrder:
    """TES5 EFIT is Magnitude, AREA, DURATION (xEdit wbEFIT).

    The dump tool had the last two labelled the other way round, which made
    every converted spell look like its duration and area were swapped.
    Settled by census: all 427 vanilla ALCH effects write 0 at offset 4 and
    30/60/300/720 at offset 8 — potion durations in seconds, and potions have
    no area.
    """

    def test_writer_packs_area_before_duration(self):
        from tes5_import.record_types import equipment, magic

        magic.register_mgef_formids([{'EditorID': 'REHE',
                                      'FormID': '00001234'}])
        blob = equipment.convert_SPEL({
            'FormID': '0000ABCD', 'EditorID': 'TestHeal',
            'SPIT.Cost': '10', 'SPIT.Type': '0',
            'EffectCount': '1', 'Effect[0].EFID': 'REHE',
            'Effect[0].Magnitude': '25', 'Effect[0].Area': '3',
            'Effect[0].Duration': '120', 'Effect[0].ActorValue': '-1',
        })
        efit = _find_subrecord(blob, b'EFIT')
        assert struct.unpack_from('<f', efit, 0)[0] == pytest.approx(25.0)
        assert struct.unpack_from('<I', efit, 4)[0] == 3      # Area
        assert struct.unpack_from('<I', efit, 8)[0] == 120    # Duration


class TestPlayGroupTargetRouting:
    """`PlayGroup` picks its API from WHAT THE TARGET IS, not from syntax.

    Animated OBJECTS (ACTI/DOOR/STAT/MSTT with a NiControllerManager) keep
    their TES4 sequence names in the converted NIF, so they need
    `PlayAnimation("Forward")`. ACTORS need `Debug.SendAnimationEvent` — a
    PlayAnimation() on an actor corrupts its behavior graph.

    Routing every EXPLICIT-REF call to SendAnimationEvent broke every
    lever-operated secret door in the game (196 calls across 86 scripts:
    Anvil/Bravil castle doors, Anga, mine traps). CharacterGen's
    `CGPrisonSecretWallRef.playgroup forward 1` became
    `SendAnimationEvent(..., "moveStart")`, which an activator has no behavior
    graph to receive, so Renault threw the switch and the wall never moved.
    The tell: the SELF-call on the very next TES4 line converted correctly, so
    two identical statements behaved differently.
    """

    def _xref(self, sigs):
        """A real CrossRefGraph with just the records this test needs.

        `sigs` is {EditorID: base signature}; each entry gets a FormID, a
        record_type, and (for placed refs) a record_base so
        get_base_signature() resolves exactly as it does in a real run.
        """
        from script_convert.cross_ref import CrossRefGraph
        x = CrossRefGraph()
        for i, (name, sig) in enumerate(sigs.items()):
            ref_fid = f'{0x00010000 + i * 2:08X}'
            base_fid = f'{0x00010001 + i * 2:08X}'
            x.edid_to_formid[name.lower()] = ref_fid
            x.formid_to_edid[ref_fid] = name
            x.record_type[ref_fid] = 'REFR'
            x.record_base[ref_fid] = base_fid
            x.record_type[base_fid] = sig
        return x

    def _convert(self, line, sigs, extends='ObjectReference'):
        from script_convert.converter import ScriptConverter
        conv = ScriptConverter(self._xref(sigs))
        return '\n'.join(conv.convert_fragment(line, extends))

    def test_activator_ref_gets_playanimation(self):
        out = self._convert('CGPrisonSecretWallRef.playgroup forward 1',
                            {'CGPrisonSecretWallRef': 'ACTI'})
        assert 'PlayAnimation("Forward")' in out, out
        assert 'SendAnimationEvent' not in out, (
            'an activator has no behavior graph — SendAnimationEvent is inert '
            'and the door never moves')
        assert 'CGPrisonSecretWallRef.PlayAnimation' in out, (
            'PlayAnimation is an ObjectReference method: it must play on the '
            'named ref, not on Self')

    def test_actor_ref_keeps_animation_event(self):
        out = self._convert('ArmandChristopheRef.playgroup idle 1',
                            {'ArmandChristopheRef': 'NPC_'})
        assert 'SendAnimationEvent' in out, out
        assert 'PlayAnimation' not in out, (
            'PlayAnimation on an actor corrupts its behavior graph')

    def test_unknown_ref_falls_back_to_the_safe_api(self):
        """Unknown target: the event is inert on an object but never corrupts
        an actor, so it is the safe default."""
        out = self._convert('SomeUnknownRef.playgroup forward 1', {})
        assert 'SendAnimationEvent' in out, out

    def test_self_call_in_object_script_still_plays_the_sequence(self):
        out = self._convert('playgroup forward 1', {})
        assert 'Self.PlayAnimation("Forward")' in out, out


class TestOwnedGroupAnchoring:
    """A type-1/6/7 GRUP must be preceded by the record that owns it.

    xEdit's TwbGroupRecord.InformPrevMainRecord (wbImplementation.pas ~18023)
    binds these groups to a record ONLY by physical adjacency:

        grsGroupType in [1, 6, 7] and aPrevMainRecord.FixedFormID = GroupLabel

    An unanchored group is attached to nothing, so every record inside it is
    unreachable in-engine — the file still loads and the records still show up
    in xEdit, which is what made this silent. DLCBattlehornCastle.esp emitted a
    Tamriel type-1 world-children group with no WRLD record in front of it and
    lost all 473 of its exterior cell/reference overrides.
    """

    class _Writer:
        """Minimal PluginWriter stand-in capturing the raw top-level groups."""

        def __init__(self):
            self.groups = {}

        def add_raw_group(self, group_sig, group_bytes):
            self.groups[group_sig] = group_bytes

    class _MasterIndex:
        """Master stub: FormID -> record bytes, with a known group nesting."""

        def __init__(self, records):
            self._records = records

        def record(self, formid):
            return self._records.get(formid, b'')

    @staticmethod
    def _record(sig, formid):
        return sig + struct.pack('<I', 0) + b'\x00' * 4 + \
            struct.pack('<I', formid) + b'\x00' * 8

    @staticmethod
    def _orphans(blob):
        """Owned groups in `blob` whose owner does not precede them."""
        orphans = []

        def walk(off, end):
            prev = None
            while off + 24 <= end:
                sig = blob[off:off + 4]
                size = struct.unpack_from('<I', blob, off + 4)[0]
                if sig == b'GRUP':
                    gtype = struct.unpack_from('<i', blob, off + 12)[0]
                    label = blob[off + 8:off + 12]
                    if gtype in (1, 6, 7):
                        owner = struct.unpack('<I', label)[0]
                        if prev != owner:
                            orphans.append((gtype, owner))
                    walk(off + 24, off + size)
                    off += size
                    prev = None
                else:
                    prev = struct.unpack_from('<I', blob, off + 12)[0]
                    off += 24 + size
        walk(0, len(blob))
        return orphans

    def _emit(self, records, master_records):
        from tes5_import.overrides import emit_nested_overrides
        writer = self._Writer()
        emitted, orphaned, anchored = emit_nested_overrides(
            records, writer, self._MasterIndex(master_records))
        return writer, emitted, orphaned, anchored

    def test_worldspace_children_group_gets_its_wrld_anchor(self):
        """Overriding a master's exterior CELL without touching the WRLD."""
        wrld_fid = 0x0100003C
        cell_fid = 0x01007AC6
        path = ((0, b'WRLD'), (1, struct.pack('<I', wrld_fid)),
                (4, struct.pack('<hh', 0, -1)), (5, struct.pack('<hh', 2, -3)))
        records = [(cell_fid, self._record(b'CELL', cell_fid), path)]
        master = {wrld_fid: self._record(b'WRLD', wrld_fid)}

        writer, _emitted, orphaned, anchored = self._emit(records, master)
        blob = writer.groups['WRLD']

        assert orphaned == 0
        assert anchored == 1, 'the unchanged WRLD must be pulled in as an anchor'
        assert self._orphans(blob) == [], (
            'a type-1 group with no WRLD in front of it is attached to '
            'nothing and the engine indexes none of its cells')
        assert blob.index(struct.pack('<I', wrld_fid)) < blob.index(b'GRUP'), \
            'the WRLD record must come BEFORE its children group'

    def test_cell_children_group_gets_its_cell_anchor(self):
        """Overriding a master's REFR without touching the parent CELL."""
        cell_fid = 0x01007AC7
        refr_fid = 0x0201AF4B
        label = struct.pack('<I', cell_fid)
        path = ((0, b'CELL'), (2, struct.pack('<i', 1)),
                (3, struct.pack('<i', 11)), (6, label), (9, label))
        records = [(refr_fid, self._record(b'REFR', refr_fid), path)]
        master = {cell_fid: self._record(b'CELL', cell_fid)}

        writer, _emitted, orphaned, anchored = self._emit(records, master)

        assert orphaned == 0
        assert anchored == 1
        assert self._orphans(writer.groups['CELL']) == []

    def test_topic_children_group_gets_its_dial_anchor(self):
        """A new INFO under a master's unchanged DIAL."""
        dial_fid = 0x011D8200
        info_fid = 0x02001234
        path = ((0, b'DIAL'), (7, struct.pack('<I', dial_fid)))
        records = [(info_fid, self._record(b'INFO', info_fid), path)]
        master = {dial_fid: self._record(b'DIAL', dial_fid)}

        writer, _emitted, orphaned, anchored = self._emit(records, master)

        assert orphaned == 0
        assert anchored == 1
        assert self._orphans(writer.groups['DIAL']) == []

    def test_overridden_owner_is_not_duplicated(self):
        """When the plugin overrides the owner too, no anchor is added."""
        wrld_fid = 0x0100003C
        cell_fid = 0x01007AC6
        wrld_path = ((0, b'WRLD'),)
        cell_path = wrld_path + ((1, struct.pack('<I', wrld_fid)),
                                 (4, struct.pack('<hh', 0, -1)),
                                 (5, struct.pack('<hh', 2, -3)))
        # The plugin's OWN version of the WRLD (distinguishable from the
        # master's by its trailing byte) must be the one that survives.
        own_wrld = self._record(b'WRLD', wrld_fid) + b'\xAB'
        records = [(wrld_fid, own_wrld, wrld_path),
                   (cell_fid, self._record(b'CELL', cell_fid), cell_path)]
        master = {wrld_fid: self._record(b'WRLD', wrld_fid)}

        writer, _emitted, orphaned, anchored = self._emit(records, master)
        blob = writer.groups['WRLD']

        assert orphaned == 0
        assert anchored == 0, 'the plugin overrides the WRLD; no anchor needed'
        assert blob.count(struct.pack('<I', wrld_fid) + b'\x00' * 8) == 1, \
            'the WRLD must appear exactly once, not once per anchor attempt'
        assert b'\xAB' in blob, "the plugin's own WRLD must not be replaced"
        assert self._orphans(blob) == []

    def test_missing_master_record_leaves_the_group_unanchored(self):
        """No master record to anchor with: reported, never faked."""
        wrld_fid = 0x0100003C
        cell_fid = 0x01007AC6
        path = ((0, b'WRLD'), (1, struct.pack('<I', wrld_fid)),
                (4, struct.pack('<hh', 0, -1)), (5, struct.pack('<hh', 2, -3)))
        records = [(cell_fid, self._record(b'CELL', cell_fid), path)]

        _writer, _emitted, _orphaned, anchored = self._emit(records, {})

        assert anchored == 0, 'nothing to anchor with: no anchor is invented'


class TestLandOverrides:
    """A LAND override must carry the author's terrain, not the master's.

    VNML/VHGT/VCLR have identical layout in TES4 and TES5, so convert_LAND
    copies the hex blob straight through — which makes the authored value
    directly substitutable. Before these mappings existed, every terrain edit
    was reported as "no output mapping" and silently kept the master's terrain:
    DLCBattlehornCastle authored VHGT on all 16 of its LAND overrides, so the
    castle sat on Oblivion's untouched hillside.
    """

    _VHGT_PAYLOAD = 4 + 33 * 33          # float offset + 33x33 signed deltas

    def _land(self, **over):
        rec = {
            'Signature': 'LAND',
            'FormID': '00008EB7',
            'RecordFlags': '0',
            'DATA.Flags': '3',
            'LayerCount': '0',
        }
        rec.update(over)
        return rec

    def _vhgt(self, offset_byte=0x40, delta=0, pad='000000'):
        body = '%02x000000' % offset_byte + ('%02x' % (delta & 0xFF)) * 1089
        return body + pad

    def _subs(self, record):
        out = []
        off = 24
        while off + 6 <= len(record):
            sig = record[off:off + 4]
            size = struct.unpack_from('<H', record, off + 4)[0]
            out.append((sig, record[off + 6:off + 6 + size]))
            off += 6 + size
        return out

    def _apply(self, master_rec, plugin_rec):
        from tes5_import.export_diff import diff_records
        from tes5_import.override_builder import apply_changes
        from tes5_import.record_types.world import convert_LAND
        base = convert_LAND(master_rec)
        changes = diff_records(master_rec, plugin_rec)
        out, applied, unmapped = apply_changes(base, changes, plugin_rec,
                                               master_rec)
        return out, changes, applied, unmapped

    def test_authored_heights_replace_the_masters(self):
        master = self._land(VHGT=self._vhgt(delta=0))
        plugin = self._land(VHGT=self._vhgt(delta=5))

        out, changes, _applied, unmapped = self._apply(master, plugin)

        assert 'VHGT' in changes, 'the diff must see the terrain change'
        assert unmapped == set(), f'VHGT must be mappable, got {unmapped}'
        vhgt = dict(self._subs(out))[b'VHGT']
        assert vhgt[4:4 + 1089] == b'\x05' * 1089, (
            "the override kept the master's heights — the castle would sit on "
            "the master's terrain")

    def test_normals_and_colours_are_mapped(self):
        master = self._land(VNML='00' * 3267, VCLR='11' * 3267)
        plugin = self._land(VNML='7f' * 3267, VCLR='22' * 3267)

        out, _changes, _applied, unmapped = self._apply(master, plugin)
        subs = dict(self._subs(out))

        assert unmapped == set()
        assert subs[b'VNML'] == b'\x7f' * 3267
        assert subs[b'VCLR'] == b'\x22' * 3267

    def test_vhgt_trailing_pad_is_not_an_authored_change(self):
        """The last 3 VHGT bytes are wbUnused(3) — uninitialised CS memory.

        A census of 15,410 vanilla Skyrim.esm LAND records finds arbitrary junk
        there (000000 is merely the most common of many values), so the engine
        ignores them. Comparing them reported phantom VHGT changes on 6 of
        DLCBattlehornCastle's 16 LAND overrides whose real terrain was
        identical, emitting override records with no authored content.
        """
        from tes5_import.export_diff import diff_records
        master = self._land(VHGT=self._vhgt(delta=3, pad='d21b02'))
        plugin = self._land(VHGT=self._vhgt(delta=3, pad='000000'))

        assert diff_records(master, plugin) == {}, (
            'a pad-only difference must not count as an authored change')

    def test_real_change_keeps_the_masters_pad(self):
        """When terrain DID change, the master's unused bytes still survive."""
        master = self._land(VHGT=self._vhgt(delta=0, pad='d21b02'))
        plugin = self._land(VHGT=self._vhgt(delta=7, pad='000000'))

        out, changes, _applied, unmapped = self._apply(master, plugin)

        assert 'VHGT' in changes and unmapped == set()
        vhgt = dict(self._subs(out))[b'VHGT']
        assert vhgt[4:4 + 1089] == b'\x07' * 1089, 'authored heights applied'
        assert vhgt[self._VHGT_PAYLOAD:] == bytes.fromhex('d21b02'), (
            "the master's uninitialised pad must be preserved so the override "
            'differs only where the author changed the terrain')

    def test_layer_run_is_rebuilt_through_the_converters_own_builder(self):
        """Layer[] changes replace the whole BTXT/ATXT/VTXT run.

        The layer mapping is lossy and order-dependent (same-texture merge,
        coverage sort, 6-alpha cap), so the override MUST reuse
        build_land_layers rather than reimplement it.
        """
        master = self._land(**{
            'LayerCount': '1',
            'Layer[0].Type': 'BASE',
            'Layer[0].BTXT.Texture': '00001111',
            'Layer[0].BTXT.Quadrant': '0',
        })
        plugin = self._land(**{
            'LayerCount': '1',
            'Layer[0].Type': 'BASE',
            'Layer[0].BTXT.Texture': '00002222',
            'Layer[0].BTXT.Quadrant': '0',
        })

        out, changes, _applied, unmapped = self._apply(master, plugin)

        assert 'Layer[]' in changes
        assert unmapped == set(), f'Layer[] must be mappable, got {unmapped}'
        btxt = dict(self._subs(out))[b'BTXT']
        from tes5_import.text_reader import remap_formid
        assert struct.unpack_from('<I', btxt)[0] == remap_formid(0x00002222), \
            "the authored base texture must replace the master's"

    def test_unchanged_land_is_dropped_entirely(self):
        from tes5_import.export_diff import diff_records
        rec = self._land(VHGT=self._vhgt(delta=2), VNML='00' * 3267)
        assert diff_records(rec, self._land(VHGT=self._vhgt(delta=2),
                                            VNML='00' * 3267)) == {}


class TestQuestObjectiveOverrideText:
    """A translation plugin must retranslate the OBJECTIVE, not just the log.

    Skyrim's journal displays the objective (NNAM); the stage log entry (CNAM)
    is the collapsed history. Both derive from one TES4 field
    (`Stage[].Log[].Text`), and mapping that key to CNAM alone left every
    objective holding the master's language — 83 of Translation.esp's 84
    quests shipped English CNAM beside German NNAM.
    """

    def _quest(self, text, full):
        return {
            'Signature': 'QUST', 'FormID': '0000E6F3', 'RecordFlags': '0',
            'EditorID': 'NQ00Merre', 'FULL': full,
            'DATA.Flags': '1', 'DATA.Priority': '70',
            'StageCount': '1',
            'Stage[0].Index': '5', 'Stage[0].LogCount': '1',
            'Stage[0].Log[0].Flags': '0', 'Stage[0].Log[0].Text': text,
        }

    def _subs(self, record):
        out = []
        off = 24
        while off + 6 <= len(record):
            sig = record[off:off + 4]
            size = struct.unpack_from('<H', record, off + 6 - 2)[0]
            out.append((sig, record[off + 6:off + 6 + size]))
            off += 6 + size
        return out

    def test_objective_text_is_retranslated(self):
        from tes5_import.export_diff import diff_records
        from tes5_import.override_builder import apply_changes
        from tes5_import.dialog_converter import convert_QUST

        master = self._quest('Merre Quest Ratten', 'Arbeit in der Mine')
        plugin = self._quest("Merre's Rat Quest", 'Work in the Mine')

        base = convert_QUST(master)
        changes = diff_records(master, plugin)
        assert 'Stage[]' in changes

        out, _applied, unmapped = apply_changes(base, changes, plugin, master)
        assert unmapped == set(), f'Stage[] must be mappable, got {unmapped}'

        subs = dict(self._subs(out))
        assert subs[b'CNAM'] == b"Merre's Rat Quest\x00"
        assert subs[b'NNAM'] == b"Merre's Rat Quest\x00", \
            'the journal objective must carry the plugin\'s translation'

    def test_derivation_matches_the_converter(self):
        """The helper must yield exactly the NNAMs convert_QUST emits."""
        from tes5_import.dialog_converter import (convert_QUST,
                                                  quest_objective_texts)
        rec = self._quest('Some journal line', 'A Quest')
        emitted = [v for s, v in self._subs(convert_QUST(rec)) if s == b'NNAM']
        derived = quest_objective_texts(rec)
        assert len(emitted) == len(derived)
        assert emitted[0] == derived[0].encode() + b'\x00'


class TestOutfitIndexAcrossMasters(TestOutfitSplit):
    """A dependent plugin dresses its actors out of its MASTER's wardrobe.

    Inherits TestOutfitSplit's record builders. The item index used to be built
    from the plugin's own records alone, so every inventory entry naming a
    master record was unclassifiable, fell to the non-wearable default, and
    stayed in CNTO — the actor got no outfit at all. DLCBattlehornCastle draws
    155 of its 165 NPC inventory entries from Oblivion.esm, and all 22 of its
    NPCs (knights, captain, maid, cook) stood around undressed in-game.
    """

    def _index_with_master(self, master_types=None, **types):
        """Index the plugin's records plus a master export, as import does."""
        from tes5_import.outfits import load_item_index
        from tes5_import.text_reader import set_formid_index_offset
        set_formid_index_offset(0)
        master_export = {}
        for recs in (master_types or {}).values():
            for rec in recs:
                master_export[rec['FormID'].upper()] = rec
        load_item_index(types, master_export)

    def test_master_owned_armor_reaches_the_outfit(self):
        from tes5_import.outfits import split_inventory
        # The wearables live in the MASTER; the plugin contains none of them.
        self._index_with_master(master_types={'ARMO': [
            self._armo('0002A17E', 'IronCuirass', self.BODY, value=100),
            self._armo('0002A17F', 'IronGreaves', self.LEGS, value=90),
        ]})

        outfit, carried = split_inventory([(0x0002A17E, 1), (0x0002A17F, 1)])

        assert sorted(outfit) == [0x0002A17E, 0x0002A17F], (
            "master-owned armor was classified non-wearable and left in CNTO "
            "— the actor spawns naked")
        assert carried == []

    def test_plugin_record_wins_over_the_masters_same_id(self):
        """The plugin's own record overrides the master's at the same id."""
        from tes5_import.outfits import split_inventory
        # Master says this id is a potion (never wearable); the plugin's own
        # export says it is armor. The plugin is authoritative.
        self._index_with_master(
            master_types={'ALCH': [{'Signature': 'ALCH',
                                    'FormID': '0002A180'}]},
            ARMO=[self._armo('0002A180', 'PluginCuirass', self.BODY)])

        outfit, _carried = split_inventory([(0x0002A180, 1)])

        assert outfit == [0x0002A180], (
            "the plugin's own record must win — it is loaded last")

    def test_master_leveled_list_resolves_through_master_leaves(self):
        """An LVLI in the master whose leaves are also in the master."""
        from tes5_import.outfits import split_inventory
        self._index_with_master(master_types={
            'ARMO': [self._armo('0002B001', 'LeatherCuirass', self.BODY)],
            'LVLI': [self._lvli('0002B010', 'LL0Cuirass', ['0002B001'])],
        })

        outfit, carried = split_inventory([(0x0002B010, 1)])

        assert outfit == [0x0002B010], (
            'an all-armor master LVLI is outfit-eligible; unresolvable leaves '
            'would have made it non-wearable and dropped the outfit')
        assert carried == []

    def test_master_loot_still_stays_in_the_inventory(self):
        """Indexing the master must not sweep non-wearables into the outfit."""
        from tes5_import.outfits import split_inventory
        self._index_with_master(master_types={
            'ARMO': [self._armo('0002C001', 'Cuirass', self.BODY)],
            'ALCH': [{'Signature': 'ALCH', 'FormID': '0002C002'}],
            'KEYM': [{'Signature': 'KEYM', 'FormID': '0002C003'}],
        })

        outfit, carried = split_inventory(
            [(0x0002C001, 1), (0x0002C002, 3), (0x0002C003, 1)])

        assert outfit == [0x0002C001]
        assert sorted(carried) == [(0x0002C002, 3), (0x0002C003, 1)], (
            'potions and keys in an outfit are what the CK rejects with '
            '"contains non-armor objects"')

    def test_no_master_export_behaves_as_before(self):
        """A master's OWN run passes no master export and must be unaffected."""
        from tes5_import.outfits import split_inventory
        self._index(ARMO=[self._armo('0002D001', 'Cuirass', self.BODY)])

        outfit, carried = split_inventory([(0x0002D001, 1)])

        assert outfit == [0x0002D001] and carried == []


class TestAidtConfidenceTiers:
    """TES4 confidence is a 0-100 scalar; TES5 wants a 0-4 tier.

    xEdit wbConfidenceEnum: 0 Cowardly, 1 Cautious, 2 Average, 3 Brave,
    4 Foolhardy.  Only Foolhardy never flees.  The original mapping
    (`<30 -> 0, >=70 -> 3, else 2`) never produced tier 1 or tier 4, so
    Oblivion's most common "fearless" value 100 landed on Brave and actors
    kept running away.  Vanilla Skyrim's own 5,118 NPC_ records are
    292/90/1730/393/2613 across the five tiers.
    """

    @staticmethod
    def _conf(raw):
        from tes5_import.record_types.actors import _npc_aidt
        rec = {'AIDT.Aggression': '5', 'AIDT.Confidence': str(raw),
               'AIDT.Responsibility': '50', 'DATA.Personality': '50'}
        return _npc_aidt(rec)[1]

    def test_fearless_maps_to_foolhardy(self):
        assert self._conf(100) == 4

    def test_all_five_tiers_reachable(self):
        got = {self._conf(v) for v in range(0, 101)}
        assert got == {0, 1, 2, 3, 4}, f'unreachable tiers: {got}'

    def test_tier_is_monotonic_in_confidence(self):
        tiers = [self._conf(v) for v in range(0, 101)]
        assert tiers == sorted(tiers), 'more confidence must never mean more fleeing'

    def test_oblivion_default_50_is_average(self):
        """50 is Oblivion's engine default and must not read as cowardly."""
        assert self._conf(50) == 2

    def test_timid_still_flees(self):
        assert self._conf(0) == 0
        assert self._conf(5) == 0


class TestAggressionTierTargeting:
    """Aggression is "which reaction tier do I attack", not "how nasty am I".

    UESP Skyrim:NPCs#Aggression: "Together with the faction relationship combat
    modifier this governs whether the NPC initiates combat" — 1 attacks enemies
    on sight, 2 attacks enemies AND NEUTRALS (the player) on sight.

    UESP Oblivion:Animals draws the matching TES4 line for the dogs that broke
    this: randomly generated dogs "are Bandit or marauder dogs ... hostile
    towards you, ALTHOUGH THEY WILL NOT NECESSARILY ATTACK ON SIGHT", while
    "the other dogs in the game are all pets of townspeople and are friendly".
    """

    PREY_FID = '0005D556'

    def setup_method(self):
        from tes5_import.record_types.actors import load_faction_player_reactions
        load_faction_player_reactions({'FACT': [
            {'FormID': self.PREY_FID, 'EditorID': 'Prey', 'RelationCount': '0'},
        ]})

    @staticmethod
    def _aggr(aggression, personality, factions=()):
        from tes5_import.record_types.actors import _npc_aidt
        rec = {'AIDT.Aggression': str(aggression), 'AIDT.Confidence': '50',
               'AIDT.Responsibility': '50', 'DATA.Personality': str(personality),
               'FactionCount': str(len(factions))}
        for i, f in enumerate(factions):
            rec[f'Faction[{i}].FormID'] = f
        return _npc_aidt(rec, is_creature=True)[0]

    def test_marauder_pet_dog_not_hostile_on_sight(self):
        """Nehrim's Benno: aggr=30, Personality=10, Marauder+Bandit factions.

        Oblivion's own CreatureDog is byte-identical, so this is the general
        case, not a Nehrim quirk. Must be tier 1, never tier 2.
        """
        assert self._aggr(30, 10) == 1

    def test_predators_still_attack_on_sight(self):
        """Wolves/bears/lions/trolls must stay tier 2 or the wild goes passive."""
        for aggr in (45, 50, 60, 70, 80, 100):
            assert self._aggr(aggr, 10) == 2, aggr

    def test_prey_faction_never_attacks_on_sight(self):
        """Horses and deer carry aggression 100 but are harmless.

        Aggression alone cannot exclude them — this is why the Prey term is
        required rather than a threshold.
        """
        assert self._aggr(100, 50, [self.PREY_FID]) == 1   # horse
        assert self._aggr(100, 10, [self.PREY_FID]) == 1   # deer

    def test_prey_beats_aggression(self):
        """Same aggression, opposite outcome, decided purely by faction."""
        assert self._aggr(100, 10) == 2
        assert self._aggr(100, 10, [self.PREY_FID]) == 1

    def test_harmless_low_aggression_never_initiates(self):
        assert self._aggr(5, 10) == 0    # sheep
        assert self._aggr(0, 10) == 0

    def test_frenzied_still_maps(self):
        assert self._aggr(106, 50) == 3


class TestFactionRelationReaction:
    """XNAM Group Combat Reaction: 0 Neutral, 1 Enemy, 2 ALLY, 3 FRIEND.

    Ally and Friend were swapped until 2026-07-31. Confirmed by xEdit
    `wbFactionRelations` (wbDefinitionsCommon) and by Skyrim.esm, where 160 of
    200 faction SELF-relations use 2 — a faction is Ally to itself.

    Ally is the tier that makes members ASSIST each other into combat, so it is
    reserved for the self-relation. A TES4 disposition is only a 0-100 "likes
    them" scalar: Oblivion's CharacterGen data has BladesCG -> MythicDawnCG at
    +100 while starting the ambush with StartCombat, so converting that edge to
    Ally made the Emperor's guards assist the Mythic Dawn and attack the player.
    """

    BLADES_CG = 0x0001EE1E
    MYTHIC_DAWN_CG = 0x00014A20
    EMPEROR = 0x000150BC

    def _fact(self, self_fid, relations):
        """Convert one FACT and return {target_fid: (modifier, reaction)}."""
        import struct
        from tes5_import.record_types.actors import convert_FACT

        rec = {'FormID': f'{self_fid:08X}', 'EditorID': 'TestFaction',
               'RelationCount': str(len(relations))}
        for i, (fid, disp) in enumerate(relations):
            rec[f'Relation[{i}].Faction'] = f'{fid:08X}'
            rec[f'Relation[{i}].Disposition'] = str(disp)

        raw = convert_FACT(rec)
        out = {}
        body = raw[24:]
        j = 0
        while j + 6 <= len(body):
            sig = body[j:j + 4]
            size = struct.unpack_from('<H', body, j + 4)[0]
            if sig == b'XNAM' and size == 12:
                fid, mod, react = struct.unpack_from('<IiI', body, j + 6)
                out[fid & 0xFFFFFF] = (mod, react)
            j += 6 + size
        return out

    def test_self_relation_is_ally_enum_2(self):
        rel = self._fact(self.BLADES_CG, [(self.BLADES_CG, 50)])
        assert rel[self.BLADES_CG][1] == 2

    def test_cross_faction_goodwill_is_friend_not_ally(self):
        """+100 to ANOTHER faction is warmth, never an assist contract."""
        rel = self._fact(self.BLADES_CG, [(self.MYTHIC_DAWN_CG, 100)])
        assert rel[self.MYTHIC_DAWN_CG][1] == 3

    def test_chargen_guards_do_not_ally_with_the_assassins(self):
        """The regression itself: BladesCG must not be Ally to MythicDawnCG."""
        rel = self._fact(self.BLADES_CG, [
            (self.MYTHIC_DAWN_CG, 100),
            (self.EMPEROR, 50),
            (self.BLADES_CG, 50),
        ])
        assert rel[self.MYTHIC_DAWN_CG][1] != 2
        assert rel[self.BLADES_CG][1] == 2

    def test_emperor_faction_still_fights_the_assassins(self):
        """The fix must not pacify the guards: -100 stays Enemy."""
        rel = self._fact(self.EMPEROR, [(self.MYTHIC_DAWN_CG, -100)])
        assert rel[self.MYTHIC_DAWN_CG][1] == 1

    def test_mild_disposition_is_neutral(self):
        rel = self._fact(self.MYTHIC_DAWN_CG, [(0x0001DBCD, 10)])
        assert rel[0x0001DBCD][1] == 0

    def test_modifier_is_always_zero(self):
        """1035 of Skyrim.esm's 1036 XNAMs write Modifier 0; the enum carries all."""
        rel = self._fact(self.BLADES_CG, [
            (self.MYTHIC_DAWN_CG, 100), (self.EMPEROR, -100), (0x0001DBCD, 10),
        ])
        assert all(mod == 0 for mod, _ in rel.values())


class TestLeveledActorShellDNAM:
    """A placed leveled creature's shell NPC_ must not cache a zero health pool.

    TES4 `REFR -> LVLC` becomes `ACHR -> NPC_ shell -> TPLT -> LVLN`. DNAM
    offsets 36/38/40 are the cached derived Health/Magicka/Stamina; the engine
    seeds the placed reference from them before template resolution, so a zero
    Health cache spawns the actor at 0 HP and it dies the instant its cell
    loads. That was the 2026-07-30 "all the animals keel over on load" bug —
    chickens/pigs/boar/deer/river crabs are placed through these shells, while
    hand-placed sheep and pack mules (direct ACHR -> base NPC_) were fine, and
    console `placeatme` on the base bypassed the shell entirely.

    Vanilla census: of Skyrim.esm's 508 shells whose TPLT is an LVLN, ZERO
    write Health=0 (minimum 47, dominant triple 55/37/49).
    """

    def _dnam(self):
        from tes5_import.leveled_actors import _shell_dnam
        return _shell_dnam()

    def test_dnam_is_52_bytes(self):
        assert len(self._dnam()) == 52

    def test_health_is_never_zero(self):
        """The regression itself: a 0 health cache = dead on load."""
        health, _magicka, _stamina = struct.unpack_from('<HHH', self._dnam(), 36)
        assert health > 0

    def test_pools_match_the_vanilla_dominant_triple(self):
        assert struct.unpack_from('<HHH', self._dnam(), 36) == (55, 37, 49)

    def test_health_within_vanilla_observed_range(self):
        """Vanilla shells never go below 47."""
        health = struct.unpack_from('<H', self._dnam(), 36)[0]
        assert health >= 47

    def test_skills_stay_zero(self):
        """Only the pool cache is seeded; skills are still inherited."""
        assert self._dnam()[:36] == bytes(36)

    def test_built_shell_carries_the_nonzero_dnam(self):
        """End-to-end: the packed NPC_ record, not just the helper."""
        from tes5_import.leveled_actors import _build_shell
        rec = _build_shell(0x00123456, 0x00654321, 0x00013746, 'TestList_Lvl')
        idx = rec.find(b'DNAM')
        assert idx != -1
        size = struct.unpack_from('<H', rec, idx + 4)[0]
        assert size == 52
        body = rec[idx + 6:idx + 6 + size]
        assert struct.unpack_from('<HHH', body, 36) == (55, 37, 49)


class TestParentRefBecomesLinkedRef:
    """TES4 GetParentRef reads the ENABLE PARENT (XESP); Skyrim's
    GetLinkedRef() reads XLKR.

    xEdit names XESP 'Enable Parent', and the UESP modding guide states the
    idiom directly: "make the container its Parent Ref", then
    `set rCont to GetParentRef`.  Skyrim has no getter for the enable parent,
    so script_convert maps GetParentRef -> GetLinkedRef().  Nothing wrote
    XLKR, so every converted GetParentRef returned None -- all 345 scripts
    that call it, not just traps.  The Vilverin pressure plate's body ran but
    `target.Activate()` never reached the mace, which hung frozen in mid-air.

    Layout (xEdit + a real Skyrim.esm dump, 11287 vanilla uses): 8 bytes,
    {Keyword/Ref, Ref}, keyword slot NULL for a plain link.
    """

    BASE = '0005325D'   # pressure-plate base, script calls GetParentRef
    PLAIN = '0004BD3A'  # a base whose script does not

    @staticmethod
    def _refr(base_fid, xesp='0006BF3C'):
        rec = {'Signature': 'REFR', 'FormID': '0006BF3D', 'RecordFlags': '1024',
               'NAME': base_fid,
               'PosX': '0.0', 'PosY': '0.0', 'PosZ': '0.0',
               'RotX': '0.0', 'RotY': '0.0', 'RotZ': '0.0'}
        if xesp:
            rec['XESP.Reference'] = xesp
            rec['XESP.Flags'] = '0'
        return rec

    def _with_bases(self, *fids):
        """Register bases as 'script calls GetParentRef' for one test."""
        from tes5_import import object_scripts
        object_scripts._GETPARENTREF_BASES.clear()
        object_scripts._GETPARENTREF_BASES.update(fids)

    def teardown_method(self):
        from tes5_import import object_scripts
        object_scripts._GETPARENTREF_BASES.clear()

    def test_enable_parent_is_mirrored_into_xlkr(self):
        self._with_bases(self.BASE)
        out = convert_REFR(self._refr(self.BASE))
        xlkr = _find_subrecord(out, b'XLKR')
        assert xlkr is not None, 'no XLKR written'
        keyword, ref = struct.unpack('<II', xlkr)
        xesp_ref = struct.unpack('<II', _find_subrecord(out, b'XESP'))[0]
        assert keyword == 0, 'keyword slot must be NULL for a plain link'
        assert ref == xesp_ref, 'XLKR must point at the enable parent'

    def test_xlkr_is_eight_bytes(self):
        self._with_bases(self.BASE)
        out = convert_REFR(self._refr(self.BASE))
        assert len(_find_subrecord(out, b'XLKR')) == 8

    def test_enable_parent_is_still_written(self):
        """The mirror ADDS a link; it must not replace enable-parenting."""
        self._with_bases(self.BASE)
        out = convert_REFR(self._refr(self.BASE))
        assert _find_subrecord(out, b'XESP') is not None

    def test_bases_that_never_read_it_get_no_link(self):
        """XESP is ordinary enable-parenting on 9157 Oblivion refs; only 2660
        belong to a base that reads it back.  Mirroring all of them would
        invent links the game never had."""
        self._with_bases(self.BASE)
        out = convert_REFR(self._refr(self.PLAIN))
        assert _find_subrecord(out, b'XLKR') is None

    def test_no_enable_parent_means_no_link(self):
        self._with_bases(self.BASE)
        out = convert_REFR(self._refr(self.BASE, xesp=None))
        assert _find_subrecord(out, b'XLKR') is None


class TestPlayerFormIDIsReferenceOnly:
    """A record's OWN id must never take the engine-fixed passthrough.

    _ENGINE_FIXED_FORMIDS pins REFERENCES to the player (NPC_ 0x07 / ACHR 0x14)
    so they resolve to Skyrim.esm's real player rather than our converted copy.
    Oblivion.esm, however, defines its own record AT 0x07 (EditorID=Player,
    FULL="Bendu Olo"). Passing that record's own id through unshifted writes it
    at 0x00000007 -- an OVERRIDE of Skyrim's Player, replacing the real
    player's race, voice, spells, AI data and factions.

    That is the measured cause of the CharacterGen Emperor losing his topic
    list: CGEmperor01-24 are gated on `GetIsID(Player) [Target]` (1,325 such
    conditions across Oblivion's INFOs), and with the player base clobbered the
    target test stopped matching, collapsing his menu to 'Rumors' alone.
    """

    PLAYER_BASE = 0x00000007
    PLAYER_REF = 0x00000014

    def setup_method(self):
        from tes5_import import text_reader
        self._saved = text_reader.get_formid_index_offset()
        text_reader.set_formid_index_offset(1)

    def teardown_method(self):
        from tes5_import import text_reader
        text_reader.set_formid_index_offset(self._saved)

    def test_own_player_id_shifts_into_our_space(self):
        """Oblivion's Player NPC_ becomes an inert 0x01000007, not an override."""
        from tes5_import.text_reader import get_formid
        out = get_formid({'FormID': '00000007'}, 'FormID')
        assert out == 0x01000007, (
            f'Oblivion Player NPC_ written at {out:08X}; at 00000007 it '
            f'overrides Skyrim.esm Player and breaks GetIsID(Player)')

    def test_reference_to_player_base_stays_pinned(self):
        from tes5_import.text_reader import get_formid
        assert get_formid({'X': '00000007'}, 'X') == self.PLAYER_BASE

    def test_reference_to_player_ref_stays_pinned(self):
        from tes5_import.text_reader import get_formid
        assert get_formid({'X': '00000014'}, 'X') == self.PLAYER_REF

    def test_own_playerref_id_also_shifts(self):
        from tes5_import.text_reader import get_formid
        assert get_formid({'FormID': '00000014'}, 'FormID') == 0x01000014

    def test_ordinary_record_unaffected_either_way(self):
        """UrielSeptim shifts identically as an own id and as a reference."""
        from tes5_import.text_reader import get_formid
        assert get_formid({'FormID': '00023F2E'}, 'FormID') == 0x01023F2E
        assert get_formid({'X': '00023F2E'}, 'X') == 0x01023F2E

    def test_remap_formid_flag_is_explicit(self):
        from tes5_import.text_reader import remap_formid
        assert remap_formid(0x07, 1) == 0x07
        assert remap_formid(0x07, 1, is_own_id=True) == 0x01000007
