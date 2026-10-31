"""ALL site-specific strings and schema interpretation. UNCALIBRATED.

Only synthetic fixtures have validated these assumptions. Edit this module when
an explicitly authorized real-page calibration establishes different signals.
"""

from dataclasses import dataclass
import json
import re
from urllib.parse import parse_qs, urlsplit

from .errors import Stop

CALIBRATED = False
LOGIN_URL = "https://www.douyin.com/"
VIDEO_URL = "https://www.douyin.com/video/{video_id}"
FAKE_ORIGIN = "https://dyc.invalid"
HOSTS = {"douyin.com", "www.douyin.com"}
SHORT_HOST = "v.douyin.com"
VIDEO_PATH = r"/video/([0-9]+)/*"
MODAL_PARAM = "modal_id"
TOP_PATH = "/aweme/v1/web/comment/list/"
REPLY_PATH = "/aweme/v1/web/comment/list/reply/"
PARAM = dict(video="aweme_id", item="item_id", root="comment_id",
             cursor="cursor", count="count", token="msToken")
SIGNATURES = ("a_bogus", "X-Bogus", "_signature")
TOKEN_STORAGE = "xmst"
REPLAY_HEADERS = ("accept", "content-type", "x-requested-with")
SIGNALS = {
    "logged_in": ('[data-e2e="user-avatar"]', '[data-dyc-login="yes"]'),
    "logged_out": ('[data-e2e="login-button"]', '[data-dyc-login="no"]'),
    "captcha": ('[id*="captcha"]', '[class*="captcha-verify"]',
                '[data-e2e="verify-dialog"]', 'iframe[src*="captcha"]',
                'iframe[src*="verify"]', '[data-dyc-risk="yes"]'),
    "qr": '[data-e2e="login-qr-code"]',
    "title": '[data-e2e="video-desc"]',
    "count": '[data-e2e="comment-count"]',
}
FIELDS = dict(status="status_code", comments="comments", cursor="cursor",
              more="has_more", total="total", id="cid", text="text",
              created="create_time", likes="digg_count",
              reply_count="reply_comment_total", inline="reply_comment",
              user="user", uid="uid", nickname="nickname", ip="ip_label",
              reply_id="reply_id", reply_to_reply="reply_to_reply_id",
              reply_user="reply_to_user", images="image_list", sticker="sticker")
RISK_FIELDS = ("verify", "captcha", "verify_info", "verify_ticket", "verify_data")
LOGIN_CODES = (8, 1001, 1002)
LOGIN_FIELDS = ("login_required", "login_expired")


@dataclass(frozen=True)
class Target:
    url: str
    video_id: str | None


def parse_url(value, *, fake=False):
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
            raise ValueError
        if parsed.port not in (None, 443):
            raise ValueError
        hosts = HOSTS | ({urlsplit(FAKE_ORIGIN).hostname} if fake else set())
        if parsed.hostname == SHORT_HOST:
            if not re.fullmatch(r"/[A-Za-z0-9_-]+/?", parsed.path):
                raise ValueError
            return Target(f"https://{SHORT_HOST}{parsed.path}", None)
        if parsed.hostname not in hosts:
            raise ValueError
        match = re.fullmatch(VIDEO_PATH, parsed.path)
        values = parse_qs(parsed.query).get(MODAL_PARAM, [])
        if len(values) > 1 or (match and values and values[0] != match[1]):
            raise ValueError
        vid = match[1] if match else (values[0] if len(values) == 1 else None)
        if not vid or not re.fullmatch(r"[0-9]{1,40}", vid):
            raise ValueError
        base = FAKE_ORIGIN + "/video/{video_id}" if fake else VIDEO_URL
        return Target(base.format(video_id=vid), vid)
    except (ValueError, TypeError):
        raise Stop(1, "invalid_video_url") from None


def request_kind(url):
    path = urlsplit(url).path
    if REPLY_PATH in path:
        return "reply"
    if TOP_PATH in path:
        return "top"
    return None


def integer(value, *, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Stop(3, "suspicious_schema")
    return value


def identifier(value, *, optional=False):
    if value is None and optional:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise Stop(3, "suspicious_id")
    result = str(value)
    if not re.fullmatch(r"[0-9]{1,80}", result):
        raise Stop(3, "suspicious_id")
    return result


def plain(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise Stop(3, "suspicious_schema")
    return value


def _unique(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate")
        obj[key] = value
    return obj


def _signals(obj):
    if isinstance(obj, dict):
        if any(obj.get(key) for key in LOGIN_FIELDS):
            raise Stop(4, "login_expired")
        if any(obj.get(key) not in (None, False, "", 0, [], {}) for key in RISK_FIELDS):
            raise Stop(3, "verification_response")
        for value in obj.values():
            _signals(value)
    elif isinstance(obj, list):
        for value in obj:
            _signals(value)


def decode_page(result):
    if result.get("kind") in ("timeout", "network"):
        raise Stop(2, result["kind"])
    if result.get("kind") == "login":
        raise Stop(4, "login_expired")
    if result.get("kind") == "risk":
        raise Stop(3, "verification_widget")
    if result.get("status") in (401, 403):
        raise Stop(4 if result["status"] == 401 else 3, "access_denied")
    if result.get("status") != 200:
        raise Stop(3, "suspicious_http_status")
    try:
        body = result.get("text")
        if not isinstance(body, str) or not body.strip():
            raise Stop(3, "empty_body")
        data = json.loads(body, object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError):
        raise Stop(3, "invalid_json") from None
    if not isinstance(data, dict):
        raise Stop(3, "suspicious_schema")
    _signals(data)
    status = data.get(FIELDS["status"])
    if status in LOGIN_CODES:
        raise Stop(4, "login_expired")
    if type(status) is not int or status != 0:
        raise Stop(3, "nonzero_status")
    comments = data.get(FIELDS["comments"])
    if comments is None and data.get(FIELDS["total"]) == 0:
        comments = []
    if not isinstance(comments, list) or any(not isinstance(c, dict) for c in comments):
        raise Stop(3, "suspicious_schema")
    more = data.get(FIELDS["more"])
    if type(more) not in (bool, int) or more not in (0, 1):
        raise Stop(3, "suspicious_schema")
    cursor = data.get(FIELDS["cursor"])
    if not isinstance(cursor, (int, str)) or isinstance(cursor, bool) or not str(cursor).isdigit():
        raise Stop(3, "suspicious_cursor")
    total = integer(data.get(FIELDS["total"]), optional=True)
    return comments, str(cursor), bool(more), total
