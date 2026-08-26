"""Debug / maintenance utilities.

This file exists so `tools` is a REGULAR package rather than a namespace one.

Without it, `import tools.navmesh.navmesh_cache` resolved only via PEP 420 implicit
namespace packages, which requires the repo root to be on sys.path and nothing
else named `tools` to shadow it -- neither guaranteed when the GUI is launched
by double-clicking gui.pyw (pythonw sets no console, so the resulting
ImportError was silent and the window just never opened).

Most modules live in a category subfolder and also run as
`python tools/<folder>/<name>.py`.  That entry point puts the SUBFOLDER -- not
the repo root -- on sys.path, so they each re-insert the root themselves (three
levels up); keep this file free of imports so it stays cheap and cannot fail.
"""
