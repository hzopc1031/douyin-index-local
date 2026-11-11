"""Synthetic region trend coverage; no live network or credentials."""
import copy
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

import baidu_adapter as b
from baidu_browser import PageTransport
from hub_store import HubError, Store, validate_query
from test_baidu_adapter import query, fixture, PTBK


class RegionTests(unittest.TestCase):
    def setUp(self):
        self.q = query()
        self.rows = b.parse_index(fixture(), PTBK, self.q)['rows']
        self.shape = dict(region='海南', http_status=200, business_status=0,
                          data_fields=['uniqid', 'userIndexes'], user_indexes_type='array',
                          user_indexes_length=0, ptbk_length=22, decoded_points=21,
                          empty_points=21, used_fallback=True)

    def test_validation_disabled_and_enabled(self):
        with patch.dict('os.environ', {'INDEX_HUB_BAIDU_REGION_ENABLED': '0'}):
            for extra in ({'regions': ['北京']}, {'extra': 1}):
                with self.assertRaisesRegex(HubError, '仅接受 platform、keywords、start_date、end_date。'):
                    validate_query(self.q | extra)
            class Page:
                def query(inner, q):
                    self.assertNotIn('_regions', q)
                    return self.rows
            self.assertNotIn('region_rows', b.collect_page(self.q, Page()))
        with patch.dict('os.environ', {'INDEX_HUB_BAIDU_REGION_ENABLED': '1'}):
            self.assertEqual(validate_query(self.q | {'regions': ['海南', '北京', '海南']})['regions'], ['海南', '北京'])
            for regions in ([], ['未知'], '北京', [None], ['北京'] * 6):
                with self.assertRaises(HubError):
                    validate_query(self.q | {'regions': regions})
            with self.assertRaises(HubError):
                validate_query(dict(self.q, platform='douyin', regions=['北京']))

    def test_collect_shapes_storage_and_disabled_read(self):
        shape = self.shape
        rows = self.rows
        class Page:
            def query(inner, payload):
                self.assertEqual(payload['_regions'], [{'name': '海南', 'code': '930'}])
                missing = [{**r, 'region': '海南', 'search_index': None, 'value_status': 'missing'} for r in rows]
                return {'rows': rows, 'region_rows': missing, 'region_shapes': [shape]}
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'INDEX_HUB_BAIDU_REGION_ENABLED': '1'}):
            q = validate_query(self.q | {'regions': ['海南']})
            result = b.collect_page(q, Page())
            self.assertFalse(result['metadata']['complete'])
            public = b.public_result(result)
            self.assertEqual(public['metadata']['extra_index_shapes']['regions'], [shape])
            bad = copy.deepcopy(result)
            bad['metadata']['extra_index_shapes']['regions'][0]['cookie'] = 'private'
            self.assertNotIn('extra_index_shapes', b.public_result(bad)['metadata'])
            store = Store(tmp + '/hub.sqlite')
            try:
                store.ingest(bad)
                saved = json.loads(store.db.execute('SELECT result FROM datasets').fetchone()[0])
                self.assertNotIn('private', json.dumps(saved))
                self.assertIsNone(store.cached(q))
                with patch.dict('os.environ', {'INDEX_HUB_BAIDU_REGION_ENABLED': '0'}):
                    adapter = b.BaiduAdapter()
                    self.assertEqual(adapter.series(store, q['keywords'][0])['points'][0]['value'], 12)
                    regional = adapter.series(store, q['keywords'][0], region='海南')
                    self.assertTrue(all(p['value'] is None for p in regional['points']))
                    self.assertEqual(list(adapter.csv_rows(regional))[1][2], '海南')
                    with self.assertRaises(HubError):
                        adapter.series(store, q['keywords'][0], region='不存在')
            finally:
                store.close()

    def test_complete_region_result_has_separate_cache(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'INDEX_HUB_BAIDU_REGION_ENABLED': '1'}):
            q = validate_query(self.q | {'regions': ['吉林']})
            class Page:
                def query(inner, payload):
                    self.assertEqual(payload['_regions'], [{'name': '吉林', 'code': '922'}])
                    return {'rows': self.rows, 'region_rows': [{**r, 'region': '吉林'} for r in self.rows], 'region_shapes': []}
            result = b.collect_page(q, Page())
            self.assertTrue(result['metadata']['complete'])
            store = Store(tmp + '/hub.sqlite')
            try:
                store.ingest(result)
                self.assertIsNotNone(store.cached(q))
                self.assertIsNone(store.cached(self.q))
            finally:
                store.close()

    def test_page_shape_logging_rejects_unsafe(self):
        page = Mock(url='https://index.baidu.com/')
        page.evaluate.return_value = dict(status='ok', rows=[], region_rows=[], region_shapes=[self.shape])
        payload = {'_regions': [{'name': '海南', 'code': '930'}]}
        with self.assertLogs('index_hub.baidu', level='INFO') as logs:
            output = PageTransport(page).query(payload)
        self.assertEqual(output['region_shapes'], [self.shape])
        self.assertIn('decoded_points', logs.output[0])
        page.evaluate.return_value['region_shapes'] = [dict(self.shape, cookie='private')]
        with self.assertNoLogs('index_hub.baidu', level='INFO'):
            self.assertEqual(PageTransport(page).query(payload)['region_shapes'], [])
