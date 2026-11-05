"""Offline hotspot topic parsing, budget, and pause rules. No browser and no network."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from hub_store import Store
from topic_fetch import (
    DAILY_LIMIT, MIN_GAP_SECONDS, PAUSE_CODES, TopicFetchError, _FETCH_JS, parse_board, parse_tags,
    plan_round, request_gate, run_page_request,
)


class ParseTests(unittest.TestCase):
    def test_missing_numbers_stay_missing_and_zero_stays_zero(self):
        items, fields = parse_board({'code': 0, 'data': {'objs': [
            {'challenge_id': '1', 'challenge_name': '有热度', 'score': 0, 'publish_cnt': 3},
            {'challenge_id': '2', 'challenge_name': '缺字段'},
        ]}})
        self.assertEqual(items[0]['hot_value'], 0)
        self.assertEqual(items[0]['publish_count'], 3)
        self.assertIsNone(items[0]['play_count'])
        self.assertIsNone(items[1]['hot_value'])
        self.assertIsNone(items[1]['publish_count'])
        self.assertIsNone(items[1]['play_count'])
        self.assertNotIn('cover_url', fields)
        self.assertEqual(items[1]['cover'], None)

    def test_code_eight_pauses_as_logged_out(self):
        with self.assertRaises(TopicFetchError) as caught:
            parse_board({'code': 8, 'data': {'objs': []}})
        self.assertEqual(caught.exception.code, 'not_logged_in')
        self.assertIn(caught.exception.code, PAUSE_CODES)

    def test_nested_tags_and_billboard_list_are_accepted(self):
        tags = parse_tags({'code': 0, 'data': {'tag_list': [
            {'id': 1, 'name': '美食', 'children': [{'id': 2, 'label': '火锅'}]},
        ]}})
        self.assertEqual([item['name'] for item in tags], ['美食', '火锅'])
        items, _ = parse_board({'code': 0, 'data': {'billboard_list': [
            {'challenge_id': '9', 'challenge_name': '词'},
        ]}})
        self.assertEqual(items[0]['title'], '词')
        self.assertIsNone(items[0]['hot_value'])
        with self.assertRaises(TopicFetchError) as caught:
            parse_board({'code': 0, 'data': {'unexpected': {'value': 'secret-text'}}})
        self.assertEqual(caught.exception.code, 'schema_mismatch')

    def test_non_json_summary_keeps_no_body_text(self):
        from topic_fetch import summarize_response
        summary = summarize_response(
            {'http_status': 200, 'url': 'https://douhot.douyin.com/login?token=abcdef',
             'content_type': 'text/html; charset=utf-8', 'body': '<html>secret</html>'},
            {'method': 'GET', 'url': 'https://douhot.douyin.com/douhot/v1/material/content_tag'},
            {'hooked': False, 'path': '/'}, json_ok=False)
        encoded = json.dumps(summary)
        self.assertFalse(summary['json'])
        self.assertEqual(summary['body_length'], len('<html>secret</html>'))
        self.assertNotIn('secret', encoded)
        self.assertNotIn('abcdef', encoded)
        self.assertEqual(summary['content_type'], 'text/html')
        self.assertEqual(summary['final_path'], '/login')

    def test_tags_replace_hardcoded_names(self):
        tags = parse_tags({'code': 0, 'data': [
            {'id': 11, 'name': '美食'}, {'tag_id': '12', 'tag_name': '旅行'}, {'name': '全部'},
        ]})
        self.assertEqual(tags, [{'id': '11', 'name': '美食'}, {'id': '12', 'name': '旅行'}])

    def test_plan_keeps_both_overall_ranks_and_one_vertical(self):
        tags = [{'id': '11', 'name': '美食'}, {'id': '12', 'name': '旅行'}]
        first = plan_round(tags, 0, include_tags=False)
        self.assertEqual([job['tag_name'] for job in first], ['全部', '全部', '美食'])
        self.assertEqual([job['rank'] for job in first], ['total', 'rising', 'total'])
        second = plan_round(tags, 1, include_tags=True)
        self.assertEqual(second[0]['action'], 'tags')
        self.assertEqual(second[-1]['tag_name'], '旅行')
        self.assertEqual(second[-1]['rank'], 'rising')

    def test_budget_blocks_the_thirty_first_request_and_the_gap(self):
        now = 1_000_000
        self.assertEqual(request_gate(now, None, 0), 'ok')
        self.assertEqual(request_gate(now, now - 14, 1), 'interval')
        self.assertEqual(request_gate(now, now - MIN_GAP_SECONDS, 1), 'ok')
        self.assertEqual(request_gate(now, None, DAILY_LIMIT), 'daily_limit')

    def test_page_script_does_not_read_cookies_and_classifies_login(self):
        self.assertNotIn('cookie', _FETCH_JS.lower())

        class Page:
            def __init__(self):
                self.scripts = []

            def get_current_url(self):
                return 'https://douhot.douyin.com/topic'

            def load_url(self, url):
                raise AssertionError(url)

            def evaluate_js(self, script):
                self.scripts.append(script)
                if 'native code' in script:
                    return {'hooked': True, 'path': '/topic'}
                if 'fetch(' in script:
                    return {'state': 'started'}
                return {'state': 'done', 'http_status': 200, 'content_type': 'application/json',
                        'url': 'https://douhot.douyin.com/douhot/v1/material/challenge_billboard?a_bogus=secret-value',
                        'body': json.dumps({'code': 8, 'message': '未登录', 'token': 'should-not-appear'})}

        page = Page()
        ticks = {'n': 0}

        def clock():
            ticks['n'] += 1
            return ticks['n']

        payload, summary = run_page_request(
            page, {'method': 'GET', 'url': 'https://douhot.douyin.com/douhot/v1/material/content_tag'},
            sleep=lambda _seconds: None, clock=clock)
        with self.assertRaises(TopicFetchError) as caught:
            parse_board(payload)
        self.assertEqual(caught.exception.code, 'not_logged_in')
        self.assertTrue(all('cookie' not in script.lower() for script in page.scripts))
        self.assertEqual(summary['business']['code'], 8)
        self.assertEqual(summary['request']['through_page_fetch'], True)
        self.assertEqual(summary['request']['fetch_hooked'], True)
        self.assertIn('a_bogus', summary['request']['extra_query_keys'])
        self.assertNotIn('secret-value', json.dumps(summary))
        self.assertNotIn('should-not-appear', json.dumps(summary))


class StoreTests(unittest.TestCase):
    def test_saved_row_keeps_missing_counts_null(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(Path(temp.name) / 'hub.sqlite3')
        self.addCleanup(store.close)
        store.save_topic_snapshot({
            'complete': True, 'rank': 'total', 'window': '1d', 'category': '全部',
            'source': 'https://douhot.douyin.com/douhot/v1/material/challenge_billboard',
            'fetched_at': '2026-11-05T12:00:00+08:00',
            'items': [{'item_key': 'id:1', 'title': '合成话题', 'position': 1, 'hot_value': None,
                       'publish_count': None, 'play_count': 0, 'cover': None}],
        })
        item = store.latest_topics(rank='total', window='1d', category='全部')['items'][0]
        self.assertIsNone(item['hot_value'])
        self.assertIsNone(item['publish_count'])
        self.assertEqual(item['play_count'], 0)
        self.assertIsNone(item['cover'])
        self.assertEqual(store.get('daily'), None)
