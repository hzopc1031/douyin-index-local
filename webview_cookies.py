"""Read-only startup cookie recovery and a private in-memory Qt cookie mirror."""
from copy import deepcopy
from contextlib import closing
from email.utils import formatdate
from http.cookies import CookieError, SimpleCookie
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time

from browser_login import DOMAINS


def cookie_domains(*, _test_cookie_host=None):
    """The loopback fixture seam is never configured from environment or HTTP."""
    if _test_cookie_host is None:
        return DOMAINS
    if _test_cookie_host != '127.0.0.1':
        raise ValueError('Only the loopback cookie fixture is supported')
    return DOMAINS | frozenset((_test_cookie_host,))


class CookieMirror:
    """Attached before any page initializes the shared profile's cookie store.

    Qt signals run on the GUI thread. Workers receive detached snapshots under
    a lock, never QNetworkCookie/QObject instances. Identity excludes the value
    and expiry so replacing a cookie cannot leave an obsolete login behind.
    """

    def __init__(self, *, _test_cookie_host=None):
        self._domains = cookie_domains(_test_cookie_host=_test_cookie_host)
        self._cookies = {}
        self._lock = threading.Lock()
        self._unavailable = False

    def restore(self, storage_path):
        """Seed once, before Qt opens this profile; live signals then own updates.

        Qt 6.12 on the supported Linux deployment writes plaintext cookie values
        (empirically verified with the loopback fixture, Chromium schema 24).
        Never guess a key for an encrypted store or silently omit its cookies.
        mode=ro preserves SQLite's committed WAL view; immutable=1 would ignore
        a WAL left by an interrupted browser. No backup or cookie export is made.
        """
        database = Path(storage_path) / 'Cookies'
        try:
            try:
                info = database.lstat()
            except FileNotFoundError:
                return  # A fresh profile has no database yet; do not create one.
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise ValueError('Unsafe cookie store')
            domains = tuple(host for domain in sorted(self._domains) for host in (domain, '.' + domain))
            with closing(sqlite3.connect(database.absolute().as_uri() + '?mode=ro', uri=True, timeout=1)) as connection:
                connection.execute('PRAGMA query_only=ON')
                connection.execute('BEGIN')
                rows = connection.execute(
                    'SELECT name, host_key, path, value, encrypted_value, '
                    'is_secure, is_httponly, has_expires, expires_utc FROM cookies '
                    'WHERE host_key COLLATE NOCASE IN (' + ','.join('?' for _ in domains) + ')', domains)
                for name, domain, path, value, encrypted, secure, httponly, has_expires, expires in rows:
                    expiry = (expires / 1_000_000 - 11644473600) if has_expires else None
                    if expiry is not None and expiry <= time.time():
                        continue
                    if encrypted:
                        raise ValueError('Unsupported cookie encryption')
                    self._put(name, domain.lower(), path or '/', value, secure, httponly, expiry)
        except (OSError, sqlite3.Error, CookieError, ValueError, TypeError, OverflowError):
            # Do not let either login_state or _client mistake a partial seed
            # for a complete store. Exception details could contain cookie data.
            self._cookies.clear()
            self._unavailable = True

    @staticmethod
    def _key(cookie):
        return (bytes(cookie.name()).decode('latin-1'), cookie.domain().lower(),
                cookie.path() or '/')

    def added(self, cookie):
        try:
            name, domain, path = self._key(cookie)
            if domain.lstrip('.') not in self._domains:
                return
            expiry = None if cookie.isSessionCookie() else cookie.expirationDate().toSecsSinceEpoch()
            self._put(name, domain, path, bytes(cookie.value()).decode('latin-1'),
                      cookie.isSecure(), cookie.isHttpOnly(), expiry)
        except (CookieError, ValueError, TypeError, OverflowError):
            with self._lock:
                self._unavailable = True

    def _put(self, name, domain, path, value, secure, httponly, expiry):
        result = SimpleCookie()
        result[name] = value
        item = result[name]
        item['domain'], item['path'] = domain, path
        item['secure'], item['httponly'] = bool(secure), bool(httponly)
        if expiry is not None:
            item['expires'] = formatdate(expiry, usegmt=True)
        with self._lock:
            self._cookies[(name, domain, path)] = (result, expiry)

    def removed(self, cookie):
        with self._lock:
            self._cookies.pop(self._key(cookie), None)

    def snapshot(self):
        with self._lock:
            if self._unavailable:
                raise ValueError('Browser cookie store unavailable')
            now = time.time()
            return deepcopy([cookie for cookie, expiry in self._cookies.values()
                             if expiry is None or expiry > now])


class CookieSignals:
    """Normalize the empirically observed Qt 6.12.0 no-change overwrite pair.

    Re-setting an identical HttpOnly session cookie emits two cookieRemoved
    signals (old and identical new cookie), with no cookieAdded. Coalesce only
    identical pairs within one GUI event batch. Keep ambiguous removals out of
    snapshots until the deferred callback commits lone deletions; an ordinary
    replacement's cookieAdded cancels any pending pair.
    """

    def __init__(self, mirror, defer, *, qt612=False):
        self.mirror, self.defer, self.qt612 = mirror, defer, qt612
        self._removed = {}
        self._scheduled = False

    def added(self, cookie):
        self._removed.pop(self.mirror._key(cookie), None)
        self.mirror.added(cookie)

    def removed(self, cookie):
        if not self.qt612:
            self.mirror.removed(cookie)
            return
        key = self.mirror._key(cookie)
        previous = self._removed.pop(key, None)
        if previous is not None and previous == cookie:
            self.mirror.added(cookie)
        else:
            self._removed[key] = cookie
        if not self._scheduled:
            self._scheduled = True
            self.defer(self._clear)

    def _clear(self):
        for cookie in self._removed.values():
            self.mirror.removed(cookie)
        self._removed.clear()
        self._scheduled = False
