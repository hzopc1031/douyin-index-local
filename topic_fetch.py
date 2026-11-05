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
    def __init__(self, code, message, summary=None):
        super().__init__(message)
        self.code = code
        self.summary = summary


_CODE_KEYS = frozenset({'code', 'status_code'})
_TEXT_KEYS = frozenset({'code', 'status_code', 'message', 'status_msg', 'msg', 'status'})
_SECRET_KEYS = frozenset({'cookie', 'cookies', 'set-cookie', 'token', 'authorization', 'a_bogus', 'bogus'})
_LIST_KEYS = ('billboard_list', 'challenge_list', 'tag_list', 'content_tags', 'tags', 'list', 'objs', 'billboard', 'data')
_CHILD_KEYS = ('children', 'child_list', 'sub_tags', 'tag_list', 'sub_list')
_PAGE_PATHS = ('/', '/ranking', '/topic', '/analysis')


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


def _status_code(payload):
    if not isinstance(payload, dict):
        raise TopicFetchError('response_format', '话题响应不是对象。')
    code = payload.get('code', payload.get('status_code'))
    if code == 8:
        raise TopicFetchError('not_logged_in', '热点宝未登录。')
    if isinstance(code, int) and code not in (0, None):
        message = payload.get('message') if isinstance(payload.get('message'), str) else ''
        message = message or (payload.get('msg') if isinstance(payload.get('msg'), str) else '')
        if any(word in message for word in ('验证', '验证码', 'captcha')):
            raise TopicFetchError('verification_required', '热点宝要求验证。')
        if any(word in message for word in ('频繁', '限流', '稍后')):
            raise TopicFetchError('rate_limited', '热点宝限频。')
        raise TopicFetchError('response_format', '话题响应码无法识别。')


def _collect_lists(node, depth=0):
    found = []
    if isinstance(node, list):
        found.append(node)
        return found
    if isinstance(node, dict) and depth < 4:
        for key in _LIST_KEYS:
            if key in node:
                found.extend(_collect_lists(node[key], depth + 1))
    return found


def _candidate_lists(payload):
    _status_code(payload)
    data = payload.get('data')
    if isinstance(data, list):
        return [data]
    if isinstance(data, dict):
        return _collect_lists(data)
    return []


def _tag_from(row):
    raw_id = row.get('id', row.get('tag_id', row.get('label_id', row.get('value'))))
    name = _text(row.get('name', row.get('tag_name', row.get('label_name', row.get('label')))), 40)
    if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)) or name is None or name == '全部':
        return None
    identifier = str(raw_id).strip()
    if not identifier or len(identifier) > 32:
        return None
    return {'id': identifier, 'name': name}


def _walk_tags(rows, tags, seen, depth=0):
    if depth > 5 or not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = _tag_from(row)
        if item is not None and item['id'] not in seen:
            seen.add(item['id'])
            tags.append(item)
        for key in _CHILD_KEYS:
            _walk_tags(row.get(key), tags, seen, depth + 1)


def parse_tags(payload):
    lists = _candidate_lists(payload)
    tags, seen = [], set()
    for rows in lists:
        _walk_tags(rows, tags, seen)
    if not tags:
        raise TopicFetchError('schema_mismatch', '没有识别到垂类。')
    return tags


def parse_board(payload):
    lists = _candidate_lists(payload)
    rows = None
    for candidate in lists:
        if candidate and isinstance(candidate[0], dict) and (
                'challenge_name' in candidate[0] or 'challenge_id' in candidate[0]):
            rows = candidate
            break
    if rows is None:
        if any(isinstance(candidate, list) and not candidate for candidate in lists):
            rows = []
        else:
            raise TopicFetchError('schema_mismatch', '话题响应缺少榜单。')
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
      var type = '';
      try { type = String(res.headers.get('content-type') || '').split(';')[0].slice(0, 80); } catch (e) {}
      window.__indexHubTopic = {state: 'done', http_status: res.status,
        url: String(res.url).slice(0, 500), content_type: type, body: String(text).slice(0, 400000)};
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


def _wait_origin(window, path=None):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        current = window.get_current_url() or ''
        parts = urlsplit(current)
        if _origin_ok(current) and (path is None or parts.path.rstrip('/') == path.rstrip('/')):
            return current
        time.sleep(0.2)
    return window.get_current_url() or ''


def _hook_state(window):
    state = window.evaluate_js(
        "(function(){var hooked=false;try{hooked=!/\\[native code\\]/.test("
        "Function.prototype.toString.call(window['fetch']));}catch(e){}"
        "return {hooked:!!hooked,path:location.pathname};})()")
    return state if isinstance(state, dict) else {'hooked': False, 'path': ''}


def ensure_douhot(window):
    """Open a douhot document so its own fetch hook can sign. Home first, then known boards."""
    if not _origin_ok(window.get_current_url() or ''):
        window.load_url(DOUHOT_ORIGIN + '/')
        if not _origin_ok(_wait_origin(window)):
            raise TopicFetchError('browser_unavailable', '热点宝页面未打开。')
    state = _hook_state(window)
    if state.get('hooked'):
        return state
    for path in _PAGE_PATHS:
        if (state.get('path') or '').rstrip('/') == path.rstrip('/'):
            continue
        window.load_url(DOUHOT_ORIGIN + path)
        _wait_origin(window, path)
        state = _hook_state(window)
        if state.get('hooked'):
            return state
    return state


def _query_keys(url):
    parts = urlsplit(url or '')
    keys = []
    for piece in (parts.query or '').split('&'):
        if not piece:
            continue
        key = piece.split('=', 1)[0]
        if key and key not in keys and key.lower() not in {'cookie', 'cookies', 'set-cookie', 'token', 'authorization'}:
            keys.append(key[:40])
    return parts.hostname, parts.path, keys


def _short_text(value):
    if not isinstance(value, str):
        return None
    text = ' '.join(value.split())
    if not text or len(text) > 80:
        return None
    lowered = text.lower()
    if any(word in lowered for word in ('cookie', 'token', 'session', 'a_bogus', 'authorization')):
        return None
    if any(len(chunk) >= 24 and chunk.isalnum() for chunk in text.replace('-', ' ').replace('_', ' ').split()):
        return None
    return text


def skeleton(value, depth=0):
    """Keys and types only. Code numbers and short status messages may be kept."""
    if depth >= 5:
        return 'max_depth'
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'bool'
    if isinstance(value, list):
        return {'type': 'list', 'length': len(value),
                'first': skeleton(value[0], depth + 1) if value else None}
    if isinstance(value, dict):
        shown = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 40 or not isinstance(key, str):
                continue
            if key.lower() in _SECRET_KEYS:
                shown[key] = 'redacted'
            elif key in _CODE_KEYS and isinstance(item, (int, float)) and not isinstance(item, bool):
                shown[key] = item if isinstance(item, int) or (isinstance(item, float) and math.isfinite(item)) else 'number'
            elif key in _TEXT_KEYS and isinstance(item, str):
                shown[key] = _short_text(item) or 'str'
            else:
                shown[key] = skeleton(item, depth + 1)
        return shown
    if isinstance(value, (int, float)):
        return 'number'
    if isinstance(value, str):
        return 'str'
    return 'other'


def business_fields(payload):
    if not isinstance(payload, dict):
        return {}
    found = {}
    for key in ('code', 'status_code', 'message', 'status_msg', 'msg', 'status'):
        if key not in payload:
            continue
        value = payload[key]
        if key in _CODE_KEYS and isinstance(value, int) and not isinstance(value, bool):
            found[key] = value
        elif isinstance(value, str):
            found[key] = _short_text(value) or 'str'
        else:
            found[key] = skeleton(value, 1)
    return found


def summarize_response(state, spec, page_state, payload=None, *, json_ok=False):
    host, path, actual_keys = _query_keys(state.get('url') if isinstance(state, dict) else '')
    req_host, req_path, req_keys = _query_keys(spec.get('url') if isinstance(spec, dict) else '')
    body_keys = []
    if isinstance(spec, dict) and spec.get('method') == 'POST':
        try:
            parsed = json.loads(spec.get('body') or '{}')
        except (TypeError, ValueError):
            parsed = {}
        if isinstance(parsed, dict):
            body_keys = [key for key in parsed if isinstance(key, str) and key.lower() not in _SECRET_KEYS][:20]
    content_type = state.get('content_type') if isinstance(state, dict) else None
    if not isinstance(content_type, str) or '/' not in content_type or len(content_type) > 80:
        content_type = None
    else:
        content_type = content_type.split(';', 1)[0].strip().lower()
    body = state.get('body') if isinstance(state, dict) else ''
    summary = {
        'http_status': state.get('http_status') if isinstance(state, dict) and isinstance(state.get('http_status'), int) else None,
        'final_host': host, 'final_path': path, 'content_type': content_type,
        'json': json_ok, 'body_length': len(body) if isinstance(body, str) else 0,
        'business': business_fields(payload) if json_ok else {},
        'skeleton': skeleton(payload) if json_ok else None,
        'request': {
            'through_page_fetch': True,
            'fetch_hooked': bool(page_state.get('hooked')) if isinstance(page_state, dict) else False,
            'page_host': 'douhot.douyin.com',
            'page_path': page_state.get('path') if isinstance(page_state, dict) and isinstance(page_state.get('path'), str) else '',
            'method': spec.get('method') if isinstance(spec, dict) else None,
            'host': req_host, 'path': req_path, 'query_keys': req_keys, 'body_keys': body_keys,
            'actual_query_keys': actual_keys,
            'extra_query_keys': [key for key in actual_keys if key not in req_keys],
        },
    }
    return summary


def write_topic_log(summary):
    import os
    from pathlib import Path
    directory = Path(os.environ.get('INDEX_HUB_LOG_DIR', Path(__file__).resolve().parent / 'logs'))
    try:
        directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        path = directory / 'topic.log'
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'event': 'topic_response', 'summary': summary}, ensure_ascii=False) + '\n')
        os.chmod(path, 0o640)
    except OSError:
        return


def run_page_request(window, spec, *, sleep=time.sleep, clock=time.monotonic):
    page_state = ensure_douhot(window)
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
        summary = summarize_response(state, spec, page_state)
        raise TopicFetchError(kind, '热点宝请求被拒绝。', summary)
    try:
        payload = json.loads(state.get('body') or '')
    except (TypeError, ValueError):
        summary = summarize_response(state, spec, page_state, json_ok=False)
        raise TopicFetchError('response_format', '热点宝响应不是 JSON。', summary) from None
    summary = summarize_response(state, spec, page_state, payload, json_ok=True)
    return payload, summary
