from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from media_dl import xiaohongshu as xhs
from media_dl.__main__ import process
from media_dl.safety import safe_log


class XhsTests(unittest.TestCase):
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
            for status in ('need_scan', 'scanned', 'logged_in', 'expired', 'error'):
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
            xhs.refresh_qr(MagicMock(), None)
            meta = json.loads((root / 'xhs-qr.meta').read_text()) if (root / 'xhs-qr.meta').exists() else None
            self.assertIsNone(meta)
            xhs.write_meta(120)
            self.assertEqual(set(json.loads((root / 'xhs-qr.meta').read_text())), {'generated_at', 'expires_at'})
            self.assertEqual((root / 'xhs-qr.meta').stat().st_mode & 0o777, 0o600)
            log = (root / 'xhs-login.log').read_text()
            for word in ('二维码生成', '刷新二维码', '状态变化', 'example.com /mobile/login'):
                self.assertIn(word, log)
            for secret in ('synthetic-secret', 'synthetic-code', 'qr_id', 'cookie', 'https://', '?'):
                self.assertNotIn(secret, log.lower())
            self.assertEqual((root / 'xhs-login.log').stat().st_mode & 0o777, 0o600)

    def test_login_requires_session_and_avatar(self):
        context = MagicMock()
        page = MagicMock()
        with patch.object(xhs, 'visible', return_value=object()):
            context.cookies.return_value = []
            self.assertFalse(xhs.logged_in(page, context))
            context.cookies.return_value = [{'name': 'web_session', 'value': 'synthetic'}]
            self.assertTrue(xhs.logged_in(page, context))
        with patch.object(xhs, 'visible', return_value=None):
            self.assertFalse(xhs.logged_in(page, context))
        self.assertEqual(xhs.status_code({'CODE_STATUS': 2}), 2)

    def test_status_and_refresh_in_login_loop(self):
        for expire in ('status', 'time'):
            with self.subTest(expire=expire), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                page = MagicMock()
                page.screenshot.return_value = b'page'
                statuses = []
                times = iter([0, 1, 2, 3, 4, 5, 6, 105, 106, 901, 902])
                def now():
                    return next(times, 902)
                def navigate(*args, **kwargs):
                    callback = page.on.call_args.args[1]
                    response = MagicMock(url='https://example.com/login/qrcode/create')
                    response.json.return_value = {'data': {'url': 'https://example.com/login?qr_id=synthetic'}}
                    callback(response)
                    response = MagicMock(url='https://example.com/api/qrcode/userinfo')
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
                    refresh.assert_called()
                self.assertIn('need_scan', statuses)
                self.assertIn('scanned', statuses)
                self.assertEqual(statuses[-1], 'expired')
                context.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
