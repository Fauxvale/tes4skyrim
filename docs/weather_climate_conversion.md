# Weather / Climate Conversion (WTHR, CLMT, sky meshes)

> **Status (2026-07-26): WTHR conversion is NOT on `master`** — it lives on a
> separate, unmerged branch. `constants.py` on `master` carries a `convert_WTHR`
> dispatch entry, but that alone does not make the feature live, so do not cite it
> as evidence weather is converted. CLMT *is* converted on `master`. Everything
> below is the format/semantics reference and is valid on either branch.

Sources of truth for everything here: UESP `Skyrim Mod:Mod File Format/WTHR`
and `.../CLMT`, xEdit `wbDefinitionsCommon.pas` (`wbWeather*`) and
`wbDefinitionsTES4/TES5.pas`, a census of the **84 vanilla WTHR records in
Skyrim.esm**, and static analysis of `Oblivion.exe`. Field LAYOUT came from
xEdit; field SEMANTICS came from UESP plus the vanilla census. Getting the
layout right while guessing the semantics is what produced a blinding-white
sky and stars drawn in front of the world.

## The chain — weather is never referenced directly

    WRLD --CNAM--> CLMT --WLST--> WTHR

CLMT used to be in `SKIP_TYPES` ("climate system differs"), which was wrong:
TES5's CLMT is near-identical to TES4's. Skipping it orphaned all 37 converted
weathers and Cyrodiil rendered under Skyrim's own climate, sun and moons.

**57 of 84 TES4 worldspaces author no CNAM at all** — including Tamriel, every
Imperial City district and every walled city. Oblivion resolves that at
RUNTIME, not load time: in `Oblivion.exe`, the sky setup at `0x667688` calls
the worldspace get-climate (`0x4CAF90`); when it returns null it falls through
to `0x543200`, which does `LookupForm(0x15F)` — the engine-created
`DefaultClimate` (bootstrap at `0x44CCE9` pushes `0x15F`, names it from the
string at `0xA37CA0`). Skyrim has **no such fallback**, so `convert_WRLD`
writes TES4's DefaultClimate explicitly. It is a raw TES4 id and must go
through `remap_formid`.

## CLMT

Only one format change: the WLST entry gained a trailing Global FormID,
8 -> 12 bytes (`FormID, Chance:s32, Global`). Everything else is verbatim:
FNAM/GNAM sun textures, the 6-byte TNAM timing struct.

`MODL` is the **night-sky/stars mesh** and every vanilla Skyrim climate has one
(`Sky\Stars.nif`); the TES4 export was dropping it, so a climate with no model
draws no stars. Falls back to `Sky\Stars.nif`.

## WTHR field semantics — where equivalence does NOT hold

### NAM0 colour table (272 bytes = 17 slots x 4 times x RGBA)

TES4 has 10 slots, TES5 has 17; times-of-day match, so only the type axis is
remapped. TES4 `Clouds-Lower`/`Clouds-Upper` become TES5 `Cloud LOD
Ambient`/`Cloud LOD Diffuse`; TES4's single `Fog` feeds both `Fog Near` and
`Fog Far`.

The four TES5-only slots must NOT be guessed — three of them tint **additive**
passes, so a wrong value blows the scene out:

| Slot | Field | Correct default | Census (of 84) |
|---|---|---|---|
| 13 | Sky Statics | **black** | mode (0,0,0); never white |
| 14 | Water Multiplier | white | mode (255,255,255), 24x |
| 15 | Sun Glare | **black** | black 35x |
| 16 | Moon Glare | **black** | black 27x |

Forcing 13 white and copying the TES4 Sun/Stars colours into 15/16 is what made
the sky blinding.

### RNAM / QNAM cloud speed — a real unit conversion

Both engines cap cloud drift at the same **0.1** units, so the byte must be
rescaled, not copied:

* TES4: UNSIGNED `0..255` scaled by `fWeatherCloudSpeedMax`, whose default
  `0.1` is read straight from `Oblivion.exe` — the settings constructor at
  `0x9E5BF0` does `fld dword ptr [0xA2FAAC]` before pushing the name at
  `0xA56C88`. Always forwards.
* TES5: SIGNED `-0.1 .. +0.1` as `0x00 .. 0xFE`, `0x7F` = 0
  (xEdit `wbWeatherCloudSpeedToStr` = `(v-127)/127/10`, clamped to 254).

So TES4 `b` maps to `127 + round(b/255*127)`. The earlier `0x7F + speed//2` ran
clouds roughly 10x too fast.

### JNAM cloud alpha

Only layers that actually carry a texture may be opaque. A blanket 1.0 across
all 32 layers asks the engine to draw 30 fully-opaque empty layers over the
sky. Vanilla alphas vary 0.0-1.2 per layer/time; TES4 has no per-layer curve,
so the two real layers get 1.0 and the rest 0.0 — and this must stay in sync
with `NAM1` (disabled-layer bitfield).

### NAM1 disabled layers

Disable only the layers with no texture. The original blanket `0xFFFFFFFF`
also disabled layers 0/1 and blanked every converted sky.

### DALC directional ambient (4 x 32 bytes)

TES5 lights the world with a 6-direction ambient cube TES4 has no equivalent
for, so it is derived from NAM0's Ambient. The per-face weights are **measured**
(median of face/Ambient over all 84 vanilla records, every time and channel):

    X+ 0.98   X- 0.94   Y+ 0.96   Y- 0.95   Z+ 0.67   Z- 1.28

**Z+ is the DARKEST face and Z- the brightest** — the opposite of the intuition
that the sky-facing side is brighter. Writing Ambient verbatim into all six
faces and then brightening Z+ overdrove the cube and washed the scene out.
Layout is 6 x RGBA + Specular RGBA + Fresnel f32; Fresnel is 1.0 in every
vanilla record.

### DATA (19 bytes)

TES5 keeps TES4's field order but replaces TES4's two cloud-speed bytes
(offsets 1-2) with padding — speed moved into RNAM/QNAM — and appends four
fields TES4 has no source for. Offsets 6-14 (precipitation/thunder fades,
frequency, classification, lightning colour) exist in both and were previously
dropped on the floor.

**Thunder frequency is inverted in BOTH games** (255 = never, 15 = constant),
so it is a genuine passthrough: vanilla Oblivion clear weathers are all 255 and
its thunderstorms are 188/132/100/24. Do not "fix" it.

### Required subrecords

`LNAM` (max cloud layers, vanilla is 29 / `0x1D`; 0 allocates none), `MNAM`
(precipitation SPGD) and `NNAM` (visual effect RFCT) are `.SetRequired` in
xEdit. TES4 has no source for MNAM/NNAM — emit the explicit NULL that vanilla
records use rather than omitting the subrecord.

### IMSP / HDR tone mapping — the records differ, not just the fields

**This is the field that decides overall scene exposure, and the two games put
it in different records.** Oblivion stores HDR per WEATHER (`WTHR.HNAM`, 14
floats). Skyrim has **no per-weather HDR field at all** — it lives in an
imagespace the weather points at (`WTHR.IMSP` -> `IMGS.HNAM`, 9 floats).

Pointing every converted weather at the stock `0x161`
(`DefaultImageSpaceExterior`) is **not** a valid conversion. `0x161` is one of
only **two** vanilla imagespaces that ship `ENAM` and **no `HNAM`** (the other
is `0x160` Interior); every one of the other 268 has a real HNAM, and 332 of
the 336 `IMSP` references in Skyrim.esm target one of those. With no HNAM the
HDR block is undefined and **the world renders blindingly white at every hour,
including night** — the symptom that survived all the NAM0/DALC fixes.

So each weather mints **four** imagespaces, one per time of day. That is not a
detail: **59 of the 84 vanilla weathers (70%) use distinct imagespaces per
slot**, and day/night differ a lot (`SkyScale` 0.235 day vs 0.02 night on
SkyrimClear). Collapsing all four onto one gives day and night identical tone
mapping.

Calibrate against the **213 imagespaces a vanilla WEATHER actually
references**, not all 268 — the rest are interior/dungeon and pull the
envelope somewhere no outdoor weather sits.

Field correspondence (xEdit `wbDefinitionsTES5` IMGS/HNAM + UESP
`Skyrim Mod:Mod File Format/IMGS`, whose note names slots 5/6 the "target
luminance" pair — exactly TES4's `TargetLum`/`UpperLumClamp`):

| TES5 IMGS.HNAM | <- TES4 WTHR.HNAM | treatment |
|---|---|---|
| 0 Eye Adapt Speed | EyeAdaptSpeed | rescale 0..1 -> 15..50, then per-slot bias |
| 1 Bloom Blur Radius | *(none)* | **constant 7.0** |
| 2 Bloom Threshold | BrightClamp | blend 35% toward the vanilla slot median |
| 3 Bloom Scale | BrightScale | rescale 1..3 -> 2.5..4 |
| 4 Receive Bloom Threshold | TargetLum | rescale 0.75..1.2 -> 0.42..0.60 |
| 5 White | UpperLumClamp | rescale 1.0..1.3 -> 0.88..1.02 |
| 6 Sunlight Scale | SunlightDimmer | rescale 0.5..2 -> 0.9..2.7, per-slot bias |
| 7 Sky Scale | *(none)* | derived from the weather's own sky colour |
| 8 Eye Adapt Strength | *(none)* | vanilla per-slot 15/5/15/20 |

**Shared field names do not mean shared ranges.** Three TES4 fields occupy a
different numeric span from the TES5 field they feed, so a raw copy lands at
or past the TES5 ceiling:

| field | TES4 observed | TES5 weather-used | effect of copying raw |
|---|---|---|---|
| TargetLum | 0.75..1.20 | 0.20..1.00 | pinned at 1.0 — **the whole frame blooms** |
| UpperLumClamp | 1.00..1.30 | 0.60..1.075 | pinned at the ceiling |
| BrightScale | 1.00..3.00 | 2.5..4.0 typical | bloom too weak |

`BloomBlurRadius` is **7.0 in all 213** weather-used imagespaces — an engine
constant, not authored data. TES4's `BlurRadius` (4..8) belongs to Oblivion's
own blur pass and merely shares a name; feeding it through made the bloom
kernel too tight.

`EyeAdaptSpeed` is a genuine **unit change**: Oblivion's is a 0..1 rate
(`fEyeAdaptSpeed:BlurShaderHDR`, default 0.7 — in Oblivion.ini and named in
Oblivion.exe at `0xA3E965`), while Skyrim's weather-used range is **15..50**.

`SkyScale` is the sky's contribution to scene exposure and TES4 has **no
equivalent field**. It tracks the weather's own sky brightness (corr +0.29 over
284 vanilla weather/slot pairs): a dark night sky sits at ~0.025, any lit sky
at ~0.20. A flat value washes the day sky out to near-white while
over-lighting the night, so it is ramped from the weather's Sky-Upper
luminance at that time of day.

Every output field is finally **clamped to the per-field min/max of those 213
imagespaces**. And a weather whose entire TES4 HNAM is zero has no authored HDR
at all — `DefaultWeather` is one, and it is exactly the weather the 57
CNAM-less worldspaces fall back to. Copied verbatim it gives `White=0` and
`SunlightScale=0` (a zero white point); clamping those zeros to the range
*minimum* instead pins every field at its flattest legal value. Neither is
right, so an unauthored record takes the vanilla per-slot defaults.

Because WTHR mints a companion record, it is **not** in the generic dispatch;
it runs in its own serial phase (`import_main` Phase 2b) so
`writer.alloc_formid()` stays deterministic. Group order is
`IMGS -> WTHR -> CLMT`, matching the reference direction.

### EditorID collision

Oblivion and Skyrim both ship a `DefaultWeather`. After remapping, ours
collides with Skyrim's `0x0000015E` and the CK renames it
`DefaultWeatherDUPLICATE001` — so it is prefixed to `TES4DefaultWeather`.

## Sky meshes need BSSkyShaderProperty

Skyrim draws sky through a **dedicated pass** keyed on
`BSSkyShaderProperty.Sky Object Type`. A sky mesh carrying
`BSLightingShaderProperty` is lit, fogged and depth-sorted as ordinary world
geometry — which is why converted stars drew in front of the landscape.

`SkyObjectType` (references/nif 0.10.0.0.xml): 0 TEXTURE, 1 SUNGLARE, 2 SKY,
3 CLOUDS, 5 STARS, 7 MOON_STARS_MASK. Confirmed against shipped assets:
`sky/stars.nif` is type 5 throughout, `sky/clouds.nif` is type 3.

`nif_converter.sky_object_type_for()` maps Oblivion's `Sky/` meshes to a type.
Oblivion had no such enum — it identified sky geometry by which climate/weather
slot referenced it — so the table is the bridge between the two models and
cannot be derived from the NIF. It keys on the `sky/` **directory**, not the
basename alone (`architecture/sky/wall.nif` must not match).

Two further contracts, both verified against vanilla `sky/stars.nif`:

* **Root stays a plain `NiNode`**, not `BSFadeNode`. Fade nodes apply
  distance-based fading, meaningless for a dome always drawn around the camera.
* **No `NiAlphaProperty`.** The sky pass does its own blending; vanilla sky
  meshes carry none, and keeping Oblivion's makes the layer vanish or z-fight.

Precipitation meshes (`rainheavy`, `rainlight`, `snow`) are particle/world
geometry and correctly keep their effect/lighting shaders.

## Verifying

    python -m pytest tests/test_import.py -k "Weather or Climate or Worldspace or SkyMesh"

After a full import, the chain should show 37 WTHR, 19 CLMT, zero dangling
`WLST -> WTHR`, and zero worldspaces missing `CNAM`.
