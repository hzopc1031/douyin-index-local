import datetime as dt
import json
import unittest
from hub_insights import project, validate_insight
from hub_store import HubError, public_result, validate_query
from test_hub import ApiTests as _ApiTests, fixture, QUERY

class InsightTests(unittest.TestCase):
    def test_client_precheck_error_classification(self):
        import threading
        from types import SimpleNamespace
        from unittest.mock import Mock
        from app import LoginRequired
        from hub_insights import collect
        for kind in ('related', 'audience'):
            for error, code in ((LoginRequired('请先完成登录。'), 'authentication_required'),
                                (ValueError('暂时无法识别官方窗口。'), 'request_failed')):
                with self.subTest(kind=kind, code=code):
                    api = SimpleNamespace(_lock=threading.Lock(), _client=Mock(side_effect=error),
                                          _on_auth_failed=Mock())
                    self.assertEqual(collect(api, {**QUERY, 'kind': kind}),
                                     {'ok': False, 'code': code, 'message': str(error) if code == 'authentication_required' else '扩展数据读取失败，未输出原始错误。'})
                    self.assertFalse(api._lock.locked())
                    self.assertEqual(api._on_auth_failed.call_count, int(code == 'authentication_required'))

    def test_official_insight_body_uses_param_wrapper(self):
        import threading
        from types import SimpleNamespace
        from hub_insights import collect
        calls=[]
        self_outer=self
        class Client:
            session=SimpleNamespace(cookies=SimpleNamespace(clear=lambda:None),close=lambda:None)
            def _post(self,endpoint,body, *, allow_empty_data=False):
                self_outer.assertTrue(allow_empty_data)
                calls.append((endpoint,body))
                return {'data':{'relation_word_list':[{'relation_word':'银杏'}]},'metadata':{'fetched_at':'2026-10-28T12:00:00+08:00'}}
        api=SimpleNamespace(_lock=threading.Lock(),_client=lambda:Client())
        result=collect(api,{**QUERY,'kind':'related'})
        self.assertTrue(result['ok'])
        self.assertEqual(calls[0][1],{'param':{'keyword':'酵素','start_date':'20250101','end_date':'20250107','app_name':'aweme'}})
        self.assertFalse(api._lock.locked())
        # A second explicit call has no browser-side cooldown.
        self.assertTrue(collect(api,{**QUERY,'kind':'related'})['ok'])
        self.assertEqual(len(calls),2)
        api._lock.acquire()
        try:
            self.assertEqual(collect(api,{**QUERY,'kind':'related'})['code'],'busy')
            self.assertEqual(len(calls),2)
        finally:
            api._lock.release()

    def test_collect_empty_and_unknown_responses(self):
        import threading
        from types import SimpleNamespace
        from client import DouyinIndexClient, ORIGIN, SanitizedClientError
        from hub_insights import collect
        from test_client import FixtureSession, response
        cases = {
            'related': [None, {}, {'BaseResp': {'StatusCode': 0}},
                        {'relation_word_list': None},
                        {'relation_word_list': [], 'search_relation_word_list': None},
                        {'relation_word_list': [], 'search_relation_word_list': []}],
            'audience': [None, {}, {'data': None}, {'data': []}, []],
        }
        for kind, values in cases.items():
            for value in values + [{'foo': 1}, {'relation_word_list': [], 'foo': 1}, 'unknown', 0]:
                with self.subTest(kind=kind, value=value):
                    session = FixtureSession(response({'status': 0, 'data': value}))
                    api = SimpleNamespace(_lock=threading.Lock(),
                                          _client=lambda: DouyinIndexClient(session))
                    result = collect(api, {**QUERY, 'kind': kind})
                    self.assertFalse(result['ok'])
                    self.assertEqual(result['code'], 'no_data' if value in values else 'response_schema')
                    if value in values:
                        self.assertEqual(result, {'ok': False, 'code': 'no_data'})
                    self.assertEqual(len(session.calls), 1)
                    self.assertFalse(api._lock.locked())
        for kind in ('get_relation_word', 'get_portrait'):
            client = DouyinIndexClient(FixtureSession(response({'status': 1, 'data': None})))
            with self.assertRaises(SanitizedClientError):
                client._post(ORIGIN+'/api/v2/index/'+kind, {}, allow_empty_data=True)

    def test_custom_dates_and_missing_day_integrity(self):
        today=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
        earliest=today.replace(year=today.year-2) if not(today.month==2 and today.day==29) else today.replace(year=today.year-2,day=28)
        self.assertEqual(validate_query({**QUERY,'start_date':earliest.isoformat(),'end_date':(today-dt.timedelta(days=1)).isoformat()})['start_date'],earliest.isoformat())
        with self.assertRaises(HubError):validate_query({**QUERY,'start_date':(earliest-dt.timedelta(days=1)).isoformat(),'end_date':(today-dt.timedelta(days=1)).isoformat()})
        q=validate_query({**QUERY,'start_date':'2025-01-02'})
        self.assertEqual(q['start_date'],'2025-01-02')
        for start,end in [('2025-01-08','2025-01-07'),('2023-01-01','2025-01-07')]:
            with self.assertRaises(HubError):validate_query({**QUERY,'start_date':start,'end_date':end})
        raw=fixture();raw['rows']=raw['rows'][:3];raw['metadata']['end_date']='2025-01-03'
        self.assertTrue(public_result(raw)['metadata']['complete'])
        raw['rows'].pop();raw['metadata']['complete']=False
        self.assertEqual(public_result(raw)['metadata']['missing_search_dates']['酵素'],['2025-01-03'])

    def test_projection_is_bounded_and_does_not_expose_extra_fields(self):
        data=project({'relation_word_list':[{'relation_word':'消化酶','relation_score':'0.5','cookie':'SECRET','content_list':['SECRET']}],'session':'SECRET'},'related')
        self.assertNotIn('SECRET',json.dumps(data));self.assertEqual(data['content_related'][0]['relation_score'],.5)
        p=project({'data':[{'name_en':'gender','label_list':[{'name_zh':'女','value':.6,'tgi':120,'cookie':'SECRET'}]}]},'audience')
        self.assertEqual(p['dimensions'][0]['labels'][0]['value'],.6)
        self.assertNotIn('SECRET',json.dumps(p))
        with self.assertRaises(HubError):project({'unexpected':[]},'related')
        with self.assertRaises(HubError):validate_insight({**QUERY,'kind':'audience','keywords':['a','b']})

class InsightWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile
        from pathlib import Path
        from hub_web import Hub
        from unittest.mock import patch
        async def inline_thread(func, *args):
            return func(*args)
        self.thread_patch = patch("hub_web.asyncio.to_thread", side_effect=inline_thread)
        self.thread_patch.start()
        self.addCleanup(self.thread_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.hub = Hub(Path(self.temp.name), bridge=self.bridge)

    def bridge(self, command, payload, timeout):
        if command == 'status':
            return {'ok': True, 'state': 'browser_ready'}
        self.assertEqual(command, 'insight')
        return self.response

    async def asyncTearDown(self):
        self.hub.store.close()
        self.temp.cleanup()

    async def test_insight_no_data_consumes_attempt_without_pause(self):
        import threading
        from types import SimpleNamespace
        from client import DouyinIndexClient
        from hub_insights import collect
        from hub_web import SAFE_ERRORS
        from test_client import FixtureSession, response
        for kind in ('related', 'audience'):
            with self.subTest(kind=kind):
                self.hub.store.set('last_attempt_at', 0)
                before = self.hub.store.get('daily', {}).get('count', 0)
                job, _ = self.hub.store.submit({**QUERY, 'kind': kind}, 'fixture', 'empty-' + kind)
                session = FixtureSession(response({'status': 0, 'data': None}))
                api = SimpleNamespace(_lock=threading.Lock(), _client=lambda: DouyinIndexClient(session))
                self.response = collect(api, {**QUERY, 'kind': kind})
                self.assertEqual(len(session.calls), 1)
                self.assertTrue(await self.hub.run_one())
                result = self.hub.store.job(job['id'])
                self.assertEqual(result['status'], 'no_data')
                self.assertEqual(result['error'], {'code': 'no_data', 'message': SAFE_ERRORS['no_data']})
                self.assertFalse(self.hub.store.get('paused'))
                self.assertTrue(self.hub.store.get('last_result')['ok'])
                self.assertEqual(self.hub.store.get('daily')['count'], before + 1)

    async def test_insight_login_failure_pauses_queued_work(self):
        from unittest.mock import Mock
        for kind in ('related', 'audience'):
            with self.subTest(kind=kind):
                self.hub.store.set('paused', None)
                self.hub.store.set('last_attempt_at', 0)
                job, _ = self.hub.store.submit({**QUERY, 'kind': kind}, 'fixture', 'login-' + kind)
                queued, _ = self.hub.store.submit({**QUERY, 'kind': kind, 'keywords': ['银杏']},
                                                   'fixture', 'queued-' + kind)
                # No login keyword: the explicit code must cause the pause.
                self.response = {'ok': False, 'code': 'authentication_required', 'message': 'Session expired'}
                self.assertTrue(await self.hub.run_one())
                failed = self.hub.store.job(job['id'])
                self.assertEqual(failed['status'], 'failed')
                self.assertEqual(failed['error']['code'], 'authentication_required')
                self.assertEqual(self.hub.store.get('paused')['code'], 'authentication_required')
                self.hub.store.set('last_attempt_at', 0)
                bridge = Mock(side_effect=AssertionError('Paused work must not call bridge'))
                original = self.hub.bridge
                self.hub.bridge = bridge
                try:
                    self.assertFalse(await self.hub.run_one())
                    bridge.assert_not_called()
                finally:
                    self.hub.bridge = original
                self.assertEqual(self.hub.store.job(queued['id'])['status'], 'queued')
                self.hub.store.update_job(queued['id'], 'failed')

class InsightApiTests(_ApiTests):
    async def test_related_replay_keeps_original_job_after_cutoff_advances(self):
        from unittest.mock import patch
        query = {**QUERY, 'kind': 'related', 'start_date': '2026-09-28', 'end_date': '2026-10-27'}
        headers = {**self.bearer, 'Idempotency-Key': 'related-replay'}
        with patch.object(self.hub.store, 'latest_available_date', return_value='2026-10-27') as latest:
            first = await self.client.post('/api/v1/insights/queries', json=query, headers=headers)
            job = (await first.json())['job']
            latest.return_value = '2026-10-28'
            replay = await self.client.post('/api/v1/insights/queries', json=query, headers=headers)
            self.assertEqual(replay.status, 202)
            self.assertEqual((await replay.json())['job'], job)
            for changed in ({'keywords': ['其他词']}, {'end_date': '2026-10-26'},
                            {'start_date': '2026-09-29'}):
                conflict = await self.client.post('/api/v1/insights/queries',
                    json={**query, **changed}, headers=headers)
                self.assertEqual(conflict.status, 409)
        self.assertEqual(self.calls, [])

    async def test_related_replay_rejects_changed_end_before_cutoff(self):
        from unittest.mock import patch
        query = {**QUERY, 'kind': 'related', 'start_date': '2026-10-20', 'end_date': '2026-10-24'}
        headers = {**self.bearer, 'Idempotency-Key': 'related-early-replay'}
        with patch.object(self.hub.store, 'latest_available_date', return_value='2026-10-27'):
            first = await self.client.post('/api/v1/insights/queries', json=query, headers=headers)
            self.assertEqual(first.status, 202)
            job = (await first.json())['job']
            self.assertEqual((job['query']['start_date'], job['query']['end_date']),
                             ('2026-10-14', '2026-10-18'))
            replay = await self.client.post('/api/v1/insights/queries',
                json=dict(reversed(list(query.items()))), headers=headers)
            self.assertEqual((await replay.json())['job'], job)
            conflict = await self.client.post('/api/v1/insights/queries',
                json={**query, 'end_date': '2026-10-25'}, headers=headers)
            self.assertEqual(conflict.status, 409)
        self.assertEqual(self.calls, [])

    async def test_related_shifted_request_replay_preserves_dates(self):
        from unittest.mock import patch
        from test_insight_guards import FixedDatetime
        query = {**QUERY, 'kind': 'related', 'start_date': '2026-10-26', 'end_date': '2026-10-27'}
        headers = {**self.bearer, 'Idempotency-Key': 'related-shifted-replay'}
        with patch('hub_insights.dt.datetime', FixedDatetime), \
                patch('hub_insights.related_end_lag_days', return_value=3), \
                patch.object(self.hub.store, 'latest_available_date', return_value=None) as latest:
            first = await self.client.post('/api/v1/insights/queries', json=query, headers=headers)
            self.assertEqual(first.status, 202)
            job = (await first.json())['job']
            self.assertEqual((job['query']['start_date'], job['query']['end_date']),
                             ('2026-10-24', '2026-10-25'))
            for latest_date in (None, '2026-10-27'):
                latest.return_value = latest_date
                replay = await self.client.post('/api/v1/insights/queries', json=query, headers=headers)
                self.assertEqual(replay.status, 202)
                self.assertEqual((await replay.json())['job'], job)
        self.assertEqual(self.calls, [])

    async def test_legacy_shifted_range_reads_original_dates_but_does_not_cache_them(self):
        query = {**QUERY, 'kind': 'related', 'start_date': '2026-10-24', 'end_date': '2026-10-25',
                 'requested_end_date': '2026-10-27'}
        result = {'metadata': {**query, 'fetched_at': dt.datetime.now(dt.timezone.utc).isoformat()},
                  'data': {'content_related': [], 'search_related': []}}
        self.hub.store.save_insight(query, result)
        original = list(self.hub.store.db.execute('SELECT * FROM insights').fetchone())
        for start, end in [('2026-10-24', '2026-10-25'), ('2026-10-26', '2026-10-27')]:
            response = await self.client.get('/api/v1/insights', params={
                'keyword': QUERY['keywords'][0], 'kind': 'related', 'start_date': start, 'end_date': end},
                headers=self.bearer)
            data = await response.json()
            self.assertEqual(data['status'], 'available')
            self.assertEqual(data['result'], result)
        for start in ('2026-10-23', '2026-10-25', '2026-10-27'):
            self.assertIsNone(self.hub.store.insight({**query, 'start_date': start, 'end_date': '2026-10-27'}))
        requested = {k: v for k, v in query.items() if k != 'requested_end_date'}
        self.assertIsNone(self.hub.store.cached({**requested, 'start_date': '2026-10-26', 'end_date': '2026-10-27'}))
        self.assertEqual(self.hub.store.cached(requested), result)
        self.assertEqual(list(self.hub.store.db.execute('SELECT * FROM insights').fetchone()), original)
        self.assertEqual(self.calls, [])

    async def test_legacy_related_clamp_is_readable_without_rewriting_cache(self):
        query = {**QUERY, 'kind': 'related', 'requested_end_date': '2025-01-09'}
        result = {'metadata': {**query, 'fetched_at': dt.datetime.now(dt.timezone.utc).isoformat()},
                  'data': {'content_related': [], 'search_related': []}}
        self.hub.store.save_insight(query, result)
        original = list(self.hub.store.db.execute('SELECT * FROM insights').fetchone())
        for end in ('2025-01-07', '2025-01-09'):
            response = await self.client.get('/api/v1/insights', params={
                'keyword': '酵素', 'kind': 'related', 'start_date': QUERY['start_date'], 'end_date': end}, headers=self.bearer)
            data = await response.json()
            self.assertEqual(data['status'], 'available')
            self.assertEqual(data['result']['metadata']['end_date'], '2025-01-07')
        actual = {k: v for k, v in query.items() if k != 'requested_end_date'}
        self.assertEqual(self.hub.store.cached(actual), result)
        self.assertIsNone(self.hub.store.cached({**actual, 'end_date': '2025-01-09'}))
        self.hub.store.save_insight(query, {**result, 'no_data': True,
            'metadata': {**result['metadata'], 'fetched_at': (dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=1)).isoformat()}})
        self.assertEqual(self.hub.store.latest_real_insight(actual), result)
        self.assertEqual(list(self.hub.store.db.execute('SELECT * FROM insights ORDER BY fetched LIMIT 1').fetchone()), original)
        self.assertEqual(self.calls, [])

    async def test_related_uses_stored_trend_limit_and_exact_history(self):
        from unittest.mock import patch
        query = {**QUERY, 'start_date': '2026-09-28', 'end_date': '2026-10-27'}
        with patch.object(self.hub.store, 'latest_available_date', return_value='2026-10-27'):
            history = await (await self.client.get('/api/v1/history', headers=self.bearer)).json()
            self.assertEqual(history['related_latest_available_date'], '2026-10-25')
            for kind in ('related', 'audience', None):
                body = {**query, **({'kind': kind} if kind else {})}
                response = await self.client.post('/api/v1/'+('insights/queries' if kind else 'queries'),
                    json=body, headers={**self.bearer, 'Idempotency-Key': 'dates-'+str(kind)})
                self.assertEqual(response.status, 202)
                actual = (await response.json())['job']['query']
                self.assertEqual(actual['start_date'], query['start_date'])
                self.assertEqual(actual['end_date'], '2026-10-25' if kind == 'related' else '2026-10-27')
                if kind == 'related':
                    result = {'data': {'content_related': [], 'search_related': []},
                              'metadata': {**actual, 'fetched_at': dt.datetime.now(dt.timezone.utc).isoformat()}}
                    self.hub.store.save_insight(actual, result)
                    response = await self.client.get('/api/v1/insights', params={
                        'keyword': '酵素', 'kind': kind, 'start_date': actual['start_date'], 'end_date': actual['end_date']}, headers=self.bearer)
                    self.assertEqual((await response.json())['status'], 'available')
            expected = {'2026-10-24': '2026-10-18', '2026-10-25': '2026-10-25'}
            for end, used in expected.items():
                response = await self.client.post('/api/v1/insights/queries',
                    json={**query, 'kind': 'related', 'end_date': end},
                    headers={**self.bearer, 'Idempotency-Key': 'early-'+end})
                self.assertEqual((await response.json())['job']['query']['end_date'], used)
            response = await self.client.get('/api/v1/insights', params={
                'keyword': '酵素', 'kind': 'related', 'start_date': query['start_date'], 'end_date': query['end_date']}, headers=self.bearer)
            self.assertEqual((await response.json())['query']['end_date'], '2026-10-27')
        self.assertEqual(self.calls, [])

    async def test_all_saved_ranges_and_attempt_metadata_are_read_only(self):
        q = {**QUERY, 'kind': 'audience'}
        def saved(query, stamp, empty=False):
            result = {'metadata': {**query, 'fetched_at': stamp}, 'no_data': empty,
                      'data': {'dimensions': [{'dimension': 'gender', 'labels': []}]},
                      'private_extra': 'SYNTHETIC_NOT_PUBLIC'}
            self.hub.store.save_insight(query, result)
        saved(q, '2025-01-08T00:00:00+08:00')
        saved(q, '2025-01-09T00:00:00+08:00', empty=True)
        old = {**q, 'start_date': '2022-01-01', 'end_date': '2022-01-07'}
        saved(old, '2022-01-08T00:00:00+08:00')
        empty = {**q, 'start_date': '2025-02-01', 'end_date': '2025-02-07'}
        saved(empty, '2025-02-08T00:00:00+08:00', empty=True)
        saved({**q, 'keywords': ['其他词']}, '2025-01-08T00:00:00+08:00')
        saved({**q, 'kind': 'related'}, '2025-01-08T00:00:00+08:00')
        failed, _ = self.hub.store.submit(old, 'admin', 'old-failed')
        self.hub.store.update_job(failed['id'], 'failed', error={'code': 'business_error', 'message': 'SYNTHETIC_NOT_PUBLIC'})
        for i in range(31):
            job, _ = self.hub.store.submit({**q, 'keywords': ['其他词']}, 'admin', f'filler-{i}')
            self.hub.store.update_job(job['id'], 'failed')
        read = self.hub.store.issue_key('ranges-read', ['read'])
        headers = {'Authorization': 'Bearer '+read['token']}
        before = self.hub.store.get('daily'), self.hub.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
        response = await self.client.get('/api/v1/insights/ranges?keyword=酵素&kind=audience', headers=headers)
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data['ranges'], [
            {'start_date': '2025-02-01', 'end_date': '2025-02-07', 'status': 'no_data'},
            {'start_date': q['start_date'], 'end_date': q['end_date'], 'status': 'available'},
            {'start_date': '2022-01-01', 'end_date': '2022-01-07', 'status': 'available'}])
        self.assertEqual(len(data['attempts']), 1)
        self.assertEqual(data['attempts'][0]['error_code'], 'business_error')
        self.assertNotIn('SYNTHETIC_NOT_PUBLIC', json.dumps(data))
        self.assertFalse(data['live_request'])
        self.assertFalse(self.calls)
        self.assertEqual(before, (self.hub.store.get('daily'), self.hub.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]))
        # A listed historical range must remain readable after the online window moves on.
        response = await self.client.get('/api/v1/insights', params={
            'keyword': '酵素', 'kind': 'audience', 'start_date': old['start_date'], 'end_date': old['end_date']}, headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['status'], 'available')
        # Historical reads must not relax either online submission endpoint.
        for path, query in [('insights/queries', old), ('queries', {k:v for k,v in old.items() if k != 'kind'})]:
            response = await self.client.post('/api/v1/'+path, json=query,
                                             headers={**self.bearer, 'Idempotency-Key': 'reject-old-'+path.replace('/', '-')})
            self.assertEqual(response.status, 400)
            self.assertEqual((await response.json())['error']['code'], 'invalid_dates')
        for start, end in [('2022-01-08','2022-01-07'), ('2022-01-01','2025-01-07'), ('invalid','2022-01-07')]:
            response = await self.client.get('/api/v1/insights', params={
                'keyword': '酵素', 'kind': 'audience', 'start_date': start, 'end_date': end}, headers=headers)
            self.assertEqual(response.status, 400)
        self.assertFalse(self.calls)
        self.assertEqual((await self.client.get('/api/v1/insights/ranges?keyword=酵素&kind=audience')).status, 401)
        for params in ['keyword=酵素', 'keyword=&kind=audience', 'keyword=酵素&kind=unknown',
                       'keyword=酵素&kind=audience&extra=1', 'keyword=酵素&keyword=其他&kind=audience']:
            self.assertEqual((await self.client.get('/api/v1/insights/ranges?'+params, headers=headers)).status, 400)

    async def test_priority_fifteen_seconds_and_http_diagnostics(self):
        import time
        related,_=self.hub.store.submit({**QUERY,'kind':'related'},'admin','priority-related')
        trend,_=self.hub.store.submit(QUERY,'admin','priority-trend')
        self.hub.store.set('last_attempt_at',time.time()-16)
        await self.hub.run_one()
        self.assertEqual(self.hub.store.job(trend['id'])['status'],'succeeded')
        self.assertEqual(self.hub.store.job(related['id'])['status'],'queued')
        self.response={'ok':False,'code':'http_error','http_status':422}
        self.hub.store.set('last_attempt_at',time.time()-16)
        await self.hub.run_one()
        self.assertEqual(self.hub.store.job(related['id'])['error']['http_status'],422)

    async def test_empty_trend_is_no_data_not_success(self):
        self.response={'ok':True,**fixture(complete=False)}
        self.response['rows']=[]
        job,_=self.hub.store.submit(QUERY,'admin','empty-series')
        await self.hub.run_one()
        self.assertEqual(self.hub.store.job(job['id'])['status'],'no_data')

    async def test_insight_queue_cache_and_no_live_reads(self):
        q={**QUERY,'kind':'related'}
        self.response={'ok':True,'data':{'content_related':[],'search_related':[]},'metadata':{**q,'fetched_at':dt.datetime.now(dt.timezone.utc).isoformat()}}
        headers={**self.bearer,'Idempotency-Key':'insight-test'}
        r=await self.client.post('/api/v1/insights/queries',headers=headers,json=q)
        self.assertEqual(r.status,202);job=(await r.json())['job']
        await self.hub.run_one()
        self.assertEqual(self.hub.store.job(job['id'])['status'],'succeeded')
        r=await self.client.get('/api/v1/insights',params={'keyword':'酵素','kind':'related','end_date':'2025-01-07'},headers=self.bearer)
        self.assertEqual((await r.json())['status'],'available');self.assertEqual(len(self.calls),1)
        r=await self.client.post('/api/v1/insights/queries',headers={**self.bearer,'Idempotency-Key':'insight-cached'},json=q)
        self.assertTrue((await r.json())['job']['cache_hit']);self.assertEqual(len(self.calls),1)
        read=self.hub.store.issue_key('readonly',['read'])
        r=await self.client.post('/api/v1/insights/queries',headers={'Authorization':'Bearer '+read['token'],'Idempotency-Key':'blocked'},json=q)
        self.assertEqual(r.status,403)
