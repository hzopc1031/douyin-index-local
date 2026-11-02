"""Synthetic failure evidence; no network, real credentials or profiles."""
from contextlib import redirect_stderr
import io
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
        browser.page.locator.side_effect = lambda selector: phone if 'normal-input' in selector else panel
        browser.challenge = AsyncMock(return_value=None)
        browser.inside = AsyncMock(return_value=False)
        async def advance(_):
            nonlocal now
            now += 4
        with patch('dyc.sms_browser.time', SimpleNamespace(monotonic=lambda: now)), \
             patch('dyc.sms_browser.asyncio', SimpleNamespace(sleep=advance)):
            await browser.reset()
        self.assertEqual(now, 24)
        self.assertEqual(browser.stage_timeout, 60)
        browser.page.goto.assert_awaited_once()

    async def test_new_cdn_permissions_are_static_only(self):
        cases = [
            ('lf-static.applogcdn.com', '/sdk.js', 'script', 'GET', True),
            ('lf3-short.ibytedapm.com', '/sdk.js', 'script', 'GET', True),
            ('lf3-static.bytednsdoc.com', '/sdk.json', 'fetch', 'GET', True),
            ('lf3-config.bytetcc.com', '/common_config', 'xhr', 'GET', False),
            ('lf6-effectcdn-tos.byteeffecttos.com', '/model.wasm', 'fetch', 'GET', True),
            ('lf3.effectcdn.com', '/style.css', 'stylesheet', 'GET', True),
            ('lf-static.applogcdn.com', '/sdk.js', 'script', 'POST', False),
            ('lf3-short.ibytedapm.com', '/report', 'fetch', 'POST', False),
            ('lf3-short.ibytedapm.com', '/report', 'fetch', 'GET', False),
            ('lf3-short.ibytedapm.com', '/login', 'document', 'GET', False),
            ('lf-static.applogcdn.com.evil.invalid', '/sdk.js', 'script', 'GET', False),
            ('invalid', '/sdk.js', 'script', 'GET', False),
        ]
        for host, path, resource, method, allowed in cases:
            with self.subTest(host=host, resource=resource, method=method):
                browser = SmsBrowser('/unused')
                browser.page = Mock()
                request = Mock(url='https://' + host + path, method=method, resource_type=resource)
                request.frame.page = browser.page
                request.is_navigation_request.return_value = resource == 'document'
                route = Mock(request=request, abort=AsyncMock(), continue_=AsyncMock())
                await browser._route(route)
                self.assertEqual(route.continue_.await_count, int(allowed))
                self.assertEqual(route.abort.await_count, int(not allowed))

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

    async def test_only_allowlisted_static_redirects_can_cross_hosts(self):
        cases = [
            ('Script', 'GET', 'https://lf3.byteimg.com/app.js', False),
            ('Script', 'GET', 'https://lf-static.applogcdn.com/sdk.js', False),
            ('Script', 'POST', 'https://lf-static.applogcdn.com/sdk.js', True),
            ('XHR', 'GET', 'https://lf-static.applogcdn.com/report', True),
            ('Stylesheet', 'GET', 'https://lf3.byteimg.com/app.css', False),
            ('Script', 'GET', 'https://evil.invalid/app.js', True),
            ('Script', 'POST', 'https://lf3.byteimg.com/app.js', True),
            ('XHR', 'GET', 'https://lf3.byteimg.com/api', True),
            ('Document', 'GET', 'https://lf3.byteimg.com/login', True),
            ('Script', 'GET', 'http://lf3.byteimg.com/app.js', True),
            ('Script', 'GET', 'https://lf3.byteimg.com:444/app.js', True),
            ('Script', 'GET', 'https://user@lf3.byteimg.com/app.js', True),
            ('Script', 'GET', 'https://lf3.byteimg.com/app.js?code=975318', True),
        ]
        for resource, method, target, denied in cases:
            with self.subTest(resource=resource, method=method, target=target):
                browser = SmsBrowser('/unused')
                browser.private_values = ['975318']
                browser.response_session = Mock(send=AsyncMock())
                await browser.check_response(dict(requestId='synthetic', responseStatusCode=302,
                    resourceType=resource, request=dict(method=method, url='https://www.douyin.com/app.js'),
                    responseHeaders=[dict(name='Location', value=target)]))
                expected = 'Fetch.failRequest' if denied else 'Fetch.continueRequest'
                self.assertEqual(browser.response_session.send.call_args.args[0], expected)
                self.assertEqual(browser.fatal, denied)
