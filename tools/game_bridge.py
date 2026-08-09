#!/usr/bin/env python3
"""Client for the in-game bridge (game_bridge/ SKSE plugin).

Drives a RUNNING SkyrimSE.exe over a named pipe: run console commands, inspect
what the engine actually built, and reload assets without relaunching. The point
is to collapse the build-and-play round trip for creature/ragdoll work, where
the defect is usually a *silent binding failure* -- valid file, offline
validator passes, actor spawns, engine binds nothing. Only the engine can
report that, so we ask it directly.

Setup (once):
    game_bridge\\build.bat deploy      # build + copy the DLL into Data/SKSE/Plugins
    (start the game through skse64_loader.exe)

Usage:
    # is the bridge alive?
    python tools/game_bridge.py ping

    # what resolved on this runtime?
    python tools/game_bridge.py capabilities

    # run a console command
    python tools/game_bridge.py console "player.placeatme 0x00023ABC 1"

    # with a selected reference
    python tools/game_bridge.py console "getpos z" --ref 0x0001A2B3

    # where are we?
    python tools/game_bridge.py status

    # raw JSON, for scripting
    python tools/game_bridge.py --json status

As a library:
    from tools.game_bridge import Bridge
    with Bridge() as b:
        b.console("coc BridgeTestCell")
        print(b.status())
"""

from __future__ import annotations

import argparse
import json
import sys
import time

PIPE_NAME = r"\\.\pipe\tes_game_bridge"
DEFAULT_TIMEOUT = 10.0


class BridgeError(RuntimeError):
    """A structured error returned by the plugin, or a transport failure."""

    def __init__(self, message: str, code: str = "E_CLIENT"):
        super().__init__(message)
        self.code = code


class Bridge:
    """Connection to the in-game bridge.

    One client at a time: the plugin refuses a second connection rather than
    queueing it, so two sessions cannot interleave state mutations.
    """

    def __init__(self, pipe: str = PIPE_NAME, timeout: float = DEFAULT_TIMEOUT):
        self.pipe_name = pipe
        self.timeout = timeout
        self._f = None
        self._next_id = 1

    # ------------------------------------------------------------- connect --

    def connect(self, retries: int = 1, retry_delay: float = 0.5) -> "Bridge":
        last = None
        for attempt in range(max(1, retries)):
            try:
                # A named pipe behaves like a file; buffering=0 keeps request
                # and response strictly paired.
                self._f = open(self.pipe_name, "r+b", buffering=0)
                return self
            except OSError as exc:  # pipe missing (game not running / no plugin)
                last = exc
                if attempt + 1 < retries:
                    time.sleep(retry_delay)
        raise BridgeError(
            f"could not connect to {self.pipe_name}: {last}. "
            "Is the game running under skse64_loader.exe with TESGameBridge.dll "
            "in Data/SKSE/Plugins?",
            "E_NO_PIPE",
        )

    def close(self) -> None:
        if self._f:
            try:
                self._f.close()
            finally:
                self._f = None

    def __enter__(self) -> "Bridge":
        if not self._f:
            self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ requests --

    def request(self, cmd: str, **args) -> dict:
        """Send one command, return its `result` dict. Raises on error."""
        if not self._f:
            self.connect()

        req = {"id": self._next_id, "cmd": cmd}
        self._next_id += 1
        if args:
            req["args"] = args

        try:
            self._f.write((json.dumps(req) + "\n").encode("utf-8"))
            line = self._readline()
        except OSError as exc:
            self.close()
            raise BridgeError(f"transport failure: {exc}", "E_TRANSPORT") from exc

        try:
            resp = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BridgeError(f"malformed response: {line[:200]!r}", "E_PROTOCOL") from exc

        if not resp.get("ok"):
            raise BridgeError(
                resp.get("error", "unknown error"), resp.get("code", "E_INTERNAL")
            )
        return resp.get("result", {})

    def _readline(self) -> str:
        chunks = []
        while True:
            b = self._f.read(1)
            if not b:
                raise OSError("pipe closed by the game")
            if b == b"\n":
                break
            chunks.append(b)
        return b"".join(chunks).decode("utf-8", "replace")

    # ------------------------------------------------------------ commands --

    def ping(self) -> dict:
        return self.request("ping")

    def capabilities(self) -> dict:
        return self.request("capabilities")

    def status(self) -> dict:
        return self.request("status")

    def console(self, command: str, ref: str | int | None = None) -> str:
        args = {"command": command}
        if ref is not None:
            args["ref"] = ref
        return self.request("console", **args).get("output", "")


# ----------------------------------------------------------------------- cli --


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Drive a running Skyrim SE through the in-game bridge.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:", 1)[-1],
    )
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    ap.add_argument("--pipe", default=PIPE_NAME, help="override the pipe name")
    ap.add_argument("--retries", type=int, default=1,
                    help="connection attempts before giving up")

    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping", help="check the bridge is alive")
    sub.add_parser("capabilities", help="what resolved on this runtime")
    sub.add_parser("status", help="game / session state")

    c = sub.add_parser("console", help="run a console command")
    c.add_argument("command")
    c.add_argument("--ref", help="select this reference first (form id)")

    args = ap.parse_args(argv)

    try:
        with Bridge(args.pipe).connect(retries=args.retries) as b:
            if args.cmd == "ping":
                out = b.ping()
            elif args.cmd == "capabilities":
                out = b.capabilities()
            elif args.cmd == "status":
                out = b.status()
            elif args.cmd == "console":
                out = {"output": b.console(args.command, args.ref)}
            else:  # unreachable; argparse enforces the choices
                ap.error(f"unhandled command {args.cmd}")
    except BridgeError as exc:
        if args.json:
            print(json.dumps({"ok": False, "code": exc.code, "error": str(exc)}))
        else:
            print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(out, indent=2))
    elif args.cmd == "console":
        sys.stdout.write(out["output"])
        if out["output"] and not out["output"].endswith("\n"):
            sys.stdout.write("\n")
    else:
        for k, v in out.items():
            print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
