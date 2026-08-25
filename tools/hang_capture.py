#!/usr/bin/env python3
"""Capture a HUNG or crash-deadlocked SkyrimSE for offline analysis.

WHY THIS EXISTS
---------------
A hang is the most informative failure this project sees -- the faulting state
is still resident and every pointer is live -- and it is also the easiest to
lose.  On 2026-08-22 a reproducible freeze at `tes4tamriel 0 -30` was attached
to live, diagnosed down to a corrupted list node, and then LOST because nothing
had been written to disk before the user closed the game.  A full dump costs
one command and preserves everything; not taking it cost a whole reproduction.

So the order here is absolute: **DUMP FIRST, ANALYSE SECOND.**  Analysis of a
dump can be redone forever; a live process cannot be recovered.

It also handles the specific failure mode seen there: CrashLoggerSSE can
DEADLOCK inside its own handler (`MSVCP140!_Mtx_lock` ->
`RtlpAcquireSRWLockExclusiveContended` -> `NtWaitForAlertByThreadId`), so the
game has really CRASHED but no crash log is ever written and the process just
sits there.  `--triage` recognises that pattern and recovers the original
exception CONTEXT off the stack, which is the only place the true faulting
registers survive.

    # the one that matters -- run this the moment the game hangs
    python tools/hang_capture.py

    # dump only, no analysis (fastest; ~8GB working set -> big file)
    python tools/hang_capture.py --dump-only

    # analyse without dumping (only when disk is short and you accept the risk)
    python tools/hang_capture.py --triage --no-dump

    # work on a dump taken earlier
    python tools/hang_capture.py --analyse-dump path\\to\\Skyrim.dmp

🛑 NON-INVASIVE ONLY.  cdb is attached with `-pv`, which cannot suspend or
resume threads.  NEVER use `~* k` on the live game: it suspends every thread to
walk them and has frozen the game outright in a previous session.  Per-thread
CPU sampling here goes through the Windows process API, not the debugger.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

CDB_CANDIDATES = [
    r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe",
    r"C:\Program Files\Windows Kits\10\Debuggers\x64\cdb.exe",
]

DEFAULT_OUT = Path("temp/hangs")


def find_cdb(explicit: str | None = None) -> str:
    if explicit:
        if not Path(explicit).is_file():
            sys.exit(f"cdb not found at {explicit}")
        return explicit
    for c in CDB_CANDIDATES:
        if Path(c).is_file():
            return c
    sys.exit("cdb.exe not found -- install the Windows SDK Debugging Tools, "
             "or pass --cdb")


def find_pid(name: str = "SkyrimSE") -> int | None:
    """PID of the running game, via tasklist (no extra dependencies)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return None
    m = re.search(r'"%s\.exe","(\d+)"' % re.escape(name), out)
    return int(m.group(1)) if m else None


def run_cdb(cdb: str, pid: int, commands: str, timeout: int = 600) -> str:
    """Attach NON-INVASIVELY (-pv) and run one command string."""
    cmd = [cdb, "-pv", "-p", str(pid), "-c", commands + "; q"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return "<cdb timed out>"
    return (p.stdout or "") + (p.stderr or "")


def run_cdb_dump(cdb: str, dump: Path, commands: str,
                 timeout: int = 600) -> str:
    cmd = [cdb, "-z", str(dump), "-c", commands + "; q"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return "<cdb timed out>"
    return (p.stdout or "") + (p.stderr or "")


def take_dump(cdb: str, pid: int, out_dir: Path) -> Path | None:
    """Full memory dump.  This runs BEFORE any analysis, always."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d-%H-%M-%S")
    dump = out_dir / f"SkyrimSE-hang-{stamp}.dmp"
    print(f"[dump] writing full dump -> {dump}")
    print("[dump] this is several GB and takes a minute; do NOT close the game")
    t0 = time.time()
    out = run_cdb(cdb, pid, f".dump /ma {dump}", timeout=900)
    if dump.is_file():
        mb = dump.stat().st_size / (1 << 20)
        print(f"[dump] OK  {mb:,.0f} MB in {time.time()-t0:.0f}s")
        return dump
    print("[dump] FAILED -- cdb said:")
    print("\n".join(out.splitlines()[-15:]))
    return None


# --- thread sampling -------------------------------------------------------
# Deliberately NOT done through the debugger: walking threads in cdb suspends
# them.  This uses the process API instead, which only reads counters.

_PS_SAMPLE = r"""
$p = Get-Process -Id {pid} -ErrorAction Stop
$s = @{{}}
foreach ($t in $p.Threads) {{ $s[$t.Id] = $t.TotalProcessorTime.TotalMilliseconds }}
Start-Sleep -Seconds {secs}
$p.Refresh()
$rows = foreach ($t in $p.Threads) {{
  if ($s.ContainsKey($t.Id)) {{
    $d = $t.TotalProcessorTime.TotalMilliseconds - $s[$t.Id]
    if ($d -gt 0) {{ "{{0}} {{1}} {{2}} {{3}}" -f $t.Id, [math]::Round($d,0), $t.ThreadState, $t.WaitReason }}
  }}
}}
"RESPONDING {{0}}" -f $p.Responding
$rows | Sort-Object {{ [int]($_ -split ' ')[1] }} -Descending | Select-Object -First 15
"""


def sample_threads(pid: int, secs: int = 3) -> tuple[bool, list[str]]:
    script = _PS_SAMPLE.format(pid=pid, secs=secs)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True,
                             timeout=secs + 60).stdout
    except Exception:
        return True, []
    responding = True
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("RESPONDING"):
            responding = line.split()[-1].lower() == "true"
        elif line:
            rows.append(line)
    return responding, rows


def classify(responding: bool, rows: list[str]) -> str:
    busy = 0
    for r in rows:
        parts = r.split()
        if len(parts) >= 2 and parts[1].isdigit():
            busy += int(parts[1])
    if busy > 1500:
        return ("SPIN -- a thread is burning CPU.  Look for an unbounded loop "
                "(the angle-normalize class of bug): find the hottest thread "
                "and disassemble where it sits.")
    return ("DEADLOCK / STALL -- nothing is running.  If the main thread is in "
            "UnhandledExceptionFilter the game has already CRASHED and the "
            "logger is stuck; recover the exception CONTEXT (--triage does "
            "this) because no crash log will ever be written.")


# --- triage ----------------------------------------------------------------

_CXR_FRAME = re.compile(r"([0-9a-f`]+)\s+[0-9a-f`]+\s+ntdll!KiUserExceptionDispatch")


def triage(cdb: str, pid: int | None, dump: Path | None) -> None:
    """Main-thread stack, then the ORIGINAL exception context if it crashed."""
    def go(commands, timeout=600):
        if dump is not None:
            return run_cdb_dump(cdb, dump, commands, timeout)
        return run_cdb(cdb, pid, commands, timeout)

    print("\n[triage] main thread stack (~0s only -- never `~* k`)")
    stack = go("~0s; k 40")
    interesting = [l for l in stack.splitlines()
                   if re.search(r"^[0-9a-f`]{8,}\s|Call Site|\(Inline", l)]
    for l in interesting[:40]:
        print("   " + l.rstrip())

    crashed = "UnhandledExceptionFilter" in stack
    if not crashed:
        print("\n[triage] no UnhandledExceptionFilter frame -- this is a true "
              "hang, not a post-crash deadlock.")
        return

    print("\n[triage] *** UnhandledExceptionFilter on the stack ***")
    print("[triage] the game CRASHED; the logger deadlocked, so no crash log "
          "exists.  Recovering the original exception CONTEXT.")

    m = _CXR_FRAME.search(stack)
    if not m:
        print("[triage] could not locate the KiUserExceptionDispatch frame; "
              "dump the stack by hand with `k` and `.cxr <child-sp>`.")
        return
    child_sp = m.group(1)
    print(f"[triage] KiUserExceptionDispatch child-SP = {child_sp}")
    ctx = go(f".cxr {child_sp}")
    for l in ctx.splitlines():
        if re.search(r"r(ax|bx|cx|dx|si|di|ip|sp|bp)=|^r\d|SkyrimSE\+|"
                     r"^[A-Za-z_]+![A-Za-z_]", l):
            print("   " + l.rstrip())
    print("\n[triage] the faulting RVA above translates with:")
    print("   python tools/address_lib.py --rva <rva> --from 1.6.1170")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Capture and triage a hung / crash-deadlocked SkyrimSE.")
    ap.add_argument("--pid", type=int, help="target PID (default: find it)")
    ap.add_argument("--cdb", help="path to cdb.exe")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help=f"dump directory (default {DEFAULT_OUT})")
    ap.add_argument("--dump-only", action="store_true",
                    help="write the dump and stop")
    ap.add_argument("--no-dump", action="store_true",
                    help="skip the dump (NOT recommended -- the live process "
                         "is unrecoverable once it closes)")
    ap.add_argument("--triage", action="store_true",
                    help="run triage (implied unless --dump-only)")
    ap.add_argument("--analyse-dump", help="triage an existing .dmp instead")
    ap.add_argument("--sample-seconds", type=int, default=3)
    args = ap.parse_args()

    cdb = find_cdb(args.cdb)

    if args.analyse_dump:
        d = Path(args.analyse_dump)
        if not d.is_file():
            sys.exit(f"no such dump: {d}")
        triage(cdb, None, d)
        return 0

    pid = args.pid or find_pid()
    if not pid:
        sys.exit("SkyrimSE is not running (and no --pid given)")
    print(f"[hang] SkyrimSE PID {pid}")

    # 1. DUMP FIRST.  Everything else is recoverable from it.
    dump = None
    if not args.no_dump:
        dump = take_dump(cdb, pid, Path(args.out))
        if dump is None:
            print("[hang] continuing without a dump -- analysis is now "
                  "one-shot, the process must stay open")
    if args.dump_only:
        return 0

    # 2. Is it spinning or stalled?  (process API, never the debugger)
    print(f"\n[hang] sampling threads for {args.sample_seconds}s")
    responding, rows = sample_threads(pid, args.sample_seconds)
    print(f"[hang] Responding = {responding}")
    for r in rows[:10]:
        print("   tid/ms/state/wait: " + r)
    print("\n[hang] " + classify(responding, rows))

    # 3. Stack + real exception context.
    triage(cdb, pid, None)

    if dump:
        print(f"\n[hang] dump kept at {dump}")
        print("[hang] re-analyse any time with:")
        print(f"   python tools/hang_capture.py --analyse-dump \"{dump}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
