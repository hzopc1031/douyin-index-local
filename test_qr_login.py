"""Synthetic-only QR login tests; no official endpoints are contacted."""
import io
import logging
import os
import struct
import threading
import time
import tempfile
from pathlib import Path
import unittest
import zlib
from unittest.mock import patch

import requests

from qr_login import CookieRejected, QrLoginSession, QrBusy, QrLoginManager, QrUnsafeDebug, QrNoDisplay, SessionHolder, narrow_cookies


def _browser_skip(reason):
    """INDEX_HUB_REQUIRE_BROWSER_TESTS=1 turns browser-test skips into failures (go-live check)."""
    if os.environ.get('INDEX_HUB_REQUIRE_BROWSER_TESTS') == '1':
        raise AssertionError('browser test required but skipped: ' + reason)
    raise unittest.SkipTest(reason)

SECRET = 'SYNTHETIC_SESSION'
HOST = 'creator.douyin.com'


def cookie(**changes):
    result = dict(name='sessionid', value=SECRET, domain=HOST, path='/',
                  secure=True, expires=-1)
    result.update(changes)
    return result


class UnitTests(unittest.TestCase):
    def test_browser_mode_config(self):
        import qr_login
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(qr_login.qr_browser_mode(), 'headed')
            self.assertEqual(QrLoginManager().status()['browser_mode'], 'headed')
            with self.assertRaises(ValueError):
                QrLoginManager(browser_mode='bogus')
            for mode in ('headed', 'headless'):
                os.environ['INDEX_HUB_QR_BROWSER'] = mode
                self.assertEqual(qr_login.qr_browser_mode(), mode)
                self.assertEqual(QrLoginManager().status()['browser_mode'], mode)
            os.environ['INDEX_HUB_QR_BROWSER'] = 'invalid'
            with patch.object(qr_login, '_browser_mode_warned', False), self.assertLogs('index_hub.qr_login') as logs:
                self.assertEqual(qr_login.qr_browser_mode(), 'headed')
                self.assertEqual(qr_login.qr_browser_mode(), 'headed')
            self.assertEqual(len(logs.output), 1)

    def test_no_display(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(QrNoDisplay) as caught:
                QrLoginManager(browser_mode='headed').start()
            self.assertEqual(caught.exception.code, 'qr_no_display')
            self.assertEqual(str(caught.exception), '有头扫码需要桌面显示环境（DISPLAY）')

    def test_launch_and_real_ua(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        for mode in ('headed', 'headless'):
            for ua in ('Synthetic Chrome', 'Synthetic HeadlessChrome'):
                with self.subTest(mode=mode, ua=ua):
                    browser, context, page = Mock(), Mock(), Mock()
                    browser.new_context.return_value = context
                    context.new_page.return_value = page
                    page.url = 'https://' + HOST + '/home'
                    page.locator.return_value.inner_text.return_value = 'Home'
                    page.locator.return_value.count.return_value = 0
                    page.evaluate.return_value = ua
                    context.cookies.return_value = [cookie()]
                    launch = Mock(return_value=browser)
                    factory = Mock()
                    factory.return_value.__enter__ = Mock(return_value=SimpleNamespace(chromium=SimpleNamespace(launch=launch)))
                    factory.return_value.__exit__ = Mock(return_value=False)
                    manager = _ephemeral_manager(browser_factory=factory, browser_mode=mode)
                    with patch.dict(os.environ, {'DISPLAY': ':107'}, clear=True), patch('qr_login.logger.warning') as warning:
                        session = manager.start()
                        session.thread.join(2)
                    self.assertFalse(session.thread.is_alive())
                    self.assertEqual(launch.call_args.kwargs, dict(headless=mode == 'headless', chromium_sandbox=False))
                    self.assertNotIn('user_agent', browser.new_context.call_args.kwargs)
                    page.evaluate.assert_called_once_with('navigator.userAgent')
                    rejected = mode == 'headed' and 'HeadlessChrome' in ua
                    self.assertEqual(manager.status()['state'], 'failed' if rejected else 'succeeded')
                    self.assertEqual(manager.holder.has_session(), not rejected)
                    self.assertEqual(manager.holder.info()['headless_ua'], mode == 'headless' and 'HeadlessChrome' in ua)
                    if 'HeadlessChrome' in ua:
                        warning.assert_called_once_with('浏览器标识含 HeadlessChrome，已拒绝会话' if rejected else '无头浏览器标识，风控风险较高')
                    if not rejected:
                        http, actual_ua = manager.holder.take_session()
                        self.assertEqual(actual_ua, ua)
                        http.cookies.clear()
                        http.close()
                    manager.holder.clear()
                    self.assertFalse(manager.holder.info()['headless_ua'])

    def test_cookie_validation(self):
        for changes in (dict(domain='evil.example'),
                        dict(value='x' * 4097), dict(name='bad;name'),
                        dict(value='bad\nvalue'), dict(path='bad'),
                        dict(expires=True), dict(expires=float('nan'))):
            with self.subTest(changes=tuple(changes)), self.assertRaises(CookieRejected) as caught:
                narrow_cookies([cookie(**changes)], HOST)
            self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(len(narrow_cookies([cookie(), cookie()], HOST)), 1)
        with self.assertRaisesRegex(CookieRejected, '^Cookie 格式无效$'):
            narrow_cookies([None], HOST)
        self.assertEqual(narrow_cookies([cookie(expires=1)], HOST, now=2), [])
        narrowed = narrow_cookies([cookie(domain='.douyin.com', secure=False)], HOST)
        self.assertEqual(narrowed[0]['domain'], HOST)
        self.assertTrue(narrowed[0]['secure'])

    def test_cookie_specificity_stats_and_order(self):
        for domains in ((HOST, '.douyin.com'), ('.douyin.com', HOST),
                        ('.' + HOST, HOST), ('.douyin.com', '.' + HOST)):
            with self.subTest(domains=domains):
                stats = {}
                best = HOST if HOST in domains else '.' + HOST
                items = [cookie(domain=d, value='winner' if d == best else 'discarded')
                         for d in domains]
                result = narrow_cookies([items[0], cookie(name='other'), items[1],
                                         cookie(partitionKey='synthetic'), cookie(expires=1)],
                                        HOST, now=2, stats=stats)
                self.assertEqual([c['name'] for c in result], ['sessionid', 'other'])
                self.assertEqual(result[0]['value'], 'winner')
                self.assertNotIn('discarded', str(result))
                self.assertEqual(stats, dict(partitioned_skipped=1, expired_skipped=1,
                                            duplicates_resolved=1, accepted=2))
        self.assertEqual(narrow_cookies([cookie(value='first'), cookie(value='second')], HOST)[0]['value'], 'first')

    def test_run_diagnostic_logs(self):
        from unittest.mock import MagicMock
        for fail_goto in (True, False):
            with self.subTest(fail_goto=fail_goto):
                factory = MagicMock()
                browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
                context = browser.new_context.return_value
                page = context.new_page.return_value
                page.url = 'https://' + HOST + '/home'
                page.locator.return_value.inner_text.return_value = 'Home'
                page.locator.return_value.count.return_value = 0
                page.evaluate.return_value = 'Synthetic Chrome'
                context.cookies.return_value = [cookie(partitionKey='x'), cookie()]
                if fail_goto:
                    page.goto.side_effect = RuntimeError(SECRET)
                manager = _ephemeral_manager(browser_factory=factory, browser_mode='headless')
                session = QrLoginSession()
                with self.assertLogs('index_hub.qr_login', level='INFO') as logs:
                    manager._run(session)
                output = '\n'.join(logs.output)
                self.assertNotIn(SECRET, output)
                if fail_goto:
                    self.assertIn('QR login failed stage=goto error=RuntimeError reason=-', output)
                else:
                    self.assertEqual(session.snapshot()['state'], 'succeeded')
                finished = [line.split('QR login finished ')[1] for line in logs.output
                            if 'QR login finished ' in line]
                self.assertEqual(finished, [
                    'state=failed stage=goto accepted=0 partitioned_skipped=0 duplicates_resolved=0'
                    if fail_goto else
                    'state=succeeded stage=close accepted=1 partitioned_skipped=1 duplicates_resolved=0'])
                context.close.assert_called_once()
                browser.close.assert_called_once()
                manager.holder.clear()

    def test_holder_scope_and_metadata(self):
        clock = [10]
        holder = SessionHolder(lambda: clock[0])
        holder.put(narrow_cookies([cookie()], HOST), 'Synthetic UA')
        clock[0] = 14.9
        self.assertEqual(holder.info(), dict(has_session=True, age_seconds=4, headless_ua=False))
        self.assertNotIn(SECRET, repr(holder) + str(holder) + str(holder.info()))
        session, ua = holder.take_session()
        try:
            self.assertEqual(ua, 'Synthetic UA')
            for host, expected in ((HOST, True), ('sub.' + HOST, False), ('douyin.com', False)):
                request = requests.Request('GET', 'https://' + host + '/').prepare()
                self.assertEqual(bool(requests.cookies.get_cookie_header(session.cookies, request)), expected)
            self.assertFalse(next(iter(session.cookies)).domain_specified)
        finally:
            session.cookies.clear()
            session.close()
        holder.clear()
        self.assertFalse(holder.has_session())
        self.assertEqual(holder.info(), dict(has_session=False, age_seconds=None, headless_ua=False))

    def test_debug_rejected(self):
        for name in ('DEBUG', 'PWDEBUG'):
            with patch.dict(os.environ, {name: '1'}), self.assertRaises(QrUnsafeDebug):
                QrLoginManager().start()

    def test_busy_cancel_preserves_holder(self):
        entered, release = threading.Event(), threading.Event()
        def run(session):
            entered.set()
            release.wait(2)
            session._set('cancelled')
        manager = QrLoginManager(browser_mode='headless')
        manager.holder.put([cookie()], 'Synthetic UA')
        with patch.object(manager, '_run', run), patch.dict(os.environ, {}, clear=True):
            session = manager.start()
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaises(QrBusy):
                    manager.start()
                manager.cancel()
                self.assertTrue(manager.holder.has_session())
                self.assertNotIn(SECRET, str(manager.status()) + str(manager.holder_info()))
            finally:
                release.set()
                session.thread.join(2)

    def test_failure_preserves_holder_and_sanitizes_logs(self):
        def fail():
            raise RuntimeError(SECRET)
        manager = _ephemeral_manager(browser_factory=fail, browser_mode='headless')
        manager.holder.put([cookie()], 'Synthetic UA')
        with patch.dict(os.environ, {}, clear=True), self.assertLogs('index_hub.qr_login', level='WARNING') as logs:
            session = manager.start()
            session.thread.join(2)
        self.assertEqual(manager.status()['state'], 'failed')
        self.assertTrue(manager.holder.has_session())
        self.assertIsNone(manager._browser)
        self.assertFalse(manager.browser_open())
        self.assertNotIn(SECRET, str(logs.output))

    def test_injected_clock_timeout(self):
        from types import SimpleNamespace
        clock = [0]
        class Browser:
            connected = True
            def new_context(self, **kwargs):
                return self
            def new_page(self):
                return self
            def set_default_timeout(self, value):
                pass
            def goto(self, *args, **kwargs):
                clock[0] = 21
            def close(self):
                self.connected = False
            def is_connected(self):
                return self.connected
        browser = Browser()
        class Factory:
            def __enter__(self):
                return SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kw: browser))
            def __exit__(self, *args):
                pass
        manager = _ephemeral_manager(browser_factory=Factory, browser_mode='headless', clock=lambda: clock[0], timeout=20)
        manager.holder.put([cookie()], 'Synthetic UA')
        with patch.dict(os.environ, {}, clear=True):
            session = manager.start()
            session.thread.join(2)
        self.assertEqual(manager.status()['state'], 'timeout')
        self.assertTrue(manager.holder.has_session())
        self.assertFalse(manager.browser_open())
        self.assertIsNone(manager._browser)


def png(color):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 160, 160, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + bytes(color) * 160) * 160)) + chunk(b'IEND', b''))


HTML = '''<html><body><img class="qrcode" src="/mock/qr.png?v=0" width="160" height="160">
<input type="tel"><p>扫码登录</p><span>验证码登录</span><div id="status"></div>
<script>setInterval(async () => {
 const s = await (await fetch('/mock/state')).text();
 if(s === 'rotate') document.querySelector('img').src='/mock/qr.png?v=1';
 if(s === 'expired') document.querySelector('#status').textContent='二维码已失效';
 if(s === 'captcha') document.querySelector('#status').innerHTML='<div id="captcha_container">安全验证</div>';
 if(s === 'success') location='/creator-micro/home';
}, 300);</script></body></html>'''


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import asyncio
            from aiohttp import web
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            _browser_skip('Optional browser test dependency unavailable: ' + type(exc).__name__)
        cls.asyncio, cls.web = asyncio, web

    def setUp(self):
        self.state = 'waiting'
        self.ready = threading.Event()
        self.error = None
        self.loop = self.asyncio.new_event_loop()
        self.runner = None
        async def handler(request):
            path = request.path
            if path == '/creator-micro/login':
                return self.web.Response(text=HTML, content_type='text/html')
            if path == '/creator-micro/home':
                return self.web.Response(text='<html><body>Home</body></html>', content_type='text/html')
            if path == '/mock/set':
                self.state = request.query['state']
                return self.web.Response(text='ok')
            if path == '/mock/qr.png':
                return self.web.Response(body=png((255, 0, 0) if request.query.get('v') == '1' else (0, 0, 255)), content_type='image/png')
            response = self.web.Response(text=self.state)
            if self.state in ('anon', 'success'):
                response.set_cookie('ttwid' if self.state == 'anon' else 'sessionid',
                                    'ANON' if self.state == 'anon' else SECRET, path='/')
            return response
        async def boot():
            app = self.web.Application()
            app.router.add_route('*', '/{path:.*}', handler)
            self.runner = self.web.AppRunner(app)
            await self.runner.setup()
            site = self.web.TCPSite(self.runner, '127.0.0.1', 0)
            await site.start()
            self.port = site._server.sockets[0].getsockname()[1]
        def serve():
            self.asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(boot())
            except Exception as exc:
                self.error = exc
            finally:
                self.ready.set()
            if self.error is None:
                self.loop.run_forever()
            if self.runner:
                self.loop.run_until_complete(self.runner.cleanup())
            self.loop.close()
        self.server = threading.Thread(target=serve, daemon=True)
        self.server.start()
        self.addCleanup(self.stop_server)
        self.assertTrue(self.ready.wait(3))
        if self.error:
            _browser_skip('Loopback server unavailable: ' + type(self.error).__name__)
        self.origin = 'http://127.0.0.1:' + str(self.port)
        self.http = requests.Session()
        self.http.trust_env = False
        self.addCleanup(self.http.close)
        # Probe only local Chromium availability; never install or download it.
        from playwright.sync_api import sync_playwright
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, chromium_sandbox=False)
                browser.close()
        except Exception as exc:
            _browser_skip('Chromium cannot launch: ' + type(exc).__name__)
        self.profile_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.profile_temp.cleanup)
        self.manager = QrLoginManager(profile_dir=Path(self.profile_temp.name), allowed_origin=self.origin,
                                      login_url=self.origin + '/creator-micro/login',
                                      cookie_url=self.origin + '/creator-micro/home',
                                      allow_insecure=True, timeout=20, browser_mode='headless')
        self.output = io.StringIO()
        self.handler = logging.StreamHandler(self.output)
        self.log = logging.getLogger('index_hub.qr_login')
        self.log.addHandler(self.handler)
        self.addCleanup(self.finish)
        with patch.dict(os.environ, {}, clear=True):
            self.session = self.manager.start()

    def stop_server(self):
        if self.error is None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.server.join(3)

    def finish(self):
        try:
            self.manager.cancel()
            if hasattr(self, 'session'):
                self.session.thread.join(4)
                self.assertFalse(self.session.thread.is_alive())
            self.assertFalse(self.manager.browser_open())
            self.assertIsNone(self.manager._browser)
            self.assertNotIn(SECRET, self.output.getvalue())
        finally:
            self.log.removeHandler(self.handler)
            self.handler.close()
            self.manager.holder.clear()

    def wait_for(self, predicate, seconds=6):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.2)
        self.fail('Timed out waiting for synthetic state: ' + str(self.manager.status()))

    def state_is(self, state):
        self.wait_for(lambda: self.manager.status()['state'] == state)

    def set_state(self, state):
        response = self.http.post(self.origin + '/mock/set', params={'state': state}, timeout=2)
        response.raise_for_status()

    def test_rotation_anonymous_and_success(self):
        self.state_is('qr_ready')
        self.assertTrue(self.manager.qr_png().startswith(b'\x89PNG\r\n\x1a\n'))
        version = self.manager.status()['qr_version']
        self.set_state('rotate')
        self.wait_for(lambda: self.manager.status()['qr_version'] > version)
        self.set_state('anon')
        time.sleep(1.6)
        self.assertEqual(self.manager.status()['state'], 'qr_ready')
        self.assertFalse(self.manager.holder_info()['has_session'])
        self.set_state('success')
        self.state_is('succeeded')
        self.session.thread.join(2)
        self.assertFalse(self.manager.browser_open())
        self.assertTrue(self.manager.holder_info()['has_session'])
        session, _ = self.manager.holder.take_session()
        try:
            self.assertEqual(session.cookies.get('sessionid'), SECRET)
        finally:
            session.cookies.clear()
            session.close()
        self.assertNotIn(SECRET, str(self.manager.status()) + str(self.manager.holder_info()))

    def test_headed_real_ua(self):
        if not os.environ.get('DISPLAY'):
            _browser_skip('DISPLAY unavailable')
        self.manager.cancel()
        self.session.thread.join(4)
        self.profile_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.profile_temp.cleanup)
        self.manager = QrLoginManager(profile_dir=Path(self.profile_temp.name), allowed_origin=self.origin,
                                     login_url=self.origin + '/creator-micro/login',
                                     cookie_url=self.origin + '/creator-micro/home',
                                     allow_insecure=True, timeout=20, browser_mode='headed')
        with patch.dict(os.environ):
            os.environ.pop('DEBUG', None)
            os.environ.pop('PWDEBUG', None)
            self.session = self.manager.start()
        self.state_is('qr_ready')
        self.set_state('success')
        self.state_is('succeeded')
        self.session.thread.join(2)
        http, ua = self.manager.holder.take_session()
        try:
            self.assertNotIn('HeadlessChrome', ua)
            self.assertFalse(self.manager.holder.info()['headless_ua'])
        finally:
            http.cookies.clear()
            http.close()

    def test_expired(self):
        self.state_is('qr_ready')
        self.set_state('expired')
        self.state_is('expired')

    def test_captcha(self):
        self.state_is('qr_ready')
        self.set_state('captcha')
        self.state_is('verification_required')

    def test_cancel_preserves_session(self):
        self.state_is('qr_ready')
        self.manager.holder.put([cookie()], 'Synthetic UA')
        self.manager.cancel()
        self.state_is('cancelled')
        self.assertTrue(self.manager.holder.has_session())

    def test_sms_login_tab_is_not_verification(self):
        self.state_is('qr_ready')
        time.sleep(1.2)
        self.assertEqual(self.manager.status()['state'], 'qr_ready')


def _ephemeral_manager(**kwargs):
    manager = QrLoginManager(**kwargs)
    original = manager._run
    def run(session):
        with patch('qr_login.persist_enabled', return_value=False):
            original(session)
    manager._run = run
    return manager

if __name__ == '__main__':
    unittest.main()



class PersistentManagerTests(unittest.TestCase):
    def test_busy_profile_fails_at_launch(self):
        from unittest.mock import MagicMock
        from browser_profile import ProfileLock
        with tempfile.TemporaryDirectory() as directory, ProfileLock(directory):
            factory = MagicMock()
            manager = QrLoginManager(profile_dir=directory, browser_factory=factory, browser_mode='headless')
            session = QrLoginSession()
            with self.assertLogs('index_hub.qr_login', level='WARNING') as logs:
                manager._run(session)
            self.assertEqual(session.snapshot()['state'], 'failed')
            self.assertIn('QR login failed stage=launch error=ProfileBusy reason=profile_busy', '\n'.join(logs.output))
            factory.return_value.__enter__.return_value.chromium.launch_persistent_context.assert_not_called()
            self.assertFalse(manager.browser_open())

    def test_persistent_context_without_browser_closed_before_unlock(self):
        from unittest.mock import MagicMock
        from browser_profile import ProfileLock, ProfileBusy
        with tempfile.TemporaryDirectory() as directory:
            factory = MagicMock()
            chromium = factory.return_value.__enter__.return_value.chromium
            context = chromium.launch_persistent_context.return_value
            context.browser = None
            page = MagicMock()
            context.pages = [page]
            page.url = 'https://' + HOST + '/home'
            page.locator.return_value.inner_text.return_value = 'Home'
            page.locator.return_value.count.return_value = 0
            page.evaluate.return_value = 'Synthetic Chrome'
            context.cookies.return_value = [cookie()]
            manager = QrLoginManager(profile_dir=directory, browser_factory=factory, browser_mode='headless')
            def closing():
                self.assertTrue(manager.browser_open())
                with self.assertRaises(ProfileBusy):
                    ProfileLock(directory).acquire()
            context.close.side_effect = closing
            session = QrLoginSession()
            manager._run(session)
            self.assertEqual(session.snapshot()['state'], 'succeeded')
            self.assertFalse(manager.browser_open())
            context.close.assert_called_once()
            chromium.launch.assert_not_called()
            context.new_page.assert_not_called()
            chromium.launch_persistent_context.assert_called_once_with(directory, headless=True, chromium_sandbox=False, accept_downloads=False, locale='zh-CN')
            with ProfileLock(directory):
                pass
            manager.holder.clear()
