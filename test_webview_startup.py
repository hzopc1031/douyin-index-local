import configparser
import http.server
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import webview_watchdog as wd
from webview_shutdown import QtShutdown


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        self.signals = patch('webview_shutdown.signal.signal').start()
        self.addCleanup(patch.stopall)
        self.qt = Mock()
        patch.dict(sys.modules, {'webview.platforms.qt': self.qt}).start()
        self.release, self.exit_fn, self.timer = Mock(), Mock(), Mock()
        self.shutdown = QtShutdown(self.release, exit_fn=self.exit_fn, timer_factory=self.timer)

    def test_sigterm_queues_gui_quit_with_nonrenewable_deadline(self):
        self.shutdown._request(signal.SIGTERM, None)
        self.shutdown._request(signal.SIGTERM, None)
        self.timer.assert_called_once_with(5, self.shutdown._force_exit)
        self.timer.return_value.start.assert_called_once()
        self.assertTrue(self.timer.return_value.daemon)
        self.qt.QtCore.QMetaObject.invokeMethod.assert_called_once_with(
            self.qt._app, 'quit', self.qt.QtCore.Qt.QueuedConnection)
        self.release.assert_not_called()
        self.exit_fn.assert_not_called()

    def test_deadline_releases_lock_and_exits_even_if_release_fails(self):
        self.release.side_effect = RuntimeError('synthetic')
        with self.assertRaises(RuntimeError):
            self.shutdown._force_exit()
        self.exit_fn.assert_called_once_with(143)

    def test_finish_deletes_pages_before_profile_and_releases_lock(self):
        calls = Mock()
        browser = Mock()
        self.qt.BrowserView.instances = {'fixture': browser}
        calls.attach_mock(browser.webview.page.return_value.deleteLater, 'page')
        calls.attach_mock(self.qt.QtCore.QCoreApplication.sendPostedEvents, 'drain')
        calls.attach_mock(self.qt.QWebEngineProfile._persistent.deleteLater, 'profile')
        calls.attach_mock(self.release, 'release')
        self.shutdown.finish()
        self.assertEqual([c[0] for c in calls.mock_calls], ['page', 'drain', 'profile', 'drain', 'release'])
        self.assertIsNone(self.qt.QWebEngineProfile._persistent)
        self.assertTrue(self.shutdown.finished.is_set())
        self.exit_fn.assert_not_called()
        self.signals.assert_called_with(signal.SIGTERM, self.shutdown.previous)

    def test_requested_finish_allows_normal_interpreter_flush_keeps_deadline(self):
        self.qt.BrowserView.instances = {}
        self.shutdown._request(signal.SIGTERM, None)
        self.shutdown.finish()
        self.timer.return_value.cancel.assert_not_called()
        self.exit_fn.assert_not_called()
        self.assertTrue(self.shutdown.finished.is_set())
        self.release.assert_called_once()


class StartupTests(unittest.TestCase):
    def test_timeout_releases_and_exits(self):
        event, release, exit_fn = Mock(), Mock(), Mock()
        event.is_set.return_value = False
        with self.assertLogs(wd.log, level='ERROR') as logs:
            wd.watch_startup(event, release, timeout=10, clock=Mock(side_effect=[0, 10]), exit_fn=exit_fn)
        release.assert_called_once()
        exit_fn.assert_called_once_with(75)
        self.assertIn('official window failed to load within 10s; exiting for systemd restart', logs.output[0])

    def test_loaded(self):
        event = threading.Event()
        event.set()
        release, exit_fn = Mock(), Mock()
        rss = Mock(return_value=3500)
        wd.watch_startup(event, release, rss=rss, exit_fn=exit_fn)
        release.assert_not_called()
        exit_fn.assert_not_called()
        rss.assert_not_called()

    def test_loaded_while_sampling_high_rss(self):
        event = threading.Event()
        release, exit_fn = Mock(), Mock()
        def rss():
            event.set()
            return 3500
        wd.watch_startup(event, release, rss=rss, exit_fn=exit_fn)
        event.clear()  # A later navigation must not restart the watchdog.
        release.assert_not_called()
        exit_fn.assert_not_called()

    def test_app_latches_first_load_and_wires_persistent_shutdown(self):
        import app
        from types import SimpleNamespace
        class Loaded:
            def __iadd__(self, callback):
                self.callback = callback
                return self
        page = SimpleNamespace(events=SimpleNamespace(loaded=Loaded()))
        webview = Mock(settings={})
        webview.create_window.side_effect = [Mock(), page]
        def start(**kwargs):
            page.events.loaded.callback()
            startup_loaded = thread.call_args.kwargs['args'][0]
            self.assertTrue(startup_loaded.is_set())
            rss, exit_fn = Mock(return_value=3500), Mock()
            wd.watch_startup(startup_loaded, Mock(), rss=rss, exit_fn=exit_fn)
            rss.assert_not_called()
            exit_fn.assert_not_called()
        webview.start.side_effect = start
        with patch.dict(sys.modules, webview=webview), patch.dict(os.environ, INDEX_HUB_ENABLED='1'), \
                patch.object(app, 'Api'), patch.object(app, '_profile_lock', None), \
                patch.object(app, '_webview_start_kwargs', return_value={'private_mode': False}), \
                patch('hub_logging.configure_logging'), patch('hub_bridge.start_bridge'), \
                patch('webview_shutdown.QtShutdown') as shutdown, \
                patch.object(app.threading, 'Thread') as thread:
            app.main()
        shutdown.assert_called_once()
        shutdown.return_value.finish.assert_called_once()

    def test_memory(self):
        release, exit_fn = Mock(), Mock()
        with self.assertLogs(wd.log, level='ERROR'):
            wd.watch_startup(threading.Event(), release, rss=lambda: 2501, exit_fn=exit_fn)
        release.assert_called_once()
        exit_fn.assert_called_once_with(75)

    def test_guard_falls_back(self):
        import app
        with patch.dict(os.environ, INDEX_HUB_ENABLED='1', INDEX_HUB_PERSIST_LOGIN='1'), patch('webview_profile_patch.version', return_value='6.3'), patch('browser_profile.prepare_profile', return_value='/tmp/synthetic-profile') as prepare:
            with self.assertLogs('webview_profile_patch', level='WARNING'):
                self.assertTrue(app._webview_start_kwargs()['private_mode'])
            prepare.assert_called_once()

    def test_factory_shares_only_named_profiles(self):
        import webview_profile_patch as profile_patch
        from types import SimpleNamespace
        factory = lambda *a, **kw: Mock()
        factory.PersistentCookiesPolicy = SimpleNamespace(ForcePersistentCookies=object())
        factory.setPersistentCookiesPolicy = Mock()
        qt = SimpleNamespace(QWebEngineProfile=factory, BrowserView=Mock(), QtCore=Mock())
        with patch.object(profile_patch, 'version', return_value='6.2.1'), patch.object(profile_patch.importlib, 'import_module', return_value=qt):
            self.assertTrue(profile_patch.install_shared_profile('/tmp/synthetic'))
            first = qt.QWebEngineProfile('pywebview')
            self.assertIs(first, qt.QWebEngineProfile('pywebview'))
            private = qt.QWebEngineProfile()
            self.assertIsNot(private, qt.QWebEngineProfile())
            private.setPersistentCookiesPolicy.assert_not_called()
            self.assertTrue(profile_patch.install_shared_profile('/tmp/synthetic'))
            self.assertIs(first, qt.QWebEngineProfile('pywebview'))
            first.setPersistentStoragePath.assert_called_once_with('/tmp/synthetic')
            first.setPersistentCookiesPolicy.assert_called_once_with(factory.PersistentCookiesPolicy.ForcePersistentCookies)

    def test_missing_cookie_policy_or_setter_falls_back_private(self):
        import app
        import webview_profile_patch as pp
        from types import SimpleNamespace
        for missing in ('enum', 'setter'):
            factory = lambda *a, **kw: Mock()
            if missing != 'enum':
                factory.PersistentCookiesPolicy = SimpleNamespace(ForcePersistentCookies=object())
            if missing != 'setter':
                factory.setPersistentCookiesPolicy = Mock()
            qt = SimpleNamespace(QWebEngineProfile=factory)
            with self.subTest(missing=missing), patch.dict(os.environ, INDEX_HUB_ENABLED='1', INDEX_HUB_PERSIST_LOGIN='1'), patch('browser_profile.prepare_profile', return_value='/tmp/synthetic-profile'), patch('browser_profile.ProfileLock') as lock, patch.object(pp, 'version', return_value='6.2.1'), patch.object(pp.importlib, 'import_module', return_value=qt):
                with self.assertLogs(pp.log, level='WARNING'):
                    self.assertTrue(app._webview_start_kwargs()['private_mode'])
                self.assertIs(qt.QWebEngineProfile, factory)
                lock.assert_not_called()

    def test_unit_limits(self):
        config = configparser.ConfigParser(interpolation=None, strict=False)
        config.read('deploy/systemd/douyin-index-desktop.service')
        for section, values in {'Unit': {'StartLimitIntervalSec':'900', 'StartLimitBurst':'4'}, 'Service': {'MemoryHigh':'2800M', 'MemoryMax':'3G', 'MemorySwapMax':'0', 'Restart':'on-failure', 'RestartSec':'15', 'RestartSteps':'4', 'RestartMaxDelaySec':'120', 'KillMode':'control-group'}}.items():
            for key, value in values.items():
                self.assertEqual(config[section][key], value)


class QtRegressionTests(unittest.TestCase):
    def setUp(self):
        if not os.environ.get('DISPLAY') or not importlib.util.find_spec('webview') or not importlib.util.find_spec('PySide6'):
            if os.environ.get('INDEX_HUB_REQUIRE_BROWSER_TESTS') == '1':
                self.fail('Qt browser tests required but dependencies/DISPLAY unavailable')
            self.skipTest('Qt dependencies/DISPLAY unavailable')

    def run_harness(self, url, profile, cycle, patched=True, sigterm=False):
        env = dict(os.environ, PYWEBVIEW_GUI='qt', QT_API='pyside6', QTWEBENGINE_DISABLE_SANDBOX='1', XDG_DATA_HOME=profile, XDG_CACHE_HOME=profile + '/cache-home')
        with tempfile.TemporaryFile(mode='w+') as output:
            proc = subprocess.Popen([sys.executable, 'qt_profile_harness.py', url, profile, str(cycle), 'patched' if patched else 'original', 'sigterm' if sigterm else 'normal'], env=env, stdout=output, stderr=output, start_new_session=True)
            peak, reason = 0, None
            cookie_time, terminated = None, False
            start = time.monotonic()
            try:
                while proc.poll() is None:
                    peak = max(peak, wd.process_tree_rss_mb(proc.pid))
                    if peak > 1500 or time.monotonic() - start > (45 if patched else 20):
                        reason = 'RSS/time watchdog'
                        break
                    if sigterm and not terminated:
                        if cookie_time is None and b'"cookies_set": true' in os.pread(output.fileno(), 65536, 0):
                            cookie_time = time.monotonic()
                        if cookie_time is not None and time.monotonic() - cookie_time >= 2:
                            proc.send_signal(signal.SIGTERM)
                            terminated = True
                    time.sleep(.05)
            finally:
                # Kill the entire group, including Chromium, even on assertion/error.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            output.seek(0)
            result = output.read()
        print(f'Qt {"patched" if patched else "negative"} {"SIGTERM" if sigterm else "normal"} run {cycle}: peak RSS {peak:.1f} MB; elapsed {time.monotonic() - start:.1f}s', flush=True)
        if patched:
            self.assertIsNone(reason, result)
            self.assertEqual(proc.returncode, 0, result)
            if sigterm:
                self.assertTrue(terminated, 'SIGTERM was not sent')
            self.assertIn('"loaded": true', result)
            self.assertIn('"all_three_cookies": true', result)
            self.assertIn('"python_sent_all_three": true', result)
            self.assertIn('"login_state": "cookie_present"', result)
            if cycle == 1:
                self.assertIn('"login_before": "no_session_cookie"', result)
                self.assertIn('"login_state": "cookie_present"', result)
            else:
                self.assertIn('"restored_via_http": true', result)
            if cycle == 2:
                self.assertIn('"live_replacement_and_removal": true', result)
                self.assertIn('"identical_cookie_retained": true', result)
            self.assertLess(time.monotonic() - start, 60, result)
            self.assertLess(peak, 1500, result)
        else:
            self.assertTrue(reason or proc.returncode != 0, 'Original bug not reproduced: ' + result)

    def persistent_cycles(self, *, sigterm=False):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                received = self.headers.get('Cookie', '')
                flags = {'session': 'sessionid=synthetic-session' in received,
                         'guard': 'sid_guard=synthetic-guard' in received,
                         'persistent': 'persistent_fixture=synthetic-persistent' in received}
                if self.path == '/echo-replaced':
                    flags = {'replacement': 'sessionid=synthetic-replacement' in received,
                             'old_session': flags['session'], 'guard': flags['guard'], 'persistent': flags['persistent']}
                body = (json.dumps(flags) if self.path.startswith('/echo') else
                        '<html><body>local fixture<script>window.fixtureCookies=' + json.dumps(flags) + '</script></body></html>').encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json' if self.path.startswith('/echo') else 'text/html')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                if self.path == '/set':
                    self.send_header('Set-Cookie', 'sessionid=synthetic-session; Domain=127.0.0.1; Path=/; HttpOnly')
                    self.send_header('Set-Cookie', 'sid_guard=synthetic-guard; Domain=127.0.0.1; Path=/; Max-Age=86400; HttpOnly')
                    self.send_header('Set-Cookie', 'persistent_fixture=synthetic-persistent; Domain=127.0.0.1; Path=/; Max-Age=86400')
                if self.path == '/replace':
                    self.send_header('Set-Cookie', 'sessionid=synthetic-replacement; Domain=127.0.0.1; Path=/; HttpOnly')
                    self.send_header('Set-Cookie', 'sid_guard=; Domain=127.0.0.1; Path=/; Max-Age=0; HttpOnly')
                if self.path == '/same-session':
                    self.send_header('Set-Cookie', 'sessionid=synthetic-session; Domain=127.0.0.1; Path=/; HttpOnly')
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        with http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server, tempfile.TemporaryDirectory() as profile:
            os.chmod(profile, 0o700)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                url = f'http://127.0.0.1:{server.server_port}/'
                for cycle in range(1, 4):
                    self.run_harness(url, profile, cycle, sigterm=sigterm and cycle == 1)
                if os.environ.get('INDEX_HUB_QT_NEGATIVE_CONTROL') == '1':
                    with tempfile.TemporaryDirectory() as negative:
                        self.run_harness(url, negative, 1, patched=False)
            finally:
                server.shutdown()

    def test_three_persistent_cycles_and_real_login_state(self):
        self.persistent_cycles()

    def test_session_cookie_survives_sigterm(self):
        self.persistent_cycles(sigterm=True)
