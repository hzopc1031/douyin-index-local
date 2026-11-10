"""Offline synthetic Baidu hot-board tests."""
import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from baidu_hotlist_client import BaiduHotListClient, project_baidu_hot_list
from hotlist_client import HotListError, project_hot_list
from hot_rising import rising_report
from group_score import group_score
from hub_store import HubError, Store, TZ
from hub_web import Hub, PRINCIPAL_KEY, mount
from test_hotlist import response


def payload():
    return {'success': True, 'data': {'cards': [{'component': 'list', 'content': [
        {'word': '甲', 'index': 0, 'hotScore': '1200', 'show': ['discard']},
        {'query': '乙', 'index': 1}], 'topContent': [{'word': '甲', 'index': 0}]}]}}


def snapshot(tab='realtime'):
    return project_baidu_hot_list(payload(), tab, fetched_at='2026-11-04T13:00:00+08:00')


class ProjectionTests(unittest.TestCase):
    def test_projection(self):
        result = snapshot()
        self.assertEqual(result['board'], 'baidu_realtime')
        self.assertEqual([i['position'] for i in result['items']], [1, 2])
        self.assertEqual(result['items'][0]['hot_value'], 1200)
        self.assertIsNone(result['items'][1]['hot_value'])
        self.assertNotIn('show', result['items'][0])
        self.assertEqual(result['items'][0]['item_key'], 'word:甲')

    def test_incomplete(self):
        for change in ('heat', 'position', 'duplicate'):
            p = payload()
            rows = p['data']['cards'][0]['content']
            if change == 'heat': rows[0]['hotScore'] = 'bad'
            if change == 'position': rows[1]['index'] = 0
            if change == 'duplicate': rows[1]['query'] = '甲'
            with self.subTest(change=change), self.assertRaises(HotListError) as e:
                project_baidu_hot_list(p, 'realtime')
            self.assertEqual(e.exception.code, 'incomplete')

    def test_unique_pinned_rank_collision_is_omitted(self):
        p = payload()
        p['data']['cards'][0]['topContent'] = [{'word': '丙', 'index': 0}]
        items = project_baidu_hot_list(p, 'realtime')['items']
        self.assertEqual([(item['word'], item['position']) for item in items], [('甲', 1), ('乙', 2)])

    def test_category_null_pinned(self):
        p = payload()
        card = p['data']['cards'][0]
        card['component'] = 'textImgListVerticalNormal'
        card['content'][1]['hotScore'] = '800'
        card['topContent'] = None
        for tab in ('novel', 'movie', 'teleplay'):
            with self.subTest(tab=tab):
                result = project_baidu_hot_list(p, tab)
                self.assertEqual([(i['word'], i['position'], i['hot_value']) for i in result['items']],
                                 [('甲', 1, 1200), ('乙', 2, 800)])

    def test_pinned_list_shape(self):
        p = payload()
        card = p['data']['cards'][0]
        del card['topContent']
        self.assertEqual(len(project_baidu_hot_list(p, 'realtime')['items']), 2)
        for value in ('invalid', {}):
            card['topContent'] = value
            with self.subTest(value=value), self.assertRaises(HotListError) as e:
                project_baidu_hot_list(p, 'realtime')
            self.assertEqual(e.exception.code, 'incomplete')

    def test_invalid_content(self):
        for value in (None, [], 'invalid'):
            p = payload()
            p['data']['cards'][0]['content'] = value
            with self.subTest(value=value), self.assertRaises(HotListError) as e:
                project_baidu_hot_list(p, 'novel')
            self.assertEqual(e.exception.code, 'incomplete')

    def test_client(self):
        for body, status, code in [(payload(), 200, None), (b'', 302, 'redirect_rejected'),
                                   (b'', 403, 'access_denied'), (b'', 429, 'rate_limited'),
                                   (b'<html>', 200, 'response_format'), (b'x' * (2 * 1024 * 1024 + 1), 200, 'response_too_large')]:
            with self.subTest(code=code):
                session = requests.Session()
                calls = []
                def get(url, **kwargs):
                    calls.append((url, kwargs))
                    return response(body, status)
                session.get = get
                client = BaiduHotListClient(session)
                if code:
                    with self.assertRaises(HotListError) as e: client.fetch('realtime')
                    self.assertEqual(e.exception.code, code)
                else: client.fetch('realtime')
                self.assertEqual(len(calls), 1)
                self.assertFalse(session.trust_env)
                self.assertEqual(session.adapters['https://'].max_retries.total, 0)
                self.assertFalse(calls[0][1]['allow_redirects'])
                self.assertTrue(calls[0][1]['verify'])
                self.assertNotIn('Cookie', calls[0][1]['headers'])
                self.assertFalse(session.cookies)
                session.close()


class StoreTests(unittest.TestCase):
    def test_isolation_and_fusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'hub.sqlite3')
            self.addCleanup(store.close)
            dy = project_hot_list({'status_code': 0, 'data': {'word_list': [{'word': '甲', 'position': 1, 'hot_value': 50}]}})
            dy['fetched_at'] = '2026-11-04T12:00:00+08:00'
            store.save_hot_snapshot(dy)
            group = store.create_group('组', ['甲'])
            before = group_score(store, group)
            store.save_hot_snapshot(snapshot())
            self.assertEqual(group_score(store, group), before)
            self.assertEqual(len(store.latest_hot()['items']), 1)
            self.assertEqual(len(store.latest_hot('baidu_realtime')['items']), 2)
            self.assertEqual(len(store.hot_history(word='甲', board='baidu_realtime')['points']), 1)
            self.assertEqual(len(rising_report(store, 'baidu_realtime')['items']), 2)
            self.assertEqual(len(rising_report(store)['items']), 1)
            with self.assertRaises(HubError): store.save_hot_snapshot({**snapshot(), 'complete': False})
            with self.assertRaises(HubError): store.save_hot_snapshot({**snapshot(), 'board': 'baidu_../x'})
            store.close()


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cycles_and_isolation(self):
        with tempfile.TemporaryDirectory() as tmp, patch('hub_web.state_dir', return_value=Path(tmp)):
            hub = Hub(tmp, bridge=lambda *args: {})
            self.addCleanup(hub.store.close)
            class Client:
                def __init__(self): self.calls = []; self.errors = {}
                def fetch(self, tab):
                    self.calls.append(tab)
                    if tab in self.errors: raise HotListError(self.errors[tab], 'synthetic')
                    return snapshot(tab)
            client = Client()
            now = 1800000000
            day = dt.datetime.fromtimestamp(now, TZ).date().isoformat()
            hub.store.set('hot_daily', {'date': day, 'count': 3})
            hub.store.set('daily', {'date': day, 'count': 7})
            with patch.dict(os.environ, {'INDEX_HUB_BAIDU_HOTLIST_ENABLED': '0'}):
                self.assertEqual((await hub.run_baidu_hot_once(client=client, now=now))['code'], 'disabled')
            self.assertEqual(client.calls, [])
            with patch.dict(os.environ, {'INDEX_HUB_BAIDU_HOTLIST_ENABLED': '1', 'INDEX_HUB_BAIDU_HOTLIST_TABS': 'realtime,novel,movie'}):
                client.errors = {'novel': 'incomplete'}
                with patch('hub_web.logger.warning') as warning:
                    self.assertEqual((await hub.run_baidu_hot_once(client=client, now=now))['stored'], 2)
                warning.assert_called_once_with('baidu hotlist stopped code=%s message=%s', 'incomplete', 'synthetic')
                self.assertEqual(client.calls, ['realtime', 'novel', 'movie'])
                self.assertIsNone(hub.store.get('baidu_hot_paused'))
                self.assertEqual(hub.store.latest_hot('baidu_novel')['items'], [])
                self.assertEqual((await hub.run_baidu_hot_once(client=client, now=now+1))['code'], 'interval')
                client.errors = {'realtime': 'rate_limited'}
                await hub.run_baidu_hot_once(client=client, now=now+3600)
                self.assertEqual(hub.store.get('baidu_hot_paused')['code'], 'rate_limited')
                self.assertIsNone(hub.store.get('hot_paused'))
                self.assertEqual(hub.store.get('hot_daily')['count'], 3)
                self.assertEqual(hub.store.get('daily')['count'], 7)
                hub.store.set('baidu_hot_paused', None)
                hub.store.set('baidu_hot_daily', {'date': day, 'count': 48})
                self.assertEqual((await hub.run_baidu_hot_once(client=client, now=now+7200))['code'], 'daily_limit')


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp, patch('hub_web.state_dir', return_value=Path(tmp)), patch.dict(os.environ, {'INDEX_HUB_BAIDU_HOTLIST_ENABLED': '0'}):
            hub = Hub(tmp, bridge=lambda *args: {'ok': True, 'state': 'browser_ready'})
            @web.middleware
            async def auth(request, handler):
                request[PRINCIPAL_KEY] = {'admin': True, 'scopes': ['read', 'query']}
                try: return await handler(request)
                except HubError as exc: return web.json_response({'code': exc.code}, status=exc.status)
            app = web.Application(middlewares=[auth])
            mount(app, hub)
            app.on_startup.clear()
            app.on_cleanup.clear()
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                hub.store.save_hot_snapshot(snapshot())
                r = await client.get('/api/v1/hot/latest?board=baidu_realtime')
                self.assertEqual((await r.json())['board'], 'baidu_realtime')
                self.assertEqual((await client.get('/api/v1/hot/latest?board=bad')).status, 400)
                r = await client.get('/api/v1/status')
                data = await r.json()
                self.assertFalse(data['baidu_hotlist']['enabled'])
                self.assertIn('hotlist', data)
                hub.store.set('hot_paused', {'code': 'a'})
                hub.store.set('baidu_hot_paused', {'code': 'b'})
                self.assertEqual((await client.post('/api/v1/hot/resume', json={'platform': 'baidu'}, headers={'X-Hub-Request': '1'})).status, 200)
                self.assertIsNone(hub.store.get('baidu_hot_paused'))
                self.assertIsNotNone(hub.store.get('hot_paused'))
                hub.store.set('baidu_hot_paused', {'code': 'b'})
                self.assertEqual((await client.post('/api/v1/hot/resume', headers={'X-Hub-Request': '1'})).status, 200)
                self.assertIsNone(hub.store.get('hot_paused'))
                self.assertIsNotNone(hub.store.get('baidu_hot_paused'))
            finally:
                await client.close()
                hub.store.close()
