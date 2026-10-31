"""Batch orchestration, signal-safe export, and isolated offline smoke pipeline."""

import asyncio
from datetime import datetime
import os
from pathlib import Path
import signal
import tempfile
import uuid

from . import site
from .browser import Browser
from .errors import Control, Stop
from .models import SHANGHAI, Video
from .output import Log, export
from .paging import collect
from .runtime import Budget, DailyCounter, Pacer, private_dir, private_open


def run_id():
    return datetime.now(SHANGHAI).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:10]


def install_signals(control):
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, control.stop, 2, "interrupted")


def remove_signals():
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.remove_signal_handler(sig)


async def fetch(args, *, backend=None, pacer=None, control=None, identity=None):
    identity = identity or run_id()
    out = private_dir(args.out or Path.home() / "dyc-output" / identity)
    log = Log(out)
    control = control or Control()
    install_signals(control)
    videos, targets, seen_inputs, completed = [], [], set(), set()
    budget = None
    code, reason = 0, "complete"
    try:
        values = list(args.urls)
        if args.urls_file:
            values += [line.strip() for line in Path(args.urls_file).read_text().splitlines()
                       if line.strip() and not line.lstrip().startswith("#")]
        if not values:
            raise Stop(1, "urls_required")
        for value in values:
            target = site.parse_url(value, fake=backend is not None)
            key = target.video_id or target.url
            if key in seen_inputs:
                continue
            seen_inputs.add(key)
            targets.append(target)
            videos.append(Video(target.video_id or "", target.url))
        counter = DailyCounter(args.state_dir)
        counter.check_start()
        budget = Budget(counter, video_limit=args.max_pages)
        pacing = pacer or Pacer()
        log.event("batch_started")
        async with Browser(args.profile_dir, control, budget, pacing, display=args.display,
                           backend=backend) as browser:
            await browser.preflight(site.FAKE_ORIGIN + "/" if backend else None)
            for index, (target, video) in enumerate(zip(targets, videos)):
                control.check()
                if target.video_id and target.video_id in completed:
                    video.status, video.reason = "duplicate", "deduplicated"
                    continue
                if index:
                    await control.run(pacing.between_videos())
                try:
                    resolved, video.title, video.displayed = await browser.open_video(target.url)
                    video.video_id, video.video_url = resolved.video_id, resolved.url
                    if video.video_id in completed:
                        video.status, video.reason = "duplicate", "deduplicated"
                        continue
                    completed.add(video.video_id)
                    await collect(video, browser, args.max_pages)
                    await control.run(pacing.before())
                    log.event("video_" + video.status)
                except Stop as error:
                    video.status, video.reason = "partial", error.reason
                    if error.code == 2 and error.reason in ("short_link_unresolved", "comment_template_missing"):
                        code, reason = 2, "partial"
                        continue
                    raise
        videos = [v for v in videos if v.status != "duplicate"]
        if any(v.status != "complete" for v in videos):
            code, reason = 2, "partial"
    except Stop as error:
        code, reason = error.code, error.reason
    except Exception:
        # Playwright errors can include full URLs/headers. Do not stringify them.
        code, reason = 1, "internal_error"
    finally:
        remove_signals()
        for video in videos:
            if video.reason == "pending":
                video.reason = reason if reason != "complete" else "not_started"
            for thread in video.threads.values():
                if thread.reason == "pending":
                    thread.reason = video.reason
        log.event(reason)
        log.close()
    videos = [video for video in videos if video.status != "duplicate"]
    result = export(out, identity, videos, code, reason, budget.used if budget else 0, args.top)
    return code, result


async def login(args, *, backend=None, wait_seconds=180, control=None):
    state = private_dir(args.state_dir)
    control = control or Control()
    install_signals(control)
    qr = state / ("login-qr-" + uuid.uuid4().hex + ".png")
    log = Log(state)
    code, reason = 4, "login_timeout"
    try:
        counter = DailyCounter(state)
        async with Browser(args.profile_dir, control, Budget(counter), Pacer(),
                           display=args.display, backend=backend) as browser:
            await control.run(browser.page.goto(site.FAKE_ORIGIN if backend else site.LOGIN_URL,
                                                 wait_until="domcontentloaded"))
            current = await browser.state()
            if current["risk"]:
                raise Stop(3, "verification_widget")
            if current["inside"]:
                code, reason = 0, "logged_in"
            else:
                locator = browser.page.locator(site.SIGNALS["qr"]).first
                png = await control.run(locator.screenshot(type="png"))
                with os.fdopen(private_open(qr, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as stream:
                    stream.write(png)
                log.event("qr_ready")
                # Stdout remains exactly one final JSON line. This path is usable
                # while the command is waiting; the final JSON reports deletion.
                import sys
                print("QR PNG: " + str(qr), file=sys.stderr, flush=True)
                deadline = asyncio.get_running_loop().time() + min(wait_seconds, 180)
                while asyncio.get_running_loop().time() < deadline:
                    control.check()
                    current = await browser.state()
                    if current["risk"]:
                        raise Stop(3, "verification_widget")
                    if current["inside"]:
                        code, reason = 0, "logged_in"
                        break
                    await control.run(asyncio.sleep(0.1))
    except Stop as error:
        code, reason = error.code, error.reason
    except Exception:
        code, reason = 1, "login_error"
    finally:
        qr.unlink(missing_ok=True)
        log.event(reason)
        log.close()
        remove_signals()
    return code, dict(exit_code=code, exit_reason=reason, qr_path=str(qr), qr_deleted=True)


async def selftest(args):
    import csv
    import json
    from argparse import Namespace
    from .fake import FakeSite, FastPacer
    # Never consult or lock the real profile/state, even if overrides were supplied.
    with tempfile.TemporaryDirectory(prefix="dyc-selftest-") as temp:
        root = Path(temp)
        out = args.out or tempfile.mkdtemp(prefix="dyc-selftest-output-")
        fake = FakeSite()
        inner = Namespace(out=out, top=args.top, max_pages=1500, urls_file=None,
                          urls=[site.FAKE_ORIGIN + "/video/100", site.FAKE_ORIGIN + "/video/200"],
                          profile_dir=root / "profile", state_dir=root / "state", display=args.display)
        code, result = await fetch(inner, backend=fake, pacer=FastPacer())
        if code:
            return code, result
        with (Path(out) / "comments.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        md = (Path(out) / "summary.md").read_text()
        machine = json.loads((Path(out) / "run.json").read_text())
        valid = (len(rows) == 46 and len(fake.calls) == 10 and not fake.foreign
                 and all(v["top_count"] == 2 and v["reply_count"] == 21 for v in result["videos"])
                 and "仅CSV合成昵称" not in md and "仅CSV合成属地" not in md
                 and "990000000000000001" not in md and machine == result)
        if not valid:
            result["exit_code"], result["exit_reason"] = 1, "selftest_failed"
            from .runtime import atomic_text
            atomic_text(Path(out) / "run.json", json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            return 1, result
        return 0, result
