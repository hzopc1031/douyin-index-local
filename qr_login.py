"""On-demand QR login prototype. Credentials live only in process memory."""
import logging
import math
import os
import sys
import threading
import time
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import urlsplit

import requests

from pathlib import Path
from browser_profile import persist_enabled, prepare_profile, ProfileLock, ProfileBusy, clear_stale_singleton

import client
from sms_login import LOGIN_COORDINATOR

LOGIN_URL = client.PAGE_URL
POLL_INTERVAL = 1


_mode_warned = False
_mode_lock = threading.Lock()


def login_mode():
    global _mode_warned
    value = os.environ.get('INDEX_HUB_LOGIN_MODE', 'desktop')
    if value in ('desktop', 'qr'):
        return value
    with _mode_lock:
        if not _mode_warned:
            logging.getLogger('index_hub.qr_login').warning('Invalid INDEX_HUB_LOGIN_MODE; using desktop')
            _mode_warned = True
    return 'desktop'


_browser_mode_warned = False


def qr_browser_mode():
    global _browser_mode_warned
    value = os.environ.get('INDEX_HUB_QR_BROWSER', 'headed')
    if value in ('headed', 'headless'):
        return value
    with _mode_lock:
        if not _browser_mode_warned:
            logging.getLogger('index_hub.qr_login').warning('Invalid INDEX_HUB_QR_BROWSER; using headed')
            _browser_mode_warned = True
    return 'headed'


def _timeout(value):
    try:
        result = float(value)
        if math.isfinite(result) and result > 0:
            return result
    except (ValueError, TypeError):
        pass
    return 180


QR_TIMEOUT_SECONDS = _timeout(os.environ.get('INDEX_HUB_QR_TIMEOUT'))
# 上线后需按真实页面校准。
SELECTORS = {
    'qr_image': 'img[class*=qrcode], img[src^="data:image"]',
    'expired': ('二维码已失效', '二维码已过期', '已过期'),
    'verification': 'iframe[src*=captcha], #captcha_container, .captcha_verify_container',
    'verification_text': ('安全验证', '滑块'),
    'session_cookies': ('sessionid', 'sessionid_ss', 'sid_tt', 'sid_guard'),
    'logged_in': 'input[type=tel]',
    'login_text': '扫码登录',
}
logger = logging.getLogger('index_hub.qr_login')


class QrBusy(ValueError):
    code = 'qr_busy'


class QrUnsafeDebug(ValueError):
    code = 'qr_unsafe_debug'


class QrNoDisplay(ValueError):
    code = 'qr_no_display'


class CookieRejected(ValueError):
    pass


def narrow_cookies(browser_cookies, host, now=None, *, allow_insecure=False, stats=None):
    """Adapted from commit 022d6ee login_transfer.make_payload / cloud_probe.validate_payload.

    Only creator.douyin.com may inherit douyin.com cookies. The insecure
    loopback exception exists solely for synthetic browser tests.
    """
    counts = dict(partitioned_skipped=0, expired_skipped=0, duplicates_resolved=0, accepted=0)
    if isinstance(stats, dict):
        stats.update(counts)
        counts = stats
    if not isinstance(browser_cookies, list) or len(browser_cookies) > 200:
        raise CookieRejected('Cookie 数量无效')
    now = time.time() if now is None else now
    accepted = {host}
    if host == 'creator.douyin.com':
        accepted.add('douyin.com')
    result, seen = [], {}
    for item in browser_cookies:
        if not isinstance(item, dict):
            raise CookieRejected('Cookie 格式无效')
        if 'partitionKey' in item:
            counts['partitioned_skipped'] += 1
            continue
        domain = item.get('domain', '')
        if not isinstance(domain, str) or domain.lstrip('.').lower() not in accepted:
            raise CookieRejected('Cookie 域无效')
        name, value, path = item.get('name'), item.get('value'), item.get('path')
        if (not isinstance(name, str) or not name or any(c in name for c in '=;\r\n\x00')
                or not isinstance(value, str) or len(value) > 4096
                or any(c in value for c in '\r\n\x00')
                or not isinstance(path, str) or not path.startswith('/')
                or any(c in path for c in '\r\n\x00')):
            raise CookieRejected('Cookie 格式无效')
        expires = item.get('expires', -1)
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires):
            raise CookieRejected('Cookie 有效期无效')
        if expires != -1 and expires <= now:
            counts['expired_skipped'] += 1
            continue
        key = (name, path)
        normalized_domain = domain.lower()
        rank = 2 if normalized_domain == host else 1 if normalized_domain == '.' + host else 0
        if key in seen:
            counts['duplicates_resolved'] += 1
            position, previous_rank = seen[key]
            if rank <= previous_rank:
                continue
        else:
            position = len(result)
            result.append(None)
        seen[key] = (position, rank)
        result[position] = dict(name=name, value=value, domain=host, path=path,
                           secure=not (allow_insecure and host == '127.0.0.1'
                                       and item.get('secure') is False), expires=expires)
        counts['accepted'] = len(result)
    return result


class SessionHolder:
    """Memory-only credential handoff; callers clear/close each returned session."""
    def __init__(self, clock=time.monotonic):
        self._lock = threading.Lock()
        self._cookies = []
        self._user_agent = ''
        self._clock = clock
        self.obtained_at = None

    def __repr__(self):
        return '<SessionHolder>'

    def _clear(self):
        for item in self._cookies:
            item['value'] = ''
        self._cookies.clear()
        self._user_agent = ''
        self.obtained_at = None

    def clear(self):
        with self._lock:
            self._clear()

    def put(self, cookies, user_agent):
        if not isinstance(user_agent, str) or not user_agent or '\r' in user_agent or '\n' in user_agent:
            raise ValueError('浏览器标识无效')
        with self._lock:
            self._clear()
            self._cookies = [dict(item) for item in cookies]
            self._user_agent = user_agent
            self.obtained_at = self._clock()

    def has_session(self):
        with self._lock:
            return bool(self._cookies)

    def info(self):
        with self._lock:
            return dict(has_session=bool(self._cookies),
                        age_seconds=max(0, int(self._clock() - self.obtained_at))
                        if self._cookies else None,
                        headless_ua=bool(self._cookies) and 'HeadlessChrome' in self._user_agent)

    def take_session(self):
        with self._lock:
            if not self._cookies:
                raise ValueError('尚无登录会话')
            session = requests.Session()
            session.trust_env = False
            session.cookies.set_policy(DefaultCookiePolicy(
                strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
            try:
                for item in self._cookies:
                    cookie = requests.cookies.create_cookie(
                        **{k: item[k] for k in ('name', 'value', 'domain', 'path', 'secure')},
                        expires=None if item['expires'] == -1 else int(item['expires']))
                    cookie.domain_specified = False
                    cookie.domain_initial_dot = False
                    session.cookies.set_cookie(cookie)
                return session, self._user_agent
            except Exception:
                session.cookies.clear()
                session.close()
                raise


_MESSAGES = {
    'idle': '尚未开始扫码登录', 'starting': '正在打开登录页面',
    'qr_ready': '请使用抖音 App 扫码登录', 'expired': '二维码已失效，请刷新',
    'verification_required': '需要安全验证，请改用远程桌面登录',
    'succeeded': '登录成功', 'timeout': '扫码登录超时，请重试',
    'failed': '扫码登录失败，请改用远程桌面登录', 'cancelled': '已取消扫码登录',
}


class QrLoginSession:
    states = tuple(_MESSAGES)

    def __init__(self, clock=time.monotonic, timeout=QR_TIMEOUT_SECONDS):
        self._lock = threading.Lock()
        self.cancelled = threading.Event()
        self._clock, self._deadline = clock, clock() + timeout
        self._state, self._png, self._version = 'idle', None, 0
        self.thread = None

    def _set(self, state):
        with self._lock:
            self._state = state

    def _image(self, png):
        with self._lock:
            if png != self._png:
                self._png = png
                self._version += 1
            self._state = 'qr_ready'

    def snapshot(self):
        with self._lock:
            return dict(state=self._state, qr_version=self._version,
                        expires_in=max(0, math.ceil(self._deadline - self._clock()))
                        if self._state in ('starting', 'qr_ready') else 0,
                        message=_MESSAGES[self._state])

    def qr_png(self):
        with self._lock:
            return self._png


def _playwright():
    from playwright.sync_api import sync_playwright
    return sync_playwright()


class QrLoginManager:
    def __init__(self, browser_factory=None, *, allowed_origin='https://creator.douyin.com',
                 login_url=LOGIN_URL, cookie_url=client.TREND_URL,
                 clock=time.monotonic, timeout=None, allow_insecure=False, browser_mode=None, profile_dir=None,
                 coordinator=None, fetch_lock=None):
        self._origin = urlsplit(allowed_origin)
        if (self._origin.hostname is None or self._origin.username or self._origin.password
                or self._origin.path not in ('', '/') or self._origin.query or self._origin.fragment
                or (self._origin.scheme != 'https' and not (
                    allow_insecure and self._origin.scheme == 'http' and self._origin.hostname == '127.0.0.1'))):
            raise ValueError('登录来源无效')
        for url in (login_url, cookie_url):
            if not self._allowed(url):
                raise ValueError('登录地址必须与允许来源一致')
        self._profile_dir = profile_dir
        self._persistent_open = False
        self._factory = browser_factory or _playwright
        self._login_url, self._cookie_url = login_url, cookie_url
        self._clock = clock
        self._timeout = _timeout(timeout if timeout is not None else os.environ.get('INDEX_HUB_QR_TIMEOUT', QR_TIMEOUT_SECONDS))
        self._allow_insecure = allow_insecure
        self._browser_mode = qr_browser_mode() if browser_mode is None else browser_mode
        if self._browser_mode not in ('headed', 'headless'):
            raise ValueError('浏览器模式无效')
        self._lock = threading.Lock()
        self._session = None
        self._browser = None
        self._last_browser = None
        self.holder = SessionHolder(clock)
        self.coordinator = coordinator if coordinator is not None else LOGIN_COORDINATOR
        self._fetch_lock = fetch_lock

    def _allowed(self, url):
        parsed = urlsplit(url)
        return (parsed.scheme, parsed.hostname, parsed.port) == (
            self._origin.scheme, self._origin.hostname, self._origin.port) and not (parsed.username or parsed.password)

    def start(self):
        with self._lock:
            if 'DEBUG' in os.environ or 'PWDEBUG' in os.environ:
                raise QrUnsafeDebug('请先关闭浏览器调试环境变量')
            if self._browser_mode == 'headed' and not os.environ.get('DISPLAY'):
                raise QrNoDisplay('有头扫码需要桌面显示环境（DISPLAY）')
            if self._session and (self._session.snapshot()['state'] in ('starting', 'qr_ready')
                                  or (self._session.thread is not None and self._session.thread.is_alive())):
                raise QrBusy('扫码登录正在进行')
            lease = self.coordinator.acquire(self._fetch_lock)
            session = QrLoginSession(self._clock, self._timeout)
            session._set('starting')
            self._session = session
            try:
                session.thread = threading.Thread(target=self._run_coordinated, args=(session, lease), daemon=True)
                session.thread.start()
            except Exception:
                session._set('failed')
                self.coordinator.release(lease)
                raise
            return session

    def _run_coordinated(self, session, lease):
        try:
            self._run(session)
        finally:
            self.coordinator.release(lease)

    def holder_info(self):
        return self.holder.info()

    def browser_open(self):
        browser = self._last_browser
        return self._persistent_open or bool(browser is not None and browser.is_connected())

    def status(self):
        with self._lock:
            snapshot = self._session.snapshot() if self._session else dict(
                state='idle', qr_version=0, expires_in=0, message=_MESSAGES['idle'])
            return {**snapshot, 'browser_mode': self._browser_mode}

    def qr_png(self):
        with self._lock:
            return self._session.qr_png() if self._session else None

    def cancel(self):
        with self._lock:
            if self._session:
                self._session.cancelled.set()

    def _run(self, session):
        context = browser = profile_lock = None
        cookies = []
        final = 'failed'
        stage = 'launch'
        stats = dict(partitioned_skipped=0, expired_skipped=0, duplicates_resolved=0, accepted=0)
        try:
            with self._factory() as playwright:
                try:
                    # Playwright 默认即不开 Chromium 沙箱（会加 --no-sandbox），这里显式写出以免误解；
                    # 开启需另做 AppArmor/userns 配置。
                    directory = self._profile_dir
                    if directory is None and persist_enabled():
                        directory = prepare_profile(Path(__file__).resolve().parent, 'qr-chromium')
                    if directory is not None:
                        profile_lock = ProfileLock(directory).acquire()
                        clear_stale_singleton(directory)
                        context = playwright.chromium.launch_persistent_context(
                            str(directory), headless=(self._browser_mode == 'headless'),
                            chromium_sandbox=False, accept_downloads=False, locale='zh-CN')
                        self._persistent_open = True
                        page = context.pages[0] if context.pages else context.new_page()
                    else:
                        browser = playwright.chromium.launch(
                            headless=(self._browser_mode == 'headless'), chromium_sandbox=False)
                        self._browser = self._last_browser = browser
                        context = browser.new_context(accept_downloads=False, locale='zh-CN')
                        page = context.new_page()
                    page.set_default_timeout(1000)
                    stage = 'goto'
                    page.goto(self._login_url, timeout=min(30000, self._timeout * 1000))
                    while True:
                        stage = 'poll'
                        if session.cancelled.is_set():
                            final = 'cancelled'
                            break
                        if self._clock() >= session._deadline:
                            final = 'timeout'
                            break
                        text = page.locator('body').inner_text()
                        if (page.locator(SELECTORS['verification']).count()
                                or any(word in text for word in SELECTORS['verification_text'])):
                            final = 'verification_required'
                            break
                        if any(word in text for word in SELECTORS['expired']):
                            final = 'expired'
                            break
                        if (self._allowed(page.url) and 'login' not in urlsplit(page.url).path.lower()
                                and not (page.locator(SELECTORS['logged_in']).count()
                                         and SELECTORS['login_text'] in text)):
                            stage = 'cookies'
                            cookies = context.cookies([self._cookie_url])
                            narrowed = narrow_cookies(cookies, self._origin.hostname,
                                                      allow_insecure=self._allow_insecure, stats=stats)
                            try:
                                if any(item['name'] in SELECTORS['session_cookies'] for item in narrowed):
                                    stage = 'ua'
                                    user_agent = page.evaluate('navigator.userAgent')
                                    if 'HeadlessChrome' in user_agent:
                                        if self._browser_mode == 'headed':
                                            logger.warning('浏览器标识含 HeadlessChrome，已拒绝会话')
                                            break
                                        logger.warning('无头浏览器标识，风控风险较高')
                                    with self._lock:
                                        if session.cancelled.is_set():
                                            final = 'cancelled'
                                        else:
                                            self.holder.put(narrowed, user_agent)
                                            final = 'succeeded'
                                    break
                            finally:
                                for item in narrowed:
                                    item['value'] = ''
                                narrowed.clear()
                        stage = 'poll'
                        images = page.locator(SELECTORS['qr_image'])
                        for i in range(images.count()):
                            element = images.nth(i)
                            box = element.bounding_box()
                            if element.is_visible() and box and box['width'] >= 100 and box['height'] >= 100:
                                session._image(element.screenshot(type='png'))
                                break
                        session.cancelled.wait(POLL_INTERVAL)
                finally:
                    # Preserve the failing operation's stage during cleanup.
                    if sys.exc_info()[0] is None:
                        stage = 'close'
                    try:
                        if context is not None:
                            context.close()
                    finally:
                        if browser is not None:
                            browser.close()
        except Exception as exc:
            # Never log raw browser exceptions: they may contain credentials.
            reason = 'profile_busy' if isinstance(exc, ProfileBusy) else str(exc) if isinstance(exc, (CookieRejected, QrNoDisplay)) else '-'
            logger.warning('QR login failed stage=%s error=%s reason=%s',
                           stage, type(exc).__name__, reason)
            final = 'failed'
        finally:
            self._browser = None
            self._persistent_open = False
            if profile_lock is not None:
                profile_lock.release()
            for item in cookies:
                if isinstance(item, dict):
                    item['value'] = ''
            cookies.clear()
            if session.cancelled.is_set():
                final = 'cancelled'
            session._set(final)
            logger.info('QR login finished state=%s stage=%s accepted=%d partitioned_skipped=%d duplicates_resolved=%d',
                        final, stage, stats['accepted'], stats['partitioned_skipped'],
                        stats['duplicates_resolved'])
