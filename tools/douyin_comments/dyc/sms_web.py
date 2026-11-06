"""Resident loopback login entry point; only safe audit fields are logged."""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import resource
import secrets
import signal
import time
from urllib.parse import urlsplit

from .errors import Stop
from .sms_browser import SmsBrowser
from .sms_state import LoginError, LoginSession, SmsQuota, TERMINAL, audit, fields, payload, reject

ERRORS = {'invalid_request', 'unauthorized', 'csrf', 'busy', 'replayed_action',
          'replayed_code', 'wrong_state', 'send_interval', 'send_hour_limit',
          'send_day_limit', 'quota_unavailable', 'login_failed', 'invalid_points',
          'stale_frame', 'frame_unavailable', 'expired', 'qr_only'}
HEADERS = {'Cache-Control': 'no-store, max-age=0', 'Pragma': 'no-cache',
           'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff',
           'X-Frame-Options': 'DENY', 'Cross-Origin-Resource-Policy': 'same-origin',
           'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; "
               "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
               "form-action 'none'; base-uri 'none'"}


def origin_checked(value):
    try:
        url = urlsplit(value)
        if (url.scheme != 'https' or not url.hostname
                or url.username or url.password or url.path or url.query or url.fragment
                or not re.fullmatch(r'[a-z0-9.-]+(?::[0-9]{1,5})?', url.netloc)
                or url.port is not None and not 1024 <= url.port <= 65535):
            raise ValueError
        return value
    except ValueError:
        raise Stop(1, 'invalid_login_origin') from None


class WebApp:
    def __init__(self, origin, token, factory, *, clock=time.monotonic, no_token=False,
                 token_ttl=1800, port=8766):
        self.origin, self.host = origin, urlsplit(origin).netloc
        self.clock, self.factory = clock, factory
        self.token_hash = hashlib.sha256(token.encode()).digest()
        self.no_token, self.token_ttl, self.port = no_token, token_ttl, port
        self.auth_deadline = float('inf') if no_token else clock() + token_ttl
        self.auth_attempts = 0
        self.session = None
        self.starting = False
        self.cookie_hash = b''
        self.csrf = ''
        self.done = asyncio.Event()
        self.terminal_at = None
        self.preparation_failed = False
        self.retry_nonce = ''

    def public(self):
        data = self.session.public() if self.session else dict(
            state='failed', failure={'reason': 'preparation_failed'}, nonce=self.retry_nonce)
        data.update(preparation_failed=self.preparation_failed,
                    retryable=data['state'] in TERMINAL - {'succeeded'})
        return dict(data, csrf=self.csrf)

    async def prepare(self):
        self.starting = True
        self.preparation_failed = False
        self.retry_nonce = ''
        self.terminal_at = None
        audit('prepare_start')
        try:
            self.session = self.factory()
            async with asyncio.timeout(max(.001, min(90, self.session.deadline-self.clock()))):
                await self.session.browser.start()
            audit('prepare_result', result='ready')
            return 200, self.public()
        except asyncio.CancelledError:
            if self.session:
                await self.session.finish('cancelled')
            raise
        except Exception as error:
            audit('prepare_result', result='failed', reason='timeout' if isinstance(error, TimeoutError) else 'browser_failure')
            if self.session:
                await self.session.fail(error)
            self.preparation_failed = True
            self.retry_nonce = secrets.token_urlsafe(24)
            return 503, dict(self.public(), error='login_failed')
        finally:
            self.starting = False

    def authenticated(self, headers):
        # Cookies are scoped to a hostname, not a port. Other trusted services
        # on this Tailscale host may have unrelated cookies. Keep exactly one
        # unambiguous authentication entry; never accept duplicate credentials.
        values = []
        for entry in headers.get('cookie', '').split(';'):
            name, separator, value = entry.strip().partition('=')
            if name == '__Host-dyc_login':
                values.append(value if separator else '')
        if len(values) != 1 or re.fullmatch(r'[A-Za-z0-9_-]{32,128}', values[0]) is None:
            return False
        return bool(self.cookie_hash and hmac.compare_digest(
            hashlib.sha256(values[0].encode()).digest(), self.cookie_hash))

    async def dispatch(self, method, target, headers, body):
        """Also used by offline tests; only static codes cross this boundary."""
        action = 'unknown'
        try:
            static = method == 'GET' and target in ('/', '/app.js', '/app.css') and not body
            if headers.get('host') != self.host and not (
                    static and headers.get('host') == f'127.0.0.1:{self.port}'):
                reject('csrf')
            if static:
                name, content_type = {'/': ('sms_login.html', 'text/html; charset=utf-8'),
                                      '/app.js': ('sms_login.js', 'text/javascript; charset=utf-8'),
                                      '/app.css': ('sms_login.css', 'text/css; charset=utf-8')}[target]
                data = Path(__file__).with_name(name).read_bytes()
                if target == '/':
                    data = data.replace(b'{{NO_TOKEN}}', b'true' if self.no_token else b'false')
                    data = data.replace(b'{{TOKEN_TTL}}', str(self.token_ttl).encode())
                return 200, {'Content-Type': content_type}, data
            if method != 'POST' or target not in ('/auth', '/api') or len(body) > 8192:
                reject()
            if (headers.get('origin') != self.origin or
                headers.get('sec-fetch-site', 'same-origin') != 'same-origin' or
                headers.get('content-type') != 'application/json'):
                reject('csrf')
            audit('request_received', endpoint='auth' if target == '/auth' else 'api')
            obj = payload(body)
            candidate = obj.get('action')
            action = candidate if isinstance(candidate, str) and candidate in (
                'status', 'frame', 'send', 'code', 'mode', 'pointer', 'cancel', 'retry') else 'unknown'
            body = b''
            audit('action_received', action=action)
            try:
                if target == '/auth':
                    fields(obj, set() if self.no_token else {'token'})
                    if self.starting:
                        reject('busy')
                    if self.authenticated(headers):
                        return 200, {}, self.public()
                    if self.session or self.done.is_set() or not self.token_hash or self.clock() >= self.auth_deadline:
                        reject('unauthorized')
                    self.auth_attempts += 1
                    token = obj.pop('token', '')
                    if not self.no_token and (not isinstance(token, str) or len(token) > 128 or
                        not hmac.compare_digest(hashlib.sha256(token.encode()).digest(), self.token_hash)):
                        if self.auth_attempts >= 8:
                            self.token_hash = b''
                        reject('unauthorized')
                    token = None
                    self.token_hash = b''  # still one redemption; retry uses cookie + CSRF + nonce
                    cookie = secrets.token_urlsafe(32)
                    self.cookie_hash = hashlib.sha256(cookie.encode()).digest()
                    self.csrf = secrets.token_urlsafe(32)
                    status, data = await self.prepare()
                    return status, {'Set-Cookie': f'__Host-dyc_login={cookie}; Path=/; Secure; HttpOnly; SameSite=Strict'}, data
                if not self.authenticated(headers):
                    reject('unauthorized')
                if not self.csrf or not hmac.compare_digest(headers.get('x-dyc-csrf', ''), self.csrf):
                    reject('csrf')
                if self.starting:
                    reject('busy')
                if obj.get('action') == 'retry':
                    fields(obj, {'action', 'nonce'})
                    data = self.public()
                    if self.session and self.session.gate.locked():
                        reject('busy')
                    if not data['retryable']:
                        reject('wrong_state')
                    if not isinstance(obj['nonce'], str) or not hmac.compare_digest(obj['nonce'], data['nonce']):
                        reject('replayed_action')
                    audit('session_restart', reason='manual')
                    self.session = None
                    status, data = await self.prepare()
                    return status, {}, data
                if not self.session:
                    fields(obj, {'action'})
                    if obj['action'] != 'status':
                        reject('wrong_state')
                    return 200, {}, self.public()
                if action != 'status':
                    audit('action_admitted', action=action, reason='authenticated')
                result = await self.session.act(obj)
                return 200, {}, result | self.public()
            finally:
                obj.clear()
        except LoginError as error:
            code = str(error)
            code = code if code in ERRORS else 'login_failed'
            audit('request_rejected', action=action, reason=code,
                  path=target if target in ('/', '/app.js', '/app.css', '/auth', '/api') else 'other',
                  method=method if method in ('GET', 'POST') else 'other',
                  missing=getattr(error, 'missing', []))
            if (action == 'send' and code not in ('csrf', 'unauthorized', 'busy', 'qr_only') and
                    self.session and not self.starting and not self.session.gate.locked() and
                    hasattr(self.session.browser, 'send_evidence')):
                try:
                    async with self.session.locked('send_rejection_evidence'):
                        async with asyncio.timeout(5):
                            await self.session.browser.send_evidence('before')
                except Exception:
                    audit('send_evidence', session=self.session.audit_id, phase='before', outcome='capture_failed')
            if code == 'login_failed' and self.session and self.session.state in TERMINAL:
                return 200, {}, self.public()
            return (403 if code in ('csrf', 'unauthorized') else 409), {}, {'error': code}
        except asyncio.CancelledError:
            raise
        except Exception:
            data = {'error': 'login_failed'}
            if self.session and self.session.failure:
                data['failure'] = self.session.failure
            return 503, {}, data

    async def tick(self):
        if not self.session:
            if self.clock() >= self.auth_deadline:
                self.token_hash = b''
            return
        session = self.session
        if session.gate.locked() or self.starting:
            return
        async with session.locked('poll'):
            try:
                async with asyncio.timeout(max(.001, min(15, session.deadline-self.clock()))):
                    await session.poll()
            except Exception as error:
                if self.clock() >= session.deadline:
                    await session.fail(reason='login_expired', state='expired')
                elif not (isinstance(error, TimeoutError) and
                        getattr(session.browser, 'stage', '') == 'observe_login'):
                    await session.fail(error)
        if session.state == 'succeeded':
            if self.terminal_at is None:
                self.terminal_at = self.clock()
            elif self.clock()-self.terminal_at >= 10:
                self.cookie_hash, self.csrf = b'', ''
                self.done.set()

    async def close(self):
        self.token_hash = self.cookie_hash = b''
        self.csrf = ''
        if self.session:
            await self.session.finish(self.session.state if self.session.state in TERMINAL else 'cancelled')
        self.done.set()


async def read_request(reader):
    # A small strict HTTP/1 subset: no keep-alive, chunking, duplicate headers,
    # pipelining or proxy-supplied origin overrides. No values enter errors.
    async with asyncio.timeout(8):
        block = await reader.readuntil(b'\r\n\r\n')
        if len(block) > 8192:
            reject()
        lines = block.decode('ascii').split('\r\n')
        method, target, version = lines[0].split(' ')
        if version != 'HTTP/1.1' or method not in ('GET', 'POST') or len(target) > 100:
            reject()
        headers = {}
        for line in lines[1:-2]:
            key, value = line.split(':', 1)
            if not re.fullmatch('[A-Za-z0-9-]+', key) or key.lower() in headers:
                reject()
            if any(ord(c) < 32 for c in value):
                reject()
            headers[key.lower()] = value.strip()
        if 'transfer-encoding' in headers or 'expect' in headers:
            reject()
        length = headers.get('content-length', '0' if method == 'GET' else '')
        if not re.fullmatch(r'[0-9]{1,4}', length) or int(length) > 8192:
            reject()
        return method, target, headers, await reader.readexactly(int(length))


async def connection(app, reader, writer):
    body = b''
    try:
        method, target, headers, body = await read_request(reader)
        status, extra, data = await app.dispatch(method, target, headers, body)
        body = b''
    except asyncio.CancelledError:
        writer.close()
        raise
    except Exception:
        status, extra, data = 400, {}, {'error': 'invalid_request'}
    try:
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()
        response_headers = HEADERS | {'Content-Type': 'application/json; charset=utf-8'} | extra
        response_headers.update({'Content-Length': str(len(data)), 'Connection': 'close'})
        reason = {200: 'OK', 400: 'Bad Request', 403: 'Forbidden', 409: 'Conflict', 503: 'Unavailable'}[status]
        head = f'HTTP/1.1 {status} {reason}\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in response_headers.items()) + '\r\n'
        writer.write(head.encode() + data)
        async with asyncio.timeout(3):
            await writer.drain()
    except Exception:
        pass
    finally:
        body = data = b''
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 1)
        except Exception:
            pass


async def serve(args):
    origin = origin_checked(args.origin)
    if args.display is not None or not 1024 <= args.port <= 65535 or args.port == 107:
        raise Stop(1, 'invalid_login_options')
    # Reject forbidden runtime paths before touching the filesystem.
    for path in (args.profile_dir, args.state_dir):
        if str(path).startswith('/var/lib/douyin-index'):
            raise Stop(1, 'forbidden_login_directory')
    limits = getattr(args, 'startup_limits', False)
    no_token = getattr(args, 'no_token', False)
    token_ttl = getattr(args, 'token_ttl', 1800)
    if limits:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    terminal = None
    if not no_token:
        try:
            terminal = open('/dev/tty', 'w')
        except OSError:
            raise Stop(5, 'login_requires_terminal') from None
    token = '' if no_token else secrets.token_urlsafe(32)
    from .sms_solver import GapWorker
    quota = SmsQuota(args.state_dir)
    app = WebApp(origin, token, lambda: LoginSession(
        SmsBrowser(args.profile_dir, solver=GapWorker(), startup_limits=limits, state_dir=args.state_dir), quota, stop_on_verification=True),
        no_token=no_token, token_ttl=token_ttl, port=args.port)
    tasks = set()
    def accepted(reader, writer):
        if len(tasks) >= 8:
            writer.close()
            return
        task = asyncio.create_task(connection(app, reader, writer))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    server = None
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, app.done.set)
    try:
        server = await asyncio.start_server(accepted, '127.0.0.1', args.port, limit=16384)
        if terminal:
            terminal.write(f'手机登录页：{origin}\n一次性口令（{token_ttl} 秒内兑换）：{token}\n')
            terminal.flush()
            terminal.close()
        token = None
        while not app.done.is_set():
            await app.tick()
            try:
                await asyncio.wait_for(app.done.wait(), .5)
            except asyncio.TimeoutError:
                pass
    finally:
        token = None
        if terminal:
            terminal.close()
        if server:
            server.close()
            await server.wait_closed()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await app.close()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
    state = app.session.state if app.session else 'expired'
    code = 0 if state == 'succeeded' else 4
    return code, dict(exit_code=code, exit_reason='login_' + state)
