"""Human login UI only; no credential extraction or CAPTCHA automation."""

import asyncio
import re

from . import site
from .errors import Stop

QR = site.SIGNALS["qr"]
PREPARE_SECONDS = 60


async def ready_qr(page):
    candidates = page.locator(QR).filter(visible=True)
    for index in range(await candidates.count()):
        candidate = candidates.nth(index)
        if await candidate.evaluate("""e => e.tagName === 'IMG'
                ? e.complete && e.naturalWidth > 0 && e.naturalHeight > 0
                : e.tagName === 'CANVAS' && e.width > 0 && e.height > 0"""):
            return candidate
    return None


def watch_http(browser):
    def response_received(response):
        if response.status in (403, 429) and response.request.resource_type in ("document", "xhr", "fetch"):
            browser.control.stop(3, "access_denied" if response.status == 403 else "rate_limited")
    browser.context.on("response", response_received)


async def prepare(browser):
    page = browser.page
    # The header button opens a main-document panel after /jingxuan navigation.
    # SMS and QR are side by side; never click the SMS submit div or broad text.
    clicked = False
    try:
        async with asyncio.timeout(PREPARE_SECONDS):
            while True:
                browser.control.check()
                if await ready_qr(page):
                    return
                panel = page.locator(site.LOGIN_PANEL).filter(visible=True)
                if not clicked and not await panel.count():
                    login = page.get_by_role("button", name="登录", exact=True).filter(visible=True)
                    if await login.count():
                        await browser.control.run(login.first.click(timeout=3000))
                        clicked = True
                await browser.control.run(asyncio.sleep(0.25))
    except asyncio.TimeoutError:
        raise Stop(5, "login_qr_missing") from None


async def snapshot(page, *, verification=False):
    # Never include typed credentials, even if the page switches login mode.
    masks = []
    for frame in page.frames:
        masks.extend([frame.locator("input, textarea"),
                      frame.get_by_text(re.compile(r"(?:1\d{10}|1\d{2}[*•·]{4,}\d{2,4})"))])
    if not verification:
        locator = await ready_qr(page)
        if locator is not None:
            return await locator.screenshot(type="png", mask=masks, timeout=3000)
        raise Stop(5, "login_qr_missing")
    return await page.screenshot(type="png", mask=masks, timeout=3000)
