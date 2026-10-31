"""Offline-only recognition/trajectory tests; optional real model is gated."""
import math
import os
import random
import threading
import unittest
from unittest.mock import patch

from login_driver import Capture, FrameMetadata, interpolate, validate_points
from slider_solver import LocalGapSolver, trajectory


class TrajectoryTests(unittest.TestCase):
    def test_endpoints_timing_variation_bounds_and_shared_interpolation(self):
        durations = set()
        for seed in range(60):
            points = trajectory((.1, .8), (.9, .8), rng=random.Random(seed))
            self.assertEqual(points[0], (.1, .8, 0))
            self.assertEqual(points[-1][:2], (.9, .8))
            self.assertEqual(validate_points('drag', points), points)
            self.assertLessEqual(len(points), 200)
            self.assertTrue(all(abs(y-.8) <= .006 for _, y, _ in points))
            self.assertTrue(all(b[0] >= a[0] for a, b in zip(points, points[1:])))
            durations.add(points[-1][2])
            mapped = [(x*640, y*480, t) for x, y, t in points]
            replay = interpolate(mapped)
            self.assertEqual(replay[-1], mapped[-1])
            self.assertTrue(all(math.dist(a[:2], b[:2]) <= 8.000001 for a, b in zip(replay, replay[1:])))
        self.assertGreater(len(durations), 30)

    def test_invalid_targets(self):
        for end in ((.1, .5), (1.1, .5), (math.nan, .5), (.7, math.inf)):
            with self.assertRaises(ValueError):
                trajectory((.2, .5), end)


class SolverContractTests(unittest.TestCase):
    def test_absent_library_is_not_handled_and_imports_are_lazy(self):
        solver = LocalGapSolver()
        self.assertIsNone(solver._model)
        with patch.dict('sys.modules', {'captcha_recognizer.slider': None}):
            self.assertIsNone(solver._load())
            self.assertIsNone(solver._load())

    def test_only_gap_slider_and_cancellation_never_load_model(self):
        solver = LocalGapSolver()
        with patch.object(solver, '_load', side_effect=AssertionError('must not load')):
            for kind in ('rotation', 'unknown', 'click', '3d'):
                self.assertIsNone(solver.solve(None, dict(kind=kind), threading.Event()))
            cancel = threading.Event()
            cancel.set()
            self.assertIsNone(solver.solve(None, dict(kind='gap_slider'), cancel))

    def test_distance_uses_both_reference_points_and_travel_ratio(self):
        # Cropped/downsampled HiDPI image: scale = zoom * DPR * downsampling.
        capture = Capture(b'synthetic', FrameMetadata(10, 20, 600, 480, 2, 2, .5))
        challenge = dict(kind='gap_slider', rects={
            'slider_handle': dict(x=20, y=210, width=20, height=20),
            'slider_track': dict(x=20, y=210, width=140, height=20),
            'slider_image': dict(x=10, y=20, width=300, height=180),
            'slider_piece': dict(x=20, y=60, width=60, height=60)})
        solver = LocalGapSolver()
        with patch.object(solver, 'detect', return_value=((40, 100, 100, 160, .95), (400, 100, 460, 160, .95))):
            points = solver.solve(capture, challenge, threading.Event())
        self.assertIsNotNone(points)
        start = capture.metadata.css_point(*points[0][:2])
        end = capture.metadata.css_point(*points[-1][:2])
        self.assertEqual(start, (30, 220))
        self.assertAlmostEqual(end[0]-start[0], 90)  # 180 CSS gap * 120/240 travel ratio.
        self.assertEqual(end[1], 220)
        challenge['rects']['slider_track'] = None
        self.assertIsNone(solver.solve(capture, challenge, threading.Event()))


class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.solver = LocalGapSolver()
        if cls.solver._load() is None:
            if os.environ.get('INDEX_HUB_REQUIRE_SOLVER_TESTS') == '1':
                raise AssertionError('optional solver/model required but unavailable')
            raise unittest.SkipTest('optional solver/model unavailable')

    def test_real_model_finds_programmatic_gap_and_generates_drag(self):
        from solver_fixture import gap_fixture
        capture, challenge = gap_fixture()
        detection = self.solver.detect(capture.png)
        self.assertIsNotNone(detection)
        piece, gap = detection
        self.assertAlmostEqual(piece[0], 15, delta=4)
        self.assertAlmostEqual(gap[0], 205, delta=4)
        self.assertGreaterEqual(min(piece[4], gap[4]), .8)
        points = self.solver.solve(capture, challenge, threading.Event())
        self.assertIsNotNone(points)
        self.assertEqual(validate_points('drag', points), points)
        self.assertAlmostEqual((points[-1][0]-points[0][0])*320, 190, delta=4)

    def test_real_model_blank_image_is_not_handled(self):
        import cv2
        import numpy as np
        _, png = cv2.imencode('.png', np.full((240, 320, 3), 220, np.uint8))
        self.assertIsNone(self.solver.detect(bytes(png)))


if __name__ == '__main__':
    unittest.main()
