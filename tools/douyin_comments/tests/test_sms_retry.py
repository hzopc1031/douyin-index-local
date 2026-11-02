"""Synthetic preparation failures and retry authorization; no live requests."""
import asyncio
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from playwright.async_api import Error
from dyc.errors import Stop
from dyc.sms_browser import SmsBrowser, PHONE
from dyc.sms_state import LoginSession, SmsQuota
from dyc.sms_web import WebApp
from tests.test_sms_state import Clock, DummyBrowser, ORIGIN


class ReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_detached_frame_count_is_retried_with_fresh_frame_list(self):
        browser = SmsBrowser('/unused')
        dead = Mock()
        dead.locator.return_value.filter.return_value.count = AsyncMock(
            side_effect=Error('Frame was detached synthetic-private'))
        live = Mock()
        live.locator.return_value.filter.return_value.count = AsyncMock(return_value=0)
        browser.page = SimpleNamespace(url=browser.url, frames=[dead])
        original = dead.locator.return_value.filter.return_value.count
        async def detach():
            browser.page.frames = [live]
            return await original()
        dead.locator.return_value.filter.return_value.count = detach
        self.assertIsNone(await browser.challenge())
        self.assertEqual(browser.last_read_error, dict(operation='challenge_candidates_count',
                         exception_type='Error', category='frame_detached'))
        live.locator.return_value.filter.return_value.count.assert_awaited_once()

    async def test_read_retry_is_bounded_and_does_not_swallow_unknown_or_guard_errors(self):
        browser = SmsBrowser('/unused')
        browser.page = SimpleNamespace(url=browser.url)
        for error, count in ((Error('Frame was detached private'), 3),
                             (Error('unexpected private'), 1),
                             (Stop(3, 'verification_required'), 1)):
            read = AsyncMock(side_effect=error)
            with self.assertRaises(type(error)):
                await browser.read_dom('challenge_read', read)
            self.assertEqual(read.await_count, count)
        browser.fatal = True
        browser.fatal_reason = 'http_403'
        read = AsyncMock()
        with self.assertRaises(Stop):
            await browser.read_dom('challenge_read', read)
        read.assert_not_awaited()

    async def test_recovered_navigation_still_checks_challenge(self):
        browser = SmsBrowser('/unused')
        browser.page = SimpleNamespace(url=browser.url)
        browser._challenge = AsyncMock(side_effect=[Error('Execution context was destroyed private'), object()])
        self.assertIsNotNone(await browser.challenge())
        self.assertEqual(browser._challenge.await_count, 2)

    async def test_diagnostic_keeps_operation_and_type_but_not_sensitive_text(self):
        with tempfile.TemporaryDirectory() as root:
            browser = SmsBrowser(Path(root)/'profile', state_dir=root)
            browser.operation = 'challenge_candidates_count'
            browser.page = Mock(url=browser.url, title=AsyncMock(return_value=''))
            browser.diagnostic_snapshot = AsyncMock(side_effect=Error('private-screenshot'))
            log = io.StringIO()
            with redirect_stderr(log):
                data = await browser.diagnose(Error('Frame was detached private-exception'))
            self.assertEqual(data['operation'], 'challenge_candidates_count')
            self.assertEqual(data['exception'], dict(exception_type='Error', category='frame_detached'))
            self.assertEqual(data['screenshot_status'], 'capture_failed')
            self.assertEqual(data['reason'], 'browser_operation_failed')
            self.assertNotIn('private-', log.getvalue())
            self.assertNotIn('private-', (Path(root)/'login-status.json').read_text())


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.browsers = []
        def factory():
            browser = DummyBrowser()
            browser.diagnose = AsyncMock(return_value=dict(reason='browser_operation_failed',
                operation='challenge_candidates_count', exception=dict(exception_type='Error', category='frame_detached')))
            if not self.browsers:
                browser.start.side_effect = Error('Frame was detached synthetic-private')
            self.browsers.append(browser)
            return LoginSession(browser, SmsQuota(self.tmp.name), clock=self.clock)
        self.app = WebApp(ORIGIN, '', factory, clock=self.clock, no_token=True)
        self.headers = dict(host=ORIGIN.removeprefix('https://'), origin=ORIGIN,
                            **{'content-type':'application/json'})

    async def request(self, target, data, headers=None):
        return await self.app.dispatch('POST', target, self.headers if headers is None else headers,
                                       json.dumps(data).encode())

    async def failed(self):
        status, headers, data = await self.request('/auth', {})
        self.assertEqual(status, 503)
        self.headers['cookie'] = headers['Set-Cookie'].split(';')[0]
        self.headers['x-dyc-csrf'] = data['csrf']
        self.assertTrue(data['retryable'])
        self.assertFalse(self.app.done.is_set())
        self.browsers[0].close.assert_awaited_once()
        return data

    async def test_failure_poll_refresh_never_restarts_explicit_retry_does(self):
        failed = await self.failed()
        self.clock.value += 30
        for _ in range(3):
            await self.app.tick()
            status, _, data = await self.request('/api', dict(action='status'))
            self.assertEqual(status, 200)
            self.assertTrue(data['retryable'])
            self.assertEqual((await self.request('/auth', {}))[2]['nonce'], failed['nonce'])
        self.assertEqual(len(self.browsers), 1)
        deadline = self.app.session.deadline
        status, _, result = await self.request('/api', dict(action='retry', nonce=failed['nonce']))
        self.assertEqual((status, result['state']), (200, 'ready'))
        self.assertEqual(len(self.browsers), 2)
        self.assertEqual(self.app.session.deadline, deadline)
        self.assertNotEqual(result['nonce'], failed['nonce'])
        self.assertFalse(self.app.preparation_failed)
        for b in self.browsers:
            b.send.assert_not_awaited()
        self.assertNotEqual((await self.request('/api', dict(action='retry', nonce=failed['nonce'])))[0], 200)

    async def test_retry_requires_cookie_csrf_origin_and_fresh_nonce(self):
        failed = await self.failed()
        for change in ({'cookie':''}, {'x-dyc-csrf':''}, {'origin':'https://evil.invalid'}):
            self.assertEqual((await self.request('/api', dict(action='retry', nonce=failed['nonce']),
                                                self.headers | change))[0], 403)
        self.assertEqual((await self.request('/api', dict(action='retry', nonce='stale')))[0], 409)
        self.assertEqual(len(self.browsers), 1)

    async def test_concurrent_auth_retry_and_poll_cannot_expose_ready_or_open_twice(self):
        failed = await self.failed()
        factory = self.app.factory
        entered, release = asyncio.Event(), asyncio.Event()
        async def wait():
            entered.set()
            await release.wait()
        def delayed():
            session = factory()
            session.browser.start.side_effect = wait
            return session
        self.app.factory = delayed
        pending = asyncio.create_task(self.request('/api', dict(action='retry', nonce=failed['nonce'])))
        await entered.wait()
        try:
            for path, obj in (('/auth', {}), ('/api', {'action':'status'}),
                              ('/api', dict(action='retry', nonce=failed['nonce']))):
                status, _, data = await self.request(path, obj)
                self.assertEqual((status, data['error']), (409, 'busy'))
            await self.app.tick()
            self.browsers[-1].observe.assert_not_awaited()
            self.assertEqual(len(self.browsers), 2)
        finally:
            release.set()
            self.assertEqual((await pending)[0], 200)

    async def test_blocked_preparation_never_retries_but_can_cancel(self):
        for reason in ('verification_required','http_401','http_403','http_429','response_guard_failed'):
            with self.subTest(reason=reason):
                await self.asyncSetUp()
                factory=self.app.factory
                def blocked():
                    session=factory()
                    session.browser.diagnose.return_value={'reason':reason}
                    return session
                self.app.factory=blocked
                status, headers, data = await self.request('/auth', {})
                self.assertEqual(status, 503)
                self.assertFalse(data['retryable'])
                self.headers['cookie']=headers['Set-Cookie'].split(';')[0]
                self.headers['x-dyc-csrf']=data['csrf']
                self.assertEqual((await self.request('/api',dict(action='retry',nonce=data['nonce'])))[0],409)
                self.assertEqual((await self.request('/api',dict(action='cancel',nonce=data['nonce'])))[2]['state'],'cancelled')
                await self.app.tick()
                self.clock.value += 11
                await self.app.tick()
                self.assertTrue(self.app.done.is_set())

    async def test_retry_cannot_extend_original_session_expiry(self):
        data=await self.failed()
        self.clock.value += 601
        status, _, result=await self.request('/api',dict(action='retry',nonce=data['nonce']))
        self.assertEqual((status,result['state']),(200,'expired'))
        self.assertEqual(len(self.browsers),1)

    async def test_success_still_exits_after_grace(self):
        data=await self.failed()
        await self.request('/api',dict(action='retry',nonce=data['nonce']))
        self.browsers[-1].observe.return_value='succeeded'
        await self.app.tick()
        self.assertEqual(self.app.session.state,'succeeded')
        self.clock.value += 11
        await self.app.tick()
        self.assertTrue(self.app.done.is_set())
