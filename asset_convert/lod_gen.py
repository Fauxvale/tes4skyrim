"""
LOD generation for converted TES4→TES5 worldspaces.

Workflow:
  1. write_lod_settings()  — write LODSettings/<WRLD>.lod (required by LODGen.exe)
  2. write_lodgen_input()  — scan the converted ESM, emit the LODGen data text file
  3. run_lodgen()          — call LODGenx64.exe to bake object LOD NIFs

All three are orchestrated by generate_lod(), which convert.py calls as Phase 4.
"""

import math
import os
import re as _re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))
from subprocess_flags import POPEN_FLAGS  # noqa: E402

LODGEN_EXE = (
    SCRIPT_DIR / "external" / "lodgen" / "LODGenx64.exe"
)


# ---------------------------------------------------------------------------
# 1. LODSettings file
#
# Format: little-endian binary
#   int16  SW cell X
#   int16  SW cell Y
#   int16  NE cell X  (or width — docs are unclear; use NE)
#   int16  NE cell Y
#
# LODGen.pas reads SWCellX/SWCellY at offset 0 (TES5 game mode).
# The game also reads this file to know the extent of terrain LOD tiles.
# ---------------------------------------------------------------------------

def write_lod_settings(worldspace_edid: str, sw_x: int, sw_y: int,
                       ne_x: int, ne_y: int, output_dir: Path) -> tuple:
    """Write LODSettings/<worldspace_edid>.lod.

    16-byte format (TES5):
      int16  SW cell X
      int16  SW cell Y
      uint32 grid width  (NE_X - SW_X, rounded up to power of 2)
      uint32 min LOD level  (always 4)
      uint32 max LOD level  (always 32)

    Returns (path, effective_sw_x, effective_sw_y) so callers can use the
    same SW coordinates in the LODGen CellSW= header line.
    """
    lod_dir = output_dir / "LODSettings"
    lod_dir.mkdir(parents=True, exist_ok=True)
    out = lod_dir / f"{worldspace_edid}.lod"

    # Round SW down and NE up to the nearest power of 2 boundary
    raw_w = ne_x - sw_x
    raw_h = ne_y - sw_y
    size = 1 << math.ceil(math.log2(max(raw_w, raw_h, 1)))
    # Centre the grid: expand SW symmetrically
    eff_sw_x = -(size // 2)
    eff_sw_y = -(size // 2)

    out.write_bytes(struct.pack("<hhIII", eff_sw_x, eff_sw_y, size, 4, 32))
    print(f"  Wrote {out}")
    return out, eff_sw_x, eff_sw_y


# ---------------------------------------------------------------------------
# 2. Parse the converted ESM to build the LODGen input text file.
#
# LODGen input format (from LODGen.pas reverse engineering):
#
#   Header lines (key=value):
#     GameMode=TES5
#     Worldspace=<EditorID>
#     CellSW=<x> <y>
#     PathData=<tes5 data dir>
#     PathOutput=<output meshes dir>
#     Resource=<bsa path>    (0 or more)
#
#   Data lines (tab-separated, one per REFR):
#     <FormID hex>  <RecordFlags hex>  <X>  <Y>  <Z>  <rX>  <rY>  <rZ>  <scale>
#         <EDID>  <StatFlags hex>  <material>  <full mesh>  <lod4 mesh>  <lod8 mesh>  <lod16 mesh>
#
# We generate LOD for:
#   - STAT/ACTI/MSTT/TREE references in exterior cells of the worldspace
#   - whose base object model path has a companion _far.nif in the output tree
#   - OR whose base STAT record has MNAM LOD entries
#
# In practice for converted Oblivion content: the _far.nif files were skipped
# by bsa_extract.  We use _far.nif as LOD4/LOD8/LOD16 if it exists, otherwise
# use the full model as the LOD mesh (LODGen will simplify it).
# ---------------------------------------------------------------------------

# ESM binary constants (TES5)
_REC_HDR   = 24
_GRP_HDR   = 24
_SUB_HDR   = 6
_FLAG_COMP       = 0x00040000
_FLAG_DISTANT_LOD  = 0x00008000   # Has Distant LOD — SSELodGen bakes LOD for this object
_FLAG_WORLD_MAP    = 0x10000000   # Show in World Map — object appears on the world map
_FLAG_PERSISTENT = 0x00000400  # on REFR


def _sub(subrecords, tag):
    for s in subrecords:
        if s[0] == tag:
            return s[1]
    return None


def _parse_subrecords(data: bytes):
    subs = []
    pos = 0
    while pos + _SUB_HDR <= len(data):
        tag  = data[pos:pos+4].decode('ascii', errors='replace')
        size = struct.unpack_from('<H', data, pos+4)[0]
        pos += _SUB_HDR
        subs.append((tag, data[pos:pos+size]))
        pos += size
    return subs


def _read_record(data: bytes, pos: int):
    if pos + _REC_HDR > len(data):
        return None, pos
    sig       = data[pos:pos+4].decode('ascii', errors='replace')
    data_size = struct.unpack_from('<I', data, pos+4)[0]
    flags     = struct.unpack_from('<I', data, pos+8)[0]
    form_id   = struct.unpack_from('<I', data, pos+12)[0]
    end       = pos + _REC_HDR + data_size

    raw = data[pos+_REC_HDR:end]
    if flags & _FLAG_COMP and len(raw) >= 4:
        import zlib
        try:
            raw = zlib.decompress(raw[4:])
        except Exception:
            pass

    subs = _parse_subrecords(raw)
    return {'sig': sig, 'flags': flags, 'form_id': form_id, 'subs': subs}, end


def _zstr(b: bytes) -> str:
    return b.rstrip(b'\x00').decode('latin-1', errors='replace')


def _parse_esm(esm_path: Path):
    """
    Minimal ESM parser. Returns dicts:
      worldspaces: {form_id: {edid, mnam_sw_x, mnam_sw_y, mnam_ne_x, mnam_ne_y}}
      cells:       {form_id: {parent_wrld, grid_x, grid_y}}
      stats:       {form_id: {edid, flags, model, lod4, lod8, lod16}}
      refs:        [{form_id, flags, base_fid, parent_wrld, parent_cell,
                     x,y,z, rx,ry,rz, scale}]
    """
    raw = esm_path.read_bytes()
    n   = len(raw)

    worldspaces = {}
    cells       = {}
    stats       = {}
    refs        = []

    # We do a single linear scan using a recursive group parser.
    pos = 0
    # Skip file header (first record)
    if n < _REC_HDR:
        return worldspaces, cells, stats, refs
    hdr_size = struct.unpack_from('<I', raw, 4)[0]
    pos = _REC_HDR + hdr_size

    def parse_group(start, end, parent_wrld, parent_cell):
        nonlocal pos
        p = start + _GRP_HDR
        grp_type = struct.unpack_from('<I', raw, start+12)[0]
        label    = raw[start+8:start+12]

        pw = parent_wrld
        pc = parent_cell
        if grp_type == 1:                   # world children
            pw = struct.unpack_from('<I', label)[0]
        elif grp_type in (6, 8, 9, 10):     # cell children
            pc = struct.unpack_from('<I', label)[0]

        while p < end and p < n:
            if p + 4 > n:
                break
            sig4 = raw[p:p+4]
            if sig4 == b'GRUP':
                if p + _GRP_HDR > n:
                    break
                g_size = struct.unpack_from('<I', raw, p+4)[0]
                parse_group(p, p + g_size, pw, pc)
                p += g_size
            else:
                rec, next_p = _read_record(raw, p)
                if rec is None:
                    break
                _dispatch(rec, pw, pc)
                if rec['sig'] == 'CELL':
                    pc = rec['form_id']
                elif rec['sig'] == 'WRLD':
                    pw = rec['form_id']
                p = next_p

    def _dispatch(rec, pw, pc):
        sig = rec['sig']
        fid = rec['form_id']
        subs = rec['subs']

        if sig == 'WRLD':
            edid = _zstr(_sub(subs, 'EDID') or b'')
            sw_x = sw_y = ne_x = ne_y = 0
            mnam = _sub(subs, 'MNAM')
            if mnam and len(mnam) >= 16:
                # MNAM: usable dim X(i16), Y(i16), NW_x(i16), NW_y(i16), SE_x(i16), SE_y(i16), ...
                # Layout: usableX(i32), usableY(i32), NWcell_x(i16), NWcell_y(i16),
                #         SEcell_x(i16), SEcell_y(i16)
                nw_x = struct.unpack_from('<h', mnam, 8)[0]
                nw_y = struct.unpack_from('<h', mnam, 10)[0]
                se_x = struct.unpack_from('<h', mnam, 12)[0]
                se_y = struct.unpack_from('<h', mnam, 14)[0]
                # SW = min corners, NE = max corners
                sw_x = min(nw_x, se_x)
                sw_y = min(nw_y, se_y)
                ne_x = max(nw_x, se_x)
                ne_y = max(nw_y, se_y)
            worldspaces[fid] = {
                'edid': edid, 'sw_x': sw_x, 'sw_y': sw_y,
                'ne_x': ne_x, 'ne_y': ne_y,
            }

        elif sig == 'CELL':
            grid_x = grid_y = None
            xclc = _sub(subs, 'XCLC')
            if xclc and len(xclc) >= 8:
                grid_x = struct.unpack_from('<i', xclc, 0)[0]
                grid_y = struct.unpack_from('<i', xclc, 4)[0]
            cells[fid] = {'parent_wrld': pw, 'grid_x': grid_x, 'grid_y': grid_y}

        elif sig in ('STAT', 'ACTI', 'MSTT', 'TREE'):
            edid  = _zstr(_sub(subs, 'EDID') or b'')
            model = ''
            modl  = _sub(subs, 'MODL')
            if modl:
                model = _zstr(modl)
            # MNAM LOD entries (STAT only: sequence of MNAM subs with LOD mesh paths)
            lod4 = lod8 = lod16 = ''
            mnam_subs = [s for s in subs if s[0] == 'MNAM']
            if len(mnam_subs) >= 1:
                lod4 = _zstr(mnam_subs[0][1])
            if len(mnam_subs) >= 2:
                lod8 = _zstr(mnam_subs[1][1])
            if len(mnam_subs) >= 3:
                lod16 = _zstr(mnam_subs[2][1])
            # OBND bounds (for tree billboard sizing)
            obnd = _sub(subs, 'OBND')
            bounds = None
            if obnd and len(obnd) >= 12:
                bounds = struct.unpack_from('<6h', obnd)
            stats[fid] = {
                'edid': edid,
                'sig': sig,
                'flags': rec['flags'],
                'model': model,
                'obnd': bounds,
                'lod4': lod4, 'lod8': lod8, 'lod16': lod16,
            }

        elif sig == 'REFR':
            base_fid = 0
            name = _sub(subs, 'NAME')
            if name and len(name) >= 4:
                base_fid = struct.unpack_from('<I', name)[0]
            x = y = z = rx = ry = rz = 0.0
            data_sub = _sub(subs, 'DATA')
            if data_sub and len(data_sub) >= 24:
                x, y, z, rx, ry, rz = struct.unpack_from('<6f', data_sub)
            scale = 1.0
            xscl = _sub(subs, 'XSCL')
            if xscl and len(xscl) >= 4:
                scale = struct.unpack_from('<f', xscl)[0]
            refs.append({
                'form_id': fid, 'flags': rec['flags'], 'base_fid': base_fid,
                'parent_wrld': pw, 'parent_cell': pc,
                'x': x, 'y': y, 'z': z,
                'rx': rx, 'ry': ry, 'rz': rz,
                'scale': scale,
            })

    # Walk top-level GRUPs
    p = pos
    while p < n:
        if p + 4 > n:
            break
        if raw[p:p+4] != b'GRUP':
            break
        if p + _GRP_HDR > n:
            break
        g_size = struct.unpack_from('<I', raw, p+4)[0]
        parse_group(p, p + g_size, 0, 0)
        p += g_size

    return worldspaces, cells, stats, refs


# ---------------------------------------------------------------------------
# LOD mesh resolution helpers
# ---------------------------------------------------------------------------

def _far_nif_path(model_path: str) -> str:
    """Return the expected _far.nif path for a given model path."""
    if not model_path:
        return ''
    base = model_path
    if base.lower().endswith('.nif'):
        base = base[:-4]
    return base + '_far.nif'


def _normalize(path: str) -> str:
    """Normalize mesh path to lowercase backslash form with meshes\\ prefix.

    Paths in the converted ESM are stored without the 'meshes\\' prefix
    (e.g. 'tes4\\Architecture\\foo.nif').  LODGen expects paths relative to
    the Data folder (e.g. 'meshes\\tes4\\architecture\\foo.nif').
    """
    p = path.lower().replace('/', '\\').strip('\\')
    if p and not p.startswith('meshes\\'):
        p = 'meshes\\' + p
    return p


def _mesh_exists(path: str, output_meshes_dir: Path) -> bool:
    """Return True if a mesh file exists in the tes4 output meshes directory."""
    if not path:
        return False
    # Strip leading 'meshes\\' if present — output_meshes_dir IS the meshes root
    rel = path.lower().replace('/', '\\').lstrip('\\')
    if rel.startswith('meshes\\'):
        rel = rel[len('meshes\\'):]
    return (output_meshes_dir / rel).exists()


# LODGenx64 casts every LOD mesh's root block to NiNode without checking. A
# root that is a bare geometry block throws
# "InvalidCastException: Unable to cast NiTriShape to NiNode" on a worker
# thread, which is UNHANDLED — the process dies and the ENTIRE worldspace gets
# no object LOD at all (two 4-triangle scum meshes cost Morrowind_ob all
# 75,000 of its LOD references).  nif_converter now wraps geometry roots so
# converted meshes are safe, but stale files from an older run, hand-authored
# _far.nif meshes and anything a future source ships can still trip it, and
# the failure mode is far too expensive to risk.  Screening costs one small
# header read per unique mesh.
_NIF_ROOT_SAFE_CACHE = {}


def _lod_mesh_is_safe(path: str, output_meshes_dir: Path) -> bool:
    """False if this mesh's root block would crash LODGen's NiNode cast."""
    rel = path.lower().replace('/', '\\').lstrip('\\')
    if rel.startswith('meshes\\'):
        rel = rel[len('meshes\\'):]
    full = output_meshes_dir / rel
    key = str(full).lower()
    cached = _NIF_ROOT_SAFE_CACHE.get(key)
    if cached is not None:
        return cached

    safe = True
    try:
        from .lod_far_gen import NifFormat
        data = NifFormat.Data()
        with open(full, 'rb') as fh:
            data.read(fh)
        roots = data.roots
        if not roots or roots[0] is None:
            safe = False
        else:
            safe = isinstance(roots[0], NifFormat.NiNode)
    except Exception:
        # Unreadable here means unreadable for LODGen too — leave it out
        # rather than gamble the whole worldspace on it.
        safe = False
    _NIF_ROOT_SAFE_CACHE[key] = safe
    return safe


# Objects smaller than this (max OBND dimension, game units) are only baked
# into the near LOD-4 tiles.  A level-8 tile starts ~2 cells out; small
# clutter is invisible there but its baked geometry still costs disk/VRAM.
_LOD8_MIN_SIZE = 400.0


def _obnd_max_dim(stat: dict) -> float:
    obnd = stat.get('obnd')
    if not obnd:
        return 0.0
    x1, y1, z1, x2, y2, z2 = obnd
    return float(max(x2 - x1, y2 - y1, z2 - z1))


def _lod_meshes_for(stat: dict, output_meshes_dir: Path):
    """
    Return (lod4, lod8, lod16) mesh paths for a stat record.

    - Trees use their billboard-card _far.nif at every level — the cards are
      8 verts each, so distant forests stay visible for almost no cost.
    - Other LOD objects (0x8000) get lod4; lod8 only if they're big enough
      to matter at level-8 distances (_LOD8_MIN_SIZE).
    - World-map objects (0x10000000) additionally get lod16 so LODGenx64
      bakes tiles for the far ring / world-map view.
    """
    lod4  = stat.get('lod4', '')
    lod8  = stat.get('lod8', '')
    lod16 = stat.get('lod16', '')

    if lod4 or lod8 or lod16:
        return lod4, lod8, lod16

    model = stat.get('model', '')
    if not model:
        return '', '', ''

    far = _far_nif_path(model)
    if not _mesh_exists(far, output_meshes_dir):
        return '', '', ''

    from .lod_far_gen import is_tree_model, _tier_path, _TIER8, _TIER16
    if is_tree_model(stat):
        return far, far, far

    flags = stat.get('flags', 0)
    lod8_mesh = lod16_mesh = ''
    if _obnd_max_dim(stat) >= _LOD8_MIN_SIZE:
        far8 = str(_tier_path(Path(far), _TIER8['suffix']))
        lod8_mesh = far8 if _mesh_exists(far8, output_meshes_dir) else far
    if flags & 0x10000000:
        far16 = str(_tier_path(Path(far), _TIER16['suffix']))
        lod16_mesh = far16 if _mesh_exists(far16, output_meshes_dir) else far
    return far, lod8_mesh, lod16_mesh


# ---------------------------------------------------------------------------
# 3. Build the LODGen input text file
#
# Trees flow through the generic object path, but their _far.nif is a
# crossed-quad billboard card built from Oblivion's shipped billboard render
# (lod_far_gen.generate_tree_billboard_far) rather than decimated geometry —
# vanilla-style flat tree LOD, ~8 verts per instance.  (LODGen's own
# FlatTextures mechanism baked "objpassthru" card shapes into the .bto that
# never rendered in-game; real billboard NIFs use the proven object path.)
# ---------------------------------------------------------------------------


def write_lodgen_input(esm_path: Path, output_dir: Path,
                       worldspace_edid: str,
                       _parsed=None,
                       cell_sw: tuple = None,
                       master_dirs=None) -> Path:
    """
    Parse the converted ESM and write the LODGen input text file.

    `master_dirs` lists the converted output dirs of this plugin's MASTERS.
    An override plugin re-uses its masters' records wholesale, so every ref
    whose LOD mesh the master already ships is DROPPED here: the master's own
    LOD run already baked it, and re-baking it would have this plugin ship a
    duplicate copy of the master's entire object LOD to gain the handful of
    objects it actually introduces.

    Returns path to the written file, or None if no LOD refs found.
    """
    if _parsed is not None:
        worldspaces, cells, stats, refs = _parsed
    else:
        print(f"  Parsing ESM: {esm_path.name}")
        worldspaces, cells, stats, refs = _parse_esm(esm_path)

    # Find worldspace form_id
    wrld_fid = None
    wrld_info = None
    for fid, w in worldspaces.items():
        if w['edid'].lower() == worldspace_edid.lower():
            wrld_fid = fid
            wrld_info = w
            break
    if wrld_fid is None:
        # Fall back to first worldspace
        if worldspaces:
            wrld_fid, wrld_info = next(iter(worldspaces.items()))
            print(f"  Warning: worldspace '{worldspace_edid}' not found, "
                  f"using '{wrld_info['edid']}'")
        else:
            print("  Error: no worldspaces found in ESM")
            return None

    edid = wrld_info['edid']
    # Use the effective SW coords from LODSettings if provided; otherwise use raw MNAM values.
    # CellSW= in the LODGen input MUST match the SW in the .lod file.
    if cell_sw is not None:
        sw_x, sw_y = cell_sw
    else:
        sw_x = wrld_info['sw_x']
        sw_y = wrld_info['sw_y']
    # LODGen resolves every listed mesh under the single PathData root, so a
    # mesh may only be listed if it exists in THIS output dir — a path that
    # resolves in some other plugin's tree makes LODGen abort with "file not
    # found" (exit 404) and no tiles at all get baked.
    output_meshes_dir = output_dir / 'meshes'
    master_meshes = [Path(d) / 'meshes' for d in (master_dirs or [])]

    # Index cells by form_id → parent_wrld for fast lookup
    cell_wrld = {fid: c['parent_wrld'] for fid, c in cells.items()}

    # Collect exterior REFR records in this worldspace whose base is a STAT/ACTI/etc.
    lines = []
    skipped_unsafe = set()

    for ref in refs:
        # Must be in our worldspace
        if ref['parent_wrld'] != wrld_fid:
            pc = ref['parent_cell']
            if cell_wrld.get(pc, 0) != wrld_fid:
                continue

        base_fid = ref['base_fid']
        if base_fid not in stats:
            continue

        stat = stats[base_fid]
        model = stat.get('model', '')
        if not model:
            continue

        stat_flags_val = stat.get('flags', 0)
        stat_is_lod = bool(stat_flags_val & (_FLAG_DISTANT_LOD | _FLAG_WORLD_MAP))
        if not stat_is_lod:
            continue
        # Already covered by a master's own LOD run — don't re-bake it.
        if any(_mesh_exists(_far_nif_path(model), m) for m in master_meshes):
            continue

        lod4, lod8, lod16 = _lod_meshes_for(stat, output_meshes_dir)
        if not (lod4 or lod8 or lod16):
            continue
        # One mesh LODGen cannot parse aborts the whole worldspace, so screen
        # each listed mesh (and the full model it falls back to) up front.
        unsafe = [m for m in (model, lod4, lod8, lod16)
                  if m and not _lod_mesh_is_safe(m, output_meshes_dir)]
        if unsafe:
            for m in unsafe:
                skipped_unsafe.add(_normalize(m))
            continue
        mat = ''
        stat_edid   = stat.get('edid', f'{base_fid:08X}')
        stat_flags  = f"{stat_flags_val:08X}"
        base_entry  = f"{stat_edid}\t{stat_flags}\t{mat}\t{_normalize(model)}\t{_normalize(lod4)}\t{_normalize(lod8)}\t{_normalize(lod16)}"

        # Reference line
        ref_fid   = f"{ref['form_id']:08X}"
        ref_flags = f"{ref['flags']:08X}"
        scale     = ref['scale']
        # Rotations in ESM are radians; LODGen expects degrees
        rx = math.degrees(ref['rx'])
        ry = math.degrees(ref['ry'])
        rz = math.degrees(ref['rz'])

        line = (f"{ref_fid}\t{ref_flags}\t"
                f"{ref['x']:.4f}\t{ref['y']:.4f}\t{ref['z']:.4f}\t"
                f"{rx:.4f}\t{ry:.4f}\t{rz:.4f}\t"
                f"{scale:.4f}\t{base_entry}")
        lines.append(line)

    if skipped_unsafe:
        print(f"  WARNING: {len(skipped_unsafe)} LOD mesh(es) excluded — "
              f"unreadable or non-NiNode root (would crash LODGen and lose "
              f"ALL of this worldspace's object LOD):")
        for m in sorted(skipped_unsafe)[:10]:
            print(f"    {m}")
        if len(skipped_unsafe) > 10:
            print(f"    ... and {len(skipped_unsafe) - 10} more")

    if not lines:
        print(f"  No LOD references found for worldspace '{edid}'")
        return None

    # Build header.
    # PathData points to our output directory so LODGen finds the extracted
    # _far.nif meshes there rather than looking in the Skyrim SE Data folder.
    # Must have a trailing backslash or LODGen will concatenate without a separator.
    # Resolve to absolute — LODGen runs with cwd=tools/ so a relative PathData
    # ("output\...") would fail its Data-directory existence check, and a
    # relative PathOutput would silently write the .bto under tools\.
    dest      = (Path(output_dir).resolve() / 'meshes' / 'terrain' / edid
                 / 'Objects')
    path_data = str(Path(output_dir).resolve()).rstrip('\\/') + '\\'
    header = [
        f"GameMode=TES5",
        f"Worldspace={edid}",
        f"CellSW={sw_x} {sw_y}",
        f"PathData={path_data}",
        f"PathOutput={dest}",
    ]

    out_txt = LODGEN_EXE.parent / f"LODGen {edid}.txt"
    with open(out_txt, 'w', encoding='utf-8') as f:
        f.write('\n'.join(header) + '\n')
        f.write('\n'.join(lines) + '\n')

    print(f"  LODGen input: {out_txt} ({len(lines)} references)")
    return out_txt


def run_lodgen(lodgen_input: Path, output_dir: Path) -> bool:
    """Invoke LODGenx64.exe on the prepared input file."""
    if not LODGEN_EXE.exists():
        print(f"  ERROR: LODGenx64.exe not found at {LODGEN_EXE}")
        return False

    # Ensure output terrain/Objects dir exists (LODGen may not create it)
    # PathOutput is embedded in the input file; LODGen reads it from there.

    cmd = [
        str(LODGEN_EXE),
        str(lodgen_input),
        "--dontFixTangents",
        "--removeUnseenFaces",
        # --skyblivionTexPath is NOT used: it prepends an extra 'tes4\\' to texture paths
        # already under textures\\tes4\\, doubling the prefix and causing null-ptr crashes.
    ]
    print(f"  Running: {' '.join(cmd)}")
    # Capture output so it reaches the GUI log instead of a popped-up console
    # window (which never exists under the console-less GUI launcher).
    result = subprocess.run(cmd, cwd=str(LODGEN_EXE.parent),
                            capture_output=True, text=True, **POPEN_FLAGS)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="")
    if result.returncode != 0:
        print(f"  WARNING: LODGenx64.exe exited with code {result.returncode}")
        return False
    return True


# ---------------------------------------------------------------------------
# 5. Top-level orchestration
# ---------------------------------------------------------------------------

def generate_lod(esm_path: Path, output_dir: Path,
                 worldspace_edid: str = 'Tamriel',
                 master_dirs=None, master_texture_dirs=None,
                 overlay_paths=None, only_cells=None) -> bool:
    """
    Full LOD generation pipeline:
      1. Write LODSettings/<worldspace>.lod
      2. Parse ESM → LODGen input text
      3. Run LODGenx64.exe

    Args:
        esm_path:          Path to the converted .esm/.esp holding the WRLD/
                           CELL/REFR records. For an OVERRIDE plugin this is
                           the MASTER's output, not the plugin's own — the
                           plugin's records arrive via `overlay_paths`.
        overlay_paths:     Plugins applied ON TOP of esm_path, in load order.
                           References merge by FormID so a moved, rescaled or
                           deleted REFR REPLACES the master's entry instead of
                           being drawn twice.
        only_cells:        Restrict output to tiles covering these (x, y)
                           cells; None means the whole worldspace.
        output_dir:        Output dir owning the assets and receiving the
                           generated LOD (contains meshes/, textures/, …).
        worldspace_edid:   Editor ID of the worldspace to generate LOD for
        master_dirs:       Converted output dirs of this plugin's masters.
                           Anything they already ship LOD for is skipped, so
                           an override plugin bakes only what IT introduces.
                           Only set when a MASTER owns the worldspace.
        master_texture_dirs: Converted output dirs of this plugin's masters,
                           always. A plugin regularly places a master's models
                           in its OWN worldspace, and their textures exist only
                           in the master's output; the .bto tiles baked here
                           still reference them, so they are copied in.

    Returns True on success.
    """
    print(f"\n[LOD] Generating object LOD for worldspace '{worldspace_edid}'")

    # Parse ESM once; reuse data for both LODSettings and LODGen input
    print(f"  Parsing ESM: {esm_path.name}")
    worldspaces, cells, stats, refs = _parse_esm(esm_path)

    # Apply override plugins on top, in load order. References merge BY FORMID
    # so a plugin that moved, rescaled or re-based one of the master's objects
    # replaces it rather than adding a second copy at the old spot, and a
    # DELETED override (header flag 0x20) removes it from LOD entirely — the
    # object is gone in-game, so a distant copy of it would be a floating
    # ghost. STAT/CELL/worldspace tables merge by key the same way.
    for ov_path in (overlay_paths or []):
        ov_path = Path(ov_path)
        print(f"  Applying override plugin: {ov_path.name}")
        o_wrld, o_cells, o_stats, o_refs = _parse_esm(ov_path)
        worldspaces.update(o_wrld)
        cells.update(o_cells)
        stats.update(o_stats)
        by_fid = {r['form_id']: i for i, r in enumerate(refs)}
        added = replaced = removed = 0
        for r in o_refs:
            idx = by_fid.get(r['form_id'])
            if r['flags'] & 0x20:          # deleted by the author
                if idx is not None:
                    refs[idx] = None
                    removed += 1
                continue
            if idx is None:
                by_fid[r['form_id']] = len(refs)
                refs.append(r)
                added += 1
            else:
                refs[idx] = r
                replaced += 1
        refs = [r for r in refs if r is not None]
        print(f"    references: {added} added, {replaced} replaced, "
              f"{removed} deleted")

    wrld_fid  = None
    wrld_info = None
    for fid, w in worldspaces.items():
        if w['edid'].lower() == worldspace_edid.lower():
            wrld_fid  = fid
            wrld_info = w
            break
    if wrld_info is None and worldspaces:
        wrld_fid, wrld_info = next(iter(worldspaces.items()))
    if wrld_info is None:
        print("  ERROR: no worldspaces found, skipping LOD generation")
        return False

    edid = wrld_info['edid']
    _, eff_sw_x, eff_sw_y = write_lod_settings(
        edid,
        wrld_info['sw_x'], wrld_info['sw_y'],
        wrld_info['ne_x'], wrld_info['ne_y'],
        output_dir,
    )

    # Ensure Objects output dir exists
    objects_dir = output_dir / 'meshes' / 'terrain' / edid / 'Objects'
    objects_dir.mkdir(parents=True, exist_ok=True)

    # Generate _far.nif LOD meshes for any LOD-flagged objects that don't have one.
    # Only process models that are actually placed in this worldspace.
    # Must happen before writing the LODGen input so the new files are found.
    cell_wrld_map = {fid: c['parent_wrld'] for fid, c in cells.items()}
    referenced_models = set()
    for ref in refs:
        pw = ref['parent_wrld']
        if pw != wrld_fid and cell_wrld_map.get(ref['parent_cell'], 0) != wrld_fid:
            continue
        base_fid = ref['base_fid']
        if base_fid in stats:
            m = stats[base_fid].get('model', '')
            if m:
                referenced_models.add(m)

    # Drop models a master already ships LOD for: this plugin overrides the
    # master's records, so re-deriving their billboards would duplicate the
    # master's whole LOD set for the sake of the few models it adds.
    master_meshes = [Path(d) / 'meshes' for d in (master_dirs or [])]
    if master_meshes:
        before = len(referenced_models)
        referenced_models = {
            m for m in referenced_models
            if not any(_mesh_exists(_far_nif_path(m), mm) for mm in master_meshes)
        }
        skipped = before - len(referenced_models)
        if skipped:
            print(f"  Skipping {skipped} model(s) already covered by a "
                  f"master's LOD; generating only this plugin's "
                  f"{len(referenced_models)}")

    from .lod_far_gen import generate_missing_far_nifs
    generate_missing_far_nifs(stats, output_dir / 'meshes',
                               referenced_models=referenced_models,
                               force_regen_generated=True,
                               tex_root=output_dir / 'textures')

    # Write LOD input (all LOD-flagged objects) and run LODGenx64 once.
    # LODGen resolves every mesh under the single PathData root (output_dir),
    # so only meshes that exist THERE may be listed.
    lodgen_txt = write_lodgen_input(esm_path, output_dir, edid,
                                    _parsed=(worldspaces, cells, stats, refs),
                                    cell_sw=(eff_sw_x, eff_sw_y),
                                    master_dirs=master_dirs)
    ok = False
    if lodgen_txt:
        # Remove stale tiles first: LODGen only rewrites tiles that still have
        # refs, so old (oversized) .bto would otherwise linger.
        stale = list(objects_dir.glob('*.bto'))
        for f in stale:
            f.unlink()
        if stale:
            print(f"  Removed {len(stale)} stale .bto tiles")
        ok = run_lodgen(lodgen_txt, output_dir)

    # An override plugin ships only the tiles its edits touch. LODGen has no
    # per-tile switch and bakes the whole worldspace in one pass, so the
    # unaffected tiles are pruned here instead. They are byte-for-byte what the
    # master already ships, so keeping them would only duplicate the master's
    # LOD and enlarge the plugin for no visual difference.
    if only_cells:
        kept = _prune_unaffected_tiles(objects_dir, '.bto', only_cells)
        print(f"  Kept {kept} .bto tile(s) covering the changed cells; "
              f"the rest are the master's and were pruned")

    # Fill in any LOD texture the .bto files reference but that does not exist:
    # atlas normal maps (synthesized) and any diffuse that lives only in a
    # master's output because this plugin baked the master's models into its LOD.
    _fill_missing_lod_textures(
        objects_dir, _textures_root(output_dir),
        master_tex_roots=[_textures_root(Path(d))
                          for d in (master_texture_dirs or master_dirs or [])])

    if ok:
        print(f"[LOD] Object LOD generation complete.")
    else:
        print(f"[LOD] LOD generation finished with warnings.")
    return ok


def _prune_unaffected_tiles(tile_dir: Path, suffix: str, only_cells) -> int:
    """Delete LOD tiles that cover none of `only_cells`. Returns the kept count.

    Tiles are named `<worldspace>.<level>.<x>.<y><suffix>`, where (x, y) is the
    tile's SW cell corner and it spans `level` cells in each direction. A tile
    is kept when ANY cell it composites was changed — an edit near a tile
    boundary changes the neighbouring tile's edge too, so overlap (not just the
    edited cell's own tile) is the right test.
    """
    only = set(only_cells)
    kept = 0
    for tile in list(tile_dir.glob(f'*{suffix}')):
        parts = tile.name[:-len(suffix)].split('.')
        try:
            level, tx, ty = int(parts[-3]), int(parts[-2]), int(parts[-1])
        except (ValueError, IndexError):
            kept += 1          # unrecognised name: never delete blind
            continue
        if any((tx + dx, ty + dy) in only
               for dy in range(level) for dx in range(level)):
            kept += 1
        else:
            tile.unlink()
    return kept


def _textures_root(plugin_out_dir: Path) -> Path:
    """The plugin's textures directory, whatever case it was created with.

    Different stages have created 'textures' and 'Textures' (Morrowind_ob has
    the capitalised one, Oblivion.esm the lowercase), and this lookup also runs
    on case-sensitive filesystems, so probe rather than assume.
    """
    for name in ('textures', 'Textures'):
        p = plugin_out_dir / name
        if p.is_dir():
            return p
    return plugin_out_dir / 'textures'


_BTO_TEX_RE = _re.compile(rb'[A-Za-z0-9_\\/ .-]{3,200}?\.dds', _re.IGNORECASE)


def _bto_texture_refs(bto_dir: Path) -> set:
    """Texture paths referenced by the .bto tiles, relative to the textures root.

    LODGen writes full paths ('data\\textures\\tes4\\...\\foo.dds'), so a ref
    resolves directly against textures/ — nothing needs copying or renaming.
    """
    refs = set()
    for bto in bto_dir.glob('*.bto'):
        for m in _BTO_TEX_RE.finditer(bto.read_bytes()):
            s = m.group(0).decode('latin-1').lower().replace('/', '\\')
            for prefix in ('data\\textures\\', 'textures\\'):
                if s.startswith(prefix):
                    s = s[len(prefix):]
                    break
            refs.add(s)
    return refs


def _fill_missing_lod_textures(bto_dir: Path, tex_root: Path,
                               master_tex_roots=None):
    """Create the LOD textures the .bto tiles reference but that don't exist.

    Mostly these are NORMAL maps: LODGen writes each atlas diffuse
    (<name>_a.dds) but no matching atlas normal (<name>_a_n.dds), and object LOD
    renders unlit against a missing _n.  Each one is written at the exact path
    the .bto asks for, built from the atlas's source normal when there is one
    (single-texture atlas) and otherwise a flat normal sized to the diffuse.

    A plugin can also bake a MASTER's models into its own LOD (Morrowind_ob
    places Oblivion architecture in its worldspace), and those diffuse textures
    live only in the master's output — 117 of them, which would render as
    untextured LOD.  They are copied in from the master, since this plugin's
    .bto tiles are what reference them.
    """
    missing = sorted(r for r in _bto_texture_refs(bto_dir)
                     if not (tex_root / r).exists())
    if not missing:
        return

    synth = 0
    from_master = 0
    unresolved = []
    for rel in missing:
        dest = tex_root / rel
        if not rel.endswith('_n.dds'):
            # Diffuse (or any non-normal) the master already converted.
            src = next((mr / rel for mr in (master_tex_roots or [])
                        if (mr / rel).exists()), None)
            if src is not None:
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    from_master += 1
                    continue
                except Exception:
                    pass
            unresolved.append(rel)
            continue
        stem = rel[:-len('_n.dds')]              # 'tes4\...\lcstone01_a'
        # An atlas ('..._a') borrows the normal of the texture it was built from.
        base = stem[:-2] if stem.endswith('_a') else stem

        # Look in this plugin's textures first, then any master's — a master's
        # model baked into our LOD keeps its textures in the master's output,
        # and using its real normal beats falling back to a flat one.
        def _find(name):
            p = tex_root / name
            if p.exists():
                return p
            for mr in (master_tex_roots or []):
                q = mr / name
                if q.exists():
                    return q
            return p          # non-existent local path (callers test .exists())

        src_normal = _find(f'{base}_n.dds')
        diffuse = _find(f'{stem}.dds')
        if not diffuse.exists():
            diffuse = _find(f'{base}.dds')
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src_normal.exists() and src_normal != dest:
                shutil.copy2(src_normal, dest)
            else:
                _write_flat_normal_for(diffuse, dest)
            synth += 1
        except Exception:
            unresolved.append(rel)

    if from_master:
        print(f"  Copied {from_master} object-LOD texture(s) from a master's "
              f"output (this plugin bakes the master's models into its LOD).")
    if synth:
        print(f"  Synthesized {synth} object-LOD normal maps.")
    if unresolved:
        print(f"  WARNING: {len(unresolved)} LOD textures missing: "
              + ", ".join(unresolved[:5])
              + ("..." if len(unresolved) > 5 else ""))


def _write_flat_normal_for(atlas_diffuse: Path, dest: Path):
    """Write a flat (128,128,255) normal DDS sized to the atlas diffuse."""
    size = 512
    try:
        from PIL import Image
        if atlas_diffuse and atlas_diffuse.exists():
            size = Image.open(atlas_diffuse).size[0]
    except Exception:
        pass
    _ensure_flat_normal_dds(dest, size)


def _ensure_flat_normal_dds(path: Path, size: int):
    """Write an uncompressed flat-normal RGBA DDS (128,128,255,255) of side=size."""
    from PIL import Image
    import numpy as _np
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = _np.zeros((size, size, 4), dtype=_np.uint8)
    arr[:, :, 0] = 128
    arr[:, :, 1] = 128
    arr[:, :, 2] = 255
    arr[:, :, 3] = 255
    # DDS uncompressed A8R8G8B8 header
    hdr = b'DDS ' + struct.pack('<I', 124)
    hdr += struct.pack('<I', 0x1 | 0x2 | 0x4 | 0x1000 | 0x8)   # caps/h/w/pf/pitch
    hdr += struct.pack('<I', size) + struct.pack('<I', size)
    hdr += struct.pack('<I', size * 4)                          # pitch
    hdr += struct.pack('<I', 0) + struct.pack('<I', 0)
    hdr += b'\x00' * 44
    hdr += struct.pack('<II', 32, 0x41)                         # RGB|ALPHAPIXELS
    hdr += struct.pack('<I', 0)                                 # not fourcc
    hdr += struct.pack('<I', 32)                                # bit count
    hdr += struct.pack('<IIII', 0x00ff0000, 0x0000ff00, 0x000000ff, 0xff000000)
    hdr += struct.pack('<I', 0x1000)
    hdr += struct.pack('<IIII', 0, 0, 0, 0)
    # BGRA byte order for A8R8G8B8
    bgra = arr[:, :, [2, 1, 0, 3]].tobytes()
    path.write_bytes(hdr + bgra)


# ---------------------------------------------------------------------------
# CLI entry point (for standalone testing)
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description="Generate object LOD for a converted TES5 plugin")
    parser.add_argument('esm', help='Path to converted ESM/ESP')
    parser.add_argument('output_dir', help='Plugin output directory (containing meshes/, textures/)')
    parser.add_argument('--worldspace', default='Tamriel', help='Worldspace EditorID')
    args = parser.parse_args()

    ok = generate_lod(
        Path(args.esm),
        Path(args.output_dir),
        args.worldspace,
    )
    sys.exit(0 if ok else 1)
