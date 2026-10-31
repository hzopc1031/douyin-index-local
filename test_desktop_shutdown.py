"""Offline process supervision tests: only synthetic children and /tmp files."""
import configparser
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parent
WRAPPERS = ('tencent-desktop.sh', 'tencent-start.sh')
REAL_DBUS = shutil.which('dbus-daemon')
DBUS_SEND = shutil.which('dbus-send')
STUB = r'''
import fcntl, json, os, signal, socket, subprocess, sys, time
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
bus_socket = None
info = {'pid': os.getpid(), 'pgid': os.getpgrp()}
def probe_bus(phase):
    address = os.environ['DBUS_SESSION_BUS_ADDRESS']
    assert address.startswith('unix:path=/tmp/dy-dbus.'), address
    for key in ('DBUS_SESSION_BUS_PID', 'DBUS_SESSION_BUS_WINDOWID',
                'DBUS_STARTER_ADDRESS', 'DBUS_STARTER_BUS_TYPE'):
        assert key not in os.environ, key
    if os.environ.get('REAL_DBUS_DAEMON') and os.environ.get('DBUS_SEND'):
        subprocess.run([os.environ['DBUS_SEND'], '--session', '--print-reply',
                        '--reply-timeout=1000', '--dest=org.freedesktop.DBus',
                        '/org/freedesktop/DBus', 'org.freedesktop.DBus.ListNames'],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2)
    else:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(1)
            connection.connect(address.removeprefix('unix:path=').split(',')[0])
    (root / ('app.bus-' + phase)).touch()
    return address
if name == 'dbus-daemon':
    info['socket'] = next(arg.removeprefix('--address=unix:path=') for arg in sys.argv
                          if arg.startswith('--address=unix:path='))
    if not os.environ.get('REAL_DBUS_DAEMON') and not os.environ.get('BUS_STARTUP'):
        bus_socket = socket.socket(socket.AF_UNIX)
        bus_socket.bind(info['socket'])
        bus_socket.listen(10)
if name == 'app':
    info['address'] = probe_bus('startup')
    lock = (root / 'synthetic.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX)
    renderer = subprocess.Popen([sys.executable, __file__, 'renderer'])
ready = root / (name + '.ready')
temporary = ready.with_suffix('.tmp')
temporary.write_text(json.dumps(info))
temporary.rename(ready)
if name == 'dbus-daemon':
    if os.environ.get('BUS_STARTUP') == 'fail':
        sys.exit(75)
    if os.environ.get('REAL_DBUS_DAEMON'):
        os.execv(os.environ['REAL_DBUS_DAEMON'], ['dbus-daemon', *sys.argv[1:]])
    if not os.environ.get('BUS_STARTUP'):
        print('unix:path=' + info['socket'], flush=True)
while not stopping:
    time.sleep(.02)
if name == 'app':
    probe_bus('term')
    time.sleep(float(os.environ.get('APP_SHUTDOWN_DELAY', '.2')))
    probe_bus('flushed')
    # Qt must retain the display, bus and its renderer throughout its flush.
    for dependency in ('Xvfb', 'dbus-daemon', 'renderer'):
        os.kill(json.loads((root / (dependency + '.ready')).read_text())['pid'], 0)
        assert not (root / (dependency + '.term')).exists(), dependency
    renderer.terminate()
    renderer.wait(timeout=1)
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
    (root / 'app.flushed').touch()
if name == 'dbus-daemon':
    # The bus must be the last managed child, after the app has been reaped.
    if (root / 'app.ready').exists():
        try:
            os.kill(json.loads((root / 'app.ready').read_text())['pid'], 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError('D-Bus stopped before app exit')
    if bus_socket is not None:
        bus_socket.close()
    # Deliberately leave the socket for the production wrapper to remove.
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
                          'wait_until_gone 10', 'kill -KILL', 'setsid "$@"'):
            self.assertIn(statement, helper)


@unittest.skipUnless(sys.platform == 'linux' and shutil.which('setsid'), 'Linux setsid required')
class SimulationTests(unittest.TestCase):
    def simulate(self, script, *, ignore='', signum=signal.SIGTERM, repeated=False,
                 app_delay='.2', child_exit=None, during_startup=False,
                 real_bus=True, bus_startup='', stop_during_bus=False):
        real_bus = real_bus and bool(REAL_DBUS)
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
                       CHILD_EXIT=str(child_exit or 0), BUS_STARTUP=bus_startup,
                       REAL_DBUS_DAEMON=REAL_DBUS if real_bus else '', DBUS_SEND=DBUS_SEND or '',
                       DBUS_SESSION_BUS_ADDRESS='unix:path=/synthetic-inherited-bus',
                       DBUS_SESSION_BUS_PID='123', DBUS_SESSION_BUS_WINDOWID='456',
                       DBUS_STARTER_ADDRESS='synthetic-inherited', DBUS_STARTER_BUS_TYPE='session')
            expected = {'Xvfb', 'fluxbox', 'x11vnc', 'app', 'renderer', 'dbus-daemon'}
            if script == 'tencent-start.sh':
                expected.add('desktop_gateway')
            with tempfile.TemporaryFile(mode='w+') as output:
                proc = subprocess.Popen(['bash', str(harness)], cwd=root, env=env,
                                        stdout=output, stderr=output, start_new_session=True)
                try:
                    ready = {'Xvfb', 'dbus-daemon'} if during_startup else expected
                    if bus_startup:
                        ready = {'dbus-daemon'}
                    deadline = time.monotonic() + 5
                    while not all((root / (name + '.ready')).exists() for name in ready):
                        output.seek(0)
                        self.assertIsNone(proc.poll(), 'wrapper exited during startup: ' + output.read())
                        self.assertLess(time.monotonic(), deadline, 'stub startup timed out')
                        time.sleep(.01)
                    pids = {p.stem: json.loads(p.read_text()) for p in root.glob('*.ready')}
                    bus_socket = Path(pids['dbus-daemon']['socket'])
                    bus_dir = bus_socket.parent
                    if not bus_startup:
                        self.assertTrue(bus_socket.is_socket())
                        self.assertEqual(stat.S_IMODE(bus_dir.stat().st_mode), 0o700)
                        if not during_startup:
                            self.assertTrue(pids['app']['address'].startswith('unix:path=' + str(bus_socket)))
                    started = time.monotonic()
                    if bus_startup and not stop_during_bus:
                        pass  # The startup deadline or failed daemon must stop the wrapper itself.
                    elif child_exit is None:
                        proc.send_signal(signum)
                    else:
                        os.kill(pids['fluxbox']['pid'], signal.SIGUSR1)
                    if repeated:
                        time.sleep(.1)
                        proc.send_signal(signal.SIGTERM)
                        proc.send_signal(signal.SIGINT)
                    result = proc.wait(timeout=10)
                    elapsed = time.monotonic() - started
                    output.seek(0)
                    logs = output.read()
                    expected_code = 128 + signum if child_exit is None else (child_exit or 1)
                    if bus_startup and not stop_during_bus:
                        expected_code = 1
                    self.assertEqual(result, expected_code, logs)
                    limit = 9 if float(app_delay) > 1 and ignore else 7 if float(app_delay) > 1 or bus_startup else 4
                    self.assertLess(elapsed, limit, logs)
                    self.assertFalse(bus_socket.exists(), logs)
                    self.assertFalse(bus_dir.exists(), logs)
                    pids = {p.stem: json.loads(p.read_text()) for p in root.glob('*.ready')}
                    if during_startup or bus_startup:
                        self.assertNotIn('app', pids)
                    for name, info in pids.items():
                        with self.subTest(child=name):
                            with self.assertRaises(ProcessLookupError):
                                os.kill(info['pid'], 0)
                            if name != 'renderer':
                                with self.assertRaises(ProcessLookupError):
                                    os.killpg(info['pgid'], 0)
                    if not during_startup and not bus_startup:
                        self.assertTrue((root / 'app.flushed').exists(), logs)
                        for phase in ('startup', 'term', 'flushed'):
                            self.assertTrue((root / ('app.bus-' + phase)).exists(), phase + logs)
                        with (root / 'synthetic.lock').open() as lock:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        for name in expected - {ignore}:
                            if name == 'dbus-daemon' and real_bus:
                                continue  # The real daemon cannot write synthetic exit markers.
                            if child_exit is not None and name == 'fluxbox':
                                continue
                            self.assertTrue((root / (name + '.exited')).exists(), name + logs)
                    if ignore:
                        self.assertFalse((root / (ignore + '.exited')).exists())
                    if bus_startup and not stop_during_bus:
                        self.assertIn('Session D-Bus failed to publish its address', logs)
                    print(f'{script}: stop {elapsed:.2f}s (bus={"real" if real_bus else "stub"}, '
                          f'ignore={ignore or "none"}, signal={signum}, child_exit={child_exit}, '
                          f'bus_startup={bus_startup or "normal"})', flush=True)
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
                    bus_ready = root / 'dbus-daemon.ready'
                    if bus_ready.exists():
                        # Assertion failures must not leave this test's socket directory behind.
                        socket_path = Path(json.loads(bus_ready.read_text())['socket'])
                        socket_path.unlink(missing_ok=True)
                        if socket_path.parent.exists():
                            socket_path.parent.rmdir()

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

    def test_slow_app_and_resistant_vnc_stay_within_unit_budget(self):
        self.simulate(WRAPPERS[0], app_delay='5.2', ignore='x11vnc')

    def test_child_exit_keeps_failure_restart_semantics(self):
        for status in (0, 75):
            with self.subTest(status=status):
                self.simulate(WRAPPERS[0], child_exit=status)

    def test_sigterm_during_x_startup(self):
        self.simulate(WRAPPERS[0], during_startup=True)

    def test_stub_bus_is_stopped_last_and_stale_socket_removed(self):
        self.simulate(WRAPPERS[0], real_bus=False)

    def test_resistant_bus_is_killed_and_socket_removed(self):
        self.simulate(WRAPPERS[0], real_bus=False, ignore='dbus-daemon')

    def test_bus_startup_failure_and_timeout(self):
        for mode in ('fail', 'hang'):
            with self.subTest(mode=mode):
                self.simulate(WRAPPERS[0], real_bus=False, bus_startup=mode)

    def test_sigterm_interrupts_bus_startup(self):
        self.simulate(WRAPPERS[0], real_bus=False, bus_startup='hang', stop_during_bus=True)

    def test_missing_daemon_fails_loudly(self):
        with tempfile.TemporaryDirectory(prefix='dy-no-dbus-', dir='/tmp') as temp:
            result = subprocess.run(['/bin/bash', '-c',
                                     'set -euo pipefail; source ' +
                                     shlex.quote(str(ROOT / 'tencent-processes.sh')) +
                                     '; start_session_bus'],
                                    env=dict(os.environ, PATH=temp), capture_output=True,
                                    text=True, timeout=2)
            self.assertEqual(result.returncode, 1)
            self.assertIn('Session D-Bus requires dbus-daemon', result.stderr)


if __name__ == '__main__':
    unittest.main()
