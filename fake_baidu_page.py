"""Synthetic Baidu page for loopback-only browser tests."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path)
        if path.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Set-Cookie', 'fakecookie=fake-cookie-secret')
            self.end_headers()
            self.wfile.write(b'<script>window.Paris={getAcsInstance:cb=>cb(null,{getSign:cb=>cb(null,"fake-signature-secret")})}</script>')
            return
        mode = getattr(self.server, 'mode', '')
        if path.path == '/api/SearchApi/index':
            self.server.index_calls = getattr(self.server, 'index_calls', 0) + 1
        if path.path == '/api/SearchApi/index' and mode and mode not in ('non_day', 'bad_type', 'missing', 'extra', 'no_uniqid', 'no_indexes', 'no_data', 'long_data', 'over_limit'):
            if mode == 'timeout':
                import time
                time.sleep(27)
                return
            if mode == 'redirect':
                self.send_response(302)
                self.send_header('Location', '/unexpected')
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html' if mode == 'html' else 'application/json')
            self.end_headers()
            body = b'<html>verification</html>' if mode == 'html' else b'x' * 1048577 if mode == 'large' else b'{"status":0,"status":0}' if mode == 'duplicate' else ('{"status":' + mode + '}').encode()
            self.wfile.write(body)
            return
        params = parse_qs(path.query)
        if path.path == '/api/SearchApi/index':
            assert self.headers.get('Cipher-Text') == 'fake-signature-secret'
            words = json.loads(params['word'][0])
            from datetime import date
            days = (date.fromisoformat(params['endDate'][0]) - date.fromisoformat(params['startDate'][0])).days + 1
            series = {'startDate': params['startDate'][0], 'endDate': params['endDate'][0], 'type': 'week' if mode == 'non_day' else 0 if mode == 'bad_type' else 'day', 'data':'ab' * (days - 1) + 'c'}
            if mode in ('long_data', 'over_limit'):
                series['data'] = ('b'.join(['a' * 15] * days) if mode == 'long_data' else 'a' * (max(6200, days * 64) + 1))
            if mode == 'no_data':
                del series['data']
            if mode == 'extra':
                for word in words:
                    word[0]['extra'] = 'synthetic-value-secret'
                series['extra'] = 'synthetic-value-secret'
            data = {'status': 0, 'data': {'uniqid':'synthetic', 'userIndexes': [] if mode == 'missing' else [{'word':w, 'all':series, 'pc':series, 'wise':series} for w in words]}}
            if mode == 'extra':
                data['data']['generalRatio'] = 'synthetic-value-secret'
            if mode == 'no_uniqid':
                del data['data']['uniqid']
            if mode == 'no_indexes':
                del data['data']['userIndexes']
        elif path.path == '/Interface/api/ptbk':
            data = {'status': 0, 'data': 'abc1,2'}
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())


def serve():
    return ThreadingHTTPServer(('127.0.0.1', 0), Handler)
