# -*- coding: utf-8 -*-
"""Descriptions and genres from Shikimori.

AnimeLib copies its descriptions from Shikimori but drops the character links,
so names vanish from the text ("встречи с  — мудрой волчицей"). Shikimori's
GraphQL API returns the full HTML for up to 50 titles per request.
"""
import html
import re

from . import http

GRAPHQL_URL = 'https://shikimori.io/api/graphql'
BATCH = 50
HEADERS = {'User-Agent': 'AnimeLib Kodi add-on', 'Accept': 'application/json'}


def shiki_id(anime):
    """Shikimori id from AnimeLib's shikimori_href (".../animes/z51122-name")."""
    m = re.search(r'/animes/[a-z]*(\d+)', anime.get('shikimori_href') or '')
    return m.group(1) if m else None


def html_to_text(value):
    if not value:
        return ''
    text = re.sub(r'<br\s*/?>|</p>|</div>', '\n', value, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def details(ids):
    """{shiki_id: {'plot': str, 'genres': [str]}} for the given ids (missing ones are skipped)."""
    ids = [str(i) for i in ids if i]
    result = {}
    for start in range(0, len(ids), BATCH):
        chunk = ids[start:start + BATCH]
        query = '{ animes(ids: "%s", limit: %d) { id descriptionHtml genres { russian kind } } }' % (
            ','.join(chunk), len(chunk))
        try:
            resp = http.fetch(GRAPHQL_URL, json_body={'query': query}, headers=HEADERS, timeout=15)
        except http.HttpError:
            break
        if resp.code != 200:
            break
        try:
            animes = (resp.json().get('data') or {}).get('animes') or []
        except http.HttpError:
            break
        for anime in animes:
            plot = html_to_text(anime.get('descriptionHtml'))
            genres = [g.get('russian') for g in anime.get('genres') or []
                      if g.get('kind') == 'genre' and g.get('russian')]
            result[str(anime.get('id'))] = {'plot': plot, 'genres': genres}
    return result
