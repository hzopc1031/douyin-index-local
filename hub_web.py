"""Authenticated index API and task worker, mounted by desktop_gateway.py."""
import asyncio
import contextlib
import csv
import datetime as dt
import io
import json
import math
import time
from pathlib import Path

from aiohttp import web
from hub_store import HubError, PLATFORMS, Store, TZ, public_result, validate_query

PRINCIPAL_KEY = web.RequestKey('principal', dict)

SAFE_ERRORS = {
    'authentication_required': '登录已失效，请通过远程桌面扫码后手动恢复采集。',
    'verification_required': '平台要求人工验证，请通过远程桌面处理后手动恢复。',
    'rate_limited': '平台返回限频信号，采集已暂停，未自动重试。',
    'access_denied': '平台拒绝访问，采集已暂停，请检查官方页面。',
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
    'no_data': '平台未返回该关键词在此日期范围的有效指数；可能未收录或该范围无数据，不代表指数为零。',
    'daily_limit': '已达到本服务今日 50 次在线尝试上限，未发出请求。',
    'request_failed': '本次取数未完成，未自动重试。',
}
PAUSING = {'authentication_required', 'verification_required', 'rate_limited', 'access_denied'}


class Hub:
    def __init__(self, root, bridge=None):
        self.root = Path(root)
        self.store = Store(self.root / '.cloud-state' / 'index-hub.sqlite3')
        if bridge is None:
            from hub_bridge import call_bridge
            bridge = call_bridge
        self.bridge = bridge
        self.worker_task = None

    async def browser_status(self):
        try:
            result = await asyncio.to_thread(self.bridge, 'status', None, 2)
            state = result.get('state') if result.get('ok') else 'unavailable'
            return state if state in ('browser_ready', 'login_required') else 'unavailable'
        except Exception:
            return 'unavailable'

    def _requeue_unattempted(self, job_id, attempt_day):
        """Refund only the reserved attempt from the current Shanghai day."""
        self.store.update_job(job_id, 'queued', error=None)
        today = dt.datetime.now(TZ).date().isoformat()
        counter = self.store.get('daily')
        if today == attempt_day and counter and counter['date'] == attempt_day:
            self.store.set('daily', {'date': attempt_day, 'count': max(0, counter['count'] - 1)})

    async def run_one(self):
        """Process at most one job. Attempts are persisted before touching the browser."""
        row = self.store.db.execute("SELECT id,query FROM jobs WHERE status='queued' ORDER BY CASE WHEN json_extract(query,'$.kind') IS NULL THEN 0 ELSE 1 END,created LIMIT 1").fetchone()
        if not row or self.store.get('paused'):
            return False
        query = json.loads(row['query'])
        cached = self.store.cached(query)
        if cached:
            self.store.update_job(row['id'], 'succeeded', result=cached)
            with self.store.db:
                self.store.db.execute('UPDATE jobs SET cache_hit=1 WHERE id=?', (row['id'],))
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
        if state != 'browser_ready':
            code = 'authentication_required' if state == 'login_required' else 'browser_unavailable'
            self.store.update_job(row['id'], 'failed', error={'code': code, 'message': SAFE_ERRORS[code]})
            if code in PAUSING:
                self.store.set('paused', {'code': code, 'since': now})
            return True
        self.store.update_job(row['id'], 'running')
        self.store.set('last_attempt_at', now)
        self.store.set('daily', {'date': today, 'count': counter['count'] + 1})
        try:
            response = await asyncio.to_thread(self.bridge, 'insight' if query.get('kind') else 'query', query, 45)
            if response.get('ok') is not True:
                code = response.get('code', '')
                if code in {'cooldown', 'busy'}:
                    self._requeue_unattempted(row['id'], today)
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
                    result = public_result(response)
                    self.store.ingest(result)
                    has_values=any(r['search_index'] is not None for r in result['rows'])
                    self.store.update_job(row['id'], 'succeeded' if result['metadata']['complete'] else 'partial' if has_values else 'no_data', result=result,
                                          error=None if has_values else {'code':'no_data','message':SAFE_ERRORS['no_data']})
                self.store.set('last_result', {'ok': True, 'at': time.time()})
        except asyncio.CancelledError:
            # Restart handling marks any interrupted job failed; never automatically replay it.
            raise
        except Exception:
            self.store.update_job(row['id'], 'failed', error={'code': 'request_failed', 'message': SAFE_ERRORS['request_failed']})
            self.store.set('last_result', {'ok': False, 'code': 'request_failed', 'at': time.time()})
        return True

    async def worker(self):
        while True:
            try:
                await self.run_one()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.store.set('worker_error', '任务处理暂不可用，请检查服务。')
            await asyncio.sleep(1)

    async def start(self, app):
        self.store.interrupt_pending()
        self.store.import_results(self.root / 'results')
        self.worker_task = asyncio.create_task(self.worker(), name='index-hub-worker')

    async def close(self, app):
        if self.worker_task:
            self.worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker_task
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
        ('/api/v1/insights/queries', 'post', '提交扩展采集任务（试验接入）'),
        ('/api/v1/export.csv', 'get', '导出已存储趋势 CSV'),
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
                'keywords':{'type':'array','minItems':1,'maxItems':1,'items':{'type':'string'}},'kind':{'enum':['related','audience']},'platform':{'enum':['douyin']},'start_date':{'type':'string','format':'date'},'end_date':{'type':'string','format':'date'}}}
        if path == '/api/v1/insights':
            operation['parameters']=[{'in':'query','name':k,'required':k in ('keyword','kind','end_date'),'schema':{'type':'string'}} for k in ('keyword','kind','start_date','end_date')]
        if '{job_id}' in path:
            operation['parameters'] = [{'in': 'path', 'name': 'job_id', 'required': True, 'schema': {'type': 'string'}}]
        if path.endswith(('series', 'export.csv')):
            operation['parameters'] = [{'in': 'query', 'name': k, 'required': k == 'keyword',
                                        'schema': {'type': 'string'}} for k in ('platform', 'keyword', 'start_date', 'end_date')]
        paths[path] = {method: operation}
    return {'openapi': '3.1.0', 'info': {'title': 'Index Atlas 指数工作台 API', 'version': '1.0.0',
             'description': '自用平台聚合接口。当前仅抖音搜索指数可用，1–5词、最近两年；非官方开放 API。搜索指数不是实际搜索次数。'},
            'servers': [{'url': '/'}], 'security': [{'BearerAuth': []}], 'paths': paths,
            'components': {'securitySchemes': {'BearerAuth': {'type': 'http', 'scheme': 'bearer'}},
             'schemas': {'Query': {'type': 'object', 'additionalProperties': False, 'required': ['keywords', 'end_date'],
              'properties': {'platform': {'type': 'string', 'enum': ['douyin'], 'default': 'douyin'},
               'keywords': {'type': 'array', 'minItems': 1, 'maxItems': 5, 'uniqueItems': True,
                            'items': {'type': 'string', 'minLength': 1, 'maxLength': 100}},
               'start_date': {'type': 'string', 'format': 'date', 'description': '不填默认截止日前六天；填写后支持最近两个日历年内的过去日期'},
               'end_date': {'type': 'string', 'format': 'date', 'description': '过去的可用截止日，以官方页面为准'}}}}}}


def mount(app, hub):
    s = hub.store

    async def index(request):
        require(request, admin=True)
        return web.FileResponse(hub.root / 'hub.html')

    async def platforms(request):
        require(request)
        return reply({'platforms': PLATFORMS})

    async def status(request):
        require(request)
        day = dt.datetime.now(TZ).date().isoformat()
        daily = s.get('daily', {'date': day, 'count': 0})
        if daily['date'] != day:
            daily = {'date': day, 'count': 0}
        browser = await hub.browser_status()
        next_attempt = s.get('last_attempt_at', 0) + 15
        remaining = max(0, math.ceil(next_attempt - time.time()))
        return reply({'version': '1.0.0', 'browser': browser, 'paused': s.get('paused'),
                      'last_result': s.get('last_result'), 'worker_error': s.get('worker_error'),
                      'next_online_attempt_at': next_attempt,
                      'cooldown_remaining_seconds': remaining,
                      'daily_online_attempts': daily['count'],
                      'limits': {'keywords': 5, 'days': 731, 'online_interval_seconds': 15,
                                 'online_attempts_per_day': 50, 'cache_hours': 24,
                                 'note': '以上为本服务保护设置，不是抖音官方配额。'},
                      'counts': {'datasets': s.db.execute('SELECT count(*) FROM datasets').fetchone()[0],
                                 'keywords': s.db.execute('SELECT count(DISTINCT keyword) FROM points').fetchone()[0],
                                 'points': s.db.execute('SELECT count(*) FROM points').fetchone()[0]}})

    def job_view(value, request):
        result = dict(value)
        if not request[PRINCIPAL_KEY].get('admin'):
            result.pop('request_source', None)
        return result

    async def submit(request):
        p = require(request, 'query')
        from hub_insights import validate_insight
        query = (validate_insight if request.path.endswith('/insights/queries') else validate_query)(await read_json(request))
        # Transport peer only: do not trust caller-supplied X-Forwarded-For.
        user_agent = ''.join(c for c in request.headers.get('User-Agent', '') if ord(c) >= 32 and ord(c) != 127)[:512]
        source = {'kind': 'admin' if p.get('admin') else 'api_key',
                  'peer_ip': request.remote, 'user_agent': user_agent,
                  'recorded_at': time.time()}
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
        unknown = set(request.query) - {'platform', 'keyword', 'start_date', 'end_date'}
        if unknown:
            raise HubError('invalid_parameters', '包含不支持的查询参数。')
        return s.series(request.query.get('platform', 'douyin'), request.query.get('keyword'),
                        request.query.get('start_date'), request.query.get('end_date'))

    async def series(request):
        require(request)
        return reply(get_series(request))

    async def export(request):
        require(request)
        from storage import _csv_keyword
        data = get_series(request)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['platform', 'metric', 'region', 'keyword', 'date', 'search_index', 'composite_index', 'fetched_at'])
        for point in data['points']:
            writer.writerow(['douyin', 'search_index', '全国', _csv_keyword(data['keyword']), point['date'],
                             '' if point['value'] is None else point['value'],
                             '' if point['composite_index'] is None else point['composite_index'], point['fetched_at']])
        return web.Response(body=('\ufeff' + output.getvalue()).encode(), content_type='text/csv',
                            charset='utf-8', headers={'Content-Disposition': 'attachment; filename="index-series.csv"'})

    async def insights(request):
        require(request)
        from hub_insights import validate_insight
        if set(request.query)-{'platform','keyword','kind','start_date','end_date'}:
            raise HubError('invalid_parameters','包含不支持的参数。')
        body={'platform':request.query.get('platform','douyin'),'keywords':[request.query.get('keyword')],
              'kind':request.query.get('kind'),'end_date':request.query.get('end_date')}
        if request.query.get('start_date'):body['start_date']=request.query['start_date']
        query=validate_insight(body)
        result=s.insight(query)
        return reply({'status':'available' if result else 'not_collected','query':query,'result':result,'live_request':False})

    async def history(request):
        require(request)
        s.import_results(hub.root / 'results')
        return reply({'datasets': [{k: r[k] for k in ('id', 'fetched', 'metadata')} for r in s.history()]})

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
        state = await hub.browser_status()
        if state != 'browser_ready':
            raise HubError('browser_not_ready', '请先通过远程桌面完成登录或验证。', 409)
        s.set('paused', None)
        s.set('worker_error', None)
        return reply({'ok': True, 'note': '已恢复。保留原有冷却间隔与每日上限；失败任务不会重试。'})

    async def schema(request):
        require(request)
        return reply(openapi())

    app.router.add_get('/', index)
    app.router.add_get('/api/v1/platforms', platforms)
    app.router.add_get('/api/v1/status', status)
    app.router.add_post('/api/v1/queries', submit)
    app.router.add_get('/api/v1/jobs', jobs)
    app.router.add_get('/api/v1/jobs/{job_id}', job)
    app.router.add_get('/api/v1/series', series)
    app.router.add_get('/api/v1/export.csv', export)
    app.router.add_get('/api/v1/history', history)
    app.router.add_get('/api/v1/insights', insights)
    app.router.add_post('/api/v1/insights/queries', submit)
    app.router.add_route('GET', '/api/v1/keys', keys)
    app.router.add_route('POST', '/api/v1/keys', keys)
    app.router.add_delete('/api/v1/keys/{key_id}', revoke)
    app.router.add_post('/api/v1/collection/resume', resume)
    app.router.add_get('/api/v1/openapi.json', schema)
    app.on_startup.append(hub.start)
    app.on_cleanup.append(hub.close)
