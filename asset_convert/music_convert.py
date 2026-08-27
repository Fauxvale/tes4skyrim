"""Music conversion: TES4 loose Data/Music -> TES5 xWMA + a track manifest.

Music is the one asset class NO record names a file for.  Oblivion's engine
scans Data/Music/<Category>/ and shuffles whatever it finds, so CELL.XCMT and
WRLD.SNAM carry only the 3-value {Default, Public, Dungeon} enum.  That makes
the FOLDER the authored unit of meaning, and this module preserves it:

    export/<plugin>/music/Explore/Atmosphere_01.mp3
      -> output/<plugin>/music/tes4/<plugin>/Explore/Atmosphere_01.xwm

The `tes4/<plugin>/` scoping is load-bearing.  Oblivion and Nehrim BOTH ship an
`Explore/` folder, so converting two plugins into a shared `Music/Explore/`
would have one silently overwrite the other.

Output is xWMA, not PCM .wav.  Vanilla SSE ships loose music as RIFF/XWMA
(wFormatTag=0x161, 44.1 kHz stereo) and decoding the 329 MB of source mp3 to
PCM would inflate it to ~1.5-3 GB.  Unlike the voice path this encodes STEREO
at a music bitrate -- `convert_file_to_xwm` downmixes to mono, which is right
for dialogue and wrong for a soundtrack.

The manifest this writes (`music_tracks.json`) is what the importer turns into
MUST/MUSC records; it carries the duration ffmpeg measured, because MUST.FLTV
is a real float in seconds the engine schedules against.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from subprocess_flags import POPEN_FLAGS, windows_cmd, to_wine_path  # noqa: E402
from worker_budget import worker_count  # noqa: E402
from output_layout import asset_root as _asset_root, plugin_out_root  # noqa: E402

from .audio_converter import find_ffmpeg, find_xwmaencode  # noqa: E402

_DEFAULT_EXPORT = Path(__file__).resolve().parent.parent / 'export'

BS = chr(92)

# Source extensions worth converting.  A source already in .xwm is re-encoded
# through the same path, which normalises the bitrate and is cheap.
MUSIC_SRC_EXTS = ('.mp3', '.wav', '.xwm')

# xWMAEncode accepts ONLY these bitrates -- anything else fails outright with
# XWMA_E_UNSUPPORTED_BITRATE, so 128000 is not selectable however natural it
# looks.
XWMA_BITRATES = (20000, 32000, 48000, 64000, 96000, 160000, 192000)

# ...and of those, only a SUBSET is native per (sample rate, channels).  From
# xWMAEncode's own usage text:
#
#     44100Hz mono:   32000, 48000
#     44100Hz stereo: 32000, 48000, 96000, 192000
#     48000Hz stereo: 48000, 64000, 96000, 160000, 192000
#
#   "Other combinations are supported by resampling the source data and/or
#    using a bitrate of 48kbps as a fallback"
#
# 🛑 64000 and 160000 are NOT native at 44.1 kHz.  Asking for either silently
# RESAMPLES the output to 48 kHz -- verified by reading the fmt chunk of the
# result, not by trusting the request:
#
#     asked 96000  -> 96 kb/s @ 44100 Hz  (native)
#     asked 160000 -> 160 kb/s @ 48000 Hz (RESAMPLED)
#
# We normalise every source to 44.1 kHz stereo before encoding, so the middle
# row above is the only one that applies and 160000 would buy a rate conversion
# nothing asked for.  Never widen this tuple without re-reading that table.
NATIVE_44K_STEREO = (32000, 48000, 96000, 192000)
NATIVE_44K_MONO = (32000, 48000)

# Source bitrate -> target.  Re-encoding lossy->lossy compounds artifacts, so
# spending 192k on a 128k mp3 preserves that mp3's existing damage more
# faithfully without recovering anything: measured SNR against the source PCM
# is 20.9 dB for a 128k source at 96k, but 25.6 dB for a 320k source at the
# same 96k.  The ceiling is the SOURCE, so the target tracks it.
#
# For calibration, vanilla Skyrim ships ALL its music at 48 kb/s 44.1 kHz
# stereo (measured: mus_combat_01/mus_dungeon_01 in Skyrim - Sounds.bsa, and
# all 49 loose AE soundtrack files), so even the bottom rung here is vanilla
# parity and the top is 4x it.
BITRATE_LADDER = (
    #  source kb/s <=, target bits/sec
    (64,   32000),
    (128,  48000),
    (224,  96000),
    (10 ** 9, 192000),
)

# Used when the source bitrate cannot be determined (a .wav or an unreadable
# header): the middle of the ladder, native, and double vanilla.
MUSIC_BITRATE_DEFAULT = 96000


def pick_bitrate(src_kbps, channels: int = 2) -> int:
    """Native xWMA bitrate for a source of `src_kbps`, as bits/sec.

    Always returns a rate that is native at 44.1 kHz for the given channel
    count, because convert_music_file normalises every input to 44.1 kHz before
    the encoder sees it.
    """
    allowed = NATIVE_44K_MONO if channels == 1 else NATIVE_44K_STEREO
    if not src_kbps:
        target = MUSIC_BITRATE_DEFAULT
    else:
        target = next(t for cap, t in BITRATE_LADDER if src_kbps <= cap)
    # Clamp into the native set for this channel count (mono tops out at 48k).
    return target if target in allowed else max(allowed)

MANIFEST_NAME = 'music_tracks.json'


def _out_root(output_dir, source_name, extract_dir=None):
    return plugin_out_root(output_dir, source_name,
                           str(extract_dir or _DEFAULT_EXPORT))


_AUDIO_RE = re.compile(r"Audio:.*?, (\d+) Hz, (\w+),.*?(\d+) kb/s")


def probe_audio(ffmpeg: str, path) -> dict:
    """{'duration', 'kbps', 'channels'} for `path`; zeros when undeterminable.

    ONE ffmpeg invocation for all three: duration feeds MUST.FLTV and the
    bitrate/channels pick the encode rate, and probing twice per file would
    double the cost of the stage for no gain.

    Uses ffmpeg rather than ffprobe: ffprobe is not guaranteed to sit beside the
    bundled ffmpeg, and everything needed is on ffmpeg's stderr banner.
    """
    out = {'duration': 0.0, 'kbps': 0, 'channels': 2}
    try:
        r = subprocess.run([ffmpeg, '-i', str(path)], capture_output=True,
                           timeout=60, **POPEN_FLAGS)
    except (subprocess.TimeoutExpired, OSError):
        return out
    err = (r.stderr or b'').decode('utf-8', 'replace')

    for line in err.splitlines():
        line = line.strip()
        if line.startswith('Duration:'):
            stamp = line.split('Duration:', 1)[1].split(',', 1)[0].strip()
            try:
                hh, mm, ss = stamp.split(':')
                out['duration'] = int(hh) * 3600 + int(mm) * 60 + float(ss)
            except ValueError:
                pass
        m = _AUDIO_RE.search(line)
        if m:
            out['kbps'] = int(m.group(3))
            out['channels'] = 1 if m.group(2) == 'mono' else 2
    return out


def convert_music_file(src_path, dst_path, ffmpeg: str,
                       xwmaencode: 'str | None', bitrate: int = None) -> bool:
    """Encode one music file to xWMA, preserving stereo.

    Mirrors convert_file_to_xwm's two-stage shape (ffmpeg -> PCM wav ->
    xWMAEncode -> xwm) but keeps 2 channels at 44.1 kHz.

    `bitrate` is bits/sec and MUST be native at 44.1 kHz (see pick_bitrate);
    None falls back to the middle of the ladder.
    """
    src_path, dst_path = Path(src_path), Path(dst_path)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    if not xwmaencode:
        return False

    tmp_dir = Path(tempfile.mkdtemp(prefix='music_'))
    wav_path = tmp_dir / 'track.wav'
    xwm_path = tmp_dir / 'track.xwm'
    try:
        cmd_wav = [
            ffmpeg, '-y', '-i', str(src_path),
            '-ac', '2',            # STEREO -- music, not voice
            '-ar', '44100',
            '-c:a', 'pcm_s16le',
            str(wav_path),
        ]
        r1 = subprocess.run(cmd_wav, capture_output=True, timeout=300,
                            **POPEN_FLAGS)
        if r1.returncode != 0 or not wav_path.is_file():
            return False

        # xWMAEncode parses a leading '/' as a switch prefix (same bug as
        # hkxcmd), so paths go through to_wine_path exactly as the voice path.
        cmd_xwm = [
            xwmaencode, '-b', str(bitrate or MUSIC_BITRATE_DEFAULT),
            to_wine_path(str(wav_path)), to_wine_path(str(xwm_path)),
        ]
        r2 = subprocess.run(windows_cmd(cmd_xwm), capture_output=True,
                            timeout=300, **POPEN_FLAGS)
        if (r2.returncode != 0 or not xwm_path.is_file()
                or not xwm_path.stat().st_size):
            return False

        shutil.copyfile(xwm_path, dst_path)
        return dst_path.is_file() and dst_path.stat().st_size > 0
    except (subprocess.TimeoutExpired, OSError):
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def music_rel_dir(source_name: str) -> str:
    """Output-relative folder holding this plugin's tracks.

    Scoped per plugin because the category folder names collide: Oblivion and
    Nehrim both have Explore/, Dungeon/, Public/, Battle/, Special/.
    """
    return 'music/tes4/' + source_name


def convert_music(
    source_file: str,
    extract_dir: str = 'export',
    output_dir: str = 'output',
    ffmpeg_path: str = None,
    force: bool = False,
) -> dict:
    """Convert every extracted music file and write the track manifest.

    Returns a stats dict; the manifest lands in the plugin's output root as
    `music_tracks.json` for the importer to build MUST/MUSC from.
    """
    source_name = _asset_root(extract_dir, source_file).name
    src_root = Path(_asset_root(extract_dir, source_file)) / 'music'
    out_root = Path(_out_root(output_dir, source_file, extract_dir))
    dst_root = out_root / music_rel_dir(source_name)

    stats = {'converted': 0, 'cached': 0, 'failed': 0, 'tracks': 0}
    if not src_root.is_dir():
        print('  No extracted music for this plugin.')
        return stats

    ffmpeg = find_ffmpeg(ffmpeg_path)
    xwmaencode = find_xwmaencode()
    if not ffmpeg or not xwmaencode:
        print('  ERROR: music needs ffmpeg (%s) and xWMAEncode (%s); skipping.'
              % (bool(ffmpeg), bool(xwmaencode)))
        stats['failed'] = 1
        return stats

    jobs = []
    for root, _dirs, files in os.walk(src_root):
        for fname in sorted(files):
            src = Path(root) / fname
            if src.suffix.lower() not in MUSIC_SRC_EXTS:
                continue
            rel = src.relative_to(src_root)
            jobs.append((src, dst_root / rel.with_suffix('.xwm'), rel))

    if not jobs:
        print('  No music files to convert.')
        return stats

    print('  Converting %d music files to xWMA '
          '(stereo 44.1 kHz, bitrate scaled to each source)...' % len(jobs))

    def _one(job):
        src, dst, rel = job
        # One probe per file: the duration goes in the manifest for MUST.FLTV
        # and the bitrate/channels choose the encode rate.
        info = probe_audio(ffmpeg, src)
        rate = pick_bitrate(info['kbps'], info['channels'])
        cached = dst.is_file() and dst.stat().st_size > 0 and not force
        ok = True if cached else convert_music_file(src, dst, ffmpeg,
                                                    xwmaencode, rate)
        return src, dst, rel, ok, cached, info['duration'], rate, info['kbps']

    tracks = []
    with ThreadPoolExecutor(max_workers=worker_count()) as pool:
        futs = [pool.submit(_one, j) for j in jobs]
        for fut in as_completed(futs):
            src, dst, rel, ok, cached, dur, rate, src_kbps = fut.result()
            if not ok:
                stats['failed'] += 1
                print('    FAILED ' + rel.as_posix())
                continue
            stats['cached' if cached else 'converted'] += 1
            parts = rel.as_posix().split('/')
            game_rel = rel.with_suffix('.xwm').as_posix().replace('/', BS)
            tracks.append({
                # Category = the TOP folder, the authored unit of meaning in
                # TES4 (Battle/Dungeon/Explore/Public/Special).
                'category': parts[0] if len(parts) > 1 else '',
                # Source path as the plugin's own scripts spell it, so a
                # StreamMusic "data\music\special\x.mp3" can be resolved back.
                'source_rel': ('music/' + rel.as_posix()).lower(),
                'game_path': (BS.join(['Data', 'Music', 'tes4', source_name])
                              + BS + game_rel),
                'duration': round(dur, 3),
                'stem': rel.stem,
                'source_kbps': src_kbps,
                'bitrate': rate,
            })

    tracks.sort(key=lambda t: t['source_rel'])
    stats['tracks'] = len(tracks)
    out_root.mkdir(parents=True, exist_ok=True)
    # Versioned envelope so a manifest written by an older converter is
    # reported as stale instead of half-read (tes5_import/artifact_schema.py).
    from tes5_import.artifact_schema import write_artifact
    write_artifact(str(out_root / MANIFEST_NAME), source_name,
                   {'plugin': source_name, 'tracks': tracks})

    import collections
    spread = collections.Counter(t['bitrate'] // 1000 for t in tracks)
    print('  Music: %d converted, %d cached, %d failed; manifest has %d tracks.'
          % (stats['converted'], stats['cached'], stats['failed'], len(tracks)))
    if spread:
        print('    bitrates: ' + ', '.join(
            '%d kbps x%d' % (k, n) for k, n in sorted(spread.items())))
    return stats
