#!/usr/bin/env python3
"""End-to-end protocol test for the game bridge, with no game running.

Stands up a named-pipe server that speaks the same protocol as the plugin, then
drives it with the REAL client (tools/game_bridge.py). This exercises framing,
request/response pairing, error propagation and the CLI without needing Skyrim.

What it cannot cover: the engine-side handlers. Those need a running game.

    python game_bridge/test_protocol.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import win32file  # noqa: E402  (pywin32)
import win32pipe  # noqa: E402

from tools.game_bridge import Bridge, BridgeError  # noqa: E402

PIPE = r"\\.\pipe\tes_game_bridge_test"

_failures = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _failures
    if cond:
        print(f"  PASS  {label}")
    else:
        _failures += 1
        print(f"  FAIL  {label}  {detail}")


def handle(req: dict) -> dict:
    """Mirrors the plugin's dispatch, including its error shape."""
    cmd = req.get("cmd")
    rid = req.get("id")

    if cmd == "ping":
        return {"id": rid, "ok": True,
                "result": {"pong": True, "plugin_version": "0.1.0",
                           "runtime_version": 0x01064920, "game_loaded": False}}
    if cmd == "console":
        command = (req.get("args") or {}).get("command", "")
        if not command:
            return {"id": rid, "ok": False, "code": "E_INTERNAL",
                    "error": "missing 'command'"}
        return {"id": rid, "ok": True, "result": {"output": f"ran: {command}\n"}}
    if cmd == "status":
        return {"id": rid, "ok": False, "code": "E_NO_GAME",
                "error": "no game loaded"}
    # E_UNKNOWN_CMD, not E_UNSUPPORTED: this build simply lacks the command, so
    # a newer client can fall back to a console-based path. E_UNSUPPORTED means
    # the runtime could not resolve the capability at all, which is not
    # recoverable and must not be retried.
    return {"id": rid, "ok": False, "code": "E_UNKNOWN_CMD",
            "error": f"unknown command: {cmd}"}


def server(ready: threading.Event, stop: threading.Event) -> None:
    pipe = win32pipe.CreateNamedPipe(
        PIPE,
        win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
        1, 1 << 20, 1 << 20, 0, None)
    ready.set()
    try:
        win32pipe.ConnectNamedPipe(pipe, None)
        buf = b""
        while not stop.is_set():
            try:
                _, chunk = win32file.ReadFile(pipe, 4096)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    resp = {"ok": False, "code": "E_INTERNAL",
                            "error": "malformed request"}
                else:
                    resp = handle(req)
                win32file.WriteFile(pipe, (json.dumps(resp) + "\n").encode())
    finally:
        try:
            win32file.CloseHandle(pipe)
        except Exception:
            pass


def main() -> int:
    print("game bridge protocol test (no game required)")

    ready, stop = threading.Event(), threading.Event()
    t = threading.Thread(target=server, args=(ready, stop), daemon=True)
    t.start()
    if not ready.wait(5):
        print("  FAIL  server did not start")
        return 1
    time.sleep(0.1)

    try:
        with Bridge(PIPE).connect(retries=5) as b:
            r = b.ping()
            check("ping returns pong", r.get("pong") is True, str(r))
            check("ping carries plugin_version",
                  r.get("plugin_version") == "0.1.0", str(r))

            out = b.console("coc BridgeTestCell")
            check("console echoes the command",
                  out.strip() == "ran: coc BridgeTestCell", repr(out))

            # Structured errors must surface as BridgeError with the code intact.
            try:
                b.status()
                check("error propagates", False, "expected BridgeError")
            except BridgeError as exc:
                check("error code preserved", exc.code == "E_NO_GAME", exc.code)

            try:
                b.request("nope")
                check("unknown command errors", False, "expected BridgeError")
            except BridgeError as exc:
                check("unknown command -> E_UNKNOWN_CMD",
                      exc.code == "E_UNKNOWN_CMD", exc.code)

            # Ordering: ids must come back paired with their requests.
            ids_ok = True
            for _ in range(20):
                before = b._next_id
                b.console("test")
                ids_ok &= (b._next_id == before + 1)
            check("20 sequential requests stay paired", ids_ok)
    except BridgeError as exc:
        print(f"  FAIL  client could not connect: {exc}")
        return 1
    finally:
        stop.set()

    print(f"\n{'FAILED' if _failures else 'OK'} ({_failures} failure(s))")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
