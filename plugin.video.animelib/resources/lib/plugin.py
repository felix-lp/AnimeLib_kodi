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

from . import animelib, auth, http, kodik, players

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo('id')
ICON = ADDON.getAddonInfo('icon')
QUALITIES = [0, 1080, 720, 480, 360]
VIDEO_SERVERS = [None, 'main', 'secondary_1', 'secondary_2']  # None = as the site (player.video_domain)
WATCHED_PERCENT = 85
SEARCH_HISTORY_SIZE = 20
NON_DIRECTORY_ACTIONS = ('new_search', 'forget_search', 'clear_search', 'login', 'logout',
                         'bookmark_add', 'bookmark_remove')


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


# --- persistent state ------------------------------------------------------------
class JsonStore(object):
    def __init__(self, filename, defaults=None):
        folder = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
        if not xbmcvfs.exists(folder):
            xbmcvfs.mkdirs(folder)
        self.path = os.path.join(folder, filename)
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                self.data = json.load(f)
        except (IOError, OSError, ValueError):
            self.data = {}
        for key, value in (defaults or {}).items():
            self.data.setdefault(key, value)

    def save(self):
        try:
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False)
        except (IOError, OSError) as e:
            log('Cannot save %s: %s' % (self.path, e), xbmc.LOGWARNING)


class State(JsonStore):
    """Search history and chosen translations."""
    def __init__(self):
        JsonStore.__init__(self, 'state.json', {'search': [], 'teams': {}})


class Account(JsonStore):
    """AnimeLib account: OAuth tokens, user info and credentials for silent re-login."""
    def __init__(self, site_url, api_url):
        JsonStore.__init__(self, 'account.json')
        self.site_url = site_url
        self.api_url = api_url

    @property
    def logged_in(self):
        return bool(self.data.get('token'))

    @property
    def user(self):
        return self.data.get('user') or {}

    def login(self, username, password):
        self.data['token'] = auth.login(username, password, self.site_url, self.api_url)
        self.data['login'] = username
        self.data['password'] = password
        self.save()

    def logout(self):
        self.data = {}
        self.save()

    def access_token(self, force_refresh=False):
        token = self.data.get('token')
        if not token:
            return None
        if force_refresh or auth.is_expired(token):
            try:
                token = auth.refresh(token, self.site_url, self.api_url)
            except http.HttpError as e:
                log('Token refresh failed (network): %s' % e, xbmc.LOGWARNING)
                return token.get('access_token')
            except auth.AuthError as e:
                log('Token refresh failed: %s' % e, xbmc.LOGWARNING)
                token = self._relogin()
                if token is None:
                    return None
            self.data['token'] = token
            self.save()
        return token.get('access_token')

    def _relogin(self):
        if self.data.get('login') and self.data.get('password'):
            try:
                return auth.login(self.data['login'], self.data['password'],
                                  self.site_url, self.api_url)
            except (auth.AuthError, http.HttpError) as e:
                log('Re-login failed: %s' % e, xbmc.LOGWARNING)
        self.data.pop('token', None)
        self.save()
        notify(L(30042), error=True)
        return None


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
    'playcount': 'setPlaycount',
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


def image_url(url):
    """The cover CDN answers 403 without a Referer; Kodi passes headers after '|'."""
    if not url or not url.startswith('http') or '|' in url:
        return url
    site = (ADDON.getSetting('site_url') or 'https://anilib.me').rstrip('/') + '/'
    return '%s|%s' % (url, urlencode({'Referer': site, 'User-Agent': http.USER_AGENT}))


def make_item(label, art=None, info=None, playable=False):
    li = xbmcgui.ListItem(label=label, offscreen=True)
    if art:
        li.setArt({k: image_url(v) for k, v in art.items() if v})
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
        api_url = ADDON.getSetting('api_url') or animelib.DEFAULT_API
        site_url = ADDON.getSetting('site_url') or 'https://anilib.me'
        self.account = Account(site_url, api_url)
        self.api = animelib.AnimeLib(api_url, site_url, token_provider=self.account.access_token)
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
        except (http.HttpError, kodik.KodikError, auth.AuthError, ValueError) as e:
            log('Error in %s: %s' % (action, e), xbmc.LOGERROR)
            notify('%s: %s' % (L(30012), e), error=True)
            if action == 'play':
                xbmcplugin.setResolvedUrl(self.handle, False, xbmcgui.ListItem())
            elif action not in NON_DIRECTORY_ACTIONS:
                xbmcplugin.endOfDirectory(self.handle, succeeded=False)

    # --- menus -------------------------------------------------------------------
    def do_root(self):
        if self.account.logged_in:
            name = self.account.user.get('username')
            self.add_dir('%s%s' % (L(30024), ' (%s)' % name if name else ''), self.url(action='folders'),
                         {'icon': 'DefaultPlaylist.png'})
        else:
            li = make_item(L(30025), {'icon': 'DefaultUser.png'})
            xbmcplugin.addDirectoryItem(self.handle, self.url(action='login'), li, isFolder=False)
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

    # --- account -----------------------------------------------------------------
    def do_login(self):
        username = xbmcgui.Dialog().input(L(30026), defaultt=self.account.data.get('login', ''),
                                          type=xbmcgui.INPUT_ALPHANUM).strip()
        if not username:
            return
        password = xbmcgui.Dialog().input(L(30027), type=xbmcgui.INPUT_ALPHANUM,
                                          option=xbmcgui.ALPHANUM_HIDE_INPUT)
        if not password:
            return
        xbmc.executebuiltin('ActivateWindow(busydialognocancel)')
        try:
            self.account.login(username, password)
            me = self.api.me()
            self.account.data['user'] = {'id': me.get('id'), 'username': me.get('username')}
            self.account.save()
        except (auth.AuthError, http.HttpError) as e:
            self.account.logout()
            xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
            xbmcgui.Dialog().ok(L(30029), str(e))
            return
        xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
        notify(L(30028).replace('%s', self.account.user.get('username') or ''))
        xbmc.executebuiltin('Container.Refresh')

    def do_logout(self):
        if not self.account.logged_in:
            return
        self.account.logout()
        notify(L(30030))
        xbmc.executebuiltin('Container.Refresh')

    def _require_login(self):
        if self.account.logged_in and self.account.user.get('id'):
            return True
        notify(L(30036), error=True)
        return False

    def do_folders(self):
        if not self._require_login():
            return xbmcplugin.endOfDirectory(self.handle, succeeded=False)
        for folder in self.api.bookmark_folders(self.account.user['id']):
            label = '%s [COLOR grey](%d)[/COLOR]' % (folder.get('name'), folder.get('count') or 0)
            self.add_dir(label, self.url(action='bookmarks', folder=folder['id']),
                         {'icon': 'DefaultPlaylist.png'})
        li = make_item('[COLOR grey]%s[/COLOR]' % L(30035), {'icon': 'DefaultUser.png'})
        xbmcplugin.addDirectoryItem(self.handle, self.url(action='logout'), li, isFolder=False)
        self.end(cache=False)

    def do_bookmarks(self):
        if not self._require_login():
            return xbmcplugin.endOfDirectory(self.handle, succeeded=False)
        page = int(self.params.get('page', 1))
        folder = self.params['folder']
        items, has_next = self.api.bookmarks(self.account.user['id'], folder, page)
        for bm in items:
            media = bm.get('media')
            if media:
                self.add_anime(media, in_folder=True)
        if has_next:
            self.add_dir('[COLOR yellow]%s (%d) >>[/COLOR]' % (L(30008), page + 1),
                         self.url(action='bookmarks', folder=folder, page=page + 1))
        self.end('tvshows', cache=False)

    def do_bookmark_add(self):
        if not self._require_login():
            return
        slug = self.params['slug']
        folders = self.api.bookmark_folders(self.account.user['id'])
        idx = xbmcgui.Dialog().select(L(30031), [f.get('name') for f in folders])
        if idx < 0:
            return
        current = self.api.media_bookmark(slug) or {}
        self.api.set_bookmark(slug, folders[idx]['id'], item_id=current.get('item_id'))
        notify(L(30033).replace('%s', folders[idx].get('name') or ''))
        xbmc.executebuiltin('Container.Refresh')

    def do_bookmark_remove(self):
        if not self._require_login():
            return
        current = self.api.media_bookmark(self.params['slug'])
        if current and current.get('id'):
            self.api.delete_bookmark(current['id'])
        notify(L(30034))
        xbmc.executebuiltin('Container.Refresh')

    # --- catalog -----------------------------------------------------------------
    def _title(self, item):
        return animelib.title_of(item, 'eng' if setting_int('title_lang') == 1 else 'rus')

    def add_anime(self, item, in_folder=False):
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
        context = []
        if self.account.logged_in:
            slug = item['slug_url']
            context.append((L(30031), 'RunPlugin(%s)' % self.url(action='bookmark_add', slug=slug)))
            context.append((L(30032), 'RunPlugin(%s)' % self.url(action='bookmark_remove', slug=slug)))
        self.add_dir(title, self.url(action='anime', slug=item['slug_url']), art, info, context)

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
            self.add_anime(item)
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
        last_index = -1
        if self.account.logged_in:
            try:
                bookmark = self.api.media_bookmark(slug) or {}
            except http.HttpError as e:
                log('Cannot get bookmark: %s' % e, xbmc.LOGWARNING)
                bookmark = {}
            item_id = bookmark.get('item_id')
            item_number = (bookmark.get('meta') or {}).get('item_number')
            for i, e in enumerate(episodes):
                if (item_id and e.get('id') == item_id) or (
                        not item_id and item_number and e.get('item_number') == item_number):
                    last_index = i
                    break

        title = self._title(anime)
        cover = anime.get('cover') or {}
        poster = cover.get('default') or cover.get('thumbnail')
        fanart = (anime.get('background') or {}).get('url') or poster
        plot = animelib.summary_text(anime.get('summary'))
        genres = [g.get('name') for g in anime.get('genres') or [] if g.get('name')]
        seasons = {str(e.get('season') or '1') for e in episodes}
        multi_season = len(seasons) > 1
        year = (anime.get('releaseDate') or '')[:4]
        for index, ep in enumerate(episodes):
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
            if index <= last_index:
                info['playcount'] = 1
            elif index == last_index + 1 and last_index >= 0:
                label = '[COLOR yellow]► %s[/COLOR]' % label
            try:
                info['episode'] = int(float(number))
                info['season'] = int(season)
            except ValueError:
                pass
            if year.isdigit():
                info['year'] = int(year)
            li = make_item(label, {'thumb': poster, 'poster': poster, 'fanart': fanart,
                                   'icon': poster}, info, playable=True)
            play_args = dict(action='play', id=ep['id'], slug=slug, n=ep.get('item_number') or index + 1)
            li.addContextMenuItems([(L(30014), 'PlayMedia(%s)' % self.url(select=1, **play_args))])
            xbmcplugin.addDirectoryItem(self.handle, self.url(**play_args), li, isFolder=False)
        xbmcplugin.addSortMethod(self.handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.addSortMethod(self.handle, xbmcplugin.SORT_METHOD_EPISODE)
        self.end('episodes', cache=not self.account.logged_in)

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
        for fields in (('team', 'type'), ('team',)):
            matches = [p for p in candidates
                       if all(self._team_key(p)[f] == saved.get(f) for f in fields)]
            if matches:
                # The AnimeLib player (up to 1080p) wins over the same team on Kodik.
                matches.sort(key=lambda p: (not players.is_native(p),
                                            p.get('player') != saved.get('player')))
                return matches[0]
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
                    not players.is_native(p),
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
        if players.is_native(player):
            servers = self.api.video_servers()
        site = (ADDON.getSetting('site_url') or 'https://anilib.me').rstrip('/') + '/'
        stream = players.resolve(
            player,
            max_quality=QUALITIES[setting_int('max_quality')] if setting_int('max_quality') < len(QUALITIES) else 0,
            video_servers=servers,
            server_id=VIDEO_SERVERS[min(setting_int('video_server'), len(VIDEO_SERVERS) - 1)]
            or player.get('video_domain') or 'main',
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
        self._track_playback(slug, episode, player)

    def _track_playback(self, slug, episode, player):
        """Keeps running while the episode plays: skips the opening and, once
        WATCHED_PERCENT is reached, marks the episode as watched on the site."""
        mark = self.account.logged_in and setting_bool('mark_watched')
        opening = None
        if setting_bool('skip_opening'):
            opening = next((t for t in player.get('timecode') or [] if t.get('type') == 'opening'), None)
        if not mark and not opening:
            return
        monitor = xbmc.Monitor()
        kodi_player = xbmc.Player()
        for _ in range(120):
            if kodi_player.isPlayingVideo():
                break
            if monitor.waitForAbort(0.5):
                return
        else:
            return
        try:
            playing_file = kodi_player.getPlayingFile()
        except RuntimeError:
            return
        watched = False
        while not monitor.abortRequested():
            try:
                if not kodi_player.isPlayingVideo() or kodi_player.getPlayingFile() != playing_file:
                    break
                position = kodi_player.getTime()
                total = kodi_player.getTotalTime()
            except RuntimeError:
                break
            if opening and opening.get('from', 0) <= position < (opening.get('to') or 0) - 1:
                kodi_player.seekTime(float(opening['to']))
                opening = None
            if mark and not watched and total > 0 and position * 100.0 / total >= WATCHED_PERCENT:
                watched = True
                self._mark_watched(slug, episode, player)
            if monitor.waitForAbort(1):
                break

    def _mark_watched(self, slug, episode, player):
        """Like the site: mark the player as viewed and move the list bookmark to this episode
        (only for titles that are already in one of the user's lists)."""
        try:
            self.api.mark_viewed(episode.get('anime_id'), player['id'])
            current = self.api.media_bookmark(slug)
            if current:
                n = self.params.get('n')
                self.api.set_bookmark(slug, current.get('status') or animelib.ANIME_FOLDER_WATCHING,
                                      item_id=episode['id'], item_number=int(n) if n else None)
            log('Marked as watched: %s #%s' % (slug, self.params.get('n')))
        except (http.HttpError, auth.AuthError, ValueError, KeyError) as e:
            log('Cannot mark as watched: %s' % e, xbmc.LOGWARNING)


def run():
    Plugin(sys.argv).run()
