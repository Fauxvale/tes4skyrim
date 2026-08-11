"""Merged LOD for the tiles two or more SIBLING plugins both change.

A sibling group is every converted plugin that shares a master and edits the
same worldspace: Tamriel.esp, ElsweyrAnequina.esp, Morrowind_ob.esm and
DLCBattlehornCastle.esp all extend Oblivion.esm's TES4Tamriel.

Each of those bakes its LOD independently, as "master + itself" — which is
correct in isolation and wrong the moment two of them are installed together.
LOD tiles are FILES on a fixed grid (meshes/terrain/<wrld>/Objects/<wrld>.4.-32.-32.bto),
so two plugins that touch the same tile ship the same path and the loser is
whichever the mod manager overwrites. That plugin's terrain edits and distant
objects vanish from the tile, leaving the other plugin's version of the world.

The overlap is far wider than the authored edits suggest, because a tile covers
a BLOCK of cells and any single changed cell dirties the whole tile. Measured on
the current output: Tamriel.esp and Morrowind_ob.esm collide on 530 cells but
6,144 level-16 tiles.

The fix is the one the generators already support: bake the contested tiles ONCE
from the master with EVERY sibling stacked as an overlay, in load order. The
existing `overlay_paths` merge is by FormID, so a REFR one sibling moved and
another left alone resolves exactly as the engine would resolve it. The result
goes to its own mod folder so it can be installed last and win the overwrite
deliberately, instead of the outcome depending on install order.

Only CONTESTED tiles are baked. A tile just one sibling touches is already
correct in that sibling's own output and re-shipping it here would only add
another copy to keep in sync.
"""

from __future__ import annotations

import os
import struct
from collections import defaultdict
from pathlib import Path

from .terrain_lod import (shipped_lod_worldspaces, _master_names,
                          _find_worldspace_fid, _scan_cell_coords)
from .lod_gen import _kept_tile_cells_by_level


# The mod folder the merged tiles ship in. Named so it sorts last in a mod
# manager's alphabetical install order, which is the order that decides the
# overwrite — the whole point of this step is to win that overwrite on purpose.
MERGED_DIR_NAME = "ZZZ Merged Sibling LOD"


def changed_cells_in_worldspace(plugin_esm: Path, master_esm: Path,
                                edid: str, coords: dict) -> set:
    """Grid cells `plugin_esm` changes the LOD of, inside ONE worldspace.

    Like `terrain_lod.changed_lod_cells`, but the worldspace is resolved from
    the MASTER and matched by FormID, so a plugin that overrides cells without
    shipping the WRLD record itself is still scoped correctly.

    That scoping is the whole point. `changed_lod_cells` treats "worldspace not
    found in this file" as "no filter" and counts every cell it sees, so a
    plugin was reported as changing worldspaces it has nothing to do with:
    Morrowind_ob.esm claimed an identical 5,919 cells in TES4Tamriel, SEWorld
    and PalePassWorld alike — two of which no converted plugin even defines.
    Merging on that would bake tiles for worldspaces nobody touched.

    `coords` maps CELL FormID -> (x, y) and must already carry the master's
    cells; the plugin's own are folded in here. Passing it in keeps the master
    scanned once per group rather than once per sibling.
    """
    m_raw = master_esm.read_bytes()
    target = _find_worldspace_fid(m_raw, len(m_raw), edid)
    if target is None:
        return set()
    del m_raw

    _scan_cell_coords(plugin_esm, coords)
    raw = plugin_esm.read_bytes()
    n = len(raw)
    changed: set = set()

    def scan(start, end, cur_cell, cur_wrld):
        p = start
        while p < end and p + 24 <= n:
            sig = raw[p:p + 4]
            size = struct.unpack_from('<I', raw, p + 4)[0]
            if sig == b'GRUP':
                g_size = size
                g_type = struct.unpack_from('<I', raw, p + 12)[0]
                label = raw[p + 8:p + 12]
                nxt_cell, nxt_wrld = cur_cell, cur_wrld
                if g_type == 1:
                    nxt_wrld = struct.unpack_from('<I', label)[0]
                elif g_type == 6:
                    nxt_cell = struct.unpack_from('<I', label)[0]
                scan(p + 24, p + g_size, nxt_cell, nxt_wrld)
                p += g_size
                continue
            fid = struct.unpack_from('<I', raw, p + 12)[0]
            if sig == b'CELL':
                cur_cell = fid
            elif sig in (b'LAND', b'REFR'):
                # Unlike changed_lod_cells, an unknown worldspace is NOT a
                # wildcard: a record only counts when it is genuinely inside
                # the target worldspace's GRUP.
                if cur_wrld == target:
                    c = coords.get(cur_cell)
                    if c is not None:
                        changed.add(c)
            p += 24 + size

    scan(24 + struct.unpack_from('<I', raw, 4)[0], n, 0, 0)
    return changed


def _master_chain(name: str, export_root: Path, known: list[str]) -> set[str]:
    """Every plugin `name` depends on, directly or transitively.

    Transitive because the dependency that matters can be indirect:
    Translation.esp lists only Nehrim.esm, and whether it can touch a
    worldspace Nehrim owns is decided by walking through Nehrim.
    """
    seen: set[str] = set()
    stack = [name]
    while stack:
        cur = stack.pop()
        for m in _master_names(export_root / cur):
            if m in seen or m not in known:
                continue
            seen.add(m)
            stack.append(m)
    return seen


def converted_plugins(out_root: Path) -> list[str]:
    """Every plugin with a converted ESM in `out_root`, in name order.

    A folder qualifies only when it holds the plugin file it is named after —
    output/ also collects the shared `Slot44 Patch.esp`, this step's own merged
    folder, and whatever else the pipeline drops at the root.
    """
    if not out_root.is_dir():
        return []
    names = []
    for p in sorted(out_root.iterdir()):
        if not p.is_dir() or p.name == MERGED_DIR_NAME:
            continue
        if (p / p.name).is_file():
            names.append(p.name)
    return names


def plugins_txt_order() -> list[str]:
    """Plugin names in the user's real Skyrim load order, or [] if unavailable.

    `%LOCALAPPDATA%/Skyrim Special Edition/plugins.txt` is what the game itself
    reads, so it is the only authoritative answer to "which of these two wins".
    Names are returned verbatim and in file order; the leading `*` (active
    flag) is stripped, and inactive entries are kept because they still record
    a position the user chose.
    """
    plugins_txt = (Path(os.environ.get("LOCALAPPDATA", ""))
                   / "Skyrim Special Edition" / "plugins.txt")
    if not plugins_txt.exists():
        return []
    order: list[str] = []
    try:
        with open(plugins_txt, "r", encoding="utf-8-sig", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                name = line.lstrip("*").strip()
                if name and name not in order:
                    order.append(name)
    except OSError:
        return []
    return order


def _load_order(names: list[str], export_root: Path,
                explicit: list[str] | None = None) -> list[str]:
    """The order sibling overlays are applied in — i.e. who wins a conflict.

    The LAST overlay applied replaces earlier ones for any shared FormID, so
    this order IS the conflict resolution, not a cosmetic detail.

    Three sources, in descending authority:

    1. `explicit` — an order the user arranged by hand in the GUI. Absolute:
       whatever they dragged is what runs.
    2. `plugins.txt` — the real Skyrim load order, which is what the game
       itself obeys. Anything it does not mention is appended alphabetically.
    3. Structural fallback, used only when plugins.txt is missing or lists
       none of these plugins: master-depth, then .esm before .esp, then name.

    The structural fallback alone used to be the whole implementation, and its
    alphabetical tiebreak is an ARBITRARY winner for two siblings that edit the
    same reference — "ElsweyrAnequina before Tamriel" was alphabetical accident
    rather than anything the user chose.
    """
    if explicit:
        # Honour the user's arrangement; anything they never saw (a plugin
        # converted since) still has to run, so it lands after in stable order.
        chosen = [n for n in explicit if n in names]
        return chosen + sorted(n for n in names if n not in chosen)

    lo = [n.lower() for n in plugins_txt_order()]
    if lo:
        rank = {name: i for i, name in enumerate(lo)}
        listed = [n for n in names if n.lower() in rank]
        if listed:
            unlisted = sorted(n for n in names if n.lower() not in rank)
            return sorted(listed, key=lambda n: rank[n.lower()]) + unlisted

    depth: dict[str, int] = {}

    def d(name: str, seen: frozenset = frozenset()) -> int:
        if name in depth:
            return depth[name]
        if name in seen:
            return 0
        masters = _master_names(export_root / name)
        val = 1 + max([d(m, seen | {name}) for m in masters if m in names],
                      default=-1)
        depth[name] = val
        return val

    for n in names:
        d(n)
    # .esm before .esp at equal depth, matching the engine's own split.
    return sorted(names, key=lambda n: (depth[n],
                                        not n.lower().endswith('.esm'), n))


def find_sibling_groups(out_root: Path, export_root: Path,
                        explicit_order: list[str] | None = None) -> dict:
    """Worldspaces that more than one converted plugin changes LOD-visibly.

    Returns {worldspace_edid: {'master': name, 'master_esm': Path,
                               'plugins': [name, ...] in load order,
                               'cells': {name: {(x, y), ...}}}}.

    A worldspace is only a conflict when TWO OR MORE siblings change it. One
    plugin editing a master's worldspace alone is the ordinary override case
    that `phase_lod` already handles correctly, and nothing here should touch
    it.

    `explicit_order` is the user's hand-arranged order from the GUI; see
    `_load_order` for how it and plugins.txt rank against each other.
    """
    names = converted_plugins(out_root)
    if len(names) < 2:
        return {}
    order = _load_order(names, export_root, explicit_order)

    # Which plugin OWNS each worldspace: the one whose export shipped LOD for
    # it. That is the same authority phase_lod routes on, so both stages agree
    # on who the master is.
    owner: dict[str, str] = {}
    for name in order:
        for edid, _fid in (shipped_lod_worldspaces(export_root / name) or []):
            owner.setdefault(edid, name)

    groups: dict[str, dict] = {}
    for edid, master in owner.items():
        master_esm = out_root / master / master
        if not master_esm.is_file():
            continue
        # The master's CELL FormID -> (x, y) map, scanned ONCE and reused for
        # every sibling: an override plugin's CELL records usually carry no
        # XCLC, so the master is the only place the grid coordinates exist.
        coords: dict = {}
        _scan_cell_coords(master_esm, coords)

        cells: dict[str, set] = {}
        for name in order:
            if name == master:
                continue
            esm = out_root / name / name
            if not esm.is_file():
                continue

            # A plugin can only touch this worldspace if it DEPENDS on the
            # plugin that owns it. This is the gate, not "does it carry a WRLD
            # record": both directions of that test are wrong here.
            #
            # Morrowind_ob.esm changes 5,919 TES4Tamriel cells while shipping no
            # TES4Tamriel WRLD record at all — its CELL/LAND/REFR overrides sit
            # under the master's worldspace — so requiring the WRLD record drops
            # a genuine conflict.
            #
            # In the other direction, a standalone plugin would look like it
            # edits any worldspace it is compared against — Nehrim.esm (no
            # masters, own worldspace) reported 6,675 changed cells in
            # Oblivion's TES4Tamriel — which would merge two completely
            # unrelated mods' LOD into one folder.
            if master not in _master_chain(name, export_root, names):
                continue
            try:
                changed = changed_cells_in_worldspace(esm, master_esm, edid,
                                                      coords)
            except Exception as exc:
                print(f"  WARNING: could not scan {name} for '{edid}': {exc}")
                continue
            if changed:
                cells[name] = changed
        if len(cells) < 2:
            continue
        groups[edid] = {
            'master': master,
            'master_esm': master_esm,
            'plugins': [n for n in order if n in cells],
            'cells': cells,
        }
    return groups


def contested_cells(cells_by_plugin: dict) -> set:
    """Cells whose TILES more than one sibling rebuilds.

    Tile granularity is what matters, not cell granularity: two plugins editing
    different cells inside one level-16 tile still ship rival copies of that
    tile. So the contest is computed per LOD level on the tile grid, then mapped
    back to the cells that drive those tiles — which is the unit `only_cells`
    takes.

    Returning cells (not tiles) keeps this compatible with both generators:
    each re-derives its own tile set from the cells, at its own levels.
    """
    # The TILES each sibling ships at each level. `_kept_tile_cells_by_level`
    # returns the CELLS those tiles composite, not the tiles themselves, so
    # fold each cell back onto its floor-aligned tile anchor — the same
    # alignment that function uses, and the anchor that names the .bto/.btr on
    # disk. Comparing its cell sets directly would compare footprints rather
    # than filenames, and two plugins whose footprints merely touch would look
    # like they contest a tile neither of them writes.
    per_level: dict[int, dict[str, set]] = defaultdict(dict)
    for name, cells in cells_by_plugin.items():
        for lvl, covered in _kept_tile_cells_by_level(cells).items():
            per_level[lvl][name] = {((cx // lvl) * lvl, (cy // lvl) * lvl)
                                    for cx, cy in covered}

    hot_tiles: dict[int, set] = {}
    for lvl, by_plugin in per_level.items():
        seen: set = set()
        dup: set = set()
        for tiles in by_plugin.values():
            dup |= (seen & tiles)
            seen |= tiles
        if dup:
            hot_tiles[lvl] = dup
    if not hot_tiles:
        return set()

    # A tile at level L covers an LxL block of cells anchored on a multiple of
    # L. Every sibling cell inside a contested tile has to be rebuilt, because
    # the merged tile has to contain all of their edits, not just the ones that
    # made it contested.
    #
    # Bucket the cells by tile anchor rather than testing each cell against each
    # tile: the pairwise scan is 6,144 contested tiles x 100k Tamriel.esp cells
    # per level, which is minutes of pure Python for a set intersection.
    union = set().union(*cells_by_plugin.values())
    hot_cells: set = set()
    for lvl, tiles in hot_tiles.items():
        buckets: dict[tuple, list] = defaultdict(list)
        for c in union:
            anchor = ((c[0] // lvl) * lvl, (c[1] // lvl) * lvl)
            buckets[anchor].append(c)
        for t in tiles:
            hot_cells.update(buckets.get(t, ()))
    return hot_cells


# LOD assets live in exactly these three trees. Tile filenames are identical
# across plugins (TES4Tamriel.16.-16.0.bto and so on), which is precisely why
# they collide in the Data folder — and what makes a plain name comparison the
# honest way to report the collision.
_LOD_SUBDIRS = (
    ('meshes', 'terrain'),      # .btr terrain + Objects/*.bto
    ('textures', 'terrain'),    # composited diffuse/normal .dds
)


def _lod_files(plugin_dir: Path, worldspace: str) -> set[str]:
    """Every LOD file a plugin ships for one worldspace, as Data-relative paths.

    Data-relative because that is the namespace the collision actually happens
    in: two plugins installed together resolve to one Data folder, so equal
    relative paths are rival copies of the same file regardless of which
    output/<plugin>/ tree they came from.
    """
    found: set[str] = set()
    for parts in _LOD_SUBDIRS:
        root = plugin_dir.joinpath(*parts) / worldspace
        if not root.is_dir():
            continue
        for p in root.rglob('*'):
            if p.is_file():
                rel = Path(*parts) / worldspace / p.relative_to(root)
                found.add(rel.as_posix().lower())
    return found


def overwrite_report(out_root: Path, worldspace: str, plugins: list[str],
                     merged_dir_name: str = None) -> dict:
    """Which of each plugin's LOD files the merged folder supersedes.

    Returns {plugin: {'overwritten': [...], 'kept': N}} — the files this merge
    takes over, and how many of that plugin's own tiles it leaves alone.

    Reported per plugin rather than as one total because that is the question a
    user actually has when installing: "what does this folder take over from
    MY mod?" A merged folder that silently replaced a plugin's whole LOD tree
    would be a very different thing from one that replaces 40 tiles of it, and
    the difference should not have to be inferred from the install order.
    """
    merged = _lod_files(out_root / (merged_dir_name or MERGED_DIR_NAME),
                        worldspace)
    report: dict[str, dict] = {}
    for name in plugins:
        own = _lod_files(out_root / name, worldspace)
        hit = sorted(own & merged)
        report[name] = {'overwritten': hit, 'kept': len(own) - len(hit)}
    return report
