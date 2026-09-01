"""FO3/FNV navmesh: authored geometry repacked into TES5's NVNM blob.

FO3/FNV ship real navmeshes where TES4 has only pathgrids, so nothing is
generated here — vertices, triangles and edge adjacency are all authored, and
only the container format changes. The TES5 serialiser is shared with the
pathgrid path.

See: docs/commentary/tes4_export_falloutnv.md#navmesh-authored-not-generated
"""

import struct

from ..pgrd_to_navm import pack_navm_record, pack_nvnm
from ..text_reader import get_hex_bytes
from ..writer import pack_string_subrecord, pack_subrecord
from .common import get_formid, get_int, get_str

#: Bytes per NVVX vertex: three f32.
_VERTEX_SIZE = 12

#: Bytes per NVTR triangle: 3 u16 verts, 3 s16 edges, u16 flags, u16 cover.
_TRIANGLE_SIZE = 16

#: Bytes per NVDP door link: REFR FormID, u16 triangle, 2 unused.
_DOOR_LINK_SIZE = 8


def parse_vertices(raw: bytes) -> list:
    """NVVX -> [(x, y, z)] in world units."""
    return [struct.unpack_from('<3f', raw, i * _VERTEX_SIZE)
            for i in range(len(raw) // _VERTEX_SIZE)]


def parse_triangles(raw: bytes) -> tuple:
    """NVTR -> ([(v0, v1, v2)], [(e01, e12, e20)]).

    FO3/FNV author the edge adjacency, so it is never recomputed. An edge
    naming no real triangle becomes -1: vanilla FalloutNV.esm has 43 such
    edges across 38 navmeshes, which downstream passes index with.

    See: docs/commentary/tes4_export_falloutnv.md#navmesh-authored-not-generated
    """
    count = len(raw) // _TRIANGLE_SIZE
    tris, adj = [], []
    for i in range(count):
        v0, v1, v2, e01, e12, e20 = struct.unpack_from(
            '<3H3h', raw, i * _TRIANGLE_SIZE)
        tris.append((v0, v1, v2))
        adj.append(tuple(e if 0 <= e < count else -1 for e in (e01, e12, e20)))
    return tris, adj


def parse_door_links(raw: bytes) -> list:
    """NVDP -> [(triangle_index, door_ref_fid)], the shape pack_nvnm wants."""
    out = []
    for i in range(len(raw) // _DOOR_LINK_SIZE):
        fid, tri = struct.unpack_from('<IH', raw, i * _DOOR_LINK_SIZE)
        out.append((tri, fid))
    return out


def _centroid(verts: list) -> tuple:
    """The mean vertex position, which NAVI's NVMI entry names."""
    n = len(verts)
    return (sum(v[0] for v in verts) / n,
            sum(v[1] for v in verts) / n,
            sum(v[2] for v in verts) / n)


def convert_NAVM(rec: dict, writer=None, cell_rec: dict = None,
                 navm_fid: int = None) -> tuple:
    """Repack one FO3/FNV NAVM into a TES5 NAVM record.

    Returns (navm_bytes, meta) matching convert_PGRD's contract, or
    (None, None) when the record carries no usable geometry.
    """
    verts = parse_vertices(get_hex_bytes(rec, 'NVVX'))
    tris, adj = parse_triangles(get_hex_bytes(rec, 'NVTR'))
    if not verts or not tris:
        return None, None

    cell_fid = get_formid(rec, 'DATA.Cell') or get_formid(rec, 'ParentCELL')
    wrld_fid = get_formid(rec, 'ParentWRLD')
    is_exterior = bool(wrld_fid)
    grid_x = get_int(cell_rec or {}, 'XCLC.X')
    grid_y = get_int(cell_rec or {}, 'XCLC.Y')

    if navm_fid is None:
        navm_fid = writer.derive_formid(
            'NAVM', (cell_fid, get_formid(rec, 'FormID')))

    door_tris = parse_door_links(get_hex_bytes(rec, 'NVDP'))
    nvnm = pack_nvnm(verts, tris, adj, [0] * len(tris),
                     wrld_fid, cell_fid, grid_x, grid_y, is_exterior,
                     door_tris=door_tris, navm_fid=navm_fid)

    subs = b''
    edid = get_str(rec, 'EditorID')
    if edid:
        subs += pack_string_subrecord('EDID', f'TES4Navm{edid}')
    subs += pack_subrecord('NVNM', nvnm)

    meta = {
        'fid': navm_fid,
        'wrld_fid': wrld_fid,
        'cell_fid': cell_fid,
        'grid_x': grid_x,
        'grid_y': grid_y,
        'is_exterior': is_exterior,
        'center': _centroid(verts),
        'base_objects': [],
        'geom_cached': False,
        'geometry': (verts, tris, []),
        'geom_hash': None,
        'door_refs': sorted({fid for (_t, fid) in door_tris}),
        'door_xndp': {fid: (navm_fid, ti) for (ti, fid) in door_tris},
    }
    return pack_navm_record(navm_fid, subs), meta


def precompute_fallout_navmeshes(by_type: dict, writer):
    """Every authored navmesh in this plugin, keyed as the generated cache is.

    None when the plugin ships none, which routes TES4 to the generator.
    The records are also aliased into by_type['PGRD'], the list the CELL/WRLD
    builders walk to find a cell's navmesh source.

    See: docs/commentary/tes4_export_falloutnv.md#navmesh-authored-not-generated
    """
    navms = by_type.get('NAVM', [])
    if not navms:
        return None
    by_type.setdefault('PGRD', []).extend(navms)

    cell_by_fid = {get_formid(c, 'FormID'): c for c in by_type.get('CELL', [])}
    cache = {}
    for rec in navms:
        cell_fid = get_formid(rec, 'DATA.Cell') or get_formid(rec, 'ParentCELL')
        key = (cell_fid, get_formid(rec, 'FormID'))
        navm_bytes, meta = convert_NAVM(
            rec, cell_rec=cell_by_fid.get(cell_fid),
            navm_fid=writer.derive_formid('NAVM', key))
        if navm_bytes:
            cache[key] = (navm_bytes, meta)
    print(f"  Repacked {len(cache)} authored navmeshes (FO3/FNV NAVM)")
    return cache
