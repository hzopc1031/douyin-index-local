"""Windows Chromium integration against the in-memory synthetic site only."""
import asyncio
from functools import partial
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.name == 'nt' and os.environ.get('DYC_WINDOWS_BROWSER_TESTS') == '1',
                     'explicit Windows Chromium integration')
class WindowsBrowserOfflineTests(unittest.TestCase):
    def test_ephemeral_collection_and_private_export(self):
        from dyc import browser, runner, site
        from dyc.fake import FakeSite, FastPacer
        from dyc.windows_private import read_private_file

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = SimpleNamespace(out=root / 'output', profile_dir=root / 'profile',
                state_dir=root / 'state', urls=[site.FAKE_ORIGIN + '/video/100'],
                urls_file=None, max_pages=25, display=None, top=5)
            fake = FakeSite()
            test_browser = partial(browser.Browser, _test_headless=True, memory_minimum=768 * 1024**2)
            with patch.object(runner, 'Browser', test_browser), \
                 patch.dict(os.environ, {'DYC_LOCAL_STOP_PIPE': '0'}):
                code, result = asyncio.run(runner.fetch(args, backend=fake, pacer=FastPacer(),
                                                        local_session=True))
            self.assertEqual((code, result['exit_reason'], result['requests']), (0, 'complete', 4))
            self.assertEqual((result['videos'][0]['top_count'], result['videos'][0]['reply_count']), (2, 21))
            self.assertFalse(fake.foreign)
            self.assertTrue(read_private_file(args.out / 'comments.csv'))
            self.assertFalse(args.profile_dir.exists())

