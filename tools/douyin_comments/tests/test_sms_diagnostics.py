"""Synthetic failure evidence; no network, real credentials or profiles."""
import asyncio
from contextlib import redirect_stderr
import io
import base64
from urllib.parse import quote
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from dyc.errors import Stop
from dyc.sms_browser import SmsBrowser
from dyc.sms_state import LoginSession, SmsQuota


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_phone_can_become_editable_after_old_twenty_second_deadline(self):
        browser = SmsBrowser('/unused')
        now = 0
        phone = Mock(is_visible=AsyncMock(return_value=True))
        phone.is_editable = AsyncMock(side_effect=lambda **_: now >= 24)
        phone.filter.return_value.first = phone
        panel = Mock(count=AsyncMock(return_value=1))
        panel.filter.return_value = panel
        browser.page = Mock(url=browser.url)
        browser.page.goto = AsyncMock()
        browser.page.locator.return_value = panel
        browser.visible = Mock(return_value=phone)
        browser.challenge = AsyncMock(return_value=None)
        browser.inside = AsyncMock(return_value=False)
        browser.sms_ready = browser.phone_ready
        browser.switch_sms = AsyncMock()
        async def advance(_):
            nonlocal now
            now += 4
        with patch('dyc.sms_browser.time', SimpleNamespace(monotonic=lambda: now)), \
             patch('dyc.sms_browser.asyncio.sleep', advance):
            await browser.reset(mode="sms")
        self.assertEqual(now, 24)
        self.assertEqual(browser.stage_timeout, 60)
        browser.page.goto.assert_awaited_once()

    async def test_disappearing_challenge_is_reread_without_an_action(self):
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        browser = SmsBrowser('/unused')
        browser.page = Mock(url=browser.url)
        browser._challenge = AsyncMock(side_effect=[PlaywrightTimeout('synthetic locator disappeared'), None])
        self.assertIsNone(await browser.challenge())
        self.assertEqual(browser._challenge.await_count, 2)
        browser.page.click.assert_not_called()

    async def test_detached_frame_is_reacquired(self):
        from playwright.async_api import Error
        browser = SmsBrowser('/unused')
        browser.page = Mock(url=browser.url)
        browser._challenge = AsyncMock(side_effect=[Error('Frame was detached'), None])
        self.assertIsNone(await browser.challenge())
        self.assertEqual(browser._challenge.await_count, 2)

    async def test_single_read_timeout_is_explicit(self):
        browser = SmsBrowser('/unused')
        browser.page = Mock(url=browser.url)
        timeout = asyncio.timeout
        async def stuck():
            await asyncio.Event().wait()
        with patch('dyc.sms_browser.asyncio.timeout', lambda _: timeout(.01)):
            with self.assertRaises(TimeoutError):
                await browser.read_dom('account_signal', stuck)

    async def test_challenge_snapshot_failure_preserves_reason(self):
        with tempfile.TemporaryDirectory() as root:
            browser = SmsBrowser('/unused', state_dir=root)
            browser.page = Mock(url=browser.url, title=AsyncMock(return_value='抖音'))
            browser.diagnostic_snapshot = AsyncMock(side_effect=RuntimeError('secret'))
            result = await browser.diagnose(reason='challenge_detected')
            self.assertEqual(result['reason'], 'challenge_detected')
            self.assertEqual(result['screenshot_status'], 'capture_failed')
            self.assertNotIn('secret', json.dumps(result))

    async def test_challenge_exposes_only_redacted_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            browser = SmsBrowser('/unused', state_dir=root)
            browser.page = Mock(url=browser.url, title=AsyncMock(return_value='抖音'))
            browser.diagnostic_snapshot = AsyncMock(return_value=b'redacted-png')
            browser.close = AsyncMock()
            session = LoginSession(browser, SmsQuota(root), stop_on_verification=True)
            browser.observe = AsyncMock(return_value='verification')
            await session.poll()
            self.assertEqual(session.failure['reason'], 'challenge_detected')
            self.assertEqual(session.failure['status'], 'BLOCKED')
            self.assertEqual(base64.b64decode(session.public()['failure_image']), b'redacted-png')
            browser.close.assert_awaited_once()

    async def test_login_requests_only_block_comments_and_private_urls(self):
        domains = ('douyin.com', 'snssdk.com', 'zijieapi.com', 'bytedance.com',
                   'byteimg.com', 'bytednsdoc.com', 'ibytedtos.com', 'pstatp.com',
                   'amemv.com', 'toutiao.com', 'ixigua.com', 'zjcdn.com',
                   'douyinpic.com', 'douyinstatic.com', 'other.invalid')
        browser = SmsBrowser('/unused')
        browser.private_values = ['13900000000', '975318']
        logs = io.StringIO()
        with redirect_stderr(logs):
            for domain in domains:
                for method in ('GET', 'HEAD', 'POST', 'OPTIONS', 'PUT', 'PATCH', 'DELETE'):
                    request = Mock(url='https://sdk.' + domain + '/register?cookie=synthetic-secret', method=method)
                    route = Mock(request=request, abort=AsyncMock(), continue_=AsyncMock())
                    await browser.route(route)
                    route.continue_.assert_awaited_once()
                    route.abort.assert_not_awaited()
            for path in ('/aweme/v1/web/comment/list/', '/aweme/v1/web/comment/list/reply/',
                         '/%61weme/v1/web/comment/list/', '/sms?phone=13900000000',
                         '/sms?code=975318', '/975318/send', '/%39%37%35%33%31%38/send',
                         '/' + quote(base64.b64encode(b'13900000000').decode(), safe='')):
                request = Mock(url='https://sso.douyin.com' + path, method='POST')
                route = Mock(request=request, abort=AsyncMock(), continue_=AsyncMock())
                await browser.route(route)
                route.abort.assert_awaited_once()
                route.continue_.assert_not_awaited()
        events = [json.loads(line) for line in logs.getvalue().splitlines()]
        self.assertTrue(all(set(('method', 'domain', 'path')) <= event.keys() for event in events))
        self.assertTrue(any(e['allowed'] and e['method'] == 'PUT' for e in events))
        for secret in ('13900000000', '975318', '%39', '?', 'synthetic-secret'):
            self.assertNotIn(secret, logs.getvalue())

    async def test_timeout_redacts_url_title_exception_and_writes_private_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            browser = SmsBrowser(Path(root) / 'profile', state_dir=root)
            browser.page = Mock(url='https://www.douyin.com/jingxuan?secret=synthetic#secret')
            browser.page.title = AsyncMock(return_value='synthetic-private-title')
            browser.diagnostic_snapshot = AsyncMock(return_value=b'synthetic-png')
            browser.step('open_login_panel', '#login-panel-new visible', 10)
            log = io.StringIO()
            with redirect_stderr(log):
                result = await browser.diagnose(TimeoutError('synthetic-private-exception'))
                self.assertIs(result, await browser.diagnose(Stop(4, 'later')))
            encoded = json.dumps(result) + log.getvalue()
            for value in ('synthetic-private', '?secret', '#secret'):
                self.assertNotIn(value, encoded)
            self.assertEqual(result['reason'], 'login_step_timeout')
            self.assertEqual(result['timeout_seconds'], 10)
            self.assertEqual(result['url'], 'https://www.douyin.com/jingxuan')
            path = Path(result['screenshot'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads((Path(root) / 'login-status.json').read_text()), result)
            browser.diagnostic_snapshot.assert_awaited_once()
            self.assertEqual(log.getvalue().count('login_failure'), 1)

    async def test_failure_is_recorded_before_browser_close_and_public_after_close(self):
        with tempfile.TemporaryDirectory() as root:
            browser = Mock(auto_attempts=0)
            events = []
            async def diagnose(error, reason):
                events.append('diagnose')
                return {'reason': reason, 'step': 'observe_login'}
            async def close():
                events.append('close')
            browser.diagnose = diagnose
            browser.close = close
            session = LoginSession(browser, SmsQuota(root))
            await session.fail(reason='verification_required')
            self.assertEqual(events, ['diagnose', 'close'])
            self.assertEqual(session.public()['failure']['reason'], 'verification_required')
            self.assertEqual(session.state, 'failed')

    async def test_login_redirects_only_block_comments_and_private_urls(self):
        for method in ('GET', 'POST', 'PUT', 'OPTIONS'):
            for target, denied in (
                ('https://lf3.byteimg.com/register', False),
                ('https://sso.amemv.com/passport/sms', False),
                ('https://other.invalid/auth', False), ('/same-host', False),
                ('https://www.douyin.com/aweme/v1/web/comment/list/', True),
                ('https://sso.douyin.com/sms?code=975318', True),
            ):
                with self.subTest(method=method, target=target):
                    browser = SmsBrowser('/unused')
                    browser.private_values = ['975318']
                    browser.response_session = Mock(send=AsyncMock())
                    await browser.check_response(dict(requestId='synthetic', responseStatusCode=307,
                        resourceType='XHR', request=dict(method=method, url='https://www.douyin.com/passport/'),
                        responseHeaders=[dict(name='Location', value=target)]))
                    expected = 'Fetch.failRequest' if denied else 'Fetch.continueRequest'
                    self.assertEqual(browser.response_session.send.call_args.args[0], expected)
                    self.assertEqual(browser.fatal, denied)

    async def test_websocket_keeps_the_same_two_exclusions(self):
        browser = SmsBrowser('/unused')
        browser.private_values = ['975318']
        for url, blocked in (('wss://sdk.bytedance.com/register', False),
                             ('wss://sdk.bytedance.com/?code=975318', True),
                             ('wss://www.douyin.com/aweme/v1/web/comment/list/', True)):
            socket = Mock(url=url, close=AsyncMock(), connect_to_server=Mock())
            await browser.websocket(socket)
            self.assertEqual(socket.close.await_count, int(blocked))
            self.assertEqual(socket.connect_to_server.call_count, int(not blocked))

    async def test_all_post_response_statuses_are_logged_only_during_window(self):
        browser = SmsBrowser('/unused')
        browser.private_values = ['13900000000', '975318']
        browser.response_session = Mock(send=AsyncMock())
        logs = io.StringIO()
        with redirect_stderr(logs), patch('dyc.sms_browser.time.monotonic', return_value=10):
            browser.sms_deadline = 25
            for path, status in (('/device/register', 200), ('/send_activation', 307), ('/report', 0)):
                await browser.check_response(dict(requestId='synthetic', responseStatusCode=status,
                    request=dict(method='POST', url='https://sdk.amemv.com' + path + '?phone=13900000000'),
                    responseHeaders=[dict(name='Location', value='/next')]))
            browser.sms_deadline = 0
            await browser.check_response(dict(requestId='synthetic', responseStatusCode=200,
                request=dict(method='POST', url='https://sdk.amemv.com/outside')))
        events = [json.loads(line) for line in logs.getvalue().splitlines()]
        posts = [e for e in events if e['event'] == 'send_post_response']
        self.assertEqual([e['http_status'] for e in posts], [200, 307, None])
        self.assertEqual([e['path'] for e in posts], ['/device/register', '/send_activation', '/report'])
        self.assertNotIn('13900000000', logs.getvalue())
        self.assertNotIn('?', logs.getvalue())

    async def test_redirected_non_get_request_is_audited_without_route(self):
        browser = SmsBrowser('/unused')
        log = io.StringIO()
        with redirect_stderr(log):
            browser.redirected_request(Mock(url='https://sdk.amemv.com/register?cookie=secret',
                                            method='POST', redirected_from=object()))
            browser.redirected_request(Mock(url='https://sdk.amemv.com/next',
                                            method='GET', redirected_from=object()))
        event = json.loads(log.getvalue())
        self.assertEqual((event['method'], event['domain'], event['path']),
                         ('POST', 'sdk.amemv.com', '/register'))
        self.assertTrue(event['allowed'])
        self.assertNotIn('secret', log.getvalue())

    async def test_websocket_does_not_connect_after_stop_or_cancel(self):
        for stopped in ('fatal', 'cancel'):
            browser = SmsBrowser('/unused')
            if stopped == 'fatal':
                browser.fatal = True
            else:
                browser.cancel.set()
            socket = Mock(url='wss://sdk.bytedance.com/register', close=AsyncMock(), connect_to_server=Mock())
            await browser.websocket(socket)
            socket.close.assert_awaited_once()
            socket.connect_to_server.assert_not_called()

class LoginSignalTests(unittest.IsolatedAsyncioTestCase):
    async def test_qr_confirmation_requires_fresh_session_and_fixed_response(self):
        from unittest.mock import AsyncMock, MagicMock
        from dyc.sms_browser import SmsBrowser
        browser = SmsBrowser('/unused')
        browser.context = MagicMock()
        browser.context.cookies = AsyncMock(return_value=[dict(name='sessionid', value='synthetic', domain='.douyin.com', path='/')])
        browser.page = MagicMock()
        browser.page.url = browser.url
        browser.page.locator.return_value.filter.return_value.count = AsyncMock(return_value=1)
        browser.initial_sessions = {('sessionid', '.douyin.com', '/'): 'synthetic'}
        response = MagicMock()
        response.status = 200
        response.json = AsyncMock(return_value={'data': {'status': '3'}})
        await browser.read_qr_response(response, browser.qr_generation)
        self.assertFalse(await browser.login_confirmed())
        browser.initial_sessions.clear()
        self.assertTrue(await browser.login_confirmed())
        response.json.return_value = {'data': {'status': '2'}}
        await browser.read_qr_response(response, browser.qr_generation)
        self.assertTrue(await browser.login_confirmed())
        browser.qr_generation += 1
        browser.qr_confirmed = False
        response.json.return_value = {'data': {'status': '3'}}
        await browser.read_qr_response(response, browser.qr_generation - 1)
        self.assertFalse(await browser.login_confirmed())
        self.assertEqual(set(browser.login_signals.values()), {True, False})

    async def test_qr_status_diagnostics_and_numeric_confirmation(self):
        browser = SmsBrowser('/unused')
        response = Mock(status=200, json=AsyncMock(return_value={
            'data': {'status': 3, 'redirect_url': 'https://example.invalid/?secret=private'}}))
        log = io.StringIO()
        with redirect_stderr(log):
            await browser.read_qr_response(response, 0)
        event = json.loads(log.getvalue())
        self.assertEqual((event['status'], event['has_redirect']), (3, True))
        self.assertTrue(browser.qr_confirmed)
        self.assertNotIn('private', log.getvalue())
        browser.qr_confirmed = False
        response.status = 500
        with redirect_stderr(io.StringIO()):
            await browser.read_qr_response(response, 0)
        self.assertFalse(browser.qr_confirmed)

    async def test_all_domain_cookie_diagnostics_keep_success_scoped_to_main_site(self):
        browser = SmsBrowser('/unused')
        browser.page = Mock(url=browser.url)
        browser.page.locator.return_value.filter.return_value.count = AsyncMock(return_value=1)
        other = dict(name='sessionid', value='synthetic-secret', domain='sso.douyin.com', path='/')
        browser.context = Mock(cookies=AsyncMock(side_effect=lambda *args: [] if args else [other]))
        browser.qr_confirmed = True
        log = io.StringIO()
        with redirect_stderr(log):
            self.assertFalse(await browser.login_confirmed())
        event = json.loads(log.getvalue())
        self.assertTrue(event['sessionid_any_domain'])
        self.assertFalse(event['sessionid'])
        self.assertNotIn('synthetic-secret', log.getvalue())

    async def test_signals_repeat_and_confirmed_without_cookie_fails(self):
        browser = SmsBrowser('/unused')
        browser.context = Mock(cookies=AsyncMock(return_value=[]))
        log = io.StringIO()
        with redirect_stderr(log), patch('dyc.sms_browser.time.monotonic', side_effect=[100, 105, 110, 120]):
            await browser.cookie_signals(None, None)
            await browser.cookie_signals(None, None)
            await browser.cookie_signals(None, None)
            browser.qr_confirmed_at = 100
            with self.assertRaises(Stop) as caught:
                await browser.check_qr_cookie_timeout()
        self.assertEqual(caught.exception.reason, 'qr_confirmed_no_fresh_session_cookie')
        self.assertEqual(len(log.getvalue().splitlines()), 2)

    async def test_document_navigation_logs_no_url_values(self):
        browser = SmsBrowser('/unused')
        request = Mock(url='https://sso.douyin.com/private-token?ticket=secret', method='GET',
                       resource_type='document', redirected_from=object())
        route = Mock(request=request, abort=AsyncMock(), continue_=AsyncMock())
        log = io.StringIO()
        with redirect_stderr(log):
            await browser.route(route)
            browser.redirected_request(request)
        route.continue_.assert_awaited_once()
        self.assertEqual(len(log.getvalue().splitlines()), 2)
        self.assertNotIn('secret', log.getvalue())
        self.assertNotIn('private-token', log.getvalue())

    async def test_qr_timeout_requires_fresh_main_site_cookie(self):
        browser = SmsBrowser('/unused')
        cookie = dict(name='sessionid', value='synthetic-old', domain='.douyin.com', path='/')
        browser.context = Mock(cookies=AsyncMock(return_value=[cookie]))
        browser.initial_sessions = {('sessionid', '.douyin.com', '/'): 'synthetic-old'}
        browser.qr_confirmed_at = 100
        with patch('dyc.sms_browser.time.monotonic', return_value=121):
            with self.assertRaises(Stop):
                await browser.check_qr_cookie_timeout()
            cookie['value'] = 'synthetic-new'
            await browser.check_qr_cookie_timeout()

    async def test_transport_error_is_not_a_guard_rejection(self):
        from unittest.mock import AsyncMock
        from dyc.sms_browser import SmsBrowser
        browser = SmsBrowser('/unused')
        session = AsyncMock()
        await browser.check_response({'requestId': 'x', 'responseErrorReason': 'Aborted',
                                      'request': {'url': 'about:blank'}}, session)
        session.send.assert_awaited_once_with('Fetch.failRequest', {'requestId': 'x', 'errorReason': 'Aborted'})
        self.assertFalse(browser.fatal)

    async def test_success_record_follows_cookie_copy_and_contains_no_values(self):
        with tempfile.TemporaryDirectory() as root:
            browser = SmsBrowser('/unused', state_dir=root)
            browser.page = Mock(url=browser.url)
            browser.login_confirmed = AsyncMock(return_value=True)
            browser.context = Mock(cookies=AsyncMock(return_value=[dict(
                name='sessionid', value='synthetic-secret', domain='.douyin.com', path='/', expires=123)]))
            browser.persistent = Mock(add_cookies=AsyncMock())
            await browser.persist()
            browser.persistent.add_cookies.assert_awaited_once()
            record = json.loads((Path(root) / 'login-status.json').read_text())
            self.assertEqual(record['status'], 'SUCCEEDED')
            self.assertNotIn('synthetic-secret', json.dumps(record))

    async def test_success_record_failure_does_not_undo_cookie_copy(self):
        with tempfile.TemporaryDirectory() as root:
            browser = SmsBrowser('/unused', state_dir=root)
            browser.page = Mock(url=browser.url)
            browser.login_confirmed = AsyncMock(return_value=True)
            browser.context = Mock(cookies=AsyncMock(return_value=[dict(
                name='sessionid', value='synthetic-secret', domain='.douyin.com', path='/', expires=123)]))
            browser.persistent = Mock(add_cookies=AsyncMock())
            with patch('dyc.sms_browser.atomic_text', side_effect=OSError('synthetic')):
                await browser.persist()
            browser.persistent.add_cookies.assert_awaited_once()
