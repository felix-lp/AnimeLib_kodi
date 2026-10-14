# -*- coding: utf-8 -*-
"""Turns AnimeLib episode "players" into playable stream URLs."""
from urllib.parse import quote

from . import kodik

TRANSLATION_SUBS = 1
TRANSLATION_VOICE = 2


class Stream(object):
    def __init__(self, url, quality=None, subtitles=None, headers=None):
        self.url = url
        self.quality = quality
        self.subtitles = subtitles or []
        self.headers = headers or {}


def describe(player):
    """Human readable label for the translation selection dialog."""
    team = (player.get('team') or {}).get('name') or 'Без названия'
    ttype = (player.get('translation_type') or {}).get('label') or ''
    source = player.get('player') or ''
    parts = [team]
    if ttype:
        parts.append(ttype)
    if is_native(player):
        best = max([int(q.get('quality') or 0) for q in (player.get('video') or {}).get('quality') or []] or [0])
        source = 'AnimeLib' + (' %dp' % best if best else '')
    if source:
        parts.append(source)
    return ' · '.join(parts)


def is_native(player):
    return (player.get('player') or '').lower() == 'animelib'


def is_supported(player):
    name = (player.get('player') or '').lower()
    if name == 'kodik':
        return bool(player.get('src'))
    if name == 'animelib':
        return bool(((player.get('video') or {}).get('quality')))
    return False


def _pick_quality(options, max_quality):
    """options: {quality: value}. Picks the best quality <= max_quality (0 = no limit)."""
    qualities = sorted(options)
    if max_quality:
        allowed = [q for q in qualities if q <= max_quality]
        if allowed:
            return allowed[-1]
        return qualities[0]
    return qualities[-1]


def _safe_url(url):
    # The main AnimeLib video server URL contains a cyrillic letter (".аs").
    return quote(url, safe=':/?&=%#+@,;~')


def resolve(player, max_quality=0, video_servers=None, server_id='main',
            referer='https://anilib.me/'):
    name = (player.get('player') or '').lower()
    if name == 'kodik':
        links = kodik.resolve(player['src'], referer=referer)
        q = _pick_quality(links, max_quality)
        return Stream(links[q], quality=q)

    if name == 'animelib':
        variants = {}
        for v in (player.get('video') or {}).get('quality') or []:
            try:
                variants[int(v.get('quality'))] = v.get('href')
            except (TypeError, ValueError):
                continue
        variants = {k: v for k, v in variants.items() if v}
        if not variants:
            raise ValueError('No video variants')
        q = _pick_quality(variants, max_quality)
        href = variants[q]
        if not href.startswith(('http://', 'https://', '//')):
            servers = video_servers or []
            base = next((s['url'] for s in servers if s.get('id') == server_id), None)
            if base is None and servers:
                base = servers[0]['url']
            # Same as the site's player: server url + href
            base = base or 'https://video1.cdnlibs.org/.аs/'
            if base.endswith('/') and href.startswith('/'):
                href = href[1:]
            href = base + href
        elif href.startswith('//'):
            href = 'https:' + href
        subs = []
        for s in player.get('subtitles') or []:
            src = s.get('src') or s.get('url')
            if src:
                subs.append(_safe_url(src))
        return Stream(_safe_url(href), quality=q, subtitles=subs,
                      headers={'Referer': referer})

    raise ValueError('Unsupported player: %s' % player.get('player'))
