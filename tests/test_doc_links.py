"""Links into `docs/` must resolve, and every doc must be indexed.

A move under `docs/` is only safe if this was green before it and after it.
"""

from tools.validate import doc_links


def test_every_doc_link_resolves():
    """No link into or between docs/ may point at a missing file or anchor."""
    broken = [(str(src), num, target, why)
              for src in doc_links.sources()
              for num, target, why in doc_links._broken(src)]
    assert broken == []


def test_every_doc_is_indexed():
    """A doc absent from docs/README.md is one nobody will find."""
    assert [str(p) for p in doc_links.unindexed()] == []
