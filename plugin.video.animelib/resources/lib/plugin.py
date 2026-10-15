# -*- coding: utf-8 -*-
"""Kodi routing and UI for the AnimeLib add-on."""
import json
import os
import sys
import time
from urllib.parse import parse_qsl, urlencode

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from . import animelib, auth, http, kodik, library, players

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo('id')
ICON = ADDON.getAddonInfo('icon')
QUALITIES = [0, 1080, 720, 480, 360]
VIDEO_SERVERS = [None, 'main', 'secondary_1', 'secondary_2']  # None = as the site (player.video_domain)
WATCHED_PERCENT = 85
PLAYBACK_PROPERTY = 'plugin.video.animelib.playback'
META_TTL = 14 * 24 * 3600
META_CACHE_SIZE = 3000
META_CONCURRENCY = 8
MISSING_TTL = 24 * 3600
BOOKMARKS_TTL = 600
LIBRARY_DEFAULT = 'special://profile/addon_data/plugin.video.animelib/library/'
LIBRARY_UPDATE_HOURS = [0, 6, 12, 24]
SEARCH_HISTORY_SIZE = 20
NON_DIRECTORY_ACTIONS = ('new_search', 'forget_search', 'clear_search', 'login', 'logout',
                         'bookmark_add', 'bookmark_remove', 'library_add', 'library_remove',
                         'library_add_folder', 'library_update', 'library_help')


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


class MetaCache(JsonStore):
    """Title descriptions for list views: the catalog API does not return them,
    so they are fetched per title once and kept for META_TTL."""
    def __init__(self):
        JsonStore.__init__(self, 'meta.json', {'items': {}})
        self.dirty = False

    def get(self, slug):
        entry = self.data['items'].get(slug)
        if entry and time.time() - entry.get('ts', 0) < META_TTL:
            return entry
        return None

    def put_missing(self, slug):
        """Title details not available (404): don't ask again for a day."""
        self.data['items'][slug] = {'ts': time.time() - META_TTL + MISSING_TTL, 'missing': True}
        self.dirty = True

    def put(self, slug, anime):
        self.data['items'][slug] = {
            'ts': time.time(),
            'plot': animelib.summary_text(anime.get('summary')),
            'genres': [g.get('name') for g in anime.get('genres') or [] if g.get('name')],
            'fanart': (anime.get('background') or {}).get('url'),
            'premiered': anime.get('releaseDate'),
        }
        self.dirty = True
        return self.data['items'][slug]

    def save(self):
        if not self.dirty:
            return
        items = self.data['items']
        if len(items) > META_CACHE_SIZE:
            keep = sorted(items, key=lambda k: items[k].get('ts', 0))[-META_CACHE_SIZE:]
            self.data['items'] = {k: items[k] for k in keep}
        JsonStore.save(self)
        self.dirty = False


class BookmarkMap(JsonStore):
    """slug -> list (folder) id for the logged-in user, used to colour titles.
    The catalog API does not say which list a title is in, so all bookmarks are
    fetched (one request per page of every folder) and kept for BOOKMARKS_TTL."""
    def __init__(self):
        JsonStore.__init__(self, 'bookmarks.json', {'map': {}, 'folders': {}})

    def load(self, api, user_id, force=False):
        fresh = (self.data.get('user') == user_id
                 and time.time() - self.data.get('ts', 0) < BOOKMARKS_TTL)
        if fresh and not force:
            return self
        folders = api.bookmark_folders(user_id)

        mapping, page = {}, 1
        remaining = list(folders)
        while remaining:  # one concurrent round per page number
            calls = [('/bookmarks', {'user_id': user_id, 'status': f['id'], 'page': page,
                                     'sort_by': 'updated_at', 'sort_type': 'desc'}) for f in remaining]
            next_round = []
            for folder, (code, resp) in zip(remaining, api.get_many(calls)):
                if code != 200 or not resp:
                    raise http.HttpError('HTTP %s: bookmarks' % code, code)
                for b in resp.get('data') or []:
                    if b.get('media'):
                        mapping[b['media']['slug_url']] = folder['id']
                if (resp.get('links') or {}).get('next'):
                    next_round.append(folder)
            remaining, page = next_round, page + 1
        self.data = {
            'user': user_id, 'ts': time.time(), 'map': mapping,
            'folders': {str(f['id']): {'name': f.get('name'), 'color': f.get('color')} for f in folders},
        }
        self.save()
        return self

    def status(self, slug):
        """Returns (name, colour 'RRGGBB') of the list containing the title, or None."""
        folder_id = self.data['map'].get(slug)
        folder = self.data['folders'].get(str(folder_id)) if folder_id is not None else None
        if not folder:
            return None
        return folder.get('name'), (folder.get('color') or '#ffffff').lstrip('#')

    def set(self, slug, folder_id):
        if folder_id is None:
            self.data['map'].pop(slug, None)
        else:
            self.data['map'][slug] = folder_id
        self.save()


def colored(text, rrggbb):
    return '[COLOR FF%s]%s[/COLOR]' % (rrggbb.upper(), text)


def make_library():
    root = ADDON.getSetting('library_path') or LIBRARY_DEFAULT
    site = (ADDON.getSetting('site_url') or 'https://anilib.me').rstrip('/') + '/'
    return library.Library(root, JsonStore('library.json'), site)


def library_source_configured(root):
    """Checks via JSON-RPC whether the library folder was added as a video source."""
    try:
        resp = json.loads(xbmc.executeJSONRPC(json.dumps({
            'jsonrpc': '2.0', 'id': 1, 'method': 'Files.GetSources', 'params': {'media': 'video'}})))
        sources = resp.get('result', {}).get('sources') or []
    except (ValueError, AttributeError):
        return True
    target = xbmcvfs.translatePath(root).rstrip('/\\').lower()
    for src in sources:
        path = xbmcvfs.translatePath(src.get('file') or '').rstrip('/\\').lower()
        if path and (target == path or target.startswith(path + '/') or target.startswith(path + '\\')):
            return True
    return False


def make_api():
    addon = xbmcaddon.Addon()  # fresh instance: may run inside the long-lived service
    api_url = addon.getSetting('api_url') or animelib.DEFAULT_API
    site_url = addon.getSetting('site_url') or 'https://anilib.me'
    account = Account(site_url, api_url)
    return account, animelib.AnimeLib(api_url, site_url, token_provider=account.access_token)


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
        self._meta = None

    @property
    def meta(self):
        if self._meta is None:
            self._meta = MetaCache()
        return self._meta

    def load_meta(self, items):
        """Returns {slug: meta} for list items, fetching missing ones in parallel."""
        if not setting_bool('list_details'):
            return {}
        result, missing = {}, []
        for item in items:
            slug = item.get('slug_url')
            if not slug:
                continue
            cached = self.meta.get(slug)
            if cached:
                if not cached.get('missing'):
                    result[slug] = cached
            else:
                missing.append(slug)

        if missing:
            fetched = {}
            responses = self.api.get_many([self.api.anime_call(s) for s in missing],
                                          anonymous=True, concurrency=META_CONCURRENCY)
            hidden = []
            for slug, (code, resp) in zip(missing, responses):
                if code == 200 and resp:
                    fetched[slug] = resp.get('data')
                elif code == 404:
                    hidden.append(slug)  # visible only to logged-in users
                else:
                    log('Cannot load %s: HTTP %s' % (slug, code), xbmc.LOGWARNING)
            if hidden and self.account.logged_in:
                responses = self.api.get_many([self.api.anime_call(s) for s in hidden],
                                              concurrency=META_CONCURRENCY)
                for slug, (code, resp) in zip(hidden, responses):
                    if code == 200 and resp:
                        fetched[slug] = resp.get('data')
                    elif code == 404:
                        self.meta.put_missing(slug)
            elif hidden:
                for slug in hidden:
                    self.meta.put_missing(slug)
            for slug, anime in fetched.items():
                if anime:
                    result[slug] = self.meta.put(slug, anime)
            self.meta.save()
        return result

    def bookmark_map(self):
        """BookmarkMap for colouring, or None when not logged in / disabled / failed."""
        if not (self.account.logged_in and self.account.user.get('id') and setting_bool('color_lists')):
            return None
        try:
            return BookmarkMap().load(self.api, self.account.user['id'])
        except http.HttpError as e:
            log('Cannot load bookmarks: %s' % e, xbmc.LOGWARNING)
            return None

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
            code = getattr(e, 'code', None)
            if code == 404:
                notify(L(30064), error=True)
            elif code == 429:
                notify(L(30065), error=True)
            else:
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
            name = folder.get('name') or ''
            if folder.get('color'):
                name = colored(name, folder['color'].lstrip('#'))
            label = '%s [COLOR grey](%d)[/COLOR]' % (name, folder.get('count') or 0)
            self.add_dir(label, self.url(action='bookmarks', folder=folder['id']),
                         {'icon': 'DefaultPlaylist.png'},
                         context=[(L(30049), 'RunPlugin(%s)' % self.url(action='library_add_folder',
                                                                        folder=folder['id']))])
        li = make_item('[COLOR grey]%s[/COLOR]' % L(30035), {'icon': 'DefaultUser.png'})
        xbmcplugin.addDirectoryItem(self.handle, self.url(action='logout'), li, isFolder=False)
        self.end(cache=False)

    def do_bookmarks(self):
        if not self._require_login():
            return xbmcplugin.endOfDirectory(self.handle, succeeded=False)
        page = int(self.params.get('page', 1))
        folder = self.params['folder']
        items, has_next = self.api.bookmarks(self.account.user['id'], folder, page)
        medias = [bm['media'] for bm in items if bm.get('media')]
        meta = self.load_meta(medias)
        bookmarks, lib = self.bookmark_map(), make_library()
        for media in medias:
            self.add_anime(media, meta.get(media.get('slug_url')), bookmarks, lib)
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
        BookmarkMap().set(slug, folders[idx]['id'])
        notify(L(30033).replace('%s', folders[idx].get('name') or ''))
        xbmc.executebuiltin('Container.Refresh')

    def do_bookmark_remove(self):
        if not self._require_login():
            return
        current = self.api.media_bookmark(self.params['slug'])
        if current and current.get('id'):
            self.api.delete_bookmark(current['id'])
        BookmarkMap().set(self.params['slug'], None)
        notify(L(30034))
        xbmc.executebuiltin('Container.Refresh')

    # --- Kodi library --------------------------------------------------------------
    def _library_title(self, slug):
        return self.params.get('title') or self._title(self.api.anime(slug))

    def _after_library_change(self, lib):
        if not library_source_configured(lib.root):
            self.do_library_help()
        else:
            xbmc.executebuiltin('UpdateLibrary(video)')

    def do_library_add(self):
        slug = self.params['slug']
        lib = make_library()
        xbmc.executebuiltin('ActivateWindow(busydialognocancel)')
        try:
            count = lib.add(self.api, slug, self._library_title(slug))
        finally:
            xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
        notify(L(30050).replace('%d', str(count)))
        self._after_library_change(lib)
        xbmc.executebuiltin('Container.Refresh')

    def do_library_remove(self):
        lib = make_library()
        if lib.remove(self.params['slug']):
            notify(L(30051))
            xbmc.executebuiltin('CleanLibrary(video)')
            xbmc.executebuiltin('Container.Refresh')

    def do_library_add_folder(self):
        if not self._require_login():
            return
        folder, page, medias = self.params['folder'], 1, []
        while True:
            items, has_next = self.api.bookmarks(self.account.user['id'], folder, page)
            medias += [b['media'] for b in items if b.get('media')]
            if not has_next:
                break
            page += 1
        lib = make_library()
        progress = xbmcgui.DialogProgress()
        progress.create('AnimeLib', L(30052))
        added = 0
        try:
            for i, media in enumerate(medias):
                if progress.iscanceled():
                    break
                title = self._title(media)
                progress.update(int(i * 100 / max(1, len(medias))), title)
                try:
                    lib.add(self.api, media['slug_url'], title)
                    added += 1
                except http.HttpError as e:
                    log('Library add failed for %s: %s' % (media.get('slug_url'), e), xbmc.LOGWARNING)
        finally:
            progress.close()
        notify(L(30053).replace('%d', str(added)))
        self._after_library_change(lib)

    def do_library_update(self):
        lib = make_library()
        xbmc.executebuiltin('ActivateWindow(busydialognocancel)')
        try:
            count = lib.update_all(self.api, log)
        finally:
            xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
        notify(L(30050).replace('%d', str(count)))
        if count:
            xbmc.executebuiltin('UpdateLibrary(video)')

    def do_library_help(self):
        root = (ADDON.getSetting('library_path') or LIBRARY_DEFAULT)
        # special:// paths can be typed into "Add videos" even though Browse hides AppData
        xbmcgui.Dialog().textviewer(L(30054), L(30055).replace('%s', root))

    # --- catalog -----------------------------------------------------------------
    def _title(self, item):
        return animelib.title_of(item, 'eng' if setting_int('title_lang') == 1 else 'rus')

    def add_anime(self, item, meta=None, bookmarks=None, lib=None):
        meta = meta or {}
        slug = item['slug_url']
        cover = item.get('cover') or {}
        title = self._title(item)
        summary_line = ' · '.join(x for x in (
            (item.get('type') or {}).get('label'),
            (item.get('status') or {}).get('label'),
            item.get('releaseDateString'),
            (item.get('ageRestriction') or {}).get('label'),
        ) if x)
        label = title
        status = bookmarks.status(slug) if bookmarks else None
        if status:
            label = colored(title, status[1])
            summary_line = colored('● ' + status[0], status[1]) + (' · ' + summary_line if summary_line else '')
        plot = '\n\n'.join(x for x in (summary_line, meta.get('plot')) if x)
        info = {
            'title': title,
            'originaltitle': item.get('name'),
            'plot': plot,
            'rating': animelib.rating_of(item),
            'genre': meta.get('genres'),
            'mediatype': 'tvshow',
        }
        date = item.get('releaseDate') or meta.get('premiered') or ''
        if date[:4].isdigit():
            info['year'] = int(date[:4])
        if len(date) == 10:
            info['premiered'] = date
        poster = cover.get('default') or cover.get('thumbnail')
        art = {'thumb': poster, 'poster': poster, 'icon': cover.get('thumbnail'),
               'fanart': meta.get('fanart')}
        context = []
        if lib is not None and lib.contains(slug):
            context.append((L(30048), 'RunPlugin(%s)' % self.url(action='library_remove', slug=slug)))
        else:
            context.append((L(30047), 'RunPlugin(%s)' % self.url(action='library_add', slug=slug,
                                                                  title=title)))
        if self.account.logged_in:
            context.append((L(30031), 'RunPlugin(%s)' % self.url(action='bookmark_add', slug=slug)))
            context.append((L(30032), 'RunPlugin(%s)' % self.url(action='bookmark_remove', slug=slug)))
        self.add_dir(label, self.url(action='anime', slug=slug), art, info, context)

    def do_catalog(self):
        page = int(self.params.get('page', 1))
        query = self.params.get('q')
        sort_by = self.params.get('sort_by')
        genre = self.params.get('genre')
        type_id = self.params.get('type')
        items, has_next = self.api.catalog(page=page, query=query, sort_by=sort_by,
                                           genres=[genre] if genre else None,
                                           types=[type_id] if type_id else None)
        if not setting_bool('show_adult'):
            items = [i for i in items if not animelib.is_adult(i)]
        meta = self.load_meta(items)
        bookmarks, lib = self.bookmark_map(), make_library()
        for item in items:
            self.add_anime(item, meta.get(item.get('slug_url')), bookmarks, lib)
        count = len(items)
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
        self.meta.put(slug, anime)
        self.meta.save()
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
        self._hand_over_to_service(slug, episode, player, stream)
        xbmcplugin.setResolvedUrl(self.handle, True, li)

    def _hand_over_to_service(self, slug, episode, player, stream):
        """The background service (service.py) tracks playback: opening skip and
        marking the episode as watched. The plugin itself must exit right away."""
        mark = self.account.logged_in and setting_bool('mark_watched')
        opening = None
        if setting_bool('skip_opening'):
            opening = next((t for t in player.get('timecode') or [] if t.get('type') == 'opening'), None)
        if not mark and not opening:
            xbmcgui.Window(10000).clearProperty(PLAYBACK_PROPERTY)
            return
        n = self.params.get('n')
        xbmcgui.Window(10000).setProperty(PLAYBACK_PROPERTY, json.dumps({
            'url': stream.url.split('|')[0],
            'created': time.time(),
            'slug': slug,
            'anime_id': episode.get('anime_id'),
            'episode_id': episode['id'],
            'item_number': int(n) if n and n.isdigit() else None,
            'player_id': player.get('id'),
            'mark': mark,
            'opening': opening,
        }))


def mark_watched(data):
    """Called by the service once WATCHED_PERCENT of the episode is played. Same as the site:
    mark the player as viewed and move the list bookmark to this episode (only for titles
    that are already in one of the user's lists)."""
    account, api = make_api()
    if not account.logged_in:
        return
    try:
        api.mark_viewed(data['anime_id'], data['player_id'])
        current = api.media_bookmark(data['slug'])
        if current:
            api.set_bookmark(data['slug'], current.get('status') or animelib.ANIME_FOLDER_WATCHING,
                             item_id=data['episode_id'], item_number=data.get('item_number'))
        log('Marked as watched: %s #%s' % (data['slug'], data.get('item_number')))
    except (http.HttpError, auth.AuthError, ValueError, KeyError) as e:
        log('Cannot mark as watched: %s' % e, xbmc.LOGWARNING)


def run():
    Plugin(sys.argv).run()
