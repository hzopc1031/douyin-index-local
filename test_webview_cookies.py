"""Synthetic cookie stores only; never open the service's browser profile."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from email.utils import parsedate_to_datetime
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from browser_login import BrowserLoginState, _cookie_snapshot
from webview_cookies import CookieMirror, CookieSignals, cookie_domains


def network_cookie(name=b'sessionid', value=b'synthetic-session', domain='.douyin.com',
                   path='/', expiry=None, secure=True, httponly=True):
    cookie = Mock()
    cookie.name.return_value, cookie.value.return_value = name, value
    cookie.domain.return_value, cookie.path.return_value = domain, path
    cookie.isSecure.return_value, cookie.isHttpOnly.return_value = secure, httponly
    cookie.isSessionCookie.return_value = expiry is None
    cookie.expirationDate.return_value.toSecsSinceEpoch.return_value = expiry
    cookie.toRawForm.side_effect = AssertionError('Must use QNetworkCookie fields')
    return cookie


class CookieMirrorTests(unittest.TestCase):
    def test_field_conversion_identity_replacement_removal_and_detached_snapshot(self):
        mirror = CookieMirror()
        mirror.added(network_cookie(value=b'synthetic=quoted;"value"'))
        mirror.added(network_cookie(path='/index', domain='creator.douyin.com', expiry=time.time() + 3600))
        snapshot = mirror.snapshot()
        self.assertEqual(len(snapshot), 2)
        item = snapshot[0]['sessionid']
        self.assertEqual(item.value, 'synthetic=quoted;"value"')
        self.assertTrue(item['httponly'])
        self.assertTrue(item['secure'])
        self.assertEqual(item['expires'], '')
        snapshot[0]['sessionid'] = 'mutated-copy'
        self.assertNotEqual(mirror.snapshot()[0]['sessionid'].value, 'mutated-copy')
        mirror.added(network_cookie(value=b'synthetic-new'))
        self.assertEqual(len(mirror.snapshot()), 2)
        self.assertEqual(mirror.snapshot()[0]['sessionid'].value, 'synthetic-new')
        mirror.removed(network_cookie())
        mirror.removed(network_cookie())
        remaining = mirror.snapshot()[0]['sessionid']
        self.assertEqual((remaining['domain'], remaining['path']), ('creator.douyin.com', '/index'))

    def test_domain_allowlist_and_expiry(self):
        mirror = CookieMirror()
        for domain in ('127.0.0.1', 'evil.douyin.com', 'douyin.com.invalid'):
            mirror.added(network_cookie(domain=domain))
        mirror.added(network_cookie(expiry=time.time() - 1))
        self.assertEqual(mirror.snapshot(), [])
        mirror.added(network_cookie(domain='.DOUYIN.COM'))
        self.assertEqual(len(mirror.snapshot()), 1)
        fixture = CookieMirror(_test_cookie_host='127.0.0.1')
        fixture.added(network_cookie(domain='127.0.0.1'))
        self.assertEqual(len(fixture.snapshot()), 1)
        with self.assertRaises(ValueError):
            cookie_domains(_test_cookie_host='arbitrary.example')
        with patch.dict('os.environ', INDEX_HUB_TEST_COOKIE_HOST='127.0.0.1'):
            self.assertNotIn('127.0.0.1', cookie_domains())


class CookieRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / 'Cookies'
        self.mirror = CookieMirror()

    def create_database(self):
        connection = sqlite3.connect(self.database)
        connection.execute('CREATE TABLE cookies (name TEXT, host_key TEXT, path TEXT, value TEXT, '
                           'encrypted_value BLOB, is_secure INTEGER, is_httponly INTEGER, '
                           'has_expires INTEGER, expires_utc INTEGER)')
        return connection

    @staticmethod
    def insert(connection, name='sessionid', domain='.douyin.com', path='/', value='synthetic-session',
               encrypted=b'', secure=1, httponly=1, expires=None):
        stamp = 0 if expires is None else int((expires + 11644473600) * 1_000_000)
        connection.execute('INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?,?)',
                           (name, domain, path, value, encrypted, secure, httponly, expires is not None, stamp))
        connection.commit()

    def test_fresh_profile_does_not_create_database(self):
        self.mirror.restore(self.root)
        self.assertEqual(list(self.root.iterdir()), [])
        page = Mock(get_cookies=self.mirror.snapshot)
        self.assertEqual(BrowserLoginState().read(page), 'no_session_cookie')

    def test_readonly_restore_preserves_fields_and_filters_in_sql(self):
        expiry = int(time.time()) + 3600
        with closing(self.create_database()) as connection:
            self.insert(connection)
            self.insert(connection, name='sid_guard', domain='creator.douyin.com', path='/index', expires=expiry)
            self.insert(connection, name='plain', secure=0, httponly=0, expires=expiry)
            # An unsupported encrypted value outside the allowlist must never
            # be decoded or cause the permitted hosts to fail.
            self.insert(connection, domain='douyin.com.invalid', encrypted=b'synthetic-unsupported')
            self.insert(connection, name='expired', expires=time.time() - 1)
        before = self.database.read_bytes()
        self.mirror.restore(self.root)
        self.assertTrue(self.database.read_bytes() == before)
        cookies = {m.key: m for c in self.mirror.snapshot() for m in c.values()}
        self.assertEqual(set(cookies), {'sessionid', 'sid_guard', 'plain'})
        self.assertEqual(cookies['sessionid']['expires'], '')
        self.assertTrue(cookies['sessionid']['httponly'])
        self.assertTrue(cookies['sid_guard']['secure'])
        self.assertEqual(cookies['sid_guard']['path'], '/index')
        self.assertEqual(parsedate_to_datetime(cookies['sid_guard']['expires']).timestamp(), expiry)
        self.assertFalse(cookies['plain']['httponly'])
        self.assertFalse(cookies['plain']['secure'])
        # Only live signals update the seed; snapshot never reopens SQLite to
        # resurrect a removed cookie or overwrite a current login.
        self.mirror.removed(network_cookie())
        self.mirror.added(network_cookie(name=b'plain', value=b'synthetic-new'))
        with patch('sqlite3.connect', side_effect=AssertionError('no reread')):
            self.assertEqual({m.key: m.value for c in self.mirror.snapshot() for m in c.values()},
                             {'sid_guard': 'synthetic-session', 'plain': 'synthetic-new'})

    def test_committed_wal_is_read_before_qt_opens_profile(self):
        with closing(self.create_database()) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            self.insert(connection)
            self.assertTrue(Path(str(self.database) + '-wal').exists())
            self.mirror.restore(self.root)
            self.assertEqual(len(self.mirror.snapshot()), 1)

    def test_unknown_encryption_fails_closed_without_partial_cookie_set(self):
        for encrypted in (b'v10-synthetic', b'v11-synthetic'):
            with self.subTest(format=encrypted[:3]):
                if self.database.exists():
                    self.database.unlink()
                with closing(self.create_database()) as connection:
                    self.insert(connection)
                    self.insert(connection, name='sid_guard', encrypted=encrypted)
                self.mirror.restore(self.root)
                with self.assertRaisesRegex(ValueError, '^Browser cookie store unavailable$'):
                    self.mirror.snapshot()
                self.assertEqual(BrowserLoginState().read(Mock(get_cookies=self.mirror.snapshot)), 'unknown')

    def test_corrupt_or_symlink_store_fails_closed(self):
        self.database.write_bytes(b'synthetic-corrupt-database')
        self.mirror.restore(self.root)
        with self.assertRaisesRegex(ValueError, '^Browser cookie store unavailable$'):
            self.mirror.snapshot()
        self.database.rename(self.root / 'synthetic-target')
        self.database.symlink_to(self.root / 'synthetic-target')
        self.mirror.restore(self.root)
        with self.assertRaisesRegex(ValueError, '^Browser cookie store unavailable$'):
            self.mirror.snapshot()

    def test_expiry_after_restore_is_not_sent(self):
        now = int(time.time())
        with closing(self.create_database()) as connection:
            self.insert(connection, expires=now + 60)
        self.mirror.restore(self.root)
        self.assertEqual(len(self.mirror.snapshot()), 1)
        with patch('webview_cookies.time.time', return_value=now + 61):
            self.assertEqual(self.mirror.snapshot(), [])


class CookieSignalTests(unittest.TestCase):
    def setUp(self):
        self.mirror = CookieMirror()
        self.deferred = []
        self.signals = CookieSignals(self.mirror, self.deferred.append, qt612=True)
        self.cookie = network_cookie()
        self.signals.added(self.cookie)

    def flush(self):
        for callback in self.deferred:
            callback()
        self.deferred.clear()

    def test_paired_removals_preserve_snapshots_and_rejected_login_between_signals(self):
        snapshot = self.mirror.snapshot()
        _, fingerprint = _cookie_snapshot(snapshot)
        page = Mock(get_cookies=self.mirror.snapshot)
        with ThreadPoolExecutor(max_workers=1) as bridge:
            for rejected in (False, True):
                with self.subTest(rejected=rejected):
                    login = BrowserLoginState()
                    if rejected:
                        login.reject(fingerprint)
                    expected = 'rejected' if rejected else 'cookie_present'
                    states, snapshots = [], []
                    # Pause the GUI after each signal so the bridge can read
                    # the single credential before deferred batch resolution.
                    for operation in (lambda: self.signals.removed(self.cookie),
                                      lambda: self.signals.removed(self.cookie), self.flush):
                        operation()
                        states.append(bridge.submit(login.read, page).result(timeout=1))
                        snapshots.append(bridge.submit(self.mirror.snapshot).result(timeout=1))
                    self.assertEqual(states, [expected] * 3)
                    self.assertEqual(snapshots, [snapshot] * 3)

    def test_identical_pair_is_retained_but_subsequent_real_deletion_is_not(self):
        self.signals.removed(self.cookie)
        self.signals.removed(self.cookie)
        self.assertEqual(len(self.mirror.snapshot()), 1)
        self.signals.removed(self.cookie)
        self.flush()
        self.assertEqual(self.mirror.snapshot(), [])

    def test_lone_deletions_in_separate_batches_never_restore_cookie(self):
        for _ in range(2):
            self.signals.removed(self.cookie)
            self.flush()
            self.assertEqual(self.mirror.snapshot(), [])

    def test_different_cookie_or_intervening_add_cannot_complete_pair(self):
        self.signals.removed(self.cookie)
        newer = network_cookie(value=b'synthetic-new')
        self.signals.removed(newer)
        self.flush()
        self.assertEqual(self.mirror.snapshot(), [])
        self.signals.added(self.cookie)
        self.signals.removed(self.cookie)
        self.signals.added(newer)
        self.signals.removed(newer)
        self.flush()
        self.assertEqual(self.mirror.snapshot(), [])

    def test_pair_workaround_is_specific_to_qt612(self):
        self.signals.qt612 = False
        self.signals.removed(self.cookie)
        self.signals.removed(self.cookie)
        self.assertEqual(self.mirror.snapshot(), [])
        self.assertEqual(self.deferred, [])
