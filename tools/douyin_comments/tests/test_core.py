import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from dyc.errors import Stop
from dyc.models import Video, normalize
from dyc.paging import collect
from dyc.runtime import Budget, DailyCounter, Display, Pacer, Profile, check_memory, private_dir
from dyc.site import decode_page, extract_share_url, parse_url, trusted_video_landing
from tests.helpers import Transport, comment, response


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_redirect_and_rate_limit_stop_without_retry(self):
        for result, reason in (({"status": 302}, "comment_redirect"),
                               ({"kind": "opaqueredirect"}, "comment_redirect"),
                               ({"status": 429}, "rate_limited")):
            with self.subTest(result=result):
                video = Video("100", "https://dyc.invalid/video/100")
                transport = Transport({(None, "0"): result})
                with self.assertRaises(Stop) as caught:
                    await collect(video, transport)
                self.assertEqual(caught.exception.reason, reason)
                self.assertEqual((len(transport.calls), video.status, video.reason),
                                 (1, "partial", reason))

    async def run_pages(self, pages, budget=1500):
        video = Video("100", "https://dyc.invalid/video/100")
        transport = Transport(pages)
        await collect(video, transport, budget)
        return video, transport

    async def test_empty_top(self):
        video, transport = await self.run_pages({(None, "0"): response(total=0)})
        self.assertEqual((video.status, len(video.comments), len(transport.calls)), ("complete", 0, 1))

    async def test_one_and_many_top_pages(self):
        for count in (1, 4):
            with self.subTest(count=count):
                pages = {(None, str(i * 13)): response([comment(i + 1)], (i + 1) * 13, i < count - 1)
                         for i in range(count)}
                video, transport = await self.run_pages(pages)
                self.assertEqual(len(video.comments), count)
                self.assertEqual(video.status, "complete")
                self.assertEqual([c[1] for c in transport.calls], [str(i * 13) for i in range(count)])

    async def test_reply_sizes(self):
        for size in (0, 20, 21, 65):
            with self.subTest(size=size):
                pages = {(None, "0"): response([comment(1, size)])}
                for start in range(0, size, 20):
                    pages[("1", str(start))] = response(
                        [comment(100 + i) for i in range(start, min(size, start + 20))],
                        start + 20, start + 20 < size)
                video, transport = await self.run_pages(pages)
                self.assertEqual(len(video.threads["1"].ids), size)
                self.assertEqual(video.status, "complete")

    async def test_each_thread_has_independent_cursor(self):
        video, transport = await self.run_pages({
            (None, "0"): response([comment(1, 2), comment(2, 1)]),
            ("1", "0"): response([comment(10)], 19, True),
            ("1", "19"): response([comment(11)]),
            ("2", "0"): response([comment(12)]),
        })
        self.assertEqual(transport.calls, [(None, "0"), ("1", "0"), ("1", "19"), ("2", "0")])
        self.assertEqual(video.status, "complete")

    async def test_dedupe_roots_and_inline_does_not_replace_pagination(self):
        root = comment(1, 2, reply_comment=[comment(10)])
        video, transport = await self.run_pages({
            (None, "0"): response([root, root]),
            ("1", "0"): response([comment(10), comment(11)]),
        })
        self.assertEqual(len(video.comments), 3)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(video.comments["10"].source, "reply")

    async def test_stalled_and_cycle_cursors(self):
        for reason, pages in (
            ("cursor_unchanged", {(None, "0"): response([comment(1)], 0, True)}),
            ("cursor_cycle", {(None, "0"): response([comment(1)], 5, True),
                              (None, "5"): response([comment(2)], 0, True)}),
            ("empty_with_more", {(None, "0"): response([], 2, True)}),
            ("duplicate_page", {(None, "0"): response([comment(1)], 2, True),
                                (None, "2"): response([comment(1)], 3, False)}),
        ):
            with self.subTest(reason=reason):
                video, _ = await self.run_pages(pages)
                self.assertEqual((video.status, video.reason), ("partial", reason))

    async def test_shortfall(self):
        video, _ = await self.run_pages({(None, "0"): response([comment(1, 3)]),
                                         ("1", "0"): response([comment(10)])})
        self.assertEqual(video.threads["1"].summary()["shortfall"], 2)
        self.assertEqual(video.status, "partial")

    async def test_reply_page_total_recovers_missing_or_stale_root_count(self):
        for reported in (None, 1, 2):
            with self.subTest(root_reported=reported):
                video, _ = await self.run_pages({
                    (None, "0"): response([comment(1, reported)]),
                    ("1", "0"): response([comment(10)], total=3),
                })
                thread = video.threads["1"].summary()
                self.assertEqual((thread["reported"], thread["collected"], thread["shortfall"]), (3, 1, 2))
                self.assertEqual((thread["status"], thread["reason"]), ("partial", "reply_shortfall"))
                self.assertEqual((video.status, video.reason), ("partial", "partial_threads"))
                self.assertTrue(all(c.thread_status == "partial" for c in video.comments.values()))

    async def test_reply_totals_never_erase_an_observed_shortfall(self):
        for totals in ((5, 2), (5, None), (None, 5), (0, None)):
            with self.subTest(totals=totals):
                video, _ = await self.run_pages({
                    (None, "0"): response([comment(1, 4)]),
                    ("1", "0"): response([comment(10)], 20, True, total=totals[0]),
                    ("1", "20"): response([comment(11)], total=totals[1]),
                })
                expected = max(4, *(total for total in totals if total is not None))
                thread = video.threads["1"].summary()
                self.assertEqual(thread["reported"], expected)
                self.assertEqual(thread["shortfall"], expected - 2)
                self.assertEqual(thread["reason"], "reply_shortfall")

    async def test_reply_total_matching_collected_is_complete(self):
        video, _ = await self.run_pages({
            (None, "0"): response([comment(1, None)]),
            ("1", "0"): response([comment(10)], total=1),
        })
        self.assertEqual(video.status, "complete")
        self.assertEqual(video.threads["1"].reported, 1)

    async def test_video_budget_exports_collected(self):
        video = Video("1", "synthetic")
        with self.assertRaises(Stop) as ctx:
            await collect(video, Transport({(None, "0"): response([comment(1)], 7, True)}), 1)
        self.assertEqual(ctx.exception.reason, "video_page_budget")
        self.assertEqual(len(video.comments), 1)
        self.assertEqual(video.status, "partial")

    async def test_one_retry_only_for_transport_failure(self):
        for kind in ("timeout", "network"):
            video, transport = await self.run_pages({(None, "0"): [
                {"kind": kind}, response([comment(1)])]})
            self.assertEqual(len(transport.calls), 2)
            self.assertEqual(video.status, "complete")
        transport = Transport({(None, "0"): [{"kind": "timeout"}] * 3})
        with self.assertRaises(Stop) as ctx:
            await collect(Video("1", "synthetic"), transport)
        self.assertEqual((len(transport.calls), ctx.exception.code), (2, 3))

    async def test_risk_never_retries(self):
        for result in ({"status": 200, "text": "not-json"},
                       {"status": 200, "text": '{"status_code":7}'},
                       {"status": 200, "text": ""}, {"kind": "risk"}):
            transport = Transport({(None, "0"): result})
            with self.assertRaises(Stop) as ctx:
                await collect(Video("1", "synthetic"), transport)
            self.assertEqual((ctx.exception.code, len(transport.calls)), (3, 1))

    async def test_login_expired(self):
        with self.assertRaises(Stop) as ctx:
            await self.run_pages({(None, "0"): {"status": 200, "text": '{"status_code":1001}'}})
        self.assertEqual(ctx.exception.code, 4)


class SchemaTests(unittest.TestCase):
    def test_relation(self):
        item = normalize(comment(10, reply_id="1", reply_to_reply_id="9"), "1")
        self.assertEqual((item.parent_comment_id, item.root_comment_id,
                          item.raw_reply_id, item.raw_reply_to_reply_id), ("9", "1", "1", "9"))

    def test_missing_is_not_zero(self):
        item = normalize({"cid": "1", "text": ""})
        self.assertIsNone(item.like_count)
        self.assertIsNone(item.reply_count_reported)
        self.assertIsNone(item.has_image)

    def test_media_flags_distinguish_missing_empty_and_present(self):
        for fields, expected in (({}, None), ({"image_list": None, "sticker": None}, None),
                                 ({"image_list": [], "sticker": {}}, False),
                                 ({"image_list": [{"id": "synthetic-image"}],
                                   "sticker": {"id": "synthetic-sticker"}}, True)):
            with self.subTest(fields=fields):
                item = normalize({"cid": "1", **fields})
                self.assertIs(item.has_image, expected)
                self.assertIs(item.has_sticker, expected)

    def test_suspicious_json_and_verification(self):
        for body in ('{"status_code":0,"status_code":0}', '[]', '{"status_code":0,"captcha":true}',
                     '{"status_code":0,"data":{"verify_info":"challenge"}}', 'null'):
            with self.subTest(body=body), self.assertRaises(Stop) as ctx:
                decode_page({"status": 200, "text": body})
            self.assertEqual(ctx.exception.code, 3)

    def test_url_formats_and_rejections(self):
        for url in ("https://www.douyin.com/video/123", "https://douyin.com/?modal_id=123",
                    "https://www.douyin.com/user/synthetic?modal_id=123"):
            self.assertEqual(parse_url(url).video_id, "123")
        self.assertIsNone(parse_url("https://v.douyin.com/Ab1/").video_id)
        for url in ("https://douyin.com.evil/video/123", "file:///video/123",
                    "https://user@douyin.com/video/123", "https://douyin.com:888/video/123",
                    "https://douyin.com/video/123?modal_id=124", "https://douyin.com/?modal_id=1&modal_id=2"):
            with self.subTest(url=url), self.assertRaises(Stop):
                parse_url(url)

    def test_share_video_urls(self):
        vid = "7300000000000000001"
        for host in ("iesdouyin.com", "www.iesdouyin.com"):
            for suffix in ("", "/", "/?source=synthetic"):
                with self.subTest(host=host, suffix=suffix):
                    target = parse_url(f"https://{host}/share/video/{vid}{suffix}")
                    self.assertEqual((target.video_id, target.url),
                                     (vid, f"https://www.douyin.com/video/{vid}"))
        for url in (vid, "https://www.douyin.com/note/" + vid,
                    "https://www.douyin.com/note/123?modal_id=" + vid,
                    "https://iesdouyin.com/share/video/", "https://iesdouyin.com/video/" + vid,
                    "https://iesdouyin.com/share/note/" + vid,
                    "https://iesdouyin.com.evil/share/video/" + vid,
                    "https://v.douyin.com/", "https://www.douyin.com/"):
            with self.subTest(url=url), self.assertRaises(Stop):
                parse_url(url)

    def test_extract_first_share_url(self):
        short = "https://v.douyin.com/AbCd123/"
        for suffix in (" 复制此链接", "中文后缀", "，打开抖音", "。", ",next", ");", "！", "\n结束"):
            with self.subTest(suffix=suffix):
                self.assertEqual(extract_share_url("合成分享口令 " + short + suffix), short)
        long = "https://www.douyin.com/?modal_id=7300000000000000001&source=synthetic"
        self.assertEqual(extract_share_url("分享 " + long + "。"), long)
        self.assertEqual(extract_share_url(short + " https://www.douyin.com/video/123"), short)
        self.assertEqual(extract_share_url("http://v.douyin.com/AbCd123/"), "http://v.douyin.com/AbCd123/")
        self.assertEqual(extract_share_url(" 7300000000000000001 "), "7300000000000000001")
        with self.assertRaises(Stop):
            parse_url(extract_share_url("https://example.invalid/ https://www.douyin.com/video/123"))

    def test_trusted_video_landings(self):
        for host in ("douyin.com", "www.douyin.com", "v.douyin.com",
                     "iesdouyin.com", "www.iesdouyin.com"):
            with self.subTest(host=host):
                self.assertTrue(trusted_video_landing(f"https://{host}/intermediate"))
                self.assertTrue(trusted_video_landing(f"https://{host}:443/intermediate"))
                self.assertFalse(trusted_video_landing(f"http://{host}/intermediate"))
                self.assertFalse(trusted_video_landing(f"https://{host}:8443/intermediate"))
                self.assertFalse(trusted_video_landing(f"https://user@{host}/intermediate"))
        for url in ("https://douyin.com.evil/", "https://evil.invalid/", "about:blank",
                    "https://www.douyin.com:bad/", "https://[broken/"):
            with self.subTest(url=url):
                self.assertFalse(trusted_video_landing(url))
        self.assertTrue(trusted_video_landing("https://dyc.invalid/intermediate", fake=True))
        self.assertFalse(trusted_video_landing("https://dyc.invalid/intermediate"))
        self.assertFalse(trusted_video_landing("https://www.douyin.com/intermediate", fake=True))

    def test_browser_resolved_target_without_launching_browser(self):
        from types import SimpleNamespace
        from dyc.browser import Browser
        browser = SimpleNamespace(page=SimpleNamespace(url=""), backend=None)
        for url in ("https://www.douyin.com/intermediate", "https://v.douyin.com/landing/error",
                    "https://iesdouyin.com/intermediate", "https://www.iesdouyin.com/intermediate"):
            browser.page.url = url
            self.assertIsNone(Browser.resolved_target(browser))
        browser.page.url = "https://www.douyin.com/video/7300000000000000001?source=synthetic"
        self.assertEqual(Browser.resolved_target(browser).video_id, "7300000000000000001")
        for host in ("iesdouyin.com", "www.iesdouyin.com"):
            browser.page.url = f"https://{host}/share/video/7300000000000000001/"
            self.assertEqual(Browser.resolved_target(browser).url,
                             "https://www.douyin.com/video/7300000000000000001")
        browser.page.url = "https://v.douyin.com/AbCd123/"
        self.assertIsNone(Browser.resolved_target(browser).video_id)
        for url in ("https://evil.invalid/", "http://www.douyin.com/intermediate"):
            browser.page.url = url
            with self.subTest(url=url), self.assertRaises(Stop) as caught:
                Browser.resolved_target(browser)
            self.assertEqual((caught.exception.code, caught.exception.reason), (3, "untrusted_video_redirect"))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_profile_permissions_symlink_and_lock(self):
        profile = self.base / "profile"
        with Profile(profile):
            self.assertEqual(profile.stat().st_mode & 0o777, 0o700)
            with self.assertRaises(Stop) as ctx:
                with Profile(profile):
                    pass
            self.assertEqual(ctx.exception.reason, "profile_locked")
        profile.chmod(0o755)
        with self.assertRaises(Stop):
            with Profile(profile):
                pass
        link = self.base / "link"
        link.symlink_to(profile)
        with self.assertRaises(Stop):
            private_dir(link)

    def test_owner_check(self):
        with patch("dyc.runtime.os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(Stop):
                private_dir(self.base)

    def test_stale_singleton_removed_only_with_lock(self):
        path = self.base / "profile"
        with Profile(path):
            (path / "SingletonLock").symlink_to(os.uname().nodename + "-99999999")
            with self.assertRaises(Stop):
                with Profile(path):
                    pass
            self.assertTrue((path / "SingletonLock").is_symlink())
        with Profile(path):
            self.assertFalse((path / "SingletonLock").is_symlink())

    def test_memory_refusal_and_unknown(self):
        mem = self.base / "meminfo"
        mem.write_text("MemAvailable: 1000 kB\n")
        with self.assertRaises(Stop) as ctx:
            check_memory(path=mem)
        self.assertEqual(ctx.exception.reason, "low_memory")
        self.assertEqual(check_memory(minimum=0, path=mem), 1024000)
        mem.write_text("missing")
        with self.assertRaises(Stop):
            check_memory(path=mem)

    def test_live_singleton_is_never_cleared(self):
        path = private_dir(self.base / "profile")
        lock = path / "SingletonLock"
        lock.symlink_to(os.uname().nodename + "-" + str(os.getpid()))
        with self.assertRaises(Stop) as ctx:
            with Profile(path):
                pass
        self.assertEqual(ctx.exception.reason, "profile_browser_alive")
        self.assertTrue(lock.is_symlink())

    def test_caps_count_only_and_day_rollover(self):
        day = ["2026-10-31"]
        counter = DailyCounter(self.base / "state", lambda: day[0], limit=3)
        budget = Budget(counter, batch_limit=2)
        budget.reserve()
        budget.reserve()
        with self.assertRaises(Stop) as ctx:
            budget.reserve()
        self.assertEqual(ctx.exception.reason, "batch_cap")
        other = Budget(counter)
        other.reserve()
        with self.assertRaises(Stop) as ctx:
            other.reserve()
        self.assertEqual(ctx.exception.reason, "daily_cap")
        with self.assertRaises(Stop) as ctx:
            counter.check_start()
        self.assertEqual(ctx.exception.code, 5)
        self.assertEqual(json.loads((counter.state / "usage.json").read_text()),
                         {"day": "2026-10-31", "count": 3})
        day[0] = "2026-11-01"
        self.assertEqual(counter.usage(), 0)
        counter.reserve()
        self.assertEqual(counter.usage(), 1)
        self.assertEqual((counter.state / "usage.json").stat().st_mode & 0o777, 0o600)

    def test_corrupt_counter_refused(self):
        counter = DailyCounter(self.base / "state")
        path = counter.state / "usage.json"
        path.write_text('{"count":-1}')
        path.chmod(0o600)
        with self.assertRaises(Stop):
            counter.usage()

    def test_no_cap_increase(self):
        defaults = Budget(DailyCounter(self.base / "defaults"))
        self.assertEqual((defaults.limit, defaults.video_limit, defaults.counter.limit), (25, 25, 150))
        counter = DailyCounter(self.base / "state", limit=9000)
        budget = Budget(counter, batch_limit=9000)
        self.assertEqual((budget.limit, counter.limit), (1500, 3000))


class PacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_pacing_fake_clock(self):
        instant = [0]
        sleeps = []
        class RNG:
            def uniform(self, a, b):
                return (a + b) / 2
        async def sleep(delay):
            sleeps.append(delay)
            instant[0] += delay
        pacer = Pacer(clock=lambda: instant[0], sleep=sleep, rng=RNG())
        for _ in range(15):
            pacer.completed()
            await pacer.before()
        await pacer.between_videos()
        self.assertEqual(sleeps, [3] * 14 + [14.5, 30])


class DisplayCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_failure_removes_authority(self):
        display = Display()
        with patch("dyc.runtime.shutil.which", return_value="synthetic-Xvfb"), \
                patch("dyc.runtime.subprocess.Popen", side_effect=OSError):
            with self.assertRaises(OSError):
                await display.start()
        self.assertIsNotNone(display.authority)
        self.assertFalse(display.authority.parent.exists())
        await display.close()

    async def test_cancelled_start_reaps_server_and_removes_authority(self):
        display = Display()
        process = Mock()
        process.poll.side_effect = [None, None, 0]
        with patch("dyc.runtime.shutil.which", return_value="synthetic-Xvfb"), \
                patch("dyc.runtime.subprocess.Popen", return_value=process), \
                patch("dyc.runtime.asyncio.sleep", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await display.start()
        process.terminate.assert_called_once()
        self.assertFalse(display.authority.parent.exists())
