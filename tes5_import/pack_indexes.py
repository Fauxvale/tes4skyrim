"""The record indexes a PackContext needs, built once per import.

Everything pack_converter asks about a package's targets and locations — what
KIND of thing a ref or a base is, where each base is placed, which cells are
interiors, where each package's runners stand — comes from these tables.
They are built here, in one place, so `import_main` (the real import) and
`tools/esm/pack_audit.py` (the routing census) see the same context and the
audit measures the production routing rather than a stand-in.

Every map is keyed on the RAW LOW-24 FormID, which is identical in a master's
index space and ours, so a master's record is seeded straight from its own
`FormID` field with no remapping.  The masters go in FIRST so this plugin's
own records win — without them a package targeting one of the master's refs
resolves to no signature at all, and _operate_target / the Find branches fall
through to the default (see CLAUDE.md, master-export blindness).
"""

from .text_reader import get_formid

# Every TES4 base signature a placed REFR/ACHR/ACRE (and so a package target)
# can have.
PLACEABLE_BASE_SIGS = (
    'NPC_', 'CREA', 'ACTI', 'FURN', 'DOOR', 'CONT', 'STAT', 'MISC', 'LIGH',
    'WEAP', 'ARMO', 'CLOT', 'BOOK', 'INGR', 'ALCH', 'KEYM', 'SGST', 'SLGM',
    'AMMO', 'APPA', 'FLOR', 'TREE', 'GRAS', 'SBSP', 'LVLC', 'LVLI', 'SOUN',
)


def _low24(rec: dict, key: str = 'FormID') -> int:
    return int(rec.get(key, '0') or '0', 16) & 0xFFFFFF


def build_pack_indexes(by_type: dict, master_export: dict = None) -> dict:
    """Return the keyword arguments for PackContext(...) that describe the
    plugin's records (everything except plan/script_vars/greeting_topic).

    Keys: ref_base_sig, base_sig, base_placements, interior_cells, ref_cell,
    pack_runner_cells.
    """
    # One pass over the masters' records (Oblivion.esm is ~1.2M of them), so
    # each signature below is an O(1) lookup rather than a full rescan.
    master_by_type = {}
    if master_export:
        for r in master_export.values():
            master_by_type.setdefault(r.get('Signature'), []).append(r)

    def _iter_bases(sigs):
        for sig in sigs:
            for r in master_by_type.get(sig, ()):
                yield sig, r
            for r in by_type.get(sig, []):
                yield sig, r

    # BASE fid -> its signature, for EVERY placeable base: a package target may
    # be a REFR of any of them (MS39SinderionMakesElixir's APPA-based mortar
    # resolved to nothing under a shorter list), and a type-1 Object-ID target
    # names one of these bases directly, where the KIND (actor / item /
    # furniture) picks the template — see pack_converter._find_object_id.
    base_sig = {}
    for sig, r in _iter_bases(PLACEABLE_BASE_SIGS):
        try:
            base_sig[_low24(r)] = sig
        except ValueError:
            pass

    # REFR -> base signature, so UseItemAt can tell furniture (sit) from a
    # switch/lever/door (activate); placed ACTORS report their base kind so a
    # Find can tell "seek this actor" from "operate this object"
    # (CGAssassinsAmbushAToGlenroy targets Glenroy's ACHR).
    ref_base_sig = {}
    for sig, r in _iter_bases(('REFR',)):
        b = r.get('NAME')
        if not b:
            continue
        try:
            bs = base_sig.get(int(b, 16) & 0xFFFFFF)
            if bs:
                ref_base_sig[_low24(r)] = bs
        except ValueError:
            pass
    for sig, bs in (('ACHR', 'NPC_'), ('ACRE', 'CREA')):
        for _, r in _iter_bases((sig,)):
            try:
                ref_base_sig[_low24(r)] = bs
            except ValueError:
                pass

    # Where each BASE is placed: raw24 base -> [(ref fid [remapped], raw24
    # cell), ...].  A type-1 Object-ID target with exactly ONE placement is
    # that reference (FindFathisUles -> FathisUlesRef); with several, their
    # cell is the search/hunting ground (FGC06's nine goblins in DesolateMine).
    # See PackContext.sole_placement / search_ground.
    base_placements = {}
    # Where every placed thing STANDS (see PackContext.location_reachable),
    # and which cells each base actor stands in.
    ref_cell = {}
    base_cell = {}
    # Placed ACTORS' positions (raw24 ACHR/ACRE -> (x, y, z)) and which actor
    # refs run each package: a hunt's seek chain is ordered nearest-first from
    # the hunter's own placement (see pack_converter.hunt_chain_targets).
    actor_pos = {}
    base_refs = {}
    for sig, r in _iter_bases(('ACHR', 'ACRE', 'REFR')):
        b = r.get('NAME')
        c = r.get('ParentCELL')
        try:
            fid = _low24(r)
            cell = int(c, 16) & 0xFFFFFF if c else 0
            if b:
                base = int(b, 16) & 0xFFFFFF
                base_placements.setdefault(base, []).append(
                    (get_formid(r, 'FormID'), cell))
                if sig != 'REFR':
                    if cell:
                        base_cell.setdefault(base, set()).add(cell)
                    base_refs.setdefault(base, set()).add(fid)
                    actor_pos[fid] = (float(r.get('PosX', 0) or 0),
                                      float(r.get('PosY', 0) or 0),
                                      float(r.get('PosZ', 0) or 0))
            if cell:
                ref_cell[fid] = cell
        except ValueError:
            pass

    # Interior cells (CELL DATA flag 0x1): the only kind a PLDT type-1
    # "in cell" location may name (448/448 vanilla uses are interiors).
    interior_cells = set()
    for _, r in _iter_bases(('CELL',)):
        try:
            if int(r.get('DATA.Flags', '0') or '0') & 0x1:
                interior_cells.add(_low24(r))
        except ValueError:
            pass

    # PACK fid -> the set of cells the actors running it stand in.  A TES4
    # Follow that names a destination in ANOTHER cell must not become a Skyrim
    # Escort: Escort walks to the destination, and there is no navmesh route
    # between two interiors, so the actor never moves (Nehrim MQ00 — Celebro
    # in StartCelle, his marker in SchattenrufMinePart01).
    pack_runner_cells = {}
    pack_runner_refs = {}
    for _, r in _iter_bases(('NPC_', 'CREA')):
        try:
            base = _low24(r)
        except ValueError:
            continue
        cells = base_cell.get(base)
        refs = base_refs.get(base)
        if not cells and not refs:
            continue
        n = int(r.get('AIPackageCount', '0') or 0)
        for i in range(n):
            p = r.get(f'AIPackage[{i}]')
            if not p:
                continue
            try:
                pk = int(p, 16) & 0xFFFFFF
            except ValueError:
                continue
            if cells:
                pack_runner_cells.setdefault(pk, set()).update(cells)
            if refs:
                pack_runner_refs.setdefault(pk, set()).update(refs)

    return dict(ref_base_sig=ref_base_sig, base_sig=base_sig,
                base_placements=base_placements,
                interior_cells=interior_cells, ref_cell=ref_cell,
                pack_runner_cells=pack_runner_cells,
                pack_runner_refs=pack_runner_refs, actor_pos=actor_pos)
