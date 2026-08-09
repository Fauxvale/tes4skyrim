"""Animationdata clip-index audit for generated creature projects.

THE CONTRACT (skyrim-creature-system skill, animation_cache.md 1.3): line 2
of every animationdata clip block is an index into the project's character
hkx `hkbCharacterStringData.animationNames` — the DEDUPLICATED animation
file list.  Two clips playing one file share one index; an index >= the
file count never binds.  This is "the single most fragile link in the whole
system": a broken index plays the wrong animation, and an out-of-range one
leaves the clip generator dead (the 2026-08-08 creature-ragdoll root cause:
FullyRagdollPose, the death-state pose source, was out of range in every
creature project, so no corpse ever ragdolled no matter what the behavior
graph did).

For every generated project in the deployed animationdatasinglefile.txt this
tool resolves each clip block's index against the character hkx on disk and
reports:
  - indices out of range            (clip never binds)
  - motion-block indices out of range / duplicated
  - (--verbose) every clip -> file resolution, for eyeballing

Usage:
  python tools/animdata_index_check.py [--plugin Oblivion.esm]
      [--project tes4dogproject.txt] [--verbose]

Exits non-zero if any generated project has an out-of-range index.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from animcache_validate import (Scanner, read_string_list,  # noqa: E402
                                parse_project_block)


def parse_singlefile(path):
    """{project_name.txt (lower): (clips, motion_lines)} using the exact
    ck-cmd wrapper grammar from animcache_validate."""
    x = open(path, encoding='latin-1').read().lstrip('\x00')
    sc = Scanner(x.splitlines())
    names = read_string_list(sc)
    out = {}
    for name in names:
        block_lines = read_string_list(sc)
        pb = parse_project_block(block_lines, name)
        motion = []
        if pb['has_cache']:
            motion = read_string_list(sc)
        out[name.lower()] = (pb['clips'], motion)
    return out


def motion_indices(motion):
    i, out = 0, []
    while i < len(motion):
        if not motion[i].strip():
            i += 1
            continue
        idx = int(motion[i])
        nt = int(motion[i + 2])
        nr = int(motion[i + 3 + nt])
        out.append(idx)
        i += 4 + nt + nr + 1
    return out


def character_anims(char_hkx):
    raw = open(char_hkx, 'rb').read()
    anims = [m.group(0).decode('latin-1')
             for m in re.finditer(rb'[ -~]{4,}\.hkx', raw, re.I)]
    return [a for a in anims if a.lower().startswith('animations')]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--plugin', default='Oblivion.esm')
    ap.add_argument('--project', help='single project name (.txt) to check')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()

    meshes = os.path.join('output', args.plugin, 'meshes')
    single = os.path.join(meshes, 'animationdatasinglefile.txt')
    actors = os.path.join(meshes, 'actors', 'tes4')
    if not os.path.isfile(single):
        sys.exit(f'missing {single}')

    blocks = parse_singlefile(single)
    failures = 0
    checked = 0
    for folder in sorted(os.listdir(actors)):
        proj_txt = f'tes4{folder}project.txt'.lower()
        if args.project and proj_txt != args.project.lower():
            continue
        if proj_txt not in blocks:
            continue
        char = os.path.join(actors, folder, 'characters',
                            f'tes4{folder}character.hkx')
        if not os.path.isfile(char):
            print(f'{proj_txt}: character hkx missing, skipped')
            continue
        anims = character_anims(char)
        clips, motion = blocks[proj_txt]
        bad = []
        for name, idx, _ntrig in clips:
            if not (0 <= idx < len(anims)):
                bad.append(f'clip {name!r} index {idx} out of range '
                           f'(files: {len(anims)})')
            elif args.verbose:
                print(f'  {name:40} -> [{idx:3}] {anims[idx]}')
        mi = motion_indices(motion)
        for idx in mi:
            if not (0 <= idx < len(anims)):
                bad.append(f'motion block index {idx} out of range')
        dupes = {x for x in mi if mi.count(x) > 1}
        if dupes:
            bad.append(f'duplicate motion indices: {sorted(dupes)}')
        checked += 1
        if bad:
            failures += 1
            print(f'{proj_txt}: {len(bad)} PROBLEMS')
            for b in bad:
                print(f'    {b}')
        elif args.verbose or args.project:
            print(f'{proj_txt}: OK ({len(anims)} files)')
    print(f'checked {checked} projects, {failures} with problems')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
