#!/usr/bin/env python3
"""Live dialogue readback + control for a RUNNING Skyrim SE.

This is the capability that makes a conversation bug debuggable without the
user at the keyboard: SEE what the game is offering and saying, DRIVE an NPC
into conversation, and PICK a response -- all remotely.

Why memory and not the bridge's console/Papyrus channels
-------------------------------------------------------
Measured 2026-08-15 against the live process:

* the console print hook fires ONLY for commands the bridge issues (316,696
  hits, delta 0 across 12s of the game running on its own).  `tdt`/`sdt`
  debug text paints the screen without going through `Console::Print`, so the
  always-on console ring carries NO dialogue events.
* the Papyrus sink carries only what scripts choose to emit.  Converted INFO
  fragments contain no trace statements, so a fired line logs nothing.

So neither existing channel can answer "which line just played" -- the state
only exists inside the engine.  `MenuTopicManager` holds it, and reading that
is the whole point of this module.

Finding the singleton (reproduce with `skyrim_disasm.py --live`)
---------------------------------------------------------------
    --live --find MenuTopicManager   -> TypeDescriptor .?AVMenuTopicManager@@
    --live --vtable MenuTopicManager -> vtables 0x1883fe0, 0x1883ff8
    then: the ONE 8-byte datum in the image holding vtable[0]'s VA is the
    singleton itself (rva 0x3191880), confirmed because BOTH vtable pointers
    sit at +0x00/+0x08 exactly as a multiply-inherited object requires.

🛑 The RVA is NOT hardcoded as a constant to trust blindly -- it is rediscovered
by that same vtable search on every run (`find_manager`), because a game update
moves it and a stale address reads plausible garbage, which is the worst
failure mode this tooling can have.

Verified layout (1.6.1170, dialogue OPEN):
    +0xC0  TESTopicInfo** topic array
    +0xC8  uint32         count
    entry +0x10 (high 32 bits)  topic FormID
    entry +0x28 -> topic EditorID   e.g. "GREETING"
    entry +0x58 -> quest-scoped id  e.g. "GREETING_0102466E"

Usage:
    python tools/live/dialog_live.py topics            # what is on offer right now
    python tools/live/dialog_live.py watch --seconds 300 --out log.txt
    python tools/live/dialog_live.py talk 1A032A18     # teleport + start conversation
    python tools/live/dialog_live.py say 1A032A18 CharGenMain
"""

import argparse
import ctypes
import json
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.disasm.skyrim_disasm import LiveBinary  # noqa: E402


class DialogueReader:
    """Reads MenuTopicManager out of the running process."""

    def __init__(self, pid: int = 0):
        self.b = LiveBinary(pid)
        self.manager = self.find_manager()

    # -- discovery ---------------------------------------------------------

    def find_manager(self) -> int:
        """RVA of the MenuTopicManager singleton, rediscovered every run.

        Never trust a baked RVA here: a game update relocates it, and a stale
        pointer still reads -- as garbage that looks like plausible dialogue
        state.  The vtable search is cheap and self-validating.
        """
        import re
        vts = self.b.vtables_for('MenuTopicManager')
        if not vts:
            raise SystemExit('MenuTopicManager RTTI not found -- is this SSE?')
        primary = min(vts)
        want = struct.pack('<Q', self.b.base + primary)
        hits = [m.start() for m in re.finditer(re.escape(want), self.b.data)]
        # The object holds BOTH vtables back to back; the type descriptor and
        # COL do not. That pair is what separates the instance from metadata.
        second = struct.pack('<Q', self.b.base + max(vts))
        for h in hits:
            if self.b.data[h + 8:h + 16] == second:
                return h
        if hits:
            return hits[0]
        raise SystemExit('MenuTopicManager instance not located')

    # -- raw reads ---------------------------------------------------------

    def _rd(self, va: int, n: int) -> bytes:
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t(0)
        ok = self.b._k32.ReadProcessMemory(self.b._h, ctypes.c_void_p(va),
                                           buf, n, ctypes.byref(got))
        return buf.raw[:got.value] if ok else b''

    def _u64(self, va: int) -> int:
        d = self._rd(va, 8)
        return struct.unpack('<Q', d)[0] if len(d) == 8 else 0

    def _u32(self, va: int) -> int:
        d = self._rd(va, 4)
        return struct.unpack('<I', d)[0] if len(d) == 4 else 0

    def _cstr(self, va: int, n: int = 160) -> str:
        d = self._rd(va, n)
        if not d:
            return ''
        d = d.split(b'\0')[0]
        if not d or not all(32 <= c < 127 for c in d):
            return ''
        return d.decode('ascii')

    # -- the actual state --------------------------------------------------

    def topics(self) -> list:
        """[{index, form_id, editor_id, scoped_id}] currently on offer.

        Empty when no dialogue is open -- that is a real answer, not an error.
        """
        base = self.b.base + self.manager
        arr = self._u64(base + 0xC0)
        n = self._u32(base + 0xC8)
        out = []
        if not arr or not n or n > 64:
            return out
        for i in range(n):
            e = self._u64(arr + i * 8)
            if not e:
                continue
            rec = {'index': i, 'entry': e}
            hi = self._u64(e + 0x10) >> 32
            rec['form_id'] = f'{hi:08X}'
            raw = self._rd(e, 0x100)
            for off in (0x28, 0x58, 0xB8):
                if off + 8 > len(raw):
                    continue
                p = struct.unpack('<Q', raw[off:off + 8])[0]
                if p > 0x10000:
                    t = self._cstr(p)
                    if t:
                        rec.setdefault('names', []).append(t)
            out.append(rec)
        return out

    def summary(self) -> str:
        t = self.topics()
        if not t:
            return '(no dialogue open)'
        parts = []
        for r in t:
            names = '/'.join(r.get('names', [])) or '?'
            parts.append(f"[{r['index']}] {r['form_id']} {names}")
        return ' | '.join(parts)


def _bridge():
    from tools.live.game_bridge import Bridge
    return Bridge().connect(retries=4)


def cmd_topics(args) -> int:
    d = DialogueReader(args.pid)
    print(f'MenuTopicManager @ rva 0x{d.manager:x}')
    t = d.topics()
    if args.json:
        print(json.dumps(t, indent=2))
        return 0
    if not t:
        print('(no dialogue open)')
        return 0
    for r in t:
        print(f"  [{r['index']}] {r['form_id']}  "
              f"{' / '.join(r.get('names', []))}")
    return 0


def cmd_watch(args) -> int:
    """Print every CHANGE to the offered-topic set, with timestamps.

    A poll, not a hook -- but the state it samples is authoritative, and a
    changed topic list is exactly the event ("a new line is being offered")
    that neither the console ring nor the Papyrus log reports.
    """
    d = DialogueReader(args.pid)
    fh = open(args.out, 'w', encoding='utf-8', buffering=1) if args.out else None

    def emit(text: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {text}"
        print(line, flush=True)
        if fh:
            fh.write(line + '\n')

    emit(f'watching MenuTopicManager @ rva 0x{d.manager:x}')
    last = None
    deadline = time.time() + args.seconds
    while time.time() < deadline:
        try:
            cur = d.summary()
        except Exception as exc:            # process died / region unmapped
            emit(f'(read failed: {exc})')
            time.sleep(args.interval)
            continue
        if cur != last:
            emit(cur)
            last = cur
        time.sleep(args.interval)
    if fh:
        fh.close()
    return 0


def cmd_talk(args) -> int:
    """Teleport to an actor and open conversation with them."""
    with _bridge() as b:
        if not args.no_move:
            b.console(f'player.moveto {args.ref}')
            time.sleep(1.0)
        r = b.inject(script=f'StartConversation player {args.topic}'
                     if args.topic else 'StartConversation player',
                     ref=f'0x{args.ref}', settle_ms=900)
        ok = r.get('ok')
        print('StartConversation ok=', ok)
        for s in (r.get('results') or []):
            if not s.get('ok'):
                print('  FAILED:', s.get('output', '').strip()[:160])
    time.sleep(0.6)
    return cmd_topics(args)


def cmd_say(args) -> int:
    with _bridge() as b:
        r = b.inject(script=f'Say {args.topic}', ref=f'0x{args.ref}',
                     settle_ms=900)
        print('Say ok=', r.get('ok'))
        for s in (r.get('results') or []):
            if not s.get('ok'):
                print('  FAILED:', s.get('output', '').strip()[:160])
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--pid', type=int, default=0)
    ap.add_argument('--json', action='store_true')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('topics', help='what dialogue is on offer right now')

    p = sub.add_parser('watch', help='log every change to the topic list')
    p.add_argument('--seconds', type=float, default=300.0)
    p.add_argument('--interval', type=float, default=0.3)
    p.add_argument('--out', default='')

    p = sub.add_parser('talk', help='teleport to an actor and start dialogue')
    p.add_argument('ref')
    p.add_argument('topic', nargs='?', default='')
    p.add_argument('--no-move', action='store_true')

    p = sub.add_parser('say', help='make an actor say a topic')
    p.add_argument('ref')
    p.add_argument('topic')

    args = ap.parse_args(argv)
    return {'topics': cmd_topics, 'watch': cmd_watch,
            'talk': cmd_talk, 'say': cmd_say}[args.cmd](args)


if __name__ == '__main__':
    raise SystemExit(main())
