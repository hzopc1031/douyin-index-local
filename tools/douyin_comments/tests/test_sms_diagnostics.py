"""Synthetic failure evidence; no network, real credentials or profiles."""
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from dyc.errors import Stop
from dyc.sms_browser import SmsBrowser
from dyc.sms_state import LoginSession, SmsQuota


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
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
