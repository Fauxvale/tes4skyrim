#!/usr/bin/env python3
"""Repo-wide dead-code scanner.

Finds module-level functions, classes and constants that are defined but never
referenced anywhere in the repository, plus modules nothing ever imports.

Purely AST + name-usage based, so it is fast (no imports, no execution) and safe
to run on the full tree. Because Python is dynamic the results are *candidates*:
a name reached only through ``getattr``, a string dispatch table or an
entry-point is still reported. The scanner suppresses the cases it can see
(dunders, ``__all__`` exports, decorated entry points, ``test_*``) and flags any
name that also appears inside a string literal, but the final call is yours.

Usage::

    python tools/audit/dead_code_scan.py                      # whole repo
    python tools/audit/dead_code_scan.py --path tes5_import   # report one package
    python tools/audit/dead_code_scan.py --kind function      # only functions
    python tools/audit/dead_code_scan.py --min-lines 15       # only large bodies
    python tools/audit/dead_code_scan.py --modules            # unimported modules
    python tools/audit/dead_code_scan.py --json temp/dead.json
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_EXCLUDES = {
    "references", "temp", "export", "output", "logs", "__pycache__",
    ".git", "external", "navmesh_cache", "build", "dist", ".venv", "venv",
    "site-packages",
}


def iter_py_files(root: Path, excludes: set[str]):
    for p in root.rglob("*.py"):
        if any(part in excludes for part in p.parts):
            continue
        yield p


class Definitions(ast.NodeVisitor):
    """Collect top-level (and class-level) definitions in one file."""

    def __init__(self, path: Path):
        self.path = path
        self.defs: list[dict] = []
        self.all_exports: set[str] = set()
        self._class_stack: list[str] = []
        self._depth = 0

    def _record(self, node, kind: str, name: str):
        decorators = [ast.unparse(d) for d in getattr(node, "decorator_list", [])]
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        self.defs.append({
            "name": name,
            "kind": kind,
            "file": str(self.path),
            "line": node.lineno,
            "lines": end - node.lineno + 1,
            "in_class": bool(self._class_stack),
            "decorators": decorators,
        })

    def visit_FunctionDef(self, node):
        if self._depth == 0:
            self._record(node, "method" if self._class_stack else "function", node.name)
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        if self._depth == 0:
            self._record(node, "class", node.name)
        self._class_stack.append(node.name)
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1
        self._class_stack.pop()

    def visit_Assign(self, node):
        if self._depth == 0 and not self._class_stack:
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    if tgt.id == "__all__":
                        try:
                            self.all_exports.update(ast.literal_eval(node.value))
                        except Exception:
                            pass
                    elif tgt.id.isupper():
                        self._record(node, "constant", tgt.id)
        self.generic_visit(node)


NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def is_test_file(rel: str) -> bool:
    """True for anything under tests/ or named test_*.py / conftest.py."""
    parts = Path(rel).as_posix().split("/")
    if "tests" in parts:
        return True
    name = parts[-1]
    return name.startswith("test_") or name == "conftest.py"


def is_tool_file(rel: str) -> bool:
    """True for anything under tools/ -- debug/analysis scripts, not pipeline."""
    return "tools" in Path(rel).as_posix().split("/")


def consumer_tier(rel: str) -> str:
    """Which tier of the codebase a referencing file belongs to.

    ``pipeline`` is the shipped product; ``tools`` and ``tests`` are
    peripheral. A definition kept alive ONLY by a peripheral tier is a
    finding, not a use.
    """
    if is_test_file(rel):
        return "tests"
    if is_tool_file(rel):
        return "tools"
    return "pipeline"


class UsageCollector(ast.NodeVisitor):
    """Record every identifier *use*, skipping the ``def``/``class`` name itself.

    A definition's own name node is not a use of it, so a function that is
    defined and never called anywhere -- including inside its own module --
    ends up with zero recorded uses.
    """

    def __init__(self, key: str, usage: dict[str, set[str]], strings: set[str]):
        self.key = key
        self.usage = usage
        self.strings = strings

    def _visit_def(self, node):
        # Decorators, defaults, annotations and the body ARE uses; the name is not.
        for d in node.decorator_list:
            self.visit(d)
        for child in ast.iter_child_nodes(node):
            if child not in node.decorator_list:
                self.visit(child)

    visit_FunctionDef = _visit_def
    visit_AsyncFunctionDef = _visit_def
    visit_ClassDef = _visit_def

    def visit_Assign(self, node):
        # The assigned-to Name is a definition, not a use; the value is a use.
        self.visit(node.value)
        for tgt in node.targets:
            if not isinstance(tgt, ast.Name):
                self.visit(tgt)

    def visit_Name(self, node):
        self.usage[node.id].add(self.key)

    def visit_Attribute(self, node):
        self.usage[node.attr].add(self.key)
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            for tok in NAME_RE.findall(node.value):
                self.strings.add(tok)

    def visit_ImportFrom(self, node):
        for a in node.names:
            self.usage[a.name].add(self.key)
            if a.asname:
                self.usage[a.asname].add(self.key)

    def visit_Import(self, node):
        for a in node.names:
            self.usage[a.name.split(".")[0]].add(self.key)


def collect_usage(files: list[Path], root: Path):
    """Map identifier -> set of files USING it, plus names seen in strings."""
    usage: dict[str, set[str]] = defaultdict(set)
    string_names: set[str] = set()
    for path in files:
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        key = str(path.relative_to(root))
        try:
            tree = ast.parse(src)
        except SyntaxError:
            for tok in NAME_RE.findall(src):
                usage[tok].add(key)
            continue
        UsageCollector(key, usage, string_names).visit(tree)
    return usage, string_names


ENTRY_DECORATORS = (
    "property", "setter", "deleter", "staticmethod", "classmethod",
    "abstractmethod", "override", "overload", "register", "fixture",
    "pytest", "click", "command", "hookimpl", "cached_property",
    "functools", "singledispatch", "atexit",
)


def is_exempt(d: dict, all_exports: dict[str, set[str]]) -> str | None:
    name = d["name"]
    if name.startswith("__") and name.endswith("__"):
        return "dunder"
    if name == "main":
        return "entry point"
    if is_test_file(d["file"]):
        return "test module"
    if name.startswith("test_"):
        return "test"
    for dec in d["decorators"]:
        if any(e in dec for e in ENTRY_DECORATORS):
            return f"decorated @{dec}"
    if name in all_exports.get(d["file"], set()):
        return "__all__ export"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="repo root to scan (default: .)")
    ap.add_argument("--path", action="append", default=[],
                    help="restrict REPORTING to definitions under this path; "
                         "usage is still collected repo-wide. Repeatable.")
    ap.add_argument("--kind", action="append", default=[],
                    choices=["function", "class", "method", "constant"],
                    help="restrict to these definition kinds. Repeatable.")
    ap.add_argument("--min-lines", type=int, default=1,
                    help="only report definitions of at least this many lines")
    ap.add_argument("--exclude", action="append", default=[],
                    help="extra directory name to exclude. Repeatable.")
    ap.add_argument("--include-methods", action="store_true",
                    help="include class methods (noisy: duck typing, overrides)")
    ap.add_argument("--no-strings-are-uses", dest="strings_are_uses",
                    action="store_false", default=True,
                    help="do not flag names that appear in string literals")
    ap.add_argument("--modules", action="store_true",
                    help="also report modules never imported by name anywhere")
    ap.add_argument("--count-tests", dest="ignore_tests", action="store_false",
                    default=True,
                    help="treat a use inside tests/ as a real use. Off by "
                         "default: a function only a TEST calls is still dead "
                         "production code, and the test dies with it.")
    ap.add_argument("--count-tools", dest="ignore_tools", action="store_false",
                    default=True,
                    help="treat a use inside tools/ as a real use. Off by "
                         "default: a PIPELINE function whose only caller is a "
                         "debug tool means either the tool is stale or the "
                         "pipeline lost its call site.")
    ap.add_argument("--json", help="write full results to this JSON file")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    excludes = DEFAULT_EXCLUDES | set(args.exclude)
    files = sorted(iter_py_files(root, excludes))
    print(f"scanning {len(files)} python files under {root}", flush=True)

    all_defs: list[dict] = []
    all_exports: dict[str, set[str]] = {}
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            print(f"  !! syntax error, skipped: {path} ({exc})", flush=True)
            continue
        rel = path.relative_to(root)
        collector = Definitions(rel)
        collector.visit(tree)
        all_defs.extend(collector.defs)
        all_exports[str(rel)] = collector.all_exports

    print(f"collected {len(all_defs)} definitions", flush=True)
    usage, string_names = collect_usage(files, root)
    print(f"collected {len(usage)} distinct referenced identifiers", flush=True)

    report_roots = [Path(p).as_posix().rstrip("/") for p in args.path]
    kinds = set(args.kind) if args.kind else None

    dead: list[dict] = []
    for d in all_defs:
        if d["kind"] == "method" and not args.include_methods:
            continue
        if kinds and d["kind"] not in kinds:
            continue
        if d["lines"] < args.min_lines:
            continue
        fposix = Path(d["file"]).as_posix()
        if report_roots and not any(fposix == r or fposix.startswith(r + "/")
                                    for r in report_roots):
            continue
        if is_exempt(d, all_exports):
            continue
        refs = usage.get(d["name"], set())
        by_tier: dict[str, set[str]] = defaultdict(set)
        for r in refs:
            by_tier[consumer_tier(r)].add(r)

        # Which tiers count as keeping a definition alive. A peripheral tier
        # that is NOT counted still gets reported -- with its callers named, so
        # you can tell "delete both" from "the pipeline lost its call site".
        live_tiers = {"pipeline"}
        if not args.ignore_tests:
            live_tiers.add("tests")
        if not args.ignore_tools:
            live_tiers.add("tools")
        if any(by_tier.get(t) for t in live_tiers):
            continue

        own_tier = consumer_tier(d["file"])
        keepers = [t for t in ("tests", "tools") if by_tier.get(t)]
        if keepers:
            # A helper called from its OWN tier is a normal intra-tier call, not
            # a finding -- only cross-tier life support is. Checked by
            # membership, not equality: a tools/ helper used by tools/ AND
            # tests/ is still just a live tools/ helper. (Equality here made
            # every tools/ helper that also has a test look dead once tools/
            # was split into subpackages.)
            if own_tier != "pipeline" and own_tier in keepers:
                continue
            label = " AND ".join(t.upper() for t in keepers)
            callers = sorted({c for t in keepers for c in by_tier[t]})
            d["note"] = (f"USED ONLY BY {label} ({len(callers)}): "
                         + ", ".join(callers[:3])
                         + (" ..." if len(callers) > 3 else ""))
        elif args.strings_are_uses and d["name"] in string_names:
            d["note"] = "name appears in a string literal"

        d["private"] = d["name"].startswith("_")
        d["own_tier"] = own_tier
        d["kept_alive_by"] = keepers
        dead.append(d)

    dead.sort(key=lambda d: (-d["lines"], d["file"], d["line"]))

    by_file: dict[str, list[dict]] = defaultdict(list)
    for d in dead:
        by_file[d["file"]].append(d)

    print()
    print("=" * 78)
    print(f"{len(dead)} unreferenced definitions in {len(by_file)} files")
    print("=" * 78)
    for f in sorted(by_file, key=lambda f: -sum(d["lines"] for d in by_file[f])):
        items = by_file[f]
        print(f"\n{f}  ({sum(d['lines'] for d in items)} lines across {len(items)})")
        for d in sorted(items, key=lambda d: d["line"]):
            note = f"   [{d['note']}]" if d.get("note") else ""
            print(f"  L{d['line']:>5}  {d['lines']:>4}L  {d['kind']:<9} {d['name']}{note}")

    result = {"dead": dead}

    if args.modules:
        imported: set[str] = set()
        for path in files:
            src = path.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        imported.update(a.name.split("."))
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imported.update(node.module.split("."))
                    for a in node.names:
                        imported.add(a.name)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for tok in NAME_RE.findall(node.value):
                        imported.add(tok)
        orphans = []
        for path in files:
            rel = path.relative_to(root)
            stem = rel.stem
            if stem in ("__init__", "__main__", "conftest") or stem.startswith("test_"):
                continue
            if stem in imported:
                continue
            src = path.read_text(encoding="utf-8", errors="replace")
            orphans.append({"file": str(rel), "lines": src.count("\n") + 1,
                            "has_main_guard": "__main__" in src})
        orphans.sort(key=lambda o: -o["lines"])
        print()
        print("=" * 78)
        print(f"{len(orphans)} modules never imported by name anywhere")
        print("  (a __main__ guard means standalone script, not necessarily dead)")
        print("=" * 78)
        for o in orphans:
            tag = "script" if o["has_main_guard"] else "ORPHAN?"
            print(f"  {tag:<8} {o['lines']:>5}L  {o['file']}")
        result["orphan_modules"] = orphans

    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
