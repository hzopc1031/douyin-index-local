"""Single-account local verification UI. Credentials remain in memory only."""
from __future__ import annotations

import logging
import json
import os
import re
import threading
import sys
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "results"
PAGE = "https://creator.douyin.com/creator-micro/creator-count/arithmetic-index"
TZ = ZoneInfo("Asia/Shanghai")


class LoginRequired(ValueError):
    code = "authentication_required"


def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)


def compare_result(result, entries, *, compared_at=None):
    """Recompute comparisons from displayed values; never trust saved status flags."""
    if not isinstance(entries, list):
        raise ValueError("页面记录必须为列表。")
    rows = result["rows"]
    keys = {(row["keyword"], row["date"]) for row in rows}
    if len(keys) != len(rows):
        raise ValueError("当前批次存在重复日期记录，无法核对。")
    displays = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("页面记录必须包含关键词、日期和显示值。")
        word, day, displayed = entry.get("keyword"), entry.get("date"), entry.get("page_display", "")
        if not isinstance(word, str) or not isinstance(day, str) or not isinstance(displayed, str):
            raise ValueError("页面记录的关键词、日期和显示值必须是文字。")
        key = (word, day)
        if key not in keys:
            raise ValueError("页面记录包含当前批次之外的关键词或日期。")
        if key in displays:
            raise ValueError("页面记录重复，不能登记核对通过。")
        if len(displayed) > 100:
            raise ValueError("页面显示值过长，请只填写图表中的数值。")
        displays[key] = displayed.strip()
    checks = []
    exact_number = r"(?:[0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)(?:\.[0-9]+)?"
    for row in rows:
        displayed = displays.get((row["keyword"], row["date"]), "")
        status = "pending"
        if displayed:
            if re.fullmatch(exact_number, displayed):
                try:
                    api_value = row["search_index"]
                    status = "match" if api_value is not None and not isinstance(api_value, bool) and Decimal(displayed.replace(",", "")) == Decimal(str(api_value)) else "mismatch"
                except InvalidOperation:
                    status = "unreadable"
            else:
                status = "insufficient_precision" if any(unit in displayed for unit in ("万", "亿", "k", "K", "m", "M")) else "unreadable"
        checks.append({"keyword": row["keyword"], "date": row["date"],
                       "api_search_index": row["search_index"], "page_display": displayed, "status": status})
    metadata = result["metadata"]
    words = metadata["keywords"]
    start = datetime.strptime(metadata["start_date"], "%Y-%m-%d").date()
    day_count = (datetime.strptime(metadata["end_date"], "%Y-%m-%d").date()-start).days+1
    if not 1 <= day_count <= 731: raise ValueError("日期范围无效。")
    expected_keys = {(word, (start + timedelta(days=i)).isoformat()) for word in words for i in range(day_count)}
    expected_rows = len(words) * day_count
    matched_rows = sum(item["status"] == "match" for item in checks)
    verified = (metadata.get("complete") is True and len(checks) == expected_rows
                and keys == expected_keys and matched_rows == expected_rows)
    return {"verified": bool(verified), "expected_rows": expected_rows, "matched_rows": matched_rows,
            "compared_at": compared_at or datetime.now(TZ).isoformat(),
            "method": "Values transcribed from official page tooltips; not copied from API output",
            "rows": checks}


class Api:
    def __init__(self):
        from storage import ResultStore
        self._qr_lock = threading.Lock()
        self._page = None
        self._lock = threading.Lock()
        self._result = None
        self._folder = None
        self._attempted = False
        self._store = ResultStore(OUTPUT)

    def show_official_page(self):
        self._page.show()
        self._page.restore()
        return {"ok": True}

    def _qr_manager(self):
        from qr_login import login_mode, QrLoginManager
        if login_mode() != 'qr':
            return None
        if not hasattr(self, '_qr'):
            with self._qr_lock:
                if not hasattr(self, '_qr'):
                    self._qr = QrLoginManager()
        return self._qr

    def _on_auth_failed(self):
        from qr_login import login_mode
        if login_mode() == 'qr' and hasattr(self, '_qr'):
            self._qr.holder.clear()

    def _client(self):
        from client import DouyinIndexClient
        qr = self._qr_manager()
        if qr is not None and qr.holder.has_session():
            session, ua = qr.holder.take_session()
            try:
                return DouyinIndexClient(session=session, user_agent=ua)
            except Exception:
                session.cookies.clear()
                session.close()
                raise
        if self._page is None:
            raise LoginRequired("请先打开官方登录窗口。")
        current = urlsplit(self._page.get_current_url() or "")
        if current.scheme != "https" or current.hostname != "creator.douyin.com" or current.username or current.password or current.port not in (None, 443):
            raise LoginRequired("请先在 HTTPS 官方窗口完成登录，并进入抖音指数页。")
        visible_login = self._page.evaluate_js("Boolean(document.querySelector('input[type=tel]')) && document.body.innerText.includes('扫码登录')")
        if visible_login:
            raise LoginRequired("官方窗口仍显示登录表单，请先扫码登录。")
        session = requests.Session()
        session.trust_env = False
        try:
            for cookie in self._page.get_cookies():
                for item in cookie.values():
                    domain = item["domain"].lstrip(".").lower()
                    if domain not in ("douyin.com", "creator.douyin.com"):
                        continue
                    session.cookies.set(item.key, item.value, domain=item["domain"],
                                        path=item["path"] or "/", secure=bool(item["secure"]))
            if not session.cookies:
                raise LoginRequired("尚未取得当前窗口的登录会话，请完成扫码登录。")
            ua = self._page.evaluate_js("navigator.userAgent")
            if not isinstance(ua, str) or not ua:
                raise ValueError("暂时无法识别官方窗口，请等待页面加载完成。")
            return DouyinIndexClient(session=session, user_agent=ua)
        except Exception:
            session.cookies.clear()
            session.close()
            raise

    def _cached_comparison(self, result, folder):
        comparison = None
        result["metadata"]["page_comparison"] = "pending"
        path = Path(folder) / "comparison.json"
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
                return None
            saved = json.loads(path.read_text(encoding="utf-8"))
            comparison = compare_result(result, saved["rows"], compared_at=saved.get("compared_at"))
            comparison["revalidated_at"] = datetime.now(TZ).isoformat()
            result["metadata"]["page_comparison"] = "verified" if comparison["verified"] else "incomplete"
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return comparison

    def _response(self, result, folder, *, cache_hit, comparison=None):
        self._result, self._folder = result, Path(folder)
        return {"ok": True, "rows": result["rows"], "metadata": result["metadata"],
                "folder": str(folder), "cache_hit": cache_hit, "comparison": comparison}

    def fetch_batch(self, keywords_text, end_date, cloud=False, start_date=None):
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "code": "busy", "message": "取数或核对正在进行，请等待。"}
        try:
            if not isinstance(keywords_text, str):
                raise ValueError("请每行输入一个关键词。")
            words = [line.strip() for line in keywords_text.splitlines() if line.strip()]
            if not 1 <= len(words) <= 5 or any(len(word) > 100 for word in words):
                raise ValueError("请输入 1—5 个关键词，每词不超过 100 字。")
            if len(set(words)) != len(words):
                raise ValueError("关键词不能重复，请每行填写不同的词。")
            if not isinstance(end_date, str):
                raise ValueError("截止日请使用 YYYY-MM-DD。")
            try:
                end = datetime.strptime(end_date, "%Y-%m-%d").date()
            except ValueError:
                raise ValueError("截止日请使用 YYYY-MM-DD。") from None
            if end.isoformat() != end_date:
                raise ValueError("截止日请使用 YYYY-MM-DD。")
            if end >= datetime.now(TZ).date():
                raise ValueError("截止日必须是过去日期，请以官方页面的可用日期为准。")
            start = datetime.strptime(start_date, "%Y-%m-%d").date() if cloud and start_date is not None else end - timedelta(days=6)
            if not 0 <= (end-start).days < 731 or (cloud and start_date is not None and start.isoformat()!=start_date):
                raise ValueError("开始日期至结束日期须为连续 1—731 天。")
            cached = self._store.lookup(words, start.isoformat(), end_date)
            if cached is not None:
                result, folder = cached["result"], cached["folder"]
                comparison = self._cached_comparison(result, folder)
                return self._response(result, folder, cache_hit=True, comparison=comparison)
            if self._attempted and not cloud:
                return {"ok": False, "code": "session_request_used", "message": "本次会话已发出过一次线上请求。可以查看已有缓存；新的线上批次请重新启动工具。"}
            client = self._client()
            self._attempted = True
            try:
                result = client.query_many(words, start.isoformat(), end_date)
            finally:
                client.session.cookies.clear()
                client.session.close()
            result["metadata"].update({"source": "creator.douyin.com", "metric": "search_index",
                                        "region": "全国", "platform": "aweme", "official_page": PAGE,
                                        "page_comparison": "pending"})
            folder = self._store.save(result)
            return self._response(result, folder, cache_hit=False)
        except Exception as exc:
            from client import SanitizedClientError
            if isinstance(exc, SanitizedClientError):
                safe = {"ok": False, **exc.as_dict()}
            elif isinstance(exc, LoginRequired):
                safe = {"ok": False, "code": "authentication_required", "message": str(exc)}
            elif isinstance(exc, ValueError):
                safe = {"ok": False, "message": str(exc)}
            else:
                safe = {"ok": False, "message": "本地执行失败；未输出会话或原始错误。", "code": type(exc).__name__}
            if safe.get('code') == 'authentication_required':
                self._on_auth_failed()
            try:
                OUTPUT.mkdir(exist_ok=True, mode=0o700)
                write_json(OUTPUT / "last-error.json", safe)
            except OSError:
                pass
            return safe
        finally:
            self._lock.release()

    def compare(self, entries):
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "message": "取数或核对正在进行，请等待。"}
        try:
            if self._result is None or self._folder is None:
                return {"ok": False, "message": "请先获取或加载一组数据。"}
            report = compare_result(self._result, entries)
            self._store.save_comparison(self._folder, report)
            self._result["metadata"]["page_comparison"] = "verified" if report["verified"] else "incomplete"
            return {"ok": True, **report}
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:
            return {"ok": False, "code": type(exc).__name__, "message": "核对记录未能保存，请检查本地文件状态。"}
        finally:
            self._lock.release()


def main():
    import webview

    os.umask(0o077)
    api = Api()
    control = webview.create_window("抖音搜索指数 · 小批量取数与核对", html=(ROOT / "ui-batch.html").read_text(),
                                    js_api=api, width=1040, height=850, x=20, y=50, text_select=True)
    api._page = webview.create_window("抖音创作者中心 · 官方登录与核对", PAGE,
                                      width=1250, height=850, x=160, y=50, text_select=True)
    webview.settings["ALLOW_FILE_URLS"] = False
    webview.settings["IGNORE_SSL_ERRORS"] = False
    webview.settings["REMOTE_DEBUGGING_PORT"] = None
    # Explicit cloud deployment opts in; ordinary desktop launches are unchanged.
    if os.environ.get("INDEX_HUB_ENABLED") == "1":
        from hub_logging import configure_logging
        configure_logging('app', ROOT)
        from hub_bridge import start_bridge
        start_bridge(api)
    webview.start(private_mode=True, debug=False, http_server=False)


if __name__ == "__main__":
    if "--connection-check" in sys.argv:
        # Public, credential-free diagnostic, never accesses a logged-in webview.
        s = requests.Session()
        s.trust_env = False
        try:
            r = s.get("https://creator.douyin.com/", timeout=12, allow_redirects=False)
            print(json.dumps({"http_status": r.status_code}), flush=True)
        except requests.RequestException as exc:
            print(json.dumps({"exception": type(exc).__name__, "detail": str(exc)[:500]}), flush=True)
        finally:
            s.close()
    else:
        main()
