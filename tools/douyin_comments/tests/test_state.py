"""Pending renderer evaluations must not prevent deadlines or signal cleanup."""

from argparse import Namespace
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from playwright.async_api import Frame

from dyc import site
from dyc.browser import Browser, STATE_JS, _processes
from dyc.errors import Control, Stop
from dyc.fake import FakeSite, FastPacer
from dyc.runner import login
from dyc.runtime import Budget, DailyCounter, Profile
from tests.browser_helpers import ManualDeadline, browser_asyncio

ROOT = Path(__file__).resolve().parents[1]


class StateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = Control()

    def browser(self, fake):
        return Browser(self.root / "profile", self.control,
                       Budget(DailyCounter(self.root / "state")), FastPacer(), backend=fake)

    async def test_scan_has_one_deadline_across_all_frames(self):
        evaluated, cancelled = [], []
        deadline = ManualDeadline()

        class SlowFrame:
            async def evaluate(frame, *_):
                evaluated.append(frame)
                try:
                    if len(evaluated) == 2:
                        # Expire the shared scan deadline in the second frame,
                        # independent of how long the first frame took on CPU.
                        deadline.expire()
                        await asyncio.Event().wait()
                    return dict(risk=False, out=False, inside=True)
                except asyncio.CancelledError:
                    cancelled.append(frame)
                    raise

        browser = self.browser(FakeSite())
        browser.page = Namespace(frames=[SlowFrame() for _ in range(10)])
        start = asyncio.get_running_loop().time()
        with browser_asyncio(timeout_at=deadline):
            with self.assertRaises(Stop) as caught:
                await asyncio.wait_for(browser.state(), 30)
        end = asyncio.get_running_loop().time()
        self.assertEqual((caught.exception.code, caught.exception.reason), (3, "state_check_timeout"))
        self.assertEqual(len(deadline.requested), 1)
        self.assertLessEqual(start + 2, deadline.requested[0])
        self.assertLessEqual(deadline.requested[0], end + 2)
        self.assertTrue(deadline.timers[0].expired())
        self.assertEqual(len(evaluated), 2)
        self.assertEqual(cancelled, [evaluated[-1]])
        self.assertTrue(self.control.event.is_set())

    async def test_stop_propagates_and_cancels_pending_frame(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        class HungFrame:
            async def evaluate(frame, *_):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        browser = self.browser(FakeSite())
        browser.page = Namespace(frames=[HungFrame()])
        with patch("dyc.browser.STATE_TIMEOUT_SECONDS", 60):
            task = asyncio.create_task(browser.state())
            await asyncio.wait_for(started.wait(), 1)
            self.control.stop(2, "interrupted")
            with self.assertRaises(Stop) as caught:
                await asyncio.wait_for(task, 0.5)
        self.assertEqual((caught.exception.code, caught.exception.reason), (2, "interrupted"))
        self.assertTrue(cancelled.is_set())

    async def test_preflight_deadline_bounds_pending_renderer_evaluation(self):
        fake = FakeSite()
        original = Frame.evaluate
        cancelled = asyncio.Event()

        async def hung(frame, expression, arg=None):
            if expression == STATE_JS and frame.url.startswith(site.FAKE_ORIGIN):
                try:
                    return await original(frame, "() => new Promise(() => {})")
                finally:
                    cancelled.set()
            return await original(frame, expression, arg)

        async with self.browser(fake) as browser:
            with patch.object(Frame, "evaluate", hung), patch("dyc.browser.STATE_TIMEOUT_SECONDS", 60):
                with self.assertRaises(Stop) as caught:
                    await asyncio.wait_for(browser.preflight(site.FAKE_ORIGIN, wait_seconds=0.15), 2)
                self.assertEqual((caught.exception.code, caught.exception.reason), (3, "state_check_timeout"))
                await asyncio.wait_for(browser.watcher, 1)
            self.assertTrue(cancelled.is_set())
            self.assertEqual(browser.budget.used, 0)
            authority = browser.display.authority
        self.assertFalse(authority.parent.exists())
        with Profile(self.root / "profile"):
            pass
        self.assertEqual(fake.calls, [])
        self.assertEqual(fake.foreign, [])

    async def test_preflight_missing_login_signal_expires_without_renderer_failure(self):
        class NoLoginSignal(FakeSite):
            def html(self, video_id):
                return '<!doctype html><p>synthetic page without login signals</p>'

        fake = NoLoginSignal()
        async with self.browser(fake) as browser:
            with self.assertRaises(Stop) as caught:
                await asyncio.wait_for(browser.preflight(site.FAKE_ORIGIN, wait_seconds=0.25), 2)
            self.assertEqual((caught.exception.code, caught.exception.reason), (4, "login_signal_missing"))
            self.assertFalse(self.control.event.is_set())
            self.assertEqual(await browser.state(), dict(risk=False, out=False, inside=False))
            self.assertEqual(browser.budget.used, 0)
        self.assertEqual(fake.calls, [])
        self.assertEqual(fake.foreign, [])

    async def test_preflight_expired_window_does_not_start_state_scan(self):
        fake = FakeSite()
        async with self.browser(fake) as browser:
            original = browser.state
            foreground_scans = []

            async def state(**kwargs):
                if "deadline" in kwargs:
                    foreground_scans.append(kwargs["deadline"])
                return await original(**kwargs)

            with patch.object(browser, "state", side_effect=state):
                with self.assertRaises(Stop) as caught:
                    await asyncio.wait_for(browser.preflight(site.FAKE_ORIGIN, wait_seconds=0), 2)
            self.assertEqual((caught.exception.code, caught.exception.reason), (4, "login_signal_missing"))
            self.assertEqual(foreground_scans, [])
            self.assertFalse(self.control.event.is_set())
            self.assertEqual(browser.budget.used, 0)
        self.assertEqual(fake.calls, [])
        self.assertEqual(fake.foreign, [])

    async def test_login_deadline_bounds_pending_state_and_deletes_qr(self):
        fake = FakeSite("login-expired")
        original = Frame.evaluate
        cancelled = asyncio.Event()
        images_seen = []

        async def hung_after_qr(frame, expression, arg=None):
            images = list((self.root / "state").glob("*.png"))
            if expression == STATE_JS and images:
                images_seen.extend(images)
                try:
                    return await original(frame, "() => new Promise(() => {})")
                finally:
                    cancelled.set()
            return await original(frame, expression, arg)

        args = Namespace(profile_dir=self.root / "profile", state_dir=self.root / "state", display=None)
        with patch.object(Frame, "evaluate", hung_after_qr), patch("dyc.browser.STATE_TIMEOUT_SECONDS", 60):
            code, result = await asyncio.wait_for(login(args, backend=fake, wait_seconds=0.2), 8)
        self.assertEqual((code, result["exit_reason"]), (3, "state_check_timeout"))
        self.assertTrue(cancelled.is_set())
        self.assertTrue(images_seen)
        self.assertFalse(Path(result["qr_path"]).exists())
        with Profile(args.profile_dir):
            pass
        self.assertEqual(fake.calls, [])
        self.assertEqual(fake.foreign, [])

    async def test_state_timeout_blocks_comment_admission(self):
        fake = FakeSite()
        original = Frame.evaluate

        async def hung_on_video(frame, expression, arg=None):
            if expression == STATE_JS and "/video/" in frame.url:
                return await original(frame, "() => new Promise(() => {})")
            return await original(frame, expression, arg)

        async with self.browser(fake) as browser:
            await browser.preflight(site.FAKE_ORIGIN)
            with patch.object(Frame, "evaluate", hung_on_video), patch("dyc.browser.STATE_TIMEOUT_SECONDS", 0.15):
                with self.assertRaises(Stop) as caught:
                    await asyncio.wait_for(browser.open_video(site.FAKE_ORIGIN + "/video/100"), 2)
                self.assertEqual(caught.exception.reason, "state_check_timeout")
                await asyncio.wait_for(browser.watcher, 1)
            self.assertEqual(browser.budget.used, 0)
        self.assertEqual(fake.calls, [])
        self.assertEqual(fake.foreign, [])


class StateSignalTests(unittest.TestCase):
    def test_sigterm_interrupts_preflight_and_login_state_checks(self):
        self.check_signal(signal.SIGTERM)

    def test_sigint_interrupts_preflight_and_login_state_checks(self):
        self.check_signal(signal.SIGINT)

    def check_signal(self, sig):
        for stage in ("preflight", "login-initial", "login-wait"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                proc = subprocess.Popen([sys.executable, "-m", "tests.state_signal_worker", temp, stage],
                                        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, start_new_session=True)
                try:
                    deadline = time.monotonic() + 20
                    while not (root / "ready").exists() and proc.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue((root / "ready").exists(), "fixture never reached pending state check")
                    children = _processes(proc.pid)
                    self.assertGreater(len(children), 3)
                    if stage == "login-wait":
                        self.assertEqual(len(list((root / "state").glob("*.png"))), 1)
                    authorities = []
                    for pid in children:
                        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                        if command and Path(os.fsdecode(command[0])).name == "Xvfb":
                            authorities.append(Path(os.fsdecode(command[command.index(b"-auth") + 1])))
                    self.assertEqual(len(authorities), 1)
                    start = time.monotonic()
                    proc.send_signal(sig)
                    stdout, stderr = proc.communicate(timeout=5)
                    self.assertLess(time.monotonic() - start, 5)
                    self.assertEqual(proc.returncode, 2, stdout + stderr)
                    self.assertEqual(len(stdout.splitlines()), 1)
                    result = json.loads(stdout)
                    self.assertEqual(result["exit_reason"], "interrupted")
                    self.assertFalse(list((root / "state").glob("*.png")))
                    if stage == "preflight":
                        self.assertEqual(result["requests"], 0)
                        self.assertTrue((root / "out/comments.csv").exists())
                    else:
                        self.assertTrue(result["qr_deleted"])
                    self.assertTrue(all(not authority.parent.exists() for authority in authorities))
                    with Profile(root / "profile"):
                        pass
                    for pid, born in children.items():
                        path = Path(f"/proc/{pid}/stat")
                        if path.exists():
                            text = path.read_text()
                            self.assertNotEqual(text[text.rfind(")") + 2:].split()[19], born,
                                                f"owned child {pid} survived shutdown")
                finally:
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.communicate(timeout=5)
