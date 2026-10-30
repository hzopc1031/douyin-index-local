"""Offline fixtures only: no production calls or real login state."""

import base64
import copy
import datetime as dt
import io
import json
import unittest

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from client import (DouyinIndexClient, SanitizedClientError, SHANGHAI, TREND_URL,
                    VALID_DATE_URL)


def fixture():
    return {"status": 0, "data": {"hot_list": [{
        "keyword": "银杏", "search_hot_list": [
            {"datetime": f"202501{day:02d}", "index": 0 if day == 1 else str(day * 100)}
            for day in range(1, 8)],
        "hot_list": [{"datetime": f"202501{day:02d}", "index": day * 10}
                     for day in range(1, 8)]}]}}


def response(payload, status=200, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    result._content_consumed = True
    result.raw = io.BytesIO(result._content)
    result.headers.update(headers or {})
    return result


def batch_fixture(keywords=("银杏", "酵素")):
    groups = []
    for offset, keyword in enumerate(keywords):
        group = copy.deepcopy(fixture()["data"]["hot_list"][0])
        group["keyword"] = keyword
        for point in group["search_hot_list"]:
            point["index"] = int(point["index"]) + offset * 1000
        groups.append(group)
    return {"status": 0, "data": {"hot_list": groups}}


class FixtureSession(requests.Session):
    def __init__(self, value):
        super().__init__()
        self.value = value
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class ClientTests(unittest.TestCase):
    def run_query(self, payload=None, *, status=200, headers=None):
        session = FixtureSession(response(fixture() if payload is None else payload, status, headers))
        session.cookies.set("sessionid", "synthetic-only", domain="creator.douyin.com")
        client = DouyinIndexClient(session, user_agent="FixtureBrowser/1")
        result = client.query("银杏", "2025-01-01", "2025-01-07")
        return result, session

    def test_exact_contract_and_complete_seven_days(self):
        result, session = self.run_query()
        self.assertEqual(len(session.calls), 1)
        url, arguments = session.calls[0]
        self.assertEqual(url, TREND_URL)
        self.assertEqual(arguments["json"], {"keyword_list": ["银杏"], "start_date": "20250101",
                                             "end_date": "20250107", "app_name": "aweme"})
        self.assertFalse(arguments["allow_redirects"])
        self.assertTrue(arguments["verify"])
        self.assertFalse(session.trust_env)
        self.assertEqual(session.adapters["https://"].max_retries.total, 0)
        self.assertTrue(result["metadata"]["complete"])
        self.assertEqual(result["rows"][0]["search_index"], 0)
        self.assertEqual(result["rows"][1]["search_index"], 200)
        self.assertEqual(result["metadata"]["timezone"], "Asia/Shanghai")
        self.assertNotIn("synthetic-only", json.dumps(result))
        self.assertNotIn("Cookie", arguments["headers"])

    def test_missing_search_is_not_zero(self):
        value = fixture()
        value["data"]["hot_list"][0]["search_hot_list"].pop(1)
        result, _ = self.run_query(value)
        self.assertFalse(result["metadata"]["complete"])
        self.assertEqual(result["metadata"]["missing_search_dates"], ["2025-01-02"])
        self.assertIsNone(result["rows"][1]["search_index"])

    def test_missing_entire_day_is_reported_without_fabricated_row(self):
        value = fixture()
        group = value["data"]["hot_list"][0]
        group["search_hot_list"].pop(1)
        group["hot_list"].pop(1)
        result, _ = self.run_query(value)
        self.assertEqual(len(result["rows"]), 6)
        self.assertEqual(result["metadata"]["missing_dates"], ["2025-01-02"])

    def test_empty_data_is_incomplete_not_zero(self):
        result, _ = self.run_query({"status": 0, "data": {"hot_list": []}})
        self.assertEqual(result["rows"], [])
        self.assertFalse(result["metadata"]["complete"])
        self.assertEqual(len(result["metadata"]["missing_search_dates"]), 7)

    def test_duplicate_date_and_wrong_keyword_rejected(self):
        value = fixture()
        value["data"]["hot_list"][0]["search_hot_list"].append({"datetime": "20250101", "index": 99})
        with self.assertRaises(SanitizedClientError) as caught:
            self.run_query(value)
        self.assertEqual(caught.exception.code, "duplicate_date")
        for keyword in ("银杏叶", "", None):
            with self.subTest(keyword=keyword):
                value = fixture()
                value["data"]["hot_list"][0]["keyword"] = keyword
                with self.assertRaises(SanitizedClientError) as caught:
                    self.run_query(value)
                self.assertEqual(caught.exception.code, "unexpected_keyword")

    def test_out_of_range_date_rejected(self):
        value = fixture()
        value["data"]["hot_list"][0]["search_hot_list"][0]["datetime"] = "20241231"
        with self.assertRaises(SanitizedClientError) as caught:
            self.run_query(value)
        self.assertEqual(caught.exception.code, "unexpected_date")

    def test_nonfinite_bool_and_malformed_values_rejected(self):
        for index in (True, "NaN", "Infinity", "not-a-number", -1, {}):
            with self.subTest(index=index):
                value = fixture()
                value["data"]["hot_list"][0]["search_hot_list"][0]["index"] = index
                with self.assertRaises(SanitizedClientError):
                    self.run_query(value)

    def test_timestamp_uses_shanghai_date(self):
        value = fixture()
        stamp = int(dt.datetime(2024, 12, 31, 16, 0, tzinfo=dt.timezone.utc).timestamp())
        value["data"]["hot_list"][0]["search_hot_list"][0]["datetime"] = stamp
        result, _ = self.run_query(value)
        self.assertEqual(result["rows"][0]["date"], "2025-01-01")

    def test_encrypted_modes_and_direct_business_object(self):
        key_pairs = {
            "1": ("91b4a33aa9fd3bb00f2ea519c5d0a44d", "26e84bd5c395e491fda23cedda0d843e"),
            "2": ("4a35db61325bef35e8513a1289c50bdc", "39e90c2e3821460f2f957fcf7a62dcf9"),
        }
        for mode, (key, iv) in key_pairs.items():
            with self.subTest(mode=mode):
                plaintext = json.dumps(fixture()["data"]).encode()
                padder = padding.PKCS7(128).padder()
                padded = padder.update(plaintext) + padder.finalize()
                encryptor = Cipher(algorithms.AES(bytes.fromhex(key)), modes.CBC(bytes.fromhex(iv))).encryptor()
                data = base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()
                result, _ = self.run_query({"status": 0, "data": data}, headers={"X-Encrypted": mode})
                self.assertTrue(result["metadata"]["complete"])
                self.assertEqual(result["raw"]["decoded"], fixture()["data"])
                self.assertEqual(result["raw"]["response"]["data"], data)

    def test_unknown_encryption_and_invalid_ciphertext_rejected(self):
        for mode, data, expected in (("3", "x", "unsupported_encryption"),
                                     ("1", "!", "response_decode"),
                                     ("2", "AAAA", "response_decode")):
            with self.subTest(mode=mode, data=data):
                with self.assertRaises(SanitizedClientError) as caught:
                    self.run_query({"data": data}, headers={"x-encrypted": mode})
                self.assertEqual(caught.exception.code, expected)

    def test_http_limit_forbidden_and_redirect_are_not_retried(self):
        for status, category in ((429, "rate_limited"), (403, "access_denied"), (302, "http_error")):
            with self.subTest(status=status):
                session = FixtureSession(response(b"sessionid=do-not-leak", status))
                client = DouyinIndexClient(session)
                with self.assertRaises(SanitizedClientError) as caught:
                    client.query("银杏", "2025-01-01", "2025-01-07")
                self.assertEqual(caught.exception.code, category)
                self.assertEqual(caught.exception.http_status, status)
                self.assertEqual(len(session.calls), 1)
                self.assertNotIn("do-not-leak", str(caught.exception))

    def test_business_errors_and_challenge_are_sanitized(self):
        for payload, category in (({"status": 1, "msg": "captcha secret-cookie"}, "verification_required"),
                                  ({"status": 429, "msg": "secret-cookie"}, "rate_limited"),
                                  ({"status": 5, "msg": "success"}, "business_error"),
                                  ({"status": 0, "BaseResp": {"StatusCode": 5}}, "business_error")):
            with self.subTest(category=category):
                with self.assertRaises(SanitizedClientError) as caught:
                    self.run_query(payload)
                self.assertEqual(caught.exception.code, category)
                self.assertNotIn("secret-cookie", json.dumps(caught.exception.as_dict()))

    def test_network_errors_do_not_expose_exception_details(self):
        session = FixtureSession(requests.ConnectionError("secret-cookie in upstream error"))
        with self.assertRaises(SanitizedClientError) as caught:
            DouyinIndexClient(session).query("银杏", "2025-01-01", "2025-01-07")
        self.assertEqual(caught.exception.code, "network_error")
        self.assertNotIn("secret-cookie", str(caught.exception))
        self.assertEqual(len(session.calls), 1)

    def test_json_schema_drift_and_duplicate_keys_rejected(self):
        for value in (b'<html>secret-cookie</html>', b'{"status": 5, "status": 0}',
                      {"status": 0}, {"status": 0, "data": []}, {"data": {"hot_list": "oops"}}):
            with self.subTest(value=value):
                with self.assertRaises(SanitizedClientError) as caught:
                    self.run_query(value)
                self.assertNotIn("secret-cookie", str(caught.exception))

    def test_input_validation_happens_before_request(self):
        session = FixtureSession(response(fixture()))
        client = DouyinIndexClient(session)
        for keyword, start, end in (("", "2025-01-01", "2025-01-07"),
                                    ("银杏", "2025-01-01", "2027-04-01"),
                                    ("银杏", "20250101", "20250107")):
            with self.subTest(keyword=keyword, start=start, end=end):
                with self.assertRaises(ValueError):
                    client.query(keyword, start, end)
        self.assertEqual(session.calls, [])

    def test_valid_date_contract_and_conflicting_values(self):
        session = FixtureSession(response({"status": 0, "data": {"end_date": "20250107"}}))
        result = DouyinIndexClient(session).latest_valid_date()
        self.assertEqual(result["date"], "2025-01-07")
        self.assertEqual(session.calls[0][0], VALID_DATE_URL)
        self.assertEqual(session.calls[0][1]["json"], {"app_name": "aweme"})
        session = FixtureSession(response({"data": {"end_date": "20250107", "date": "20250106"}}))
        with self.assertRaises(SanitizedClientError):
            DouyinIndexClient(session).latest_valid_date()


class BatchClientTests(unittest.TestCase):
    def run_query(self, payload, keywords=None):
        session = FixtureSession(response(payload))
        result = DouyinIndexClient(session).query_many(
            ["银杏", "酵素"] if keywords is None else keywords, "2025-01-01", "2025-01-07")
        return result, session

    def test_batch_is_one_request_and_matches_keyword_not_response_order(self):
        payload = batch_fixture()
        payload["data"]["hot_list"].reverse()
        for group in payload["data"]["hot_list"]:
            group["search_hot_list"].reverse()
        result, session = self.run_query(payload)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][1]["json"]["keyword_list"], ["银杏", "酵素"])
        self.assertEqual([(r["keyword"], r["date"]) for r in result["rows"]],
                         [(word, f"2025-01-{day:02d}") for word in ("银杏", "酵素")
                          for day in range(1, 8)])
        self.assertEqual(result["rows"][0]["search_index"], 0)
        self.assertEqual(result["rows"][7]["search_index"], 1000)
        meta = result["metadata"]
        self.assertEqual(meta["keywords"], ["银杏", "酵素"])
        self.assertEqual((meta["keyword_count"], meta["requested_days"], meta["expected_rows"],
                          meta["returned_rows"]), (2, 7, 14, 14))
        self.assertTrue(meta["complete"])
        self.assertTrue(all(q["complete"] for q in meta["quality_by_keyword"].values()))

    def test_missing_keyword_cannot_borrow_another_keywords_values(self):
        result, _ = self.run_query(batch_fixture(["酵素"]))
        self.assertFalse(result["metadata"]["complete"])
        self.assertEqual(result["metadata"]["missing_keywords"], ["银杏"])
        self.assertEqual({r["keyword"] for r in result["rows"]}, {"酵素"})
        absent = result["metadata"]["quality_by_keyword"]["银杏"]
        self.assertEqual(absent["returned_days"], 0)
        self.assertEqual(len(absent["missing_dates"]), 7)
        self.assertEqual(len(absent["missing_search_dates"]), 7)
        self.assertTrue(result["metadata"]["quality_by_keyword"]["酵素"]["complete"])

    def test_partial_search_is_flagged_per_keyword_without_filling_zero(self):
        payload = batch_fixture()
        payload["data"]["hot_list"][1]["search_hot_list"].pop(3)
        result, _ = self.run_query(payload)
        self.assertFalse(result["metadata"]["complete"])
        missing = result["metadata"]["quality_by_keyword"]["酵素"]
        self.assertEqual(missing["missing_search_dates"], ["2025-01-04"])
        row = next(r for r in result["rows"] if r["keyword"] == "酵素" and r["date"] == "2025-01-04")
        self.assertIsNone(row["search_index"])
        self.assertTrue(result["metadata"]["quality_by_keyword"]["银杏"]["complete"])

    def test_unknown_and_duplicate_returned_keywords_stop_batch(self):
        for words, expected in ((["银杏", "银杏"], "duplicate_keyword"),
                                (["银杏", "酵素", "陌生词"], "unexpected_keyword"),
                                (["银杏", None], "unexpected_keyword")):
            with self.subTest(words=words):
                with self.assertRaises(SanitizedClientError) as caught:
                    self.run_query(batch_fixture(words))
                self.assertEqual(caught.exception.code, expected)

    def test_duplicate_day_in_one_keyword_stops_entire_batch(self):
        payload = batch_fixture()
        series = payload["data"]["hot_list"][1]["search_hot_list"]
        series.append(copy.deepcopy(series[0]))
        with self.assertRaises(SanitizedClientError) as caught:
            self.run_query(payload)
        self.assertEqual(caught.exception.code, "duplicate_date")

    def test_five_words_allowed_but_invalid_input_never_sends_request(self):
        words = ["酵素", "消化酶", "膳食补充剂", "银杏", "银杏提取物"]
        result, session = self.run_query(batch_fixture(words), words)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(len(result["rows"]), 35)
        self.assertEqual(result["metadata"]["expected_rows"], 35)
        session = FixtureSession(response(batch_fixture(words)))
        client = DouyinIndexClient(session)
        for invalid in ([], words + ["第六词"], ["酵素", "酵素"], [""], [" 酵素"],
                        ["词" * 101], [True], "酵素", None):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    client.query_many(invalid, "2025-01-01", "2025-01-07")
        self.assertEqual(session.calls, [])

    def test_empty_batch_response_is_incomplete_for_every_requested_word(self):
        result, _ = self.run_query({"status": 0, "data": {"hot_list": []}})
        self.assertFalse(result["metadata"]["complete"])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["metadata"]["missing_keywords"], ["银杏", "酵素"])
        self.assertEqual(result["metadata"]["returned_rows"], 0)

    def test_single_query_keeps_legacy_metadata(self):
        session = FixtureSession(response(fixture()))
        result = DouyinIndexClient(session).query("银杏", "2025-01-01", "2025-01-07")
        self.assertEqual(result["metadata"]["keyword"], "银杏")
        self.assertEqual(result["metadata"]["returned_days"], 7)
        self.assertEqual(result["metadata"]["missing_dates"], [])
        self.assertEqual(result["metadata"]["missing_search_dates"], [])
        self.assertEqual(result["metadata"]["keyword_count"], 1)


if __name__ == "__main__":
    unittest.main()
