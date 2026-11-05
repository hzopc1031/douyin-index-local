"""SQLite data/tasks/API-key store. Never stores browser credentials."""
import datetime as dt
import hashlib
import json
import math
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


def validate_query(body, *, historical=False):
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
        if end.isoformat() != end_text or end >= today or (not historical and start < earliest):
            raise ValueError
        if not 0 <= (end-start).days < 731 or ('start_date' in body and body['start_date'] != start.isoformat()):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        message = ('日期格式 YYYY-MM-DD，须为过去日期，起止有序且最多 731 天。' if historical else
                   '日期格式 YYYY-MM-DD，仅接受最近两个日历年内的过去日期；截止日以官方页面为准。')
        raise HubError('invalid_dates', message) from None
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
            CREATE TABLE IF NOT EXISTS hot_snapshots(
                id TEXT NOT NULL, fetched REAL NOT NULL, board TEXT NOT NULL,
                item_key TEXT NOT NULL, key_kind TEXT NOT NULL, sentence_id TEXT,
                word TEXT NOT NULL, position INTEGER NOT NULL, hot_value TEXT,
                label TEXT, event_time TEXT, video_count TEXT, sentence_tag TEXT,
                word_type TEXT, group_id TEXT, source TEXT NOT NULL,
                PRIMARY KEY(id, item_key));
            CREATE INDEX IF NOT EXISTS dataset_cache ON datasets(qhash,fetched);
            CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,created);
            CREATE INDEX IF NOT EXISTS hot_snapshots_fetch ON hot_snapshots(board, fetched);
            CREATE INDEX IF NOT EXISTS hot_snapshots_sentence ON hot_snapshots(sentence_id, fetched);
            CREATE INDEX IF NOT EXISTS hot_snapshots_word ON hot_snapshots(word, fetched);
            CREATE TABLE IF NOT EXISTS keyword_groups(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, keywords TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS topic_snapshots(
                id TEXT NOT NULL, fetched REAL NOT NULL, rank TEXT NOT NULL,
                window TEXT NOT NULL, category TEXT NOT NULL, item_key TEXT NOT NULL,
                title TEXT NOT NULL, position INTEGER NOT NULL, hot_value TEXT,
                source TEXT NOT NULL, PRIMARY KEY(id, item_key));
            CREATE INDEX IF NOT EXISTS topic_snapshots_fetch
                ON topic_snapshots(rank, window, category, fetched);
        ''')
        topic_columns = {row['name'] for row in self.db.execute('PRAGMA table_info(topic_snapshots)')}
        for name in ('publish_count', 'play_count', 'avg_play', 'cover'):
            if name not in topic_columns:
                self.db.execute(f'ALTER TABLE topic_snapshots ADD COLUMN {name} TEXT')
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

    def key_principal(self, token, *, record_usage=True):
        if not isinstance(token, str) or not token.startswith('idx_') or len(token) > 150:
            return None
        row = self.db.execute('SELECT * FROM keys WHERE hash=? AND revoked=0 AND expires>?',
                              (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        if not row:
            return None
        if record_usage:
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

    def insight(self, query, max_age=None, *, real_only=False):
        # Legacy clamp hashes include requested_end_date; match their metadata without rewriting storage.
        rows = self.db.execute('''SELECT result,fetched FROM insights WHERE qhash=? OR (
            ?='related' AND json_extract(result,'$.metadata.kind')='related'
            AND json_extract(result,'$.metadata.platform')=?
            AND json_extract(result,'$.metadata.keywords')=?
            AND ((json_extract(result,'$.metadata.start_date')=?
                  AND json_extract(result,'$.metadata.end_date')=?) OR
                 (? AND json_extract(result,'$.metadata.requested_end_date')=?
                  AND (json_extract(result,'$.metadata.start_date')=? OR
                       (? > json_extract(result,'$.metadata.end_date') AND
                        julianday(json_extract(result,'$.metadata.start_date'))
                        + julianday(json_extract(result,'$.metadata.requested_end_date'))
                        - julianday(json_extract(result,'$.metadata.end_date'))=julianday(?))))))
            ORDER BY json_extract(result,'$.metadata.end_date')=? DESC, fetched DESC''',
            (query_hash(query), query['kind'], query['platform'], encode(query['keywords']),
             query['start_date'], query['end_date'], max_age is None, query['end_date'],
             query['start_date'], query['start_date'], query['start_date'], query['end_date']))
        for row in rows:
            if max_age is not None and time.time()-row['fetched'] > max_age:
                continue
            result = json.loads(row['result'])
            if not real_only or not result.get('no_data'):
                return result
        return None

    def latest_real_insight(self, query):
        return self.insight(query, real_only=True)

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

    def trend_no_data(self, query, within_seconds=86400):
        """Return the empty trend's provenance only for this exact query range."""
        keyword = query['keywords'][0]
        scope = {key: query[key] for key in ('platform', 'start_date', 'end_date')}
        latest = None
        for row in self.db.execute(
                "SELECT id,query,status,result FROM jobs WHERE updated>=? "
                "AND status IN ('no_data','partial','succeeded') ORDER BY updated DESC,rowid DESC",
                (time.time() - within_seconds,)):
            trend = json.loads(row['query'])
            if (not trend.get('kind') and keyword in trend.get('keywords', [])
                    and all(trend.get(key) == value for key, value in scope.items())):
                latest = row
                break
        if latest is None:
            return None
        result = json.loads(latest['result']) if latest['result'] else {}
        rows = [r for r in result.get('rows', []) if r.get('keyword') == keyword]
        if any(r.get('search_index') is not None for r in rows):
            return None
        if not rows and latest['status'] != 'no_data':
            return None
        # A saved nonempty observation in this range still prevents a skip.
        for row in self.db.execute('SELECT value FROM points WHERE platform=? AND keyword=? '
                                   'AND metric=? AND day BETWEEN ? AND ?',
                                   (scope['platform'], keyword, 'search_index',
                                    scope['start_date'], scope['end_date'])):
            if json.loads(row['value']) is not None:
                return None
        for row in self.db.execute('SELECT result FROM datasets WHERE json_extract(query,\'$.platform\')=?',
                                   (scope['platform'],)):
            if any(r.get('keyword') == keyword and r.get('search_index') is not None
                   and scope['start_date'] <= r.get('date', '') <= scope['end_date']
                   for r in json.loads(row['result']).get('rows', [])):
                return None
        return {'job_id': latest['id'], 'keyword': keyword, **scope}

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

    def latest_available_date(self):
        """Latest locally known usable search day, not a claim about upstream freshness."""
        row = self.db.execute(
            "SELECT MAX(day) FROM points WHERE platform='douyin' AND metric='search_index' "
            "AND region='全国' AND value!='null' AND day<?",
            (dt.datetime.now(TZ).date().isoformat(),)).fetchone()
        return row[0]

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

    def save_hot_snapshot(self, snapshot):
        """Store one complete hot-list observation. Incomplete input is rejected."""
        if not isinstance(snapshot, dict) or snapshot.get('complete') is not True or snapshot.get('board') != 'word_list':
            raise HubError('incomplete_snapshot', '不完整的热榜快照不会入库。')
        items = snapshot.get('items')
        source = snapshot.get('source')
        if not isinstance(items, list) or not items or not isinstance(source, str) or len(source) > 200:
            raise HubError('incomplete_snapshot', '不完整的热榜快照不会入库。')
        try:
            stamp = dt.datetime.fromisoformat(snapshot['fetched_at'])
            if stamp.tzinfo is None:
                raise ValueError
            fetched = stamp.timestamp()
        except (KeyError, TypeError, ValueError, OverflowError):
            raise HubError('incomplete_snapshot', '热榜采集时间无效，本次快照不入库。') from None
        seen_words, seen_positions, seen_keys = set(), set(), set()
        rows = []
        for item in items:
            if not isinstance(item, dict):
                raise HubError('incomplete_snapshot', '不完整的热榜快照不会入库。')
            word, position = item.get('word'), item.get('position')
            key, kind = item.get('item_key'), item.get('key_kind')
            if (not isinstance(word, str) or not word or word in seen_words
                    or type(position) is not int or position < 1 or position in seen_positions
                    or kind not in ('sentence_id', 'word') or not isinstance(key, str) or key in seen_keys):
                raise HubError('incomplete_snapshot', '热榜词或名次无法核验，本次快照不入库。')
            sentence_id = item.get('sentence_id')
            if kind == 'sentence_id' and (not isinstance(sentence_id, str) or not sentence_id or key != 'id:' + sentence_id):
                raise HubError('incomplete_snapshot', '热榜标识无法核验，本次快照不入库。')
            if kind == 'word' and (sentence_id is not None or key != 'word:' + word):
                raise HubError('incomplete_snapshot', '热榜标识无法核验，本次快照不入库。')
            seen_words.add(word)
            seen_positions.add(position)
            seen_keys.add(key)
            heat = item.get('hot_value')
            if heat is not None and (isinstance(heat, bool) or not isinstance(heat, (int, float)) or isinstance(heat, float) and not math.isfinite(heat) or heat < 0):
                raise HubError('incomplete_snapshot', '热榜热度无法核验，本次快照不入库。')
            group_id = item.get('group_id')
            if group_id is not None and (not isinstance(group_id, str) or len(group_id) > 64):
                raise HubError('incomplete_snapshot', '热榜标识无法核验，本次快照不入库。')

            def stored_int(value):
                if value is None:
                    return encode(None)
                if isinstance(value, bool) or type(value) is not int:
                    raise HubError('incomplete_snapshot', '热榜字段无法核验，本次快照不入库。')
                return encode(value)

            rows.append((key, kind, sentence_id, word, position, encode(heat),
                         stored_int(item.get('label')), stored_int(item.get('event_time')),
                         stored_int(item.get('video_count')), stored_int(item.get('sentence_tag')),
                         stored_int(item.get('word_type')), group_id))
        identifier = uuid.uuid4().hex
        with self.db:
            self.db.executemany('''INSERT INTO hot_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                                [(identifier, fetched, 'word_list', *row, source) for row in rows])
        return identifier

    @staticmethod
    def _hot_item(row):
        return {'item_key': row['item_key'], 'key_kind': row['key_kind'], 'sentence_id': row['sentence_id'],
                'word': row['word'], 'position': row['position'], 'hot_value': json.loads(row['hot_value']),
                'label': json.loads(row['label']), 'event_time': json.loads(row['event_time']),
                'video_count': json.loads(row['video_count']), 'sentence_tag': json.loads(row['sentence_tag']),
                'word_type': json.loads(row['word_type']), 'group_id': row['group_id'],
                'fetched_at': dt.datetime.fromtimestamp(row['fetched'], TZ).isoformat()}

    def latest_hot(self):
        row = self.db.execute(
            "SELECT id FROM hot_snapshots WHERE board='word_list' ORDER BY fetched DESC, id DESC LIMIT 1").fetchone()
        if row is None:
            return {'board': 'word_list', 'source': None, 'fetched_at': None, 'items': []}
        rows = self.db.execute("SELECT * FROM hot_snapshots WHERE id=? ORDER BY position", (row['id'],)).fetchall()
        return {'board': 'word_list', 'source': rows[0]['source'],
                'fetched_at': dt.datetime.fromtimestamp(rows[0]['fetched'], TZ).isoformat(),
                'items': [self._hot_item(item) for item in rows]}

    def hot_history(self, *, sentence_id=None, word=None):
        if bool(sentence_id) == bool(word):
            raise HubError('invalid_parameters', '请指定 sentence_id 或精确话题之一。')
        if sentence_id is not None:
            rows = self.db.execute(
                "SELECT * FROM hot_snapshots WHERE sentence_id=? ORDER BY fetched DESC LIMIT 500", (sentence_id,))
        else:
            rows = self.db.execute(
                "SELECT * FROM hot_snapshots WHERE word=? ORDER BY fetched DESC LIMIT 500", (word,))
        points = [self._hot_item(item) for item in rows][::-1]
        return {'sentence_id': sentence_id, 'word': word, 'points': points}

    @staticmethod
    def _group(row):
        return {'id': row['id'], 'name': row['name'], 'keywords': json.loads(row['keywords']),
                'created': row['created']}

    def create_group(self, name, keywords):
        if self.db.execute('SELECT COUNT(*) FROM keyword_groups').fetchone()[0] >= 100:
            raise HubError('group_limit', '关键词组已达 100 个上限。', 429)
        identifier = uuid.uuid4().hex
        with self.db:
            self.db.execute('INSERT INTO keyword_groups VALUES(?,?,?,?)',
                            (identifier, name, encode(keywords), time.time()))
        return self.get_group(identifier)

    def list_groups(self):
        rows = self.db.execute('SELECT * FROM keyword_groups ORDER BY created DESC, id DESC')
        return [self._group(row) for row in rows]

    def get_group(self, identifier):
        row = self.db.execute('SELECT * FROM keyword_groups WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise HubError('group_not_found', '关键词组不存在。', 404)
        return self._group(row)

    def update_group(self, identifier, name, keywords):
        self.get_group(identifier)
        with self.db:
            self.db.execute('UPDATE keyword_groups SET name=?, keywords=? WHERE id=?',
                            (name, encode(keywords), identifier))
        return self.get_group(identifier)

    def delete_group(self, identifier):
        self.get_group(identifier)
        with self.db:
            self.db.execute('DELETE FROM keyword_groups WHERE id=?', (identifier,))

    def save_topic_snapshot(self, snapshot):
        """Store one complete topic board. Incomplete input is rejected.

        No caller in the gateway fetches this yet. The Qt login integration
        must pass only rows observed on the live page.
        """
        from topic_board import TOPIC_RANKS, TOPIC_WINDOW, category_name_ok
        if not isinstance(snapshot, dict) or snapshot.get('complete') is not True:
            raise HubError('incomplete_snapshot', '不完整的话题快照不会入库。')
        rank, window, category = snapshot.get('rank'), snapshot.get('window'), snapshot.get('category')
        items, source = snapshot.get('items'), snapshot.get('source')
        if (rank not in TOPIC_RANKS or window != TOPIC_WINDOW or not category_name_ok(category)
                or not isinstance(items, list) or not items
                or not isinstance(source, str) or not source or len(source) > 200):
            raise HubError('incomplete_snapshot', '不完整的话题快照不会入库。')
        try:
            stamp = dt.datetime.fromisoformat(snapshot['fetched_at'])
            if stamp.tzinfo is None:
                raise ValueError
            fetched = stamp.timestamp()
        except (KeyError, TypeError, ValueError, OverflowError):
            raise HubError('incomplete_snapshot', '话题采集时间无效，本次快照不入库。') from None
        seen_titles, seen_positions = set(), set()
        rows = []
        for item in items:
            if not isinstance(item, dict):
                raise HubError('incomplete_snapshot', '不完整的话题快照不会入库。')
            title, position = item.get('title'), item.get('position')
            if (not isinstance(title, str) or not title or title in seen_titles
                    or any(ord(char) < 32 or ord(char) == 127 for char in title)
                    or type(position) is not int or position < 1 or position in seen_positions):
                raise HubError('incomplete_snapshot', '话题或名次无法核验，本次快照不入库。')
            heat = item.get('hot_value', None)
            if heat is not None and (isinstance(heat, bool) or not isinstance(heat, (int, float))
                                      or isinstance(heat, float) and not math.isfinite(heat) or heat < 0):
                raise HubError('incomplete_snapshot', '话题热度无法核验，本次快照不入库。')

            def optional_number(value):
                if value is None:
                    return encode(None)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise HubError('incomplete_snapshot', '话题计数字段无法核验，本次快照不入库。')
                if isinstance(value, float) and not math.isfinite(value) or value < 0:
                    raise HubError('incomplete_snapshot', '话题计数字段无法核验，本次快照不入库。')
                return encode(value)

            cover = item.get('cover')
            if cover is not None and (not isinstance(cover, str) or not cover.startswith('https://') or len(cover) > 500):
                raise HubError('incomplete_snapshot', '话题封面无法核验，本次快照不入库。')
            seen_titles.add(title)
            seen_positions.add(position)
            key = item.get('item_key') if isinstance(item.get('item_key'), str) else 'title:' + title
            rows.append((key, title, position, encode(heat), optional_number(item.get('publish_count')),
                         optional_number(item.get('play_count')), optional_number(item.get('avg_play')),
                         cover))
        identifier = uuid.uuid4().hex
        with self.db:
            self.db.executemany(
                '''INSERT INTO topic_snapshots
                   (id, fetched, rank, window, category, item_key, title, position, hot_value, source,
                    publish_count, play_count, avg_play, cover)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                [(identifier, fetched, rank, window, category, row[0], row[1], row[2], row[3], source,
                  row[4], row[5], row[6], row[7]) for row in rows])
        return identifier

    def latest_topics(self, *, rank, window, category):
        row = self.db.execute(
            '''SELECT id FROM topic_snapshots
               WHERE rank=? AND window=? AND category=?
               ORDER BY fetched DESC, id DESC LIMIT 1''',
            (rank, window, category)).fetchone()
        if row is None:
            return {'rank': rank, 'window': window, 'category': category,
                    'fetched_at': None, 'source': None, 'items': []}
        rows = self.db.execute(
            'SELECT * FROM topic_snapshots WHERE id=? ORDER BY position', (row['id'],)).fetchall()
        def load_number(raw):
            return None if raw is None else json.loads(raw)

        return {'rank': rank, 'window': window, 'category': category,
                'source': rows[0]['source'],
                'fetched_at': dt.datetime.fromtimestamp(rows[0]['fetched'], TZ).isoformat(),
                'items': [{'title': item['title'], 'position': item['position'],
                           'hot_value': json.loads(item['hot_value']),
                           'publish_count': load_number(item['publish_count']),
                           'play_count': load_number(item['play_count']),
                           'avg_play': load_number(item['avg_play']),
                           'cover': item['cover'],
                           'fetched_at': dt.datetime.fromtimestamp(item['fetched'], TZ).isoformat()}
                          for item in rows]}
