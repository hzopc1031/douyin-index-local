"""Owned headed Chromium, event-captured templates, page-context XHR only."""

import asyncio
from dataclasses import dataclass, field
import os
from pathlib import Path
import secrets
import signal
from urllib.parse import parse_qs, urlsplit

from . import site
from .errors import Stop
from .paging import ingest_page
from .runtime import Display, Profile, browser_defaults, check_memory

REPLAY_HEADER = "x-dyc-replay"
STATE_TIMEOUT_SECONDS = 2

STATE_JS = """s => {
  const visible = q => [...document.querySelectorAll(q)].some(e =>
    e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
  return {risk: s.captcha.some(visible), out: s.logged_out.some(visible),
          inside: s.logged_in.some(visible)};
}"""

XHR_JS = """cfg => new Promise(resolve => {
  let xhr, timer, observer, finished = false;
  const visible = q => [...document.querySelectorAll(q)].some(e =>
    e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
  const state = () => cfg.signals.captcha.some(visible) ? 'risk' :
                     cfg.signals.logged_out.some(visible) ? 'login' : null;
  const finish = value => {
    if (finished) return;
    finished = true;
    clearTimeout(timer);
    if (observer) observer.disconnect();
    window.__dycAbort = null;
    resolve(value);
  };
  const abort = kind => {
    finish({kind});
    if (xhr) xhr.abort();
  };
  const initial = state();
  if (initial) { finish({kind: initial}); return; }
  try {
    const url = new URL(cfg.url);
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
    xhr = new XMLHttpRequest();
    window.__dycAbort = () => abort('cancelled');
    xhr.open('GET', url.href, true);
    xhr.withCredentials = true;
    for (const [key, value] of Object.entries(cfg.headers)) xhr.setRequestHeader(key, value);
    xhr.setRequestHeader(cfg.markerHeader, cfg.marker);
    xhr.onload = () => finish({status: xhr.status, text: xhr.responseText});
    xhr.onerror = () => finish({kind: 'network'});
    xhr.onabort = () => finish({kind: 'cancelled'});
    timer = setTimeout(() => abort('timeout'), cfg.timeout);
    observer = new MutationObserver(() => {
      const reason = state();
      if (reason) abort(reason);
    });
    observer.observe(document.documentElement, {subtree: true, childList: true, attributes: true});
    xhr.send();
  } catch (_) { abort('network'); }
})"""


@dataclass(repr=False)
class Template:
    url: str = field(repr=False)
    headers: dict = field(repr=False)


def _processes(root):
    """PID + birth time for descendants of our Playwright driver only (Linux)."""
    records = {}
    for item in Path("/proc").glob("[0-9]*/stat"):
        try:
            text = item.read_text()
            fields = text[text.rfind(")") + 2:].split()
            records[int(item.parent.name)] = (int(fields[1]), fields[19])
        except (OSError, ValueError, IndexError):
            continue
    found = {root}
    while True:
        more = {pid for pid, (parent, _) in records.items() if parent in found}
        if more <= found:
            break
        found.update(more)
    return {pid: records[pid][1] for pid in found if pid in records}


def _kill_owned(records):
    for pid, born in records.items():
        try:
            text = Path(f"/proc/{pid}/stat").read_text()
            if text[text.rfind(")") + 2:].split()[19] == born:
                os.kill(pid, signal.SIGKILL)
        except (OSError, IndexError):
            pass


class Browser:
    def __init__(self, profile, control, budget, pacer, *, display=None,
                 backend=None, memory_minimum=2 * 1024**3, timeout_ms=20000,
                 skip_video_ids=()):
        self.profile = Profile(profile)
        self.control, self.budget, self.pacer = control, budget, pacer
        self.display = Display(display)
        self.backend = backend
        self.memory_minimum, self.timeout_ms = memory_minimum, timeout_ms
        self.request_timeout = timeout_ms / 1000 + 2
        # Keep the runner's set by reference as it records processed videos.
        self.skip_video_ids = skip_video_ids
        self.context = self.page = self.playwright = None
        self.response_session = None
        self.driver_pid = None
        self.gate = asyncio.Lock()
        self.tasks = set()
        self.templates = {}
        self.accepted = {}
        self.active = {}
        self.seeded = set()
        self.seed_pages = {}
        self.seed_ready = asyncio.Event()
        self.armed = self.seeding = False
        self.expected = None
        self.watcher = None
        self.video_id = None
        self.closing = False

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def __aenter__(self):
        check_memory(self.memory_minimum)
        self.profile.__enter__()
        try:
            await self.control.run(self.display.start())
            browser_defaults()
            from playwright.async_api import async_playwright
            self.playwright = await self.control.run(async_playwright().start())
            # Private driver handle is used only for emergency cleanup, never data acquisition.
            self.driver_pid = self.playwright._impl_obj._connection._transport._proc.pid
            self.context = await self.control.run(self.playwright.chromium.launch_persistent_context(
                str(self.profile.path), headless=False, env={**os.environ, **self.display.environment()},
                handle_sigint=False, handle_sigterm=False, handle_sighup=False,
                service_workers="block", accept_downloads=False,
                args=["--disable-background-networking", "--disable-component-update",
                      "--disable-sync", "--no-first-run", "--disable-crash-reporter"],
            ))
            # Installed before ANY navigation. Test backend fulfils in memory and
            # rejects every other origin. No Python HTTP or local receiver exists.
            await self.context.route("**/*", self.route)
            await self.context.route_web_socket("**/*", lambda socket: socket.close())
            self.context.on("request", self.on_request)
            self.context.on("response", lambda response: self.spawn(self.on_response(response)))
            self.context.on("requestfinished", self.on_finished)
            self.context.on("requestfailed", self.on_finished)
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            self.page.set_default_timeout(5000)
            # Playwright routes only the first hop of a redirected request.
            # Pause comment responses inside Chromium before it can follow a
            # Location header, for both page seeds and collector replays. Keep
            # the page's signed XHR and allow ordinary short-link navigation.
            self.response_session = await self.control.run(self.context.new_cdp_session(self.page))
            self.response_session.on("Fetch.requestPaused", lambda event: self.spawn(self.guard_comment_response(event)))
            await self.control.run(self.response_session.send("Fetch.enable", dict(patterns=[
                dict(urlPattern="*" + path + "*", requestStage="Response")
                for path in (site.TOP_PATH, site.REPLY_PATH)
            ])))
            self.watcher = self.spawn(self.watch())
            return self
        except BaseException as error:
            await self.close()
            if isinstance(error, (Stop, asyncio.CancelledError, KeyboardInterrupt)):
                raise
            raise Stop(5, "browser_start_failed") from None

    async def __aexit__(self, *_):
        await self.close()

    def on_request(self, request):
        key = self.request_key(request)
        if self.armed and self.seeding and key and key[2] not in self.templates:
            headers = {key: value for key, value in request.headers.items()
                       if key.lower() in site.REPLAY_HEADERS}
            self.templates[key[2]] = Template(request.url, headers)

    async def guard_comment_response(self, event):
        request_id = event["requestId"]
        if event.get("responseErrorReason"):
            # Aborted/failed requests have no HTTP response or redirect to
            # inspect. Navigation may already have discarded their CDP id.
            try:
                await self.response_session.send("Fetch.failRequest", dict(
                    requestId=request_id, errorReason=event["responseErrorReason"]))
            except Exception:
                pass
            return
        try:
            if 300 <= event.get("responseStatusCode", 0) < 400:
                # Stop before failing the XHR, so its network-error handler
                # cannot retry. The original admission still consumes budget
                # and completes pacing; no redirect hop is ever admitted.
                self.control.stop(3, "comment_redirect")
                await self.response_session.send("Fetch.failRequest", dict(
                    requestId=request_id, errorReason="BlockedByClient"))
            else:
                await self.response_session.send("Fetch.continueRequest", dict(requestId=request_id))
        except Exception:
            if not self.closing:
                self.control.stop(3, "comment_redirect_guard_failed")
            try:
                await self.response_session.send("Fetch.failRequest", dict(
                    requestId=request_id, errorReason="BlockedByClient"))
            except Exception:
                pass

    def request_key(self, request):
        """Only unambiguous GETs for the intended video's main-frame origin."""
        try:
            if request.method != "GET" or request.frame != self.page.main_frame:
                return None
            url, frame = urlsplit(request.url), urlsplit(request.frame.url)
            hosts = {urlsplit(site.FAKE_ORIGIN).hostname} if self.backend else site.HOSTS
            origin = (url.scheme, url.hostname, url.port or 443)
            if (origin != (frame.scheme, frame.hostname, frame.port or 443)
                    or url.scheme != "https" or url.hostname not in hosts
                    or url.port not in (None, 443) or url.username or url.password):
                return None
            kind = {site.TOP_PATH: "top", site.REPLY_PATH: "reply"}.get(url.path)
            if kind is None:
                return None
            params = parse_qs(url.query, keep_blank_values=True)
            for name in ("video", "item", "root", "cursor"):
                values = params.get(site.PARAM[name], [])
                if len(values) > 1 or (values and not values[0].isascii()) or (values and not values[0].isdigit()):
                    return None
            video = params.get(site.PARAM["video"], params.get(site.PARAM["item"], [None]))[0]
            item = params.get(site.PARAM["item"], [video])[0]
            current = site.parse_url(request.frame.url, fake=self.backend is not None).video_id
            if not video or item != video or video != (self.video_id or current) or video != current:
                return None
            root = params.get(site.PARAM["root"], [None])[0]
            if (kind == "reply") != bool(root):
                return None
            cursor = params.get(site.PARAM["cursor"], ["0"])[0]
            return origin, video, kind, root, cursor
        except (ValueError, Stop):
            return None

    async def on_response(self, response):
        request = response.request
        accepted = self.accepted.get(request)
        if not accepted or accepted[0] != "seed":
            return
        try:
            decoded = site.decode_page({"status": response.status, "text": await response.text()})
            _, video, kind, root, cursor = accepted[1]
            self.seed_pages[video, root, cursor] = decoded
            if kind == "top":
                self.seed_ready.set()
        except Stop as error:
            self.control.stop(error.code, error.reason)
        except Exception:
            if not self.closing:
                self.control.stop(3, "seed_response_unavailable")
        finally:
            event = self.active.get(request)
            if event:
                event.set()

    def on_finished(self, request):
        event = self.active.get(request)
        accepted = self.accepted.get(request)
        # A seed isn't settled until its body has been decoded and retained.
        if event and (not accepted or accepted[0] != "seed" or request.failure):
            event.set()

    async def route(self, route):
        try:
            await self._route(route)
        except Exception:
            if not self.closing:
                self.control.stop(1, "request_route_error")
            try:
                await route.abort()
            except Exception:
                pass

    async def _route(self, route):
        request = route.request
        if self.backend and not self.backend.allowed(request.url):
            await route.abort()
            return
        kind = site.request_kind(request.url)
        if not kind:
            if self.control.event.is_set():
                await route.abort()
            elif self.backend:
                await self.backend.deliver(route)
            else:
                await route.continue_()
            return
        async with self.gate:
            if not self.armed or self.control.event.is_set():
                await route.abort()
                return
            actual = self.request_key(request)
            marker = request.headers.get(REPLAY_HEADER)
            if actual is None:
                await route.abort()
                return
            if self.expected is not None and self.expected == (actual, marker):
                self.expected = None
                admission = "replay"
            elif (self.seeding and not marker and kind not in self.seeded
                  and actual[1] not in self.skip_video_ids):
                self.seeded.add(kind)
                admission = "seed"
            else:
                # Block page auto-pagination: all further work is explicit/serial.
                await route.abort()
                return
            self.video_id = actual[1]
            try:
                await self.control.run(self.pacer.before())
                state = await self.state()
                if state["risk"]:
                    raise Stop(3, "verification_widget")
                if state["out"]:
                    raise Stop(4, "login_expired")
                self.control.check()
                self.budget.reserve()
            except Stop as error:
                self.control.stop(error.code, error.reason)
                await route.abort()
                return
            done = asyncio.Event()
            self.active[request] = done
            self.accepted[request] = admission, actual
            try:
                async with asyncio.timeout(self.request_timeout):
                    if self.backend:
                        await self.backend.deliver(route)
                    else:
                        # This one-use marker is local admission metadata, never
                        # a header sent to the site or retained in a template.
                        await route.continue_(headers={key: value for key, value in request.headers.items()
                                                       if key.lower() != REPLAY_HEADER})
                    await done.wait()
            except asyncio.TimeoutError:
                self.control.stop(3, "unsettled_request")
            except Exception:
                # The caller's XHR returns the network failure and owns its one retry.
                pass
            finally:
                self.pacer.completed()
                self.active.pop(request, None)
                self.accepted.pop(request, None)

    async def state(self, *, deadline=None):
        self.control.check()
        combined = dict(risk=False, out=False, inside=False)
        if not self.page:
            return combined
        end = asyncio.get_running_loop().time() + STATE_TIMEOUT_SECONDS
        if deadline is not None:
            end = min(end, deadline)
        try:
            # One deadline for the entire scan, including child frames. A
            # stalled renderer must not bypass risk checks or hold up shutdown.
            async with asyncio.timeout_at(end):
                for frame in self.page.frames:
                    try:
                        result = await self.control.run(frame.evaluate(STATE_JS, site.SIGNALS))
                        for key in combined:
                            combined[key] |= result[key]
                    except Stop:
                        raise
                    except Exception:
                        # Navigation can detach a frame; preflight still requires a positive signal.
                        continue
        except asyncio.TimeoutError:
            self.control.stop(3, "state_check_timeout")
            self.control.check()
        return combined

    async def watch(self):
        while not self.closing:
            try:
                state = await self.state()
            except Stop:
                await self.abort()
                return
            if state["risk"]:
                self.control.stop(3, "verification_widget")
            if self.armed and state["out"]:
                self.control.stop(4, "login_expired")
            if self.control.event.is_set():
                await self.abort()
                return
            await asyncio.sleep(0.1)

    async def abort(self):
        if self.page:
            try:
                await asyncio.wait_for(self.page.evaluate(
                    "() => { if (window.__dycAbort) window.__dycAbort(); }"), 0.3)
            except Exception:
                pass

    async def preflight(self, url=None, wait_seconds=5):
        await self.control.run(self.page.goto(url or site.LOGIN_URL, wait_until="domcontentloaded"))
        end = asyncio.get_running_loop().time() + wait_seconds
        while True:
            self.control.check()
            if asyncio.get_running_loop().time() >= end:
                raise Stop(4, "login_signal_missing")
            state = await self.state(deadline=end)
            if state["risk"]:
                raise Stop(3, "verification_widget")
            if state["out"]:
                raise Stop(4, "login_missing")
            if state["inside"]:
                return
            if asyncio.get_running_loop().time() >= end:
                raise Stop(4, "login_signal_missing")
            await self.control.run(asyncio.sleep(0.1))

    async def finish_seeding(self):
        # FIFO gate: admitted/queued seeds settle before admission closes.
        # Later page requests see seeding=False and cannot outlive this video.
        async with self.gate:
            self.seeding = False

    async def open_video(self, url):
        self.templates.clear()
        self.seeded.clear()
        self.seed_pages.clear()
        self.seed_ready.clear()
        self.video_id = site.parse_url(url, fake=self.backend is not None).video_id
        self.budget.new_video()
        self.armed = self.seeding = True
        try:
            await self.control.run(self.page.goto(url, wait_until="domcontentloaded"))
            resolved = site.parse_url(self.page.url, fake=self.backend is not None)
            if not resolved.video_id:
                for _ in range(150):
                    await self.control.run(asyncio.sleep(0.1))
                    resolved = site.parse_url(self.page.url, fake=self.backend is not None)
                    if resolved.video_id:
                        break
                else:
                    raise Stop(2, "short_link_unresolved")
            self.video_id = resolved.video_id
            if self.video_id in self.skip_video_ids:
                return resolved, "", ""
            try:
                await self.control.run(asyncio.wait_for(self.seed_ready.wait(), self.request_timeout))
            except asyncio.TimeoutError:
                # A seed admitted late still has its own bounded request time.
                # Settle it before deciding whether to skip or collect.
                pass
        finally:
            try:
                await self.control.run(self.finish_seeding())
            finally:
                self.seeding = False
        if not self.seed_ready.is_set():
            raise Stop(2, "comment_template_missing")
        async def text_at(selector):
            locator = self.page.locator(selector)
            return await locator.first.text_content() if await locator.count() else ""
        title = await self.control.run(text_at(site.SIGNALS["title"]))
        displayed = await self.control.run(text_at(site.SIGNALS["count"]))
        return resolved, title or "", displayed or ""

    def take_seed(self, video_id, root, cursor):
        return self.seed_pages.pop((video_id, root, cursor), None)

    def seed_cursors(self, video_id, root):
        """Snapshot received pages for this thread, still owned by the transport."""
        return tuple(cursor for video, thread, cursor in self.seed_pages
                     if video == video_id and thread == root)

    def preserve_seeds(self, video, *, consume=True):
        """Drain received pages even if navigation/metadata or collection stopped."""
        if not video.video_id and self.video_id:
            video.video_id = self.video_id
            video.video_url = site.parse_url(self.page.url, fake=self.backend is not None).url
        # Roots first, then reply seeds, regardless of response event timing.
        for key in sorted(self.seed_pages, key=lambda key: key[1] is not None):
            video_id, root, _ = key
            if video_id == video.video_id:
                ingest_page(video, root, self.seed_pages[key])
                if consume:
                    self.seed_pages.pop(key)
                    video.pages += 1

    async def fetch(self, video_id, root, cursor):
        self.control.check()
        # Let the prior request settle and pace BEFORE starting the XHR timer.
        # The route repeats this guard for unsolicited seed requests.
        await self.control.run(self.gate.acquire())
        try:
            await self.control.run(self.pacer.before())
        finally:
            self.gate.release()
        kind = "reply" if root else "top"
        template = self.templates.get(kind) or self.templates.get("top")
        if template is None:
            raise Stop(2, "comment_template_missing")
        origin = urlsplit(template.url)
        key = (origin.scheme, origin.hostname, origin.port or 443), video_id, kind, root, cursor
        marker = secrets.token_hex(16)
        self.expected = key, marker
        try:
            result = await self.control.run(self.page.evaluate(XHR_JS, dict(
                url=template.url, headers=template.headers, video=video_id, root=root,
                cursor=cursor, path=site.REPLY_PATH if root else site.TOP_PATH,
                params=site.PARAM, signatures=site.SIGNATURES, storage=site.TOKEN_STORAGE,
                signals=site.SIGNALS, timeout=self.timeout_ms,
                markerHeader=REPLAY_HEADER, marker=marker,
            )))
        finally:
            self.expected = None
        if result.get("kind") in ("risk", "login"):
            self.control.stop(3 if result["kind"] == "risk" else 4,
                              "verification_widget" if result["kind"] == "risk" else "login_expired")
        self.control.check()
        return result

    async def close(self):
        if self.closing:
            return
        self.closing = True
        records = _processes(self.driver_pid) if self.driver_pid else {}
        await self.abort()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.context:
            try:
                await asyncio.wait_for(self.context.close(), 2.5)
            except Exception:
                pass
        if self.playwright:
            try:
                await asyncio.wait_for(self.playwright.stop(), 1)
            except Exception:
                pass
        # Only exact process identities from our own driver tree are eligible.
        # Also reap stragglers if a driver reported successful shutdown early.
        _kill_owned(records)
        await self.display.close()
        self.profile.__exit__(None, None, None)
        self.templates.clear()
        self.seed_pages.clear()
