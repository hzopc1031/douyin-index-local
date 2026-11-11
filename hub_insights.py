"""Bounded projections for official index insight endpoints; no credential output."""
import datetime as dt
import os
import logging
import math
from hub_store import HubError, TZ, exc_summary, validate_query
from platforms import require_platform

KINDS = {'related', 'audience'}

def related_end_lag_days():
    value = os.environ.get('INDEX_HUB_RELATED_END_LAG_DAYS', '3')
    try:
        return int(value) if value.isascii() and value.isdecimal() else 3
    except ValueError:
        return 3


def last_sunday_on_or_before(day):
    """Official related-word windows end on Sunday; other end dates return business 500."""
    return day - dt.timedelta(days=(day.weekday() + 1) % 7)


RELATED_ALIGN_REASON = '关联词接口按周出数，截止日已调整为可查范围内最近的周日'
RELATED_WEEK_FALLBACK_REASON = '关联词该周日尚未出数，已回退到上一周日'
RELATED_SUNDAY_CAP_TTL = 12 * 60 * 60


def _iso_date(value):
    if not isinstance(value, str):
        return None
    try:
        day = dt.date.fromisoformat(value)
    except ValueError:
        return None
    return day if day.isoformat() == value else None


def _cap_is_fresh(checked_at, now=None):
    now = now or dt.datetime.now(TZ)
    if isinstance(checked_at, (int, float)):
        then = dt.datetime.fromtimestamp(checked_at, TZ)
    elif isinstance(checked_at, str):
        try:
            then = dt.datetime.fromisoformat(checked_at)
        except ValueError:
            return False
        then = then.replace(tzinfo=TZ) if then.tzinfo is None else then.astimezone(TZ)
    else:
        return False
    return 0 <= (now - then).total_seconds() <= RELATED_SUNDAY_CAP_TTL


def related_sunday_fallback(natural, cap, now=None):
    """Return a stored earlier Sunday if a recent official 500 forced a one-week fallback."""
    if not isinstance(cap, dict) or cap.get('source') != 'fallback':
        return None
    if cap.get('tried') != natural.isoformat():
        return None
    used = _iso_date(cap.get('date'))
    if used is None or used >= natural:
        return None
    if not _cap_is_fresh(cap.get('checked_at'), now=now):
        return None
    return used


def remember_related_sunday(store, metadata):
    """Persist or clear the short-lived official-week cap after a related collect."""
    if not isinstance(metadata, dict) or metadata.get('kind') != 'related':
        return
    used = _iso_date(metadata.get('end_date'))
    if used is None:
        return
    try:
        natural = last_sunday_on_or_before(
            dt.datetime.now(TZ).date() - dt.timedelta(days=related_end_lag_days()))
    except OverflowError:
        return
    clamp = metadata.get('end_date_clamped') or {}
    if clamp.get('reason') == RELATED_WEEK_FALLBACK_REASON:
        tried = _iso_date(clamp.get('tried')) or used + dt.timedelta(days=7)
        store.set('related_sunday_cap', {
            'date': used.isoformat(),
            'tried': tried.isoformat(),
            'source': 'fallback',
            'checked_at': metadata.get('fetched_at') or dt.datetime.now(TZ).isoformat(),
        })
        return
    if used == natural and store.get('related_sunday_cap'):
        store.set('related_sunday_cap', None)


def _related_week_unready(query, exc):
    from client import SanitizedClientError
    return (isinstance(query, dict) and query.get('kind') == 'related'
            and isinstance(exc, SanitizedClientError)
            and exc.code == 'business_error'
            and exc.business_code in (500, '500'))


def previous_related_week(query):
    """Shift a related window back seven days, keeping the original requested end."""
    end = dt.date.fromisoformat(query['end_date'])
    start = dt.date.fromisoformat(query['start_date'])
    used_end = end - dt.timedelta(days=7)
    used_start = start - dt.timedelta(days=7) if start > used_end else start
    requested = query.get('requested_end_date') or end.isoformat()
    body = {k: v for k, v in query.items() if k != 'requested_end_date'}
    extra = {'requested_end_date': requested} if requested != used_end.isoformat() else {}
    shifted = validate_insight({**body, 'start_date': used_start.isoformat(),
                                'end_date': used_end.isoformat(), **extra})
    return None if shifted['end_date'] == query['end_date'] else shifted


def validate_insight(body, *, historical=False, latest_related_date=None):
    if type(body) is not dict:
        raise HubError('invalid_query', '请求须为 JSON 对象。')
    if body.get('kind') not in KINDS:
        raise HubError('invalid_kind', 'kind 须为 related 或 audience。')
    return require_platform(body.get('platform', 'douyin')).validate_insight(
        body, historical=historical, latest_related_date=latest_related_date)


def _validate_douyin_insight(body, *, historical=False, latest_related_date=None):
    body = dict(body)
    kind = body.pop('kind')
    requested_end = body.pop('requested_end_date', None)
    query = validate_query(body, historical=historical)
    if kind == 'related' and not historical:
        end = dt.date.fromisoformat(query['end_date'])
        start = dt.date.fromisoformat(query['start_date'])
        original_end, original_start = end, start
        try:
            cap = dt.datetime.now(TZ).date() - dt.timedelta(days=related_end_lag_days())
        except OverflowError:
            raise HubError('invalid_dates', '关联词截止日上限超出支持的日期范围。') from None
        if latest_related_date is not None:
            cap = min(cap, dt.date.fromisoformat(latest_related_date))
        used = last_sunday_on_or_before(min(end, cap))
        if used != original_end or start > used:
            requested_end = requested_end or original_end.isoformat()
            if start > used:
                start = used - (original_end - original_start)
            query = validate_query({**query, 'start_date': start.isoformat(), 'end_date': used.isoformat()}, historical=historical)
        if requested_end is not None:
            try:
                requested = dt.date.fromisoformat(requested_end)
                if requested.isoformat() != requested_end or requested <= dt.date.fromisoformat(query['end_date']):
                    raise ValueError
            except (TypeError, ValueError):
                raise HubError('invalid_dates', '原截止日须为晚于实际截止日的 YYYY-MM-DD 日期。') from None
            query['requested_end_date'] = requested_end
    if len(query['keywords']) != 1:
        raise HubError('invalid_keywords', '关联分析和人群画像每次仅支持一个关键词。')
    return {**query, 'kind': kind}


def number(v):
    if v is None or v == '': return None
    if isinstance(v, bool): return None
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError): return None


def label(v):
    return v[:200] if isinstance(v, str) else ''


def project(data, kind):
    if not isinstance(data, dict): raise HubError('response_schema', '扩展数据格式尚未确认。', 502)
    if kind == 'related':
        output = {}
        if not any(k in data for k in ('relation_word_list','search_relation_word_list')):
            raise HubError('response_schema', '未找到关联词字段；未将未知响应当作空数据。',502)
        for source, dest in [('relation_word_list','content_related'),('search_relation_word_list','search_related')]:
            rows = data.get(source, [])
            if not isinstance(rows,list):raise HubError('response_schema','关联词数据格式异常。',502)
            output[dest] = [{'keyword':label(r.get('relation_word')),
                'relation_score':number(r.get('relation_score')), 'search_index':number(r.get('search_index')),
                'composite_index':number(r.get('compos_index')), 'score_rate':number(r.get('score_rate'))}
                for r in rows[:100] if isinstance(r,dict) and label(r.get('relation_word'))]
        return output
    dimensions=data.get('data')
    if not isinstance(dimensions,list):raise HubError('response_schema','未找到人群画像维度；未将未知响应当作空数据。',502)
    allowed={'gender','age','province','city','yuntu_city_level','interest','interest_category','interest_category_v2'}
    result=[]
    for d in dimensions[:30]:
        if not isinstance(d,dict) or d.get('name_en') not in allowed:continue
        rows=d.get('label_list')
        if not isinstance(rows,list):raise HubError('response_schema','画像分组格式异常。',502)
        result.append({'dimension':d['name_en'],'name':label(d.get('name_zh')) or d['name_en'],
            'labels':[{'name':label(r.get('name_zh')) or label(r.get('name_en')),
                       'value':number(r.get('value')),'tgi':number(r.get('tgi'))}
                      for r in rows[:100] if isinstance(r,dict)]})
    if dimensions and not result:raise HubError('response_schema','画像返回了尚未适配的维度。',502)
    return {'dimensions':result,'value_unit':'upstream_raw','note':'value 保留接口原值，尚未核实比例单位，不擅自乘以 100；TGI 参考群体以官方说明为准。'}


def collect(api, body):
    try:
        query=validate_insight(body)
    except HubError as exc:
        logging.getLogger('index_hub.insights').warning('collect invalid_query code=%s', exc.code)
        return {'ok':False,'code':exc.code,'message':str(exc)}
    if not api._lock.acquire(blocking=False):return {'ok':False,'code':'busy'}
    try:
        client=api._client()
        try:
            from client import ORIGIN
            endpoint=ORIGIN+'/api/v2/index/'+('get_relation_word' if query['kind']=='related' else 'get_portrait')
            def post(current):
                return client._post(endpoint,{'param':{'keyword':current['keywords'][0],
                    'start_date':current['start_date'].replace('-',''),'end_date':current['end_date'].replace('-',''),'app_name':'aweme'}}, allow_empty_data=True)
            week_fallback_from=None
            try:
                result=post(query)
            except Exception as exc:
                fallback=None
                if _related_week_unready(query, exc):
                    try:
                        fallback=previous_related_week(query)
                    except HubError:
                        fallback=None
                if not fallback:
                    raise
                try:
                    result=post(fallback)
                except Exception:
                    raise exc
                week_fallback_from=query['end_date']
                query=fallback
            raw_data=result['data']
            if raw_data is None or raw_data == {}:
                return {'ok':False,'code':'no_data'}
            if isinstance(raw_data,dict):
                if query['kind']=='related':
                    keys={'relation_word_list','search_relation_word_list'}
                    if all(raw_data.get(k) is None or raw_data.get(k) == [] for k in keys):
                        metadata={'BaseResp','status_code','status','code','msg','message','extra','log_pb'}
                        if raw_data.keys() <= keys | metadata:
                            return {'ok':False,'code':'no_data'}
                        raise HubError('response_schema','关联词空响应包含未知字段。',502)
                elif 'data' in raw_data and (raw_data['data'] is None or raw_data['data'] == []):
                    return {'ok':False,'code':'no_data'}
            data=project(result['data'],query['kind'])
            clamp = {}
            note = '扩展接口试验接入；未完成与官方页面的逐项人工核对。'
            if week_fallback_from or 'requested_end_date' in query:
                if week_fallback_from:
                    reason = RELATED_WEEK_FALLBACK_REASON
                    clamp['end_date_clamped'] = {'requested': query.get('requested_end_date') or week_fallback_from,
                                                 'tried': week_fallback_from,
                                                 'used': query['end_date'], 'reason': reason}
                    note += f"{reason}，已从 {week_fallback_from} 调整为 {query['end_date']}。"
                else:
                    reason = RELATED_ALIGN_REASON
                    clamp['end_date_clamped'] = {'requested': query['requested_end_date'],
                                                 'used': query['end_date'], 'reason': reason}
                    note += f"{reason}，已从 {query['requested_end_date']} 调整为 {query['end_date']}。"
            return {'ok':True,'data':data,'metadata':{**query,**clamp,'source':'creator.douyin.com',
                'fetched_at':result['metadata']['fetched_at'],'verification':'pending',
                'note':note}}
        finally:
            client.session.cookies.clear();client.session.close()
    except Exception as exc:
        from client import SanitizedClientError
        if getattr(exc, 'code', None) == 'authentication_required':
            api._on_auth_failed()
        if isinstance(exc,SanitizedClientError):return {'ok':False,**exc.as_dict()}
        if isinstance(exc,HubError):return {'ok':False,'code':exc.code,'message':str(exc)}
        if isinstance(exc,ValueError) and getattr(exc,'code',None)=='authentication_required':
            return {'ok':False,'code':'authentication_required','message':str(exc)}
        logging.getLogger('index_hub.insights').error('collect kind=%s %s', query['kind'], exc_summary(exc))
        return {'ok':False,'code':'request_failed','message':'扩展数据读取失败，未输出原始错误。'}
    finally:
        api._fetch_cookie_fingerprint = None
        api._lock.release()
