"""Hub-owned Douyin comments probe and batched Qt collection.

Runs only inside the desktop process. The HTTP API never receives cookies.
Playwright is not used against the Qt webview profile.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from . import site
from .errors import Stop
from .models import Video
from .paging import ingest_page
from .runtime import BATCH_LIMIT, DAILY_LIMIT, Budget, DailyCounter, HubPacer, atomic_text, private_open

COMMENTS_BUSY = "正在采集评论，稍后重试"
INDEX_BUSY = "取数或核对正在进行，请等待。"
LOGIN_URL = "https://www.douyin.com/"
LOGIN_ENTRY = {"login_entry": "index_sms", "login_path": "/api/v1/login/sms"}
LOCK_TIMEOUT = 30
PROBE_TIMEOUT = 10
SHORT_LINK_TIMEOUT = 15
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
    modal_id: new URL(location.href).searchParams.getAll('modal_id').length === 1
      ? new URL(location.href).searchParams.get('modal_id') : null,
    account: visible('[data-e2e="user-avatar"]') || visible('[data-dyc-login="yes"]'),
    login: visible('#login-panel-new, #douyin_login_comp_flat_panel, [data-e2e="login-button"], [data-dyc-login="no"]'),
    verification: visible('[id*="captcha"], [class*="captcha-verify"], [data-e2e="verify-dialog"], iframe[src*="captcha"], iframe[src*="verify"], [data-dyc-risk="yes"]')
      || ['安全验证', '身份验证', '使用原设备'].some(t => text.includes(t))
  };
})())"""

MARK_RESOLVE_JS = "(token => (window.__dycResolveToken = token) === token)"
RESOLVE_JS = "(token => window.__dycResolveToken === token ? null : " + PROBE_JS + ")"

# One page request is admitted per scheduler slice; only its non-secret ticket survives navigation.
GUARD_JS = r"""(function(){
  if (window.__dycHubGuard) return;
  let hub = window;
  try { if (window.top.location.origin === location.origin) hub = window.top; } catch (e) {}
  const storageKey = '__dycPending';
  const ticket = () => {
    try { return JSON.parse(hub.sessionStorage.getItem(storageKey)) || {}; } catch (e) { return {}; }
  };
  if (!hub.__dycCapture) {
    const pending = ticket();
    hub.__dycCapture = {...pending, result:null, templates:{}};
  }
  const state = hub.__dycCapture;
  const read = () => {
    const pending = ticket();
    if (pending.epoch === state.epoch && pending.sent) state.sent = true;
    return {sent:!!state.sent, result:state.result};
  };
  const active = () => {
    read();
    const pending = ticket();
    return !!state.epoch && pending.epoch === state.epoch && pending.expires > Date.now() && !state.sent && !state.result;
  };
  const onTarget = () => {
    const page = new URL(hub.location.href);
    return hub === window.top && page.protocol === 'https:' && ['www.douyin.com', 'douyin.com'].includes(page.hostname) && !page.port &&
      page.pathname === '/video/' + state.video;
  };
  hub.__dycRead = read;
  hub.__dycBegin = cfg => {
    if (!/^\d{1,80}$/.test(cfg.video) || !/^\d*$/.test(cfg.root) || !/^\d+$/.test(cfg.cursor)) return false;
    const pending = {epoch:Date.now() + ':' + Math.random(), video:cfg.video, root:cfg.root,
                     cursor:cfg.cursor, expires:Date.now() + 28000, sent:false};
    try { hub.sessionStorage.setItem(storageKey, JSON.stringify(pending)); } catch (e) { return false; }
    if (state.video !== cfg.video) state.templates = {};
    Object.assign(state, pending, {result:null, replay:null});
    return true;
  };
  hub.__dycClose = () => {
    const snapshot = read();
    if (ticket().epoch === state.epoch) hub.sessionStorage.removeItem(storageKey);
    state.epoch = null;
    state.replay = null;
    return snapshot;
  };
  hub.__dycReplay = cfg => {
    if (!active() || !onTarget() || cfg.video !== state.video || cfg.root !== state.root || cfg.cursor !== state.cursor) return false;
    const source = state.templates[cfg.root ? 'reply' : 'list'];
    try {
      if (!source || source.frame.closed || source.frame.document !== source.document || !source.wrapped()) return false;
    } catch (e) { return false; }
    const target = new URL(source.url);
    const edits = new Map([[source.videoKey, cfg.video], ['cursor', cfg.cursor]]);
    if (cfg.root) edits.set('comment_id', cfg.root);
    // Drop stale signatures so the page SDK can resign; refresh msToken from xmst when present.
    const query = target.search.slice(1).split('&').map(part => {
      const key = new URLSearchParams(part).keys().next().value;
      if (['a_bogus', 'x-bogus', '_signature'].includes((key || '').toLowerCase())) return null;
      if (key === 'count' && !(cfg.root && Number(new URLSearchParams(part).get(key)) > 20)) {
        return part.split('=')[0] + '=20';
      }
      if ((key || '').toLowerCase() === 'mstoken') {
        let token = null;
        try { token = localStorage.getItem('xmst'); } catch (e) {}
        if (token) { edits.delete(key); return part.split('=')[0] + '=' + encodeURIComponent(token); }
      }
      if (!edits.has(key)) return part;
      const value = edits.get(key); edits.delete(key);
      return part.split('=')[0] + '=' + encodeURIComponent(value);
    }).filter(part => part != null);
    for (const [key, value] of edits) query.push(key + '=' + encodeURIComponent(value));
    target.search = query.join('&');
    const xhr = new source.frame.XMLHttpRequest();
    xhr.open('GET', target.href);
    if (!active()) return false;
    // Reserve before dispatch so an SDK replacement cannot send uncounted replays.
    const pending = ticket();
    pending.sent = true;
    hub.sessionStorage.setItem(storageKey, JSON.stringify(pending));
    state.sent = true;
    state.replay = xhr;
    xhr.__dycReplayEpoch = state.epoch;
    try { xhr.send(); }
    catch (e) { finish(state.epoch, {kind:'network'}); }
    return true;
  };
  function consume(method, url, body, xhr){
    let target;
    try { target = new URL(url, location.href); } catch (e) { return null; }
    const path = target.pathname;
    if (!path.includes('/comment/list/')) return null;
    const reply = path.slice(path.indexOf('/comment/list/') + '/comment/list/'.length).split('/').includes('reply');
    if (xhr && xhr.__dycReplayEpoch && xhr.__dycReplayEpoch !== state.epoch) return false;
    const reserved = xhr && state.replay === xhr && ticket().epoch === state.epoch && ticket().expires > Date.now();
    if (target.origin !== new URL(hub.location.href).origin || (!reserved && !active()) || !onTarget()) return false;
    const params = target.searchParams;
    if (['aweme_id', 'item_id', 'comment_id', 'cursor'].some(key => params.getAll(key).length > 1)) return false;
    const videoKeys = ['aweme_id', 'item_id'].filter(key => params.has(key));
    if (videoKeys.length !== 1 || params.get(videoKeys[0]) !== state.video) return false;
    if (state.root ? !reply || params.get('comment_id') !== state.root : reply || params.has('comment_id')) return false;
    // shortcut: only URL-parameter GET templates are replayable; add body handling after an official request is observed.
    if (String(method).toUpperCase() !== 'GET' || (body != null && body !== '')) {
      state.result = JSON.stringify({kind:'unsupported_request'});
      return false;
    }
    const kind = reply ? 'reply' : 'list';
    const previous = state.templates[kind];
    try {
      if (previous && (previous.frame.closed || previous.frame.document !== previous.document)) delete state.templates[kind];
    } catch (e) { delete state.templates[kind]; }
    state.templates[kind] ||= {url:target.href, videoKey:videoKeys[0], frame:window, document,
      wrapped:() => window.XMLHttpRequest !== xhrType || window.XMLHttpRequest.prototype.open !== guardedOpen ||
                    window.XMLHttpRequest.prototype.send !== guardedSend};
    if ((params.get('cursor') || '0') !== state.cursor) return false;
    const pending = ticket();
    pending.sent = true;
    try { hub.sessionStorage.setItem(storageKey, JSON.stringify(pending)); } catch (e) { return false; }
    state.sent = true;
    state.replay = null;
    return state.epoch;
  }
  const finish = (epoch, result) => {
    if (state.epoch === epoch && ticket().epoch === epoch) state.result = JSON.stringify(result);
  };
  const rawFetch = window.fetch;
  window.fetch = function(input, init){
    const signal = (init && init.signal) || (input && input.signal);
    if (signal && signal.aborted) return rawFetch.apply(this, arguments);
    const url = typeof input === 'string' ? input : input && (input.url || input.href);
    const epoch = consume((init && init.method) || (input && input.method) || 'GET', url,
                          init && init.body != null ? init.body : input && input.body);
    let promise;
    try { promise = rawFetch.apply(this, arguments); }
    catch (e) { if (epoch) finish(epoch, {kind:'network'}); throw e; }
    if (epoch) promise.then(r => {
      if (r.redirected || r.type === 'opaqueredirect') finish(epoch, {kind:'opaqueredirect', status:r.status});
      else r.clone().text().then(text => finish(epoch, {status:r.status, text}), () => finish(epoch, {kind:'network'}));
    }, () => finish(epoch, {kind:'network'}));
    return promise;
  };
  const xhrType = XMLHttpRequest;
  const open = XMLHttpRequest.prototype.open;
  const guardedOpen = XMLHttpRequest.prototype.open = function(method, url){
    this.__dycMethod = method;
    this.__dycUrl = url;
    return open.apply(this, arguments);
  };
  const send = XMLHttpRequest.prototype.send;
  const guardedSend = XMLHttpRequest.prototype.send = function(body){
    const epoch = consume(this.__dycMethod, this.__dycUrl, body, this);
    if (epoch) this.addEventListener('loadend', () => {
      try { finish(epoch, this.responseURL && new URL(this.responseURL).href !== new URL(this.__dycUrl, location.href).href
        ? {kind:'opaqueredirect', status:this.status} : {status:this.status, text:this.responseText}); }
      catch (e) { finish(epoch, {kind:'network'}); }
    }, {once:true});
    try { return send.apply(this, arguments); }
    catch (e) { if (epoch) finish(epoch, {kind:'network'}); throw e; }
  };
  window.__dycHubGuard = true;
})();"""

ARM_JS = """(cfg => !!(window.__dycHubGuard && window.__dycBegin && window.__dycBegin(cfg)))"""
READ_JS = "JSON.stringify(window.__dycRead ? window.__dycRead() : {})"
CLOSE_JS = "JSON.stringify(window.__dycClose ? window.__dycClose() : {})"
REPLAY_JS = "(cfg => !!(window.__dycReplay && window.__dycReplay(cfg)))"
REENTER_JS = "(location.reload(), true)"

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
  if (cfg.afterReload) { if (!box && button) button.click(); return true; }
  const state = window.__dycRead ? window.__dycRead() : {};
  if (state.sent || state.result) return true;
  if (!box) {
    if (!button || cfg.opened) return false;
    button.click();
    return "opened";
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


class InvalidVideoLines(ValueError):
    pass


class QtCommentTransport:
    """Capture one page-originated request per locked slice."""
    no_retry = True

    def __init__(self, window, budget, pacer, *, evaluate=None):
        self.window, self.budget, self.pacer = window, budget, pacer
        self._evaluate = evaluate or (lambda js, arg=None: _eval(window, js, arg))
        self.calls = 0
        self.video_id = None

    async def resolve(self, video):
        token = secrets.token_hex(16)
        if self._evaluate(MARK_RESOLVE_JS, token) is not True:
            raise Stop(2, "short_link_unresolved")
        self.window.load_url(video.video_url)
        deadline = time.monotonic() + SHORT_LINK_TIMEOUT
        while time.monotonic() < deadline:
            page = parse_probe_js(self._evaluate(RESOLVE_JS, token))
            if page and page.get("host"):
                url = f"{'https' if page.get('https') else 'http'}://{page.get('host', '')}{page.get('path', '/')}"
                if not site.trusted_video_landing(url):
                    raise Stop(2, "untrusted_video_redirect")
                if page.get("modal_id") is not None:
                    url += "?" + urlencode({site.MODAL_PARAM: page["modal_id"]})
                try:
                    target = site.parse_url(url)
                except Stop:
                    target = None
                if target and target.video_id and page.get("ready") == "complete":
                    video.video_id, video.video_url = target.video_id, target.url
                    return
            await asyncio.sleep(0.1)
        raise Stop(2, "short_link_unresolved")

    async def fetch(self, video_id, root, cursor):
        started = self.pacer.clock()
        if self.video_id != video_id:
            self.budget.new_video()
        if self.budget.used >= self.budget.limit:
            raise Stop(2, "batch_cap")
        if self.budget.video_used >= self.budget.video_limit:
            raise Stop(2, "video_page_budget")
        self.budget.counter.check_start()
        sent = False
        outcome = {}
        armed = False
        try:
            cfg = {"video": video_id, "root": root or "", "cursor": cursor}
            navigated = self.video_id != video_id
            if navigated:
                self.window.load_url(f"https://www.douyin.com/video/{video_id}")
                self.video_id = video_id
            acted = False
            opened = False
            reentered = False
            ready_at = None
            deadline = time.monotonic() + 28
            listen_deadline = deadline
            while (now := time.monotonic()) < min(deadline, listen_deadline):
                state = _coerce_fetch(self._evaluate(READ_JS)) if armed else {}
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
                    if reentered and sent and not result.get("kind"):
                        try:
                            site.decode_page(result)
                        except Stop:
                            pass
                        else:
                            if not (on_site and page.get("ready") == "complete" and page.get("path") == f"/video/{video_id}"):
                                await asyncio.sleep(0.1)
                                continue
                            self._evaluate(SCROLL_JS, {"root": "", "text": "", "nickname": "", "opened": False,
                                                       "afterReload": True})
                    result["request_sent"] = sent
                    outcome = result
                    return outcome
                target_ready = on_site and page.get("ready") == "complete" and page.get("path") == f"/video/{video_id}"
                if on_site and page.get("ready") == "complete" and not target_ready and not navigated:
                    self.window.load_url(f"https://www.douyin.com/video/{video_id}")
                    navigated = True
                if target_ready:
                    if not armed:
                        self._evaluate(GUARD_JS)
                        if self._evaluate(ARM_JS, cfg) is not True:
                            raise RuntimeError("Comment guard could not be armed")
                        armed = True
                    if ready_at is None:
                        ready_at = now
                        listen_deadline = min(deadline, now + 20)
                    if not sent:
                        if self._evaluate(REPLAY_JS, cfg) is True:
                            acted = True
                            continue
                    if not acted and not sent:
                        comment = getattr(self, "video", None)
                        comment = comment.comments.get(root) if comment and root else None
                        action = self._evaluate(SCROLL_JS, {"root": root or "", "text": comment.text if comment else "",
                                                          "nickname": comment.nickname if comment else "", "opened": opened})
                        opened = opened or action == "opened"
                        acted = action is True
                    if not sent and not reentered and root is None and cursor == "0" and now - ready_at >= 5:
                        reentered = True
                        if self._evaluate(REENTER_JS) is True:
                            acted = opened = False
                            ready_at = None
                            listen_deadline = deadline
                await asyncio.sleep(0.1)
            outcome = {"kind": "timeout" if sent else "no_request", "request_sent": sent}
            return outcome
        finally:
            state = _coerce_fetch(self._evaluate(CLOSE_JS))
            if not armed:
                state = {}
            if not sent and state.get("sent"):
                self.budget.reserve()
                self.calls += 1
                sent = True
                outcome["request_sent"] = True
                if outcome.get("kind") == "no_request":
                    outcome.clear()
                    outcome.update(_coerce_fetch(state["result"]) if state.get("result") else {"kind": "timeout"})
                    outcome["request_sent"] = True
            if sent:
                self.pacer.completed(started)


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
                raise Stop(2, "video_page_budget")
            root, cursor = self.queue.pop(0)
            decoded = await self._page(root, cursor)
            started += 1
            comments, next_cursor, more, _ = decoded
            self.video.record_page(root, decoded)
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
                raise Stop(2, "video_page_budget")
            take_seed = getattr(self.transport, "take_seed", None)
            seed = take_seed(self.video.video_id, root, cursor) if take_seed else None
            if seed is not None:
                self.used += 1
                self.video.record_request(root, retry=attempt > 0)
                return seed
            result = await self.transport.fetch(self.video.video_id, root, cursor)
            sent = result.get("request_sent", True)
            if sent:
                self.used += 1
                self.video.record_request(root, retry=attempt > 0)
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
                if result.get("kind") in ("no_request", "unsupported_request"):
                    raise Stop(2, result["kind"])
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
        self._progress_source = None
        self._snapshot = {
            "probe": None,
            "running": False,
            "paused": False,
            "pause_reason": None,
            "busy": False,
            "waiting_for_index": False,
            "summary": None,
            "output": None,
            "progress": None,
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

    def _update_progress(self):
        with self._guard:
            if self._progress_source is not None:
                budget, videos = self._progress_source
                self._snapshot["progress"] = dict(
                    self._snapshot["progress"], requests=budget.used,
                    top_items=sum(video.top_items for video in videos),
                    reply_items=sum(video.reply_items for video in videos))

    def status(self):
        self._update_progress()
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
        except InvalidVideoLines as error:
            lines = error.args[0]
            return {"ok": False, "code": "invalid_parameters", "invalid_lines": lines,
                    "message": "第 " + "、".join(map(str, lines)) + " 行无法识别为抖音视频链接。"
                    "支持：视频页链接、v.douyin.com 短链、App 分享口令、iesdouyin 分享页；不支持纯数字 ID 和图文。"}
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
                                  waiting_for_index=False, summary=None, output=None, progress=None)
            self._save_pause()
        counter = DailyCounter(self.state_dir, limit=DAILY_LIMIT)
        try:
            counter.check_start()
        except Stop as error:
            self._halt(False, pause_reason(error.reason))
            return {"ok": False, "code": error.reason, **self.status()}
        used_budget = budget or Budget(counter, batch_limit=self._limit(payload, "batch_limit", BATCH_LIMIT),
                                       video_limit=BATCH_LIMIT)
        used_pacer = pacer or HubPacer()
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
        with self._guard:
            self._progress_source = (budget, videos)
            self._snapshot["progress"] = dict(
                started_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
                requests=budget.used, batch_limit=budget.limit, top_items=0, reply_items=0,
                video_index=0, video_total=len(videos), finished_at=None)
        known_ids = {video.video_id for video in videos if video.video_id}
        try:
            for index, video in enumerate(videos):
                if self._stop.is_set():
                    break
                with self._guard:
                    self._snapshot["progress"] = dict(self._snapshot["progress"], video_index=index + 1)
                if index:
                    try:
                        asyncio.run(pacer.between_videos())
                    except Exception:
                        pass
                remaining = len(videos) - index
                budget.video_limit = (budget.limit - budget.used + remaining - 1) // remaining
                budget.new_video()
                transport = self._make_transport(budget, pacer)
                locked = self._locked_transport(transport, pacer)
                if not video.video_id:
                    try:
                        asyncio.run(locked.resolve(video))
                    except Stop as error:
                        video.status, video.reason = "partial", error.reason
                        summaries.append(video.summary())
                        continue
                    if video.video_id in known_ids:
                        video.status, video.reason = "duplicate", "deduplicated"
                        summaries.append(video.summary())
                        continue
                    known_ids.add(video.video_id)
                transport.video_url = video.video_url
                transport.video = video
                job = CollectJob(video, locked, budget.video_limit)
                try:
                    while not job.done and not self._stop.is_set():
                        more = asyncio.run(job.run_slice(1))
                        self._update_progress()
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
        finally:
            self._update_progress()
            with self._guard:
                self._snapshot["progress"] = dict(
                    self._snapshot["progress"],
                    finished_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"))
                self._progress_source = None

    def _locked_transport(self, transport, pacer):
        hub = self

        class LockedTransport:
            no_retry = getattr(transport, "no_retry", False)

            async def fetch(self, video_id, root, cursor):
                await pacer.before()
                return await self._call(lambda: transport.fetch(video_id, root, cursor))

            async def resolve(self, video):
                return await self._call(lambda: transport.resolve(video))

            async def _call(self, action):
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
                    return await hub._timed(action())
                finally:
                    with hub._guard:
                        hub._snapshot.update(waiting_for_index=False, busy=False)
                    if acquired and hub._acquire is not None and hub._release is not None:
                        hub._release()
                    if acquired:
                        await asyncio.sleep(0.05)

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
        videos = [video for video in videos if video.status != "duplicate"]
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
        invalid_lines = []
        for line, item in enumerate(raw, 1):
            if not isinstance(item, str):
                invalid_lines.append(line)
                continue
            try:
                target = site.parse_url(site.extract_share_url(item))
            except Stop:
                invalid_lines.append(line)
                continue
            videos.append(Video(target.video_id or "", target.url))
        if invalid_lines:
            raise InvalidVideoLines(invalid_lines)
        return videos

    def _limit(self, payload, key, default):
        value = (payload or {}).get(key, default)
        if type(value) is not int or isinstance(value, bool) or value < 1:
            return default
        return min(value, default)
