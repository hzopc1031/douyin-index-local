"""One-shot cloud probe. Login material arrives only through bounded stdin.

The sender deliberately narrows eligible browser cookies to creator.douyin.com
host-only cookies. This is not a claim to reconstruct their original scope.
No browser, credential file, automatic retry, or persistent worker is used here.
"""

from __future__ import annotations

import datetime as dt
import http.cookiejar
import json
import math
import platform
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Any


HOST = "creator.douyin.com"
TREND_URL = "https://creator.douyin.com/api/v2/index/get_multi_keyword_hot_trend"
TREND_PATH = "/api/v2/index/get_multi_keyword_hot_trend"
PAGE_URL = "https://creator.douyin.com/creator-micro/creator-count/arithmetic-index"
TZ = dt.timezone(dt.timedelta(hours=8))
MAX_INPUT_BYTES = 128 * 1024
MAX_COOKIES = 100
MAX_COOKIE_HEADER_BYTES = 32 * 1024
_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,256}\Z")
_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{0,8192}\Z")
_COOKIE_KEYS = {"name", "value", "domain", "path", "secure", "expires", "host_only"}
_PAYLOAD_KEYS = {"version", "user_agent", "cookies", "keyword", "end_date"}


class ProbeValidationError(ValueError):
    def __init__(self):
        super().__init__("会话或查询输入不符合协议。")


@dataclass(repr=False)
class ProbeInput:
    user_agent: str = field(repr=False)
    cookies: list[dict[str, Any]] = field(repr=False)
    keyword: str
    start_date: str
    end_date: str

    def clear(self):
        for cookie in self.cookies:
            cookie.clear()
        self.cookies.clear()
        self.user_agent = ""


def _require(condition):
    if not condition:
        raise ProbeValidationError()


def _clean_text(value: Any, maximum: int, *, ascii_only=False) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and value == value.strip()
            and not any(unicodedata.category(char).startswith("C") for char in value)
            and (not ascii_only or all(0x20 <= ord(char) <= 0x7e for char in value)))


def _path_matches(path: str) -> bool:
    return (TREND_PATH == path or (TREND_PATH.startswith(path)
            and (path.endswith("/") or TREND_PATH[len(path):].startswith("/"))))


def validate_payload(payload: dict, *, now: dt.datetime | None = None) -> ProbeInput:
    """Validate in memory, without imports of network clients or any I/O.

    All seven cookie attributes are mandatory. expires=null denotes a session
    cookie; an explicit expiry must be a future integer Unix timestamp. Only the
    sender's restricted, secure, host-only scope is accepted by this PoC.
    """
    _require(type(payload) is dict and set(payload) == _PAYLOAD_KEYS)
    _require(type(payload["version"]) is int and payload["version"] == 1)
    _require(_clean_text(payload["user_agent"], 1024, ascii_only=True))
    _require(_clean_text(payload["keyword"], 100))
    _require(isinstance(payload["end_date"], str)
             and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", payload["end_date"]) is not None)
    current = now if now is not None else dt.datetime.now(TZ)
    _require(isinstance(current, dt.datetime) and current.tzinfo is not None)
    try:
        end = dt.date.fromisoformat(payload["end_date"])
        start = end - dt.timedelta(days=6)
    except (ValueError, OverflowError):
        raise ProbeValidationError() from None
    _require(end < current.astimezone(TZ).date())
    cookies = payload["cookies"]
    _require(type(cookies) is list and 1 <= len(cookies) <= MAX_COOKIES)
    normalized = []
    seen = set()
    header_bytes = 0
    for cookie in cookies:
        _require(type(cookie) is dict and set(cookie) == _COOKIE_KEYS)
        name, value, path = cookie["name"], cookie["value"], cookie["path"]
        _require(isinstance(name, str) and _NAME.fullmatch(name) is not None)
        _require(isinstance(value, str) and _VALUE.fullmatch(value) is not None)
        _require(cookie["domain"] == HOST and cookie["host_only"] is True)
        _require(cookie["secure"] is True)
        _require(_clean_text(path, 1024, ascii_only=True) and path.startswith("/")
                 and ";" not in path and _path_matches(path))
        expiry = cookie["expires"]
        _require(expiry is None or (type(expiry) is int
                 and current.timestamp() < expiry <= 253402300799))
        _require(not name.startswith("__Host-") or path == "/")
        _require((name, path) not in seen)
        seen.add((name, path))
        header_bytes += len(name) + len(value) + 3
        _require(header_bytes <= MAX_COOKIE_HEADER_BYTES)
        normalized.append(dict(cookie))
    return ProbeInput(payload["user_agent"], normalized, payload["keyword"],
                      start.isoformat(), end.isoformat())


def _unique_object(pairs):
    output = {}
    for key, value in pairs:
        _require(key not in output)
        output[key] = value
    return output


def _reject_constant(_):
    raise ProbeValidationError()


def read_payload(stream) -> dict:
    """Read a single bounded JSON document. Never echo parser diagnostics."""
    _require(not getattr(stream, "isatty", lambda: False)())
    data = stream.read(MAX_INPUT_BYTES + 1)
    try:
        if isinstance(data, bytes):
            _require(len(data) <= MAX_INPUT_BYTES)
            data = data.decode("utf-8")
        _require(isinstance(data, str) and len(data.encode("utf-8")) <= MAX_INPUT_BYTES)
        value = json.loads(data, object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
        _require(type(value) is dict)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ProbeValidationError() from None
    finally:
        data = None


def disable_core_dumps():
    """Fail closed if this Unix process cannot disable credential-bearing dumps."""
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if sys.platform.startswith("linux"):
        # RLIMIT_CORE alone does not disable every piped Linux core collector.
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE = 4
            raise RuntimeError("process protection unavailable")
    import faulthandler
    import http.client
    import logging
    faulthandler.disable()
    http.client.HTTPConnection.debuglevel = 0
    logging.disable(logging.CRITICAL)


def _load_client():
    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12 or newer required")
    from client import DouyinIndexClient, TREND_URL as actual_endpoint
    if actual_endpoint != TREND_URL:
        raise RuntimeError("endpoint contract changed")
    return DouyinIndexClient


def make_session():
    """Keep host-only semantics even after requests merges its cookie jars."""
    import requests

    class StrictHostSession(requests.Session):
        def prepare_request(self, request):
            prepared = super().prepare_request(request)
            # Session.prepare_request creates a new jar with the default policy.
            prepared._cookies.set_policy(http.cookiejar.DefaultCookiePolicy(
                strict_ns_domain=http.cookiejar.DefaultCookiePolicy.DomainStrictNonDomain))
            prepared.headers.pop("Cookie", None)
            prepared.prepare_cookies(prepared._cookies)
            return prepared

    session = StrictHostSession()
    session.trust_env = False
    return session


def _add_cookies(session, request: ProbeInput):
    for value in request.cookies:
        session.cookies.set_cookie(http.cookiejar.Cookie(
            version=0, name=value["name"], value=value["value"], port=None,
            port_specified=False, domain=HOST, domain_specified=False,
            domain_initial_dot=False, path=value["path"], path_specified=True,
            secure=True, expires=value["expires"], discard=value["expires"] is None,
            comment=None, comment_url=None, rest={}, rfc2109=False))


def _public_result(result: dict, request: ProbeInput) -> dict:
    """Construct output from known fields; raw/envelope/extra fields never escape."""
    start = dt.date.fromisoformat(request.start_date)
    days = [(start + dt.timedelta(days=i)).isoformat() for i in range(7)]
    rows, seen = [], set()
    _require(type(result) is dict and type(result.get("rows")) is list)
    for original in result["rows"]:
        _require(type(original) is dict and original.get("keyword") == request.keyword)
        day = original.get("date")
        _require(isinstance(day, str) and day in days and day not in seen)
        seen.add(day)
        row = {"keyword": request.keyword, "date": day}
        for key in ("search_index", "composite_index"):
            number = original.get(key)
            _require(number is None or (type(number) in (int, float)
                     and number >= 0 and math.isfinite(number)))
            row[key] = number
        rows.append(row)
    rows.sort(key=lambda row: row["date"])
    metadata = result.get("metadata")
    _require(type(metadata) is dict and metadata.get("http_status") == 200)
    _require(metadata.get("encryption") in (None, "1", "2"))
    stamp = metadata.get("fetched_at")
    _require(isinstance(stamp, str) and len(stamp) <= 40)
    parsed_stamp = dt.datetime.fromisoformat(stamp)
    _require(parsed_stamp.tzinfo is not None)
    missing = metadata.get("missing_keywords")
    _require(missing in ([], [request.keyword]))
    search_days = {row["date"] for row in rows if row["search_index"] is not None}
    return {"ok": True, "rows": rows, "metadata": {
        "source": HOST, "metric": "search_index", "region": "全国", "platform": "aweme",
        "official_page": PAGE_URL, "endpoint": TREND_URL, "http_status": 200,
        "encryption": metadata.get("encryption"), "timezone": "Asia/Shanghai",
        "fetched_at": parsed_stamp.isoformat(timespec="seconds"),
        "start_date": request.start_date, "end_date": request.end_date,
        "page_comparison": "pending"}, "quality": {
        "keywords": [request.keyword], "keyword_count": 1, "requested_days": 7,
        "expected_rows": 7, "returned_rows": len(rows), "complete": len(search_days) == 7,
        "missing_dates": [day for day in days if day not in seen],
        "missing_search_dates": [day for day in days if day not in search_days],
        "missing_keywords": list(missing)}}


def run_probe(request: ProbeInput) -> dict:
    """Exactly one query; always discard the supplied session afterwards."""
    session = None
    try:
        client_type = _load_client()
        session = make_session()
        _add_cookies(session, request)
        client = client_type(session=session, user_agent=request.user_agent)
        result = client.query_many([request.keyword], request.start_date, request.end_date)
        return _public_result(result, request)
    finally:
        try:
            if session is not None:
                try:
                    session.cookies.clear()
                finally:
                    session.close()
        finally:
            request.clear()


def _failure(code: str) -> dict:
    messages = {
        "invalid_arguments": "参数无效；会话仅允许从标准输入接收。",
        "invalid_input": "会话或查询输入不符合协议。",
        "environment_error": "运行环境检查未通过；未发送请求。",
        "authentication_required": "登录会话不可用；本次请求已停止。",
        "access_denied": "平台拒绝访问；本次请求已停止。",
        "rate_limited": "平台限制请求；本次请求已停止。",
        "verification_required": "需要在官方页面人工验证；本次请求已停止。",
        "request_failed": "取数未完成；未重试，未输出原始错误。",
    }
    return {"ok": False, "code": code, "message": messages[code]}


def main(argv=None, *, stdin=None, stdout=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    output = sys.stdout if stdout is None else stdout
    payload = None
    request = None
    stage = "environment"
    code = 1
    try:
        if args not in ([], ["--validate-only"], ["--self-check"]):
            answer = _failure("invalid_arguments")
            code = 2
        else:
            disable_core_dumps()
            if args == ["--self-check"]:
                _load_client()
                answer = {"ok": True, "mode": "self-check", "python": platform.python_version()}
            else:
                stage = "input"
                stream = getattr(sys.stdin, "buffer", sys.stdin) if stdin is None else stdin
                payload = read_payload(stream)
                request = validate_payload(payload)
                if args == ["--validate-only"]:
                    answer = {"ok": True, "mode": "validate-only", "network_request": False,
                              "keyword": request.keyword, "start_date": request.start_date,
                              "end_date": request.end_date}
                else:
                    stage = "request"
                    answer = run_probe(request)
            code = 0
    except BaseException as exc:
        if stage == "input":
            answer = _failure("invalid_input")
        elif stage == "environment":
            answer = _failure("environment_error")
        else:
            category = getattr(exc, "code", "")
            answer = _failure(category if category in {
                "authentication_required", "access_denied", "rate_limited", "verification_required"
            } else "request_failed")
    finally:
        if request is not None:
            request.clear()
        if isinstance(payload, dict):
            for cookie in payload.get("cookies", []) if isinstance(payload.get("cookies"), list) else []:
                if isinstance(cookie, dict):
                    cookie.clear()
            payload.clear()
    try:
        output.write(json.dumps(answer, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
        output.flush()
    except BaseException:
        return 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
