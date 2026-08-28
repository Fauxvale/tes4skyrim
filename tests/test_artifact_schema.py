"""Versioned pipeline artifacts (tes5_import/artifact_schema.py).

The guard test is `test_schema_matches_frozen_hash`: it fails when an
artifact's required-key set changes without a version bump.  That is the exact
slip that caused the original bug -- commit a4fdb47 added `behavior_hkx` to
creature_projects.json's contract, nothing bumped a version, and every plugin
reading a master's older file died with a bare KeyError.
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tes5_import.artifact_schema import (  # noqa: E402
    ARTIFACTS, StaleArtifactError, read_artifact, write_artifact)


def _schema_hash(art):
    """Stable digest of the contract a version number stands for."""
    return hashlib.sha256(
        json.dumps(sorted(art.required)).encode()).hexdigest()[:16]


# (version, sha256(sorted(required))[:16]) per artifact.  BUMP THE VERSION and
# update the hash together -- never the hash alone, which would silently accept
# a contract change that breaks every file already on disk.
# Literal digests -- deriving them from ARTIFACTS would make this test vacuous.
FROZEN = {
    'creature_projects.json': (1, '1538a9ac96e2b58a'),
    'music_tracks.json': (1, '1a6ecdbb4da40885'),
}


class TestSchemaGuard(unittest.TestCase):
    """(b) -- catch a required-field addition that forgot its version bump."""

    def test_every_artifact_is_frozen(self):
        self.assertEqual(sorted(ARTIFACTS), sorted(FROZEN),
                         'a new artifact must be added to FROZEN too')

    def test_schema_matches_frozen_hash(self):
        for name, (version, digest) in FROZEN.items():
            art = ARTIFACTS[name]
            self.assertEqual(
                art.version, version,
                f'{name}: version moved to {art.version}; update FROZEN')
            self.assertEqual(
                _schema_hash(art), digest,
                f'{name}: required keys changed without a version bump. '
                f'Bump Artifact.version and update FROZEN together, or every '
                f'{name} already on disk breaks with no way to detect it.')


class TestRoundTrip(unittest.TestCase):

    def _path(self, td, name='creature_projects.json'):
        return os.path.join(td, name)

    def _good(self):
        return {'boar': {'behavior_hkx': 'Actors\\TES4\\boar\\Behaviors\\b.hkx',
                         'body_dir': 'Actors\\TES4\\boar',
                         'skeleton_nif': 'actors\\boar\\skeleton.nif',
                         'project_hkx': 'Actors\\TES4\\boar\\p.hkx',
                         'bodies': ['boar.nif']}}

    def test_write_then_read_returns_the_data(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._path(td)
            write_artifact(p, 'Oblivion.esm', self._good())
            self.assertEqual(read_artifact(p), self._good())

    def test_envelope_carries_plugin_and_stage(self):
        """The two fields that make the error actionable."""
        with tempfile.TemporaryDirectory() as td:
            p = self._path(td)
            write_artifact(p, 'Oblivion.esm', self._good())
            with open(p, encoding='utf-8') as f:
                payload = json.load(f)
            self.assertEqual(payload['plugin'], 'Oblivion.esm')
            self.assertEqual(payload['stage'], '--creatures-only')
            self.assertEqual(payload['version'], 1)


class TestStaleDetection(unittest.TestCase):

    def _write_raw(self, td, payload, name='creature_projects.json'):
        p = os.path.join(td, name)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        return p

    def test_v0_bare_dict_is_rejected(self):
        """Every file written before the envelope existed reads as v0."""
        with tempfile.TemporaryDirectory() as td:
            p = self._write_raw(td, {'boar': {'project_hkx': 'x'}})
            with self.assertRaises(StaleArtifactError) as cm:
                read_artifact(p, 'Oblivion.esm')
            self.assertIn('v0', str(cm.exception))

    def test_v0_error_names_the_hinted_plugin_and_command(self):
        """The reported bug: the stale file belongs to the MASTER."""
        with tempfile.TemporaryDirectory() as td:
            p = self._write_raw(td, {'boar': {'project_hkx': 'x'}})
            with self.assertRaises(StaleArtifactError) as cm:
                read_artifact(p, 'Oblivion.esm')
            msg = str(cm.exception)
            self.assertIn(
                'python convert.py -f Oblivion.esm --creatures-only', msg)

    def test_older_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write_raw(td, {'version': 0, 'plugin': 'Nehrim.esm',
                                     'stage': '--creatures-only', 'data': {}})
            with self.assertRaises(StaleArtifactError):
                read_artifact(p)

    def test_missing_required_key_at_current_version_is_rejected(self):
        """(a) -- the backstop for a required field added without a bump.

        This is exactly the a4fdb47 shape: right version, missing field.
        """
        with tempfile.TemporaryDirectory() as td:
            entry = {'body_dir': 'd', 'skeleton_nif': 's',
                     'project_hkx': 'p', 'bodies': ['b.nif']}
            p = self._write_raw(td, {'version': 1, 'plugin': 'Oblivion.esm',
                                     'stage': '--creatures-only',
                                     'data': {'boar': entry}})
            with self.assertRaises(StaleArtifactError) as cm:
                read_artifact(p)
            self.assertIn('behavior_hkx', str(cm.exception))

    def test_music_manifest_checks_top_level_keys(self):
        """per_entry=False: required keys apply to `data` itself."""
        with tempfile.TemporaryDirectory() as td:
            p = self._write_raw(td, {'version': 1, 'plugin': 'Oblivion.esm',
                                     'stage': '--sounds-only',
                                     'data': {'tracks': []}},
                                name='music_tracks.json')
            with self.assertRaises(StaleArtifactError) as cm:
                read_artifact(p)
            self.assertIn('plugin', str(cm.exception))


class TestEveryConsumerReadsTheEnvelope(unittest.TestCase):
    """Each registered artifact has more than one reader.

    music_tracks.json is read by the IMPORTER (record_types/music.py) and,
    separately, by the SCRIPT converter (script_convert/pipeline.py) to bind
    StreamMusic properties.  The script one was missed when the envelope went
    in: it kept doing a bare json.loads and `data.get('tracks')` then returned
    {} against the new shape, silently stripping the MUSC binding off every
    StreamMusic property rather than failing.  Pin both.
    """

    def test_importer_reads_music_manifest(self):
        from tes5_import.record_types.music import load_music_manifest
        with tempfile.TemporaryDirectory() as td:
            write_artifact(os.path.join(td, 'music_tracks.json'),
                           'Oblivion.esm',
                           {'plugin': 'Oblivion.esm',
                            'tracks': [{'source_rel': 'music/dungeon/a.mp3',
                                        'category': 'dungeon'}]})
            got = load_music_manifest(td)
            self.assertEqual(len(got['tracks']), 1)
            self.assertEqual(got['plugin'], 'Oblivion.esm')

    def test_script_converter_reads_music_manifest(self):
        from script_convert.pipeline import _load_music_cues
        with tempfile.TemporaryDirectory() as td:
            write_artifact(os.path.join(td, 'music_tracks.json'),
                           'Oblivion.esm',
                           {'plugin': 'Oblivion.esm',
                            'tracks': [{'source_rel': 'music/dungeon/a.mp3',
                                        'category': 'dungeon'}]})
            cues = _load_music_cues(td)
            self.assertTrue(cues, 'StreamMusic cues lost against the envelope')
            self.assertIn('music/dungeon/a.mp3', cues)

    def test_script_converter_still_tolerates_a_missing_manifest(self):
        """No music converted is the normal state for a plugin with masters."""
        from script_convert.pipeline import _load_music_cues
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(_load_music_cues(td), {})


class TestPreflight(unittest.TestCase):
    """Fail at the TOP of the import, not 15 phase-0 steps in.

    build_creature_races runs deep in phase 0 (after the master load, fid
    maps, cross-ref graph and script plans), so relying on the consumer alone
    made a stale file cost a minute of work before saying anything.
    """

    def _plugin(self, td, name, master=None, projects=None):
        d = os.path.join(td, name)
        os.makedirs(d, exist_ok=True)
        if master:
            with open(os.path.join(d, '_HEADER.txt'), 'w',
                      encoding='utf-8') as f:
                f.write('Master[0]=%s' % master + chr(10))
        if projects is not None:
            with open(os.path.join(d, 'creature_projects.json'), 'w',
                      encoding='utf-8') as f:
                json.dump(projects, f)      # bare == v0
        return d

    def _patch_layout(self, td):
        import tes5_import.overrides as ov
        self._saved = (ov._export_root, ov._master_export_dir)
        ov._export_root = lambda d: td
        ov._master_export_dir = lambda root, name: os.path.join(root, name)
        self.addCleanup(self._restore)

    def _restore(self):
        import tes5_import.overrides as ov
        ov._export_root, ov._master_export_dir = self._saved

    def test_missing_artifacts_are_not_an_error(self):
        """Plenty of plugins ship no creatures and no music."""
        from tes5_import.artifact_schema import preflight_artifacts
        with tempfile.TemporaryDirectory() as td:
            self._patch_layout(td)
            d = self._plugin(td, 'Bare.esp')
            preflight_artifacts(d, None)      # must not raise

    def test_own_stale_artifact_names_this_plugin(self):
        from tes5_import.artifact_schema import preflight_artifacts
        with tempfile.TemporaryDirectory() as td:
            self._patch_layout(td)
            d = self._plugin(td, 'Nehrim.esm',
                             projects={'rat': {'project_hkx': 'x'}})
            with self.assertRaises(StaleArtifactError) as cm:
                preflight_artifacts(d, None)
            self.assertIn('-f Nehrim.esm --creatures-only', str(cm.exception))

    def test_stale_master_artifact_names_the_MASTER(self):
        """The reported bug: DLCFrostcrag died over Oblivion.esm's file."""
        from tes5_import.artifact_schema import preflight_artifacts
        with tempfile.TemporaryDirectory() as td:
            self._patch_layout(td)
            self._plugin(td, 'Oblivion.esm',
                         projects={'rat': {'project_hkx': 'x'}})
            child = self._plugin(td, 'DLCFrostcrag.esp', master='Oblivion.esm')
            with self.assertRaises(StaleArtifactError) as cm:
                preflight_artifacts(child, None)
            msg = str(cm.exception)
            self.assertIn('-f Oblivion.esm --creatures-only', msg)
            self.assertNotIn('-f DLCFrostcrag.esp', msg)

    def test_stale_music_manifest_in_the_output_root(self):
        from tes5_import.artifact_schema import preflight_artifacts
        with tempfile.TemporaryDirectory() as td:
            self._patch_layout(td)
            d = self._plugin(td, 'Oblivion.esm')
            out = os.path.join(td, 'out'); os.makedirs(out)
            with open(os.path.join(out, 'music_tracks.json'), 'w',
                      encoding='utf-8') as f:
                json.dump({'plugin': 'Oblivion.esm', 'tracks': []}, f)
            with self.assertRaises(StaleArtifactError) as cm:
                preflight_artifacts(d, out)
            self.assertIn('--sounds-only', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
