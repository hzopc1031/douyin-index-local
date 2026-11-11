"""Authenticated index API and task worker, mounted by desktop_gateway.py."""
import asyncio
import base64
import contextlib
import csv
import datetime as dt
import io
import json
import math
import logging
import os
import re
import socket
import time
from pathlib import Path
from hub_paths import state_dir

from aiohttp import web
import hub_bridge
from group_score import GROUP_NOTE, group_score, validate_group
from topic_board import (apply_empty_mark, category_names, record_topic_result, snapshot_is_fresh,
                         tag_for, topic_enabled, topic_view, validate_topic_query)
from topic_fetch import DAILY_LIMIT, MIN_GAP_SECONDS, plan_round, request_gate, round_interval_seconds
from baidu_hotlist_client import BaiduHotListClient, DEFAULT_TABS, KNOWN_TABS, SOURCE
from hot_rising import RISING_NOTE, rising_report
from hotlist_client import HOT_LIST_URL, HotListClient, HotListError
from qr_login import login_mode
from hub_store import HubError, PLATFORMS, Store, TZ, exc_summary, public_result, validate_query
from platforms import registry, require_platform
from sms_login import (FRAME_BASE64_LIMIT, SmsLoginError, TERMINAL, public_sms_status,
                       validate_sms_payload)

logger = logging.getLogger('index_hub')

PRINCIPAL_KEY = web.RequestKey('principal', dict)

# shortcut: related 暂无可靠最新日来源，确认官方来源后替换趋势减两天的回退。
RELATED_TREND_LAG_DAYS = 2


def related_latest_date(store):
    from hub_insights import related_end_lag_days
    latest = store.latest_available_date()
    if latest is None:
        return None
    try:
        return min(dt.date.fromisoformat(latest) - dt.timedelta(days=RELATED_TREND_LAG_DAYS),
                   dt.datetime.now(TZ).date() - dt.timedelta(days=related_end_lag_days())).isoformat()
    except OverflowError:
        return None

SAFE_ERRORS = {
    'authentication_required': '登录已失效，请通过远程桌面扫码后手动恢复采集。',
    'verification_required': '平台要求人工验证，请通过远程桌面处理后手动恢复。',
    'rate_limited': '平台返回限频信号，采集已暂停，未自动重试。',
    'access_denied': '平台拒绝访问，采集已暂停，请检查官方页面。',
    'bridge_timeout': f'浏览器桥接 {hub_bridge.BRIDGE_TIMEOUT} 秒内未返回；平台请求可能仍在进行，未自动重试。',
    'browser_unavailable': '浏览器未就绪，请检查远程桌面。',
    'network_error': '上游网络请求失败，未自动重试。',
    'response_schema': '平台响应格式变化，未自动重试。',
    'http_error': '平台 HTTP 请求失败，请查看 HTTP 状态码。',
    'business_error': '平台业务请求未成功，请查看业务错误码。',
    'unexpected_keyword': '返回关键词与请求不一致，已阻止存入错误数据；请在官方页面核实准确词条。',
    'duplicate_keyword': '平台重复返回关键词，未保存不可靠结果。',
    'response_format': '平台返回格式异常，请检查官方页面。',
    'cooldown': '采集进程仍处于请求冷却期，任务已回到队列。',
    'busy': '采集进程正忙，任务已回到队列。',
    'skipped_trend_no_data': '该词在相同日期段趋势无数据，已跳过关联词和画像查询，未消耗在线次数。',
    'no_data': '平台未返回该关键词在此日期范围的有效指数；可能未收录或该范围无数据，不代表指数为零。',
    'daily_limit': '已达到本服务今日 50 次在线尝试上限，未发出请求。',
    'request_failed': '本次取数未完成，未自动重试。',
}
PARAMETER_ERRORS = {'invalid_query', 'invalid_dates', 'invalid_keywords', 'invalid_kind'}
SAFE_ERRORS.update({code: '参数错误，请检查请求' for code in PARAMETER_ERRORS})
SAFE_ERRORS['busy_requeue_exhausted'] = '浏览器长时间被占用，已放回队列多次仍未执行，未自动重试'
PAUSING = {'browser_unavailable', 'authentication_required', 'verification_required', 'rate_limited', 'access_denied'}
HOT_INTERVAL_FLOOR = 1800
HOT_INTERVAL_DEFAULT = 3600
HOT_DAILY_SUCCESS_LIMIT = 48
HOT_PAUSE_CODES = frozenset({'rate_limited', 'access_denied', 'response_format'})
HOT_POLL_SECONDS = 5
HOT_NOTE = '热榜热度不是搜索指数，也不是创作者中心综合指数。缺失为 null。未上榜不记为 0 或第 50 名。'
_HOT_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')


def hotlist_enabled():
    return os.environ.get('INDEX_HUB_HOTLIST_ENABLED') == '1'


def hotlist_interval_seconds():
    raw = os.environ.get('INDEX_HUB_HOTLIST_INTERVAL_SECONDS', str(HOT_INTERVAL_DEFAULT))
    try:
        value = int(raw) if isinstance(raw, str) and raw.isascii() and raw.isdecimal() else HOT_INTERVAL_DEFAULT
    except (TypeError, ValueError):
        value = HOT_INTERVAL_DEFAULT
    return max(HOT_INTERVAL_FLOOR, value)
def baidu_hotlist_enabled():
    return os.environ.get('INDEX_HUB_BAIDU_HOTLIST_ENABLED') == '1'


def baidu_hotlist_interval_seconds():
    raw = os.environ.get('INDEX_HUB_BAIDU_HOTLIST_INTERVAL_SECONDS', '3600')
    try:
        value = int(raw) if raw.isascii() and raw.isdecimal() else 3600
    except ValueError:
        value = 3600
    return max(1800, value)


def baidu_hotlist_tabs():
    raw = os.environ.get('INDEX_HUB_BAIDU_HOTLIST_TABS')
    return list(dict.fromkeys(tab.strip() for tab in (raw.split(',') if raw is not None else DEFAULT_TABS)
                              if tab.strip() in KNOWN_TABS))


BAIDU_NOTE = '百度热搜热度不是百度搜索指数，缺失为 null，未上榜不记 0。'


SMS_ERRORS = {
    'invalid_payload': (400, '请求字段无效。'), 'invalid_phone': (400, '请输入有效的手机号。'),
    'invalid_code': (400, '请输入 4 至 8 位数字验证码。'), 'invalid_text': (400, '仅允许 1 至 8 位数字或字母。'),
    'invalid_frame': (400, '截图版本无效。'), 'invalid_pointer': (400, '点击或拖动轨迹无效。'),
    'out_of_bounds': (400, '操作超出截图范围。'),
    'login_busy': (409, '登录正在进行，请先取消或等待结束。'),
    'busy_fetching': (409, '正在采集，请等待结束后登录。'),
    'sms_disabled': (409, '未启用短信登录。'), 'login_not_active': (409, '请先开始短信登录。'),
    'code_consumed': (409, '此验证码已提交或失效，请等待结果或重新发码。'),
    'stale_frame': (409, '画面已更新，请重新操作。'),
    'not_in_verification': (409, '当前不需要截图验证。'),
    'cancelled': (409, '登录操作已取消。'),
    'verification_busy': (409, '自动验证中，请稍候。'),
    'remote_desktop_required': (409, '请改用远程桌面完成验证。'),
    'qr_not_ready': (409, '二维码尚未就绪。'),
    'frame_not_ready': (409, '截图尚未就绪，请稍后刷新。'),
    'resend_too_early': (429, '请等待倒计时结束后重发。'),
    'sms_rate_limited': (429, '已达到本服务操作频率限制，请稍后再试。'),
    'browser_unavailable': (503, '浏览器未就绪，请检查远程桌面。'),
    'pointer_timeout': (503, '回放超时，请检查登录状态。'),
    'frame_too_large': (503, '截图过大，请改用远程桌面。'),
    'login_failed': (503, '登录未能启动，请改用远程桌面。'),
}


def sms_error(code):
    code = code if isinstance(code, str) and code in SMS_ERRORS else 'browser_unavailable'
    status, message = SMS_ERRORS[code]
    return HubError(code, message, status)


class Hub:
    def __init__(self, root, bridge=None, bridge_timeout_pause_after=None):
        if bridge_timeout_pause_after is None:
            bridge_timeout_pause_after = os.environ.get('INDEX_HUB_BRIDGE_TIMEOUT_PAUSE_AFTER', '2')
        try:
            value = str(bridge_timeout_pause_after)
            threshold = int(value) if value.isascii() and value.isdecimal() else -1
        except (ValueError, TypeError):
            threshold = -1
        self.bridge_timeout_pause_after = threshold if threshold >= 0 else 2
        value = os.environ.get('INDEX_HUB_BUSY_REQUEUE_LIMIT', '20')
        try:
            self.busy_requeue_limit = int(value) if value.isascii() and value.isdecimal() else 20
        except ValueError:
            self.busy_requeue_limit = 20
        self.root = Path(root)
        self.store = Store(state_dir(self.root) / 'index-hub.sqlite3')
        if bridge is None:
            from hub_bridge import call_bridge
            bridge = call_bridge
        self._qr_last_start = None
        self._sms_lock = asyncio.Lock()
        self._platform_locks = {}
        self.bridge = bridge
        self.worker_task = None
        self.hotlist_task = None
        self.baidu_hotlist_task = None
        self.baidu_hot_client_factory = BaiduHotListClient
        self.topic_task = None
        self._topic_inflight = set()
        self._topic_lock = asyncio.Lock()
        self.hot_client_factory = HotListClient

    def _sms_pause(self, snapshot):
        paused = (self.store.get('paused') or {}).get('code')
        if snapshot['state'] not in TERMINAL | {'idle'}:
            if paused != 'login_in_progress':
                self.store.set('paused', {'code': 'login_in_progress'})
        elif paused == 'login_in_progress':
            self.store.set('paused', {'code': 'manual_resume_required'})

    async def sms_call(self, action, body):
        try:
            result = await asyncio.to_thread(self.bridge, 'sms_' + action, body,
                                             12 if action == 'pointer' else 2)
            if not result.get('ok'):
                raise sms_error(result.get('code'))
            if action in ('frame', 'qr'):
                encoded = result['png_base64']
                if not isinstance(encoded, str) or len(encoded) > FRAME_BASE64_LIMIT:
                    raise ValueError
                png = base64.b64decode(encoded, validate=True)
                if result['frame_id'] != body['frame']:
                    raise sms_error('stale_frame')
                if not png.startswith(b'\x89PNG\r\n\x1a\n'):
                    raise ValueError
                return png
            return public_sms_status(result)
        except HubError:
            raise
        except Exception:
            # No exception text or request payload may enter logs/responses.
            raise sms_error('browser_unavailable') from None

    async def sync_sms_pause(self):
        async with self._sms_lock:
            if (self.store.get('paused') or {}).get('code') == 'login_in_progress':
                await self._check_sms_pause()

    async def _check_sms_pause(self):
        try:
            self._sms_pause(await self.sms_call('status', {}))
        except HubError as exc:
            # Both services may have restarted in the supported QR/desktop
            # fallback mode while SQLite still carries the old SMS pause.
            if exc.code != 'sms_disabled' or login_mode() == 'sms':
                raise
            self._sms_pause({'state': 'idle'})

    async def browser_status(self, *, details=False):
        from browser_login import LOGIN_STATES
        state, login_state = 'unavailable', 'unknown'
        try:
            result = await asyncio.to_thread(self.bridge, 'status', None, 2)
            state = result.get('state') if result.get('ok') else 'unavailable'
            state = state if state in ('browser_ready', 'login_required') else 'unavailable'
            login_state = result.get('login_state', 'unknown') if result.get('ok') else 'unknown'
            login_state = login_state if login_state in LOGIN_STATES else 'unknown'
            if state == 'browser_ready' and 'login_state' in result and login_state not in ('cookie_present', 'qr_session'):
                state = 'login_required'
        except Exception as exc:
            logger.warning('command=status %s', exc_summary(exc))
            state, login_state = 'unavailable', 'unknown'
        # Preserve the string return used by worker, resume and QR callers.
        return {'state': state, 'login_state': login_state} if details else state

    def _requeue_unattempted(self, job_id, attempt_day, *, busy=False):
        return self.store.requeue_job(job_id, attempt_day,
                                      self.busy_requeue_limit if busy else None)

    async def run_one(self):
        """Process at most one job. Attempts are persisted before touching the browser."""
        rows = self.store.db.execute("SELECT id,query FROM jobs WHERE status='queued' ORDER BY CASE WHEN json_extract(query,'$.kind') IS NULL THEN 0 ELSE 1 END,created").fetchall()
        blocked = set()
        for row in rows:
            platform = json.loads(row['query'])['platform']
            if platform in blocked:
                continue
            adapter = require_platform(platform)
            if not adapter.paused(self.store) and await adapter.run_one(self, row):
                return True
            blocked.add(platform)
        return False

    async def _run_douyin(self, row):
        query = json.loads(row['query'])
        cached = self.store.cached(query)
        if cached:
            self.store.update_job(row['id'], 'no_data' if cached.get('no_data') else 'succeeded', result=cached,
                                  error={'code': 'no_data', 'message': SAFE_ERRORS['no_data']} if cached.get('no_data') else None)
            with self.store.db:
                self.store.db.execute('UPDATE jobs SET cache_hit=1 WHERE id=?', (row['id'],))
            return True
        source_trend = self.store.trend_no_data(query) if query.get('kind') else None
        if source_trend:
            code = 'skipped_trend_no_data'
            self.store.update_job(row['id'], 'no_data', error={
                'code': code, 'message': SAFE_ERRORS[code], 'source_trend': source_trend})
            return True
        now = time.time()
        if now - self.store.get('last_attempt_at', 0) < 15:
            return False
        today = dt.datetime.now(TZ).date().isoformat()
        counter = self.store.get('daily', {'date': today, 'count': 0})
        if counter['date'] != today:
            counter = {'date': today, 'count': 0}
        if counter['count'] >= 50:
            self.store.update_job(row['id'], 'failed', error={'code': 'daily_limit', 'message': SAFE_ERRORS['daily_limit']})
            return True
        # Socket readiness checking never calls the upstream. No hidden login request.
        state = await self.browser_status()
        # A login may have been admitted while the status preflight awaited.
        if self.store.get('paused'):
            return False
        if state != 'browser_ready':
            code = 'authentication_required' if state == 'login_required' else 'browser_unavailable'
            if code == 'browser_unavailable':
                self.store.set('paused', {'code': code, 'since': now})
                logger.warning('collection paused code=browser_unavailable')
                return True
            self.store.update_job(row['id'], 'failed', error={'code': code, 'message': SAFE_ERRORS[code]})
            if code in PAUSING:
                self.store.set('paused', {'code': code, 'since': now})
            return True
        self.store.update_job(row['id'], 'running')
        self.store.set('last_attempt_at', now)
        self.store.set('daily', {'date': today, 'count': counter['count'] + 1})
        started = time.monotonic()
        try:
            response = await asyncio.to_thread(self.bridge, require_platform(query['platform']).bridge_command(query), query, hub_bridge.BRIDGE_TIMEOUT)
            # Only collection replies reset the streak; status preflights do not.
            self.store.set('consecutive_bridge_timeouts', 0)
            if response.get('ok') is not True:
                code = response.get('code', '')
                if code in {'cooldown', 'busy'}:
                    self._requeue_unattempted(row['id'], today, busy=True)
                    return True
                if code == 'no_data' and query.get('kind'):
                    self.store.save_insight(query, {'no_data': True, 'metadata': {**query, 'fetched_at': dt.datetime.now(TZ).isoformat()}})
                    self.store.update_job(row['id'], 'no_data', error={'code':code,'message':SAFE_ERRORS[code]})
                    self.store.set('last_result', {'ok': True, 'at': time.time()})
                    return True
                if code not in SAFE_ERRORS:
                    code = 'authentication_required' if '登录' in str(response.get('message', '')) else 'request_failed'
                details={'code':code,'message':SAFE_ERRORS[code]}
                http=response.get('http_status')
                business=response.get('business_code')
                if type(http) is int and 100<=http<=599:details['http_status']=http
                if type(business) is int or isinstance(business,str) and len(business)<=64 and all(c.isalnum() or c in '_-.' for c in business):details['business_code']=business
                kd=response.get('keyword_details')
                if code=='unexpected_keyword' and isinstance(kd,dict) and all(isinstance(kd.get(k),list) and len(kd[k])<=10 and all(isinstance(v,str) and len(v)<=100 for v in kd[k]) for k in ('requested','returned')):details['keyword_details']={k:kd[k] for k in ('requested','returned')}
                if http==422:details['message']='平台拒绝请求参数（HTTP 422），请检查接口参数或支持的日期范围。'
                if code in PARAMETER_ERRORS:
                    with self.store.db:
                        self.store.refund_attempt(today)
                self.store.update_job(row['id'], 'failed', error=details)
                self.store.set('last_result', {'ok': False, 'code': code, 'at': time.time()})
                if code in PAUSING:
                    self.store.set('paused', {'code': code, 'since': time.time()})
            else:
                if query.get('kind'):
                    result = {'data':response['data'],'metadata':response['metadata']}
                    self.store.save_insight(query,result)
                    self.store.update_job(row['id'],'succeeded',result=result)
                else:
                    result = require_platform(query['platform']).project(response)
                    self.store.ingest(result)
                    has_values=any(r['search_index'] is not None for r in result['rows'])
                    self.store.update_job(row['id'], 'succeeded' if result['metadata']['complete'] else 'partial' if has_values else 'no_data', result=result,
                                          error=None if has_values else {'code':'no_data','message':SAFE_ERRORS['no_data']})
                self.store.set('last_result', {'ok': True, 'at': time.time()})
        except asyncio.CancelledError:
            # Restart handling marks any interrupted job failed; never automatically replay it.
            raise
        except Exception as exc:
            if isinstance(exc, (socket.timeout, TimeoutError)):
                code = 'bridge_timeout'
                # Do not refund: the browser may already have sent the upstream request.
                count = self.store.get('consecutive_bridge_timeouts', 0) + 1
                self.store.set('consecutive_bridge_timeouts', count)
                if self.bridge_timeout_pause_after and count >= self.bridge_timeout_pause_after:
                    self.store.set('paused', {'code': code, 'since': time.time(), 'count': count})
                    logger.warning('collection paused code=bridge_timeout count=%d', count)
            elif isinstance(exc, (ConnectionRefusedError, FileNotFoundError)):
                self._requeue_unattempted(row['id'], today)
                self.store.set('paused', {'code': 'browser_unavailable', 'since': time.time()})
                logger.warning('job=%s collection paused code=browser_unavailable %s', row['id'], exc_summary(exc))
                return True
            else:
                code = 'request_failed'
            log = logger.error if code == 'request_failed' else logger.warning
            log('job=%s kind=%s elapsed=%.3fs %s', row['id'],
                query.get('kind', 'query'), time.monotonic() - started, exc_summary(exc))
            self.store.update_job(row['id'], 'failed', error={'code': code, 'message': SAFE_ERRORS[code]})
            self.store.set('last_result', {'ok': False, 'code': code, 'at': time.time()})
        return True

    async def worker(self):
        while True:
            try:
                try:
                    await self.sync_sms_pause()
                except HubError:
                    pass
                await self.run_one()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error('worker %s', exc_summary(exc))
                self.store.set('worker_error', '任务处理暂不可用，请检查服务。')
            await asyncio.sleep(1)

    async def run_hot_once(self, *, client=None, now=None):
        """Fetch one hot-list snapshot. Does not use the search-index quota."""
        if not hotlist_enabled():
            return {'ok': False, 'code': 'disabled'}
        if self.store.get('hot_paused'):
            return {'ok': False, 'code': 'paused'}
        now = time.time() if now is None else now
        last = self.store.get('hot_last_attempt_at') or 0
        if now - last < hotlist_interval_seconds():
            return {'ok': False, 'code': 'interval'}
        day = dt.datetime.fromtimestamp(now, TZ).date().isoformat()
        counter = self.store.get('hot_daily') or {'date': day, 'count': 0}
        if counter.get('date') != day:
            counter = {'date': day, 'count': 0}
        if counter.get('count', 0) >= HOT_DAILY_SUCCESS_LIMIT:
            return {'ok': False, 'code': 'daily_limit'}
        # Record the attempt before the request so a failure is not fetched again
        # until the interval elapses. This does not touch the search-index counter.
        self.store.set('hot_last_attempt_at', now)
        owns_client = client is None
        client = client or self.hot_client_factory()
        try:
            snapshot = await asyncio.to_thread(client.fetch)
            self.store.save_hot_snapshot(snapshot)
        except HotListError as exc:
            if exc.code in HOT_PAUSE_CODES:
                paused = {'code': exc.code, 'since': now}
                if type(exc.http_status) is int:
                    paused['http_status'] = exc.http_status
                self.store.set('hot_paused', paused)
                logger.warning('hotlist paused code=%s', exc.code)
            else:
                logger.warning('hotlist stopped code=%s', exc.code)
            return {'ok': False, 'code': exc.code}
        finally:
            if owns_client:
                client.close()
        self.store.set('hot_daily', {'date': day, 'count': counter['count'] + 1})
        self.store.set('hot_last_success_at', now)
        return {'ok': True, 'fetched_at': snapshot['fetched_at']}

    async def run_baidu_hot_once(self, *, client=None, now=None):
        if not baidu_hotlist_enabled():
            return {'ok': False, 'code': 'disabled'}
        if self.store.get('baidu_hot_paused'):
            return {'ok': False, 'code': 'paused'}
        now = time.time() if now is None else now
        if now - (self.store.get('baidu_hot_last_attempt_at') or 0) < baidu_hotlist_interval_seconds():
            return {'ok': False, 'code': 'interval'}
        day = dt.datetime.fromtimestamp(now, TZ).date().isoformat()
        counter = self.store.get('baidu_hot_daily') or {'date': day, 'count': 0}
        if counter.get('date') != day:
            counter = {'date': day, 'count': 0}
        if counter.get('count', 0) >= HOT_DAILY_SUCCESS_LIMIT:
            return {'ok': False, 'code': 'daily_limit'}
        self.store.set('baidu_hot_last_attempt_at', now)
        owns_client = client is None
        client = client or self.baidu_hot_client_factory()
        stored = 0
        try:
            for tab in baidu_hotlist_tabs():
                try:
                    snapshot = await asyncio.to_thread(client.fetch, tab)
                    self.store.save_hot_snapshot(snapshot)
                    stored += 1
                except HotListError as exc:
                    if exc.code in HOT_PAUSE_CODES:
                        paused = {'code': exc.code, 'since': now}
                        if type(exc.http_status) is int:
                            paused['http_status'] = exc.http_status
                        self.store.set('baidu_hot_paused', paused)
                        break
                    logger.warning('baidu hotlist stopped code=%s message=%s', exc.code, str(exc))
        finally:
            if owns_client:
                client.close()
        if stored:
            self.store.set('baidu_hot_daily', {'date': day, 'count': counter['count'] + 1})
            self.store.set('baidu_hot_last_success_at', now)
        return {'ok': bool(stored), 'stored': stored}

    async def baidu_hotlist_worker(self):
        while True:
            try:
                await self.run_baidu_hot_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error('baidu hotlist %s', exc_summary(exc))
            await asyncio.sleep(HOT_POLL_SECONDS)

    async def hotlist_worker(self):
        while True:
            try:
                await self.run_hot_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error('hotlist %s', exc_summary(exc))
            await asyncio.sleep(HOT_POLL_SECONDS)

    def _topic_daily(self, now):
        day = dt.datetime.fromtimestamp(now, TZ).date().isoformat()
        counter = self.store.get('topic_daily') or {'date': day, 'count': 0}
        if counter.get('date') != day:
            counter = {'date': day, 'count': 0}
        return counter

    async def topic_request(self, job, wait=False):
        deadline = time.monotonic() + 30
        try:
            if wait:
                await asyncio.wait_for(self._topic_lock.acquire(), timeout=30)
            else:
                await self._topic_lock.acquire()
        except asyncio.TimeoutError:
            raise HubError('interval', '采集排队超时，请稍后重试。', 429) from None
        try:
            if self.store.get('topic_paused'):
                raise HubError('paused', '话题采集已暂停，请先恢复。', 409)
            now = time.time()
            gate = request_gate(now, self.store.get('topic_request_at'), int(self._topic_daily(now).get('count') or 0))
            if gate == 'interval' and wait:
                delay = MIN_GAP_SECONDS - (now - self.store.get('topic_request_at'))
                if delay > deadline - time.monotonic():
                    raise HubError('interval', '采集等待时间过长，请稍后重试。', 429)
                await asyncio.sleep(max(0, delay))
                if self.store.get('topic_paused'):
                    raise HubError('paused', '话题采集已暂停，请先恢复。', 409)
                now = time.time()
                gate = request_gate(now, self.store.get('topic_request_at'), int(self._topic_daily(now).get('count') or 0))
            if gate != 'ok':
                message = '今日话题采集次数已达上限，请明天再试。' if gate == 'daily_limit' else '未到下一次话题请求时间，请稍后重试。'
                raise HubError(gate, message, 429)
            result = await asyncio.to_thread(self.bridge, 'topic', job, hub_bridge.BRIDGE_TIMEOUT)
            return record_topic_result(self.store, job, result)
        finally:
            self._topic_lock.release()

    async def run_topic_round(self):
        """One serial round. Disabled unless INDEX_HUB_TOPIC_ENABLED is exactly 1."""
        if not topic_enabled() or self.store.get('topic_paused'):
            return {'ok': False, 'code': 'paused' if self.store.get('topic_paused') else 'disabled'}
        now = time.time()
        last_round = self.store.get('topic_round_at') or 0
        if last_round and now - last_round < round_interval_seconds():
            return {'ok': False, 'code': 'interval'}
        tags = self.store.get('topic_categories') or []
        if not isinstance(tags, list):
            tags = []
        cursor = self.store.get('topic_cursor') or 0
        stale = not tags or (self.store.get('topic_tags_at') or 0) < now - 86400
        jobs = plan_round(tags, int(cursor or 0), include_tags=stale)
        started = False
        for job in jobs:
            now = time.time()
            gate = request_gate(now, self.store.get('topic_request_at'), int(self._topic_daily(now).get('count') or 0))
            if gate == 'daily_limit':
                break
            if gate == 'interval':
                wait = MIN_GAP_SECONDS - (now - self.store.get('topic_request_at'))
                if wait > 0:
                    await asyncio.sleep(wait)
            try:
                result = await self.topic_request(job)
            except HubError as exc:
                return {'ok': False, 'code': exc.code}
            if result.get('code') == 'busy':
                if not started:
                    return {'ok': False, 'code': 'busy'}
                break
            started = True
            if not result.get('ok'):
                if not self.store.get('topic_round_at'):
                    self.store.set('topic_round_at', time.time())
                logger.warning('topic stopped code=%s', result.get('code'))
                return {'ok': False, 'code': result.get('code')}
        if started:
            self.store.set('topic_round_at', time.time())
            self.store.set('topic_cursor', int(cursor or 0) + 1)
        return {'ok': True}

    async def topic_worker(self):
        while True:
            try:
                await self.run_topic_round()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error('topic %s', exc_summary(exc))
            await asyncio.sleep(HOT_POLL_SECONDS)

    async def start(self, app):
        counts = self.store.interrupt_pending()
        logger.info('interrupt_pending requeued=%d interrupted=%d', counts['requeued'], counts['interrupted'])
        self.store.import_results(self.root / 'results')
        self.worker_task = asyncio.create_task(self.worker(), name='index-hub-worker')
        if hotlist_enabled():
            self.hotlist_task = asyncio.create_task(self.hotlist_worker(), name='index-hub-hotlist')
        if baidu_hotlist_enabled():
            self.baidu_hotlist_task = asyncio.create_task(self.baidu_hotlist_worker(), name='index-hub-baidu-hotlist')
        # Topic boards are collected only when the workbench asks for one vertical.
        # INDEX_HUB_TOPIC_ENABLED does not start this loop.

    async def close(self, app):
        for task in (self.worker_task, self.hotlist_task, self.baidu_hotlist_task, self.topic_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.store.close()


def require(request, scope='read', admin=False):
    p = request.get(PRINCIPAL_KEY, {})
    if admin and not p.get('admin') or scope not in p.get('scopes', []):
        raise HubError('forbidden', '此 API Key 无相应权限。', 403)
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and p.get('admin'):
        origin = request.headers.get('Origin')
        if request.headers.get('X-Hub-Request') != '1' or (origin and origin != 'https://' + request.host):
            raise HubError('csrf_rejected', '请求未通过同源校验。', 403)
    return p


def reply(value, status=200):
    return web.json_response(value, status=status, dumps=lambda x: json.dumps(x, ensure_ascii=False, allow_nan=False))


async def read_json(request):
    if request.content_type != 'application/json':
        raise HubError('json_required', '请使用 Content-Type: application/json。', 415)
    try:
        def unique(items):
            obj = {}
            for k, v in items:
                if k in obj:
                    raise ValueError
                obj[k] = v
            return obj
        return await request.json(loads=lambda text: json.loads(text, object_pairs_hook=unique,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError())))
    except (ValueError, UnicodeError):
        raise HubError('invalid_json', 'JSON 格式无效。') from None


def _hotlist_status(store, day):
    counter = store.get('hot_daily') or {'date': day, 'count': 0}
    if not isinstance(counter, dict) or counter.get('date') != day:
        successes = 0
    else:
        successes = counter.get('count') or 0
    return {'enabled': hotlist_enabled(), 'paused': store.get('hot_paused'),
            'interval_seconds': hotlist_interval_seconds(),
            'daily_success_limit': HOT_DAILY_SUCCESS_LIMIT, 'daily_successes': successes,
            'last_attempt_at': store.get('hot_last_attempt_at'),
            'last_success_at': store.get('hot_last_success_at'),
            'source': HOT_LIST_URL,
            'note': '热榜调度默认关闭，与搜索指数每日 50 次分开计数。' + HOT_NOTE}


def _baidu_hotlist_status(store, day):
    counter = store.get('baidu_hot_daily') or {}
    return {'enabled': baidu_hotlist_enabled(), 'paused': store.get('baidu_hot_paused'),
            'interval_seconds': baidu_hotlist_interval_seconds(), 'daily_success_limit': HOT_DAILY_SUCCESS_LIMIT,
            'daily_successes': counter.get('count', 0) if counter.get('date') == day else 0,
            'last_attempt_at': store.get('baidu_hot_last_attempt_at'),
            'last_success_at': store.get('baidu_hot_last_success_at'),
            'boards': ['baidu_' + tab for tab in baidu_hotlist_tabs()], 'source': SOURCE + '<tab>', 'note': BAIDU_NOTE}


def openapi():
    error = {'description': '结构化错误；不包含认证信息', 'content': {'application/json': {'schema': {'type': 'object'}}}}
    paths = {}
    for path, method, summary in [
        ('/api/v1/platforms', 'get', '列出平台与接入状态'),
        ('/api/v1/status', 'get', '浏览器与采集状态（不触发平台取数）'),
        ('/api/v1/queries', 'post', '提交单词或小批关键词的自定义日期查询任务'),
        ('/api/v1/jobs', 'get', '列出最近任务'),
        ('/api/v1/jobs/{job_id}', 'get', '读取任务状态及结果'),
        ('/api/v1/series', 'get', '查询本地已存储趋势，不触发平台取数'),
        ('/api/v1/history', 'get', '查询采集历史'),
        ('/api/v1/insights', 'get', '读取已保存的关联词或人群画像'),
        ('/api/v1/insights/ranges', 'get', '列出全部已存扩展日期段及每段最近任务状态（只读）'),
        ('/api/v1/insights/queries', 'post', '提交扩展采集任务（试验接入）'),
        ('/api/v1/export.csv', 'get', '导出已存储趋势 CSV'),
        ('/api/v1/hot/latest', 'get', '读取最近一次完整热榜快照，不触发抓取'),
        ('/api/v1/hot/word', 'get', '读取一个话题的实际上榜记录，不触发抓取'),
        ('/api/v1/hot/rising', 'get', '按本库热榜快照计算飙升指标，不触发抓取'),
    ]:
        operation = {'summary': summary, 'operationId': method + '_' + path.rsplit('/', 1)[-1].replace('.', '_').replace('{', '').replace('}', ''),
                     'responses': {'200': {'description': '成功'}, '400': error, '401': error, '403': error, '429': error}}
        if method == 'post':
            operation['parameters'] = [{'in': 'header', 'name': 'Idempotency-Key', 'required': True,
                                        'schema': {'type': 'string', 'maxLength': 100}}]
            operation['requestBody'] = {'required': True, 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Query'}}}}
            operation['responses']['202'] = {'description': '任务已排队；轮询 jobs/{job_id}，不要重复提交新幂等键'}
        if path == '/api/v1/insights/queries':
            operation['operationId'] = 'post_insight_query'
            operation['requestBody']['content']['application/json']['schema'] = {'type':'object','additionalProperties':False,'required':['keywords','kind','end_date'],'properties':{
                'keywords':{'type':'array','minItems':1,'maxItems':1,'items':{'type':'string'}},'kind':{'enum':['related','audience']},'platform':{'enum':[p['id'] for p in registry.descriptions() if p['available']]},'start_date':{'type':'string','format':'date'},'end_date':{'type':'string','format':'date'}}}
        if path == '/api/v1/insights':
            operation['parameters']=[{'in':'query','name':k,'required':k in ('keyword','kind','end_date'),'schema':{'type':'string'}} for k in ('keyword','kind','start_date','end_date')]
        if path == '/api/v1/insights/ranges':
            operation['parameters']=[{'in':'query','name':k,'required':True,'schema':{'type':'string'}} for k in ('keyword','kind')]
        if '{job_id}' in path:
            operation['parameters'] = [{'in': 'path', 'name': 'job_id', 'required': True, 'schema': {'type': 'string'}}]
        if path.endswith(('series', 'export.csv')):
            operation['parameters'] = [{'in': 'query', 'name': k, 'required': k == 'keyword',
                                        'schema': {'type': 'string'}} for k in ('platform', 'keyword', 'start_date', 'end_date')]
        if path in ('/api/v1/hot/latest', '/api/v1/hot/rising'):
            operation['parameters'] = [{'in': 'query', 'name': 'board', 'schema': {'type': 'string', 'default': 'word_list'}}]
        if path == '/api/v1/hot/word':
            operation['parameters'] = [{'in': 'query', 'name': name, 'required': False,
                                        'schema': {'type': 'string'}} for name in ('sentence_id', 'word', 'board')]
        paths[path] = {method: operation}
    paths['/api/v1/hot/resume'] = {'post': {
        'summary': '人工恢复热榜调度；scope 或 platform 为 baidu 时仅恢复百度。不立即补抓。',
        'requestBody': {'required': False, 'content': {'application/json': {'schema': {'type': 'object', 'properties': {'scope': {'type': 'string'}, 'platform': {'type': 'string'}}}}}},
        'responses': {'200': {'description': '已恢复'}, '401': error, '403': error}}}
    paths['/api/v1/groups'] = {
        'get': {'summary': '列出关键词组，不计算分数也不触发采集',
                'responses': {'200': {'description': '成功'}, '401': error, '403': error}},
        'post': {'summary': '创建关键词组。不提交搜索指数任务。',
                 'requestBody': {'required': True, 'content': {'application/json': {'schema': {'type': 'object'}}}},
                 'responses': {'201': {'description': '已创建'}, '400': error, '401': error, '403': error}}}
    paths['/api/v1/groups/{id}'] = {
        'put': {'summary': '更新关键词组的名称和关键词。不提交搜索指数任务。',
                'parameters': [{'in': 'path', 'name': 'id', 'required': True, 'schema': {'type': 'string'}}],
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {'type': 'object'}}}},
                'responses': {'200': {'description': '已更新'}, '400': error, '401': error, '403': error, '404': error}},
        'delete': {'summary': '删除关键词组，不删除已保存的搜索指数',
                   'parameters': [{'in': 'path', 'name': 'id', 'required': True, 'schema': {'type': 'string'}}],
                   'responses': {'200': {'description': '已删除'}, '401': error, '403': error, '404': error}}}
    paths['/api/v1/groups/{id}/score'] = {
        'get': {'summary': '用已保存数据计算工作台热度分，不触发采集',
                'parameters': [{'in': 'path', 'name': 'id', 'required': True, 'schema': {'type': 'string'}}],
                'responses': {'200': {'description': '成功'}, '401': error, '403': error, '404': error}}}
    paths['/api/v1/topics'] = {
        'get': {'summary': '读取已保存的热点宝话题。缺失为 null。采集只在 INDEX_HUB_TOPIC_ENABLED=1 时由调度进行。',
                'parameters': [{'in': 'query', 'name': name, 'required': False, 'schema': {'type': 'string'}}
                               for name in ('category', 'rank', 'window')],
                'responses': {'200': {'description': '成功；items 可为空'}, '400': error, '401': error, '403': error}}}
    for path, method, summary in (
        ('/api/v1/topics/login', 'get', '热点宝登录状态'),
        ('/api/v1/topics/login/qr', 'get', '放大的热点宝登录二维码 PNG'),
        ('/api/v1/topics/login/refresh', 'post', '打开或刷新热点宝授权页'),
        ('/api/v1/topics/categories/refresh', 'post', '请求一次热点宝垂类，受间隔、每日上限和暂停限制')):
        paths[path] = {method: {'summary': summary + '（仅管理员）',
                               'responses': {'200': {'description': '成功'}, '403': error, '409': error, '429': error, '503': error}}}
    paths['/api/v1/topics/collect-once'] = {'post': {
        'summary': '管理员通过现有浏览器会话打一次话题请求。不重试。',
        'responses': {'200': {'description': '状态、条数和字段名'}, '401': error, '403': error, '409': error, '429': error}}}
    paths['/api/v1/topics/resume'] = {'post': {
        'summary': '人工恢复话题采集。不立即补抓。',
        'responses': {'200': {'description': '已恢复'}, '401': error, '403': error}}}
    for path, method in (('/api/v1/login/qr', 'get'), ('/api/v1/login/qr/image', 'get'),
                         ('/api/v1/login/qr/start', 'post'), ('/api/v1/login/qr/cancel', 'post')):
        paths[path] = {method: {'summary': '管理员扫码登录（需网关认证，POST 需 CSRF 校验）',
                              'responses': {'200': {'description': '成功'}, '403': error, '409': error, '429': error, '503': error}}}
    for action in ('status', 'frame', 'start', 'code', 'resend', 'pointer', 'text', 'cancel'):
        path = '/api/v1/login/sms' + ('' if action == 'status' else '/' + action)
        method = 'get' if action in ('status', 'frame') else 'post'
        paths[path] = {method: {'summary': '管理员短信登录（仅 Basic；POST 需 X-Hub-Request 和同源校验）',
            'security': [{'BasicAuth': []}],
            'responses': {'200': {'description': '状态快照或 PNG；no-store'},
                          **{str(code): error for code in (400, 401, 403, 409, 429, 503)}}}}
    for p in registry.descriptions():
        platform = registry.get(p['id'])
        if p['available'] and hasattr(platform, 'openapi_paths'):
            paths.update(platform.openapi_paths())
    return {'openapi': '3.1.0', 'info': {'title': 'Index Atlas 指数工作台 API', 'version': '1.0.0',
             'description': '自用平台聚合接口。抖音与百度搜索指数支持 1–5 词、最近两年；热榜是独立快照，不是搜索指数。非官方开放 API。搜索指数不是实际搜索次数。'},
            'servers': [{'url': '/'}], 'security': [{'BearerAuth': []}], 'paths': paths,
            'components': {'securitySchemes': {'BearerAuth': {'type': 'http', 'scheme': 'bearer'},
                                               'BasicAuth': {'type': 'http', 'scheme': 'basic'}},
             'schemas': {'Query': {'type': 'object', 'additionalProperties': False, 'required': ['keywords', 'end_date'],
              'properties': {'platform': {'type': 'string', 'enum': [p['id'] for p in registry.descriptions() if p['available']], 'default': 'douyin'},
               'keywords': {'type': 'array', 'minItems': 1, 'maxItems': 5, 'uniqueItems': True,
                            'items': {'type': 'string', 'minLength': 1, 'maxLength': 100}},
               'start_date': {'type': 'string', 'format': 'date', 'description': '不填默认截止日前六天；填写后支持最近两个日历年内的过去日期'},
               'end_date': {'type': 'string', 'format': 'date', 'description': '过去的可用截止日，以官方页面为准'}}}}}}


def mount(app, hub):
    s = hub.store

    async def index(request):
        require(request, admin=True)
        return web.FileResponse(hub.root / 'hub.html')

    async def sms_login(request):
        require(request, admin=True)
        action = request.match_info.get('action', 'status')
        if request.method != 'POST' and request.can_read_body:
            raise sms_error('invalid_payload')
        if request.query and (action not in ('frame', 'qr') or set(request.query) != {'v'}
                              or len(request.query.getall('v')) != 1):
            raise sms_error('invalid_payload')
        if action in ('frame', 'qr'):
            version = request.query.get('v', '')
            if re.fullmatch(r'[0-9]{1,16}', version) is None:
                raise sms_error('invalid_frame')
            body = {'frame': int(version)}
        else:
            body = await read_json(request) if request.method == 'POST' else {}
        try:
            validate_sms_payload(action, body)
        except SmsLoginError as exc:
            raise sms_error(exc.code) from None
        if action == 'start':
            async with hub._sms_lock:
                previous = s.get('paused')
                provisional = {'code': 'login_in_progress'}
                s.set('paused', provisional)
                try:
                    result = await hub.sms_call(action, body)
                except HubError as exc:
                    # A timeout is ambiguous: keep paused until status resolves
                    # it. Known admission failures did not start a new round.
                    # Collection may have set a safety pause while the bridge
                    # was awaited. Roll back only our still-current pause.
                    if (exc.status != 503 and exc.code != 'login_busy'
                            and s.get('paused') == provisional):
                        s.set('paused', previous)
                    raise
                hub._sms_pause(result)
        else:
            result = await hub.sms_call(action, body)
            if action not in ('frame', 'qr'):
                # Re-read under the lock: an older terminal response must not
                # clear a new start's login pause.
                await hub.sync_sms_pause()
        if action in ('frame', 'qr'):
            return web.Response(body=result, content_type='image/png', headers={'Cache-Control': 'no-store'})
        if action == 'status':
            result['browser'] = (await hub.browser_status() if result['state'] in TERMINAL | {'idle'}
                                 else 'login_in_progress')
        return reply({'ok': True, **result, 'login_mode': login_mode()})

    async def qr_login(request):
        require(request, admin=True)
        action = request.match_info.get('action', 'status')
        if action == 'start':
            now = time.monotonic()
            if hub._qr_last_start is not None and now - hub._qr_last_start < 10:
                raise HubError('qr_rate_limited', '请等待 10 秒后再启动扫码。', 429)
            hub._qr_last_start = now
        try:
            result = await asyncio.to_thread(hub.bridge, 'qr_' + action, None, 2)
        except Exception:
            raise HubError('browser_unavailable', '浏览器未就绪，请检查远程桌面。', 503) from None
        if not result.get('ok'):
            code = result.get('code', 'browser_unavailable')
            status = 404 if code == 'qr_not_ready' else 409 if code in ('qr_busy', 'login_busy', 'busy_fetching', 'qr_disabled', 'qr_unsafe_debug', 'qr_no_display') else 503
            messages = {'qr_no_display': '有头扫码需要桌面显示环境（DISPLAY）', 'qr_busy': '扫码登录正在进行，请先取消或等待结束。', 'qr_disabled': '未启用扫码登录。',
                        'login_busy': '另一种登录正在进行，请先取消或等待结束。', 'busy_fetching': '正在采集，请等待结束后登录。',
                        'qr_unsafe_debug': '浏览器调试环境变量已开启，已拒绝启动。', 'qr_not_ready': '二维码尚未就绪。'}
            return reply({'error': {'code': code, 'message': messages.get(code, '浏览器未就绪，请检查远程桌面。')}}, status)
        if action == 'image':
            return web.Response(body=base64.b64decode(result['png_base64'], validate=True),
                                content_type='image/png', headers={'Cache-Control': 'no-store'})
        if action == 'status' and result.get('state') not in ('starting', 'qr_ready'):
            result = {**result, 'browser': await hub.browser_status()}
        return reply({**result, 'login_mode': login_mode()})

    async def platforms(request):
        require(request)
        return reply({'platforms': registry.descriptions()})

    async def status(request):
        require(request)
        day = dt.datetime.now(TZ).date().isoformat()
        daily = s.get('daily', {'date': day, 'count': 0})
        if daily['date'] != day:
            daily = {'date': day, 'count': 0}
        browser = await hub.browser_status(details=True)
        next_attempt = s.get('last_attempt_at', 0) + 15
        remaining = max(0, math.ceil(next_attempt - time.time()))
        words = {r[0] for r in s.db.execute('SELECT DISTINCT keyword FROM points')}
        point_count = s.db.execute('SELECT count(*) FROM points').fetchone()[0]
        for p in registry.descriptions():
            adapter = registry.get(p['id'])
            if p['available'] and hasattr(adapter, 'status_counts'):
                extra_words, extra_points = adapter.status_counts(s)
                words.update(extra_words)
                point_count += extra_points
        platform_status = {}
        for p in registry.descriptions():
            platform = registry.get(p['id'])
            if p['available']:
                async_status = getattr(platform, 'async_status', None)
                platform_status[p['id']] = await async_status(hub) if async_status else platform.status(s)
        topic_counter = s.get('topic_daily') or {'date': day, 'count': 0}
        if topic_counter.get('date') != day:
            topic_counter = {'date': day, 'count': 0}
        return reply({'login_mode': login_mode(), 'version': '1.0.0', 'browser': browser['state'],
                      'login_state': browser['login_state'], 'paused': s.get('paused'),
                      'platform_status': platform_status,
                      'last_result': s.get('last_result'), 'worker_error': s.get('worker_error'),
                      'next_online_attempt_at': next_attempt,
                      'cooldown_remaining_seconds': remaining,
                      'daily_online_attempts': daily['count'],
                      'consecutive_bridge_timeouts': s.get('consecutive_bridge_timeouts', 0),
                      'limits': {'keywords': 5, 'days': 731, 'online_interval_seconds': 15,
                                 'online_attempts_per_day': 50, 'cache_hours': 24,
                                 'note': '以上为本服务保护设置，不是抖音官方配额。'},
                      'hotlist': _hotlist_status(s, day),
                      'baidu_hotlist': _baidu_hotlist_status(s, day),
                      'topics': {'enabled': topic_enabled(), 'paused': s.get('topic_paused'),
                                 'interval_seconds': round_interval_seconds(),
                                 'min_interval_seconds': MIN_GAP_SECONDS,
                                 'daily_limit': DAILY_LIMIT, 'daily_count': topic_counter.get('count') or 0},
                      'counts': {'datasets': s.db.execute('SELECT count(*) FROM datasets').fetchone()[0],
                                 'keywords': len(words), 'points': point_count}})

    def job_view(value, request):
        result = dict(value)
        if not request[PRINCIPAL_KEY].get('admin'):
            result.pop('request_source', None)
        return result

    async def submit(request):
        p = require(request, 'query')
        from hub_insights import validate_insight
        body = await read_json(request)
        submitted = None
        if request.path.endswith('/insights/queries'):
            submitted = validate_insight(body, historical=True)
            if 'requested_end_date' in body:
                submitted['requested_end_date'] = body['requested_end_date']
            existing = s.db.execute('SELECT query,request_source FROM jobs WHERE owner=? AND idem=?',
                                   (p['id'], request.headers.get('Idempotency-Key'))).fetchone()
            original = (json.loads(existing['request_source'] or '{}').get('submitted_query')
                        if existing else None)
            if original is not None:
                if submitted != original:
                    raise HubError('idempotency_conflict', '该幂等键已用于其他查询。', 409)
                query = json.loads(existing['query'])
            else:
                query = validate_insight(body, latest_related_date=related_latest_date(s))
        else:
            query = validate_query(body)
        # Transport peer only: do not trust caller-supplied X-Forwarded-For.
        user_agent = ''.join(c for c in request.headers.get('User-Agent', '') if ord(c) >= 32 and ord(c) != 127)[:512]
        source = {'kind': 'admin' if p.get('admin') else 'api_key',
                  'peer_ip': request.remote, 'user_agent': user_agent,
                  'recorded_at': time.time()}
        if submitted is not None:
            # Preserve replay identity without adding request dates to cache/worker keys.
            source['submitted_query'] = submitted
        if not p.get('admin'):
            key = s.db.execute('SELECT name,prefix FROM keys WHERE id=?', (p['id'],)).fetchone()
            source.update({'key_name': key['name'], 'key_prefix': key['prefix']})
        job, created = s.submit(query, p['id'], request.headers.get('Idempotency-Key'), source)
        return reply({'job': job_view(job, request)}, 202 if job['status'] == 'queued' else 200)

    async def jobs(request):
        require(request)
        return reply({'jobs': [job_view(s._job(r), request) for r in s.db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 30')]})

    async def job(request):
        require(request)
        return reply({'job': job_view(s.job(request.match_info['job_id']), request)})

    def get_series(request):
        platform = request.query.get('platform', 'douyin')
        unknown = set(request.query) - ({'platform', 'keyword', 'start_date', 'end_date', 'device', 'metric'} | ({'region'} if platform == 'baidu' else set()))
        if unknown:
            raise HubError('invalid_parameters', '包含不支持的查询参数。')
        device = request.query.get('device')
        platform = request.query.get('platform', 'douyin')
        if device is not None and device not in (('all', 'pc', 'wise', 'all_devices') if platform == 'baidu' else ('all', 'pc', 'wise')):
            raise HubError('invalid_parameters', '不支持的设备。')
        metric = request.query.get('metric')
        if metric is not None and (platform != 'baidu' or metric not in ('search_index', 'feed_index', 'news_index')):
            raise HubError('invalid_parameters', '不支持的指标。')
        args = (s, request.query.get('keyword'), request.query.get('start_date'), request.query.get('end_date'))
        if platform == 'baidu':
            return require_platform(platform).series(*args, device=device or 'all', metric=metric or 'search_index', region=request.query.get('region', '全国'))
        return s.series(platform, *args[1:])

    async def series(request):
        require(request)
        return reply(get_series(request))

    async def export(request):
        require(request)
        data = get_series(request)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerows(require_platform(data['platform']).csv_rows(data))
        return web.Response(body=('\ufeff' + output.getvalue()).encode(), content_type='text/csv',
                            charset='utf-8', headers={'Content-Disposition': 'attachment; filename="index-series.csv"'})

    async def insight_ranges(request):
        require(request)
        if set(request.query) - {'keyword', 'kind', 'platform'} or any(len(request.query.getall(k)) != 1 for k in request.query):
            raise HubError('invalid_parameters', '仅接受单个 keyword、kind 和 platform。')
        word, kind = request.query.get('keyword'), request.query.get('kind')
        if kind not in {'audience', 'related'}:
            raise HubError('invalid_kind', 'kind 须为 related 或 audience。')
        if (not isinstance(word, str) or not 1 <= len(word) <= 100 or word != word.strip()
                or any(ord(c) < 32 or ord(c) == 127 for c in word)):
            raise HubError('invalid_keywords', '请输入一个有效关键词，每词 1—100 字。')
        platform = request.query.get('platform', 'douyin')
        require_platform(platform)
        return reply(s.insight_ranges(word, kind, platform))

    async def insights(request):
        require(request)
        from hub_insights import validate_insight
        if set(request.query)-{'platform','keyword','kind','start_date','end_date'}:
            raise HubError('invalid_parameters','包含不支持的参数。')
        body={'platform':request.query.get('platform','douyin'),'keywords':[request.query.get('keyword')],
              'kind':request.query.get('kind'),'end_date':request.query.get('end_date')}
        if request.query.get('start_date'):body['start_date']=request.query['start_date']
        query=validate_insight(body, historical=True)
        result=s.insight(query)
        newer_no_data=None
        if result and result.get('no_data'):
            # A later empty answer must not hide data that was actually collected.
            real=s.latest_real_insight(query)
            if real:
                newer_no_data={'fetched_at':result['metadata'].get('fetched_at')}
                result=real
        status=('no_data' if result.get('no_data') else 'available') if result else 'not_collected'
        body={'status':status,'query':query,'result':result,'live_request':False}
        if newer_no_data:
            body['newer_no_data']=newer_no_data
        return reply(body)

    async def history(request):
        require(request)
        return reply({'datasets': [{k: r[k] for k in ('id', 'fetched', 'metadata')} for r in s.history()],
                      'latest_available_date': s.latest_available_date(),
                      'related_latest_available_date': related_latest_date(s),
                      'latest_available_date_source': 'stored_search_index'})

    async def keys(request):
        require(request, admin=True)
        if request.method == 'GET':
            return reply({'keys': s.list_keys()})
        body = await read_json(request)
        if type(body) is not dict or set(body) - {'name', 'scopes', 'days'}:
            raise HubError('invalid_key', 'API Key 参数无效。')
        return reply(s.issue_key(body.get('name'), body.get('scopes', ['read', 'query']), body.get('days', 30)), 201)

    async def revoke(request):
        require(request, admin=True)
        s.revoke_key(request.match_info['key_id'])
        return reply({'ok': True})

    async def resume(request):
        require(request, admin=True)
        body = await read_json(request) if request.can_read_body else {}
        if type(body) is not dict or set(body) - {'platform'}:
            raise HubError('invalid_query', '仅接受 platform。')
        adapter = require_platform(body.get('platform', 'douyin'))
        if adapter.description['id'] != 'douyin':
            await adapter.resume(hub)
            return reply({'ok': True, 'note': '已恢复。'})
        async with hub._sms_lock:
            if login_mode() == 'sms' or (s.get('paused') or {}).get('code') == 'login_in_progress':
                await hub._check_sms_pause()
                if (s.get('paused') or {}).get('code') == 'login_in_progress':
                    raise HubError('login_in_progress', '登录仍在进行，请等待结束后恢复采集。', 409)
            state = await hub.browser_status()
            if state != 'browser_ready':
                raise HubError('browser_not_ready', '请先通过远程桌面完成登录或验证。', 409)
            s.set('paused', None)
            s.set('consecutive_bridge_timeouts', 0)
            s.set('worker_error', None)
        return reply({'ok': True, 'note': '已恢复。保留原有冷却间隔与每日上限；失败任务不会重试。'})

    def hot_public(value):
        return {**value, 'live_request': False, 'note': HOT_NOTE}

    def hot_board(request, allowed):
        if set(request.query) - allowed or any(len(request.query.getall(key)) != 1 for key in request.query):
            raise HubError('invalid_parameters', '热榜查询参数无效。')
        board = request.query.get('board', 'word_list')
        if board != 'word_list' and board not in {'baidu_' + tab for tab in KNOWN_TABS}:
            raise HubError('invalid_parameters', '热榜类型无效。')
        return board

    async def hot_latest(request):
        require(request)
        board = hot_board(request, {'board'})
        result = hot_public(s.latest_hot(board))
        if board != 'word_list':
            result['note'] = BAIDU_NOTE
        return reply(result)

    async def hot_word(request):
        require(request)
        board = hot_board(request, {'sentence_id', 'word', 'board'})
        sentence_id, word = request.query.get('sentence_id'), request.query.get('word')
        if bool(sentence_id) == bool(word) or (board != 'word_list' and sentence_id is not None):
            raise HubError('invalid_parameters', '请指定 sentence_id 或精确话题之一。')
        if sentence_id is not None and _HOT_ID.fullmatch(sentence_id) is None:
            raise HubError('invalid_parameters', 'sentence_id 无效。')
        if word is not None and (word != word.strip() or not 1 <= len(word) <= 200
                                 or any(ord(char) < 32 or ord(char) == 127 for char in word)):
            raise HubError('invalid_parameters', '话题须为 1—200 字的精确文本。')
        history = s.hot_history(sentence_id=sentence_id, word=word, board=board)
        history['note'] = (HOT_NOTE if board == 'word_list' else BAIDU_NOTE) + '仅返回实际上榜记录。'
        history['live_request'] = False
        return reply(history)

    async def hot_rising(request):
        require(request)
        board = hot_board(request, {'board'})
        report = rising_report(s, board=board)
        report.update(live_request=False, note=RISING_NOTE + (BAIDU_NOTE if board != 'word_list' else ''))
        return reply(report)

    def group_id(request):
        identifier = request.match_info['id']
        if re.fullmatch(r'[0-9a-f]{32}', identifier) is None:
            raise HubError('group_not_found', '关键词组不存在。', 404)
        return identifier

    async def groups(request):
        if request.method == 'GET':
            require(request)
            return reply({'groups': s.list_groups(), 'live_request': False, 'note': GROUP_NOTE})
        require(request, 'query')
        name, keywords = validate_group(await read_json(request))
        return reply({'group': s.create_group(name, keywords), 'live_request': False,
                      'note': '已保存关键词组，未提交搜索指数任务。'}, 201)

    async def group_update(request):
        require(request, 'query')
        name, keywords = validate_group(await read_json(request))
        return reply({'group': s.update_group(group_id(request), name, keywords), 'live_request': False,
                      'note': '已更新关键词组，未提交搜索指数任务。'})

    async def group_delete(request):
        require(request, 'query')
        s.delete_group(group_id(request))
        return reply({'ok': True, 'live_request': False})

    async def group_score_view(request):
        require(request)
        return reply(group_score(s, s.get_group(group_id(request))))

    async def topics(request):
        require(request)
        names = category_names(s.get('topic_categories'))
        category, rank, window = validate_topic_query(request.query, names)
        snapshot = apply_empty_mark(s, s.latest_topics(rank=rank, window=window, category=category))
        return reply(topic_view(snapshot, enabled=topic_enabled(), paused=s.get('topic_paused'),
                                categories=names))

    async def topic_once(request):
        require(request, admin=True)
        body = await read_json(request)
        if type(body) is not dict or set(body) - {'rank', 'category'}:
            raise HubError('invalid_parameters', '只接受 rank 和 category。')
        rank = body.get('rank', 'total')
        category = body.get('category', '全部')
        names = category_names(s.get('topic_categories'))
        if rank not in ('total', 'rising') or category not in names or category == '全部':
            raise HubError('invalid_parameters', '请选择一个具体垂类的总榜或飙升。')
        now = time.time()
        key = (rank, category, '1d')
        if key in hub._topic_inflight:
            return reply({'status': 'in_flight', 'item_count': 0, 'fields': [], 'rank': rank,
                          'category': category, 'live_request': False})
        stored = apply_empty_mark(s, s.latest_topics(rank=rank, window='1d', category=category))
        if snapshot_is_fresh(stored, now):
            return reply({'status': 'cached', 'item_count': len(stored['items']), 'fields': [],
                          'rank': rank, 'category': category, 'live_request': False})
        if s.get('topic_paused'):
            raise HubError('paused', '话题采集已暂停，请先恢复。', 409)
        found = tag_for(s, category)
        job = {'action': 'board', 'rank': rank, 'tag_id': '' if found is None else str(found['id']),
               'parent_id': '' if found is None else str(found.get('parent_id') or ''),
               'child_ids': [] if found is None else [str(item) for item in (found.get('child_ids') or [])],
               'tag_name': category}
        hub._topic_inflight.add(key)
        try:
            result = await hub.topic_request(job, wait=True)
        finally:
            hub._topic_inflight.discard(key)
        status = 'ok' if result.get('ok') else (result.get('code') or 'browser_unavailable')
        messages = {'invalid_params': '这个垂类的参数不被热点宝接受，采集未暂停。'}
        return reply({'status': status, 'item_count': result.get('item_count') or 0,
                      'fields': result.get('fields') or [], 'rank': rank, 'category': category,
                      'message': messages.get(status),
                      'live_request': bool(result.get('requested'))})

    async def topic_login(request):
        require(request, admin=True)
        action = request.match_info.get('action', 'status')
        if request.method == 'POST' and await read_json(request) != {}:
            raise HubError('invalid_parameters', '请求体必须为空对象。')
        try:
            result = await asyncio.to_thread(hub.bridge, 'topic_login', action, hub_bridge.BRIDGE_TIMEOUT)
            state = result.get('state')
            if state not in ('logged_in', 'need_scan', 'expired', 'browser_unavailable'):
                state = 'browser_unavailable'
            available = state == 'need_scan' and result.get('qr_available') is True
            if action == 'qr':
                encoded = result.get('png_base64', '')
                if not available or not isinstance(encoded, str) or len(encoded) > FRAME_BASE64_LIMIT:
                    raise ValueError
                png = base64.b64decode(encoded, validate=True)
                if not png.startswith(b'\x89PNG\r\n\x1a\n'):
                    raise ValueError
                return web.Response(body=png, content_type='image/png', headers={'Cache-Control': 'no-store'})
            return web.json_response({'state': state, 'qr_available': available}, headers={'Cache-Control': 'no-store'})
        except Exception:
            if action == 'qr':
                raise HubError('browser_unavailable', '二维码尚未就绪。', 503) from None
            return web.json_response({'state': 'browser_unavailable', 'qr_available': False}, headers={'Cache-Control': 'no-store'})

    async def topic_categories_refresh(request):
        require(request, admin=True)
        if await read_json(request) != {}:
            raise HubError('invalid_parameters', '请求体必须为空对象。')
        result = await hub.topic_request({'action': 'tags'})
        return reply({'status': 'ok' if result.get('ok') else result.get('code', 'browser_unavailable'),
                      'item_count': result.get('item_count') or 0, 'live_request': bool(result.get('requested'))})

    async def topic_resume(request):
        require(request, admin=True)
        s.set('topic_paused', None)
        return reply({'ok': True, 'note': '已恢复话题采集。不会立刻补抓，仍受 15 秒间隔和每日 30 次约束。'})

    async def hot_resume(request):
        require(request, admin=True)
        body = await read_json(request) if request.can_read_body else {}
        if not isinstance(body, dict) or set(body) - {'platform', 'scope'} or set(request.query) - {'platform', 'scope'}:
            raise HubError('invalid_parameters', '恢复范围无效。')
        scopes = list(body.values()) + list(request.query.values())
        if any(value not in ('baidu', 'douyin', 'hotlist') for value in scopes) or ('baidu' in scopes and any(value != 'baidu' for value in scopes)):
            raise HubError('invalid_parameters', '恢复范围无效。')
        s.set('baidu_hot_paused' if 'baidu' in scopes else 'hot_paused', None)
        return reply({'ok': True, 'note': '已恢复热榜调度。不会立即补抓，仍受间隔和每日成功上限约束。'})

    async def schema(request):
        require(request)
        return reply(openapi())

    app.router.add_get('/', index)
    app.router.add_get('/api/v1/login/sms', sms_login)
    app.router.add_get('/api/v1/login/sms/{action:frame|qr}', sms_login)
    app.router.add_post('/api/v1/login/sms/{action:start|code|resend|pointer|text|cancel}', sms_login)
    app.router.add_get('/api/v1/login/qr', qr_login)
    app.router.add_get('/api/v1/login/qr/{action:image}', qr_login)
    app.router.add_post('/api/v1/login/qr/{action:start|cancel}', qr_login)
    for p in registry.descriptions():
        adapter = registry.get(p['id'])
        mount_routes = getattr(adapter, 'mount_routes', None)
        if p['available'] and mount_routes:
            mount_routes(app, hub, require, reply)
    app.router.add_get('/api/v1/platforms', platforms)
    app.router.add_get('/api/v1/status', status)
    app.router.add_post('/api/v1/queries', submit)
    app.router.add_get('/api/v1/jobs', jobs)
    app.router.add_get('/api/v1/jobs/{job_id}', job)
    app.router.add_get('/api/v1/series', series)
    app.router.add_get('/api/v1/export.csv', export)
    app.router.add_get('/api/v1/history', history)
    app.router.add_get('/api/v1/insights', insights)
    app.router.add_get('/api/v1/insights/ranges', insight_ranges)
    app.router.add_post('/api/v1/insights/queries', submit)
    app.router.add_route('GET', '/api/v1/keys', keys)
    app.router.add_route('POST', '/api/v1/keys', keys)
    app.router.add_delete('/api/v1/keys/{key_id}', revoke)
    app.router.add_post('/api/v1/collection/resume', resume)
    app.router.add_get('/api/v1/hot/latest', hot_latest)
    app.router.add_get('/api/v1/hot/word', hot_word)
    app.router.add_get('/api/v1/hot/rising', hot_rising)
    app.router.add_get('/api/v1/groups', groups)
    app.router.add_post('/api/v1/groups', groups)
    app.router.add_put('/api/v1/groups/{id}', group_update)
    app.router.add_delete('/api/v1/groups/{id}', group_delete)
    app.router.add_get('/api/v1/groups/{id}/score', group_score_view)
    app.router.add_get('/api/v1/topics/login', topic_login)
    app.router.add_get('/api/v1/topics/login/{action:qr}', topic_login)
    app.router.add_post('/api/v1/topics/login/{action:refresh}', topic_login)
    app.router.add_post('/api/v1/topics/categories/refresh', topic_categories_refresh)
    app.router.add_get('/api/v1/topics', topics)
    app.router.add_post('/api/v1/topics/collect-once', topic_once)
    app.router.add_post('/api/v1/topics/resume', topic_resume)
    app.router.add_post('/api/v1/hot/resume', hot_resume)
    app.router.add_get('/api/v1/openapi.json', schema)
    app.on_startup.append(hub.start)
    app.on_cleanup.append(hub.close)
