"""Resolve Oblivion.exe RTTI type names -> vtables (MSVC x86-32, no /GR- stripping).

MSVC x86 RTTI chain:
    TypeDescriptor  = { void* vftable; void* spare; char name[]; }
    A COL (CompleteObjectLocator) points at the TypeDescriptor.
    The vtable's slot -1 points at the COL.

So: find the TypeDescriptor VA, find COLs referencing it, find the pointer to
each COL, and the vtable starts 4 bytes after that pointer.
"""
import struct
import sys

EXE = r"D:\Other Games\Nehrim At Fate's Edge\Oblivion.exe"


class PE32:
    def __init__(self, path):
        self.d = open(path, 'rb').read()
        d = self.d
        pe = struct.unpack_from('<I', d, 0x3c)[0]
        assert d[pe:pe + 4] == b'PE\0\0'
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

    def va2off(self, va):
        rva = va - self.base
        for name, vaddr, vsz, ra, rsz in self.sections:
            if vaddr <= rva < vaddr + max(vsz, rsz):
                return ra + (rva - vaddr)
        return None

    def off2va(self, off):
        for name, vaddr, vsz, ra, rsz in self.sections:
            if ra <= off < ra + rsz:
                return self.base + vaddr + (off - ra)
        return None

    def find_all(self, needle):
        out, i = [], 0
        while True:
            i = self.d.find(needle, i)
            if i < 0:
                break
            out.append(i)
            i += 1
        return out


def main():
    pe = PE32(EXE)
    names = sys.argv[1:] or ['.?AVSky@@', '.?AVAtmosphere@@', '.?AVClouds@@',
                             '.?AVSun@@', '.?AVStars@@', '.?AVMoon@@',
                             '.?AVPrecipitation@@', '.?AVSkyObject@@',
                             '.?AVHDRShader@@']
    print(f'imagebase 0x{pe.base:x}')
    for nm in names:
        hits = pe.find_all(nm.encode())
        if not hits:
            print(f'{nm}: not found')
            continue
        for h in hits:
            # TypeDescriptor starts 8 bytes before the name
            td_off = h - 8
            td_va = pe.off2va(td_off)
            if td_va is None:
                continue
            print(f'\n{nm}  TypeDescriptor VA 0x{td_va:08x}')
            # COLs referencing this TypeDescriptor
            for col_ref in pe.find_all(struct.pack('<I', td_va)):
                col_va = pe.off2va(col_ref)
                if col_va is None:
                    continue
                # In a COL the TypeDescriptor pointer sits at +0x0C
                col_start = col_va - 0x0C
                for vt_ref in pe.find_all(struct.pack('<I', col_start)):
                    vt_va = pe.off2va(vt_ref)
                    if vt_va is None:
                        continue
                    vtable = vt_va + 4
                    off = pe.va2off(vtable)
                    slots = []
                    if off:
                        for k in range(10):
                            v = struct.unpack_from('<I', pe.d, off + k * 4)[0]
                            if pe.va2off(v) is None:
                                break
                            slots.append(v)
                    if slots:
                        print(f'   COL 0x{col_start:08x}  VTABLE 0x{vtable:08x}'
                              f'  ({len(slots)} slots)')
                        for k, v in enumerate(slots):
                            print(f'      [{k:2d}] 0x{v:08x}')


if __name__ == '__main__':
    main()
