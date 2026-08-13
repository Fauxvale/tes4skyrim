"""Override orchestration for a plugin that has TES4 masters.

This is the plugin-side counterpart to `override_merge` (the master's converted
bytes), `master_manifest` (the master's companion pairings), `export_diff`
(what the author changed) and `override_builder` (applying those changes to the
master's record). It owns everything import_main needs to route a record down
the override path:

    ctx = OverrideContext(export_dir, masters, num_tes4_masters, output_root)
    ov = ctx.build(rec)          # None -> not an override, convert normally
    if ov.record_bytes: ...      # emit; else correctly dropped

The rule, stated once: an OVERRIDE is the master's converted record bytes
EXACTLY, with only the fields the author changed substituted in — and
authorship comes from diffing the two TES4 EXPORTS, never from comparing two
conversion runs (see export_diff for why that distinction is load-bearing).
"""

import os
import struct
from collections import Counter, namedtuple

from .export_diff import diff_records
from .master_manifest import load_master_manifests
from .override_builder import (_HEADER_SIZE, RECONVERT_KEYS, apply_changes,
                               rebuild_sndr_override, soun_companion_changes,
                               split_subrecords)
from .override_merge import load_master_index
from .text_reader import parse_export_directory, remap_formid
from .writer import PluginWriter, pack_group

# Types whose override CANNOT be expressed against the master's output because
# conversion does not produce a corresponding record to substitute into
# (PGRD/ROAD become generated NAVM/nothing). These are counted and reported,
# never silently dropped.
OVERRIDE_UNMAPPABLE_TYPES = frozenset({'PGRD', 'ROAD'})

# Record header flag bit 5. Both games use it, and the meaning is the same:
# the author DELETED this record. xEdit treats bit 5 as 'Deleted' for every
# signature (wbFlagsList's aDeleted branch, Core/wbInterface.pas:5874).
#
# A deleted override is an empty stub — FormID and flags only, with every
# subrecord stripped. That shape has to be recognised BEFORE the field diff
# runs, because a field-by-field comparison reads all those absent subrecords
# as authored changes: DLCBattlehornCastle deletes 74 references, and the diff
# reported them as 74 XSCL.Scale + 73 NAME "edits" with no mapping, so all 74
# shipped ALIVE at the master's position.
DELETED_FLAG = 0x20

Override = namedtuple('Override', ['status', 'out_fid', 'record_bytes'])
# status: 'emitted' | 'deleted' | 'unchanged' | 'no-base' | 'no-path'
#         | 'reconvert'


def _export_master_names(export_dir: str) -> list:
    """This plugin's TES4 master names, in load order, from its export header."""
    header = os.path.join(export_dir, '_HEADER.txt')
    if not os.path.isfile(header):
        return []
    names = []
    with open(header, 'r', encoding='utf-8') as f:
        for line in f:
            if line.startswith('Master['):
                _, _, val = line.partition('=')
                names.append(val.strip())
    return names


def load_master_export(export_dir: str) -> dict:
    """The masters' export records, keyed by the TES4 FormID THIS PLUGIN uses.

    This is the baseline for deciding what a plugin's author actually changed.
    Diffing against the master's EXPORT (rather than against a second
    conversion) is what makes the override path deterministic: a field neither
    export touches is never rewritten, so it cannot drift.

    **Each master's ids are re-keyed into THIS plugin's index space.** A record
    is named by its index byte, which is the master's slot in *this* plugin's
    master list — NOT the slot it uses in its own file. Merging the exports on
    the raw id instead collapses every master's id space into one, so the
    last-loaded master silently wins ids that belong to an earlier one, and the
    override is then diffed against a record of a COMPLETELY DIFFERENT TYPE.

    Measured on TWMP Valenwood/Elsweyr (masters Oblivion.esm, Tamriel.esp,
    ElsweyrAnequina.esp): 119,443 of 121,505 shared ids resolved to the wrong
    record type — 59,770 LAND and 59,668 CELL clobbered, mostly by ANQ's REFRs.
    `0102DDE5` is a LAND in Tamriel.esp and the creature ANQCORPantherCaged
    ("Black Panther Cub") in ElsweyrAnequina.esp, so the terrain override was
    diffed against a creature and the builder spliced that creature's FULL (and
    DESC) into the LAND. xEdit: "record LAND contains unexpected (or out of
    order) subrecord FULL", and the engine hangs forever on the main menu.

    A master's OWN records carry the index byte equal to its own master count.
    Ids BELOW that belong to the master's own masters and are translated by
    NAME through this plugin's list — the two orders usually agree, but relying
    on that is the same unchecked assumption this function exists to remove.
    """
    names = _export_master_names(export_dir)
    if not names:
        return {}
    slot_of = {n.lower(): i for i, n in enumerate(names)}

    root = os.path.dirname(os.path.normpath(export_dir))
    out = {}
    for slot, name in enumerate(names):
        mdir = os.path.join(root, name)
        if not os.path.isdir(mdir):
            print(f"  WARNING: master export not found ({mdir}); "
                  f"overrides cannot be diffed against it")
            continue
        # Index byte -> this plugin's index byte, for everything this master
        # can name: its own records, plus each of ITS masters by name.
        own = _export_master_names(mdir)
        remap = {len(own): slot}
        for k, sub in enumerate(own):
            target = slot_of.get(sub.lower())
            if target is not None:
                remap[k] = target
        for rec in parse_export_directory(mdir):
            fid = rec.get('FormID')
            if not fid:
                continue
            try:
                raw = int(fid, 16)
            except ValueError:
                continue
            mapped = remap.get((raw >> 24) & 0xFF)
            if mapped is None:
                # Names a file this plugin does not load: unreachable from here.
                continue
            out['%08X' % ((mapped << 24) | (raw & 0x00FFFFFF))] = rec
    return out


# A converted record may legitimately carry a signature the plugin's source
# type does not name. A REFR that places a LEVELLED CREATURE (LVLC) becomes an
# ACHR aimed at a generated shell NPC_ (see leveled_actors), so the master's
# converted record for a source REFR can be either. Keyed by source signature.
_ALSO_ACCEPTED = {
    'REFR': (b'ACHR',),
}


def _expected_output_sig(tes4_sig: str) -> bytes:
    """The TES5 signature a TES4 record converts to, or b'' if unknown.

    Several types are RENAMED by conversion (CREA->NPC_, CLOT->ARMO, ...), so
    the master's record legitimately carries a different signature than the
    plugin's source; TYPE_MAP is the single definition of those renames and is
    reused here rather than restated.
    """
    if not tes4_sig:
        return b''
    from .constants import TYPE_MAP
    return TYPE_MAP.get(tes4_sig, tes4_sig).encode('ascii', 'replace')


def _signature_mismatch(tes4_sig: str, base_sig: bytes) -> bool:
    """True when the master's record is the WRONG record to override.

    See the call site: a source id can land on an unrelated master record, and
    adopting it ships one record type's body under another's signature.
    """
    want = _expected_output_sig(tes4_sig)
    if not want or base_sig == want:
        return False
    return base_sig not in _ALSO_ACCEPTED.get(tes4_sig, ())


def make_deleted_record(base: bytes) -> bytes:
    """The master's record restated as a DELETED override.

    Shape verified by census against the vanilla record-deleting masters
    (Update/Dawnguard/HearthFires/Dragonborn, 612 deleted records):

      * a deleted REFR/ACHR keeps ONLY its NAME subrecord — 532 of the 612 are
        exactly `NAME(4) = base object`, dataSize 10. It is not a bare header;
        the engine still wants to know what the reference was.
      * everything else (NAVM and the handful of deleted STAT/NPC_/SPEL/IDLE)
        carries an EMPTY body, dataSize 0.

    In both cases the Deleted flag is set and every other subrecord is
    dropped. Keeping the master's full body with only the flag added is NOT
    what vanilla does and leaves the engine loading a record it is being told
    to remove.
    """
    if len(base) < _HEADER_SIZE:
        return base
    flags = struct.unpack_from('<I', base, 8)[0] | DELETED_FLAG
    # Preserve NAME for reference records, matching vanilla's shape.
    body = b''
    if base[:4] in (b'REFR', b'ACHR', b'ACRE'):
        for sig, payload in split_subrecords(base):
            if sig == b'NAME':
                body = sig + struct.pack('<H', len(payload)) + payload
                break
    return (base[:4] + struct.pack('<II', len(body), flags)
            + base[12:_HEADER_SIZE] + body)


def master_output_formid(src_fid: str, master_manifest) -> int:
    """The master's converted FormID for one of its source records.

    The manifest is authoritative — it was recorded when the record was
    created. Records the master emits as pre-built GRUP bytes (CELL/WRLD/REFR/
    ACHR/ACRE) never pass through the conversion loop and so have no manifest
    entry, but they also never change FormID: their id is the source id with
    the load-order index shifted, exactly like any reference to them. Falling
    back to that shift is arithmetic, not a guess, and the caller verifies it
    against the master index — a wrong id yields no record, and the override is
    dropped rather than emitted wrong.
    """
    if master_manifest is not None:
        fid = master_manifest.output_formid(src_fid)
        if fid:
            return fid
    try:
        return remap_formid(int(src_fid, 16), is_own_id=True)
    except (ValueError, TypeError):
        return 0


def detect_injected_records(all_records: list, master_export: dict,
                            num_tes4_masters: int,
                            writer: PluginWriter) -> dict:
    """{raw TES4 FormID -> our-space FormID} for INJECTED records.

    Oblivion let a plugin ADD a record carrying a MASTER's load-order index.
    Remapping those like an override puts them in the converted master's range
    at ids it does not define, so the engine resolves against the master, finds
    nothing, and hangs on load. They are new records — the caller redirects
    them into our own space (text_reader.set_injected_formids) before anything
    converts.

    Authority is the master's EXPORT (its raw TES4 FormIDs), never its
    converted output: conversion re-keys DIAL/INFO and skips whole types, so
    judging by the output called 1693 records injected instead of the real 3.
    """
    injected = {}
    if not master_export:
        return injected
    for rec in all_records:
        fid_raw = int(rec.get('FormID', '0'), 16)
        if not fid_raw or (fid_raw >> 24) & 0xFF >= num_tes4_masters:
            continue          # our own record, not master-indexed
        if (rec.get('FormID') or '').upper() in master_export:
            continue          # a real override of a master record
        injected[fid_raw] = writer.alloc_formid()
    return injected


class OverrideContext:
    """Everything a plugin's import needs to emit overrides of its masters."""

    def __init__(self, export_dir: str, masters: list, num_tes4_masters: int,
                 output_root: str):
        # Kept so consumers can re-resolve the MASTERS' export directories the
        # same way `load_master_export` does (voice-type adoption reads their
        # RACE.txt — see `_master_export_dirs` in import_main).
        self.export_dir = export_dir
        self.master_index = load_master_index(
            masters, num_tes4_masters, output_root)
        # export_root lets the loader re-key each manifest out of its master's
        # own TES4 id space into the one THIS plugin names it by.
        self.master_manifest = load_master_manifests(
            masters, num_tes4_masters, output_root,
            export_root=os.path.dirname(os.path.normpath(export_dir)))
        self.master_export = load_master_export(export_dir)
        self.stats = Counter()
        self.unmapped_keys = Counter()
        # WRLD overrides this plugin emitted, {out FormID -> record bytes}.
        # A plugin that adds cells to a MASTER's worldspace needs the WRLD
        # record in front of their type-1 group, and it must be THIS record
        # rather than the master's — emitting the master's copy as well would
        # ship the same FormID twice and the engine keeps the LAST one, which
        # silently reverted the author's own worldspace edits (Tamriel.esp
        # widens NAM0/NAM9 to hold the land it adds; the stale duplicate put
        # the vanilla bounds back and clipped the new terrain off the map).
        self.emitted_wrld = {}
        # Records emit_nested_overrides pulled from the master as an anchor.
        # The WRLD builder runs afterwards and must not anchor the same record
        # again — that ships the FormID twice and the engine drops one copy.
        self.anchored_wrld = set()

    def __len__(self):
        return len(self.master_export)

    def master_record(self, rec: dict):
        """The master's EXPORT record this plugin record overrides, or None."""
        src_fid = (rec.get('FormID') or '').upper()
        return self.master_export.get(src_fid)

    def build(self, rec: dict, sig: str = None):
        """Build the override for one plugin record.

        Returns None when the record is NOT an override (a new record — the
        caller converts it normally), else an Override whose `record_bytes` is
        set only for status 'emitted':

          emitted    the master's bytes with the author's changes applied
          unchanged  authorially identical to the master — pure bloat, drop
          no-base    the master's conversion has no record to override
          no-path    the type's conversion output has no record to patch
        """
        master_rec = self.master_record(rec)
        if master_rec is None:
            return None

        src_fid = (rec.get('FormID') or '').upper()
        if sig in OVERRIDE_UNMAPPABLE_TYPES:
            self.stats['no-path'] += 1
            return Override('no-path', 0, b'')

        out_fid = master_output_formid(src_fid, self.master_manifest)
        base = self.master_index.record(out_fid) if out_fid else b''
        if not base:
            # The master's conversion dropped this record, so there is nothing
            # to override. Emitting it would leave a record the engine cannot
            # resolve against the master.
            self.stats['no-base'] += 1
            return Override('no-base', out_fid, b'')

        # The id must resolve to a record of the SAME TYPE. A plugin's source
        # id can land on an unrelated master record — Elsweyr Anequina's NPC_
        # 0100110C converts to 0200110C, which in Oblivion.esm's own space is
        # a REFR — and adopting that record's bytes and nesting shipped the
        # NPC_ as a "REFR" inside a bogus top-level GRUP (xEdit: "File contains
        # top level group without known sort order: GRUP Top 'REFR'"). Treat a
        # type mismatch as "no master record", so the caller converts it as the
        # new record it actually is.
        if _signature_mismatch(sig or rec.get('Signature') or '', base[:4]):
            self.stats['no-base'] += 1
            return Override('no-base', 0, b'')

        if int(rec.get('RecordFlags') or 0) & DELETED_FLAG:
            # The author DELETED this record. Deletion is expressed by the
            # header flag, not by the field diff — the plugin's record is an
            # empty stub, so diffing it against the master reports every
            # subrecord the master has as an unmappable "change" and the
            # record ships alive. Emit the master's header with the flag set
            # and NO body, which is exactly what the deleting plugin itself
            # ships and what the engine reads as "remove this reference".
            self.stats['deleted'] += 1
            return Override('deleted', out_fid,
                            make_deleted_record(base))

        changes = diff_records(master_rec, rec)
        if not changes:
            # An override that changes nothing is pure bloat.
            self.stats['unchanged'] += 1
            return Override('unchanged', out_fid, b'')

        if any((sig or rec.get('Signature'), key) in RECONVERT_KEYS
               for key in changes):
            # The authored change rewrites content whose conversion mints
            # companion records (spell effect lists -> aimed-MGEF clones).
            # That cannot be spliced into the master's bytes, so the caller
            # reconverts the record from the plugin's export instead — its
            # FormID still lands on the master's, keeping it an override.
            self.stats['reconverted'] += 1
            return Override('reconvert', out_fid, b'')

        record_bytes, _applied, unmapped = apply_changes(
            base, changes, rec, master_rec)
        for key in unmapped:
            self.unmapped_keys[key] += 1
        self.stats['emitted'] += 1
        return Override('emitted', out_fid, record_bytes)

    def build_soun_companion(self, rec: dict, writer) -> bytes:
        """Override of the master's SNDR when a SOUN's volume/falloff changed.

        Returns b'' when nothing sound-related was authored (the master's SNDR
        already says the right thing) or when the master's companion cannot be
        located. The SNDR keeps the MASTER's FormID, so every SOUN already
        pointing at it — including this override's own SDSC — still resolves.
        """
        master_rec = self.master_record(rec)
        if master_rec is None:
            return b''
        changes = diff_records(master_rec, rec)
        if not soun_companion_changes(changes):
            return b''
        src_fid = (rec.get('FormID') or '').upper()
        for fid in self.master_manifest.companions(src_fid):
            base = self.master_index.record(fid)
            if base[:4] == b'SNDR':
                out = rebuild_sndr_override(base, rec, writer)
                if out:
                    self.stats['soun-companion'] += 1
                    for key in changes:
                        self.unmapped_keys.pop(key, None)
                return out
        return b''

    def report(self):
        print(f"  Overrides: {self.stats['emitted']} emitted, "
              f"{self.stats['deleted']} deleted by the author, "
              f"{self.stats['reconverted']} reconverted (effect-list change), "
              f"{self.stats['unchanged']} unchanged (dropped), "
              f"{self.stats['no-base']} without a converted master record, "
              f"{self.stats['no-path']} inexpressible (PGRD/ROAD)")
        if self.unmapped_keys:
            print(f"  NOTE: {sum(self.unmapped_keys.values())} authored "
                  f"changes in {len(self.unmapped_keys)} field(s) have no "
                  f"output mapping and kept the master's value:")
            for key, count in self.unmapped_keys.most_common(10):
                print(f"    {key}: {count}")


# GRUP types whose label is the FormID of the record that OWNS the group, and
# which the engine binds to that record ONLY by physical adjacency: xEdit's
# TwbGroupRecord.InformPrevMainRecord (wbImplementation.pas ~18023) attaches
# the group to the previous record iff
#     grsGroupType in [1, 6, 7] and aPrevMainRecord.FixedFormID = GroupLabel
# 1 = world children (under WRLD), 6 = cell children (under CELL),
# 7 = topic children (under DIAL). A group of one of these types that is NOT
# immediately preceded by its owning record is attached to nothing, so every
# record inside it is unreachable — as invisible as if it were never written.
OWNED_GROUP_TYPES = frozenset({1, 6, 7})


def _group_sort_key(step: tuple):
    """Ordering key for one GRUP nesting step, matching vanilla's file order.

    A worldspace's children group holds the PERSISTENT cell (its own type-6
    group, labelled by that cell's FormID) followed by the exterior blocks
    (type 4). Vanilla puts the persistent cell FIRST: in the converted
    Oblivion.esm, Tamriel's type-1 group opens with CELL 01023777 and its
    type-6 group, and only then the 21 type-4 blocks.

    Sorting by (group type, label bytes) put it LAST instead, because 6 > 4.
    The engine walks a worldspace's block tree to build the cell grid and
    reaches the persistent cell through that walk; finding an unexpected
    type-6 group after the blocks left TWMP_ValenwoodImproved hanging on the
    main menu, and deleting the exterior blocks in xEdit made it load.

    Within the blocks, order by the UNSIGNED 16-bit halves of the label with
    **X major, Y minor**. The label packs Y into the LOW word, so sorting on
    its own word order yields the TRANSPOSE and lets X descend mid-list; the
    engine's parse-time grid walk never terminates on such a run. See
    import_main._grid_sort_key for the Skyrim.esm census and the symptom.
    """
    gtype, label = step[0], bytes(step[1])
    if gtype in OWNED_GROUP_TYPES:
        # Persistent cell / topic groups lead, in FormID order.
        return (0, gtype, 0, 0, label)
    if gtype in (4, 5) and len(label) == 4:
        y, x = struct.unpack('<HH', label)
        return (1, gtype, x, y, b'')
    return (1, gtype, 0, 0, label)


def emit_nested_overrides(records: list, writer: PluginWriter,
                          master_index=None, anchored_fids: set = None) -> tuple:
    """Write override records back into the master's exact GRUP nesting.

    A CELL is only reachable by the engine from inside its block/sub-block
    hierarchy (interior: CELL -> type 2 -> type 3; exterior: WRLD -> type 1 ->
    type 4 -> type 5). Writing one flat under the top-level group leaves it
    unindexed, which is what made every renamed cell black and empty in-game.
    A REFR likewise lives under its parent cell's type-6 children group.

    The nesting is COPIED from the master (MasterIndex.group_path) rather than
    recomputed, so there is no block-number formula to get wrong, and a record
    the master nests unusually still lands where the engine expects it.

    Every OWNED_GROUP_TYPES group must be preceded by its owning record. When
    this plugin does not override that owner (it changed a worldspace's cells
    but not the WRLD record, or a cell's references but not the CELL record),
    the owner's converted bytes are pulled from `master_index` VERBATIM as an
    anchor — the same thing xEdit's copy-as-override does. Without the anchor
    the group stands alone and the engine indexes none of its contents: a
    Tamriel type-1 group with no WRLD record in front of it silently discarded
    all 473 of DLCBattlehornCastle's exterior overrides.

    `records` is [(output_formid, record_bytes, path), ...] where `path` is
    the ((grup_type, label_bytes), ...) nesting the record belongs in.
    Returns (emitted_count, orphan_count, anchored_count).
    """
    # Bucket the records by the exact GRUP path they belong in.
    by_path = {}
    orphans = 0
    for _out_fid, record_bytes, path in records:
        if not path:
            # No nesting known: emitting it flat would leave it unindexed, so
            # skip rather than ship an invisible record.
            orphans += 1
            continue
        by_path.setdefault(path, []).append(record_bytes)

    # LAND FIRST in every temporary children group. Census of real Skyrim.esm:
    # 15,564 of 15,564 type-9 groups that contain a LAND have it at index 0.
    # Emitted after the references instead, the engine does not draw the
    # terrain at all — Tamriel (-7,-32) had LAND at index 150 behind 150 REFRs
    # and rendered as a hole with its clutter still floating in place.
    # Stable: only the LAND moves, everything else keeps its relative order.
    for path, bodies in by_path.items():
        if path[-1][0] == 9 and any(b[:4] == b'LAND' for b in bodies):
            bodies.sort(key=lambda b: b[:4] != b'LAND')

    # A record already being emitted at `prefix` serves as its own anchor.
    emitted_at = {}
    for path, bodies in by_path.items():
        for body in bodies:
            emitted_at.setdefault(path, set()).add(
                struct.unpack_from('<I', body, 12)[0])

    # A cell's children GRUP REPLACES the master's rather than merging, so any
    # children group we emit must carry the master's LAND when this plugin is
    # not shipping one of its own — otherwise overriding a single REFR deletes
    # the terrain under it. The land is dropped precisely BECAUSE it is
    # unchanged (diff_records correctly reports no authored difference: the
    # VHGT bytes match once the uninitialised 3-byte pad is normalised), so
    # nothing upstream knows the cell still needs it.
    #
    # Measured on ElsweyrAnequina.esp: 8 cells emitted a type-9 group holding
    # one REFR or ACHR and no LAND, and in-game those cells rendered as blank
    # missing ground with the placed references still floating on it.
    _land_of = getattr(master_index, 'land', None)
    if callable(_land_of):
        for path in list(by_path):
            if not path or path[-1][0] != 9 or len(path[-1][1]) != 4:
                continue
            cell_fid = struct.unpack('<I', path[-1][1])[0]
            if any(b[:4] == b'LAND' for b in by_path[path]):
                continue
            land_fid = _land_of(cell_fid)
            if not land_fid:
                continue
            land_rec = master_index.record(land_fid)
            if land_rec:
                # LAND goes FIRST in a temporary children group, matching
                # vanilla and the normal builders; appended after the
                # references the engine does not draw it at all.
                by_path[path].insert(0, land_rec)
                emitted_at.setdefault(path, set()).add(land_fid)

    anchored = [0]
    # Every record this pass pulls in from the master as an anchor. The WRLD
    # builder runs AFTER this one and would otherwise anchor the same
    # worldspace a second time, shipping the FormID twice (xEdit: "Skipped
    # Load: Duplicate FormID [0100003C]") — ElsweyrAnequina wrote Tamriel's
    # WRLD twice for exactly that reason.
    if anchored_fids is None:
        anchored_fids = set()

    def anchor_for(path: tuple, fid: int) -> bytes:
        """The owning record's bytes, pulled from the master if we lack it."""
        if fid in emitted_at.get(path, ()):
            return b''          # already emitted at this level
        if master_index is None:
            return b''
        rec = master_index.record(fid)
        if rec:
            anchored[0] += 1
            anchored_fids.add(fid)
        return rec

    def build(prefix: tuple, depth: int) -> bytes:
        """Serialize everything at `prefix`, recursing into deeper paths.

        A record's own children group must directly FOLLOW that record — the
        engine reads `CELL, GRUP(6, cell), CELL, GRUP(6, cell), ...`, so
        emitting the records first and the groups afterwards pairs each group
        with the wrong cell.
        """
        deeper = {p[:depth + 1] for p in by_path
                  if len(p) > depth and p[:depth] == prefix}
        # An owned group belongs to the record whose FormID labels it.
        owned = {struct.unpack('<I', child[depth][1])[0]: child
                 for child in deeper
                 if child[depth][0] in OWNED_GROUP_TYPES
                 and len(child[depth][1]) == 4}
        body = b''
        for record_bytes in by_path.get(prefix, ()):
            body += record_bytes
            fid = struct.unpack_from('<I', record_bytes, 12)[0]
            child = owned.pop(fid, None)
            if child is not None:
                inner = build(child, depth + 1)
                if inner:
                    body += pack_group(child[depth][0], child[depth][1], inner)

        # Everything else: blocks/sub-blocks, plus any owned group whose owning
        # record this plugin does not override (its parent cell/worldspace is
        # unchanged, but some of its contents are). Those need the owner pulled
        # in from the master as an anchor, immediately before the group.
        rest = [c for c in deeper
                if c[depth][0] not in OWNED_GROUP_TYPES or c in owned.values()]
        for child in sorted(rest, key=lambda p: _group_sort_key(p[depth])):
            inner = build(child, depth + 1)
            if not inner:
                continue
            gtype, label = child[depth][0], child[depth][1]
            if gtype in OWNED_GROUP_TYPES and len(label) == 4:
                body += anchor_for(prefix, struct.unpack('<I', label)[0])
            body += pack_group(gtype, label, inner)
        return body

    # Depth 0 is the top-level group itself; writer.add_raw_group wraps it.
    for top in sorted({p[0] for p in by_path},
                      key=lambda t: (t[0], bytes(t[1]))):
        body = build((top,), 1)
        if body:
            writer.add_raw_group(top[1].decode('ascii', 'replace'), body)

    return len(records) - orphans, orphans, anchored[0]


def build_nested_overrides(by_type: dict, sigs: tuple, ctx: OverrideContext,
                           writer: PluginWriter, label: str) -> list:
    """Emit overrides for record types that live inside GRUP hierarchies.

    Used for CELL/WRLD/REFR/ACHR/ACRE/LAND (the cell tree) and DIAL/INFO (the
    topic tree). The shared reasoning: a record's child GRUP REPLACES the
    master's — it is not merged. A plugin that rebuilds the hierarchy therefore
    deletes every child the master put there: renaming 571 cells emptied them
    all (black, contentless interiors in-game), and a rebuilt DIAL would drop
    the master's INFO list. So each override goes out as a flat record carrying
    only the author's changes, placed in the master's exact nesting, with no
    children of its own.

    Ship ONLY the records this plugin changes — never the master's whole child
    list. Verified against BS_DLC_patch.esp: 54 cell overrides, ~5 REFRs each
    (278 total). The master's other references stay visible because ONAM (built
    by the writer at save time) tells the engine to keep loading a cell's
    temporary children on demand even though the cell record is overridden.

    Returns the records that are NEW to this plugin and sit in its OWN
    hierarchy rather than the master's. A master-dependent plugin can carry a
    whole world of its own (Morroblivion lists Oblivion.esm as a master but
    every one of its 387,813 world records is new), and those must be handed
    back to the normal group builders — the override path cannot express them.
    """
    pending = []
    new_records = []
    dropped = 0
    for sig in sigs:
        for rec in by_type.get(sig, []):
            ov = ctx.build(rec, sig)
            if ov is None:
                new_records.append((sig, rec))
                continue
            # 'deleted' carries real bytes (the author's deletion) and must be
            # shipped in the master's nesting exactly like an edit — dropping
            # it would leave the master's reference alive in-game.
            if ov.status not in ('emitted', 'deleted'):
                dropped += 1
                continue
            if sig == 'WRLD':
                ctx.emitted_wrld[ov.out_fid] = ov.record_bytes
            pending.append((ov.out_fid, ov.record_bytes,
                            ctx.master_index.group_path(ov.out_fid)))

    new_done, unattached = _attach_new_records(new_records, ctx, pending)

    # A bark INFO handed back above needs its parent DIAL in the same batch or
    # the normal builder has no topic to group it under. That parent is the
    # MASTER's shared GREETING/HELLO record, which this loop just counted as an
    # unchanged override and dropped — so re-add this plugin's own copy of it.
    # The builder re-keys it into per-quest topics of our own; the master's
    # record is left untouched (we ship no override for it).
    _bark_parents = {(r.get('ParentDIAL') or '').upper()
                     for s, r in unattached if s == 'INFO'}
    if _bark_parents:
        seen = {(r.get('FormID') or '').upper()
                for s, r in unattached if s == 'DIAL'}
        for rec in by_type.get('DIAL', []):
            fid = (rec.get('FormID') or '').upper()
            if fid in _bark_parents and fid not in seen:
                unattached.append(('DIAL', rec))
                dropped -= 1

    emitted, orphaned, anchored = emit_nested_overrides(
        pending, writer, ctx.master_index, ctx.anchored_wrld)
    msg = (f"  {label} overrides: {emitted} emitted in the master's "
           f"group nesting, {dropped} unchanged")
    if anchored:
        msg += (f", {anchored} unchanged parent record(s) pulled from the "
                f"master to anchor their children group")
    if new_done:
        msg += f", {new_done} NEW records nested under master parents"
    if unattached:
        msg += (f", {len(unattached)} NEW records in this plugin's OWN "
                f"hierarchy (built by the normal builders)")
    if orphaned:
        msg += f", {orphaned} SKIPPED (no master nesting)"
    print(msg)
    return unattached


# New (non-override) records inside GRUP trees: which export key names the
# parent, and how the child group chain under the parent is built.
_NEW_NESTED_PARENT = {
    'REFR': 'ParentCELL',
    'ACHR': 'ParentCELL',
    'ACRE': 'ParentCELL',
    'INFO': 'ParentDIAL',
}


def _is_bark_parent(rec: dict, ctx: OverrideContext) -> bool:
    """True when this new INFO's parent DIAL is a MASTER-owned BARK topic.

    GREETING/HELLO/Attack/Idle and friends are engine-named topics shared by
    every plugin, so a dependent plugin's own bark lines all point at the
    master's copy. They must go through the normal bark builder (which splits
    them into per-quest topics and gates them) rather than being nested under
    the master's topic — see the comment at the call site.

    Read from the MASTER export, since the parent record is the master's.
    """
    from .dialog_converter import classify_topic
    from .text_reader import get_int
    parent_src = (rec.get('ParentDIAL') or '').upper()
    if not parent_src:
        return False
    parent = (ctx.master_export or {}).get(parent_src)
    if parent is None:
        return False
    _cat, _sub, _snam, is_bark = classify_topic(
        parent.get('EditorID') or '', get_int(parent, 'DATA.Type'))
    return is_bark


def _attach_new_records(new_records: list, ctx: OverrideContext,
                        pending: list) -> tuple:
    """Convert NEW records that live inside a MASTER's GRUP tree.

    A plugin can add its own references to a master's cell (Translation.esp
    injects a map-marker REFR) or its own INFO to a master's topic. They are
    new records — converted normally — but they must sit under the master
    parent's children group or the engine never indexes them. Anchoring that
    group (pulling the unchanged parent's bytes in VERBATIM when this plugin
    does not override it) is handled generically by emit_nested_overrides for
    every type-1/6/7 group, so this function only has to place the record at
    the right path.

    Returns (attached_count, unattached) where `unattached` is every record
    whose parent is NOT the master's — those belong to this plugin's own
    hierarchy and must be built by the normal group builders instead.
    """
    from .record_types.world import convert_ACHR, convert_REFR
    from .text_reader import get_formid, get_int

    done = 0
    unattached = []
    for sig, rec in new_records:
        parent_key = _NEW_NESTED_PARENT.get(sig)
        parent_src = (rec.get(parent_key) or '') if parent_key else ''
        # A NEW INFO under a master BARK topic must NOT be nested here.
        # GREETING/HELLO are shared, engine-named topics every plugin fills, so
        # every one of this plugin's own greetings resolves to the master's
        # single GREETING DIAL — and nesting them there:
        #   * bypasses the dialogue pipeline entirely (the bare convert_INFO
        #     below emits no GetIsVoiceType, no quest gate, no unlock gate:
        #     0 of Morroblivion's 2,727 greetings had a voice gate, against
        #     15,574 everywhere else), and
        #   * defeats the one-bark-topic-per-quest split. Skyrim honours ONE
        #     HELO topic per owning quest, and the master's GREETING is owned
        #     by the MASTER's quest, so 2,727 greetings spanning 384 different
        #     quests all landed under a topic gated on Oblivion's Charactergen
        #     — dead for the whole game. Vanilla splits GREETING into 271
        #     per-quest topics for exactly this reason.
        # Sending them back as `unattached` makes the normal builder emit this
        # plugin's OWN per-quest bark topics, fully gated.
        if sig == 'INFO' and _is_bark_parent(rec, ctx):
            unattached.append((sig, rec))
            continue
        parent_out = (master_output_formid(parent_src.upper(),
                                           ctx.master_manifest)
                      if parent_key else 0)
        parent_path = (ctx.master_index.group_path(parent_out)
                       if parent_out else ())
        if not parent_key or not parent_path:
            # NOT an error: the record's parent is this plugin's OWN, not the
            # master's (Morroblivion declares Oblivion.esm as a master but its
            # entire world — 387,813 CELL/REFR/LAND/PGRD records — is new).
            # These belong to the normal group builders; dropping them here
            # deleted every cell and reference in the game world.
            unattached.append((sig, rec))
            continue

        try:
            if sig == 'INFO':
                from .dialog_converter import convert_INFO
                record_bytes = convert_INFO(rec)
                chain = ((7, struct.pack('<I', parent_out)),)
            else:
                conv = convert_ACHR if sig in ('ACHR', 'ACRE') else convert_REFR
                record_bytes = conv(rec)
                # Persistent refs (flag 0x400) sit in the type-8 children
                # group, temporary ones in type 9 — mirroring the master's
                # own builders.
                gtype = 8 if get_int(rec, 'RecordFlags') & 0x400 else 9
                label = struct.pack('<I', parent_out)
                chain = ((6, label), (gtype, label))
        except Exception as e:
            print(f"    SKIPPED new {sig} {rec.get('FormID', '?')}: "
                  f"conversion failed: {e}")
            continue

        # No anchor is added here: emit_nested_overrides pulls the owner of any
        # type-1/6/7 group from the master when this plugin does not override
        # it, so the parent CELL/DIAL is anchored by the same generic path that
        # anchors an unchanged WRLD above a worldspace's cells.
        new_fid = get_formid(rec, 'FormID')
        pending.append((new_fid, record_bytes, parent_path + chain))
        done += 1
    return done, unattached
