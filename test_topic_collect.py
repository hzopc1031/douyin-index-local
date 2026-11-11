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
            {'label': '科技', 'value': 628, 'children': [
                {'label': '数码', 'value': 62801, 'children': []},
            ]},
        ]}})
        self.assertEqual(tags, [
            {'id': '628', 'name': '科技', 'child_ids': ['62801']},
            {'id': '62801', 'name': '科技 / 数码', 'parent_id': '628', 'parent_name': '科技'},
        ])
        items, _ = parse_board({'code': 0, 'data': {'billboard_list': [
            {'challenge_id': '9', 'challenge_name': '词'},
        ]}})
        self.assertEqual(items[0]['title'], '词')
        self.assertIsNone(items[0]['hot_value'])
        with self.assertRaises(TopicFetchError) as caught:
            parse_board({'code': 0, 'data': {'unexpected': {'value': 'secret-text'}}})
        self.assertEqual(caught.exception.code, 'schema_mismatch')

    def test_leaf_tags_are_nested_and_code_five_does_not_pause(self):
        from topic_fetch import board_body
        self.assertEqual(board_body('total', '', '')['tags'], [])
        self.assertEqual(board_body('total', '', '')['tag_version'], 'v2')
        self.assertEqual(board_body('total', '628', '', ['62801', '62808'])['tags'],
                         [{'value': 628, 'children': [{'value': 62801}, {'value': 62808}]}])
        self.assertEqual(board_body('total', '62801', '628')['tags'],
                         [{'value': 628, 'children': [{'value': 62801}]}])
        self.assertEqual(board_body('rising', '7', '')['tags'], [{'value': 7}])
        with self.assertRaises(TopicFetchError) as bad:
            board_body('total', '科技', '')
        self.assertEqual(bad.exception.code, 'invalid_params')
        with self.assertRaises(TopicFetchError) as caught:
            parse_board({'code': 5, 'message': '参数不合法', 'data': {}})
        self.assertEqual(caught.exception.code, 'invalid_params')
        self.assertNotIn('invalid_params', PAUSE_CODES)
        same_name = parse_tags({'code': 0, 'data': [
            {'label': '科技', 'value': 1, 'children': [{'label': '数码', 'value': 11}]},
            {'label': '生活', 'value': 2, 'children': [{'label': '数码', 'value': 21}]},
        ]})
        self.assertEqual([(item['name'], item.get('parent_id'), item.get('child_ids')) for item in same_name],
                         [('科技', None, ['11']), ('科技 / 数码', '1', None),
                          ('生活', None, ['21']), ('生活 / 数码', '2', None)])

    def test_same_parent_duplicate_leaves_get_id_suffix(self):
        tags = parse_tags({'code': 0, 'data': [
            {'label': '电影', 'value': 604, 'children': [
                {'label': '电影剧情解说', 'value': 60405},
                {'label': '电影剧情解说', 'value': 60407},
                {'label': '纪录电影解说', 'value': 60406},
            ]},
        ]})
        self.assertEqual([(item['name'], item.get('parent_id')) for item in tags],
                         [('电影', None),
                          ('电影 / 电影剧情解说 · 60405', '604'),
                          ('电影 / 电影剧情解说 · 60407', '604'),
                          ('电影 / 纪录电影解说', '604')])
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        from topic_board import category_names, tag_for, latest_topic_snapshot
        store = Store(Path(temp.name) / 'hub.sqlite3')
        self.addCleanup(store.close)
        store.set('topic_categories', tags)
        names = category_names(tags)
        self.assertIn('电影 / 纪录电影解说', names)
        self.assertIn('电影 / 电影剧情解说 · 60405', names)
        self.assertIn('电影 / 电影剧情解说 · 60407', names)
        self.assertNotIn('电影 / 电影剧情解说', names)
        self.assertEqual(tag_for(store, '电影 / 电影剧情解说 · 60405')['id'], '60405')
        self.assertEqual(tag_for(store, '电影 / 纪录电影解说')['id'], '60406')
        # Old short snapshots remain readable under the new label.
        store.save_topic_snapshot({
            'complete': True, 'rank': 'total', 'window': '1d', 'category': '纪录电影解说',
            'source': 'douhot.douyin.com', 'fetched_at': '2026-11-08T09:00:00+08:00',
            'items': [{'item_key': 'id:1', 'title': '样例', 'position': 1,
                       'hot_value': 1, 'publish_count': None, 'play_count': None, 'cover': None}],
        })
        snapshot = latest_topic_snapshot(store, rank='total', window='1d', category='电影 / 纪录电影解说')
        self.assertEqual([item['title'] for item in snapshot['items']], ['样例'])
        self.assertEqual(snapshot['category'], '电影 / 纪录电影解说')
        # Labels are also derived from already-saved short names plus parent_id.
        stored = [
            {'id': '628', 'name': '美食', 'child_ids': ['62804']},
            {'id': '62804', 'name': '美食教程', 'parent_id': '628'},
            {'id': '60405', 'name': '电影 / 电影剧情解说', 'parent_id': '604'},
            {'id': '60407', 'name': '电影 / 电影剧情解说', 'parent_id': '604'},
            {'id': '604', 'name': '电影', 'child_ids': ['60405', '60407']},
        ]
        labels = category_names(stored)
        self.assertIn('美食 / 美食教程', labels)
        self.assertIn('电影 / 电影剧情解说 · 60405', labels)
        self.assertIn('电影 / 电影剧情解说 · 60407', labels)
        store.set('topic_categories', stored)
        self.assertEqual(tag_for(store, '美食教程')['id'], '62804')
        self.assertEqual(tag_for(store, '美食 / 美食教程')['id'], '62804')

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

    def test_invalid_params_does_not_pause(self):
        from topic_board import record_topic_result
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(Path(temp.name) / 'hub.sqlite3')
        self.addCleanup(store.close)
        record_topic_result(store, {'action': 'board', 'rank': 'total', 'tag_name': '数码'},
                            {'ok': False, 'code': 'invalid_params', 'requested': True})
        self.assertIsNone(store.get('topic_paused'))
        self.assertEqual(store.get('topic_daily')['count'], 1)
        record_topic_result(store, {'action': 'board', 'rank': 'total', 'tag_name': '数码'},
                            {'ok': False, 'code': 'not_logged_in', 'requested': False})
        self.assertEqual(store.get('topic_paused')['code'], 'not_logged_in')

    def test_empty_board_is_remembered_without_a_fake_row(self):
        from topic_board import apply_empty_mark, record_topic_result, topic_view
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(Path(temp.name) / 'hub.sqlite3')
        self.addCleanup(store.close)
        record_topic_result(store, {'action': 'board', 'rank': 'total', 'tag_name': '科技'},
                            {'ok': True, 'requested': True, 'items': []})
        self.assertIsNone(store.get('topic_paused'))
        snapshot = apply_empty_mark(store, store.latest_topics(rank='total', window='1d', category='科技'))
        self.assertEqual(snapshot['items'], [])
        self.assertIsNotNone(snapshot['fetched_at'])
        old = dict(snapshot, fetched_at='2020-01-01T00:00:00+08:00', items=[{'title': '旧榜'}])
        self.assertEqual(apply_empty_mark(store, old)['items'], [])
        view = topic_view(snapshot, enabled=False)
        self.assertEqual(view['status'], 'available')
        self.assertEqual(view['status_label'], '该垂类暂无话题')
        self.assertFalse(view['enabled'])
