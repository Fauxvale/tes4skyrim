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
_STOCK_HALF_EXTENT = 56902.8 * 8.0

# Skyrim's Tamriel (Skyrim.esm WRLD 0x3C NAM0/NAM9): 487424 x 385024 units.
# The stock bank spans 910445, so Bethesda covers the LARGER worldspace axis
# 910445/487424 = 1.868x.  Reproducing that ratio is the whole design intent:
# it keeps the big converted worldspaces looking exactly as they do now and
# only pulls the deck in on the small ones.
_STOCK_COVER_RATIO = (_STOCK_HALF_EXTENT * 2.0) / 487424.0

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


def compute_scale(width: float, height: float) -> float:
    """Child-node scale that covers a `width` x `height` worldspace.

    Returns the absolute node scale (the stock file uses 8.0), not a
    multiplier.  Sized off the LARGER axis so the sheet always covers the
    whole rectangle -- the bank is square, so fitting the short axis would
    leave the long one bare.
    """
    span = max(width, height)
    if span <= 0.0:
        return 8.0
    target_half = (span * _STOCK_COVER_RATIO) / 2.0
    return target_half / 56902.8


def _rescale_and_flatten(data, scale: float, keep: float):
    """Set every sheet node's scale, and flatten its geometry's Z relief.

    Operates on a parsed graph, NOT on raw bytes.  Byte-patching is not an
    option here: the BSAs ship this mesh in SSE BSTriShape form (96,851 bytes,
    half-float packed vertices) while `references/Skyrim Meshes` holds the LE
    NiTriShape form (182,953 bytes).  A patch written against either layout
    silently no-ops on the other, which is exactly what happened to the first
    version of this function.  sse_nif.read_nif normalises both to LE
    NiTriShape, so the edit is done on the graph and written out LE.

    Scale: applied to the four sheet nodes (stock value 8.0), leaving the
    BSFadeNode root at 1.0.  Only the horizontal span changes; the nodes' Z
    translations are cloud ALTITUDES and are never touched.

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
    scaled = flattened = 0
    for root in data.roots:
        for block in root.tree():
            if not isinstance(block, NifFormat.NiTriShape):
                continue
            block.scale = scale
            scaled += 1
            shape_data = block.data
            verts = shape_data.vertices if shape_data else None
            if not verts:
                continue
            if keep < 1.0:
                zs = sorted(v.z for v in verts)
                mid = zs[len(zs) // 2]
                for v in verts:
                    v.z = mid + (v.z - mid) * keep
                # Bounding sphere must follow the geometry or the engine can
                # cull the sheet against a volume it no longer occupies.
                shape_data.update_center_radius()
                flattened += 1
    return scaled, flattened


def generate_cloud_bank(editor_id: str, width: float, height: float,
                        out_root: str, flatten: float = 0.0) -> str:
    """Write a scaled cloud bank for one worldspace; return its MODL path.

    out_root: the plugin's output folder (the one that holds `meshes\\`).
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
    scaled, _ = _rescale_and_flatten(data, compute_scale(width, height),
                                     flatten)
    if scaled == 0:
        # Not the layout we verified against -- ship nothing rather than a
        # mesh we may have mangled.
        return None

    rel = cloud_model_path(editor_id)
    # MODL omits the `meshes\` prefix; the file on disk needs it.
    dest = os.path.join(out_root, 'meshes', *rel.split('\\'))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, 'wb') as fh:
        data.write(fh)
    return rel
