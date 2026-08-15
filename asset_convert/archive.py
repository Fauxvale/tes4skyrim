"""Reading mod archives (.zip / .7z / .rar) with the bundled 7-Zip.

Mod archives are downloaded from the internet, so every path inside one is
untrusted input. This module is the ONLY place archives are opened, and it
enforces two rules for the whole pipeline:

  * ``safe_relpath`` — every member path is normalised and rejected if it
    escapes the destination (``..``, absolute paths, drive letters, UNC).
    ``ZipFile.extractall`` is never used; 7-Zip is always given an explicit
    output directory and its results are re-checked on the way out.
  * listing is separate from extraction, so a caller can inspect an archive
    (member list, layout, plugin names) without writing anything to disk.

``.zip`` uses stdlib ``zipfile``: it streams members natively and needs no
subprocess. ``.7z`` and ``.rar`` go through ``external/7zip/7z.exe``.

NOTE: the bundled 7-Zip is ``7z.exe`` + ``7z.dll``, NOT the standalone
``7za.exe`` — RAR support lives in the DLL and the standalone build cannot read
RAR at all. See ``external/7zip/BUILD.md``.
"""
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from subprocess_flags import POPEN_FLAGS, windows_cmd

SCRIPT_DIR = Path(__file__).resolve().parent.parent
SEVENZIP_EXE = SCRIPT_DIR / 'external' / '7zip' / '7z.exe'

# Extensions this module can open. `.bsa` is deliberately absent: BSAs are read
# by asset_convert.bsa_extract, which understands their internal layout.
ARCHIVE_EXTS = frozenset({'.zip', '.7z', '.rar'})

# 7-Zip writes "Path = ..." / "Size = ..." blocks in `l -slt` output, one block
# per entry, separated by blank lines.
_SLT_KEY_RE = re.compile(r'^([A-Za-z0-9_ ]+) = (.*)$')


class ArchiveError(RuntimeError):
    """An archive could not be opened or read."""


class UnsafeMemberError(ArchiveError):
    """An archive member's path escapes the extraction destination."""


class Member:
    """One entry in an archive.

    `path` is always normalised to forward slashes and is guaranteed safe (it
    passed `safe_relpath`). `is_dir` entries carry no data.
    """

    __slots__ = ('path', 'size', 'is_dir')

    def __init__(self, path: str, size: int = 0, is_dir: bool = False):
        self.path = path
        self.size = size
        self.is_dir = is_dir

    @property
    def name(self) -> str:
        return self.path.rsplit('/', 1)[-1]

    @property
    def ext(self) -> str:
        return os.path.splitext(self.path)[1].lower()

    def __repr__(self):
        return f'<Member {self.path!r} size={self.size}>'


def is_archive(path) -> bool:
    """True if `path` looks like an archive this module can open."""
    return os.path.splitext(str(path))[1].lower() in ARCHIVE_EXTS


def safe_relpath(member_path: str) -> str:
    """Normalise an archive member path, or raise if it escapes.

    Rejects absolute paths, drive letters, UNC paths and any '..' that walks
    above the root. Returns a clean forward-slash relative path.

    This is the single gate every archive member passes through. A mod archive
    is attacker-controlled input downloaded from the internet: without this a
    member named '../../../Windows/System32/x.dll' would be written outside the
    destination (the classic Zip Slip).
    """
    raw = str(member_path).replace('\\', '/').strip()
    if not raw:
        raise UnsafeMemberError('empty member path')

    # Drive letter ("C:/x", "C:x") or UNC ("//server/share").
    if re.match(r'^[A-Za-z]:', raw) or raw.startswith('//'):
        raise UnsafeMemberError(f'absolute path in archive: {member_path!r}')
    if raw.startswith('/'):
        raise UnsafeMemberError(f'absolute path in archive: {member_path!r}')

    parts = []
    for part in raw.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            # Walking above the root is always an escape attempt. Popping a
            # previous segment instead would silently "fix" the path and hide
            # a malicious archive, so refuse outright.
            if not parts:
                raise UnsafeMemberError(
                    f'path escapes archive root: {member_path!r}')
            parts.pop()
            continue
        parts.append(part)

    if not parts:
        raise UnsafeMemberError(f'path resolves to nothing: {member_path!r}')
    return '/'.join(parts)


def safe_join(dest_root, member_path) -> Path:
    """Absolute destination for `member_path` under `dest_root`.

    Applies `safe_relpath`, then verifies the resolved result really is inside
    `dest_root` — belt and braces, because symlinked destination roots and
    case-insensitive filesystems can both defeat pure string checks.
    """
    root = Path(dest_root).resolve()
    target = (root / safe_relpath(member_path)).resolve()
    if target != root and root not in target.parents:
        raise UnsafeMemberError(
            f'path escapes destination: {member_path!r}')
    return target


def sevenzip_available() -> bool:
    """True if the bundled 7-Zip binary is present."""
    return SEVENZIP_EXE.is_file()


def _run_7z(args, timeout=1800):
    """Invoke the bundled 7-Zip, returning completed process stdout."""
    if not sevenzip_available():
        raise ArchiveError(
            f'Bundled 7-Zip not found at {SEVENZIP_EXE}. It ships with the '
            f'repo -- restore it with: git checkout -- external/7zip')
    cmd = windows_cmd([str(SEVENZIP_EXE)] + [str(a) for a in args])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              errors='replace', timeout=timeout,
                              **POPEN_FLAGS)
    except subprocess.TimeoutExpired as exc:
        raise ArchiveError(f'7-Zip timed out after {timeout}s') from exc
    except OSError as exc:
        raise ArchiveError(f'could not run 7-Zip: {exc}') from exc
    # 7-Zip: 0 = ok, 1 = warning (e.g. a file was locked) -- both usable.
    if proc.returncode > 1:
        detail = (proc.stdout or '') + (proc.stderr or '')
        tail = '\n'.join(
            ln for ln in detail.splitlines() if ln.strip())[-600:]
        raise ArchiveError(
            f'7-Zip failed (exit {proc.returncode}):\n{tail}')
    return proc.stdout or ''


def _list_zip(path):
    try:
        with zipfile.ZipFile(path) as zf:
            out = []
            for info in zf.infolist():
                try:
                    rel = safe_relpath(info.filename)
                except UnsafeMemberError:
                    # Skip rather than abort: one hostile entry must not make
                    # an otherwise good mod archive unusable. The caller never
                    # sees it, so it can never be extracted.
                    continue
                out.append(Member(rel, info.file_size, info.is_dir()))
            return out
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f'not a valid zip: {path} ({exc})') from exc


def _list_7z(path):
    out = _run_7z(['l', '-slt', '-ba', str(path)])
    members, cur = [], {}
    for line in out.splitlines():
        if not line.strip():
            if cur.get('Path'):
                members.append(cur)
            cur = {}
            continue
        m = _SLT_KEY_RE.match(line)
        if m:
            cur[m.group(1).strip()] = m.group(2).strip()
    if cur.get('Path'):
        members.append(cur)

    result = []
    for entry in members:
        attrib = entry.get('Attributes', '')
        is_dir = attrib.startswith('D') or 'D_' in attrib
        try:
            rel = safe_relpath(entry['Path'])
        except UnsafeMemberError:
            continue
        try:
            size = int(entry.get('Size') or 0)
        except ValueError:
            size = 0
        result.append(Member(rel, size, is_dir))
    return result


def list_members(path):
    """Every member of `path`, as `Member` objects, in sorted order.

    Sorted deterministically so two ingests of equivalent archives produce
    identical trees. Members whose paths are unsafe are omitted entirely.
    """
    path = Path(path)
    if not path.is_file():
        raise ArchiveError(f'archive not found: {path}')
    ext = path.suffix.lower()
    if ext == '.zip':
        members = _list_zip(path)
    elif ext in ARCHIVE_EXTS:
        members = _list_7z(path)
    else:
        raise ArchiveError(f'unsupported archive type: {path.suffix}')
    return sorted(members, key=lambda m: m.path.lower())


def extract_all(path, dest_dir, members=None):
    """Extract `path` into `dest_dir`, returning the number of files written.

    `members`: optional iterable of member paths to limit extraction to.

    Every written path is re-validated with `safe_join`, whichever backend did
    the extraction -- 7-Zip is given an output directory, but its own output is
    not trusted to have stayed inside it.
    """
    path = Path(path)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    wanted = None if members is None else {
        str(m).replace('\\', '/').lower() for m in members}

    if path.suffix.lower() == '.zip':
        return _extract_zip(path, dest, wanted)
    return _extract_7z(path, dest, wanted)


def _extract_zip(path, dest, wanted):
    written = 0
    with zipfile.ZipFile(path) as zf:
        for info in sorted(zf.infolist(), key=lambda i: i.filename.lower()):
            if info.is_dir():
                continue
            try:
                rel = safe_relpath(info.filename)
            except UnsafeMemberError:
                continue
            if wanted is not None and rel.lower() not in wanted:
                continue
            target = safe_join(dest, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            # Stream rather than read() -- a texture pack member can be
            # hundreds of MB and this project must never balloon memory.
            with zf.open(info) as src, open(target, 'wb') as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            written += 1
    return written


def _extract_7z(path, dest, wanted):
    # `x` preserves directory structure; `-y` assumes yes; `-bd` no progress
    # meter (we are capturing output, not showing a console).
    args = ['x', str(path), f'-o{dest}', '-y', '-bd']
    if wanted:
        # 7-Zip matches include patterns case-insensitively on Windows, which
        # is what we want -- archives are inconsistent about case.
        args += [f'-i!{m}' for m in sorted(wanted)]
    _run_7z(args)

    # Re-validate everything 7-Zip actually wrote, and count it.
    written = 0
    root = dest.resolve()
    for dirpath, _dirnames, filenames in os.walk(dest):
        for fn in filenames:
            full = Path(dirpath, fn).resolve()
            if full != root and root not in full.parents:
                raise UnsafeMemberError(
                    f'7-Zip wrote outside the destination: {full}')
            written += 1
    return written


def extract_one(path, member_path, dest_file):
    """Extract a single member to an exact destination file path.

    Used for streaming a contained `.bsa` or nested archive out to a temp file
    without unpacking everything around it.
    """
    path = Path(path)
    dest_file = Path(dest_file)
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    rel = safe_relpath(member_path)

    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as zf:
            name = _zip_real_name(zf, rel)
            if name is None:
                raise ArchiveError(f'member not found: {member_path}')
            with zf.open(name) as src, open(dest_file, 'wb') as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
        return dest_file

    # 7-Zip cannot rename on extract, so unpack the single member into a temp
    # directory and move it into place.
    tmp = dest_file.parent / f'.7ztmp_{dest_file.name}'
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        _run_7z(['x', str(path), f'-o{tmp}', f'-i!{rel}', '-y', '-bd'])
        produced = tmp / rel
        if not produced.is_file():
            found = [p for p in tmp.rglob('*') if p.is_file()]
            if not found:
                raise ArchiveError(f'member not found: {member_path}')
            produced = found[0]
        if dest_file.exists():
            dest_file.unlink()
        shutil.move(str(produced), str(dest_file))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dest_file


def _zip_real_name(zf, rel):
    """Map a normalised relative path back to the zip's own member name."""
    target = rel.lower()
    for name in zf.namelist():
        try:
            if safe_relpath(name).lower() == target:
                return name
        except UnsafeMemberError:
            continue
    return None
