"""Synthetic, in-memory cookie scope regression tests; no browser or network."""
from http.cookies import SimpleCookie
import unittest
from unittest.mock import Mock

from browser_login import BrowserLoginState, _cookie_snapshot


class CookieScopeTests(unittest.TestCase):
    def test_rejected_session_tracks_normalized_cookie_scope(self):
        cases = [
            ('.douyin.com', '/creator-micro', '.douyin.com', '/', 'cookie_present'),
            ('.douyin.com', '/creator-micro', '.douyin.com', '', 'cookie_present'),
            ('.douyin.com', '/', 'creator.douyin.com', '/', 'cookie_present'),
            ('.douyin.com', '/Index', '.douyin.com', '/index', 'cookie_present'),
            ('.DOUYIN.COM', '/', 'douyin.com', '/', 'rejected'),
            ('.douyin.com', '', '.douyin.com', '/', 'rejected'),
        ]
        for domain, path, new_domain, new_path, expected in cases:
            with self.subTest(domain=domain, path=path, new_domain=new_domain, new_path=new_path):
                cookies = SimpleCookie()
                cookies['sessionid'] = 'synthetic-session'
                item = cookies['sessionid']
                item['domain'], item['path'] = domain, path
                page = Mock()
                page.get_cookies.return_value = [cookies]
                login = BrowserLoginState()
                login.reject(_cookie_snapshot([cookies])[1])
                self.assertEqual(login.read(page), 'rejected')

                item['domain'], item['path'] = new_domain, new_path
                self.assertEqual(login.read(page), expected)
