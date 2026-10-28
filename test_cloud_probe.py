"""Synthetic credentials only; all requests are stubbed and stay offline."""

import datetime as dt
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import cloud_probe as probe
from client import TREND_URL
from test_client import FixtureSession, fixture, response


SECRET = "synthetic-cookie-do-not-echo"
NOW = dt.datetime(2026, 10, 28, 8, tzinfo=dt.timezone.utc)


def payload():
    return {"version": 1, "user_agent": "FixtureBrowser/1.0", "cookies": [{
        "name": "sessionid", "value": SECRET, "domain": "creator.douyin.com",
        "path": "/", "secure": True, "expires": None, "host_only": True}],
        "keyword": "银杏", "end_date": "2025-01-07"}


class ProbeSession(FixtureSession):
    def __init__(self, value):
        super().__init__(value)
        self.closed = False
        self.request_cookie_count = None

    def post(self, url, **kwargs):
        self.request_cookie_count = len(self.cookies)
        return super().post(url, **kwargs)

    def close(self):
        self.closed = True
        super().close()


class ValidationTests(unittest.TestCase):
    def test_exact_protocol_and_in_memory_copy(self):
        source = payload()
        result = probe.validate_payload(source, now=NOW)
        self.assertEqual((result.start_date, result.end_date), ("2025-01-01", "2025-01-07"))
        self.assertNotIn(SECRET, repr(result))
        result.clear()
        self.assertEqual(result.cookies, [])
        self.assertEqual(result.user_agent, "")
        self.assertEqual(source["cookies"][0]["value"], SECRET)

    def test_host_scope_attributes_and_expiry_fail_closed(self):
        changes = [
            ("domain", ".douyin.com"), ("domain", "douyin.com"),
            ("domain", ".creator.douyin.com"), ("domain", "CREATOR.douyin.com"),
            ("domain", "creator.douyin.com.attacker.invalid"),
            ("host_only", False), ("host_only", 1), ("secure", False), ("secure", 1),
            ("expires", -1), ("expires", 0), ("expires", int(NOW.timestamp())),
            ("expires", True), ("expires", NOW.timestamp() + 3600.5),
            ("expires", "1900000000"), ("expires", 253402300800),
            ("path", ""), ("path", "/api/v2/index/get_multi_keyword_hot"),
            ("path", "/other"), ("path", "/;Cookie:bad"), ("path", "/\r\n"),
        ]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                source = payload()
                source["cookies"][0][key] = value
                with self.assertRaises(probe.ProbeValidationError):
                    probe.validate_payload(source, now=NOW)
        for key in list(payload()["cookies"][0]):
            source = payload()
            del source["cookies"][0][key]
            with self.assertRaises(probe.ProbeValidationError):
                probe.validate_payload(source, now=NOW)

    def test_valid_cookie_paths_and_future_expiry(self):
        for path in ("/", "/api", "/api/v2/", probe.TREND_PATH):
            source = payload()
            source["cookies"][0].update(path=path, expires=int(NOW.timestamp()) + 3600)
            self.assertEqual(probe.validate_payload(source, now=NOW).cookies[0]["path"], path)

    def test_cookie_syntax_size_duplicate_and_prefix_checks(self):
        for name, value in (("bad name", SECRET), ("bad;name", SECRET),
                            ("x\n", SECRET), ("x", "injected\r\nHeader:x"),
                            ("x", "value;other=secret"), ("x", "a b"),
                            ("x", '"quoted"'), ("x", "a\\b"),
                            ("x", "\u007f"), ("x", "中文"),
                            ("n" * 257, SECRET), ("x", "v" * 8193)):
            source = payload()
            source["cookies"][0].update(name=name, value=value)
            with self.subTest(name=name[:20], value=value[:20]):
                with self.assertRaises(probe.ProbeValidationError):
                    probe.validate_payload(source, now=NOW)
        source = payload()
        source["cookies"] *= 2
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_payload(source, now=NOW)
        source = payload()
        source["cookies"][0].update(name="__Host-session", path="/api")
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_payload(source, now=NOW)
        source = payload()
        source["cookies"] = [dict(source["cookies"][0], name=f"s{i}", value="v" * 8192) for i in range(5)]
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_payload(source, now=NOW)
        for value in ([], [payload()["cookies"][0]] * 101):
            source = payload()
            source["cookies"] = value
            with self.assertRaises(probe.ProbeValidationError):
                probe.validate_payload(source, now=NOW)

    def test_payload_ua_keyword_and_dates(self):
        changes = [("version", True), ("version", 2), ("version", "1"),
                   ("user_agent", ""), ("user_agent", " leading"),
                   ("user_agent", "x\r\nCookie: " + SECRET),
                   ("user_agent", "x" * 1025), ("user_agent", "x\x80"),
                   ("keyword", ""), ("keyword", "word\nword"),
                   ("keyword", "x\u200b"), ("keyword", "a" * 101),
                   ("keyword", ["one", "two"]), ("keyword", " trailing "),
                   ("end_date", "20261020"), ("end_date", "2026-02-30"),
                   ("end_date", "0001-01-01"), ("end_date", "2026-10-28"),
                   ("end_date", "2026-10-29")]
        for key, value in changes:
            source = payload()
            source[key] = value
            with self.subTest(key=key, value=value):
                with self.assertRaises(probe.ProbeValidationError):
                    probe.validate_payload(source, now=NOW)
        source = payload()
        source["endpoint"] = "https://attacker.invalid"
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_payload(source, now=NOW)

    def test_shanghai_date_boundary(self):
        source = payload()
        source["end_date"] = "2026-10-28"
        late_utc = dt.datetime(2026, 10, 28, 16, tzinfo=dt.timezone.utc)
        self.assertEqual(probe.validate_payload(source, now=late_utc).end_date, "2026-10-28")

    def test_bounded_json_rejects_duplicate_nan_trailing_and_bad_utf8(self):
        for raw in (b'{"version":1,"version":1}', b'{"version":NaN}',
                    b'{} {}', b'[]', b'\xff', b' ' * (probe.MAX_INPUT_BYTES + 1)):
            with self.subTest(length=len(raw)):
                with self.assertRaises(probe.ProbeValidationError):
                    probe.read_payload(io.BytesIO(raw))
        self.assertEqual(probe.read_payload(io.BytesIO(json.dumps(payload()).encode())), payload())

    def test_host_only_cookie_policy_on_actual_prepared_requests(self):
        session = probe.make_session()
        request = probe.validate_payload(payload(), now=NOW)
        try:
            probe._add_cookies(session, request)
            cookie = next(iter(session.cookies))
            self.assertFalse(cookie.domain_specified)
            self.assertFalse(cookie.domain_initial_dot)
            for url, expected in ((TREND_URL, True),
                                  ("https://sub.creator.douyin.com/", False),
                                  ("https://douyin.com/", False),
                                  ("http://creator.douyin.com/", False),
                                  ("https://attacker.invalid/", False)):
                prepared = session.prepare_request(requests.Request("POST", url))
                self.assertEqual("Cookie" in prepared.headers, expected)
        finally:
            session.cookies.clear()
            session.close()
            request.clear()


class ExecutionTests(unittest.TestCase):
    def invoke(self, raw=None, args=None):
        out = io.StringIO()
        incoming = io.BytesIO(json.dumps(payload()).encode() if raw is None else raw)
        with patch.object(probe, "disable_core_dumps"):
            code = probe.main([] if args is None else args, stdin=incoming, stdout=out)
        return code, json.loads(out.getvalue()), out.getvalue()

    def test_validate_only_never_loads_client_or_opens_session(self):
        with patch.object(probe, "make_session", side_effect=AssertionError("network")), \
             patch.object(probe, "_load_client", side_effect=AssertionError("client import")):
            code, result, text = self.invoke(args=["--validate-only"])
        self.assertEqual(code, 0)
        self.assertEqual(result["mode"], "validate-only")
        self.assertFalse(result["network_request"])
        self.assertNotIn(SECRET, text)

    def test_self_check_never_reads_stdin_or_opens_session(self):
        stream = unittest.mock.Mock()
        stream.read.side_effect = AssertionError("stdin was read")
        out = io.StringIO()
        with patch.object(probe, "disable_core_dumps"), \
             patch.object(probe, "make_session", side_effect=AssertionError("network")):
            self.assertEqual(probe.main(["--self-check"], stdin=stream, stdout=out), 0)
        self.assertEqual(json.loads(out.getvalue())["mode"], "self-check")

    def test_one_request_fixed_endpoint_and_cleanup_with_whitelisted_output(self):
        body = fixture()
        body["unexpected_secret"] = SECRET
        body["data"]["hot_list"][0]["Cookie"] = SECRET
        session = ProbeSession(response(body))
        with patch.object(probe, "make_session", return_value=session):
            code, result, text = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(len(session.calls), 1)
        url, kwargs = session.calls[0]
        self.assertEqual(url, TREND_URL)
        self.assertEqual(kwargs["json"], {"keyword_list": ["银杏"], "start_date": "20250101",
                                          "end_date": "20250107", "app_name": "aweme"})
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["verify"])
        self.assertEqual(session.adapters["https://"].max_retries.total, 0)
        self.assertEqual(session.request_cookie_count, 1)
        self.assertTrue(session.closed)
        self.assertEqual(len(session.cookies), 0)
        self.assertEqual(set(result), {"ok", "rows", "metadata", "quality"})
        self.assertEqual(result["metadata"]["page_comparison"], "pending")
        self.assertTrue(result["quality"]["complete"])
        self.assertEqual(len(result["rows"]), 7)
        self.assertNotIn(SECRET, text)
        self.assertNotIn('"raw"', text)

    def test_missing_search_not_zero_and_incomplete_output(self):
        body = fixture()
        body["data"]["hot_list"][0]["search_hot_list"].pop(1)
        with patch.object(probe, "make_session", return_value=ProbeSession(response(body))):
            code, result, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertIsNone(result["rows"][1]["search_index"])
        self.assertFalse(result["quality"]["complete"])
        self.assertEqual(result["quality"]["missing_search_dates"], ["2025-01-02"])

    def test_http_and_business_failures_do_not_retry_or_expose_details(self):
        samples = [(response(SECRET.encode(), status), category) for status, category in
                   ((401, "authentication_required"), (403, "access_denied"),
                    (429, "rate_limited"), (302, "request_failed"))]
        samples += [(response({"status": 1, "msg": "captcha " + SECRET}), "verification_required"),
                    (requests.ConnectionError(SECRET), "request_failed")]
        for incoming, expected in samples:
            session = ProbeSession(incoming)
            with self.subTest(expected=expected):
                with patch.object(probe, "make_session", return_value=session):
                    code, result, text = self.invoke()
                self.assertNotEqual(code, 0)
                self.assertEqual(result["code"], expected)
                self.assertEqual(len(session.calls), 1)
                self.assertTrue(session.closed)
                self.assertEqual(len(session.cookies), 0)
                self.assertNotIn(SECRET, text)

    def test_cleanup_also_runs_on_constructor_failure_or_interrupt(self):
        for error_type in (RuntimeError, KeyboardInterrupt):
            session = ProbeSession(response(fixture()))
            with patch.object(probe, "make_session", return_value=session), \
                 patch.object(probe, "_load_client", return_value=unittest.mock.Mock(side_effect=error_type(SECRET))):
                code, result, text = self.invoke()
            self.assertNotEqual(code, 0)
            self.assertEqual(result["code"], "request_failed")
            self.assertTrue(session.closed)
            self.assertEqual(len(session.cookies), 0)
            self.assertNotIn(SECRET, text)

    def test_output_projection_failure_clears_session_and_input(self):
        session = ProbeSession(response(fixture()))
        request = probe.validate_payload(payload(), now=NOW)
        with patch.object(probe, "make_session", return_value=session), \
             patch.object(probe, "_public_result", side_effect=RuntimeError(SECRET)):
            with self.assertRaises(RuntimeError):
                probe.run_probe(request)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(len(session.cookies), 0)
        self.assertTrue(session.closed)
        self.assertEqual(request.cookies, [])
        self.assertEqual(request.user_agent, "")

    def test_invalid_input_and_arguments_never_echo_or_start_network(self):
        cases = [(b'{"secret":"' + SECRET.encode() + b'"}', []),
                 (b'{}', ["--cookie=" + SECRET]), (SECRET.encode(), [])]
        with patch.object(probe, "make_session", side_effect=AssertionError("network")):
            for raw, args in cases:
                code, result, text = self.invoke(raw, args)
                self.assertNotEqual(code, 0)
                self.assertFalse(result["ok"])
                self.assertNotIn(SECRET, text)

    def test_core_protection_failure_happens_before_read_or_request(self):
        stream = unittest.mock.Mock()
        out = io.StringIO()
        with patch.object(probe, "disable_core_dumps", side_effect=OSError(SECRET)), \
             patch.object(probe, "make_session", side_effect=AssertionError("network")):
            self.assertNotEqual(probe.main([], stdin=stream, stdout=out), 0)
        stream.read.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["code"], "environment_error")
        self.assertNotIn(SECRET, out.getvalue())

    def test_real_cli_validate_only_has_one_json_line_and_no_stderr(self):
        completed = subprocess.run([sys.executable, str(Path(probe.__file__)), "--validate-only"],
                                   input=json.dumps(payload()), text=True, capture_output=True,
                                   timeout=10, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(len(completed.stdout.splitlines()), 1)
        self.assertNotIn(SECRET, completed.stdout)
        self.assertEqual(json.loads(completed.stdout)["mode"], "validate-only")


if __name__ == "__main__":
    unittest.main()
