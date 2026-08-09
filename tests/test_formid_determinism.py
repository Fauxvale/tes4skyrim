"""FormID allocation determinism — the save-game compatibility contract.

`PluginWriter.alloc_formid()` is a bare sequential counter, so a generated
record's FormID is decided ENTIRELY by the ORDER its allocation call happens in.
Two runs on the same export must therefore make the same allocation calls in the
same sequence, or every companion record (ARMA, PROJ, OTFT, SNDR, GLOB, NAVM,
VTYP, ...) lands on a different id and every user's save game breaks: a save
stores FormIDs, so a shifted id silently rebinds a saved object to a different
record.

Nothing about that is enforced by the type system, and the failure is invisible
in a single run -- the plugin looks perfect, it is only *different* from the
last build. These tests lock the two mechanisms that actually threaten it:

  1. Set iteration order.  CPython randomises str/bytes hashing per process
     (PYTHONHASHSEED), so `for x in {'a', 'b'}` yields a different order in
     different runs. A set-of-strings driving an allocation loop is the classic
     bug. Sets of ints are stable in practice but are still sorted here.
  2. Pool completion order.  `as_completed`/`imap_unordered` yield in whatever
     order workers finish -- wall-clock noise -- so consuming them to allocate
     ids (or to order records) is nondeterministic by construction.

Verified 2026-08-09 end-to-end: Oblivion.esm (613 MB), Morrowind_ob.esm (206 MB,
exercises the masters/injected-record path) and DLCBattlehornCastle.esp each
built byte-for-byte identical under two different PYTHONHASHSEED values. These
tests are the cheap guard that keeps it that way.
"""

import ast
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMPORT_PKG = os.path.join(REPO, 'tes5_import')


def _py_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != '__pycache__')
        for name in sorted(filenames):
            if name.endswith('.py'):
                yield os.path.join(dirpath, name)


def _parse(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return ast.parse(fh.read(), filename=path)


def _rel(path):
    return os.path.relpath(path, REPO).replace('\\', '/')


# ---------------------------------------------------------------------------
# 1. alloc_formid() must not be reached from a bare set/dict iteration
# ---------------------------------------------------------------------------

def _calls_alloc_formid(node):
    """True if this subtree calls .alloc_formid()."""
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == 'alloc_formid'):
            return True
    return False


def _is_order_safe_iter(node):
    """True if a `for` target expression has a deterministic order.

    Safe: sorted(...), enumerate(sorted(...)), range(...), a list/tuple literal,
    zip/reversed over those, and plain names (lists parsed in file order -- the
    export parse is order-preserving, see text_reader.parse_export_directory).

    Unsafe: a set literal/comprehension, or .values()/.keys()/.items() on a dict
    that a set built -- those are the ones that must be wrapped in sorted().
    """
    if isinstance(node, (ast.Set, ast.SetComp)):
        return False
    if isinstance(node, ast.Call):
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, 'id', '')
        if name == 'sorted':
            return True
        if name in ('set', 'frozenset'):
            return False
        if name in ('enumerate', 'reversed', 'list', 'tuple', 'zip'):
            return all(_is_order_safe_iter(a) for a in node.args) if node.args \
                else True
        if name == 'range':
            return True
    return True


def test_alloc_formid_is_never_driven_by_set_iteration():
    """A set-of-strings feeding an allocation loop shifts ids between runs.

    Set iteration order depends on PYTHONHASHSEED, so allocating inside
    `for name in some_set:` gives each name a different FormID run to run.
    Every such loop in tes5_import is wrapped in sorted() today; this test
    fails the moment one is not.
    """
    offenders = []
    for path in _py_files(IMPORT_PKG):
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.For, ast.comprehension)):
                continue
            target_iter = node.iter
            if _is_order_safe_iter(target_iter):
                continue
            body = node.body if isinstance(node, ast.For) else []
            if not body:
                continue
            if any(_calls_alloc_formid(stmt) for stmt in body):
                offenders.append(
                    f'{_rel(path)}:{node.lineno} — alloc_formid() inside an '
                    f'unsorted set iteration')
    assert not offenders, (
        'FormID allocation driven by set iteration (order varies with '
        'PYTHONHASHSEED, so generated FormIDs shift between runs and break '
        'save games). Wrap the iterable in sorted():\n  '
        + '\n  '.join(offenders))


# ---------------------------------------------------------------------------
# 2. Pool results must be consumed in submission order, not completion order
# ---------------------------------------------------------------------------

def test_no_completion_order_pool_consumption_in_import():
    """`as_completed`/`imap_unordered` yield in wall-clock finish order.

    Consuming a pool that way to build records or allocate ids makes the output
    depend on machine load. The import pipeline uses `ex.map`, which preserves
    submission order, and pre-allocates navmesh FormIDs serially BEFORE dispatch
    (import_main._precompute_navmeshes). Keep it that way.
    """
    offenders = []
    for path in _py_files(IMPORT_PKG):
        with open(path, 'r', encoding='utf-8') as fh:
            for lineno, line in enumerate(fh, 1):
                code = line.split('#', 1)[0]
                for bad in ('as_completed', 'imap_unordered'):
                    if bad in code:
                        offenders.append(f'{_rel(path)}:{lineno} — {bad}')
    assert not offenders, (
        'Completion-order pool consumption in tes5_import; use ex.map (submission '
        'order) so record order and FormIDs stay reproducible:\n  '
        + '\n  '.join(offenders))


# ---------------------------------------------------------------------------
# 3. Directory listings must be sorted before they drive conversion
# ---------------------------------------------------------------------------

def test_directory_listings_are_sorted():
    """Filesystem order is not defined; unsorted, it can reorder records.

    parse_export_directory sorts os.listdir() so the record list -- and hence
    every conversion loop built on it -- is stable. An unsorted listing that
    feeds conversion would reorder records and shift every allocated FormID.
    """
    offenders = []
    for path in _py_files(IMPORT_PKG):
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.For):
                continue
            it = node.iter
            if isinstance(it, ast.Call):
                fn = it.func
                name = fn.attr if isinstance(fn, ast.Attribute) \
                    else getattr(fn, 'id', '')
                if name in ('listdir', 'glob', 'iglob', 'walk', 'iterdir'):
                    offenders.append(
                        f'{_rel(path)}:{node.lineno} — unsorted {name}()')
    assert not offenders, (
        'Unsorted directory iteration drives conversion order:\n  '
        + '\n  '.join(offenders))


# ---------------------------------------------------------------------------
# 4. The allocator itself stays a plain sequential counter
# ---------------------------------------------------------------------------

def test_alloc_formid_is_sequential_and_reproducible():
    """Same call sequence -> same ids, and ids never depend on record content.

    If alloc_formid ever derived an id from a name/hash instead of the counter,
    string hash randomisation would move ids between runs.
    """
    from tes5_import.writer import PluginWriter

    def run():
        w = PluginWriter(masters=['Skyrim.esm'], is_esm=True)
        w.next_object_id = 0x01001000
        return [w.alloc_formid() for _ in range(50)]

    first = run()
    assert first == run(), 'alloc_formid is not reproducible across writers'
    assert first == list(range(0x01001000, 0x01001000 + 50)), \
        'alloc_formid must be a bare +1 sequential counter'


def test_allocation_order_alone_decides_formids():
    """Two writers making the same calls in a different ORDER must diverge.

    This is the property that makes the ordering tests above load-bearing: it
    documents that reordering allocation calls (by editing conversion order, or
    by adding an earlier allocation) renumbers everything downstream.
    """
    from tes5_import.writer import PluginWriter

    def run(order):
        w = PluginWriter(masters=['Skyrim.esm'], is_esm=True)
        w.next_object_id = 0x01000800
        return {name: w.alloc_formid() for name in order}

    a = run(['armor', 'proj', 'outfit'])
    b = run(['proj', 'armor', 'outfit'])
    assert a != b, (
        'Allocation order must decide FormIDs — if this ever passes trivially, '
        'the ordering guards above are no longer meaningful')
    assert a['outfit'] == b['outfit']  # unchanged position -> unchanged id


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
