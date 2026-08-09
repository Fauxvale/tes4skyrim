"""Shared navmesh geometry cache: hashing, gating and the publish contract.

The cache is shipped as a GitHub Release asset (tools/navmesh_cache.py), so
three properties have to hold or downloaders silently lose the benefit -- or
worse, get stale geometry:

  * the cache tag must be MACHINE-INDEPENDENT (no mtime, no absolute paths),
  * one changed mesh must invalidate only the cells that place it,
  * the pre-push gate must fire for every file that can change cached geometry.
"""
import glob
import hashlib
import json
import os
import pickle
import sys
import zipfile

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asset_convert.collision_extract as ce  # noqa: E402
from tes5_import import import_main as im  # noqa: E402
from tes5_import.pgrd_to_navm import _geom_hash  # noqa: E402
from tools import navmesh_cache as nc  # noqa: E402
from tools import navmesh_cache_hook as hook  # noqa: E402


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fake_collision(monkeypatch, table):
    monkeypatch.setattr(ce, '_COLLISION', table)
    monkeypatch.setattr(ce, '_DIGESTS', {})


def _soup(seed, n=4):
    rng = np.arange(n * 9, dtype=np.float32) + float(seed)
    return {'w': rng.copy(), 'b': (rng * 2).copy()}


def _refr(fid):
    return {'NAME': '%06X' % fid, 'PosX': '1', 'PosY': '2', 'PosZ': '3',
            'RotX': '0', 'RotY': '0', 'RotZ': '0'}


HASH_ARGS = dict(tag='T', points=[(0, 0)], edges=[], doors=[], land_rec=None,
                 origin_x=0, origin_y=0)


# ---------------------------------------------------------------------------
# Tag stability
# ---------------------------------------------------------------------------

def test_tag_ignores_collision_mtime(tmp_path, monkeypatch):
    """The tag must not move when only the collision cache's mtime changes.

    It used to hash (size, mtime).  mtime is machine-local and survives neither
    git nor a zip round-trip, so every downloader computed a different tag and
    a published cache would have missed 100% of the time.
    """
    col = tmp_path / 'collision_cache.bin'
    col.write_bytes(b'collision-payload')
    first = im._navmesh_geom_cache(str(col))
    assert first is not None
    st = os.stat(col)
    os.utime(col, (st.st_atime, st.st_mtime + 3600))
    assert im._navmesh_geom_cache(str(col))[1] == first[1]


def test_tag_tracks_navmesh_sources(tmp_path):
    """Editing a navmesh source must change the tag (self-invalidation)."""
    col = tmp_path / 'collision_cache.bin'
    col.write_bytes(b'x')
    before = im._navmesh_geom_cache(str(col))[1]
    src = os.path.join(REPO, 'tes5_import', 'navmesh', 'params.py')
    original = open(src, 'rb').read()
    try:
        with open(src, 'ab') as fh:
            fh.write(b'\n# cache-tag probe\n')
        assert im._navmesh_geom_cache(str(col))[1] != before
    finally:
        with open(src, 'wb') as fh:
            fh.write(original)
    assert im._navmesh_geom_cache(str(col))[1] == before


# ---------------------------------------------------------------------------
# Per-mesh invalidation
# ---------------------------------------------------------------------------

def test_one_changed_mesh_spares_other_cells(monkeypatch):
    """A replaced mesh must invalidate ONLY the cells that place it.

    With the whole-file collision hash this was false: swapping one mesh
    invalidated all ~8,200 Oblivion entries and forced a full regeneration.
    """
    _fake_collision(monkeypatch, {'a.nif': _soup(1), 'b.nif': _soup(2)})
    models = {0x111111: 'a.nif', 0x222222: 'b.nif'}
    cell_a = [_refr(0x111111)]
    cell_b = [_refr(0x222222)]

    a1 = _geom_hash(refr_recs=cell_a, base_model_by_fid=models, **HASH_ARGS)
    b1 = _geom_hash(refr_recs=cell_b, base_model_by_fid=models, **HASH_ARGS)

    _fake_collision(monkeypatch, {'a.nif': _soup(99), 'b.nif': _soup(2)})
    a2 = _geom_hash(refr_recs=cell_a, base_model_by_fid=models, **HASH_ARGS)
    b2 = _geom_hash(refr_recs=cell_b, base_model_by_fid=models, **HASH_ARGS)

    assert a1 != a2, 'cell placing the changed mesh must miss'
    assert b1 == b2, 'cell placing only unchanged meshes must still hit'


def test_geom_hash_is_refr_order_independent_for_collision(monkeypatch):
    """Collision digests are folded in sorted, so REFR order cannot perturb them.

    REFR order still contributes through the per-REFR lines above (position
    matters); this pins the *collision* contribution specifically.
    """
    _fake_collision(monkeypatch, {'a.nif': _soup(1), 'b.nif': _soup(2)})
    models = {0x111111: 'a.nif', 0x222222: 'b.nif'}
    refrs = [_refr(0x111111), _refr(0x222222)]
    h1 = _geom_hash(refr_recs=refrs, base_model_by_fid=models, **HASH_ARGS)
    _fake_collision(monkeypatch, {'b.nif': _soup(2), 'a.nif': _soup(1)})
    h2 = _geom_hash(refr_recs=refrs, base_model_by_fid=models, **HASH_ARGS)
    assert h1 == h2


def test_missing_collision_digest_is_empty(monkeypatch):
    """A mesh with no collision entry digests to '' rather than raising."""
    _fake_collision(monkeypatch, {})
    assert ce.collision_digest('nope.nif') == ''


def test_digest_accepts_lists_and_arrays(monkeypatch):
    """The scanners build float LISTS; load_collision builds numpy arrays.

    Both shapes reach collision_digest, so it must accept either and produce
    the SAME digest -- `.tobytes()` on a list raises AttributeError, which
    would crash any import that digested a freshly-scanned table.
    """
    w, b = [1.5] * 9, [2.5] * 9
    _fake_collision(monkeypatch, {'a.nif': {'w': w, 'b': b}})
    as_list = ce.collision_digest('a.nif')
    _fake_collision(monkeypatch, {'a.nif': {'w': np.array(w, np.float32),
                                            'b': np.array(b, np.float32)}})
    assert ce.collision_digest('a.nif') == as_list


def test_digests_cleared_on_reload(tmp_path, monkeypatch):
    """A reload must drop memoised digests, or they describe the OLD cache."""
    _fake_collision(monkeypatch, {'a.nif': _soup(1)})
    first = ce.collision_digest('a.nif')
    path = tmp_path / 'c.bin'
    path.write_bytes(ce._serialize({'a.nif': {'w': list(_soup(9)['w']),
                                              'b': list(_soup(9)['b'])}}))
    ce.load_collision(str(path), quiet=True)
    assert ce.collision_digest('a.nif') != first


def test_content_hash_ignores_key_order(monkeypatch):
    """collision_content_hash certifies CONTENT, not dict/file layout."""
    _fake_collision(monkeypatch, {'a.nif': _soup(1), 'b.nif': _soup(2)})
    h1 = ce.collision_content_hash()
    _fake_collision(monkeypatch, {'b.nif': _soup(2), 'a.nif': _soup(1)})
    assert ce.collision_content_hash() == h1


# ---------------------------------------------------------------------------
# Pre-push gate
# ---------------------------------------------------------------------------

def test_gate_watches_every_tag_source():
    """Every file feeding the tag must be gated, or a push ships a dead cache.

    import_main._navmesh_geom_cache hashes tes5_import/navmesh/*.py plus
    pgrd_to_navm.py; the hook's NAVMESH_PATHS must cover exactly those.
    """
    watched = set(hook.NAVMESH_PATHS)
    assert 'tes5_import/pgrd_to_navm.py' in watched
    assert 'tes5_import/navmesh/' in watched
    # Anything new in the navmesh package is covered by the directory prefix.
    for src in glob.glob(os.path.join(REPO, 'tes5_import', 'navmesh', '*.py')):
        rel = os.path.relpath(src, REPO).replace('\\', '/')
        assert any(rel.startswith(w) for w in watched), rel


def test_gate_covers_cache_defining_modules():
    """import_main and collision_extract change caching without feeding the tag."""
    assert '_navmesh_geom_cache' in hook.NAVMESH_FUNCS['tes5_import/import_main.py']
    assert '_gather_navm_jobs' in hook.NAVMESH_FUNCS['tes5_import/import_main.py']
    assert 'collision_digest' in \
        hook.NAVMESH_FUNCS['asset_convert/collision_extract.py']


def test_gate_ignores_post_cache_stitching():
    """navm_edge_links runs AFTER the cache, so it must not gate a push."""
    assert hook.touches_navmesh(['tes5_import/navm_edge_links.py']) == []


def test_gate_matches_expected_paths():
    assert hook.touches_navmesh(['tes5_import/navmesh/corridor.py'])
    assert hook.touches_navmesh(['tes5_import/pgrd_to_navm.py'])
    assert hook.touches_navmesh(['docs/x.md', 'tools/y.py']) == []


def test_stamp_written_only_by_a_real_build(tmp_path):
    """Computing the tag must NOT certify the cache.

    _navmesh_geom_cache is called by tools that merely want to know the tag; if
    it stamped CACHE_TAG, reading the tag would make a stale cache look freshly
    built and the gate would wave it through.
    """
    col = tmp_path / 'collision_cache.bin'
    col.write_bytes(b'payload')
    geom = im._navmesh_geom_cache(str(col))
    stamp = os.path.join(geom[0], 'CACHE_TAG')
    assert not os.path.exists(stamp), 'reading the tag must not stamp'

    im._stamp_navmesh_cache_tag(geom)
    assert open(stamp).read().strip() == geom[1]


def test_cache_matches_tag_is_exact(tmp_path, monkeypatch):
    """A correct cache passes regardless of mtime; a stale one never does."""
    monkeypatch.setattr(nc, 'repo_root', lambda: str(tmp_path))
    cdir = tmp_path / 'export' / 'Test.esm' / 'navmesh_geom_cache'
    cdir.mkdir(parents=True)

    assert hook.cache_matches_tag('Test.esm', 'TAG') is False   # unstamped
    (cdir / 'CACHE_TAG').write_text('TAG')
    assert hook.cache_matches_tag('Test.esm', 'TAG') is True
    # An old mtime must not matter -- a checkout or unzip rewrites mtimes, and
    # rejecting on that would cry wolf on a perfectly valid cache.
    os.utime(cdir / 'CACHE_TAG', (0, 0))
    assert hook.cache_matches_tag('Test.esm', 'TAG') is True
    assert hook.cache_matches_tag('Test.esm', 'OTHER') is False


def test_next_tag_format(monkeypatch):
    """The manifest's starting tag must match tag-on-push.yml's MAJOR.MM."""
    monkeypatch.setattr(hook.subprocess, 'run', lambda *a, **k: None)
    monkeypatch.setattr(hook, 'git',
                        lambda *a: '0.54\n0.55\nnot-a-tag' if a[0] == 'tag' else '')
    assert hook.next_tag() == '0.56'


def test_next_tag_skips_names_already_taken(monkeypatch):
    """Must not predict a tag the remote already has.

    tag-on-push.yml fetches tags and then advances past any name already in
    use.  Reading only local refs predicted 0.56 while the remote already had
    it, which would have labelled the cache one version behind the code CI
    actually tagged (0.57).
    """
    monkeypatch.setattr(hook.subprocess, 'run', lambda *a, **k: None)
    monkeypatch.setattr(hook, 'git',
                        lambda *a: '0.54\n0.55\n0.56' if a[0] == 'tag' else '')
    assert hook.next_tag() == '0.57'


def test_next_tag_fetches_before_computing(monkeypatch):
    """A stale clone must not decide the version on its own."""
    ran = []
    monkeypatch.setattr(hook.subprocess, 'run',
                        lambda a, **k: ran.append(a))
    monkeypatch.setattr(hook, 'git', lambda *a: '0.55' if a[0] == 'tag' else '')
    hook.next_tag()
    assert any('fetch' in c and '--tags' in c for c in ran), \
        'next_tag must fetch remote tags first'


# ---------------------------------------------------------------------------
# Publish contract
# ---------------------------------------------------------------------------

def test_archive_excludes_collision_and_big_indexes(tmp_path, monkeypatch):
    """Only navmesh_geom_cache/*.pkl ships.

    collision_cache.bin is keyed-by-name Bethesda collision geometry and must
    never be redistributed; navmesh_index.pkl / audit_index3.pkl are ~2.1 GB
    each.  A glob over export/**/*.pkl would sweep in the latter, so the
    archiver names the cache directory explicitly.
    """
    monkeypatch.setattr(nc, 'repo_root', lambda: str(tmp_path))
    exp = tmp_path / 'export' / 'Test.esm'
    (exp / 'navmesh_geom_cache').mkdir(parents=True)
    (exp / 'collision_cache.bin').write_bytes(b'SECRET-COLLISION')
    (exp / 'navmesh_index.pkl').write_bytes(b'huge')
    for i in range(3):
        with open(exp / 'navmesh_geom_cache' / ('%08X_%08X.pkl' % (i, i)), 'wb') as fh:
            pickle.dump({'hash': 'h%d' % i,
                         'verts': np.zeros((3, 3), np.float32),
                         'tris': np.zeros((1, 3), np.int32),
                         'ledges': []}, fh)

    monkeypatch.setattr(nc, 'source_tag', lambda p: 'tag123')
    monkeypatch.setattr(nc, 'collision_hash', lambda p: 'col456')
    zpath = nc.archive('Test.esm', str(tmp_path / 'out'), '0.56', quiet=True)
    assert zpath is not None

    with zipfile.ZipFile(zpath) as zf:
        names = zf.namelist()
        assert sorted(n for n in names if n.endswith('.pkl')) == [
            '00000000_00000000.pkl', '00000001_00000001.pkl',
            '00000002_00000002.pkl']
        assert not any('collision' in n for n in names)
        assert 'navmesh_index.pkl' not in names
        blob = b''.join(zf.read(n) for n in names)
        assert b'SECRET-COLLISION' not in blob
        manifest = json.loads(zf.read(nc.MANIFEST_NAME))

    assert manifest['source_tag'] == 'tag123'
    assert manifest['starting_tag'] == '0.56'
    assert manifest['entries'] == 3


def test_archive_refuses_corrupt_cache(tmp_path, monkeypatch):
    """A truncated entry must block the publish, not ship silently."""
    monkeypatch.setattr(nc, 'repo_root', lambda: str(tmp_path))
    cdir = tmp_path / 'export' / 'Test.esm' / 'navmesh_geom_cache'
    cdir.mkdir(parents=True)
    (cdir / 'bad.pkl').write_bytes(b'not a pickle')
    monkeypatch.setattr(nc, 'source_tag', lambda p: 'tag')
    monkeypatch.setattr(nc, 'collision_hash', lambda p: 'col')
    assert nc.archive('Test.esm', str(tmp_path / 'out'), '0.56', quiet=True) is None


def test_install_refuses_mismatched_manifest(tmp_path, monkeypatch):
    """A cache from different navmesh code must not install without --force."""
    monkeypatch.setattr(nc, 'repo_root', lambda: str(tmp_path))
    (tmp_path / 'export' / 'Test.esm').mkdir(parents=True)
    zpath = tmp_path / 'c.zip'
    with zipfile.ZipFile(zpath, 'w') as zf:
        zf.writestr(nc.MANIFEST_NAME, json.dumps(
            {'plugin': 'Test.esm', 'starting_tag': '0.56',
             'source_tag': 'OLD', 'collision_hash': 'OLD'}))
    monkeypatch.setattr(nc, 'source_tag', lambda p: 'NEW')
    monkeypatch.setattr(nc, 'collision_hash', lambda p: 'NEW')
    assert nc.install('Test.esm', None, str(zpath)) == 1
    assert nc.install('Test.esm', None, str(zpath), force=True) == 0


def test_install_certifies_only_a_matching_cache(tmp_path, monkeypatch):
    """CACHE_TAG is written on a clean install and withheld under --force.

    A downloaded cache that matches must be certified, or the next verify calls
    a perfectly good download stale.  A --force install of a KNOWN-mismatched
    archive must not be certified, or the stamp would vouch for a cache the
    user was just warned about.
    """
    monkeypatch.setattr(nc, 'repo_root', lambda: str(tmp_path))
    (tmp_path / 'export' / 'Test.esm').mkdir(parents=True)
    zpath = tmp_path / 'c.zip'
    with zipfile.ZipFile(zpath, 'w') as zf:
        zf.writestr(nc.MANIFEST_NAME, json.dumps(
            {'plugin': 'Test.esm', 'starting_tag': '0.56',
             'source_tag': 'MATCHING', 'collision_hash': 'C'}))
        zf.writestr('CACHE_TAG', 'MATCHING')
        zf.writestr('a.pkl', b'x')
    stamp = tmp_path / 'export' / 'Test.esm' / 'navmesh_geom_cache' / 'CACHE_TAG'

    monkeypatch.setattr(nc, 'source_tag', lambda p: 'MATCHING')
    monkeypatch.setattr(nc, 'collision_hash', lambda p: 'C')
    assert nc.install('Test.esm', None, str(zpath)) == 0
    assert stamp.read_text() == 'MATCHING'

    # Now the local tree moves on: the same archive is stale, and --force must
    # clear the inherited stamp rather than leave it vouching for the cache.
    monkeypatch.setattr(nc, 'source_tag', lambda p: 'MOVED_ON')
    assert nc.install('Test.esm', None, str(zpath), force=True) == 0
    assert not stamp.exists()


def test_install_rejects_path_traversal(tmp_path, monkeypatch):
    """A crafted archive must not write outside the cache directory."""
    monkeypatch.setattr(nc, 'repo_root', lambda: str(tmp_path))
    (tmp_path / 'export' / 'Test.esm').mkdir(parents=True)
    zpath = tmp_path / 'evil.zip'
    with zipfile.ZipFile(zpath, 'w') as zf:
        zf.writestr(nc.MANIFEST_NAME, json.dumps(
            {'plugin': 'Test.esm', 'starting_tag': '0.56',
             'source_tag': 'T', 'collision_hash': 'C'}))
        zf.writestr('../../../evil.pkl', b'x')
    monkeypatch.setattr(nc, 'source_tag', lambda p: 'T')
    monkeypatch.setattr(nc, 'collision_hash', lambda p: 'C')
    assert nc.install('Test.esm', None, str(zpath)) == 1
    assert not (tmp_path.parent / 'evil.pkl').exists()


def test_install_requires_manifest(tmp_path, monkeypatch):
    """An archive with no manifest is unidentified and must be refused."""
    monkeypatch.setattr(nc, 'repo_root', lambda: str(tmp_path))
    (tmp_path / 'export' / 'Test.esm').mkdir(parents=True)
    zpath = tmp_path / 'nomanifest.zip'
    with zipfile.ZipFile(zpath, 'w') as zf:
        zf.writestr('a.pkl', b'x')
    assert nc.install('Test.esm', None, str(zpath)) == 1


def test_cache_release_never_shadows_the_code_tag():
    """A cache release must not be mistakable for the converter download.

    The repo ships code as annotated TAGS (tag-on-push.yml), and GitHub renders
    a real Release above a plain tag on /releases.  A release named '0.56'
    would therefore sit at the top looking like THE 0.56 download while holding
    only a build cache.
    """
    assert nc.cache_release_tag('0.56') == 'navmesh-cache-0.56+'
    assert nc.cache_release_tag('0.56') != '0.56'

    notes = nc.cache_release_notes('0.56')
    # Must say what it is not, and point at the tag list.
    assert 'not the converter' in notes.lower()
    assert '/tags' in notes
    assert '0.56' in notes
    # Release notes go through gh's argv; non-ASCII has bitten this repo
    # before (6b443f0), so keep the body plain.
    assert all(ord(c) < 128 for c in notes)


def test_latest_cache_release_sorts_numerically(monkeypatch):
    """0.10 must beat 0.9, and non-cache releases must be ignored."""
    class _R:
        returncode = 0
        stdout = '\n'.join(('navmesh-cache-0.9+', 'navmesh-cache-0.10+',
                            '0.55', 'some-other-release'))

    monkeypatch.setattr(nc.subprocess, 'run', lambda *a, **k: _R())
    assert nc.latest_cache_release() == 'navmesh-cache-0.10+'


def test_latest_cache_release_none_when_absent(monkeypatch):
    class _R:
        returncode = 0
        stdout = '0.55\n0.54\n'

    monkeypatch.setattr(nc.subprocess, 'run', lambda *a, **k: _R())
    assert nc.latest_cache_release() is None


def test_release_name_states_the_version_range():
    """The name must say which versions the cache covers.

    Open-ended at publish time (the upper bound is not knowable yet -- nobody
    knows whether 0.56's cache survives to 0.57 or 0.72), closed later by
    close_cache_release when a navmesh change actually invalidates it.
    """
    assert nc.cache_release_tag('0.56') == 'navmesh-cache-0.56+'
    assert nc.cache_release_tag('0.56', '0.72') == 'navmesh-cache-0.56-0.72'
    assert nc.parse_cache_release_tag('navmesh-cache-0.56+') == ('0.56', None)
    assert nc.parse_cache_release_tag(
        'navmesh-cache-0.56-0.72') == ('0.56', '0.72')
    assert nc.parse_cache_release_tag('0.55') is None


def test_previous_tag_matches_tag_on_push_numbering():
    """A closed range ends on the last version the cache was VALID for."""
    assert nc.previous_tag('0.73') == '0.72'
    assert nc.previous_tag('1.00') == '0.99'   # MAJOR.MM rollover
    assert nc.previous_tag('0.57') == '0.56'


def _mock_releases(monkeypatch, listing):
    seen = []

    class _R:
        returncode = 0
        stdout = listing

    def _run(args, **kw):
        seen.append(args)
        return _R()

    monkeypatch.setattr(nc.subprocess, 'run', _run)
    return seen


def test_resolve_cache_release_matches_the_range_not_the_name(monkeypatch):
    """`--tag 0.60` must find the release COVERING 0.60.

    Constructing 'navmesh-cache-0.60+' would 404: the cache serving 0.60 is
    published as '0.56+' and later renamed '0.56-0.72'.
    """
    _mock_releases(monkeypatch,
                   'navmesh-cache-0.56-0.72\nnavmesh-cache-0.73+\n0.75\n')
    assert nc.resolve_cache_release('0.56') == 'navmesh-cache-0.56-0.72'
    assert nc.resolve_cache_release('0.60') == 'navmesh-cache-0.56-0.72'
    assert nc.resolve_cache_release('0.72') == 'navmesh-cache-0.56-0.72'
    assert nc.resolve_cache_release('0.73') == 'navmesh-cache-0.73+'
    assert nc.resolve_cache_release('0.90') == 'navmesh-cache-0.73+'
    # Older than every published cache -> nothing covers it.
    assert nc.resolve_cache_release('0.50') is None


def test_close_cache_release_renames_open_range(monkeypatch):
    seen = _mock_releases(monkeypatch, 'navmesh-cache-0.56+\n0.55\n')
    assert nc.close_cache_release('0.73') == 'navmesh-cache-0.56-0.72'
    edit = [c for c in seen if 'edit' in c]
    assert edit and 'navmesh-cache-0.56-0.72' in edit[0]


def test_close_cache_release_leaves_closed_ranges_alone(monkeypatch):
    """An already-closed range must never be renamed again."""
    _mock_releases(monkeypatch, 'navmesh-cache-0.56-0.72\n')
    assert nc.close_cache_release('0.73') is None


def test_close_cache_release_ignores_same_or_older(monkeypatch):
    """Republishing the same version must not close its own range."""
    _mock_releases(monkeypatch, 'navmesh-cache-0.56+\n')
    assert nc.close_cache_release('0.56') is None
    assert nc.close_cache_release('0.50') is None


def test_have_gh_requires_auth_not_just_presence(monkeypatch):
    """An installed-but-logged-out gh must not count as usable.

    Otherwise publish builds every archive and then dies inside
    `gh release create` with an opaque error, having already spent the time.
    """
    monkeypatch.setattr(nc.shutil, 'which', lambda _n: None)
    assert nc.have_gh() is False

    monkeypatch.setattr(nc.shutil, 'which', lambda _n: 'C:/gh.exe')

    class _R:
        def __init__(self, rc):
            self.returncode = rc

    monkeypatch.setattr(nc.subprocess, 'run', lambda *a, **k: _R(1))
    assert nc.have_gh() is False, 'logged-out gh must not count as available'
    monkeypatch.setattr(nc.subprocess, 'run', lambda *a, **k: _R(0))
    assert nc.have_gh() is True


def test_asset_name_is_stable():
    assert nc.asset_name('Oblivion.esm') == 'navmesh-cache-Oblivion.zip'
    assert nc.asset_name('Morrowind_ob.esm') == 'navmesh-cache-Morrowind_ob.zip'
    # Spaces would break `gh release download --pattern`.
    assert ' ' not in nc.asset_name('Morrowind_ob - Chargen and Transport Mod.esp')
