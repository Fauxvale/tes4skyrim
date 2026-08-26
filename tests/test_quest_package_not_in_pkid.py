"""A quest-owned package must never appear in an NPC's PKID list.

xEdit states the rule outright:

    NDAnvilListenProphetGogan8x4 [PACK:02002D7C]
    error: package is owned by quest ND00 and cannot be assigned to an npc
    record

Such a package reaches its actor through the quest's reference alias (ALPC).
Knights.esp shipped `PACK 02002D7C` in the PKID list of `NPC_ 0103AF03`
(Gogan) and the game froze at the main menu; removing it fixed the freeze
(confirmed in-game 2026-08-26).

The normal converter path (packages.npc_packages) always filtered these.  The
OVERRIDE path derived its exclusion set from the master alone, so a package
the PLUGIN newly quest-owns -- which the master never saw -- passed straight
through.  Both paths now consult packages.is_quest_package.
"""
import struct
from pathlib import Path

import pytest

from tes5_import import packages


def _walk(buf):
    hsz = struct.unpack_from('<I', buf, 4)[0]
    stack = [(24 + hsz, len(buf))]
    while stack:
        s, e = stack.pop()
        o = s
        while o + 24 <= e:
            t = buf[o:o + 4]
            sz = struct.unpack_from('<I', buf, o + 4)[0]
            if t == b'GRUP':
                if sz < 24:
                    break
                stack.append((o + 24, o + sz))
                o += sz
            else:
                yield t, o, sz
                o += 24 + sz


def _subs(buf, o, sz):
    body = buf[o + 24:o + 24 + sz]
    i = 0
    while i + 6 <= len(body):
        sg = body[i:i + 4]
        ss = struct.unpack_from('<H', body, i + 4)[0]
        yield sg, body[i + 6:i + 6 + ss]
        i += 6 + ss


def test_is_quest_package_reads_the_registered_set():
    try:
        packages.set_quest_packages({0x02002D7C})
        assert packages.is_quest_package(0x02002D7C)
        assert not packages.is_quest_package(0x0101DC54)
    finally:
        packages.set_quest_packages(())


def test_override_pkid_rebuild_drops_plugin_owned_quest_packages():
    """The override path must filter, not just the normal converter path."""
    from tes5_import.override_builder import _rebuild_packages

    quest_pkg = 0x02002D7C      # newly owned by THIS plugin's quest
    plain = 0x0101DC54          # ordinary package, must survive

    master = {'AIPackageCount': '1', 'AIPackage[0]': f'{plain:08X}'}
    plugin = {'AIPackageCount': '2',
              'AIPackage[0]': f'{plain:08X}',
              'AIPackage[1]': f'{quest_pkg:08X}'}
    old_subs = [(b'PKID', struct.pack('<I', plain))]

    try:
        packages.set_quest_packages({quest_pkg})
        out = _rebuild_packages(plugin, master, old_subs)
    finally:
        packages.set_quest_packages(())

    fids = [struct.unpack_from('<I', payload)[0] for _, payload in out]
    assert quest_pkg not in fids, \
        'a quest-owned package leaked into an overridden NPC PKID list'
    assert fids == [plain]


def test_built_plugin_has_no_quest_package_in_any_pkid():
    """Regression against the real artifact, when it has been built."""
    p = Path('output/Knights.esp/Knights.esp')
    if not p.exists():
        pytest.skip('Knights.esp not built')
    data = p.read_bytes()

    alias_packages = set()
    for t, o, sz in _walk(data):
        if t != b'QUST':
            continue
        for sg, payload in _subs(data, o, sz):
            if sg == b'ALPC' and len(payload) == 4:
                alias_packages.add(struct.unpack_from('<I', payload)[0])

    violations = []
    for t, o, sz in _walk(data):
        if t not in (b'NPC_', b'CREA'):
            continue
        fid = struct.unpack_from('<I', data, o + 12)[0]
        for sg, payload in _subs(data, o, sz):
            if sg == b'PKID' and len(payload) == 4:
                pk = struct.unpack_from('<I', payload)[0]
                if pk in alias_packages:
                    violations.append((fid, pk))

    assert not violations, (
        f'{len(violations)} NPC PKID entries name a quest-owned package, e.g. '
        + ', '.join(f'NPC {f:08X} -> PACK {p_:08X}'
                    for f, p_ in violations[:5]))
