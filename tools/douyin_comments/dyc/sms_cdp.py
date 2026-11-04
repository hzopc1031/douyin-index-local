"""Install response interception on child targets that support Fetch.

An iframe can start its first POST before a framenavigated callback runs, and
enabling Fetch while that POST is paused is too late for its response. Use
Chromium's debugger-wait auto-attach instead. Non-flat CDP sessions are carried
through the documented Target.sendMessageToTarget transport. Nothing is logged.
"""
import asyncio
import json
import re

from .errors import Stop


def failure():
    return Stop(4, 'login_redirect_guard_failed')


class ProtocolError(Stop):
    """Keep only the numeric code, never the credential-bearing CDP message."""
    def __init__(self, code):
        super().__init__(4, 'login_redirect_guard_failed')
        self.protocol_error_code = code if type(code) is int else None


class Target:
    def __init__(self, owner, parent, session_id, info):
        self.owner, self.parent, self.session_id = owner, parent, session_id
        self.info = info
        self.children = {}
        self.pending = {}
        self.sequence = 0
        self.closed = False
        self.protocol_error_code = None
        self.qr_requests = set()

    async def send(self, method, params=None):
        if self.closed:
            raise failure()
        self.protocol_error_code = None
        self.sequence += 1
        number = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[number] = future
        try:
            async with asyncio.timeout(5):
                await self.parent.send('Target.sendMessageToTarget', dict(sessionId=self.session_id,
                    message=json.dumps(dict(id=number, method=method, params=params or {}))))
                return await future
        finally:
            self.pending.pop(number, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # consume even if transport failed first

    def receive(self, message):
        if self.closed:
            return
        data = json.loads(message)
        if 'id' in data:
            pending = self.pending.get(data['id'])
            if pending is not None and not pending.done():
                if 'error' in data:
                    code = data['error'].get('code')
                    self.protocol_error_code = code if type(code) is int else None
                    pending.set_exception(ProtocolError(code))
                else:
                    pending.set_result(data.get('result', {}))
        else:
            self.event(data['method'], data.get('params', {}))

    def event(self, method, params):
        if method == 'Target.attachedToTarget':
            child = Target(self.owner, self, params['sessionId'], params['targetInfo'])
            self.children[params['sessionId']] = child
            self.owner.spawn(child.initialize())
        elif method == 'Target.receivedMessageFromTarget':
            child = self.children.get(params['sessionId'])
            if child:
                child.receive(params['message'])
        elif method == 'Target.detachedFromTarget':
            child = self.children.pop(params['sessionId'], None)
            if child:
                child.stop()
        elif method == 'Network.responseReceived':
            from urllib.parse import urlsplit
            if urlsplit(params.get('response', {}).get('url', '')).path.rstrip('/').endswith('/check_qrconnect'):
                self.qr_requests.add(params['requestId'])
        elif method in ('Network.loadingFinished', 'Network.loadingFailed'):
            request_id = params['requestId']
            if request_id in self.qr_requests:
                self.qr_requests.discard(request_id)
                if method == 'Network.loadingFinished':
                    self.owner.spawn(self.owner.browser.read_cdp_qr_body(self, request_id))
        elif method == 'Fetch.requestPaused':
            self.owner.browser.guard_response(params, self)

    async def optional_domain(self, method, params):
        try:
            await self.send(method, params)
        except ProtocolError as error:
            # Some worklets/worker/iframe targets do not implement these CDP
            # domains. Keep the owning page guard and let the component load.
            # A timeout, closed transport or other protocol failure is distinct.
            if error.protocol_error_code != -32601:
                raise

    async def initialize(self):
        method = 'fetch_enable'
        try:
            # Dedicated-worker requests are intercepted by the owning page's
            # Fetch agent. Worker targets themselves
            # do not expose Fetch; invoking it fails with method-not-found.
            # Probe other types (including future/unknown types) by capability.
            # The authorized login-debug fallback skips an unsupported child
            # Fetch agent; it does not claim that all such traffic is guarded.
            if self.info.get('type') != 'worker':
                await self.optional_domain('Fetch.enable', dict(patterns=[dict(urlPattern='*', requestStage='Response')]))
            # Recursion is necessary: a page's child iframe/worker may itself
            # create another target. Install supported guards before resuming.
            method = 'network_enable'
            await self.optional_domain('Network.enable', {})
            method = 'auto_attach'
            await self.optional_domain('Target.setAutoAttach', dict(autoAttach=True, waitForDebuggerOnStart=True, flatten=False))
            method = 'resume_target'
            await self.send('Runtime.runIfWaitingForDebugger')
            ready = self.owner.ready.get(self.info['targetId'])
            if ready is not None and not ready.done():
                ready.set_result(True)
        except Exception:
            if not self.owner.closing:
                kind = self.info.get('type', 'unknown')
                kind = kind if isinstance(kind, str) and re.fullmatch('[a-z_]{1,32}', kind) else 'unknown'
                self.owner.browser.fatal_context = dict(target_type=kind, command=method,
                    protocol_error_code=self.protocol_error_code)
                self.owner.browser.fatal_reason = getattr(self.owner.browser, 'fatal_reason', '') or 'child_guard_' + kind + '_' + method
                self.owner.browser.fatal = True
                ready = self.owner.ready.get(self.info['targetId'])
                if ready is not None and not ready.done():
                    ready.set_exception(failure())
            # Unexpected failures still stop preparation; only explicit
            # method-not-found on the optional domains permits resumption.

    def stop(self):
        self.closed = True
        for pending in self.pending.values():
            if not pending.done():
                pending.set_exception(failure())
        for child in self.children.values():
            child.stop()
        self.children.clear()


class TargetGuard(Target):
    def __init__(self, browser):
        self.browser = browser
        self.closing = False
        self.tasks = set()
        self.ready = {}
        self.session = None
        super().__init__(self, None, '', {})

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def send(self, method, params=None):
        return await self.session.send(method, params or {})

    async def start(self, main_session):
        # Page-level auto-attach supports non-flat child transports and pauses
        # its new iframe/worker targets before their first script executes.
        self.session = main_session
        for method in ('Target.attachedToTarget', 'Target.receivedMessageFromTarget',
                       'Target.detachedFromTarget', 'Fetch.requestPaused', 'Network.responseReceived',
                       'Network.loadingFinished', 'Network.loadingFailed'):
            self.session.on(method, lambda params, method=method: self.event(method, params))
        await self.send('Network.enable')
        await self.send('Fetch.enable', dict(patterns=[dict(urlPattern='*', requestStage='Response')]))
        await self.send('Target.setAutoAttach', dict(autoAttach=True, waitForDebuggerOnStart=True, flatten=False))

    async def close(self):
        self.closing = True
        self.stop()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        for pending in self.ready.values():
            if not pending.done():
                pending.cancel()
            elif not pending.cancelled():
                pending.exception()
        self.ready.clear()
