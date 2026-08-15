#!/usr/bin/env python3
"""Debug a quest in the RUNNING game: read its real state, drive it, watch it.

Built on tools/game_bridge.py. Where that is a general channel into the game,
this knows what a quest IS -- stages, aliases, objectives, the scripts attached
to it -- and answers the questions that actually come up when a converted quest
misbehaves.

The core problem it solves: the converted plugin's quest may be running with
state that no offline artifact can show you. `sqv` knows. This reads `sqv`.

    # everything the engine knows about a quest, parsed
    python tools/quest_debug.py state charactergen

    # is it running, and at what stage?
    python tools/quest_debug.py stage charactergen

    # every stage the engine will accept, and which are done
    python tools/quest_debug.py stages charactergen

    # drive it, capturing exactly what Papyrus said in response
    python tools/quest_debug.py setstage charactergen 27

    # watch a quest advance: poll its stage and print every change
    python tools/quest_debug.py watch charactergen --seconds 60

    # what scripts are attached, and did their properties bind?
    python tools/quest_debug.py scripts charactergen

    # the aliases and what filled them (empty alias = a very common defect)
    python tools/quest_debug.py aliases charactergen

Every subcommand takes --json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from game_bridge import Bridge, BridgeError  # noqa: E402


# `sqv` output shapes, from the engine's own printer.
RE_STAGE = re.compile(r"Current stage:\s*(\d+)", re.I)
RE_RUNNING = re.compile(r"\bRunning\b", re.I)
RE_STOPPED = re.compile(r"\bStopped\b|\bnot running\b", re.I)
RE_PRIORITY = re.compile(r"priority:\s*(\d+)", re.I)
RE_ALIAS = re.compile(r"^\s*Alias\s+(\S+)\s*=\s*(.*)$", re.I)
RE_OBJECTIVE = re.compile(r"Objective\s+(\d+)\s*:?\s*(.*)", re.I)
RE_SCRIPT = re.compile(r"^\s*(?:script|Script)\s+(\S+)", re.I)
# "  [ 10] Done" / "  [ 20]"
RE_STAGE_LINE = re.compile(r"\[\s*(\d+)\s*\]\s*(.*)")
# A script variable as `sqv` prints it: "\t::convCount_var = 23", and the
# plain script-scope form the converter emits: "\tTES4_ChargenMenuBusy = False".
# Both matter -- the conversation state a TES4 quest script drives lives in
# the `::`-prefixed properties, while the converter's own latches/timers do
# not carry that prefix.
RE_SCRIPT_VAR = re.compile(r"^\s*(?:::)?([A-Za-z_]\w*?)(?:_var)?\s*=\s*(.*)$")


def parse_sqv(text: str) -> dict:
    """Turn `sqv` output into structure.

    Deliberately tolerant: `sqv` formatting varies by quest and by what is
    filled in, and a parser that throws on an unexpected line would be useless
    exactly when a quest is in a strange state -- which is when you are reading
    it. Unrecognised lines are preserved in `raw`.
    """
    out: dict = {
        "stage": None,
        "running": None,
        "priority": None,
        "aliases": [],
        "objectives": [],
        "scripts": [],
        "vars": {},
        "raw": text.splitlines(),
    }
    if not text.strip():
        return out

    if m := RE_STAGE.search(text):
        out["stage"] = int(m.group(1))
    if RE_RUNNING.search(text) and not RE_STOPPED.search(text):
        out["running"] = True
    elif RE_STOPPED.search(text):
        out["running"] = False
    if m := RE_PRIORITY.search(text):
        out["priority"] = int(m.group(1))

    for line in text.splitlines():
        if m := RE_ALIAS.match(line):
            name, value = m.group(1), m.group(2).strip()
            out["aliases"].append({
                "name": name,
                "value": value,
                # An alias that resolved to nothing is one of the most common
                # conversion defects -- the quest runs but does nothing.
                "filled": bool(value) and "none" not in value.lower(),
            })
        elif m := RE_OBJECTIVE.search(line):
            out["objectives"].append({"index": int(m.group(1)),
                                      "text": m.group(2).strip()})
        elif m := RE_SCRIPT.match(line):
            out["scripts"].append(m.group(1))
        elif m := RE_SCRIPT_VAR.match(line):
            # Last writer wins: several scripts can be attached to one quest
            # and `sqv` prints each one's variables in turn.  Prefix a name
            # that repeats so a collision cannot silently hide one script's
            # value behind another's.
            name, value = m.group(1), m.group(2).strip()
            if name in out["vars"] and out["vars"][name] != value:
                name = f"{name}#{len(out['vars'])}"
            out["vars"][name] = value
    return out


class QuestDebugger:
    def __init__(self, bridge: Bridge):
        self.b = bridge

    def sqv(self, quest: str) -> tuple[str, dict]:
        text = self.b.console(f"sqv {quest}")
        return text, parse_sqv(text)

    def stage(self, quest: str) -> int | None:
        text = self.b.console(f"getstage {quest}")
        m = re.search(r"(-?\d+)", text or "")
        return int(m.group(1)) if m else None


def _connect(args) -> Bridge:
    return Bridge().connect(retries=2)


def cmd_state(args) -> int:
    with _connect(args) as b:
        text, parsed = QuestDebugger(b).sqv(args.quest)
    if args.json:
        print(json.dumps(parsed, indent=2))
        return 0 if text.strip() else 1
    if not text.strip():
        print(f"no output for `sqv {args.quest}` -- is console capture working, "
              f"and does the quest exist?", file=sys.stderr)
        return 1
    print(text)
    return 0


def cmd_stage(args) -> int:
    with _connect(args) as b:
        d = QuestDebugger(b)
        stage = d.stage(args.quest)
        _, parsed = d.sqv(args.quest)
    result = {"quest": args.quest, "stage": stage, "running": parsed.get("running")}
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{args.quest}: stage {stage}, "
              f"{'running' if parsed.get('running') else 'not running'}")
    return 0 if stage is not None else 1


def cmd_setstage(args) -> int:
    """Set a stage and report exactly what Papyrus did in response.

    This is the pairing that matters: the stage fragment usually IS the thing
    under suspicion, so its VM output is the answer.
    """
    # One injection does read -> act -> read, so the before/after pair cannot be
    # separated by the game loop the way three round trips could be.
    script = (f"getstage {args.quest}\n"
              f"setstage {args.quest} {args.stage}\n"
              f"getstage {args.quest}")
    with _connect(args) as b:
        r = b.inject(script, settle_ms=args.settle_ms)

    stmts = r.get("statements", [])

    def _num(i: int):
        if i >= len(stmts):
            return None
        m = re.search(r"(-?\d+)", stmts[i].get("output", "") or "")
        return int(m.group(1)) if m else None

    before, after = _num(0), _num(2)
    out = {
        "quest": args.quest,
        "requested": args.stage,
        "stage_before": before,
        "stage_after": after,
        "applied": after == args.stage,
        "ok": r.get("ok"),
        "papyrus": r.get("papyrus") or [],
    }
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"{args.quest}: {before} -> {after} (requested {args.stage})")
        if not out["applied"]:
            print("  NOTE: the engine did not move to the requested stage; "
                  "a stage's conditions or its fragment may have rejected it")
        for line in out["papyrus"]:
            print(f"  VM| {line}")
    return 0 if out["applied"] else 1


def cmd_watch(args) -> int:
    """Poll a quest and print every change, for a bounded time.

    Bounded because an agent-driven session has a hard command timeout; a
    watcher that never returns burns the whole budget and reports nothing.

    By default this watches the STAGE only (one cheap `getstage` per tick).
    `--vars` widens it to every script variable the engine prints for the
    quest, which is what makes a conversation defect legible: the ORDER in
    which `speaker`, `convCount` and the stage move is the evidence, and a
    snapshot taken after the fact cannot show it.  Each tick is a single
    `sqv` round trip, so the extra fields cost nothing beyond the parse.

    Output is a transition log, not a state dump: a line is printed only
    when a field actually changes, timestamped from the start of the watch,
    and it is flushed per line so a run that outlives the command timeout
    still leaves usable evidence behind.
    """
    changes = []
    # Fields whose value is pure noise at poll rate (a countdown timer
    # changes every tick and would bury every real transition).  Watched
    # only when the user asks for them explicitly.
    noisy = {"TES4_LastTick", "TES4_SecondsPassed", "TES4_Now"}
    only = {f.strip().lower() for f in (args.only or "").split(",") if f.strip()}

    def _snapshot(d):
        if args.vars:
            _, parsed = d.sqv(args.quest)
            state = dict(parsed["vars"])
            state["<stage>"] = parsed["stage"]
            return state
        return {"<stage>": d.stage(args.quest)}

    def _watched(state):
        out = {}
        for k, v in state.items():
            if only:
                if k.lower() in only or k == "<stage>":
                    out[k] = v
            elif k not in noisy and not args.all_fields:
                out[k] = v
            elif args.all_fields:
                out[k] = v
        return out

    with _connect(args) as b:
        d = QuestDebugger(b)
        last = _watched(_snapshot(d))
        t0 = time.time()
        print(f"{args.quest}: watching {len(last)} field(s) from "
              f"stage {last.get('<stage>')}", file=sys.stderr)
        deadline = t0 + args.seconds
        stalled = False
        while time.time() < deadline:
            time.sleep(args.interval)
            try:
                now = _watched(_snapshot(d))
            except BridgeError as exc:
                # A load screen (or an alt-tab) stops the game draining SKSE's
                # task queue, so every poll times out until it comes back.
                # That is expected across a playthrough and must NOT end the
                # watch -- report the gap once and keep polling, or the run
                # the evidence lives in dies at the first door.
                if not stalled:
                    dt = round(time.time() - t0, 1)
                    print(f"  +{dt:6.1f}s  -- bridge stalled ({exc}); "
                          f"still watching", flush=True)
                    stalled = True
                continue
            if stalled:
                dt = round(time.time() - t0, 1)
                print(f"  +{dt:6.1f}s  -- bridge back", flush=True)
                stalled = False
                # NOTE: deliberately NOT re-baselining here.  The state moved
                # while we were blind, and those transitions (a stage that
                # advanced across a load screen) are exactly the evidence a
                # playthrough is being recorded for.  They are reported as
                # normal changes, timestamped at the moment we regained
                # sight -- the gap markers above bound when they really
                # happened.
            for key in sorted(set(last) | set(now)):
                before, after = last.get(key), now.get(key)
                if before != after:
                    dt = round(time.time() - t0, 1)
                    # Wall clock too: the Papyrus log is timestamped, so a
                    # transition here can be lined up against what the VM
                    # said at the same instant.
                    wall = time.strftime("%H:%M:%S")
                    changes.append({"t": dt, "wall": wall, "field": key,
                                    "from": before, "to": after})
                    label = "stage" if key == "<stage>" else key
                    mark = " <<<< STAGE" if key == "<stage>" else ""
                    print(f"  [{wall}] +{dt:7.1f}s  {label:<26} "
                          f"{before} -> {after}{mark}", flush=True)
            last = now
    if args.json:
        print(json.dumps({"quest": args.quest, "changes": changes,
                          "final": last}, indent=2))
    else:
        print(f"\n-- {len(changes)} change(s); final stage "
              f"{last.get('<stage>')}", file=sys.stderr)
    return 0


def cmd_record(args) -> int:
    """Record EVERYTHING that happens during a playthrough, in one timeline.

    `watch` answers "what did the quest state become".  That is not enough to
    debug a conversation: when the complaint is "he played the wrong lines",
    the question is WHICH LINE FIRED, IN WHAT ORDER -- an event stream, not a
    state sample.

    Three sources are merged here, all timestamped from one clock:

    * **console ring** (`console_log`) -- always-on, records console output
      whoever caused it, INCLUDING the game's own.  With `tdt` dialogue debug
      enabled in-game this carries topic/INFO selection, which is the actual
      per-line event record.  Drained by monotonic `seq`, so nothing is lost
      or double-counted between polls.
    * **quest variables** -- the conversation baton (`speaker`, `target`,
      `convCount`) and the stage, as `watch --vars` collects them.
    * **Papyrus VM** -- errors and any script output, which is where an
      aborted fragment shows up.

    Everything lands in one file in arrival order, so a wrong line can be
    read against the state that selected it.  Lines flush individually: a run
    that outlives the command timeout still leaves the evidence behind.
    """
    noisy = {"TES4_LastTick", "TES4_SecondsPassed", "TES4_Now", "convtimer",
             "convTimer"}
    out_path = args.out
    fh = open(out_path, "w", encoding="utf-8", buffering=1) if out_path else None

    def emit(kind: str, text: str) -> None:
        wall = time.strftime("%H:%M:%S")
        line = f"[{wall}] {kind:<7} {text}"
        print(line, flush=True)
        if fh:
            fh.write(line + "\n")

    seq = None
    with _connect(args) as b:
        d = QuestDebugger(b)
        # Prime the console ring cursor so we only report NEW output.
        try:
            r = b.console_log(limit=1)
            seq = r.get("seq")
        except BridgeError:
            pass
        _, parsed = d.sqv(args.quest)
        last = {k: v for k, v in parsed["vars"].items() if k not in noisy}
        last["<stage>"] = parsed["stage"]
        emit("START", f"{args.quest} stage={parsed['stage']} "
                      f"({len(last)} fields watched)")
        t0 = time.time()
        deadline = t0 + args.seconds
        stalled = False
        while time.time() < deadline:
            time.sleep(args.interval)
            # --- console event ring -------------------------------------
            try:
                r = b.console_log(limit=args.ring_limit)
                new_seq = r.get("seq")
                lines = r.get("lines") or []
                if seq is not None and new_seq is not None:
                    fresh = int(new_seq) - int(seq)
                    if fresh > 0:
                        for l in lines[-min(fresh, len(lines)):]:
                            if l.strip():
                                emit("CONSOLE", l.rstrip())
                seq = new_seq if new_seq is not None else seq
            except BridgeError:
                pass
            # --- quest state --------------------------------------------
            try:
                _, parsed = d.sqv(args.quest)
            except BridgeError as exc:
                if not stalled:
                    emit("GAP", f"bridge stalled ({exc}); still recording")
                    stalled = True
                continue
            if stalled:
                emit("GAP", "bridge back")
                stalled = False
            now = {k: v for k, v in parsed["vars"].items() if k not in noisy}
            now["<stage>"] = parsed["stage"]
            for key in sorted(set(last) | set(now)):
                before, after = last.get(key), now.get(key)
                if before != after:
                    kind = "STAGE" if key == "<stage>" else "VAR"
                    label = "stage" if key == "<stage>" else key
                    emit(kind, f"{label} {before} -> {after}")
            last = now
    if fh:
        fh.close()
    return 0


def cmd_aliases(args) -> int:
    with _connect(args) as b:
        _, parsed = QuestDebugger(b).sqv(args.quest)
    aliases = parsed["aliases"]
    if args.json:
        print(json.dumps(aliases, indent=2))
        return 0
    if not aliases:
        print("no aliases parsed (the quest may have none, or sqv printed "
              "a shape this parser does not recognise -- try `state`)")
        return 1
    unfilled = [a for a in aliases if not a["filled"]]
    for a in aliases:
        print(f"  {'OK  ' if a['filled'] else 'EMPTY'}  {a['name']:28} {a['value']}")
    if unfilled:
        print(f"\n{len(unfilled)} unfilled alias(es) -- a quest with an empty "
              f"alias usually runs but does nothing", file=sys.stderr)
    return 0


def cmd_scripts(args) -> int:
    """Attached scripts, plus any VM complaints about them."""
    with _connect(args) as b:
        _, parsed = QuestDebugger(b).sqv(args.quest)
        vm = b.vmlog(limit=200) if _has_vmlog(b) else {"lines": []}
    name = args.quest.lower()
    related = [ln for ln in vm.get("lines", []) if name in ln.lower()]
    out = {"scripts": parsed["scripts"], "vm_mentions": related}
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    print("attached scripts:")
    for s in parsed["scripts"] or ["  (none parsed)"]:
        print(f"  {s}")
    if related:
        print("\nrecent VM lines mentioning this quest:")
        for ln in related[-20:]:
            print(f"  {ln}")
    return 0


def _has_vmlog(b: Bridge) -> bool:
    try:
        return bool(b.capabilities().get("capabilities", {}).get("papyrus_capture"))
    except BridgeError:
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("    #", 1)[-1],
    )
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, help_ in (
        ("state", "full sqv output, parsed"),
        ("stage", "current stage and running flag"),
        ("aliases", "aliases and whether each is filled"),
        ("scripts", "attached scripts + VM lines mentioning the quest"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("quest")

    p = sub.add_parser("setstage", help="set a stage; report what Papyrus did")
    p.add_argument("quest")
    p.add_argument("stage", type=int)
    p.add_argument("--settle-ms", type=int, default=600)

    p = sub.add_parser("watch", help="poll the quest and print every change")
    p.add_argument("quest")
    p.add_argument("--seconds", type=float, default=60.0)
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--vars", action="store_true",
                   help="watch every script variable, not just the stage "
                        "(the ORDER of speaker/convCount/stage moves is what "
                        "makes a conversation defect legible)")
    p.add_argument("--only", default="",
                   help="comma-separated field names to watch (implies "
                        "--vars); the stage is always included")
    p.add_argument("--all-fields", action="store_true",
                   help="also watch per-tick noise (TES4_LastTick etc.)")

    p = sub.add_parser("record",
                       help="record console events + quest state + VM output "
                            "in ONE timeline (for a whole playthrough)")
    p.add_argument("quest")
    p.add_argument("--seconds", type=float, default=3600.0)
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("--ring-limit", type=int, default=200,
                   help="console lines pulled per tick")
    p.add_argument("--out", default="",
                   help="also write the timeline to this file")

    args = ap.parse_args(argv)
    # Naming fields to watch only makes sense against the full variable dump.
    if getattr(args, "only", "") and not getattr(args, "vars", False):
        args.vars = True
    fn = {
        "state": cmd_state, "stage": cmd_stage, "setstage": cmd_setstage,
        "watch": cmd_watch, "aliases": cmd_aliases, "scripts": cmd_scripts,
        "record": cmd_record,
    }[args.cmd]
    try:
        return fn(args)
    except BridgeError as exc:
        print(f"bridge error [{exc.code}]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
