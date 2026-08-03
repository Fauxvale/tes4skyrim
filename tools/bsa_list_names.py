"""List all file paths inside a BSA (v103/104/105) — names only, no data.

Reuses the same layout facts as asset_convert.bsa_extract.read_bsa_files.
Usage: python temp/bsa_list_names.py <bsa> [substring-filter]
"""
import struct
import sys


def list_names(bsa_path):
    with open(bsa_path, 'rb') as fh:
        head = fh.read(36)
        if head[:4] != b'BSA\x00':
            raise ValueError('not a BSA')
        (version, dir_offset, flags, folder_count, file_count, _,
         total_fname_len, _) = struct.unpack_from('<IIIIIIII', head, 4)
        fh.seek(dir_offset)
        folder_counts = []
        for _ in range(folder_count):
            if version >= 105:
                _h, cnt, _unk, _off = struct.unpack('<QIIq', fh.read(24))
            else:
                _h, cnt, _off = struct.unpack('<QII', fh.read(16))
            folder_counts.append(cnt)
        names = []
        folders = []
        for cnt in folder_counts:
            ln = fh.read(1)[0]
            folder = fh.read(ln)[:-1].decode('latin1')
            folders.append((folder, cnt))
            fh.seek(cnt * 16, 1)          # skip file records
        # file name block
        blob = fh.read(total_fname_len)
        fnames = blob.split(b'\x00')
        i = 0
        for folder, cnt in folders:
            for _ in range(cnt):
                names.append(folder + '\\' + fnames[i].decode('latin1'))
                i += 1
        return names


if __name__ == '__main__':
    filt = sys.argv[2].lower() if len(sys.argv) > 2 else ''
    for n in list_names(sys.argv[1]):
        if filt in n.lower():
            print(n)
