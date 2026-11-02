"""SQLite data/tasks/API-key store. Never stores browser credentials."""
import datetime as dt
import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

from hub_logging import redact

TZ = ZoneInfo('Asia/Shanghai')
PLATFORMS = [
    {'id': 'douyin', 'name': '抖音指数', 'available': True, 'metric': 'search_index',
     'metric_name': '搜索指数', 'unit': '指数', 'region': '全国', 'timezone': 'Asia/Shanghai',
     'description': '创作者中心关键词搜索指数；不代表实际搜索次数。'},
    {'id': 'baidu', 'name': '百度指数', 'available': False, 'metric': None,
     'description': '待接入，指标及授权方式尚未确定。'},
    {'id': 'google_trends', 'name': 'Google Trends', 'available': False, 'metric': None,
     'description': '待接入；未来按其自身地区、时间窗和相对热度口径展示。'},
]


class HubError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def exc_summary(exc) -> str:
    """Bounded local diagnostic; redact credentials before truncating."""
    message = redact(str(exc))
    message = ' '.join(message.split())[:160]
    errno = f' errno={exc.errno}' if isinstance(exc, OSError) else ''
    return f'{type(exc).__name__}{errno}: {message}'


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def validate_query(body):
    if type(body) is not dict or set(body) - {'platform', 'keywords', 'start_date', 'end_date'}:
        raise HubError('invalid_query', '仅接受 platform、keywords、start_date、end_date。')
    if body.get('platform', 'douyin') != 'douyin':
        raise HubError('platform_unavailable', '该平台尚未接入。', 422)
    words, end_text = body.get('keywords'), body.get('end_date')
    if (type(words) is not list or not 1 <= len(words) <= 5
            or any(not isinstance(w, str) or not 1 <= len(w) <= 100 or w != w.strip()
                   or any(ord(c) < 32 or ord(c) == 127 for c in w) for w in words)
            or len(set(words)) != len(words)):
        raise HubError('invalid_keywords', '请输入 1—5 个唯一关键词，每词 1—100 字。')
    try:
        end = dt.date.fromisoformat(end_text)
        start = dt.date.fromisoformat(body['start_date']) if 'start_date' in body else end - dt.timedelta(days=6)
        today = dt.datetime.now(TZ).date()
        try:
            earliest = today.replace(year=today.year-2)
        except ValueError:
            earliest = today.replace(year=today.year-2, day=28)
        if end.isoformat() != end_text or end >= today or start < earliest:
            raise ValueError
        if not 0 <= (end-start).days < 731 or ('start_date' in body and body['start_date'] != start.isoformat()):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise HubError('invalid_dates', '日期格式 YYYY-MM-DD，仅接受最近两个日历年内的过去日期；截止日以官方页面为准。') from None
    return {'platform': 'douyin', 'keywords': list(words), 'start_date': start.isoformat(),
            'end_date': end.isoformat()}


def query_hash(query):
    return hashlib.sha256(encode(query).encode()).hexdigest()


def public_result(result, *, verification='pending'):
    from storage import _complete_rows, _query
    try:
        m = result['metadata']
        words, days = _query(m['keywords'], m['start_date'], m['end_date'])
        stamp = dt.datetime.fromisoformat(m['fetched_at'])
        if stamp.tzinfo is None or m.get('metric') != 'search_index':
            raise ValueError
        if m.get('platform', 'aweme') != 'aweme' or m.get('region', '全国') != '全国':
            raise ValueError
        complete = _complete_rows(result, words, days)
        if m.get('complete') is True and not complete:
            raise ValueError
        rows, seen = [], set()
        for r in result['rows']:
            if r['keyword'] not in words or r['date'] not in days or (r['keyword'], r['date']) in seen:
                raise ValueError
            seen.add((r['keyword'], r['date']))
            row = {'keyword': r['keyword'], 'date': r['date']}
            for field in ('search_index', 'composite_index'):
                from storage import _numeric
                v = r.get(field)
                if v is not None and not _numeric(v):
                    raise ValueError
                row[field] = v
            rows.append(row)
        return {'rows': rows, 'metadata': {
            'platform': 'douyin', 'metric': 'search_index', 'metric_name': '搜索指数',
            'region': '全国', 'timezone': 'Asia/Shanghai', 'unit': '指数',
            'source': 'creator.douyin.com', 'keywords': words, 'start_date': days[0],
            'end_date': days[-1], 'fetched_at': stamp.isoformat(), 'complete': complete,
            'verification': verification,
            'missing_search_dates': {w: [d for d in days if not any(
                r['keyword'] == w and r['date'] == d and r['search_index'] is not None for r in rows)] for w in words},
            'note': '搜索指数不等于实际搜索次数；综合指数单独列出。'}}
    except (KeyError, TypeError, ValueError, OverflowError):
        raise HubError('invalid_result', '结果未通过数据完整性检查。', 502) from None


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS keys(id TEXT PRIMARY KEY,name TEXT,hash TEXT UNIQUE,
                prefix TEXT,scopes TEXT,created REAL,expires REAL,revoked INTEGER DEFAULT 0,last_used REAL);
            CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY,qhash TEXT,query TEXT,
                fetched REAL,result TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,owner TEXT,idem TEXT,qhash TEXT,
                query TEXT,status TEXT,created REAL,updated REAL,result TEXT,error TEXT,
                cache_hit INTEGER DEFAULT 0,UNIQUE(owner,idem));
            CREATE TABLE IF NOT EXISTS points(platform TEXT,metric TEXT,region TEXT,keyword TEXT,day TEXT,
                value TEXT,composite TEXT,fetched REAL,dataset TEXT,
                PRIMARY KEY(platform,metric,region,keyword,day));
            CREATE TABLE IF NOT EXISTS insights(id TEXT PRIMARY KEY,qhash TEXT,fetched REAL,result TEXT);
            CREATE INDEX IF NOT EXISTS dataset_cache ON datasets(qhash,fetched);
            CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,created);
        ''')
        if 'request_source' not in {r['name'] for r in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute('ALTER TABLE jobs ADD COLUMN request_source TEXT')
        if 'requeues' not in {r['name'] for r in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute('ALTER TABLE jobs ADD COLUMN requeues INTEGER NOT NULL DEFAULT 0')
        self.db.commit()
        path.chmod(0o600)
        self.deduplicate()

    def close(self):
        self.db.close()

    def get(self, k, default=None):
        row = self.db.execute('SELECT v FROM settings WHERE k=?', (k,)).fetchone()
        return json.loads(row['v']) if row else default

    def set(self, k, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (k, encode(value)))

    def issue_key(self, name, scopes, days=30):
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 60
                or any(ord(c) < 32 for c in name) or type(scopes) is not list
                or not scopes or set(scopes) - {'read', 'query'} or 'read' not in scopes
                or type(days) is not int or not 1 <= days <= 365):
            raise HubError('invalid_key', '名称 1—60 字；权限 read 或 read+query；有效期 1—365 天。')
        token = 'idx_' + secrets.token_urlsafe(32)
        now = time.time()
        identifier = uuid.uuid4().hex
        with self.db:
            self.db.execute('INSERT INTO keys(id,name,hash,prefix,scopes,created,expires) VALUES(?,?,?,?,?,?,?)',
                            (identifier, name.strip(), hashlib.sha256(token.encode()).hexdigest(),
                             token[:12], encode(sorted(set(scopes))), now, now + days * 86400))
        return {'id': identifier, 'token': token, 'expires_at': now + days * 86400}

    def key_principal(self, token):
        if not isinstance(token, str) or not token.startswith('idx_') or len(token) > 150:
            return None
        row = self.db.execute('SELECT * FROM keys WHERE hash=? AND revoked=0 AND expires>?',
                              (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        if not row:
            return None
        with self.db:
            self.db.execute('UPDATE keys SET last_used=? WHERE id=?', (time.time(), row['id']))
        return {'id': row['id'], 'scopes': json.loads(row['scopes']), 'admin': False}

    def list_keys(self):
        return [dict(r) | {'scopes': json.loads(r['scopes'])} for r in self.db.execute(
            'SELECT id,name,prefix,scopes,created,expires,revoked,last_used FROM keys ORDER BY created DESC')]

    def revoke_key(self, key):
        with self.db:
            self.db.execute('UPDATE keys SET revoked=1 WHERE id=?', (key,))

    @staticmethod
    def observation_key(result):
        # Verification is an annotation, not a second collection event.
        clean = json.loads(encode(result))
        clean['metadata'].pop('verification', None)
        clean['rows'].sort(key=lambda r: (r['keyword'], r['date']))
        return json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    def deduplicate(self):
        seen = {}
        with self.db:
            for row in self.db.execute('SELECT id,result FROM datasets ORDER BY created,id').fetchall():
                result = json.loads(row['result'])
                key = self.observation_key(result)
                if key not in seen:
                    seen[key] = row['id']
                    continue
                keep = seen[key]
                if result['metadata'].get('verification') == 'user_reported_match':
                    self.db.execute('UPDATE datasets SET result=? WHERE id=?', (encode(result), keep))
                self.db.execute('UPDATE points SET dataset=? WHERE dataset=?', (keep, row['id']))
                self.db.execute('DELETE FROM datasets WHERE id=?', (row['id'],))

    def ingest(self, result, dataset_id=None):
        m = result['metadata']
        query = {'platform': 'douyin', 'keywords': m['keywords'], 'start_date': m['start_date'], 'end_date': m['end_date']}
        stamp = dt.datetime.fromisoformat(m['fetched_at']).timestamp()
        identifier = dataset_id or uuid.uuid4().hex
        for existing in self.db.execute('SELECT id,result FROM datasets WHERE qhash=? AND fetched=?',
                                        (query_hash(query), stamp)):
            old = json.loads(existing['result'])
            if self.observation_key(old) == self.observation_key(result):
                if m.get('verification') == 'user_reported_match' and old['metadata'].get('verification') != 'user_reported_match':
                    with self.db:
                        self.db.execute('UPDATE datasets SET result=? WHERE id=?', (encode(result), existing['id']))
                return existing['id']
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO datasets VALUES(?,?,?,?,?,?)',
                            (identifier, query_hash(query), encode(query), stamp, encode(result), time.time()))
            # Explicit nulls remain null; never convert missing points into zero.
            for row in result['rows']:
                self.db.execute('''INSERT INTO points VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(platform,metric,region,keyword,day) DO UPDATE SET value=excluded.value,
                    composite=excluded.composite,fetched=excluded.fetched,dataset=excluded.dataset
                    WHERE excluded.fetched>=points.fetched''',
                    ('douyin', 'search_index', '全国', row['keyword'], row['date'], encode(row['search_index']),
                     encode(row.get('composite_index')), stamp, identifier))
        return identifier

    def import_results(self, root):
        imported = 0
        for file in sorted(Path(root).glob('*/result.json')):
            try:
                if file.is_symlink() or file.stat().st_size > 10 * 1024 * 1024:
                    continue
                identifier = 'legacy_' + hashlib.sha256(file.parent.name.encode()).hexdigest()[:32]
                if self.db.execute('SELECT 1 FROM datasets WHERE id=?', (identifier,)).fetchone():
                    continue
                confirmation = file.parent / 'user-confirmation.json'
                verification = 'pending'
                if confirmation.is_file() and not confirmation.is_symlink() and confirmation.stat().st_size < 16384:
                    c = json.loads(confirmation.read_text())
                    if c.get('evidence_level') == 'user_attestation' and c.get('user_reported_all_daily_values_match') is True:
                        verification = 'user_reported_match'
                result = public_result(json.loads(file.read_text()), verification=verification)
                self.ingest(result, identifier)
                imported += 1
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return imported

    def save_insight(self, query, result):
        stamp=dt.datetime.fromisoformat(result['metadata']['fetched_at']).timestamp()
        with self.db:
            self.db.execute('INSERT INTO insights VALUES(?,?,?,?)', (uuid.uuid4().hex,query_hash(query),stamp,encode(result)))

    def insight(self, query, max_age=None):
        row=self.db.execute('SELECT result,fetched FROM insights WHERE qhash=? ORDER BY fetched DESC LIMIT 1', (query_hash(query),)).fetchone()
        if not row or (max_age is not None and time.time()-row['fetched']>max_age):return None
        return json.loads(row['result'])

    def latest_real_insight(self, query):
        for row in self.db.execute('SELECT result FROM insights WHERE qhash=? ORDER BY fetched DESC', (query_hash(query),)):
            result = json.loads(row['result'])
            if not result.get('no_data'):
                return result
        return None

    def insight_ranges(self, keyword, kind):
        """Read all saved ranges and latest task metadata; never return raw results."""
        ranges, attempts = {}, {}
        def matches(meta):
            return (meta.get('platform') == 'douyin' and meta.get('kind') == kind
                    and meta.get('keywords') == [keyword])
        for row in self.db.execute('SELECT result FROM insights ORDER BY fetched DESC, id DESC'):
            result = json.loads(row['result'])
            meta = result['metadata']
            if not matches(meta):
                continue
            key = (meta['start_date'], meta['end_date'])
            status = 'no_data' if result.get('no_data') else 'available'
            # Match GET /insights: a later empty answer does not hide saved data.
            if key not in ranges or (ranges[key]['status'] == 'no_data' and status == 'available'):
                ranges[key] = dict(start_date=key[0], end_date=key[1], status=status)
        for row in self.db.execute('SELECT query,status,error,created,updated FROM jobs ORDER BY created DESC, id DESC'):
            query = json.loads(row['query'])
            if not matches(query):
                continue
            key = (query['start_date'], query['end_date'])
            if key not in attempts:
                error = json.loads(row['error']) if row['error'] else {}
                attempts[key] = dict(start_date=key[0], end_date=key[1], status=row['status'],
                                     error_code=error.get('code'), updated_at=row['updated'])
        return {'ranges': sorted(ranges.values(), key=lambda r: (r['end_date'], r['start_date']), reverse=True),
                'attempts': list(attempts.values()), 'live_request': False}

    def cached(self, query):
        if query.get('kind'):
            return self.insight(query, 86400)

        for row in self.db.execute('SELECT result FROM datasets WHERE qhash=? AND fetched BETWEEN ? AND ? ORDER BY fetched DESC',
                                   (query_hash(query), time.time() - 86400, time.time() + 1)):
            result = json.loads(row['result'])
            if result['metadata']['complete']:
                return result
        return None

    def trend_no_data(self, keyword, within_seconds=86400):
        latest = None
        for row in self.db.execute(
                "SELECT query,status,result FROM jobs WHERE updated>=? "
                "AND status IN ('no_data','partial','succeeded') ORDER BY updated DESC,rowid DESC",
                (time.time() - within_seconds,)):
            query = json.loads(row['query'])
            if not query.get('kind') and keyword in query.get('keywords', []):
                latest = row
                break
        if latest is None:
            return False
        result = json.loads(latest['result']) if latest['result'] else {}
        rows = [r for r in result.get('rows', []) if r.get('keyword') == keyword]
        if any(r.get('search_index') is not None for r in rows):
            return False
        if not rows and latest['status'] != 'no_data':
            return False
        # Historical nonempty observations prevent classifying a known term as empty.
        for row in self.db.execute('SELECT value FROM points WHERE keyword=? AND metric=?',
                                   (keyword, 'search_index')):
            if json.loads(row['value']) is not None:
                return False
        for row in self.db.execute('SELECT result FROM datasets'):
            if any(r.get('keyword') == keyword and r.get('search_index') is not None
                   for r in json.loads(row['result']).get('rows', [])):
                return False
        return True

    def job(self, identifier):
        row = self.db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise HubError('job_not_found', '任务不存在。', 404)
        return self._job(row)

    @staticmethod
    def _job(row):
        return {'id': row['id'], 'query': json.loads(row['query']), 'status': row['status'],
                'created_at': row['created'], 'updated_at': row['updated'],
                'cache_hit': bool(row['cache_hit']), 'requeues': row['requeues'],
                'request_source': json.loads(row['request_source']) if row['request_source'] else None,
                'result': json.loads(row['result']) if row['result'] else None,
                'error': json.loads(row['error']) if row['error'] else None}

    def submit(self, query, owner, idem, request_source=None):
        if not isinstance(idem, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', idem):
            raise HubError('idempotency_required', '请提供 1—100 位字母数字组成的 Idempotency-Key 请求头。')
        existing = self.db.execute('SELECT * FROM jobs WHERE owner=? AND idem=?', (owner, idem)).fetchone()
        if existing:
            if existing['qhash'] != query_hash(query):
                raise HubError('idempotency_conflict', '该幂等键已用于其他查询。', 409)
            return self._job(existing), False
        if self.db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 20:
            raise HubError('queue_full', '任务队列已满，请稍后再试。', 429)
        cache = self.cached(query)
        if not cache and self.get('paused'):
            raise HubError('collection_paused', '采集已暂停，请在网页处理登录或平台验证后手动恢复。', 409)
        now, identifier = time.time(), uuid.uuid4().hex
        with self.db:
            self.db.execute('INSERT INTO jobs(id,owner,idem,qhash,query,status,created,updated,result,error,cache_hit,request_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (identifier, owner, idem, query_hash(query), encode(query), ('no_data' if cache.get('no_data') else 'succeeded') if cache else 'queued',
                 now, now, encode(cache) if cache else None, encode({'code': 'no_data', 'message': '平台明确返回无数据，不代表指数为零。'}) if cache and cache.get('no_data') else None, int(bool(cache)), encode(request_source) if request_source else None))
        return self.job(identifier), True

    def update_job(self, identifier, status, *, result=None, error=None):
        with self.db:
            self.db.execute('UPDATE jobs SET status=?,updated=?,result=?,error=? WHERE id=?',
                (status, time.time(), encode(result) if result is not None else None,
                 encode(error) if error else None, identifier))

    def refund_attempt(self, attempt_day):
        # Caller owns the transaction; never commit a partial requeue.
        counter = self.get('daily')
        if dt.datetime.now(TZ).date().isoformat() == attempt_day and counter and counter['date'] == attempt_day:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                            ('daily', encode({'date': attempt_day, 'count': max(0, counter['count'] - 1)})))

    def requeue_job(self, job_id, attempt_day, limit=None):
        with self.db:
            count = self.db.execute('SELECT requeues FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
            exhausted = limit is not None and count >= limit
            error = {'code': 'busy_requeue_exhausted', 'message': '浏览器长时间被占用，已放回队列多次仍未执行，未自动重试'} if exhausted else None
            self.db.execute('UPDATE jobs SET status=?,updated=?,result=NULL,error=?,requeues=requeues+1 WHERE id=?',
                            ('failed' if exhausted else 'queued', time.time(), encode(error) if error else None, job_id))
            self.refund_attempt(attempt_day)
        return exhausted

    def interrupt_pending(self):
        with self.db:
            requeued = self.db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0]
            interrupted = self.db.execute("UPDATE jobs SET status='failed',updated=?,error=? WHERE status='running'",
                (time.time(), encode({'code': 'service_restarted', 'message': '服务重启时任务正在执行，可能已向平台发出请求；未自动重试，请确认后重新提交。', 'recoverable': True}))).rowcount
        return {'requeued': requeued, 'interrupted': interrupted}

    def history(self, limit=30):
        return [dict(row) | {'metadata': json.loads(row['result'])['metadata']}
                for row in self.db.execute('SELECT id,fetched,result FROM datasets ORDER BY fetched DESC LIMIT ?', (limit,))]

    def series(self, platform, keyword, start=None, end=None):
        if platform != 'douyin':
            raise HubError('platform_unavailable', '该平台尚未接入。', 422)
        if not isinstance(keyword, str) or not 1 <= len(keyword) <= 100:
            raise HubError('invalid_keyword', '请指定关键词。')
        if bool(start) != bool(end):
            raise HubError('invalid_dates', '起止日期须同时指定。')
        try:
            if start:
                a, b = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
                if a.isoformat() != start or b.isoformat() != end or not 0 <= (b - a).days <= 730:
                    raise ValueError
        except (ValueError, TypeError):
            raise HubError('invalid_dates', '最多查询连续 731 天的已存储数据。') from None
        sql = "SELECT * FROM points WHERE platform=? AND keyword=? AND metric='search_index' AND region='全国'"
        args = [platform, keyword]
        if start:
            sql += ' AND day BETWEEN ? AND ?'
            args += [start, end]
        rows = list(self.db.execute(sql + ' ORDER BY day DESC LIMIT 731', args))[::-1]
        values = [{'date': r['day'], 'value': json.loads(r['value']), 'composite_index': json.loads(r['composite']),
                   'fetched_at': dt.datetime.fromtimestamp(r['fetched'], TZ).isoformat(), 'dataset_id': r['dataset']} for r in rows]
        if start:
            by_day = {p['date']: p for p in values}
            values = [by_day.get((a + dt.timedelta(days=i)).isoformat(),
                      {'date': (a + dt.timedelta(days=i)).isoformat(), 'value': None, 'composite_index': None,
                       'fetched_at': None, 'dataset_id': None}) for i in range((b - a).days + 1)]
        return {'platform': platform, 'metric': 'search_index', 'metric_name': '搜索指数',
                'keyword': keyword, 'region': '全国', 'unit': '指数', 'points': values,
                'source': 'creator.douyin.com', 'live_request': False,
                'note': '仅返回已保存数据；缺失为 null，搜索指数不是实际搜索次数。'}
