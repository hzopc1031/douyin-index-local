"""Windows groundwork; the comment entrypoint remains disabled pending ACL work."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dyc import browser, runtime


class WindowsPrimitiveTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows signal and pipe integration')
    async def test_windows_runner_pipe_triggers_graceful_stop(self):
        import subprocess
        import sys
        code = '''import asyncio, sys
from dyc.errors import Control
from dyc.runner import install_signals, remove_signals
async def main():
    control = Control()
    previous = install_signals(control)
    try:
        await asyncio.wait_for(control.event.wait(), 5)
        print(control.error.reason)
    finally:
        remove_signals(previous)
asyncio.run(main())
'''
        result = subprocess.run([sys.executable, '-c', code], input=b'\x00', capture_output=True,
                                timeout=10, env={**os.environ, 'DYC_LOCAL_STOP_PIPE': '1'})
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertEqual(result.stdout.decode().strip(), 'interrupted')

    async def test_stop_pipe_notifies_owned_child(self):
        import subprocess
        import sys
        code = '''import asyncio, sys
from dyc.errors import Control
from dyc.windows_stop import start_stop_pipe
async def main():
    control = Control()
    start_stop_pipe(control, sys.stdin.buffer)
    await asyncio.wait_for(control.event.wait(), 5)
    print(control.error.reason)
asyncio.run(main())
'''
        result = subprocess.run([sys.executable, '-c', code], input=b'\x00',
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertEqual(result.stdout.decode().strip(), 'interrupted')

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
    def test_runner_stop_pipe_exports_interrupted_result(self):
        import subprocess
        import sys
        import tempfile
        from pathlib import Path
        from dyc.windows_private import read_private_file

        script = '''import asyncio, json, os, sys
from pathlib import Path
from types import SimpleNamespace
from dyc import runner, site
class Browser:
    def __init__(self, profile, control, budget, pacing, **kwargs):
        self.control, self.video_id = control, ''
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def preflight(self, *args): pass
    async def open_video(self, url):
        target = site.parse_url(url)
        self.video_id = target.video_id
        return target, 'synthetic', ''
    def preserve_seeds(self, video): pass
async def login(*args, **kwargs): pass
async def collect(video, browser, pages):
    await browser.control.run(asyncio.sleep(10))
class Pacer:
    async def before(self): pass
root = Path(sys.argv[1])
args = SimpleNamespace(out=root / 'output', profile_dir=root / 'profile', state_dir=root / 'state',
    urls=['https://www.douyin.com/video/123'], urls_file=None, max_pages=1, display=None, top=5)
runner.Browser, runner.local_login, runner.collect = Browser, login, collect
code, result = asyncio.run(runner.fetch(args, local_session=True, pacer=Pacer()))
print(json.dumps({'code': code, 'reason': result['exit_reason']}))
'''
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable, '-c', script, temporary], input=b'\x00',
                capture_output=True, timeout=20, env={**os.environ, 'DYC_LOCAL_STOP_PIPE': '1'})
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            import json
            self.assertEqual(json.loads(result.stdout), {'code': 2, 'reason': 'interrupted'})
            saved = json.loads(read_private_file(Path(temporary) / 'output' / 'run.json'))
            self.assertEqual((saved['exit_code'], saved['exit_reason']), (2, 'interrupted'))

    @unittest.skipUnless(os.name == 'nt', 'Windows runner integration')
    def test_offline_runner_uses_private_storage_and_exports(self):
        import asyncio
        import tempfile
        from pathlib import Path
        from dyc import runner, site
        from dyc.windows_private import read_private_file

        class SyntheticBrowser:
            def __init__(self, profile, control, budget, pacing, **kwargs):
                self.budget = budget
                self.video_id = ''

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def preflight(self, *args):
                pass

            async def open_video(self, url):
                self.budget.new_video()
                self.budget.reserve()
                target = site.parse_url(url)
                self.video_id = target.video_id
                return target, 'synthetic', ''

            def preserve_seeds(self, video):
                pass

        async def no_login(*args, **kwargs):
            pass

        async def collect(video, browser, pages):
            video.status, video.reason = 'complete', 'exhausted'

        class NoWait:
            async def before(self):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = SimpleNamespace(out=root / 'output', profile_dir=root / 'profile',
                state_dir=root / 'state', urls=['https://www.douyin.com/video/123'],
                urls_file=None, max_pages=1, display=None, top=5)
            with patch.object(runner, 'Browser', SyntheticBrowser), \
                 patch.object(runner, 'local_login', no_login), \
                 patch.object(runner, 'collect', collect), \
                 patch.dict(os.environ, {'DYC_LOCAL_STOP_PIPE': '0'}):
                code, result = asyncio.run(runner.fetch(args, local_session=True, pacer=NoWait()))
            self.assertEqual((code, result['exit_reason'], result['requests']), (0, 'complete', 1))
            self.assertEqual(runtime.DailyCounter(args.state_dir).usage(), 1)
            for filename in ('summary.md', 'run.json', 'comments.csv'):
                self.assertTrue(read_private_file(args.out / filename))

    def test_synthetic_comment_export_stays_private(self):
        import tempfile
        from pathlib import Path
        from dyc.models import Comment, Video
        from dyc.output import Log, export
        from dyc.windows_private import read_private_file
        with tempfile.TemporaryDirectory() as temporary:
            folder = runtime.private_dir(Path(temporary) / 'result')
            log = Log(folder)
            log.event('synthetic')
            log.close()
            video = Video('123', 'https://www.douyin.com/video/123')
            video.comments['1'] = Comment('1', 1, '1', '', text='合成评论')
            result = export(folder, 'synthetic-run', [video], 0, 'complete', 1)
            self.assertEqual(result['exit_code'], 0)
            self.assertIn('合成评论', read_private_file(folder / 'summary.md').decode('utf-8'))
            self.assertTrue(read_private_file(folder / 'comments.csv').startswith(b'\xef\xbb\xbf'))
            self.assertEqual(read_private_file(folder / 'run.log'), b'synthetic\r\n')

    def test_runtime_counter_round_trip(self):
        import tempfile
        from pathlib import Path
        from dyc.windows_private import create_private_empty_directory
        with tempfile.TemporaryDirectory() as temporary:
            folder = create_private_empty_directory(Path(temporary) / 'state')
            counter = runtime.DailyCounter(folder, today=lambda: '2026-11-15', limit=3)
            self.assertEqual(counter.usage(), 0)
            counter.reserve()
            counter.reserve()
            self.assertEqual(counter.usage(), 2)

    def test_runtime_private_open_checks_before_truncate(self):
        import tempfile
        from pathlib import Path
        from dyc.errors import Stop
        from dyc.windows_private import create_private_empty_directory, read_private_file
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = create_private_empty_directory(root / 'private')
            target = folder / 'run.log'
            fd = runtime.private_open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(b'synthetic')
            self.assertEqual(read_private_file(target), b'synthetic')
            link = root / 'hardlink'
            os.link(target, link)
            with self.assertRaises(Stop):
                runtime.private_open(target, os.O_WRONLY | os.O_TRUNC)
            self.assertEqual(link.read_bytes(), b'synthetic')
            link.unlink()
            fd = runtime.private_open(target, os.O_RDONLY)
            with os.fdopen(fd, 'rb') as stream:
                self.assertEqual(stream.read(), b'synthetic')

    def test_runtime_creates_nested_private_directories_and_reuses_them(self):
        import tempfile
        from pathlib import Path
        from dyc.windows_private import verify_private_directory
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'dyc-output' / 'run'
            self.assertEqual(runtime.private_dir(target), target)
            verify_private_directory(target.parent)
            verify_private_directory(target)
            self.assertEqual(runtime.private_dir(target), target)

    def test_runtime_atomic_text_uses_private_windows_replacement(self):
        import tempfile
        from pathlib import Path
        from dyc.windows_private import create_private_empty_directory, read_private_file
        with tempfile.TemporaryDirectory() as temporary:
            folder = create_private_empty_directory(Path(temporary) / 'private')
            target = folder / 'usage.json'
            runtime.atomic_text(target, 'first')
            runtime.atomic_text(target, 'second')
            self.assertEqual(read_private_file(target), b'second')

    def test_private_replace_rejects_hardlinks_and_preserves_original(self):
        import tempfile
        from pathlib import Path
        from dyc.errors import Stop
        from dyc.windows_private import (create_private_empty_directory, read_private_file,
                                         replace_private_file, write_new_private_file)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = create_private_empty_directory(root / 'private')
            target = directory / 'usage.json'
            write_new_private_file(target, b'old')
            replace_private_file(target, b'new')
            self.assertEqual(read_private_file(target), b'new')
            other = root / 'another-link'
            os.link(target, other)
            with self.assertRaises(Stop):
                replace_private_file(target, b'not written')
            self.assertEqual(other.read_bytes(), b'new')
            other.unlink()

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
