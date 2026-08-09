"""Work out which output textures the shipped plugin can actually ask for.

Oblivion's BSAs carry textures for content the conversion never emits — the
character/face/body art whose meshes are skipped outright being the biggest
block — and copying the texture tree wholesale ships all of it.

This module only BUILDS the keep-set (`build_refs`); `bsa_pack` applies it
while staging the textures archive.  Nothing here deletes.  An earlier design
ran as its own phase and unlinked from `output/`, which was wrong twice over:
the mesh phase re-copies the whole texture tree on every run, so the deletions
were silently undone (and thus never noticed when the keep-set was wrong), and
the user tests with loose files, so deleting from `output/` removed the very
assets under test.  Packing is the only phase that decides what ships.

The reference set is assembled from every producer of a texture reference,
without re-reading the (multi-GB) output tree:

  * meshes  — nif_converter harvests each mesh's texture paths as it writes it
              (batch_convert's stats['textures_used']), so this costs nothing
  * records — the plugin's own texture fields (EYES/HAIR icons, BOOK, LTEX...),
              read from the export text
  * late assets — speedtree NIFs, LOD .bto/.btr and _far meshes are generated
              after mesh conversion, so they are scanned from disk; there are
              few of them and they are small

Anything under textures/ that no reference names is left out of the archive.
"""

import os
import re
from pathlib import Path

# A texture reference embedded in a binary asset.
_TEX_BYTES_RE = re.compile(rb'[A-Za-z0-9_\\/ .()&+-]{3,200}?\.dds', re.IGNORECASE)
# A texture path in the KEY=VALUE export text.
_TEX_TEXT_RE = re.compile(r'[a-z0-9_\\/ .()&+-]*?\.dds')

# Binary assets that can name a texture and are produced after mesh conversion.
_LATE_ASSET_SUFFIXES = ('.nif', '.bto', '.btr')


# Where mesh conversion leaves the texture set it harvested, for the prune
# phase to pick up later.
MANIFEST_NAME = 'textures_used.txt'


def write_manifest(plugin_dir, refs) -> Path:
    """Record the textures the converted meshes reference."""
    plugin_dir = Path(plugin_dir)
    plugin_dir.mkdir(parents=True, exist_ok=True)
    out = plugin_dir / MANIFEST_NAME
    out.write_text('\n'.join(sorted(refs)), encoding='utf-8')
    return out


def read_manifest(plugin_dir) -> set:
    """Read back the mesh-conversion texture set (empty if it was never run)."""
    f = Path(plugin_dir) / MANIFEST_NAME
    if not f.is_file():
        return set()
    return {ln.strip() for ln in
            f.read_text(encoding='utf-8').splitlines() if ln.strip()}


def _norm(raw) -> str:
    """Normalise a texture reference to a key relative to the textures root."""
    if isinstance(raw, bytes):
        raw = raw.decode('latin-1', errors='replace')
    p = raw.strip().lower().replace('\\', '/')
    while '//' in p:       # the export escapes its backslashes
        p = p.replace('//', '/')
    p = p.lstrip('/')
    if not p.endswith('.dds'):
        return ''
    if p.startswith('data/'):
        p = p[len('data/'):]
    if p.startswith('textures/'):
        p = p[len('textures/'):]
    return p


# Record types whose texture field is relative to a SUBFOLDER of textures\,
# not to the textures root.  The prune has to reproduce whatever the importer
# prepends, or the reference it is holding never matches the shipped path and
# the texture is deleted as unused.
#   LTEX ICON: relative to Textures\Landscape\
#              (record_types/world.py:111 does the same prepend)
_RECORD_TEX_PREFIX = {'ltex': 'landscape/'}

# Map suffixes the engine loads implicitly beside a diffuse. Longest first, so
# `_msn` and `_em` are recognised before `_n`/`_m` swallow their tail.
_MAP_SUFFIXES = ('_msn', '_em', '_sk', '_n', '_g', '_m', '_s', '_e', '_p')


def refs_from_records(export_dir) -> set:
    """Texture paths named by the plugin's records (icons, LTEX, ...)."""
    refs = set()
    for txt in Path(export_dir).glob('*.txt'):
        # The filename IS the record signature, which is the only way to know
        # what the paths inside are relative to.
        prefix = _RECORD_TEX_PREFIX.get(txt.stem.lower(), '')
        body = txt.read_text(encoding='utf-8', errors='replace').lower()
        # `_TEX_TEXT_RE` opens with a LAZY star, so on text containing no match
        # it still expands at every position — quadratic. Most of the export is
        # exactly that: LAND.txt (386 MB) and REFR.txt (166 MB) hold vertex and
        # placement data with ZERO '.dds' in them, yet they are 92% of the bytes
        # scanned. A plain substring test is a C-level memchr and rejects them
        # outright, turning a multi-minute phase into seconds.
        if '.dds' not in body:
            continue
        for m in _TEX_TEXT_RE.finditer(body):
            p = _norm(m.group(0))     # collapses the export's escaped slashes
            if not p:
                continue
            for variant in ({p, prefix + p} if prefix else {p}):
                refs.add(variant)
                # records name the path as Oblivion wrote it; the importer
                # prefixes it with tes4\ on the way into the plugin.
                refs.add('tes4/' + variant)
    return refs


def refs_from_assets(paths) -> set:
    """Texture paths embedded in binary assets (generated meshes, LOD tiles)."""
    refs = set()
    for p in paths:
        try:
            raw = Path(p).read_bytes()
        except OSError:
            continue
        for m in _TEX_BYTES_RE.finditer(raw):
            key = _norm(m.group(0))
            if key:
                refs.add(key)
    return refs


def _companions(refs: set) -> set:
    """Maps the engine loads implicitly beside a referenced diffuse.

    A mesh names its diffuse and normal, but Skyrim's shader also reaches for
    the environment-mask/glow/specular siblings when the shader flags call for
    them, and those are never spelled out in the NIF.  Keeping them costs a few
    MB and avoids stripping a map some shader silently wants.
    """
    extra = set()
    for r in refs:
        stem = r[:-4]
        for suffix in _MAP_SUFFIXES:
            if stem.endswith(suffix):
                continue
            extra.add(stem + suffix + '.dds')
    return extra


def build_refs(plugin_dir, export_dir, mesh_texture_refs=None) -> set:
    """Every texture the shipped plugin can ask for, as textures-root keys."""
    plugin_dir = Path(plugin_dir)

    if mesh_texture_refs is None:
        mesh_texture_refs = read_manifest(plugin_dir)
    if not mesh_texture_refs:
        raise RuntimeError(
            f'no mesh texture manifest in {plugin_dir} — run mesh conversion '
            f'first; pruning without it would delete textures that are in use')

    refs = {_norm(r) for r in mesh_texture_refs}
    refs.discard('')
    refs |= refs_from_records(export_dir)

    # Meshes generated after mesh conversion (speedtrees, _far, LOD/terrain
    # tiles, the grass copies) — no converter harvested these, so read them.
    late = [p for p in (plugin_dir / 'meshes').rglob('*')
            if p.suffix.lower() in _LATE_ASSET_SUFFIXES]
    refs |= refs_from_assets(late)

    refs |= _companions(refs)
    refs |= _shared_maps_on_disk(plugin_dir, refs)
    return refs


def _shared_maps_on_disk(plugin_dir, refs: set) -> set:
    """Map siblings a VARIANT diffuse borrows from its base name.

    Oblivion's convention lets a colour/state variant reuse the base texture's
    maps: `brumawoodpost_grey.dds` is shipped without its own normal map and the
    engine loads `brumawoodpost_n.dds` from the same folder. Nothing writes that
    down — not the NIF, not the record — and `_companions` only derives from the
    FULL name, so it produces `brumawoodpost_grey_n.dds`, which does not exist,
    while the map actually in use is left out. Examples on Nehrim:
    `armor/nehrimsoldier/cuirass_n.dds` (used by `cuirass_b.dds`) and
    `creatures/deer/deer_n.dds` (used by `deer_doe01.dds`).

    So this looks at what is really on disk: a map sibling survives when some
    KEPT diffuse in the same folder starts with its base name. Disk-bounded, so
    it can only ever keep files that exist, and it only ever adds.

    NOT covered here: a diffuse whose own name ends in a map suffix, e.g.
    `characters/imperial/headhuman_m.dds`, where `_m` is the gender marker
    rather than a map. It is classified as a map, so it never enters
    `kept_stems` and can never rescue its own `headhuman_m_n.dds`. That file is
    kept anyway — `_companions` uses `continue`, not `break`, so a stem ending
    in `_m` still gets `_n`/`_g`/`_s`… appended; only `_m` itself is skipped.
    """
    tex_root = Path(plugin_dir) / 'textures'
    if not tex_root.is_dir():
        return set()

    # folder -> (map siblings present, kept diffuse stems)
    maps: dict = {}
    kept_stems: dict = {}
    for f in tex_root.rglob('*.dds'):
        key = f.relative_to(tex_root).as_posix().lower()
        folder, _, name = key.rpartition('/')
        stem = name[:-4]
        suffix = next((s for s in _MAP_SUFFIXES if stem.endswith(s)), None)
        if suffix:
            maps.setdefault(folder, []).append((key, stem[:-len(suffix)]))
        elif key in refs:
            kept_stems.setdefault(folder, set()).add(stem)

    rescued = set()
    for folder, entries in maps.items():
        stems = kept_stems.get(folder)
        if not stems:
            continue
        for key, base in entries:
            if key in refs or not base:
                continue
            if any(s.startswith(base) for s in stems):
                rescued.add(key)
    return rescued
