from types import SimpleNamespace
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from media_dl.asr import format_srt
from media_dl.__main__ import process


class AsrTests(unittest.TestCase):
    def test_srt(self):
        segments = [SimpleNamespace(start=1.25, end=62.005, text=' 你好 '), SimpleNamespace(start=3600, end=3601, text='世界')]
        self.assertEqual(format_srt(segments), '1\n00:00:01,250 --> 00:01:02,005\n你好\n\n2\n01:00:00,000 --> 01:00:01,000\n世界\n\n')

    def test_cpu_configuration(self):
        from media_dl.asr import transcribe
        dependency = MagicMock()
        dependency.WhisperModel.return_value.transcribe.return_value = ([], None)
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {'faster_whisper': dependency}):
            transcribe(Path(directory) / 'video.mp4', Path(directory) / 'out.srt', 'base')
        dependency.WhisperModel.assert_called_once_with('base', device='cpu', compute_type='int8')

    def test_optional_dependency(self):
        with tempfile.TemporaryDirectory() as directory, patch('media_dl.__main__.douyin.fetch', return_value={'play_url': 'https://example.com/v'}), patch('media_dl.__main__.download', return_value='123.mp4'), patch('media_dl.asr.transcribe', side_effect=ImportError):
            result = process('https://www.douyin.com/video/123', Path(directory), True)
        self.assertEqual(result['video_file'], '123.mp4')
        self.assertEqual(result['subtitle_source'], 'none')
        self.assertIn('faster-whisper', result['error'])
