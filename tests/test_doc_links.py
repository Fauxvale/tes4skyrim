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


def test_repo_files_prunes_as_it_walks():
    """`rglob` visited 2,752 .py to keep 465, costing 5.9s of every run."""
    import time
    from tools.validate import code_rules as CR
    start = time.perf_counter()
    found = CR.repo_files()
    assert time.perf_counter() - start < 1.0
    assert len(found) > 100


def test_a_broken_doc_link_is_reported():
    """The doc tree is gated: a link that does not resolve must be caught."""
    from tools.validate import doc_links as DL
    index = DL.DOCS / 'README.md'
    text = index.read_text(encoding='utf-8')
    assert DL.main([]) == 0
    index.write_text(text + '\n[x](reference/definitely_not_here.md)\n',
                     encoding='utf-8', newline='')
    try:
        assert DL.main([]) == 1
    finally:
        index.write_text(text, encoding='utf-8', newline='')
    assert DL.main([]) == 0
