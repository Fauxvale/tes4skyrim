#!/usr/bin/env python3
"""Dump a LIVE actor's animation-event handler bindings (event name -> handler
class), read straight out of the engine's per-actor dispatcher. READ-ONLY.

Why: the engine reacts to graph-raised animation events (weaponSwing,
HitFrame, the cast-begin event that moves ActorMagicCaster out of state 1...)
through handler objects looked up BY EVENT NAME in a per-actor hash map.
Those event names are NOT literals in the exe (only the handler CLASS names
are -- the BSTCreateFactoryManager registry), so the only place the binding
can be read is this map. Knowing it tells a generated behavior graph exactly
which event name it must raise for the engine to act.

Usage:
    python tools/live/anim_event_handlers.py 12154ce3          # an NPC ref
    python tools/live/anim_event_handlers.py 14                # the player
    python tools/live/anim_event_handlers.py 12154ce3 --grep Cast

Chain (1.6.1170 live, offsets read out of the GOG 1.6.659 disassembly):
    TESForm::LookupByID(formid)             -> Actor*        (stable id 14617)
    actor+0x38 (IAnimationGraphManagerHolder) -> [+0xC0] AIProcess
    AIProcess+0x8 MiddleHighProcess          -> [+0xF8] dispatcher (refcounted)
    dispatcher: entries* @+0x38, capacity u32 @+0x1C, sentinel @+0x28,
                parent dispatcher* @+0x40 (searched when a tag misses)
    entry (0x18): key = BSFixedString char* @0, value = handler* @8, next @0x10
    (Actor::ProcessEvent 0x645160 -> Dispatch 0x657600 in 1.6.659.)

🛑 Never install bridge `hook`s on the handlers themselves
(LeftHandSpellCastHandler::executeHandler etc.): that crashed the game on the
first firing (2026-08-26, frame 0 = the hook trampoline). This tool only reads.
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.live.game_bridge import Bridge, BridgeError  # noqa: E402
from tools.live.graph_vars import Mem, actor_ptr  # noqa: E402


def rtti_name(m: Mem, obj: int) -> str:
    try:
        vt = m.u64(obj)
        col = m.u64(vt - 8)
        self_rva = m.i32(col + 0x14) & 0xFFFFFFFF
        base = col - self_rva
        td_rva = m.i32(col + 0xC) & 0xFFFFFFFF
        return m.cstr(base + td_rva + 0x10, 72)
    except BridgeError:
        return '?'


def dump_map(m: Mem, disp: int, depth: int = 0, grep: str | None = None,
             seen=None) -> None:
    seen = seen if seen is not None else set()
    if not disp or disp in seen:
        return
    seen.add(disp)
    cap = m.i32(disp + 0x1C) & 0xFFFFFFFF
    entries = m.u64(disp + 0x38)
    sentinel = m.u64(disp + 0x28)
    parent = m.u64(disp + 0x40)
    print(f'{"  " * depth}dispatcher @{disp:#x} ({rtti_name(m, disp)}) '
          f'capacity={cap} entries={entries:#x} parent={parent:#x}')
    rows = []
    if entries and 0 < cap <= 4096:
        raw = b''.join(m.read(entries + off, min(0x1000, cap * 0x18 - off))
                       for off in range(0, cap * 0x18, 0x1000))
        for i in range(cap):
            key, val, nxt = struct.unpack_from('<QQQ', raw, i * 0x18)
            if nxt == 0 or key == 0:
                continue
            try:
                ks = m.cstr(key, 64)
            except BridgeError:
                ks = '?'
            rows.append((ks, rtti_name(m, val) if val else 'NULL'))
    for ks, cls in sorted(rows, key=lambda t: t[0].lower()):
        if grep and grep.lower() not in (ks + cls).lower():
            continue
        print(f'{"  " * depth}  {ks:40} -> {cls}')
    print(f'{"  " * depth}  ({len(rows)} bindings)')
    if parent:
        dump_map(m, parent, depth + 1, grep, seen)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('formid', help='actor reference FormID (hex)')
    ap.add_argument('--grep', help='only show bindings containing this text')
    a = ap.parse_args(argv)
    fid = int(a.formid, 16)
    with Bridge() as b:
        m = Mem(b)
        actor = actor_ptr(b, fid)
        if not actor:
            print(f'{fid:08X}: LookupByID returned null')
            return 1
        process = m.u64(actor + 0x38 + 0xC0)
        middle_high = m.u64(process + 0x8) if process else 0
        disp = m.u64(middle_high + 0xF8) if middle_high else 0
        print(f'actor {fid:08X} @{actor:#x} process={process:#x} '
              f'middleHigh={middle_high:#x}')
        if not disp:
            print('no animation-event dispatcher on this actor (unloaded?)')
            return 1
        dump_map(m, disp, 0, a.grep)
    return 0


if __name__ == '__main__':
    sys.exit(main())
