"""Real spoken-line durations for converted `Say`/`SayTo` timers.

TES4's `Say`/`SayTo` RETURNED the voice line's length in seconds, and every
Oblivion polling conversation stores that in a timer it counts down before
speaking the next line:

    set timer to SayTo player, CharGenTaunt2 1
    ...
    if timer > 0
        set timer to timer - getSecondsPassed

Papyrus `Say()` returns nothing, so the converter has to supply a number.  A
flat constant is not good enough: Valen Dreth's CharacterGen taunts run 8-14.5
seconds, and a 3-second stand-in made the script re-issue `Say` while the
previous line was still playing.  Skyrim drops a `Say` on an actor who is
already talking, so the line restarted forever, its End fragment never ran, and
the `tauntCount` that selects the NEXT taunt never incremented — the tutorial
sat on one repeating line.

The durations are knowable: the exported Oblivion voice files are the very
audio the converted plugin ships.  This module walks the MP3 frame headers (no
external dependency) and reports, per dialogue TOPIC, the longest response —
the safe timer value, since the topic's conditions decide at runtime which
response actually plays.

Durations are cached to `<export>/voice_durations.json` because a full scan is
~39k files.
"""
import json
import os
import re
import struct
from collections import defaultdict

# MPEG-1 Layer III bitrate table (kbps), index 0/15 invalid.
_BITRATES_V1_L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224,
                   256, 320, 0]
# MPEG-2/2.5 Layer III bitrates
_BITRATES_V2_L3 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144,
                   160, 0]
_SAMPLE_RATES = {3: [44100, 48000, 32000],    # MPEG-1
                 2: [22050, 24000, 16000],    # MPEG-2
                 0: [11025, 12000, 8000]}     # MPEG-2.5

# Oblivion voice filenames: <quest>_<topic>_<infofid>_<n>.mp3
_VOICE_NAME_RE = re.compile(
    r'^(?P<quest>.+?)_(?P<topic>.+?)_(?P<fid>[0-9a-fA-F]{8})_\d+\.mp3$')

CACHE_NAME = 'voice_durations.json'


def mp3_duration(path: str) -> float:
    """Duration in seconds by summing MPEG frame durations. 0.0 if unreadable.

    Frame-walking rather than trusting a header: Oblivion's files are CBR but
    carry ID3 tags and occasional garbage between frames, and there is no
    Xing/Info header to read a frame count from.
    """
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return 0.0
    p = 0
    if data[:3] == b'ID3' and len(data) >= 10:
        size = ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 |
                (data[8] & 0x7F) << 7 | (data[9] & 0x7F))
        p = 10 + size
    total = 0.0
    n = len(data)
    while p + 4 <= n:
        if data[p] != 0xFF or (data[p + 1] & 0xE0) != 0xE0:
            p += 1
            continue
        ver = (data[p + 1] >> 3) & 3       # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
        layer = (data[p + 1] >> 1) & 3     # 1 = Layer III
        br_i = (data[p + 2] >> 4) & 0xF
        sr_i = (data[p + 2] >> 2) & 3
        pad = (data[p + 2] >> 1) & 1
        if layer != 1 or br_i in (0, 15) or sr_i == 3 or ver == 1:
            p += 1
            continue
        rates = _SAMPLE_RATES.get(ver)
        if not rates:
            p += 1
            continue
        sr = rates[sr_i]
        if ver == 3:
            bitrate = _BITRATES_V1_L3[br_i] * 1000
            spf = 1152
        else:
            bitrate = _BITRATES_V2_L3[br_i] * 1000
            spf = 576
        if not bitrate:
            p += 1
            continue
        frame_len = int((spf / 8 * bitrate) / sr) + pad
        if frame_len <= 0:
            p += 1
            continue
        total += spf / sr
        p += frame_len
    return total


def scan_voice_durations(export_dir: str, use_cache: bool = True,
                         workers: int = None) -> dict:
    """topic (lowercase) -> longest response duration in seconds.

    Also keyed by `<quest>_<topic>` so a caller can disambiguate a topic name
    reused across quests, and by `info:<FID>` (uppercase 8-hex) for the exact
    per-response length.  The per-INFO entries are what an End fragment uses to
    clear a conversation timer precisely; the per-topic maximum is only the
    fallback for a `Say()` whose response cannot be known statically.
    """
    cache_path = os.path.join(export_dir, CACHE_NAME)
    voice_root = os.path.join(export_dir, 'sound', 'voice')
    if not os.path.isdir(voice_root):
        return {}
    if use_cache and os.path.isfile(cache_path):
        try:
            with open(cache_path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            pass

    files = []
    for root, _dirs, names in os.walk(voice_root):
        for nm in names:
            if nm.lower().endswith('.mp3'):
                files.append(os.path.join(root, nm))
    if not files:
        return {}

    if workers is None:
        workers = max(1, (os.cpu_count() or 2) - 1)

    # Pure file I/O + a tight byte scan: a thread pool is the right tool.
    from concurrent.futures import ThreadPoolExecutor
    durations = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for path, secs in zip(files, ex.map(mp3_duration, files)):
            if secs <= 0:
                continue
            m = _VOICE_NAME_RE.match(os.path.basename(path))
            if not m:
                continue
            topic = m.group('topic').lower()
            quest = m.group('quest').lower()
            # Per-topic maximum (static fallback) ...
            for key in (topic, f'{quest}_{topic}'):
                if secs > durations.get(key, 0.0):
                    durations[key] = round(secs, 2)
            # ... and the exact per-response length. A response is recorded once
            # per voice type; they are the same performance length within a few
            # frames, so the longest is the safe representative.
            ikey = f"info:{m.group('fid').upper()}"
            if secs > durations.get(ikey, 0.0):
                durations[ikey] = round(secs, 2)

    try:
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(durations, f, indent=0, sort_keys=True)
    except OSError:
        pass
    return durations
