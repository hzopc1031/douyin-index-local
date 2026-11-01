"""Login-only UI helpers, with synthetic DOM and no real site traffic."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from dyc.errors import Control, Stop
from dyc.login_ui import prepare, snapshot, watch_http


class LoginUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_risk_interrupts_pending_scan(self):
        for status, reason in ((403, 'access_denied'), (429, 'rate_limited')):
            control = Control()
            context = Mock()
            browser = SimpleNamespace(control=control, context=context)
            watch_http(browser)
            callback = context.on.call_args.args[1]
            pending = asyncio.create_task(control.run(asyncio.Event().wait()))
            callback(SimpleNamespace(status=status, request=SimpleNamespace(resource_type='xhr')))
            with self.assertRaises(Stop) as caught:
                await asyncio.wait_for(pending, 0.5)
            self.assertEqual((caught.exception.code, caught.exception.reason), (3, reason))

    async def test_missing_qr_never_returns_full_page_as_ready(self):
        page = Mock()
        page.frames = []
        page.locator.return_value.filter.return_value.count = AsyncMock(return_value=0)
        page.screenshot = AsyncMock()
        with self.assertRaises(Stop) as caught:
            await snapshot(page)
        self.assertEqual(caught.exception.reason, 'login_qr_missing')
        page.screenshot.assert_not_awaited()

    async def test_qr_snapshot_masks_all_frame_inputs(self):
        frames = [Mock(), Mock()]
        page = Mock(frames=frames)
        locator = page.locator.return_value.filter.return_value
        locator.count = AsyncMock(return_value=1)
        locator.first.screenshot = AsyncMock(return_value=b'synthetic png')
        self.assertEqual(await snapshot(page), b'synthetic png')
        masks = locator.first.screenshot.call_args.kwargs['mask']
        self.assertEqual(len(masks), 4)
        for frame in frames:
            frame.locator.assert_called_once_with('input, textarea')
            self.assertIn(frame.locator.return_value, masks)

    async def test_prepare_checks_stop_before_clicking(self):
        control = Control()
        control.stop(3, 'verification_widget')
        page = Mock()
        with self.assertRaises(Stop):
            await prepare(SimpleNamespace(control=control, page=page))
        page.get_by_text.assert_not_called()
