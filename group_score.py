"""Workbench scores for a user keyword group. Reads stored rows only.

Search index and hot-list heat stay separate. This module never writes
points.composite, never submits a collection job, and never calls the network.
"""

import datetime as dt
import json
import math
import os

from hub_store import TZ, HubError

GROUP_NOTE = '工作台热度分，不是搜索次数，也不是抖音综合指数。缺失为 null，不补 0。'
DEFAULT_WEIGHTS = {'search': 0.7, 'hot': 0.3}


def _finite(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not low <= number <= high:
        return None
    return number


def _env_weight(name, default):
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    parsed = _finite(raw, 0, 1)
    return default if parsed is None else parsed


def group_weights(store):
    weights = {
        'search': _env_weight('INDEX_HUB_GROUP_SEARCH_WEIGHT', DEFAULT_WEIGHTS['search']),
        'hot': _env_weight('INDEX_HUB_GROUP_HOT_WEIGHT', DEFAULT_WEIGHTS['hot']),
    }
    saved = store.get('group_score_weights')
    if isinstance(saved, dict):
        for key in ('search', 'hot'):
            if key in saved:
                parsed = _finite(saved[key], 0, 1)
                if parsed is not None:
                    weights[key] = parsed
    return weights


def validate_group(body):
    if type(body) is not dict or set(body) - {'name', 'keywords'}:
        raise HubError('invalid_group', '仅接受 name 和 keywords。')
    name, words = body.get('name'), body.get('keywords')
    if (not isinstance(name, str) or name != name.strip() or not 1 <= len(name) <= 40
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise HubError('invalid_group', '名称须为 1—40 字，首尾不能有空白。')
    if (type(words) is not list or not 1 <= len(words) <= 20 or len(set(words)) != len(words)
            or any(not isinstance(word, str) or word != word.strip() or not 1 <= len(word) <= 100
                   or any(ord(char) < 32 or ord(char) == 127 for char in word) for word in words)):
        raise HubError('invalid_group', '请提供 1—20 个不重复的关键词，每词 1—100 字。')
    return name, list(words)


def _heat(raw):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _round(value):
    return round(value, 6) if isinstance(value, float) else value


def _search_values(store, keywords):
    marks = ','.join('?' * len(keywords))
    rows = store.db.execute(
        "SELECT keyword, day, value FROM points WHERE platform='douyin' AND metric='search_index' "
        f"AND region='全国' AND keyword IN ({marks})", keywords).fetchall()
    found = {word: {} for word in keywords}
    for row in rows:
        value = _heat(row['value'])
        if value is not None and row['keyword'] in found:
            found[row['keyword']][row['day']] = value
    return found


def _search_score(store, keywords):
    found = _search_values(store, keywords)
    complete = None
    for word in keywords:
        days = set(found[word])
        complete = days if complete is None else complete & days
    if complete:
        current_day, baseline_day = max(complete), min(complete)
        raw = sum(found[word][current_day] for word in keywords)
        baseline_raw = sum(found[word][baseline_day] for word in keywords)
        if baseline_raw == 0:
            score, reason = None, 'baseline_zero'
        else:
            score, reason = _round(100 * raw / baseline_raw), None
        return {'score': score, 'raw': raw, 'day': current_day, 'baseline_day': baseline_day,
                'baseline_raw': baseline_raw, 'reason': reason, 'missing': []}
    days = [day for word in keywords for day in found[word]]
    if not days:
        missing = [{'day': None, 'keyword': word} for word in keywords]
        day = None
    else:
        day = max(days)
        missing = [{'day': day, 'keyword': word} for word in keywords if day not in found[word]]
    return {'score': None, 'raw': None, 'day': day, 'baseline_day': None, 'baseline_raw': None,
            'reason': 'incomplete_day', 'missing': missing}


def _latest_board(store):
    # Fusion uses Douyin heat only.
    row = store.db.execute(
        "SELECT id, fetched FROM hot_snapshots WHERE board='word_list' "
        "ORDER BY fetched DESC, id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    items = store.db.execute(
        "SELECT word, position, hot_value FROM hot_snapshots WHERE id=? ORDER BY position",
        (row['id'],)).fetchall()
    return {'fetched': row['fetched'], 'items': [
        {'word': item['word'], 'position': item['position'], 'hot_value': _heat(item['hot_value'])}
        for item in items]}


def _hot_score(store, keywords):
    board = _latest_board(store)
    if board is None:
        return {'score': None, 'snapshot_status': 'missing', 'snapshot_fetched_at': None,
                'board_max': None, 'reason': 'snapshot_missing', 'members': [
                    {'keyword': word, 'status': None, 'hot_value': None, 'ratio': None, 'position': None}
                    for word in keywords], 'substring_hits': []}
    fetched_at = dt.datetime.fromtimestamp(board['fetched'], TZ).isoformat()
    heats = [item['hot_value'] for item in board['items'] if item['hot_value'] is not None]
    board_max = max(heats) if heats else None
    exact = {}
    for item in board['items']:
        exact.setdefault(item['word'], item)
    members = []
    substring_hits = []
    ratios = []
    usable = board_max is not None and board_max > 0
    for word in keywords:
        hit = exact.get(word)
        for item in board['items']:
            if item['word'] != word and word and item['word'] and (word in item['word'] or item['word'] in word):
                substring_hits.append({'keyword': word, 'word': item['word'], 'position': item['position'],
                                       'hot_value': item['hot_value']})
        if hit is None:
            members.append({'keyword': word, 'status': '未上榜', 'hot_value': None, 'ratio': None, 'position': None})
            continue
        heat = hit['hot_value']
        if heat is None or not usable or heat <= 0:
            members.append({'keyword': word, 'status': '热度缺失', 'hot_value': heat,
                            'ratio': None, 'position': hit['position']})
            continue
        ratio = heat / board_max
        ratios.append(ratio)
        members.append({'keyword': word, 'status': 'exact', 'hot_value': heat,
                        'ratio': _round(ratio), 'position': hit['position']})
    if len(ratios) == len(keywords):
        score, reason = _round(100 * sum(ratios) / len(ratios)), None
    else:
        score, reason = None, 'board_max_not_positive' if not usable else 'not_all_exact'
    return {'score': score, 'snapshot_status': 'available', 'snapshot_fetched_at': fetched_at,
            'board_max': board_max, 'reason': reason, 'members': members, 'substring_hits': substring_hits}


def group_score(store, group):
    """Return the three scores. Does not persist them or touch collection quota."""
    keywords = group['keywords']
    search = _search_score(store, keywords)
    hot = _hot_score(store, keywords)
    weights = group_weights(store)
    fusion = None
    if search['score'] is not None and hot['score'] is not None:
        fusion = _round(weights['search'] * search['score'] + weights['hot'] * hot['score'])
    return {'group': group, 'weights': weights, 'search': search, 'hot': hot,
            'fusion': {'score': fusion}, 'live_request': False, 'note': GROUP_NOTE}
