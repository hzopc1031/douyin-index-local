"""One unauthenticated request to a Baidu public hot-search board."""
import datetime as dt

import requests
from requests.adapters import HTTPAdapter
from hotlist_client import (HotListError, MAX_ITEMS, MAX_POSITION, MAX_RESPONSE_BYTES,
                            REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT, SHANGHAI,
                            _HEADERS, _REDIRECTS, _error, _json_loads, _optional_heat, _word)

DEFAULT_TABS = ('realtime', 'novel', 'movie', 'teleplay', 'livelihood', 'finance', 'sports',
                'new_entertainment', 'internation_news', 'challenge', 'car', 'game')
KNOWN_TABS = DEFAULT_TABS + ('drama',)
SOURCE = 'https://top.baidu.com/api/board?platform=pc&tab='


def project_baidu_hot_list(payload, tab, *, fetched_at=None):
    if tab not in KNOWN_TABS or not isinstance(payload, dict) or payload.get('success') is not True:
        raise _error('incomplete', '百度热榜状态或类型无效。')
    data = payload.get('data')
    cards = data.get('cards') if isinstance(data, dict) else None
    if not isinstance(cards, list) or len(cards) != 1 or not isinstance(cards[0], dict):
        raise _error('incomplete', '百度热榜卡片无法核验。')
    card = cards[0]
    rows, pinned = card.get('content'), card.get('topContent', [])
    if pinned is None:
        pinned = []
    if not isinstance(card.get('component'), str) or not isinstance(rows, list) or not rows or not isinstance(pinned, list):
        raise _error('incomplete', '百度热榜词列表无法核验。')
    items, words, positions = [], set(), set()
    # Pinned-only words are intentionally omitted and must not invalidate the board.
    for row in rows:
        if not isinstance(row, dict):
            raise _error('incomplete', '百度热榜词条无效。')
        word = _word(row.get('query') if row.get('word') is None else row['word'])
        index = row.get('index')
        if type(index) is not int or not 0 <= index < MAX_POSITION or word in words or index + 1 in positions:
            raise _error('incomplete', '百度热榜词或名次重复或无效。')
        heat = row.get('hotScore')
        if heat == '':
            heat = None
        if isinstance(heat, str):
            if not heat.isascii() or not heat.isdecimal():
                raise _error('incomplete', '百度热榜热度无效。')
            try:
                heat = int(heat)
            except ValueError:
                raise _error('incomplete', '百度热榜热度无效。') from None
        words.add(word)
        positions.add(index + 1)
        items.append({'item_key': 'word:' + word, 'key_kind': 'word', 'sentence_id': None,
                      'word': word, 'position': index + 1, 'hot_value': _optional_heat(heat)})
    if len(items) > MAX_ITEMS:
        raise _error('incomplete', '百度热榜词条过多。')
    return {'complete': True, 'board': 'baidu_' + tab, 'source': SOURCE + tab,
            'fetched_at': fetched_at or dt.datetime.now(SHANGHAI).isoformat(timespec='seconds'), 'items': items}


class BaiduHotListClient:
    def __init__(self, session=None):
        self.session = session if session is not None else requests.Session()
        self.owns_session = session is None
        self.session.trust_env = False
        self.session.mount('https://', HTTPAdapter(max_retries=0))

    def close(self):
        if self.owns_session:
            self.session.close()

    def fetch(self, tab):
        if tab not in KNOWN_TABS:
            raise _error('incomplete', '百度热榜类型无效。')
        response = None
        self.session.cookies.clear()
        try:
            response = self.session.get(SOURCE + tab, headers={**_HEADERS, 'Referer': 'https://top.baidu.com/board?tab=' + tab},
                                        timeout=(REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT),
                                        allow_redirects=False, stream=True, verify=True)
            status = response.status_code
            if status in _REDIRECTS:
                raise _error('redirect_rejected', '热榜重定向已拒绝。', http_status=status)
            if status in (403, 429):
                raise _error('access_denied' if status == 403 else 'rate_limited', '热榜请求已停止。', http_status=status)
            if status != 200:
                raise _error('http_error', '热榜请求失败。', http_status=status)
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise _error('response_too_large', '热榜响应过大。', http_status=status)
                chunks.append(chunk)
            try:
                payload = _json_loads(b''.join(chunks).decode('utf-8-sig'))
            except (ValueError, UnicodeError):
                raise _error('response_format', '热榜响应不是 JSON。', http_status=status) from None
            return project_baidu_hot_list(payload, tab)
        except requests.RequestException:
            raise _error('network_error', '热榜网络请求失败。') from None
        finally:
            self.session.cookies.clear()
            if response is not None:
                response.close()
