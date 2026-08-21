#!/usr/bin/env python3
"""Read a loaded reference's 3D and Gamebryo animation state out of the RUNNING game.

Answers, without a relaunch, the questions that decided the arena-spectator
bug (2026-08-18):

  * what is each node's CURRENT local transform (is Bip01 identity or authored?)
  * is it changing frame to frame (animating) or frozen?
  * which NiControllerSequences does the NiControllerManager hold, what are
    their cycle types, and which are ANIMATING / INACTIVE right now?
  * and, to prove a fix before rebuilding: flip a loaded sequence's cycle type
    or a controlled block's pose in memory, `sae AutoReset`, and watch.

Everything is read with the bridge's `readmem`/`call`/`writemem` primitives
(tools/game_bridge.py) -- no plugin rebuild, no thread suspension.

Usage:
    python tools/nif_live.py nodes 1318B6C5 --names "Bip01,Bip01 NonAccum" --samples 6
    python tools/nif_live.py tree 1318B6C5 [--depth 3]
    python tools/nif_live.py sequences 1318B6C5
    python tools/nif_live.py set-cycle 1318B6C5 AutoLoop 0        # 0 LOOP 1 REVERSE 2 CLAMP
    python tools/nif_live.py set-pose 1318B6C5 AutoPlay Bip01 --identity   # or --sentinel
    python tools/nif_live.py sae 1318B6C5 AutoReset

Verified layouts (SSE 1.6.1170, read back and cross-checked against the NIF):
    TESObjectREFR +0x68 -> LOADED_REF_DATA, whose +0x68 -> root NiAVObject
    NiObjectNET   +0x10 name (BSFixedString char*), +0x18 first controller
    NiAVObject    +0x48 local rotate (3x3 row-major), +0x6C local translate,
                  +0x78 local scale, +0x7C world rotate, +0xA0 world translate
    NiNode        +0x110 children NiTArray (data +0x8, capacity/free/size u16)
    NiControllerManager (first controller on the root):
                  +0x48 sequences NiTArray (data +0x50, cap/free/size @+0x58)
    NiControllerSequence: +0x10 name, +0x18 arraySize, +0x20 InterpArrayItem*,
                  +0x28 IDTag* (0x28 each, avObjectName first), +0x40 cycleType,
                  +0x44 frequency, +0x48 beginKeyTime, +0x4C endKeyTime,
                  +0x50 lastTime, +0x68 state (0 INACTIVE 1 ANIMATING 2 EASEIN
                  3 EASEOUT 4 TRANSSOURCE 5 TRANSDEST 6 MORPHSOURCE),
                  +0x88 accumRootName, +0x90 accumRoot
    InterpArrayItem 0x20: +0 interpolator, +8 controller, +0x10 blendInterp
    NiTransformInterpolator: +0x18 translate, +0x24 quat (w,x,y,z), +0x34 scale,
                  +0x38 NiTransformData*
"""

import argparse
import math
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from game_bridge import Bridge  # noqa: E402

LOOKUP_BY_ID = 14617           # TESForm::LookupByID stable id
NO_VALUE = -3.4028234663852886e+38
STATES = {0: 'INACTIVE', 1: 'ANIMATING', 2: 'EASEIN', 3: 'EASEOUT',
          4: 'TRANSSOURCE', 5: 'TRANSDEST', 6: 'MORPHSOURCE'}
CYCLES = {0: 'LOOP', 1: 'REVERSE', 2: 'CLAMP'}


class Live:
    def __init__(self, bridge: Bridge):
        self.b = bridge

    # -- raw ---------------------------------------------------------------
    def rd(self, addr, n):
        return bytes.fromhex(self.b.readmem(address=addr, length=n)['hex'])

    def u64(self, addr):
        return struct.unpack('<Q', self.rd(addr, 8))[0]

    def cstr(self, p, n=96):
        if not p or p < 0x10000:
            return ''
        try:
            s = self.b.readmem(address=p, length=n, as_string=True)
            return s.get('string') or ''
        except Exception:
            return ''

    # -- reference / tree --------------------------------------------------
    def root_3d(self, formid):
        ptr = int(self.b.call(id=LOOKUP_BY_ID, args_=[formid])['result'])
        if not ptr:
            raise SystemExit(f'form {formid:08X} not found')
        loaded = self.u64(ptr + 0x68)
        if not loaded:
            raise SystemExit('reference has no loaded data (not in a loaded cell?)')
        root = self.u64(loaded + 0x68)
        if not root:
            raise SystemExit('reference has no 3D')
        return root

    def name_of(self, node):
        return self.cstr(self.u64(node + 0x10))

    def children(self, node):
        hdr = self.rd(node + 0x110, 0x18)
        data = struct.unpack_from('<Q', hdr, 0x08)[0]
        cap, free, size = struct.unpack_from('<3H', hdr, 0x10)
        if not data or cap > 1024 or free > cap:
            return []
        return [p for p in struct.unpack(f'<{free}Q', self.rd(data, 8 * free)) if p]

    def local(self, node):
        d = self.rd(node + 0x48, 0x34)
        rot = struct.unpack_from('<9f', d, 0)
        tr = struct.unpack_from('<3f', d, 0x24)
        sc = struct.unpack_from('<f', d, 0x30)[0]
        return rot, tr, sc

    def world_translate(self, node):
        return struct.unpack_from('<3f', self.rd(node + 0xA0, 12), 0)

    def walk(self, node, depth=0, max_depth=32, parent=''):
        nm = self.name_of(node)
        yield node, nm, depth, parent
        if depth >= max_depth:
            return
        try:
            kids = self.children(node)
        except Exception:
            kids = []          # NiTriShape etc. -- no children array
        for c in kids:
            yield from self.walk(c, depth + 1, max_depth, nm)

    def find_nodes(self, root, names):
        want = set(names)
        found = {}
        for node, nm, _, _ in self.walk(root):
            if nm in want and nm not in found:
                found[nm] = node
        return found

    # -- controller manager / sequences ------------------------------------
    def manager(self, root):
        mgr = self.u64(root + 0x18)
        if not mgr:
            raise SystemExit('root has no controller (no NiControllerManager)')
        return mgr

    def sequences(self, mgr):
        data = self.u64(mgr + 0x50)
        cap, free, size = struct.unpack_from('<3H', self.rd(mgr + 0x58, 6), 0)
        if not data or free > cap:
            return []
        return [p for p in struct.unpack(f'<{free}Q', self.rd(data, 8 * free)) if p]

    def seq_info(self, seq):
        d = self.rd(seq, 0xA0)
        return {
            'addr': seq,
            'name': self.cstr(struct.unpack_from('<Q', d, 0x10)[0]),
            'n': struct.unpack_from('<I', d, 0x18)[0],
            'items': struct.unpack_from('<Q', d, 0x20)[0],
            'tags': struct.unpack_from('<Q', d, 0x28)[0],
            'cycle': struct.unpack_from('<I', d, 0x40)[0],
            'freq': struct.unpack_from('<f', d, 0x44)[0],
            'begin': struct.unpack_from('<f', d, 0x48)[0],
            'end': struct.unpack_from('<f', d, 0x4C)[0],
            'lastTime': struct.unpack_from('<f', d, 0x50)[0],
            'state': struct.unpack_from('<I', d, 0x68)[0],
            'accum': self.cstr(struct.unpack_from('<Q', d, 0x88)[0]),
            'accumRoot': struct.unpack_from('<Q', d, 0x90)[0],
        }

    def seq_items(self, info):
        """[(node_name, interpolator_ptr)] in controlled-block order."""
        n = info['n']
        if not n or not info['items'] or not info['tags']:
            return []
        tags = self.rd(info['tags'], 0x28 * n)
        items = self.rd(info['items'], 0x20 * n)
        out = []
        for i in range(n):
            nm = self.cstr(struct.unpack_from('<Q', tags, i * 0x28)[0])
            interp = struct.unpack_from('<Q', items, i * 0x20)[0]
            out.append((nm, interp))
        return out

    def interp_pose(self, interp):
        d = self.rd(interp, 0x40)
        return (struct.unpack_from('<3f', d, 0x18), struct.unpack_from('<4f', d, 0x24),
                struct.unpack_from('<f', d, 0x34)[0], struct.unpack_from('<Q', d, 0x38)[0])


def _fmt_rot(rot):
    yaw = math.degrees(math.atan2(rot[3], rot[0]))
    return f'row0=({rot[0]:.3f},{rot[1]:.3f},{rot[2]:.3f}) yaw~{yaw:.1f}'


def cmd_tree(a):
    with Bridge() as b:
        L = Live(b)
        root = L.root_3d(int(a.ref, 16))
        for node, nm, depth, _ in L.walk(root, max_depth=a.depth):
            rot, tr, sc = L.local(node)
            print(f"{'  ' * depth}{nm!r} t=({tr[0]:.2f},{tr[1]:.2f},{tr[2]:.2f}) "
                  f"{_fmt_rot(rot)} s={sc:.2f}")
    return 0


def cmd_nodes(a):
    names = [n.strip() for n in a.names.split(',') if n.strip()]
    with Bridge() as b:
        L = Live(b)
        root = L.root_3d(int(a.ref, 16))
        found = L.find_nodes(root, names)
        missing = [n for n in names if n not in found]
        if missing:
            print('not found:', missing)
        for i in range(a.samples):
            parts = []
            for nm in names:
                if nm not in found:
                    continue
                rot, tr, sc = L.local(found[nm])
                parts.append(f"{nm}: t=({tr[0]:.2f},{tr[1]:.2f},{tr[2]:.2f}) {_fmt_rot(rot)}")
            print(f'[{i}] ' + ' | '.join(parts), flush=True)
            if i + 1 < a.samples:
                time.sleep(a.interval)
    return 0


def cmd_sequences(a):
    with Bridge() as b:
        L = Live(b)
        root = L.root_3d(int(a.ref, 16))
        mgr = L.manager(root)
        seqs = L.sequences(mgr)
        print(f'manager {mgr:#x}: {len(seqs)} sequence(s)')
        for s in seqs:
            i = L.seq_info(s)
            print(f"  {i['name']!r}: cycle={CYCLES.get(i['cycle'], i['cycle'])} "
                  f"state={STATES.get(i['state'], i['state'])} keys[{i['begin']:.3f},{i['end']:.3f}] "
                  f"lastTime={i['lastTime']:.3f} blocks={i['n']} accumRoot={i['accum']!r}"
                  f"{'' if i['accumRoot'] else ' (unbound)'}")
            if a.blocks:
                for nm, interp in L.seq_items(i):
                    if not interp:
                        print(f'      {nm!r}: (no interpolator)')
                        continue
                    try:
                        tr, q, sc, data = L.interp_pose(interp)
                    except Exception:
                        print(f'      {nm!r}: interp {interp:#x}')
                        continue
                    def f(v):
                        return 'NONE' if v <= NO_VALUE else f'{v:.3g}'
                    print(f"      {nm!r}: pose t=({f(tr[0])},{f(tr[1])},{f(tr[2])}) "
                          f"q=({f(q[0])},{f(q[1])},{f(q[2])},{f(q[3])}) s={f(sc)} "
                          f"data={'yes' if data else 'no'}")
    return 0


def _find_seq(L, root, name):
    for s in L.sequences(L.manager(root)):
        i = L.seq_info(s)
        if i['name'].lower() == name.lower():
            return i
    raise SystemExit(f'no sequence named {name!r} on this reference')


def cmd_set_cycle(a):
    with Bridge() as b:
        L = Live(b)
        root = L.root_3d(int(a.ref, 16))
        i = _find_seq(L, root, a.sequence)
        b.writemem(i['addr'] + 0x40, struct.pack('<I', a.cycle))
        after = struct.unpack('<I', L.rd(i['addr'] + 0x40, 4))[0]
        print(f"{i['name']}: cycleType {CYCLES.get(i['cycle'])} -> {CYCLES.get(after)}")
    return 0


def cmd_set_pose(a):
    with Bridge() as b:
        L = Live(b)
        root = L.root_3d(int(a.ref, 16))
        i = _find_seq(L, root, a.sequence)
        hit = [(nm, ip) for nm, ip in L.seq_items(i) if nm == a.node and ip]
        if not hit:
            raise SystemExit(f'{a.node!r} is not a controlled block of {i["name"]!r}')
        for nm, ip in hit:
            tr, q, sc, data = L.interp_pose(ip)
            if data:
                raise SystemExit(f'{nm!r} has key data; only pose (data-less) interpolators are patched')
            if a.identity:
                payload = struct.pack('<3f', 0, 0, 0) + struct.pack('<4f', 1, 0, 0, 0) + struct.pack('<f', 1.0)
            else:
                payload = struct.pack('<8f', *([NO_VALUE] * 8))
            b.writemem(ip + 0x18, payload)
            tr2, q2, sc2, _ = L.interp_pose(ip)
            print(f'{nm!r} interp {ip:#x}: t={tr}->{tr2} q={q}->{q2} s={sc:.3g}->{sc2:.3g}')
    return 0


def cmd_sae(a):
    with Bridge() as b:
        out = b.console(f'sae {a.event}', ref=int(a.ref, 16))
        print(out if out else '(accepted: empty reply)')
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('tree'); s.add_argument('ref'); s.add_argument('--depth', type=int, default=4); s.set_defaults(fn=cmd_tree)
    s = sub.add_parser('nodes'); s.add_argument('ref'); s.add_argument('--names', required=True)
    s.add_argument('--samples', type=int, default=1); s.add_argument('--interval', type=float, default=0.4); s.set_defaults(fn=cmd_nodes)
    s = sub.add_parser('sequences'); s.add_argument('ref'); s.add_argument('--blocks', action='store_true'); s.set_defaults(fn=cmd_sequences)
    s = sub.add_parser('set-cycle'); s.add_argument('ref'); s.add_argument('sequence'); s.add_argument('cycle', type=int, choices=(0, 1, 2)); s.set_defaults(fn=cmd_set_cycle)
    s = sub.add_parser('set-pose'); s.add_argument('ref'); s.add_argument('sequence'); s.add_argument('node')
    g = s.add_mutually_exclusive_group(required=True); g.add_argument('--identity', action='store_true'); g.add_argument('--sentinel', action='store_true'); s.set_defaults(fn=cmd_set_pose)
    s = sub.add_parser('sae'); s.add_argument('ref'); s.add_argument('event'); s.set_defaults(fn=cmd_sae)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == '__main__':
    sys.exit(main())
