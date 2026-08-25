"""Dump Oblivion sky shaders, skipping the CTAB comment block properly.

D3D9 embeds the constant table in a COMMENT token: (0xFFFE | len<<16).
Skipping it by its declared length recovers clean instruction decoding, and
the CTAB itself carries the CONSTANT NAMES -- which tell us exactly which
engine value each register holds.
"""
import struct, sys
sys.path.insert(0, '.')
from sdp_extract import find_shaders
from d3d9dis import OPS, dst_str, src_str

def parse_ctab(data, off, nbytes):
    """D3DXSHADER_CONSTANTTABLE at data[off:off+nbytes]."""
    try:
        size, creator, ver, consts, cinfo, flags, target = \
            struct.unpack_from('<7I', data, off)
    except Exception:
        return []
    out = []
    for i in range(consts):
        b = off + cinfo + i * 20
        try:
            name_off, regset, regidx, regcnt, tinfo, dinfo = \
                struct.unpack_from('<I2H2I', data, b)
        except Exception:
            break
        e = data.find(b'\x00', off + name_off)
        nm = data[off + name_off:e].decode('ascii', 'replace')
        rs = {0:'b',1:'i',2:'c',3:'s'}.get(regset, '?')
        out.append((nm, f'{rs}{regidx}', regcnt))
    return out

def dump(buf, off, ln, label=''):
    data = buf[off:off+ln]
    n = len(data)//4
    t = struct.unpack(f'<{n}I', data[:n*4])
    ver = t[0]
    kind = 'ps' if (ver>>16)==0xFFFF else 'vs'
    print(f'\n======== 0x{off:06x} {kind}_{(ver>>8)&0xFF}_{ver&0xFF} '
          f'{ln}B {label} ========')
    i = 1
    sm2 = ((ver>>8)&0xFF) >= 2
    while i < len(t):
        tok = t[i]
        if tok == 0x0000FFFF:
            print('end'); break
        if (tok & 0xFFFF) == 0xFFFE:          # COMMENT
            cl = (tok >> 16) & 0x7FFF
            cstart = (i+1)*4
            if data[cstart:cstart+4] == b'CTAB':
                for nm, reg, cnt in parse_ctab(data, cstart+4, cl*4):
                    print(f'   ; const {reg:6s} x{cnt}  {nm}')
            i += 1 + cl
            continue
        op = tok & 0xFFFF
        info = OPS.get(op)
        i += 1
        if info is None:
            print(f'  ; unk 0x{op:04x}'); continue
        name, ndst, nsrc = info
        if name == 'def':
            d,_ = dst_str(t[i])
            v = struct.unpack('<4f', struct.pack('<4I', *t[i+1:i+5]))
            print(f'  def {d}, {v[0]:g}, {v[1]:g}, {v[2]:g}, {v[3]:g}')
            i += 5; continue
        if name == 'dcl':
            u = t[i]; d,_ = dst_str(t[i+1])
            print(f'  dcl {d}'); i += 2; continue
        parts=[]; sat=''
        for k in range(ndst):
            d,sat = dst_str(t[i]); parts.append(d); i+=1
        for k in range(nsrc):
            if i>=len(t): break
            s = src_str(t[i]); i+=1
            parts.append(s)
        print(f'  {name}{sat} ' + ', '.join(parts))

if __name__ == '__main__':
    f = r"D:\Other Games\Nehrim At Fate's Edge\Data\Shaders\shaderpackage001.sdp"
    buf = open(f,'rb').read()
    sh = {o:(k,l) for o,k,l in find_shaders(buf)}
    offs = [int(a,0) for a in sys.argv[1:]] or [0x0288c0]
    for o in offs:
        if o in sh:
            dump(buf, o, sh[o][1])
