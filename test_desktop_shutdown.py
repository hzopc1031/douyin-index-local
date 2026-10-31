"""Offline process supervision tests: only synthetic children and /tmp files."""
import configparser
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parent
WRAPPERS = ('tencent-desktop.sh', 'tencent-start.sh')
STUB = r'''
import fcntl, json, os, signal, subprocess, sys, time
from pathlib import Path
root = Path(os.environ['FIXTURE_DIR'])
name = Path(sys.argv[0]).name
if name == 'python':
    name = Path(sys.argv[1]).stem
if sys.argv[1:] == ['renderer']:
    name = 'renderer'
stopping = False
def stop(signum, frame):
    global stopping
    (root / (name + '.term')).touch()
    stopping = True
signal.signal(signal.SIGTERM, signal.SIG_IGN if os.environ.get('IGNORE_TERM') == name else stop)
signal.signal(signal.SIGUSR1, lambda *args: sys.exit(int(os.environ.get('CHILD_EXIT', '0'))))
renderer = None
if name == 'app':
    lock = (root / 'synthetic.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX)
    renderer = subprocess.Popen([sys.executable, __file__, 'renderer'])
ready = root / (name + '.ready')
temporary = ready.with_suffix('.tmp')
temporary.write_text(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp()}))
temporary.rename(ready)
if name == 'dbus-daemon':
    print('unix:path=/synthetic-session-bus', flush=True)
while not stopping:
    time.sleep(.02)
if name == 'app':
    time.sleep(float(os.environ.get('APP_SHUTDOWN_DELAY', '.2')))
    # Qt must retain the display, bus and its renderer throughout its flush.
    for dependency in ('Xvfb', 'dbus-daemon', 'renderer'):
        os.kill(json.loads((root / (dependency + '.ready')).read_text())['pid'], 0)
        assert not (root / (dependency + '.term')).exists(), dependency
    renderer.terminate()
    renderer.wait(timeout=1)
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
    (root / 'app.flushed').touch()
(root / (name + '.exited')).touch()
'''


class TemplateTests(unittest.TestCase):
    def test_supervisor_is_main_pid_with_bounded_cgroup_fallback(self):
        legacy = (ROOT / 'tencent-enable-service.sh').read_text().split("<<'UNIT'\n", 1)[1].split('\nUNIT\n', 1)[0]
        desktop = (ROOT / 'deploy/systemd/douyin-index-desktop.service').read_text()
        for content, script in ((desktop, WRAPPERS[0]), (legacy, WRAPPERS[1])):
            with self.subTest(script=script):
                config = configparser.ConfigParser(interpolation=None, strict=False)
                config.read_string(content)
                service = config['Service']
                self.assertEqual(service['ExecStart'], '/bin/bash /srv/work-ecs-projects/douyin-index-local/' + script)
                self.assertEqual(service['KillMode'], 'mixed')
                self.assertEqual(service['TimeoutStopSec'], '12')
                self.assertNotIn('SuccessExitStatus', service)

    def test_entry_points_use_shared_supervision(self):
        for name in (*WRAPPERS, 'tencent-processes.sh', 'tencent-enable-service.sh'):
            with self.subTest(script=name):
                subprocess.run(['bash', '-n', str(ROOT / name)], check=True, capture_output=True)
        for name in WRAPPERS:
            script = (ROOT / name).read_text()
            self.assertIn('source ./tencent-processes.sh', script)
            self.assertIn('start_session_bus\n', script)
            for command in ('Xvfb', 'fluxbox', 'x11vnc', '.venv/bin/python app.py'):
                self.assertIn('start_child ' + command, script)
            self.assertIn('app_pid=${children[-1]}', script)
            self.assertIn('wait -n "${children[@]}"', script)
        helper = (ROOT / 'tencent-processes.sh').read_text()
        for statement in ('trap cleanup EXIT', "trap 'exit 143' TERM", "trap 'exit 130' INT",
                          "trap '' TERM INT", 'wait_until_gone 60', 'wait_until_gone 20',
                          'kill -KILL', 'wait "${children[@]}"', 'setsid "$@"'):
            self.assertIn(statement, helper)


@unittest.skipUnless(sys.platform == 'linux' and shutil.which('setsid'), 'Linux setsid required')
class SimulationTests(unittest.TestCase):
    def simulate(self, script, *, ignore='', signum=signal.SIGTERM, repeated=False,
                 app_delay='.2', child_exit=None, during_startup=False):
        with tempfile.TemporaryDirectory(prefix='dy-shutdown-', dir='/tmp') as temp:
            root = Path(temp)
            bindir = root / 'bin'
            bindir.mkdir()
            (root / '.venv/bin').mkdir(parents=True)
            for name in ('Xvfb', 'fluxbox', 'x11vnc', 'dbus-daemon', 'python'):
                path = root / '.venv/bin/python' if name == 'python' else bindir / name
                path.write_text('#!' + sys.executable + '\n' + STUB)
                path.chmod(0o700)
            # Use the production launch/wait body and helper. Replace only the
            # X socket readiness probe with the synthetic Xvfb's ready marker;
            # omit the deployment preflight (paths, credentials and environment).
            body = (ROOT / script).read_text().split('\nstart_session_bus\n', 1)[1]
            body = body.replace('test -S /tmp/.X11-unix/X107',
                                'test -f "$FIXTURE_DIR/' +
                                ('hold-startup' if during_startup else 'Xvfb.ready') + '"')
            harness = root / 'wrapper.sh'
            harness.write_text('set -euo pipefail\nsource ' + shlex.quote(str(ROOT / 'tencent-processes.sh')) +
                               '\nLOG_DIR=$FIXTURE_DIR\nSTATE_DIR=$FIXTURE_DIR\nstart_session_bus\n' + body)
            env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'],
                       FIXTURE_DIR=temp, IGNORE_TERM=ignore, APP_SHUTDOWN_DELAY=app_delay,
                       CHILD_EXIT=str(child_exit or 0))
            expected = {'Xvfb', 'fluxbox', 'x11vnc', 'app', 'renderer', 'dbus-daemon'}
            if script == 'tencent-start.sh':
                expected.add('desktop_gateway')
            with tempfile.TemporaryFile(mode='w+') as output:
                proc = subprocess.Popen(['bash', str(harness)], cwd=root, env=env,
                                        stdout=output, stderr=output, start_new_session=True)
                try:
                    ready = {'Xvfb', 'dbus-daemon'} if during_startup else expected
                    deadline = time.monotonic() + 5
                    while not all((root / (name + '.ready')).exists() for name in ready):
                        self.assertIsNone(proc.poll(), 'wrapper exited during startup')
                        self.assertLess(time.monotonic(), deadline, 'stub startup timed out')
                        time.sleep(.01)
                    pids = {p.stem: json.loads(p.read_text()) for p in root.glob('*.ready')}
                    started = time.monotonic()
                    if child_exit is None:
                        proc.send_signal(signum)
                    else:
                        os.kill(pids['fluxbox']['pid'], signal.SIGUSR1)
                    if repeated:
                        time.sleep(.1)
                        proc.send_signal(signal.SIGTERM)
                        proc.send_signal(signal.SIGINT)
                    result = proc.wait(timeout=8)
                    elapsed = time.monotonic() - started
                    output.seek(0)
                    logs = output.read()
                    expected_code = 128 + signum if child_exit is None else (child_exit or 1)
                    self.assertEqual(result, expected_code, logs)
                    self.assertLess(elapsed, 7 if float(app_delay) > 1 else 4, logs)
                    pids = {p.stem: json.loads(p.read_text()) for p in root.glob('*.ready')}
                    if during_startup:
                        self.assertNotIn('app', pids)
                    for name, info in pids.items():
                        with self.subTest(child=name):
                            with self.assertRaises(ProcessLookupError):
                                os.kill(info['pid'], 0)
                            if name != 'renderer':
                                with self.assertRaises(ProcessLookupError):
                                    os.killpg(info['pgid'], 0)
                    if not during_startup:
                        self.assertTrue((root / 'app.flushed').exists(), logs)
                        with (root / 'synthetic.lock').open() as lock:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        for name in expected - {ignore}:
                            if child_exit is not None and name == 'fluxbox':
                                continue
                            self.assertTrue((root / (name + '.exited')).exists(), name + logs)
                    if ignore:
                        self.assertFalse((root / (ignore + '.exited')).exists())
                    print(f'{script}: stop {elapsed:.2f}s (ignore={ignore or "none"}, signal={signum}, child_exit={child_exit})', flush=True)
                finally:
                    # Clean up only this test's recorded process groups, even if
                    # a regression leaves descendants after the wrapper exits.
                    for ready in root.glob('*.ready'):
                        try:
                            os.killpg(json.loads(ready.read_text())['pgid'], signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=2)

    def test_sigterm_normal_children(self):
        for script in WRAPPERS:
            with self.subTest(script=script):
                self.simulate(script)

    def test_sigterm_escalates_resistant_vnc(self):
        for script in WRAPPERS:
            with self.subTest(script=script):
                self.simulate(script, ignore='x11vnc')

    def test_sigint_and_repeated_signals_do_not_restart_cleanup(self):
        self.simulate(WRAPPERS[0], ignore='x11vnc', signum=signal.SIGINT, repeated=True)

    def test_app_gets_full_five_second_shutdown_window(self):
        self.simulate(WRAPPERS[0], app_delay='5.2')

    def test_child_exit_keeps_failure_restart_semantics(self):
        for status in (0, 75):
            with self.subTest(status=status):
                self.simulate(WRAPPERS[0], child_exit=status)

    def test_sigterm_during_x_startup(self):
        self.simulate(WRAPPERS[0], during_startup=True)


if __name__ == '__main__':
    unittest.main()
