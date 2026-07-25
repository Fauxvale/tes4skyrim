"""Loader: wire the real converted output into the emulator's engine model.

Reads the generated .psc sources, the converted ESM's DIAL/INFO records (for
topic contents, response order, conditions and fragment bindings), the quest
stage fragments, and the measured voice durations, then builds an `Engine`
whose state matches a freshly started game at a chosen quest stage.
"""
import os
import re
import struct
from collections import defaultdict

from tools.papyrus_emulator import Engine, ScriptInstance, InfoModel, TopicModel
from tools.tes5_esm_reader import read_tes5_file, _get

_OPS = {0: '==', 1: '!=', 2: '>', 3: '>=', 4: '<', 5: '<='}

F_GETSTAGE = 58
F_GETSTAGEDONE = 59
F_GETISID = 72
F_GETVMQUESTVAR = 629
F_GETVMSCRIPTVAR = 630


def _z(sub):
    return sub.data.rstrip(b'\x00').decode('utf-8', 'replace') if sub else None


def load_scripts(scripts_dir):
    """name (lower) -> source text, for every generated .psc."""
    out = {}
    for name in os.listdir(scripts_dir):
        if name.lower().endswith('.psc'):
            path = os.path.join(scripts_dir, name)
            with open(path, encoding='utf-8', errors='replace') as f:
                out[name[:-4].lower()] = f.read()
    return out


def parse_ctdas(rec):
    """[(func, param1, op, value, cis2)] in record order."""
    out = []
    for sub in rec.subrecords:
        if sub.type == 'CTDA' and len(sub.data) >= 32:
            d = sub.data
            out.append([struct.unpack_from('<H', d, 8)[0],
                        struct.unpack_from('<I', d, 12)[0],
                        _OPS.get((d[0] >> 5) & 7, '=='),
                        struct.unpack_from('<f', d, 4)[0],
                        None,
                        bool(d[0] & 1)])          # OR flag
        elif sub.type == 'CIS2' and out:
            out[-1][4] = _z(sub)
    return out


def info_fragment_script(rec):
    """Fragment script name bound to this INFO's VMAD, or None."""
    v = _get(rec, 'VMAD')
    if not v:
        return None
    d = v.data
    try:
        _ver, _objf, cnt = struct.unpack_from('<hhH', d, 0)
        p = 6
        for _ in range(cnt):
            n = struct.unpack_from('<H', d, p)[0]
            p += 2 + n
            p += 1
            np_ = struct.unpack_from('<H', d, p)[0]
            p += 2
            for _ in range(np_):
                pn = struct.unpack_from('<H', d, p)[0]
                p += 2 + pn
                t = d[p]
                p += 2
                if t == 1:
                    p += 8
                elif t == 2:
                    sl = struct.unpack_from('<H', d, p)[0]
                    p += 2 + sl
                elif t in (3, 4):
                    p += 4
                elif t == 5:
                    p += 1
                else:
                    return None
        p += 2                       # fragment version + flags
        fn = struct.unpack_from('<H', d, p)[0]
        p += 2
        return d[p:p + fn].decode('utf-8', 'replace')
    except (struct.error, UnicodeDecodeError):
        return None


def _quest_topic_names(esm_recs, quest_edid, dial_name):
    """Topic names owned by this quest (DIAL.QNAM points at the quest)."""
    return set()


def _relevant_scripts(sources, quest_edid, dial_name, infos_by_dial,
                      quest_topics=None):
    """Script names whose bodies can drive this quest's conversation.

    A script qualifies when it mentions the quest by name (so it reads or
    writes its stages/variables) or Says one of the quest's topics.  That is
    the closed set the state machine runs on, and it keeps the simulation to
    a few dozen instances instead of every converted script in the game.
    """
    q = quest_edid.lower()
    topics = {n for n in dial_name.values() if n}
    out = set()
    for key, src in sources.items():
        if key.startswith('tes4_tif__'):
            continue
        low = src.lower()
        if q in low:
            out.add(key)
            continue
        # Only a Say of one of THIS QUEST's topics matters; matching any
        # topic in the game pulled in 8,881 scripts.
        if quest_topics:
            for t in re.findall(r'\.say\(\s*(\w+)', low):
                if t in quest_topics:
                    out.add(key)
                    break
    out.add(('tes4_qf_%s' % q))
    return {k for k in out if k in sources}


def build_engine(scripts_dir, esm_path, export_dir, quest_edid, rng,
                 trace=False, durations=None):
    """Assemble an Engine holding the converted scripts, topics and fragments."""
    eng = Engine(rng, trace=trace)
    sources = load_scripts(scripts_dir)

    _h, recs, _l = read_tes5_file(esm_path,
                                  parse_types=frozenset({'DIAL', 'INFO',
                                                         'QUST', 'ACHR',
                                                         'NPC_'}))
    dial_name, infos_by_dial = {}, defaultdict(list)
    quest_by_fid, achr_script, npc_name = {}, {}, {}
    dial_quest = {}
    for r in recs:
        if r.type == 'DIAL':
            dial_name[r.form_id] = (_z(_get(r, 'EDID')) or '').lower()
            qn = _get(r, 'QNAM')
            if qn and len(qn.data) >= 4:
                dial_quest[r.form_id] = struct.unpack('<I', qn.data[:4])[0]
        elif r.type == 'INFO':
            infos_by_dial[r.parent_dial].append(r)
        elif r.type == 'QUST':
            quest_by_fid[r.form_id] = (_z(_get(r, 'EDID')) or '').lower()
        elif r.type == 'ACHR':
            achr_script[r.form_id] = _z(_get(r, 'EDID')) or ''
        elif r.type == 'NPC_':
            npc_name[r.form_id] = _z(_get(r, 'EDID')) or ''

    # Topics owned by the quest under test (DIAL.QNAM -> quest).
    quest_fid = next((f for f, n in quest_by_fid.items()
                      if n == quest_edid.lower()), None)
    quest_topic_fids = {f for f, q in dial_quest.items() if q == quest_fid}

    # --- script instances -------------------------------------------------
    def instance(script_name, owner=None):
        key = script_name.lower()
        if key in eng.scripts:
            return eng.scripts[key]
        src = sources.get(key)
        if src is None:
            return None
        inst = ScriptInstance(script_name, src, owner=owner)
        eng.scripts[key] = inst
        return inst

    # Only the scripts this quest's conversation can touch.  Instantiating all
    # ~11,600 converted scripts (and ticking every one that has an OnUpdate)
    # turns a 180s simulation into millions of interpreted events; the quest's
    # own script, its QF fragments, and the actors that Say its topics are the
    # closed set that actually matters.
    quest_topics = {dial_name[f].lower() for f in quest_topic_fids
                    if dial_name.get(f)}
    relevant = set(_relevant_scripts(sources, quest_edid, dial_name,
                                     infos_by_dial, quest_topics))
    for key in relevant:
        instance(key)

    # quest scripts own their quest name so GetStage resolves
    for fid, qname in quest_by_fid.items():
        for cand in (f'tes4_{qname}script', f'tes4_{qname}'):
            inst = eng.scripts.get(cand)
            if inst:
                inst.owner = qname
    # the CharGenQuest-style case: script EditorID differs from quest EditorID
    for key, inst in eng.scripts.items():
        if inst.owner:
            continue
        m = re.match(r'^tes4_(.*)quest$', key)
        if m and m.group(1) in eng.quests:
            inst.owner = m.group(1)

    # --- topics -----------------------------------------------------------
    durations = durations or {}
    for dial_fid, infos in infos_by_dial.items():
        tname = dial_name.get(dial_fid)
        # Only this quest's topics: building every topic in the game also
        # instantiates every TIF fragment script (~5,600 of them).
        if not tname or dial_fid not in quest_topic_fids:
            continue
        models = []
        for r in infos:
            text = None
            for s in r.subrecords:
                if s.type == 'NAM1':
                    text = _z(s)
                    break
            frag_name = info_fragment_script(r)
            frag_inst = instance(frag_name) if frag_name else None
            fid_hex = '%08X' % r.form_id
            secs = durations.get('info:%s' % fid_hex[2:], 0.0) or 2.0
            models.append(InfoModel(fid_hex, text or '', secs,
                                    parse_ctdas(r), frag_inst))
        eng.topics[tname] = TopicModel(tname, models)

    # --- condition evaluation state --------------------------------------
    # GetStage/GetStageDone name a QUEST by FormID; GetIsID names an actor
    # BASE form; GetVMQuestVariable/GetVMScriptVariable name the object whose
    # script holds the variable.
    eng.owner_of_fid = {}
    by_owner = {}
    for inst in eng.scripts.values():
        if inst.owner:
            by_owner.setdefault(inst.owner, inst)
    for fid, qname in quest_by_fid.items():
        inst = by_owner.get(qname)
        if inst is not None:
            eng.owner_of_fid[fid] = inst
    for fid, edid in achr_script.items():
        inst = eng.scripts.get(('tes4_%s' % edid).lower())
        if inst is None:
            # ACHR EditorID is 'ValenDrethRef'; its script is ValenDrethScript
            base = edid[:-3] if edid.lower().endswith('ref') else edid
            inst = eng.scripts.get(('tes4_%sscript' % base).lower())
        if inst is not None:
            eng.owner_of_fid[fid] = inst

    from tools.papyrus_emulator import make_cond_eval
    speaker_base = {}
    for fid, name in npc_name.items():
        for cand in ('tes4_%sscript' % name, 'tes4_%s' % name):
            if cand.lower() in eng.scripts:
                speaker_base[fid] = eng.scripts[cand.lower()].name
                break
    eng.cond_eval = make_cond_eval(eng, quest_by_fid, speaker_base)

    # --- quest stage fragments -------------------------------------------
    qf = instance('tes4_qf_%s' % quest_edid.lower())
    if qf:
        qf.owner = quest_edid.lower()
        for ev in qf.events:
            m = re.match(r'^fragment_stage_(\d+)_item_(\d+)$', ev)
            if m:
                stage = int(m.group(1))
                eng.stage_fragments[(quest_edid.lower(), stage)].append(
                    _bind(eng, qf, ev))
    return eng


def _bind(eng, inst, event):
    def run():
        eng.interp.run_event(inst, event)
    return run
