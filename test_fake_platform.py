import asyncio
import csv
import io
import json
import unittest

from platforms import registry
import test_hub

QUERY = test_hub.QUERY


class SyntheticPlatform:
    description = {'id': 'synthetic', 'name': '合成平台', 'available': True,
                   'metric': 'fake', 'metric_name': '合成指数', 'unit': '分',
                   'region': '全国', 'description': '合成来源', 'modules': []}

    bridge_commands = ('synthetic_status',)

    def dispatch_bridge(self, command, payload):
        return {'ok': True, 'value': payload}

    def status_counts(self, store):
        return {'synthetic-word'}, 2

    async def async_status(self, hub):
        return self.status(hub.store)

    def interrupted(self, store):
        store.set('synthetic_interrupted', True)

    def repoint_dataset(self, store, keep, old):
        store.set('synthetic_repoint', [keep, old])

    def openapi_paths(self):
        return {'/api/v1/synthetic': {'get': {'responses': {'200': {'description': 'synthetic'}}}}}

    def mount_routes(self, app, hub, require, reply):
        async def synthetic(request):
            require(request, admin=True)
            return reply({'synthetic': True})
        app.router.add_get('/api/v1/synthetic', synthetic)

    def initialize(self, store):
        store.db.execute('CREATE TABLE IF NOT EXISTS fake_points(keyword TEXT, day TEXT, value INTEGER)')

    def validate_query(self, body, *, historical=False):
        from hub_store import _validate_douyin_query
        return {**_validate_douyin_query({**body, 'platform': 'douyin'}, historical=historical), 'platform': 'synthetic'}

    def paused(self, store):
        return store.get('fake_paused')

    def status(self, store):
        return {'paused': self.paused(store), 'attempts': store.get('fake_attempts', 0)}

    async def resume(self, hub):
        hub.store.set('fake_paused', None)

    def bridge_command(self, query):
        return 'query'

    def collect(self, api, query):
        return {'ok': True, 'value': 42}

    async def run_one(self, hub, row):
        store = hub.store
        if store.get('fake_cooldown'):
            return False
        if store.get('fake_attempts', 0) >= 1:
            store.update_job(row['id'], 'failed', error={'code': 'daily_limit'})
            return True
        query = json.loads(row['query'])
        store.set('fake_attempts', store.get('fake_attempts', 0) + 1)
        response = await asyncio.to_thread(hub.bridge, self.bridge_command(query), query, 75)
        with store.db:
            store.db.execute('INSERT INTO fake_points VALUES(?,?,?)',
                             (query['keywords'][0], query['end_date'], response['value']))
        store.update_job(row['id'], 'succeeded', result={'value': response['value']})
        return True

    def series(self, store, keyword, start=None, end=None):
        rows = store.db.execute('SELECT day,value FROM fake_points WHERE keyword=?', (keyword,))
        return {'platform': 'synthetic', 'keyword': keyword,
                'points': [{'date': row['day'], 'value': row['value']} for row in rows]}

    def csv_rows(self, data):
        yield ['date', 'value']
        for point in data['points']:
            yield [point['date'], point['value']]


class FakePlatformApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        registry.register(SyntheticPlatform())
        try:
            await test_hub.ApiTests.asyncSetUp(self)
        except BaseException:
            registry._platforms.pop('synthetic')
            raise
        registry.get('synthetic').initialize(self.hub.store)
        import hub_bridge
        from unittest.mock import patch
        self.socket_patch = patch('hub_bridge._socket_path', return_value=self.root / 'synthetic.sock')
        self.socket_patch.start()
        self.bridge_server = hub_bridge.start_bridge(object())
        self.commands = []
        def bridge(command, payload, timeout):
            self.commands.append((command, payload))
            if command == 'status':
                return {'ok': True, 'state': 'browser_ready'}
            return hub_bridge.call_bridge(command, payload, timeout)
        self.hub.bridge = bridge

    async def asyncTearDown(self):
        self.bridge_server.shutdown()
        self.bridge_server.server_close()
        self.socket_patch.stop()
        await test_hub.ApiTests.asyncTearDown(self)
        registry._platforms.pop('synthetic')

    async def test_submit_worker_storage_csv_status_and_resume(self):
        store = self.hub.store
        response = await self.client.get('/api/v1/platforms', headers=self.bearer)
        self.assertIn('synthetic', [p['id'] for p in (await response.json())['platforms']])
        response = await self.client.get('/api/v1/openapi.json', headers=self.bearer)
        self.assertIn('/api/v1/synthetic', (await response.json())['paths'])
        response = await self.client.get('/api/v1/synthetic', headers=self.admin)
        self.assertEqual((await response.json())['synthetic'], True)
        import hub_bridge
        answer = await asyncio.to_thread(hub_bridge.call_bridge, 'synthetic_status', {'sample': 42}, 5)
        self.assertEqual(answer['value'], {'sample': 42})
        from unittest.mock import patch
        with patch.object(registry.get('synthetic'), 'bridge_command', return_value='other'):
            answer = await asyncio.to_thread(hub_bridge.call_bridge, 'query', {**QUERY, 'platform': 'synthetic'}, 5)
        self.assertEqual(answer['code'], 'invalid_query')
        store.set('paused', {'code': 'rate_limited'})
        store.set('daily', {'date': '2099-01-01', 'count': 50})
        response = await self.client.post('/api/v1/queries', headers={**self.bearer, 'Idempotency-Key': 'fake'},
                                          json={**QUERY, 'platform': 'synthetic'})
        self.assertEqual(response.status, 202)
        job = (await response.json())['job']
        self.assertTrue(await self.hub.run_one())
        self.assertEqual(store.job(job['id'])['status'], 'succeeded')
        self.assertEqual(self.commands[-1][0], 'query')
        self.assertEqual(self.commands[-1][1]['platform'], 'synthetic')
        args = {'platform': 'synthetic', 'keyword': QUERY['keywords'][0]}
        response = await self.client.get('/api/v1/series', params=args, headers=self.bearer)
        self.assertEqual((await response.json())['points'][0]['value'], 42)
        response = await self.client.get('/api/v1/export.csv', params=args, headers=self.bearer)
        self.assertEqual(list(csv.reader(io.StringIO((await response.text()).lstrip('\ufeff'))))[1][1], '42')
        response = await self.client.get('/api/v1/status', headers=self.bearer)
        state = await response.json()
        self.assertEqual(state['platform_status']['synthetic']['attempts'], 1)
        self.assertEqual(state['counts']['points'], 2)
        self.assertEqual(state['counts']['keywords'], 1)
        store.set('fake_paused', {'code': 'test'})
        response = await self.client.post('/api/v1/collection/resume', headers=self.admin, json={'platform': 'synthetic'})
        self.assertEqual(response.status, 200)
        self.assertIsNone(store.get('fake_paused'))
        self.assertEqual(store.get('paused')['code'], 'rate_limited')

    async def test_interruption_and_dataset_repoint_hooks(self):
        from hub_store import encode
        store = self.hub.store
        store.db.execute('INSERT INTO jobs(id,query,status,created) VALUES(?,?,?,?)',
                         ('running', encode({**QUERY, 'platform': 'synthetic'}), 'running', 1))
        store.interrupt_pending()
        self.assertTrue(store.get('synthetic_interrupted'))
        result = encode({'metadata': {}, 'rows': []})
        store.db.execute('INSERT INTO datasets(id,result,created) VALUES(?,?,?)', ('a', result, 1))
        store.db.execute('INSERT INTO datasets(id,result,created) VALUES(?,?,?)', ('b', result, 2))
        store.deduplicate()
        self.assertEqual(store.get('synthetic_repoint'), ['a', 'b'])

    async def test_fake_cooldown_pause_and_budget_do_not_block_douyin(self):
        from unittest.mock import AsyncMock, patch
        from hub_store import encode
        store = self.hub.store
        for index, platform in enumerate(('synthetic', 'douyin')):
            store.db.execute('INSERT INTO jobs(id,query,status,created) VALUES(?,?,?,?)',
                             (platform, encode({**QUERY, 'platform': platform}), 'queued', index))
        douyin = registry.get('douyin')
        with patch.object(douyin, 'run_one', new=AsyncMock(return_value=True)) as run:
            for key, value in [('fake_paused', {'code': 'test'}), ('fake_cooldown', True)]:
                store.set(key, value)
                self.assertTrue(await self.hub.run_one())
                self.assertEqual(run.await_args.args[1]['id'], 'douyin')
                store.set(key, None)
            store.set('fake_attempts', 1)
            self.assertTrue(await self.hub.run_one())
            self.assertEqual(store.job('synthetic')['error']['code'], 'daily_limit')
            self.assertTrue(await self.hub.run_one())
            self.assertEqual(run.await_args.args[1]['id'], 'douyin')
        self.assertIsNone(store.get('paused'))
        self.assertIsNone(store.get('daily'))
