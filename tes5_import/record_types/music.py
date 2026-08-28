"""MUSC / MUST — Skyrim's music system, built from TES4's folder categories.

TES4 has no music records at all.  Oblivion's engine scans Data/Music/<Category>/
and shuffles what it finds; CELL.XCMT and WRLD.SNAM carry only a 3-value enum
{0 Default, 1 Public, 2 Dungeon} (xEdit wbMusicEnum).  So the conversion is:

    the FOLDER a track sits in  ->  a MUSC music type
    each track file             ->  a MUST music track
    XCMT / SNAM enum value      ->  which MUSC a cell / worldspace points at

Structure follows wbDefinitionsTES5.pas and a real Skyrim.esm dump:

  MUSC: EDID, FNAM flags(u32), PNAM {Priority u16, Ducking u16}, WNAM fade(f32),
        TNAM array of MUST FormIDs
  MUST: EDID, CNAM track type(u32), FLTV duration(f32), DNAM fade-out(f32),
        ANAM track filename

Vanilla census (Skyrim.esm, 258 MUST / 50 MUSC) drove the shape:
  * 240 Single Track, 13 Palette, 5 Silent Track -- so single tracks are the
    norm and this writes those plus one Silent Track for a silence source.
  * Only 1 record carries LNAM loop data, 10 carry FNAM cue points and 10 a
    BNAM finale, so all three are omitted rather than invented.
  * 210 of 240 ANAMs start with a leading backslash; the 30 that do not load
    identically.  We follow the majority form.
"""
import struct

from ..writer import pack_record, pack_string_subrecord, pack_subrecord

# CNAM track types (xEdit wbDefinitionsTES5.pas:7203).  These are hashes, not
# an ordinal enum -- writing 0/1/2 here produces a track the engine ignores.
TRACK_SINGLE = 0x6ED7E048
TRACK_PALETTE = 0x23F678C3
TRACK_SILENT = 0xA1A9C4D5

# MUSC FNAM flags.
F_PLAYS_ONE_SELECTION = 0x01
F_ABRUPT_TRANSITION = 0x02
F_CYCLE_TRACKS = 0x04
F_MAINTAIN_TRACK_ORDER = 0x08
F_DUCKS_CURRENT_TRACK = 0x20

# TES4 Data/Music/<folder> -> how that category behaves in Skyrim.
#
# 🛑 PNAM PRIORITY IS INVERTED FROM THE OBVIOUS READING: **LOWER WINS.**
# Measured across all 50 vanilla MUSC in Skyrim.esm, the two bands are cleanly
# separated with no overlap:
#
#     INTERRUPT types (MUSCombat, MUSSpecial*, MUSDiscovery*,
#                      MUSReveal, MUSReward, MUSStinger, MUSDread)   1 .. 3
#     BED / ambient   (MUSExplore*, MUSDungeon*, MUSTavern*,
#                      MUSTownTest, MUSCastle)                      47 .. 55
#
# So combat sits at 1-2 and the persistent exploration bed at 49-50.  An
# earlier version of this table had it backwards (explore=20, battle=6) on the
# assumption that "higher priority = more important"; that put the ambient bed
# BELOW vanilla's `_NONE` placeholder (priority 5) and no exploration music
# ever played.  Never renumber these without re-running the vanilla census.
#
# `Battle` has NO record-level source in TES4 -- Oblivion's engine picks combat
# music by folder name alone -- so its MUSC is authored here.  That is the one
# unavoidable authored value; every other category is a direct folder mapping.
#
# Vanilla ducking for the ambient tier is 0 and for combat 10000 (=100.00 dB
# via wbDiv(100)), which is how combat silences the bed rather than mixing
# over it.
CATEGORY_SPECS = {
    # Ambient beds -- vanilla MUSExplore* = 49, MUSDungeon* = 50,
    # MUSTownTest = 48.
    'explore':  {'priority': 49, 'flags': F_CYCLE_TRACKS, 'fade': 4.0},
    'public':   {'priority': 48, 'flags': F_CYCLE_TRACKS, 'fade': 4.0},
    'dungeon':  {'priority': 50, 'flags': F_CYCLE_TRACKS, 'fade': 4.0},
    # Combat -- vanilla MUSCombat: priority 2, ducking 10000, flags 0x24
    # (Cycle Tracks | Ducks Current Track).
    'battle':   {'priority': 2, 'ducking': 10000,
                 'flags': F_CYCLE_TRACKS | F_DUCKS_CURRENT_TRACK,
                 'fade': 2.0},
    # Special tracks are one-shot quest/event cues fired by script
    # (38 StreamMusic calls in Nehrim.esm), so they must not cycle: each gets
    # its own MUSC that plays exactly one selection and ducks whatever is on.
    # Vanilla MUSSpecial* sit at priority 1 with ducking 0-6000.
    'special':  {'priority': 1, 'ducking': 6000,
                 'flags': F_PLAYS_ONE_SELECTION | F_DUCKS_CURRENT_TRACK,
                 'fade': 1.0},
}

# TES4 XCMT / SNAM enum -> category folder.  Value 0 (Default) deliberately maps
# to Explore: Oblivion's "default" music IS the exploration set.
MUSIC_ENUM_CATEGORY = {0: 'explore', 1: 'public', 2: 'dungeon'}

# A source whose stem matches this becomes a Silent Track rather than a track
# with an ANAM -- vanilla ships 5 such records (MUSTavernSILENCE and kin) and
# they are how a script stops music without fighting the ducking system.
# Both games author one: Nehrim's Special/Silence.mp3 and, at the music ROOT
# with no category folder, Oblivion's own 5min-silence.mp3 (300 s).  Matching a
# bare 'silence' stem would miss the latter, so this is a substring test.
def is_silent_stem(stem: str) -> bool:
    """True when a track file is an authored silence placeholder."""
    return 'silence' in (stem or '').lower()

# Vanilla writes ANAM with a leading backslash in 210 of 240 records.
ANAM_PREFIX = chr(92)


def _f32(v: float) -> bytes:
    return struct.pack('<f', float(v))


def musc_editor_id(plugin: str, category: str) -> str:
    """EditorID for a category MUSC.  Plugin-scoped so two conversions coexist."""
    stem = ''.join(c for c in plugin if c.isalnum())
    return 'MUS%s%s' % (stem, category.capitalize())


def musc_cue_editor_id(plugin: str, source_rel: str) -> str:
    """EditorID of the per-cue MUSC built for one `Special/` track.

    MUST match script_convert.constants.music_cue_editor_id: the script
    converter declares a Papyrus property under this name so a StreamMusic call
    binds to this record.  If the two disagree the property binds to nothing and
    the cue is silent.
    """
    stem = ''.join(c for c in plugin if c.isalnum())
    tail = source_rel.rsplit('/', 1)[-1].rsplit('.', 1)[0]
    tail = ''.join(c if c.isalnum() else '_' for c in tail)
    return 'MUSCue%s_%s' % (stem, tail)


def must_editor_id(plugin: str, source_rel: str) -> str:
    """EditorID for one track, derived from its authored source path."""
    stem = ''.join(c for c in plugin if c.isalnum())
    tail = source_rel.rsplit('/', 1)[-1].rsplit('.', 1)[0]
    tail = ''.join(c if c.isalnum() else '_' for c in tail)
    return 'MUSTrk%s_%s' % (stem, tail)


def build_MUST(track: dict, form_id: int, plugin: str) -> bytes:
    """Pack one MUST music track record from a music_tracks.json entry.

    `track` carries the measured duration; FLTV is a real float in seconds the
    engine schedules against, so it must be probed rather than guessed.
    """
    silent = is_silent_stem(track.get('stem', ''))

    subs = pack_string_subrecord('EDID',
                                 must_editor_id(plugin, track['source_rel']))
    subs += pack_subrecord('CNAM', struct.pack(
        '<I', TRACK_SILENT if silent else TRACK_SINGLE))

    if not silent:
        # Duration and fade-out. A silent track carries neither in vanilla.
        subs += pack_subrecord('FLTV', _f32(track.get('duration') or 0.0))
        subs += pack_subrecord('DNAM', _f32(2.0))
        subs += pack_string_subrecord('ANAM', ANAM_PREFIX + track['game_path'])

    return pack_record('MUST', form_id, 0, subs)


def build_MUSC(category: str, track_fids: list, form_id: int,
               plugin: str, spec: dict = None, edid: str = None) -> bytes:
    """Pack one MUSC music type pointing at `track_fids`.

    PNAM packs two u16 in one subrecord: Priority then Ducking in hundredths of
    a dB (xEdit shows it divided by 100).  Vanilla ducking is commonly 0 or a
    few hundred; 0 leaves the engine's default mix alone.
    """
    spec = spec or CATEGORY_SPECS.get(category.lower()) or \
        CATEGORY_SPECS['explore']

    subs = pack_string_subrecord(
        'EDID', edid or musc_editor_id(plugin, category))
    subs += pack_subrecord('FNAM', struct.pack('<I', spec['flags']))
    subs += pack_subrecord('PNAM', struct.pack(
        '<HH', spec['priority'], spec.get('ducking', 0)))
    subs += pack_subrecord('WNAM', _f32(spec['fade']))
    if track_fids:
        subs += pack_subrecord(
            'TNAM', b''.join(struct.pack('<I', f) for f in track_fids))
    return pack_record('MUSC', form_id, 0, subs)


def load_music_manifest(out_root) -> dict:
    """Read music_tracks.json written by asset_convert.music_convert.

    Returns {} when music was never converted, which is the normal state for a
    plugin with masters (music is only ingested for masterless plugins).
    """
    from pathlib import Path
    from ..artifact_schema import read_artifact, StaleArtifactError
    p = Path(out_root) / 'music_tracks.json'
    if not p.is_file():
        return {}
    try:
        return read_artifact(str(p))
    except StaleArtifactError:
        # Deliberately NOT swallowed by the except below: a stale manifest is
        # a real failure with a fix the user can act on, not a missing file.
        raise
    except (ValueError, OSError):
        return {}


def build_music_records(manifest: dict, writer, plugin: str) -> dict:
    """Build every MUST and MUSC for a plugin.

    Returns a dict with:
      'must'      : [(formid, bytes), ...]
      'musc'      : [(formid, bytes), ...]
      'by_enum'   : {tes4 enum value -> MUSC formid}  for CELL.XCMO / WRLD.ZNAM
      'by_source' : {source_rel -> MUSC formid}       for StreamMusic in scripts

    FormIDs are derived from AUTHORED data only -- the track's own source path,
    or the category folder name -- so ids never move between builds.
    """
    tracks = manifest.get('tracks') or []
    out = {'must': [], 'musc': [], 'by_enum': {}, 'by_source': {}}
    if not tracks:
        return out

    by_cat = {}
    fid_for_source = {}
    for t in tracks:
        # Key on the authored source path: it is the one identifier that exists
        # in the TES4 data and never changes.
        fid = writer.derive_formid('MUST', t['source_rel'])
        fid_for_source[t['source_rel']] = fid
        out['must'].append((fid, build_MUST(t, fid, plugin)))
        # A file at the music ROOT has no category folder, so it is not part
        # of any rotation -- Oblivion's own 5min-silence.mp3 sits there.  Give
        # it the one-shot 'special' treatment (its own addressable MUSC) rather
        # than dropping it into Explore's shuffle, where it would play as five
        # minutes of dead air on a third of the exploration rotations.
        cat = (t.get('category') or 'special').lower()
        by_cat.setdefault(cat, []).append((t['source_rel'], fid))

    for cat, entries in sorted(by_cat.items()):
        entries.sort()
        spec = CATEGORY_SPECS.get(cat)
        if cat == 'special':
            # One MUSC per cue: a script names a specific FILE, so each needs
            # its own addressable music type.  Sharing one MUSC would make
            # every StreamMusic play whichever track the engine picked.
            for source_rel, track_fid in entries:
                mfid = writer.derive_formid('MUSC_TRACK', source_rel)
                out['musc'].append(
                    (mfid, build_MUSC(cat, [track_fid], mfid, plugin, spec,
                                      edid=musc_cue_editor_id(plugin,
                                                              source_rel))))
                out['by_source'][source_rel] = mfid
            continue

        mfid = writer.derive_formid('MUSC', '%s|%s' % (plugin, cat))
        out['musc'].append(
            (mfid, build_MUSC(cat, [f for _s, f in entries], mfid, plugin, spec)))
        for source_rel, _f in entries:
            out['by_source'][source_rel] = mfid
        for enum_val, enum_cat in MUSIC_ENUM_CATEGORY.items():
            if enum_cat == cat:
                out['by_enum'][enum_val] = mfid

    return out
