import datetime as dt
import unittest
from client import normalize_keyword, parse_trends, SanitizedClientError

class KeywordNormalizationTests(unittest.TestCase):
    def test_han_and_boundaries(self):
        for value in ['杭  州', ' 杭\u3000州 ', '杭\n州', '杭\t州']:
            self.assertEqual(normalize_keyword(value), '杭州')
        for value in ['New York', 'A 1', '维生素 B', 'A B']:
            self.assertEqual(normalize_keyword(value), value)

    def parse(self, returned, requested):
        return parse_trends({'hot_list':[{'keyword':word,'search_hot_list':[]} for word in returned]},requested,dt.date(2026,10,19),dt.date(2026,10,25))

    def test_preserves_returned_label_and_input(self):
        _, quality=self.parse(['杭  州'], ['杭州'])
        self.assertEqual(quality['returned_keywords'], {'杭州':'杭  州'})
        self.assertEqual(quality['missing_keywords'], [])

    def test_ambiguity_and_different_words_rejected(self):
        for returned,requested,code in [(['杭州','杭 州'],['杭州'],'duplicate_keyword'),(['杭州'],['杭州','杭 州'],'duplicate_keyword'),(['杭州市'],['杭州'],'unexpected_keyword'),(['AB'],['A B'],'unexpected_keyword')]:
            with self.assertRaises(SanitizedClientError) as caught:
                self.parse(returned,requested)
            self.assertEqual(caught.exception.code,code)

    def test_query_many_normalizes_platform_keyword_whitespace(self):
        from client import DouyinIndexClient
        from test_client import FixtureSession, fixture, response
        for returned in ('杭  州', '杭\u3000州'):
            with self.subTest(returned=returned):
                payload = fixture()
                payload['data']['hot_list'][0]['keyword'] = returned
                session = FixtureSession(response(payload))
                result = DouyinIndexClient(session).query_many(['杭州'], '2025-01-01', '2025-01-07')
                self.assertTrue(result['metadata']['complete'])
                self.assertEqual(len(result['rows']), 7)
                self.assertTrue(all(row['keyword'] == '杭州' for row in result['rows']))
                self.assertEqual(len(session.calls), 1)
