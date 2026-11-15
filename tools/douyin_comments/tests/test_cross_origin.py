"""Offline Chromium check: replay identity must not trigger an extra CORS preflight."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import unittest

from dyc.browser import XHR_JS
from dyc import site


@unittest.skipUnless(os.environ.get('DYC_CORS_BROWSER_TESTS') == '1', 'opt-in browser test')
class CrossOriginReplayTests(unittest.TestCase):
    def test_replay_marker_does_not_leave_browser_or_cause_preflight(self):
        from playwright.sync_api import sync_playwright
        observed = []
        class API(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_OPTIONS(self):
                observed.append(('OPTIONS', ''))
                self.send_response(204)
                self.send_header('Access-Control-Allow-Origin', self.headers.get('Origin', ''))
                self.send_header('Access-Control-Allow-Credentials', 'true')
                self.send_header('Access-Control-Allow-Methods', 'GET')
                self.end_headers()
            def do_GET(self):
                observed.append(('GET', self.headers.get('Accept', '')))
                self.send_response(200)
                self.send_header('Access-Control-Allow-Origin', self.headers.get('Origin', ''))
                self.send_header('Access-Control-Allow-Credentials', 'true')
                self.end_headers()
                self.wfile.write(b'{}')
        class Page(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'<html></html>')
        api, page_server = ThreadingHTTPServer(('127.0.0.1', 0), API), ThreadingHTTPServer(('127.0.0.1', 0), Page)
        for server in (api, page_server):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.goto(f'http://127.0.0.1:{page_server.server_port}/')
                    seen = []
                    def route_handler(route):
                        headers = route.request.headers.copy()
                        seen.append(headers.get('accept'))
                        headers['accept'] = 'application/json'
                        route.continue_(headers=headers)
                    page.route('**/aweme/v1/web/comment/list/**', route_handler)
                    marker = 'a' * 32
                    result = page.evaluate(XHR_JS, dict(url=f'http://127.0.0.1:{api.server_port}{site.TOP_PATH}',
                        headers={'accept': 'application/json'}, video='123', root=None, cursor='0',
                        path=site.TOP_PATH, params=site.PARAM, signatures=site.SIGNATURES,
                        storage=site.TOKEN_STORAGE, signals={'captcha': [], 'logged_out': []},
                        timeout=5000, markerHeader='x-dyc-replay', marker=marker, markerInAccept=True))
                    self.assertEqual(result.get('status'), 200, result)
                    self.assertEqual(seen, ['application/json;dyc-replay=' + marker])
                    self.assertEqual(observed, [('GET', 'application/json')])
                finally:
                    browser.close()
        finally:
            for server in (api, page_server):
                server.shutdown()
                server.server_close()
