"""Synthetic keyword-group scores. No live collection and no quota use."""
import asyncio
import base64
import contextlib
import datetime as dt
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from desktop_gateway import HUB_KEY, create_app
from group_score import group_score
from hotlist_client import HOT_LIST_URL
from hub_store import HubError, Store, TZ, encode


def entry(word, position, hot, sentence_id=None):
    if sentence_id:
        key, kind = 'id:' + sentence_id, 'sentence_id'
    else:
        key, kind = 'word:' + word, 'word'
    return {'item_key': key, 'key_kind': kind, 'sentence_id': sentence_id, 'word': word,
            'position': position, 'hot_value': hot, 'label': None, 'event_time': None,
            'video_count': None, 'sentence_tag': None, 'word_type': None, 'group_id': None}


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'hub.sqlite3')
        self.addCleanup(self.store.close)
        self.store.set('daily', {'date': '2026-11-04', 'count': 7})
        self.group = self.store.create_group('合成组', ['甲', '乙'])

    def point(self, word, day, value, composite=999):
        with self.store.db:
            self.store.db.execute('INSERT INTO points VALUES(?,?,?,?,?,?,?,?,?)',
                                  ('douyin', 'search_index', '全国', word, day, encode(value),
                                   encode(composite), time.time(), 'synthetic'))

    def board(self, items, when='2026-11-04T12:00:00+08:00'):
        self.store.save_hot_snapshot({'complete': True, 'board': 'word_list', 'source': HOT_LIST_URL,
                                      'fetched_at': when, 'items': items})

    def score(self):
        return group_score(self.store, self.group)

    def test_three_scores_stay_separate_and_do_not_write_composite_or_quota(self):
        self.point('甲', '2026-11-01', 10)
        self.point('乙', '2026-11-01', 10)
        self.point('甲', '2026-11-03', 30)
        self.point('乙', '2026-11-03', 10)
        self.board([entry('甲', 1, 100, 'a'), entry('乙', 2, 50, 'b')])
        before = self.store.db.execute('SELECT composite FROM points').fetchall()
        report = self.score()
        self.assertEqual(report['search']['score'], 200)
        self.assertEqual(report['search']['baseline_raw'], 20)
        self.assertEqual(report['hot']['score'], 75)
        self.assertEqual(report['fusion']['score'], 162.5)
        self.assertEqual(report['weights'], {'search': 0.7, 'hot': 0.3})
        self.assertIn('不是搜索次数', report['note'])
        self.assertIn('不是抖音综合指数', report['note'])
        self.assertEqual(self.store.db.execute('SELECT composite FROM points').fetchall(), before)
        self.assertEqual(self.store.get('daily')['count'], 7)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)

    def test_missing_search_day_hot_absence_and_substring_are_not_zero(self):
        self.point('甲', '2026-11-03', 30, composite=999)
        self.point('乙', '2026-11-01', 10)
        self.board([entry('甲', 1, 80, 'a'), entry('西湖醋鱼', 2, 40, 'fish')])
        report = self.score()
        self.assertIsNone(report['search']['score'])
        self.assertEqual(report['search']['missing'], [{'day': '2026-11-03', 'keyword': '乙'}])
        self.assertIsNone(report['hot']['score'])
        absent = next(item for item in report['hot']['members'] if item['keyword'] == '乙')
        self.assertEqual(absent['status'], '未上榜')
        self.assertIsNone(absent['ratio'])
        self.assertEqual(report['hot']['substring_hits'], [])
        self.assertIsNone(report['fusion']['score'])
        fish = self.store.create_group('子串', ['西湖'])
        substring = group_score(self.store, fish)
        self.assertEqual(substring['hot']['members'][0]['status'], '未上榜')
        self.assertEqual(substring['hot']['substring_hits'][0]['word'], '西湖醋鱼')
        self.assertIsNone(substring['hot']['score'])

    def test_baseline_zero_and_missing_snapshot_do_not_invent_status(self):
        self.point('甲', '2026-11-01', 0)
        self.point('乙', '2026-11-01', 0)
        self.point('甲', '2026-11-03', 10)
        self.point('乙', '2026-11-03', 10)
        report = self.score()
        self.assertIsNone(report['search']['score'])
        self.assertEqual(report['search']['reason'], 'baseline_zero')
        self.assertEqual(report['search']['raw'], 20)
        self.assertEqual(report['hot']['snapshot_status'], 'missing')
        self.assertTrue(all(item['status'] is None for item in report['hot']['members']))
        self.assertNotIn('未上榜', json.dumps(report['hot']['members']))
        self.assertIsNone(report['fusion']['score'])

    def test_exact_null_heat_is_not_counted_as_off_board(self):
        self.board([entry('甲', 1, None, 'a'), entry('乙', 2, 40, 'b')])
        report = self.score()
        self.assertEqual(report['hot']['members'][0]['status'], '热度缺失')
        self.assertIsNone(report['hot']['score'])

    def test_weights_are_configurable(self):
        self.point('甲', '2026-11-01', 10)
        self.point('乙', '2026-11-01', 10)
        self.board([entry('甲', 1, 100, 'a'), entry('乙', 2, 50, 'b')])
        with patch.dict(os.environ, {'INDEX_HUB_GROUP_SEARCH_WEIGHT': '0.5', 'INDEX_HUB_GROUP_HOT_WEIGHT': '0.5'}):
            self.assertEqual(self.score()['fusion']['score'], 87.5)
            self.store.set('group_score_weights', {'search': 1, 'hot': 0})
            self.assertEqual(self.score()['fusion']['score'], 100)
            self.assertEqual(self.score()['weights']['hot'], 0)

    def test_update_replaces_keywords_without_new_group_or_collection(self):
        created = self.group['created']
        updated = self.store.update_group(self.group['id'], '改名', ['甲', '丙'])
        self.assertEqual(updated['id'], self.group['id'])
        self.assertEqual(updated['created'], created)
        self.assertEqual(updated['name'], '改名')
        self.assertEqual(updated['keywords'], ['甲', '丙'])
        self.assertEqual(len(self.store.list_groups()), 1)
        self.point('甲', '2026-11-03', 30)
        report = group_score(self.store, self.store.get_group(self.group['id']))
        self.assertIsNone(report['search']['score'])
        self.assertEqual(report['search']['missing'], [{'day': '2026-11-03', 'keyword': '丙'}])
        self.assertIsNone(report['hot']['score'])
        self.assertIsNone(report['fusion']['score'])
        self.assertEqual(self.store.get('daily')['count'], 7)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        with self.assertRaises(HubError) as caught:
            self.store.update_group('c' * 32, '无', ['甲'])
        self.assertEqual(caught.exception.status, 404)

    def test_existing_database_creates_group_table(self):
        path = Path(self.temp.name) / 'old.sqlite3'
        sqlite3.connect(path).close()
        store = Store(path)
        self.addCleanup(store.close)
        store.create_group('迁移', ['甲'])
        self.assertEqual(store.list_groups()[0]['keywords'], ['甲'])


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
        reader = self.hub.store.issue_key('Reader', ['read'])
        querier = self.hub.store.issue_key('Querier', ['read', 'query'])
        self.reader = {'Authorization': 'Bearer ' + reader['token']}
        self.querier = {'Authorization': 'Bearer ' + querier['token']}

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_permissions_and_score_without_collection(self):
        self.hub.store.set('daily', {'date': '2026-11-04', 'count': 7})
        body = {'name': '合成组', 'keywords': ['甲', '乙']}
        self.assertEqual((await self.client.post('/api/v1/groups', json=body)).status, 401)
        self.assertEqual((await self.client.post('/api/v1/groups', headers=self.reader, json=body)).status, 403)
        self.assertEqual((await self.client.post(
            '/api/v1/groups', headers={'Authorization': self.admin['Authorization']}, json=body)).status, 403)
        created = await self.client.post('/api/v1/groups', headers=self.querier, json=body)
        self.assertEqual(created.status, 201)
        group = (await created.json())['group']
        self.assertEqual((await self.client.get('/api/v1/groups', headers=self.reader)).status, 200)
        score = await self.client.get('/api/v1/groups/' + group['id'] + '/score', headers=self.reader)
        self.assertEqual(score.status, 200)
        payload = await score.json()
        self.assertFalse(payload['live_request'])
        self.assertIsNone(payload['fusion']['score'])
        self.assertEqual(payload['hot']['snapshot_status'], 'missing')
        self.assertEqual(self.hub.store.get('daily')['count'], 7)
        self.assertEqual(self.hub.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        self.assertEqual((await self.client.delete('/api/v1/groups/' + group['id'], headers=self.reader)).status, 403)
        self.assertEqual((await self.client.delete('/api/v1/groups/' + group['id'], headers=self.querier)).status, 200)
        self.assertEqual((await self.client.get('/api/v1/groups/' + group['id'] + '/score', headers=self.reader)).status, 404)
        schema = await (await self.client.get('/api/v1/openapi.json', headers=self.reader)).json()
        self.assertIn('/api/v1/groups', schema['paths'])
        self.assertIn('/api/v1/groups/{id}/score', schema['paths'])
        self.assertIn('put', schema['paths']['/api/v1/groups/{id}'])
        self.assertIn('delete', schema['paths']['/api/v1/groups/{id}'])

    async def test_put_updates_keywords_and_score_without_collection(self):
        self.hub.store.set('daily', {'date': '2026-11-04', 'count': 3})
        created = await self.client.post(
            '/api/v1/groups', headers=self.querier, json={'name': '合成组', 'keywords': ['甲', '乙']})
        self.assertEqual(created.status, 201)
        group = (await created.json())['group']
        identifier = group['id']
        now = time.time()
        with self.hub.store.db:
            for word, value in (('甲', 10), ('乙', 10), ('丙', 30)):
                self.hub.store.db.execute(
                    'INSERT INTO points VALUES(?,?,?,?,?,?,?,?,?)',
                    ('douyin', 'search_index', '全国', word, '2026-11-03',
                     encode(value), encode(None), now, 'synthetic'))
        body = {'name': '合成组', 'keywords': ['甲', '丙']}
        self.assertEqual((await self.client.put('/api/v1/groups/' + identifier, json=body)).status, 401)
        self.assertEqual((await self.client.put(
            '/api/v1/groups/' + identifier, headers=self.reader, json=body)).status, 403)
        self.assertEqual((await self.client.put(
            '/api/v1/groups/' + identifier,
            headers={'Authorization': self.admin['Authorization']}, json=body)).status, 403)
        updated = await self.client.put('/api/v1/groups/' + identifier, headers=self.querier, json=body)
        self.assertEqual(updated.status, 200)
        payload = await updated.json()
        self.assertEqual(payload['group']['id'], identifier)
        self.assertEqual(payload['group']['created'], group['created'])
        self.assertEqual(payload['group']['keywords'], ['甲', '丙'])
        self.assertFalse(payload['live_request'])
        self.assertIn('未提交搜索指数任务', payload['note'])
        listed = await (await self.client.get('/api/v1/groups', headers=self.reader)).json()
        self.assertEqual([item['keywords'] for item in listed['groups']], [['甲', '丙']])
        score = await (await self.client.get(
            '/api/v1/groups/' + identifier + '/score', headers=self.reader)).json()
        self.assertEqual(score['search']['raw'], 40)
        self.assertEqual(score['search']['score'], 100)
        self.assertIsNone(score['hot']['score'])
        self.assertIsNone(score['fusion']['score'])
        self.assertEqual(self.hub.store.get('daily')['count'], 3)
        self.assertEqual(self.hub.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        missing = await self.client.put('/api/v1/groups/' + ('b' * 32), headers=self.querier, json=body)
        self.assertEqual(missing.status, 404)
        rejected = await self.client.put(
            '/api/v1/groups/' + identifier, headers=self.querier,
            json={'name': '合成组', 'keywords': ['甲'], 'extra': 1})
        self.assertEqual(rejected.status, 400)
        self.assertEqual((await self.client.delete(
            '/api/v1/groups/' + identifier, headers=self.querier)).status, 200)
        self.assertEqual((await self.client.get(
            '/api/v1/groups/' + identifier + '/score', headers=self.reader)).status, 404)
