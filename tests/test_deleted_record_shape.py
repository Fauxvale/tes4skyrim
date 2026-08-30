"""Deletion stubs must match the three shapes vanilla actually writes.

Measured 2026-08-25 across Update/Dawnguard/HearthFires/Dragonborn (279
deleted records): REFR-likes keep NAME only, NAVM is emptied, and EVERY other
type keeps its full body.  A zero-size non-NAVM deletion is a shape vanilla
never writes -- see docs/notes/override_conversion.md.
"""
import struct

from tes5_import.overrides import make_deleted_record

DELETED = 0x20
COMPRESSED = 0x40000
HEADER = 24


def _record(sig, body, flags=0, fid=0x01069B12):
    return (sig + struct.pack('<II', len(body), flags)
            + struct.pack('<I', fid) + b'\x00' * 8 + body)


def _sub(sig, payload):
    return sig + struct.pack('<H', len(payload)) + payload


def _parse(rec):
    size, flags = struct.unpack_from('<II', rec, 4)
    return size, flags, rec[HEADER:HEADER + size]


def test_refr_keeps_only_name():
    body = _sub(b'NAME', b'\x01\x02\x03\x04') + _sub(b'DATA', b'\x09' * 24)
    size, flags, out = _parse(make_deleted_record(_record(b'REFR', body)))
    assert flags & DELETED
    assert size == 10
    assert out == _sub(b'NAME', b'\x01\x02\x03\x04')


def test_navm_is_emptied():
    body = _sub(b'NVNM', b'\x00' * 64)
    size, flags, out = _parse(make_deleted_record(_record(b'NAVM', body)))
    assert flags & DELETED
    assert size == 0
    assert out == b''


def test_pack_keeps_full_body():
    """The Knights.esp PACK 01069B12 regression."""
    body = (_sub(b'PKDT', b'\x00' * 12) + _sub(b'PSDT', b'\x00' * 12)
            + _sub(b'CTDA', b'\x00' * 32) + _sub(b'PLDT', b'\x00' * 12))
    size, flags, out = _parse(make_deleted_record(_record(b'PACK', body)))
    assert flags & DELETED
    assert size == len(body), "a deleted PACK must keep its body"
    assert out == body


def test_other_types_keep_full_body():
    for sig in (b'NPC_', b'STAT', b'SPEL', b'IDLE', b'SMQN', b'EXPL',
                b'INFO', b'QUST'):
        body = _sub(b'EDID', b'x\x00') + _sub(b'OBND', b'\x00' * 12)
        size, flags, out = _parse(make_deleted_record(_record(sig, body)))
        assert flags & DELETED, sig
        assert size == len(body), sig
        assert out == body, sig


def test_compressed_flag_survives():
    rec = _record(b'NPC_', b'\x01\x02\x03', flags=COMPRESSED)
    _, flags, _ = _parse(make_deleted_record(rec))
    assert flags & COMPRESSED
    assert flags & DELETED


def test_header_fields_preserved():
    rec = _record(b'PACK', _sub(b'PKDT', b'\x00' * 12), fid=0x01069B12)
    out = make_deleted_record(rec)
    assert out[:4] == b'PACK'
    assert struct.unpack_from('<I', out, 12)[0] == 0x01069B12
    assert out[16:HEADER] == rec[16:HEADER]


def test_short_record_returned_unchanged():
    stub = b'PACK' + b'\x00' * 8
    assert make_deleted_record(stub) == stub
