"""Voice-type identity for a plugin's own races.

Oblivion names a voice folder after the race's DISPLAY name (the RACE record's
FULL), not its EditorID:

    sound\\voice\\<plugin>\\<FULL lowercased>\\<gender>\\<topic>_<fid>_<n>.mp3

In the English game the two strings are identical, so the distinction never
shows and a table keyed on EditorIDs works by accident.  In a localised plugin
they diverge and everything keyed on the EditorID stops matching the folders on
disk.  Measured on Nehrim.esm:

    RACE EditorID                     FULL (= folder name)
    Alemanne1, Alemanne2, Alemanne1Gabor, ...   Alemanne
    HighElf                                     Hochelf
    DarkElf                                     Eraterna
    Argonian                                    Argonier

Both halves of the voice pipeline resolve identity through this module so they
cannot disagree:

  * the importer creates one VTYP record per (voice key, gender) and points
    EVERY race EditorID sharing that FULL at it, so the seven Alemanne races
    share one voice type;
  * the sound stage maps each source race folder to the same VTYP EditorID, so
    generic per-race lines land in the folder the speaker's VTYP names.

For Oblivion this reproduces the previously hardcoded names exactly — "High
Elf" -> HighElf, "Dark Elf" -> DarkElf, "Golden Saint" -> GoldenSaint — so
every race keeps the voice type it already had.

A race with no FULL is SKIPPED rather than falling back to its EditorID: the
folder on disk is the display name, so a race that has no display name has no
folder, and giving it an identity here would point it at an empty directory.
Oblivion's VampireRace is exactly that case and the fixed table routes it to
Imperial on purpose.
"""

from pathlib import Path

__all__ = ['voice_key', 'vtyp_edid', 'load_race_voices', 'RaceVoices']


def voice_key(name: str) -> str:
    """Collapse a race display name into the EditorID fragment used for VTYPs.

    Keeps letters and digits, drops everything else, preserves case:
    "High Elf" -> "HighElf", "Halb-Aeterna" -> "HalbAeterna".
    """
    return ''.join(c for c in (name or '') if c.isalnum())


def vtyp_edid(key: str, gender: str) -> str:
    """VTYP EditorID for a voice key. *gender* is 'Male'/'Female' or 'M'/'F'."""
    g = 'Female' if gender.upper().startswith('F') else 'Male'
    return f'TES4{g}{key}'


class RaceVoices:
    """Resolved race identity for one plugin's export.

    by_race_edid: RACE EditorID -> voice key
    by_folder:    lowercased voice-folder name -> voice key
    keys:         every distinct voice key, sorted (deterministic FormIDs)
    """

    __slots__ = ('by_race_edid', 'by_folder', 'keys')

    def __init__(self, by_race_edid, by_folder, keys):
        self.by_race_edid = by_race_edid
        self.by_folder = by_folder
        self.keys = keys

    def folder_key(self, folder: str) -> 'str | None':
        """Voice key for a source race folder, or None if the plugin has no
        race by that display name."""
        return self.by_folder.get((folder or '').strip().lower())

    def __bool__(self):
        return bool(self.keys)


def _iter_records(txt: Path):
    """Same export-text idiom as wearable_plan._iter_records."""
    if not txt.is_file():
        return
    body = txt.read_text(encoding='utf-8', errors='replace')
    for chunk in body.split('---RECORD_BEGIN---')[1:]:
        rec = {}
        for line in chunk.split('---RECORD_END---')[0].splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            rec[k] = v
        if rec:
            yield rec


def load_race_voices(export_dir) -> RaceVoices:
    """Read RACE.txt from a per-plugin export directory (e.g. export/Nehrim.esm).

    A plugin with no RACE.txt — or one whose races carry no FULL — yields an
    empty result, and callers keep their previous behaviour.
    """
    by_race_edid: dict = {}
    by_folder: dict = {}

    for rec in _iter_records(Path(export_dir) / 'RACE.txt'):
        edid = (rec.get('EditorID') or '').strip()
        if not edid:
            continue
        # A race with NO display name has no voice folder either — the folder
        # IS the display name.  Minting it an identity of its own therefore
        # points it at a directory that cannot exist, so it must fall through
        # to the caller's fixed table instead.  Oblivion's VampireRace
        # (0x00000019, FULL absent) is the case that matters: the fixed table
        # deliberately routes it to Imperial, which HAS recordings, and
        # claiming it here overrode that and silenced those actors.  "Somebody
        # else's voice type" is a folder with audio in it; its own would be
        # empty.
        full = (rec.get('FULL') or '').strip()
        if not full:
            continue
        key = voice_key(full)
        if not key:
            continue
        by_race_edid[edid] = key
        # The folder on disk is the display name.  Register the EditorID too:
        # an English-named folder in a localised plugin still has to resolve,
        # and for Oblivion both spellings collapse onto the same key anyway.
        by_folder.setdefault(full.lower(), key)
        by_folder.setdefault(edid.lower(), key)

    return RaceVoices(by_race_edid, by_folder, sorted(set(by_race_edid.values())))
