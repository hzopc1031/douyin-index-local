from argparse import Namespace
import asyncio
import csv
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from dyc import site
from dyc.browser import Browser, REPLAY_HEADER, _processes
from dyc.errors import Control, Stop
from dyc.fake import FakeSite, FastPacer, fixture_comment, fixture_page
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

    async def test_owned_display_requires_private_authorization(self):
        fake = FakeSite()
        async with self.browser(fake) as browser:
            await browser.preflight(site.FAKE_ORIGIN)
            authority = browser.display.authority
            self.assertEqual(authority.stat().st_mode & 0o777, 0o600)
            self.assertEqual(authority.parent.stat().st_mode & 0o777, 0o700)
            command = Path(f"/proc/{browser.display.process.pid}/cmdline").read_bytes().split(b"\0")
            self.assertEqual(command[command.index(b"-auth") + 1], os.fsencode(authority))
            data = authority.read_bytes()
            fields, pos = [], 2
            for _ in range(4):
                length, = struct.unpack_from("!H", data, pos)
                pos += 2
                fields.append(data[pos:pos + length])
                pos += length
            name, cookie = fields[2:]
            self.assertEqual(name, b"MIT-MAGIC-COOKIE-1")
            self.assertEqual(len(cookie), 16)

            def connect(auth_name, auth_data):
                # A real X11 setup handshake, exclusively over the owned Unix
                # socket. No display contents or generated secrets are logged.
                header = struct.pack("<BBHHHHH", ord("l"), 0, 11, 0, len(auth_name), len(auth_data), 0)
                padded = lambda value: value + b"\0" * (-len(value) % 4)
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(2)
                    client.connect("/tmp/.X11-unix/X" + browser.display.name[1:])
                    client.sendall(header + padded(auth_name) + padded(auth_data))
                    return client.recv(1)

            self.assertEqual(await asyncio.to_thread(connect, b"", b""), b"\0")
            self.assertEqual(await asyncio.to_thread(connect, name, b"synthetic-wrong!"), b"\0")
            self.assertEqual(await asyncio.to_thread(connect, name, cookie), b"\1")
        self.assertFalse(authority.parent.exists())
        self.assertEqual(fake.foreign, [])

    async def test_page_requests_cannot_consume_collector_replay(self):
        fake = FakeSite()
        async with self.browser(fake) as browser:
            await browser.preflight(site.FAKE_ORIGIN)
            await browser.open_video(site.FAKE_ORIGIN + "/video/100")
            await browser.page.evaluate("""markerHeader => {
                const open = XMLHttpRequest.prototype.open;
                const send = XMLHttpRequest.prototype.send;
                const header = XMLHttpRequest.prototype.setRequestHeader;
                XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                    this.testURL = url;
                    return open.call(this, method, url, ...rest);
                };
                XMLHttpRequest.prototype.setRequestHeader = function(key, value) {
                    if (key === markerHeader) this.testMarker = value;
                    return header.call(this, key, value);
                };
                window.unsolicited = [];
                XMLHttpRequest.prototype.send = function(...args) {
                    if (!this.testMarker) return send.apply(this, args);
                    const collector = this;
                    (async () => {
                        for (const [video, item, marked] of [
                            ['999', null, false], ['100', null, false],
                            ['999', null, true], ['100', '999', true]
                        ]) {
                            await new Promise(resolve => {
                                const url = new URL(collector.testURL);
                                url.searchParams.set('aweme_id', video);
                                if (item) url.searchParams.set('item_id', item);
                                const extra = new XMLHttpRequest();
                                open.call(extra, 'GET', url.href, true);
                                if (marked) header.call(extra, markerHeader, collector.testMarker);
                                extra.onload = () => { window.unsolicited.push('admitted'); resolve(); };
                                extra.onerror = () => { window.unsolicited.push('blocked'); resolve(); };
                                send.call(extra);
                            });
                        }
                        send.apply(collector, args);
                    })();
                };
            }""", REPLAY_HEADER)
            result = await browser.fetch("100", None, "7")
            self.assertEqual([c["cid"] for c in site.decode_page(result)[0]], ["10002"])
            self.assertEqual(await browser.page.evaluate("window.unsolicited"), ["blocked"] * 4)
            self.assertEqual(browser.budget.used, 2)
            self.assertEqual(len(fake.calls), 2)
            self.assertEqual(fake.calls[-1][1][site.PARAM["video"]], "100")
            self.assertEqual(fake.calls[-1][1][site.PARAM["cursor"]], "7")
            self.assertIsNone(browser.expected)
        self.assertEqual(fake.foreign, [])

    async def test_request_identity_rejects_foreign_origins_and_ambiguous_parameters(self):
        fake = FakeSite()
        async with self.browser(fake) as browser:
            await browser.preflight(site.FAKE_ORIGIN)
            await browser.open_video(site.FAKE_ORIGIN + "/video/100")
            base = site.FAKE_ORIGIN + site.TOP_PATH
            for url in ("https://foreign.invalid" + site.TOP_PATH + "?aweme_id=100",
                        "http://dyc.invalid" + site.TOP_PATH + "?aweme_id=100",
                        base + "?aweme_id=100&aweme_id=999", base + "?aweme_id=100&cursor=0&cursor=1",
                        base + "extra?aweme_id=100", base + "?aweme_id=100&item_id=999"):
                with self.subTest(url=url):
                    request = Namespace(url=url, method="GET", frame=browser.page.main_frame)
                    self.assertIsNone(browser.request_key(request))
            self.assertEqual(browser.request_key(Namespace(
                url=base + "?aweme_id=100&cursor=7", method="GET", frame=browser.page.main_frame))[1:],
                ("100", "top", None, "7"))
        self.assertEqual(fake.foreign, [])

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
            self.assertIsNone(browser.expected)
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
        self.assertEqual(result["requests"], 8)
        self.assertEqual(len(fake.calls), 8)
        self.assertEqual([params[site.PARAM["cursor"]] for kind, params in fake.calls if kind == "top"],
                         ["0", "7", "0", "7"])
        with (args.out / "comments.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 46)
        self.assertEqual(len({(row["video_id"], row["comment_id"]) for row in rows}), 46)
        self.assertEqual(json.loads((args.state_dir / "usage.json").read_text())["count"], 8)
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

    async def test_failed_initial_page_still_deduplicates_resolved_video_aliases(self):
        class FailedSeed(FakeSite):
            async def deliver(self, route):
                if site.request_kind(route.request.url) == "top":
                    params = {key: values[0] for key, values in parse_qs(urlsplit(route.request.url).query).items()}
                    if params[site.PARAM["video"]] == "100":
                        self.calls.append(("top", params))
                        await route.abort("failed")
                        return
                await super().deliver(route)

        def browser(*args, **kwargs):
            return Browser(*args, timeout_ms=100, **kwargs)

        for first in ("/video/100", "/short/synthetic-first"):
            for reply_seed in (False, True):
                with self.subTest(first=first, reply_seed=reply_seed):
                    args = self.args()
                    suffix = ("direct" if first.startswith("/video") else "short") + str(reply_seed)
                    args.state_dir = self.root / ("state-" + suffix)
                    args.out = self.root / ("out-" + suffix)
                    args.urls = [site.FAKE_ORIGIN + path for path in
                                 (first, "/short/synthetic-alias", "/video/100", "/video/200")]
                    fake = FailedSeed(reply_seed=reply_seed)
                    with patch("dyc.runner.Browser", side_effect=browser):
                        code, result = await fetch(args, backend=fake, pacer=FastPacer())
                    self.assertEqual((code, result["exit_reason"]), (2, "partial"), result)
                    self.assertEqual([v["video_id"] for v in result["videos"]], ["100", "200"])
                    first_video, second_video = result["videos"]
                    self.assertEqual((first_video["status"], first_video["reason"]),
                                     ("partial", "comment_template_missing"))
                    self.assertEqual(first_video["reply_count"], 20 if reply_seed else 0)
                    self.assertEqual(second_video["status"], "complete")
                    expected_requests = 6 if reply_seed else 5
                    self.assertEqual(result["requests"], expected_requests)
                    self.assertEqual(len(fake.calls), expected_requests)
                    self.assertEqual(DailyCounter(args.state_dir).usage(), expected_requests)
                    self.assertEqual(sum(params[site.PARAM["video"]] == "100"
                                         for kind, params in fake.calls), 2 if reply_seed else 1)
                    self.assertIn("/short/synthetic-alias", fake.navigations)
                    with (args.out / "comments.csv").open(encoding="utf-8-sig", newline="") as stream:
                        rows = list(csv.DictReader(stream))
                    self.assertEqual(len(rows), 43 if reply_seed else 23)
                    self.assertEqual(len({(r["video_id"], r["comment_id"]) for r in rows}), len(rows))
                    self.assertEqual(fake.foreign, [])

    async def test_canonical_then_short_links_do_not_consume_request_budget(self):
        for cap in ("normal", "daily", "batch"):
            with self.subTest(cap=cap):
                args = self.args()
                args.state_dir = self.root / ("state-" + cap)
                args.out = self.root / ("out-" + cap)
                args.urls = [site.FAKE_ORIGIN + path for path in
                             ("/video/100", "/short/synthetic-one", "/short/synthetic-two")]
                counter = DailyCounter(args.state_dir)
                initial = 2996 if cap == "daily" else 0
                from dyc.runtime import atomic_text
                atomic_text(counter.state / "usage.json", json.dumps(
                    {"day": counter.today(), "count": initial}))
                # Leave exactly enough batch/daily budget for the unique video.
                def budget(counter, **kwargs):
                    return Budget(counter, batch_limit=4 if cap == "batch" else 1500, **kwargs)
                fake = FakeSite(reply_seed=True)
                with patch("dyc.runner.Budget", side_effect=budget):
                    code, result = await fetch(args, backend=fake, pacer=FastPacer())
                self.assertEqual((code, result["exit_reason"]), (0, "complete"), result)
                self.assertEqual(result["requests"], 4)
                self.assertEqual(len(fake.calls), 4)
                self.assertEqual(counter.usage(), initial + 4)
                self.assertEqual([v["video_id"] for v in result["videos"]], ["100"])
                self.assertEqual(result["videos"][0]["top_count"], 2)
                self.assertEqual(result["videos"][0]["reply_count"], 21)
                self.assertIn("/short/synthetic-one", fake.navigations)
                self.assertIn("/short/synthetic-two", fake.navigations)
                with (args.out / "comments.csv").open(encoding="utf-8-sig", newline="") as stream:
                    self.assertEqual(len(list(csv.DictReader(stream))), 23)
                self.assertEqual(fake.foreign, [])

    async def test_slow_seed_response_is_exported_before_next_video(self):
        class SlowSeed(FakeSite):
            async def deliver(self, route):
                if site.request_kind(route.request.url):
                    video = parse_qs(urlsplit(route.request.url).query)[site.PARAM["video"]][0]
                    self.calls.append(video)
                    if video == "100":
                        # Reproduce the original 15-second discovery / 22-second
                        # admitted-request race using the production deadlines.
                        await asyncio.sleep(16)
                    await route.fulfill(status=200, content_type="application/json",
                                        body=json.dumps(fixture_page(
                                            [fixture_comment(int(video) * 100 + 1)], 0, False, 1)))
                else:
                    await super().deliver(route)

        class BetweenVideos(FastPacer):
            async def between_videos(self):
                await asyncio.sleep(2)

        args, fake = self.args(), SlowSeed()
        code, result = await fetch(args, backend=fake, pacer=BetweenVideos())
        self.assertEqual((code, result["requests"]), (0, 2), result)
        self.assertEqual([(v["status"], v["top_count"], v["pages"]) for v in result["videos"]],
                         [("complete", 1, 1), ("complete", 1, 1)])
        with (args.out / "comments.csv").open(encoding="utf-8-sig", newline="") as stream:
            self.assertEqual([(row["video_id"], row["comment_id"]) for row in csv.DictReader(stream)],
                             [("100", "10001"), ("200", "20001")])
        self.assertEqual(fake.calls, ["100", "200"])
        self.assertEqual(fake.foreign, [])

    async def test_missing_top_seed_settles_admitted_reply_before_skip(self):
        class ReplyOnly(FakeSite):
            def html(self, video_id):
                html = super().html(video_id)
                if video_id == "100":
                    # Admit a reply near the discovery deadline, with no top
                    # response. Its body finishes after that deadline.
                    html = html.replace("seed.send();", "")
                    html = html.replace("reply.send();", "setTimeout(() => reply.send(), 1500);")
                return html

            async def deliver(self, route):
                kind = site.request_kind(route.request.url)
                if kind:
                    params = parse_qs(urlsplit(route.request.url).query)
                    video = params[site.PARAM["video"]][0]
                    if video == "100":
                        self.calls.append((kind, video))
                        await asyncio.sleep(1.2)
                        await route.fulfill(status=200, content_type="application/json",
                                            body=json.dumps(fixture_page([fixture_comment(10100)], 0, False, 1)))
                        return
                await super().deliver(route)

        instances = []
        def browser(*args, **kwargs):
            instance = Browser(*args, timeout_ms=100, **kwargs)
            instances.append(instance)
            return instance

        class CheckSettled(FastPacer):
            async def between_videos(pacer):
                self.assertFalse(instances[0].active)
                self.assertFalse(instances[0].accepted)
                self.assertFalse(instances[0].seeding)

        args, fake = self.args(), ReplyOnly(reply_seed=True)
        with patch("dyc.runner.Browser", side_effect=browser):
            code, result = await fetch(args, backend=fake, pacer=CheckSettled())
        self.assertEqual((code, result["exit_reason"]), (2, "partial"), result)
        first, second = result["videos"]
        self.assertEqual((first["reason"], first["reply_count"], first["pages"]),
                         ("comment_template_missing", 1, 1))
        self.assertEqual(second["status"], "complete")
        self.assertEqual(result["requests"], 5)
        with (args.out / "comments.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = [row for row in csv.DictReader(stream) if row["video_id"] == "100"]
        self.assertEqual([row["comment_id"] for row in rows], ["10100"])
        self.assertEqual(len(fake.calls), 5)
        self.assertEqual(fake.foreign, [])

    async def check_canonical_then_short_links(self, *, batch_limit=1500, daily_used=0):
        fake = FakeSite()
        args = self.args()
        args.urls = [site.FAKE_ORIGIN + path for path in
                     ("/video/100", "/short/synthetic-one", "/short/synthetic-two")]
        counter = DailyCounter(args.state_dir)
        from dyc.runtime import atomic_text
        atomic_text(counter.state / "usage.json", json.dumps({"day": counter.today(), "count": daily_used}))
        budget = Budget(counter, batch_limit=batch_limit)
        with patch("dyc.runner.Budget", return_value=budget):
            code, result = await fetch(args, backend=fake, pacer=FastPacer())
        self.assertEqual((code, result["exit_reason"], result["requests"]), (0, "complete", 4))
        self.assertEqual(len(result["videos"]), 1)
        video = result["videos"][0]
        self.assertEqual((video["video_id"], video["top_count"], video["reply_count"]), ("100", 2, 21))
        self.assertEqual(len(fake.calls), 4)
        self.assertEqual(counter.usage(), daily_used + 4)
        self.assertIn("/short/synthetic-one", fake.navigations)
        self.assertIn("/short/synthetic-two", fake.navigations)
        with (args.out / "comments.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 23)
        self.assertEqual({row["video_id"] for row in rows}, {"100"})
        self.assertEqual(fake.foreign, [])

    async def test_canonical_then_short_links_do_not_admit_duplicate_requests(self):
        await self.check_canonical_then_short_links()

    async def test_duplicate_short_links_at_batch_cap_keep_success(self):
        await self.check_canonical_then_short_links(batch_limit=4)

    async def test_duplicate_short_links_at_daily_cap_keep_success(self):
        await self.check_canonical_then_short_links(daily_used=2996)

    async def test_slow_initial_seed_is_exported_before_next_video(self):
        class SlowSeed(FakeSite):
            async def deliver(self, route):
                if site.request_kind(route.request.url) and not self.calls:
                    # Reproduce the real 15-second discovery / 22-second
                    # request deadline gap without altering asyncio's clock.
                    await asyncio.sleep(16)
                await super().deliver(route)
        fake = SlowSeed()
        args = self.args()
        code, result = await fetch(args, backend=fake, pacer=FastPacer())
        self.assertEqual((code, result["requests"]), (0, 8))
        self.assertEqual([(v["video_id"], v["status"], v["pages"], v["top_count"], v["reply_count"])
                          for v in result["videos"]],
                         [("100", "complete", 4, 2, 21), ("200", "complete", 4, 2, 21)])
        with (args.out / "comments.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 46)
        self.assertIn(("100", "10001"), {(r["video_id"], r["comment_id"]) for r in rows})
        self.assertEqual(len(fake.calls), 8)
        self.assertEqual(fake.foreign, [])

    async def test_late_admitted_top_seed_can_complete_after_discovery_deadline(self):
        class LateSeed(FakeSite):
            def html(self, video_id):
                return super().html(video_id).replace("seed.send();", "setTimeout(() => seed.send(), 1500);")

            async def deliver(self, route):
                if site.request_kind(route.request.url):
                    self.calls.append(route.request.url)
                    await asyncio.sleep(1.2)
                    await route.fulfill(status=200, content_type="application/json",
                                        body=json.dumps(fixture_page([fixture_comment(10001)], 0, False, 1)))
                else:
                    await super().deliver(route)

        fake = LateSeed()
        async with self.browser(fake, timeout_ms=100) as browser:
            await browser.preflight(site.FAKE_ORIGIN)
            await browser.open_video(site.FAKE_ORIGIN + "/video/100")
            self.assertFalse(browser.active)
            self.assertFalse(browser.accepted)
            self.assertFalse(browser.seeding)
            video = Video("100", site.FAKE_ORIGIN + "/video/100")
            await collect(video, browser)
            self.assertEqual((video.status, video.pages), ("complete", 1))
            self.assertEqual(list(video.comments), ["10001"])
            self.assertEqual(browser.budget.used, 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.foreign, [])

    async def test_unsettled_seed_stops_batch_without_starting_next_video(self):
        cancelled = asyncio.Event()

        class HungSeed(FakeSite):
            async def deliver(self, route):
                if site.request_kind(route.request.url):
                    self.calls.append(route.request.url)
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()
                else:
                    await super().deliver(route)

        def browser(*args, **kwargs):
            return Browser(*args, timeout_ms=100, **kwargs)

        args, fake = self.args(), HungSeed()
        with patch("dyc.runner.Browser", side_effect=browser):
            code, result = await fetch(args, backend=fake, pacer=FastPacer())
        self.assertEqual((code, result["exit_reason"], result["requests"]), (3, "unsettled_request", 1))
        self.assertTrue(cancelled.is_set())
        self.assertNotIn("/video/200", fake.navigations)
        self.assertTrue(all(v["top_count"] == 0 and v["status"] == "partial" for v in result["videos"]))
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.foreign, [])

    async def test_page_cap_counts_initial_seed(self):
        fake = FakeSite()
        args = self.args()
        args.max_pages = 2
        code, result = await fetch(args, backend=fake, pacer=FastPacer())
        self.assertEqual(code, 2)
        self.assertEqual(result["exit_reason"], "video_page_budget")
        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["videos"][0]["top_count"], 2)
        self.assertNotIn("/video/200", fake.navigations)
        self.assertEqual(fake.foreign, [])

    async def test_one_request_budget_exports_seed_comments_and_inline_replies(self):
        class InlineSeed(FakeSite):
            async def deliver(self, route):
                if site.request_kind(route.request.url):
                    self.calls.append(route.request.url)
                    root = fixture_comment(10001, 3)
                    root[site.FIELDS["inline"]] = [fixture_comment(10100)]
                    await route.fulfill(status=200, content_type="application/json",
                                        body=json.dumps(fixture_page([root], 7, True, 4)))
                else:
                    await super().deliver(route)
        args = self.args()
        args.max_pages = 1
        fake = InlineSeed()
        code, result = await fetch(args, backend=fake, pacer=FastPacer())
        self.assertEqual((code, result["exit_reason"], result["requests"]), (2, "video_page_budget", 1))
        video = result["videos"][0]
        self.assertEqual((video["top_count"], video["reply_count"], video["pages"]), (1, 1, 1))
        self.assertEqual(video["partial_threads"][0]["shortfall"], 2)
        with (args.out / "comments.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([row["comment_id"] for row in rows], ["10001", "10100"])
        self.assertTrue(all(row["thread_status"] == "partial" for row in rows))
        self.assertEqual(len(fake.calls), 1)
        self.assertNotIn("/video/200", fake.navigations)
        self.assertEqual(fake.foreign, [])

    async def test_final_seed_page_completes_without_replay(self):
        class FinalSeed(FakeSite):
            async def deliver(self, route):
                if site.request_kind(route.request.url):
                    self.calls.append(route.request.url)
                    await route.fulfill(status=200, content_type="application/json",
                                        body=json.dumps(fixture_page([fixture_comment(10001)], 0, False, 1)))
                else:
                    await super().deliver(route)
        args = self.args()
        args.max_pages, args.urls = 1, args.urls[:1]
        fake = FinalSeed()
        code, result = await fetch(args, backend=fake, pacer=FastPacer())
        self.assertEqual((code, result["requests"], result["videos"][0]["top_count"]), (0, 1, 1))
        self.assertEqual(result["videos"][0]["status"], "complete")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.foreign, [])

    async def test_seed_is_bound_to_video_and_original_cursor(self):
        class LaterSeed(FakeSite):
            def html(self, video_id):
                return super().html(video_id).replace("set(cfg.params.cursor, '0')", "set(cfg.params.cursor, '7')")
        fake = LaterSeed()
        async with self.browser(fake) as browser:
            await browser.preflight(site.FAKE_ORIGIN)
            await browser.open_video(site.FAKE_ORIGIN + "/video/100")
            self.assertIsNone(browser.take_seed("999", None, "7"))
            self.assertIsNone(browser.take_seed("100", None, "0"))
            video = Video("100", site.FAKE_ORIGIN + "/video/100")
            await collect(video, browser)
            self.assertEqual(video.status, "complete")
            self.assertEqual((video.summary()["top_count"], video.summary()["reply_count"]), (2, 21))
            self.assertEqual([params[site.PARAM["cursor"]] for kind, params in fake.calls if kind == "top"],
                             ["7", "0"])
            self.assertEqual(browser.budget.used, 4)
            self.assertEqual(browser.seed_pages, {})
        self.assertEqual(fake.foreign, [])

    async def check_off_path_top_seed(self, seed, replays, reason, top_ids, *,
                                      replies=None, reply_ids=(), max_pages=1500):
        replies = replies or {}

        class OffPathTopSeed(FakeSite):
            def html(self, video_id):
                return super().html(video_id).replace(
                    "set(cfg.params.cursor, '0')", "set(cfg.params.cursor, '19')")

            async def deliver(self, route):
                kind = site.request_kind(route.request.url)
                if not kind:
                    await super().deliver(route)
                    return
                params = parse_qs(urlsplit(route.request.url).query)
                cursor = params[site.PARAM["cursor"]][0]
                root = params.get(site.PARAM["root"], [None])[0]
                self.calls.append((kind, root, cursor))
                if kind == "top":
                    data = seed if cursor == "19" else replays[cursor]
                else:
                    data = replies[root]
                await route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

        args, fake = self.args(), OffPathTopSeed()
        args.urls, args.max_pages = args.urls[:1], max_pages
        code, result = await fetch(args, backend=fake, pacer=FastPacer())
        complete = reason == "exhausted"
        status = "complete" if complete else "partial"
        exit_reason = "complete" if complete else "partial"
        if reason == "video_page_budget":
            exit_reason = reason
        self.assertEqual((code, result["exit_reason"]), (0 if complete else 2, exit_reason))
        video, = result["videos"]
        self.assertEqual((video["status"], video["reason"], video["top_count"], video["reply_count"]),
                         (status, reason, len(top_ids), len(reply_ids)))
        self.assertEqual(fake.calls, [("top", None, "19")] +
                         [("top", None, cursor) for cursor in replays] +
                         [("reply", root, "0") for root in replies])
        # Received seeds count once, without being requested again.
        self.assertEqual((video["pages"], result["requests"]), (len(fake.calls), len(fake.calls)))
        with (args.out / "comments.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual({row["comment_id"] for row in rows if row["level"] == "1"}, set(top_ids))
        self.assertEqual({row["comment_id"] for row in rows if row["level"] == "2"}, set(reply_ids))
        if complete:
            self.assertEqual(video["partial_threads"], [])
            self.assertTrue(all(row["thread_status"] == "complete" for row in rows))
        else:
            self.assertIn(reason, (args.out / "summary.md").read_text())
        self.assertEqual(json.loads((args.out / "run.json").read_text()), result)
        self.assertEqual(fake.foreign, [])

    async def test_off_path_top_seed_follows_continuation_before_threads(self):
        await self.check_off_path_top_seed(
            fixture_page([fixture_comment(10002, 1)], 23, True),
            {"0": fixture_page([fixture_comment(10001, 1)], 7, True),
             "7": fixture_page([fixture_comment(10004)], 11),
             "23": fixture_page([fixture_comment(10003, 1)], 29)},
            "exhausted", ("10001", "10002", "10003", "10004"),
            replies={"10002": fixture_page([fixture_comment(10200)], 0),
                     "10001": fixture_page([fixture_comment(10100)], 0),
                     "10003": fixture_page([fixture_comment(10300)], 0)},
            reply_ids=("10100", "10200", "10300"))

    async def test_off_path_top_seed_validates_empty_page_with_more(self):
        await self.check_off_path_top_seed(
            fixture_page([], 23, True), {"0": fixture_page([fixture_comment(10001)], 7)},
            "empty_with_more", ("10001",))

    async def test_off_path_top_seed_validates_unchanged_cursor(self):
        await self.check_off_path_top_seed(
            fixture_page([fixture_comment(10002)], 19, True),
            {"0": fixture_page([fixture_comment(10001)], 7)},
            "cursor_unchanged", ("10001", "10002"))

    async def test_off_path_top_seed_validates_cursor_cycle(self):
        await self.check_off_path_top_seed(
            fixture_page([fixture_comment(10002)], 23, True),
            {"0": fixture_page([fixture_comment(10001)], 7),
             "23": fixture_page([fixture_comment(10003)], 19, True)},
            "cursor_cycle", ("10001", "10002", "10003"))

    async def test_off_path_top_seed_validates_duplicate_continuation(self):
        await self.check_off_path_top_seed(
            fixture_page([fixture_comment(10002)], 23, True),
            {"0": fixture_page([fixture_comment(10001)], 7),
             "23": fixture_page([fixture_comment(10002)], 29)},
            "duplicate_page", ("10001", "10002"))

    async def test_final_off_path_top_seed_allows_completion(self):
        await self.check_off_path_top_seed(
            fixture_page([fixture_comment(10002)], 23),
            {"0": fixture_page([fixture_comment(10001)], 7)},
            "exhausted", ("10001", "10002"))

    async def test_final_off_path_top_seed_keeps_main_walk_failure(self):
        await self.check_off_path_top_seed(
            fixture_page([], 23), {"0": fixture_page([fixture_comment(10001)], 0, True)},
            "cursor_unchanged", ("10001",))

    async def test_off_path_top_seed_keeps_first_failure(self):
        await self.check_off_path_top_seed(
            fixture_page([], 23, True), {"0": fixture_page([fixture_comment(10001)], 0, True)},
            "cursor_unchanged", ("10001",))

    async def test_off_path_top_seed_budget_stops_before_continuation(self):
        await self.check_off_path_top_seed(
            fixture_page([fixture_comment(10002)], 23, True),
            {"0": fixture_page([fixture_comment(10001)], 7)},
            "video_page_budget", ("10001", "10002"), max_pages=2)

    async def test_interrupted_after_seed_before_collection_still_exports(self):
        original = Browser.open_video
        async def interrupted(browser, url):
            await original(browser, url)
            browser.control.stop(2, "interrupted")
            browser.control.check()
        args = self.args()
        args.urls = [site.FAKE_ORIGIN + "/short/synthetic"]
        fake = FakeSite()
        with patch.object(Browser, "open_video", interrupted):
            code, result = await fetch(args, backend=fake, pacer=FastPacer())
        self.assertEqual((code, result["exit_reason"], result["requests"]), (2, "interrupted", 1))
        video = result["videos"][0]
        self.assertEqual((video["video_id"], video["top_count"], video["pages"]), ("100", 1, 1))
        self.assertEqual(video["status"], "partial")
        with (args.out / "comments.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([(row["video_id"], row["comment_id"]) for row in rows], [("100", "10001")])
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.foreign, [])

    async def test_reply_seed_is_preserved_when_budget_stops_top_pagination(self):
        fake = FakeSite(reply_seed=True)
        args = self.args()
        args.max_pages = 2
        code, result = await fetch(args, backend=fake, pacer=FastPacer())
        self.assertEqual((code, result["requests"]), (2, 2))
        video = result["videos"][0]
        self.assertEqual((video["top_count"], video["reply_count"]), (1, 20))
        self.assertEqual(video["partial_threads"][0]["shortfall"], 1)
        self.assertEqual(fake.foreign, [])

    async def test_reply_seed_total_overrides_stale_zero_before_completeness_check(self):
        class StaleRoot(FakeSite):
            async def deliver(self, route):
                kind = site.request_kind(route.request.url)
                if kind:
                    self.calls.append(kind)
                    comments = [fixture_comment(10001, 0)] if kind == "top" else [fixture_comment(10100)]
                    await route.fulfill(status=200, content_type="application/json",
                                        body=json.dumps(fixture_page(comments, 0, False, 3)))
                else:
                    await super().deliver(route)
        args = self.args()
        args.urls = args.urls[:1]
        args.max_pages = 2
        fake = StaleRoot(reply_seed=True)
        code, result = await fetch(args, backend=fake, pacer=FastPacer())
        self.assertEqual((code, result["requests"]), (2, 2))
        video = result["videos"][0]
        self.assertEqual((video["status"], video["top_count"], video["reply_count"]), ("partial", 1, 1))
        self.assertEqual(video["partial_threads"][0]["reason"], "reply_shortfall")
        self.assertEqual(video["partial_threads"][0]["shortfall"], 2)
        self.assertEqual(fake.calls, ["top", "reply"])
        self.assertEqual(fake.foreign, [])

    async def check_empty_reply_seed(self, more, cursor="0"):
        class EmptyReplySeed(FakeSite):
            def html(self, video_id):
                return super().html(video_id).replace(
                    "u.pathname = cfg.reply;",
                    "u.pathname = cfg.reply; u.searchParams.set(cfg.params.cursor, " + json.dumps(cursor) + ");")

            async def deliver(self, route):
                kind = site.request_kind(route.request.url)
                if kind:
                    self.calls.append(kind)
                    comments = [fixture_comment(10001, 0)] if kind == "top" else []
                    data = fixture_page(comments, 7, more if kind == "reply" else False)
                    del data[site.FIELDS["total"]]
                    await route.fulfill(status=200, content_type="application/json", body=json.dumps(data))
                else:
                    await super().deliver(route)

        args = self.args()
        args.urls, args.max_pages = args.urls[:1], 2
        fake = EmptyReplySeed(reply_seed=True)
        code, result = await fetch(args, backend=fake, pacer=FastPacer())
        self.assertEqual((code, result["exit_reason"]), (2, "partial") if more else (0, "complete"))
        video = result["videos"][0]
        self.assertEqual((video["pages"], video["top_count"], video["reply_count"]), (2, 1, 0))
        self.assertEqual(video["status"], "partial" if more else "complete")
        if more:
            self.assertEqual(video["reason"], "partial_threads")
            thread, = video["partial_threads"]
            self.assertEqual((thread["reason"], thread["reported"], thread["collected"]),
                             ("empty_with_more", 0, 0))
            self.assertIn("empty_with_more", (args.out / "summary.md").read_text())
        else:
            self.assertEqual(video["partial_threads"], [])
        with (args.out / "comments.csv").open(encoding="utf-8-sig", newline="") as stream:
            row, = list(csv.DictReader(stream))
        self.assertEqual(row["thread_status"], "partial" if more else "complete")
        self.assertEqual(json.loads((args.out / "run.json").read_text()), result)
        self.assertEqual((result["requests"], fake.calls, fake.foreign), (2, ["top", "reply"], []))

    async def test_zero_reply_root_validates_empty_seed_with_more(self):
        await self.check_empty_reply_seed(True)

    async def test_zero_reply_root_consumes_final_empty_seed_without_replay(self):
        await self.check_empty_reply_seed(False)

    async def test_zero_reply_root_validates_empty_seed_at_nonzero_cursor(self):
        await self.check_empty_reply_seed(True, "19")

    async def test_zero_reply_root_consumes_final_empty_seed_at_nonzero_cursor(self):
        await self.check_empty_reply_seed(False, "19")

    async def check_off_path_reply_seed(self, reported, seed, replays, reason, reply_count):
        class OffPathReplySeed(FakeSite):
            def html(self, video_id):
                return super().html(video_id).replace(
                    "u.pathname = cfg.reply;",
                    "u.pathname = cfg.reply; u.searchParams.set(cfg.params.cursor, '19');")

            async def deliver(self, route):
                kind = site.request_kind(route.request.url)
                if not kind:
                    await super().deliver(route)
                    return
                cursor = parse_qs(urlsplit(route.request.url).query)[site.PARAM["cursor"]][0]
                self.calls.append((kind, cursor))
                if kind == "top":
                    data = fixture_page([fixture_comment(10001, reported)], 0)
                else:
                    data = seed if cursor == "19" else replays[cursor]
                await route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

        args, fake = self.args(), OffPathReplySeed(reply_seed=True)
        args.urls = args.urls[:1]
        code, result = await fetch(args, backend=fake, pacer=FastPacer())
        complete = reason == "exhausted"
        status = "complete" if complete else "partial"
        self.assertEqual((code, result["exit_reason"]), (0, "complete") if complete else (2, "partial"))
        video, = result["videos"]
        self.assertEqual((video["status"], video["top_count"], video["reply_count"]),
                         (status, 1, reply_count))
        if complete:
            self.assertEqual(video["partial_threads"], [])
        else:
            thread, = video["partial_threads"]
            self.assertEqual((thread["reason"], thread["reported"], thread["collected"]),
                             (reason, reported, reply_count))
            self.assertIn(reason, (args.out / "summary.md").read_text())
        with (args.out / "comments.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1 + reply_count)
        self.assertTrue(all(row["thread_status"] == status for row in rows))
        self.assertEqual(json.loads((args.out / "run.json").read_text()), result)
        self.assertEqual(fake.calls, [("top", "0"), ("reply", "19")] +
                         [("reply", cursor) for cursor in replays])
        self.assertEqual((video["pages"], result["requests"]), (len(fake.calls), len(fake.calls)))
        self.assertEqual(fake.foreign, [])

    async def test_nonzero_reply_root_validates_off_path_empty_seed_with_more(self):
        await self.check_off_path_reply_seed(
            1, fixture_page([], 23, True),
            {"0": fixture_page([fixture_comment(10100)], 7)}, "empty_with_more", 1)

    async def test_unknown_reply_count_validates_off_path_empty_seed_with_more(self):
        await self.check_off_path_reply_seed(
            None, fixture_page([], 23, True),
            {"0": fixture_page([fixture_comment(10100)], 7)}, "empty_with_more", 1)

    async def test_off_path_reply_seed_validates_unchanged_cursor(self):
        await self.check_off_path_reply_seed(
            1, fixture_page([fixture_comment(10100)], 19, True),
            {"0": fixture_page([fixture_comment(10100)], 7)}, "cursor_unchanged", 1)

    async def test_off_path_reply_seed_validates_duplicate_continuation(self):
        await self.check_off_path_reply_seed(
            1, fixture_page([fixture_comment(10100)], 23, True),
            {"0": fixture_page([fixture_comment(10100)], 7),
             "23": fixture_page([fixture_comment(10100)], 29)}, "duplicate_page", 1)

    async def test_off_path_reply_seed_follows_valid_continuation(self):
        await self.check_off_path_reply_seed(
            None, fixture_page([fixture_comment(10100)], 23, True),
            {"0": fixture_page([fixture_comment(10100)], 7),
             "23": fixture_page([fixture_comment(10101)], 29)}, "exhausted", 2)

    async def test_final_off_path_reply_seed_allows_completion(self):
        await self.check_off_path_reply_seed(
            1, fixture_page([], 23),
            {"0": fixture_page([fixture_comment(10100)], 7)}, "exhausted", 1)

    async def test_final_off_path_reply_seed_keeps_main_walk_failure(self):
        await self.check_off_path_reply_seed(
            1, fixture_page([], 23),
            {"0": fixture_page([fixture_comment(10100)], 0, True)}, "cursor_unchanged", 1)

    async def test_reply_page_total_shortfall_is_exported_as_partial_batch(self):
        class Shortfall(FakeSite):
            async def deliver(self, route):
                kind = site.request_kind(route.request.url)
                if kind:
                    self.calls.append(kind)
                    comments = [fixture_comment(10001, None)] if kind == "top" else [fixture_comment(10100)]
                    await route.fulfill(status=200, content_type="application/json",
                                        body=json.dumps(fixture_page(comments, 0, False, 3)))
                else:
                    await super().deliver(route)
        args = self.args()
        args.urls = args.urls[:1]
        fake = Shortfall()
        code, result = await fetch(args, backend=fake, pacer=FastPacer())
        self.assertEqual((code, result["exit_reason"]), (2, "partial"))
        thread = result["videos"][0]["partial_threads"][0]
        self.assertEqual((thread["reported"], thread["collected"], thread["shortfall"]), (3, 1, 2))
        self.assertEqual(thread["reason"], "reply_shortfall")
        self.assertIn("报告 3，已采 1，缺口 2", (args.out / "summary.md").read_text())
        self.assertEqual(json.loads((args.out / "run.json").read_text()), result)
        self.assertEqual(fake.calls, ["top", "reply"])
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
        self.assertEqual(result["videos"][0]["top_count"], 2)
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
            self.assertEqual(json.loads(result.stdout)["requests"], 8)
            self.assertFalse(real_profile.exists())
            self.assertFalse((root / "untouched-state").exists())

    def test_sigterm_exports_and_reaps_owned_children(self):
        self.check_signal(signal.SIGTERM)

    def test_sigint_exports_and_reaps_owned_children(self):
        self.check_signal(signal.SIGINT)

    def test_sigterm_during_csv_serialization_finishes_export(self):
        self.check_export_signal(signal.SIGTERM, "serialize")

    def test_sigint_during_csv_serialization_finishes_export(self):
        self.check_export_signal(signal.SIGINT, "serialize")

    def test_sigterm_during_file_write_finishes_export(self):
        self.check_export_signal(signal.SIGTERM, "write")

    def test_sigint_during_file_write_finishes_export(self):
        self.check_export_signal(signal.SIGINT, "write")

    def check_export_signal(self, sig, stage):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            proc = subprocess.Popen([sys.executable, "-m", "tests.export_signal_worker", temp, stage],
                                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, start_new_session=True)
            try:
                deadline = time.monotonic() + 30
                while not (root / "ready").exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue((root / "ready").exists(), "fixture never reached export gate")
                # Browser cleanup must already have completed before exporting.
                self.assertEqual(set(_processes(proc.pid)), {proc.pid})
                proc.send_signal(sig)
                time.sleep(0.05)
                if proc.poll() is None:
                    proc.send_signal(sig)
                (root / "continue").write_text("finish export")
                stdout, stderr = proc.communicate(timeout=15)
                self.assertEqual(proc.returncode, 0, stdout + stderr)
                self.assertEqual(len(stdout.splitlines()), 1)
                result = json.loads(stdout)
                self.assertEqual((result["exit_code"], result["exit_reason"]), (0, "complete"))
                self.assertEqual((result["requests"], result["videos"][0]["top_count"]), (1, 30000))
                out = root / "out"
                self.assertEqual({p.name for p in out.iterdir()},
                                 {"run.log", "comments.csv", "summary.md", "run.json"})
                self.assertEqual(json.loads((out / "run.json").read_text()), result)
                self.assertIn("采集一级：30000", (out / "summary.md").read_text())
                self.assertEqual((out / "run.log").read_text().splitlines()[-1], "complete")
                with (out / "comments.csv").open(encoding="utf-8-sig", newline="") as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(len(rows), 30000)
                self.assertEqual({r["comment_id"] for r in rows}, {str(i) for i in range(1, 30001)})
                self.assertTrue(all(r["thread_status"] == "complete" for r in rows))
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate(timeout=5)

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
                authorities = []
                for pid in children:
                    command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                    if command and Path(os.fsdecode(command[0])).name == "Xvfb":
                        authorities.append(Path(os.fsdecode(command[command.index(b"-auth") + 1])))
                self.assertEqual(len(authorities), 1)
                start = time.monotonic()
                proc.send_signal(sig)
                stdout, stderr = proc.communicate(timeout=8)
                self.assertLess(time.monotonic() - start, 8)
                self.assertEqual(proc.returncode, 2, stdout + stderr)
                self.assertEqual(len(stdout.splitlines()), 1)
                result = json.loads(stdout)
                self.assertEqual(result["exit_reason"], "interrupted")
                self.assertEqual(result["videos"][0]["top_count"], 2)
                self.assertEqual(result["videos"][1]["status"], "partial")
                self.assertTrue((root / "out/comments.csv").exists())
                self.assertTrue(all(not authority.parent.exists() for authority in authorities))
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
