"""Pipeline orchestration — convert all scripts, VMAD helpers, CLI."""

import argparse
import os
import re
import struct
import time

from tes5_import.text_reader import parse_export_file
from worker_budget import worker_count

from script_convert.constants import (_PAPYRUS_RESERVED, _RECORD_TYPE_PAPYRUS, _GLOBAL_CANONICAL,
                                     _sanitize_name, _safe_property_name, _canonical_global,
                                     _record_type_to_papyrus, papyrus_script_name,
                                     PAPYRUS_MAX_SCRIPT_NAME)
from script_convert.cross_ref import CrossRefGraph, master_names
from script_convert.converter import ScriptConverter


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
                        quest_script_vars=None,
                        quest_edid_by_fid=None, topic_unlock_globals=None,
                        message_menus=None, mesh_bounds_cache=None,
                        chargen_menus=None, say_topics=None):
    # Windows spawns workers, so module-level caches loaded in the parent do
    # NOT carry over — each worker reloads the mesh-bounds cache or every
    # needs_havok_release() lookup answers 0 and no trap gets its release.
    if mesh_bounds_cache:
        from tes5_import.mesh_bounds import load_mesh_bounds
        load_mesh_bounds(mesh_bounds_cache, quiet=True)
    _WORKER_CTX.update(xref=xref, output_dir=output_dir,
                       info_reveals=info_reveals,
                       service_topics=service_topics,
                       stage_reveals=stage_reveals,
                       quest_script_vars=quest_script_vars or {},
                       quest_edid_by_fid=quest_edid_by_fid or {})
    # Class-level, so every ScriptConverter a worker builds sees the measured
    # voice-line lengths (per INFO for the Begin fragments, per topic for the
    # SayLine fallback).
    ScriptConverter.say_durations = say_durations or {}
    # Topics a script drives via Say/SayTo.  Windows SPAWNS workers, so this
    # must be passed in explicitly -- a set built in the parent while scanning
    # scripts does not survive into the child, and an empty set here would
    # make info_needs_fragment() drop the timing fragments that SayLine needs.
    ScriptConverter.say_topics = set(say_topics or ())
    # DIAL EditorID -> unlock global, so a script `AddTopic X` opens the same
    # gate the INFO/QUST fragments do.
    ScriptConverter.topic_unlock_globals = topic_unlock_globals or {}
    # script EditorID -> button-MessageBox MESG plan; the importer writes the
    # records this makes the converter reference (message_menus.py).
    ScriptConverter.message_menus = message_menus or {}
    # ShowBirthsignMenu/ShowClassMenu → modal Message pages + per-choice
    # spell grants (message_menus.build_chargen_menus; importer authors the
    # MESG records at fixed FormIDs).
    ScriptConverter.chargen_menus = chargen_menus or {}


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

def build_script_context(export_dir: str, output_dir: str) -> dict:
    """Everything a script-conversion worker needs, built ONCE per plugin.

    Returns {'initargs': tuple for _script_worker_init, 'scpt_work': [...],
    'info_work': [...], 'qust_work': [...], 'stats': dict}.  Shared by
    convert_all_scripts (the pipeline stage) and tools/convert_scripts_subset.py
    (a filtered rebuild of named scripts for iteration), so a subset build is
    the SAME conversion as the full one — same say-timer owners, unlock plan,
    durations and menu plans — never a parallel approximation of it.
    """
    # 🛑 START FROM AN EMPTY DIRECTORY.  Which scripts the conversion produces
    # changes with the plan and with the source records, and nothing used to
    # delete the ones that stopped being generated — so they SURVIVED in
    # output/ and kept being attached.  Measured 2026-08-15: scoping
    # speaker-kind Say-timer owners stopped generating 10,268 bogus INFO
    # fragments, but the stale .psc/.pex stayed on disk, so Uriel Septim's
    # GREETING went on binding a fragment that cast him to two scripts he does
    # not carry: the line kept its subtitle on screen and never advanced to
    # its remaining two responses.
    #
    # Wiping is the honest form of that guarantee.  Every alternative
    # (prefix lists, mtime "written this run" checks) needs a growing set of
    # exceptions for the static scripts deployed alongside the generated ones,
    # and each exception is another way to keep a stale file.  After a wipe,
    # anything present IS something this run produced.  Both source and
    # compiled output are cleared: a stale .pex is worse than a stale .psc,
    # because the VM loads it whether or not the source is still there.
    if os.path.isdir(output_dir):
        import shutil
        shutil.rmtree(output_dir)
    pex_dir = os.path.dirname(output_dir)
    if os.path.isdir(pex_dir):
        for _n in os.listdir(pex_dir):
            if _n.lower().endswith('.pex'):
                try:
                    os.remove(os.path.join(pex_dir, _n))
                except OSError:
                    pass
    os.makedirs(output_dir, exist_ok=True)

    # Mesh physics facts, so a converted `playgroup` can ask whether the object
    # it animates is HELD until a script releases it (breakaway pieces,
    # constrained trap islands).  Without this the lookup silently answers 0
    # for every mesh and no trap ever gets its SetMotionType release — see
    # CrossRefGraph.needs_havok_release.
    from tes5_import.mesh_bounds import load_mesh_bounds
    from asset_convert.collision_extract import bounds_cache_is_current
    _bounds_cache = os.path.join(export_dir, 'mesh_bounds_cache.json')
    # A cache from before the HELD bit existed loads fine and answers 0 for
    # every mesh, so the release silently vanishes from every converted script.
    # This step cannot rebuild it (--scripts-only runs with no mesh scan), so
    # say so instead of emitting quietly-wrong scripts.
    if not bounds_cache_is_current(_bounds_cache):
        print("  WARNING: mesh bounds cache is missing or predates the current "
              "schema.\n"
              "           Breakaway/trap havok releases will NOT be emitted "
              "(planks and traps\n"
              "           will hang instead of falling).  Run the import or "
              "meshes step to\n"
              f"           rebuild it: {_bounds_cache}")
    load_mesh_bounds(_bounds_cache, quiet=True)

    # Deploy static scripts (TES4Polyfill + shared service-menu fragments) so
    # they compile alongside the generated ones.
    #
    # ONLY a masterless plugin (Oblivion.esm, Nehrim.esm) owns these. They are
    # plugin-independent — TES4Polyfill is Hidden with none but Global
    # functions, and the two service fragments are stateless TopicInfos — so a
    # dependent plugin shipping its own copy just duplicates the master's
    # .psc/.pex under the same script name, and whichever loads last wins.
    # Dependents still CALL them: the generated bodies reference
    # `TES4Polyfill.*` and INFO VMADs name the service fragments, both of
    # which resolve to the master's shipped copy (phase_compile puts every
    # master's source dir on the -h header path).
    static_dir = os.path.join(os.path.dirname(__file__), 'static_scripts')
    if master_names(export_dir):
        print('  Static scripts: skipped (owned by this plugin\'s master)')
        # Remove copies an older (pre-skip) build left in this plugin's
        # output. They are poison twice over: the stale .psc shadows the
        # master's fresh copy on the compile header path (Translation.esp's
        # Aug-1 TES4Polyfill had no ReleaseBreakaway, so every script calling
        # it failed to compile), and the stale .pex ships under the same
        # script name as the master's — whichever loads last wins in-game.
        if os.path.isdir(static_dir):
            for name in os.listdir(static_dir):
                if not name.endswith('.psc'):
                    continue
                stale_psc = os.path.join(output_dir, name)
                stale_pex = os.path.join(os.path.dirname(output_dir),
                                         name[:-4] + '.pex')
                for stale in (stale_psc, stale_pex):
                    if os.path.isfile(stale):
                        os.remove(stale)
                        print(f'    removed stale master-owned copy: {stale}')
    else:
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
    for sig in ('DIAL', 'INFO', 'QUST', 'SCPT', 'NPC_'):
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

    # EVERY INFO produces a fragment (Begin/End line hooks for
    # TES4Polyfill.SayLine, plus its result script / unlocks / service menu
    # when it has them) — see _info_batch.  The importer writes a VMAD on
    # every INFO to match; there is deliberately no "which INFOs" plan for the
    # two sides to disagree on.
    info_work = [r for r in by_type.get('INFO', []) if r.get('FormID')]
    qust_work = [r for r in by_type.get('QUST', []) if r.get('EditorID', '')]
    print(f'  Converting {len(scpt_work)} SCPT / {len(info_work)} INFO / '
          f'{len(qust_work)} QUST scripts...')

    # Measured spoken-line lengths: each INFO's Begin fragment reports its
    # own line's length to TES4Polyfill.SayLine, and the per-topic maximum is
    # the fallback for a line with no voice file.
    from script_convert.say_durations import scan_voice_durations
    say_durations = scan_voice_durations(export_dir)
    if say_durations:
        print(f'    voice durations: {len(say_durations)} lines/topics '
              f'measured (Say() timers)')

    # Which topics a script drives via Say/SayTo -- the ONLY INFOs that need a
    # timing fragment for TES4Polyfill.SayLine.  Computed here, before the
    # worker pool, so every process sees the same set (see scan_say_topics).
    say_topics = scan_say_topic_fids(by_type)
    ScriptConverter.say_topics = say_topics
    print(f'    script-driven topics: {len(say_topics)} '
          f'(their INFOs keep Say() timing fragments)')

    quest_script_vars = build_quest_script_vars(by_type)
    quest_edid_by_fid = {int(r.get('FormID','0'),16) & 0xFFFFFF:
                         (r.get('EditorID') or '')
                         for r in by_type.get('QUST', [])
                         if r.get('FormID')}

    # NPC-to-NPC conversation driver: the generated TES4NPCConv<plugin>.psc
    # replays Oblivion's engine-scheduled quest-advancing conversations
    # (CharacterGen 26→27, MQ16, MS91, ...).  MUST be built from the same
    # plan the importer bound the driver quest's VMAD against — the
    # message_menus/dialog_unlocks mirroring contract.  Masterless plugins
    # only, mirroring the importer's gate (a dependent plugin's copy would
    # collide with its master's script name).
    if not master_names(export_dir):
        from tes5_import.npc_conversations import (build_conversation_plan,
                                                   generate_driver_psc)
        conv_by_type = dict(by_type)
        for sig in ('ACHR', 'ACRE'):
            _p = os.path.join(export_dir, f'{sig}.txt')
            conv_by_type[sig] = (parse_export_file(_p)
                                 if os.path.exists(_p) else [])
        _stem = os.path.splitext(
            os.path.basename(os.path.normpath(export_dir)))[0]
        conv_plan = build_conversation_plan(
            conv_by_type, script_vars=quest_script_vars, plugin_stem=_stem)
        conv_psc = generate_driver_psc(conv_plan, say_durations)
        if conv_psc:
            _pname = conv_plan['script_name'] + '.psc'
            with open(os.path.join(output_dir, _pname), 'w',
                      encoding='utf-8') as f:
                f.write(conv_psc)
            print(f"    NPC conversations: {len(conv_plan['chains'])} chains "
                  f"-> {_pname} ({len(conv_plan['skipped'])} skipped)")

    # `AddTopic X` in a SCRIPT body is the third reveal route (alongside INFO
    # fragments and quest stages), so it needs the gated topic's global by
    # EditorID. Keyed off the same unlock_plan the other two use, so all three
    # set the identical global the importer binds.
    topic_unlock_globals = {}
    for d in by_type.get('DIAL', []):
        edid = (d.get('EditorID') or '').lower()
        if not edid:
            continue
        try:
            fid24 = int(d.get('FormID', '0'), 16) & 0xFFFFFF
        except (TypeError, ValueError):
            continue
        gname = unlock_plan['gated'].get(fid24)
        if gname:
            topic_unlock_globals[edid] = gname

    # Button-MessageBox plan — the SAME analysis the importer runs to author
    # the MESG records, so the Message properties emitted here bind to them.
    from .message_menus import build_message_plan
    message_menus = build_message_plan(by_type.get('SCPT', []))
    if message_menus:
        n_sites = sum(len(v) for v in message_menus.values())
        print(f'    Button menus: {n_sites} MessageBox sites in '
              f'{len(message_menus)} scripts')

    # Chargen menu plan (ShowBirthsignMenu / ShowClassMenu → modal
    # Message.Show() pages) — shared with the importer, which authors the
    # MESG records at fixed FormIDs (message_menus.build_chargen_menus).
    from .message_menus import build_chargen_menus
    chargen_menus = {}
    _bsgn_p = os.path.join(export_dir, 'BSGN.txt')
    _clas_p = os.path.join(export_dir, 'CLAS.txt')
    if os.path.exists(_bsgn_p) or os.path.exists(_clas_p):
        _spel_map = {}
        _spel_p = os.path.join(export_dir, 'SPEL.txt')
        if os.path.exists(_spel_p):
            for _r in parse_export_file(_spel_p):
                _f, _e = _r.get('FormID'), _r.get('EditorID')
                if _f and _e:
                    _spel_map[int(_f, 16) & 0xFFFFFF] = _e
        chargen_menus = build_chargen_menus(
            parse_export_file(_bsgn_p) if os.path.exists(_bsgn_p) else [],
            parse_export_file(_clas_p) if os.path.exists(_clas_p) else [],
            _spel_map)
        if chargen_menus:
            print('    Chargen menus: ' + ', '.join(
                f"{k} ({len(v['actions'])} options, {len(v['pages'])} pages)"
                for k, v in sorted(chargen_menus.items())))

    initargs = (xref, output_dir, info_reveals, service_topics,
                unlock_plan['stage_reveals'], say_durations,
                quest_script_vars, quest_edid_by_fid, topic_unlock_globals,
                message_menus, _bounds_cache, chargen_menus, say_topics)
    return {'initargs': initargs, 'scpt_work': scpt_work,
            'info_work': info_work, 'qust_work': qust_work, 'stats': stats}


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

    ctx = build_script_context(export_dir, output_dir)
    initargs = ctx['initargs']
    stats = ctx['stats']
    scpt_work, info_work, qust_work = (ctx['scpt_work'], ctx['info_work'],
                                       ctx['qust_work'])
    jobs = ([('scpt', c) for c in _chunk(scpt_work, 48)]
            + [('info', c) for c in _chunk(info_work, 128)]
            + [('qust', c) for c in _chunk(qust_work, 8)])
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

    _fix_udf_call_arg_types(output_dir)
    _comment_undeclared_identifiers(output_dir)

    total = stats['scpt_ok'] + stats['info_ok'] + stats['qust_ok']
    errs = stats['scpt_err'] + stats['info_err'] + stats['qust_err']
    print(f'\n  Script conversion complete:')
    print(f'    SCPT: {stats["scpt_ok"]}/{stats["scpt_total"]} converted')
    print(f'    INFO: {stats["info_ok"]}/{stats["info_total"]} fragments')
    print(f'    QUST: {stats["qust_ok"]}/{stats["qust_total"]} stage scripts')
    print(f'    Total: {total} converted, {errs} errors, {stats["todo_count"]} TODOs')

    return stats


_UDF_SIG_RE = re.compile(
    r'^\s*(?:\w+\s+)?Function\s+TES4Call\s*\((.*)\)\s*$', re.IGNORECASE)
_UDF_CALL_RE = re.compile(
    r'\b([A-Za-z_]\w*)\.TES4Call\(([^()]*)\)')
# Papyrus converts freely UP to these, so only a DOWNCAST needs the explicit
# `as`. Anything already this type, or a literal, is left alone.
_UDF_WIDE_TYPES = {'form', 'objectreference'}


def _fix_udf_call_arg_types(output_dir: str) -> None:
    """Insert the casts a cross-script `X.TES4Call(...)` needs to compile.

    An OBSE user function's parameter type is inferred from how its OWN body
    uses the value, so a callee that calls GetRace() takes an `Actor` while the
    caller holds the same thing in an `ObjectReference` property (TES4 spelled
    both `ref`).  Papyrus refuses that implicit downcast and fails the CALLER —
    and a script that fails to compile takes down every script declaring a
    property of its type, so one mismatch can silently disable a quest line.

    Signatures are only known once every script has been converted, which is why
    this runs here rather than in the converter: it reads each generated
    `Function TES4Call(...)` header, then rewrites the argument at each call
    site whose property is typed wider than the parameter.
    """
    if not os.path.isdir(output_dir):
        return
    # Parameter types per callee script name, e.g. TES4_Foo -> ['Actor', 'String'].
    sigs: dict = {}
    sources: dict = {}
    for name in os.listdir(output_dir):
        if not name.endswith('.psc'):
            continue
        path = os.path.join(output_dir, name)
        try:
            with open(path, encoding='utf-8') as fh:
                text = fh.read()
        except OSError:
            continue
        sources[name[:-4]] = (path, text)
        for line in text.splitlines():
            m = _UDF_SIG_RE.match(line)
            if not m:
                continue
            params = [p.strip() for p in m.group(1).split(',') if p.strip()]
            sigs[name[:-4].lower()] = [p.split()[0] for p in params if p.split()]
            break

    if not sigs:
        return

    _prop_re = re.compile(
        r'^\s*([A-Za-z_][\w]*)\s+Property\s+(\w+)\b', re.IGNORECASE)
    fixed = 0
    for script, (path, text) in sources.items():
        if '.TES4Call(' not in text:
            continue
        # The CALLER's own property types, to know what each argument is.
        prop_types = {}
        for line in text.splitlines():
            pm = _prop_re.match(line)
            if pm:
                prop_types[pm.group(2).lower()] = pm.group(1)

        def _fix(m):
            callee = m.group(1)
            # The property naming the callee is typed as that script.
            callee_type = prop_types.get(callee.lower(), '')
            want = sigs.get(callee_type.lower())
            if not want:
                return m.group(0)
            args = [a.strip() for a in m.group(2).split(',')]
            if len(args) != len(want):
                return m.group(0)
            out_args = []
            for arg, ptype in zip(args, want):
                have = prop_types.get(arg.lower(), '')
                if (have and have.lower() in _UDF_WIDE_TYPES
                        and ptype.lower() not in _UDF_WIDE_TYPES
                        and ' as ' not in arg):
                    out_args.append(f'({arg} as {ptype})')
                else:
                    out_args.append(arg)
            return f'{callee}.TES4Call({", ".join(out_args)})'

        new_text = _UDF_CALL_RE.sub(_fix, text)
        if new_text != text:
            try:
                with open(path, 'w', encoding='utf-8') as fh:
                    fh.write(new_text)
                fixed += 1
            except OSError:
                pass
    if fixed:
        print(f'    UDF call arg casts inserted in {fixed} script(s)')


# `Owner.member` at the head of a statement, which is the shape a dangling
# cross-script reference takes.  Anchored so it only sees the STATEMENT's
# subject, never an identifier deeper in an expression.
_MEMBER_STMT_RE = re.compile(r'^(\s*)([A-Za-z_]\w*)\.(\w+)')

# Names a generated script may use without declaring them: Papyrus globals,
# script-scope keywords, and the event parameters the fragments are handed.
_IMPLICIT_NAMES = {
    'game', 'debug', 'utility', 'self', 'parent', 'math', 'input',
    # 'weather' is the CLASS in `Weather.ReleaseOverride()` /
    # `Weather.GetCurrentWeather()` global calls, not a variable.
    'weather',
    'akspeakerref', 'akactionref', 'aktarget', 'akcaster', 'akaggressor',
    'akkiller', 'akactor', 'akitem', 'aksource', 'akrefself',
    'tes4polyfill', 'form', 'true', 'false', 'none',
}


def _comment_undeclared_identifiers(output_dir: str) -> None:
    """Comment out statements whose SUBJECT was never declared.

    Morroblivion's scripts contain references the mod itself never defines --
    `fbmwMQHlaaluSuccess.hortvotes`, `fbmwMVRichTrader.follownow` -- pointing at
    records that exist in no plugin, master included.  Oblivion ignored the
    dangling name silently; Papyrus rejects the whole file, and a fragment that
    fails to compile takes its quest stage with it.

    Only a statement whose leading `Owner.` is neither a declared property, a
    local, nor a Papyrus built-in is touched, so a legitimate call is never
    suppressed.  Mirrors ScriptConverter._dangling_cross_script_target, which
    handles the case where the owner DOES resolve but the variable does not.
    """
    if not os.path.isdir(output_dir):
        return
    _decl_re = re.compile(
        r'^\s*(?:\w+(?:\[\])?)\s+(?:Property\s+)?(\w+)\b', re.IGNORECASE)
    _sig_re = re.compile(r'\((.*)\)')
    commented = 0
    for name in sorted(os.listdir(output_dir)):
        if not name.endswith('.psc'):
            continue
        path = os.path.join(output_dir, name)
        try:
            with open(path, encoding='utf-8') as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue

        known = set(_IMPLICIT_NAMES)
        for line in lines:
            s = line.strip()
            low = s.lower()
            if low.startswith('scriptname'):
                continue
            if (low.startswith('function ') or low.startswith('event ')
                    or re.match(r'^\w+\s+function\s', low)):
                sm = _sig_re.search(s)
                if sm:
                    for p in sm.group(1).split(','):
                        bits = p.split()
                        if len(bits) >= 2:
                            known.add(bits[1].strip('=').lower())
                continue
            dm = _decl_re.match(s)
            if dm and not s.startswith(';'):
                known.add(dm.group(1).lower())

        changed = False
        for i, line in enumerate(lines):
            s = line.strip()
            if not s or s.startswith(';'):
                continue
            m = _MEMBER_STMT_RE.match(line)
            if not m or m.group(2).lower() in known:
                continue
            lines[i] = (f'{m.group(1)};{s}  ;NE: {m.group(2)} is not declared '
                        f'anywhere (dangling in the original mod)')
            changed = True
            commented += 1
        if changed:
            try:
                with open(path, 'w', encoding='utf-8') as fh:
                    fh.write('\n'.join(lines) + '\n')
            except OSError:
                pass
    if commented:
        print(f'    dangling references commented out: {commented} line(s)')


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


def build_quest_script_vars(by_type: dict) -> dict:
    """quest fid (low-24) -> {script-local var index: name}.

    A TES4 `GetQuestVariable` condition stores the variable's SLSD INDEX; the
    name lives only in the SCPT the quest runs. `_seq_counter_condition` needs
    the name to emit a Papyrus property reference.
    """
    script_vars = {}
    for rec in by_type.get('SCPT', []):
        try:
            sfid = int(rec.get('FormID', ''), 16) & 0x00FFFFFF
        except ValueError:
            continue
        table = {}
        i = 0
        while f'Variable[{i}].Index' in rec:
            try:
                idx = int(rec[f'Variable[{i}].Index'])
            except (TypeError, ValueError):
                i += 1
                continue
            name = rec.get(f'Variable[{i}].Name')
            if name:
                table[idx] = name
            i += 1
        if table:
            script_vars[sfid] = table

    out = {}
    for rec in by_type.get('QUST', []):
        try:
            qfid = int(rec.get('FormID', ''), 16) & 0x00FFFFFF
            scri = int(rec.get('SCRI', '') or '0', 16) & 0x00FFFFFF
        except ValueError:
            continue
        if scri in script_vars:
            out[qfid] = script_vars[scri]
    return out



_GET_QUEST_VARIABLE = 79        # TES4 func index; param2 = script-local index


def _seq_counter_condition(rec: dict):
    """(quest_edid, var_name, int_value) from this INFO's
    `GetQuestVariable <q>.<v> == N` condition, or None. Equality only — a
    `>=`/`<` gate is not a sequencer."""
    script_vars = _WORKER_CTX.get('quest_script_vars') or {}
    names = _WORKER_CTX.get('quest_edid_by_fid') or {}
    i = -1
    while True:
        i += 1
        raw = rec.get(f'Condition[{i}].Raw')
        if raw is None:
            return None
        try:
            d = bytes.fromhex(raw)
        except ValueError:
            continue
        if len(d) < 20 or struct.unpack_from('<H', d, 8)[0] != _GET_QUEST_VARIABLE:
            continue
        if (d[0] >> 5) != 0:                      # operator must be '=='
            continue
        comp = struct.unpack_from('<f', d, 4)[0]
        if comp != int(comp):
            continue
        quest_fid = struct.unpack_from('<I', d, 12)[0] & 0x00FFFFFF
        var_idx = struct.unpack_from('<I', d, 16)[0]
        name = script_vars.get(quest_fid, {}).get(var_idx)
        quest = names.get(quest_fid, '')
        if name and quest:
            return quest, name, int(comp)
    return None


def _sequence_gate(rec: dict) -> str:
    """`<quest>.<var> == <n>` guard for a polled-conversation INFO, else ''.

    These conversations are a sequencer: each INFO is gated on an exact counter
    (`GetQuestVariable CharacterGen.convCount == 8`) and its result script does
    `convCount + 1` to hand off to the next line. The counter is ALSO re-seeded
    out-of-band by quest stages ("make sure we're at the right spot", 10 of them
    in CharacterGen alone), and those stages fire off package completion —
    which lands whenever the actor arrives, not when the line ends.

    A re-seed can land while a line is still playing. The in-flight line's End
    fragment then applies `+1` to the RE-SEEDED value and overshoots:
    CharacterGen stage 12 sets convCount=8 for "What's this prisoner doing
    here?" while line 7 is still audible, line 7's fragment makes it 9, and the
    cell-door exchange never plays (verified from a runtime trace:
    `FRAG 00032B0A cnt=8` -> `cnt=9 spk=0`).

    Guarding the fragment on the counter the INFO itself requires makes the
    re-seed authoritative — a line whose turn has passed applies nothing. The
    gate is only APPLIED when the body actually steps that counter (see
    _split_counter_step): a line the quest script advances for is not a
    sequencer and must never be gated.
    """
    var = _seq_counter_condition(rec)
    if not var:
        return ''
    quest, name, value = var
    # The VARIABLE name needs the same sanitising the converter gives every
    # property it declares — TES4 allows names Papyrus reserves (`endstate`,
    # MS40) and a raw name here is a parser error.
    return (f'{_safe_property_name(quest)}.'
            f'{_safe_property_name(name)} == {value}')


_SETSTAGE_RE = re.compile(r'^\s*\w[\w.]*\.SetStage\s*\(', re.IGNORECASE)
# The conversation bookkeeping: `<quest>.<field> = <literal>` (speaker, target)
# and the counter handoff `<quest>.<field> = <quest>.<field> +/- <literal>`
# (convCount). Deliberately narrow — no calls, nothing whose value depends on
# anything a SetStage could change except the counter itself, which MUST be
# hoisted: 13 CharacterGen stage fragments RE-SEED convCount, and a `+ 1`
# landing after such a SetStage overshoots the re-seeded value (the very drift
# _sequence_gate exists to stop).
_STATE_WRITE_RE = re.compile(
    r'^\s*(?P<lhs>\w[\w.]*)\s*=\s*'
    r'(?:[-+]?[\d.]+|(?P<base>\w[\w.]*)\s*[-+]\s*[\d.]+)\s*(;.*)?$')


def _is_state_write(line: str) -> bool:
    """A bare literal assignment, or a counter step `x = x + n` on ITSELF."""
    m = _STATE_WRITE_RE.match(line)
    if not m:
        return False
    base = m.group('base')
    return base is None or base.lower() == m.group('lhs').lower()


def _split_counter_step(lines: list, seq_gate: str) -> tuple:
    """Split off the `<counter> = <counter> + n` step the sequence gate tests.

    The gate is `<quest>.<counter> == K`. Emitting that step FIRST closes the
    gate against a re-fire, so the timer release can follow immediately —
    before the body's SetStage hands control to the engine. Returns
    (counter_lines, rest) preserving order; (,[]) when there is no such step,
    in which case the caller just emits the body unchanged.
    """
    m = re.match(r'\s*(\S+)\s*==', seq_gate or '')
    if not m:
        return [], list(lines)
    counter = m.group(1)
    step = re.compile(
        r'^\s*' + re.escape(counter) + r'\s*=\s*' + re.escape(counter)
        + r'\s*[-+]\s*[\d.]+\s*(;.*)?$', re.IGNORECASE)
    idx = next((i for i, ln in enumerate(lines) if step.match(ln)), None)
    if idx is None:
        return [], list(lines)
    return [lines[idx]], lines[:idx] + lines[idx + 1:]


_STAGE_ADVANCE_RE = re.compile(
    r'^(\s*)([A-Za-z_]\w*)\.SetStage\((\d+)\)\s*(;.*)?$', re.IGNORECASE)


# `<quest>.speaker = N` / `<quest>.target = N` -- the TURN HANDOFF of a polled
# conversation.  Deliberately narrow: a bare literal assignment to a field
# whose name says it selects the next talker.
_HANDOFF_WRITE_RE = re.compile(
    r'^\s*\w[\w.]*\.(?:speaker|target)\s*=\s*'
    r'(?:-?[\d.]+|[A-Za-z_]\w*)\s*(;.*)?$', re.IGNORECASE)


def _split_turn_handoff(counter_step, gated_rest):
    """Split the turn handoff out of an End-fragment body.

    Returns (handoff, remainder): the counter step plus any speaker/target
    literal writes, and everything else in original order.

    THE HANDOFF CANNOT WAIT FOR OnEnd.  TES4's Say was SYNCHRONOUS -- it
    returned the line's length, so the result script set `convCount` /
    `speaker` and charged `convTimer` IN THE SAME FRAME THE LINE STARTED.
    The next speaker's guard (`speaker == N && convTimer <= 0`) was then
    already open, held off only by the timer -- the pacing the author wrote.

    Emitting the handoff in the End fragment makes every line pay a serial
    round trip: the next speaker cannot even LOOK until the previous line
    has completely finished.  Measured 2026-08-16 (temp/chargen_rec_5.log):

        79.11  LineBegan Renault len=2.06     <- line starts
        81.54  LineEnded Renault              <- 2.43s later
        81.57  request   Baurus ("Yessir.")   <- only now does he ask

    Moving ONLY these writes to OnBegin restores the TES4 timing.  Stage
    advances, AddTopic unlocks and item/faction changes stay in OnEnd: those
    are consequences of the line having been DELIVERED, not of whose turn
    it is.
    """
    handoff = list(counter_step)
    rest = []
    for line in gated_rest:
        if _HANDOFF_WRITE_RE.match(line):
            handoff.append(line)
        else:
            rest.append(line)
    return handoff, rest


def _stepped_gate(seq_gate, counter_step):
    """The sequence gate rewritten for AFTER the counter step has applied.

    Fragment_1 (OnBegin) now performs the handoff, so by the time
    Fragment_0 (OnEnd) runs the counter has already moved.  `convCount == 8`
    would be false and the REST of the body -- item grants, faction changes,
    the author's other result-script writes -- would be silently dropped.
    Shift the compared value by the same delta the step applies.
    """
    m = re.match(r'(.*?==\s*)(-?[\d.]+)\s*$', seq_gate or '')
    if not m or not counter_step:
        return seq_gate
    step = re.search(r'([-+])\s*([\d.]+)', counter_step[0].split('=', 1)[1])
    if not step:
        return seq_gate
    delta = float(step.group(2))
    if step.group(1) == '-':
        delta = -delta
    val = float(m.group(2)) + delta
    txt = str(int(val)) if val == int(val) else ('%g' % val)
    return m.group(1) + txt


def _split_stage_advances(body: list) -> tuple:
    """Split a sequenced fragment body into (gated writes, stage advances).

    The sequence gate exists to stop an out-of-turn `counter + 1` and stale
    speaker/target writes from clobbering a mid-line re-seed. Its original
    form swallowed the fragment's SetStage too, and that line is frequently
    the ONLY path to the next quest beat: a package-completion re-seed landed
    while CharacterGen line 11 was still audible (Say() is async), line 11's
    End fragment was rejected, its `SetStage(13)` never ran, and the quest
    stalled forever — the Emperor greeted generically and offered only
    'Rumors', with nothing in the Papyrus log because a rejected gate is
    silent by design.

    A stage advance is safe OUTSIDE the gate because it is emitted behind a
    monotonic guard: TES4 stages are flags and its GetStage returns the
    highest one set, so an authored `SetStage N` can only mean "beat N is
    reached". Past N already → the guard skips it; turn rejected but the
    advance still owed → it runs.

    Only TOP-LEVEL `<quest>.SetStage(<literal>)` lines are lifted; one nested
    in the body's own If/While block stays where the author put it.
    """
    gated, advances = [], []
    depth = 0
    for line in body:
        m = _STAGE_ADVANCE_RE.match(line) if depth == 0 else None
        if m:
            indent, quest, stage, comment = m.groups()
            advances.append(f'{indent}If {quest}.GetStage() < {stage}'
                            '  ; advance survives a rejected turn')
            advances.append(f'{indent}  {quest}.SetStage({stage})'
                            + (f'  {comment}' if comment else ''))
            advances.append(f'{indent}EndIf')
            continue
        gated.append(line)
        s = line.strip().lower()
        if s.startswith('if ') or s.startswith('while '):
            depth += 1
        elif s == 'endif' or s == 'endwhile':
            depth -= 1
    return gated, advances


def _state_writes_before_setstage(lines: list) -> list:
    """Move plain state assignments ahead of the first SetStage call.

    `SetStage(N)` executes stage N's fragment INLINE, and those fragments call
    `EvaluatePackage()`. The engine then arbitrates packages against whatever
    state is committed at that instant — so a `speaker`/`convCount` write that
    comes after the SetStage is invisible to it. CharacterGen stage 18 showed
    this directly: the package was selected and then kicked back within the
    same second (`PKGSTART 04D84D` -> `PKGCHANGE` back to `032B14`), and
    whether it stuck varied run to run purely on engine latency.

    Only literal assignments are hoisted, and only from AFTER the first
    SetStage. Anything with a call, an expression, or a conditional keeps its
    place, so no side-effecting statement is ever reordered.
    """
    first = next((i for i, ln in enumerate(lines) if _SETSTAGE_RE.match(ln)),
                 None)
    if first is None:
        return lines
    # Only hoist from a FLAT tail — a nested block (If/While) after the
    # SetStage may depend on what the stage did.
    tail = lines[first + 1:]
    if any(re.match(r'\s*(If|While|Else|ElseIf|EndIf|EndWhile)\b', ln,
                    re.IGNORECASE) for ln in tail):
        return lines
    hoist = [ln for ln in tail if _is_state_write(ln)]
    if not hoist:
        return lines
    rest = [ln for ln in tail if not _is_state_write(ln)]
    out = lines[:first] + hoist + [lines[first]] + rest
    # Moving a write ABOVE the SetStage can strand it below a `Start()` on the
    # same quest, which is the one reordering that silently destroys it:
    # Skyrim's Start() resets every Auto property, so the seeded value is gone
    # (ArenaICGrandChampion's `CrazyIdea`, 2 sites).  The converter already
    # hoists Start() above such writes, but it ran BEFORE this pass, so re-run
    # it on the reordered result.  Both passes are order-preserving elsewhere,
    # so applying it twice is idempotent.
    from .converter import ScriptConverter
    return ScriptConverter._hoist_quest_start_above_writes(
        ScriptConverter.__new__(ScriptConverter), out)


def _info_batch(records: list, output_dir: str, xref: CrossRefGraph,
                stats: dict, info_reveals: dict = None,
                service_topics: dict = None):
    """Convert a batch of INFO records into TopicInfo fragment .psc files.

    EVERY INFO gets a fragment script `TES4_TIF__<fid>` (the importer writes
    the matching VMAD on every INFO — build_vmad_info_fragment, flags 0x03):

        Fragment_1 (OnBegin)  TES4Polyfill.LineBegan(akSpeakerRef, <length>)
        Fragment_0 (OnEnd)    [unlock globals] [TES4 result script]
                              [service menu]  TES4Polyfill.LineEnded(akSpeakerRef)

    The Begin/End hooks are how a converted `set T to Say topic` learns that
    the engine has started the line and how long it is (see
    TES4Polyfill.SayLine); they carry the speaker only, so no property is
    bound and no INFO can be missed.  The TES4 result script stays in the End
    fragment: Oblivion ran an INFO's result when the line FINISHED (the CS
    wiki's own scripted-conversation recipe writes `set Q.convTimer to <pause>`
    in results as an after-line pause, which only works at end).

    info_reveals ({info_fid24: [unlock global names]}) marks AddTopic revealer
    INFOs: their End fragment sets the unlock globals. Must stay in sync with
    the VMADs the importer writes (same unlock plan).

    service_topics ({dial_formid_str: 'barter'|'training'}) marks the service-
    menu topics; fragments for their INFOs also open the corresponding menu.
    """
    info_reveals = info_reveals or {}
    service_topics = service_topics or {}
    say_durations = ScriptConverter.say_durations or {}

    for rec in records:
        result_script = rec.get('ResultScript', '')
        has_script = bool(result_script and result_script.strip())
        formid = rec.get('FormID', '')
        if not formid:
            continue
        try:
            fid24 = int(formid, 16) & 0xFFFFFF
        except (TypeError, ValueError):
            fid24 = 0
        reveals = info_reveals.get(fid24, [])
        service_kind = service_topics.get(rec.get('ParentDIAL', ''), '')
        # Skip INFOs whose fragment would do nothing.  The engine BINDS an
        # INFO's fragment script when it selects that line -- loading and
        # linking the .pex before a word is spoken -- so a fragment with no
        # behaviour is a per-line cost paid on the dialogue path.  Must stay
        # in lockstep with the importer's VMAD writer: both call this.
        if not info_needs_fragment(rec, info_reveals, service_topics):
            stats['info_total'] += 1
            stats['info_ok'] += 1
            continue
        # This line's own measured length (all of its responses, played back
        # to back).  0 when the line has no voice file: SayLine then falls
        # back to the topic's longest line.
        try:
            length = float(say_durations.get(f'info:{formid.upper()}') or 0.0)
        except (TypeError, ValueError):
            length = 0.0
        seq_gate = _sequence_gate(rec)

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
            if prop_refs:
                # Merge case-variant keys, most specific type wins — the same
                # rule the QUST-stage and standalone emitters already apply.
                # Without it this site declared whichever spelling sorted first:
                # _preload_scro_refs types a QUST SCRO as the generic `Quest`,
                # then _convert_ref adds the specific TES4_<script> type, and if
                # the two EditorID spellings differ in case they land under
                # different keys.  The generic one won and every cross-script
                # variable read through it failed ("field or property StartTimer
                # not found" on a plain Quest).
                _merged: dict[str, tuple[str, str]] = {}
                for pname, ptype in sorted(prop_refs.items()):
                    key = _safe_property_name(pname).lower()
                    if key in _merged:
                        _, ex_type = _merged[key]
                        if ex_type == 'Quest' and ptype != 'Quest':
                            _merged[key] = (pname, ptype)
                    else:
                        _merged[key] = (pname, ptype)
                for pname, ptype in sorted(_merged.values(), key=lambda x: x[0].lower()):
                    safe = _safe_property_name(pname)
                    if safe.lower() in declared:
                        continue
                    declared.add(safe.lower())
                    out_lines.append(f'{ptype} Property {safe} Auto')
            if declared:
                out_lines.append('')

            # OnBegin: the engine has selected this line and started it.
            #
            # The TURN HANDOFF belongs here, not in OnEnd: TES4's synchronous
            # Say let the result script hand the turn over in the frame the
            # line STARTED, so the next speaker's guard was already open and
            # only `convTimer` paced him.  See _split_turn_handoff.
            _begin_handoff = []
            if seq_gate and body_lines:
                _cs, _rest = _split_counter_step(body_lines, seq_gate)
                if _cs:
                    _gr, _ = _split_stage_advances(_rest)
                    _begin_handoff, _ = _split_turn_handoff(_cs, _gr)
            out_lines.append('Function Fragment_1(ObjectReference akSpeakerRef)')
            out_lines.append(
                f'  TES4Polyfill.LineBegan(akSpeakerRef, {length:g})')
            if _begin_handoff:
                out_lines.append(f"  If {seq_gate}  ; still this line's turn")
                out_lines.extend('  ' + b for b in _begin_handoff)
                out_lines.append('  EndIf')
            out_lines.append('EndFunction')
            out_lines.append('')

            # OnEnd: the line has finished.  Unlock the AddTopic-revealed
            # topics first (right before the topic menu refreshes), then the
            # TES4 result, then the "line over" hook LAST — a poll waiting on
            # this speaker must see the result's state writes before it can
            # issue the next line.
            out_lines.append('Function Fragment_0(ObjectReference akSpeakerRef)')
            for gname in reveals:
                out_lines.append(f'  {gname}.SetValue(1)')
            body_lines = _state_writes_before_setstage(body_lines)
            # A polled-conversation line whose turn has already passed (a quest
            # stage re-seeded the counter mid-line) must apply NOTHING, or its
            # `counter + 1` overshoots the re-seeded value. See _sequence_gate.
            #
            # ONLY when THIS fragment owns the handoff, i.e. its own body steps
            # the counter the gate tests (`convCount = convCount + 1`).  That
            # step is the AUTHORED marker of a sequencer.  When the QUEST
            # SCRIPT advances the variable instead, it does so as it STARTS
            # each line (`set waittimer to X.SayTo ...` then `set JiubSpeak to
            # 3`) and the value has already moved on by the time this End
            # fragment runs; gating there would discard the whole body
            # (Morroblivion's Jiub set `Guard01Stage = 1` inside an
            # `If JiubSpeak == 2` that was 3 by then, so the guard never got
            # his move package and never walked to the player).
            counter_step, rest_body = _split_counter_step(body_lines, seq_gate)
            if seq_gate and body_lines and counter_step:
                # The quest's stage advance must survive a REJECTED turn — see
                # _split_stage_advances. Only the counter/speaker state writes
                # stay inside the gate.
                gated_rest, stage_advances = _split_stage_advances(rest_body)
                # The counter step and speaker/target writes already ran in
                # Fragment_1 (OnBegin); repeating them here would apply the
                # `+ 1` twice and skip a line.  What remains must gate on the
                # STEPPED value, or it would all be silently discarded.
                _, end_rest = _split_turn_handoff(counter_step, gated_rest)
                if end_rest:
                    out_lines.append(
                        f"  If {_stepped_gate(seq_gate, counter_step)}"
                        f"  ; turn still ours (counter stepped in OnBegin)")
                    out_lines.extend('  ' + b for b in end_rest)
                    out_lines.append('  EndIf')
                out_lines.extend(stage_advances)
            else:
                out_lines.extend(body_lines)
            if service_kind:
                out_lines.append(_SERVICE_MENU_CALL[service_kind])
            out_lines.append(f'  TES4Polyfill.LineEnded(akSpeakerRef, {length:g})')
            out_lines.append('EndFunction')
            out_lines.append('')

            # GetInCell prefix-family helpers the fragment body calls by name.
            # Only a scripted INFO has a converter (and therefore a body).
            if has_script:
                out_lines.extend(conv.get_cell_family_helpers())

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
            uses_chargen_latch = False
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
                    # convert_fragment resets per-fragment state; accumulate
                    # the chargen-menu latch across all of this QF's fragments.
                    uses_chargen_latch = (uses_chargen_latch
                                          or conv._uses_chargen_menus)
                out_lines.append('EndFunction')
                out_lines.append('')

            # Insert property declarations after ScriptName line
            if uses_chargen_latch:
                # Script-scope re-entrancy latch for the modal chargen menus
                # (see the converter's ShowBirthsignMenu emission).
                out_lines.insert(2, 'Bool TES4_ChargenMenuBusy = False')
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
                # quest_globals above already declared the stage-reveal unlock
                # globals. The converter ALSO registers them now that a script
                # `AddTopic X` emits TES4Unlock_X.SetValue(1), and a stage whose
                # result script contains that AddTopic is reached by both paths
                # — so without this seed the same name is declared twice
                # ("property with `TES4Unlock_...` name already exists").
                declared = {g.lower() for g in quest_globals}
                count = 0
                for pname, ptype in sorted(merged.values(), key=lambda x: x[0].lower()):
                    safe = _safe_property_name(pname)
                    if safe.lower() in declared:
                        continue
                    declared.add(safe.lower())
                    out_lines.insert(insert_idx + count, f'{ptype} Property {safe} Auto')
                    count += 1
                out_lines.insert(insert_idx + count, '')

            # GetInCell prefix-family helpers the stage bodies call by name.
            out_lines.extend(conv.get_cell_family_helpers())

            papyrus = '\n'.join(out_lines)
            out_path = os.path.join(output_dir, f'{script_name}.psc')
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(papyrus)
            stats['qust_ok'] += scripted_count
            stats['todo_count'] += papyrus.count(';TODO')
        except Exception as e:
            stats['qust_err'] += scripted_count
            stats['errors'].append(f'QUST {edid}: {e}')


# The player, in both spellings a TES4 SCRO can carry — skipped when
# pre-loading SCRO refs, because `player`/`playerref` is a KEYWORD the converter
# emits as `Game.GetPlayer()`, never a bound property.
#
# 0x14 is PlayerRef (the placed reference); 0x07 is the player's base NPC_, and
# a script that writes `Player.AddItem` lists THAT one.  Only 0x14 was skipped,
# so 0x07 fell through to the generic path and was typed by whatever script the
# plugin attaches to the player base — giving every caller a property
# `TES4_GlobalplayerScript Property Player` that then failed to convert to
# ObjectReference at each `X.GetDistance(Player)` / `MoveTo(Player)` call site
# (242 Nehrim scripts).  Vanilla Oblivion attaches no script to the player base,
# which is why this only ever surfaced on Nehrim.
_PLAYER_FORMIDS = frozenset({'00000014', '00000007'})


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
    if fid in _PLAYER_FORMIDS:
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
                               attached_script: tuple = None,
                               alias_scripts: list = None,
                               quest_fid: int = 0) -> bytes:
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
        alias_scripts: optional [(alias_id, [(script_name, {prop: formid})])]
            binding scripts to this quest's reference aliases (how vanilla
            hosts player-side logic — JailQuestPlayerScript on JailQuest's
            alias 15, TutorialPlayerScript on TutorialEnchanting's alias 5).
        quest_fid: this quest's own output FormID.  The alias entry's
            ScriptPropertyObject names the QUEST, not the alias target —
            verified against Skyrim.esm, where every alias group's formID is
            the owning QUST's.

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
    # only once the trailing count is read).
    #
    # Each entry (xEdit wbArrayS('Aliases', ...), byte layout confirmed by
    # parsing JailQuest / TutorialEnchanting / MQSkyHavenSparring out of
    # Skyrim.esm):
    #   ScriptPropertyObject  U16 unused, S16 aliasID, U32 formID (the QUEST's)
    #   S16 Version, S16 ObjectFormat
    #   S16 script count, then that many ordinary script entries
    alias_scripts = alias_scripts or []
    buf += struct.pack('<h', len(alias_scripts))
    for alias_id, scripts in alias_scripts:
        buf += struct.pack('<HhI', 0, alias_id, quest_fid)
        buf += struct.pack('<hh', 5, 2)          # version=5, objectFormat=2
        buf += struct.pack('<h', len(scripts))
        for sname, props in scripts:
            buf += _pack_wstring(sname)
            buf += struct.pack('<B', 0)          # flags=0
            buf += struct.pack('<H', len(props))
            for pname, fid in props.items():
                buf += _pack_wstring(pname)
                buf += struct.pack('<BB', 1, 1)  # type=Object, status=Edited
                buf += struct.pack('<HhI', 0, -1, fid)

    return bytes(buf)


# `[set X to] [ref.]Say[To] [target] <TopicEDID> [flags]` in raw TES4 script
# text.  Both forms are matched in one pass; which capture holds the topic
# depends on whether this was SayTo (target first) or Say (topic first).
_TES4_SAY_RE = re.compile(
    r'\b(?:\w+\s*\.\s*)?say(to)?\s+(\w+)(?:\s+(\w+))?', re.IGNORECASE)


def scan_say_topic_fids(by_type: dict) -> set:
    """DIAL FormIDs (upper hex, as INFO.ParentDIAL stores them) whose topic a
    script drives via Say/SayTo.

    Keyed by FORMID, not EditorID: an INFO record carries only
    `ParentDIAL=000000AA`, so the emitter would otherwise have to resolve a
    name it does not have.  Resolving here also means the lookup in
    info_needs_fragment() is a plain set membership test.
    """
    names = scan_say_topics(by_type)
    if not names:
        return set()
    out = set()
    for rec in by_type.get('DIAL', []):
        edid = (rec.get('EditorID') or '').strip().lower()
        fid = (rec.get('FormID') or '').strip().upper()
        if edid and fid and edid in names:
            out.add(fid)
    return out


def scan_say_topics(by_type: dict) -> set:
    """Topic EditorIDs (lowercase) that a TES4 script drives via Say/SayTo.

    Computed ONCE, before the worker pool starts, because the fragment
    emitter and the VMAD writer run in DIFFERENT PROCESSES -- a set filled
    while converting SCPT records is invisible to the process converting
    INFO records, so this cannot be collected as a side effect of conversion.

    Every candidate is validated against the real DIAL EditorIDs: the naive
    regex also matches English prose ("say it was spiked", "say anything"),
    and Oblivion's script comments are full of it.  Requiring the token to
    name an actual topic removes those without needing a comment-aware
    parser -- measured on Oblivion.esm: 98 raw candidates -> 31 real topics.
    """
    dial_edids = {(r.get('EditorID') or '').strip().lower()
                  for r in by_type.get('DIAL', [])}
    dial_edids.discard('')
    if not dial_edids:
        return set()

    topics = set()
    for kind in ('SCPT', 'INFO', 'QUST'):
        for rec in by_type.get(kind, []):
            # Field names differ per record type in the export: a SCPT keeps
            # its body in SCTX, an INFO's result script is ResultScript, and a
            # QUST stage's is also ResultScript.  Checking all of them on
            # every record is cheaper than branching and cannot miss one.
            for field in ('SCTX', 'ResultScript', 'ScriptText'):
                text = rec.get(field) or ''
                if not text:
                    continue
                for raw in text.splitlines():
                    line = raw.split(';', 1)[0]
                    for m in _TES4_SAY_RE.finditer(line):
                        is_to = bool(m.group(1))
                        first, second = m.group(2), m.group(3)
                        # SayTo names the TARGET first, then the topic.
                        for cand in ((second, first) if is_to else (first,)):
                            if cand and cand.lower() in dial_edids:
                                topics.add(cand.lower())
                                break
    return topics


def info_needs_fragment(rec: dict, info_reveals: dict = None,
                        service_topics: dict = None) -> bool:
    """Does this INFO need a Papyrus fragment script at all?

    🛑 THE SINGLE SOURCE OF TRUTH.  The fragment EMITTER (_info_batch) and the
    VMAD WRITER (tes5_import.dialog_converter) must agree exactly: a VMAD flag
    bit with no function behind it makes the engine bind a missing function,
    and a .pex nothing attaches is dead weight.  Both call THIS.

    WHY NOT ALWAYS (the 2026-08-17 stutter fix)
    -------------------------------------------
    Every INFO used to get one, so the plugin shipped 19,278 per-INFO .pex
    files against vanilla Skyrim's ~5,500 -- 100% of INFOs carrying a fragment
    where vanilla carries one on 17.6%, and 141 bytes of VMAD per INFO against
    vanilla's 14.

    That costs time ON THE LINE-SELECTION PATH: when the engine picks a
    dialogue line it must bind that INFO's fragment -- load the .pex, link it,
    resolve its properties -- BEFORE anything is spoken.  Measured against the
    user's report, this matches every symptom the other theories could not:
    it fires on plain NPC activation (no script of ours runs, but the greeting's
    fragment is still bound), on topic selection, and on Say(); it happens even
    when the voice file is MISSING, because binding precedes playback; it warms
    up on repeat, because a bound script stays loaded; and consecutive lines
    that reuse an already-loaded fragment do not stutter.

    54% of the fragments (10,417) contained nothing but the LineBegan/LineEnded
    timing calls.  Those are only meaningful for a topic a converted SCRIPT
    drives through TES4Polyfill.SayLine, which blocks until OnBegin reports the
    line started.  A line the PLAYER picks never goes through SayLine, so its
    timing-only fragment was pure per-line cost with no behaviour attached.

    So a fragment is emitted only when it actually DOES something:
      * the INFO has a TES4 result script to run;
      * it reveals AddTopic unlock globals;
      * it opens a service (barter/training) menu;
      * or its topic is script-driven, so SayLine needs the Begin/End hooks.
    """
    from script_convert.converter import ScriptConverter
    info_reveals = info_reveals or {}
    service_topics = service_topics or {}

    result_script = (rec.get('ResultScript') or '').strip()
    if result_script:
        code = [ln for ln in result_script.splitlines()
                if ln.strip() and not ln.strip().startswith(';')]
        if code:
            return True

    try:
        fid24 = int(rec.get('FormID') or '0', 16) & 0xFFFFFF
    except (TypeError, ValueError):
        fid24 = 0
    if fid24 and fid24 in info_reveals:
        return True

    parent = (rec.get('ParentDIAL') or '').strip()
    if parent and parent in service_topics:
        return True

    # Script-driven topic: SayLine reads the line's start and length from the
    # Begin/End fragments, so these must keep theirs.
    return bool(parent) and parent.upper() in ScriptConverter.say_topics


def build_vmad_info_fragment(info_formid: str, property_values: dict = None,
                             script_name: str = None) -> bytes:
    """Build VMAD binary for an INFO record's fragment script.

    Every INFO carries BOTH fragments (flags 0x03): `Fragment_1` (OnBegin) and
    `Fragment_0` (OnEnd) — the line hooks TES4Polyfill.SayLine relies on, see
    _info_batch.  A flag bit with no matching function in the .pex makes the
    engine bind a missing function, so the emitter and this builder must never
    disagree: both are unconditional.

    Args:
        info_formid: INFO FormID string (e.g. "00012345")
        property_values: optional dict {property_name: formid} for script properties
        script_name: override the per-INFO TES4_TIF__ name with a shared static
            fragment script (e.g. TES4_ShowBarterMenu for the service-menu
            fallback INFO); the static scripts define both fragments too.

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
    #
    # 🛑 THE ENTRIES ARE POSITIONAL, NOT NAME-BOUND.  The engine walks the set
    # flag bits in order (bit0 OnBegin, then bit1 OnEnd) and binds the Nth
    # entry to the Nth set bit; the FragmentName string is arbitrary.  Verified
    # against a real Skyrim.esm: of the 250 INFOs carrying BOTH, some name them
    # ('Fragment_0','Fragment_1') and others ('Fragment_1','Fragment_0') or
    # ('Fragment_1','Fragment_2') — the order of the ENTRIES is what decides,
    # so the Begin entry must be written FIRST.  Writing them the other way
    # round runs the End body when the line starts and vice versa.
    buf += struct.pack('<b', 2)        # Extra bind data version = 2
    buf += struct.pack('<B', 0x03)     # bit0=OnBegin, bit1=OnEnd
    buf += _pack_wstring(script_name)  # FileName

    # OnBegin entry FIRST (bit0) — reports the line's start and length.
    buf += struct.pack('<B', 1)
    buf += _pack_wstring(script_name)
    buf += _pack_wstring('Fragment_1')

    # OnEnd entry (bit1) — the result script, then the line-over hook.
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
