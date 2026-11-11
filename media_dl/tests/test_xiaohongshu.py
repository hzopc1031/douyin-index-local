from pathlib import Path
import tempfile
import os
import io
import json
import unittest
from unittest.mock import MagicMock, patch
from media_dl import xiaohongshu as xhs
from media_dl.__main__ import process
from media_dl.safety import safe_log


class XhsTests(unittest.TestCase):
    def setUp(self):
        display = patch.dict(os.environ, {'DISPLAY': ':99'})
        display.start()
        self.addCleanup(display.stop)

    def test_login_launch_and_risk_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = MagicMock(url='https://example.com/login')
            page.screenshot.return_value = b'page'
            def locate(target, selector):
                return object() if selector == 'text=薯队长遇到了点小麻烦' else None
            with patch.object(xhs, 'RUN_DIR', root), patch.object(xhs, 'profile_path', return_value=root / 'profile'), patch.object(xhs, 'playwright') as browser, patch.object(xhs, 'visible', side_effect=locate), patch.object(xhs, 'login_container', return_value=MagicMock()), patch.object(xhs, 'logged_in', side_effect=[False, True]), patch.object(xhs, 'refresh_qr') as refresh:
                chromium = browser.return_value.__enter__.return_value.chromium
                context = chromium.launch_persistent_context.return_value
                context.pages = [page]
                self.assertEqual(xhs.login(), 0)
                self.assertEqual(chromium.launch_persistent_context.call_args.kwargs, {'headless': False, 'args': ['--disable-blink-features=AutomationControlled']})
                self.assertIn('navigator', context.add_init_script.call_args.args[0])
                self.assertIn('webdriver', context.add_init_script.call_args.args[0])
                refresh.assert_called_once()
                self.assertIn('登录页风控提示，将重试', (root / 'xhs-login.log').read_text())

    def test_login_retries_when_create_does_not_arrive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = MagicMock(url='https://example.com/login')
            page.screenshot.return_value = b'page'
            clock = [0]
            page.wait_for_timeout.side_effect = lambda milliseconds: clock.__setitem__(0, clock[0] + milliseconds / 1000)
            with patch.object(xhs, 'RUN_DIR', root), patch.object(xhs, 'profile_path', return_value=root / 'profile'), patch.object(xhs, 'playwright') as browser, patch.object(xhs, 'visible', return_value=None), patch.object(xhs, 'login_container', return_value=MagicMock()), patch.object(xhs, 'logged_in', side_effect=lambda *args: clock[0] >= 27), patch.object(xhs, 'qr', return_value=[]), patch.object(xhs, 'refresh_qr') as refresh, patch.object(xhs.time, 'monotonic', side_effect=lambda: clock[0]):
                browser.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value.pages = [page]
                self.assertEqual(xhs.login(), 0)
                refresh.assert_called_once()
                self.assertIn('登录码等待超时，将重试', (root / 'xhs-login.log').read_text())

    def test_international_entry_after_refresh_for_risk_or_missing_create(self):
        for risk in (False, True):
            with self.subTest(risk=risk), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                page = MagicMock(url='https://www.xiaohongshu.com/login')
                page.screenshot.return_value = b'page'
                clock = [0]
                button = MagicMock()
                page.wait_for_timeout.side_effect = lambda ms: clock.__setitem__(0, clock[0] + ms / 1000)
                def locate(target, selector):
                    return object() if risk and selector == 'text=薯队长遇到了点小麻烦' else None
                with patch.object(xhs, 'RUN_DIR', root), patch.object(xhs, 'profile_path', return_value=root / 'profile'), patch.object(xhs, 'playwright') as browser, patch.object(xhs, 'visible', side_effect=locate), patch.object(xhs, 'login_container', return_value=MagicMock()), patch.object(xhs, 'login_button', return_value=button), patch.object(xhs, 'logged_in', side_effect=lambda *args: button.click.called), patch.object(xhs, 'qr', return_value=[]), patch.object(xhs, 'refresh_qr') as refresh, patch.object(xhs.time, 'monotonic', side_effect=lambda: clock[0]):
                    browser.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value.pages = [page]
                    self.assertEqual(xhs.login(), 0)
                    refresh.assert_called_once()
                self.assertEqual([call.args[0] for call in page.goto.call_args_list], ['https://www.xiaohongshu.com/login', 'https://www.rednote.com/explore'])
                button.click.assert_called_once()
                log = (root / 'xhs-login.log').read_text()
                self.assertIn('改走国际站入口重试', log)
                self.assertNotIn('https://', log)

    def test_login_display_starts_and_cleans_up_xvfb(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(xhs.os, 'pipe', return_value=(10, 11)), patch.object(xhs.os, 'close'), patch.object(xhs.os, 'fdopen', return_value=io.StringIO('123\n')), patch.object(xhs.subprocess, 'Popen') as popen:
            with xhs.login_display():
                self.assertEqual(os.environ['DISPLAY'], ':123')
            self.assertNotIn('DISPLAY', os.environ)
            self.assertEqual(popen.call_args.args[0][0], 'Xvfb')
            popen.return_value.terminate.assert_called_once()
            popen.return_value.wait.assert_called_once()

    def test_hosts_and_fixture(self):
        for url in ('https://www.xiaohongshu.com/explore/abc123', 'https://www.xiaohongshu.com/discovery/item/abc123', 'https://rednote.com/explore/abc123'):
            self.assertEqual(xhs.resolve_id(url), 'abc123')
        self.assertTrue(xhs.is_xhs('https://xhslink.cn/a'))
        self.assertFalse(xhs.is_xhs('https://xiaohongshu.com.evil/a'))
        page = Path('media_dl/tests/xhs.html').read_text().replace('"title":"测试标题"', '"unused":undefined,"title":"测试标题"')
        item = xhs.parse_page(page, 'abc123')
        self.assertEqual((item['title'], item['author'], item['duration_seconds'], item['play_url']),
                         ('测试标题', '测试作者', 12, 'https://example.com/video.mp4'))

    def test_no_profile_or_session_does_not_navigate(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(xhs, 'profile_path', return_value=out / 'missing'), patch.object(xhs, 'playwright') as browser:
                result = process('https://xhslink.com/a', out)
                self.assertEqual(result['error'], xhs.LOGIN_REQUIRED)
                browser.assert_not_called()
            with patch.object(xhs, 'profile_path', return_value=out), patch.object(xhs, 'playwright') as browser:
                context = browser.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
                context.cookies.return_value = []
                result = process('https://xhslink.com/a', out)
                self.assertEqual(result['error'], xhs.LOGIN_REQUIRED)
                context.new_page.assert_not_called()

    def test_redaction_and_failure(self):
        for message in ('Cookie: test-cookie', 'ttwid=secret', 'Authorization: Bearer token'):
            self.assertNotIn(message.split()[-1], safe_log(message))
        with tempfile.TemporaryDirectory() as directory, patch.object(xhs, 'fetch', side_effect=RuntimeError('test-cookie')):
            result = process('https://www.xiaohongshu.com/explore/abc123', Path(directory))
            self.assertNotIn('test-cookie', str(result))
            self.assertNotIn('test-cookie', next(Path(directory).glob('*.meta.json')).read_text())

    def test_authenticated_download_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(xhs, 'profile_path', return_value=Path(directory)), patch.object(xhs, 'playwright') as browser, patch.object(xhs, 'visible', return_value=None):
            context = browser.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
            context.cookies.return_value = [{'name': 'web_session', 'value': 'test-cookie'}]
            page = MagicMock()
            page.url = 'https://www.xiaohongshu.com/explore/abc123'
            page.content.return_value = Path('media_dl/tests/xhs.html').read_text()
            context.pages = [page]
            response = context.request.get.return_value
            response.ok = True
            response.headers = {'content-type': 'video/mp4'}
            response.body.return_value = b'\0\0\0\x18ftyp' + b'x' * 1100
            result = process('https://xhslink.com/a?token=test-cookie', Path(directory))
            self.assertIsNone(result['error'])
            self.assertEqual(result['title'], '测试标题')
            self.assertTrue((Path(directory) / 'abc123.mp4').exists())
            self.assertNotIn('test-cookie', (Path(directory) / 'abc123.meta.json').read_text())
            context.close.assert_called_once()

    def test_status_values(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(xhs, 'RUN_DIR', Path(directory)):
            for status in ('need_scan', 'scanned', 'verify', 'logged_in', 'expired', 'error'):
                xhs.write_status(status)
                path = Path(directory) / 'xhs-login.status'
                self.assertEqual(path.read_text().strip(), status)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                xhs.write_status('test-cookie')
            self.assertNotIn('test-cookie', path.read_text())

    def test_qr_validation(self):
        for payload in ('https://rednote.onelink.me/example', 'https://example.com/?af_xp=qr',
                        'https://example.com/download/app?login=1'):
            self.assertFalse(xhs.qr_valid(payload, True))
        for payload in ('https://example.com/scan?qr_id=synthetic', 'https://example.com/login?qrId=synthetic'):
            self.assertTrue(xhs.qr_valid(payload, True))
        self.assertFalse(xhs.qr_valid(None, True))
        self.assertFalse(xhs.qr_valid('https://example.com/mobile/login', True))
        self.assertFalse(xhs.qr_valid(None, False))
        self.assertFalse(xhs.qr_valid('https://example.com/login?token=synthetic', False))

    def test_qr_only_searches_login_container(self):
        page = MagicMock()
        container = MagicMock()
        download = MagicMock()
        code = MagicMock()
        page.locator.return_value.count.return_value = 1
        page.locator.return_value.nth.return_value = container
        container.is_visible.return_value = True
        container.locator.return_value.count.return_value = 2
        container.locator.return_value.nth.side_effect = [download, code]
        download.is_visible.return_value = True
        download.evaluate.return_value = False
        code.is_visible.return_value = True
        code.evaluate.return_value = True
        self.assertEqual(list(xhs.qr(xhs.login_container(page))), [code])
        self.assertNotIn('qr', page.locator.call_args.args[0])
        self.assertIn('qr', container.locator.call_args.args[0])

    def test_screenshot_validation_and_permissions(self):
        import io
        from PIL import Image
        import zxingcpp

        image = Image.new('RGB', (30, 30), 'white')
        data = io.BytesIO()
        image.save(data, format='PNG')
        code = MagicMock()
        code.screenshot.return_value = data.getvalue()
        with tempfile.TemporaryDirectory() as directory, patch.object(xhs, 'RUN_DIR', Path(directory)):
            path = Path(directory) / 'qr.png'
            with patch.object(zxingcpp, 'read_barcode', return_value=MagicMock(text='https://onelink.me/?af_xp=qr')):
                self.assertFalse(xhs.save_qr(code, path))
                self.assertFalse(path.exists())
            with patch.object(zxingcpp, 'read_barcode', return_value=None):
                self.assertFalse(xhs.save_qr(code, path))
            self.assertFalse(path.exists())

    def test_synthetic_qr_decoded_without_exposing_payload(self):
        import contextlib
        import io
        from PIL import Image
        import zxingcpp

        for payload, accepted in (('https://onelink.me/path?af_xp=qr', False),
                                  ('https://example.com/login?qr_id=synthetic-secret', True)):
            barcode = zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.QRCode)
            source = zxingcpp.write_barcode_to_image(barcode, scale=4)
            height, width = memoryview(source).shape
            image = Image.frombytes('L', (width, height), bytes(source))
            data = io.BytesIO()
            image.save(data, format='PNG')
            code = MagicMock()
            code.screenshot.return_value = data.getvalue()
            output = io.StringIO()
            with tempfile.TemporaryDirectory() as directory, patch.object(xhs, 'RUN_DIR', Path(directory)), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = xhs.save_qr(code, Path(directory) / 'qr.png')
                self.assertEqual(result, accepted)
                self.assertNotIn('synthetic-secret', str(result) + output.getvalue())

    def test_native_login_qr_preferred_and_id_checked(self):
        import qrcode
        from PIL import Image
        import zxingcpp

        with tempfile.TemporaryDirectory() as directory, patch.object(xhs, 'RUN_DIR', Path(directory)):
            root = Path(directory)
            page, container, code = MagicMock(), MagicMock(), MagicMock()
            page.screenshot.return_value = b'page'
            image = io.BytesIO()
            qrcode.make('https://example.com/login?qr_id=native-secret').save(image, format='PNG')
            code.screenshot.return_value = image.getvalue()
            data = {'url': 'https://example.com/login?qr_id=native-secret'}
            with patch.object(xhs, 'qr', return_value=[code]), patch.object(xhs, 'save_url_qr') as fallback:
                self.assertEqual(xhs.save_login_qr(page, container, data, 'native-secret', root / 'xhs-qr.png'), 'native')
                fallback.assert_not_called()
                self.assertIn('native-secret', zxingcpp.read_barcode(Image.open(root / 'xhs-qr.png')).text)
                self.assertFalse(xhs.save_page_qr(page, container, root / 'other.png', 'different'))
            self.assertEqual((root / 'xhs-qr.png').stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / 'xhs-page.png').read_bytes(), b'page')
            self.assertNotIn('native-secret', (root / 'xhs-login.log').read_text())

    def test_security_qr_refresh_capture_and_wait_for_session(self):
        import qrcode
        from PIL import Image
        import zxingcpp

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = MagicMock(url='https://www.rednote.com/explore')
            page.screenshot.return_value = b'security-page'
            security, expired, code = MagicMock(), MagicMock(), MagicMock()
            image = io.BytesIO()
            qrcode.make('security-secret').save(image, format='PNG')
            code.screenshot.return_value = image.getvalue()
            clock = [0]
            page.wait_for_timeout.side_effect = lambda ms: clock.__setitem__(0, clock[0] + ms / 1000)
            def locate(target, selector):
                if target is security and 'Expired' in selector and not expired.click.called:
                    return expired
                return None
            def container(_):
                if clock[0] == 8:
                    self.assertEqual((root / 'xhs-login.status').read_text().strip(), 'verify')
                    self.assertEqual(json.loads((root / 'xhs-qr.meta').read_text())['kind'], 'verify')
                return security if clock[0] < 8 else None
            with patch.object(xhs, 'RUN_DIR', root), patch.object(xhs, 'profile_path', return_value=root / 'profile'), patch.object(xhs, 'playwright') as browser, patch.object(xhs, 'security_container', side_effect=container), patch.object(xhs, 'visible', side_effect=locate), patch.object(xhs, 'login_container', return_value=MagicMock()), patch.object(xhs, 'qr', return_value=[code]), patch.object(xhs, 'refresh_qr') as refresh, patch.object(xhs.time, 'monotonic', side_effect=lambda: clock[0]):
                context = browser.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
                context.pages = [page]
                context.cookies.side_effect = lambda *args: ([{'name': 'web_session', 'value': 'new-secret'}] if clock[0] >= 10 else [])
                self.assertEqual(xhs.login(), 0)
                refresh.assert_not_called()
                expired.click.assert_called_once()
                context.close.assert_called_once()
                page.goto.assert_called_once()
            self.assertEqual(zxingcpp.read_barcode(Image.open(root / 'xhs-qr.png')).text, 'security-secret')
            self.assertEqual((root / 'xhs-page.png').read_bytes(), b'security-page')
            self.assertEqual((root / 'xhs-login.status').read_text().strip(), 'logged_in')
            log = (root / 'xhs-login.log').read_text()
            self.assertIn('页面出现安全验证，已截取页面原生码', log)
            for file in ('xhs-login.log', 'xhs-login.status', 'xhs-qr.meta'):
                for secret in ('security-secret', 'new-secret', 'qr_id', 'https://', 'cookie'):
                    self.assertNotIn(secret, (root / file).read_text().lower())

    def test_security_loading_and_decoded_payload_validation(self):
        from PIL import Image
        import zxingcpp

        with tempfile.TemporaryDirectory() as directory, patch.object(xhs, 'RUN_DIR', Path(directory)):
            path = Path(directory) / 'xhs-qr.png'
            path.write_bytes(b'old')
            container, code, page = MagicMock(), MagicMock(), MagicMock()
            container.locator.return_value.count.return_value = 1
            container.locator.return_value.nth.return_value = code
            code.is_visible.return_value = True
            code.evaluate.return_value = False
            self.assertFalse(xhs.save_page_qr(page, container, path, security=True))
            self.assertEqual(path.read_bytes(), b'old')
            code.screenshot.assert_not_called()
            image = Image.new('RGB', (10, 10), 'white')
            with patch.object(zxingcpp, 'read_barcode', return_value=MagicMock(text='https://onelink.me/download')):
                self.assertFalse(xhs.save_image(image, path, security=True))
            self.assertEqual(path.read_bytes(), b'old')
            with patch.object(zxingcpp, 'read_barcode', return_value=MagicMock(text='verification-secret', format=None)):
                self.assertTrue(xhs.save_image(image, path, expected_id='ignored', security=True))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('verification-secret', (Path(directory) / 'xhs-login.log').read_text())

    def test_security_text_selectors(self):
        page = MagicMock()
        with patch.object(xhs, 'visible', return_value=None) as find:
            self.assertIsNone(xhs.security_container(page))
        selectors = ' '.join(call.args[1] for call in find.call_args_list)
        for text in ('Security Verification', 'Scan with logged-in', '安全验证', '账户安全', 'Expired', 'QR code expires'):
            self.assertIn(text, selectors)

    def test_create_response_meta_and_redacted_log(self):
        import json
        from PIL import Image
        import zxingcpp

        with tempfile.TemporaryDirectory() as directory, patch.object(xhs, 'RUN_DIR', Path(directory)):
            root = Path(directory)
            for key in ('qr_id', 'qrId'):
                payload = 'https://example.com/mobile/login?' + key + '=synthetic-secret'
                response = MagicMock(url='https://example.com/api/sns/web/v1/login/qrcode/create')
                response.json.return_value = {'data': {'url': payload, 'qr_id': 'synthetic-secret', 'code': 'synthetic-code'}}
                events = []
                xhs.response_data(response, events)
                self.assertTrue(xhs.save_url_qr(events[0][1]['url'], root / 'xhs-qr.png'))
                decoded = zxingcpp.read_barcode(Image.open(root / 'xhs-qr.png'))
                self.assertEqual(decoded.text, payload)
            xhs.write_meta(120)
            xhs.log_event('二维码生成')
            xhs.set_status('need_scan')
            page = MagicMock(url='https://example.com/explore')
            page.screenshot.return_value = b'page'
            xhs.refresh_qr(page, None)
            meta = json.loads((root / 'xhs-qr.meta').read_text()) if (root / 'xhs-qr.meta').exists() else None
            self.assertIsNone(meta)
            xhs.write_meta(120)
            meta = json.loads((root / 'xhs-qr.meta').read_text())
            self.assertEqual(set(meta), {'kind', 'generated_at', 'expires_at'})
            self.assertEqual(meta['kind'], 'login')
            self.assertEqual((root / 'xhs-qr.meta').stat().st_mode & 0o777, 0o600)
            log = (root / 'xhs-login.log').read_text()
            for word in ('二维码生成', '刷新二维码', '状态变化', '二维码校验通过'):
                self.assertIn(word, log)
            for secret in ('synthetic-secret', 'synthetic-code', 'qr_id', 'cookie', 'https://', '?'):
                self.assertNotIn(secret, log.lower())
            self.assertEqual((root / 'xhs-login.log').stat().st_mode & 0o777, 0o600)

    def test_login_requires_session_and_confirmation(self):
        context = MagicMock()
        page = MagicMock()
        context.cookies.return_value = []
        self.assertFalse(xhs.logged_in(page, context, True))
        context.cookies.return_value = [{'name': 'web_session', 'value': 'synthetic'}]
        self.assertFalse(xhs.logged_in(page, context))
        self.assertTrue(xhs.logged_in(page, context, True))
        self.assertEqual(context.cookies.call_args.args[0], ['https://xiaohongshu.com', 'https://www.xiaohongshu.com', 'https://rednote.com', 'https://www.rednote.com'])
        self.assertEqual(xhs.status_code({'CODE_STATUS': 2}), 2)

    def test_session_on_rednote_only(self):
        context = MagicMock()
        context.cookies.side_effect = lambda urls: ([{'name': 'web_session', 'value': 'synthetic'}]
                                                    if 'https://www.rednote.com' in urls else [])
        self.assertTrue(xhs.has_session(context))

    def test_identity_response_keeps_only_authenticated_boolean(self):
        response = MagicMock(url='https://www.rednote.com/api/sns/web/v2/user/me')
        events = []
        response.json.return_value = {'success': True, 'data': {'guest': True}}
        xhs.response_data(response, events)
        self.assertEqual(events, [])
        response.json.return_value = {'success': True, 'data': {'guest': False, 'user_id': 'synthetic'}}
        xhs.response_data(response, events)
        self.assertEqual(events, [('identity', True)])
        for host in ('webapi.rednote.com', 'edith.xiaohongshu.com'):
            response.url = 'https://' + host + '/api/sns/web/v2/user/me'
            xhs.response_data(response, events)
        self.assertEqual(events, [('identity', True)] * 3)
        for host in ('rednote.com.evil', 'evilrednote.com', 'evilxiaohongshu.com'):
            response.url = 'https://' + host + '/api/sns/web/v2/user/me'
            xhs.response_data(response, events)
        self.assertEqual(events, [('identity', True)] * 3)

    def test_scan_waits_without_refresh_and_success_waits_for_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = MagicMock(url='https://example.com/login')
            page.screenshot.return_value = b'page'
            clock = [0]
            waits = [0]
            statuses = []
            def emit(path, data):
                response = MagicMock(url='https://example.com/' + path)
                response.json.return_value = {'data': data}
                response.request = MagicMock(url='https://www.rednote.com/api/qrcode/userinfo?qr_id=synthetic', method='GET', post_data=None, headers={})
                page.on.call_args.args[1](response)
            def navigate(*args, **kwargs):
                emit('login/qrcode/create', {'url': 'https://example.com/login?qr_id=synthetic', 'ttl': 1})
                emit('login/qrcode/status', {'codeStatus': 1})
            def wait(milliseconds):
                clock[0] += milliseconds / 1000
                if milliseconds == 1000:
                    waits[0] += 1
                    if waits[0] == 3:
                        emit('login/qrcode/status', {'code_status': 2})
                    if waits[0] == 5:
                        context.cookies.return_value = [{'name': 'web_session', 'value': 'synthetic'}]
            page.goto.side_effect = navigate
            page.wait_for_timeout.side_effect = wait
            with patch.object(xhs, 'RUN_DIR', root), patch.object(xhs, 'profile_path', return_value=root / 'profile'), patch.object(xhs, 'playwright') as browser, patch.object(xhs, 'login_container', return_value=None), patch.object(xhs, 'visible', return_value=None), patch.object(xhs, 'save_url_qr', return_value=True), patch.object(xhs.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(xhs, 'write_status', side_effect=statuses.append), patch.object(xhs, 'refresh_qr') as refresh:
                context = browser.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
                context.pages = [page]
                context.cookies.return_value = []
                self.assertEqual(xhs.login(), 0)
                refresh.assert_not_called()
                context.close.assert_called_once()
            self.assertEqual(statuses, ['need_scan', 'scanned', 'logged_in'])
            self.assertGreaterEqual(waits[0], 5)

    def test_status_and_refresh_in_login_loop(self):
        for expire in ('status', 'time'):
            with self.subTest(expire=expire), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                page = MagicMock()
                page.url = 'https://example.com/explore'
                page.screenshot.return_value = b'page'
                statuses = []
                times = iter([0, 1, 2, 3, 4, 5, 6, 105, 106, 1801, 1802])
                def now():
                    return next(times, 1802)
                def navigate(*args, **kwargs):
                    callback = page.on.call_args.args[1]
                    response = MagicMock(url='https://example.com/login/qrcode/create')
                    response.json.return_value = {'data': {'url': 'https://example.com/login?qr_id=synthetic'}}
                    callback(response)
                    response = MagicMock(url='https://example.com/api/qrcode/userinfo')
                    response.request = MagicMock(url='https://www.rednote.com/api/qrcode/userinfo?qr_id=synthetic', method='GET', post_data=None, headers={})
                    response.json.return_value = {'data': {'codeStatus': 1}}
                    callback(response)
                    if expire == 'status':
                        response.json.return_value = {'data': {'code_status': 3}}
                        callback(response)
                page.goto.side_effect = navigate
                with patch.object(xhs, 'RUN_DIR', root), patch.object(xhs, 'profile_path', return_value=root / 'profile'), patch.object(xhs, 'playwright') as browser, patch.object(xhs, 'login_container', return_value=MagicMock()), patch.object(xhs, 'visible', return_value=None), patch.object(xhs, 'logged_in', return_value=False), patch.object(xhs, 'qr', return_value=[]), patch.object(xhs.time, 'monotonic', side_effect=now), patch.object(xhs, 'write_status', side_effect=statuses.append), patch.object(xhs, 'refresh_qr') as refresh:
                    context = browser.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
                    context.pages = [page]
                    self.assertEqual(xhs.login(), 1)
                    if expire == 'status':
                        refresh.assert_called()
                    else:
                        refresh.assert_not_called()
                self.assertIn('need_scan', statuses)
                self.assertIn('scanned', statuses)
                self.assertEqual(statuses[-1], 'expired')
                context.close.assert_called_once()

    def test_login_retries_missing_container_and_handles_later_create(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = MagicMock()
            page.url = 'https://example.com/explore'
            page.screenshot.return_value = b'page'
            button = MagicMock()
            container = MagicMock()
            statuses = []
            def click():
                response = MagicMock(url='https://example.com/login/qrcode/create')
                response.json.return_value = {'data': {'url': 'https://example.com/login?qr_id=synthetic'}}
                page.on.call_args.args[1](response)
            button.click.side_effect = click
            page.goto.side_effect = lambda *args, **kwargs: self.assertTrue(page.on.called)
            with patch.object(xhs, 'RUN_DIR', root), patch.object(xhs, 'profile_path', return_value=root / 'profile'), patch.object(xhs, 'playwright') as browser, patch.object(xhs, 'login_container', side_effect=[None, container]), patch.object(xhs, 'login_button', return_value=button), patch.object(xhs, 'visible', return_value=None), patch.object(xhs, 'logged_in', side_effect=[False, False, True]), patch.object(xhs, 'qr', return_value=[]), patch.object(xhs, 'save_url_qr', return_value=True) as save, patch.object(xhs.time, 'monotonic', return_value=1), patch.object(xhs, 'write_status', side_effect=statuses.append):
                context = browser.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
                context.pages = [page]
                self.assertEqual(xhs.login(), 0)
                save.assert_called_once()
                self.assertEqual(statuses, ['need_scan', 'logged_in'])
                self.assertIn('未找到登录容器，将重试', (root / 'xhs-login.log').read_text())

    def test_login_page_waits_for_later_create_without_modal(self):
        for body_visible in (False, True):
            with self.subTest(body_visible=body_visible), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                page = MagicMock()
                page.url = 'https://example.com/login?synthetic=1'
                page.screenshot.return_value = b'page'
                body, dismiss = MagicMock(), MagicMock()
                statuses = []
                def locate(target, selector):
                    if selector == 'body' and body_visible:
                        return body
                    if selector == 'text="我知道了"' and not dismiss.click.called:
                        return dismiss
                    return None
                def wait(milliseconds):
                    if milliseconds == 1000:
                        response = MagicMock(url='https://example.com/login/qrcode/create')
                        response.json.return_value = {'data': {'url': 'https://example.com/login?qr_id=synthetic'}}
                        page.on.call_args.args[1](response)
                page.wait_for_timeout.side_effect = wait
                with patch.object(xhs, 'RUN_DIR', root), patch.object(xhs, 'profile_path', return_value=root / 'profile'), patch.object(xhs, 'playwright') as browser, patch.object(xhs, 'visible', side_effect=locate), patch.object(xhs, 'logged_in', side_effect=[False, False, True]), patch.object(xhs, 'qr', return_value=[]), patch.object(xhs, 'save_url_qr', return_value=True) as save, patch.object(xhs.time, 'monotonic', return_value=1), patch.object(xhs, 'write_status', side_effect=statuses.append):
                    context = browser.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
                    context.pages = [page]
                    self.assertEqual(xhs.login(), 0)
                    save.assert_called_once()
                page.goto.assert_called_once_with('https://www.xiaohongshu.com/login', wait_until='domcontentloaded')
                page.wait_for_timeout.assert_any_call(5000)
                dismiss.click.assert_called_once()
                self.assertEqual(statuses, ['need_scan', 'logged_in'])

    def test_no_expiry_signal_does_not_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = MagicMock(url='https://www.rednote.com/login')
            page.screenshot.return_value = b'page'
            clock = [0]
            def navigate(*args, **kwargs):
                response = MagicMock(url='https://www.rednote.com/login/qrcode/create')
                response.json.return_value = {'data': {'url': 'https://example.com/login?qr_id=synthetic'}}
                page.on.call_args.args[1](response)
            page.goto.side_effect = navigate
            page.wait_for_timeout.side_effect = lambda ms: clock.__setitem__(0, clock[0] + ms / 1000)
            with patch.object(xhs, 'RUN_DIR', root), patch.object(xhs, 'profile_path', return_value=root / 'profile'), patch.object(xhs, 'playwright') as browser, patch.object(xhs, 'login_container', return_value=MagicMock()), patch.object(xhs, 'visible', return_value=None), patch.object(xhs, 'qr', return_value=[]), patch.object(xhs, 'save_url_qr', return_value=True), patch.object(xhs.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(xhs, 'refresh_qr') as refresh:
                browser.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value.pages = [page]
                self.assertEqual(xhs.login(), 1)
                refresh.assert_not_called()
                self.assertEqual(clock[0], 1800)
                self.assertEqual((root / 'xhs-login.status').read_text().strip(), 'expired')

    def test_follow_page_poll_and_confirm_without_leaking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = MagicMock(url='https://www.rednote.com/login')
            page.screenshot.return_value = b'page'
            clock = [0]
            statuses = []
            saved = []
            def emit(path, data, request=None):
                response = MagicMock(url='https://www.rednote.com/' + path)
                response.json.return_value = {'data': data}
                if request:
                    response.request = request
                page.on.call_args.args[1](response)
            def navigate(*args, **kwargs):
                for name in ('first-secret', 'second-secret'):
                    emit('login/qrcode/create', {'url': 'https://example.com/login?qr_id=' + name, 'code': 'code-secret'})
                request = MagicMock(url='https://www.rednote.com/api/qrcode/userinfo?qrId=second-secret&code=code-secret',
                                    method='GET', post_data=None, headers={})
                emit('api/qrcode/userinfo', {'codeStatus': 0}, request)
            page.goto.side_effect = navigate
            page.wait_for_timeout.side_effect = lambda ms: clock.__setitem__(0, clock[0] + ms / 1000)
            def poll(page, request):
                self.assertEqual(request['id'], 'second-secret')
                return {'codeStatus': 1 if clock[0] < 130 else 2}
            def save(url, path):
                saved.append(url)
                return True
            with patch.object(xhs, 'RUN_DIR', root), patch.object(xhs, 'profile_path', return_value=root / 'profile'), patch.object(xhs, 'playwright') as browser, patch.object(xhs, 'login_container', return_value=MagicMock()), patch.object(xhs, 'visible', return_value=None), patch.object(xhs, 'qr', return_value=[]), patch.object(xhs, 'save_url_qr', side_effect=save), patch.object(xhs, 'poll_status', side_effect=poll), patch.object(xhs.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(xhs, 'refresh_qr') as refresh:
                context = browser.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
                context.pages = [page]
                context.cookies.side_effect = lambda *args: ([{'name': 'web_session', 'value': 'new-session'}] if clock[0] >= 132 else [{'name': 'web_session', 'value': 'old-session'}])
                self.assertEqual(xhs.login(), 0)
                refresh.assert_not_called()
                context.close.assert_called_once()
            self.assertEqual(len(saved), 2)
            self.assertIn('second-secret', saved[-1])
            log = (root / 'xhs-login.log').read_text()
            self.assertIn('状态轮询与当前码不一致', log)
            self.assertIn('状态码: 0', log)
            self.assertIn('状态码: 1', log)
            self.assertIn('已改为跟随页面正在轮询的码', log)
            self.assertIn('scanned', log)
            self.assertEqual((root / 'xhs-login.status').read_text().strip(), 'logged_in')
            for file in ('xhs-login.log', 'xhs-login.status', 'xhs-qr.meta'):
                content = (root / file).read_text().lower()
                for secret in ('first-secret', 'second-secret', 'code-secret', 'session', 'cookie', 'qr_id', 'https://'):
                    self.assertNotIn(secret, content)

    def test_status_request_body_and_untrusted_origin(self):
        request = MagicMock(url='https://www.rednote.com/api/qrcode/userinfo', method='POST',
                            post_data='{"qr_id":"synthetic", "code":"synthetic-code"}',
                            headers={'content-type': 'application/json'})
        self.assertEqual(xhs.status_request(request)['id'], 'synthetic')
        request.url = 'https://rednote.com.evil/api/qrcode/userinfo'
        self.assertIsNone(xhs.status_request(request))

    def test_login_button_selectors_include_sidebar_and_exclude_submit(self):
        page = MagicMock()
        with patch.object(xhs, 'visible', return_value=object()) as find:
            self.assertIsNotNone(xhs.login_button(page))
        selector = find.call_args.args[1]
        for part in selector.split(', '):
            self.assertIn(':not([class*="submit"])', part)
            self.assertNotIn('header', part)
        self.assertIn('button.login-btn', selector)
        self.assertIn(':text-is("登录")', selector)
        self.assertIn(':text-is("Log in")', selector)

    def test_login_exception_logs_redacted_reason(self):
        for detail in ('navigation failed https://example.com/?qr_id=secret', 'failed Cookie: secret', 'failed qr_id=secret', 'failed url=secret', 'session=secret user_id=secret 手机号=secret'):
            with self.subTest(detail=detail), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with patch.object(xhs, 'RUN_DIR', root), patch.object(xhs, 'profile_path', return_value=root / 'profile'), patch.object(xhs, 'playwright', side_effect=RuntimeError(detail)):
                    self.assertEqual(xhs.login(), 1)
                log = (root / 'xhs-login.log').read_text()
                self.assertIn('RuntimeError', log)
                for sensitive in ('secret', 'cookie', 'qr_id', 'https://', 'url'):
                    self.assertNotIn(sensitive, log.lower())


if __name__ == '__main__':
    unittest.main()
