import json
import os
import tempfile
import time
from pathlib import Path
import re
from urllib.parse import urlparse


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
    if status not in {'need_scan', 'logged_in', 'expired', 'error'}:
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


def qr(page):
    return visible(page, '[class*="qrcode"] img, [class*="qrcode"] canvas, [class*="qr-code"] img, [class*="qr-code"] canvas, img[alt*="二维码"], canvas[class*="qr"], img[class*="qr"]')


def login():
    deadline = time.monotonic() + 600
    try:
        profile = profile_path()
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile.chmod(0o700)
        with playwright() as browser:
            context = browser.chromium.launch_persistent_context(str(profile), headless=True)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto('https://www.xiaohongshu.com/login', wait_until='domcontentloaded')
                page.wait_for_timeout(2000)
                if logged_in(page, context):
                    write_status('logged_in')
                    return 0
                if not qr(page):
                    page.goto('https://www.xiaohongshu.com/explore', wait_until='domcontentloaded')
                    page.wait_for_timeout(2000)
                    if logged_in(page, context):
                        write_status('logged_in')
                        return 0
                    button = visible(page, 'text=登录')
                    if button:
                        button.click()
                    page.wait_for_timeout(1000)
                    button = visible(page, 'text=扫码登录')
                    if button:
                        button.click()
                    page.wait_for_timeout(1000)
                screenshot = None
                while time.monotonic() < deadline:
                    if logged_in(page, context):
                        write_status('logged_in')
                        return 0
                    refresh = visible(page, 'text=/二维码.*(过期|失效)|点击刷新|刷新二维码/')
                    if refresh:
                        refresh.click()
                        page.wait_for_timeout(1000)
                        screenshot = None
                    code = qr(page) or visible(page, '[class*="login-container"], [class*="login-modal"]')
                    if code and screenshot is None:
                        RUN_DIR.mkdir(parents=True, exist_ok=True)
                        path = RUN_DIR / 'xhs-qr.png'
                        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                        os.fchmod(fd, 0o600)
                        os.close(fd)
                        code.screenshot(path=str(path))
                        path.chmod(0o600)
                        screenshot = path
                        write_status('need_scan')
                    page.wait_for_timeout(1000)
                write_status('expired')
                return 1
            finally:
                context.close()
    except Exception:
        write_status('error')
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
