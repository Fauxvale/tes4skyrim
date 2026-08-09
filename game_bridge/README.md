# Game Bridge

A live control channel into a **running** Skyrim SE, so conversion output can be
verified in-engine without a relaunch cycle.

    python (tools/game_bridge.py)  <--named pipe-->  TESGameBridge.dll (SKSE plugin)

## Why

Most creature/ragdoll defects in this project are **silent binding failures**:
the `.hkx` is grammatically valid, the offline validator passes it, the actor
spawns and looks fine — and the engine bound nothing. Offline tools cannot see
this, because the verdict only exists inside the engine.

So the bridge is a **readback** tool first, a command injector second. Anything
it can make the game do, it should also be able to report what the game decided.

## Build

Needs only MSVC + the Windows SDK. No SKSE source tree, no CMake, no vcpkg.

```bat
game_bridge\build.bat            :: build to game_bridge\TESGameBridge.dll
game_bridge\build.bat deploy     :: build, then copy into Data\SKSE\Plugins
```

Then launch the game through `skse64_loader.exe` as usual.

## Use

```bash
python tools/game_bridge.py ping           # is the bridge alive?
python tools/game_bridge.py capabilities   # what resolved on this runtime?
python tools/game_bridge.py status         # game / session state
python tools/game_bridge.py console "coc WhiterunDragonsreach"
python tools/game_bridge.py console "getpos z" --ref 0x0001A2B3
python tools/game_bridge.py --json status  # machine-readable
```

As a library:

```python
from tools.game_bridge import Bridge

with Bridge() as b:
    b.console("coc BridgeTestCell")
    print(b.status())
```

## How it stays correct across game updates

**No RVA is hardcoded.** Every entry point resolves at runtime, in this order:

1. **Address Library stable ID** (`versionlib-<ver>.bin`, already deployed in
   this install).
2. **Signature scan** of `.text`, so a game update that lands before a new
   versionlib does not take the bridge offline.
3. Neither → the capability reports `E_UNSUPPORTED` and is **never called
   through**.

The IDs were derived by locating each target in the GOG/AE 1.6.659 build (the
only non-DRM-packed copy, so the only one that disassembles statically) and
inverting through `versionlib-1-6-659-0.bin`. The same ID then resolves on the
DRM-packed Steam 1.6.1170 the user actually plays. See `plugin/ids.h`.

DRM only blocks *static disassembly of the file on disk*. An SKSE plugin loads
after the exe has unpacked itself in memory, so Steam is fully supported — and
is the right target, since it is the build being played.

## Design decisions worth keeping

**Console commands go through the engine's own executor** (`ids::kConsoleExecute`),
which builds the compiler, script buffer and script internally. The alternative —
constructing a `ScriptBuffer` and calling `CompileAndRun` — means matching a
struct layout exactly, and a mismatch does not fail loudly: it corrupts memory or
reads garbage, producing symptoms indistinguishable from a conversion bug. The
only struct offset assumed anywhere is the script text pointer at `+0x38`, read
straight out of `Script::SetText`'s own store instruction.

**Reference selection uses `prid`**, not a resolved `TESObjectREFR*`. Same end
state, zero layout assumptions.

**Every handler that touches game state is marshalled to the main thread** via
`SKSETaskInterface`, and the pipe thread blocks on completion. Off-thread access
to an `Actor*` races the game loop and yields intermittent crashes that look
exactly like data bugs — the most expensive possible failure for this tool,
because it sends both sides hunting ghosts in the data. A marshalling timeout is
reported as `E_LOADING` rather than hanging the pipe.

**A named pipe, not TCP** — local-only by construction, no listening port.
One client at a time; a second connection is refused rather than queued, so two
clients cannot interleave mutations.

## Testing

```bash
python game_bridge/test_protocol.py    # framing, pairing, error codes; no game needed
```

The versionlib parser is verified against `tools/address_lib.py` (the Python
reference): 428,461 entries and all 7 probe addresses match exactly on
1.6.1170. That check matters because the parser's failure mode is silent — a
desynced stream still yields plausible numbers, which would then be called as
function pointers.

## Status

Working now:

| | |
|---|---|
| `ping` / `capabilities` / `status` | session + capability introspection |
| `console` | any console command, optional selected reference |

The engine-side handlers below are specified in `docs/protocol.md` and not yet
implemented — they need struct work that should be validated against a live game
first, one at a time:

| | |
|---|---|
| `ragdoll_probe` | rigid bodies, constraints, `AddRagdollToWorld` state, bone transforms |
| `anim_probe` | behavior graph bind status, resolved clip index |
| `spawn` / `cleanup` | tracked test actors |
| `reload_nif` / `reload_hkx_anim` | hot reload without relaunch |

Note that `coc` already works today through `console`, including from the main
menu — which covers autonomous navigation to a test cell.

## Safety

A corrupted live session produces symptoms indistinguishable from conversion
bugs, so the guards are part of the design, not decoration:

- **Spawn tracking** — every spawn recorded, removable via `cleanup`, and
  released on client disconnect.
- **Save guard** — mutating commands refuse to run unless the session is a
  designated test save (`BRIDGE_` prefix) or was started by the bridge itself.
  `"force": true` overrides, and is logged.
- **Spawn cap** — default 256, so a runaway loop cannot fill the cell.

## Files

| | |
|---|---|
| `plugin/plugin.cpp` | SKSE entry point, message handling, startup |
| `plugin/commands.cpp` | JSON command dispatch |
| `plugin/console_exec.cpp` | console execution (the riskiest file, isolated) |
| `plugin/script_object.cpp` | Script allocation via the engine's own heap |
| `plugin/addresses.cpp` | versionlib parser + signature scanner |
| `plugin/ids.h` | **verified stable IDs — read the warnings before adding one** |
| `plugin/main_thread.cpp` | main-thread marshalling |
| `plugin/pipe_server.cpp` | named-pipe server |
| `plugin/json.cpp` | dependency-free JSON |
| `docs/protocol.md` | full command surface and error codes |
| `tools/game_bridge.py` | Python client + CLI |
