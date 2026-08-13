"""
TES5 (Skyrim SE) binary file writer.

Writes ESM/ESP files with proper record headers, group hierarchy,
and subrecord structures for Skyrim Special Edition.

TES5 record header: 24 bytes
  sig[4] + dataSize[4] + flags[4] + formID[4] + vcs1[4] + formVersion[2] + vcs2[2]
GRUP header: 24 bytes
  'GRUP'[4] + groupSize[4] + label[4] + groupType[4] + stamp[4] + unknown[4]
Subrecord header: 6 bytes
  sig[4] + dataSize[2]
"""

import contextlib
import struct
import threading

RECORD_HEADER_SIZE = 24
GROUP_HEADER_SIZE = 24
SUBRECORD_HEADER_SIZE = 6
FORM_VERSION_SSE = 44
HEDR_VERSION_SSE = 1.7100000381469727  # float32 representation of 1.71


def pack_subrecord(sig: str, data: bytes) -> bytes:
    """Pack a single subrecord: header + data."""
    sig_bytes = sig.encode('ascii')[:4].ljust(4, b'\x00')
    if len(data) > 65535:
        # Use XXXX protocol for oversized subrecords
        xxxx = b'XXXX' + struct.pack('<H', 4) + struct.pack('<I', len(data))
        return xxxx + sig_bytes + struct.pack('<H', 0) + data
    return sig_bytes + struct.pack('<H', len(data)) + data


def pack_string_subrecord(sig: str, value: str) -> bytes:
    """Pack a null-terminated string subrecord."""
    data = value.encode('utf-8') + b'\x00'
    return pack_subrecord(sig, data)


def pack_record(sig: str, form_id: int, flags: int, subrecords: bytes,
                form_version: int = FORM_VERSION_SSE) -> bytes:
    """Pack a complete record: header + subrecord data."""
    # Clear compressed flag (0x00040000) since we write uncompressed data
    flags = flags & ~0x00040000
    sig_bytes = sig.encode('ascii')[:4].ljust(4, b'\x00')
    header = struct.pack('<4sIIIIHH',
                         sig_bytes,
                         len(subrecords),  # dataSize
                         flags,
                         form_id,
                         0,  # vcs1
                         form_version,
                         0)  # vcs2
    return header + subrecords


def pack_group(group_type: int, label: bytes, contents: bytes) -> bytes:
    """Pack a GRUP with its contents. label is 4 raw bytes."""
    total_size = GROUP_HEADER_SIZE + len(contents)
    header = struct.pack('<4sI4siII',
                         b'GRUP',
                         total_size,
                         label,
                         group_type,
                         0,  # stamp
                         0)  # unknown
    return header + contents


def pack_top_group(sig: str, contents: bytes) -> bytes:
    """Pack a type-0 (top-level) group for a record signature."""
    label = sig.encode('ascii')[:4].ljust(4, b'\x00')
    return pack_group(0, label, contents)


def _count_records_and_groups(blob: bytes) -> int:
    """Count every record and GRUP in a serialized group blob (recursively).

    Used to compute the TES4 header's HEDR record count from the actual written
    bytes. A GRUP header is 24 bytes with its total size at offset 4 (covering
    the header); a record header is 24 bytes with its DATA size at offset 4
    (NOT covering the header). Both GRUPs and records are counted, matching
    vanilla Skyrim.esm (HEDR ≈ records + groups).
    """
    count = 0
    pos = 0
    n = len(blob)
    while pos + 24 <= n:
        tag = blob[pos:pos + 4]
        size = struct.unpack_from('<I', blob, pos + 4)[0]
        count += 1
        if tag == b'GRUP':
            # Recurse into the group's children (size includes the 24B header).
            count += _count_records_and_groups(blob[pos + 24:pos + size])
            pos += size
        else:
            pos += 24 + size
    return count


# Record types the engine loads ON DEMAND from a cell's TEMPORARY children
# group. An override of one of these must be announced in the header's ONAM
# array or the engine never looks for it.  Sourced from xEdit's TES4 header
# definition (wbDefinitionsTES5.pas: wbArray(ONAM, 'Overridden Forms', ...))
# and its ONAM builder (wbImplementation.pas ~5412).
ONAM_SIGNATURES = frozenset({
    b'ACHR', b'LAND', b'NAVM', b'PARW', b'PBAR', b'PBEA', b'PCON',
    b'PFLA', b'PGRE', b'PHZD', b'PMIS', b'REFR',
})


def pack_tes4_header(masters: list, num_records: int = 0,
                     next_object_id: int = 0x800,
                     author: str = "TES4-to-TES5 Converter",
                     description: str = "",
                     is_esm: bool = True,
                     overridden_forms: list = None) -> bytes:
    """Pack the TES4 file header record.

    `overridden_forms` becomes the ONAM array: every record this file overrides
    that lives in a master's TEMPORARY cell-children group. Skyrim loads those
    on demand per cell, so an override the header does not announce is simply
    ignored — xEdit's docs put it plainly: "the game engine will ignore the
    override that is missing from ONAM".
    """
    subs = b''

    # HEDR
    hedr_data = struct.pack('<fII', HEDR_VERSION_SSE, num_records, next_object_id)
    subs += pack_subrecord('HEDR', hedr_data)

    # CNAM (author)
    if author:
        subs += pack_string_subrecord('CNAM', author)

    # SNAM (description)
    if description:
        subs += pack_string_subrecord('SNAM', description)

    # MAST + DATA pairs
    for master in masters:
        subs += pack_string_subrecord('MAST', master)
        subs += pack_subrecord('DATA', b'\x00' * 8)

    # ONAM follows the master list (xEdit places it right after the
    # 'Master Files' array). Sorted so the output stays reproducible.
    if overridden_forms:
        subs += pack_subrecord('ONAM', b''.join(
            struct.pack('<I', fid) for fid in sorted(set(overridden_forms))))

    flags = 0x01 if is_esm else 0x00  # ESM flag
    return pack_record('TES4', 0, flags, subs, FORM_VERSION_SSE)


# GRUP types whose label is the FormID of the record that OWNS the group.
# There may be at most ONE of each per owner: World Children (1), Cell Children
# (6) and Topic Children (7).
_OWNED_GROUP_TYPES = (1, 6, 7)

# Exterior block (4) and sub-block (5) groups. Their label packs the grid as
# `struct.pack('<hh', Y, X)` -- Y in the LOW word -- and the engine requires
# the siblings to ascend by UNSIGNED (X, Y), X major. See
# import_main._grid_sort_key for the Skyrim.esm census.
_GRID_GROUP_TYPES = (4, 5)


def _grid_key(label: bytes):
    y, x = struct.unpack('<HH', label)
    return (x, y)


def _merge_grid_groups(body: bytes) -> bytes:
    """Merge duplicate block/sub-block groups in `body` and re-sort them.

    Concatenating two owned-group bodies is not enough on its own. The
    override pass and the WRLD builder each emit a worldspace's exterior
    blocks ALREADY SORTED, so folding their type-1 groups together yields one
    group holding two ascending runs -- and a duplicate type-4 group wherever
    both passes touched the same block.

    The engine walks this list to build the worldspace's cell grid while
    PARSING the file. A run where X descends and re-ascends never terminates:
    the game hangs on the main menu with no crash and no log, and xEdit still
    calls the file clean. Measured before this merge: Tamriel.esp shipped 121
    block groups in 15 ascending runs with 14 duplicate labels;
    ElsweyrAnequina.esp 8 in 2 runs with 3 duplicates.

    Groups of the same type and label are folded into one and their contents
    merged recursively, so a sub-block that both passes wrote also collapses
    to a single group. Records and other group types are copied through in
    place, and a malformed tail is left untouched.
    """
    if not body:
        return body
    parts = []            # [[key, header+contents]] — key None = copy through
    index = {}
    off, end = 0, len(body)
    while off + GROUP_HEADER_SIZE <= end:
        sig = body[off:off + 4]
        raw = struct.unpack_from('<I', body, off + 4)[0]
        if sig != b'GRUP':
            size = GROUP_HEADER_SIZE + raw
            if off + size > end:
                break
            parts.append([None, bytearray(body[off:off + size])])
            off += size
            continue
        size = raw
        if size < GROUP_HEADER_SIZE or off + size > end:
            break
        gtype = struct.unpack_from('<i', body, off + 12)[0]
        label = bytes(body[off + 8:off + 12])
        key = ((gtype, label)
               if gtype in _GRID_GROUP_TYPES and len(label) == 4 else None)
        if key is not None and key in index:
            parts[index[key]][1] += body[off + GROUP_HEADER_SIZE:off + size]
        else:
            if key is not None:
                index[key] = len(parts)
            parts.append([key, bytearray(body[off:off + size])])
        off += size

    # Sort ONLY the grid groups, leaving every other element where it sits:
    # the persistent cell's type-6 group must keep its place at the front.
    grid_at = [i for i, (key, _d) in enumerate(parts) if key is not None]
    if grid_at:
        ordered = sorted((parts[i] for i in grid_at),
                         key=lambda p: _grid_key(p[0][1]))
        for slot, item in zip(grid_at, ordered):
            parts[slot] = item

    out = bytearray()
    for key, data in parts:
        if key is not None:
            inner = _merge_grid_groups(bytes(data[GROUP_HEADER_SIZE:]))
            data = bytearray(data[:GROUP_HEADER_SIZE]) + inner
            struct.pack_into('<I', data, 4, len(data))
        out += data
    out += body[off:]
    return bytes(out)


def _merge_owned_groups(blob: bytes) -> bytes:
    """Fold repeated owned-groups with the same label into the first one.

    Two passes append to the same top-level group — the override pass writes a
    worldspace's children, then the WRLD builder writes the cells this plugin
    adds to it — so the file ended up with TWO `GRUP World Children of
    0100003C` in a row. xEdit reports "Found additional GRUP World Children of
    ... Skipped Load: Merged N elements from duplicate group", and what the
    ENGINE does with the second copy is undefined: it indexes a worldspace's
    children once, so the later group's cells can simply never load.

    Only the top level of `blob` is scanned, EXCEPT for a group that actually
    absorbed a duplicate: both passes emit a worldspace's exterior blocks
    already sorted, so the folded body holds two ascending runs (and a
    duplicate type-4 group per block both passes touched). The engine's
    parse-time grid walk needs one monotonic run, so a merged body is handed
    to `_merge_grid_groups`. A group with no duplicate is moved verbatim.
    """
    if not blob:
        return blob
    parts = []            # [[key, bytearray]] — key None = copied through
    index = {}
    merged = set()        # keys that absorbed at least one duplicate
    off, end = 0, len(blob)
    while off + GROUP_HEADER_SIZE <= end:
        sig = blob[off:off + 4]
        raw = struct.unpack_from('<I', blob, off + 4)[0]
        if sig != b'GRUP':
            # A RECORD's size excludes its 24-byte header; a GRUP's includes
            # it. Applying the group rule to a record truncates the walk.
            size = GROUP_HEADER_SIZE + raw
            if off + size > end:
                break
            parts.append([None, bytearray(blob[off:off + size])])
            off += size
            continue
        size = raw
        if size < GROUP_HEADER_SIZE or off + size > end:
            break         # malformed: leave the remainder untouched
        gtype = struct.unpack_from('<i', blob, off + 12)[0]
        label = blob[off + 8:off + 12]
        body = blob[off + GROUP_HEADER_SIZE:off + size]
        key = (gtype, bytes(label)) if gtype in _OWNED_GROUP_TYPES else None
        if key is not None and key in index:
            parts[index[key]][1] += body
            merged.add(key)
        else:
            if key is not None:
                index[key] = len(parts)
            parts.append([key, bytearray(blob[off:off + size])])
        off += size

    out = bytearray()
    for key, data in parts:
        if key is not None:
            if key in merged:
                # Two sorted runs were concatenated above: fold the duplicate
                # blocks together and restore one ascending (X, Y) run.
                inner = _merge_grid_groups(bytes(data[GROUP_HEADER_SIZE:]))
                data = bytearray(data[:GROUP_HEADER_SIZE]) + inner
            # Re-stamp the (possibly grown) size in the group header.
            struct.pack_into('<I', data, 4, len(data))
        out += data
    out += blob[off:]     # trailing bytes from a malformed tail, if any
    return bytes(out)


def pack_obnd(x1: int = 0, y1: int = 0, z1: int = 0,
              x2: int = 0, y2: int = 0, z2: int = 0) -> bytes:
    """Pack OBND (Object Bounds) subrecord — required on most TES5 records."""
    data = struct.pack('<6h', x1, y1, z1, x2, y2, z2)
    return pack_subrecord('OBND', data)


def pack_formid_subrecord(sig: str, form_id: int) -> bytes:
    """Pack a FormID subrecord."""
    return pack_subrecord(sig, struct.pack('<I', form_id))


def pack_float_subrecord(sig: str, value: float) -> bytes:
    """Pack a float32 subrecord."""
    return pack_subrecord(sig, struct.pack('<f', value))


def pack_uint32_subrecord(sig: str, value: int) -> bytes:
    """Pack a uint32 subrecord."""
    return pack_subrecord(sig, struct.pack('<I', value))


def pack_uint16_subrecord(sig: str, value: int) -> bytes:
    """Pack a uint16 subrecord."""
    return pack_subrecord(sig, struct.pack('<H', value))


def pack_uint8_subrecord(sig: str, value: int) -> bytes:
    """Pack a uint8 subrecord."""
    return pack_subrecord(sig, struct.pack('<B', value))


class PluginWriter:
    """High-level writer for building a TES5 plugin file.

    Usage:
        w = PluginWriter(masters=['Skyrim.esm'], is_esm=True)
        w.add_record('STAT', formid, flags, subrecords_bytes)
        w.write('output.esm')
    """

    def __init__(self, masters: list = None, is_esm: bool = True,
                 author: str = "TES4-to-TES5 Converter",
                 description: str = ""):
        self.masters = masters or []
        self.is_esm = is_esm
        self.author = author
        self.description = description
        # Groups: sig -> list of (record_bytes)
        # For CELL/WRLD/DIAL, we store pre-built group bytes
        self._top_groups = {}
        self._record_count = 0
        self._next_object_id = 0x800
        self._lock = threading.Lock()  # guards alloc_formid and add_record
        self.own_index = len(self.masters)

        # --- Companion manifest -------------------------------------------
        # Converting a record often generates COMPANION records (ARMO -> ARMA,
        # NPC_ -> OTFT/VTYP, AMMO -> PROJ, ...) whose FormIDs come from
        # alloc_formid(), a bare sequential counter. A plugin that overrides
        # such a record must reuse the MASTER's companions, not mint its own —
        # and it can only do that if the master's run recorded which companion
        # belongs to which source record.
        #
        # Rather than touch all 39 alloc_formid() call sites, the writer
        # records the pairing itself: converters run inside
        # `converting(source_formid)` and every id allocated during that window
        # is attributed to that source record. No inference, no guessing.
        self._manifest = {}          # source TES4 fid (hex str) -> {...}
        self._converting = None

    @property
    def next_object_id(self):
        return self._next_object_id

    @next_object_id.setter
    def next_object_id(self, val):
        self._next_object_id = val

    @contextlib.contextmanager
    def converting(self, source_formid: str, output_formid: int = 0):
        """Attribute records/FormIDs created in this block to a source record."""
        prev = self._converting
        key = (source_formid or '').upper()
        self._converting = key
        if key:
            entry = self._manifest.setdefault(
                key, {'fid': 0, 'companions': []})
            if output_formid:
                entry['fid'] = output_formid
        try:
            yield
        finally:
            self._converting = prev

    def alloc_formid(self) -> int:
        """Allocate a new FormID for generated records (ARMA, TXST, etc.). Thread-safe."""
        with self._lock:
            fid = self._next_object_id
            self._next_object_id += 1
            if self._converting:
                self._manifest[self._converting]['companions'].append(fid)
        return fid

    def manifest(self) -> dict:
        """source TES4 FormID -> {'fid': converted id, 'companions': [ids]}."""
        return self._manifest

    def add_record(self, group_sig: str, record_bytes: bytes):
        """Add a packed record to a top-level group. Thread-safe."""
        with self._lock:
            if group_sig not in self._top_groups:
                self._top_groups[group_sig] = []
            self._top_groups[group_sig].append(record_bytes)
            self._record_count += 1

    def add_raw_group(self, group_sig: str, group_bytes: bytes):
        """Add pre-built group bytes (for CELL/WRLD/DIAL hierarchies)."""
        with self._lock:
            if group_sig not in self._top_groups:
                self._top_groups[group_sig] = []
            self._top_groups[group_sig].append(group_bytes)

    def _collect_overridden_temporary(self, group_blobs: list) -> list:
        """FormIDs this file overrides inside a master's TEMPORARY cell group.

        These go in the header's ONAM array. Skyrim loads a cell's temporary
        children on demand, and it discovers a plugin's overrides of them from
        ONAM alone — an override missing from the list is ignored outright, so
        the master's version keeps winning (or, when we replaced the cell's
        child list, nothing loads at all).

        Only TEMPORARY children count: persistent records (group type 8) are
        always resident and are explicitly skipped by xEdit's builder too. A
        record we define ourselves is not an override, so only FormIDs at a
        master's load-order index qualify.
        """
        out = []

        def walk(blob, temporary=False):
            off, end = 0, len(blob)
            while off + GROUP_HEADER_SIZE <= end:
                sig = blob[off:off + 4]
                size = struct.unpack_from('<I', blob, off + 4)[0]
                if sig == b'GRUP':
                    gtype = struct.unpack_from('<i', blob, off + 12)[0]
                    # 9 = temporary children, 8 = persistent, 10 = visible-distant
                    walk(blob[off + GROUP_HEADER_SIZE:off + size],
                         temporary or gtype == 9)
                    off += size
                else:
                    if temporary and sig in ONAM_SIGNATURES:
                        fid = struct.unpack_from('<I', blob, off + 12)[0]
                        if (fid >> 24) & 0xFF < self.own_index:
                            out.append(fid)
                    off += RECORD_HEADER_SIZE + size

        for blob in group_blobs:
            walk(blob)
        return out

    def write(self, filepath: str):
        """Write the complete plugin file using an atomic temp-then-rename approach.

        Writes to a .tmp sibling first so that a locked output file (e.g. held
        open by xEdit or Skyrim) gives a clear error rather than corrupting an
        existing plugin.  On success, the .tmp is renamed to the final path.
        """
        import os
        tmp_path = filepath + '.tmp'

        # Assemble the top-level group bodies first so the header's HEDR record
        # count can be computed from the ACTUAL written content. The count must
        # include records nested in CELL/WRLD/DIAL hierarchies (added via
        # add_raw_group, which never touched _record_count) plus every GRUP —
        # vanilla Skyrim.esm's HEDR ≈ records + groups. A count that omits the
        # hierarchies (our old 38,585 vs 1.17M real) leaves the header wildly
        # under-reporting, which the engine's loader does not tolerate cleanly.
        staged = {}
        for sig in self._group_order():
            if sig not in self._top_groups:
                continue
            contents = _merge_owned_groups(b''.join(self._top_groups[sig]))
            if contents:
                staged[sig] = [contents]

        group_blobs = []
        for sig in self._group_order():
            for blob in staged.get(sig, []):
                if blob:
                    group_blobs.append(pack_top_group(sig, blob))

        total_count = sum(_count_records_and_groups(b) for b in group_blobs)
        overridden = self._collect_overridden_temporary(group_blobs)

        try:
            with open(tmp_path, 'wb') as f:
                # TES4 header
                header = pack_tes4_header(
                    self.masters,
                    num_records=total_count,
                    next_object_id=self._next_object_id,
                    author=self.author,
                    description=self.description,
                    is_esm=self.is_esm,
                    overridden_forms=overridden,
                )
                f.write(header)

                for blob in group_blobs:
                    f.write(blob)
        except Exception:
            # Clean up partial temp file so it doesn't litter the output dir
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

        # Atomic rename: replaces the target even if it already exists
        try:
            os.replace(tmp_path, filepath)
        except PermissionError as e:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise PermissionError(
                f"Cannot write to '{filepath}': file is locked (is it open in xEdit or Skyrim?). "
                f"Original error: {e}"
            ) from e

    def _group_order(self) -> list:
        """Canonical top-level group ordering for TES5.

        CRITICAL: QUST must come AFTER CELL, WRLD and DIAL (this is vanilla
        Skyrim.esm's order: …CELL, WRLD, DIAL, QUST, …). The engine/CK loads
        top-level groups in file order and resolves a quest's forced-reference
        aliases (ALFR) WHEN it loads the QUST group. If QUST precedes CELL/WRLD,
        the ACHR/REFR targets living in those cell groups are not in the form
        map yet, so every forced ref fails with "[QUESTS] Could not find forced
        ref (…)" and the alias fills NONE (no marker). A quest in a PLUGIN can
        still find a ref in a MASTER because masters load fully first — which is
        exactly why the same alias resolved in the test ESP but not in-file.
        DIAL is also placed before QUST to match vanilla.
        """
        order = [
            'GMST', 'KYWD', 'TXST', 'GLOB', 'CLAS', 'FACT', 'HDPT', 'EYES',
            'RACE', 'SOUN', 'SOPM', 'SNDR', 'MGEF',
            # xEdit canonical: ... FLST PERK BPTD ADDN AVIF ... VTYP MATT —
            # BPTD (creature body part data) precedes the MATT/impact block
            'BPTD', 'MATT',
            # Impact/footstep chain. xEdit's canonical order is
            # "... VTYP MATT IPCT IPDS ARMA ... FSTP FSTS ...", so IPCT/IPDS
            # must precede ARMA (whose SNDD points into it) and FSTP must
            # precede the FSTS that lists it. Creature footstep audio rides
            # this chain — see tes5_import/creature_footsteps.py.
            'IPCT', 'IPDS', 'FSTP', 'FSTS',
            'STAT', 'ACTI', 'CONT', 'DOOR',
            'FLOR', 'FURN', 'GRAS', 'TREE', 'LIGH', 'MISC', 'KEYM', 'ARMO',
            'ARMA', 'BOOK', 'AMMO', 'ENCH', 'SPEL', 'ALCH', 'INGR', 'SCRL',
            'SLGM', 'VTYP', 'OTFT', 'NPC_', 'LVLN', 'LVLI', 'LVSP',
            # IMGS before WTHR: a weather's IMSP points at the imagespaces
            # carrying its HDR tone mapping, and CLMT/REGN then point at WTHR.
            'IMGS', 'WTHR',
            'CLMT', 'REGN', 'IDLE', 'PACK', 'EFSH', 'LSCR', 'ANIO',
            'WEAP', 'NAVI',
            # References resolved by quests must exist before QUST loads:
            'CELL', 'WRLD',
            'DIAL',
            'QUST',
            # LCTN must come AFTER CELL/WRLD (vanilla: ... ECZN LCTN ... DLBR
            # DLVW at the very end): the CK resolves each Location's LCEC
            # worldspace + MNAM marker ref when the LCTN group loads, and with
            # LCTN first every location logged "Could not find worldspace
            # (0100003C) in load" — 512 warnings and undiscoverable markers.
            'LCTN',
            # Vanilla puts MESG just after LCTN. Nothing in a MESG is resolved
            # order-sensitively at load (ours carry no conditions), but keep
            # the canonical slot rather than letting it append after DLVW.
            'MESG',
            'SMBN', 'SMQN', 'SMEN',
            'DLBR', 'DLVW',
        ]
        # Append any groups not in the canonical order
        for sig in self._top_groups:
            if sig not in order:
                order.append(sig)
        return order
