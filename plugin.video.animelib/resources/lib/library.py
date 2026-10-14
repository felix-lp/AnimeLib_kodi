# -*- coding: utf-8 -*-
"""Kodi video library export.

Every title becomes a folder with tvshow.nfo, poster/fanart and one .strm file per
episode pointing back to the plugin. Kodi scans the folder as a "TV shows" source
with the "Local information only" scraper.
"""
import re
from xml.sax.saxutils import escape

import xbmcvfs

from . import animelib, http

PLUGIN_URL = 'plugin://plugin.video.animelib/'


def _join(folder, name):
    return folder.rstrip('/\\') + '/' + name


def _write(path, data):
    f = xbmcvfs.File(path, 'w')
    try:
        f.write(bytearray(data.encode('utf-8') if isinstance(data, str) else data))
    finally:
        f.close()


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', name or '')
    name = re.sub(r'\s+', ' ', name).strip().rstrip('.')
    return name[:100] or 'anime'


def episode_numbers(episodes):
    """Yields (episode, season, number) with Kodi-friendly integers.
    Fractional episodes (e.g. 12.5) go to season 0 as specials."""
    used = set()
    special = 0
    for index, ep in enumerate(episodes, 1):
        try:
            season = int(ep.get('season') or 1)
        except ValueError:
            season = 1
        raw = str(ep.get('number') or '')
        try:
            number = float(raw)
        except ValueError:
            number = None
        if number is None or number != int(number) or (season, int(number)) in used:
            special += 1
            season, number = 0, special
        number = int(number)
        used.add((season, number))
        yield ep, season, number


def tvshow_nfo(anime, title):
    genres = ''.join('  <genre>%s</genre>\n' % escape(g.get('name'))
                     for g in anime.get('genres') or [] if g.get('name'))
    date = anime.get('releaseDate') or ''
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<tvshow>\n'
            '  <title>%s</title>\n  <originaltitle>%s</originaltitle>\n  <plot>%s</plot>\n'
            '  <year>%s</year>\n  <premiered>%s</premiered>\n  <rating>%s</rating>\n%s'
            '  <uniqueid type="animelib" default="true">%s</uniqueid>\n</tvshow>\n') % (
        escape(title), escape(anime.get('name') or ''),
        escape(animelib.summary_text(anime.get('summary'))),
        date[:4], date, animelib.rating_of(anime), genres, escape(anime.get('slug_url') or ''))


def episode_nfo(ep, season, number, show_title):
    title = ep.get('name') or 'Эпизод %s' % (ep.get('number') or number)
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<episodedetails>\n'
            '  <title>%s</title>\n  <showtitle>%s</showtitle>\n  <season>%d</season>\n'
            '  <episode>%d</episode>\n  <aired>%s</aired>\n</episodedetails>\n') % (
        escape(title), escape(show_title), season, number, (ep.get('created_at') or '')[:10])


def strm(ep, slug, index):
    return '%s?action=play&id=%s&slug=%s&n=%s' % (PLUGIN_URL, ep['id'], slug,
                                                  ep.get('item_number') or index)


class Library(object):
    def __init__(self, root, store, referer):
        """store: JsonStore with data['titles'] = {slug: {'folder', 'title'}}."""
        self.root = root.rstrip('/\\') + '/'
        self.store = store
        self.store.data.setdefault('titles', {})
        self.referer = referer

    @property
    def titles(self):
        return self.store.data['titles']

    def contains(self, slug):
        return slug in self.titles

    def _image(self, url, path):
        if not url or xbmcvfs.exists(path):
            return
        try:
            _write(path, http.download(url, headers={'Referer': self.referer}))
        except http.HttpError:
            pass

    def add(self, api, slug, title):
        """Creates/updates the title folder. Returns the number of new episodes."""
        anime = api.anime(slug)
        episodes = api.episodes(slug)
        entry = self.titles.get(slug)
        if entry:
            folder = entry['folder']
        else:
            year = (anime.get('releaseDate') or '')[:4]
            name = safe_name(title) + (' (%s)' % year if year.isdigit() else '')
            folder = self.root + name + '/'
            if any(e['folder'] == folder for e in self.titles.values()):
                folder = self.root + '%s [%s]/' % (name, anime.get('id'))
        if not xbmcvfs.exists(folder):
            xbmcvfs.mkdirs(folder)
        _write(_join(folder, 'tvshow.nfo'), tvshow_nfo(anime, title))
        cover = anime.get('cover') or {}
        self._image(cover.get('default') or cover.get('thumbnail'), _join(folder, 'poster.jpg'))
        self._image((anime.get('background') or {}).get('url'), _join(folder, 'fanart.jpg'))

        added = 0
        for index, (ep, season, number) in enumerate(episode_numbers(episodes), 1):
            base = _join(folder, 'S%02dE%02d' % (season, number))
            if xbmcvfs.exists(base + '.strm'):
                continue
            _write(base + '.strm', strm(ep, slug, index))
            _write(base + '.nfo', episode_nfo(ep, season, number, title))
            added += 1
        self.titles[slug] = {'folder': folder, 'title': title}
        self.store.save()
        return added

    def remove(self, slug):
        entry = self.titles.pop(slug, None)
        self.store.save()
        if entry and xbmcvfs.exists(entry['folder']):
            xbmcvfs.rmdir(entry['folder'], True)
        return entry

    def update_all(self, api, log=None):
        """Adds new episodes for every title. Returns the total number of new episodes."""
        total = 0
        for slug, entry in list(self.titles.items()):
            try:
                total += self.add(api, slug, entry.get('title') or slug)
            except http.HttpError as e:
                if log:
                    log('Library update failed for %s: %s' % (slug, e))
        return total
