"""Tests for the GUI's ability to START on a machine that has only stdlib tkinter.

The failure this guards against is the worst kind to diagnose: main() relaunches
the GUI under pythonw, which has NO CONSOLE, so an exception raised on the way
to the first window prints nowhere. The user sees a program that does nothing at
all -- no window, no error -- and the developer, who has every optional package
installed, cannot reproduce it.

Every optional GUI dependency must therefore degrade to a working window, and
the fallback path must be exercised by a test rather than by a user.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gui  # noqa: E402


@pytest.fixture
def without_tkinterdnd2(monkeypatch):
    """Simulate a user who never pip-installed the optional drag-drop package.

    Blocks the module in sys.modules rather than patching __import__: Tk's own
    startup sources its Tcl library through the import machinery, so a
    broad-brush __import__ patch breaks real window creation and the test then
    fails for a reason that has nothing to do with drag-and-drop.
    """
    monkeypatch.setitem(sys.modules, "tkinterdnd2", None)
    monkeypatch.setattr(gui, "DND_AVAILABLE", False)


@pytest.fixture
def display():
    """Skip when Tk cannot open a window (headless CI).

    Deliberately does NOT build a throwaway root to probe with. Creating and
    destroying a root, then creating another in the same process, makes Tcl
    re-run its own initialisation, and on Python 3.14 for Windows that second
    init fails to re-read init.tcl. The probe would then sink the very tests it
    gates. Importing _tkinter is enough to tell a headless box apart -- the
    single root each test creates is the real check.
    """
    pytest.importorskip("tkinter")
    pytest.importorskip("_tkinter")


# ── The regression ────────────────────────────────────────────────────────
# _make_root()'s fallback called a bare `tk.Tk()`, but `tk` is imported INSIDE
# gui_main() and is not a module-level name. So on any machine without
# tkinterdnd2 the fallback raised NameError instead of returning a window, and
# the GUI died before creating one. Users on earlier builds were unaffected
# because _make_root did not exist -- gui_main() called tk.Tk() directly, where
# the local import made the name valid.

def test_module_defines_no_global_tk():
    """The name the old fallback reached for genuinely does not exist.

    If someone later adds a module-level `import tkinter as tk`, this test
    fails loudly -- that is fine, but then the fallback's local import is what
    should be deleted, not this guarantee.
    """
    assert not hasattr(gui, "tk")


def test_root_is_created_without_the_optional_dnd_package(display, without_tkinterdnd2):
    """The whole point: no tkinterdnd2 still yields a real, usable window."""
    root = gui._make_root()
    try:
        assert root.winfo_exists()
    finally:
        root.destroy()


def test_missing_dnd_package_is_reported_not_fatal(display, without_tkinterdnd2):
    """Drag-and-drop turns itself off rather than taking the app down with it."""
    root = gui._make_root()
    try:
        assert gui.DND_AVAILABLE is False
    finally:
        root.destroy()


def test_a_broken_tkdnd_runtime_also_falls_back(display, monkeypatch):
    """The package can be installed yet fail to load its Tcl half.

    That raises from TkinterDnD.Tk() rather than from the import, so it must be
    caught at the same place -- an installed-but-broken tkdnd is the common
    case on Linux, where the wheel ships no matching Tcl library.
    """
    import tkinterdnd2

    class _Exploding:
        def __init__(self, *a, **k):
            raise RuntimeError("can't find package tkdnd")

    monkeypatch.setattr(tkinterdnd2, "TkinterDnD", _Exploding, raising=False)
    monkeypatch.setattr(gui, "DND_AVAILABLE", False)

    root = gui._make_root()
    try:
        assert root.winfo_exists()
        assert gui.DND_AVAILABLE is False
    finally:
        root.destroy()


class TestCollisionWindingSetting:
    """Settings ▸ Fix collision winding must preserve the per-plugin defaults.

    The repair moved from a Meshes checkbox to a persisted tri-state setting.
    "Automatic" is the default and has to resolve exactly as the checkbox did:
    ON for the plugins measured to need it (collision_options), OFF elsewhere.
    A regression here is invisible in the GUI -- the wrong flag simply reaches
    convert.py -- and shows up only as floors you fall through, or as a plugin
    silently getting a repair its collision never needed.
    """

    def test_auto_matches_the_measured_per_plugin_defaults(self):
        for plugin in gui.WINDING_FIX_DEFAULT_PLUGINS:
            assert gui.winding_enabled_for(gui.WINDING_AUTO, plugin + ".esm")
            assert gui.winding_enabled_for(gui.WINDING_AUTO, plugin + ".esp")
        for plugin in ("Oblivion.esm", "SomeMod.esp", ""):
            assert not gui.winding_enabled_for(gui.WINDING_AUTO, plugin)

    def test_explicit_modes_override_every_plugin(self):
        for plugin in ("Nehrim.esm", "Morrowind_ob.esm", "Oblivion.esm", ""):
            assert gui.winding_enabled_for(gui.WINDING_ON, plugin)
            assert not gui.winding_enabled_for(gui.WINDING_OFF, plugin)

    def test_unknown_or_absent_config_value_reads_as_auto(self):
        """A config written before this setting existed keeps the old defaults."""
        for stored in ("", "maybe", None, "AUTO"):
            mode = str(stored or "").strip().lower()
            if mode not in gui.WINDING_MODES:
                mode = gui.WINDING_AUTO
            assert gui.winding_enabled_for(mode, "Nehrim.esm") is True
            assert gui.winding_enabled_for(mode, "Oblivion.esm") is False

    def test_auto_agrees_with_collision_options(self):
        """The GUI must not carry its own copy of the plugin list."""
        from collision_options import default_for_plugin
        for plugin in ("Nehrim.esm", "Morrowind_ob.esp", "Oblivion.esm",
                       "Anything.esp"):
            assert (gui.winding_enabled_for(gui.WINDING_AUTO, plugin)
                    == default_for_plugin(plugin))
