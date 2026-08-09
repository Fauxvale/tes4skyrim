# `skeleton.nif` — the creature skeleton, collision and ragdoll

Sources: niftools `nif.xml`, the Oblivion CS wiki (`Skeleton`,
`3ds Max: Custom Creatures`), and byte-level parsing of the vanilla goblin, rat
and mountain lion skeletons from `Oblivion - Meshes.bsa`.

Path: `meshes\creatures\<creature>\skeleton.nif`
(characters: `meshes\characters\_male\skeleton.nif` and `skeletonbeast.nif`).

The CS wiki defines its role: the skeleton "is the base model containing all of
the skeletal information", holding "the skeletal structure, collision and havok
data, and bone LOD information for that creature", and **it is the first object
brought into the game for rendering a creature** — meshes and animations are then
applied onto its bone structure.

For characters the choice is automatic: a race with a tail mesh on the Body Data
page gets `skeletonbeast.nif`, otherwise `skeleton.nif`. Creature skeletons are
"much more loosely defined in comparison to races, and so are the most
customizable models".

---

## 1. NIF version and header

```
Header string : "Gamebryo File Format, Version 20.0.0.4"
Version       : 0x14000004
Endian        : 1 (little)
User Version  : 11
BS Version    : 11
```

Version-driven field presence (from `nif.xml`) — the traps:

| Field | Status at 20.0.0.4 |
|---|---|
| `Num Strings` / `Strings` (header string table) | **ABSENT** (`since 20.1.0.1`) — all strings inline |
| `Block Size` array | **ABSENT** (`since 20.2.0.5`) — parse blocks sequentially |
| `NiAVObject.Flags` | **`ushort`** (BSVER ≤ 26), not `uint` |
| `NiExtraData.Next Extra Data` | **ABSENT** (`until 4.2.2.0`) |

### 1.1 `NiNode` layout at this version

```
NiObjectNET:
    Name                  SizedString
    Num Extra Data List   u32
    Extra Data List[]     Ref × N
    Controller            Ref
NiAVObject:
    Flags                 u16          ← ushort at BSVER <= 26
    Translation           Vector3
    Rotation              Matrix33     (9 floats)
    Scale                 float
    Num Properties        u32
    Properties[]          Ref × N
    Collision Object      Ref          ← the bhk* attachment
NiNode:
    Num Children          u32
    Children[]            Ref × N
    Num Effects           u32
    Effects[]             Ref × N
```

---

## 2. Block census of real skeletons

| Block | Goblin | Rat | Mountain lion |
|---|---|---|---|
| total blocks | 264 | 314 | 265 |
| `NiNode` | 58 | 70 | 40 |
| `NiTransformController` | 57 | 69 | 39 |
| `NiStringExtraData` (UPB) | 56 | 68 | 38 |
| `bhkRigidBody` | 18 | 21 | 29 |
| `bhkBlendCollisionObject` | 18 | 21 | 29 |
| `bhkBlendController` | 18 | 21 | 29 |
| `bhkCapsuleShape` | 17 | 20 | 28 |
| `bhkSphereShape` | 1 | 1 | 1 |
| `bhkRagdollConstraint` | 6 | 13 | 16 |
| `bhkLimitedHingeConstraint` | 11 | — | 8 |
| `bhkMalleableConstraint` | — | 7 | 4 |
| `BSBound` | 1 | 1 | 1 |
| `BSXFlags` | 1 | 1 | 1 |
| `NiBSBoneLODController` | 1 | 1 | 1 |
| `NiTextKeyExtraData` | 1 | — | 1 |

Every bone gets a `NiTransformController` (the animation binding) and a `UPB`
`NiStringExtraData`. Only ragdoll bones get a rigid body.

---

## 3. The bone tree

The root chain is fixed:

```
Scene Root
└── Bip01                ← accumulation root; AccumRootName of every .kf
    └── Bip01 NonAccum   ← non-accumulating twin
        └── ...          ← the real bones
```

`Scene Root` sits at the origin with no collision. `Bip01` carries the
`BSBoneLOD#BoneRoot#` UPB and a `NiTransformController`.

### 3.1 Mountain lion — 40 nodes

```
Scene Root, Bip01, Bip01 NonAccum,
Bip01 Spine0, Bip01 Pelvis, Bip01 Spine, Bip01 Spine1, Bip01 Spine2, Bip01 Spine3,
Bip01 Neck, Bip01 Neck1, Bip01 Neck2, Bip01 Head,
Bip01 L Clavicle, Bip01 L UpperArm, Bip01 L Forearm, Bip01 L Hand,
  Bip01 L Finger0, Bip01 L Finger01,
Bip01 R Clavicle, Bip01 R UpperArm, Bip01 R Forearm, Bip01 R Hand,
  Bip01 R Finger0, Bip01 R Finger01,
Bip01 L Thigh, Bip01 L Calf, Bip01 L Foot, Bip01 L Toe0, Bip01 L Toe01,
Bip01 R Thigh, Bip01 R Calf, Bip01 R Foot, Bip01 R Toe0, Bip01 R Toe01,
Bip01 Tail, Bip01 Tail1, Bip01 Tail2, Bip01 Tail3, Bip01 Tail4
```

Note `Bip01 Spine0` is a child of `Bip01 NonAccum` and the parent of the rest —
it is the first physical bone and carries the first collision body.

### 3.2 Rat — 70 nodes

Same skeleton family, but with a 5-segment tail and full finger/toe chains:

```
Scene Root, Bip01, Bip01 NonAccum,
Bip01 Pelvis, Bip01 Spine, Bip01 Spine1, Bip01 Spine2,
Bip01 Neck, Bip01 Skull, Bip01 Ponytail1, Bip01 Ponytail2,
Bip01 L/R Clavicle → UpperArm → Forearm → Hand
  → Finger0..Finger4 each with a  ...1  child   (10 finger bones per hand)
Bip01 L/R Thigh → Calf → Foot
  → Toe0..Toe4 each with a  ...1  child          (10 toe bones per foot)
Bip01 Tail, Bip01 Tail1 .. Bip01 Tail4
```

The rat uses `Bip01 Skull` (not `Bip01 Head`) and `Bip01 Ponytail1/2` for the
snout/whiskers. The full finger and toe chains are inherited from the biped rig
even though a rat has no fingers — they are simply unused.

### 3.3 Goblin — 58 nodes, with attachment bones

```
Scene Root, Bip01, Bip01 NonAccum,
Bip01 Spine0, Bip01 Pelvis, Bip01 Spine, Bip01 Spine1, Bip01 Spine2,
Bip01 Neck, Bip01 Head,
Bip01 L Clavicle → L UpperArm → L Forearm → L Hand
  → L Finger0/01/02, Finger1/11/12, Finger2/21/22, Finger3/31/32
Torch                       ← attachment bone
Bip01 L ForearmTwist        ← shield bone
Bip01 R Clavicle → ... (mirrored)
Weapon                      ← weapon-wielding bone
Quiver                      ← arrow quiver bone
Bip01 L/R Thigh → Calf → Foot → Toe0
Bip01 Tail, Bip01 Tail1
```

### 3.4 Attachment bones

Added only when the creature needs the capability (CS wiki custom-creature
tutorial):

| Bone | Purpose |
|---|---|
| `Weapon` | weapon wielding |
| `SideWeapon` | 1H weapon sheath |
| `BackWeapon` | 2H weapon sheath |
| `Quiver` | arrow quiver |
| `Torch` | torch |
| `MagicNode` | spell-casting origin |
| `Bip01 L ForearmTwist` | shield |

The tutorial warns that `SideWeapon`, `BackWeapon` and `Quiver` place their item
based on **which bone they are parented to**, not on their own position.

---

## 4. Extra data blocks

### 4.1 `BSXFlags` — name `"BSX"`

A `u32` bitfield. **All vanilla creature skeletons use value 7.**

| Bit | Value | Name | Set on creatures |
|---|---|---|---|
| 0 | 1 | Animated | ✅ |
| 1 | 2 | Havok | ✅ |
| 2 | 4 | Ragdoll | ✅ |
| 3 | 8 | Complex | |
| 4 | 16 | Addon | |
| 5 | 32 | Editor Marker | |
| 6 | 64 | Dynamic | |
| 7 | 128 | Articulated | |
| 8 | 256 | IK Target | |
| 9 | 512 | External Emit | |

`7 = Animated | Havok | Ragdoll`. Missing `Havok`/`Ragdoll` means the creature has
no physics and no death ragdoll.

### 4.2 `BSBound` — name `"BBX"`

Culling / bounding box: a centre and **half-extents**.

```
Name     SizedString  "BBX"
Center   Vector3
Dimensions Vector3
```

| Creature | Center | Dimensions |
|---|---|---|
| Goblin | `(0, 0, 65.14)` | `(30.0, 30.0, 65.14)` |
| Rat | `(0, 0, 20.00)` | `(24.00, 56.0, 20.0)` |
| Mountain lion | `(0, 0, 30.60)` | `(16.48, 53.95, 30.68)` |

The Z centre equals the Z half-extent, i.e. the box sits on the ground plane.
The rat's Y (56.0) exceeds its X (24.0) because it is long and low.

### 4.3 `NiStringExtraData` — the `UPB` user property buffer

One per bone, name `"UPB"`, value a `\r\n`-separated `key = value` text blob.
This is where 3ds Max's user-property buffer is carried into the NIF (the
exporter's "Add User Prop Buffer" option), and it is how per-bone Havok authoring
data reaches the engine.

Goblin **root** (`Bip01`):

```
BSBoneLOD#BoneRoot#
BSPriority#20#
KFAccumRoot =
Mass = 74.000000
Ellasticity = 0.300000        ← Bethesda's spelling, not "Elasticity"
Friction = 0.300000
Unyielding = 0
Simulation_Geometry = 2
Proxy_Geometry = <None>
Use_Display_Proxy = 0
Display_Children = 1
Disable_Collisions = 0
Inactive = 0
Display_Proxy = <None>
Collision_Groups = 458760
```

Goblin **ragdoll bone**:

```
BSBoneLOD#Bone#5#
sgoKeep                       ← "do not optimize this node away"
KFAccumRoot =
Mass = 20.000000
Ellasticity = 0.500000
Friction = 0.300000
Simulation_Geometry = 5
Collision_Groups = 458760
... (rest as above)
```

Mountain lion **root** — note `Mass = 0` and `Simulation_Geometry = 2`:

```
BSBoneLOD#BoneRoot#
KFAccumRoot =
Mass = 0.000000
Ellasticity = 0.300000
Friction = 0.300000
Simulation_Geometry = 2
```

Rat root, showing `BSPriority` and empty padding lines:

```
BSBoneLOD#BoneRoot#
BSPriority#6#
(5 blank lines)
Mass = 20.000000
Friction = 0.600000
Ellasticity = 0.200000
Simulation_Geometry = 5
Collision_Groups = 458760
```

**Keys observed:**

| Key | Meaning |
|---|---|
| `BSBoneLOD#BoneRoot#` | this node is the bone-LOD root |
| `BSBoneLOD#Bone#<n>#` | bone-LOD group membership |
| `BSPriority#<n>#` | default animation priority for this bone |
| `KFAccumRoot =` | marks the KF accumulation root |
| `Mass` | rigid-body mass |
| `Ellasticity` | restitution (**sic** — two Ls) |
| `Friction` | |
| `Unyielding` | 0/1 — immovable |
| `Simulation_Geometry` | 2 = root/none, 5 = capsule |
| `Collision_Groups` | packed layer/group, `458760` throughout vanilla creatures |
| `Proxy_Geometry`, `Use_Display_Proxy`, `Display_Proxy`, `Display_Children`, `Disable_Collisions`, `Inactive` | Max-side authoring display options, not engine input |
| `sgoKeep` | Gamebryo "keep this node, do not optimize away" |

### 4.4 `NiTextKeyExtraData` on the skeleton

Some skeletons embed a default pose sequence. Goblin:

```
0.0000  'start -name Idle -loop  -at xy  -GlobalRatio 100
         -GlobalCompressFloats true -GlobalDontCompress false'
3.3333  'end '
```

These are exporter/compression directives, not gameplay events.

### 4.5 `NiBSBoneLODController`

Bethesda's bone level-of-detail controller: groups of bones that drop out at
distance. One per skeleton. Its groups pair with the `BSBoneLOD#Bone#<n>#` UPB
entries.

---

## 5. The ragdoll

The ragdoll is **inside `skeleton.nif`** — Oblivion has no separate ragdoll file
and no `.hkx` of any kind.

### 5.1 The per-bone chain

```
NiNode (bone)
  └─ Collision Object → bhkBlendCollisionObject
                          ├─ Target  → the bone NiNode
                          └─ Body    → bhkRigidBody
                                        └─ Shape → bhkCapsuleShape / bhkSphereShape
                                        └─ Constraints[] → bhkRagdollConstraint /
                                                           bhkLimitedHingeConstraint /
                                                           bhkMalleableConstraint
```

`bhkBlendCollisionObject` (rather than a plain `bhkCollisionObject`) is the key
piece: it lets a bone be **animation-driven while alive** and **physics-driven
when ragdolled**, with `bhkBlendController` supplying the blend weight. This is
Oblivion's equivalent of Skyrim's graph-driven ragdoll raise — and it is
automatic, not event-driven.

### 5.2 Shapes

`bhkCapsuleShape` (the limb primitive):

```
Unused          byte[8]
First Point     Vector3
Radius 1        float
Second Point    Vector3
Radius 2        float
```

`bhkSphereShape` inherits `bhkConvexShape` and adds no fields of its own (radius
comes from the convex-shape base). Exactly one appears per vanilla creature
skeleton — typically the head/pelvis anchor.

### 5.3 Constraints

| Constraint | DOF | Used for |
|---|---|---|
| `bhkRagdollConstraint` | 3 (cone + 2 orthogonal twist cones) | shoulders, hips, spine, tail |
| `bhkLimitedHingeConstraint` | 1, with limits and a motor | elbows, knees |
| `bhkMalleableConstraint` | wrapper | softens another constraint; does **not** affect angular limits or motors |

The mix is anatomy-driven: the goblin (biped, jointed limbs) uses 11 limited
hinges and 6 ragdoll constraints; the rat uses 13 ragdoll constraints and 7
malleable wrappers for its flexible spine and tail.

`bhkRigidBody` at this version carries `bhkRigidBodyCInfo550_660`
(`vercond="#NI_BS_LTE_FO3#"`), then `Num Constraints`, `Constraints[]`, and a
`u32 Body Flags` (`BSVER < 76`).

---

## 6. Body meshes and skinning

Body parts are ordinary NIFs in the same folder, listed by `CREA.NIFZ`, each
skinned to this skeleton via `NiTriShape`/`NiTriStrips` → `NiSkinInstance` →
`NiSkinData` + `NiSkinPartition`, whose bone list references these NiNode names.

Rules (CS wiki custom-creature tutorial):

- The skeleton must be built from **bones, not a 3ds Max Biped** — the NifTools
  exporter does not support Bipeds.
- **`Bip01` must be the first bone in the hierarchy.**
- **Every separate mesh needs its own skin instance**, not a shared one.
- Export settings for the mesh: Hidden Nodes, Flatten Hierarchy, Vertex Colors,
  Update Tangent Space, Export Skin Modifier, Enable Multiple Partitions (18,4),
  Add User Prop Buffer, Sort Nodes.
- Export settings for the skeleton: Hidden Nodes, **Skeleton Only**,
  Add User Prop Buffer, Sort Nodes, **Add Accum Nodes**.

The head is deliberately a separate NIF so NPCs look at the head rather than the
body centre — which is also why vanilla creatures show visible seams at the neck
and wrists.

Orientation: **+Y is forward** in Max (the mesh faces the top of the screen in
Top view). Oblivion animates at **30 FPS**.

To add bones for custom body parts, parent them correctly (tails to
`Bip01 Spine`, wings to `Bip01 Spine2`) and copy the flags and extra-data blocks
from the parent bone.

---

## 7. Parsing checklist

1. Parse **sequentially** — there is no block-size table.
2. All strings are inline `SizedString` (u32 length, no NUL); the BS header's
   `ExportString` is a **1-byte** length including a NUL.
3. `NiAVObject.Flags` is `u16` here, not `u32`.
4. `NiExtraData` has **no** `Next Extra Data` field.
5. `NiNode` ends with `Num Effects` + `Effects[]` — don't forget the effects array.
6. Bone hierarchy comes from `Children[]` refs, not from name prefixes.
7. `Collision Object` ref `>= 0` marks a ragdoll bone.
