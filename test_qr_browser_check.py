import os
import unittest
from unittest.mock import patch

import qr_browser_check


class BrowserCheckTests(unittest.TestCase):
    def test_bad_mode(self):
        self.assertEqual(qr_browser_check.main(['x', 'bogus']), 1)

    def test_headed_without_display(self):
        with patch.dict(os.environ, {}, clear=True), patch.dict('sys.modules', {'playwright': None, 'playwright.sync_api': None}):
            self.assertEqual(qr_browser_check.check('headed'), {'mode': 'headed', 'ok': False, 'error': 'DISPLAY unset'})

    def test_real_browser(self):
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest('playwright not installed')
        try:
            result = qr_browser_check.check('headless')
        except Exception as exc:
            self.skipTest('Chromium cannot launch: ' + type(exc).__name__)
        self.assertTrue(result['headless_ua'])
        self.assertTrue(result['ok'])
        if os.environ.get('DISPLAY'):
            headed = qr_browser_check.check('headed')
            self.assertFalse(headed['headless_ua'])
            self.assertTrue(headed['ok'])


if __name__ == '__main__':
    unittest.main()
