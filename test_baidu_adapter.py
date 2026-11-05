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


def response(data, status=0):
    return 200, 'application/json', json.dumps({'status': status, 'data': data})


class Browser:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url):
        self.calls.append(url)
        return self.responses.pop(0)


class AdapterTests(unittest.TestCase):
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
        for body in ({**query(), 'kind': 'audience'}, {**query(), 'kind': 'related', 'keywords': ['a', 'b']}):
            with self.assertRaises(HubError):
                b.validate_insight(body)
        with self.assertRaises(HubError) as caught:
            b.validate_insight({**query(), 'kind': 'audience'})
        self.assertEqual(caught.exception.status, 422)
        graph = {'wordlist': [{'wordGraph': [{'word': '合成', 'unknown': 4}] * 100}]}
        data = b.project_wordgraph(graph)
        self.assertEqual(len(data['search_related']), 100)
        self.assertEqual(data['search_related'][0], {'keyword': '合成', 'relation_score': None, 'search_index': None})
        self.assertEqual(data['content_related'], [])
        self.assertEqual(b.project_wordgraph({'wordlist': [{'wordGraph': []}]})['search_related'], [])
        for invalid in ({}, {'wordlist': []}, {'wordlist': [{'wordGraph': [{}]}]},
                        {'wordlist': [{'wordGraph': [{'word': '合成'}] * 101}]}):
            with self.assertRaises(b.BaiduError):
                b.project_wordgraph(invalid)
