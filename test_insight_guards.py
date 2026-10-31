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
        for target in ('hub_insights.dt.datetime', 'hub_store.dt.datetime'):
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

    def test_environment_and_two_year_boundary(self):
        for value, end in [('0', '2026-10-27'), ('1', '2026-10-27'),
                           ('5', '2026-10-23'), ('-1', '2026-10-25'),
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
            'reason': '关联词数据通常比趋势晚出，截止日限制为今天前 3 天'})
        self.assertIn('已从 2026-10-27 调整为 2026-10-25', metadata['note'])
        self.assertEqual(client._post.call_args.args[1]['param']['end_date'], '20261025')
        client.session.cookies.clear.assert_called_once()
        client.session.close.assert_called_once()


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
        self.trend()
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
                self.assertFalse(self.hub.store.trend_no_data('合成词'))
        job, _ = self.hub.store.submit({**self.q, 'kind': 'related'}, 'fixture', 'not-skipped')
        self.hub.bridge = Mock(return_value={'ok': False, 'state': 'unavailable'})
        await self.hub.run_one()
        self.assertEqual(self.hub.store.job(job['id'])['error']['code'], 'browser_unavailable')
        self.hub.bridge.assert_called_once()

    def test_latest_matching_and_historical_observations(self):
        self.trend('partial', None)
        self.assertTrue(self.hub.store.trend_no_data('合成词'))
        self.assertFalse(self.hub.store.trend_no_data('其他词'))
        self.trend('succeeded', 0)
        self.assertFalse(self.hub.store.trend_no_data('合成词'))
        self.trend()
        s = self.hub.store
        with s.db:
            s.db.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?)',
                         ('synthetic', 'hash', encode(self.q), time.time(),
                          encode({'rows': [{'keyword': '合成词', 'search_index': 0}]}), time.time()))
        self.assertFalse(s.trend_no_data('合成词'))
        with s.db:
            s.db.execute('DELETE FROM datasets')
            s.db.execute('INSERT INTO points VALUES(?,?,?,?,?,?,?,?,?)',
                         ('douyin', 'search_index', '全国', '合成词', '2026-10-20',
                          '0', 'null', time.time(), 'synthetic'))
        self.assertFalse(s.trend_no_data('合成词'))
