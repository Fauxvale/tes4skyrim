"""
FO3/FNV export deltas: the subrecords Oblivion never emits.

Only fields whose FO3/FNV layout differs from TES4, or that TES4 lacks
entirely, live here. Everything the two games share is dumped by the
regular per-type exporters in this package.

See: docs/commentary/tes4_export_falloutnv.md
"""

import struct

from ..tes4_reader import Record, get_formid_str, get_string, get_subrecord
from .common import emit_model, emit_script, emit_string

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


#: Per-type delta emitters, consulted by format_record only for FO3/FNV sources.
_DELTA_DISPATCH = {
    "CELL": _emit_cell_deltas,
    "REFR": _emit_refr_deltas,
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
FALLOUT_BASE_EXPORTERS = {
    "MSTT": export_STATIC_BASE,
    "SCOL": export_STATIC_BASE,
    "PWAT": export_STATIC_BASE,
    "IDLM": export_STATIC_BASE,
    "ASPC": export_STATIC_BASE,
    "TERM": export_ACTIVATOR_BASE,
    "NOTE": export_ACTIVATOR_BASE,
    "TACT": export_ACTIVATOR_BASE,
}
