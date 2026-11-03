"""Offline QR/SMS views and redacted evidence; all requests fulfilled in memory."""
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from dyc import site
from dyc.errors import Stop
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
            await browser.page.locator('#terms').check()
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
            }''', [PHONE[:3] + ' ' + PHONE[3:7] + ' ' + PHONE[7:], CODE])
            first = await browser.diagnostic_snapshot()
            await browser.page.evaluate('''() => {
                document.body.lastElementChild.textContent='XXXXXXXXXXX YYYYYY';
                document.querySelector('#normal-input').value='XXXXXXXXXXX';
                document.querySelector('#button-input').value='YYYYYY';
            }''')
            self.assertEqual(first, await browser.diagnostic_snapshot())

    async def test_click_diagnostics_and_redacted_values(self):
        async with self.browser() as browser:
            await browser.page.locator('#terms').check()
            await browser.page.evaluate("""([phone, code]) => {
              document.body.insertAdjacentHTML('beforeend', '<div role="alert">未知提示 ' + phone + ' ' + code + '</div>');
            }""", [PHONE[:3] + ' ' + PHONE[3:7] + ' ' + PHONE[7:], CODE])
            with patch('dyc.sms_browser.SMS_RESPONSE_SECONDS', .3):
                result = await browser.send(PHONE)
            diagnostic = result['diagnostic']
            self.assertTrue(diagnostic['hit'])
            self.assertTrue(diagnostic['panel_active'])
            self.assertEqual(diagnostic['hint'], 'other')
            for event in diagnostic['events'].values():
                self.assertTrue(event['arrived'])
                self.assertTrue(event['trusted'])
            self.assertNotIn(PHONE, str(diagnostic))
            self.assertNotIn(CODE, str(diagnostic))
            metadata = browser.request_metadata('https://example.test/path/' + PHONE[:3] + '%20' + PHONE[3:] + '?code=' + CODE, 'POST')
            self.assertNotIn(CODE, str(metadata))
            self.assertNotIn(PHONE[:3], str(metadata))
            self.assertEqual(self.sends, 1)

    async def test_page_owned_events_are_reduced_to_booleans(self):
        async with self.browser() as browser:
            await browser.page.evaluate("""([phone, code]) => {
              window.__dycClickEvents = {click: {arrived: phone, trusted: code}, extra: phone};
            }""", [PHONE, CODE])
            result = await browser.send_diagnostic()
            self.assertFalse(result['events']['click']['arrived'])
            self.assertFalse(result['events']['click']['trusted'])
            self.assertNotIn(PHONE, str(result))
            self.assertNotIn(CODE, str(result))

    async def test_challenge_iframe_and_text_detected(self):
        async with self.browser() as browser:
            await browser.page.evaluate("""() => {
                const frame = document.createElement('iframe');
                frame.src = '/captcha'; frame.style = 'width:80px;height:80px';
                document.body.append(frame);
            }""")
            self.assertIsNotNone(await browser.challenge())
            await browser.page.evaluate("""() => {
                document.querySelector('iframe[src="/captcha"]').remove();
                document.body.insertAdjacentHTML('beforeend', '<div>安全验证</div>');
            }""")
            self.assertIsNotNone(await browser.challenge())
            self.assertEqual(self.sends, 0)

    async def test_existing_panel_and_fallback_fields(self):
        async with self.browser() as browser:
            await browser.page.evaluate("""() => {
              document.querySelector('#normal-input').outerHTML = '<input name="normal-input">';
              document.querySelector('#button-input').outerHTML = '<input placeholder="验证码">';
              document.querySelector('[role=tab]').onclick = () => { window.unwantedSwitch = true; };
            }""")
            self.assertTrue(await browser.sms_ready())
            await browser.switch_sms(set())
            self.assertIsNone(await browser.page.evaluate('window.unwantedSwitch'))

    async def test_nested_send_and_unrelated_countdown(self):
        async with self.browser() as browser:
            await browser.page.evaluate("""() => {
              const span = document.querySelector('#douyin_login_comp_button_input_id span');
              span.outerHTML = '<div><span>获取验证码</span></div>';
              document.querySelector('#douyin_login_comp_mobile_code').insertAdjacentHTML('beforeend', '<button>59秒后</button>');
            }""")
            self.assertEqual(await browser.send_button().count(), 1)
            self.assertFalse(await browser.countdown())
            await browser.page.evaluate("document.querySelector('#douyin_login_comp_button_input_id span').textContent='59秒后'")
            self.assertTrue(await browser.countdown())

    async def test_disabled_span_is_not_clicked(self):
        async with self.browser() as browser:
            await browser.page.locator('#terms').check()
            await browser.page.evaluate("document.querySelector('#douyin_login_comp_button_input_id').setAttribute('aria-disabled','true')")
            with patch('dyc.sms_browser.SMS_RESPONSE_SECONDS', .1):
                result = await browser.send(PHONE)
            self.assertEqual(result['outcome'], 'unconfirmed')
            self.assertEqual(self.sends, 0)

    async def test_role_button_enabled_by_phone_and_checked_agreement(self):
        async with self.browser() as browser:
            await browser.page.locator('#terms').check()
            await browser.page.evaluate("""() => {
                document.querySelector('#douyin_login_comp_button_input_id span').outerHTML =
                  '<button id="real-send" aria-label="重新获取验证码" disabled onclick="send()"><span aria-hidden="true">↻</span></button>';
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
            self.assertIn('手机号输入或协议检查失败', result['description'])
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

    async def test_recorded_send_selector_targets_reconstructed_span(self):
        evidence = (Path(__file__).parent / 'fixtures/send-before-redacted.html').read_text()
        async with self.browser() as browser:
            recorded = await browser.page.evaluate("""html => {
              const doc = new DOMParser().parseFromString(html, 'text/html');
              return [...doc.querySelectorAll('[data-selector]')]
                .filter(e => e.textContent === '获取验证码' && e.dataset.selector === '#douyin_login_comp_button_input_id span')
                .map(e => e.dataset.selector);
            }""", evidence)
            self.assertEqual(len(recorded), 1)
            self.assertEqual(await browser.send_button().count(), 1)
            self.assertEqual(await browser.page.locator(recorded[0]).count(), 1)

    async def test_unchecked_agreement_stops_without_toggling(self):
        async with self.browser() as browser:
            result = await browser.send(PHONE)
            self.assertIn('协议复选框未勾选', result['description'])
            self.assertFalse(await browser.page.locator('#terms').is_checked())
            self.assertEqual(self.sends, 0)

    async def test_no_checkbox_requires_visible_notice(self):
        async with self.browser() as browser:
            await browser.page.locator('#terms').evaluate('(e) => e.parentElement.remove()')
            await browser.page.get_by_text('登录即代表同意', exact=True).evaluate('(e) => e.remove()')
            result = await browser.send(PHONE)
            self.assertIn('协议提示不存在或不可见', result['description'])
            self.assertEqual(self.sends, 0)

    async def test_formatted_number_and_enabled_span(self):
        async with self.browser() as browser:
            await browser.page.locator('#terms').check()
            await browser.page.evaluate(r"""() => {
              const phone = document.querySelector('#normal-input');
              phone.addEventListener('input', () => {
                const d = phone.value.replace(/\D/g, '');
                if (d.length === 11) phone.value = d.slice(0,3)+' '+d.slice(3,7)+' '+d.slice(7);
              });
            }""")
            with patch('dyc.sms_browser.SMS_RESPONSE_SECONDS', .2):
                result = await browser.send(PHONE)
            self.assertEqual(result['outcome'], 'accepted')
            self.assertEqual(self.sends, 1)

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

    async def test_gap_during_send_stops_without_solving_or_resending(self):
        async with self.browser() as browser:
            await browser.page.locator('#terms').check()
            await browser.page.evaluate("""() => {
              document.querySelector('#douyin_login_comp_button_input_id span')
                .addEventListener('click', () => challenge(), {once: true});
            }""")
            browser.auto_step = AsyncMock(side_effect=lambda: None)
            with patch('dyc.sms_browser.SMS_RESPONSE_SECONDS', .1):
                with self.assertRaisesRegex(Stop, 'challenge_detected'):
                    await browser.send(PHONE)
            browser.auto_step.assert_not_awaited()
            self.assertEqual(self.sends, 1)
