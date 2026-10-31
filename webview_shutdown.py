"""Bounded SIGTERM shutdown for the Hub's persistent Qt browser."""
import os
import signal
import sys
import threading


class QtShutdown:
    """Install on the main thread before webview.start; finish on that thread.

    pywebview's Qt timer delivers pending Python signals every 500 ms. Queue
    quit on the GUI thread, then destroy pages before the profile to flush its
    cookie database. A daemon timer bounds a stuck quit/flush to five seconds.
    """

    def __init__(self, release, *, exit_fn=os._exit, timer_factory=threading.Timer):
        self.release = release
        self.exit_fn = exit_fn
        self.timer_factory = timer_factory
        self.timer = None
        self.requested = False
        self.finished = threading.Event()
        self.previous = signal.signal(signal.SIGTERM, self._request)

    def _force_exit(self):
        try:
            self.release()
        finally:
            self.exit_fn(128 + signal.SIGTERM)

    def _request(self, signum, frame):
        if self.requested:
            return
        self.requested = True
        self.timer = self.timer_factory(5, self._force_exit)
        self.timer.daemon = True
        self.timer.start()
        qt = sys.modules.get('webview.platforms.qt')
        app = getattr(qt, '_app', None)
        if app is not None:
            qt.QtCore.QMetaObject.invokeMethod(app, 'quit', qt.QtCore.Qt.QueuedConnection)

    def finish(self):
        try:
            qt = sys.modules.get('webview.platforms.qt')
            factory = getattr(qt, 'QWebEngineProfile', None)
            profile = getattr(factory, '_persistent', None)
            if profile is not None:
                # quit() leaves windows alive; QWebEngineProfile must outlive
                # every QWebEnginePage that uses it, including both Hub windows.
                for browser in list(qt.BrowserView.instances.values()):
                    browser.webview.page().deleteLater()
                deferred = qt.QtCore.QEvent.DeferredDelete
                qt.QtCore.QCoreApplication.sendPostedEvents(None, deferred)
                profile.deleteLater()
                qt.QtCore.QCoreApplication.sendPostedEvents(None, deferred)
                factory._persistent = None
        finally:
            self.release()
        self.finished.set()
        # Allow Python/Qt's normal teardown to drain Chromium's pending writes.
        # Keep the deadline armed: an in-flight worker must not hold up systemd.
        if not self.requested:
            signal.signal(signal.SIGTERM, self.previous)
