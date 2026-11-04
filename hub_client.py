"""Small HTTPS client for WorkBuddy integrations; no browser cookies required."""
from urllib.parse import urlsplit
import requests


class IndexHubError(RuntimeError):
    pass


class IndexHubClient:
    def __init__(self, base_url, api_key, *, ca_bundle=True, timeout=15):
        parsed = urlsplit(base_url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('base_url must be an HTTPS origin')
        if not isinstance(api_key, str) or not api_key.startswith('idx_') or '\n' in api_key or '\r' in api_key:
            raise ValueError('Invalid API key')
        if ca_bundle is False or ca_bundle is None:
            raise ValueError('TLS verification must remain enabled')
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({'Authorization': 'Bearer ' + api_key, 'Accept': 'application/json'})
        self.verify, self.timeout = ca_bundle, timeout

    def _request(self, method, path, **kwargs):
        try:
            response = self.session.request(method, self.base_url + '/api/v1/' + path,
                                            timeout=self.timeout, verify=self.verify,
                                            allow_redirects=False, **kwargs)
            try:
                if response.status_code not in (200, 201, 202):
                    raise IndexHubError('Index API returned HTTP ' + str(response.status_code) + '; no automatic retry')
                value = response.json()
                if not isinstance(value, dict):
                    raise ValueError
                return value
            finally:
                response.close()
        except requests.RequestException:
            raise IndexHubError('Index API connection failed; no automatic retry. Check TLS trust and service availability.') from None
        except ValueError:
            raise IndexHubError('Index API response format is invalid') from None

    def status(self):
        return self._request('GET', 'status')

    def submit_query(self, keywords, *, end_date, idempotency_key, start_date=None, platform='douyin'):
        return self._request('POST', 'queries', headers={'Idempotency-Key': idempotency_key},
                             json={'platform': platform, 'keywords': keywords, 'end_date': end_date, **({'start_date':start_date} if start_date else {})})['job']

    def job(self, identifier):
        if not isinstance(identifier, str) or len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
            raise ValueError('Invalid job id')
        return self._request('GET', 'jobs/' + identifier)['job']

    def series(self, keyword, *, start_date=None, end_date=None, platform='douyin'):
        params = {'platform': platform, 'keyword': keyword}
        if start_date is not None:
            params.update(start_date=start_date, end_date=end_date)
        return self._request('GET', 'series', params=params)

    def close(self):
        self.session.headers.pop('Authorization', None)
        self.session.close()
