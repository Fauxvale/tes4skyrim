---
name: oblivion-creature-system
description: Complete reference for how a creature (goblin, rat, mountain lion, etc.) works in Oblivion (TES4) — the CREA record and every field, the skeleton.nif (bone tree, Bip01/NonAccum accumulation, BSXFlags, BSBound, UPB user-property buffers, bhk ragdoll bodies and constraints), the body-part NIF list (NIFZ/NIFT), the .kf animation files (NiControllerSequence, AnimGroups, ControlledBlock priorities, NiTextKeyExtraData text keys and their exact vocabulary), the IDLE record and Idle Manager tree, and creature sound types. Use when authoring, reading, debugging, or converting Oblivion creature assets, when you need the exact binary layout of a NIF/KF block at version 20.0.0.4, the exact set of legal animation group names, or the exact chain that turns a game event into a playing animation. This describes vanilla Oblivion only — it contains no conversion or import logic.
---

# How a Creature Works in Oblivion (TES4)

Every fact here was verified against **primary sources only**:

- **xEdit** `Core/wbDefinitionsTES4.pas` — the authoritative TES4 record definitions.
- **niftools `nif.xml`** (cloned to `references/nifxml/`) — the authoritative NIF
  format spec, used for every binary layout below.
- **Real vanilla Oblivion assets**, parsed byte-for-byte from
  `Oblivion - Meshes.bsa` (NIF 20.0.0.4). Census figures come from all **1,460**
  creature `.kf` sequences across all **34** vanilla creature folders.
- **The Oblivion Construction Set wiki** (`cs.uesp.net`) — `AnimGroups`,
  `Idle Animations`, `Animation Tab`, `Skeleton`, `3ds Max: Custom Creatures`.
  (Fetch these with `?action=raw`; normal WebFetch 403s.)
- **UESP** `Oblivion Mod:Mod File Format/CREA`.

Nothing here is derived from any conversion tool's source. Facts that could not
be verified are marked **[UNVERIFIED]**.

Running examples: the **goblin** (biped, weapon-using, full equip/attack set),
the **rat** (quadruped with a tail chain and swim animations), and the
**mountain lion** (quadruped, hand-to-hand only).

---

## 0. The big picture — the whole chain

An Oblivion creature is **four** cooperating pieces. There is **no behavior
graph**, **no animation cache**, and **no Havok `.hkx`** anywhere — those are
Skyrim concepts. Oblivion resolves animations by **filename convention inside
one folder**.

```
                    ┌─────────────────────────────────────────┐
   Oblivion.esm     │  CREA record  (e.g. "Goblin" 0009661A)  │
                    └─────────────────────────────────────────┘
                      │ MODL            │ NIFZ[]        │ KFFZ[]
                      │ (THE SKELETON)  │ (body parts)  │ (anim overrides)
                      ▼                 ▼               ▼
      meshes\creatures\goblin\skeleton.nif
                      │
   ┌──────────────────┼───────────────────────────────────────┐
   │ NiNode tree      │  BSXFlags=7 (Animated|Havok|Ragdoll)  │
   │  Scene Root      │  BSBound "BBX" (culling box)          │
   │   └ Bip01        │  UPB strings (BSBoneLOD, Mass, ...)   │
   │      └ Bip01 NonAccum                                     │
   │          └ Bip01 Pelvis → Spine → ... (the real bones)   │
   │  bhkRigidBody + bhkCapsuleShape per bone  ← the RAGDOLL  │
   │  bhkRagdollConstraint / bhkLimitedHingeConstraint         │
   │  NiBSBoneLODController                                    │
   └───────────────────────────────────────────────────────────┘
                      ▲                 ▲
        skinned to ───┘                 │
      meshes\creatures\goblin\*.nif  (GoblinChest01, GoblinHandL, ...)
                                        │
      meshes\creatures\goblin\*.kf   ───┘   ← animations, THE SAME FOLDER
        forward.kf idle.kf handtohandattackpower.kf ...
        idleanims\    ← SpecialIdle_*.kf, reachable ONLY via IDLE records
        specialanims\ ← per-actor overrides named by CREA.KFFZ
```

**The five links that must agree**, and what breaks when they don't:

| Link | Must match | Symptom when broken |
|---|---|---|
| `CREA.MODL` → `skeleton.nif` | file exists at that path | creature invisible / CS error |
| `NIFZ[]` body-part names | resolved **relative to MODL's folder** | body part missing |
| body-part NIF bone names | `skeleton.nif` NiNode names | mesh doesn't deform / explodes |
| `.kf` `NiControllerSequence.Name` | a legal **AnimGroup** (§4.1) | animation never plays |
| `.kf` `AccumRootName` | `Bip01` (or `Bip02`) present in skeleton | creature slides / doesn't move |

**Read these for the detail:**
- `references/crea_record.md` — every CREA field, enum and flag, byte-exact.
- `references/skeleton_nif.md` — skeleton.nif structure, ragdoll, UPB, NIF header.
- `references/animation_kf.md` — .kf layout, AnimGroups, text keys, priorities.
- `references/idle_and_sound.md` — IDLE records / Idle Manager, creature sounds.
- `references/worked_example.md` — goblin, rat and lion resolved end to end.

---

## 1. The ESM side — the CREA record

Oblivion creatures are **`CREA` records, not `NPC_`+`RACE`**. A creature carries
its own model, stats and sounds directly; **there is no RACE record involved**
and therefore no `RACE`-level skeleton pointer like Skyrim's `ANAM`. (TES4 `RACE`
exists, but it applies to NPCs only — it has no skeletal-model field at all.)

The fields that matter for assets:

| Sub | Name | Type | Meaning |
|---|---|---|---|
| `MODL` | Model | zstring | **Path to `skeleton.nif`**, e.g. `Creatures\Goblin\Skeleton.nif`. This is the *skeleton*, not the body. |
| `MODB` | Bound radius | float | Bounding radius. |
| `MODT` | Texture hashes | bytes | Model texture hash block. |
| `NIFZ` | Model List | zstring[] | **Body-part NIF filenames**, resolved in MODL's folder. |
| `NIFT` | Model list textures | bytes | Texture hashes for the list. Required. |
| `KFFZ` | Animations | zstring[] | **Animation overrides** taken from `specialanims\`. |
| `ACBS` | Configuration | struct[16] | Flags, spell points, fatigue, barter gold, level, calc min/max. |
| `DATA` | Creature Data | struct[20] | Type, skills, soul, health, attack damage, 8 attributes. |
| `RNAM` | Attack reach | u8 | Required. Goblin = 52. |
| `TNAM` | Turning speed | float | Required. |
| `BNAM` | Base scale | float | Required, default 1.0. |
| `WNAM` | Foot weight | float | Required, default 3.0. Goblin = 6.0. |
| `NAM0`/`NAM1` | Blood spray / decal | zstring | Blood effect overrides. |
| `CSCR` | Inherits sounds from | formid→CREA | Sound inheritance. |
| `CSDT`/`CSDI`/`CSDC` | Sound type / sound / chance | u32 / formid→SOUN / u8 | See §5. |

Full field list, every flag and every enum: **`references/crea_record.md`**.

### 1.1 The two model fields are not interchangeable

This is the single most misread part of the record:

- **`MODL` is the SKELETON.** Verified across vanilla: every creature's `MODL`
  ends in `Skeleton.nif`. It is *not* the visible body.
- **`NIFZ` is the list of visible body parts**, each skinned to that skeleton.
  Filenames only — the **folder comes from `MODL`**.

Real goblin (`0009661A`):

```
Model.MODL = Creatures\Goblin\Skeleton.nif
NIFZ[0] = GobLegs01.NIF      NIFZ[3] = GoblinHandR.NIF
NIFZ[1] = GoblinChest01.NIF  NIFZ[4] = GoblinHead.NIF
NIFZ[2] = GoblinHandL.NIF
```

A creature is therefore assembled from **several skinned meshes** sharing one
skeleton — which is why vanilla creatures show seams at the neck/wrist, and why
the head is a separate NIF (so NPCs look at the head, not the body centre).

### 1.2 `KFFZ` — per-actor animation overrides

`KFFZ` lists `.kf` filenames that live in the creature folder's **`specialanims\`**
subfolder and replace the default animation of the same AnimGroup for *that
actor only*. Only 6 vanilla creature folders have `specialanims\`:
`dog`, `gatekeeper`, `goblin`, `grummite`, `shambles`, `zombie`.

Example — `SE02Gatekeeper6` swaps its touch-spell animation per weapon:

```
Model.MODL = Creatures\GateKeeper\Skeleton.nif
KFFZ[0] = CastTouch_Punch.kf
KFFZ[1] = CastTouch_Sword.kf
```

Goblin shamans use `specialanims\idle_sharman.kf` and `forward_sharman.kf`
(Bethesda's spelling) to walk and idle differently from other goblins while
sharing the goblin skeleton and body meshes.

---

## 2. The skeleton — `skeleton.nif`

Path: `meshes\creatures\<creature>\skeleton.nif` (characters use
`meshes\characters\_male\skeleton.nif` / `skeletonbeast.nif`).

Per the CS wiki, the skeleton "is the base model containing all of the skeletal
information", holding "the skeletal structure, collision and havok data, and bone
LOD information". **It is the first object loaded for a creature**; meshes and
animations are then applied onto its bone structure.

### 2.1 File version (byte-verified)

Every vanilla creature NIF and KF:

```
Header string : "Gamebryo File Format, Version 20.0.0.4"
Version       : 0x14000004
Endian        : 1 (little)
User Version  : 11   (NIFs and most KFs; some KFs use 10)
BS Version    : 11
```

Consequences of 20.0.0.4 that trip up parsers written against Skyrim's 20.2.0.7:

| Field | Status at 20.0.0.4 |
|---|---|
| Header string table (`Num Strings`/`Strings`) | **ABSENT** (since 20.1.0.1) — all strings are inline `SizedString` |
| `Block Size` array | **ABSENT** (since 20.2.0.5) — blocks must be parsed **sequentially** |
| `NiAVObject.Flags` | `ushort` (BSVER ≤ 26), not `uint` |
| `NiExtraData.Next Extra Data` | **ABSENT** (until 4.2.2.0) |
| `NiControllerSequence.Accum Flags` | **ABSENT** (since 20.3.0.8) |
| `NiControllerSequence.Phase` | **ABSENT** in Bethesda's stream (byte-verified) |
| `NiControllerSequence.String Palette` | **PRESENT** (10.1.0.113 → 20.1.0.0) |
| `ControlledBlock.Priority` | single `byte`, **no padding** |

### 2.2 The bone tree and the accumulation contract

The root chain is fixed and is the heart of how Oblivion moves actors:

```
Scene Root
└── Bip01                 ← the ACCUMULATION root (AccumRootName in every .kf)
    └── Bip01 NonAccum    ← the NON-accumulating twin
        └── Bip01 Spine0 / Bip01 Pelvis / ...   ← the real bones
```

Both quadrupeds and bipeds use `Bip01`-prefixed names — the rat's tail is
`Bip01 Tail`…`Bip01 Tail4`, and it still has full `Bip01 L Finger*` /
`Bip01 R Toe*` chains inherited from the biped rig.

**How movement works** (CS wiki *3ds Max: Custom Creatures*, confirmed by the
byte layout): the animation stores the *same* motion on two nodes.

- **`Bip01`** carries only the **translation the engine consumes as movement**.
  Its interpolator has rotation and scale keys deleted, and (for ground movement)
  its **Z translation keys deleted** so the actor doesn't fly.
- **`Bip01 NonAccum`** carries the **in-place** version: translation X/Y zeroed,
  **Z kept**, so the body bobs without travelling.

The engine plays the skeleton in place and applies `Bip01`'s accumulated
translation to the actor's world position. This is why:

- Turn animations (`TurnLeft`/`TurnRight`) must be authored **in place** — the
  engine performs the actual rotation.
- For a **flying** creature you invert the rule: keep Z on `Bip01` and zero it on
  `Bip01 NonAccum`.
- `Bip01`/`Bip02` is the **first** controlled block and `Bip01 NonAccum` the
  **last**, in 1,426 of 1,449 vanilla sequences (the rest use `Bip02 NonAccum`).
  Treat this as the strong convention, not an invariant.

`AccumRootName` census over all 1,460 vanilla creature sequences: **`Bip01` 1,427,
`Bip02` 31**. (`Bip02` appears in multi-actor/rider rigs.)

### 2.3 Extra data on the skeleton

| Block | Name | Purpose |
|---|---|---|
| `BSXFlags` | `"BSX"` | Behaviour bits. Creatures = **7** = `Animated \| Havok \| Ragdoll`. |
| `BSBound` | `"BBX"` | Culling/bounding box: centre + half-dimensions. Lion: centre `(0, 0, 30.60)`, dims `(16.48, 53.95, 30.68)`. |
| `NiStringExtraData` | `"UPB"` | **User Property Buffer** — a `\r\n`-separated `key = value` text blob, one per bone. |
| `NiTextKeyExtraData` | — | On the skeleton, holds the export/compression settings for the embedded pose. |
| `NiBSBoneLODController` | — | Bone LOD groups (which bones drop out at distance). |

The **UPB** blob is where Oblivion stores per-bone Havok authoring data. Verified
verbatim from `mountainlion/skeleton.nif`:

```
BSBoneLOD#BoneRoot#              ← on Bip01
KFAccumRoot =
Mass = 0.000000
Ellasticity = 0.300000           ← Bethesda's spelling
Friction = 0.300000
Unyielding = 0
Simulation_Geometry = 2
Proxy_Geometry = <None>
Use_Display_Proxy = 0
Display_Children = 1
Disable_Collisions = 0
Inactive = 0
Display_Proxy = <None>
```

```
BSBoneLOD#Bone#5#                ← on a ragdoll bone (Bip01 Spine0)
Collision_Groups = 458760
Mass = 30.000000
Simulation_Geometry = 5
... (rest as above)
```

### 2.4 The ragdoll

The ragdoll lives **inside `skeleton.nif`** — there is no separate ragdoll file.
Real mountain lion (265 blocks total):

| Block type | Count | Role |
|---|---|---|
| `NiNode` | 40 | bones |
| `NiTransformController` | 39 | per-bone animation binding |
| `bhkBlendCollisionObject` | 29 | attaches a body to a bone, blends anim↔physics |
| `bhkRigidBody` | 29 | one rigid body per ragdoll bone |
| `bhkCapsuleShape` | 28 | limb collision volumes |
| `bhkSphereShape` | 1 | one rounded volume |
| `bhkBlendController` | 29 | animation/ragdoll blend weight |
| `bhkRagdollConstraint` | 16 | 3-DOF cone-twist joints |
| `bhkLimitedHingeConstraint` | 8 | 1-DOF joints (elbows/knees) |
| `bhkMalleableConstraint` | 4 | softened constraint wrappers |

`bhkBlendCollisionObject` (rather than a plain `bhkCollisionObject`) is what lets
a bone be animation-driven while alive and physics-driven when ragdolled.

Full block layouts: **`references/skeleton_nif.md`**.

---

## 3. The body meshes

Each `NIFZ` entry is an ordinary skinned NIF in the creature's folder, using
`NiTriShape`/`NiTriStrips` with `NiSkinInstance` → `NiSkinData` →
`NiSkinPartition`, whose bone list references `skeleton.nif` node names.

Rules verified from the CS wiki (*3ds Max: Custom Creatures*) and vanilla assets:

- **Every separate mesh needs its own skin instance** — not a shared one.
- **`Bip01` must be the first bone in the hierarchy.**
- The skeleton must be built from **bones, not a 3ds Max Biped**.
- Optional attachment bones, added only if the creature needs them:
  `Weapon` (wield), `SideWeapon` (1H sheath), `BackWeapon` (2H sheath),
  `Quiver`, `Torch`, `MagicNode` (cast origin),
  `Bip01 L Forearm Twist` (shield).
- Head is a separate NIF **on purpose** so NPCs look at the head.

Orientation and scale conventions (CS wiki): **+Y is forward**, Oblivion animates
at **30 FPS**, most animations are ~1 second, and per second of animation a
creature covers roughly **80–150 units walking / 200–400 running**.

---

## 4. Animations — the `.kf` files

Animations are **loose `.kf` files in the creature's own folder**. There is no
index, no cache and no manifest: the engine finds an animation by **filename
convention**, and identifies its purpose by the **sequence name inside the file**.

### 4.1 AnimGroups — the hardcoded vocabulary

The CS wiki is explicit: AnimGroups "appear to be hardcoded (ie. you cannot add
new animgroups)". Every animation must either be one of these or be a
`SpecialIdle_*`. The complete list, verbatim from the wiki:

```
AttackBackPower    BlockIdle       Equip         Recoil
AttackBow          CastSelf        FastBackward  Right
AttackForwardPower CastSelfAlt     FastForward   Stagger
AttackLeft         CastTarget      FastLeft      TorchIdle
AttackLeftPower    CastTargetAlt   FastRight     TurnLeft
AttackPower        CastTouch       Forward       TurnRight
AttackRight        CastTouchAlt    Idle          Unequip
AttackRightPower   DodgeBack       JumpLand      DynamicIdle
Backward           DodgeForward    JumpLoop      SpecialIdle
BlockAttack        DodgeLeft       JumpStart     Death
BlockHit           DodgeRight      Left
```

> **The AnimGroup is the value of the `Name` field on block 0, the
> `NiControllerSequence`** — *not* the filename. The filename selects *which
> variant* of the group is used; the sequence name declares *which group it is*.

**Variants.** Vanilla extends group names with suffixes, which the engine picks
between at runtime: `_A`/`_B`/`_C` (random variants), `_Chop`/`_Slash`/`_Slice`/
`_Cross`/`_UpperCut`/`_Scratch` (attack styles), and `_HandtoHand` (weapon
state). Measured examples: `AttackLeft_A`, `AttackPower_HandtoHand`,
`CastTouch_B`, `SpecialIdle_GetUpFaceUp`.

**Filename prefixes select by equipment.** The same group is stored in several
files distinguished by a prefix; the engine chooses by the actor's weapon/shield
state (CS wiki *Animation Tab*): `handtohand*`, `onehand*`, `twohand*`, `staff*`,
`bow*`, plus the movement modifiers `sneak*` and `swim*`. So `castself.kf` and
`onehandcastself.kf` both declare the `CastSelf` group; the first plays bare-handed,
the second with a 1H weapon drawn.

Top sequence names by frequency across all 1,460 vanilla creature sequences:
`Idle` 88, `Forward` 81, `TurnRight` 69, `TurnLeft` 68, `FastForward` 67,
`Backward` 65, `Equip` 57, `Unequip` 54, `Left`/`Right` 46, `Recoil` 43.
Note vanilla is **case-inconsistent** (`idle`, `forward`, `Fastforward` all occur)
— matching must be case-insensitive.

### 4.2 `NiControllerSequence` — block 0 (byte-verified layout)

At 20.0.0.4 the field order is exactly:

```
Name                 SizedString   ← THE ANIMGROUP
Num Controlled Blocks u32
Array Grow By        u32
Controlled Blocks[]  ControlledBlock × N     (33 bytes each, see below)
Weight               float         (1.0)
Text Keys            Ref → NiTextKeyExtraData
Cycle Type           u32           0=LOOP 1=REVERSE 2=CLAMP
Frequency            float         (1.0)
  -- NO Phase field at this version --
Start Time           float
Stop Time            float
Manager              Ptr           (-1 in a standalone .kf)
Accum Root Name      SizedString   ← "Bip01"
String Palette       Ref → NiStringPalette
```

Real `rat\forward.kf`: `Name='Forward'`, 71 controlled blocks, `Weight=1.0`,
`CycleType=0` (LOOP), `Frequency=1.0`, `0.0 → 1.1667s`, `AccumRootName='Bip01'`.

Cycle-type census: **CLAMP (2) = 837 files, LOOP (0) = 621**. Idles and locomotion
loop; attacks, recoils and staggers clamp.

### 4.3 `ControlledBlock` — 33 bytes, and the priority system

```
Interpolator          Ref    (4)
Controller            Ref    (4)
Priority              byte   (1)   ← NO padding
String Palette        Ref    (4)
Node Name Offset      u32    (4)   ← offset into the palette
Property Type Offset  u32    (4)
Controller Type Offset u32   (4)
Controller ID Offset  u32    (4)
Interpolator ID Offset u32   (4)
```

All five name fields are **offsets into the sequence's `NiStringPalette`** (a
single `\0`-separated blob), not inline strings.

**Priority decides who wins when two animations drive the same bone.** Higher
wins. Verified distribution across vanilla creature files:

| Priority | Files | Typical use |
|---|---|---|
| 0 | 149 | bones the animation explicitly does *not* claim |
| 20–31 | ~900 | ordinary locomotion/idle (30 is the single most common) |
| 40–55 | ~500 | attacks and reactions layered over movement |
| 60–70 | ~230 | high-priority upper-body overrides |
| 255 | 148 | absolute override |

A single file mixes priorities: goblin `idle.kf` uses `{0, 20, 255}`, so parts of
the body yield to whatever else is playing while others force through. The CS
wiki's guidance matches: idles use low values, "important parts of the animations"
use high ones.

### 4.4 Interpolators

| Block | Count (vanilla creatures) | Meaning |
|---|---|---|
| `NiBSplineCompTransformInterpolator` | 47,226 | **compressed B-spline** — the dominant format |
| `NiTransformInterpolator` | 44,482 | plain keyframes, backed by `NiTransformData` |
| `NiTransformData` | 23,423 | quat rotation / vector translation / float scale key arrays |
| `NiFloatInterpolator` | 3,569 | scalar tracks |
| `NiBoolInterpolator` | 354 | visibility toggles |

Bethesda compressed most creature motion as **B-splines** (`NiBSplineData` +
`NiBSplineBasisData`), which is why naive KF parsers that only handle
`NiTransformData` read vanilla creature animations as empty.

### 4.5 `NiTextKeyExtraData` — the event channel

This is Oblivion's **only** in-animation event mechanism (Skyrim's annotation
equivalent). It is an array of `(float time, SizedString value)`.

Two structural rules, true in all 1,458 vanilla sequences that have text keys:

- The **first key is `start`** at the sequence start time.
- The **last key is `end`** at the stop time, and these must equal the
  sequence's `Start Time`/`Stop Time`.

**A single key may hold several events separated by `\r\n`** — e.g.
`'Enum: Left\r\nEnum: Right'`, `'Hit\r\nEnum: Left\r\nEnum: Right'`.

The complete vocabulary, with measured counts:

| Key | Count | Meaning |
|---|---|---|
| `start` / `end` | 1458 each | mandatory bounds |
| `Enum: Left` / `Right` / `BackLeft` / `BackRight` | ~290 | **footstep** — fires the matching foot sound (`CSDT` types 0–3) |
| `Enum: Idle` | 63 | idle-state sound point |
| `Enum: Aware` | 39 | aware/alert sound point |
| `Enum: Attack` | 14 | attack sound point |
| `Hit` | ~390 | **the moment damage lands / a spell fires** |
| `Blend: <n>` / `blend: <n>` | ~90 | blend duration into the next animation |
| `Sound: <SoundEditorID>` | 249 | play that `SOUN` record at this time |
| `a:L` / `a:R` (also `a: L`) | ~150 | left/right arm marker |
| `m:L` / `m:R` | ~14 | movement marker |
| `Attach` / `Detach` | 51 / 47 | attach or detach an AnimObject |
| `Hold` / `Release` | 3 each | bow draw/loose |

Matching is **case-insensitive** (`enum:`/`Enum:`, `blend:`/`Blend:` all occur),
and vanilla contains genuine **typos that still work at their intended time** —
`'Eum: BackRight'`, `'Blind: 12'`, `'Hit.'`, `'HIT'`. Do not "fix" these when
reading vanilla data; do not rely on them when authoring.

Real `mountainlion\forward.kf` (4.0 s loop):

```
0.0000  start
0.1333  Blend: 15
0.2667  m:R
1.0000  Enum: Left
1.2333  m:L
1.5000  Enum: BackRight
...
4.0000  end
```

Real `goblin\handtohandattackpower.kf` (`AttackPower_HandtoHand`, CLAMP, 1.4667 s):

```
0.0000  start
0.5333  Enum: Attack
0.7000  Hit          ← damage lands here
0.8667  Enum: Right
1.0333  Enum: Left
1.4667  end
```

---

## 5. Idles, and the two special folders

Two subfolders of the creature folder have engine meaning:

| Folder | Reached via | Purpose |
|---|---|---|
| `idleanims\` | **`IDLE` records only** | `SpecialIdle_*.kf` — get-ups, look-arounds, scratches |
| `specialanims\` | **`CREA.KFFZ`** | per-actor overrides of a normal AnimGroup |

The CS wiki states the rule precisely: SpecialIdles "must be identified as
`SpecialIdle_*` … and must be located in a sub-folder of the main
creature/character folder named `idleanims`". The Idle Manager will only offer a
KF that sits there.

### 5.1 The `IDLE` record

```
EDID   Editor ID
MODL   the .kf file  (must be under idleanims\)
MODB   bound radius
CTDA   conditions   ← standard TES4 condition list
ANAM   Animation Group Section  (u8, see below)
DATA   Parent (formid→IDLE) + Previous (formid→IDLE)
```

`ANAM` encodes the body section **and** a flag in bit 7 (from xEdit
`wbIdleAnam`; note the flag is **inverted**):

| `ANAM & 0x7F` | Section |
|---|---|
| 0 | Lower Body |
| 1 | Left Arm |
| 2 | Left Hand |
| 3 | Right Arm |
| 4 | **Special Idle** |
| 5 | Whole Body |
| 6 | Upper Body |

If **bit `0x80` is clear**, the idle is flagged **"Must return a file"**.

These sections are the same LB/LA/LH/RA channels the CS animation preview
exposes: **one animation can play per section at a time**, which is how Oblivion
layers an upper-body action over lower-body locomotion without a behavior graph.

### 5.2 How the Idle Manager picks (CS wiki, *Idle Animations*)

Idles form a **tree per skeleton**. Top-level nodes are skeletons (e.g.
`Creatures\Goblin`); everything below is a candidate.

1. Find the top-level node matching the actor's skeleton.
2. Walk children in order; test each idle's **conditions**.
3. On a match, descend into *its* children and repeat.
4. The chosen idle is the one that matches while **none of its children do**.
5. If the chosen idle has **no KF**: if *Must Return a File* is set, skip it and
   continue to its next sibling; otherwise return nothing and play no idle.

Idles fire from `PickIdle`, on knockdown/get-up, on yield, continuously during
sleep and eat packages, on each line of dialogue (both speaker and listener), and
at random governed by the actor's **Energy Level** (`AIDT.EnergyLevel`) — low
energy idles more often.

### 5.3 Creature sounds

Sounds attach to the CREA record itself as repeating triples:

```
CSDT  Type (u32)   CSDI  Sound (formid→SOUN)   CSDC  Chance (u8)
```

| `CSDT` | Type | Fired by |
|---|---|---|
| 0 | Left Foot | text key `Enum: Left` |
| 1 | Right Foot | `Enum: Right` |
| 2 | Left Back Foot | `Enum: BackLeft` |
| 3 | Right Back Foot | `Enum: BackRight` |
| 4 | Idle | `Enum: Idle` |
| 5 | Aware | `Enum: Aware` |
| 6 | Attack | `Enum: Attack` |
| 7 | Hit | `Hit` |
| 8 | Death | death |
| 9 | Weapon | weapon impact |

`CSCR` ("Inherits Sounds from") points at another CREA to reuse its whole set.

Real goblin: 7 sound types — `0`/`1` share one footstep sound at chance 100,
`4` (Idle) at 75, `5` (Aware) 100, `6` (Attack) 80, `7` (Hit) 80, `8` (Death) 100.

**This is the join between §4.5 and the record**: a text key names the *category*,
and the CREA record supplies the actual `SOUN`. A creature with footstep text keys
but no `CSDT` 0–3 entries is silent when it walks. Conversely `Sound: <EditorID>`
text keys bypass this table and name a `SOUN` record directly.

---

## 6. What Oblivion does NOT have

Stated explicitly because the Skyrim equivalents are so often assumed:

- **No Havok behavior graphs.** No `.hkx` of any kind for creatures. No state
  machines, no `hkbClipGenerator`, no behavior variables or events.
- **No animation cache.** No `animationdata`/`animationsetdata` text files, no
  clip-index tables, no CRC-named folders.
- **No `RACE` record for creatures.** No `ANAM` skeletal-model field, no
  `MNAM`/`FNAM` behavior-graph fields.
- **No `.hkx` skeleton or ragdoll.** Bones, collision, constraints and bone-LOD
  all live in `skeleton.nif`.
- **No FootIK / graph-driven ragdoll raise.** Ragdoll blending is the
  `bhkBlendCollisionObject`/`bhkBlendController` pair.
- **AnimGroups cannot be extended.** The list in §4.1 is hardcoded; only
  `SpecialIdle_*` names are open-ended.

---

## 7. Quick diagnostic table

| Symptom | Check first |
|---|---|
| Creature invisible | `MODL` path; `skeleton.nif` present; `BSXFlags` has `Havok`+`Ragdoll` |
| Body part missing | `NIFZ` filename vs actual file (folder comes from `MODL`) |
| Mesh explodes / doesn't deform | body NIF bone names vs `skeleton.nif` NiNode names; `Bip01` first |
| T-pose, no motion | `NiControllerSequence.Name` is not a legal AnimGroup (§4.1) |
| Animates in place, doesn't travel | `Bip01` translation keys stripped, or `AccumRootName` ≠ `Bip01` |
| Flies / sinks while walking | Z keys left on `Bip01` (should be on `NonAccum` for ground creatures) |
| Silent footsteps | `Enum: Left/Right/...` text keys present but no `CSDT` 0–3 on the CREA |
| No damage from an attack | missing `Hit` text key |
| Never plays get-up / special idles | KF not in `idleanims\`, name not `SpecialIdle_*`, or IDLE conditions fail |
| Animation ignored in favour of another | `ControlledBlock.Priority` too low |
| Parser reads animation as empty | file uses `NiBSplineCompTransformInterpolator`, not `NiTransformData` |
| Parser desyncs mid-file | assumed a `Block Size` table or header string table — neither exists at 20.0.0.4 |
