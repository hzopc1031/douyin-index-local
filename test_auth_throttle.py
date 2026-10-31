"""Synthetic authentication protection tests with a deterministic clock."""
import unittest
from unittest.mock import patch

from hub_auth_throttle import AuthThrottle


class AuthThrottleTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        with patch.dict('os.environ', {}, clear=True):
            self.throttle = AuthThrottle(clock=lambda: self.now)

    def fail(self, ip='attacker', count=12):
        for _ in range(count):
            self.throttle.failure(ip)

    def test_ban_expiry_escalation_and_cap(self):
        self.fail(count=11)
        self.assertEqual(self.throttle.remaining('attacker'), 0)
        self.fail(count=1)
        self.assertEqual(self.throttle.remaining('attacker'), 900)
        self.now += .1
        self.assertEqual(self.throttle.remaining('attacker'), 900)
        self.now += 100
        self.assertEqual(self.throttle.remaining('attacker'), 800)
        self.throttle.success('attacker')
        self.assertEqual(self.throttle.remaining('attacker'), 800)
        until = self.throttle.ips['attacker'].banned_until
        for _ in range(100):
            self.throttle.reject('attacker')
        self.assertEqual(self.throttle.ips['attacker'].banned_until, until)
        self.now = until
        self.assertEqual(self.throttle.remaining('attacker'), 0)
        self.fail()
        self.assertEqual(self.throttle.remaining('attacker'), 1800)
        for _ in range(8):
            self.now = self.throttle.ips['attacker'].banned_until
            self.fail()
        self.assertEqual(self.throttle.remaining('attacker'), 86400)

    def test_global_protection_and_success_expiry(self):
        self.throttle.success('owner')
        self.fail(count=199)
        self.assertEqual(self.throttle.remaining('new'), 0)
        self.fail(ip='last', count=1)
        self.assertEqual(self.throttle.remaining('new'), 300)
        self.assertEqual(self.throttle.remaining('owner'), 0)
        self.now += 10.25
        self.assertEqual(self.throttle.remaining('new'), 290)
        self.now = 1300
        self.assertEqual(self.throttle.remaining('new'), 0)
        self.now = 87400
        self.fail(ip='new', count=200)
        self.assertEqual(self.throttle.remaining('owner'), 300)

    def test_sliding_window(self):
        self.fail(count=11)
        self.now += 60
        self.fail(count=1)
        self.assertEqual(self.throttle.remaining('attacker'), 0)
        self.assertEqual(len(self.throttle.failures), 1)

    def test_eviction_preserves_bans_and_success_records(self):
        with patch('hub_auth_throttle.AUTH_MAX_IPS', 3):
            self.fail('banned')
            self.throttle.success('owner')
            self.fail('old', 1)
            self.now += 1
            self.fail('new', 1)
            self.assertEqual(set(self.throttle.ips), {'banned', 'owner', 'new'})
            self.fail('new', 11)
            self.fail('overflow', 1)
            self.assertEqual(len(self.throttle.ips), 3)
            self.assertGreater(self.throttle.remaining('overflow'), 0)
            self.assertEqual(self.throttle.remaining('owner'), 0)

    def test_logs_are_bounded(self):
        with self.assertLogs('index_hub.auth', level='INFO') as captured:
            for _ in range(30):
                self.throttle.unauthorized('a', 'GET', '/api/test', 'api')
            self.now += 60
            self.throttle.unauthorized('a', 'GET', '/api/test', 'api')
            self.fail('a')
            for _ in range(100):
                self.throttle.reject('a')
            self.now += 60
            self.throttle.reject('a')
            self.throttle.reject('a')
        warnings = [line for line in captured.output if line.startswith('WARNING')]
        self.assertEqual(len(warnings), 22)  # 20 + next-window 401 + one ban
        self.assertTrue(any('unauthorized_suppressed=10' in line for line in captured.output))
        self.assertTrue(any('rejected=101' in line for line in captured.output))
        self.assertEqual(sum('rejected=' in line for line in captured.output), 1)

    def test_environment_overrides_and_invalid_defaults(self):
        with patch.dict('os.environ', {'INDEX_HUB_AUTH_FAIL_LIMIT': '2',
                                      'INDEX_HUB_AUTH_BAN_SECONDS': '7',
                                      'INDEX_HUB_AUTH_GLOBAL_LIMIT': '3'}):
            throttle = AuthThrottle(clock=lambda: self.now)
        throttle.failure('a')
        throttle.failure('a')
        self.assertEqual(throttle.remaining('a'), 7)
        throttle.failure('b')
        self.assertEqual(throttle.remaining('new'), 300)
        for invalid in ('0', '-1', 'oops', '1.5', ''):
            with patch.dict('os.environ', dict.fromkeys([
                    'INDEX_HUB_AUTH_FAIL_LIMIT', 'INDEX_HUB_AUTH_BAN_SECONDS',
                    'INDEX_HUB_AUTH_GLOBAL_LIMIT'], invalid)):
                throttle = AuthThrottle()
            self.assertEqual((throttle.fail_limit, throttle.ban_seconds, throttle.global_limit),
                             (12, 900, 200))


class GatewayMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import base64
        import json
        import tempfile
        from pathlib import Path
        from desktop_gateway import create_app, HUB_KEY
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / '.cloud-state').mkdir()
        (root / '.cloud-state/access.json').write_text(json.dumps({
            'username': 'fixture', 'password': 'synthetic-only'}))
        self.now = 1000.0
        with patch.dict('os.environ', {}, clear=True):
            self.throttle = AuthThrottle(clock=lambda: self.now)
        self.app = create_app(root, desktop_dir=root, auth_throttle=self.throttle)
        self.addAsyncCleanup(self.app[HUB_KEY].close, self.app)
        self.admin = 'Basic ' + base64.b64encode(b'fixture:synthetic-only').decode()

    async def request(self, ip='owner', authorization=None):
        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request
        headers = {} if authorization is None else {'Authorization': authorization}
        request = make_mocked_request('GET', '/api/v1/platforms', headers=headers, app=self.app)
        request._cache['remote'] = ip

        async def handler(request):
            return web.json_response({'ok': True})
        return await self.app.middlewares[0](request, handler)

    async def test_missing_header_ban_and_remaining(self):
        import json
        for _ in range(25):
            self.assertEqual((await self.request()).status, 401)
        self.assertFalse(self.throttle.failures)
        for _ in range(12):
            self.assertEqual((await self.request(authorization='wrong')).status, 401)
        response = await self.request(authorization=self.admin)
        self.assertEqual(response.status, 429)
        self.assertEqual(response.headers['Retry-After'], '900')
        self.assertEqual(json.loads(response.text)['error'], {
            'code': 'authentication_throttled', 'message': '认证失败次数过多，请稍后再试。'})
        self.now += 100.1
        response = await self.request(authorization=self.admin)
        self.assertEqual(response.headers['Retry-After'], '800')
        self.assertEqual((await self.request()).status, 401)
        self.now = 1900
        self.assertEqual((await self.request(authorization=self.admin)).status, 200)
        for _ in range(12):
            await self.request(authorization='wrong')
        self.assertEqual((await self.request(authorization=self.admin)).headers['Retry-After'], '1800')

    async def test_global_block_exempts_successful_ip(self):
        self.assertEqual((await self.request(authorization=self.admin)).status, 200)
        for i in range(200):
            self.assertEqual((await self.request(ip=str(i), authorization='wrong')).status, 401)
        self.assertEqual((await self.request(ip='new', authorization=self.admin)).status, 429)
        self.assertEqual((await self.request(authorization=self.admin)).status, 200)
        self.assertEqual((await self.request(ip='new')).status, 401)


class TrustedNetworkTests(unittest.TestCase):
    def test_global_lockout_skips_tailnet_but_not_ip_ban(self):
        import os
        from unittest.mock import patch
        from hub_auth_throttle import AuthThrottle
        now = [1000.0]
        with patch.dict(os.environ, {'INDEX_HUB_AUTH_GLOBAL_LIMIT': '3'}):
            throttle = AuthThrottle(clock=lambda: now[0])
        for i in range(3):
            throttle.failure(f'203.0.113.{i}')
        self.assertGreater(throttle.remaining('198.51.100.7'), 0)
        self.assertEqual(throttle.remaining('100.112.4.9'), 0)
        for _ in range(12):
            throttle.failure('100.112.4.9')
        self.assertGreater(throttle.remaining('100.112.4.9'), 0)

    def test_trusted_networks_configurable(self):
        import os
        from unittest.mock import patch
        from hub_auth_throttle import AuthThrottle
        with patch.dict(os.environ, {'INDEX_HUB_AUTH_TRUSTED_NETS': '10.0.0.0/8, bad'}):
            with self.assertLogs('index_hub.auth', 'WARNING'):
                throttle = AuthThrottle()
        self.assertTrue(throttle.is_trusted('10.1.2.3'))
        self.assertFalse(throttle.is_trusted('100.112.4.9'))
        self.assertFalse(throttle.is_trusted('unknown'))
