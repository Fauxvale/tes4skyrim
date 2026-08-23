"""Tests for the creature animation pipeline:
kf_decode (B-spline decode) → hkx_skeleton (skeleton.hkx) → hkx_anim (clip hkx).

Uses the Oblivion dog as the fixture creature (small, mixed interpolator types,
real root motion). Tests that need source assets or hkxcmd skip cleanly when
they are absent.
"""

import os
import subprocess
import sys

import numpy as np
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from asset_convert.kf_decode import (decode_kf, eval_bspline,  # noqa: E402
                                     split_root_motion, _knots,
                                     _basis_weights)
from asset_convert.hkx_xml import HKXCMD  # noqa: E402

DOG_DIR = os.path.join(REPO, 'export', 'Oblivion.esm', 'meshes',
                       'creatures', 'dog')
DOG_SKEL = os.path.join(DOG_DIR, 'skeleton.nif')
DOG_FORWARD = os.path.join(DOG_DIR, 'forward.kf')
DOG_IDLE = os.path.join(DOG_DIR, 'idle.kf')

needs_assets = pytest.mark.skipif(
    not os.path.exists(DOG_FORWARD), reason='Oblivion export assets missing')
needs_hkxcmd = pytest.mark.skipif(
    not os.path.exists(HKXCMD), reason='hkxcmd.exe missing')


# ---------------------------------------------------------------------------
# B-spline evaluation math
# ---------------------------------------------------------------------------

class TestBSplineEval:
    def test_endpoints_interpolated(self):
        ctrl = np.array([[0.0, 0], [1, 5], [2, -3], [3, 1], [4, 4]])
        v = np.array([0.0, float(len(ctrl) - 3)])
        out = eval_bspline(ctrl, v)
        assert np.allclose(out[0], ctrl[0])
        assert np.allclose(out[-1], ctrl[-1])

    def test_partition_of_unity(self):
        n = 9
        u = _knots(n)
        for v in np.linspace(0.0, n - 3 - 1e-6, 20):
            w = _basis_weights(n, v, u)
            assert abs(w.sum() - 1.0) < 1e-9

    def test_curve_stays_in_convex_hull(self):
        ctrl = np.array([[0.0], [1], [2], [3], [4], [5]])
        out = eval_bspline(ctrl, np.linspace(0, 3, 50))
        assert out.min() >= -1e-9 and out.max() <= 5 + 1e-9

    def test_single_control_point(self):
        ctrl = np.array([[7.0, 8, 9]])
        out = eval_bspline(ctrl, np.array([0.0, 0.5]))
        assert np.allclose(out, [[7, 8, 9], [7, 8, 9]])


# ---------------------------------------------------------------------------
# KF decoding
# ---------------------------------------------------------------------------

@needs_assets
class TestDecodeKf:
    def test_forward_clip(self):
        clip = decode_kf(DOG_FORWARD)[0]
        assert clip.name == 'Forward'
        assert abs(clip.duration - 4.0 / 3.0) < 1e-3
        assert len(clip.tracks) == 45          # all transform tracks decoded
        bones = {t.bone for t in clip.tracks}
        assert 'Bip01' in bones and 'Bip01 Spine0' in bones
        # only float channels skipped
        assert all('FloatInterpolator' in why or 'rest pose' in why
                   for _, why in clip.skipped_blocks)

    def test_quaternions_unit_and_finite(self):
        clip = decode_kf(DOG_FORWARD)[0]
        for tr in clip.tracks:
            if tr.rotations is not None:
                n = np.linalg.norm(tr.rotations, axis=1)
                assert abs(n - 1).max() < 1e-5
                assert np.isfinite(tr.rotations).all()
            if tr.translations is not None:
                assert np.isfinite(tr.translations).all()

    def test_text_keys(self):
        clip = decode_kf(DOG_FORWARD)[0]
        texts = [s.strip() for _, s in clip.text_keys]
        assert texts[0] == 'start' and texts[-1] == 'end'
        assert any(s.startswith('Enum:') for s in texts)

    def test_static_rotation_fallback(self):
        # idle.kf Spine0 B-spline has no rotation channel — must fall back to
        # the interpolator's static transform, not drop the channel
        clip = decode_kf(DOG_IDLE)[0]
        sp0 = next(t for t in clip.tracks if t.bone == 'Bip01 Spine0')
        assert sp0.rotations is not None

    def test_root_motion_split_forward(self):
        clip = decode_kf(DOG_FORWARD)[0]
        motion = split_root_motion(clip)
        assert motion is not None and motion['bone'] == 'Bip01'
        assert np.linalg.norm(motion['translations'][-1]) > 70
        track = next(t for t in clip.tracks if t.bone == 'Bip01')
        assert np.allclose(track.translations, track.translations[0])

    def test_root_motion_split_idle_none(self):
        clip = decode_kf(DOG_IDLE)[0]
        assert split_root_motion(clip) is None


# ---------------------------------------------------------------------------
# Skeleton hkx generation
# ---------------------------------------------------------------------------

@needs_assets
class TestSkeletonHkx:
    def test_bone_collection(self):
        from asset_convert.hkx_skeleton import load_skeleton_bones
        bones = load_skeleton_bones(DOG_SKEL)
        # engine contract: rig root renamed to the name SSE binds by
        # (all 30 vanilla creature rigs use exactly this root bone name)
        assert bones[0].name == 'NPC Root [Root]' and bones[0].parent == -1
        assert len(bones) == 45
        # parent-before-child ordering (required by Havok)
        for i, b in enumerate(bones):
            assert b.parent < i

    def test_quat_matrix_roundtrip(self):
        from asset_convert import pyffi_monkey_patch  # noqa: F401
        from asset_convert.hkx_skeleton import (_mat33_to_quat_xyzw,
                                                find_skeleton_root,
                                                quat_xyzw_to_mat33)
        from pyffi.formats.nif import NifFormat
        data = NifFormat.Data()
        with open(DOG_SKEL, 'rb') as f:
            data.read(f)
        root = find_skeleton_root(data)
        stack = [root]
        while stack:
            nd = stack.pop()
            stack.extend(c for c in nd.children
                         if isinstance(c, NifFormat.NiNode))
            m = nd.rotation
            orig = [[m.m_11, m.m_12, m.m_13], [m.m_21, m.m_22, m.m_23],
                    [m.m_31, m.m_32, m.m_33]]
            rec = quat_xyzw_to_mat33(_mat33_to_quat_xyzw(m))
            assert np.abs(np.array(rec) - np.array(orig)).max() < 1e-5

    @needs_hkxcmd
    def test_generate_and_roundtrip(self, tmp_path):
        from asset_convert.hkx_skeleton import generate_skeleton_hkx
        from asset_convert.hkx_xml import decompile_hkx
        out = str(tmp_path / 'skeleton.hkx')
        bones = generate_skeleton_hkx(DOG_SKEL, out)
        assert os.path.getsize(out) > 1000
        back = str(tmp_path / 'skeleton.xml')
        decompile_hkx(out, back)
        txt = open(back, encoding='ascii', errors='replace').read()
        assert 'hkaSkeleton' in txt
        for b in bones:
            assert b.name in txt


# ---------------------------------------------------------------------------
# Animation hkx generation (full path)
# ---------------------------------------------------------------------------

@needs_assets
@needs_hkxcmd
class TestAnimHkx:
    def test_convert_and_verify(self, tmp_path):
        from asset_convert.hkx_anim import convert_clip_hkx, verify_hkx
        from asset_convert.hkx_skeleton import load_skeleton_bones
        bones = load_skeleton_bones(DOG_SKEL)
        out = str(tmp_path / 'forward.hkx')
        clip, motion = convert_clip_hkx(DOG_FORWARD, bones, out)
        assert motion is not None
        stats = verify_hkx(out, clip, [b.name for b in bones])
        assert stats['tracks'] == len(bones)
        # spline-compressed round trip must be lossless within quantization
        assert stats['max_trans_err'] < 0.01
        assert stats['max_rot_err_deg'] < 0.1

    def test_hkxcmd_can_deserialize(self, tmp_path):
        # the real Havok deserializer (what the engine uses) must accept it
        from asset_convert.hkx_anim import convert_clip_hkx
        from asset_convert.hkx_skeleton import load_skeleton_bones
        bones = load_skeleton_bones(DOG_SKEL)
        out = str(tmp_path / 'forward.hkx')
        convert_clip_hkx(DOG_FORWARD, bones, out)
        res = subprocess.run(
            [HKXCMD, 'convert', '-v:XML', os.path.abspath(out),
             os.path.abspath(str(tmp_path / 'back.xml'))],
            capture_output=True, text=True)
        assert res.returncode == 0

    def test_annotations_carried(self, tmp_path):
        """Annotations must be TRANSLATED Skyrim events, never Oblivion's raw
        text keys — raw `Sound: X`/`Enum: Y` embedded verbatim was the
        confirmed root cause of totally silent creatures (2026-08-07)."""
        from asset_convert.hkx_anim import convert_clip_hkx
        from asset_convert.hkx_skeleton import load_skeleton_bones
        from external.pynifly_hkx.anim_skyrim import load_skyrim_animation
        bones = load_skeleton_bones(DOG_SKEL)
        kf = os.path.join(DOG_DIR, 'handtohandattackleft.kf')
        out = str(tmp_path / 'attack.hkx')
        convert_clip_hkx(kf, bones, out)
        back = load_skyrim_animation(out)
        texts = {a.text for a in back.annotations}
        assert any(t == 'HitFrame' for t in texts)
        assert any(t.startswith('SoundPlay.TES4_') and t.endswith('_SNDR')
                   for t in texts)
        low = {t.lower() for t in texts}
        assert not any(t.startswith(('sound:', 'enum:')) for t in low)

    def test_foot_enums_translated(self, tmp_path):
        """`Enum: Left/BackLeft/...` gait keys become engine footstep events
        at their AUTHORED times (FSTP.ANAM matches these names verbatim)."""
        from asset_convert.hkx_anim import convert_clip_hkx
        from asset_convert.hkx_skeleton import load_skeleton_bones
        from external.pynifly_hkx.anim_skyrim import load_skyrim_animation
        bones = load_skeleton_bones(DOG_SKEL)
        out = str(tmp_path / 'forward.hkx')
        convert_clip_hkx(DOG_FORWARD, bones, out)
        back = load_skyrim_animation(out)
        texts = {a.text for a in back.annotations}
        assert 'FootFront' in texts and 'FootBack' in texts


class TestPerPluginProjectNamespace:
    """Two plugins' same-named creature folders must never share a path.

    Oblivion's scamp and Morrowind_ob's Morrowind scamp both came from a
    folder called `scamp`; under one shared `actors\\tes4\\scamp` path only
    one survived in Data, and a Stunted Scamp in Oblivion ran Morrowind_ob's
    graph — no Spell_FireForget_LH, none of Oblivion's attackStart_TES4_*
    events (read back from the engine's live event map, 2026-08-23). The
    owning plugin is now part of the directory, every project file stem and
    the project name the animation caches are keyed on.
    """

    def test_layout_is_namespaced_everywhere(self):
        from asset_convert.hkx_behavior import project_layout
        a = project_layout('scamp', 'oblivion')
        b = project_layout('scamp', 'morrowind_ob')
        for key in ('project_hkx', 'project_txt', 'behavior_hkx', 'anim_dir',
                    'skeleton_nif', 'body_dir', 'fs_dir'):
            assert a[key] != b[key], key
        assert a['project_hkx'] == \
            'Actors\\TES4\\oblivion\\scamp\\tes4oblivion_scampproject.hkx'
        assert a['behavior_hkx'] == ('Actors\\TES4\\oblivion\\scamp\\'
                                     'Behaviors\\tes4oblivion_scampbehavior.hkx')
        assert a['project_txt'] == 'tes4oblivion_scampproject.txt'
        assert a['fs_dir'] == os.path.join('actors', 'tes4', 'oblivion',
                                           'scamp')

    def test_namespace_is_the_plugin_stem(self):
        from asset_convert.creature_pipeline import plugin_namespace
        assert plugin_namespace('Oblivion.esm') == 'oblivion'
        assert plugin_namespace('Morrowind_ob.esm') == 'morrowind_ob'
        assert plugin_namespace('DLCShiveringIsles.esp') == 'dlcshiveringisles'
        assert plugin_namespace(
            'Morrowind_ob - Chargen and Transport Mod.esp') == \
            'morrowind_ob_chargen_and_transport_mod'

    def test_sibling_union_keeps_both_plugins_projects(self, tmp_path):
        """The shared singlefile registers every plugin's block; same folder
        name in two plugins = two distinct projects, no winner picked."""
        import json
        from asset_convert.creature_pipeline import _manifests_under
        for plug, ns in (('Oblivion.esm', 'oblivion'),
                         ('Morrowind_ob.esm', 'morrowind_ob')):
            d = tmp_path / plug / 'meshes' / 'actors' / 'tes4' / ns / 'scamp'
            d.mkdir(parents=True)
            (d / 'project_manifest.json').write_text(json.dumps(
                {'name': 'scamp', 'namespace': ns,
                 'project_txt': f'tes4{ns}_scampproject.txt'}))
        a = _manifests_under(str(tmp_path / 'Oblivion.esm' / 'meshes'))
        b = _manifests_under(str(tmp_path / 'Morrowind_ob.esm' / 'meshes'))
        assert set(a) == {'tes4oblivion_scampproject.txt'}
        assert set(b) == {'tes4morrowind_ob_scampproject.txt'}
        assert not (set(a) & set(b))

    def test_every_clip_index_is_in_range(self):
        """No clip may index past the character file list."""
        from asset_convert.animation_data import _anim_file_index
        m = {'project_txt': 'tes4xproject.txt',
             'clips': [{'anim': 'Animations\\a%d.hkx' % i}
                       for i in range(17)]}
        idx = _anim_file_index(m)
        n_files = len(dict.fromkeys(c['anim'] for c in m['clips']))
        assert all(0 <= i < n_files for i in idx.values())


# ---------------------------------------------------------------------------
# Spellcasting lane (the magic handshake the engine waits on)
# ---------------------------------------------------------------------------

SCAMP_DIR = os.path.join(REPO, 'export', 'Oblivion.esm', 'meshes',
                         'creatures', 'scamp')
needs_scamp = pytest.mark.skipif(
    not os.path.exists(os.path.join(SCAMP_DIR, 'castself.kf')),
    reason='Oblivion scamp export assets missing')


@needs_scamp
class TestCastLane:
    """Cast clips must become the vanilla FireForget chain wired to the
    ENGINE's magic handshake (decompiled from atronachflamebehavior.hkx):
    the graph raises BeginCast* TO the engine, the engine replies with
    Spell_FireForget_LH/RH (the state-entry events), Magic_Pre_Out chains
    In->Loop, Spell_Release moves to the Out, and the Out clip's
    MLh_SpellFire_Event trigger is what actually fires the spell."""

    def _clips(self):
        from asset_convert.hkx_behavior import classify_clips
        return classify_clips(SCAMP_DIR)

    def test_cast_clips_are_claimed_not_dropped(self):
        clips = self._clips()
        assert set(clips['cast']) == {'Self', 'Target'}
        # nothing named cast* may survive in the dead bucket
        assert not [p for p in clips['extra']
                    if os.path.basename(p).lower().startswith('cast')]

    def test_cast_states_are_three_phase(self):
        """A Skyrim cast is In -> Loop -> Out (vanilla Mag_FF_RH_In/_Loop/
        _Out), ONE chain per graph — vanilla creature casters route
        everything through the left hand (the atronach's RH states are dead
        code and even its RH Out fires MLh_SpellFire_Event)."""
        from asset_convert.hkx_behavior import cast_phase_defs
        names = [s for s, _kf, _p, _st in cast_phase_defs(self._clips())]
        assert names == ['Mag_FF_In', 'Mag_FF_Loop', 'Mag_FF_Out']

    def test_cast_plays_the_aimed_clip_first(self):
        """The engine's entry event carries no delivery, so the chain plays
        the most cast-like gesture available: casttarget over castself."""
        from asset_convert.hkx_behavior import cast_clip
        assert os.path.basename(cast_clip(self._clips())).lower() \
            == 'casttarget.kf'

    def test_only_the_loop_repeats(self):
        from asset_convert.hkx_behavior import state_defs
        loops = {n: l for n, _k, l, _e, _x in state_defs(self._clips())
                 if n.startswith('Mag_FF_')}
        assert loops == {'Mag_FF_In': False, 'Mag_FF_Loop': True,
                         'Mag_FF_Out': False}

    def test_each_phase_gets_its_own_animation(self):
        """The phases are CUT from one source clip, so they cannot share
        one animation file."""
        from asset_convert.hkx_behavior import cast_anim_stems
        stems = cast_anim_stems(self._clips())
        assert stems == {'Mag_FF_In': 'casttarget_In',
                         'Mag_FF_Loop': 'casttarget_Loop',
                         'Mag_FF_Out': 'casttarget_Out'}

    def test_split_is_anchored_on_the_authored_hit_key(self):
        """The release moment is the clip's authored 'Hit' key; the Out
        opens CAST_PRE_RELEASE before it (vanilla's Out carries ~0.23s of
        wind-up before its SpellFire trigger) and the returned offset points
        the trigger back at the authored moment exactly."""
        from asset_convert.hkx_anim import (decode_clip, split_cast_clip,
                                            CAST_LOOP_SECONDS,
                                            CAST_PRE_RELEASE)
        clip, _m = decode_clip(self._clips()['cast']['Target'], 30.0)
        hit = [t for t, v in clip.text_keys if v.strip().lower() == 'hit'][0]
        i, l, o, rel = split_cast_clip(clip)
        cut = max(0.0, hit - CAST_PRE_RELEASE)
        assert abs(i.duration - cut) < 0.05
        assert abs(l.duration - CAST_LOOP_SECONDS) < 1e-6
        assert abs(o.duration - (clip.duration - cut)) < 0.05
        assert abs((cut + rel) - hit) < 1e-6
        # the slices must carry real animation, not empty tracks
        assert len(i.tracks) == len(clip.tracks) == len(o.tracks)
        assert len(i.times) > 1 and len(o.times) > 1

    def test_graph_declares_the_engine_magic_interface(self):
        """Without these variables the engine cannot ask for a cast, and
        without the entry/release vocabulary it can never drive one."""
        from asset_convert.hkx_behavior import build_behavior_xml
        xml = build_behavior_xml('TES4ScampBehavior', self._clips())
        for var in ('bWantCastRight', 'bWantCastLeft', 'bMRh_Ready',
                    'bMLh_Ready', 'IsCasting'):
            assert var in xml, var
        for evt in ('MRh_SpellFire_Event', 'MLh_SpellFire_Event',
                    'BeginCastRight', 'BeginCastLeft',
                    'Spell_FireForget_LH', 'Spell_FireForget_RH',
                    'Magic_Pre_Out', 'Spell_Release', 'Spell_Stop'):
            assert evt in xml, evt
        # the vanilla gating conditions, verbatim — and ONLY those: an
        # expression can read variables, never events, so no invented
        # event-conditioned expressions may exist
        assert ('BeginCastRight if (bWantCastRight && bMRh_Ready '
                '&& !IsCasting)') in xml
        assert ('BeginCastLeft if (bWantCastLeft && bMLh_Ready '
                '&& !IsCasting)') in xml
        assert 'if (BeginCast' not in xml

    def test_ready_and_want_variables_init_to_zero(self):
        """Vanilla inits bM*h_Ready/bWantCast*/IsCasting to 0 — readiness is
        the ENGINE's to grant. (A working caster, the atronach, ships all
        five at 0 in its wordVariableValues.)"""
        import re
        from asset_convert.hkx_behavior import build_behavior_xml
        xml = build_behavior_xml('TES4ScampBehavior', self._clips())
        names = re.search(r'name="variableNames"[^>]*>(.*?)</hkparam>',
                          xml, re.S).group(1)
        order = re.findall(r'<hkcstring>(.*?)</hkcstring>', names)
        vals = xml[xml.index('wordVariableValues'):
                   xml.index('quadVariableValues')]
        words = re.findall(r'name="value">(-?\d+)</hkparam>', vals)
        for var in ('bWantCastRight', 'bWantCastLeft', 'bMRh_Ready',
                    'bMLh_Ready', 'IsCasting'):
            assert int(words[order.index(var)]) == 0, var

    def test_release_fires_from_the_out_phase(self):
        """The Out clip's trigger array must carry the SpellFire release —
        it is what actually fires the spell — plus Spell_Stop at clip end;
        vanilla's Mag_FF_RH_Out block is exactly that pair."""
        import re
        from asset_convert.hkx_behavior import build_behavior_xml
        xml = build_behavior_xml('TES4ScampBehavior', self._clips())
        names = re.search(r'name="eventNames"[^>]*>(.*?)</hkparam>',
                          xml, re.S).group(1)
        order = re.findall(r'<hkcstring>(.*?)</hkcstring>', names)
        fire_id = order.index('MLh_SpellFire_Event')
        stop_id = order.index('Spell_Stop')
        assert f'<hkparam name="id">{fire_id}</hkparam>' in xml
        assert f'<hkparam name="id">{stop_id}</hkparam>' in xml
