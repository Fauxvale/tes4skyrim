#!/usr/bin/env python3
"""Send synthetic keyboard input to the running Skyrim window.

WHY THIS EXISTS
---------------
The bridge's console executor needs an "execution context" -- a live object the
console builds in its own stack frame when the game itself dispatches a typed
command. It cannot be synthesized, so the plugin captures it by watching the
game's own CompileAndRun calls (see game_bridge/plugin/console_capture.cpp).

That creates a bootstrap problem: until the game runs ONE console command by
itself, the bridge can compile commands but never run them -- which silently
looks like "the command did nothing". Previously the only fix was for a human to
open the console and press Enter once.

This tool removes that last manual step, so a session can go from a cold launch
to a fully working bridge with no user input at all. It drives the console the
way a player does -- real WM_* key messages to the game's own window -- so the
engine builds the context exactly as it normally would. Nothing is patched and
no engine state is written.

    python tools/game_input.py bootstrap        # open console, run a no-op, close
    python tools/game_input.py type "coc riverwood" --enter
    python tools/game_input.py key ENTER

The console key is the one bound in Skyrim's controlmap; `~` (grave) is the
default and is what `bootstrap` uses.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102

VK_RETURN = 0x0D
VK_OEM_3 = 0xC0  # `~` / grave -- the default console key
VK_ESCAPE = 0x1B

NAMED_KEYS = {
    "ENTER": VK_RETURN,
    "RETURN": VK_RETURN,
    "TILDE": VK_OEM_3,
    "GRAVE": VK_OEM_3,
    "CONSOLE": VK_OEM_3,
    "ESC": VK_ESCAPE,
    "ESCAPE": VK_ESCAPE,
}


def find_window(title_contains: str = "Skyrim") -> int:
    """Find the game's top-level window handle by title substring."""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if title_contains.lower() in buf.value.lower() and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    return found[0] if found else 0


# 🛑 PostMessage DOES NOT WORK ON SKYRIM.
#
# Skyrim reads the keyboard through DirectInput, which polls the device state
# directly and never looks at the window's message queue. PostMessage'd
# WM_KEYDOWN/WM_CHAR are accepted by the window and ignored by the game --
# silently, which is the worst kind of failure (verified 2026-08-14: the
# bootstrap "succeeded" and the console never opened).
#
# SendInput injects at the driver level, below DirectInput, so the game sees a
# real key. The cost is that it goes to whatever window has focus -- so we must
# focus the game first, and scan codes matter more than virtual keys because
# DirectInput is scan-code based.

# 🛑 dwExtraInfo must be ULONG_PTR, not POINTER(c_ulong).
#
# With a POINTER field ctypes lays the struct out so that sizeof(INPUT) is
# wrong for the platform, and SendInput rejects every call with error 87
# (ERROR_INVALID_PARAMETER) while returning 0 -- silently sending nothing,
# which reads exactly like "the game ignored my keypress" (2026-08-14).
# Always check SendInput's return value; a 0 means nothing was sent.
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _INPUTunion(ctypes.Union):
    # MOUSEINPUT is the largest member (32 bytes on x64), and the union's size
    # is what makes sizeof(INPUT) come out at the 40 bytes SendInput demands.
    # Sizing this to KEYBDINPUT alone yields a 32-byte INPUT and every call
    # fails with error 87.
    _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


assert ctypes.sizeof(INPUT) == 40, (
    f"INPUT is {ctypes.sizeof(INPUT)} bytes, SendInput requires 40 on x64")


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_UNICODE = 0x0004


def _scan(vk: int) -> int:
    return user32.MapVirtualKeyW(vk, 0)


def focus(hwnd: int) -> None:
    """Bring the game to the foreground so SendInput reaches it.

    🛑 Steals focus from whoever is using the desktop. Never call this
    unprompted while someone may be working -- check `is_foreground` first and
    say what you need instead.
    """
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.25)


def is_foreground(hwnd: int) -> bool:
    """Is the game the active window? SendInput only reaches that one."""
    return user32.GetForegroundWindow() == hwnd


def _send(inputs: list[INPUT]) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        # Never fail silently: a 0 here means the game got NOTHING, which is
        # indistinguishable from "the game ignored it" unless we say so.
        raise OSError(f"SendInput sent {sent}/{len(inputs)} "
                      f"(error {ctypes.get_last_error()})")


def send_key(hwnd: int, vk: int, hold_ms: int = 45) -> None:
    """One real key press+release, by SCAN CODE (what DirectInput reads)."""
    sc = _scan(vk)
    down = INPUT(type=INPUT_KEYBOARD,
                 u=_INPUTunion(ki=KEYBDINPUT(0, sc, KEYEVENTF_SCANCODE, 0, 0)))
    up = INPUT(type=INPUT_KEYBOARD,
               u=_INPUTunion(ki=KEYBDINPUT(0, sc,
                                           KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP,
                                           0, 0)))
    _send([down])
    time.sleep(hold_ms / 1000.0)
    _send([up])
    time.sleep(hold_ms / 1000.0)


VK_SHIFT = 0x10


def send_text(hwnd: int, text: str, per_char_ms: int = 25) -> None:
    """Type a string into the console's text field, by SCAN CODE.

    🛑 KEYEVENTF_UNICODE DOES NOT WORK HERE. It synthesizes a WM_CHAR-style
    event, and Skyrim's console reads the keyboard through DirectInput, which
    only ever sees scan codes. The keys "succeed" (SendInput returns 1) and
    nothing appears in the console -- verified in-game 2026-08-14: the console
    opened and closed correctly via scan codes while every UNICODE-typed
    character was dropped.

    So each character is mapped back to a virtual key + shift state for the
    CURRENT keyboard layout via VkKeyScanExW, then sent as a scan code.
    """
    layout = user32.GetKeyboardLayout(0)
    for ch in text:
        res = user32.VkKeyScanExW(wintypes.WCHAR(ch), layout)
        if res == -1:
            continue  # not typable on this layout
        vk = res & 0xFF
        shift = bool(res & 0x100)
        sc = _scan(vk)
        if not sc:
            continue

        seq: list[INPUT] = []
        if shift:
            seq.append(INPUT(type=INPUT_KEYBOARD,
                             u=_INPUTunion(ki=KEYBDINPUT(0, _scan(VK_SHIFT),
                                                         KEYEVENTF_SCANCODE, 0, 0))))
        seq.append(INPUT(type=INPUT_KEYBOARD,
                         u=_INPUTunion(ki=KEYBDINPUT(0, sc, KEYEVENTF_SCANCODE, 0, 0))))
        _send(seq)
        time.sleep(per_char_ms / 2000.0)

        seq = [INPUT(type=INPUT_KEYBOARD,
                     u=_INPUTunion(ki=KEYBDINPUT(0, sc,
                                                 KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP,
                                                 0, 0)))]
        if shift:
            seq.append(INPUT(type=INPUT_KEYBOARD,
                             u=_INPUTunion(ki=KEYBDINPUT(0, _scan(VK_SHIFT),
                                                         KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP,
                                                         0, 0))))
        _send(seq)
        time.sleep(per_char_ms / 1000.0)


def bootstrap(hwnd: int, command: str = "getgs fJumpHeightMin",
              settle_s: float = 0.6, take_focus: bool = False) -> None:
    """Make the GAME run one console command, so the bridge learns its context.

    The command is deliberately read-only -- this runs against whatever save is
    loaded, and a bootstrap step must never change game state.

    `take_focus` is OFF by default: SendInput goes to whatever window is
    focused, so forcing focus yanks the desktop away from whoever is using it
    mid-click. Only pass it when nobody is at the keyboard; otherwise make sure
    the game is already foreground (`is_foreground`).
    """
    if take_focus:
        focus(hwnd)                   # SendInput follows focus, not the hwnd
    send_key(hwnd, VK_OEM_3)          # open the console
    time.sleep(settle_s)
    send_text(hwnd, command)
    time.sleep(0.2)
    send_key(hwnd, VK_RETURN)         # dispatch it -- this builds the context
    time.sleep(settle_s)
    send_key(hwnd, VK_OEM_3)          # close the console
    time.sleep(0.2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--window", default="Skyrim",
                    help="window title substring (default: Skyrim)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    bs = sub.add_parser("bootstrap",
                        help="open the console, run one read-only command, close it")
    bs.add_argument("--command", default="getgs fJumpHeightMin",
                    help="the command to run (must be read-only)")
    bs.add_argument("--take-focus", action="store_true",
                    help="force the game foreground first (STEALS focus from "
                         "whoever is using the desktop; off by default)")

    ty = sub.add_parser("type", help="type text into the game window")
    ty.add_argument("text")
    ty.add_argument("--enter", action="store_true", help="press Enter afterwards")

    ky = sub.add_parser("key", help="send one named key")
    ky.add_argument("name", help=f"one of: {', '.join(sorted(NAMED_KEYS))}")

    args = ap.parse_args(argv)

    hwnd = find_window(args.window)
    if not hwnd:
        print(f"no visible window matching {args.window!r} -- is the game running?",
              file=sys.stderr)
        return 2

    if args.cmd == "bootstrap":
        if not args.take_focus and not is_foreground(hwnd):
            print("the game is not the foreground window, so the keystrokes "
                  "would go elsewhere. Click the game first, or pass "
                  "--take-focus to grab it.", file=sys.stderr)
            return 3
        bootstrap(hwnd, args.command, take_focus=args.take_focus)
        print(f"bootstrap sent to hwnd {hwnd:#x} ({args.command!r})")
    elif args.cmd == "type":
        send_text(hwnd, args.text)
        if args.enter:
            send_key(hwnd, VK_RETURN)
        print(f"typed {args.text!r}")
    elif args.cmd == "key":
        vk = NAMED_KEYS.get(args.name.upper())
        if vk is None:
            print(f"unknown key {args.name!r}", file=sys.stderr)
            return 2
        send_key(hwnd, vk)
        print(f"sent {args.name.upper()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
