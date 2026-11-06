"""Offline hotspot QR extraction checks; no real login data is stored."""
import unittest
from io import BytesIO
from pathlib import Path

try:
    import zxingcpp
    from PIL import Image, ImageDraw
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

    def test_local_user_images(self):
        root = Path('/workspace/dev/prompts')
        if not (root / 'H47-served-ok.png').exists():
            self.skipTest('local user images unavailable')
        for name in ('H47-served-ok.png', 'H47-served-dense.png'):
            with self.subTest(name=name):
                self.assertIsNotNone(extract_topic_qr((root / name).read_bytes()))
        self.assertIsNone(extract_topic_qr((root / 'H45-douhot-login.png').read_bytes()))
