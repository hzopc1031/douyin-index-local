import json
import io
import os
import subprocess
from contextlib import contextmanager
import tempfile
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
import re
from urllib.parse import urlparse, parse_qsl


def is_xhs(url):
    host = urlparse(url).hostname or ''
    return any(host == domain or host.endswith('.' + domain) for domain in
               ('xiaohongshu.com', 'xhslink.com', 'xhslink.cn', 'rednote.com'))


LOGIN_REQUIRED = '需要先运行 python -m media_dl login xhs 扫码登录'
NO_VIDEO = '该笔记没有视频'
RUN_DIR = Path('/workspace/dev/media-dl-run')


def profile_path():
    return Path.home() / '.config/media-dl/xhs-profile'


def playwright():
    from playwright.sync_api import sync_playwright
    return sync_playwright()


def write_status(status):
    if status not in {'need_scan', 'scanned', 'verify', 'logged_in', 'expired', 'error'}:
        raise ValueError('无效状态')
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    path = RUN_DIR / 'xhs-login.status'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(status + '\n')


def has_session(context):
    return any(cookie.get('name') == 'web_session' and cookie.get('value')
               for cookie in context.cookies(['https://xiaohongshu.com', 'https://www.xiaohongshu.com',
                                              'https://rednote.com', 'https://www.rednote.com']))


def visible(page, selector):
    locator = page.locator(selector)
    for index in range(locator.count()):
        if locator.nth(index).is_visible():
            return locator.nth(index)
    return None


def logged_in(page, context, authenticated=False):
    return bool(authenticated and has_session(context))


def login_container(page):
    container = visible(page, '.login-container, [class*="login-modal"], [role="dialog"]:has-text("登录"), dialog:has-text("登录")')
    if not container and urlparse(page.url).path.rstrip('/') == '/login':
        container = visible(page, 'body')
    return container


def security_container(page):
    for text in ('Security Verification', 'Scan with logged-in', '安全验证', '账户安全',
                 'Expired. Click QR code to refresh', 'QR code expires'):
        for selector in ('[class*="verification"]', '[role="dialog"]', 'dialog', '[class*="dialog"]', '[class*="modal"]'):
            container = visible(page, f'{selector}:has-text("{text}")')
            if container:
                return container
        heading = visible(page, f'text="{text}"')
        if heading:
            container = heading.locator('xpath=ancestor::*[.//img or .//canvas][not(self::body or self::html)][1]')
            if container.count() and container.is_visible():
                return container
    return None


def login_button(page):
    return visible(page, 'button.login-btn:not([class*="submit"]), button:not([class*="submit"]):text-is("登录"), a:not([class*="submit"]):text-is("登录"), button:not([class*="submit"]):text-is("Log in"), a:not([class*="submit"]):text-is("Log in"), button:not([class*="submit"]):text-is("Log In"), a:not([class*="submit"]):text-is("Log In")')


def qr(container, security=False):
    selector = '[class*="qr"] img, [class*="qr"] canvas, img[class*="qr"], canvas[class*="qr"], img[alt*="二维码"], :text("扫码登录") ~ * img, :text("扫码登录") ~ * canvas'
    codes = container.locator(selector + (', img, canvas' if security else ''))
    for index in range(codes.count()):
        code = codes.nth(index)
        if code.is_visible() and code.evaluate(r'''(element, container) => {
            if (element.tagName === 'IMG' && (!element.complete || !element.naturalWidth || !element.naturalHeight)) return false;
            if (element.tagName === 'CANVAS' && (!element.width || !element.height)) return false;
            for (let node = element; node && node !== container; node = node.parentElement) {
                if (/下载|打开\s*(App|应用)|download/i.test((node.innerText || '') + ' ' + (node.className || '') + ' ' + (node.getAttribute('alt') || ''))) return false;
            }
            return true;
        }''', container.element_handle()):
            yield code


def qr_info(payload):
    if not isinstance(payload, str):
        return False, '-', '-', False
    try:
        parsed = urlparse(payload)
        host = (parsed.hostname or '').lower()
        path = parsed.path or '/'
        has_id = any(key.lower().replace('_', '') == 'qrid' and value
                     for key, value in parse_qsl(parsed.query))
        has_id = bool(has_id or re.search(r'qr_?id', path, re.I))
        valid = (parsed.scheme in ('http', 'https') and bool(host) and has_id
                 and not any(word in payload.lower() for word in ('onelink', 'af_xp', 'download', 'app下载', '下载app')))
        # Only safe characters from the host and path may enter the log.
        host = host if re.fullmatch(r'[a-z0-9.-]{1,253}', host) else '-'
        path = path if path in ('/', '/mobile/login') else '/[redacted]'
        return valid, host, path, has_id
    except ValueError:
        return False, '-', '-', False


def qr_valid(payload, in_login_container):
    return bool(in_login_container and qr_info(payload)[0])


def save_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(data)


def log_event(message):
    path = RUN_DIR / 'xhs-login.log'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'a') as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(message + '\n')


def set_status(status):
    write_status(status)
    log_event('状态变化: ' + status)


def save_image(image, path, expected_id=None, security=False):
    import zxingcpp

    result = zxingcpp.read_barcode(image)
    payload = result.text if result else None
    valid = bool(result and (isinstance(payload, str) and payload and not any(
        word in payload.lower() for word in ('onelink', 'af_xp', 'download', 'app下载', '下载app'))
        if security else qr_info(payload)[0]))
    if valid and not security and expected_id is not None:
        valid = qr_id(dict(parse_qsl(urlparse(payload).query))) == expected_id
    log_event("二维码校验通过" if valid else "二维码校验失败")
    if not valid:
        return False
    data = io.BytesIO()
    image.save(data, format='PNG')
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temp_name = output.name
            os.fchmod(output.fileno(), 0o600)
            output.write(data.getvalue())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    return True


def save_qr(code, path, expected_id=None, security=False):
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(code.screenshot())).convert('RGB')
    image = image.resize((image.width * 3, image.height * 3), Image.Resampling.NEAREST)
    return save_image(ImageOps.expand(image, border=24, fill='white'), path, expected_id, security)


def save_page_qr(page, container, path, expected_id=None, security=False):
    if container:
        for code in qr(container, True) if security else qr(container):
            if save_qr(code, path, expected_id, security):
                save_private(RUN_DIR / 'xhs-page.png', page.screenshot())
                return True
    return False


def save_login_qr(page, container, data, identifier, path):
    if save_page_qr(page, container, path, identifier):
        log_event('码来源: 页面原生')
        return 'native'
    if save_url_qr(data['url'], path):
        save_private(RUN_DIR / 'xhs-page.png', page.screenshot())
        log_event('码来源: 接口生成')
        return 'generated'
    return None


def save_url_qr(payload, path):
    import qrcode

    image = qrcode.QRCode(box_size=12, border=4)
    image.add_data(payload)
    image.make(fit=True)
    return save_image(image.make_image().convert('RGB'), path)


def qr_id(data):
    if not isinstance(data, dict):
        return None
    return next((value for key, value in data.items()
                 if key.lower().replace('_', '') == 'qrid' and isinstance(value, str) and value), None)


def create_id(data):
    return qr_id(data) or qr_id(dict(parse_qsl(urlparse(data.get('url', '')).query)))


def status_request(request):
    parsed = urlparse(request.url)
    if (parsed.scheme != 'https' or not any(parsed.hostname == domain or (parsed.hostname or '').endswith('.' + domain)
            for domain in ('xiaohongshu.com', 'rednote.com')) or parsed.port not in (None, 443)):
        return None
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    body = request.post_data
    content_type = request.headers.get('content-type', '')
    fields = {}
    if body:
        try:
            fields = json.loads(body) if 'json' in content_type else dict(parse_qsl(body, keep_blank_values=True))
        except (ValueError, TypeError):
            return None
    identifier = qr_id(query) or qr_id(fields)
    if not identifier or request.method not in ('GET', 'POST'):
        return None
    return {'url': request.url, 'method': request.method, 'body': body,
            'content_type': content_type, 'id': identifier}


def poll_status(page, request):
    try:
        data = page.evaluate("""async request => {
            try {
                const options = {method: request.method, credentials: 'include', redirect: 'error'};
                if (request.body) {
                    options.body = request.body;
                    options.headers = {'content-type': request.content_type};
                }
                const response = await fetch(request.url, options);
                if (!response.ok) return null;
                return await response.json();
            } catch (_) { return null; }
        }""", request)
        return data.get('data', data) if isinstance(data, dict) else None
    except Exception:
        return None


def response_data(response, events):
    try:
        parsed = urlparse(response.url)
        path = parsed.path.lower()
        host = parsed.hostname or ''
        if (parsed.scheme == 'https' and any(host == domain or host.endswith('.' + domain) for domain in ('xiaohongshu.com', 'rednote.com'))
                and parsed.port in (None, 443) and path == '/api/sns/web/v2/user/me'):
            body = response.json()
            data = body.get('data') if isinstance(body, dict) and body.get('success') is True else None
            if isinstance(data, dict) and data.get('guest') is False:
                events.append(('identity', True))
            return
        if 'login/qrcode/create' in path:
            kind = 'create'
        elif 'login/qrcode/status' in path or '/api/qrcode/userinfo' in path:
            kind = 'status'
        else:
            return
        body = response.json()
        data = body.get('data', body) if isinstance(body, dict) else None
        if isinstance(data, dict):
            events.append((kind, (data, status_request(response.request)) if kind == 'status' else data))
    except Exception:
        pass


def status_code(data):
    for key, value in data.items():
        if key.lower().replace('_', '') == 'codestatus':
            if type(value) is int:
                return value
            if isinstance(value, str) and re.fullmatch(r'-?[0-9]{1,10}', value):
                return int(value)
    return None


def expiry_seconds(data, now):
    for key, value in data.items():
        name = key.lower().replace('_', '')
        try:
            if name in ('ttl', 'expiresin', 'expirein'):
                seconds = float(value)
            elif name in ('expiretime', 'expiresat', 'expiredat'):
                try:
                    timestamp = float(value)
                    seconds = timestamp / (1000 if timestamp > 1e11 else 1) - now.timestamp()
                except (ValueError, TypeError):
                    seconds = (datetime.fromisoformat(value.replace('Z', '+00:00')) - now).total_seconds()
            else:
                continue
            if 0 < seconds <= 86400:
                return seconds
        except (ValueError, TypeError, OverflowError):
            pass
    log_event('未读到官方有效期，到期前不主动换码')
    return None


def write_meta(seconds, kind='login'):
    now = datetime.now(timezone(timedelta(hours=8)))
    save_private(RUN_DIR / 'xhs-qr.meta', json.dumps({
        'kind': kind, 'generated_at': now.isoformat(),
        'expires_at': (now + timedelta(seconds=seconds)).isoformat() if seconds is not None else None,
    }).encode())


def refresh_qr(page, container):
    log_event('刷新二维码')
    (RUN_DIR / 'xhs-qr.png').unlink(missing_ok=True)
    (RUN_DIR / 'xhs-qr.meta').unlink(missing_ok=True)
    button = visible(container, 'text=/二维码.*(过期|失效)|点击刷新|刷新二维码/') if container else None
    if button:
        button.click()
    else:
        page.reload(wait_until='domcontentloaded')
        page.wait_for_timeout(1000)
        container = login_container(page)
        if not container:
            button = login_button(page)
            if button:
                button.click()
            container = login_container(page)
        if container:
            button = visible(container, 'text=扫码登录')
            if button:
                button.click()


@contextmanager
def login_display():
    if os.environ.get('DISPLAY'):
        yield
        return
    read_fd, write_fd = os.pipe()
    try:
        process = subprocess.Popen(['Xvfb', '-displayfd', str(write_fd), '-screen', '0', '1280x900x24'],
                                   pass_fds=(write_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    os.close(write_fd)
    try:
        with os.fdopen(read_fd) as output:
            display = output.readline().strip()
        if not display.isdigit():
            raise RuntimeError('虚拟显示启动失败')
        old_display = os.environ.get('DISPLAY')
        os.environ['DISPLAY'] = ':' + display
        try:
            yield
        finally:
            if old_display is None:
                os.environ.pop('DISPLAY', None)
            else:
                os.environ['DISPLAY'] = old_display
    finally:
        process.terminate()
        process.wait()


def proxy_options(proxy):
    proxy = proxy if proxy is not None else os.environ.get('MEDIA_DL_PROXY')
    if not proxy:
        return {}
    return {'proxy': {'server': proxy if '://' in proxy else 'http://' + proxy}}


def login(proxy=None):
    deadline = time.monotonic() + 1800
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        path = RUN_DIR / 'xhs-qr.png'
        path.unlink(missing_ok=True)
        (RUN_DIR / 'xhs-qr.meta').unlink(missing_ok=True)
        save_private(RUN_DIR / 'xhs-login.log', b'')
        (RUN_DIR / 'xhs-login.status').unlink(missing_ok=True)
        profile = profile_path()
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile.chmod(0o700)
        with login_display(), playwright() as browser:
            context = browser.chromium.launch_persistent_context(
                str(profile), headless=False, args=['--disable-blink-features=AutomationControlled'],
                **proxy_options(proxy))
            try:
                context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                page = context.pages[0] if context.pages else context.new_page()
                events = []
                page.on('response', lambda response: response_data(response, events))
                page.goto('https://www.xiaohongshu.com/login', wait_until='domcontentloaded')
                page.wait_for_timeout(5000)
                issued = None
                qr_source = None
                current_id = None
                creates = {}
                poll_request = None
                poll_at = 0
                initial_sessions = {(c.get('domain'), c.get('value')) for c in context.cookies() if c.get('name') == 'web_session' and c.get('value')}
                create_seen = False
                no_create_at = time.monotonic() + 20
                risk_retry_at = 0
                entry_retried = False
                international = False
                state = None
                authenticated = False
                security_seen = security_active = security_saved = security_refreshing = False
                retry_at = time.monotonic() + 15 if urlparse(page.url).path.rstrip('/') == '/login' else 0
                had_container = False
                while time.monotonic() < deadline:
                    security = security_container(page)
                    if security:
                        if not security_active:
                            security_active = security_seen = True
                            security_saved = security_refreshing = False
                            save_private(RUN_DIR / 'xhs-page.png', page.screenshot())
                        expired_security = visible(security, 'text=/Expired\\. Click QR code to refresh|二维码.*(过期|失效)|点击刷新|刷新二维码/i')
                        if expired_security:
                            security_saved = False
                            if not security_refreshing:
                                expired_security.click()
                                security_refreshing = True
                        else:
                            security_refreshing = False
                            if not security_saved and save_page_qr(page, security, path, security=True):
                                write_meta(None, 'verify')
                                set_status('verify')
                                log_event('页面出现安全验证，已截取页面原生码')
                                security_saved = True
                        authenticated = authenticated or any(kind == 'identity' for kind, _ in events)
                        events.clear()
                        page.wait_for_timeout(1000)
                        continue
                    security_active = False
                    sessions = {(c.get('domain'), c.get('value')) for c in context.cookies() if c.get('name') == 'web_session' and c.get('value')}
                    if logged_in(page, context, authenticated or ((state == 'confirmed' or security_seen) and bool(sessions - initial_sessions))):
                        set_status('logged_in')
                        return 0
                    on_login_page = urlparse(page.url).path.rstrip('/') == '/login'
                    if on_login_page:
                        dismiss = visible(page, 'text="我知道了"')
                        if dismiss:
                            dismiss.click()
                    container = login_container(page)
                    if container and not had_container:
                        button = None if on_login_page else visible(container, 'text=扫码登录')
                        if button:
                            button.click()
                    had_container = bool(container)
                    while events:
                        kind, data = events.pop(0)
                        if kind == 'identity':
                            authenticated = True
                        elif kind == 'create':
                            create_seen = True
                            identifier = create_id(data)
                            if identifier and isinstance(data.get('url'), str):
                                creates[identifier] = data
                                if issued is None and state not in ('scanned', 'confirmed'):
                                    qr_source = save_login_qr(page, container, data, identifier, path)
                                if issued is None and state not in ('scanned', 'confirmed') and qr_source:
                                    current_id = identifier
                                    issued = time.monotonic()
                                    write_meta(expiry_seconds(data, datetime.now(timezone.utc)))
                                    log_event('二维码生成')
                                    set_status('need_scan')
                                    state = 'need_scan'
                        elif kind == 'status' and issued is not None and state != 'confirmed':
                            data, request = data
                            if not request:
                                continue
                            matches = request['id'] == current_id
                            log_event('状态轮询与当前码一致' if matches else '状态轮询与当前码不一致')
                            value = status_code(data)
                            log_event('状态码: ' + (str(value) if value is not None else '无'))
                            if not matches and state != 'scanned' and request['id'] in creates:
                                candidate = creates[request['id']]
                                source = save_login_qr(page, container, candidate, request['id'], path)
                                if source:
                                    qr_source = source
                                    current_id = request['id']
                                    issued = time.monotonic()
                                    write_meta(expiry_seconds(candidate, datetime.now(timezone.utc)))
                                    state = 'need_scan'
                                    log_event('已改为跟随页面正在轮询的码')
                                    matches = True
                            if matches:
                                poll_request = request
                                if value == 1 and state != 'scanned':
                                    set_status('scanned')
                                    state = 'scanned'
                                elif value == 2:
                                    state = 'confirmed'
                                elif value == 3:
                                    state = 'expired'
                    if state == 'need_scan' and qr_source == 'generated' and save_page_qr(page, container, path, current_id):
                        qr_source = 'native'
                        log_event('码来源: 页面原生')
                    if poll_request and state not in ('confirmed', 'expired') and time.monotonic() >= poll_at:
                        data = poll_status(page, poll_request)
                        poll_at = time.monotonic() + 2
                        value = status_code(data) if isinstance(data, dict) else None
                        log_event('状态码: ' + (str(value) if value is not None else '无'))
                        if isinstance(data, dict):
                            if value == 1 and state != 'scanned':
                                set_status('scanned')
                                state = 'scanned'
                            elif value == 2:
                                state = 'confirmed'
                            elif value == 3:
                                state = 'expired'
                    if state == 'confirmed' or authenticated or security_seen:
                        page.wait_for_timeout(1000)
                        continue
                    risk = time.monotonic() >= risk_retry_at and visible(page, 'text=薯队长遇到了点小麻烦')
                    no_create = issued is None and not create_seen and time.monotonic() >= no_create_at
                    expired = container and visible(container, 'text=/二维码.*(过期|失效)|点击刷新|刷新二维码/')
                    if state != 'scanned' and ((issued is None and (risk or no_create)) or expired or state == 'expired'):
                        if risk or no_create:
                            log_event('登录页风控提示，将重试' if risk else '登录码等待超时，将重试')
                        issued = current_id = poll_request = None
                        creates.clear()
                        state = None
                        events.clear()
                        if (risk or no_create) and entry_retried and not international:
                            log_event('改走国际站入口重试')
                            international = True
                            page.goto('https://www.rednote.com/explore', wait_until='domcontentloaded')
                            page.wait_for_timeout(3000)
                            button = login_button(page)
                            if button:
                                button.click()
                            had_container = False
                        else:
                            refresh_qr(page, container)
                            if risk or no_create:
                                entry_retried = True
                        create_seen = False
                        no_create_at = time.monotonic() + 20
                        risk_retry_at = no_create_at
                        continue
                    if issued is None and state != 'scanned' and not container and time.monotonic() >= retry_at:
                        log_event('未找到登录容器，将重试')
                        button = None if on_login_page else login_button(page)
                        if button:
                            button.click()
                            page.wait_for_timeout(3000)
                        else:
                            page.goto('https://www.rednote.com/explore' if international else 'https://www.xiaohongshu.com/explore', wait_until='domcontentloaded')
                            page.wait_for_timeout(3000)
                        retry_at = time.monotonic() + 3
                    page.wait_for_timeout(1000)
                set_status('expired')
                return 1
            finally:
                context.close()
    except Exception as exc:
        log_event(f'登录失败: {type(exc).__name__}')
        set_status('error')
        return 1


def resolve_id(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not is_xhs(url):
        raise ValueError('不支持的小红书链接')
    match = re.fullmatch(r'/(?:explore|discovery/item)/([a-zA-Z0-9_-]+)/?', parsed.path)
    if not match:
        raise ValueError('无法识别小红书笔记 ID')
    return match.group(1)


def parse_page(html, item_id):
    match = re.search(r'window\.__INITIAL_STATE__\s*=\s*', html)
    if not match:
        raise ValueError('笔记页缺少数据')
    source = html[match.end():].lstrip()
    source = re.sub(r'"(?:\\.|[^"\\])*"|\bundefined\b|\bnew\s+(?:Map|Set)\s*\(\s*\[\s*\]\s*\)',
                    lambda match: ('null' if match.group() == 'undefined' else '{}')
                    if not match.group().startswith('"') else match.group(), source)
    state = json.JSONDecoder().raw_decode(source)[0]
    return parse_state(state, item_id)


def parse_state(state, item_id):
    note = state['note']['noteDetailMap'][item_id]['note']
    video = note.get('video') or {}
    streams = (video.get('media') or {}).get('stream') or {}
    stream = next((entry for entries in streams.values() for entry in entries if entry.get('masterUrl') or entry.get('master_url')), None)
    if stream is None:
        raise ValueError(NO_VIDEO)
    duration = video.get('duration')
    if isinstance(duration, (int, float)) and duration > 1000:
        duration /= 1000
    play_url = stream.get('masterUrl') or stream.get('master_url')
    if play_url.startswith('http://'):
        play_url = 'https://' + play_url[7:]
    elif play_url.startswith('//'):
        play_url = 'https:' + play_url
    return {'title': note.get('title') or note.get('desc', '').split('\n')[0][:80],
            'author': note.get('user', {}).get('nickname'), 'duration_seconds': duration,
            'width': stream.get('width'), 'height': stream.get('height'),
            'play_url': play_url}


def fetch(url, out, proxy=None):
    profile = profile_path()
    if not profile.is_dir():
        raise ValueError(LOGIN_REQUIRED)
    with playwright() as browser:
        context = browser.chromium.launch_persistent_context(str(profile), headless=True, **proxy_options(proxy))
        try:
            if not has_session(context):
                raise ValueError(LOGIN_REQUIRED)
            if urlparse(url).scheme != 'https' or not is_xhs(url):
                raise ValueError('不支持的小红书链接')
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until='domcontentloaded')
            if not has_session(context) or urlparse(page.url).path.startswith('/login') or visible(page, '[class*="login-container"], [class*="login-modal"]'):
                raise ValueError(LOGIN_REQUIRED)
            item_id = resolve_id(page.url)
            try:
                note = page.evaluate('''id => {
                    const note = window.__INITIAL_STATE__.note.noteDetailMap[id].note;
                    const video = note.video || {};
                    const streams = video.media?.stream || {};
                    return {title: note.title, desc: note.desc || '', user: {nickname: note.user?.nickname},
                        video: {duration: video.duration, media: {stream: Object.fromEntries(
                            Object.entries(streams).map(([key, entries]) => [key, entries.map(
                                ({masterUrl, master_url, width, height}) => ({masterUrl, master_url, width, height}))]))}}};
                }''', item_id)
                info = parse_state({'note': {'noteDetailMap': {item_id: {'note': note}}}}, item_id)
            except Exception:
                info = parse_page(page.content(), item_id)
            item_id = item_id[:100]
            play_url = info.pop('play_url')
            if urlparse(play_url).scheme != 'https':
                raise ValueError('播放地址无效')
            response = context.request.get(play_url, headers={'Referer': 'https://www.xiaohongshu.com/'}, timeout=60000)
            if not response.ok:
                raise ValueError('视频下载失败')
            data = response.body()
            content_type = response.headers.get('content-type', '').split(';')[0]
            is_webm = data.startswith(b'\x1a\x45\xdf\xa3')
            if len(data) < 1024 or data.lstrip().lower().startswith((b'<!doctype html', b'<html')) or not (content_type.startswith('video/') or data[4:8] == b'ftyp' or is_webm):
                raise ValueError('下载内容不是视频')
            name = item_id + ('.webm' if is_webm or content_type == 'video/webm' else '.mp4')
            temp_name = None
            try:
                with tempfile.NamedTemporaryFile(dir=out, delete=False) as temp:
                    temp_name = temp.name
                    temp.write(data)
                os.replace(temp_name, out / name)
            finally:
                if temp_name and os.path.exists(temp_name):
                    os.unlink(temp_name)
            return item_id, info, name
        finally:
            context.close()
