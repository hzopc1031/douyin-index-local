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

    def test_login_open_click_timeout(self):
        from unittest.mock import Mock
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from baidu_browser import login_open
        page = Mock()
        login = page.get_by_text.return_value.first
        login.is_visible.return_value = True
        login.click.side_effect = PlaywrightTimeoutError('synthetic timeout')
        with patch('baidu_browser._page', return_value=page):
            self.assertEqual(login_open(None), {'ok': True, 'state': 'login_required'})
        login.click.side_effect = RuntimeError('other error')
        with patch('baidu_browser._page', return_value=page), self.assertRaises(RuntimeError):
            login_open(None)


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
                from baidu_browser import status, login_open
                self.assertEqual(status(page.context, origin=origin, allow_loopback=True)['state'], 'login_required')
                page.evaluate("() => { const b = document.createElement('button'); b.textContent = '登录'; b.onclick = () => b.textContent = '退出登录'; document.body.append(b); }")
                with patch('baidu_browser._page', return_value=page):
                    login_open(None)
                page.context.add_cookies([{'name': 'BDUSS', 'value': 'synthetic-cookie-secret', 'url': origin}])
                state = status(page.context, origin=origin, allow_loopback=True)
                self.assertEqual(state['state'], 'browser_ready')
                self.assertNotIn('synthetic-cookie-secret', repr(state))
                import datetime
                today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
                query = dict(keywords=['a', 'b', 'c', 'd', 'e'], start_date='2026-10-01', end_date='2026-10-02', _budget_day=today)
                rows = PageTransport(page, origin=origin, allow_loopback=True).query(query)
                self.assertEqual(len(rows), 30)
                self.assertEqual([r['search_index'] for r in rows[:2]], [1, 2])
                self.assertNotIn('secret', repr(rows))
                related = {**query, 'keywords': ['a'], 'kind': 'related'}
                page.evaluate('window.Paris = null')
                graph = PageTransport(page, origin=origin, allow_loopback=True).query(related)
                self.assertEqual(graph['wordlist'][0]['wordGraph'][0]['word'], '合成关联词')
                self.assertEqual(server.graph_calls, 1)
                self.assertEqual(server.index_calls, 1)
                from baidu_adapter import BaiduError
                server.mode = 'graph_schema'
                with self.assertLogs('index_hub.baidu', level='WARNING'), self.assertRaises(BaiduError):
                    PageTransport(page, origin=origin, allow_loopback=True).query(related)
                server.mode = 'graph_oversized'
                with self.assertLogs('index_hub.baidu', level='WARNING'), self.assertRaises(BaiduError) as caught:
                    PageTransport(page, origin=origin, allow_loopback=True).query(related)
                self.assertEqual(caught.exception.code, 'response_schema')
                server.mode = 'graph_empty'
                self.assertEqual(PageTransport(page, origin=origin, allow_loopback=True).query(related)['wordlist'][0]['wordGraph'], [])
                server.mode = ''
                page.evaluate("window.Paris={getAcsInstance:cb=>cb(null,{getSign:cb=>cb(null,'fake-signature-secret')})}")

                from baidu_adapter import BaiduError, collect_page
                before = server.index_calls
                page.evaluate("day => { window.realNow = Date.now; Date.now = () => Date.parse(day + 'T15:59:31Z'); }", today)
                with self.assertRaises(BaiduError) as caught:
                    PageTransport(page, origin=origin, allow_loopback=True).query(query)
                self.assertEqual(caught.exception.code, 'baidu_daily_limit')
                self.assertEqual(server.index_calls, before)
                page.evaluate('Date.now = window.realNow')
                for mode in ('non_day', 'bad_type'):
                    server.mode = mode
                    with self.subTest(mode=mode), self.assertRaises(BaiduError) as caught:
                        PageTransport(page, origin=origin, allow_loopback=True).query(query)
                    self.assertEqual(caught.exception.code, 'response_schema')
                server.mode = 'extra'
                self.assertEqual(PageTransport(page, origin=origin, allow_loopback=True).query(query)[0]['search_index'], 1)
                for mode, present, absent in [('no_uniqid', 'userIndexes', 'uniqid'), ('no_indexes', 'uniqid', 'userIndexes'), ('no_data', 'startDate', 'data')]:
                    server.mode = mode
                    with self.subTest(mode=mode), self.assertLogs('index_hub.baidu', level='WARNING') as logs:
                        with self.assertRaises(BaiduError) as caught:
                            PageTransport(page, origin=origin, allow_loopback=True).query(query)
                    self.assertEqual(caught.exception.code, 'response_schema')
                    self.assertIn(present, str(logs.output))
                    if mode != 'no_data':
                        self.assertNotIn(absent, str(logs.output))
                    for secret in ('fake-cookie-secret', 'synthetic-cookie-secret', 'fake-signature-secret', 'abc1,2', 'synthetic-value-secret'):
                        self.assertNotIn(secret, str(logs.output) + str(caught.exception))
                long_series = {**query, 'keywords': ['a'], 'start_date': '2025-10-02', 'end_date': '2026-10-01'}
                server.mode = 'long_data'
                self.assertEqual(len(PageTransport(page, origin=origin, allow_loopback=True).query(long_series)), 365 * 3)
                server.mode = 'over_limit'
                with self.assertLogs('index_hub.baidu', level='WARNING'), self.assertRaises(BaiduError) as caught:
                    PageTransport(page, origin=origin, allow_loopback=True).query(long_series)
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
                mask = os.umask(0o077)
                os.umask(mask)
                owner = browser.call(lambda ctx: (threading.get_ident(), len(ctx.pages)))
                current = os.umask(0o077)
                os.umask(current)
                self.assertEqual(current, mask)
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
                 patch.object(baidu_browser, 'status', return_value={'ok': True, 'state':'browser_ready', 'live_enabled': True}), \
                 patch.object(baidu_browser, 'PageTransport', side_effect=lambda page: transport(page, origin=origin, allow_loopback=True)), \
                 patch.object(hub_bridge, '_socket_path', return_value=sock):
                bridge = hub_bridge.start_bridge(Mock())
                try:
                    query = dict(platform='baidu', keywords=['synthetic'], start_date='2026-10-01', end_date='2026-10-02')
                    import datetime
                    day = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
                    answer = hub_bridge.call_bridge('baidu_query', {**query, '_budget_day':day})
                    self.assertTrue(answer['ok'])
                    related = {**query, 'kind': 'related'}
                    related_answer = hub_bridge.call_bridge('baidu_query', {**related, '_budget_day':day})
                    self.assertTrue(related_answer['ok'])
                    self.assertEqual(related_answer['data']['search_related'][0]['keyword'], '合成关联词')
                    self.assertEqual(server.graph_calls, 1)
                    store = Store(Path(tmp) / 'test.sqlite')
                    store.ingest(answer)
                    store.save_insight(related, {k: related_answer[k] for k in ('data', 'metadata')})
                    self.assertEqual(store.insight(related)['data'], related_answer['data'])
                    dump = '\n'.join(store.db.iterdump())
                    store.close()
                    result_file = Path(tmp) / 'synthetic-result.json'
                    result_file.write_text(json.dumps(answer), encoding='utf-8')
                    import asyncio
                    from hub_web import Hub
                    async def related_worker():
                        hub = Hub(tmp, hub_bridge.call_bridge)
                        try:
                            job, _ = hub.store.submit(related, 'synthetic', 'related-browser-fixture')
                            self.assertTrue(await hub.run_one())
                            self.assertEqual(hub.store.job(job['id'])['status'], 'succeeded')
                            self.assertEqual(hub.store.insight(related)['data'], related_answer['data'])
                            self.assertEqual(hub.store.get('baidu.daily')['http_reserved'], 1)
                        finally:
                            hub.store.close()
                    asyncio.run(related_worker())
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


class ReadOnlyStatusTests(unittest.TestCase):
    def test_query_prefers_existing_index_page(self):
        from unittest.mock import Mock
        from baidu_browser import _page
        passport = Mock(url='https://passport.baidu.com/verify')
        index = Mock(url='https://index.baidu.com/')
        self.assertIs(_page(Mock(pages=[passport, index])), index)
        passport.goto.assert_not_called()
        index.goto.assert_not_called()

    def test_pages_cookie_and_signature(self):
        from unittest.mock import Mock
        from baidu_browser import status
        secret = 'synthetic-cookie-secret'
        passport = Mock(url='https://passport.baidu.com/verify')
        index = Mock(url='https://index.baidu.com/')
        index.evaluate.return_value = True
        context = Mock(pages=[passport])
        self.assertEqual(status(context)['state'], 'login_in_progress')
        passport.goto.assert_not_called()
        context.new_page.assert_not_called()
        context.pages = [passport, index]
        for cookies, signature, expected in [
            ([], True, 'login_required'),
            ([{'name': 'other', 'expires': -1, 'value': secret}], True, 'login_required'),
            ([{'name': 'BDUSS', 'expires': 1, 'value': secret}], True, 'login_required'),
            ([{'name': 'BDUSS', 'expires': -1, 'value': secret}], True, 'browser_ready'),
            ([{'name': 'BDUSS', 'expires': 9999999999, 'value': secret}], False, 'signature_unavailable'),
        ]:
            with self.subTest(expected=expected, cookies=len(cookies)):
                context.cookies.return_value = cookies
                index.evaluate.return_value = signature
                answer = status(context)
                self.assertEqual(answer['state'], expected)
                self.assertNotIn(secret, repr(answer))
        context.cookies.side_effect = RuntimeError(secret)
        with self.assertRaises(Exception) as caught:
            status(context)
        self.assertNotIn(secret, str(caught.exception))
        index.goto.assert_not_called()
        index.bring_to_front.assert_not_called()
        passport.goto.assert_not_called()

    def test_status_bridge_and_hub_accept_progress(self):
        import asyncio
        from unittest.mock import Mock
        import baidu_adapter
        import baidu_browser
        import hub_baidu
        worker = Mock(started=True, busy=False)
        worker.call.return_value = {'ok': True, 'state': 'login_in_progress', 'live_enabled': True}
        with patch.object(baidu_browser, 'browser', return_value=worker):
            result = baidu_adapter.bridge_command('baidu_status')
            worker.call.assert_called_once_with(baidu_browser.status, touch='status')
        self.assertEqual(asyncio.run(hub_baidu.browser_status(Mock(bridge=Mock(return_value=result)))), 'login_in_progress')

    def test_idle_status_and_cold_query(self):
        import asyncio
        import datetime
        from unittest.mock import Mock
        import baidu_adapter
        import baidu_browser
        import hub_baidu
        from test_baidu_adapter import query
        worker = Mock(started=False, busy=False, last_state='browser_ready')
        context = Mock()
        worker.call.side_effect = lambda action, **kwargs: action(context)
        with patch.object(baidu_browser, 'browser', return_value=worker):
            result = baidu_adapter.bridge_command('baidu_status')
            self.assertEqual(result['state'], 'browser_idle')
            self.assertEqual(asyncio.run(hub_baidu.browser_status(Mock(bridge=Mock(return_value=result)))), 'browser_idle')
            with patch.object(baidu_browser, '_page') as page, patch.object(baidu_browser, 'status', return_value={'state': 'login_required'}) as status:
                day = datetime.datetime.now(baidu_adapter.TZ).date().isoformat()
                answer = baidu_adapter.bridge_command('baidu_query', {**query(), '_budget_day': day})
            self.assertEqual(answer['code'], 'login_required')
            page.assert_called_once_with(context)
            status.assert_called_once_with(context)
            self.assertEqual(worker.last_state, 'login_required')

    def test_fresh_browser_status_and_cold_query_checks_cookie(self):
        import datetime
        from unittest.mock import Mock
        import baidu_adapter
        import baidu_browser
        from test_baidu_adapter import query
        day = datetime.datetime.now(baidu_adapter.TZ).date().isoformat()
        for cookies in ([{'name': 'BDUSS', 'expires': -1}], []):
            with self.subTest(logged_in=bool(cookies)):
                worker = baidu_browser.BaiduBrowser(root='.')
                page = Mock(url='https://index.baidu.com/')
                page.evaluate.return_value = True
                context = Mock(pages=[page])
                context.cookies.return_value = cookies
                try:
                    with patch.object(baidu_browser, 'browser', return_value=worker), \
                         patch.object(worker, 'call', side_effect=lambda action, **kwargs: action(context)) as call, \
                         patch.object(baidu_adapter, 'collect_page', return_value={'rows': []}) as collect:
                        self.assertEqual(baidu_adapter.bridge_command('baidu_status')['state'], 'browser_idle')
                        self.assertFalse(worker.started)
                        call.assert_not_called()
                        answer = baidu_adapter.bridge_command('baidu_query', {**query(), '_budget_day': day})
                        self.assertTrue(worker.login_checked)
                        if cookies:
                            self.assertTrue(answer['ok'])
                            collect.assert_called_once()
                        else:
                            self.assertEqual(answer['code'], 'login_required')
                            collect.assert_not_called()
                            self.assertEqual(baidu_adapter.bridge_command('baidu_status')['state'], 'login_required')
                finally:
                    worker.close()

    def test_status_exception_clears_ready(self):
        from unittest.mock import Mock
        import baidu_browser
        worker = baidu_browser.BaiduBrowser(root='.')
        worker.last_state = 'browser_ready'
        try:
            with patch.object(baidu_browser, 'prepare_profile', side_effect=ProfileError('synthetic')):
                with self.assertRaises(ProfileError):
                    worker.call(lambda context: None, touch='status')
            self.assertEqual(worker.last_state, 'login_required')
        finally:
            worker.close()

    def test_untrusted_digest_is_not_logged(self):
        from unittest.mock import Mock
        from baidu_adapter import BaiduError
        from baidu_browser import PageTransport
        page = Mock(url='https://index.baidu.com/')
        page.evaluate.return_value = {'status': 'response_schema', 'schema_digest': {'type': 'string', 'secret': 'synthetic-secret'}}
        with patch('baidu_browser.logging.getLogger') as logger, self.assertRaises(BaiduError):
            PageTransport(page).query({})
        logger.assert_not_called()

    def test_hold_bounds(self):
        from baidu_browser import BaiduBrowser
        for value in ('0', '86401', 'invalid'):
            with self.subTest(value=value), patch.dict(os.environ, {'INDEX_HUB_BAIDU_LOGIN_HOLD_SECONDS': value}):
                with self.assertRaises(ValueError):
                    BaiduBrowser()


class LifetimeClockTests(unittest.TestCase):
    def test_failed_action_keeps_hold_and_refreshes_activity(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import baidu_browser
        now = [0]
        context = Mock()
        playwright = Mock()
        playwright.chromium.launch_persistent_context.return_value = context
        failure = RuntimeError('synthetic failure')
        result = Mock()
        def fail(_context):
            now[0] = 100
            raise failure
        events = [(0, fail, result, True, True),
                  (350, lambda c: None, Mock(), False, False),
                  (351, lambda c: {'state': 'browser_ready'}, Mock(), 'status', False),
                  (352, None, Mock(), True, False)]
        timeouts = []
        class Queue:
            def get(self, timeout=None):
                timeouts.append(timeout)
                stamp, action, response, touch, hold = events.pop(0)
                now[0] = stamp
                if stamp == 350:
                    context.close.assert_not_called()
                return action, response, touch, hold
        worker = baidu_browser.BaiduBrowser.__new__(baidu_browser.BaiduBrowser)
        worker.root = '.'
        worker.idle_seconds = 300
        worker.hold_seconds = 900
        worker._queue = Queue()
        worker.started = worker.busy = False
        worker.last_state = 'login_required'
        with patch.object(baidu_browser, 'time', SimpleNamespace(monotonic=lambda: now[0])), \
             patch.object(baidu_browser, 'prepare_profile', return_value='profile'), \
             patch.object(baidu_browser, 'ProfileLock') as lock, \
             patch('playwright.sync_api.sync_playwright') as start:
            lock.return_value.acquire.return_value = Mock()
            start.return_value.start.return_value = playwright
            worker._run()
        result.put.assert_called_once_with((False, failure))
        self.assertEqual(timeouts, [None, 900, 650, 49])
        context.close.assert_called_once()

    def test_status_activity_and_login_hold(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import baidu_browser
        now = [0]
        context = Mock()
        playwright = Mock()
        playwright.chromium.launch_persistent_context.return_value = context
        events = [
            (0, lambda c: {'state': 'login_required'}, True, True),
            (100, lambda c: {'state': 'login_in_progress'}, 'status', False),
            (200, lambda c: {'state': 'login_required'}, 'status', False),
            (300, lambda c: {'state': 'browser_ready'}, 'status', False),
            (301, None, True, False),
        ]
        timeouts = []
        class Queue:
            def get(self, timeout=None):
                timeouts.append(timeout)
                stamp, action, touch, hold = events.pop(0)
                now[0] = stamp
                return action, Mock(), touch, hold
        worker = baidu_browser.BaiduBrowser.__new__(baidu_browser.BaiduBrowser)
        worker.root = '.'
        worker.idle_seconds = 300
        worker.hold_seconds = 900
        worker._queue = Queue()
        worker.started = worker.busy = False
        worker.last_state = 'login_required'
        fake_clock = SimpleNamespace(monotonic=lambda: now[0])
        with patch.object(baidu_browser, 'time', fake_clock), \
             patch.object(baidu_browser, 'prepare_profile', return_value='profile'), \
             patch.object(baidu_browser, 'ProfileLock') as lock, \
             patch('playwright.sync_api.sync_playwright') as start:
            lock.return_value.acquire.return_value = Mock()
            start.return_value.start.return_value = playwright
            worker._run()
        self.assertEqual(timeouts, [None, 900, 800, 700, 200])
        self.assertEqual(worker.last_state, 'browser_ready')
        context.close.assert_called_once()
