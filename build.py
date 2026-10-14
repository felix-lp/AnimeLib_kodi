#!/usr/bin/env python3
"""Packs the add-on into dist/plugin.video.animelib-<version>.zip for "Install from zip file"."""
import os
import re
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
ADDON_ID = 'plugin.video.animelib'
SRC = os.path.join(ROOT, ADDON_ID)


def main():
    with open(os.path.join(SRC, 'addon.xml'), encoding='utf-8') as f:
        version = re.search(r'<addon[^>]*\sversion="([^"]+)"', f.read()).group(1)
    os.makedirs(os.path.join(ROOT, 'dist'), exist_ok=True)
    target = os.path.join(ROOT, 'dist', '%s-%s.zip' % (ADDON_ID, version))
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as zf:
        for folder, dirs, files in os.walk(SRC):
            dirs[:] = [d for d in dirs if d != '__pycache__']
            for name in files:
                if name.endswith(('.pyc', '.pyo')):
                    continue
                path = os.path.join(folder, name)
                zf.write(path, os.path.relpath(path, ROOT))
    print(target)


if __name__ == '__main__':
    main()
