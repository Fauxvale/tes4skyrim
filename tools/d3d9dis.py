"""Correct D3D9 shader-bytecode disassembler (SM 1.x, 2.x, 3.x).

The earlier attempt used bits 24-27 of the opcode token as an instruction
length.  That field only exists in SM 2.0+.  In SM 1.x the operand count is
implied by the opcode, and in ALL models a source token may be followed by a
relative-addressing token when bit 13 is set.  Both are handled here.

Reference: d3d9types.h (D3DSIO_*, D3DSP_*), MSDN "Shader Binary Format".
"""
import struct

# opcode -> (name, ndst, nsrc)
OPS = {
    0x00: ('nop', 0, 0), 0x01: ('mov', 1, 1), 0x02: ('add', 1, 2),
    0x03: ('sub', 1, 2), 0x04: ('mad', 1, 3), 0x05: ('mul', 1, 2),
    0x06: ('rcp', 1, 1), 0x07: ('rsq', 1, 1), 0x08: ('dp3', 1, 2),
    0x09: ('dp4', 1, 2), 0x0A: ('min', 1, 2), 0x0B: ('max', 1, 2),
    0x0C: ('slt', 1, 2), 0x0D: ('sge', 1, 2), 0x0E: ('exp', 1, 1),
    0x0F: ('log', 1, 1), 0x10: ('lit', 1, 1), 0x11: ('dst', 1, 2),
    0x12: ('lrp', 1, 3), 0x13: ('frc', 1, 1), 0x14: ('m4x4', 1, 2),
    0x15: ('m4x3', 1, 2), 0x16: ('m3x4', 1, 2), 0x17: ('m3x3', 1, 2),
    0x18: ('m3x2', 1, 2), 0x19: ('call', 0, 1), 0x1A: ('callnz', 0, 2),
    0x1B: ('loop', 0, 2), 0x1C: ('ret', 0, 0), 0x1D: ('endloop', 0, 0),
    0x1E: ('label', 0, 1), 0x1F: ('dcl', 1, 0), 0x20: ('pow', 1, 2),
    0x21: ('crs', 1, 2), 0x22: ('sgn', 1, 3), 0x23: ('abs', 1, 1),
    0x24: ('nrm', 1, 1), 0x25: ('sincos', 1, 3), 0x26: ('rep', 0, 1),
    0x27: ('endrep', 0, 0), 0x28: ('if', 0, 1), 0x29: ('ifc', 0, 2),
    0x2A: ('else', 0, 0), 0x2B: ('endif', 0, 0), 0x2C: ('break', 0, 0),
    0x2D: ('breakc', 0, 2), 0x2E: ('mova', 1, 1), 0x2F: ('defb', 1, 0),
    0x30: ('defi', 1, 0),
    0x40: ('texcoord', 1, 0), 0x41: ('texkill', 1, 0), 0x42: ('texld', 1, 2),
    0x43: ('texbem', 1, 1), 0x44: ('texbeml', 1, 1),
    0x45: ('texreg2ar', 1, 1), 0x46: ('texreg2gb', 1, 1),
    0x47: ('texm3x2pad', 1, 1), 0x48: ('texm3x2tex', 1, 1),
    0x49: ('texm3x3pad', 1, 1), 0x4A: ('texm3x3tex', 1, 1),
    0x4C: ('texm3x3spec', 1, 2), 0x4D: ('texm3x3vspec', 1, 1),
    0x4E: ('expp', 1, 1), 0x4F: ('logp', 1, 1), 0x50: ('cnd', 1, 3),
    0x51: ('def', 1, 0), 0x52: ('texreg2rgb', 1, 1),
    0x53: ('texdp3tex', 1, 1), 0x54: ('texm3x2depth', 1, 1),
    0x55: ('texdp3', 1, 1), 0x56: ('texm3x3', 1, 1), 0x57: ('texdepth', 1, 0),
    0x58: ('cmp', 1, 3), 0x59: ('bem', 1, 2), 0x5A: ('dp2add', 1, 3),
    0x5B: ('dsx', 1, 1), 0x5C: ('dsy', 1, 1), 0x5D: ('texldd', 1, 4),
    0x5E: ('setp', 1, 2), 0x5F: ('texldl', 1, 2), 0x60: ('breakp', 0, 1),
}
# vs_1_1/2_0: 4=oPos(RASTOUT) 5=oD(ATTROUT) 6=oT(TEXCRDOUT/OUTPUT)
REGT = {0: 'r', 1: 'v', 2: 'c', 3: 't', 4: 'oPos', 5: 'oD', 6: 'oT',
        7: 'oD', 8: 'oT', 9: 'c', 10: 'in', 11: 'i', 12: 'aL',
        13: 'c', 14: 'c', 15: 'b', 16: 's', 17: 'null', 18: 'label',
        19: 'p'}
SWZ = 'xyzw'
DSTMOD = {0: '', 1: '_sat', 2: '_pp', 4: '_centroid'}


def _regtype(tok):
    return ((tok >> 28) & 0x7) | ((tok >> 8) & 0x18)


def dst_str(tok, ps13=False):
    num = tok & 0x7FF
    name = REGT.get(_regtype(tok), '?')
    s = f'{name}{num}'
    mask = (tok >> 16) & 0xF
    if mask not in (0xF, 0x0):
        s += '.' + ''.join(SWZ[i] for i in range(4) if mask & (1 << i))
    sat = ''
    if (tok >> 20) & 0x1:
        sat = '_sat'
    return s, sat


def src_str(tok):
    num = tok & 0x7FF
    name = REGT.get(_regtype(tok), '?')
    s = f'{name}{num}'
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
    elif mod == 4:
        s = f'!{s}'
    return s


def disassemble(data):
    n = len(data) // 4
    t = struct.unpack(f'<{n}I', data[:n * 4])
    ver = t[0]
    kind = 'ps' if (ver >> 16) == 0xFFFF else 'vs'
    major, minor = (ver >> 8) & 0xFF, ver & 0xFF
    out = [f'{kind}_{major}_{minor}']
    sm2plus = major >= 2
    i = 1
    while i < len(t):
        tok = t[i]
        if tok == 0x0000FFFF:
            out.append('end')
            break
        op = tok & 0xFFFF
        info = OPS.get(op)
        i += 1
        if info is None:
            out.append(f'  ; unknown opcode 0x{op:04x}')
            continue
        name, ndst, nsrc = info
        if name == 'def':
            d, _ = dst_str(t[i])
            vals = struct.unpack('<4f', struct.pack('<4I', *t[i+1:i+5]))
            out.append(f'  def {d}, {vals[0]:g}, {vals[1]:g}, {vals[2]:g}, '
                       f'{vals[3]:g}')
            i += 5
            continue
        if name == 'defi':
            d, _ = dst_str(t[i])
            out.append(f'  defi {d}, ' + ', '.join(str(struct.unpack("<i", struct.pack("<I", x))[0]) for x in t[i+1:i+5]))
            i += 5
            continue
        if name == 'dcl':
            usage = t[i]
            d, _ = dst_str(t[i + 1])
            out.append(f'  dcl_{usage & 0x1F}_{(usage>>16)&0xF} {d}')
            i += 2
            continue
        parts = []
        sat = ''
        for k in range(ndst):
            d, sat = dst_str(t[i])
            parts.append(d)
            i += 1
        for k in range(nsrc):
            if i >= len(t):
                break
            stok = t[i]
            i += 1
            s = src_str(stok)
            if sm2plus and (stok & (1 << 13)):
                # relative addressing token follows
                if i < len(t):
                    s += f'[{src_str(t[i])}]'
                    i += 1
            parts.append(s)
        out.append(f'  {name}{sat} ' + ', '.join(parts))
        if len(out) > 500:
            out.append('  ...')
            break
    return out
