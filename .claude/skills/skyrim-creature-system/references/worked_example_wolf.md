# Worked Example — One Wolf, End to End

Every file, field, and value below was read from vanilla Skyrim assets. This traces
a single creature completely, then follows two runtime events through the whole
stack.

---

## 1. Every file a wolf uses

```
Skyrim.esm
  RACE  WolfRace                       FormID 0001320A
  NPC_  (various: Wolf, WolfWhite, …)  → RACE WolfRace

Data\meshes\actors\canine\
  WolfProject.hkx                       ← named by RACE MNAM/FNAM MODL
  Character Assets Wolf\
    skeleton.nif                        ← named by RACE ANAM
    skeleton.hkx                        ← 72 anim bones + 22 ragdoll bodies
  Characters Wolf\
    Wolf.hkx                            ← character file: the 74-entry anim list
  Behaviors Wolf\
    WolfBehavior.hkx                    ← root graph
    QuadrupedBehavior.hkx               ← locomotion + combat + idles
    ForwardLocomotion.hkx               ← parametric walk/trot/run
    NonCombatIdle.hkx                   ← idle picker
  animations\                           ← 74 .hkx clips
    Attack1.hkx, Death.hkx, GetUpLeft.hkx, MT_Idle_Wolf.hkx, …

Data\meshes\actors\sharedkillmoves\human&wolf\
    Paired_1HMKillMoveWolfA.hkx, …      ← 6 shared paired killmoves

Data\meshes\animationdata\
  wolfproject.txt                       ← 110 clip blocks
Data\meshes\animationsetdata\wolfprojectdata\
  fullcharacter.txt                     ← 7 attacks, 72 CRC'd animations

Data\meshes\actors\canine\character assets wolf\   (body mesh via ARMO/ARMA)
  wolf.nif                              ← 2 shapes, 40 skin bones, 3154 verts
```

Note the **dog and wolf share the `canine` folder** but have separate
`behaviors`/`characters`/`character assets` subfolders (`Behaviors Wolf` vs
`behaviors`), while sharing one `animations` directory. That shared directory is why
both projects hash the same path CRC `7848002`.

---

## 2. The RACE record

```
RACE WolfRace  (0001320A)
  WNAM = 0004E886                                        → skin ARMO → body mesh
  ANAM = Actors\Canine\Character Assets Wolf\skeleton.nif    (male)
  ANAM = Actors\Canine\Character Assets Wolf\skeleton.nif    (female — same file)
  MODL = Actors\Canine\WolfProject.hkx                       (male, behavior graph)
  MODL = Actors\Canine\WolfProject.hkx                       (female)
  MTNM = WALK, RUN1, SNEK, BLDO, SWIM
  GNAM = 0004FBF5                                        → BPTD body part data
  NAM4 = 0005A28F                                        → MATT material
  NAM5 = 000A956F                                        → IPDS impact data
  VNAM = 7FFFE001                                        → equipment flags
  ATKE = attackStart_Attack1
         attackStart_Attack2
         attackStart_ForwardPower
         attackStart_ForwardPowerShort
         attackStart_StandingPower
         attackStart_SkeeverLungeShort / Medium / Long
         attackStart_AttackLeft1 / AttackLeft2
         attackStart_AttackRight1 / AttackRight2
```

The `ATKE` strings are literal behavior events. Note the RACE declares **12**
attack events while `fullcharacter.txt` maps only **7** — the extras
(`AttackLeft1`, `AttackRight2`, `SkeeverLungeMedium`, …) have no animation
mapping. Unmapped attack events are legal and simply produce no animation.

---

## 3. The character file — the animation index

`Characters Wolf\Wolf.hkx` → `hkbCharacterStringData.animationFilenames`, all 74
entries in file order (this ordering **is** the index):

```
 0 AggroWarning1              25 Dog_Spice_Sit_Head2         50 StaggerBackLarge
 1 AggroWarning2              26 Dog_Spice_Sit_Head3         51 StaggerBackSmall
 2 Attack1                    27 Dog_Spice_Sit_Head4         52 StaggerForward
 3 Attack2                    28 Dog_Spice_Sit_Idle          53 SwimForward
 4 AttackPowerForward         29 Dog_Spice_Sit_Scratch       54 TrotForward_Wolf
 5 AttackPowerForward_Large   30 Dog_Spice_Sit_Start         55 TrotForwardL_Wolf
 6 AttackPowerForward_Short   31 Dog_Spice_Sit_Stop          56 TrotForwardR_Wolf
 7 AttackPowerStanding        32 GetUpLeft                   57 TurnCannedL180
 8 CombatIdle                 33 GetUpRight                  58 TurnCannedL180Flee
 9 Death                      34 IdleCombat_BarkRun          59 TurnCannedL90
10 Dog_Idle_Spice_Head1       35 IdleCombat1                 60 TurnCannedL90Flee
11 Dog_Idle_Spice_Head2       36 IdleCombat2                 61 TurnCannedR180
12 Dog_Spice_Bark             37 MT_Idle                     62 TurnCannedR180Flee
13 Dog_Spice_Head3            38 MT_Idle_Wolf                63 TurnCannedR90
14 Dog_Spice_Head4            39 Paired_1HMKillMoveWolfA     64 TurnLoopingL
15 Dog_Spice_Lay_Idle         40 Paired_1HMKillMoveWolfB     65 TurnLoopingR
16 Dog_Spice_Lay_Start        41 Paired_2HMKillMoveWolfA     66 WalkBackward
17 Dog_Spice_Lay_Stop         42 Paired_2HMKillMoveWolfB     67 WalkForward_Wolf
18 Dog_Spice_Lay_Tail1        43 Paired_2HWKillMoveWolfA     68 WalkForwardL_Wolf
19 Dog_Spice_Lay_Tail2        44 Paired_ExtractWerewolfSpirit 69 WalkForwardR_Wolf
20 Dog_Spice_Lay_Tail3        45 Recoil                      70 Wolf_face_offset
21 Dog_Spice_Scratch          46 RunForward                  71 Wolf_Idle_Spice
22 Dog_Spice_Shake            47 RunForwardL                 72 (skeleton.HKX)
23 Dog_Spice_Sit_Bark         48 RunForwardR                 73 (WolfBehavior.hkx)
24 Dog_Spice_Sit_Head1        49 StaggerForward…
```

Entries 0–71 are animations; 72 and 73 are the skeleton and behavior file, which sit
in the same string list. Sorting is case-insensitive by path, which is why the
killmoves (paths beginning `..\SharedKillMoves\`) land at 39–44 under `P` for
`Paired_`.

**Note indices 10–31: the wolf carries the dog's "spice" idles.** The two share the
`QuadrupedBehavior` graph, so the wolf's animation list includes clips it will never
play. This is normal.

---

## 4. `animationdata` clip blocks

`meshes\animationdata\wolfproject.txt` — 110 clip blocks, all indices valid.
Representative entries:

```
Main_Idle_Wolf        →  38  MT_Idle_Wolf.hkx     speed 1     0 triggers
CombatIdle            →   8  CombatIdle.hkx       speed 1     0 triggers
TurnLoopingRight      →  65  TurnLoopingR.hkx     speed 1     4 triggers
TurnLoopingRight[Mirrored] → 64  TurnLoopingL.hkx speed 1     4 triggers
WalkSlowForward00_Wolf →  67  WalkForward_Wolf.hkx
WalkForward00_Wolf    →  67  WalkForward_Wolf.hkx  ← same clip, different speed
WalkForwardFast_Wolf  →  67  WalkForward_Wolf.hkx  ←  "
TrotForward_Wolf      →  53  TrotForward_Wolf.hkx
Death                 →   9  Death.hkx            speed 1     4 triggers
GetUpRight            →  33  GetUpRight.hkx       speed 1     8 triggers
Wolf_Idle_Spice       →  71  Wolf_Idle_Spice.hkx  speed 1     3 triggers
IdleTrap              →   0  AggroWarning1.hkx    speed 1     3 triggers
Warning2              →   0  AggroWarning1.hkx    speed 1     2 triggers
```

One animation serving many clips at different playback speeds is the core
locomotion trick: `WalkForward_Wolf.hkx` is the *only* walk clip, replayed at three
speeds and blended by `ForwardLocomotion.hkx`.

---

## 5. `animationsetdata`

`meshes\animationsetdata\wolfprojectdata\fullcharacter.txt`:

```
V3
0                       ← no cache events (a wolf never changes weapon set)
0                       ← no behavior variables
7                       ← attack entries
  attackStart_Attack1           → clip "Attack1"
  attackStart_Attack2           → clip "Attack2"
  attackStart_ForwardPower      → clip "AttackPowerForward"
  attackStart_ForwardPowerShort → clip "AttackPowerForward_Short"
  attackStart_SkeeverLungeLong  → clip "AttackPowerForward_Large"
  attackStart_SkeeverLungeShort → clip "AttackPowerForward_Short"
  attackStart_StandingPower     → clip "AttackPowerStanding"
72                      ← animation infos (CRC triples)
```

Of the 72 CRC triples: 65 hash to `meshes\actors\canine\animations` (`7848002`),
6 to `meshes\actors\sharedkillmoves\human&wolf` (`2697173992`), and one
(`turncannedl90flee`, CRC `1620581211`) is a **stale reference** — that file exists
for the cow, deer, goat, horker and sabrecat but **not** the wolf. Vanilla ships
this dangling entry and the game is fine.

Compare the **draugr**, which has 23 such files because a humanoid swaps its entire
animation set by weapon:
```
Draugr_1HMAxeMace.txt   Draugr_1HMAxeMace_Shield.txt   Draugr_1HMSword.txt
Draugr_1HMSword_LHM.txt Draugr_1HMMaceAxe_LHM.txt      Draugr_1HMSwordShield.txt
Draugr_2HM.txt          Draugr_GS.txt                  Draugr_Bow.txt
Draugr_H2H.txt          Draugr_H2H_LHM.txt             Draugr_MTSolo.txt
Combat_1HM_Taunts.txt   Combat_2HM_Taunts.txt          Combat_2GS_Taunts.txt   …
```
`Draugr_1HMAxeMace.txt` declares `iLeftHandType` range 0..0 and `iRightHandType`
range 3..4 — the selector that activates this set.

---

## 6. Skeleton and ragdoll

`Character Assets Wolf\skeleton.hkx`:

| | count | examples |
|---|---|---|
| Animation bones | **72** | `NPC Root [Root]`, `Canine_COM`, `Canine_Spine1..3`, `Canine_Tail1..3`, `Canine_LEar01`, `Canine_Tongue01..03` |
| Ragdoll bodies | **22** | `Ragdoll_Canine_COM`, `Ragdoll_Canine_Head`, `Ragdoll_Canine_Spine1/3`, `Ragdoll_Canine_Tail1/2/3`, `Ragdoll_Canine_LFrontLeg1/2/Palm` |

Classes present: `hkaAnimationContainer`, `hkaSkeleton`, `hkpRigidBody`,
`hkpConvexVerticesShape` (+ `hkpConvexVerticesConnectivity`), `hkpCapsuleShape`,
`hkpPhysicsData`, `hkpPhysicsSystem`, `hkpConstraintInstance`,
`hkpRagdollConstraintData`, `hkpLimitedHingeConstraintData`, `hkaRagdollInstance`,
`hkpPositionConstraintMotor`, `hkaSkeletonMapper`, `hkMemoryResourceContainer`.

The 50 bones with no rigid body (ears, eyes, eyelids, brows, tongue, lips, jaw,
ribcage, shoulderblades, some spine/neck links) go in the mapper's `unmappedBones`.

The wolf uses **convex hulls** for bulky parts, unlike the human skeleton which is
all capsules.

---

## 7. The body mesh

`wolf.nif` — NIF 20.2.0.7, user version 12, BS version 83 (LE), 57 blocks.

```
NiNode "wolf.nif"                    ← root, +BSInvMarker "INV"
├─ 40 bone NiNodes (Canine_Pelvis, Canine_Spine1, …)
├─ NiTriShape "WolfREDUCED"          ← the body
│   ├─ NiTriShapeData    3154 verts, 5058 tris, 1 UV set, tangents,
│   │                    vertex colors, bounds r=83.827
│   ├─ NiSkinInstance    40 bones, skeleton root = block 0
│   │   ├─ NiSkinData       per-bone bind transform + bounding sphere + weights
│   │   └─ NiSkinPartition  1 partition, 4 weights/vertex
│   ├─ BSLightingShaderProperty  flags1 0x82400303, flags2 0x06008021
│   │   └─ BSShaderTextureSet   Wolf.dds / Wolf_n.dds / Wolf_sk.dds
│   └─ NiAlphaProperty   flags 0x12EC, threshold 62  (alpha TEST, not blend)
└─ NiTriShape "Fur"                  ← shares texture set block 48
```

Shader flags 2 sets `Soft_Lighting` + `Rim_Lighting`, with the rim mask in texture
slot 2 (`Wolf_sk.dds`) — the standard fur look.

The wolf uses plain `NiSkinInstance`: **no dismemberment**. The draugr uses
`BSDismemberSkinInstance` with body parts 30/32/33.

Skin bone names (`Canine_Pelvis`, `Canine_Head`, …) match `skeleton.nif` and
`skeleton.hkx` exactly. That name match is the *only* binding — there is no path
reference between mesh and skeleton.

---

## 8. Trace 1 — the wolf bites

```
1. AI/combat decides to attack.
2. The CK Idle Animations tree (or the combat system) sends the event
   "attackStart_Attack1"  — the string comes from RACE ATKE.
3. animationsetdata fullcharacter.txt maps
       attackStart_Attack1 → clip generator "Attack1"
4. In QuadrupedBehavior.hkx, the attack state machine has a transition with
   eventId = id_of("attackStart_Attack1") → toStateId = the Attack1 state,
   with an hkbBlendingTransitionEffect (duration, blendCurve).
5. That state's generator is the hkbClipGenerator named "Attack1",
   animationName = "Animations\Attack1.hkx", mode = MODE_SINGLE_PLAY.
6. animationdata wolfproject.txt block "Attack1" supplies
       index 2 → Animations\Attack1.hkx
       playback speed, crop, and the trigger list.
7. As the clip plays, its triggers fire back into the graph:
       SoundPlay:…   → sound
       preHitFrame   → windup complete
       HitFrame      → THE DAMAGE IS APPLIED HERE
       attackStop    → return to combat idle
8. Meanwhile the anim→ragdoll skeleton mapper keeps the 22 physics bodies
   following the animated pose, so the bite has real collision.
```

**`HitFrame` is why an attack's damage timing lives in the animation cache, not in
the ESM.** Change the trigger time and you change when the wolf's bite connects.

---

## 9. Trace 2 — the wolf dies and gets up

```
1. Health reaches 0 → the engine sends "deathStart" (or "KillActor").
2. WolfBehavior.hkx wildcard transition → DeathBlend → state "Death",
   playing hkbClipGenerator "Death" (Animations\Death.hkx).
3. animationdata "Death" block, trigger list:
       FootBack:0.133333
       Ragdoll:0.267        ← at 0.267 s the clip raises "Ragdoll"
       FootFront:0.3
       FootBack:0.333333
4. "Ragdoll" transitions the graph to AnimateToRagdoll.
   That state's enterNotifyEvents raise "AddRagdollToWorld";
   its modifier list runs KeyframeFullRagdoll and
   CollisionListener (BSRagdollContactListenerModifier).
5. → Fully Ragdoll: hkbPoweredRagdollControlsModifier / FullRagdoll take over.
   The ragdoll→animation mapper now drives the visible skeleton FROM physics.
   The 22 rigid bodies fall under gravity, constrained by the ragdoll and
   limited-hinge joints. Their collision layer is DEADBIP so the corpse
   doesn't shove the player.

   ── if the wolf is revived (necromancy) or recovers ──

6. "GetUpStart" is sent.
7. GetUpFromRagdoll state:
     · hkbGetUpModifier
     · BSIsActiveModifier_AnimDriven → sets bIsActive0
     · "Get Up Pose Matcher" (hkbPoseMatchingGenerator, MODE_MATCH)
         compares the settled ragdoll pose (via rootBoneIndex, otherBoneIndex,
         anotherBoneIndex, pelvisIndex) against:
           GetUpLeft   (index 32)
           GetUpRight  (index 33)
         and selects whichever matches the side the wolf actually fell on.
8. The chosen clip plays. Its animationdata triggers (GetUpRight shown):
       FootBack:0.866667
       AddCharacterControllerToWorld:0.867  ← restore the walking capsule
       Getup:0.867
       FootFront:0.9  FootBack:1.3  FootBack:1.53333  FootFront:1.56667
       GetUpEnd:1.73333                    ← tell the state machine it's done
9. "GetUpEnd" transitions back to RootAnimState and normal locomotion resumes.
```

`Reanimate Pose Matcher` is a parallel copy of step 7 using the same two clips, so
a raised-by-necromancy wolf can use different blend timing than a stagger recovery.

---

## 10. Trace 3 — walking at variable speed

```
1. The AI package sets a movement speed; the engine writes the behavior
   variable "Speed" (float) each frame.
2. ForwardLocomotion.hkx contains an hkbBlenderGenerator with
   FLAG_PARAMETRIC_BLEND set, its blendParameter bound (via
   hkbVariableBindingSet, memberPath "blendParameter") to "Speed".
3. Its children, in increasing speed order:
       WalkForward_Wolf   (+ L/R variants)
       TrotForward_Wolf   (+ L/R variants)
       RunForward         (+ L/R variants)
4. The blender interpolates between the two bracketing children.
   FLAG_SYNC aligns their gait cycles so feet don't skate.
5. Left/right variants are blended by the "Direction" variable, giving
   turning-while-moving.
6. SpeedSampled / BSSpeedSamplerModifier smooths the input so the blend
   doesn't jitter.
7. Foot placement is corrected by hkbFootIkControlsModifier using the
   m_* foot IK variables (m_footPlantedAnkleHeightMS, m_onOffGain, …).
```

The RACE's `MTNM` list (`WALK`, `RUN1`, `SNEK`, `BLDO`, `SWIM`) plus `MTYP`/`SPED`
set the actual speed values that feed `Speed`; `iState_WolfDefault` /
`iState_WolfRun` select which movement type is active.

---

## 11. What breaks what

| Change | Result |
|---|---|
| Insert an animation mid-list in `Wolf.hkx` | every later `animationdata` index is off by one — wrong animations play |
| Delete a clip block from `animationdata` | that clip loses all triggers: silent, no footsteps, and `Death` never hands off to the ragdoll |
| Rename a bone in `skeleton.nif` but not `wolf.nif` | mesh doesn't deform / explodes |
| Ship a 32-bit LE `.hkx` to SSE | creature invisible or T-posing |
| Wrong CRC in `animationsetdata` | attack event resolves to nothing |
| Remove `HitFrame` from an attack clip | the attack plays but deals no damage |
| Miss the blank line after a clip block | the parser desyncs and every later block is misread |
