"""Qt login input/capture adapter. No navigation, credential JS or disk output.

Construct with the existing QWebEngineView and a fast, memory-only
session_present callback (e.g. the existing cookie mirror). All Qt access is
queued to its GUI thread. Public operations are called from a worker thread.
"""
from collections import deque
from dataclasses import dataclass, field
import json
import math
import random
import threading
import time


class DriverError(ValueError):
    def __init__(self, code='driver_failed'):
        self.code = code
        super().__init__(code)


class DriverTimeout(TimeoutError):
    def __init__(self):
        super().__init__('driver_timeout')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True)
class FrameMetadata:
    x0: float
    y0: float
    width: int  # PNG physical pixels
    height: int
    zoom: float = 1
    dpr: float = 1
    image_scale: float = 1  # Optional PNG downsampling, independent of Qt zoom.

    def __post_init__(self):
        if (not all(_number(v) for v in (self.x0, self.y0, self.width, self.height, self.zoom, self.dpr, self.image_scale))
                or min(self.x0, self.y0) < 0 or min(self.width, self.height, self.zoom, self.dpr, self.image_scale) <= 0):
            raise DriverError('invalid_frame')

    @property
    def scale(self):
        return self.zoom * self.dpr * self.image_scale

    def css_point(self, u, v):
        if not all(_number(n) and 0 <= n <= 1 for n in (u, v)):
            raise DriverError('out_of_bounds')
        return self.x0 + u * self.width / self.scale, self.y0 + v * self.height / self.scale


@dataclass(frozen=True)
class Capture:
    png: bytes = field(repr=False)
    metadata: FrameMetadata


def css_to_view(x, y, zoom, dpr):
    """Qt mouse events use logical pixels, unlike the physical PNG pixels.

    CSS -> physical is zoom*dpr; dividing by dpr gives Qt logical pixels.
    Multiplying event coordinates by dpr again would misclick on HiDPI.
    """
    if not all(_number(v) for v in (x, y, zoom, dpr)) or min(zoom, dpr) <= 0:
        raise DriverError('out_of_bounds')
    return x * zoom, y * zoom


def validate_points(kind, points):
    if kind not in ('tap', 'drag') or not isinstance(points, (list, tuple)):
        raise DriverError('invalid_pointer')
    if not (len(points) == 1 if kind == 'tap' else 2 <= len(points) <= 200):
        raise DriverError('invalid_pointer')
    result = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 3 or not all(_number(n) for n in point):
            raise DriverError('invalid_pointer')
        u, v, stamp = point
        if not (0 <= u <= 1 and 0 <= v <= 1 and 0 <= stamp <= 10000):
            raise DriverError('out_of_bounds')
        if (not result and stamp != 0) or (result and stamp - result[-1][2] < 16):
            raise DriverError('invalid_pointer')
        result.append((u, v, stamp))
    return result


def interpolate(points, max_step=8):
    """Densify a supplied human trajectory; retain its endpoints and timestamps.

    This is replay only: no trajectory is inferred from a CAPTCHA or generated
    from a target. Coordinates here are Qt logical pixels.
    """
    if not _number(max_step) or max_step <= 0 or not points:
        raise DriverError('invalid_pointer')
    if any(not isinstance(p, (tuple, list)) or len(p) != 3 or not all(_number(v) for v in p) for p in points):
        raise DriverError('invalid_pointer')
    result = [tuple(points[0])]
    for a, b in zip(points, points[1:]):
        if not all(_number(v) for v in (*a, *b)) or b[2] < a[2]:
            raise DriverError('invalid_pointer')
        steps = max(1, math.ceil(math.hypot(b[0] - a[0], b[1] - a[1]) / max_step))
        for i in range(1, steps + 1):
            result.append(tuple(a[j] + (b[j] - a[j]) * i / steps for j in range(3)))
    return result


class _Request:
    def __init__(self, function, cancelled):
        self.function, self.cancelled = function, cancelled
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.expired = False
        self.value = self.error = None

    def finish(self, value=None, error=None):
        with self.lock:
            if not self.expired:
                self.value, self.error = value, error
                self.done.set()


class QtDispatcher:
    """Queue short operations with a five-second deadline, never block Qt.

    Timed-out queued requests are discarded, including their callable's secret
    references. Already executing short GUI calls cannot be rolled back.
    """
    def __init__(self, timeout=5):
        from PySide6.QtCore import QCoreApplication, QMetaObject, QObject, Qt, Slot
        app = QCoreApplication.instance()
        if app is None:
            raise DriverError('qt_unavailable')
        self.timeout = min(5, timeout)
        self._queue, self._lock = deque(), threading.Lock()
        dispatcher = self

        class Receiver(QObject):
            @Slot()
            def drain(self):
                while True:
                    with dispatcher._lock:
                        if not dispatcher._queue:
                            return
                        request = dispatcher._queue.popleft()
                    with request.lock:
                        function, request.function = request.function, None
                        skip = request.expired or (request.cancelled is not None and request.cancelled.is_set())
                    if skip:
                        request.finish(error=DriverError('cancelled'))
                        continue
                    try:
                        function(request.finish)
                    except Exception as exc:
                        request.finish(error=exc if isinstance(exc, DriverError) else DriverError())
                    finally:
                        function = None

        self._receiver = Receiver()
        self._receiver.moveToThread(app.thread())
        self._invoke = lambda: QMetaObject.invokeMethod(self._receiver, 'drain', Qt.QueuedConnection)

    def call_async(self, function, *, cancel=None):
        from PySide6.QtCore import QThread
        if QThread.currentThread() == self._receiver.thread():
            raise DriverError('worker_thread_required')
        request = _Request(function, cancel)
        with self._lock:
            self._queue.append(request)
        self._invoke()
        deadline = time.monotonic() + self.timeout
        while not request.done.wait(min(.05, max(0, deadline - time.monotonic()))):
            if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                with request.lock:
                    request.expired = True
                    request.function = None
                raise DriverError('cancelled') if cancel is not None and cancel.is_set() else DriverTimeout()
        if request.error is not None:
            raise request.error from None
        return request.value

    def call(self, function, *, cancel=None):
        return self.call_async(lambda finish: finish(function()), cancel=cancel)


def probe_script(selectors):
    # Only the selector table enters JS. Never input values, cookies, or raw text.
    return '''(() => {
      const s = %s;
      const visible = e => { const r = e.getBoundingClientRect(), c = getComputedStyle(e);
        return r.width > 0 && r.height > 0 && c.display !== 'none' && c.visibility !== 'hidden'; };
      const find = key => { const rule = s[key];
        return Array.from(document.querySelectorAll(rule.css)).find(e =>
          visible(e) && (!rule.text || rule.text.some(t => e.textContent.includes(t)))); };
      const rect = e => { if (!e) return null; const r = e.getBoundingClientRect();
        return {x:r.x, y:r.y, width:r.width, height:r.height}; };
      const found = {}, rects = {};
      for (const key of s.elements) { const e = find(key); found[key] = !!e; rects[key] = rect(e); }
      const text = document.body ? document.body.innerText : '';
      const has = key => s[key].some(t => text.includes(t));
      const countdown = find('countdown');
      const match = countdown && countdown.textContent.match(new RegExp(s.countdown_pattern));
      return {url:location.origin + location.pathname, found, rects,
        login_page:s.login_paths.some(p => location.pathname.toLowerCase().includes(p)),
        login_form:found.login_form, verification:found.verification || has('verification_text'),
        rate_limited:has('rate_text'), code_invalid:has('code_error_text'), error:has('error_text'),
        countdown:match ? Number(match[1]) : null};
    })()''' % json.dumps(selectors, ensure_ascii=True)


class QtLoginDriver:
    def __init__(self, view, *, selectors=None, session_present=None, dispatcher=None):
        if selectors is None:
            from sms_login import SMS_SELECTORS
            selectors = SMS_SELECTORS
        self._view = view
        self._selectors = selectors
        self._script = probe_script(selectors)
        # Callback must read the memory mirror only, never make a network call.
        self._session_present = session_present or (lambda: False)
        self.dispatcher = dispatcher or QtDispatcher()

    def probe(self):
        def run(finish):
            def receive(value):
                if not isinstance(value, dict):
                    finish(error=DriverError('probe_failed'))
                    return
                try:
                    value['session_present'] = bool(self._session_present())
                    finish(value)
                except Exception:
                    finish(error=DriverError('probe_failed'))
            self._view.page().runJavaScript(self._script, receive)
        return self.dispatcher.call_async(run)

    def element_rect(self, key):
        if key not in self._selectors['elements']:
            raise DriverError('invalid_element')
        script = '''(() => { const rule = %s;
          const e = Array.from(document.querySelectorAll(rule.css)).find(e => {
            const r = e.getBoundingClientRect(), c = getComputedStyle(e);
            return r.width > 0 && r.height > 0 && c.visibility !== 'hidden' && c.display !== 'none'
              && (!rule.text || rule.text.some(t => e.textContent.includes(t))); });
          if (!e) return null; const r = e.getBoundingClientRect();
          return {x:r.x,y:r.y,width:r.width,height:r.height}; })()''' % json.dumps(self._selectors[key])
        return self.dispatcher.call_async(lambda finish: self._view.page().runJavaScript(script, finish))

    def grab(self, rect=None):
        from PySide6.QtCore import QBuffer, QIODevice, QRect, Qt
        from PySide6.QtGui import QColor, QPainter
        def capture(private_rects):
            zoom, dpr = self._view.zoomFactor(), self._view.devicePixelRatioF()
            width, height = self._view.width(), self._view.height()
            if rect is None:
                left = top = 0
                right, bottom = width, height
            else:
                x, y, w, h = (rect[k] for k in ('x', 'y', 'width', 'height'))
                if not all(_number(v) for v in (x, y, w, h)) or min(w, h) <= 0:
                    raise DriverError('invalid_frame')
                left, top = max(0, math.floor(x * zoom)), max(0, math.floor(y * zoom))
                right, bottom = min(width, math.ceil((x+w)*zoom)), min(height, math.ceil((y+h)*zoom))
            if right <= left or bottom <= top:
                raise DriverError('invalid_frame')
            bitmap = self._view.grab(QRect(left, top, right-left, bottom-top)).toImage()
            bitmap.setDevicePixelRatio(1)
            # The full-view fallback can include the phone/code inputs. Hide
            # their pixels, without reading their values or mutating the DOM.
            painter = QPainter(bitmap)
            for r in private_rects:
                x, y = math.floor((r['x']*zoom-left)*dpr), math.floor((r['y']*zoom-top)*dpr)
                w, h = math.ceil(r['width']*zoom*dpr)+2, math.ceil(r['height']*zoom*dpr)+2
                painter.fillRect(QRect(x-1, y-1, w, h), QColor('#202020'))
            painter.end()
            factor = 1
            while True:
                w, h = bitmap.width()//factor, bitmap.height()//factor
                if min(w, h) < 1:
                    raise DriverError('capture_failed')
                # Crop the remainder so one scale describes both axes exactly.
                output = bitmap.copy(0, 0, w*factor, h*factor).scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                buffer = QBuffer()
                buffer.open(QIODevice.WriteOnly)
                if not output.save(buffer, 'PNG'):
                    raise DriverError('capture_failed')
                png = bytes(buffer.data())
                if len(png) <= 300*1024:
                    return Capture(png, FrameMetadata(left/zoom, top/zoom, w, h, zoom, dpr, 1/factor))
                factor *= 2
        script = '''Array.from(document.querySelectorAll(%s)).map(e => {
          const r = e.getBoundingClientRect();
          return {x:r.x,y:r.y,width:r.width,height:r.height}; })''' % json.dumps(self._selectors['private_fields'])
        def run(finish):
            def receive(rectangles):
                try:
                    if not isinstance(rectangles, list):
                        raise DriverError('capture_failed')
                    finish(capture(rectangles))
                except Exception:
                    finish(error=DriverError('capture_failed'))
            self._view.page().runJavaScript(script, receive)
        return self.dispatcher.call_async(run)

    def _point(self, x, y, frame=None):
        from PySide6.QtCore import QPointF
        zoom, dpr = self._view.zoomFactor(), self._view.devicePixelRatioF()
        if frame is not None and (zoom != frame.zoom or dpr != frame.dpr):
            raise DriverError('stale_frame')
        x, y = css_to_view(x, y, zoom, dpr)
        if not (0 <= x < self._view.width() and 0 <= y < self._view.height()):
            raise DriverError('out_of_bounds')
        return QPointF(x, y)

    def _mouse(self, kind, point):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication
        target = self._view.focusProxy() or self._view
        event_type = {'press': QEvent.MouseButtonPress, 'move': QEvent.MouseMove,
                      'release': QEvent.MouseButtonRelease}[kind]
        button = Qt.NoButton if kind == 'move' else Qt.LeftButton
        buttons = Qt.NoButton if kind == 'release' else Qt.LeftButton
        QApplication.sendEvent(target, QMouseEvent(event_type, point, target.mapToGlobal(point),
                                                  button, buttons, Qt.NoModifier))

    def click(self, x, y, *, cancel=None, frame=None):
        def click():
            point = self._point(x, y, frame)
            self._mouse('press', point)
            self._mouse('release', point)
        self.dispatcher.call(click, cancel=cancel)

    def drag(self, points, *, cancel=None, frame=None):
        # Validate the entire path before pressing; interpolation is in Qt pixels.
        if (not isinstance(points, (list, tuple)) or not 2 <= len(points) <= 200
                or any(not isinstance(p, (list, tuple)) or len(p) != 3 or not all(_number(v) for v in p) for p in points)
                or points[0][2] != 0 or points[-1][2] > 10000
                or any(b[2]-a[2] < 16 for a, b in zip(points, points[1:]))):
            raise DriverError('invalid_pointer')
        def convert():
            return [(p.x(), p.y(), t) for x, y, t in points for p in (self._point(x, y, frame),)]
        path = interpolate(self.dispatcher.call(convert, cancel=cancel))
        from PySide6.QtCore import QPointF
        last = path[0]
        pressed = False
        def press():
            self._mouse('press', QPointF(*last[:2]))
            return time.monotonic()
        try:
            started = self.dispatcher.call(press, cancel=cancel)
            pressed = True
            for point in path[1:]:
                self._wait(max(0, started + point[2]/1000 - time.monotonic()), cancel)
                self.dispatcher.call(lambda p=point: self._mouse('move', QPointF(*p[:2])), cancel=cancel)
                last = point
        finally:
            if pressed:
                # Always release even on cancellation; no stuck mouse button.
                self.dispatcher.call(lambda: self._mouse('release', QPointF(*last[:2])))

    @staticmethod
    def _wait(seconds, cancel):
        if cancel is None:
            time.sleep(seconds)
        elif cancel.wait(seconds):
            raise DriverError('cancelled')

    def type_text(self, text, *, clear=True, cancel=None):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtWidgets import QApplication
        def key(keycode, value='', modifiers=Qt.NoModifier):
            target = self._view.focusProxy() or self._view
            for kind in (QEvent.KeyPress, QEvent.KeyRelease):
                QApplication.sendEvent(target, QKeyEvent(kind, keycode, modifiers, value))
        try:
            if clear:
                self.dispatcher.call(lambda: key(Qt.Key_A, modifiers=Qt.ControlModifier), cancel=cancel)
                self.dispatcher.call(lambda: key(Qt.Key_Backspace), cancel=cancel)
            for char in text:
                self.dispatcher.call(lambda c=char: key(ord(c.upper()), c), cancel=cancel)
                self._wait(random.uniform(.06, .15), cancel)
        finally:
            text = char = None


class FakeDriver:
    """Synthetic memory-only driver. Deliberately records no text contents."""
    def __init__(self, probes=()):
        self.probes = deque(probes)
        self.current = dict(url='https://creator.douyin.com/login', login_page=True,
                            login_form=True, session_present=False, found={}, rects={}, countdown=None)
        self.actions = []
        self.capture = Capture(b'synthetic-png', FrameMetadata(0, 0, 300, 150))

    def probe(self):
        if self.probes:
            value = self.probes.popleft()
            if isinstance(value, Exception):
                raise value
            self.current = value
        return self.current.copy()

    def element_rect(self, key):
        return self.current.get('rects', {}).get(key)

    def grab(self, rect=None):
        self.actions.append(('grab', rect))
        return self.capture

    def click(self, x, y, **kwargs):
        self.actions.append(('click', x, y))

    def drag(self, points, **kwargs):
        self.actions.append(('drag', points))

    def type_text(self, text, **kwargs):
        self.actions.append(('type_text', len(text)))
