"""Synthetic QR wiring tests: no browser, network or credential persistence."""
import json
import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app
import hub_bridge
import qr_login
from client import SanitizedClientError
import test_hub
from test_hub import QUERY


class WiringTests(unittest.TestCase):
    def test_mode(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(qr_login.login_mode(), 'desktop')
            for value in ('desktop', 'qr', 'sms'):
                os.environ['INDEX_HUB_LOGIN_MODE'] = value
                self.assertEqual(qr_login.login_mode(), value)
            os.environ['INDEX_HUB_LOGIN_MODE'] = 'invalid'
            with patch.object(qr_login, '_mode_warned', False), self.assertLogs('index_hub.qr_login') as logs:
                self.assertEqual(qr_login.login_mode(), 'desktop')
                self.assertEqual(qr_login.login_mode(), 'desktop')
            self.assertEqual(len(logs.output), 1)

    def test_desktop_disabled_without_playwright(self):
        before = {k for k in sys.modules if k.startswith('playwright')}
        with patch.dict(os.environ, {'INDEX_HUB_LOGIN_MODE': 'desktop'}):
            api = app.Api()
            for command in ('qr_start', 'qr_status', 'qr_image', 'qr_cancel'):
                self.assertEqual(hub_bridge.qr_command(api, command),
                                 {'ok': False, 'code': 'qr_disabled', 'message': '未启用扫码登录。'})
            self.assertFalse(hasattr(api, '_qr'))
        self.assertEqual(before, {k for k in sys.modules if k.startswith('playwright')})

    def test_qr_client_and_fallback_and_cleanup(self):
        with patch.dict(os.environ, {'INDEX_HUB_LOGIN_MODE': 'qr'}), patch('qr_login.QrLoginManager') as factory:
            api = app.Api()
            self.assertFalse(hasattr(api, '_qr'))
            session = Mock()
            factory.return_value.holder.take_session.return_value = (session, 'Synthetic UA')
            with patch('client.DouyinIndexClient') as client:
                self.assertIs(api._client(), client.return_value)
                client.assert_called_once_with(session=session, user_agent='Synthetic UA')
                client.side_effect = ValueError('synthetic')
                with self.assertRaises(ValueError):
                    api._client()
                session.cookies.clear.assert_called_once()
                session.close.assert_called_once()
            factory.return_value.holder.has_session.return_value = False
            with self.assertRaises(app.LoginRequired):
                api._client()
            api._on_auth_failed()
            factory.return_value.holder.clear.assert_called_once()

    def test_auth_failures_clear_holder(self):
        from hub_insights import collect
        with tempfile.TemporaryDirectory() as root, patch.object(app, 'OUTPUT', Path(root)), patch.dict(os.environ, {'INDEX_HUB_LOGIN_MODE': 'qr'}):
            api = app.Api()
            api._qr = Mock()
            api._store.lookup = Mock(return_value=None)
            client = Mock()
            client.query_many.side_effect = SanitizedClientError('authentication_required', 'synthetic')
            client._post.side_effect = SanitizedClientError('authentication_required', 'synthetic')
            with patch.object(api, '_client', return_value=client):
                self.assertEqual(api.fetch_batch('酵素', '2025-01-07')['code'], 'authentication_required')
                self.assertEqual(collect(api, {**QUERY, 'kind': 'audience'})['code'], 'authentication_required')
            self.assertEqual(api._qr.holder.clear.call_count, 2)

    def test_bridge_commands(self):
        with patch.dict(os.environ, {'INDEX_HUB_LOGIN_MODE': 'qr'}):
            manager = Mock()
            manager.status.return_value = dict(state='qr_ready', qr_version=3, expires_in=100, message='synthetic')
            manager.holder_info.return_value = dict(has_session=False, age_seconds=None)
            api = Mock()
            api._qr_manager.return_value = manager
            for command in ('qr_start', 'qr_status', 'qr_cancel'):
                self.assertEqual(hub_bridge.qr_command(api, command)['session'], manager.holder_info())
            manager.qr_png.return_value = b'synthetic-png'
            self.assertEqual(hub_bridge.qr_command(api, 'qr_image')['png_base64'], base64.b64encode(b'synthetic-png').decode())
            manager.qr_png.return_value = None
            self.assertEqual(hub_bridge.qr_command(api, 'qr_image')['code'], 'qr_not_ready')
            for error in (qr_login.QrBusy, qr_login.QrUnsafeDebug, qr_login.QrNoDisplay):
                manager.start.side_effect = error()
                self.assertEqual(hub_bridge.qr_command(api, 'qr_start')['code'], error.code)
            message = '有头扫码需要桌面显示环境（DISPLAY）'
            manager.start.side_effect = qr_login.QrNoDisplay(message)
            self.assertEqual(hub_bridge.qr_command(api, 'qr_start')['message'], message)


class RoutesTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_hub.ApiTests.asyncSetUp
    asyncTearDown = test_hub.ApiTests.asyncTearDown

    async def test_qr_routes(self):
        self.hub.bridge = lambda command, payload, timeout: ({'ok': True, 'png_base64': base64.b64encode(b'PNG').decode(), 'qr_version': 1} if command == 'qr_image' else {'ok': True, 'state': 'qr_ready', 'qr_version': 1, 'expires_in': 100, 'message': 'synthetic', 'session': {'has_session': False, 'age_seconds': None}})
        self.hub.store.set('daily', {'date': '2025-01-01', 'count': 7})
        self.hub.store.set('last_attempt_at', 123)
        for method, path in [('get', '/api/v1/login/qr'), ('get', '/api/v1/login/qr/image'), ('post', '/api/v1/login/qr/start'), ('post', '/api/v1/login/qr/cancel')]:
            self.assertEqual((await getattr(self.client, method)(path, headers=self.bearer)).status, 403)
        self.assertEqual((await self.client.get('/api/v1/login/qr')).status, 401)
        self.assertEqual((await self.client.post('/api/v1/login/qr/start', headers={'Authorization': self.admin['Authorization']})).status, 403)
        with patch.dict(os.environ, {'INDEX_HUB_LOGIN_MODE': 'qr'}):
            response = await self.client.get('/api/v1/login/qr', headers=self.admin)
            self.assertEqual((await response.json())['login_mode'], 'qr')
        self.assertEqual((await self.client.post('/api/v1/login/qr/start', headers=self.admin)).status, 200)
        self.assertEqual((await self.client.post('/api/v1/login/qr/start', headers=self.admin)).status, 429)
        response = await self.client.get('/api/v1/login/qr/image', headers=self.admin)
        self.assertEqual(response.content_type, 'image/png')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(await response.read(), b'PNG')
        self.assertEqual((await self.client.post('/api/v1/login/qr/cancel', headers=self.admin)).status, 200)
        self.assertEqual(self.hub.store.get('daily')['count'], 7)
        self.assertEqual(self.hub.store.get('last_attempt_at'), 123)
        for code, status in [('qr_not_ready',404), ('qr_busy',409), ('qr_disabled',409), ('qr_unsafe_debug',409), ('qr_no_display',409), ('browser_unavailable',503)]:
            self.hub.bridge = lambda *args: {'ok': False, 'code': code}
            self.assertEqual((await self.client.get('/api/v1/login/qr/image', headers=self.admin)).status, status)
        self.hub.bridge = Mock(side_effect=OSError())
        self.assertEqual((await self.client.get('/api/v1/login/qr', headers=self.admin)).status, 503)


class DirectRouteTests(unittest.IsolatedAsyncioTestCase):
    """Exercise mounted handlers even where the sandbox forbids local sockets."""
    async def test_handlers_without_sockets(self):
        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request
        from hub_web import Hub, PRINCIPAL_KEY, mount
        from hub_store import HubError
        import json
        async def inline_thread(func, *args):
            return func(*args)
        thread_patch = patch('hub_web.asyncio.to_thread', inline_thread)
        thread_patch.start()
        self.addCleanup(thread_patch.stop)
        with tempfile.TemporaryDirectory() as root:
            bridge = Mock(return_value={'ok': True, 'state': 'idle', 'qr_version': 0,
                                        'expires_in': 0, 'message': 'synthetic',
                                        'session': {'has_session': False, 'age_seconds': None}})
            hub = Hub(root, bridge)
            self.addCleanup(hub.store.close)
            webapp = web.Application()
            mount(webapp, hub)
            hub.store.set('daily', {'date': '2025-01-01', 'count': 50})
            hub.store.set('last_attempt_at', 123)

            async def call(method, path, admin=True, csrf=True):
                request = make_mocked_request(method, path, app=webapp,
                                              headers={'X-Hub-Request': '1'} if csrf else {})
                request[PRINCIPAL_KEY] = {'admin': admin, 'scopes': ['read', 'query']}
                match = await webapp.router.resolve(request)
                request._match_info = match
                return await match.handler(request)

            for method, path in [('GET', '/api/v1/login/qr'), ('GET', '/api/v1/login/qr/image'),
                                 ('POST', '/api/v1/login/qr/start'), ('POST', '/api/v1/login/qr/cancel')]:
                with self.assertRaises(HubError) as caught:
                    await call(method, path, admin=False)
                self.assertEqual(caught.exception.status, 403)
            with self.assertRaises(HubError) as caught:
                await call('POST', '/api/v1/login/qr/start', csrf=False)
            self.assertEqual(caught.exception.status, 403)
            with patch.dict(os.environ, {'INDEX_HUB_LOGIN_MODE': 'qr'}):
                result = await call('GET', '/api/v1/login/qr')
                self.assertEqual(json.loads(result.body)['login_mode'], 'qr')
            with patch('hub_web.time', Mock(monotonic=Mock(return_value=100))):
                self.assertEqual((await call('POST', '/api/v1/login/qr/start')).status, 200)
                with self.assertRaises(HubError) as caught:
                    await call('POST', '/api/v1/login/qr/start')
                self.assertEqual(caught.exception.status, 429)
            with patch('hub_web.time', Mock(monotonic=Mock(return_value=110))):
                self.assertEqual((await call('POST', '/api/v1/login/qr/start')).status, 200)
            bridge.return_value = {'ok': True, 'png_base64': base64.b64encode(b'PNG').decode(), 'qr_version': 1}
            image = await call('GET', '/api/v1/login/qr/image')
            self.assertEqual(image.content_type, 'image/png')
            self.assertEqual(image.headers['Cache-Control'], 'no-store')
            self.assertEqual(image.body, b'PNG')
            await call('POST', '/api/v1/login/qr/cancel')
            for code, status in [('qr_not_ready',404), ('qr_busy',409), ('login_busy',409), ('busy_fetching',409), ('qr_disabled',409), ('qr_unsafe_debug',409), ('qr_no_display',409), ('browser_unavailable',503)]:
                bridge.return_value = {'ok': False, 'code': code}
                response = await call('GET', '/api/v1/login/qr/image')
                self.assertEqual(response.status, status)
                error = json.loads(response.text)['error']
                self.assertEqual(error['code'], code)
                self.assertTrue(error['message'])
                if code == 'qr_no_display':
                    self.assertEqual(error['message'], '有头扫码需要桌面显示环境（DISPLAY）')
            bridge.side_effect = OSError()
            with self.assertRaises(HubError) as caught:
                await call('GET', '/api/v1/login/qr')
            self.assertEqual(caught.exception.status, 503)
            self.assertEqual(hub.store.get('daily')['count'], 50)
            self.assertEqual(hub.store.get('last_attempt_at'), 123)

class HandlerTests(unittest.TestCase):
    def test_qr_commands_and_status_use_handler(self):
        import io
        import json
        class Server:
            def __init__(self, address, handler):
                self.handler = handler
            def serve_forever(self):
                pass
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'INDEX_HUB_LOGIN_MODE': 'qr'}), \
                patch.object(hub_bridge, 'SOCKET', Path(root) / 'browser.sock'), \
                patch.object(hub_bridge.socketserver, 'UnixStreamServer', Server), \
                patch.object(hub_bridge.os, 'chmod'):
            api = Mock()
            manager = api._qr_manager.return_value
            manager.status.return_value = {'state': 'idle', 'qr_version': 0, 'expires_in': 0, 'message': 'synthetic'}
            manager.holder_info.return_value = {'has_session': True, 'age_seconds': 10}
            manager.holder.has_session.return_value = True
            api._login_state.side_effect = lambda: 'qr_session' if manager.holder.has_session() else 'no_session_cookie'
            manager.qr_png.return_value = b'PNG'
            server = hub_bridge.start_bridge(api)
            def call(command):
                handler = object.__new__(server.handler)
                handler.request = Mock()
                handler.rfile = io.BytesIO(json.dumps({'command': command}).encode() + b'\n')
                handler.wfile = io.BytesIO()
                handler.handle()
                return json.loads(handler.wfile.getvalue())
            for command in ('qr_start', 'qr_status', 'qr_image', 'qr_cancel'):
                self.assertTrue(call(command)['ok'])
            self.assertEqual(call('status')['state'], 'browser_ready')
            api._page.get_current_url.assert_not_called()
            manager.holder.has_session.return_value = False
            api._page.get_current_url.return_value = 'https://creator.douyin.com/'
            api._page.evaluate_js.return_value = True
            self.assertEqual(call('status')['state'], 'login_required')
            with patch.dict(os.environ, {'INDEX_HUB_LOGIN_MODE': 'desktop'}):
                for command in ('qr_start', 'qr_status', 'qr_image', 'qr_cancel'):
                    self.assertEqual(call(command)['code'], 'qr_disabled')
            api.fetch_batch.assert_not_called()
