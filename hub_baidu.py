"""Baidu-only worker policy; no Douyin settings or credentials are shared."""
import asyncio
import datetime as dt
import math
import time
import logging
import os
import random
import tomllib
from pathlib import Path

import baidu_adapter as adapter
from hub_store import HubError, TZ

DEFAULTS = dict(interval_seconds=20, jitter_seconds=10, chunk_interval_seconds=8,
                logical_daily=120, http_daily=400, rate_limit_cooldown_seconds=3600)
BOUNDS = dict(interval_seconds=(5, 3600), jitter_seconds=(0, 3600), chunk_interval_seconds=(5, 3600),
              logical_daily=(1, 1000), http_daily=(1, 1000), rate_limit_cooldown_seconds=(3600, 86400))
_warned = False


def limits():
    global _warned
    invalid = False
    try:
        path = Path(os.environ.get('INDEX_HUB_PLATFORM_LIMITS', Path(__file__).with_name('platform_limits.toml')))
        with path.open('rb') as f:
            values = tomllib.load(f)['baidu']
        if type(values) is not dict:
            raise ValueError
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError):
        values, invalid = {}, True
    result = {}
    for key, default in DEFAULTS.items():
        value = values.get(key, default)
        lo, hi = BOUNDS[key]
        if type(value) is not int or not lo <= value <= hi:
            value, invalid = default, True
        result[key] = value
    if invalid and not _warned:
        logging.getLogger('index_hub.baidu').warning('Invalid platform limits; defaults used for invalid values')
        _warned = True
    return result


def daily(store):
    today = dt.datetime.now(TZ).date().isoformat()
    value = store.get('baidu.daily', {})
    return value if value.get('date') == today else dict(date=today, attempts=0, http_reserved=0)


async def browser_status(hub):
    try:
        result = await asyncio.to_thread(hub.bridge, 'baidu_status', None, 2)
        if result.get('ok') is True and result.get('live_enabled') is True and result.get('state') in ('browser_ready', 'browser_idle', 'login_required', 'login_in_progress', 'signature_unavailable'):
            return result['state']
    except Exception:
        pass
    return 'login_required'


def status_snapshot(store):
    config = limits()
    return {'browser': 'login_required', 'login_state': '待登录', 'live_enabled': True,
            'paused': store.get('baidu.paused'), 'daily': daily(store),
            'cooldown_remaining_seconds': max(0, math.ceil(store.get('baidu.next_attempt_at', 0)-time.time())),
            'limits': config, 'note': '额度为工程保护，不是官方配额。'}


async def status(hub):
    return status_snapshot(hub.store)


async def resume(hub):
    paused = hub.store.get('baidu.paused') or {}
    if paused.get('code') == 'rate_limited' and time.time() < paused.get('since', time.time()) + limits()['rate_limit_cooldown_seconds']:
        raise HubError('rate_limited', '百度冷却尚未结束。', 409)
    if await browser_status(hub) not in ('browser_ready', 'browser_idle', 'signature_unavailable'):
        raise HubError('login_required', '百度浏览器未就绪。', 409)
    hub.store.set('baidu.paused', None)


def fail(store, job_id, code):
    message = adapter.ERRORS.get(code, '百度任务未完成，已暂停；不会自动重试。')
    store.update_job(job_id, 'failed', error={'code': code, 'message': message})
    store.set('baidu.paused', {'code': code, 'since': time.time()})


def related_date(store, query):
    row = store.db.execute("""SELECT max(day) FROM baidu_points WHERE keyword=? AND region=?
        AND value_status='available' AND day<=?""",
        (query['keywords'][0], '全国', query['end_date'])).fetchone()
    return row[0] or min(query['end_date'], (dt.datetime.now(TZ).date() - dt.timedelta(days=1)).isoformat())


async def run_one(hub, row, query):
    async with hub._platform_locks.setdefault('baidu', asyncio.Lock()):
        store = hub.store
        if store.job(row['id'])['status'] != 'queued' or store.get('baidu.paused'):
            return False
        cached = store.cached(query)
        if cached:
            store.update_job(row['id'], 'no_data' if cached.get('no_data') else 'succeeded', result=cached)
            with store.db:
                store.db.execute('UPDATE jobs SET cache_hit=1 WHERE id=?', (row['id'],))
            return True
        config = limits()
        if time.time() < store.get('baidu.next_attempt_at', 0):
            return False
        counter = daily(store)
        http_reserved = 1 if query.get('kind') == 'related' else 2 * len(list(adapter.chunks(query)))
        if counter['attempts'] >= config['logical_daily'] or counter['http_reserved'] + http_reserved > config['http_daily']:
            fail(store, row['id'], 'baidu_daily_limit')
            return True
        state = await browser_status(hub)
        if state not in (('browser_ready', 'browser_idle', 'signature_unavailable') if query.get('kind') == 'related' else ('browser_ready', 'browser_idle')):
            fail(store, row['id'], state)
            return True
        # Reserve traffic before dispatch; timeout/restart never refunds uncertain traffic.
        counter = daily(store)
        with store.db:
            from hub_store import encode
            for key, value in [('baidu.last_attempt_at', time.time()),
                               ('baidu.next_attempt_at', time.time() + config['interval_seconds'] + random.uniform(0, config['jitter_seconds'])),
                               ('baidu.daily', {**counter, 'attempts': counter['attempts']+1,
                                                'http_reserved': counter['http_reserved']+http_reserved})]:
                store.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, encode(value)))
            store.db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), row['id']))
        try:
            selected_date = related_date(store, query) if query.get('kind') == 'related' else None
            payload = {**query, '_budget_day': counter['date'], '_job_id': row['id']}
            if selected_date:
                payload['related_date'] = selected_date
            response = await asyncio.to_thread(hub.bridge, 'baidu_query', payload, 30 + 80 * len(list(adapter.chunks(query))) + config['chunk_interval_seconds'] * (len(list(adapter.chunks(query))) - 1))
            if response.get('ok') is not True:
                code = response.get('code')
                fail(store, row['id'], code if code in adapter.ERRORS else 'request_failed')
                return True
            if query.get('kind') == 'related':
                result = {'data': response['data'], 'metadata': response['metadata']}
                if any(result['metadata'].get(k) != query[k] for k in ('platform', 'kind', 'keywords', 'start_date', 'end_date')):
                    raise HubError('invalid_result', '百度结果与任务不一致。')
                if result['metadata'].get('related_date') != selected_date:
                    raise HubError('invalid_result', '百度结果与任务不一致。')
                if not result['data']['search_related']:
                    result['no_data'] = True
                store.save_insight(query, result)
                store.update_job(row['id'], 'no_data' if result.get('no_data') else 'succeeded', result=result)
                return True
            result = adapter.public_result(response)
            if any(result['metadata'][k] != query[k] for k in ('platform', 'keywords', 'start_date', 'end_date')):
                raise HubError('invalid_result', '百度结果与任务不一致。')
            store.ingest(result)
            has_values = any(r['search_index'] is not None for r in result['rows'])
            store.update_job(row['id'], 'succeeded' if result['metadata']['complete'] else 'partial' if has_values else 'no_data', result=result)
        except asyncio.CancelledError:
            fail(store, row['id'], 'interrupted')
            raise
        except Exception:
            # Do not log browser exceptions, response bodies, or arbitrary messages.
            fail(store, row['id'], 'request_failed')
        return True


def mount_routes(app, hub, require, reply):
    async def login_open(request):
        require(request, admin=True)
        try:
            result = await asyncio.to_thread(hub.bridge, 'baidu_login_open', None, 35)
        except Exception:
            raise HubError('browser_unavailable', '百度浏览器未就绪，请检查远程桌面。', 503) from None
        if not result.get('ok'):
            raise HubError('browser_unavailable', '百度浏览器未就绪，请检查远程桌面。', 503)
        return reply(result)
    app.router.add_post('/api/v1/login/baidu/open', login_open)
