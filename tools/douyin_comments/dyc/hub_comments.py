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

# One page request is admitted per scheduler slice; responses stay in page memory.
GUARD_JS = """(function(){
  if (window.__dycHubGuard) return;
  window.__dycHubGuard = true;
  window.__dycArm = sessionStorage.getItem('__dycNavigate') === '1';
  sessionStorage.removeItem('__dycNavigate');
  const video = sessionStorage.getItem('__dycVideo');
  sessionStorage.removeItem('__dycVideo');
  window.__dycResult = null;
  window.__dycSent = false;
  const templates = {};
  let replaying = false;
  function remember(url) {
    if (replaying) return;
    const target = new URL(url, location.href);
    templates[target.pathname.endsWith('/reply/') ? 'reply' : 'list'] ||= target.href;
  }
  window.__dycReplay = function(cfg) {
    if (!window.__dycArm) return false;
    const source = templates[cfg.root ? 'reply' : 'list'] || (cfg.root && templates.list);
    if (!source) return false;
    const target = new URL(source);
    target.pathname = cfg.root ? '/aweme/v1/web/comment/list/reply/' : '/aweme/v1/web/comment/list/';
    const videoKey = cfg.root && target.searchParams.has('item_id') ? 'item_id' : 'aweme_id';
    target.searchParams.set(videoKey, cfg.video);
    target.searchParams.set('cursor', cfg.cursor);
    if (cfg.root) target.searchParams.set('comment_id', cfg.root);
    for (const key of [...target.searchParams.keys()]) {
      if (['a_bogus', 'x-bogus', '_signature'].includes(key.toLowerCase())) target.searchParams.delete(key);
      else if (key.toLowerCase() === 'mstoken') {
        let value = null;
        try { value = localStorage.getItem('xmst'); } catch (e) {}
        if (value) target.searchParams.set(key, value);
      }
    }
    replaying = true;
    try { const xhr = new XMLHttpRequest(); xhr.open('GET', target.href); xhr.send(); }
    finally { replaying = false; }
    return true;
  };
  function consume(url){
    try {
      const target = new URL(url, location.href);
      const path = target.pathname;
      if (path !== '/aweme/v1/web/comment/list/' && path !== '/aweme/v1/web/comment/list/reply/') return null;
      if (target.origin !== new URL(location.href).origin) return false;
      if (!window.__dycArm) return false;
      if (video && (target.searchParams.get('aweme_id') || target.searchParams.get('item_id')) !== video) return false;
      const root = window.__dycRoot || null;
      if (root ? path !== '/aweme/v1/web/comment/list/reply/' || target.searchParams.get('comment_id') !== root
               : path !== '/aweme/v1/web/comment/list/') return false;
      window.__dycArm = false;
      return true;
    } catch (e) { return null; }
  }
  const rawFetch = window.fetch;
  window.fetch = function(input, init){
    const allowed = consume(typeof input === 'string' ? input : input && (input.url || input.href));
    if (allowed === false) return Promise.reject(new TypeError('blocked'));
    const promise = rawFetch.apply(this, arguments);
    const signal = (init && init.signal) || (input && input.signal);
    if (allowed && !(signal && signal.aborted)) { window.__dycSent = true; remember(typeof input === 'string' ? input : input && (input.url || input.href)); }
    if (allowed) promise.then(r => {
      if (r.redirected || r.type === 'opaqueredirect') window.__dycResult = JSON.stringify({kind:'opaqueredirect', status:r.status});
      else r.clone().text().then(text => { window.__dycResult = JSON.stringify({status:r.status, text}); },
        () => { window.__dycResult = JSON.stringify({kind:'network'}); });
    }, () => { window.__dycResult = JSON.stringify({kind:'network'}); });
    return promise;
  };
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url){
    this.__dycUrl = url;
    return open.apply(this, arguments);
  };
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function(){
    const allowed = consume(this.__dycUrl);
    if (allowed === false) { this.abort(); return; }
    if (allowed) this.addEventListener('loadend', () => {
      try { window.__dycResult = JSON.stringify(this.responseURL && new URL(this.responseURL).pathname !== new URL(this.__dycUrl, location.href).pathname ? {kind:'opaqueredirect', status:this.status} : {status:this.status, text:this.responseText}); }
      catch (e) { window.__dycResult = JSON.stringify({kind:'network'}); }
    }, {once:true});
    const result = send.apply(this, arguments);
    if (allowed) { window.__dycSent = true; remember(this.__dycUrl); }
    return result;
  };
})();"""

# shortcut: reply matching requires unique visible comment text and nickname; upgrade when stable comment IDs are observed in the DOM.
SCROLL_JS = r"""(cfg => {
  const visible = e => {
    const r = e.getBoundingClientRect(), style = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && r.left < innerWidth && r.right > 0 &&
      r.top < innerHeight && r.bottom > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  };
  const find = q => [...document.querySelectorAll(q)].find(visible);
  const risk = '[id*="captcha"], [class*="captcha-verify"], [data-e2e="verify-dialog"], iframe[src*="captcha"], iframe[src*="verify"], [data-dyc-risk="yes"]';
  const text = document.body ? (document.body.innerText || '') : '';
  if (find(risk) || ['安全验证', '身份验证', '使用原设备'].some(t => text.includes(t))) return false;
  const box = find('[data-e2e="comment-list"]');
  // shortcut: the share-anchored row has four numeric/icon actions; upgrade if its structure changes.
  const rows = [...document.querySelectorAll('[data-e2e="video-share-icon-container"]')].filter(visible).flatMap(share => {
    const matches = [];
    for (let row = share.parentElement; row; row = row.parentElement) {
      const items = [...row.children];
      if (visible(row) && items.length === 4 && items.every(visible) && items[3].contains(share) &&
          items.slice(0, 3).every(x => /^\s*\d+\s*$/.test(x.innerText || '') && x.querySelector('svg, img')) &&
          (items[3].innerText || '').trim() === '分享' && getComputedStyle(items[1]).cursor === 'pointer' &&
          getComputedStyle(items[1]).pointerEvents !== 'none' &&
          !items[1].matches(':disabled, [aria-disabled="true"]')) matches.push(row);
    }
    return matches;
  });
  const button = rows.length === 1 ? rows[0].children[1] : null;
  if (document.readyState !== 'complete' && !box && !button) return false;
  if (window.__dycSent || window.__dycResult) return true;
  window.__dycRoot = cfg.root;
  window.__dycArm = true;
  if (!box) {
    if (!button || cfg.opened) return false;
    button.click();
    return cfg.root ? "opened" : true;
  }
  if (cfg.root) {
    const items = [...box.querySelectorAll('[data-e2e="comment-item"]')].filter(e =>
      visible(e) && cfg.text && cfg.nickname && (e.innerText || '').includes(cfg.text) && (e.innerText || '').includes(cfg.nickname));
    if (items.length === 1) {
      const control = [...items[0].querySelectorAll('button, span, div, a')].find(e =>
        visible(e) && /^展开\s*\d*\s*条?回复$/.test((e.innerText || '').trim()));
      if (control) control.click();
    }
    return true;
  }
  let scroller = box;
  while (scroller && !(scroller.scrollHeight > scroller.clientHeight &&
      /^(auto|scroll)$/.test(getComputedStyle(scroller).overflowY))) scroller = scroller.parentElement;
  if (scroller) scroller.scrollTop = scroller.scrollHeight;
  else window.scrollTo(0, document.documentElement.scrollHeight);
  return true;
})"""


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
    """Capture one page-originated request per locked slice."""
    no_retry = True

    def __init__(self, window, budget, pacer, *, evaluate=None):
        self.window, self.budget, self.pacer = window, budget, pacer
        self._evaluate = evaluate or (lambda js, arg=None: _eval(window, js, arg))
        self.calls = 0
        self.video_id = None

    async def fetch(self, video_id, root, cursor):
        if self.video_id != video_id:
            self.budget.new_video()
        if self.budget.used >= self.budget.limit:
            raise Stop(2, "batch_cap")
        if self.budget.video_used >= self.budget.video_limit:
            raise Stop(2, "video_page_budget")
        self.budget.counter.check_start()
        sent = False
        try:
            if self.video_id != video_id:
                self._evaluate("window.__dycSent = false; window.__dycResult = null; sessionStorage.setItem('__dycNavigate', '1'); sessionStorage.setItem('__dycVideo', " + json.dumps(video_id) + ")")
                self.window.load_url(self.video_url)
                self.video_id = video_id
            else:
                self._evaluate("window.__dycSent = false; window.__dycResult = null")
            acted = False
            opened = False
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                state = _coerce_fetch(self._evaluate("JSON.stringify({sent:window.__dycSent, result:window.__dycResult})"))
                if state.get("sent") and not sent:
                    sent = True
                    self.budget.reserve()
                    self.calls += 1
                result = _coerce_fetch(state["result"]) if state.get("result") else {}
                page = parse_probe_js(self._evaluate(PROBE_JS))
                on_site = page and page.get("https") and page.get("host") in site.HOSTS
                if on_site and page.get("verification"):
                    result["kind"] = "risk"
                elif on_site and page.get("ready") == "complete" and page.get("login") and not page.get("account"):
                    result["kind"] = "login"
                if result:
                    result["request_sent"] = sent
                    return result
                if not acted and not sent and on_site and page.get("ready") == "complete" and page.get("path") == f"/video/{video_id}":
                    if self._evaluate("(cfg => { window.__dycRoot = cfg.root; window.__dycArm = true; const sent = window.__dycReplay && window.__dycReplay(cfg); if (!sent) window.__dycArm = false; return !!sent; })", {"video": video_id, "root": root or "", "cursor": cursor}) is True:
                        acted = True
                        continue
                    comment = getattr(self, "video", None)
                    comment = comment.comments.get(root) if comment and root else None
                    action = self._evaluate(SCROLL_JS, {"root": root or "", "text": comment.text if comment else "",
                                                        "nickname": comment.nickname if comment else "", "opened": opened})
                    opened = opened or action == "opened"
                    acted = action is True
                await asyncio.sleep(0.1)
            return {"kind": "timeout" if sent else "no_request", "request_sent": sent}
        finally:
            if not sent:
                state = _coerce_fetch(self._evaluate("JSON.stringify({sent:window.__dycSent})"))
                if state.get("sent"):
                    self.budget.reserve()
                    self.calls += 1
                    sent = True
            self._evaluate("window.__dycArm = false; sessionStorage.removeItem('__dycNavigate'); sessionStorage.removeItem('__dycVideo')")
            if sent:
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
        self.metadata = None

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
            take_seed = getattr(self.transport, "take_seed", None)
            seed = take_seed(self.video.video_id, root, cursor) if take_seed else None
            if seed is not None:
                self.used += 1
                self.video.pages += 1
                return seed
            result = await self.transport.fetch(self.video.video_id, root, cursor)
            sent = result.get("request_sent", True)
            if sent:
                self.used += 1
                self.video.pages += 1
            keys = []
            try:
                body = json.loads(result.get("text", ""))
                if isinstance(body, dict):
                    fields = {*site.FIELDS.values(), *site.RISK_FIELDS, *site.LOGIN_FIELDS, "status_msg", "message"}
                    keys = [key for key in body if key in fields]
            except (ValueError, TypeError):
                pass
            self.metadata = {"stop_reason": None, "http_status": result.get("status") if type(result.get("status")) is int else None,
                             "response_fields": keys, "request_sent": sent}
            try:
                if result.get("kind") == "no_request":
                    raise Stop(2, "no_request")
                return site.decode_page(result)
            except Stop as error:
                self.metadata["stop_reason"] = error.reason
                if error.reason in ("timeout", "network") and attempt == 0 and not getattr(self.transport, "no_retry", False):
                    continue
                if error.reason in ("timeout", "network") and not getattr(self.transport, "no_retry", False):
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
        job = None
        try:
            for index, video in enumerate(videos):
                if self._stop.is_set():
                    break
                if index:
                    try:
                        asyncio.run(pacer.between_videos())
                    except Exception:
                        pass
                transport = self._make_transport(budget, pacer)
                transport.video_url = video.video_url
                transport.video = video
                job = CollectJob(video, self._locked_transport(transport, pacer), BATCH_LIMIT)
                try:
                    while not job.done and not self._stop.is_set():
                        more = asyncio.run(job.run_slice(1))
                        if not more:
                            break
                except Stop as error:
                    if job.metadata is None:
                        job.metadata = {"http_status": None, "response_fields": [], "request_sent": False}
                    job.metadata["stop_reason"] = error.reason
                    reason = pause_reason(error.reason)
                    if error.reason in PAUSE_STOPS:
                        self._halt(True, reason)
                        summaries.append(video.summary() if hasattr(video, "summary") else {"reason": reason})
                        self._store_summary(summaries)
                        self._export(videos, error.code, reason, job.metadata)
                        return
                    video.status, video.reason = "partial", error.reason
                summaries.append(video.summary() if hasattr(video, "summary") else {"status": video.status,
                                                                                   "reason": video.reason})
            self._store_summary(summaries)
            self._export(videos, 0 if not self._stop.is_set() else 2,
                         (self.status().get("pause_reason") or "complete"), job.metadata if job else None)
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
            no_retry = getattr(transport, "no_retry", False)

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

    def _export(self, videos, code, reason, metadata=None):
        from .output import export
        run_id = time.strftime("%Y%m%dT%H%M%S", time.localtime())
        out = self.state_dir / "runs" / run_id
        requests = sum(getattr(video, "pages", 0) for video in videos)
        result = export(out, run_id, videos, code, reason, requests, metadata=metadata)
        with self._guard:
            self._snapshot["output"] = {"run_id": run_id, "requests": requests,
                                        "videos": result.get("videos"), "metadata": metadata}

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
