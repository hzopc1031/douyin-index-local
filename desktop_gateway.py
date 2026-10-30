"""TLS index portal/API plus authenticated noVNC. Never logs access credentials."""
import logging
import asyncio
import base64
import hmac
import json
import ssl
import time
from pathlib import Path
from aiohttp import web, WSMsgType
from hub_store import HubError
from hub_web import Hub, PRINCIPAL_KEY, mount, reply

ROOT = Path(__file__).resolve().parent
HUB_KEY = web.AppKey('hub', Hub)


def create_app(root=ROOT, *, bridge=None, desktop_dir='/usr/share/novnc'):
    root = Path(root)
    state = root / '.cloud-state'
    credentials = json.loads((state / 'access.json').read_text())
    expected = ('Basic ' + base64.b64encode((credentials['username'] + ':' + credentials['password']).encode()).decode()).encode()
    hub = Hub(root, bridge)
    failures, rates = {}, {}

    @web.middleware
    async def protected(request, handler):
        is_api = request.path.startswith('/api/')
        try:
            now = time.monotonic()
            ip = request.remote or 'unknown'
            failed = [t for t in failures.get(ip, []) if now - t < 60]
            if len(failed) >= 12:
                raise HubError('authentication_throttled', '认证失败次数过多，请一分钟后再试。', 429)
            auth = request.headers.get('Authorization', '')
            principal = None
            if hmac.compare_digest(auth.encode(), expected):
                principal = {'id': 'admin', 'admin': True, 'scopes': ['read', 'query']}
            elif is_api and auth.startswith('Bearer '):
                principal = hub.store.key_principal(auth[7:])
            if principal is None:
                if len(failures) >= 1024:
                    failures.pop(next(iter(failures)))
                failures[ip] = failed + [now]
                response = reply({'error': {'code': 'unauthorized', 'message': '请提供有效的 API Key。'}}, 401) if is_api else web.Response(status=401, text='Authentication required')
                response.headers['WWW-Authenticate'] = 'Bearer' if is_api else 'Basic realm="Douyin cloud"'
            else:
                request[PRINCIPAL_KEY] = principal
                recent = [t for t in rates.get(principal['id'], []) if now - t < 60]
                if len(recent) >= 120:
                    raise HubError('api_rate_limited', '接口请求过于频繁，请一分钟后再试。', 429)
                if len(rates) >= 1024 and principal['id'] not in rates:
                    rates.pop(next(iter(rates)))
                rates[principal['id']] = recent + [now]
                response = await handler(request)
        except HubError as exc:
            response = reply({'error': {'code': exc.code, 'message': str(exc)}}, exc.status)
        except web.HTTPException as exc:
            if not is_api:
                raise
            response = reply({'error': {'code': 'http_' + str(exc.status), 'message': '请求路径、方法或请求大小无效。'}}, exc.status)
        except (ConnectionError, asyncio.CancelledError):
            raise
        except Exception:
            response = reply({'error': {'code': 'internal_error', 'message': '服务暂不可用；未输出原始错误。'}}, 500)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        if response.status == 429:
            response.headers['Retry-After'] = '60'
        return response

    async def websocket(request):
        if not request[PRINCIPAL_KEY].get('admin') or request.headers.get('Origin') != 'https://' + request.host:
            raise web.HTTPForbidden()
        reader, writer = await asyncio.open_connection('127.0.0.1', 5907)
        ws = web.WebSocketResponse(protocols=('binary',), max_msg_size=4 * 1024 * 1024, heartbeat=30)
        try:
            await ws.prepare(request)

            async def incoming():
                async for message in ws:
                    if message.type != WSMsgType.BINARY:
                        break
                    writer.write(message.data)
                    await writer.drain()

            async def outgoing():
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    await ws.send_bytes(data)

            tasks = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            writer.close()
            await writer.wait_closed()
            await ws.close()
        return ws

    async def certificate(request):
        from hub_web import require
        require(request)
        return web.FileResponse(state / 'server.crt', headers={'Content-Disposition': 'attachment; filename="index-hub-ca.pem"'})

    app = web.Application(middlewares=[protected], client_max_size=32 * 1024)
    app[HUB_KEY] = hub
    mount(app, hub)
    app.router.add_get('/websockify', websocket)
    app.router.add_get('/api/v1/tls-certificate.pem', certificate)
    app.router.add_static('/desktop/', desktop_dir, show_index=False)
    return app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(ROOT / '.cloud-state/server.crt', ROOT / '.cloud-state/server.key')
    web.run_app(create_app(), host='0.0.0.0', port=8443, ssl_context=tls, access_log=None, print=None)
