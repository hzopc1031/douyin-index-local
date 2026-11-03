"""Synthetic send responses only; never contact an authentication service."""
import asyncio
from contextlib import redirect_stderr
import io
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from dyc.sms_browser import SmsBrowser, sms_result
from dyc.sms_state import LoginSession, SmsQuota
from tests.test_sms_state import Clock, DummyBrowser, PHONE, CODE


CASES = (
    (200, {'message': 'success', 'data': {'error_code': 0}}, 'accepted'),
    (200, {'message': 'success', 'data': {'error_code': 0, 'description': '验证码发送成功'}}, 'accepted'),
    (200, {'message': 'success', 'data': {'error_code': 0, 'description': 'Verification code sent'}}, 'accepted'),
    (200, {'message': 'success', 'data': {'error_code': 0, 'description': 'Security verification required'}}, 'verification_required'),
    (200, {'message': 'error', 'data': {'error_code': 1001, 'description': 'invalid phone'}}, 'business_failed'),
    (429, None, 'rate_limited'),
    (200, {'data': {'error_code': 1105, 'description': '请完成安全验证'}}, 'verification_required'),
    (None, None, 'unconfirmed'),
    (200, {}, 'unconfirmed'),
    (200, {'code': 0, 'data': {'error_code': 9}}, 'business_failed'),
    (200, {'code': 0, 'message': 'error'}, 'business_failed'),
    (403, {'code': 0}, 'access_denied'),
)


class ResultTests(unittest.TestCase):
    def test_http_and_business_results_fail_closed(self):
        for status, body, expected in CASES:
            with self.subTest(status=status, body=body):
                self.assertEqual(sms_result(status, body)['outcome'], expected)
        for value in (PHONE, CODE, True, {}, 'secret-cookie', 'https://secret.invalid/?q=private'):
            result = sms_result(200, {'code': value, 'description': PHONE + CODE})
            self.assertEqual(result['outcome'], 'business_failed')
            self.assertEqual(result['business_code'], 'redacted')
            self.assertNotIn(PHONE, json.dumps(result))
            self.assertNotIn(CODE, json.dumps(result))

    def test_response_matcher_only_accepts_send_paths(self):
        browser = SmsBrowser('/unused')
        for path in ('/passport/web/send_code/', '/sms/send', '/send_activation_code', '/send_sms_code'):
            self.assertTrue(browser.sms_request('https://sso.amemv.com' + path + '?ignored=1', 'POST'))
            self.assertFalse(browser.sms_request('https://sso.amemv.com' + path, 'GET'))
        for path in ('/passport/web/check_qrconnect/', '/passport/login', '/passport/precheck',
                     '/send_activation', '/sms', '/report?path=send_code', '/sms/check_qrconnect/'):
            self.assertFalse(browser.sms_request('https://sso.douyin.com' + path, 'POST'))


class SendTests(unittest.IsolatedAsyncioTestCase):
    async def run_send(self, responses, *, rejected_challenge=False, countdown=False):
        browser = SmsBrowser('/unused')
        browser.visible = Mock(return_value=SimpleNamespace(fill=AsyncMock(), click=AsyncMock(),
            press=AsyncMock(), press_sequentially=AsyncMock(), input_value=AsyncMock(return_value=PHONE)))
        browser.prepare_send = AsyncMock(return_value='')
        browser.countdown = AsyncMock(side_effect=[False] + [countdown] * 100)
        browser.challenge = AsyncMock(side_effect=lambda: bool(rejected_challenge and browser.fatal))
        browser.auto_step = AsyncMock()
        listeners = {}
        browser.context = Mock()
        browser.context.on.side_effect = lambda event, callback: listeners.update({event: callback})
        browser.context.remove_listener.side_effect = lambda event, callback: listeners.pop(event)
        async def clicked(**_):
            self.assertEqual(set(listeners), {'request', 'response'})
            for path, status, body in responses:
                request = Mock(url='https://sso.amemv.com' + path + '?cookie=synthetic-secret', method='POST', redirected_from=None)
                response = SimpleNamespace(url=request.url, request=request, status=status,
                                           json=AsyncMock(return_value=body))
                listeners['request'](request)
                if status is not None:
                    listeners['response'](response)
                await asyncio.sleep(0)
        click = AsyncMock(side_effect=clicked)
        browser.page = Mock(url=browser.url)
        panel = browser.page.locator.return_value.filter.return_value.first
        panel.get_by_role.return_value.count = AsyncMock(return_value=0)
        panel.get_by_text.return_value.filter.return_value.count = AsyncMock(return_value=1)
        browser.send_button = Mock(return_value=SimpleNamespace(click=click, wait_for=AsyncMock(), evaluate=AsyncMock(return_value=True)))
        logs = io.StringIO()
        with redirect_stderr(logs), patch('dyc.sms_browser.SMS_RESPONSE_SECONDS', .03):
            result = await browser.send(PHONE)
        click.assert_awaited_once()
        if rejected_challenge:
            browser.auto_step.assert_not_awaited()
        self.assertEqual(listeners, {})
        self.assertEqual(browser.sms_deadline, 0)
        for event in ('send_click_start', 'send_click_complete', 'send_response'):
            self.assertIn(event, logs.getvalue())
        for secret in (PHONE, CODE, 'synthetic-secret', '?'):
            self.assertNotIn(secret, logs.getvalue())
        return result

    async def test_wait_before_single_click_results_and_safe_audit(self):
        for status, body, expected in CASES:
            with self.subTest(expected=expected, status=status):
                result = await self.run_send([('/passport/web/send_code/', status, body)])
                self.assertEqual(result['outcome'], expected)

    async def test_qr_poll_zero_is_never_send_success(self):
        result = await self.run_send([('/passport/web/check_qrconnect/', 200, {'code': 0}),
                                      ('/passport/precheck', 200, {'code': 0})])
        self.assertEqual(result['outcome'], 'unconfirmed')
        self.assertIn('未捕获真实发码请求', result['description'])

    async def test_rejection_takes_precedence_over_success(self):
        failed = ('/passport/web/send_code/', 200, {'code': 1001})
        success = ('/send_activation_code', 200, {'code': 0})
        for responses in ((failed, success), (success, failed),
                          (('/report', 200, {'code': 0}), failed, success)):
            result = await self.run_send(responses)
            self.assertEqual(result['outcome'], 'business_failed')
        result = await self.run_send([('/report', 200, {'code': 0}), failed, ('/sms', 200, {})])
        self.assertEqual(result['outcome'], 'business_failed')

    async def test_countdown_only_and_rejection_priority(self):
        result = await self.run_send([], countdown=True)
        self.assertEqual(result['outcome'], 'code_sent_by_countdown')
        result = await self.run_send([('/send_code', 200, {'code': 9})], countdown=True)
        self.assertEqual(result['outcome'], 'business_failed')
        result = await self.run_send([('/check_qrconnect/', 200, {'code': 0})], countdown=True)
        self.assertEqual(result['outcome'], 'code_sent_by_countdown')

    async def test_platform_stop_outcomes_take_precedence_over_success(self):
        success = ('/send_code', 200, {'code': 0})
        for reason, outcome in (('Security verification required', 'verification_required'),
                                ('too many', 'rate_limited'), ('risk control', 'access_denied')):
            stopped = ('/send_code', 200, {'code': 9, 'description': reason})
            for responses in ((success, stopped), (stopped, success)):
                with self.subTest(outcome=outcome, responses=responses):
                    result = await self.run_send(responses)
                    self.assertEqual(result['outcome'], outcome)

    async def test_business_stop_does_not_solve_remaining_challenge(self):
        for description in ('too many', 'risk control', 'security verification required'):
            result = await self.run_send([('/send_code', 200, {'code': 9, 'description': description})],
                                         rejected_challenge=True)
            self.assertIn(result['outcome'], ('rate_limited', 'access_denied', 'verification_required'))

    async def test_redirected_get_late_body_and_old_response(self):
        browser = SmsBrowser('/unused')
        browser.visible = Mock(return_value=SimpleNamespace(fill=AsyncMock(), click=AsyncMock(),
            press=AsyncMock(), press_sequentially=AsyncMock(), input_value=AsyncMock(return_value=PHONE)))
        browser.prepare_send = AsyncMock(return_value='')
        browser.countdown = AsyncMock(return_value=False)
        browser.challenge = AsyncMock(return_value=None)
        listeners = {}
        browser.context = Mock()
        browser.context.on.side_effect = lambda event, callback: listeners.update({event: callback})
        browser.context.remove_listener.side_effect = lambda event, callback: listeners.pop(event)
        body_read = asyncio.Event()
        old_body = AsyncMock(return_value={'code': 0})
        async def body():
            await asyncio.sleep(.04)
            body_read.set()
            return {'code': 0}
        async def clicked(**_):
            old = Mock(url='https://sso.amemv.com/passport/old', method='POST', redirected_from=None)
            listeners['response'](SimpleNamespace(request=old, status=200,
                                                   json=old_body))
            first = Mock(url='https://sso.amemv.com/sms/send', method='POST', redirected_from=None)
            listeners['request'](first)
            redirected = Mock(url='https://sso.amemv.com/result', method='GET', redirected_from=first)
            listeners['response'](SimpleNamespace(request=redirected, status=200, json=body))
        browser.page = Mock(url=browser.url)
        panel = browser.page.locator.return_value.filter.return_value.first
        panel.get_by_role.return_value.count = AsyncMock(return_value=0)
        panel.get_by_text.return_value.filter.return_value.count = AsyncMock(return_value=1)
        browser.send_button = Mock(return_value=SimpleNamespace(click=AsyncMock(side_effect=clicked), wait_for=AsyncMock(), evaluate=AsyncMock(return_value=True)))
        with patch('dyc.sms_browser.SMS_RESPONSE_SECONDS', .02):
            result = await browser.send(PHONE)
        old_body.assert_not_awaited()
        self.assertTrue(body_read.is_set())
        self.assertEqual(result['outcome'], 'accepted')
        self.assertEqual(listeners, {})

    async def test_session_acceptance_failure_and_quota_survive_restart(self):
        for status, body, expected in CASES:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as root:
                clock, browser = Clock(), DummyBrowser()
                quota = SmsQuota(root, clock)
                session = LoginSession(browser, quota, clock=clock)
                browser.send.return_value = sms_result(status, body)
                result = await session.act(dict(action='send', nonce=session.nonce, phone=PHONE))
                self.assertEqual(result['state'], 'code_sent' if expected == 'accepted' else 'failed')
                self.assertEqual(session.code_allowed, expected == 'accepted')
                if expected != 'accepted':
                    browser.close.assert_awaited_once()
                with self.assertRaisesRegex(Exception, 'send_interval'):
                    quota.reserve()
                clock.value += 60
                quota.reserve()
