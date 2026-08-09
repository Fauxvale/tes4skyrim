# The Animation Cache Text Files

Two independent caches under `Data\meshes\`. Both are plain ASCII, line-oriented,
and both exist in an aggregated "singlefile" form (what the engine reads) and an
unpacked per-project form.

Sources: the **Skyrim Behavior Editor** parsers
(`src/animData/*.cpp`, `src/animSetData/*.cpp`), **Pandora**'s `BSCRC32.cs`, and
byte-level verification against vanilla `wolfproject.txt`,
`animationsetdata\wolfprojectdata\fullcharacter.txt`, and
`animationsetdatasinglefile.txt`.

```
meshes\
  animationdatasinglefile.txt          ← aggregate  (clip cache + root motion)
  animationdata\
    dirlist.txt                        ← ordered list of every project .txt
    wolfproject.txt                    ← one project, unpacked
    draugrproject.txt
    boundanims\anims_wolfproject.txt   ← see §4
  animationsetdatasinglefile.txt       ← aggregate  (attack/event cache)
  animationsetdata\
    dirlist.txt
    wolfprojectdata\fullcharacter.txt
    draugrprojectdata\draugr_1hmsword.txt   (…23 files for the draugr)
```

---

## 1. `animationdata` — clip cache and root motion

### 1.1 Purpose

For each **clip generator** in the behavior graph (matched by name) this file
supplies:
- which animation file to play (**by index**),
- playback speed and cropping,
- the **annotation triggers** — timed events fired during playback.

It also carries **root motion** (translation/rotation over time) in the aggregate
file, used when the creature's movement is animation-driven.

### 1.2 Per-project file grammar (`wolfproject.txt`)

```
<line>  1                       ← literal "1" (block/format marker)
<line>  6                       ← number of project file paths that follow
<line>  Behaviors Wolf\WolfBehavior.hkx
<line>  Behaviors Wolf\NonCombatIdle.hkx
<line>  Behaviors Wolf\QuadrupedBehavior.hkx
<line>  Behaviors Wolf\ForwardLocomotion.hkx
<line>  Characters Wolf\Wolf.hkx
<line>  Character Assets Wolf\skeleton.HKX
<line>  1                       ← "has clip data" flag: 1 = clip blocks follow, 0 = none
        ── then N clip blocks, to end of file ──
```

The project-file list is the same set the character `.hkx` names, in the same
order: behaviors first, then the character file, then the skeleton.

### 1.3 The clip block

Exactly 6 fixed lines, then one line per trigger, then **one blank line**:

```
TurnLoopingRight        ← [1] clip generator NAME  (matches hkbClipGenerator.name)
65                      ← [2] animation INDEX into the character file's list
1                       ← [3] playback speed        (float; 1 = normal)
0                       ← [4] crop start amount     (seconds off the front)
0                       ← [5] crop end amount       (seconds off the back)
4                       ← [6] trigger count
FootFront:0.0666667     ←     trigger: EVENTNAME:TIME_IN_SECONDS
FootBack:0.366667
FootFront:0.5
FootBack:0.733333
                        ←     MANDATORY blank line terminating the block
```

A block with zero triggers still ends with the blank line:
```
Main_Idle_Wolf
38
1
0
0
0
                        ← still required
```

Field notes:
- **[1] name** is free-form; vanilla contains names with spaces and even literal
  `.hkx` suffixes (`TrotForward Wolf.hkx` is a *clip name*, not a path). Mirrored
  variants use a `[Mirrored]` suffix: `TurnLoopingRight[Mirrored]`.
- **[2] index** is positional into `hkbCharacterStringData.animationFilenames`.
  This is the single most fragile link in the whole system.
- **[3] speed** is how one animation serves several clips. The wolf plays
  `WalkForward_Wolf.hkx` (index 67) as `WalkSlowForward00_Wolf`,
  `WalkForward00_Wolf`, and `WalkForwardFast_Wolf` at different speeds.
- **triggers** are `Name:Time`, colon-separated, time in seconds from clip start,
  normally ascending. The name is an **event** the graph receives (§6 of SKILL.md).

### 1.4 Verified example — the wolf

`wolfproject.txt` contains **110 clip blocks**; every index resolves inside the
74-entry animation list (0 out of range). Selected:

| clip name | idx | resolves to |
|---|---|---|
| `TurnLoopingRight` | 65 | `Animations\TurnLoopingR.hkx` |
| `TurnLoopingRight[Mirrored]` | 64 | `Animations\TurnLoopingL.hkx` |
| `Main_Idle_Wolf` | 38 | `Animations\MT_Idle_Wolf.hkx` |
| `CombatIdle` | 8 | `Animations\CombatIdle.hkx` |
| `Death` | 9 | `Animations\Death.hkx` |
| `WalkForward00_Wolf` | 67 | `Animations\WalkForward_Wolf.hkx` |
| `WalkForwardFast_Wolf` | 67 | *same file*, different speed |

The `Death` block shows the annotation mechanism driving state:
```
Death
9
1
0
0
4
FootBack:0.133333
Ragdoll:0.267       ← hands control to the ragdoll mid-animation
FootFront:0.3
FootBack:0.333333
```
and `GetUpRight` shows physics hand-back:
```
GetUpRight
33
1
0
0
8
FootBack:0.866667
AddCharacterControllerToWorld:0.867   ← restore the character controller
Getup:0.867
FootFront:0.9
FootBack:1.3
FootBack:1.53333
FootFront:1.56667
GetUpEnd:1.73333                      ← tell the state machine the get-up finished
```

### 1.5 The aggregate (`animationdatasinglefile.txt`)

```
<count of project .txt names>
<name 1>.txt
… (order matches animationdata\dirlist.txt)
<per-project block>   ← for each project, in the same order
```

Each per-project block is:
```
<animationDataLines>        ← line count of the clip-data section
1
<numProjectFiles>
<project file paths…>
<1 or 0>                    ← has clip data
<clip blocks…>              ← as §1.3
<animationMotionDataLines>  ← line count of the motion section
<motion blocks…>            ← §1.6
```

The two `…Lines` counts are **byte-exact line counts** of the sections that follow,
and the engine uses them to skip. Recompute them if you edit anything.

> **Extraction artifact:** vanilla copies of this file are frequently seen with
> **12288 leading NUL bytes** (an artifact of BSA extraction). Strip leading NULs
> before parsing. The `animationsetdatasinglefile.txt` counterpart is normally clean.

### 1.6 Root-motion (motion data) blocks

Only present in the aggregate. One block per animation **index**:

```
<animationIndex>
<duration>                 ← seconds
<translationCount>
<t> <x> <y> <z>            ← space separated, 4 values, per sample
…
<rotationCount>
<t> <x> <y> <z> <w>        ← space separated, 5 values (quaternion), per sample
…
                           ← blank line terminator
```
This is the extracted root translation/rotation curve. With it, the engine moves the
actor through the world to match the animation. Without an entry, root motion is
zero and the creature animates in place.

---

## 2. `animationsetdata` — attack and event cache

### 2.1 Aggregate grammar (`animationsetdatasinglefile.txt`)

```
39                                        ← project count
ChickenProjectData\ChickenProject.txt     ← project "data" names
HareProjectData\HareProject.txt
…
── then one project block per name, in order ──
```

Each **project block**:
```
<numFiles>
<file name 1>            e.g. FullCharacter.txt   (wolf: 1 file)
…                        draugr: 23 files, one per weapon configuration
<AnimSetData block 1>    ← one per file name, in order
…
```

Each **AnimSetData block**:
```
V3                       ← literal version tag
<numCacheEvents>
<event name…>            ← e.g. idleStart1HMA
<numVariables>
  <name>                 ┐ repeated numVariables times
  <minValue>             │  behavior variable + its legal range
  <maxValue>             ┘
<numAttackEntries>
  <attack event name>    ┐ e.g. attackStart_Attack1
  <unknown>              │  0 or 1; 1 seen on sprinting/normal attacks  [SEMANTICS UNVERIFIED]
  <numClips>             │
  <clip name…>           ┘  clip generator names that can service this attack
<numAnimationInfos>
  <path CRC>             ┐ decimal CRC32 of the DIRECTORY (see §3)
  <name CRC>             │ decimal CRC32 of the file stem
  7891816                ┘ constant extension slot
```

**Full-file verification:** parsing all 39 projects with this grammar consumes
`animationsetdatasinglefile.txt` exactly — 63522 of 63523 lines, zero non-empty
remainder. The grammar is correct.

### 2.2 Verified example — the wolf

`wolfprojectdata\fullcharacter.txt`, complete:
```
V3
0                     ← no cache events
0                     ← no behavior variables
7                     ← 7 attack entries
attackStart_Attack1
0
1
Attack1               ← the clip generator that plays it
attackStart_Attack2
0
1
Attack2
attackStart_ForwardPower
0
1
AttackPowerForward
attackStart_ForwardPowerShort
0
1
AttackPowerForward_Short
attackStart_SkeeverLungeLong
0
1
AttackPowerForward_Large
attackStart_SkeeverLungeShort
0
1
AttackPowerForward_Short
attackStart_StandingPower
0
1
AttackPowerStanding
72                    ← 72 animation infos
7848002               ← CRC("meshes\actors\canine\animations")
329189360             ← CRC(stem)
7891816
… (×72)
```
The attack event names match `WolfRace`'s `ATKE` subrecords exactly. **That is the
ESM→Havok binding**: the CK fires `attackStart_Attack1`, this cache says the
`Attack1` clip services it, and the behavior graph transitions accordingly.

Of the 72 animation infos: 65 hash to `meshes\actors\canine\animations`, 6 to
`meshes\actors\sharedkillmoves\human&wolf`, and 1 (`turncannedl90flee`) is a
**stale entry** the wolf project does not ship. Stale entries are tolerated.

### 2.3 The draugr contrast

23 files, one per weapon configuration:
```
Draugr_1HMAxeMace.txt, Draugr_1HMAxeMace_Shield.txt, Draugr_1HMSword.txt,
Draugr_2HM.txt, Draugr_Bow.txt, Draugr_GS.txt, Draugr_H2H.txt, Draugr_MTSolo.txt,
Combat_1HM_Taunts.txt, …
```
and each carries real cache events and variables:
```
V3
3
ForceEquipNoWeapChange
weapEquip
weapForceEquip
2
iLeftHandType
0
0
iRightHandType
3
4
7
attackStart1HMBackSlash
0
1
1HMAttackF.HKX
…
```
Here `iRightHandType` is declared with range 3..4 — this file set is selected when
the right-hand weapon type is in that range. **That is how a humanoid switches its
whole animation set when it draws a different weapon**, and it is why the wolf (one
weapon-less configuration) needs only `FullCharacter.txt`.

---

## 3. The hash — "BSCRC32"

### 3.1 Definition

CRC-32 with:

| parameter | value |
|---|---|
| polynomial | `0x04C11DB7` |
| initial value | `0` |
| reflect input | yes |
| reflect output | yes |
| **final XOR** | **`0` (none)** |

Standard CRC-32 uses init `0xFFFFFFFF` and xorout `0xFFFFFFFF`. **This is the
difference that breaks naive implementations** — `zlib.crc32` will not reproduce
these numbers.

Input is the string **lowercased**, with **backslash** separators, ASCII, and
the result is written in **decimal**.

Corroborated by two independent implementations: `HkCRC` in the Skyrim Behavior
Editor (`src/animSetData/hkcrc.cpp`) and `BSCRC32` in Pandora
(`Models/Patch.Skyrim64/AnimSetData/BSCRC32.cs`, which declares
`Initializer = 0, TruncatedPolynomial = 0x04C11DB7, FinalXorValue = 0,
ReverseResultBeforeFinalXor = true, ReverseDataBytes = true`).

### 3.2 Reference implementation

```python
def _revbyte(b):
    r = 0
    for i in range(8):
        r = (r << 1) | ((b >> i) & 1)
    return r

def _rev32(x):
    r = 0
    for i in range(32):
        r = (r << 1) | ((x >> i) & 1)
    return r

def bscrc32(s: str) -> int:
    """Skyrim animationsetdata path/name hash. Input must already be lowercase
    with backslash separators."""
    c = 0
    for b in s.encode('ascii'):
        c ^= _revbyte(b) << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if c & 0x80000000 \
                else (c << 1) & 0xFFFFFFFF
    return _rev32(c)
```

### 3.3 Verified values

| input | output |
|---|---|
| `meshes\actors\canine\animations` | `7848002` |
| `meshes\actors\sharedkillmoves\human&wolf` | `2697173992` |
| `attack1` | `2952534464` |
| `attack2` | `922044538` |
| `death` | `560276019` |
| `paired_extractwerewolfspirit` | `261631` |
| `hkx` | `2652099066` ← **note: NOT the extension constant** |

### 3.4 The three-line animation info

```
<CRC of directory path>   relative to Data\, lowercase, backslashes, NO trailing slash
<CRC of file stem>        lowercase, NO directory, NO ".hkx" extension
7891816                   literal constant
```
`7891816` occupies the extension slot and is identical in all 15865 animation-info
records across every vanilla project. It is **not** `CRC32("hkx")`. Treat it as a
magic constant. **[The origin of the value 7891816 is UNVERIFIED.]**

---

## 4. `boundanims\anims_<project>.txt`

A flat list of animation paths per project, e.g.
`animationdata\boundanims\anims_wolfproject.txt`. These accompany the clip cache.
**[The exact runtime role of the boundanims files is UNVERIFIED from primary
sources.]**

---

## 5. `dirlist.txt`

Present in both `animationdata\` and `animationsetdata\`. A plain ordered list of
the project `.txt` names. The order matches the order of project blocks in the
corresponding singlefile aggregate. `animationdata\dirlist.txt` includes not just
creatures but every animated object in the game (doors, traps, levers, FX,
bows) — creature projects are only the first ~40 entries.

---

## 6. Editing checklist

If you add an animation to a creature:

1. Add the file under `meshes\actors\<race>\animations\`.
2. Add its path to the character `.hkx` `animationFilenames` — **and accept that
   every index at or after the insertion point shifts**.
3. Fix every affected `<animationIndex>` in the project's `animationdata` block.
4. Add a clip block (name, new index, speed, crops, triggers).
5. Add the clip generator to the behavior graph and wire a transition to it.
6. Add an animation-info triple (dir CRC, stem CRC, `7891816`) in `animationsetdata`
   if the animation is reachable as an attack.
7. Recompute `animationDataLines` / `animationMotionDataLines` if editing the
   aggregate.

Appending at the **end** of `animationFilenames` avoids step 3 entirely, which is
why tools like Nemesis and Pandora append rather than insert.
