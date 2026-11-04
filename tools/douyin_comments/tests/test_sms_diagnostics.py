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






class LoginSignalTests(unittest.IsolatedAsyncioTestCase):


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





    async def test_success_record_follows_cookie_copy_and_contains_no_values(self):
        with tempfile.TemporaryDirectory() as root:
            browser = SmsBrowser('/unused', state_dir=root)
            browser.page = Mock(url=browser.url)
            browser.login_confirmed = AsyncMock(return_value=True)
            browser.context = Mock(cookies=AsyncMock(return_value=[dict(
                name='sessionid', value='synthetic-secret', domain='.douyin.com', path='/', expires=123)]))
            browser.persistent = Mock(add_cookies=AsyncMock())
            await browser.persist()
            browser.persistent.add_cookies.assert_not_awaited()
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
            browser.persistent.add_cookies.assert_not_awaited()

class FreshLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_main_site_cookie_and_avatar_required(self):
        browser = SmsBrowser('/unused')
        cookie = dict(name='sessionid', value='synthetic-old', domain='.douyin.com', path='/')
        browser.context = Mock(cookies=AsyncMock(return_value=[cookie]))
        browser.page = Mock(url=browser.url)
        count = AsyncMock()
        browser.page.locator.return_value.filter.return_value.count = count
        browser.initial_sessions = {('sessionid', '.douyin.com', '/'): 'synthetic-old'}
        async def check(panel, avatar):
            count.side_effect = [panel, avatar]
            return await browser.login_confirmed()
        with redirect_stderr(io.StringIO()):
            self.assertFalse(await check(0, 1))
            cookie['value'] = 'synthetic-new'
            self.assertFalse(await check(1, 1))
            self.assertFalse(await check(0, 0))
            self.assertTrue(await check(0, 1))
            self.assertTrue(all(isinstance(v, bool) for v in browser.login_signals.values()))

class AccessDenialTests(unittest.IsolatedAsyncioTestCase):
    async def test_access_denial_closes_context_without_waiting_for_poll(self):
        from types import SimpleNamespace
        browser = SmsBrowser('/unused')
        browser.context = Mock(close=AsyncMock())
        browser.response(SimpleNamespace(status=429, request=SimpleNamespace(resource_type='xhr')))
        await browser.abort_task
        browser.context.close.assert_awaited_once()
        self.assertEqual(browser.fatal_reason, 'http_429')

    async def test_cleanup_error_still_releases_profile_lock(self):
        with tempfile.TemporaryDirectory() as root:
            browser = SmsBrowser(Path(root) / 'profile')
            browser.profile.__enter__()
            browser.display.close = AsyncMock(side_effect=RuntimeError('synthetic'))
            with self.assertRaises(RuntimeError):
                await browser.close()
            self.assertIsNone(browser.profile.fd)
