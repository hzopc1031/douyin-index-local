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
from topic_board import TOPIC_NOTE, snapshot_is_fresh


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

    async def test_empty_board_is_not_disabled_and_does_not_collect(self):
        self.hub.store.set('daily', {'date': '2026-11-04', 'count': 4})
        self.assertEqual((await self.client.get('/api/v1/topics')).status, 401)
        response = await self.client.get('/api/v1/topics', headers=self.reader)
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload['status'], 'available')
        self.assertEqual(payload['status_label'], '尚未采集')
        self.assertFalse(payload['enabled'])
        self.assertEqual(payload['items'], [])
        self.assertEqual(payload['window_label'], '近1天')
        self.assertEqual(payload['rank_label'], '总榜')
        self.assertEqual(payload['categories'], ['全部'])
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
        self.assertIsNone(self.hub.topic_task)

    async def test_collect_once_reuses_a_fresh_snapshot_and_rejects_all(self):
        self.hub.store.set('topic_categories', [{'id': '11', 'name': '美食'}])
        self.hub.store.save_topic_snapshot({
            'complete': True, 'rank': 'total', 'window': '1d', 'category': '美食',
            'source': 'https://douhot.douyin.com/douhot/v1/material/challenge_billboard',
            'fetched_at': '2026-11-05T12:00:00+08:00',
            'items': [{'title': '已存话题', 'position': 1, 'hot_value': None, 'publish_count': None}]})
        calls = []

        def bridge(*_args):
            calls.append(True)
            return {'ok': True, 'requested': True, 'items': [], 'item_count': 0, 'fields': []}

        self.hub.bridge = bridge
        admin = {'Authorization': 'Basic ' + base64.b64encode(b'fixture:admin-fixture-only').decode(),
                 'X-Hub-Request': '1'}
        with patch('hub_web.snapshot_is_fresh', return_value=True):
            cached = await self.client.post('/api/v1/topics/collect-once', headers=admin,
                                            json={'rank': 'total', 'category': '美食'})
        self.assertEqual(cached.status, 200)
        self.assertEqual((await cached.json())['status'], 'cached')
        self.assertEqual((await cached.json())['live_request'], False)
        self.assertEqual(calls, [])
        self.hub._topic_inflight.add(('total', '美食', '1d'))
        with patch('hub_web.snapshot_is_fresh', return_value=False):
            busy = await self.client.post('/api/v1/topics/collect-once', headers=admin,
                                          json={'rank': 'total', 'category': '美食'})
        self.assertEqual((await busy.json())['status'], 'in_flight')
        self.assertEqual(calls, [])
        self.hub._topic_inflight.clear()
        refused = await self.client.post('/api/v1/topics/collect-once', headers=admin,
                                         json={'rank': 'total', 'category': '全部'})
        self.assertEqual(refused.status, 400)
        self.assertEqual(calls, [])

class TopicLoginTests(unittest.TestCase):
    def test_page_state_and_expired_qr_do_not_read_cookies(self):
        from app import Api
        from types import SimpleNamespace
        import threading
        api = Api.__new__(Api)
        api._lock = threading.Lock()
        class Window:
            native = SimpleNamespace(webview=object())
            url = 'https://open.douyin.com/authorize'
            text = '使用抖音账号登录 生活服务热点中心 二维码失效'
            def get_current_url(self): return self.url
            def evaluate_js(self, _script): return json.dumps({'ready': 'complete', 'text': self.text})
            def get_cookies(self): raise AssertionError('cookie access')
        window = Window()
        api._topic_window = lambda: window
        self.assertEqual(api.topic_login('status'), {'state': 'need_scan', 'qr_available': False})
        window.url, window.text = 'https://douhot.douyin.com/', '热点宝 首页'
        self.assertEqual(api.topic_login('status')['state'], 'logged_in')
        window.url = 'https://evil.example/'
        self.assertEqual(api.topic_login('status')['state'], 'browser_unavailable')


class TopicLoginApiTests(ApiTests):
    async def test_login_and_categories_guards_and_redaction(self):
        admin = {'Authorization': 'Basic ' + base64.b64encode(b'fixture:admin-fixture-only').decode(),
                 'X-Hub-Request': '1'}
        calls = []
        def bridge(command, payload, timeout):
            calls.append((command, payload))
            if command == 'topic_login':
                return {'state': 'need_scan', 'qr_available': True, 'cookie': 'secret-fixture',
                        'png_base64': base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()}
            return {'ok': True, 'requested': True, 'tags': [{'id': '11', 'name': '美食'}], 'item_count': 1,
                    'cookie': 'secret-fixture'}
        self.hub.bridge = bridge
        self.assertEqual((await self.client.get('/api/v1/topics/login', headers=self.reader)).status, 403)
        response = await self.client.get('/api/v1/topics/login', headers=admin)
        self.assertEqual(await response.json(), {'state': 'need_scan', 'qr_available': True})
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        qr = await self.client.get('/api/v1/topics/login/qr', headers=admin)
        self.assertEqual(await qr.read(), b'\x89PNG\r\n\x1a\nfixture')
        no_csrf = dict(admin); no_csrf.pop('X-Hub-Request')
        self.assertEqual((await self.client.post('/api/v1/topics/login/refresh', headers=no_csrf, json={})).status, 403)
        self.assertEqual((await self.client.post('/api/v1/topics/login/refresh', headers=admin, json={})).status, 200)
        result = await self.client.post('/api/v1/topics/categories/refresh', headers=admin, json={})
        self.assertEqual((await result.json())['status'], 'ok')
        self.assertEqual(self.hub.store.get('topic_categories')[0]['name'], '美食')
        self.assertNotIn('secret-fixture', json.dumps(self.hub.store.get('topic_categories')))
        self.assertEqual((await self.client.post('/api/v1/topics/categories/refresh', headers=admin, json={})).status, 429)
        self.assertEqual(sum(command == 'topic' for command, _ in calls), 1)
        self.hub.store.set('topic_paused', {'code': 'not_logged_in'})
        self.assertEqual((await self.client.post('/api/v1/topics/categories/refresh', headers=admin, json={})).status, 409)

class TopicRequestConcurrencyTests(ApiTests):
    async def test_concurrent_tags_and_board_share_gate(self):
        import threading
        started, release = threading.Event(), threading.Event()
        calls = []
        def bridge(command, payload, timeout):
            calls.append(payload['action'])
            started.set()
            release.wait(2)
            return {'ok': True, 'requested': True, 'tags': [], 'item_count': 0}
        self.hub.bridge = bridge
        first = asyncio.create_task(self.hub.topic_request({'action': 'tags'}))
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        second = asyncio.create_task(self.hub.topic_request({'action': 'board'}))
        release.set()
        self.assertTrue((await first)['ok'])
        with self.assertRaises(HubError) as error:
            await second
        self.assertEqual(error.exception.code, 'interval')
        self.assertEqual(calls, ['tags'])
