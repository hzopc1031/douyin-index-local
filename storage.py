"""Local, credential-free batch files and exact-query cache.

Only business results supplied by the caller are written. This module never
reads browser profiles, cookies, environment credentials, or the network.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Shanghai")
_SCOPE = {"metric": "search_index", "region": "全国", "platform": "aweme"}
_CSV_FIELDS = ["keyword", "date", "search_index", "composite_index"]
_MAX_FILE_BYTES = 10 * 1024 * 1024


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _read_json(path: Path) -> Any:
    if path.is_symlink() or path.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError("结果文件类型或大小不符合要求。")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("结果文件包含重复字段。")
            result[key] = value
        return result

    def reject_nonfinite(value):
        raise ValueError("结果文件包含非有限数值。")

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                      parse_constant=reject_nonfinite)


def _atomic_write(path: Path, content: bytes) -> None:
    """Replace one file atomically in its owner-only result directory."""
    if path.is_symlink():
        raise ValueError("不能写入符号链接结果文件。")
    descriptor, temp_name = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        # Windows does not expose fchmod; its files inherit the directory ACL.
        # Callers should keep the result directory in a private user location.
        if hasattr(os, 'fchmod'):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")


def _query(keywords: Any, start_date: Any, end_date: Any) -> tuple[list[str], list[str]]:
    if not isinstance(keywords, (list, tuple)) or not 1 <= len(keywords) <= 5:
        raise ValueError("关键词数量必须为 1—5。")
    words = list(keywords)
    if any(not isinstance(word, str) or not word.strip() or word != word.strip()
           or len(word) > 100 for word in words) or len(set(words)) != len(words):
        raise ValueError("关键词必须唯一、非空且不超过 100 字。")
    if not isinstance(start_date, str) or not isinstance(end_date, str):
        raise ValueError("日期必须使用 YYYY-MM-DD。")
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start.isoformat() != start_date or end.isoformat() != end_date or not 0 <= (end - start).days < 731:
        raise ValueError("结果区间须包含首尾共 1—731 天。")
    return words, [(start + timedelta(days=i)).isoformat() for i in range((end-start).days+1)]


def _numeric(value: Any) -> bool:
    return (type(value) in (int, float) and value >= 0
            and (not isinstance(value, float) or math.isfinite(value)))


def _complete_rows(result: dict[str, Any], words: list[str], days: list[str]) -> bool:
    metadata, rows = result.get("metadata"), result.get("rows")
    if not isinstance(metadata, dict) or metadata.get("complete") is not True or not isinstance(rows, list):
        return False
    expected = [(word, day) for word in words for day in days]
    if len(rows) != len(expected):
        return False
    for row, pair in zip(rows, expected):
        if not isinstance(row, dict) or (row.get("keyword"), row.get("date")) != pair:
            return False
        if not _numeric(row.get("search_index")):
            return False
        if row.get("composite_index") is not None and not _numeric(row["composite_index"]):
            return False
    quality = metadata.get("quality_by_keyword")
    if quality is not None:
        if not isinstance(quality, dict) or set(quality) != set(words):
            return False
        for word in words:
            item = quality[word]
            if not isinstance(item, dict) or item.get("complete") is not True:
                return False
            if item.get("missing_dates") or item.get("missing_search_dates"):
                return False
    return True


def _csv_keyword(keyword: str) -> str:
    # CSV quoting alone does not prevent spreadsheet formula evaluation.
    prefix = keyword.lstrip(" \t\r\n")
    if keyword.startswith(("\t", "\r", "\n")) or prefix.startswith(("=", "+", "-", "@")):
        return "'" + keyword
    return keyword


class ResultStore:
    def __init__(self, root: str | Path, ttl_hours: float = 24):
        if not isinstance(ttl_hours, (int, float)) or isinstance(ttl_hours, bool) or not math.isfinite(ttl_hours) or ttl_hours <= 0:
            raise ValueError("缓存有效期必须是正数小时。")
        self.root = Path(root).expanduser().resolve()
        self.ttl = timedelta(hours=ttl_hours)

    def lookup(self, keywords: list[str], start_date: str, end_date: str) -> dict[str, Any] | None:
        words, days = _query(keywords, start_date, end_date)
        if not self.root.is_dir():
            return None
        now = datetime.now(timezone.utc)
        hits: list[tuple[datetime, Path, dict[str, Any]]] = []
        for folder in self.root.iterdir():
            if folder.is_symlink() or not folder.is_dir() or folder.name.startswith("."):
                continue
            try:
                result = _read_json(folder / "result.json")
                metadata = result.get("metadata") if isinstance(result, dict) else None
                if not isinstance(metadata, dict):
                    continue
                if metadata.get("keywords") != words or metadata.get("start_date") != start_date or metadata.get("end_date") != end_date:
                    continue
                if any(metadata.get(key) != value for key, value in _SCOPE.items()):
                    continue
                stamp = datetime.fromisoformat(metadata["fetched_at"])
                if stamp.tzinfo is None or not timedelta(0) <= now - stamp <= self.ttl:
                    continue
                if not _complete_rows(result, words, days):
                    continue
                hits.append((stamp, folder, result))
            except (OSError, ValueError, TypeError, KeyError, OverflowError):
                # Corrupt or older files are never used to manufacture a hit.
                continue
        if not hits:
            return None
        _, folder, result = max(hits, key=lambda item: item[0])
        return {"result": result, "folder": str(folder)}

    def save(self, result: dict[str, Any]) -> str:
        snapshot = _json_copy(result)
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("metadata"), dict) or not isinstance(snapshot.get("rows"), list):
            raise ValueError("结果必须包含 metadata 和 rows。")
        metadata = snapshot["metadata"]
        words, days = _query(metadata.get("keywords"), metadata.get("start_date"), metadata.get("end_date"))
        for key, value in _SCOPE.items():
            if key in metadata and metadata[key] != value:
                raise ValueError("结果数据口径与本工具不一致。")
            metadata[key] = value
        stamp = datetime.fromisoformat(metadata["fetched_at"])
        if stamp.tzinfo is None:
            raise ValueError("采集时间必须包含时区。")
        if not isinstance(metadata.get("complete"), bool):
            raise ValueError("结果必须明确完整性状态。")
        if metadata["complete"] and not _complete_rows(snapshot, words, days):
            raise ValueError("完整结果标记与实际日序列不一致。")
        csv_buffer = io.StringIO(newline="")
        writer = csv.DictWriter(csv_buffer, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for row in snapshot["rows"]:
            if not isinstance(row, dict) or not isinstance(row.get("keyword"), str):
                raise ValueError("结果包含无效记录。")
            export = {key: row.get(key) for key in _CSV_FIELDS}
            export["keyword"] = _csv_keyword(export["keyword"])
            writer.writerow(export)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = datetime.now(_TZ).strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid.uuid4().hex[:8]
        folder = self.root / name
        folder.mkdir(mode=0o700, exist_ok=False)
        # result.json is committed last so readers never see a partial CSV export.
        _atomic_write(folder / "search-index.csv", csv_buffer.getvalue().encode("utf-8-sig"))
        _atomic_write(folder / "result.json", _json_bytes(snapshot))
        return str(folder)

    def save_comparison(self, folder: str | Path, report: dict[str, Any]) -> None:
        supplied = Path(folder).expanduser()
        if supplied.is_symlink():
            raise ValueError("核对结果目录不能是符号链接。")
        resolved = supplied.resolve()
        if resolved.parent != self.root or not resolved.is_dir() or resolved.name.startswith("."):
            raise ValueError("只能更新当前结果根目录中的直属批次。")
        result = _read_json(resolved / "result.json")
        metadata = result.get("metadata") if isinstance(result, dict) else None
        snapshot = _json_copy(report)
        if not isinstance(metadata, dict) or not isinstance(snapshot, dict) or type(snapshot.get("verified")) is not bool:
            raise ValueError("核对报告必须明确 verified 状态。")
        words, days = _query(metadata.get("keywords"), metadata.get("start_date"), metadata.get("end_date"))
        if snapshot["verified"]:
            if not _complete_rows(result, words, days):
                raise ValueError("数据尚不完整，不能登记核对通过。")
            checks = snapshot.get("rows")
            if not isinstance(checks, list) or len(checks) != len(words) * len(days):
                raise ValueError("核对报告条数不符，不能登记通过。")
            for check, row in zip(checks, result["rows"]):
                if not isinstance(check, dict) or check.get("status") != "match" or check.get("keyword") != row["keyword"] or check.get("date") != row["date"] or check.get("api_search_index") != row["search_index"]:
                    raise ValueError("核对报告与当前批次不对应。")
        metadata["page_comparison"] = "verified" if snapshot["verified"] else "incomplete"
        _atomic_write(resolved / "comparison.json", _json_bytes(snapshot))
        _atomic_write(resolved / "result.json", _json_bytes(result))
