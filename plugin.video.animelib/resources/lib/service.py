# -*- coding: utf-8 -*-
"""Background service: follows playback started by the plugin.

The plugin leaves a description of the started episode in a window property;
this service picks it up on playback start, skips the opening (if enabled)
and marks the episode as watched on the site at WATCHED_PERCENT.
Player state is only queried while Kodi reports the video as playing.
"""
import json
import time

import xbmc
import xbmcgui

from . import library, plugin

PENDING_TTL = 120  # seconds between resolving the URL and playback start


class Tracker(xbmc.Player):
    def __init__(self):
        xbmc.Player.__init__(self)
        self.current = None
        self.watched = False

    def onAVStarted(self):
        self.current = None
        window = xbmcgui.Window(10000)
        raw = window.getProperty(plugin.PLAYBACK_PROPERTY)
        if not raw:
            return
        window.clearProperty(plugin.PLAYBACK_PROPERTY)
        try:
            data = json.loads(raw)
        except ValueError:
            return
        if time.time() - data.get('created', 0) > PENDING_TTL:
            return
        self.current = data
        self.watched = False

    def _stop(self):
        self.current = None

    onPlayBackStopped = _stop
    onPlayBackEnded = _stop
    onPlayBackError = _stop

    def tick(self):
        data = self.current
        if not data or not self.isPlayingVideo():
            return
        try:
            position = self.getTime()
            total = self.getTotalTime()
        except RuntimeError:
            return
        if self.current is not data:  # stopped while we were reading
            return
        opening = data.get('opening')
        if opening and opening.get('from', 0) <= position < (opening.get('to') or 0) - 1:
            data['opening'] = None
            self.seekTime(float(opening['to']))
        if (data.get('mark') and not self.watched and total > 0
                and position * 100.0 / total >= plugin.WATCHED_PERCENT):
            self.watched = True
            plugin.mark_watched(data)


LIBRARY_FIRST_DELAY = 120  # seconds after Kodi start


def update_library(force=False):
    """Adds new episodes of library titles every N hours (setting)."""
    hours = plugin.LIBRARY_UPDATE_HOURS[min(plugin.setting_int('library_update_hours', 2),
                                            len(plugin.LIBRARY_UPDATE_HOURS) - 1)]
    lib = plugin.make_library()
    if not lib.titles or (not hours and not force):
        return
    last = lib.store.data.get('last_update', 0)
    if not force and time.time() - last < hours * 3600:
        return
    lib.store.data['last_update'] = time.time()
    lib.store.save()
    _, api = plugin.make_api()
    added = lib.update_all(api, plugin.log)
    plugin.log('Library update: %d new episodes' % added)
    if added:
        xbmc.executebuiltin('UpdateLibrary(video)')


def run():
    try:
        if library.remove_anime_node():
            plugin.log('Removed the "Anime" library section added by 1.6.0')
    except Exception as e:
        plugin.log('Cannot remove the anime library section: %s' % e, xbmc.LOGWARNING)
    monitor = xbmc.Monitor()
    tracker = Tracker()
    started = time.time()
    next_library_check = started + LIBRARY_FIRST_DELAY
    while not monitor.waitForAbort(1):
        tracker.tick()
        if time.time() >= next_library_check and not tracker.isPlayingVideo():
            next_library_check = time.time() + 600
            try:
                update_library()
            except Exception as e:  # never let the service die
                plugin.log('Library update error: %s' % e, xbmc.LOGWARNING)
    del tracker
