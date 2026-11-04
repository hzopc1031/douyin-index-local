"""Login-only admission, durable timestamp quota and replay protection.

No phone/code is retained here or included in exception messages. All clocks
shown to people use Asia/Shanghai; deadlines use monotonic time.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sys
import time

from .models import SHANGHAI
from .runtime import DailyCounter, atomic_text, private_open


def audit(event, **fields):
    # Callers supply only fixed labels, random operation IDs and sanitized codes.
    print(json.dumps(dict(event=event, **fields)), file=sys.stderr, flush=True)


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
        self.mode = "qr"
        self.masked_phone = ""
        self.nonce = secrets.token_urlsafe(24)
        self.key = secrets.token_bytes(32)
        self.used_codes = set()
        self.code_allowed = False
        self.closed = False
        self.gate = asyncio.Lock()
        self.next_send = 0
        self.failure = None
        self.last_picture = None
        self.send_result = None
        self.audit_id = secrets.token_hex(6)
        self.browser.audit_id = self.audit_id

    def public(self):
        return dict(state=self.state, mode=self.mode, phone=self.masked_phone,
                    nonce=self.nonce, expires_in=max(0, math.ceil(self.deadline-self.clock())),
                    resend_in=max(0, math.ceil(self.next_send-self.clock())),
                    auto_attempts=self.browser.auto_attempts, failure=self.failure,
                    send_result=self.send_result,
                    failure_image=getattr(self.browser, 'failure_image', '') if self.failure and self.failure.get('reason') == 'challenge_detected' else '')

    @asynccontextmanager
    async def locked(self, phase):
        await self.gate.acquire()
        audit('lock_acquired', session=self.audit_id, phase=phase, stage=getattr(self.browser, 'stage', 'unknown'))
        try:
            yield
        finally:
            audit('lock_released', session=self.audit_id, phase=phase, stage=getattr(self.browser, 'stage', 'unknown'))
            self.gate.release()

    async def fail(self, error=None, reason='login_failed', state='failed'):
        self.failure = {'reason': reason}
        if hasattr(self.browser, 'diagnose'):
            try:
                self.failure = await self.browser.diagnose(error, reason)
            except Exception:
                audit('diagnose_failed', session=self.audit_id, reason=reason)
        audit('session_failure', session=self.audit_id, reason=self.failure.get('reason', reason))
        await self.finish(state)

    async def finish(self, state):
        if self.state != state:
            audit('session_end', session=self.audit_id, state=state)
        self.state = state
        self.last_picture = None
        self.code_allowed = False
        self.nonce = secrets.token_urlsafe(24)
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
            await self.fail(reason="login_expired", state="expired")
            return
        status = await self.browser.observe()
        if status == "succeeded":
            if await self.browser.persist() is False:
                return
            await self.finish("succeeded")
        elif status == "verification":
            if self.stop_on_verification:
                await self.fail(reason="challenge_detected")
                return
            if self.state != "verification":
                self.before_verification = self.state
            self.state = "verification"
            await self.browser.auto_step()
        elif self.state == "verification":
            self.state = self.before_verification

    async def act(self, obj):
        if obj.get('action') == 'status' or (self.gate.locked() and obj.get('action') == 'frame'):
            action = obj['action']
            fields(obj, {'action'})
            if action == 'status':
                return self.public()
            if self.last_picture is not None:
                return dict(self.public(), **self.last_picture)
            reject('frame_unavailable')
        if self.gate.locked():
            reject("busy")
        async with self.locked(obj.get('action') if obj.get('action') in
                               ('status', 'frame', 'send', 'code', 'mode', 'pointer', 'cancel') else 'unknown'):
            if self.state not in TERMINAL and self.clock() >= self.deadline:
                await self.fail(reason="login_expired", state="expired")
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
            if action in ("send", "code") or (action == "mode" and obj["mode"] == "sms"):
                reject("qr_only")
            if self.state in TERMINAL:
                return self.public()
            if action not in ("status", "frame"):
                if not isinstance(obj["nonce"], str) or not hmac.compare_digest(obj["nonce"], self.nonce):
                    reject("replayed_action")
                self.nonce = secrets.token_urlsafe(24)  # consumed before any await
            try:
                # Switching mode also reloads the login panel (60-second wait).
                budget = 90 if action == 'mode' else 40 if action == 'send' else 25
                async with asyncio.timeout(max(.001, min(budget, self.deadline-self.clock()))):
                    # Cancellation must never probe/solve another challenge or
                    # perform any new online action before releasing the lease.
                    if action == "cancel":
                        await self.finish("cancelled")
                        return self.public()
                    if action == "mode":
                        if obj["mode"] != "qr":
                            reject()
                        if self.stop_on_verification:
                            status = await self.browser.observe()
                            if status == "verification":
                                await self.fail(reason="challenge_detected")
                                return self.public()
                            if status == "succeeded":
                                await self.browser.persist()
                                await self.finish("succeeded")
                                return self.public()
                        # Switching to the fallback must not first complete
                        # the old SMS challenge and trigger its pending send.
                        await self.browser.reset(mode=obj["mode"])
                        self.mode, self.state = obj["mode"], "ready"
                        self.code_allowed = False
                        self.masked_phone = ""
                        self.send_result = None
                        await self.poll()
                        return self.public()
                    if action != "frame":
                        await self.poll()
                    if self.state in TERMINAL:
                        return self.public()
                    if action == "pointer":
                        if self.state != "verification":
                            reject("wrong_state")
                        await self.browser.pointer(obj["frame"], obj["points"])
                    if action == "frame":
                        picture = await self.browser.frame(self.state == "verification")
                        self.last_picture = picture
                        return dict(self.public(), **picture)
                    await self.poll()
                    return self.public()
            except LoginError:
                raise
            except asyncio.CancelledError:
                await self.finish("cancelled")
                raise
            except Exception as error:
                if action == 'frame' and hasattr(self.browser, 'transient_read') and (
                        self.browser.transient_read(error) or
                        (type(error).__name__ == 'TargetClosedError' and self.browser.page.is_closed())):
                    reject('frame_unavailable')
                await self.fail(error)
                reject("login_failed")
            finally:
                obj.clear()
