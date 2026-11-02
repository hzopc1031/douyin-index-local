"""Isolated Playwright login; credentials live only in an incognito context.

The persistent dyc profile stays at about:blank. Only allowlisted login cookies
are copied back after a positive dyc login signal. No traces, HARs, downloads,
page storage or credential-bearing browser errors are written. Failure screenshots
are redacted before rasterization and stored only in the private state directory.
"""
import asyncio
import base64
from dataclasses import dataclass
import hashlib
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
from urllib.parse import quote, unquote, urlsplit

from . import site
from .browser import _kill_owned, _processes
from .errors import Stop
from .login_ui import ready_qr
from .runtime import Display, Profile, browser_defaults, check_memory, private_dir, private_open, atomic_text
from .sms_state import reject
from .sms_cdp import TargetGuard

# Fixed copy/adaptation of the 2026-10-31 login-B selectors; see SMS_LOGIN.md.
PHONE = '#douyin_login_comp_mobile_code input#normal-input'
CODE = '#douyin_login_comp_mobile_code input#button-input'
SEND = '#douyin_login_comp_button_input_id span'
SUBMIT = '#douyin_login_comp_btn_id'
CHALLENGE = ('#captcha_container, .captcha_verify_container, #rc-verifycenter, '
             '.rc-verifycenter, [id^="rc-verifycenter"], [data-e2e="verify-dialog"], '
             'iframe[src*="captcha"], iframe[src*="verify"]')
RECTS = dict(slider_handle='.secsdk-captcha-drag-icon',
             slider_track='.secsdk-captcha-drag-wrapper',
             slider_image='#captcha-verify-image', slider_piece='.captcha_verify_img_slide')
COOKIE_NAMES = {'sessionid', 'sessionid_ss', 'sid_tt', 'uid_tt', 'uid_tt_ss',
                'sid_guard', 'sid_ucp_v1', 'ssid_ucp_v1', 'passport_csrf_token',
                'passport_csrf_token_default', 'ttwid'}
SESSION_COOKIE_SECONDS = 86400
ASSET_DOMAINS = ('douyin.com', 'douyinpic.com', 'douyinvod.com', 'byteimg.com',
                 'bytedance.com', 'bytegecko.com', 'zijieapi.com', 'snssdk.com',
                 'pstatp.com', 'bytedance.net', 'ibytedtos.com', 'douyinstatic.com',
                 'bytegoofy.com', 'bytetos.com', 'bytescm.com', 'yhgfb-cn-static.com')
AUTH_DOMAINS = ('douyin.com', 'zijieapi.com', 'snssdk.com')
LOGIN_PANEL_SECONDS = 60
# SDK scripts and static data observed on the official login landing page.
# These hosts do NOT inherit the general API/POST permission above.
LOGIN_STATIC_DOMAINS = ('applogcdn.com', 'ibytedapm.com', 'bytednsdoc.com',
                        'bytetcc.com', 'effectcdn.com', 'byteeffecttos.com')


def login_static(url, method, resource):
    parsed = urlsplit(url)
    if (method not in ('GET', 'HEAD') or
            not in_domains(parsed.hostname or '', LOGIN_STATIC_DOMAINS)):
        return False
    if resource.lower() in ('script', 'stylesheet', 'image', 'font', 'media'):
        return True
    # fetch/XHR is also used to load SDK bundles/data. An arbitrary config or
    # reporting endpoint is not a static asset just because it uses GET.
    return resource.lower() in ('xhr', 'fetch') and parsed.path.lower().endswith(
        ('.js', '.css', '.wasm', '.json', '.bin', '.zip'))

# All DOM text (including reflected codes), form fields and editable content
# are hidden BEFORE rasterization, in every frame. The trusted phone UI gives
# instructions. Images/canvas inside the cropped challenge remain visible.
REDACT_STYLE = '''* { color: transparent !important; -webkit-text-fill-color: transparent !important;
 text-shadow: none !important; caret-color: transparent !important; }
 input, textarea, [contenteditable], svg text { visibility: hidden !important; }
 *::before, *::after { content: none !important; }'''


def in_domains(host, domains):
    return any(host == item or host.endswith('.' + item) for item in domains)


def points_checked(points):
    if not isinstance(points, (list, tuple)) or not 1 <= len(points) <= 160:
        reject('invalid_points')
    previous = -1
    for point in points:
        if (not isinstance(point, (list, tuple)) or len(point) != 3 or
            any(type(v) not in (int, float) or not math.isfinite(v) for v in point)):
            reject('invalid_points')
        x, y, stamp = point
        if not (0 <= x <= 1 and 0 <= y <= 1 and 0 <= stamp <= 5000 and stamp > previous):
            reject('invalid_points')
        previous = stamp
    if points[0][2] != 0:
        reject('invalid_points')
    return points


@dataclass(frozen=True)
class Metadata:
    x0: float
    y0: float
    width: float
    height: float
    x_scale: float = 1
    y_scale: float = 1


@dataclass(repr=False)
class Capture:
    png: bytes
    metadata: Metadata
    prompt: str = ''


@dataclass(repr=False)
class Challenge:
    element: object
    rect: dict
    frame: object


class SmsBrowser:
    def __init__(self, profile, *, backend=None, solver=None, startup_limits=False, state_dir=None):
        self.state_dir = private_dir(state_dir) if state_dir is not None else None
        self.diagnostic = None
        self.operation = 'initializing'
        self.last_read_error = None
        self.fatal_reason = ''
        self.fatal_context = None
        self.step('initializing', 'browser startup', 90)
        self.startup_limits = startup_limits
        self.profile = Profile(profile)
        self.display = Display()  # always owned; never use :107 or inherited DISPLAY
        self.backend = backend
        self.url = site.FAKE_ORIGIN if backend else site.LOGIN_URL
        self.solver = solver
        self.playwright = self.persistent = self.context = self.page = None
        self.driver_pid = None
        self.response_session = None
        self.guard_tasks = set()
        self.target_guard = None
        self.private_values = []
        self.fatal = False
        self.auto_attempts = 0
        self.auto_seen = set()
        self.auto_disabled = False
        self.cancel = threading.Event()
        self.last_frame = None
        self.frame_token = ''

    def step(self, name, condition, timeout):
        self.stage = name
        self.operation = name
        self.condition = condition
        self.stage_timeout = timeout
        self.stage_started = time.monotonic()

    @staticmethod
    def exception_info(error):
        # Inspect known markers in memory only. Never retain exception text or
        # arbitrary class names (which can themselves contain private values).
        name = type(error).__name__
        kind = name if name in ('Error', 'TimeoutError', 'TargetClosedError',
                               'TypeError', 'AttributeError', 'RuntimeError',
                               'CancelledError', 'Stop') else 'other'
        message = str(error)
        category = next((label for marker, label in (
            ('strict mode violation', 'strict_mode'),
            ('Execution context was destroyed', 'context_destroyed'),
            ('Cannot find context with specified id', 'context_destroyed'),
            ('Frame was detached', 'frame_detached'),
            ('not attached to the DOM', 'element_detached')) if marker in message), 'other')
        return dict(exception_type=kind, category=category)

    async def read_dom(self, operation, read):
        # Only repeat reads, never navigation, input or clicks. A transient
        # document replacement must not skip the challenge/access checks.
        for attempt in range(3):
            self.operation = operation
            self.require_origin()
            try:
                result = await read()
                self.require_origin()
                return result
            except Exception as error:
                info = self.exception_info(error)
                self.last_read_error = dict(operation=self.operation, **info)
                transient = (info['exception_type'] == 'Error' and info['category'] in
                             ('context_destroyed', 'frame_detached', 'element_detached'))
                transient |= operation == 'phone_editable' and info['exception_type'] == 'TimeoutError'
                if not transient or attempt == 2:
                    raise
                print(json.dumps(dict(event='browser_read_retry', attempt=attempt + 1,
                                      **self.last_read_error)), file=sys.stderr, flush=True)
                await asyncio.sleep(.1)

    def visible(self, selector):
        return self.page.locator(selector).filter(visible=True).first

    async def diagnose(self, error=None, reason='login_failed'):
        """Never serialize exception messages, input, URL parameters or DOM text."""
        if self.diagnostic is not None:
            return self.diagnostic
        if isinstance(error, Stop):
            reason = error.reason
        elif isinstance(error, (TimeoutError, asyncio.CancelledError)) or type(error).__name__ == 'TimeoutError':
            reason = 'login_step_timeout'
        elif error is not None:
            reason = 'browser_operation_failed'
        reason = self.fatal_reason or reason
        data = dict(recorded_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                    guard=self.fatal_context, reason=reason, status='BLOCKED' if reason in ('http_401', 'http_403', 'http_429', 'verification_required') else 'FAILED',
                    step=self.stage, condition=self.condition,
                    operation=self.operation, exception=self.exception_info(error) if error else None,
                    last_read_error=self.last_read_error,
                    timeout_seconds=self.stage_timeout,
                    elapsed_seconds=round(time.monotonic()-self.stage_started, 3),
                    url='unavailable', title='unavailable', screenshot='',
                    screenshot_status='page_unavailable')
        self.diagnostic = data
        if self.page is not None:
            # Only known public landing paths survive; no query/hash/userinfo.
            parsed = urlsplit(self.page.url)
            if parsed.scheme == 'https' and parsed.netloc == urlsplit(self.url).netloc:
                path = parsed.path if parsed.path in ('', '/', '/jingxuan') else '/[redacted]'
                data['url'] = 'https://' + parsed.netloc + path
            elif self.page.url == 'about:blank':
                data['url'] = 'about:blank'
            else:
                data['url'] = '[redacted]'
            try:
                async with asyncio.timeout(2):
                    title = await self.page.title()
                    data['title'] = title if title in ('', '抖音', '抖音-记录美好生活',
                        '抖音 - 记录美好生活', '抖音 - 记录美好生活 - 抖音') else '[redacted nonstandard title]'
            except Exception:
                pass
            if self.state_dir is not None:
                try:
                    png = await self.diagnostic_snapshot()
                    path = self.state_dir / ('login-failure-' + secrets.token_hex(8) + '.png')
                    with os.fdopen(private_open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'wb') as stream:
                        stream.write(png)
                    data['screenshot'] = str(path)
                    data['screenshot_status'] = 'saved_redacted'
                except Exception as capture_error:
                    data['screenshot_status'] = 'capture_failed'
                    data['screenshot_error'] = self.exception_info(capture_error)
        line = json.dumps(data, ensure_ascii=False)
        if self.state_dir is not None:
            try:
                atomic_text(self.state_dir / 'login-status.json', line + '\n')
            except Exception:
                data['state_write_failed'] = True
        print(json.dumps(dict(event='login_failure', **data), ensure_ascii=False), file=sys.stderr, flush=True)
        return data

    async def diagnostic_snapshot(self):
        # Keep empty field borders visible, but hide all text, input values,
        # QR codes and media; no screenshot can contain a login credential.
        style = REDACT_STYLE + """
          input, textarea { visibility: visible !important; color: transparent !important;
            -webkit-text-fill-color: transparent !important; }
          img, canvas, video, svg, iframe { visibility: hidden !important; }
          * { background-image: none !important; }
        """
        return await self.page.screenshot(type='png', style=style, animations='disabled', timeout=3000)

    def __repr__(self):
        return '<SmsBrowser>'

    def require_origin(self):
        parsed = urlsplit(self.page.url)
        if (parsed.scheme != 'https' or parsed.netloc != urlsplit(self.url).netloc
                or parsed.username or parsed.password or self.fatal):
            raise Stop(4, self.fatal_reason or 'login_origin_or_access')

    def contains_private(self, value):
        decoded = unquote(value)
        return any(v in decoded or quote(v, safe='') in value or
                   base64.b64encode(v.encode()).decode() in value for v in self.private_values)

    async def route(self, route):
        try:
            await self._route(route)
        except Exception:
            # Never let Playwright log an unhandled route exception containing
            # a URL or input. Missing guard coverage fails closed.
            self.fatal_reason = self.fatal_reason or 'request_guard_failed'
            self.fatal = True
            try:
                await route.abort()
            except Exception:
                pass

    async def _route(self, route):
        request = route.request
        if self.fatal or self.cancel.is_set():
            await route.abort()
            return
        if request.frame.page != self.page:
            await route.abort()  # auth is confined to the owned page, never popups
            return
        if self.backend:
            await self.backend(route)
            return
        parsed = urlsplit(request.url)
        host = parsed.hostname or ''
        allowed = (parsed.scheme == 'https' and parsed.port in (None, 443) and
                   not parsed.username and not parsed.password and
                   (in_domains(host, ASSET_DOMAINS) or
                    login_static(request.url, request.method, request.resource_type)) and
                   not self.contains_private(request.url) and
                   request.method in ('GET', 'HEAD', 'POST', 'OPTIONS'))
        if request.method == 'POST':
            allowed = allowed and in_domains(host, AUTH_DOMAINS)
        if request.is_navigation_request() and request.frame == self.page.main_frame:
            allowed = allowed and parsed.netloc == urlsplit(self.url).netloc
        # Login must not collect comments as a side effect.
        if site.request_kind(request.url):
            allowed = False
        if allowed:
            await route.continue_()
        else:
            await route.abort()

    async def start(self):
        if self.startup_limits:
            check_memory()
        try:
            self.step('profile_lock', 'exclusive dyc profile lock', 0)
            self.profile.__enter__()
            self.step('display_start', 'owned Xvfb ready', 10)
            await self.display.start()
            browser_defaults()
            from playwright.async_api import async_playwright
            self.step('playwright_start', 'Playwright driver ready', 35)
            self.playwright = await async_playwright().start()
            self.driver_pid = self.playwright._impl_obj._connection._transport._proc.pid
            self.step('browser_launch', 'persistent Chromium about:blank', 35)
            self.persistent = await self.playwright.chromium.launch_persistent_context(
                str(self.profile.path), headless=False,
                env={**os.environ, **self.display.environment()},
                handle_sigint=False, handle_sigterm=False, handle_sighup=False,
                service_workers='block', accept_downloads=False,
                args=['--disable-background-networking', '--disable-component-update',
                      '--disable-sync', '--no-first-run', '--disable-crash-reporter',
                      '--disable-breakpad', '--disable-extensions'])
            await self.persistent.route('**/*', lambda route: route.abort())
            await self.persistent.route_web_socket('**/*', lambda socket: socket.close())
            self.step('isolated_context', 'isolated login page and network guards', 35)
            self.context = await self.persistent.browser.new_context(
                viewport=dict(width=1000, height=800), device_scale_factor=1,
                service_workers='block', accept_downloads=False)
            await self.context.route('**/*', self.route)
            await self.context.route_web_socket('**/*', lambda socket: socket.close())
            await self.context.add_cookies(await self.persistent.cookies(self.url))
            self.context.on('response', self.response)
            self.page = await self.context.new_page()
            self.page.set_default_timeout(3000)
            self.response_session = await self.context.new_cdp_session(self.page)
            self.target_guard = TargetGuard(self)
            self.step('redirect_guard', 'CDP Fetch/Target interception ready', 5)
            await self.target_guard.start(self.response_session)
            await self.reset()
        except BaseException as error:
            await self.diagnose(error)
            await self.close()
            raise

    def response(self, response):
        if response.status in (401, 403, 429) and response.request.resource_type in ('document', 'xhr', 'fetch'):
            self.fatal_reason = 'http_' + str(response.status)
            self.fatal = True

    def guard_response(self, event, session=None):
        task = asyncio.create_task(self.check_response(event, session))
        self.guard_tasks.add(task)
        task.add_done_callback(self.guard_tasks.discard)

    async def check_response(self, event, session=None):
        """Chromium pauses before redirects, which Playwright routes may skip."""
        session = session or self.response_session
        request_id = event['requestId']
        try:
            status = event.get('responseStatusCode', 0)
            if status in (401, 403, 429) and event.get('resourceType') in ('Document', 'XHR', 'Fetch'):
                self.fatal_reason = 'http_' + str(status)
                self.fatal = True
            denied = self.fatal or bool(event.get('responseErrorReason'))
            if 300 <= status < 400:
                request = event['request']
                location = next((h['value'] for h in event.get('responseHeaders', [])
                                 if h['name'].lower() == 'location'), '')
                from urllib.parse import urljoin
                target = urlsplit(urljoin(request['url'], location))
                # Static CDN GETs may move between approved asset hosts. Auth,
                # documents and API redirects keep the stricter same-host rule;
                # POST and credential-bearing redirects are always rejected.
                source = urlsplit(request['url'])
                asset = event.get('resourceType') in ('Script', 'Stylesheet', 'Image', 'Font', 'Media')
                resource = event.get('resourceType', '')
                approved_asset = (
                    (asset and in_domains(source.hostname or '', ASSET_DOMAINS) or
                     login_static(source.geturl(), request['method'], resource)) and
                    (asset and in_domains(target.hostname or '', ASSET_DOMAINS) or
                     login_static(target.geturl(), request['method'], resource)))
                denied = denied or (request['method'] not in ('GET', 'HEAD') or
                          target.scheme != 'https' or target.port not in (None, 443) or
                          target.username is not None or target.password is not None or
                          (target.netloc != source.netloc and not approved_asset) or
                          self.contains_private(target.geturl()) or bool(site.request_kind(target.geturl())))
                if denied:
                    kind = event.get('resourceType', '')
                    kind = kind.lower() if kind in ('Document', 'XHR', 'Fetch', 'Script', 'Image', 'Other') else 'resource'
                    self.fatal_reason = self.fatal_reason or 'redirect_denied_' + kind
                    self.fatal = True
            if denied:
                await session.send('Fetch.failRequest', dict(requestId=request_id, errorReason='BlockedByClient'))
            else:
                await session.send('Fetch.continueRequest', dict(requestId=request_id))
        except Exception:
            self.fatal_reason = self.fatal_reason or 'response_guard_failed'
            self.fatal = True
            try:
                await session.send('Fetch.failRequest', dict(requestId=request_id, errorReason='BlockedByClient'))
            except Exception:
                pass

    async def reset(self):
        self.last_frame = None
        self.frame_token = ''
        # Destroy the DOM containing old inputs, without storing page state.
        self.step('navigate', 'DOMContentLoaded on fixed login URL', 15)
        self.operation = 'page_goto'
        await self.page.goto(self.url, wait_until='domcontentloaded', timeout=15000)
        self.require_origin()
        self.step('login_entry', 'visible header button 登录 or ' + PHONE + ' editable or account signal', LOGIN_PANEL_SECONDS)
        deadline = time.monotonic() + LOGIN_PANEL_SECONDS
        clicked = False
        while time.monotonic() < deadline:
            self.require_origin()
            if await self.challenge():
                raise Stop(3, 'verification_required')
            if await self.inside():
                return
            if await self.phone_ready():
                self.require_origin()
                return
            panel_visible = await self.read_dom('login_panel_count',
                lambda: self.page.locator(site.LOGIN_PANEL).filter(visible=True).count())
            if not clicked and not panel_visible:
                login = self.page.get_by_role('button', name='登录', exact=True).filter(visible=True).first
                if await self.read_dom('login_button_visible', login.is_visible):
                    self.step('phone_input_ready', PHONE + ' visible and editable after header 登录 click',
                              round(max(0, deadline-time.monotonic()), 3))
                    self.require_origin()
                    self.operation = 'login_button_click'
                    await login.click()
                    clicked = True
            await asyncio.sleep(.2)
        raise Stop(5, 'phone_input_missing' if clicked else 'login_panel_missing')

    async def phone_ready(self):
        if not await self.read_dom('phone_visible', lambda: self.visible(PHONE).is_visible()):
            return False
        # Re-resolve after each read: React may have replaced the input.
        return await self.read_dom('phone_editable',
                                   lambda: self.visible(PHONE).is_editable(timeout=500))

    async def inside(self):
        return await self.read_dom('account_signal', self._inside)

    async def _inside(self):
        # Require absence of the login panel AND the existing dyc account signal.
        # Live account signals remain pending verification; never infer success
        # from navigation, an absent error or a submit click alone.
        if await self.page.locator(site.LOGIN_PANEL).filter(visible=True).count():
            return False
        selectors = site.SIGNALS['logged_in'] if self.backend else site.SIGNALS['logged_in'][:1]
        return bool(await self.page.locator(', '.join(selectors)).filter(visible=True).count())

    async def observe(self):
        self.step('observe_login', 'no challenge/access denial; confirmed account signal', 15)
        self.require_origin()
        if await self.challenge():
            return 'verification'
        if await self.inside():
            return 'succeeded'
        if await self.read_dom('code_error_visible', lambda: self.page.get_by_text(
                '验证码错误', exact=False).filter(visible=True).count()):
            raise Stop(4, 'code_invalid')
        return 'pending'

    async def send(self, phone):
        self.step('send_sms', PHONE + ' editable; ' + SEND + ' clickable', 3)
        self.require_origin()
        self.private_values.append(phone)
        await self.visible(PHONE).fill(phone)
        self.require_origin()
        await self.page.locator(SEND).filter(has_text='获取验证码').filter(visible=True).first.click()

    async def submit(self, code):
        self.step('submit_code', CODE + ' editable; ' + SUBMIT + ' clickable', 3)
        self.require_origin()
        self.private_values.append(code)
        try:
            await self.visible(CODE).fill(code)
            self.require_origin()
            await self.visible(SUBMIT).click()
        finally:
            # Clear DOM even when click/verification fails. The context is also
            # destroyed at timeout/cancel; never save storage_state.
            try:
                await self.visible(CODE).fill('', timeout=500)
            except Exception:
                pass

    async def challenge(self):
        return await self.read_dom('challenge_read', self._challenge)

    async def _challenge(self):
        self.require_origin()
        for frame in self.page.frames:
            candidates = frame.locator(CHALLENGE).filter(visible=True)
            self.operation = 'challenge_candidates_count'
            for index in range(await candidates.count()):
                element = candidates.nth(index)
                self.operation = 'challenge_bounds'
                rect = await element.bounding_box()
                if rect and rect['width'] >= 40 and rect['height'] >= 40:
                    owner = frame
                    self.operation = 'challenge_tag'
                    if await element.evaluate('(e) => e.tagName') == 'IFRAME':
                        self.operation = 'challenge_content_frame'
                        handle = await element.element_handle()
                        owner = await handle.content_frame() if handle else None
                        if owner is not None:
                            element = owner.locator('body')
                    return Challenge(element, rect, owner)
        return None

    async def safe_prompt(self, challenge):
        text = unicodedata.normalize('NFKC', await challenge.element.inner_text(timeout=1000))[:2000]
        # Only recognizable instructions cross the boundary, never arbitrary
        # page text. Hide long numbers and exact credentials, including digits
        # separated by spaces/punctuation. Render this as textContent on phone.
        allowed = re.compile(r'(?:请)?(?:(?:依次|按顺序)?(?:点击|点选)|拖动|旋转)[\u3400-\u9fffA-Za-z0-9\s:：、,，。!！“”‘’「」【】()（）-]{1,80}')
        for line in text.splitlines():
            line = line.strip()
            digits = re.sub(r'\D', '', line)
            if (allowed.fullmatch(line) and not re.search(r'\d{4,}', digits)
                    and not self.contains_private(line) and
                    not any(value in digits for value in self.private_values)):
                return line
        return ''

    async def capture(self, challenge):
        rect = challenge.rect
        viewport = self.page.viewport_size
        x, y = max(0, rect['x']), max(0, rect['y'])
        w = min(rect['x'] + rect['width'], viewport['width']) - x
        h = min(rect['y'] + rect['height'], viewport['height']) - y
        if w < 40 or h < 40:
            reject('frame_unavailable')
        marker = secrets.token_hex(16)
        try:
            # Short Chinese/letter-only leaf labels can be click targets. They
            # cannot contain a phone or six-digit SMS code. All numbers and
            # longer/arbitrary prose stay hidden; safe instructions are sent
            # separately. Clone immutable safe text: unmasking the live node
            # could expose an input reflected by a concurrent DOM update.
            await challenge.element.evaluate(r'''(root, marker) => {
              for (const e of [root, ...root.querySelectorAll('*')]) {
                if (!e.children.length && !e.matches('input, textarea, script, style, [contenteditable]')
                    && /^[\u3400-\u9fffA-Za-z]{1,4}$/.test(e.textContent.trim())) {
                  const rect=e.getBoundingClientRect(), computed=getComputedStyle(e);
                  if (!rect.width || !rect.height || computed.visibility === 'hidden') continue;
                  const label=root.ownerDocument.createElement('span');
                  label.textContent=e.textContent.trim();
                  label.setAttribute('data-dyc-safe-label', marker);
                  Object.assign(label.style, {position:'fixed', left:rect.left+'px', top:rect.top+'px',
                    width:rect.width+'px', height:rect.height+'px', font:computed.font,
                    textAlign:computed.textAlign, lineHeight:computed.lineHeight,
                    zIndex:'2147483647', pointerEvents:'none'});
                  root.ownerDocument.body.append(label);
                }
              }
            }''', marker)
            style = REDACT_STYLE + f'''[data-dyc-safe-label="{marker}"] {{
              color: white !important; -webkit-text-fill-color: white !important;
              background-color: #172633 !important; }}'''
            png = await self.page.screenshot(type='png', clip=dict(x=x, y=y, width=w, height=h),
                                            style=style, animations='disabled', timeout=3000)
        finally:
            try:
                await challenge.element.evaluate('''(root, marker) => {
                  for (const e of root.ownerDocument.querySelectorAll('[data-dyc-safe-label]'))
                    if (e.getAttribute('data-dyc-safe-label') === marker) e.remove();
                }''', marker)
            except Exception:
                pass
        if len(png) > 500_000:
            reject('frame_unavailable')
        return Capture(png, Metadata(x, y, w, h), await self.safe_prompt(challenge))

    @staticmethod
    def identity(capture):
        return hashlib.sha256(capture.png).digest(), capture.metadata, capture.prompt

    async def frame(self, verification):
        self.require_origin()
        if verification:
            challenge = await self.challenge()
            if challenge is None:
                reject('frame_unavailable')
            capture = await self.capture(challenge)
            self.last_frame = self.identity(capture)
            self.frame_token = secrets.token_urlsafe(24)
            return dict(image=base64.b64encode(capture.png).decode(), frame=self.frame_token,
                        width=capture.metadata.width, height=capture.metadata.height, prompt=capture.prompt)
        qr = await ready_qr(self.page)
        if qr is None:
            reject('frame_unavailable')
        png = await qr.screenshot(type='png', style=REDACT_STYLE, timeout=3000)
        return dict(image=base64.b64encode(png).decode(), frame='', width=0, height=0)

    async def pointer(self, token, points):
        points = points_checked(points)
        if not isinstance(token, str) or not token or token != self.frame_token:
            reject('stale_frame')
        self.frame_token = ''  # single use even if replay fails
        challenge = await self.challenge()
        if challenge is None:
            reject('stale_frame')
        capture = await self.capture(challenge)
        if self.identity(capture) != self.last_frame:
            reject('stale_frame')
        self.auto_disabled = True
        await self.replay(points, capture.metadata)

    async def replay(self, points, meta):
        points_checked(points)
        self.require_origin()
        try:
            previous = 0
            for index, (x, y, stamp) in enumerate(points):
                self.require_origin()
                if self.cancel.is_set():
                    raise asyncio.CancelledError
                await asyncio.sleep((stamp-previous)/1000)
                await self.page.mouse.move(meta.x0+x*meta.width, meta.y0+y*meta.height, steps=2)
                if index == 0:
                    await self.page.mouse.down()
                previous = stamp
        finally:
            await self.page.mouse.up()

    async def auto_step(self):
        if self.auto_disabled or self.auto_attempts >= 3 or self.solver is None:
            return
        challenge = await self.challenge()
        if challenge is None:
            return
        if challenge.frame != self.page.main_frame:
            return  # all child-frame challenges remain manual, regardless of URL
        element = challenge.element
        # Cross-origin iframe or any ambiguous/rotation challenge is manual.
        text = await element.inner_text(timeout=1000)
        if ('旋转' in text or '转正' in text or not any(hint in text for hint in
                ('拖动滑块完成拼图', '拖动滑块使拼图完整'))):
            return
        rects = {}
        for name, selector in RECTS.items():
            locator = element.locator(selector).filter(visible=True)
            if await locator.count() != 1:
                return
            rects[name] = await locator.bounding_box()
        capture = await self.capture(challenge)
        identity = self.identity(capture)
        if identity in self.auto_seen:
            return
        self.auto_seen.add(identity)
        # Solver is synchronous, screenshot-only, bounded by its worker process.
        points = await self.solver.solve(capture, dict(kind='gap_slider', rects=rects))
        if not points:
            self.auto_disabled = True
            return
        current = await self.challenge()
        if current is None or self.identity(await self.capture(current)) != identity:
            return
        self.auto_attempts += 1  # charge before mouse-down, never reset on mode switch
        await self.replay(points, capture.metadata)

    async def persist(self):
        self.step('persist_login', 'confirmed account and allowlisted session cookie', 3)
        self.require_origin()
        if not await self.inside():
            raise Stop(4, 'login_unconfirmed')
        cookies = await self.context.cookies(self.url)
        safe = [cookie for cookie in cookies if cookie['name'] in COOKIE_NAMES and
                in_domains(cookie['domain'].lstrip('.'), (urlsplit(self.url).hostname, 'douyin.com')) and
                not self.contains_private(json.dumps(cookie))]
        if not any(c['name'] in ('sessionid', 'sessionid_ss', 'sid_tt') for c in safe):
            raise Stop(4, 'login_cookie_missing')
        # Chromium discards session-only cookies on a clean restart. Give only
        # these allowlisted login cookies a conservative local 24-hour horizon;
        # this never extends the platform's server-side session validity. Keep
        # every explicit official Expires value unchanged. Restoring whole tabs
        # would navigate before our network guards and is deliberately avoided.
        for cookie in safe:
            if cookie.get('expires', -1) < 0:
                cookie['expires'] = time.time() + SESSION_COOKIE_SECONDS
        await self.persistent.add_cookies(safe)

    async def close(self):
        self.cancel.set()
        self.private_values.clear()
        self.last_frame = None
        self.frame_token = ''
        records = _processes(self.driver_pid) if self.driver_pid else {}
        if self.target_guard:
            self.target_guard.closing = True
        try:
            async with asyncio.timeout(5):
                if self.context:
                    await self.context.close()
                if self.persistent:
                    await self.persistent.close()
                if self.playwright:
                    await self.playwright.stop()
        except BaseException:
            _kill_owned(records)
        finally:
            self.context = self.persistent = self.playwright = self.page = None
            for task in self.guard_tasks:
                task.cancel()
            await asyncio.gather(*self.guard_tasks, return_exceptions=True)
            self.guard_tasks.clear()
            if self.target_guard:
                await self.target_guard.close()
            if self.solver:
                await self.solver.close()
            await self.display.close()
            self.profile.__exit__(None, None, None)
