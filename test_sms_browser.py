"""Real Qt + aiohttp + Unix bridge SMS flow, entirely synthetic and loopback.

Opt in under Xvfb with INDEX_HUB_REQUIRE_BROWSER_TESTS=1. The test adapter
maps only this fixture's exact origin to the official origin for the strict
state/input guards; production URL/domain checks and selectors stay unchanged.
"""
import asyncio
import base64
import contextlib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, web

import hub_bridge
from login_driver import CREATOR_PAGE, QtLoginDriver
from sms_login import FRAME_PNG_LIMIT, LoginCoordinator, SmsLoginManager

PHONE, CODE, WRONG_CODE = '13800138000', '246810', '135790'


@unittest.skipUnless(os.environ.get('INDEX_HUB_REQUIRE_BROWSER_TESTS') == '1',
                     'Qt SMS tests require explicit Xvfb opt-in')
class SmsBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        from slider_solver import LocalGapSolver
        cls.solver = LocalGapSolver()
        if cls.solver._load() is None:
            if os.environ.get('INDEX_HUB_REQUIRE_SOLVER_TESTS') == '1':
                raise AssertionError('optional solver/model required but unavailable')
            raise unittest.SkipTest('optional solver/model unavailable')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sms-browser-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, INDEX_HUB_LOGIN_MODE='sms',
                              INDEX_HUB_STATE_DIR=str(self.root/'state'),
                              INDEX_HUB_LOG_DIR=str(self.root/'logs'))
        self.env.start()
        self.addCleanup(self.env.stop)
        from hub_logging import configure_logging
        root_logger = logging.getLogger()
        old_handlers, old_level = list(root_logger.handlers), root_logger.level
        configure_logging('sms-browser-fixture', self.root)
        self.log_handlers = [h for h in root_logger.handlers if h not in old_handlers]
        def close_logs():
            for handler in self.log_handlers:
                root_logger.removeHandler(handler)
                handler.close()
            root_logger.setLevel(old_level)
        self.addCleanup(close_logs)
        # Capture native Qt/Chromium output too, before starting the view.
        # Only these test-owned logs are scanned; no service state is opened.
        self.native_log = (self.root/'logs'/'native.log').open('w+b')
        sys.stdout.flush()
        sys.stderr.flush()
        saved_fds = [os.dup(fd) for fd in (1, 2)]
        for fd in (1, 2):
            os.dup2(self.native_log.fileno(), fd)
        def restore_output():
            sys.stdout.flush()
            sys.stderr.flush()
            for fd, saved in zip((1, 2), saved_fds):
                os.dup2(saved, fd)
                os.close(saved)
            self.native_log.close()
        self.addCleanup(restore_output)
        self.addCleanup(self.scan_artifacts)  # Also catch writes during browser/server shutdown.
        self.responses = []
        self.loop = asyncio.new_event_loop()
        self.server_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.close_server)
        self.runners = []
        self.http = None
        self.await_future(asyncio.run_coroutine_threadsafe(self.open_server(), self.loop))

        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineUrlRequestInterceptor
        from PySide6.QtWebEngineWidgets import QWebEngineView
        class LoopbackOnly(QWebEngineUrlRequestInterceptor):
            def interceptRequest(self, info):
                url = info.requestUrl()
                if not (url.scheme() in ('data', 'about') or
                        (url.scheme() == 'http' and url.host() == '127.0.0.1')):
                    info.block(True)
        self.profile = QWebEngineProfile()  # Off the record; no credential files.
        self.assertTrue(self.profile.isOffTheRecord())
        self.profile.setPersistentStoragePath(str(self.root/'browser'))
        self.profile.setCachePath(str(self.root/'cache'))
        self.interceptor = LoopbackOnly(self.profile)
        self.profile.setUrlRequestInterceptor(self.interceptor)
        self.view = QWebEngineView()
        self.page = QWebEnginePage(self.profile, self.view)
        self.view.setPage(self.page)
        self.view.resize(720, 640)
        self.view.show()
        self.addCleanup(self.close_view)
        self.session_cookie = threading.Event()
        def cookie_added(cookie):
            if bytes(cookie.name()) == b'sessionid' and cookie.domain() == '127.0.0.1':
                self.session_cookie.set()  # No value leaves the browser's memory.
        self.profile.cookieStore().cookieAdded.connect(cookie_added)
        origin = self.origin
        self.public_login = False
        self.creator_navigations = 0
        fixture = self
        def mapped_url(url):
            if url.startswith(origin+'/'):
                host = ('www.douyin.com' if fixture.public_login and not url.startswith(origin+'/creator')
                        else 'creator.douyin.com')
                return 'https://'+host+url[len(origin):]
            return url
        class FixtureDriver(QtLoginDriver):
            def _trusted_origin(self, url):
                return url.startswith(origin+'/')

            def probe(self):
                value = super().probe()
                # Test-only origin adapter, never enabled by an env var in production.
                value['url'] = mapped_url(value['url'])
                return value

            def _navigate(self, target=None, *, cancel=None):
                if target == CREATOR_PAGE:
                    fixture.creator_navigations += 1
                    target = origin+'/creator'
                return super()._navigate(target, cancel=cancel)
        self.driver = FixtureDriver(self.view, session_present=self.session_cookie.is_set)
        self.manager = SmsLoginManager(self.driver, threading.Lock(), coordinator=LoginCoordinator(), solver=self.solver)
        self.addCleanup(self.stop_manager)
        # Run the production collection-status guard over a narrowly mapped
        # fixture page, including its creator-only origin requirement.
        page_adapter = SimpleNamespace(
            get_current_url=lambda: self.driver.dispatcher.call(lambda: mapped_url(self.view.url().toString())),
            evaluate_js=lambda script: self.driver._read_json(script, 'fixture_js_failed'))
        api = SimpleNamespace(_sms_manager=lambda: self.manager, _page=page_adapter,
            _login_state=lambda: 'cookie_present' if self.session_cookie.is_set() else 'no_session_cookie')
        self.bridge = hub_bridge.start_bridge(api)
        self.addCleanup(self.close_bridge)

    async def open_server(self):
        from fake_sms_page import create_fake_app
        from desktop_gateway import create_app, HUB_KEY
        async def serve(app):
            runner = web.AppRunner(app, access_log=logging.getLogger('sms.fixture.http'))
            self.runners.append(runner)
            await runner.setup()
            site = web.TCPSite(runner, '127.0.0.1', 0)
            await site.start()
            return 'http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])
        self.navigation_gate, self.creator_gate = asyncio.Event(), asyncio.Event()
        self.navigation_gate.set()
        self.creator_gate.set()
        self.origin = await serve(create_fake_app(navigation_gate=self.navigation_gate, creator_gate=self.creator_gate))
        def bridge(command, payload, timeout):
            return hub_bridge.call_bridge(command, payload, timeout)
        # Supply synthetic admin credentials in memory; never open a password file.
        original_read = Path.read_text
        def fixture_read(path, *args, **kwargs):
            if path == self.root/'state'/'access.json':
                return json.dumps(dict(username='fixture', password='synthetic-admin-only'))
            return original_read(path, *args, **kwargs)
        with patch.object(Path, 'read_text', fixture_read):
            gateway = create_app(self.root, bridge=bridge, desktop_dir=self.root)
        self.hub = gateway[HUB_KEY]
        self.gateway = await serve(gateway)
        self.hub.worker_task.cancel()  # No collection in a login fixture.
        with contextlib.suppress(asyncio.CancelledError):
            await self.hub.worker_task
        auth = base64.b64encode(b'fixture:synthetic-admin-only').decode()
        self.http = ClientSession(headers={'Authorization': 'Basic '+auth, 'X-Hub-Request': '1'}, trust_env=False)

    def close_server(self):
        async def close():
            self.navigation_gate.set()
            self.creator_gate.set()
            if self.http:
                await self.http.close()
            for runner in reversed(self.runners):
                await runner.cleanup()
        self.await_future(asyncio.run_coroutine_threadsafe(close(), self.loop))
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.server_thread.join(5)
        self.loop.close()
        self.assertFalse(self.server_thread.is_alive())

    def close_bridge(self):
        self.bridge.shutdown()
        self.bridge.server_close()

    def close_view(self):
        from PySide6.QtCore import QCoreApplication, QEvent
        self.view.close()
        self.page.deleteLater()
        self.view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.profile.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def stop_manager(self):
        self.manager.cancel()
        session = self.manager._session
        if session and session.thread:
            self.pump_until(lambda: not session.thread.is_alive(), 6)
            session.thread.join(.1)
            self.assertIsNone(session.phone)
            self.assertIsNone(session.lease)

    def pump_until(self, predicate, timeout=30):
        deadline = time.monotonic()+timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.003)
        self.assertTrue(predicate(), 'Qt fixture deadline exceeded')

    def await_future(self, future, timeout=30):
        self.pump_until(future.done, timeout)
        return future.result()

    def work(self, function):
        async def run():
            return await asyncio.to_thread(function)
        return self.await_future(asyncio.run_coroutine_threadsafe(run(), self.loop))

    def js(self, script):
        return self.work(lambda: self.driver._read_json(script, 'fixture_js_failed'))

    def load(self, query='', path='/login'):
        from PySide6.QtCore import QUrl
        loaded = []
        self.view.loadFinished.connect(loaded.append)
        try:
            self.view.load(QUrl(self.origin+path+query))
            self.pump_until(lambda: bool(loaded))
            self.assertEqual(loaded, [True])
            self.pump_until(lambda: not self.page.contentsSize().isEmpty())
        finally:
            self.view.loadFinished.disconnect(loaded.append)

    def request(self, method, path='', body=None, expected=200):
        return self.await_future(self.begin_request(method, path, body, expected))

    def begin_request(self, method, path='', body=None, expected=200):
        async def run():
            async with self.http.request(method, self.gateway+'/api/v1/'+path, json=body) as response:
                raw = await response.read()
                self.responses.append(raw + json.dumps(dict(response.headers)).encode())
                self.assertEqual(response.status, expected, 'unexpected fixture HTTP status')
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assert_private(raw)
                return raw if response.content_type == 'image/png' else json.loads(raw)
        return asyncio.run_coroutine_threadsafe(run(), self.loop)

    def post(self, action, body, expected=200):
        return self.request('POST', 'login/sms/'+action, body, expected)

    def status(self):
        return self.request('GET', 'login/sms')

    def wait_state(self, state, mode=None):
        self.pump_until(lambda: self.manager.status()['state'] == state and
                        (mode is None or self.manager.status()['verification_mode'] == mode))
        result = self.status()
        self.assertEqual(result['state'], state)
        if mode:
            self.assertEqual(result['verification_mode'], mode)
        return result

    def start(self, query='', *, manual=False):
        if manual:
            self.manager.solver = None
        self.load(query)
        self.assertTrue(self.js('document.getElementById("login-full-panel-synthetic").hidden'))
        self.post('start', {'phone': PHONE})

    def manual_drag(self, distance=190):
        # A preceding rejected gesture can move/reset the handle between probes.
        # Wait for the worker's challenge geometry and matching screenshot.
        handle = self.work(lambda: self.driver.element_rect('slider_handle'))
        self.pump_until(lambda: self.manager._session.challenge.get('rects', {}).get('slider_handle') == handle)
        self.pump_until(lambda: self.manager.status()['frame_id'] > 0)
        status = self.status()
        png = self.request('GET', 'login/sms/frame?v='+str(status['frame_id']))
        self.assertTrue(png.startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertLessEqual(len(png), FRAME_PNG_LIMIT)
        # Human gesture fixture: independently known target, no solver/model.
        # Map from the screenshot's crop, as the phone UI does after display scaling.
        meta = self.manager._session.frame.metadata
        from PySide6.QtGui import QImage
        image = QImage.fromData(png)
        self.assertEqual((image.width(), image.height()), (meta.width, meta.height))
        self.assertEqual(meta.image_scale, 1)
        self.assertAlmostEqual(meta.width, meta.logical_width * meta.dpr, delta=1)
        self.assertAlmostEqual(meta.height, meta.logical_height * meta.dpr, delta=1)
        x, y = handle['x']+handle['width']/2, handle['y']+handle['height']/2
        points = [[(x+distance*i/40-meta.x0)*meta.x_scale/meta.width,
                   (y-meta.y0)*meta.y_scale/meta.height, i*25] for i in range(41)]
        self.post('pointer', {'frame': status['frame_id'], 'kind': 'drag', 'points': points})

    def assert_trusted(self, drags):
        audit = self.js('window.audit')
        self.assertEqual(len(audit['drags']), drags)
        self.assertTrue(audit['inputTrusted'])
        self.assertTrue(all(audit['inputTrusted']))
        for drag in audit['drags']:
            self.assertTrue(drag['trusted'])
            self.assertTrue(drag['smooth'])
            self.assertTrue(drag['passed'])
            self.assertAlmostEqual(drag['dx'], 190, delta=5)
        return audit

    def assert_private(self, contents):
        for secret in (PHONE, CODE, WRONG_CODE):
            for encoding in ('utf-8', 'utf-16-le', 'utf-16-be'):
                self.assertFalse(secret.encode(encoding) in contents, 'synthetic input leaked')

    def scan_artifacts(self):
        for handler in self.log_handlers:
            handler.flush()
        self.native_log.flush()
        files = [path for path in self.root.rglob('*') if path.is_file()]
        self.assertTrue(any(path.suffix == '.log' for path in files))
        self.assertTrue(any(path.suffix == '.sqlite3' for path in files))
        for path in files:  # Includes SQLite/WAL, logs and any Qt temp files.
            self.assert_private(path.read_bytes())
        for response in self.responses:
            self.assert_private(response)

    def test_auto_flow_and_complete_leak_scan(self):
        self.start('?second=1')
        sent = self.wait_state('code_sent')
        self.assertEqual(sent['phone'], '138****8000')
        self.assertGreater(sent['resend_in'], 50)  # Fixture countdown is only 4s.
        self.post('code', {'code': CODE})
        self.wait_state('succeeded')
        self.assertTrue(self.session_cookie.is_set())
        self.assertEqual(self.manager._session.auto_submissions, 2)
        audit = self.assert_trusted(2)
        self.assertEqual((audit['opens'], audit['tabs'], audit['sends'], audit['codeSubmits']), (1, 0, 1, 1))
        self.assertTrue(self.js('document.querySelector("input[name=web-login-area-code-input]").value === "+86"'))
        self.assertEqual(self.js('localStorage.length + sessionStorage.length'), 0)
        self.stop_manager()
        self.assertFalse(self.manager._session.actions)
        for path in ('login/sms', 'status', 'jobs'):
            self.request('GET', path)
        async def database():
            self.assertEqual(self.hub.store.get('paused'), {'code': 'manual_resume_required'})
            return '\n'.join(self.hub.store.db.iterdump()).encode()
        dump = self.await_future(asyncio.run_coroutine_threadsafe(database(), self.loop))
        self.assert_private(dump)
        self.scan_artifacts()
        self.assertTrue(any(b'138****8000' in response for response in self.responses))

    def test_public_login_returns_to_creator_before_success_and_manual_resume(self):
        self.public_login = True
        self.loop.call_soon_threadsafe(self.creator_gate.clear)
        self.start('?stay_public=1', manual=True)
        self.wait_state('verify_send', 'manual')
        self.manual_drag()
        self.wait_state('code_sent')
        self.post('code', {'code': CODE})
        self.pump_until(lambda: self.creator_navigations == 1)
        self.assertTrue(self.session_cookie.is_set())
        # The return navigation is deliberately held open. No success callback,
        # released browser lease, or resumable collection is allowed yet.
        self.assertEqual(self.status()['state'], 'submitting')
        self.assertTrue(self.manager._fetch_lock.locked())
        self.assertEqual(self.request('POST', 'collection/resume', {}, expected=409)['error']['code'], 'login_in_progress')
        self.loop.call_soon_threadsafe(self.creator_gate.set)
        self.wait_state('succeeded')
        self.stop_manager()
        self.assertEqual(self.view.url().toString(), self.origin+'/creator')
        status = self.work(lambda: hub_bridge.call_bridge('status'))
        self.assertEqual(status['state'], 'browser_ready')
        self.assertEqual(self.request('GET', 'status')['paused'], {'code': 'manual_resume_required'})
        self.assertTrue(self.request('POST', 'collection/resume', {})['ok'])
        self.assertIsNone(self.request('GET', 'status')['paused'])
        self.assertEqual(self.creator_navigations, 1)
        self.assertEqual(len(self.manager._send_times), 1)
        self.scan_artifacts()

    def test_header_navigation_visible_form_waits_for_load_finished_before_send(self):
        self.loop.call_soon_threadsafe(self.navigation_gate.clear)
        self.start('?slow_load=1', manual=True)
        self.pump_until(lambda: self.view.url().path() == '/jingxuan')
        self.pump_until(lambda: self.js('Boolean(window.audit && !el("dialog").hidden)'))
        probe = self.work(self.driver.probe)
        self.assertTrue(probe['found']['phone'])
        self.assertTrue(probe['found']['send'])
        self.assertFalse(probe['navigation_ready'])
        self.assertTrue(self.driver._loading)
        self.assertTrue(self.page.isLoading())
        # Wait for the real worker to consume an incomplete-form observation.
        self.pump_until(lambda: self.manager._session.navigation_started is not None)
        self.assertEqual(self.status()['state'], 'opening')
        self.assertEqual(len(self.manager._send_times), 0)
        self.assertEqual(self.manager._day_count, 0)
        self.assertFalse(self.manager._session.nonce)
        audit = self.js('window.audit')
        self.assertEqual((audit['inputTrusted'], audit['sends'], audit['opens']), ([], 0, 1))
        self.loop.call_soon_threadsafe(self.navigation_gate.set)
        self.wait_state('verify_send', 'manual')
        self.assertFalse(self.driver._loading)
        self.assertEqual(len(self.manager._send_times), 1)
        self.assertTrue(all(self.js('window.audit.inputTrusted')))
        self.manual_drag()
        self.wait_state('code_sent')
        self.post('code', {'code': CODE})
        self.wait_state('succeeded')
        audit = self.assert_trusted(1)
        self.assertEqual((audit['opens'], audit['sends'], audit['codeSubmits']), (1, 1, 1))

    def test_calibrated_dialog_controls_and_ordered_fallbacks(self):
        from qr_login import SELECTORS
        self.load(path='/jingxuan')
        probe = self.work(self.driver.probe)
        for key in ('phone', 'code', 'send', 'submit', 'login_form', 'sms_pane', 'qr_pane', 'qr_image'):
            self.assertTrue(probe['found'][key], key)
        self.assertFalse(probe['verification'])  # Hidden verifycenter helper is not a challenge.
        self.assertFalse(probe['enabled']['send'])
        self.assertFalse(probe['enabled']['submit'])
        self.assertIsNone(self.work(lambda: self.driver.element_rect('submit')))
        self.assertTrue(self.js('document.querySelector("#login-full-panel-synthetic input[type=checkbox]") === null'))
        self.js('(document.querySelector("img[aria-label=二维码]").removeAttribute("src"), true)')
        self.assertTrue(self.js('document.querySelector('+json.dumps(SELECTORS['qr_image'])+') !== null'))
        # IDs take precedence over earlier placeholder matches; then name and
        # placeholder remain usable if a later public-page revision removes IDs.
        self.js('''(() => {const decoy=document.createElement('input');
          decoy.placeholder='请输入手机号'; document.body.prepend(decoy); return true;})()''')
        phone = self.work(lambda: self.driver.element_rect('phone'))
        expected = self.js('''(() => {const r=document.getElementById('normal-input').getBoundingClientRect();
          return {x:r.x,y:r.y,width:r.width,height:r.height};})()''')
        self.assertEqual(phone, expected)
        self.js('(document.body.firstElementChild.remove(), true)')
        for attribute in ('id', 'name'):
            self.js('''(() => {for (const e of document.querySelectorAll('input[placeholder]'))
              e.removeAttribute(%s); return true;})()''' % json.dumps(attribute))
            current = self.work(self.driver.probe)
            self.assertTrue(current['found']['phone'])
            self.assertTrue(current['found']['code'])
            self.assertNotEqual(current['rects']['phone'], current['rects']['code'])
        self.js('''(() => {for (const e of document.querySelectorAll('#login-full-panel-synthetic [id]'))
          e.removeAttribute('id'); return true;})()''')
        self.assertTrue(self.work(self.driver.probe)['login_form'])

    def test_disabled_span_and_div_controls_never_yield_click_rectangles(self):
        self.load(path='/jingxuan')
        for key, text in (('phone', PHONE), ('code', CODE)):
            rect = self.work(lambda: self.driver.element_rect(key))
            self.work(lambda: self.driver.click(rect['x']+10, rect['y']+10))
            self.work(lambda: self.driver.type_text(text))
        for key, selector in (('send', '#douyin_login_comp_button_input_id'),
                              ('submit', '#douyin_login_comp_btn_id')):
            for attribute, value in (('disabled', ''), ('aria-disabled', 'true'), ('data-disabled', 'true'),
                                     ('class', 'control-disabled-synthetic'), ('style', 'pointer-events:none')):
                with self.subTest(key=key, attribute=attribute):
                    self.js('(document.querySelector(%s).setAttribute(%s,%s), true)' % tuple(
                        json.dumps(v) for v in (selector, attribute, value)))
                    self.assertFalse(self.work(self.driver.probe)['enabled'][key])
                    self.assertIsNone(self.work(lambda: self.driver.element_rect(key)))
                    self.js('(document.querySelector(%s).removeAttribute(%s), true)' % (
                        json.dumps(selector), json.dumps(attribute)))
                    self.assertTrue(self.work(self.driver.probe)['enabled'][key])
                    self.assertIsNotNone(self.work(lambda: self.driver.element_rect(key)))
        self.assertEqual(self.js('window.audit.codeSubmits + window.audit.sends'), 0)

    def test_visible_verifycenter_overlay_and_iframe_detection(self):
        self.load(path='/jingxuan')
        for markup in ('<div class="rc-verifycenter" style="width:320px;height:200px"></div>',
                       '<iframe src="about:blank#verify" style="width:320px;height:200px"></iframe>'):
            with self.subTest(markup=markup):
                self.js('''(() => {const host=document.createElement('div'); host.id='synthetic-verify';
                  host.hidden=true; host.innerHTML=%s; document.body.append(host); return true;})()''' % json.dumps(markup))
                self.assertFalse(self.work(self.driver.probe)['verification'])
                self.js('document.getElementById("synthetic-verify").hidden=false')
                probe = self.work(self.driver.probe)
                self.assertTrue(probe['verification'])
                self.assertIsNotNone(probe['rects']['verification'])
                self.assertEqual(probe['challenge_kind'], 'unknown')
                self.js('document.getElementById("synthetic-verify").style.opacity="0"')
                self.assertFalse(self.work(self.driver.probe)['verification'])
                self.js('(document.getElementById("synthetic-verify").remove(), true)')

    def test_password_pane_switches_to_sms_once(self):
        self.start('?password=1', manual=True)
        self.wait_state('verify_send', 'manual')
        self.assertEqual(self.js('window.audit.tabs'), 1)
        self.manual_drag()
        self.wait_state('code_sent')
        self.post('code', {'code': CODE})
        self.wait_state('succeeded')
        self.assertEqual(self.js('window.audit.tabs'), 1)

    def test_production_manager_rejects_matching_nonofficial_page(self):
        self.load()
        self.js("(() => {document.getElementById('sms-tab').click(); return true;})()")
        self.manager.driver = QtLoginDriver(self.view)
        self.post('start', {'phone': PHONE})
        self.wait_state('failed')
        audit = self.js('window.audit')
        self.assertEqual(audit['inputTrusted'], [])
        self.assertEqual((audit['sends'], audit['codeSubmits']), (0, 0))
        self.assertTrue(self.js("document.getElementById('normal-input').value === ''"))
        self.assertIsNone(self.manager._session.phone)

    def test_navigation_during_phone_input_aborts_round(self):
        self.load(path='/jingxuan')
        self.js("""(() => {document.getElementById('normal-input').addEventListener('input',
            () => { location.href = 'about:blank'; }, {once:true}); return true;})()""")
        self.post('start', {'phone': PHONE})
        self.wait_state('failed')
        self.assertIsNone(self.manager._session.phone)
        self.assertTrue(self.manager._session.cancelled.is_set())
        self.assertEqual(self.view.url().toString(), 'about:blank')

    def test_post_captcha_expired_code_restores_code_entry(self):
        self.start('?second=1&post_code_error=1', manual=True)
        self.wait_state('verify_send', 'manual')
        self.manual_drag()
        self.wait_state('code_sent')
        self.post('code', {'code': CODE})
        self.wait_state('verify_login', 'manual')
        self.manual_drag()
        status = self.wait_state('code_invalid')
        self.assertEqual(status['attempts_left'], 2)
        self.assertEqual(status['frame_id'], 0)
        self.assertIsNone(status['verification_mode'])
        self.assertFalse(self.manager._session.code_consumed)
        self.post('code', {'code': CODE}, 409)
        self.post('resend', {}, 429)
        self.assert_trusted(2)

    def test_cancelled_verification_restarts_on_clean_page_and_code_is_usable(self):
        self.start(manual=True)
        self.wait_state('verify_send', 'manual')
        old_nonce = self.manager._session.nonce
        self.assertTrue(old_nonce)
        self.assertEqual(self.js('window.audit.sends'), 0)
        self.js('window.previousRound = true')
        self.post('cancel', {})
        self.stop_manager()
        # Advance the manager's monotonic clock past both guards, retaining
        # the actual browser/page and all send accounting from the first round.
        self.manager._clock = lambda: time.monotonic()+61
        self.post('start', {'phone': PHONE})
        self.wait_state('verify_send', 'manual')
        self.assertTrue(self.js('window.previousRound !== true'))
        self.assertTrue(self.manager._session.nonce)
        self.assertNotEqual(self.manager._session.nonce, old_nonce)
        self.assertEqual(self.manager._day_count, 2)
        self.assertEqual(len(self.manager._send_times), 2)
        self.manual_drag()
        sent = self.wait_state('code_sent')
        self.assertEqual(sent['attempts_left'], 3)
        self.assertGreater(sent['resend_in'], 50)
        self.post('code', {'code': CODE})
        self.wait_state('succeeded')
        audit = self.assert_trusted(1)
        self.assertEqual((audit['tabs'], audit['sends'], audit['codeSubmits']), (0, 1, 1))

    def test_successive_manual_puzzles_remain_operable_in_both_phases(self):
        self.start('?second=1&send_challenges=2&login_challenges=2', manual=True)
        for phase in ('verify_send', 'verify_login'):
            with self.subTest(phase=phase):
                self.wait_state(phase, 'manual')
                session = self.manager._session
                identity = session.challenge['identity']
                self.manual_drag()
                until = time.monotonic()+3.2
                self.pump_until(lambda: session.challenge['identity'] != identity)
                self.pump_until(lambda: time.monotonic() >= until)
                status = self.status()
                self.assertEqual(status['state'], phase)
                self.assertEqual(status['verification_mode'], 'manual')
                self.assertGreater(status['frame_id'], 0)
                self.assertIsNone(session.manual_pending)
                self.assertFalse(session.desktop_requested)
                self.manual_drag()
                if phase == 'verify_send':
                    self.wait_state('code_sent')
                    self.post('code', {'code': CODE})
                else:
                    self.wait_state('succeeded')
        self.assert_trusted(4)
        self.assertEqual(self.manager._session.auto_submissions, 0)

    def test_long_drag_keeps_status_cancel_and_worker_responsive(self):
        self.start(manual=True)
        self.wait_state('verify_send', 'manual')
        self.pump_until(lambda: self.manager.status()['frame_id'] > 0)
        handle = self.work(lambda: self.driver.element_rect('slider_handle'))
        meta = self.manager._session.frame.metadata
        x, y = handle['x']+handle['width']/2, handle['y']+handle['height']/2
        points = [[(x+190*i/40-meta.x0)*meta.x_scale/meta.width,
                   (y-meta.y0)*meta.y_scale/meta.height, i*200] for i in range(41)]
        pending = self.begin_request('POST', 'login/sms/pointer',
                                    {'frame': self.manager.status()['frame_id'], 'kind': 'drag', 'points': points}, 409)
        self.pump_until(lambda: self.manager._session.pointer_pending)
        async def start_worker():
            self.hub.worker_task = asyncio.create_task(self.hub.worker())
        self.await_future(asyncio.run_coroutine_threadsafe(start_worker(), self.loop))
        # Observe several real worker polling cycles during an eight-second drag.
        until = time.monotonic()+3.2
        self.pump_until(lambda: time.monotonic() >= until)
        self.assertFalse(pending.done())
        self.assertTrue(self.manager._fetch_lock.locked())
        started = time.monotonic()
        self.assertEqual(self.status()['state'], 'verify_send')
        self.assertLess(time.monotonic()-started, 2)
        self.assertIsNone(self.request('GET', 'status')['worker_error'])
        started = time.monotonic()
        self.assertEqual(self.post('cancel', {})['state'], 'cancelled')
        self.assertLess(time.monotonic()-started, 2)
        result = self.await_future(pending, 2)
        self.assertEqual(result['error']['code'], 'cancelled')
        self.stop_manager()
        self.assertFalse(self.manager._fetch_lock.locked())
        self.assertIsNone(self.request('GET', 'status')['worker_error'])

    def test_manual_drag_wrong_code_then_success(self):
        self.start(manual=True)
        self.wait_state('verify_send', 'manual')
        # JS-created events must fail even with a smooth correct-offset sequence.
        self.js('''(() => {const h=document.querySelector('.secsdk-captcha-drag-icon');
          h.dispatchEvent(new PointerEvent('pointerdown',{clientX:110,clientY:472,pointerId:1}));
          for(let i=1;i<=40;i++) setTimeout(() => h.dispatchEvent(new PointerEvent('pointermove',
            {clientX:110+190*i/40,clientY:472,pointerId:1})), i*25);
          setTimeout(() => h.dispatchEvent(new PointerEvent('pointerup',
            {clientX:300,clientY:472,pointerId:1})), 1020);
          return true;})()''')
        until = time.monotonic()+1.2
        self.pump_until(lambda: time.monotonic() >= until)
        audit = self.js('window.audit')
        self.assertFalse(audit['drags'][0]['trusted'])
        self.assertTrue(audit['drags'][0]['smooth'])
        self.assertFalse(audit['drags'][0]['passed'])
        self.js('window.audit.drags = []')
        self.manual_drag()
        self.wait_state('code_sent')
        # The full-view fallback must mask both fields, even while filled.
        code_rect = self.work(lambda: self.driver.element_rect('code'))
        self.work(lambda: self.driver.click(code_rect['x']+10, code_rect['y']+10))
        self.work(lambda: self.driver.type_text(CODE))
        capture = self.work(self.driver.grab)
        from PySide6.QtGui import QImage
        image = QImage.fromData(capture.png)
        for field in ('phone', 'code'):
            rect = self.work(lambda: self.driver.element_rect(field))
            pixel = image.pixelColor(int((rect['x']+10)*capture.metadata.x_scale),
                                     int((rect['y']+10)*capture.metadata.y_scale))
            self.assertEqual(pixel.name(), '#202020')
        self.post('code', {'code': WRONG_CODE})
        self.wait_state('code_invalid')
        self.post('code', {'code': WRONG_CODE}, 409)
        self.post('code', {'code': CODE})
        self.wait_state('succeeded')
        self.assert_trusted(1)
        self.assertEqual(self.manager._session.auto_submissions, 0)

    def test_noisy_capture_keeps_pixels_and_fractional_crop_coordinates(self):
        from PySide6.QtGui import QImage
        self.load()
        self.view.setZoomFactor(1.25)
        self.js('''(() => { const c=document.createElement('canvas'); c.width=600; c.height=400;
          c.style.cssText='position:fixed;left:0;top:0;z-index:9999';
          const ctx=c.getContext('2d'), data=ctx.createImageData(c.width,c.height);
          for(let i=0;i<data.data.length;i+=4){data.data[i]=Math.random()*256;
            data.data[i+1]=Math.random()*256;data.data[i+2]=Math.random()*256;data.data[i+3]=255}
          ctx.putImageData(data,0,0);document.body.appendChild(c);return true;})()''')
        deadline = time.monotonic() + 5
        while True:
            capture = self.work(lambda: self.driver.grab(dict(x=10.2, y=20.3, width=400.2, height=300.3)))
            if len(capture.png) > 225*1024 or time.monotonic() >= deadline:
                break
        meta = capture.metadata
        image = QImage.fromData(capture.png)
        self.assertGreater(len(capture.png), 225*1024)
        self.assertEqual(meta.image_scale, 1)
        self.assertEqual((meta.logical_width, meta.logical_height), (501, 376))
        self.assertEqual((meta.x0, meta.y0, meta.zoom), (12/1.25, 25/1.25, 1.25))
        self.assertEqual((image.width(), image.height()), (meta.width, meta.height))
        self.assertAlmostEqual(meta.width, meta.logical_width * meta.dpr, delta=1)
        self.assertAlmostEqual(meta.height, meta.logical_height * meta.dpr, delta=1)
        self.assertEqual(meta.css_point(0, 0), (meta.x0, meta.y0))
        self.assertAlmostEqual(meta.css_point(1, 1)[0], meta.x0 + meta.logical_width / meta.zoom)
        self.assertAlmostEqual(meta.css_point(1, 1)[1], meta.y0 + meta.logical_height / meta.zoom)

    def test_three_automatic_submissions_across_phases_then_manual(self):
        self.start('?second=1&login_challenges=3')
        self.wait_state('code_sent')
        self.post('code', {'code': CODE})
        self.wait_state('verify_login', 'manual')
        self.assertEqual(self.manager._session.auto_submissions, 3)
        self.assert_trusted(3)
        # A fourth stable challenge remains manual over another probe cycle.
        until = time.monotonic()+1.2
        self.pump_until(lambda: time.monotonic() >= until)
        self.assertEqual(self.manager._session.auto_submissions, 3)
        self.manual_drag()
        self.wait_state('succeeded')
        self.assert_trusted(4)

    def test_rotation_goes_directly_to_remote_desktop(self):
        self.start('?rotation=1')
        self.wait_state('verify_send', 'desktop')
        self.assertEqual(self.manager._session.auto_submissions, 0)
        self.assertEqual(self.js('window.audit.drags'), [])
        self.post('pointer', {'frame': 1, 'kind': 'tap', 'points': [[.5, .5, 0]]}, 409)
        self.post('cancel', {})
        self.wait_state('cancelled')

    def test_platform_send_limit_stops_without_verification(self):
        self.start('?frequent=1')
        self.wait_state('rate_limited')
        self.assertEqual(self.js('window.audit.sends'), 0)
        self.assertEqual(self.js('window.audit.drags'), [])
        self.assertEqual(self.manager._session.auto_submissions, 0)

    def test_failed_manual_drag_falls_back_to_desktop(self):
        self.start(manual=True)
        self.wait_state('verify_send', 'manual')
        self.manual_drag(distance=70)
        self.wait_state('verify_send', 'desktop')
        audit = self.js('window.audit')
        self.assertTrue(audit['drags'][0]['trusted'])
        self.assertTrue(audit['drags'][0]['smooth'])
        self.assertFalse(audit['drags'][0]['passed'])
        self.assertEqual(audit['sends'], 0)


if __name__ == '__main__':
    unittest.main()
