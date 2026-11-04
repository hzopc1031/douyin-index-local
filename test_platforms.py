import unittest

from hub_store import HubError, PLATFORMS, validate_query
from platforms import Placeholder, Registry, registry, require_platform


class PlatformTests(unittest.TestCase):
    def test_register_lookup_and_duplicate(self):
        platforms = Registry()
        adapter = Placeholder({'id': 'sample', 'available': False})
        platforms.register(adapter)
        self.assertIs(platforms.get('sample'), adapter)
        self.assertIsNone(platforms.get('missing'))
        with self.assertRaises(ValueError):
            platforms.register(adapter)

    def test_placeholders_and_unknown(self):
        self.assertEqual([p['id'] for p in PLATFORMS], ['douyin', 'baidu', 'google_trends'])
        self.assertEqual(PLATFORMS, registry.descriptions())
        for identifier in ('baidu', 'google_trends', 'unknown'):
            with self.subTest(identifier=identifier), self.assertRaises(HubError) as error:
                require_platform(identifier)
            self.assertEqual((error.exception.code, error.exception.status), ('platform_unavailable', 422))
            with self.assertRaises(HubError) as error:
                validate_query({'platform': identifier})
            self.assertEqual((error.exception.code, error.exception.status), ('platform_unavailable', 422))

    def test_douyin_descriptions_and_csv(self):
        adapter = require_platform('douyin')
        self.assertEqual(adapter.description, {
            'id': 'douyin', 'name': '抖音指数', 'available': True, 'metric': 'search_index',
            'metric_name': '搜索指数', 'unit': '指数', 'region': '全国', 'timezone': 'Asia/Shanghai',
            'description': '创作者中心关键词搜索指数；不代表实际搜索次数。'})
        rows = list(adapter.csv_rows({'keyword': '=word', 'points': [
            {'date': '2025-01-01', 'value': 0, 'composite_index': None, 'fetched_at': None}]}))
        self.assertEqual(rows, [
            ['platform', 'metric', 'region', 'keyword', 'date', 'search_index', 'composite_index', 'fetched_at'],
            ['douyin', 'search_index', '全国', "'=word", '2025-01-01', 0, '', None]])

    def test_douyin_validation_errors(self):
        for body, expected in [({}, 'invalid_keywords'),
                               ({'keywords': ['x', 'x']}, 'invalid_keywords'),
                               ({'keywords': ['x'], 'end_date': 'bad'}, 'invalid_dates'),
                               ({'extra': 1}, 'invalid_query')]:
            with self.subTest(body=body), self.assertRaises(HubError) as error:
                validate_query(body)
            self.assertEqual(error.exception.code, expected)
