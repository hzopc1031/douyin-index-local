"""Synthetic date and scheduler guards; no network or browser."""
import datetime as dt
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hub_insights import collect, validate_insight
from hub_store import HubError, encode
from hub_web import Hub


class FixedDatetime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 28, 12, tzinfo=tz)


class ClampTests(unittest.TestCase):
    def setUp(self):
        for target in ('hub_insights.dt.datetime', 'hub_store.dt.datetime', 'hub_web.dt.datetime'):
            p = patch(target, FixedDatetime)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop('INDEX_HUB_RELATED_END_LAG_DAYS', None)
        self.query = {'kind': 'related', 'keywords': ['合成词'],
                      'start_date': '2026-10-21', 'end_date': '2026-10-27'}

    def test_clamp_and_idempotence(self):
        q = validate_insight(self.query)
        self.assertEqual(q['end_date'], '2026-10-25')
        self.assertEqual(q['start_date'], '2026-10-21')
        self.assertEqual(q['requested_end_date'], '2026-10-27')
        self.assertEqual(validate_insight(q), q)
        q = validate_insight({**self.query, 'start_date': '2026-10-26'})
        self.assertEqual((q['start_date'], q['end_date']), ('2026-10-24', '2026-10-25'))
        self.assertEqual(validate_insight(q), q)

    def test_no_clamp_and_audience(self):
        for q in ({**self.query, 'end_date': '2026-10-25'},
                  {**self.query, 'kind': 'audience'}):
            result = validate_insight(q)
            self.assertEqual(result['end_date'], q['end_date'])
            self.assertNotIn('requested_end_date', result)

    def test_trend_based_limit_preserves_earlier_ranges_and_collector_dates(self):
        q = validate_insight(self.query, latest_related_date='2026-10-25')
        self.assertEqual((q['start_date'], q['end_date']), ('2026-10-21', '2026-10-25'))
        self.assertEqual(q['requested_end_date'], '2026-10-27')
        self.assertEqual(validate_insight(q), q)
        early = {**self.query, 'end_date': '2026-10-25'}
        self.assertEqual(validate_insight(early, latest_related_date='2026-10-25')['end_date'], '2026-10-25')
        saturday = validate_insight({**self.query, 'end_date': '2026-10-24'}, latest_related_date='2026-10-25')
        self.assertEqual(saturday['end_date'], '2026-10-18')
        self.assertEqual(saturday['requested_end_date'], '2026-10-24')
        audience = validate_insight({**self.query, 'kind': 'audience'}, latest_related_date='2026-10-25')
        self.assertEqual(audience['end_date'], '2026-10-27')
        shifted = validate_insight({**self.query, 'start_date': '2026-10-26'}, latest_related_date='2026-10-25')
        self.assertEqual((shifted['start_date'], shifted['end_date']), ('2026-10-24', '2026-10-25'))
        self.assertEqual(shifted['requested_end_date'], '2026-10-27')
        historical = validate_insight(self.query, historical=True, latest_related_date='2026-10-25')
        self.assertEqual(historical['end_date'], '2026-10-27')

    def test_non_sunday_end_aligns_even_inside_the_cap(self):
        class Nov11(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 11, 11, 10, tzinfo=tz)
        query = {**self.query, 'start_date': '2026-10-03', 'end_date': '2026-10-30'}
        with patch('hub_insights.dt.datetime', Nov11), patch('hub_store.dt.datetime', Nov11):
            friday = validate_insight(query, latest_related_date='2026-10-30')
            self.assertEqual(friday['end_date'], '2026-10-25')
            self.assertEqual(friday['requested_end_date'], '2026-10-30')
            sunday = validate_insight({**query, 'end_date': '2026-11-01'},
                                      latest_related_date='2026-11-01')
            self.assertEqual(sunday['end_date'], '2026-11-01')
            self.assertNotIn('requested_end_date', sunday)

    def test_related_latest_date_ignores_stale_trend_and_honors_fallback_cap(self):
        from hub_web import related_latest_date
        # FixedDatetime is 2026-10-28, so today-3 is already Sunday 10-25.
        empty = SimpleNamespace(latest_available_date=lambda: None)
        self.assertEqual(related_latest_date(empty), '2026-10-25')
        self.assertEqual(related_latest_date(SimpleNamespace(latest_available_date=lambda: '2026-10-18')),
                         '2026-10-25')
        class Nov11(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 11, 11, 10, tzinfo=tz)
        stale = SimpleNamespace(latest_available_date=lambda: '2026-11-01')
        with patch('hub_web.dt.datetime', Nov11), patch('hub_insights.dt.datetime', Nov11):
            self.assertEqual(related_latest_date(stale), '2026-11-08')
            cap = {'source': 'fallback', 'date': '2026-11-01', 'tried': '2026-11-08',
                   'checked_at': '2026-11-11T10:00:00+08:00'}
            pinned = SimpleNamespace(get=lambda key, default=None: cap if key == 'related_sunday_cap' else default,
                                     latest_available_date=lambda: '2026-11-01')
            self.assertEqual(related_latest_date(pinned), '2026-11-01')
            stale_cap = dict(cap, checked_at='2026-11-10T21:00:00+08:00')
            expired = SimpleNamespace(get=lambda key, default=None: stale_cap if key == 'related_sunday_cap' else default)
            self.assertEqual(related_latest_date(expired), '2026-11-08')

    def test_environment_and_two_year_boundary(self):


        for value, end in [('0', '2026-10-25'), ('1', '2026-10-25'),
                           ('5', '2026-10-18'), ('-1', '2026-10-25'),
                           ('oops', '2026-10-25'), ('1.5', '2026-10-25')]:
            with self.subTest(value=value):
                os.environ['INDEX_HUB_RELATED_END_LAG_DAYS'] = value
                self.assertEqual(validate_insight(self.query)['end_date'], end)
        for lag in ('731', '999999999999999999999'):
            os.environ['INDEX_HUB_RELATED_END_LAG_DAYS'] = lag
            with self.assertRaises(HubError) as error:
                validate_insight(self.query)
            self.assertEqual(error.exception.code, 'invalid_dates')

    def test_collect_clamp_metadata_and_payload(self):
        client = Mock()
        client._post.return_value = {'data': {'relation_word_list': [{'relation_word': '合成关联'}]},
                                    'metadata': {'fetched_at': '2026-10-28T12:00:00+08:00'}}
        api = SimpleNamespace(_lock=threading.Lock(), _client=lambda: client)
        result = collect(api, validate_insight(self.query))
        self.assertTrue(result['ok'])
        metadata = result['metadata']
        self.assertEqual(metadata['end_date_clamped'], {
            'requested': '2026-10-27', 'used': '2026-10-25',
            'reason': '关联词接口按周出数，截止日已调整为可查范围内最近的周日'})
        self.assertIn('已从 2026-10-27 调整为 2026-10-25', metadata['note'])
        self.assertEqual(client._post.call_args.args[1]['param']['end_date'], '20261025')
        client.session.cookies.clear.assert_called_once()
        client.session.close.assert_called_once()

    def test_collect_retries_previous_sunday_on_business_500(self):
        from client import SanitizedClientError
        from hub_insights import RELATED_WEEK_FALLBACK_REASON, remember_related_sunday
        client = Mock()
        fail = SanitizedClientError('business_error', '平台返回未成功的业务状态，本次请求已停止。',
                                    http_status=200, business_code=500)
        client._post.side_effect = [
            fail,
            {'data': {'relation_word_list': [{'relation_word': '合成关联'}]},
             'metadata': {'fetched_at': '2026-10-28T12:00:00+08:00'}},
        ]
        api = SimpleNamespace(_lock=threading.Lock(), _client=lambda: client)
        result = collect(api, {**self.query, 'end_date': '2026-10-25'})
        self.assertTrue(result['ok'])
        self.assertEqual(result['metadata']['end_date'], '2026-10-18')
        self.assertEqual(result['metadata']['start_date'], '2026-10-14')
        self.assertEqual(result['metadata']['end_date_clamped'], {
            'requested': '2026-10-25', 'tried': '2026-10-25', 'used': '2026-10-18',
            'reason': RELATED_WEEK_FALLBACK_REASON})
        self.assertEqual([call.args[1]['param']['end_date'] for call in client._post.call_args_list],
                         ['20261025', '20261018'])
        store = SimpleNamespace(values={})
        store.get = lambda key, default=None: store.values.get(key, default)
        store.set = lambda key, value: store.values.__setitem__(key, value)
        remember_related_sunday(store, result['metadata'])
        self.assertEqual(store.values['related_sunday_cap']['date'], '2026-10-18')
        self.assertEqual(store.values['related_sunday_cap']['tried'], '2026-10-25')
        self.assertEqual(store.values['related_sunday_cap']['source'], 'fallback')

    def test_collect_does_not_retry_non_500_and_gives_up_after_one_fallback(self):
        from client import SanitizedClientError
        limited = SanitizedClientError('rate_limited', '平台返回限频信号，本次请求已停止。',
                                       http_status=200, business_code=429)
        fail = SanitizedClientError('business_error', '平台返回未成功的业务状态，本次请求已停止。',
                                    http_status=200, business_code=500)
        client = Mock()
        client._post.side_effect = [limited]
        api = SimpleNamespace(_lock=threading.Lock(), _client=lambda: client)
        result = collect(api, {**self.query, 'end_date': '2026-10-25'})
        self.assertEqual((result['ok'], result['code']), (False, 'rate_limited'))
        self.assertEqual(client._post.call_count, 1)
        client._post.side_effect = [fail, fail]
        result = collect(api, {**self.query, 'end_date': '2026-10-25'})
        self.assertEqual((result['ok'], result['code'], result.get('business_code')),
                         (False, 'business_error', 500))
        self.assertEqual(client._post.call_count, 3)



class TrendDefaultTests(unittest.TestCase):
    def setUp(self):
        p = patch('hub_web.dt.datetime', FixedDatetime)
        p.start()
        self.addCleanup(p.stop)

    def test_default_end_follows_official_cadence_not_stale_local(self):
        from hub_web import douyin_default_end_date
        stale = SimpleNamespace(latest_available_date=lambda: '2026-10-18')
        self.assertEqual(douyin_default_end_date(stale), ('2026-10-27', 'official_cadence'))
        empty = SimpleNamespace(latest_available_date=lambda: None)
        self.assertEqual(douyin_default_end_date(empty), ('2026-10-27', 'official_cadence'))

    def test_default_end_falls_back_and_records_official_latest(self):
        from hub_web import douyin_default_end_date, remember_douyin_trend_end
        failed = {'source': 'failed', 'tried': '2026-10-27',
                  'checked_at': '2026-10-28T12:00:00+08:00'}
        store = SimpleNamespace(latest_available_date=lambda: '2026-10-18',
                                get=lambda key, default=None: failed if key == 'douyin_trend_end_cap' else default)
        self.assertEqual(douyin_default_end_date(store), ('2026-10-18', 'stored_search_index'))
        stale = dict(failed, checked_at='2026-10-27T12:00:00+08:00')
        expired = SimpleNamespace(latest_available_date=lambda: '2026-10-18',
                                  get=lambda key, default=None: stale if key == 'douyin_trend_end_cap' else default)
        self.assertEqual(douyin_default_end_date(expired), ('2026-10-27', 'official_cadence'))
        mem = SimpleNamespace(values={}, latest_available_date=lambda: '2026-10-18')
        mem.get = lambda key, default=None: mem.values.get(key, default)
        mem.set = lambda key, value: mem.values.__setitem__(key, value)
        remember_douyin_trend_end(mem, {'platform': 'douyin', 'end_date': '2026-10-20'},
                                  result={'rows': [{'date': '2026-10-20', 'search_index': 1}]})
        self.assertEqual(douyin_default_end_date(mem), ('2026-10-27', 'official_cadence'))
        remember_douyin_trend_end(mem, {'platform': 'douyin', 'end_date': '2026-10-27'},
                                  result={'rows': [{'date': '2026-10-26', 'search_index': 1},
                                                   {'date': '2026-10-27', 'search_index': None}]})
        self.assertEqual(douyin_default_end_date(mem), ('2026-10-26', 'official_latest'))
        remember_douyin_trend_end(mem, {'platform': 'douyin', 'end_date': '2026-10-27'},
                                  error={'code': 'business_error'})
        self.assertEqual(douyin_default_end_date(mem), ('2026-10-18', 'stored_search_index'))
        mem.values.clear()
        remember_douyin_trend_end(mem, {'platform': 'douyin', 'kind': 'related', 'end_date': '2026-10-27'},
                                  error={'code': 'business_error'})
        self.assertEqual(douyin_default_end_date(mem), ('2026-10-27', 'official_cadence'))


class SkipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        async def inline_thread(func, *args):
            return func(*args)
        p = patch("hub_web.asyncio.to_thread", side_effect=inline_thread)
        p.start()
        self.addCleanup(p.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bridge = Mock(side_effect=AssertionError('No bridge calls expected'))
        self.hub = Hub(self.temp.name, bridge=self.bridge)
        self.addCleanup(self.hub.store.close)
        self.q = {'platform': 'douyin', 'keywords': ['合成词'],
                  'start_date': '2026-10-20', 'end_date': '2026-10-25'}

    def trend(self, status='no_data', value=None, age=0):
        job, _ = self.hub.store.submit(self.q, 'fixture', str(time.time_ns()))
        self.hub.store.update_job(job['id'], status, result={
            'rows': [{'keyword': '合成词', 'search_index': value}]})
        with self.hub.store.db:
            self.hub.store.db.execute('UPDATE jobs SET updated=? WHERE id=?',
                                      (time.time() - age, job['id']))
        return job

    async def test_skip_both_before_cooldown_and_daily_limit(self):
        trend = self.trend()
        daily = {'date': '2026-10-28', 'count': 50}
        self.hub.store.set('daily', daily)
        last = time.time()
        self.hub.store.set('last_attempt_at', last)
        for kind in ('related', 'audience'):
            job, _ = self.hub.store.submit({**self.q, 'kind': kind}, 'fixture', kind)
            self.assertTrue(await self.hub.run_one())
            result = self.hub.store.job(job['id'])
            self.assertEqual(result['status'], 'no_data')
            self.assertEqual(result['error']['code'], 'skipped_trend_no_data')
            self.assertEqual(result['error']['source_trend'], {
                'job_id': trend['id'], 'platform': 'douyin', 'keyword': '合成词',
                'start_date': self.q['start_date'], 'end_date': self.q['end_date']})
            self.assertEqual(self.hub.store.get('daily'), daily)
            self.assertEqual(self.hub.store.get('last_attempt_at'), last)
        self.bridge.assert_not_called()

    async def test_nonempty_or_expired_trend_does_not_skip(self):
        for status, value, age in [('succeeded', 0, 0), ('partial', 12, 0),
                                   ('no_data', None, 86401)]:
            with self.subTest(status=status, value=value, age=age):
                with self.hub.store.db:
                    self.hub.store.db.execute('DELETE FROM jobs')
                self.trend(status, value, age)
                self.assertFalse(self.hub.store.trend_no_data(self.q))
        job, _ = self.hub.store.submit({**self.q, 'kind': 'related'}, 'fixture', 'not-skipped')
        self.hub.bridge = Mock(return_value={'ok': False, 'state': 'unavailable'})
        await self.hub.run_one()
        self.assertEqual(self.hub.store.job(job['id'])['status'], 'queued')
        self.assertEqual(self.hub.store.get('paused')['code'], 'browser_unavailable')
        self.hub.bridge.assert_called_once()

    def test_latest_matching_and_historical_observations(self):
        self.trend('partial', None)
        self.assertTrue(self.hub.store.trend_no_data(self.q))
        self.assertFalse(self.hub.store.trend_no_data({**self.q, 'keywords': ['其他词']}))
        self.trend('succeeded', 0)
        self.assertFalse(self.hub.store.trend_no_data(self.q))
        self.trend()
        s = self.hub.store
        with s.db:
            s.db.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?)',
                         ('synthetic', 'hash', encode(self.q), time.time(),
                          encode({'rows': [{'keyword': '合成词', 'date': '2026-10-20', 'search_index': 0}]}), time.time()))
        self.assertFalse(s.trend_no_data(self.q))
        with s.db:
            s.db.execute('DELETE FROM datasets')
            s.db.execute('INSERT INTO points VALUES(?,?,?,?,?,?,?,?,?)',
                         ('douyin', 'search_index', '全国', '合成词', '2026-10-20',
                          '0', 'null', time.time(), 'synthetic'))
        self.assertFalse(s.trend_no_data(self.q))

    async def test_other_ranges_and_platforms_do_not_skip(self):
        self.trend()
        self.hub.store.set('last_attempt_at', time.time())
        for changes in ({'start_date': '2026-10-19'}, {'end_date': '2026-10-24'},
                        {'platform': 'other'}, {'keywords': ['其他词']}):
            with self.subTest(changes=changes):
                query = {**self.q, **changes, 'kind': 'audience'}
                self.assertIsNone(self.hub.store.trend_no_data(query))
                if changes.get('platform') == 'other':
                    with self.assertRaises(HubError) as error:
                        self.hub.store.submit(query, 'fixture', str(time.time_ns()))
                    self.assertEqual(error.exception.code, 'platform_unavailable')
                    continue
                job, _ = self.hub.store.submit(query, 'fixture', str(time.time_ns()))
                self.assertFalse(await self.hub.run_one())
                self.assertEqual(self.hub.store.job(job['id'])['status'], 'queued')
                with self.hub.store.db:
                    self.hub.store.db.execute('DELETE FROM jobs WHERE id=?', (job['id'],))
        self.bridge.assert_not_called()

    def test_matching_batch_and_newer_unrelated_trend(self):
        self.q['keywords'].append('其他词')
        source = self.trend()
        query = {**self.q, 'keywords': ['合成词'], 'kind': 'audience'}
        self.q = {**self.q, 'start_date': '2026-10-19'}
        self.trend('succeeded', 123)
        self.assertEqual(self.hub.store.trend_no_data(query)['job_id'], source['id'])

    def test_old_observations_outside_range_do_not_suppress_skip(self):
        self.trend()
        s = self.hub.store
        with s.db:
            s.db.execute('INSERT INTO points VALUES(?,?,?,?,?,?,?,?,?)',
                         ('douyin', 'search_index', '全国', '合成词', '2026-10-19',
                          '0', 'null', time.time(), 'synthetic'))
            s.db.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?)',
                         ('synthetic', 'hash', encode(self.q), time.time(),
                          encode({'rows': [{'keyword': '合成词', 'date': '2026-10-19',
                                            'search_index': 0}]}), time.time()))
        self.assertIsNotNone(s.trend_no_data(self.q))
