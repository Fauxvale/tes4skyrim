"""Which Skyrim objective each converted quest stage FINISHES.

See: docs/commentary/script_convert.md#journal-objective-completion
"""

import json
import os
import struct

#: EditorID (lowercased) -> objective stages exempt from the runtime sweep.
_PARALLEL = {}

_PARALLEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'data', 'parallel_objectives.json')

#: Condition function: GetStage(quest).
_GETSTAGE = 58
#: Condition function: GetStageDone(quest, stage) — the stage is param 2.
_GETSTAGEDONE = 59
#: CTDA operators carrying a closing edge: ==, <, <= (the operator is raw[0] >> 5).
_CLOSING_OPS = frozenset((0, 4, 5))


def _ctda_supersede_stage(raw: bytes, stage_idx: int):
    """Stage at which this one log-entry CTDA stops displaying the entry.

    Reads the two authored idioms, `GetStage < N` and `GetStageDone N == 0`.
    None when the condition is neither, or names a stage at or before
    `stage_idx` — that is a wording choice, not a supersede.

    See: docs/commentary/script_convert.md#journal-objective-completion
    """
    if len(raw) < 20:
        return None
    func = struct.unpack_from('<I', raw, 8)[0]
    op = raw[0] >> 5
    comp = int(struct.unpack_from('<f', raw, 4)[0])
    if func == _GETSTAGE:
        end = comp if op == 4 else comp + 1 if op == 5 else None
    elif func == _GETSTAGEDONE and op == 0 and comp == 0:
        end = struct.unpack_from('<I', raw, 16)[0]
    else:
        end = None
    return end if end is not None and end > stage_idx else None


def _target_closes(raws) -> bool:
    """True when this TES4 quest target's gate can ever stop being satisfied.

    A `GetStage` bound with a closing edge (`==`, `<`, `<=`) ends at a known
    stage; a `GetStageDone` test ends when that stage is done, whatever the
    order. An open-ended `GetStage >= N` never closes, so its liveness says
    nothing about whether a step finished and it must not be read as evidence.

    See: docs/commentary/script_convert.md#journal-objective-completion
    """
    closing = False
    for raw_hex in raws:
        try:
            raw = bytes.fromhex(raw_hex)
        except ValueError:
            continue
        if len(raw) < 20:
            continue
        func = struct.unpack_from('<I', raw, 8)[0]
        if func == _GETSTAGEDONE:
            return True
        if func == _GETSTAGE and raw[0] >> 5 in _CLOSING_OPS:
            closing = True
    return closing


def _terminal_stages(rec: dict) -> frozenset:
    """Stage indices flagged TES4 QSDT 0x01 — the stages that END the quest.

    Success and failure endings share the one flag, so these are mutually
    exclusive outcomes: a later one must never be read as superseding an
    earlier one.

    See: docs/commentary/script_convert.md#journal-objective-completion
    """
    out = set()
    i = 0
    while f'Stage[{i}].Index' in rec:
        idx = int(rec[f'Stage[{i}].Index'])
        log_count = int(rec.get(f'Stage[{i}].LogCount', 0) or 0)
        if log_count:
            for j in range(log_count):
                flags = int(rec.get(f'Stage[{i}].Log[{j}].Flags', 0) or 0)
                if flags & 0x01:
                    out.add(idx)
        elif int(rec.get(f'Stage[{i}].CompleteQuest', 0) or 0):
            out.add(idx)
        i += 1
    return frozenset(out)


def _log_entry_supersede_stage(rec: dict, stage_arr_idx: int, log_arr_idx: int,
                               stage_idx: int):
    """The stage at which Oblivion itself stops DISPLAYING this log entry.

    Authored data, not inference: the journal filter runs each entry's own
    CTDAs and shows it only while they pass.  Returns the earliest superseding
    stage, or None when the entry carries no such gate.

    See: docs/commentary/script_convert.md#journal-objective-completion
    """
    k = 0
    best = None
    while True:
        raw_hex = rec.get(f'Stage[{stage_arr_idx}].Log[{log_arr_idx}].'
                          f'Condition[{k}].Raw')
        if raw_hex is None:
            break
        k += 1
        try:
            raw = bytes.fromhex(raw_hex)
        except ValueError:
            continue
        end = _ctda_supersede_stage(raw, stage_idx)
        if end is not None and (best is None or end < best):
            best = end
    return best


def _authored_ends(rec: dict, fragments: list) -> dict:
    """{stage index: earliest stage the AUTHOR says supersedes it}."""
    authored = {}
    for stage_idx, _log_idx, text, _rs, _cf, sa, la in fragments:
        if not text:
            continue
        end = _log_entry_supersede_stage(rec, sa, la, stage_idx)
        if end is not None:
            cur = authored.get(stage_idx)
            authored[stage_idx] = end if cur is None else min(cur, end)
    return authored


def _quest_targets(rec: dict) -> list:
    """Per-target TES4 stage gates, as lists of raw CTDA hex."""
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
    return targets


def _closed_by(rec: dict, fragments: list) -> tuple:
    """({objective stage: the stage that finishes it}, [unresolved stages]).

    The authored per-log-entry display gate wins where present; otherwise the
    quest TARGETS decide, a step ending at the first stage its CLOSING markers
    go dark. A stage that ends the quest is never finished by a later one.

    See: docs/commentary/script_convert.md#journal-objective-completion
    """
    from tes5_import.dialog_converter import _target_live_at_stage

    targets = _quest_targets(rec)
    obj_stages = sorted({s for s, _j, text, *_ in fragments if text})
    live = {s: frozenset(i for i, raws in enumerate(targets)
                         if raws and _target_closes(raws)
                         and _target_live_at_stage(raws, s))
            for s in obj_stages}
    authored = _authored_ends(rec, fragments)
    terminal = _terminal_stages(rec)

    closed_by = {}
    residue = []
    for prior in obj_stages:
        later = [s for s in obj_stages if s > prior]
        if prior in terminal or not later:
            continue
        there = live[prior]
        if prior in authored:
            end = next((s for s in later if s >= authored[prior]), None)
        elif there:
            end = next((s for s in later if not (there & live[s])), None)
        else:
            end = later[0]
        if end is None:
            residue.append(prior)
        else:
            closed_by[prior] = end
    return closed_by, residue


def _superseded_stages(rec: dict, fragments: list) -> dict:
    """{(stage_idx, log_idx): [stage indices this fragment completes]}.

    See: docs/commentary/script_convert.md#journal-objective-completion
    """
    closed_by, _residue = _closed_by(rec, fragments)
    obj_frags = [(s, j) for s, j, text, *_ in fragments if text]
    supersedes = {}
    for stage, log_idx in obj_frags:
        first_log = min(j for s, j in obj_frags if s == stage)
        supersedes[(stage, log_idx)] = (
            sorted(p for p, end in closed_by.items() if end == stage)
            if log_idx == first_log else [])
    return supersedes


def residue_stages(rec: dict, fragments: list) -> list:
    """Objective stages no static rule can finish, needing the runtime sweep.

    See: docs/commentary/script_convert.md#journal-objective-completion
    """
    return _closed_by(rec, fragments)[1]


def load_parallel_objectives(path: str = None, quiet: bool = False) -> int:
    """Load the order-independent-objective table. Returns quests loaded."""
    global _PARALLEL
    path = path or _PARALLEL_PATH
    if not os.path.exists(path):
        _PARALLEL = {}
        if not quiet:
            print(f"  Parallel objectives: table not found ({path})")
        return 0
    with open(path, encoding='utf-8') as fh:
        raw = json.load(fh)
    entries = raw.get('entries', raw) if isinstance(raw, dict) else raw
    _PARALLEL = {str(k).lower(): frozenset(int(s) for s in v)
                 for k, v in entries.items() if isinstance(v, list)}
    if not quiet:
        print(f"  Parallel objectives: {len(_PARALLEL)} quests exempt "
              f"from the objective sweep")
    return len(_PARALLEL)


def parallel_stages(edid: str) -> frozenset:
    """Objective stages of `edid` that must stay open alongside each other."""
    if not _PARALLEL:
        load_parallel_objectives(quiet=True)
    return _PARALLEL.get((edid or '').lower(), frozenset())


def sweep_targets(rec: dict, fragments: list, edid: str) -> list:
    """Residue stages to sweep, minus this quest's order-independent ones."""
    return sorted(set(residue_stages(rec, fragments)) - parallel_stages(edid))


def objective_lines(supersedes: dict, sweepable: list, stage_idx: int,
                    log_idx: int) -> list:
    """Papyrus tracking this stage's objective: close what it ends, display it.

    Statically-known supersedes close outright; the rest are swept at runtime,
    which only closes objectives the player actually saw.

    See: docs/commentary/script_convert.md#journal-objective-completion
    """
    out = []
    for prior in supersedes.get((stage_idx, log_idx), ()):
        out.append(f'  SetObjectiveCompleted({prior}, true)')
    for prior in sweepable:
        if prior >= stage_idx:
            continue
        out.append(f'  If IsObjectiveDisplayed({prior}) && '
                   f'!IsObjectiveCompleted({prior})')
        out.append(f'    SetObjectiveCompleted({prior}, true)')
        out.append('  EndIf')
    out.append(f'  SetObjectiveDisplayed({stage_idx}, true)')
    return out
