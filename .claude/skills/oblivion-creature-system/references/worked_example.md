# Worked examples — goblin, rat and mountain lion, resolved end to end

Every value below was read from vanilla `Oblivion.esm` and
`Oblivion - Meshes.bsa`. Nothing is inferred.

---

## 1. The goblin — a biped that uses weapons

### 1.1 The record (`Goblin`, `0009661A`)

```
Model.MODL = Creatures\Goblin\Skeleton.nif
NIFZ = GobLegs01.NIF, GoblinChest01.NIF, GoblinHandL.NIF,
       GoblinHandR.NIF, GoblinHead.NIF
DATA.Type = 0 (Creature)   Health = 15   AttackDamage = 3   Speed = 12
DATA.Soul = 1 (Petty)
RNAM.AttackReach = 52      BNAM.BaseScale = 1.0    WNAM.FootWeight = 6.0
AIDT.Aggression = 100  Confidence = 70  EnergyLevel = 80  Responsibility = 0
(no KFFZ — uses the folder defaults)
```

### 1.2 The skeleton — 58 nodes, 264 blocks

```
BSXFlags "BSX" = 7  (Animated | Havok | Ragdoll)
BSBound  "BBX"  center (0, 0, 65.14)  dims (30.0, 30.0, 65.14)
```

Bone tree (abridged — full list in `skeleton_nif.md` §3.3):

```
Scene Root
└─ Bip01                        ← accumulation root
   └─ Bip01 NonAccum
      └─ Bip01 Spine0
         ├─ Bip01 Pelvis → L/R Thigh → Calf → Foot → Toe0
         ├─ Bip01 Tail, Bip01 Tail1
         └─ Bip01 Spine → Spine1 → Spine2
            ├─ Bip01 Neck → Bip01 Head
            ├─ Bip01 L Clavicle → L UpperArm → L Forearm → L Hand
            │     → L Finger0/01/02 … Finger3/31/32
            │  Torch                    ← attachment bone
            │  Bip01 L ForearmTwist     ← shield bone
            └─ Bip01 R Clavicle → … → R Hand
                  Weapon                ← weapon bone
                  Quiver                ← quiver bone
```

Ragdoll: 18 `bhkRigidBody` + 17 `bhkCapsuleShape` + 1 `bhkSphereShape`,
joined by 11 `bhkLimitedHingeConstraint` (elbows/knees) and
6 `bhkRagdollConstraint` (shoulders/hips/spine).

Root UPB:

```
BSBoneLOD#BoneRoot#
BSPriority#20#
Mass = 74.000000        Ellasticity = 0.300000   Friction = 0.300000
Simulation_Geometry = 2 Collision_Groups = 458760
```

### 1.3 The animation set — 105 files

Because the goblin has `Weapon`, `Quiver` and `Torch` bones and the
`Weapon & Shield` capability, it carries the **full equipment matrix**:

| Group | Bare hands | 1H | Staff | Bow |
|---|---|---|---|---|
| Idle | `handtohandidle.kf` | `onehandidle.kf` | `staffidle.kf` | `bowidle.kf` |
| Equip | `handtohandequip.kf` | `onehandequip.kf` | `staffequip.kf` | `bowequip.kf` |
| Unequip | `handtohandunequip.kf` | `onehandunequip.kf` | `staffunequip.kf` | `bowunequip.kf` |
| Block | `handtohandblock.kf` | `onehandblock.kf` | `staffblock.kf` | — |
| BlockHit | `handtohandblockhit.kf` | `onehandblockhit.kf` | `staffblockhit.kf` | — |
| Attack | `handtohandattackright_cross.kf`, `…_uppercut.kf`, `…_scratch.kf` | `onehandattackright_chop.kf`, `…_slash.kf`, `…_slice.kf` | `staffattackright.kf` | `attackbow.kf` |
| Power attack | `handtohandattackpower.kf`, `…leftpower.kf`, `…rightpower.kf`, `…forwardpower.kf` | `onehandattack*power.kf` | `staffforward.kf` | — |

Shared, equipment-independent: `forward.kf`, `backward.kf`, `left.kf`,
`right.kf`, `runforward.kf`, `turnleft.kf`, `turnright.kf`, `idle.kf`,
`recoil.kf`, `stagger.kf`, `block.kf`, `blockhit.kf`,
`castself.kf`, `casttarget.kf`, `casttouch.kf`.

`idleanims\` (reachable only through `IDLE` records):
`getupfaceup.kf`, `getupfacedown.kf`, `specialidle_tracking.kf`,
`specialidle_dodge.kf`, `specialidle_flee.kf`, `specialidle_intimidate.kf`
(+ `2`, `3`), and `specialldle_guard.kf` — **note the shipped typo**
(`specialldle`, an `l` for the `i`).

`specialanims\`: `idle_sharman.kf`, `forward_sharman.kf` (Bethesda's spelling).

### 1.4 A real sequence — `handtohandattackpower.kf`

```
Name         = 'AttackPower_HandtoHand'   ← group + weapon-state suffix
CycleType    = 2 (CLAMP)
Start .. Stop= 0.0 .. 1.4667 s
AccumRoot    = 'Bip01'
Priorities   = {31, 255}

0.0000  start
0.5333  Enum: Attack     → CSDT 6, plays SOUN 000A653C at 80% chance
0.7000  Hit              → damage applies; CSDT 7 plays 000A6541
0.8667  Enum: Right      → CSDT 1, footstep 000A653F
1.0333  Enum: Left       → CSDT 0, footstep 000A653F
1.4667  end
```

### 1.5 The Goblin Shaman — `KFFZ` in action (`CreatureGoblin4Shaman`, `0001FCB4`)

```
Model.MODL = Creatures\Goblin\Skeleton.NIF      ← same skeleton
NIFZ = GoblinHead.NIF, ShamanBag.NIF, ShamanCape.NIF,
       ShamanChest.NIF, ShamanHelmet.NIF, ShamanLegs01.NIF   ← different body
KFFZ[0] = Idle_Sharman.kf                        ← from specialanims\
WNAM.FootWeight = 7.5     DATA.Health = 11    DATA.Soul = 3 (Common)
```

This is the whole override model in one record: **same skeleton, different body
meshes, one animation swapped**. The engine loads
`meshes\creatures\goblin\specialanims\idle_sharman.kf` in place of the folder's
`idle.kf` for this actor only. Everything else still comes from the shared goblin
folder.

---

## 2. The rat — a quadruped that swims

### 2.1 The record (`SE11SanctifiedRat`, `0006D107` — a scaled variant of the base rat)

```
Model.MODL = Creatures\Rat\Skeleton.NIF
NIFZ = Eyes.NIF, Head.NIF, mange.NIF, Rat.NIF, Whiskers.NIF
DATA.Type = 0    Health = 4   AttackDamage = 2   Speed = 9   Soul = 1 (Petty)
RNAM.AttackReach = 96    WNAM.FootWeight = 3.0
```

**All nine sound types are populated, including all four feet** — the quadruped
contrast with the goblin:

| `CSDT` | Type | Sound | Chance |
|---|---|---|---|
| 0 | Left Foot | `0004C5A0` | 100 |
| 1 | Right Foot | `0004C5A0` | 100 |
| 2 | **Left Back Foot** | `0004C5A0` | 100 |
| 3 | **Right Back Foot** | `0004C5A0` | 100 |
| 4 | Idle | `0004C5A1` | 75 |
| 5 | Aware | `0004C59E` | 100 |
| 6 | Attack | `0004C59D` | 100 |
| 7 | Hit | `0004C5A2` | 75 |
| 8 | Death | `0004C59F` | 100 |

All four feet share one sound; the *timing* comes from the animation's text keys.

### 2.2 The skeleton — 70 nodes, 314 blocks

```
BSXFlags = 7      BSBound center (0, 0, 20.0)  dims (24.0, 56.0, 20.0)
```

Y (56.0) exceeds X (24.0) — a long, low body. The rat uses `Bip01 Skull`
(not `Bip01 Head`) and `Bip01 Ponytail1/2` for the snout and whiskers, keeps a
5-segment tail (`Bip01 Tail`…`Tail4`), and **retains the complete biped finger and
toe chains** (`Finger0`…`Finger4` with children, `Toe0`…`Toe4` with children)
even though a rat has no fingers — they are inherited from the base rig and
simply unused.

Ragdoll: 21 rigid bodies, 20 capsules, 1 sphere, 13 `bhkRagdollConstraint` and
7 `bhkMalleableConstraint` (the malleable wrappers soften the flexible spine and
tail). Root UPB: `BSPriority#6#`, `Mass = 20.0`, `Friction = 0.600000`,
`Ellasticity = 0.200000`, `Simulation_Geometry = 5`.

### 2.3 Animations — 37 files, including a full swim set

```
land:  forward.kf backward.kf fastforward.kf turnleft.kf turnright.kf idle.kf
hand:  handtohandidle.kf handtohandforward.kf handtohandbackward.kf
       handtohandfastforward.kf handtohandturnleft/right.kf
       handtohandattackpower.kf handtohandattackforwardpower.kf
       handtohandrecoil.kf handtohandstagger.kf
       handtohandequip.kf handtohandunequip.kf
swim:  swimforward.kf swimbackward.kf swimfastforward.kf swimidle.kf
       swimhandtohandattackpower.kf swimhandtohandattackforwardpower.kf
       swimhandtohandrecoil.kf swimhandtohandstagger.kf
idleanims\: scan.kf scratch.kf scratch2.kf getupleft.kf getupright.kf
```

The `swim*` prefix pairs with `ACBS` bit 4 (**Swims**). The rat has **no**
`onehand*`/`staff*`/`bow*` files and no `Weapon` bone — it cannot use weapons.

### 2.4 Real sequences

`forward.kf` — `Forward`, LOOP, 1.1667 s, 71 controlled blocks, uniform priority
30, all four feet in gait order:

```
0.0000  start
0.0667  Enum: Left        → front-left
0.4000  Enum: BackRight   → rear-right
0.5667  Enum: Right       → front-right
1.0000  Enum: BackLeft    → rear-left
1.1667  end
```

The last controlled block is `Bip01 NonAccum`; the first is `Bip01`.

`idle.kf` — `Idle`, LOOP, 2.0 s, priority 30 uniform:

```
0.0000  start
0.0333  Enum: Idle        → CSDT 4, 75% chance
2.0000  end
```

`handtohandattackpower.kf` — `AttackPower`, CLAMP, 1.0 s, priority **55**:

```
0.0000  start
0.0333  Enum: Attack
0.6667  Hit
0.8333  Enum: Left\r\nEnum: Right    ← TWO events in ONE key
1.0000  end
```

---

## 3. The mountain lion — a pure quadruped predator

### 3.1 The record (`MS47MountainLion`, `000CA004`)

```
Model.MODL = Creatures\MountainLion\Skeleton.NIF
NIFZ = Head.NIF, lion_Body.NIF, paw_L.NIF, paw_R.NIF
ACBS.Flags = 576  (0x240 = Walks | No Low Level Processing)
DATA.Type = 0   Health = 160   AttackDamage = 32   Speed = 25   Soul = 3 (Common)
RNAM.AttackReach = 55    BNAM.BaseScale = 1.0   WNAM.FootWeight = 3.0
```

Only four body parts — the simplest of the three.

### 3.2 The skeleton — 40 nodes, 265 blocks

```
BSXFlags = 7    BSBound center (0, 0, 30.60)  dims (16.48, 53.95, 30.68)
```

40 bones (full list in `skeleton_nif.md` §3.1): a longer neck than the rat
(`Bip01 Neck`, `Neck1`, `Neck2` → `Bip01 Head`), a 4-segment spine
(`Spine`…`Spine3`), short 2-bone paws (`Finger0`/`Finger01`, `Toe0`/`Toe01`)
and a 5-segment tail.

**Heaviest ragdoll of the three**: 29 rigid bodies, 28 capsules, 1 sphere,
16 `bhkRagdollConstraint`, 8 `bhkLimitedHingeConstraint`, 4 `bhkMalleableConstraint`.

Root UPB has `Mass = 0.000000` and `Simulation_Geometry = 2` (the root is not a
simulated body); ragdoll bones use `BSBoneLOD#Bone#5#`,
`Simulation_Geometry = 5`, `Mass = 30.0`, `Collision_Groups = 458760`.

The skeleton also embeds a default pose in `NiTextKeyExtraData`:

```
0.0000  'start -name Idle  -loop -GlobalRatio 100
         -GlobalCompressFloats true -GlobalDontCompress false'
0.8333  'end '
```

### 3.3 Animations — 26 files, hand-to-hand only

```
forward.kf backward.kf runforward.kf turnleft.kf turnright.kf idle.kf
recoil.kf stagger.kf
handtohandidle.kf handtohandforward.kf
handtohandattackpower.kf handtohandattackforwardpower.kf
handtohandattackleftpower.kf handtohandattackrightpower.kf
handtohandequip.kf handtohandunequip.kf
idleanims\: getup_left.kf getup_right.kf
            specialidle_aware.kf specialidle_clean.kf specialidle_look.kf
```

No `swim*` (no Swims flag), no weapon animations, no `specialanims\`.

### 3.4 Real sequences

`forward.kf` — `Forward`, LOOP, **4.0 s** (a long, slow prowl), priority 30:

```
0.0000  start
0.1333  Blend: 15         ← 15-unit blend into this animation
0.2667  m:R
1.0000  Enum: Left
1.2333  m:L
1.5000  Enum: BackRight
  …
4.0000  end
```

`idle.kf` — `Idle`, LOOP, 3.3333 s, priorities `{20, 255}`, only `start`/`end`.
The mixed priorities let parts of the body yield to a layered special idle while
others force through.

`handtohandattackpower.kf` — `AttackPower`, CLAMP, 1.1667 s, priorities `{31, 255}`:

```
0.0000  start
0.3667  Enum: Attack
0.6333  Hit
0.7333  Enum: BackRight
0.8333  Enum: Left\r\n     ← trailing CRLF, single event
0.9333  Enum: Right
1.1000  Enum: Left\r\n
1.1667  Enum: BackRight    ← same time as 'end'
1.1667  end
```

Note the lion **lands all four feet during a pounce** and that two keys share the
stop time — both legal.

---

## 4. Side-by-side

| | Goblin | Rat | Mountain lion |
|---|---|---|---|
| Body parts (`NIFZ`) | 5 | 5 | 4 |
| Skeleton nodes | 58 | 70 | 40 |
| Total NIF blocks | 264 | 314 | 265 |
| Rigid bodies | 18 | 21 | 29 |
| Ragdoll constraints | 6 | 13 | 16 |
| Limited hinges | 11 | 0 | 8 |
| Malleable constraints | 0 | 7 | 4 |
| `.kf` files | 105 | 37 | 26 |
| Weapon bones | `Weapon`, `Quiver`, `Torch` | none | none |
| Foot sound types | 0, 1 | 0, 1, 2, 3 | — |
| Swim set | no | **yes** | no |
| `specialanims\` | yes (shaman) | no | no |
| `BSBound` dims | 30.0, 30.0, 65.14 | 24.0, 56.0, 20.0 | 16.48, 53.95, 30.68 |
| `BSXFlags` | 7 | 7 | 7 |
| Head bone | `Bip01 Head` | `Bip01 Skull` | `Bip01 Head` |
| Tail segments | 2 | 5 | 5 |

The pattern: **the folder is the creature**. Capability is expressed by which
files exist in it (`swim*` → swims, `onehand*` → uses weapons,
`idleanims\` → has special idles), by which bones the skeleton has
(`Weapon` → can wield), and by which `CSDT` entries the record carries
(2 and 3 → has back feet). There is no manifest anywhere tying these together —
the engine resolves each by name at the moment it is needed.
