"""Cross-references in the RUNNING SkyrimSE.exe: who calls what.

    python tools/live_xref.py string  Say ObjectReference   # rva of each "\\0<s>\\0" + every lea xref
    python tools/live_xref.py calls   0x163d50 0x96cb00     # every `call rel32` to these rvas (+ function starts)
    python tools/live_xref.py vslot   0x40 [--context 20]  # every `call [reg+slot*8]` site, grouped by function

Why this exists (2026-08-18): the question "does ANYTHING tick a non-actor
speaker's line?" is answered by listing every call site of TESObjectREFR
vtable slot 0x40 in the image and reading each -- there were 24, and the only
non-actor drivers were the scene action.  `skyrim_disasm.py` finds classes and
disassembles; this finds CALLERS, which it cannot.  Reads the live process via
LiveBinary (ReadProcessMemory), so it works on the Steam build and reports the
RVAs of the build actually running.

`string` is how a Papyrus native is located without guessing: the registration
site does `lea r8, "<Name>"; lea rdx, "<Class>"` back to back, and the
NativeFunction constructor stores the callback right after (see the SCEN
finding in docs/dialogue_engine_contracts.md).  `calls` and `vslot` report the
enclosing function start (previous `cc cc` padding) so a hit can be handed
straight to `skyrim_disasm.py --live --disasm <start>`.
"""

import argparse
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from skyrim_disasm import LiveBinary  # noqa: E402

_LEA = re.compile(rb'[\x48\x4c]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d]')


def _func_start(data: bytes, p: int) -> int:
    q = p
    while q > 0 and not (data[q - 1] == 0xCC and data[q - 2] == 0xCC):
        q -= 1
    return q


def _lea_xrefs(data: bytes, target: int) -> list:
    out = []
    for m in _LEA.finditer(data):
        p = m.start()
        disp = struct.unpack_from('<i', data, p + 3)[0]
        if p + 7 + disp == target:
            out.append(p)
    return out


def cmd_string(b, args) -> int:
    for s in args.strings:
        pat = b'\x00' + s.encode() + b'\x00'
        hits = [m.start() + 1 for m in re.finditer(re.escape(pat), b.data)]
        print(f'"{s}": {len(hits)} string(s)')
        for h in hits:
            xs = _lea_xrefs(b.data, h)
            print(f'  rva {h:#x}: {len(xs)} lea xref(s): '
                  + ' '.join(f'{x:#x}(fn {_func_start(b.data, x):#x})' for x in xs[:40]))
    return 0


def cmd_calls(b, args) -> int:
    targets = [int(t, 0) for t in args.rvas]
    for t in targets:
        out = []
        for m in re.finditer(rb'\xe8', b.data):
            p = m.start()
            if p + 5 > len(b.data):
                break
            disp = struct.unpack_from('<i', b.data, p + 1)[0]
            if p + 5 + disp == t:
                out.append(p)
        print(f'callers of {t:#x}: {len(out)}')
        for p in out[:60]:
            print(f'  {p:#x}  (fn {_func_start(b.data, p):#x})')
    return 0


def cmd_vslot(b, args) -> int:
    off = args.slot * 8
    if off < 0x80:
        raise SystemExit('slot too small for the disp32 encoding scan (< 0x10)')
    disp = struct.pack('<i', off)
    pats = [rb'\xff[\x90-\x97]' + re.escape(disp), rb'\x41\xff[\x90-\x97]' + re.escape(disp),
            rb'\xff[\xa0-\xa7]' + re.escape(disp), rb'\x41\xff[\xa0-\xa7]' + re.escape(disp)]
    hits = []
    for pat in pats:
        hits += [m.start() for m in re.finditer(pat, b.data)]
    hits.sort()
    print(f'{len(hits)} call/jmp sites through vtable slot {args.slot:#x} (+{off:#x})')
    by_fn = {}
    for h in hits:
        by_fn.setdefault(_func_start(b.data, h), []).append(h)
    for fn, hs in sorted(by_fn.items()):
        print(f'  fn {fn:#x}: ' + ' '.join(f'{h:#x}' for h in hs))
    if args.context:
        from capstone import Cs, CS_ARCH_X86, CS_MODE_64
        md = Cs(CS_ARCH_X86, CS_MODE_64)
        for h in hits:
            fn = _func_start(b.data, h)
            lines = [f'  {ins.address:08x}  {ins.mnemonic:<8} {ins.op_str}'
                     for ins in md.disasm(b.data[fn:h + 8], fn)]
            print(f'--- {h:#x} in fn {fn:#x}')
            print('\n'.join(lines[-args.context:]))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--pid', type=int, default=0)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('string'); p.add_argument('strings', nargs='+')
    p = sub.add_parser('calls'); p.add_argument('rvas', nargs='+')
    p = sub.add_parser('vslot'); p.add_argument('slot', type=lambda s: int(s, 0))
    p.add_argument('--context', type=int, default=0,
                   help='disassemble the N instructions before each site')
    args = ap.parse_args(argv)
    b = LiveBinary(args.pid)
    print(f'LIVE pid={b.pid} imagebase={b.base:#x} image={len(b.data) / 2**20:.1f} MB')
    return {'string': cmd_string, 'calls': cmd_calls, 'vslot': cmd_vslot}[args.cmd](b, args)


if __name__ == '__main__':
    sys.exit(main())
