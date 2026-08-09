"""Do the landscape textures a plugin declares resolve to real files?

Chain: LAND (per-cell texture layers) -> LTEX -> TNAM -> TXST -> TX00 -> .dds

Every break in that chain shows in game as untextured terrain, and none of it
is visible to a mesh- or record-level check, so this walks the whole thing.

Usage: python tools/ltex_check.py [plugin] [--all]
"""
import os
import struct
import sys
from collections import Counter

sys.path.insert(0, 'tools')
sys.path.insert(0, '.')
from tes5_esm_reader import read_tes5_file      # noqa: E402
from voice_audit import _get, _zstring          # noqa: E402

plugin = 'Nehrim.esm'
show_all = '--all' in sys.argv
for a in sys.argv[1:]:
    if not a.startswith('--'):
        plugin = a

ROOT = f'output/{plugin}/'
_h, recs, _ = read_tes5_file(f'{ROOT}{plugin}')
byfid = {r.form_id: r for r in recs}
kinds = Counter(r.type for r in recs)
print(f'LTEX {kinds.get("LTEX", 0)}   TXST {kinds.get("TXST", 0)}   '
      f'LAND {kinds.get("LAND", 0)}')


def edid(r):
    e = _get(r, 'EDID')
    return _zstring(e.data) if e else '?'


def tex_on_disk(rel):
    rel = rel.replace('\\', '/').lstrip('/')
    low = rel.lower()
    cand = [ROOT + rel]
    if not low.startswith('textures/'):
        cand.append(ROOT + 'Textures/' + rel)
        cand.append(ROOT + 'textures/' + rel)
    return any(os.path.exists(c) for c in cand)


ok, problems = 0, []
ltex_fids = set()
for r in recs:
    if r.type != 'LTEX':
        continue
    ltex_fids.add(r.form_id)
    name = edid(r)
    s = _get(r, 'TNAM')
    if not s or len(s.data) != 4:
        problems.append(('NO_TNAM', name, 'LTEX names no texture set'))
        continue
    tx = byfid.get(struct.unpack('<I', s.data)[0])
    if tx is None:
        problems.append(('NO_TXST', name, 'TNAM points at no record here'))
        continue
    p = _get(tx, 'TX00')
    if not p:
        problems.append(('NO_TX00', name, f'TXST {edid(tx)} has no diffuse'))
        continue
    rel = _zstring(p.data)
    if tex_on_disk(rel):
        ok += 1
    else:
        problems.append(('NO_FILE', name, rel))

print(f'\nLTEX resolving to a real .dds: {ok}')
print(f'LTEX broken:                   {len(problems)}')
for kind, cnt in Counter(k for k, _n, _d in problems).most_common():
    print(f'   {kind:8} {cnt}')
for kind, name, detail in (problems if show_all else problems[:15]):
    print(f'   {kind:8} {name:34} {detail}')
if not show_all and len(problems) > 15:
    print(f'   ... ({len(problems) - 15} more, pass --all)')

# Which LTEX do the LAND records actually reference?  A perfect LTEX set is
# useless if the terrain layers point somewhere else.
used, dangling = Counter(), Counter()
for r in recs:
    if r.type != 'LAND':
        continue
    for s in r.subrecords:
        if s.type in ('ATXT', 'BTXT') and len(s.data) >= 4:
            fid = struct.unpack_from('<I', s.data, 0)[0]
            (used if fid in ltex_fids else dangling)[fid] += 1
print(f'\nLAND texture layers pointing at an LTEX in this plugin: {sum(used.values())}')
print(f'LAND layers pointing ELSEWHERE (dangling): {sum(dangling.values())}')
for fid, n in dangling.most_common(8):
    print(f'   {fid:08X} x{n}')
