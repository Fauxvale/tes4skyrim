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
    """Poll a quest's stage and print every change, for a bounded time.

    Bounded because an agent-driven session has a hard command timeout; a
    watcher that never returns burns the whole budget and reports nothing.
    """
    changes = []
    with _connect(args) as b:
        d = QuestDebugger(b)
        last = d.stage(args.quest)
        t0 = time.time()
        print(f"{args.quest}: starting at stage {last}", file=sys.stderr)
        deadline = t0 + args.seconds
        while time.time() < deadline:
            time.sleep(args.interval)
            try:
                now = d.stage(args.quest)
            except BridgeError as exc:
                print(f"  (bridge error: {exc})", file=sys.stderr)
                continue
            if now != last:
                dt = round(time.time() - t0, 1)
                changes.append({"t": dt, "from": last, "to": now})
                print(f"  +{dt:6.1f}s  stage {last} -> {now}", flush=True)
                last = now
    if args.json:
        print(json.dumps({"quest": args.quest, "changes": changes,
                          "final_stage": last}, indent=2))
    else:
        print(f"\n-- {len(changes)} change(s); final stage {last}", file=sys.stderr)
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

    p = sub.add_parser("watch", help="poll the stage and print every change")
    p.add_argument("quest")
    p.add_argument("--seconds", type=float, default=60.0)
    p.add_argument("--interval", type=float, default=1.0)

    args = ap.parse_args(argv)
    fn = {
        "state": cmd_state, "stage": cmd_stage, "setstage": cmd_setstage,
        "watch": cmd_watch, "aliases": cmd_aliases, "scripts": cmd_scripts,
    }[args.cmd]
    try:
        return fn(args)
    except BridgeError as exc:
        print(f"bridge error [{exc.code}]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
