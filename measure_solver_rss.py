"""Fresh-process offline RSS; add --qt under Xvfb for the full SMS/Qt flow.

The process-tree sample sums resident pages (including shared pages) every
50ms on Linux. It is a conservative RSS sum, not a service-cgroup measurement.
Only synthetic data and 127.0.0.1 are used. Nothing is downloaded or installed.
"""
import argparse
import os
import resource
import subprocess
import sys
import threading

from slider_solver import LocalGapSolver
from solver_fixture import gap_fixture


class TreePeak:
    def __init__(self):
        self.kib = 0
        self.failed = False
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self):
        try:
            while not self.stop.is_set():
                # ps reads process accounting; do not inspect profiles or sessions.
                with subprocess.Popen(['ps', '-e', '-o', 'pid=,ppid=,rss='], stdout=subprocess.PIPE) as process:
                    try:
                        output, _ = process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                        raise
                    if process.returncode:
                        raise RuntimeError('process_accounting_failed')
                rows = [tuple(map(int, line.split())) for line in output.splitlines()]
                family = {os.getpid()}
                while True:
                    children = {pid for pid, parent, _ in rows if parent in family and pid != process.pid}
                    if children <= family:
                        break
                    family.update(children)
                self.kib = max(self.kib, sum(rss for pid, _, rss in rows if pid in family))
                self.stop.wait(.05)
        except Exception:
            self.failed = True

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(6)
        if self.thread.is_alive() or self.failed or not self.kib:
            raise RuntimeError('process_tree_measurement_failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qt', action='store_true', help='run the real Qt SMS login/leak fixture before the extra solve')
    args = parser.parse_args()
    if args.qt and os.environ.get('INDEX_HUB_REQUIRE_BROWSER_TESTS') != '1':
        parser.error('--qt requires INDEX_HUB_REQUIRE_BROWSER_TESTS=1 and Xvfb/display')
    with TreePeak() as tree:
        if args.qt:
            import app  # Same app-side imports as the desktop, without starting it.
            import unittest
            from test_sms_browser import SmsBrowserTests
            suite = unittest.TestSuite([SmsBrowserTests('test_auto_flow_and_complete_leak_scan')])
            result = unittest.TextTestRunner(verbosity=1).run(suite)
            if not result.wasSuccessful() or result.skipped:
                return 1
            solver = SmsBrowserTests.solver
        else:
            solver = LocalGapSolver()
        if solver._load() is None:
            print('Optional solver/model unavailable; requirements-solver.txt is required for measurement')
            return 2
        capture, challenge = gap_fixture()
        points = solver.solve(capture, challenge, threading.Event())
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        mib = rss / (1024*1024 if sys.platform == 'darwin' else 1024)
    label = 'SMS/ONNX + Qt login flow + synthetic solve' if args.qt else 'ONNX model + synthetic solve'
    print(f'{label}: process peak RSS {mib:.1f} MiB')
    print(f'Sampled process-tree peak RSS: {tree.kib/1024:.1f} MiB (50ms sampling, shared pages counted per process)')
    print(f'Synthetic solve handled: {points is not None}; service MemoryMax: 3072 MiB')
    return 0 if points is not None and max(mib, tree.kib/1024) < 3072 else 1


if __name__ == '__main__':
    raise SystemExit(main())
