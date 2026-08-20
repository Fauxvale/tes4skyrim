"""Per-worldspace world-map cloud bank meshes.

WHY THIS EXISTS
---------------
Skyrim's world map draws a bank of cloud planes over the terrain.  The mesh is
chosen by a three-step fallback in the engine (SkyrimSE.exe, function at RVA
0x2c7e00):

    1. the PARENT worldspace's cloud model, when WRLD PNAM bit 2
       ("Use Map Data") is set;
    2. this worldspace's own WRLD `MODL` ("Cloud Model" in xEdit's TES5 WRLD
       definition, wbRStruct('Cloud Model', [wbGenericModel]));
    3. neither set (empty string) -> a HARDCODED default.  The tail of that
       function is

           cmp  byte ptr [rax], 0
           jne  <return it>
           lea  rax, [rip + 0x1397ef1]      ; -> 0x14165fd50

       and 0x14165fd50 is the string `Meshes\\Sky\\SkyrimWorldMapCloudBank.nif`.
       That is the ONLY cross-reference to it in the binary.

Oblivion has no world-map cloud layer to convert from, and vanilla Skyrim
authors no MODL either (0 of 35 uncompressed Skyrim.esm WRLDs carry one) -- so
every converted worldspace lands on step 3 and inherits a cloud bank sized for
Skyrim's Tamriel.

The bank is four flat XY sheets, each spanning +/-56902.8 local units at node
scale 8.0, i.e. 910,445 units (222 cells) across.  Against the worldspaces we
actually emit that ranges from roughly right to wildly oversized:

    Skyrim Tamriel      119 x  94 cells   ~1.9x cover   (what Bethesda tuned)
    TES4Tamriel         134 x 129 cells   ~1.7x
    NehrimWorldspace     92 x 101 cells   ~2.2x
    Arktwend             39 x  25 cells   ~5.7x
    ErothinFeste         16 x  26 cells   ~8.5x

so the small worldspaces get a cloud deck many times their landmass and it
reads as a solid overcast sheet rather than scattered banks.

WHAT THIS DOES
--------------
Emit one cloud-bank NIF per worldspace, scaled so the sheet covers that
worldspace's own NAM0/NAM9 rectangle at the same proportion Bethesda's covers
Skyrim's Tamriel, and point the WRLD's MODL at it.

Only the X/Y span is scaled.  The per-child Z offsets (0 / 1000 / 1500 / 12500)
are cloud ALTITUDES -- scaling them with the horizontal span would sink the
deck into the terrain on a small worldspace and launch it out of frame on a
large one.  Scaling is applied to each child node's `scale` (the four sheets
sit at scale 8.0 under a scale-1.0 BSFadeNode root), which leaves vertex data,
shader properties and the alpha-fade controllers untouched.
"""

import os

from pyffi.formats.nif import NifFormat

# Bethesda's bank: half-extent 56902.8 local * node scale 8.0.
_STOCK_NODE_SCALE = 8.0
_STOCK_HALF_EXTENT = 56902.8 * _STOCK_NODE_SCALE

# SIZE IS SET BY THE LAND, using Bethesda's own deck-to-land ratio.
#
# The stock deck spans 910445 and Skyrim's land is 487424 x 385024, so it is
# 1.868x that land on X and 2.365x on Y.  A converted worldspace's deck is
# given the same relationship to ITS land, per axis, so a portrait worldspace
# gets a portrait deck instead of a square one sized off its long side.
#
# The control that justifies this over the alternatives: feed it Skyrim's own
# land and it returns exactly 8.0 / 8.0, reproducing the shipped mesh.  A rule
# built instead from where the sheet's opaque band falls (t ~ 0.4-0.7 of the
# half-extent, measured area-weighted) has to explain why vanilla Skyrim's own
# land sits 2.7x outside its clear middle -- Bethesda lets the band cover
# Tamriel's unplayable border cells -- and any threshold that "fixes" that for
# Skyrim stops being measurable for a worldspace with no such border.
#
# For reference, measured on the stock sheet (area-weighted mean alpha per
# Chebyshev shell), in case a future change needs the band's real position:
#     t 0.0-0.2  0.08-0.13 clear | t 0.2-0.4  0.38-0.55 ramp
#     t 0.4-0.7  0.73-0.89 BAND  | t 0.7-0.9  0.47->0.33 fading out
_SKYRIM_LAND_X, _SKYRIM_LAND_Y = 487424.0, 385024.0
_DECK_OVER_LAND_X = (_STOCK_HALF_EXTENT * 2.0) / _SKYRIM_LAND_X   # 1.868
_DECK_OVER_LAND_Y = (_STOCK_HALF_EXTENT * 2.0) / _SKYRIM_LAND_Y   # 2.365

_SOURCE_REL = 'meshes\\sky\\skyrimworldmapcloudbank.nif'

# Where generated banks go, relative to `meshes\`.  Under the converter's
# `tes4\` namespace like every other shipped asset, and NOT in `sky\`, so a
# generated bank can never shadow the vanilla file for the SKY renderer (the
# same folder holds clouds.nif etc. that the weather system loads by name).
_OUT_DIR = 'tes4\\worldmapclouds'


def cloud_model_path(editor_id: str) -> str:
    """MODL value for a worldspace's generated cloud bank.

    MODL is relative to `meshes\\` and does NOT include it (vanilla writes e.g.
    `LoadScreenArt\\LoadScreenMRaltar01.nif`).  Mirrors what generate_cloud_bank
    writes on disk, so the record writer and the asset writer can never
    disagree about the name.
    """
    return '%s\\%s.nif' % (_OUT_DIR, editor_id.lower())


def compute_axis_scales(reach_x: float, reach_y: float) -> tuple:
    """Per-axis (sx, sy) from Bethesda's own deck-to-land ratio.

    reach_x/reach_y: distance from the deck's CENTRE to the farthest land edge
    on that axis.  Not half the land span -- the sheet is symmetric about its
    own centre, so what matters is the longer side.

    Each axis is given the same deck-to-land relationship the stock sheet has
    to Skyrim's own land, so feeding this Skyrim's land reproduces the stock
    8.0 on both axes exactly.  That control is the reason to prefer this over
    a rule derived from where the sheet's opaque band happens to fall: any
    such rule has to explain why vanilla Skyrim violates it by 2.7x, and this
    one simply doesn't need to.

    Independent axes matter: the stock sheet is SQUARE, and a single node scale
    can only size it off the longer side, which on a portrait worldspace like
    Nehrim hangs more cloud across the short axis than the tall one needs.

    Returned as multipliers of the stock 56902.8 half-extent, i.e. directly
    comparable to the stock node scale of 8.0.
    """
    if reach_x <= 0.0 or reach_y <= 0.0:
        return (8.0, 8.0)
    sx = (reach_x * _DECK_OVER_LAND_X) / 56902.8
    sy = (reach_y * _DECK_OVER_LAND_Y) / 56902.8
    return (sx, sy)


_CELL = 4096.0


def framed_rect(nw_x, nw_y, se_x, se_y):
    """World-unit (min_x, min_y, max_x, max_y) of the region the map frames.

    MNAM stores the map's NW and SE CELL corners.  NW is the top-left, so it
    holds the smaller X and the LARGER Y; SE holds the larger X and the smaller
    Y.  Returned min/max are normalised so the caller never has to care.

    The SE cell is inclusive -- the map frames through the far edge of that
    cell, not up to its near edge -- so the span runs to (se_x + 1) cells.

    Returns None when the corners are missing or degenerate, so the caller can
    fall back to the NAM0/NAM9 rectangle.
    """
    if None in (nw_x, nw_y, se_x, se_y):
        return None
    min_x, max_x = sorted((float(nw_x), float(se_x) + 1.0))
    min_y, max_y = sorted((float(se_y), float(nw_y) + 1.0))
    if max_x <= min_x or max_y <= min_y:
        return None
    return (min_x * _CELL, min_y * _CELL, max_x * _CELL, max_y * _CELL)


def compute_center(min_x: float, min_y: float,
                   max_x: float, max_y: float) -> tuple:
    """World-unit (x, y) the sheet must sit over: the rectangle's midpoint.

    A worldspace's NAM0/NAM9 rectangle is NOT centred on the worldspace origin
    -- it is wherever its author laid the terrain out.  The stock bank IS
    origin-centred (every sheet node has translation x=y=0 and vertices
    symmetric about zero, verified against the shipped mesh), so scaling alone
    leaves the deck sitting over (0,0) while the landmass sits somewhere else,
    and the terrain on the far side of the origin runs out from under the
    clouds.

    NehrimWorldspace is the reported case: NAM0 (-266240,-188416) to NAM9
    (110592,225280), midpoint (-77824, 18432).  The origin-centred deck hangs
    east and north, leaving the WEST and SOUTH terrain bare -- exactly the two
    edges seen in game.  16 of 34 Nehrim worldspaces and 31 of 84 Oblivion
    worldspaces are not covered by an origin-centred sheet at their own scale.
    """
    return ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)


def _rescale_and_flatten(data, scales, keep: float, center=(0.0, 0.0)):
    """Stretch, centre and flatten every sheet; `scales` is (sx, sy).

    Operates on a parsed graph, NOT on raw bytes.  Byte-patching is not an
    option here: the BSAs ship this mesh in SSE BSTriShape form (96,851 bytes,
    half-float packed vertices) while `references/Skyrim Meshes` holds the LE
    NiTriShape form (182,953 bytes).  A patch written against either layout
    silently no-ops on the other, which is exactly what happened to the first
    version of this function.  sse_nif.read_nif normalises both to LE
    NiTriShape, so the edit is done on the graph and written out LE.

    Scale: X and Y are stretched INDEPENDENTLY (see compute_axis_scales), so
    the deck can match a map whose aspect differs from Skyrim's.  A NIF node
    `scale` is a single float and cannot express that, so the stretch is baked
    into the VERTICES and each node's scale is set to 1.0 -- the node scale and
    the vertex scale multiply, so leaving it at the stock 8.0 would apply the
    factor twice.  Only the horizontal axes are touched; vertex Z (the sheet's
    own relief) and the nodes' Z translations (cloud ALTITUDES) are preserved.

    Centre: the sheet nodes' X/Y translations are set to `center`.  Stock is
    (0,0) on all four, and the root above them is an identity-transform
    BSFadeNode at scale 1.0, so a node translation is already in world units --
    no division by the parent scale, and the node's own scale applies to its
    vertices, not to its translation.  Setting rather than adding is safe for
    the same reason the stock values are all zero, and keeps the operation
    idempotent if the mesh is ever regenerated from a previous output.

    Flatten: the stock sheets are not flat.  Each carries billowing Z relief --
    up to 3523 local units (~28,000 world units at scale 8) over the interior,
    with a skirt dropping to -899 at the rim.  That relief is modelled for
    Skyrim's terrain, most visibly the bank piled around High Hrothgar; over a
    converted worldspace it is a mountain of cloud on unrelated flat land.
    `keep` is the fraction retained (0.0 = flat, 1.0 = untouched), applied
    about each shape's MEDIAN z so the sheet settles onto its own base plane
    instead of being dragged to local zero -- the rim skirt is part of the
    silhouette, and collapsing everything to 0 would flare it up into the deck.

    Returns (n_nodes_scaled, n_shapes_flattened).
    """
    sx, sy = scales
    scaled = flattened = 0
    for root in data.roots:
        for block in root.tree():
            if not isinstance(block, NifFormat.NiTriShape):
                continue
            # The stretch lives in the vertices, so the node must not scale
            # them a second time.
            block.scale = 1.0
            # Z is the cloud ALTITUDE and is deliberately preserved.
            block.translation.x = center[0]
            block.translation.y = center[1]
            scaled += 1
            shape_data = block.data
            verts = shape_data.vertices if shape_data else None
            if not verts:
                continue
            # Stock vertices are symmetric about local (0,0), so a plain
            # multiply stretches about the sheet's own centre.
            for v in verts:
                v.x *= sx
                v.y *= sy

            # THE CLOUDS ARE A TEXTURE, NOT VERTEX ALPHA.  Every sheet samples
            # textures\sky\SkyrimCloudsMap01.dds, so the visible pattern -- the
            # open middle and the dense band around it -- lives in the UVs.
            # Stretching vertices alone leaves the UV range untouched, which
            # pins that dense band to the same FRACTION of the sheet no matter
            # how big the sheet gets: on a worldspace shaped unlike Skyrim's it
            # lands on playable land, and no amount of rescaling moves it.
            #
            # Scaling UVs by the SAME factor as the vertices keeps texel
            # density constant in world units, so one cloud stays one cloud and
            # the dense band stays out at the sheet's rim where Bethesda put it.
            # The stock ranges run outside 0..1 (u -8.2..11.7) and the sampler
            # is set to wrap (texture_clamp_mode 65283), so the pattern tiles
            # and a wider range simply shows more of it -- no clamping artefact
            # at the edges.
            # The factor is the change in WORLD span, not the node scale: the
            # vertices already carried the stock node scale of 8.0 once it was
            # baked in, so scaling UVs by sx/sy directly would over-tile by
            # 8x.  world_new / world_stock = sx / 8.0 per axis.
            uv_sets = getattr(shape_data, 'uv_sets', None)
            if uv_sets:
                fu = sx / _STOCK_NODE_SCALE
                fv = sy / _STOCK_NODE_SCALE
                for uvs in uv_sets:
                    for uv in uvs:
                        uv.u *= fu
                        uv.v *= fv
            if keep < 1.0:
                zs = sorted(v.z for v in verts)
                mid = zs[len(zs) // 2]
                for v in verts:
                    v.z = mid + (v.z - mid) * keep
                flattened += 1
            # Bounding sphere must follow the geometry or the engine can cull
            # the sheet against a volume it no longer occupies.  Unconditional:
            # the X/Y stretch above always changes the extent, even when no
            # flattening is requested.
            shape_data.update_center_radius()
    return scaled, flattened


def generate_cloud_bank(editor_id: str, width: float, height: float,
                        out_root: str, flatten: float = 0.0,
                        center=(0.0, 0.0), land_rect=None,
                        write: bool = True) -> str:
    """Write a scaled, centred cloud bank for one worldspace; return its MODL path.

    land_rect: (min_x, min_y, max_x, max_y) of the worldspace's REAL LAND, in
    world units.  This is what the sizing is driven from -- the sheet is grown
    until its clear middle reaches the farthest land edge, so the opaque band
    lands beyond the terrain.  When given it supersedes width/height entirely.

    width/height: legacy span-based sizing, kept for callers that have no land
    rectangle to offer.
    out_root: the plugin's output folder (the one that holds `meshes\\`).
    center: world-unit (x, y) the deck is centred on.

    write: False computes and validates the bank but writes no file,
    returning the MODL path it WOULD have written. The mesh is ONE file at
    a fixed path shared by every plugin in a worldspace, so the per-plugin
    copies were rival versions of it -- each sized to its own bounds, the
    install order picking a winner. `sibling_lod.merge_cloud_bank` writes
    the single authoritative copy, sized to the UNION of every sibling's
    land, into the LOD mod that installs last. MODL is the same string
    either way, and every validity check above still runs, so a worldspace
    whose bank cannot be built returns None here exactly as before.

    Returns None when the vanilla source mesh is unavailable, so the caller
    simply omits MODL and the engine falls back to its own default -- exactly
    today's behaviour, never a broken model reference.
    """
    from .skyrim_assets import get_asset_bytes
    from .sse_nif import read_nif

    raw = get_asset_bytes(_SOURCE_REL)
    if not raw:
        return None

    # read_nif normalises the BSA's SSE BSTriShape graph to LE NiTriShape and
    # marks the data LE, so the write below produces an LE NIF (uv2=83), which
    # SSE loads natively.
    data = read_nif(raw)
    if land_rect:
        # Reach from the deck's centre to the farthest land edge on each axis.
        mnx, mny, mxx, mxy = land_rect
        reach_x = max(abs(mnx - center[0]), abs(mxx - center[0]))
        reach_y = max(abs(mny - center[1]), abs(mxy - center[1]))
    else:
        reach_x, reach_y = width / 2.0, height / 2.0
    scaled, _ = _rescale_and_flatten(data,
                                     compute_axis_scales(reach_x, reach_y),
                                     flatten, center)
    if scaled == 0:
        # Not the layout we verified against -- ship nothing rather than a
        # mesh we may have mangled.
        return None

    rel = cloud_model_path(editor_id)
    if not write:
        return rel
    # MODL omits the `meshes\` prefix; the file on disk needs it.
    dest = os.path.join(out_root, 'meshes', *rel.split('\\'))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, 'wb') as fh:
        data.write(fh)
    return rel
