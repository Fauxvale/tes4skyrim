"""Rebuild the minimal LGPL ffmpeg.exe that ships in external/ffmpeg/.

The Sounds phase runs exactly one ffmpeg command -- decode MP3/WAV, write
mono 44.1 kHz PCM WAV -- so shipping a stock build means carrying ~120 MB of
codecs for a job that needs three. This script builds a static binary with
everything else compiled out: 1.05 MB, no DLLs, LGPL v2.1 (no --enable-gpl,
no --enable-version3).

It is also the project's LGPL section 6 compliance artifact. A statically
linked LGPL binary obliges us to let recipients relink against a modified
FFmpeg, which is satisfied by publishing the exact recipe -- the pinned
upstream tarball plus every configure flag below. No FFmpeg source is patched.

Usage
-----
    python tools/build_ffmpeg.py                     # -> external/ffmpeg/ffmpeg.exe
    python tools/build_ffmpeg.py --output /tmp/f.exe # somewhere else
    python tools/build_ffmpeg.py --keep-build        # leave the tree for inspection
    python tools/build_ffmpeg.py --jobs 8            # limit parallelism

Requirements
------------
A Linux environment with a MinGW-w64 cross-compiler. On Windows, run this
from WSL, or run it from Windows and it will drive `wsl` itself.

No root required. When the toolchain is missing, `--bootstrap` fetches the
Ubuntu packages with `apt-get download` and unpacks them into ~/.cache, which
needs no privileges -- `sudo apt-get install` would need a password this
script has no way to supply.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_OUTPUT = PROJECT_ROOT / 'external' / 'ffmpeg' / 'ffmpeg.exe'

# Pinned upstream release. Not master: a moving source makes the shipped
# binary unreproducible, and the LGPL recipe has to name an exact tree.
FFMPEG_VERSION = '7.1.2'
FFMPEG_URL = f'https://ffmpeg.org/releases/ffmpeg-{FFMPEG_VERSION}.tar.xz'

# The authoritative record of how external/ffmpeg/ffmpeg.exe was produced.
#
# Only the codecs the Sounds phase can encounter are enabled. `-ac 1 -ar
# 44100` builds an aresample graph internally, so those filters are required
# even though the command line never names them.
CONFIGURE_FLAGS = [
    '--arch=x86_64', '--target-os=mingw32',
    '--cross-prefix=x86_64-w64-mingw32-',
    # Strip everything, then add back only what the one ffmpeg invocation in
    # audio_converter.convert_file_to_xwm actually needs.
    '--disable-everything', '--disable-shared', '--enable-static',
    '--disable-doc', '--disable-htmlpages', '--disable-manpages',
    '--disable-podpages', '--disable-txtpages',
    '--disable-network', '--disable-autodetect', '--disable-iconv',
    '--disable-zlib', '--disable-bzlib', '--disable-lzma',
    '--disable-sdl2', '--disable-schannel', '--disable-debug',
    '--disable-programs', '--enable-ffmpeg',
    '--disable-avdevice', '--disable-postproc', '--disable-swscale',
    # Oblivion voice lines are MP3; sound effects are PCM WAV. adpcm_ms and
    # the wider PCM widths cover the handful of odd files in the BSAs.
    '--enable-decoder=mp3,mp3float,pcm_s16le,pcm_u8,pcm_s24le,pcm_s32le,'
    'pcm_f32le,adpcm_ms',
    '--enable-encoder=pcm_s16le',
    '--enable-demuxer=mp3,wav,aiff',
    '--enable-muxer=wav',
    '--enable-parser=mpegaudio',
    '--enable-protocol=file',
    '--enable-filter=aresample,aformat,anull,atrim,volume',
    '--enable-small',
]

# Ubuntu packages for the no-root bootstrap. gcc-13 and cpp-13 are only
# symlinks and docs -- the real driver and cc1 live in the -x86-64-linux-gnu
# packages, which is easy to miss and fails late with "No such file".
HOST_PACKAGES = [
    'gcc-13', 'cpp-13', 'gcc-13-x86-64-linux-gnu', 'cpp-13-x86-64-linux-gnu',
    'libgcc-13-dev', 'libc6-dev', 'libc6', 'libgcc-s1', 'linux-libc-dev',
    'libcrypt-dev', 'libc-dev-bin', 'gcc-13-base',
    'binutils', 'binutils-common', 'binutils-x86-64-linux-gnu', 'libbinutils',
    'libctf0', 'libctf-nobfd0', 'libsframe1', 'libgprofng0', 'libjansson4',
    'libisl23', 'libmpc3', 'libmpfr6', 'libgmp10',
    # mp3 decode is the Sounds phase inner loop; keep the SIMD paths.
    'nasm',
]
CROSS_PACKAGES = [
    'mingw-w64-x86-64-dev', 'gcc-mingw-w64-x86-64-win32',
    'binutils-mingw-w64-x86-64', 'mingw-w64-common',
]

# Bootstrap lives under ~/.cache so a rebuild reuses it.
CACHE = '$HOME/.cache/tesconv-ffmpeg'


def _bash(script: str, *, check: bool = True, capture: bool = False):
    """Run a bash script in the Linux environment, via WSL when on Windows."""
    if sys.platform == 'win32':
        cmd = ['wsl', '-e', 'bash', '-lc', script]
    else:
        cmd = ['bash', '-lc', script]
    if capture:
        return subprocess.run(cmd, check=check, capture_output=True, text=True)
    return subprocess.run(cmd, check=check)


def _have_toolchain() -> bool:
    r = _bash(
        f'PATH={CACHE}/cross/usr/bin:{CACHE}/host/usr/bin:$PATH; '
        'command -v x86_64-w64-mingw32-gcc-win32 >/dev/null '
        f'&& test -x {CACHE}/hostcc && command -v nasm >/dev/null',
        check=False, capture=True)
    return r.returncode == 0


def bootstrap() -> None:
    """Fetch and unpack the toolchains into ~/.cache without root.

    `apt-get download` + `dpkg-deb -x` needs no privileges, unlike
    `apt-get install`. A private apt state dir is used because the system
    index is often stale enough that some .debs 404.
    """
    print('Bootstrapping toolchain (no root required)...')
    host = ' '.join(HOST_PACKAGES)
    cross = ' '.join(CROSS_PACKAGES)
    script = f'''
set -e
C={CACHE}
mkdir -p $C/debs $C/aptroot/etc/apt $C/aptroot/var/lib/apt/partial \\
         $C/aptroot/var/cache/apt/archives/partial $C/aptroot/var/lib/dpkg
cp /etc/apt/sources.list $C/aptroot/etc/apt/ 2>/dev/null || true
cp -r /etc/apt/sources.list.d $C/aptroot/etc/apt/ 2>/dev/null || true
touch $C/aptroot/var/lib/dpkg/status
APT="-o Dir::Etc::sourcelist=$C/aptroot/etc/apt/sources.list \\
     -o Dir::Etc::sourceparts=$C/aptroot/etc/apt/sources.list.d \\
     -o Dir::State=$C/aptroot/var/lib/apt \\
     -o Dir::Cache=$C/aptroot/var/cache/apt \\
     -o Dir::State::status=$C/aptroot/var/lib/dpkg/status"

# The system index may be stale; a private refresh avoids 404s on .debs.
echo "  refreshing package index..."
apt-get $APT update >/dev/null 2>&1 || true

cd $C/debs
echo "  downloading host toolchain..."
apt-get $APT download {host} >/dev/null
echo "  downloading mingw cross-compiler..."
apt-get $APT download {cross} >/dev/null

rm -rf $C/host $C/cross
mkdir -p $C/host $C/cross
for d in $C/debs/*mingw*.deb; do dpkg-deb -x "$d" $C/cross/; done
for d in $C/debs/*.deb; do
  case "$d" in *mingw*) ;; *) dpkg-deb -x "$d" $C/host/ ;; esac
done

# Ubuntu's /lib -> /usr/lib merge; without these the linker looks for
# ld-linux and libc.so.6 at absolute paths that don't exist in the sysroot.
ln -sfn usr/lib $C/host/lib
ln -sfn usr/lib64 $C/host/lib64

# The host compiler builds ffmpeg's build-time codegen tools. It must be a
# wrapper: the extracted tree is not a real sysroot, so every search path
# has to be passed explicitly.
cat > $C/hostcc <<WRAP
#!/bin/bash
R=$C/host
exec \\$R/usr/bin/x86_64-linux-gnu-gcc-13 \\\\
  --sysroot=\\$R \\\\
  -B \\$R/usr/lib/gcc/x86_64-linux-gnu/13 \\\\
  -B \\$R/usr/libexec/gcc/x86_64-linux-gnu/13 \\\\
  -B \\$R/usr/bin \\\\
  -Wl,-rpath-link,\\$R/usr/lib/x86_64-linux-gnu \\\\
  -Wl,--dynamic-linker=/lib64/ld-linux-x86-64.so.2 \\\\
  "\\$@"
WRAP
chmod +x $C/hostcc
echo "  toolchain ready"
'''
    _bash(script)


def build(output: Path, jobs: int, keep_build: bool) -> None:
    src = f'{CACHE}/src'
    flags = ' \\\n  '.join(CONFIGURE_FLAGS)
    script = f'''
set -e
C={CACHE}
export PATH=$C/cross/usr/bin:$C/host/usr/bin:$PATH
mkdir -p {src}
cd {src}
TARBALL=ffmpeg-{FFMPEG_VERSION}.tar.xz
# Re-download when the file is missing OR truncated from an interrupted run;
# a partial tarball would otherwise fail much later inside `tar`.
if [ ! -s "$TARBALL" ] || ! tar tf "$TARBALL" >/dev/null 2>&1; then
  echo "Downloading FFmpeg {FFMPEG_VERSION}..."
  rm -f "$TARBALL"
  for attempt in 1 2 3; do
    if curl -fsSL --max-time 600 --retry 3 -o "$TARBALL" {FFMPEG_URL}; then
      break
    fi
    echo "  download attempt $attempt failed; retrying..."
    rm -f "$TARBALL"
    sleep 2
  done
  if [ ! -s "$TARBALL" ]; then
    echo "ERROR: could not download {FFMPEG_URL}" >&2
    exit 1
  fi
fi
rm -rf ffmpeg-{FFMPEG_VERSION}
tar xf ffmpeg-{FFMPEG_VERSION}.tar.xz
cd ffmpeg-{FFMPEG_VERSION}

echo "Configuring..."
./configure \\
  --cc=x86_64-w64-mingw32-gcc-win32 \\
  --ld=x86_64-w64-mingw32-gcc-win32 \\
  --host-cc=$C/hostcc \\
  {flags} > /tmp/ffconf.log 2>&1 || {{ tail -25 /tmp/ffconf.log; exit 1; }}

grep -E '^License:' /tmp/ffconf.log || true

echo "Building..."
make -j{jobs} > /tmp/ffmake.log 2>&1 || {{ tail -25 /tmp/ffmake.log; exit 1; }}
ls -la ffmpeg.exe
'''
    _bash(script)

    # Copy out of the Linux tree.
    output.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == 'win32':
        drive = output.drive.rstrip(':').lower()
        wsl_path = f"/mnt/{drive}{output.as_posix()[len(output.drive):]}"
        _bash(f'cp {src}/ffmpeg-{FFMPEG_VERSION}/ffmpeg.exe "{wsl_path}"')
        lic = output.parent / 'COPYING.LGPLv2.1'
        lic_wsl = f"/mnt/{drive}{lic.as_posix()[len(lic.drive):]}"
        _bash(f'cp {src}/ffmpeg-{FFMPEG_VERSION}/COPYING.LGPLv2.1 "{lic_wsl}"',
              check=False)
    else:
        shutil.copyfile(f'{Path.home()}/.cache/tesconv-ffmpeg/src/'
                        f'ffmpeg-{FFMPEG_VERSION}/ffmpeg.exe', output)

    if not keep_build:
        _bash(f'rm -rf {src}/ffmpeg-{FFMPEG_VERSION}', check=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--output', type=Path, default=DEFAULT_OUTPUT,
                    help='where to write ffmpeg.exe '
                         '(default: external/ffmpeg/ffmpeg.exe)')
    ap.add_argument('--jobs', type=int, default=0,
                    help='parallel make jobs (default: all cores)')
    ap.add_argument('--keep-build', action='store_true',
                    help='keep the unpacked source tree after building')
    ap.add_argument('--bootstrap', action='store_true',
                    help='force a toolchain re-fetch even if one is cached')
    args = ap.parse_args()

    jobs = args.jobs
    if jobs <= 0:
        r = _bash('nproc', capture=True)
        jobs = int(r.stdout.strip() or 4)

    if args.bootstrap or not _have_toolchain():
        bootstrap()
    else:
        print('Toolchain already bootstrapped.')

    build(args.output, jobs, args.keep_build)

    size = args.output.stat().st_size
    print(f'\nWrote {args.output} ({size / 1e6:.2f} MB)')
    print('License: LGPL v2.1 or later (statically linked, no DLLs)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
