import configparser
import http.server
import importlib.util
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
        wd.watch_startup(event, release, exit_fn=exit_fn)
        release.assert_not_called()
        exit_fn.assert_not_called()

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
        qt = SimpleNamespace(QWebEngineProfile=factory)
        with patch.object(profile_patch, 'version', return_value='6.2.1'), patch.object(profile_patch.importlib, 'import_module', return_value=qt):
            self.assertTrue(profile_patch.install_shared_profile('/tmp/synthetic'))
            first = qt.QWebEngineProfile('pywebview')
            self.assertIs(first, qt.QWebEngineProfile('pywebview'))
            self.assertIsNot(qt.QWebEngineProfile(), qt.QWebEngineProfile())
            self.assertTrue(profile_patch.install_shared_profile('/tmp/synthetic'))
            self.assertIs(first, qt.QWebEngineProfile('pywebview'))
            first.setPersistentStoragePath.assert_called_once_with('/tmp/synthetic')

    def test_unit_limits(self):
        config = configparser.ConfigParser(interpolation=None, strict=False)
        config.read('deploy/systemd/douyin-index-desktop.service')
        for section, values in {'Unit': {'StartLimitIntervalSec':'900', 'StartLimitBurst':'4'}, 'Service': {'MemoryHigh':'2500M', 'MemoryMax':'3G', 'MemorySwapMax':'0', 'Restart':'on-failure', 'RestartSec':'15', 'RestartSteps':'4', 'RestartMaxDelaySec':'120', 'KillMode':'control-group'}}.items():
            for key, value in values.items():
                self.assertEqual(config[section][key], value)


class QtRegressionTests(unittest.TestCase):
    def setUp(self):
        if not os.environ.get('DISPLAY') or not importlib.util.find_spec('webview') or not importlib.util.find_spec('PySide6'):
            if os.environ.get('INDEX_HUB_REQUIRE_BROWSER_TESTS') == '1':
                self.fail('Qt browser tests required but dependencies/DISPLAY unavailable')
            self.skipTest('Qt dependencies/DISPLAY unavailable')

    def run_harness(self, url, profile, cycle, patched=True):
        env = dict(os.environ, PYWEBVIEW_GUI='qt', QT_API='pyside6', QTWEBENGINE_DISABLE_SANDBOX='1', XDG_DATA_HOME=profile, XDG_CACHE_HOME=profile + '/cache-home')
        with tempfile.TemporaryFile(mode='w+') as output:
            proc = subprocess.Popen([sys.executable, 'qt_profile_harness.py', url, profile, str(cycle), 'patched' if patched else 'original'], env=env, stdout=output, stderr=output, start_new_session=True)
            peak, reason = 0, None
            start = time.monotonic()
            try:
                while proc.poll() is None:
                    peak = max(peak, wd.process_tree_rss_mb(proc.pid))
                    if peak > 1500 or time.monotonic() - start > (45 if patched else 20):
                        reason = 'RSS/time watchdog'
                        break
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
        print(f'Qt {"patched" if patched else "negative"} run {cycle}: peak RSS {peak:.1f} MB', flush=True)
        if patched:
            self.assertIsNone(reason, result)
            self.assertEqual(proc.returncode, 0, result)
            self.assertIn('"loaded": true', result)
            self.assertLess(time.monotonic() - start, 25, result)
            self.assertLess(peak, 1200, result)
        else:
            self.assertTrue(reason or proc.returncode != 0, 'Original bug not reproduced: ' + result)

    def test_three_persistent_cycles(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'<html><body>local fixture</body></html>'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
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
                    self.run_harness(url, profile, cycle)
                if os.environ.get('INDEX_HUB_QT_NEGATIVE_CONTROL') == '1':
                    with tempfile.TemporaryDirectory() as negative:
                        self.run_harness(url, negative, 1, patched=False)
            finally:
                server.shutdown()
