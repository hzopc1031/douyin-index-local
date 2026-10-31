"""Synthetic login state, authentication failure and quota guards; no live fetch."""
import io
import json
import logging
import os
from http.cookies import SimpleCookie
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
import app
from browser_login import BrowserLoginState, _cookie_snapshot, sid_guard_expiry
from client import SanitizedClientError
from hub_bridge import browser_status
from hub_insights import collect
from hub_web import Hub, PRINCIPAL_KEY, mount
from test_hub import QUERY


def cookie(name='sessionid', value='synthetic-session', domain='.douyin.com', **attrs):
    result = SimpleCookie()
    result[name] = value
    result[name]['domain'] = domain
    for key, value in attrs.items():
        result[name][key] = value
    return result


class LoginStateTests(unittest.TestCase):
    def setUp(self):
        self.login = BrowserLoginState()
        self.page = Mock()
        self.page.get_cookies.return_value = []

    def state(self, cookies):
        self.page.get_cookies.return_value = cookies
        return self.login.read(self.page, now=1700000000)

    def test_sid_guard_parsing(self):
        for value, expected in [('hash%7C1700000000%7C3600%7Cignored',1700003600),
                                ('hash|1699990000|1|extra',1699990001),
                                ('hash|1700000000|0',1700000000),
                                ('garbage',None), ('hash|bad|30',None),
                                ('hash|1700000000|-1',None), ('|1700000000|1',None),
                                ('hash|0|1',None), ('hash|NaN|inf',None),
                                (None,None), ('hash|999999999999999999|1',None)]:
            with self.subTest(value=value):
                self.assertEqual(sid_guard_expiry(value), expected)

    def test_sid_guard_expired_valid_and_garbage(self):
        for value, expected in [('hash%7C1699990000%7C60%7Cextra','expired'),
                                ('hash|1700000000|3600|extra','cookie_present'),
                                ('malformed','cookie_present')]:
            with self.subTest(expected=expected):
                self.assertEqual(self.state([cookie('sid_guard', value)]), expected)

    def test_expiry(self):
        for attrs, expected in [({'expires':'Tue, 14 Nov 2023 20:00:00 GMT'},'expired'),
                                ({'expires':'Tue, 14 Nov 2023 23:00:00 GMT'},'cookie_present'),
                                ({'expires':'1700000000'},'expired'),
                                ({'expires':'garbage'},'cookie_present'),
                                ({'expires':'NaN'},'cookie_present'),
                                ({'max-age':'0'},'expired'), ({'max-age':'-1'},'expired'),
                                ({'max-age':'3600'},'cookie_present'), ({},'cookie_present')]:
            with self.subTest(attrs=attrs):
                self.assertEqual(self.state([cookie(**attrs)]), expected)
        self.assertEqual(self.state([cookie(), cookie('sid_guard', 'hash|1699990000|60')]), 'expired')

    def test_names_domains_and_empty_values(self):
        self.assertEqual(self.state([]), 'no_session_cookie')
        for name in ('sessionid', 'sessionid_ss', 'sid_tt', 'sid_guard'):
            for domain in ('.douyin.com', 'creator.douyin.com', '.DOUYIN.COM'):
                self.assertEqual(self.state([cookie(name, domain=domain)]), 'cookie_present')
        for item in (cookie('tracking'), cookie(value=''), cookie(domain='evil.douyin.com'),
                     cookie(domain='douyin.com.invalid'), cookie(domain='127.0.0.1')):
            self.assertEqual(self.state([item]), 'no_session_cookie')
        with patch.dict(os.environ, INDEX_HUB_LOGIN_COOKIE_DOMAINS='127.0.0.1'):
            self.assertEqual(self.state([cookie(domain='127.0.0.1')]), 'no_session_cookie')
        self.assertEqual(self.login.read(self.page, accepted_domains={'127.0.0.1'}), 'cookie_present')

    def test_unreadable_store_and_qr(self):
        self.page.get_cookies.side_effect = RuntimeError('synthetic private exception')
        self.assertEqual(self.login.read(self.page), 'unknown')
        self.assertEqual(self.login.read(None), 'unknown')
        self.page.get_cookies.reset_mock()
        self.assertEqual(self.login.read(self.page, qr_session=True), 'qr_session')
        self.page.get_cookies.assert_not_called()

    def test_fingerprint_sorted_rejected_and_cleared_on_change(self):
        cookies = [cookie(), cookie('sid_tt', 'synthetic-other')]
        _, fingerprint = _cookie_snapshot(cookies)
        self.assertEqual(fingerprint, _cookie_snapshot(list(reversed(cookies)))[1])
        self.login.reject(fingerprint)
        self.assertEqual(self.state(cookies), 'rejected')
        self.assertEqual(self.state(cookies + [cookie('tracking')]), 'rejected')
        self.assertEqual(self.state([cookie(value='synthetic-new')]), 'cookie_present')
        self.assertEqual(self.state(cookies), 'cookie_present')
        self.login.reject(fingerprint)
        self.assertEqual(self.state([]), 'no_session_cookie')
        self.assertEqual(self.state(cookies), 'cookie_present')
        self.login.reject(fingerprint)
        self.assertEqual(BrowserLoginState().read(self.page), 'cookie_present')


class DesktopLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for p in (patch.object(app, 'OUTPUT', Path(self.temp.name)),
                  patch.dict(os.environ, INDEX_HUB_LOGIN_MODE='desktop')):
            p.start()
            self.addCleanup(p.stop)
        self.api = app.Api()
        self.api._page = Mock()
        self.page = self.api._page
        self.page.get_current_url.return_value = 'https://creator.douyin.com/'
        self.page.evaluate_js.side_effect = lambda js: 'Synthetic UA' if js == 'navigator.userAgent' else False
        self.page.get_cookies.return_value = [cookie()]

    def test_bridge_mapping_and_page_check(self):
        for state in ('cookie_present', 'qr_session', 'no_session_cookie', 'expired', 'rejected', 'unknown'):
            with patch.object(self.api, '_login_state', return_value=state):
                result = browser_status(self.api)
            self.assertEqual(result['login_state'], state)
            self.assertEqual(result['state'], 'browser_ready' if state in ('cookie_present','qr_session') else 'login_required')
        self.page.get_cookies.return_value = []
        self.assertEqual(browser_status(self.api)['state'], 'login_required')
        self.page.get_cookies.return_value = [cookie()]
        for url in ('http://creator.douyin.com/', 'https://creator.douyin.com.invalid/',
                    'https://user@creator.douyin.com/', 'https://creator.douyin.com:444/'):
            self.page.get_current_url.return_value = url
            self.assertEqual(browser_status(self.api)['state'], 'login_required')
        self.page.get_current_url.return_value = 'https://creator.douyin.com/'
        for value in (True, None):
            self.page.evaluate_js.side_effect = None
            self.page.evaluate_js.return_value = value
            self.assertEqual(browser_status(self.api)['state'], 'login_required')

    def test_auth_failure_records_fetch_cookie_set_without_disclosure(self):
        for kind in ('trend', 'related', 'audience'):
            with self.subTest(kind=kind):
                self.api._browser_login = BrowserLoginState()
                client = Mock()
                error = SanitizedClientError('authentication_required', 'synthetic failure')
                client.query_many.side_effect = error
                client._post.side_effect = error
                output = io.StringIO()
                handler = logging.StreamHandler(output)
                logging.getLogger().addHandler(handler)
                self.addCleanup(logging.getLogger().removeHandler, handler)
                with patch('client.DouyinIndexClient', return_value=client):
                    result = (self.api.fetch_batch('fixture', '2025-01-07', cloud=True) if kind == 'trend'
                              else collect(self.api, {**QUERY, 'kind': kind}))
                self.assertEqual(result['code'], 'authentication_required')
                status = browser_status(self.api)
                self.assertEqual(status['login_state'], 'rejected')
                self.assertEqual(status['state'], 'login_required')
                fingerprint = self.api._browser_login._rejected_fingerprint
                self.assertEqual(len(fingerprint), 32)
                self.assertIsNone(self.api._fetch_cookie_fingerprint)
                emitted = json.dumps([result, status]) + output.getvalue()
                emitted += ''.join(p.read_text() for p in Path(self.temp.name).glob('*.json'))
                for private in (fingerprint.hex(), str(fingerprint), 'synthetic-session'):
                    self.assertNotIn(private, emitted)
                client.session.cookies.clear.assert_called_once()
                client.session.close.assert_called_once()
        self.page.get_cookies.return_value = [cookie(value='synthetic-new')]
        self.assertEqual(self.api._login_state(), 'cookie_present')

    def test_cookie_changed_during_failed_fetch_is_not_rejected(self):
        client = Mock()
        def fail(*args):
            self.page.get_cookies.return_value = [cookie(value='new-login')]
            raise SanitizedClientError('authentication_required', 'synthetic')
        client.query_many.side_effect = fail
        with patch('client.DouyinIndexClient', return_value=client):
            self.api.fetch_batch('fixture', '2025-01-07', cloud=True)
        self.assertEqual(self.api._login_state(), 'cookie_present')

    def test_loginrequired_fetch_path_marks_rejected(self):
        client = Mock()
        client.query_many.side_effect = app.LoginRequired('synthetic')
        with patch('client.DouyinIndexClient', return_value=client):
            self.api.fetch_batch('fixture', '2025-01-07', cloud=True)
        self.assertEqual(self.api._login_state(), 'rejected')

    def test_preflight_without_fetch_does_not_reject_cookie(self):
        self.page.get_current_url.return_value = 'about:blank'
        self.api.fetch_batch('fixture', '2025-01-07', cloud=True)
        self.assertEqual(self.api._login_state(), 'cookie_present')
        self.assertFalse(self.api._attempted)

    def test_preflight_failure_does_not_forget_previously_rejected_cookie(self):
        self.api._browser_login.reject(_cookie_snapshot(self.page.get_cookies())[1])
        self.page.get_current_url.return_value = 'about:blank'
        self.api.fetch_batch('fixture', '2025-01-07', cloud=True)
        self.assertEqual(self.api._login_state(), 'rejected')

    def test_loopback_client_requires_explicit_test_parameter(self):
        self.page.get_current_url.return_value = 'http://127.0.0.1:12345/'
        self.page.get_cookies.return_value = [cookie(domain='127.0.0.1')]
        with patch.dict(os.environ, INDEX_HUB_TEST_COOKIE_HOST='127.0.0.1'):
            with self.assertRaises(app.LoginRequired):
                self.api._client()
            self.assertEqual(self.api._login_state(), 'no_session_cookie')
        self.assertEqual(self.api._login_state(_test_cookie_host='127.0.0.1'), 'cookie_present')
        with self.assertRaises(ValueError):
            self.api._client(_test_cookie_host='arbitrary.example')
        for url in ('http://127.0.0.1.invalid/', 'http://user@127.0.0.1/'):
            self.page.get_current_url.return_value = url
            with self.assertRaises(app.LoginRequired):
                self.api._client(_test_cookie_host='127.0.0.1')

    def test_unreadable_cookie_source_aborts_client_and_closes_session(self):
        self.page.get_cookies.side_effect = ValueError('Browser cookie store unavailable')
        session = Mock()
        with patch.object(app.requests, 'Session', return_value=session):
            with self.assertRaisesRegex(ValueError, '^Browser cookie store unavailable$'):
                self.api._client()
        session.cookies.clear.assert_called_once()
        session.close.assert_called_once()
        self.assertIsNone(self.api._fetch_cookie_fingerprint)
        self.assertEqual(self.api._login_state(), 'unknown')


class HubLoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bridge = Mock()
        self.hub = Hub(self.temp.name, self.bridge)
        self.addCleanup(self.hub.store.close)
        self.webapp = web.Application()
        mount(self.webapp, self.hub)

    async def status(self):
        request = make_mocked_request('GET', '/api/v1/status', app=self.webapp)
        request[PRINCIPAL_KEY] = {'admin': True, 'scopes': ['read']}
        match = await self.webapp.router.resolve(request)
        return json.loads((await match.handler(request)).body)

    async def test_status_passes_login_state_and_preserves_string_callers(self):
        for login_state in ('cookie_present','qr_session','no_session_cookie','expired','rejected','unknown'):
            self.bridge.return_value = {'ok': True, 'state': 'browser_ready', 'login_state': login_state}
            expected = 'browser_ready' if login_state in ('cookie_present','qr_session') else 'login_required'
            self.assertEqual(await self.hub.browser_status(), expected)
            data = await self.status()
            self.assertEqual(data['browser'], expected)
            self.assertEqual(data['login_state'], login_state)
            self.assertEqual(data['daily_online_attempts'], 0)
        self.bridge.return_value = {'ok': True, 'state': 'browser_ready'}
        self.assertEqual(await self.hub.browser_status(), 'browser_ready')
        self.assertEqual((await self.status())['login_state'], 'unknown')

    async def test_invalid_or_unreadable_bridge_is_not_ready(self):
        self.bridge.return_value = {'ok': True, 'state': 'browser_ready', 'login_state': 'invalid'}
        self.assertEqual(await self.hub.browser_status(details=True), {'state':'login_required','login_state':'unknown'})
        self.bridge.side_effect = OSError('synthetic')
        with self.assertLogs('index_hub', 'WARNING'):
            self.assertEqual(await self.hub.browser_status(details=True), {'state':'unavailable','login_state':'unknown'})

    async def test_worker_blocks_missing_expired_rejected_without_quota(self):
        for kind in (None, 'related', 'audience'):
            for login_state in ('no_session_cookie','expired','rejected','unknown'):
                with self.subTest(kind=kind, login_state=login_state):
                    self.hub.store.set('paused', None)
                    query = {**QUERY, **({'kind':kind} if kind else {})}
                    job = self.hub.store.submit(query, 'fixture', f'{kind}-{login_state}')[0]
                    self.bridge.reset_mock()
                    self.bridge.return_value = {'ok': True, 'state': 'browser_ready', 'login_state': login_state}
                    self.assertTrue(await self.hub.run_one())
                    self.bridge.assert_called_once_with('status', None, 2)
                    self.assertIsNone(self.hub.store.get('daily'))
                    self.assertIsNone(self.hub.store.get('last_attempt_at'))
                    self.assertEqual(self.hub.store.job(job['id'])['error']['code'], 'authentication_required')
                    self.assertEqual(self.hub.store.get('paused')['code'], 'authentication_required')
