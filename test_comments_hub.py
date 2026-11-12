"""Offline tests for hub comments probe, lock priority, and verification pause."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from http.cookies import SimpleCookie
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
from dyc.models import Video
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

    def get_cookies(self):
        return self.cookies

    def get_current_url(self):
        return self.url

    def load_url(self, url):
        self.loaded.append(url)
        self.url = url

    def evaluate_js(self, js, arg=None):
        self.scripts.append(js)
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
        self.assertTrue(any("window.__dycArm = false" in script for script in scripts))

    def test_invalid_and_short_urls_are_rejected(self):
        for url in ("not-a-url", "https://v.douyin.com/AbCdeFg/"):
            with self.subTest(url=url):
                result = self.hub.start({"videos": [url]})
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "invalid_parameters")
                self.assertIsNone(self.hub._worker)

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
                if 'JSON.stringify({sent:' in js:
                    return json.dumps(state)
                if '__dycReplay &&' in js:
                    actions.append(('replay', arg['cursor']))
                    if arg['cursor'] == '0':
                        return False
                    state.update(sent=True, result=json.dumps(_page()))
                    return True
                if js == app_module_comments.SCROLL_JS:
                    actions.append(('ui', '0'))
                    state.update(sent=True, result=json.dumps(_page([comment(1)], cursor=2, more=1)))
                    return True
                if 'window.__dycSent = false' in js:
                    state.update(sent=False, result=None)
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
                if js == app_module_comments.ARM_JS:
                    return True
                if 'JSON.stringify({sent:' in js:
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
            with patch('dyc.hub_comments.time.monotonic', side_effect=[0, 21]):
                with self.assertRaises(Exception):
                    await job.run_slice(1)
            self.assertEqual((budget.used, budget.counter.usage(), other.pages), (1, 1, 0))

    async def test_reply_response_is_parsed_and_ingested(self):
        with tempfile.TemporaryDirectory() as folder:
            budget = Budget(DailyCounter(Path(folder)))
            def evaluate(js, arg=None):
                if 'JSON.stringify({sent:' in js:
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
                return json.dumps({'sent': False, 'result': None})
            transport = QtCommentTransport(window, budget, InstantPacer(), evaluate=evaluate)
            transport.video_id = '100'
            transport.video_url = 'https://www.douyin.com/video/100'
            video = Video('100', transport.video_url)
            job = CollectJob(video, transport)
            with patch('dyc.hub_comments.time.monotonic', side_effect=[0, 21]):
                with self.assertRaises(Exception):
                    await job.run_slice(1)
            self.assertEqual((budget.used, budget.counter.usage(), video.pages), (0, 0, 0))
            self.assertFalse(job.metadata['request_sent'])

    async def test_first_navigation_opens_panel_without_request_or_quota(self):
        with tempfile.TemporaryDirectory() as folder:
            budget = Budget(DailyCounter(Path(folder)))
            window = FakeWindow()
            events = []
            def evaluate(js, arg=None):
                if js == app_module_comments.GUARD_JS:
                    self.assertEqual(window.loaded, [])
                    events.append("install_guard")
                if js == app_module_comments.ARM_JS:
                    self.assertEqual(window.loaded, [])
                    self.assertEqual(events, ["install_guard"])
                    self.assertEqual(arg, {"video": "100", "root": ""})
                    events.append("arm_navigation")
                    return True
                if js == app_module_comments.PROBE_JS:
                    return json.dumps({"ready": "complete", "https": True, "host": "www.douyin.com",
                                       "path": "/video/100", "account": True})
                if js == app_module_comments.SCROLL_JS:
                    self.assertEqual(window.loaded, [transport.video_url])
                    events.append("open")
                    return True
                return json.dumps({"sent": False, "result": None})
            transport = QtCommentTransport(window, budget, InstantPacer(), evaluate=evaluate)
            transport.video_url = 'https://www.douyin.com/video/100'
            with patch('dyc.hub_comments.time.monotonic', side_effect=[0, 0, 21]), \
                 patch('dyc.hub_comments.asyncio.sleep', new=unittest.mock.AsyncMock()):
                result = await transport.fetch('100', None, '0')
            self.assertEqual(events, ["install_guard", "arm_navigation", "open"])
            self.assertEqual(result, {"kind": "no_request", "request_sent": False})
            self.assertEqual((budget.used, budget.counter.usage()), (0, 0))

    async def test_navigation_refused_when_guard_not_armed(self):
        with tempfile.TemporaryDirectory() as folder:
            window = FakeWindow()
            transport = QtCommentTransport(window, Budget(DailyCounter(Path(folder))), InstantPacer(),
                                           evaluate=lambda js, arg=None: False)
            transport.video_url = 'https://www.douyin.com/video/100'
            with self.assertRaisesRegex(RuntimeError, 'guard could not be armed'):
                await transport.fetch('100', None, '0')
            self.assertEqual(window.loaded, [])

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
