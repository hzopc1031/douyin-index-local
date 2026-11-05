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
                if 'fetch(' in script:
                    return {'state': 'started'}
                return {'state': 'done', 'http_status': 200,
                        'url': 'https://douhot.douyin.com/douhot/v1/material/challenge_billboard',
                        'body': json.dumps({'code': 8})}

        page = Page()
        ticks = {'n': 0}

        def clock():
            ticks['n'] += 1
            return ticks['n']

        payload = run_page_request(page, {'method': 'GET', 'url': 'https://douhot.douyin.com/douhot/v1/material/content_tag'},
                                   sleep=lambda _seconds: None, clock=clock)
        with self.assertRaises(TopicFetchError) as caught:
            parse_board(payload)
        self.assertEqual(caught.exception.code, 'not_logged_in')
        self.assertTrue(all('cookie' not in script.lower() for script in page.scripts))


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
