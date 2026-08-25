"""Find every instruction in Oblivion.exe that references an ABSOLUTE address.

The HDR globals live at 0xB432xx (found via the double-buffered setter at
0x50b100).  x86-32 encodes absolute memory operands directly, so a literal
4-byte scan of .text plus a disassembly window around each hit gives every
reader/writer.
"""
import struct
import sys
from collections import defaultdict

EXE = r"D:\Other Games\Nehrim At Fate's Edge\Oblivion.exe"

try:
    import capstone
except ImportError:
    sys.exit('pip install capstone')


class PE32:
    def __init__(self, path):
        self.d = open(path, 'rb').read()
        d = self.d
        pe = struct.unpack_from('<I', d, 0x3c)[0]
        nsec = struct.unpack_from('<H', d, pe + 6)[0]
        optsz = struct.unpack_from('<H', d, pe + 20)[0]
        self.base = struct.unpack_from('<I', d, pe + 24 + 28)[0]
        self.sections = []
        so = pe + 24 + optsz
        for i in range(nsec):
            o = so + i * 40
            name = d[o:o + 8].rstrip(b'\x00').decode('ascii', 'replace')
            vsz, va, rsz, ra = struct.unpack_from('<IIII', d, o + 8)
            self.sections.append((name, va, vsz, ra, rsz))

    def text(self):
        for name, va, vsz, ra, rsz in self.sections:
            if name == '.text':
                return self.base + va, ra, rsz, self.d[ra:ra + rsz]
        raise SystemExit('no .text')


def main():
    targets = [int(a, 0) for a in sys.argv[1:]]
    if not targets:
        targets = [0xB43200, 0xB43204, 0xB43208, 0xB4320C,
                   0xB43210, 0xB43214, 0xB43218, 0xB4321C,
                   0xB43074, 0xB42EA8, 0xB42F44]
    pe = PE32(EXE)
    tbase, tra, trsz, code = pe.text()
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)

    for tgt in targets:
        pat = struct.pack('<I', tgt)
        hits = []
        i = 0
        while True:
            i = code.find(pat, i)
            if i < 0:
                break
            hits.append(i)
            i += 1
        print(f'\n=== 0x{tgt:08x}: {len(hits)} literal refs in .text ===')
        shown = 0
        for h in hits:
            start = max(0, h - 12)
            for ins in md.disasm(code[start:h + 10], tbase + start):
                if f'0x{tgt:x}' in ins.op_str:
                    print(f'   0x{ins.address:08x}  {ins.mnemonic:8s} '
                          f'{ins.op_str}')
                    shown += 1
                    break
            if shown > 40:
                print('   ...')
                break


if __name__ == '__main__':
    main()
