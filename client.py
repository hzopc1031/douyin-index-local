"""Single-account, small-batch Douyin search-index verification client.

Protocol references (inspected 2026-10-27; independent implementation):
https://github.com/Ecow0ker/douyin-index-tool/blob/main/src/douyin_index_tool/api.py
https://github.com/Ecow0ker/douyin-index-tool/blob/main/src/douyin_index_tool/crypto.py

Cookies are supplied in memory through requests.Session.cookies. This module
never reads a browser profile, saves credentials, retries, or follows redirects.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import math
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from requests.adapters import HTTPAdapter

ORIGIN = "https://creator.douyin.com"
PAGE_URL = ORIGIN + "/creator-micro/creator-count/arithmetic-index"
TREND_URL = ORIGIN + "/api/v2/index/get_multi_keyword_hot_trend"
VALID_DATE_URL = ORIGIN + "/api/v2/index/get_keyword_valid_date"
SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# Each trend or extension task sends exactly one POST, with no automatic retries.
REQUEST_CONNECT_TIMEOUT = 10
REQUEST_READ_TIMEOUT = 20
# The deadline is checked between chunks, so one blocking read may add up to
# REQUEST_READ_TIMEOUT: worst case per request is TOTAL_DEADLINE + READ_TIMEOUT.
REQUEST_TOTAL_DEADLINE = 35
_AES = {
    "1": ("91b4a33aa9fd3bb00f2ea519c5d0a44d", "26e84bd5c395e491fda23cedda0d843e"),
    "2": ("4a35db61325bef35e8513a1289c50bdc", "39e90c2e3821460f2f957fcf7a62dcf9"),
}


class SanitizedClientError(RuntimeError):
    """Only stable, non-sensitive diagnostics are exposed to callers."""

    def __init__(self, code: str, message: str, *, http_status: int | None = None,
                 business_code: int | str | None = None, keyword_details=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.business_code = business_code
        self.keyword_details = keyword_details

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self),
                "http_status": self.http_status, "business_code": self.business_code,
                **({"keyword_details":self.keyword_details} if self.keyword_details else {})}


def _error(code: str, message: str) -> SanitizedClientError:
    return SanitizedClientError(code, message)


def _json_loads(text: str) -> Any:
    def reject_constant(_: str) -> None:
        raise ValueError("Non-finite JSON number")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    return json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_object)


def _decode(payload: dict[str, Any], encryption: str | None) -> Any | None:
    if not encryption:
        return None
    if encryption not in _AES:
        raise _error("unsupported_encryption", "接口响应加密版本已变化，请检查兼容性。")
    value = payload.get("data")
    if not isinstance(value, str):
        raise _error("response_schema", "加密响应缺少预期的字符串数据。")
    try:
        ciphertext = base64.b64decode(value, validate=True)
        if not ciphertext or len(ciphertext) % 16:
            raise ValueError("Invalid ciphertext size")
        key, iv = (bytes.fromhex(part) for part in _AES[encryption])
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
        result = _json_loads(plaintext.decode("utf-8"))
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise _error("response_decode", "接口响应无法按已知格式解码。") from None
    if not isinstance(result, dict):
        raise _error("response_schema", "解码后的接口响应不是对象。")
    return result


def _safe_business_code(value: Any) -> int | str:
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,10}", value):
        return value
    return "unrecognized"


def _check_business_status(payload: dict[str, Any], http_status: int) -> None:
    statuses: list[Any] = [payload[key] for key in ("status", "status_code", "code") if key in payload]
    message = " ".join(str(payload.get(key, "")) for key in ("msg", "message")).lower()
    if "BaseResp" in payload:
        base = payload["BaseResp"]
        if not isinstance(base, dict) or "StatusCode" not in base:
            raise _error("response_schema", "接口业务状态结构已变化。")
        statuses.append(base["StatusCode"])
        message += " " + str(base.get("StatusMessage", "")).lower()
    failed = [value for value in statuses if type(value) not in (int, str) or value not in (0, "0")]
    # Challenge notices are actionable even when a backend omits an error code.
    challenge = any(word in message for word in ("captcha", "verify", "risk", "验证码", "验证", "风控"))
    rate = any(word in message for word in ("rate limit", "too many", "频繁", "限频"))
    login = any(word in message for word in ("login", "登录", "session expired", "cookie expired"))
    if not failed and not challenge and not rate and not login:
        return
    code = _safe_business_code(failed[0]) if failed else None
    if challenge:
        category, description = "verification_required", "平台要求安全验证，请在官方页面处理后再继续。"
    elif rate or code in (429, "429"):
        category, description = "rate_limited", "平台返回限频信号，本次请求已停止。"
    elif login or code in (401, "401"):
        category, description = "authentication_required", "平台要求重新登录，请在官方页面处理。"
    else:
        category, description = "business_error", "平台返回未成功的业务状态，本次请求已停止。"
    raise SanitizedClientError(category, description, http_status=http_status, business_code=code)


def _payload_object(envelope: dict[str, Any], http_status: int, *,
                    allow_empty_data: bool = False) -> dict[str, Any] | None:
    _check_business_status(envelope, http_status)
    if "data" not in envelope:
        return envelope
    data = envelope["data"]
    if data is None and allow_empty_data:
        return None
    if not isinstance(data, dict):
        raise _error("response_schema", "接口业务数据不是预期的对象。")
    _check_business_status(data, http_status)
    return data


def _date(value: Any) -> dt.date:
    if isinstance(value, bool) or value is None:
        raise _error("response_schema", "接口包含无法识别的日期。")
    text = str(value).strip()
    try:
        if re.fullmatch(r"[0-9]{8}", text):
            return dt.datetime.strptime(text, "%Y%m%d").date()
        if re.fullmatch(r"[0-9]{10}|[0-9]{13}", text):
            seconds = int(text) / (1000 if len(text) == 13 else 1)
            return dt.datetime.fromtimestamp(seconds, SHANGHAI).date()
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
            return dt.date.fromisoformat(text)
        stamp = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=SHANGHAI)
        return stamp.astimezone(SHANGHAI).date()
    except (ValueError, OverflowError, OSError):
        raise _error("response_schema", "接口包含无法识别的日期。") from None


def _number(value: Any) -> int | float | None:
    if value is None or (isinstance(value, str) and value.strip() in ("", "-", "--")):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _error("response_schema", "接口指数不是有效数值。")
    try:
        parsed = Decimal(str(value).strip())
        if not parsed.is_finite() or parsed < 0:
            raise ValueError("Invalid index")
        if parsed == parsed.to_integral_value():
            return int(parsed)
        result = float(parsed)
        if not math.isfinite(result):
            raise ValueError("Invalid index")
        return result
    except (ValueError, InvalidOperation, OverflowError):
        raise _error("response_schema", "接口指数不是有效数值。") from None


def _series(value: Any, expected: set[str]) -> dict[str, int | float | None]:
    if not isinstance(value, list):
        raise _error("response_schema", "接口指数序列不是列表。")
    output: dict[str, int | float | None] = {}
    for point in value:
        if not isinstance(point, dict) or "index" not in point:
            raise _error("response_schema", "接口指数记录缺少必要字段。")
        if "datetime" not in point and "date" not in point:
            raise _error("response_schema", "接口指数记录缺少日期。")
        day = _date(point.get("datetime", point.get("date"))).isoformat()
        if "datetime" in point and "date" in point and _date(point["date"]).isoformat() != day:
            raise _error("response_schema", "接口同一条记录包含冲突日期。")
        if day not in expected:
            raise _error("unexpected_date", "接口返回了请求范围外的日期。")
        if day in output:
            raise _error("duplicate_date", "接口返回了重复日期，不能进行逐日核对。")
        output[day] = _number(point["index"])
    return output


def normalize_keyword(value: str) -> str:
    """Ignore boundary whitespace and whitespace between Han characters only."""
    han = r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]"
    return re.sub(r"(?<=" + han + r")\s+(?=" + han + r")", "", value.strip())


def parse_trend(payload: dict[str, Any], keyword: str, start: dt.date,
                end: dt.date) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate a known business payload without making any network request."""
    groups = payload.get("hot_list")
    if not isinstance(groups, list):
        raise _error("response_schema", "趋势响应缺少关键词列表。")
    days = [(start + dt.timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    expected = set(days)
    if not groups:
        search: dict[str, int | float | None] = {}
        composite: dict[str, int | float | None] = {}
    else:
        if len(groups) != 1 or not isinstance(groups[0], dict) or not isinstance(groups[0].get("keyword"), str) or normalize_keyword(groups[0]["keyword"]) != normalize_keyword(keyword):
            raise _error("unexpected_keyword", "接口返回的关键词与请求不完全一致。")
        group = groups[0]
        if "search_hot_list" not in group:
            raise _error("response_schema", "趋势响应缺少搜索指数序列。")
        search = _series(group["search_hot_list"], expected)
        composite = _series(group.get("hot_list", []), expected)
    returned = set(search) | set(composite)
    rows = [{"keyword": keyword, "date": day, "search_index": search.get(day),
             "composite_index": composite.get(day)} for day in sorted(returned)]
    missing_dates = [day for day in days if day not in returned]
    missing_search = [day for day in days if search.get(day) is None]
    return rows, {"requested_days": len(days), "returned_days": len(returned),
                  "complete": not missing_search, "missing_dates": missing_dates,
                  "missing_search_dates": missing_search}


def parse_trends(payload: dict[str, Any], keywords: list[str], start: dt.date,
                 end: dt.date) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Match returned groups by their exact keyword, never by response order."""
    groups = payload.get("hot_list")
    if not isinstance(groups, list):
        raise _error("response_schema", "趋势响应缺少关键词列表。")
    requested = {normalize_keyword(word): word for word in keywords}
    if len(requested) != len(keywords):
        raise _error("duplicate_keyword", "请求词在空白规范化后重复，请只保留一个。")
    returned_keywords = {}
    by_keyword: dict[str, dict[str, Any]] = {}
    for group in groups:
        if not isinstance(group, dict):
            raise _error("response_schema", "接口关键词记录不是对象。")
        keyword = group.get("keyword")
        if not isinstance(keyword, str) or normalize_keyword(keyword) not in requested:
            details={"requested":list(keywords),"returned":[
                "".join(c for c in g.get('keyword','') if ord(c)>=32)[:100] if isinstance(g,dict) and isinstance(g.get('keyword'),str) else "[关键词字段缺失或类型异常]"
                for g in groups[:10]]}
            raise SanitizedClientError("unexpected_keyword", "接口返回了请求之外或无法识别的关键词。", keyword_details=details)
        original_keyword = keyword
        keyword = requested[normalize_keyword(keyword)]
        if keyword in by_keyword:
            raise _error("duplicate_keyword", "接口重复返回同一关键词，不能进行逐日核对。")
        by_keyword[keyword] = group
        returned_keywords[keyword] = original_keyword
    rows: list[dict[str, Any]] = []
    quality_by_keyword: dict[str, dict[str, Any]] = {}
    for keyword in keywords:
        group = by_keyword.get(keyword)
        word_rows, quality = parse_trend({"hot_list": [group] if group is not None else []},
                                        keyword, start, end)
        rows.extend(word_rows)
        quality_by_keyword[keyword] = quality
    requested_days = (end - start).days + 1
    return rows, {"keywords": list(keywords), "returned_keywords": returned_keywords,
                  "keyword_match_rule": "trim_and_ignore_whitespace_between_han",
                  "keyword_count": len(keywords),
                  "requested_days": requested_days, "expected_rows": requested_days * len(keywords),
                  "returned_rows": len(rows),
                  "complete": all(item["complete"] for item in quality_by_keyword.values()),
                  "missing_keywords": [word for word in keywords if word not in by_keyword],
                  "quality_by_keyword": quality_by_keyword}


class DouyinIndexClient:
    def __init__(self, session: requests.Session | None = None, user_agent: str | None = None,
                 timeout: float = REQUEST_READ_TIMEOUT):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.session = session if session is not None else requests.Session()
        self.session.trust_env = False
        self.session.mount("https://", HTTPAdapter(max_retries=0))
        self.timeout = timeout
        self.headers = {
            "Accept": "application/json, text/plain, */*", "Origin": ORIGIN,
            "Referer": PAGE_URL, "appsource": "PC", "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json; charset=UTF-8",
        }
        if user_agent:
            if "\r" in user_agent or "\n" in user_agent:
                raise ValueError("Invalid user agent")
            self.headers["User-Agent"] = user_agent

    def _post(self, endpoint: str, body: dict[str, Any], *, allow_empty_data: bool = False) -> dict[str, Any]:
        if endpoint not in (TREND_URL, VALID_DATE_URL, ORIGIN+'/api/v2/index/get_relation_word', ORIGIN+'/api/v2/index/get_portrait'):
            raise ValueError("Unsupported endpoint")
        response = None
        started = time.monotonic()
        try:
            response = self.session.post(endpoint, json=body, headers=self.headers,
                                         timeout=(REQUEST_CONNECT_TIMEOUT, self.timeout), allow_redirects=False,
                                         stream=True, verify=True)
            status = response.status_code
            if status != 200:
                category = {401: "authentication_required", 403: "access_denied",
                            429: "rate_limited"}.get(status, "http_error")
                raise SanitizedClientError(category, "平台未返回成功响应，本次请求已停止。",
                                           http_status=status)
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=65536):
                if time.monotonic() - started > REQUEST_TOTAL_DEADLINE:
                    raise _error("network_error", "平台响应超过总时限，本次请求已停止；未自动重试。")
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise _error("response_too_large", "接口响应超过本地验证允许的大小。")
                chunks.append(chunk)
            try:
                envelope = _json_loads(b"".join(chunks).decode("utf-8-sig"))
            except (ValueError, UnicodeError):
                raise _error("response_format", "接口未返回有效 JSON，可能需要在官方页面检查登录或验证。") from None
            if not isinstance(envelope, dict):
                raise _error("response_schema", "接口响应不是对象。")
            _check_business_status(envelope, status)
            encryption = response.headers.get("x-encrypted") or None
            decoded = _decode(envelope, encryption)
            payload = decoded if decoded is not None else envelope
            if endpoint.endswith('/get_portrait') and isinstance(payload.get('data'), list):
                _check_business_status(payload, status)
                data = {'data': payload['data']}
            else:
                data = _payload_object(payload, status, allow_empty_data=allow_empty_data and
                                       endpoint in (ORIGIN+'/api/v2/index/get_relation_word', ORIGIN+'/api/v2/index/get_portrait'))
            return {"raw": {"response": envelope, "decoded": decoded}, "data": data,
                    "metadata": {"endpoint": endpoint, "http_status": status,
                                 "encryption": encryption, "timezone": "Asia/Shanghai",
                                 "fetched_at": dt.datetime.now(SHANGHAI).isoformat(timespec="seconds")}}
        except requests.RequestException:
            raise _error("network_error", "网络请求失败；未自动重试，请检查连接后再操作。") from None
        finally:
            if response is not None:
                response.close()

    def query(self, keyword: str, start_date: str, end_date: str) -> dict[str, Any]:
        """Single-keyword compatibility API, preserving its original quality fields."""
        result = self.query_many([keyword], start_date, end_date)
        result["metadata"].update({"keyword": keyword,
                                  **result["metadata"]["quality_by_keyword"][keyword]})
        return result

    def query_many(self, keywords: list[str], start_date: str, end_date: str) -> dict[str, Any]:
        """Read one bounded date-range batch with exactly one request and no retries."""
        if not isinstance(keywords, list) or not 1 <= len(keywords) <= 5:
            raise ValueError("请输入 1—5 个关键词。")
        if any(not isinstance(word, str) or not word.strip() or word != word.strip()
               or len(word) > 100 for word in keywords):
            raise ValueError("每个关键词须为 1—100 字，且首尾不能有空白。")
        if len(set(keywords)) != len(keywords):
            raise ValueError("关键词不能重复。")
        keywords = list(keywords)
        try:
            start, end = dt.date.fromisoformat(start_date), dt.date.fromisoformat(end_date)
        except (TypeError, ValueError):
            raise ValueError("Dates must use YYYY-MM-DD") from None
        if start.isoformat() != start_date or end.isoformat() != end_date:
            raise ValueError("Dates must use YYYY-MM-DD")
        if not 0 <= (end - start).days < 731:
            raise ValueError("Query requires 1 to 731 inclusive days")
        if end >= dt.datetime.now(SHANGHAI).date():
            raise ValueError("The last day must be earlier than today in Asia/Shanghai")
        result = self._post(TREND_URL, {"keyword_list": keywords,
                            "start_date": start.strftime("%Y%m%d"),
                            "end_date": end.strftime("%Y%m%d"), "app_name": "aweme"})
        rows, quality = parse_trends(result.pop("data"), keywords, start, end)
        result["rows"] = rows
        result["metadata"].update({"start_date": start_date,
                                   "end_date": end_date, "metric": "search_index", **quality})
        return result

    def latest_valid_date(self) -> dict[str, Any]:
        result = self._post(VALID_DATE_URL, {"app_name": "aweme"})
        data = result.pop("data")
        candidates = [_date(data[key]).isoformat() for key in
                      ("end_date", "latest_date", "date", "datetime", "day") if key in data]
        if not candidates or len(set(candidates)) != 1:
            raise _error("response_schema", "有效日期响应缺失或存在冲突，请以官方页面为准。")
        result["date"] = candidates[0]
        return result
