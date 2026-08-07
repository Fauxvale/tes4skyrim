"""Restore Oblivion's engine-scheduled NPC-to-NPC conversation chains.

Oblivion has an ambient conversation scheduler Skyrim lacks: when two NPCs
idle near each other, the engine may start a conversation — the initiator
speaks a HELLO line whose `GetIsID(<npc>)[Target]` condition names the other
actor, then the engine walks the line's Choice (TCLT) links, alternating
speakers per each INFO's NextSpeaker, until a GOODBYE ends it.  Quest authors
lean on this: CharacterGen stage 26→27 IS such a chain (Baurus "Are you all
right, sire?" → Emperor "Captain Renault?" → Baurus GOODBYE "She's dead..."
whose result runs `setstage charactergen 27`), and so are conversations in
MQ13, MQ15, MQ16 (the endgame), TG01, TG03 and MS91.  Skyrim never evaluates
a HELO topic against anything but the player and has no chain-walking
scheduler, so converted as-is every one of these chains is silent and the
quests stall.

Skyrim's native vessel would be a SCEN scene, but scenes need quest-alias
plumbing per conversation and are unverifiable without many in-game cycles
(docs/ambient_dialogue_channel_plan.md Step 4 — deferred).  These chains,
however, are IDENTITY-PINNED: the head names both actors via GetIsID, so the
proven `Actor.Say()` machinery (which drives every scripted CharGen
conversation already) can replay them from a generated driver script:

  * the head INFO is re-homed onto a synthesized, hidden, quest-owned CUST
    topic (the same shape as the script-driven CharGenVoice — Say() reaches
    it, the menu never shows it), and
  * a generated `TES4NPCConv<plugin>` start-game-enabled quest polls the
    chain's gate (converted from the head's own conditions) plus the same
    guards Oblivion's scheduler applies (both actors loaded, alive, near,
    not fighting), then Says the topic sequence with measured waits.  Each
    line's INFO is picked by the engine from its own converted conditions —
    exactly how Oblivion picked the line within a Choice-linked topic — so
    per-line gating keeps full CTDA fidelity, and INFO End fragments (the
    setstage payloads) fire exactly as they do for every other Say().

Only QUEST-ADVANCING chains are restored (a chain whose closure carries a
setstage/startquest/quest-variable result).  Pure flavor chatter stays
dropped per the "better absent than wrong" decision in
docs/ambient_dialogue_channel_plan.md — restoring it wholesale is still
TODO.txt Later-Issues #16.

This module is the SHARED analysis both stages must agree on (the
message_menus / dialog_unlocks mirroring contract): tes5_import builds the
head topics + driver QUST/VMAD from the plan, script_convert generates the
matching .psc from the same plan.  Any divergence leaves VMAD properties
unbound, which the generated script guards against but cannot repair.
"""
import re
import struct

# TES4 CTDA layout constants (24-byte raw conditions from the export dump).
CTDA_OR = 0x01
CTDA_RUN_ON_TARGET = 0x02
_COMPARISON_MASK = 0xE0
_OP_BY_NIBBLE = {0x00: '==', 0x20: '!=', 0x40: '>',
                 0x60: '>=', 0x80: '<', 0xA0: '<='}

FUNC_GET_IS_ID = 72
FUNC_GET_STAGE = 58
FUNC_GET_QUEST_VARIABLE = 79
FUNC_GET_ITEM_COUNT = 47          # TES4 index (xEdit wbDefinitionsTES4)

_PLAYER_FIDS = {0x00000014, 0x00000007}

# Result-script content that makes a chain quest-advancing.  `set X.y to`
# counts: quest scripts poll their own variables to advance (MQ13.convDone,
# TG03Elven.TrackConversation).
_QUEST_ADVANCING_RE = re.compile(
    r'setstage|startquest|stopquest|\bset\s+\w+\.\w+\s+to\b', re.I)

_MAX_HOPS = 24


def _raw_fid(rec) -> int:
    try:
        return int(rec.get('FormID', '0') or '0', 16)
    except ValueError:
        return 0


def _conds(rec, prefix: str = ''):
    """Parsed TES4 conditions: (type_byte, comp_value, func, param1, param2)."""
    out, i = [], 0
    while True:
        raw_hex = rec.get(f'{prefix}Condition[{i}].Raw')
        if raw_hex is None:
            break
        i += 1
        try:
            raw = bytes.fromhex(raw_hex or '')
        except ValueError:
            continue
        if len(raw) < 20:
            continue
        raw = raw + b'\x00' * max(0, 24 - len(raw))
        out.append((raw[0],
                    struct.unpack_from('<f', raw, 4)[0],
                    struct.unpack_from('<H', raw, 8)[0],
                    struct.unpack_from('<I', raw, 12)[0],
                    struct.unpack_from('<I', raw, 16)[0]))
    return out


def _choices(rec):
    out, i = [], 0
    while True:
        v = rec.get(f'Choice[{i}]')
        if v is None:
            break
        i += 1
        try:
            out.append(int(v, 16))
        except ValueError:
            pass
    return out


def _is_positive_id(t, v):
    """A GetIsID that ASSERTS identity (== 1), not an exclusion (== 0)."""
    return (t & _COMPARISON_MASK) == 0x00 and v >= 0.5


def _subject_ids(conds):
    return [p1 for (t, v, f, p1, _p2) in conds
            if f == FUNC_GET_IS_ID and not (t & CTDA_RUN_ON_TARGET)
            and _is_positive_id(t, v)]


def _target_ids(conds):
    return [p1 for (t, v, f, p1, _p2) in conds
            if f == FUNC_GET_IS_ID and (t & CTDA_RUN_ON_TARGET)
            and _is_positive_id(t, v)]


def head_is_npc_addressed(rec) -> bool:
    """True for a HELLO INFO whose every target-side identity names an NPC."""
    tgt = _target_ids(_conds(rec))
    return bool(tgt) and all((p & 0xFFFFFF) not in _PLAYER_FIDS
                             and (p & 0xFFFFFF) != 0 for p in tgt)


def _build_gates(head_conds, quest_edid_by_fid, script_vars,
                 scpt_edid_by_qfid):
    """Compile the head's non-identity conditions into driver-poll gates.

    Returns (gates, None) or (None, reason) when a condition falls outside the
    supported set — the chain is then skipped rather than restored with a
    trigger looser than Oblivion's (a conversation firing EARLY is worse than
    one that stays absent).
    """
    gates = []
    for (t, comp, func, p1, p2) in head_conds:
        if func == FUNC_GET_IS_ID:
            if not _is_positive_id(t, comp) and not (t & CTDA_RUN_ON_TARGET):
                # "not spoken by X" exclusion: the pinned speaker either IS X
                # (chain impossible) or is not (gate vacuous).  The pinned
                # positive identity already decides which, so drop it.
                continue
            continue                      # consumed as speaker/listener identity
        if t & CTDA_OR:
            return None, f'OR-chained gate (func {func})'
        if t & CTDA_RUN_ON_TARGET:
            return None, f'run-on-target gate (func {func})'
        op = _OP_BY_NIBBLE.get(t & _COMPARISON_MASK)
        if op is None:
            return None, f'unknown comparison 0x{t:02X}'
        if func == FUNC_GET_STAGE:
            qedid = quest_edid_by_fid.get(p1 & 0xFFFFFF)
            if not qedid:
                return None, f'GetStage on unknown quest {p1:08X}'
            gates.append({'kind': 'stage', 'quest_fid': p1,
                          'quest_edid': qedid, 'op': op,
                          'value': int(round(comp))})
        elif func == FUNC_GET_QUEST_VARIABLE:
            name = (script_vars or {}).get(p1 & 0xFFFFFF, {}).get(p2)
            qedid = quest_edid_by_fid.get(p1 & 0xFFFFFF)
            # The var lives as a property on the CONVERTED QUEST SCRIPT, whose
            # Papyrus name comes from the SCPT EditorID (papyrus_script_name),
            # not from the quest's.
            sedid = scpt_edid_by_qfid.get(p1 & 0xFFFFFF)
            if not name or not qedid or not sedid:
                return None, f'GetQuestVariable {p1:08X}[{p2}] unresolvable'
            gates.append({'kind': 'var', 'quest_fid': p1,
                          'quest_edid': qedid, 'script_edid': sedid,
                          'var': name, 'op': op, 'value': comp})
        elif func == FUNC_GET_ITEM_COUNT:
            # Evaluated against the SPEAKER, same as Oblivion ran it against
            # the conversation initiator (TG01: Methredhel no longer holds
            # the stolen diary).
            gates.append({'kind': 'itemcount', 'item_fid': p1, 'op': op,
                          'value': int(round(comp))})
        else:
            return None, f'unsupported gate func {func}'
    return gates, None


def _stage_compatible(cand_conds, stage_eq):
    """Can this INFO pass while the head's GetStage==N gates hold?"""
    for (t, comp, func, p1, _p2) in cand_conds:
        if func != FUNC_GET_STAGE:
            continue
        if t & CTDA_OR:            # OR-chained: can't statically bound; accept
            continue
        head_val = stage_eq.get(p1 & 0xFFFFFF)
        if head_val is None:
            continue
        op = _OP_BY_NIBBLE.get(t & _COMPARISON_MASK)
        v = comp
        ok = {'==': head_val == v, '!=': head_val != v,
              '>': head_val > v, '>=': head_val >= v,
              '<': head_val < v, '<=': head_val <= v}.get(op, True)
        if not ok:
            return False
    return True


def build_conversation_plan(by_type: dict, script_vars: dict = None,
                            plugin_stem: str = '', log=None) -> dict:
    """Detect and linearize the quest-advancing NPC-to-NPC HELLO chains.

    Deterministic pure analysis over export records — MUST produce identical
    output in the import and script pipelines (the VMAD/psc mirroring
    contract).  `script_vars` is pack_aliases.build_script_var_map's low-24
    fid -> {index: name} table, used to resolve GetQuestVariable gates.
    """
    log = log or (lambda *_a, **_k: None)
    stem = re.sub(r'\W', '', plugin_stem or '')
    plan = {'quest_edid': f'TES4NPCConv{stem}',
            'script_name': f'TES4NPCConv{stem}',
            'chains': [], 'skipped': []}

    dials = by_type.get('DIAL', [])
    infos = by_type.get('INFO', [])
    if not dials or not infos:
        return plan

    dial_by_fid = {_raw_fid(d): d for d in dials}
    quest_edid_by_fid = {_raw_fid(q) & 0xFFFFFF: q.get('EditorID', '')
                         for q in by_type.get('QUST', [])
                         if q.get('EditorID')}
    scpt_edid_by_fid = {_raw_fid(s) & 0xFFFFFF: s.get('EditorID', '')
                        for s in by_type.get('SCPT', [])
                        if s.get('EditorID')}
    scpt_edid_by_qfid = {}
    for q in by_type.get('QUST', []):
        scri = q.get('SCRI', '')
        if not scri:
            continue
        try:
            sedid = scpt_edid_by_fid.get(int(scri, 16) & 0xFFFFFF)
        except ValueError:
            continue
        if sedid:
            scpt_edid_by_qfid[_raw_fid(q) & 0xFFFFFF] = sedid
    hello_fid = next((_raw_fid(d) for d in dials
                      if d.get('EditorID') == 'HELLO'), 0)
    if not hello_fid:
        return plan

    info_by_dial = {}
    for inf in infos:
        try:
            p = int(inf.get('ParentDIAL', '0') or '0', 16)
        except ValueError:
            continue
        info_by_dial.setdefault(p, []).append(inf)

    # base low-24 -> unique named persistent placed ref EditorID (raw fid).
    ref_by_base = {}
    for sig in ('ACHR', 'ACRE'):
        for a in by_type.get(sig, []):
            edid = a.get('EditorID')
            if not edid:
                continue
            try:
                base = int(a.get('NAME', '0') or '0', 16) & 0xFFFFFF
            except ValueError:
                continue
            ref_by_base.setdefault(base, []).append((edid, _raw_fid(a)))

    # The say-driven topic set, so a chain never routes through a topic the
    # NPC-to-NPC drop removes (Say on a dropped topic plays nothing).  Built
    # here rather than taken from the caller so both pipelines agree.
    from .dialog_converter import (build_say_topic_dispositions,
                                   _CONV_KEEP_EDIDS, DIAL_TYPE_CONVERSATION)
    say_driven = set(build_say_topic_dispositions(by_type).keys())

    def _topic_dropped(dial_fid: int) -> bool:
        d = dial_by_fid.get(dial_fid)
        if d is None:
            return True                       # unknown topic — treat as gone
        try:
            dtype = int(d.get('DATA.Type', '0') or '0')
        except ValueError:
            dtype = 0
        if dtype != DIAL_TYPE_CONVERSATION:
            return False
        if d.get('EditorID', '') in _CONV_KEEP_EDIDS:
            return False
        return (dial_fid & 0xFFFFFF) not in say_driven

    def _unique_ref(base_fid: int):
        refs = ref_by_base.get(base_fid & 0xFFFFFF, [])
        return refs[0] if len(refs) == 1 else None

    n = 0
    for head in info_by_dial.get(hello_fid, []):
        if not head_is_npc_addressed(head):
            continue
        head_fid = _raw_fid(head)
        head_conds = _conds(head)
        quest_raw = head.get('QSTI.Quest', '')
        try:
            quest_fid = int(quest_raw or '0', 16)
        except ValueError:
            quest_fid = 0

        def _skip(reason):
            plan['skipped'].append((head_fid, reason))
            log(f'    npc-conversation skip {head_fid:08X}: {reason}')

        if not quest_fid:
            _skip('no owning quest')
            continue

        # --- quest-advancing? (same-quest TCLT closure) ---
        seen_topics, frontier, closure = set(), _choices(head), []
        while frontier:
            t = frontier.pop(0)
            if t in seen_topics:
                continue
            seen_topics.add(t)
            for inf in info_by_dial.get(t, []):
                if inf.get('QSTI.Quest', '') != quest_raw:
                    continue
                closure.append(inf)
                frontier.extend(_choices(inf))
        if not (_QUEST_ADVANCING_RE.search(head.get('ResultScript', ''))
                or any(_QUEST_ADVANCING_RE.search(i.get('ResultScript', ''))
                       for i in closure)):
            continue                          # flavor chatter: stays dropped

        # --- actors ---
        subj = _subject_ids(head_conds)
        tgt = _target_ids(head_conds)
        if len(set(subj)) != 1:
            _skip(f'{len(set(subj))} subject identities')
            continue
        a_base, b_base = subj[0], tgt[0]
        a_ref, b_ref = _unique_ref(a_base), _unique_ref(b_base)
        if not a_ref or not b_ref:
            _skip('no unique placed ref for a participant')
            continue

        # --- gate ---
        gates, why = _build_gates(head_conds, quest_edid_by_fid, script_vars,
                                  scpt_edid_by_qfid)
        if gates is None:
            _skip(why)
            continue
        stage_eq = {g['quest_fid'] & 0xFFFFFF: g['value']
                    for g in gates if g['kind'] == 'stage' and g['op'] == '=='}

        # --- linearize the chain ---
        # Runtime line selection stays with the engine (each Say picks the
        # first INFO whose converted conditions pass, Oblivion's own rule);
        # this walk only fixes the TOPIC sequence, each hop's speaker and its
        # expected line length.
        hops = []
        undrop = []
        cur = head
        cur_speaker = 'A'
        bases = {'A': a_base & 0xFFFFFF, 'B': b_base & 0xFFFFFF}
        visited = {id(head)}
        while len(hops) < _MAX_HOPS:
            nxt_info = None
            nxt_topic = 0
            # NextSpeaker: 0 Target (the other), 1 Self, 2 Either -> other.
            try:
                ns = int(cur.get('DATA.NextSpeaker', '0') or '0')
            except ValueError:
                ns = 0
            expected = cur_speaker if ns == 1 else \
                ('B' if cur_speaker == 'A' else 'A')
            for t in _choices(cur):
                for inf in info_by_dial.get(t, []):
                    if id(inf) in visited:
                        continue
                    if inf.get('QSTI.Quest', '') != quest_raw:
                        continue
                    ic = _conds(inf)
                    if not _stage_compatible(ic, stage_eq):
                        continue
                    isubj = {p & 0xFFFFFF for p in _subject_ids(ic)}
                    if isubj:
                        if bases[expected] in isubj:
                            speaker = expected
                        elif bases[cur_speaker] in isubj and ns != 0:
                            speaker = cur_speaker
                        else:
                            continue      # a third party / other branch
                    else:
                        speaker = expected
                    nxt_info, nxt_topic, cur_speaker = inf, t, speaker
                    break
                if nxt_info is not None:
                    break
            if nxt_info is None:
                break
            if nxt_topic not in dial_by_fid:
                _skip(f'chain routes through unknown topic {nxt_topic:08X}')
                hops = None
                break
            if _topic_dropped(nxt_topic):
                # The hop rides a topic the NPC-to-NPC drop would remove.
                # Since this chain now has a driver, the topic must survive:
                # the import pass registers it say-driven (kept, hidden
                # branch, target-conditions retargeted at the listener) —
                # the same treatment CharGenVoice already gets.
                undrop.append(nxt_topic)
            visited.add(id(nxt_info))
            hops.append({'topic_fid': nxt_topic,
                         'topic_edid': dial_by_fid.get(nxt_topic, {})
                                       .get('EditorID', ''),
                         'speaker': cur_speaker,
                         'info_fid': _raw_fid(nxt_info)})
            cur = nxt_info
        if hops is None:
            continue

        chain = {
            'index': n,
            'head_fid': head_fid,
            'head_topic_edid': f'{plan["quest_edid"]}Topic{n}',
            'owner_quest_fid': quest_fid,
            'owner_quest_edid': quest_edid_by_fid.get(quest_fid & 0xFFFFFF, ''),
            'subj': {'base': a_base, 'ref_edid': a_ref[0], 'ref_fid': a_ref[1]},
            'tgt': {'base': b_base, 'ref_edid': b_ref[0], 'ref_fid': b_ref[1]},
            'gates': gates,
            'hops': hops,
            'undrop_topic_fids': list(dict.fromkeys(undrop)),
        }
        plan['chains'].append(chain)
        n += 1

    # --- Post-pass 1: hops that ride the shared HELLO topic. ---
    # The raw HELLO DIAL does not survive conversion as one record (the bark
    # pass splits it per quest), so such a hop must point at the restored
    # chain whose reparented head IS that INFO.  A hello-hop with no restored
    # head has nowhere to live — drop the chain.
    head_chain_by_fid = {c['head_fid']: c['index'] for c in plan['chains']}
    kept = []
    for c in plan['chains']:
        ok = True
        for hop in c['hops']:
            if hop['topic_edid'] == 'HELLO':
                target = head_chain_by_fid.get(hop['info_fid'])
                if target is None:
                    plan['skipped'].append(
                        (c['head_fid'], 'hop rides an unrestored HELLO INFO'))
                    ok = False
                    break
                hop['head_chain'] = target
        if ok:
            kept.append(c)
    # Renumber (indices name properties; they must be gapless and identical
    # across both pipelines).
    old_to_new = {}
    for new_i, c in enumerate(kept):
        old_to_new[c['index']] = new_i
        c['index'] = new_i
        c['head_topic_edid'] = f'{plan["quest_edid"]}Topic{new_i}'
    for c in kept:
        for hop in c['hops']:
            if 'head_chain' in hop:
                hop['head_chain'] = old_to_new[hop['head_chain']]
    plan['chains'] = kept

    # --- Post-pass 2: overlapping chains are mutually exclusive. ---
    # Two heads can open the SAME authored conversation (MS91: Weebam-Na's
    # "You want to speak to me?" and Mazoga's "You are Weebam-Na?" both walk
    # the MazogaTalk lines).  Oblivion's scheduler ran whichever fired first,
    # once; whichever of ours runs must retire the other or the whole talk
    # replays.
    for c in plan['chains']:
        c['_info_set'] = ({h['info_fid'] for h in c['hops']}
                          | {c['head_fid']})
    for c in plan['chains']:
        c['exclusive_with'] = sorted(
            d['index'] for d in plan['chains']
            if d is not c and (d['_info_set'] & c['_info_set']))
    for c in plan['chains']:
        del c['_info_set']
    return plan


# ---------------------------------------------------------------------------
# Generated driver script (script_convert side of the mirroring contract)
# ---------------------------------------------------------------------------

# Oblivion starts ambient conversations only between actors close together;
# fAIMaxSocialDistance-scale.  Also the poll cadence for the driver.
_CONVERSE_DISTANCE = 500.0
_POLL_SECONDS = 4.0
_FALLBACK_LINE_SECONDS = 4.0
_LINE_BEAT = 0.6            # breath between lines, like Oblivion's scheduler


def chain_property_bindings(chain, remap, resolve_hop_topic):
    """(property_name, output fid) pairs for one chain's VMAD.

    `remap` maps a RAW TES4 FormID to the output plugin space.
    `resolve_hop_topic(hop)` returns the OUTPUT DIAL FormID a hop's Say must
    target — the caller owns the mapping because bark topics (GOODBYE) are
    regrouped per quest and hello-hops point at another chain's synthesized
    head topic.  The chain's own head topic is bound separately.
    A resolver returning 0 leaves the property unbound; the generated script
    guards every topic against None and skips the chain.
    """
    i = chain['index']
    props = {f'Conv{i}A': remap(chain['subj']['ref_fid']),
             f'Conv{i}B': remap(chain['tgt']['ref_fid'])}
    for k, hop in enumerate(chain['hops']):
        fid = resolve_hop_topic(hop)
        if fid:
            props[f'Conv{i}T{k + 1}'] = fid
    seen_q = []
    n_item = n_var = 0
    for g in chain['gates']:
        if g['kind'] == 'itemcount':
            props[f'Conv{i}I{n_item}'] = remap(g['item_fid'])
            n_item += 1
        elif g['kind'] == 'var':
            # Script-typed property; the VMAD object is still the QUEST record
            # (the VM resolves the attached script instance from it).
            props[f'Conv{i}V{n_var}'] = remap(g['quest_fid'])
            n_var += 1
        elif g['quest_fid'] not in seen_q:
            seen_q.append(g['quest_fid'])
    for j, qfid in enumerate(seen_q):
        props[f'Conv{i}Q{j}'] = remap(qfid)
    return props


def _gate_exprs(chain):
    """Papyrus boolean terms for the chain's gates + the property decls."""
    i = chain['index']
    decls, terms = [], []
    seen_q = []
    for g in chain['gates']:
        if g['kind'] == 'stage' and g['quest_fid'] not in seen_q:
            seen_q.append(g['quest_fid'])
    qprop = {qfid: f'Conv{i}Q{j}' for j, qfid in enumerate(seen_q)}
    from script_convert.constants import (_safe_property_name,
                                          papyrus_script_name)
    declared = set()
    n_item = n_var = 0
    guard = []
    for g in chain['gates']:
        if g['kind'] == 'itemcount':
            p = f'Conv{i}I{n_item}'
            n_item += 1
            decls.append(f'Form Property {p} Auto')
            guard.append(p)
            terms.append(f'Conv{i}A.GetItemCount({p}) {g["op"]} {g["value"]}')
        elif g['kind'] == 'stage':
            p = qprop[g['quest_fid']]
            if p not in declared:
                decls.append(f'Quest Property {p} Auto')
                declared.add(p)
                guard.append(p)
            terms.append(f'{p}.GetStage() {g["op"]} {g["value"]}')
        else:                                   # quest-variable gate
            p = f'Conv{i}V{n_var}'
            n_var += 1
            sname = papyrus_script_name(g['script_edid'])
            decls.append(f'{sname} Property {p} Auto')
            guard.append(p)
            var = _safe_property_name(g['var'])
            val = g['value']
            vtxt = str(int(val)) if float(val).is_integer() else f'{val}'
            terms.append(f'{p}.{var} {g["op"]} {vtxt}')
    for p in reversed(guard):
        # A None property (binding divergence) must disable the chain, not
        # abort the whole poll function.
        terms.insert(0, f'{p} != None')
    return decls, terms


def generate_driver_psc(plan, say_durations: dict = None) -> str:
    """The full TES4NPCConv<plugin>.psc source, or '' when no chains."""
    if not plan['chains']:
        return ''
    say_durations = say_durations or {}

    def _wait(hop_or_head, topic_edid):
        if hop_or_head is not None:
            d = say_durations.get(f'info:{hop_or_head:08X}')
            if d:
                return min(float(d), 15.0) + _LINE_BEAT
        d = say_durations.get((topic_edid or '').lower())
        if d:
            return min(float(d), 10.0) + _LINE_BEAT
        return _FALLBACK_LINE_SECONDS + _LINE_BEAT

    lines = [
        f'ScriptName {plan["script_name"]} extends Quest',
        '{Drives Oblivion engine-scheduled NPC-to-NPC conversations.',
        ' Generated by tes5_import.npc_conversations - do not edit.}',
        '',
    ]
    decls, bodies = [], []
    for chain in plan['chains']:
        i = chain['index']
        decls += [f'Actor Property Conv{i}A Auto',
                  f'Actor Property Conv{i}B Auto',
                  f'Topic Property Conv{i}T0 Auto']
        decls += [f'Topic Property Conv{i}T{k + 1} Auto'
                  for k in range(len(chain['hops']))]
        gdecls, terms = _gate_exprs(chain)
        decls += gdecls
        decls.append(f'Bool _done{i} = False')
        topic_guards = [f'Conv{i}T{k} != None'
                        for k in range(len(chain['hops']) + 1)]
        cond = ' && '.join([f'!_done{i}'] + topic_guards + terms
                           + [f'CanConverse(Conv{i}A, Conv{i}B)'])
        done_sets = [f'        _done{i} = True']
        done_sets += [f'        _done{j} = True'
                      for j in chain.get('exclusive_with', ())]
        body = [f'    ; {chain["owner_quest_edid"]}: head INFO '
                f'{chain["head_fid"]:08X}',
                f'    if {cond}']
        body += done_sets
        body.append(f'        Conv{i}A.Say(Conv{i}T0)')
        head_wait = _wait(chain['head_fid'], 'HELLO')
        body.append(f'        Utility.Wait({head_wait:.2f})')
        for k, hop in enumerate(chain['hops']):
            spk = 'A' if hop['speaker'] == 'A' else 'B'
            body.append(f'        Conv{i}{spk}.Say(Conv{i}T{k + 1})')
            if k + 1 < len(chain['hops']):
                body.append(f'        Utility.Wait('
                            f'{_wait(hop["info_fid"], hop["topic_edid"]):.2f})')
        body.append('    endif')
        bodies.append('\n'.join(body))

    lines += decls
    lines += [
        '',
        'Event OnInit()',
        f'    RegisterForSingleUpdate({_POLL_SECONDS})',
        'EndEvent',
        '',
        'Event OnUpdate()',
        '    CheckConversations()',
        f'    RegisterForSingleUpdate({_POLL_SECONDS})',
        'EndEvent',
        '',
        'Function CheckConversations()',
    ]
    lines.append('\n'.join(bodies))
    lines += [
        'EndFunction',
        '',
        'Bool Function CanConverse(Actor akA, Actor akB)',
        '    if akA == None || akB == None',
        '        return False',
        '    endif',
        '    if !akA.Is3DLoaded() || !akB.Is3DLoaded()',
        '        return False',
        '    endif',
        '    if akA.IsDead() || akB.IsDead()',
        '        return False',
        '    endif',
        '    if akA.IsInCombat() || akB.IsInCombat()',
        '        return False',
        '    endif',
        f'    return akA.GetDistance(akB) < {_CONVERSE_DISTANCE}',
        'EndFunction',
        '',
    ]
    return '\n'.join(lines)
