# Rideable Horse Conversion: Oblivion CREA → Skyrim Mountable Actor

**Status: PLAN, unimplemented.** Nothing reads mount data; `grep -rn "mount_data"`
returns zero hits. What a mountable actor IS lives in
[reference/skyrim_mountable_actor.md](../reference/skyrim_mountable_actor.md).

Feasibility + implementation plan for making converted Oblivion horses behave like
vanilla Skyrim horses (player can approach and mount them). This sits ON TOP of the
existing creature pipeline (see [docs/commentary/asset_convert_creature.md](../commentary/asset_convert_creature.md)) —
horses already convert through that path as ordinary quadrupeds (locomotion, combat,
death). This doc covers only the DELTA needed for the ridden state.

---

## 3. Implementation plan

### Step 1 — Manifest: identify mount-capable creature folders
Extend `tools/creature_inventory.py` (or the `creature_projects.json` contract) with a
`is_mount: bool` flag, set for the `horse` folder (matched by clip inventory shape —
check for paired/rider-adjacent clip absence rather than hardcoding the string "horse",
since Nehrim/other plugins may ship differently-named horse-equivalents or additional
mountable creatures — e.g. detect via a config allowlist keyed on skeleton bone census
matching the vanilla horse, falling back to folder-name heuristic like the existing
`_FOLDER_KEYWORDS` animal-tagging in `creature_races.py:107`).

### Step 2 — RACE Mount Data (creature_races.py)
Add `mount_data` struct emission gated on `is_mount`, using the vanilla HorseRace
constants (§2.1) as the only values we ship — Oblivion has no equivalent field to pull
per-plugin values from, so this is intentionally a constant, not derived data (same
category of exception as horse animations, §2.3).

### Step 3 — Rider-side vanilla asset passthrough
Add a `mount_assets` stage to `asset_convert/creature_pipeline.py` (horse-only): pull
vanilla `actors/character/behaviors/horsebehavior.hkx` +
`animations/horse_rider/*.hkx` + `actors/horse/behaviors/horsebehavior.hkx` mount/
dismount/ride-loop clips via `asset_convert/skyrim_assets.py` (cache in
`export/skyrim_assets/`, same contract as every other vanilla-asset pull in this
project — never touch `references/` at runtime). No conversion needed — these are
already SSE-loadable Havok/NIF.

### Step 4 — Horse behavior graph: extend the quadruped template
In `asset_convert/hkx_behavior.py`, add a `mount` branch to `build_behavior_xml` (only
emitted when `is_mount`): a `Ridden` state entered/exited by whatever event names
vanilla actually uses (dump `horsebehavior.hkx` via `hkxcmd convert -v:XML` FIRST —
don't guess event names, same rule as every other behavior-graph fact in this project)
wrapping the horse's own locomotion states but driven by the rider's input variables
instead of AI `Speed`/`Direction`. Reuse every existing mechanism
(`BSSpeedSamplerModifier`, `iState_*`/MOVT contract, nested SM topology rules from the
2026-07-16 head-whipping fix) — the Ridden state is additive, not a replacement
graph.

### Step 5 — Verification
- Build an A/B ESP repointing the converted horse RACE at vanilla horse
  behavior/animation assets, to isolate record-side correctness from graph correctness
  (the per-layer bisection method used throughout creature_conversion.md;
  `tools/creature_vanilla_ab.py` did this for canines and was removed 2026-08-25).
- In-game: spawn a converted horse, `player.placeatme`, attempt mount via activate
  prompt; verify camera offset, dismount, and that ordinary (non-ridden) locomotion/
  combat/death — already working via the base creature pipeline — is unaffected.
- Explicitly OUT of scope for v1 (document as known limitation, matching Oblivion's own
  behavior): jump-over-obstacles, mounted combat, mounted fast-travel prompts tied to
  Skyrim's stable/city-horse ownership system (Oblivion has no per-city owned-horse
  concept — every converted horse is a plain CREA, so "buy a horse at the stable"
  UX would need its own separate design pass, not covered here).

---

## 4. Open questions (resolve during implementation)

1. Exact vanilla event names for mount/dismount/ridden-state transitions — must dump
   `horsebehavior.hkx` XML rather than assume names by analogy to quadruped events.
2. Whether the rider-side graph can be grafted onto the player's EXISTING behavior
   graph as a sub-state (likely — vanilla does exactly this, since the player has only
   one graph total) or needs separate handling in whatever governs the player actor's
   graph in this pipeline (the project currently has no "player behavior" conversion
   surface at all — this may be the first).
3. Whether non-horse Oblivion creatures should ever be mount-flagged (e.g. Nehrim or
   other plugins may add rideable variants) — the manifest-driven `is_mount` flag in
   Step 1 should stay data-driven rather than hardcoded to unblock this later without
   code changes.
