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
import shutil
import struct
import zlib
from collections import defaultdict
from pathlib import Path

from .terrain_lod import (shipped_lod_worldspaces, _master_names,
                          _find_worldspace_fid, _scan_cell_coords)
from .lod_gen import _kept_tile_cells_by_level


# Shared-folder resolution -- see output_layout. An imported mod's plugins keep
# their records in `export/<Mod>/<plugin>/` and write into `output/<Mod>/`, so a
# name must never be joined onto a root by hand.

def _record_dir(export_root, plugin: str) -> Path:
    try:
        from output_layout import record_dir
        return record_dir(export_root, plugin)
    except ImportError:
        return Path(export_root) / plugin


def _out_root(out_root, plugin: str, export_root=None) -> Path:
    try:
        from output_layout import plugin_out_root
        return plugin_out_root(out_root, plugin,
                               str(export_root) if export_root else None)
    except ImportError:
        return Path(out_root) / plugin


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


def _lod_mod_deliverables(lod_dir: Path) -> list:
    """Mesh subtrees the LOD mod legitimately OWNS, as absolute dirs.

    Everything else under `meshes/` is scratch. Kept as one list so a new
    generator that writes into the LOD mod has exactly one place to register
    itself, instead of the sweep silently eating its output.
    """
    from .worldmap_clouds import _OUT_DIR as _CLOUD_DIR

    return [
        # The baked tiles -- the whole point of the mod.
        lod_dir / 'meshes' / 'terrain',
        # World-map cloud banks: ONE file per worldspace at a fixed shared
        # path, written here by merge_cloud_bank from the UNION of every
        # sibling's land. The per-plugin copies are the rival versions; this
        # is the authoritative one, so it must survive the sweep.
        lod_dir / 'meshes' / _CLOUD_DIR.replace(chr(92), '/'),
    ]


def drop_staged_meshes(lod_dir: Path) -> int:
    """Delete the meshes a previous bake staged into the LOD mod.

    Staged models are scratch: LODGen resolves geometry under a single
    PathData root, so `lod_gen._import_master_mesh` copies each model in for
    the duration of the bake and `_drop_staged_master_meshes` removes it
    afterwards. Nothing DERIVES an ordinary mesh here -- `far_nif_dirs` routes
    every generated _far.nif to the plugin tree that ships its full model,
    which is also where lod_far_gen writes the `.nif.generated` marker that
    proves authorship. Staging copies the .nif alone and never its marker, so
    a staged mesh in this tree has no provenance to lose. Measured before this
    first ran: 4,120 .nif here, 0 markers.

    What the mod genuinely owns is listed by `_lod_mod_deliverables` and is
    never touched -- the tiles, and the world-map cloud banks that
    `merge_cloud_bank` writes here precisely BECAUSE the per-plugin copies
    conflict. Sweeping by "everything that is not terrain/" deleted those
    banks, which was survivable only because create_lod happens to rewrite
    them later in the same run; a sweep-only invocation would have left every
    worldspace's MODL pointing at a missing mesh.

    Scratch is removed as whole SUBTREES where a top-level directory holds no
    deliverable (one rmtree, not thousands of unlinks), and file-by-file only
    inside a directory that also holds one. Returns the number of files
    removed.

    The sweep is needed because the in-process set cannot survive a run.
    `_import_master_mesh` early-returns for a mesh that is already present and
    so never registers it, which means anything a killed or pre-one-bake run
    left behind is invisible to the post-bake cleanup and pins itself forever
    -- shadowing, since the LOD mod installs last to win the tile overwrite,
    every plugin's current copy of that mesh.
    """
    meshes = lod_dir / 'meshes'
    if not meshes.is_dir():
        return 0

    keep = [Path(os.path.normcase(str(d))) for d in _lod_mod_deliverables(lod_dir)]

    def _protected(d: Path) -> bool:
        """True if `d` is, or contains, a deliverable."""
        nd = Path(os.path.normcase(str(d)))
        return any(k == nd or nd in k.parents or k in nd.parents for k in keep)

    def _is_deliverable_dir(d: Path) -> bool:
        """True only for a deliverable root or something inside one."""
        nd = Path(os.path.normcase(str(d)))
        return any(k == nd or k in nd.parents for k in keep)

    def _count(d: Path) -> int:
        return sum(len(f) for _r, _dirs, f in os.walk(d))

    n = 0
    for child in sorted(meshes.iterdir()):
        if not child.is_dir():
            continue
        if not _protected(child):
            n += _count(child)
            shutil.rmtree(child, ignore_errors=True)
            continue
        # Mixed: this subtree holds a deliverable somewhere beneath it, so
        # recurse and drop only the branches that hold none.
        stack = [child]
        while stack:
            cur = stack.pop()
            for sub in sorted(cur.iterdir()):
                if sub.is_dir():
                    if _protected(sub):
                        stack.append(sub)
                    else:
                        n += _count(sub)
                        shutil.rmtree(sub, ignore_errors=True)
                elif not _is_deliverable_dir(cur):
                    # A loose file inside a directory that merely CONTAINS a
                    # deliverable is still scratch; only files sitting in the
                    # deliverable directory itself are kept.
                    sub.unlink()
                    n += 1
    return n


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
        for m in _master_names(_record_dir(export_root, cur)):
            if m in seen or m not in known:
                continue
            seen.add(m)
            stack.append(m)
    return seen


def converted_plugins(out_root: Path) -> list[str]:
    """Every plugin with a converted ESM in `out_root`, in name order.

    Two folder shapes both count. A plugin converted on its own lives in a
    folder named after it (`output/Oblivion.esm/Oblivion.esm`). Plugins
    imported together from one mod archive share a folder named after the MOD,
    and are found by their `<name>.manifest.json`.

    Either way the plugin file itself must be present — output/ also collects
    the shared `Slot44 Patch.esp`, this step's own merged folder, and whatever
    else the pipeline drops at the root.
    """
    if not out_root.is_dir():
        return []
    names = []
    for p in sorted(out_root.iterdir()):
        if not p.is_dir() or p.name in (LOD_DIR_NAME, MERGED_DIR_NAME):
            continue
        if (p / p.name).is_file():
            names.append(p.name)
            continue
        # A GROUP folder is named for the mod, not for any one plugin, so the
        # `<folder>/<folder>` test above cannot see the plugins inside it.
        # Every converted plugin writes `<name>.manifest.json` beside itself,
        # which is what distinguishes a real plugin from a stray .esp copied
        # into the tree.
        for man in sorted(p.glob('*.manifest.json')):
            plugin = man.name[:-len('.manifest.json')]
            if (p / plugin).is_file():
                names.append(plugin)
    return sorted(set(names), key=str.lower)


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
                       for m in _master_names(_record_dir(export_root, name))
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


def worldspaces_by_plugin(names: list[str], export_root: Path,
                          out_root: Path = None) -> dict[str, list[str]]:
    """{plugin: [worldspace EDID, ...]} for each of `names`.

    The same authority `worldspace_owner` routes on, so the dialog offers
    exactly the set a run would build and unticking one genuinely removes work.

    `out_root` is what makes MOD-ADDED worldspaces visible: a plugin that adds
    a landmass ships no LOD assets, so without the converted ESM to read
    terrain from it looks like it has no worldspaces at all. Optional only so
    callers that genuinely want the shipped-only set can omit it.

    Returned per plugin, not flattened, because the dialog has to recompute the
    worldspace list as plugins are ticked on and off. Scanning is the expensive
    part, so it happens ONCE when the dialog opens and every later toggle is a
    dict lookup.
    """
    out, _reasons = worldspaces_by_plugin_diagnosed(names, export_root,
                                                    out_root)
    return out


def worldspaces_by_plugin_diagnosed(names: list[str], export_root: Path,
                                    out_root: Path = None):
    """`worldspaces_by_plugin` plus why each empty plugin came back empty.

    Returns ({plugin: [edid, ...]}, {plugin: reason}). Only plugins with
    nothing to offer appear in the reason map.
    """
    from .terrain_lod import lod_capable_worldspaces

    out: dict[str, list[str]] = {}
    reasons: dict[str, str] = {}
    for name in names:
        try:
            found, why = lod_capable_worldspaces(
                _record_dir(export_root, name), out_root, plugin=name)
        except Exception as exc:
            found, why = [], f"{name}: scan failed ({exc})."
        out[name] = [edid for edid, _fid in found]
        if why:
            reasons[name] = why
    return out, reasons


def lod_worldspaces(names: list[str], export_root: Path,
                    out_root: Path = None) -> list[str]:
    """Every worldspace the selected plugins would generate LOD for.

    Ordered by first appearance across `names` so the biggest, most-edited
    worldspaces (which is the order `shipped_lod_worldspaces` already returns
    per plugin) surface at the top.
    """
    return merge_worldspaces(names, worldspaces_by_plugin(names, export_root,
                                                          out_root))


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


def worldspace_owner(edid: str, order: list[str], export_root: Path,
                     out_root: Path = None):
    """The plugin whose records a worldspace's LOD is baked FROM.

    The FIRST plugin in load order that can supply `edid`'s terrain. Shipped
    LOD assets alone were the old test, which silently skipped every worldspace
    a MOD adds — those ship no LOD, so the bake printed "no selected plugin
    ships LOD for it" and generated nothing for entire landmasses. Ownership
    now falls back to "defines the worldspace with terrain in its converted
    ESM", which is the property the bake actually needs.

    Ownership matters because the bake reads WRLD/CELL/LAND/REFR out of ONE
    file and applies the rest as overlays. Sourcing from a later plugin that
    merely EXTENDS the worldspace would drop everything the owner holds:
    Tamriel.esp adds a landmass around Cyrodiil, and building from it alone
    left all of Oblivion.esm's terrain missing and edge-extended into flat
    plateaus at the vanilla border.
    """
    return owner_map([edid], order, export_root, out_root).get(edid)


def _shipped_edids(export_dir: Path) -> frozenset:
    """EDIDs a plugin shipped LOD assets for."""
    try:
        return frozenset(e for e, _f in
                         (shipped_lod_worldspaces(export_dir) or []))
    except Exception:
        return frozenset()


def _defined_edids(esm: Path) -> frozenset:
    """EDIDs of every ROOT worldspace defined in a converted ESM."""
    from .terrain_lod import detect_terrain_worldspaces
    try:
        return frozenset(e for _sz, _f, e in detect_terrain_worldspaces(esm))
    except Exception:
        return frozenset()


def owner_map(edids, order: list[str], export_root: Path,
              out_root: Path = None) -> dict:
    """{worldspace EDID: owning plugin}, resolved in ONE pass over the order.

    Two-pass precedence, and the order of the passes is the point: a plugin
    that SHIPPED LOD for a worldspace outranks any later plugin that merely
    defines it. That is what keeps Oblivion.esm owning TES4Tamriel rather than
    Tamriel.esp, which only extends it — sourcing from the extender would drop
    all of Oblivion's terrain. Pass 2 then covers mod-added worldspaces, which
    ship no LOD at all and used to be skipped entirely.

    Resolving the whole load order at once rather than per worldspace: the
    per-worldspace form re-listed every plugin's export dir for each of 80
    worldspaces.
    """
    wanted = set(edids)
    out: dict = {}
    for name in order:                      # pass 1: shipped LOD wins
        for e in _shipped_edids(_record_dir(export_root, name)) & wanted:
            out.setdefault(e, name)
    if out_root is not None:
        for name in order:                  # pass 2: defined in the ESM
            rest = wanted - out.keys()
            if not rest:
                break
            esm = _out_root(out_root, name, export_root) / name
            if esm.is_file():
                for e in _defined_edids(esm) & rest:
                    out.setdefault(e, name)
    return out


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
        n: [m for m in _master_names(_record_dir(export_root, n)) if m in names]
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
        masters = _master_names(_record_dir(export_root, name))
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
        rec = _record_dir(export_root, name)
        for edid, _fid in (shipped_lod_worldspaces(rec) or []):
            owner.setdefault(edid, name)

    groups: dict[str, dict] = {}
    for edid, master in owner.items():
        master_esm = _out_root(out_root, master, export_root) / master
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
            esm = _out_root(out_root, name, export_root) / name
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


def _wrld_land_bounds(esm: Path, wrld_fid: int):
    """(minX, minY, maxX, maxY) of a plugin's real terrain in worldspace
    `wrld_fid`, measured from its exterior CELL grid, or None.

    Why not the WRLD record: a dependent plugin overrides the master's WRLD
    without touching MNAM/NAM0/NAM9, so every sibling reports the SAME
    rectangle and the "union" collapses to the master's.  Measured on the real
    outputs, all five TES4Tamriel contributors return X[-241664,245760]
    (487424 x 434176) while their combined land actually spans 1572864 x
    1183744 -- a deck 3.2x too small, which is precisely the overwrite bug
    merge_cloud_bank exists to prevent.

    Cells are read straight out of the built ESM: CELL records carry XCLC
    (grid X, grid Y) and sit inside the GRUP tree under their worldspace.
    Persistent cells (RecordFlags 0x400) are skipped -- they hold the
    worldspace's persistent refs, are commonly parked at a dummy (0,0), and
    would drag the extent toward the origin.
    """
    try:
        data = esm.read_bytes()
    except OSError:
        return None

    min_gx = min_gy = None
    max_gx = max_gy = None
    cur_world = None
    i = 0
    n = len(data)
    while i + 24 <= n:
        sig = data[i:i + 4]
        if sig == b'GRUP':
            size, label, gtype = struct.unpack('<I4si', data[i + 4:i + 16])
            # type 1 = worldspace children; the label is the WRLD FormID.
            if gtype == 1:
                cur_world = struct.unpack('<I', label)[0]
            i += 24
            continue
        size, flags = struct.unpack('<II', data[i + 4:i + 12])
        if sig == b'CELL' and cur_world == wrld_fid and not (flags & 0x400):
            body = data[i + 24:i + 24 + size]
            if flags & 0x00040000:
                try:
                    body = zlib.decompress(body[4:])
                except Exception:
                    body = b''
            j = 0
            while j + 6 <= len(body):
                sub = body[j:j + 4]
                sz = struct.unpack('<H', body[j + 4:j + 6])[0]
                if sub == b'XCLC' and sz >= 8:
                    gx, gy = struct.unpack('<ii', body[j + 6:j + 14])
                    min_gx = gx if min_gx is None else min(min_gx, gx)
                    max_gx = gx if max_gx is None else max(max_gx, gx)
                    min_gy = gy if min_gy is None else min(min_gy, gy)
                    max_gy = gy if max_gy is None else max(max_gy, gy)
                    break
                j += 6 + sz
        i += 24 + size

    if min_gx is None:
        return None
    return (min_gx * 4096.0, min_gy * 4096.0,
            (max_gx + 1) * 4096.0, (max_gy + 1) * 4096.0)


def _wrld_formid(esm: Path, edid: str):
    """FormID of the WRLD named `edid` in a built ESM, or None."""
    try:
        data = esm.read_bytes()
    except OSError:
        return None
    off = 0
    while True:
        k = data.find(b'WRLD', off)
        if k < 0:
            return None
        off = k + 4
        if k + 24 > len(data):
            return None
        size, flags, fid = struct.unpack('<IiI', data[k + 4:k + 16])
        if size == 0 or size > 500000 or (flags & 0x00040000):
            continue
        body = data[k + 24:k + 24 + size]
        j = 0
        while j + 6 <= len(body):
            sub = body[j:j + 4]
            if not re.fullmatch(rb'[A-Z0-9_]{4}', sub):
                break
            sz = struct.unpack('<H', body[j + 4:j + 6])[0]
            if sub == b'EDID':
                name = body[j + 6:j + 6 + sz].rstrip(b'\0')
                if name.decode('ascii', 'replace') == edid:
                    return fid
                break
            j += 6 + sz


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
        n0 = n9 = mnam = None
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
            elif sig == b'MNAM' and sz >= 16:
                mnam = struct.unpack('<hhhh', val[8:16])
            j += 6 + sz
        if name == edid:
            # The map frames MNAM's NW/SE cell corners, which on Skyrim itself
            # is only a third of the NAM0/NAM9 landmass rectangle -- the rest
            # is unexplorable filler the map never shows.  Prefer it, and fall
            # back to NAM0/NAM9 only when the corners are absent.
            if mnam:
                from .worldmap_clouds import framed_rect
                rect = framed_rect(mnam[0], mnam[1], mnam[2], mnam[3])
                if rect:
                    return rect
            if n0 and n9:
                return (n0[0], n0[1], n9[0], n9[1])


def merge_cloud_bank(out_root: Path, merged_dir: Path, edid: str,
                     master: str, plugins: list[str],
                     export_root: Path = None) -> str:
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
    from .worldmap_clouds import (generate_cloud_bank, cloud_model_path,
                                  compute_center)

    # Union of every sibling's real LAND, not of their WRLD records: a
    # dependent overrides the WRLD without touching MNAM/NAM, so record-based
    # bounds are identical for all of them and the union collapses to the
    # master's (see _wrld_land_bounds).  Fall back to the record only when a
    # plugin contributes no cells of its own.
    boxes = []
    for name in [master] + list(plugins):
        esm = _out_root(out_root, name, export_root) / name
        if not esm.is_file():
            continue
        box = None
        fid = _wrld_formid(esm, edid)
        if fid is not None:
            box = _wrld_land_bounds(esm, fid)
        if box is None:
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

    # Centred on the UNION's midpoint for the same reason it is sized off the
    # union: that is the rectangle the map actually draws once every sibling
    # is installed, and it is not centred on the worldspace origin.
    center = compute_center(min_x, min_y, max_x, max_y)
    if not generate_cloud_bank(edid, width, height, str(merged_dir),
                               center=center,
                               land_rect=(min_x, min_y, max_x, max_y)):
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
                     merged_dir_name: str = None,
                     export_root: Path = None) -> dict:
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
        own = _lod_files(_out_root(out_root, name, export_root), worldspace)
        hit = sorted(own & merged)
        report[name] = {'overwritten': hit, 'kept': len(own) - len(hit)}
    return report
