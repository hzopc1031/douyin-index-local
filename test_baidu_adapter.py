"""Synthetic fixtures only; no browser, credentials, or external network."""
import copy
import datetime as dt
import json
import unittest
from urllib.parse import parse_qs, urlsplit

import baidu_adapter as b

PTBK = 'abcdefghijk0123456789,'


def query():
    end = dt.datetime.now(b.TZ).date()-dt.timedelta(days=1)
    return dict(platform='baidu', keywords=['合成 & 词'],
                start_date=(end-dt.timedelta(days=6)).isoformat(), end_date=end.isoformat())


def fixture(plain='12,23,34,45,56,67,78'):
    q = query()
    series = {'startDate': q['start_date'], 'endDate': q['end_date'], 'type': 'day',
              'data': plain.translate(str.maketrans(PTBK[11:], PTBK[:11]))}
    return {'uniqid': 'synthetic-id', 'userIndexes': [{'word': [{'name': q['keywords'][0], 'wordType': 1}],
        'all': dict(series), 'pc': dict(series), 'wise': dict(series)}]}


def extra_shape():
    return dict(http_status=200, business_status=0, body_fields=['data', 'status'],
                data_fields=['index', 'uniqid'], index_type='array', index_length=1,
                user_indexes_present=True, user_indexes_type='array', user_indexes_length=1,
                user_indexes_item_fields=[[{'field': 'word', 'type': 'string'}]],
                index_items=[dict(field_names=['data', 'endDate', 'key', 'startDate'], data_length=12,
                                  start_date=query()['start_date'], end_date=query()['end_date'])],
                ptbk_length=6, decoded_points=7, empty_points=1, used_fallback=False)


def response(data, status=0):
    return 200, 'application/json', json.dumps({'status': status, 'data': data})


class Browser:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url):
        self.calls.append(url)
        return self.responses.pop(0)


class AdapterTests(unittest.TestCase):
    def test_extra_shapes_public_boundary(self):
        q = query()
        rows = b.parse_index(fixture(), PTBK, q)['rows']
        extra = [dict(keyword=q['keywords'][0], device='all', date=q['start_date'], value=None, value_status='missing')]
        class Page:
            def __init__(self, shape): self.shape = shape
            def query(self, payload):
                return {'rows': rows, 'news_rows': extra, **({'news_shape': self.shape} if self.shape is not None else {})}
        shape = extra_shape()
        result = b.collect_page(q, Page(shape), news_enabled=True)
        self.assertEqual(b.public_result(result)['metadata']['extra_index_shapes'], {'news': [shape]})
        for bad in (dict(shape, cookie='private'), dict(shape, index_items=[dict(shape['index_items'][0], data='private')]), dict(shape, data_fields=['private'])):
            result = b.collect_page(q, Page(bad), news_enabled=True)
            public = b.public_result(result)
            self.assertNotIn('extra_index_shapes', public['metadata'])
            self.assertNotIn('private', json.dumps(public))
        self.assertNotIn('extra_index_shapes', b.public_result(b.collect_page(q, Page(None), news_enabled=True))['metadata'])

    def test_extra_shapes_collect_each_chunk(self):
        q = query()
        q['start_date'] = (dt.date.fromisoformat(q['end_date']) - dt.timedelta(days=399)).isoformat()
        shape = extra_shape()
        class Page:
            def query(self, chunk):
                first = dt.date.fromisoformat(chunk['start_date'])
                days = (dt.date.fromisoformat(chunk['end_date']) - first).days + 1
                rows = [dict(keyword=word, device=device, date=(first + dt.timedelta(days=i)).isoformat(),
                             search_index=None, value_status='missing') for word in chunk['keywords']
                        for device in ('all', 'pc', 'wise') for i in range(days)]
                return {'rows': rows, 'feed_rows': [], 'feed_shape': shape}
        result = b.public_result(b.collect_page(q, Page(), feed_enabled=True, sleep=lambda _: None))
        self.assertEqual(result['metadata']['extra_index_shapes']['feed'], [shape, shape])

    def test_missing_news_rows_is_response_schema(self):
        import baidu_adapter as b
        for answer in ({'rows': []}, {'rows': [], 'news_rows': None}):
            class MissingPage:
                def query(self, query):
                    return answer
            with self.assertRaises(b.BaiduError) as caught:
                b.collect_page(query(), MissingPage(), news_enabled=True)
            self.assertEqual(caught.exception.code, 'response_schema')

    def test_feed_page_bridge_normalizes_and_validates(self):
        q = query()
        search = b.parse_index(fixture(), PTBK, q)['rows']
        feed = [dict(keyword=q['keywords'][0], device='all', date=q['start_date'], value=None, value_status='missing')]
        class Page:
            def query(self, payload):
                assert payload['_feed_enabled'] is True
                return {'rows': search, 'feed_rows': feed}
        result = b.collect_page(q, Page(), feed_enabled=True)
        self.assertEqual(b.public_result(result)['feed_rows'], feed)
        result['feed_rows'] = feed * 2
        from hub_store import HubError
        with self.assertRaises(HubError):
            b.public_result(result)

    def test_news_page_bridge_and_validation(self):
        q = query()
        search = b.parse_index(fixture(), PTBK, q)['rows']
        news = [dict(keyword=q['keywords'][0], device='all', date=q['start_date'], value=None, value_status='missing')]
        class Page:
            def query(self, payload):
                assert payload['_news_enabled'] is True and payload['_feed_enabled'] is True
                return {'rows': search, 'feed_rows': news, 'news_rows': news}
        result = b.collect_page(q, Page(), feed_enabled=True, news_enabled=True)
        self.assertEqual(b.public_result(result)['news_rows'], news)
        result['news_rows'] = news * 2
        from hub_store import HubError
        with self.assertRaises(HubError):
            b.public_result(result)

    def test_csv_escapes_formula_for_each_device(self):
        adapter = b.BaiduAdapter()
        data = {'keyword': '=合成词', 'device': 'all_devices', 'points': [
            {'date': '2026-01-01', 'value': None, 'fetched_at': None, 'device': device, 'value_status': 'missing'}
            for device in ('all', 'pc', 'wise')]}
        rows = list(adapter.csv_rows(data))
        self.assertEqual([row[3] for row in rows[1:]], ["'=合成词"] * 3)
        self.assertEqual([row[8] for row in rows[1:]], ['all', 'pc', 'wise'])
        self.assertTrue(all(row[5:7] == ['', ''] for row in rows[1:]))

    def test_related_entry_keys_logged_with_job_on_success_and_failure(self):
        from unittest.mock import Mock, patch
        import baidu_browser
        worker = Mock()
        worker.call.side_effect = lambda action: action(Mock())
        page = Mock(url='https://index.baidu.com/')
        body = {**query(), 'kind': 'related', '_budget_day': dt.datetime.now(b.TZ).date().isoformat(), '_job_id': 'a' * 32}
        graph = {'period': {'start_date': '2026-10-26', 'end_date': '2026-11-01', 'days': 7}, 'wordlist': [{'wordGraph': []}]}
        diag = dict(reason='related_period', request_start=None, request_end=None, response_start=None, response_end=None,
                    word_type=None, name_matches=False, ptbk_length=0, decoded_points=None, data_length=None,
                    data_type='object', datelist_sent='none', period_returned=True,
                    period_shape={'type': 'string', 'length': 3, 'pattern': '9Ca'}, entry_keys={'word': 'string'})
        for result, ok in [({'status': 'ok', 'data': graph, 'meta': {'entry_keys': {'word': 'string'}}}, True),
                           ({'status': 'response_schema', 'schema_digest': {'type': 'object', 'keys': {}}, 'schema_diag': diag}, False)]:
            page.evaluate.return_value = result
            with patch.object(baidu_browser, 'browser', return_value=worker), patch.object(baidu_browser, '_page', return_value=page), \
                 patch.object(baidu_browser, 'wait_ready'), patch.object(baidu_browser, 'status', return_value={'state': 'browser_ready'}), \
                 self.assertLogs('index_hub.baidu', level='WARNING') as logs:
                answer = b.bridge_command('baidu_query', body)
            self.assertEqual(answer['ok'], ok)
            self.assertIn('baidu_related_fields job=' + 'a' * 32 + ' fields word:string', str(logs.output))

    def test_lagged_series_and_device_cutoff(self):
        for trailing in (False, True):
            raw = fixture('12,,34,45,56,,' if trailing else '12,,34,45,56')
            if not trailing:
                for device in ('all', 'pc', 'wise'):
                    raw['userIndexes'][0][device]['endDate'] = (dt.date.fromisoformat(query()['end_date']) - dt.timedelta(days=2)).isoformat()
            result = b.public_result(b.parse_index(raw, PTBK, query()))
            self.assertEqual(len(result['rows']), 15)
            self.assertIsNone(result['rows'][1]['search_index'])
            self.assertTrue(result['metadata']['lagged'])
            self.assertFalse(result['metadata']['complete'])
            self.assertIn('（滞后）', result['metadata']['note'])
        raw = fixture()
        raw['userIndexes'][0]['pc']['data'] = fixture('12,23,34,45,56,,')['userIndexes'][0]['pc']['data']
        self.assertEqual(len(b.parse_index(raw, PTBK, query())['rows']), 15)
        q = {**query(), 'keywords': query()['keywords'] + ['未收录']}
        result = b.public_result(b.parse_index(raw, PTBK, q))
        self.assertTrue(result['metadata']['lagged'])
        self.assertEqual(result['metadata']['data_end_date'], (dt.date.fromisoformat(q['end_date']) - dt.timedelta(days=2)).isoformat())
        self.assertEqual(sum(r['keyword'] == '未收录' for r in result['rows']), 21)

    def test_reject_range_and_identity_changes(self):
        for field, value in [('startDate', (dt.date.fromisoformat(query()['start_date']) + dt.timedelta(days=1)).isoformat()),
                             ('endDate', (dt.date.fromisoformat(query()['end_date']) + dt.timedelta(days=1)).isoformat()),
                             ('endDate', '2026-02-30')]:
            raw = fixture()
            raw['userIndexes'][0]['all'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(b.BaiduError):
                b.parse_index(raw, PTBK, query())
        for field, value in [('name', 'wrong'), ('wordType', 2)]:
            raw = fixture()
            raw['userIndexes'][0]['word'][0][field] = value
            with self.assertRaises(b.BaiduError):
                b.parse_index(raw, PTBK, query())

    def test_multiple_slices_preserve_gaps(self):
        end = dt.date.fromisoformat(query()['end_date'])
        q = {**query(), 'start_date': (end - dt.timedelta(days=399)).isoformat()}
        class Page:
            def __init__(self, truncate_first): self.calls = 0; self.truncate_first = truncate_first
            def query(self, chunk):
                self.calls += 1
                start, last = (dt.date.fromisoformat(chunk[k]) for k in ('start_date', 'end_date'))
                if (self.calls == 1) == self.truncate_first: last -= dt.timedelta(days=2)
                return [dict(keyword=word, device=device, date=(start+dt.timedelta(days=i)).isoformat(), search_index=1, value_status='available')
                        for word in chunk['keywords'] for device in ('all', 'pc', 'wise') for i in range((last-start).days+1)]
        for first in (False, True):
            result = b.public_result(b.collect_page(q, Page(first), sleep=lambda _: None))
            self.assertEqual(len(result['rows']), 398 * 3)
            self.assertFalse(result['metadata']['complete'])
            self.assertEqual(result['metadata']['lagged'], not first)
            self.assertIn('部分日期段' if first else '（滞后）', result['metadata']['note'])

    def test_two_requests_encoded_and_projected(self):
        browser = Browser(response(fixture()), response(PTBK))
        result = b.public_result(b.collect(query(), browser))
        self.assertEqual(len(browser.calls), 2)
        first = urlsplit(browser.calls[0])
        self.assertEqual(first.netloc, 'index.baidu.com')
        self.assertEqual(first.path, '/api/SearchApi/index')
        params = parse_qs(first.query)
        self.assertEqual(json.loads(params['word'][0]), [[{'name': query()['keywords'][0], 'wordType': 1}]])
        self.assertEqual(params['area'], ['0'])
        self.assertEqual(urlsplit(browser.calls[1]).path, '/Interface/api/ptbk')
        self.assertEqual(result['rows'][0]['search_index'], 12)
        self.assertTrue(result['metadata']['complete'])
        self.assertNotIn('uniqid', json.dumps(result))

    def test_missing_and_explicit_page_absence_never_zero(self):
        result = b.public_result(b.parse_index(fixture('12,,0,45,56,67,78'), PTBK, query()))
        self.assertIsNone(result['rows'][1]['search_index'])
        self.assertEqual(result['rows'][1]['value_status'], 'missing')
        self.assertEqual(result['rows'][2]['search_index'], 0)  # Explicit numeric zero is not an absence.
        self.assertFalse(result['metadata']['complete'])
        for state in ('unindexed', 'low_volume'):
            result = b.public_result(b.page_absence(query(), state))
            self.assertTrue(all(r['search_index'] is None and r['value_status'] == state for r in result['rows']))
        result = b.parse_index({'userIndexes': []}, PTBK, query())
        self.assertEqual({r['value_status'] for r in result['rows']}, {'missing'})

    def test_errors_stop_without_retry_at_either_step(self):
        cases = [(response({}, n), c) for n, c in ((10000, 'login_required'),
                 (10001, 'rate_limited'), (10002, 'bad_request'), (10018, 'abnormal_access'), (999, 'business_error'))]
        cases += [((200, 'text/html', '<html>synthetic-secret</html>'), 'verification_required'),
                  ((302, 'text/plain', 'synthetic-secret'), 'http_error')]
        for bad, code in cases:
            for step in (1, 2):
                with self.subTest(code=code, step=step):
                    browser = Browser(*([response(fixture())] if step == 2 else []), bad)
                    with self.assertRaises(b.BaiduError) as cm:
                        b.collect(query(), browser)
                    self.assertEqual(cm.exception.code, code)
                    self.assertNotIn('synthetic-secret', str(cm.exception))
                    self.assertEqual(len(browser.calls), step)

    def test_strict_mapping_json_series_and_identity(self):
        for table, encrypted in [('', ''), ('abc', 'a'), ('aa12', 'a'), ('ab11', 'a'), (PTBK, '!')]:
            with self.subTest(table=table), self.assertRaises(b.BaiduError):
                b.decrypt(table, encrypted)
        for text in ('{"status":0,"status":0,"data":{}}', '{"status":true,"data":{}}', '{"status":0,"data":NaN}'):
            with self.assertRaises(b.BaiduError):
                b.response_data((200, 'application/json', text))
        for plain in ('1,2', '1,2,3,4,5,6,7,8', '1,2,-3,4,5,6,7'):
            with self.assertRaises(b.BaiduError):
                b.parse_index(fixture(plain), PTBK, query())
        for field, value in [('startDate', '2020-01-01'), ('endDate', '2020-01-01'), ('type', 'week')]:
            raw = fixture()
            raw['userIndexes'][0]['all'][field] = value
            with self.assertRaises(b.BaiduError):
                b.parse_index(raw, PTBK, query())
        raw = fixture()
        raw['userIndexes'] *= 2
        with self.assertRaises(b.BaiduError):
            b.parse_index(raw, PTBK, query())
        raw = fixture()
        raw['userIndexes'][0]['word'][0]['name'] = 'other'
        with self.assertRaises(b.BaiduError):
            b.parse_index(raw, PTBK, query())

    def test_word_extra_fields_and_required_types(self):
        raw = fixture()
        word = raw['userIndexes'][0]['word'][0]
        word['extra'] = 'synthetic'
        self.assertEqual(b.parse_index(raw, PTBK, query())['rows'][0]['search_index'], 12)
        for bad in (True, '1', None):
            word['wordType'] = bad
            with self.assertRaises(b.BaiduError):
                b.parse_index(raw, PTBK, query())

    def test_scope_and_public_trust_boundary(self):
        from hub_store import HubError
        for patch in ({'keywords': ['a', 'a']}, {'cookie': 'synthetic'}, {'device': 'pc'},
                      {'end_date': '2020-01-01'}, {'start_date': '2020-01-01'}):
            with self.assertRaises(HubError):
                b.validate_query(query() | patch)
        clean = b.parse_index(fixture(), PTBK, query())
        dirty = copy.deepcopy(clean)
        dirty['metadata']['cookie'] = dirty['rows'][0]['raw'] = 'synthetic-secret'
        self.assertNotIn('synthetic-secret', json.dumps(b.public_result(dirty)))
        for mutation in ('duplicate', 'bool', 'dimension', 'status', 'complete'):
            bad = copy.deepcopy(clean)
            if mutation == 'duplicate': bad['rows'][1] = bad['rows'][0]
            if mutation == 'bool': bad['rows'][0]['search_index'] = True
            if mutation == 'dimension': bad['metadata']['device'] = 'pc'
            if mutation == 'status': bad['rows'][0]['value_status'] = 'low_volume'
            if mutation == 'complete': bad['metadata']['complete'] = False
            with self.subTest(mutation=mutation), self.assertRaises(HubError):
                b.public_result(bad)

    def test_login_window_uses_private_separate_context_without_launch(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock, patch
        import baidu_window
        webview = Mock()
        webview.settings = {}
        with tempfile.TemporaryDirectory() as directory, patch.dict('sys.modules', {'webview': webview}), patch.object(baidu_window, 'state_dir', return_value=Path(directory)):
            baidu_window.main()
        webview.start.assert_called_once_with(private_mode=True, debug=False, http_server=False)
        self.assertIsNone(webview.settings['REMOTE_DEBUGGING_PORT'])
        self.assertFalse(webview.settings['IGNORE_SSL_ERRORS'])
        self.assertNotIn('js_api', webview.create_window.call_args.kwargs)

    def test_live_bridge_needs_login(self):
        self.assertTrue(b.bridge_command('baidu_status')['live_enabled'])
        from unittest.mock import Mock, patch
        with patch('baidu_browser.browser', return_value=Mock(started=False, last_state='login_required', call=Mock(side_effect=b.BaiduError('login_required')))):
            self.assertEqual(b.bridge_command('baidu_query', {**query(), '_budget_day': dt.datetime.now(b.TZ).date().isoformat()})['code'], 'login_required')


if __name__ == '__main__':
    unittest.main()


class ExpandedScopeTests(unittest.TestCase):
    def test_five_words_731_days_three_chunks_and_devices(self):
        from urllib.parse import parse_qs, urlsplit
        q = dict(platform='baidu', start_date='2023-03-01', end_date='2025-02-28')
        q['keywords'] = ['词一', '词二', '词三', '词四', '词五']
        class Synthetic:
            def __init__(self): self.calls = []
            def get(self, url):
                self.calls.append(url)
                if '/ptbk?' in url: return response(PTBK)
                params = parse_qs(urlsplit(url).query)
                start, end = params['startDate'][0], params['endDate'][0]
                days = (dt.date.fromisoformat(end)-dt.date.fromisoformat(start)).days+1
                encoded = ','.join(['1']*days).translate(str.maketrans(PTBK[11:], PTBK[:11]))
                series = dict(startDate=start, endDate=end, type='day', data=encoded)
                return response(dict(uniqid='synthetic', userIndexes=[dict(word=word, all=series, pc=series, wise=series)
                    for word in json.loads(params['word'][0])]))
        transport = Synthetic()
        sleeps = []
        from unittest.mock import patch
        real_datetime = dt.datetime
        class Frozen(real_datetime):
            @classmethod
            def now(cls, tz=None): return cls(2025, 3, 1, tzinfo=tz)
        with patch('hub_store.dt.datetime', Frozen):
            result = b.public_result(b.collect(q, transport, sleep=sleeps.append))
        self.assertEqual((len(transport.calls), sleeps, len(result['rows'])), (6, [8, 8], 5*731*3))
        self.assertEqual({r['device'] for r in result['rows']}, {'all', 'pc', 'wise'})
        self.assertTrue(result['metadata']['complete'])

    def test_invalid_chunk_granularity_rejected(self):
        raw = fixture()
        raw['userIndexes'][0]['wise']['type'] = 'week'
        with self.assertRaises(b.BaiduError):
            b.parse_index(raw, PTBK, query())


class WordGraphTests(unittest.TestCase):
    def test_validation_and_projection(self):
        from hub_store import HubError
        q = b.validate_insight({**query(), 'kind': 'related'}, latest_related_date='2000-01-01')
        self.assertEqual(q['end_date'], query()['end_date'])
        self.assertEqual(b.validate_insight({**query(), 'kind': 'audience'})['kind'], 'audience')
        self.assertIn('audience', b.BaiduAdapter.description['modules'])
        for body in ({**query(), 'kind': 'unknown'}, {**query(), 'kind': 'audience', 'keywords': ['a', 'b']}, {**query(), 'kind': 'related', 'keywords': ['a', 'b']}):
            with self.assertRaises(HubError):
                b.validate_insight(body)
        with self.assertRaises(HubError) as caught:
            b.validate_insight({**query(), 'kind': 'unknown'})
        self.assertEqual(caught.exception.status, 422)
        graph = {'wordlist': [{'wordGraph': [{'word': '合成', 'unknown': 4}] * 100}]}
        data = b.project_wordgraph(graph)
        self.assertEqual(len(data['search_related']), 100)
        self.assertEqual(data['search_related'][0], {'keyword': '合成', 'relation_score': None, 'search_index': None, 'change_ratio': None})
        self.assertEqual(data['content_related'], [])
        row = b.project_wordgraph({'wordlist': [{'wordGraph': [{'word': '合成', 'keyword': {'secret': 'x'}, 'sim': 0.5, 'pv': 12, 'ratio': -0.2}]}]})['search_related'][0]
        self.assertEqual(row, {'keyword': '合成', 'relation_score': 0.5, 'search_index': 12, 'change_ratio': -0.2})
        for field in ('pv', 'sim', 'ratio'):
            with self.assertRaises(b.BaiduError):
                b.project_wordgraph({'wordlist': [{'wordGraph': [{'word': '合成', field: '12'}]}]})
        self.assertEqual(b.project_wordgraph({'wordlist': [{'wordGraph': []}]})['search_related'], [])
        for invalid in ({}, {'wordlist': []}, {'wordlist': [{'wordGraph': [{}]}]},
                        {'wordlist': [{'wordGraph': [{'word': '合成'}] * 101}]}):
            with self.assertRaises(b.BaiduError):
                b.project_wordgraph(invalid)


class AudienceSyntheticTests(unittest.TestCase):
    def test_projection_and_unknown_codes(self):
        fixture = {'gender': [{'desc': '男', 'rate': 0.6, 'tgi': 120}], 'age': [{'desc': '青年', 'rate': 0.5, 'tgi': None}], 'interest': [], 'prov': {'917': 1000, '999999': 12}, 'city': {'138': 800}}
        with self.assertLogs('index_hub.baidu') as logs:
            result = b.project_audience(fixture)
        self.assertEqual(result['dimensions'][0]['labels'][0], {'name': '男', 'value': 0.6, 'tgi': 120})
        self.assertEqual(result['dimensions'][3]['labels'], [{'name': '浙江', 'value': 1000, 'tgi': None}])
        self.assertEqual(result['dimensions'][4]['labels'][0]['name'], '杭州')
        self.assertIn('region_code_unknown', str(logs.output))
        with self.assertLogs('index_hub.baidu') as logs, self.assertRaises(b.BaiduError):
            b.project_audience({**fixture, 'city': {'999999': 42}})
        self.assertIn('all_unknown', str(logs.output))
        self.assertNotIn('999999', str(logs.output))
