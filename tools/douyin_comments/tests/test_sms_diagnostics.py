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
            browser.page = Mock(url='https://creator.douyin.com/jingxuan?secret=synthetic#secret')
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
            self.assertEqual(result['url'], 'https://creator.douyin.com/jingxuan')
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
        event = next(json.loads(line) for line in log.getvalue().splitlines() if json.loads(line)['event'] == 'login_signals')
        self.assertTrue(event['sessionid_any_domain'])
        self.assertFalse(event['sessionid'])
        self.assertNotIn('synthetic-secret', log.getvalue())





    async def test_success_record_follows_cookie_copy_and_contains_no_values(self):
        with tempfile.TemporaryDirectory() as root:
            browser = SmsBrowser('/unused', backend=True, state_dir=root)
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
            browser = SmsBrowser('/unused', backend=True, state_dir=root)
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
        browser = SmsBrowser('/unused', backend=True)
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
        browser.response(SimpleNamespace(url='https://creator.douyin.com/', status=429, request=SimpleNamespace(resource_type='xhr')))
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

class CreatorLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_creator_cookie_scope_and_guard_expiry(self):
        browser = SmsBrowser('/unused')
        browser.page = Mock(url=browser.url)
        browser.context = Mock()
        now = int(__import__('time').time())
        cookie = dict(name='sid_guard', value=f'synthetic|{now - 100}|10', domain='.douyin.com', expires=-1)
        browser.context.cookies = AsyncMock(return_value=[cookie])
        with patch('dyc.sms_browser.audit'):
            self.assertFalse(await browser.cookie_signals(False, False))
            cookie['value'] = f'synthetic|{now}|1000'
            self.assertEqual(len(await browser.cookie_signals(False, False)), 1)
            cookie['domain'] = 'login.douyin.com'
            self.assertFalse(await browser.cookie_signals(False, False))

    async def test_poll_diagnostics_redact_redirect(self):
        browser = SmsBrowser('/unused')
        response = Mock(status=200, json=AsyncMock(return_value={'data': {
            'status': '3', 'redirect_url': 'synthetic-secret'}}))
        with patch('dyc.sms_browser.audit') as audit:
            await browser.qr_response(response)
        self.assertEqual(audit.call_args.kwargs['status'], 3)
        self.assertTrue(audit.call_args.kwargs['has_redirect'])
        self.assertNotIn('synthetic-secret', str(audit.call_args))

    async def test_qr_status_shapes_and_safe_fields(self):
        browser = SmsBrowser('/unused')
        for body, expected in (({'status': 3}, 3), ({'data': {'status': '3'}}, 3),
                               ({'data': {'data': {'status': 2}}}, 2)):
            body.update(error_code='0', description='synthetic-secret', **{'synthetic-secret': 'hidden'})
            with patch('dyc.sms_browser.audit') as audit:
                await browser.qr_response(Mock(status=200, json=AsyncMock(return_value=body)))
            fields = audit.call_args.kwargs
            self.assertEqual(fields['status'], expected)
            self.assertEqual(fields['error_code'], 0)
            self.assertEqual(fields['description'], 'present')
            self.assertNotIn('synthetic-secret', str(fields))

    async def test_confirmed_redirect_follows_only_allowed_host(self):
        browser = SmsBrowser('/unused')
        browser.page = Mock(url=browser.url, is_closed=Mock(return_value=False), goto=AsyncMock())
        browser.context = Mock(pages=[browser.page])
        browser.challenge = AsyncMock(return_value=None)
        browser.login_confirmed = AsyncMock(return_value=False)
        browser.read_dom = AsyncMock(return_value=0)
        redirect = 'https://sso.douyin.com/path?ticket=synthetic-secret'
        with patch('dyc.sms_browser.audit'):
            await browser.qr_response(Mock(status=200, json=AsyncMock(return_value={'data': {'status': '3', 'redirect_url': redirect}})))
            url, stamp, page, original_url = browser.qr_redirect
            browser.qr_redirect = (url, stamp - 3, page, original_url)
            await browser.observe()
        browser.page.goto.assert_awaited_once_with(redirect, wait_until='domcontentloaded', timeout=15000)
        with patch('dyc.sms_browser.audit'):
            await browser.qr_response(Mock(status=200, json=AsyncMock(return_value={'data': {'status': 3, 'redirect_url': 'https://evil.invalid/'}})))
        self.assertIsNone(browser.qr_redirect)

    async def test_redirect_waits_for_challenge_and_keeps_first_timestamp(self):
        browser = SmsBrowser('/unused')
        browser.page = Mock(url=browser.url, is_closed=Mock(return_value=False), goto=AsyncMock())
        browser.context = Mock(pages=[browser.page])
        browser.challenge = AsyncMock(return_value=object())
        browser.login_confirmed = AsyncMock(return_value=False)
        response = Mock(status=200, json=AsyncMock(return_value={'data': {
            'status': 3, 'redirect_url': 'https://sso.douyin.com/redirect'}}))
        with patch('dyc.sms_browser.audit'), patch('dyc.sms_browser.time.monotonic', return_value=10):
            await browser.qr_response(response)
            await browser.qr_response(response)
            self.assertEqual(browser.qr_redirect[1], 10)
            self.assertEqual(await browser.observe(), 'verification')
        browser.page.goto.assert_not_awaited()

    async def test_stale_response_cannot_queue_redirect_after_reset(self):
        browser = SmsBrowser('/unused')
        browser.page = Mock(url=browser.url)
        browser.qr_generation = 1
        response = Mock(status=200, json=AsyncMock(return_value={'data': {
            'status': 3, 'redirect_url': 'https://sso.douyin.com/redirect'}}))
        with patch('dyc.sms_browser.audit'):
            await browser.qr_response(response, 0)
        self.assertIsNone(browser.qr_redirect)

    async def test_redirect_on_login_domain_reopens_creator(self):
        browser = SmsBrowser('/unused')
        original = Mock(url=browser.url, is_closed=Mock(return_value=False))
        async def navigate(url, **kwargs):
            original.url = url
        original.goto = AsyncMock(side_effect=navigate)
        creator = Mock(url=browser.url, goto=AsyncMock(), is_closed=Mock(return_value=False))
        browser.page = original
        browser.context = Mock(pages=[original], new_page=AsyncMock(return_value=creator))
        browser.challenge = AsyncMock(return_value=None)
        browser._challenge = AsyncMock(return_value=None)
        browser.login_confirmed = AsyncMock(return_value=False)
        browser.qr_redirect = ('https://sso.douyin.com/redirect', 0, original, browser.url)
        await browser.observe()
        creator.goto.assert_awaited_once_with(browser.url, wait_until='domcontentloaded', timeout=15000)
        self.assertIs(browser.page, creator)

    async def test_main_site_challenge_stops_success(self):
        browser = SmsBrowser('/unused')
        browser.page = Mock(url=browser.url)
        browser.login_confirmed = AsyncMock(return_value=True)
        frame = Mock()
        frame.locator.return_value.filter.return_value.count = AsyncMock(return_value=1)
        page = Mock(frames=[frame], goto=AsyncMock(), close=AsyncMock())
        browser.context = Mock(new_page=AsyncMock(return_value=page))
        with self.assertRaises(Stop) as error:
            await browser.persist()
        self.assertEqual(error.exception.reason, 'verification_required')
        page.close.assert_awaited_once()

    async def test_creator_requires_new_key_cookie(self):
        browser = SmsBrowser('/unused')
        cookie = dict(name='sid_guard', value='synthetic', domain='.douyin.com', path='/', expires=-1)
        browser.context = Mock(cookies=AsyncMock(return_value=[cookie]))
        browser.page = Mock(url=browser.url)
        browser.page.locator.return_value.filter.return_value.count = AsyncMock(return_value=0)
        browser.initial_sessions = {('sid_guard', '.douyin.com', '/'): 'synthetic'}
        with patch('dyc.sms_browser.audit'):
            self.assertFalse(await browser.login_confirmed())
            cookie['value'] = 'synthetic-new'
            self.assertTrue(await browser.login_confirmed())
