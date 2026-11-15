"""Exercise macOS startup recovery with isolated fake executables, no network."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


@unittest.skipUnless(shutil.which('zsh'), 'zsh is required for the macOS launcher')
class StartupTests(unittest.TestCase):
    def test_comment_setup_invokes_only_declared_installers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / 'setup-comments.command', root / 'setup-comments.command')
            executable = root / '.venv/bin/python'
            executable.parent.mkdir(parents=True)
            executable.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n', encoding='utf-8')
            executable.chmod(0o700)
            env = {**os.environ, 'CALLS': str(root / 'calls')}
            result = subprocess.run([shutil.which('zsh'), str(root / 'setup-comments.command')],
                                    env=env, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertEqual((root / 'calls').read_text().splitlines(), [
                '-m pip install -r tools/douyin_comments/requirements.txt',
                '-m playwright install chromium',
            ])

    def test_failed_dependency_install_is_retried_and_then_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / 'start.command', root / 'start.command')
            executable = root / '.venv/bin/python'
            executable.parent.mkdir(parents=True)
            executable.write_text('''#!/bin/sh
if [ "$1" = "-c" ]; then test -f "$READY"; exit $?; fi
if [ "$1" = "-m" ]; then
  echo install >> "$CALLS"
  if [ "$FAIL_INSTALL" = "1" ]; then exit 1; fi
  touch "$READY"; exit 0
fi
if [ "$1" = "app.py" ]; then touch "$STARTED"; exit 0; fi
exit 2
''', encoding='utf-8')
            executable.chmod(0o700)
            env = {**os.environ, 'READY': str(root / 'ready'), 'CALLS': str(root / 'calls'),
                   'STARTED': str(root / 'started'), 'FAIL_INSTALL': '1'}
            cmd = [shutil.which('zsh'), str(root / 'start.command')]
            first = subprocess.run(cmd, env=env, capture_output=True, timeout=10)
            self.assertNotEqual(first.returncode, 0)
            self.assertFalse((root / 'started').exists())
            env['FAIL_INSTALL'] = '0'
            self.assertEqual(subprocess.run(cmd, env=env, capture_output=True, timeout=10).returncode, 0)
            self.assertTrue((root / 'started').exists())
            self.assertEqual(subprocess.run(cmd, env=env, capture_output=True, timeout=10).returncode, 0)
            self.assertEqual((root / 'calls').read_text().splitlines(), ['install', 'install'])


@unittest.skipUnless(os.name == 'nt', 'Windows launcher integration')
class WindowsStartupTests(unittest.TestCase):
    def test_comment_setup_creates_environment_and_stops_on_install_failure(self):
        with tempfile.TemporaryDirectory(prefix='评论 安装 ') as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / 'setup-comments-windows.cmd', root / 'setup-comments-windows.cmd')
            (root / 'pip.py').write_text(
                "import os, sys\nfrom pathlib import Path\n"
                "Path('calls.txt').open('a').write('pip\\n')\n"
                "sys.exit(1 if os.getenv('FAKE_PIP_FAIL') == '1' else 0)\n", encoding='utf-8')
            (root / 'playwright.py').write_text(
                "from pathlib import Path\nPath('calls.txt').open('a').write('playwright\\n')\n", encoding='utf-8')
            command = ['cmd.exe', '/d', '/c', str(root / 'setup-comments-windows.cmd')]
            env = {**os.environ, 'FAKE_PIP_FAIL': '1'}
            first = subprocess.run(command, cwd=root, env=env, capture_output=True, timeout=120)
            self.assertNotEqual(first.returncode, 0)
            self.assertTrue((root / '.venv/Scripts/python.exe').is_file())
            self.assertEqual((root / 'calls.txt').read_text().splitlines(), ['pip'])
            env['FAKE_PIP_FAIL'] = '0'
            second = subprocess.run(command, cwd=root, env=env, capture_output=True, timeout=45)
            self.assertEqual(second.returncode, 0, second.stderr.decode(errors='replace'))
            self.assertEqual((root / 'calls.txt').read_text().splitlines(), ['pip', 'pip', 'playwright'])

    def test_existing_environment_launches_from_unicode_path_without_installing(self):
        import sys
        with tempfile.TemporaryDirectory(prefix='指数 启动 ') as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / 'start-windows.cmd', root / 'start-windows.cmd')
            subprocess.run([sys.executable, '-m', 'venv', str(root / '.venv')],
                           check=True, capture_output=True, timeout=90)
            for name in ('requests', 'cryptography', 'webview', 'tzdata'):
                (root / (name + '.py')).write_text('', encoding='utf-8')
            (root / 'app.py').write_text("from pathlib import Path\nPath('started.txt').write_text('ok')\n", encoding='utf-8')
            result = subprocess.run(['cmd.exe', '/d', '/c', str(root / 'start-windows.cmd')],
                                    cwd=root, capture_output=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            self.assertEqual((root / 'started.txt').read_text(), 'ok')


if __name__ == '__main__':
    unittest.main()
