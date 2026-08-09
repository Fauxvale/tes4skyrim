---
name: skyrim-creature-system
description: Complete reference for how a creature (wolf, draugr, dragon, etc.) works in Skyrim (TES5) — the RACE record hookup, the NIF mesh, the skeleton.hkx (animation skeleton + ragdoll + skeleton mappers), the behavior graph .hkx files (state machines, clip generators, events, variables), and the animationdata / animationsetdata text cache formats. Use when authoring, reading, debugging, or converting Skyrim creature assets, when you need the exact binary layout of any Havok class in a creature file, the exact line-by-line format of the animation cache text files, or the exact chain that turns a game event into a playing animation. This describes vanilla Skyrim only — it contains no conversion or import logic.
---

# How a Creature Works in Skyrim

Every fact here was verified against **real vanilla Skyrim assets** (byte-level), the
**HKX2-Enhanced-Library** auto-generated Havok class definitions, **Haviour**'s
`hkclass.inl` (official Havok enum documentation), the **Skyrim Behavior Editor**
C++ parsers, **xEdit**'s record definitions, and the **Creation Kit wiki**. Where a
fact could not be verified it is marked **[UNVERIFIED]**. Nothing here is inferred
from any conversion tool's source.

The running examples are the **wolf** (`WolfRace`, a quadruped with a
multi-file behavior project) and the **draugr** (`DraugrRace`, a humanoid with a
monolithic behavior graph and weapon-specific animation sets).

---

## 0. The big picture — the whole chain in one place

A creature is **seven** cooperating pieces. Break any one and the creature is
invisible, frozen, silent, or T-posing.

```
                        ┌──────────────────────────────────────┐
   Skyrim.esm           │ RACE record (e.g. WolfRace 0001320A) │
                        └──────────────────────────────────────┘
                          │ ANAM              │ MNAM/FNAM+MODL
                          │ (skeletal model)  │ (behavior graph project)
                          ▼                   ▼
    meshes\actors\canine\           meshes\actors\canine\
      Character Assets Wolf\          WolfProject.hkx        ← project stub
        skeleton.nif                        │
             │                              │ names the project; the ENGINE
             │ NiNode tree                  │ finds the rest via the cache
             │ = bone names                 ▼
             │                       characters wolf\Wolf.hkx   ← "character file"
             ▼                              │  · animationFilenames[]  (INDEXED!)
      skeleton.hkx                          │  · rigName   → skeleton.hkx
      · hkaSkeleton  "animation"  ◄─────────┤  · behaviorFilename → WolfBehavior.hkx
      · hkaSkeleton  "ragdoll"              │
      · hkaSkeletonMapper ×2                ▼
      · hkaRagdollInstance          behaviors wolf\WolfBehavior.hkx  ← root graph
      · hkpRigidBody[] + constraints         │ hkbBehaviorReferenceGenerator
                                             ├─► QuadrupedBehavior.hkx
                                             ├─► ForwardLocomotion.hkx
                                             └─► NonCombatIdle.hkx
                                                   │ hkbClipGenerator
                                                   ▼
                                          animations\*.hkx   (the actual motion)

   meshes\animationdata\wolfproject.txt          ← clip cache: name→index, speed, TRIGGERS
   meshes\animationsetdata\wolfprojectdata\      ← attack/event cache + CRC'd anim paths
   meshes\actors\canine\character assets wolf\   ← body NIF is elsewhere (see §2)
```

**The five links that must agree**, and what breaks when they don't:

| Link | Must match | Symptom when broken |
|---|---|---|
| RACE `ANAM` → `skeleton.nif` | file exists | invisible / no bones |
| RACE `MNAM`+`MODL` → project `.hkx` | project registered in `animationdatasinglefile.txt` | actor T-poses, no behavior |
| character `.hkx` `animationFilenames[i]` | **index `i`** used by `animationdata` | wrong animation plays |
| behavior `hkbClipGenerator.name` | clip block name in `animationdata` | no annotations/sound/footsteps |
| body NIF bone names | `skeleton.nif` / `hkaSkeleton` bone names | mesh doesn't deform / explodes |

**Read these in order** for the detail:
- `references/hkx_container.md` — the binary `.hkx` packfile format (header, sections, fixups).
- `references/behavior_graph.md` — every `hkb*` class, every enum, events and variables.
- `references/skeleton_ragdoll.md` — `hkaSkeleton`, ragdoll bodies, constraints, mappers.
- `references/animation_cache.md` — `animationdata` and `animationsetdata` text formats + the hash.
- `references/creature_nif.md` — the mesh side.
- `references/worked_example_wolf.md` — every one of the above, resolved for one real creature.

---

## 1. The ESM side — the RACE record

Verified from `references/xEdit/Core/wbDefinitionsTES5.pas` and a real
`Skyrim.esm` dump.

A creature's *actor* is an `NPC_` record pointing at a `RACE`. The RACE holds every
asset path. The subrecords that matter for animation:

| Subrecord | Meaning |
|---|---|
| `ANAM` | **Skeletal model** — path to `skeleton.nif`. Appears **twice**, after an `MNAM` (male) and an `FNAM` (female) empty marker. |
| `MNAM`/`FNAM` + `MODL` (in the *Behavior Graph* struct) | **Behavior graph project** `.hkx`. Also once per gender. |
| `MTNM` | Movement type names, 4-char tags (`WALK`,`RUN1`,`SNEK`,`BLDO`,`SWIM`). |
| `MTYP`+`SPED` | `MOVT` form + per-direction speed overrides. |
| `ATKD`+`ATKE` | Attack data + the **attack event name** the graph listens for. |
| `NAM4` | Material type (`MATT`) — impact/footstep material. |
| `NAM5` | Impact data set (`IPDS`). |
| `GNAM` | Body part data (`BPTD`) — dismemberment/hit boxes. |
| `VNAM` | Equipment flags bitfield (which weapon types the race can use). |
| `WNAM` | Skin (`ARMO`) — the actual body mesh, see §2. |
| `VTCK` | Voice types (`VTYP`), male + female. |

**Real values — `WolfRace` (`0001320A`)**
```
ANAM = Actors\Canine\Character Assets Wolf\skeleton.nif    (both genders)
MODL = Actors\Canine\WolfProject.hkx                       (both genders)
MTNM = WALK, RUN1, SNEK, BLDO, SWIM
ATKE = attackStart_Attack1, attackStart_Attack2,
       attackStart_ForwardPower, attackStart_StandingPower,
       attackStart_SkeeverLungeShort/Medium/Long, ...
VNAM = 7FFFE001   (bit 0 "Hand To Hand Melee" set; weapon bits set but unusable)
```

**Real values — `DraugrRace` (`00000D53`)**
```
ANAM = Actors\Draugr\Character Assets\Skeleton.nif   (male)
ANAM = Actors\Draugr\Character Assets\SkeletonF.nif  (female — a DIFFERENT file)
ATKE = attackStart1HMBackSlash, attackStartGSChop, bashStart, ...
```

> **The draugr proves two things at once:** genders can use different skeletons, and a
> humanoid's attack events are weapon-specific (`1HM`/`2HM`/`GS`/`H2H`) while the wolf's
> are a flat list. That distinction propagates all the way to §5's animsetdata.

`ATKE` is the **exact string** the behavior graph receives as an event. This is the
hand-off point between the ESM and Havok: the CK's *Idle Animations* tree sends an
event name, and the behavior graph's state machine has a transition keyed on it.

---

## 2. The mesh (NIF)

A creature has **two** distinct NIF files, easily confused:

1. **`skeleton.nif`** — pointed to by RACE `ANAM`. A pure `NiNode` hierarchy, no
   geometry. Defines the bone names and the bind pose. This is the *scene graph*
   counterpart of `skeleton.hkx`.
2. **The body mesh** — reached via RACE `WNAM` → `ARMO` → `ARMA` → its model path
   (e.g. `Actors\Canine\Character Assets Wolf\wolf.nif`). This holds the skinned
   geometry and is what you actually see.

The body mesh's skin bone list references bones **by name**; those names must exist
in `skeleton.nif`. See `references/creature_nif.md` for the full block-by-block
layout, shader properties, dismemberment partitions, and skin data.

---

## 3. The skeleton and ragdoll (`skeleton.hkx`)

One file, containing **both** skeletons plus the physics. Verified from the real
wolf and human files.

```
hkRootLevelContainer
└─ "Merged Animation Container"  hkaAnimationContainer
   ├─ skeletons[0]  hkaSkeleton   name = "NPC Root [Root]"    ← ANIMATION skeleton
   │                              72 bones (wolf): Canine_COM, Canine_Spine1…
   └─ skeletons[1]  hkaSkeleton                                ← RAGDOLL skeleton
                                  22 bones (wolf): Ragdoll_Canine_COM…
   "Physics Data"     hkpPhysicsData → hkpPhysicsSystem
                       ├─ hkpRigidBody[22]        (shapes: capsule / convex hull)
                       └─ hkpConstraintInstance[] (ragdoll + limited-hinge)
   "RagdollInstance"  hkaRagdollInstance  → rigidBodies, constraints,
                                            boneToRigidBodyMap, skeleton
   "SkeletonMapper"   hkaSkeletonMapper ×2   ← anim→ragdoll and ragdoll→anim
```

**The animation skeleton and the ragdoll skeleton are different sizes** (wolf: 72 vs
22). That is exactly why two `hkaSkeletonMapper` objects exist:

- **anim → ragdoll**: drives the physics bodies from the playing animation (a living
  creature's collision follows its animation).
- **ragdoll → anim**: drives the visible skeleton from physics (death ragdoll, and
  the pose-matching that makes `GetUpLeft`/`GetUpRight` pick the right get-up
  animation).

Bone names follow a `Name [4charTag]` convention: `NPC Root [Root]`,
`NPC COM [COM ]`, `NPC L Thigh [LThg]`. The bracketed tag is padded to 4
characters — the apparent "trailing space" in `[COM ]` is padding, not a typo.
Ragdoll bodies are named `Ragdoll_<animation bone name>`.

Full field tables: `references/skeleton_ragdoll.md`.

---

## 4. The behavior graph

This is the creature's "brain": a hierarchical Havok state machine that turns
**events** into **playing animations**.

### Project structure

The wolf's project is **six** files (from `animationdata/wolfproject.txt`):
```
Behaviors Wolf\WolfBehavior.hkx        ← root graph
Behaviors Wolf\NonCombatIdle.hkx       ← sub-graph
Behaviors Wolf\QuadrupedBehavior.hkx   ← sub-graph (the big one: locomotion+combat)
Behaviors Wolf\ForwardLocomotion.hkx   ← sub-graph (parametric walk/trot/run blend)
Characters Wolf\Wolf.hkx               ← character file
Character Assets Wolf\skeleton.HKX     ← skeleton
```
The draugr's is **three** — one monolithic `DraugrBehavior.hkx` and no sub-graphs.
Sub-graphs are stitched in with `hkbBehaviorReferenceGenerator`, which names another
`.hkx` file; the engine splices its root generator in at that point.

### The character file (`Wolf.hkx`)

`hkbCharacterData` + `hkbCharacterStringData`. The critical field is
**`animationFilenames`** — an ordered array. **The index into this array is the
animation ID used everywhere else**, most importantly by `animationdata`. Verified:
```
[ 0] Animations\AggroWarning1.hkx        [ 8] Animations\CombatIdle.hkx
[ 2] Animations\Attack1.hkx              [ 9] Animations\Death.hkx
[38] Animations\MT_Idle_Wolf.hkx         [65] Animations\TurnLoopingR.hkx
```
and `wolfproject.txt` says `Main_Idle_Wolf → 38`, `CombatIdle → 8`. They match.
The list is sorted case-insensitively by path; killmoves in
`..\SharedKillMoves\Human&Wolf\` sort under `P` for `Paired_…`.

### Inside the graph

- `hkbStateMachine` — states + transitions. Transitions fire on an **event ID** and
  may carry a condition and a blend (`hkbBlendingTransitionEffect`).
- `hkbClipGenerator` — a leaf that plays one animation. Its `name` is the key that
  `animationdata` matches to attach triggers.
- `hkbBlenderGenerator` — blends children by weight; with `FLAG_PARAMETRIC_BLEND` it
  becomes the speed-parametric walk→trot→run blend.
- `hkbModifierList` / `hkb*Modifier` — per-frame operations (foot IK, look-at,
  ragdoll control, timers).
- `hkbVariableBindingSet` — binds a node property to a **behavior variable**, which is
  how gameplay values (`Speed`, `Direction`, `iState`) steer the graph.

The wolf's death/getup topology, read straight from `WolfBehavior.hkx`:
```
RootAnimState ──(deathStart)──► DeathBlend → Death (Animations\Death.hkx)
              ──(Ragdoll)─────► AnimateToRagdoll → FullRagdoll
                                  └─ hkbRigidBodyRagdollControlsModifier
                                  └─ BSRagdollContactListenerModifier
              ──(GetUpStart)──► GetUpFromRagdoll
                                  └─ hkbPoseMatchingGenerator "Get Up Pose Matcher"
                                       ├─ GetUpLeft   (Animations\GetUpLeft.hkx)
                                       └─ GetUpRight  (Animations\GetUpRight.hkx)
```
`hkbPoseMatchingGenerator` compares the settled ragdoll pose against each candidate
clip's first frame and picks the closest — that is how the engine knows whether the
wolf fell on its left or right side.

Every class, field, and enum: `references/behavior_graph.md`.

---

## 5. The animation cache text files

Two parallel caches under `meshes\`. **The engine reads the `…singlefile.txt`
aggregates**; the per-project files are the unpacked equivalents.

| | `animationdata` | `animationsetdata` |
|---|---|---|
| Aggregate | `animationdatasinglefile.txt` | `animationsetdatasinglefile.txt` |
| Per project | `animationdata\wolfproject.txt` | `animationsetdata\wolfprojectdata\*.txt` |
| Holds | clip name → animation index, playback speed, crop, **annotation triggers**; plus root-motion | cache events, behavior variable ranges, **attack event → clip** map, CRC'd animation paths |
| Keyed by | `hkbClipGenerator.name` | attack event name |

### Why `animationdata` matters

It is where **annotations** live — the timed events inside a clip:
```
Death
9              ← animation index → Animations\Death.hkx
1              ← playback speed
0              ← crop start
0              ← crop end
4              ← trigger count
FootBack:0.133333
Ragdoll:0.267        ← at t=0.267s the graph receives "Ragdoll" → ragdoll takes over
FootFront:0.3
FootBack:0.333333
```
`SoundPlay:…` triggers fire sound, `Foot*` fire footstep effects, and
`Ragdoll`/`GetUpEnd` drive the state machine. A clip with no entry here is silent
and has no footsteps.

### The hash in `animationsetdata`

Animation paths are stored as **decimal CRC values**, not text:
```
7848002        ← CRC32("meshes\actors\canine\animations")   — directory
2952534464     ← CRC32("attack1")                           — file stem, no extension
7891816        ← constant for the "hkx" extension slot
```
The algorithm is **CRC-32 with polynomial 0x04C11DB7, init 0, reflected in and out,
and NO final XOR** (standard CRC-32 uses `0xFFFFFFFF` for init and xorout — this is
the one real trap). Input is lowercased, backslash-separated. Verified by reproducing
the real wolf values; **all 72 entries resolve**, including six that point into
`meshes\actors\sharedkillmoves\human&wolf`.

Note: `7891816` is a **literal constant** in the extension slot, not a CRC of
`"hkx"` (which is `2652099066`). Do not try to compute it.

Full line-by-line grammar for both files: `references/animation_cache.md`.

---

## 6. Events and variables — the runtime vocabulary

**Events** are strings, interned per graph into IDs. The graph reacts to them;
clips raise them via annotations. Wolf examples: `attackStart_Attack1`, `moveStart`,
`deathStart`, `Ragdoll`, `GetUpStart`, `SoundPlay`, `FootFront`, `staggerStart`.

Two conventions:
- `SoundPlay.<descriptor>` / `SoundStop.<descriptor>` — plays/stops a sound descriptor.
- `attackStart_<name>` — matches RACE `ATKE`, closing the ESM↔Havok loop.

**Variables** are typed per-graph slots readable and writable from Papyrus
(`GetAnimationVariableFloat` / `SetAnimationVariableBool` …) and bindable to node
properties. Wolf examples: `Speed`, `Direction`, `TurnDelta`, `iState`,
`bIsAnimationDriven`, `bAllowRotation`, `IsAttacking`, `IsBleedingOut`,
`SpeedSampled`, `iGetUpType`, `bIsWolf`.

`iState_<MovementTypeName>` variables hold the index of a CK movement type; an
`hkbEvaluateExpressionModifier` running `iState = iState_Sprinting` is what switches
the creature's movement type at runtime.

Full lists: `references/behavior_graph.md`.

---

## 7. Verification recipes

Fast checks against real files, all confirmed working:

```bash
# What files does a project reference, and in what index order?
python -c "import re;d=open(r'...\characters wolf\wolf.hkx','rb').read();\
print([m.decode() for m in re.findall(rb'[ -~]{4,}',d) if b'.hkx' in m.lower()])"

# Recompute an animationsetdata CRC (see references/animation_cache.md for the code)
bscrc32(r'meshes\actors\canine\animations')   # -> 7848002
bscrc32('attack1')                            # -> 2952534464

# Does every animationdata clip index resolve?
#   parse wolfproject.txt clip blocks, index into the character file's list.
#   Vanilla result: 110 clip blocks, 0 out of range.
```

---

## 8. Known traps

1. **The animation index is positional.** Inserting a file into
   `animationFilenames` renumbers everything after it and silently corrupts every
   `animationdata` clip block.
2. **`7891816` is a constant**, not `CRC32("hkx")`.
3. **The CRC has no final XOR.** Using stock CRC-32 gives wrong values everywhere.
4. **Skyrim LE `.hkx` is 32-bit, SSE is 64-bit** (packfile header `PointerSize`
   4 vs 8, `FileVersion` 0x08, version string `hk_2010.2.0-r1`). SSE will not load a
   32-bit behavior/skeleton file.
5. **`animationdatasinglefile.txt` is often shipped with 12288 NUL bytes of
   leading padding** in extracted copies; parsers must tolerate it.
6. **Line endings are mixed** (CRLF and bare LF) in the singlefile caches — split
   on universal newlines.
7. **Stale cache entries are legal.** The wolf's animsetdata references
   `turncannedl90flee`, which the wolf project does not ship. The engine tolerates it.
8. **A clip block with 0 triggers still needs its blank terminator line.**
