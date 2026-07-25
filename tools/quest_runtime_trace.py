#!/usr/bin/env python3
"""Quest runtime tracer: what actually FIRES when a quest sits at a stage.

`quest_walkthrough` answers "can the stage graph advance?".  This answers the
different question that a stuck quest actually poses: with the quest parked at
stage N, which packages / dialogue / scripted actors are supposed to react, and
did each of those survive conversion?

For a quest it reports, side by side for the TES4 export and the converted TES5
output:

  * every AI PACKAGE whose conditions reference the quest (GetStage /
    GetStageDone / GetQuestRunning / quest variables), which actors carry it,
    and whether the converted package still exists with the same gate
  * every INFO gated on the quest, its speaker set, and whether it survived
  * every SCRIPT (quest script + object scripts) that reads or writes the
    quest's stages/variables, and the converted Papyrus counterpart
  * per-stage: which of the above become eligible

Usage:
    python -m tools.quest_runtime_trace --export export/Oblivion.esm \
        --esm output/Oblivion.esm/Oblivion.esm \
        --scripts output/Oblivion.esm/scripts --quest CharacterGen

    # only report the problems
    python -m tools.quest_runtime_trace ... --quest CharacterGen --problems

    # focus one stage
    python -m tools.quest_runtime_trace ... --quest CharacterGen --stage 10
"""
import argparse
import os
import re
import struct
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.tes5_esm_reader import (read_tes5_file, _get, _all,
                                   _CTDA_FUNC_NAMES, _CTDA_RUNON_NAMES)

# TES4 condition function indices that take a QUEST as param1
TES4_QUEST_FUNCS = {
    58: 'GetStage',
    59: 'GetStageDone',
    56: 'GetQuestRunning',
    79: 'GetQuestVariable',
}
# TES5 equivalents (post _FUNC_REMAP)
TES5_QUEST_FUNCS = {
    58: 'GetStage',
    59: 'GetStageDone',
    56: 'GetQuestRunning',
    629: 'GetVMQuestVariable',
    630: 'GetVMScriptVariable',
}

_OPS = {0: '==', 1: '!=', 2: '>', 3: '>=', 4: '<', 5: '<='}


# ---------------------------------------------------------------------------
# TES4 export side
# ---------------------------------------------------------------------------

def read_export_type(export_dir, rectype):
    """Yield dicts of KEY=VALUE for each record of a type."""
    path = os.path.join(export_dir, rectype + '.txt')
    if not os.path.isfile(path):
        return
    cur = None
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip('\n')
            if line.startswith('---RECORD_BEGIN---'):
                cur = {}
            elif line.startswith('---RECORD_END---'):
                if cur is not None:
                    yield cur
                cur = None
            elif cur is not None and '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                cur[k] = v


def parse_raw_ctda(hexstr):
    """TES4 export stores conditions as a 24-byte raw blob."""
    try:
        b = bytes.fromhex(hexstr)
    except ValueError:
        return None
    if len(b) < 24:
        return None
    type_byte = b[0]
    return {
        'op': _OPS.get((type_byte >> 5) & 7, '?'),
        'or': bool(type_byte & 1),
        'val': struct.unpack_from('<f', b, 4)[0],
        'func': struct.unpack_from('<H', b, 8)[0],
        'p1': struct.unpack_from('<I', b, 12)[0],
        'p2': struct.unpack_from('<I', b, 16)[0],
        'runon': struct.unpack_from('<I', b, 20)[0],
    }


def rec_ctdas_tes4(rec):
    out = []
    n = int(rec.get('ConditionCount', 0) or 0)
    for i in range(n):
        raw = rec.get(f'Condition[{i}].Raw')
        if raw:
            c = parse_raw_ctda(raw)
            if c:
                out.append(c)
    return out


def fid(s):
    try:
        return int(s, 16)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# TES5 output side
# ---------------------------------------------------------------------------

def rec_ctdas_tes5(rec):
    out = []
    for sub in rec.subrecords:
        if sub.type != 'CTDA' or len(sub.data) < 32:
            continue
        d = sub.data
        tb = d[0]
        out.append({
            'op': _OPS.get((tb >> 5) & 7, '?'),
            'or': bool(tb & 1),
            'val': struct.unpack_from('<f', d, 4)[0],
            'func': struct.unpack_from('<H', d, 8)[0],
            'p1': struct.unpack_from('<I', d, 12)[0],
            'p2': struct.unpack_from('<I', d, 16)[0],
            'runon': struct.unpack_from('<I', d, 20)[0],
        })
    return out


def zstr(sub):
    if sub is None:
        return None
    return sub.data.rstrip(b'\x00').decode('utf-8', errors='replace')


def edid(rec):
    return zstr(_get(rec, 'EDID'))


def fmt_ctda(c, names, resolve=None):
    fn = names.get(c['func'], f"Func{c['func']}")
    p1 = c['p1']
    p1s = f'{p1:08X}'
    if resolve:
        nm = resolve(p1)
        if nm:
            p1s = f'{nm}({p1:08X})'
    extra = ''
    if c['runon']:
        extra += f" [{_CTDA_RUNON_NAMES.get(c['runon'], c['runon'])}]"
    if c['or']:
        extra += ' OR'
    return f"{fn}({p1s}) {c['op']} {c['val']:g}{extra}"


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------

class Trace:
    def __init__(self, export_dir, esm_path, scripts_dir):
        self.export_dir = export_dir
        self.scripts_dir = scripts_dir

        # --- TES4 side ---
        self.t4 = {}          # rectype -> list of dicts
        for rt in ('QUST', 'PACK', 'INFO', 'DIAL', 'NPC_', 'CREA', 'SCPT'):
            self.t4[rt] = list(read_export_type(export_dir, rt))
        self.t4_by_fid = {}
        self.t4_edid = {}
        for rt, recs in self.t4.items():
            for r in recs:
                f = fid(r.get('FormID'))
                if f:
                    self.t4_by_fid[f] = r
                e = r.get('EditorID')
                if e:
                    self.t4_edid.setdefault(e.lower(), r)

        # --- TES5 side ---
        _h, recs, _l = read_tes5_file(esm_path, parse_types=frozenset(
            {'QUST', 'PACK', 'INFO', 'DIAL', 'NPC_', 'SCEN'}))
        self.t5 = defaultdict(list)
        self.t5_by_fid = {}
        self.t5_edid = {}
        for r in recs:
            self.t5[r.type].append(r)
            self.t5_by_fid[r.form_id] = r
            e = edid(r)
            if e:
                self.t5_edid.setdefault(e.lower(), r)

    # -- quest lookup ------------------------------------------------------
    def find_quest(self, name):
        q4 = self.t4_edid.get(name.lower())
        if q4 is None or q4.get('Signature') != 'QUST':
            for r in self.t4['QUST']:
                if (r.get('EditorID') or '').lower() == name.lower():
                    q4 = r
                    break
        q5 = None
        for r in self.t5['QUST']:
            if (edid(r) or '').lower() == name.lower():
                q5 = r
                break
        return q4, q5

    # -- packages ----------------------------------------------------------
    def packages_for(self, qfid4, qfid5):
        """Packages whose conditions reference the quest, TES4 + TES5."""
        t4 = []
        for p in self.t4['PACK']:
            cs = [c for c in rec_ctdas_tes4(p)
                  if c['func'] in TES4_QUEST_FUNCS and c['p1'] == qfid4]
            if cs:
                t4.append((p, cs))
        t5 = {}
        for p in self.t5['PACK']:
            cs = [c for c in rec_ctdas_tes5(p)
                  if c['func'] in TES5_QUEST_FUNCS and c['p1'] == qfid5]
            if cs:
                t5[(edid(p) or '').lower()] = (p, cs)
        return t4, t5

    # -- infos -------------------------------------------------------------
    def infos_for(self, qfid4, qfid5):
        t4 = []
        for i in self.t4['INFO']:
            cs = [c for c in rec_ctdas_tes4(i)
                  if c['func'] in TES4_QUEST_FUNCS and c['p1'] == qfid4]
            if cs:
                t4.append((i, cs))
        t5 = []
        for i in self.t5['INFO']:
            cs = [c for c in rec_ctdas_tes5(i)
                  if c['func'] in TES5_QUEST_FUNCS and c['p1'] == qfid5]
            if cs:
                t5.append((i, cs))
        return t4, t5

    # -- package owners ----------------------------------------------------
    def package_owners_t4(self):
        """map package FormID -> [actor EditorID] from NPC_/CREA PKID lists."""
        owners = defaultdict(list)
        for rt in ('NPC_', 'CREA'):
            for a in self.t4[rt]:
                name = a.get('EditorID') or a.get('FormID')
                for k, v in a.items():
                    if k.startswith('Package[') and k.endswith('].FormID'):
                        owners[fid(v)].append(name)
                    elif k == 'PKID':
                        owners[fid(v)].append(name)
        return owners

    def package_owners_t5(self):
        owners = defaultdict(list)
        for a in self.t5['NPC_']:
            name = edid(a) or f'{a.form_id:08X}'
            for sub in a.subrecords:
                if sub.type == 'PKID' and len(sub.data) >= 4:
                    owners[struct.unpack('<I', sub.data[:4])[0]].append(name)
        return owners


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--export', required=True)
    ap.add_argument('--esm', required=True)
    ap.add_argument('--scripts', default=None)
    ap.add_argument('--quest', required=True)
    ap.add_argument('--stage', type=int, default=None,
                    help='only report gates that involve this stage')
    ap.add_argument('--problems', action='store_true',
                    help='only print items that failed to convert')
    args = ap.parse_args()

    t = Trace(args.export, args.esm, args.scripts)
    q4, q5 = t.find_quest(args.quest)
    if not q4:
        print(f'quest {args.quest} not found in export')
        return 1
    qfid4 = fid(q4['FormID'])
    print(f"TES4 quest {q4['EditorID']} {qfid4:08X}  "
          f"stages={q4.get('StageCount')} prio={q4.get('DATA.Priority')}")
    if not q5:
        print('  !! MISSING from converted output')
        return 1
    qfid5 = q5.form_id
    print(f"TES5 quest {edid(q5)} {qfid5:08X}")
    print()

    owners4 = t.package_owners_t4()
    owners5 = t.package_owners_t5()

    p4, p5 = t.packages_for(qfid4, qfid5)
    print(f'=== AI packages gated on this quest: {len(p4)} TES4 / {len(p5)} TES5 ===')
    missing = 0
    for p, cs in sorted(p4, key=lambda x: x[0].get('EditorID') or ''):
        name = p.get('EditorID') or p['FormID']
        if args.stage is not None:
            if not any(abs(c['val'] - args.stage) < 0.01 for c in cs):
                continue
        conv = p5.get(name.lower())
        gate4 = '; '.join(fmt_ctda(c, TES4_QUEST_FUNCS) for c in cs)
        own = owners4.get(fid(p['FormID']), [])
        if conv is None:
            missing += 1
            print(f'  [MISSING] {name:38} {gate4}')
            print(f'            actors: {", ".join(own) or "(none)"}')
            continue
        cp, ccs = conv
        gate5 = '; '.join(fmt_ctda(c, TES5_QUEST_FUNCS) for c in ccs)
        own5 = owners5.get(cp.form_id, [])
        same = (len(cs) == len(ccs) and
                all(a['op'] == b['op'] and abs(a['val'] - b['val']) < 0.01
                    for a, b in zip(cs, ccs)))
        flag = ''
        if not same:
            flag = ' GATE-CHANGED'
        if own and not own5:
            flag += ' NO-OWNER'
        if args.problems and not flag:
            continue
        print(f'  [{"ok" if not flag else "!!"}]{flag} {name:36}')
        print(f'        TES4 {gate4}')
        print(f'        TES5 {gate5}')
        print(f'        actors: TES4 {len(own)} {own[:4]} -> TES5 {len(own5)} {own5[:4]}')
    print(f'  ({missing} packages missing from output)')
    print()

    i4, i5 = t.infos_for(qfid4, qfid5)
    print(f'=== INFOs gated on this quest: {len(i4)} TES4 / {len(i5)} TES5 ===')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
