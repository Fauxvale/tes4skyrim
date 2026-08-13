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
import re
import struct
from collections import defaultdict
from pathlib import Path

from .terrain_lod import (shipped_lod_worldspaces, _master_names,
                          _find_worldspace_fid, _scan_cell_coords)
from .lod_gen import _kept_tile_cells_by_level


# The standalone mod ALL generated LOD ships in.
#
# One folder, not one per plugin, because a LOD tile is a file on a fixed grid
# keyed only by worldspace and coordinate: every plugin editing a worldspace
# produces the SAME tile paths, so per-plugin output meant rival copies of one
# file and the mod manager's install order silently picked a winner. Generating
# once for the whole load order leaves exactly one copy of each tile, so there
# is no overwrite to win and no merge pass to reconcile it afterwards.
#
# What lives here is what belongs to the whole load order: LODSettings and the
# baked .btr/.bto/.dds tiles. Derived _far.nif meshes do NOT — they are ordinary
# converted meshes and stay in the plugin that ships the full model they came
# from (see lod_gen.generate_lod's `far_nif_dirs`).
LOD_DIR_NAME = "AutoConvertLOD"

# The previous merged-tile folder. Kept only so an existing install can be
# recognised and cleaned up; nothing writes here any more.
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
    # Both sides normalised: `target` comes from the MASTER's id space and is
    # compared against the PLUGIN's GRUP labels, and `coords` accumulates cells
    # from both files. A raw id means nothing outside its own file.
    from .lod_gen import _formid_remap_table
    m_raw = master_esm.read_bytes()
    _t = _find_worldspace_fid(m_raw, len(m_raw), edid)
    del m_raw
    if _t is None:
        return set()
    _mmap = _formid_remap_table(master_esm)
    target = _mmap[_t >> 24] | (_t & 0x00FFFFFF)

    _scan_cell_coords(plugin_esm, coords)
    raw = plugin_esm.read_bytes()
    n = len(raw)
    gmap = _formid_remap_table(plugin_esm)

    def g(fid: int) -> int:
        return gmap[fid >> 24] | (fid & 0x00FFFFFF)

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
                    nxt_wrld = g(struct.unpack_from('<I', label)[0])
                elif g_type == 6:
                    nxt_cell = g(struct.unpack_from('<I', label)[0])
                scan(p + 24, p + g_size, nxt_cell, nxt_wrld)
                p += g_size
                continue
            fid = g(struct.unpack_from('<I', raw, p + 12)[0])
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


def touched_worldspace_fids(plugin_esm: Path) -> set:
    """Every worldspace FormID `plugin_esm` actually has WRLD/CELL/LAND/REFR under.

    Answers the question the master chain cannot: a plugin DEPENDING on the
    worldspace's owner is not the same as it EDITING that worldspace. Attaching
    an overlay that touches nothing costs a full parse of the file per
    worldspace and contributes zero records.

    Measured on the current 12-plugin selection: all 9 Oblivion.esm dependents
    were stacked onto all 18 of its worldspaces (162 overlay parses, 114 s), yet
    most touch exactly ONE worldspace. Scoping on this leaves 7 useful parses.

    Records are attributed by their enclosing type-1 GRUP label, which is the
    DEFINING file's WRLD FormID, so an override plugin that edits a master's
    worldspace without shipping a WRLD record of its own is still detected.
    A plugin's own WRLD records count too, so a file that defines a worldspace
    but has yet to place anything in it is not mistaken for uninvolved.

    Judged from THIS FILE'S OWN records only — a plugin's scope is what it
    itself places, never what another file's numbering implies. The returned
    ids are NORMALISED (`lod_gen._formid_remap_table`) so they can be compared
    against a worldspace id resolved from a different file.

    Both halves matter. A raw FormID's index byte offsets into its own file's
    master list, so Morrowind_ob.esm's 02xxxxxx and Tamriel.esp's 02xxxxxx are
    both "self" and name unrelated records; the two collide on 4 CELL ids, and
    resolving one plugin's cells against the other's table reads that
    coincidence as 183 overrides, dragging Morrowind INTERIOR objects into
    Cyrodiil's distant terrain.

    One linear header walk; record bodies are skipped, never parsed.
    """
    from .lod_gen import _formid_remap_table
    plugin_esm = Path(plugin_esm)
    gmap = _formid_remap_table(plugin_esm)
    raw = plugin_esm.read_bytes()
    n = len(raw)
    found: set = set()

    def g(fid: int) -> int:
        return gmap[fid >> 24] | (fid & 0x00FFFFFF)

    def scan(start, end, cur_wrld):
        p = start
        while p < end and p + 24 <= n:
            sig = raw[p:p + 4]
            size = struct.unpack_from('<I', raw, p + 4)[0]
            if sig == b'GRUP':
                g_type = struct.unpack_from('<I', raw, p + 12)[0]
                nxt = cur_wrld
                if g_type == 1:
                    nxt = g(struct.unpack_from('<I', raw, p + 8)[0])
                scan(p + 24, p + size, nxt)
                p += size
                continue
            if sig == b'WRLD':
                found.add(g(struct.unpack_from('<I', raw, p + 12)[0]))
            elif cur_wrld and sig in (b'CELL', b'LAND', b'REFR'):
                found.add(cur_wrld)
            p += 24 + size

    if n < 24:
        return found
    scan(24 + struct.unpack_from('<I', raw, 4)[0], n, 0)
    return found


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
        if not p.is_dir() or p.name in (LOD_DIR_NAME, MERGED_DIR_NAME):
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


def _master_rank(names: list[str], export_root: Path) -> dict[str, int]:
    """Master DEPTH per plugin: 0 for a plugin depending on none of `names`.

    Depth is what keeps a master from overwriting its own dependent's LOD. A
    dependent's tiles are baked as "master + dependent", so they already
    contain the master's terrain; the master's own tiles do not contain the
    dependent's. Applying the master LAST would therefore undo the dependent —
    so a master must always sort BEFORE anything that depends on it, however
    the alphabet falls.

    Cycles cannot occur in a real master list, but a malformed header could
    produce one, so the walk carries its own `seen` set and reports 0 rather
    than recursing forever.
    """
    depth: dict[str, int] = {}

    def d(name: str, seen: frozenset = frozenset()) -> int:
        if name in depth:
            return depth[name]
        if name in seen:
            return 0
        val = 1 + max([d(m, seen | {name})
                       for m in _master_names(export_root / name)
                       if m in names], default=-1)
        depth[name] = val
        return val

    for n in names:
        d(n)
    return depth


def create_lod_order(names: list[str], export_root: Path) -> list[str]:
    """The default plugin order for a Create LOD run, lowest priority first.

    Two sources, in the order the user asked for:

    1. `plugins.txt` — the real Skyrim load order. Whatever it lists comes
       FIRST, in exactly its own order, because that is the order the game
       itself resolves these plugins in and the user chose it.
    2. Everything else, appended at the bottom, sorted alphabetically but
       constrained by MASTERS: a plugin never sorts before one of its own
       masters. Alphabetical alone would let ``AAAPatch.esp`` (mastered on
       ``Tamriel.esp``) apply first and then be overwritten by the very plugin
       it patches; sorting by master depth first makes the dependent always
       land after the thing it depends on.

    This differs from `_load_order`, which puts unlisted plugins FIRST so an
    unpositioned plugin can never outrank a positioned one. That rule is right
    for a merge the user never looked at. Here the list is shown, reorderable
    and confirmed before anything runs, so the user's stated preference —
    plugins.txt first, the rest appended at the bottom — is what is built.
    """
    rank = {n.lower(): i for i, n in enumerate(plugins_txt_order())}
    listed = sorted((n for n in names if n.lower() in rank),
                    key=lambda n: rank[n.lower()])
    rest = [n for n in names if n.lower() not in rank]
    depth = _master_rank(names, export_root)
    # .esm before .esp at equal depth, matching the engine's own split, then
    # name for a stable, predictable tiebreak.
    rest.sort(key=lambda n: (depth.get(n, 0),
                             not n.lower().endswith('.esm'), n.lower()))
    return listed + rest


def worldspaces_by_plugin(names: list[str],
                          export_root: Path) -> dict[str, list[str]]:
    """{plugin: [worldspace EDID, ...]} for each of `names`.

    The same authority `phase_lod` routes on — the worldspaces the SOURCE game
    shipped distant LOD for — so the dialog offers exactly the set a run would
    otherwise build, and unticking one genuinely removes work rather than
    filtering a list that never matched.

    Returned per plugin, not flattened, because the dialog has to recompute the
    worldspace list as plugins are ticked on and off. Scanning the export dirs
    is the expensive part, so it happens ONCE when the dialog opens and every
    later toggle is a dict lookup.
    """
    out: dict[str, list[str]] = {}
    for name in names:
        try:
            shipped = shipped_lod_worldspaces(export_root / name) or []
        except Exception:
            shipped = []
        out[name] = [edid for edid, _fid in shipped]
    return out


def lod_worldspaces(names: list[str], export_root: Path) -> list[str]:
    """Every worldspace the selected plugins would generate LOD for.

    Ordered by first appearance across `names` so the biggest, most-edited
    worldspaces (which is the order `shipped_lod_worldspaces` already returns
    per plugin) surface at the top.
    """
    return merge_worldspaces(names, worldspaces_by_plugin(names, export_root))


def merge_worldspaces(names, by_plugin: dict[str, list[str]]) -> list[str]:
    """Flatten `worldspaces_by_plugin` for `names`, first appearance wins.

    Split out from the scan so the dialog can re-flatten on every tick without
    re-reading a single export directory.
    """
    seen: list[str] = []
    for name in names:
        for edid in by_plugin.get(name, ()):
            if edid not in seen:
                seen.append(edid)
    return seen


def worldspace_owner(edid: str, order: list[str], export_root: Path):
    """The plugin whose records a worldspace's LOD is baked FROM.

    The FIRST plugin in load order that shipped distant LOD for `edid` — the
    same authority `phase_lod` routes on, so both agree on who owns what.

    Ownership matters because the bake reads WRLD/CELL/LAND/REFR out of ONE
    file and applies the rest as overlays. Sourcing from a later plugin that
    merely EXTENDS the worldspace would drop everything the owner holds:
    Tamriel.esp adds a landmass around Cyrodiil, and building from it alone
    left all of Oblivion.esm's terrain missing and edge-extended into flat
    plateaus at the vanilla border.
    """
    for name in order:
        try:
            shipped = shipped_lod_worldspaces(export_root / name) or []
        except Exception:
            continue
        if any(e == edid for e, _f in shipped):
            return name
    return None


def dependents_of(names: list[str], export_root: Path) -> dict[str, set[str]]:
    """{plugin: every plugin in `names` that depends on it, transitively}.

    Deselecting a plugin has to deselect everything built on top of it: a
    dependent's LOD is baked as "master + dependent", so with the master
    dropped there is no terrain to overlay onto and the dependent's tiles would
    come out as its own isolated edits floating in nothing.

    Transitive because the dependency that matters can be indirect —
    Translation.esp lists only Nehrim.esm, so dropping Nehrim must drop
    Translation even though nothing names the two together.
    """
    direct: dict[str, list[str]] = {
        n: [m for m in _master_names(export_root / n) if m in names]
        for n in names}

    out: dict[str, set[str]] = {n: set() for n in names}
    for n in names:
        # Walk UP from each plugin to every master it rests on, and record the
        # plugin against each. Cheaper and cycle-safe compared with walking
        # down from every master, and a malformed header that made A master B
        # and B master A terminates on the `seen` guard instead of recursing.
        stack = list(direct[n])
        seen: set[str] = set()
        while stack:
            m = stack.pop()
            if m in seen or m == n:
                continue
            seen.add(m)
            out[m].add(n)
            stack.extend(direct.get(m, ()))
    return out


def _load_order(names: list[str], export_root: Path,
                explicit: list[str] | None = None) -> list[str]:
    """The order sibling overlays are applied in — i.e. who wins a conflict.

    The LAST overlay applied replaces earlier ones for any shared FormID, so
    this order IS the conflict resolution, not a cosmetic detail.

    Three sources, in descending authority:

    1. `explicit` — an order the user arranged by hand in the GUI. Absolute:
       whatever they dragged is what runs.
    2. `plugins.txt` — the real Skyrim load order, which is what the game
       itself obeys. Anything it does not mention sorts BEFORE everything it
       does, so a plugin the user never placed can never outrank one they did.
    3. Structural fallback, used only when plugins.txt is missing or lists
       none of these plugins: master-depth, then .esm before .esp, then name.

    The structural fallback alone used to be the whole implementation, and its
    alphabetical tiebreak is an ARBITRARY winner for two siblings that edit the
    same reference — "ElsweyrAnequina before Tamriel" was alphabetical accident
    rather than anything the user chose.

    Unlisted plugins used to be appended AFTER the ranked ones, which handed
    the highest priority — the last word on every contested tile — to exactly
    the plugins the user never positioned. DLCBattlehornCastle.esp (14 changed
    cells, absent from plugins.txt) thereby outranked ElsweyrAnequina.esp
    (1,855 cells) and Tamriel.esp (99,910), and won every tile the three
    shared, so merged tiles disagreed with the load order the game itself
    obeys. Sorting them first makes an unknown plugin the LOWEST priority,
    which is also what the engine does with a plugin that is not in the list:
    it is not loaded at all.
    """
    if explicit:
        # Honour the user's arrangement; anything they never saw (a plugin
        # converted since) still has to run, but it sorts BEFORE their choices
        # for the same reason as the plugins.txt case below — a plugin the
        # user never positioned must not win a tile against one they did.
        chosen = [n for n in explicit if n in names]
        return sorted(n for n in names if n not in chosen) + chosen

    lo = [n.lower() for n in plugins_txt_order()]
    if lo:
        rank = {name: i for i, name in enumerate(lo)}
        listed = [n for n in names if n.lower() in rank]
        if listed:
            unlisted = sorted(n for n in names if n.lower() not in rank)
            return unlisted + sorted(listed, key=lambda n: rank[n.lower()])

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


def _wrld_bounds(esm: Path, edid: str):
    """(minX, minY, maxX, maxY) from a plugin's WRLD record, or None.

    Reads the built ESM rather than the export so the bounds are exactly what
    the engine will see, including anything the override path rewrote.
    """
    try:
        data = esm.read_bytes()
    except OSError:
        return None
    sig_ok = re.compile(rb'[A-Z0-9_]{4}')
    off = 0
    while True:
        k = data.find(b'WRLD', off)
        if k < 0:
            return None
        off = k + 4
        if k + 24 > len(data):
            return None
        size, flags = struct.unpack('<II', data[k + 4:k + 12])
        if size == 0 or size > 500000 or (flags & 0x00040000):
            continue
        body = data[k + 24:k + 24 + size]
        j = 0
        name = None
        n0 = n9 = None
        while j + 6 <= len(body):
            sig = body[j:j + 4]
            if not sig_ok.fullmatch(sig):
                break
            sz = struct.unpack('<H', body[j + 4:j + 6])[0]
            val = body[j + 6:j + 6 + sz]
            if sig == b'EDID':
                name = val.rstrip(b'\0').decode('ascii', 'replace')
            elif sig == b'NAM0' and sz == 8:
                n0 = struct.unpack('<ff', val)
            elif sig == b'NAM9' and sz == 8:
                n9 = struct.unpack('<ff', val)
            j += 6 + sz
        if name == edid and n0 and n9:
            return (n0[0], n0[1], n9[0], n9[1])


def merge_cloud_bank(out_root: Path, merged_dir: Path, edid: str,
                     master: str, plugins: list[str]) -> str:
    """One world-map cloud bank covering the UNION of every sibling's bounds.

    Same overwrite problem the LOD tiles have, one level up.  The bank is a
    FILE at a fixed path (meshes/tes4/worldmapclouds/<worldspace>.nif) and each
    sibling generates its own sized to ITS OWN NAM0/NAM9 rectangle -- correct
    in isolation, wrong together.  Tamriel.esp and ElsweyrAnequina.esp both
    extend TES4Tamriel in different directions, so whichever the mod manager
    installs last supplies the bank for both, and it is sized for only one of
    them.  The plugin that loses gets a deck that stops short of its terrain.

    The bounds have the same problem in the record: the winning WRLD override
    supplies NAM0/NAM9 for everyone, and the map is drawn over exactly that
    rectangle.  So the honest fit is the union of every sibling's rectangle --
    that is the extent the map will actually show once they are all installed.

    Written into the merged folder, which installs last and wins the overwrite
    deliberately, exactly like the merged tiles.  Returns the Data-relative
    path written, or None when no bank could be built.
    """
    from .worldmap_clouds import generate_cloud_bank, cloud_model_path

    boxes = []
    for name in [master] + list(plugins):
        esm = out_root / name / name
        if not esm.is_file():
            continue
        box = _wrld_bounds(esm, edid)
        if box:
            boxes.append(box)
    if not boxes:
        return None

    min_x = min(b[0] for b in boxes)
    min_y = min(b[1] for b in boxes)
    max_x = max(b[2] for b in boxes)
    max_y = max(b[3] for b in boxes)
    width = abs(max_x - min_x)
    height = abs(max_y - min_y)
    if width <= 0.0 or height <= 0.0:
        return None

    if not generate_cloud_bank(edid, width, height, str(merged_dir)):
        return None
    return cloud_model_path(edid)


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
