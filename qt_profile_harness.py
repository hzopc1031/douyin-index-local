"""Local-only subprocess fixture for test_webview_startup (no real login)."""
import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
import webview
import app
from browser_profile import ProfileLock
from webview_profile_patch import install_shared_profile
from webview_shutdown import QtShutdown
from webview_cookies import CookieSignals

# Numeric diagnostics only: prove which Qt callbacks execute, never print values.
signals = {'added': 0, 'removed': 0, 'load_all': 0, 'gui_thread': True}
original_added = CookieSignals.added
def count_added(self, cookie):
    signals['added'] += 1
    original_added(self, cookie)
CookieSignals.added = count_added
original_removed = CookieSignals.removed
def count_removed(self, cookie):
    signals['removed'] += 1
    original_removed(self, cookie)
CookieSignals.removed = count_removed
from PySide6.QtWebEngineCore import QWebEngineCookieStore
from PySide6.QtCore import QThread
original_load_all = QWebEngineCookieStore.loadAllCookies
def count_load_all(self):
    signals['load_all'] += 1
    signals['gui_thread'] &= QThread.currentThread() == self.thread()
    return original_load_all(self)
QWebEngineCookieStore.loadAllCookies = count_load_all

fixture_url = urlsplit(sys.argv[1])
if fixture_url.scheme != 'http' or fixture_url.hostname != '127.0.0.1':
    sys.exit(2)
if sys.argv[4] == 'patched' and not install_shared_profile(sys.argv[2], _test_cookie_host='127.0.0.1'):
    sys.exit(3)
from webview.platforms import qt

def local_requests_only(self, info):
    url = info.requestUrl()
    if url.scheme() not in ('about', 'data') and not (url.scheme() == 'http' and url.host() == '127.0.0.1'):
        info.block(True)

qt.BrowserView.RequestInterceptor.interceptRequest = local_requests_only
profile_lock = ProfileLock(sys.argv[2]).acquire()
shutdown = QtShutdown(profile_lock.release)
control = webview.create_window('control', html='<html><body>fixture control</body></html>')
page = webview.create_window('official fixture', sys.argv[1])
app.OUTPUT = Path(sys.argv[2]) / 'unused-fixture-output'
api = app.Api()
api._page = page

def check():
    deadline = time.monotonic() + 20
    for window in (control, page):
        if not window.events.loaded.wait(max(0, deadline - time.monotonic())):
            os._exit(4)
    value = page.evaluate_js("localStorage.getItem('synthetic')")
    def login_state():
        return api._login_state(_test_cookie_host='127.0.0.1')
    def cookies_ready():
        cookies = {m.key: m for c in page.get_cookies() for m in c.values()}
        expected = {'sessionid': 'synthetic-session', 'sid_guard': 'synthetic-guard',
                    'persistent_fixture': 'synthetic-persistent'}
        return (all(k in cookies and cookies[k].value == v for k, v in expected.items())
                and cookies['sessionid']['httponly'] and not cookies['sessionid']['expires']
                and cookies['sid_guard']['httponly'] and bool(cookies['sid_guard']['expires'])
                and not cookies['persistent_fixture']['httponly']
                and bool(cookies['persistent_fixture']['expires']))
    if sys.argv[3] == '1':
        if login_state() != 'no_session_cookie':
            os._exit(6)
        print(json.dumps({'login_before': 'no_session_cookie'}), flush=True)
        page.evaluate_js("localStorage.setItem('synthetic', 'fixture-only')")
        page.load_url(sys.argv[1] + 'set')
    else:
        # No Set-Cookie on this request: all three must come from disk, through
        # the production mirror AND the production requests.Session builder.
        flags = page.evaluate_js('window.fixtureCookies')
        if flags != {'session': True, 'guard': True, 'persistent': True}:
            print(json.dumps({'restored_via_http': False, 'request_cookie_flags': flags}), flush=True)
            os._exit(7)
        if value != 'fixture-only':
            os._exit(5)
        print(json.dumps({'restored_via_http': True}), flush=True)
    while not cookies_ready() and time.monotonic() < deadline:
        time.sleep(.05)
    if not cookies_ready() or login_state() != 'cookie_present':
        print(json.dumps({'login_state': login_state(), 'production_cookies_ready': bool(cookies_ready()), 'signals': signals}), flush=True)
        os._exit(7)
    try:
        api._client()  # Default production allowlist must reject loopback.
    except app.LoginRequired:
        pass
    else:
        os._exit(9)
    client = api._client(_test_cookie_host='127.0.0.1')
    try:
        with client.session.get(sys.argv[1] + 'echo', timeout=5, allow_redirects=False) as response:
            sent = response.status_code == 200 and response.json() == {'session': True, 'guard': True, 'persistent': True}
        if not sent:
            os._exit(10)
    finally:
        client.session.cookies.clear()
        client.session.close()
    if sys.argv[3] == '2':
        # Re-setting the restored HttpOnly session to the identical value must
        # retain the startup seed even though Qt emits no cookieAdded for it.
        page.load_url(sys.argv[1] + 'same-session')
        if not page.events.loaded.wait(max(0, deadline - time.monotonic())):
            os._exit(16)
        while not cookies_ready() and time.monotonic() < deadline:
            time.sleep(.05)
        if not cookies_ready():
            os._exit(17)
        # Replacing and removing restored cookies must supersede the startup
        # seed immediately, even while SQLite still contains the old value.
        page.load_url(sys.argv[1] + 'replace')
        def replaced():
            cookies = {m.key: m.value for c in page.get_cookies() for m in c.values()}
            return cookies.get('sessionid') == 'synthetic-replacement' and 'sid_guard' not in cookies
        while not replaced() and time.monotonic() < deadline:
            time.sleep(.05)
        if not replaced() or signals['removed'] == 0:
            os._exit(11)
        client = api._client(_test_cookie_host='127.0.0.1')
        try:
            with client.session.get(sys.argv[1] + 'echo-replaced', timeout=5, allow_redirects=False) as response:
                if response.json() != {'replacement': True, 'old_session': False, 'guard': False, 'persistent': True}:
                    os._exit(12)
        finally:
            client.session.cookies.clear()
            client.session.close()
        # A second identical session Set-Cookie may emit no event; keep the
        # authoritative value, then restore the fixture for the next restart.
        page.load_url(sys.argv[1] + 'set')
        while not cookies_ready() and time.monotonic() < deadline:
            time.sleep(.05)
        if not cookies_ready():
            os._exit(13)
        page.load_url(sys.argv[1] + 'set')
        page.events.loaded.wait(max(0, deadline - time.monotonic()))
        while not cookies_ready() and time.monotonic() < deadline:
            time.sleep(.05)
        if not cookies_ready():
            print(json.dumps({'identical_cookie_retained': False, 'signals': signals}), flush=True)
            os._exit(14)
        print(json.dumps({'live_replacement_and_removal': True, 'identical_cookie_retained': True}), flush=True)
    if not signals['gui_thread'] or signals['load_all'] < 2:
        os._exit(15)
    print(json.dumps({'loaded': True, 'all_three_cookies': True, 'python_sent_all_three': True,
                      'login_state': login_state(),
                      'cookies_set': sys.argv[3] == '1', 'signals': signals}), flush=True)
    if len(sys.argv) > 5 and sys.argv[5] == 'sigterm':
        # Parent sends SIGTERM two seconds after the flushed cookies_set marker.
        if not shutdown.finished.wait(30):
            os._exit(8)
        return
    # Allow Chromium to flush storage, then close normally for the next cycle.
    time.sleep(1)
    page.destroy()
    control.destroy()

try:
    webview.start(check, private_mode=False, storage_path=sys.argv[2], http_server=False, debug=False, gui='qt')
finally:
    shutdown.finish()
