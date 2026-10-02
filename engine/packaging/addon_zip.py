# -*- coding: utf-8 -*-
"""アドオンのフォルダを zip にする（scripts/build-addon.mjs から呼ぶ）。

    python addon_zip.py <フォルダ> <出力.zip>

同じ中身なら同じバイト列になるように、名前の順に並べ、日時と属性を固定する（SHA-256 を照らせるように）。
"""
import os
import sys
import zipfile

FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def main(src, out):
    paths = []
    for root, dirs, files in os.walk(src):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(root, name)
            paths.append((os.path.relpath(full, src).replace(os.sep, "/"), full))
    paths.sort()
    tmp = out + ".part"
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for arc, full in paths:
            info = zipfile.ZipInfo(arc, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(full, "rb") as f, z.open(info, "w", force_zip64=True) as dst:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
    os.replace(tmp, out)
    print("zip: %d files -> %s" % (len(paths), out))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
