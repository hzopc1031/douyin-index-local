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
from .safety import safe_log


def is_xhs(url):
    host = urlparse(url).hostname or ''
    return any(host == domain or host.endswith('.' + domain) for domain in
               ('xiaohongshu.com', 'xhslink.com', 'xhslink.cn', 'rednote.com'))


LOGIN_REQUIRED = '需要先运行 python -m media_dl login xhs 扫码登录'
RUN_DIR = Path('/workspace/dev/media-dl-run')


def profile_path():
    return Path.home() / '.config/media-dl/xhs-profile'


def playwright():
    from playwright.sync_api import sync_playwright
    return sync_playwright()


def write_status(status):
    if status not in {'need_scan', 'scanned', 'logged_in', 'expired', 'error'}:
        raise ValueError('无效状态')
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    path = RUN_DIR / 'xhs-login.status'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(status + '\n')


def has_session(context):
    return any(cookie.get('name') == 'web_session' and cookie.get('value')
               for cookie in context.cookies('https://www.xiaohongshu.com'))


def visible(page, selector):
    locator = page.locator(selector)
    for index in range(locator.count()):
        if locator.nth(index).is_visible():
            return locator.nth(index)
    return None


def logged_in(page, context):
    return bool(has_session(context) and visible(page, 'a[href*="/user/profile/"], [data-testid="user-avatar"]'))


def login_container(page):
    container = visible(page, '.login-container, [class*="login-modal"], [role="dialog"]:has-text("登录"), dialog:has-text("登录")')
    if not container and urlparse(page.url).path.rstrip('/') == '/login':
        container = visible(page, 'body')
    return container


def login_button(page):
    return visible(page, 'button.login-btn:not([class*="submit"]), button:not([class*="submit"]):text-is("登录"), a:not([class*="submit"]):text-is("登录"), button:not([class*="submit"]):text-is("Log in"), a:not([class*="submit"]):text-is("Log in"), button:not([class*="submit"]):text-is("Log In"), a:not([class*="submit"]):text-is("Log In")')


def qr(container):
    codes = container.locator('[class*="qr"] img, [class*="qr"] canvas, img[class*="qr"], canvas[class*="qr"], img[alt*="二维码"], :text("扫码登录") ~ * img, :text("扫码登录") ~ * canvas')
    for index in range(codes.count()):
        code = codes.nth(index)
        if code.is_visible() and code.evaluate(r'''(element, container) => {
            if (element.tagName === 'IMG' && (!element.complete || !element.naturalWidth)) return false;
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


def save_image(image, path):
    import zxingcpp

    result = zxingcpp.read_barcode(image)
    valid, host, route, has_id = qr_info(result.text if result else None)
    log_event(f"校验{'通过' if valid else '失败'}: {host} {route} has_id={has_id}")
    if not valid:
        return False
    data = io.BytesIO()
    image.save(data, format='PNG')
    save_private(path, data.getvalue())
    return True


def save_qr(code, path):
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(code.screenshot())).convert('RGB')
    image = image.resize((image.width * 3, image.height * 3), Image.Resampling.NEAREST)
    return save_image(ImageOps.expand(image, border=24, fill='white'), path)


def save_url_qr(payload, path):
    import qrcode

    image = qrcode.QRCode(box_size=12, border=4)
    image.add_data(payload)
    image.make(fit=True)
    return save_image(image.make_image().convert('RGB'), path)


def response_data(response, events):
    try:
        path = urlparse(response.url).path.lower()
        if 'login/qrcode/create' in path:
            kind = 'create'
        elif 'login/qrcode/status' in path or '/api/qrcode/userinfo' in path:
            kind = 'status'
        else:
            return
        body = response.json()
        data = body.get('data', body) if isinstance(body, dict) else None
        if isinstance(data, dict):
            events.append((kind, data))
    except Exception:
        pass


def status_code(data):
    for key, value in data.items():
        if key.lower().replace('_', '') == 'codestatus':
            try:
                return int(value)
            except (ValueError, TypeError):
                pass
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
    log_event('未读到官方有效期，按 120 秒刷新')
    return 120


def write_meta(seconds):
    now = datetime.now(timezone(timedelta(hours=8)))
    save_private(RUN_DIR / 'xhs-qr.meta', json.dumps({
        'generated_at': now.isoformat(),
        'expires_at': (now + timedelta(seconds=seconds)).isoformat(),
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
    save_private(RUN_DIR / 'xhs-page.png', page.screenshot(full_page=True))


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


def login():
    deadline = time.monotonic() + 900
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
                str(profile), headless=False, args=['--disable-blink-features=AutomationControlled'])
            try:
                context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                page = context.pages[0] if context.pages else context.new_page()
                events = []
                page.on('response', lambda response: response_data(response, events))
                page.goto('https://www.xiaohongshu.com/login', wait_until='domcontentloaded')
                page.wait_for_timeout(5000)
                save_private(RUN_DIR / 'xhs-page.png', page.screenshot(full_page=True))
                issued = None
                refresh_at = None
                fallback_at = time.monotonic() + 5
                create_seen = False
                no_create_at = time.monotonic() + 20
                risk_retry_at = 0
                state = None
                retry_at = time.monotonic() + 15 if urlparse(page.url).path.rstrip('/') == '/login' else 0
                had_container = False
                while time.monotonic() < deadline:
                    if logged_in(page, context):
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
                        save_private(RUN_DIR / 'xhs-page.png', page.screenshot(full_page=True))
                    had_container = bool(container)
                    while events:
                        kind, data = events.pop(0)
                        if kind == 'create':
                            create_seen = True
                            if not isinstance(data.get('url'), str) or not save_url_qr(data['url'], path):
                                path.unlink(missing_ok=True)
                                (RUN_DIR / 'xhs-qr.meta').unlink(missing_ok=True)
                                issued = None
                                refresh_at = time.monotonic() + 100
                                if state is not None:
                                    set_status('error')
                                    state = None
                            else:
                                issued = time.monotonic()
                                seconds = expiry_seconds(data, datetime.now(timezone.utc))
                                refresh_at = issued + max(1, seconds - min(20, seconds / 4))
                                write_meta(seconds)
                                log_event('二维码生成')
                                set_status('need_scan')
                                state = 'need_scan'
                                save_private(RUN_DIR / 'xhs-page.png', page.screenshot(full_page=True))
                        elif kind == 'status' and issued is not None:
                            value = status_code(data)
                            if value == 1 and state != 'scanned':
                                set_status('scanned')
                                state = 'scanned'
                            elif value == 3:
                                refresh_at = 0
                    risk = time.monotonic() >= risk_retry_at and visible(page, 'text=薯队长遇到了点小麻烦')
                    no_create = issued is None and not create_seen and time.monotonic() >= no_create_at
                    expired = container and visible(container, 'text=/二维码.*(过期|失效)|点击刷新|刷新二维码/')
                    if risk or no_create or expired or (refresh_at is not None and time.monotonic() >= refresh_at):
                        if risk or no_create:
                            log_event('登录页风控提示，将重试' if risk else '登录码等待超时，将重试')
                        issued = refresh_at = None
                        state = None
                        events.clear()
                        refresh_qr(page, container)
                        fallback_at = time.monotonic() + 5
                        create_seen = False
                        no_create_at = time.monotonic() + 20
                        risk_retry_at = no_create_at
                    elif issued is None and not create_seen and container and time.monotonic() >= fallback_at:
                        for code in qr(container):
                            if save_qr(code, path):
                                issued = time.monotonic()
                                seconds = expiry_seconds({}, datetime.now(timezone.utc))
                                refresh_at = issued + 100
                                write_meta(seconds)
                                log_event('二维码生成')
                                set_status('need_scan')
                                state = 'need_scan'
                                save_private(RUN_DIR / 'xhs-page.png', page.screenshot(full_page=True))
                                break
                    if not container and time.monotonic() >= retry_at:
                        log_event('未找到登录容器，将重试')
                        button = None if on_login_page else login_button(page)
                        if button:
                            button.click()
                            page.wait_for_timeout(3000)
                        else:
                            page.goto('https://www.xiaohongshu.com/explore', wait_until='domcontentloaded')
                            page.wait_for_timeout(3000)
                        save_private(RUN_DIR / 'xhs-page.png', page.screenshot(full_page=True))
                        retry_at = time.monotonic() + 3
                    page.wait_for_timeout(1000)
                set_status('expired')
                return 1
            finally:
                context.close()
    except Exception as exc:
        message = safe_log(str(exc)).replace('\n', ' ').replace('\r', ' ')
        message = re.sub(r'(?i)(?:https?://|\b(?:cookie|url|qr_?id)\b).*', '[redacted]', message)
        log_event(f'登录失败: {type(exc).__name__}: {message[:200]}')
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
    source = re.sub(r'"(?:\\.|[^"\\])*"|\bundefined\b',
                    lambda match: 'null' if match.group() == 'undefined' else match.group(), source)
    state = json.JSONDecoder().raw_decode(source)[0]
    note = state['note']['noteDetailMap'][item_id]['note']
    video = note['video']
    streams = video['media']['stream']
    stream = next(entry for entries in streams.values() for entry in entries if entry.get('masterUrl') or entry.get('master_url'))
    duration = video.get('duration')
    if isinstance(duration, (int, float)) and duration > 1000:
        duration /= 1000
    return {'title': note.get('title') or note.get('desc', '').split('\n')[0][:80],
            'author': note.get('user', {}).get('nickname'), 'duration_seconds': duration,
            'width': stream.get('width'), 'height': stream.get('height'),
            'play_url': stream.get('masterUrl') or stream.get('master_url')}


def fetch(url, out):
    profile = profile_path()
    if not profile.is_dir():
        raise ValueError(LOGIN_REQUIRED)
    with playwright() as browser:
        context = browser.chromium.launch_persistent_context(str(profile), headless=True)
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
