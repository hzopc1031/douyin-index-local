"""Bounded, in-memory gateway authentication protection; never handles secrets."""
from collections import deque
from dataclasses import dataclass, field
import logging
import math
import os
import time

AUTH_WINDOW_SECONDS = 60
AUTH_FAIL_LIMIT = 12
AUTH_BAN_SECONDS = 900
AUTH_MAX_BAN_SECONDS = 86400
AUTH_GLOBAL_LIMIT = 200
AUTH_GLOBAL_BAN_SECONDS = 300
AUTH_SUCCESS_SECONDS = 86400
AUTH_MAX_IPS = 4096
AUTH_WARNING_LIMIT = 20
AUTH_LOG_SECONDS = 60
LOGGER = logging.getLogger('index_hub.auth')


def _positive_env(name, default):
    try:
        value = int(os.environ.get(name, ''))
        return value if value > 0 else default
    except (ValueError, OverflowError):
        return default


@dataclass
class _IP:
    active: float
    failures: deque = field(default_factory=deque)
    banned_until: float = 0
    strikes: int = 0
    success_until: float = 0
    rejected: int = 0
    summary_at: float = 0
    warning_at: float = 0
    warnings: deque = field(default_factory=deque)
    suppressed: int = 0


class AuthThrottle:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.fail_limit = _positive_env('INDEX_HUB_AUTH_FAIL_LIMIT', AUTH_FAIL_LIMIT)
        self.ban_seconds = _positive_env('INDEX_HUB_AUTH_BAN_SECONDS', AUTH_BAN_SECONDS)
        self.global_limit = _positive_env('INDEX_HUB_AUTH_GLOBAL_LIMIT', AUTH_GLOBAL_LIMIT)
        self.ips = {}
        self.failures = deque()
        self.global_until = 0

    def _entry(self, ip, now):
        entry = self.ips.get(ip)
        if entry is None:
            if len(self.ips) >= AUTH_MAX_IPS:
                candidates = [(e.active, key) for key, e in self.ips.items()
                              if e.banned_until <= now and e.success_until <= now]
                if not candidates:
                    self._global_ban(now)
                    return None
                del self.ips[min(candidates)[1]]
            entry = self.ips[ip] = _IP(now, summary_at=now, warning_at=now)
        entry.active = now
        return entry

    def _global_ban(self, now):
        if now >= self.global_until:
            self.global_until = now + AUTH_GLOBAL_BAN_SECONDS
            LOGGER.warning('ip=all duration=%s strikes=1', AUTH_GLOBAL_BAN_SECONDS)

    def remaining(self, ip):
        now = self.clock()
        entry = self.ips.get(ip)
        until = entry.banned_until if entry else 0
        if entry is None or entry.success_until <= now:
            until = max(until, self.global_until)
        return max(1, math.ceil(until - now)) if until > now else 0

    def reject(self, ip):
        now = self.clock()
        entry = self._entry(ip, now)
        if entry is not None:
            entry.rejected += 1
            if now - entry.summary_at >= AUTH_LOG_SECONDS:
                LOGGER.info('ip=%s rejected=%s', ip, entry.rejected)
                entry.rejected = 0
                entry.summary_at = now

    def success(self, ip):
        now = self.clock()
        entry = self._entry(ip, now)
        if entry is not None:
            entry.success_until = now + AUTH_SUCCESS_SECONDS

    def failure(self, ip):
        now = self.clock()
        entry = self._entry(ip, now)
        while self.failures and self.failures[0] <= now - AUTH_WINDOW_SECONDS:
            self.failures.popleft()
        # Only the threshold is needed; keep memory bounded under a flood.
        if len(self.failures) >= self.global_limit:
            self.failures.popleft()
        self.failures.append(now)
        if len(self.failures) >= self.global_limit:
            self._global_ban(now)
        if entry is None:
            return
        while entry.failures and entry.failures[0] <= now - AUTH_WINDOW_SECONDS:
            entry.failures.popleft()
        if len(entry.failures) >= self.fail_limit:
            entry.failures.popleft()
        entry.failures.append(now)
        if len(entry.failures) >= self.fail_limit and now >= entry.banned_until:
            entry.strikes += 1
            duration = min(self.ban_seconds * (2 ** min(entry.strikes - 1, 17)), AUTH_MAX_BAN_SECONDS)
            entry.banned_until = now + duration
            entry.failures.clear()
            entry.summary_at = now
            entry.rejected = 0
            LOGGER.warning('ip=%s duration=%s strikes=%s', ip, duration, entry.strikes)

    def unauthorized(self, ip, method, path, kind):
        now = self.clock()
        entry = self._entry(ip, now)
        if entry is None:
            return
        if now - entry.warning_at >= AUTH_LOG_SECONDS:
            if entry.suppressed:
                LOGGER.info('ip=%s unauthorized_suppressed=%s', ip, entry.suppressed)
            entry.warning_at, entry.suppressed = now, 0
        while entry.warnings and entry.warnings[0] <= now - AUTH_LOG_SECONDS:
            entry.warnings.popleft()
        if len(entry.warnings) < AUTH_WARNING_LIMIT:
            LOGGER.warning('ip=%s method=%s path=%s kind=%s', ip, method, path[:200], kind)
            entry.warnings.append(now)
        else:
            entry.suppressed += 1
