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


if __name__ == '__main__':
    unittest.main()
