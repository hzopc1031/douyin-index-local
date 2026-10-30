"""Bounded projections for official index insight endpoints; no credential output."""
import logging
import math
from hub_store import HubError, exc_summary, validate_query

KINDS = {'related', 'audience'}

def validate_insight(body):
    if type(body) is not dict:
        raise HubError('invalid_query', '请求须为 JSON 对象。')
    body = dict(body)
    kind = body.pop('kind', None)
    if kind not in KINDS:
        raise HubError('invalid_kind', 'kind 须为 related 或 audience。')
    query = validate_query(body)
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
    query=validate_insight(body)
    if not api._lock.acquire(blocking=False):return {'ok':False,'code':'busy'}
    try:
        client=api._client()
        try:
            from client import ORIGIN
            endpoint=ORIGIN+'/api/v2/index/'+('get_relation_word' if query['kind']=='related' else 'get_portrait')
            result=client._post(endpoint,{'param':{'keyword':query['keywords'][0],
                'start_date':query['start_date'].replace('-',''),'end_date':query['end_date'].replace('-',''),'app_name':'aweme'}}, allow_empty_data=True)
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
            return {'ok':True,'data':data,'metadata':{**query,'source':'creator.douyin.com',
                'fetched_at':result['metadata']['fetched_at'],'verification':'pending',
                'note':'扩展接口试验接入；未完成与官方页面的逐项人工核对。'}}
        finally:
            client.session.cookies.clear();client.session.close()
    except Exception as exc:
        from client import SanitizedClientError
        if isinstance(exc,SanitizedClientError):return {'ok':False,**exc.as_dict()}
        if isinstance(exc,HubError):return {'ok':False,'code':exc.code,'message':str(exc)}
        if isinstance(exc,ValueError):
            code='authentication_required' if getattr(exc,'code',None)=='authentication_required' else 'browser_unavailable'
            return {'ok':False,'code':code,'message':str(exc)}
        logging.getLogger('index_hub.insights').error('collect kind=%s %s', query['kind'], exc_summary(exc))
        return {'ok':False,'code':'request_failed','message':'扩展数据读取失败，未输出原始错误。'}
    finally:api._lock.release()
