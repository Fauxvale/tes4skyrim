"""Extract shaders from Oblivion .sdp shader packages and disassemble the
D3D9 (SM 2.0/3.0) bytecode.

Why: the sky colour and the HDR tone map are done ON THE GPU.  The exe only
stages constants; the arithmetic is in the compiled shaders shipped in
Data/Shaders/shaderpackageNNN.sdp.  Reading that bytecode is the only way to
know exactly what Oblivion computes.

D3D9 shader bytecode starts with a version token:
    0xFFFF____ = pixel shader,  0xFFFE____ = vertex shader
and ends with the END token 0x0000FFFF.  Scanning for those bounds recovers
each shader regardless of the container format.
"""
import glob
import os
import struct
import sys

# ---- D3D9 shader model opcode table (d3d9types.h D3DSIO_*) ----
OPC = {
    0x00: 'nop', 0x01: 'mov', 0x02: 'add', 0x03: 'sub', 0x04: 'mad',
    0x05: 'mul', 0x06: 'rcp', 0x07: 'rsq', 0x08: 'dp3', 0x09: 'dp4',
    0x0A: 'min', 0x0B: 'max', 0x0C: 'slt', 0x0D: 'sge', 0x0E: 'exp',
    0x0F: 'log', 0x10: 'lit', 0x11: 'dst', 0x12: 'lrp', 0x13: 'frc',
    0x14: 'm4x4', 0x15: 'm4x3', 0x16: 'm3x4', 0x17: 'm3x3', 0x18: 'm3x2',
    0x19: 'call', 0x1A: 'callnz', 0x1B: 'loop', 0x1C: 'ret', 0x1D: 'endloop',
    0x1E: 'label', 0x1F: 'dcl', 0x20: 'pow', 0x21: 'crs', 0x22: 'sgn',
    0x23: 'abs', 0x24: 'nrm', 0x25: 'sincos', 0x26: 'rep', 0x27: 'endrep',
    0x28: 'if', 0x29: 'ifc', 0x2A: 'else', 0x2B: 'endif', 0x2C: 'break',
    0x2D: 'breakc', 0x2E: 'mova', 0x2F: 'defb', 0x30: 'defi',
    0x40: 'texcoord', 0x41: 'texkill', 0x42: 'tex', 0x43: 'texbem',
    0x44: 'texbeml', 0x45: 'texreg2ar', 0x46: 'texreg2gb', 0x47: 'texm3x2pad',
    0x48: 'texm3x2tex', 0x49: 'texm3x3pad', 0x4A: 'texm3x3tex',
    0x4C: 'texm3x3spec', 0x4D: 'texm3x3vspec', 0x4E: 'expp', 0x4F: 'logp',
    0x50: 'cnd', 0x51: 'def', 0x52: 'texreg2rgb', 0x53: 'texdp3tex',
    0x54: 'texm3x2depth', 0x55: 'texdp3', 0x56: 'texm3x3', 0x57: 'texdepth',
    0x58: 'cmp', 0x59: 'bem', 0x5A: 'dp2add', 0x5B: 'dsx', 0x5C: 'dsy',
    0x5D: 'texldd', 0x5E: 'setp', 0x5F: 'texldl', 0x60: 'breakp',
    0xFFFF: 'end',
}
REGT = {0: 'r', 1: 'v', 2: 'c', 3: 't', 4: 'oPos', 5: 'oFog', 6: 'oPts',
        7: 'oD', 8: 'oT', 9: 'const2', 10: 'input', 11: 'i', 12: 'loop',
        13: 'const3', 14: 'const4', 15: 'b', 16: 's', 17: 'null'}
SWZ = 'xyzw'


def regstr(tok, is_dst):
    num = tok & 0x7FF
    rt = ((tok >> 28) & 0x7) | ((tok >> 8) & 0x18)
    name = REGT.get(rt, f'?{rt}')
    s = f'{name}{num}'
    if is_dst:
        mask = (tok >> 16) & 0xF
        if mask not in (0xF, 0):
            s += '.' + ''.join(SWZ[i] for i in range(4) if mask & (1 << i))
    else:
        sw = (tok >> 16) & 0xFF
        comps = [(sw >> (2 * i)) & 3 for i in range(4)]
        if comps != [0, 1, 2, 3]:
            if comps[0] == comps[1] == comps[2] == comps[3]:
                s += '.' + SWZ[comps[0]]
            else:
                s += '.' + ''.join(SWZ[c] for c in comps)
        mod = (tok >> 24) & 0xF
        if mod == 1:
            s = '-' + s
        elif mod == 2:
            s = f'abs({s})'
        elif mod == 3:
            s = f'-abs({s})'
    return s


def disasm(data):
    """data: bytes starting at the version token."""
    out = []
    n = len(data) // 4
    toks = struct.unpack(f'<{n}I', data[:n * 4])
    ver = toks[0]
    kind = 'ps' if (ver >> 16) == 0xFFFF else 'vs'
    out.append(f'{kind}_{(ver >> 8) & 0xFF}_{ver & 0xFF}')
    i = 1
    while i < len(toks):
        t = toks[i]
        if t == 0x0000FFFF:
            out.append('end')
            i += 1
            break
        op = t & 0xFFFF
        name = OPC.get(op, f'op{op:02x}')
        size = (t >> 24) & 0xF
        i += 1
        if name == 'def':
            if i + 5 <= len(toks):
                dst = regstr(toks[i], True)
                vals = struct.unpack('<4f', struct.pack('<4I', *toks[i+1:i+5]))
                out.append(f'  def {dst}, {vals[0]:g}, {vals[1]:g}, '
                           f'{vals[2]:g}, {vals[3]:g}')
                i += 5
            continue
        if name == 'dcl':
            if i + 2 <= len(toks):
                out.append(f'  dcl {regstr(toks[i+1], True)}')
                i += 2
            continue
        args = []
        cnt = size if size else 0
        for k in range(cnt):
            if i + k < len(toks):
                args.append(regstr(toks[i + k], k == 0))
        i += cnt
        out.append(f'  {name} ' + ', '.join(args))
        if len(out) > 400:
            out.append('  ...truncated')
            break
    return kind, out


def find_shaders(buf):
    """Yield (offset, kind, length) for each D3D9 shader blob."""
    i = 0
    while i + 4 <= len(buf):
        tok = struct.unpack_from('<I', buf, i)[0]
        hi = tok >> 16
        if hi in (0xFFFF, 0xFFFE) and (tok & 0xFF00) in (0x0100, 0x0200,
                                                          0x0300, 0x0000):
            # scan for END
            j = i + 4
            while j + 4 <= len(buf):
                if struct.unpack_from('<I', buf, j)[0] == 0x0000FFFF:
                    yield i, ('ps' if hi == 0xFFFF else 'vs'), j + 4 - i
                    i = j
                    break
                j += 4
            else:
                break
        i += 4


def main():
    pat = sys.argv[1] if len(sys.argv) > 1 else None
    base = r"D:\Other Games\Nehrim At Fate's Edge\Data\Shaders"
    files = sorted(glob.glob(os.path.join(base, 'shaderpackage*.sdp')))
    want = sys.argv[2] if len(sys.argv) > 2 else None
    for f in files:
        if pat and pat not in os.path.basename(f):
            continue
        buf = open(f, 'rb').read()
        shaders = list(find_shaders(buf))
        # names are stored as ASCII near each shader in the sdp directory
        print(f'\n===== {os.path.basename(f)}  {len(buf)} bytes  '
              f'{len(shaders)} shaders =====')
        for off, kind, ln in shaders[:200]:
            # look back for a printable name
            lo = max(0, off - 0x100)
            seg = buf[lo:off]
            name = ''
            cur = b''
            for ch in seg:
                if 32 <= ch < 127:
                    cur += bytes([ch])
                else:
                    if len(cur) > len(name):
                        name = cur.decode('ascii', 'replace')
                    cur = b''
            if len(cur) > len(name):
                name = cur.decode('ascii', 'replace')
            print(f'  0x{off:06x}  {kind}  {ln:5d} bytes   {name[:60]}')
    print('\n(use: sdp_extract.py <pkgsubstr> dump:<hexoffset> to disassemble)')


if __name__ == '__main__':
    main()
