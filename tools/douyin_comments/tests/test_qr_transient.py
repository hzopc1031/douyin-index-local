"""Offline QR navigation and transient DOM regression tests."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import Error
from dyc.errors import Stop
from dyc.sms_browser import SmsBrowser
from dyc.sms_state import LoginError, LoginSession
from dyc.sms_web import WebApp


class QrTransientTests(unittest.IsolatedAsyncioTestCase):
    def browser(self):
        browser = SmsBrowser('/unused')
        page = Mock(url=browser.url, is_closed=Mock(return_value=False))
        browser.page = page
        browser.context = SimpleNamespace(pages=[page])
        browser.challenge = AsyncMock(return_value=None)
        browser.login_confirmed = AsyncMock(return_value=False)
        page.get_by_text.return_value.filter.return_value.count = AsyncMock(return_value=0)
        return browser

    async def test_detached_read_waits_then_rechecks_challenge(self):
        browser = self.browser()
        count = browser.page.get_by_text.return_value.filter.return_value.count
        count.side_effect = [Error('Element is not attached to the DOM')] * 3 + [0]
        self.assertEqual(await browser.observe(), 'pending')
        self.assertEqual(await browser.observe(), 'pending')
        self.assertEqual(browser.challenge.await_count, 2)
        self.assertEqual(count.await_count, 4)

    async def test_known_passport_navigation_waits_but_unknown_origin_fails(self):
        browser = self.browser()
        browser.context.cookies = AsyncMock(return_value=[])
        browser._challenge = AsyncMock(return_value=None)
        browser.page.url = 'https://sso.douyin.com/passport/'
        self.assertEqual(await browser.observe(), 'pending')
        with self.assertRaises(LoginError):
            await browser.frame(False)
        browser._challenge.return_value = object()
        with self.assertRaises(Stop) as caught:
            await browser.observe()
        self.assertEqual(caught.exception.reason, 'verification_required')
        browser.page.url = 'https://untrusted.invalid/'
        with self.assertRaises(Stop):
            await browser.observe()

    async def test_confirmed_timeout_is_not_suppressed_on_blank_page(self):
        browser = self.browser()
        browser.page.url = 'about:blank'
        browser.context.cookies = AsyncMock(return_value=[])
        browser.qr_confirmed_at = 100
        with patch('dyc.sms_browser.time.monotonic', return_value=121):
            with self.assertRaises(Stop) as caught:
                await browser.observe()
        self.assertEqual(caught.exception.reason, 'qr_confirmed_no_fresh_session_cookie')

    async def test_new_page_success_persists_and_closes(self):
        browser = self.browser()
        new = Mock(url=browser.url + '/jingxuan', is_closed=Mock(return_value=False))
        browser.context.pages.append(new)
        browser.login_confirmed.side_effect = lambda: browser.page is new
        browser.persist = AsyncMock()
        browser.close = AsyncMock()
        session = LoginSession(browser, None)
        await session.poll()
        self.assertEqual(session.state, 'succeeded')
        self.assertIs(browser.page, new)
        self.assertEqual(browser.challenge.await_count, 2)
        browser.persist.assert_awaited_once()
        browser.close.assert_awaited_once()

    async def test_real_challenge_on_old_page_blocks_new_page_success(self):
        browser = self.browser()
        browser.context.pages.append(Mock(url=browser.url, is_closed=Mock(return_value=False)))
        browser.challenge.side_effect = [object()]
        browser.login_confirmed.return_value = True
        self.assertEqual(await browser.observe(), 'verification')
        browser.login_confirmed.assert_not_awaited()
        browser.fatal = True
        browser.fatal_reason = 'http_403'
        with self.assertRaises(Stop):
            await browser.observe()

    async def test_earlier_page_can_succeed_and_pending_keeps_qr_page(self):
        browser = self.browser()
        old = browser.page
        browser.context.pages.append(Mock(url=browser.url, is_closed=Mock(return_value=False)))
        browser.login_confirmed.side_effect = lambda: browser.page is old
        self.assertEqual(await browser.observe(), 'succeeded')
        self.assertIs(browser.page, old)
        browser.login_confirmed.side_effect = None
        browser.login_confirmed.return_value = False
        self.assertEqual(await browser.observe(), 'pending')
        self.assertIs(browser.page, old)

    async def test_transient_during_confirmation_does_not_finish_session(self):
        browser = self.browser()
        browser.login_confirmed.side_effect = [True, Error('Execution context was destroyed')]
        browser.close = AsyncMock()
        session = LoginSession(browser, None)
        await session.poll()
        self.assertEqual(session.state, 'ready')
        browser.close.assert_not_awaited()

    async def test_slow_observation_remains_pending_until_session_deadline(self):
        browser = self.browser()
        browser.stage = 'observe_login'
        browser.observe = AsyncMock(side_effect=TimeoutError())
        browser.close = AsyncMock()
        session = LoginSession(browser, None)
        app = WebApp('https://dyc.invalid', '', lambda: session)
        app.session = session
        await app.tick()
        self.assertEqual(session.state, 'ready')
        browser.close.assert_not_awaited()

    async def test_page_change_categories_are_transient(self):
        browser = self.browser()
        for message in ('Execution context was destroyed', 'Execution context changed', 'Cannot find context with specified id',
                        'Frame was detached', 'navigation interrupted', 'Element is not attached to the DOM'):
            browser.challenge.side_effect = Error(message)
            self.assertEqual(await browser.observe(), 'pending')
        browser.challenge.side_effect = Error('unrelated failure')
        with self.assertRaises(Error):
            await browser.observe()

    def test_failure_copy_is_clear(self):
        root = Path(__file__).resolve().parents[1] / 'dyc'
        js = (root / 'sms_login.js').read_text()
        html = (root / 'sms_login.html').read_text()
        self.assertIn('页面操作出错，可重新获取二维码重试', js)
        self.assertIn('检测到安全验证，已停止', js)
        self.assertIn('访问受限，已停止', js)
        self.assertIn('重新获取二维码</button>', html)
