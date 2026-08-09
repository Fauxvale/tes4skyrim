# Game Bridge Protocol

A live, bidirectional control channel into a **running** SkyrimSE.exe, so the
conversion pipeline can be verified in-engine without a relaunch cycle.

    Python (tools/game_bridge.py)  <--named pipe-->  TESGameBridge.dll (SKSE plugin)

## Why this exists

Most creature/ragdoll defects in this project are **silent binding failures**:
the file is grammatically valid, the offline validator passes it, the actor
spawns, and the engine binds *nothing*. Offline tools cannot see this because
the verdict only exists inside the engine. The bridge asks the engine directly.

Design consequence: the bridge is a **readback** tool first and a command
injector second. Anything it can make the game *do*, it must also be able to
report what the game *decided*.

## Transport

- **Named pipe**: `\\.\pipe\tes_game_bridge` (local-only by construction; no
  listening TCP port is ever opened).
- **Framing**: one JSON object per line, UTF-8, `\n` terminated. Request and
  response are 1:1 and strictly ordered on a single connection.
- **Concurrency**: the pipe accepts one client at a time. A second connect
  attempt is rejected rather than queued, so two agents cannot interleave state
  mutations.

### Request

```json
{"id": 17, "cmd": "console", "args": {"command": "player.placeatme 0x00023ABC 1"}}
```

### Response

```json
{"id": 17, "ok": true, "result": {...}, "log": ["..."], "elapsed_ms": 3}
```

On failure:

```json
{"id": 17, "ok": false, "error": "no game loaded", "code": "E_NO_GAME"}
```

`id` is echoed verbatim. Every response carries `ok`. Errors are **always**
structured — the client never has to parse prose.

## Threading contract (non-negotiable)

The pipe server runs on its own thread. **No command handler touches game state
on that thread.** Every handler that reads or mutates a form, reference, cell,
or Havok object is marshalled onto the game's main thread via
`SKSETaskInterface::AddTask`, and the pipe thread blocks on a completion event
until the task has run.

Violating this produces intermittent crashes and torn reads that look exactly
like conversion bugs — the single most expensive possible failure mode for this
project, because it sends both sides chasing ghosts in the data.

Commands that touch **only** the filesystem or plugin-local state (`ping`,
`status`) may answer on the pipe thread and are marked as such below.

## Command surface

### Session / introspection

| cmd | args | returns | main thread |
|---|---|---|---|
| `ping` | – | `{pong, plugin_version, runtime_version}` | no |
| `status` | – | `{in_main_menu, game_loaded, cell, player_pos, save_name}` | yes |
| `capabilities` | – | list of supported cmds + whether each is available in the current game state | no |

### Navigation / lifecycle

| cmd | args | returns |
|---|---|---|
| `coc` | `{cell}` | `{cell, player_pos}` — works **from the main menu**; boots a new game into the target cell |
| `load_save` | `{name}` | `{cell}` |
| `wait_ready` | `{timeout_ms}` | `{ready}` — blocks until the game is out of a load screen |

### Console

| cmd | args | returns |
|---|---|---|
| `console` | `{command, ref?}` | `{output}` — runs a console command, optionally with a selected reference (the `ref` is the console's implicit `this`) |

Console output is captured by hooking the console print path, so `getpos`,
`getav`, etc. return their text rather than only appearing on screen.

### Papyrus

| cmd | args | returns |
|---|---|---|
| `papyrus` | `{script, function, args, self?}` | `{value}` — invokes a Papyrus function and returns its typed result |

### Forms / records

| cmd | args | returns |
|---|---|---|
| `lookup` | `{editor_id?, form_id?}` | `{form_id, editor_id, type, ...}` |
| `get_record` | `{form_id, fields?}` | decoded field values as the **engine** currently holds them (post-load-order, not what the ESM on disk says) |
| `set_record` | `{form_id, field, value}` | `{old, new}` — live edit of a loaded form (see *Hot reload* for what sticks) |

### Actors / spawning

| cmd | args | returns |
|---|---|---|
| `spawn` | `{form_id, count?, at?}` | `{refs: [...]}` — tracked; every spawn is recorded for `cleanup` |
| `cleanup` | `{scope?}` | `{removed}` — deletes everything this session spawned |
| `actor_state` | `{ref}` | position, 3D-loaded flag, animation graph state, current state machine node |

### The readback layer (the reason this exists)

| cmd | args | returns |
|---|---|---|
| `ragdoll_probe` | `{ref}` | see below |
| `anim_probe` | `{ref}` | behavior graph: project name, bound clips, active state machine node, last event, whether the graph bound at all |
| `nif_probe` | `{ref}` | loaded 3D node tree, skin bone count/names, per-bone world transforms |

`ragdoll_probe` returns, for a live reference:

```json
{
  "has_3d": true,
  "graph_bound": true,
  "ragdoll_instance": true,
  "rigid_bodies": 21,
  "constraints": 20,
  "in_world": false,
  "bones": [{"name": "NPC Spine", "world": [x,y,z], "rot": [...]}, ...]
}
```

`ragdoll_instance:false` or `rigid_bodies:0` on a creature that looks fine on
screen is the exact silent-binding signature that offline validation misses.
`in_world` distinguishes "ragdoll constructed" from "ragdoll actually raised
into the physics world" — per project notes only `AnimateToRagdoll`'s
enterNotify raises it, so a constructed-but-not-raised ragdoll is a real and
distinct failure mode.

## Hot reload

The goal is to avoid relaunching. What the engine actually permits differs by
asset class, and the bridge is honest about which is which:

| cmd | what it does | relaunch still needed? |
|---|---|---|
| `reload_nif` | drops the cached model and re-reads the `.nif` from disk, then refreshes 3D on matching refs | **no** |
| `reload_hkx_anim` | reloads animation binaries for a given project | **no** for clip data |
| `reload_behavior` | rebuilds the behavior graph for a project | **partial** — new graph binds to newly spawned actors; existing actors must be respawned |
| `reload_texture` | re-reads a texture | **no** |
| `set_record` | live-edits a loaded form in memory | **no**, but not persisted to the ESM |
| plugin/ESM structural change (new records, changed masters) | – | **yes** — the load order is read once at startup |

The honest boundary: **asset files can be re-read live; the plugin's record
list cannot.** A `--import-only` rebuild that adds or removes records requires a
relaunch. A mesh/hkx rebuild does not — which covers the large majority of the
creature iteration loop, and is where the round-trip savings come from.

`reload_*` commands report `{reloaded: n, refreshed_refs: n}` so a no-op reload
is visibly a no-op rather than a false success.

## Safety

The bridge can silently corrupt a live session, and a corrupted session
produces symptoms indistinguishable from conversion bugs. Guards:

- **Spawn tracking** — every `spawn` is recorded; `cleanup` removes them. The
  plugin also cleans up on client disconnect.
- **Save guard** — `spawn`, `set_record`, and `reload_*` refuse to run unless
  the loaded save is a designated test save (name prefix `BRIDGE_`) or the
  session was started by the bridge itself via `coc`. Override per-command with
  `"force": true`, which is logged.
- **No autosave** — the bridge disables autosave on connect so a test session
  cannot overwrite a real save.
- **Spawn cap** — a per-session ceiling (default 256) prevents a runaway loop
  from filling the cell and hanging the game.

## Error codes

| code | meaning |
|---|---|
| `E_NO_GAME` | command needs a loaded game; currently at the main menu |
| `E_LOADING` | a load screen is in progress; retry after `wait_ready` |
| `E_NOT_FOUND` | form / editor ID / reference does not resolve |
| `E_NO_3D` | reference has no loaded 3D (not in an attached cell) |
| `E_GUARDED` | blocked by the save guard; pass `force` to override |
| `E_UNSUPPORTED` | command not available on this runtime version |
| `E_INTERNAL` | handler threw; details in `error` |
