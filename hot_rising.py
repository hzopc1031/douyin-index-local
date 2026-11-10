"""Read-only rising-word scores from stored hot-list snapshots.

The official trending_list is not used. Nothing here calls the network or
spends the search-index quota. Missing inputs stay null.
"""

import datetime as dt
import json
import math
import os
import statistics

from hub_store import TZ

RISING_NOTE = (
    '这是本库热榜快照的计算结果，不是搜索指数，也不是官方飙升榜。'
    '无基线、样本不足或搜索指数缺一天时为 null，不补 0。未上榜不记为第 50 名。'
)
DEFAULT_THRESHOLDS = {
    'min_age_seconds': 1800,
    'max_age_seconds': 21600,
    'lookback_seconds': 86400,
    'rank_rise': 10,
    'rank_max': 15,
    'heat_pct': 0.5,
    'heat_delta': 500000,
    'z_min_samples': 8,
    'z_min': 2.0,
}
_ENV = {
    'min_age_seconds': 'INDEX_HUB_RISING_MIN_AGE_SECONDS',
    'max_age_seconds': 'INDEX_HUB_RISING_MAX_AGE_SECONDS',
    'lookback_seconds': 'INDEX_HUB_RISING_LOOKBACK_SECONDS',
    'rank_rise': 'INDEX_HUB_RISING_RANK_RISE',
    'rank_max': 'INDEX_HUB_RISING_RANK_MAX',
    'heat_pct': 'INDEX_HUB_RISING_HEAT_PCT',
    'heat_delta': 'INDEX_HUB_RISING_HEAT_DELTA',
    'z_min_samples': 'INDEX_HUB_RISING_Z_MIN_SAMPLES',
    'z_min': 'INDEX_HUB_RISING_Z_MIN',
}
_INT_LIMITS = {
    'min_age_seconds': (0, 86400 * 14),
    'max_age_seconds': (1, 86400 * 14),
    'lookback_seconds': (1, 86400 * 30),
    'rank_rise': (0, 500),
    'rank_max': (1, 500),
    'heat_delta': (0, 10 ** 12),
    'z_min_samples': (2, 500),
}
_FLOAT_LIMITS = {
    'heat_pct': (0, 1000),
    'z_min': (0, 100),
}


def _as_int(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text.isascii() or not text.lstrip('-').isdecimal():
            return None
        try:
            value = int(text)
        except ValueError:
            return None
    if type(value) is not int or not low <= value <= high:
        return None
    return value


def _as_float(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or not low <= value <= high:
        return None
    return value


def _env_value(key):
    default = DEFAULT_THRESHOLDS[key]
    raw = os.environ.get(_ENV[key])
    if raw is None or not str(raw).strip():
        return default
    if key in _INT_LIMITS:
        parsed = _as_int(raw, *_INT_LIMITS[key])
    else:
        parsed = _as_float(raw, *_FLOAT_LIMITS[key])
    return default if parsed is None else parsed


def _saved_value(key, value):
    if key in _INT_LIMITS:
        return _as_int(value, *_INT_LIMITS[key])
    return _as_float(value, *_FLOAT_LIMITS[key])


def effective_thresholds(store):
    """Environment defaults, then a valid settings override per field."""
    thresholds = {key: _env_value(key) for key in DEFAULT_THRESHOLDS}
    saved = store.get('hot_rising_thresholds') if store is not None else None
    if isinstance(saved, dict):
        for key in DEFAULT_THRESHOLDS:
            if key in saved:
                parsed = _saved_value(key, saved[key])
                if parsed is not None:
                    thresholds[key] = parsed
    if thresholds['min_age_seconds'] > thresholds['max_age_seconds']:
        thresholds['min_age_seconds'] = DEFAULT_THRESHOLDS['min_age_seconds']
        thresholds['max_age_seconds'] = DEFAULT_THRESHOLDS['max_age_seconds']
    return thresholds


def _heat(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _round(value):
    if isinstance(value, float):
        return round(value, 6)
    return value


def _groups(rows):
    grouped = {}
    for row in rows:
        bucket = grouped.get(row['id'])
        if bucket is None:
            bucket = {'id': row['id'], 'fetched': row['fetched'], 'items': {}}
            grouped[row['id']] = bucket
        bucket['items'][row['item_key']] = row
    return list(grouped.values())


def _search_ratio(store, word):
    rows = store.db.execute(
        "SELECT day, value FROM points WHERE platform='douyin' AND metric='search_index' "
        "AND region='全国' AND keyword=? ORDER BY day", (word,)).fetchall()
    present = {}
    for row in rows:
        value = _heat(row['value'])
        if value is not None:
            present[row['day']] = value
    if not present:
        return {'search_index_latest_day': None, 'search_index_previous_day': None,
                'search_index_latest': None, 'search_index_previous': None, 'search_index_ratio': None}
    latest_day = max(present)
    previous_day = (dt.date.fromisoformat(latest_day) - dt.timedelta(days=1)).isoformat()
    latest = present[latest_day]
    previous = present.get(previous_day)
    ratio = None
    if previous is not None and previous > 0 and latest is not None:
        ratio = _round((latest - previous) / previous)
    return {'search_index_latest_day': latest_day, 'search_index_previous_day': previous_day,
            'search_index_latest': latest, 'search_index_previous': previous, 'search_index_ratio': ratio}


def rising_report(store, board='word_list'):
    """Score the latest stored board. Does not read the network or write quota."""
    thresholds = effective_thresholds(store)
    rows = store.db.execute(
        "SELECT id, fetched, item_key, key_kind, sentence_id, word, position, hot_value "
        "FROM hot_snapshots WHERE board=? ORDER BY fetched, id", (board,)).fetchall()
    groups = _groups(rows)
    if not groups:
        return {'current_fetched_at': None, 'baseline_fetched_at': None,
                'thresholds': thresholds, 'items': []}
    current = max(groups, key=lambda group: (group['fetched'], group['id']))
    low = current['fetched'] - thresholds['max_age_seconds']
    high = current['fetched'] - thresholds['min_age_seconds']
    eligible = [group for group in groups
                if group['id'] != current['id'] and low <= group['fetched'] <= high]
    baseline = max(eligible, key=lambda group: (group['fetched'], group['id'])) if eligible else None
    lookback = current['fetched'] - thresholds['lookback_seconds']
    seen = set()
    history = {}

    def before_current(group):
        return group['fetched'] < current['fetched'] or (
            group['fetched'] == current['fetched'] and group['id'] < current['id'])

    for group in groups:
        if not before_current(group):
            continue
        if group['fetched'] >= lookback:
            seen.update(group['items'])
        for item in group['items'].values():
            if not item['sentence_id']:
                continue
            heat = _heat(item['hot_value'])
            if heat is not None:
                history.setdefault(item['sentence_id'], []).append(heat)
    items = []
    baseline_items = baseline['items'] if baseline else {}
    for row in sorted(current['items'].values(), key=lambda item: item['position']):
        heat = _heat(row['hot_value'])
        previous = baseline_items.get(row['item_key'])
        previous_heat = _heat(previous['hot_value']) if previous else None
        rank_rise = None
        if previous is not None:
            rank_rise = previous['position'] - row['position']
        heat_pct = heat_delta = None
        raw_pct = None
        if heat is not None and heat > 0 and previous_heat is not None and previous_heat > 0:
            heat_delta = heat - previous_heat
            raw_pct = heat_delta / previous_heat
            heat_pct = _round(raw_pct)
        prior = history.get(row['sentence_id'], []) if row['sentence_id'] else []
        z_score = None
        raw_z = None
        if row['sentence_id'] and heat is not None and len(prior) >= thresholds['z_min_samples']:
            deviation = statistics.stdev(prior)
            if deviation != 0:
                raw_z = (heat - statistics.fmean(prior)) / deviation
                z_score = _round(raw_z)
        items.append({
            'item_key': row['item_key'], 'key_kind': row['key_kind'], 'sentence_id': row['sentence_id'],
            'word': row['word'], 'position': row['position'], 'hot_value': heat,
            'new_entry': row['item_key'] not in seen,
            'baseline_position': previous['position'] if previous else None,
            'rank_rise': rank_rise,
            'rank_rise_hit': None if rank_rise is None else (
                rank_rise >= thresholds['rank_rise'] and row['position'] <= thresholds['rank_max']),
            'baseline_hot_value': previous_heat,
            'heat_delta': heat_delta, 'heat_pct': heat_pct,
            'heat_hit': None if raw_pct is None else (
                raw_pct >= thresholds['heat_pct'] and heat_delta >= thresholds['heat_delta']),
            'z_score': z_score,
            'z_hit': None if raw_z is None else raw_z >= thresholds['z_min'],
            **(_search_ratio(store, row['word']) if board == 'word_list' else {
                'search_index_latest_day': None, 'search_index_previous_day': None,
                'search_index_latest': None, 'search_index_previous': None, 'search_index_ratio': None}),
        })
    return {'current_fetched_at': _iso(current['fetched']),
            'baseline_fetched_at': None if baseline is None else _iso(baseline['fetched']),
            'thresholds': thresholds, 'items': items}


def _iso(timestamp):
    return dt.datetime.fromtimestamp(timestamp, TZ).isoformat()
