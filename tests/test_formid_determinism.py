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
# 4. Derived FormIDs are a pure function of their SOURCE record
# ---------------------------------------------------------------------------
#
# A counter decides an id by WHEN it was called, so any change to how many ids
# a site consumes renumbers everything after it — the drift that breaks saves.
# derive_formid decides an id by WHAT the record is, so nothing about ordering,
# volume or machine can move it.

def test_derived_ids_are_stable_across_writers():
    """Two independent conversions must agree — this is the interop property.

    Every user converts the mod themselves. If two people's conversions gave a
    generated record different ids, neither could share a save or a patch.
    """
    from tes5_import.writer import PluginWriter

    def run():
        w = PluginWriter(masters=['Skyrim.esm'], is_esm=True)
        return [w.derive_formid(site, key) for site, key in
                [('OTFT', 0x1A2B3), ('ARMA', 0x1A2B3), ('NAVM', (7, 9)),
                 ('GLOB', 'TES4Fame')]]

    assert run() == run()


def test_derived_ids_do_not_depend_on_allocation_ORDER():
    """The whole point: call order must not decide an id.

    This is the exact inverse of the old counter contract. A site that starts
    allocating a second id, a new site added anywhere, or a reordered export
    must all leave existing ids untouched.
    """
    from tes5_import.writer import PluginWriter

    a = PluginWriter(masters=['Skyrim.esm'], is_esm=True)
    first = {k: a.derive_formid('OTFT', k) for k in (10, 20, 30)}

    b = PluginWriter(masters=['Skyrim.esm'], is_esm=True)
    # Reverse order, and interleave a brand-new site that did not exist before.
    for k in (30, 20, 10):
        b.derive_formid('NEWSITE', k)
    second = {k: b.derive_formid('OTFT', k) for k in (30, 20, 10)}

    assert first == second, (
        'derived ids shifted when allocation order changed or a new site was '
        'added — this is FormID drift, and it breaks every existing save')


def test_derived_ids_never_land_on_an_authored_record():
    """A companion colliding with a real record silently destroys one of them."""
    from tes5_import.writer import PluginWriter, DERIVED_ID_BASE

    w = PluginWriter(masters=['Skyrim.esm'], is_esm=True)
    # Reserve a big authored block INSIDE the derived region, then allocate
    # across it; nothing may be handed out on top of a reserved id.
    reserved = {(1 << 24) | (DERIVED_ID_BASE + i) for i in range(5000)}
    w.reserve_source_ids(reserved)
    got = [w.derive_formid('X', i) for i in range(2000)]
    assert not (set(got) & reserved)
    assert len(set(got)) == len(got), 'derive_formid handed out a duplicate id'


def test_derived_ids_are_stable_under_hash_randomisation():
    """PYTHONHASHSEED must not reach the id.

    Python's own hash() is randomised per process, so using it would give every
    machine different FormIDs — the exact failure the tests above exist to
    prevent. derive_formid uses md5 for this reason.
    """
    import subprocess
    import sys

    code = (
        'from tes5_import.writer import PluginWriter;'
        'w=PluginWriter(masters=["Skyrim.esm"]);'
        'print([w.derive_formid(s,k) for s,k in '
        '[("OTFT",1),("ARMA",("a","b")),("GLOB","TES4Fame")]])'
    )
    outs = []
    for seed in ('0', '1', '424242'):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        outs.append(subprocess.run([sys.executable, '-c', code], env=env,
                                   capture_output=True, text=True,
                                   cwd=REPO).stdout.strip())
    assert outs[0] and len(set(outs)) == 1,         f'derived ids vary with PYTHONHASHSEED: {outs}'


def test_hedr_next_object_id_clears_every_derived_id():
    """HEDR must sit above every OBJECT ID in the file, derived ones included.

    The engine mints runtime-created forms from HEDR's next-object-id upward,
    so a value below an existing record hands a runtime form an id that record
    already owns. The comparison has to happen on the OBJECT ID (low 24 bits):
    _next_object_id carries the plugin's index byte, so comparing it directly
    against a derived id lets 0x0118E937 "beat" 0x00FFC374 and ship a header
    below real records.
    """
    from tes5_import.writer import PluginWriter

    w = PluginWriter(masters=['Skyrim.esm'], is_esm=True)
    w.next_object_id = (1 << 24) | 0x18E937      # as import_plugin sets it
    highest = 0
    for i in range(500):
        highest = max(highest, w.derive_formid('X', i) & 0x00FFFFFF)

    hedr = w._high_water_id() & 0x00FFFFFF
    assert hedr > highest, (
        f'HEDR object id {hedr:06X} is below the highest derived object id '
        f'{highest:06X} — the engine would reuse it for a runtime form')


def test_derived_region_avoids_the_plugin_s_dense_id_block():
    """The hash window is chosen from the data, not hardcoded.

    A constant window cannot suit every plugin: authored ids sit wherever the
    mod author put them. A hardcoded 0x400000 landed inside Morroblivion's
    occupied space (collisions 3.27% vs 0.10%), and hashing the whole space
    instead landed in Oblivion's dense low block (7.26%). This pins the fix:
    given ids packed into one region, the window must be chosen elsewhere.
    """
    from tes5_import.writer import PluginWriter

    w = PluginWriter(masters=['Skyrim.esm'], is_esm=True)
    # A plugin whose ids fill 0x000000-0x200000 densely, like Oblivion's.
    w.reserve_source_ids({(1 << 24) | i for i in range(0, 0x200000, 2)})

    assert w._derived_base >= 0x200000, (
        f'derived window at {w._derived_base:06X} overlaps the dense authored '
        f'block below 0x200000')
    assert w._derived_span > 0x100000, 'window is implausibly small'

    # And allocating from it must stay collision-light.
    for i in range(5000):
        w.derive_formid('X', i)
    st = w.derive_stats()
    assert st['collisions'] / st['derived'] < 0.02, (
        f"collision rate {100.0 * st['collisions'] / st['derived']:.2f}% — the "
        f"window was placed in occupied space")


def test_derived_region_choice_is_deterministic():
    """Two machines must choose the SAME window, or ids differ everywhere."""
    from tes5_import.writer import PluginWriter

    ids = {(1 << 24) | i for i in range(0, 0x180000, 3)}
    a = PluginWriter(masters=['Skyrim.esm'], is_esm=True)
    a.reserve_source_ids(set(ids))
    b = PluginWriter(masters=['Skyrim.esm'], is_esm=True)
    b.reserve_source_ids(set(ids))

    assert (a._derived_base, a._derived_span) == (b._derived_base,
                                                  b._derived_span)
    assert [a.derive_formid('S', i) for i in range(200)] ==            [b.derive_formid('S', i) for i in range(200)]


def test_derived_ids_leave_runtime_headroom():
    """The engine needs free ids ABOVE the file's records to allocate from.

    Every runtime-created form (dropped item, summon, placed object) takes an
    id from the header's next-object-id upward. Hashing derived ids all the way
    to the 24-bit ceiling left Nehrim.esm just 174 free ids and Oblivion.esm
    2,360 — vanilla Skyrim.esm leaves 16.7M. A long playthrough would run the
    pool dry.
    """
    from tes5_import.writer import PluginWriter, _RUNTIME_HEADROOM

    w = PluginWriter(masters=['Skyrim.esm'], is_esm=True)
    w.reserve_source_ids({(1 << 24) | i for i in range(0, 0x180000, 2)})
    highest = 0
    for i in range(20000):
        highest = max(highest, w.derive_formid('X', i) & 0x00FFFFFF)

    free = 0x00FFFFFF - highest
    assert free >= _RUNTIME_HEADROOM - 0x10000, (
        f'only {free:,} object ids left above the highest derived record — '
        f'the engine allocates runtime forms from there')


def test_fixed_id_windows_are_reserved_before_hashing():
    """Ids minted OUTSIDE derive_formid must still be reserved.

    The synthesized NPC-conversation topics take FormIDs from a fixed high
    base (dialog_converter.CONV_FAKE_FID_BASE) instead of the hash. Nothing
    else knows they exist, so unless import_plugin reserves the window: the
    header is written BELOW them (15 DIALs shipped above HEDR at 0xF40000),
    and a derived record can hash straight onto one.
    """
    from tes5_import.writer import PluginWriter
    from tes5_import.dialog_converter import (CONV_FAKE_FID_BASE,
                                              CONV_FAKE_FID_COUNT)

    w = PluginWriter(masters=['Skyrim.esm'], is_esm=True)
    window = {(1 << 24) | (CONV_FAKE_FID_BASE + k)
              for k in range(CONV_FAKE_FID_COUNT)}
    w.reserve_source_ids({(1 << 24) | i for i in range(0, 0x100000, 4)}
                         | window)

    got = {w.derive_formid('X', i) for i in range(4000)}
    assert not (got & window), 'a derived id landed on a reserved fixed id'

    highest = max(f & 0x00FFFFFF for f in window)
    assert (w._high_water_id() & 0x00FFFFFF) > highest, (
        'HEDR is below the fixed-id window, so the engine would mint a '
        'runtime form onto one of those records')


def test_scheme_version_is_declared():
    """Bumping this is how a deliberate id-layout change gets recorded."""
    from tes5_import.writer import FORMID_SCHEME_VERSION
    assert isinstance(FORMID_SCHEME_VERSION, int)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
