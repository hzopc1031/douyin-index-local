"""Narrow workaround for pywebview 6.2.1's duplicate Qt persistent profiles."""
import importlib
import logging
from importlib.metadata import version

log = logging.getLogger(__name__)


def install_shared_profile(storage_path, *, _test_cookie_host=None):
    """Return False on an unsupported backend/version: caller must use private mode.

    Both windows share one QObject, retained for the process lifetime. No Qt
    object is constructed here; the factory runs on Qt's GUI thread as before.
    """
    try:
        if version('pywebview') != '6.2.1':
            raise RuntimeError('unsupported pywebview version')
        qt = importlib.import_module('webview.platforms.qt')
        original = qt.QWebEngineProfile
        if not callable(original):
            raise RuntimeError('Qt WebEngine profile unavailable')
        if getattr(original, '_index_hub_shared', False):
            return True
        policy = getattr(getattr(original, 'PersistentCookiesPolicy', original),
                         'ForcePersistentCookies', None)
        if policy is None or not callable(getattr(original, 'setPersistentCookiesPolicy', None)):
            raise RuntimeError('Qt session-cookie persistence unavailable')
        from webview_cookies import CookieMirror, CookieSignals, cookie_domains
        cookie_domains(_test_cookie_host=_test_cookie_host)
        original_get_cookies = qt.BrowserView.get_cookies
        original_load_finished = qt.BrowserView.on_load_finished
        # qt.py snapshots storage_path at import, before webview.start sets it.
        qt._profile_storage_path = str(storage_path)

        class SharedProfile:
            _index_hub_shared = True
            _persistent = None

            def __new__(cls, *args, **kwargs):
                if args and args[0] == 'pywebview':
                    if cls._persistent is None:
                        mirror = CookieMirror(_test_cookie_host=_test_cookie_host)
                        # Read once while holding the profile lock and before
                        # Qt opens it. Subsequent changes/removals are signals,
                        # never a reread of potentially stale on-disk cookies.
                        mirror.restore(storage_path)
                        profile = original(*args, **kwargs)
                        profile.setPersistentStoragePath(str(storage_path))
                        profile.setCachePath(str(storage_path) + '/cache')
                        # Session cookies have no Expires/Max-Age; the Qt default
                        # only restores persistent cookies after a clean restart.
                        profile.setPersistentCookiesPolicy(policy)
                        # Connect before any QWebEnginePage can initialize the
                        # store. pywebview's per-window mirror attaches too late
                        # and does not explicitly enumerate restored cookies.
                        profile._index_hub_cookie_mirror = mirror
                        store = profile.cookieStore()
                        signals = CookieSignals(mirror, lambda callback: qt.QtCore.QTimer.singleShot(0, callback),
                                                qt612=qt.QtCore.qVersion() == '6.12.0')
                        profile._index_hub_cookie_signals = signals
                        store.cookieAdded.connect(signals.added)
                        store.cookieRemoved.connect(signals.removed)
                        qt.QtCore.QTimer.singleShot(0, store.loadAllCookies)
                        cls._persistent = profile
                    return cls._persistent
                return original(*args, **kwargs)

        # Preserve these exact signatures: loadFinished emits a bool, which Qt
        # discards for the original zero-argument method (apart from self).
        def on_load_finished(self):
            mirror = getattr(self.profile, '_index_hub_cookie_mirror', None)
            if mirror is not None and not getattr(self, '_index_hub_cookies_loaded', False):
                self._index_hub_cookies_loaded = True
                self.profile.cookieStore().loadAllCookies()
            return original_load_finished(self)

        def get_cookies(self):
            mirror = getattr(self.profile, '_index_hub_cookie_mirror', None)
            if mirror is not None:
                return mirror.snapshot()
            return original_get_cookies(self)

        qt.BrowserView.on_load_finished = on_load_finished
        qt.BrowserView.get_cookies = get_cookies
        qt.QWebEngineProfile = SharedProfile
        return True
    except Exception:
        log.warning('Persistent Qt profile workaround unavailable; falling back to private mode')
        return False
