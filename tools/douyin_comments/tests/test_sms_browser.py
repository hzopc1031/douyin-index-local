"""Headed Chromium + owned Xvfb, with ALL navigation fulfilled in memory.

No HTTP listener is created. Fake official DOM uses calibrated public IDs;
unknown URLs are aborted. All identifiers below are synthetic test values.
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from dyc import site
from dyc.errors import Stop
from dyc.runtime import Profile
from dyc.sms_browser import SmsBrowser, points_checked
from dyc.sms_state import LoginError
from tests.test_sms_state import PHONE, CODE

HTML = '''<!doctype html><meta charset="utf-8"><style>
body {margin:0} #douyin_login_comp_button_input_id span {cursor:pointer} #captcha_container {width:320px;height:220px;background:#abcdef;position:relative}
.secsdk-captcha-drag-wrapper {position:absolute;left:5px;top:180px;width:300px;height:30px;background:gray}
.secsdk-captcha-drag-icon {position:absolute;width:30px;height:30px;background:blue}
#captcha-verify-image {position:absolute;left:5px;top:30px;width:300px;height:140px;background:gray}
.captcha_verify_img_slide {position:absolute;left:5px;top:60px;width:30px;height:30px;background:green}
</style><button id="header" onclick="document.querySelector('#login-panel-new').hidden=false">登录</button>
<article id="login-panel-new" hidden><div id="douyin_login_comp_scan_code">
<img aria-label="二维码" width="178" height="178" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='178' height='178'%3E%3Crect width='178' height='178' fill='black'/%3E%3C/svg%3E"></div>
<div id="douyin_login_comp_mobile_code"><input id="normal-input" type="tel">
<div id="douyin_login_comp_button_input_id"><input id="button-input" type="tel"><span onclick="send()">获取验证码</span></div>
<div id="douyin_login_comp_btn_id" onclick="submit()">登录</div></div><span>登录即代表同意</span></article>
<script>
window.sends=0; window.submissions=0;
async function send() { await fetch('/passport/web/send_code/', {method:'POST',body:document.querySelector('#normal-input').value}); window.sends++; }
async function submit() { await fetch('/submit', {method:'POST',body:document.querySelector('#button-input').value}); window.submissions++; }
function success() { document.querySelector('#login-panel-new').remove(); document.querySelector('#header').remove();
document.body.insertAdjacentHTML('beforeend','<div data-e2e="user-avatar">Synthetic account</div>');
document.cookie='sessionid=synthetic-session-token-abcdefghijklmnopqrstuvwxyz; Secure; Path=/'; }
function challenge(text='拖动滑块完成拼图') { document.querySelector('#captcha_container')?.remove();
document.body.insertAdjacentHTML('beforeend', `<div id="captcha_container"><div id="hint">${text}</div>
<div id="captcha-verify-image"></div><div class="captcha_verify_img_slide"></div>
<div class="secsdk-captcha-drag-wrapper"><div class="secsdk-captcha-drag-icon"></div></div>
<span id="echo" style="position:absolute;top:70px;left:100px">hidden</span><input id="echo-input" style="position:absolute;top:110px;left:80px"></div>`); }
</script>'''


class SmsDomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = Path(self.tmp.name) / 'profile'
        self.foreign = 0
        self.sends = self.submits = 0
        self.correct_phone = self.correct_code = False

    async def backend(self, route):
        request = route.request
        if request.url == site.FAKE_ORIGIN + '/passport/web/send_code/' and request.method == 'POST':
            self.correct_phone = request.post_data == PHONE
            self.sends += 1
            await route.fulfill(status=200, body='{"message":"success","data":{"error_code":0}}')
        elif request.url == site.FAKE_ORIGIN + '/submit' and request.method == 'POST':
            self.correct_code = request.post_data == CODE
            self.submits += 1
            await route.fulfill(status=200, body='{"message":"success","data":{"error_code":0}}')
        elif request.url in (site.FAKE_ORIGIN, site.FAKE_ORIGIN + '/'):
            await route.fulfill(content_type='text/html; charset=utf-8', body=HTML)
        else:
            self.foreign += 1
            await route.abort()

    @asynccontextmanager
    async def browser(self, solver=None):
        browser = SmsBrowser(self.profile, backend=self.backend, solver=solver)
        try:
            await browser.start()
            await browser.reset(mode="sms")
            yield browser
        finally:
            await browser.close()

    async def test_start_waits_for_delayed_editable_phone_not_just_panel(self):
        original = self.backend
        async def delayed(route):
            if route.request.url.rstrip('/') == site.FAKE_ORIGIN.rstrip('/'):
                await route.fulfill(content_type='text/html; charset=utf-8', body=HTML + """
                <script>
                document.querySelector('#login-panel-new').hidden=false;
                document.querySelector('#normal-input').disabled=true;
                setTimeout(()=>document.querySelector('#normal-input').disabled=false, 500);
                </script>""")
            else:
                await original(route)
        self.backend = delayed
        async with self.browser() as browser:
            self.assertTrue(await browser.page.locator('#normal-input').is_editable())
            self.assertEqual(self.sends, 0)
            self.assertEqual(self.submits, 0)

    async def test_duplicate_hidden_and_visible_inputs_still_become_ready(self):
        async def duplicate(route):
            await route.fulfill(content_type='text/html; charset=utf-8', body=HTML + """
            <script>
            const panel=document.querySelector('#login-panel-new');
            const visible=panel.cloneNode(true); visible.hidden=false;
            document.body.append(visible, visible.cloneNode(true));
            </script>""")
        self.backend = duplicate
        async with self.browser() as browser:
            self.assertEqual(await browser.page.locator('#normal-input').count(), 3)
            self.assertTrue(await browser.phone_ready())
            self.assertEqual(await browser.visible('#normal-input').input_value(), '')
            self.assertEqual(await browser.observe(), 'pending')
            self.assertEqual((self.sends, self.submits), (0, 0))

    async def test_actual_detached_iframe_read_recovers_without_skipping_challenge(self):
        async with self.browser() as browser:
            page = browser.page
            await page.evaluate("""() => {
                const f=document.createElement('iframe'); f.srcdoc='<p>synthetic</p>';
                document.body.append(f);
            }""")
            handle = await page.locator('iframe').element_handle()
            frame = await handle.content_frame()
            await page.locator('iframe').evaluate('e=>e.remove()')
            self.assertTrue(frame.is_detached())
            class PageProxy:
                stale = True
                def __getattr__(self, name):
                    return getattr(page, name)
                @property
                def frames(self):
                    if self.stale:
                        self.stale = False
                        return [frame]
                    return page.frames
            browser.page = PageProxy()
            try:
                self.assertIsNone(await browser.challenge())
                self.assertEqual(browser.last_read_error['category'], 'frame_detached')
                self.assertEqual(browser.last_read_error['operation'], 'challenge_candidates_count')
                await page.evaluate('challenge()')
                self.assertIsNotNone(await browser.challenge())
            finally:
                browser.page = page

    async def test_start_stops_on_challenge_before_opening_login(self):
        async def blocked(route):
            await route.fulfill(content_type='text/html; charset=utf-8', body=HTML + '<script>challenge()</script>')
        browser = SmsBrowser(self.profile, backend=blocked, state_dir=Path(self.tmp.name) / 'state')
        with self.assertRaisesRegex(Stop, 'challenge_detected'):
            await browser.start()
        self.assertEqual(browser.diagnostic['reason'], 'challenge_detected')
        self.assertTrue(Path(browser.diagnostic['screenshot']).exists())
        self.assertIsNone(browser.page)
        self.assertEqual(self.sends, 0)

    async def test_calibrated_phone_code_flow_persists_only_login_cookies(self):
        async with self.browser() as browser:
            self.assertEqual(await browser.observe(), 'pending')
            self.assertNotEqual(browser.context, browser.persistent)
            self.assertTrue(all(p.url == 'about:blank' for p in browser.persistent.pages))
            with self.assertRaises(Stop):
                with Profile(self.profile):
                    self.fail('profile lock bypassed')
            await browser.send(PHONE)
            await browser.page.wait_for_function('window.sends === 1')
            await browser.submit(CODE)
            await browser.page.wait_for_function('window.submissions === 1')
            self.assertTrue(self.correct_phone and self.correct_code)
            self.assertEqual(await browser.page.locator('#button-input').input_value(), '')
            await browser.page.evaluate('''([phone,code]) => {
              localStorage.setItem('phone', phone); localStorage.setItem('code',code);
              document.cookie='phone='+phone+'; Secure; Path=/';
              success(); }''', [PHONE, CODE])
            self.assertEqual(await browser.observe(), 'succeeded')
            await browser.persist()
            cookies = await browser.persistent.cookies(site.FAKE_ORIGIN)
            self.assertEqual({c['name'] for c in cookies}, {'sessionid'})
        self.assertEqual(self.foreign, 0)
        for path in self.profile.rglob('*'):
            if path.is_file() and not path.is_symlink():
                data = path.read_bytes()
                for value in (PHONE, CODE):
                    self.assertNotIn(value.encode(), data, path.name)
                    self.assertNotIn(value.encode('utf-16-le'), data, path.name)
        with Profile(self.profile):
            pass
        async with self.browser() as reopened:
            restored = await reopened.context.cookies(site.FAKE_ORIGIN)
            self.assertEqual({c['name'] for c in restored}, {'sessionid'})
            self.assertGreater(restored[0]['expires'], 0)

    async def test_official_cookie_expiration_preserved(self):
        import time
        expiration = int(time.time()) + 3600
        async with self.browser() as browser:
            await browser.page.evaluate('success()')
            await browser.context.add_cookies([dict(name='sessionid', value='synthetic-persistent-token',
                domain='dyc.invalid', path='/', secure=True, expires=expiration)])
            await browser.persist()
            cookies = await browser.persistent.cookies(site.FAKE_ORIGIN)
            self.assertEqual(cookies[0]['expires'], expiration)

    async def test_screenshots_hide_reflected_numbers_and_inputs_before_capture(self):
        async with self.browser() as browser:
            await browser.page.evaluate('challenge()')
            await browser.page.evaluate('''([phone,code]) => {
              document.querySelector('#echo').textContent=phone+' '+code;
              document.querySelector('#echo-input').value=phone+code; }''', [PHONE, CODE])
            first = await browser.capture(await browser.challenge())
            await browser.page.evaluate('''() => {
              document.querySelector('#echo').textContent='XXXXXXXXXXX YYYYYY';
              document.querySelector('#echo-input').value='XXXXXXXXXXXYYYYYY'; }''')
            second = await browser.capture(await browser.challenge())
            self.assertEqual(first.png, second.png)
            self.assertEqual((first.metadata.width, first.metadata.height), (320, 220))

    async def test_manual_frame_replay_stale_pixels_bounds_and_qr_switch(self):
        async with self.browser() as browser:
            self.assertTrue((await browser.frame(False))['image'])
            await browser.page.evaluate("challenge('旋转图片')")
            frame = await browser.frame(True)
            await browser.page.evaluate("document.querySelector('#captcha_container').style.background='red'")
            with self.assertRaisesRegex(LoginError, 'stale_frame'):
                await browser.pointer(frame['frame'], [[.1,.9,0], [.5,.9,50]])
            fresh = await browser.frame(True)
            await browser.page.evaluate('window.mousedown=0;document.addEventListener("mousedown",()=>window.mousedown++)')
            await browser.pointer(fresh['frame'], [[.1,.9,0], [.5,.9,50]])
            self.assertEqual(await browser.page.evaluate('window.mousedown'), 1)
            with self.assertRaisesRegex(LoginError, 'stale_frame'):
                await browser.pointer(fresh['frame'], [[.1,.9,0]])
            with self.assertRaisesRegex(LoginError, 'invalid_points'):
                await browser.pointer('fresh', [[float('nan'), .5, 0]])
            await browser.reset()
            self.assertIsNone(await browser.challenge())
            self.assertTrue((await browser.frame(False))['image'])

    async def test_ordinary_gap_only_max_three_total_never_reset_by_mode(self):
        solver = type('Solver', (), {'solve': AsyncMock(return_value=[[.1,.9,0],[.5,.9,10]]),
                                      'close': AsyncMock()})()
        async with self.browser(solver) as browser:
            await browser.page.evaluate("challenge('旋转图片')")
            await browser.auto_step()
            solver.solve.assert_not_awaited()
            for n in range(5):
                await browser.page.evaluate('challenge()')
                await browser.page.evaluate('(n)=>document.querySelector("#captcha_container").style.background=`rgb(${n*30},100,100)`', n)
                await browser.auto_step()
            self.assertEqual(browser.auto_attempts, 3)
            self.assertEqual(solver.solve.await_count, 3)
            await browser.reset()
            await browser.page.evaluate('challenge()')
            await browser.auto_step()
            self.assertEqual(solver.solve.await_count, 3)

    async def test_solver_uncertain_stays_manual_and_http_risk_fails(self):
        solver = type('Solver', (), {'solve': AsyncMock(return_value=None), 'close': AsyncMock()})()
        async with self.browser(solver) as browser:
            await browser.page.evaluate('challenge()')
            await browser.auto_step()
            self.assertEqual(browser.auto_attempts, 0)
            self.assertTrue(browser.auto_disabled)
            self.assertTrue((await browser.frame(True))['frame'])
            browser.fatal = True
            with self.assertRaises(Stop):
                await browser.send(PHONE)
            self.assertEqual(self.sends, 0)

    async def test_manual_prompt_order_is_visible_sanitized_and_bound_to_frame(self):
        async with self.browser() as browser:
            await browser.send(PHONE)
            browser.private_values.append(CODE)
            await browser.page.evaluate("challenge('请依次点击：春、夏、秋')")
            first = await browser.frame(True)
            self.assertEqual(first['prompt'], '请依次点击:春、夏、秋')
            await browser.page.locator('#hint').evaluate("e => e.textContent='请依次点击：秋、夏、春'")
            with self.assertRaisesRegex(LoginError, 'stale_frame'):
                await browser.pointer(first['frame'], [[.5,.5,0]])
            second = await browser.frame(True)
            self.assertEqual(first['image'], second['image'])
            self.assertNotEqual(first['prompt'], second['prompt'])
            await browser.page.locator('#echo').evaluate("e => e.textContent='春'")
            label_one = await browser.frame(True)
            await browser.page.locator('#echo').evaluate("e => e.textContent='秋'")
            label_two = await browser.frame(True)
            self.assertNotEqual(label_one['image'], label_two['image'])
            self.assertEqual(await browser.page.locator('[data-dyc-safe-label]').count(), 0)
            for text in ('请点击：' + PHONE, '请点击：' + CODE, '请点击：' + ' '.join(CODE)):
                await browser.page.locator('#hint').evaluate('(e, text) => e.textContent=text', text)
                data = await browser.frame(True)
                self.assertEqual(data['prompt'], '')
                self.assertNotIn(PHONE, str(data))
                self.assertNotIn(CODE, str(data))

    async def test_cross_origin_gap_stays_manual_and_redirect_cannot_escape(self):
        solver = type('Solver', (), {'solve': AsyncMock(return_value=[[.1,.9,0],[.5,.9,10]]),
                                      'close': AsyncMock()})()
        original_backend = self.backend
        async def frames(route):
            url = route.request.url
            if url == 'https://frame.invalid/flow':
                await route.fulfill(content_type='text/html; charset=utf-8', body=HTML + '''
                <script>document.querySelector('#login-panel-new').remove();
                document.querySelector('#header').remove();challenge();</script>''')
            elif url == 'https://frame.invalid/post':
                await route.fulfill(status=307, headers={'Location': 'https://leak.invalid/aweme/v1/web/comment/list/'})
            else:
                await original_backend(route)
        self.backend = frames
        async with self.browser(solver) as browser:
            await browser.page.evaluate('''() => { const f=document.createElement('iframe');
              f.src='https://frame.invalid/flow'; f.style='position:fixed;left:10px;top:10px;width:400px;height:350px';
              document.body.append(f); }''')
            for _ in range(50):
                frame = next((f for f in browser.page.frames if f.url == 'https://frame.invalid/flow'), None)
                if frame and await frame.locator('#captcha_container').count():
                    break
                await asyncio.sleep(.05)
            self.assertIsNotNone(frame)
            await browser.auto_step()
            solver.solve.assert_not_awaited()
            self.assertEqual(browser.auto_attempts, 0)
            self.assertTrue((await browser.frame(True))['image'])
            # Independent request-stage sink: even if the production response
            # guard regresses, NO redirected request can reach any real network.
            sink = await browser.context.new_cdp_session(frame)
            hops, tasks = [], set()
            async def paused(event):
                rid = event['requestId']
                if event['request']['url'] != 'https://frame.invalid/post':
                    hops.append('redirected')
                    await sink.send('Fetch.failRequest', dict(requestId=rid, errorReason='BlockedByClient'))
                else:
                    await sink.send('Fetch.continueRequest', dict(requestId=rid))
            def launch(event):
                task = asyncio.create_task(paused(event)); tasks.add(task); task.add_done_callback(tasks.discard)
            sink.on('Fetch.requestPaused', launch)
            await sink.send('Fetch.enable', dict(patterns=[dict(urlPattern='*', requestStage='Request')]))
            try:
                await frame.evaluate("() => fetch('/post', {method:'POST',body:'synthetic-credentials'}).catch(()=>{})")
                self.assertEqual(hops, [])
                self.assertTrue(browser.fatal)
            finally:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_worker_first_post_redirect_is_guarded_by_owner_page(self):
        original = self.backend
        async def workers(route):
            path = route.request.url.removeprefix(site.FAKE_ORIGIN.rstrip('/'))
            if path == '/worker.js':
                await route.fulfill(content_type='application/javascript', body="fetch('/worker-post', {method:'POST',body:'synthetic'}).catch(()=>{})")
            elif path == '/worker-post':
                await route.fulfill(status=307, headers={'Location': 'https://leak.invalid/aweme/v1/web/comment/list/'})
            else:
                await original(route)
        self.backend = workers
        async with self.browser() as browser:
            sink = await browser.context.new_cdp_session(browser.page)
            escaped, tasks = [], set()
            async def paused(event):
                rid = event['requestId']
                allowed = [site.FAKE_ORIGIN.rstrip('/') + path for path in ('/worker.js', '/worker-post')]
                if event['request']['url'] not in allowed:
                    escaped.append('unexpected_request')
                    await sink.send('Fetch.failRequest', dict(requestId=rid, errorReason='BlockedByClient'))
                else:
                    await sink.send('Fetch.continueRequest', dict(requestId=rid))
            def launch(event):
                task = asyncio.create_task(paused(event)); tasks.add(task); task.add_done_callback(tasks.discard)
            sink.on('Fetch.requestPaused', launch)
            await sink.send('Fetch.enable', dict(patterns=[dict(urlPattern='*', requestStage='Request')]))
            try:
                await browser.page.evaluate("() => {window.worker = new Worker('/worker.js')}")
                async with asyncio.timeout(5):
                    while not browser.fatal:
                        await asyncio.sleep(.02)
                self.assertEqual(browser.fatal_reason, 'redirect_denied_xhr')
                self.assertEqual(escaped, [])
            finally:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_worker_post_redirect_to_login_dependency_is_allowed(self):
        original = self.backend
        async def workers(route):
            path = route.request.url.removeprefix(site.FAKE_ORIGIN.rstrip('/'))
            if path == '/worker.js':
                await route.fulfill(content_type='application/javascript', body="fetch('/worker-post', {method:'POST',body:'synthetic'}).catch(()=>{})")
            elif path == '/worker-post':
                await route.fulfill(status=307, headers={'Location': 'https://sdk.amemv.com/device/register'})
            else:
                await original(route)
        self.backend = workers
        async with self.browser() as browser:
            sink = await browser.context.new_cdp_session(browser.page)
            escaped, tasks = [], set()
            async def paused(event):
                rid = event['requestId']
                allowed = [site.FAKE_ORIGIN.rstrip('/') + path for path in ('/worker.js', '/worker-post')]
                if event['request']['url'] not in allowed:
                    escaped.append('unexpected_request')
                    await sink.send('Fetch.failRequest', dict(requestId=rid, errorReason='BlockedByClient'))
                else:
                    await sink.send('Fetch.continueRequest', dict(requestId=rid))
            def launch(event):
                task = asyncio.create_task(paused(event)); tasks.add(task); task.add_done_callback(tasks.discard)
            sink.on('Fetch.requestPaused', launch)
            await sink.send('Fetch.enable', dict(patterns=[dict(urlPattern='*', requestStage='Request')]))
            try:
                await browser.page.evaluate("() => {window.worker = new Worker('/worker.js')}")
                async with asyncio.timeout(5):
                    while not escaped:
                        await asyncio.sleep(.02)
                self.assertFalse(browser.fatal)
                self.assertEqual(escaped, ['unexpected_request'])
            finally:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_audio_worklet_runs_and_phone_stays_editable(self):
        async with self.browser() as browser:
            async with asyncio.timeout(8):
                result = await browser.page.evaluate("""async () => {
                    const context = new OfflineAudioContext(1, 128, 48000);
                    const url = URL.createObjectURL(new Blob([`
                        class Ready extends AudioWorkletProcessor {
                            constructor() { super(); this.port.postMessage('ready'); }
                            process() { return true; }
                        }
                        registerProcessor('ready', Ready);
                    `], {type:'application/javascript'}));
                    try {
                        await context.audioWorklet.addModule(url);
                        const node = new AudioWorkletNode(context, 'ready');
                        const ready = new Promise(resolve => node.port.onmessage = e => resolve(e.data));
                        node.connect(context.destination);
                        await context.startRendering();
                        return await ready;
                    } finally { URL.revokeObjectURL(url); }
                }""")
            self.assertEqual(result, 'ready')
            self.assertFalse(browser.fatal)
            self.assertTrue(await browser.page.locator('#normal-input').is_visible())
            self.assertTrue(await browser.page.locator('#normal-input').is_editable())
            self.assertEqual(self.sends, 0)
            self.assertEqual(self.submits, 0)
            self.assertEqual(self.foreign, 0)

    async def test_unconfirmed_account_never_commits_cookie(self):
        async with self.browser() as browser:
            await browser.send(PHONE)
            with self.assertRaises(Stop):
                await browser.persist()
            self.assertEqual(await browser.persistent.cookies(), [])
            await browser.page.evaluate('success()')
            await browser.context.clear_cookies()
            with self.assertRaises(Stop):
                await browser.persist()

    async def test_lease_released_after_failed_start(self):
        async def broken(route):
            await route.fulfill(content_type='text/html', body='<div>no login</div>')
        browser = SmsBrowser(self.profile, backend=broken)
        with self.assertRaises(asyncio.TimeoutError):
            async with asyncio.timeout(2):
                await browser.start()
        self.assertIsNone(browser.profile.fd)
        self.assertIsNone(browser.display._auth_dir)
        with Profile(self.profile):
            pass


class PointTests(unittest.TestCase):
    def test_reject_invalid_and_unbounded_trajectories(self):
        for values in ([], [[2, .5, 0]], [[.5,.5,1]], [[.5,.5,0],[.5,.5,0]],
                       [[.5,.5,0],[.5,.5,5001]], [[True,.5,0]]):
            with self.assertRaises(LoginError):
                points_checked(values)


class RedirectTests(unittest.IsolatedAsyncioTestCase):
    async def test_fatal_response_blocks_page_retry_before_next_poll(self):
        from types import SimpleNamespace
        for status in (401, 403, 429):
            browser = SmsBrowser('/unused', backend=AsyncMock())
            browser.response(SimpleNamespace(status=status, request=SimpleNamespace(resource_type='xhr')))
            route = SimpleNamespace(request=SimpleNamespace(url='https://www.douyin.com/retry', method='POST'), abort=AsyncMock(), continue_=AsyncMock())
            await browser.route(route)
            route.abort.assert_awaited_once()
            route.continue_.assert_not_awaited()
            browser.backend.assert_not_awaited()

    async def test_private_and_comment_redirects_stopped_before_follow(self):
        browser = SmsBrowser('/unused')
        browser.response_session = type('CDP', (), {'send': AsyncMock()})()
        browser.private_values = [PHONE, CODE]
        for method, target in (('POST', '/aweme/v1/web/comment/list/'), ('GET', '/aweme/v1/web/comment/list/reply/'),
                               ('GET', '/path?phone=' + PHONE)):
            await browser.check_response(dict(requestId='synthetic', responseStatusCode=307,
                request=dict(method=method, url='https://www.douyin.com/passport/'),
                responseHeaders=[dict(name='Location', value=target)]))
            self.assertEqual(browser.response_session.send.call_args.args[0], 'Fetch.failRequest')
            self.assertTrue(browser.fatal)
        browser.fatal = False
        await browser.check_response(dict(requestId='synthetic', responseStatusCode=302,
            request=dict(method='GET', url='https://www.douyin.com/'),
            responseHeaders=[dict(name='Location', value='/jingxuan')]))
        self.assertEqual(browser.response_session.send.call_args.args[0], 'Fetch.continueRequest')
        self.assertFalse(browser.fatal)
