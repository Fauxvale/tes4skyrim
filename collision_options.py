"""Single source of truth for the collision winding-repair toggle.

``asset_convert.collision._repair_inverted_floors`` rewinds collision triangles
whose winding was reversed at the source (the "I fall through the floor"
symptom).  It has two halves, and **only the second one is what this toggle
controls**:

* **Step 0 — the authored normal. ALWAYS ON, not gated.**  Every collision
  triangle stores the direction it is meant to face independently of the
  winding that produces that facing (``hkPackedNiTriStripsData.triangles[i]
  .normal``, or ``NiTriStripsData.normals[]`` averaged per face).  A strip
  flatten reverses the winding and carries the stored normal through
  untouched, so a triangle that contradicts its own normal is damaged BY ITS
  OWN RECORD.  That is read off the file — no adjacency, no oracle, no
  threshold — so it is inert wherever the two agree and safe everywhere.

* **Steps 1-3 — inference. GATED, and that is what lives here.**  A BFS over
  shared edges, an enclosed-volume sign test, and a render-mesh vote.  These
  GUESS the answer, and guessing costs false positives: step 1 seeds each
  welded component from an arbitrary triangle, so a component seeded inward
  inverts wholesale (leyawiincastle02 lost 274 of 284 triangles, 806 walkable
  raycast cells, on a *vanilla* mesh).

Vanilla Oblivion is NOT clean, which is why step 0 is unconditional:
seIsland.nif ships 1480 of 3590 collision triangles contradicting their own
normals, and 14.5% of decidable floor faces in ``meshes/rocks`` are inverted —
you fall through the Shivering Isles island in vanilla.  Step 0 fixes it and
leaves already-correct meshes untouched.

The gate exists for corruption that is SELF-CONSISTENT — an exporter that
rewrote the normals to match the winding it emitted.  Both sources then agree
while both are wrong, step 0 has nothing to detect, and inference is the only
thing left.  Morroblivion is that case and is the only default member; see
:data:`WINDING_FIX_DEFAULT_PLUGINS` for the measurements and for why Nehrim
was removed.

Numbers here come from ``tools/nif/collision_winding_truth.py``, which compares
each near-horizontal collision face against the render face coincident with
it.  Read them with care: a naive nearest-skin comparison scores vanilla stairs
48/48 falsely inverted and furniture at 16%, because a thin slab has both its
skins in range.  See that tool's THIN-SLAB TRAP note.

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

# Plugins that need the INFERRED repair steps on top of the authored-normal
# rewind, matched on the plugin's stem, case-insensitively (so "Morrowind_ob.esm"
# and "Morrowind_ob.esp" both hit).
#
# Morroblivion's exporter rewrote each triangle's stored normal to match the
# winding it emitted, so both agree while both are wrong and step 0 has nothing
# to detect: inuhlaaluuroomuside.nif's 10 triangles ALL score dot +1.0 over a
# floor you fall straight through, and across morro/i the authored normals
# change 0 of 5496 inverted faces.  Only the inferred steps recover those
# (5496 -> 20 with steps 1-3).
#
# Nehrim is deliberately NOT here any more.  Its exporter left the normals
# intact, so step 0 alone takes morro-scale damage down to a handful (dungeons
# 2710 inverted -> 14, architecture 669 -> 34) without inference -- and the
# inferred steps are the part that costs false positives on correctly authored
# meshes.  Re-add it only with a measurement showing the remainder is worth
# that risk.
WINDING_FIX_DEFAULT_PLUGINS = frozenset({
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
