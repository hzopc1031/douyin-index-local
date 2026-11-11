import unittest
from unittest.mock import patch
from media_dl import douyin


class DouyinTests(unittest.TestCase):
    def test_urls(self):
        for url in ('https://www.douyin.com/video/123', 'https://www.douyin.com/note/123',
                    'https://www.iesdouyin.com/share/video/123/'):
            self.assertEqual(douyin.resolve_id(url), '123')
        with self.assertRaises(ValueError):
            douyin.video_id('https://evil.test/video/123')

    def test_short_url(self):
        with patch('media_dl.douyin.urlopen') as opened:
            opened.return_value.__enter__.return_value.geturl.return_value = 'https://www.douyin.com/video/123'
            self.assertEqual(douyin.resolve_id('https://v.douyin.com/abc/'), '123')

    def test_page(self):
        with open('media_dl/tests/douyin.html', encoding='utf-8') as fixture:
            item = douyin.parse_page(fixture.read())
        self.assertEqual((item['title'], item['author'], item['duration_seconds']), ('标题', '作者', 12))
        self.assertEqual((item['width'], item['height']), (720, 1280))
        self.assertEqual(item['play_url'], 'https://example.com/playwm/video')
