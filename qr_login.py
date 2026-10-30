"""On-demand QR login prototype. Credentials live only in process memory."""
import logging
import math
import os
import threading
import time
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import urlsplit

import requests

import client
from hub_store import exc_summary

LOGIN_URL = client.PAGE_URL
POLL_INTERVAL = 1


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


def narrow_cookies(browser_cookies, host, now=None, *, allow_insecure=False):
    """Adapted from commit 022d6ee login_transfer.make_payload / cloud_probe.validate_payload.

    Only creator.douyin.com may inherit douyin.com cookies. The insecure
    loopback exception exists solely for synthetic browser tests.
    """
    if not isinstance(browser_cookies, list) or len(browser_cookies) > 200:
        raise ValueError('Cookie 数量无效')
    now = time.time() if now is None else now
    accepted = {host}
    if host == 'creator.douyin.com':
        accepted.add('douyin.com')
    result, seen = [], set()
    for item in browser_cookies:
        if not isinstance(item, dict) or 'partitionKey' in item:
            raise ValueError('不支持分区 Cookie')
        domain = item.get('domain', '')
        if not isinstance(domain, str) or domain.lstrip('.').lower() not in accepted:
            raise ValueError('Cookie 域无效')
        name, value, path = item.get('name'), item.get('value'), item.get('path')
        if (not isinstance(name, str) or not name or any(c in name for c in '=;\r\n\x00')
                or not isinstance(value, str) or len(value) > 4096
                or any(c in value for c in '\r\n\x00')
                or not isinstance(path, str) or not path.startswith('/')
                or any(c in path for c in '\r\n\x00')):
            raise ValueError('Cookie 格式无效')
        expires = item.get('expires', -1)
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires):
            raise ValueError('Cookie 有效期无效')
        if expires != -1 and expires <= now:
            continue
        key = (name, path)
        if key in seen:
            raise ValueError('重复 Cookie')
        seen.add(key)
        result.append(dict(name=name, value=value, domain=host, path=path,
                           secure=not (allow_insecure and host == '127.0.0.1'
                                       and item.get('secure') is False), expires=expires))
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
                        if self._cookies else None)

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
                 clock=time.monotonic, timeout=None, allow_insecure=False):
        self._origin = urlsplit(allowed_origin)
        if (self._origin.hostname is None or self._origin.username or self._origin.password
                or self._origin.path not in ('', '/') or self._origin.query or self._origin.fragment
                or (self._origin.scheme != 'https' and not (
                    allow_insecure and self._origin.scheme == 'http' and self._origin.hostname == '127.0.0.1'))):
            raise ValueError('登录来源无效')
        for url in (login_url, cookie_url):
            if not self._allowed(url):
                raise ValueError('登录地址必须与允许来源一致')
        self._factory = browser_factory or _playwright
        self._login_url, self._cookie_url = login_url, cookie_url
        self._clock = clock
        self._timeout = _timeout(timeout if timeout is not None else os.environ.get('INDEX_HUB_QR_TIMEOUT', QR_TIMEOUT_SECONDS))
        self._allow_insecure = allow_insecure
        self._lock = threading.Lock()
        self._session = None
        self._browser = None
        self._last_browser = None
        self.holder = SessionHolder(clock)

    def _allowed(self, url):
        parsed = urlsplit(url)
        return (parsed.scheme, parsed.hostname, parsed.port) == (
            self._origin.scheme, self._origin.hostname, self._origin.port) and not (parsed.username or parsed.password)

    def start(self):
        with self._lock:
            if 'DEBUG' in os.environ or 'PWDEBUG' in os.environ:
                raise QrUnsafeDebug('请先关闭浏览器调试环境变量')
            if self._session and (self._session.snapshot()['state'] in ('starting', 'qr_ready')
                                  or self._session.thread.is_alive()):
                raise QrBusy('扫码登录正在进行')
            session = QrLoginSession(self._clock, self._timeout)
            session._set('starting')
            self._session = session
            session.thread = threading.Thread(target=self._run, args=(session,), daemon=True)
            session.thread.start()
            return session

    def holder_info(self):
        return self.holder.info()

    def browser_open(self):
        browser = self._last_browser
        return bool(browser is not None and browser.is_connected())

    def status(self):
        with self._lock:
            return self._session.snapshot() if self._session else dict(
                state='idle', qr_version=0, expires_in=0, message=_MESSAGES['idle'])

    def qr_png(self):
        with self._lock:
            return self._session.qr_png() if self._session else None

    def cancel(self):
        with self._lock:
            if self._session:
                self._session.cancelled.set()

    def _run(self, session):
        context = browser = None
        cookies = []
        final = 'failed'
        try:
            with self._factory() as playwright:
                try:
                    args = ['--no-sandbox'] if hasattr(os, 'geteuid') and os.geteuid() == 0 else []
                    browser = playwright.chromium.launch(headless=True, args=args)
                    self._browser = self._last_browser = browser
                    context = browser.new_context(accept_downloads=False, locale='zh-CN')
                    page = context.new_page()
                    page.set_default_timeout(1000)
                    page.goto(self._login_url, timeout=min(30000, self._timeout * 1000))
                    while True:
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
                            cookies = context.cookies([self._cookie_url])
                            narrowed = narrow_cookies(cookies, self._origin.hostname,
                                                      allow_insecure=self._allow_insecure)
                            try:
                                if any(item['name'] in SELECTORS['session_cookies'] for item in narrowed):
                                    user_agent = page.evaluate('navigator.userAgent')
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
                        images = page.locator(SELECTORS['qr_image'])
                        for i in range(images.count()):
                            element = images.nth(i)
                            box = element.bounding_box()
                            if element.is_visible() and box and box['width'] >= 100 and box['height'] >= 100:
                                session._image(element.screenshot(type='png'))
                                break
                        session.cancelled.wait(POLL_INTERVAL)
                finally:
                    try:
                        if context is not None:
                            context.close()
                    finally:
                        if browser is not None:
                            browser.close()
        except Exception as exc:
            # Playwright exceptions may embed URLs, page text and credentials.
            # Summarize only a sanitized surrogate, never the original message.
            logger.warning('QR login %s %s', type(exc).__name__,
                           exc_summary(RuntimeError('浏览器操作失败')))
            final = 'failed'
        finally:
            self._browser = None
            for item in cookies:
                item['value'] = ''
            cookies.clear()
            if session.cancelled.is_set():
                final = 'cancelled'
            session._set(final)
