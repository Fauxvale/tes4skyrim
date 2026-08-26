#!/usr/bin/env python3
"""Static analysis helper for Oblivion.exe (x86-32) — flow-following disassembly
plus a symbolic x87 FPU simulator.

Why this exists: Oblivion.exe statically links SpeedTreeRT 4.x with symbols
intact, so the REAL procedural-tree algorithm (branch counts, placement, the
bend integrator, the RNG) is readable.  That is the ground truth for
`asset_convert/spt_generator.py` — the billboard renders are a 2-D projection
the generator was already fitted to, so they cannot reveal a 3-D error.
See docs/speedtree_engine_decomp.md.

Two things this does that `tools/disasm/skyrim_disasm.py` cannot (that one is x64 and
SkyrimSE-specific):
  * RECURSIVE-DESCENT disassembly — follows branches, so it never desyncs.
    Linear disassembly of this binary drifts within ~40 bytes and silently
    produces garbage instructions.
  * SYMBOLIC x87 SIMULATION — the SpeedTree math is all x87 stack code
    (fld/fxch/faddp/fstp).  Hand-tracing the stack is error-prone; this prints
    the resolved expression for every store.

Read-only interoperability analysis: never patches or redistributes anything.

Usage:
    # flow-following disassembly of a function (optionally windowed)
    python tools/disasm/oblivion_disasm.py --fn 0x78feb0
    python tools/disasm/oblivion_disasm.py --fn 0x78feb0 --lo 0x78ff65 --hi 0x790050

    # symbolic FPU trace over a range (resolves x87 expressions)
    python tools/disasm/oblivion_disasm.py --fpu 0x78feb0 --hi 0x78ff1a

    # decode a jump table (chained sub/add dispatch is common here)
    python tools/disasm/oblivion_disasm.py --jt 0x7a7cbc --count 18 --first 6000

    # find rel32 call/jmp xrefs to an address
    python tools/disasm/oblivion_disasm.py --xref 0x7925b0

    # read a float/double constant at a VA
    python tools/disasm/oblivion_disasm.py --const 0xa3f420
"""
from __future__ import annotations
import argparse, re, struct, sys

DEFAULT_EXE = r"D:\Other Games\Nehrim At Fate's Edge\Oblivion.exe"

try:
    import capstone
except ImportError:
    sys.exit('capstone required:  pip install capstone')


class PE32:
    def __init__(self, path):
        self.d = open(path, 'rb').read()
        d = self.d
        pe = struct.unpack_from('<I', d, 0x3c)[0]
        if d[pe:pe + 4] != b'PE\0\0':
            raise SystemExit(f'{path}: not a PE')
        nsec = struct.unpack_from('<H', d, pe + 6)[0]
        optsz = struct.unpack_from('<H', d, pe + 20)[0]
        self.base = struct.unpack_from('<I', d, pe + 24 + 28)[0]
        self.secs = []
        off = pe + 24 + optsz
        for _ in range(nsec):
            nm = d[off:off + 8].rstrip(b'\0').decode('latin-1')
            vsz, va, rsz, ra = struct.unpack_from('<IIII', d, off + 8)
            self.secs.append((nm, va, vsz, ra, rsz))
            off += 40
        self.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        self.md.detail = True

    def v2f(self, va):
        r = va - self.base
        for _, sva, vsz, ra, rsz in self.secs:
            if sva <= r < sva + vsz:
                o = ra + (r - sva)
                return o if o < len(self.d) else None
        return None

    def f2v(self, off):
        for _, sva, vsz, ra, rsz in self.secs:
            if ra <= off < ra + rsz:
                return self.base + sva + (off - ra)
        return None

    def f32(self, va):
        o = self.v2f(va)
        return None if o is None or o + 4 > len(self.d) else struct.unpack_from('<f', self.d, o)[0]

    def f64(self, va):
        o = self.v2f(va)
        return None if o is None or o + 8 > len(self.d) else struct.unpack_from('<d', self.d, o)[0]

    def u32(self, va):
        o = self.v2f(va)
        return None if o is None or o + 4 > len(self.d) else struct.unpack_from('<I', self.d, o)[0]

    # -- recursive descent -------------------------------------------------
    def disasm_fn(self, start, maxins=6000):
        seen, work = {}, [start]
        while work:
            va = work.pop()
            while va and va not in seen and len(seen) < maxins:
                o = self.v2f(va)
                if o is None:
                    break
                ins = next(iter(self.md.disasm(self.d[o:o + 16], va)), None)
                if ins is None:
                    break
                seen[va] = ins
                m = ins.mnemonic
                if m == 'int3' or m.startswith('ret'):
                    break
                if m.startswith('j'):
                    if ins.op_str.startswith('0x'):
                        t = int(ins.op_str, 16)
                        if t not in seen:
                            work.append(t)
                    if m == 'jmp':
                        break
                va = ins.address + ins.size
        return seen

    def annotate(self, ins):
        mm = re.search(r'\[(0x[0-9a-f]+)\]', ins.op_str)
        if not mm or not ins.mnemonic.startswith('f'):
            return ''
        va = int(mm.group(1), 16)
        if 'qword' in ins.op_str:
            v = self.f64(va)
            return f'   ; f64={v:.9g}' if v is not None else ''
        v = self.f32(va)
        return f'   ; f32={v:.9g}' if v is not None else ''

    def xrefs(self, target):
        out = []
        for _, sva, vsz, ra, rsz in self.secs:
            if not (sva <= 0x1000 or True):
                continue
        text = [s for s in self.secs if s[0] == '.text']
        if not text:
            return out
        _, sva, vsz, ra, rsz = text[0]
        blob = self.d[ra:ra + rsz]
        for m in re.finditer(rb'[\xe8\xe9]', blob):
            o = ra + m.start()
            if o + 5 > len(self.d):
                continue
            rel = struct.unpack_from('<i', self.d, o + 1)[0]
            src = self.f2v(o)
            if src is None:
                continue
            if src + 5 + rel == target:
                out.append((src, 'call' if self.d[o] == 0xe8 else 'jmp'))
        return out


# -- symbolic x87 ----------------------------------------------------------
class FPU:
    def __init__(self):
        self.st = []

    def push(self, v):
        self.st.insert(0, v)

    def pop(self):
        return self.st.pop(0) if self.st else '<underflow>'

    def get(self, i):
        return self.st[i] if i < len(self.st) else f'<empty{i}>'

    def set(self, i, v):
        while len(self.st) <= i:
            self.st.append('<empty>')
        self.st[i] = v

    def __str__(self):
        return ' | '.join(self.st) if self.st else '(empty)'


_BIN = {'fadd': '+', 'fsub': '-', 'fmul': '*', 'fdiv': '/',
        'fsubr': '-r', 'fdivr': '/r'}
_BINP = {'faddp': '+', 'fsubp': '-', 'fmulp': '*', 'fdivp': '/',
         'fsubrp': '-r', 'fdivrp': '/r'}


def _sti(op):
    m = re.match(r'st\((\d)\)', op or '')
    if m:
        return int(m.group(1))
    return 0 if op == 'st' else None


def fpu_trace(pe, start, end, verbose=True):
    """Symbolically execute the x87 stack over [start, end], printing the
    resolved expression after every instruction and every memory store."""
    fn = pe.disasm_fn(start)
    f, mem = FPU(), {}
    for a in sorted(fn):
        if not (start <= a <= end):
            continue
        i = fn[a]
        m, src = i.mnemonic, i.op_str
        ops = [o.strip() for o in src.split(',')] if src else []
        def nm(s):
            mm = re.search(r'\[([^\]]+)\]', s)
            return mm.group(1) if mm else s
        if m == 'fldz':
            f.push('0.0')
        elif m == 'fld1':
            f.push('1.0')
        elif m in ('fld', 'fild'):
            f.push(f.get(_sti(src)) if src.startswith('st')
                   else mem.get(nm(src), 'M[%s]' % nm(src)))
        elif m in ('fst', 'fstp'):
            v = f.get(0)
            if src.startswith('st'):
                f.set(_sti(src), v)
            else:
                mem[nm(src)] = v
            if m == 'fstp':
                f.pop()
        elif m == 'fxch':
            j = _sti(src) if src else 1
            a0, aj = f.get(0), f.get(j)
            f.set(0, aj); f.set(j, a0)
        elif m in _BIN:
            s = _BIN[m]
            if len(ops) == 2:
                d, sr = _sti(ops[0]), _sti(ops[1])
                f.set(d, '(%s %s %s)' % (f.get(d), s, f.get(sr)))
            elif src.startswith('st'):
                f.set(0, '(%s %s %s)' % (f.get(0), s, f.get(_sti(src))))
            else:
                f.set(0, '(%s %s %s)' % (f.get(0), s, mem.get(nm(src), 'M[%s]' % nm(src))))
        elif m in _BINP:
            s = _BINP[m]
            j = _sti(ops[0]) if ops else 1
            dv, sv = f.get(j), f.get(0)
            f.set(j, '(%s %s %s)' % (sv, s[0], dv) if s in ('-r', '/r')
                     else '(%s %s %s)' % (dv, s, sv))
            f.pop()
        elif m == 'fchs':
            f.set(0, '(-%s)' % f.get(0))
        elif m == 'fabs':
            f.set(0, 'abs(%s)' % f.get(0))
        elif m == 'fsqrt':
            f.set(0, 'sqrt(%s)' % f.get(0))
        elif m in ('fcomp', 'fucomp'):
            f.pop()
        elif m in ('fcompp', 'fucompp'):
            f.pop(); f.pop()
        elif m == 'call':
            f.set(0, 'CALL(%s, %s)' % (src, f.get(0)))
        if verbose:
            print('  %08x %-8s %-30s %s' % (a, m, src, f))
    return f, mem


def esp_track(pe, start, targets=None, maxsteps=200000):
    """Walk the function from `start` following flow, tracking esp relative to
    function entry.  Returns {va: esp_delta}.  Callee-cleanup (`ret N`) is
    accounted for at call sites via the callee's own `ret imm16`.

    This exists because SpeedTree's branch builder addresses the SAME frame slot
    with different [esp+N] offsets depending on how many pushes are live, and
    naming slots per-block (rather than relative to entry) silently produces
    wrong identifications.
    """
    fn = pe.disasm_fn(start)
    # cache: callee -> bytes popped by its `ret N`
    retpop = {}

    def callee_pop(t):
        if t in retpop:
            return retpop[t]
        v = 0
        sub = pe.disasm_fn(t, maxins=400)
        for a2 in sorted(sub):
            i2 = sub[a2]
            if i2.mnemonic == 'ret' and i2.op_str:
                try:
                    v = int(i2.op_str, 16)
                except ValueError:
                    v = 0
                break
        retpop[t] = v
        return v

    delta = {start: 0}
    work = [start]
    seen = set()
    while work:
        va = work.pop()
        while va in fn and (va, delta.get(va)) not in seen:
            seen.add((va, delta.get(va)))
            i = fn[va]
            d = delta.get(va, 0)
            m, ops = i.mnemonic, i.op_str
            nd = d
            if m == 'push':
                nd = d - 4
            elif m == 'pop':
                nd = d + 4
            elif m in ('sub', 'add') and ops.startswith('esp,'):
                try:
                    n = int(ops.split(',')[1].strip(), 16)
                    nd = d - n if m == 'sub' else d + n
                except ValueError:
                    pass
            elif m == 'call' and ops.startswith('0x'):
                try:
                    nd = d + callee_pop(int(ops, 16))
                except ValueError:
                    pass
            if m.startswith('ret') or m == 'int3':
                break
            if m.startswith('j') and ops.startswith('0x'):
                t = int(ops, 16)
                if t not in delta:
                    delta[t] = nd
                    work.append(t)
                if m == 'jmp':
                    break
            nxt = i.address + i.size
            if nxt not in delta:
                delta[nxt] = nd
            va = nxt
    if targets:
        for t in targets:
            print('  %08x  esp delta %+d' % (t, delta.get(t, 0)))
    return delta


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--exe', default=DEFAULT_EXE)
    ap.add_argument('--fn', help='recursive-descent disassemble this VA')
    ap.add_argument('--fpu', help='symbolic x87 trace starting at this VA')
    ap.add_argument('--lo', help='window start VA')
    ap.add_argument('--hi', help='window end VA')
    ap.add_argument('--jt', help='decode a jump table at this VA')
    ap.add_argument('--count', type=int, default=16)
    ap.add_argument('--first', type=int, default=0, help='--jt: first section id')
    ap.add_argument('--xref', help='find rel32 call/jmp xrefs to this VA')
    ap.add_argument('--const', help='read the float/double constant at this VA')
    ap.add_argument('--esp', help='track esp deltas from this function VA')
    ap.add_argument('--at', help='comma-separated VAs to report esp delta for')
    a = ap.parse_args()
    pe = PE32(a.exe)
    H = lambda s: int(s, 16)

    if a.const:
        va = H(a.const)
        print('%08x: f32=%r  f64=%r  u32=%r' % (va, pe.f32(va), pe.f64(va), pe.u32(va)))
    if a.xref:
        t = H(a.xref)
        rs = pe.xrefs(t)
        print('xrefs to %08x: %d' % (t, len(rs)))
        for src, kind in rs:
            print('  %s %08x' % (kind, src))
    if a.jt:
        base = H(a.jt)
        for i in range(a.count):
            tv = pe.u32(base + 4 * i)
            sid = (a.first + i) if a.first else i
            print('  idx %2d  %-6s -> %08x' % (i, sid, tv))
    if a.fn:
        lo = H(a.lo) if a.lo else 0
        hi = H(a.hi) if a.hi else 0xffffffff
        fn = pe.disasm_fn(H(a.fn))
        for va in sorted(fn):
            if lo <= va <= hi:
                i = fn[va]
                print('  %08x  %-8s %-40s%s' % (va, i.mnemonic, i.op_str, pe.annotate(i)))
        print('  [%d instructions reachable]' % len(fn))
    if a.esp:
        tg = [int(x,16) for x in a.at.split(',')] if a.at else None
        dl = esp_track(pe, H(a.esp), tg)
        if not tg:
            print('tracked %d addresses' % len(dl))
    if a.fpu:
        s = H(a.fpu)
        e = H(a.hi) if a.hi else s + 0x200
        f, mem = fpu_trace(pe, s, e)
        print()
        print('FINAL FPU: %s' % f)
        print('MEM writes:')
        for k, v in mem.items():
            if not v.startswith('M['):
                print('   %-22s = %s' % (k, v))


if __name__ == '__main__':
    main()
