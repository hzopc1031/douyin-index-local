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
            for status in ('need_scan', 'logged_in', 'expired', 'error'):
                xhs.write_status(status)
                path = Path(directory) / 'xhs-login.status'
                self.assertEqual(path.read_text().strip(), status)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                xhs.write_status('test-cookie')
            self.assertNotIn('test-cookie', path.read_text())

    def test_login_success_and_timeout(self):
        for success in (True, False):
            with self.subTest(success=success), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                page = MagicMock()
                code = MagicMock()
                statuses = []
                with patch.object(xhs, 'RUN_DIR', root), patch.object(xhs, 'profile_path', return_value=root / 'profile'), patch.object(xhs, 'playwright') as browser, patch.object(xhs, 'qr', return_value=code), patch.object(xhs, 'visible', return_value=None), patch.object(xhs, 'logged_in', side_effect=[False, False, success]), patch.object(xhs.time, 'monotonic', side_effect=[0, 1, 2, 601]), patch.object(xhs, 'write_status', side_effect=lambda value: statuses.append(value)):
                    context = browser.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
                    context.pages = [page]
                    result = xhs.login()
                self.assertEqual(statuses, ['need_scan', 'logged_in' if success else 'expired'])
                self.assertEqual(result, 0 if success else 1)
                self.assertEqual((root / 'profile').stat().st_mode & 0o777, 0o700)
                self.assertEqual((root / 'xhs-qr.png').stat().st_mode & 0o777, 0o600)
                context.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
