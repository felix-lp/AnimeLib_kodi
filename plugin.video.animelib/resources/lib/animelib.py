# -*- coding: utf-8 -*-
"""Client for the public AnimeLib API (the one used by anilib.me itself)."""
from . import http

DEFAULT_API = 'https://api.cdnlibs.org/api'
SITE_ID = '5'  # 5 = AnimeLib in the *lib family of sites
PER_PAGE = 60

ANIME_FIELDS = ['summary', 'genres', 'releaseDate', 'background', 'episodes_count']

ANIME_FOLDER_WATCHING = 21  # default "Смотрю" list for anime

# Fallback for constants?fields[]=videoServers
DEFAULT_VIDEO_SERVERS = [
    {'id': 'main', 'url': 'https://video1.cdnlibs.org/.%D0%B0s/'},
    {'id': 'secondary_1', 'url': 'https://video2.cdnlibs.org/'},
    {'id': 'secondary_2', 'url': 'https://video1.imglib.info/'},
]


class AnimeLib(object):
    def __init__(self, api_url=DEFAULT_API, site_url='https://anilib.me', token_provider=None):
        """token_provider(force_refresh=False) -> access token or None."""
        self.api = (api_url or DEFAULT_API).rstrip('/')
        self.site = site_url.rstrip('/')
        self.token_provider = token_provider
        self.headers = {
            'Site-Id': SITE_ID,
            'Accept': 'application/json',
            'Referer': self.site + '/',
            'Origin': self.site,
        }

    def _headers(self, force_refresh=False):
        headers = dict(self.headers)
        token = self.token_provider(force_refresh) if self.token_provider else None
        if token:
            headers['Authorization'] = 'Bearer ' + token
        return headers

    def _call(self, path, params=None, json_body=None, method=None):
        url = self.api + path
        resp = http.fetch(url, params=params, json_body=json_body, method=method,
                          headers=self._headers())
        if resp.code == 401 and self.token_provider:
            resp = http.fetch(url, params=params, json_body=json_body, method=method,
                              headers=self._headers(force_refresh=True))
        if resp.code >= 400:
            raise http.HttpError('HTTP %d: %s' % (resp.code, url), resp.code, resp.text)
        return resp.json() if resp.text.strip() else {}

    def _get(self, path, params=None):
        resp = self._call(path, params)
        return resp.get('data', resp) if isinstance(resp, dict) else resp

    def _get_raw(self, path, params=None):
        return self._call(path, params)

    # --- account -------------------------------------------------------------
    def me(self):
        return self._get('/auth/me')

    def bookmark_folders(self, user_id):
        folders = self._get('/bookmarks/folder/%s' % user_id) or []
        return [f for f in folders if int(SITE_ID) in (f.get('site_ids') or [])]

    def bookmarks(self, user_id, folder_id, page=1, sort_by='updated_at'):
        """Returns (bookmarks, has_next). Each bookmark has 'media' (anime) and 'item_id'."""
        resp = self._get_raw('/bookmarks', {'user_id': user_id, 'status': folder_id, 'page': page,
                                            'sort_by': sort_by, 'sort_type': 'desc'})
        items = resp.get('data') or []
        return items, bool((resp.get('links') or {}).get('next'))

    def media_bookmark(self, slug_url):
        try:
            data = self._get('/anime/%s/bookmark' % slug_url)
        except http.HttpError as e:
            if e.code == 404:
                return None
            raise
        return data or None

    def set_bookmark(self, slug_url, status, item_id=None, item_number=None):
        bookmark = {'status': status}
        if item_id is not None:
            bookmark['item_id'] = item_id
        return self._call('/bookmarks', json_body={
            'media_type': 'anime', 'media_slug': slug_url, 'bookmark': bookmark,
            'meta': {'item_number': item_number} if item_number is not None else {},
        }, method='POST')

    def delete_bookmark(self, bookmark_id):
        return self._call('/bookmarks/%s' % bookmark_id, params={'media_type': 'anime'},
                          method='DELETE')

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
