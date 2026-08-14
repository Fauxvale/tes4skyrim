"""Resolving Bethesda-format relative paths against a real filesystem root.

Model, texture and BSA-internal paths that come out of the game's own binary
formats (MODL/ICON/TX00 subrecords, NIF texture strings, BSA folder+file
records) are ALWAYS backslash-separated, because that is the format's own
convention -- it has nothing to do with the OS the converter runs on.

`root / rel` (pathlib) and `os.path.join(root, rel)` only split on the HOST's
separator.  On Windows that happens to be a backslash, so those work by
coincidence; on Linux/Mac the whole `rel` survives as ONE filename with literal
embedded backslashes, and every lookup silently misses or every write lands in a
single flat file.  `win_join` splits explicitly instead, so a multi-segment
relative path becomes real nested directories on any platform.

This lives in its own module rather than in `lod_gen` so that the terrain-LOD,
grass and _far.nif code can share one implementation without importing a heavy
sibling module for a three-line path helper.
"""
from pathlib import Path

__all__ = ["win_join"]


def win_join(root, rel: str) -> Path:
    """Join a backslash-form (game-format) relative path onto `root`.

    Accepts either separator in `rel` and treats both as path separators, which
    is what the game's own loaders do.  Empty segments (a leading separator, or
    a doubled one) are dropped, so `rel` can never escape `root` the way
    `Path(root) / '\\abs.nif'` would.
    """
    parts = [p for p in str(rel).replace('/', '\\').split('\\') if p]
    return Path(root).joinpath(*parts)
