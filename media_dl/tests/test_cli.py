import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from media_dl.__main__ import process


class CLITests(unittest.TestCase):
    def test_proxy_cli(self):
        from media_dl.__main__ import main
        with patch('sys.argv', ['media_dl', 'login', 'xhs', '--proxy', '127.0.0.1:8791']), patch('media_dl.__main__.xiaohongshu.login', return_value=0) as login:
            self.assertEqual(main(), 0)
            login.assert_called_once_with(proxy='127.0.0.1:8791')
        with tempfile.TemporaryDirectory() as directory, patch('sys.argv', ['media_dl', 'https://xhslink.com/a', '--out', directory, '--proxy', '127.0.0.1:8791']), patch('media_dl.__main__.xiaohongshu.fetch', return_value=('id', {}, 'id.mp4')) as fetch:
            self.assertEqual(main(), 0)
            fetch.assert_called_once_with('https://xhslink.com/a', Path(directory), proxy='127.0.0.1:8791')

    def test_metadata_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch('media_dl.__main__.douyin.fetch', return_value={'title': 't', 'play_url': 'https://example.com/v?secret=1'}), patch('media_dl.__main__.download', return_value='123.mp4'):
                result = process('https://www.douyin.com/video/123', out)
            self.assertIsNone(result['error'])
            data = (out / '123.meta.json').read_text()
            self.assertNotIn('secret', data)
            self.assertEqual(json.loads(data)['video_file'], '123.mp4')
            with patch('media_dl.__main__.douyin.fetch', side_effect=ValueError('test-cookie')):
                result = process('https://www.douyin.com/video/123', out)
            self.assertNotIn('test-cookie', json.dumps(result))
            self.assertIsNotNone(result['error'])

    def test_download_fallback(self):
        import io
        from email.message import Message
        from media_dl.__main__ import download

        class Response(io.BytesIO):
            def __init__(self, content, content_type):
                super().__init__(content)
                self.headers = Message()
                self.headers['Content-Type'] = content_type

        with tempfile.TemporaryDirectory() as directory, patch('media_dl.__main__.urlopen', side_effect=[Response(b'<html>' + b'x' * 2000, 'text/html'), Response(b'\0\0\0\x18ftyp' + b'x' * 2000, 'application/octet-stream')]) as opened:
            result = download('https://example.com/playwm/video', Path(directory), '123')
            self.assertEqual(result, '123.mp4')
            self.assertEqual(opened.call_count, 2)
            self.assertTrue((Path(directory) / result).exists())
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
