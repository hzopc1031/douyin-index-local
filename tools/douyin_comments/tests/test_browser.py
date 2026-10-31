from argparse import Namespace
import asyncio
import csv
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from dyc import site
from dyc.browser import Browser, _processes
from dyc.errors import Control, Stop
from dyc.fake import FakeSite, FastPacer
from dyc.runner import fetch, login
from dyc.models import Video
from dyc.paging import collect
from dyc.runtime import Budget, DailyCounter

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = os.environ.get("DYC_REQUIRE_BROWSER_TESTS") == "1"


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not shutil.which("Xvfb") and not REQUIRED:
            self.skipTest("Xvfb unavailable (set DYC_REQUIRE_BROWSER_TESTS=1 to require)")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = Control()

    def args(self):
        return Namespace(profile_dir=self.root / "profile", state_dir=self.root / "state",
                         display=None, out=self.root / "out", top=30, max_pages=1500,
                         urls=[site.FAKE_ORIGIN + "/video/100", site.FAKE_ORIGIN + "/video/200"],
                         urls_file=None)

    def browser(self, fake, **kwargs):
        return Browser(self.root / "profile", self.control,
                       Budget(DailyCounter(self.root / "state")), FastPacer(), backend=fake, **kwargs)

    async def test_sdk_template_signatures_token_and_reply_derivation(self):
        fake = FakeSite()
        async with self.browser(fake) as browser:
            await browser.preflight(site.FAKE_ORIGIN)
            await browser.open_video(site.FAKE_ORIGIN + "/video/100")
            self.assertIn("top", browser.templates)
            self.assertNotIn("reply", browser.templates)
            self.assertIn("synthetic-stale-signature", browser.templates["top"].url)
            result = await browser.fetch("100", None, "0")
            self.assertEqual(len(site.decode_page(result)[0]), 1)
            await browser.fetch("100", "10001", "41")
            calls = await browser.page.evaluate("window.sdkCalls")
            for call in calls[1:]:
                params = parse_qs(urlsplit(call["url"]).query)
                for signature in site.SIGNATURES:
                    self.assertNotIn(signature, params)
                self.assertEqual(params[site.PARAM["token"]], ["synthetic-fresh-token"])
                self.assertEqual(params[site.PARAM["count"]], ["20"])
                self.assertTrue(call["withCredentials"])
            actual = fake.calls[-1][1]
            self.assertEqual(actual[site.SIGNATURES[0]], "synthetic-sdk-signature")
            self.assertEqual(actual[site.PARAM["item"]], "100")
            self.assertEqual(actual[site.PARAM["root"]], "10001")
            self.assertEqual(actual[site.PARAM["cursor"]], "41")
            self.assertEqual(browser.budget.used, 3)
        self.assertEqual(fake.foreign, [])

    async def test_observed_reply_template(self):
        fake = FakeSite(reply_seed=True)
        async with self.browser(fake) as browser:
            await browser.preflight(site.FAKE_ORIGIN)
            await browser.open_video(site.FAKE_ORIGIN + "/video/100")
            self.assertIn("reply", browser.templates)
            self.assertIn(site.REPLY_PATH, browser.templates["reply"].url)
            await browser.fetch("100", "10001", "41")
        self.assertEqual(fake.foreign, [])

    async def test_abort_on_timeout(self):
        fake = FakeSite("timeout")
        async with self.browser(fake, timeout_ms=100) as browser:
            await browser.preflight(site.FAKE_ORIGIN)
            await browser.open_video(site.FAKE_ORIGIN + "/video/100")
            result = await browser.fetch("100", None, "0")
            self.assertEqual(result, {"kind": "timeout"})
            self.assertEqual(await browser.page.evaluate("window.abortCount"), 1)
            self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.foreign, [])

    async def test_timeout_retry_is_counted_and_limited(self):
        fake = FakeSite("timeout")
        async with self.browser(fake, timeout_ms=100) as browser:
            await browser.preflight(site.FAKE_ORIGIN)
            await browser.open_video(site.FAKE_ORIGIN + "/video/100")
            with self.assertRaises(Stop) as ctx:
                await collect(Video("100", site.FAKE_ORIGIN + "/video/100"), browser)
            self.assertEqual((ctx.exception.code, ctx.exception.reason), (3, "repeated_timeout"))
            self.assertEqual(browser.budget.used, 3)  # native seed + two attempts
            self.assertEqual(len(fake.calls), 3)
            self.assertEqual(await browser.page.evaluate("window.abortCount"), 2)
        self.assertEqual(fake.foreign, [])

    async def test_cancellation_while_waiting_for_request_gate(self):
        fake = FakeSite()
        async with self.browser(fake) as browser:
            await browser.preflight(site.FAKE_ORIGIN)
            await browser.open_video(site.FAKE_ORIGIN + "/video/100")
            await browser.gate.acquire()
            try:
                pending = asyncio.create_task(browser.fetch("100", None, "0"))
                await asyncio.sleep(0.05)
                self.control.stop(2, "interrupted")
                with self.assertRaises(Stop) as ctx:
                    await asyncio.wait_for(pending, 0.5)
                self.assertEqual(ctx.exception.reason, "interrupted")
            finally:
                browser.gate.release()
            self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.foreign, [])

    async def test_captcha_before_any_comment_request(self):
        fake = FakeSite("captcha-before")
        code, result = await fetch(self.args(), backend=fake, pacer=FastPacer())
        self.assertEqual(code, 3)
        self.assertEqual(len(fake.calls), 0)
        self.assertTrue(all(v["status"] == "partial" for v in result["videos"]))
        self.assertEqual(fake.foreign, [])

    async def test_captcha_mid_request_in_iframe_stops_entire_batch(self):
        fake = FakeSite("captcha-mid")
        code, result = await fetch(self.args(), backend=fake, pacer=FastPacer())
        self.assertEqual(code, 3)
        self.assertEqual(result["exit_reason"], "verification_widget")
        self.assertEqual(len(fake.calls), 2)
        self.assertNotIn("/video/200", fake.navigations)
        self.assertEqual(fake.foreign, [])

    async def test_login_missing_before_comments(self):
        fake = FakeSite("login-expired")
        code, result = await fetch(self.args(), backend=fake, pacer=FastPacer())
        self.assertEqual(code, 4)
        self.assertEqual(fake.calls, [])
        self.assertEqual(fake.foreign, [])

    async def test_api_login_expired(self):
        fake = FakeSite("login-api")
        code, result = await fetch(self.args(), backend=fake, pacer=FastPacer())
        self.assertEqual(code, 4)
        self.assertEqual(len(fake.calls), 2)
        self.assertNotIn("/video/200", fake.navigations)
        self.assertEqual(fake.foreign, [])

    async def test_invalid_and_nonzero_api_stop_without_retry(self):
        for scenario in ("invalid-json", "nonzero"):
            with self.subTest(scenario=scenario):
                fake = FakeSite(scenario)
                code, result = await fetch(self.args(), backend=fake, pacer=FastPacer())
                self.assertEqual(code, 3)
                self.assertEqual(len(fake.calls), 2)
                self.assertNotIn("/video/200", fake.navigations)
                self.assertEqual(fake.foreign, [])

    async def test_full_pipeline_outputs_dedupe_and_urls_file(self):
        fake = FakeSite()
        args = self.args()
        args.urls += [args.urls[0], site.FAKE_ORIGIN + "/?modal_id=100"]
        urls_file = self.root / "urls.txt"
        urls_file.write_text(site.FAKE_ORIGIN + "/video/200\n")
        args.urls_file = urls_file
        code, result = await fetch(args, backend=fake, pacer=FastPacer())
        self.assertEqual(code, 0, result)
        self.assertEqual(len(result["videos"]), 2)
        self.assertEqual(result["requests"], 10)
        with (args.out / "comments.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 46)
        self.assertEqual(len({(row["video_id"], row["comment_id"]) for row in rows}), 46)
        self.assertEqual(json.loads((args.state_dir / "usage.json").read_text())["count"], 10)
        for file in args.out.iterdir():
            text = file.read_text(encoding="utf-8-sig")
            for secret in ("synthetic-sdk-signature", "synthetic-stale-signature", "synthetic-fresh-token",
                           "msToken", "a_bogus", "X-Bogus"):
                self.assertNotIn(secret, text)
        self.assertEqual(fake.foreign, [])

    async def test_short_link_resolved_by_browser_and_deduped(self):
        fake = FakeSite()
        args = self.args()
        args.urls = [site.FAKE_ORIGIN + "/short/synthetic", site.FAKE_ORIGIN + "/video/100"]
        code, result = await fetch(args, backend=fake, pacer=FastPacer())
        self.assertEqual(code, 0, result)
        self.assertEqual(len(result["videos"]), 1)
        self.assertEqual(result["videos"][0]["video_id"], "100")
        self.assertIn("/short/synthetic", fake.navigations)
        self.assertEqual(fake.foreign, [])

    async def test_page_cap_counts_initial_seed(self):
        fake = FakeSite()
        args = self.args()
        args.max_pages = 2
        code, result = await fetch(args, backend=fake, pacer=FastPacer())
        self.assertEqual(code, 2)
        self.assertEqual(result["exit_reason"], "video_page_budget")
        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["videos"][0]["top_count"], 1)
        self.assertNotIn("/video/200", fake.navigations)
        self.assertEqual(fake.foreign, [])

    async def test_daily_cap_refused_before_browser(self):
        counter = DailyCounter(self.root / "state")
        from dyc.runtime import atomic_text
        atomic_text(counter.state / "usage.json", json.dumps({"day": counter.today(), "count": 3000}))
        fake = FakeSite()
        code, result = await fetch(self.args(), backend=fake, pacer=FastPacer())
        self.assertEqual(code, 5)
        self.assertEqual(fake.calls, [])
        self.assertFalse((self.root / "profile").exists())

    async def test_daily_cap_during_batch_keeps_output(self):
        counter = DailyCounter(self.root / "state")
        from dyc.runtime import atomic_text
        atomic_text(counter.state / "usage.json", json.dumps({"day": counter.today(), "count": 2998}))
        fake = FakeSite()
        code, result = await fetch(self.args(), backend=fake, pacer=FastPacer())
        self.assertEqual((code, result["exit_reason"]), (2, "daily_cap"))
        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["videos"][0]["top_count"], 1)
        self.assertEqual(counter.usage(), 3000)
        self.assertEqual(len(fake.calls), 2)
        self.assertNotIn("/video/200", fake.navigations)
        self.assertEqual(fake.foreign, [])

    async def test_login_qr_mode_and_deletion_timeout_and_success(self):
        for scenario, expected in (("login-expired", 4), ("login-after", 0)):
            with self.subTest(scenario=scenario):
                fake = FakeSite(scenario)
                task = asyncio.create_task(login(self.args(), backend=fake, wait_seconds=2 if expected == 0 else 0.2))
                seen = False
                while not task.done():
                    images = list((self.root / "state").glob("*.png"))
                    for image in images:
                        seen = True
                        self.assertEqual(image.stat().st_mode & 0o777, 0o600)
                    await asyncio.sleep(0.02)
                code, result = await task
                self.assertEqual(code, expected, result)
                self.assertTrue(seen)
                self.assertFalse(Path(result["qr_path"]).exists())
                self.assertEqual(fake.foreign, [])

    async def test_login_qr_cleanup_on_cancellation(self):
        control = Control()
        fake = FakeSite("login-expired")
        task = asyncio.create_task(login(self.args(), backend=fake, control=control))
        while not list((self.root / "state").glob("*.png")):
            self.assertFalse(task.done())
            await asyncio.sleep(0.02)
        control.stop(2, "interrupted")
        code, result = await task
        self.assertEqual(code, 2)
        self.assertFalse(Path(result["qr_path"]).exists())
        self.assertEqual(fake.foreign, [])

    async def test_context_guard_aborts_unexpected_origin(self):
        fake = FakeSite()
        async with self.browser(fake) as browser:
            await browser.preflight(site.FAKE_ORIGIN)
            result = await browser.page.evaluate("""async () => {
                try { await fetch('https://unexpected.invalid/'); return 'escaped'; }
                catch (_) { return 'aborted'; }
            }""")
            self.assertEqual(result, "aborted")
        self.assertEqual(fake.foreign, ["unexpected.invalid"])


class BrowserProcessTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which("Xvfb") and not REQUIRED:
            self.skipTest("Xvfb unavailable")

    def test_selftest_one_json_and_no_real_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            real_profile = root / "must-not-touch"
            result = subprocess.run([sys.executable, "-m", "dyc", "selftest", "--out", str(root / "out"),
                                     "--profile-dir", str(real_profile), "--state-dir", str(root / "untouched-state")],
                                    cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(len(result.stdout.splitlines()), 1)
            self.assertEqual(json.loads(result.stdout)["requests"], 10)
            self.assertFalse(real_profile.exists())
            self.assertFalse((root / "untouched-state").exists())

    def test_sigterm_exports_and_reaps_owned_children(self):
        self.check_signal(signal.SIGTERM)

    def test_sigint_exports_and_reaps_owned_children(self):
        self.check_signal(signal.SIGINT)

    def check_signal(self, sig):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            proc = subprocess.Popen([sys.executable, "-m", "tests.signal_worker", temp], cwd=ROOT,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
            try:
                deadline = time.monotonic() + 20
                while not (root / "ready").exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue((root / "ready").exists(), "fixture never reached in-flight request")
                children = _processes(proc.pid)
                self.assertGreater(len(children), 3)
                start = time.monotonic()
                proc.send_signal(sig)
                stdout, stderr = proc.communicate(timeout=8)
                self.assertLess(time.monotonic() - start, 8)
                self.assertEqual(proc.returncode, 2, stdout + stderr)
                self.assertEqual(len(stdout.splitlines()), 1)
                result = json.loads(stdout)
                self.assertEqual(result["exit_reason"], "interrupted")
                self.assertEqual(result["videos"][0]["top_count"], 1)
                self.assertEqual(result["videos"][1]["status"], "partial")
                self.assertTrue((root / "out/comments.csv").exists())
                # Check the exact owned process identities, never production browser names.
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
