"""Synthetic worker guards: no sockets, credentials or global logging setup."""
import datetime as dt
import json
import io
import sqlite3
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from hub_insights import collect
from hub_store import Store, TZ
from hub_web import Hub, PRINCIPAL_KEY, mount
from test_hub import QUERY


class WorkerGuards(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = 'browser_ready'
        self.response = {'ok': False, 'code': 'busy'}
        self.calls = []

        def bridge(command, *args):
            self.calls.append(command)
            if command == 'status':
                return {'ok': True, 'state': self.state}
            if isinstance(self.response, Exception):
                raise self.response
            return self.response

        self.hub = Hub(self.temp.name, bridge)
        self.s = self.hub.store
        self.addCleanup(self.s.close)

        async def inline(func, *args):
            return func(*args)
        p = patch('hub_web.asyncio.to_thread', inline)
        p.start()
        self.addCleanup(p.stop)
        self.day = dt.datetime.now(TZ).date().isoformat()
        self.s.set('daily', {'date': self.day, 'count': 3})

    def submit(self, query=None):
        return self.s.submit(query or QUERY, 'synthetic', str(time.time_ns()))[0]

    async def test_unavailable_preflight_preserves_queue_and_attempts(self):
        job = self.submit()
        self.state = 'unavailable'
        with self.assertLogs('index_hub', 'WARNING'):
            await self.hub.run_one()
        self.assertEqual(self.s.job(job['id']), job)
        self.assertEqual(self.s.get('daily')['count'], 3)
        self.assertIsNone(self.s.get('last_attempt_at'))
        self.assertEqual(self.s.get('paused')['code'], 'browser_unavailable')
        self.assertFalse(await self.hub.run_one())
        self.assertEqual(self.calls, ['status'])

    async def test_connection_errors_refund_and_pause(self):
        for exc in (ConnectionRefusedError(), FileNotFoundError()):
            with self.subTest(exc=type(exc).__name__):
                self.s.set('paused', None)
                self.s.set('last_attempt_at', 0)
                job = self.submit()
                self.response = exc
                with self.assertLogs('index_hub', 'WARNING'):
                    await self.hub.run_one()
                self.assertEqual(self.s.job(job['id'])['status'], 'queued')
                self.assertEqual(self.s.get('daily')['count'], 3)
                self.assertEqual(self.s.get('paused')['code'], 'browser_unavailable')
                self.s.update_job(job['id'], 'failed')

    async def test_browser_reply_failure_is_not_refunded(self):
        job = self.submit()
        self.response = {'ok': False, 'code': 'browser_unavailable'}
        await self.hub.run_one()
        self.assertEqual(self.s.job(job['id'])['status'], 'failed')
        self.assertEqual(self.s.get('daily')['count'], 4)
        self.assertEqual(self.s.get('paused')['code'], 'browser_unavailable')

    async def test_busy_limit_and_cooldown(self):
        self.hub.busy_requeue_limit = 2
        job = self.submit()
        for index, code in enumerate(('busy', 'cooldown', 'busy'), 1):
            self.response = {'ok': False, 'code': code}
            self.s.set('last_attempt_at', 0)
            await self.hub.run_one()
            result = self.s.job(job['id'])
            self.assertEqual(result['requeues'], index)
            self.assertEqual(self.s.get('daily')['count'], 3)
            self.assertEqual(result['status'], 'failed' if index == 3 else 'queued')
        self.assertEqual(result['error']['code'], 'busy_requeue_exhausted')
        self.hub.busy_requeue_limit = 0
        job = self.submit()
        self.s.set('last_attempt_at', 0)
        await self.hub.run_one()
        self.assertEqual(self.s.job(job['id'])['status'], 'failed')
        self.assertEqual(self.s.get('daily')['count'], 3)

    def test_requeue_transaction_rolls_back(self):
        job = self.submit()
        self.s.update_job(job['id'], 'running')
        before = self.s.job(job['id'])
        self.s.db.execute("CREATE TEMP TRIGGER reject_refund BEFORE INSERT ON settings WHEN NEW.k='daily' BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.requeue_job(job['id'], self.day, 20)
        self.assertEqual(self.s.job(job['id']), before)
        self.assertEqual(self.s.get('daily')['count'], 3)

    async def test_parameter_errors_refunded_without_pause(self):
        for code in ('invalid_query', 'invalid_dates', 'invalid_keywords', 'invalid_kind'):
            job = self.submit()
            self.s.set('last_attempt_at', 0)
            self.response = {'ok': False, 'code': code}
            await self.hub.run_one()
            self.assertEqual(self.s.job(job['id'])['error']['code'], code)
            self.assertEqual(self.s.job(job['id'])['status'], 'failed')
            self.assertEqual(self.s.get('daily')['count'], 3)
            self.assertIsNone(self.s.get('paused'))

    async def test_no_data_cache_submit_worker_read_and_expiry(self):
        query = {**QUERY, 'kind': 'audience'}
        job = self.submit(query)
        self.response = {'ok': False, 'code': 'no_data'}
        await self.hub.run_one()
        self.assertEqual(self.s.job(job['id'])['status'], 'no_data')
        self.assertTrue(self.s.insight(query)['no_data'])
        self.calls.clear()
        cached = self.submit(query)
        self.assertEqual(cached['status'], 'no_data')
        self.assertTrue(cached['cache_hit'])
        # Also exercise a job queued before another task filled the cache.
        self.s.update_job(cached['id'], 'queued')
        await self.hub.run_one()
        self.assertEqual(self.s.job(cached['id'])['status'], 'no_data')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.s.get('daily')['count'], 4)
        app = web.Application()
        mount(app, self.hub)
        from urllib.parse import urlencode
        path = '/api/v1/insights?' + urlencode({
            'keyword': query['keywords'][0], 'kind': 'audience',
            'start_date': query['start_date'], 'end_date': query['end_date']})
        request = make_mocked_request('GET', path, app=app)
        request[PRINCIPAL_KEY] = {'scopes': ['read']}
        match = await app.router.resolve(request)
        response = await match.handler(request)
        self.assertEqual(json.loads(response.text)['status'], 'no_data')
        with self.s.db:
            self.s.db.execute('UPDATE insights SET fetched=?', (time.time() - 86401,))
        expired = self.submit(query)
        self.assertEqual(expired['status'], 'queued')
        self.s.set('last_attempt_at', 0)
        await self.hub.run_one()
        self.assertEqual(self.calls, ['status', 'insight'])
        self.assertEqual(self.s.get('daily')['count'], 5)

    async def test_start_logs_interrupt_counts(self):
        self.submit()
        running = self.submit()
        self.s.update_job(running['id'], 'running')
        with self.assertLogs('index_hub', 'INFO') as logs:
            await self.hub.start(None)
        self.hub.worker_task.cancel()
        try:
            await self.hub.worker_task
        except __import__('asyncio').CancelledError:
            pass
        self.assertIn('interrupt_pending requeued=1 interrupted=1', '\n'.join(logs.output))


class StoreAndCollectGuards(unittest.TestCase):
    def test_old_schema_migration(self):
        with tempfile.TemporaryDirectory() as root:
            path = root + '/old.sqlite'
            store = Store(path)
            job, _ = store.submit(QUERY, 'synthetic', 'old')
            store.db.execute('ALTER TABLE jobs DROP COLUMN requeues')
            store.db.commit()
            store.close()
            store = Store(path)
            try:
                self.assertEqual(store.job(job['id'])['requeues'], 0)
            finally:
                store.close()

    def test_limit_configuration(self):
        for value, expected in [('0', 0), ('3', 3), ('-1', 20), ('bad', 20), ('1.5', 20), ('', 20)]:
            with tempfile.TemporaryDirectory() as root, patch.dict('os.environ', {'INDEX_HUB_BUSY_REQUEUE_LIMIT': value}):
                hub = Hub(root, Mock())
                self.assertEqual(hub.busy_requeue_limit, expected)
                hub.store.close()

    def test_collect_validation_logs_only_code(self):
        api = SimpleNamespace(_lock=Mock())
        for body, code in [(None, 'invalid_query'),
                           ({'kind': 'unknown', 'keywords': ['合成隐私词']}, 'invalid_kind'),
                           ({**QUERY, 'kind': 'audience', 'keywords': []}, 'invalid_keywords'),
                           ({**QUERY, 'kind': 'audience', 'end_date': 'bad'}, 'invalid_dates')]:
            with self.assertLogs('index_hub.insights', 'WARNING') as logs:
                result = collect(api, body)
            self.assertEqual(result['code'], code)
            self.assertIn('collect invalid_query code=' + code, '\n'.join(logs.output))
            self.assertNotIn('合成隐私词', '\n'.join(logs.output))
        api._lock.acquire.assert_not_called()

    def test_collect_internal_value_error(self):
        client = Mock()
        client._post.side_effect = ValueError('synthetic JSON decode')
        api = SimpleNamespace(_lock=threading.Lock(), _client=lambda: client)
        with self.assertLogs('index_hub.insights', 'ERROR') as logs:
            result = collect(api, {**QUERY, 'kind': 'audience'})
        self.assertEqual(result['code'], 'request_failed')
        self.assertIn('ValueError', '\n'.join(logs.output))
        client.session.cookies.clear.assert_called_once()
        client.session.close.assert_called_once()


class BridgeParameterGuards(unittest.TestCase):
    def test_query_validation_preserves_error_without_socket(self):
        import hub_bridge
        server = Mock()
        handlers = []

        def capture_server(path, handler):
            handlers.append(handler)
            return server

        with tempfile.TemporaryDirectory() as root:
            from pathlib import Path
            with patch.object(hub_bridge, 'SOCKET', Path(root) / 'browser.sock'), patch.object(
                    hub_bridge.socketserver, 'UnixStreamServer', side_effect=capture_server), patch.object(
                    hub_bridge.os, 'chmod'), patch.object(hub_bridge.threading, 'Thread'):
                hub_bridge.start_bridge(Mock())
        handler = object.__new__(handlers[0])
        handler.request = Mock()
        handler.rfile = io.BytesIO(json.dumps({'command': 'query', 'payload': None}).encode() + b'\n')
        handler.wfile = io.BytesIO()
        with self.assertLogs('index_hub.bridge', 'WARNING'):
            handler.handle()
        self.assertEqual(json.loads(handler.wfile.getvalue())['code'], 'invalid_query')
