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
convert.py's zip and body-patch phases, tools/release/package_start_mod.py and
tools/release/pack_lod.py — and the tools must not import the whole pipeline to learn
one folder name.

The name has a SPACE in it and is user-facing, so it is spelled exactly once,
here. Note for scanners: nothing in here is a converted plugin. `output/` is
scanned for plugin folders by `sibling_lod.converted_plugins` and
`gui.scan_converted`; both now accept EITHER `<folder>/<folder>` or a
`<plugin>.manifest.json` inside the folder, because an imported mod's folder is
named for the MOD rather than for any one plugin. `Finished Mods/` holds zips
and a loose .esp but no manifest, so it still satisfies neither test and is
never mistaken for a converted plugin.
"""

from pathlib import Path

FINISHED_DIR_NAME = "Finished Mods"

# Marks the export ROOT. Used to tell `export/<mod>/<plugin>/`
# (records nested inside a mod) from a plain `export/<plugin>/`.
REGISTRY_FILENAME = "sources.json"


def finished_dir(out_root) -> Path:
    """`out_root`'s finished-mods folder, created if it does not exist.

    Created on demand rather than up front: a run that packages nothing should
    not leave an empty folder promising deliverables it never made.
    """
    d = Path(out_root) / FINISHED_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- Shared-folder resolution ----------------------------------------------
# Plugins imported together from one mod archive share ONE asset payload but
# keep their own records, so a plugin name maps to a folder in three different
# ways. All three live here, in the one module that already exists to spell a
# path convention exactly once, and that any tool can import without dragging
# in the pipeline.
#
# The registry import is optional ON PURPOSE -- output_layout must stay usable
# from standalone tools -- but ONLY ImportError is caught. An exception raised
# INSIDE the resolver is a real bug, and swallowing it would silently hand back
# the pre-group path: meshes read from a folder that no longer exists, no error
# anywhere. That is the failure mode this project keeps getting bitten by, so
# it is deliberately allowed to raise.

def _registry():
    """`source_registry`, or None when it cannot be imported at all."""
    try:
        from asset_convert import source_registry
    except ImportError:
        return None
    return source_registry


def asset_root(export_dir, plugin: str) -> Path:
    """`export/<group-or-plugin>/` — the SHARED meshes/textures/sound/trees."""
    reg = _registry()
    if reg is None or not export_dir:
        return Path(export_dir or '') / plugin
    return reg.asset_root(export_dir, plugin)


def record_dir(export_dir, plugin: str) -> Path:
    """`export/<group>/<plugin>/` — THIS plugin's records and own caches."""
    reg = _registry()
    if reg is None or not export_dir:
        return Path(export_dir or '') / plugin
    return reg.record_dir(export_dir, plugin)


def plugin_out_root(out_root, plugin: str, export_dir=None) -> Path:
    """The folder in `out_root` holding `plugin`'s converted artefacts.

    Mirrors the export side: plugins imported together from one mod archive
    share a single folder named for the mod, so the three converted ESMs of a
    resource pack sit side by side rather than in three trees that each hold a
    private copy of the same meshes.

    `export_dir` is where the source registry lives; without it (or for a
    plugin that is not an imported mod) this is `out_root/<plugin>` exactly as
    it has always been.
    """
    reg = _registry() if export_dir else None
    name = reg.asset_root_name(export_dir, plugin) if reg else plugin
    return Path(out_root) / name


def plugin_esm(out_root, plugin: str, export_dir=None) -> Path:
    """The converted plugin file itself: `output/<group-or-plugin>/<plugin>`.

    `out_root / plugin / plugin` was the idiom in a dozen places. It is wrong
    for an imported mod, whose plugins share one folder named for the MOD, and
    every copy of it had to be found and fixed by hand. Call this instead.
    """
    return plugin_out_root(out_root, plugin, export_dir) / plugin


def master_record_dir(export_dir, master: str) -> Path:
    """Where MASTER `master`'s exported records live.

    Identical to `record_dir`, named for the calling context: master lookups
    are where joining a name onto `export/` silently returns a path that does
    not exist, the importer then diffs overrides against nothing, and the
    plugin converts wrong with only a warning. Always resolve a master here.
    """
    return record_dir(export_dir, master)


# ---------------------------------------------------------------------------
#  One handle per plugin
#
#  The functions above each answer ONE question and each need the roots passed
#  in, so every module ended up re-deriving paths itself -- and every module
#  that got it wrong did so silently. `PluginPaths` bundles the whole answer
#  for one plugin behind a single call, so a caller asks once and reads
#  attributes:
#
#      pp = paths('TamRes.esm')
#      pp.records            export/<Mod>/TamRes.esm/
#      pp.assets             export/<Mod>/            (shared by the mod)
#      pp.out                output/<Mod>/
#      pp.esm                output/<Mod>/TamRes.esm
#      pp.master('Oblivion.esm').records
#
#  The roots default to the repo's own export/ and output/, which is what every
#  pipeline caller wants; a test or tool passes its own.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_EXPORT = REPO_ROOT / "export"
DEFAULT_OUTPUT = REPO_ROOT / "output"


class PluginPaths:
    """Every path belonging to one plugin. Read attributes, never build paths.

    Cheap to construct: each attribute resolves on access and the registry
    behind it is cached, so holding one of these is not holding a snapshot.
    """

    __slots__ = ("plugin", "export_root", "out_root")

    def __init__(self, plugin: str, export_root=None, out_root=None):
        self.plugin = plugin
        self.export_root = Path(export_root
                                if export_root is not None else DEFAULT_EXPORT)
        self.out_root = Path(out_root
                             if out_root is not None else DEFAULT_OUTPUT)

    @property
    def records(self) -> Path:
        """`export/<group>/<plugin>/` — this plugin's .txt dump and caches."""
        return record_dir(self.export_root, self.plugin)

    @property
    def assets(self) -> Path:
        """`export/<group>/` — meshes/textures/sound/trees, shared by the mod."""
        return asset_root(self.export_root, self.plugin)

    @property
    def source(self) -> Path:
        """`export/<group>/_source/` — plugin binaries and retained archive."""
        return self.assets / "_source"

    @property
    def out(self) -> Path:
        """`output/<group>/` — where converted artefacts land."""
        return plugin_out_root(self.out_root, self.plugin, self.export_root)

    @property
    def esm(self) -> Path:
        """`output/<group>/<plugin>` — the converted plugin file itself."""
        return self.out / self.plugin

    def master(self, master: str) -> "PluginPaths":
        """The same handle for one of this plugin's MASTERS.

        Masters are where the plain join hurt most: a master that IS converted
        resolved to a path that does not exist, the importer diffed every
        override against nothing, and the only symptom was a warning.
        """
        return PluginPaths(master, self.export_root, self.out_root)

    def __repr__(self):
        return f"PluginPaths({self.plugin!r})"


def paths(plugin: str, export_root=None, out_root=None) -> PluginPaths:
    """The path handle for `plugin`. The one entry point worth memorising."""
    return PluginPaths(plugin, export_root, out_root)


def assets_for(export_subdir) -> Path:
    """The SHARED asset tree that `export_subdir`'s records belong to.

    Most of the pipeline is handed a plugin's RECORD directory (the folder of
    .txt dumps) and then reaches sideways for assets: `export_dir / 'meshes'`,
    `export_dir / 'collision_cache.bin'`, `export_dir / 'sound' / 'voice'`.
    That worked only while records and assets shared one folder. For an
    imported mod the records are one level deeper than the assets, so those
    joins point at a folder that does not exist and the lookup silently
    answers "nothing" -- no mesh bounds, no collision, no voice.

    Given `export/<Mod>/<plugin>/` this returns `export/<Mod>/`; given a
    plain `export/<plugin>/` it returns it unchanged. Pass a record dir and
    read assets from the result.
    """
    d = Path(export_subdir)
    parent = d.parent
    # `<root>/<mod>/<plugin>` is the only nested shape: the grandparent is the
    # export root, marked by the registry file sitting in it.
    if (parent.parent / REGISTRY_FILENAME).is_file() and not (
            d / REGISTRY_FILENAME).is_file():
        if (parent / REGISTRY_FILENAME).is_file():
            return d          # d is directly under the root: not nested
        return parent
    return d

