# The Skeleton and Ragdoll (`skeleton.hkx`)

One file holds **two skeletons**, the **physics bodies**, the **constraints**, and
the **mappers** that tie animation and physics together.

Field layouts from **HKX2-Enhanced-Library** `Autogen/*.cs` (exact serialized member
order, byte offsets for the 64-bit/SSE layout, and the class signature). Structure
verified byte-level against the vanilla wolf
(`meshes\actors\canine\character assets wolf\skeleton.hkx`) and human
(`meshes\actors\character\character assets\skeleton.hkx`) files.

---

## 1. File structure

Verified class inventory of the **wolf** `skeleton.hkx` (227 unique strings):

```
hkRootLevelContainer
├─ "Merged Animation Container"  hkaAnimationContainer
│     skeletons[0]  hkaSkeleton   ← ANIMATION skeleton, 72 bones
│     skeletons[1]  hkaSkeleton   ← RAGDOLL skeleton,   22 bones
├─ "Resource Data"     hkMemoryResourceContainer → hkMemoryResourceHandle
├─ "Physics Data"      hkpPhysicsData → hkpPhysicsSystem
│     ├─ hkpRigidBody[22]
│     │     shapes: hkpCapsuleShape, hkpConvexVerticesShape
│     │             (+ hkpConvexVerticesConnectivity, hkpShapeInfo)
│     └─ hkpConstraintInstance[]
│           hkpRagdollConstraintData | hkpLimitedHingeConstraintData
│           (+ hkpPositionConstraintMotor)
├─ "RagdollInstance"   hkaRagdollInstance
└─ "SkeletonMapper"    hkaSkeletonMapper ×2
```

The human skeleton is identical in shape, differing only in bone counts and in using
`hkpCapsuleShape` throughout (no convex hulls).

**No `BS*` (Bethesda) classes appear in a vanilla `skeleton.hkx`** — every `BS*`
class belongs to behavior files. Verified by string dump of both files.

---

## 2. hkaAnimationContainer — signature `0x8dc20333`

| Field | Type | Offset |
|---|---|---|
| `skeletons` | array of ptr `hkaSkeleton` | 16 |
| `animations` | array of ptr `hkaAnimation` | 32 |
| `bindings` | array of ptr `hkaAnimationBinding` | 48 |
| `attachments` | array of ptr `hkaBoneAttachment` | 64 |
| `skins` | array of ptr `hkaMeshBinding` | 80 |

In a `skeleton.hkx` only `skeletons` is populated (two entries); the rest are empty.
In an **animation** `.hkx`, `animations` and `bindings` carry the motion data.

---

## 3. hkaSkeleton — signature `0x366e8220`

| Field | Type | Offset | Meaning |
|---|---|---|---|
| `name` | string | 16 | e.g. `NPC Root [Root]` |
| `parentIndices` | array of int16 | 24 | parent index per bone; **−1 = root** |
| `bones` | array of `hkaBone` | 40 | parallel to `parentIndices` |
| `referencePose` | array of QsTransform | 56 | bind pose, **local to parent** |
| `referenceFloats` | array of float | 72 | values for `floatSlots` |
| `floatSlots` | array of string | 88 | named float channels |
| `localFrames` | array of `hkaSkeletonLocalFrameOnBone` | 104 | optional attached frames |

`hkaBone` — signature `0x35912f8a`, 16 bytes: `name` (string, offset 0),
`lockTranslation` (bool, offset 8). That is the entire struct.

`hkaSkeletonLocalFrameOnBone` — signature `0x052e8043`:
`localFrame` (ptr `hkLocalFrame`), `boneIndex` (int32).

**The three arrays `parentIndices`, `bones`, `referencePose` are strictly parallel**
and must always have equal length. Adding a bone means appending to all three.

A **QsTransform** is translation (Vector4) + rotation (Quaternion) + scale
(Vector4) — 48 bytes. In XML it prints as `(x y z)(qx qy qz qw)(sx sy sz)`.

### Bone naming convention

Vanilla bone names embed a 4-character accessor tag in brackets, space-padded:

```
NPC Root [Root]
x_NPC LookNode [Look]
x_NPC Translate [Pos ]
x_NPC Rotate [Rot ]
NPC COM [COM ]
NPC Pelvis [Pelv]
NPC L Thigh [LThg]
NPC L Calf [LClf]
NPC L Foot [Lft ]
```

The padding inside `[COM ]`, `[Pos ]`, `[Lft ]` is significant — it is part of the
literal string. Names are matched exactly.

**Quadrupeds use a species prefix and mostly drop the tags.** The wolf's 72
animation bones:
```
NPC Root [Root]          ← the skeleton's name / root bone
Canine_COM
Canine_Pelvis
Canine_Spine1  Canine_Spine2  Canine_Spine3  Canine_Ribcage
Canine_Neck1   Canine_Neck2   Canine_Head    Canine_JawBone
Canine_LFrontLegShoulderblade / RFrontLegShoulderblade
Canine_LFrontLeg1 / 2 / Palm / Toe   (and R…)
Canine_LBackLeg1 / 2 / Palm / Toe    (and R…)
Canine_Tail1 / Tail2 / Tail3
Canine_LEar_Wolf / REar_Wolf / LEar01 / LEar02 / REar01 / REar02
Canine_LEye / REye / Dog_LEyelid / Dog_REyelid / Dog_LBrow …
Canine_FrontLip / LUpperLip / RUpperLip
Canine_Tongue01 / 02 / 03
```

> **A wolf skeleton contains exactly one root bone.** The string `NPC Root [Root]`
> present in the wolf file is the skeleton name / root bone, **not** evidence of a
> second human-style root. Reports of "two roots" in Skyrim skeletons conflate the
> `hkaSkeleton.name` field with a bone entry — verify against `parentIndices`
> (count the −1 entries) before believing any such claim.

---

## 4. The ragdoll

### hkaRagdollInstance — signature `0x154948e8`

| Field | Type | Offset | Meaning |
|---|---|---|---|
| `rigidBodies` | array of ptr `hkpRigidBody` | 16 | one per ragdoll bone |
| `constraints` | array of ptr `hkpConstraintInstance` | 32 | joints between them |
| `boneToRigidBodyMap` | array of int32 | 48 | ragdoll bone → rigid body |
| `skeleton` | ptr `hkaSkeleton` | 64 | **the ragdoll skeleton** |

The wolf has **22 ragdoll bodies** against **72 animation bones** — the ragdoll is a
coarse approximation. Bodies are named `Ragdoll_<animation bone name>`:
```
Ragdoll_Canine_COM        Ragdoll_Canine_Head       Ragdoll_Canine_Spine1
Ragdoll_Canine_Spine3     Ragdoll_Canine_Neck2      Ragdoll_Canine_Tail1/2/3
Ragdoll_Canine_LFrontLeg1/2   Ragdoll_Canine_LFrontLegPalm
Ragdoll_Canine_RFrontLeg1/2   Ragdoll_Canine_RFrontLegPalm
Ragdoll_Canine_LBackLeg1/2    Ragdoll_Canine_LBackLegPalm/Toe
Ragdoll_Canine_RBackLeg1/2    Ragdoll_Canine_RBackLegPalm/Toe
```
Fine bones (ears, eyes, tongue, lips, eyelids, shoulderblades, ribcage, jaw) have no
physics body — they follow their parent.

### hkpRigidBody — signature `0x75f8d805`

Adds no fields of its own. Everything lives in the base chain
`hkpRigidBody → hkpEntity → hkpWorldObject → hkReferencedObject`.

**hkpWorldObject** (signature `0x49fb6f2e`) — authored fields:
| Field | Type | Offset |
|---|---|---|
| `userData` | uint64 | 24 |
| `collidable` | `hkpLinkedCollidable` | 32 |
| `name` | string | 176 |
| `properties` | array of `hkpProperty` | 184 |

**hkpEntity** (signature `0xa03c774b`) — authored fields:
| Field | Type | Offset | Meaning |
|---|---|---|---|
| `material` | `hkpMaterial` | 208 | friction, restitution |
| `damageMultiplier` | float | 232 | |
| `storageIndex` | uint16 | 252 | |
| `contactPointCallbackDelay` | uint16 | 254 | |
| `autoRemoveLevel` | int8 | 312 | |
| `numShapeKeysInContactPointProperties` | uint8 | 313 | |
| `responseModifierFlags` | uint8 | 314 | |
| `uid` | uint32 | 316 | |
| `motion` | `hkpMaxSizeMotion` (inline) | 336 | **the physical state** |
| `localFrame` | ptr `hkLocalFrame` | 688 | |
| `npData` | uint32 | 704 | |

**hkpMotion** (signature `0x98aadb4f`) — the fields inside `motion`:
| Field | Type | Offset |
|---|---|---|
| `type` | `MotionType` (uint8) | 16 |
| `deactivationIntegrateCounter` | uint8 | 17 |
| `deactivationNumInactiveFrames` | uint16[2] | 18 |
| `motionState` | `hkMotionState` | 32 |
| `inertiaAndMassInv` | Vector4 | 208 |
| `linearVelocity` | Vector4 | 224 |
| `angularVelocity` | Vector4 | 240 |
| `deactivationRefPosition` | Vector4[2] | 256 |
| `deactivationRefOrientation` | uint32[2] | 288 |
| `savedMotion` | ptr `hkpMaxSizeMotion` | 296 |
| `savedQualityTypeIndex` | uint16 | 304 |
| `gravityFactor` | half | 306 |

`MotionType`: `0 INVALID`, `1 DYNAMIC`, `2 SPHERE_INERTIA`, `3 BOX_INERTIA`,
`4 KEYFRAMED`, `5 FIXED`, `6 THIN_BOX_INERTIA`, `7 CHARACTER`, `8 MAX_ID`.

`inertiaAndMassInv.w` is **inverse** mass — `0` means infinite mass (immovable).

`hkMotionState` (signature `0x5797386e`): `transform` (Matrix4x4, offset 0),
`sweptTransform` (offset 64), `deltaAngle` (Vector4, 144), `objectRadius`
(float, 160), `linearDamping` (half, 164), `angularDamping` (half, 166),
`timeFactor` (half, 168), `maxLinearVelocity` (uint8, 170),
`maxAngularVelocity` (uint8, 171), `deactivationClass` (uint8, 172).

> `hkpRigidBodyCinfo` is **not** a serialized class — it is a construction-info
> struct used only in memory. It never appears in a packfile.

---

## 5. Shapes

Hierarchy: `hkpShape → hkpSphereRepShape → hkpConvexShape → {Capsule, Box, Sphere,
ConvexVertices}`.

`hkpShape` (signature `0x666490a1`): `userData` (uint64, offset 16), `type`
(uint32, 24, not serialized).

`hkpConvexShape` (signature `0xf8f74f85`): `radius` (float, offset 32) — the convex
margin, inherited by all below.

| Class | Signature | Extra fields |
|---|---|---|
| `hkpCapsuleShape` | `0xdd0b1fd3` | `vertexA` (Vector4, 48), `vertexB` (Vector4, 64) — a segment thickened by `radius` |
| `hkpBoxShape` | `0x3444d2d5` | `halfExtents` (Vector4, 48) |
| `hkpSphereShape` | `0x0795d9fa` | none (just the inherited `radius`) |
| `hkpConvexVerticesShape` | `0x28726ad8` | `aabbHalfExtents` (Vector4, 48), `aabbCenter` (Vector4, 64), `rotatedVertices` (array of `hkpConvexVerticesShapeFourVectors`, 80), `numVertices` (int32, 96), `planeEquations` (array of Vector4, 120), `connectivity` (ptr, 136) |

`hkpConvexVerticesShapeFourVectors` packs 4 vertices SoA-style: `x`, `y`, `z` each a
Vector4 holding that coordinate for four vertices (SIMD layout).

The wolf uses capsules for limbs/tail/neck and convex hulls for the bulkier
head/torso; the human skeleton uses capsules exclusively.

---

## 6. Collision filtering

The collision layer is **not** on the shape or the entity — it is inside
`collidable`:

`hkpRigidBody.collidable` → `hkpLinkedCollidable` → **`hkpCollidable`** (signature
`0x9a0e42a5`) → `broadPhaseHandle` (offset 36) →
**`hkpTypedBroadPhaseHandle`** (signature `0xf4b0f799`):

| Field | Type | Offset |
|---|---|---|
| `type` | int8 | 4 |
| `objectQualityType` | int8 | 6 |
| **`collisionFilterInfo`** | **uint32** | **8** |

`hkpCollidable` also carries `allowedPenetrationDepth` (float, offset 104), and
`hkpCdBody` supplies `shape` (ptr, 0) and `shapeKey` (uint32, 8).

The **low byte** of `collisionFilterInfo` is the Skyrim collision **layer**. The
enumeration (from ck-cmd's `SkyrimLayer`), in index order:

```
0  UNIDENTIFIED     1  STATIC          2  ANIMSTATIC      3  TRANSPARENT
4  CLUTTER          5  WEAPON          6  PROJECTILE      7  NONCOLLIDABLE
8  BIPED            9  TREES          10  PROPS          11  WATER
12  TRIGGER        13  TERRAIN        14  TRAP           15  CLOUD_TRAP
16  GROUND         17  PORTAL         18  DEBRIS_SMALL   19  DEBRIS_LARGE
20  ACOUSTIC_SPACE 21  ACTORZONE      22  PROJECTILEZONE 23  GASTRAP
24  SHELLCASING    25  TRANSPARENT_SMALL              26  INVISIBLE_WALL
27  TRANSPARENT_SMALL_ANIM           28  WARD          29  CHARCONTROLLER
30  STAIRHELPER    31  DEADBIP        32  BIPED_NO_CC   33  AVOIDBOX
34  COLLISIONBOX   35  CAMERASHPERE   36  DOORDETECTION 37  CONEPROJECTILE
38  CAMERAPICK     39  ITEMPICK       40  LINEOFSIGHT   41  PATHPICK
42  CUSTOMPICK1    43  CUSTOMPICK2    44  SPELLEXPLOSION
45  DROPPINGPICK   46  NULL
```

For creature ragdolls the relevant layers are **`BIPED` (8)** for a living actor,
**`DEADBIP` (31)** once it is a corpse ragdoll (so bodies stop shoving the player),
**`BIPED_NO_CC` (32)** for biped collision that ignores the character controller,
and **`CHARCONTROLLER` (29)** for the live capsule from
`hkbCharacterDataCharacterControllerInfo`.

The remaining bytes carry a **system group** id so a creature's own limbs do not
collide with each other, resolved through `hkpGroupCollisionFilter`
(signature `0x5cc01561`: `noGroupCollisionEnabled` bool at 72,
`collisionGroups` uint32[32] at 76). **[The exact bit partitioning of the upper 24
bits in Skyrim's filter word is UNVERIFIED from the sources used here.]**

---

## 7. Constraints

`hkpConstraintInstance` — signature `0x034eba5f`:

| Field | Type | Offset |
|---|---|---|
| `data` | ptr `hkpConstraintData` | 24 |
| `constraintModifiers` | ptr `hkpModifierConstraintAtom` | 32 |
| `entities` | ptr `hkpEntity`[2] | 40 | the two bodies joined |
| `priority` | `ConstraintPriority` (uint8) | 56 |
| `wantRuntime` | bool | 57 |
| `destructionRemapInfo` | uint8 | 58 |
| `name` | string | 80 |
| `userData` | uint64 | 88 |

`hkpConstraintData` base (signature `0x80559a4e`): `userData` (uint64, offset 16).

Skyrim creature ragdolls mix two constraint types (both present in the wolf and the
human file):

### hkpRagdollConstraintData — signature `0x8fb5dd29`

A 3-DOF ball-socket with cone/twist/plane limits and optional motors. `atoms`
(`hkpRagdollConstraintDataAtoms`, signature `0xeed76b00`, offset 32):

| Atom | Type | Offset |
|---|---|---|
| `transforms` | `hkpSetLocalTransformsConstraintAtom` | 0 |
| `setupStabilization` | `hkpSetupStabilizationAtom` | 144 |
| `ragdollMotors` | `hkpRagdollMotorConstraintAtom` | 160 |
| `angFriction` | `hkpAngFrictionConstraintAtom` | 256 |
| `twistLimit` | `hkpTwistLimitConstraintAtom` | 268 |
| `coneLimit` | `hkpConeLimitConstraintAtom` | 288 |
| `planesLimit` | `hkpConeLimitConstraintAtom` | 308 |
| `ballSocket` | `hkpBallSocketConstraintAtom` | 328 |

### hkpLimitedHingeConstraintData — signature `0x7c15bb6b`

A 1-DOF hinge with a sweep limit — used for elbows, knees, jaws. `atoms`
(`hkpLimitedHingeConstraintDataAtoms`, signature `0x54c7715b`, offset 32):

| Atom | Type | Offset |
|---|---|---|
| `transforms` | `hkpSetLocalTransformsConstraintAtom` | 0 |
| `setupStabilization` | `hkpSetupStabilizationAtom` | 144 |
| `angMotor` | `hkpAngMotorConstraintAtom` | 160 |
| `angFriction` | `hkpAngFrictionConstraintAtom` | 184 |
| `angLimit` | `hkpAngLimitConstraintAtom` | 196 |
| `_2dAng` | `hkp_2dAngConstraintAtom` | 212 |
| `ballSocket` | `hkpBallSocketConstraintAtom` | 216 |

### Atom fields

All atoms derive from `hkpConstraintAtom` (2 bytes): `type` (`AtomType`, uint16, 0).

| Atom | Fields (offset) |
|---|---|
| `hkpSetLocalTransformsConstraintAtom` | `transformA` (Matrix4x4, 16), `transformB` (Matrix4x4, 80) — the pivot + axis frames in each body's space |
| `hkpSetupStabilizationAtom` | `enabled` (bool, 2), `maxAngle` (float, 4) |
| `hkpRagdollMotorConstraintAtom` | `isEnabled` (bool, 2), `initializedOffset` (int16, 4), `previousTargetAnglesOffset` (int16, 6), `target_bRca` (Matrix3, 16), `motors` (ptr `hkpConstraintMotor`[3], 64) |
| `hkpAngFrictionConstraintAtom` | `isEnabled` (uint8, 2), `firstFrictionAxis` (uint8, 3), `numFrictionAxes` (uint8, 4), `maxFrictionTorque` (float, 8) |
| `hkpTwistLimitConstraintAtom` | `isEnabled` (uint8, 2), `twistAxis` (uint8, 3), `refAxis` (uint8, 4), `minAngle` (float, 8), `maxAngle` (float, 12), `angularLimitsTauFactor` (float, 16) |
| `hkpConeLimitConstraintAtom` | `isEnabled` (uint8, 2), `twistAxisInA` (uint8, 3), `refAxisInB` (uint8, 4), `angleMeasurementMode` (uint8, 5), `memOffsetToAngleOffset` (uint8, 6), `minAngle` (float, 8), `maxAngle` (float, 12), `angularLimitsTauFactor` (float, 16) |
| `hkpBallSocketConstraintAtom` | `solvingMethod` (uint8, 2), `bodiesToNotify` (uint8, 3), `velocityStabilizationFactor` (uint8, 4), `maxImpulse` (float, 8), `inertiaStabilizationFactor` (float, 12) |
| `hkpAngMotorConstraintAtom` | `isEnabled` (bool, 2), `motorAxis` (uint8, 3), `initializedOffset` (int16, 4), `previousTargetAngleOffset` (int16, 6), `correspondingAngLimitSolverResultOffset` (int16, 8), `targetAngle` (float, 12), `motor` (ptr, 16) |
| `hkpAngLimitConstraintAtom` | `isEnabled` (uint8, 2), `limitAxis` (uint8, 3), `minAngle` (float, 4), `maxAngle` (float, 8), `angularLimitsTauFactor` (float, 12) |

`hkpPositionConstraintMotor` appears in both vanilla files — it is the motor that
lets a *powered* ragdoll drive toward an animated target pose (used by
`hkbPoweredRagdollControlsModifier`).

---

## 8. The skeleton mappers

### hkaSkeletonMapper — signature `0x12df42a5`

Single field: `mapping` (`hkaSkeletonMapperData`, inline at offset 16).

### hkaSkeletonMapperData — signature `0x95687ea0`

| Field | Type | Offset |
|---|---|---|
| `skeletonA` | ptr `hkaSkeleton` | 0 |
| `skeletonB` | ptr `hkaSkeleton` | 8 |
| `simpleMappings` | array of `hkaSkeletonMapperDataSimpleMapping` | 16 |
| `chainMappings` | array of `hkaSkeletonMapperDataChainMapping` | 32 |
| `unmappedBones` | array of int16 | 48 |
| `extractedMotionMapping` | QsTransform | 64 |
| `keepUnmappedLocal` | bool | 112 |
| `mappingType` | `MappingType` (int32) | 116 |

`MappingType`: `0 HK_RAGDOLL_MAPPING`, `1 HK_RETARGETING_MAPPING`. Skyrim's
animation↔ragdoll mappers always use `HK_RAGDOLL_MAPPING`.

> There is **no `mappingPartitionRanges` field** in this class version. Confirmed
> absent from the entire HKX2 Autogen set. It exists in some later Havok SDKs'
> runtime structure but is not part of the Skyrim serialized layout.

`hkaSkeletonMapperDataSimpleMapping` — signature `0x3405deca`:
`boneA` (int16, 0), `boneB` (int16, 2), `aFromBTransform` (QsTransform, 16).

`hkaSkeletonMapperDataChainMapping` — signature `0xa528f7cf`:
`startBoneA` (int16, 0), `endBoneA` (int16, 2), `startBoneB` (int16, 4),
`endBoneB` (int16, 6), `startAFromBTransform` (QsTransform, 16),
`endAFromBTransform` (QsTransform, 64).

**Vanilla Skyrim ragdoll mappers use only `simpleMappings`.** Chain mappings are a
retargeting feature for differing-topology skeletons and are unused here.

### The two mappers, and what each is for

A `skeleton.hkx` carries **two** `hkaSkeletonMapper` objects (both literally named
`SkeletonMapper`):

1. **Animation → Ragdoll.** Drives the physics bodies from the currently playing
   animation. This is what makes a *living* creature's collision follow its
   animation, and what `hkbKeyframeBonesModifier` / `hkbRigidBodyRagdollControls
   Modifier` rely on.
2. **Ragdoll → Animation.** Drives the visible skeleton from physics results. This
   powers death ragdolls, and supplies the settled pose that
   `hkbPoseMatchingGenerator` reads to choose `GetUpLeft` vs `GetUpRight`.

Each `simpleMappings[i]` ties ragdoll bone index to animation bone index with a
fixed relative transform `aFromBTransform`. Animation bones with no physics body
(the wolf's ears, eyes, tongue, lips, jaw, ribcage, shoulderblades — 50 of the 72)
are listed in `unmappedBones`; `keepUnmappedLocal` decides whether they keep their
local animated transform when physics drives the rest.

---

## 9. `hkbBoneWeightArray`

Signature `0xcd902b77`, base `hkbBindable`. Single field `boneWeights` (array of
float, offset 48). Despite touching bones this is a **behavior** class, not a
skeleton one — it lives in behavior files and weights per-bone influence for blends
and IK. The wolf's `wolfFaceOffsetWeightArray` is one, restricting the
`Wolf_face_offset` clip to the face bones.

**`BSBoneLOD` does not exist** in the HKX2 class set; bone LOD data for creatures is
carried in the NIF (`NiSkinPartition`), and per-LOD bone counts in
`hkbCharacterData.numBonesPerLod`.
