"""Baidu protocol, validation and browser bridge adapter."""
import datetime as dt
import json
import re
from typing import Protocol
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
ORIGIN = 'https://index.baidu.com'
DIMENSIONS = dict(platform='baidu', metric='search_index', device='all', region='全国',
                  granularity='day', source='index.baidu.com')
ERRORS = {
    'login_required': '百度未登录，请登录。',
    'rate_limited': '百度限流，请冷却后人工恢复。',
    'bad_request': '百度拒绝请求参数。',
    'abnormal_access': '百度报告异常访问。',
    'signature_unavailable': '百度签名不可用。',
    'authentication_required': '百度未登录，请通过远程桌面在官方页面登录。',
    'access_denied': '百度请求受限，已停止，请检查官方页面。',
    'cipher_text_required': '百度要求 Cipher-Text，已停止，需校准官方浏览器请求。',
    'verification_required': '百度返回验证页面，已停止，请由用户处理。',
    'response_schema': '百度响应未通过严格校验，已停止。',
    'http_error': '百度 HTTP 请求失败，已停止。',
    'business_error': '百度业务请求失败，已停止。',
    'network_error': '百度浏览器取数失败，已停止。',
}


class BaiduError(ValueError):
    def __init__(self, code='response_schema'):
        self.code = code
        super().__init__(ERRORS[code])


class BrowserTransport(Protocol):
    def get(self, url: str) -> tuple[int, str, str]:
        """Same official browser context; (HTTP status, content type, text).

        One GET only, TLS verified, redirects/retries forbidden, bounded response
        (1 MiB) and 25s total deadline per step. Official JS supplies Cipher-Text.
        No cookie/header arguments, persistence, logging, or external HTTP client.
        P0 deliberately has no live implementation of this contract.
        """
        ...


def validate_query(body, *, historical=False):
    from hub_store import HubError, _validate_douyin_query
    if type(body) is not dict or body.get('platform') != 'baidu':
        raise HubError('invalid_query', '请选择百度。')
    q = _validate_douyin_query({**body, 'platform': 'douyin'}, historical=historical)
    return {**q, 'platform': 'baidu'}


def chunks(query):
    start, end = (dt.date.fromisoformat(query[k]) for k in ('start_date', 'end_date'))
    while start <= end:
        last = min(end, start + dt.timedelta(days=364))
        yield {**query, 'start_date': start.isoformat(), 'end_date': last.isoformat()}
        start = last + dt.timedelta(days=1)


def index_url(query):
    q = validate_query(query)
    return ORIGIN + '/api/SearchApi/index?' + urlencode(dict(
        word=json.dumps([[{'name': word, 'wordType': 1}] for word in q['keywords']], ensure_ascii=False, separators=(',', ':')),
        area=0, startDate=q['start_date'], endDate=q['end_date']))


def ptbk_url(uniqid):
    if not isinstance(uniqid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', uniqid):
        raise BaiduError()
    return ORIGIN + '/Interface/api/ptbk?' + urlencode({'uniqid': uniqid})


def _json(text):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError
            obj[key] = value
        return obj
    try:
        return json.loads(text, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError):
        raise BaiduError() from None


def response_data(response):
    try:
        status, content_type, text = response
        if not isinstance(text, str) or len(text.encode('utf-8')) > 1024 * 1024:
            raise BaiduError()
        if 'html' in content_type.lower() or text.lstrip().startswith('<'):
            raise BaiduError('verification_required')
        if type(status) is not int or status != 200:
            raise BaiduError('http_error')
        obj = _json(text)
        if type(obj) is not dict or type(obj.get('status')) is not int:
            raise BaiduError()
        code = obj['status']
        if code:
            raise BaiduError({10000: 'login_required', 10001: 'rate_limited',
                              10002: 'bad_request', 10018: 'abnormal_access'}.get(code, 'business_error'))
        return obj['data']
    except (KeyError, TypeError, AttributeError, UnicodeError):
        raise BaiduError() from None


def decrypt(ptbk, encrypted):
    if (not isinstance(ptbk, str) or not ptbk or len(ptbk) % 2 or len(ptbk) > 256
            or not isinstance(encrypted, str) or len(encrypted) > 6200):
        raise BaiduError()
    half = len(ptbk) // 2
    left, right = ptbk[:half], ptbk[half:]
    if len(set(left)) != half or len(set(right)) != half or set(encrypted) - set(left):
        raise BaiduError()
    return encrypted.translate(str.maketrans(left, right))


def make_result(query, rows, *, fetched_at=None):
    return {'rows': rows, 'metadata': {**DIMENSIONS, **query,
        'fetched_at': fetched_at or dt.datetime.now(TZ).isoformat(),
        'complete': all(r['value_status'] == 'available' for r in rows)}}


def parse_index(data, ptbk, query):
    try:
        days = (dt.date.fromisoformat(query['end_date']) - dt.date.fromisoformat(query['start_date'])).days + 1
        items = data['userIndexes']
        if type(items) is not list or len(items) > len(query['keywords']):
            raise BaiduError()
        by_word = {}
        for item in items:
            word = _json(item['word']) if isinstance(item['word'], str) else item['word']
            if type(word) is not list or len(word) != 1 or type(word[0]) is not dict:
                raise BaiduError()
            name = word[0].get('name')
            if word != [{'name': name, 'wordType': 1}] or name not in query['keywords'] or name in by_word:
                raise BaiduError()
            by_word[name] = item
        rows = []
        start = dt.date.fromisoformat(query['start_date'])
        for word in query['keywords']:
            for device in ('all', 'pc', 'wise'):
                item = by_word.get(word)
                if item is None:
                    tokens = [''] * days
                else:
                    series = item[device]
                    if (series['startDate'] != query['start_date'] or series['endDate'] != query['end_date']
                            or series.get('type', 'day') != 'day'):
                        raise BaiduError()
                    plain = decrypt(ptbk, series['data'])
                    tokens = plain.split(',')
                    if len(tokens) != days:
                        raise BaiduError()
                for i, token in enumerate(tokens):
                    if token and not re.fullmatch(r'[0-9]{1,15}', token):
                        raise BaiduError()
                    rows.append(dict(keyword=word, device=device, date=(start+dt.timedelta(days=i)).isoformat(),
                                     search_index=int(token) if token else None,
                                     value_status='available' if token else 'missing'))
        return make_result(query, rows)
    except (KeyError, TypeError, ValueError):
        raise BaiduError() from None


def page_absence(query, state):
    if state not in ('unindexed', 'low_volume'):
        raise BaiduError()
    q = validate_query(query)
    rows = parse_index({'userIndexes': []}, '', q)['rows']
    for row in rows:
        row['value_status'] = state
    return make_result(q, rows)


def collect_page(query, browser, *, chunk_interval=8, sleep=None):
    import time
    q = validate_query(query)
    rows = []
    for i, chunk in enumerate(chunks(q)):
        if i:
            (sleep or time.sleep)(chunk_interval)
        rows.extend(browser.query(chunk))
    result = make_result(q, rows)
    public_result(result)
    return result


def collect(query, browser: BrowserTransport, *, chunk_interval=8, sleep=None):
    import time
    q = validate_query(query)
    rows = []
    try:
        for i, chunk in enumerate(chunks(q)):
            if i:
                (sleep or time.sleep)(chunk_interval)
            data = response_data(browser.get(index_url(chunk)))
            ptbk = response_data(browser.get(ptbk_url(data['uniqid'])))
            rows.extend(parse_index(data, ptbk, chunk)['rows'])
        return make_result(q, rows)
    except BaiduError:
        raise
    except Exception:
        raise BaiduError('network_error') from None


def public_result(result):
    from hub_store import HubError
    try:
        m = result['metadata']
        q = validate_query({k: m[k] for k in ('platform', 'keywords', 'start_date', 'end_date')}, historical=True)
        if any(m.get(k) != v for k, v in DIMENSIONS.items()):
            raise ValueError
        stamp = dt.datetime.fromisoformat(m['fetched_at'])
        if stamp.tzinfo is None or stamp > dt.datetime.now(TZ)+dt.timedelta(seconds=1):
            raise ValueError
        start = dt.date.fromisoformat(q['start_date'])
        days = (dt.date.fromisoformat(q['end_date']) - start).days + 1
        expected = {(word, device, (start+dt.timedelta(days=i)).isoformat())
                    for word in q['keywords'] for device in ('all', 'pc', 'wise') for i in range(days)}
        clean = []
        for r in result['rows']:
            key = (r['keyword'], r['device'], r['date'])
            if key not in expected:
                raise ValueError
            expected.remove(key)
            value, state = r['search_index'], r['value_status']
            if state not in ('available', 'missing', 'unindexed', 'low_volume') or (state == 'available' and (type(value) is not int or not 0 <= value < 10**15)) or (state != 'available' and value is not None):
                raise ValueError
            clean.append(dict(keyword=key[0], device=key[1], date=key[2], search_index=value, value_status=state))
        if expected or type(m.get('complete')) is not bool or m['complete'] != all(r['value_status'] == 'available' for r in clean):
            raise ValueError
        return {'rows': clean, 'metadata': {**DIMENSIONS, **q, 'fetched_at': stamp.isoformat(),
            'complete': m['complete'], 'verification': 'pending', 'metric_name': '整体搜索指数', 'unit': '指数',
            'timezone': 'Asia/Shanghai', 'note': '指数不等于搜索次数。',
            'missing_search_dates': {w: [r['date'] for r in clean if r['keyword'] == w and r['device'] == 'all' and r['search_index'] is None] for w in q['keywords']}}}
    except (KeyError, TypeError, ValueError, OverflowError):
        raise HubError('invalid_result', '百度结果未通过严格校验。', 502) from None


def bridge_command(command, payload=None):
    import baidu_browser
    if command == 'baidu_status':
        instance = baidu_browser.browser()
        if not instance.started or instance.busy:
            return {'ok': True, 'state': instance.last_state, 'live_enabled': True}
        result = instance.call(baidu_browser.status, touch=False)
        instance.last_state = result['state']
        return result
    if command == 'baidu_login_open':
        return baidu_browser.browser().call(baidu_browser.login_open)
    query = validate_query(payload)
    instance = baidu_browser.browser()
    if not instance.started and instance.last_state != 'browser_ready':
        return {'ok': False, 'code': 'login_required'}
    import hub_baidu
    try:
        def run(context):
            state = baidu_browser.status(context)['state']
            instance.last_state = state
            if state != 'browser_ready':
                raise BaiduError(state)
            page = baidu_browser._page(context)
            result = collect_page(query, baidu_browser.PageTransport(page), chunk_interval=hub_baidu.limits()['chunk_interval_seconds'])
            return {'ok': True, **result}
        return instance.call(run)
    except BaiduError as exc:
        return {'ok': False, 'code': exc.code}


class BaiduAdapter:
    description = {'id': 'baidu', 'name': '百度指数', 'available': True, 'live_enabled': True,
                   'metric': 'search_index', 'metric_name': '整体搜索指数', 'unit': '指数',
                   'region': '全国', 'timezone': 'Asia/Shanghai', 'modules': [],
                   'description': '全国百度搜索指数；指数不等于搜索次数。'}

    bridge_commands = ('baidu_status', 'baidu_query', 'baidu_login_open')
    dispatch_bridge = staticmethod(bridge_command)

    def initialize(self, store):
        store.db.execute('''CREATE TABLE IF NOT EXISTS baidu_points(
            platform TEXT,metric TEXT,device TEXT,region TEXT,granularity TEXT,source TEXT,
            keyword TEXT,day TEXT,value TEXT,value_status TEXT,fetched REAL,dataset TEXT,
            PRIMARY KEY(platform,metric,device,region,granularity,source,keyword,day))''')
        store.db.commit()

    validate_query = staticmethod(validate_query)
    project = staticmethod(lambda result, verification='pending': public_result(result))

    def validate_insight(self, body, **kwargs):
        from hub_store import HubError
        raise HubError('platform_unavailable', '百度未接入关联词或画像。', 422)

    def ingest(self, store, result, dataset_id=None):
        import time
        import uuid
        from hub_store import encode, query_hash
        result = public_result(result)
        m = result['metadata']
        q = {k: m[k] for k in ('platform', 'keywords', 'start_date', 'end_date')}
        stamp = dt.datetime.fromisoformat(m['fetched_at']).timestamp()
        identifier = dataset_id or uuid.uuid4().hex
        for old in store.db.execute('SELECT id,result FROM datasets WHERE qhash=? AND fetched=?', (query_hash(q), stamp)):
            if store.observation_key(json.loads(old['result'])) == store.observation_key(result):
                return old['id']
        with store.db:
            store.db.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?)', (identifier, query_hash(q), encode(q), stamp, encode(result), time.time()))
            for row in result['rows']:
                store.db.execute('''INSERT INTO baidu_points VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(platform,metric,device,region,granularity,source,keyword,day)
                    DO UPDATE SET value=excluded.value,value_status=excluded.value_status,fetched=excluded.fetched,dataset=excluded.dataset
                    WHERE excluded.fetched>=baidu_points.fetched''',
                    ('baidu', 'search_index', row['device'], '全国', 'day', 'index.baidu.com', row['keyword'], row['date'], encode(row['search_index']), row['value_status'], stamp, identifier))
        return identifier

    def series(self, store, keyword, start=None, end=None):
        from hub_store import HubError
        if not isinstance(keyword, str) or not 1 <= len(keyword) <= 100:
            raise HubError('invalid_keyword', '请指定关键词。')
        try:
            if bool(start) != bool(end):
                raise ValueError
            if start:
                a, b = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
                if a.isoformat() != start or b.isoformat() != end or not 0 <= (b-a).days <= 730:
                    raise ValueError
        except (ValueError, TypeError):
            raise HubError('invalid_dates', '最多查询连续 731 天的已存储数据。') from None
        sql = "SELECT * FROM baidu_points WHERE keyword=? AND device='all'"
        args = [keyword]
        if start:
            sql += ' AND day BETWEEN ? AND ?'
            args += [start, end]
        rows = list(store.db.execute(sql + ' ORDER BY day DESC LIMIT 731', args))[::-1]
        points = [{'date': r['day'], 'value': json.loads(r['value']), 'composite_index': None,
                   'value_status': r['value_status'], 'fetched_at': dt.datetime.fromtimestamp(r['fetched'], TZ).isoformat(),
                   'dataset_id': r['dataset']} for r in rows]
        if start:
            by_day = {r['date']: r for r in points}
            points = [by_day.get((a+dt.timedelta(days=i)).isoformat(), {'date': (a+dt.timedelta(days=i)).isoformat(),
                      'value': None, 'composite_index': None, 'value_status': 'missing', 'fetched_at': None, 'dataset_id': None})
                      for i in range((b-a).days+1)]
        return {'platform': 'baidu', 'metric': 'search_index', 'metric_name': '整体搜索指数', 'keyword': keyword,
                'region': '全国', 'device': 'all', 'granularity': 'day', 'unit': '指数', 'points': points,
                'source': 'index.baidu.com', 'live_request': False, 'note': '仅返回已保存数据。'}

    def csv_rows(self, data):
        from storage import _csv_keyword
        yield ['platform', 'metric', 'region', 'keyword', 'date', 'search_index', 'composite_index', 'fetched_at', 'device', 'granularity', 'source', 'value_status']
        for p in data['points']:
            yield ['baidu', 'search_index', '全国', _csv_keyword(data['keyword']), p['date'],
                   '' if p['value'] is None else p['value'], '', p['fetched_at'], 'all', 'day', 'index.baidu.com', p['value_status']]

    def status_counts(self, store):
        words = {r[0] for r in store.db.execute('SELECT DISTINCT keyword FROM baidu_points')}
        return words, store.db.execute('SELECT count(*) FROM baidu_points').fetchone()[0]

    def repoint_dataset(self, store, keep, old):
        store.db.execute('UPDATE baidu_points SET dataset=? WHERE dataset=?', (keep, old))

    def interrupted(self, store):
        import time
        store.set('baidu.paused', {'code': 'interrupted', 'since': time.time()})

    def paused(self, store):
        return store.get('baidu.paused')

    def status(self, store):
        import hub_baidu
        return hub_baidu.status_snapshot(store)

    def mount_routes(self, app, hub, require, reply):
        import hub_baidu
        hub_baidu.mount_routes(app, hub, require, reply)

    async def async_status(self, hub):
        import hub_baidu
        return {**hub_baidu.status_snapshot(hub.store), 'browser': await hub_baidu.browser_status(hub)}

    async def resume(self, hub):
        import hub_baidu
        return await hub_baidu.resume(hub)

    async def run_one(self, hub, row):
        import hub_baidu
        return await hub_baidu.run_one(hub, row, json.loads(row['query']))

    def bridge_command(self, query):
        return 'baidu_query'

    def collect(self, api, query):
        return bridge_command('baidu_query', query)

    def collect_insight(self, api, query):
        return self.validate_insight(query)
