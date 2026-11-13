"""Offline tests for hub comments probe, lock priority, and verification pause."""
from __future__ import annotations

import asyncio
import csv
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools" / "douyin_comments"))

import app as app_module
import dyc.hub_comments as app_module_comments
from storage import ResultStore
from dyc.hub_comments import (
    COMMENTS_BUSY, CommentsHub, CollectJob, QtCommentTransport, _eval,
    classify_probe, lock_busy,
)
from dyc.models import Video, normalize
from dyc.runtime import Budget, DailyCounter, Pacer
from tests.helpers import Transport, comment, response


class FakeWindow:
    def __init__(self, page=None, url="https://www.douyin.com/"):
        self.url = url
        self.page = page or {
            "ready": "complete", "https": True, "host": "www.douyin.com",
            "path": "/", "account": True, "login": False, "verification": False,
        }
        self.scripts = []
        self.loaded = []
        self.cookies = []
        self.resolve_token = None

    def get_cookies(self):
        return self.cookies

    def get_current_url(self):
        return self.url

    def load_url(self, url):
        self.loaded.append(url)
        self.url = url

    def evaluate_js(self, js, arg=None):
        self.scripts.append(js)
        if app_module_comments.MARK_RESOLVE_JS in js:
            self.resolve_token = json.loads(js.rsplit(")(", 1)[1][:-1])
            return True
        if app_module_comments.ARM_JS in js:
            return True
        if "user-avatar" in js or "document.readyState" in js:
            return json.dumps(self.page)
        return "null"


class InstantPacer(Pacer):
    def __init__(self):
        super().__init__(sleep=self._sleep, rng=_ZeroRng())

    async def _sleep(self, delay):
        return None


class _ZeroRng:
    def uniform(self, a, b):
        return 0


class RedirectWindow(FakeWindow):
    def __init__(self, redirects, stale_polls=0):
        super().__init__()
        self.redirects = redirects
        self.locations = iter(())
        self.probed_locations = []
        self.stale_polls = stale_polls
        self.stale_locations = []
        self.navigating = False

    def load_url(self, url):
        assert self.resolve_token is not None
        previous_url = self.url
        super().load_url(url)
        self.url = previous_url
        self.locations = iter(self.redirects.get(url, [url]))
        self.navigating = True

    def evaluate_js(self, js, arg=None):
        resolving = app_module_comments.RESOLVE_JS in js
        if resolving or js == app_module_comments.PROBE_JS:
            stale = self.navigating and self.stale_polls > 0
            if stale:
                self.stale_polls -= 1
                self.stale_locations.append(self.url)
            else:
                self.url = next(self.locations, self.url)
                self.resolve_token = None
            parsed = urlsplit(self.url)
            self.page.update(https=parsed.scheme == "https", host=parsed.hostname,
                             path=parsed.path, modal_id=parse_qs(parsed.query).get("modal_id", [None])[0])
            self.probed_locations.append(self.url)
            if resolving and self.resolve_token == json.loads(js.rsplit(")(", 1)[1][:-1]):
                return None
        return super().evaluate_js(js, arg)


def _page(comments=None, more=False, cursor=1, **extra):
    return response(comments=comments or [comment(1)], cursor=cursor, more=more, **extra)


class ClassifyTests(unittest.TestCase):
    def test_probe_states(self):
        base = {"ready": "complete", "https": True, "host": "www.douyin.com", "path": "/"}
        self.assertEqual(classify_probe({**base, "verification": True}), "verification_required")
        self.assertEqual(classify_probe({**base, "login": True, "account": False}), "not_logged_in")
        self.assertEqual(classify_probe({**base, "account": True}), "logged_in")
        self.assertEqual(classify_probe({**base, "login": False, "account": False, "verification": False}), "not_logged_in")

    def test_lock_busy_message(self):
        self.assertEqual(lock_busy("comments")["code"], "comments_busy")
        self.assertIn("正在采集评论", lock_busy("comments")["message"])
        self.assertEqual(lock_busy("index")["code"], "busy")


class CommentsHubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.window = FakeWindow()
        self.owner = [None]
        self.lock = threading.Lock()

        def acquire(name):
            if not self.lock.acquire(blocking=False):
                return False
            self.owner[0] = name
            return True

        def release():
            self.owner[0] = None
            self.lock.release()

        self.hub = CommentsHub(
            window_factory=lambda: self.window,
            state_dir=self.tmp.name,
            sleeper=lambda _: None,
            acquire=acquire,
            release=release,
            owner=lambda: self.owner[0],
            lock_timeout=5,
        )

    def test_probe_logged_in_and_does_not_call_comment_api(self):
        result = self.hub.probe()
        self.assertEqual(result["probe"], "logged_in")
        self.assertTrue(result["ok"])
        self.assertNotIn("login_entry", result)
        probe_scripts = [script for script in self.window.scripts if "user-avatar" in script]
        self.assertTrue(probe_scripts)
        self.assertNotIn("/aweme/v1/web/comment/list/", probe_scripts[0])
        joined = "\n".join(self.window.scripts)
        self.assertNotIn("sessionid", joined)
        self.assertNotIn("sid_tt", joined)
        self.assertNotIn("sid_guard", joined)

    def test_probe_not_logged_in_points_at_index_login(self):
        self.window.page = {
            "ready": "complete", "https": True, "host": "www.douyin.com",
            "path": "/", "account": False, "login": True, "verification": False,
        }
        result = self.hub.probe()
        self.assertEqual(result["probe"], "not_logged_in")
        self.assertEqual(result["login_entry"], "index_sms")
        self.assertEqual(result["login_path"], "/api/v1/login/sms")

    def test_probe_waits_and_times_out_without_classifying_loading_page(self):
        self.window.page = {"ready": "loading", "https": True, "host": "www.douyin.com"}
        ticks = iter((0, 1, 11))
        self.hub._clock = lambda: next(ticks)
        result = self.hub.probe()
        self.assertEqual(result["code"], "browser_unavailable")
        self.assertIsNone(result["probe"])
        self.assertEqual(sum("user-avatar" in script for script in self.window.scripts), 2)

    def test_probe_waits_until_ready(self):
        loading = {"ready": "loading", "https": True, "host": "www.douyin.com"}
        snapshots = iter((json.dumps(loading), json.dumps(self.window.page)))
        original = self.window.evaluate_js
        self.window.evaluate_js = lambda js: next(snapshots) if "user-avatar" in js else original(js)
        self.assertEqual(self.hub.probe()["probe"], "logged_in")

    def test_probe_session_cookie_name_and_login_panel(self):
        self.window.page.update(account=False, login=False)
        cookie = SimpleCookie()
        cookie["sessionid"] = "synthetic"
        cookie["sessionid"]["domain"] = ".douyin.com"
        self.window.cookies = [cookie]
        self.assertEqual(self.hub.probe()["probe"], "logged_in")
        self.window.page["login"] = True
        self.assertEqual(self.hub.probe()["probe"], "not_logged_in")
        self.window.page["verification"] = True
        self.assertEqual(self.hub.probe()["probe"], "verification_required")
        self.window.page.update(login=False, verification=False)
        cookie["sessionid"]["domain"] = "creator.douyin.com"
        self.assertEqual(self.hub.probe()["probe"], "not_logged_in")

    def test_probe_does_not_navigate_during_collection(self):
        self.hub._snapshot["running"] = True
        self.window.url = "https://example.com/"
        self.assertEqual(self.hub.probe()["code"], "browser_unavailable")
        self.assertEqual(self.window.loaded, [])

    def test_waiting_for_login_is_not_waiting_for_index(self):
        self.lock.acquire()
        self.owner[0] = "login"
        async def run():
            task = asyncio.create_task(self.hub._locked_transport(Mock(), InstantPacer()).fetch("1", None, "0"))
            await asyncio.sleep(0.05)
            self.assertFalse(self.hub.status()["waiting_for_index"])
            self.hub._stop.set()
            with self.assertRaises(Exception):
                await task
        try:
            asyncio.run(run())
        finally:
            self.owner[0] = None
            self.lock.release()

    def test_start_rejected_when_not_logged_in_or_verification(self):
        for page, code in (
            ({"ready": "complete", "https": True, "host": "www.douyin.com",
              "account": False, "login": True, "verification": False}, "not_logged_in"),
            ({"ready": "complete", "https": True, "host": "www.douyin.com",
              "account": False, "login": False, "verification": True}, "verification_required"),
        ):
            with self.subTest(code=code):
                self.window.page = page
                result = self.hub.start({"videos": ["https://www.douyin.com/video/1234567890"]})
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], code)
                self.assertEqual(result.get("login_path"), "/api/v1/login/sms")

    def test_verification_stops_and_pauses_without_retry(self):
        calls = []

        class RiskTransport:
            async def fetch(self, video_id, root, cursor):
                calls.append((root, cursor))
                return {"kind": "risk"}

        self.hub._transport_factory = lambda budget, pacer: RiskTransport()
        started = self.hub.start(
            {"videos": ["https://www.douyin.com/video/1234567890"]},
            budget=Budget(DailyCounter(Path(self.tmp.name)), batch_limit=1500),
            pacer=InstantPacer(),
        )
        self.assertTrue(started["ok"])
        self.hub._worker.join(5)
        status = self.hub.status()
        self.assertFalse(status["running"])
        self.assertTrue(status["paused"])
        self.assertEqual(status["pause_reason"], "verification_required")
        self.assertEqual(len(calls), 1)

    def test_pause_and_resume_reprobe(self):
        self.hub.pause()
        status = self.hub.status()
        self.assertTrue(status["paused"])
        self.assertEqual(status["pause_reason"], "user")
        resumed = self.hub.resume()
        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["probe"], "logged_in")
        self.assertFalse(self.hub.status()["paused"])


    def test_pause_survives_restart(self):
        self.hub._halt(True, "verification_required")
        restored = CommentsHub(window_factory=lambda: self.window, state_dir=self.tmp.name)
        self.assertTrue(restored.status()["paused"])
        self.assertEqual(restored.status()["pause_reason"], "verification_required")
        self.assertEqual(set(json.loads((Path(self.tmp.name) / "pause.json").read_text())), {"paused", "pause_reason"})
        self.assertTrue(restored.resume()["ok"])
        self.assertFalse(CommentsHub(window_factory=lambda: self.window, state_dir=self.tmp.name).status()["paused"])

    def test_pacing_and_yield_do_not_hold_lock(self):
        hub = self.hub
        class CheckingPacer(InstantPacer):
            async def before(self):
                self_test.assertFalse(hub.lock.locked())
        self_test = self
        hub.lock = self.lock
        class RiskTransport:
            async def fetch(self, *_):
                self_test.assertTrue(hub.lock.locked())
                return {"kind": "risk"}
        hub._transport_factory = lambda budget, pacer: RiskTransport()
        hub.start({"videos": ["https://www.douyin.com/video/1234567890"]},
                  budget=Budget(DailyCounter(Path(self.tmp.name))), pacer=CheckingPacer())
        hub._worker.join(3)
        self.assertFalse(hub._worker.is_alive())
        self.assertFalse(self.lock.locked())

    def test_post_request_yield_is_one_second_outside_lock(self):
        delays = []
        async def sleep(delay):
            self.assertFalse(self.lock.locked())
            delays.append(delay)
        class Transport:
            async def fetch(self, *_):
                return {}
        with patch("dyc.hub_comments.asyncio.sleep", sleep):
            asyncio.run(self.hub._locked_transport(Transport(), InstantPacer()).fetch("1", None, "0"))
        self.assertEqual(delays, [1])

    def test_guard_injection_failure_is_reported(self):
        self.window.evaluate_js = lambda script: (_ for _ in ()).throw(RuntimeError())
        self.assertEqual(self.hub.probe()["code"], "browser_unavailable")
        self.assertFalse(self.lock.locked())

    def test_waiting_and_timeout_abort_release(self):
        self.lock.acquire()
        self.owner[0] = "index"
        self.hub.lock_timeout = 0.05
        scripts = []
        transport = QtCommentTransport(self.window, Budget(DailyCounter(Path(self.tmp.name))), InstantPacer(),
                                       evaluate=lambda js, arg=None: scripts.append(js))
        self.hub._transport_factory = lambda budget, pacer: transport
        worker = threading.Thread(target=self.hub._run, args=([Video("123", "https://www.douyin.com/video/123")],
                                  Budget(DailyCounter(Path(self.tmp.name))), InstantPacer()))
        worker.start()
        for _ in range(100):
            if self.hub.status()["waiting_for_index"]:
                break
            time.sleep(0.01)
        self.assertTrue(self.hub.status()["waiting_for_index"])
        self.owner[0] = None
        self.lock.release()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.hub.status()["waiting_for_index"])
        self.assertFalse(self.lock.locked())
        self.assertIn(app_module_comments.CLOSE_JS, scripts)

    def test_invalid_urls_are_rejected(self):
        for url in ("not-a-url", "7300000000000000001", "https://www.douyin.com/note/7300000000000000001",
                    "https://example.invalid/video/7300000000000000001", "https://www.douyin.com/"):
            with self.subTest(url=url):
                result = self.hub.start({"videos": [url]})
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "invalid_parameters")
                self.assertEqual(result["invalid_lines"], [1])
                self.assertIsNone(self.hub._worker)

    def test_video_formats_normalize_without_navigation(self):
        vid = "7300000000000000001"
        canonical = "https://www.douyin.com/video/" + vid
        for url in (canonical + "?source=synthetic", "https://douyin.com/?modal_id=" + vid,
                    "https://www.douyin.com/user/synthetic?modal_id=" + vid,
                    "合成分享文案 " + canonical + "，复制链接打开抖音",
                    "https://iesdouyin.com/share/video/" + vid + "/",
                    "https://www.iesdouyin.com/share/video/" + vid + "/?source=synthetic"):
            with self.subTest(url=url):
                videos = self.hub._videos({"videos": [url]})
                self.assertEqual([(v.video_id, v.video_url) for v in videos], [(vid, canonical)])
        self.assertEqual(self.window.loaded, [])
        self.assertEqual(self.window.scripts, [])

    def test_start_accepts_short_share_without_resolving_before_worker(self):
        short = "https://v.douyin.com/AbCd123/"
        with patch("dyc.hub_comments.threading.Thread") as worker:
            started = self.hub.start({"videos": ["合成口令 " + short + "，复制打开抖音"]})
        self.assertTrue(started["ok"])
        worker.return_value.start.assert_called_once_with()
        videos = worker.call_args.kwargs["args"][0]
        self.assertEqual([(v.video_id, v.video_url) for v in videos], [("", short)])
        self.assertEqual(self.window.loaded, [])
        self.assertFalse(any(app_module_comments.ARM_JS in js for js in self.window.scripts))

    def test_invalid_mixed_batch_reports_all_lines_without_starting_or_echoing(self):
        inputs = ["https://v.douyin.com/AbCd123/", "synthetic-private-invalid-line",
                  "https://iesdouyin.com/share/video/7300000000000000001/", "7300000000000000002"]
        with patch.object(self.hub, "probe") as probe:
            result = self.hub.start({"videos": inputs})
        self.assertEqual(result["code"], "invalid_parameters")
        self.assertEqual(result["invalid_lines"], [2, 4])
        self.assertIn("第 2、4 行无法识别为抖音视频链接", result["message"])
        self.assertNotIn(inputs[1], json.dumps(result))
        self.assertNotIn(inputs[3], json.dumps(result))
        probe.assert_not_called()
        self.assertIsNone(self.hub._worker)
        self.assertFalse(self.hub.status()["running"])

    def run_redirect_batch(self, urls, redirects, *, stale_polls=0, previous_url=None):
        self.window = RedirectWindow(redirects, stale_polls=stale_polls)
        if previous_url:
            self.window.url = previous_url
        budget = Budget(DailyCounter(Path(self.tmp.name)))
        calls = []
        real_sleep = asyncio.sleep

        async def quick_sleep(delay):
            if delay == 1:
                self.assertFalse(self.lock.locked())
            await real_sleep(0)

        async def fetch(transport, video_id, root, cursor):
            self.assertIs(transport.window, self.window)
            self.assertTrue(self.lock.locked())
            self.assertTrue(self.hub.status()["busy"])
            calls.append(video_id)
            budget.new_video()
            budget.reserve()
            return response(total=0)

        original_load = self.window.load_url
        def load(url):
            self.assertTrue(self.lock.locked())
            self.assertEqual(budget.used, len(calls))
            original_load(url)
        self.window.load_url = load
        with patch.object(QtCommentTransport, "fetch", fetch), \
                patch("dyc.hub_comments.asyncio.sleep", quick_sleep), \
                patch("dyc.hub_comments.SHORT_LINK_TIMEOUT", 0.03), \
                patch.object(self.hub, "_store_summary", wraps=self.hub._store_summary) as stored:
            result = self.hub.start({"videos": urls}, budget=budget, pacer=InstantPacer())
            self.assertTrue(result["ok"])
            self.hub._worker.join(3)
            self.assertFalse(self.hub._worker.is_alive())
        status = self.hub.status()
        self.assertFalse(status["running"])
        self.assertFalse(status["paused"])
        self.assertFalse(self.lock.locked())
        self.assertEqual(budget.used, len(calls))
        self.assertEqual(DailyCounter(Path(self.tmp.name)).usage(), len(calls))
        self.assertEqual(status["output"]["requests"], len(calls))
        summaries = stored.call_args.args[0]
        self.assertEqual(status["summary"]["statuses"], [v["status"] for v in summaries])
        self.assertEqual(status["summary"]["reasons"], [v["reason"] for v in summaries])
        self.assertEqual(status["output"]["videos"],
                         [v for v in summaries if v["status"] != "duplicate"])
        return summaries, calls

    def test_short_redirect_success_and_official_intermediates(self):
        short = "https://v.douyin.com/AbCd123/"
        vid = "7300000000000000001"
        canonical = "https://www.douyin.com/video/" + vid
        locations = [short, "https://www.douyin.com/landing", "https://iesdouyin.com/landing",
                     "https://www.iesdouyin.com/landing", canonical + "?source=synthetic"]
        videos, calls = self.run_redirect_batch([short], {short: locations})
        self.assertEqual(calls, [vid])
        self.assertEqual((videos[0]["video_id"], videos[0]["video_url"], videos[0]["status"]),
                         (vid, canonical, "complete"))
        self.assertEqual(self.window.probed_locations[1:], locations)
        self.assertEqual(self.window.loaded, [short])

    def test_short_redirect_modal_and_share_page_success(self):
        short = "https://v.douyin.com/AbCd123/"
        vid = "7300000000000000001"
        for landing in ("https://www.douyin.com/?modal_id=" + vid,
                        "https://www.iesdouyin.com/share/video/" + vid + "/"):
            with self.subTest(landing=landing):
                video = Video("", short)
                window = RedirectWindow({short: [short, landing]})
                transport = QtCommentTransport(window, Mock(), InstantPacer())
                asyncio.run(transport.resolve(video))
                self.assertEqual((video.video_id, video.video_url),
                                 (vid, "https://www.douyin.com/video/" + vid))

    def test_short_resolution_ignores_previous_document(self):
        short = "https://v.douyin.com/AbCd123/"
        old = "https://www.douyin.com/video/7300000000000000001"
        new = "https://www.douyin.com/video/7300000000000000002"
        videos, calls = self.run_redirect_batch([short], {short: [new]}, stale_polls=3, previous_url=old)
        self.assertEqual(self.window.stale_locations, [old] * 3)
        self.assertEqual(calls, ["7300000000000000002"])
        self.assertEqual((videos[0]["video_url"], videos[0]["status"]), (new, "complete"))

    def test_short_resolution_ignores_previous_home_document(self):
        short = "https://v.douyin.com/AbCd123/"
        home = "https://www.douyin.com/"
        canonical = "https://www.douyin.com/video/7300000000000000002"
        videos, calls = self.run_redirect_batch([short], {short: [canonical]},
                                                stale_polls=3, previous_url=home)
        self.assertEqual(self.window.stale_locations, [home] * 3)
        self.assertEqual(calls, ["7300000000000000002"])
        self.assertEqual((videos[0]["video_url"], videos[0]["status"]), (canonical, "complete"))

    def test_short_resolution_waits_through_blank_document(self):
        short = "https://v.douyin.com/AbCd123/"
        old = "https://www.douyin.com/video/7300000000000000001"
        canonical = "https://www.douyin.com/video/7300000000000000002"
        locations = ["about:blank", canonical]
        videos, calls = self.run_redirect_batch([short], {short: locations}, previous_url=old)
        self.assertEqual(self.window.probed_locations[1:], locations)
        self.assertEqual(calls, ["7300000000000000002"])
        self.assertEqual((videos[0]["video_url"], videos[0]["status"]), (canonical, "complete"))

    def test_short_returning_to_previous_video_is_deduplicated(self):
        short = "https://v.douyin.com/AbCd123/"
        canonical = "https://www.douyin.com/video/7300000000000000001"
        videos, calls = self.run_redirect_batch([canonical, short], {short: [canonical]},
                                                stale_polls=3, previous_url=canonical)
        self.assertEqual(self.window.stale_locations, [canonical] * 3)
        self.assertEqual(calls, ["7300000000000000001"])
        self.assertEqual((videos[1]["status"], videos[1]["reason"]), ("duplicate", "deduplicated"))

    def test_short_resolution_waits_for_new_document_ready(self):
        short = "https://v.douyin.com/AbCd123/"
        canonical = "https://www.douyin.com/video/7300000000000000002"
        window = RedirectWindow({short: [canonical, canonical]})
        transport = QtCommentTransport(window, Mock(), InstantPacer())
        evaluate = transport._evaluate
        states = iter(("loading", "complete"))

        def loading_page(js, arg=None):
            raw = evaluate(js, arg)
            if js == app_module_comments.RESOLVE_JS:
                page = json.loads(raw)
                page["ready"] = next(states)
                if page["ready"] == "loading":
                    page["path"] = "/video/7300000000000000001"
                return json.dumps(page)
            return raw

        transport._evaluate = loading_page
        video = Video("", short)
        asyncio.run(transport.resolve(video))
        self.assertEqual(video.video_url, canonical)
        self.assertEqual(window.probed_locations, [canonical, canonical])

    def test_short_resolution_requires_old_document_marker(self):
        window = FakeWindow()
        transport = QtCommentTransport(window, Mock(), InstantPacer(), evaluate=lambda *args: None)
        with self.assertRaises(app_module_comments.Stop) as caught:
            asyncio.run(transport.resolve(Video("", "https://v.douyin.com/AbCd123/")))
        self.assertEqual(caught.exception.reason, "short_link_unresolved")
        self.assertEqual(window.loaded, [])

    def test_short_timeout_only_marks_that_video_partial(self):
        short = "https://v.douyin.com/AbCd123/"
        vid = "7300000000000000002"
        videos, calls = self.run_redirect_batch(
            [short, "https://www.douyin.com/video/" + vid],
            {short: [short, "https://www.iesdouyin.com/landing"]})
        self.assertEqual(calls, [vid])
        self.assertEqual((videos[0]["video_id"], videos[0]["status"], videos[0]["reason"], videos[0]["pages"]),
                         ("", "partial", "short_link_unresolved", 0))
        self.assertEqual(videos[1]["status"], "complete")

    def test_short_untrusted_redirect_only_marks_that_video_partial(self):
        short = "https://v.douyin.com/AbCd123/"
        vid = "7300000000000000002"
        videos, calls = self.run_redirect_batch(
            [short, "https://www.douyin.com/video/" + vid],
            {short: [short, "https://evil.invalid/video/7300000000000000001",
                     "https://www.douyin.com/video/7300000000000000001"]})
        self.assertEqual(calls, [vid])
        self.assertEqual((videos[0]["status"], videos[0]["reason"], videos[0]["pages"]),
                         ("partial", "untrusted_video_redirect", 0))
        self.assertNotIn("https://www.douyin.com/video/7300000000000000001", self.window.probed_locations)
        self.assertEqual(videos[1]["status"], "complete")

    def test_short_duplicates_known_ids_before_and_after_resolution(self):
        short = "https://v.douyin.com/AbCd123/"
        other = "https://v.douyin.com/EfGh456/"
        canonical = "https://www.douyin.com/video/7300000000000000001"
        videos, calls = self.run_redirect_batch([short, canonical, other],
                                                {short: [short, canonical], other: [other, canonical]})
        self.assertEqual(calls, ["7300000000000000001"])
        self.assertEqual([(v["status"], v["reason"]) for v in videos],
                         [("duplicate", "deduplicated"), ("complete", "exhausted"),
                          ("duplicate", "deduplicated")])

    def test_two_shorts_resolving_to_same_id_are_collected_once(self):
        short = "https://v.douyin.com/AbCd123/"
        other = "https://v.douyin.com/EfGh456/"
        canonical = "https://www.douyin.com/video/7300000000000000001"
        videos, calls = self.run_redirect_batch([short, other],
                                                {short: [short, canonical], other: [other, canonical]})
        self.assertEqual(calls, ["7300000000000000001"])
        self.assertEqual([(v["status"], v["reason"]) for v in videos],
                         [("complete", "exhausted"), ("duplicate", "deduplicated")])

    def test_only_failed_shorts_use_no_comment_budget(self):
        short = "https://v.douyin.com/AbCd123/"
        videos, calls = self.run_redirect_batch([short], {short: [short]})
        self.assertEqual(calls, [])
        self.assertEqual(videos[0]["reason"], "short_link_unresolved")
        self.assertFalse((Path(self.tmp.name) / "usage.json").exists())

    def test_export_skips_duplicates_and_unresolved_comment_rows(self):
        canonical = "https://www.douyin.com/video/7300000000000000001"
        complete = Video("7300000000000000001", canonical, status="complete", reason="exhausted", pages=1)
        duplicate = Video(complete.video_id, canonical, status="duplicate", reason="deduplicated")
        unresolved = Video("", "https://v.douyin.com/AbCd123/", reason="short_link_unresolved")
        videos = [complete, duplicate, unresolved]
        for index, video in enumerate(videos, 1):
            item = normalize(comment(index))
            video.comments[item.comment_id] = item
        self.hub._store_summary([v.summary() for v in videos])
        self.hub._export(videos, 2, "partial")
        status = self.hub.status()
        self.assertEqual(status["summary"]["statuses"], ["complete", "duplicate", "partial"])
        self.assertEqual(status["summary"]["reasons"], ["exhausted", "deduplicated", "short_link_unresolved"])
        out = Path(self.tmp.name) / "runs" / status["output"]["run_id"]
        saved = json.loads((out / "run.json").read_text())
        self.assertEqual(saved["videos"], status["output"]["videos"])
        self.assertEqual(saved["videos"], [complete.summary(), unresolved.summary()])
        self.assertEqual(saved["requests"], 1)
        with (out / "comments.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([(row["video_id"], row["comment_id"]) for row in rows], [(complete.video_id, "1")])
        self.assertIn("short_link_unresolved", (out / "summary.md").read_text())
        self.assertNotIn("deduplicated", (out / "summary.md").read_text())

    def test_all_valid_formats_can_share_one_batch(self):
        short = "https://v.douyin.com/AbCd123/"
        prefix = "https://www.douyin.com/video/"
        ids = [str(7300000000000000001 + i) for i in range(5)]
        urls = [prefix + ids[0] + "?source=synthetic", "https://douyin.com/?modal_id=" + ids[1],
                "合成分享口令 " + short + "，复制打开抖音",
                "https://iesdouyin.com/share/video/" + ids[3] + "/",
                "https://www.iesdouyin.com/share/video/" + ids[4] + "/"]
        videos, calls = self.run_redirect_batch(urls, {short: [short, prefix + ids[2]]})
        self.assertEqual(calls, ids)
        self.assertEqual([v["video_url"] for v in videos], [prefix + vid for vid in ids])
        self.assertTrue(all(v["status"] == "complete" for v in videos))

    def test_short_resolution_waits_for_index_window_lock(self):
        short = "https://v.douyin.com/AbCd123/"
        canonical = "https://www.douyin.com/video/7300000000000000001"
        self.window = RedirectWindow({short: [short, canonical]})
        budget = Budget(DailyCounter(Path(self.tmp.name)))
        transport = QtCommentTransport(self.window, budget, InstantPacer())
        video = Video("", short)
        original_load = self.window.load_url
        def load(url):
            self.assertEqual(self.owner[0], "comments")
            self.assertTrue(self.hub.status()["busy"])
            original_load(url)
        self.window.load_url = load
        self.lock.acquire()
        self.owner[0] = "index"

        async def run():
            task = asyncio.create_task(self.hub._locked_transport(transport, InstantPacer()).resolve(video))
            await asyncio.sleep(0.05)
            self.assertTrue(self.hub.status()["waiting_for_index"])
            self.assertEqual(self.window.loaded, [])
            self.owner[0] = None
            self.lock.release()
            await asyncio.wait_for(task, 3)
        asyncio.run(run())
        self.assertEqual(video.video_url, canonical)
        self.assertFalse(self.hub.status()["waiting_for_index"])
        self.assertFalse(self.lock.locked())
        self.assertEqual(budget.used, 0)

    def test_evaluate_js_invokes_fetch_with_serialized_config(self):
        recorded = []

        class Window:
            def evaluate_js(self, script, callback=None):
                recorded.append((script, callback))
                return {"kind": "risk"}

        raw = _eval(Window(), "(cfg) => cfg", {"video": "123", "path": "/aweme/v1/web/comment/list/"})
        self.assertEqual(len(recorded), 1)
        script, callback = recorded[0]
        self.assertIsNone(callback)
        self.assertIn('"video": "123"', script)
        self.assertTrue(script.strip().startswith("("))

    def test_worker_does_not_release_lock_it_never_acquired(self):
        releases = []
        acquires = []

        def acquire(name):
            acquires.append(name)
            # First acquire is the probe inside start; later ones are the worker.
            if len(acquires) == 1:
                return True
            return False

        def release():
            releases.append(1)

        self.hub._acquire = acquire
        self.hub._release = release
        started = self.hub.start(
            {"videos": ["https://www.douyin.com/video/1234567890"]},
            budget=Budget(DailyCounter(Path(self.tmp.name)), batch_limit=1500),
            pacer=InstantPacer(),
        )
        self.assertTrue(started["ok"])
        self.hub.pause()
        self.hub._worker.join(5)
        self.assertEqual(len(releases), 1)

    def test_verification_exports_partial_results(self):
        class RiskTransport:
            async def fetch(self, video_id, root, cursor):
                return {"kind": "risk"}

        self.hub._transport_factory = lambda budget, pacer: RiskTransport()
        self.hub.start(
            {"videos": ["https://www.douyin.com/video/1234567890"]},
            budget=Budget(DailyCounter(Path(self.tmp.name)), batch_limit=1500),
            pacer=InstantPacer(),
        )
        self.hub._worker.join(5)
        output = self.hub.status()["output"]
        self.assertIsNotNone(output)
        self.assertIn("run_id", output)
        run = Path(self.tmp.name) / "runs" / output["run_id"]
        self.assertTrue((run / "comments.csv").is_file())
        self.assertTrue((run / "run.json").is_file())
        saved = json.loads((run / "run.json").read_text())
        self.assertEqual(saved["metadata"]["stop_reason"], "verification_widget")
        self.assertEqual(output["metadata"], saved["metadata"])
        self.assertNotIn("sessionid", json.dumps(saved))


class CollectSliceTests(unittest.IsolatedAsyncioTestCase):
    async def test_slice_releases_and_continues(self):
        video = Video("100", "https://www.douyin.com/video/100")
        pages = {
            (None, "0"): response(comments=[comment(1)], cursor=10, more=True, total=2),
            (None, 10): response(comments=[comment(2)], cursor=20, more=False, total=2),
        }
        # helpers.response uses int cursor; decode may need cursor as int? site.integer?
        transport = Transport({(None, "0"): pages[(None, "0")], (None, "10"): pages[(None, 10)]})
        # collect_slice queue uses string cursors from decode
        job = CollectJob(video, transport, page_budget=1500)
        more = await job.run_slice(1)
        self.assertTrue(more)
        self.assertEqual(len(transport.calls), 1)
        more = await job.run_slice(1)
        self.assertFalse(more)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(video.status, "complete")


class IndexBusyTests(unittest.TestCase):
    def test_comments_window_waits_for_view_before_navigation(self):
        from PySide6.QtWebEngineCore import QWebEngineScript

        api = app_module.Api()
        window = MagicMock()
        view = Mock()
        window.native.webview = None
        events = []
        dispatcher = Mock()

        def call(function):
            events.append('check')
            result = function()
            window.native.webview = view
            return result

        dispatcher.call.side_effect = call
        view.page.return_value.scripts.return_value.insert.side_effect = lambda script: events.append('insert')
        window.load_url.side_effect = lambda url: events.append('navigate')
        window.show.side_effect = lambda: events.append('show')
        create = Mock(return_value=window)
        with patch.dict(sys.modules, {'webview': Mock(create_window=create)}), \
             patch('login_driver.QtDispatcher', return_value=dispatcher), patch('app.time.sleep'):
            self.assertIs(api._comments_window(), window)

        self.assertEqual(events, ['check', 'check', 'insert', 'navigate', 'show'])
        self.assertTrue(create.call_args.kwargs['hidden'])
        self.assertEqual(len(create.call_args.args), 1)
        script = view.page.return_value.scripts.return_value.insert.call_args.args[0]
        self.assertEqual(script.name(), 'dyc-comments-guard')
        self.assertEqual(script.injectionPoint(), QWebEngineScript.InjectionPoint.DocumentCreation)
        self.assertEqual(script.worldId(), QWebEngineScript.ScriptWorldId.MainWorld.value)
        self.assertTrue(script.runsOnSubFrames())
        self.assertEqual(script.sourceCode(), app_module_comments.GUARD_JS)
        window.destroy.assert_not_called()
        window.evaluate_js.assert_not_called()

    def test_comments_window_times_out_without_view(self):
        api = app_module.Api()
        window = MagicMock()
        window.native.webview = None
        dispatcher = Mock()
        dispatcher.call.side_effect = lambda function: function()
        with patch.dict(sys.modules, {'webview': Mock(create_window=Mock(return_value=window))}), \
             patch('login_driver.QtDispatcher', return_value=dispatcher), \
             patch('app.time.monotonic', side_effect=[0, 1, 5]), patch('app.time.sleep'):
            with self.assertRaisesRegex(RuntimeError, 'document-start injection unavailable'):
                api._comments_window()
        self.assertEqual(dispatcher.call.call_count, 2)
        window.destroy.assert_called_once()
        window.load_url.assert_not_called()
        window.show.assert_not_called()
        window.evaluate_js.assert_not_called()
        self.assertIsNone(getattr(api, '_dyc_window', None))

    def test_index_returns_comments_busy_when_comments_hold_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            api = app_module.Api()
            api._store = ResultStore(Path(folder))
            self.assertTrue(api._acquire_lock("comments"))
            try:
                result = api.fetch_batch("酵素", "2025-01-07")
                self.assertEqual(result["code"], "comments_busy")
                self.assertEqual(result["message"], COMMENTS_BUSY)
                self.assertFalse(result["ok"])
            finally:
                api._release_lock()

    def test_window_factory_guard_failure_is_unavailable(self):
        api = app_module.Api()
        window = MagicMock()
        with tempfile.TemporaryDirectory() as folder, patch.dict(sys.modules, {"webview": Mock(create_window=Mock(return_value=window))}):
            with patch.object(api, "_install_comments_guard", side_effect=RuntimeError()):
                hub = CommentsHub(window_factory=api._comments_window, state_dir=folder)
                self.assertEqual(hub.probe()["code"], "browser_unavailable")
        self.assertIsNone(getattr(api, "_dyc_window", None))
        window.destroy.assert_called_once()

    def test_topic_login_reports_comments_busy(self):
        api = app_module.Api()
        self.assertTrue(api._acquire_lock("comments"))
        try:
            result = api.topic_login("start")
            self.assertEqual(result["code"], "comments_busy")
            self.assertEqual(result["state"], "comments_busy")
            self.assertFalse(result["qr_available"])
        finally:
            api._release_lock()

    def test_unlabelled_lock_still_returns_generic_busy(self):
        with tempfile.TemporaryDirectory() as folder:
            api = app_module.Api()
            api._store = ResultStore(Path(folder))
            api._lock.acquire()
            try:
                result = api.fetch_batch("酵素", "2025-01-07")
                self.assertEqual(result["code"], "busy")
                self.assertNotEqual(result["message"], COMMENTS_BUSY)
            finally:
                api._lock.release()


if __name__ == "__main__":
    unittest.main()

class PageCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_capture_and_next_page_use_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            budget = Budget(DailyCounter(Path(folder)))
            window = FakeWindow()
            actions = []
            state = {'sent': False, 'result': None}
            def evaluate(js, arg=None):
                if js == app_module_comments.ARM_JS:
                    state.update(sent=False, result=None)
                    return True
                if js == app_module_comments.PROBE_JS:
                    return json.dumps({'ready': 'complete', 'https': True, 'host': 'www.douyin.com',
                                       'path': '/video/100', 'account': True})
                if js in (app_module_comments.READ_JS, app_module_comments.CLOSE_JS):
                    return json.dumps(state)
                if js == app_module_comments.REPLAY_JS:
                    actions.append(('replay', arg['cursor']))
                    if arg['cursor'] == '0':
                        return False
                    state.update(sent=True, result=json.dumps(_page()))
                    return True
                if js == app_module_comments.SCROLL_JS:
                    actions.append(('ui', '0'))
                    state.update(sent=True, result=json.dumps(_page([comment(1)], cursor=2, more=1)))
                    return True
            transport = QtCommentTransport(window, budget, InstantPacer(), evaluate=evaluate)
            transport.video_url = 'https://www.douyin.com/video/100'
            video = Video('100', transport.video_url)
            job = CollectJob(video, transport)
            await job.run_slice(2)
            self.assertIn('1', video.comments)
            self.assertEqual(actions, [('replay', '0'), ('ui', '0'), ('replay', '2')])
            self.assertEqual((budget.used, budget.counter.usage(), video.pages), (2, 2, 2))

    async def test_sent_and_unsent_accounting(self):
        with tempfile.TemporaryDirectory() as folder:
            budget = Budget(DailyCounter(Path(folder)))
            window = FakeWindow()
            states = iter([json.dumps({'sent': True, 'result': json.dumps(_page())})])
            def evaluate(js, arg=None):
                if js == app_module_comments.PROBE_JS:
                    return json.dumps({'ready':'complete', 'https':True, 'host':'www.douyin.com', 'path':'/video/100'})
                if js == app_module_comments.ARM_JS:
                    return True
                if js in (app_module_comments.READ_JS, app_module_comments.CLOSE_JS):
                    return next(states, json.dumps({'sent': False}))
            transport = QtCommentTransport(window, budget, InstantPacer(), evaluate=evaluate)
            transport.video_url = 'https://www.douyin.com/video/100'
            video = Video('100', transport.video_url)
            job = CollectJob(video, transport)
            await job.run_slice(1)
            self.assertEqual((budget.used, budget.counter.usage(), video.pages), (1, 1, 1))
            self.assertEqual(window.loaded, [transport.video_url])
            other = Video('100', transport.video_url)
            job = CollectJob(other, transport)
            job.queue = [('1', '0')]
            with patch('dyc.hub_comments.time.monotonic', side_effect=[0, 29]):
                with self.assertRaises(Exception):
                    await job.run_slice(1)
            self.assertEqual((budget.used, budget.counter.usage(), other.pages), (1, 1, 0))

    async def test_reply_response_is_parsed_and_ingested(self):
        with tempfile.TemporaryDirectory() as folder:
            budget = Budget(DailyCounter(Path(folder)))
            def evaluate(js, arg=None):
                if js == app_module_comments.PROBE_JS:
                    return json.dumps({'ready':'complete', 'https':True, 'host':'www.douyin.com', 'path':'/video/100'})
                if js == app_module_comments.ARM_JS:
                    return True
                if js in (app_module_comments.READ_JS, app_module_comments.CLOSE_JS):
                    return json.dumps({'sent': True, 'result': json.dumps(_page([comment(2)]))})
            transport = QtCommentTransport(FakeWindow(), budget, InstantPacer(), evaluate=evaluate)
            transport.video_id = '100'
            video = Video('100', 'https://www.douyin.com/video/100')
            job = CollectJob(video, transport)
            job.queue = [('1', '0')]
            await job.run_slice(1)
            self.assertEqual(video.comments['2'].root_comment_id, '1')
            self.assertEqual((budget.used, video.pages), (1, 1))

    async def test_scroll_without_request_uses_no_quota(self):
        with tempfile.TemporaryDirectory() as folder:
            budget = Budget(DailyCounter(Path(folder)))
            window = FakeWindow()
            def evaluate(js, arg=None):
                if js == app_module_comments.ARM_JS:
                    return True
                return json.dumps({'sent': False, 'result': None})
            transport = QtCommentTransport(window, budget, InstantPacer(), evaluate=evaluate)
            transport.video_id = '100'
            transport.video_url = 'https://www.douyin.com/video/100'
            video = Video('100', transport.video_url)
            job = CollectJob(video, transport)
            with patch('dyc.hub_comments.time.monotonic', side_effect=[0, 29]):
                with self.assertRaises(Exception):
                    await job.run_slice(1)
            self.assertEqual((budget.used, budget.counter.usage(), video.pages), (0, 0, 0))
            self.assertFalse(job.metadata['request_sent'])

    async def test_direct_video_then_one_reload_with_persistent_listener(self):
        with tempfile.TemporaryDirectory() as folder:
            budget = Budget(DailyCounter(Path(folder)))
            events = []
            state = {'sent': False, 'result': None}
            class Window(FakeWindow):
                def load_url(self, url):
                    events.append(('navigate', url))
                    super().load_url(url)
            window = Window()
            def evaluate(js, arg=None):
                if js == app_module_comments.PROBE_JS:
                    events.append(('ready',))
                    return json.dumps({'ready':'complete', 'https':True, 'host':'www.douyin.com',
                                       'path':'/video/100', 'account':True})
                if js == app_module_comments.GUARD_JS:
                    events.append(('install',))
                if js == app_module_comments.ARM_JS:
                    events.append(('arm',))
                    return True
                if js in (app_module_comments.READ_JS, app_module_comments.CLOSE_JS):
                    return json.dumps(state)
                if js == app_module_comments.REPLAY_JS:
                    return False
                if js == app_module_comments.SCROLL_JS:
                    events.append(('click',))
                    return True
                if js == app_module_comments.REENTER_JS:
                    self.assertEqual(events[:4], [('navigate', 'https://www.douyin.com/video/100'),
                                                 ('ready',), ('install',), ('arm',)])
                    events.append(('reload',))
                    return True
            transport = QtCommentTransport(window, budget, InstantPacer(), evaluate=evaluate)
            transport.video_url = 'https://www.douyin.com/jingxuan?modal_id=100'
            with patch('dyc.hub_comments.time.monotonic', side_effect=[0, 0, 5, 6, 27, 29]), \
                 patch('dyc.hub_comments.asyncio.sleep', new=unittest.mock.AsyncMock()):
                result = await transport.fetch('100', None, '0')
            self.assertEqual(events.count(('reload',)), 1)
            self.assertEqual(events.count(('click',)), 2)
            self.assertEqual(result, {'kind':'no_request', 'request_sent':False})
            self.assertEqual(budget.used, 0)

    async def test_existing_video_session_reopens_video_from_feed(self):
        with tempfile.TemporaryDirectory() as folder:
            window = FakeWindow()
            def evaluate(js, arg=None):
                if js == app_module_comments.PROBE_JS:
                    return json.dumps({'ready':'complete', 'https':True, 'host':'www.douyin.com',
                                       'path':'/jingxuan', 'account':True})
                return None
            transport = QtCommentTransport(window, Budget(DailyCounter(Path(folder))), InstantPacer(), evaluate=evaluate)
            transport.video_id = '100'
            with patch('dyc.hub_comments.time.monotonic', side_effect=[0, 0, 29]), \
                 patch('dyc.hub_comments.asyncio.sleep', new=unittest.mock.AsyncMock()):
                await transport.fetch('100', None, '0')
            self.assertEqual(window.loaded, ['https://www.douyin.com/video/100'])

    async def test_sent_at_each_discovery_stage_prevents_further_actions(self):
        for stage in ('open', 'scroll', 'reenter'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as folder:
                budget = Budget(DailyCounter(Path(folder)))
                state = {'sent': stage == 'navigation', 'result': json.dumps(_page()) if stage == 'navigation' else None}
                actions = []
                def evaluate(js, arg=None):
                    if js == app_module_comments.ARM_JS:
                        return True
                    if js == app_module_comments.PROBE_JS:
                        return json.dumps({'ready': 'complete', 'https': True, 'host': 'www.douyin.com',
                                           'path': '/video/100', 'account': True})
                    if js in (app_module_comments.READ_JS, app_module_comments.CLOSE_JS):
                        return json.dumps(state)
                    if js == app_module_comments.REPLAY_JS:
                        return False
                    if js in (app_module_comments.SCROLL_JS, app_module_comments.REENTER_JS):
                        action = 'reenter' if js == app_module_comments.REENTER_JS else 'scroll' if arg['opened'] else 'open'
                        actions.append(action)
                        if action == stage:
                            state.update(sent=True, result=json.dumps(_page()))
                        return 'opened' if action == 'open' else True
                transport = QtCommentTransport(FakeWindow(), budget, InstantPacer(), evaluate=evaluate)
                transport.video_url = 'https://www.douyin.com/video/100'
                with patch('dyc.hub_comments.time.monotonic', side_effect=[0, 0, 1, 5, 6, 29]), \
                     patch('dyc.hub_comments.asyncio.sleep', new=unittest.mock.AsyncMock()):
                    result = await transport.fetch('100', None, '0')
                self.assertEqual(actions, {'navigation': [], 'open': ['open'], 'scroll': ['open', 'scroll'],
                                           'reenter': ['open', 'scroll', 'reenter', 'open']}[stage])
                self.assertEqual(result, {**_page(), 'request_sent': True})
                self.assertEqual((budget.used, budget.counter.usage(), transport.pacer.count), (1, 1, 1))

    async def test_reloaded_verification_response_does_not_click(self):
        with tempfile.TemporaryDirectory() as folder:
            state = {'sent': False, 'result': None}
            actions = []
            def evaluate(js, arg=None):
                if js == app_module_comments.PROBE_JS:
                    return json.dumps({'ready':'complete', 'https':True, 'host':'www.douyin.com',
                                       'path':'/video/100', 'account':True})
                if js == app_module_comments.ARM_JS:
                    return True
                if js in (app_module_comments.READ_JS, app_module_comments.CLOSE_JS):
                    return json.dumps(state)
                if js == app_module_comments.SCROLL_JS:
                    actions.append('click')
                    return True
                if js == app_module_comments.REENTER_JS:
                    actions.append('reload')
                    state.update(sent=True, result=json.dumps({'status':429, 'text':''}))
                    return True
                return False
            transport = QtCommentTransport(FakeWindow(), Budget(DailyCounter(Path(folder))), InstantPacer(), evaluate=evaluate)
            with patch('dyc.hub_comments.time.monotonic', side_effect=[0, 0, 5, 6]), \
                 patch('dyc.hub_comments.asyncio.sleep', new=unittest.mock.AsyncMock()):
                result = await transport.fetch('100', None, '0')
            self.assertEqual(actions, ['click', 'reload'])
            self.assertEqual(result['status'], 429)

    async def test_scroll_template_replays_first_page_without_repeating_ui(self):
        with tempfile.TemporaryDirectory() as folder:
            budget = Budget(DailyCounter(Path(folder)))
            state = {'sent': False, 'result': None}
            template = [False]
            actions = []
            def evaluate(js, arg=None):
                if js == app_module_comments.ARM_JS:
                    return True
                if js == app_module_comments.PROBE_JS:
                    return json.dumps({'ready': 'complete', 'https': True, 'host': 'www.douyin.com',
                                       'path': '/video/100', 'account': True})
                if js in (app_module_comments.READ_JS, app_module_comments.CLOSE_JS):
                    return json.dumps(state)
                if js == app_module_comments.REPLAY_JS:
                    actions.append(('replay', arg['cursor']))
                    if not template[0]:
                        return False
                    self.assertEqual((budget.used, state['sent']), (0, False))
                    state.update(sent=True, result=json.dumps(_page()))
                    return True
                if js == app_module_comments.SCROLL_JS:
                    actions.append(('scroll', ''))
                    template[0] = True
                    return True
                if js == app_module_comments.REENTER_JS:
                    self.fail('The newly captured template already enables first-page replay')
            transport = QtCommentTransport(FakeWindow(), budget, InstantPacer(), evaluate=evaluate)
            transport.video_url = 'https://www.douyin.com/video/100'
            with patch('dyc.hub_comments.asyncio.sleep', new=unittest.mock.AsyncMock()):
                result = await transport.fetch('100', None, '0')
            self.assertEqual(actions, [('replay', '0'), ('scroll', ''), ('replay', '0')])
            self.assertEqual(result, {**_page(), 'request_sent': True})
            self.assertEqual((budget.used, budget.counter.usage(), transport.calls, transport.pacer.count), (1, 1, 1, 1))

    async def test_slow_navigation_ignores_old_document_and_keeps_listening(self):
        with tempfile.TemporaryDirectory() as folder:
            budget = Budget(DailyCounter(Path(folder)))
            now = [0]
            times = iter([0, 0, 8, 21, 22, 29])
            state = {'sent': False, 'result': None}
            actions = []
            def clock():
                now[0] = next(times)
                return now[0]
            def evaluate(js, arg=None):
                if js == app_module_comments.ARM_JS:
                    return True
                if js == app_module_comments.PROBE_JS:
                    return json.dumps({'ready': 'loading' if now[0] == 8 else 'complete',
                                       'https': True, 'host': 'www.douyin.com', 'account': True,
                                       'path': '/jingxuan' if now[0] < 8 else '/video/100'})
                if js in (app_module_comments.READ_JS, app_module_comments.CLOSE_JS):
                    return json.dumps(state)
                if js == app_module_comments.REPLAY_JS:
                    actions.append(('replay', now[0]))
                    return False
                if js == app_module_comments.SCROLL_JS:
                    actions.append(('ui', now[0]))
                    state.update(sent=True, result=json.dumps(_page()))
                    return True
                if js == app_module_comments.REENTER_JS:
                    self.fail('A response arrived before reentry was needed')
            transport = QtCommentTransport(FakeWindow(), budget, InstantPacer(), evaluate=evaluate)
            transport.video_url = 'https://www.douyin.com/video/100'
            with patch('dyc.hub_comments.time.monotonic', side_effect=clock), \
                 patch('dyc.hub_comments.asyncio.sleep', new=unittest.mock.AsyncMock()):
                result = await transport.fetch('100', None, '0')
            self.assertEqual(actions, [('replay', 21), ('ui', 21)])
            self.assertEqual(result, {**_page(), 'request_sent': True})
            self.assertEqual(budget.used, 1)

    async def test_last_moment_send_is_counted_and_returned_consistently(self):
        for late in (None, _page(), {'kind': 'risk'}):
            with self.subTest(late=late), tempfile.TemporaryDirectory() as folder:
                budget = Budget(DailyCounter(Path(folder)))
                def evaluate(js, arg=None):
                    if js == app_module_comments.PROBE_JS:
                        return json.dumps({'ready':'complete', 'https':True, 'host':'www.douyin.com', 'path':'/video/100'})
                    if js == app_module_comments.ARM_JS:
                        return True
                    if js == app_module_comments.CLOSE_JS:
                        return json.dumps({'sent': True, 'result': json.dumps(late) if late else None})
                transport = QtCommentTransport(FakeWindow(), budget, InstantPacer(), evaluate=evaluate)
                transport.video_url = 'https://www.douyin.com/video/100'
                with patch('dyc.hub_comments.time.monotonic', side_effect=[0, 0, 29]), \
                     patch('dyc.hub_comments.asyncio.sleep', new=unittest.mock.AsyncMock()):
                    result = await transport.fetch('100', None, '0')
                self.assertEqual(result, {**(late or {'kind': 'timeout'}), 'request_sent': True})
                self.assertEqual((budget.used, budget.counter.usage(), transport.calls, transport.pacer.count), (1, 1, 1, 1))

    async def test_slow_reentry_keeps_remaining_navigation_time(self):
        with tempfile.TemporaryDirectory() as folder:
            budget = Budget(DailyCounter(Path(folder)))
            state = {'sent': False, 'result': None}
            pages = iter([
                {'path': '/video/100', 'ready': 'complete'},
                {'path': '/video/100', 'ready': 'complete'},
                {'path': '/video/100', 'ready': 'loading'},
                {'path': '/video/100', 'ready': 'complete'},
                {'path': '/video/100', 'ready': 'complete'},
            ])
            reentered = [False]
            def evaluate(js, arg=None):
                if js == app_module_comments.ARM_JS:
                    return True
                if js == app_module_comments.PROBE_JS:
                    return json.dumps({'https': True, 'host': 'www.douyin.com', 'account': True, **next(pages)})
                if js in (app_module_comments.READ_JS, app_module_comments.CLOSE_JS):
                    return json.dumps(state)
                if js == app_module_comments.REPLAY_JS:
                    return False
                if js == app_module_comments.SCROLL_JS:
                    if reentered[0]:
                        state.update(sent=True, result=json.dumps(_page()))
                    return True
                if js == app_module_comments.REENTER_JS:
                    reentered[0] = True
                    return True
            transport = QtCommentTransport(FakeWindow(), budget, InstantPacer(), evaluate=evaluate)
            transport.video_url = 'https://www.douyin.com/video/100'
            with patch('dyc.hub_comments.time.monotonic', side_effect=[0, 0, 5, 8, 21, 22, 29]), \
                 patch('dyc.hub_comments.asyncio.sleep', new=unittest.mock.AsyncMock()):
                result = await transport.fetch('100', None, '0')
            self.assertTrue(reentered[0])
            self.assertEqual(result, {**_page(), 'request_sent': True})
            self.assertEqual(budget.used, 1)

    async def test_reply_without_own_template_opens_thread_in_page(self):
        with tempfile.TemporaryDirectory() as folder:
            budget = Budget(DailyCounter(Path(folder)))
            state = {'sent': False, 'result': None}
            actions = []
            def evaluate(js, arg=None):
                if js == app_module_comments.ARM_JS:
                    self.assertEqual(arg, {'video': '100', 'root': '1', 'cursor': '0'})
                    return True
                if js == app_module_comments.PROBE_JS:
                    return json.dumps({'ready': 'complete', 'https': True, 'host': 'www.douyin.com',
                                       'path': '/video/100', 'account': True})
                if js in (app_module_comments.READ_JS, app_module_comments.CLOSE_JS):
                    return json.dumps(state)
                if js == app_module_comments.REPLAY_JS:
                    actions.append('missing_template')
                    return False
                if js == app_module_comments.SCROLL_JS:
                    self.assertEqual((arg['root'], arg['text'], arg['nickname']), ('1', '合成评论', '合成昵称'))
                    actions.append('expand' if arg['opened'] else 'open')
                    if arg['opened']:
                        state.update(sent=True, result=json.dumps(_page([comment(2)])))
                        return True
                    return 'opened'
                if js == app_module_comments.REENTER_JS:
                    self.fail('Reply discovery must not reload the video')
            window = FakeWindow()
            transport = QtCommentTransport(window, budget, InstantPacer(), evaluate=evaluate)
            transport.video_id = '100'
            transport.video = Video('100', 'https://www.douyin.com/video/100')
            transport.video.comments['1'] = Mock(text='合成评论', nickname='合成昵称')
            with patch('dyc.hub_comments.asyncio.sleep', new=unittest.mock.AsyncMock()):
                result = await transport.fetch('100', '1', '0')
            self.assertEqual(actions, ['missing_template', 'open', 'missing_template', 'expand'])
            self.assertEqual(result, {**_page([comment(2)]), 'request_sent': True})
            self.assertEqual(window.loaded, [])
            self.assertEqual(budget.used, 1)

    async def test_stop_metadata_redacts_values(self):
        from dyc.errors import Stop
        class Transport:
            no_retry = True
            async def fetch(self, *_):
                return {'status': 429, 'text': '{"status_code":1,"cookie":"secret","message":"signature-value","secret_body_as_key":1}', 'request_sent': True}
        video = Video('100', 'https://www.douyin.com/video/100')
        job = CollectJob(video, Transport())
        with self.assertRaises(Stop):
            await job.run_slice(1)
        self.assertEqual(job.metadata, {'stop_reason': 'rate_limited', 'http_status': 429,
                                       'response_fields': ['status_code', 'message'], 'request_sent': True})
        self.assertNotIn('secret', json.dumps(job.metadata))
        self.assertNotIn('signature-value', json.dumps(job.metadata))
        self.assertNotIn('secret_body_as_key', json.dumps(job.metadata))

    async def test_login_or_verification_after_navigation_stops_without_request(self):
        for signal, reason in (('login', 'login_expired'), ('verification', 'verification_widget')):
            with self.subTest(signal=signal), tempfile.TemporaryDirectory() as folder:
                window = FakeWindow(page={'https': True, 'host': 'www.douyin.com', 'path': '/login',
                                          'ready': 'complete', 'account': False, signal: True})
                budget = Budget(DailyCounter(Path(folder)))
                transport = QtCommentTransport(window, budget, InstantPacer(), evaluate=window.evaluate_js)
                transport.video_url = 'https://www.douyin.com/video/100'
                job = CollectJob(Video('100', transport.video_url), transport)
                from dyc.errors import Stop
                with self.assertRaises(Stop) as error:
                    await job.run_slice(1)
                self.assertEqual(error.exception.reason, reason)
                self.assertFalse(job.metadata['request_sent'])
                self.assertEqual((budget.used, budget.counter.usage(), job.video.pages), (0, 0, 0))
                for script in (app_module_comments.REPLAY_JS, app_module_comments.SCROLL_JS,
                               app_module_comments.REENTER_JS):
                    self.assertNotIn(script, window.scripts)
                self.assertIn(app_module_comments.CLOSE_JS, window.scripts)


class CommentsRunsApiTests(unittest.IsolatedAsyncioTestCase):
    """Gateway-only reads from synthetic exports; no desktop or upstream calls."""

    async def asyncSetUp(self):
        import contextlib
        from types import SimpleNamespace
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from unittest.mock import AsyncMock
        from hub_web import mount, PRINCIPAL_KEY
        from hub_store import HubError

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.env = patch.dict('os.environ', {'INDEX_HUB_STATE_DIR': str(self.state)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.runs = self.state / 'comments' / 'runs'

        @web.middleware
        async def authenticated(request, handler):
            request[PRINCIPAL_KEY] = {'admin': request.headers.get('X-Test-Role', 'admin') == 'admin', 'scopes': ['read']}
            try:
                return await handler(request)
            except HubError as exc:
                return web.json_response({'error': {'code': exc.code, 'message': str(exc)}}, status=exc.status)

        self.bridge = Mock(side_effect=AssertionError('No bridge calls for saved runs'))
        app = web.Application(middlewares=[authenticated])
        hub = SimpleNamespace(root=self.root, store=Mock(), bridge=self.bridge,
                              start=AsyncMock(), close=AsyncMock())
        mount(app, hub)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def export(self, run_id='20261113T101112', count=2):
        import csv
        from dyc.output import COLUMNS
        folder = self.runs / run_id
        folder.mkdir(parents=True)
        data = dict(run_id=run_id, out='/synthetic/private/path', exit_code=0, exit_reason='complete', requests=3,
                    user_id='synthetic-private-id', metadata={'out': 'private'}, videos=[dict(
                        video_id='100', status='complete', reason='exhausted', top_count=1, reply_count=max(0, count-1),
                        user_id='synthetic-private-id', out='private', pages=3)])
        (folder / 'run.json').write_text(json.dumps(data), encoding='utf-8')
        with (folder / 'comments.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=COLUMNS)
            writer.writeheader()
            for i in range(count):
                writer.writerow(dict(video_id='100', comment_id=str(i), level=1 if i == 0 else 2,
                                     root_comment_id='0', parent_comment_id='0', text=f'示例评论{i+1}',
                                     nickname='测试用户甲', reply_to_nickname='测试用户乙', like_count=0,
                                     user_id='synthetic-private-id', video_url='synthetic-private-video', raw_reply_id='private'))
        return folder

    async def get(self, suffix='', status=200, **kwargs):
        response = await self.client.get('/api/v1/comments/runs' + suffix, **kwargs)
        self.assertEqual(response.status, status)
        return response

    async def test_empty_and_usage_failures_are_read_only(self):
        from hub_store import TZ
        import datetime as dt
        self.assertEqual((await (await self.get()).json())['runs'], [])
        self.assertFalse((self.state / 'comments').exists())
        (self.state / 'comments').mkdir()
        usage = self.state / 'comments' / 'usage.json'
        for value, expected in [({'day': dt.datetime.now(TZ).date().isoformat(), 'count': 17}, 17),
                                ({'day': '2020-01-01', 'count': 9}, 0), ({'day': 'broken', 'count': 4}, None),
                                ({'day': '2020-01-01', 'count': -1}, None), ({'day': '2020-01-01', 'count': True}, None)]:
            usage.write_text(json.dumps(value))
            before = usage.stat()
            data = await (await self.get()).json()
            self.assertEqual(data['usage'], {'today_used': expected, 'daily_limit': 150, 'batch_limit': 25})
            self.assertEqual(usage.stat().st_mtime_ns, before.st_mtime_ns)
        usage.write_text('{')
        self.assertIsNone((await (await self.get()).json())['usage']['today_used'])
        self.assertFalse((usage.parent / '.usage.lock').exists())
        self.bridge.assert_not_called()

    async def test_list_sorted_limited_and_projected(self):
        for hour in range(22):
            self.export(f'20261113T{hour:02d}1112')
        data = await (await self.get()).json()
        self.assertEqual(len(data['runs']), 20)
        self.assertEqual([r['run_id'] for r in data['runs']], [f'20261113T{h:02d}1112' for h in range(21, 1, -1)])
        self.assertEqual(data['runs'][0]['finished_at'], '2026-11-13T21:11:12+08:00')
        self.assertEqual(set(data['runs'][0]), {'run_id', 'finished_at', 'exit_code', 'exit_reason', 'requests', 'videos'})
        self.assertEqual(set(data['runs'][0]['videos'][0]), {'video_id', 'status', 'reason', 'top_count', 'reply_count'})
        self.assertNotIn('private', json.dumps(data))
        self.bridge.assert_not_called()

    async def test_list_skips_invalid_runs_and_keeps_valid_run(self):
        self.export()
        broken = self.export('20261113T111112') / 'run.json'
        original = json.loads(broken.read_text())
        for content in ('{', json.dumps(dict(original, requests=[])),
                        json.dumps(dict(original, videos=None)),
                        json.dumps(dict(original, videos=[{'top_count': []}]))):
            with self.subTest(content=content):
                broken.write_text(content, encoding='utf-8')
                data = await (await self.get()).json()
                self.assertEqual([r['run_id'] for r in data['runs']], ['20261113T101112'])
        self.bridge.assert_not_called()

    async def test_list_skips_unreadable_run_but_not_unreadable_runs_directory(self):
        from hub_web import comments_run_bytes
        self.export()
        self.export('20261113T111112')

        def read(root, fd, run_id, filename):
            if run_id == '20261113T111112':
                raise PermissionError
            return comments_run_bytes(root, fd, run_id, filename)

        with patch('hub_web.comments_run_bytes', side_effect=read):
            data = await (await self.get()).json()
        self.assertEqual([r['run_id'] for r in data['runs']], ['20261113T101112'])
        with patch('hub_web.comments_runs_dir', side_effect=PermissionError):
            response = await self.get(status=404)
        self.assertEqual((await response.json())['error']['code'], 'not_found')
        self.bridge.assert_not_called()

    async def test_detail_truncation_projection_and_csv_headers(self):
        from hub_web import COMMENTS_FIELDS
        folder = self.export(count=2001)
        response = await self.get('/20261113T101112')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        data = await response.json()
        self.assertEqual((data['total'], data['truncated'], len(data['comments'])), (2001, True, 2000))
        self.assertEqual(set(data['comments'][0]), set(COMMENTS_FIELDS))
        self.assertEqual(data['comments'][0]['like_count'], '0')
        self.assertNotIn('private', json.dumps(data))
        response = await self.get('/20261113T101112/comments.csv')
        self.assertEqual(response.headers['Content-Type'], 'text/csv; charset=utf-8')
        self.assertEqual(response.headers['Content-Disposition'], 'attachment; filename="comments-20261113T101112.csv"')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(await response.read(), (folder / 'comments.csv').read_bytes())
        self.assertIn('user_id', (await response.read()).decode('utf-8-sig'))
        self.export('20261113T111112', count=0)
        self.assertEqual((await (await self.get('/20261113T111112')).json())['comments'], [])

    async def test_auth_invalid_ids_traversal_and_missing(self):
        self.export()
        for suffix in ('', '/20261113T101112', '/20261113T101112/comments.csv'):
            await self.get(suffix, 403, headers={'X-Test-Role': 'reader'})
        for suffix in ('/bad', '/20261313T101112', '/20261113T101112extra', '/%2e%2e%2foutside'):
            response = await self.get(suffix, 400)
            self.assertEqual((await response.json())['error']['code'], 'invalid_parameters')
        for suffix in ('/20261112T101112', '/20261112T101112/comments.csv'):
            response = await self.get(suffix, 404)
            self.assertEqual((await response.json())['error']['code'], 'not_found')

    async def test_symlinks_are_refused_at_each_component(self):
        folder = self.export()
        for name, suffix in [('run.json', ''), ('comments.csv', '/20261113T101112'),
                             ('comments.csv', '/20261113T101112/comments.csv')]:
            path = folder / name
            target = self.root / ('outside-' + name)
            path.rename(target)
            path.symlink_to(target)
            response = await self.get(suffix, 200 if name == 'run.json' else 404)
            if name == 'run.json':
                self.assertEqual((await response.json())['runs'], [])
            self.assertNotIn(str(self.root), await response.text())
            path.unlink()
            target.rename(path)
        for path in (folder, self.runs, self.runs.parent, self.state):
            target = path.with_name(path.name + '-saved')
            path.rename(target)
            path.symlink_to(target, target_is_directory=True)
            await self.get('/20261113T101112', 404)
            response = await self.get('', 200 if path == folder else 404)
            if path == folder:
                self.assertEqual((await response.json())['runs'], [])
            path.unlink()
            target.rename(path)

    async def test_size_and_corruption_errors_do_not_expose_content(self):
        from hub_web import COMMENTS_FILE_LIMIT
        folder = self.export()
        for name, suffix in [('run.json', ''), ('comments.csv', '/20261113T101112'),
                             ('comments.csv', '/20261113T101112/comments.csv')]:
            path = folder / name
            original = path.read_bytes()
            with path.open('wb') as stream:
                stream.truncate(COMMENTS_FILE_LIMIT + 1)
            response = await self.get(suffix, 200 if name == 'run.json' else 413)
            if name == 'run.json':
                self.assertEqual((await response.json())['runs'], [])
            path.write_bytes(original)
        for name, suffix in [('run.json', ''), ('comments.csv', '/20261113T101112')]:
            path = folder / name
            original = path.read_bytes()
            path.write_text('synthetic-private-malformed')
            response = await self.get(suffix, 200 if name == 'run.json' else 422)
            if name == 'run.json':
                self.assertEqual((await response.json())['runs'], [])
            self.assertNotIn('synthetic-private', await response.text())
            path.write_bytes(original)
