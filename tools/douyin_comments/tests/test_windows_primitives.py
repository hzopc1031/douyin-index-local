"""Windows groundwork; the comment entrypoint remains disabled pending ACL work."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dyc import browser, runtime


class WindowsPrimitiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_display_does_not_require_xvfb(self):
        with patch.object(runtime, '_WINDOWS', True), patch.object(runtime, '_MACOS', False):
            display = runtime.Display()
            self.assertIsNone(await display.start())
            self.assertEqual(display.environment(), {})
            await display.close()

    async def test_windows_memory_uses_psutil_available(self):
        fake = SimpleNamespace(virtual_memory=lambda: SimpleNamespace(available=123456), Error=RuntimeError)
        with patch.object(runtime, '_WINDOWS', True), patch.object(runtime, '_MACOS', False), \
             patch.dict('sys.modules', {'psutil': fake}):
            self.assertEqual(runtime.mem_available(), 123456)

    @unittest.skipUnless(os.name == 'nt', 'Windows process discovery')
    async def test_windows_process_discovery_uses_real_process_identity(self):
        records = browser._processes(os.getpid())
        self.assertIn(os.getpid(), records)
        self.assertIsInstance(records[os.getpid()], float)
