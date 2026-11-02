"""Install response interception before any child target runs JavaScript.

An iframe can start its first POST before a framenavigated callback runs, and
enabling Fetch while that POST is paused is too late for its response. Use
Chromium's debugger-wait auto-attach instead. Non-flat CDP sessions are carried
through the documented Target.sendMessageToTarget transport. Nothing is logged.
"""
import asyncio
import json

from .errors import Stop


def failure():
    return Stop(4, 'login_redirect_guard_failed')


class Target:
    def __init__(self, owner, parent, session_id, info):
        self.owner, self.parent, self.session_id = owner, parent, session_id
        self.info = info
        self.children = {}
        self.pending = {}
        self.sequence = 0
        self.closed = False

    async def send(self, method, params=None):
        if self.closed:
            raise failure()
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
                    pending.set_exception(failure())
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
        elif method == 'Fetch.requestPaused':
            self.owner.browser.guard_response(params, self)

    async def initialize(self):
        method = 'fetch_enable'
        try:
            # Dedicated-worker requests are intercepted by the owning page's
            # Fetch agent. Worker targets themselves
            # do not expose Fetch; invoking it fails with method-not-found.
            # Keep recursive attachment; iframe targets still need their own
            # Fetch agent before any child script runs. Other target kinds fail
            # closed if they cannot install the guard.
            if self.info.get('type') != 'worker':
                await self.send('Fetch.enable', dict(patterns=[dict(urlPattern='*', requestStage='Response')]))
            # Recursion is necessary: a page's child iframe/worker may itself
            # create another target. Each is held until its guard is installed.
            method = 'auto_attach'
            await self.send('Target.setAutoAttach', dict(autoAttach=True, waitForDebuggerOnStart=True, flatten=False))
            method = 'resume_target'
            await self.send('Runtime.runIfWaitingForDebugger')
            ready = self.owner.ready.get(self.info['targetId'])
            if ready is not None and not ready.done():
                ready.set_result(True)
        except Exception:
            if not self.owner.closing:
                kind = self.info.get('type', 'unknown')
                kind = kind if kind in ('iframe', 'worker', 'service_worker', 'shared_worker', 'page') else 'unknown'
                self.owner.browser.fatal_reason = getattr(self.owner.browser, 'fatal_reason', '') or 'child_guard_' + kind + '_' + method
                self.owner.browser.fatal = True
                ready = self.owner.ready.get(self.info['targetId'])
                if ready is not None and not ready.done():
                    ready.set_exception(failure())
            # Do not resume an unguarded child, even on protocol/version errors.

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
                       'Target.detachedFromTarget', 'Fetch.requestPaused'):
            self.session.on(method, lambda params, method=method: self.event(method, params))
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
