"""Synthetic geometry/dispatcher tests. Qt tests are opt-in under Xvfb."""
import math
import json
import os
import threading
import time
import unittest
from types import SimpleNamespace

from login_driver import (Capture, DriverError, DriverTimeout, FakeDriver, FrameMetadata,
                          QtDispatcher, css_to_view, interpolate, probe_script, validate_points)
from sms_login import SMS_SELECTORS


class GeometryTests(unittest.TestCase):
    def test_crop_zoom_and_device_ratio(self):
        for zoom in (.5, 1, 1.25, 2):
            for dpr in (1, 1.5, 2):
                with self.subTest(zoom=zoom, dpr=dpr):
                    meta = FrameMetadata(16, 32, 240, 120, zoom, dpr)
                    css = meta.css_point(.25, .75)
                    self.assertEqual(css, (16+60/(zoom*dpr), 32+90/(zoom*dpr)))
                    self.assertEqual(css_to_view(*css, zoom, dpr), (16*zoom+60/dpr, 32*zoom+90/dpr))
                    self.assertEqual(meta.css_point(0, 0), (16, 32))
        downsampled = FrameMetadata(16, 32, 120, 60, 2, 2, .5)
        self.assertEqual(downsampled.css_point(.5, .5), (46, 47))
        rounded = FrameMetadata(16, 32, 77, 47, 1.25, 1.5, 1, 51, 31)
        for actual, expected in zip(rounded.css_point(1, 1), (16+51/1.25, 32+31/1.25)):
            self.assertAlmostEqual(actual, expected)

    def test_nonfinite_and_out_of_bounds_rejected(self):
        frame = FrameMetadata(0, 0, 100, 100)
        for point in ((-.01, 0), (1.01, 0), (0, 2), (math.nan, 0), (0, math.inf), (True, 0), ('0', 0)):
            with self.subTest(point=point), self.assertRaises(DriverError):
                frame.css_point(*point)
        for args in ((0, 0, 0, 100), (-1, 0, 100, 100), (0, 0, 100, 100, 0),
                     (0, 0, 100, 100, 1, math.nan)):
            with self.assertRaises(DriverError):
                FrameMetadata(*args)

    def test_manual_interpolation_preserves_timing(self):
        original = [(10, 20, 0), (110, 20, 320), (110, 64, 496)]
        path = interpolate(original)
        self.assertEqual(path[0], original[0])
        self.assertEqual(path[-1], original[-1])
        for point in original:
            self.assertIn(point, path)
        for a, b in zip(path, path[1:]):
            self.assertLessEqual(math.dist(a[:2], b[:2]), 8.0000001)
            self.assertGreaterEqual(b[2], a[2])
        for x, y, stamp in path:
            if y == 20:
                self.assertAlmostEqual(stamp, (x-10)/100*320)
            else:
                self.assertAlmostEqual(stamp, 320+(y-20)/44*176)

    def test_pointer_admission_limits(self):
        self.assertEqual(validate_points('tap', [[.1, .2, 0]]), [(.1, .2, 0)])
        valid = [[0, 0, 0], [1, 1, 10000]]
        self.assertEqual(len(validate_points('drag', valid)), 2)
        invalid = [('tap', valid), ('drag', valid[:1]), ('bad', valid),
                   ('drag', [[0, 0, 1], [1, 1, 17]]), ('drag', [[0, 0, 0], [1, 1, 15]]),
                   ('drag', [[0, 0, 0], [1, 1, 10001]]), ('tap', [[0, math.nan, 0]]),
                   ('drag', [[0, 0, i*16] for i in range(201)]), ('tap', [[0, 0]])]
        for kind, points in invalid:
            with self.subTest(kind=kind, size=len(points)), self.assertRaises(DriverError):
                validate_points(kind, points)

    def test_fake_and_probe_do_not_record_credentials(self):
        driver = FakeDriver()
        driver.type_text('13800138000')
        driver.type_text('246810')
        output = repr(driver.actions) + repr(driver.capture) + probe_script(SMS_SELECTORS)
        self.assertNotIn('13800138000', output)
        self.assertNotIn('246810', output)
        self.assertNotIn('.value', probe_script(SMS_SELECTORS))
        self.assertNotIn('document.cookie', probe_script(SMS_SELECTORS))

    def test_json_transport_for_nested_objects_arrays_null_and_invalid_results(self):
        from login_driver import QtLoginDriver
        calls = []
        values = [dict(found={'phone': True}, rects={'phone': dict(x=1, y=2, width=30, height=40)}),
                  None, [dict(x=1, y=2, width=3, height=4)], 'invalid']
        def js(script, callback):
            calls.append(script)
            # Reproduce bindings which cannot marshal raw JS object results.
            value = values.pop(0)
            callback(json.dumps(value) if script.startswith('JSON.stringify(') and value != 'invalid' else '')
        class Immediate:
            def call_async(self, function):
                result = []
                def finish(value=None, error=None):
                    if error:
                        raise error
                    result.append(value)
                function(finish)
                return result[0]
        page = SimpleNamespace(runJavaScript=js)
        driver = QtLoginDriver(SimpleNamespace(page=lambda: page), dispatcher=Immediate())
        self.assertEqual(driver.probe()['rects']['phone']['width'], 30)
        self.assertIsNone(driver.element_rect('verification'))
        self.assertEqual(driver._read_json('[]', 'capture_failed')[0]['width'], 3)
        with self.assertRaisesRegex(DriverError, 'probe_failed'):
            driver.probe()
        self.assertTrue(all(s.startswith('JSON.stringify(') for s in calls))


@unittest.skipUnless(os.environ.get('INDEX_HUB_REQUIRE_BROWSER_TESTS') == '1', 'Qt tests require explicit Xvfb opt-in')
class DispatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def pump(self, worker, timeout=3):
        deadline = time.monotonic()+timeout
        while worker.is_alive() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.001)
        worker.join(.1)
        self.assertFalse(worker.is_alive())

    def test_calls_run_on_gui_thread_and_exception_is_sanitized(self):
        from PySide6.QtCore import QThread
        dispatcher = QtDispatcher()
        values = []
        def work():
            values.append(dispatcher.call(lambda: QThread.currentThread() == self.app.thread()))
            try:
                dispatcher.call(lambda: (_ for _ in ()).throw(ValueError('246810')))
            except Exception as exc:
                values.append(str(exc))
        thread = threading.Thread(target=work)
        thread.start()
        self.pump(thread)
        self.assertEqual(values, [True, 'driver_failed'])

    def test_timeout_drops_queued_input_and_late_callback(self):
        dispatcher = QtDispatcher(timeout=.05)
        effects, errors = [], []
        def work():
            try:
                dispatcher.call(lambda: effects.append('must not run'))
            except Exception as exc:
                errors.append(type(exc))
        thread = threading.Thread(target=work)
        thread.start()
        thread.join(1)  # Deliberately do not pump the Qt queue until timeout.
        self.app.processEvents()
        self.assertEqual(errors, [DriverTimeout])
        self.assertEqual(effects, [])
        callbacks = []
        def work_async():
            try:
                dispatcher.call_async(lambda finish: callbacks.append(finish))
            except Exception as exc:
                errors.append(type(exc))
        thread = threading.Thread(target=work_async)
        thread.start()
        self.pump(thread)
        callbacks[0]('late response')
        self.assertEqual(errors, [DriverTimeout, DriverTimeout])

    def test_cancelled_queue_never_runs_and_gui_wait_is_rejected(self):
        dispatcher = QtDispatcher()
        cancel = threading.Event()
        cancel.set()
        effects, errors = [], []
        def work():
            try:
                dispatcher.call(lambda: effects.append(True), cancel=cancel)
            except DriverError as exc:
                errors.append(exc.code)
        thread = threading.Thread(target=work)
        thread.start()
        self.pump(thread)
        self.assertEqual(effects, [])
        self.assertEqual(errors, ['cancelled'])
        with self.assertRaisesRegex(DriverError, 'worker_thread_required'):
            dispatcher.call(lambda: None)

    def test_real_qt_widget_input_and_capture_without_browser_process(self):
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QImage
        from PySide6.QtWidgets import QWidget, QLineEdit
        from login_driver import QtLoginDriver
        moves = []
        class Field(QLineEdit):
            def event(self, event):
                if event.type() in (QEvent.MouseButtonPress, QEvent.MouseMove, QEvent.MouseButtonRelease):
                    moves.append((event.type(), event.position().x(), event.position().y()))
                return super().event(event)
        class Page:
            def runJavaScript(self, script, finish):
                # A read-only geometry callback; no input text enters JS.
                if 'private_fields' in script:
                    raise AssertionError('expected selector value')
                self.assert_json(script)
                finish(json.dumps([dict(x=0, y=0, width=100, height=40)]))
            @staticmethod
            def assert_json(script):
                assert script.startswith('JSON.stringify(')
        class View(QWidget):
            def zoomFactor(self):
                return 2
            def page(self):
                return Page()
        view = View()
        view.resize(400, 200)
        target = Field(view)
        target.resize(400, 200)
        view.setFocusProxy(target)
        view.show()
        target.setFocus()
        self.app.processEvents()
        self.addCleanup(view.close)
        driver = QtLoginDriver(view)
        captures, errors = [], []
        def work():
            try:
                driver.click(50, 20)
                driver.type_text('246810')
                driver.drag([(20, 50, 0), (80, 50, 160)])
                captures.append(driver.grab())
                with self.assertRaisesRegex(DriverError, 'out_of_bounds'):
                    driver.click(300, 20)
            except Exception as exc:
                errors.append(type(exc).__name__)
        worker = threading.Thread(target=work)
        worker.start()
        self.pump(worker, 4)
        self.assertEqual(errors, [])
        self.assertTrue(target.text() == '246810')
        self.assertEqual(moves[0][1:], (100, 40))
        self.assertEqual(moves[-1][0], QEvent.MouseButtonRelease)
        meta = captures[0].metadata
        pixel = QImage.fromData(captures[0].png).pixelColor(int(50*meta.scale), int(20*meta.scale))
        self.assertEqual(pixel.name(), '#202020')

    def test_qtimer_drag_cancellation_releases_button_on_gui_thread(self):
        from PySide6.QtCore import QEvent, QThread, QTimer
        from PySide6.QtWidgets import QWidget
        from login_driver import QtLoginDriver
        cancel = threading.Event()
        events, errors = [], []
        class View(QWidget):
            def zoomFactor(self):
                return 1
            def event(self, event):
                if event.type() in (QEvent.MouseButtonPress, QEvent.MouseMove, QEvent.MouseButtonRelease):
                    events.append((event.type(), QThread.currentThread() == self.thread()))
                    if event.type() == QEvent.MouseButtonPress:
                        QTimer.singleShot(40, cancel.set)
                return super().event(event)
        view = View()
        view.resize(400, 200)
        view.show()
        self.addCleanup(view.close)
        driver = QtLoginDriver(view)
        def work():
            try:
                driver.drag([(20, 50, 0), (200, 50, 900)], cancel=cancel)
            except DriverError as exc:
                errors.append(exc.code)
        worker = threading.Thread(target=work)
        worker.start()
        self.pump(worker)
        self.assertEqual(errors, ['cancelled'])
        self.assertEqual(events[0][0], QEvent.MouseButtonPress)
        self.assertEqual(events[-1][0], QEvent.MouseButtonRelease)
        self.assertTrue(all(on_gui for _, on_gui in events))


@unittest.skipUnless(os.environ.get('INDEX_HUB_REQUIRE_BROWSER_TESTS') == '1', 'Qt tests require explicit Xvfb opt-in')
class QtInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        DispatcherTests.setUpClass.__func__(cls)

    pump = DispatcherTests.pump

    # No listener/socket needed: fixture HTML and all assets stay in memory.
    def setUp(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineUrlRequestInterceptor
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from login_driver import QtLoginDriver
        class DenyNetwork(QWebEngineUrlRequestInterceptor):
            def interceptRequest(self, info):
                if info.requestUrl().scheme() not in ('data', 'about'):
                    info.block(True)
        self.profile = QWebEngineProfile()
        self.interceptor = DenyNetwork()
        self.profile.setUrlRequestInterceptor(self.interceptor)
        self.view = QWebEngineView()
        self.page = QWebEnginePage(self.profile, self.view)
        self.view.setPage(self.page)
        self.view.resize(640, 480)
        self.view.setZoomFactor(1.25)
        self.view.show()
        loaded = []
        self.view.loadFinished.connect(loaded.append)
        self.view.setHtml('''<html><head><style>
          body { margin:0; background:#eef; } input {position:absolute;left:40px;top:40px;width:180px;height:30px;}
          #captcha_container {position:absolute;left:40px;top:140px;width:240px;height:80px;background:#89a;}
          </style></head><body><input type="tel"><div id="captcha_container"></div>
          <script>window.events=[];
          for (const type of ['pointerdown','pointermove','pointerup','input'])
            document.addEventListener(type, e => events.push({type, trusted:e.isTrusted,x:e.clientX,y:e.clientY,t:performance.now()}));
          </script></body></html>''', QUrl('http://127.0.0.1/fixture'))
        deadline = time.monotonic()+10
        while not loaded and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertEqual(loaded, [True])
        self.driver = QtLoginDriver(self.view)
        self.addCleanup(self.close_view)

    def close_view(self):
        from PySide6.QtCore import QCoreApplication, QEvent
        self.view.close()
        self.page.deleteLater()
        self.view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.profile.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def work(self, function, timeout=10):
        output, errors = [], []
        def run():
            try:
                output.append(function())
            except Exception as exc:
                errors.append(type(exc).__name__ + ':' + str(exc))
        worker = threading.Thread(target=run)
        worker.start()
        self.pump(worker, timeout)
        self.assertEqual(errors, [])
        return output[0]

    def js(self, script):
        return self.work(lambda: self.driver._read_json(script, 'fixture_js_failed'))

    def test_real_probe_capture_key_events_and_manual_drag(self):
        from PySide6.QtGui import QImage
        self.assertTrue(self.work(self.driver.probe)['found']['phone'])
        rect = self.work(lambda: self.driver.element_rect('verification'))
        self.assertEqual(rect, dict(x=40, y=140, width=240, height=80))
        self.work(lambda: self.driver.click(80, 55))
        self.work(lambda: self.driver.type_text('13800138000'))
        self.assertTrue(self.js("document.querySelector('input').value === '13800138000'"))
        self.work(lambda: self.driver.type_text('246810'))
        self.assertTrue(self.js("document.querySelector('input').value === '246810'"))
        full = self.work(self.driver.grab)
        image = QImage.fromData(full.png)
        # Input field pixels are masked even in the full-view fallback.
        x, y = int(80*full.metadata.scale), int(55*full.metadata.scale)
        self.assertEqual(image.pixelColor(x, y).name(), '#202020')
        capture = self.work(lambda: self.driver.grab(dict(x=24, y=124, width=272, height=112)))
        meta = capture.metadata
        self.assertAlmostEqual(meta.x0, 24)
        self.assertAlmostEqual(meta.y0, 124)
        self.assertEqual(meta.width, round(272*meta.scale))
        self.assertLessEqual(len(capture.png), 300*1024)
        path = [(50, 170, 0), (90, 170, 160), (180, 170, 480)]
        self.work(lambda: self.driver.drag(path, frame=meta))
        events = self.js('window.events')
        pointer = [e for e in events if e['type'].startswith('pointer')]
        self.assertTrue(all(e['trusted'] for e in events))
        self.assertEqual(pointer[-1]['type'], 'pointerup')
        drag = pointer[pointer.index(next(e for e in pointer if e['type'] == 'pointerdown' and e['y'] > 100)):]
        self.assertAlmostEqual(drag[0]['x'], 50, delta=1)
        self.assertAlmostEqual(drag[-1]['x'], 180, delta=1)
        self.assertGreaterEqual(drag[-1]['t']-drag[0]['t'], 450)
        self.assertLess(drag[-1]['t']-drag[0]['t'], 1000)
        for a, b in zip(drag, drag[1:]):
            self.assertLessEqual(math.hypot(b['x']-a['x'], b['y']-a['y'])*meta.zoom, 8.01)

    def test_real_bounds_and_changed_zoom_reject_input(self):
        capture = self.work(self.driver.grab)
        self.view.setZoomFactor(2)
        def rejected():
            with self.assertRaisesRegex(DriverError, 'stale_frame'):
                self.driver.click(20, 20, frame=capture.metadata)
            with self.assertRaisesRegex(DriverError, 'out_of_bounds'):
                self.driver.click(10000, 20)
        self.work(rejected)

    def test_real_probe_identity_ignores_animation_but_detects_new_challenge(self):
        before = self.work(self.driver.probe)
        self.js("(() => { document.querySelector('#captcha_container').style.background='#123'; return true; })()")
        animated = self.work(self.driver.probe)
        self.assertEqual(before['challenge_key'], animated['challenge_key'])
        self.js("(() => { document.querySelector('#captcha_container').setAttribute('data-challenge-id','synthetic-new'); return true; })()")
        refreshed = self.work(self.driver.probe)
        self.assertNotEqual(before['challenge_key'], refreshed['challenge_key'])
        self.assertNotIn('synthetic-new', repr(refreshed))


if __name__ == '__main__':
    unittest.main()
