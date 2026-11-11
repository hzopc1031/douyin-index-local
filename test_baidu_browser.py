import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from baidu_browser import prepare_profile
from browser_profile import ProfileError



def graph_handler():
    import json
    from urllib.parse import parse_qs, urlsplit
    from fake_baidu_page import Handler
    class GraphHandler(Handler):
        def do_GET(self):
            path = urlsplit(self.path)
            if path.path != '/api/WordGraph/multi':
                return super().do_GET()
            self.server.graph_calls = getattr(self.server, 'graph_calls', 0) + 1
            assert len(parse_qs(path.query)['wordlist[]']) == 1
            mode = getattr(self.server, 'mode', '')
            graph = [] if mode == 'graph_empty' else [{'word': '合成关联词'}] * (101 if mode == 'graph_oversized' else 1)
            data = {} if mode == 'graph_schema' else {'period': '20261026|20261101', 'wordlist': [{'wordGraph': graph}]}
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 0, 'data': data}).encode())
    return GraphHandler

class ReadinessTests(unittest.TestCase):
    def test_success_extra_shape_logging_and_validation(self):
        import copy
        import json
        from unittest.mock import Mock
        from baidu_browser import PageTransport
        from hub_logging import redact
        from test_baidu_adapter import extra_shape
        page = Mock(url='https://index.baidu.com/')
        shape = extra_shape()
        page.evaluate.return_value = {'status': 'ok', 'rows': [], 'news_rows': [], 'news_shape': shape}
        with self.assertLogs('index_hub.baidu', level='INFO') as logs:
            answer = PageTransport(page).query({'_news_enabled': True})
        line = redact(' '.join(logs.output))
        for field in ('index_length', 'data_length', 'ptbk_length', 'decoded_points', 'empty_points', 'used_fallback', 'user_indexes_present'):
            self.assertIn('"' + field + '"', line)
        self.assertEqual(answer['news_shape'], shape)
        for secret in ('private-cookie-value', 'private-cipher-value'):
            self.assertNotIn(secret, line + json.dumps(answer))
        for field in ('cookie', 'data'):
            bad = copy.deepcopy(shape)
            bad[field] = 'private-cookie-value' if field == 'cookie' else 'private-cipher-value'
            page.evaluate.return_value['news_shape'] = bad
            with self.assertNoLogs('index_hub.baidu', level='INFO'):
                answer = PageTransport(page).query({'_news_enabled': True})
            self.assertNotIn('news_shape', answer)

    def test_related_query_with_job_id_and_unavailable_signature(self):
        import datetime
        from unittest.mock import Mock
        import baidu_adapter as adapter
        import baidu_browser
        from test_baidu_adapter import query

        worker = Mock()
        worker.call.side_effect = lambda action: action(Mock())
        graph = {'period': {'start_date': '2026-10-26', 'end_date': '2026-11-01', 'days': 7}, 'wordlist': [{'wordGraph': []}]}
        body = {**query(), 'kind': 'related', 'related_date': query()['end_date'], '_budget_day': datetime.datetime.now(adapter.TZ).date().isoformat(), '_job_id': 'a' * 32}
        with patch.object(baidu_browser, 'browser', return_value=worker), \
             patch.object(baidu_browser, '_page'), patch.object(baidu_browser, 'wait_ready') as ready, \
             patch.object(baidu_browser, 'status', return_value={'state': 'signature_unavailable'}), \
             patch.object(baidu_browser, 'PageTransport') as transport, \
             patch.object(adapter, 'project_wordgraph', return_value={'search_related': [], 'content_related': []}) as project:
            transport.return_value.query.return_value = graph
            answer = adapter.bridge_command('baidu_query', body)
        self.assertTrue(answer['ok'])
        ready.assert_called_once()
        project.assert_called_once_with(graph)
        self.assertNotIn('_job_id', transport.return_value.query.call_args.args[0])
        self.assertEqual(answer['metadata']['related_period'], graph['period'])
        self.assertEqual(answer['metadata']['related_date'], body['related_date'])
        for invalid in ('2026-02-30', '20261103', '9999-12-31', None):
            with self.subTest(invalid=invalid), self.assertRaises(adapter.BaiduError):
                adapter.bridge_command('baidu_query', {**body, 'related_date': invalid})

    def test_query_readiness_and_safe_failures(self):
        import datetime
        import time
        from unittest.mock import Mock
        import baidu_adapter as adapter
        import baidu_browser
        from test_baidu_adapter import query

        class Error(Exception):
            pass

        class TimeoutError(Error):
            pass

        day = datetime.datetime.now(adapter.TZ).date().isoformat()
        for mode in ('navigation', 'closed', 'timeout', 'login', 'unexpected'):
            with self.subTest(mode=mode):
                page = Mock(url='https://index.baidu.com/?fake-cookie-secret=synthetic')
                worker = Mock(started_at=time.monotonic() - 2)
                worker.call.side_effect = lambda action: action(Mock())
                if mode == 'navigation':
                    page.evaluate.side_effect = [Error('Execution context was destroyed synthetic'), Error('Cannot find context'), True, {'status': 'ok', 'rows': []}]
                elif mode in ('closed', 'timeout'):
                    error = Error('Target page, context or browser has been closed fake-cookie-secret') if mode == 'closed' else TimeoutError('Timeout synthetic')
                    page.evaluate.side_effect = [True, error]
                elif mode == 'login':
                    page.url = 'https://passport.baidu.com/login?fake-cookie-secret=synthetic'
                else:
                    page.evaluate.side_effect = RuntimeError('exception message fake-cookie-secret synthetic')
                with patch.object(baidu_browser, 'browser', return_value=worker), \
                     patch.object(baidu_browser, '_page', return_value=page), \
                     patch.object(baidu_browser, 'status', return_value={'state': 'browser_ready'}), \
                     patch.object(adapter, 'collect_page', side_effect=lambda q, transport, **kw: {'rows': transport.query(q)}), \
                     patch('baidu_browser.time.sleep'):
                    if mode == 'navigation':
                        answer = adapter.bridge_command('baidu_query', {**query(), '_budget_day': day})
                    else:
                        with self.assertLogs('index_hub.baidu', level='WARNING') as logs:
                            answer = adapter.bridge_command('baidu_query', {**query(), '_budget_day': day, '_job_id': 'a' * 32})
                if mode == 'navigation':
                    self.assertTrue(answer['ok'])
                    self.assertEqual(page.evaluate.call_count, 4)
                else:
                    code = {'closed': 'page_closed', 'timeout': 'page_timeout', 'login': 'login_required', 'unexpected': 'page_other'}[mode]
                    self.assertEqual(answer['code'], code)
                    self.assertEqual(page.evaluate.call_count, 0 if mode == 'login' else 1 if mode == 'unexpected' else 2)
                    line = logs.output[0]
                    self.assertIn('job=' + 'a' * 32, line)
                    category = 'other' if mode == 'unexpected' else code
                    sent = '0' if mode in ('login', 'unexpected') else 'unknown'
                    self.assertIn(f'category={category} requests_sent={sent} since_start=', line)
                    self.assertIn('on_index=' + ('false' if mode == 'login' else 'true'), line)
                    self.assertIn('datelist_sent=none period_returned=false', line)
                    if mode == 'unexpected':
                        self.assertIn('stage=wait_ready exception_type=RuntimeError', line)
                        self.assertIn("exception_message='exception message'", line)
                    for secret in ('fake-cookie-secret', 'synthetic', '?', 'Target'):
                        self.assertNotIn(secret, line)

    def test_related_failure_logs_safe_period_diagnostics(self):
        import datetime
        from unittest.mock import Mock
        import baidu_adapter as adapter
        import baidu_browser
        from test_baidu_adapter import query
        page = Mock(url='https://index.baidu.com/?synthetic-secret')
        worker = Mock()
        worker.call.side_effect = lambda action: action(Mock())
        diag = dict(reason='related_period', request_start=None, request_end=None, response_start=None, response_end=None,
                    word_type=None, name_matches=False, ptbk_length=0, decoded_points=None, data_length=None,
                    data_type='object', datelist_sent='20260927', period_returned=True)
        page.evaluate.return_value = {'status': 'response_schema', 'schema_digest': {'type': 'object', 'keys': {}}, 'schema_diag': diag}
        body = {**query(), 'kind': 'related', 'related_date': '2026-09-27', '_budget_day': datetime.datetime.now(adapter.TZ).date().isoformat()}
        with patch.object(baidu_browser, 'browser', return_value=worker), patch.object(baidu_browser, '_page', return_value=page), \
             patch.object(baidu_browser, 'wait_ready'), patch.object(baidu_browser, 'status', return_value={'state': 'browser_ready'}), \
             self.assertLogs('index_hub.baidu', level='WARNING') as logs:
            answer = adapter.bridge_command('baidu_query', body)
        self.assertEqual(answer['code'], 'response_schema')
        line = next(line for line in logs.output if 'baidu_failure' in line)
        self.assertIn('datelist_sent=present period_returned=true', line)
        self.assertNotIn('synthetic-secret', str(logs.output))

    def test_goto_and_ready_playwright_errors_are_classified(self):
        from unittest.mock import Mock
        import baidu_browser
        from baidu_adapter import BaiduError
        class Error(Exception): pass
        class TimeoutError(Error): pass
        page = Mock(url='about:blank')
        page.goto.side_effect = TimeoutError('timeout fake-cookie-secret')
        with self.assertRaises(BaiduError) as caught:
            baidu_browser._page(Mock(pages=[page]))
        self.assertEqual(caught.exception.code, 'page_timeout')
        self.assertEqual(caught.exception.exception_type, 'TimeoutError')
        self.assertNotIn('fake-cookie-secret', caught.exception.exception_message)
        page = Mock(url='https://index.baidu.com/')
        page.evaluate.side_effect = Error('unexpected fake-cookie-secret')
        with self.assertRaises(BaiduError) as caught:
            baidu_browser.wait_ready(page, 'https://index.baidu.com')
        self.assertEqual(caught.exception.code, 'page_other')
        self.assertEqual(caught.exception.exception_type, 'Error')

    def test_unexpected_query_error_logs_exception(self):
        import datetime
        from unittest.mock import Mock
        import baidu_adapter as adapter
        import baidu_browser
        from test_baidu_adapter import query
        page = Mock(url='https://index.baidu.com/')
        page.evaluate.side_effect = RuntimeError('network failure fake-cookie-secret ' + 'x' * 200)
        worker = Mock()
        worker.call.side_effect = lambda action: action(Mock())
        body = {**query(), '_budget_day': datetime.datetime.now(adapter.TZ).date().isoformat()}
        with patch.object(baidu_browser, 'browser', return_value=worker), patch.object(baidu_browser, '_page', return_value=page), \
             patch.object(baidu_browser, 'wait_ready'), patch.object(baidu_browser, 'status', return_value={'state':'browser_ready'}), \
             self.assertLogs('index_hub.baidu', level='WARNING') as logs:
            answer = adapter.bridge_command('baidu_query', body)
        self.assertEqual(answer, {'ok':False, 'code':'page_other'})
        line = str(logs.output)
        for text in ('category=other', 'stage=search', 'requests_sent=unknown', 'exception_type=RuntimeError', 'network failure'):
            self.assertIn(text, line)
        self.assertNotIn('fake-cookie-secret', line)
        self.assertNotIn('x' * 80, line)

    def test_page_failure_logs_safe_http_diagnostics(self):
        from unittest.mock import Mock
        from baidu_browser import PageTransport
        from baidu_adapter import BaiduError
        page = Mock(url='https://index.baidu.com/')
        page.evaluate.return_value = {'status':'business_error', 'stage':'news_ptbk', 'requests_sent':6,
            'http_status':200, 'business_status':42, 'message':'server error 人工智能 fixture-token ciphertext'}
        with self.assertLogs('index_hub.baidu', level='WARNING') as logs, self.assertRaises(BaiduError):
            PageTransport(page).query({})
        line = str(logs.output)
        for field in ('stage=news_ptbk', 'requests_sent=6', 'http_status=200', 'business_status=42'):
            self.assertIn(field, line)
        for secret in ('人工智能', 'fixture-token', 'ciphertext'):
            self.assertNotIn(secret, line)

    def test_readiness_deadline(self):
        from unittest.mock import Mock
        from baidu_adapter import BaiduError
        from baidu_browser import wait_ready
        page = Mock(url='https://index.baidu.com/')
        page.evaluate.return_value = False
        with self.assertRaises(BaiduError) as caught:
            wait_ready(page, 'https://index.baidu.com', timeout=0.01)
        self.assertEqual(caught.exception.code, 'page_not_ready')
        page.evaluate.assert_called_once()


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

    def test_login_open_preserves_existing_session(self):
        from unittest.mock import Mock
        from baidu_browser import login_open
        page = Mock(url='https://index.baidu.com/')
        with patch('baidu_browser._page', return_value=page), patch('baidu_browser.status', return_value={'state': 'browser_ready'}):
            self.assertEqual(login_open(None)['state'], 'browser_ready')
        page.get_by_text.assert_not_called()

    def test_login_open_click_timeout(self):
        from unittest.mock import Mock
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from baidu_browser import login_open
        page = Mock(url='https://index.baidu.com/')
        login = page.get_by_text.return_value.first
        login.is_visible.return_value = True
        login.click.side_effect = PlaywrightTimeoutError('synthetic timeout')
        with patch('baidu_browser._page', return_value=page), patch('baidu_browser.status', return_value={'state': 'login_required'}):
            self.assertEqual(login_open(None), {'ok': True, 'state': 'login_required'})
        login.click.side_effect = RuntimeError('other error')
        with patch('baidu_browser._page', return_value=page), patch('baidu_browser.status', return_value={'state': 'login_required'}), self.assertRaises(RuntimeError):
            login_open(None)


@unittest.skipUnless(os.environ.get('INDEX_HUB_REQUIRE_BROWSER_TESTS') == '1', 'browser tests not requested')
class BrowserPageTests(unittest.TestCase):
    def test_five_words_real_browser(self):
        import threading
        from playwright.sync_api import sync_playwright
        from baidu_browser import PageTransport
        from fake_baidu_page import serve
        from test_baidu_browser import graph_handler
        server = serve()
        server.RequestHandlerClass = graph_handler()
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
                related = {**query, 'keywords': ['a'], 'kind': 'related', 'related_date': query['end_date']}
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
                from baidu_adapter import public_result
                lag_query = dict(platform='baidu', keywords=['a'], start_date='2026-09-26', end_date='2026-10-02')
                for mode in ('truncated_end', 'trailing_empty'):
                    server.mode = mode
                    result = public_result(collect_page(lag_query, PageTransport(page, origin=origin, allow_loopback=True)))
                    self.assertEqual(len(result['rows']), 15)
                    self.assertTrue(result['metadata']['lagged'])
                    self.assertIn('（滞后）', result['metadata']['note'])
                    self.assertTrue(all(row['search_index'] != 0 for row in result['rows']))
                for mode, reason in [('end_after', 'end_date'), ('start_mismatch', 'start_date'), ('bad_word_type', 'word_type'), ('wrong_name', 'word')]:
                    server.mode = mode
                    with self.subTest(mode=mode), self.assertLogs('index_hub.baidu', level='WARNING') as logs, self.assertRaises(BaiduError):
                        PageTransport(page, origin=origin, allow_loopback=True).query({**lag_query, '_budget_day':today})
                    self.assertIn('"reason":"' + reason + '"', str(logs.output))
                    for secret in ('fake-cookie-secret', 'synthetic-cookie-secret', 'fake-signature-secret', 'abc1,2', 'synthetic-value-secret'):
                        self.assertNotIn(secret, str(logs.output))
                server.mode = ''
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
        from test_baidu_browser import graph_handler
        server = serve()
        server.RequestHandlerClass = graph_handler()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = 'http://127.0.0.1:' + str(server.server_port)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'INDEX_HUB_BAIDU_PROFILE_DIR': str(Path(tmp) / 'profile')}):
            worker = baidu_browser.BaiduBrowser(root=tmp, idle_seconds=30)
            worker.last_state = 'browser_ready'
            transport = baidu_browser.PageTransport
            wait_ready = baidu_browser.wait_ready
            def page_for(context):
                page = context.pages[0]
                if not page.url.startswith(origin):
                    page.goto(origin)
                return page
            sock = Path(tmp) / 'bridge.sock'
            with patch.object(baidu_browser, '_browser', worker), patch.object(baidu_browser, '_page', page_for), \
                 patch.object(baidu_browser, 'status', return_value={'ok': True, 'state':'browser_ready', 'live_enabled': True}), \
                 patch.object(baidu_browser, 'wait_ready', side_effect=lambda page, _: wait_ready(page, origin)), \
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
                    related_answer = hub_bridge.call_bridge('baidu_query', {**related, 'related_date': query['end_date'], '_budget_day':day})
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
            with patch.object(baidu_browser, '_page') as page, patch.object(baidu_browser, 'wait_ready'), patch.object(baidu_browser, 'status', return_value={'state': 'login_required'}) as status:
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

    def test_feed_digest_whitelist(self):
        from baidu_browser import _safe_digest
        digest = {'type': 'object', 'keys': {'index': {'type': 'array', 'length': 1, 'items': {
            'type': 'object', 'keys': {'key': {'type': 'string'}, 'word': {'type': 'array', 'length': 1, 'items': {'type': 'object', 'keys': {'name': {'type': 'string'}}}}, 'data': {'type': 'string'}}}}}}
        self.assertEqual(_safe_digest(digest), digest)
        digest['keys']['index']['items']['keys']['key']['value'] = 'synthetic-secret'
        with self.assertRaises(ValueError):
            _safe_digest(digest)

    def test_untrusted_digest_is_not_logged(self):
        from unittest.mock import Mock
        from baidu_adapter import BaiduError
        from baidu_browser import PageTransport
        page = Mock(url='https://index.baidu.com/')
        page.evaluate.return_value = {'status': 'response_schema', 'schema_digest': {'type': 'string', 'secret': 'synthetic-secret'}}
        with patch('baidu_browser.logging.getLogger') as logger, self.assertRaises(BaiduError):
            PageTransport(page).query({})
        self.assertNotIn('synthetic-secret', str(logger.mock_calls))
        self.assertNotIn('schema_digest=', str(logger.mock_calls))

    def test_untrusted_diagnostics_are_not_logged(self):
        from unittest.mock import Mock
        from baidu_adapter import BaiduError
        from baidu_browser import PageTransport
        diag = dict(reason='word', request_start=None, request_end=None, response_start=None, response_end=None,
                    word_type=None, name_matches=False, ptbk_length=0, decoded_points=None, data_length=None, data_type=None, datelist_sent='none', period_returned=False)
        page = Mock(url='https://index.baidu.com/')
        for change in ({'secret':'synthetic-secret'}, {'name_matches':1}, {'request_start':'synthetic-secret'}, {'reason':'synthetic-secret'}, {'ptbk_length':True}, {'data_type':'synthetic-secret'}):
            page.evaluate.return_value = {'status':'response_schema', 'schema_digest':{'type':'string'}, 'schema_diag':diag | change}
            with patch('baidu_browser.logging.getLogger') as logger, self.assertRaises(BaiduError):
                PageTransport(page).query({})
            self.assertNotIn('synthetic-secret', str(logger.mock_calls))
            self.assertNotIn('schema_digest=', str(logger.mock_calls))
        page.evaluate.return_value = {'status':'response_schema', 'schema_digest':{'type':'string'}, 'schema_diag':diag}
        with self.assertLogs('index_hub.baidu', level='WARNING') as logs, self.assertRaises(BaiduError):
            PageTransport(page).query({})
        self.assertIn('"reason":"word"', str(logs.output))
        self.assertIn('schema_digest=', str(logs.output))
        secret = 'synthetic-response-secret'
        page.evaluate.return_value = {'status':'response_schema', 'schema_digest':{'type':'string'},
                                      'schema_diag':diag | {'reason':'related_data_type', 'data_type':'string', 'data_length':len(secret)}}
        with self.assertLogs('index_hub.baidu', level='WARNING') as logs, self.assertRaises(BaiduError):
            PageTransport(page).query({})
        self.assertIn('"data_type":"string"', str(logs.output))
        self.assertIn(f'"data_length":{len(secret)}', str(logs.output))
        self.assertNotIn(secret, str(logs.output))

    def test_period_shape_validation_and_related_key_logs(self):
        from unittest.mock import Mock
        from baidu_adapter import BaiduError
        from baidu_browser import PageTransport, _safe_period_shape
        shape = {'type': 'object', 'keys': ['masked', 'start'], 'values': {
            'masked': {'type': 'array', 'length': 2, 'items': [{'type': 'object'}, {'type': 'null'}]},
            'start': {'type': 'string', 'length': 5, 'pattern': '99Caa'}}}
        self.assertEqual(_safe_period_shape(shape), shape)
        for bad in ({'type': 'string', 'length': 1, 'pattern': '1'},
                    {'type': 'string', 'length': 1, 'pattern': 'x'},
                    {'type': 'string', 'length': 1, 'pattern': '中'},
                    {'type': 'string', 'length': 1, 'pattern': '\n'},
                    {'type': 'object', 'keys': ['bad-name'], 'values': {'bad-name': {'type': 'null'}}},
                    {'type': 'array', 'length': 1, 'items': [{'type': 'string', 'length': 3, 'pattern': 'aaa', 'secret': 'x'}]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _safe_period_shape(bad)
        page = Mock(url='https://index.baidu.com/')
        keys = {'word': 'string', 'masked': 'mixed', 'score': 'number', 'keyword': 'object'}
        page.evaluate.return_value = {'status': 'ok', 'data': {}, 'meta': {'entry_keys': keys}}
        with self.assertLogs('index_hub.baidu', level='WARNING') as logs:
            PageTransport(page, job_id='a' * 32).query({'kind': 'related'})
        self.assertIn('baidu_related_fields job=' + 'a' * 32 + ' fields keyword:object,masked:mixed,score:number,word:string', str(logs.output))
        diag = dict(reason='related_period', request_start=None, request_end=None, response_start=None, response_end=None,
                    word_type=None, name_matches=False, ptbk_length=0, decoded_points=None, data_length=None,
                    data_type='object', datelist_sent='none', period_returned=True, period_shape=shape, entry_keys=keys)
        page.evaluate.return_value = {'status': 'response_schema', 'schema_digest': {'type': 'object', 'keys': {}}, 'schema_diag': diag}
        with self.assertLogs('index_hub.baidu', level='WARNING') as logs, self.assertRaises(BaiduError):
            PageTransport(page, job_id='a' * 32).query({'kind': 'related'})
        self.assertNotIn('"period_shape"', str(logs.output))
        self.assertIn('fields keyword:object,masked:mixed,score:number,word:string', str(logs.output))
        self.assertNotIn('secret', str(logs.output))
        from hub_logging import redact
        redacted = redact(' '.join(logs.output))
        self.assertIn('keyword:', redacted)
        self.assertNotIn('20261026', redacted)
        self.assertNotIn('synthetic-word', redacted)
        page.evaluate.return_value['schema_diag'] = diag | {'period_shape': {'type': 'string', 'length': 6, 'pattern': 'secret'}}
        with self.assertLogs('index_hub.baidu', level='WARNING') as logs, self.assertRaises(BaiduError):
            PageTransport(page).query({'kind': 'related'})
        self.assertNotIn('secret', str(logs.output))
        self.assertNotIn('schema_digest=', str(logs.output))

    def test_related_period_diagnostics_survive_redaction_without_dates(self):
        from unittest.mock import Mock
        from baidu_adapter import BaiduError
        from baidu_browser import PageTransport
        from hub_logging import redact
        diag = dict(reason='related_period', request_start='2026-10-01', request_end='2027-10-02', response_start=None, response_end=None,
                    word_type=None, name_matches=False, ptbk_length=0, decoded_points=None, data_length=None,
                    data_type='object', datelist_sent='20261001', period_returned=True, period_rule='span_out_of_range', period_days=367,
                    entry_keys={'keyword': 'object', 'pv': 'number'})
        page = Mock(url='https://index.baidu.com/')
        page.evaluate.return_value = {'status': 'response_schema', 'schema_digest': {'type': 'object', 'keys': {}}, 'schema_diag': diag}
        with self.assertLogs('index_hub.baidu', level='WARNING') as logs, self.assertRaises(BaiduError):
            PageTransport(page).query({'kind': 'related'})
        output = redact(' '.join(logs.output))
        self.assertIn('span_out_of_range', output)
        self.assertIn('"period_days":367', output)
        self.assertIn('keyword:', output)
        for secret in ('2026-10-01', '2027-10-02', '20261001'):
            self.assertNotIn(secret, output)

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


class AudienceDiagnosticSyntheticTests(unittest.TestCase):
    def test_audience_period_diagnostic_redaction(self):
        from hub_logging import redact
        from baidu_browser import PageTransport
        from unittest.mock import Mock
        import baidu_adapter as adapter
        diag = dict(reason='audience_period', request_start='2026-10-01', request_end='2027-10-02', response_start=None, response_end=None, word_type=None, name_matches=False, ptbk_length=0, decoded_points=None, data_length=None, data_type=None, datelist_sent='none', period_returned=True, period_rule='span_out_of_range', period_days=367)
        page = Mock(url='https://index.baidu.com/')
        page.evaluate.return_value = {'status':'response_schema', 'schema_digest':{'type':'object', 'keys':{}}, 'schema_diag':diag}
        with self.assertLogs('index_hub.baidu') as logs, self.assertRaises(adapter.BaiduError):
            PageTransport(page).query({'kind':'audience'})
        output = redact(str(logs.output))
        self.assertIn('audience_period', output)
        self.assertIn('span_out_of_range', output)
        self.assertNotIn('2026-10-01', output)
        self.assertNotIn('2027-10-02', output)

class LoginQrTests(unittest.TestCase):
    def test_only_visible_qr_element_is_captured(self):
        from unittest.mock import Mock
        import base64
        import baidu_browser
        png = b'\x89PNG\r\n\x1a\nsynthetic'
        element = Mock()
        element.bounding_box.return_value = {'width': 180, 'height': 180}
        element.is_visible.return_value = True
        element.screenshot.return_value = png
        locator = Mock()
        locator.count.return_value = 1
        locator.nth.return_value = element
        frame = Mock(url='https://passport.baidu.com/login')
        frame.locator.return_value = locator
        page = Mock(frames=[frame])
        context = Mock(pages=[page])
        with patch.object(baidu_browser, 'status', return_value={'state': 'login_required'}):
            result = baidu_browser.login_qr(context)
            self.assertEqual(base64.b64decode(result['png_base64']), png)
            element.screenshot.assert_called_once_with(type='png', timeout=3000)
            element.is_visible.return_value = False
            self.assertEqual(baidu_browser.login_qr(context)['code'], 'qr_not_ready')
        with patch.object(baidu_browser, 'status', return_value={'state': 'browser_ready'}):
            self.assertEqual(baidu_browser.login_qr(context)['code'], 'qr_not_ready')
        frame.url = 'https://example.invalid/'
        with patch.object(baidu_browser, 'status', return_value={'state': 'login_required'}):
            self.assertEqual(baidu_browser.login_qr(context)['code'], 'qr_not_ready')
