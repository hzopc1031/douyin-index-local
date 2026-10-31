"""Owned headed Chromium, event-captured templates, page-context XHR only."""

import asyncio
from dataclasses import dataclass, field
import os
from pathlib import Path
import signal
from urllib.parse import parse_qs, urlsplit

from . import site
from .errors import Stop
from .runtime import Display, Profile, browser_defaults, check_memory


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
                 backend=None, memory_minimum=2 * 1024**3, timeout_ms=20000):
        self.profile = Profile(profile)
        self.control, self.budget, self.pacer = control, budget, pacer
        self.display = Display(display)
        self.backend = backend
        self.memory_minimum, self.timeout_ms = memory_minimum, timeout_ms
        self.context = self.page = self.playwright = None
        self.driver_pid = None
        self.gate = asyncio.Lock()
        self.tasks = set()
        self.templates = {}
        self.accepted = {}
        self.active = {}
        self.seeded = set()
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
            name = await self.control.run(self.display.start())
            browser_defaults()
            from playwright.async_api import async_playwright
            self.playwright = await self.control.run(async_playwright().start())
            # Private driver handle is used only for emergency cleanup, never data acquisition.
            self.driver_pid = self.playwright._impl_obj._connection._transport._proc.pid
            self.context = await self.control.run(self.playwright.chromium.launch_persistent_context(
                str(self.profile.path), headless=False, env={**os.environ, "DISPLAY": name},
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
            self.watcher = self.spawn(self.watch())
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *_):
        await self.close()

    def on_request(self, request):
        kind = site.request_kind(request.url)
        if self.armed and kind and kind not in self.templates:
            headers = {key: value for key, value in request.headers.items()
                       if key.lower() in site.REPLAY_HEADERS}
            self.templates[kind] = Template(request.url, headers)

    async def on_response(self, response):
        request = response.request
        if self.accepted.get(request) != "seed":
            return
        try:
            site.decode_page({"status": response.status, "text": await response.text()})
            if site.request_kind(request.url) == "top":
                self.seed_ready.set()
        except Stop as error:
            self.control.stop(error.code, error.reason)
        except Exception:
            if not self.closing:
                self.control.stop(3, "seed_response_unavailable")

    def on_finished(self, request):
        event = self.active.get(request)
        if event:
            event.set()

    async def route(self, route):
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
            params = parse_qs(urlsplit(request.url).query)
            actual = (kind, params.get(site.PARAM["root"], [None])[0],
                      params.get(site.PARAM["cursor"], ["0"])[0])
            if self.expected == actual:
                self.expected = None
                admission = "replay"
            elif self.seeding and kind not in self.seeded:
                self.seeded.add(kind)
                admission = "seed"
            else:
                # Block page auto-pagination: all further work is explicit/serial.
                await route.abort()
                return
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
            self.accepted[request] = admission
            try:
                if self.backend:
                    await self.backend.deliver(route)
                else:
                    await route.continue_()
                await asyncio.wait_for(done.wait(), self.timeout_ms / 1000 + 2)
            except asyncio.TimeoutError:
                self.control.stop(3, "unsettled_request")
            except Exception:
                # The caller's XHR returns the network failure and owns its one retry.
                pass
            finally:
                self.pacer.completed()
                self.active.pop(request, None)
                self.accepted.pop(request, None)

    async def state(self):
        combined = dict(risk=False, out=False, inside=False)
        if not self.page:
            return combined
        for frame in self.page.frames:
            try:
                result = await frame.evaluate(STATE_JS, site.SIGNALS)
                for key in combined:
                    combined[key] |= result[key]
            except Exception:
                # Navigation can detach a frame; preflight still requires a positive signal.
                continue
        return combined

    async def watch(self):
        while not self.closing:
            state = await self.state()
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
            state = await self.state()
            if state["risk"]:
                raise Stop(3, "verification_widget")
            if state["out"]:
                raise Stop(4, "login_missing")
            if state["inside"]:
                return
            if asyncio.get_running_loop().time() >= end:
                raise Stop(4, "login_signal_missing")
            await self.control.run(asyncio.sleep(0.1))

    async def open_video(self, url):
        self.templates.clear()
        self.seeded.clear()
        self.seed_ready.clear()
        self.budget.new_video()
        self.armed = self.seeding = True
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
        try:
            await self.control.run(asyncio.wait_for(self.seed_ready.wait(), 15))
        except asyncio.TimeoutError:
            raise Stop(2, "comment_template_missing") from None
        self.seeding = False
        title = await self.page.locator(site.SIGNALS["title"]).first.text_content() if await self.page.locator(site.SIGNALS["title"]).count() else ""
        displayed = await self.page.locator(site.SIGNALS["count"]).first.text_content() if await self.page.locator(site.SIGNALS["count"]).count() else ""
        return resolved, title or "", displayed or ""

    async def fetch(self, video_id, root, cursor):
        self.control.check()
        kind = "reply" if root else "top"
        template = self.templates.get(kind) or self.templates.get("top")
        if template is None:
            raise Stop(2, "comment_template_missing")
        self.expected = (kind, root, cursor)
        result = await self.control.run(self.page.evaluate(XHR_JS, dict(
            url=template.url, headers=template.headers, video=video_id, root=root,
            cursor=cursor, path=site.REPLY_PATH if root else site.TOP_PATH,
            params=site.PARAM, signatures=site.SIGNATURES, storage=site.TOKEN_STORAGE,
            signals=site.SIGNALS, timeout=self.timeout_ms,
        )))
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
        failed = False
        if self.context:
            try:
                await asyncio.wait_for(self.context.close(), 2.5)
            except Exception:
                failed = True
        if self.playwright:
            try:
                await asyncio.wait_for(self.playwright.stop(), 1)
            except Exception:
                failed = True
        if failed:
            _kill_owned(records)
        await self.display.close()
        self.profile.__exit__(None, None, None)
        self.templates.clear()
