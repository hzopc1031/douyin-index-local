"""Offline gateway auth toggle tests; all state and credentials are synthetic."""
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from desktop_gateway import HUB_KEY, create_app


class AuthToggleTests(unittest.IsolatedAsyncioTestCase):
    async def make_client(self, value=None, credentials=True):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        state = root / 'synthetic-state'
        state.mkdir()
        (root / 'hub.html').write_text('<h1>Fixture hub</h1>')
        if credentials:
            (state / 'access.json').write_text(json.dumps({
                'username': 'fixture', 'password': 'synthetic-only'}))

        def bridge(command, payload, timeout):
            self.assertEqual(command, 'status')
            return {'ok': True, 'state': 'browser_ready'}

        env = {'INDEX_HUB_STATE_DIR': str(state)}
        if value is not None:
            env['INDEX_HUB_AUTH_ENABLED'] = value
        with patch.dict(os.environ, env, clear=True):
            if value == '0':
                original = Path.read_text

                def no_credential_read(path, *args, **kwargs):
                    self.assertNotEqual(path.name, 'access.json')
                    return original(path, *args, **kwargs)

                with patch.object(Path, 'read_text', no_credential_read):
                    with self.assertLogs('index_hub.auth', 'WARNING') as captured:
                        app = create_app(root, bridge=bridge, desktop_dir=root)
                self.assertEqual(len(captured.output), 1)
                self.assertIn('anonymous requests have administrator access', captured.output[0])
            else:
                app = create_app(root, bridge=bridge, desktop_dir=root)
        # Exercise real HTTP handlers without starting a query worker.
        app.on_startup.clear()
        client = TestClient(TestServer(app))
        self.addAsyncCleanup(client.close)
        await client.start_server()
        return client, app[HUB_KEY]

    async def test_default_explicit_on_and_invalid_values_require_basic(self):
        admin = 'Basic ' + base64.b64encode(b'fixture:synthetic-only').decode()
        for value in (None, '1', '', 'false', 'typo'):
            with self.subTest(value=value):
                client, _ = await self.make_client(value)
                for path in ('/', '/api/v1/status'):
                    self.assertEqual((await client.get(path)).status, 401)
                    self.assertEqual((await client.get(path, headers={
                        'Authorization': 'Basic invalid'})).status, 401)
                    self.assertEqual((await client.get(path, headers={
                        'Authorization': admin})).status, 200)

    async def test_off_skips_credential_read_and_auth_throttle(self):
        client, _ = await self.make_client('0', credentials=False)
        with patch('desktop_gateway.AuthThrottle.remaining', side_effect=AssertionError):
            for path in ('/', '/api/v1/status', '/api/v1/keys'):
                for headers in ({}, {'Authorization': 'Basic stale'}):
                    response = await client.get(path, headers=headers)
                    self.assertEqual(response.status, 200)
                    self.assertNotIn('WWW-Authenticate', response.headers)
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')

    async def test_off_keeps_bearer_scopes_and_rejects_invalid_keys(self):
        client, hub = await self.make_client('0', credentials=False)
        token = hub.store.issue_key('Synthetic reader', ['read'])['token']
        headers = {'Authorization': 'Bearer ' + token}
        self.assertEqual((await client.get('/api/v1/status', headers=headers)).status, 200)
        self.assertEqual((await client.get('/api/v1/keys', headers=headers)).status, 403)
        response = await client.get('/api/v1/status', headers={
            'Authorization': 'Bearer invalid-synthetic-key'})
        self.assertEqual(response.status, 401)

    async def test_off_keeps_csrf_origin_and_request_rate_guards(self):
        client, hub = await self.make_client('0', credentials=False)
        response = await client.post('/api/v1/desktop/ticket')
        self.assertEqual(response.status, 403)
        response = await client.post('/api/v1/desktop/ticket', headers={
            'X-Hub-Request': '1', 'Origin': 'https://invalid.example'})
        self.assertEqual(response.status, 403)
        response = await client.post('/api/v1/desktop/ticket', headers={'X-Hub-Request': '1'})
        self.assertEqual(response.status, 200)
        with patch('desktop_gateway.asyncio.open_connection', new_callable=AsyncMock) as connect:
            self.assertEqual((await client.get('/websockify')).status, 403)
            connect.assert_not_called()
        for _ in range(116):
            self.assertEqual((await client.get('/api/v1/platforms')).status, 200)
        response = await client.get('/api/v1/platforms')
        self.assertEqual(response.status, 429)
        self.assertEqual((await response.json())['error']['code'], 'api_rate_limited')
        self.assertEqual(response.headers['Retry-After'], '60')
