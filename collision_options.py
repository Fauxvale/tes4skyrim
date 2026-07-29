"""Single source of truth for the collision winding-repair toggle.

``asset_convert.collision._repair_inverted_floors`` rewinds collision triangles
whose winding was reversed at the source (the "I fall through the floor"
symptom).  The repair is aimed at plugins that re-export their collision as
flattened ``bhkPackedNiTriStripsShape`` triangle lists, which drops the strip's
alternating parity — measured on **Nehrim** and **Morrowind_ob**, where the
Oblivion originals of the same meshes give exact ground truth for what the
winding should be.

Vanilla Oblivion assets do not have that damage, and while the repair is
designed to be inert on correctly wound input (step 1 is exact; step 2 requires
a quorum), "inert" is not "free": it still costs a per-shape weld + BFS, and any
false positive it *does* make punches a hole in geometry that was already
correct.  So the repair is **opt-in per plugin** rather than always-on.

Resolution order (see :func:`winding_fix_enabled`):

  1. ``TESCONV_COLLISION_WINDING_FIX`` env var, if set, is the explicit choice
     and wins outright.  ``convert.py`` sets it from its CLI flag; the GUI sets
     its own default from the selected plugin.  It lives in the environment so
     it propagates to every child process and every ``multiprocessing`` mesh
     worker — the repair runs deep inside those workers, far from any call site
     a parameter could reasonably be threaded through.
  2. Otherwise the per-plugin default: on for the plugins measured to need it
     (:data:`WINDING_FIX_DEFAULT_PLUGINS`), off for everything else.

``default_for_plugin`` is the *only* place the plugin list lives; both the CLI
and the GUI read their default from it so the two can never disagree.
"""
import os

__all__ = [
    "WINDING_FIX_ENV_VAR",
    "WINDING_FIX_DEFAULT_PLUGINS",
    "default_for_plugin",
    "winding_fix_enabled",
    "env_for",
]

WINDING_FIX_ENV_VAR = "TESCONV_COLLISION_WINDING_FIX"

# Plugins whose collision is known to arrive with reversed winding, matched on
# the plugin's stem, case-insensitively (so "Nehrim.esm" and "Nehrim.esp" both
# hit).  These re-export Oblivion's own meshes through a tool that flattens
# triangle strips, so the damage is structural and measurable; everything else
# ships its collision as authored and needs no repair.
WINDING_FIX_DEFAULT_PLUGINS = frozenset({
    "nehrim",
    "morrowind_ob",
})

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def default_for_plugin(plugin: str) -> bool:
    """Whether the winding repair defaults to on for `plugin`.

    `plugin` may be a bare name, a filename with extension, or a full path.
    """
    if not plugin:
        return False
    stem = os.path.splitext(os.path.basename(str(plugin)))[0]
    return stem.lower() in WINDING_FIX_DEFAULT_PLUGINS


def winding_fix_enabled(plugin: str = None) -> bool:
    """Whether to run the collision winding repair.

    The ``TESCONV_COLLISION_WINDING_FIX`` env var wins if set to a recognised
    boolean; otherwise falls back to :func:`default_for_plugin`.  An
    unrecognised value is ignored rather than guessed at.
    """
    raw = os.environ.get(WINDING_FIX_ENV_VAR, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default_for_plugin(plugin)


def env_for(enabled: bool) -> dict:
    """Environment fragment that pins the toggle for child processes."""
    return {WINDING_FIX_ENV_VAR: "1" if enabled else "0"}
