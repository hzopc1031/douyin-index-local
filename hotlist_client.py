"""Unauthenticated Douyin hot-list client.

One GET to the public snssdk board. TLS is verified, redirects are not
followed, and failures are not retried. Cookies are never read or written.
This is not the creator-center search index.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from typing import Any
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter

HOT_LIST_URL = "https://aweme.snssdk.com/aweme/v1/hot/search/list/"
SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 100
MAX_POSITION = 500
REQUEST_CONNECT_TIMEOUT = 10
REQUEST_READ_TIMEOUT = 20
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


class HotListError(RuntimeError):
    def __init__(self, code: str, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "http_status": self.http_status}


def _error(code: str, message: str, *, http_status: int | None = None) -> HotListError:
    return HotListError(code, message, http_status=http_status)


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


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or type(value) is not int:
        raise _error("incomplete", "热榜字段不是整数，本次快照不入库。")
    return value


def _optional_heat(value: Any) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error("incomplete", "热榜热度不是数值，本次快照不入库。")
    if isinstance(value, float) and not math.isfinite(value):
        raise _error("incomplete", "热榜热度不是有限数值，本次快照不入库。")
    if value < 0:
        raise _error("incomplete", "热榜热度为负，本次快照不入库。")
    return value


def _optional_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise _error("incomplete", "热榜标识无效，本次快照不入库。")
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise _error("incomplete", "热榜标识无效，本次快照不入库。")
    return value


def _word(value: Any) -> str:
    if not isinstance(value, str):
        raise _error("incomplete", "热榜话题不是文本，本次快照不入库。")
    word = value.strip()
    if not word or len(word) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in word):
        raise _error("incomplete", "热榜话题为空或无法保存，本次快照不入库。")
    return word


def project_hot_list(payload: Any, *, fetched_at: str | None = None) -> dict[str, Any]:
    """Project one decoded board. Incomplete boards raise and are not stored."""
    if not isinstance(payload, dict) or type(payload.get("status_code")) is not int or payload.get("status_code") != 0:
        raise _error("incomplete", "热榜状态码不是 0，本次快照不入库。")
    data = payload.get("data")
    rows = data.get("word_list") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows or len(rows) > MAX_ITEMS:
        raise _error("incomplete", "热榜词列表为空或无法核验，本次快照不入库。")
    items: list[dict[str, Any]] = []
    seen_words: set[str] = set()
    seen_positions: set[int] = set()
    seen_keys: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise _error("incomplete", "热榜词条不是对象，本次快照不入库。")
        word = _word(row.get("word"))
        position = row.get("position")
        if isinstance(position, bool) or type(position) is not int or not 1 <= position <= MAX_POSITION:
            raise _error("incomplete", "热榜名次无效，本次快照不入库。")
        sentence_id = _optional_id(row.get("sentence_id"))
        item_key = "id:" + sentence_id if sentence_id else "word:" + word
        if word in seen_words or position in seen_positions or item_key in seen_keys:
            raise _error("incomplete", "热榜词或名次重复，本次快照不入库。")
        seen_words.add(word)
        seen_positions.add(position)
        seen_keys.add(item_key)
        # Only allowlisted scalars. Nested media and unknown keys are dropped.
        items.append({
            "item_key": item_key,
            "key_kind": "sentence_id" if sentence_id else "word",
            "sentence_id": sentence_id,
            "word": word,
            "position": position,
            "hot_value": _optional_heat(row.get("hot_value")),
            "label": _optional_int(row.get("label")),
            "event_time": _optional_int(row.get("event_time")),
            "video_count": _optional_int(row.get("video_count")),
            "sentence_tag": _optional_int(row.get("sentence_tag")),
            "word_type": _optional_int(row.get("word_type")),
            "group_id": _optional_id(row.get("group_id")),
        })
    if fetched_at is None:
        fetched_at = dt.datetime.now(SHANGHAI).isoformat(timespec="seconds")
    return {"complete": True, "board": "word_list", "source": HOT_LIST_URL,
            "fetched_at": fetched_at, "items": items}


class HotListClient:
    def __init__(self, session: requests.Session | None = None):
        self.session = session if session is not None else requests.Session()
        self._owns_session = session is None
        self.session.trust_env = False
        self.session.mount("https://", HTTPAdapter(max_retries=0))

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    def fetch(self) -> dict[str, Any]:
        """Fetch the snssdk board once. The caller stores only a complete result."""
        response = None
        try:
            response = self.session.get(
                HOT_LIST_URL, headers=_HEADERS,
                timeout=(REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT),
                allow_redirects=False, stream=True, verify=True)
            status = response.status_code
            if status in _REDIRECTS:
                raise _error("redirect_rejected", "热榜响应要求跳转，已拒绝并停止。", http_status=status)
            if status == 429:
                raise _error("rate_limited", "热榜返回限频，已停止，未自动重试。", http_status=status)
            if status == 403:
                raise _error("access_denied", "热榜拒绝访问，已停止，未自动重试。", http_status=status)
            if status != 200:
                raise _error("http_error", "热榜未返回成功响应，本次请求已停止。", http_status=status)
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise _error("response_too_large", "热榜响应超过允许的大小，本次快照不入库。",
                                 http_status=status)
                chunks.append(chunk)
            try:
                payload = _json_loads(b"".join(chunks).decode("utf-8-sig"))
            except (ValueError, UnicodeError):
                raise _error("response_format", "热榜响应不是 JSON，已停止，未自动重试。",
                             http_status=status) from None
            return project_hot_list(payload)
        except requests.RequestException:
            raise _error("network_error", "热榜网络请求失败；未自动重试。") from None
        finally:
            if response is not None:
                response.close()
