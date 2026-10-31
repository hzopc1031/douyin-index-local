"""Memory-only SMS login worker for the existing official browser window.

App wiring passes Api._lock, Api._login_coordinator and an on_success callback
clearing qr.holder. Short methods only admit work/read snapshots;
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
from zoneinfo import ZoneInfo

from login_driver import DriverError, DriverTimeout, is_official_origin, validate_points

# UNCALIBRATED: all DOM selectors and text detection belong in this table.
# Calibration must change this table only, never add page-specific driver logic.
SMS_SELECTORS = {
    'elements': ('tab', 'phone', 'send', 'countdown', 'code', 'submit', 'verification', 'login_form',
                 'slider_handle', 'slider_track', 'slider_image', 'slider_piece'),
    'tab': {'css': 'button, [role=tab], span', 'text': ('验证码登录', '短信登录')},
    'phone': {'css': 'input[type=tel], input[placeholder*=手机号]'},
    'send': {'css': 'button, [role=button]', 'text': ('获取验证码', '发送验证码', '重新发送')},
    'countdown': {'css': 'button, [role=button], span', 'text': ('秒', 's后')},
    'code': {'css': 'input[autocomplete=one-time-code], input[placeholder*=验证码]'},
    'submit': {'css': 'button, [role=button]', 'text': ('登录',)},
    'verification': {'css': 'iframe[src*=captcha], #captcha_container, .captcha_verify_container'},
    'login_form': {'css': 'input[type=tel], input[placeholder*=手机号], input[autocomplete=one-time-code]'},
    'slider_handle': {'css': '.secsdk-captcha-drag-icon'},
    'slider_track': {'css': '.secsdk-captcha-drag-wrapper'},
    'slider_image': {'css': '#captcha-verify-image'},
    'slider_piece': {'css': '.captcha_verify_img_slide'},
    'challenge_assets': 'img, canvas, iframe',
    'challenge_attributes': ('src', 'data-challenge-id'),
    'rotation_text': ('旋转', '转正'),
    'gap_text': ('拖动滑块完成拼图', '拖动滑块使拼图完整'),
    'private_fields': 'input:not([type=button]):not([type=submit]), textarea, [contenteditable=true]',
    'countdown_pattern': r'(\d{1,3})\s*(?:秒|s后)',
    'verification_text': ('安全验证', '滑块', '身份验证'),
    'rate_text': ('发送频繁', '操作频繁', '发送次数过多'),
    'blocked_text': ('账号异常', '拒绝访问', '访问被拒绝'),
    'verification_error_text': ('验证失败', '请重试'),
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
FRAME_BASE64_LIMIT = 300 * 1024
FRAME_PNG_LIMIT = FRAME_BASE64_LIMIT // 4 * 3


def validate_sms_payload(action, body):
    """Shared strict bridge/HTTP schemas; never include input in errors."""
    fields = {'start': {'phone'}, 'code': {'code'}, 'text': {'text'},
              'pointer': {'frame', 'kind', 'points'}, 'frame': {'frame'},
              'resend': set(), 'cancel': set(), 'status': set()}
    if action not in fields or type(body) is not dict or set(body) != fields[action]:
        raise SmsLoginError('invalid_payload')
    patterns = {'start': ('phone', r'1[3-9][0-9]{9}'),
                'code': ('code', r'[0-9]{4,8}'), 'text': ('text', r'[A-Za-z0-9]{1,8}')}
    if action in patterns:
        key, pattern = patterns[action]
        if not isinstance(body[key], str) or re.fullmatch(pattern, body[key]) is None:
            raise SmsLoginError('invalid_' + key)
    if action in ('frame', 'pointer'):
        if type(body['frame']) is not int or not 1 <= body['frame'] <= 2**53-1:
            raise SmsLoginError('invalid_frame')
    if action == 'pointer':
        try:
            validate_points(body['kind'], body['points'])
        except DriverError as exc:
            raise SmsLoginError(exc.code) from None
    return body


def public_sms_status(value):
    """Allowlist snapshot fields at the transport boundary, including copy."""
    state = value.get('state')
    if state not in _MESSAGES:
        raise SmsLoginError('browser_unavailable')
    mode = value.get('verification_mode') if state in VERIFY else None
    if state in VERIFY and mode not in ('auto', 'manual', 'desktop'):
        raise SmsLoginError('browser_unavailable')
    phone = value.get('phone', '')
    if not isinstance(phone, str) or (phone and re.fullmatch(r'1[3-9][0-9]\*{4}[0-9]{4}', phone) is None):
        raise SmsLoginError('browser_unavailable')
    message = {'auto': '自动验证中…', 'manual': _MESSAGES[state],
               'desktop': '请改用远程桌面完成验证'}.get(mode, _MESSAGES[state])
    result = dict(state=state, phone=phone, verification_mode=mode, message=message)
    for key in ('resend_in', 'attempts_left', 'frame_id', 'challenge_epoch', 'frame_width', 'frame_height'):
        number = value.get(key, 0)
        if type(number) is not int or not 0 <= number <= 2**53-1:
            raise SmsLoginError('browser_unavailable')
        result[key] = number
    return result


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
    """Local screenshot-only extension; returns normalized drag points or None.

    No driver is exposed: the manager owns the three-submission round budget
    and the SAME replayer as manual input. No network, logging or disk writes.
    Must be bounded and honor cancel. None/exception falls back to manual;
    a trajectory still requires official-page probes to confirm progress.
    """
    def solve(self, capture, challenge: dict, cancel: threading.Event): ...


_DEFAULT_SOLVER = object()


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
        self.state_generation = 0
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
        self.opening_reset = False
        self.frame = None
        self.frame_id = 0
        self.frame_hash = None
        self.frame_floor = 0
        self.challenge_key = None
        self.challenge_epoch = 0
        self.challenge = {}
        self.challenge_since = now
        self.auto_attempted_epoch = 0
        self.auto_submissions = 0
        self.auto_challenges = set()
        self.auto_disabled = False
        self.auto_pending = None
        self.manual_pending = None
        self.verification_mode = 'manual'
        self.desktop_requested = False
        self.pointer_pending = False

    def __repr__(self):
        return '<SmsLoginSession>'

    def snapshot(self, now):
        message = _MESSAGES[self.state]
        if self.state in VERIFY:
            message = {'auto': '正在尝试本地滑块验证', 'manual': message,
                       'desktop': '请改用远程桌面完成验证'}[self.verification_mode]
        return dict(state=self.state, phone=self.masked_phone,
                    resend_in=max(0, math.ceil(self.resend_at-now)) if self.state not in TERMINAL else 0,
                    attempts_left=max(0, 3-self.submissions) if self.nonce and self.state not in TERMINAL else 0,
                    frame_id=self.frame_id if self.frame else 0, message=message,
                    challenge_epoch=self.challenge_epoch,
                    verification_mode=self.verification_mode if self.state in VERIFY else None,
                    frame_width=self.frame.metadata.width if self.frame else 0,
                    frame_height=self.frame.metadata.height if self.frame else 0)


class SmsLoginManager:
    def __init__(self, driver, fetch_lock, *, coordinator=None, solver=_DEFAULT_SOLVER,
                 clock=time.monotonic, wall_clock=time.time, on_success=None):
        self.driver, self._fetch_lock = driver, fetch_lock
        self.coordinator = coordinator if coordinator is not None else LOGIN_COORDINATOR
        if solver is _DEFAULT_SOLVER:
            from slider_solver import LocalGapSolver
            solver = LocalGapSolver()  # Heavy optional imports/model load only on solve.
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
                    frame_id=0, challenge_epoch=0, verification_mode=None,
                    frame_width=0, frame_height=0, message=_MESSAGES['idle'])

    def start(self, phone):
        if not isinstance(phone, str) or re.fullmatch(r'1[3-9][0-9]{9}', phone) is None:
            raise SmsLoginError('invalid_phone')
        with self._lock:
            now = self._clock()
            if self._session and (self._session.state not in TERMINAL or self._session.lease is not None):
                raise LoginBusy()
            if now - self._last_start < 10:
                raise SmsLoginError('sms_rate_limited')
            # Reject known local send limits during admission, so transports
            # return 429 instead of accepting work that will immediately fail.
            self._send_allowed(now)
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
            if session.verification_mode == 'auto':
                raise SmsLoginError('verification_busy')
            if session.text_count >= 5:
                raise SmsLoginError('sms_rate_limited')
            session.text_count += 1
            session.auto_disabled = True
            session.actions.append(_Action('text', text))
            session.wake.set()
            return self._snapshot()

    def _verification(self):
        if self._session is None or self._session.state not in VERIFY:
            raise SmsLoginError('not_in_verification')
        if self._session.verification_mode == 'desktop':
            raise SmsLoginError('remote_desktop_required')
        return self._session

    @staticmethod
    def _valid_frame(session, frame_id):
        # Every picture version in the current challenge/layout epoch has the
        # same transform. Animation does not invalidate a human's gesture.
        return (type(frame_id) is int and session.frame is not None
                and session.frame_floor <= frame_id <= session.frame_id)

    def pointer(self, frame_id, kind, points):
        points = validate_points(kind, points)
        with self._lock:
            session = self._verification()
            if not self._valid_frame(session, frame_id):
                raise SmsLoginError('stale_frame')
            if session.verification_mode == 'auto':
                raise SmsLoginError('verification_busy')
            now = self._clock()
            while session.pointer_times and now-session.pointer_times[0] >= 1:
                session.pointer_times.popleft()
            if len(session.pointer_times) >= 3 or session.pointer_pending:
                raise SmsLoginError('sms_rate_limited')
            session.pointer_times.append(now)
            session.pointer_pending = True
            session.auto_disabled = True
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
            return dict(frame_id=session.frame_id, challenge_epoch=session.challenge_epoch, width=frame.metadata.width,
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
            session.state_generation += 1
        if state not in VERIFY:
            session.frame = None
            session.frame_hash = None
            session.challenge_key = None
            session.auto_pending = session.manual_pending = None
            session.desktop_requested = False
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
                    due = min(session.next_probe, session.next_frame if session.state in VERIFY
                              and session.verification_mode != 'desktop' else math.inf,
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
            # Bind the read before releasing admission's lock: a new code/send
            # may be queued even before driver.probe() starts reading the page.
            generation = session.state_generation
        if action is not None:
            self._action(session, action)
            return
        if now >= session.next_probe:
            try:
                probe = self.driver.probe()  # Driver bounds one read-only JS call to 5s.
            except DriverTimeout:
                with self._lock:
                    if session.state in TERMINAL or generation != session.state_generation:
                        return
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
                    if session.state in TERMINAL or generation != session.state_generation:
                        return
                    session.probe_timeouts = 0
                self._observe(session, probe, generation=generation)
            session.next_probe = self._clock()+1
        with self._lock:
            capture = (session.state in VERIFY and session.verification_mode != 'desktop'
                       and self._clock() >= session.next_frame)
        if capture:
            self._capture(session)

    @staticmethod
    def _success(probe):
        return (is_official_origin(probe.get('url'))
                and not probe.get('login_page', True) and not probe.get('login_form', True)
                and probe.get('session_present') is True)

    def _observe(self, session, probe, *, generation):
        try_solver = False
        with self._lock:
            # Check inside the same lock as submit()/resend(), including the
            # interval between a probe returning and its result being applied.
            if session.state in TERMINAL or generation != session.state_generation:
                return
            if not is_official_origin(probe.get('url')):
                self._finish(session, 'failed')
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
            if probe.get('blocked'):
                self._finish(session, 'failed')
                return
            if probe.get('verification'):
                if state == 'opening':
                    # A cancelled round can leave its CAPTCHA on the page.
                    # It cannot belong to this round: no send/nonce exists yet.
                    # Reset once, then require normal send admission on a fresh
                    # page. Never solve/adopt the old send's challenge or code.
                    if session.opening_reset:
                        self._finish(session, 'failed')
                    else:
                        session.opening_reset = True
                        session.actions.append(_Action('reset'))
                    return
                phase = 'verify_login' if state in ('submitting', 'verify_login', 'code_invalid') else 'verify_send'
                self._set(session, phase)
                kind = probe.get('challenge_kind', 'unknown')
                rects = {k: probe.get('rects', {}).get(k) for k in
                         ('verification', 'slider_handle', 'slider_track', 'slider_image', 'slider_piece')}
                key = (phase, kind, probe.get('challenge_key'), repr(rects))
                if key != session.challenge_key:
                    session.challenge_key = key
                    session.challenge_epoch += 1
                    session.challenge_since = self._clock()
                    session.frame = session.frame_hash = None
                    session.next_frame = 0
                    identity = (phase, probe.get('challenge_key'))
                    if identity != session.challenge.get('identity'):
                        # Progress to another puzzle ends the old drag's wait.
                        # Layout/animation changes alone must not extend it.
                        session.manual_pending = None
                    session.challenge = dict(kind=kind, rects=rects, phase=phase,
                                             identity=identity)
                    session.auto_pending = None
                    session.verification_mode = ('auto' if kind == 'gap_slider' and self.solver is not None
                                                 and not session.auto_disabled and session.auto_submissions < 3
                                                 and session.challenge['identity'] not in session.auto_challenges else 'manual')
                if kind == 'rotation' or session.desktop_requested:
                    session.desktop_requested = True
                    session.verification_mode = 'desktop'
                    session.frame = session.frame_hash = None
                elif session.manual_pending is not None:
                    # Allow a short page-response interval. Taps may be part of
                    # a multi-click puzzle; only a completed drag is a submission.
                    if probe.get('verification_failed') or self._clock() >= session.manual_pending:
                        session.desktop_requested = True
                        session.verification_mode = 'desktop'
                        session.frame = session.frame_hash = None
                elif probe.get('verification_failed') or (session.auto_pending is not None
                                                         and self._clock() >= session.auto_pending):
                    self._manual(session)
                try_solver = (session.verification_mode == 'auto' and session.auto_pending is None
                              and session.auto_attempted_epoch != session.challenge_epoch
                              and self._clock()-session.challenge_since >= .7)
            elif probe.get('error'):
                self._finish(session, 'failed')
            elif probe.get('code_invalid') and state in ('submitting', 'verify_login'):
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
        if try_solver:
            self._try_solver(session)

    @staticmethod
    def _manual(session):
        session.auto_disabled = True  # Any solver failure ends automation for this round.
        session.auto_pending = None
        session.verification_mode = 'manual'

    def _try_solver(self, session):
        try:
            self._capture(session)
            with self._lock:
                if session.state not in VERIFY or session.cancelled.is_set():
                    return
                epoch, frame = session.challenge_epoch, session.frame
                generation = session.state_generation
                session.auto_attempted_epoch = epoch
                challenge = session.challenge
            points = self.solver.solve(frame, challenge, session.cancelled)
            if points is None:
                raise DriverError('not_handled')
            points = validate_points('drag', points)
            # Re-probe after inference: no input if the question/layout changed,
            # or if a stop/success signal appeared while the model was working.
            self._observe(session, self.driver.probe(), generation=generation)
            with self._lock:
                if (session.state not in VERIFY or session.cancelled.is_set()
                        or session.challenge_epoch != epoch or session.verification_mode != 'auto'
                        or session.auto_submissions >= 3):
                    raise DriverError('stale_frame')
                session.auto_submissions += 1  # Count before pressing, including interrupted replays.
                session.auto_challenges.add(challenge['identity'])
            self._replay(session, 'drag', points, frame.metadata)
            with self._lock:
                session.auto_pending = self._clock()+3
            self._capture(session)
        except Exception as exc:
            # Model/driver payloads may contain input: never format/log them.
            with self._lock:
                if (session.state not in TERMINAL and isinstance(exc, DriverError)
                        and exc.code in ('untrusted_origin', 'navigation_changed')):
                    self._finish(session, 'failed')
                elif session.state in VERIFY and session.verification_mode != 'desktop':
                    self._manual(session)

    def _replay(self, session, kind, points, meta):
        mapped = [(*meta.css_point(u, v), stamp) for u, v, stamp in points]
        if kind == 'tap':
            self.driver.click(*mapped[0][:2], cancel=session.cancelled, frame=meta)
        else:
            self.driver.drag(mapped, cancel=session.cancelled, frame=meta)

    def _click_element(self, session, key):
        self.driver.require_official_origin()
        rect = self.driver.element_rect(key)
        if rect is None:
            raise DriverError('selector_missing')
        self.driver.click(rect['x']+rect['width']/2, rect['y']+rect['height']/2, cancel=session.cancelled)

    def _action(self, session, action):
        try:
            if session.cancelled.is_set():
                raise DriverError('cancelled')
            self.driver.require_official_origin()
            if action.kind == 'reset':
                self.driver.reset_login(cancel=session.cancelled)
                session.tab_clicked = False
                session.next_probe = 0
            elif action.kind == 'send':
                self._click_element(session, 'phone')
                self.driver.require_official_origin()
                self.driver.type_text(session.phone, cancel=session.cancelled)
                self._click_element(session, 'send')
            elif action.kind == 'tab':
                self._click_element(session, 'tab')
            elif action.kind == 'code':
                if action.nonce != session.nonce:
                    raise DriverError('code_consumed')
                self._click_element(session, 'code')
                self.driver.require_official_origin()
                self.driver.type_text(action.value, cancel=session.cancelled)
                self._click_element(session, 'submit')
            elif action.kind == 'text':
                if session.state not in VERIFY:
                    raise DriverError('not_in_verification')
                self.driver.type_text(action.value, cancel=session.cancelled)
            elif action.kind == 'pointer':
                frame_id, kind, points = action.value
                # A request can arrive just before the next scheduled probe.
                # Validate the live challenge/layout before applying old pixels.
                with self._lock:
                    generation = session.state_generation
                self._observe(session, self.driver.probe(), generation=generation)
                if session.state in VERIFY and session.verification_mode == 'manual':
                    self._capture(session)
                with self._lock:
                    if session.state not in VERIFY:
                        raise DriverError('not_in_verification')
                    if session.verification_mode != 'manual':
                        raise DriverError('not_in_verification')
                    if not self._valid_frame(session, frame_id):
                        raise DriverError('stale_frame')
                    meta = session.frame.metadata
                self._replay(session, kind, points, meta)
                if kind == 'drag':
                    session.manual_pending = self._clock()+3
                self._capture(session)
        except Exception as exc:
            action.error = exc.code if isinstance(exc, DriverError) else 'driver_failed'
            with self._lock:
                if session.state not in TERMINAL and action.error not in ('stale_frame', 'not_in_verification', 'out_of_bounds'):
                    if action.error in ('untrusted_origin', 'navigation_changed'):
                        self._finish(session, 'failed')
                    elif action.kind == 'pointer' and session.state in VERIFY:
                        session.desktop_requested = True
                        session.verification_mode = 'desktop'
                        session.frame = session.frame_hash = None
                    else:
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
        if len(frame.png) > FRAME_PNG_LIMIT:
            raise DriverError('frame_too_large')
        # Picture versions drive refresh; only a challenge/transform change
        # invalidates coordinates. Do not reject gestures because of animation.
        digest = hashlib.sha256(frame.png).digest(), frame.metadata
        with self._lock:
            if session.state not in VERIFY:
                return
            if digest != session.frame_hash:
                self._frame_version += 1
                if session.frame is None or session.frame.metadata != frame.metadata:
                    if session.frame is not None:
                        session.challenge_epoch += 1
                    session.frame_floor = self._frame_version
                session.frame_id = self._frame_version
                session.frame, session.frame_hash = frame, digest
            session.next_frame = self._clock()+.7
