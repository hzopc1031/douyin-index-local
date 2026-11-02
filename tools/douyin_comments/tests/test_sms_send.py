"""Synthetic send responses only; never contact an authentication service."""
import asyncio
from contextlib import redirect_stderr
import io
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from playwright.async_api import TimeoutError as PlaywrightTimeout
from dyc.sms_browser import SmsBrowser, sms_result
from dyc.sms_state import LoginSession, SmsQuota
from tests.test_sms_state import Clock, DummyBrowser, PHONE, CODE


CASES = (
    (200, {'message': 'success', 'data': {'error_code': 0}}, 'accepted'),
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

    def test_response_matcher_auth_post_only(self):
        browser = SmsBrowser('/unused')
        url = 'https://sso.douyin.com/passport/web/send_code/'
        self.assertTrue(browser.sms_request(url + '?private=ignored', 'POST'))
        for wrong, method in ((url, 'GET'), (url.replace('sso.douyin.com', 'evil.invalid'), 'POST'),
                              (url.replace('https:', 'http:'), 'POST'),
                              (url.replace('send_code', 'login'), 'POST'),
                              (url.replace('douyin.com', 'douyin.com.evil.invalid'), 'POST')):
            self.assertFalse(browser.sms_request(wrong, method))


class SendTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_before_single_click_results_and_safe_audit(self):
        for status, body, expected in CASES:
            with self.subTest(expected=expected, status=status):
                browser = SmsBrowser('/unused')
                browser.visible = Mock(return_value=SimpleNamespace(fill=AsyncMock()))
                click = AsyncMock()
                response = SimpleNamespace(status=status, json=AsyncMock(return_value=body))
                waiting = False
                class Pending:
                    async def __aenter__(self):
                        nonlocal waiting
                        waiting = True
                        self.value = asyncio.get_running_loop().create_future()
                        self.value.set_result(response)
                        return self
                    async def __aexit__(self, *args):
                        if status is None:
                            raise PlaywrightTimeout('synthetic-private ' + PHONE)
                def expect(predicate, timeout):
                    self.assertEqual(timeout, 15000)
                    self.assertTrue(predicate(SimpleNamespace(url='https://sso.douyin.com/passport/web/send_code/',
                                                              request=SimpleNamespace(method='POST'))))
                    return Pending()
                async def clicked():
                    self.assertTrue(waiting)
                click.side_effect = clicked
                browser.page = Mock(url=browser.url, expect_response=expect)
                browser.page.locator.return_value.filter.return_value.filter.return_value.first.click = click
                logs = io.StringIO()
                with redirect_stderr(logs):
                    result = await browser.send(PHONE)
                self.assertEqual(result['outcome'], expected)
                click.assert_awaited_once()
                for event in ('send_click_start', 'send_click_complete', 'send_response'):
                    self.assertIn(event, logs.getvalue())
                for secret in (PHONE, CODE, 'synthetic-private', 'passport', '?'):
                    self.assertNotIn(secret, logs.getvalue())

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
