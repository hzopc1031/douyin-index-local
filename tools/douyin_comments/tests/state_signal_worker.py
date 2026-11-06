"""Signal fixture: real Chromium with in-memory routes and pending evaluations."""

from argparse import Namespace
import asyncio
import json
from pathlib import Path
import sys
from unittest.mock import patch

from playwright.async_api import Frame

from dyc import site
from dyc.browser import Browser, STATE_JS
from dyc.fake import FakeSite, FastPacer
from dyc.runner import fetch, login


async def main():
    root, stage = Path(sys.argv[1]), sys.argv[2]
    fake = FakeSite("normal" if stage == "preflight" else "login-expired")
    args = Namespace(urls=[site.FAKE_ORIGIN + "/video/100"], urls_file=None,
                     out=root / "out", profile_dir=root / "profile", state_dir=root / "state",
                     display=None, top=30, max_pages=1500)
    original = Frame.evaluate

    def should_hang(url):
        return url.startswith(site.FAKE_ORIGIN) and (
            stage != "login-wait" or bool(list(args.state_dir.glob("*.png"))))

    async def hung(frame, expression, arg=None):
        if expression == STATE_JS and should_hang(frame.url):
            return await original(frame, "() => new Promise(() => {})")
        return await original(frame, expression, arg)

    class ObservedBrowser(Browser):
        async def state(self, **kwargs):
            if asyncio.current_task() is not self.watcher and should_hang(self.page.url):
                (root / "ready").write_text("synthetic foreground state evaluation pending")
            return await super().state(**kwargs)

    # A long bound ensures that only the delivered signal can cause a timely
    # exit. Keep production control, watcher, browser and cleanup intact.
    with patch("dyc.runner.Browser", ObservedBrowser), patch.object(Frame, "evaluate", hung), \
            patch("dyc.browser.STATE_TIMEOUT_SECONDS", 60):
        if stage == "preflight":
            code, result = await fetch(args, backend=fake, pacer=FastPacer())
        else:
            code, result = await login(args, backend=fake)
    assert not fake.foreign and not fake.calls
    print(json.dumps(result), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
