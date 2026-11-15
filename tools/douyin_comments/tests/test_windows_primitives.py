"""Windows groundwork; the comment entrypoint remains disabled pending ACL work."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dyc import browser, runtime


class WindowsPrimitiveTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipIf(os.name == 'nt', 'non-Windows guard')
    async def test_acl_helper_refuses_non_windows(self):
        from dyc.errors import Stop
        from dyc.windows_private import create_private_empty_directory
        with self.assertRaises(Stop) as caught:
            create_private_empty_directory('/tmp/unused')
        self.assertEqual(caught.exception.reason, 'windows_only')

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


@unittest.skipUnless(os.name == 'nt', 'Windows ACL verification')
class WindowsAclTests(unittest.TestCase):
    def test_new_empty_directory_is_private_and_existing_is_untouched(self):
        import tempfile
        from pathlib import Path
        from dyc.errors import Stop
        from dyc.windows_private import create_private_empty_directory
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'private'
            self.assertEqual(create_private_empty_directory(target), target)
            self.assertTrue(target.is_dir())
            with self.assertRaises(Stop):
                create_private_empty_directory(target)
            self.assertTrue(target.is_dir())

    def test_reparse_point_ancestor_is_rejected(self):
        import tempfile
        import subprocess
        from pathlib import Path
        from dyc.errors import Stop
        from dyc.windows_private import create_private_empty_directory
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'real').mkdir()
            link = root / 'junction'
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(root / 'real')],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            with self.assertRaises(Stop) as caught:
                create_private_empty_directory(link / 'private')
            self.assertEqual(caught.exception.reason, 'directory_symlink')
            self.assertFalse((root / 'real' / 'private').exists())
            link.rmdir()
