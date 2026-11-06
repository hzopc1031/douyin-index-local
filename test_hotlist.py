"""Synthetic hot-list tests. No live board responses are stored or required."""
import asyncio
import base64
import contextlib
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from aiohttp.test_utils import TestClient, TestServer

from desktop_gateway import HUB_KEY, create_app
from hot_rising import rising_report
from hotlist_client import HOT_LIST_URL, HotListClient, HotListError, project_hot_list
from hub_store import HubError, Store, TZ, encode
from hub_web import HOT_DAILY_SUCCESS_LIMIT, Hub, hotlist_interval_seconds


def response(payload, status=200, headers=None):
    result = requests.Response()
    result.status_code = status
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    result.raw = io.BytesIO(raw)
    result.headers.update(headers or {})
    return result


def board(**overrides):
    item = {'position': 1, 'word': '合成话题甲', 'sentence_id': 's1', 'hot_value': 1200,
            'label': 0, 'aweme_infos': [{'secret': 'drop-me'}], 'word_cover': {'url': 'https://example.invalid/x'}}
    item.update(overrides)
    return {'status_code': 0, 'data': {'word_list': [item, {
        'position': 2, 'word': '合成话题乙', 'hot_value': None, 'sentence_id': 's2'}],
        'trending_list': [{'word': '不入库'}]}}


class ClientTests(unittest.TestCase):
    def fetch(self, payload, status=200, headers=None):
        session = requests.Session()
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            return response(payload, status, headers)

        session.get = fake_get
        client = HotListClient(session)
        try:
            return client.fetch(), calls, session
        except HotListError:
            self.assertEqual(len(calls), 1)
            self.assertFalse(calls[0][1]['allow_redirects'])
            raise
        finally:
            self.assertFalse(session.trust_env)
            self.assertEqual(session.adapters['https://'].max_retries.total, 0)

    def test_projects_scalars_and_keeps_null_heat(self):
        result, calls, _ = self.fetch(board())
        self.assertTrue(result['complete'])
        self.assertEqual(calls[0][0], HOT_LIST_URL)
        self.assertFalse(calls[0][1]['allow_redirects'])
        self.assertTrue(calls[0][1]['verify'])
        self.assertNotIn('Cookie', calls[0][1]['headers'])
        encoded = json.dumps(result)
        self.assertNotIn('aweme_infos', encoded)
        self.assertNotIn('word_cover', encoded)
        self.assertNotIn('trending_list', encoded)
        self.assertNotIn('drop-me', encoded)
        second = result['items'][1]
        self.assertIsNone(second['hot_value'])
        self.assertEqual(second['position'], 2)
        self.assertEqual(second['key_kind'], 'sentence_id')
        self.assertNotEqual(second['position'], 50)

    def test_exact_word_key_when_sentence_id_missing(self):
        projected = project_hot_list({'status_code': 0, 'data': {'word_list': [
            {'position': 3, 'word': '  仅有话题  '}]}})
        item = projected['items'][0]
        self.assertEqual(item['word'], '仅有话题')
        self.assertIsNone(item['sentence_id'])
        self.assertIsNone(item['hot_value'])
        self.assertEqual(item['key_kind'], 'word')
        self.assertEqual(item['item_key'], 'word:仅有话题')

    def test_redirect_is_not_followed(self):
        with self.assertRaises(HotListError) as caught:
            self.fetch(b'', 302, {'Location': 'https://evil.example/next'})
        self.assertEqual(caught.exception.code, 'redirect_rejected')

    def test_non_json_rate_limit_and_denied(self):
        with self.assertRaises(HotListError) as caught:
            self.fetch(b'<html>', 200)
        self.assertEqual(caught.exception.code, 'response_format')
        with self.assertRaises(HotListError) as caught:
            self.fetch({}, 429)
        self.assertEqual(caught.exception.code, 'rate_limited')
        with self.assertRaises(HotListError) as caught:
            self.fetch({}, 403)
        self.assertEqual(caught.exception.code, 'access_denied')

    def test_incomplete_boards_raise(self):
        cases = [
            {'status_code': '0', 'data': {'word_list': [{'position': 1, 'word': '甲'}]}},
            {'status_code': 0, 'data': {'word_list': []}},
            {'status_code': 0, 'data': {'word_list': [
                {'position': 1, 'word': '甲'}, {'position': 1, 'word': '乙'}]}},
            {'status_code': 0, 'data': {'word_list': [
                {'position': 1, 'word': '甲'}, {'position': 2, 'word': '甲'}]}},
            {'status_code': 0, 'data': {'word_list': [{'word': '甲'}]}},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(HotListError) as caught:
                    project_hot_list(payload)
                self.assertEqual(caught.exception.code, 'incomplete')

    def test_oversized_response_is_not_retried(self):
        with patch('hotlist_client.MAX_RESPONSE_BYTES', 8):
            with self.assertRaises(HotListError) as caught:
                self.fetch(board())
        self.assertEqual(caught.exception.code, 'response_too_large')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'hub.sqlite3')
        self.addCleanup(self.store.close)

    def snapshot(self, items, fetched='2026-11-04T12:00:00+08:00'):
        return {'complete': True, 'board': 'word_list', 'source': HOT_LIST_URL,
                'fetched_at': fetched, 'items': items}

    def item(self, word, position, sentence_id, hot):
        return {'item_key': 'id:' + sentence_id if sentence_id else 'word:' + word,
                'key_kind': 'sentence_id' if sentence_id else 'word',
                'sentence_id': sentence_id, 'word': word, 'position': position, 'hot_value': hot,
                'label': None, 'event_time': None, 'video_count': None, 'sentence_tag': None,
                'word_type': None, 'group_id': None}

    def test_incomplete_snapshot_is_not_stored(self):
        with self.assertRaises(HubError):
            self.store.save_hot_snapshot({'complete': False, 'items': []})
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM hot_snapshots').fetchone()[0], 0)

    def test_null_heat_and_absence_are_not_zero_or_rank_50(self):
        first = project_hot_list({'status_code': 0, 'data': {'word_list': [
            {'position': 1, 'word': '合成话题甲', 'sentence_id': 's1', 'hot_value': None},
            {'position': 2, 'word': '仅有话题'}]}})
        first['fetched_at'] = '2026-11-04T12:00:00+08:00'
        self.store.save_hot_snapshot(first)
        second = project_hot_list({'status_code': 0, 'data': {'word_list': [
            {'position': 4, 'word': '合成话题甲', 'sentence_id': 's1', 'hot_value': 80}]}})
        second['fetched_at'] = '2026-11-04T13:00:00+08:00'
        self.store.save_hot_snapshot(second)
        latest = self.store.latest_hot()
        self.assertEqual([item['word'] for item in latest['items']], ['合成话题甲'])
        self.assertIsNone(self.store.hot_history(sentence_id='s1')['points'][0]['hot_value'])
        absent = self.store.hot_history(word='仅有话题')['points']
        self.assertEqual(len(absent), 1)
        self.assertEqual(absent[0]['position'], 2)
        self.assertIsNone(absent[0]['hot_value'])
        self.assertNotIn(50, [point['position'] for point in absent])
        self.assertNotIn(0, [point['hot_value'] for point in absent])


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bridge_calls = []

        def bridge(*args):
            self.bridge_calls.append(args)
            return {'ok': True, 'state': 'browser_ready'}

        self.hub = Hub(self.temp.name, bridge)
        self.addCleanup(self.hub.store.close)
        self.hub.store.set('daily', {'date': '2026-11-04', 'count': 7})
        self.hub.store.set('last_attempt_at', 123)
        self.now = 1_800_000_000
        self.day = dt.datetime.fromtimestamp(self.now, TZ).date().isoformat()

    def enable(self, **extra):
        values = {'INDEX_HUB_HOTLIST_ENABLED': '1', 'INDEX_HUB_HOTLIST_INTERVAL_SECONDS': '3600'}
        values.update(extra)
        return patch.dict(os.environ, values)

    def script(self, *results):
        class Scripted:
            def __init__(self):
                self.results = list(results)
                self.calls = 0

            def fetch(self):
                self.calls += 1
                item = self.results.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

            def close(self):
                pass

        return Scripted()

    def saved(self, hot=100):
        projected = project_hot_list({'status_code': 0, 'data': {'word_list': [
            {'position': 1, 'word': '合成话题甲', 'sentence_id': 's1', 'hot_value': hot}]}})
        projected['fetched_at'] = dt.datetime.fromtimestamp(self.now, TZ).isoformat()
        return projected

    async def test_disabled_does_not_fetch_or_start(self):
        scripted = self.script(self.saved())
        result = await self.hub.run_hot_once(client=scripted, now=self.now)
        self.assertEqual(result['code'], 'disabled')
        self.assertEqual(scripted.calls, 0)
        await self.hub.start(None)
        self.assertIsNone(self.hub.hotlist_task)
        self.hub.worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.hub.worker_task

    async def test_success_does_not_spend_search_quota(self):
        scripted = self.script(self.saved())
        with self.enable():
            result = await self.hub.run_hot_once(client=scripted, now=self.now)
        self.assertTrue(result['ok'])
        self.assertEqual(scripted.calls, 1)
        self.assertEqual(self.hub.store.get('daily'), {'date': '2026-11-04', 'count': 7})
        self.assertEqual(self.hub.store.get('last_attempt_at'), 123)
        self.assertEqual(self.hub.store.get('hot_daily'), {'date': self.day, 'count': 1})
        self.assertEqual(self.bridge_calls, [])
        with self.enable():
            again = await self.hub.run_hot_once(client=scripted, now=self.now + 10)
        self.assertEqual(again['code'], 'interval')
        self.assertEqual(scripted.calls, 1)

    async def test_rate_limit_pauses_until_manual_resume(self):
        scripted = self.script(HotListError('rate_limited', 'stop', http_status=429), self.saved())
        with self.enable():
            first = await self.hub.run_hot_once(client=scripted, now=self.now)
            second = await self.hub.run_hot_once(client=scripted, now=self.now + 7200)
        self.assertEqual(first['code'], 'rate_limited')
        self.assertEqual(second['code'], 'paused')
        self.assertEqual(scripted.calls, 1)
        self.assertEqual(self.hub.store.db.execute('SELECT COUNT(*) FROM hot_snapshots').fetchone()[0], 0)
        self.assertEqual(self.hub.store.get('daily')['count'], 7)
        self.assertIsNone(self.hub.store.get('hot_daily'))
        self.hub.store.set('hot_paused', None)
        with self.enable():
            too_soon = await self.hub.run_hot_once(client=scripted, now=self.now + 10)
            resumed = await self.hub.run_hot_once(client=scripted, now=self.now + 7200)
        self.assertEqual(too_soon['code'], 'interval')
        self.assertTrue(resumed['ok'])
        self.assertEqual(scripted.calls, 2)

    async def test_non_json_pauses_and_incomplete_does_not(self):
        scripted = self.script(HotListError('response_format', 'stop'), self.saved())
        with self.enable():
            await self.hub.run_hot_once(client=scripted, now=self.now)
            paused = await self.hub.run_hot_once(client=scripted, now=self.now + 7200)
        self.assertEqual(paused['code'], 'paused')
        self.assertEqual(scripted.calls, 1)
        other = self.script(HotListError('incomplete', 'stop'), self.saved())
        self.hub.store.set('hot_paused', None)
        self.hub.store.set('hot_last_attempt_at', 0)
        with self.enable():
            failed = await self.hub.run_hot_once(client=other, now=self.now)
            retried = await self.hub.run_hot_once(client=other, now=self.now + 3600)
        self.assertEqual(failed['code'], 'incomplete')
        self.assertIsNone(self.hub.store.get('hot_paused'))
        self.assertTrue(retried['ok'])
        self.assertEqual(other.calls, 2)

    async def test_daily_success_cap_blocks_before_fetch(self):
        scripted = self.script(self.saved())
        self.hub.store.set('hot_daily', {'date': self.day, 'count': HOT_DAILY_SUCCESS_LIMIT})
        with self.enable():
            result = await self.hub.run_hot_once(client=scripted, now=self.now)
        self.assertEqual(result['code'], 'daily_limit')
        self.assertEqual(scripted.calls, 0)

    def test_interval_floor(self):
        with patch.dict(os.environ, {'INDEX_HUB_HOTLIST_INTERVAL_SECONDS': '60'}):
            self.assertEqual(hotlist_interval_seconds(), 1800)
        with patch.dict(os.environ, {'INDEX_HUB_HOTLIST_INTERVAL_SECONDS': '7200'}):
            self.assertEqual(hotlist_interval_seconds(), 7200)
        with patch.dict(os.environ, {'INDEX_HUB_HOTLIST_INTERVAL_SECONDS': 'soon'}):
            self.assertEqual(hotlist_interval_seconds(), 3600)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / '.cloud-state').mkdir()
        (self.root / '.cloud-state/access.json').write_text(
            json.dumps({'username': 'fixture', 'password': 'admin-fixture-only'}))
        (self.root / 'hub.html').write_text('<h1>Fixture hub</h1>')
        app = create_app(self.root, bridge=lambda *args: {'ok': True, 'state': 'browser_ready'},
                         desktop_dir=self.root)
        self.hub = app[HUB_KEY]
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.hub.worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.hub.worker_task
        self.admin = {'Authorization': 'Basic ' + base64.b64encode(b'fixture:admin-fixture-only').decode(),
                      'X-Hub-Request': '1'}
        issued = self.hub.store.issue_key('Reader', ['read'])
        self.reader = {'Authorization': 'Bearer ' + issued['token']}

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    def seed(self):
        projected = project_hot_list(board())
        projected['fetched_at'] = '2026-11-04T12:00:00+08:00'
        self.hub.store.save_hot_snapshot(projected)

    async def test_bearer_read_and_admin_resume(self):
        self.assertEqual((await self.client.get('/api/v1/hot/latest')).status, 401)
        self.seed()
        response = await self.client.get('/api/v1/hot/latest', headers=self.reader)
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertFalse(body['live_request'])
        self.assertIn('不是搜索指数', body['note'])
        self.assertIsNone(body['items'][1]['hot_value'])
        self.assertNotIn('aweme_infos', json.dumps(body))
        denied = await self.client.post('/api/v1/hot/resume', headers=self.reader)
        self.assertEqual(denied.status, 403)
        self.hub.store.set('hot_paused', {'code': 'rate_limited', 'since': 1})
        resumed = await self.client.post('/api/v1/hot/resume', headers=self.admin)
        self.assertEqual(resumed.status, 200)
        self.assertIsNone(self.hub.store.get('hot_paused'))
        history = await self.client.get('/api/v1/hot/word?sentence_id=s2', headers=self.reader)
        self.assertEqual(history.status, 200)
        self.assertEqual((await history.json())['points'][0]['position'], 2)
        missing = await self.client.get('/api/v1/hot/word?word=没有这个话题', headers=self.reader)
        self.assertEqual((await missing.json())['points'], [])
        schema = await (await self.client.get('/api/v1/openapi.json', headers=self.reader)).json()
        self.assertIn('/api/v1/hot/latest', schema['paths'])
        self.assertIn('/api/v1/hot/word', schema['paths'])
        self.assertIn('/api/v1/hot/resume', schema['paths'])
        status = await (await self.client.get('/api/v1/status', headers=self.reader)).json()
        self.assertFalse(status['hotlist']['enabled'])
        self.assertEqual(status['hotlist']['daily_success_limit'], 48)
        self.assertEqual(status['daily_online_attempts'], 0)

    async def test_rising_read_does_not_spend_search_quota(self):
        self.hub.store.set('daily', {'date': '2026-11-04', 'count': 7})
        self.seed()
        self.assertEqual((await self.client.get('/api/v1/hot/rising')).status, 401)
        response = await self.client.get('/api/v1/hot/rising', headers=self.reader)
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertFalse(body['live_request'])
        self.assertIn('不是搜索指数', body['note'])
        self.assertIn('rank_rise', body['thresholds'])
        self.assertEqual(body['thresholds']['rank_rise'], 10)
        self.assertEqual(self.hub.store.get('daily'), {'date': '2026-11-04', 'count': 7})
        self.assertEqual(self.hub.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        schema = await (await self.client.get('/api/v1/openapi.json', headers=self.reader)).json()
        self.assertIn('/api/v1/hot/rising', schema['paths'])
        rejected = await self.client.get('/api/v1/hot/rising?word=合成话题甲', headers=self.reader)
        self.assertEqual(rejected.status, 400)


class RisingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'hub.sqlite3')
        self.addCleanup(self.store.close)
        self.current = dt.datetime(2026, 11, 4, 12, tzinfo=TZ)
        self.store.set('daily', {'date': '2026-11-04', 'count': 7})

    def entry(self, word, position, hot, sentence_id='s1'):
        if sentence_id:
            key, kind = 'id:' + sentence_id, 'sentence_id'
        else:
            key, kind, sentence_id = 'word:' + word, 'word', None
        return {'item_key': key, 'key_kind': kind, 'sentence_id': sentence_id, 'word': word,
                'position': position, 'hot_value': hot, 'label': None, 'event_time': None,
                'video_count': None, 'sentence_tag': None, 'word_type': None, 'group_id': None}

    def put(self, seconds_before, items):
        stamp = self.current - dt.timedelta(seconds=seconds_before)
        self.store.save_hot_snapshot({'complete': True, 'board': 'word_list', 'source': HOT_LIST_URL,
                                      'fetched_at': stamp.isoformat(), 'items': items})

    def report(self):
        return rising_report(self.store)

    def by_word(self, word='合成话题甲'):
        return next(item for item in self.report()['items'] if item['word'] == word)

    def test_no_baseline_leaves_dependent_metrics_null_and_marks_new(self):
        self.put(0, [self.entry('合成话题甲', 4, 1000)])
        item = self.by_word()
        self.assertIsNone(self.report()['baseline_fetched_at'])
        self.assertTrue(item['new_entry'])
        self.assertIsNone(item['rank_rise'])
        self.assertIsNone(item['rank_rise_hit'])
        self.assertIsNone(item['heat_pct'])
        self.assertIsNone(item['heat_hit'])
        self.assertIsNone(item['z_score'])
        self.assertIsNone(item['search_index_ratio'])
        self.assertNotEqual(item['baseline_position'], 50)
        self.assertEqual(self.store.get('daily')['count'], 7)

    def reset(self):
        with self.store.db:
            self.store.db.execute('DELETE FROM hot_snapshots')

    def test_baseline_window_edges(self):
        current = [self.entry('合成话题甲', 5, 2_000_000, 'edge')]
        self.put(29 * 60, [self.entry('合成话题甲', 20, 1_000_000, 'edge')])
        self.put(0, current)
        self.assertIsNone(self.report()['baseline_fetched_at'])
        self.assertIsNone(self.by_word()['rank_rise'])
        self.assertFalse(self.by_word()['new_entry'])
        self.reset()
        self.put(30 * 60, [self.entry('合成话题甲', 20, 1_000_000, 'edge')])
        self.put(0, current)
        self.assertEqual(self.by_word()['rank_rise'], 15)
        self.reset()
        self.put(6 * 3600, [self.entry('合成话题甲', 8, 1_000_000, 'edge')])
        self.put(0, current)
        self.assertEqual(self.by_word()['baseline_position'], 8)
        self.reset()
        self.put(6 * 3600 + 1, [self.entry('合成话题甲', 8, 1_000_000, 'edge')])
        self.put(0, current)
        self.assertIsNone(self.report()['baseline_fetched_at'])
        self.reset()
        self.put(2 * 3600, [self.entry('合成话题甲', 11, 1_000_000, 'edge')])
        self.put(5 * 3600, [self.entry('合成话题甲', 40, 1_000_000, 'edge')])
        self.put(0, current)
        self.assertEqual(self.by_word()['baseline_position'], 11)

    def test_new_entry_uses_24_hour_identity(self):
        self.put(23 * 3600, [self.entry('合成话题甲', 3, 10, 'old')])
        self.put(0, [self.entry('合成话题甲', 3, 10, 'old'), self.entry('新话题', 9, 10, 'fresh')])
        self.assertFalse(self.by_word()['new_entry'])
        self.assertTrue(self.by_word('新话题')['new_entry'])
        self.store.db.execute('DELETE FROM hot_snapshots')
        self.store.db.commit()
        self.put(25 * 3600, [self.entry('合成话题甲', 3, 10, 'old')])
        self.put(0, [self.entry('合成话题甲', 3, 10, 'old')])
        self.assertTrue(self.by_word()['new_entry'])
        self.assertIsNone(self.by_word()['rank_rise'])

    def test_rank_and_heat_thresholds_and_missing_previous(self):
        self.put(3600, [
            self.entry('刚好命中', 25, 1_000_000, 'hit'),
            self.entry('名次不够', 26, 1_000_000, 'rank'),
            self.entry('升幅不够', 19, 1_000_000, 'small'),
            self.entry('热度比例不够', 2, 2_000_000, 'pct'),
            self.entry('热度增量不够', 3, 100_000, 'delta'),
            self.entry('热度缺失', 4, None, 'missing'),
            self.entry('热度为零', 5, 0, 'zero'),
        ])
        self.put(0, [
            self.entry('刚好命中', 15, 1_500_000, 'hit'),
            self.entry('名次不够', 16, 1_500_000, 'rank'),
            self.entry('升幅不够', 10, 1_500_000, 'small'),
            self.entry('热度比例不够', 2, 2_900_000, 'pct'),
            self.entry('热度增量不够', 3, 200_000, 'delta'),
            self.entry('热度缺失', 4, 1_500_000, 'missing'),
            self.entry('热度为零', 5, 1_500_000, 'zero'),
            self.entry('基线没有', 6, 1_500_000, 'absent'),
        ])
        hit = self.by_word('刚好命中')
        self.assertEqual(hit['rank_rise'], 10)
        self.assertTrue(hit['rank_rise_hit'])
        self.assertEqual(hit['heat_pct'], 0.5)
        self.assertEqual(hit['heat_delta'], 500000)
        self.assertTrue(hit['heat_hit'])
        self.assertFalse(self.by_word('名次不够')['rank_rise_hit'])
        self.assertEqual(self.by_word('升幅不够')['rank_rise'], 9)
        self.assertFalse(self.by_word('升幅不够')['rank_rise_hit'])
        self.assertFalse(self.by_word('热度比例不够')['heat_hit'])
        self.assertFalse(self.by_word('热度增量不够')['heat_hit'])
        self.assertIsNone(self.by_word('热度缺失')['heat_pct'])
        self.assertIsNone(self.by_word('热度缺失')['heat_hit'])
        self.assertIsNone(self.by_word('热度为零')['heat_pct'])
        absent = self.by_word('基线没有')
        self.assertIsNone(absent['rank_rise'])
        self.assertIsNone(absent['baseline_position'])
        self.assertNotEqual(absent['baseline_position'], 50)

    def test_z_score_requires_eight_prior_values_and_nonzero_spread(self):
        for index, heat in enumerate(range(1, 8), start=1):
            self.put(index * 3600, [self.entry('合成话题甲', 1, heat)])
        self.put(0, [self.entry('合成话题甲', 1, 100)])
        self.assertIsNone(self.by_word()['z_score'])
        self.assertIsNone(self.by_word()['z_hit'])
        self.put(8 * 3600, [self.entry('合成话题甲', 1, 8)])
        scored = self.by_word()
        self.assertIsNotNone(scored['z_score'])
        self.assertTrue(scored['z_hit'])
        self.store.db.execute('DELETE FROM hot_snapshots')
        self.store.db.commit()
        for index in range(1, 9):
            self.put(index * 3600, [self.entry('合成话题甲', 1, 5)])
        self.put(0, [self.entry('合成话题甲', 1, 50)])
        self.assertIsNone(self.by_word()['z_score'])
        self.store.db.execute('DELETE FROM hot_snapshots')
        self.store.db.commit()
        for index in range(1, 9):
            self.put(index * 3600, [self.entry('只有词', 1, index, sentence_id=None)])
        self.put(0, [self.entry('只有词', 1, 100, sentence_id=None)])
        self.assertIsNone(self.by_word('只有词')['z_score'])
        self.assertEqual(self.by_word('只有词')['key_kind'], 'word')

    def test_search_index_ratio_requires_both_calendar_days(self):
        self.put(0, [self.entry('合成话题甲', 1, 10), self.entry('有环比', 2, 10, 'ratio')])
        now = self.current.timestamp()
        with self.store.db:
            self.store.db.execute('INSERT INTO points VALUES(?,?,?,?,?,?,?,?,?)',
                                  ('douyin', 'search_index', '全国', '合成话题甲', '2026-11-03',
                                   encode(200), encode(999), now, 'gap'))
            self.store.db.execute('INSERT INTO points VALUES(?,?,?,?,?,?,?,?,?)',
                                  ('douyin', 'search_index', '全国', '合成话题甲', '2026-11-01',
                                   encode(50), encode(None), now, 'older'))
            self.store.db.execute('INSERT INTO points VALUES(?,?,?,?,?,?,?,?,?)',
                                  ('douyin', 'search_index', '全国', '有环比', '2026-11-03',
                                   encode(150), encode(999), now, 'latest'))
            self.store.db.execute('INSERT INTO points VALUES(?,?,?,?,?,?,?,?,?)',
                                  ('douyin', 'search_index', '全国', '有环比', '2026-11-02',
                                   encode(100), encode(1), now, 'previous'))
        missing = self.by_word()
        self.assertEqual(missing['search_index_latest'], 200)
        self.assertEqual(missing['search_index_previous_day'], '2026-11-02')
        self.assertIsNone(missing['search_index_previous'])
        self.assertIsNone(missing['search_index_ratio'])
        paired = self.by_word('有环比')
        self.assertEqual(paired['search_index_ratio'], 0.5)
        self.assertNotEqual(paired['search_index_previous'], 1)

    def test_thresholds_come_from_environment_then_settings(self):
        self.put(3600, [self.entry('合成话题甲', 20, 1_000_000)])
        self.put(0, [self.entry('合成话题甲', 10, 1_500_000)])
        with patch.dict(os.environ, {'INDEX_HUB_RISING_RANK_RISE': '11'}):
            self.assertFalse(self.by_word()['rank_rise_hit'])
            self.store.set('hot_rising_thresholds', {'rank_rise': 10, 'heat_pct': 'nope'})
            self.assertTrue(self.by_word()['rank_rise_hit'])
            self.assertEqual(self.report()['thresholds']['heat_pct'], 0.5)
            self.assertEqual(self.report()['thresholds']['rank_rise'], 10)
