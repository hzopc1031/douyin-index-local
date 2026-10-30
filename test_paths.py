"""Offline state-path and shell guard checks using synthetic files only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from desktop_gateway import HUB_KEY, create_app
from hub_paths import state_dir
from hub_web import Hub
import hub_bridge

ROOT = Path(__file__).resolve().parent


class PathsTests(unittest.TestCase):
    def test_default(self):
        with patch.dict(os.environ):
            os.environ.pop('INDEX_HUB_STATE_DIR', None)
            self.assertEqual(state_dir(Path('/synthetic')), Path('/synthetic/.cloud-state'))

    def test_configured_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'project'
            root.mkdir()
            state = Path(temp) / 'state'
            with patch.dict(os.environ, {'INDEX_HUB_STATE_DIR': str(state)}):
                hub = Hub(root, bridge=lambda *args: None)
                try:
                    self.assertTrue((state / 'index-hub.sqlite3').is_file())
                    self.assertFalse((root / '.cloud-state').exists())
                finally:
                    hub.store.close()
                (state / 'access.json').write_text(json.dumps({
                    'username': 'synthetic', 'password': 'synthetic-only'}))
                app = create_app(root, bridge=lambda *args: None, desktop_dir=root)
                app[HUB_KEY].store.close()
                self.assertEqual(hub_bridge._socket_path(), state / 'browser.sock')

    @unittest.skipUnless(shutil.which('bash'), 'bash unavailable')
    def test_script_syntax(self):
        for name in ('tencent-start.sh', 'tencent-desktop.sh', 'tencent-gateway.sh',
                     'tencent-vnc-check.sh'):
            with self.subTest(script=name):
                subprocess.run(['bash', '-n', str(ROOT / name)], check=True, capture_output=True)

    @unittest.skipUnless(shutil.which('bash'), 'bash unavailable')
    def test_vnc_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            def check():
                return subprocess.run(['bash', str(ROOT / 'tencent-vnc-check.sh'), temp],
                                      capture_output=True, text=True)
            result = check()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('缺少 VNC 口令文件', result.stderr)
            passwd = Path(temp) / 'vnc.passwd'
            passwd.write_bytes(b'synthetic')
            for mode, valid in ((0o600, True), (0o400, True), (0o640, False),
                                (0o604, False), (0o700, False), (0o1600, False)):
                with self.subTest(mode=oct(mode)):
                    passwd.chmod(mode)
                    self.assertEqual(check().returncode == 0, valid)
