"""Tests for the creature footstep sound chain (tes5_import/creature_footsteps).

Skyrim reads creature locomotion audio through
    ARMA.SNDD -> FSTS -> FSTP -> IPDS -> IPCT -> SNDR
and NOT from the actor record or from animationdata. Oblivion keeps the same
sounds in CREA CSDT slots 0-3, which were being dropped entirely — every
converted creature was silent on foot.
"""

import os
import struct
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tes5_import.creature_footsteps import (  # noqa: E402
    _FOOTSTEP_MATERIALS, build_creature_footsteps, get_creature_footstep_set,
    patch_creature_footsteps, reset_creature_footsteps)
from tes5_import.writer import pack_record, pack_string_subrecord  # noqa: E402


class FakeWriter:
    """Minimal Writer stand-in: sequential FormIDs + top-group buckets."""

    def __init__(self, first=0x01200000):
        self._next = first
        self._top_groups = {}

    def alloc_formid(self):
        self._next += 1
        return self._next

    def add_record(self, sig, blob):
        self._top_groups.setdefault(sig, []).append(blob)


def _subrecords(blob):
    pos = 24
    while pos + 6 <= len(blob):
        sig = blob[pos:pos + 4]
        size = struct.unpack_from('<H', blob, pos + 4)[0]
        yield sig, blob[pos + 6:pos + 6 + size]
        pos += 6 + size


def _slots(**kw):
    return kw


def test_builds_full_chain_per_folder():
    reset_creature_footsteps()
    w = FakeWriter()
    n = build_creature_footsteps(
        w, {'goblin': {0: 'Foot', 1: 'Foot', 4: 'Idle'}},
        lambda edid: 0x01191000 if edid == 'Foot' else 0)
    assert n == 1
    # One distinct foot sound -> one IPCT/IPDS/FSTP, and one FSTS per folder.
    assert len(w._top_groups['IPCT']) == 1
    assert len(w._top_groups['IPDS']) == 1
    assert len(w._top_groups['FSTP']) == 1
    assert len(w._top_groups['FSTS']) == 1
    assert get_creature_footstep_set('goblin')


def test_ipct_carries_the_sound_descriptor():
    reset_creature_footsteps()
    w = FakeWriter()
    build_creature_footsteps(w, {'goblin': {0: 'Foot'}},
                             lambda edid: 0x01191000)
    ipct = w._top_groups['IPCT'][0]
    snam = dict(_subrecords(ipct))[b'SNAM']
    assert struct.unpack('<I', snam)[0] == 0x01191000


def test_ipds_maps_every_material_to_the_impact():
    reset_creature_footsteps()
    w = FakeWriter()
    build_creature_footsteps(w, {'goblin': {0: 'Foot'}},
                             lambda edid: 0x01191000)
    ipct_fid = struct.unpack_from('<I', w._top_groups['IPCT'][0], 12)[0]
    pnams = [v for s, v in _subrecords(w._top_groups['IPDS'][0])
             if s == b'PNAM']
    assert len(pnams) == len(_FOOTSTEP_MATERIALS)
    assert {struct.unpack_from('<I', v, 4)[0] for v in pnams} == {ipct_fid}


def test_quadruped_gets_one_chain_per_distinct_foot_sound():
    """Front/back pairs share a sound each -> 2 chains, not 4."""
    reset_creature_footsteps()
    w = FakeWriter()
    build_creature_footsteps(
        w, {'horse': {0: 'Front', 1: 'Front', 2: 'Back', 3: 'Back'}},
        lambda edid: {'Front': 0x11, 'Back': 0x22}[edid])
    assert len(w._top_groups['IPCT']) == 2
    assert len(w._top_groups['FSTP']) == 2


def test_fsts_counts_match_the_data_arrays():
    """XCNT is walk/run/sprint/sneak/swim; DATA is the REVERSE order."""
    reset_creature_footsteps()
    w = FakeWriter()
    build_creature_footsteps(w, {'goblin': {0: 'Foot'}},
                             lambda edid: 0x01191000)
    subs = dict(_subrecords(w._top_groups['FSTS'][0]))
    walk, run, sprint, sneak, swim = struct.unpack('<IIIII', subs[b'XCNT'])
    assert (walk, run, sprint, sneak) == (1, 1, 1, 1)
    assert swim == 0
    assert len(subs[b'DATA']) == 4 * (walk + run + sprint + sneak + swim)


def test_folder_without_foot_sounds_is_skipped():
    reset_creature_footsteps()
    w = FakeWriter()
    n = build_creature_footsteps(w, {'wisp': {4: 'Idle', 6: 'Attack'}},
                                 lambda edid: 0x01191000)
    assert n == 0
    assert 'FSTS' not in w._top_groups


def test_unresolvable_sound_is_skipped():
    reset_creature_footsteps()
    w = FakeWriter()
    n = build_creature_footsteps(w, {'goblin': {0: 'Missing'}},
                                 lambda edid: 0)
    assert n == 0


def test_patch_inserts_sndd_and_fixes_data_size():
    reset_creature_footsteps()
    w = FakeWriter()
    arma_fid = 0x0118EFDE
    w._top_groups['ARMA'] = [
        pack_record('ARMA', arma_fid, 0,
                    pack_string_subrecord('EDID', 'TES4GoblinAA'))]
    build_creature_footsteps(w, {'goblin': {0: 'Foot'}},
                             lambda edid: 0x01191000)
    assert patch_creature_footsteps(w, {arma_fid: 'goblin'}) == 1
    blob = w._top_groups['ARMA'][0]
    # The 24-byte header's dataSize must match the real payload length, or
    # every following record in the group is misparsed.
    assert struct.unpack_from('<I', blob, 4)[0] == len(blob) - 24
    sndd = dict(_subrecords(blob))[b'SNDD']
    assert struct.unpack('<I', sndd)[0] == get_creature_footstep_set('goblin')


def test_patch_is_idempotent():
    reset_creature_footsteps()
    w = FakeWriter()
    arma_fid = 0x0118EFDE
    w._top_groups['ARMA'] = [
        pack_record('ARMA', arma_fid, 0,
                    pack_string_subrecord('EDID', 'TES4GoblinAA'))]
    build_creature_footsteps(w, {'goblin': {0: 'Foot'}},
                             lambda edid: 0x01191000)
    assert patch_creature_footsteps(w, {arma_fid: 'goblin'}) == 1
    assert patch_creature_footsteps(w, {arma_fid: 'goblin'}) == 0


def test_unknown_arma_is_left_alone():
    reset_creature_footsteps()
    w = FakeWriter()
    w._top_groups['ARMA'] = [
        pack_record('ARMA', 0xDEAD, 0,
                    pack_string_subrecord('EDID', 'SomeArmorAA'))]
    build_creature_footsteps(w, {'goblin': {0: 'Foot'}},
                             lambda edid: 0x01191000)
    before = w._top_groups['ARMA'][0]
    assert patch_creature_footsteps(w, {0x0118EFDE: 'goblin'}) == 0
    assert w._top_groups['ARMA'][0] == before
