"""Platform registry; adapters own platform-specific collection and storage."""


class Registry:
    def __init__(self):
        self._platforms = {}

    def register(self, adapter):
        identifier = adapter.description['id']
        if identifier in self._platforms:
            raise ValueError('duplicate platform: ' + identifier)
        self._platforms[identifier] = adapter

    def get(self, identifier):
        return self._platforms.get(identifier)

    def descriptions(self):
        return [dict(adapter.description) for adapter in self._platforms.values()]


class Placeholder:
    def __init__(self, description):
        self.description = description


class Douyin:
    description = {'id': 'douyin', 'name': '抖音指数', 'available': True, 'metric': 'search_index',
                   'metric_name': '搜索指数', 'unit': '指数', 'region': '全国', 'timezone': 'Asia/Shanghai',
                   'description': '创作者中心关键词搜索指数；不代表实际搜索次数。'}

    def initialize(self, store):
        pass

    def paused(self, store):
        return store.get('paused')

    async def run_one(self, hub, row):
        return await hub._run_douyin(row)

    def validate_query(self, body, *, historical=False):
        from hub_store import _validate_douyin_query
        return _validate_douyin_query(body, historical=historical)

    def project(self, result, *, verification='pending'):
        from hub_store import public_result
        return public_result(result, verification=verification)

    def ingest(self, store, result, dataset_id=None):
        return store._ingest_douyin(result, dataset_id)

    def series(self, store, keyword, start=None, end=None):
        return store._douyin_series(self.description['id'], keyword, start, end)

    def csv_rows(self, data):
        from storage import _csv_keyword
        yield ['platform', 'metric', 'region', 'keyword', 'date', 'search_index', 'composite_index', 'fetched_at']
        for point in data['points']:
            yield ['douyin', 'search_index', '全国', _csv_keyword(data['keyword']), point['date'],
                   '' if point['value'] is None else point['value'],
                   '' if point['composite_index'] is None else point['composite_index'], point['fetched_at']]

    def bridge_command(self, query):
        return 'insight' if query.get('kind') else 'query'

    def collect(self, api, query):
        answer = api.fetch_batch('\n'.join(query['keywords']), query['end_date'], cloud=True, start_date=query['start_date'])
        answer.pop('folder', None)
        answer.pop('comparison', None)
        return answer


registry = Registry()
registry.register(Douyin())
registry.register(Placeholder({'id': 'baidu', 'name': '百度指数', 'available': False, 'metric': None,
                               'description': '待接入，指标及授权方式尚未确定。'}))
registry.register(Placeholder({'id': 'google_trends', 'name': 'Google Trends', 'available': False, 'metric': None,
                               'description': '待接入；未来按其自身地区、时间窗和相对热度口径展示。'}))


def require_platform(identifier):
    from hub_store import HubError
    adapter = registry.get(identifier) if isinstance(identifier, str) else None
    if adapter is None or not adapter.description['available']:
        raise HubError('platform_unavailable', '该平台尚未接入。', 422)
    return adapter
