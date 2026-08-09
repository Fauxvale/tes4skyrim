# The Behavior Graph

The behavior graph is a hierarchical Havok state machine that converts **events**
into **playing animations**. It is the creature's animation brain.

Field layouts below come from the **HKX2-Enhanced-Library** auto-generated class
definitions (`Pandora API/HKX2-Enhanced-Library/HKX2/Autogen/*.cs`), which encode
the exact serialized member order and the class **signature** (a 32-bit type hash
stored in the packfile). Enum semantics come from **Haviour**'s
`src/hkx/hkclass.inl`, which carries Havok's own documentation strings. Structure
was cross-checked against the vanilla wolf and draugr `.hkx` files.

Naming: `hk*` = core Havok, `hka*` = animation, `hkp*` = physics, `hkb*` = Havok
Behavior, `BS*` = Bethesda's own extension classes.

---

## 1. File roles

A creature project has up to four kinds of `.hkx`:

| Kind | Root object | Purpose |
|---|---|---|
| **Project** (`WolfProject.hkx`) | `hkbProjectData` | Stub named by the RACE record. |
| **Character** (`Wolf.hkx`) | `hkbCharacterData` | The animation list, rig/ragdoll names, foot IK setup. |
| **Behavior** (`WolfBehavior.hkx`) | `hkbBehaviorGraph` | The state machines and generators. |
| **Skeleton** (`skeleton.hkx`) | `hkaAnimationContainer` | See `skeleton_ragdoll.md`. |

Sub-graphs are ordinary behavior files pulled in by `hkbBehaviorReferenceGenerator`.

---

## 2. The character file

### hkbCharacterData — signature `0x300d6808`

Serialized member order:

| Field | Type | Meaning |
|---|---|---|
| `characterControllerInfo` | `hkbCharacterDataCharacterControllerInfo` | capsule + collision filter (below) |
| *(8 bytes padding)* | | |
| `modelUpMS` | Vector4 | model-space up axis |
| `modelForwardMS` | Vector4 | model-space forward axis |
| `modelRightMS` | Vector4 | model-space right axis |
| `characterPropertyInfos` | array of `hkbVariableInfo` | character property declarations |
| `numBonesPerLod` | array of int32 | bone counts per animation LOD |
| `characterPropertyValues` | ptr `hkbVariableValueSet` | initial values |
| `footIkDriverInfo` | ptr `hkbFootIkDriverInfo` | foot IK config (null = no foot IK) |
| `handIkDriverInfo` | ptr `hkbHandIkDriverInfo` | hand IK config |
| `stringData` | ptr `hkbCharacterStringData` | **the file lists** |
| `mirroredSkeletonInfo` | ptr `hkbMirroredSkeletonInfo` | left/right bone pairing for mirroring |
| `scale` | float | character scale |
| `numHands` | int16 | |
| `numFloatSlots` | int16 | |

`hkbCharacterDataCharacterControllerInfo` — signature `0xa0f415bf`:
`capsuleHeight` (float), `capsuleRadius` (float), `collisionFilterInfo` (uint32),
`characterControllerCinfo` (ptr). **This capsule is the creature's world collision
while alive** — distinct from the ragdoll bodies.

### hkbCharacterStringData — signature `0x655b42bc`

| Field | Type | Meaning |
|---|---|---|
| `deformableSkinNames` | array of string | skinned meshes |
| `rigidSkinNames` | array of string | rigid attachments |
| `animationNames` | array of string | |
| **`animationFilenames`** | **array of string** | **the indexed animation list** |
| `characterPropertyNames` | array of string | |
| `retargetingSkeletonMapperFilenames` | array of string | |
| `lodNames` | array of string | |
| `mirroredSyncPointSubstringsA` | array of string | e.g. `SyncLeft` |
| `mirroredSyncPointSubstringsB` | array of string | e.g. `SyncRight` |
| `name` | string | |
| `rigName` | string | path to the skeleton `.hkx` |
| `ragdollName` | string | ragdoll name |
| `behaviorFilename` | string | path to the root behavior `.hkx` |

**`animationFilenames` index = the animation ID used by `animationdata`.** This is
the linchpin. Verified for the wolf: 74 entries, sorted case-insensitively by path,
index 38 = `Animations\MT_Idle_Wolf.hkx`, matching `Main_Idle_Wolf → 38`.

The wolf's `mirroredSyncPointSubstringsA/B` are `SyncLeft`/`SyncRight`: when a clip
is mirrored, an annotation named `SyncLeft` is rewritten to `SyncRight`, keeping
gait sync correct on mirrored turns.

---

## 3. The graph root

### hkbBehaviorGraph — signature `0xb1218f86`

Public/authored fields (the rest are runtime state, serialized as null/empty):

| Field | Type | Meaning |
|---|---|---|
| `variableMode` | int8 enum | see `e_variableMode` below |
| `uniqueIdPool` | array | |
| `mirroredExternalIdMap` | array | |
| `rootGenerator` | ptr `hkbGenerator` | **the top of the tree** |
| `data` | ptr `hkbBehaviorGraphData` | variables/events declarations |

`e_variableMode`:
| value | name | meaning (Havok's own text) |
|---|---|---|
| 0 | `VARIABLE_MODE_DISCARD_WHEN_INACTIVE` | Throw away variable values and memory on `deactivate()`; reallocate and reset on each `activate()`. |
| 1 | `VARIABLE_MODE_MAINTAIN_VALUES_WHEN_INACTIVE` | Keep memory and values across deactivation (reset only the first time). |

### hkbBehaviorGraphData — signature `0x095aca5d`

| Field | Type | Meaning |
|---|---|---|
| `attributeDefaults` | array of float | |
| `variableInfos` | array of `hkbVariableInfo` | variable **types** |
| `characterPropertyInfos` | array of `hkbVariableInfo` | |
| `eventInfos` | array of `hkbEventInfo` | event **flags** |
| `wordMinVariableValues` | array of `hkbVariableValue` | per-variable min |
| `wordMaxVariableValues` | array of `hkbVariableValue` | per-variable max |
| `variableInitialValues` | ptr `hkbVariableValueSet` | |
| `stringData` | ptr `hkbBehaviorGraphStringData` | the **names** |

### hkbBehaviorGraphStringData — signature `0xc713064e`

`eventNames`, `attributeNames`, `variableNames`, `characterPropertyNames` — all
arrays of string. **Names and infos are parallel arrays**: event ID *n* is
`eventNames[n]` with flags `eventInfos[n]`; variable *n* is `variableNames[n]` with
type `variableInfos[n]` and range `wordMin/MaxVariableValues[n]`.

`hkbVariableValue` (signature `0x0b99bd6a`) is a single `int` — floats are stored
bit-reinterpreted in that int.

`hkbEventInfo` (signature `0x5874eed4`) is a single `uint flags`:
| bit | name | meaning |
|---|---|---|
| `0x1` | `FLAG_SILENT` | Whether clip generators should raise the event. |
| `0x2` | `FLAG_SYNC_POINT` | Marks the event as a sync point. |

Variable types (`e_variableTypeEnum`), index order:
`0 INVALID`, `1 BOOL`, `2 INT8`, `3 INT16`, `4 INT32`, `5 REAL`, `6 POINTER`,
`7 VECTOR3`, `8 VECTOR4`, `9 QUATERNION`.

---

## 4. Generators — nodes that produce a pose

All derive from `hkbGenerator` → `hkbNode` → `hkbBindable`.

### hkbStateMachine — signature `0x816c1dcb`

| Field | Type | Meaning |
|---|---|---|
| `eventToSendWhenStateOrTransitionChanges` | `hkbEvent` | fired on any state change |
| `startStateChooser` | ptr `hkbStateChooser` | used when `startStateMode`=CHOOSER |
| `startStateId` | int32 | initial state |
| `returnToPreviousStateEventId` | int32 | event that pops back |
| `randomTransitionEventId` | int32 | event that jumps randomly |
| `transitionToNextHigherStateEventId` | int32 | |
| `transitionToNextLowerStateEventId` | int32 | |
| `syncVariableIndex` | int32 | variable driving SYNC start mode |
| `wrapAroundStateId` | bool | higher/lower wraps at the ends |
| `maxSimultaneousTransitions` | int8 | |
| `startStateMode` | int8 enum | below |
| `selfTransitionMode` | int8 enum | below |
| `states` | array of ptr `hkbStateMachineStateInfo` | |
| `wildcardTransitions` | ptr `hkbStateMachineTransitionInfoArray` | from-any-state transitions |

`e_hkbStateMachine_StartStateMode`:
| 0 | `START_STATE_MODE_DEFAULT` | use `m_startStateId` |
| 1 | `START_STATE_MODE_SYNC` | use the variable at `m_syncVariableIndex` |
| 2 | `START_STATE_MODE_RANDOM` | random state |
| 3 | `START_STATE_MODE_CHOOSER` | use `m_startStateIdSelector` |

`e_hkbStateMachine_StateMachineSelfTransitionMode`:
| 0 | `SELF_TRANSITION_MODE_NO_TRANSITION` | stay in the current state |
| 1 | `SELF_TRANSITION_MODE_TRANSITION_TO_START_STATE` | transition to start state if one exists |
| 2 | `SELF_TRANSITION_MODE_FORCE_TRANSITION_TO_START_STATE` | transition, or change abruptly |

### hkbStateMachineStateInfo — signature `0x0ed7f9d0`

`listeners`, `enterNotifyEvents` (ptr `hkbStateMachineEventPropertyArray`),
`exitNotifyEvents`, `transitions` (ptr `hkbStateMachineTransitionInfoArray`),
`generator` (ptr `hkbGenerator` — **what this state plays**), `name` (string),
`stateId` (int32), `probability` (float), `enable` (bool).

`enterNotifyEvents`/`exitNotifyEvents` fire automatically on entry/exit. This is the
mechanism that raises `AddRagdollToWorld` when the wolf enters `AnimateToRagdoll`.

### hkbStateMachineTransitionInfo — signature `0xcdec8025`

| Field | Type | Meaning |
|---|---|---|
| `triggerInterval` | `hkbStateMachineTimeInterval` | window in which the event is accepted |
| `initiateInterval` | `hkbStateMachineTimeInterval` | window in which the transition may start |
| `transition` | ptr `hkbTransitionEffect` | the blend to use |
| `condition` | ptr `hkbCondition` | extra predicate |
| `eventId` | int32 | **the triggering event** |
| `toStateId` | int32 | destination |
| `fromNestedStateId` | int32 | |
| `toNestedStateId` | int32 | |
| `priority` | int16 | |
| `flags` | int16 | below |

`hkbStateMachineTimeInterval` (signature `0x60a881e5`): `enterEventId`,
`exitEventId` (int32), `enterTime`, `exitTime` (float).

`f_hkbStateMachine_TransitionInfo_TransitionFlags`:
| bit | name | meaning |
|---|---|---|
| `0x1` | `FLAG_USE_TRIGGER_INTERVAL` | only accept the event inside `triggerInterval` |
| `0x2` | `FLAG_USE_INITIATE_INTERVAL` | only begin inside `initiateInterval` |
| `0x4` | `FLAG_UNINTERRUPTIBLE_WHILE_PLAYING` | cannot be interrupted mid-transition |
| `0x8` | `FLAG_UNINTERRUPTIBLE_WHILE_DELAYED` | cannot be interrupted while waiting |
| `0x10` | `FLAG_DELAY_STATE_CHANGE` | change state at the END of the transition |
| `0x20` | `FLAG_DISABLED` | disabled |
| `0x40` | `FLAG_DISALLOW_RETURN_TO_PREVIOUS_STATE` | |
| `0x80` | `FLAG_DISALLOW_RANDOM_TRANSITION` | |
| `0x100` | `FLAG_DISABLE_CONDITION` | treat the condition as always true |
| `0x200` | `FLAG_ALLOW_SELF_TRANSITION_BY_TRANSITION_FROM_ANY_STATE` | wildcards may self-transition |
| `0x400` | `FLAG_IS_GLOBAL_WILDCARD` | applies regardless of active subgraph |
| `0x800` | `FLAG_IS_LOCAL_WILDCARD` | ordinary (local) wildcard |
| `0x1000` | `FLAG_FROM_NESTED_STATE_ID_IS_VALID` | |
| `0x2000` | `FLAG_TO_NESTED_STATE_ID_IS_VALID` | |
| `0x4000` | `FLAG_ABUT_AT_END_OF_FROM_GENERATOR` | delay to the end of the from-generator minus blend lead |

### hkbClipGenerator — signature `0x333b85b9`

The leaf that plays an animation.

| Field | Type | Meaning |
|---|---|---|
| `animationName` | string | path, e.g. `Animations\Death.hkx` |
| `triggers` | ptr `hkbClipTriggerArray` | in-graph annotations |
| `cropStartAmountLocalTime` | float | |
| `cropEndAmountLocalTime` | float | |
| `startTime` | float | |
| `playbackSpeed` | float | |
| `enforcedDuration` | float | |
| `userControlledTimeFraction` | float | for `MODE_USER_CONTROLLED` |
| `animationBindingIndex` | int16 | |
| `mode` | int8 enum | below |
| `flags` | int8 | below |

`e_hkbClipGenerator_PlaybackMode`:
| 0 | `MODE_SINGLE_PLAY` | play once start to finish |
| 1 | `MODE_LOOPING` | loop |
| 2 | `MODE_USER_CONTROLLED` | don't advance; the user sets local time |
| 3 | `MODE_PING_PONG` | forward, then backward, repeat |
| 4 | `MODE_COUNT` | count sentinel |

`f_hkbClipGenerator_ClipFlags`:
| bit | name | meaning |
|---|---|---|
| `0x1` | `FLAG_CONTINUE_MOTION_AT_END` | keep the end-of-clip motion after the clip ends |
| `0x2` | `FLAG_SYNC_HALF_CYCLE_IN_PING_PONG_MODE` | sync on half a ping-pong cycle |
| `0x4` | `FLAG_MIRROR` | mirror the pose about a plane |
| `0x8` | `FLAG_FORCE_DENSE_POSE` | |
| `0x10` | `FLAG_DONT_CONVERT_ANNOTATIONS_TO_TRIGGERS` | do not convert embedded annotations |
| `0x20` | `FLAG_IGNORE_MOTION` | do not extract root motion |

`hkbClipTrigger` (signature `0x7eb45cea`): `localTime` (float), `_event`
(`hkbEventProperty`), `relativeToEndOfClip` (bool), `acyclic` (bool),
`isAnnotation` (bool).

> **Two places carry annotations.** Triggers can live *in the graph*
> (`hkbClipTriggerArray`) **or** *in the animation `.hkx` itself* (embedded
> annotation tracks), and `animationdata` also lists them. `FLAG_DONT_CONVERT_
> ANNOTATIONS_TO_TRIGGERS` controls whether the embedded ones are promoted.

### hkbBlenderGenerator — signature `0x22df7147`

| Field | Type | Meaning |
|---|---|---|
| `referencePoseWeightThreshold` | float | |
| `blendParameter` | float | **the parametric blend input** (e.g. speed) |
| `minCyclicBlendParameter` | float | |
| `maxCyclicBlendParameter` | float | |
| `indexOfSyncMasterChild` | int16 | which child sets the cycle |
| `flags` | int16 | below |
| `subtractLastChild` | bool | |
| `children` | array of ptr `hkbBlenderGeneratorChild` | |

`hkbBlenderGeneratorChild` (signature `0xe2b384b0`): `generator` (ptr),
`boneWeights` (ptr `hkbBoneWeightArray`), `weight` (float),
`worldFromModelWeight` (float).

`f_hkbBlenderGenerator_BlenderFlags`:
| bit | name | meaning |
|---|---|---|
| `0x1` | `FLAG_SYNC` | adjust child speeds so cycles align |
| `0x4` | `FLAG_SMOOTH_GENERATOR_WEIGHTS` | filter weights with `w = w²(-2w+3)` |
| `0x8` | `FLAG_DONT_DEACTIVATE_CHILDREN_WITH_ZERO_WEIGHTS` | |
| `0x10` | `FLAG_PARAMETRIC_BLEND` | this is a parametric blend |
| `0x20` | `FLAG_IS_PARAMETRIC_BLEND_CYCLIC` | cyclic parametric blend |
| `0x40` | `FLAG_FORCE_DENSE_POSE` | fill missing bones from the reference pose |
| `0x80` | `FLAG_BLEND_MOTION_OF_ADDITIVE_ANIMATIONS` | |
| `0x100` | `FLAG_USE_VELOCITY_SYNCHRONIZATION` | |

Common composite values: `0` plain, `0x11` (=17) parametric+sync,
`0x31` (=49) cyclic parametric+sync. This is the walk→trot→run blend in
`ForwardLocomotion.hkx`, driven by `blendParameter` bound to `Speed`.

### hkbManualSelectorGenerator — signature `0xd932fab8`

`generators` (array of ptr), `selectedGeneratorIndex` (int8),
`currentGeneratorIndex` (int8). Picks one child by index — normally bound to a
variable, making it a variable-driven switch.

### hkbBehaviorReferenceGenerator — signature `0x0fcb5423`

`behaviorName` (string). Splices another behavior `.hkx` in at this point. The wolf
uses it for `Behaviors Wolf\QuadrupedBehavior.hkx`, `ForwardLocomotion.hkx`,
`NonCombatIdle.hkx`.

### hkbPoseMatchingGenerator — signature `0x29e271b4`

Derives from `hkbBlenderGenerator`, adding:
`worldFromModelRotation` (Quaternion), `blendSpeed`, `minSpeedToSwitch`,
`minSwitchTimeNoError`, `minSwitchTimeFullError` (floats), `startPlayingEventId`,
`startMatchingEventId` (int32), `rootBoneIndex`, `otherBoneIndex`,
`anotherBoneIndex`, `pelvisIndex` (int16), `mode` (int8).

`e_hkbPoseMatchingGenerator_Mode`: `0 MODE_MATCH` (find the best-matching pose),
`1 MODE_PLAY` (play from the matched pose).

**This is how get-up works.** It compares the ragdoll's settled pose against each
child clip using the three named bones plus the pelvis, and selects the closest —
choosing `GetUpLeft` vs `GetUpRight` automatically.

---

## 5. Transition effects

### hkbBlendingTransitionEffect — signature `0xfd8584fe`

`duration` (float), `toGeneratorStartTimeFraction` (float), `flags` (uint16),
`endMode` (int8), `blendCurve` (int8).

`f_hkbBlendingTransitionEffect_FlagBits`:
| `0x0` | `FLAG_NONE` | |
| `0x1` | `FLAG_IGNORE_FROM_WORLD_FROM_MODEL` | use only the to-generator's worldFromModel |
| `0x2` | `FLAG_SYNC` | synchronize the children's cycles |
| `0x4` | `FLAG_IGNORE_TO_WORLD_FROM_MODEL` | use only the from-generator's |
| `0x8` | `FLAG_IGNORE_TO_WORLD_FROM_MODEL_ROTATION` | blend both but ignore to-rotation |

`e_hkbBlendingTransitionEffect_EndMode`:
| 0 | `END_MODE_NONE` | |
| 1 | `END_MODE_TRANSITION_UNTIL_END_OF_FROM_GENERATOR` | ignore `duration`; run to the from-generator's end |
| 2 | `END_MODE_CAP_DURATION_AT_END_OF_FROM_GENERATOR` | shorten so the from-generator isn't overrun |

`e_hkbBlendCurveUtils_BlendCurve`:
| 0 | `BLEND_CURVE_SMOOTH` | `f(t) = -6t³ + 3t²` (smooth at both ends) |
| 1 | `BLEND_CURVE_LINEAR` | `f(t) = t` |
| 2 | `BLEND_CURVE_LINEAR_TO_SMOOTH` | `f(t) = -t³ + t² + t` |
| 3 | `BLEND_CURVE_SMOOTH_TO_LINEAR` | `f(t) = -t³ + 2t²` |

Base `hkbTransitionEffect` enums:

`e_hkbTransitionEffect_SelfTransitionMode`:
| 0 | `..._CONTINUE_IF_CYCLIC_BLEND_IF_ACYCLIC` | continue if cyclic, else blend via echo |
| 1 | `..._CONTINUE` | continue uninterrupted |
| 2 | `..._RESET` | reset to the beginning |
| 3 | `..._BLEND` | reset, blending via echo |

`e_hkbTransitionEffect_EventMode`:
| 0 | `EVENT_MODE_DEFAULT` | use `m_defaultEventMode` |
| 1 | `EVENT_MODE_PROCESS_ALL` | process both generators' events |
| 2 | `EVENT_MODE_IGNORE_FROM_GENERATOR` | |
| 3 | `EVENT_MODE_IGNORE_TO_GENERATOR` | |

---

## 6. Modifiers

`hkbModifierList` (signature `0xa4180ca1`) holds `modifiers` (array of ptr
`hkbModifier`) and runs them in order each frame. `hkbModifierGenerator` pairs a
modifier with a generator.

Havok modifiers seen in the wolf graph: `hkbKeyframeBonesModifier` (force bones to
animation), `hkbRigidBodyRagdollControlsModifier`, `hkbPoweredRagdollControlsModifier`,
`hkbGetUpModifier`, `hkbTimerModifier`, `hkbEventDrivenModifier`,
`hkbFootIkControlsModifier`, `hkbLookAtModifier`, `hkbTwistModifier`,
`hkbComputeDirectionModifier`, `hkbDampingModifier`, `hkbDelayedModifier`,
`hkbEvaluateExpressionModifier`, `hkbDetectCloseToGroundModifier`,
`hkbMoveCharacterModifier`, `hkbRotateCharacterModifier`, `hkbMirrorModifier`,
`hkbExtractRagdollPoseModifier`, `hkbSenseHandleModifier`.

**Bethesda modifiers** (`BS*`), present in the HKX2 class set:
`BSIsActiveModifier`, `BSRagdollContactListenerModifier`, `BSBoneSwitchGenerator`,
`BSSynchronizedClipGenerator`, `BSLimbIKModifier`, `BSLookAtModifier`,
`BSDirectAtModifier`, `BSDistTriggerModifier`, `BSComputeAddBoneAnimModifier`,
`BSDecomposeVectorModifier`, `BSTweenerModifier`, `BSSpeedSamplerModifier`,
`BSTimerModifier`, `BSPassByTargetTriggerModifier`, `BSInterpValueModifier`,
`BSGetTimeStepModifier`, `BSEventEveryNEventsModifier`, `BSModifyOnceModifier`,
`BSiStateTaggingGenerator`, `BSCyclicBlendTransitionGenerator`,
`BSOffsetAnimationGenerator`, `BSEventOnDeactivateModifier`,
`BSEventOnFalseToTrueModifier`.

Behaviour of the two most common:
- **`BSIsActiveModifier`** — sets a bool variable true while the node is active.
  `bInvertActive` inverts it. The wolf's `BSIsActiveModifier_AnimDriven` sets
  `bIsActive0`.
- **`BSRagdollContactListenerModifier`** — raises an event when the ragdoll
  contacts the world; used to detect that a falling body has landed.

Enums for expression/range modifiers:

`e_hkbExpressionData_ExpressionEventMode`:
| 0 | `EVENT_MODE_SEND_ONCE` | send once, the first time the expression is true |
| 1 | `EVENT_MODE_SEND_ON_TRUE` | every frame while true |
| 2 | `EVENT_MODE_SEND_ON_FALSE_TO_TRUE` | on each false→true edge |
| 3 | `EVENT_MODE_SEND_EVERY_FRAME_ONCE_TRUE` | every frame after first becoming true |

`e_hkbEventRangeData_EventRangeMode`:
| 0 | `EVENT_MODE_SEND_ON_ENTER_RANGE` | on entering the range |
| 1 | `EVENT_MODE_SEND_WHEN_IN_RANGE` | every frame while in range |

`e_hkbWorldFromModelModeData_WorldFromModelMode`:
| 0 | `WORLD_FROM_MODEL_MODE_USE_OLD` | keep the previous transform (position frozen in ragdoll) |
| 1 | `WORLD_FROM_MODEL_MODE_USE_INPUT` | use the input pose's transform |
| 2 | `WORLD_FROM_MODEL_MODE_COMPUTE` | compute by matching animation to ragdoll |
| 3 | `WORLD_FROM_MODEL_MODE_NONE` | do not set |

`e_hkbTwistModifier_RotationAxisCoordinates`:
`0 ROTATION_AXIS_IN_MODEL_COORDINATES`, `1 ..._IN_PARENT_COORDINATES`,
`2 ..._IN_LOCAL_COORDINATES`. `e_hkbTwistModifier_SetAngleMethod`: `0 LINEAR`,
`1 RAMPED`.

---

## 7. Variable binding

### hkbVariableBindingSet — signature `0x338ad4ff`

`bindings` (array of `hkbVariableBindingSetBinding`), `indexOfBindingToEnable` (int32).

`hkbVariableBindingSetBinding` — signature `0x4d592f72`:

| Field | Type | Meaning |
|---|---|---|
| `memberPath` | string | property to bind, e.g. `blendParameter` |
| `variableIndex` | int32 | index into `variableNames` |
| `bitIndex` | int8 | for binding one bit of an int (−1 = whole value) |
| `bindingType` | int8 | `0 BINDING_TYPE_VARIABLE`, `1 BINDING_TYPE_CHARACTER_PROPERTY` |

Every `hkbBindable` node may carry a `variableBindingSet`. Each frame, bound
variable values are copied **into** node properties (inputs) or **out of** them
(outputs, marked with `FLAG_OUTPUT`).

`e_hkbRoleAttribute_Role` describes what a property *means*, which is how editors
know to offer a bone/event picker:
`0 ROLE_DEFAULT`, `1 ROLE_FILE_NAME`, `2 ROLE_BONE_INDEX`, `3 ROLE_EVENT_ID`,
`4 ROLE_VARIABLE_INDEX`, `5 ROLE_ATTRIBUTE_INDEX`, `6 ROLE_TIME`, `7 ROLE_SCRIPT`,
`8 ROLE_LOCAL_FRAME`, `9 ROLE_BONE_ATTACHMENT`.

`f_hkbRoleAttribute_roleFlags`:
| bit | name | meaning |
|---|---|---|
| `0` | `FLAG_NONE` | |
| `1` | `FLAG_RAGDOLL` | the bone index refers to the **ragdoll** skeleton, not the animation skeleton |
| `2` | `FLAG_NORMALIZED` | |
| `4` | `FLAG_NOT_VARIABLE` | cannot be bound to a variable |
| `8` | `FLAG_HIDDEN` | |
| `16` | `FLAG_OUTPUT` | property is an output (value copied out) |
| `32` | `FLAG_NOT_CHARACTER_PROPERTY` | |
| `64` | `FLAG_CHAIN` | contributes to a bone chain |

---

## 8. The wolf's graph, verified

`WolfBehavior.hkx` root generator `WolfRootBehavior`, state `RootAnimState`:

```
RootAnimState
  └ Root Mod Gen (hkbModifierGenerator)
      ├ Root Mod List (hkbModifierList)
      │   ├ KeyframeLowerBody   (hkbKeyframeBonesModifier, bone array "L=33")
      │   └ DriveRagdollRB      (hkbRigidBodyRagdollControlsModifier)
      └ RootAnimBlend (hkbBlenderGenerator)
          ├ QuadrupedBFR  → hkbBehaviorReferenceGenerator
          │                   "Behaviors Wolf\QuadrupedBehavior.hkx"
          └ Wolf face offset → hkbClipGenerator
                                "Animations\Wolf_face_offset.hkx"
                                (boneWeights: wolfFaceOffsetWeightArray)

Death / ragdoll branch:
  DeathBlend → Death                       (Animations\Death.hkx)
  AnimateToRagdoll
    └ AnimateToRagdoll Mod List
        ├ KeyframeFullRagdoll
        └ CollisionListener  (BSRagdollContactListenerModifier)
  Fully Ragdoll
    └ Fully Ragdoll Mod List
        ├ FullRagdoll
        ├ PoweredRagdoll No Matching   (hkbPoweredRagdollControlsModifier)
        ├ PoweredRagdollMatching
        ├ TurnOnMatchingRagdoll
        ├ MatchAndSendGetup
        └ GetUpTimerMod   (hkbTimerModifier)
  GetUpFromRagdoll
    ├ Get Up Modifier          (hkbGetUpModifier)
    ├ BSIsActiveModifier_AnimDriven → bIsActive0
    ├ PoseMatching MSG         (hkbManualSelectorGenerator,
    │                           selectedGeneratorIndex bound)
    ├ Reanimate Pose Matcher   (hkbPoseMatchingGenerator)
    │   ├ ReanimateLeft   (Animations\GetUpLeft.hkx)
    │   └ ReanimateRight  (Animations\GetUpRight.hkx)
    └ Get Up Pose Matcher      (hkbPoseMatchingGenerator)
        ├ GetUpLeft   (Animations\GetUpLeft.hkx)
        └ GetUpRight  (Animations\GetUpRight.hkx)
```

Note the pattern: `Reanimate*` and `GetUp*` reuse the **same two clips** but through
separate pose matchers, so a reanimated (necromancy) rise and a stagger recovery can
have different blend/timing settings.

---

## 9. Wolf variables (complete, from `WolfBehavior.hkx`)

**Float**: `Speed`, `TurnDelta`, `TurnDeltaDamped`, `SpeedSampled`, `Direction`,
`fMinSpeed`, `fMinTurnDelta`, `fMinMoveSpeed`, `turnSpeedMult`, `walkBackRate`,
`staggerMagnitude`, `blendDefault`, `blendFast`, `blendSlow`, `TargetLocation`.

**Int**: `iState`, `iState_WolfDefault`, `iState_WolfRun`, `iSyncIdleLocomotion`,
`iSyncTurnState`, `iSyncForwardState`, `iCombatStance`, `iCharacterSelector`,
`iIsCanine`, `iTurnMirrored`, `iMovementSpeed`, `iGetUpType`, `staggerDirection`.

**Bool**: `bAnimationDriven`, `bAllowRotation`, `bHeadTracking`,
`bHeadTrackingOn`, `bDisableHeadTrack`, `bCanHeadTrack`, `bIsWolf`,
`bIsSynced`, `bEquipOK`, `bMirroredAttack`, `bSkeeverLunge`, `bFootIKDisable`,
`isIdleSitting`, `IsAttacking`, `IsAttackReady`, `IsBashing`, `IsRecoiling`,
`IsStaggering`, `IsBleedingOut`, `FootIKEnable`.

**Foot-IK tuning** (bound into `hkbFootIkDriverInfo`): `m_onOffGain`,
`m_groundAscendingGain`, `m_groundDescendingGain`, `m_footPlantedGain`,
`m_footRaisedGain`, `m_footUnlockGain`, `m_worldFromModelFeedbackGain`,
`m_errorUpDownBias`, `m_alignWorldFromModelGain`, `m_hipOrientationGain`,
`m_footPlantedAnkleHeightMS`, `m_footRaisedAnkleHeightMS`, `m_maxAnkleHeightMS`,
`m_minAnkleHeightMS`.

Also `wolfFaceOffsetWeightArray` (a bone weight array), `test`.

## 10. Wolf events (complete)

**Locomotion**: `moveStart`, `moveStop`, `moveForward`, `moveBackward`,
`walkStart`, `runStart`, `turnLeft`, `turnRight`, `turnStop`,
`cannedTurnLeft90`, `cannedTurnRight90`, `cannedTurnLeft180`,
`cannedTurnRight180`, `cannedTurnStop`, `cannedTurnMove`,
`cannedTurnLeft90Flee`, `cannedTurnRight90Flee`, `cannedTurnLeft180Flee`,
`cannedTurnRight180Flee`, `swimStart`, `swimStop`.

**Combat**: `attackStart_Attack1`, `attackStart_Attack2`,
`attackStart_StandingPower`, `attackStart_ForwardPower`,
`attackStart_SkeeverLungeLong`, `attackStart_SkeeverLungeShort`, `attackStop`,
`weaponSwing`, `weaponDraw`, `weaponSheathe`, `preHitFrame`, `HitFrame`,
`combatStanceStart`, `combatStanceStop`, `combatIdle1Start`,
`aggroWarningStart`, `aggroWarning2Start`, `recoilStart`, `recoilLargeStart`,
`recoilStop`, `staggerStart`, `staggerStop`.

**Death / ragdoll**: `deathStart`, `DeathAnimation`, `Ragdoll`, `RagdollInstant`,
`AddRagdollToWorld`, `AddCharacterControllerToWorld`,
`RemoveCharacterControllerFromWorld`, `GetUpStart`, `GetUpBegin`, `GetUpEnd`,
`Getup`, `Reanimated`, `bleedOutStart`, `bleedOutStop`, `bleedOutEnterOut`,
`KillActor`, `2_KillActor`, `KillMoveStart`, `2_KillMoveStart`, `2_KillMoveEnd`,
`pairedStop`, `2_pairedStop`, `NPCpairedStop`, `PairEnd`.

**Idle / misc**: `idleExit`, `PickNewIdle`, `idleWolfHowlStart`, `idleBarkRun`,
`IdleStop`, `returnToDefault`, `Intialize` *(sic — vanilla typo)*,
`StartAnimationDriven`, `StartAllowRotation`.

**Annotation-only**: `FootFront`, `FootBack`, `SoundPlay`, `SoundRelease`,
`SyncPoint`, `SyncLeft`, `SyncRight`.

Note `Intialize` is misspelled in vanilla. Event names are matched **literally** —
correcting the spelling would break the graph.

---

## 11. Papyrus access

- `Debug.SendAnimationEvent(akActor, "eventName")` — inject an event.
- `akActor.GetAnimationVariableFloat/Int/Bool("name")` — read a variable.
- `Form.RegisterForAnimationEvent` / `OnAnimationEvent` — react to events raised
  by clips.
- `PlayAnimation` / `PlayGamebryoAnimation` — for non-actor animated objects.

The CK's **Gameplay → Animations** tree lists each behavior project and, per
**Actor Action**, the conditions under which an event is sent. That tree is the
authoring surface for the ESM→event binding; `ATKE` on the RACE is its
attack-specific counterpart.
