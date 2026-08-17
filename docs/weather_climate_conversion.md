# Weather / Climate Conversion (WTHR, CLMT, REGN weather, sky meshes)

> **Status (2026-08-09): the FULL chain is live on this branch** (ported from
> the old `weather-conversion` WIP branch and extended). WTHR runs in its own
> serial import phase (2b) minting four IMGS companions per weather; CLMT is
> in the generic dispatch; WRLD always writes CNAM (DefaultClimate fallback);
> REGN weather entries are converted; exterior CELLs carry XCLR (the runtime
> path for region weather); rain/snow weathers get vanilla SPGD precipitation
> via MNAM; and the script converter emits real Weather.* calls. The former
> `CONVERT_CLIMATE` feature flag is deleted.
>
> In-game rounds (2026-08-09) found, in order: day overexposure (median-
> anchored IMGS + fog max), weather never changing (CELL XCLR + volatility
> 50), the scripted-weather override lock (abOverride=False now), Sun Glare/
> Trans Delta out of vanilla range, missing IMGS DNAM — and finally the REAL
> bloom source: **Oblivion's colour palette itself runs 1.5-4x vanilla
> luminance**, now self-normalized per plugin (see below), plus weather winds
> mixed as SFX instead of ambience, and moons shipped from Oblivion's own
> textures at the engine's hardcoded paths. Fourth build awaits in-game
> confirmation.

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
8 -> 12 bytes (`FormID, Chance:s32, Global`). FNAM/GNAM sun textures and the
TNAM times/moons byte are verbatim (TES4's moons byte 0xC3 = Masser+Secunda+
phase 3 is byte-identical to vanilla SkyrimClimate's).

**TNAM volatility is NOT a passthrough.** TES4's byte spans 0..255 and
Oblivion cycles weather regardless of it (TamrielClimate 174, DefaultClimate
0 — both change in-game); in Skyrim it is the re-roll chance and the vanilla
census is bimodal: **SkyrimClimate, the one variable outdoor climate, ships
exactly 50**, and 0 appears only on locked skies (Sovngarde, Blackreach,
BloatedMan's Grotto). Passing TES4's 0 through froze every converted sky on
its first weather — one of the two halves of the "weather is always clear"
bug (the other was missing CELL XCLR). Converted climates write 50.

`MODL` is the **night-sky/stars mesh** and every vanilla Skyrim climate has one
(`Sky\Stars.nif`); the TES4 export was dropping it, so a climate with no model
draws no stars. Falls back to `Sky\Stars.nif`.

## WTHR field semantics — where equivalence does NOT hold

### NAM0 colour table (272 bytes = 17 slots x 4 times x RGBA)

TES4 has 10 slots, TES5 has 17; times-of-day match, so only the type axis is
remapped. TES4's single `Fog` feeds both `Fog Near` and `Fog Far`. The TES4
cloud tints do NOT go to the Cloud LOD slots — vanilla ships those BLACK
(median and p90 are 0 in every slot); they feed the per-layer PNAM instead.

### Colours are LUMINANCE-NORMALIZED, not copied (the real bloom source)

**Oblivion authors its weather palette far hotter than Skyrim, and bloom
triggers on rendered luminance — no imagespace calibration can fix colours
that run 1.5-4x vanilla.** Census (real Skyrim.esm vs raw conversion, midday
medians): Sun slot 193 vs vanilla **43** (Skyrim's sun brightness is HDR, not
this slot — a 255-luminance disc blooms enormously, the "white toward the
sun"), Sunlight 206 vs 152, Sky-Upper 137 vs 84, Fog 137 vs 96 — while
Ambient is authored at HALF vanilla (92 vs 172), stacking blown highlights on
black shadows ("lighting doesn't look right").

`set_nam0_normalization()` (import Phase 2b pre-pass) is self-calibrating
per plugin: it computes the plugin's median luminance per slot/time over all
its weathers and scales every colour so the plugin median lands on the
vanilla median — hue preserved, hard cap at vanilla p90, authored
between-weather variation intact. Applies to Sky-Upper, Fog Near/Far,
Ambient (and therefore DALC), Sunlight, Sun, Stars, Sky-Lower, Horizon;
PNAM cloud tints get the p90 cap only (vanilla medians there are 0);
Effect Lighting (slot 9, no TES4 source) takes vanilla per-time channel
medians instead of black. Verified post-build: every converted slot median
sits exactly on the vanilla median.

### Weather sounds: ambience loops go in AudioCategoryAMB

A 2D LOOPING TES4 sound is an ambience bed. Every converted SNDR used to be
filed under `AudioCategorySFX`, the wrong mix bus — no ambience
slider/ducking — which is why Oblivion's weather winds (authored even on
Clear: two quiet wind beds, where vanilla Skyrim clear weathers are silent)
played loud over everything. 2D+loop now lands in `AudioCategoryAMB`
(0x7F80B), matching every vanilla `AMBWeather*` descriptor; one-shots and 3D
sounds stay SFX.

### Moons: engine-drawn; verified facts and the open question

**Nothing in this pipeline may override Skyrim defaults** — a first attempt
shipped Oblivion's moon textures at the engine's global
`textures\sky\masser_*.dds` paths and was reverted the same day (2026-08-09);
Skyrim's own moons are acceptable.

What is VERIFIED in SkyrimSE.exe (GOG build, create function
0x3ce0a0-0x3ce4fa, called from Sky::Update 0x3caf80):

* Masser is created iff the active climate's byte at +0x7D (the CLMT TNAM
  moons byte) has the SIGN bit (0x80); Secunda iff bit 0x40.  Our climates
  pass 0xC3 through — byte-identical to vanilla SkyrimClimate.
* Creation is otherwise UNCONDITIONAL — same function that loads the climate
  stars model (which demonstrably works: stars render in-game).  No
  worldspace record gate exists in this path; the converted TES4Tamriel WRLD
  is field-for-field equivalent to vanilla Tamriel where sky-relevant
  (DATA flags 0x00 both).
* Phase textures come from the hardcoded template
  `Data/Textures/Sky/%s_%s.dds` (Masser/Secunda x full/three_wax/... — the
  vanilla BSA copies).  Moon visibility params (iMasserSize, fade angles,
  z-offset) are INI settings read once at construction.
* Ruled out as occluders: the converted cloud layers (clear-sky Oblivion
  cloud textures average alpha 16-63/255; JNAM 1.0 matches the 79% of
  vanilla textured layers at 1.0) and the stars dome (structurally identical
  to vanilla: same shapes, extents, BSSkyShaderProperty types and flags).
* Moon PHASE is (GameDaysPassed / 3) % 8 and the `_new` phase is invisible —
  a test save parked in that window shows no moons legitimately.

In-game discrimination if moons are still absent (each step isolates one
layer): (A) vanilla worldspace `coc Riverwood`, `set gamehour to 23` — no
moons here means the install/ini, not the conversion; (B) `cow TES4Tamriel
3 -3`, same hour — moons here mean phase/weather luck earlier; (C) still in
TES4Tamriel force a VANILLA weather `fw 81A` (SkyrimClear) — moons appearing
only now mean OUR WTHR records suppress them (binary-diff next); absent even
under vanilla weather in our worldspace means a climate/worldspace-level
mechanism the create-path disassembly does not show — go dynamic
(game_bridge probes).  `set gamedayspassed to 12` re-rolls the phase.

The four TES5-only slots must NOT be guessed — and they must not be flat
either. **Two successive censuses got this wrong** (2026-08-09): copying the
TES4 Sun/Stars colours into 15/16 made the sky blinding, and the "vanilla
mode is black" correction **hid the moons** — slot 13 Sky Statics tints the
moon discs and is *never black* in vanilla outdoor weathers (SkyrimClear
night is 45,137,208). The mode-black artifact came from collapsing the time
axis and mixing dungeon weathers in, **and the references/Skyrim.esm dump
truncates NAM0 hex before slot 13** — this census must be run against the
real Skyrim.esm (`temp`-style script over the binary, see
`_NAM0_SLOT_CLASS_DEFAULTS`).

The real vanilla shape is per-CLASSIFICATION and per-time (medians):

* **13 Sky Statics** — pale sky-toned colours by day, blues/grays at night;
  never black. Black here = invisible moons (stars are slot 6 and keep
  rendering, which is what localises the symptom).
* **14 Water Multiplier** — NOT flat white: every class ships dark teal
  (31,63,75) at night; white over-brightens night water.
* **15 Sun Glare** — dark browns by day on Pleasant weathers, black at night,
  black all day under cloud/rain/snow.
* **16 Moon Glare** — inverse of 15: Pleasant ships a warm bright halo at
  night (255,173,138); classes whose clouds hide the moons ship black.

The converter keys these off the shared classification bits
(`_nam0_class_defaults`, snow > rain > cloud > pleasant), so an Oblivion
thunderstorm gets Skyrim's storm treatment and a clear night gets the moon
halo.

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

**Trans Delta and Sun Glare are NOT passthroughs** (2026-08-09 census of the
real Skyrim.esm): TES4 Trans Delta spans 0..255 with median 255, vanilla TES5
ships 125 almost universally — mapped x125/255. TES4 clear weathers author
Sun Glare 255 where vanilla's median is 0, p90 153, max 191 — a raw 255
overdrives the glare pass and paints the sky white toward the sun; mapped
x0.6 (TES4 max -> vanilla p90).

### Required subrecords

`LNAM` (max cloud layers, vanilla is 29 / `0x1D`; 0 allocates none), `MNAM`
(precipitation SPGD) and `NNAM` (visual effect RFCT) are `.SetRequired` in
xEdit. NNAM stays NULL (82 of 84 vanilla weathers ship NULL). The SSE-only
volumetric-lighting `HNAM` is omitted entirely — 0 of 84 records in the
Skyrim.esm dump carry it (form version 40), so its absence is vanilla-legal.

### MNAM precipitation — NULL means a rainstorm with no rain

Oblivion draws rain/snow through hardcoded `Sky\` meshes picked off the
weather's classification bits; Skyrim draws them through the SPGD named in
MNAM (18 of 84 vanilla weathers). So the classification is the authored
source, mapped onto the vanilla Skyrim.esm particle systems
(`_wthr_precipitation`):

| authored TES4 data | SPGD |
|---|---|
| Snow (bit 3) | `SnowParticlesMed` 0x00023C49 |
| Rainy (bit 2) + authored thunder (`ThunderFrequency` < 255) | `RainStormParticles` 0x0010780F |
| Rainy, no thunder | `RainParticles` 0x00023C48 |
| neither | NULL |

ThunderFrequency is inverted in both games (255 = never), and the rainy bit
only exists when DATA was authored, so frequency 0 there is authored
"constant thunder", not a missing field.

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
| 2 Bloom Threshold | BrightClamp | median-anchored, gain 1.0 |
| 3 Bloom Scale | BrightScale | median-anchored, gain 0.5 |
| 4 Receive Bloom Threshold | TargetLum | median-anchored, gain 0.3 |
| 5 White | UpperLumClamp | median-anchored, gain 0.25 |
| 6 Sunlight Scale | SunlightDimmer | rescale 0.5..2 -> 0.9..2.7, per-slot bias |
| 7 Sky Scale | *(none)* | own sky luminance, clamped to slot p10..p90 |
| 8 Eye Adapt Strength | *(none)* | vanilla per-slot 15/5/15/20 |

**Shared field names do not mean shared ranges — and rescaling SPANS is not
enough either.** The first calibration mapped each TES4 field's observed span
onto the vanilla span, but the TES4 *medians sit at the edge of their spans*
(median UpperLumClamp is 1.0, the bottom of 1.0..1.3; median TargetLum is
1.2, the very top of 0.75..1.2), so nearly every weather landed at the edge
of the vanilla range too: White = 0.88 — vanilla's bottom decile, a lowered
white point — with bloom threshold and receive-bloom simultaneously mapped
bloom-heavy. In-game: the day looked like an overexposed camera (confirmed
2026-08-09; nights were fine).

The fix is **median anchoring** (`_IMGS_ANCHORED_FIELDS`):

    value = vanilla_slot_median + (tes4 - tes4_median) * gain,
    clamped to the vanilla p10..p90 band for that slot

so a median TES4 weather renders exactly base-Skyrim and authored deviation
moves it modestly within the vanilla band. TES4 medians (36 authored
Oblivion.esm HNAMs; Nehrim agrees, its outliers — SunlightDimmer 50,
TargetLum 5.2 — are what the p10..p90 clamp is for): TargetLum 1.2,
UpperLumClamp 1.0, BrightScale 2.0, BrightClamp 0.3. Sky Scale keeps the
luminance ramp but clamps to the slot band (night p90 = 0.13) — Oblivion
authors bright night skies and an uncapped ramp gave night a near-daytime
sky exposure.

`FNAM` fog power/max are also NOT xEdit's 1.0 defaults: vanilla ships
medians 0.4/0.4 power and 0.9/0.925 max. Max 1.0 lets fog reach full opacity
at the horizon and paints it with Oblivion's pale fog colour — a big part of
the blown-white horizon.

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

Each minted IMGS also ships **DNAM** — 213 of the 214 weather-used vanilla
imagespaces carry it, and omitting it leaves depth-of-field and SKY BLUR
undefined (part of the "everything is bloomy/blurry" symptom). Vanilla
medians: Strength 0.5, Distance 0, Range 10000, and the modal Sky/Blur enum
16920 = "No Sky, Radius 4" — the sky is explicitly EXCLUDED from the blur
pass.

Because WTHR mints companion records, it is **not** in the generic dispatch;
it runs in its own serial phase (`import_main` Phase 2b) so record order stays
deterministic. Group order is `IMGS -> WTHR -> CLMT`, matching the reference
direction.

### EditorID collision

Oblivion and Skyrim both ship a `DefaultWeather`. After remapping, ours
collides with Skyrim's `0x0000015E` and the CK renames it
`DefaultWeatherDUPLICATE001` — so it is prefixed to `TES4DefaultWeather`.

## Vanilla-substitution map (Oblivion WTHR -> vanilla Skyrim WTHR)

For a converter option that **ships vanilla Skyrim weathers instead of the
converted Oblivion ones**, sidestepping the whole NAM0-normalization /
IMGS-minting path. The map below is derived from the authored discriminators
on both sides — `DATA.Classification`, `WindSpeed`, `ThunderFrequency`,
`SunGlare`, and the `FNAM` fog distances — read from
`export/Oblivion.esm/WTHR.txt` and the real `references/Skyrim.esm/WTHR.txt`
(84 vanilla records). Only genuinely close matches are listed; everything else
is deliberately absent and must keep the converted record.

**The classification flags byte is at DATA offset 11 in BOTH games** (xEdit
`wbDefinitionsTES4.pas:3778` / `wbDefinitionsTES5.pas:10656`) — bit 0
Pleasant, 1 Cloudy, 2 Rainy, 3 Snow. TES5 adds bits 4/5 (Aurora) that TES4 has
no source for. Reading it anywhere else yields 0 for all 84 vanilla records,
which looks like "vanilla never sets classification" and is wrong.

### Close matches — safe to substitute

| Oblivion WTHR | class / wind / thunder / fogFar | vanilla Skyrim WTHR | FormID | why it matches |
|---|---|---|---|---|
| `Clear` | Pleasant, 25, never, 170000 | `SkyrimClear` | `0x0000081A` | identical class, **identical wind 25**, no thunder; both the climate's fair-weather default |
| `SEClear` / `SEClear01` / `SEClear03` / `SEClearBlue` / `TestBlissClear` | Pleasant, 25, never | `SkyrimClear` | `0x0000081A` | DATA identical to `Clear` (SI reskins the colours only) |
| `SEClearTrans` | Pleasant, 25, never | `SkyrimClear` | `0x0000081A` | identical to `Clear` except `TransDelta` 0 vs 255 — an instant-transition variant; TransDelta is remapped x125/255 anyway |
| `Cloudy` | Cloudy, 37, never, 150000 | `SkyrimCloudy` | `0x00012F89` | **wind 37 vs 38**, no precipitation; note vanilla `SkyrimCloudy` is flagged *Pleasant*, not Cloudy — it is Skyrim's partly-cloudy fair weather, which is what Oblivion's `Cloudy` is |
| `SECloudy` / `SEManiaFog` | Cloudy, 37, never | `SkyrimCloudy` | `0x00012F89` | same DATA as `Cloudy` |
| `Overcast` | Cloudy, 19, never, 150000 | `SkyrimOvercastWar` | `0x000D299E` | the only vanilla *Cloudy*-flagged overcast with no precipitation; wind 19 vs 89 is the weak point |
| `SEOvercast` | Cloudy, 19, never, 80000 | `SkyrimOvercastWar` | `0x000D299E` | DATA identical to `Overcast` |
| `Fog` | Cloudy, 8, never, **4096** | `RiftenOvercastFog` | `0x0010FE7E` | Cloudy on both sides; vanilla's tightest fog (dayFar/nightFar both 9000) against Oblivion's 4096. `SkyrimFog` dayFar 25000 is far too open |
| `SEFog` / `SEOrderedFringe` / `SE13JiggyWeather` | Cloudy, 8, never, 8000 | `SkyrimFog` | `0x000C821E` | fogFar 8000 sits between vanilla's 25000/9000; `SkyrimFog` is the general-purpose one |
| `Rain` | Rainy, 14, **never**, 10000 | `SkyrimOvercastRain` | `0x000C821F` | rain with **no thunder** on both sides — the exact vanilla rain/storm split |
| `SERain` | Rainy, 14, never, 10000 | `SkyrimOvercastRain` | `0x000C821F` | same DATA as `Rain` |
| `Thunderstorm` | Rainy, 81, **188**, 6000 | `SkyrimStormRain` | `0x000C8220` | both authored rain **with** thunder; vanilla freq 246 vs 188 (more frequent in Oblivion) |
| `SEThunderstorm` | Rainy, 81, 188, 6000 | `SkyrimStormRain` | `0x000C8220` | DATA identical to `Thunderstorm` |
| `ThunderstormKvatch` | Rainy, 81, 188, 6000 | `SkyrimStormRain` | `0x000C8220` | identical to `Thunderstorm` but for lightning colour (236,240,253 vs 245,248,254) — substituting loses that tint |
| `SE32GloomStorm` | Rainy, 81, **24**, 3000 | `SkyrimStormRain` | `0x000C8220` | near-constant thunder; closest vanilla storm, though gloomier than any |
| `Snow` | Snow, 7, never, 12000 | `SkyrimOvercastSnow` | `0x0004D7FB` | calm snowfall, no thunder; vanilla wind 76 vs 7 is the gap (`SkyrimStormSnow` wind 178 + thunder is much further off) |
| `SETestAsh` | Snow, 14, never, 10000 | `SkyrimOvercastSnow` | `0x0004D7FB` | uses the Snow bit for ashfall; vanilla snow is the nearest particle system |
| `SEWaitingRoomWeather` | **Snow**, 19, never, 80000 | `SkyrimOvercastSnow` | `0x0004D7FB` | shares `Overcast`'s DATA except the classification, which is Snow (8) not Cloudy — the flag is what drives precipitation, so it maps with the snows |

Reference: `SkyrimStormSnow` `0x000C8221` (Snow, wind 178, thunder 203) is the
vanilla blizzard. **No Oblivion weather matches it** — Oblivion authors no
thundering snow — so it is listed for completeness but is not a substitution
target.

### Deliberately NOT mapped — keep the converted record

These have no vanilla counterpart, and substituting the nearest-looking one
would replace authored art direction with something visually unrelated:

* **Oblivion-realm skies** — `OblivionStormTamriel`, `OblivionStormTamrielMQ16`,
  `OblivionStormOblivion`, `OblivionElectrical`, `OblivionMountainFog`,
  `OblivionSigil`, `Obliviondefault`, `SigilWhiteOut`. The red/black Deadlands
  sky is the single most recognisable weather in the game and vanilla Skyrim
  has nothing near it. `OblivionStormTamriel` is also the weather the gate
  scripts force (see *Scripted weather* above), so substituting it changes what
  `ForceActive` puts over the sky.
* **Quest/set-piece skies** — `CamoranWeather`, `MS14Sky`,
  `SE09SummoningWeather`. Authored for one scene.
* **`DefaultWeather`** — the fallback for all 57 CNAM-less worldspaces, and
  already special-cased (`TES4DefaultWeather`, EditorID collision above). Its
  TES4 HNAM is all-zero, so it takes the vanilla per-slot IMGS defaults
  regardless.

### What substitution has to redirect

A vanilla weather is referenced, never copied, so the option is a **FormID
redirect at every referrer**, not a skip in Phase 2b. Four referrers exist:

1. `CLMT.WLST` — climate weather lists (`convert_CLMT`)
2. `REGN.RDWT` — region weather lists (`convert_REGN`), where Cyrodiil's actual
   variety lives
3. Script `Weather` properties — auto-bound **by EditorID**
   (`script_convert/converter.py`), so a substituted weather must resolve to
   the vanilla EditorID or the property silently fails to bind
4. `WTHR` itself — the record is not emitted, and **its four IMGS companions
   must not be minted**

Point 4 costs nothing in FormIDs: companion ids are
[hashed from the source weather](../CLAUDE.md#formid-drift), so skipping a
weather's IMGS leaves every other record's id untouched. Toggling the option
still changes which records exist, so it remains a new-game-only setting,
exactly like enabling weather conversion itself.

## REGN weather — where Cyrodiil's weather variety actually lives

**TamrielClimate's WLST is a single Clear weather at 100%.** All the rain,
snow and fog variety in Oblivion comes from **59 region weather lists** (RDAT
type 3 + RDWT) layered over the climate — the identical mechanism Skyrim uses
for its own coasts and holds (`WeatherCoastFog`, `WeatherWinterhold`, ...).
Converting the climate chain without regions gives a permanently clear
Cyrodiil.

- `tes4_export/record_types/world.py::export_REGN` walks subrecords **in
  order** (each RDWT belongs to the RDAT before it, each RPLD to its RPLI)
  and dumps areas as raw hex — the TES5 RPLD layout is byte-identical
  (x,y float pairs, world units unchanged).
- `tes5_import/record_types/world.py::convert_REGN` emits EDID, RCLR, WNAM,
  the RPLI/RPLD areas, and ONLY the weather RDAT entries; RDWT entries widen
  8 → 12 bytes with a trailing null Global FormID (same widening as CLMT's
  WLST). RDAT's Override/Priority bytes pass through (vanilla Skyrim weather
  regions use exactly this: Override=1 Priority=95 etc.).
- A region with no weather list, or no area polygon to apply it in, emits
  nothing (`convert_REGN` returns None; the generic dispatch skips falsy
  results). Object/grass/sound/map generators stay dropped.
- **The RPLD polygons alone do NOTHING at runtime — region weather reaches
  the sky through the CELL's XCLR region list.** Skyrim.esm puts
  WeatherWinterhold in 30 cells' XCLR and WeatherCoastFog in 51. Without
  XCLR every converted exterior fell back to the climate WLST (TamrielClimate
  = single Clear at 100%) and the sky never changed. `convert_CELL` now
  writes XCLR, filtered against the regions `convert_REGN` actually emitted
  (`_EMITTED_REGION_FIDS`, reset per import) and sorted (xEdit `wbArrayS`).
  Oblivion.esm: 8,501 exterior cells carry XCLR, 0 dangling refs.
  Known limit: a master-dependent ESP's cells only keep refs to its OWN
  emitted regions, not the master's.

## Scripted weather (ForceWeather / SetWeather / ReleaseWeatherOverride)

The Oblivion-gate scripts (`MQ10/11/13/16`, `MS48/94`, the random gates) hold
`OblivionStormTamriel` over the sky while the player is near a gate, and SE
quests force their own skies. These were no-op stubs while the chain was
gated; now (`script_convert/converter.py`, signatures verified against
`references/skse64-master/scripts/vanilla/Weather.psc`):

| TES4 | Papyrus |
|---|---|
| `ForceWeather X [flag]` / `fw` | `X.ForceActive(False)` (instant switch) |
| `SetWeather X [flag]` / `sw` | `X.SetActive(False, False)` (natural transition) |
| `ReleaseWeatherOverride` | `Weather.ReleaseOverride()` |
| `GetIsCurrentWeather X` | `(Weather.GetCurrentWeather() == X)` |
| `GetCurrentWeatherPercent` | `Weather.GetCurrentWeatherTransition()` (both 0..1 — the old constant-50 stub assumed 0..100) |

**abOverride must be FALSE on both** (2026-08-09, confirmed in-game the hard
way).  Oblivion holds scripted weather by CONTINUOUS RE-APPLICATION — the
gate scripts re-force the storm every GameMode pass while the player is near
— not by an engine lock, and its scripts stop running when the ref unloads.
Skyrim's abOverride=True is a GLOBAL lock that survives the caller
unloading, so the first mapping (True) let a fast-travel away from an open
Oblivion gate strand `OblivionStormTamriel` over the whole world FOREVER:
the `ReleaseWeatherOverride` lives in the same script's update loop, which
stopped the moment the gate unloaded.  With False the converted loops keep
the storm applied while they run (same look near the gate) and the
region/climate system reclaims the sky on its next re-roll once they stop.
A save stuck this way is cleared from the console: `cf "Weather.ReleaseOverride"`.

The weather argument is registered as a `Weather` property and auto-binds to
the converted WTHR by EditorID (no Oblivion script references
`DefaultWeather`, the one renamed EditorID).

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

    python -m pytest tests/test_import.py -k "Weather or Climate or WorldspaceClimate or SkyMesh or RegionWeather"
    python -m pytest tests/test_script_converter.py -k "Weather"

After a full import of Oblivion.esm, the chain should show 37 WTHR, 148 IMGS
(4 per weather), 19 CLMT, ~57 weather REGN, ~8,500 exterior cells with XCLR
(zero dangling), zero dangling `WLST -> WTHR` / `RDWT -> WTHR`, and zero
worldspaces missing `CNAM`.

**FormID drift warning:** Phase 2b allocates 148 IMGS FormIDs before every
later companion-allocating phase (SNDR, SOPM, ...), so enabling or disabling
weather conversion shifts all subsequently allocated FormIDs and breaks
existing saves.
