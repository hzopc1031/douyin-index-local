"""Human login on a separate computer; a single credential transfer over SSH.

Run only on the login computer, never on EC2. No browser profile, cookie file,
storage_state, HAR, trace or screenshot is exported. The target is an existing,
verified OpenSSH host alias. The SSH connection lasts for this one-shot probe.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
from urllib.parse import urlsplit

from client import PAGE_URL, TREND_URL, SHANGHAI

REMOTE_DIR = "/srv/work-ecs-projects/douyin-index-local"
MAX_OUTPUT = 128 * 1024


class TransferError(RuntimeError):
    """Messages are fixed and must never include credentials or subprocess output."""


def ssh_command(host: str, remote_dir: str, mode: str = "query", run_as: str | None = None) -> list[str]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,200}", host):
        raise TransferError("SSH 目标须为已配置的主机别名或 user@host。")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", remote_dir) or ".." in remote_dir.split("/"):
        raise TransferError("远端目录须为不含空格的绝对路径。")
    if mode not in ("query", "self-check", "validate-only"):
        raise TransferError("不支持的操作。")
    if run_as is not None and not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", run_as):
        raise TransferError("远端运行账号格式不正确。")
    command = " ".join(shlex.quote(part) for part in
                       [remote_dir + "/.venv/bin/python", "-B", remote_dir + "/cloud_probe.py"])
    if mode != "query":
        command += " --" + mode
    if run_as:
        command = "sudo -n -u " + shlex.quote(run_as) + " -- " + command
    return ["ssh", "-T", "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
            "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=10",
            "-o", "ConnectionAttempts=1", "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "LogLevel=ERROR", host, command]


def send(host: str, remote_dir: str, payload: bytes = b"", *, mode: str = "query", run_as: str | None = None) -> dict:
    """Secrets travel only in a pipe; never in argv, environment or a temp file."""
    try:
        completed = subprocess.run(ssh_command(host, remote_dir, mode, run_as), input=payload,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=75, check=False)
        if len(completed.stdout) > MAX_OUTPUT:
            raise TransferError("远端输出异常，已停止；不要自动重试。")
        reply = json.loads(completed.stdout)
        if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
            raise ValueError
        if mode != "query" and (completed.returncode != 0 or reply["ok"] is not True):
            raise ValueError
        if mode == "query" and completed.returncode != (0 if reply["ok"] else 1):
            raise ValueError
        return reply
    except TransferError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise TransferError("SSH 或远端执行失败。请检查主机密钥、密钥登录及部署路径；本工具没有重试。查询可能已发出，先核查再决定下一次操作。") from None


def make_payload(browser_cookies: list[dict], user_agent: str, keyword: str, end_date: str) -> bytes:
    # cookies([TREND_URL]) supplies only cookies applicable to that exact URL.
    # Deliberately narrow their authority to creator.douyin.com; do not infer
    # host-only semantics from ambiguous exported domain fields.
    cookies = []
    seen = set()
    for item in browser_cookies:
        if item.get("domain", "").lstrip(".").lower() not in ("douyin.com", "creator.douyin.com"):
            raise TransferError("浏览器返回了预期域之外的 Cookie，已停止。")
        if item.get("partitionKey"):
            raise TransferError("当前验证工具不支持分区 Cookie，已停止。")
        expires = item.get("expires", -1)
        if type(expires) not in (int, float) or not math.isfinite(expires):
            raise TransferError("浏览器会话格式不受支持。")
        if expires != -1 and expires <= time.time():
            continue
        pair = (item.get("name"), item.get("path"))
        if pair in seen:
            raise TransferError("会话包含同名同路径 Cookie，无法安全缩小域范围，已停止。")
        seen.add(pair)
        # Re-scope non-Secure browser cookies to HTTPS-only as well.
        cookies.append({"name": item.get("name"), "value": item.get("value"),
                        "domain": "creator.douyin.com", "host_only": True,
                        "path": item.get("path"), "secure": True,
                        "expires": None if expires == -1 else int(expires)})
    payload = {"version": 1, "user_agent": user_agent, "cookies": cookies,
               "keyword": keyword, "end_date": end_date}
    # Validate locally before sending any credential. This uses no network.
    from cloud_probe import validate_payload
    validate_payload(payload)
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > 128 * 1024:
        raise TransferError("会话超过验证工具的大小限制。")
    return encoded


def validate_query(keyword: str, end_date: str) -> None:
    if not keyword or keyword != keyword.strip() or len(keyword) > 100 or any(ord(c) < 32 or ord(c) == 127 for c in keyword):
        raise TransferError("请输入一个 1—100 字的关键词。")
    try:
        end = dt.date.fromisoformat(end_date)
        if end.isoformat() != end_date or end >= dt.datetime.now(SHANGHAI).date():
            raise ValueError
        end - dt.timedelta(days=6)
    except (ValueError, OverflowError):
        raise TransferError("截止日必须为过去日期，格式 YYYY-MM-DD；请以官方页面为准。") from None


def display_reply(reply: dict, *, keyword: str | None = None, end_date: str | None = None) -> None:
    # Do not echo arbitrary remote output. No raw business response is shown.
    if not reply.get("ok"):
        allowed = {"authentication_required", "verification_required", "rate_limited",
                   "access_denied", "network_error", "response_schema", "response_format",
                   "business_error", "http_error"}
        code = reply.get("code")
        print("EC2 请求停止：" + (code if code in allowed else "probe_failed") + "。没有重试。")
        return
    rows = reply.get("rows", [])
    if not isinstance(rows, list) or len(rows) > 7:
        raise TransferError("远端结果格式不符合单词七天约束。")
    seen = set()
    expected = None
    if end_date:
        end = dt.date.fromisoformat(end_date)
        expected = {(end - dt.timedelta(days=i)).isoformat() for i in range(7)}
    for row in rows:
        if not isinstance(row, dict) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", str(row.get("date", ""))):
            raise TransferError("远端结果日期格式错误。")
        try:
            dt.date.fromisoformat(row["date"])
        except ValueError:
            raise TransferError("远端结果日期格式错误。") from None
        if (row["date"] in seen or (expected is not None and row["date"] not in expected)
                or (keyword is not None and row.get("keyword") != keyword)):
            raise TransferError("远端结果日期或关键词与请求不符。")
        seen.add(row["date"])
        value = row.get("search_index")
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise TransferError("远端结果数值格式错误。")
    complete = len(rows) == 7 and all(row.get("search_index") is not None for row in rows)
    print("EC2 已返回" + ("完整七天数据" if complete else "不完整数据（未补零）") + "。页面核对仍待完成；搜索指数不是实际搜索次数。")
    for row in rows:
        value = row.get("search_index")
        print(row["date"], "缺失" if value is None else value)
    print("完成后会话已在 EC2 清除。本次不是长期会话或断网运行测试。")


def main() -> int:
    parser = argparse.ArgumentParser(description="在此电脑扫码；通过已验证的 SSH 将一次查询交给 EC2。")
    parser.add_argument("--ssh-host", required=True, help="已有 SSH 主机别名或 user@host")
    parser.add_argument("--remote-dir", default=REMOTE_DIR)
    parser.add_argument("--run-as", help="可选：用已有免密 sudo 切换到项目账号；不修改服务器权限")
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--end-date", required=True, help="官方页面显示的可用截止日 YYYY-MM-DD")
    parser.add_argument("--channel", choices=("chrome", "msedge", "chromium"), default="chrome")
    args = parser.parse_args()
    try:
        # Playwright protocol debugging can log cookies; reject before launch.
        if os.environ.get("DEBUG") or os.environ.get("PWDEBUG"):
            raise TransferError("请先关闭 DEBUG / PWDEBUG 环境变量，避免记录浏览器会话。")
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except ImportError:  # Windows does not expose resource.
            pass
        validate_query(args.keyword, args.end_date)
        send(args.ssh_host, args.remote_dir, mode="self-check", run_as=args.run_as)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            launch = {"headless": False}
            if args.channel != "chromium":
                launch["channel"] = args.channel
            browser = p.chromium.launch(**launch)
            try:
                context = browser.new_context(accept_downloads=False)
                page = context.new_page()
                page.goto(PAGE_URL, wait_until="domcontentloaded")
                print("请在新窗口扫码登录，进入抖音指数页，确认关键词和截止日期；先独立记录七天页面提示值。")
                print("将向 SSH 主机 " + args.ssh_host + " 直接发送本次会话，并在 EC2 查询一次。")
                if input("确认后输入 SEND 并回车；其他输入取消：").strip() != "SEND":
                    print("已取消，没有向 EC2 发送会话。")
                    return 0
                current = urlsplit(page.url)
                if (current.scheme != "https" or current.hostname != "creator.douyin.com"
                        or current.username or current.password or current.port not in (None, 443)):
                    raise TransferError("请保持官方创作者中心 HTTPS 页面后再操作。")
                payload = make_payload(context.cookies([TREND_URL]), page.evaluate("navigator.userAgent"),
                                       args.keyword, args.end_date)
                # Closing this disposable browser context is not an account logout.
                context.close()
                browser.close()
                try:
                    reply = send(args.ssh_host, args.remote_dir, payload, run_as=args.run_as)
                finally:
                    payload = b""
                display_reply(reply, keyword=args.keyword, end_date=args.end_date)
                return 0 if reply["ok"] else 1
            finally:
                browser.close()
    except TransferError as exc:
        print(str(exc), file=sys.stderr)
    except (Exception, KeyboardInterrupt):
        print("操作中止；未显示原始错误或会话。请检查依赖、浏览器及连接；不要自动重试查询。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
