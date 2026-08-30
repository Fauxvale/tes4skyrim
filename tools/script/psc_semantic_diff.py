#!/usr/bin/env python3
"""Compare generated Papyrus by WHAT IT DOES, not by its text.

`script_convert/` is being rewritten around the parse tree.  A rewrite emits
different TEXT for the same behaviour -- different spacing, different temporary
variable names, a different property order -- so `psc_corpus_diff`'s byte
comparison reports every one of the ~40,000 files as changed on day one and is
useless as the safety net for this work.  (It stays the right tool for a change
that is SUPPOSED to be byte-identical.)

This tool extracts a normalised MODEL from each script and diffs the models:

    properties  name -> Papyrus type      the VMAD binding contract
    locals      name -> Papyrus type      script-scope declarations
    events      names defined             Fragment_0/1 presence lives here
    calls       callee(arity) -> count    including TES4Polyfill.*
    literals    string/number literals    catches a changed timer or stage
    writes      assignment targets
    extends     base script + flags

What it deliberately IGNORES: whitespace, indentation, comment text, the order
of declarations, the spelling of temporaries, and how an expression is
parenthesised.  What it deliberately KEEPS: every call with its ARGUMENT COUNT
(dropping an argument is a behaviour change), every literal (a changed Say
duration or quest stage is a behaviour change), and every property type string
(the importer picks base-vs-reference FormIDs off that literal).

    python tools/script/psc_semantic_diff.py snapshot --all
    ... rewrite, rebuild ...
    python tools/script/psc_semantic_diff.py compare --all --show 20

Exit code is non-zero when any model differs, so it can gate a build.
"""

import argparse
import json
import os
import re
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

OUTPUT_DIR = ROOT / 'output'
DEFAULT_BASELINE = ROOT / 'temp' / 'psc_semantic'

#: Play-tested scripts; a model change blocks whatever the overall count says.
DEFAULT_GATE = [
    'TES4_CharGenQuest.psc',
    'TES4_CGEmperorScript.psc',
    'TES4_BaurusScript.psc',
    'TES4_ValenDrethScript.psc',
    'TES4_Dark04ValenDrethScript.psc',
    'TES4_Dark04ValenDrethGate.psc',
]

#: The opening dungeon by prefix; a name list went stale at 6 of 52 (ledger 18).
DEFAULT_GATE_PREFIX = ['TES4_CG']

_SCRIPTNAME = re.compile(
    r'^\s*ScriptName\s+(\w+)(?:\s+extends\s+(\w+))?(.*)$', re.IGNORECASE)
_PROPERTY = re.compile(
    r'^\s*([A-Za-z_]\w*(?:\[\])?)\s+Property\s+(\w+)\s*'
    r'(?:=\s*(\S+))?', re.IGNORECASE)
_HEADER = re.compile(
    r'^\s*(?:\w+(?:\[\])?\s+)?(?:Event|Function)\s+(\w+)\s*\(', re.IGNORECASE)
_LOCAL = re.compile(
    r'^\s*(Int|Float|Bool|String|Form|ObjectReference|Actor|Quest|'
    r'GlobalVariable|Topic|Package|Faction|Spell|Message|Sound|Weather|'
    r'FormList|EffectShader|ActorBase|MiscObject|Ingredient|Potion|Weapon|'
    r'Armor|Book|Key|Cell|WorldSpace|MusicType|TES4_\w+)(?:\[\])?\s+(\w+)\s*'
    r'(?:=|$)', re.IGNORECASE)
_ASSIGN = re.compile(r'^\s*([\w.]+(?:\s+as\s+\w+)?)\s*=(?!=)')
_STRING = re.compile(r'"([^"]*)"')
_NUMBER = re.compile(r'(?<![\w.])(-?\d+(?:\.\d+)?)(?![\w.])')

# `Foo.Bar(` or bare `Bar(`. The receiver is kept only when it is a NAME, not
# an expression, because `(x as Actor).Say(t)` and `x.Say(t)` are the same call
# and the cast is an emission detail the rewrite is allowed to change.
_CALL = re.compile(r'(?:(?<![\w.])([A-Za-z_]\w*)\s*\.\s*)?([A-Za-z_]\w*)\s*\(')

# Papyrus keywords that look like calls but are not.
_NOT_CALLS = frozenset({
    'if', 'elseif', 'while', 'return', 'property', 'event', 'function',
    'endif', 'endwhile', 'endevent', 'endfunction', 'else', 'as', 'new',
})


def _strip_comment(line: str) -> str:
    """Drop a trailing `;` comment, respecting string literals."""
    out, in_str = [], False
    for ch in line:
        if ch == '"':
            in_str = not in_str
        elif ch == ';' and not in_str:
            break
        out.append(ch)
    return ''.join(out)


def _arity(text: str, open_paren: int) -> int:
    """Argument count of the call whose `(` is at `open_paren`.

    Counts top-level commas, so a nested call contributes one argument rather
    than its own.  Returns -1 when the parens do not close on this line (a
    multi-line call), which keeps the model stable rather than guessing.
    """
    depth, args, seen, in_str = 0, 1, False, False
    for ch in text[open_paren:]:
        if in_str:
            in_str = ch != '"'
            continue
        if ch == '"':
            in_str, seen = True, True     # a literal occupies an argument slot
        elif ch == '(':
            depth += 1
            # A nested `(` occupies this slot too: `f((x as Actor), y)` takes
            # two arguments, not zero.
            seen = seen or depth > 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return args if seen else 0
        elif ch == ',' and depth == 1:
            args += 1
        elif depth == 1 and not ch.isspace():
            seen = True
    return -1


def model(text: str) -> dict:
    """Normalised behavioural model of one generated script."""
    props: dict = {}
    locals_: dict = {}
    events: list = []
    calls: dict = {}
    writes: list = []
    strings: dict = {}
    numbers: dict = {}
    extends = ''

    in_doc = False
    for raw in text.split('\n'):
        stripped = raw.strip()
        # `{ ... }` doc comments carry no behaviour.
        if in_doc:
            if '}' in stripped:
                in_doc = False
            continue
        if stripped.startswith('{'):
            if '}' not in stripped:
                in_doc = True
            continue
        if not stripped or stripped.startswith(';'):
            continue

        m = _SCRIPTNAME.match(stripped)
        if m:
            # Flags (`Hidden`, `Conditional`) are behavioural; their order is not.
            flags = ' '.join(sorted(m.group(3).split()))
            extends = f'{m.group(2) or ""} {flags}'.strip()
            continue

        code = _strip_comment(raw)
        low = code.strip()
        if not low:
            continue

        m = _PROPERTY.match(low)
        if m:
            # The DEFAULT is part of the contract: `Float Property timer = 0.0`
            # and `= 0.99` are different behaviour, and a property line is the
            # one place a literal does not reach the `numbers` scan below.
            default = m.group(3) or ''
            if default:
                default = f'{float(default):g}' if _NUMBER.fullmatch(
                    default) else default
            props[m.group(2).lower()] = f'{m.group(1)}={default}' if default \
                else m.group(1)
            continue

        m = _HEADER.match(low)
        if m:
            events.append(m.group(1).lower())
            continue

        m = _LOCAL.match(low)
        if m and not low.lower().startswith(('if ', 'while ', 'return ')):
            locals_[m.group(2).lower()] = m.group(1)

        m = _ASSIGN.match(low)
        if m:
            writes.append(m.group(1).split(' as ')[0].strip().lower())

        for cm in _CALL.finditer(low):
            name = cm.group(2).lower()
            if name in _NOT_CALLS:
                continue
            recv = (cm.group(1) or '').lower()
            # A local temporary's name is an emission detail, but a call ON a
            # property or a known class is not -- keep the receiver only when
            # it names something declared.
            qualifier = f'{recv}.' if recv in props or recv in (
                'game', 'debug', 'utility', 'tes4polyfill', 'math',
                'input', 'weather', 'self') else ''
            key = f'{qualifier}{name}/{_arity(low, cm.end() - 1)}'
            calls[key] = calls.get(key, 0) + 1

        for s in _STRING.findall(low):
            strings[s] = strings.get(s, 0) + 1
        for n in _NUMBER.findall(_STRING.sub('""', low)):
            # Normalise `5` and `5.0` to one key: Papyrus promotes freely and
            # the rewrite may spell a float either way.
            key = f'{float(n):g}'
            numbers[key] = numbers.get(key, 0) + 1

    return {
        'extends': extends,
        'properties': props,
        'locals': locals_,
        'events': sorted(events),
        'calls': calls,
        'writes': sorted(set(writes)),
        'strings': strings,
        'numbers': numbers,
    }


def plugin_dirs(names=None, use_all=False):
    """[(plugin_name, scripts_source_dir)] for plugins with generated .psc."""
    if not OUTPUT_DIR.is_dir():
        raise SystemExit(f'no output directory: {OUTPUT_DIR}')
    out = []
    for child in sorted(OUTPUT_DIR.iterdir()):
        src = child / 'scripts' / 'source'
        if child.is_dir() and src.is_dir() and (
                use_all or not names or child.name in names):
            out.append((child.name, src))
    if names and not use_all:
        missing = set(names) - {n for n, _ in out}
        if missing:
            raise SystemExit(f'no generated scripts for: {", ".join(sorted(missing))}')
    if not out:
        raise SystemExit('no plugins with generated scripts under output/')
    return out


def _model_file(path):
    return model(path.read_text(encoding='utf-8', errors='replace'))


def scan_plugin(src_dir, workers):
    """{relative_name: model} for every .psc under src_dir."""
    files = sorted(src_dir.rglob('*.psc'))
    if not files:
        return {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        models = list(ex.map(_model_file, files))
    return {str(p.relative_to(src_dir)).replace('\\', '/'): m
            for p, m in zip(files, models, strict=True)}


def baseline_paths(baseline, plugin):
    safe = plugin.replace(os.sep, '_')
    return Path(baseline) / f'{safe}.json.zip'


def _write(archive, payload):
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('models.json', json.dumps(payload))


def _read(archive):
    if not archive.is_file():
        raise SystemExit(f'no baseline at {archive} -- run `snapshot` first')
    with zipfile.ZipFile(archive) as zf:
        return json.loads(zf.read('models.json'))


def cmd_snapshot(args):
    total = 0
    for plugin, src in plugin_dirs(args.plugin, args.all):
        models = scan_plugin(src, args.workers)
        _write(baseline_paths(args.baseline, plugin),
               {'plugin': plugin, 'models': models})
        print(f'  {plugin:32} {len(models):6} scripts')
        total += len(models)
    print(f'\nsnapshot: {total} models in {args.baseline}')
    return 0


def _diff_model(old: dict, new: dict) -> list:
    """Human-readable differences between two models, field by field."""
    out = []
    for field in ('extends', 'properties', 'locals', 'events', 'calls',
                  'writes', 'strings', 'numbers'):
        a, b = old.get(field), new.get(field)
        if a == b:
            continue
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) | set(b)):
                if a.get(k) != b.get(k):
                    out.append(f'    {field}[{k}]: {a.get(k)!r} -> {b.get(k)!r}')
        elif isinstance(a, list) and isinstance(b, list):
            for k in sorted(set(a) ^ set(b)):
                out.append(f'    {field}: {"-" if k in a else "+"}{k}')
        else:
            out.append(f'    {field}: {a!r} -> {b!r}')
    return out


def _is_gated(basename: str, gate, prefixes) -> bool:
    """True when this script is play-tested and any change is a regression."""
    return basename in gate or any(basename.startswith(p) for p in prefixes)


def cmd_compare(args):
    gate = args.gate if args.gate is not None else DEFAULT_GATE
    prefixes = (args.gate_prefix if args.gate_prefix is not None
                else DEFAULT_GATE_PREFIX)
    shown = 0
    tot_changed = tot_added = tot_removed = 0
    gated_hits = []

    for plugin, src in plugin_dirs(args.plugin, args.all):
        old = _read(baseline_paths(args.baseline, plugin))['models']
        new = scan_plugin(src, args.workers)

        changed = sorted(n for n in old.keys() & new.keys() if old[n] != new[n])
        added = sorted(new.keys() - old.keys())
        removed = sorted(old.keys() - new.keys())
        tot_changed += len(changed)
        tot_added += len(added)
        tot_removed += len(removed)

        flag = '' if not (changed or added or removed) else '  <-- differs'
        print(f'  {plugin:32} {len(new):6} scripts  '
              f'changed {len(changed):5}  added {len(added):5}  '
              f'removed {len(removed):5}{flag}')

        gated_hits += [f'{plugin}/{n}' for n in changed + added + removed
                       if _is_gated(os.path.basename(n), gate, prefixes)]

        for name in changed:
            if shown >= args.show:
                break
            print(f'\n  {plugin}/{name}')
            for line in _diff_model(old[name], new[name])[:args.lines]:
                print(line)
            shown += 1

    total = tot_changed + tot_added + tot_removed
    print(f'\ntotal: {tot_changed} changed, {tot_added} added, '
          f'{tot_removed} removed')
    if shown < tot_changed:
        print(f'  ({tot_changed - shown} more not shown; raise --show)')

    rc = 0
    if gated_hits:
        print(f'\nGATED SCRIPTS CHANGED ({len(gated_hits)}) -- play-tested, '
              f'treat every difference as a regression:')
        for g in gated_hits:
            print(f'    {g}')
        rc = 1
    if total:
        rc = 1
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    def common(p):
        p.add_argument('-f', '--plugin', action='append',
                       help='plugin name (repeatable); default all')
        p.add_argument('--all', action='store_true',
                       help='every plugin with generated scripts')
        p.add_argument('--baseline', default=str(DEFAULT_BASELINE))
        p.add_argument('--workers', type=int,
                       default=max(1, (os.cpu_count() or 2) - 1))

    common(sub.add_parser('snapshot', help='record the current models'))
    c = sub.add_parser('compare', help='diff current output against the baseline')
    common(c)
    c.add_argument('--show', type=int, default=10,
                   help='print differences for the first N changed scripts')
    c.add_argument('--lines', type=int, default=12,
                   help='max difference lines per script')
    c.add_argument('--gate', action='append',
                   help='basename whose change always fails (repeatable)')
    c.add_argument('--gate-prefix', action='append',
                   help='basename PREFIX whose change always fails '
                        f'(repeatable; default {DEFAULT_GATE_PREFIX})')

    args = ap.parse_args(argv)
    return {'snapshot': cmd_snapshot, 'compare': cmd_compare}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
