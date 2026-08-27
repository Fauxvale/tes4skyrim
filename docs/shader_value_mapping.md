# Shader values: what the converter writes, and why

Measured 2026-08-19/20 with `tools/shader_value_census.py`,
`tools/mesh_identity_census.py` and `tools/bc4_preview.py`. Every number here
was computed in that session; nothing is quoted from a wiki without a
measurement beside it.

## The defect this replaced

Our output assigned **no** material values at all, so every shape shipped at
pyffi's defaults. Measured over 400 output meshes / 3931 lighting shaders:

| | our output | vanilla Skyrim (type 0) |
|---|---|---|
| glossiness | **0.0 — 100%** | 80.0 median **and** mode |
| specular colour | **black — 100%** | white 56.1%, black 3.0% |
| specular strength | 1.0 | 1.0 mode (44.7%) |
| `SLSF1_Specular` | **on — 100%** | — |

Glossiness 0 with a black specular colour and the flag on is the combination
that reads as a flat blown-out sheen.

## Vanilla census: 80 is real, the tail is not

Random 1500-mesh sample of `references/Skyrim Meshes`, 4693 lighting shaders.
**Glossiness is a property of the shader TYPE, not of the asset category:**

| type | shaders | median | modal |
|---|---|---|---|
| 0 Default | 2961 | **80.0** | 80.0 (1333) |
| 6 HairTint | 761 | 10.0 | 10.0 |
| 16 EyeEnvmap | 233 | 479.0 | 479.0 |
| 4 FaceTint | 223 | 33.0 | 33.0 |
| 5 SkinTint | 143 | 64.0 | 64.0 |
| 2 GlowMap | 39 | 80.0 | 80.0 |

Within type 0, 80 is the modal value in **12 of 15** top folders — architecture
613/1092, dungeons, clutter, armor, clothes, landscape, weapons, traps, and
100% of plants, furniture and animobjects. So **no per-category table is
needed**; what actors need is the correct shader TYPE, which we do not yet
write (see gaps).

🔴 **Do not copy the vanilla distribution, only its mode.** Arcane University
puts typical specular strength at 0.25–1.0; Bethesda's own meshes ship 2.2
(263×) and 3.0 (152×). The spread is per-artist noise, not a system.

## Oblivion's glossiness does not transfer

Nehrim source, 4031 `NiMaterialProperty` from 1200 meshes:

| | |
|---|---|
| glossiness median | **10.0**, with 59.4% sitting on exactly 10 |
| specular colour | (0.9,0.9,0.9) 39.9%, black 32.1%, white 13.1% |

10 is an authoring default, not a chosen value — and 10 in Skyrim is what HAIR
uses, a very wide highlight. Carrying it across would give every Nehrim surface
a hair-like sheen. **Glossiness is written as 80, never copied.**

The specular colours are equally uninformative: only 170 of 4038 shapes carry
`NiSpecularProperty`, and without it Gamebryo renders no specular at all, so
the colour on the other 95.8% was never used.

## The rule: slot 1's alpha decides

`asset_convert/spec_mask.py`. Both engines read the normal map's alpha as the
specular mask — Arcane University's slot table says so for Skyrim ("Black is
zero reflection, white full") and `landscape_normals.py` already relies on it
for terrain. It is the one piece of Oblivion's material authoring that
transfers intact and changes how a surface looks.

**Why it needs no height-style classifier.** A diffuse's alpha is ambiguous —
transparency OR height — which is why `parallax.classify_alpha` weighs mid-tone
ratios and level counts. Slot 1's alpha has no competing meaning. So:

| verdict | condition | `specular_strength` |
|---|---|---|
| `mask` | alpha present, >2 levels, amplitude ≥ 8 | **1.0** |
| `no_alpha` | DXT1 / uncompressed | 0.25 |
| `flat` | amplitude < 8 (a tool that saved DXT5 it never needed) | 0.25 |
| `binary` | two values — on/off, more likely a stray mask | 0.25 |

`SLSF1_Specular` stays **on in every case**. Switching it off would split the
world into shiny and dead surfaces along a line the player cannot read, and a
missing alpha reads as 1.0 in Skyrim, so the strength is what holds it back.

🔴 **Glossiness cannot do this job.** AU defines it as the INVERSE WIDTH of the
highlight: a low value gives a broad sheen over the whole surface, a high one a
small bright hotspot. Neither is "barely shines". Intensity is
`specular_strength`.

`NiSpecularProperty` is deliberately ignored: too rare (4.2%) to carry the
decision, and a shape with the property but no mask would render a uniform
sheen over its whole surface — worse than none.

### The yield tracks material reality

Share of `_n` maps carrying an alpha channel, per area:

```
armor 94.5%  weapons 89.9%  rocks 75.0%  dungeons 67.9%  nehrim 63.6%
clutter 45.9%  landscape 45.9%  architecture 38.1%  plants 15.3%
```

That ordering is not noise — metal and leather reflect, plaster and leaves do
not. The artists drew masks where the material warrants one, which is why the
uneven distribution is authored intent rather than a defect to smooth over.

## Interaction with `landscape_normals`

`textures/…/landscape` serves BOTH terrain (from `LAND` records, which has no
mesh and therefore no shader property we could set) and object meshes such as
rocks. Classified with our own rule, Nehrim's 135 landscape normals are
**58 `mask`, 73 `no_alpha`, 4 `binary`**.

| case | texture stage | mesh stage | result |
|---|---|---|---|
| mask (58) | untouched — the fix only rewrites DXT1 | strength 1.0 | authored mask, applied once ✅ |
| DXT1 (73) | → DXT5 with alpha 32/255 = 0.125 | strength 0.25 | 0.03 — **doubly damped** |
| binary (4) | untouched | strength 0.25 | 0.25 |

The masked case resolves itself correctly with no extra work, and the fix is
mod-safe by construction: it bails on anything that is not DXT1, so a mod's
real specular map can never be overwritten.

The DXT1 double-damping is a known, deliberately unfixed overlap — both stages
independently solve the same problem. Left in place pending an in-game look,
because a too-matte rock is far less noticeable than shiny ground. If it does
show, the precise fix is to narrow `landscape_normals` to the textures `LTEX`
records actually name, so each stage only touches what it owns.

## What the source does NOT carry

Measured on 1200 Nehrim meshes / 4038 shapes:

| signal | present | status |
|---|---|---|
| `NiStencilProperty` → `SLSF2_Double_Sided` | 3.5% | mapped |
| emissive colour | 7.6% | mapped |
| vertex colour `lighting_mode=0` → effect shader | 1.3% | mapped |
| `apply_mode=4` → parallax | 6.7% | mapped (opt-in) |
| **glow texture slot** | **0.7%**, 588 `_g` files exist | **dropped** |
| `apply_mode=3` (HILIGHT) | 3.1% | unexplained |

No dark, detail, gloss or decal slots. No `BSShader*Property`. **No `_e`
cubemaps at all**, so environment mapping cannot be reconstructed — the 12 `_m`
masks have nothing to mask.

Root node type is not a discriminator either: 89% of Oblivion roots are plain
`NiNode`. `BSFadeNode` / `BSLeafAnimNode` / `BSTreeNode` are Skyrim-side
distinctions the converter must CHOOSE, not read. BSXFlags describe physics and
animation, not surface.

The Havok material on the collision body does describe what a surface is made
of, and `collision.py` already translates it — but only for physics. Note it is
per rigid BODY, not per shape: a first census read 30% "Skin" until it turned
out all of it came from one goblin ragdoll skeleton with 18 bodies.

## Known gaps

- **Actor shader types.** We write type 0 for skin, hair and faces; vanilla
  uses 4/5/6/16 with quite different glossiness. This is the real actor defect,
  not the glossiness value.
- **Glow.** 588 `_g` textures exist and slot 2 has a direct equivalent
  (type 2 + `SLSF2_Glow_Map`, with the env-map flag off — AU says the two are
  mutually exclusive). Currently dropped entirely.
- **Trees.** Leaf flutter needs a `TREE`/`FLOR` record *and* a `BSLeafAnimNode`
  root *and* three flags; skinned branches crash on load if the bone arrays are
  empty. Vanilla puts soft lighting on 102 of 364 tree shaders with
  `Lighting Effect 1` around 7.5. Deferred.

## Prior art: the guards PGPatcher applies (read 2026-08-20, not adopted)

[PGPatcher](https://github.com/hakasapl/PGPatcher) has been patching parallax
across Skyrim load orders for years, and its wiki lists exactly when it
**refuses** to. We set parallax on `APPLY_HILIGHT2` + usable height data with a
single guard (`_is_lod_tier_mesh`), so this list is worth measuring against:

* mesh has attached havok — `BSBehaviorGraphExtraData` present
* shape is skinned
* shape has an `NiAlphaProperty`
* shader flags `decal` or `dynamic_decal`
* shader flags `soft lighting`, `rim lighting`, `back lighting`, or
  `anisotropic lighting`
* the mesh is used as a `GRAS` record, or has single-pass `MATO`
* shader type before patching is not `default`, `parallax`, or `environment`

Two of those we satisfy incidentally: the alpha property is dropped for
HILIGHT2 shapes anyway, and we never write the lighting-effect flags. **Skinned
shapes and havok-graph meshes are unchecked** — whether we currently set
parallax on any is unmeasured, and that is the measurement to run before
copying the rule.

Its Complex Material patcher also sets `specular strength` to 1.0, but only
where a CM texture exists — a richer texture type Oblivion content does not
have, which is why the normal-alpha rule here has no overlap with it.
