"""Synthetic bridge timeout tests; no network, sockets or global logging setup."""
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from hub_web import Hub, PRINCIPAL_KEY, mount
from test_hub import QUERY, fixture


class BridgeTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calls = []
        self.response = TimeoutError('synthetic timeout')

        def bridge(command, payload, timeout):
            self.calls.append(command)
            if command == 'status':
                return {'ok': True, 'state': 'browser_ready'}
            if isinstance(self.response, Exception):
                raise self.response
            return self.response

        self.hub = Hub(self.temp.name, bridge, bridge_timeout_pause_after=2)
        self.addCleanup(self.hub.store.close)
        self.s = self.hub.store
        self.sequence = 0

        async def local_thread_call(func, *args):
            return func(*args)

        thread_patch = patch('hub_web.asyncio.to_thread', local_thread_call)
        thread_patch.start()
        self.addCleanup(thread_patch.stop)

    async def attempt(self):
        self.sequence += 1
        self.s.set('last_attempt_at', 0)
        job, _ = self.s.submit(QUERY, 'fixture', str(self.sequence))
        with self.assertLogs('index_hub', 'WARNING'):
            self.assertTrue(await self.hub.run_one())
        self.assertEqual(self.s.job(job['id'])['error']['code'], 'bridge_timeout')

    async def endpoint(self, path, method='GET'):
        app = web.Application()
        mount(app, self.hub)
        request = make_mocked_request(method, path, headers={'X-Hub-Request': '1'}, app=app)
        request[PRINCIPAL_KEY] = {'admin': True, 'scopes': ['read']}
        match = await app.router.resolve(request)
        return await match.handler(request)

    async def test_two_timeouts_pause_and_block_queued_bridge_calls(self):
        await self.attempt()
        self.assertIsNone(self.s.get('paused'))
        self.assertEqual(self.s.get('consecutive_bridge_timeouts'), 1)
        self.s.submit(QUERY, 'fixture', 'second')
        queued, _ = self.s.submit(QUERY, 'fixture', 'queued')
        self.s.set('last_attempt_at', 0)
        with self.assertLogs('index_hub', 'WARNING'):
            self.assertTrue(await self.hub.run_one())
        paused = self.s.get('paused')
        self.assertEqual(paused['code'], 'bridge_timeout')
        self.assertEqual(paused['count'], 2)
        self.assertIsInstance(paused['since'], float)
        self.assertEqual(self.s.get('daily')['count'], 2)
        calls = list(self.calls)
        self.s.set('last_attempt_at', 0)
        self.assertFalse(await self.hub.run_one())
        self.assertEqual(self.calls, calls)
        self.assertEqual(self.s.job(queued['id'])['status'], 'queued')

    async def test_every_collection_reply_resets_streak(self):
        for response in ({'ok': True, **fixture()},
                         *({'ok': False, 'code': code} for code in
                           ('cooldown', 'busy', 'no_data', 'business_error'))):
            with self.subTest(response_code=response.get('code', 'ok')):
                await self.attempt()
                self.response = response
                self.sequence += 1
                job, _ = self.s.submit(QUERY, 'fixture', str(self.sequence))
                self.s.set('last_attempt_at', 0)
                self.assertTrue(await self.hub.run_one())
                self.assertEqual(self.s.get('consecutive_bridge_timeouts'), 0)
                # Avoid cache hits or requeued jobs hiding the next synthetic timeout.
                self.s.update_job(job['id'], 'failed')
                with self.s.db:
                    self.s.db.execute('DELETE FROM datasets')
                self.response = TimeoutError('synthetic timeout')
                await self.attempt()
                self.assertIsNone(self.s.get('paused'))
                self.s.set('consecutive_bridge_timeouts', 0)

    async def test_zero_disables_pause_but_counts_attempts(self):
        self.hub.bridge_timeout_pause_after = 0
        for _ in range(3):
            await self.attempt()
        self.assertIsNone(self.s.get('paused'))
        self.assertEqual(self.s.get('consecutive_bridge_timeouts'), 3)
        self.assertEqual(self.s.get('daily')['count'], 3)

    async def test_status_and_resume_reset(self):
        import json
        await self.attempt()
        await self.attempt()
        status = await self.endpoint('/api/v1/status')
        self.assertEqual(json.loads(status.text)['consecutive_bridge_timeouts'], 2)
        response = await self.endpoint('/api/v1/collection/resume', 'POST')
        self.assertEqual(response.status, 200)
        self.assertEqual(self.s.get('consecutive_bridge_timeouts'), 0)
        self.assertIsNone(self.s.get('paused'))
        self.assertEqual(self.s.get('daily')['count'], 2)

    def test_threshold_configuration(self):
        for value, expected in (('0', 0), ('3', 3), ('-1', 2), ('bad', 2),
                                ('1.5', 2), ('', 2)):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as root:
                with patch.dict('os.environ', {'INDEX_HUB_BRIDGE_TIMEOUT_PAUSE_AFTER': value}):
                    hub = Hub(root, self.hub.bridge)
                try:
                    self.assertEqual(hub.bridge_timeout_pause_after, expected)
                finally:
                    hub.store.close()
