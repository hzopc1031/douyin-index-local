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
    logical_width: float | None = None  # Actual cropped Qt extent, including pixel rounding.
    logical_height: float | None = None

    def __post_init__(self):
        if (not all(_number(v) for v in (self.x0, self.y0, self.width, self.height, self.zoom, self.dpr, self.image_scale))
                or min(self.x0, self.y0) < 0 or min(self.width, self.height, self.zoom, self.dpr, self.image_scale) <= 0):
            raise DriverError('invalid_frame')
        if ((self.logical_width is None) != (self.logical_height is None)
                or any(v is not None and (not _number(v) or v <= 0)
                       for v in (self.logical_width, self.logical_height))):
            raise DriverError('invalid_frame')

    @property
    def scale(self):
        return self.zoom * self.dpr * self.image_scale

    @property
    def x_scale(self):
        return self.width*self.zoom/self.logical_width if self.logical_width else self.scale

    @property
    def y_scale(self):
        return self.height*self.zoom/self.logical_height if self.logical_height else self.scale

    def css_point(self, u, v):
        if not all(_number(n) and 0 <= n <= 1 for n in (u, v)):
            raise DriverError('out_of_bounds')
        return self.x0 + u * self.width / self.x_scale, self.y0 + v * self.height / self.y_scale


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
    """Densify a manual or solver trajectory, preserving endpoints/timing.

    Coordinates here are Qt logical pixels. Recognition lives outside the driver.
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
      const verification = find('verification');
      // Hash identity attributes in-page. Never return image URLs/IDs, which
      // can contain opaque tokens. Animated pixels do not change this identity.
      const assets = verification ? [verification, ...verification.querySelectorAll(s.challenge_assets)] : [];
      const identity = assets.map(e => s.challenge_attributes.map(a => e.getAttribute(a) || '').join('|')).join(';');
      let hash = 2166136261;
      for (let i=0; i<identity.length; i++) hash = Math.imul(hash ^ identity.charCodeAt(i), 16777619);
      return {url:location.origin + location.pathname, found, rects,
        login_page:s.login_paths.some(p => location.pathname.toLowerCase().includes(p)),
        login_form:found.login_form, verification:found.verification || has('verification_text'),
        rate_limited:has('rate_text'), code_invalid:has('code_error_text'), error:has('error_text'),
        blocked:has('blocked_text'), verification_failed:has('verification_error_text'),
        challenge_key:String(hash >>> 0),
        challenge_kind:has('rotation_text') ? 'rotation' : has('gap_text') ? 'gap_slider' : 'unknown',
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
        value = self._read_json(self._script, 'probe_failed')
        if not isinstance(value, dict):
            raise DriverError('probe_failed')
        try:
            value['session_present'] = bool(self._session_present())
        except Exception:
            raise DriverError('probe_failed') from None
        return value

    def _read_json(self, script, error, transform=None):
        # PySide's runJavaScript callback conversion is not a portable transport
        # for JS objects/arrays (some bindings deliver an empty string). Use a
        # JSON string across the Qt boundary, including nested geometry arrays.
        # Only our read-only summaries enter this path, never field values.
        def run(finish):
            def receive(value):
                try:
                    decoded = json.loads(value)
                    finish(transform(decoded) if transform else decoded)
                except Exception:
                    finish(error=DriverError(error))
            self._view.page().runJavaScript('JSON.stringify(' + script + ')', receive)
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
        return self._read_json(script, 'probe_failed')

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
                    return Capture(png, FrameMetadata(left/zoom, top/zoom, w, h, zoom, dpr, 1/factor,
                        (right-left)*w*factor/bitmap.width(), (bottom-top)*h*factor/bitmap.height()))
                factor *= 2
        script = '''Array.from(document.querySelectorAll(%s)).map(e => {
          const r = e.getBoundingClientRect();
          return {x:r.x,y:r.y,width:r.width,height:r.height}; })''' % json.dumps(self._selectors['private_fields'])
        def masked_capture(rectangles):
            if not isinstance(rectangles, list):
                raise DriverError('capture_failed')
            return capture(rectangles)
        return self._read_json(script, 'capture_failed', masked_capture)

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

    def _prepare_input(self, *, cancel=None):
        from PySide6.QtCore import Qt

        def focus():
            window = self._view.window()
            window.raise_()
            window.activateWindow()
            (self._view.focusProxy() or self._view).setFocus(Qt.OtherFocusReason)

        def ready():
            window = self._view.window()
            handle = window.windowHandle()
            target = self._view.focusProxy() or self._view
            page = self._view.page() if hasattr(self._view, 'page') else None
            # WebEngine's loadFinished/DOM geometry and Qt exposure can precede
            # the first Chromium frame metadata. Until contentsSize is populated,
            # xcb can silently drop mouse presses even with the right receiver,
            # coordinates and focus. Widget-only adapters have no such barrier.
            if hasattr(page, 'contentsSize') and page.contentsSize().isEmpty():
                return False
            return (self._view.isVisible() and self._view.isEnabled()
                    and handle is not None and handle.isExposed()
                    and window.isActiveWindow() and target.hasFocus())

        # sendEvent does not activate a native window like a real X11 click.
        # Wait on the worker for both activation and WebEngine frame readiness,
        # leaving the GUI free to process rendering, exposure and FocusIn. Do
        # not probe readiness with a click or retry dropped input: either could
        # submit a login action twice. Timeout/cancellation sends no input.
        deadline = time.monotonic() + self.dispatcher.timeout
        self.dispatcher.call(focus, cancel=cancel)
        while not self.dispatcher.call(ready, cancel=cancel):
            if time.monotonic() >= deadline:
                raise DriverTimeout()
            self._wait(.01, cancel)

    def click(self, x, y, *, cancel=None, frame=None):
        self.dispatcher.call(lambda: self._point(x, y, frame), cancel=cancel)
        self._prepare_input(cancel=cancel)
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
            return ([(p.x(), p.y(), t) for x, y, t in points for p in (self._point(x, y, frame),)],
                    self._view.zoomFactor(), self._view.devicePixelRatioF())
        path, zoom, dpr = self.dispatcher.call(convert, cancel=cancel)
        path = interpolate(path)
        from PySide6.QtCore import QPointF, QTimer
        done = threading.Event()
        replay = dict(last=path[0], index=1, pressed=False, error=None, timer=None)
        def stop(error=None):
            if replay['timer'] is not None:
                replay['timer'].stop()
                replay['timer'].deleteLater()
                replay['timer'] = None
            if replay['pressed']:
                self._mouse('release', QPointF(*replay['last'][:2]))
                replay['pressed'] = False
            replay['error'] = error
            done.set()
        def start():
            # Rendering/activation may have taken time after convert(). Reject
            # a changed transform or bounds before pressing, not on the first
            # timer tick after the press has already reached the page.
            if self._view.zoomFactor() != zoom or self._view.devicePixelRatioF() != dpr:
                raise DriverError('stale_frame')
            for x, y, _ in points:
                self._point(x, y, frame)
            # One GUI-owned timer, with a short cancellation heartbeat. The
            # worker waits for replay, never blocks the GUI with sleep/wait.
            timer = replay['timer'] = QTimer(self._view)
            timer.setSingleShot(True)
            started = time.monotonic()
            def tick():
                try:
                    if cancel is not None and cancel.is_set():
                        stop(DriverError('cancelled'))
                        return
                    if self._view.zoomFactor() != zoom or self._view.devicePixelRatioF() != dpr:
                        stop(DriverError('stale_frame'))
                        return
                    elapsed = (time.monotonic()-started)*1000
                    while replay['index'] < len(path) and path[replay['index']][2] <= elapsed:
                        point = path[replay['index']]
                        # Recheck the current transform/bounds throughout replay.
                        self._point(point[0]/zoom, point[1]/zoom, frame)
                        self._mouse('move', QPointF(*point[:2]))
                        replay['last'] = point
                        replay['index'] += 1
                    if replay['index'] == len(path):
                        stop()
                    else:
                        timer.start(max(1, min(50, math.ceil(path[replay['index']][2]-elapsed))))
                except Exception:
                    stop(DriverError('driver_failed'))
            timer.timeout.connect(tick)
            if cancel is not None and cancel.is_set():
                stop(DriverError('cancelled'))
                return
            self._mouse('press', QPointF(*path[0][:2]))
            replay['pressed'] = True
            tick()
        self._prepare_input(cancel=cancel)
        try:
            self.dispatcher.call(start, cancel=cancel)
            if not done.wait(points[-1][2]/1000+1):
                raise DriverTimeout()
            if replay['error'] is not None:
                raise replay['error']
        finally:
            if not done.is_set():
                self.dispatcher.call(lambda: stop(DriverError('cancelled')))

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
            self._prepare_input(cancel=cancel)
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
