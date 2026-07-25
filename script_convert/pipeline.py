"""Pipeline orchestration — convert all scripts, VMAD helpers, CLI."""

import argparse
import os
import re
import struct

from tes5_import.text_reader import parse_export_file
from worker_budget import worker_count

from script_convert.constants import (_PAPYRUS_RESERVED, _RECORD_TYPE_PAPYRUS, _GLOBAL_CANONICAL,
                                     _sanitize_name, _safe_property_name, _canonical_global,
                                     _record_type_to_papyrus, papyrus_script_name,
                                     PAPYRUS_MAX_SCRIPT_NAME)
from script_convert.cross_ref import CrossRefGraph
from script_convert.converter import (ScriptConverter, SAY_LINE_PARK_SECONDS,
                                      SAY_TIMER_PARKED_THRESHOLD)


# ===========================================================================
# Process-pool plumbing
#
# Script conversion is pure-Python CPU work (ScriptConverter holds the GIL),
# so batches run across a ProcessPoolExecutor. The read-only CrossRefGraph and
# plan dicts are shipped once per worker via the pool initializer; each job is
# a (kind, records) chunk whose .psc files the worker writes directly.
# ===========================================================================

_WORKER_CTX: dict = {}


def _new_stats() -> dict:
    return {
        'scpt_total': 0, 'scpt_ok': 0, 'scpt_err': 0,
        'info_total': 0, 'info_ok': 0, 'info_err': 0,
        'qust_total': 0, 'qust_ok': 0, 'qust_err': 0,
        'todo_count': 0, 'errors': [],
    }


def _script_worker_init(xref, output_dir, info_reveals, service_topics,
                        stage_reveals, say_durations=None,
                        say_timer_owners=None, topic_by_dial=None,
                        beat_fields_by_owner=None):
    _WORKER_CTX.update(xref=xref, output_dir=output_dir,
                       info_reveals=info_reveals,
                       service_topics=service_topics,
                       stage_reveals=stage_reveals,
                       say_timer_owners=say_timer_owners or {},
                       topic_by_dial=topic_by_dial or {})
    # Class-level, so every ScriptConverter a worker builds sees the measured
    # voice-line lengths that converted Say() timers are charged with.
    ScriptConverter.say_durations = say_durations or {}
    ScriptConverter.beat_fields_by_owner = beat_fields_by_owner or {}
    # The call site sizes its park from the SAME per-topic beat the End fragment
    # tests against, so the two thresholds cannot drift (see say_beat_threshold).
    ScriptConverter.say_timer_owners = say_timer_owners or {}


def _script_worker_run(job):
    kind, records = job
    ctx = _WORKER_CTX
    stats = _new_stats()
    if kind == 'scpt':
        _scpt_batch(records, ctx['output_dir'], ctx['xref'], stats)
    elif kind == 'info':
        _info_batch(records, ctx['output_dir'], ctx['xref'], stats,
                    ctx['info_reveals'], ctx['service_topics'])
    elif kind == 'qust':
        _qust_batch(records, ctx['output_dir'], ctx['xref'], stats,
                    ctx['stage_reveals'])
    return stats


def _merge_stats(into: dict, part: dict):
    for k, v in part.items():
        if k == 'errors':
            into['errors'].extend(v)
        else:
            into[k] += v


def _chunk(records: list, size: int):
    return [records[i:i + size] for i in range(0, len(records), size)]


# ===========================================================================
# High-level conversion functions
# ===========================================================================

def convert_all_scripts(export_dir: str, output_dir: str, workers: int = None) -> dict:
    """Convert all TES4 scripts from export directory to Papyrus .psc files.

    Args:
        export_dir: Path to export/Oblivion.esm (contains .txt files)
        output_dir: Path to write .psc files
        workers: Number of worker threads (default: cpu_count-1)

    Returns dict with conversion statistics.
    """
    if workers is None:
        workers = worker_count()

    os.makedirs(output_dir, exist_ok=True)

    # Deploy static scripts (TES4Polyfill + shared service-menu fragments) so
    # they compile alongside the generated ones.
    static_dir = os.path.join(os.path.dirname(__file__), 'static_scripts')
    if os.path.isdir(static_dir):
        import shutil
        for name in os.listdir(static_dir):
            if name.endswith('.psc'):
                shutil.copy2(os.path.join(static_dir, name),
                             os.path.join(output_dir, name))

    # Phase 1: Build cross-reference graph
    print('  Building cross-reference graph...')
    xref = CrossRefGraph()
    xref.load_from_export(export_dir)
    print(f'    {len(xref.formid_to_edid)} FormID->EditorID mappings')
    print(f'    {len(xref.script_formid_to_edid)} scripts, {len(xref.quest_edids)} quests')

    # Phase 1.5: Analyze cross-script ref-as-int patterns
    scpt_path = os.path.join(export_dir, 'SCPT.txt')
    if os.path.exists(scpt_path):
        xref.build_ref_as_int_map(scpt_path)
        if xref.ref_as_int:
            print(f'    {len(xref.ref_as_int)} ref variables detected as integer-only (cross-script)')

    # Phase 1.6: AddTopic unlock plan — MUST be the same analysis the importer
    # runs, so the SetValue lines in the generated fragments match the VMAD
    # property bindings and GLOB records written into the ESM.
    from tes5_import.dialog_unlocks import build_unlock_plan
    by_type = {}
    for sig in ('DIAL', 'INFO', 'QUST', 'SCPT'):
        path = os.path.join(export_dir, f'{sig}.txt')
        by_type[sig] = parse_export_file(path) if os.path.exists(path) else []
    unlock_plan = build_unlock_plan(by_type)
    print(f'    AddTopic unlocks: {len(unlock_plan["gated"])} gated topics, '
          f'{len(unlock_plan["info_reveals"])} revealer INFOs')

    stats = _new_stats()

    # Service-menu topics (Barter/Training): INFOs under them whose fragment
    # is generated here must ALSO open the Skyrim menu — the importer attaches
    # the shared static script only to INFOs WITHOUT their own fragment.
    from tes5_import.dialog_converter import SERVICE_MENU_TOPICS, DIAL_TYPE_SERVICE
    service_topics = {}
    for rec in by_type.get('DIAL', []):
        edid = rec.get('EditorID', '')
        if (edid in SERVICE_MENU_TOPICS
                and rec.get('DATA.Type', '') == str(DIAL_TYPE_SERVICE)):
            service_topics[rec.get('FormID', '')] = SERVICE_MENU_TOPICS[edid][0]

    # Phases 2-4: convert SCPT records, INFO result scripts and QUST stage
    # scripts. Records that produce no output are filtered here so they are
    # never pickled to a worker; the remaining work is chunked into (kind,
    # records) jobs that all share one process pool.
    scpt_work = []
    scpt_path = os.path.join(export_dir, 'SCPT.txt')
    if os.path.exists(scpt_path):
        scpt_records = parse_export_file(scpt_path)
        stats['scpt_total'] = len(scpt_records)
        scpt_work = [r for r in scpt_records
                     if r.get('SCTX', '').strip()]

    info_reveals = unlock_plan['info_reveals']

    # Which conversation timer each Say-driven topic parks, and the topic name
    # behind each DIAL FormID.  Needed HERE, before the filter below, because
    # an INFO under a parked topic must produce a fragment even when it has no
    # result script of its own.
    say_timer_owners = build_say_timer_owners(by_type)
    topic_by_dial = {d.get('FormID', ''): (d.get('EditorID') or '').lower()
                     for d in by_type.get('DIAL', []) if d.get('FormID')}

    def _info_makes_output(rec):
        if rec.get('ResultScript', '').strip():
            return True
        # A line under a topic whose timer was PARKED must emit a fragment to
        # release it, script or not.  Requiring a result script dropped 87% of
        # them (5,450 of 6,248): the parked timer was then never cleared and
        # the conversation stopped for good on the first script-less line —
        # CharacterGen died on Glenroy's "Baurus, lock the door behind us".
        if say_timer_owners.get(topic_by_dial.get(rec.get('ParentDIAL', ''), '')):
            return True
        try:
            return (int(rec.get('FormID', ''), 16) & 0xFFFFFF) in info_reveals
        except (TypeError, ValueError):
            return False

    info_work = [r for r in by_type.get('INFO', []) if _info_makes_output(r)]
    qust_work = [r for r in by_type.get('QUST', []) if r.get('EditorID', '')]

    jobs = ([('scpt', c) for c in _chunk(scpt_work, 48)]
            + [('info', c) for c in _chunk(info_work, 128)]
            + [('qust', c) for c in _chunk(qust_work, 8)])
    print(f'  Converting {len(scpt_work)} SCPT / {len(info_work)} INFO / '
          f'{len(qust_work)} QUST scripts ({len(jobs)} jobs)...')

    # Measured spoken-line lengths for converted Say() timers. A converted
    # polling conversation that re-Says before the previous line ends is
    # silently dropped by the engine (and loses its End fragment), so these
    # must be real durations, not a constant.
    from script_convert.say_durations import scan_voice_durations
    say_durations = scan_voice_durations(export_dir)
    if say_durations:
        print(f'    voice durations: {len(say_durations)} topics measured '
              f'(Say() timers)')

    # (say_timer_owners / topic_by_dial are built above, before the INFO filter
    # that consumes them.)
    beat_fields_by_owner = build_beat_fields_by_owner(by_type)
    if say_timer_owners:
        print(f'    say timers: {len(say_timer_owners)} topics drive a quest '
              f'conversation timer (per-line correction)')

    initargs = (xref, output_dir, info_reveals, service_topics,
                unlock_plan['stage_reveals'], say_durations,
                say_timer_owners, topic_by_dial, beat_fields_by_owner)
    if workers <= 1 or len(jobs) <= 2:
        _script_worker_init(*initargs)
        for job in jobs:
            _merge_stats(stats, _script_worker_run(job))
        _WORKER_CTX.clear()
    else:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=min(workers, len(jobs)),
                                 initializer=_script_worker_init,
                                 initargs=initargs) as ex:
            for part in ex.map(_script_worker_run, jobs):
                _merge_stats(stats, part)

    total = stats['scpt_ok'] + stats['info_ok'] + stats['qust_ok']
    errs = stats['scpt_err'] + stats['info_err'] + stats['qust_err']
    print(f'\n  Script conversion complete:')
    print(f'    SCPT: {stats["scpt_ok"]}/{stats["scpt_total"]} converted')
    print(f'    INFO: {stats["info_ok"]}/{stats["info_total"]} fragments')
    print(f'    QUST: {stats["qust_ok"]}/{stats["qust_total"]} stage scripts')
    print(f'    Total: {total} converted, {errs} errors, {stats["todo_count"]} TODOs')

    _write_report(output_dir, stats)
    return stats


def _scpt_batch(records: list, output_dir: str, xref: CrossRefGraph, stats: dict):
    """Convert a batch of SCPT records (runs in parent or worker process)."""
    for rec in records:
        formid = rec.get('FormID', '')
        edid = rec.get('EditorID', '')
        sctx = rec.get('SCTX', '')
        if not sctx or not sctx.strip():
            continue

        try:
            extends = xref.get_extends_class(formid)
            conv = ScriptConverter(xref)
            # Pre-populate external references from SCRO entries
            _preload_scro_refs(conv, rec, xref)
            name = _sanitize_name(edid or f'Script_{formid}')
            papyrus = conv.convert_standalone(name, sctx, extends, edid)

            # The FILENAME must match the ScriptName the converter emitted, or
            # the compiler cannot find the script by name.
            out_path = os.path.join(output_dir, papyrus_script_name(name) + '.psc')
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(papyrus)
            stats['scpt_ok'] += 1
            stats['todo_count'] += papyrus.count(';TODO')
        except Exception as e:
            stats['scpt_err'] += 1
            stats['errors'].append(f'SCPT {edid} ({formid}): {e}')


# Fragment lines that open the Skyrim service menus (appended to scripted
# INFOs under the Barter/Training topics; script-less ones get the shared
# static scripts of the same content instead).
_SERVICE_MENU_CALL = {
    'barter': '  (akSpeakerRef as Actor).ShowBarterMenu()',
    'training': '  Game.ShowTrainingMenu(akSpeakerRef as Actor)',
}


# `set <timer> to [<ref>.]Say <topic> [flag]`
#           or  `set <timer> to [<ref>.]SayTo <target> <topic> [flag]`
#
# The keyword decides the ARITY, so it has to be captured: SayTo takes a target
# BEFORE the topic, Say does not.  An `(?:\w+[\s,]+)?` optional-target group
# cannot tell the two apart and greedily consumed the topic of every `Say
# <topic> 1` form, capturing the trailing "say the line even if the speaker is
# not the player's target" FLAG as the topic ("1").  That silently denied a
# release owner to 120+ topics — every Daedric shrine speech, the Boethia
# champions, SE quest chatter, MQ13/MQ06 speeches — so their parked timers were
# never cleared and each conversation stopped after ONE line.
#
# `[^\S\n]` (not `\s`) keeps every match on a single line: SCTX is one long
# escaped blob, and `\s` let a match run past the end of a statement and pick up
# `else`/`endif`/`setstage` from following lines as the "topic".
_SAY_TIMER_RE = re.compile(
    r'set\s+([\w.]+)\s+to[^\S\n]+(?:[\w]+\.)?(say(?:to)?)'
    r'[^\S\n,]*[\s,][^\S\n,]*(\w+)'
    r'(?:[^\S\n,]*[,][^\S\n,]*|[^\S\n]+)(\w+)?',
    re.IGNORECASE)


def _say_timer_topic(m: 're.Match') -> str:
    """The TOPIC captured by _SAY_TIMER_RE, honouring Say vs SayTo arity.

    SayTo's first argument is the dialogue TARGET and the second is the topic;
    Say's first argument IS the topic.  A trailing numeric flag is never a
    topic, so a digit in the topic slot means the optional group matched the
    flag instead and the earlier group holds the real topic.
    """
    kw, first, second = m.group(2).lower(), m.group(3), m.group(4)
    if kw == 'sayto' and second and not second.isdigit():
        return second
    return first


def build_say_timer_owners(by_type: dict) -> dict:
    """topic (lowercase) -> the Papyrus timer expression to clear when a line
    of that topic FINISHES.

    Oblivion wrote `set CharacterGen.convTimer to SayTo player, CharGenMain 1`
    — the timer held the line's own length purely so the NEXT speaker's
    `convTimer <= 0` guard would not fire until this line finished.

    Skyrim needs no such countdown: the INFO's result script becomes its End
    fragment and the engine runs it when the line FINISHES.  Clearing the timer
    there releases the next speaker at exactly the right moment, with no
    estimation anywhere.  (Charging a measured duration instead makes the
    engine's wait and the script's wait ADD, which reads in game as a long dead
    pause after every line.)
    """
    # script EditorID (lower) -> the QUST EditorID that runs it.  A quest
    # script's bare `set convTimer to ...` names a variable on the QUEST, so
    # its End fragment must bind a quest property rather than cast the speaker.
    _script_edid = {(r.get('FormID') or '').upper(): (r.get('EditorID') or '')
                    for r in by_type.get('SCPT', [])}
    quest_script_owner = {}
    for rec in by_type.get('QUST', []):
        sname = _script_edid.get((rec.get('SCRI') or '').upper(), '')
        qname = rec.get('EditorID') or ''
        if sname and qname:
            quest_script_owner[sname.lower()] = qname

    # Every QUST EditorID, so a dotted timer target can be told apart from one
    # prefixed by a placed reference (see the `.` branch below).
    quest_edids = {(r.get('EditorID') or '').lower()
                   for r in by_type.get('QUST', []) if r.get('EditorID')}

    owners = {}
    for rec in by_type.get('SCPT', []):
        txt = (rec.get('SCTX') or '').replace('\\r\\n', '\n')
        edid = rec.get('EditorID') or ''
        for m in _SAY_TIMER_RE.finditer(txt):
            target, topic = m.group(1), _say_timer_topic(m).lower()
            # A deliberate beat (`set convTimer to convTimer + 2.5`) that the
            # script applies right after this Say. Oblivion charged it on top
            # of the line's length, so it belongs AFTER the line — the End
            # fragment applies it, because anything written at the call site
            # decays under the loop's countdown while the line plays.
            short = target.split('.')[-1]
            beat = 0.0
            bm = re.search(r'set\s+[\w.]*\b' + re.escape(short) +
                           r'\b\s+to\s+[\w.]*\b' + re.escape(short) +
                           r'\b\s*\+\s*([\d.]+)', txt, re.IGNORECASE)
            if bm:
                try:
                    beat = float(bm.group(1))
                except ValueError:
                    beat = 0.0
            if '.' in target and target.split('.')[0].lower() in quest_edids:
                # Quest-scoped (`CharacterGen.convTimer`): bind a property to
                # that quest in the fragment.
                owners.setdefault(topic, ('quest', target, beat))
            elif '.' in target:
                # A dotted target whose prefix is NOT a quest — `set
                # MS27CarvingWall.timer to Say MS27Voice`, where MS27CarvingWall
                # is a placed REFR and the timer lives on that object's own
                # script.  Binding it as a quest property emitted
                # `Quest Property MS27CarvingWall` and the fragment failed to
                # compile ("field or property `timer` not found"), because a bare
                # Quest has no converted script members.  There is no reliable
                # handle from a TopicInfo fragment to an arbitrary third-party
                # reference, so leave it unowned: the call site's park still
                # holds the loop off and the dropped-line watchdog bounds it.
                continue
            elif edid and edid.lower() in quest_script_owner:
                # A QUEST script writing its OWN timer with no prefix
                # (`set convTimer to BaurusRef.SayTo ...` in CharGenQuest).
                # It is script-local in TES4 syntax but the timer lives on the
                # QUEST, so it must bind a quest property — casting the SPEAKER
                # to a Quest script yields None, the write is silently dropped
                # and the parked timer is NEVER released.  That one
                # misclassification killed every CharGenVoice line, which is
                # where CharacterGen stopped after "Baurus, lock the door".
                owners.setdefault(
                    topic,
                    ('quest', f'{quest_script_owner[edid.lower()]}.{target}',
                     beat))
            elif edid:
                # Script-local (`timer` on ValenDrethScript): the timer lives
                # on the SPEAKER's own script, which a TopicInfo fragment
                # reaches by casting akSpeakerRef to that script type.
                owners.setdefault(topic, ('speaker', f'{edid}|{target}', beat))
    return owners


def build_beat_fields_by_owner(by_type: dict) -> dict:
    """owner EditorID (lower) -> {timer fields needing a pending-beat property}.

    A script that pauses between lines writes `set SomeQuest.convTimer to
    SomeQuest.convTimer + 2.5`.  The converter redirects that to a
    `convTimerPendingBeat` companion (the timer itself is counted DOWN while
    the line plays, so a value stored there is eroded before the End fragment
    can read it back) — but the companion has to be DECLARED on SomeQuest's
    script, which a different converter run produces.  Scanning the whole
    export up front makes that independent of conversion order and safe across
    the process pool.
    """
    owners: dict = {}
    for rec in by_type.get('SCPT', []):
        txt = (rec.get('SCTX') or '').replace('\\r\\n', '\n')
        script_name = (rec.get('EditorID') or '').lower()
        for m in _SAY_TIMER_RE.finditer(txt):
            target = m.group(1)
            owner, field = (target.rsplit('.', 1) if '.' in target
                            else (script_name, target))
            if not owner:
                continue
            pat = (r'set\s+[\w.]*\b' + re.escape(field) + r'\b\s+to\s+'
                   r'[\w.]*\b' + re.escape(field) + r'\b\s*\+\s*[\d.]+')
            if re.search(pat, txt, re.IGNORECASE):
                owners.setdefault(owner.lower(), set()).add(field)
    # Re-key from the RECORD's EditorID (`charactergen`) to the EditorID of the
    # SCRIPT that record runs (`chargenquest`), because that is the script the
    # property must be declared on and the name each converter matches itself
    # against.
    scri_by_edid = {}
    script_edid = {}
    for rec in by_type.get('SCPT', []):
        script_edid[(rec.get('FormID') or '').upper()] = rec.get('EditorID') or ''
    for sig in ('QUST', 'NPC_', 'CREA', 'ACHR', 'ACRE', 'REFR'):
        for rec in by_type.get(sig, []):
            e = (rec.get('EditorID') or '').lower()
            scri = (rec.get('SCRI') or '').upper()
            if e and scri:
                scri_by_edid[e] = script_edid.get(scri, '')
    # A key that is already a SCRIPT name (script-local timer) stays as-is;
    # only a RECORD name is redirected to the script it runs.
    known_scripts = {v.lower() for v in script_edid.values() if v}
    out: dict = {}
    for rec_edid, fields in owners.items():
        key = rec_edid
        if rec_edid not in known_scripts:
            key = (scri_by_edid.get(rec_edid) or rec_edid).lower()
        out.setdefault(key, set()).update(fields)
    return out


def _owner_has_beat(spec: str, kind: str) -> bool:
    """Does the script owning this Say timer declare a pending-beat companion?

    Mirrors the declaration rule in ScriptConverter (`beat_fields_by_owner`,
    keyed by SCRIPT EditorID) so a fragment never references a property that
    was not emitted.
    """
    by_owner = ScriptConverter.beat_fields_by_owner or {}
    if kind == 'quest':
        owner, field = spec.split('.', 1)
    else:
        owner, field = spec.split('|', 1)
    return field.lower() in {f.lower() for f in by_owner.get(owner.lower(), ())}


def _info_batch(records: list, output_dir: str, xref: CrossRefGraph,
                stats: dict, info_reveals: dict = None,
                service_topics: dict = None):
    """Convert a batch of INFO records into TopicInfo fragment .psc files.

    info_reveals ({info_fid24: [unlock global names]}) marks AddTopic revealer
    INFOs: their OnEnd fragment sets the unlock globals (a fragment is
    generated even when the INFO has no result script). Must stay in sync with
    the VMADs the importer writes (same unlock plan).

    service_topics ({dial_formid_str: 'barter'|'training'}) marks the service-
    menu topics; fragments for their INFOs also open the corresponding menu.
    """
    info_reveals = info_reveals or {}
    service_topics = service_topics or {}
    timer_owners = _WORKER_CTX.get('say_timer_owners') or {}
    durations = ScriptConverter.say_durations or {}
    topic_by_dial = _WORKER_CTX.get('topic_by_dial') or {}

    for rec in records:
        result_script = rec.get('ResultScript', '')
        has_script = bool(result_script and result_script.strip())
        formid = rec.get('FormID', '')
        try:
            fid24 = int(formid, 16) & 0xFFFFFF
        except (TypeError, ValueError):
            fid24 = 0
        reveals = info_reveals.get(fid24, [])
        service_kind = service_topics.get(rec.get('ParentDIAL', ''), '')
        # This INFO's own measured length, and the conversation timer its topic
        # drives — see build_say_timer_owners. Emitting `<timer> = <secs>` in
        # the End fragment replaces the call site's worst-case estimate with
        # the length of the line that actually played.
        timer_fix = ''
        topic_name = topic_by_dial.get(rec.get('ParentDIAL', ''), '')
        owner = timer_owners.get(topic_name)
        # Release the conversation timer this topic drives. The call site
        # PARKED it (see converter._say_seconds) so the 0.1s polling loop could
        # not re-Say mid-line; the engine runs this fragment when the line
        # ENDS, so clearing it here is the exact "line finished" signal — no
        # duration estimate is involved in the pacing.
        #
        # Assign 0 — never `timer - park`. Several actor scripts poll the SAME
        # quest timer on independent 0.5s updates while the quest script
        # decrements it every 0.1s, and `Say()` is asynchronous, so a relative
        # adjustment is a race: if this fragment's subtraction lands before the
        # call site's park (short line, or an interleaved tick) the timer ends
        # at +park with nobody speaking and the conversation stalls for a
        # minute; if two speakers park in the same window, both subtract. An
        # absolute 0 is idempotent and order-independent, so the handoff is
        # deterministic no matter how the ticks interleave.
        #
        # The 13 scripts that stack a deliberate beat re-read the timer at the
        # CALL SITE (converter._resolve_parked_timer_expr rewrites those), so
        # they do not depend on the value this fragment leaves behind.
        # Release is GUARDED (`If timer >= park`) and subtracts the park rather
        # than assigning 0 outright. Both details matter:
        #
        #  * The guard makes it idempotent under interleaving. Several actor
        #    scripts poll this one timer on independent 0.5s updates while the
        #    quest script decrements it every 0.1s, and Say() is asynchronous,
        #    so an unguarded relative adjustment races: land it before the call
        #    site's park and the timer sits at +park with nobody speaking (a
        #    minute-long stall); let two speakers park in one window and it is
        #    subtracted twice. Only a timer still holding the park is released.
        #  * Subtracting (not zeroing) preserves the deliberate beats. Oblivion
        #    charged those ON TOP of the line's length, so the call site emits
        #    `park + k` (see converter._resolve_parked_timer_expr) — writing the
        #    bare pause there would release the guard mid-line and the next
        #    speaker's Say would be dropped. `park + k` is still >= park, so
        #    this fires and leaves exactly `k` to run after the line ends.
        owner_prop = ''
        if owner:
            kind, spec = owner[0], owner[1]
            # Per-topic, from the beat this topic stages — the call site sizes
            # its park off the identical call, so the two stay in step.
            thresh = ScriptConverter.say_beat_threshold(owner[2])
            if kind == 'quest':
                owner_prop, field = spec.split('.', 1)
                ref = f'{_safe_property_name(owner_prop)}.{field}'
            else:
                script_edid, field = spec.split('|', 1)
                cls = papyrus_script_name(script_edid)
                ref = f'(akSpeakerRef as {cls}).{field}'
            # Consume the staged pause only when the owning script actually
            # HAS one — most Say timers never take a beat, and referencing a
            # companion that was never declared fails to compile ("field or
            # property `timerPendingBeat` not found", 300+ fragments).
            if _owner_has_beat(spec, kind):
                beat_ref = ScriptConverter.beat_property(ref)
                # The beat travels in its own property because the owning loop
                # counts the timer DOWN while the line plays, so anything
                # encoded in the timer itself is eroded before this runs.
                release = (f'    {ref} = {beat_ref}\n'
                           f'    {beat_ref} = 0\n')
            else:
                release = f'    {ref} = 0\n'
            timer_fix = (f'  If {ref} > {thresh:g}\n'
                         f'{release}'
                         f'  EndIf')
        if not has_script and not reveals and not timer_fix:
            # Script-less service-menu INFOs use the shared static scripts.
            continue

        if has_script:
            stats['info_total'] += 1

        try:
            body_lines = []
            prop_refs = {}
            if has_script:
                conv = ScriptConverter(xref)
                _preload_scro_refs(conv, rec, xref)
                body_lines = conv.convert_fragment(result_script, 'TopicInfo')
                prop_refs = dict(conv._property_refs)

            script_name = f'TES4_TIF__{formid}'
            out_lines = [
                f'ScriptName {script_name} extends TopicInfo Hidden',
                '',
            ]
            declared = set()
            for gname in reveals:
                declared.add(gname.lower())
                out_lines.append(f'GlobalVariable Property {gname} Auto')
            # Only the quest-scoped form needs a bound property; the speaker
            # form casts akSpeakerRef and declares nothing.
            if owner_prop:
                # Must be typed as the quest's CONVERTED script class, not
                # `Quest` — convTimer lives on the generated script.
                safe_owner = _safe_property_name(owner_prop)
                if safe_owner.lower() not in declared:
                    declared.add(safe_owner.lower())
                    otype = xref.get_quest_script_type(owner_prop)
                    out_lines.append(f'{otype} Property {safe_owner} Auto')
            if prop_refs:
                for pname, ptype in sorted(prop_refs.items()):
                    safe = _safe_property_name(pname)
                    if safe.lower() in declared:
                        continue
                    declared.add(safe.lower())
                    out_lines.append(f'{ptype} Property {safe} Auto')
            if declared:
                out_lines.append('')
            out_lines.append('Function Fragment_0(ObjectReference akSpeakerRef)')
            # Unlock the AddTopic-revealed topics first — OnEnd fires when the
            # line finishes, right before the topic menu refreshes.
            for gname in reveals:
                out_lines.append(f'  {gname}.SetValue(1)')
            # Correct the conversation timer to THIS line's real length BEFORE
            # the converted body: Oblivion's value at this point was the line's
            # own duration, and a result script that retimes the beat does so
            # RELATIVE to it (CharGenMain 0x32B0C cuts Glenroy off with
            # `convTimer - .4`). Writing it afterwards would discard that.
            if timer_fix:
                out_lines.append(f'{timer_fix}  ; line ended: release the parked timer')
            out_lines.extend(body_lines)
            if service_kind:
                out_lines.append(_SERVICE_MENU_CALL[service_kind])
            out_lines.append('EndFunction')
            out_lines.append('')

            papyrus = '\n'.join(out_lines)
            out_path = os.path.join(output_dir, f'{script_name}.psc')
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(papyrus)
            if has_script:
                stats['info_ok'] += 1
            stats['todo_count'] += papyrus.count(';TODO')
        except Exception as e:
            stats['info_err'] += 1
            stats['errors'].append(f'INFO {formid}: {e}')


def _superseded_stages(rec: dict, fragments: list) -> dict:
    """Work out which objectives each stage FINISHES.

    Oblivion has no "objective completed" concept — its journal is an append-only
    log — so the completion points have to be recovered from the data.  The
    signal is the quest TARGETS: every TES4 QSTA carries `GetStage` conditions
    saying exactly which stages that target's compass marker is live at, which is
    Oblivion's own encoding of "the player still has this errand to run".
    (FGC01Rats: Arvena is live at 10/30/55/65/90 — the report-back steps; Pinarus
    at 40-50 — the hunt; Quill-Weave at 70-80/105 — the stakeout.)

    An objective's step is in progress while its markers are live and is finished
    at the first stage where they go dark.  So stage N completes objective M iff
    M's marker set was live at M and is no longer live at N.  Crucially this is
    NOT "N completes everything numerically below it": an objective whose marker
    stays live across several stages stays open, so a quest can hold several
    objectives open at once and side branches are not force-ticked by an
    unrelated higher-numbered stage.

    Returns {(stage_idx, log_idx): [stage indices this fragment completes]}.

    Fallback: a stage whose objective has no target at all (a marker-less "return
    when you're ready" entry, or a quest with no QSTA records) has no gate to read.
    Those are closed by the next objective that fires, which is the best available
    reading of "the log moved on" and matches Oblivion's linear default.
    """
    from tes5_import.dialog_converter import _target_live_at_stage

    # Per-target TES4 stage gates.
    targets = []
    t = 0
    while f'Target[{t}].FormID' in rec:
        raws = []
        k = 0
        while f'Target[{t}].Condition[{k}].Raw' in rec:
            raws.append(rec[f'Target[{t}].Condition[{k}].Raw'])
            k += 1
        targets.append(raws)
        t += 1

    # Objective-bearing stages, in quest order.
    obj_frags = [(s, j) for s, j, text, *_ in fragments if text]
    obj_stages = sorted({s for s, _ in obj_frags})

    def live_set(stage):
        """Indices of the targets whose marker is live at `stage`."""
        return frozenset(i for i, raws in enumerate(targets)
                         if raws and _target_live_at_stage(raws, stage))

    live = {s: live_set(s) for s in obj_stages}

    # For each objective, find the single stage that ends it: the FIRST later
    # objective-stage at which its markers are no longer live.  Completing an
    # objective once, at that stage, is what keeps parallel objectives open —
    # re-emitting it at every subsequent stage would be redundant no-ops, and
    # sweeping every lower index would force-tick branches that are still live.
    closed_by = {}
    for prior in obj_stages:
        there = live[prior]
        later = [s for s in obj_stages if s > prior]
        if there:
            # Gate-driven: the errand is over at the first stage none of its
            # markers survive into.
            end = next((s for s in later if not (there & live[s])), None)
        else:
            # No marker to read (a marker-less "return when you're ready" entry,
            # or a quest with no targets at all) — the log simply moves on.
            end = later[0] if later else None
        if end is not None:
            closed_by[prior] = end

    supersedes = {}
    for stage, log_idx in obj_frags:
        # Attach the completions to the first log entry of the stage, so a stage
        # with several log entries does not emit them once per entry.
        first_log = min(j for s, j in obj_frags if s == stage)
        supersedes[(stage, log_idx)] = (
            sorted(p for p, end in closed_by.items() if end == stage)
            if log_idx == first_log else [])
    return supersedes


def _qust_batch(records: list, output_dir: str, xref: CrossRefGraph,
                stats: dict, stage_reveals: dict = None):
    """Convert a batch of QUST records into Quest fragment .psc files.

    A fragment is generated for every stage that has journal log text (CNAM),
    whether or not it also has a result script.  Each fragment calls
    SetObjectiveDisplayed / SetObjectiveCompleted so the quest appears in the
    Skyrim journal — without those calls CNAM text is never visible.

    stage_reveals ({(quest_edid_lower, stage): [unlock global names]}) marks
    stages whose TES4 result scripts contained `AddTopic X`: the fragment sets
    the unlock globals (the AddTopic command itself is a no-op in conversion).
    """
    stage_reveals = stage_reveals or {}

    for rec in records:
        edid = rec.get('EditorID', '')
        if not edid:
            continue

        stage_count_str = rec.get('StageCount', '0')
        try:
            stage_count = int(stage_count_str)
        except ValueError:
            continue

        # Collect all stages that need a fragment:
        # - stages with log text (need objective calls even if no result script)
        # - stages with result scripts (need script body)
        # Each entry: (stage_idx, log_idx, log_text, result_script, complete_flag, stage_arr_idx, log_arr_idx)
        fragments = []
        for i in range(stage_count):
            stage_idx_str = rec.get(f'Stage[{i}].Index', '0')
            try:
                stage_idx = int(stage_idx_str)
            except ValueError:
                continue

            log_count_str = rec.get(f'Stage[{i}].LogCount', '0')
            try:
                log_count = int(log_count_str)
            except ValueError:
                continue

            for j in range(log_count):
                log_text = rec.get(f'Stage[{i}].Log[{j}].Text', '')
                script = rec.get(f'Stage[{i}].Log[{j}].ResultScript', '')
                log_flags_str = rec.get(f'Stage[{i}].Log[{j}].Flags', '0')
                try:
                    log_flags = int(log_flags_str)
                except ValueError:
                    log_flags = 0
                complete_flag = bool(log_flags & 0x01)
                if log_text or (script and script.strip()):
                    fragments.append((stage_idx, j, log_text, script, complete_flag, i, j))

        if not fragments:
            continue

        # Which objectives each stage finishes, recovered from the TES4 target
        # stage-gates.  (convert_QUST emits one QOBJ per stage index that has log
        # text, so the stage indices here are exactly the objectives on the record.)
        supersedes = _superseded_stages(rec, fragments)

        # Count only fragments that have result scripts for stats
        scripted_count = sum(1 for f in fragments if f[3] and f[3].strip())
        stats['qust_total'] += scripted_count

        try:
            conv = ScriptConverter(xref)
            # Pre-populate external references from SCRO entries
            _preload_scro_refs(conv, rec, xref)
            script_name = papyrus_script_name(edid, 'TES4_QF_')
            out_lines = [
                f'ScriptName {script_name} extends Quest Hidden',
                '',
            ]

            # A stage has ONE objective (index = stage index) no matter how many
            # journal entries it carried in TES4 — MQ01's tutorial stages ship a
            # gamepad text and a keyboard text, and emitting the objective calls
            # per entry displayed the same objective twice.
            objective_emitted = set()
            for stage_idx, log_idx, log_text, script_src, complete_flag, stage_arr_idx, log_arr_idx in fragments:
                # Load per-stage SCROs for this fragment
                _preload_stage_scro_refs(conv, rec, xref, stage_arr_idx, log_arr_idx)
                func_name = f'Fragment_Stage_{stage_idx:04d}_Item_{log_idx}'
                out_lines.append(f'Function {func_name}()')
                if stage_idx in objective_emitted:
                    log_text = None
                elif log_text:
                    objective_emitted.add(stage_idx)
                # Objective tracking.  Oblivion's journal is an append-only LOG:
                # setting stage 20 just adds entry 20 under entry 10, and 10 stays
                # as history — it was never a checkbox, so nothing "completes" it.
                # Skyrim's journal is a SET of objectives, each independently
                # Displayed / Completed / Failed, and a Displayed-but-not-Completed
                # objective renders as an open bullet with a live compass marker.
                #
                # So a stage must explicitly close out the step it FINISHES.  Note
                # this is NOT "complete every lower-numbered objective": a quest can
                # legitimately hold several objectives open at once (fetch A *and*
                # talk to B), and side branches are not superseded just because a
                # higher-numbered stage fired.  An objective is completed only when
                # the quest actually moves past that specific step — see
                # _superseded_stages() for how that is derived from the TES4 data.
                if log_text:
                    for prior in supersedes.get((stage_idx, log_idx), ()):
                        out_lines.append(f'  SetObjectiveCompleted({prior}, true)')
                    out_lines.append(f'  SetObjectiveDisplayed({stage_idx}, true)')
                # TES4 QSDT 0x01 is "complete the QUEST" — it is not per-objective,
                # it marks the stage that ENDS the quest (TES4 has no fail bit; a
                # quest's success and failure endings are both just flag 0x01, and
                # 89 of Oblivion's 390 quests have several such stages).  The quest
                # is over, so nothing may be left hanging as an open bullet.  We
                # cannot know statically which branch the player took to get here,
                # so let the engine settle it: CompleteAllObjectives() closes
                # whatever is still displayed and leaves the never-shown entries of
                # the skipped branch alone.
                if complete_flag:
                    out_lines.append('  CompleteAllObjectives()')
                    out_lines.append('  CompleteQuest()')
                # AddTopic unlock globals revealed by this stage's TES4 script
                for gname in stage_reveals.get((edid.lower(), stage_idx), []):
                    out_lines.append(f'  {gname}.SetValue(1)')
                # Original result script body (if any)
                if script_src and script_src.strip():
                    body_lines = conv.convert_fragment(script_src, 'Quest')
                    out_lines.extend(body_lines)
                out_lines.append('EndFunction')
                out_lines.append('')

            # Insert property declarations after ScriptName line
            quest_globals = sorted({g for (q, _s), gs in stage_reveals.items()
                                    if q == edid.lower() for g in gs})
            for gi, gname in enumerate(quest_globals):
                out_lines.insert(2 + gi, f'GlobalVariable Property {gname} Auto')
            prop_refs = conv.get_property_refs()
            if prop_refs:
                # Merge case-variant keys: pick the most specific type (non-Quest wins)
                merged: dict[str, tuple[str, str]] = {}  # lower_name -> (canonical_name, type)
                for pname, ptype in sorted(prop_refs.items()):
                    key = pname.lower()
                    if key in merged:
                        existing_name, existing_type = merged[key]
                        # Keep the more specific type; prefer the first-seen
                        # (SCRO-canonical) name so it matches the VMAD binding.
                        if existing_type == 'Quest' and ptype != 'Quest':
                            merged[key] = (existing_name, ptype)
                        elif ptype == 'ActorBase' and existing_type != 'ActorBase':
                            # Base typing from a base-semantics function
                            # (SetEssential base) must win over ANY reference
                            # type — including Actor and Actor-derived TES4_*
                            # scripts. The VMAD binds this property to a base
                            # (NPC_/CREA) record, and a reference-typed property
                            # bound to a base is UNBINDABLE: Papyrus aborts the
                            # whole script's init, so the quest never finishes
                            # initialising and its aliases never fill. (FGC01Rats:
                            # QuillWeave, an NPC_ base, was typed as the Actor
                            # script TES4_FGC01QuillweaveScript.)
                            merged[key] = (existing_name, ptype)
                        # else: keep existing (already specific, or both Quest)
                    else:
                        merged[key] = (pname, ptype)
                insert_idx = 2  # After ScriptName + blank line
                declared = set()
                count = 0
                for pname, ptype in sorted(merged.values(), key=lambda x: x[0].lower()):
                    safe = _safe_property_name(pname)
                    if safe.lower() in declared:
                        continue
                    declared.add(safe.lower())
                    out_lines.insert(insert_idx + count, f'{ptype} Property {safe} Auto')
                    count += 1
                out_lines.insert(insert_idx + count, '')

            papyrus = '\n'.join(out_lines)
            out_path = os.path.join(output_dir, f'{script_name}.psc')
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(papyrus)
            stats['qust_ok'] += scripted_count
            stats['todo_count'] += papyrus.count(';TODO')
        except Exception as e:
            stats['qust_err'] += scripted_count
            stats['errors'].append(f'QUST {edid}: {e}')


def _write_report(output_dir: str, stats: dict):
    """Write a conversion summary report."""
    report_path = os.path.join(output_dir, '_CONVERSION_REPORT.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('TES4 Script -> Papyrus Conversion Report\n')
        f.write('=' * 50 + '\n\n')
        f.write(f'SCPT records: {stats["scpt_ok"]}/{stats["scpt_total"]} converted\n')
        f.write(f'INFO fragments: {stats["info_ok"]}/{stats["info_total"]} converted\n')
        f.write(f'QUST stage scripts: {stats["qust_ok"]}/{stats["qust_total"]} converted\n')
        total = stats['scpt_ok'] + stats['info_ok'] + stats['qust_ok']
        errs = stats['scpt_err'] + stats['info_err'] + stats['qust_err']
        f.write(f'\nTotal: {total} converted, {errs} errors\n')
        f.write(f';TODO markers: {stats["todo_count"]}\n\n')

        if stats['errors']:
            f.write('Errors:\n')
            for err in stats['errors'][:100]:
                f.write(f'  {err}\n')
            if len(stats['errors']) > 100:
                f.write(f'  ... and {len(stats["errors"]) - 100} more\n')


# Player FormID — skip when pre-loading SCRO refs
_PLAYER_FORMID = '00000014'


def _preload_scro_refs(conv: 'ScriptConverter', rec: dict, xref: CrossRefGraph):
    """Pre-populate converter property_refs from SCRO entries in a record."""
    i = 0
    while True:
        key = f'SCRO[{i}]'
        fid = rec.get(key)
        if fid is None:
            break
        i += 1
        _add_scro_ref(conv, fid, xref)


def _preload_stage_scro_refs(conv: 'ScriptConverter', rec: dict, xref: CrossRefGraph,
                              stage_arr_idx: int, log_arr_idx: int):
    """Pre-populate converter property_refs from per-stage/log SCRO entries."""
    k = 0
    while True:
        key = f'Stage[{stage_arr_idx}].Log[{log_arr_idx}].SCRO[{k}]'
        fid = rec.get(key)
        if fid is None:
            break
        k += 1
        _add_scro_ref(conv, fid, xref)


def _add_scro_ref(conv: 'ScriptConverter', fid: str, xref: CrossRefGraph):
    """Add a single SCRO FormID as a property ref on the converter."""
    if fid == _PLAYER_FORMID:
        return
    edid = xref.formid_to_edid.get(fid)
    if not edid:
        return
    rtype = xref.record_type.get(fid, '')
    ptype = _record_type_to_papyrus(rtype)
    # Prefer attached SCPT-derived type for cross-script property accesses
    # (e.g. Arena.AnnounceWin). For QUST records, start with 'Quest' base type —
    # the specific type will be promoted later if the script body uses dot-notation
    # variable access (e.g. Arena.AnnounceWin) which the converter handles.
    if rtype != 'QUST':
        script_type = xref.get_record_script_type(edid)
        if script_type:
            ptype = script_type
    # Key on the Papyrus-SAFE name, which is what _convert_ref stores and what
    # _collect_scro_properties writes into the VMAD.  Keying on the raw EditorID
    # instead created a SECOND entry for any EditorID that gets renamed (MS14 is
    # a vanilla Skyrim script name, so it becomes myMS14): the generic 'Quest'
    # from this SCRO and the specific 'TES4_MS14Script' from _convert_ref lived
    # under different keys, so the downgrade guard below never fired and the
    # generic one won the declaration — leaving the body calling myMS14.QuestDone
    # on a plain Quest ("field or property QuestDone not found").
    key = _safe_property_name(edid)
    # Don't downgrade a type already upgraded by _convert_ref (e.g. Quest → TES4_FGQuestTrack).
    # _preload_stage_scro_refs is called once per stage and would otherwise reset types
    # that were promoted when a prior stage's result script accessed cross-script vars.
    cur = conv._property_refs.get(key, '')
    if cur and cur != 'Quest' and ptype == 'Quest':
        return
    # Never overwrite an ActorBase typing set by a base-semantics function
    # (SetEssential base). The SCRO here is the base record, so a reference /
    # Actor-script type would be UNBINDABLE against the base and abort the whole
    # script's init. ActorBase is a hard constraint, not a promotable guess.
    if cur == 'ActorBase':
        return
    conv._property_refs[key] = ptype


# ===========================================================================
# VMAD binary helpers (for tes5_import integration)
# ===========================================================================

def build_vmad_quest_fragments(quest_edid: str, stage_fragments: list[tuple[int, int]],
                               property_values: dict = None,
                               attached_script: tuple = None) -> bytes:
    """Build VMAD binary for a QUST record with stage script fragments and/or
    an attached quest script.

    Args:
        quest_edid: Quest EditorID
        stage_fragments: list of (stage_index, log_index) tuples; may be empty
            when only an attached script is present (vanilla then writes the
            fragments section with count=0 and an EMPTY file name — e.g.
            MS12PostQuest / WIThief01 in Skyrim.esm).
        property_values: optional dict {property_name: formid} for the QF
            fragment script's properties
        attached_script: optional (script_name, {prop: formid}) for the
            converted TES4 quest script (SCRI) to attach alongside

    Returns VMAD binary data.
    """
    script_name = papyrus_script_name(quest_edid, 'TES4_QF_')
    buf = bytearray()

    # VMAD header
    buf += struct.pack('<HH', 5, 2)  # version=5, objectFormat=2

    scripts = []
    if stage_fragments:
        scripts.append((script_name, property_values or {}))
    if attached_script:
        scripts.append(attached_script)

    buf += struct.pack('<H', len(scripts))
    for sname, props in scripts:
        buf += _pack_wstring(sname)
        buf += struct.pack('<B', 0)   # flags=0
        buf += struct.pack('<H', len(props))
        for pname, fid in props.items():
            buf += _pack_wstring(pname)
            buf += struct.pack('<BB', 1, 1)       # type=Object, status=Edited
            buf += struct.pack('<HhI', 0, -1, fid) # unused=0, alias=-1, FormID

    # Script fragments (quest type, wbScriptFragmentsQuest):
    #   S8  Extra bind data version = 2
    #   U16 FragmentCount
    #   LenString(U16) FileName
    buf += struct.pack('<b', 2)                  # Extra bind data version = 2
    buf += struct.pack('<H', len(stage_fragments))  # FragmentCount
    buf += _pack_wstring(script_name if stage_fragments else '')  # FileName
    for stage_idx, log_idx in stage_fragments:
        frag_name = f'Fragment_Stage_{stage_idx:04d}_Item_{log_idx}'
        buf += struct.pack('<H', stage_idx)   # Quest Stage (U16)
        buf += struct.pack('<h', 0)           # Unknown (S16)
        buf += struct.pack('<i', log_idx)     # Quest Stage Index = log entry index (S32)
        buf += struct.pack('<b', 1)           # Unknown (S8) — vanilla always 1
        buf += _pack_wstring(script_name)
        buf += _pack_wstring(frag_name)

    # Alias-script array (wbVMADFragmentedQUST: Version, ObjectFormat, Scripts,
    # ScriptFragmentsQuest, **Aliases**) — an S16 count followed by that many
    # alias-script entries.  A QUST VMAD is malformed without it, and the engine
    # parses VMAD strictly: running off the end of the buffer where it expects
    # this count aborts the record's whole script/alias binding, so EVERY quest
    # alias fills as NONE *and* every QF script property comes back None.  That
    # is the real reason converted quests showed a journal objective but never a
    # marker.  Verified against Skyrim.esm: vanilla QUST VMADs end with exactly
    # these two bytes (e.g. DBSideContract03's 643-byte VMAD parses to 643/643
    # only once the trailing count is read).  We attach no alias scripts, so 0.
    buf += struct.pack('<h', 0)

    return bytes(buf)


def build_vmad_info_fragment(info_formid: str, property_values: dict = None,
                             script_name: str = None) -> bytes:
    """Build VMAD binary for an INFO record with a result script fragment.

    Args:
        info_formid: INFO FormID string (e.g. "00012345")
        property_values: optional dict {property_name: formid} for script properties
        script_name: override the per-INFO TES4_TIF__ name with a shared static
            fragment script (e.g. TES4_ShowBarterMenu for service-menu INFOs)

    Returns VMAD binary data.
    """
    script_name = script_name or f'TES4_TIF__{info_formid}'
    buf = bytearray()

    # VMAD header
    buf += struct.pack('<HH', 5, 2)   # version=5, objectFormat=2

    # Attached scripts: 1 script with properties
    buf += struct.pack('<H', 1)       # 1 attached script
    buf += _pack_wstring(script_name)
    buf += struct.pack('<B', 0)       # flags=0
    # Properties
    if property_values:
        buf += struct.pack('<H', len(property_values))
        for pname, fid in property_values.items():
            buf += _pack_wstring(pname)
            buf += struct.pack('<BB', 1, 1)       # type=Object, status=Edited
            buf += struct.pack('<HhI', 0, -1, fid) # unused=0, alias=-1, FormID
    else:
        buf += struct.pack('<H', 0)   # propertyCount=0

    # Script fragments for INFO (wbScriptFragmentsInfo):
    #   S8  Extra bind data version = 2
    #   U8  Flags: bit0=OnBegin, bit1=OnEnd (no other bits defined for INFO)
    #   LenString(U16) FileName
    #   For each set bit in Flags, one fragment: S8 Unknown + LenString ScriptName + LenString FragmentName
    # Fragment count is implicit (popcount of Flags bits 0-1).
    buf += struct.pack('<b', 2)        # Extra bind data version = 2
    buf += struct.pack('<B', 0x02)     # Flags = OnEnd (1 fragment)
    buf += _pack_wstring(script_name)  # FileName

    # Fragment 0 — OnEnd
    buf += struct.pack('<B', 1)        # Unknown (always 1 in vanilla Skyrim.esm)
    buf += _pack_wstring(script_name)  # ScriptName
    buf += _pack_wstring('Fragment_0') # FragmentName

    return bytes(buf)


# Papyrus property object-type codes for the VMAD property record (objectFormat 2).
#   1 = Object (FormID + alias), 2 = wstring, 3 = Int32, 4 = Float, 5 = Bool
_VMAD_PROP_OBJECT = 1
_VMAD_PROP_INT = 3
_VMAD_PROP_FLOAT = 4
_VMAD_PROP_BOOL = 5


def build_vmad_object_script(script_name: str,
                             object_props: dict = None,
                             value_props: dict = None) -> bytes:
    """Build VMAD binary attaching a single Papyrus script to an object record.

    Unlike QUST/INFO VMADs this has NO fragment section — plain object scripts
    (ACTI/CONT/DOOR/FLOR/… on their placed instances or, as here, on the base
    record) run their own event handlers (OnActivate, OnLoad, …) directly.

    Args:
        script_name: full Papyrus script name (e.g. 'TES4_SE07AltarScript').
        object_props: {property_name: formid_int} — Object-typed properties
            bound to a record FormID (records/spells/quests/globals/actors).
        value_props: {property_name: (kind, value)} — literal-valued properties
            where kind is 'int' | 'float' | 'bool'.  Optional; usually the
            script's non-ref locals stay unbound and default to 0.

    Returns VMAD binary data (version 5, objectFormat 2).
    """
    object_props = object_props or {}
    value_props = value_props or {}
    buf = bytearray()

    # VMAD header
    buf += struct.pack('<HH', 5, 2)   # version=5, objectFormat=2

    # Attached scripts: exactly 1
    buf += struct.pack('<H', 1)
    buf += _pack_wstring(script_name)
    buf += struct.pack('<B', 0)       # flags=0

    total_props = len(object_props) + len(value_props)
    buf += struct.pack('<H', total_props)
    for pname, fid in object_props.items():
        buf += _pack_wstring(pname)
        buf += struct.pack('<BB', _VMAD_PROP_OBJECT, 1)   # type=Object, status=Edited
        buf += struct.pack('<HhI', 0, -1, fid)            # unused=0, alias=-1, FormID
    for pname, (kind, value) in value_props.items():
        buf += _pack_wstring(pname)
        if kind == 'float':
            buf += struct.pack('<BB', _VMAD_PROP_FLOAT, 1)
            buf += struct.pack('<f', float(value))
        elif kind == 'bool':
            buf += struct.pack('<BB', _VMAD_PROP_BOOL, 1)
            buf += struct.pack('<B', 1 if value else 0)
        else:  # int
            buf += struct.pack('<BB', _VMAD_PROP_INT, 1)
            buf += struct.pack('<i', int(value))

    return bytes(buf)


def _pack_wstring(s: str) -> bytes:
    """Pack a VMAD wstring: U16 length + UTF-8 bytes."""
    encoded = s.encode('utf-8')
    return struct.pack('<H', len(encoded)) + encoded


# ===========================================================================
# CLI
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description='Convert TES4 scripts to Papyrus')
    parser.add_argument('export_dir', help='Path to export directory (e.g. export/Oblivion.esm)')
    parser.add_argument('-o', '--output', default=None,
                        help='Output dir for .psc files (default: output/oblivion.esm/scripts/source)')
    parser.add_argument('--workers', type=int, default=None, help='Worker threads')
    args = parser.parse_args()

    output_dir = args.output
    if output_dir is None:
        output_dir = os.path.join('output', 'oblivion.esm', 'scripts', 'source')

    convert_all_scripts(args.export_dir, output_dir, args.workers)


if __name__ == '__main__':
    main()
