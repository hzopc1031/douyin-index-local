"""Offline fresh-process peak RSS: python measure_solver_rss.py (optional deps).

Measures the solver process, not Qt/Chromium or the whole service cgroup. Add
the deployed browser baseline before assessing the 3G MemoryMax headroom.
"""
import resource
import sys
import threading

from slider_solver import LocalGapSolver
from solver_fixture import gap_fixture


def main():
    solver = LocalGapSolver()
    if solver._load() is None:
        print('SKIP: optional solver/model unavailable; install requirements-solver.txt')
        return 2
    capture, challenge = gap_fixture()
    points = solver.solve(capture, challenge, threading.Event())
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    mib = rss / (1024*1024 if sys.platform == 'darwin' else 1024)
    print(f'Process peak RSS after ONNX model load + synthetic solve: {mib:.1f} MiB')
    print(f'Synthetic solve handled: {points is not None}; service MemoryMax: 3072 MiB')
    return 0 if points is not None and mib < 3072 else 1


if __name__ == '__main__':
    raise SystemExit(main())
