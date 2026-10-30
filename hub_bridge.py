"""Private Unix socket into the existing desktop process; no cookies leave it."""
import json
import logging
import os
import socket
import socketserver
import threading
from pathlib import Path

import client
from hub_store import HubError, exc_summary

ROOT = Path(__file__).resolve().parent
SOCKET = ROOT / '.cloud-state' / 'browser.sock'
MAX = 2 * 1024 * 1024
BRIDGE_TIMEOUT = 75
JS_MARGIN = 20  # Allow for evaluate_js calls and local processing.
# Worst case for one browser-side task: total deadline plus one blocking read plus JS margin.
if BRIDGE_TIMEOUT < client.REQUEST_TOTAL_DEADLINE + client.REQUEST_READ_TIMEOUT + JS_MARGIN:
    raise RuntimeError('Bridge timeout must cover the request deadline and browser processing margin')


def call_bridge(command, payload=None, timeout=BRIDGE_TIMEOUT):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(SOCKET))
        sock.sendall(json.dumps({'command': command, 'payload': payload}, ensure_ascii=False).encode() + b'\n')
        with sock.makefile('rb') as stream:
            raw = stream.readline(MAX + 1)
        if len(raw) > MAX:
            raise ValueError('bridge response size')
        return json.loads(raw)


def start_bridge(api):
    SOCKET.parent.mkdir(exist_ok=True, mode=0o700)
    if SOCKET.exists():
        SOCKET.unlink()

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
                command = command if command in ('status', 'insight', 'query') else 'unknown'
                if message.get('command') == 'status':
                    from urllib.parse import urlsplit
                    page = api._page
                    url = urlsplit(page.get_current_url() or '')
                    official = url.scheme == 'https' and url.hostname == 'creator.douyin.com'
                    needs_login = bool(page.evaluate_js("Boolean(document.querySelector('input[type=tel]')) && document.body.innerText.includes('扫码登录')")) if official else True
                    answer = {'ok': True, 'state': 'login_required' if needs_login else 'browser_ready',
                              'note': '浏览器就绪不等于账号认证已验证；以最近实际取数结果为准。'}
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
                logging.getLogger('index_hub.bridge').warning('command=%s invalid_query code=%s', command, exc.code)
                answer = {'ok': False, 'code': exc.code, 'message': str(exc)}
            except Exception as exc:
                logging.getLogger('index_hub.bridge').warning('command=%s %s', command, exc_summary(exc))
                answer = {'ok': False, 'code': 'browser_unavailable', 'message': '浏览器未就绪，请检查远程桌面。'}
            try:
                self.wfile.write(json.dumps(answer, ensure_ascii=False, allow_nan=False).encode() + b'\n')
            except (OSError, ValueError) as exc:
                logging.getLogger('index_hub.bridge').warning('command=%s write %s', command, exc_summary(exc))

    server = socketserver.UnixStreamServer(str(SOCKET), Handler)
    os.chmod(SOCKET, 0o600)
    thread = threading.Thread(target=server.serve_forever, name='hub-browser', daemon=True)
    thread.start()
    return server
