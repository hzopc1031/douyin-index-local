"""Offline browser self-check for QR login. Opens no network page and touches no credentials.

Usage: PLAYWRIGHT_BROWSERS_PATH=... .venv/bin/python qr_browser_check.py [headed|headless]
Prints browser version, UA, navigator.webdriver; exits 2 if headed mode reports HeadlessChrome.
"""
import json
import os
import sys


def check(mode):
    if mode == 'headed' and not os.environ.get('DISPLAY'):
        return {'mode': mode, 'ok': False, 'error': 'DISPLAY unset'}
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=(mode == 'headless'))
        try:
            page = browser.new_context(locale='zh-CN').new_page()
            page.set_content('<p>offline check</p>')
            probe = page.evaluate('() => ({ua: navigator.userAgent, webdriver: navigator.webdriver})')
        finally:
            browser.close()
    headless_ua = 'HeadlessChrome' in probe['ua']
    return {'mode': mode, 'browser_version': browser.version, 'user_agent': probe['ua'],
            'webdriver': probe['webdriver'], 'headless_ua': headless_ua,
            'ok': not (mode == 'headed' and headless_ua)}


def main(argv):
    mode = argv[1] if len(argv) > 1 else 'headed'
    if mode not in ('headed', 'headless'):
        print('mode must be headed or headless', file=sys.stderr)
        return 1
    result = check(mode)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
