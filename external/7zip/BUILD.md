# 7-Zip — bundled console binary

Used by `asset_convert/archive.py` to read mod archives (`.zip`, `.7z`, `.rar`)
during **Mods ▸ Import Mod Archive…** / `convert.py --import-mod`.

| File | Size | Purpose |
|---|---|---|
| `7z.exe` | 0.55 MB | console front end |
| `7z.dll` | 1.82 MB | format + codec library, **including RAR and RAR5** |
| `License.txt` | 5 KB | 7-Zip LGPL + unRAR extraction-only licence |

## Version

7-Zip **26.02** (x64), build date 2026-06-25.

Source: <https://www.7-zip.org/> — the full Windows x64 package
(`7z2602-x64.exe`), from which only `7z.exe` and `7z.dll` are taken.

## Why `7z.exe` + `7z.dll` and NOT `7za.exe`

`7za.exe` is the "standalone" console binary from the `7zXXXX-extra.7z` package,
and it is the obvious-looking choice. **It cannot read RAR.** Its format list is
7z / bzip2 / gzip / zip only, and it fails on a real `.rar` with:

```
ERROR: <file>.rar : Cannot open the file as archive
```

RAR and RAR5 support lives in `7z.dll`, so the two files must ship together.
Verified by copying only these two files to an empty directory and listing a
real Oblivion mod `.rar` with them — no install, no registry keys, no `Codecs/`
subfolder required.

```
$ 7z.exe i | grep Rar
 0  ...F..................  Rar      rar r00
 0  ...F..................  Rar5     rar r00
 0   D     40301 Rar1 … 40305 Rar5
```

## Licence

7-Zip is licensed under the **GNU LGPL**, with the unRAR decoder carrying its
own restriction: the RAR decompression code may not be used to develop a RAR
*compressor*. This project only ever **reads** archives, so that restriction is
satisfied. See `License.txt`, shipped verbatim beside the binaries.

This mirrors how `external/ffmpeg/` is bundled (LGPL, with `COPYING.LGPLv2.1`
committed alongside).

## Updating

1. Download the current 7-Zip x64 installer from <https://www.7-zip.org/>.
2. Extract (or install and copy) `7z.exe`, `7z.dll` and `License.txt`.
3. Confirm RAR support survived: `7z.exe i` must list `Rar` and `Rar5`.
4. Update the version and build date above.
