"""Human login UI only; no credential extraction or CAPTCHA automation."""

import asyncio
import re

from . import site
from .errors import Stop

QR = site.SIGNALS["qr"] + ', [class*="qrcode"], [class*="qr-code"], [class*="QRCode"], [class*="qrCode"]'


def watch_http(browser):
    def response_received(response):
        if response.status in (403, 429) and response.request.resource_type in ("document", "xhr", "fetch"):
            browser.control.stop(3, "access_denied" if response.status == 403 else "rate_limited")
    browser.context.on("response", response_received)


async def prepare(browser):
    page = browser.page
    # Let the SPA expose its normal login entry; click it at most once.
    clicked = False
    end = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < end:
        browser.control.check()
        if await page.locator(QR).filter(visible=True).count():
            return
        login = page.get_by_text("登录", exact=True).filter(visible=True)
        if not clicked and await login.count():
            await browser.control.run(login.first.click(timeout=2000))
            clicked = True
        await browser.control.run(asyncio.sleep(0.25))
    raise Stop(5, "login_qr_missing")


async def snapshot(page, *, verification=False):
    # Never include typed credentials, even if the page switches login mode.
    masks = []
    for frame in page.frames:
        masks.extend([frame.locator("input, textarea"),
                      frame.get_by_text(re.compile(r"(?:1\d{10}|1\d{2}[*•·]{4,}\d{2,4})"))])
    if not verification:
        locator = page.locator(QR).filter(visible=True)
        if await locator.count():
            return await locator.first.screenshot(type="png", mask=masks, timeout=3000)
        raise Stop(5, "login_qr_missing")
    return await page.screenshot(type="png", mask=masks, timeout=3000)
