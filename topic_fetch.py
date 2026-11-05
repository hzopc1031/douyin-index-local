"""Parse hotspot topic responses and run one page-context request.

The browser page issues fetch() itself. This module does not sign requests,
read cookies, or retry. Missing numbers stay None.
"""

from __future__ import annotations

import json
import math
import time
from urllib.parse import urlsplit

DOUHOT_ORIGIN = 'https://douhot.douyin.com'
TAG_URL = DOUHOT_ORIGIN + '/douhot/v1/material/content_tag'
BOARD_URL = DOUHOT_ORIGIN + '/douhot/v1/material/challenge_billboard'
BOARD_SOURCE = BOARD_URL
FIELD_NAMES = ('challenge_id', 'challenge_name', 'score', 'publish_cnt', 'play_cnt', 'avg_play_cnt', 'cover_url')
PAUSE_CODES = frozenset({'not_logged_in', 'verification_required', 'rate_limited', 'access_denied', 'response_format'})
RANK_SUBTYPE = {'total': 2001, 'rising': 2002}
DATE_WINDOW_HOURS = 24
PAGE_SIZE = 20
MIN_GAP_SECONDS = 15
DAILY_LIMIT = 30
ROUND_FLOOR = 7200
ROUND_DEFAULT = 9000
ROUND_CEILING = 10800


class TopicFetchError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _number(value):
    """Return a finite non-negative number, None when the field is absent."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TopicFetchError('response_format', '话题数值字段无效。')
    if isinstance(value, float) and not math.isfinite(value):
        raise TopicFetchError('response_format', '话题数值字段无效。')
    if value < 0:
        raise TopicFetchError('response_format', '话题数值字段无效。')
    return value


def _text(value, limit):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > limit or any(ord(char) < 32 or ord(char) == 127 for char in text):
        return None
    return text


def _rows(payload):
    if not isinstance(payload, dict):
        raise TopicFetchError('response_format', '话题响应不是对象。')
    code = payload.get('code')
    if code == 8:
        raise TopicFetchError('not_logged_in', '热点宝未登录。')
    if isinstance(code, int) and code not in (0, None):
        message = payload.get('message') if isinstance(payload.get('message'), str) else ''
        if any(word in message for word in ('验证', '验证码', 'captcha')):
            raise TopicFetchError('verification_required', '热点宝要求验证。')
        if any(word in message for word in ('频繁', '限流', '稍后')):
            raise TopicFetchError('rate_limited', '热点宝限频。')
        raise TopicFetchError('response_format', '话题响应码无法识别。')
    data = payload.get('data')
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ('tags', 'content_tags', 'list', 'objs', 'billboard', 'challenge_list'):
            if isinstance(data.get(key), list):
                return data[key]
    raise TopicFetchError('schema_mismatch', '话题响应缺少列表。')


def parse_tags(payload):
    rows = _rows(payload)
    tags = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise TopicFetchError('schema_mismatch', '垂类项不是对象。')
        raw_id = row.get('id', row.get('tag_id', row.get('label_id')))
        name = _text(row.get('name', row.get('tag_name', row.get('label_name'))), 40)
        if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)) or name is None or name == '全部':
            continue
        identifier = str(raw_id).strip()
        if not identifier or len(identifier) > 32 or identifier in seen:
            continue
        seen.add(identifier)
        tags.append({'id': identifier, 'name': name})
    if not tags:
        raise TopicFetchError('schema_mismatch', '没有识别到垂类。')
    return tags


def parse_board(payload):
    rows = _rows(payload)
    items = []
    seen = set()
    present = set()
    for row in rows:
        if not isinstance(row, dict):
            raise TopicFetchError('schema_mismatch', '话题项不是对象。')
        present.update(key for key in FIELD_NAMES if key in row)
        title = _text(row.get('challenge_name'), 200)
        if title is None:
            continue
        raw_id = row.get('challenge_id')
        identifier = None if raw_id is None else str(raw_id).strip()
        if identifier is not None and (not identifier or len(identifier) > 64):
            raise TopicFetchError('schema_mismatch', '话题标识无效。')
        key = 'id:' + identifier if identifier else 'title:' + title
        if key in seen:
            raise TopicFetchError('schema_mismatch', '话题重复。')
        seen.add(key)
        cover = _text(row.get('cover_url'), 500)
        if cover is not None and not cover.startswith('https://'):
            cover = None
        items.append({
            'item_key': key,
            'title': title,
            'position': len(items) + 1,
            'hot_value': _number(row.get('score')),
            'publish_count': _number(row.get('publish_cnt')),
            'play_count': _number(row.get('play_cnt')),
            'avg_play': _number(row.get('avg_play_cnt')),
            'cover': cover,
        })
    return items, sorted(present)


def classify_http(status, final_url):
    if not isinstance(final_url, str) or urlsplit(final_url).hostname != 'douhot.douyin.com':
        return 'not_logged_in'
    lowered = final_url.lower()
    if any(token in lowered for token in ('captcha', 'verify', 'secsdk')):
        return 'verification_required'
    if status == 429:
        return 'rate_limited'
    if status == 403:
        return 'access_denied'
    if status in (401, 302, 303, 307, 308):
        return 'not_logged_in'
    if status != 200:
        return 'response_format'
    return None


def round_interval_seconds():
    import os
    raw = os.environ.get('INDEX_HUB_TOPIC_INTERVAL_SECONDS', str(ROUND_DEFAULT))
    try:
        value = int(raw) if isinstance(raw, str) and raw.isascii() and raw.isdecimal() else ROUND_DEFAULT
    except (TypeError, ValueError):
        value = ROUND_DEFAULT
    return min(ROUND_CEILING, max(ROUND_FLOOR, value))


def request_gate(now, last_request, daily_count):
    if daily_count >= DAILY_LIMIT:
        return 'daily_limit'
    if last_request is not None and now - last_request < MIN_GAP_SECONDS:
        return 'interval'
    return 'ok'


def plan_round(tags, cursor, *, include_tags):
    """全部 for both ranks, then one vertical rank. Tags refresh is its own request."""
    jobs = []
    if include_tags:
        jobs.append({'action': 'tags'})
    jobs.append({'action': 'board', 'rank': 'total', 'tag_id': '', 'tag_name': '全部'})
    jobs.append({'action': 'board', 'rank': 'rising', 'tag_id': '', 'tag_name': '全部'})
    if tags:
        choice = tags[cursor % len(tags)]
        jobs.append({'action': 'board', 'rank': 'total' if cursor % 2 == 0 else 'rising',
                     'tag_id': choice['id'], 'tag_name': choice['name']})
    return jobs


def board_body(rank, tag_id):
    if rank not in RANK_SUBTYPE:
        raise TopicFetchError('invalid_parameters', '榜单只能是总榜或飙升。')
    body = {'sub_type': RANK_SUBTYPE[rank], 'date_window': DATE_WINDOW_HOURS,
            'page': 1, 'page_size': PAGE_SIZE, 'tags': []}
    if tag_id:
        body['tags'] = [int(tag_id) if str(tag_id).isdecimal() else tag_id]
    return body


_FETCH_JS = """
(function () {
  if (location.protocol !== 'https:' || location.hostname !== 'douhot.douyin.com') {
    return {state: 'wrong_origin'};
  }
  if (window.__indexHubTopic && window.__indexHubTopic.state === 'pending') {
    return {state: 'busy'};
  }
  var spec = __SPEC__;
  window.__indexHubTopic = {state: 'pending'};
  var headers = {accept: 'application/json'};
  var opts = {method: spec.method, credentials: 'include', headers: headers};
  if (spec.method === 'POST') {
    headers['content-type'] = 'application/json';
    opts.body = spec.body;
  }
  fetch(spec.url, opts).then(function (res) {
    return res.text().then(function (text) {
      window.__indexHubTopic = {state: 'done', http_status: res.status,
        url: String(res.url).slice(0, 240), body: String(text).slice(0, 400000)};
    });
  }).catch(function () {
    window.__indexHubTopic = {state: 'error'};
  });
  return {state: 'started'};
})()
"""


def _script(spec):
    if 'cookie' in _FETCH_JS.lower():
        raise RuntimeError('topic fetch script must not read cookies')
    return _FETCH_JS.replace('__SPEC__', json.dumps(spec, ensure_ascii=False))


def _origin_ok(url):
    parts = urlsplit(url or '')
    return parts.scheme == 'https' and parts.hostname == 'douhot.douyin.com' and parts.port in (None, 443)


def ensure_douhot(window):
    current = window.get_current_url() or ''
    if _origin_ok(current):
        return
    window.load_url(DOUHOT_ORIGIN + '/')
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if _origin_ok(window.get_current_url() or ''):
            return
        time.sleep(0.2)
    raise TopicFetchError('browser_unavailable', '热点宝页面未打开。')


def run_page_request(window, spec, *, sleep=time.sleep, clock=time.monotonic):
    ensure_douhot(window)
    started = window.evaluate_js(_script(spec))
    if not isinstance(started, dict) or started.get('state') not in ('started', 'busy'):
        raise TopicFetchError('browser_unavailable', '热点宝页面没有开始请求。')
    deadline = clock() + 20
    state = started
    while clock() < deadline:
        state = window.evaluate_js('window.__indexHubTopic')
        if isinstance(state, dict) and state.get('state') in ('done', 'error'):
            break
        sleep(0.2)
    if not isinstance(state, dict) or state.get('state') != 'done':
        raise TopicFetchError('browser_unavailable', '热点宝页面没有返回结果。')
    kind = classify_http(state.get('http_status'), state.get('url'))
    if kind:
        raise TopicFetchError(kind, '热点宝请求被拒绝。')
    try:
        payload = json.loads(state.get('body') or '')
    except (TypeError, ValueError):
        raise TopicFetchError('response_format', '热点宝响应不是 JSON。') from None
    return payload
