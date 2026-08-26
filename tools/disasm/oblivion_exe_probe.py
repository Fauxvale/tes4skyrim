"""Reusable read-only probe for an unpacked 32-bit Oblivion.exe.

Motivation matches tools/disasm/oblivion_engine_extract.py: the only descriptions of
Oblivion engine BEHAVIOUR (as opposed to record layout) available to this
project are xEdit defs and vanilla data. Oblivion.exe ships unpacked, so the
real logic is disassemblable. This tool is the generic lever for that — string
search, 4-byte little-endian pointer xref search, and capstone disassembly
around an address — so behaviour questions (topic visibility, CTDA run-on, OR
groups) are answered from the engine, not guessed.

Read-only interoperability analysis: nothing is patched or redistributed.

Usage:
    python tools/disasm/oblivion_exe_probe.py --exe <path> --str AddTopic
    python tools/disasm/oblivion_exe_probe.py --exe <path> --xref 0xa4f6e0
    python tools/disasm/oblivion_exe_probe.py --exe <path> --disasm 0x5c1234 --count 60
"""
import argparse
import struct

import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_32, CS_MODE_64


class Exe:
    def __init__(self, path):
        self.pe = pefile.PE(path)
        self.base = self.pe.OPTIONAL_HEADER.ImageBase
        self.img = self.pe.get_memory_mapped_image()   # indexed by RVA
        self.is64 = self.pe.FILE_HEADER.Machine == 0x8664
        self.ptr_size = 8 if self.is64 else 4
        self.md = Cs(CS_ARCH_X86, CS_MODE_64 if self.is64 else CS_MODE_32)
        self.md.detail = False

    def va_to_rva(self, va):
        return va - self.base

    def read(self, va, n):
        rva = self.va_to_rva(va)
        return self.img[rva:rva + n]

    def find_str(self, s):
        b = s.encode('latin1') if isinstance(s, str) else s
        out, i = [], 0
        while True:
            i = self.img.find(b, i)
            if i < 0:
                break
            out.append(self.base + i)
            i += 1
        return out

    def find_ptr(self, va):
        """Every location holding the little-endian pointer value `va`."""
        needle = (struct.pack('<Q', va) if self.is64
                  else struct.pack('<I', va))
        out, i = [], 0
        while True:
            i = self.img.find(needle, i)
            if i < 0:
                break
            out.append(self.base + i)
            i += self.ptr_size
        return out

    def find_lea_rip(self, target_va):
        """x64: LEA reg,[rip+disp] whose effective address is target_va.
        Scans .text for 48 8D xx disp32 forms (REX.W lea). Approximate."""
        out = []
        text = next(s for s in self.pe.sections
                    if s.Name.rstrip(b'\x00') == b'.text')
        rva0 = text.VirtualAddress
        size = text.Misc_VirtualSize
        img = self.img
        base = self.base
        for off in range(size - 7):
            # REX.W (48-4F) 8D /r with ModRM mod=00 rm=101 (rip-relative)
            if 0x48 <= img[rva0 + off] <= 0x4F and img[rva0 + off + 1] == 0x8D:
                modrm = img[rva0 + off + 2]
                if (modrm & 0xC7) == 0x05:  # mod=00, rm=101 => rip+disp32
                    disp = struct.unpack_from('<i', img, rva0 + off + 3)[0]
                    insn_end = base + rva0 + off + 7
                    if insn_end + disp == target_va:
                        out.append(base + rva0 + off)
        return out

    def find_call_rel(self, target_va):
        """E8 rel32 CALLs whose target is target_va (approximate; scans .text)."""
        out = []
        text = None
        for s in self.pe.sections:
            if s.Name.rstrip(b'\x00') == b'.text':
                text = s
                break
        start = self.base + text.VirtualAddress
        size = text.Misc_VirtualSize
        img = self.img
        rva0 = text.VirtualAddress
        for off in range(size - 5):
            if img[rva0 + off] == 0xE8:
                rel = struct.unpack_from('<i', img, rva0 + off + 1)[0]
                src = start + off
                if src + 5 + rel == target_va:
                    out.append(src)
        return out

    def disasm(self, va, count=40):
        code = self.read(va, count * 8)
        for ins in self.md.disasm(code, va):
            yield ins
            count -= 1
            if count <= 0:
                break


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exe', required=True)
    ap.add_argument('--str', dest='s')
    ap.add_argument('--xref', help='hex VA; find 4-byte LE pointers to it')
    ap.add_argument('--callers', help='hex VA; find E8 CALLs targeting it')
    ap.add_argument('--lea', help='hex VA; x64 find LEA reg,[rip+disp] to it')
    ap.add_argument('--disasm', help='hex VA to disassemble from')
    ap.add_argument('--count', type=int, default=40)
    a = ap.parse_args()
    e = Exe(a.exe)
    if a.s:
        for h in e.find_str(a.s):
            print(f'{h:#x}')
    if a.xref:
        for h in e.find_ptr(int(a.xref, 16)):
            print(f'{h:#x}')
    if a.callers:
        for h in e.find_call_rel(int(a.callers, 16)):
            print(f'{h:#x}')
    if a.lea:
        for h in e.find_lea_rip(int(a.lea, 16)):
            print(f'{h:#x}')
    if a.disasm:
        for ins in e.disasm(int(a.disasm, 16), a.count):
            print(f'{ins.address:#x}: {ins.mnemonic}\t{ins.op_str}')


if __name__ == '__main__':
    main()
