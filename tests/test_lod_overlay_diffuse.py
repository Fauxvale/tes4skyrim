"""Detail-overlay diffuses must be opaque in object LOD, and only those.

Oblivion's APPLY_HILIGHT2 apply mode makes a diffuse's alpha channel a per-texel
BLEND WEIGHT for laying detail over a surface, not a transparency mask.  Nothing
in the full-size mesh samples that channel as opacity, so these render correctly
up close even though the alpha is still there -- and most of them carry no
NiAlphaProperty at all, so nothing in the converted mesh records the fact.

LODGen stamps every shape it bakes with `slsf_2_lod_objects`, and the LOD object
shader DOES read diffuse alpha as opacity.  The rock then renders see-through at
distance while looking correct up close (RockGreatForest645).

The discrimination is AUTHORED, never inferred from pixels: mesh conversion
records the diffuse of every shape whose NiTexturingProperty said APPLY_HILIGHT2
(4), and the LOD stage flattens exactly those into a LOD-local copy.  A genuine
cutout mask -- tree billboards, cobwebs, hanging moss -- ships APPLY_MODULATE (2)
and must come through untouched, because its alpha is the entire silhouette.
"""


import numpy as np
import pytest

from asset_convert import lod_gen, texture_prune

PIL = pytest.importorskip('PIL.Image')
from PIL import Image  # noqa: E402


OVERLAY = 'tes4/rocks/greatforestrock03.dds'
MASK = 'tes4/trees/billboards/treedeodar.dds'


def _dds(path, alpha):
    """Write a tiny uncompressed RGBA DDS whose alpha channel is `alpha`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    size = alpha.shape[0]
    arr = np.zeros((size, size, 4), np.uint8)
    arr[..., :3] = 128
    arr[..., 3] = alpha
    Image.fromarray(arr, 'RGBA').save(path, format='DDS')


def _fake_bto(path, texture_rels):
    """A file the .bto texture scanner finds `texture_rels` in.

    _bto_texture_refs regex-scans raw bytes for '*.dds', so the tile does not
    have to be a valid NIF for this test -- only to contain the strings.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = b''.join(
        b'\x00' + ('data\\textures\\' + r.replace('/', '\\')).encode('latin-1')
        for r in texture_rels)
    path.write_bytes(blob)


@pytest.fixture
def tree(tmp_path):
    """A source texture tree, an empty LOD tree, a tile, and a manifest dir."""
    src = tmp_path / 'plugin' / 'textures'
    lod = tmp_path / 'lod' / 'textures'
    bto = tmp_path / 'lod' / 'Objects'
    exp = tmp_path / 'export' / 'Plugin.esm'
    lod.mkdir(parents=True)
    exp.mkdir(parents=True)

    # A blend weight: smooth midtones, nothing at either extreme.
    _dds(src / OVERLAY.replace('/', '\\'),
         np.full((8, 8), 102, np.uint8))
    # A cutout mask: alpha IS the silhouette.
    mask = np.zeros((8, 8), np.uint8)
    mask[2:6, 2:6] = 255
    _dds(src / MASK.replace('/', '\\'), mask)

    _fake_bto(bto / 'tile.4.0.0.bto', [OVERLAY, MASK])
    return src, lod, bto, exp


def _run(tree, manifest):
    src, lod, bto, exp = tree
    texture_prune.write_manifest(exp, manifest,
                                 texture_prune.OVERLAY_MANIFEST_NAME)
    lod_gen._force_opaque_lod_diffuses(bto, lod, [src], [exp])
    return lod


def _alpha(path):
    return np.asarray(Image.open(path).convert('RGBA'))[..., 3]


class TestOverlayDiffusesForcedOpaque:
    def test_manifest_texture_is_shipped_fully_opaque(self, tree):
        lod = _run(tree, [OVERLAY])
        out = lod / OVERLAY.replace('/', '\\')
        assert out.is_file(), 'overlay diffuse was not shadowed into LOD'
        assert _alpha(out).min() == 255

    def test_source_texture_is_not_modified(self, tree):
        src = tree[0]
        before = _alpha(src / OVERLAY.replace('/', '\\')).copy()
        _run(tree, [OVERLAY])
        after = _alpha(src / OVERLAY.replace('/', '\\'))
        # The full-size mesh still needs that alpha as a blend weight.
        assert np.array_equal(before, after)

    def test_cutout_mask_is_left_alone(self, tree):
        """A billboard is alpha-bearing and IS in the tile -- but not authored
        APPLY_HILIGHT2, so it must not be flattened into a solid rectangle."""
        lod = _run(tree, [OVERLAY])
        assert not (lod / MASK.replace('/', '\\')).exists()

    def test_nothing_happens_without_a_manifest(self, tree):
        lod = _run(tree, [])
        assert not list(lod.rglob('*.dds'))

    def test_texture_absent_from_the_tiles_is_not_copied(self, tree):
        lod = _run(tree, [OVERLAY, 'tes4/rocks/not_in_any_tile.dds'])
        assert not (lod / 'tes4' / 'rocks' / 'not_in_any_tile.dds').exists()

    def test_existing_lod_copy_is_not_overwritten(self, tree):
        """An atlas, or a previous run, already owns that path."""
        src, lod, bto, exp = tree
        dest = lod / OVERLAY.replace('/', '\\')
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b'sentinel')
        _run(tree, [OVERLAY])
        assert dest.read_bytes() == b'sentinel'


class TestOverlayManifest:
    def test_round_trips_independently_of_the_texture_manifest(self, tmp_path):
        texture_prune.write_manifest(tmp_path, {'a.dds', 'b.dds'})
        texture_prune.write_manifest(tmp_path, {OVERLAY},
                                     texture_prune.OVERLAY_MANIFEST_NAME)
        assert texture_prune.read_manifest(tmp_path) == {'a.dds', 'b.dds'}
        assert texture_prune.read_manifest(
            tmp_path, texture_prune.OVERLAY_MANIFEST_NAME) == {OVERLAY}

    def test_missing_manifest_reads_empty(self, tmp_path):
        assert texture_prune.read_manifest(
            tmp_path, texture_prune.OVERLAY_MANIFEST_NAME) == set()
