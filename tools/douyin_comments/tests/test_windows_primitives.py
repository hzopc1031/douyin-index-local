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
    def test_new_private_file_never_overwrites_existing_file(self):
        import tempfile
        from pathlib import Path
        from dyc.errors import Stop
        from dyc.windows_private import create_private_empty_directory, read_private_file, write_new_private_file
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = create_private_empty_directory(root / 'private')
            target = folder / 'sample.txt'
            write_new_private_file(target, b'synthetic')
            self.assertEqual(read_private_file(target), b'synthetic')
            with self.assertRaises(Stop):
                write_new_private_file(target, b'must not overwrite')
            self.assertEqual(read_private_file(target), b'synthetic')

    def test_private_file_rejects_hardlinks(self):
        import tempfile
        from pathlib import Path
        import win32api
        import win32con
        import win32security
        from dyc.errors import Stop
        from dyc.windows_private import create_private_empty_directory, verify_private_file, read_private_file
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = create_private_empty_directory(root / 'private')
            target = directory / 'sample.txt'
            target.write_text('synthetic', encoding='utf-8')
            token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
            try:
                user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            finally:
                token.Close()
            win32security.SetNamedSecurityInfo(str(target), win32security.SE_FILE_OBJECT,
                win32security.OWNER_SECURITY_INFORMATION, user, None, None, None)
            verify_private_file(target)
            self.assertEqual(read_private_file(target), b'synthetic')
            with self.assertRaises(Stop) as oversized:
                read_private_file(target, max_bytes=1)
            self.assertEqual(oversized.exception.reason, 'file_too_large')
            other = root / 'another-link.txt'
            os.link(target, other)
            with self.assertRaises(Stop) as caught:
                verify_private_file(target)
            self.assertEqual(caught.exception.reason, 'file_permissions')
            other.unlink()

    def test_new_empty_directory_is_private_and_existing_is_untouched(self):
        import tempfile
        from pathlib import Path
        from dyc.errors import Stop
        from dyc.windows_private import create_private_empty_directory, verify_private_directory
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'private'
            self.assertEqual(create_private_empty_directory(target), target)
            self.assertTrue(target.is_dir())
            verify_private_directory(target)
            with self.assertRaises(Stop):
                create_private_empty_directory(target)
            self.assertTrue(target.is_dir())

    def test_rejects_directory_with_extra_reader(self):
        import tempfile
        from pathlib import Path
        import ntsecuritycon
        import win32security
        from dyc.errors import Stop
        from dyc.windows_private import create_private_empty_directory, verify_private_directory
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'private'
            create_private_empty_directory(target)
            info = win32security.GetNamedSecurityInfo(str(target), win32security.SE_FILE_OBJECT,
                                                      win32security.DACL_SECURITY_INFORMATION)
            acl = info.GetSecurityDescriptorDacl()
            everyone = win32security.ConvertStringSidToSid('S-1-1-0')
            acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_GENERIC_READ, everyone)
            win32security.SetNamedSecurityInfo(str(target), win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                None, None, acl, None)
            with self.assertRaises(Stop) as caught:
                verify_private_directory(target)
            self.assertEqual(caught.exception.reason, 'directory_permissions')

    def test_rejects_missing_child_inheritance(self):
        import tempfile
        from pathlib import Path
        import ntsecuritycon
        import win32security
        from dyc.errors import Stop
        from dyc.windows_private import create_private_empty_directory, verify_private_directory
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'private'
            create_private_empty_directory(target)
            info = win32security.GetNamedSecurityInfo(str(target), win32security.SE_FILE_OBJECT,
                                                      win32security.DACL_SECURITY_INFORMATION)
            old = info.GetSecurityDescriptorDacl()
            acl = win32security.ACL()
            for i in range(old.GetAceCount()):
                acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, old.GetAce(i)[2])
            win32security.SetNamedSecurityInfo(str(target), win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                None, None, acl, None)
            with self.assertRaises(Stop) as caught:
                verify_private_directory(target)
            self.assertEqual(caught.exception.reason, 'directory_permissions')

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
