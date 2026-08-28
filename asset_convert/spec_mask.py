r"""Does a normal map carry a SPECULAR MASK?

Slot 1's alpha channel is the specular map in both engines -- Arcane
University's texture-slot table states it outright for Skyrim ("For specular in
the alpha channel ... Black is zero reflection, white full"), and
`asset_convert/landscape_normals.py` already relies on the same fact for
terrain.  It is the one part of Oblivion's material authoring that transfers to
Skyrim intact and makes a visible difference, so it -- not the mesh's
`NiSpecularProperty` -- decides whether a shape gets specular.

**The mesh property is deliberately ignored.**  Measured on Nehrim: only 170 of
4038 shapes carry `NiSpecularProperty` (4.2%), while ~58% of normal maps carry
an alpha channel.  And a shape with the property but no mask would render a
uniform sheen over its whole surface, which is worse than none -- the property
alone cannot carry the decision.

**Why this needs no height-style classifier.**  A diffuse's alpha is ambiguous:
transparency OR height, which is why `parallax.classify_alpha` has to weigh
mid-tone ratios and level counts before it dares call something a height field.
Slot 1's alpha has no competing meaning.  Nothing else lives there.  So the
test is only: is there an alpha channel, and does it actually vary?

Three rejections, and they are the whole rule:

  no alpha   DXT1 or uncompressed -- 39.6% of Nehrim's normal maps.  Skyrim
             reads a missing alpha as 1.0 = FULL specular everywhere, the exact
             bug `landscape_normals` fixes for terrain, so these must not get
             the flag.
  flat       amplitude below `MIN_RANGE`: a compression artefact, a tool that
             saved DXT5 without needing alpha and filled it with 255.
  binary     two distinct values: on/off with no modulation, far more likely a
             stray mask than an authored specular map.

`parallax.classify_alpha` supplies the raw statistics (`rng`, `levels`, `fmt`);
only its VERDICT is height-calibrated and is not used here.  In particular
`_MIN_LEVELS = 64` rightly rejects every DXT3 source as a height field --
16 levels of height is visible terracing -- while 16 levels of specular MASK is
perfectly ordinary, and DXT3 normal maps are 25% of the tree.
"""

MIN_RANGE = 8      # peak-to-peak alpha below this is flat, not a mask
MIN_LEVELS = 3     # two values is on/off, not modulation


def verdict(info) -> str:
    """`parallax.AlphaInfo` -> 'mask' | 'no_alpha' | 'flat' | 'binary'."""
    if info is None:
        return 'no_alpha'
    if info.kind in ('no_alpha', 'unreadable'):
        return 'no_alpha'
    if info.levels < MIN_LEVELS:
        return 'binary'
    if info.rng < MIN_RANGE:
        return 'flat'
    return 'mask'


def classify_bytes(data: bytes) -> str:
    """Verdict straight from a DDS file's bytes."""
    from . import parallax
    if not data:
        return 'no_alpha'
    return verdict(parallax.classify_alpha(data))


def has_mask(data: bytes) -> bool:
    return classify_bytes(data) == 'mask'
