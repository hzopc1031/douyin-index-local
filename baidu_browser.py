"""Isolated, lazily started Baidu browser owned by one worker thread."""
import os
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
        self.root = root
        self.idle_seconds = idle_seconds
        self._queue = queue.Queue()
        self.started = False
        self.busy = False
        self.last_state = 'login_required'
        self._thread = threading.Thread(target=self._run, name='baidu-browser', daemon=True)
        self._thread.start()

    def call(self, action, *, touch=True):
        result = queue.Queue(maxsize=1)
        self._queue.put((action, result, touch))
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
                timeout = max(0, self.idle_seconds - (time.monotonic() - last_activity)) if context else None
                action, result, touch = self._queue.get(timeout=timeout)
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
                    # Chromium inherits the desktop process mask for profile files.
                    os.umask(0o077)
                    profile = prepare_profile(self.root)
                    lock = ProfileLock(profile).acquire()
                    playwright = sync_playwright().start()
                    context = playwright.chromium.launch_persistent_context(str(profile), headless=False)
                    self.started = True
                value = action(context)
                if touch:
                    last_activity = time.monotonic()
                result.put((True, value))
            except Exception as exc:
                if context is None:
                    try:
                        stop()
                    except Exception:
                        pass
                result.put((False, exc))
            finally:
                self.busy = False


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
    page = context.pages[0] if context.pages else context.new_page()
    from urllib.parse import urlsplit
    url = urlsplit(page.url)
    if (url.scheme, url.netloc) != ('https', 'index.baidu.com'):
        page.goto('https://index.baidu.com/', wait_until='domcontentloaded', timeout=25000)
    url = urlsplit(page.url)
    if (url.scheme, url.netloc) != ('https', 'index.baidu.com'):
        raise ProfileError('Unsafe Baidu page origin')
    return page


def login_open(context):
    page = _page(context)
    page.bring_to_front()
    login = page.get_by_text('登录', exact=True).first
    if login.is_visible(timeout=1000):
        login.click(timeout=3000)
    return {'ok': True, 'state': 'login_required'}


def page_status(page, *, origin='https://index.baidu.com', allow_loopback=False):
    PageTransport(page, origin=origin, allow_loopback=allow_loopback)
    state = page.evaluate('''origin => {
      if (location.origin !== origin) return 'login_required';
      if (!window.Paris || typeof window.Paris.getAcsInstance !== 'function') return 'signature_unavailable';
      const visible = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
      return [...document.querySelectorAll('a,button,span,div')].some(e => visible(e) && e.textContent.trim() === '退出登录') ? 'browser_ready' : 'login_required';
    }''', origin)
    return {'ok': True, 'state': state, 'live_enabled': True}


def status(context):
    return page_status(_page(context))
