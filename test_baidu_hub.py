"""Baidu routing/budget/storage/API offline integration, using temporary state."""
import asyncio
import contextlib
import copy
import datetime as dt
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch, Mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import baidu_adapter as b
import hub_baidu
from hub_store import HubError, Store, query_hash, public_result
from hub_web import Hub, PRINCIPAL_KEY, mount
from test_baidu_adapter import query, fixture, PTBK
from test_hub import fixture as dy_fixture, QUERY as DY_QUERY

SECRET = 'SYNTHETIC_BAIDU_SECRET'


def result():
    return b.parse_index(fixture(), PTBK, query())


class HubTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calls = []
        self.ready = True
        self.response = {'ok': True, **result()}
        def bridge(command, payload=None, timeout=None):
            self.calls.append(command)
            if command == 'baidu_status':
                return {'ok': True, 'state': 'browser_ready' if self.ready else 'login_required', 'live_enabled': True}
            if command == 'status':
                return {'ok': True, 'state': 'browser_ready'}
            if command == 'query':
                return {'ok': True, **dy_fixture()}
            if isinstance(self.response, Exception):
                raise self.response
            return self.response
        self.hub = Hub(self.temp.name, bridge)
        self.s = self.hub.store
        self.addCleanup(self.s.close)

    def submit(self, q=None):
        return self.s.submit(q or query(), 'synthetic', str(time.time_ns()))[0]

    async def test_success_dimensions_cache_and_douyin_isolation(self):
        self.s.set('paused', {'code': 'verification_required'})
        self.s.set('daily', {'date': 'synthetic-dy-day', 'count': 50})
        job = self.submit()
        await self.hub.run_one()
        self.assertEqual(self.s.job(job['id'])['status'], 'succeeded')
        self.assertEqual(self.calls, ['baidu_status', 'baidu_query'])
        self.assertEqual(self.s.get('daily')['count'], 50)
        self.assertEqual(self.s.get('paused')['code'], 'verification_required')
        self.assertEqual(hub_baidu.daily(self.s)['http_reserved'], 2)
        self.assertEqual(hub_baidu.daily(self.s)['attempts'], 1)
        saved = self.s.cached(query())
        for key, value in b.DIMENSIONS.items():
            self.assertEqual(saved['metadata'][key], value)
        self.assertIsNone(self.s.cached({**query(), 'platform': 'douyin'}))
        self.assertNotEqual(query_hash(query()), query_hash({**query(), 'device': 'pc'}))
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM points').fetchone()[0], 0)
        self.assertEqual(self.s.series('baidu', query()['keywords'][0])['points'][0]['value'], 12)
        self.assertEqual(self.submit()['cache_hit'], True)
        self.assertIsNone(self.s.latest_available_date())  # Douyin date source must stay isolated.

    async def test_login_preflight_no_budget_and_pause_only_baidu(self):
        self.ready = False
        baidu = self.submit()
        await self.hub.run_one()
        self.assertEqual(self.s.job(baidu['id'])['error']['code'], 'login_required')
        self.assertEqual(hub_baidu.daily(self.s)['attempts'], 0)
        self.assertNotIn('baidu_query', self.calls)
        self.assertIsNone(self.s.get('paused'))
        dy = self.submit(DY_QUERY)
        await self.hub.run_one()
        self.assertEqual(self.s.job(dy['id'])['status'], 'succeeded')
        self.assertEqual(self.s.get('daily')['count'], 1)

    async def test_idle_dispatch_and_resume(self):
        original = self.hub.bridge
        def idle_bridge(command, *args):
            if command == 'baidu_status':
                self.calls.append(command)
                return {'ok': True, 'state': 'browser_idle', 'live_enabled': True}
            return original(command, *args)
        self.hub.bridge = idle_bridge
        self.s.set('baidu.paused', {'code': 'login_required'})
        await hub_baidu.resume(self.hub)
        self.assertIsNone(self.s.get('baidu.paused'))
        self.calls.clear()
        job = self.submit()
        await self.hub.run_one()
        self.assertEqual(self.calls, ['baidu_status', 'baidu_query'])
        self.assertEqual(self.s.job(job['id'])['status'], 'succeeded')
        self.assertIsNone(self.s.get('baidu.paused'))

    async def test_other_nonready_states_remain_blocked(self):
        for state in ('login_required', 'login_in_progress', 'signature_unavailable'):
            with self.subTest(state=state):
                self.hub.bridge = Mock(return_value={'ok': True, 'state': state, 'live_enabled': True})
                self.s.set('baidu.paused', {'code': state})
                with self.assertRaises(HubError):
                    await hub_baidu.resume(self.hub)
                self.s.set('baidu.paused', None)
                job = self.submit()
                await self.hub.run_one()
                self.assertEqual(self.s.job(job['id'])['error']['code'], state)
                self.hub.bridge.assert_called_with('baidu_status', None, 2)
                self.assertEqual(hub_baidu.daily(self.s)['attempts'], 0)

    async def test_limits_interval_and_day_rollover(self):
        today = dt.datetime.now(b.TZ).date().isoformat()
        for attempts, http in [(120, 0), (0, 399), (0, 400)]:
            with self.subTest(attempts=attempts, http=http):
                self.s.set('baidu.paused', None)
                self.s.set('baidu.daily', dict(date=today, attempts=attempts, http_reserved=http))
                job = self.submit()
                await self.hub.run_one()
                self.assertEqual(self.s.job(job['id'])['error']['code'], 'baidu_daily_limit')
                self.assertEqual(self.calls, [])
        self.s.set('baidu.paused', None)
        self.s.set('baidu.daily', dict(date='2000-01-01', attempts=20, http_reserved=60))
        self.s.set('baidu.next_attempt_at', time.time()+20)
        job = self.submit()
        self.assertFalse(await self.hub.run_one())
        self.assertEqual(self.calls, [])
        # A cooling Baidu job cannot block a queued Douyin job.
        dy = self.submit(DY_QUERY)
        await self.hub.run_one()
        self.assertEqual(self.s.job(dy['id'])['status'], 'succeeded')
        self.s.set('baidu.next_attempt_at', 0)
        await self.hub.run_one()
        self.assertEqual(self.s.job(job['id'])['status'], 'succeeded')
        self.assertEqual(hub_baidu.daily(self.s)['attempts'], 1)

    async def test_failures_pause_no_retry_and_no_secret(self):
        for failure in [TimeoutError(SECRET), {'ok': False, 'code': 'busy', 'message': SECRET},
                        {'ok': False, 'code': 'cipher_text_required', 'raw': SECRET},
                        {'ok': True, 'metadata': {'cookie': SECRET}}]:
            self.s.set('baidu.paused', None)
            self.s.set('baidu.next_attempt_at', 0)
            self.response = failure
            job = self.submit()
            await self.hub.run_one()
            count = len(self.calls)
            self.assertEqual(self.s.job(job['id'])['status'], 'failed')
            self.assertIsNotNone(self.s.get('baidu.paused'))
            self.assertFalse(await self.hub.run_one())
            self.assertEqual(len(self.calls), count)
            self.assertNotIn(SECRET, '\n'.join(self.s.db.iterdump()))
        self.assertEqual(hub_baidu.daily(self.s)['attempts'], 4)
        self.assertEqual(hub_baidu.daily(self.s)['http_reserved'], 8)

    async def test_single_concurrency(self):
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        original = self.hub.bridge
        def slow(command, *args):
            if command == 'baidu_query':
                entered.set()
                gate.wait(5)
            return original(command, *args)
        self.hub.bridge = slow
        first = self.submit()
        second = self.submit({**query(), 'keywords': ['第二个合成词']})
        work = asyncio.create_task(self.hub.run_one())
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        other = asyncio.create_task(self.hub.run_one())
        await asyncio.sleep(.01)
        gate.set()
        await asyncio.gather(work, other)
        self.assertEqual(self.calls.count('baidu_query'), 1)
        self.assertEqual(self.s.job(first['id'])['status'], 'succeeded')
        self.assertEqual(self.s.job(second['id'])['status'], 'queued')

    async def test_partial_absence_and_wrong_task(self):
        for state in ('unindexed', 'low_volume'):
            self.s.set('baidu.next_attempt_at', 0)
            self.response = {'ok': True, **b.page_absence(query(), state)}
            job = self.submit()
            await self.hub.run_one()
            self.assertEqual(self.s.job(job['id'])['status'], 'no_data')
            self.assertIsNone(self.s.cached(query()))
            self.assertEqual({p['value_status'] for p in self.s.series('baidu', query()['keywords'][0])['points']}, {state})
        self.s.set('baidu.next_attempt_at', 0)
        wrong = copy.deepcopy(result())
        wrong['metadata']['keywords'] = ['other']
        for row in wrong['rows']: row['keyword'] = 'other'
        self.response = {'ok': True, **wrong}
        job = self.submit()
        await self.hub.run_one()
        self.assertEqual(self.s.job(job['id'])['status'], 'failed')
        self.assertEqual(self.s.series('baidu', 'other')['points'], [])

    async def test_restart_retains_budget_and_pauses_interrupted(self):
        job = self.submit()
        self.s.update_job(job['id'], 'running')
        self.s.set('baidu.daily', hub_baidu.daily(self.s) | {'attempts': 3, 'http_reserved': 6})
        self.s.interrupt_pending()
        self.assertEqual(self.s.job(job['id'])['status'], 'failed')
        self.assertEqual(self.s.get('baidu.paused')['code'], 'interrupted')
        self.assertEqual(hub_baidu.daily(self.s)['http_reserved'], 6)
        self.assertIsNone(self.s.get('paused'))
        with self.assertRaises(HubError): self.submit()
        self.assertFalse(await self.hub.run_one())

    async def test_private_socket_routes_baidu_without_douyin_calls(self):
        import hub_bridge
        api = Mock()
        socket_path = Path(self.temp.name) / 'synthetic.sock'
        context = Mock(pages=[Mock(url='https://index.baidu.com/')])
        context.cookies.return_value = []
        worker = Mock(started=False, busy=False, last_state='browser_idle', login_checked=False)
        worker.call.side_effect = lambda action, **kw: action(context)
        with patch.object(hub_bridge, '_socket_path', return_value=socket_path), \
                patch('baidu_browser.browser', return_value=worker):
            server = hub_bridge.start_bridge(api)
            try:
                state = await asyncio.to_thread(hub_bridge.call_bridge, 'baidu_status')
                self.assertEqual(state['state'], 'browser_idle')
                answer = await asyncio.to_thread(hub_bridge.call_bridge, 'baidu_query', {**query(), '_budget_day': dt.datetime.now(b.TZ).date().isoformat()})
                self.assertEqual(answer['code'], 'login_required')
                rejected = await asyncio.to_thread(hub_bridge.call_bridge, 'query', query())
                self.assertEqual(rejected['code'], 'invalid_query')
                self.assertEqual(socket_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(api.mock_calls, [])
            finally:
                await asyncio.to_thread(server.shutdown)
                server.server_close()

    async def test_api_route_schema_export_and_resume(self):
        @web.middleware
        async def auth(request, handler):
            request[PRINCIPAL_KEY] = {'id': 'synthetic', 'admin': True, 'scopes': ['read', 'query']}
            try:
                return await handler(request)
            except HubError as exc:
                return web.json_response({'error': {'code': exc.code}}, status=exc.status)
        app = web.Application(middlewares=[auth])
        mount(app, self.hub)
        app.on_startup.clear()
        app.on_cleanup.clear()
        async with TestClient(TestServer(app)) as client:
            headers = {'X-Hub-Request': '1', 'Idempotency-Key': 'synthetic-job'}
            login = await client.post('/api/v1/login/baidu/open', headers=headers, json={})
            self.assertEqual(login.status, 200)
            self.assertIn('baidu_login_open', self.calls)
            self.calls.clear()
            response = await client.post('/api/v1/queries', headers=headers, json=query())
            self.assertEqual(response.status, 202)
            await self.hub.run_one()
            args = urlencode(dict(platform='baidu', keyword=query()['keywords'][0]))
            export = await (await client.get('/api/v1/export.csv?'+args)).text()
            self.assertIn('device,granularity,source,value_status', export)
            self.assertIn('baidu,search_index,全国', export)
            self.assertIn('all,day,index.baidu.com,available', export)
            with self.s.db:
                self.s.db.execute("UPDATE baidu_points SET value='42' WHERE device='pc'")
                self.s.db.execute("UPDATE baidu_points SET value='84' WHERE device='wise'")
            for device in ('all', 'pc', 'wise'):
                result = await (await client.get('/api/v1/series?'+args+'&device='+device)).json()
                self.assertEqual(result['device'], device)
                self.assertEqual(result['metric_name'], {'all': '整体搜索指数', 'pc': 'PC 搜索指数', 'wise': '移动搜索指数'}[device])
                self.assertEqual(result['points'][0]['value'], {'all': 12, 'pc': 42, 'wise': 84}[device])
                self.assertEqual(len(result['points']), 7)
                csv_text = await (await client.get('/api/v1/export.csv?'+args+'&device='+device)).text()
                self.assertIn(device+',day,index.baidu.com,available', csv_text)
            self.assertEqual((await client.get('/api/v1/series?'+args+'&device=phone')).status, 400)
            self.assertEqual((await client.get('/api/v1/series?platform=douyin&keyword=x&device=pc')).status, 200)
            schema = await (await client.get('/api/v1/openapi.json')).json()
            self.assertIn('/api/v1/login/baidu/open', schema['paths'])
            platforms = await (await client.get('/api/v1/platforms')).json()
            self.assertTrue(platforms['platforms'][1]['live_enabled'])
            state = await (await client.get('/api/v1/status')).json()
            self.assertEqual(state['limits']['online_attempts_per_day'], 50)
            self.assertEqual((state['counts']['keywords'], state['counts']['points']), (1, 21))
            self.assertEqual(state['platform_status']['baidu']['limits']['http_daily'], 400)
            for kind in ('related', 'audience'):
                response = await client.post('/api/v1/insights/queries', headers=headers, json=query() | {'kind': kind})
                self.assertEqual(response.status, 422)
            self.ready = False
            self.s.set('baidu.paused', {'code': 'login_required'})
            response = await client.post('/api/v1/collection/resume', headers=headers, json={'platform': 'baidu'})
            self.assertEqual(response.status, 409)
            self.ready = True
            response = await client.post('/api/v1/collection/resume', headers=headers, json={'platform': 'baidu'})
            self.assertEqual(response.status, 200)
            self.assertEqual(hub_baidu.daily(self.s)['attempts'], 1)
            self.assertIsNone(self.s.get('baidu.paused'))


if __name__ == '__main__':
    unittest.main()


class LimitTests(unittest.IsolatedAsyncioTestCase):
    def test_config_bounds_and_fallback(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'limits.toml'
            path.write_text('[baidu]\ninterval_seconds=4\nlogical_daily=1001\nhttp_daily=1000\njitter_seconds=true\n')
            with patch.dict(os.environ, {'INDEX_HUB_PLATFORM_LIMITS': str(path)}):
                values = hub_baidu.limits()
            self.assertEqual(values['interval_seconds'], 20)
            self.assertEqual(values['logical_daily'], 120)
            self.assertEqual(values['http_daily'], 1000)
            self.assertEqual(values['jitter_seconds'], 10)

    async def test_rate_limit_cooldown_requires_manual_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            hub = Hub(directory, lambda *args: {'ok': True, 'state': 'browser_ready', 'live_enabled': True})
            try:
                hub.store.set('baidu.paused', {'code': 'rate_limited', 'since': time.time()})
                with self.assertRaises(HubError):
                    await hub_baidu.resume(hub)
                hub.store.set('baidu.paused', {'code': 'rate_limited', 'since': time.time()-3601})
                await hub_baidu.resume(hub)
                self.assertIsNone(hub.store.get('baidu.paused'))
            finally:
                hub.store.close()


class SyntheticWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_words_two_chunks_through_worker_and_storage(self):
        q = query()
        q['start_date'] = (dt.date.fromisoformat(q['end_date'])-dt.timedelta(days=365)).isoformat()
        q['keywords'] = ['甲', '乙', '丙', '丁', '戊']
        calls, waits = [], []
        class Transport:
            def get(self, url):
                from urllib.parse import parse_qs, urlsplit
                calls.append(url)
                if '/ptbk?' in url:
                    return 200, 'application/json', json.dumps({'status': 0, 'data': PTBK})
                p = parse_qs(urlsplit(url).query)
                start, end = p['startDate'][0], p['endDate'][0]
                days = (dt.date.fromisoformat(end)-dt.date.fromisoformat(start)).days+1
                text = ','.join(['2']*days).translate(str.maketrans(PTBK[11:], PTBK[:11]))
                series = dict(startDate=start, endDate=end, type='day', data=text)
                data = {'uniqid': 'synthetic', 'userIndexes': [dict(word=w, all=series, pc=series, wise=series)
                         for w in json.loads(p['word'][0])]}
                return 200, 'application/json', json.dumps({'status': 0, 'data': data})
        def bridge(command, payload=None, timeout=None):
            if command == 'baidu_status':
                return {'ok': True, 'state': 'browser_ready', 'live_enabled': True}
            return {'ok': True, **b.collect({k:v for k,v in payload.items() if k != '_budget_day'}, Transport(), chunk_interval=hub_baidu.limits()['chunk_interval_seconds'], sleep=waits.append)}
        with tempfile.TemporaryDirectory() as directory:
            hub = Hub(directory, bridge)
            try:
                job, _ = hub.store.submit(q, 'owner', 'synthetic')
                self.assertTrue(await hub.run_one())
                self.assertEqual(hub.store.job(job['id'])['status'], 'succeeded')
                self.assertEqual((len(calls), waits), (4, [8]))
                self.assertEqual(hub_baidu.daily(hub.store)['http_reserved'], 4)
                self.assertEqual(hub.store.db.execute('SELECT count(*) FROM baidu_points').fetchone()[0], 5*366*3)
                self.assertEqual(hub.store.db.execute('SELECT count(*) FROM points').fetchone()[0], 0)
                self.assertTrue(hub.store.submit(q, 'owner', 'cached')[0]['cache_hit'])
                self.assertEqual(hub_baidu.daily(hub.store)['attempts'], 1)
            finally:
                hub.store.close()

    async def test_error_codes_pause_only_baidu_without_retry(self):
        for code in ('login_required', 'rate_limited', 'bad_request', 'abnormal_access',
                     'verification_required', 'signature_unavailable', 'unknown'):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                calls = []
                def bridge(command, payload=None, timeout=None):
                    calls.append(command)
                    if command == 'baidu_status':
                        return {'ok': True, 'state': 'browser_ready', 'live_enabled': True}
                    return {'ok': False, 'code': code}
                hub = Hub(directory, bridge)
                try:
                    job, _ = hub.store.submit(query(), 'owner', 'error')
                    await hub.run_one()
                    paused = hub.store.get('baidu.paused')
                    self.assertEqual(paused['code'], code if code != 'unknown' else 'request_failed')
                    self.assertIsNone(hub.store.get('paused'))
                    self.assertEqual(hub.store.job(job['id'])['status'], 'failed')
                    self.assertFalse(await hub.run_one())
                    self.assertEqual(calls, ['baidu_status', 'baidu_query'])
                finally:
                    hub.store.close()
