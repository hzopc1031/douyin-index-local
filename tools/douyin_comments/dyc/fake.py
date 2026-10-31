"""Synthetic offline origin. This is the ONLY place fast pacing is constructed."""

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

from . import site
from .runtime import Pacer


class FastPacer(Pacer):
    async def before(self):
        await asyncio.sleep(0)

    def completed(self):
        self.count += 1

    async def between_videos(self):
        await asyncio.sleep(0)


def fixture_comment(cid, replies=0, text=None):
    f = site.FIELDS
    return {f["id"]: str(cid), f["text"]: text or "合成评论 😀\n第二行",
            f["created"]: 1700000000, f["likes"]: cid % 13,
            f["reply_count"]: replies,
            f["user"]: {f["uid"]: "990000000000000001", f["nickname"]: "仅CSV合成昵称"},
            f["ip"]: "仅CSV合成属地"}


def fixture_page(comments, cursor, more=False, total=None):
    f = site.FIELDS
    return {f["status"]: 0, f["comments"]: comments, f["cursor"]: cursor,
            f["more"]: int(more), f["total"]: total}


class FakeSite:
    def __init__(self, scenario="normal", *, reply_seed=False):
        self.scenario = scenario
        self.reply_seed = reply_seed
        self.calls = []
        self.foreign = []
        self.navigations = []

    def allowed(self, url):
        allowed = urlsplit(url).netloc == urlsplit(site.FAKE_ORIGIN).netloc
        if not allowed:
            self.foreign.append(urlsplit(url).hostname)
        return allowed

    async def deliver(self, route):
        url = urlsplit(route.request.url)
        if not self.allowed(route.request.url):
            await route.abort()
            return
        kind = site.request_kind(route.request.url)
        if not kind:
            if route.request.is_navigation_request():
                self.navigations.append(url.path)
            if url.path.startswith("/short/"):
                # A client navigation is routed independently. Playwright can
                # follow a fulfilled HTTP redirect outside its route callback.
                await route.fulfill(status=200, content_type="text/html",
                                    body='<script>location.replace("/video/100")</script>')
            else:
                vid = url.path.rsplit("/", 1)[-1] if url.path.startswith("/video/") else None
                await route.fulfill(status=200, content_type="text/html", body=self.html(vid))
            return
        params = {key: values[0] for key, values in parse_qs(url.query).items()}
        self.calls.append((kind, params))
        number = len(self.calls)
        if (self.scenario in ("timeout", "captcha-mid") and number > 1) or (self.scenario == "hang" and number > 2):
            await asyncio.sleep(60 if self.scenario == "hang" else 0.6)
        if self.scenario == "invalid-json" and number > 1:
            await route.fulfill(status=200, body="not-json")
            return
        vid = int(params.get(site.PARAM["video"], "100"))
        base, cursor = vid * 100, int(params.get(site.PARAM["cursor"], "0"))
        if kind == "top":
            comments = [fixture_comment(base + 1, 21)] if cursor == 0 else [fixture_comment(base + 2)]
            data = fixture_page(comments, 7 if cursor == 0 else 19, cursor == 0, 23)
        else:
            start = 0 if cursor == 0 else 20
            comments = [fixture_comment(base + 100 + i) for i in range(start, min(start + 20, 21))]
            data = fixture_page(comments, 41 if start == 0 else 88, start == 0, 21)
        if self.scenario == "partial" and number > 1:
            data[site.FIELDS["cursor"]] = 0
        if self.scenario == "nonzero" and number > 1:
            data[site.FIELDS["status"]] = 7
        if self.scenario == "login-api" and number > 1:
            data[site.FIELDS["status"]] = site.LOGIN_CODES[0]
        await route.fulfill(status=200, content_type="application/json", body=json.dumps(data, ensure_ascii=False))

    def html(self, video_id):
        login = "no" if self.scenario in ("login-expired", "login-after") else "yes"
        risk = '<div data-dyc-risk="yes">synthetic challenge</div>' if self.scenario == "captcha-before" else ""
        config = dict(origin=site.FAKE_ORIGIN, top=site.TOP_PATH, reply=site.REPLY_PATH,
                      params=site.PARAM, storage=site.TOKEN_STORAGE,
                      signatures=site.SIGNATURES, vid=video_id, scenario=self.scenario,
                      replySeed=self.reply_seed)
        return """<!doctype html><meta charset="utf-8">
<div data-dyc-login="%s">synthetic account state</div>%s
<div data-e2e="video-desc">合成视频</div><div data-e2e="comment-count">23</div>
<canvas data-e2e="login-qr-code" width="120" height="120"></canvas>
<script>
const cfg = %s;
window.sdkCalls = []; window.abortCount = 0;
const nativeOpen = XMLHttpRequest.prototype.open;
const nativeSend = XMLHttpRequest.prototype.send;
const nativeAbort = XMLHttpRequest.prototype.abort;
XMLHttpRequest.prototype.open = function(method, input, ...rest) {
  const u = new URL(input, location.href);
  window.sdkCalls.push({url: u.href, method});
  u.searchParams.set(cfg.signatures[0], 'synthetic-sdk-signature');
  return nativeOpen.call(this, method, u.href, ...rest);
};
XMLHttpRequest.prototype.send = function(...args) {
  window.sdkCalls[window.sdkCalls.length - 1].withCredentials = this.withCredentials;
  if (cfg.scenario === 'captcha-mid' && window.sdkCalls.length > 1) {
    setTimeout(() => {
      const challenge = document.createElement('iframe');
      challenge.srcdoc = '<div data-dyc-risk="yes">synthetic iframe challenge</div>';
      document.body.appendChild(challenge);
    }, 50);
  }
  return nativeSend.apply(this, args);
};
XMLHttpRequest.prototype.abort = function(...args) {
  window.abortCount++;
  return nativeAbort.apply(this, args);
};
if (cfg.scenario === 'login-after') {
  setTimeout(() => document.querySelector('[data-dyc-login]').setAttribute('data-dyc-login', 'yes'), 800);
}
if (cfg.vid) {
  localStorage.setItem(cfg.storage, 'synthetic-old-token');
  const u = new URL(cfg.origin + cfg.top);
  u.searchParams.set(cfg.params.video, cfg.vid);
  u.searchParams.set(cfg.params.cursor, '0');
  u.searchParams.set(cfg.params.count, '20');
  u.searchParams.set(cfg.params.token, 'synthetic-old-token');
  for (const key of cfg.signatures) u.searchParams.set(key, 'synthetic-stale-signature');
  const seed = new XMLHttpRequest();
  seed.open('GET', u.href);
  seed.onload = () => localStorage.setItem(cfg.storage, 'synthetic-fresh-token');
  seed.send();
  if (cfg.replySeed) {
    u.pathname = cfg.reply;
    u.searchParams.set(cfg.params.root, String(Number(cfg.vid) * 100 + 1));
    const reply = new XMLHttpRequest(); reply.open('GET', u.href); reply.send();
  }
}
</script>""" % (login, risk, json.dumps(config))
