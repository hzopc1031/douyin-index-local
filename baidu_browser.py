"""Isolated, lazily started Baidu browser owned by one worker thread."""
import os
import json
import logging
from pathlib import Path
import queue
import stat
import threading
import time

from browser_profile import ProfileError, ProfileLock, profile_root

ROOT = Path(__file__).resolve().parent


def prepare_profile(root=ROOT):
    root = Path(root).absolute()
    configured = Path(os.environ.get('INDEX_HUB_BAIDU_PROFILE_DIR', root / '.cloud-home' / 'baidu-profile'))
    if '..' in configured.parts:
        raise ProfileError('Unsafe Baidu profile path')
    path = configured.absolute()
    forbidden = [root / name for name in ('.cloud-state', '.runtime', 'results')]
    forbidden.append(profile_root(root).absolute())
    if any(path == p or p in path.parents or path in p.parents for p in forbidden):
        raise ProfileError('Unsafe Baidu profile location')
    # Check each existing ancestor before creating anything, including symlinks.
    for part in reversed((path, *path.parents)):
        if os.path.lexists(part):
            info = part.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise ProfileError('Unsafe Baidu profile path')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.lstat().st_uid != os.geteuid():
        raise ProfileError('Baidu profile parent must be owned')
    if path == root / '.cloud-home' / 'baidu-profile':
        parent = path.parent.lstat()
        if parent.st_uid != os.geteuid():
            raise ProfileError('Baidu profile parent must be owned')
        os.chmod(path.parent, 0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ProfileError('Baidu profile must be an owned directory')
    os.chmod(path, 0o700)
    for directory, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            entry = Path(directory) / name
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                if entry.parent == path and name in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
                    continue  # Chromium creates singleton symlinks.
                raise ProfileError('Unsafe Baidu profile contents')
            if info.st_uid != os.geteuid() or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ProfileError('Unsafe Baidu profile contents')
            os.chmod(entry, 0o700 if stat.S_ISDIR(info.st_mode) else 0o600)
    return path


class BaiduBrowser:
    def __init__(self, root=ROOT, idle_seconds=None):
        if idle_seconds is None:
            try:
                idle_seconds = int(os.environ.get('INDEX_HUB_BAIDU_IDLE_SECONDS', '300'))
            except ValueError:
                idle_seconds = 300
        if not 1 <= idle_seconds <= 86400:
            raise ValueError('Invalid browser idle timeout')
        try:
            hold_seconds = int(os.environ.get('INDEX_HUB_BAIDU_LOGIN_HOLD_SECONDS', '900'))
        except ValueError:
            raise ValueError('Invalid browser login hold') from None
        if not 1 <= hold_seconds <= 86400:
            raise ValueError('Invalid browser login hold')
        self.root = root
        self.idle_seconds = idle_seconds
        self.hold_seconds = hold_seconds
        self._queue = queue.Queue()
        self.started = False
        self.busy = False
        self.last_state = 'browser_idle'
        self.login_checked = False
        self._thread = threading.Thread(target=self._run, name='baidu-browser', daemon=True)
        self._thread.start()

    def call(self, action, *, touch=True, hold=False):
        result = queue.Queue(maxsize=1)
        self._queue.put((action, result, touch, hold))
        ok, value = result.get()
        if not ok:
            raise value
        return value

    def close(self):
        self.call(None)
        self._thread.join()

    def _run(self):
        context = playwright = lock = None
        last_activity = time.monotonic()
        hold_until = 0

        def stop():
            nonlocal context, playwright, lock
            try:
                if context:
                    context.close()
            finally:
                try:
                    if playwright:
                        playwright.stop()
                finally:
                    if lock:
                        lock.release()
                    context = playwright = lock = None
                    self.started = False

        while True:
            try:
                timeout = max(0, max(last_activity + self.idle_seconds, hold_until) - time.monotonic()) if context else None
                action, result, touch, hold = self._queue.get(timeout=timeout)
            except queue.Empty:
                try:
                    stop()
                except Exception:
                    pass
                continue
            if action is None:
                try:
                    stop()
                    result.put((True, None))
                except Exception as exc:
                    result.put((False, exc))
                return
            try:
                self.busy = True
                if context is None:
                    from playwright.sync_api import sync_playwright
                    profile = prepare_profile(self.root)
                    lock = ProfileLock(profile).acquire()
                    # Chromium inherits the mask; restore it for the desktop process.
                    previous_mask = os.umask(0o077)
                    try:
                        playwright = sync_playwright().start()
                        context = playwright.chromium.launch_persistent_context(str(profile), headless=False)
                    finally:
                        os.umask(previous_mask)
                    self.started = True
                value = action(context)
                now = time.monotonic()
                if touch == 'status':
                    self.last_state = value['state']
                    self.login_checked = True
                    if self.last_state == 'browser_ready':
                        hold_until = 0
                    else:
                        last_activity = now
                elif touch:
                    last_activity = now
                if hold:
                    hold_until = now + self.hold_seconds
                result.put((True, value))
            except Exception as exc:
                if touch == 'status':
                    self.last_state = 'login_required'
                    self.login_checked = False
                if context is None:
                    try:
                        stop()
                    except Exception:
                        pass
                else:
                    now = time.monotonic()
                    if touch:
                        last_activity = now
                    if hold:
                        hold_until = now + self.hold_seconds
                result.put((False, exc))
            finally:
                self.busy = False


def _safe_digest(value, depth=0):
    keys = {'status', 'data', 'uniqid', 'userIndexes', 'word', 'name', 'wordType',
            'all', 'pc', 'wise', 'startDate', 'endDate', 'type', 'generalRatio'}
    if type(value) is not dict or value.get('type') not in ('string', 'number', 'boolean', 'null', 'array', 'object'):
        raise ValueError
    kind = value['type']
    if kind == 'array':
        if set(value) - {'type', 'length', 'items'} or type(value.get('length')) is not int or not 0 <= value['length'] <= 1048576:
            raise ValueError
        if 'items' in value:
            if not value['length'] or depth >= 3:
                raise ValueError
            _safe_digest(value['items'], depth)
    elif kind == 'object':
        if set(value) - {'type', 'keys'} or (depth < 3) != ('keys' in value):
            raise ValueError
        if 'keys' in value:
            fields = value['keys']
            if type(fields) is not dict or set(fields) - keys:
                raise ValueError
            for child in fields.values():
                _safe_digest(child, depth + 1)
    elif set(value) != {'type'}:
        raise ValueError
    return value


class PageTransport:
    def __init__(self, page, *, origin='https://index.baidu.com', allow_loopback=False):
        from urllib.parse import urlsplit
        parsed = urlsplit(origin)
        if origin != 'https://index.baidu.com' and not (allow_loopback and parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port and not parsed.path and not parsed.query and not parsed.fragment and not parsed.username):
            raise ValueError('Invalid Baidu origin')
        self.page = page
        self.origin = origin

    def query(self, query):
        from urllib.parse import urlsplit
        import baidu_adapter
        current = urlsplit(self.page.url)
        expected = urlsplit(self.origin)
        if (current.scheme, current.netloc) != (expected.scheme, expected.netloc):
            raise baidu_adapter.BaiduError('network_error')
        script = Path(__file__).with_name('baidu_page.js').read_text(encoding='utf-8')
        result = self.page.evaluate(script, {**query, '_origin': self.origin})
        if not isinstance(result, dict) or result.get('status') != 'ok':
            code = result.get('status') if isinstance(result, dict) else None
            if code == 'response_schema' and 'schema_digest' in result:
                try:
                    digest = _safe_digest(result['schema_digest'])
                    logging.getLogger('index_hub.baidu').warning('schema_digest=%s', json.dumps(digest, separators=(',', ':')))
                except (TypeError, ValueError, RecursionError):
                    pass
            raise baidu_adapter.BaiduError(code if code in baidu_adapter.ERRORS else 'network_error')
        return result.get('rows')


_browser = None
_browser_guard = threading.Lock()


def browser():
    global _browser
    with _browser_guard:
        if _browser is None:
            _browser = BaiduBrowser()
        return _browser


def _page(context):
    from urllib.parse import urlsplit
    pages = context.pages
    page = next((p for p in pages if (urlsplit(p.url).scheme, urlsplit(p.url).netloc) ==
                 ('https', 'index.baidu.com')), None)
    if page is None:
        page = pages[0] if pages else context.new_page()
    url = urlsplit(page.url)
    if (url.scheme, url.netloc) != ('https', 'index.baidu.com'):
        page.goto('https://index.baidu.com/', wait_until='domcontentloaded', timeout=25000)
    url = urlsplit(page.url)
    if (url.scheme, url.netloc) != ('https', 'index.baidu.com'):
        raise ProfileError('Unsafe Baidu page origin')
    return page


def login_open(context):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    page = _page(context)
    page.bring_to_front()
    login = page.get_by_text('登录', exact=True).first
    if login.is_visible(timeout=1000):
        try:
            login.click(timeout=3000)
        except PlaywrightTimeoutError:
            pass
    return {'ok': True, 'state': 'login_required'}


def status(context, *, origin='https://index.baidu.com', allow_loopback=False):
    from urllib.parse import urlsplit
    try:
        pages = context.pages
        page = next((p for p in pages if (urlsplit(p.url).scheme, urlsplit(p.url).netloc) ==
                     (urlsplit(origin).scheme, urlsplit(origin).netloc)), None)
        if page is None:
            state = 'login_in_progress'
        else:
            PageTransport(page, origin=origin, allow_loopback=allow_loopback)
            now = time.time()
            logged_in = any(c.get('name') == 'BDUSS' and
                            (c.get('expires') == -1 or c.get('expires', 0) > now)
                            for c in context.cookies(origin))
            if not logged_in:
                state = 'login_required'
            else:
                ready = page.evaluate("origin => location.origin === origin && !!window.Paris && typeof window.Paris.getAcsInstance === 'function'", origin)
                state = 'browser_ready' if ready else 'signature_unavailable'
        return {'ok': True, 'state': state, 'live_enabled': True}
    except Exception:
        raise ProfileError('Baidu status unavailable') from None
