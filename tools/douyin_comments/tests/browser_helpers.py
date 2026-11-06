"""Event-controlled races; all traffic still uses Browser's FakeSite routes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from dyc.browser import Browser


class DiscoveryDeadline(asyncio.Event):
    """Expire discovery once the fixture has reached a specific event."""

    def __init__(self, before_expiry):
        super().__init__()
        self.before_expiry = before_expiry
        self.expired = False

    async def wait(self):
        if not self.expired:
            await self.before_expiry()
            self.expired = True
            raise asyncio.TimeoutError
        return await super().wait()


class SeedRace:
    """Hold an admitted seed until discovery expires and settlement starts.

    The real route gate, response decoding and request deadlines stay intact.
    Removing the gate or releasing it at headers instead of decoded data must
    fail the settlement assertions, even on an otherwise idle machine.
    """

    def __init__(self, case, *, root=None, late=False, hold_body=False):
        self.case, self.root, self.late = case, root, late
        self.hold_body = hold_body
        self.admitted, self.settling = asyncio.Event(), asyncio.Event()
        self.http_finished = asyncio.Event()
        self.stages = []
        race = self

        class ObservedBrowser(Browser):
            def __init__(browser, *args, **kwargs):
                super().__init__(*args, **kwargs)

                async def expire():
                    if race.late:
                        race.stages.append("discovery_waiting")
                        await browser.page.evaluate("window.sendSeed()")
                    await race.admitted.wait()
                    if hold_body:
                        await race.http_finished.wait()
                        race.stages.append("http_finished")
                    race.stages.append("discovery_expired")

                browser.seed_ready = DiscoveryDeadline(expire)

            async def finish_seeding(browser):
                if browser.video_id != "100":
                    return await super().finish_seeding()
                case.assertTrue(browser.active)
                case.assertTrue(browser.accepted)
                case.assertTrue(browser.gate.locked())
                case.assertNotIn(("100", root, "0"), browser.seed_pages)
                race.stages.append("settling")
                race.settling.set()
                await super().finish_seeding()
                case.assertEqual(browser.seed_ready.is_set(), root is None)
                case.assertIn(("100", root, "0"), browser.seed_pages)
                case.assertFalse(browser.active)
                case.assertFalse(browser.accepted)
                case.assertFalse(browser.seeding)
                race.stages.append("settled")

            async def on_response(browser, response):
                accepted = browser.accepted.get(response.request)
                first = accepted and accepted[0] == "seed" and accepted[1][1] == "100"
                if first and hold_body:
                    await race.hold()
                await super().on_response(response)
                if first:
                    race.stages.append("response_saved")

            def on_finished(browser, request):
                accepted = browser.accepted.get(request)
                if accepted and accepted[0] == "seed" and accepted[1][1] == "100":
                    race.http_finished.set()
                super().on_finished(request)

        self.browser_type = ObservedBrowser

    async def hold(self):
        self.stages.append("admitted")
        self.admitted.set()
        await self.settling.wait()
        self.stages.append("response_released")

    def assert_settled(self, *, next_video=False):
        expected = (["discovery_waiting"] if self.late else []) + ["admitted"]
        if self.hold_body:
            expected.append("http_finished")
        expected += ["discovery_expired", "settling", "response_released",
            "response_saved", "settled",
        ]
        if next_video:
            expected.append("next_video")
        self.case.assertEqual(self.stages, expected)


class ManualDeadline:
    """Use real asyncio cancellation, triggered by an event instead of a sleep."""

    def __init__(self):
        self.requested = []
        self.timers = []

    def __call__(self, deadline):
        self.requested.append(deadline)
        timer = asyncio.timeout(None)
        self.timers.append(timer)
        return timer

    def expire(self):
        self.timers[-1].reschedule(asyncio.get_running_loop().time())


def browser_asyncio(**overrides):
    # Patch only Browser's reference, never asyncio used by Playwright/unittest.
    return patch("dyc.browser.asyncio", SimpleNamespace(**(vars(asyncio) | overrides)))
