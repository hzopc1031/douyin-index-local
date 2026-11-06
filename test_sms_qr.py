"""Offline QR extraction tests; the optional user samples remain outside the repository."""
import unittest
from pathlib import Path

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None

from sms_login import extract_frame_qr


@unittest.skipUnless(cv2 is not None and hasattr(cv2, 'QRCodeDetectorAruco'), 'OpenCV QR detector unavailable')
class FrameQrTests(unittest.TestCase):
    def test_synthetic_qr(self):
        image = cv2.QRCodeEncoder_create().encode('synthetic test')
        image = cv2.resize(image, None, fx=5, fy=5, interpolation=cv2.INTER_NEAREST)
        _, png = cv2.imencode('.png', image)
        result = extract_frame_qr(png.tobytes())
        self.assertIsNotNone(result)
        enlarged = cv2.imdecode(np.frombuffer(result, np.uint8), cv2.IMREAD_COLOR)
        self.assertGreaterEqual(min(enlarged.shape[:2]), 600)
        self.assertEqual(cv2.QRCodeDetectorAruco().detectAndDecode(enlarged)[0], 'synthetic test')

    def test_user_samples(self):
        root = Path('/workspace/dev/prompts')
        valid, expired = (root / ('H43-sample-' + name + '.png') for name in ('desktop-qr', 'expired'))
        if not valid.exists() or not expired.exists():
            self.skipTest('user samples unavailable')
        result = extract_frame_qr(valid.read_bytes())
        self.assertIsNotNone(result)
        enlarged = cv2.imdecode(np.frombuffer(result, np.uint8), cv2.IMREAD_COLOR)
        self.assertGreaterEqual(min(enlarged.shape[:2]), 600)
        self.assertTrue(cv2.QRCodeDetector().detectAndDecode(enlarged)[0])
        self.assertIsNone(extract_frame_qr(expired.read_bytes()))
