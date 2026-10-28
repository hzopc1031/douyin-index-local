"""Synthetic offline transfer checks: no browser, SSH process or real cookies."""

import copy
import datetime as dt
import io
import json
import subprocess
import sys
import types
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

import login_transfer as transfer
from cloud_probe import ProbeValidationError


SENTINEL = "synthetic-login-cookie-not-real"
END_DATE = "2025-01-07"
KEYWORD = "银杏"


def browser_cookie(**changes):
    cookie = {"name": "sessionid", "value": SENTINEL, "domain": ".douyin.com",
              "path": "/", "secure": True, "expires": -1}
    cookie.update(changes)
    return cookie


def successful_reply():
    return {"ok": True, "rows": [
        {"keyword": KEYWORD, "date": f"2025-01-{day:02d}", "search_index": day * 10}
        for day in range(1, 8)]}


class TransferTests(unittest.TestCase):
    def test_ssh_requires_known_host_and_disables_retry_and_reuse(self):
        command = transfer.ssh_command("fixture-ec2", transfer.REMOTE_DIR)
        for option in ("StrictHostKeyChecking=yes", "BatchMode=yes", "ForwardAgent=no",
                       "ClearAllForwardings=yes", "ConnectionAttempts=1",
                       "ControlMaster=no", "ControlPath=none"):
            self.assertIn(option, command)
        self.assertIn("-T", command)
        self.assertEqual(command[-2], "fixture-ec2")
        self.assertNotIn("-t", command)
        self.assertNotIn(SENTINEL, " ".join(command))

    def test_rejects_command_injection_before_spawning_ssh(self):
        bad_arguments = [
            ("-oProxyCommand=bad", transfer.REMOTE_DIR, "query", None),
            ("fixture;bad", transfer.REMOTE_DIR, "query", None),
            ("fixture\nother", transfer.REMOTE_DIR, "query", None),
            ("fixture", "/srv/project;bad", "query", None),
            ("fixture", "/srv/../private", "query", None),
            ("fixture", "relative/path", "query", None),
            ("fixture", transfer.REMOTE_DIR, "query;bad", None),
            ("fixture", transfer.REMOTE_DIR, "query", "root;bad"),
            ("fixture", transfer.REMOTE_DIR, "query", "-root"),
        ]
        with patch.object(transfer.subprocess, "run") as run:
            for host, directory, mode, run_as in bad_arguments:
                with self.subTest(host=host, directory=directory, mode=mode, run_as=run_as):
                    with self.assertRaises(transfer.TransferError):
                        transfer.send(host, directory, mode=mode, run_as=run_as)
            run.assert_not_called()

    def test_optional_run_as_is_noninteractive_and_keeps_python_stdin(self):
        command = transfer.ssh_command("fixture", transfer.REMOTE_DIR,
                                       "validate-only", "work-aws-manager")
        self.assertTrue(command[-1].startswith("sudo -n -u work-aws-manager -- "))
        self.assertTrue(command[-1].endswith("cloud_probe.py --validate-only"))
        self.assertNotIn(" -S ", command[-1])

    def test_credentials_only_enter_pipe_once(self):
        payload = json.dumps({"cookie": SENTINEL}).encode()
        result = subprocess.CompletedProcess([], 0, b'{"ok":true}', SENTINEL.encode())
        with patch.object(transfer.subprocess, "run", return_value=result) as run:
            self.assertEqual(transfer.send("fixture", transfer.REMOTE_DIR, payload), {"ok": True})
        run.assert_called_once()
        arguments, options = run.call_args
        self.assertNotIn(SENTINEL, repr(arguments))
        self.assertEqual(options.pop("input"), payload)
        self.assertNotIn(SENTINEL, repr(options))
        self.assertEqual(options["stdout"], subprocess.PIPE)
        self.assertEqual(options["stderr"], subprocess.PIPE)
        self.assertNotIn("shell", options)
        self.assertNotIn("env", options)
        self.assertGreater(options["timeout"], 0)

    def test_connection_errors_are_sanitized_and_never_retried(self):
        failures = [OSError(SENTINEL), subprocess.TimeoutExpired(
            "ssh", 1, output=SENTINEL.encode(), stderr=SENTINEL.encode())]
        for failure in failures:
            with self.subTest(kind=type(failure).__name__):
                with patch.object(transfer.subprocess, "run", side_effect=failure) as run:
                    with self.assertRaises(transfer.TransferError) as raised:
                        transfer.send("fixture", transfer.REMOTE_DIR, SENTINEL.encode())
                run.assert_called_once()
                self.assertNotIn(SENTINEL, str(raised.exception))
                self.assertTrue(raised.exception.__suppress_context__)

    def test_malformed_or_failed_ssh_replies_do_not_echo_output(self):
        results = [
            (255, SENTINEL.encode()), (0, b"[]"), (0, b'{"ok":1}'),
            (1, b'{"ok":true}'), (0, b'{"ok":false}'),
            (0, b"x" * (transfer.MAX_OUTPUT + 1)),
        ]
        for returncode, stdout in results:
            with self.subTest(returncode=returncode, size=len(stdout)):
                completed = subprocess.CompletedProcess([], returncode, stdout, SENTINEL.encode())
                with patch.object(transfer.subprocess, "run", return_value=completed) as run:
                    with self.assertRaises(transfer.TransferError) as raised:
                        transfer.send("fixture", transfer.REMOTE_DIR)
                run.assert_called_once()
                self.assertNotIn(SENTINEL, str(raised.exception))

    def test_business_failure_is_returned_without_a_second_attempt(self):
        completed = subprocess.CompletedProcess([], 1, b'{"ok":false,"code":"rate_limited"}', b"")
        with patch.object(transfer.subprocess, "run", return_value=completed) as run:
            reply = transfer.send("fixture", transfer.REMOTE_DIR, SENTINEL.encode())
        self.assertEqual(reply, {"ok": False, "code": "rate_limited"})
        run.assert_called_once()

    def test_self_check_must_succeed_and_sends_no_credentials(self):
        completed = subprocess.CompletedProcess([], 1, b'{"ok":false}', b"")
        with patch.object(transfer.subprocess, "run", return_value=completed) as run:
            with self.assertRaises(transfer.TransferError):
                transfer.send("fixture", transfer.REMOTE_DIR, mode="self-check")
        self.assertEqual(run.call_args.kwargs["input"], b"")

    def test_payload_narrows_cookie_authority_without_modifying_source(self):
        original = [browser_cookie(secure=False), browser_cookie(
            name="second", domain="creator.douyin.com", path="/api", expires=2000000000)]
        before = copy.deepcopy(original)
        payload = json.loads(transfer.make_payload(original, "FixtureBrowser/1", KEYWORD, END_DATE))
        self.assertEqual(original, before)
        self.assertEqual(set(payload), {"version", "user_agent", "cookies", "keyword", "end_date"})
        self.assertEqual(len(payload["cookies"]), 2)
        for cookie in payload["cookies"]:
            self.assertEqual(cookie["domain"], "creator.douyin.com")
            self.assertIs(cookie["host_only"], True)
            self.assertIs(cookie["secure"], True)
        self.assertIsNone(payload["cookies"][0]["expires"])
        self.assertEqual(payload["cookies"][1]["expires"], 2000000000)

    def test_expired_cookie_is_omitted_not_refreshed(self):
        source = [browser_cookie(), browser_cookie(name="expired", expires=1)]
        payload = json.loads(transfer.make_payload(source, "FixtureBrowser/1", KEYWORD, END_DATE))
        self.assertEqual([item["name"] for item in payload["cookies"]], ["sessionid"])

    def test_rejects_cookie_authority_ambiguity_and_partitioning(self):
        sources = [
            [browser_cookie(domain="evil.douyin.com")],
            [browser_cookie(domain="douyin.com.evil.example")],
            [browser_cookie(partitionKey="https://example.com")],
            [browser_cookie(), browser_cookie(domain="creator.douyin.com")],
        ]
        for source in sources:
            with self.subTest(source=source):
                with self.assertRaises(transfer.TransferError) as raised:
                    transfer.make_payload(source, "FixtureBrowser/1", KEYWORD, END_DATE)
                self.assertNotIn(SENTINEL, str(raised.exception))

    def test_rejects_invalid_cookie_expiry(self):
        for expiry in (True, "2000000000", None, float("nan"), float("inf")):
            with self.subTest(expiry=expiry):
                with self.assertRaises(transfer.TransferError):
                    transfer.make_payload([browser_cookie(expires=expiry)], "FixtureBrowser/1", KEYWORD, END_DATE)

    def test_local_cloud_validation_rejects_nonmatching_path_and_header_injection(self):
        cases = [([browser_cookie(path="/unrelated")], "FixtureBrowser/1"),
                 ([browser_cookie(value=SENTINEL + "\r\nInjected: bad")], "FixtureBrowser/1"),
                 ([browser_cookie()], "FixtureBrowser/1\r\nInjected: bad"),
                 ([], "FixtureBrowser/1")]
        for cookies, agent in cases:
            with self.subTest(agent=agent, count=len(cookies)):
                with self.assertRaises(ProbeValidationError) as raised:
                    transfer.make_payload(cookies, agent, KEYWORD, END_DATE)
                self.assertNotIn(SENTINEL, str(raised.exception))

    def test_query_rejects_invalid_or_nonpast_dates_and_keyword(self):
        today = dt.datetime.now(transfer.SHANGHAI).date().isoformat()
        for keyword, date in (("", END_DATE), (" 银杏", END_DATE), ("银杏\n", END_DATE),
                              ("x" * 101, END_DATE), (KEYWORD, "2025-02-31"),
                              (KEYWORD, "20250107"), (KEYWORD, "0001-01-01"),
                              (KEYWORD, today), (KEYWORD, "9999-01-01")):
            with self.subTest(keyword=keyword, date=date):
                with self.assertRaises(transfer.TransferError):
                    transfer.validate_query(keyword, date)
        transfer.validate_query(KEYWORD, END_DATE)

    def test_output_prints_only_validated_values_and_distinguishes_missing(self):
        reply = successful_reply()
        reply["message"] = SENTINEL
        reply["rows"][0]["untrusted"] = SENTINEL
        output = io.StringIO()
        with redirect_stdout(output):
            transfer.display_reply(reply, keyword=KEYWORD, end_date=END_DATE)
        self.assertIn("完整七天数据", output.getvalue())
        self.assertNotIn(SENTINEL, output.getvalue())
        reply["rows"][0]["search_index"] = None
        output = io.StringIO()
        with redirect_stdout(output):
            transfer.display_reply(reply, keyword=KEYWORD, end_date=END_DATE)
        self.assertIn("不完整数据（未补零）", output.getvalue())
        self.assertIn("2025-01-01 缺失", output.getvalue())

    def test_output_rejects_invalid_dates_values_and_keywords_before_printing(self):
        for change in ({"date": "2025-02-31"}, {"date": "2025-01-08"},
                       {"date": "2025-01-02"}, {"keyword": "unexpected"},
                       {"search_index": True}, {"search_index": -1},
                       {"search_index": float("nan")}, {"search_index": SENTINEL}):
            reply = successful_reply()
            reply["rows"][0].update(change)
            output = io.StringIO()
            with self.subTest(change=change), redirect_stdout(output):
                with self.assertRaises(transfer.TransferError):
                    transfer.display_reply(reply, keyword=KEYWORD, end_date=END_DATE)
            self.assertEqual(output.getvalue(), "")

    def test_unknown_remote_error_is_not_printed(self):
        output = io.StringIO()
        with redirect_stdout(output):
            transfer.display_reply({"ok": False, "code": SENTINEL, "message": SENTINEL})
        self.assertNotIn(SENTINEL, output.getvalue())
        self.assertIn("probe_failed", output.getvalue())


class LoginFlowTests(unittest.TestCase):
    def fake_main(self, *, answer="SEND", url=transfer.PAGE_URL, debug=None,
                  payload_error=None, query_error=None):
        """All browser and transport objects are in-memory mocks."""
        browser = MagicMock()
        context = browser.new_context.return_value
        page = context.new_page.return_value
        page.url = url
        page.evaluate.return_value = "FixtureBrowser/1"
        context.cookies.return_value = [browser_cookie()]
        runtime = MagicMock()
        runtime.__enter__.return_value.chromium.launch.return_value = browser
        sync_api = types.ModuleType("playwright.sync_api")
        sync_api.sync_playwright = MagicMock(return_value=runtime)
        playwright = types.ModuleType("playwright")
        playwright.__path__ = []
        resource = types.ModuleType("resource")
        resource.RLIMIT_CORE = 4
        resource.setrlimit = MagicMock()
        output, errors = io.StringIO(), io.StringIO()
        args = ["login_transfer.py", "--ssh-host", "fixture-ec2", "--run-as", "work-aws-manager",
                "--keyword", KEYWORD, "--end-date", END_DATE]
        responses = [{"ok": True}, query_error if query_error else successful_reply()]
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"playwright": playwright,
                                                        "playwright.sync_api": sync_api,
                                                        "resource": resource}))
            stack.enter_context(patch.dict(transfer.os.environ, debug or {}, clear=True))
            stack.enter_context(patch.object(sys, "argv", args))
            stack.enter_context(patch("builtins.input", return_value=answer))
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(errors))
            send = stack.enter_context(patch.object(transfer, "send", side_effect=responses))
            if payload_error:
                stack.enter_context(patch.object(transfer, "make_payload", side_effect=payload_error))
            # Any accidental process launch in these tests must fail immediately.
            spawn = stack.enter_context(patch.object(transfer.subprocess, "run", side_effect=AssertionError("unexpected SSH")))
            status = transfer.main()
        spawn.assert_not_called()
        return status, send, browser, context, sync_api, output.getvalue() + errors.getvalue()

    def test_success_extracts_only_fixed_endpoint_cookies_and_one_query(self):
        status, send, browser, context, api, output = self.fake_main()
        self.assertEqual(status, 0)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0].kwargs["mode"], "self-check")
        self.assertEqual(len(send.call_args_list[0].args), 2)
        self.assertEqual(send.call_args_list[1].kwargs["run_as"], "work-aws-manager")
        payload = json.loads(send.call_args_list[1].args[2])
        self.assertEqual(payload["cookies"][0]["value"], SENTINEL)
        context.cookies.assert_called_once_with([transfer.TREND_URL])
        browser.new_context.assert_called_once_with(accept_downloads=False)
        context.close.assert_called_once()
        self.assertTrue(browser.close.called)
        self.assertNotIn(SENTINEL, output)

    def test_cancel_never_extracts_or_transfers_cookies(self):
        status, send, browser, context, api, output = self.fake_main(answer="CANCEL")
        self.assertEqual(status, 0)
        send.assert_called_once()
        context.cookies.assert_not_called()
        self.assertTrue(browser.close.called)

    def test_wrong_origin_or_nonhttps_is_rejected_before_cookie_extraction(self):
        for url in ("http://creator.douyin.com/", "https://creator.douyin.com.evil.example/",
                    "https://user@creator.douyin.com/", "https://creator.douyin.com:444/"):
            with self.subTest(url=url):
                status, send, browser, context, api, output = self.fake_main(url=url)
                self.assertEqual(status, 1)
                send.assert_called_once()
                context.cookies.assert_not_called()
                self.assertTrue(browser.close.called)

    def test_debug_environment_blocks_both_browser_and_transport(self):
        for variable in ("DEBUG", "PWDEBUG"):
            with self.subTest(variable=variable):
                status, send, browser, context, api, output = self.fake_main(debug={variable: "1"})
                self.assertEqual(status, 1)
                send.assert_not_called()
                api.sync_playwright.assert_not_called()

    def test_local_payload_failure_closes_browser_without_sending_secrets(self):
        status, send, browser, context, api, output = self.fake_main(payload_error=RuntimeError(SENTINEL))
        self.assertEqual(status, 1)
        send.assert_called_once()
        self.assertTrue(browser.close.called)
        self.assertNotIn(SENTINEL, output)

    def test_query_failure_never_retries_and_hides_raw_error(self):
        status, send, browser, context, api, output = self.fake_main(query_error=RuntimeError(SENTINEL))
        self.assertEqual(status, 1)
        self.assertEqual(send.call_count, 2)
        context.close.assert_called_once()
        self.assertTrue(browser.close.called)
        self.assertNotIn(SENTINEL, output)


if __name__ == "__main__":
    unittest.main()
