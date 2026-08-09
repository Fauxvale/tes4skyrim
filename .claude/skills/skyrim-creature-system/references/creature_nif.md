# The Creature NIF

All values below were parsed byte-level from **real vanilla Skyrim LE meshes**
(`wolf.nif`, `draugrmale.nif`, and the wolf/draugr `skeleton.nif`), with every block
verified to consume exactly its declared block size. Field semantics come from
`references/nif 0.10.0.0.xml` (the niftools schema) and the **NifSkope** source.

---

## 1. Two different NIFs

A creature has two NIF files that are easy to confuse:

| | **Body mesh** (`wolf.nif`, `draugrmale.nif`) | **Armature** (`skeleton.nif`) |
|---|---|---|
| Reached via | RACE `WNAM` → `ARMO` → `ARMA` → model | RACE `ANAM` directly |
| Root block | `NiNode` | **`BSFadeNode`** |
| Has geometry | yes (`NiTriShape`) | no |
| Has `BSXFlags` | **no** | **yes** |
| Has `BSBoneLODExtraData`, `BSBound` | no | yes |
| Purpose | the visible skinned mesh | bone hierarchy + bind pose for animation/havok |

Verified across all 3,391 `.nif` files under `meshes/actors/`.

---

## 2. Header

Identical in all 3,391 creature meshes, zero exceptions:

| Field | Value |
|---|---|
| Header string | `Gamebryo File Format, Version 20.2.0.7` (newline-terminated) |
| Version | `0x14020007` (20.2.0.7) |
| Endian | `1` (little-endian) |
| **User Version** | **12** |
| **BS Version** (User Version 2) | **83** = Skyrim **LE**. SSE is **100**. |
| Author | free text, e.g. `bcarnow` (wolf), `gnoonan` (draugr) |
| Export Script | e.g. `" PE Skinned Geometry"`, `" PE Skinned Geometry (Dismem)"` |
| Num Blocks / Block Types / Block Type Index / Block Sizes | per file |
| Strings table | length-prefixed ASCII: file name, `INV`, every bone name, shape names |
| Num Groups | `0` in every file examined |

**BS 83 vs 100 is the LE/SSE discriminator.** Every creature mesh in this reference
set is LE; **zero** use the SSE-only `BSTriShape`.

---

## 3. BSXFlags

Present only on the armature `skeleton.nif` (block name `"BSX"`). Both the wolf and
draugr skeletons read **198** (`0xC6`).

| Bit | Value | Name | Meaning |
|---|---|---|---|
| 0 | 1 | `bAnimated` | enable havok / animated |
| 1 | 2 | `bHavok` | enable collision |
| 2 | 4 | `bRagdoll` | **is a skeleton nif** |
| 3 | 8 | `bComplex` | enable animation |
| 4 | 16 | `bAddon` | FlameNodes present |
| 5 | 32 | `bEditorMarker` | editor markers present |
| 6 | 64 | `bDynamic` | dynamic |
| 7 | 128 | `bArticulated` | articulated |
| 8 | 256 | `needsTransformUpdates` | |
| 9 | 512 | `bExternalEmit` | |
| 10 | 1024 | `bMagicShaderParticles` | |
| 11 | 2048 | `bLights` | |
| 12 | 4096 | `bBreakable` | |
| 13 | 8192 | `bSearchedBreakable` | runtime only |

198 = bits 1+2+6+7 = **Havok + Ragdoll + Dynamic + Articulated** — the expected
combination for a ragdoll-bearing armature.

Sources agree exactly: `nif 0.10.0.0.xml` lines 4298–4313 and NifSkope
`src/spells/flags.cpp` `bsxFlags()`.

---

## 4. Body mesh scene graph

`wolf.nif` root `NiNode` (block 0, 252 bytes, byte-exact):
- Name → `"wolf.nif"`
- Extra Data: one **`BSInvMarker`** (name `INV`, RotX=1570, RotY=0, RotZ=0,
  Zoom=1.0) — the inventory-preview camera pose.
- Flags `0x0E`, identity transform, no collision object.
- **42 children**: 40 bone `NiNode`s + 2 `NiTriShape`s (`WolfREDUCED`, `Fur`).

Bones and geometry are **flat siblings** under one root, not separate branches.

---

## 5. NiTriShape

`wolf.nif` block 42 (`WolfREDUCED`, 97 bytes, byte-exact). Inheritance is
`NiObjectNET → NiAVObject → NiGeometry → NiTriShape`. For BS > FO3 the legacy
`Num Properties`/`Properties` array is **absent**:

| Field | Type | Value |
|---|---|---|
| Name | string index | `WolfREDUCED` |
| Num Extra Data / Extra Data | uint32 + refs | 0 |
| Controller | ref | -1 |
| Flags | uint32 | `0x0E` |
| Translation / Rotation / Scale | Vec3 / Mat33 / float | identity |
| Collision Object | ref | -1 |
| **Data** | ref → `NiTriShapeData` | block 43 |
| **Skin Instance** | ref → `NiSkinInstance` | block 44 |
| Material Data | struct | NumMaterials=0, Active=-1, NeedsUpdate=0 |
| **Shader Property** | ref → `BSLightingShaderProperty` | block 47 |
| **Alpha Property** | ref → `NiAlphaProperty` | block 49 |

### NiTriShapeData (block 43, 257,484 bytes, byte-exact)

| Field | Value (wolf body) |
|---|---|
| Group ID | 0 |
| Num Vertices | 3154 |
| Keep / Compress Flags | 0 / 0 |
| Has Vertices | true → `Vector3[3154]` |
| **BS Data Flags** | `0x1001` = 1 UV set (low 6 bits) + bit 12 (tangents+bitangents present) |
| Material CRC | 0 |
| Has Normals | true → `Vector3[3154]`, then Tangents and Bitangents |
| Bounding Sphere | center (-0.221, -6.790, 39.119), radius 83.827 |
| Has Vertex Colors | true → `Color4[3154]` |
| Num UV Sets | 1 → `TexCoord[3154]` |
| Consistency Flags | `0x4000` (`CT_MUTABLE`) |
| Additional Data | -1 |
| Num Triangles | 5058 |
| Num Triangle Points | 15174 |
| Has Triangles | true → `Triangle[5058]` |
| Num Match Groups | 0 |

Low 6 bits of BS Data Flags = UV set count; bit 12 (`0x1000`) = has tangents.

---

## 6. Skinning

### NiSkinInstance vs BSDismemberSkinInstance

The wolf uses plain **`NiSkinInstance`**; the draugr uses
**`BSDismemberSkinInstance`** (a subclass adding dismemberment data).

`NiSkinInstance` (wolf block 44, 176 bytes, byte-exact):

| Field | Type | Value |
|---|---|---|
| Data | ref → `NiSkinData` | block 45 |
| Skin Partition | ref → `NiSkinPartition` | block 46 |
| **Skeleton Root** | ptr → `NiNode` | block 0 — **the mesh's own root**, not an external file |
| Num Bones | uint32 | 40 |
| Bones | ptr × N | refs to sibling `NiNode`s 2–41 |

`BSDismemberSkinInstance` appends:

| Field | Type |
|---|---|
| Num Partitions | uint32 (default 1) |
| Partitions | `BodyPartList[N]` — 4 bytes each: ushort Part Flag + ushort Body Part |

### NiSkinData (block 45, 38,365 bytes, byte-exact)

| Field | Type | Value (wolf) |
|---|---|---|
| Skin Transform | Mat33 + Vec3 + float | identity, scale 1.0 |
| Num Bones | uint32 | 40 |
| Has Vertex Weights | bool | 1 |
| Bone List | `BoneData[40]` | below |

Each `BoneData` = **Skin Transform** (Mat33 + Vec3 + float — the bone's bind-pose
offset) + **Bounding Sphere** (Vec3 center + float radius) + **Num Vertices**
(ushort) + **Vertex Weights** array of `(ushort index, float weight)`.

Real `BoneData[0]` (`Canine_Pelvis`): translation (-3.37e-06, 64.44, 3.21),
bounding sphere center (-7.27e-06, 4.14, -2.87) radius 27.23, 358 weighted
vertices.

> Bounding spheres here are per-bone. A zero radius means the bone influences
> nothing and can cause culling artifacts.

For Skyrim (`since 4.0.0.2 until 10.1.0.0` only) the legacy `Skin Partition` ref
field inside `NiSkinData` is **absent**.

### NiSkinPartition (block 46, 99,836 bytes, byte-exact)

The hardware-skinning form: geometry split so each submesh fits the GPU bone limit.

| Field | Value (wolf) |
|---|---|
| Num Partitions | 1 |
| Num Vertices | 3154 |
| Num Triangles | 5058 |
| Num Bones | 40 |
| Num Strips | 0 |
| **Num Weights Per Vertex** | **4** |
| Bones[40] | partition-local bone list — indices into `NiSkinInstance.Bones` |
| Has Vertex Map | true → `Vertex Map[3154]` (partition slot → mesh vertex) |
| Has Vertex Weights | true → `float[3154][4]`, e.g. vertex 0 = `[0.8515, 0.1485, 0, 0]` |
| Strip Lengths / Strips | empty (triangle list, not strips) |
| Has Faces | true → `Triangles[5058]` |
| Has Bone Indices | true → `byte[3154][4]`, e.g. vertex 0 = `[20, 26, 0, 0]` |
| LOD Level | 0 |
| Global VB | 0 |

`Bone Indices` are **partition-local** (into `Bones[]`), paired positionally with
`Vertex Weights`. Four weights per vertex is the engine's practical limit.

The SSE-only fields (`Data Size`, `Vertex Size`, `Vertex Desc`, `Vertex Data`,
gated on BS = 100) are absent in LE files.

---

## 7. BSLightingShaderProperty

Block 47, **exactly 100 bytes**, byte-exact.

**Layout trap:** `BSShaderProperty` inherits `NiShadeProperty → NiProperty →
NiObjectNET`. `NiProperty` does **not** inherit `NiAVObject`, so a shader property
has **no** Flags/Translation/Rotation/Scale/Collision fields — unlike `NiTriShape`.
The legacy `Shader Type`/`Shader Flags`/`EnvMapScale` fields on `BSShaderProperty`
exist only for BS ≤ 34 and are absent here.

| Field | Type | Value (wolf) |
|---|---|---|
| Shader Type | uint32 (`BSLightingShaderType`) | 0 (default) |
| Name | string index | -1 |
| Num Extra Data / Extra Data | uint32 + refs | 0 |
| Controller | ref | -1 |
| **Shader Flags 1** | uint32 | `0x82400303` |
| **Shader Flags 2** | uint32 | `0x06008021` |
| UV Offset | TexCoord | (0, 0) |
| UV Scale | TexCoord | (1, 1) |
| Texture Set | ref → `BSShaderTextureSet` | block 48 |
| Emissive Color | Color3 | (0,0,0) |
| Emissive Multiple | float | 1.0 |
| Texture Clamp Mode | uint32 | 3 (`WRAP_S_WRAP_T`) |
| Alpha | float | 1.0 |
| Refraction Strength | float | 0.0 |
| Glossiness | float | 20.0 |
| Specular Color | Color3 | (0.910, 0.808, 0.780) |
| Specular Strength | float | 0.800 |
| Lighting Effect 1 | float | 5.400 |
| Lighting Effect 2 | float | 2.800 |

Fields from `Smoothness`/`Root Material`/SF1-SF2 CRC arrays onward are gated on
BS ≥ 130 and correctly absent at BS 83.

### Shader Flags 1 (`SLSF1`)

| Bit | Name | Bit | Name | Bit | Name | Bit | Name |
|---|---|---|---|---|---|---|---|
| 0 | Specular | 8 | Recieve_Shadows | 16 | Fire_Refraction | 24 | Multiple_Textures |
| 1 | Skinned | 9 | Cast_Shadows | 17 | Eye_Environment_Mapping | 25 | Remappable_Textures |
| 2 | Temp_Refraction | 10 | Facegen_Detail_Map | 18 | Hair_Soft_Lighting | 26 | Decal |
| 3 | Vertex_Alpha | 11 | Parallax | 19 | Screendoor_Alpha_Fade | 27 | Dynamic_Decal |
| 4 | Greyscale_To_PaletteColor | 12 | Model_Space_Normals | 20 | Localmap_Hide_Secret | 28 | Parallax_Occlusion |
| 5 | Greyscale_To_PaletteAlpha | 13 | Non_Projective_Shadows | 21 | FaceGen_RGB_Tint | 29 | External_Emittance |
| 6 | Use_Falloff | 14 | Landscape | 22 | Own_Emit | 30 | Soft_Effect |
| 7 | Environment_Mapping | 15 | Refraction | 23 | Projected_UV | 31 | ZBuffer_Test |

Wolf `0x82400303` = **Specular, Skinned, Recieve_Shadows, Cast_Shadows, Own_Emit,
Remappable_Textures, ZBuffer_Test**. `Skinned` (bit 1) is mandatory on a skinned
creature mesh.

### Shader Flags 2 (`SLSF2`)

| Bit | Name | Bit | Name | Bit | Name | Bit | Name |
|---|---|---|---|---|---|---|---|
| 0 | ZBuffer_Write | 8 | Packed_Tangent | 16 | Wireframe | 24 | Multi_Layer_Parallax |
| 1 | LOD_Landscape | 9 | Multi_Index_Snow | 17 | Weapon_Blood | 25 | Soft_Lighting |
| 2 | LOD_Objects | 10 | Vertex_Lighting | 18 | Hide_On_Local_Map | 26 | Rim_Lighting |
| 3 | No_Fade | 11 | Uniform_Scale | 19 | Premult_Alpha | 27 | Back_Lighting |
| 4 | Double_Sided | 12 | Fit_Slope | 20 | Cloud_LOD | 28 | Unused02 |
| 5 | Vertex_Colors | 13 | Billboard | 21 | Anisotropic_Lighting | 29 | Tree_Anim |
| 6 | Glow_Map | 14 | No_LOD_Land_Blend | 22 | No_Transparency_Multisampling | 30 | Effect_Lighting |
| 7 | Assume_Shadowmask | 15 | EnvMap_Light_Fade | 23 | Unused01 | 31 | HD_LOD_Objects |

Wolf `0x06008021` = **ZBuffer_Write, Vertex_Colors, EnvMap_Light_Fade,
Soft_Lighting, Rim_Lighting** — the standard fur recipe for beast creatures.

The draugr's second shape uses flags1 `0x8240030B` (adds `Vertex_Alpha`), flags2
identical.

---

## 8. BSShaderTextureSet

Block 48, 132 bytes, byte-exact. `Num Textures` (uint32) then that many
length-prefixed strings. **Real files use 9 slots**, not the schema default of 6.

| Slot | Meaning |
|---|---|
| 0 | Diffuse |
| 1 | Normal / Gloss |
| 2 | Glow (if `SLSF2_Glow_Map`) / Skin / Hair / **Rim light** (if `SLSF2_Rim_Lighting`) |
| 3 | Height / Parallax |
| 4 | Environment |
| 5 | Environment Mask |
| 6 | Subsurface (multilayer parallax) |
| 7 | Back Lighting Map (if `SLSF2_Back_Lighting`) |
| 8 | *(unused in observed files)* |

Real values:
```
wolf.nif        0: textures\actors\wolf\Wolf.dds
                1: textures\actors\wolf\Wolf_n.dds
                2: textures\actors\wolf\Wolf_sk.dds     ← rim/skin mask
                3-8: empty
draugrmale.nif  0: Draugr.dds     1: Draugr_n.dds
draugr eyes     0: Draugreyes.dds 1: Draugreyes_n.dds  2: Draugreyes_g.dds  ← glow
```
The wolf sets `Rim_Lighting` and populates slot 2 with `_sk`; the draugr's eyes set
a glow map in the same slot — confirming slot 2's documented dual meaning.

**Texture sets are shared by reference**: both of `wolf.nif`'s shapes point at
block 48.

---

## 9. NiAlphaProperty

Block 49, 15 bytes, byte-exact.

| Field | Type | Value (wolf) |
|---|---|---|
| Name | string index | -1 |
| Num Extra Data / Extra Data | uint32 + refs | 0 |
| Controller | ref | -1 |
| Flags | ushort | `0x12EC` (4844) |
| Threshold | byte | 62 |

`AlphaFlags` bitfield:

| Bits | Width | Name |
|---|---|---|
| 0 | 1 | Alpha Blend |
| 1–4 | 4 | Source Blend Mode (`AlphaFunction`) |
| 5–8 | 4 | Destination Blend Mode (`AlphaFunction`) |
| 9 | 1 | Alpha Test |
| 10–12 | 3 | Test Func (`TestFunction`) |
| 13 | 1 | No Sorter |
| 14 | 1 | Clone Unique (Bethesda) |
| 15 | 1 | Editor Alpha Threshold (Bethesda) |

Wolf `4844` decodes to: Alpha Blend **false**, Source = 6 (`ALPHA_SRC_ALPHA`),
Destination = 7 (`ALPHA_INV_SRC_ALPHA`), Alpha Test **true**, Test Func = 4
(`TEST_GREATER`), No Sorter false. With `Threshold = 62` this is **alpha-test
cutout**, not blending — the standard fur-card setup.

---

## 10. How the mesh finds the skeleton

**There is no file-path link inside the NIF.** The binding is by **exact bone
name**:

- `NiSkinInstance.Skeleton Root` points at the **mesh's own root `NiNode`**, not an
  external file.
- Each entry of `NiSkinInstance.Bones` is a pointer to a sibling `NiNode` **inside
  the body mesh**, carrying that bone's bind pose.
- Those `NiNode` names must match names in the separate `skeleton.nif` (and in
  `skeleton.hkx`) **character for character**.

Verified: `wolf.nif`'s skin bones are `Canine_Pelvis`, `Canine_Spine1`,
`Canine_Head`, `Canine_LFrontLeg1`, … — all present verbatim in the wolf
`skeleton.nif`. `draugrmale.nif` uses the humanoid tag convention
`NPC Pelvis [Pelv]`, `NPC Spine [Spn0]`, `NPC Head [Head]`, `NPC R Hand [RHnd]`,
likewise all present in the draugr `skeleton.nif`.

Because the mesh embeds its own copy of every referenced bone, it renders
standalone; the external skeleton supplies the animated pose at runtime.

---

## 11. Dismemberment

`BSPartFlag` (ushort):

| Bit | Name |
|---|---|
| 0 | `PF_EDITOR_VISIBLE` |
| 8 | `PF_START_NET_BONESET` — starts a new shared bone set |

Real draugr data (each block byte-exact):

| Shape | Num Partitions | (Part Flag, Body Part) |
|---|---|---|
| block 65 | 1 | (257, 32) |
| block 73 | 1 | (257, 32) |
| main body (block 80) | 3 | (257, 33), (257, 32), (**1**, 30) |
| block 87 | 1 | (257, 32) |

`257` = `0x101` = both flags set (the documented default). The `1` entry omits
`PF_START_NET_BONESET`, so that partition continues sharing the previous bone set.

`BSDismemberBodyPartType` values used:

| Value | Name | Documented meaning |
|---|---|---|
| 30 | `SBP_30_HEAD` | Head (human), Body (atronachs, beasts), Mask (dragon priest) |
| 32 | `SBP_32_BODY` | Main body, extras (spriggan) |
| 33 | `SBP_33_HANDS` | Hands L/R, BodyToo (dragon priest), **Legs (Draugr)**, Arms (giant) |

**The enum is deliberately overloaded per creature.** Value 33 means *legs* on a
draugr, not hands — the schema documents this explicitly. Similarly value 40
(`SBP_40_TAIL`) means tail on Argonians/Khajiit but "Skeleton01" on a dragon,
"Spit" on chaurus/spiders, and so on.

`NiSkinPartition.Num Partitions` always equals `BSDismemberSkinInstance.Num
Partitions` on the same shape (verified: both 3 for the draugr main body). The two
lists are **positionally parallel** — partition *i* of the skin partition is tagged
with body part *i* from the dismember instance, which is how the engine hides
geometry and spawns gore caps on dismemberment.

The wolf has **no** dismemberment (plain `NiSkinInstance`) — it is opt-in per shape
by choosing the skin-instance subclass.

---

## 12. Creature-specific blocks

- **`BSInvMarker`** — root extra data, name `INV`, fields RotX/RotY/RotZ (ushort) +
  Zoom (float). Sets the inventory preview camera.
- **`BSFadeNode`** — root type of armature files; supports distance fade.
- **`BSBoneLODExtraData`** — bone LOD distance table; armature files only.
- **`BSBound`** — culling/collision bounding box; armature files only.

---

## 13. Unverified

- `BSDismemberBodyPartType` meanings for creatures beyond wolf/draugr (dragon,
  chaurus, spider) are taken from the schema's inline documentation and were not
  re-derived from those meshes.
- The full bone-list name resolution for `draugrmale.nif`'s main shape was confirmed
  for the first 21 of 61 entries; the remainder follow the same pointer pattern.
