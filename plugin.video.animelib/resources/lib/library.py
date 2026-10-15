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
ANIME_TAG = 'Аниме'
NODES_DIR = 'special://profile/library/video/'
SYSTEM_NODES_DIR = 'special://xbmc/system/library/video/'
NODE_FOLDER = 'animelib/'


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
            '  <tag>%s</tag>\n'
            '  <uniqueid type="animelib" default="true">%s</uniqueid>\n</tvshow>\n') % (
        escape(title), escape(anime.get('name') or ''),
        escape(animelib.summary_text(anime.get('summary'))),
        date[:4], date, animelib.rating_of(anime), genres, ANIME_TAG,
        escape(anime.get('slug_url') or ''))


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
        anime = api.anime(slug, anonymous=True)
        episodes = api.episodes(slug, anonymous=True)
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


# --- "Anime" section of the video library ----------------------------------------
def _copy_tree(src, dst):
    xbmcvfs.mkdirs(dst)
    dirs, files = xbmcvfs.listdir(src)
    for name in files:
        xbmcvfs.copy(src + name, dst + name)
    for name in dirs:
        _copy_tree(src + name + '/', dst + name + '/')


def _node(order, label, icon, content, rules, match='all', sort=None, limit=None):
    body = ''.join('  <rule field="%s" operator="%s">%s</rule>\n' % (
        field, op, '<value>%s</value>' % escape(value) if value is not None else '')
        for field, op, value in rules)
    extra = ''
    if sort:
        extra += '  <order direction="%s">%s</order>\n' % (sort[1], sort[0])
    if limit:
        extra += '  <limit>%d</limit>\n' % limit
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n'
            '<node order="%d" type="filter">\n  <label>%s</label>\n  <icon>%s</icon>\n'
            '  <content>%s</content>\n  <match>%s</match>\n%s%s</node>\n') % (
        order, escape(label), icon, content, match, body, extra)


def path_marker(root):
    """Substring of library paths that identifies titles exported by the add-on.
    Kodi may store the source path as special://... or translated, so for the default
    folder only the add-on id is used."""
    if root.startswith('special://'):
        return 'plugin.video.animelib'
    return root


def ensure_anime_node(root, labels):
    """Adds an "Anime" section next to Movies/TV shows. Kodi uses
    special://profile/library/video/ *instead of* the default nodes once it exists,
    so the defaults are copied there first. labels: dict with section/all/inprogress/recent."""
    if not xbmcvfs.exists(NODES_DIR):
        _copy_tree(SYSTEM_NODES_DIR, NODES_DIR)
    folder = NODES_DIR + NODE_FOLDER
    xbmcvfs.mkdirs(folder)
    marker = path_marker(root)
    _write(folder + 'index.xml',
           '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n'
           '<node order="25" visible="Library.HasContent(TVShows)">\n'
           '  <label>%s</label>\n  <icon>DefaultTVShows.png</icon>\n</node>\n' % escape(labels['section']))
    _write(folder + 'titles.xml', _node(
        1, labels['all'], 'DefaultTVShowTitle.png', 'tvshows',
        [('path', 'contains', marker), ('tag', 'is', ANIME_TAG)], match='any',
        sort=('sorttitle', 'ascending')))
    _write(folder + 'inprogress.xml', _node(
        2, labels['inprogress'], 'DefaultInProgressShows.png', 'tvshows',
        [('path', 'contains', marker), ('inprogress', 'true', None)],
        sort=('lastplayed', 'descending')))
    _write(folder + 'recent.xml', _node(
        3, labels['recent'], 'DefaultRecentlyAddedEpisodes.png', 'episodes',
        [('path', 'contains', marker)], sort=('dateadded', 'descending'), limit=50))
    _write(folder + 'genres.xml', _node(
        4, labels['genres'], 'DefaultGenre.png', 'tvshows',
        [('path', 'contains', marker)]).replace('<content>tvshows</content>',
                                               '<content>tvshows</content>\n  <group>genres</group>'))
    return folder
