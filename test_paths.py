"""Offline state-path and shell guard checks using synthetic files only."""
import json
import os
from pathlib import Path
import re
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
    def test_desktop_exit_is_failure(self):
        lines = (ROOT / 'tencent-desktop.sh').read_text().strip().splitlines()
        commands = [line for line in lines if not line.startswith('#')]
        self.assertEqual(commands[-2:], ['wait -n "${children[@]}"', 'exit 1'])

    def test_qr_session_documentation(self):
        for name in ('HUB_API.md', 'deploy/README.md'):
            with self.subTest(document=name):
                text = (ROOT / name).read_text()
                for detail in ('`app.py` 进程内存', '不写状态文件和日志',
                               '0700', '近似明文', 'INDEX_HUB_PERSIST_LOGIN=0', '直到单元停止', 'chromium_sandbox=False'):
                    self.assertIn(detail, text)

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

    @unittest.skipUnless(shutil.which('bash'), 'bash unavailable')
    def test_vnc_auth_toggle_builds_args_without_starting_vnc(self):
        with tempfile.TemporaryDirectory() as temp:
            log_dir = Path(temp) / 'logs'
            state = Path(temp) / 'state'
            log_dir.mkdir()
            state.mkdir()
            env = os.environ.copy()
            env['STATE_DIR'] = str(state)
            env['LOG_DIR'] = str(log_dir)
            script = '''
set -euo pipefail
source ./tencent-processes.sh
prepare_vnc_auth
printf '%s\\n' "${vnc_auth_args[@]}"
'''

            def run(value=None):
                current = env.copy()
                current.pop('INDEX_HUB_VNC_AUTH', None)
                if value is not None:
                    current['INDEX_HUB_VNC_AUTH'] = value
                return subprocess.run(['bash', '-c', script], cwd=ROOT, env=current,
                                       capture_output=True, text=True)

            missing = run()
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn('缺少 VNC 口令文件', missing.stderr)
            self.assertFalse((log_dir / 'desktop.log').exists())
            for value in ('', '1', 'false', '00', 'no'):
                with self.subTest(value=value):
                    refused = run(value)
                    self.assertNotEqual(refused.returncode, 0)
                    self.assertIn('缺少 VNC 口令文件', refused.stderr)

            secret = b'synthetic-secret-not-a-real-password'
            blocked = state / 'vnc.passwd'
            blocked.write_bytes(secret)
            blocked.chmod(0)
            disabled = run('0')
            self.assertEqual(disabled.returncode, 0, disabled.stderr)
            self.assertEqual(disabled.stdout.splitlines(), ['-nopw'])
            self.assertNotIn('vnc.passwd', disabled.stdout)
            log = (log_dir / 'desktop.log').read_bytes()
            self.assertEqual(log.count(b'\n'), 1)
            self.assertIn(b'INDEX_HUB_VNC_AUTH is 0', log)
            self.assertIn(b'without a password', log)
            self.assertNotIn(secret, log)

            blocked.chmod(0o600)
            enabled = run('1')
            self.assertEqual(enabled.returncode, 0, enabled.stderr)
            self.assertEqual(enabled.stdout.splitlines(), ['-rfbauth', str(blocked)])
            self.assertEqual((log_dir / 'desktop.log').read_bytes().count(b'\n'), 1)

        command = ('x11vnc -display :107 -localhost -rfbport 5907 -forever -shared '
                   '"${vnc_auth_args[@]}" -quiet')
        for name in ('tencent-desktop.sh', 'tencent-start.sh'):
            text = (ROOT / name).read_text()
            self.assertIn('prepare_vnc_auth\n' + 'start_child ' + command, text)
            self.assertNotIn('-nopw', text)
            self.assertNotIn('rfbauth', text)
        page = (ROOT / 'hub.html').read_text()
        urls = re.findall(r'/desktop/vnc\.html\?[^"\'<\s]+', page)
        self.assertGreaterEqual(len(urls), 1)
        for url in urls:
            self.assertIn('autoconnect=1', url)
            self.assertNotIn('password', url)
