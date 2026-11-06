"""Offline hotspot QR extraction checks; no real login data is stored."""
import unittest
from io import BytesIO
from pathlib import Path

try:
    import zxingcpp
    from PIL import Image, ImageDraw, ImageOps, ImageChops
except ImportError:
    zxingcpp = None

from topic_qr import extract_topic_qr


@unittest.skipUnless(zxingcpp, 'hub QR dependencies unavailable')
class TopicQrTests(unittest.TestCase):
    def test_synthetic_and_non_qr(self):
        image = Image.open(Path(__file__).parent / 'tests/fixtures/topic-qr-synthetic.png').convert('RGB')
        canvas = Image.new('RGB', (image.width + 100, image.height + 80), 'white')
        canvas.paste(image, (50, 40))
        data = BytesIO()
        canvas.save(data, format='PNG')
        result = extract_topic_qr(data.getvalue())
        self.assertIsNotNone(result)
        self.assertTrue(zxingcpp.read_barcodes(Image.open(BytesIO(result))))
        ImageDraw.Draw(canvas).rectangle((0, 0, canvas.width, canvas.height), fill='white')
        data = BytesIO()
        canvas.save(data, format='PNG')
        self.assertIsNone(extract_topic_qr(data.getvalue()))

    def test_large_logo_and_covered_finders(self):
        image = Image.open(Path(__file__).parent / 'tests/fixtures/topic-qr-synthetic.png').convert('RGB')
        draw = ImageDraw.Draw(image)
        w, h = image.size
        draw.rectangle((w * .3, h * .3, w * .7, h * .7), fill='white')
        data = BytesIO()
        image.save(data, format='PNG')
        self.assertFalse(zxingcpp.read_barcodes(image))
        self.assertIsNotNone(extract_topic_qr(data.getvalue()))
        draw.rectangle((0, 0, w, h), fill=(160, 160, 160))
        data = BytesIO()
        image.save(data, format='PNG')
        self.assertIsNone(extract_topic_qr(data.getvalue()))

    def test_local_desktop_authorization(self):
        from app import Api
        from types import SimpleNamespace
        from unittest.mock import patch
        import base64
        import json
        import threading

        path = Path('/workspace/dev/prompts/H48-desktop-valid.png')
        if not path.exists():
            self.skipTest('local authorization screenshot unavailable')
        class Window:
            native = SimpleNamespace(webview=object())
            def get_current_url(self): return 'https://open.douyin.com/authorize'
            def evaluate_js(self, _):
                return json.dumps({'ready': 'complete', 'text': '使用抖音账号登录 生活服务热点中心'})
        api = Api.__new__(Api)
        api._lock = threading.Lock()
        api._topic_window = Window
        with patch('login_driver.QtDispatcher') as dispatcher:
            dispatcher.return_value.call.return_value = path.read_bytes()
            result = api.topic_login('qr')
        self.assertEqual(result['state'], 'need_scan')
        self.assertTrue(result['qr_available'])
        crop = Image.open(BytesIO(base64.b64decode(result['png_base64'])))
        self.assertGreaterEqual(min(crop.size), 600)
        self.assertLess(max(crop.size) / min(crop.size), 1.05)
        source = Image.open(path).convert('RGB').crop((252, 367, 410, 525))
        expected = ImageOps.expand(source, border=16, fill='white').resize(crop.size, Image.Resampling.NEAREST)
        self.assertIsNone(ImageChops.difference(crop.convert('RGB'), expected).getbbox())

    def test_local_user_images(self):
        root = Path('/workspace/dev/prompts')
        if not (root / 'H47-served-ok.png').exists():
            self.skipTest('local user images unavailable')
        for name in ('H47-served-ok.png', 'H47-served-dense.png'):
            with self.subTest(name=name):
                self.assertIsNotNone(extract_topic_qr((root / name).read_bytes()))
        self.assertIsNone(extract_topic_qr((root / 'H45-douhot-login.png').read_bytes()))
