#!/usr/bin/env python3
"""A/B a script-driven (Say/SayTo) topic: which INFO does each engine pick?

Oblivion's `ref.SayTo target, Topic` and Skyrim's `ref.Say(Topic)` both select
ONE response out of the topic's INFO list by walking it in order and taking the
first whose conditions all pass.  The two engines disagree whenever a condition
depends on something only one of them supplies — above all the dialogue TARGET.
Skyrim's Say has no target at all (ObjectReference.psc:
`Say(Topic, Actor akActorToSpeakAs, bool abSpeakInPlayersHead)` — arg 2 is the
SPEAKER), so a converted RunOn=Target condition evaluates against nothing and a
line written for a different player race/sex can win.

This tool evaluates both sides under an explicit hypothetical player (race, sex)
and quest/script variable state, and prints the winning INFO for each, flagging
any topic where they differ.

Usage:
    # which line does Valen Dreth actually say, for an Argonian male player?
    python -m tools.dialog.say_topic_ab --export export/Oblivion.esm \
        --esm output/Oblivion.esm/Oblivion.esm \
        --topic CharGenTaunt2 --race Argonian --sex male --var tauntCount=0

    # sweep every taunt step
    python -m tools.dialog.say_topic_ab ... --topic CharGenTaunt2 --race Nord \
        --sex female --sweep tauntCount=0..3

    # audit every Say-driven topic for target-dependent selection
    python -m tools.dialog.say_topic_ab --export ... --esm ... --audit
"""
import argparse
import os
import struct
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.esm.tes5_esm_reader import read_tes5_file, _get, _CTDA_FUNC_NAMES

OPS = {0: '==', 1: '!=', 2: '>', 3: '>=', 4: '<', 5: '<='}

# TES4 condition indices this evaluator understands.
F_GETISRACE = 69
F_GETISSEX = 70
F_GETISID = 72
F_GETQUESTVAR = 79
F_GETSCRIPTVAR = 53
F_GETSTAGE = 58
# TES5 equivalents
F_GETVMQUESTVAR = 629
F_GETVMSCRIPTVAR = 630
F_GETISVOICETYPE = 426
F_GETISPLAYABLERACE = 254

RUNON_SUBJECT, RUNON_TARGET, RUNON_REFERENCE = 0, 1, 2


def cmp_ok(op, lhs, rhs):
    return {0: lhs == rhs, 1: lhs != rhs, 2: lhs > rhs,
            3: lhs >= rhs, 4: lhs < rhs, 5: lhs <= rhs}[op]


def read_export_type(export_dir, rectype):
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


def parse_tes4_ctdas(rec):
    out = []
    for i in range(int(rec.get('ConditionCount', 0) or 0)):
        raw = rec.get(f'Condition[{i}].Raw')
        if not raw:
            continue
        b = bytes.fromhex(raw)
        if len(b) < 24:
            continue
        tb = b[0]
        out.append({
            'op': (tb >> 5) & 7, 'or': bool(tb & 1),
            'runon_target': bool(tb & 2),
            'val': struct.unpack_from('<f', b, 4)[0],
            'func': struct.unpack_from('<H', b, 8)[0],
            'p1': struct.unpack_from('<I', b, 12)[0],
            'p2': struct.unpack_from('<I', b, 16)[0],
            'cis2': None,
        })
    return out


def parse_tes5_ctdas(rec):
    out = []
    pending = None
    for sub in rec.subrecords:
        if sub.type == 'CTDA' and len(sub.data) >= 32:
            d = sub.data
            tb = d[0]
            pending = {
                'op': (tb >> 5) & 7, 'or': bool(tb & 1),
                'val': struct.unpack_from('<f', d, 4)[0],
                'func': struct.unpack_from('<H', d, 8)[0],
                'p1': struct.unpack_from('<I', d, 12)[0],
                'p2': struct.unpack_from('<I', d, 16)[0],
                'runon': struct.unpack_from('<I', d, 20)[0],
                'ref': struct.unpack_from('<I', d, 24)[0],
                'cis2': None,
            }
            out.append(pending)
        elif sub.type == 'CIS2' and pending is not None:
            pending['cis2'] = sub.data.rstrip(b'\x00').decode(
                'utf-8', errors='replace')
    return out


class World:
    """The hypothetical runtime state both evaluators are scored against."""

    def __init__(self, race4=0, race5=0, sex=0, speaker4=0, speaker5=0,
                 target_is_player=True, variables=None, stages=None):
        self.race4 = race4              # TES4 RACE fid the player has
        self.race5 = race5              # Skyrim RACE fid the player has
        self.sex = sex                  # 0 male, 1 female
        self.speaker4 = speaker4        # who is talking (base fid, TES4)
        self.speaker5 = speaker5        # who is talking (base fid, TES5)
        self.target_is_player = target_is_player
        self.variables = variables or {}   # name -> value
        self.stages = stages or {}         # quest fid24 -> stage


def eval_tes4(conds, w):
    """Oblivion: RunOnTarget resolves to the SayTo target (the player)."""
    return _eval(conds, w, tes5=False)


def eval_tes5(conds, w):
    return _eval(conds, w, tes5=True)


def _eval(conds, w, tes5, trace=None):
    """Walk the AND/OR chain; unknown functions are treated as PASS so the
    comparison isolates target-dependent divergence rather than coverage.

    `trace`, when a list, collects a per-condition verdict line for --explain.
    """
    if not conds:
        return True, []
    groups, cur = [], []
    for c in conds:
        cur.append(c)
        if not c.get('or'):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    why = []
    ok_all = True
    for gi, grp in enumerate(groups):
        results = [_eval_one(c, w, tes5, why) for c in grp]
        if trace is not None:
            for c, r in zip(grp, results):
                trace.append(
                    f'      [{"PASS" if r else "FAIL"}] {_n(c["func"])}'
                    f'({c["p1"]:#x}, {c["p2"]:#x}) {OPS[c["op"]]} {c["val"]:g}'
                    + (f' runon={c.get("runon")}' if tes5 else '')
                    + (f' cis2={c["cis2"]}' if c.get('cis2') else ''))
        if not any(results):
            ok_all = False
            if trace is None:
                return False, why
    return ok_all, why


def _eval_one(c, w, tes5, why):
    f = c['func']
    op = c['op']
    val = c['val']

    if tes5:
        runon = c.get('runon', 0)
        # Who does this condition read?
        if runon == RUNON_SUBJECT:
            subject_is_player = False
        elif runon == RUNON_REFERENCE:
            subject_is_player = (c.get('ref') == 0x14)
        elif runon == RUNON_TARGET:
            # Say() supplies no target: the condition reads nothing.
            why.append(f'{_n(f)}: RunOn=Target under Say() -> no actor')
            return False
        else:
            subject_is_player = False
    else:
        subject_is_player = c.get('runon_target') and w.target_is_player

    if f == F_GETISRACE:
        have = (w.race5 if tes5 else w.race4) if subject_is_player else -1
        return cmp_ok(op, 1.0 if have == c['p1'] else 0.0, val)
    if f == F_GETISSEX:
        if not subject_is_player:
            return cmp_ok(op, 0.0, val)
        return cmp_ok(op, 1.0 if w.sex == c['p1'] else 0.0, val)
    if f == F_GETISID:
        who = 0x14 if subject_is_player else (w.speaker5 if tes5
                                              else w.speaker4)
        return cmp_ok(op, 1.0 if who == c['p1'] else 0.0, val)
    if f in (F_GETQUESTVAR, F_GETSCRIPTVAR, F_GETVMQUESTVAR,
             F_GETVMSCRIPTVAR):
        name = c.get('cis2')
        if name:
            name = name.strip(':').rsplit('_var', 1)[0].lstrip(':')
        else:
            name = c.get('varname')
        if name is None:
            return True
        have = w.variables.get(name)
        if have is None:
            return True
        return cmp_ok(op, float(have), val)
    if f == F_GETSTAGE:
        have = w.stages.get(c['p1'] & 0xFFFFFF)
        if have is None:
            return True
        return cmp_ok(op, float(have), val)
    return True          # not modelled -> pass


def _n(f):
    return _CTDA_FUNC_NAMES.get(f, f'Func{f}')


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--export', required=True)
    ap.add_argument('--esm', required=True)
    ap.add_argument('--topic', help='DIAL EditorID')
    ap.add_argument('--audit', action='store_true',
                    help='report every topic with RunOn=Target conditions')
    ap.add_argument('--race', default=None,
                    help='TES4 player race EditorID (e.g. Argonian)')
    ap.add_argument('--sex', default='male', choices=('male', 'female'))
    ap.add_argument('--var', action='append', default=[], metavar='NAME=VAL')
    ap.add_argument('--stage', action='append', default=[],
                    metavar='QUEST:N', help='quest stage state, e.g. Charactergen:16')
    ap.add_argument('--speaker', default=None,
                    help='EditorID of the NPC doing the Say (sets GetIsID)')
    ap.add_argument('--sweep', default=None, metavar='NAME=A..B')
    ap.add_argument('--explain', action='store_true',
                    help='print every INFO with a per-condition PASS/FAIL '
                         'verdict on both sides (why nothing was selected)')
    args = ap.parse_args()

    t4_dial = {r.get('EditorID', '').lower(): r
               for r in read_export_type(args.export, 'DIAL')}
    t4_infos = defaultdict(list)
    for r in read_export_type(args.export, 'INFO'):
        p = r.get('ParentDIAL')
        if p:
            t4_infos[p.upper()].append(r)

    # TES4 GetQuestVariable/GetScriptVariable name the variable by INDEX; the
    # index->name table lives on the SCPT the quest/ref runs.  Build
    # owner-fid24 -> {index: name} so those conditions can be evaluated by
    # the same variable names the TES5 side reads out of CIS2.
    script_vars = {}
    scpt_vars = {}
    for s in read_export_type(args.export, 'SCPT'):
        fid = int(s.get('FormID', '0'), 16) & 0xFFFFFF
        table = {}
        i = 0
        while f'Variable[{i}].Index' in s:
            table[int(s[f'Variable[{i}].Index'])] = s[f'Variable[{i}].Name']
            i += 1
        scpt_vars[fid] = table
    for rectype in ('QUST', 'NPC_', 'CREA', 'ACHR', 'ACRE', 'REFR'):
        for r in read_export_type(args.export, rectype):
            scri = r.get('SCRI')
            if scri:
                t = scpt_vars.get(int(scri, 16) & 0xFFFFFF)
                if t:
                    script_vars[int(r['FormID'], 16) & 0xFFFFFF] = t

    _h, recs, _l = read_tes5_file(args.esm,
                                  parse_types=frozenset({'DIAL', 'INFO'}))

    def z(s):
        return s.data.rstrip(b'\x00').decode('utf-8', 'replace') if s else None

    t5_dial, t5_infos = {}, defaultdict(list)
    for r in recs:
        if r.type == 'DIAL':
            e = z(_get(r, 'EDID'))
            if e:
                t5_dial[e.lower()] = r
        elif r.type == 'INFO':
            t5_infos[r.parent_dial].append(r)

    if args.audit:
        bad = []
        for name, d in sorted(t5_dial.items()):
            n = 0
            for i in t5_infos.get(d.form_id, []):
                for c in parse_tes5_ctdas(i):
                    if c.get('runon') == RUNON_TARGET:
                        n += 1
            if n:
                bad.append((name, n, len(t5_infos.get(d.form_id, []))))
        bad.sort(key=lambda x: -x[1])
        print(f'{len(bad)} topics still carry RunOn=Target conditions')
        for name, n, tot in bad[:40]:
            print(f'  {name:44} {n:4} target-conds over {tot} INFOs')
        return 0

    if not args.topic:
        ap.error('--topic or --audit required')

    from tes5_import.skyrim_overrides import RACE_MAP, TES4_RACE_FID_TO_EDID
    race4 = race5 = 0
    if args.race:
        for fid24, edid in TES4_RACE_FID_TO_EDID.items():
            if edid.lower() == args.race.lower():
                race4 = fid24
                race5 = RACE_MAP.get(edid, 0)
                break
        if not race4:
            print(f'unknown race {args.race}')
            return 1

    # quest stages: resolve EditorID -> fid24 so both evaluators agree
    stages = {}
    for sv in args.stage:
        qname, num = sv.rsplit(':', 1)
        for q in read_export_type(args.export, 'QUST'):
            if (q.get('EditorID') or '').lower() == qname.lower():
                stages[int(q['FormID'], 16) & 0xFFFFFF] = int(num)
                break

    variables = {}
    for kv in args.var:
        k, v = kv.split('=', 1)
        variables[k] = float(v)

    speaker4 = speaker5 = 0
    if args.speaker:
        for rt in ('NPC_', 'CREA'):
            for r in read_export_type(args.export, rt):
                if (r.get('EditorID') or '').lower() == args.speaker.lower():
                    speaker4 = int(r['FormID'], 16)
                    break
            if speaker4:
                break
        for r in recs:
            if r.type == 'NPC_':
                pass
        # the TES5 side keeps the same 24-bit id under the plugin's index
        speaker5 = (speaker4 & 0xFFFFFF) | 0x01000000 if speaker4 else 0

    sweep_name, sweep_range = None, [None]
    if args.sweep:
        n, rng = args.sweep.split('=', 1)
        a, b = rng.split('..')
        sweep_name, sweep_range = n, list(range(int(a), int(b) + 1))

    d4 = t4_dial.get(args.topic.lower())
    d5 = t5_dial.get(args.topic.lower())
    if not d4 or not d5:
        print(f'topic {args.topic} missing '
              f'(TES4={bool(d4)} TES5={bool(d5)})')
        return 1
    infos4 = t4_infos.get(d4['FormID'].upper(), [])
    infos5 = t5_infos.get(d5.form_id, [])
    print(f'{args.topic}: {len(infos4)} TES4 INFOs / {len(infos5)} TES5 INFOs')
    print(f'player: race={args.race} sex={args.sex}')
    print()

    def txt4(r):
        return r.get('Response[0].ResponseText', '') if r else None

    def txt5(r):
        if not r:
            return None
        for s in r.subrecords:
            if s.type == 'NAM1':
                return s.data.rstrip(b'\x00').decode('utf-8', 'replace')
        return ''

    for sv in sweep_range:
        vs = dict(variables)
        if sweep_name:
            vs[sweep_name] = float(sv)
        w = World(race4=race4, race5=race5,
                  sex=(0 if args.sex == 'male' else 1),
                  speaker4=speaker4, speaker5=speaker5,
                  variables=vs, stages=stages)

        pick4 = None
        for r in infos4:
            conds = parse_tes4_ctdas(r)
            for c in conds:
                if c['func'] in (F_GETQUESTVAR, F_GETSCRIPTVAR):
                    table = script_vars.get(c['p1'] & 0xFFFFFF, {})
                    c['varname'] = table.get(c['p2'])
            tr = [] if args.explain else None
            ok, _why = _eval(conds, w, False, tr)
            if args.explain:
                print(f'   TES4 INFO {r.get("FormID")} '
                      f'{"SELECTED" if ok else "rejected"} '
                      f'"{(r.get("Response[0].ResponseText") or "")[:50]}"')
                for ln in tr:
                    print(ln)
            if ok and pick4 is None:
                pick4 = r
                if not args.explain:
                    break
        pick5 = None
        why5 = None
        for r in infos5:
            tr = [] if args.explain else None
            ok, why = _eval(parse_tes5_ctdas(r), w, True, tr)
            if args.explain:
                print(f'   TES5 INFO {r.form_id:08X} '
                      f'{"SELECTED" if ok else "rejected"} '
                      f'"{(txt5(r) or "")[:50]}"')
                for ln in tr:
                    print(ln)
            if ok and pick5 is None:
                pick5 = r
                if not args.explain:
                    break
            if why5 is None:
                why5 = why

        label = f'{sweep_name}={sv}' if sweep_name else 'state'
        a, b = txt4(pick4), txt5(pick5)
        same = (a or '')[:60] == (b or '')[:60]
        print(f'[{label}] {"MATCH" if same else "DIVERGE"}')
        print(f'   TES4 -> {(a or "(none)")[:78]}')
        print(f'   TES5 -> {(b or "(none)")[:78]}')
        if not same and why5:
            print(f'   why: {why5[0]}')
        print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
