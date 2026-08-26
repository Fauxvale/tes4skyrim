#!/usr/bin/env python3
"""Clean-room in-game test harness for converted quest / dialogue / script logic.

Test ANY quest. Nothing here is specific to one: the quest EDID is an argument,
and the whole cast -- speakers, their placed references, the stage list -- is
DISCOVERED from the export for whatever quest you name. There are no hardcoded
actor, variable or topic names anywhere in this file.

Full methodology: docs/ingame_test_methodology.md

WHY A CLEAN ROOM
----------------
Observing a bug in a live playthrough is unreliable in a way that is easy to
miss: the quest keeps advancing while you measure, other scripts write the same
variables, AI packages drag actors out of the cell, and any variable you poke to
set up a test is immediately fought over by whatever else is running. A reading
taken that way is not evidence -- it is a snapshot of several interleaved
systems, and it will support almost any theory you bring to it (measured the
hard way, 2026-08-15: a 95-second "reproduction" was taken at stage 50 of a bug
that only exists at stage 42, with hand-written baton variables, and proved
nothing at all).

So: move to an EMPTY interior, bring exactly the actors under test, drive only
the thing being tested, and reset between runs.

    python tools/live/quest_labtest.py doctor
    python tools/live/quest_labtest.py cast    --quest Charactergen
    python tools/live/quest_labtest.py setup   --quest Charactergen
    python tools/live/quest_labtest.py reset   --quest Charactergen --stage 42
    python tools/live/quest_labtest.py run     --quest Charactergen --seconds 60 \\
        --dialogue --out temp/run1.log
    python tools/live/quest_labtest.py restore

`cast` needs no game -- it is a pure export read, so the cast can be worked out
before the game is even launched.
"""

import argparse
import json
import re
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(ROOT))

from tools.live.game_bridge import Bridge, BridgeError  # noqa: E402

STATE = ROOT / 'temp' / 'labtest_state.json'

# 🛑 AnvilMarkTest is THE clean room for this project. It is the established,
# agreed test cell -- do not "improve" on it.
#
# A previous session talked itself out of it: its child GRUP carries 14 REFR
# records, so it was declared a furnished dungeon and swapped for QASmoke. That
# reasoning was wrong in the way that matters. The refs are the room's own
# geometry -- it is a TEST room, so it HAS a floor and walls, which is the whole
# requirement. What a clean room must not contain is *other actors and other
# quests' scripts*, and AnvilMarkTest contains neither.
#
# QASmoke is the worse choice precisely because it is Skyrim's live QA cell: it
# is stuffed with vanilla test actors and containers that contribute their own
# GREETING/HELLO candidates to the very topic-selection machinery a dialogue
# test is measuring.
#
# 🛑 Verify a cell with `player.getincell <cell>`, never with coordinates. Two
# different cells here both report z~7239, so a position check "confirmed" a
# room the player had never left. `getincell` also distinguishes a cell that
# does not exist at all (WITestHold: "Item 'WITestHold' not found") from one
# the coc simply failed to reach -- a distinction coordinates cannot make.
DEFAULT_CELL = 'AnvilMarkTest'

# TES4 condition functions that name WHO may speak a line. Verified by decoding
# real Condition[N].Raw blobs out of export/Oblivion.esm/INFO.txt:
#   000000000000803f480000002a3f02000000000000000000
#   -> func 72 (GetIsID), param1 0x00023F2A == NPC_ Baurus
# Only GetIsID yields a concrete actor; the rest are reported as "broad" so a
# non-actor-specific topic is visible as such instead of silently empty.
FUNC_GET_IS_ID = 72
FUNC_GET_IN_FACTION = 71
FUNC_GET_IS_RACE = 69
FUNC_GET_IS_SEX = 42
# func 58 GetStage(quest) -- the gate that says WHEN a line is live. Used to
# scope the cast to the stage under test instead of dragging in the whole quest.
FUNC_GET_STAGE = 58

# CTDA type byte: the comparison operator lives in the high 3 bits.
CTDA_OPS = {0: '==', 1: '!=', 2: '>', 3: '>=', 4: '<', 5: '<='}
BROAD_FUNCS = {FUNC_GET_IN_FACTION: 'GetInFaction',
               FUNC_GET_IS_RACE: 'GetIsRace',
               FUNC_GET_IS_SEX: 'GetIsSex'}


# --------------------------------------------------------------- export io --

def _export_dir(plugin: str) -> Path:
    d = ROOT / 'export' / plugin
    if not d.is_dir():
        raise SystemExit(f'no export for {plugin!r} at {d} -- run the export '
                         f'stage first (python convert.py -f {plugin} --export-only)')
    return d


def iter_records(path: Path):
    """Yield each record in an export .txt as a dict of KEY=VALUE.

    Streams rather than loading the file: INFO.txt alone is tens of MB and the
    harness may be asked about a quest in the middle of it.
    """
    if not path.exists():
        return
    rec = {}
    with path.open('r', encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line = line.rstrip('\n')
            if line == '---RECORD_BEGIN---':
                rec = {}
            elif line == '---RECORD_END---':
                if rec:
                    yield rec
                rec = {}
            elif '=' in line:
                k, _, v = line.partition('=')
                rec[k] = v


def find_quest(plugin: str, quest: str) -> dict:
    """Look a quest up by EditorID (case-insensitive) or FormID."""
    want = quest.strip().lower()
    want_fid = want.replace('0x', '').zfill(8) if re.fullmatch(
        r'(0x)?[0-9a-f]{1,8}', want) else None
    for rec in iter_records(_export_dir(plugin) / 'QUST.txt'):
        if (rec.get('EditorID', '').lower() == want
                or (want_fid and rec.get('FormID', '').lower() == want_fid)):
            return rec
    raise SystemExit(
        f'quest {quest!r} not found in {plugin} QUST.txt. '
        f'Note EditorIDs are case-sensitive in the game but matched '
        f'case-insensitively here; check spelling (e.g. "Charactergen").')


def decode_condition(raw_hex: str) -> dict | None:
    """Decode one 24-byte TES4 CTDA blob.

    Layout (same as tes5_import/dialog_conditions.py, verified live):
        [0]   type/flags   [4:8] comparison float
        [8:10] function u16 [12:16] param1 u32  [16:20] param2 u32
    """
    try:
        raw = bytes.fromhex(raw_hex)
    except ValueError:
        return None
    if len(raw) < 20:
        return None
    comp = struct.unpack_from('<f', raw, 4)[0]
    return {
        'type': raw[0],
        'comp': comp,
        'value': comp,
        'op': CTDA_OPS.get(raw[0] >> 5, '=='),
        'func': struct.unpack_from('<H', raw, 8)[0],
        'param1': struct.unpack_from('<I', raw, 12)[0],
        'param2': struct.unpack_from('<I', raw, 16)[0],
    }


def quest_infos(plugin: str, quest_fid: str) -> list:
    """Every INFO line attached to this quest, via QSTI.Quest."""
    want = quest_fid.lower()
    out = []
    for rec in iter_records(_export_dir(plugin) / 'INFO.txt'):
        if rec.get('QSTI.Quest', '').lower() == want:
            out.append(rec)
    return out


def speakers_from_infos(infos: list) -> tuple[dict, dict]:
    """(specific speakers, broad gates) across a quest's dialogue.

    specific: {base_formid -> line count}  from GetIsID
    broad:    {func_name  -> count}        faction/race/sex gates
    """
    specific: dict = {}
    broad: dict = {}
    for rec in infos:
        i = 0
        while True:
            raw = rec.get(f'Condition[{i}].Raw')
            if raw is None:
                break
            i += 1
            c = decode_condition(raw)
            if not c:
                continue
            if c['func'] == FUNC_GET_IS_ID and c['param1']:
                fid = f"{c['param1']:08X}"
                specific[fid] = specific.get(fid, 0) + 1
            elif c['func'] in BROAD_FUNCS:
                name = BROAD_FUNCS[c['func']]
                broad[name] = broad.get(name, 0) + 1
    return specific, broad


def stage_cast(plugin: str, qrec: dict, stage: int, window: int = 6,
               back: int = 0) -> list:
    """Placed refs the stage scripts around `stage` actually NAME.

    WHY NOT filter the dialogue by stage: measured on Charactergen, only 78 of
    296 lines carry a GetStage gate at all. The conversation is driven by func
    79 GetQuestVariable on speaker/target/convCount, so a stage window over
    INFO conditions keeps 246/296 lines and 7 speakers -- it does not scope
    anything. Scoping on it would be a heuristic dressed up as a filter.

    The AUTHORED signal is the stage's own script. Each `Stage[i].Log[j]` lists
    its `SCRO[k]` -- the forms that log entry's result script references, which
    the CK derives from the script text itself. For Charactergen stage 40 that
    is exactly BaurusRef/UrielSeptimRef/GlenroyRef + player + the quest: the
    three actors the sequence drives, named by the plugin rather than guessed.

    The window is FORWARD-looking by default (`back=0`): a test starts at a
    stage and plays onward, so the actors that matter are the ones the coming
    stages name. Reaching backwards pulls in the previous scene's cast -- a
    +/-6 window at stage 40 dragged in stage 34's two ambush assassins, who are
    dead by the time the sequence under test begins.

    Returns placed-ref FormIDs (ACHR/ACRE), skipping the player and any SCRO
    that is not an actor placement (quests, markers, factions, sounds).
    """
    refs: list[str] = []
    i = 0
    while True:
        idx = qrec.get(f'Stage[{i}].Index')
        if idx is None:
            break
        try:
            si = int(idx)
        except ValueError:
            i += 1
            continue
        if -back <= (si - stage) <= window:
            j = 0
            while True:
                if (qrec.get(f'Stage[{i}].Log[{j}].Flags') is None
                        and qrec.get(f'Stage[{i}].Log[{j}].ResultScript') is None):
                    break
                k = 0
                while True:
                    s = qrec.get(f'Stage[{i}].Log[{j}].SCRO[{k}]')
                    if s is None:
                        break
                    k += 1
                    fid = s.strip().upper().zfill(8)
                    if fid == TES4_PLAYER_BASE or fid in refs:
                        continue
                    refs.append(fid)
                j += 1
        i += 1
    return refs


def actor_refs_only(plugin: str, fids: list) -> list:
    """Keep just the FormIDs that are placed ACTOR references.

    A stage's SCRO list mixes actors with quests, markers, factions, doors and
    sounds. Only the ACHR/ACRE placements can be moved into a room, so the rest
    are dropped -- and dropping them is why the room gets 3 actors instead of 7
    entries, most of which are not actors at all.
    """
    want = {f.lower() for f in fids}
    out = []
    for sig in ('ACHR', 'ACRE'):
        for rec in iter_records(_export_dir(plugin) / f'{sig}.txt'):
            fid = rec.get('FormID', '').lower()
            if fid in want:
                out.append({'ref': rec.get('FormID', ''), 'sig': sig,
                            'base': rec.get('NAME', ''),
                            'cell': rec.get('ParentCELL', '')})
    return out


def name_for_base(plugin: str, fid: str) -> tuple[str, str]:
    """(EditorID, kind) for a base FormID, searching NPC_ then CREA."""
    want = fid.lower()
    for sig in ('NPC_', 'CREA'):
        for rec in iter_records(_export_dir(plugin) / f'{sig}.txt'):
            if rec.get('FormID', '').lower() == want:
                return rec.get('EditorID', ''), sig
    return '', ''


def placed_refs(plugin: str, base_fid: str) -> list:
    """ACHR/ACRE placements of a base actor.

    May legitimately be EMPTY: some quest-critical actors are placed purely by
    script (moveto/placeatme from a result script), so "no placement" is a real
    answer, not an error -- the harness falls back to the base record.
    """
    want = base_fid.lower()
    out = []
    for sig in ('ACHR', 'ACRE'):
        for rec in iter_records(_export_dir(plugin) / f'{sig}.txt'):
            if rec.get('NAME', '').lower() == want:
                out.append({'ref': rec.get('FormID', ''), 'sig': sig,
                            'cell': rec.get('ParentCELL', ''),
                            'pos': [rec.get('PosX'), rec.get('PosY'),
                                    rec.get('PosZ')]})
    return out


def quest_stages(qrec: dict) -> list:
    """Stage indices declared by the quest, in order."""
    stages = []
    i = 0
    while True:
        idx = qrec.get(f'Stage[{i}].Index')
        if idx is None:
            break
        try:
            stages.append(int(idx))
        except ValueError:
            pass
        i += 1
    return sorted(set(stages))


# The TES4 player base record. It does NOT convert into the output plugin --
# Skyrim's player is a vanilla Skyrim.esm form, so re-indexing it into the
# converted plugin's slot yields an id that resolves to nothing (or, worse, to
# an unrelated record). The runtime equivalent is PlayerRef 0x00000014, which is
# also what dialog_conditions.py emits for actor-typed player parameters.
TES4_PLAYER_BASE = '00000007'
SKYRIM_PLAYER_REF = '00000014'

# A plugin build that predates a command answers E_UNSUPPORTED; newer builds
# answer E_UNKNOWN_CMD (which distinguishes "this build lacks it" from "this
# runtime could not resolve the capability"). Either way the client should fall
# back to a console-based path rather than failing the whole test, so the
# harness keeps working against a DLL that is already loaded in a running game
# -- swapping the DLL costs a relaunch, which is exactly what this avoids.
MISSING_CMD_CODES = {'E_UNKNOWN_CMD', 'E_UNSUPPORTED'}


def runtime_formid(export_fid: str, index: str = '01') -> str:
    """Export FormID -> the id the converted plugin uses at runtime.

    The converter keeps the low 24 bits and re-indexes the high byte to the
    plugin's load-order slot (Oblivion.esm -> 01 when loaded after Skyrim.esm).
    Verified against the output ESM's own record header: export CELL 0003E2B8
    AnvilMarkTest -> 0103E2B8.

    The player is the exception: it is a vanilla form and keeps its own id.
    """
    if export_fid.upper().zfill(8) == TES4_PLAYER_BASE:
        return SKYRIM_PLAYER_REF
    return f'{index}{export_fid[-6:].upper()}'


# ---------------------------------------------------------------- state io --

def _load() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, indent=2))


# A ref in a different (unloaded) cell reports FLT_MAX rather than a real
# distance, so "is this actor actually here?" is decidable without any struct
# reading. Anything under a cell's worth of units means it arrived.
FLT_MAX_DISTANCE = 1e38
NEARBY_UNITS = 512.0

# How far the player may be from the scene before re-anchoring. Set from the
# authored force-greet radii: CGEmperorToPlayerA/B use 400 and
# CGEmperorGreetPlayerInCell/CGBaurusGreetPlayer use 500, so an observer beyond
# ~500 units is outside the range where the scene's own packages fire at all.
FOLLOW_UNITS = 500.0


def _distance(b: Bridge, ref: str) -> 'float | None':
    """Distance from the player to a reference, or None if unreadable.

    `player.getdistance <ref>` is player-side, so it needs no reference
    selection -- which matters because selection is exactly the mechanism that
    was silently failing. This is the harness's ground truth for whether a
    teleport actually happened.
    """
    out = (b.console(f'player.getdistance {ref}') or '').strip()
    m = re.search(r'(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)', out)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _fmt(d: 'float | None') -> str:
    if d is None:
        return '?'
    return 'ELSEWHERE' if d >= FLT_MAX_DISTANCE else f'{d:.1f}'


def detect_index(b: Bridge, probe_fid: str) -> 'str | None':
    """Find the load-order index byte the converted plugin actually has.

    The index is a property of the USER'S load order, not of the conversion, so
    assuming `01` is wrong the moment anything else is installed -- measured
    live: the plugin was at 1A, and every id the harness built from `01`
    resolved to nothing while still reporting success at the console layer.

    Probes a form we know exists (the quest under test) across the plausible
    indices and returns the one the engine recognises. The engine's own
    "Item '<id>' not found" reply is what makes this decidable.
    """
    low = probe_fid[-6:].upper()
    for idx in [f'{i:02X}' for i in range(0x01, 0x40)]:
        out = (b.console(f'getstage {idx}{low}') or '')
        if 'not found' not in out and 'GetStage' in out:
            return idx
    return None


def _qvars(b: Bridge, quest: str) -> dict:
    """Every script variable the engine prints for `quest`, plus its stage."""
    sq = b.console(f'sqv {quest}') or ''
    out = {}
    for m in re.finditer(r'^\s*(?:::)?([A-Za-z_]\w*?)(?:_var)?\s*=\s*(.*)$',
                         sq, re.M):
        out[m.group(1)] = m.group(2).strip()
    st = re.search(r'Current stage:\s*(\d+)', sq)
    if st:
        out['<stage>'] = st.group(1)
    return out


# ------------------------------------------------------------- subcommands --

def cmd_cast(args) -> int:
    """Who and what is involved in this quest -- from the export, no game."""
    q = find_quest(args.plugin, args.quest)
    qfid = q.get('FormID', '')
    infos = quest_infos(args.plugin, qfid)
    specific, broad = speakers_from_infos(infos)
    stages = quest_stages(q)

    cast = []
    for fid, lines in sorted(specific.items(), key=lambda kv: -kv[1]):
        edid, kind = name_for_base(args.plugin, fid)
        refs = placed_refs(args.plugin, fid)
        cast.append({
            'base': fid, 'editor_id': edid, 'kind': kind, 'lines': lines,
            'refs': refs,
            'runtime_base': runtime_formid(fid, args.index),
            'runtime_refs': [runtime_formid(r['ref'], args.index) for r in refs],
        })

    out = {'quest': q.get('EditorID'), 'quest_formid': qfid,
           'runtime_quest': runtime_formid(qfid, args.index),
           'infos': len(infos), 'stages': stages, 'cast': cast,
           'broad_gates': broad}

    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    print(f"quest {q.get('EditorID')}  export {qfid} -> runtime "
          f"{out['runtime_quest']}")
    print(f"  {len(infos)} dialogue line(s), {len(stages)} stage(s): "
          f"{stages if len(stages) <= 24 else str(stages[:24]) + ' ...'}")
    if not cast:
        print('\n  no actor-specific (GetIsID) speakers found -- this quest\'s '
              'lines are not locked to particular actors')
    else:
        print(f'\n  {len(cast)} speaker(s) named by GetIsID conditions:')
        for c in cast:
            if c['base'].upper().zfill(8) == TES4_PLAYER_BASE:
                where = 'the PLAYER (already present; not a test subject)'
            elif c['refs']:
                where = f"{len(c['refs'])} placement(s)"
            else:
                where = 'NO static placement (script-placed)'
            print(f"    {c['editor_id'] or '?':28} base {c['base']} "
                  f"-> {c['runtime_base']}  {c['lines']:4} line(s)  {where}")
            for r, rt in zip(c['refs'], c['runtime_refs']):
                print(f"        ref {r['ref']} -> {rt}  cell {r['cell']}")
    if broad:
        print('\n  broad (non-actor-specific) gates: '
              + ', '.join(f'{k}x{v}' for k, v in sorted(broad.items())))

    # The quest's own debug staging entries. These are what put the cast where
    # a stage expects them -- `setstage` alone moves nobody.
    presets = staging_presets(q)
    if presets:
        print(f'\n  {len(presets)} staging preset(s) authored by the quest '
              f'(--preset N):')
        for p in presets:
            print(f"    --preset {p['log']}  {p['text'] or '(no caption)'}")
            for mv in p['moves']:
                print(f'                 {mv}')

    print('\n  test these with:\n'
          f"    python tools/live/quest_labtest.py setup --quest {q.get('EditorID')}")
    return 0


def cmd_doctor(args) -> int:
    """Is the channel healthy enough to trust a test result?"""
    rows = []

    def add(name, ok, detail=''):
        rows.append((name, ok, detail))

    try:
        with Bridge().connect(retries=2) as b:
            p = b.ping()
            add('bridge pipe', True, f"plugin {p.get('plugin_version')}")
            caps = b.capabilities().get('capabilities', {})
            add('console execution', bool(caps.get('console')))
            add('output capture', bool(caps.get('output_capture')),
                'without this every command reads as empty')
            add('papyrus capture', bool(caps.get('papyrus_capture')))
            loaded = b.ping().get('game_loaded')
            add('game loaded', bool(loaded), 'coc from the main menu also works')
            if caps.get('console'):
                # A real query proves the whole path, not just that it resolved.
                out = b.console('getgs fJumpHeightMin') or ''
                add('console round trip', bool(out.strip()),
                    out.strip()[:60] or 'no output -- run '
                    'tools/live/game_input.py bootstrap once per session')
    except BridgeError as exc:
        add('bridge pipe', False, str(exc))

    ok_all = all(ok for _, ok, _ in rows)
    if args.json:
        print(json.dumps({'ok': ok_all,
                          'checks': [{'name': n, 'ok': o, 'detail': d}
                                     for n, o, d in rows]}, indent=2))
        return 0 if ok_all else 1
    for name, ok, detail in rows:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:22} {detail}")
    print(f"\n{'READY' if ok_all else 'NOT READY'}")
    return 0 if ok_all else 1


def cmd_setup(args) -> int:
    """Build the clean room: coc to the empty cell, bring the cast in."""
    state = _load()
    state.setdefault('moved', [])
    state.setdefault('spawned', [])

    with Bridge().connect(retries=4) as b:
        # Work out the cast unless the caller named refs explicitly.
        refs: list[str] = list(args.actor)
        if not refs and args.quest:
            q = find_quest(args.plugin, args.quest)

            # The load-order index is a property of the USER'S setup, so it is
            # asked of the running game rather than assumed. Getting this wrong
            # builds ids that resolve to nothing while every console call still
            # reports success (measured: the plugin was at 1A, not 01).
            index = args.index
            if args.auto_index:
                found = detect_index(b, q.get('FormID', ''))
                if found:
                    if found != index:
                        print(f'  load-order index detected as {found} '
                              f'(not {index}); using it')
                    index = found
                else:
                    print(f'  ** could not detect the load-order index; '
                          f'falling back to {index}. Is the converted plugin '
                          f'actually enabled?')
            state['index'] = index

            # Scope the cast to the stage under test. The whole-quest speaker
            # list is the whole quest -- for Charactergen that is 7 actors
            # across stages 0-88, when the sequence being tested involves 3.
            # Extra actors are not neutral: they bring their own
            # GREETING/HELLO candidates into the topic selection under test.
            stage = getattr(args, 'stage', None)
            scoped = []
            if stage is not None:
                named = stage_cast(args.plugin, q, stage)
                scoped = actor_refs_only(args.plugin, named)
                for p in scoped:
                    nm, _k = name_for_base(args.plugin, p['base'])
                    print(f'  stage {stage} script names {nm or p["base"]} '
                          f'(ref {p["ref"]})')
                    refs.append(runtime_formid(p['ref'], index))

            if not scoped:
                # No stage given (or its scripts name no actors): fall back to
                # every speaker in the quest.
                infos = quest_infos(args.plugin, q.get('FormID', ''))
                specific, _ = speakers_from_infos(infos)
                for fid, _lines in sorted(specific.items(),
                                          key=lambda kv: -kv[1]):
                    # The player is already in the room by definition, and is
                    # not a movable test subject.
                    if fid.upper().zfill(8) == TES4_PLAYER_BASE:
                        continue
                    placements = placed_refs(args.plugin, fid)
                    if placements:
                        # Quest properties point at PLACED refs, so move those.
                        refs.append(runtime_formid(placements[0]['ref'], index))
                    elif args.spawn:
                        refs.append(runtime_formid(fid, index))
            if args.max_actors:
                refs = refs[:args.max_actors]
            state['quest'] = q.get('EditorID')

        if not refs:
            print('no actors resolved -- pass --actor <formid> explicitly, or '
                  'use --spawn to allow script-placed actors to be spawned as '
                  'copies', file=sys.stderr)

        if 'origin' not in state:
            state['origin'] = {
                'player': [(b.console(f'player.getpos {ax}') or '').strip()
                           for ax in 'xyz'],
            }

        print(f'coc {args.cell}')
        b.console(f'coc {args.cell}')
        _wait_ready(b, args.load_wait)

        for ref in refs:
            if args.spawn:
                # Engine-side spawn so the created reference is TRACKED and can
                # actually be removed later; console placeatme does not report
                # the ref it made.
                try:
                    r = b.request('spawn', form_id=ref, count=1)
                    made = r.get('refs') or []
                    state['spawned'].extend(made)
                    print(f'  spawned {ref} -> {made}')
                except BridgeError as exc:
                    if exc.code in MISSING_CMD_CODES:
                        out = b.console(f'player.placeatme {ref} 1') or ''
                        print(f'  spawned {ref} (untracked, old plugin): '
                              f'{out.strip()[:60]}')
                    else:
                        print(f'  spawn {ref} FAILED: {exc}')
            else:
                # Move the REAL reference. Quest scripts bind their properties
                # to specific placed refs, so a spawned copy -- a different
                # FormID -- is invisible to the quest and useless for testing.
                before = _distance(b, ref)
                pos = [(b.console('getpos ' + ax, ref=ref) or '').strip()
                       for ax in 'xyz']
                # Keep the FIRST recorded position: it is the actor's real home,
                # and overwriting it with a position captured after an earlier
                # test would make `restore` return the actor to the test cell.
                if not any(m['ref'].upper() == ref.upper()
                           for m in state['moved']):
                    state['moved'].append({'ref': ref, 'pos': pos})
                b.console('moveto player', ref=ref)
                time.sleep(0.6)
                after = _distance(b, ref)
                # 🛑 VERIFY, never trust the return value. A ref-targeted
                # command reports success even when it acts on nothing (that
                # bug hid here for a whole session), so the only honest check
                # is that the actor actually got closer.
                # Two conditions, both required. Proximity alone is a FALSE
                # POSITIVE: an actor that was already 338 units away reads as
                # "near" without having moved at all, which is exactly how the
                # broken selection escaped notice. `moveto player` puts the
                # target essentially on top of the player, so a real move both
                # lands close AND changes the distance.
                moved = (before is None or after is None
                         or abs(before - after) > 1.0)
                if after is not None and after < NEARBY_UNITS and moved:
                    print(f'  moved real ref {ref}  distance {_fmt(before)} '
                          f'-> {after:.1f}')
                else:
                    print(f'  ** ref {ref} DID NOT MOVE (distance '
                          f'{_fmt(before)} -> {_fmt(after)}). The loaded DLL '
                          f'may predate the target fix -- relaunch the game '
                          f'with the rebuilt TESGameBridge.dll.')
            time.sleep(0.3)
    _save(state)
    print(f'\nsetup complete ({len(refs)} actor(s)); state -> {STATE}')
    return 0


def _wait_ready(b: Bridge, seconds: float) -> None:
    """Block until the game answers again after a load screen.

    `coc` triggers a load; every command issued during it fails with E_LOADING.
    Polls a cheap read-only command rather than sleeping a fixed time.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if (b.console('player.getav health') or '').strip():
                return
        except BridgeError:
            pass
        time.sleep(0.5)


def cmd_reset(args) -> int:
    """Stop, reset and re-seed the quest so the next run starts clean."""
    with Bridge().connect(retries=4) as b:
        print(f'resetting {args.quest}')
        _do_reset(b, args.quest, args.stage, args.set)
        v = _qvars(b, args.quest)
        print(f"after reset: stage={v.get('<stage>')} ({len(v)} field(s))")
    return 0


def cmd_run(args) -> int:
    """Observe for a bounded time, writing a transition transcript.

    PASSIVE: writes nothing to the quest during the window, because a write
    during measurement is exactly what makes a reading untrustworthy. Set-up
    writes belong in `reset`, before the window opens.
    """
    emit, close = _emitter(args.out)
    try:
        with Bridge().connect(retries=4) as b:
            reader = _dialogue_reader(emit) if args.dialogue else None
            _run_window(b, args.quest, args.seconds, args.interval, emit,
                        reader, args.ring_limit)
    finally:
        close()
    if args.out:
        print(f'\ntranscript -> {args.out}', file=sys.stderr)
    return 0


# ---------------------------------------------------------------- recording --
# A live playthrough is the one thing a clean-room trial cannot reproduce: the
# player's own timing IS the input. `record` therefore watches a REAL session
# the user is playing and writes a correlated timeline, without writing
# anything to the game.
#
# 🛑 WHAT TO RECORD IS THE WHOLE DESIGN. A transcript of quest variables alone
# cannot explain a package-arbitration bug, because the losing and winning
# packages never appear in it. The signal that decides "who stole the
# conversation" is WHICH PACKAGE EACH ACTOR IS RUNNING, sampled against the
# dialogue menu state -- so both are polled on one clock and stamped into one
# file. Everything else (stage, quest vars, VM output) is context for that.

# `getcurrentaipackage` reports a package INDEX, not a FormID: -1 means no
# package is running at all, which is itself a real and distinct observation
# (an actor with no package is idle, not force-greeting). Identity comes from
# `getiscurrentpackage <formid>`, probed per candidate.
_RE_NUM = re.compile(r'>>\s*(-?\d+(?:\.\d+)?)')

# The recorder's own probes print into the shared console ring, so without
# this every tick would echo its own getdistance/getcurrentaipackage/sqv output
# back into the transcript and bury the real events.
_PROBE_ECHO = ('GetDistance >>', 'Current Process >>', 'GetIsCurrentPackage >>',
               'GetIsInDialogueWithPlayer >>', 'GetInDialogueWithPlayer >>',
               'GetStage >>', 'GetDead >>', '--- Quest state ---',
               'Current stage:', 'Priority:', 'Enabled?', 'State:',
               'is not in dialogue state with player',
               'Script state =', 'Quest state', 'Object Reference state',
               '--- Papyrus ---', 'Registered for update every',
               'Aliases for quest', 'TES4_QF_', 'TES4_CharGenQuest',
               'Current crime gold', 'Stolen Item Value')

# `sqv` dumps the quest's ENTIRE alias and variable table every call -- 64
# TES4Target aliases plus every script variable. That is the recorder's own
# probe echoing back through the shared console ring, and it dwarfs everything
# else: measured 4,621 lines in 12 seconds, 3,969 of them `REF 'TES4TargetN'`.
# At that rate a 50-minute recording is over a million lines of our own noise
# and the real events are unfindable. The VAR/STAGE rows already carry every
# value `sqv` prints, so dropping the raw echo loses nothing.
_RE_SQV_ECHO = re.compile(
    r"^\s*(REF\s+'[^']*'\s*->|::|TES4_\w+\s*=|\w+\s*=\s*$)")


def _num(out: str):
    """The number an ObScript query printed, or None.

    Every reader here goes through this rather than trusting a return value:
    a ref-targeted command reports success even when it selected nothing, so
    the printed number is the only honest evidence the probe ran.
    """
    m = _RE_NUM.search(out or '')
    return float(m.group(1)) if m else None


def quest_packages_for(plugin: str, quest_fid: str, index: str) -> list:
    """Every PACK gated on this quest: [{edid, formid, runtime, gate}].

    Discovered from the export, never hardcoded: a package belongs to the quest
    if one of its own conditions names the quest (GetStage/GetStageDone/
    GetQuestRunning/GetQuestVariable). That is the same authored signal
    pack_aliases.py uses to decide ownership, so the recorder watches exactly
    the set the converter treated as the quest's.
    """
    want = int(quest_fid, 16)
    out = []
    for rec in iter_records(_export_dir(plugin) / 'PACK.txt'):
        gates = []
        i = 0
        while True:
            raw = rec.get(f'Condition[{i}].Raw')
            if raw is None:
                break
            i += 1
            c = decode_condition(raw)
            if not c or c['func'] not in QUEST_PARAM_FUNCS:
                continue
            if c['param1'] == want:
                gates.append(f"{QUEST_PARAM_FUNCS[c['func']]} "
                             f"{c['op']} {c['comp']:g}")
        if gates:
            fid = rec.get('FormID', '')
            out.append({'edid': rec.get('EditorID', ''), 'formid': fid,
                        'runtime': runtime_formid(fid, index),
                        'gate': ' AND '.join(gates)})
    return out


# TES4 condition functions whose param1 is a QUEST FormID. Same set
# pack_aliases.py uses to attribute a package to its quest.
QUEST_PARAM_FUNCS = {56: 'GetQuestRunning', 58: 'GetStage',
                     59: 'GetStageDone', 79: 'GetQuestVariable'}


def info_text_index(plugin: str, quest_fid: str, index: str) -> dict:
    """{runtime INFO FormID -> (speaker text, response text)} for a quest.

    🛑 THIS IS WHY NOTHING IS COMPILED INTO THE GAME.

    "Which line was spoken" looks like it needs a probe inside the running
    scripts, and instrumenting the shipped Papyrus is the obvious way to get
    it. It is also the wrong way: it pollutes the artifact the user ships and
    costs a relaunch, which is exactly the round trip the bridge exists to
    remove.

    The engine already tells us the INFO identity for free (the offered-topic
    entries carry it, and the console/VM rings carry fragment ids), and the
    RESPONSE TEXT of every one of those INFOs is sitting in the export --
    119,385 Response lines for Oblivion.esm, all 296 of Charactergen's. So the
    live side records IDENTITY only, and the text is joined in afterwards.
    Zero game-side changes, full fidelity.
    """
    out = {}
    want = quest_fid.lower()
    for rec in iter_records(_export_dir(plugin) / 'INFO.txt'):
        if rec.get('QSTI.Quest', '').lower() != want:
            continue
        fid = rec.get('FormID', '')
        if not fid:
            continue
        texts = []
        j = 0
        while True:
            t = rec.get(f'Response[{j}].ResponseText')
            if t is None:
                break
            if t.strip():
                texts.append(t.strip())
            j += 1
        if texts:
            out[runtime_formid(fid, index).upper()] = ' / '.join(texts)
            out[fid.upper().lstrip('0')] = ' / '.join(texts)
    return out


def _vm_new_lines(lines: list, tail: list) -> tuple:
    """(lines not seen last poll, the new tail) for a FIXED-SIZE ring.

    🛑 A `len(lines)` cursor is WRONG here and silently emits nothing.

    `vmlog` returns a ring buffer with no sequence number (measured: keys are
    exactly count/lines/source -- unlike `console_log`, which does carry `seq`).
    Once the ring is full its length pins at the limit forever, so `seen =
    len(lines)` stops advancing and every subsequent poll looks like "no new
    output". Measured live before this fix: the ring was ALREADY full at 200
    lines, so a long recording would have logged zero VM events start to finish
    while looking perfectly healthy.

    Overlap by CONTENT instead: find where last poll's tail sits in the current
    ring and take everything after it. If the tail is gone entirely the ring
    wrapped completely between polls -- output was genuinely lost, so that is
    reported rather than papered over.
    """
    if not lines:
        return [], tail
    if not tail:
        return list(lines), list(lines)

    # The ring SHIFTS LEFT as it advances, so the join is found by sliding the
    # previous snapshot over the current one: if the ring advanced by k, then
    # tail[k:] lines up with the front of the current ring and everything from
    # lines[len(tail)-k:] is new.
    #
    # Slide from k=0 (nothing advanced) upwards and take the FIRST match, i.e.
    # the smallest advance consistent with the data. Preferring the largest
    # overlap is what makes repeated identical spam ('x' * 200) read as a huge
    # advance of phantom lines.
    for k in range(0, len(tail)):
        overlap = tail[k:]
        n = len(overlap)
        if n > len(lines):
            continue
        if lines[:n] == overlap:
            return list(lines[n:]), list(lines)
    # No alignment at all: the ring turned over completely since the last poll,
    # so output was genuinely lost. Say so rather than pretending continuity.
    return ([_VM_GAP_MARKER] + list(lines)), list(lines)


# The PREVIOUS POLL'S WHOLE RING is kept, not a short tail. A truncated tail
# cannot align: the overlap test needs a suffix of the old ring to match the
# FRONT of the new one, and a 64-line tail against a 200-line ring never can --
# which made every poll report a full wrap. (Caught by the unit test below
# before any recording was made; it would have produced 200 phantom lines per
# poll instead of the truth.)
_VM_GAP_MARKER = ('(VM ring wrapped between polls -- output was lost; '
                  'raise --ring-limit or lower --interval)')


# The AUTHORED force-greet radii for this scene: CGEmperorToPlayerA/B use 400
# and CGEmperorGreetPlayerInCell / CGBaurusGreetPlayer use 500. Reporting which
# band an actor is in answers "could this package have fired?", which raw
# distance drift does not.
_DIST_BANDS = ((150.0, 'talking-range'), (400.0, 'within-400'),
               (500.0, 'within-500'), (2000.0, 'near'))


def _dist_band(d) -> str:
    """Coarse distance band, so drift inside a band is not an event."""
    if d is None:
        return '?'
    if d >= FLT_MAX_DISTANCE:
        return 'ELSEWHERE'
    for limit, name in _DIST_BANDS:
        if d < limit:
            return name
    return 'far'


def _is_timer_drain(old, new) -> bool:
    """True if this is a countdown ticking DOWN (noise), not being set (event).

    A timer being SET jumps upward and means "a new line just started" -- that
    is the event worth recording. The monotonic drain toward zero that follows
    is the same fact repeated every poll, and at a 0.4s interval it outnumbers
    real transitions roughly 4:1. Reaching zero is kept: it is when the timer
    EXPIRES, which is what releases the next line.
    """
    try:
        o, n = float(old), float(new)
    except (TypeError, ValueError):
        return False
    return n < o and n > 0.0


_RE_FID = re.compile(r'\b[0-9A-Fa-f]{6,8}\b')


def _emit_line_texts(line: str, info_text: dict, emit) -> None:
    """Print the spoken text for any INFO id mentioned in an engine log line.

    Converted fragment scripts are named after their source INFO, so a VM or
    console line naming one identifies the line that PLAYED -- which is the
    ground truth a topic offer cannot give (an offered topic may never be
    chosen).
    """
    seen = set()
    for tok in _RE_FID.findall(line):
        key = tok.upper()
        txt = info_text.get(key) or info_text.get(key.lstrip('0'))
        if txt and key not in seen:
            seen.add(key)
            emit('LINE', f'{tok} "{txt[:160]}"')


def _actor_probe(b: Bridge, ref: str, packs: list) -> dict:
    """One actor's arbitration-relevant state, in as few commands as possible.

    Only the package INDEX and distance are read every tick. Identifying which
    package is running costs one command PER CANDIDATE, so it is done only when
    the index says a package is actually running AND the index changed -- a
    full identity sweep every tick would take longer than the poll interval and
    the timeline would stop being a timeline.
    """
    return {
        'pkg_index': _num(b.console('getcurrentaipackage', ref=ref)),
        'distance': _num(b.console(f'player.getdistance {ref}')),
        # 🛑 `isindialoguewithplayer`, NOT a guessed name. The console reports a
        # bad command by PRINTING, so a typo reads as a clean 0 and every row
        # of the timeline would silently say "nobody is talking".
        'talking': _num(b.console('isindialoguewithplayer', ref=ref)),
    }


def _identify_package(b: Bridge, ref: str, packs: list) -> str:
    """Which of the quest's packages this actor is running right now.

    Returns the EditorID, or '' if none of the quest's packages match (the
    actor is running something outside this quest -- also a real answer, and
    the one that says "this is not a quest-package problem at all").
    """
    for p in packs:
        if _num(b.console(f"getiscurrentpackage {p['runtime']}", ref=ref)):
            return p['edid']
    return ''


def cmd_record(args) -> int:
    """Record a LIVE playthrough: packages + dialogue + stage, on one clock.

    🛑 STRICTLY PASSIVE. It never writes to the quest, never moves an actor and
    never touches the player -- the user is playing, and any write would make
    the recording a measurement of the harness instead of the bug.

    This is the counterpart to `trial`: `trial` reproduces a scene the harness
    drives, `record` captures one the USER drives. For a bug whose trigger is
    the player's own timing inside a conversation, only the second can show it.
    """
    q = find_quest(args.plugin, args.quest)
    qfid = q.get('FormID', '')

    emit, close = _emitter(args.out)
    try:
        with Bridge().connect(retries=4) as b:
            index = args.index
            found = detect_index(b, qfid)
            if found:
                if found != index:
                    emit('SETUP', f'load-order index is {found} (not {index})')
                index = found
            else:
                emit('SETUP', f'** could not detect the load-order index; '
                              f'using {index}. Is the plugin enabled?')

            packs = quest_packages_for(args.plugin, qfid, index)
            emit('SETUP', f'{len(packs)} package(s) gated on {args.quest}')

            # The cast: whoever the caller named, else every actor the quest's
            # dialogue names via GetIsID (discovered, not hardcoded).
            cast = {}
            if args.actor:
                for a in args.actor:
                    cast[a.upper()] = a.upper()
            else:
                infos = quest_infos(args.plugin, qfid)
                specific, _ = speakers_from_infos(infos)
                for fid, _n in sorted(specific.items(), key=lambda kv: -kv[1]):
                    if fid.upper().zfill(8) == TES4_PLAYER_BASE:
                        continue
                    for pl in placed_refs(args.plugin, fid)[:1]:
                        edid, _k = name_for_base(args.plugin, fid)
                        cast[runtime_formid(pl['ref'], index)] = edid or fid
                    if len(cast) >= args.max_actors:
                        break
            for ref, nm in cast.items():
                emit('SETUP', f'watching {nm} ({ref})')

            # Spoken-line text, joined from the export rather than probed from
            # instrumented scripts. See info_text_index().
            info_text = info_text_index(args.plugin, qfid, index)
            emit('SETUP', f'{len(info_text) // 2} dialogue line(s) resolvable '
                          f'to their spoken text')

            reader = _dialogue_reader(emit)
            emit('BEGIN', f'>>> PLAY NOW -- recording {args.seconds:.0f}s <<<')

            b.vmlog(arm=True)
            last: dict = {}
            last_topics = None
            last_qv = {k: v for k, v in _qvars(b, args.quest).items()
                       if k not in ('TES4_LastTick', 'TES4_SecondsPassed',
                                    'TES4_Now')}
            emit('START', f"stage={last_qv.get('<stage>')}")
            vm_tail: list = []
            con_seq = None
            t_start = time.time()
            deadline = t_start + args.seconds
            last_beat = t_start

            while time.time() < deadline:
                time.sleep(args.interval)

                # A whole-playthrough recording spans load screens, a new-game
                # start and menu time, any of which can drop the pipe. Dying
                # there would cost the user the entire run, so the loop
                # RECONNECTS instead -- and says so, because a silent gap in a
                # timeline is indistinguishable from "nothing happened".
                if b._f is None:
                    try:
                        b.connect(retries=3, retry_delay=1.0)
                        emit('LINK', 'reconnected to the bridge')
                        b.vmlog(arm=True)
                        vm_tail = []
                    except BridgeError:
                        emit('LINK', 'bridge down; retrying')
                        time.sleep(2.0)
                        continue

                # A heartbeat makes a quiet stretch readable as "recorder alive,
                # nothing changed" rather than "recorder died 20 minutes ago".
                now = time.time()
                if args.heartbeat and (now - last_beat) >= args.heartbeat:
                    mins = (now - t_start) / 60.0
                    emit('ALIVE', f'{mins:.1f} min elapsed, '
                                  f'stage={last_qv.get("<stage>")}')
                    last_beat = now

                # Dialogue menu first: it is the fastest-moving signal and the
                # one the bug is defined in terms of.
                if reader is not None:
                    try:
                        cur = reader.summary()
                        if cur != last_topics:
                            emit('TOPIC', cur)
                            # Resolve any INFO id in the offer list to the text
                            # that line actually says, joined from the export.
                            for tok in re.findall(r'\b[0-9A-F]{8}\b', cur):
                                txt = info_text.get(tok.upper())
                                if txt:
                                    emit('LINE', f'{tok} "{txt[:160]}"')
                            last_topics = cur
                    except Exception:
                        pass

                for ref, nm in cast.items():
                    try:
                        st = _actor_probe(b, ref, packs)
                    except BridgeError:
                        # E_TRANSPORT closes the handle; the reconnect at the
                        # top of the loop picks it up next tick.
                        break
                    prev = last.get(ref, {})
                    pi = st['pkg_index']
                    if pi != prev.get('pkg_index'):
                        # The index changed, so the identity may have too --
                        # this is the ONLY point worth paying for a sweep.
                        who = (_identify_package(b, ref, packs)
                               if pi is not None and pi >= 0 else '')
                        emit('PKG', f'{nm:12} package {prev.get("pkg_index")} '
                                    f'-> {pi}  {who or "(not a quest package)"}')
                    if st['talking'] != prev.get('talking'):
                        emit('TALK', f'{nm:12} talking-state '
                                     f'{prev.get("talking")} -> {st["talking"]}')
                    # Distance is reported against a BAND, not per-tick delta:
                    # a walking actor crosses the delta threshold every poll
                    # and produced 87 of the first 500 lines. What matters for
                    # a force-greet is which RADIUS the actor is inside (the
                    # authored triggers are 400 and 500 units), so a band
                    # change is an event and drift within a band is not.
                    pd, cd = prev.get('distance'), st['distance']
                    if _dist_band(pd) != _dist_band(cd):
                        emit('MOVE', f'{nm:12} distance {_fmt(pd)} -> '
                                     f'{_fmt(cd)}  [{_dist_band(cd)}]')
                    last[ref] = st

                try:
                    cur_qv = {k: v for k, v in _qvars(b, args.quest).items()
                              if k not in ('TES4_LastTick', 'TES4_SecondsPassed',
                                           'TES4_Now')}
                except BridgeError as exc:
                    emit('GAP', str(exc))
                    continue
                for k in sorted(set(last_qv) | set(cur_qv)):
                    ov, nv = last_qv.get(k), cur_qv.get(k)
                    if ov == nv:
                        continue
                    # A countdown timer changes EVERY poll, so logging the drain
                    # buries the events: measured 48 of 187 lines in the first
                    # 25 seconds were one convtimer ticking down. Only a timer
                    # being SET (a jump UPWARDS = a new line starting) is an
                    # event; the drain toward zero is not. Same rule
                    # _run_window applies, but value-based so it catches any
                    # timer regardless of name.
                    if _is_timer_drain(ov, nv):
                        continue
                    emit('STAGE' if k == '<stage>' else 'VAR',
                         f'{k} {ov} -> {nv}')
                last_qv = cur_qv

                # The CONSOLE ring is a separate, always-on 4000-line buffer
                # that DOES carry a monotonic `seq`, so unlike the VM ring it
                # can be read exactly rather than by overlap. It catches
                # anything the engine prints that the VM sink never sees.
                if args.console_log:
                    try:
                        cl = b.console_log(limit=args.ring_limit)
                        seq = int(cl.get('seq', 0))
                        if con_seq is None:
                            con_seq = seq
                        elif seq > con_seq:
                            produced = seq - con_seq
                            got = cl.get('lines', [])
                            for ln in (got[-produced:]
                                       if produced <= len(got) else got):
                                if not ln.strip():
                                    continue
                                # Our own probe output would otherwise flood the
                                # transcript: every tick issues getdistance /
                                # getcurrentaipackage / sqv.
                                if any(p in ln for p in _PROBE_ECHO):
                                    continue
                                if _RE_SQV_ECHO.match(ln):
                                    continue
                                if any(pat.lower() in ln.lower()
                                       for pat in args.vm_mute):
                                    continue
                                emit('CON', ln.rstrip())
                                _emit_line_texts(ln, info_text, emit)
                            con_seq = seq
                    except BridgeError:
                        pass

                try:
                    lines = b.vmlog(limit=args.ring_limit).get('lines', [])
                    fresh, vm_tail = _vm_new_lines(lines, vm_tail)
                    for ln in fresh:
                        # Other mods share the VM sink, and a chatty one buries
                        # the timeline: an 8s smoke test produced ~30 lines of
                        # one bard mod's OnInit spam and nothing else was
                        # readable. Filtering is by SUBSTRING the caller names,
                        # never by a built-in list -- silently dropping VM
                        # output would hide the converted scripts we came for.
                        if not ln.strip():
                            continue
                        if any(pat.lower() in ln.lower() for pat in args.vm_mute):
                            continue
                        emit('VM', ln.rstrip())
                        _emit_line_texts(ln, info_text, emit)
                except BridgeError:
                    pass

            emit('END', 'recording complete')
    finally:
        close()
    if args.out:
        print(f'\ntranscript -> {args.out}', file=sys.stderr)
    return 0


def _run_window(b: Bridge, quest: str, seconds: float, interval: float,
                emit, reader=None, ring_limit: int = 200,
                follow: list | None = None) -> list:
    """One passive observation window. Returns the transition list.

    Shared by `run`, `trial` and `ab` so an A/B comparison is guaranteed to be
    measuring the two runs the same way -- if the observation differed between
    trials, the diff would be an artefact of the harness rather than of the
    change under test.

    `follow` names the cast to keep the PLAYER near. This is the one thing the
    window does that is not strictly passive, and it is deliberate: the scene
    walks to its next marker during the window, and an observer left behind
    measures an empty corridor. Re-anchoring moves only the player -- it never
    touches the actors or the quest -- so it cannot alter what is being
    measured, only whether it is visible. Each re-anchor is logged as FOLLOW so
    the transcript shows exactly when the viewpoint changed.
    """
    # Countdown timers tick EVERY poll, so logging each change buries the real
    # events: one 50s window produced 302 transitions, ~280 of them convTimer
    # decrements. Only a timer being SET (a jump upwards, i.e. a new line
    # starting) is an event; the drain towards zero is not.
    noisy = {'TES4_LastTick', 'TES4_SecondsPassed', 'TES4_Now'}
    events: list = []
    last = {k: v for k, v in _qvars(b, quest).items() if k not in noisy}
    emit('START', f"{quest} stage={last.get('<stage>')} ({len(last)} fields)")
    last_topics = None
    vm_seen = 0
    t0 = time.time()
    deadline = t0 + seconds
    while time.time() < deadline:
        time.sleep(interval)
        if follow:
            try:
                near = [r for r in follow
                        if (_distance(b, r) or 1e9) < FOLLOW_UNITS]
                if not near:
                    follow_scene(b, follow, emit)
            except BridgeError:
                pass
        if reader is not None:
            try:
                cur = reader.summary()
                if cur != last_topics:
                    emit('TOPIC', cur)
                    events.append(('TOPIC', cur))
                    last_topics = cur
            except Exception:
                pass
        try:
            cur = {k: v for k, v in _qvars(b, quest).items() if k not in noisy}
        except BridgeError as exc:
            emit('GAP', str(exc))
            continue
        for k in sorted(set(last) | set(cur)):
            if last.get(k) != cur.get(k):
                tag = 'STAGE' if k == '<stage>' else 'VAR'
                text = f'{k} {last.get(k)} -> {cur.get(k)}'
                emit(tag, text)
                events.append((tag, text))
        last = cur
        try:
            lines = b.vmlog(limit=ring_limit).get('lines', [])
            if len(lines) > vm_seen:
                for ln in lines[vm_seen:]:
                    if ln.strip():
                        emit('VM', ln.rstrip())
                        events.append(('VM', ln.rstrip()))
            vm_seen = len(lines)
        except BridgeError:
            pass
    return events


def _emitter(out_path: str):
    """(emit, close) writing to stdout and optionally a transcript file."""
    fh = open(out_path, 'w', encoding='utf-8', buffering=1) if out_path else None

    def emit(kind: str, text: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {kind:<7} {text}"
        print(line, flush=True)
        if fh:
            fh.write(line + '\n')

    def close():
        if fh:
            fh.close()
    return emit, close


def _dialogue_reader(emit):
    try:
        from tools.live.dialog_live import DialogueReader
        r = DialogueReader(0)
        emit('START', f'dialogue readback @ rva 0x{r.manager:x}')
        return r
    except Exception as exc:
        emit('START', f'dialogue readback unavailable: {exc}')
        return None


def _state_refs(args) -> list:
    """The cast to keep in the room: --actor if given, else what setup saved."""
    refs = list(getattr(args, 'actor', None) or [])
    if not refs:
        st = _load()
        refs = ([m['ref'] for m in st.get('moved', [])]
                + list(st.get('spawned', [])))
    # De-duplicate, preserving order: setup appends on every run, so a repeated
    # setup would otherwise move the same actor several times per trial.
    seen, out = set(), []
    for r in refs:
        key = str(r).upper()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _in_cell(b: Bridge, cell: str) -> 'bool | None':
    """Is the player in `cell`? None if the cell does not exist at all.

    `player.getincell <cell>` is the engine's own answer and is the ONLY
    reliable check -- it separates "not there" from "no such cell", which a
    coordinate comparison cannot do.
    """
    out = (b.console(f'player.getincell {cell}') or '')
    if 'not found' in out:
        return None
    m = re.search(r'>>\s*(-?\d+(?:\.\d+)?)', out)
    return bool(float(m.group(1))) if m else None


def _player_axis(b: Bridge, axis: str) -> 'float | None':
    out = (b.console(f'player.getpos {axis}') or '').strip()
    m = re.search(r'>>\s*(-?\d+(?:\.\d+)?)', out) or re.search(
        r'(-?\d+(?:\.\d+)?)', out)
    return float(m.group(1)) if m else None


def _player_cell_x(b: Bridge) -> 'float | None':
    return _player_axis(b, 'x')


def establish_room(b: Bridge, cell: str, refs: list, load_wait: float = 12.0,
                   emit=None) -> dict:
    """Put the player AND the cast in the test cell, after any quest writes.

    🛑 ORDER MATTERS, and getting it wrong silently destroys the isolation.

    A quest's own stage scripts move actors and the player. Charactergen stage 5
    runs `player.moveto CGPlayerStartMarker` and stage 0 runs
    `<actor>Ref.moveto CGMarker*`, so `startquest` during a reset drags
    EVERYONE back to the quest's own cell. Measured: after resetting to stage
    10, the player was at x=567 (the prison) and all three actors read
    ELSEWHERE -- the "clean room" test was really running in the CharacterGen
    cell, which is exactly the confounded environment this harness exists to
    avoid.

    So the room is (re)established AFTER the reset, not before: coc the player
    back, then bring the refs to the player. Both are verified rather than
    assumed.
    """
    say = emit or (lambda k, t: print(f'  {t}'))
    b.console(f'coc {cell}')
    _wait_ready(b, load_wait)

    # A cell with no floor is not a clean room -- the player free-falls and
    # every actor moved in falls with them, breaking pathing and physics. Two
    # z samples settle it: an empty worldspace dummy cell keeps dropping
    # (measured -12691 and falling), a real room holds still.
    # 🛑 Ask the ENGINE which cell we are in. A coordinate check is not a cell
    # check: two different cells here both report z~7239, so comparing
    # positions "confirmed" a room the player had never actually left.
    inside = _in_cell(b, cell)
    z1 = _player_axis(b, 'z')
    time.sleep(1.2)
    z2 = _player_axis(b, 'z')
    room_x = _player_cell_x(b)

    if inside is None:
        say('ROOM', f'** cell {cell!r} does not exist in this load order -- '
                    f'pick one that does (AnvilMarkTest is the project test '
                    f'cell)')
    elif not inside:
        say('ROOM', f'** NOT in {cell}: the coc did not take effect. Every '
                    f'reading below is from whatever cell the player is '
                    f'actually in.')
    # A settling drop after a coc is normal (measured 6984 -> 6976 as the player
    # lands). A floorless cell falls without bound, so the threshold is set well
    # above a landing but far below a real fall (-12691 and counting).
    elif z1 is not None and z2 is not None and (z1 - z2) > 200.0:
        say('ROOM', f'** {cell} has NO FLOOR (z {z1:.0f} -> {z2:.0f}, still '
                    f'falling). Pick a cell with geometry -- results from a '
                    f'void cell are worthless.')
    else:
        say('ROOM', f'player in {cell} (confirmed; x={room_x}, z={z2})')

    placed, missing = [], []
    for ref in refs:
        before = _distance(b, ref)
        b.console('moveto player', ref=ref)
        time.sleep(0.4)
        after = _distance(b, ref)
        moved = (before is None or after is None or abs(before - after) > 1.0)
        if after is not None and after < NEARBY_UNITS and moved:
            placed.append(ref)
            say('ROOM', f'{ref} {_fmt(before)} -> {after:.1f}')
        else:
            missing.append(ref)
            say('ROOM', f'** {ref} NOT in the room ({_fmt(before)} -> '
                        f'{_fmt(after)})')
    return {'player_x': room_x, 'placed': placed, 'missing': missing}


def staging_presets(qrec: dict) -> list:
    """The quest's OWN debug staging entries: [{log, text, moves}].

    🛑 `setstage <quest> N` SETS A NUMBER. IT DOES NOT STAGE THE SCENE.

    Measured 2026-08-15: after `setstage Charactergen 40` the whole cast was
    still standing at its spawn point in ImperialDungeon01, while stage 40's
    scene takes place in Room B (ImperialDungeon02). The actors only reach Room
    B by PLAYING stages 0..40, which a bare setstage skips -- so the harness was
    observing an empty room and calling it a result.

    Bethesda already solved this. Charactergen's stage 0 carries seven log
    entries that are pure staging presets, each captioned with the scene it sets
    up and each doing nothing but `<Actor>Ref.moveto <Marker>`:

        Log[5] "Set everyone in their places for player to enter room B"
               BaurusRef.moveto CGMarkerDBaurus
               GlenroyRef.moveto CGMarkerDGlenroy
               UrielSeptimRef.moveto CGMarkerDEmperor

    That is the AUTHORED way to put the cast where a stage expects them, so the
    harness runs the preset rather than inventing positions. A preset is
    identified structurally -- a log entry whose result script is entirely
    moveto/kill/disable calls -- so this works for any quest that ships debug
    staging, and quests without it simply yield an empty list.
    """
    out = []
    i = 0
    while True:
        idx = qrec.get(f'Stage[{i}].Index')
        if idx is None:
            break
        j = 0
        while True:
            rs = qrec.get(f'Stage[{i}].Log[{j}].ResultScript')
            if rs is None and qrec.get(f'Stage[{i}].Log[{j}].Flags') is None:
                break
            if rs:
                body = rs.replace('\\r\\n', '\n')
                stmts = [s.strip() for s in body.split('\n')
                         if s.strip() and not s.strip().startswith(';')]
                moves = [s for s in stmts if '.moveto ' in s.lower()]
                # The preset's housekeeping runs too: Charactergen's entries
                # begin `RenoteRef.kill` / `CGGenericAssassinsParent.disable`,
                # which is how the authored preset clears the previous scene's
                # actors. Dropping it would leave a live Renote in a scene the
                # developer staged without one.
                keep = [s for s in stmts
                        if re.match(r'(?i)\S+\.(kill|disable)\b', s)]
                # A staging preset is MOVES ONLY (plus housekeeping kill/
                # disable). A log entry that also sets variables or stages is
                # real quest logic, not a preset, and running it would advance
                # the quest instead of positioning it.
                other = [s for s in stmts if s not in moves
                         and not re.match(r'(?i)\S+\.(kill|disable)\b', s)
                         and not re.match(r'(?i)(setessential)\b', s)]
                if moves and not other:
                    out.append({
                        'stage': int(idx) if str(idx).isdigit() else idx,
                        'log': j,
                        'text': qrec.get(f'Stage[{i}].Log[{j}].Text', ''),
                        'moves': moves,
                        'housekeeping': keep,
                    })
            j += 1
        i += 1
    return out


def _editorid_index(plugin: str, sigs=('REFR', 'ACHR', 'ACRE')) -> dict:
    """{EditorID.lower() -> FormID} across the given record types."""
    out = {}
    for sig in sigs:
        for rec in iter_records(_export_dir(plugin) / f'{sig}.txt'):
            ed = (rec.get('EditorID') or '').strip().lower()
            if ed:
                out.setdefault(ed, rec.get('FormID', ''))
    return out


def apply_staging(b: Bridge, plugin: str, preset: dict, index: str,
                  say=None) -> list:
    """Run one staging preset's moves through the console.

    Each `<Actor>Ref.moveto <Marker>` becomes a targeted console `moveto`, with
    both sides resolved from the export by EditorID -- so the actor lands on the
    marker the QUEST names, not on a position this harness guessed.

    Returns the failures, and verifies rather than trusts: a ref-targeted
    console command reports success even when it selected nothing, so each move
    is confirmed by reading the actor's distance to its marker afterwards.
    """
    say = say or (lambda k, t: print(f'  {t}'))
    ids = _editorid_index(plugin)
    fails = []

    # Housekeeping first, in the order the preset writes it: the authored entry
    # clears the previous scene (`RenoteRef.kill`,
    # `CGGenericAssassinsParent.disable`) before positioning anyone.
    for stmt in preset.get('housekeeping', []):
        m = re.match(r'(?i)\s*(\w+)\s*\.\s*(kill|disable)\b', stmt)
        if not m:
            continue
        who, verb = m.group(1), m.group(2).lower()
        w_fid = ids.get(who.lower())
        if not w_fid:
            fails.append(f'{stmt}: unresolved ref {who}')
            continue
        b.console(verb, ref=runtime_formid(w_fid, index))
        time.sleep(0.3)
        say('STAGE', f'{who}.{verb}')

    for stmt in preset['moves']:
        m = re.match(r'(?i)\s*(\w+)\s*\.\s*moveto\s+(\w+)', stmt)
        if not m:
            continue
        actor_ed, marker_ed = m.group(1), m.group(2)
        a_fid = ids.get(actor_ed.lower())
        k_fid = ids.get(marker_ed.lower())
        if not a_fid or not k_fid:
            fails.append(f'{stmt}: unresolved '
                         f'({actor_ed}={a_fid}, {marker_ed}={k_fid})')
            continue
        a_rt = runtime_formid(a_fid, index)
        k_rt = runtime_formid(k_fid, index)
        b.console(f'moveto {k_rt}', ref=a_rt)

        # 🛑 A distance read straight after the moveto is NOT the result of the
        # moveto. Measured 2026-08-15: all three actors reported exactly
        # "0.0 units" and the staging was declared successful, while Baurus had
        # in fact never left ImperialDungeon01 -- the whole trial then ran a
        # three-hander with two actors. An exact 0.0 is the signature of a
        # position the engine has not applied yet, so it is treated as NOT YET
        # SETTLED and re-read, never as a perfect arrival.
        dist = None
        for _ in range(6):
            time.sleep(0.5)
            out = (b.console(f'getdistance {k_rt}', ref=a_rt) or '')
            mm = re.search(r'>>\s*(-?\d+(?:\.\d+)?)', out)
            d = float(mm.group(1)) if mm else None
            if d is not None and d != 0.0:
                dist = d
                break
        if dist is None or dist > NEARBY_UNITS:
            fails.append(f'{actor_ed} did NOT reach {marker_ed} '
                         f'(distance {dist})')
            say('STAGE', f'** {actor_ed} -> {marker_ed} FAILED ({dist})')
        else:
            say('STAGE', f'{actor_ed} -> {marker_ed} ({dist:.1f} units)')
    return fails


def check_preconditions(b: Bridge, quest: str, refs: list, stage=None,
                        say=None) -> list:
    """Assert the world is fit to measure. Returns the list of FAILURES.

    🛑 THIS IS WHAT REPLACES THE CLEAN ROOM for a scripted set piece.

    A quest like Charactergen is defined by its geography: its travel packages
    target CGMarker* refs that exist only in the prison cells, and its
    force-greets are radius-based on actors standing where the quest put them.
    Moving that scene into an empty room does not isolate it -- it breaks the
    mechanism under test, and every "bug" observed afterwards is the harness's
    own. So the scene is left where it is, and the harness instead VERIFIES the
    state before opening a window, naming whichever precondition failed rather
    than producing a reading nobody can trust.

    Checked, all by asking the ENGINE rather than assuming:
      * the quest is running and sitting at the expected stage
      * every cast member is alive, NOT restrained, and not in combat
      * the player is in the same cell as the cast (else the scene is
        unobservable and any 'nothing happened' reading is meaningless)
    """
    say = say or (lambda k, t: print(f'  {t}'))
    fails = []

    running = (b.console(f'getquestrunning {quest}') or '')
    if '1' not in running:
        fails.append(f'quest {quest} is NOT running ({running.strip()[:40]})')

    if stage is not None:
        got = (b.console(f'getstage {quest}') or '')
        m = re.search(r'>>\s*(-?\d+(?:\.\d+)?)', got) or re.search(
            r'(-?\d+(?:\.\d+)?)', got)
        actual = int(float(m.group(1))) if m else None
        if actual != stage:
            fails.append(f'stage is {actual}, expected {stage}')

    for ref in refs:
        dead = (b.console('getdead', ref=ref) or '')
        if re.search(r'>>\s*1', dead):
            fails.append(f'{ref} is DEAD')
        # 🛑 The restraint check exists because the harness itself once set it
        # and then measured the result. A restrained actor cannot turn to or
        # approach its conversation target, so Say/force-greet never completes
        # and lines repeat with stretched timers -- a fabricated bug.
        # Answers in prose: "X is restrained" / "X is not restrained". Matched
        # with a word boundary so the negative form cannot satisfy the positive
        # test by substring.
        res = (b.console('getrestrained', ref=ref) or '')
        if re.search(r'\bis restrained\b', res, re.I):
            fails.append(f'{ref} is RESTRAINED (would fake repeated lines)')
        # `getincombat` does NOT exist (probed live: "unknown console
        # command"). The real one is `isincombat`, and like `getrestrained` it
        # answers in prose rather than a number, so it is matched as text.
        combat = (b.console('isincombat', ref=ref) or '')
        if re.search(r'\bis in combat\b', combat, re.I):
            fails.append(f'{ref} is IN COMBAT')

    for f in fails:
        say('PRE', f'** {f}')
    if not fails:
        say('PRE', f'preconditions OK ({len(refs)} actor(s), stage {stage})')
    return fails


def follow_scene(b: Bridge, refs: list, say=None) -> 'str | None':
    """Put the PLAYER where the scene is, instead of dragging the scene along.

    🛑 THE ACTORS ARE NOT DRIFTING -- THE OBSERVER IS.

    `moveto <actor>` puts the player next to an actor at ONE INSTANT. The actor
    is then scripted to walk to its next marker and the player does not go with
    it, so the window is spent watching an empty corridor while the scene plays
    somewhere else. Readings taken that way ("nobody said anything") describe
    the harness's viewpoint, not the conversion.

    So the player is moved TO the cast -- never the cast to the player -- and
    re-anchored whenever it falls out of range. Anchoring on the actor the scene
    currently centres on keeps force-greet radii (400-500 units) satisfied.

    Returns the ref the player was anchored to, or None if none could be found.
    """
    say = say or (lambda k, t: print(f'  {t}'))
    for ref in refs:
        d = _distance(b, ref)
        if d is not None and d < FOLLOW_UNITS:
            return ref            # already close enough to observe
    for ref in refs:
        b.console(f'player.moveto {ref}')
        time.sleep(0.5)
        d = _distance(b, ref)
        if d is not None and d < NEARBY_UNITS:
            say('FOLLOW', f'player -> {ref} ({d:.1f} units)')
            return ref
    say('FOLLOW', '** could not anchor the player to any cast member')
    return None


def unpin_cast(b: Bridge, refs: list) -> None:
    """Clear `setrestrained` from the cast.

    🛑 NEVER RESTRAIN THE CAST TO KEEP IT IN THE ROOM. This function only
    UNDOES a restraint; there is deliberately no counterpart that applies one.

    Measured live 2026-08-15. Restraining the actors looked like the obvious fix
    for "they keep walking out" -- their quest travel packages are live at the
    stage under test and path them straight back to the prison markers. But a
    restrained actor cannot turn to face or approach its conversation target, so
    the Say / force-greet handshake never completes: the script re-fires the
    same line while convTimer waits, and every gap stretches.

    That produced a textbook false bug -- lines repeating over and over and
    timers "extremely drawn out" -- which was entirely an artefact of the
    harness, in the exact clean room built to prevent artefacts. The observation
    would have been blamed on the converted script.

    If the cast walks out, that is the QUEST RUNNING. Let it. Re-establish the
    room between trials instead, and read the drift in the transcript.
    """
    for ref in refs:
        b.console('setrestrained 0', ref=ref)
        time.sleep(0.1)


def _do_reset(b: Bridge, quest: str, stage, sets) -> None:
    """Stop/reset/restart, then seed. Every write happens HERE, never in a
    window -- a write during measurement is what makes a reading untrustworthy.
    """
    for cmd in (f'stopquest {quest}', f'resetquest {quest}',
                f'startquest {quest}'):
        b.console(cmd)
        time.sleep(1.0)
    if stage is not None:
        b.console(f'setstage {quest} {stage}')
        time.sleep(1.0)
    for assign in sets or []:
        prop, _, val = assign.partition('=')
        if prop and val:
            b.console(f'setpqv {quest} {prop.strip()} {val.strip()}')


def cmd_trial(args) -> int:
    """ONE complete isolated trial: reset to a known state, then observe.

    This is the unit an A/B comparison is built from. Reset and observation are
    deliberately in the same command so a trial cannot accidentally be run
    against the residue of the previous one -- the single most common way an
    in-game "reproduction" ends up proving nothing.
    """
    emit, close = _emitter(args.out)
    try:
        with Bridge().connect(retries=4) as b:
            reader = _dialogue_reader(emit) if args.dialogue else None
            emit('RESET', f'{args.quest} -> stage {args.stage}')
            _do_reset(b, args.quest, args.stage, args.set)
            refs = _state_refs(args)

            if args.in_place:
                # 🛑 IN-PLACE: let the scene stay where it was authored and move
                # the OBSERVER to it. For a geography-dependent set piece this
                # is the only faithful mode -- see follow_scene/check_preconditions.
                _wait_ready(b, args.load_wait)
                unpin_cast(b, refs)          # never measure a restrained cast

                # 🛑 setstage sets a NUMBER; it does not stage the SCENE. Run
                # the quest's own debug staging preset so the cast is standing
                # where the stage expects, instead of at its spawn point.
                if args.preset is not None:
                    q = find_quest(args.plugin, args.quest)
                    presets = staging_presets(q)
                    match = [p for p in presets if p['log'] == args.preset]
                    if not match:
                        emit('ABORT', f'no staging preset log[{args.preset}]; '
                                      f'available: '
                                      f'{[p["log"] for p in presets]}')
                        return 2
                    idx = args.index
                    found = detect_index(b, q.get('FormID', ''))
                    if found:
                        idx = found
                    emit('STAGE', f'preset log[{args.preset}]: '
                                  f'{match[0]["text"] or "(no caption)"}')
                    sf = apply_staging(b, args.plugin, match[0], idx, emit)
                    if sf and not args.force:
                        emit('ABORT', f'staging failed: {sf}')
                        return 2
                follow_scene(b, refs, emit)
                fails = check_preconditions(b, args.quest, refs, args.stage,
                                            emit)
                if fails and not args.force:
                    emit('ABORT', f'{len(fails)} precondition(s) failed -- '
                                  f'refusing to measure. Re-run with --force '
                                  f'to observe anyway.')
                    return 2
            elif refs or args.cell:
                # AFTER the reset: its stage scripts move the player and the
                # cast into the quest's own cell (see establish_room).
                establish_room(b, args.cell, refs, args.load_wait, emit)
            b.vmlog(arm=True)
            events = _run_window(b, args.quest, args.seconds, args.interval,
                                 emit, reader, args.ring_limit,
                                 follow=refs if args.in_place else None)
            emit('END', f'{len(events)} transition(s)')
    finally:
        close()
    if args.out:
        print(f'\ntranscript -> {args.out}', file=sys.stderr)
    return 0


def cmd_ab(args) -> int:
    """The full autonomous loop: run, reset, change the script, run again, diff.

    Sequence, all without a relaunch:

        trial A   reset -> observe -> transcript A
        modify    apply the script change (see below)
        trial B   reset -> observe -> transcript B
        diff      what the change actually altered in behaviour

    TWO WAYS TO CHANGE THE SCRIPT, and they are NOT equivalent:

    --inject <file>   Compile and run a script body in-process, between the
                      reset and the window. This needs no relaunch and no
                      recompile, because the engine's own compiler does the
                      work live. Use it to test a DIFFERENT SEQUENCE OF ACTIONS
                      (what a fragment would do), which covers most quest and
                      dialogue logic questions.

    --pex <file>      Copy a recompiled .pex into the load path. HONEST LIMIT:
                      the VM binds script types when the save/session loads, so
                      an already-loaded script is NOT re-read. This is staged
                      for the next session and reported as such -- it is not a
                      live swap, and pretending otherwise would make trial B a
                      measurement of trial A's code.

    So: --inject is the live loop; --pex is the staged loop. The command tells
    you which one you got rather than letting a stale binding masquerade as a
    result.
    """
    out_a = args.out_a or 'temp/labtest_A.log'
    out_b = args.out_b or 'temp/labtest_B.log'
    Path(out_a).parent.mkdir(parents=True, exist_ok=True)

    results: dict = {}
    windows: dict = {}
    for label, out_path in (('A', out_a), ('B', out_b)):
        emit, close = _emitter(out_path)
        try:
            with Bridge().connect(retries=4) as b:
                reader = _dialogue_reader(emit) if args.dialogue else None
                emit('TRIAL', f'{label}: reset {args.quest} -> stage {args.stage}')
                _do_reset(b, args.quest, args.stage, args.set)
                # Re-establish AFTER the reset: quest stage scripts move the
                # player and cast back into the quest's own cell.
                refs = _state_refs(args)
                if refs or args.cell:
                    establish_room(b, args.cell, refs, args.load_wait, emit)

                # The state the window OPENS at. A change applied before the
                # window (an injection that sets a stage, say) moves this
                # baseline instead of appearing as a transition inside the
                # window -- so without recording it, a modification that
                # demonstrably worked can read as "no difference". Measured:
                # injecting `setstage X 12` made trial B start at 12, and the
                # 10->12 transition then showed up only in trial A.
                baseline = _qvars(b, args.quest).get('<stage>')

                if label == 'B':
                    # The ONLY difference between the two trials.
                    if args.inject:
                        body = Path(args.inject).read_text(encoding='utf-8')
                        emit('MODIFY', f'injecting {args.inject} '
                                       f'({len(body.splitlines())} statement(s))')
                        r = b.inject(body, ref=args.inject_ref or None,
                                     settle_ms=args.settle_ms)
                        emit('MODIFY', f"inject ok={r.get('ok')}")
                        for s in r.get('statements', []):
                            if not s.get('ok'):
                                emit('MODIFY', f"  FAILED: {s.get('text','')} "
                                               f"-- {s.get('error','')}")
                        for ln in (r.get('papyrus') or []):
                            emit('VM', ln)
                    elif args.pex:
                        emit('MODIFY', f'STAGED ONLY: {args.pex} copied into the '
                                       f'load path; the VM binds script types at '
                                       f'session load, so this trial still runs '
                                       f'the CURRENTLY LOADED code')

                opened_at = _qvars(b, args.quest).get('<stage>')
                if opened_at != baseline:
                    emit('MODIFY', f'stage moved before the window: '
                                   f'{baseline} -> {opened_at}')

                b.vmlog(arm=True)
                events = _run_window(b, args.quest, args.seconds, args.interval,
                                     emit, reader, args.ring_limit)
                final = _qvars(b, args.quest).get('<stage>')
                results[label] = events
                windows[label] = {'opened_at': opened_at, 'final': final}
                emit('END', f'{label}: {len(events)} transition(s), '
                            f'stage {opened_at} -> {final}')
        finally:
            close()

    a, bb = results.get('A', []), results.get('B', [])
    only_a = [e for e in a if e not in bb]
    only_b = [e for e in bb if e not in a]
    wa, wb = windows.get('A', {}), windows.get('B', {})

    print('\n' + '=' * 68)
    print(f'A/B DIFF  {args.quest} @ stage {args.stage}')
    print('=' * 68)
    print(f"  A: {len(a):4} transition(s)  stage {wa.get('opened_at')} -> "
          f"{wa.get('final')}  -> {out_a}")
    print(f"  B: {len(bb):4} transition(s)  stage {wb.get('opened_at')} -> "
          f"{wb.get('final')}  -> {out_b}")

    # A change applied BEFORE the window moves the starting state rather than
    # showing up as a transition inside it. Report that explicitly, or a
    # modification that plainly worked reads as "no difference".
    if wa.get('opened_at') != wb.get('opened_at'):
        print(f"\n  NOTE: the two windows OPENED at different stages "
              f"({wa.get('opened_at')} vs {wb.get('opened_at')}). The change "
              f"took effect before the window, so compare the end states, not "
              f"just the transitions.")
    if wa.get('final') != wb.get('final'):
        print(f"  END STATE DIFFERS: A finished at stage {wa.get('final')}, "
              f"B at {wb.get('final')}.")

    if not only_a and not only_b:
        print('\n  NO BEHAVIOURAL DIFFERENCE between the two trials.')
        print('  Either the change had no effect, or it does not reach this '
              'stage/path.')
    else:
        if only_a:
            print(f'\n  only in A (lost in B) -- {len(only_a)}:')
            for k, t in only_a[:args.diff_limit]:
                print(f'    - {k:<6} {t}')
        if only_b:
            print(f'\n  only in B (new) -- {len(only_b)}:')
            for k, t in only_b[:args.diff_limit]:
                print(f'    + {k:<6} {t}')
    if args.json:
        print(json.dumps({'quest': args.quest, 'stage': args.stage,
                          'a': a, 'b': bb, 'windows': windows,
                          'only_a': only_a, 'only_b': only_b}, indent=2))
    return 0


def cmd_restore(args) -> int:
    """Undo setup: delete tracked spawns, send moved refs home."""
    state = _load()
    with Bridge().connect(retries=4) as b:
        spawned = state.get('spawned', [])
        if spawned:
            try:
                r = b.request('cleanup')
                print(f"  cleanup removed {r.get('removed', 0)} tracked spawn(s)")
                state['spawned'] = []
            except BridgeError as exc:
                if exc.code in MISSING_CMD_CODES:
                    for ref in spawned:
                        b.console('disable', ref=ref)
                        b.console('markfordelete', ref=ref)
                    print(f'  removed {len(spawned)} spawn(s) via console')
                    state['spawned'] = []
                else:
                    print(f'  cleanup FAILED: {exc}')

        # Lift the movement pin BEFORE sending anyone home, so a restored actor
        # resumes its packages normally instead of standing frozen at its
        # original marker for the rest of the session.
        unpin_cast(b, [m['ref'] for m in state.get('moved', [])])

        for m in state.get('moved', []):
            ref, pos = m['ref'], m.get('pos', [])
            nums = []
            for p in pos:
                mm = re.search(r'(-?\d+\.?\d*)', p or '')
                nums.append(mm.group(1) if mm else None)
            if all(nums) and len(nums) == 3:
                for ax, val in zip('xyz', nums):
                    b.console(f'setpos {ax} {val}', ref=ref)
                print(f'  returned {ref} to {nums}')
            else:
                print(f'  CANNOT restore {ref}: original position not captured')
        state['moved'] = []
    _save(state)
    if args.clear:
        STATE.unlink(missing_ok=True)
    print('restore done')
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--plugin', default='Oblivion.esm',
                    help='which export to read the cast from')
    ap.add_argument('--index', default='01',
                    help='load-order index byte of the converted plugin')
    ap.add_argument('--json', action='store_true')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('cast', help='who/what is in this quest (export only)')
    p.add_argument('--quest', required=True)

    sub.add_parser('doctor', help='is the channel healthy enough to trust?')

    p = sub.add_parser('setup', help='move to the test cell with the cast')
    p.add_argument('--quest', help='discover the cast from this quest')
    p.add_argument('--stage', type=int,
                   help='scope the cast to the lines live around this stage, '
                        'instead of every speaker in the whole quest')
    p.add_argument('--cell', default=DEFAULT_CELL)
    p.add_argument('--actor', action='append', default=[],
                   help='runtime FormID (repeatable); overrides discovery')
    p.add_argument('--spawn', action='store_true',
                   help='spawn tracked COPIES instead of moving the real refs '
                        '(copies are different references, so quest properties '
                        'will not see them -- only for identity-independent '
                        'behaviour)')
    p.add_argument('--max-actors', type=int, default=8)
    p.add_argument('--load-wait', type=float, default=20.0)
    p.add_argument('--no-auto-index', dest='auto_index', action='store_false',
                   help='trust --index instead of asking the running game '
                        'which load-order slot the plugin actually occupies')

    p = sub.add_parser('reset', help='stop/reset/restart the quest')
    p.add_argument('--quest', required=True)
    p.add_argument('--stage', type=int)
    p.add_argument('--set', action='append', help='Prop=Value after the seed')

    p = sub.add_parser('run', help='observe passively and transcribe changes')
    p.add_argument('--quest', required=True)
    p.add_argument('--seconds', type=float, default=60.0)
    p.add_argument('--interval', type=float, default=0.5)
    p.add_argument('--dialogue', action='store_true',
                   help='also record which topics the game is offering '
                        '(MenuTopicManager -- the only source for that)')
    p.add_argument('--ring-limit', type=int, default=200)
    p.add_argument('--out', default='')

    p = sub.add_parser('record',
                       help='record a LIVE playthrough the USER drives: which '
                            'package each actor runs, what dialogue is offered, '
                            'and the quest state, all on one clock. Strictly '
                            'passive -- writes nothing to the game.')
    p.add_argument('--quest', required=True)
    p.add_argument('--seconds', type=float, default=180.0)
    p.add_argument('--interval', type=float, default=0.4)
    p.add_argument('--actor', action='append', default=[],
                   help='runtime FormID to watch (repeatable); default is every '
                        'GetIsID speaker the quest names')
    p.add_argument('--max-actors', type=int, default=4,
                   help='each actor costs 3 console commands per tick, so a '
                        'wide cast slows the clock the timeline depends on')
    p.add_argument('--move-units', type=float, default=64.0,
                   help='ignore distance jitter below this')
    p.add_argument('--vm-mute', action='append', default=[],
                   help='drop VM lines containing this substring (repeatable) '
                        '-- other mods share the Papyrus sink and a chatty one '
                        'buries the timeline')
    p.add_argument('--no-console-log', dest='console_log',
                   action='store_false',
                   help='skip the always-on console ring (it is a SEPARATE '
                        '4000-line buffer with a real sequence number, and it '
                        'catches engine output the Papyrus sink never sees)')
    p.add_argument('--heartbeat', type=float, default=60.0,
                   help='emit an ALIVE line this often (0 disables), so a quiet '
                        'stretch reads as "recorder alive" not "recorder died"')
    p.add_argument('--ring-limit', type=int, default=1000,
                   help='VM lines to pull per poll. Bigger is safer on a long '
                        'recording: the sink is shared with every other mod, '
                        'and if the ring turns over between polls that output '
                        'is gone (the transcript says so rather than hiding it)')
    p.add_argument('--out', default='')

    p = sub.add_parser('trial',
                       help='ONE isolated trial: reset to a known state, '
                            'then observe (the unit an A/B is built from)')
    p.add_argument('--quest', required=True)
    p.add_argument('--stage', type=int)
    p.add_argument('--set', action='append')
    p.add_argument('--seconds', type=float, default=60.0)
    p.add_argument('--interval', type=float, default=0.5)
    p.add_argument('--dialogue', action='store_true')
    p.add_argument('--ring-limit', type=int, default=200)
    p.add_argument('--in-place', action='store_true',
                   help='do NOT build a clean room: leave the scene where the '
                        'quest authored it and move the PLAYER to it, '
                        're-anchoring as the scene walks. Required for a '
                        'geography-dependent set piece, whose packages target '
                        'markers that exist only in its own cell.')
    p.add_argument('--force', action='store_true',
                   help='observe even if a precondition fails (default: abort '
                        'rather than produce an untrustworthy reading)')
    p.add_argument('--preset', type=int,
                   help="log index of the quest's OWN stage-0 debug staging "
                        "entry to run, putting the cast where the stage "
                        "expects them (setstage alone does NOT move anyone). "
                        "List them with `cast --quest X`.")
    p.add_argument('--cell', default=DEFAULT_CELL,
                   help='re-establish this test cell after each reset')
    p.add_argument('--actor', action='append', default=[],
                   help='keep these refs in the room (default: what setup saved)')
    p.add_argument('--load-wait', type=float, default=12.0)
    p.add_argument('--out', default='')

    p = sub.add_parser('ab',
                       help='the autonomous loop: run, reset, change the '
                            'script, run again, diff the two')
    p.add_argument('--quest', required=True)
    p.add_argument('--stage', type=int)
    p.add_argument('--set', action='append')
    p.add_argument('--seconds', type=float, default=45.0)
    p.add_argument('--interval', type=float, default=0.5)
    p.add_argument('--dialogue', action='store_true')
    p.add_argument('--ring-limit', type=int, default=200)
    p.add_argument('--inject',
                   help='script body applied before trial B ONLY -- compiled '
                        'and run live by the engine, no relaunch needed')
    p.add_argument('--inject-ref',
                   help='run the injected body against this reference')
    p.add_argument('--pex',
                   help='recompiled .pex to stage (NOT a live swap: the VM '
                        'binds script types at session load)')
    p.add_argument('--settle-ms', type=int, default=600)
    p.add_argument('--diff-limit', type=int, default=40)
    p.add_argument('--cell', default=DEFAULT_CELL,
                   help='re-establish this test cell after each reset')
    p.add_argument('--actor', action='append', default=[],
                   help='keep these refs in the room (default: what setup saved)')
    p.add_argument('--load-wait', type=float, default=12.0)
    p.add_argument('--out-a', default='')
    p.add_argument('--out-b', default='')

    p = sub.add_parser('restore', help='undo setup')
    p.add_argument('--clear', action='store_true')

    args = ap.parse_args(argv)
    fn = {'cast': cmd_cast, 'doctor': cmd_doctor, 'setup': cmd_setup,
          'reset': cmd_reset, 'run': cmd_run, 'trial': cmd_trial,
          'ab': cmd_ab, 'restore': cmd_restore, 'record': cmd_record}[args.cmd]
    try:
        return fn(args)
    except BridgeError as exc:
        print(f'bridge error [{exc.code}]: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
