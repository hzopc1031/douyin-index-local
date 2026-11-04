"""Hotspot topic board skeleton. No live collection and no invented rows."""
import asyncio
import base64
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from desktop_gateway import HUB_KEY, create_app
from hub_store import HubError, Store
from topic_board import TOPIC_NOTE


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'hub.sqlite3')
        self.addCleanup(self.store.close)
        self.store.set('daily', {'date': '2026-11-04', 'count': 4})

    def snapshot(self, **overrides):
        body = {'complete': True, 'rank': 'total', 'window': '1d', 'category': '美食',
                'source': 'https://douhot.douyin.com/ranking',
                'fetched_at': '2026-11-04T12:00:00+08:00',
                'items': [{'title': '合成话题', 'position': 1, 'hot_value': None},
                          {'title': '另一话题', 'position': 2, 'hot_value': 80}]}
        body.update(overrides)
        return body

    def test_null_heat_stays_null_and_categories_do_not_mix(self):
        self.store.save_topic_snapshot(self.snapshot())
        food = self.store.latest_topics(rank='total', window='1d', category='美食')
        self.assertIsNone(food['items'][0]['hot_value'])
        self.assertEqual(food['items'][1]['hot_value'], 80)
        self.assertEqual(food['items'][0]['title'], '合成话题')
        empty = self.store.latest_topics(rank='total', window='1d', category='全部')
        self.assertEqual(empty['items'], [])
        self.assertIsNone(empty['fetched_at'])
        rising = self.store.latest_topics(rank='rising', window='1d', category='美食')
        self.assertEqual(rising['items'], [])
        self.assertEqual(self.store.get('daily')['count'], 4)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM points').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM hot_snapshots').fetchone()[0], 0)

    def test_incomplete_snapshot_is_rejected(self):
        with self.assertRaises(HubError):
            self.store.save_topic_snapshot(self.snapshot(items=[]))
        with self.assertRaises(HubError):
            self.store.save_topic_snapshot(self.snapshot(items=[{'title': '缺热度当零', 'position': 1, 'hot_value': -1}]))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM topic_snapshots').fetchone()[0], 0)


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
        self.reader = {'Authorization': 'Bearer ' + self.hub.store.issue_key('Reader', ['read'])['token']}

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_empty_board_is_disabled_and_does_not_collect(self):
        self.hub.store.set('daily', {'date': '2026-11-04', 'count': 4})
        self.assertEqual((await self.client.get('/api/v1/topics')).status, 401)
        response = await self.client.get('/api/v1/topics', headers=self.reader)
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload['status'], 'disabled')
        self.assertEqual(payload['status_label'], '未启用')
        self.assertEqual(payload['items'], [])
        self.assertEqual(payload['window_label'], '近1天')
        self.assertEqual(payload['rank_label'], '总榜')
        self.assertIn('美食', payload['categories'])
        self.assertFalse(payload['live_request'])
        self.assertIn('不伪造', payload['note'])
        self.assertIn('不是搜索指数', TOPIC_NOTE)
        self.assertEqual((await self.client.get('/api/v1/topics?window=7d', headers=self.reader)).status, 400)
        self.assertEqual((await self.client.get('/api/v1/topics?category=不存在', headers=self.reader)).status, 400)
        self.assertEqual(self.hub.store.get('daily')['count'], 4)
        self.assertEqual(self.hub.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        schema = await (await self.client.get('/api/v1/openapi.json', headers=self.reader)).json()
        self.assertIn('/api/v1/topics', schema['paths'])

    async def test_enabled_without_login_stays_empty_until_a_real_snapshot_exists(self):
        with patch.dict(os.environ, {'INDEX_HUB_TOPIC_ENABLED': '1'}):
            waiting = await (await self.client.get('/api/v1/topics?rank=rising', headers=self.reader)).json()
        self.assertEqual(waiting['status'], 'not_logged_in')
        self.assertEqual(waiting['status_label'], '未登录')
        self.assertEqual(waiting['items'], [])
        self.assertEqual(waiting['rank'], 'rising')
        self.hub.store.save_topic_snapshot({
            'complete': True, 'rank': 'rising', 'window': '1d', 'category': '全部',
            'source': 'pending-qt-login', 'fetched_at': '2026-11-04T08:00:00+08:00',
            'items': [{'title': '已存话题', 'position': 1, 'hot_value': None}]})
        stored = await (await self.client.get(
            '/api/v1/topics?rank=rising&category=全部&window=1d', headers=self.reader)).json()
        self.assertEqual(stored['status'], 'available')
        self.assertEqual(stored['items'][0]['title'], '已存话题')
        self.assertIsNone(stored['items'][0]['hot_value'])
        self.assertEqual(self.hub.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
