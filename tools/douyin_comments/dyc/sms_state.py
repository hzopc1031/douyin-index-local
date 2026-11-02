"""Login-only admission, durable timestamp quota and replay protection.

No phone/code is retained here or included in exception messages. All clocks
shown to people use Asia/Shanghai; deadlines use monotonic time.
"""
import asyncio
from datetime import datetime
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time

from .models import SHANGHAI
from .runtime import DailyCounter, atomic_text, private_open


class LoginError(Exception):
    """Only locally defined codes, never browser exception text."""


def reject(code="invalid_request"):
    raise LoginError(code) from None


def unique(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            reject()
        obj[key] = value
    return obj


def payload(raw):
    try:
        obj = json.loads(raw, object_pairs_hook=unique,
                         parse_constant=lambda _: reject())
        if type(obj) is not dict:
            reject()
        return obj
    except (ValueError, UnicodeError, RecursionError):
        reject()


def fields(obj, names):
    if type(obj) is not dict or set(obj) != set(names):
        reject()


def secret(value, pattern):
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        reject()
    return value


class SmsQuota(DailyCounter):
    """Shared within the dyc state directory; charge BEFORE browser input.

    Store timestamps only, including failed/uncertain attempts. Locked atomic
    writes survive process restarts. Clock rollback/corrupt state fail closed.
    These limits do not include the independent index tool's SMS attempts.
    """
    def __init__(self, state, clock=time.time):
        super().__init__(state)
        self.clock = clock

    def reserve(self):
        with self.locked():
            path = self.state / "sms-attempts.json"
            now = self.clock()
            stamps = []
            if path.exists() or path.is_symlink():
                try:
                    with os.fdopen(private_open(path, os.O_RDONLY)) as stream:
                        raw = stream.read(4097)
                    if len(raw) > 4096:
                        reject("quota_unavailable")
                    stamps = json.loads(raw)
                    if (type(stamps) is not list or len(stamps) > 12 or
                        any(type(t) not in (int, float) or not math.isfinite(t)
                            or t < 0 or t > now for t in stamps) or stamps != sorted(stamps)):
                        reject("quota_unavailable")
                except (ValueError, OSError):
                    reject("quota_unavailable")
            if stamps and now - stamps[-1] < 60:
                reject("send_interval")
            stamps = [t for t in stamps if now - t < 86400]
            day = datetime.fromtimestamp(now, SHANGHAI).date()
            if sum(now - t < 3600 for t in stamps) >= 3:
                reject("send_hour_limit")
            if sum(datetime.fromtimestamp(t, SHANGHAI).date() == day for t in stamps) >= 6:
                reject("send_day_limit")
            atomic_text(path, json.dumps(stamps + [now]) + "\n")


TERMINAL = {"succeeded", "failed", "expired", "cancelled"}


class LoginSession:
    def __init__(self, browser, quota, *, clock=time.monotonic, seconds=600,
                 stop_on_verification=False):
        self.stop_on_verification = stop_on_verification
        self.browser, self.quota, self.clock = browser, quota, clock
        self.deadline = clock() + min(seconds, 600)
        self.state = "ready"
        self.before_verification = "ready"
        self.mode = "sms"
        self.masked_phone = ""
        self.nonce = secrets.token_urlsafe(24)
        self.key = secrets.token_bytes(32)
        self.used_codes = set()
        self.code_allowed = False
        self.closed = False
        self.gate = asyncio.Lock()
        self.next_send = 0

    def public(self):
        return dict(state=self.state, mode=self.mode, phone=self.masked_phone,
                    nonce=self.nonce, expires_in=max(0, math.ceil(self.deadline-self.clock())),
                    resend_in=max(0, math.ceil(self.next_send-self.clock())),
                    auto_attempts=self.browser.auto_attempts)

    async def finish(self, state):
        self.state = state
        self.code_allowed = False
        self.nonce = ""
        self.used_codes.clear()
        self.key = b""
        self.masked_phone = ""
        if not self.closed:
            self.closed = True
            await self.browser.close()

    async def poll(self):
        if self.state in TERMINAL:
            return
        if self.clock() >= self.deadline:
            await self.finish("expired")
            return
        status = await self.browser.observe()
        if status == "succeeded":
            await self.browser.persist()
            await self.finish("succeeded")
        elif status == "verification":
            if self.stop_on_verification:
                await self.finish("failed")
                return
            if self.state != "verification":
                self.before_verification = self.state
            self.state = "verification"
            await self.browser.auto_step()
        elif self.state == "verification":
            self.state = self.before_verification

    async def act(self, obj):
        if self.gate.locked():
            reject("busy")
        async with self.gate:
            if self.clock() >= self.deadline:
                await self.finish("expired")
            if self.state in TERMINAL:
                return self.public()
            action = obj.get("action")
            schema = {"status": {"action"}, "frame": {"action"},
                      "send": {"action", "nonce", "phone"},
                      "code": {"action", "nonce", "code"},
                      "mode": {"action", "nonce", "mode"},
                      "pointer": {"action", "nonce", "frame", "points"},
                      "cancel": {"action", "nonce"}}
            if not isinstance(action, str) or action not in schema:
                reject()
            fields(obj, schema[action])
            if action not in ("status", "frame"):
                if not isinstance(obj["nonce"], str) or not hmac.compare_digest(obj["nonce"], self.nonce):
                    reject("replayed_action")
                self.nonce = secrets.token_urlsafe(24)  # consumed before any await
            try:
                async with asyncio.timeout(max(.001, min(25, self.deadline-self.clock()))):
                    # Cancellation must never probe/solve another challenge or
                    # perform any new online action before releasing the lease.
                    if action == "cancel":
                        await self.finish("cancelled")
                        return self.public()
                    if action == "mode":
                        if obj["mode"] not in ("sms", "qr"):
                            reject()
                        if self.stop_on_verification:
                            await self.poll()
                            if self.state in TERMINAL:
                                return self.public()
                        # Switching to the fallback must not first complete
                        # the old SMS challenge and trigger its pending send.
                        await self.browser.reset()
                        self.mode, self.state = obj["mode"], "ready"
                        self.code_allowed = False
                        self.masked_phone = ""
                        await self.poll()
                        return self.public()
                    await self.poll()
                    if self.state in TERMINAL:
                        return self.public()
                    if action == "send":
                        if self.mode != "sms" or self.state not in ("ready", "code_sent"):
                            reject("wrong_state")
                        phone = secret(obj.pop("phone"), r"1[3-9][0-9]{9}")
                        try:
                            self.quota.reserve()
                            self.next_send = self.clock() + 60
                            self.code_allowed = True
                            self.masked_phone = phone[:3] + "****" + phone[-4:]
                            await self.browser.send(phone)
                            self.state = "code_sent"
                        finally:
                            phone = None
                    elif action == "code":
                        if self.mode != "sms" or self.state != "code_sent" or not self.code_allowed:
                            reject("wrong_state")
                        code = secret(obj.pop("code"), r"[0-9]{6}")
                        try:
                            digest = hmac.digest(self.key, code.encode(), "sha256")
                            if digest in self.used_codes:
                                reject("replayed_code")
                            self.used_codes.add(digest)
                            self.code_allowed = False
                            self.state = "submitting"
                            await self.browser.submit(code)
                        finally:
                            code = None
                    elif action == "pointer":
                        if self.state != "verification":
                            reject("wrong_state")
                        await self.browser.pointer(obj["frame"], obj["points"])
                    if action == "frame":
                        picture = await self.browser.frame(self.state == "verification")
                        return dict(self.public(), **picture)
                    await self.poll()
                    return self.public()
            except LoginError:
                raise
            except asyncio.CancelledError:
                await self.finish("cancelled")
                raise
            except Exception:
                await self.finish("failed")
                reject("login_failed")
            finally:
                obj.clear()
