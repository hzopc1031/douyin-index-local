import datetime as dt
import json
import unittest
from hub_insights import project, validate_insight
from hub_store import HubError, public_result, validate_query
from test_hub import ApiTests as _ApiTests, fixture, QUERY

class InsightTests(unittest.TestCase):
    def test_official_insight_body_uses_param_wrapper(self):
        import threading
        from types import SimpleNamespace
        from hub_insights import collect
        calls=[]
        class Client:
            session=SimpleNamespace(cookies=SimpleNamespace(clear=lambda:None),close=lambda:None)
            def _post(self,endpoint,body):
                calls.append((endpoint,body))
                return {'data':{'relation_word_list':[],'search_relation_word_list':[]},'metadata':{'fetched_at':'2026-10-28T12:00:00+08:00'}}
        api=SimpleNamespace(_lock=threading.Lock(),_cloud_last_attempt=0,_client=lambda:Client())
        result=collect(api,{**QUERY,'kind':'related'})
        self.assertTrue(result['ok'])
        self.assertEqual(calls[0][1],{'param':{'keyword':'酵素','start_date':'20250101','end_date':'20250107','app_name':'aweme'}})
        self.assertFalse(api._lock.locked())

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

class InsightApiTests(_ApiTests):
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
