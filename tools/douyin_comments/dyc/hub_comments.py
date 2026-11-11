"""Hub-owned Douyin comments probe and batched Qt collection.

Runs only inside the desktop process. The HTTP API never receives cookies.
Playwright is not used against the Qt webview profile.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import site
from .errors import Stop
from .models import Video
from .paging import ingest_page
from .runtime import Budget, DailyCounter, Pacer, atomic_text, private_open

COMMENTS_BUSY = "正在采集评论，稍后重试"
INDEX_BUSY = "取数或核对正在进行，请等待。"
LOGIN_URL = "https://www.douyin.com/"
LOGIN_ENTRY = {"login_entry": "index_sms", "login_path": "/api/v1/login/sms"}
LOCK_TIMEOUT = 30
PROBE_TIMEOUT = 10
BATCH_LIMIT = 25
DAILY_LIMIT = 150
PAUSE_STOPS = {
    "verification_widget": "verification_required",
    "verification_response": "verification_required",
    "rate_limited": "rate_limited",
    "login_expired": "logged_out",
    "access_denied": "access_denied",
    "comment_redirect": "comment_redirect",
}

PROBE_JS = """JSON.stringify((() => {
  const visible = q => [...document.querySelectorAll(q)].some(e =>
    e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
  const text = document.body ? (document.body.innerText || '') : '';
  return {
    ready: document.readyState,
    https: location.protocol === 'https:',
    host: location.hostname,
    path: location.pathname,
    account: visible('[data-e2e="user-avatar"]') || visible('[data-dyc-login="yes"]'),
    login: visible('#login-panel-new, #douyin_login_comp_flat_panel, [data-e2e="login-button"], [data-dyc-login="no"]'),
    verification: visible('[id*="captcha"], [class*="captcha-verify"], [data-e2e="verify-dialog"], iframe[src*="captcha"], iframe[src*="verify"], [data-dyc-risk="yes"]')
      || ['安全验证', '身份验证', '使用原设备'].some(t => text.includes(t))
  };
})())"""

# Blocks spontaneous comment-list traffic unless the scheduler armed a one-use token.
GUARD_JS = """(function(){
  if (window.__dycHubGuard) return;
  window.__dycHubGuard = true;
  window.__dycArm = null;
  function isComment(url){
    try {
      const path = new URL(url, location.href).pathname;
      return path.indexOf('/aweme/v1/web/comment/list/') >= 0;
    } catch (e) { return false; }
  }
  function consume(url){
    if (!isComment(url)) return 'allow';
    const arm = window.__dycArm;
    window.__dycArm = null;
    return (arm === 'seed' || arm === 'fetch') ? 'allow' : 'block';
  }
  const rawFetch = window.fetch;
  window.fetch = function(input, init){
    const url = typeof input === 'string' ? input : (input && input.url);
    if (consume(url) === 'block') return Promise.reject(new TypeError('blocked'));
    return rawFetch.apply(this, arguments);
  };
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url){
    this.__dycVerdict = consume(url);
    return open.apply(this, arguments);
  };
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function(){
    if (this.__dycVerdict === 'block') { this.abort(); return; }
    return send.apply(this, arguments);
  };
})();"""

# In-page fetch with manual redirects so Python can see 3xx.
# shortcut: seed-time 3xx-before-follow is only as strict as this hook; opaque redirects also stop.
FETCH_JS = """(cfg) => {
  const visible = q => [...document.querySelectorAll(q)].some(e =>
    e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
  if (cfg.signals.captcha.some(visible)) return Promise.resolve({kind: 'risk'});
  if (cfg.signals.logged_out.some(visible)) return Promise.resolve({kind: 'login'});
  try {
    const url = new URL(cfg.url || (location.origin + cfg.path));
    for (const name of [...url.searchParams.keys()]) {
      if (cfg.signatures.some(s => s.toLowerCase() === name.toLowerCase()))
        url.searchParams.delete(name);
    }
    url.pathname = cfg.path;
    const p = cfg.params;
    url.searchParams.set(p.video, cfg.video);
    url.searchParams.set(p.cursor, cfg.cursor);
    url.searchParams.set(p.count, '20');
    if (cfg.root) {
      url.searchParams.set(p.item, cfg.video);
      url.searchParams.set(p.root, cfg.root);
    } else {
      url.searchParams.delete(p.item);
      url.searchParams.delete(p.root);
    }
    const token = localStorage.getItem(cfg.storage);
    if (token) url.searchParams.set(p.token, token);
    window.__dycArm = 'fetch';
    const ac = new AbortController();
    const abort = () => ac.abort();
    window.__dycAbort = abort;
    const timer = setTimeout(() => ac.abort('timeout'), cfg.timeout);
    return fetch(url.href, {credentials: 'include', redirect: 'manual', signal: ac.signal,
                            headers: Object.assign({}, cfg.headers || {}, {[cfg.markerHeader]: cfg.marker})})
      .then(r => {
        if (r.type === 'opaqueredirect' || (r.status >= 300 && r.status < 400))
          return {kind: 'opaqueredirect', status: r.status};
        return r.text().then(text => ({status: r.status, text: text}));
      })
      .catch(err => ({kind: err === 'timeout' || (err && err.name === 'TimeoutError') ? 'timeout'
                      : (err && err.name === 'AbortError' ? 'timeout' : 'network')}))
      .finally(() => { clearTimeout(timer); if (window.__dycAbort === abort) window.__dycAbort = null; window.__dycArm = null; });
  } catch (e) {
    window.__dycArm = null;
    return Promise.resolve({kind: 'network'});
  }
}"""


def lock_busy(owner):
    if owner == "comments":
        return {"ok": False, "code": "comments_busy", "message": COMMENTS_BUSY}
    return {"ok": False, "code": "busy", "message": INDEX_BUSY}


def classify_probe(page, session_cookie=False):
    """Map a cookie-free page snapshot to a probe state."""
    if not isinstance(page, dict):
        return "not_logged_in"
    if page.get("verification"):
        return "verification_required"
    host = (page.get("host") or "").lower().lstrip(".")
    on_site = page.get("https") is True and host in ("www.douyin.com", "douyin.com")
    if on_site and (page.get("account") or (not page.get("login") and session_cookie)):
        return "logged_in"
    return "not_logged_in"


def parse_probe_js(raw):
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        page = json.loads(raw)
    except ValueError:
        return None
    return page if isinstance(page, dict) else None


def login_hint(state):
    if state == "logged_in":
        return {}
    return dict(LOGIN_ENTRY)


def pause_reason(stop_reason):
    return PAUSE_STOPS.get(stop_reason, stop_reason)


class QtCommentTransport:
    """Scheduler-driven in-page fetch. Guards live in GUARD_JS plus this arming."""

    def __init__(self, window, budget, pacer, *, evaluate=None):
        self.window = window
        self.budget = budget
        self.pacer = pacer
        self._evaluate = evaluate or (lambda js, arg=None: _eval(window, js, arg))
        self.calls = 0

    async def fetch(self, video_id, root, cursor):
        self.budget.reserve()
        self.calls += 1
        cfg = {
            "url": LOGIN_URL,
            "path": site.REPLY_PATH if root else site.TOP_PATH,
            "params": site.PARAM,
            "signatures": list(site.SIGNATURES),
            "storage": site.TOKEN_STORAGE,
            "signals": {key: list(value) for key, value in site.SIGNALS.items()},
            "video": video_id,
            "root": root or "",
            "cursor": cursor,
            "timeout": 20000,
            "headers": {},
            "markerHeader": "X-Dyc-Hub",
            "marker": "1",
        }
        try:
            self._evaluate("(cfg) => { const request = {}; window.__dycRequest = request; window.__dycResult = null; (" + FETCH_JS + ")(cfg).then(r => { if (window.__dycRequest === request) window.__dycResult = JSON.stringify(r); }); }", cfg)
            while True:
                raw = self._evaluate("window.__dycResult")
                if raw and raw != "null":
                    return _coerce_fetch(raw)
                await asyncio.sleep(0.1)
        finally:
            self._evaluate("window.__dycAbort && window.__dycAbort()")
            self.pacer.completed()


def _eval(window, js, arg=None):
    # pywebview Window.evaluate_js(script, callback=None): the second
    # argument is a callback, not a JavaScript value.
    if arg is None:
        return window.evaluate_js(js)
    return window.evaluate_js(f"({js})({json.dumps(arg, ensure_ascii=False)})")


def _coerce_fetch(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except ValueError:
            return {"kind": "network"}
        return data if isinstance(data, dict) else {"kind": "network"}
    return {"kind": "network"}


class CollectJob:
    """Resumable pagination so lock slices can release between short holds."""

    def __init__(self, video, transport, page_budget=BATCH_LIMIT):
        self.video = video
        self.transport = transport
        self.page_budget = page_budget
        self.used = 0
        self.queue = [(None, "0")]
        self.seen = {None: set()}
        self.cursors = {None: {"0"}}
        self.top_done = False
        self.done = False

    async def run_slice(self, max_pages):
        started = 0
        while self.queue and started < max_pages:
            if self.used >= self.page_budget:
                raise Stop(2, "batch_cap")
            root, cursor = self.queue.pop(0)
            decoded = await self._page(root, cursor)
            started += 1
            comments, next_cursor, more, _ = decoded
            ingest_page(self.video, root, decoded)
            bucket = self.seen.setdefault(root, set())
            fresh = 0
            for raw in comments:
                cid = site.identifier(raw.get(site.FIELDS["id"]))
                if cid not in bucket:
                    fresh += 1
                    bucket.add(cid)
                if root is None:
                    thread = self.video.threads.get(cid)
                    if thread is not None and (thread.reported != 0 or thread.ids):
                        if (cid, "0") not in self.queue and "0" not in self.cursors.get(cid, set()):
                            self.queue.append((cid, "0"))
                            self.cursors.setdefault(cid, set()).add("0")
                    elif thread is not None and thread.reason == "pending":
                        thread.status, thread.reason = "complete", "exhausted"
            if comments and not fresh:
                self._finish_branch(root, "duplicate_page")
                continue
            if not more:
                self._finish_branch(root, "exhausted")
                continue
            if not comments:
                self._finish_branch(root, "empty_with_more")
                continue
            known = self.cursors.setdefault(root, set())
            if next_cursor in known:
                reason = "cursor_cycle" if next_cursor != cursor else "cursor_unchanged"
                self._finish_branch(root, reason)
                continue
            known.add(next_cursor)
            self.queue.insert(0, (root, next_cursor))
        if not self.queue:
            self.done = True
            thread_ok = all(thread.status == "complete" for thread in self.video.threads.values())
            top_ok = self.video.reason in (None, "pending", "exhausted")
            if top_ok and thread_ok:
                self.video.status, self.video.reason = "complete", "exhausted"
            else:
                self.video.status = "partial"
                if top_ok:
                    self.video.reason = "partial_threads"
        return not self.done

    def _finish_branch(self, root, reason):
        if root is None:
            self.video.reason = reason
            self.top_done = True
            if reason != "exhausted":
                self.video.status = "partial"
        else:
            thread = self.video.threads.get(root)
            if thread is not None:
                if thread.reported is not None and len(thread.ids) < thread.reported and reason == "exhausted":
                    reason = "reply_shortfall"
                thread.reason = reason
                thread.status = "complete" if reason == "exhausted" else "partial"
                if thread.status == "partial":
                    self.video.status = "partial"
                    if self.video.reason in (None, "exhausted"):
                        self.video.reason = "partial_threads"

    async def _page(self, root, cursor):
        for attempt in range(2):
            if self.used >= self.page_budget:
                raise Stop(2, "batch_cap")
            self.used += 1
            self.video.pages += 1
            take_seed = getattr(self.transport, "take_seed", None)
            seed = take_seed(self.video.video_id, root, cursor) if take_seed else None
            if seed is not None:
                return seed
            result = await self.transport.fetch(self.video.video_id, root, cursor)
            try:
                return site.decode_page(result)
            except Stop as error:
                if error.reason in ("timeout", "network") and attempt == 0:
                    continue
                if error.reason in ("timeout", "network"):
                    raise Stop(3, "repeated_" + error.reason) from None
                raise


class CommentsHub:
    def __init__(self, *, window_factory, state_dir, sleeper=time.sleep, clock=time.monotonic,
                 transport_factory=None, lock_timeout=LOCK_TIMEOUT,
                 acquire=None, release=None, owner=None):
        self._window_factory = window_factory
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        self._sleeper = sleeper
        self._clock = clock
        self._transport_factory = transport_factory
        self.lock_timeout = lock_timeout
        self._acquire = acquire
        self._release = release
        self._owner = owner or (lambda: None)
        self._guard = threading.Lock()
        self._worker = None
        self._stop = threading.Event()
        self._snapshot = {
            "probe": None,
            "running": False,
            "paused": False,
            "pause_reason": None,
            "busy": False,
            "waiting_for_index": False,
            "summary": None,
            "output": None,
        }

        path = self.state_dir / "pause.json"
        if path.exists() or path.is_symlink():
            try:
                with os.fdopen(private_open(path, os.O_RDONLY)) as stream:
                    saved = json.load(stream)
                if set(saved) != {"paused", "pause_reason"} or type(saved["paused"]) is not bool or (saved["pause_reason"] is not None and saved["pause_reason"] not in set(PAUSE_STOPS.values()) | {"user", "browser_unavailable", "not_logged_in"}):
                    raise ValueError
                self._snapshot.update(saved)
            except (OSError, ValueError, TypeError, Stop):
                self._snapshot.update(paused=True, pause_reason="browser_unavailable")

    def _save_pause(self):
        atomic_text(self.state_dir / "pause.json", json.dumps({
            "paused": self._snapshot["paused"], "pause_reason": self._snapshot["pause_reason"]}) + "\n")

    def status(self):
        with self._guard:
            data = dict(self._snapshot)
        data.update(login_hint(data.get("probe")))
        return data

    def probe(self):
        if self._acquire is not None and not self._acquire("comments"):
            status = dict(lock_busy(self._owner()), **self.status())
            status["ok"] = False
            return status
        try:
            window = self._window_factory()
            self._ensure_url(window)
            self._ensure_guard(window)
            deadline = self._clock() + PROBE_TIMEOUT
            while True:
                page = parse_probe_js(window.evaluate_js(PROBE_JS))
                if page and (page.get("ready") == "complete" or any(
                        page.get(key) for key in ("account", "login", "verification"))):
                    break
                if self._clock() >= deadline:
                    raise TimeoutError("Probe page unavailable")
                self._sleeper(0.2)
            session_cookie = False
            if (page.get("ready") == "complete" and not any(
                    page.get(key) for key in ("account", "login", "verification"))):
                from browser_login import KEY_COOKIES
                session_cookie = any(item.key in KEY_COOKIES and item["domain"].lower() in
                                     (".douyin.com", "douyin.com")
                                     for cookie in window.get_cookies() for item in cookie.values())
            state = classify_probe(page, session_cookie)
            with self._guard:
                self._snapshot["probe"] = state
            result = {"ok": True, "probe": state}
            result.update(login_hint(state))
            return result
        except Exception:
            with self._guard:
                self._snapshot["probe"] = None
            return {"ok": False, "code": "browser_unavailable", "probe": None, **LOGIN_ENTRY}
        finally:
            if self._release is not None:
                self._release()

    def start(self, payload, *, budget=None, pacer=None):
        try:
            videos = self._videos(payload)
        except ValueError:
            return {"ok": False, "code": "invalid_parameters", "message": "请提供有效的视频链接列表。"}
        probed = self.probe()
        if not probed.get("ok"):
            return probed
        if probed["probe"] != "logged_in":
            return {"ok": False, "code": probed["probe"], "probe": probed["probe"], **login_hint(probed["probe"])}
        if self._worker is not None and self._worker.is_alive():
            return {"ok": False, "code": "busy", "message": COMMENTS_BUSY, **self.status()}
        with self._guard:
            running = self._snapshot["running"]
            paused = self._snapshot["paused"]
            reason = self._snapshot["pause_reason"]
            snap = dict(self._snapshot)
        if running:
            return {"ok": False, "code": "busy", "message": COMMENTS_BUSY, **snap, **login_hint(snap.get("probe"))}
        if paused and reason not in (None, "user"):
            return {"ok": False, "code": "paused", "pause_reason": reason, **snap, **login_hint(snap.get("probe"))}
        with self._guard:
            self._stop.clear()
            self._snapshot.update(running=True, paused=False, pause_reason=None, busy=False,
                                  waiting_for_index=False, summary=None, output=None)
            self._save_pause()
        counter = DailyCounter(self.state_dir, limit=DAILY_LIMIT)
        try:
            counter.check_start()
        except Stop as error:
            self._halt(False, pause_reason(error.reason))
            return {"ok": False, "code": error.reason, **self.status()}
        used_budget = budget or Budget(counter, batch_limit=self._limit(payload, "batch_limit", BATCH_LIMIT))
        used_pacer = pacer or Pacer()
        thread = threading.Thread(target=self._run, args=(videos, used_budget, used_pacer),
                                  name="hub-comments", daemon=True)
        self._worker = thread
        thread.start()
        return {"ok": True, "state": "running", "probe": "logged_in"}

    def pause(self):
        self._stop.set()
        with self._guard:
            self._snapshot["paused"] = True
            self._snapshot["pause_reason"] = self._snapshot["pause_reason"] or "user"
            self._save_pause()
        return {"ok": True, **self.status()}

    def resume(self):
        with self._guard:
            was = self._snapshot["pause_reason"]
        probed = self.probe()
        if not probed.get("ok") or probed.get("probe") != "logged_in":
            with self._guard:
                self._snapshot["paused"] = True
                self._snapshot["pause_reason"] = probed.get("probe") or "not_logged_in"
                self._save_pause()
            return {"ok": False, "code": probed.get("probe") or probed.get("code") or "not_logged_in",
                    **self.status()}
        with self._guard:
            self._snapshot.update(paused=False, pause_reason=None)
            self._save_pause()
        return {"ok": True, "note": "已清除暂停，请再次调用 start 继续采集。", "probe": probed["probe"],
                "previous_reason": was}

    def _run(self, videos, budget, pacer):
        summaries = []
        try:
            for index, video in enumerate(videos):
                if self._stop.is_set():
                    break
                if index:
                    try:
                        asyncio.run(pacer.between_videos())
                    except Exception:
                        pass
                job = CollectJob(video, self._locked_transport(self._make_transport(budget, pacer), pacer), BATCH_LIMIT)
                try:
                    while not job.done and not self._stop.is_set():
                        more = asyncio.run(job.run_slice(1))
                        if not more:
                            break
                except Stop as error:
                    reason = pause_reason(error.reason)
                    if error.reason in PAUSE_STOPS:
                        self._halt(True, reason)
                        summaries.append(video.summary() if hasattr(video, "summary") else {"reason": reason})
                        self._store_summary(summaries)
                        self._export(videos, error.code, reason)
                        return
                    video.status, video.reason = "partial", error.reason
                summaries.append(video.summary() if hasattr(video, "summary") else {"status": video.status,
                                                                                   "reason": video.reason})
            self._store_summary(summaries)
            self._export(videos, 0 if not self._stop.is_set() else 2,
                         (self.status().get("pause_reason") or "complete"))
            with self._guard:
                self._snapshot["running"] = False
                if self._stop.is_set() and not self._snapshot["pause_reason"]:
                    self._snapshot["paused"] = True
                    self._snapshot["pause_reason"] = "user"
        except Exception:
            try:
                self._export(videos, 3, "browser_unavailable")
            except Exception:
                pass
            self._halt(True, "browser_unavailable")

    def _locked_transport(self, transport, pacer):
        hub = self

        class LockedTransport:
            async def fetch(self, video_id, root, cursor):
                await pacer.before()
                acquired = hub._acquire is None
                try:
                    while not acquired and not hub._stop.is_set():
                        acquired = hub._acquire("comments")
                        with hub._guard:
                            hub._snapshot["waiting_for_index"] = not acquired and hub._owner() == "index"
                        if not acquired:
                            await asyncio.sleep(0.2)
                    if not acquired:
                        raise Stop(2, "user")
                    with hub._guard:
                        hub._snapshot.update(waiting_for_index=False, busy=True)
                    return await hub._timed(transport.fetch(video_id, root, cursor))
                finally:
                    with hub._guard:
                        hub._snapshot.update(waiting_for_index=False, busy=False)
                    if acquired and hub._acquire is not None and hub._release is not None:
                        hub._release()
                    if acquired:
                        await asyncio.sleep(1)

        return LockedTransport()

    async def _timed(self, awaitable):
        try:
            return await asyncio.wait_for(awaitable, timeout=self.lock_timeout)
        except asyncio.TimeoutError:
            raise Stop(2, "batch_timeout") from None

    def _make_transport(self, budget, pacer):
        if self._transport_factory:
            return self._transport_factory(budget, pacer)
        return QtCommentTransport(self._window_factory(), budget, pacer)

    def _halt(self, paused, reason):
        self._stop.set()
        with self._guard:
            self._snapshot["running"] = False
            self._snapshot["busy"] = False
            self._snapshot["waiting_for_index"] = False
            if paused:
                self._snapshot["paused"] = True
                self._snapshot["pause_reason"] = reason
                self._save_pause()

    def _store_summary(self, summaries):
        with self._guard:
            self._snapshot["summary"] = {
                "videos": len(summaries),
                "statuses": [item.get("status") if isinstance(item, dict) else None for item in summaries],
                "reasons": [item.get("reason") if isinstance(item, dict) else None for item in summaries],
            }

    def _export(self, videos, code, reason):
        from .output import export
        run_id = time.strftime("%Y%m%dT%H%M%S", time.localtime())
        out = self.state_dir / "runs" / run_id
        requests = sum(getattr(video, "pages", 0) for video in videos)
        result = export(out, run_id, videos, code, reason, requests)
        with self._guard:
            self._snapshot["output"] = {"run_id": run_id, "requests": requests,
                                        "videos": result.get("videos")}

    def _ensure_url(self, window):
        current = urlsplit(window.get_current_url() or "")
        if current.scheme != "https" or current.hostname not in ("www.douyin.com", "douyin.com") or current.port not in (None, 443):
            with self._guard:
                running = self._snapshot["running"]
            if running or (self._worker is not None and self._worker.is_alive()):
                raise RuntimeError("Comments collection running")
            window.load_url(LOGIN_URL)

    def _ensure_guard(self, window):
        window.evaluate_js(GUARD_JS)

    def _videos(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("invalid_parameters")
        raw = payload.get("videos") or payload.get("urls") or []
        if not isinstance(raw, list) or not raw or len(raw) > 20:
            raise ValueError("invalid_parameters")
        videos = []
        for item in raw:
            if not isinstance(item, str):
                raise ValueError("invalid_parameters")
            try:
                target = site.parse_url(item)
            except Stop:
                raise ValueError("invalid_parameters") from None
            if not target.video_id:
                raise ValueError("invalid_parameters")
            videos.append(Video(target.video_id, target.url))
        return videos

    def _limit(self, payload, key, default):
        value = (payload or {}).get(key, default)
        if type(value) is not int or isinstance(value, bool) or value < 1:
            return default
        return min(value, default)
