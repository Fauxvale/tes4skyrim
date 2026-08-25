#!/usr/bin/env python3
"""Conversation emulator — runs the CONVERTED Papyrus against a model of
Skyrim's runtime, so a scripted conversation can be proven to advance before
it is ever loaded in-game.

Why this exists
---------------
Every previous fix to the converted Say/timer handshake was reasoned about
rather than measured, and each one was wrong in a different way:

  1. a measured line duration ADDED to the wait the engine already performs;
  2. a relative timer release that RACED the other scripts polling the same
     timer, so a different handoff stalled on each run;
  3. a `>= park` guard that could never fire, because the owning loop counts
     the timer DOWN while the line plays.

All three compiled cleanly and all three failed in play. Compilation proves
syntax; it proves nothing about whether the state machine advances. This
emulator closes that gap and reports the exact tick where progress stops.

Engine facts encoded (sources in brackets)
------------------------------------------
* `Say()` is ASYNCHRONOUS. The selected response's End fragment runs when the
  LINE FINISHES, not when Say() is called.
  [ObjectReference.psc: `Say(Topic, Actor akActorToSpeakAs, bool)` -> void;
   Skyrim.esm uses the End-fragment flag (0x02) on 3,415 of its Say-driven
   CUST responses, exactly to sequence on line completion.]
* A `Say()` aimed at an actor who is already speaking is DROPPED, and a
  dropped call runs NO fragment.
* `RegisterForSingleUpdate(n)` fires OnUpdate no SOONER than n seconds, and
  later when the VM is busy — Papyrus gets `fUpdateBudgetMS` per frame.
  [SkyrimSE.exe Papyrus:fUpdateBudgetMS = 1.2]
* Updates for different scripts are NOT synchronised; their order within a
  tick is arbitrary, so `--seeds` shuffles it to expose ordering races.
* An INFO's response is chosen by walking the topic in record order and
  taking the first whose conditions all pass.

Usage
-----
    python -m tools.papyrus_emulator --quest Charactergen \\
        --scripts output/Oblivion.esm/scripts/source \\
        --esm output/Oblivion.esm/Oblivion.esm --export export/Oblivion.esm \\
        --start-stage 6 --goal-stage 20

    # hunt ordering races across many interleavings
    python -m tools.papyrus_emulator ... --seeds 200

    # full trace of one run
    python -m tools.papyrus_emulator ... --trace
"""
import argparse
import heapq
import os
import random
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.papyrus_interp import Interp

# Papyrus' per-frame budget, read from SkyrimSE.exe (Papyrus:fUpdateBudgetMS).
# Update latency beyond a timer's nominal delay comes from this.
VM_UPDATE_BUDGET_MS = 1.2
FRAME_SECONDS = 1.0 / 60.0


# ---------------------------------------------------------------------------
# Script model
# ---------------------------------------------------------------------------

class ScriptInstance:
    """One attached script instance: properties plus parsed event bodies."""

    _PROP_RE = re.compile(
        r'^\s*([\w]+)\s+Property\s+(\w+)\s*(?:=\s*([-\d.]+))?\s*Auto',
        re.IGNORECASE | re.MULTILINE)
    _EVENT_RE = re.compile(
        r'^\s*(?:Event|Function)\s+(\w+)\s*\([^)]*\)(.*?)^\s*End(?:Event|Function)',
        re.IGNORECASE | re.MULTILINE | re.DOTALL)

    def __init__(self, name, source, owner=None):
        self.name = name
        self.owner = owner          # quest name this script belongs to, if any
        self.props = {}
        self.prop_types = {}
        self.events = {}
        for m in self._PROP_RE.finditer(source):
            ptype, pname, default = m.group(1), m.group(2), m.group(3)
            self.prop_types[pname.lower()] = ptype
            if ptype.lower() in ('int', 'float', 'bool'):
                self.props[pname.lower()] = float(default) if default else 0.0
            else:
                self.props[pname.lower()] = None
        for m in self._EVENT_RE.finditer(source):
            self.events[m.group(1).lower()] = _strip(m.group(2))

    def __repr__(self):
        return '<%s>' % self.name


def _strip(body):
    out = []
    for raw in body.splitlines():
        line = raw.split(';', 1)[0].rstrip()
        if line.strip():
            out.append(line)
    return out


class InfoModel:
    """One response in a topic."""

    def __init__(self, fid, text, duration, conditions, fragment_src):
        self.fid = fid
        self.text = text
        self.duration = duration
        self.conditions = conditions          # [(func, param, op, value)]
        self.fragment_src = fragment_src      # ScriptInstance or None


class TopicModel:
    def __init__(self, name, infos):
        self.name = name
        self.infos = infos


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Engine:
    def __init__(self, rng, trace=False):
        self.time = 0.0
        self.rng = rng
        self.trace = trace
        self.log = []
        self.scripts = {}                 # lowercased name -> ScriptInstance
        self.quests = {}                  # quest (lower) -> stage
        self.quest_running = set()
        self.stage_fragments = defaultdict(list)   # (quest, stage) -> [fn]
        self.topics = {}
        self.pending = []                 # heap of (due, seq, gen, inst)
        self.gen = {}                     # id(inst) -> cancellation generation
        self.seq = 0
        self.speaking = {}                # inst -> (finish, topic, info)
        self.dropped_says = 0
        self.lines_spoken = []
        self.interp = Interp(self)
        self.cond_eval = lambda info, speaker: True
        self.aborted = None

    # -- tracing ---------------------------------------------------------
    def trace_line(self, msg):
        if self.trace:
            self.log.append('[%7.2f] %s' % (self.time, msg))

    # -- scheduling ------------------------------------------------------
    def register_update(self, inst, delay):
        """Fires no SOONER than `delay`; jitter models VM budget pressure.

        `pending` is a heap, and cancellation is LAZY (a generation counter per
        instance) rather than a list rebuild: these loops re-register on every
        tick, so an O(n) scan per event made a 60s simulation take minutes.
        """
        self.seq += 1
        jitter = self.rng.uniform(0.0, FRAME_SECONDS * 2)
        gen = self.gen.get(id(inst), 0)
        heapq.heappush(self.pending,
                       (self.time + delay + jitter, self.seq, gen, inst))

    def cancel_updates(self, inst):
        """Invalidate this instance's outstanding updates without touching the
        heap — stale entries are discarded when they surface."""
        self.gen[id(inst)] = self.gen.get(id(inst), 0) + 1

    # -- dialogue --------------------------------------------------------
    def say(self, inst, topic_name):
        if inst in self.speaking:
            # The engine ignores a Say on an actor mid-line, and the dropped
            # call runs NO fragment.
            self.dropped_says += 1
            self.trace_line('  Say(%s) DROPPED - %s still speaking'
                            % (topic_name, inst.name))
            return None
        topic = self.topics.get(topic_name.lower())
        if topic is None:
            self.trace_line('  Say(%s) -> topic not modelled' % topic_name)
            return None
        info = None
        for cand in topic.infos:
            if self.cond_eval(cand, inst):
                info = cand
                break
        if info is None:
            self.trace_line('  Say(%s) -> NO INFO MATCHED' % topic_name)
            return None
        self.speaking[inst] = (self.time + info.duration, topic, info)
        self.lines_spoken.append((self.time, inst.name, info.fid, info.text))
        self.trace_line('  %s says [%s] "%s" (%.2fs)'
                        % (inst.name, info.fid, info.text[:46], info.duration))
        return info

    # -- main loop -------------------------------------------------------
    def run(self, seconds, on_tick=None, max_events=2_000_000):
        """Advance the simulation. `max_events` is a hard stop so a modelling
        bug can never hang the tool; hitting it is reported, not silent."""
        end = self.time + seconds
        events = 0
        while self.time < end:
            events += 1
            if events > max_events:
                self.aborted = 'event limit (%d) hit at t=%.1f' % (
                    max_events, self.time)
                return
            # drop updates cancelled since they were scheduled
            while self.pending and self.pending[0][2] < self.gen.get(
                    id(self.pending[0][3]), 0):
                heapq.heappop(self.pending)

            # next event: an update firing, or a line finishing
            next_update = self.pending[0][0] if self.pending else None
            next_line = min((v[0] for v in self.speaking.values()), default=None)
            candidates = [t for t in (next_update, next_line) if t is not None]
            if not candidates:
                break
            self.time = max(self.time, min(candidates))
            if self.time > end:
                break

            # lines that finished at this instant run their End fragment
            for inst in [k for k, v in self.speaking.items()
                         if v[0] <= self.time + 1e-9]:
                _finish, _topic, info = self.speaking.pop(inst)
                self.trace_line('  %s finished [%s]' % (inst.name, info.fid))
                if info.fragment_src is not None:
                    self.interp.run_event(info.fragment_src, 'fragment_0',
                                          {'akspeakerref': inst})

            # updates due now, in ARBITRARY order (the VM does not synchronise
            # separate scripts) — this is what --seeds explores
            due = []
            while self.pending and self.pending[0][0] <= self.time + 1e-9:
                _t, _seq, gen, inst = heapq.heappop(self.pending)
                if gen >= self.gen.get(id(inst), 0):
                    due.append(inst)
            if due:
                self.rng.shuffle(due)
                for inst in due:
                    self.interp.run_event(inst, 'onupdate')
            if on_tick:
                on_tick(self)


# ---------------------------------------------------------------------------
# Condition evaluation (mirrors the engine's INFO selection)
# ---------------------------------------------------------------------------

F_GETSTAGE = 58
F_GETSTAGEDONE = 59
F_GETISID = 72
F_GETVMQUESTVAR = 629
F_GETVMSCRIPTVAR = 630


def make_cond_eval(engine, quest_fid_to_name, speaker_base):
    """Return f(info, speaker) -> bool using the engine's live state.

    Unmodelled functions PASS, so the emulator reports sequencing failures
    rather than gaps in its own condition coverage.
    """
    def var_of(cis2, owner_inst):
        if not cis2 or owner_inst is None:
            return None
        name = cis2.strip(':').rsplit('_var', 1)[0].lstrip(':').lower()
        return owner_inst.props.get(name)

    def ev(info, speaker):
        groups, cur = [], []
        for c in info.conditions:
            cur.append(c)
            if not c[5]:            # OR flag clear -> end of AND group
                groups.append(cur)
                cur = []
        if cur:
            groups.append(cur)
        for grp in groups:
            if not any(_one(c, speaker) for c in grp):
                return False
        return True

    def _one(c, speaker):
        func, p1, op, val, cis2, _or = c
        if func == F_GETSTAGE:
            q = quest_fid_to_name.get(p1)
            return _cmp(engine.quests.get(q, 0), op, val) if q else True
        if func == F_GETSTAGEDONE:
            q = quest_fid_to_name.get(p1)
            return _cmp(1 if engine.quests.get(q, 0) >= val else 0, op, 1)                 if q else True
        if func == F_GETISID:
            want = speaker_base.get(p1)
            return _cmp(1 if want == speaker.name else 0, op, val)
        if func in (F_GETVMQUESTVAR, F_GETVMSCRIPTVAR):
            owner = engine.owner_of_fid.get(p1)
            got = var_of(cis2, owner)
            return True if got is None else _cmp(got, op, val)
        return True
    return ev


def _cmp(a, op, b):
    a, b = float(a), float(b)
    return {'==': a == b, '!=': a != b, '>=': a >= b,
            '<=': a <= b, '>': a > b, '<': a < b}[op]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--scripts', required=True, help='generated .psc dir')
    ap.add_argument('--esm', required=True)
    ap.add_argument('--export', required=True)
    ap.add_argument('--quest', required=True)
    ap.add_argument('--start-stage', type=int, default=0)
    ap.add_argument('--goal-stage', type=int, default=None)
    ap.add_argument('--seconds', type=float, default=180.0)
    ap.add_argument('--seeds', type=int, default=1,
                    help='run N interleavings to expose ordering races')
    ap.add_argument('--trace', action='store_true')
    args = ap.parse_args()

    from tools.papyrus_emulator_load import build_engine
    from script_convert.say_durations import scan_voice_durations
    durations = scan_voice_durations(args.export)

    failures, reached = [], []
    for seed in range(args.seeds):
        rng = random.Random(seed)
        eng = build_engine(args.scripts, args.esm, args.export, args.quest,
                           rng, trace=(args.trace and args.seeds == 1),
                           durations=durations)
        _prime(eng, args.quest, args.start_stage)
        eng.run(args.seconds)
        got = eng.quests.get(args.quest.lower(), 0)
        reached.append(got)
        if args.goal_stage is not None and got < args.goal_stage:
            failures.append((seed, got, len(eng.lines_spoken),
                             eng.dropped_says))

    if args.trace and args.seeds == 1:
        for line in eng.log:
            print(line)
        print()

    print('quest %s: start=%d goal=%s  seeds=%d'
          % (args.quest, args.start_stage, args.goal_stage, args.seeds))
    print('  stages reached: min=%d max=%d' % (min(reached), max(reached)))
    print('  lines spoken (last run): %d, dropped Say calls: %d'
          % (len(eng.lines_spoken), eng.dropped_says))
    if args.goal_stage is None:
        return 0
    if failures:
        print('  STALLED in %d/%d interleavings' % (len(failures), args.seeds))
        for seed, got, lines, dropped in failures[:5]:
            print('     seed %-4d stopped at stage %-3d (%d lines, %d dropped)'
                  % (seed, got, lines, dropped))
        return 1
    print('  OK - reached the goal in all %d interleavings' % args.seeds)
    return 0


def _prime(eng, quest, stage):
    """Start the quest at `stage` and kick every script's update loop, the way
    a fresh game + SetStage would."""
    q = quest.lower()
    eng.quest_running.add(q)
    eng.quests[q] = 0
    eng.interp.set_stage(q, stage)
    for inst in eng.scripts.values():
        if 'onupdate' in inst.events:
            eng.register_update(inst, 0.1)
        if 'oninit' in inst.events:
            eng.interp.run_event(inst, 'oninit')


if __name__ == '__main__':
    sys.exit(main())
