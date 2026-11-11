import argparse
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import sys

from .errors import Stop
from .runtime import DailyCounter, Profile, browser_defaults, mem_available


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # Invalid CLI arguments may contain secrets. Keep the error fixed.
        raise Stop(1, "invalid_arguments")


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def parser():
    common = argparse.ArgumentParser(add_help=False)
    for name, help_text in (("profile-dir", "Dedicated private browser profile"),
                            ("state-dir", "Private request counter and login QR directory"),
                            ("display", "Explicit X display; otherwise start an owned Xvfb")):
        common.add_argument("--" + name, default=argparse.SUPPRESS, help=help_text)
    root = Parser(prog="dyc", description="Private comment collector; site adapter uncalibrated.", parents=[common])
    subs = root.add_subparsers(dest="command", required=True, parser_class=Parser)
    fetch = subs.add_parser("fetch", parents=[common], help="Fetch a serial batch")
    fetch.add_argument("urls", metavar="URL", nargs="*")
    fetch.add_argument("--urls-file", metavar="FILE")
    fetch.add_argument("--out", metavar="DIR")
    fetch.add_argument("--top", type=positive, default=30)
    fetch.add_argument("--max-pages", type=positive, default=25, help="Per-video request budget (at most 25)")
    login = subs.add_parser("login", parents=[common], help="Wait for human login; refreshed private QR path on stderr")
    login.add_argument("--wait-seconds", type=positive, default=180, help="Login wait, at most 600 seconds")
    mobile = subs.add_parser("login-web", parents=[common], help="Resident private phone SMS/QR login (loopback only)")
    mobile.add_argument("--origin", required=True, help="Exact HTTPS origin, e.g. https://host.trycloudflare.com")
    mobile.add_argument("--port", type=positive, default=8766, help="Loopback port (default 8766)")
    mobile.add_argument("--no-token", action="store_true", help="Open phone login directly; supports background operation")
    mobile.add_argument("--token-ttl", type=positive, default=1800, help="Token validity in seconds (default 1800)")
    mobile.add_argument("--startup-limits", action="store_true", help="Enable memory preflight and disable core dumps")
    subs.add_parser("check-env", parents=[common], help="Offline environment report")
    smoke = subs.add_parser("selftest", parents=[common], help="Entire pipeline against an in-memory fake origin")
    smoke.add_argument("--out", metavar="DIR")
    smoke.add_argument("--top", type=positive, default=30)
    return root


def defaults(args):
    if not hasattr(args, "profile_dir"):
        args.profile_dir = os.environ.get("DYC_PROFILE_DIR", "~/.local/share/dyc/profile")
    if not hasattr(args, "state_dir"):
        args.state_dir = os.environ.get("DYC_STATE_DIR", "~/.local/state/dyc")
    args.profile_dir = Path(args.profile_dir).expanduser().absolute()
    args.state_dir = Path(args.state_dir).expanduser().absolute()
    if not hasattr(args, "display"):
        args.display = None
    return args


def check_env(args):
    browser_defaults()
    browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache/ms-playwright")))
    result = dict(python=platform.python_version(), playwright=None, browsers_path=str(browsers),
                  xvfb=shutil.which("Xvfb"), mem_available=None,
                  profile_dir=str(args.profile_dir), profile_state="unknown", today_requests=None)
    try:
        result["playwright"] = importlib.metadata.version("playwright")
        result["mem_available"] = mem_available()
        if result["mem_available"] < 2 * 1024**3:
            raise Stop(5, "low_memory")
        present = args.profile_dir.exists()
        with Profile(args.profile_dir, clear_stale=False):
            result["profile_state"] = "ready" if present else "created_private"
        result["today_requests"] = DailyCounter(args.state_dir).usage()
        if result["today_requests"] >= 150:
            raise Stop(5, "daily_cap_at_start")
        if not result["xvfb"] and not args.display:
            raise Stop(5, "xvfb_missing")
        # Ask the installed package which headed Chromium it will launch. A
        # different cached revision (or headless shell alone) is insufficient.
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise Stop(5, "chromium_missing")
        code, reason = 0, "ready"
    except Stop as error:
        code, reason = 5, error.reason
    except Exception:
        code, reason = 5, "environment_unavailable"
    return code, dict(result, exit_code=code, exit_reason=reason)


def main(argv=None):
    os.umask(0o077)
    # Never let inherited Playwright debug flags dump signed URLs to a terminal.
    os.environ.pop("DEBUG", None)
    os.environ.pop("PWDEBUG", None)
    os.environ.pop("DEBUGP", None)  # Even "0" enables raw protocol logging.
    args = None
    identity = None
    try:
        args = defaults(parser().parse_args(argv))
        if args.command == "check-env":
            code, result = check_env(args)
        elif args.command == "login-web":
            from .sms_web import serve
            code, result = asyncio.run(serve(args))
        else:
            from . import runner
            if args.command == "fetch":
                identity = runner.run_id()
                code, result = asyncio.run(runner.fetch(args, identity=identity))
            else:
                code, result = asyncio.run(getattr(runner, args.command)(args))
    except Stop as error:
        code, result = error.code, dict(exit_code=error.code, exit_reason=error.reason)
    except KeyboardInterrupt:
        code, result = 2, dict(exit_code=2, exit_reason="interrupted")
    except Exception:
        code, result = 1, dict(exit_code=1, exit_reason="internal_error")
    if args is not None and args.command == "fetch" and "run_id" not in result:
        result.update(run_id=identity,
                      out=str(Path(args.out).expanduser().absolute() if args.out
                              else Path.home() / "dyc-output" / identity),
                      videos=[], requests=0, site_calibrated=False)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    return code
