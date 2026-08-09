# Oblivion `.kf` animation files — complete binary and semantic reference

Sources: niftools `nif.xml` (every layout below), the Oblivion CS wiki
(`AnimGroups`, `Animation Tab`, `3ds Max: Custom Creatures`,
`Importing Animations from Blender`), and byte-level parsing of all **1,460**
vanilla creature sequences from `Oblivion - Meshes.bsa`.

---

## 1. File identity

```
Header string : "Gamebryo File Format, Version 20.0.0.4\n"
Version       : 0x14000004
Endian        : 1  (little)
User Version  : 10 or 11     ← both occur in vanilla .kf
BS Version    : 11
```

A `.kf` contains **exactly one** `NiControllerSequence`, and it is **block 0**.

### 1.1 Header layout at 20.0.0.4

```
HeaderString    line terminated by 0x0A
Version         u32
Endian Type     u8
User Version    u32
Num Blocks      u32
BSStreamHeader:
    BS Version      u32
    Author          ExportString   ← 1-BYTE length prefix, includes trailing NUL
    Process Script  ExportString
    Export Script   ExportString
Num Block Types u16
Block Types     SizedString × N     (u32 length prefix, no NUL)
Block Type Index u16 × Num Blocks
                                    ← NO Block Size array (since 20.2.0.5)
                                    ← NO Num Strings / Strings (since 20.1.0.1)
Num Groups      u32
Groups          u32 × Num Groups
```

**Two traps** that break parsers written for Skyrim's 20.2.0.7:

1. **There is no `Block Size` array.** Blocks must be parsed **strictly
   sequentially**; you cannot seek to block *n*.
2. **There is no header string table.** Every string is an inline `SizedString`
   (u32 length + raw bytes, no terminator). `ExportString` in the BS header is
   different again: a **single byte** length that *includes* a trailing NUL.

---

## 2. `NiControllerSequence` (block 0)

Byte-verified field order at 20.0.0.4:

| Field | Type | Notes |
|---|---|---|
| `Name` | SizedString | **THE ANIMGROUP** |
| `Num Controlled Blocks` | u32 | |
| `Array Grow By` | u32 | always 1 in vanilla |
| `Controlled Blocks[]` | ControlledBlock × N | **33 bytes each**, §3 |
| `Weight` | float | 1.0 |
| `Text Keys` | Ref (i32) | → `NiTextKeyExtraData` |
| `Cycle Type` | u32 | 0 = LOOP, 1 = REVERSE, 2 = CLAMP |
| `Frequency` | float | 1.0 |
| `Start Time` | float | |
| `Stop Time` | float | |
| `Manager` | Ptr (i32) | `-1` in a standalone `.kf` |
| `Accum Root Name` | SizedString | `"Bip01"` |
| `String Palette` | Ref (i32) | → `NiStringPalette` |

**`Phase` is absent.** `nif.xml` marks it `since 10.1.0.106 until 10.4.0.1`, and
byte inspection confirms it is not in Bethesda's 20.0.0.4 stream. **`Accum Flags`
is also absent** (`since 20.3.0.8`).

Verified `rat\forward.kf`:

```
Name                 = 'Forward'
NumControlledBlocks  = 71
Weight               = 1.0
TextKeys ref         = 99
CycleType            = 0    (LOOP)
Frequency            = 1.0
Start .. Stop        = 0.0 .. 1.1666667      (1.1667 s)
Manager              = -1
AccumRootName        = 'Bip01'
StringPalette ref    = 3
```

### 2.1 Cycle type census (1,458 sequences with keys)

| Cycle | Count | Used by |
|---|---|---|
| 2 = `CYCLE_CLAMP` | 837 | attacks, recoil, stagger, equip, get-ups, death |
| 0 = `CYCLE_LOOP` | 621 | idles, all locomotion |

`CYCLE_REVERSE` (1) does not occur in vanilla creature animations.

---

## 3. `ControlledBlock` — 33 bytes

| Field | Type | Bytes |
|---|---|---|
| `Interpolator` | Ref | 4 |
| `Controller` | Ref | 4 |
| `Priority` | **byte** | **1** |
| `String Palette` | Ref | 4 |
| `Node Name Offset` | u32 | 4 |
| `Property Type Offset` | u32 | 4 |
| `Controller Type Offset` | u32 | 4 |
| `Controller ID Offset` | u32 | 4 |
| `Interpolator ID Offset` | u32 | 4 |

**`Priority` is a single byte with NO padding.** Assuming 3 bytes of alignment
padding (as later Bethesda formats use) desyncs the whole array — this is the
most common parsing mistake for this version.

All five `*Offset` fields index into the `NiStringPalette` blob. `0xFFFFFFFF`
means "no string". Property Type is normally unset; Controller Type is
`NiTransformController` for bones.

### 3.1 `NiStringPalette`

```
Palette   SizedString    a single blob of NUL-separated strings
Length    u32            the same length repeated
```

Read a name by scanning from `offset` to the next `\0`.

### 3.2 Priority — the layering system

Higher priority wins when two sequences drive the same bone. Distribution across
vanilla creature files (a file counts once per distinct priority it contains):

| Priority | Files | Meaning |
|---|---|---|
| 0 | 149 | bone explicitly *not* claimed — always yields |
| 10 | 34 | very low; background idles |
| 20 | 178 | low |
| 25 | 74 | |
| **30** | **452** | the single most common — ordinary locomotion/idle |
| 31 | 128 | |
| 40 | 124 | attacks layered over movement |
| 50 | 115 | |
| **55** | **256** | second most common — reactions |
| 60–70 | ~230 | high-priority upper-body overrides |
| **255** | **148** | absolute override |

One file mixes several. Goblin `idle.kf` uses `{0, 20, 255}`; goblin
`handtohandattackpower.kf` uses `{31, 255}`; `rat\forward.kf` uses a uniform
`{30}`.

The CS wiki's description matches the data: idle animations "tend to have low
values for this, and high values tend to correspond with the important parts of
the animations". The custom-creature tutorial recommends priority **1–10** for
idles so more important animations can take over.

---

## 4. Interpolators

Counts over all vanilla creature `.kf` files:

| Block | Count | Role |
|---|---|---|
| `NiBSplineCompTransformInterpolator` | 47,226 | **compressed B-spline transform** — the dominant format |
| `NiTransformInterpolator` | 44,482 | uncompressed; holds a base transform + `NiTransformData` ref |
| `NiTransformData` | 23,423 | the actual rotation/translation/scale key arrays |
| `NiFloatInterpolator` | 3,569 | scalar tracks |
| `NiBSplineData` | 1,324 | B-spline control points |
| `NiBSplineBasisData` | 1,326 | B-spline basis |
| `NiBSplineCompFloatInterpolator` | 570 | compressed scalar |
| `NiBoolInterpolator` | 354 | visibility |
| `NiBoolData` | 59 | |
| `NiBSplineTransformInterpolator` | 30 | uncompressed B-spline |
| `NiPoint3Interpolator` / `NiPosData` | 2 each | |

**Most vanilla creature motion is B-spline compressed.** A reader that only
understands `NiTransformData` will see a large fraction of vanilla creature
animation as empty. `NiTransformInterpolator` blocks whose `Data` ref is `-1`
carry only the static base transform.

`NiTransformInterpolator` layout:

```
Translation   Vector3
Rotation      Quaternion (w,x,y,z)
Scale         float
Data          Ref → NiTransformData
```

---

## 5. `NiTextKeyExtraData` — the event channel

This is Oblivion's only in-animation event mechanism. There is no annotation
track, no behavior-graph event, and no separate cache.

```
Name        SizedString
                            ← NO 'Next Extra Data' field at this version
Num Text Keys  u32
Text Keys[]    { float Time ; SizedString Value } × N
```

(`NiExtraData.Next Extra Data` is `until 4.2.2.0`, so absent at 20.0.0.4.)

### 5.1 Structural rules

- First key is **`start`** at `Start Time`.
- Last key is **`end`** at `Stop Time`.
- Per the CS wiki these "should be equal in value" to the sequence's
  `Start Time`/`Stop Time`. Vanilla honours this.
- **One key may carry several events separated by `\r\n`**, e.g.
  `'Enum: Left\r\nEnum: Right'`, `'Hit\r\nEnum: Left\r\nEnum: Right'`,
  `'Enum: Left\r\nSound: NPCLichFootFloat'`.

### 5.2 Complete vocabulary (measured counts)

| Key | Count | Meaning |
|---|---|---|
| `start` | 1458 | mandatory first key |
| `end` | 1458 | mandatory last key |
| `Enum: BackLeft` | 76 | rear-left footstep → `CSDT` 2 |
| `Enum: BackRight` | 73 | rear-right footstep → `CSDT` 3 |
| `Enum: Left` | ~72 | left footstep → `CSDT` 0 |
| `Enum: Right` | ~70 | right footstep → `CSDT` 1 |
| `Enum: Idle` | 63 | idle sound point → `CSDT` 4 |
| `Enum: Aware` | 39 | aware/alert sound → `CSDT` 5 |
| `Enum: Attack` | 14 | attack sound → `CSDT` 6 |
| `Hit` | ~390 | **damage lands / spell fires** → `CSDT` 7 |
| `Sound: <EditorID>` | 249 | play that `SOUN` record directly |
| `Blend: <n>` | ~90 | blend duration into the next animation |
| `a:L` / `a:R` | ~150 | left/right arm marker |
| `m:L` / `m:R` | ~14 | movement marker |
| `Attach` | 51 | attach an AnimObject |
| `Detach` | 47 | detach an AnimObject |
| `Hold` | 3 | bow draw held |
| `Release` | 3 | bow loosed |
| `Enum: RiderSoft` / `RiderLoud` | 9 | horse rider sound variants |
| `Enum: Hit` / `Enum: HitShader` | 3 | |
| `Enum: Death` | 1 | |

`Blend: <n>` values seen: 3, 5, 6, 8, 9, 10, 12, 15, 20, 30.

### 5.3 Case, spacing and typos

Matching is **case-insensitive** and tolerant of a space after the colon. All of
these occur in vanilla and all work: `Enum:`/`enum:`, `Blend:`/`blend:`,
`a:R`/`a: R`, `Hit`/`hit`/`HIT`.

Genuine misspellings that survive in shipped files (the engine simply ignores the
unrecognised token, but the *timing* of the surrounding keys still holds):
`'Eum: BackRight'`, `'Eum: Right'`, `'Eum: BackLeft'`, `'Blind: 12'`, `'Hit.'`,
`'Hit  -at xy'`.

Some keys also carry the **exporter's compression settings** rather than gameplay
events, e.g.
`'start -name Idle -loop -GlobalRatio 100 -GlobalCompressFloats true -GlobalDontCompress false'`
and `'-ObjRatio 100 -ObjCompressFloats true -ObjDontCompress true'`. These are
Civ4-exporter/KFUpdater artefacts, not engine input.

### 5.4 Worked examples

`mountainlion\forward.kf` — `Forward`, LOOP, 4.0 s:

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

`rat\forward.kf` — `Forward`, LOOP, 1.1667 s (all four feet):

```
0.0000  start
0.0667  Enum: Left
0.4000  Enum: BackRight
0.5667  Enum: Right
1.0000  Enum: BackLeft
1.1667  end
```

`goblin\handtohandattackpower.kf` — `AttackPower_HandtoHand`, CLAMP, 1.4667 s:

```
0.0000  start
0.5333  Enum: Attack
0.7000  Hit           ← damage applies here
0.8667  Enum: Right
1.0333  Enum: Left
1.4667  end
```

`goblin\idle.kf` — `Idle`, LOOP, 3.3333 s: only `start` and `end`.

---

## 6. AnimGroups — the hardcoded set

From the CS wiki `AnimGroups`, verbatim and complete. "AnimGroups … appear to be
hardcoded (ie. you cannot add new animgroups)." Every animation must be one of
these, or be a `SpecialIdle`.

```
AttackBackPower     BlockIdle        Equip          Recoil
AttackBow           CastSelf         FastBackward   Right
AttackForwardPower  CastSelfAlt      FastForward    Stagger
AttackLeft          CastTarget       FastLeft       TorchIdle
AttackLeftPower     CastTargetAlt    FastRight      TurnLeft
AttackPower         CastTouch        Forward        TurnRight
AttackRight         CastTouchAlt     Idle           Unequip
AttackRightPower    DodgeBack        JumpLand       DynamicIdle
Backward            DodgeForward     JumpLoop       SpecialIdle
BlockAttack         DodgeLeft        JumpStart      Death
BlockHit            DodgeRight       Left
```

- The AnimGroup is **`NiControllerSequence.Name`**, set in NifSkope as the value
  of the block-0 sequence. The CS wiki states this explicitly.
- `SpecialIdle` entries must be named **`SpecialIdle_*`** and live in
  **`idleanims\`**.
- `DynamicIdle` moves an actor into position to use a static (e.g. walking to a
  chair before sitting).
- Some combat animations only play while the actor is **alerted** (weapon drawn).
- `PlayGroup` accepts only these names — the script compiler rejects others.

### 6.1 Variant suffixes seen in vanilla

Appended to a base group name; the engine selects among them:

| Suffix | Purpose | Examples |
|---|---|---|
| `_A` `_B` `_C` | random variants | `AttackLeft_A`, `CastTouch_B`, `SpecialIdle_C` |
| `_Chop` `_Slash` `_Slice` | blade attack styles | `AttackRight_Chop` |
| `_Cross` `_UpperCut` `_Scratch` `_Jab` `_Hook` | unarmed styles | `AttackLeft_Scratch` |
| `_HandtoHand` | weapon state | `AttackPower_HandtoHand`, `Idle_HandtoHand` |
| `SpecialIdle_<name>` | named special idles | `SpecialIdle_GetUpFaceUp`, `SpecialIdle_Look` |

Most common vanilla creature sequence names: `Idle` 88, `Forward` 81,
`TurnRight` 69, `TurnLeft` 68, `FastForward` 67, `Backward` 65, `Equip` 57,
`Unequip` 54, `Left` 46, `Right` 46, `Recoil` 43, `AttackLeftPower` 41,
`AttackRightPower` 41, `AttackPower` 40, `Stagger` 40, `AttackRight` 40,
`AttackLeft` 37, `AttackForwardPower` 35, `AttackBackPower` 26, `CastTouch` 22,
`SpecialIdle_GetUpFaceUp` 21, `BlockIdle` 19, `BlockHit` 18,
`SpecialIdle_GetUpFaceDown` 18.

Vanilla is **case-inconsistent** — `idle` (8), `forward` (6), `backward` (6),
`Fastforward` (8), `stagger` (4), `equip` (4) all appear. Match case-insensitively.

### 6.2 Filename prefixes — selection by equipment state

The sequence *name* declares the group; the *filename* selects which variant of
that group is used, based on what the actor is holding. From the CS wiki
`Animation Tab`:

> "both castself.kf and onehandcastself.kf belong to the CastSelf animation
> group. … castself.kf is played when no weapon is held at the ready (and no
> shield), while onehandcastself.kf is played when a one-handed weapon is out."

Prefixes: `handtohand*`, `onehand*`, `twohand*`, `staff*`, `bow*`, plus movement
modifiers `sneak*` and `swim*`. The custom-creature tutorial is explicit:
*"To localize anims change the filename from WalkXXXX to handtohandxxx,
onehandxxx, twohandxxx, Staffxxx, Sneakxxx, swimxxx — **DO NOT CHANGE THE ACTIVE
SEQUENCE NAME**."*

Real goblin folder (105 files) shows the full matrix: `handtohandidle.kf`,
`onehandidle.kf`, `staffidle.kf`, `bowidle.kf`; `handtohandattackleft_cross.kf`,
`onehandattackright_slash.kf`, `staffattackright.kf`, `attackbow.kf`; plus
`block.kf`/`onehandblock.kf`/`staffblock.kf`, `recoil.kf`, `stagger.kf`.

### 6.3 Animation group sections (LB / LA / LH / RA)

The CS preview exposes four channels, which are the same values the `IDLE`
record's `ANAM` selects:

| | Section |
|---|---|
| LB | Lower Body |
| LA | Left Arm |
| LH | Left Hand |
| RA | Right Arm |

**One animation may play per section at a time.** This is how Oblivion layers an
upper-body action over lower-body locomotion without a behavior graph. Sections
do *not* restrict which bones an animation touches — `bowattack.kf` plays in the
Right Arm section but visibly animates both arms.

---

## 7. Accumulation — how a creature actually moves

The rule, from the CS wiki custom-creature tutorial and confirmed by the data:
the animation stores its motion **twice**.

| Node | Carries | Keys removed |
|---|---|---|
| `Bip01` | the **travel** the engine applies to the actor's world position | rotation keys, scale keys, and **Z translation** (ground creatures) |
| `Bip01 NonAccum` | the **in-place** motion | X/Y translation zeroed, **Z kept** |

Authoring procedure from the tutorial: delete `Bip01`'s interpolator data, copy
`Bip01 NonAccum`'s data into it, then strip the two sets as above.

Consequences:

- `TurnLeft` / `TurnRight` are authored **in place**; the engine rotates the actor.
- **Flying** creatures invert the Z rule — keep Z on `Bip01`, zero it on `NonAccum`.
- Block ordering, measured over 1,449 vanilla sequences with resolvable palettes:
  the **first** controlled block is `Bip01` (1,422) or `Bip02` (22); the **last**
  is `Bip01 NonAccum` (1,426) or `Bip02 NonAccum` (22). A strong convention, but
  a handful of files break it (`Bip01 Spine0` first in 3 files).
- `AccumRootName` census: `Bip01` 1,427 · `Bip02` 31.

Movement distance guidance (tutorial): per second of animation roughly **80–150
units walking**, **200–400 running**. Oblivion animates at **30 FPS**; most
animations are about **1 second**. A too-short attack animation makes the creature
attack unrealistically fast, because the `Hit` key arrives sooner.

---

## 8. The two special folders

| Folder | Reached via | Contents |
|---|---|---|
| `<creature>\idleanims\` | **`IDLE` records only** | `SpecialIdle_*.kf` |
| `<creature>\specialanims\` | **`CREA.KFFZ`** | per-actor overrides of normal groups |

Only these vanilla creature folders have `specialanims\`: `dog` (wolf variants),
`gatekeeper`, `goblin` (shaman), `grummite` (staff set), `shambles`, `zombie`.

Rat `idleanims\`: `scan.kf`, `scratch.kf`, `scratch2.kf`, `getupleft.kf`,
`getupright.kf`.
Goblin `idleanims\`: `getupfaceup.kf`, `getupfacedown.kf`,
`specialidle_tracking.kf`, `specialidle_dodge.kf`, `specialidle_flee.kf`,
`specialidle_intimidate.kf` (+2 variants), `specialldle_guard.kf` (note the
shipped typo — `l` for `i`).

---

## 9. Parsing checklist

Writing a correct Oblivion `.kf` reader:

1. Parse the header **sequentially**; there is no block-size table and no string
   table at 20.0.0.4.
2. `ExportString` (BS header) = **1-byte** length including NUL.
   `SizedString` (everywhere else) = **u32** length, no NUL.
3. Block 0 is the `NiControllerSequence`.
4. `ControlledBlock` is **33 bytes**; `Priority` is one byte with **no padding**.
5. After the block array: `Weight`, `TextKeys`, `CycleType`, `Frequency`,
   **`StartTime`**, **`StopTime`**, `Manager`, `AccumRootName`, `StringPalette`.
   **No `Phase`, no `Accum Flags`.**
6. Resolve node/controller names through the `NiStringPalette` blob.
7. Expect **B-spline** interpolators, not just `NiTransformData`.
8. Split text-key values on `\r\n`; compare case-insensitively; tolerate a space
   after the colon.
