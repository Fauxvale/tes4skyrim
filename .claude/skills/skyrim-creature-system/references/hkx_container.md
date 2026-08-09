# The HKX Packfile Container

Every `.hkx` — skeleton, behavior, character, project, animation — is a Havok
**packfile**: a memory image of serialized C++ objects plus relocation tables.

Layout from **HKX2-Enhanced-Library** `HKX2/PackFileCommon.cs` (the reader/writer),
verified against the vanilla wolf `characters wolf/wolf.hkx` byte-for-byte.

---

## 1. Header (`HKXHeader`)

| Offset | Field | Type | Value in Skyrim |
|---|---|---|---|
| 0x00 | `Magic0` | uint32 | **`0x57E0E057`** |
| 0x04 | `Magic1` | uint32 | **`0x10C0C010`** |
| 0x08 | `UserTag` | int32 | 0 |
| 0x0C | `FileVersion` | int32 | **`0x08`** (Skyrim). `0x0B` = later titles |
| 0x10 | `PointerSize` | uint8 | **4 = LE (32-bit)**, **8 = SSE (64-bit)** |
| 0x11 | `Endian` | uint8 | 0 = big, **1 = little** |
| 0x12 | `PaddingOption` | uint8 | 0 or 1 |
| 0x13 | `BaseClass` | uint8 | always 1 |
| 0x14 | `SectionCount` | int32 | always **3** |
| 0x18 | `ContentsSectionIndex` | int32 | 2 (`__data__`) |
| 0x1C | `ContentsSectionOffset` | int32 | 0 |
| 0x20 | `ContentsClassNameSectionIndex` | int32 | 0 (`__classnames__`) |
| 0x24 | `ContentsClassNameSectionOffset` | int32 | `0x4B` |
| 0x28 | `ContentsVersionString` | char[16] | **`hk_2010.2.0-r1`** (NUL, then `0xFF` pad) |
| 0x38 | `Flags` | int32 | 0 |
| 0x3C | `MaxPredicate` | int16 | **-1** in Skyrim |
| 0x3E | `SectionOffset` | int16 | **-1** in Skyrim |
| 0x40 | `Unk40`, `Unk42` | int16 ×2 | *only present if `SectionOffset == 16`* |
| 0x44 | `Unk44`, `Unk48`, `Unk4C` | uint32 ×3 | *same condition* |

**If `SectionOffset != 16` the header ends at 0x40.** Skyrim uses `-1`, so the
Skyrim header is 0x40 (64) bytes.

### The LE/SSE distinction

| | Skyrim LE | Skyrim SE |
|---|---|---|
| `FileVersion` | `0x08` | `0x08` |
| **`PointerSize`** | **4** | **8** |
| `Endian` | 1 | 1 |
| `PaddingOption` | 1 | 0 |
| `ContentsVersionString` | `hk_2010.2.0-r1` | `hk_2010.2.0-r1` |

**`PointerSize` is the discriminator.** SSE will not load a 32-bit behavior or
skeleton file — every pointer and every class offset differs. This is why LE
animation `.hkx` files need conversion (`HavokBehaviorPostProcess`) before SSE use.
The reference assets in `references/Skyrim Animations` are LE (`PointerSize` 4).

Verified header bytes from the wolf character file:
```
00000000: 57e0 e057 10c0 c010 0000 0000 0800 0000
00000010: 0401 0001 0300 0000 0200 0000 0000 0000
00000020: 0000 0000 4b00 0000 686b 5f32 3031 302e   ....K...hk_2010.
00000030: 322e 302d 7231 00ff 0000 0000 ffff ffff   2.0-r1..........
```
Reading: magic pair, UserTag 0, FileVersion 8, PointerSize 4, Endian 1,
PaddingOption 0, BaseClass 1, SectionCount 3, ContentsSectionIndex 2,
ContentsSectionOffset 0, ClassNameSectionIndex 0, ClassNameSectionOffset 0x4B,
version string, Flags 0, MaxPredicate -1, SectionOffset -1.

---

## 2. Sections

Exactly three, in this order, each with a 48-byte header:

| Index | Tag | Contents |
|---|---|---|
| 0 | `__classnames__` | the class-name string pool + signatures |
| 1 | `__types__` | type metadata (empty in Skyrim files) |
| 2 | `__data__` | the actual serialized objects |

### Section header

| Offset | Field | Type |
|---|---|---|
| 0x00 | `SectionTag` | char[19] |
| 0x13 | *terminator* | uint8 = `0xFF` |
| 0x14 | `AbsoluteDataStart` | uint32 |
| 0x18 | `LocalFixupsOffset` | uint32 |
| 0x1C | `GlobalFixupsOffset` | uint32 |
| 0x20 | `VirtualFixupsOffset` | uint32 |
| 0x24 | `ExportsOffset` | uint32 |
| 0x28 | `ImportsOffset` | uint32 |
| 0x2C | `EndOffset` | uint32 |

All offsets are **relative to `AbsoluteDataStart`**, and each region runs to the
next offset:

```
AbsoluteDataStart ─┬─► [0 .. LocalFixupsOffset)      raw object data
                   ├─► [LocalFixupsOffset  .. GlobalFixupsOffset)   local fixups
                   ├─► [GlobalFixupsOffset .. VirtualFixupsOffset)  global fixups
                   ├─► [VirtualFixupsOffset .. ExportsOffset)       virtual fixups
                   ├─► [ExportsOffset .. ImportsOffset)             exports
                   └─► [ImportsOffset .. EndOffset)                 imports
```

---

## 3. Fixups (relocations)

Because a packfile is a memory image, pointers are stored as offsets and patched
at load. A fixup entry whose first uint32 is `0xFFFFFFFF` is **padding — skip it**.

### LocalFixup — 8 bytes

| Field | Type | Meaning |
|---|---|---|
| `Src` | uint32 | offset within this section holding a pointer |
| `Dst` | uint32 | offset within **this same section** it points to |

Count = `(GlobalFixupsOffset - LocalFixupsOffset) / 8`. Used for
pointers to data in the same section (array contents, inline strings).

### GlobalFixup — 12 bytes

| Field | Type | Meaning |
|---|---|---|
| `Src` | uint32 | offset holding the pointer |
| `DstSectionIndex` | uint32 | which section the target lives in |
| `Dst` | uint32 | offset within that section |

Count = `(VirtualFixupsOffset - GlobalFixupsOffset) / 12`. Used for
object-to-object references — this is how, say, an
`hkbStateMachineStateInfo.generator` points at an `hkbClipGenerator`.

### VirtualFixup — 12 bytes

Same three fields. Count =
`(ExportsOffset - VirtualFixupsOffset) / 12`. These bind an object's location to
its **class name** in `__classnames__` — the equivalent of a vtable pointer, and
what tells a reader "the object at `Src` is an `hkbClipGenerator`".

---

## 4. The class name section

`__classnames__` is a packed table of records:

```
<uint32 signature>  <uint8 0x09 separator>  <NUL-terminated class name>
```

From the wolf character file:
```
f65e 5875 09 "hkClass"
c2a4 7e5c 09 "hkClassMember"
cf09 368a 09 "hkClassEnum"
6c8a 6fce 09 "hkClassEnumItem"
1ec1 7227 09 "hkRootLevelContainer"
0868 0d30 09 "hkbCharacterData"
8d2d 8127 09 "hkbVariableValueSet"
772b 90cd 09 "hkbBoneWeightArray"
bf9d a0c6 09 "hkbFootIkDriverInfo"
bc42 5b65 09 "hkbCharacterStringData"
4fda c2c6 09 "hkbMirroredSkeletonInfo"
```

The uint32 preceding each name is that class's **signature** — the same value the
HKX2 class definitions carry as `Signature`. For example `hkbCharacterData` is
`0x300d6808`, matching the bytes `0868 0d30` read little-endian. Signatures are how
a loader validates that its compiled-in class layout matches the file's.

A quick way to inventory any `.hkx` is simply to extract printable strings — the
class-name section lists every type the file uses, and `__data__` contains all
object names and file paths.

---

## 5. The XML form

Havok also defines an XML serialization (`hkpackfile`), which is what behavior
editors read and write:

```xml
<?xml version="1.0" encoding="ascii"?>
<hkpackfile classversion="8" contentsversion="hk_2010.2.0-r1" toplevelobject="#0108">
  <hksection name="__data__">
    <hkobject name="#0060" class="hkbClipGenerator" signature="0x333b85b9">
      <hkparam name="animationName">Animations\Death.hkx</hkparam>
      ...
```

`classversion="8"` and `contentsversion="hk_2010.2.0-r1"` mirror the binary header.
Object ids are `#NNNN`; `toplevelobject` names the root
(`hkRootLevelContainer`). Conversion between the two forms is done by
`hkxcmd` (LE only) or `serde-hkx`; `HavokBehaviorPostProcess` converts LE binary
to SSE binary.

---

## 6. Reading order

To parse a packfile:

1. Read the 64-byte header; confirm the magic pair and note `PointerSize`.
2. Read three 48-byte section headers.
3. Parse `__classnames__` into `offset → (signature, name)`.
4. For `__data__`, apply **virtual fixups** to learn each object's class.
5. Apply **local** and **global** fixups to resolve every pointer.
6. Walk from `hkRootLevelContainer` through the now-resolved graph, decoding each
   object with its class's field layout (see `behavior_graph.md`,
   `skeleton_ragdoll.md`).

Field offsets in those references are for the **64-bit** layout. For a 32-bit LE
file, pointer fields are 4 bytes and all subsequent offsets shift accordingly —
which is precisely why the two builds are not interchangeable.
