# -*- coding: utf-8 -*-
"""Kodi routing and UI for the AnimeLib add-on."""
import json
import os
import sys
from urllib.parse import parse_qsl, urlencode

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from . import animelib, http, kodik, players

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo('id')
ICON = ADDON.getAddonInfo('icon')
QUALITIES = [0, 1080, 720, 480, 360]
VIDEO_SERVERS = ['main', 'secondary_1', 'secondary_2']
SEARCH_HISTORY_SIZE = 20


def L(string_id):
    return ADDON.getLocalizedString(string_id)


def setting_int(key, default=0):
    try:
        return int(ADDON.getSetting(key))
    except (TypeError, ValueError):
        return default


def setting_bool(key):
    return ADDON.getSetting(key) == 'true'


def log(msg, level=xbmc.LOGINFO):
    xbmc.log('[%s] %s' % (ADDON_ID, msg), level)


def notify(msg, error=False):
    xbmcgui.Dialog().notification('AnimeLib', msg,
                                  xbmcgui.NOTIFICATION_ERROR if error else xbmcgui.NOTIFICATION_INFO,
                                  5000)


# --- persistent state (search history, chosen translations) -------------------
class State(object):
    def __init__(self):
        folder = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
        if not xbmcvfs.exists(folder):
            xbmcvfs.mkdirs(folder)
        self.path = os.path.join(folder, 'state.json')
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                self.data = json.load(f)
        except (IOError, OSError, ValueError):
            self.data = {}
        self.data.setdefault('search', [])
        self.data.setdefault('teams', {})

    def save(self):
        try:
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False)
        except (IOError, OSError) as e:
            log('Cannot save state: %s' % e, xbmc.LOGWARNING)


# --- list item helpers ---------------------------------------------------------
_INFO_SETTERS = {
    'title': 'setTitle',
    'originaltitle': 'setOriginalTitle',
    'plot': 'setPlot',
    'year': 'setYear',
    'rating': 'setRating',
    'genre': 'setGenres',
    'episode': 'setEpisode',
    'season': 'setSeason',
    'tvshowtitle': 'setTvShowTitle',
    'mediatype': 'setMediaType',
    'premiered': 'setPremiered',
}


def set_info(li, info):
    tag = li.getVideoInfoTag() if hasattr(li, 'getVideoInfoTag') else None
    if tag is not None and hasattr(tag, 'setTitle'):  # Kodi 20+
        for key, value in info.items():
            setter = _INFO_SETTERS.get(key)
            if setter is None or value in (None, ''):
                continue
            if key == 'genre' and not isinstance(value, list):
                value = [value]
            getattr(tag, setter)(value)
    else:
        if isinstance(info.get('genre'), list):
            info = dict(info, genre=' / '.join(info['genre']))
        li.setInfo('video', info)


def make_item(label, art=None, info=None, playable=False):
    li = xbmcgui.ListItem(label=label, offscreen=True)
    if art:
        li.setArt(art)
    if info:
        set_info(li, info)
    if playable:
        li.setProperty('IsPlayable', 'true')
    return li


class Plugin(object):
    def __init__(self, argv):
        self.base = argv[0]
        self.handle = int(argv[1])
        self.params = dict(parse_qsl(argv[2].lstrip('?')))
        self.api = animelib.AnimeLib(ADDON.getSetting('api_url') or animelib.DEFAULT_API,
                                     ADDON.getSetting('site_url') or 'https://anilib.me')
        self._state = None

    @property
    def state(self):
        if self._state is None:
            self._state = State()
        return self._state

    def url(self, **kwargs):
        return '%s?%s' % (self.base, urlencode({k: v for k, v in kwargs.items() if v not in (None, '')}))

    def add_dir(self, label, url, art=None, info=None, context=None):
        li = make_item(label, art or {'icon': 'DefaultFolder.png'}, info)
        if context:
            li.addContextMenuItems(context)
        xbmcplugin.addDirectoryItem(self.handle, url, li, isFolder=True)

    def end(self, content=None, cache=True):
        if content:
            xbmcplugin.setContent(self.handle, content)
        xbmcplugin.endOfDirectory(self.handle, cacheToDisc=cache)

    # --- routing ---------------------------------------------------------------
    def run(self):
        action = self.params.get('action', 'root')
        handler = getattr(self, 'do_' + action, None)
        if handler is None:
            log('Unknown action: %s' % action, xbmc.LOGWARNING)
            return
        try:
            handler()
        except (http.HttpError, kodik.KodikError, ValueError) as e:
            log('Error in %s: %s' % (action, e), xbmc.LOGERROR)
            notify('%s: %s' % (L(30012), e), error=True)
            if action == 'play':
                xbmcplugin.setResolvedUrl(self.handle, False, xbmcgui.ListItem())
            elif action not in ('new_search', 'forget_search', 'clear_search'):
                xbmcplugin.endOfDirectory(self.handle, succeeded=False)

    # --- menus -------------------------------------------------------------------
    def do_root(self):
        self.add_dir(L(30000), self.url(action='search'), {'icon': 'DefaultAddonsSearch.png'})
        for sort_by, string_id in (('last_episode_at', 30002), ('views', 30003),
                                   ('rate_avg', 30004), ('created_at', 30005)):
            self.add_dir(L(string_id), self.url(action='catalog', sort_by=sort_by))
        self.add_dir(L(30006), self.url(action='genres'))
        self.add_dir(L(30007), self.url(action='types'))
        self.end()

    def do_genres(self):
        for g in self.api.genres():
            if not setting_bool('show_adult') and g.get('adult'):
                continue
            self.add_dir(g['name'], self.url(action='catalog', genre=g['id'], sort_by='views'))
        self.end()

    def do_types(self):
        for t in self.api.types():
            self.add_dir(t['label'], self.url(action='catalog', type=t['id'], sort_by='views'))
        self.end()

    def do_search(self):
        li = make_item('[B]%s[/B]' % L(30001), {'icon': 'DefaultAddonsSearch.png'})
        xbmcplugin.addDirectoryItem(self.handle, self.url(action='new_search'), li, isFolder=False)
        for query in self.state.data['search']:
            self.add_dir(query, self.url(action='catalog', q=query), {'icon': 'DefaultAddonsSearch.png'},
                         context=[(L(30020), 'RunPlugin(%s)' % self.url(action='forget_search', q=query)),
                                  (L(30015), 'RunPlugin(%s)' % self.url(action='clear_search'))])
        self.end(cache=False)

    def do_new_search(self):
        query = xbmcgui.Dialog().input(L(30016), type=xbmcgui.INPUT_ALPHANUM).strip()
        if not query:
            return
        history = [q for q in self.state.data['search'] if q.lower() != query.lower()]
        self.state.data['search'] = ([query] + history)[:SEARCH_HISTORY_SIZE]
        self.state.save()
        xbmc.executebuiltin('Container.Update(%s)' % self.url(action='catalog', q=query))

    def do_forget_search(self):
        query = self.params.get('q')
        self.state.data['search'] = [q for q in self.state.data['search'] if q != query]
        self.state.save()
        xbmc.executebuiltin('Container.Refresh')

    def do_clear_search(self):
        self.state.data['search'] = []
        self.state.save()
        xbmc.executebuiltin('Container.Refresh')

    # --- catalog -----------------------------------------------------------------
    def _title(self, item):
        return animelib.title_of(item, 'eng' if setting_int('title_lang') == 1 else 'rus')

    def do_catalog(self):
        page = int(self.params.get('page', 1))
        query = self.params.get('q')
        sort_by = self.params.get('sort_by')
        genre = self.params.get('genre')
        type_id = self.params.get('type')
        items, has_next = self.api.catalog(page=page, query=query, sort_by=sort_by,
                                           genres=[genre] if genre else None,
                                           types=[type_id] if type_id else None)
        show_adult = setting_bool('show_adult')
        count = 0
        for item in items:
            if not show_adult and animelib.is_adult(item):
                continue
            count += 1
            cover = item.get('cover') or {}
            title = self._title(item)
            plot = ' · '.join(x for x in (
                (item.get('type') or {}).get('label'),
                (item.get('status') or {}).get('label'),
                item.get('releaseDateString'),
                (item.get('ageRestriction') or {}).get('label'),
            ) if x)
            info = {
                'title': title,
                'originaltitle': item.get('name'),
                'plot': plot,
                'rating': animelib.rating_of(item),
                'mediatype': 'tvshow',
            }
            year = (item.get('releaseDate') or '')[:4]
            if year.isdigit():
                info['year'] = int(year)
            art = {'thumb': cover.get('default') or cover.get('thumbnail'),
                   'poster': cover.get('default') or cover.get('thumbnail'),
                   'icon': cover.get('thumbnail')}
            self.add_dir(title, self.url(action='anime', slug=item['slug_url']), art, info)
        if has_next:
            self.add_dir('[COLOR yellow]%s (%d) >>[/COLOR]' % (L(30008), page + 1),
                         self.url(action='catalog', q=query, sort_by=sort_by, genre=genre,
                                  type=type_id, page=page + 1),
                         {'icon': 'DefaultFolder.png'})
        if not count and page == 1:
            notify(L(30017))
        self.end('tvshows')

    def do_anime(self):
        slug = self.params['slug']
        anime = self.api.anime(slug)
        episodes = self.api.episodes(slug)
        if not episodes:
            notify(L(30019))
        title = self._title(anime)
        cover = anime.get('cover') or {}
        poster = cover.get('default') or cover.get('thumbnail')
        fanart = (anime.get('background') or {}).get('url') or poster
        plot = animelib.summary_text(anime.get('summary'))
        genres = [g.get('name') for g in anime.get('genres') or [] if g.get('name')]
        seasons = {str(e.get('season') or '1') for e in episodes}
        multi_season = len(seasons) > 1
        year = (anime.get('releaseDate') or '')[:4]
        for ep in episodes:
            number = ep.get('number') or str(ep.get('item_number') or '')
            season = str(ep.get('season') or '1')
            label = '%s %s' % (L(30013), number)
            if multi_season:
                label = '%s %s · %s' % (L(30018), season, label)
            if ep.get('name'):
                label += '. ' + ep['name']
            info = {
                'title': label,
                'tvshowtitle': title,
                'plot': plot,
                'genre': genres,
                'rating': animelib.rating_of(anime),
                'mediatype': 'episode',
            }
            try:
                info['episode'] = int(float(number))
                info['season'] = int(season)
            except ValueError:
                pass
            if year.isdigit():
                info['year'] = int(year)
            li = make_item(label, {'thumb': poster, 'poster': poster, 'fanart': fanart,
                                   'icon': poster}, info, playable=True)
            li.addContextMenuItems([(L(30014), 'PlayMedia(%s)' % self.url(
                action='play', id=ep['id'], slug=slug, select=1))])
            xbmcplugin.addDirectoryItem(self.handle, self.url(action='play', id=ep['id'], slug=slug),
                                        li, isFolder=False)
        xbmcplugin.addSortMethod(self.handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.addSortMethod(self.handle, xbmcplugin.SORT_METHOD_EPISODE)
        self.end('episodes')

    # --- playback ----------------------------------------------------------------
    @staticmethod
    def _team_key(player):
        return {
            'team': (player.get('team') or {}).get('id'),
            'type': (player.get('translation_type') or {}).get('id'),
            'player': player.get('player'),
        }

    def _remembered(self, slug, candidates):
        saved = self.state.data['teams'].get(slug)
        if not saved:
            return None
        for fields in (('team', 'type', 'player'), ('team', 'type'), ('team',)):
            for p in candidates:
                key = self._team_key(p)
                if all(key[f] == saved.get(f) for f in fields):
                    return p
        return None

    def _choose_player(self, slug, candidates, force_select):
        remember = setting_bool('remember_team')
        if not force_select and remember:
            player = self._remembered(slug, candidates)
            if player:
                return player

        preferred_type = {1: players.TRANSLATION_VOICE, 2: players.TRANSLATION_SUBS}.get(
            setting_int('translation_type', 1))

        def sort_key(p):
            ttype = (p.get('translation_type') or {}).get('id')
            return (0 if preferred_type is None or ttype == preferred_type else 1,
                    -(p.get('views') or 0))

        candidates = sorted(candidates, key=sort_key)
        if len(candidates) == 1 and not force_select:
            player = candidates[0]
        else:
            idx = xbmcgui.Dialog().select(L(30009), [players.describe(p) for p in candidates])
            if idx < 0:
                return None
            player = candidates[idx]
        if remember:
            self.state.data['teams'][slug] = self._team_key(player)
            self.state.save()
        return player

    def do_play(self):
        episode = self.api.episode(self.params['id'])
        slug = self.params.get('slug') or str(episode.get('anime_id'))
        candidates = [p for p in episode.get('players') or [] if players.is_supported(p)]
        if not candidates:
            notify(L(30010), error=True)
            xbmcplugin.setResolvedUrl(self.handle, False, xbmcgui.ListItem())
            return
        player = self._choose_player(slug, candidates, self.params.get('select') == '1')
        if player is None:
            xbmcplugin.setResolvedUrl(self.handle, False, xbmcgui.ListItem())
            return

        servers = None
        if (player.get('player') or '').lower() == 'animelib':
            servers = self.api.video_servers()
        site = (ADDON.getSetting('site_url') or 'https://anilib.me').rstrip('/') + '/'
        stream = players.resolve(
            player,
            max_quality=QUALITIES[setting_int('max_quality')] if setting_int('max_quality') < len(QUALITIES) else 0,
            video_servers=servers,
            server_id=VIDEO_SERVERS[min(setting_int('video_server'), len(VIDEO_SERVERS) - 1)],
            referer=site)
        log('Playing %s (%sp) via %s' % (players.describe(player), stream.quality, player.get('player')))

        headers = dict(stream.headers, **{'User-Agent': http.USER_AGENT})
        li = xbmcgui.ListItem(path='%s|%s' % (stream.url, urlencode(headers)), offscreen=True)
        if '.m3u8' in stream.url:
            li.setMimeType('application/vnd.apple.mpegurl')
        else:
            li.setMimeType('video/mp4')
        li.setContentLookup(False)
        if stream.subtitles:
            li.setSubtitles(stream.subtitles)
        xbmcplugin.setResolvedUrl(self.handle, True, li)


def run():
    Plugin(sys.argv).run()
