import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from media_dl import xiaohongshu
from media_dl.__main__ import process
from media_dl.safety import safe_log


class XhsTests(unittest.TestCase):
    def test_hosts(self):
        for url in ('https://www.xiaohongshu.com/explore/abc123', 'https://www.xiaohongshu.com/discovery/item/abc123', 'https://rednote.com/explore/abc123'):
            self.assertEqual(xiaohongshu.resolve_id(url), 'abc123')
        self.assertTrue(xiaohongshu.is_xhs('https://xhslink.cn/a'))
        self.assertFalse(xiaohongshu.is_xhs('https://xiaohongshu.com.evil/a'))

    def test_cookie_and_page(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cookie'
            path.write_text('test-cookie')
            with patch.dict(os.environ, {'XHS_COOKIE_FILE': str(path)}, clear=True):
                self.assertEqual(xiaohongshu.cookie(), 'test-cookie')
            with patch.dict(os.environ, {}, clear=True), patch('media_dl.xiaohongshu.urlopen') as network:
                result = process('https://xhslink.com/a', Path(directory))
                self.assertIn('需要 Cookie/登录', result['error'])
                network.assert_not_called()
        page = Path('media_dl/tests/xhs.html').read_text()
        item = xiaohongshu.parse_page(page, 'abc123')
        self.assertEqual((item['title'], item['author'], item['duration_seconds']), ('测试标题', '测试作者', 12))
        self.assertEqual(item['play_url'], 'https://example.com/video.mp4')

    def test_redaction(self):
        for message in ('Cookie: test-cookie', 'ttwid=secret', 'Authorization: Bearer token'):
            self.assertNotIn(message.split()[-1], safe_log(message))
        self.assertNotIn('test-cookie', safe_log('failure test-cookie', ['test-cookie']))
