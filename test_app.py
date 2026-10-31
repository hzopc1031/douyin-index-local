"""Batch controller and comparison tests; no GUI or network interactions."""
import copy
import json
import tempfile
import threading
import time
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import app
from client import DouyinIndexClient, SanitizedClientError
from test_client import FixtureSession, batch_fixture, response


def complete_result():
    payload = batch_fixture()
    payload["data"]["hot_list"][0]["search_hot_list"][0]["index"] = 41615
    result = DouyinIndexClient(FixtureSession(response(payload))).query_many(
        ["银杏", "酵素"], "2025-01-01", "2025-01-07")
    result["metadata"].update({"source": "creator.douyin.com", "region": "全国",
                               "platform": "aweme", "page_comparison": "pending"})
    return result


def entries_for(result):
    return [{"keyword": row["keyword"], "date": row["date"],
             "page_display": f'{row["search_index"]:,}'} for row in result["rows"]]


class TrackingSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.was_closed = False
        self.cookies.set("sessionid", "synthetic-only", domain="creator.douyin.com")

    def close(self):
        self.was_closed = True
        super().close()


class QrManagerConcurrencyTests(unittest.TestCase):
    def test_single_lazy_manager(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(app, 'OUTPUT', Path(temp)):
            api = app.Api()
            barrier = threading.Barrier(8)
            results = []
            def construct():
                time.sleep(.03)
                return object()
            def get_manager():
                barrier.wait(timeout=3)
                results.append(api._qr_manager())
            with patch('qr_login.login_mode', return_value='qr'), patch('qr_login.QrLoginManager', side_effect=construct) as constructor:
                threads = [threading.Thread(target=get_manager) for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
                constructor.assert_called_once_with()
            self.assertEqual(len(results), 8)
            self.assertTrue(all(result is results[0] for result in results))


class ComparisonTests(unittest.TestCase):
    def test_keyed_values_align_independently_of_entry_order(self):
        result = complete_result()
        report = app.compare_result(result, list(reversed(entries_for(result))))
        self.assertTrue(report["verified"])
        self.assertEqual((report["matched_rows"], report["expected_rows"]), (14, 14))
        self.assertEqual([(r["keyword"], r["date"]) for r in report["rows"]],
                         [(r["keyword"], r["date"]) for r in result["rows"]])

    def test_duplicates_and_extra_keys_cannot_be_verified(self):
        result = complete_result()
        entries = entries_for(result)
        for invalid in (entries + [entries[0]], entries + [{"keyword": "未请求词", "date": "2025-01-01", "page_display": "1"}]):
            with self.subTest(invalid=invalid[-1]):
                with self.assertRaises(ValueError):
                    app.compare_result(result, invalid)

    def test_grouping_and_rounded_values_never_produce_false_match(self):
        result = complete_result()
        for text, expected in (("4,16,15", "unreadable"), ("4.16万", "insufficient_precision"),
                               ("4.1615e4", "unreadable"), ("", "pending")):
            with self.subTest(text=text):
                entries = entries_for(result)
                entries[0]["page_display"] = text
                report = app.compare_result(result, entries)
                self.assertFalse(report["verified"])
                self.assertEqual(report["matched_rows"], 13)
                self.assertEqual(report["rows"][0]["status"], expected)

    def test_missing_page_observations_are_pending_and_incomplete_data_cannot_pass(self):
        result = complete_result()
        report = app.compare_result(result, entries_for(result)[1:])
        self.assertFalse(report["verified"])
        self.assertEqual(report["rows"][0]["status"], "pending")
        result["metadata"]["complete"] = False
        self.assertFalse(app.compare_result(result, entries_for(result))["verified"])


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output_patch = patch.object(app, "OUTPUT", Path(self.temp.name))
        self.output_patch.start()
        self.api = app.Api()

    def tearDown(self):
        self.output_patch.stop()
        self.temp.cleanup()

    def test_cloud_login_precheck_returns_authentication_required(self):
        self.assertTrue(issubclass(app.LoginRequired, ValueError))
        reply = self.api.fetch_batch("银杏", "2025-01-07", cloud=True)
        self.assertEqual(reply, {"ok": False, "code": "authentication_required",
                                 "message": "请先打开官方登录窗口。"})
        self.assertFalse(self.api._attempted)
        self.assertFalse(self.api._lock.locked())

    def test_cache_hit_has_no_login_or_network_side_effect(self):
        result = complete_result()
        folder = self.api._store.save(result)
        with patch.object(self.api, "_client", side_effect=AssertionError("Must not open login or network")) as client:
            reply = self.api.fetch_batch("银杏\n酵素", "2025-01-07")
        client.assert_not_called()
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["cache_hit"])
        self.assertEqual(reply["folder"], folder)
        self.assertFalse(self.api._attempted)

    def test_cache_report_is_recomputed_instead_of_trusting_verified_flag(self):
        result = complete_result()
        result["metadata"]["page_comparison"] = "verified"
        folder = Path(self.api._store.save(result))
        saved = app.compare_result(result, entries_for(result))
        saved["rows"][0]["page_display"] = "99"
        self.assertTrue(saved["verified"])
        (folder / "comparison.json").write_text(json.dumps(saved))
        with patch.object(self.api, "_client", side_effect=AssertionError("No network")):
            reply = self.api.fetch_batch("银杏\n酵素", "2025-01-07")
        self.assertFalse(reply["comparison"]["verified"])
        self.assertEqual(reply["comparison"]["matched_rows"], 13)
        self.assertEqual(reply["metadata"]["page_comparison"], "incomplete")

    def test_cache_without_report_does_not_trust_metadata_verified(self):
        result = complete_result()
        result["metadata"]["page_comparison"] = "verified"
        self.api._store.save(result)
        reply = self.api.fetch_batch("银杏\n酵素", "2025-01-07")
        self.assertTrue(reply["cache_hit"])
        self.assertIsNone(reply["comparison"])
        self.assertEqual(reply["metadata"]["page_comparison"], "pending")

    def test_failed_request_is_not_retried_and_session_credentials_are_cleared(self):
        session = TrackingSession()
        client = Mock(session=session)
        client.query_many.side_effect = SanitizedClientError("network_error", "网络失败")
        with patch.object(self.api, "_client", return_value=client) as factory:
            first = self.api.fetch_batch("银杏\n酵素", "2025-01-07")
            second = self.api.fetch_batch("消化酶", "2025-01-07")
        self.assertEqual(first["code"], "network_error")
        self.assertEqual(second["code"], "session_request_used")
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(client.query_many.call_count, 1)
        self.assertTrue(session.was_closed)
        self.assertEqual(len(session.cookies), 0)

    def test_cloud_explicit_calls_have_no_browser_cooldown(self):
        client = Mock(session=TrackingSession())
        client.query_many.side_effect = lambda *args: complete_result()
        with patch.object(self.api, "_client", return_value=client), \
                patch.object(self.api._store, "lookup", return_value=None):
            for _ in range(2):
                reply = self.api.fetch_batch("银杏\n酵素", "2025-01-07", cloud=True)
                self.assertTrue(reply["ok"])
                self.assertFalse(reply["cache_hit"])
        self.assertEqual(client.query_many.call_count, 2)
        self.assertEqual(self.api.fetch_batch("消化酶", "2025-01-07")["code"], "session_request_used")

    def test_input_errors_do_not_consume_request_and_successful_batch_exports(self):
        session = TrackingSession()
        client = Mock(session=session)
        client.query_many.return_value = complete_result()
        with patch.object(self.api, "_client", return_value=client) as factory:
            invalid = self.api.fetch_batch("银杏\n银杏", "2025-01-07")
            self.assertFalse(invalid["ok"])
            self.assertFalse(self.api._attempted)
            reply = self.api.fetch_batch("银杏\n酵素", "2025-01-07")
        self.assertEqual(factory.call_count, 1)
        self.assertTrue(reply["ok"])
        self.assertFalse(reply["cache_hit"])
        self.assertTrue(session.was_closed)
        self.assertEqual(len(session.cookies), 0)
        folder = Path(reply["folder"])
        self.assertTrue((folder / "search-index.csv").is_file())
        comparison = self.api.compare(entries_for(complete_result()))
        self.assertTrue(comparison["verified"])
        self.assertEqual(comparison["expected_rows"], 14)
        saved = json.loads((folder / "result.json").read_text())
        self.assertEqual(saved["metadata"]["page_comparison"], "verified")

    def test_cookie_session_is_closed_when_client_construction_fails(self):
        session = TrackingSession()
        cookie = SimpleCookie()
        cookie["sessionid"] = "synthetic-only"
        cookie["sessionid"]["domain"] = "creator.douyin.com"
        page = Mock()
        page.get_current_url.return_value = "https://creator.douyin.com/creator-micro/home"
        page.get_cookies.return_value = [cookie]
        page.evaluate_js.side_effect = [False, RuntimeError("private-upstream-details")]
        self.api._page = page
        with patch.object(app.requests, "Session", return_value=session):
            with self.assertRaises(RuntimeError):
                self.api._client()
        self.assertTrue(session.was_closed)
        self.assertEqual(len(session.cookies), 0)
        self.assertFalse(self.api._attempted)

    def test_http_page_cannot_supply_login_cookies(self):
        page = Mock()
        page.get_current_url.return_value = "http://creator.douyin.com/creator-micro/home"
        self.api._page = page
        with self.assertRaises(ValueError):
            self.api._client()
        page.get_cookies.assert_not_called()

    def test_query_and_compare_use_the_same_exclusion_lock(self):
        self.api._lock.acquire()
        try:
            self.assertEqual(self.api.fetch_batch("银杏", "2025-01-07")["code"], "busy")
            self.assertFalse(self.api.compare([])["ok"])
        finally:
            self.api._lock.release()


if __name__ == "__main__":
    unittest.main()


class WebviewProfileTests(unittest.TestCase):
    def test_hub_profile_and_busy_exit(self):
        import tempfile
        import os
        import app
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            'INDEX_HUB_ENABLED': '1', 'INDEX_HUB_PERSIST_LOGIN': '1', 'INDEX_HUB_PROFILE_DIR': root}):
            try:
                kwargs = app._webview_start_kwargs()
                self.assertFalse(kwargs['private_mode'])
                self.assertEqual(kwargs['storage_path'], str(Path(root) / 'webview'))
                with self.assertRaises(SystemExit) as caught:
                    app._webview_start_kwargs()
                self.assertEqual(caught.exception.code, 2)
            finally:
                app._profile_lock.release()
                app._profile_lock = None

    def test_desktop_and_rollback_remain_private(self):
        import os
        import app
        from unittest.mock import patch
        for enabled, persist in [('0', '1'), ('1', '0')]:
            with patch.dict(os.environ, {'INDEX_HUB_ENABLED': enabled, 'INDEX_HUB_PERSIST_LOGIN': persist}), patch('browser_profile.prepare_profile') as prepare:
                self.assertEqual(app._webview_start_kwargs(), dict(private_mode=True, debug=False, http_server=False))
                prepare.assert_not_called()
