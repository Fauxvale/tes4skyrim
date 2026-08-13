"""Where finished, installable artefacts land inside the output directory.

`output/` is a WORKSPACE, not a delivery folder. It holds one working folder per
converted plugin (`output/Oblivion.esm/`), the baked LOD mod
(`output/AutoConvertLOD/`), export caches and manifests — all of it intermediate,
none of it what the user installs. The things they DO install were mixed in at
the same level, so finding the four files that actually ship meant knowing which
of a dozen entries were products and which were scaffolding.

Everything installable is collected here instead: every mod zip
(`<plugin>.zip`, `TESGameSelect.zip`, `AutoConvertLOD.zip`) and the standalone
`Slot44 Patch.esp`, which ships as a loose plugin rather than an archive.

Deliberately its own tiny module: four independent producers write here —
convert.py's zip and body-patch phases, tools/package_start_mod.py and
tools/pack_lod.py — and the tools must not import the whole pipeline to learn
one folder name.

The name has a SPACE in it and is user-facing, so it is spelled exactly once,
here. Note for scanners: nothing in here is a converted plugin. `output/` is
scanned for plugin folders by `sibling_lod.converted_plugins` (needs
`<folder>/<folder>` to exist) and `gui.scan_converted` (needs a manifest), and
this folder satisfies neither, so it is never mistaken for one.
"""

from pathlib import Path

FINISHED_DIR_NAME = "Finished Mods"


def finished_dir(out_root) -> Path:
    """`out_root`'s finished-mods folder, created if it does not exist.

    Created on demand rather than up front: a run that packages nothing should
    not leave an empty folder promising deliverables it never made.
    """
    d = Path(out_root) / FINISHED_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d
