"""Local cookie heuristics. Never log, persist or expose cookie material/digests."""
import hashlib
import json
import math
import threading
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote

DOMAINS = frozenset(('douyin.com', 'creator.douyin.com'))
KEY_COOKIES = frozenset(('sessionid', 'sessionid_ss', 'sid_tt', 'sid_guard'))
LOGIN_STATES = frozenset(('qr_session', 'cookie_present', 'expired', 'rejected',
                          'no_session_cookie', 'unknown'))


def _expiry(value):
    if value in ('', None):
        return None
    try:
        stamp = float(value)
        return stamp if math.isfinite(stamp) else None
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            return date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
        except (ValueError, TypeError, OverflowError):
            return None


def sid_guard_expiry(value):
    """Ignore malformed/implausible issue-time|max-age rather than guessing."""
    try:
        parts = unquote(value).split('|')
        if len(parts) < 3 or not parts[0]:
            return None
        if not all(p.isascii() and p.isdecimal() and len(p) <= 12 for p in parts[1:3]):
            return None
        issued, age = map(int, parts[1:3])
        if issued <= 0:
            return None
        return issued + age
    except (ValueError, TypeError, AttributeError):
        return None


def _cookie_snapshot(cookies, *, now=None, accepted_domains=DOMAINS):
    """Consume pywebview SimpleCookies; return only a local state and digest."""
    now = time.time() if now is None else now
    pairs, expired = [], False
    for cookie in cookies:
        for item in cookie.values():
            if item.key not in KEY_COOKIES or item['domain'].lstrip('.').lower() not in accepted_domains:
                continue
            if not item.value:
                continue
            pairs.append((item.key, item.value))
            expiry = _expiry(item['expires'])
            guard_expiry = sid_guard_expiry(item.value) if item.key == 'sid_guard' else None
            # A positive Max-Age alone has no recoverable issue time here.
            age = _expiry(item['max-age'])
            expired |= ((expiry is not None and expiry <= now) or
                        (guard_expiry is not None and guard_expiry <= now) or
                        (age is not None and age <= 0))
    if not pairs:
        return 'no_session_cookie', None
    # JSON framing avoids ambiguities if a cookie value contains separators.
    digest = hashlib.sha256(json.dumps(sorted(set(pairs)), separators=(',', ':')).encode()).digest()
    return ('expired' if expired else 'cookie_present'), digest


class BrowserLoginState:
    def __init__(self):
        self._rejected_fingerprint = None
        self._lock = threading.Lock()

    def reject(self, fingerprint):
        with self._lock:
            self._rejected_fingerprint = fingerprint

    def read(self, page, *, qr_session=False, now=None, accepted_domains=DOMAINS):
        # accepted_domains is an explicit test seam for loopback fixtures only;
        # production callers always use the fixed Douyin domain allowlist.
        if qr_session:
            return 'qr_session'
        try:
            state, fingerprint = _cookie_snapshot(page.get_cookies(), now=now,
                                                  accepted_domains=accepted_domains)
        except Exception:
            return 'unknown'
        with self._lock:
            if fingerprint != self._rejected_fingerprint:
                self._rejected_fingerprint = None
            if fingerprint is not None and fingerprint == self._rejected_fingerprint:
                return 'rejected'
        return state
