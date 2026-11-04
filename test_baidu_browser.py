import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from baidu_browser import prepare_profile
from browser_profile import ProfileError


class ProfileTests(unittest.TestCase):
    def test_profile_permissions_and_rejected_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {'INDEX_HUB_BAIDU_PROFILE_DIR': str(root / 'profile')}):
                path = prepare_profile(root)
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
                with patch('baidu_browser.os.geteuid', return_value=-1), self.assertRaises(ProfileError):
                    prepare_profile(root)
            (root / 'link').symlink_to(root / 'profile', target_is_directory=True)
            for path in (root / 'link' / 'child', root / '.runtime' / 'profile', root / 'results' / 'profile', root / '.cloud-state' / 'profile', root):
                with self.subTest(path=path), patch.dict(os.environ, {'INDEX_HUB_BAIDU_PROFILE_DIR': str(path)}):
                    with self.assertRaises(ProfileError):
                        prepare_profile(root)


class PageTransportTests(unittest.TestCase):
    def test_origin_is_fixed_and_page_must_match(self):
        from baidu_browser import PageTransport
        from baidu_adapter import BaiduError
        class Page:
            url = 'https://example.com/'
        with self.assertRaises(ValueError):
            PageTransport(Page(), origin='http://127.0.0.1:1234')
        with self.assertRaises(BaiduError):
            PageTransport(Page()).query({})


@unittest.skipUnless(os.environ.get('INDEX_HUB_REQUIRE_BROWSER_TESTS') == '1', 'browser tests not requested')
class BrowserPageTests(unittest.TestCase):
    def test_five_words_real_browser(self):
        import threading
        from playwright.sync_api import sync_playwright
        from baidu_browser import PageTransport
        from fake_baidu_page import serve
        server = serve()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = 'http://127.0.0.1:' + str(server.server_port)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=False)
                page = browser.new_page()
                page.goto(origin)
                from baidu_browser import page_status, login_open
                self.assertEqual(page_status(page, origin=origin, allow_loopback=True)['state'], 'login_required')
                page.evaluate("() => { const b = document.createElement('button'); b.textContent = '登录'; b.onclick = () => b.textContent = '退出登录'; document.body.append(b); }")
                with patch('baidu_browser._page', return_value=page):
                    login_open(None)
                self.assertEqual(page_status(page, origin=origin, allow_loopback=True)['state'], 'browser_ready')
                query = dict(keywords=['a', 'b', 'c', 'd', 'e'], start_date='2026-10-01', end_date='2026-10-02')
                rows = PageTransport(page, origin=origin, allow_loopback=True).query(query)
                self.assertEqual(len(rows), 30)
                self.assertEqual([r['search_index'] for r in rows[:2]], [1, 2])
                self.assertNotIn('secret', repr(rows))
                from baidu_adapter import BaiduError, collect_page
                server.mode = 'non_day'
                with self.assertRaises(BaiduError) as caught:
                    PageTransport(page, origin=origin, allow_loopback=True).query(query)
                self.assertEqual(caught.exception.code, 'response_schema')
                server.mode = 'missing'
                missing = PageTransport(page, origin=origin, allow_loopback=True).query(query)
                self.assertTrue(all(r['search_index'] is None for r in missing))
                server.mode = ''
                before = server.index_calls
                waits = []
                long_query = dict(platform='baidu', keywords=['synthetic'], start_date='2025-10-01', end_date='2026-10-01')
                collected = collect_page(long_query, PageTransport(page, origin=origin, allow_loopback=True), chunk_interval=8, sleep=waits.append)
                self.assertEqual(len(collected['rows']), 366 * 3)
                self.assertEqual(server.index_calls - before, 2)
                self.assertEqual(waits, [8])
                for mode, code in [('10000','login_required'), ('10001','rate_limited'), ('10018','abnormal_access'), ('10002','bad_request'), ('html','verification_required'), ('large','response_schema'), ('redirect','network_error'), ('duplicate','response_schema'), ('timeout','network_error')]:
                    server.mode = mode
                    with self.subTest(mode=mode), self.assertRaises(BaiduError) as caught:
                        PageTransport(page, origin=origin, allow_loopback=True).query(query)
                    self.assertEqual(caught.exception.code, code)
                page.evaluate('delete window.Paris')
                with self.assertRaises(BaiduError) as caught:
                    PageTransport(page, origin=origin, allow_loopback=True).query(query)
                self.assertEqual(caught.exception.code, 'signature_unavailable')
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


@unittest.skipUnless(os.environ.get('INDEX_HUB_REQUIRE_BROWSER_TESTS') == '1', 'browser tests not requested')
class BrowserLifetimeTests(unittest.TestCase):
    def test_lazy_idle_close_and_owner_thread(self):
        import threading
        import time
        from baidu_browser import BaiduBrowser
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'INDEX_HUB_BAIDU_PROFILE_DIR': str(Path(tmp) / 'profile')}):
            browser = BaiduBrowser(root=tmp, idle_seconds=1)
            try:
                self.assertFalse(browser.started)
                owner = browser.call(lambda ctx: (threading.get_ident(), len(ctx.pages)))
                self.assertNotEqual(owner[0], threading.get_ident())
                for _ in range(3):
                    self.assertEqual(owner[0], browser.call(lambda ctx: threading.get_ident(), touch=False))
                    time.sleep(0.2)
                time.sleep(1)
                self.assertFalse(browser.started)
                self.assertEqual((Path(tmp) / 'profile').stat().st_mode & 0o777, 0o700)
            finally:
                browser.close()


@unittest.skipUnless(os.environ.get('INDEX_HUB_REQUIRE_BROWSER_TESTS') == '1', 'browser tests not requested')
class BridgeLeakTests(unittest.TestCase):
    def test_browser_through_bridge_and_store(self):
        import json
        import logging
        import threading
        from unittest.mock import Mock
        import baidu_browser
        import hub_bridge
        from hub_store import Store
        from fake_baidu_page import serve
        server = serve()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = 'http://127.0.0.1:' + str(server.server_port)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'INDEX_HUB_BAIDU_PROFILE_DIR': str(Path(tmp) / 'profile')}):
            worker = baidu_browser.BaiduBrowser(root=tmp, idle_seconds=30)
            worker.last_state = 'browser_ready'
            transport = baidu_browser.PageTransport
            def page_for(context):
                page = context.pages[0]
                if not page.url.startswith(origin):
                    page.goto(origin)
                return page
            sock = Path(tmp) / 'bridge.sock'
            with patch.object(baidu_browser, '_browser', worker), patch.object(baidu_browser, '_page', page_for), \
                 patch.object(baidu_browser, 'status', return_value={'state':'browser_ready'}), \
                 patch.object(baidu_browser, 'PageTransport', side_effect=lambda page: transport(page, origin=origin, allow_loopback=True)), \
                 patch.object(hub_bridge, '_socket_path', return_value=sock):
                bridge = hub_bridge.start_bridge(Mock())
                try:
                    query = dict(platform='baidu', keywords=['synthetic'], start_date='2026-10-01', end_date='2026-10-02')
                    answer = hub_bridge.call_bridge('baidu_query', query)
                    self.assertTrue(answer['ok'])
                    store = Store(Path(tmp) / 'test.sqlite')
                    store.ingest(answer)
                    dump = '\n'.join(store.db.iterdump())
                    store.close()
                    result_file = Path(tmp) / 'synthetic-result.json'
                    result_file.write_text(json.dumps(answer), encoding='utf-8')
                    import asyncio
                    from aiohttp import web
                    from aiohttp.test_utils import TestClient, TestServer
                    from hub_web import Hub, PRINCIPAL_KEY, mount
                    async def api_output():
                        @web.middleware
                        async def auth(request, handler):
                            request[PRINCIPAL_KEY] = {'id':'test', 'admin':True, 'scopes':['read','query']}
                            return await handler(request)
                        hub = Hub(tmp, lambda *args: {'ok':True, 'state':'login_required'})
                        hub.store.ingest(answer)
                        app = web.Application(middlewares=[auth])
                        mount(app, hub)
                        app.on_startup.clear()
                        app.on_cleanup.clear()
                        try:
                            async with TestClient(TestServer(app)) as client:
                                response = await client.get('/api/v1/series?platform=baidu&keyword=synthetic')
                                self.assertEqual(response.status, 200)
                                return await response.text()
                        finally:
                            hub.store.close()
                    with patch.dict(os.environ, {'INDEX_HUB_STATE_DIR': str(Path(tmp) / 'hubstate')}):
                        response = asyncio.run(api_output())
                    public = json.dumps(answer) + dump + result_file.read_text(encoding='utf-8') + response
                    for secret in ('fake-cookie-secret', 'fake-signature-secret', 'abc1,2'):
                        self.assertNotIn(secret, public)
                        self.assertNotIn(secret.encode(), (Path(tmp) / 'test.sqlite').read_bytes())
                finally:
                    bridge.shutdown()
                    bridge.server_close()
                    worker.close()
        server.shutdown()
        server.server_close()
        thread.join()
