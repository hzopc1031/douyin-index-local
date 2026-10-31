"""Memory-only SMS login worker for the existing official browser window.

No HTTP/bridge wiring or default-mode switch lives here (plan items 3–5).
Later app wiring must pass Api._lock, Api._login_coordinator and an on_success
callback clearing qr.holder. Short methods only admit work/read snapshots;
pointer waits at most 12s. Secrets never enter messages, reprs or selectors.
Python strings cannot be securely erased; references are dropped on completion.
"""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import math
import re
import secrets
import threading
import time
from typing import Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from login_driver import DriverError, DriverTimeout, validate_points

# UNCALIBRATED: all DOM selectors and text detection belong in this table.
# Calibration must change this table only, never add page-specific driver logic.
SMS_SELECTORS = {
    'elements': ('tab', 'phone', 'send', 'countdown', 'code', 'submit', 'verification', 'login_form'),
    'tab': {'css': 'button, [role=tab], span', 'text': ('验证码登录', '短信登录')},
    'phone': {'css': 'input[type=tel], input[placeholder*=手机号]'},
    'send': {'css': 'button, [role=button]', 'text': ('获取验证码', '发送验证码', '重新发送')},
    'countdown': {'css': 'button, [role=button], span', 'text': ('秒', 's后')},
    'code': {'css': 'input[autocomplete=one-time-code], input[placeholder*=验证码]'},
    'submit': {'css': 'button, [role=button]', 'text': ('登录',)},
    'verification': {'css': 'iframe[src*=captcha], #captcha_container, .captcha_verify_container'},
    'login_form': {'css': 'input[type=tel], input[placeholder*=手机号], input[autocomplete=one-time-code]'},
    'private_fields': 'input:not([type=button]):not([type=submit]), textarea, [contenteditable=true]',
    'countdown_pattern': r'(\d{1,3})\s*(?:秒|s后)',
    'verification_text': ('安全验证', '滑块', '身份验证'),
    'rate_text': ('发送频繁', '操作频繁', '发送次数过多'),
    'code_error_text': ('验证码错误', '验证码已过期', '验证码失效'),
    'error_text': ('登录失败', '系统异常', '网络异常'),
    'login_paths': ('login',),
}

_MESSAGES = {
    'idle': '尚未开始短信登录', 'opening': '正在准备验证码登录',
    'already_logged_in': '官方窗口已登录', 'sending': '正在发送验证码',
    'verify_send': '请在截图中完成安全验证', 'code_sent': '验证码已发送，请输入短信验证码',
    'submitting': '正在提交验证码', 'code_invalid': '验证码错误或已过期，请重新输入',
    'verify_login': '请在截图中完成身份验证', 'succeeded': '登录成功，请手动恢复采集',
    'rate_limited': '发送或操作过于频繁，请稍后重试',
    'failed': '登录失败，请重新发码或改用远程桌面', 'timeout': '登录超时，请重新开始',
    'cancelled': '已取消短信登录',
}
TERMINAL = frozenset(('already_logged_in', 'succeeded', 'rate_limited', 'failed', 'timeout', 'cancelled'))
VERIFY = frozenset(('verify_send', 'verify_login'))
SHANGHAI = ZoneInfo('Asia/Shanghai')


class SmsLoginError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class LoginBusy(SmsLoginError):
    def __init__(self, code='login_busy'):
        super().__init__(code)


class LoginCoordinator:
    """Lease spans browser cleanup, not merely the visible terminal state."""
    def __init__(self):
        self._lock = threading.Lock()
        self._lease = self._fetch_lock = None

    def acquire(self, fetch_lock=None):
        with self._lock:
            if self._lease is not None:
                raise LoginBusy()
            if fetch_lock is not None and not fetch_lock.acquire(blocking=False):
                raise LoginBusy('busy_fetching')
            self._lease, self._fetch_lock = object(), fetch_lock
            return self._lease

    def release(self, lease):
        with self._lock:
            if lease is not self._lease or lease is None:
                return
            if self._fetch_lock is not None:
                self._fetch_lock.release()
            self._lease = self._fetch_lock = None


LOGIN_COORDINATOR = LoginCoordinator()


class VerificationSolver(Protocol):
    """Future app-side extension, invoked once on entering either verify state.

    Return True only if handled. False or any exception falls back to manual
    screenshot/input. Must be bounded and honor cancel; never log/copy secrets.
    No solver is registered/shipped here. Even True still requires subsequent
    official-page probes to confirm progress; it cannot declare login success.
    """
    def solve(self, driver, phase: str, cancel: threading.Event) -> bool: ...


@dataclass(repr=False)
class _Action:
    kind: str
    value: object = None
    nonce: bytes = b''
    done: threading.Event = field(default_factory=threading.Event)
    error: str = ''


class SmsLoginSession:
    states = tuple(_MESSAGES)

    def __init__(self, phone, now):
        self.state = 'opening'
        self.phone = phone
        self.masked_phone = phone[:3] + '****' + phone[-4:]
        self.started = self.entered = now
        self.resend_at = 0
        self.nonce = b''
        self.code_consumed = True
        self.submissions = self.code_count = self.text_count = 0
        self.used_codes = set()
        self.pointer_times = deque()
        self.actions = deque()
        self.cancelled = threading.Event()
        self.wake = threading.Event()
        self.thread = self.lease = None
        self.timer = None
        self.probe_timeouts = 0
        self.next_probe = self.next_frame = now
        self.tab_clicked = False
        self.frame = None
        self.frame_id = 0
        self.frame_hash = None
        self.pointer_pending = False
        self.manual_verification = True

    def __repr__(self):
        return '<SmsLoginSession>'

    def snapshot(self, now):
        return dict(state=self.state, phone=self.masked_phone,
                    resend_in=max(0, math.ceil(self.resend_at-now)) if self.state not in TERMINAL else 0,
                    attempts_left=max(0, 3-self.submissions) if self.nonce and self.state not in TERMINAL else 0,
                    frame_id=self.frame_id if self.frame else 0, message=_MESSAGES[self.state])


class SmsLoginManager:
    def __init__(self, driver, fetch_lock, *, coordinator=None, solver=None,
                 clock=time.monotonic, wall_clock=time.time, on_success=None):
        self.driver, self._fetch_lock = driver, fetch_lock
        self.coordinator = coordinator if coordinator is not None else LOGIN_COORDINATOR
        self.solver, self._on_success = solver, on_success
        self._clock, self._wall_clock = clock, wall_clock
        self._lock = threading.RLock()
        self._session = None
        self._last_start = -math.inf
        self._send_times = deque()
        self._send_day, self._day_count = None, 0
        self._next_send = 0
        self._frame_version = 0

    def status(self):
        with self._lock:
            return self._snapshot()

    def _snapshot(self):
        if self._session:
            return self._session.snapshot(self._clock())
        return dict(state='idle', phone='', resend_in=0, attempts_left=0,
                    frame_id=0, message=_MESSAGES['idle'])

    def start(self, phone):
        if not isinstance(phone, str) or re.fullmatch(r'1[3-9][0-9]{9}', phone) is None:
            raise SmsLoginError('invalid_phone')
        with self._lock:
            now = self._clock()
            if self._session and (self._session.state not in TERMINAL or self._session.lease is not None):
                raise LoginBusy()
            if now - self._last_start < 10:
                raise SmsLoginError('sms_rate_limited')
            lease = self.coordinator.acquire(self._fetch_lock)
            session = SmsLoginSession(phone, now)
            session.lease = lease
            self._session = session
            self._last_start = now
            try:
                self._launch(session)
            except Exception:
                self._finish(session, 'failed')
                self._release(session)
                raise SmsLoginError('login_failed') from None
            return self._snapshot()

    def _launch(self, session):
        session.thread = threading.Thread(target=self._run, args=(session,), daemon=True)
        self._arm_deadline(session)
        session.thread.start()

    def _arm_deadline(self, session):
        # Cancellation must also interrupt a slow probe/input/extension at the
        # round deadline; do not wait for the next worker iteration.
        if session.timer is not None:
            session.timer.cancel()
        if session.thread is None or session.state in TERMINAL:
            return
        deadline = min(session.started+600,
                       session.entered+300 if session.state == 'code_sent' else math.inf)
        def expire():
            with self._lock:
                if session.timer is timer and session.state not in TERMINAL:
                    if self._clock() >= deadline:
                        self._finish(session, 'timeout')
                    else:
                        self._arm_deadline(session)
        timer = threading.Timer(max(0, deadline-self._clock()), expire)
        timer.daemon = True
        session.timer = timer
        timer.start()

    def _active(self):
        session = self._session
        if session is None or session.state in TERMINAL:
            raise SmsLoginError('login_not_active')
        return session

    def _send_allowed(self, now):
        day = datetime.fromtimestamp(self._wall_clock(), SHANGHAI).date()
        if day != self._send_day:
            self._send_day, self._day_count = day, 0
        while self._send_times and now - self._send_times[0] >= 3600:
            self._send_times.popleft()
        if now < self._next_send:
            raise SmsLoginError('resend_too_early')
        if len(self._send_times) >= 3 or self._day_count >= 6:
            raise SmsLoginError('sms_rate_limited')

    def _begin_send(self, session):
        now = self._clock()
        self._send_allowed(now)
        self._send_times.append(now)
        self._day_count += 1
        self._next_send = session.resend_at = now + 60
        session.nonce = secrets.token_bytes(24)
        session.used_codes.clear()
        session.code_consumed = True
        session.submissions = 0
        self._set(session, 'sending')
        session.actions.append(_Action('send'))
        session.wake.set()

    def resend(self):
        with self._lock:
            session = self._active()
            if session.state not in ('code_sent', 'code_invalid'):
                raise SmsLoginError('code_consumed')
            if self._clock() < session.resend_at:
                raise SmsLoginError('resend_too_early')
            self._begin_send(session)
            return self._snapshot()

    def submit(self, code, *, nonce=None):
        """nonce is an optional app-side generation guard, never a credential.

        Admission always binds queued input to the current per-send nonce. It
        is server-side metadata, omitted from public status. A caller retaining
        a generation may pass it to reject work from a previous send.
        """
        if not isinstance(code, str) or re.fullmatch(r'[0-9]{4,8}', code) is None:
            raise SmsLoginError('invalid_code')
        with self._lock:
            session = self._active()
            digest = hashlib.sha256(session.nonce + code.encode('ascii')).digest()
            if (session.state not in ('code_sent', 'code_invalid') or session.code_consumed
                    or (nonce is not None and nonce != session.nonce) or digest in session.used_codes):
                raise SmsLoginError('code_consumed')
            if session.code_count >= 5:
                raise SmsLoginError('sms_rate_limited')
            session.code_count += 1
            session.submissions += 1
            session.code_consumed = True
            session.used_codes.add(digest)
            self._set(session, 'submitting')
            session.actions.append(_Action('code', code, session.nonce))
            session.wake.set()
            return self._snapshot()

    def type_into_focused(self, text):
        if not isinstance(text, str) or re.fullmatch(r'[A-Za-z0-9]{1,8}', text) is None:
            raise SmsLoginError('invalid_text')
        with self._lock:
            session = self._verification()
            if session.text_count >= 5:
                raise SmsLoginError('sms_rate_limited')
            session.text_count += 1
            session.actions.append(_Action('text', text))
            session.wake.set()
            return self._snapshot()

    def _verification(self):
        if self._session is None or self._session.state not in VERIFY:
            raise SmsLoginError('not_in_verification')
        return self._session

    def pointer(self, frame_id, kind, points):
        points = validate_points(kind, points)
        with self._lock:
            session = self._verification()
            if type(frame_id) is not int or session.frame is None or frame_id != session.frame_id:
                raise SmsLoginError('stale_frame')
            now = self._clock()
            while session.pointer_times and now-session.pointer_times[0] >= 1:
                session.pointer_times.popleft()
            if len(session.pointer_times) >= 3 or session.pointer_pending:
                raise SmsLoginError('sms_rate_limited')
            session.pointer_times.append(now)
            session.pointer_pending = True
            action = _Action('pointer', (frame_id, kind, points))
            session.actions.append(action)
            session.wake.set()
        if not action.done.wait(12):
            # A timed-out command must not replay later against a changed page.
            with self._lock:
                if session.state not in TERMINAL:
                    self._finish(session, 'cancelled')
            raise SmsLoginError('pointer_timeout')
        if action.error:
            raise SmsLoginError(action.error)
        return self.status()

    def frame(self, frame_id=None):
        with self._lock:
            session = self._verification()
            if session.frame is None:
                raise SmsLoginError('frame_not_ready')
            if frame_id is not None and (type(frame_id) is not int or frame_id != session.frame_id):
                raise SmsLoginError('stale_frame')
            frame = session.frame
            # Metadata (crop origin, zoom, DPR) never leaves this process.
            return dict(frame_id=session.frame_id, width=frame.metadata.width,
                        height=frame.metadata.height, png=frame.png)

    def refresh_frame(self):
        with self._lock:
            session = self._verification()
            session.next_frame = 0
            session.wake.set()
            return self._snapshot()

    def cancel(self):
        with self._lock:
            if self._session and self._session.state not in TERMINAL:
                self._finish(self._session, 'cancelled')
            return self._snapshot()

    def _set(self, session, state):
        if session.state != state:
            session.state, session.entered = state, self._clock()
        if state not in VERIFY:
            session.frame = None
            session.frame_hash = None
        self._arm_deadline(session)

    def _finish(self, session, state):
        self._set(session, state)
        session.phone = None
        session.nonce = b''
        session.used_codes.clear()
        session.code_consumed = True
        session.cancelled.set()
        if session.timer is not None:
            session.timer.cancel()
            session.timer = None
        for action in session.actions:
            action.value, action.nonce = None, b''
            action.error = 'cancelled'
            action.done.set()
        session.actions.clear()
        session.wake.set()

    def _release(self, session):
        with self._lock:
            self.coordinator.release(session.lease)
            session.lease = None

    def _run(self, session):
        try:
            while not session.cancelled.is_set():
                self._step(session)
                with self._lock:
                    due = min(session.next_probe, session.next_frame if session.state in VERIFY else math.inf,
                              session.started+600,
                              session.entered+300 if session.state == 'code_sent' else math.inf)
                    delay = 0 if session.actions else max(0, due-self._clock())
                session.wake.wait(delay)
                session.wake.clear()
        except Exception:
            # Browser/extension exceptions may contain input. Never format them.
            with self._lock:
                if session.state not in TERMINAL:
                    self._finish(session, 'failed')
        finally:
            with self._lock:
                if session.state not in TERMINAL:
                    self._finish(session, 'failed')
            self._release(session)

    def _step(self, session):
        with self._lock:
            now = self._clock()
            if session.state in TERMINAL:
                return
            if now-session.started >= 600 or (session.state == 'code_sent' and now-session.entered >= 300):
                self._finish(session, 'timeout')
                return
            action = session.actions.popleft() if session.actions else None
        if action is not None:
            self._action(session, action)
            return
        if now >= session.next_probe:
            try:
                probe = self.driver.probe()  # Driver bounds one read-only JS call to 5s.
            except DriverTimeout:
                with self._lock:
                    session.probe_timeouts += 1
                    if session.probe_timeouts >= 3 and session.state not in TERMINAL:
                        self._finish(session, 'failed')
                    session.next_probe = self._clock()+1
                    # Do not turn the first probe timeout into a terminal
                    # screenshot timeout in this same iteration.
                    session.next_frame = max(session.next_frame, session.next_probe)
                return
            else:
                with self._lock:
                    session.probe_timeouts = 0
                self._observe(session, probe)
            session.next_probe = self._clock()+1
        with self._lock:
            capture = session.state in VERIFY and session.manual_verification and self._clock() >= session.next_frame
        if capture:
            self._capture(session)

    @staticmethod
    def _success(probe):
        parsed = urlsplit(probe.get('url', ''))
        return (parsed.scheme == 'https' and parsed.hostname == 'creator.douyin.com'
                and parsed.port in (None, 443) and not (parsed.username or parsed.password)
                and not probe.get('login_page', True) and not probe.get('login_form', True)
                and probe.get('session_present') is True)

    def _observe(self, session, probe):
        solver_phase = None
        with self._lock:
            if session.state in TERMINAL:
                return
            state = session.state
            if self._success(probe):
                # Callback only clears the old QR holder; never resumes collection.
                if self._on_success:
                    self._on_success()
                self._finish(session, 'already_logged_in' if state == 'opening' else 'succeeded')
                return
            if probe.get('rate_limited'):
                self._finish(session, 'rate_limited')
                return
            if probe.get('verification'):
                phase = 'verify_login' if state in ('submitting', 'verify_login', 'code_invalid') else 'verify_send'
                if state != phase:
                    self._set(session, phase)
                    session.next_frame = 0
                    session.manual_verification = self.solver is None
                    solver_phase = phase
                else:
                    # A handled hook which leaves verification on the next
                    # official probe also falls back to manual interaction.
                    session.manual_verification = True
            elif probe.get('error'):
                self._finish(session, 'failed')
            elif probe.get('code_invalid') and state == 'submitting':
                if session.submissions >= 3:
                    self._finish(session, 'failed')
                else:
                    self._set(session, 'code_invalid')
                    session.code_consumed = False
            elif state in ('sending', 'verify_send') and type(probe.get('countdown')) is int and probe['countdown'] > 0:
                if not session.nonce:
                    self._finish(session, 'failed')
                    return
                session.resend_at = max(session.resend_at, self._clock()+probe['countdown'])
                self._next_send = max(self._next_send, session.resend_at)
                session.code_consumed = False
                self._set(session, 'code_sent')
            elif state == 'code_sent' and type(probe.get('countdown')) is int:
                session.resend_at = max(session.resend_at, self._clock()+probe['countdown'])
                self._next_send = max(self._next_send, session.resend_at)
            elif state == 'opening':
                found = probe.get('found', {})
                if type(probe.get('countdown')) is int and probe['countdown'] > 0:
                    self._next_send = max(self._next_send, self._clock()+max(60, probe['countdown']))
                    self._finish(session, 'rate_limited')
                elif found.get('phone') and found.get('send'):
                    try:
                        self._begin_send(session)
                    except SmsLoginError:
                        self._finish(session, 'rate_limited')
                elif found.get('tab') and not session.tab_clicked:
                    session.tab_clicked = True
                    session.actions.append(_Action('tab'))
                elif self._clock()-session.entered >= 15 or not found.get('tab'):
                    self._finish(session, 'failed')
            elif state in ('sending', 'submitting') and self._clock()-session.entered >= 15:
                self._finish(session, 'failed')
        if solver_phase and self.solver is not None:
            handled = False
            try:
                handled = self.solver.solve(self.driver, solver_phase, session.cancelled) is True
            except Exception:
                pass  # No exception payload logging; manual frame is next.
            with self._lock:
                session.manual_verification = not handled
                if handled:
                    session.next_frame = session.next_probe = self._clock()+1
        # No-op/absence/failure reach the manual capture path immediately.

    def _click_element(self, session, key):
        rect = self.driver.element_rect(key)
        if rect is None:
            raise DriverError('selector_missing')
        self.driver.click(rect['x']+rect['width']/2, rect['y']+rect['height']/2, cancel=session.cancelled)

    def _action(self, session, action):
        try:
            if session.cancelled.is_set():
                raise DriverError('cancelled')
            if action.kind == 'send':
                self._click_element(session, 'phone')
                self.driver.type_text(session.phone, cancel=session.cancelled)
                self._click_element(session, 'send')
            elif action.kind == 'tab':
                self._click_element(session, 'tab')
            elif action.kind == 'code':
                if action.nonce != session.nonce:
                    raise DriverError('code_consumed')
                self._click_element(session, 'code')
                self.driver.type_text(action.value, cancel=session.cancelled)
                self._click_element(session, 'submit')
            elif action.kind == 'text':
                if session.state not in VERIFY:
                    raise DriverError('not_in_verification')
                self.driver.type_text(action.value, cancel=session.cancelled)
            elif action.kind == 'pointer':
                frame_id, kind, points = action.value
                with self._lock:
                    if session.state not in VERIFY:
                        raise DriverError('not_in_verification')
                    if not session.frame or frame_id != session.frame_id:
                        raise DriverError('stale_frame')
                    meta = session.frame.metadata
                mapped = [(*meta.css_point(u, v), stamp) for u, v, stamp in points]
                if kind == 'tap':
                    self.driver.click(*mapped[0][:2], cancel=session.cancelled, frame=meta)
                else:
                    self.driver.drag(mapped, cancel=session.cancelled, frame=meta)
                self._capture(session)
        except Exception as exc:
            action.error = exc.code if isinstance(exc, DriverError) else 'driver_failed'
            with self._lock:
                if session.state not in TERMINAL and action.error not in ('stale_frame', 'not_in_verification', 'out_of_bounds'):
                    self._finish(session, 'failed')
        finally:
            action.value, action.nonce = None, b''
            with self._lock:
                if action.kind == 'pointer':
                    session.pointer_pending = False
            action.done.set()

    def _capture(self, session):
        rect = self.driver.element_rect('verification')
        if rect:
            rect = dict(x=rect['x']-16, y=rect['y']-16,
                        width=rect['width']+32, height=rect['height']+32)
        frame = self.driver.grab(rect)
        if len(frame.png) > 300*1024:
            raise DriverError('frame_too_large')
        # A transform change must invalidate input even if its pixels match.
        digest = hashlib.sha256(frame.png).digest(), frame.metadata
        with self._lock:
            if session.state not in VERIFY:
                return
            if digest != session.frame_hash:
                self._frame_version += 1
                session.frame_id = self._frame_version
                session.frame, session.frame_hash = frame, digest
            session.next_frame = self._clock()+.7
