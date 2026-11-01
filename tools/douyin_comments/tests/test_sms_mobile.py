"""Exercise the actual mobile HTML/JS through Playwright's in-memory routes."""
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit
from unittest.mock import AsyncMock

from dyc.sms_browser import SmsBrowser
from dyc.sms_state import LoginSession, SmsQuota
from dyc.sms_web import HEADERS, WebApp
from tests.test_sms_browser import HTML
from tests.test_sms_state import ORIGIN, PHONE, CODE


class MobileTests(unittest.IsolatedAsyncioTestCase):
    async def test_mobile_sms_flow_qr_toggle_and_no_browser_storage(self):
        with tempfile.TemporaryDirectory() as root:
            sent = []
            foreign = []
            async def official(route):
                request = route.request
                if request.url == 'https://dyc.invalid/':
                    await route.fulfill(content_type='text/html; charset=utf-8', body=HTML)
                elif request.url in ('https://dyc.invalid/sms', 'https://dyc.invalid/submit') and request.method == 'POST':
                    sent.append((urlsplit(request.url).path, request.post_data == (PHONE if request.url.endswith('/sms') else CODE)))
                    await route.fulfill(body='{}')
                else:
                    foreign.append('blocked')
                    await route.abort()
            browser = SmsBrowser(Path(root) / 'profile', backend=official)
            await browser.start()
            browser.start = AsyncMock()  # owned browser already active, no second profile
            session = LoginSession(browser, SmsQuota(Path(root) / 'state'))
            app = WebApp(ORIGIN, 'synthetic-owner-token', lambda: session)
            front = await browser.persistent.browser.new_context(viewport=dict(width=390, height=844),
                                                                 is_mobile=True, has_touch=True, service_workers='block')
            requests = []
            console = []
            async def frontend(route):
                req = route.request
                if not req.url.startswith(ORIGIN + '/'):
                    foreign.append('blocked'); await route.abort(); return
                headers = await req.all_headers()
                headers['host'] = urlsplit(req.url).netloc
                target = urlsplit(req.url).path
                requests.append((req.method, target))
                status, extra, data = await app.dispatch(req.method, target, headers,
                                                        (req.post_data or '').encode())
                if isinstance(data, dict):
                    data = json.dumps(data).encode()
                await route.fulfill(status=status, headers=HEADERS | {'Content-Type': 'application/json'} | extra, body=data)
            await front.route('**/*', frontend)
            await front.route_web_socket('**/*', lambda socket: socket.close())
            await front.add_cookies([dict(name='other_tool', value='synthetic-theme', url=ORIGIN)])
            page = await front.new_page()
            page.on('console', lambda item: console.append(item.text))
            page.on('pageerror', lambda error: console.append(str(error)))
            try:
                await page.goto(ORIGIN)
                self.assertLessEqual(await page.evaluate('document.documentElement.scrollWidth'), 390)
                await page.locator('#token').fill('synthetic-owner-token')
                await page.locator('#unlock').click()
                await page.locator('#login').wait_for(state='visible')
                self.assertEqual(await page.locator('#token').input_value(), '')
                await page.locator('#qrMode').click()
                await page.locator('#frame').wait_for(state='visible')
                self.assertTrue(await page.locator('#sms').is_hidden())
                await page.locator('#smsMode').click()
                await page.locator('#sms').wait_for(state='visible')
                await page.locator('#phone').fill(PHONE)
                await page.locator('#send').click()
                await page.wait_for_function("() => document.querySelector('#status').textContent.includes('已请求发码')")
                self.assertEqual(await page.locator('#phone').input_value(), '')
                await page.locator('#code').fill(CODE)
                await page.locator('#submit').click()
                await page.wait_for_function("() => document.querySelector('#status').textContent.includes('已提交')")
                self.assertEqual(await page.locator('#code').input_value(), '')
                self.assertEqual(sent, [('/sms', True), ('/submit', True)])
                self.assertEqual(await page.evaluate('[localStorage.length, sessionStorage.length]'), [0, 0])
                self.assertEqual(await page.evaluate('document.cookie'), 'other_tool=synthetic-theme')  # auth remains HttpOnly
                self.assertNotIn(PHONE, ''.join(console))
                self.assertNotIn(CODE, ''.join(console))
                self.assertFalse(foreign)
                self.assertTrue(all(method == 'POST' for method, path in requests if path in ('/auth', '/api')))
            finally:
                await front.close()
                await app.close()
