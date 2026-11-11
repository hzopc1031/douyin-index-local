import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from media_dl.__main__ import process


class CLITests(unittest.TestCase):
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
