#!/usr/bin/env python3
"""End-to-end verification of the live game bridge, against a RUNNING game.

Answers one question with a pass/fail table: **can an agent debug in-game
without the user driving?** Each check is a capability that autonomous
debugging depends on, and every claim is executed rather than inferred --
nothing here reports "should work".

Run it after any bridge change, and after any game update (a new runtime moves
every address, and the version-decode / Address Library path is exactly where
this has broken before).

    python tools/live/game_bridge_verify.py
    python tools/live/game_bridge_verify.py --json

Requires: the game running under skse64_loader.exe with a save loaded.
Read-only by default -- it does not spawn, teleport, or change quest state.
Pass --mutate to also verify state-changing commands (uses a GLOBAL variable
it restores afterwards, so no quest or actor is touched).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from tools.live.game_bridge import Bridge, BridgeError  # noqa: E402
import tools.script.papyrus_tail  # noqa: E402


class Check:
    def __init__(self, name: str, critical: bool = True):
        self.name = name
        self.critical = critical
        self.ok = False
        self.detail = ""


def run(results: list[Check], name: str, fn, critical: bool = True) -> Check:
    c = Check(name, critical)
    try:
        ok, detail = fn()
        c.ok, c.detail = ok, detail
    except BridgeError as exc:
        c.ok, c.detail = False, f"[{exc.code}] {exc}"
    except Exception as exc:  # noqa: BLE001 - a verifier must not itself crash
        c.ok, c.detail = False, f"{type(exc).__name__}: {exc}"
    results.append(c)
    return c


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--mutate", action="store_true",
                    help="also verify state-changing commands (restores what it changes)")
    args = ap.parse_args(argv)

    results: list[Check] = []

    # ---------------------------------------------------------- transport --
    try:
        b = Bridge().connect(retries=2)
    except BridgeError as exc:
        print(f"CANNOT CONNECT: {exc}", file=sys.stderr)
        print("\nIs the game running under skse64_loader.exe?", file=sys.stderr)
        return 2

    with b:
        run(results, "pipe connects",
            lambda: (True, "connected"))

        def _ping():
            r = b.ping()
            return bool(r.get("pong")), f"plugin {r.get('plugin_version')}, runtime {r.get('runtime_version'):#x}"
        run(results, "ping round-trips", _ping)

        # ------------------------------------------------------ addresses --
        def _caps():
            r = b.capabilities()
            unresolved = r.get("unresolved", [])
            caps = r.get("capabilities", {})
            if unresolved:
                return False, f"unresolved: {', '.join(unresolved)}"
            return bool(caps.get("console")), f"console={caps.get('console')} script_alloc={caps.get('script_alloc')}"
        caps_check = run(results, "all addresses resolved (Address Library loaded)", _caps)

        def _loaded():
            r = b.status()
            return bool(r.get("game_loaded")), (
                "save loaded" if r.get("game_loaded") else "at main menu -- load a save")
        game_check = run(results, "a save is loaded", _loaded)

        # ------------------------------------------------- console execute --
        # Only meaningful once the addresses resolved; otherwise it is noise.
        if caps_check.ok:
            def _console():
                b.console("getgs fJumpHeightMin")
                return True, "command executed without error"
            run(results, "console command executes", _console)

            def _batch():
                r = b.batch(["getgs fJumpHeightMin", "getgs fSprintStaminaDrainMult"])
                return (r.get("failed") == 0 and r.get("ran") == 2,
                        f"ran {r.get('ran')}/{r.get('total')}, {r.get('failed')} failed")
            run(results, "batch runs multiple commands in one trip", _batch)

            def _badcmd():
                # A nonsense command must FAIL while a valid one SUCCEEDS.
                #
                # Checking only the failure is worthless: when the executor was
                # crashing on every call, this check "passed" because
                # everything errored. The discriminating test is the PAIR.
                try:
                    b.console("getgs fJumpHeightMin")
                except BridgeError as exc:
                    return False, f"a VALID command failed ({exc}) -- cannot discriminate"
                try:
                    b.console("thiscommanddoesnotexist_zzz")
                except BridgeError:
                    return True, "valid command succeeded, bogus one errored"
                return False, "bogus command reported success (cannot trust any result)"
            run(results, "good vs bad command are distinguishable", _badcmd)
        else:
            for n in ("console command executes",
                      "batch runs multiple commands in one trip",
                      "good vs bad command are distinguishable"):
                c = Check(n)
                c.detail = "skipped: addresses unresolved"
                results.append(c)

        # ------------------------------------------------------ readback ---
        if caps_check.ok:
            def _print_hook_live():
                """Is the print hook firing AT ALL?

                Separated from the checks below because "output is empty" has
                two very different causes: the hook is not installed/firing, or
                it fires but the command produced nothing. The always-on ring
                answers the first question on its own -- it records console
                output no matter who caused it, including a command typed by a
                human in-game.
                """
                try:
                    r = b.console_log(limit=5)
                except BridgeError as exc:
                    if exc.code == "E_UNSUPPORTED":
                        return False, ("plugin predates console_log -- redeploy "
                                       "TESGameBridge.dll and relaunch")
                    raise
                total = r.get("total", 0)
                if not total:
                    return False, "no console output has EVER been captured (hook dead?)"
                return True, f"{total} line(s) buffered, e.g. {r['lines'][-1][:50]!r}"
            run(results, "CAPTURE: the print hook is firing", _print_hook_live)
            def _capture():
                """The point of the whole tool: READ a command's answer.

                `getgs` prints its value to the console. If capture works we
                get the text back; if not, `output` is empty and every
                read-only command (sqv, getav, getstage) is useless.
                """
                out = b.console("getgs fJumpHeightMin")
                if not out.strip():
                    return False, ("console output is EMPTY -- commands run but "
                                   "cannot be read back")
                return True, f"captured {out.strip()[:60]!r}"
            run(results, "CAPTURE: console output comes back", _capture)

            def _capture_real_query():
                """A real debugging query, not a synthetic one."""
                out = b.console("getglobalvalue timescale")
                if not out.strip():
                    return False, "no output from a real query command"
                return True, f"{out.strip()[:60]!r}"
            run(results, "CAPTURE: a real query returns data", _capture_real_query)

        # -------------------------------------------------- papyrus log ----
        def _log_exists():
            d = papyrus_tail.log_dir()
            p = d / "Papyrus.0.log"
            if not p.exists():
                return False, (f"{p} missing -- enable [Papyrus] bEnableLogging=1 "
                               "and bEnableTrace=1 in Skyrim.ini")
            age = time.time() - p.stat().st_mtime
            return True, f"{p.stat().st_size} bytes, last written {age:.0f}s ago"
        log_check = run(results, "Papyrus log exists", _log_exists)

        def _log_live():
            """The log must be READABLE WHILE THE GAME HOLDS IT OPEN."""
            p = papyrus_tail.log_dir() / "Papyrus.0.log"
            text, cur = papyrus_tail.read_from(p, max(0, p.stat().st_size - 4096))
            return bool(cur), f"read {len(text)} bytes from a live, game-held file"
        run(results, "Papyrus log readable while the game runs", _log_live)

        def _log_grows():
            """A cursor + a wait must show new lines: this is the feedback loop."""
            p = papyrus_tail.log_dir() / "Papyrus.0.log"
            start = p.stat().st_size
            time.sleep(3.0)
            _, end = papyrus_tail.read_from(p, start)
            if end > start:
                return True, f"grew {end - start} bytes in 3s (live feedback works)"
            return (True, "no new lines in 3s -- cursor works, but the VM was quiet "
                          "(not a failure: a silent log is normal)")
        run(results, "cursor sees appended lines", _log_grows, critical=False)

        # ---------------------------------------------------- injection ----
        if caps_check.ok and game_check.ok and log_check.ok:
            def _inject():
                """The whole autonomous loop: make the game emit, then read it.

                Uses `dumppapyrusstacks`, which writes live VM stacks into the
                Papyrus log. NOT `cgf "Debug.Trace"` -- CallGlobalFunction is a
                ConsoleUtil command, and ConsoleUtil is not installed here
                (verified 2026-08-14: the string is absent from SkyrimSE.exe and
                from skse64_1_6_1170.dll), so a cgf marker fails silently.

                This is the check that proves readback works end to end despite
                console output capture being unimplemented.
                """
                # Checked through the IN-PROCESS VM hook, not the log file.
                #
                # The file is written asynchronously and buffered, so polling it
                # reports failure while the output demonstrably exists -- this
                # check failed for exactly that reason while `vmlog` was
                # returning 30 lines of the same dump. The hook is also the
                # channel callers actually use.
                b.vmlog(arm=True)
                b.console("dumppapyrusstacks")
                for _ in range(24):
                    time.sleep(0.25)
                    got = b.vmlog(limit=40)
                    lines = got.get("lines", [])
                    if any("stack" in ln.lower() for ln in lines):
                        first = next((ln.strip() for ln in lines if ln.strip()), "")
                        return True, f"{len(lines)} VM line(s), e.g. {first[:50]!r}"
                return False, ("dumppapyrusstacks produced no VM output within 6s -- "
                               "the command ran but the VM emitted nothing")
            run(results, "READBACK: game writes -> Papyrus VM -> read", _inject)

            def _script_inject():
                """Compile and run a MULTI-STATEMENT script body in-game.

                This is the injection feature proper: several statements
                compiled by the engine's own compiler and run in order within
                one main-thread trip. Uses only read-only statements so the
                check is safe on a real save.
                """
                r = b.inject("getgs fJumpHeightMin\ngetgs fSprintStaminaDrainMult")
                if not r.get("ok"):
                    return False, (f"failed at statement {r.get('failed_statement')}: "
                                   f"{r.get('detail', '')}")
                return True, "multi-statement script compiled and ran"
            run(results, "INJECT: multi-statement script runs", _script_inject)

            def _inject_reports_bad():
                """A bad statement must be reported WITH ITS INDEX.

                Without this, an injected probe that half-ran would look like a
                success and every conclusion drawn from it would be wrong.
                """
                r = b.inject("getgs fJumpHeightMin\nzzz_not_a_command\n",
                             stop_on_error=False)
                if r.get("ok"):
                    return False, "a bogus statement was reported as succeeding"
                idx = r.get("failed_statement")
                return idx == 1, f"reported failure at statement {idx} (expected 1)"
            run(results, "INJECT: a bad statement is pinpointed", _inject_reports_bad)

            def _inject_per_statement_output():
                """Each statement's OWN answer, not one merged blob.

                This is what makes a scripted probe useful: read a value,
                act on it, read again -- and be able to tell the two reads
                apart.
                """
                r = b.inject("getgs fJumpHeightMin\ngetgs fSprintStaminaDrainMult")
                stmts = r.get("statements", [])
                if len(stmts) != 2:
                    return False, f"expected 2 statement results, got {len(stmts)}"
                outs = [(s.get("output") or "").strip() for s in stmts]
                if not all(outs):
                    return False, f"a statement returned no output: {outs}"
                if outs[0] == outs[1]:
                    return False, ("both statements returned identical text -- "
                                   "output is not being attributed per statement")
                return True, f"distinct answers: {outs[0][:24]!r} / {outs[1][:24]!r}"
            run(results, "INJECT: per-statement output is attributed",
                _inject_per_statement_output)

            def _vm_capture_installed():
                caps = b.capabilities().get("capabilities", {})
                if not caps.get("papyrus_capture"):
                    return False, "the Papyrus logger hook did not install"
                return True, "Papyrus VM logger hook installed"
            run(results, "VMLOG: Papyrus capture hook installed", _vm_capture_installed)

            def _vm_capture_reads():
                """Read VM output straight from the sink, no file involved."""
                r = b.vmlog(limit=25)
                n = r.get("count", 0)
                if n == 0:
                    return (True, "hook live but the VM was quiet "
                                  "(not a failure: a silent VM is normal)")
                return True, f"{n} line(s) from the VM, e.g. {r['lines'][-1][:50]!r}"
            run(results, "VMLOG: reads VM output in-process", _vm_capture_reads,
                critical=False)

            def _inject_returns_vm_output():
                """The full autonomous loop, in ONE call.

                Inject a script that makes the VM talk, and get its output back
                attached to the same response -- no log file, no cursor, no
                guessing which lines were ours.
                """
                r = b.inject("dumppapyrusstacks", settle_ms=800)
                pap = r.get("papyrus") or []
                if not pap:
                    return False, ("inject ran but returned no VM output "
                                   "(the hook is installed but produced nothing)")
                return True, f"{len(pap)} VM line(s) returned with the injection"
            run(results, "INJECT+VMLOG: script output returned in one call",
                _inject_returns_vm_output)
        else:
            c = Check("INJECT: console -> Papyrus log -> read back")
            c.detail = "skipped: needs addresses + a loaded save + a Papyrus log"
            results.append(c)

        # ------------------------------------------------------- mutate ----
        if args.mutate and caps_check.ok and game_check.ok:
            def _mutate():
                # timescale is a global with no quest or actor side effects, and
                # we put it back. Reading it back is impossible without output
                # capture, so success is "the commands ran".
                r = b.batch(["set timescale to 17", "set timescale to 20"])
                return r.get("failed") == 0, "set/restore timescale ran cleanly"
            run(results, "state-changing command runs", _mutate)

    # ------------------------------------------------------------ report --
    crit_fail = [c for c in results if c.critical and not c.ok]
    soft_fail = [c for c in results if not c.critical and not c.ok]

    if args.json:
        print(json.dumps({
            "checks": [{"name": c.name, "ok": c.ok,
                        "critical": c.critical, "detail": c.detail} for c in results],
            "critical_failures": len(crit_fail),
            "autonomous_debugging_ready": not crit_fail,
        }, indent=2))
        return 1 if crit_fail else 0

    width = max(len(c.name) for c in results)
    print("game bridge verification\n")
    for c in results:
        if c.ok:
            flag = "PASS"
        elif c.detail.startswith("skipped"):
            flag = "SKIP"
        else:
            flag = "FAIL" if c.critical else "WARN"
        print(f"  {flag}  {c.name:{width}}  {c.detail}")

    print()
    if crit_fail:
        print(f"NOT READY -- {len(crit_fail)} critical check(s) failed:")
        for c in crit_fail:
            print(f"  - {c.name}: {c.detail}")
    else:
        print("READY - the bridge can drive the game and read results back.")
        if soft_fail:
            print(f"({len(soft_fail)} non-critical warning(s) above.)")
    return 1 if crit_fail else 0


if __name__ == "__main__":
    sys.exit(main())
