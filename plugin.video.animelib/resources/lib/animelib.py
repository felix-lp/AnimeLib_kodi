# -*- coding: utf-8 -*-
"""Client for the public AnimeLib API (the one used by anilib.me itself)."""
from . import http

DEFAULT_API = 'https://api.cdnlibs.org/api'
SITE_ID = '5'  # 5 = AnimeLib in the *lib family of sites
PER_PAGE = 60

ANIME_FIELDS = ['summary', 'genres', 'releaseDate', 'background', 'episodes_count']

SORTS = {
    'last_episode_at': 'Последние обновления',
    'views': 'Популярное',
    'rate_avg': 'По рейтингу',
    'created_at': 'Новое на сайте',
}

# Fallback for constants?fields[]=videoServers
DEFAULT_VIDEO_SERVERS = [
    {'id': 'main', 'url': 'https://video1.cdnlibs.org/.%D0%B0s/'},
    {'id': 'secondary_1', 'url': 'https://video2.cdnlibs.org/'},
    {'id': 'secondary_2', 'url': 'https://video1.imglib.info/'},
]


class AnimeLib(object):
    def __init__(self, api_url=DEFAULT_API, site_url='https://anilib.me'):
        self.api = (api_url or DEFAULT_API).rstrip('/')
        self.site = site_url.rstrip('/')
        self.headers = {
            'Site-Id': SITE_ID,
            'Accept': 'application/json',
            'Referer': self.site + '/',
            'Origin': self.site,
        }

    def _get(self, path, params=None):
        resp = http.get_json(self.api + path, params=params, headers=self.headers)
        return resp.get('data', resp)

    def _get_raw(self, path, params=None):
        return http.get_json(self.api + path, params=params, headers=self.headers)

    # --- catalog -----------------------------------------------------------
    def catalog(self, page=1, query=None, sort_by=None, genres=None, types=None):
        """Returns (items, has_next)."""
        params = [('page', page)]
        if query:
            params.append(('q', query))
        if sort_by:
            params += [('sort_by', sort_by), ('sort_type', 'desc')]
        for g in genres or []:
            params.append(('genres[]', g))
        for t in types or []:
            params.append(('types[]', t))
        params += [('fields[]', f) for f in ('rate_avg', 'rate', 'releaseDate')]
        resp = self._get_raw('/anime', params)
        items = resp.get('data') or []
        links = resp.get('links') or {}
        has_next = bool(links.get('next')) if 'next' in links else len(items) >= PER_PAGE
        return items, has_next

    def constants(self, *fields):
        return self._get('/constants', [('fields[]', f) for f in fields])

    def genres(self):
        data = self.constants('genres').get('genres') or []
        return [g for g in data if not g.get('site_ids') or 5 in g.get('site_ids')]

    def types(self):
        data = self.constants('types').get('types') or []
        return [t for t in data if 5 in (t.get('site_ids') or [])]

    def video_servers(self):
        try:
            servers = self.constants('videoServers').get('videoServers')
        except http.HttpError:
            servers = None
        return servers or DEFAULT_VIDEO_SERVERS

    # --- title -------------------------------------------------------------
    def anime(self, slug_url):
        return self._get('/anime/%s' % slug_url, [('fields[]', f) for f in ANIME_FIELDS])

    def episodes(self, slug_url):
        return self._get('/episodes', {'anime_id': slug_url}) or []

    def episode(self, episode_id):
        return self._get('/episodes/%s' % episode_id)


# --- helpers ---------------------------------------------------------------
def title_of(item, prefer='rus'):
    order = ('rus_name', 'eng_name', 'name') if prefer == 'rus' else ('eng_name', 'name', 'rus_name')
    for key in order:
        if item.get(key):
            return item[key]
    return item.get('slug') or str(item.get('id', ''))


def summary_text(summary):
    """Description can be plain text or a ProseMirror document."""
    if not summary:
        return ''
    if isinstance(summary, str):
        return summary
    parts = []

    def walk(node, out):
        if isinstance(node, list):
            for n in node:
                walk(n, out)
            return
        if not isinstance(node, dict):
            return
        if node.get('type') == 'text':
            out.append(node.get('text', ''))
        elif node.get('type') == 'hardBreak':
            out.append('\n')
        walk(node.get('content') or [], out)

    for block in summary.get('content') or []:
        buf = []
        walk(block, buf)
        parts.append(''.join(buf))
    return '\n'.join(p for p in parts if p)


def rating_of(item):
    rating = item.get('rating') or {}
    for key in ('average', 'averageFormated'):
        try:
            return float(rating.get(key))
        except (TypeError, ValueError):
            pass
    try:
        return float(item.get('shiki_rate'))
    except (TypeError, ValueError):
        return 0.0


def is_adult(item):
    age = item.get('ageRestriction') or {}
    return (age.get('id') or 0) >= 4
