#!/usr/bin/env python3
"""Follow Skyrim's Papyrus logs live, from an agent-friendly CLI.

Why this exists
---------------
Papyrus logs are the only runtime record of what a converted script actually
did, but reading them usefully means: knowing where they live, tolerating the
game holding them open, filtering the flood of unrelated mod chatter, and --
crucially for automated debugging -- being able to say "watch from HERE" and
"tell me what happened SINCE then" across separate commands.

`tail -f` cannot do the last part, and blocking forever is useless to a caller
that has a 120s ceiling. So this tool is built around a **cursor**: record a
byte offset now, do something in-game, then read only what was appended.

Log locations (all under Documents\\My Games\\Skyrim Special Edition\\Logs\\Script):
  Papyrus.N.log        the main VM log (0 = current session)
  User\\<Name>.N.log    per-script user logs (Debug.OpenUserLog / TraceUser)

Usage:
    # what logs exist, how big, how recently touched
    python tools/script/papyrus_tail.py list

    # mark the current end of the log; prints a cursor you pass back later
    python tools/script/papyrus_tail.py mark

    # everything appended since a cursor (the normal agent loop)
    python tools/script/papyrus_tail.py since --cursor 502545

    # last N lines right now
    python tools/script/papyrus_tail.py tail --lines 40

    # follow for a bounded time, printing as lines arrive
    python tools/script/papyrus_tail.py follow --seconds 20

    # only lines matching a pattern (case-insensitive regex)
    python tools/script/papyrus_tail.py tail --grep "charactergen|TES4"

    # a user log instead of the main VM log
    python tools/script/papyrus_tail.py tail --log User/TES4CharGen.0.log

    # errors only -- the usual first question
    python tools/script/papyrus_tail.py since --cursor 0 --errors

Every subcommand accepts --json for machine-readable output, and always reports
the new cursor so the next call can continue exactly where this one stopped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

DEFAULT_LOG = "Papyrus.0.log"

# Lines the VM emits for genuine problems. Anchored to the message body so a
# script merely *named* "...error..." does not match.
ERROR_RE = re.compile(
    r"\]\s*(?:error|warning)\s*:"          # "[time] error: ..."
    r"|cannot (?:be bound|open store|find)"
    r"|Unable to (?:bind|call|find)"
    r"|has no (?:property|function)"
    r"|stack (?:overflow|count)"
    r"|Assigning None"
    r"|attempted to (?:call|access)",
    re.IGNORECASE,
)


def documents_dir() -> Path:
    """The real 'Documents' folder, honouring OneDrive Known Folder Move.

    OneDrive relocates Documents to %USERPROFILE%\\OneDrive\\Documents and records
    the new path in the registry, so %USERPROFILE%\\Documents no longer exists.
    The authoritative location is the 'Personal' shell-folder value; fall back to
    the classic path only when that cannot be read (non-Windows, locked registry).
    """
    try:
        import winreg  # Windows-only; import lazily so the module loads elsewhere

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            raw, _ = winreg.QueryValueEx(key, "Personal")
        expanded = os.path.expandvars(raw)
        if expanded and Path(expanded).is_dir():
            return Path(expanded)
    except OSError:
        pass
    return Path(os.environ.get("USERPROFILE", Path.home())) / "Documents"


def log_dir() -> Path:
    return (
        documents_dir()
        / "My Games"
        / "Skyrim Special Edition"
        / "Logs"
        / "Script"
    )


def resolve_log(name: str) -> Path:
    p = log_dir() / name
    if not p.exists():
        raise SystemExit(
            f"no such log: {p}\n"
            f"(run `python tools/script/papyrus_tail.py list` to see what exists; "
            f"if the list is empty, Papyrus logging is off -- set "
            f"bEnableLogging=1 / bEnableTrace=1 under [Papyrus] in Skyrim.ini)"
        )
    return p


def read_from(path: Path, start: int) -> tuple[str, int]:
    """Read from byte offset `start` to EOF. Tolerates the game holding the file.

    Returns (text, new_offset). A file that SHRANK since the cursor was taken
    means the game restarted and rotated the log, so we restart from 0 rather
    than returning a slice of unrelated text.
    """
    size = path.stat().st_size
    if start > size:
        start = 0  # log rotated / new session
    # Open in binary and decode leniently: the game writes as the reader reads,
    # so the tail can land mid-multibyte-character.
    with open(path, "rb") as fh:
        fh.seek(start)
        data = fh.read()
    return data.decode("utf-8", "replace"), start + len(data)


def apply_filters(text: str, grep: str | None, errors: bool) -> list[str]:
    lines = text.splitlines()
    if errors:
        lines = [ln for ln in lines if ERROR_RE.search(ln)]
    if grep:
        rx = re.compile(grep, re.IGNORECASE)
        lines = [ln for ln in lines if rx.search(ln)]
    return lines


def cmd_list(args) -> int:
    d = log_dir()
    if not d.exists():
        print(f"no log directory: {d}")
        return 1
    rows = []
    for p in sorted(d.rglob("*.log")):
        st = p.stat()
        rows.append(
            {
                "log": str(p.relative_to(d)).replace("\\", "/"),
                "bytes": st.st_size,
                "modified": time.strftime("%H:%M:%S", time.localtime(st.st_mtime)),
                "age_s": round(time.time() - st.st_mtime, 1),
            }
        )
    rows.sort(key=lambda r: r["age_s"])
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"{'log':44} {'bytes':>9}  {'modified':>8}  age")
        for r in rows:
            print(f"{r['log']:44} {r['bytes']:>9}  {r['modified']:>8}  {r['age_s']}s")
    return 0


def cmd_mark(args) -> int:
    p = resolve_log(args.log)
    cursor = p.stat().st_size
    if args.json:
        print(json.dumps({"log": args.log, "cursor": cursor}))
    else:
        print(f"cursor: {cursor}   ({args.log})")
        print("do the thing in-game, then:")
        print(f"  python tools/script/papyrus_tail.py since --cursor {cursor}"
              + (f" --log {args.log}" if args.log != DEFAULT_LOG else ""))
    return 0


def cmd_since(args) -> int:
    p = resolve_log(args.log)
    text, new_cursor = read_from(p, args.cursor)
    lines = apply_filters(text, args.grep, args.errors)
    if args.json:
        print(json.dumps({"log": args.log, "cursor": new_cursor,
                          "lines": lines, "count": len(lines)}, indent=2))
    else:
        for ln in lines:
            print(ln)
        print(f"\n-- {len(lines)} line(s); cursor {args.cursor} -> {new_cursor}",
              file=sys.stderr)
    return 0


def cmd_tail(args) -> int:
    p = resolve_log(args.log)
    text, new_cursor = read_from(p, 0)
    lines = apply_filters(text, args.grep, args.errors)
    lines = lines[-args.lines:] if args.lines > 0 else lines
    if args.json:
        print(json.dumps({"log": args.log, "cursor": new_cursor,
                          "lines": lines, "count": len(lines)}, indent=2))
    else:
        for ln in lines:
            print(ln)
        print(f"\n-- cursor {new_cursor}", file=sys.stderr)
    return 0


def cmd_follow(args) -> int:
    """Print appended lines for a BOUNDED time, then stop and report the cursor.

    Bounded on purpose: an agent-driven session has a hard command timeout, and
    a follow that never returns is worse than useless -- it burns the whole
    budget and yields nothing.
    """
    p = resolve_log(args.log)
    cursor = args.cursor if args.cursor is not None else p.stat().st_size
    deadline = time.time() + args.seconds
    total = 0
    try:
        while time.time() < deadline:
            text, cursor = read_from(p, cursor)
            if text:
                for ln in apply_filters(text, args.grep, args.errors):
                    print(ln, flush=True)
                    total += 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    print(f"\n-- followed {args.seconds}s, {total} matching line(s); cursor {cursor}",
          file=sys.stderr)
    if args.json:
        print(json.dumps({"log": args.log, "cursor": cursor, "count": total}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Follow Skyrim Papyrus logs live.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:", 1)[-1],
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--log", default=DEFAULT_LOG,
                    help=f"log file under Logs/Script (default {DEFAULT_LOG}); "
                         "e.g. User/TES4CharGen.0.log")
    ap.add_argument("--grep", help="only lines matching this regex (case-insensitive)")
    ap.add_argument("--errors", action="store_true",
                    help="only error/warning-shaped lines")

    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="what logs exist, size, how recently written")
    sub.add_parser("mark", help="print the current end-of-log as a cursor")

    s = sub.add_parser("since", help="everything appended since a cursor")
    s.add_argument("--cursor", type=int, required=True)

    t = sub.add_parser("tail", help="the last N lines right now")
    t.add_argument("--lines", type=int, default=40)

    f = sub.add_parser("follow", help="print new lines for a bounded time")
    f.add_argument("--seconds", type=float, default=15.0)
    f.add_argument("--interval", type=float, default=0.4)
    f.add_argument("--cursor", type=int, default=None)

    args = ap.parse_args(argv)
    return {
        "list": cmd_list, "mark": cmd_mark, "since": cmd_since,
        "tail": cmd_tail, "follow": cmd_follow,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
