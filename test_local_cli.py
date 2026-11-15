"""Offline tests for the local machine-readable interface."""
import io
import importlib.util
import json
import unittest
from unittest.mock import patch

from hotlist_client import HotListError
from local_cli import main


class FakeClient:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.closed = False

    def fetch(self):
        if self.error:
            raise self.error
        return self.result

    def close(self):
        self.closed = True


class LocalCliTests(unittest.TestCase):
    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows subprocess check')
    def test_windows_persistent_comments_login_remains_disabled(self):
        import os
        from pathlib import Path
        import subprocess
        import sys
        root = Path(__file__).resolve().parent
        env = dict(os.environ, PYTHONPATH=str(root / 'tools/douyin_comments'))
        result = subprocess.run([sys.executable, '-m', 'dyc', 'login'], cwd=root,
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 5)
        self.assertEqual(json.loads(result.stdout)['exit_reason'], 'local_session_only_not_ready')

    def test_success_preserves_missing_values_and_closes_client(self):
        client = FakeClient({'items': [{'word': '合成话题', 'hot_value': None}]})
        out, err = io.StringIO(), io.StringIO()
        code = main(['hotlist'], stdout=out, stderr=err, client_factory=lambda: client)
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(out.getvalue())['data']['items'][0]['hot_value'])
        self.assertEqual(err.getvalue(), '')
        self.assertTrue(client.closed)

    def test_normal_desktop_does_not_import_cloud_profile(self):
        import builtins
        import app
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name in ('browser_profile', 'qr_login', 'dyc.hub_comments'):
                raise AssertionError('desktop should not import Unix-only profile support')
            return original_import(name, *args, **kwargs)

        with patch.dict('os.environ', {'INDEX_HUB_ENABLED': '0'}), \
                patch('builtins.__import__', side_effect=guarded_import):
            self.assertTrue(app._webview_start_kwargs()['private_mode'])
            api = app.Api()
            self.assertIsNone(api._qr_manager())
            api._on_auth_failed()
            api._lock.acquire()
            try:
                self.assertEqual(api.fetch_hotlist()['code'], 'busy')
            finally:
                api._lock.release()

    def test_failure_is_structured_and_does_not_return_stale_data(self):
        client = FakeClient(error=HotListError('rate_limited', '已限频', http_status=429))
        out, err = io.StringIO(), io.StringIO()
        code = main(['hotlist'], stdout=out, stderr=err, client_factory=lambda: client)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(json.loads(err.getvalue())['error']['code'], 'rate_limited')
        self.assertTrue(client.closed)

    def test_desktop_hotlist_uses_shared_client_and_releases_lock(self):
        import app
        client = FakeClient({'fetched_at': '2026-01-01', 'items': []})
        api = app.Api()
        with patch('hotlist_client.HotListClient', return_value=client):
            self.assertEqual(api.fetch_hotlist()['data'], client.result)
        self.assertTrue(client.closed)
        self.assertFalse(api._lock.locked())

    def test_desktop_hotlist_fails_closed_and_releases_lock(self):
        import app
        client = FakeClient(error=HotListError('rate_limited', '已限频'))
        api = app.Api()
        with patch('hotlist_client.HotListClient', return_value=client):
            self.assertEqual(api.fetch_hotlist()['code'], 'rate_limited')
        self.assertTrue(client.closed)
        self.assertFalse(api._lock.locked())

    def test_desktop_hotlist_releases_lock_even_if_close_fails(self):
        import app
        client = FakeClient({'items': []})
        api = app.Api()
        with patch('hotlist_client.HotListClient', return_value=client), \
                patch.object(client, 'close', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                api.fetch_hotlist()
        self.assertFalse(api._lock.locked())

    def test_atomic_write_without_unix_fchmod(self):
        import os
        import tempfile
        from pathlib import Path
        from storage import _atomic_write
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'result.json'
            if hasattr(os, 'fchmod'):
                with patch.object(os, 'fchmod'):
                    del os.fchmod
                    _atomic_write(target, b'{}')
            else:
                _atomic_write(target, b'{}')
            self.assertEqual(target.read_bytes(), b'{}')

    def test_cache_read_never_fetches_online_and_omits_raw_data(self):
        from unittest.mock import Mock
        hit = {'result': {'metadata': {'fetched_at': '2026-01-01T00:00:00+08:00'},
                          'rows': [{'keyword': '测试', 'date': '2026-01-01', 'search_index': 8}],
                          'raw_response': 'must-not-leave'}}
        store = Mock()
        store.lookup.return_value = hit
        out, err = io.StringIO(), io.StringIO()
        with patch('local_cli.ResultStore', return_value=store):
            code = main(['index-cache', '--keyword', '测试', '--end-date', '2026-01-07'], stdout=out, stderr=err,
                        client_factory=lambda: self.fail('cache must not access network'))
        self.assertEqual(code, 0)
        store.lookup.assert_called_once_with(['测试'], '2026-01-01', '2026-01-07')
        self.assertNotIn('must-not-leave', out.getvalue())
        self.assertEqual(json.loads(out.getvalue())['data']['rows'][0]['search_index'], 8)

    def test_cache_miss_and_invalid_date_do_not_fetch(self):
        from unittest.mock import Mock
        store = Mock()
        store.lookup.return_value = None
        for day, code in [('2026-01-07', 'cache_miss'), ('bad', 'invalid_or_unavailable')]:
            out, err = io.StringIO(), io.StringIO()
            with patch('local_cli.ResultStore', return_value=store):
                status = main(['index-cache', '--keyword', '测试', '--end-date', day], stdout=out, stderr=err,
                              client_factory=lambda: self.fail('must not fetch'))
            self.assertEqual(status, 1)
            self.assertEqual(json.loads(err.getvalue())['error']['code'], code)
            self.assertEqual(out.getvalue(), '')

    @unittest.skipIf(__import__('os').name == 'nt', 'POSIX directory descriptor path only')
    def test_comment_summary_requires_owned_private_files(self):
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory(dir=str(Path(tempfile.gettempdir()).resolve())) as temp:
            home = Path(temp)
            folder = home / 'dyc-output' / '20261114-120000-abcdef1234'
            folder.mkdir(parents=True, mode=0o700)
            folder.parent.chmod(0o700)
            summary = folder / 'summary.md'
            summary.write_text('合成评论摘要', encoding='utf-8')
            summary.chmod(0o600)
            out, err = io.StringIO(), io.StringIO()
            with patch('pathlib.Path.home', return_value=home):
                self.assertEqual(main(['comment-summary', '--run-id', folder.name], stdout=out, stderr=err), 0)
                self.assertEqual(json.loads(out.getvalue())['data']['summary'], '合成评论摘要')
                summary.unlink()
                summary.symlink_to('/etc/passwd')
                out, err = io.StringIO(), io.StringIO()
                self.assertEqual(main(['comment-summary', '--run-id', folder.name], stdout=out, stderr=err), 1)
                self.assertNotIn('root:', out.getvalue())


@unittest.skipUnless(__import__('os').name == 'nt' and importlib.util.find_spec('win32security'),
                     'Windows ACL dependency required')
class WindowsSummaryTests(unittest.TestCase):
    def test_reads_only_private_windows_summary(self):
        import os
        import sys
        import tempfile
        from pathlib import Path
        import win32api
        import win32con
        import win32security
        sys.path.insert(0, str(Path(__file__).resolve().parent / 'tools' / 'douyin_comments'))
        from dyc.windows_private import create_private_empty_directory

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            base = create_private_empty_directory(home / 'dyc-output')
            folder = create_private_empty_directory(base / '20261114-120000-abcdef1234')
            summary = folder / 'summary.md'
            summary.write_text('合成评论摘要', encoding='utf-8')
            token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
            try:
                user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            finally:
                token.Close()
            win32security.SetNamedSecurityInfo(str(summary), win32security.SE_FILE_OBJECT,
                win32security.OWNER_SECURITY_INFORMATION, user, None, None, None)
            with patch('pathlib.Path.home', return_value=home):
                out, err = io.StringIO(), io.StringIO()
                self.assertEqual(main(['comment-summary', '--run-id', folder.name], stdout=out, stderr=err), 0)
                self.assertEqual(json.loads(out.getvalue())['data']['summary'], '合成评论摘要')
                other = home / 'link.md'
                os.link(summary, other)
                out, err = io.StringIO(), io.StringIO()
                self.assertEqual(main(['comment-summary', '--run-id', folder.name], stdout=out, stderr=err), 1)
                self.assertEqual(json.loads(err.getvalue())['error']['code'], 'summary_unavailable')
                other.unlink()


if __name__ == '__main__':
    unittest.main()
