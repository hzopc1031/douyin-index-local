"""Hotspot-topic read contract and one-shot probe.

Collection runs only inside the desktop browser via the hub socket. This
module does not sign requests or read cookies. Missing numbers stay null.
The scheduler starts only when INDEX_HUB_TOPIC_ENABLED is exactly 1.
"""

import argparse
import json
import os
import sys

from hub_store import HubError

TOPIC_NOTE = (
    '热点宝话题不是搜索指数，也不是热搜榜热度。'
    '只保存浏览器页面返回的话题，不伪造。缺失为 null，不补 0。'
    '未登录、验证码、限频或拒绝访问会暂停，且不自动重试。'
)
TOPIC_RANKS = {'total': '总榜', 'rising': '飙升'}
TOPIC_WINDOW = '1d'
TOPIC_FRESH_SECONDS = 600


def topic_enabled():
    """Exact 1 starts the gateway scheduler. A manual probe does not need it."""
    return os.environ.get('INDEX_HUB_TOPIC_ENABLED') == '1'


def category_name_ok(name):
    return (isinstance(name, str) and name == name.strip() and 1 <= len(name) <= 40
            and not any(ord(char) < 32 or ord(char) == 127 for char in name))


def category_names(saved):
    names = ['全部']
    if isinstance(saved, list):
        for item in saved:
            name = item.get('name') if isinstance(item, dict) else None
            if category_name_ok(name) and name not in names:
                names.append(name)
    return names


def validate_topic_query(query, categories):
    allowed = {'category', 'rank', 'window'}
    if set(query) - allowed or any(len(query.getall(key)) != 1 for key in query):
        raise HubError('invalid_parameters', '话题榜只接受 category、rank、window。')
    category = query.get('category', '全部')
    rank = query.get('rank', 'total')
    window = query.get('window', TOPIC_WINDOW)
    if category not in categories:
        raise HubError('invalid_parameters', '垂类不在已保存的筛选范围内。')
    if rank not in TOPIC_RANKS:
        raise HubError('invalid_parameters', '榜单只能是总榜或飙升。')
    if window != TOPIC_WINDOW:
        raise HubError('invalid_parameters', '当前只支持近1天。')
    return category, rank, window


def snapshot_is_fresh(snapshot, now, window=TOPIC_FRESH_SECONDS):
    """True when this rank/category/window was stored recently enough to reuse."""
    fetched = snapshot.get('fetched_at') if isinstance(snapshot, dict) else None
    if not isinstance(fetched, str) or not fetched:
        return False
    try:
        from datetime import datetime
        stamp = datetime.fromisoformat(fetched)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        return False
    age = now - stamp.timestamp()
    return 0 <= age < window


def empty_mark_key(rank, window, category):
    return f'{rank}|{window}|{category}'


def apply_empty_mark(store, snapshot):
    """A code-0 empty board has no rows. Remember the fetch so the UI can say so."""
    if snapshot.get('items') or snapshot.get('fetched_at'):
        return snapshot
    marks = store.get('topic_empty') or {}
    if not isinstance(marks, dict):
        return snapshot
    stamp = marks.get(empty_mark_key(snapshot['rank'], snapshot['window'], snapshot['category']))
    if not isinstance(stamp, str) or not stamp:
        return snapshot
    marked = dict(snapshot)
    marked['fetched_at'] = stamp
    return marked


def topic_view(snapshot, *, enabled, paused=None, categories=None):
    """On-demand reads stay available when the scheduler is off.

    `enabled` still reports whether INDEX_HUB_TOPIC_ENABLED is exactly 1.
    An empty saved board is not the same as never collected, and neither is 未启用.
    """
    if paused:
        status, label = 'paused', '已暂停'
    elif snapshot['items']:
        status, label = 'available', '已保存'
    elif snapshot.get('fetched_at'):
        status, label = 'available', '该垂类暂无话题'
    elif not enabled:
        status, label = 'available', '尚未采集'
    else:
        status, label = 'not_logged_in', '未登录'
    return {'status': status, 'status_label': label, 'window': snapshot['window'],
            'window_label': '近1天', 'rank': snapshot['rank'], 'rank_label': TOPIC_RANKS[snapshot['rank']],
            'category': snapshot['category'], 'categories': list(categories or ['全部']),
            'items': snapshot['items'], 'fetched_at': snapshot['fetched_at'],
            'paused': paused, 'enabled': enabled, 'live_request': False, 'note': TOPIC_NOTE}


def tag_for(store, category):
    """Return one stored parent or leaf. Lookup failure is a client error."""
    if category == '全部':
        return None
    saved = store.get('topic_categories') or []
    matches = [item for item in saved if isinstance(item, dict) and item.get('name') == category and item.get('id')]
    if len(matches) != 1:
        raise HubError('invalid_parameters', '垂类不在已保存的分类中。')
    return matches[0]


def tag_id_for(store, category):
    found = tag_for(store, category)
    return '' if found is None else str(found['id'])


def record_topic_result(store, job, result):
    """Persist one browser result. Does not retry."""
    import time
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from topic_fetch import BOARD_SOURCE, PAUSE_CODES
    if result.get('requested'):
        now = time.time()
        day = datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai')).date().isoformat()
        counter = store.get('topic_daily') or {'date': day, 'count': 0}
        if counter.get('date') != day:
            counter = {'date': day, 'count': 0}
        store.set('topic_request_at', now)
        store.set('topic_daily', {'date': day, 'count': int(counter.get('count') or 0) + 1})
    if not result.get('ok'):
        if result.get('code') in PAUSE_CODES:
            store.set('topic_paused', {'code': result.get('code'), 'since': time.time()})
        return result
    if job.get('action') == 'tags':
        store.set('topic_categories', result.get('tags') or [])
        store.set('topic_tags_at', time.time())
    elif job.get('action') == 'board' and isinstance(result.get('items'), list):
        key = empty_mark_key(job['rank'], '1d', job['tag_name'])
        marks = store.get('topic_empty') or {}
        if not isinstance(marks, dict):
            marks = {}
        if result['items']:
            store.save_topic_snapshot({
                'complete': True, 'rank': job['rank'], 'window': '1d', 'category': job['tag_name'],
                'source': BOARD_SOURCE,
                'fetched_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                'items': result['items'],
            })
            if key in marks:
                marks.pop(key, None)
                store.set('topic_empty', marks)
        else:
            marks[key] = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
            store.set('topic_empty', marks)
    return result


def probe_main(argv):
    """One browser request. Prints status, count, and field names only."""
    parser = argparse.ArgumentParser(description='One hotspot topic request through the desktop browser.')
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--tags', action='store_true')
    parser.add_argument('--rank', choices=('total', 'rising'), default='total')
    parser.add_argument('--category', default='全部')
    args = parser.parse_args(argv)
    if not args.probe:
        parser.error('请使用 --probe')
    from pathlib import Path
    from hub_bridge import call_bridge
    from hub_paths import state_dir
    from hub_store import Store
    from topic_fetch import request_gate
    import time
    from datetime import datetime
    from zoneinfo import ZoneInfo
    root = Path(__file__).resolve().parent
    store = Store(state_dir(root) / 'index-hub.sqlite3')
    job = {'action': 'tags'} if args.tags else {'action': 'board', 'rank': args.rank, 'tag_name': args.category}
    try:
        if store.get('topic_paused'):
            result = {'ok': False, 'code': 'paused'}
        else:
            now = time.time()
            day = datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai')).date().isoformat()
            counter = store.get('topic_daily') or {'date': day, 'count': 0}
            if counter.get('date') != day:
                counter = {'date': day, 'count': 0}
            gate = request_gate(now, store.get('topic_request_at'), int(counter.get('count') or 0))
            if gate != 'ok':
                result = {'ok': False, 'code': gate}
            else:
                if not args.tags:
                    found = tag_for(store, args.category)
                    job['tag_id'] = '' if found is None else str(found['id'])
                    job['parent_id'] = '' if found is None else str(found.get('parent_id') or '')
                    job['child_ids'] = [] if found is None else [str(item) for item in (found.get('child_ids') or [])]
                try:
                    result = call_bridge('topic', job)
                except OSError:
                    result = {'ok': False, 'code': 'browser_unavailable', 'requested': False}
                result = record_topic_result(store, job, result)
    except HubError as exc:
        result = {'ok': False, 'code': exc.code}
    finally:
        store.close()
    status = 'ok' if result.get('ok') else (result.get('code') or 'browser_unavailable')
    print(json.dumps({'status': status, 'item_count': result.get('item_count') or 0,
                      'fields': result.get('fields') or [], 'rank': job.get('rank'),
                      'category': job.get('tag_name'), 'summary': result.get('summary')},
                     ensure_ascii=False))
    return 0 if status == 'ok' else 2


if __name__ == '__main__':
    sys.exit(probe_main(sys.argv[1:]))
