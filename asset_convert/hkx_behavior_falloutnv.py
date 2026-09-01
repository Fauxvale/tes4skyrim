"""FO3/FNV creature animation naming, mapped onto the TES4 clip names.

FO3/FNV prefix every movement-type clip with `mt` and keep the gaits in a
`locomotion/` subfolder, where Oblivion writes bare names flat in the creature
folder. Aliasing the FO3/FNV spellings to the TES4 ones lets `classify_clips`
keep a single vocabulary.

See: docs/commentary/asset_convert_creature.md#fo3fnv-creature-clip-naming
"""

import os

#: Subfolders FO3/FNV keep gait clips in; Oblivion keeps them flat.
LOCOMOTION_DIRS = ('locomotion',)

#: FO3/FNV movement-type prefix, stripped to recover the TES4 basename.
_MT_PREFIX = 'mt'

#: Prefixes that may precede `mt`; the swim set is spelled `swimmtforward`.
_KEPT_PREFIXES = ('swim',)

#: FO3/FNV spellings with no `mt` counterpart, including vanilla's own typos.
_ALIASES = {
    'mtfoward': 'forward',
    'mtfastfoward': 'fastforward',
}


def _tes4_name(stem: str) -> str:
    """The TES4 clip basename an FO3/FNV stem corresponds to, or the stem.

    Strips the `mt` movement-type prefix, keeping any leading `swim` so a swim
    clip still reads as one.
    """
    kept = ''
    for pre in _KEPT_PREFIXES:
        if stem.startswith(pre):
            kept, stem = pre, stem[len(pre):]
            break
    if stem in _ALIASES:
        return kept + _ALIASES[stem]
    if stem.startswith(_MT_PREFIX) and len(stem) > len(_MT_PREFIX):
        return kept + stem[len(_MT_PREFIX):]
    return kept + stem


def alias_clips(creature_dir: str, kfs: dict) -> dict:
    """Add TES4-named entries for the FO3/FNV clips of one creature folder.

    Scans the `locomotion/` subfolder and re-keys every `mt`-prefixed clip to
    its TES4 basename. Existing keys always win, so an Oblivion folder that
    already ships the bare name is left untouched. Returns `kfs`.
    """
    found = dict(kfs)
    for sub in LOCOMOTION_DIRS:
        d = os.path.join(creature_dir, sub)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.lower().endswith('.kf'):
                stem = os.path.splitext(fn)[0].lower()
                found.setdefault(stem, os.path.join(d, fn))
    for stem, path in found.items():
        kfs.setdefault(_tes4_name(stem), path)
    return kfs
