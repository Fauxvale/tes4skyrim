"""Creature conversion orchestrator: Oblivion creature folders → complete
Skyrim LE actor projects.

Per creature folder (``<export>/meshes/creatures/<name>/``), emits under
``<out_meshes>/actors/tes4/<name>/``:

  tes4<name>project.hkx / characters / behaviors / character assets/skeleton.hkx
  animations/*.hkx                      (spline-compressed, from the .kf files)
  character assets/skeleton.nif         (converted, ragdoll bhk kept on bones)
  <body part>.nif                       (converted, plain NiSkinInstance)
  project_manifest.json                 (contract for animation_data + import)

Then registers every generated project in the two merged singlefiles
(meshes/animationdatasinglefile.txt + animationsetdatasinglefile.txt — the
engine only loads projects listed there) and writes
``<export>/creature_projects.json`` for the record-side import (RACE/ARMA/
ARMO generation reads project paths, attack events and body-part lists
from it).

NPCs are NOT processed here: humanoid NPC_ records keep the Skyrim race
override system. This pipeline is for everything CREA.
"""

import json
import os
import re
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from worker_budget import worker_count  # noqa: E402
from output_layout import assets_for  # noqa: E402

_WORKERS = worker_count()

# Not real creatures: 'boxtest' is a Bethesda test asset; 'endgame' is the
# KFM-driven Mehrunes Dagon avatar cinematic (morph-controller NIFs PyFFI
# cannot parse — needs its own conversion path if ever wanted). The playable
# Dagon creature is the separate 'mehrunesdagon' folder, which converts.
_EXCLUDE = {'boxtest', 'endgame'}


def plugin_namespace(plugin: str) -> str:
    """The creature-path namespace for a plugin: its file stem, lowercase,
    [a-z0-9_] only ('Morrowind_ob.esm' -> 'morrowind_ob'). One per plugin,
    so two plugins' same-named creature folders never share a path — see
    hkx_behavior.project_layout."""
    stem = os.path.splitext(os.path.basename(str(plugin).rstrip('\\/')))[0]
    return re.sub(r'[^a-z0-9_]+', '_', stem.lower()).strip('_') or 'plugin'


def _namespace_for(out_meshes_dir: str) -> str:
    """output/<plugin>/meshes -> that plugin's namespace."""
    return plugin_namespace(os.path.basename(
        os.path.dirname(os.path.normpath(out_meshes_dir))))


def _convert_creature(creature_dir: str, name: str, out_meshes_dir: str,
                      part_sets: list = None, fps: float = 30.0,
                      sound_slots: dict = None,
                      sound_chances: dict = None,
                      attr_speed: int = 0,
                      namespace: str = '') -> dict:
    """Full conversion of one creature folder. Returns its manifest
    (with added 'skeleton_nif'/'bodies' keys) or raises.

    part_sets: the distinct NIFZ part groupings that CREA records in this
    folder actually use (e.g. dog's [('dogbody.nif','doghead.nif',
    'dogeyes01.nif'), ('wolfbody.nif',...), ...]).  Each set is merged into
    ONE skinned NIF named after its body part (vanilla one-file layout).  If
    None, every .nif in the folder is treated as one set."""
    from asset_convert.hkx_behavior import generate_creature_project
    from asset_convert.hkx_xml import convert_hkx_to_amd64
    from asset_convert.nif_converter import convert_nif, merge_creature_body

    manifest = generate_creature_project(creature_dir, name, out_meshes_dir,
                                         fps=fps, sound_slots=sound_slots,
                                         sound_chances=sound_chances,
                                         attr_speed=attr_speed,
                                         namespace=namespace)
    proj_dir = os.path.join(out_meshes_dir, manifest['dir'])

    # SSE only loads 64-bit havok files: a 32-bit project makes the engine
    # silently fail the behavior-graph load → invisible actor (collision
    # capsule still works).  Generation/validation above is 32-bit WIN32
    # (hkxcmd can't read AMD64 back), so convert everything in place LAST.
    for dirpath, _dirs, files in os.walk(proj_dir):
        for fn in files:
            if fn.lower().endswith('.hkx'):
                convert_hkx_to_amd64(os.path.join(dirpath, fn))

    # Clear stale part/merged NIFs from earlier runs (root level only — the
    # skeleton lives in 'character assets').
    for fn in os.listdir(proj_dir):
        if fn.lower().endswith('.nif'):
            try:
                os.remove(os.path.join(proj_dir, fn))
            except OSError:
                pass

    # Convert every non-skeleton part NIF once into an ISOLATED staging dir.
    # Merges must never read a file another merge has written: creature
    # variants (goblin tribes, zombie limb combos, sheep fleeces) SHARE parts
    # across their NIFZ sets, and the old in-place layout let later merges
    # pick up earlier whole-body merge outputs as "parts" — compounding the
    # entire body into every subsequent file (mangled overlapping geometry,
    # 70x file sizes, quadratic merge times).
    parts_dir = os.path.join(proj_dir, '_parts')
    converted = {}       # lower filename -> pristine converted part path
    nif_failures = []
    for fn in sorted(os.listdir(creature_dir)):
        if not fn.lower().endswith('.nif'):
            continue
        if fn.lower().startswith('skeleton'):
            dst = os.path.join(proj_dir, 'character assets', fn.lower())
            convert_nif(os.path.join(creature_dir, fn), dst, creature=True)
            continue
        dst = os.path.join(parts_dir, fn.lower())
        res = convert_nif(os.path.join(creature_dir, fn), dst, creature=True)
        if res.get('error'):
            nif_failures.append((fn, res['error']))
            continue
        converted[fn.lower()] = dst

    # A single Oblivion creature folder holds several DISTINCT creatures (dog,
    # wolf, skeletal-hound) each with its own NIFZ part set.  Merge EACH set
    # into one skinned NIF (whole animal under one root), the vanilla layout —
    # the engine renders only the single BODY-slot ARMA, so separate head/eyes
    # NIFs never show.  Merged names are unique per set (first part's stem,
    # numbered on collision); the exact set→file mapping ships in the
    # manifest as body_map so the record side never has to re-derive it.
    if not part_sets:
        part_sets = [tuple(converted.keys())] if converted else []
    bodies = []          # merged NIF filenames (one per distinct part set)
    body_map = {}        # '|'.join(pset) -> merged NIF filename
    used_stems, used = set(), set()
    for pset in part_sets:
        paths = [converted[p] for p in pset if p in converted]
        if not paths:
            continue
        base_stem = os.path.splitext(os.path.basename(pset[0]))[0]
        stem, n = base_stem, 2
        while stem in used_stems:
            stem = f'{base_stem}_{n:02d}'
            n += 1
        used_stems.add(stem)
        merged_name = f'{stem}.nif'
        try:
            merge_creature_body(
                paths, os.path.join(proj_dir, merged_name),
                skeleton_path=os.path.join(proj_dir, 'character assets',
                                           'skeleton.nif'))
        except Exception as e:
            nif_failures.append((merged_name, f'{type(e).__name__}: {e}'))
            shutil.copy2(paths[0], os.path.join(proj_dir, merged_name))
        bodies.append(merged_name)
        body_map['|'.join(pset)] = merged_name
        used.update(paths)

    # Parts no set consumed stay as standalone meshes next to the merges.
    for fn_l, p in converted.items():
        if p not in used and not os.path.exists(os.path.join(proj_dir, fn_l)):
            shutil.move(p, os.path.join(proj_dir, fn_l))
    shutil.rmtree(parts_dir, ignore_errors=True)

    manifest['bodies'] = bodies
    manifest['body_map'] = body_map
    manifest['nif_failures'] = nif_failures

    # keep the on-disk manifest in sync (includes the mesh keys)
    with open(os.path.join(proj_dir, 'project_manifest.json'), 'w',
              encoding='utf-8') as f:
        json.dump(manifest, f)
    return manifest


# TES4 CREA CSDT sound type -> the clip roles that should fire it, as
# (clip-role, position) where position is 'start' | 'end' | 'foot'.
#
# Skyrim voices a creature from ANIMATION ANNOTATIONS, not from the actor
# record: a census of all 5118 vanilla Skyrim NPC_ records finds only 36 CSDT
# entries total (31 Hit, 4 Attack, 1 Left Foot) and ZERO Idle/Aware/Death,
# while 23 of the 33 wolf/bear voice SNDRs are referenced by no record at all —
# they are bound purely by name from `SoundPlay.<SNDR EditorID>` triggers in
# animationdata.  Oblivion instead put the whole voice in the CREA record and
# its .kf files carry almost no sound keys (exactly 1 of the goblin's 56, the
# bow string), so converting the records alone leaves every creature silent.
# This table moves that data across: each CSDT slot becomes a trigger on the
# clip that represents it.
_CSDT_TO_CLIP = {
    0: ('locomotion', 'foot'),    # Left Foot
    1: ('locomotion', 'foot'),    # Right Foot
    2: ('locomotion', 'foot'),    # Left Back Foot
    3: ('locomotion', 'foot'),    # Right Back Foot
    6: ('attacks', 'start'),      # Attack
    8: ('death', 'start'),        # Death (only creatures WITH a death anim)
    # 4 (Idle) and 5 (Aware) must NOT be annotated onto the base clips: the
    # Idle/CombatStance clips LOOP, so an embedded SoundPlay fires every
    # cycle — the confirmed "same squeak over and over, even after death"
    # bug (the ragdoll wrapper states also play the idle clip as their pose
    # source). They become dedicated single-play vocal states instead,
    # paced by the engine's own idle system (ActionIdle / ActionIdleWarn
    # IDLE records — the vanilla WolfIdleHowl / WolfIdleWarn pattern). See
    # hkx_behavior.generate_creature_project + tes5_import/creature_idles.
    # 7 (Hit) is driven by the engine's own hit event, and is the ONE slot
    # vanilla still writes on the record (31/36) — left to the CSDT array.
}


def foot_tags(slots: dict) -> dict:
    """{footstep tag: SOUN EditorID} for a creature's CSDT foot slots (0-3).

    The tag is simultaneously the animation event the clips fire AND the
    FSTP.ANAM string the engine matches that event against — vanilla FSTPs
    carry exactly the event name (NPCWolfFootFrontWalkFootstep ANAM=FootFront).
    Both sides MUST derive it from this one function, or the fired event and
    the record tag drift apart and every footstep goes silent.

    Quadrupeds (an authored back-foot slot) collapse to the vanilla wolf pair
    FootFront/FootBack; bipeds get FootLeft/FootRight like vanilla two-legged
    creatures. Slot layout: 0=LeftFoot, 1=RightFoot, 2=LBackFoot, 3=RBackFoot.
    """
    slots = slots or {}
    front = slots.get(0) or slots.get(1)
    back = slots.get(2) or slots.get(3)
    if back:
        out = {'FootBack': back}
        if front:
            out['FootFront'] = front
        return out
    if not front:
        return {}
    return {'FootLeft': slots.get(0) or front,
            'FootRight': slots.get(1) or front}


def foot_enum_map(slots: dict) -> dict:
    """Oblivion kf 'Enum: <X>' foot text key (lowercased, spaces stripped) →
    the Skyrim footstep event to fire, consistent with foot_tags()."""
    if 'FootLeft' in foot_tags(slots):
        return {'left': 'FootLeft', 'right': 'FootRight',
                'backleft': 'FootLeft', 'backright': 'FootRight'}
    return {'left': 'FootFront', 'right': 'FootFront',
            'backleft': 'FootBack', 'backright': 'FootBack'}


def _sound_data_by_folder(export_dir: str) -> dict:
    """folder(lower) -> {csdt_type: (SOUN EditorID, chance)}, from the CREA
    export.

    Resolves CSCR inheritance (817 of Oblivion's 909 CREA records inherit their
    sounds from another creature rather than defining their own), and takes the
    richest slot set in a folder: one behavior project serves every creature
    sharing that mesh folder, so the annotations have to be the union.

    chance is the authored CSDC play-chance (0-100); 100 when the export
    predates the field.
    """
    from tes5_import.text_reader import parse_export_file

    crea_path = os.path.join(export_dir, 'CREA.txt')
    soun_path = os.path.join(export_dir, 'SOUN.txt')
    if not os.path.exists(crea_path):
        return {}

    soun_edid = {}
    if os.path.exists(soun_path):
        for rec in parse_export_file(soun_path):
            fid = (rec.get('FormID') or '').upper()
            edid = rec.get('EditorID')
            if fid and edid:
                soun_edid[fid] = edid

    recs = list(parse_export_file(crea_path))
    by_fid = {(r.get('FormID') or '').upper(): r for r in recs}

    def slots_of(rec, depth=0):
        """{type: (SOUN fid, chance)} for a CREA, following CSCR
        inheritance."""
        n = int(rec.get('SoundTypeCount', 0) or 0)
        if n:
            out = {}
            for i in range(n):
                t = rec.get(f'SoundType[{i}].Type')
                s = rec.get(f'SoundType[{i}].Sound')
                c = rec.get(f'SoundType[{i}].Sound.Chance')
                if t is not None and s:
                    out[int(t)] = (s.upper(),
                                   int(c) if c is not None else 100)
            return out
        if depth < 4:
            src = (rec.get('CSCR.InheritSound') or '').upper()
            if src and src in by_fid:
                return slots_of(by_fid[src], depth + 1)
        return {}

    out = {}
    for rec in recs:
        model = (rec.get('Model.MODL') or '').replace('/', '\\')
        parts = [p for p in model.lower().split('\\') if p]
        folder = parts[-2] if len(parts) >= 2 else ''
        if not folder:
            continue
        slots = {t: (soun_edid[s], c) for t, (s, c) in slots_of(rec).items()
                 if s in soun_edid}
        if not slots:
            continue
        # Richest set wins — the project is shared across the whole folder.
        if len(slots) > len(out.get(folder, {})):
            out[folder] = slots
    return out


def _sound_slots_by_folder(export_dir: str) -> dict:
    """folder(lower) -> {csdt_type: SOUN EditorID} (see
    _sound_data_by_folder; this is the chance-less view most callers use)."""
    return {folder: {t: edid for t, (edid, _c) in slots.items()}
            for folder, slots in _sound_data_by_folder(export_dir).items()}


def _speed_attr_by_folder(export_dir: str) -> dict:
    """folder(lower) -> MAX TES4 DATA.Speed attribute across its CREA records.

    Feeds the speed bake (hkx_behavior.generate_creature_project attr_speed):
    Oblivion moved creatures at the Speed-attribute GMST formula, not at the
    clip's root motion. MAX because the combat variants are the fast ones —
    dead/prop variants (Speed ~9-12) never move — and one behavior project
    serves the whole folder.
    """
    from tes5_import.text_reader import parse_export_file

    crea_path = os.path.join(export_dir, 'CREA.txt')
    if not os.path.exists(crea_path):
        return {}
    out = {}
    for rec in parse_export_file(crea_path):
        model = (rec.get('Model.MODL') or '').replace('/', '\\')
        parts = [p for p in model.lower().split('\\') if p]
        folder = parts[-2] if len(parts) >= 2 else ''
        if not folder:
            continue
        try:
            spd = int(rec.get('DATA.Speed', 0) or 0)
        except (TypeError, ValueError):
            spd = 0
        out[folder] = max(out.get(folder, 0), spd)
    return out


def _part_sets_by_folder(export_dir: str) -> dict:
    """folder(lower) -> list of distinct NIFZ part sets (each a tuple of
    lowercase .nif filenames), read from the CREA export.

    A single creature folder holds several distinct creatures (dog/wolf/
    skeletal-hound) each listing its own body parts in NIFZ.  Each distinct
    set is merged into its own whole-animal NIF, so the record side can point
    each CREA at the right merged mesh."""
    from tes5_import.text_reader import parse_export_file

    crea_path = os.path.join(export_dir, 'CREA.txt')
    if not os.path.exists(crea_path):
        return {}
    out = {}
    for rec in parse_export_file(crea_path):
        model = (rec.get('Model.MODL') or '').replace('/', '\\')
        parts = [p for p in model.lower().split('\\') if p]
        folder = parts[-2] if len(parts) >= 2 else ''
        if not folder:
            continue
        n = int(rec.get('NIFZCount', 0) or 0)
        pset = tuple((rec.get(f'NIFZ[{i}]') or '').lower()
                     for i in range(n))
        pset = tuple(p for p in pset if p.endswith('.nif'))
        if pset:
            out.setdefault(folder, [])
            if pset not in out[folder]:
                out[folder].append(pset)
    return out


def _crea_model_dirs(export_dir: str) -> set:
    """The mesh directories CREA records point their Model.MODL at, as
    lowercase paths relative to the meshes root (Model.MODL is already
    meshes-relative: "Creatures\\Rat\\skeleton.nif").

    Used to break ties when two folders share a leaf name (Morrowind_ob has
    both meshes\\characters\\draugr — a humanoid body-part folder — and
    meshes\\creatures\\aa_blood\\draugr, which is the one its CREA records
    actually reference). Picking by what the records use beats any
    walk-order heuristic."""
    from tes5_import.text_reader import parse_export_file

    crea_path = os.path.join(export_dir, 'CREA.txt')
    if not os.path.exists(crea_path):
        return set()
    out = set()
    for rec in parse_export_file(crea_path):
        model = (rec.get('Model.MODL') or '').replace('/', '\\')
        model = model.replace('\\\\', '\\').lower().lstrip('\\')
        parts = [p for p in model.split('\\') if p]
        if parts and parts[0] == 'meshes':
            parts = parts[1:]
        if len(parts) >= 2:
            out.add('\\'.join(parts[:-1]))
    return out


def _shared_singlefile_dir(out_meshes_dir: str, master_dirs) -> str:
    """Where the two SHARED animation singlefiles belong, or None for 'here'.

    Data holds exactly ONE animationdatasinglefile.txt. A child that ships its
    own copy does not add a file — it races its master for the same path, and
    whichever deploys last de-registers the other's creatures (they then freeze
    in their idles; confirmed in game 2026-08-07). So a child writes through to
    its master's copy, which already carries the union of every project.

    Picks the FIRST master that has a meshes dir; masters are passed in load
    order, so that is the base everything else overrides.
    """
    for d in (master_dirs or []):
        cand = os.path.join(str(d), 'meshes')
        if os.path.normpath(cand) == os.path.normpath(out_meshes_dir):
            continue
        if os.path.isdir(cand):
            return cand
    return None


def _remove_unnamespaced_projects(meshes_dir: str, log=print) -> None:
    """Delete project trees from the pre-namespace layout
    (actors/tes4/<folder>/project_manifest.json directly under tes4).

    They are generated output, and leaving them beside the namespaced tree
    would re-create the very collision the namespace removes the moment the
    whole meshes folder is deployed.
    """
    root = os.path.join(meshes_dir, 'actors', 'tes4')
    if not os.path.isdir(root):
        return
    for d in sorted(os.listdir(root)):
        p = os.path.join(root, d)
        if os.path.isfile(os.path.join(p, 'project_manifest.json')):
            shutil.rmtree(p, ignore_errors=True)
            log(f'  [cleanup] removed pre-namespace project tree '
                f'actors\\tes4\\{d}')


def _manifests_under(meshes_dir: str) -> dict:
    """{project_txt: manifest} for every generated project in a plugin's
    meshes tree: actors/tes4/<namespace>/<folder>/project_manifest.json.

    Keyed on the project file name, which carries the plugin namespace, so
    two plugins' manifests can never collide (hkx_behavior.project_layout).
    """
    out = {}
    root = os.path.join(meshes_dir, 'actors', 'tes4')
    if not os.path.isdir(root):
        return out
    for ns in sorted(os.listdir(root)):
        ns_dir = os.path.join(root, ns)
        if not os.path.isdir(ns_dir):
            continue
        for d in sorted(os.listdir(ns_dir)):
            mp = os.path.join(ns_dir, d, 'project_manifest.json')
            if not os.path.exists(mp):
                continue
            with open(mp, encoding='utf-8') as f:
                m = json.load(f)
            out[m['project_txt'].lower()] = m
    return out


def convert_creatures(export_dir: str, out_meshes_dir: str,
                      skyrim_data_path: str = None,
                      names: list = None, workers: int = None,
                      master_dirs=None,
                      log=print) -> dict:
    """Convert every creature folder under <export_dir>/meshes/creatures.

    Writes the actor projects + converted meshes, merges the animation
    singlefiles (vanilla base from the user's Skyrim install, cached in
    <export_dir>/animdata_base), and saves <export_dir>/creature_projects.json.

    `master_dirs` lists this plugin's MASTERS' converted output dirs. When set,
    the two shared singlefiles are written into the FIRST master's meshes dir
    instead of this one's, and this plugin ships only the projects it owns.

    Returns {'projects': {name: manifest}, 'errors': {name: str}}.
    """
    from asset_convert.animation_data import write_singlefiles

    meshes_root = str(assets_for(export_dir) / 'meshes')
    if not os.path.isdir(meshes_root):
        log(f'  No meshes folder at {meshes_root}')
        return {'projects': {}, 'errors': {}}

    # A creature is ANY folder holding a skeleton.nif plus .kf animations —
    # not just the direct children of meshes\creatures.  Oblivion itself uses
    # that flat layout, but plugins nest theirs freely: Morrowind_ob ships 67
    # such folders under meshes\morro\creatures\<name>,
    # meshes\morroblivion\creatures\<category>\<name> and deeper
    # (…\symphony\fbr\fst), of which the old depth-1 scan of meshes\creatures
    # found only 16 — the other 167 CREA records fell through to Skyrim race
    # aliasing and shipped as BASE SKYRIM creatures.  Walking the whole mesh
    # tree keys on the same last-path-component the record side derives from
    # Model.MODL, so discovery and lookup agree for any layout.
    referenced = _crea_model_dirs(export_dir)
    candidates = []
    for cdir, subdirs, files in os.walk(meshes_root):
        lower = {f.lower() for f in files}
        if 'skeleton.nif' not in lower:
            continue
        name = os.path.basename(cdir)
        if names and name.lower() not in {n.lower() for n in names}:
            continue
        if name.lower() in _EXCLUDE and not names:
            log(f'  [skip] {name}: excluded (test/cinematic asset)')
            continue
        if not any(f.endswith('.kf') for f in lower):
            log(f'  [skip] {name}: no animations')
            continue
        rel = os.path.relpath(cdir, meshes_root).lower().replace('/', '\\')
        candidates.append((cdir, name, rel in referenced))

    # Two folders can share a leaf name (Morrowind_ob ships both
    # meshes\characters\draugr and meshes\creatures\aa_blood\draugr).  They
    # would collide in the output tree (actors/tes4/<name>) and in the
    # record-side lookup, which is keyed on that same leaf.  Prefer whichever
    # folder the CREA records actually point at; otherwise fall back to the
    # shallowest path, then alphabetical, so the choice is deterministic.
    seen_names = {}
    dirs = []
    for cdir, name, is_ref in sorted(
            candidates,
            key=lambda c: (not c[2], c[0].count(os.sep), c[0].lower())):
        key = name.lower()
        if key in seen_names:
            log(f'  [skip] {cdir}: duplicate creature name "{name}" '
                f'(using {seen_names[key]})')
            continue
        seen_names[key] = cdir
        dirs.append((cdir, name))
    dirs.sort(key=lambda d: d[1].lower())

    # Distinct NIFZ part sets per folder (dog/wolf/skeletal-hound share a
    # folder but each merges into its own whole-animal NIF).
    part_sets = _part_sets_by_folder(export_dir)
    # CSDT sound slots per folder — replayed as animation annotations and
    # vocal idle states, which is how Skyrim voices a creature (see
    # _sound_data_by_folder / hkx_behavior._apply_sound_slots /
    # generate_creature_project's vocal states).
    sound_data = _sound_data_by_folder(export_dir)
    sound_slots = {f: {t: e for t, (e, _c) in s.items()}
                   for f, s in sound_data.items()}
    sound_chances = {f: {t: c for t, (_e, c) in s.items()}
                     for f, s in sound_data.items()}
    speed_attrs = _speed_attr_by_folder(export_dir)
    namespace = _namespace_for(out_meshes_dir)
    _remove_unnamespaced_projects(out_meshes_dir, log)

    log(f'  Converting {len(dirs)} creatures '
        f'({workers or _WORKERS} workers, namespace {namespace})...')
    # ProcessPoolExecutor: the per-creature work is CPU-bound pure Python
    # (pyffi NIF conversion, KF decode, spline compression) — threads
    # serialize on the GIL and give no speedup at all.
    projects, errors = {}, {}
    with ProcessPoolExecutor(max_workers=workers or _WORKERS) as pool:
        futs = {pool.submit(_convert_creature, cdir, name, out_meshes_dir,
                            part_sets.get(name.lower()), 30.0,
                            sound_slots.get(name.lower()),
                            sound_chances.get(name.lower()),
                            speed_attrs.get(name.lower(), 0),
                            namespace):
                name for cdir, name in dirs}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                manifest = fut.result()
            except Exception as e:
                errors[name] = f'{type(e).__name__}: {e}'
                log(f'  [FAIL] {name}: {errors[name]}')
                continue
            projects[name] = manifest
            n_fail = len(manifest['failures']) + len(manifest['nif_failures'])
            log(f'  [ok] {name}: {len(manifest["clips"])} clips, '
                f'{len(manifest["bodies"])} body nifs'
                + (f', {n_fail} failures' if n_fail else ''))

    # Registration: merged singlefiles (vanilla base + ALL projects on disk).
    # A subset run (--names) must not drop the other creatures' registrations,
    # so pick up every previously generated project_manifest.json too.
    all_manifests = dict(projects)
    for m in _manifests_under(out_meshes_dir).values():
        if m.get('namespace') == namespace:
            all_manifests.setdefault(m['name'], m)

    # UNION across EVERY built plugin (masters and siblings): the game's Data
    # folder holds exactly ONE animationdatasinglefile.txt, so the single
    # deployed copy must register every plugin's creatures — deploying
    # Morrowind_ob's file over Oblivion's de-registered all of Oblivion's
    # projects and its creatures froze in their idles (confirmed in game
    # 2026-08-07). A child writes through to its master's copy
    # (_shared_singlefile_dir), but whichever plugin is built LAST rewrites
    # that one shared file from the vanilla base and must put every sibling's
    # projects back, so every plugin's project_manifest.json is read here.
    #
    # Projects are keyed on their project file name, which carries the owning
    # plugin's namespace (hkx_behavior.project_layout), so two plugins can
    # never contribute rival blocks for one name and no winner has to be
    # picked: each block describes exactly the character hkx its own plugin
    # ships at its own path.
    union = {m['project_txt'].lower(): m for m in all_manifests.values()}
    plugins_root = os.path.dirname(os.path.dirname(out_meshes_dir))
    try:
        siblings = sorted(os.listdir(plugins_root))
    except OSError:
        siblings = []
    for plug in siblings:
        sib_meshes = os.path.join(plugins_root, plug, 'meshes')
        if os.path.normpath(sib_meshes) == os.path.normpath(out_meshes_dir):
            continue
        for key, cand in _manifests_under(sib_meshes).items():
            union.setdefault(key, cand)

    if union:
        cache_dir = os.path.join(export_dir, 'animdata_base')
        manifests = [union[n] for n in sorted(union)]
        own = [all_manifests[n] for n in sorted(all_manifests)]
        # The two singlefiles are ONE shared file in the game's Data folder.
        # A child plugin must not ship its own copy racing the master's for
        # that path — it writes THROUGH to the master's instead. Only the
        # per-project sources (written from `own`) land in this plugin's tree.
        sf_dir = _shared_singlefile_dir(out_meshes_dir, master_dirs)
        counts = write_singlefiles(manifests, out_meshes_dir,
                                   skyrim_data_path, cache_dir,
                                   singlefile_dir=sf_dir,
                                   own_manifests=own)
        where = ('the master\'s shared file' if sf_dir else 'this plugin')
        log(f'  Registered {len(manifests)} projects in {where} '
            f'({len(all_manifests)} own, '
            f'{len(union) - len(all_manifests)} from sibling plugins; '
            f'animationdatasinglefile: {counts["animationdatasinglefile.txt"]}'
            f' total)')

    # Contract for tes5_import (RACE/ARMA/ARMO generation).
    # .get defaults: an interrupted run can leave a manifest without the
    # mesh keys (they are added after project generation) — don't let one
    # stale file kill the summary for every other creature.
    summary = {name: {
        'project_hkx': m['project_hkx'],
        # root behavior path: the engine matches creature IDLE roots to an
        # actor by this (creature_idles DNAM)
        'behavior_hkx': m['behavior_hkx'],
        # directory the merged body NIFs sit in (ARMA MOD2)
        'body_dir': m['body_dir'],
        'skeleton_nif': m['skeleton_nif'],
        'bodies': m.get('bodies', []),
        'body_map': m.get('body_map', {}),
        'attacks': m.get('attacks', []),
        # engine movement-type registration contract (iState_* graph vars ↔
        # MOVT MNAM); fallback derives the same names for stale manifests
        'movement_types': m.get('movement_types',
                                [f'TES4{name.lower()}Default',
                                 f'TES4{name.lower()}Run']),
        # clip root-motion speeds (u/s) → per-creature MOVT SPED columns
        'speeds': m.get('speeds', {}),
        'has_ragdoll': m.get('has_ragdoll', False),
        # cast/block graph lanes -> their IDLE action routing (creature_idles)
        'has_cast': m.get('has_cast', False),
        'has_block': m.get('has_block', False),
        'clips': [c['name'] for c in m.get('clips', [])],
        'bones': m.get('bones', []),
        # ragdoll part bone names -> per-creature BPTD (creature_races)
        'ragdoll_bones': m.get('ragdoll_bones', []),
        # vocal idle states -> import generates their ActionIdle/
        # ActionIdleWarn IDLE entry records (creature_idles)
        'vocal_events': m.get('vocal_events', []),
    } for name, m in all_manifests.items()}
    with open(os.path.join(export_dir, 'creature_projects.json'), 'w',
              encoding='utf-8') as f:
        json.dump(summary, f, indent=1)

    return {'projects': projects, 'errors': errors}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(
        description='Convert Oblivion creatures to Skyrim actor projects')
    ap.add_argument('export_dir', help='export/<plugin> directory')
    ap.add_argument('out_meshes_dir', help='output meshes/ directory')
    ap.add_argument('--skyrim-data', help='Skyrim Data folder (for the '
                    'vanilla animation singlefile merge base)')
    ap.add_argument('--names', nargs='+', help='only these creature folders')
    ap.add_argument('--workers', type=int)
    args = ap.parse_args()

    out = convert_creatures(args.export_dir, args.out_meshes_dir,
                            skyrim_data_path=args.skyrim_data,
                            names=args.names, workers=args.workers)
    print(f"{len(out['projects'])} projects, {len(out['errors'])} errors")
    for name, err in out['errors'].items():
        print(f'  {name}: {err}')
