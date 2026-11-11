"""Synthetic offline API/security/task tests. No official network or real secrets."""
import asyncio
import base64
import contextlib
import datetime as dt
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from desktop_gateway import HUB_KEY, create_app
from hub_store import HubError, Store, TZ, public_result, validate_query

END = '2025-01-07'
QUERY = {'platform': 'douyin', 'keywords': ['酵素'], 'start_date': '2025-01-01', 'end_date': END}
SECRET = 'SYNTHETIC_COOKIE_MUST_NOT_ESCAPE'


def fixture(*, complete=True):
    return {'raw': {'cookie': SECRET}, 'rows': [
        {'keyword': '酵素', 'date': f'2025-01-{day:02}', 'search_index': day * 100 if complete or day != 3 else None,
         'composite_index': day * 7, 'extra': SECRET} for day in range(1, 8)],
        'metadata': {'keywords': ['酵素'], 'start_date': '2025-01-01', 'end_date': END,
                     'fetched_at': dt.datetime.now(TZ).isoformat(), 'metric': 'search_index',
                     'platform': 'aweme', 'region': '全国', 'complete': complete, 'extra': SECRET}}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'hub.sqlite3'
        self.s = Store(self.path)

    def tearDown(self):
        self.s.close()
        self.temp.cleanup()

    def test_same_collection_import_is_one_dataset(self):
        raw = fixture()
        first = public_result(raw)
        original = self.s.ingest(first)
        verified = public_result(raw, verification='user_reported_match')
        self.assertEqual(self.s.ingest(verified, 'legacy_test'), original)
        self.assertEqual(len(self.s.history()), 1)
        self.assertEqual(self.s.history()[0]['metadata']['verification'], 'user_reported_match')
        newer = public_result(fixture())
        self.s.ingest(newer)
        self.assertEqual(len(self.s.history()), 2)

    def test_latest_available_date_uses_all_valid_points_not_latest_dataset_range(self):
        self.assertIsNone(self.s.latest_available_date())
        result = public_result(fixture())
        result['rows'][-1]['search_index'] = 0  # Zero is an observed value.
        self.s.ingest(result)
        for index in range(31):
            older = public_result(fixture())
            older['metadata']['start_date'] = '2024-12-01'
            older['metadata']['end_date'] = '2024-12-07'
            older['metadata']['fetched_at'] = (dt.datetime.now(TZ) + dt.timedelta(seconds=index+1)).isoformat()
            for row in older['rows']:
                row['date'] = row['date'].replace('2025-01', '2024-12')
            self.s.ingest(older)
        self.assertEqual(len(self.s.history()), 30)
        self.assertTrue(all(d['metadata']['end_date'] == '2024-12-07' for d in self.s.history()))
        with self.s.db:
            self.s.db.execute('INSERT INTO points VALUES(?,?,?,?,?,?,?,?,?)',
                              ('douyin', 'search_index', '全国', '合成词', '2025-01-08',
                               'null', '123', time.time(), 'synthetic'))
        self.assertEqual(self.s.latest_available_date(), END)

    def test_existing_duplicates_migrate_without_losing_points(self):
        from hub_store import encode
        result = public_result(fixture())
        keep = self.s.ingest(result)
        self.s.db.execute("INSERT INTO datasets SELECT 'duplicate',qhash,query,fetched,result,created+1 FROM datasets WHERE id=?", (keep,))
        self.s.db.execute("UPDATE points SET dataset='duplicate'")
        self.s.db.commit()
        self.s.deduplicate()
        self.assertEqual(len(self.s.history()), 1)
        self.assertEqual(len(self.s.series('douyin','酵素')['points']),7)
        self.assertEqual(self.s.db.execute('SELECT DISTINCT dataset FROM points').fetchone()[0],keep)

    def test_query_scope_canonical_dates_and_platform(self):
        self.assertEqual(validate_query({'keywords': ['酵素'], 'end_date': END}), QUERY)
        for body in ({'keywords': ['x', 'x'], 'end_date': END},
                     {'keywords': ['x'], 'end_date': END, 'start_date': '2025-01-08'},
                     {'platform': 'google_trends', 'keywords': ['x'], 'end_date': END},
                     {'keywords': ['x\n'], 'end_date': END},
                     {'keywords': ['x'], 'end_date': END, 'cookies': SECRET},
                     {'keywords': ['x'], 'end_date': '9999-01-01'}):
            with self.subTest(body=body), self.assertRaises(HubError):
                validate_query(body)

    def test_projection_missing_null_and_no_credential_fields(self):
        result = public_result(fixture(complete=False))
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertIsNone(result['rows'][2]['search_index'])
        self.assertFalse(result['metadata']['complete'])
        self.assertEqual(result['metadata']['missing_search_dates']['酵素'], ['2025-01-03'])
        bad = fixture()
        bad['rows'].append(bad['rows'][0])
        with self.assertRaises(HubError):
            public_result(bad)

    def test_key_hash_only_expiry_revocation(self):
        key = self.s.issue_key('WorkBuddy', ['read', 'query'])
        self.assertNotIn(key['token'], str(self.s.list_keys()))
        self.assertNotIn(key['token'], str([tuple(r) for r in self.s.db.execute('SELECT * FROM keys')]))
        self.assertIsNotNone(self.s.key_principal(key['token']))
        self.s.revoke_key(key['id'])
        self.assertIsNone(self.s.key_principal(key['token']))
        expired = self.s.issue_key('Expired', ['read'])
        self.s.db.execute('UPDATE keys SET expires=0 WHERE id=?', (expired['id'],))
        self.assertIsNone(self.s.key_principal(expired['token']))

    def test_idempotency_and_cache_prevent_new_online_work(self):
        self.s.ingest(public_result(fixture()))
        first, created = self.s.submit(QUERY, 'fixture', 'same-request')
        second, repeated = self.s.submit(QUERY, 'fixture', 'same-request')
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(first['id'], second['id'])
        self.assertTrue(first['cache_hit'])
        self.assertEqual(first['status'], 'succeeded')
        with self.assertRaises(HubError):
            self.s.submit(QUERY | {'keywords': ['other']}, 'fixture', 'same-request')

    def test_partial_not_cached_and_missing_range_not_zero(self):
        self.s.ingest(public_result(fixture(complete=False)))
        self.assertIsNone(self.s.cached(QUERY))
        series = self.s.series('douyin', '酵素', '2025-01-01', '2025-01-08')
        self.assertIsNone(series['points'][2]['value'])
        self.assertIsNone(series['points'][-1]['value'])
        self.assertFalse(series['live_request'])

    def test_queue_pause_and_interrupted_jobs_not_replayed(self):
        job, _ = self.s.submit(QUERY, 'fixture', 'one')
        running, _ = self.s.submit(QUERY, 'fixture', 'running')
        self.s.update_job(running['id'], 'running')
        self.assertEqual(self.s.interrupt_pending(), {'requeued': 1, 'interrupted': 1})
        self.assertEqual(self.s.job(job['id']), job)
        self.assertIsNone(self.s.job(job['id'])['error'])
        interrupted = self.s.job(running['id'])
        self.assertEqual(interrupted['status'], 'failed')
        self.assertEqual(interrupted['error'], {
            'code': 'service_restarted',
            'message': '服务重启时任务正在执行，可能已向平台发出请求；未自动重试，请确认后重新提交。',
            'recoverable': True,
        })
        self.assertEqual(self.s.interrupt_pending(), {'requeued': 1, 'interrupted': 0})
        self.assertEqual(self.s.job(running['id']), interrupted)
        self.s.set('paused', {'code': 'rate_limited'})
        with self.assertRaises(HubError):
            self.s.submit(QUERY, 'fixture', 'two')

    def test_newer_observation_wins_and_safe_import(self):
        result = public_result(fixture())
        self.s.ingest(result, 'newer')
        result['metadata']['fetched_at'] = '2024-01-01T00:00:00+08:00'
        result['rows'][0]['search_index'] = 999
        self.s.ingest(result, 'older')
        self.assertEqual(self.s.series('douyin', '酵素')['points'][0]['value'], 100)
        data = Path(self.temp.name) / 'results' / 'fixture'
        data.mkdir(parents=True)
        (data / 'result.json').write_text(json.dumps(fixture()))
        (data / 'user-confirmation.json').write_text(json.dumps({'evidence_level': 'user_attestation', 'user_reported_all_daily_values_match': True}))
        self.assertEqual(self.s.import_results(data.parent), 1)
        self.assertEqual(self.s.import_results(data.parent), 0)
        self.assertEqual(self.s.cached(QUERY)['metadata']['verification'], 'user_reported_match')


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / '.cloud-state').mkdir()
        (self.root / '.cloud-state/access.json').write_text(json.dumps({'username': 'fixture', 'password': 'admin-fixture-only'}))
        (self.root / 'hub.html').write_text('<h1>Fixture hub</h1>')
        self.calls = []
        self.response = {'ok': True, **fixture()}

        def bridge(command, payload, timeout):
            if command == 'status':
                return {'ok': True, 'state': 'browser_ready'}
            self.calls.append(payload)
            return self.response

        from hub_auth_throttle import AuthThrottle
        self.auth_now = 1000.0
        self.throttle = AuthThrottle(clock=lambda: self.auth_now)
        app = create_app(self.root, bridge=bridge, desktop_dir=self.root, auth_throttle=self.throttle)
        self.hub = app[HUB_KEY]
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.hub.worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.hub.worker_task
        self.admin = {'Authorization': 'Basic ' + base64.b64encode(b'fixture:admin-fixture-only').decode(), 'X-Hub-Request': '1'}
        key = self.hub.store.issue_key('Client', ['read', 'query'])
        self.bearer = {'Authorization': 'Bearer ' + key['token']}

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_history_get_is_read_only_even_with_new_results_on_disk(self):
        folder = self.root / 'results' / 'synthetic'
        folder.mkdir(parents=True)
        (folder / 'result.json').write_text(json.dumps(fixture()))
        store = self.hub.store
        before = list(store.db.iterdump())
        changes = store.db.total_changes
        with patch.object(store, 'import_results', side_effect=AssertionError('GET must not import')):
            store.db.execute('PRAGMA query_only=ON')
            try:
                for headers in (self.admin, self.bearer):
                    response = await self.client.get('/api/v1/history', headers=headers)
                    self.assertEqual(response.status, 200)
                    self.assertEqual((await response.json())['datasets'], [])
            finally:
                store.db.execute('PRAGMA query_only=OFF')
        self.assertEqual(store.db.total_changes, changes)
        self.assertEqual(list(store.db.iterdump()), before)
        self.assertEqual(self.calls, [])

    async def test_startup_still_imports_existing_results(self):
        folder = self.root / 'results' / 'synthetic'
        folder.mkdir(parents=True)
        (folder / 'result.json').write_text(json.dumps(fixture()))
        await self.hub.start({})
        self.hub.worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.hub.worker_task
        response = await self.client.get('/api/v1/history', headers=self.admin)
        self.assertEqual(response.status, 200)
        datasets = (await response.json())['datasets']
        self.assertEqual(len(datasets), 1)
        self.assertEqual(datasets[0]['metadata']['end_date'], END)
        body = await response.json()
        from hub_web import expected_douyin_trend_end
        self.assertEqual(body['latest_available_date'], expected_douyin_trend_end().isoformat())
        self.assertEqual(body['latest_available_date_source'], 'official_cadence')
        self.assertEqual(self.hub.store.latest_available_date(), END)
        self.assertEqual(self.calls, [])

    async def test_request_source_admin_only_and_idempotent(self):
        headers = {**self.bearer, 'Idempotency-Key': 'source-test', 'User-Agent': 'WorkBuddy-Test/1', 'X-Forwarded-For': '203.0.113.99'}
        response = await self.client.post('/api/v1/queries', headers=headers, json=QUERY)
        job = (await response.json())['job']
        self.assertNotIn('request_source', job)
        first = (await (await self.client.get('/api/v1/jobs/'+job['id'], headers=self.admin)).json())['job']['request_source']
        self.assertEqual(first['key_name'], 'Client')
        self.assertEqual(first['user_agent'], 'WorkBuddy-Test/1')
        self.assertNotEqual(first['peer_ip'], '203.0.113.99')
        self.assertNotIn(self.bearer['Authorization'], json.dumps(first))
        await self.client.post('/api/v1/queries', headers={**headers,'User-Agent':'Changed'}, json=QUERY)
        again = (await (await self.client.get('/api/v1/jobs/'+job['id'], headers=self.admin)).json())['job']['request_source']
        self.assertEqual(first, again)
        listed = (await (await self.client.get('/api/v1/jobs', headers=self.bearer)).json())['jobs']
        self.assertTrue(all('request_source' not in j for j in listed))

    async def test_auth_failure_logs_no_credentials(self):
        authorization = 'Basic ' + base64.b64encode(b'fixture:SYNTHETIC_PASSWORD').decode()
        with self.assertLogs('index_hub.auth', level='WARNING') as captured:
            for _ in range(12):
                response = await self.client.get('/api/v1/platforms?ignored=SYNTHETIC_PASSWORD',
                                                 headers={'Authorization': authorization})
                self.assertEqual(response.status, 401)
            response = await self.client.get('/', headers={'Authorization': authorization})
            self.assertEqual(response.status, 429)
        self.assertEqual(len(captured.output), 13)
        output = '\n'.join(captured.output)
        for secret in ('SYNTHETIC_PASSWORD', authorization, 'Authorization', 'Basic'):
            self.assertNotIn(secret, output)
        self.assertIn('method=GET path=/api/v1/platforms kind=api', output)
        self.assertIn('duration=900 strikes=1', output)

    async def test_auth_ban_remaining_and_missing_header(self):
        for _ in range(25):
            self.assertEqual((await self.client.get('/')).status, 401)
        self.assertFalse(self.throttle.failures)
        for _ in range(12):
            self.assertEqual((await self.client.get('/', headers={'Authorization': 'wrong'})).status, 401)
        self.auth_now += 0.25
        response = await self.client.get('/api/v1/platforms', headers=self.admin)
        self.assertEqual(response.status, 429)
        self.assertEqual(response.headers['Retry-After'], '900')
        self.assertEqual((await response.json())['error'], {
            'code': 'authentication_throttled', 'message': '认证失败次数过多，请稍后再试。'})
        self.auth_now += 100
        response = await self.client.get('/', headers=self.admin)
        self.assertEqual(response.headers['Retry-After'], '800')
        self.assertEqual((await self.client.get('/')).status, 401)
        self.auth_now = 1900
        self.assertEqual((await self.client.get('/api/v1/platforms', headers=self.admin)).status, 200)

    async def test_auth_required_and_keys_not_admin(self):
        self.assertEqual((await self.client.get('/api/v1/series?keyword=酵素')).status, 401)
        self.assertEqual((await self.client.get('/', headers=self.bearer)).status, 401)
        self.assertEqual((await self.client.get('/api/v1/keys', headers=self.bearer)).status, 403)
        self.assertEqual((await self.client.post('/api/v1/collection/resume', headers=self.bearer)).status, 403)
        self.assertEqual((await self.client.get('/api/v1/platforms', headers=self.bearer)).status, 200)

    async def test_admin_csrf_and_key_lifecycle(self):
        no_csrf = {'Authorization': self.admin['Authorization']}
        self.assertEqual((await self.client.post('/api/v1/keys', headers=no_csrf, json={'name': 'x'})).status, 403)
        bad_origin = self.admin | {'Origin': 'https://evil.example'}
        self.assertEqual((await self.client.post('/api/v1/keys', headers=bad_origin, json={'name': 'x'})).status, 403)
        response = await self.client.post('/api/v1/keys', headers=self.admin, json={'name': 'Reader', 'scopes': ['read'], 'days': 1})
        self.assertEqual(response.status, 201)
        key = await response.json()
        header = {'Authorization': 'Bearer ' + key['token'], 'Idempotency-Key': 'read-only'}
        self.assertEqual((await self.client.post('/api/v1/queries', headers=header, json=QUERY)).status, 403)
        listing = await (await self.client.get('/api/v1/keys', headers=self.admin)).text()
        self.assertNotIn(key['token'], listing)
        await self.client.delete('/api/v1/keys/' + key['id'], headers=self.admin)
        self.assertEqual((await self.client.get('/api/v1/status', headers=header)).status, 401)

    async def test_cached_submit_and_csv_no_live_requests(self):
        self.hub.store.ingest(public_result(fixture()))
        headers = self.bearer | {'Idempotency-Key': 'cache-one'}
        response = await self.client.post('/api/v1/queries', headers=headers, json=QUERY)
        self.assertEqual(response.status, 200)
        job = (await response.json())['job']
        self.assertTrue(job['cache_hit'])
        text = await (await self.client.get('/api/v1/jobs/' + job['id'], headers=self.bearer)).text()
        self.assertNotIn(SECRET, text)
        csv_response = await self.client.get('/api/v1/export.csv?keyword=酵素', headers=self.bearer)
        self.assertEqual(csv_response.status, 200)
        self.assertIn('search_index', await csv_response.text())
        self.assertEqual(self.calls, [])

    async def test_worker_executes_queued_jobs_after_restart(self):
        store = self.hub.store
        running, _ = store.submit(QUERY, 'fixture', 'restart-running')
        store.update_job(running['id'], 'running')
        first, _ = store.submit(QUERY, 'fixture', 'restart-first')
        second, _ = store.submit(QUERY, 'fixture', 'restart-second')
        store.close()
        self.hub.store = Store(self.root / '.cloud-state' / 'index-hub.sqlite3')
        await self.hub.start(None)
        self.hub.worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.hub.worker_task
        store = self.hub.store
        self.assertEqual(store.job(first['id']), first)
        self.assertEqual(store.job(second['id']), second)
        self.assertEqual(store.job(running['id'])['status'], 'failed')
        self.assertTrue(await self.hub.run_one())
        self.assertEqual(store.job(first['id'])['status'], 'succeeded')
        self.assertEqual(store.job(second['id'])['status'], 'queued')
        self.assertEqual(self.calls, [QUERY])
        self.assertTrue(await self.hub.run_one())
        self.assertEqual(store.job(second['id'])['status'], 'succeeded')
        self.assertEqual(store.job(running['id'])['status'], 'failed')
        self.assertFalse(await self.hub.run_one())
        self.assertEqual(self.calls, [QUERY])

    async def test_single_attempt_cooldown_and_platform_stop(self):
        job, _ = self.hub.store.submit(QUERY, 'fixture', 'one')
        self.response = {'ok': False, 'code': 'rate_limited', 'message': SECRET}
        await self.hub.run_one()
        self.assertEqual(len(self.calls), 1)
        failed = self.hub.store.job(job['id'])
        self.assertNotIn(SECRET, json.dumps(failed))
        self.assertEqual(failed['error']['code'], 'rate_limited')
        self.assertIsNotNone(self.hub.store.get('paused'))
        self.assertFalse(await self.hub.run_one())
        self.hub.store.set('paused', None)
        self.hub.store.submit(QUERY, 'fixture', 'two')
        self.assertFalse(await self.hub.run_one())
        self.assertEqual(len(self.calls), 1)

    async def test_unattempted_bridge_response_requeues_and_refunds(self):
        day = dt.datetime.now(TZ).date().isoformat()
        previous_result = {'ok': True, 'at': 1}
        for code in ('cooldown', 'busy'):
            with self.subTest(code=code):
                self.hub.store.set('last_attempt_at', 0)
                self.hub.store.set('daily', {'date': day, 'count': 7})
                self.hub.store.set('last_result', previous_result)
                self.response = {'ok': False, 'code': code}
                job, _ = self.hub.store.submit(QUERY, 'fixture', 'requeue-' + code)
                before = time.time()
                self.assertTrue(await self.hub.run_one())
                saved = self.hub.store.job(job['id'])
                self.assertEqual(saved['status'], 'queued')
                self.assertIsNone(saved['error'])
                self.assertEqual(self.hub.store.get('daily'), {'date': day, 'count': 7})
                self.assertIsNone(self.hub.store.get('paused'))
                self.assertEqual(self.hub.store.get('last_result'), previous_result)
                self.assertGreaterEqual(self.hub.store.get('last_attempt_at'), before)
                self.assertFalse(await self.hub.run_one())
                self.hub.store.update_job(job['id'], 'failed')

    async def test_refund_is_same_day_only_and_never_negative(self):
        today = dt.datetime.now(TZ).date()
        day = today.isoformat()
        old_day = (today - dt.timedelta(days=1)).isoformat()
        job, _ = self.hub.store.submit(QUERY, 'fixture', 'refund-boundaries')
        for attempt_day, counter, expected in (
            (day, {'date': day, 'count': 0}, {'date': day, 'count': 0}),
            (old_day, {'date': old_day, 'count': 1}, {'date': old_day, 'count': 1}),
            (old_day, {'date': day, 'count': 3}, {'date': day, 'count': 3}),
            (day, {'date': old_day, 'count': 1}, {'date': old_day, 'count': 1}),
        ):
            self.hub.store.set('daily', counter)
            self.hub._requeue_unattempted(job['id'], attempt_day)
            self.assertEqual(self.hub.store.get('daily'), expected)

    async def test_status_cooldown_remaining_seconds(self):
        for last_attempt, expected in ((1000, 15), (985.1, 1), (985, 0), (900, 0)):
            self.hub.store.set('last_attempt_at', last_attempt)
            with patch('hub_web.time.time', return_value=1000):
                response = await self.client.get('/api/v1/status', headers=self.bearer)
                state = await response.json()
            self.assertEqual(state['next_online_attempt_at'], last_attempt + 15)
            self.assertEqual(state['cooldown_remaining_seconds'], expected)
            self.assertIs(type(state['cooldown_remaining_seconds']), int)

    async def test_daily_limit_before_touching_upstream(self):
        self.hub.store.set('daily', {'date': dt.datetime.now(TZ).date().isoformat(), 'count': 50})
        job, _ = self.hub.store.submit(QUERY, 'fixture', 'daily')
        await self.hub.run_one()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.hub.store.job(job['id'])['error']['code'], 'daily_limit')

    async def test_live_fixture_success_and_partial_never_cached(self):
        job, _ = self.hub.store.submit(QUERY, 'fixture', 'success')
        self.response = {'ok': True, **fixture(complete=False)}
        await self.hub.run_one()
        self.assertEqual(len(self.calls), 1)
        saved = self.hub.store.job(job['id'])
        self.assertEqual(saved['status'], 'partial')
        self.assertIsNone(self.hub.store.cached(QUERY))
        self.assertIsNone(saved['result']['rows'][2]['search_index'])

    async def test_bad_json_unknown_fields_and_idempotency_conflict(self):
        response = await self.client.post('/api/v1/queries', headers=self.bearer | {'Idempotency-Key': 'one'}, json=QUERY)
        self.assertEqual(response.status, 202)
        response = await self.client.post('/api/v1/queries', headers=self.bearer | {'Idempotency-Key': 'one'}, json=QUERY | {'keywords': ['other']})
        self.assertEqual(response.status, 409)
        response = await self.client.post('/api/v1/queries', headers=self.bearer, json=QUERY)
        self.assertEqual(response.status, 400)
        response = await self.client.get('/api/v1/series?keyword=x&platform=google_trends', headers=self.bearer)
        self.assertEqual(response.status, 422)

    async def test_openapi_and_pause_resume_requires_admin(self):
        schema = await (await self.client.get('/api/v1/openapi.json', headers=self.bearer)).json()
        self.assertEqual(schema['openapi'], '3.1.0')
        self.assertIn('/api/v1/queries', schema['paths'])
        self.hub.store.set('paused', {'code': 'authentication_required'})
        response = await self.client.post('/api/v1/collection/resume', headers=self.admin, json={})
        self.assertEqual(response.status, 200)
        self.assertIsNone(self.hub.store.get('paused'))


class DiagnosticTests(unittest.TestCase):
    def test_redacts_cookie_headers_and_short_credentials(self):
        from hub_store import exc_summary, redact
        samples = (
            ('Cookie: sessionid=abc; sid_guard=short|1700; odin_tt=xyz',
             ('abc', 'short', '1700', 'xyz')),
            ('set-cookie: uid_tt=uidv; Path=/; ttwid=ttw1', ('uidv', 'Path=/', 'ttw1')),
            ('params msToken=tok1&a_bogus=bogus1&verifyFp=verify_abc',
             ('tok1', 'bogus1', 'verify_abc')),
            ("{'Cookie': 'sid_tt=s1; s_v_web_id=web_id_1', 'x-csrf-token': 'csrfv'}",
             ('s1', 'web_id_1', 'csrfv')),
            ('{"cookie": "passport_csrf_token=pc1; odin_tt=o2"}', ('pc1', 'o2')),
        )
        for message, values in samples:
            with self.subTest(message=message):
                summary = exc_summary(RuntimeError(message))
                self.assertTrue(summary.startswith('RuntimeError: '))
                for output in (redact(message), summary):
                    self.assertIn('***', output)
                    for value in values:
                        self.assertNotIn(value, output)

    def test_redaction_boundaries_and_plain_diagnostics(self):
        from hub_store import exc_summary, redact
        self.assertEqual(redact('ConnectionResetError: peer closed'),
                         'ConnectionResetError: peer closed')
        self.assertEqual(exc_summary(ConnectionResetError('peer closed')),
                         'ConnectionResetError errno=None: peer closed')
        for key in ('cookie', 'token', 'session', 'sessionid', 'sid_', 'sid', 'uid_tt',
                    'odin_tt', 'ttwid', 'mstoken', 'a_bogus', 'verifyfp', 's_v_web_id',
                    'csrf', 'passport', 'passwd', 'password', 'secret', 'auth', 'key'):
            for template in ('prefix_{}=sensitive', '{}: sensitive', '"{}": "sensitive"'):
                with self.subTest(key=key, template=template):
                    self.assertNotIn('sensitive', redact(template.format(key.upper())))
        self.assertEqual(redact('Cookie: a=short; b=tiny\npeer closed'),
                         'Cookie: ***\npeer closed')
        self.assertEqual(redact('errno=2, status: failed; peer closed'),
                         'errno=2, status: failed; peer closed')
        self.assertEqual(redact('A' * 24), '***')

    def test_summary_redacts_before_truncation(self):
        from hub_store import exc_summary
        for key in ('cookie', 'access_token', 'session', 'sessionid', 'passwd',
                    'password', 'secret', 'authorization', 'api_key'):
            for separator in ('=', ': '):
                for value in ('short', '"two words"', "'two words'", SECRET):
                    summary = exc_summary(RuntimeError(key + separator + value))
                    self.assertIn('***', summary)
                    self.assertNotIn(value, summary)
        self.assertNotIn('A' * 24, exc_summary(ValueError('A' * 24)))
        self.assertIn('errno=2', exc_summary(FileNotFoundError(2, 'missing')))
        self.assertEqual(exc_summary(ValueError('错' * 200)), 'ValueError: ' + '错' * 160)

    def test_handler_logs_sanitized_exception(self):
        import io
        import hub_bridge
        class Server:
            def __init__(self, address, handler):
                self.handler = handler
            def serve_forever(self):
                pass
        with tempfile.TemporaryDirectory() as root:
            with patch.object(hub_bridge, 'SOCKET', Path(root) / 'browser.sock'), \
                    patch.object(hub_bridge.socketserver, 'ThreadingUnixStreamServer', Server), \
                    patch.object(hub_bridge.os, 'chmod'):
                api = unittest.mock.Mock()
                api._login_state.side_effect = RuntimeError('cookie=' + SECRET)
                server = hub_bridge.start_bridge(api)
            handler = object.__new__(server.handler)
            handler.request = unittest.mock.Mock()
            handler.rfile = io.BytesIO(b'{"command":"status"}\n')
            handler.wfile = io.BytesIO()
            with self.assertLogs('index_hub.bridge', 'WARNING') as logs:
                handler.handle()
            handler.request.settimeout.assert_called_once_with(hub_bridge.BRIDGE_TIMEOUT)
            self.assertIn('command=status', logs.output[0])
            self.assertIn('RuntimeError', logs.output[0])
            self.assertNotIn(SECRET, str(logs.output))
            self.assertEqual(json.loads(handler.wfile.getvalue())['code'], 'browser_unavailable')


class WorkerDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def test_bridge_timeout_covers_browser_task(self):
        import client
        import hub_bridge
        from hub_web import SAFE_ERRORS
        self.assertEqual(hub_bridge.BRIDGE_TIMEOUT, 75)
        self.assertEqual(hub_bridge.JS_MARGIN, 20)
        self.assertGreaterEqual(hub_bridge.BRIDGE_TIMEOUT,
                                client.REQUEST_TOTAL_DEADLINE + client.REQUEST_READ_TIMEOUT
                                + hub_bridge.JS_MARGIN)
        # Connection setup must also fit inside the total request deadline.
        self.assertLess(client.REQUEST_CONNECT_TIMEOUT, client.REQUEST_TOTAL_DEADLINE)
        self.assertEqual(hub_bridge.call_bridge.__defaults__[-1], hub_bridge.BRIDGE_TIMEOUT)
        self.assertIn(f'{hub_bridge.BRIDGE_TIMEOUT} 秒', SAFE_ERRORS['bridge_timeout'])


    async def test_bridge_exceptions_are_logged_without_retry(self):
        from hub_web import Hub
        for exc, code, level in (
            (TimeoutError('token=' + SECRET), 'bridge_timeout', 'WARNING'),
            (RuntimeError('cookie=' + SECRET), 'request_failed', 'ERROR'),
            (ConnectionRefusedError(111, 'refused'), 'browser_unavailable', 'WARNING'),
            (FileNotFoundError(2, 'missing'), 'browser_unavailable', 'WARNING'),
        ):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as root:
                calls = []
                def bridge(command, payload, timeout):
                    if command == 'status':
                        return {'ok': True, 'state': 'browser_ready'}
                    from hub_bridge import BRIDGE_TIMEOUT
                    self.assertEqual(timeout, BRIDGE_TIMEOUT)
                    calls.append(command)
                    raise exc
                hub = Hub(root, bridge)
                try:
                    job, _ = hub.store.submit(QUERY, 'fixture', 'diagnostic')
                    async def local_thread_call(func, *args):
                        return func(*args)
                    with patch('hub_web.asyncio.to_thread', local_thread_call), self.assertLogs('index_hub', level) as logs:
                        self.assertTrue(await hub.run_one())
                    saved = hub.store.job(job['id'])
                    if code == 'browser_unavailable':
                        self.assertEqual(saved['status'], 'queued')
                        self.assertIsNone(saved['error'])
                        self.assertEqual(hub.store.get('daily')['count'], 0)
                        self.assertEqual(hub.store.get('paused')['code'], code)
                    else:
                        self.assertEqual(saved['status'], 'failed')
                        self.assertEqual(saved['error']['code'], code)
                        self.assertEqual(hub.store.get('daily')['count'], 1)
                        self.assertIsNone(hub.store.get('paused'))
                        self.assertEqual(hub.store.get('last_result')['code'], code)
                        self.assertFalse(hub.store.get('last_result')['ok'])
                    self.assertNotIn(SECRET, str(logs.output))
                    self.assertIn(type(exc).__name__, str(logs.output))
                    self.assertIn(job['id'], str(logs.output))
                    self.assertFalse(await hub.run_one())
                    self.assertEqual(len(calls), 1)
                finally:
                    hub.store.close()


if __name__ == '__main__':
    unittest.main()


class DesktopTicketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def vnc(reader, writer):
            try:
                writer.write(b'RFB 003.008\n')
                await writer.drain()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
        self.vnc = await asyncio.start_server(vnc, '127.0.0.1', 0)
        self.now = 100.0
        port = self.vnc.sockets[0].getsockname()[1]
        original = create_app
        with patch('test_hub.create_app', side_effect=lambda *a, **kw: original(*a, **kw, vnc_port=port, clock=lambda: self.now)):
            await ApiTests.asyncSetUp(self)

    async def asyncTearDown(self):
        await ApiTests.asyncTearDown(self)
        self.vnc.close()
        await self.vnc.wait_closed()

    async def ticket(self):
        response = await self.client.post('/api/v1/desktop/ticket', headers=self.admin, json={})
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data['expires_in'], 60)
        return data['ticket']

    async def test_admin_csrf_required(self):
        for headers, status in [({}, 401), (self.bearer, 403), ({'Authorization': self.admin['Authorization']}, 403)]:
            response = await self.client.post('/api/v1/desktop/ticket', headers=headers, json={})
            self.assertEqual(response.status, status)

    async def test_websocket_ticket_single_use_and_origin(self):
        ticket = await self.ticket()
        origin = 'https://' + self.client.server.host + ':' + str(self.client.server.port)
        ws = await self.client.ws_connect('/websockify?ticket='+ticket, headers={'Origin': origin}, protocols=['binary'])
        self.assertEqual((await ws.receive()).data, b'RFB 003.008\n')
        await ws.close()
        with self.assertLogs('index_hub', level='WARNING') as logs:
            response = await self.client.get('/websockify?ticket='+ticket)
            self.assertEqual(response.status, 401)
        self.assertNotIn(ticket, '\n'.join(logs.output))
        ticket = await self.ticket()
        response = await self.client.get('/websockify?ticket='+ticket, headers={'Origin': 'https://wrong.invalid'})
        self.assertEqual(response.status, 403)

    async def test_expiry_path_restriction_and_missing(self):
        ticket = await self.ticket()
        response = await self.client.get('/?ticket='+ticket)
        self.assertEqual(response.status, 401)
        self.now += 61
        response = await self.client.get('/websockify?ticket='+ticket)
        self.assertEqual(response.status, 401)
        response = await self.client.get('/websockify')
        self.assertEqual(response.status, 401)

    async def test_ticket_cap(self):
        tickets = [await self.ticket() for _ in range(33)]
        response = await self.client.get('/websockify?ticket='+tickets[0])
        self.assertEqual(response.status, 401)

    async def test_qr_status_browser_field_only_when_inactive(self):
        from unittest.mock import AsyncMock
        for state in ('verification_required', 'starting', 'qr_ready', 'succeeded'):
            with patch.object(self.hub, 'bridge', return_value={'ok': True, 'state': state}), patch.object(self.hub, 'browser_status', new_callable=AsyncMock, return_value='browser_ready') as browser:
                response = await self.client.get('/api/v1/login/qr', headers=self.admin)
                data = await response.json()
                if state in ('starting', 'qr_ready'):
                    self.assertNotIn('browser', data)
                    browser.assert_not_awaited()
                else:
                    self.assertEqual(data['browser'], 'browser_ready')
                for action in ('start', 'cancel'):
                    self.hub._qr_last_start = None
                    response = await self.client.post('/api/v1/login/qr/'+action, headers=self.admin, json={})
                    self.assertNotIn('browser', await response.json())
