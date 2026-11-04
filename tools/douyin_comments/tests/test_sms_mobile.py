"""Exercise the actual mobile HTML/JS through Playwright's in-memory routes."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit
from unittest.mock import AsyncMock, patch

from dyc.errors import Stop
from dyc.sms_browser import SmsBrowser
from dyc.sms_state import LoginSession, SmsQuota
from dyc.sms_web import HEADERS, WebApp
from tests.test_sms_browser import HTML
from tests.test_sms_state import ORIGIN, PHONE, CODE, DummyBrowser


class MobileTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_observes_qr_responses_on_pages_and_frames(self):
        with tempfile.TemporaryDirectory() as root:
            async def official(route):
                if '/check_qrconnect' in route.request.url:
                    await route.fulfill(content_type='application/json', body='{"data":{"status":2}}',
                                        headers={'Access-Control-Allow-Origin': '*'})
                else:
                    await route.fulfill(content_type='text/html; charset=utf-8', body=HTML)
            browser = SmsBrowser(Path(root) / 'profile', backend=official)
            await browser.start()
            try:
                page = await browser.context.new_page()
                await page.goto('https://www.douyin.com/')
                async with page.expect_event('framenavigated'):
                    await page.evaluate("""() => { const f = document.createElement('iframe');
                        f.src = 'https://sso.douyin.com/'; document.body.appendChild(f); }""")
                frame = await (await page.locator('iframe').element_handle()).content_frame()
                with patch('dyc.sms_browser.audit') as audit:
                    await page.evaluate("fetch('https://www.douyin.com/check_qrconnect/')")
                    await frame.evaluate("fetch('https://sso.douyin.com/check_qrconnect/')")
                    await page.evaluate("fetch('https://untrusted.invalid/check_qrconnect/')")
                    await asyncio.gather(*browser.response_tasks)
                events = [c for c in audit.call_args_list if c.args[0] == 'qrconnect_response']
                self.assertEqual(len(events), 2)
            finally:
                await browser.close()

    async def test_mobile_qr_flow_and_no_browser_storage(self):
        await self.mobile_flow(no_token=False)

    async def test_mobile_no_token_direct_login_and_refresh(self):
        await self.mobile_flow(no_token=True)

    async def test_preparation_failure_manual_retry_refresh_and_polling(self):
        with tempfile.TemporaryDirectory() as root:
            async def official(route):
                await route.fulfill(content_type='text/html; charset=utf-8', body=HTML)
            engine = SmsBrowser(Path(root)/'profile', backend=official)
            await engine.start()
            browsers = []
            entered, release = asyncio.Event(), asyncio.Event()
            async def wait():
                entered.set()
                await release.wait()
            def factory():
                browser = DummyBrowser()
                browser.diagnose = AsyncMock(return_value=dict(reason='browser_operation_failed',
                    step='phone_input_ready', operation='challenge_candidates_count'))
                if not browsers:
                    browser.start.side_effect = RuntimeError('synthetic-private')
                else:
                    browser.start.side_effect = wait
                browsers.append(browser)
                return LoginSession(browser, SmsQuota(Path(root)/'state'))
            app = WebApp(ORIGIN, '', factory, no_token=True)
            front = await engine.persistent.browser.new_context(viewport=dict(width=390,height=844),
                is_mobile=True, has_touch=True, service_workers='block')
            requests = []
            async def frontend(route):
                req = route.request
                if not req.url.startswith(ORIGIN + '/'):
                    await route.abort(); return
                headers = await req.all_headers()
                headers['host'] = urlsplit(req.url).netloc
                target = urlsplit(req.url).path
                action = json.loads(req.post_data or '{}').get('action') if target == '/api' else None
                requests.append((target, action))
                status, extra, data = await app.dispatch(req.method, target, headers, (req.post_data or '').encode())
                if isinstance(data, dict): data=json.dumps(data).encode()
                await route.fulfill(status=status, headers=HEADERS | {'Content-Type':'application/json'} | extra, body=data)
            await front.route('**/*', frontend)
            await front.route_web_socket('**/*', lambda socket:socket.close())
            page = await front.new_page()
            await page.clock.install()
            try:
                await page.goto(ORIGIN)
                await page.locator('#retry').wait_for(state='visible')
                self.assertIn('准备手机号输入', await page.locator('#details').inner_text())
                self.assertTrue(await page.locator('#cancel').is_disabled())
                await page.clock.fast_forward(30_000)
                await app.tick()
                self.assertEqual(len(browsers),1)
                self.assertFalse(app.done.is_set())
                await page.reload()
                await page.locator('#retry').wait_for(state='visible')
                self.assertEqual(len(browsers),1)
                await page.locator('#retry').click()
                await asyncio.wait_for(entered.wait(),5)
                await page.clock.fast_forward(30_000)
                self.assertTrue(await page.locator('#retry').is_disabled())
                self.assertEqual(requests.count(('/api','retry')),1)
                self.assertEqual(requests.count(('/api','status')),0)
                release.set()
                await page.locator('#picture').wait_for(state='visible')
                self.assertTrue(await page.locator('#retry').is_hidden())
                await page.clock.fast_forward(2600)
                await page.wait_for_function("() => !document.querySelector('#refresh').disabled")
                self.assertGreaterEqual(requests.count(('/api','status')),1)
                await page.locator('#cancel').click()
                await page.wait_for_function("() => document.querySelector('#status').textContent.includes('已结束')")
                self.assertEqual(len(browsers),2)
                await page.locator('#retry').click()
                await page.locator('#picture').wait_for(state='visible')
                self.assertEqual(len(browsers),3)
                app.session.deadline = 0
                await app.tick()
                await page.clock.fast_forward(2600)
                await page.locator('#retry').wait_for(state='visible')
                self.assertIn('到期', await page.locator('#status').inner_text())
                self.assertFalse(app.done.is_set())
                await page.locator('#retry').click()
                await page.locator('#picture').wait_for(state='visible')
                self.assertEqual(len(browsers),4)
                for browser in browsers: browser.send.assert_not_awaited()
            finally:
                release.set()
                await front.close(); await app.close(); await engine.close()

    async def mobile_flow(self, *, no_token):
        with tempfile.TemporaryDirectory() as root:
            sent = []
            foreign = []
            async def official(route):
                request = route.request
                if request.url == 'https://dyc.invalid/':
                    await route.fulfill(content_type='text/html; charset=utf-8', body=HTML)
                elif request.url in ('https://dyc.invalid/passport/web/send_code/', 'https://dyc.invalid/submit') and request.method == 'POST':
                    sent.append((urlsplit(request.url).path, request.post_data == (PHONE if request.url.endswith('/passport/web/send_code/') else CODE)))
                    await route.fulfill(body='{"message":"success","data":{"error_code":0}}')
                else:
                    foreign.append('blocked')
                    await route.abort()
            browser = SmsBrowser(Path(root) / 'profile', backend=official)
            await browser.start()
            start_requested, release_start = asyncio.Event(), asyncio.Event()
            async def delayed_start():
                start_requested.set()
                await release_start.wait()
            browser.start = AsyncMock(side_effect=delayed_start)  # no second profile
            session = LoginSession(browser, SmsQuota(Path(root) / 'state'))
            app = WebApp(ORIGIN, 'synthetic-owner-token', lambda: session, no_token=no_token)
            front = await browser.persistent.browser.new_context(viewport=dict(width=390, height=844),
                                                                 is_mobile=True, has_touch=True, service_workers='block')
            requests = []
            rejected = []
            console = []
            frame_requested, release_frame = asyncio.Event(), asyncio.Event()
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
                if status != 200:
                    rejected.append((target, status))
                if (target == '/api' and json.loads(req.post_data or '{}').get('action') == 'frame'
                        and not frame_requested.is_set()):
                    frame_requested.set()
                    await release_frame.wait()
                if isinstance(data, dict):
                    data = json.dumps(data).encode()
                await route.fulfill(status=status, headers=HEADERS | {'Content-Type': 'application/json'} | extra, body=data)
            await front.route('**/*', frontend)
            await front.route_web_socket('**/*', lambda socket: socket.close())
            await front.add_cookies([dict(name='other_tool', value='synthetic-theme', url=ORIGIN)])
            page = await front.new_page()
            await page.clock.install()
            page.on('console', lambda item: console.append(item.text))
            page.on('pageerror', lambda error: console.append(str(error)))
            try:
                await page.goto(ORIGIN)
                self.assertLessEqual(await page.evaluate('document.documentElement.scrollWidth'), 390)
                if not no_token:
                    await page.locator('#token').fill('synthetic-owner-token')
                    await page.locator('#unlock').click()
                await asyncio.wait_for(start_requested.wait(), 5)
                # Simulate a mobile page waiting longer than the server budget.
                # It must neither cancel /auth nor poll/retry while it is pending.
                await page.clock.fast_forward(95_000)
                self.assertEqual([r for r in requests if r[0] == 'POST'], [('POST', '/auth')])
                self.assertTrue(await page.locator('#login').is_hidden())
                release_start.set()
                await page.locator('#login').wait_for(state='visible')
                if no_token:
                    self.assertTrue(await page.locator('#auth').is_hidden())
                    self.assertEqual(sent, [])
                    await page.reload()
                    await page.locator('#login').wait_for(state='visible')
                    browser.start.assert_awaited_once()
                self.assertEqual(await page.locator('#token').input_value(), '')
                await asyncio.wait_for(frame_requested.wait(), 5)
                release_frame.set()
                await page.locator('#frame').wait_for(state='visible')
                await page.wait_for_function("() => document.querySelector('#frame').src.startsWith('data:image/png;base64,')")
                self.assertEqual(await page.locator('#smsMode, #phone, #send, #code, #submit').count(), 0)
                await page.evaluate("""() => { window.frameChanges = 0;
                  new MutationObserver(() => window.frameChanges++).observe(document.querySelector('#frame'), {attributes: true, attributeFilter: ['src']}); }""")
                before = requests.count(('POST', '/api'))
                await page.clock.fast_forward(2600)
                await page.wait_for_function("() => !document.querySelector('#refresh').disabled")
                self.assertGreaterEqual(requests.count(('POST', '/api')), before + 2)
                self.assertEqual(await page.evaluate('window.frameChanges'), 0)
                self.assertEqual(sent, [])
                self.assertEqual(await page.evaluate('[localStorage.length, sessionStorage.length]'), [0, 0])
                self.assertEqual(await page.evaluate('document.cookie'), 'other_tool=synthetic-theme')  # auth remains HttpOnly
                self.assertNotIn(PHONE, ''.join(console))
                self.assertNotIn(CODE, ''.join(console))
                self.assertFalse(foreign)
                self.assertFalse(rejected)
                self.assertTrue(all(method == 'POST' for method, path in requests if path in ('/auth', '/api')))
            finally:
                release_start.set()
                release_frame.set()
                await front.close()
                await app.close()
