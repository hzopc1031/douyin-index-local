"""Local comment process lifecycle tests; no browser or network activity."""
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import app  # Adds the comment package to the import path.
from local_comments import LocalCommentJob


class LocalCommentTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin' and importlib.util.find_spec('psutil') and
                         importlib.util.find_spec('playwright'), 'macOS comment dependencies required')
    def test_real_child_stops_before_browser_on_low_memory(self):
        with tempfile.TemporaryDirectory(dir=str(Path(tempfile.gettempdir()).resolve())) as temp:
            root = Path(temp)
            (root / 'sitecustomize.py').write_text(
                'import psutil\nclass LowMemory:\n    available = 1\n'
                'psutil.virtual_memory = lambda: LowMemory()\n', encoding='utf-8')
            done = threading.Event()
            job = LocalCommentJob(app.ROOT, on_finished=done.set)
            with patch.dict(os.environ, {'HOME': str(root), 'PYTHONPATH': str(root)}):
                self.assertTrue(job.start('https://www.douyin.com/video/123')['ok'])
                self.assertTrue(done.wait(10))
            self.assertEqual(job.status()['code'], 'low_memory')
            self.assertEqual(job.status()['state'], 'stopped')

    def test_invalid_input_and_unsupported_platform_never_spawn(self):
        spawn = Mock()
        job = LocalCommentJob(app.ROOT, platform='win32', frozen=False, popen=spawn)
        self.assertEqual(job.start('https://www.douyin.com/video/123')['code'], 'platform_unsupported')
        job = LocalCommentJob(app.ROOT, platform='darwin', frozen=False, popen=spawn, dependency_check=lambda: True)
        self.assertEqual(job.start('https://example.invalid/video/1')['code'], 'invalid_urls')
        job = LocalCommentJob(app.ROOT, platform='darwin', frozen=False, popen=spawn, dependency_check=lambda: False)
        self.assertEqual(job.start('https://www.douyin.com/video/123')['code'], 'dependencies_missing')
        job = LocalCommentJob(app.ROOT, platform='darwin', frozen=True, popen=spawn, dependency_check=lambda: True)
        self.assertEqual(job.start('https://www.douyin.com/video/123')['code'], 'source_required')
        spawn.assert_not_called()

    def test_launch_normalizes_urls_and_collects_safe_result(self):
        done = threading.Event()
        calls = []
        result = {'exit_code': 0, 'exit_reason': 'complete', 'out': '/synthetic/output',
                  'private_detail': 'must not appear in status'}
        def fake_spawn(args, **options):
            calls.append((args, options))
            return subprocess.Popen([sys.executable, '-c', 'print(' + repr(json.dumps(result)) + ')'],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        job = LocalCommentJob(app.ROOT, platform='darwin', frozen=False, popen=fake_spawn,
                              on_finished=done.set, dependency_check=lambda: True)
        reply = job.start('https://www.douyin.com/video/123?tracking=secret')
        self.assertTrue(reply['ok'])
        self.assertTrue(done.wait(5))
        self.assertEqual(calls[0][0][-1], 'https://www.douyin.com/video/123')
        self.assertFalse(calls[0][1]['shell'])
        self.assertEqual(job.status(), {'state': 'complete', 'code': 'complete',
                                        'exit_code': 0, 'out': '/synthetic/output'})

    def test_partial_result_reports_only_known_safe_reasons(self):
        done = threading.Event()
        result = {'exit_code': 2, 'exit_reason': 'partial', 'out': '/synthetic/output',
                  'videos': [{'reason': 'comment_template_missing'}, {'reason': 'untrusted private text'}]}
        def fake_spawn(_args, **_options):
            return subprocess.Popen([sys.executable, '-c', 'import sys; print(' + repr(json.dumps(result)) + '); sys.exit(2)'],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        job = LocalCommentJob(app.ROOT, platform='darwin', frozen=False, popen=fake_spawn,
                              on_finished=done.set, dependency_check=lambda: True)
        self.assertTrue(job.start('https://www.douyin.com/video/123')['ok'])
        self.assertTrue(done.wait(5))
        self.assertEqual(job.status()['details'], ['comment_template_missing'])

    def test_app_keeps_index_lock_until_comment_job_finishes(self):
        api = app.Api()
        job = Mock()
        job.start.return_value = {'ok': True, 'state': 'running'}
        with patch('local_comments.LocalCommentJob', return_value=job) as constructor:
            self.assertTrue(api.local_comments_start('synthetic')['ok'])
            self.assertEqual(api.fetch_hotlist()['code'], 'comments_busy')
            constructor.call_args.kwargs['on_finished']()
        self.assertFalse(api._lock.locked())

    def test_closing_app_stops_owned_comment_process(self):
        done = threading.Event()
        children = []
        def fake_spawn(_args, **_options):
            process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            children.append(process)
            return process
        job = LocalCommentJob(app.ROOT, platform='darwin', frozen=False, popen=fake_spawn,
                              on_finished=done.set, dependency_check=lambda: True)
        self.assertTrue(job.start('https://www.douyin.com/video/123')['ok'])
        job.close()
        self.assertTrue(done.wait(5))
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(job.status()['state'], 'stopped')

    def test_forced_shutdown_checks_descendant_identity(self):
        from types import SimpleNamespace
        original = Mock(pid=12)
        original.create_time.return_value = 1.0
        replaced = Mock(pid=12)
        replaced.create_time.return_value = 2.0
        parent = Mock()
        parent.children.return_value = [original]
        fake_psutil = SimpleNamespace(Process=lambda pid: parent if pid == 11 else replaced, Error=RuntimeError)
        process = Mock(pid=11)
        process.wait.side_effect = [subprocess.TimeoutExpired('synthetic', 8), 0]
        job = LocalCommentJob(app.ROOT, platform='darwin', frozen=False, dependency_check=lambda: True)
        job._process = process
        with patch.dict(sys.modules, {'psutil': fake_psutil}):
            job.close()
        process.kill.assert_called_once()
        replaced.kill.assert_not_called()

    def test_app_releases_lock_if_job_rejects_start(self):
        api = app.Api()
        job = Mock()
        job.start.return_value = {'ok': False, 'code': 'invalid_urls'}
        with patch('local_comments.LocalCommentJob', return_value=job):
            self.assertFalse(api.local_comments_start('bad')['ok'])
        self.assertFalse(api._lock.locked())


if __name__ == '__main__':
    unittest.main()
