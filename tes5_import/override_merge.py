"""Random-access index over a converted master plugin.

A plugin that has masters emits each override as the MASTER's converted record
with the author's changes applied (see override_builder). This module provides
the master side of that: the converted record bytes for a given FormID.

There is deliberately no merging heuristic here. An earlier version tried to
reconcile two independent conversion runs by comparing their output bytes and
guessing which differences were authored and which were re-derivation
artifacts. That is not decidable from the output, and guessing wrong produced
an unloadable plugin — 1821 NPC_ races rewritten to vanilla Skyrim ones the
authors never touched. Authorship now comes from diffing the two TES4 exports
(export_diff), which answers the question directly.
"""

import os
import struct
import zlib

_HEADER_SIZE = 24


def _read_masters(data: bytes, hdr_size: int) -> list:
    """The MAST names in a plugin's TES4 header, in load order.

    Their COUNT is also the index byte this file's own records carry, since a
    file's records sit immediately after its masters in load order.
    """
    names = []
    i = _HEADER_SIZE
    end = _HEADER_SIZE + hdr_size
    while i + 6 <= end and i + 6 <= len(data):
        sig = data[i:i + 4]
        size = struct.unpack_from('<H', data, i + 4)[0]
        if sig == b'MAST':
            names.append(data[i + 6:i + 6 + size].rstrip(b'\0')
                         .decode('latin1'))
        i += 6 + size
    return names


class MasterIndex:
    """FormID -> converted record body, read from a converted master plugin."""

    def __init__(self, path: str):
        self.path = path
        self._data = b''
        self._offsets = {}      # formid -> (signature, offset, total_size)
        self._paths = {}        # formid -> ((grup_type, label), ...)
        self._land_by_cell = {}  # cell formid -> LAND formid
        # This file's own master list, and the index byte its OWN records carry
        # (= that list's length). Both are needed to translate between this
        # master's id space and a child's — see ChainedMasterIndex.
        self.masters = []
        self.own_index = 0
        self._load()

    def _load(self):
        with open(self.path, 'rb') as f:
            self._data = f.read()
        d = self._data
        if len(d) < 8 or d[:4] != b'TES4':
            raise ValueError(f"Not a plugin file: {self.path}")
        hdr_size = struct.unpack_from('<I', d, 4)[0]
        self.masters = _read_masters(d, hdr_size)
        self.own_index = len(self.masters)
        start = _HEADER_SIZE + hdr_size
        self._scan(start, len(d))

    def _scan(self, off: int, end: int, path: tuple = ()):
        d = self._data
        while off + _HEADER_SIZE <= end:
            sig = d[off:off + 4]
            size = struct.unpack_from('<I', d, off + 4)[0]
            if sig == b'GRUP':
                # GRUP header: 'GRUP'(4) size(4) label(4) type(4) ...
                label = d[off + 8:off + 12]
                gtype = struct.unpack_from('<i', d, off + 12)[0]
                self._scan(off + _HEADER_SIZE, off + size,
                           path + ((gtype, label),))
                off += size
            else:
                fid = struct.unpack_from('<I', d, off + 12)[0]
                self._offsets[fid] = (sig, off, _HEADER_SIZE + size)
                self._paths[fid] = path
                if sig == b'LAND':
                    # A cell has at most one LAND, and the type-6 GRUP label
                    # names the owning cell. Keyed here because a LAND's own
                    # FormID is NOT recoverable by arithmetic — see land().
                    for gtype, label in reversed(path):
                        if gtype == 6 and len(label) == 4:
                            self._land_by_cell[
                                struct.unpack_from('<I', label)[0]] = fid
                            break
                off += _HEADER_SIZE + size

    def group_path(self, formid: int) -> tuple:
        """The GRUP nesting a record sits in, as ((type, label), ...).

        A CELL is only reachable by the engine from inside its block/sub-block
        hierarchy (interior: CELL -> type 2 -> type 3; exterior: WRLD -> type 1
        -> type 4 -> type 5). A CELL written flat under the top-level group is
        never indexed, which is what left every renamed cell black and empty.
        An override must therefore reproduce the master's exact nesting — read
        here rather than recomputed, so there is no formula to get wrong.
        """
        return self._paths.get(formid, ())

    def land(self, cell_formid: int) -> int:
        """FormID of the LAND inside a converted cell, or 0 if it has none.

        LAND is the one override type whose own FormID cannot be derived from
        its source id. Every other record either has a manifest entry or keeps
        its id with only the load-order index shifted, but the master's
        conversion REALLOCATES land ids (Oblivion.esm: 0 of 3,999 sampled
        source ids resolve to a real output LAND by that arithmetic — the
        0x00034A7D that owns cell 0x000082F3 comes out as 0x0100F8AF).

        The lookup that does hold is structural: a cell owns at most one LAND,
        and the cell's own id DOES resolve. Without this every LAND override
        from a dependent plugin was classified 'no-base' and dropped, and
        because a plugin's child GRUP REPLACES the master's rather than
        merging with it, the emitted cell then shadowed the master's terrain
        with a children group that had no LAND in it — land blank and missing
        in-game while the cell's placed references still rendered.
        """
        return self._land_by_cell.get(cell_formid, 0)

    def __contains__(self, formid: int) -> bool:
        return formid in self._offsets

    def __len__(self) -> int:
        return len(self._offsets)

    def formids(self) -> set:
        return set(self._offsets)

    def signature(self, formid: int) -> bytes:
        entry = self._offsets.get(formid)
        return entry[0] if entry else b''

    def record(self, formid: int) -> bytes:
        """Full record bytes (header + body) for a FormID, or b'' if absent."""
        entry = self._offsets.get(formid)
        if not entry:
            return b''
        _, off, size = entry
        return self._data[off:off + size]

    def find_by_edid(self, signature: bytes, edid: str) -> int:
        """FormID of the master's record with this signature + EditorID (0 if none).

        For SYNTHETIC records the master minted with no TES4 source, so the
        companion manifest (keyed by source FormID) cannot name them — the
        generic dialogue quest is the case that matters. EDID is always the
        first subrecord of the records this is used for, so this stops at the
        first one rather than parsing the whole body.
        """
        want = edid.encode('ascii', 'replace')
        for fid, (sig, off, size) in self._offsets.items():
            if sig != signature:
                continue
            # Compressed bodies start with a u32 decompressed size, not EDID.
            if struct.unpack_from('<I', self._data, off + 8)[0] & 0x00040000:
                continue
            body = self._data[off + _HEADER_SIZE:off + size]
            if len(body) < 6 or body[:4] != b'EDID':
                continue
            ln = struct.unpack_from('<H', body, 4)[0]
            if body[6:6 + ln].rstrip(b'\0') == want:
                return fid
        return 0


# GRUP types whose 4-byte label is a FormID (the owning record), not a
# block/sub-block coordinate pair. xEdit wbImplementation: 1=World Children,
# 6=Cell Children, 7=Topic Children, 8/9/10=the cell's persistent/temporary/
# visible-when-distant child groups (all labelled with the parent CELL).
_FORMID_LABEL_GROUPS = frozenset({1, 6, 7, 8, 9, 10})

# Subrecords in the cell tree whose payload is one or more FormIDs, with the
# byte offsets that hold them. Verified against the xEdit TES5 definitions and
# a census of the converted output; only these are rewritten, so a field like
# LAND's VHGT/VNML (raw terrain bytes that can look like anything) is never
# touched. `None` means "the whole payload is a run of u32 FormIDs".
_FORMID_FIELDS = {
    # Whole payload is one FormID (or a run of them).
    b'NAME': None,    # base object / linked record
    b'XLCN': None,    # persistent location
    b'XOWN': None,    # owner (our writer emits a bare FormID)
    b'XCLR': None,    # wbArrayS(XCLR, 'Regions', ...) — a RUN of FormIDs
    b'LTMP': None,    # lighting template
    b'XLTW': None,    # lit water
    b'XEZN': None,    # encounter zone
    b'XCWT': None,    # cell water
    b'XCIM': None,    # image space
    b'XCAS': None,    # acoustic space
    b'XCMO': None,    # music type
    b'XLIB': None,    # leveled item base
    b'XATR': None,    # attach ref
    b'XEMI': None,    # emittance
    b'XMBR': None,    # multibound ref
    b'XLRT': None,    # location ref type
    b'XLRL': None,    # location reference
    # Structs: only these byte offsets hold a FormID.
    b'XESP': (0,),    # parent ref + 4 flag bytes
    b'XNDP': (0,),    # navmesh ref + u16 index + pad
    b'XTEL': (0,),    # door ref + 6 floats + flags
    b'XLOC': (4,),    # u32 level, key FormID, flags
    b'XPWR': (0,),    # wbStructSK(XPWR, [0], 'Water', ...)
    b'XLKR': (0,),    # linked-ref keyword + ref
    b'BTXT': (0,),    # LTEX + quadrant/layer
    b'ATXT': (0,),    # LTEX + quadrant/layer
    # DELIBERATELY ABSENT — verified against wbDefinitionsTES5.pas, these are
    # NOT FormIDs and rewriting them corrupts real data:
    #   XLCM  wbInteger  (level modifier)
    #   XPRD  wbFloat    (patrol idle time)
    #   XPPA  wbEmpty    (patrol script marker)
    #   XRGD / XRGB      (ragdoll/biped data blobs)
}


def _shift_formid(fid: int, index_map: dict) -> int:
    """Restate a FormID's index byte via `index_map`, leaving 0 (null) alone.

    An index the map does not mention is left ALONE. That is the common case
    and the important one: a master and its child usually share their own low
    masters (Skyrim.esm at 0, Oblivion.esm at 1), so a reference to one of
    those is already correct and moving it would silently retarget it — a
    blanket +1 turned every Oblivion.esm reference into a Tamriel.esp one.
    """
    if not fid:
        return fid
    mapped = index_map.get((fid >> 24) & 0xFF)
    if mapped is None:
        return fid
    return (mapped << 24) | (fid & 0x00FFFFFF)


def _shift_record_formids(rec: bytes, index_map: dict) -> bytes:
    """A converted record restated in the child's load order.

    `index_map` is {master's index byte -> child's index byte}, holding ONLY
    the bytes that actually move. Rewrites the record's own FormID in the
    header plus every reference in a known FormID-bearing subrecord.
    Compressed bodies are decompressed, rewritten and left DECOMPRESSED with
    the flag cleared — the engine accepts either form, and the override builder
    needs to read the subrecords anyway.
    """
    if len(rec) < _HEADER_SIZE:
        return rec
    sig = rec[:4]
    size = struct.unpack_from('<I', rec, 4)[0]
    flags = struct.unpack_from('<I', rec, 8)[0]
    fid = struct.unpack_from('<I', rec, 12)[0]
    body = rec[_HEADER_SIZE:_HEADER_SIZE + size]
    if flags & 0x00040000:
        try:
            body = zlib.decompress(body[4:])
        except zlib.error:
            return rec
        flags &= ~0x00040000

    out = bytearray()
    j = 0
    while j + 6 <= len(body):
        ssig = body[j:j + 4]
        ssize = struct.unpack_from('<H', body, j + 4)[0]
        payload = bytearray(body[j + 6:j + 6 + ssize])
        if ssig in _FORMID_FIELDS:
            spots = _FORMID_FIELDS[ssig]
            offsets = (range(0, len(payload) - 3, 4) if spots is None
                       else spots)
            for off in offsets:
                if off + 4 <= len(payload):
                    v = struct.unpack_from('<I', payload, off)[0]
                    struct.pack_into('<I', payload, off,
                                     _shift_formid(v, index_map))
        out += ssig + struct.pack('<H', ssize) + bytes(payload)
        j += 6 + ssize
    out += body[j:]        # any trailing bytes, untouched

    head = bytearray(rec[:_HEADER_SIZE])
    struct.pack_into('<I', head, 4, len(out))
    struct.pack_into('<I', head, 8, flags)
    struct.pack_into('<I', head, 12, _shift_formid(fid, index_map))
    return bytes(head) + bytes(out)


class ChainedMasterIndex:
    """Several converted masters, addressed in the CHILD plugin's FormID space.

    Every FormID reaching this class is in the child's space, where the index
    byte names one specific master: the child's TES5 master list puts the
    master converted from `indices[k]` at slot `base_slot + k`, so an id whose
    high byte is that slot belongs to that master and to no other.

    Routing by index byte rather than by "first file that happens to contain
    the integer" is the whole point. Each converted master renumbers into its
    OWN space, so two masters' id ranges overlap almost completely, and a
    first-match scan silently answers from the wrong file. TWMP Valenwood/
    Elsweyr hit this exactly: 0202E438 is a Tamriel.esp exterior CELL and also
    an ElsweyrAnequina.esp WRLD (ANQVerkarthHillsWorld). The reverse-order scan
    returned ANQ's worldspace for the Tamriel cell, so the writer emitted a
    phantom worldspace and 4,992 duplicate FormIDs (4,552 REFR, 237 CELL, 178
    LAND) — the same id twice with conflicting types and group nesting. The
    engine builds its FormID table while parsing the plugin, before any cell
    loads, so the game hung on the main menu with no crash.

    A master's own records carry ITS index (its master count), which is not the
    slot it occupies in the child. Both directions are translated here so
    callers never have to think about whose space an id is in.
    """

    def __init__(self, indices: list, base_slot: int = None,
                 child_masters: list = None):
        self._indices = list(indices)
        # Default: masters occupy the LAST len(indices) slots of the child's
        # master list, i.e. the TES4 masters after any prepended new ones.
        # Callers that know the real layout pass it explicitly.
        self._base_slot = (base_slot if base_slot is not None
                           else len(self._indices))
        self._by_slot = {self._base_slot + k: idx
                         for k, idx in enumerate(self._indices)}
        # {index -> {its index byte -> the child's}} for the record rewriter.
        # Built from the CHILD's full master list so a name shared by both (the
        # usual Skyrim.esm/Oblivion.esm prefix) maps to itself and is left
        # alone; only indices that genuinely move appear here.
        child = [n.lower() for n in (child_masters or [])]
        self._index_maps = {}
        for k, idx in enumerate(self._indices):
            slot = self._base_slot + k
            own_masters = [n.lower() for n in (idx.masters or ())]
            m = {}
            if idx.own_index != slot:
                m[idx.own_index] = slot
            for j, sub in enumerate(own_masters):
                target = child.index(sub) if sub in child else None
                if target is not None and target != j:
                    m[j] = target
            self._index_maps[id(idx)] = m

    def _route(self, formid: int):
        """(index, id in that master's own space) for a child-space FormID."""
        idx = self._by_slot.get((formid >> 24) & 0xFF)
        if idx is None:
            return None, 0
        return idx, ((idx.own_index << 24) | (formid & 0x00FFFFFF))

    def _to_child(self, idx, formid: int) -> int:
        """Translate one of `idx`'s own-space ids back into the child's space."""
        for slot, cand in self._by_slot.items():
            if cand is idx:
                return (slot << 24) | (formid & 0x00FFFFFF)
        return formid

    def __contains__(self, formid: int) -> bool:
        idx, own = self._route(formid)
        return idx is not None and own in idx

    def __len__(self) -> int:
        return len(self.formids())

    def formids(self) -> set:
        return {(slot << 24) | (f & 0x00FFFFFF)
                for slot, idx in self._by_slot.items()
                for f in idx._offsets
                if (f >> 24) & 0xFF == idx.own_index}

    def signature(self, formid: int) -> bytes:
        idx, own = self._route(formid)
        return idx.signature(own) if idx else b''

    def record(self, formid: int) -> bytes:
        """The master's converted record, RESTATED in the child's id space.

        The bytes on disk are numbered in the master's own converted space, so
        when the child gives that master a different slot every FormID inside
        them — the record's own id in the header, and every reference in the
        body — names the wrong file. Shipping them verbatim emitted an override
        at an id another master owns as a different record type: 2,553 of them
        on TWMP Valenwood/Elsweyr, e.g. ElsweyrAnequina's REFR 0201C4B3 written
        over Tamriel.esp's CELL 0201C4B3 (xEdit: "Record [CELL:0201C4B3] in
        Tamriel.esp is being overridden by record [REFR:0201C4B3]"), which
        hangs the engine on the main menu.

        A master at the same slot it uses itself needs no rewrite, which is why
        this stayed invisible until a plugin declared a SECOND TES4 master.
        """
        idx, own = self._route(formid)
        if idx is None:
            return b''
        rec = idx.record(own)
        imap = self._index_maps.get(id(idx))
        return _shift_record_formids(rec, imap) if (rec and imap) else rec

    def group_path(self, formid: int) -> tuple:
        """The master's nesting, with its GRUP labels restated for the child.

        A type-1/6/7 label IS a FormID (the owning WRLD/CELL), so it needs the
        same translation the record bytes do or the override nests under a
        group the child resolves to a different record.
        """
        idx, own = self._route(formid)
        if idx is None:
            return ()
        path = idx.group_path(own)
        imap = self._index_maps.get(id(idx))
        if not imap or not path:
            return path
        out = []
        for gtype, label in path:
            if gtype in _FORMID_LABEL_GROUPS and len(label) == 4:
                fid = struct.unpack('<I', label)[0]
                shifted = _shift_formid(fid, imap)
                if shifted != fid:
                    label = struct.pack('<I', shifted)
            out.append((gtype, label))
        return tuple(out)

    def land(self, cell_formid: int) -> int:
        """The LAND inside a cell, answered by the cell's owning master."""
        idx, own = self._route(cell_formid)
        if idx is None:
            return 0
        fid = idx.land(own)
        return self._to_child(idx, fid) if fid else 0

    def find_by_edid(self, signature: bytes, edid: str) -> int:
        """Later masters win, matching load order."""
        for idx in reversed(self._indices):
            fid = idx.find_by_edid(signature, edid)
            if fid:
                return self._to_child(idx, fid)
        return 0


class MissingMasterOutputError(RuntimeError):
    """A plugin's converted master output is required but absent."""


def resolve_master_outputs(masters: list, tes4_master_count: int,
                           output_root: str) -> list:
    """Converted-master plugin paths for a plugin's TES4 masters.

    `masters` is the TES5 master list (new masters prepended); only the trailing
    `tes4_master_count` entries are TES4 masters we convert ourselves. Skyrim.esm
    and friends are vanilla and are never merged against.

    Returns [(master_name, path_or_None), ...] in load order.
    """
    if not tes4_master_count:
        return []
    out = []
    for name in masters[len(masters) - tes4_master_count:]:
        # convert.py writes output/<plugin>/<plugin>
        path = os.path.join(output_root, name, name)
        out.append((name, path if os.path.isfile(path) else None))
    return out


def load_master_index(masters: list, tes4_master_count: int,
                      output_root: str):
    """Index the converted TES4 masters, or raise if any is missing.

    An override IS the master's converted record, so without it there is
    nothing to override and the conversion cannot proceed.
    """
    resolved = resolve_master_outputs(masters, tes4_master_count, output_root)
    if not resolved:
        return None

    missing = [(n, os.path.join(output_root, n, n))
               for n, p in resolved if p is None]
    if missing:
        lines = [
            "Converted master output not found - cannot convert overrides.",
            "",
            "This plugin's records mostly OVERRIDE its master, and an override",
            "is emitted as the master's converted record with the author's",
            "changes applied. Without it there is nothing to override.",
            "",
            "Missing:",
        ]
        lines += [f"  {name}  (expected at {path})" for name, path in missing]
        lines += ["", "Convert the master first:"]
        lines += [f"  python convert.py -f {name}" for name, _ in missing]
        raise MissingMasterOutputError("\n".join(lines))

    indices = [MasterIndex(path) for _, path in resolved]
    if len(indices) == 1:
        return indices[0]
    # The TES4 masters are the TRAILING entries of the TES5 master list, so the
    # first of them sits at this slot in the child's FormID space. Routing by
    # index byte needs the real slot, not a guess — see ChainedMasterIndex.
    return ChainedMasterIndex(
        indices,
        base_slot=len(masters) - tes4_master_count,
        child_masters=masters)
