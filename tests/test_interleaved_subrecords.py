"""Interleaved subrecord families must keep their pairing through an override.

A REGN's Region Areas are a repeating struct whose members each carry their own
signature, so the run on disk is RPLI RPLD RPLI RPLD ... The generic
substitution path replaced each signature "as a unit at the position of the
first occurrence" -- correct for a genuinely repeating single-signature run
(CNAM, NAM1), fatal here: AnvilCoastline came out

    RPLI RPLD RPLD RPLD RPLD RPLI RPLI RPLI

against the master's correct RPLI RPLD x4. xEdit rejects it with "record REGN
contains unexpected (or out of order) subrecord RPLD" and the engine reads the
areas wrong.
"""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tes5_import.override_builder import _apply_generic


def _sigs(pairs):
    return [s.decode() for s, _p in pairs]


class TestInterleavedFamily:
    def test_rpli_rpld_pairing_survives(self):
        out = [(b'EDID', b'x\x00')]
        for i in range(4):
            out.append((b'RPLI', bytes([i])))
            out.append((b'RPLD', bytes([i, i])))
        subs = {
            b'RPLI': [bytes([9 + i]) for i in range(4)],
            b'RPLD': [bytes([9 + i, 9 + i]) for i in range(4)],
        }
        got = _apply_generic(out, subs, set())
        assert _sigs(got) == ['EDID'] + ['RPLI', 'RPLD'] * 4

    def test_substituted_values_land_in_their_own_slots(self):
        out = [(b'RPLI', b'\x00'), (b'RPLD', b'\x00'),
               (b'RPLI', b'\x01'), (b'RPLD', b'\x01')]
        subs = {b'RPLI': [b'\xaa', b'\xbb'], b'RPLD': [b'\xcc', b'\xdd']}
        got = _apply_generic(out, subs, set())
        assert got == [(b'RPLI', b'\xaa'), (b'RPLD', b'\xcc'),
                       (b'RPLI', b'\xbb'), (b'RPLD', b'\xdd')]

    def test_plugin_with_more_areas_appends_the_remainder(self):
        """No authored area may be silently dropped."""
        out = [(b'RPLI', b'\x00'), (b'RPLD', b'\x00')]
        subs = {b'RPLI': [b'\xaa', b'\xbb'], b'RPLD': [b'\xcc', b'\xdd']}
        got = _apply_generic(out, subs, set())
        assert got.count((b'RPLI', b'\xbb')) == 1
        assert got.count((b'RPLD', b'\xdd')) == 1
        assert len([s for s, _ in got if s == b'RPLI']) == 2
        assert len([s for s, _ in got if s == b'RPLD']) == 2

    def test_plugin_with_fewer_areas_truncates(self):
        out = [(b'RPLI', b'\x00'), (b'RPLD', b'\x00'),
               (b'RPLI', b'\x01'), (b'RPLD', b'\x01')]
        subs = {b'RPLI': [b'\xaa'], b'RPLD': [b'\xcc']}
        got = _apply_generic(out, subs, set())
        assert got == [(b'RPLI', b'\xaa'), (b'RPLD', b'\xcc')]


class TestNonInterleavedUnaffected:
    """A normal repeating run still collapses to the first occurrence."""

    def test_repeated_single_signature_run_is_replaced_as_a_unit(self):
        out = [(b'EDID', b'x\x00'), (b'CNAM', b'a'), (b'CNAM', b'b'),
               (b'DATA', b'z')]
        subs = {b'CNAM': [b'1', b'2', b'3']}
        got = _apply_generic(out, subs, set())
        assert got == [(b'EDID', b'x\x00'), (b'CNAM', b'1'), (b'CNAM', b'2'),
                       (b'CNAM', b'3'), (b'DATA', b'z')]

    def test_claimed_signatures_are_left_alone(self):
        out = [(b'RPLI', b'\x00'), (b'RPLD', b'\x00')]
        subs = {b'RPLI': [b'\xaa'], b'RPLD': [b'\xcc']}
        got = _apply_generic(out, subs, {b'RPLI', b'RPLD'})
        assert got == out

    def test_signature_absent_from_master_is_appended(self):
        out = [(b'EDID', b'x\x00')]
        subs = {b'FULL': [b'hi\x00']}
        got = _apply_generic(out, subs, set())
        assert got == [(b'EDID', b'x\x00'), (b'FULL', b'hi\x00')]
