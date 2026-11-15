"""Offline checks for nonpersistent local login and its failure boundaries."""
import asyncio
import contextlib
import io
import json
import runpy
import unittest
from unittest.mock import AsyncMock, Mock, patch

from dyc.browser import Browser
from dyc.errors import Stop
from dyc.runner import local_login


class Control:
    async def run(self, awaitable):
        return await awaitable

    def check(self):
        pass


class LocalSessionTests(unittest.IsolatedAsyncioTestCase):
    def test_macos_preflight_reports_native_browser_cache(self):
        from argparse import Namespace
        from pathlib import Path
        from dyc.cli import check_env

        args = Namespace(profile_dir=Path('/unused/profile'), state_dir=Path('/unused/state'), display=None)
        with patch('dyc.cli.sys.platform', 'darwin'), patch('dyc.cli.Path.home', return_value=Path('/user')), \
             patch('dyc.cli.browser_defaults'), patch('dyc.cli.mem_available', return_value=0), \
             patch('dyc.cli.importlib.metadata.version', return_value='test'), \
             patch.dict('os.environ', {}, clear=True):
            code, result = check_env(args)
        self.assertEqual(code, 5)
        self.assertEqual(result['exit_reason'], 'low_memory')
        self.assertEqual(result['browsers_path'], '/user/Library/Caches/ms-playwright')

    async def test_windows_entrypoint_fails_closed_before_unix_import(self):
        output = io.StringIO()
        with patch('sys.platform', 'win32'), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module('dyc', run_name='__main__')
        self.assertEqual(caught.exception.code, 5)
        self.assertEqual(json.loads(output.getvalue())['exit_reason'], 'windows_comments_not_supported')

    async def test_login_waits_for_positive_signal(self):
        browser = Mock()
        browser.control = Control()
        browser.page.goto = AsyncMock()
        browser.state = AsyncMock(side_effect=[{'inside': False, 'risk': False},
                                               {'inside': True, 'risk': False}])
        with patch('dyc.runner.asyncio.sleep', new=AsyncMock()):
            await local_login(browser, backend=object())
        self.assertEqual(browser.state.await_count, 2)

    async def test_verification_stops_without_retry(self):
        browser = Mock()
        browser.control = Control()
        browser.page.goto = AsyncMock()
        browser.state = AsyncMock(return_value={'inside': False, 'risk': True})
        with self.assertRaises(Stop) as caught:
            await local_login(browser, backend=object())
        self.assertEqual(caught.exception.reason, 'verification_widget')
        self.assertEqual(browser.state.await_count, 1)

    async def test_nonpersistent_browser_has_no_profile(self):
        browser = Browser('/unused', Control(), Mock(), Mock(), ephemeral=True)
        self.assertIsNone(browser.profile)
        self.assertTrue(browser.ephemeral)

    async def test_comment_request_allows_only_observed_official_cross_origin(self):
        from types import SimpleNamespace
        from dyc import site

        browser = Browser('/unused', Control(), Mock(), Mock(), ephemeral=True)
        frame = SimpleNamespace(url='https://www.douyin.com/video/123')
        browser.page = SimpleNamespace(main_frame=frame)
        browser.video_id = '123'
        def key(url, method='GET'):
            return browser.request_key(SimpleNamespace(url=url, method=method, frame=frame))
        path = site.TOP_PATH + '?aweme_id=123&cursor=0'
        self.assertEqual(key('https://www-hj.douyin.com' + path)[1:], ('123', 'top', None, '0'))
        for url in ('https://evil.douyin.com' + path, 'http://www-hj.douyin.com' + path,
                    'https://www-hj.douyin.com:444' + path,
                    'https://www-hj.douyin.com' + site.TOP_PATH + '?aweme_id=456',
                    'https://www-hj.douyin.com' + site.TOP_PATH + '?aweme_id=123&aweme_id=456'):
            self.assertIsNone(key(url))
        self.assertIsNone(key('https://www-hj.douyin.com' + path, method='POST'))
        frame.url = 'https://evil.example/video/123'
        self.assertIsNone(key('https://www-hj.douyin.com' + path))

    async def test_session_fallback_is_scoped_and_fails_closed(self):
        browser = Browser('/unused', Control(), Mock(), Mock(), ephemeral=True)
        frame = Mock()
        frame.evaluate = AsyncMock(return_value={'risk': False, 'out': False, 'inside': False, 'ready': True})
        browser.page = Mock(url='https://www.douyin.com/jingxuan', frames=[frame], main_frame=frame)
        browser.context = Mock()
        browser.context.cookies = AsyncMock(return_value=[{'name': 'sessionid', 'value': 'synthetic',
                                                          'domain': '.douyin.com', 'expires': -1}])
        self.assertTrue((await browser.state())['inside'])
        browser.page.url = 'https://example.invalid/'
        self.assertFalse((await browser.state())['inside'])
        browser.page.url = 'https://www.douyin.com/jingxuan'
        frame.evaluate.return_value = {'risk': False, 'out': True, 'inside': False, 'ready': True}
        self.assertFalse((await browser.state())['inside'])
        frame.evaluate.return_value = {'risk': False, 'out': False, 'inside': False, 'ready': False}
        self.assertFalse((await browser.state())['inside'])
        frame.evaluate.return_value['ready'] = True
        browser.context.cookies.return_value = [{'name': 'sessionid', 'value': 'synthetic',
                                                  'domain': '.example.invalid', 'expires': -1}]
        self.assertFalse((await browser.state())['inside'])
        browser.context.cookies.return_value = [{'name': 'sessionid', 'value': 'synthetic',
                                                  'domain': '.douyin.com', 'expires': 1}]
        self.assertFalse((await browser.state())['inside'])
        browser.context.cookies.return_value = [{'name': 'sessionid', 'value': '',
                                                  'domain': '.douyin.com', 'expires': -1}]
        self.assertFalse((await browser.state())['inside'])

    async def test_nonpersistent_browser_uses_new_context_and_closes_it(self):
        from types import SimpleNamespace
        page = Mock()
        context = Mock()
        context.pages = [page]
        context.route = AsyncMock()
        context.route_web_socket = AsyncMock()
        cdp = Mock()
        cdp.send = AsyncMock()
        context.new_cdp_session = AsyncMock(return_value=cdp)
        context.close = AsyncMock()
        launched = Mock()
        launched.new_context = AsyncMock(return_value=context)
        launched.close = AsyncMock()
        chromium = Mock()
        chromium.launch = AsyncMock(return_value=launched)
        chromium.launch_persistent_context = AsyncMock()
        playwright = Mock()
        playwright.chromium = chromium
        playwright.stop = AsyncMock()
        playwright._impl_obj = SimpleNamespace(_connection=SimpleNamespace(_transport=SimpleNamespace(_proc=SimpleNamespace(pid=123))))
        factory = Mock()
        factory.start = AsyncMock(return_value=playwright)
        browser = Browser('/unused', Control(), Mock(), Mock(), ephemeral=True)
        browser.display.start = AsyncMock()
        browser.display.close = AsyncMock()
        async def watch():
            await asyncio.Event().wait()
        with patch('dyc.browser.check_memory'), patch('dyc.browser.browser_defaults'), \
                patch('playwright.async_api.async_playwright', return_value=factory), \
                patch('dyc.browser._processes', return_value={}), patch.object(browser, 'watch', watch):
            async with browser:
                chromium.launch_persistent_context.assert_not_called()
                launched.new_context.assert_awaited_once()
        context.close.assert_awaited_once()
        launched.close.assert_awaited_once()
