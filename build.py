#!/usr/bin/env python3
"""Builds installable zips and a Kodi repository (addons.xml + addons.xml.md5 + zips).

    python3 build.py               # -> dist/<id>-<version>.zip for every add-on
    python3 build.py --repo DIR    # -> Kodi repository layout in DIR (published to gh-pages by CI)
"""
import argparse
import hashlib
import os
import re
import shutil
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
ADDONS = ['plugin.video.animelib', 'repository.animelib']


def addon_xml(addon_id):
    with open(os.path.join(ROOT, addon_id, 'addon.xml'), encoding='utf-8') as f:
        return f.read()


def addon_version(addon_id):
    return re.search(r'<addon[^>]*\sversion="([^"]+)"', addon_xml(addon_id)).group(1)


def make_zip(addon_id, target):
    src = os.path.join(ROOT, addon_id)
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as zf:
        for folder, dirs, files in os.walk(src):
            dirs[:] = sorted(d for d in dirs if d != '__pycache__')
            for name in sorted(files):
                if name.endswith(('.pyc', '.pyo')):
                    continue
                path = os.path.join(folder, name)
                zf.write(path, os.path.relpath(path, ROOT))
    return target


def build_zips(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for addon_id in ADDONS:
        print(make_zip(addon_id, os.path.join(out_dir, '%s-%s.zip' % (addon_id, addon_version(addon_id)))))


def build_repo(out_dir):
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    entries = []
    for addon_id in ADDONS:
        version = addon_version(addon_id)
        addon_dir = os.path.join(out_dir, addon_id)
        os.makedirs(addon_dir)
        make_zip(addon_id, os.path.join(addon_dir, '%s-%s.zip' % (addon_id, version)))
        for asset in ('icon.png', 'fanart.jpg'):
            path = os.path.join(ROOT, addon_id, asset)
            if os.path.exists(path):
                shutil.copy(path, addon_dir)
        xml = re.sub(r'^<\?xml[^>]*\?>\s*', '', addon_xml(addon_id).strip())
        entries.append(xml)
        print('%s %s' % (addon_id, version))
    addons = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>\n%s\n</addons>\n' % '\n'.join(entries)
    with open(os.path.join(out_dir, 'addons.xml'), 'w', encoding='utf-8') as f:
        f.write(addons)
    with open(os.path.join(out_dir, 'addons.xml.md5'), 'w') as f:
        f.write(hashlib.md5(addons.encode('utf-8')).hexdigest())
    # Plain index so the folder can also be added as a Kodi file source.
    with open(os.path.join(out_dir, 'index.html'), 'w', encoding='utf-8') as f:
        name = 'repository.animelib-%s.zip' % addon_version('repository.animelib')
        f.write('<!DOCTYPE html>\n<a href="repository.animelib/%s">%s</a>\n' % (name, name))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', metavar='DIR', help='build a Kodi repository into DIR')
    args = parser.parse_args()
    if args.repo:
        build_repo(os.path.abspath(args.repo))
    else:
        build_zips(os.path.join(ROOT, 'dist'))


if __name__ == '__main__':
    main()
