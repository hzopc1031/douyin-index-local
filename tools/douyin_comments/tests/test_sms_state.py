"""Synthetic secrets only; no sockets, SMS, real profile or external traffic."""
import asyncio
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from argparse import Namespace

from dyc.models import SHANGHAI
from dyc.runtime import Profile
from dyc.errors import Stop
from dyc.sms_state import LoginError, LoginSession, SmsQuota, payload
from dyc.sms_web import WebApp, connection, origin_checked, read_request, serve

PHONE, CODE = '13900001234', '975318'  # deliberately synthetic fixtures
ORIGIN = 'https://dyc.example.ts.net:8444'


class Clock:
    def __init__(self, value=1000):
        self.value = value

    def __call__(self):
        return self.value


class DummyBrowser:
    auto_attempts = 0

    def __init__(self):
        self.start = AsyncMock()
        self.close = AsyncMock()
        self.observe = AsyncMock(return_value='pending')
        self.send = AsyncMock(return_value=dict(outcome="accepted", http_status=200, business_code="0", description="平台已接受发码请求"))
        self.submit = AsyncMock()
        self.reset = AsyncMock()
        self.auto_step = AsyncMock()
        self.persist = AsyncMock()
        self.pointer = AsyncMock()
        self.frame = AsyncMock(return_value=dict(image='synthetic', frame='safe'))


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock(datetime(2026, 11, 1, 12, tzinfo=SHANGHAI).timestamp())
        self.quota = SmsQuota(self.tmp.name, self.clock)

    def error(self, expected):
        with self.assertRaises(LoginError) as caught:
            self.quota.reserve()
        self.assertEqual(str(caught.exception), expected)

    def test_interval_rolling_hour_restart_and_day(self):
        self.quota.reserve()
        self.clock.value += 59.99
        self.error('send_interval')
        self.clock.value += .01
        self.quota.reserve()
        self.clock.value += 60
        self.quota.reserve()
        self.clock.value += 60
        self.quota = SmsQuota(self.tmp.name, self.clock)
        self.error('send_hour_limit')
        self.clock.value += 3600
        for _ in range(3):
            self.quota.reserve()
            self.clock.value += 60
        self.clock.value += 3600
        self.error('send_day_limit')
        data = json.loads((Path(self.tmp.name) / 'sms-attempts.json').read_text())
        self.assertEqual(len(data), 6)
        self.assertTrue(all(isinstance(v, (int, float)) for v in data))

    def test_shanghai_midnight_does_not_reset_rolling_hour(self):
        self.clock.value = datetime(2026, 11, 1, 23, 57, tzinfo=SHANGHAI).timestamp()
        for _ in range(3):
            self.quota.reserve()
            self.clock.value += 60
        self.error('send_hour_limit')
        self.clock.value += 3600
        self.quota.reserve()

    def test_corruption_rollback_and_symlink_fail_closed(self):
        self.quota.reserve()
        self.clock.value -= 1
        self.error('quota_unavailable')
        path = Path(self.tmp.name) / 'sms-attempts.json'
        path.write_text('[NaN]')
        self.error('quota_unavailable')
        path.unlink()
        path.symlink_to(Path(self.tmp.name) / 'missing')
        with self.assertRaises(Stop):
            self.quota.reserve()

    def test_profile_shared_with_login_and_fetch(self):
        with Profile(Path(self.tmp.name) / 'profile'):
            with self.assertRaises(Stop) as caught:
                with Profile(Path(self.tmp.name) / 'profile'):
                    self.fail('second profile owner')
            self.assertEqual(caught.exception.reason, 'profile_locked')


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.browser = DummyBrowser()
        self.session = LoginSession(self.browser, SmsQuota(self.tmp.name, self.clock), clock=self.clock)

    async def action(self, action, **extra):
        return await self.session.act(dict(action=action, nonce=self.session.nonce, **extra))

    async def test_nonce_code_replay_and_redacted_response(self):
        nonce = self.session.nonce
        result = await self.action('send', phone=PHONE)
        self.assertNotIn(PHONE, json.dumps(result))
        self.assertEqual(result['phone'], '139****1234')
        with self.assertRaisesRegex(LoginError, 'replayed_action'):
            await self.session.act(dict(action='send', nonce=nonce, phone=PHONE))
        await self.action('code', code=CODE)
        with self.assertRaisesRegex(LoginError, 'wrong_state'):
            await self.action('code', code=CODE)
        await self.action('mode', mode='sms')
        self.clock.value += 60
        await self.action('send', phone=PHONE)
        with self.assertRaisesRegex(LoginError, 'replayed_code'):
            await self.action('code', code=CODE)
        self.browser.submit.assert_awaited_once_with(CODE)

    async def test_serial_rejection_no_queued_credentials(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(_):
            entered.set()
            await release.wait()
            return dict(outcome="accepted")
        self.browser.send.side_effect = send
        pending = asyncio.create_task(self.action('send', phone=PHONE))
        await entered.wait()
        try:
            with self.assertRaisesRegex(LoginError, 'busy'):
                await self.action('send', phone=PHONE)
        finally:
            release.set()
            await pending
        self.browser.send.assert_awaited_once()

    async def test_expiry_releases_browser_and_replay_material(self):
        await self.action('send', phone=PHONE)
        self.clock.value += 601
        result = await self.session.act(dict(action='status'))
        self.assertEqual(result['state'], 'expired')
        self.assertFalse(self.session.key or self.session.used_codes)
        self.browser.close.assert_awaited_once()

    async def test_cancel_pending_operation_closes_browser(self):
        entered = asyncio.Event()
        async def send(_):
            entered.set()
            await asyncio.Event().wait()
        self.browser.send.side_effect = send
        pending = asyncio.create_task(self.action('send', phone=PHONE))
        await entered.wait()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        self.browser.close.assert_awaited_once()
        self.assertEqual(self.session.state, 'cancelled')

    async def test_explicit_cancel_does_not_probe_or_solve_a_challenge(self):
        self.session.state = 'verification'
        self.browser.observe.return_value = 'verification'
        result = await self.action('cancel')
        self.assertEqual(result['state'], 'cancelled')
        self.browser.observe.assert_not_awaited()
        self.browser.auto_step.assert_not_awaited()
        self.browser.close.assert_awaited_once()

    async def test_mode_change_drops_old_challenge_before_probing(self):
        self.session.state = 'verification'
        self.browser.observe.return_value = 'verification'
        async def reset():
            self.browser.observe.assert_not_awaited()
            self.browser.observe.return_value = 'pending'
        self.browser.reset.side_effect = reset
        result = await self.action('mode', mode='qr')
        self.assertEqual((result['state'], result['mode']), ('ready', 'qr'))
        self.browser.auto_step.assert_not_awaited()

    async def test_success_requires_browser_observation_and_persistence(self):
        await self.action('send', phone=PHONE)
        self.browser.observe.return_value = 'succeeded'
        result = await self.session.act(dict(action='status'))
        self.assertEqual(result['state'], 'succeeded')
        self.browser.persist.assert_awaited_once()
        self.browser.close.assert_awaited_once()

    async def test_verification_restores_actual_prior_state(self):
        for state, mode, allowed in (('ready', 'sms', False), ('code_sent', 'sms', True),
                                     ('submitting', 'sms', False), ('ready', 'qr', False)):
            self.session.state, self.session.mode, self.session.code_allowed = state, mode, allowed
            self.browser.observe.return_value = 'verification'
            await self.session.poll()
            await self.session.poll()  # repeated polls must not overwrite the saved state
            self.browser.observe.return_value = 'pending'
            await self.session.poll()
            self.assertEqual(self.session.state, state)
        self.session.mode = 'sms'
        await self.action('send', phone=PHONE)
        self.browser.send.assert_awaited_once()


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.browser = DummyBrowser()
        self.session = LoginSession(self.browser, SmsQuota(self.tmp.name, self.clock), clock=self.clock)
        self.app = WebApp(ORIGIN, 'synthetic-owner-token', lambda: self.session, clock=self.clock)
        self.headers = {'host': 'dyc.example.ts.net:8444', 'origin': ORIGIN,
                        'content-type': 'application/json', 'sec-fetch-site': 'same-origin'}

    async def request(self, target, data, headers=None):
        return await self.app.dispatch('POST', target, self.headers if headers is None else headers,
                                       json.dumps(data).encode())

    async def auth(self):
        status, headers, data = await self.request('/auth', {'token': 'synthetic-owner-token'})
        self.assertEqual(status, 200)
        self.headers['cookie'] = headers['Set-Cookie'].split(';')[0]
        self.headers['x-dyc-csrf'] = data['csrf']
        return data

    async def test_auth_is_once_cookie_secure_and_csrf_required(self):
        _, headers, _ = await self.request('/auth', {'token': 'synthetic-owner-token'})
        for flag in ('HttpOnly', 'Secure', 'SameSite=Strict', 'Path=/'):
            self.assertIn(flag, headers['Set-Cookie'])
        status, _, _ = await self.request('/auth', {'token': 'synthetic-owner-token'})
        self.assertEqual(status, 403)
        self.headers['cookie'] = headers['Set-Cookie'].split(';')[0]
        status, _, data = await self.request('/api', {'action': 'status'})
        self.assertEqual((status, data['error']), (403, 'csrf'))

    async def test_auth_and_mode_allow_full_panel_wait_with_startup_headroom(self):
        original = asyncio.timeout
        budgets = []
        def capture(seconds):
            budgets.append(seconds)
            return original(seconds)
        with patch('dyc.sms_web.asyncio.timeout', capture):
            await self.auth()
            self.assertEqual(budgets, [90])
            budgets.clear()
            await self.request('/api', dict(action='mode', nonce=self.session.nonce, mode='qr'))
            self.assertEqual(budgets, [90])
        self.browser.reset.assert_awaited_once()

    async def test_unrelated_cookies_accepted_duplicate_auth_rejected(self):
        await self.auth()
        own = self.headers['cookie']
        self.headers['cookie'] = 'theme=dark; ' + own + '; other_service=synthetic'
        self.assertEqual((await self.request('/api', {'action': 'status'}))[0], 200)
        for header in (own + '; ' + own, own + '; __Host-dyc_login=bad',
                       own + '; __Host-dyc_login', '__Host-dyc_login=bad'):
            self.headers['cookie'] = header
            self.assertEqual((await self.request('/api', {'action': 'status'}))[0], 403)

    async def test_cross_origin_missing_origin_wrong_host_and_form_rejected(self):
        for change in ({'origin': 'https://evil.invalid'}, {'host': 'evil.invalid'},
                       {'content-type': 'text/plain'}, {'sec-fetch-site': 'cross-site'}, {'origin': ''}):
            status, _, _ = await self.request('/auth', {'token': 'synthetic-owner-token'}, self.headers | change)
            self.assertEqual(status, 403)
        self.browser.start.assert_not_awaited()

    async def test_auth_expiry_and_guess_budget(self):
        for _ in range(8):
            self.assertEqual((await self.request('/auth', {'token': 'wrong'}))[0], 403)
        self.assertFalse(self.app.done.is_set())
        self.assertEqual((await self.request('/auth', {'token': 'synthetic-owner-token'}))[0], 403)
        self.clock.value += 181
        await self.app.tick()
        self.browser.start.assert_not_awaited()

    async def test_configurable_token_expiry(self):
        self.assertEqual(self.app.auth_deadline, self.clock() + 1800)
        self.app = WebApp(ORIGIN, 'synthetic-owner-token', lambda: self.session,
                          clock=self.clock, token_ttl=45)
        self.clock.value += 45
        self.assertEqual((await self.request('/auth', {'token': 'synthetic-owner-token'}))[0], 403)
        await self.app.tick()
        self.assertFalse(self.app.done.is_set())
        self.browser.start.assert_not_awaited()

    async def test_no_token_waits_then_opens_and_resumes_one_session(self):
        self.app = WebApp(ORIGIN, '', lambda: self.session, clock=self.clock, no_token=True)
        self.clock.value += 3600
        await self.app.tick()
        self.assertFalse(self.app.done.is_set())
        self.browser.start.assert_not_awaited()
        self.session.deadline = self.clock() + 600
        status, headers, data = await self.request('/auth', {})
        self.assertEqual(status, 200)
        self.headers['cookie'] = headers['Set-Cookie'].split(';')[0]
        self.headers['x-dyc-csrf'] = data['csrf']
        self.assertEqual((await self.request('/auth', {}))[2]['csrf'], data['csrf'])
        self.assertEqual((await self.request('/api', {'action': 'status'}))[0], 200)
        self.browser.start.assert_awaited_once()
        self.browser.send.assert_not_awaited()
        self.clock.value += 601
        await self.app.tick()
        self.assertEqual(self.session.state, 'expired')

    async def test_verification_stops_without_solver_or_send(self):
        self.session.stop_on_verification = True
        self.browser.observe.return_value = 'verification'
        await self.session.act(dict(action='send', nonce=self.session.nonce, phone=PHONE))
        self.assertEqual(self.session.state, 'failed')
        self.browser.auto_step.assert_not_awaited()
        self.browser.send.assert_not_awaited()
        self.browser.close.assert_awaited_once()

    async def test_verification_cannot_be_bypassed_by_switching_mode(self):
        self.session.stop_on_verification = True
        self.browser.observe.return_value = 'verification'
        await self.session.act(dict(action='mode', nonce=self.session.nonce, mode='qr'))
        self.assertEqual(self.session.state, 'failed')
        self.browser.reset.assert_not_awaited()

    async def test_loopback_static_smoke_does_not_start_browser(self):
        self.app.no_token = True
        status, _, data = await self.app.dispatch('GET', '/', {'host': '127.0.0.1:8766'}, b'')
        self.assertEqual(status, 200)
        self.assertIn(b'data-no-token="true"', data)
        self.assertNotIn(b'{{', data)
        self.browser.start.assert_not_awaited()

    async def test_exceptions_and_responses_never_leak_secret(self):
        await self.auth()
        self.browser.send.side_effect = RuntimeError(PHONE + CODE)
        logs = io.StringIO()
        with redirect_stdout(logs), redirect_stderr(logs):
            result = await self.request('/api', dict(action='send', nonce=self.session.nonce, phone=PHONE))
        serialized = json.dumps(result) + logs.getvalue()
        self.assertNotIn(PHONE, serialized)
        self.assertNotIn(CODE, serialized)
        self.assertEqual((result[0], result[2]['state'], result[2]['retryable']), (200, 'failed', True))
        self.assertEqual(self.session.state, 'failed')
        self.browser.close.assert_awaited_once()

    async def test_body_strict_duplicates_unknown_keys_get_secrets(self):
        for raw in (b'{"phone":"a","phone":"b"}', b'{"a":NaN}', b'[]'):
            with self.assertRaises(LoginError):
                payload(raw)
        result = await self.app.dispatch('GET', '/?phone=' + PHONE, self.headers, b'')
        self.assertEqual(result[0], 409)
        self.assertNotIn(PHONE, str(result))
        await self.auth()
        result = await self.request('/api', {'action': 'status', 'phone': PHONE})
        self.assertEqual(result[2], {'error': 'invalid_request'})

    async def test_timer_closes_abandoned_session(self):
        await self.auth()
        self.clock.value += 601
        await self.app.tick()
        self.assertEqual(self.session.state, 'expired')
        self.browser.close.assert_awaited_once()
        self.clock.value += 11
        await self.app.tick()
        self.assertFalse(self.app.done.is_set())
        self.assertTrue(self.app.cookie_hash and self.app.csrf)

    async def test_http_framing_and_redacted_errors_without_listener(self):
        class Writer:
            data = b''
            closed = False
            def write(self, data): self.data += data
            async def drain(self): pass
            def close(self): self.closed = True
            async def wait_closed(self): pass
        for raw in (b'POST /auth HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\nx',
                    b'POST /auth HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n',
                    ('GET /?phone='+PHONE+' HTTP/1.1\r\nHost: wrong\r\n\r\n').encode()):
            reader, writer = asyncio.StreamReader(), Writer()
            reader.feed_data(raw); reader.feed_eof()
            await connection(self.app, reader, writer)
            self.assertTrue(writer.closed)
            self.assertNotIn(PHONE.encode(), writer.data)
            self.assertIn(b'Cache-Control: no-store', writer.data)
        reader, writer = asyncio.StreamReader(), Writer()
        reader.feed_data(b'GET / HTTP/1.1\r\nHost: dyc.example.ts.net:8444\r\n\r\n'); reader.feed_eof()
        await connection(self.app, reader, writer)
        self.assertIn(b'200 OK', writer.data)
        self.assertIn(b'text/html', writer.data)

    def test_exact_https_origin_accepts_non_tailscale_hosts(self):
        for value in (ORIGIN, 'https://dyc.example.com', 'https://dyc.example.com:8444',
                      'https://discretion-spokesman-pose-fair.trycloudflare.com'):
            self.assertEqual(origin_checked(value), value)
        for value in ('http://dyc.example.ts.net', ORIGIN+'/', ORIGIN+'?phone='+PHONE,
                      'https://user@dyc.ts.net', 'https://', ORIGIN+'#fragment',
                      'https://dyc.ts.net:107'):
            with self.assertRaises(Stop):
                origin_checked(value)

    async def test_tunnel_origin_keeps_exact_header_and_csrf_checks(self):
        origin = 'https://discretion-spokesman-pose-fair.trycloudflare.com'
        self.app = WebApp(origin_checked(origin), 'synthetic-owner-token',
                          lambda: self.session, clock=self.clock)
        self.headers.update(host=origin.removeprefix('https://'), origin=origin)
        await self.auth()
        self.assertEqual((await self.request('/api', {'action': 'status'}))[0], 200)
        for change in ({'origin': ORIGIN}, {'host': 'dyc.example.ts.net:8444'},
                       {'origin': ''}, {'x-dyc-csrf': ''}):
            status, _, data = await self.request('/api', {'action': 'status'}, self.headers | change)
            self.assertEqual((status, data['error']), (403, 'csrf'))
        self.browser.send.assert_not_awaited()

    async def test_launcher_binds_only_loopback_and_writes_token_only_to_tty(self):
        class Terminal(io.StringIO):
            def close(self): pass  # retain synthetic startup output for assertions
        terminal, logs = Terminal(), io.StringIO()
        server = type('Server', (), {'close': lambda _: None, 'wait_closed': AsyncMock()})()
        args = Namespace(origin=ORIGIN, display=None, port=8766,
                         profile_dir=Path(self.tmp.name) / 'profile', state_dir=Path(self.tmp.name) / 'state')
        async def done(app):
            app.done.set()
        with patch('dyc.sms_web.open', return_value=terminal) as opened, \
             patch('dyc.sms_web.asyncio.start_server', AsyncMock(return_value=server)) as start, \
             patch.object(WebApp, 'tick', done), redirect_stdout(logs), redirect_stderr(logs):
            await serve(args)
        opened.assert_called_once_with('/dev/tty', 'w')
        self.assertEqual(start.call_args.args[1:], ('127.0.0.1', 8766))
        self.assertEqual(logs.getvalue(), '')
        self.assertIn(ORIGIN, terminal.getvalue())
        self.assertIn('一次性口令', terminal.getvalue())

    async def test_launcher_refuses_display_and_index_paths_before_access(self):
        for display, directory in ((':107', Path(self.tmp.name)), (None, Path('/var/lib/douyin-index/profile'))):
            args = Namespace(origin=ORIGIN, display=display, port=8766,
                             profile_dir=directory, state_dir=Path(self.tmp.name))
            with patch('dyc.sms_web.open') as opened, patch('dyc.sms_web.asyncio.start_server') as start:
                with self.assertRaises(Stop):
                    await serve(args)
                opened.assert_not_called()
                start.assert_not_called()

    async def test_background_launcher_needs_no_tty_or_startup_limits(self):
        args = Namespace(origin=ORIGIN, display=None, port=8766, no_token=True,
                         profile_dir=Path(self.tmp.name) / 'profile', state_dir=Path(self.tmp.name) / 'state')
        server = type('Server', (), {'close': lambda _: None, 'wait_closed': AsyncMock()})()
        async def done(app):
            app.done.set()
        with patch('dyc.sms_web.open') as opened, \
             patch('dyc.sms_web.resource.setrlimit') as limits, \
             patch('dyc.sms_web.asyncio.start_server', AsyncMock(return_value=server)), \
             patch.object(WebApp, 'tick', done):
            await serve(args)
        opened.assert_not_called()
        limits.assert_not_called()
