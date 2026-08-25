#!/usr/bin/env python3
"""Measure what CONDITION EVALUATION actually costs in the running game.

WHY THIS EXISTS
---------------
NPC activation and a scripted Say() share exactly one expensive engine step:
the engine walks the candidate INFOs of a topic and evaluates each one's CTDA
stack until one qualifies.  Everything else about the two paths differs, so a
stutter common to BOTH points here.  Papyrus-side timing cannot see it (the
walk is native), and stack sampling cannot be used -- taking thousands of
`~* k` stack walks suspends every thread and FREEZES the game (measured
2026-08-16; the attach is non-invasive, the sampling is not).

So measure it where it happens: hook the condition evaluator and count.

THE TARGET
----------
`TESCondition::Run` -- Address Library id **21971** (offset 0), the function
that evaluates one condition stack.  Identified statically, not guessed:

  * The CTDA function-info table is at rva 0x1e45a10 (1.6.659), stride 0x50,
    indexed by CTDA function id.  Verified by decoding it: id 426 ->
    "GetIsVoiceType", id 630 -> "GetVMScriptVariable", matching the function
    numbers this project's converter emits.
  * Exactly five sites reference that table.  0x303cd1 is the accessor
    (`id*0x50 + base; ret`, id 21963).  0x305460 is the consumer that indexes
    the table and dispatches on the entry -- the evaluator (id 21971).

Identifying by the table it indexes is the proof of identity that
project_find_engine_functions_via_rtti requires; nothing here rests on a
plausible-looking prologue.

USE
---
Bridge must be up (game under skse64_loader.exe with TESGameBridge.dll).

    # 1. verify the target resolves and is safe to hook (DRY RUN, no hook)
    python tools/ctda_cost_probe.py --analyze

    # 2. arm it, then play until the stutter happens
    python tools/ctda_cost_probe.py --arm

    # 3. read the counter -- how many condition evaluations since arming
    python tools/ctda_cost_probe.py --read

    # bracket ONE action (activate an NPC, trigger a Say) and count it:
    python tools/ctda_cost_probe.py --bracket 8
    #   -> "press activate now", counts evaluations over 8 seconds

    # 4. always remove the hook when done
    python tools/ctda_cost_probe.py --remove

WHAT THE NUMBER MEANS
---------------------
The converted plugin carries 110,634 CTDAs total, INFOGENERAL alone holding
33,872 across 1,854 INFOs (measured from the built ESM by
the removed dialog_selection_cost.py).  Vanilla Skyrim's mean is 3.7 per topic.

  * ~thousands of evaluations per activation  -> the walk IS the stutter, and
    the fix is to cut the per-topic condition volume.
  * ~tens                                     -> the engine rejects topics
    cheaply before the walk; condition volume is NOT the cause and this whole
    line of investigation is dead.  Say so and look elsewhere.

That is a decidable question, which is the point.

🛑 A hook on a HOT function can crash the game (a compile-finalizer hook did,
2026-08-14).  --analyze first, ALWAYS, and --remove when finished.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from game_bridge import Bridge, BridgeError  # noqa: E402

# TESCondition::Run -- see module docstring for how this was identified.
CTDA_EVAL_ID = 21971
LABEL = "ctda_eval"


def _hits(b: Bridge, label: str = LABEL) -> int:
    """Total recorded calls for our hook, 0 if not installed.

    🛑 The count comes from `hook` WITH NO ARGUMENTS (which returns
    {"hooks": [...]}), NOT from `hookstats`.  CmdHookStats reports only the
    built-in console/papyrus counters and says nothing whatever about generic
    hooks -- reading it for a per-hook `hits` returns 0 unconditionally, which
    is indistinguishable from "the function never ran".  That cost a whole
    90-second measurement window reading a hardcoded zero.
    """
    try:
        listing = b.request("hook")
    except BridgeError:
        return 0
    for entry in (listing.get("hooks") or []):
        if entry.get("label") == label:
            return int(entry.get("hits", 0))
    return 0


def cmd_analyze(b: Bridge) -> int:
    r = b.resolve(id=CTDA_EVAL_ID)
    print(f"id {CTDA_EVAL_ID} -> {r}")
    a = b.hook(id=CTDA_EVAL_ID, label=LABEL, analyze=True)
    print(f"analyze: {a}")
    ok = a.get("hookable")
    print("\nSAFE to hook" if ok else
          "\nNOT hookable -- do not force it; the blocking instruction is above")
    return 0 if ok else 1


def cmd_arm(b: Bridge) -> int:
    a = b.hook(id=CTDA_EVAL_ID, label=LABEL, analyze=True)
    if not a.get("hookable"):
        print(f"refusing to hook: {a}")
        return 1
    r = b.hook(id=CTDA_EVAL_ID, label=LABEL, keep=0)
    print(f"armed: {r}")
    print(f"baseline hits: {_hits(b)}")
    print("\nPlay now. Then: --read (or --bracket) ... and --remove when done.")
    return 0


def cmd_read(b: Bridge) -> int:
    print(f"condition evaluations recorded: {_hits(b)}")
    return 0


def cmd_bracket(b: Bridge, seconds: float) -> int:
    """Count evaluations across one deliberate action."""
    if _hits(b) == 0:
        try:
            b.hook(id=CTDA_EVAL_ID, label=LABEL, keep=0)
        except BridgeError as e:
            print(f"could not arm: {e}")
            return 1
    before = _hits(b)
    print(f"\n>>> DO IT NOW -- activate the NPC / trigger the line "
          f"({seconds:.0f}s window) <<<", flush=True)
    time.sleep(seconds)
    after = _hits(b)
    delta = after - before
    print(f"\ncondition evaluations during window: {delta}")
    if delta > 2000:
        print("  -> THOUSANDS: the condition walk is the cost. Cutting "
              "per-topic\n     CTDA volume is the fix.")
    elif delta > 0:
        print("  -> modest: the engine is NOT walking every INFO. Condition "
              "volume\n     is not the stutter; look elsewhere.")
    else:
        print("  -> ZERO: hook never fired. Wrong function, or nothing "
              "happened in\n     the window. Do not interpret this as 'cheap'.")
    return 0


def cmd_remove(b: Bridge) -> int:
    stats = b.hookstats()
    for i, entry in enumerate(stats.get("hooks") or []):
        if entry.get("label") == LABEL:
            print(f"removing hook index {entry.get('index', i)}")
            print(b.hook(remove=entry.get("index", i)))
            return 0
    print("no hook installed under this label")
    return 0


def cmd_measure(b: Bridge, window: float, samples: int) -> int:
    """Whole measurement on ONE connection: analyze, arm, sample, remove.

    🛑 ONE CONNECTION, NOT FIVE.  The plugin's pipe server is strictly serial
    (pipe_server.cpp): on disconnect it does DisconnectNamedPipe / CloseHandle
    and only THEN loops round to CreateNamedPipeA, so between those two points
    no pipe instance exists and a client connecting in that window fails with
    error 3 / errno 22.  Running analyze/arm/read as separate processes hit
    exactly that race -- --analyze succeeded and the --arm seconds later
    "could not connect", which looks like the game is down when it is fine.
    Retrying is the wrong fix; not disconnecting is the right one.
    """
    r = b.resolve(id=CTDA_EVAL_ID)
    if not r.get("found"):
        print(f"target id {CTDA_EVAL_ID} did not resolve: {r}")
        return 1
    a = b.hook(id=CTDA_EVAL_ID, label=LABEL, analyze=True)
    if not a.get("hookable"):
        print(f"refusing to hook (not relocatable): {a}")
        return 1
    print(f"target ok: rva 0x{r['rva']:x}, stolen {a.get('stolen')} bytes")

    print(b.hook(id=CTDA_EVAL_ID, label=LABEL, keep=0).get("status", "armed"))
    try:
        base = _hits(b)
        print(f"\n>>> PLAY NOW -- sampling {samples} windows of {window:.0f}s "
              f"<<<\n", flush=True)
        prev = base
        peak = 0
        for i in range(samples):
            time.sleep(window)
            now = _hits(b)
            delta = now - prev
            prev = now
            peak = max(peak, delta)
            print(f"  [{(i+1)*window:5.0f}s] +{delta:>9,} evals "
                  f"({delta/window:>10,.0f}/s)", flush=True)
        total = prev - base
        print(f"\ntotal condition evaluations: {total:,} over "
              f"{samples*window:.0f}s")
        print(f"peak window rate: {peak/window:,.0f}/s")
    finally:
        # ALWAYS unhook on the same connection, even on Ctrl-C: a counter hook
        # left installed on a hot function is a permanent tax on the session.
        stats = b.hookstats()
        for i, entry in enumerate(stats.get("hooks") or []):
            if entry.get("label") == LABEL:
                b.hook(remove=entry.get("index", i))
                print("hook removed")
                break
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--analyze", action="store_true",
                   help="dry run: resolve the target and check hookability")
    g.add_argument("--arm", action="store_true", help="install the counter hook")
    g.add_argument("--read", action="store_true", help="read the counter")
    g.add_argument("--bracket", type=float, metavar="SECONDS",
                   help="count evaluations across one action")
    g.add_argument("--remove", action="store_true", help="remove the hook")
    g.add_argument("--measure", action="store_true",
                   help="★ the whole measurement on ONE connection: analyze, "
                        "arm, sample per window, unhook. Use this.")
    ap.add_argument("--window", type=float, default=5.0,
                    help="seconds per sample window (--measure)")
    ap.add_argument("--samples", type=int, default=12,
                    help="number of windows (--measure)")
    args = ap.parse_args(argv)

    try:
        with Bridge().connect(retries=2) as b:
            if args.analyze:
                return cmd_analyze(b)
            if args.arm:
                return cmd_arm(b)
            if args.read:
                return cmd_read(b)
            if args.bracket is not None:
                return cmd_bracket(b, args.bracket)
            if args.remove:
                return cmd_remove(b)
            if args.measure:
                return cmd_measure(b, args.window, args.samples)
    except BridgeError as e:
        print(f"bridge unavailable: {e}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
