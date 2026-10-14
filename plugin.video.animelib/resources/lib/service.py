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

from . import plugin

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


def run():
    monitor = xbmc.Monitor()
    tracker = Tracker()
    while not monitor.waitForAbort(1):
        tracker.tick()
    del tracker
