"""Synthetic private-profile and local-only Chromium persistence tests."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import browser_profile as bp


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'INDEX_HUB_PROFILE_DIR': str(self.root / 'profiles')})
        env.start()
        self.addCleanup(env.stop)

    def test_private_permissions_and_tightening(self):
        previous = os.umask(0)
        try:
            directory = bp.prepare_profile(self.root, 'test')
        finally:
            os.umask(previous)
        for path in (directory.parent, directory):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            path.chmod(0o755)
        with self.assertLogs('browser_profile', level='WARNING'):
            bp.prepare_profile(self.root, 'test')
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(directory.parent.stat().st_mode & 0o777, 0o700)

    def test_symlinks_and_names_refused(self):
        directory = bp.prepare_profile(self.root, 'test')
        directory.rmdir()
        directory.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(bp.ProfileError):
            bp.prepare_profile(self.root, 'test')
        directory.unlink()
        directory.parent.rmdir()
        directory.parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(bp.ProfileError):
            bp.prepare_profile(self.root, 'test')
        for name in ('../x', 'UPPER', '', 'a_b'):
            with self.assertRaises(bp.ProfileError):
                bp.prepare_profile(self.root, name)

    def test_wrong_owner_refused(self):
        from types import SimpleNamespace
        with patch('browser_profile.Path.lstat', return_value=SimpleNamespace(st_mode=0o40700, st_uid=os.geteuid()+1)):
            with self.assertRaises(bp.ProfileError):
                bp.prepare_profile(self.root, 'test')

    def test_defaults_and_rollback(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(bp.profile_root(self.root), self.root / '.cloud-state/browser-profiles')
            self.assertTrue(bp.persist_enabled())
        with patch.dict(os.environ, {'INDEX_HUB_PERSIST_LOGIN': '0'}):
            self.assertFalse(bp.persist_enabled())
        with patch.dict(os.environ, {'INDEX_HUB_PERSIST_LOGIN': 'invalid'}), patch.object(bp, '_warned', False):
            with self.assertLogs('browser_profile', level='WARNING') as logs:
                self.assertTrue(bp.persist_enabled())
                self.assertTrue(bp.persist_enabled())
            self.assertEqual(len(logs.output), 1)

    def test_exclusive_lock_in_process_and_subprocess(self):
        directory = bp.prepare_profile(self.root, 'test')
        with bp.ProfileLock(directory):
            with self.assertRaises(bp.ProfileBusy):
                bp.ProfileLock(directory).acquire()
            script = "from browser_profile import *; import sys\ntry: ProfileLock(sys.argv[1]).acquire()\nexcept ProfileBusy: sys.exit(7)\n"
            result = subprocess.run([sys.executable, '-c', script, str(directory)], capture_output=True)
            self.assertEqual(result.returncode, 7, result.stderr)
        with bp.ProfileLock(directory):
            pass
        self.assertEqual((directory.parent / '.test.lock').stat().st_mode & 0o777, 0o600)

    def test_singleton_cleanup(self):
        directory = bp.prepare_profile(self.root, 'test')
        for target in ('other-host-123', 'unparseable', socket.gethostname()+'-99999999'):
            with self.subTest(target=target), bp.ProfileLock(directory):
                for name in ('SingletonLock', 'SingletonCookie', 'SingletonSocket'):
                    (directory / name).symlink_to(target)
                with self.assertLogs('browser_profile', level='INFO'):
                    bp.clear_stale_singleton(directory)
                self.assertFalse(any(directory.iterdir()))
        with bp.ProfileLock(directory):
            (directory / 'SingletonLock').symlink_to(f'{socket.gethostname()}-{os.getpid()}')
            with self.assertRaises(bp.ProfileBusy):
                bp.clear_stale_singleton(directory)
            self.assertTrue((directory / 'SingletonLock').is_symlink())


class ChromiumPersistenceTests(unittest.TestCase):
    def test_persistence_and_stale_singleton_launch(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as temporary, sync_playwright() as playwright:
            directory = Path(temporary) / 'profile'
            directory.mkdir(mode=0o700)
            page_file = Path(temporary) / 'page.html'
            page_file.write_text('<title>Synthetic persistence fixture</title>')
            with bp.ProfileLock(directory):
                context = playwright.chromium.launch_persistent_context(str(directory), headless=True, chromium_sandbox=False)
                try:
                    page = context.pages[0]
                    page.goto(page_file.as_uri())
                    page.evaluate("localStorage.setItem('fixture', 'synthetic-only')")
                finally:
                    context.close()
            (directory / 'SingletonLock').symlink_to('other-host-12345')
            with bp.ProfileLock(directory):
                bp.clear_stale_singleton(directory)
                context = playwright.chromium.launch_persistent_context(str(directory), headless=True, chromium_sandbox=False)
                try:
                    page = context.pages[0]
                    page.goto(page_file.as_uri())
                    self.assertEqual(page.evaluate("localStorage.getItem('fixture')"), 'synthetic-only')
                finally:
                    context.close()
