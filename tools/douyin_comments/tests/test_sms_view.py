"""Offline QR/SMS views and redacted evidence; all requests fulfilled in memory."""
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from dyc import site
from dyc.sms_browser import PHONE as PHONE_SELECTOR
from tests import test_sms_browser as fixture
from tests.test_sms_state import PHONE, CODE

# Synthetic QR-only initial view; a decoy send label must never be clicked.
QR_HTML = fixture.HTML.replace('<div id="douyin_login_comp_mobile_code">', '''
<button role="tab" onclick="document.querySelector('#douyin_login_comp_mobile_code').hidden=false;
 document.querySelector('#douyin_login_comp_scan_code').hidden=true">手机号登录</button>
<div id="douyin_login_comp_mobile_code" hidden>''').replace(
    '<div id="douyin_login_comp_btn_id"', '''<label><input id="terms" type="checkbox">同意用户协议和隐私政策</label>
<div id="douyin_login_comp_btn_id"''').replace('<script>', '''
<span onclick="window.decoy=true">获取验证码</span><script>''').replace(
    'async function send() {', "async function send() { if (!document.querySelector('#terms').checked) return;")


class ViewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.SmsDomTests.asyncSetUp
    browser = fixture.SmsDomTests.browser

    async def backend(self, route):
        if route.request.url.rstrip('/') == site.FAKE_ORIGIN:
            await route.fulfill(content_type='text/html; charset=utf-8', body=QR_HTML)
        else:
            await fixture.SmsDomTests.backend(self, route)

    async def test_qr_switch_agreement_unique_send_and_evidence(self):
        async with self.browser() as browser:
            browser.state_dir = Path(self.tmp.name)
            self.assertTrue(await browser.sms_ready())
            self.assertTrue(await browser.page.locator('#douyin_login_comp_scan_code').is_hidden())
            self.assertEqual(await browser.page.locator(PHONE_SELECTOR).input_value(), '')
            self.assertFalse(await browser.page.locator('#terms').is_checked())
            with patch('dyc.sms_browser.SMS_RESPONSE_SECONDS', .2):
                result = await browser.send(PHONE)
            self.assertEqual(result['outcome'], 'accepted')
            self.assertTrue(await browser.page.locator('#terms').is_checked())
            self.assertIsNone(await browser.page.evaluate('window.decoy'))
            self.assertEqual(self.sends, 1)
            evidence = list((browser.state_dir / 'send-evidence').iterdir())
            self.assertEqual(len(evidence), 4)
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in evidence))
            for path in evidence:
                for secret in (PHONE, CODE, 'synthetic-session-token'):
                    self.assertNotIn(secret.encode(), path.read_bytes())
            await browser.page.evaluate('''([phone,code]) => {
                document.body.insertAdjacentHTML('beforeend', '<button style="width:250px">'+phone+' '+code+'</button>');
                document.querySelector('#button-input').value=code;
            }''', [PHONE, CODE])
            first = await browser.diagnostic_snapshot()
            await browser.page.evaluate('''() => {
                document.body.lastElementChild.textContent='XXXXXXXXXXX YYYYYY';
                document.querySelector('#normal-input').value='XXXXXXXXXXX';
                document.querySelector('#button-input').value='YYYYYY';
            }''')
            self.assertEqual(first, await browser.diagnostic_snapshot())

    async def test_role_button_enabled_by_phone_and_checked_agreement(self):
        async with self.browser() as browser:
            await browser.page.locator('#terms').check()
            await browser.page.evaluate("""() => {
                document.querySelector('#douyin_login_comp_button_input_id span').outerHTML =
                  '<button id="real-send" disabled onclick="send()"><span>获取验证码</span></button>';
                document.querySelector('#normal-input').addEventListener('input', () => {
                  document.querySelector('#real-send').disabled = false;
                });
            }""")
            with patch('dyc.sms_browser.SMS_RESPONSE_SECONDS', .2):
                result = await browser.send(PHONE)
            self.assertEqual(result['outcome'], 'accepted')
            self.assertTrue(await browser.page.locator('#terms').is_checked())
            self.assertEqual(self.sends, 1)

    async def test_blocking_dialog_does_not_produce_acceptance(self):
        async with self.browser() as browser:
            await browser.page.evaluate('''() => {
                document.body.insertAdjacentHTML('beforeend', '<div style="position:fixed;inset:0;background:white;z-index:99999">Modal</div>');
            }''')
            with patch('dyc.sms_browser.SMS_RESPONSE_SECONDS', .1):
                result = await browser.send(PHONE)
            self.assertEqual(result['outcome'], 'unconfirmed')
            self.assertIn('遮挡', result['description'])
            self.assertEqual(self.sends, 0)
            self.assertEqual(await browser.page.locator(PHONE_SELECTOR).input_value(), '')

    async def test_missing_sms_view_does_not_fill_or_click(self):
        async with self.browser() as browser:
            await browser.page.locator('#douyin_login_comp_mobile_code').evaluate('(e)=>e.hidden=true')
            await browser.page.get_by_role('tab').evaluate('(e)=>e.remove()')
            result = await browser.send(PHONE)
            self.assertEqual(result['outcome'], 'unconfirmed')
            self.assertIn('未进入手机号验证码登录视图', result['description'])
            self.assertEqual(await browser.page.locator(PHONE_SELECTOR).input_value(), '')
            self.assertEqual(self.sends, 0)

    async def test_qr_fallback_does_not_switch_back_to_sms(self):
        async with self.browser() as browser:
            await browser.reset(mode='qr')
            self.assertTrue(await browser.page.locator('#douyin_login_comp_scan_code').is_visible())
            self.assertFalse(await browser.phone_ready())
            self.assertTrue((await browser.frame(False))['image'])
            self.assertEqual(self.sends, 0)

    async def test_gap_waits_for_delayed_platform_confirmation(self):
        solver = type('Solver', (), {'solve': AsyncMock(return_value=[[.1,.9,0],[.5,.9,10]]),
                                      'close': AsyncMock()})()
        async with self.browser(solver) as browser:
            await browser.page.evaluate("challenge()")
            async def replay(*_):
                await browser.page.evaluate("setTimeout(()=>document.querySelector('#captcha_container').remove(), 250)")
            browser.replay = replay
            await browser.auto_step()
            self.assertIsNone(await browser.challenge())
            self.assertEqual(browser.auto_attempts, 1)
            self.assertEqual(self.sends, 0)

    async def test_business_stop_during_solver_prevents_replay(self):
        browser_ref = None
        async def rejected(*_):
            browser_ref.fatal = True
            browser_ref.fatal_reason = 'sms_rate_limited'
            return [[.1,.9,0],[.5,.9,10]]
        solver = type('Solver', (), {'solve': AsyncMock(side_effect=rejected), 'close': AsyncMock()})()
        async with self.browser(solver) as browser:
            browser_ref = browser
            await browser.page.evaluate("challenge()")
            browser.replay = AsyncMock()
            from dyc.errors import Stop
            with self.assertRaises(Stop):
                await browser.auto_step()
            browser.replay.assert_not_awaited()
            self.assertEqual(browser.auto_attempts, 0)

    async def test_gap_during_send_uses_existing_solver_once(self):
        async with self.browser() as browser:
            original = browser.send_button
            button = original()
            async def click(**kwargs):
                await button.click(**kwargs)
                await browser.page.evaluate('challenge()')
            browser.send_button = lambda: type('Button', (), {'count': button.count, 'click': staticmethod(click)})()
            browser.auto_step = AsyncMock(side_effect=lambda: None)
            with patch('dyc.sms_browser.SMS_RESPONSE_SECONDS', .1):
                result = await browser.send(PHONE)
            self.assertEqual(result['outcome'], 'accepted')
            browser.auto_step.assert_awaited()
