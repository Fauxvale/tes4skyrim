"""
FO3/FNV export deltas: the subrecords Oblivion never emits.

Only fields whose FO3/FNV layout differs from TES4, or that TES4 lacks
entirely, live here. Everything the two games share is dumped by the
regular per-type exporters in this package.

See: docs/commentary/tes4_export_falloutnv.md
"""

import struct

from ..tes4_reader import Record, get_formid_str, get_string, get_subrecord
from .common import emit_model, emit_raw_hex, emit_script, emit_string

#: HEDR.Version reported by FO3/FNV plugins; Oblivion reports 0.8 or 1.0.
FALLOUT_HEDR_MIN = 1.2


def is_fallout(hedr_version: float) -> bool:
    """True when a plugin's HEDR version marks it FO3/FNV rather than TES4."""
    return hedr_version >= FALLOUT_HEDR_MIN


def _emit_obnd(lines: list, rec: Record):
    """OBND (12B) -> six s16 bounds; Skyrim-native, so it passes through."""
    obnd = get_subrecord(rec, "OBND")
    if obnd and len(obnd.data) >= 12:
        x1, y1, z1, x2, y2, z2 = struct.unpack_from("<6h", obnd.data, 0)
        lines.append(f"OBND.X1={x1}")
        lines.append(f"OBND.Y1={y1}")
        lines.append(f"OBND.Z1={z1}")
        lines.append(f"OBND.X2={x2}")
        lines.append(f"OBND.Y2={y2}")
        lines.append(f"OBND.Z2={z2}")


def _emit_cell_deltas(lines: list, rec: Record):
    """CELL's FO3/FNV-only fields: the land-flag byte and the water noise texture."""
    xclc = get_subrecord(rec, "XCLC")
    if xclc and len(xclc.data) >= 12:
        lines.append(f"XCLC.LandFlags={xclc.data[8]}")

    xcll = get_subrecord(rec, "XCLL")
    if xcll and len(xcll.data) >= 40:
        lines.append(f"XCLL.FogPower={struct.unpack_from('<f', xcll.data, 36)[0]}")

    xnam = get_subrecord(rec, "XNAM")
    if xnam and len(xnam.data) > 1:
        lines.append(f"XNAM.WaterNoiseTexture={get_string(xnam)}")


def _emit_refr_deltas(lines: list, rec: Record):
    """REFR's FO3/FNV-only placement fields that map onto Skyrim equivalents."""
    xprm = get_subrecord(rec, "XPRM")
    if xprm and len(xprm.data) >= 32:
        bx, by, bz = struct.unpack_from("<3f", xprm.data, 0)
        lines.append(f"XPRM.BoundX={bx}")
        lines.append(f"XPRM.BoundY={by}")
        lines.append(f"XPRM.BoundZ={bz}")

    xrds = get_subrecord(rec, "XRDS")
    if xrds and len(xrds.data) >= 4:
        lines.append(f"XRDS.Radius={struct.unpack_from('<f', xrds.data, 0)[0]}")

    xemi = get_subrecord(rec, "XEMI")
    if xemi and len(xemi.data) >= 4:
        lines.append(f"XEMI.Emittance={get_formid_str(struct.unpack_from('<I', xemi.data, 0)[0])}")

    xlkr = get_subrecord(rec, "XLKR")
    if xlkr and len(xlkr.data) >= 4:
        lines.append(f"XLKR.LinkedRef={get_formid_str(struct.unpack_from('<I', xlkr.data, 0)[0])}")


#: FO3/FNV weapon anim type -> the equivalent TES4 DATA.Type.
_FALLOUT_WEAPON_TYPE = {
    0: 0, 1: 0, 2: 1, 3: 5, 4: 5, 5: 5, 6: 5,
    7: 5, 8: 1, 9: 5, 10: 5, 11: 5, 12: 5, 13: 5,
}

#: FO3/FNV anim types that are firearms rather than melee, by hand count.
FALLOUT_PISTOL_TYPES = frozenset({3, 4, 10, 11, 12, 13})
FALLOUT_LONGARM_TYPES = frozenset({5, 6, 7, 9})


def _emit_weap_deltas(lines: list, rec: Record):
    """WEAP's FO3/FNV layout: DATA carries the economy, DNAM the animation.

    TES4 packs both into one DATA; the keys emitted here are the TES4 ones so
    the importer needs no new vocabulary for the shared fields.

    See: docs/commentary/tes4_export_falloutnv.md#weapons-guns-become-crossbows
    """
    dnam = get_subrecord(rec, "DNAM")
    if dnam and len(dnam.data) >= 12:
        anim = struct.unpack_from("<I", dnam.data, 0)[0]
        lines.append(f"DATA.Type={_FALLOUT_WEAPON_TYPE.get(anim, 0)}")
        lines.append(f"DNAM.FalloutAnimType={anim}")
        lines.append(f"DATA.Speed={struct.unpack_from('<f', dnam.data, 4)[0]}")
        lines.append(f"DATA.Reach={struct.unpack_from('<f', dnam.data, 8)[0]}")

    data = get_subrecord(rec, "DATA")
    if data and len(data.data) >= 15:
        d = data.data
        lines.append(f"DATA.Value={struct.unpack_from('<i', d, 0)[0]}")
        lines.append(f"DATA.Health={struct.unpack_from('<i', d, 4)[0]}")
        lines.append(f"DATA.Weight={struct.unpack_from('<f', d, 8)[0]}")
        lines.append(f"DATA.Damage={struct.unpack_from('<h', d, 12)[0]}")
        lines.append(f"DATA.ClipSize={d[14]}")


def _emit_navm_deltas(lines: list, rec: Record):
    """NAVM's authored geometry: the cell it covers, its vertices and triangles.

    FO3/FNV ship real navmeshes where TES4 has only pathgrids, so this is
    authored data to repack rather than geometry to generate. NVVX/NVTR/NVDP
    are dumped verbatim; the importer reinterprets them into TES5's NVNM blob.

    See: docs/commentary/tes4_export_falloutnv.md#navmesh-authored-not-generated
    """
    data = get_subrecord(rec, "DATA")
    if data and len(data.data) >= 24:
        cell, nvert, ntri, nedge, ncover, ndoor = struct.unpack_from(
            "<6I", data.data, 0)
        lines.append(f"DATA.Cell={get_formid_str(cell)}")
        lines.append(f"DATA.VertexCount={nvert}")
        lines.append(f"DATA.TriangleCount={ntri}")
        lines.append(f"DATA.EdgeLinkCount={nedge}")
        lines.append(f"DATA.CoverTriangleCount={ncover}")
        lines.append(f"DATA.DoorLinkCount={ndoor}")

    for sig in ("NVVX", "NVTR", "NVDP"):
        emit_raw_hex(lines, sig, get_subrecord(rec, sig))


def _emit_navi_deltas(lines: list, rec: Record):
    """NAVI's per-navmesh info entries, one NVMI blob per line.

    NVMI repeats thousands of times in a single record, so each is emitted
    with its ordinal plus a total count.

    See: docs/commentary/tes4_export_falloutnv.md#navmesh-authored-not-generated
    """
    index = 0
    for sub_rec in rec.subrecords:
        if sub_rec.type != "NVMI":
            continue
        lines.append(f"NVMI[{index}]={sub_rec.data.hex().upper()}")
        index += 1
    lines.append(f"NVMI.Count={index}")


#: Per-type delta emitters, consulted by format_record only for FO3/FNV sources.
_DELTA_DISPATCH = {
    "CELL": _emit_cell_deltas,
    "REFR": _emit_refr_deltas,
    "WEAP": _emit_weap_deltas,
    "NAVM": _emit_navm_deltas,
    "NAVI": _emit_navi_deltas,
}

#: Types carrying an OBND that TES4 has no field for; Skyrim reads it natively.
_OBND_TYPES = frozenset({
    "STAT", "DOOR", "ACTI", "CONT", "FURN", "LIGH", "MISC", "KEYM",
    "BOOK", "TREE", "GRAS", "FLOR", "ALCH", "AMMO", "ARMO", "WEAP",
})


def export_deltas(rec: Record) -> list:
    """The FO3/FNV-only lines for one record, appended after its TES4 export."""
    lines = []
    if rec.type in _OBND_TYPES:
        _emit_obnd(lines, rec)
    handler = _DELTA_DISPATCH.get(rec.type)
    if handler:
        handler(lines, rec)
    return lines


def export_STATIC_BASE(rec: Record) -> list:
    """A model-only FO3/FNV base object, converted as a Skyrim STAT.

    MSTT, SCOL, PWAT and IDLM all reduce to a model plus bounds. They are the
    base objects of 10,000+ placed references; without them those REFRs have a
    null base and the engine faults promoting them into their location.

    See: docs/commentary/tes4_export_falloutnv.md#fallout-only-base-objects
    """
    lines = []
    emit_string(lines, "EditorID", get_subrecord(rec, "EDID"))
    emit_model(lines, "Model", rec)
    _emit_obnd(lines, rec)
    return lines


def export_ACTIVATOR_BASE(rec: Record) -> list:
    """A named, scriptable FO3/FNV base object, converted as a Skyrim ACTI.

    TERM, NOTE and TACT are activators in all but signature: each carries a
    model, a display name and (for TACT/TERM) a script.

    See: docs/commentary/tes4_export_falloutnv.md#fallout-only-base-objects
    """
    lines = []
    emit_string(lines, "EditorID", get_subrecord(rec, "EDID"))
    emit_string(lines, "FULL", get_subrecord(rec, "FULL"))
    emit_model(lines, "Model", rec)
    emit_script(lines, rec)
    _emit_obnd(lines, rec)
    return lines


#: FO3/FNV base-object types Oblivion lacks -> the exporter that reduces them.
def export_NAVMESH(rec: Record) -> list:
    """A navmesh record's identity; export_deltas emits its geometry."""
    lines = []
    emit_string(lines, "EditorID", get_subrecord(rec, "EDID"))
    return lines


FALLOUT_BASE_EXPORTERS = {
    "NAVM": export_NAVMESH,
    "NAVI": export_NAVMESH,
    "MSTT": export_STATIC_BASE,
    "SCOL": export_STATIC_BASE,
    "PWAT": export_STATIC_BASE,
    "IDLM": export_STATIC_BASE,
    "ASPC": export_STATIC_BASE,
    "TERM": export_ACTIVATOR_BASE,
    "NOTE": export_ACTIVATOR_BASE,
    "TACT": export_ACTIVATOR_BASE,
}
