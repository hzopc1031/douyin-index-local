"""Vendored local solver contract and real bundled model; synthetic images."""
import asyncio
import os
import random
import threading
import unittest
from unittest.mock import patch

from dyc.slider_solver import LocalGapSolver, trajectory
from dyc.sms_browser import points_checked
from dyc.sms_solver import GapWorker


class SolverTests(unittest.TestCase):
    def test_trajectory_bounds(self):
        for seed in range(40):
            points = trajectory((.1, .8), (.9, .8), rng=random.Random(seed))
            self.assertEqual(points_checked(points), points)
            self.assertEqual(points[-1][:2], (.9, .8))

    def test_only_ordinary_gap_and_no_download_on_missing_library(self):
        solver = LocalGapSolver()
        for kind in ('rotation', '3d', 'click', 'unknown'):
            self.assertIsNone(solver.solve(None, {'kind': kind}, threading.Event()))
        with patch.dict('sys.modules', {'onnxruntime': None, 'captcha_recognizer.slider': None}):
            self.assertIsNone(solver._load())


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_model_in_child_and_cleanup(self):
        from tests.sms_solver_fixture import gap_fixture
        capture, challenge = gap_fixture()
        worker = GapWorker()
        try:
            points = await worker.solve(capture, challenge)
            self.assertIsNotNone(points, 'Install requirements-solver.txt for the complete offline suite')
            self.assertEqual(points_checked(points), points)
            self.assertAlmostEqual((points[-1][0]-points[0][0])*320, 190, delta=4)
        finally:
            await worker.close()
        self.assertIsNone(worker.process)

    async def test_worker_timeout_kills_inference_without_thread_leak(self):
        # Real worker, but expire its bounded wait immediately after spawn.
        from tests.sms_solver_fixture import gap_fixture
        capture, challenge = gap_fixture()
        worker = GapWorker()
        original = asyncio.timeout
        with patch('dyc.sms_solver.asyncio.timeout', side_effect=lambda _: original(.00001)):
            self.assertIsNone(await worker.solve(capture, challenge))
        self.assertIsNone(worker.process)
