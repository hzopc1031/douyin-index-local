"""Private Unix socket into the existing desktop process; no cookies leave it."""
import json
import base64
import logging
import os
import socket
import socketserver
import threading
from pathlib import Path
from hub_paths import state_dir

import client
from hub_store import HubError, exc_summary

ROOT = Path(__file__).resolve().parent
SOCKET = ROOT / '.cloud-state' / 'browser.sock'


def _socket_path():
    # Preserve the public constant (including existing test overrides).
    return state_dir(ROOT) / 'browser.sock' if 'INDEX_HUB_STATE_DIR' in os.environ else SOCKET


MAX = 2 * 1024 * 1024
BRIDGE_TIMEOUT = 75
JS_MARGIN = 20  # Allow for evaluate_js calls and local processing.
# Worst case for one browser-side task: total deadline plus one blocking read plus JS margin.
if BRIDGE_TIMEOUT < client.REQUEST_TOTAL_DEADLINE + client.REQUEST_READ_TIMEOUT + JS_MARGIN:
    raise RuntimeError('Bridge timeout must cover the request deadline and browser processing margin')


def call_bridge(command, payload=None, timeout=BRIDGE_TIMEOUT):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(_socket_path()))
        sock.sendall(json.dumps({'command': command, 'payload': payload}, ensure_ascii=False).encode() + b'\n')
        with sock.makefile('rb') as stream:
            raw = stream.readline(MAX + 1)
        if len(raw) > MAX:
            raise ValueError('bridge response size')
        return json.loads(raw)


def qr_command(api, command):
    from qr_login import login_mode, QrBusy, QrUnsafeDebug, QrNoDisplay
    from sms_login import LoginBusy
    if login_mode() != 'qr':
        return {'ok': False, 'code': 'qr_disabled', 'message': '未启用扫码登录。'}
    manager = api._qr_manager()
    if command == 'qr_start':
        try:
            manager.start()
        except (QrBusy, QrUnsafeDebug, QrNoDisplay, LoginBusy) as exc:
            return {'ok': False, 'code': exc.code, **({'message': str(exc)} if isinstance(exc, QrNoDisplay) else {})}
    elif command == 'qr_cancel':
        manager.cancel()
    elif command == 'qr_image':
        png = manager.qr_png()
        return ({'ok': True, 'png_base64': base64.b64encode(png).decode('ascii'),
                 'qr_version': manager.status()['qr_version']} if png else
                {'ok': False, 'code': 'qr_not_ready'})
    return {'ok': True, **manager.status(), 'session': manager.holder_info()}


def browser_status(api):
    """Read local page/cookies only; no platform request or quota consumption."""
    from urllib.parse import urlsplit
    login_state = api._login_state()
    # A transferred QR session is already in memory and independent of the
    # official window's navigation (preserve the existing QR path).
    page_ready = login_state == 'qr_session'
    if not page_ready and api._page is not None:
        try:
            page = api._page
            url = urlsplit(page.get_current_url() or '')
            official = (url.scheme == 'https' and url.hostname == 'creator.douyin.com'
                        and not url.username and not url.password and url.port in (None, 443))
            page_ready = official and page.evaluate_js("Boolean(document.querySelector('input[type=tel]')) && document.body.innerText.includes('扫码登录')") is False
        except Exception:
            page_ready = False
    return {'ok': True,
            'state': 'browser_ready' if page_ready and login_state in ('cookie_present', 'qr_session') else 'login_required',
            'login_state': login_state,
            'note': '本地登录凭据检查不等于认证已验证；以最近实际取数结果为准。'}


def start_bridge(api):
    socket_path = _socket_path()
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if socket_path.exists():
        socket_path.unlink()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(BRIDGE_TIMEOUT)
            command = 'unknown'
            try:
                raw = self.rfile.readline(16385)
                if len(raw) > 16384:
                    raise ValueError
                message = json.loads(raw)
                command = message.get('command')
                command = command if command in ('status', 'insight', 'query', 'qr_start', 'qr_status', 'qr_image', 'qr_cancel') else 'unknown'
                if command.startswith('qr_'):
                    answer = qr_command(api, command)
                elif message.get('command') == 'status':
                    answer = browser_status(api)
                elif message.get('command') == 'insight':
                    from hub_insights import collect
                    answer = collect(api, message.get('payload'))
                elif message.get('command') == 'query':
                    from hub_store import validate_query
                    query = validate_query(message.get('payload'))
                    answer = api.fetch_batch('\n'.join(query['keywords']), query['end_date'], cloud=True, start_date=query['start_date'])
                    # The HTTP layer validates and projects the business result again.
                    answer.pop('folder', None)
                    answer.pop('comparison', None)
                else:
                    answer = {'ok': False, 'code': 'invalid_command', 'message': '不支持的内部操作。'}
            except HubError as exc:
                logging.getLogger('index_hub.bridge').warning('command=%s rejected code=%s', command, exc.code)
                answer = {'ok': False, 'code': exc.code, 'message': str(exc)}
            except Exception as exc:
                logging.getLogger('index_hub.bridge').warning('command=%s %s', command, exc_summary(exc))
                answer = {'ok': False, 'code': 'browser_unavailable', 'message': '浏览器未就绪，请检查远程桌面。'}
            try:
                self.wfile.write(json.dumps(answer, ensure_ascii=False, allow_nan=False).encode() + b'\n')
            except (OSError, ValueError) as exc:
                logging.getLogger('index_hub.bridge').warning('command=%s write %s', command, exc_summary(exc))

    server = socketserver.UnixStreamServer(str(socket_path), Handler)
    os.chmod(socket_path, 0o600)
    thread = threading.Thread(target=server.serve_forever, name='hub-browser', daemon=True)
    thread.start()
    return server
