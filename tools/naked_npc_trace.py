"""Which actors wear gear whose mesh does not exist?

Chains ARMA (unresolvable MOD2) -> ARMO -> OTFT/inventory -> NPC_, so a
"this NPC has no clothes" report can be answered with names instead of guesses.

Usage: python tools/naked_npc_trace.py [plugin] [--all]
"""
import os
import struct
import sys
from collections import defaultdict

sys.path.insert(0, 'tools')
sys.path.insert(0, '.')
from tes5_esm_reader import read_tes5_file      # noqa: E402
from voice_audit import _get, _zstring          # noqa: E402

plugin = 'Nehrim.esm'
show_all = '--all' in sys.argv
for a in sys.argv[1:]:
    if not a.startswith('--'):
        plugin = a

MESHES = f'output/{plugin}/meshes/'
_h, recs, _ = read_tes5_file(f'output/{plugin}/{plugin}')

by_type = defaultdict(list)
for r in recs:
    by_type[r.type].append(r)


def edid(r):
    e = _get(r, 'EDID')
    return _zstring(e.data) if e else ''


def full(r):
    f = _get(r, 'FULL')
    return _zstring(f.data) if f else ''


# 1. ARMA whose worn mesh is not on disk
bad_arma = {}
for r in by_type['ARMA']:
    for s in r.subrecords:
        if s.type == 'MOD2':
            rel = _zstring(s.data).replace('\\', '/')
            if not os.path.exists(MESHES + rel):
                bad_arma[r.form_id] = rel
            break
print(f'ARMA with an unresolvable worn mesh: {len(bad_arma)}')

# 2. ARMO referencing one of them
bad_armo = {}
for r in by_type['ARMO']:
    for s in r.subrecords:
        if s.type == 'MODL' and len(s.data) == 4:
            fid = struct.unpack('<I', s.data)[0]
            if fid in bad_arma:
                bad_armo[r.form_id] = (edid(r), full(r), bad_arma[fid])
                break
print(f'ARMO built on such an ARMA:          {len(bad_armo)}')

# 3. OTFT containing such an ARMO
bad_otft = {}
for r in by_type['OTFT']:
    for s in r.subrecords:
        if s.type != 'INAM':
            continue
        for i in range(0, len(s.data) - 3, 4):
            fid = struct.unpack_from('<I', s.data, i)[0]
            if fid in bad_armo:
                bad_otft.setdefault(r.form_id, (edid(r), set()))[1].add(fid)
print(f'OTFT outfits containing one:         {len(bad_otft)}')

# 4. actors wearing it, by outfit (DOFT) or directly in inventory (CNTO)
hits = []
for r in by_type['NPC_']:
    worn = set()
    for s in r.subrecords:
        if s.type == 'DOFT' and len(s.data) == 4:
            o = struct.unpack('<I', s.data)[0]
            if o in bad_otft:
                worn |= bad_otft[o][1]
        elif s.type == 'CNTO' and len(s.data) >= 4:
            fid = struct.unpack_from('<I', s.data, 0)[0]
            if fid in bad_armo:
                worn.add(fid)
    if worn:
        hits.append((edid(r), full(r), worn))

print(f'\nACTORS affected: {len(hits)}\n')
print(f'{"NPC EditorID":34} {"Name":22} missing gear')
for e, f, worn in (hits if show_all else hits[:25]):
    items = ', '.join(sorted(bad_armo[w][0] or f'{w:08X}' for w in worn))
    print(f'{e:34} {f[:22]:22} {items}')
if not show_all and len(hits) > 25:
    print(f'... ({len(hits) - 25} more, pass --all)')

print('\nUnresolvable gear, by item:')
for fid, (e, f, mesh) in sorted(bad_armo.items(), key=lambda t: t[1][0]):
    n = sum(1 for _e, _f, w in hits if fid in w)
    print(f'  {e:34} {f[:20]:20} x{n:<4} {mesh}')

# 5. The other way to render nothing: an ARMO with NO armature at all. It still
# equips and occupies its slot, so the actor looks naked while dressed. The
# usual cause is a wearable authored for one gender only (empty male fields).
no_arma = []
for r in by_type['ARMO']:
    if any(s.type == 'MODL' and len(s.data) == 4 for s in r.subrecords):
        continue
    b = _get(r, 'BOD2')
    slots = struct.unpack('<I', b.data[:4])[0] if b and len(b.data) >= 4 else 0
    if slots:                      # occupies a body slot, so it IS worn
        no_arma.append((r.form_id, edid(r), full(r), slots))

print(f'\nARMO with NO armature at all (equips, renders nothing): {len(no_arma)}')
for fid, e, f, slots in no_arma:
    wearers = []
    for r in by_type['NPC_']:
        for s in r.subrecords:
            if s.type == 'CNTO' and len(s.data) >= 4 \
                    and struct.unpack_from('<I', s.data, 0)[0] == fid:
                wearers.append(edid(r))
                break
            if s.type == 'DOFT' and len(s.data) == 4:
                o = struct.unpack('<I', s.data)[0]
                for ot in by_type['OTFT']:
                    if ot.form_id != o:
                        continue
                    for x in ot.subrecords:
                        if x.type == 'INAM' and any(
                                struct.unpack_from('<I', x.data, i)[0] == fid
                                for i in range(0, len(x.data) - 3, 4)):
                            wearers.append(edid(r))
                    break
    who = ', '.join(sorted(set(wearers))[:4]) if wearers else '(nobody)'
    print(f'  {e:34} {f[:20]:20} slots={slots:#010x}  worn by: {who}')
