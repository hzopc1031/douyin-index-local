"""Calibrated public login structure recreated entirely from synthetic HTML."""
import asyncio
import base64
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from dyc import site
from dyc.browser import Browser
from dyc.errors import Control, Stop
from dyc.fake import FakeSite, FastPacer
from dyc.login_ui import prepare, snapshot
from dyc.runner import login

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC')
PANEL = '''<meta charset="utf-8"><article id="login-panel-new"><div id="douyin_login_comp_scan_code">
<div class="qrcode-placeholder"></div><img aria-label="二维码" width="178" height="178" src="/qr.png">
</div><div id="douyin_login_comp_mobile_code"><input type="tel">
<div id="douyin_login_comp_btn_id" onclick="window.submitted=true">登录</div></div></article>'''


class LoginDomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.control = Control()

    def browser(self, backend):
        return Browser(Path(self.tmp.name) / 'profile', self.control, None, FastPacer(),
                       backend=backend, login_mode=True)

    async def test_header_navigation_waits_for_loaded_image_not_placeholder(self):
        requested, release = asyncio.Event(), asyncio.Event()

        class Fixture(FakeSite):
            async def deliver(self, route):
                if route.request.url.endswith('/qr.png'):
                    requested.set()
                    await release.wait()
                    await route.fulfill(status=200, content_type='image/png', body=PNG)
                elif route.request.url.endswith('/jingxuan'):
                    await route.fulfill(content_type='text/html; charset=utf-8', body=PANEL)
                else:
                    await route.fulfill(content_type='text/html; charset=utf-8', body='''
                    <div onclick="window.wrong=true">登录</div><div class="qrcode-placeholder">Loading</div>
                    <button onclick="location.href='/jingxuan'">登录</button>''')

        fake = Fixture()
        async with self.browser(fake) as browser:
            await browser.page.goto(site.FAKE_ORIGIN)
            pending = asyncio.create_task(prepare(browser))
            try:
                await asyncio.wait_for(requested.wait(), 5)
                self.assertFalse(pending.done())
                with self.assertRaises(Stop):
                    await snapshot(browser.page)
                release.set()
                await asyncio.wait_for(pending, 5)
                self.assertTrue(browser.page.url.endswith('/jingxuan'))
                self.assertFalse(await browser.page.evaluate('Boolean(window.submitted)'))
                self.assertTrue((await snapshot(browser.page)).startswith(b'\x89PNG'))
                self.assertEqual(fake.calls, [])
                self.assertEqual(fake.foreign, [])
            finally:
                release.set()
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_open_panel_never_clicks_header_or_submit(self):
        class Fixture(FakeSite):
            def html(self, _):
                return ('<button onclick="window.wrong=true">登录</button>' + PANEL).replace(
                    '/qr.png', 'data:image/png;base64,' + base64.b64encode(PNG).decode())

        async with self.browser(Fixture()) as browser:
            await browser.page.goto(site.FAKE_ORIGIN)
            await prepare(browser)
            self.assertFalse(await browser.page.evaluate('Boolean(window.wrong || window.submitted)'))

    async def test_empty_qrcode_container_and_broken_image_are_not_ready(self):
        class Fixture(FakeSite):
            def html(self, _):
                return '''<meta charset="utf-8"><article id="login-panel-new"><div class="qrcode">Loading</div>
                <div id="douyin_login_comp_scan_code"><img aria-label="二维码" src="data:image/png;base64,bad"></div>
                <div onclick="window.submitted=true">登录</div></article>'''

        async with self.browser(Fixture()) as browser:
            await browser.page.goto(site.FAKE_ORIGIN)
            with patch('dyc.login_ui.PREPARE_SECONDS', 0.1):
                with self.assertRaises(Stop) as caught:
                    await prepare(browser)
            self.assertEqual((caught.exception.code, caught.exception.reason), (5, 'login_qr_missing'))
            with self.assertRaises(Stop):
                await snapshot(browser.page)
            self.assertFalse(await browser.page.evaluate('Boolean(window.submitted)'))

    async def test_verification_interrupts_image_wait(self):
        class Fixture(FakeSite):
            def html(self, _):
                return '<div id="login-panel-new">Loading</div>'

        async with self.browser(Fixture()) as browser:
            await browser.page.goto(site.FAKE_ORIGIN)
            pending = asyncio.create_task(prepare(browser))
            await browser.page.evaluate("document.body.insertAdjacentHTML('beforeend','<div data-dyc-risk=\"yes\">Verify</div>')")
            with self.assertRaises(Stop) as caught:
                await asyncio.wait_for(pending, 2)
            self.assertEqual(caught.exception.reason, 'verification_widget')

    async def test_session_restored_during_prepare_succeeds_without_qr(self):
        class Fixture(FakeSite):
            def html(self, _):
                return '<div data-dyc-login="no">Synthetic pending session</div>'

        fake = Fixture()
        class OfflineBrowser(Browser):
            def __init__(self, *args, **kwargs):
                kwargs['backend'] = fake
                super().__init__(*args, **kwargs)

        async def restore_during_prepare(browser):
            self.assertFalse((await browser.state())['inside'])
            await browser.page.evaluate("""() => setTimeout(() =>
                document.querySelector('[data-dyc-login]').setAttribute('data-dyc-login', 'yes'), 100)""")
            return await prepare(browser)

        args = Namespace(profile_dir=Path(self.tmp.name) / 'profile',
                         state_dir=Path(self.tmp.name) / 'state', display=None, wait_seconds=2)
        with patch('dyc.runner.Browser', OfflineBrowser), patch.object(site, 'LOGIN_URL', site.FAKE_ORIGIN), \
                patch('dyc.login_ui.prepare', side_effect=restore_during_prepare) as preparation, \
                patch('dyc.login_ui.snapshot') as screenshot:
            code, result = await login(args)
        self.assertEqual((code, result['exit_reason']), (0, 'logged_in'))
        preparation.assert_awaited_once()
        screenshot.assert_not_called()
        self.assertFalse(Path(result['qr_path']).exists())
        self.assertEqual(fake.calls, [])
        self.assertEqual(fake.foreign, [])
