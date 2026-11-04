"""Guard supported children; unsupported domains must not freeze login loading."""
import asyncio
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from types import SimpleNamespace
import unittest

from dyc.sms_cdp import Target


class TargetTests(unittest.IsolatedAsyncioTestCase):
    async def test_qr_network_body_is_read_only_after_loading_finishes(self):
        calls = []
        class Browser:
            async def read_cdp_qr_body(self, session, request_id):
                calls.append((session, request_id))
        tasks = []
        owner = SimpleNamespace(browser=Browser(), spawn=lambda coro: tasks.append(asyncio.create_task(coro)))
        target = Target(owner, None, 'child', {})
        target.event('Network.responseReceived', {'requestId': '1', 'response': {
            'url': 'https://sso.douyin.com/passport/web/check_qrconnect/?private'}})
        self.assertFalse(tasks)
        target.event('Network.loadingFinished', {'requestId': '1'})
        await asyncio.gather(*tasks)
        self.assertEqual(calls, [(target, '1')])

    async def check_initialization(self, errors=None, kind="iframe", fatal=False):
        errors = errors or {}
        methods = []
        owner = SimpleNamespace(browser=SimpleNamespace(fatal=False), ready={}, closing=False)
        target = None
        class Parent:
            async def send(self, method, params):
                message = json.loads(params['message'])
                methods.append(message['method'])
                result = {'id': message['id'], 'result': {}}
                if message['method'] in errors:
                    result = {'id': message['id'], 'error': {'code': errors[message['method']], 'message': 'synthetic private browser error'}}
                asyncio.get_running_loop().call_soon(target.receive, json.dumps(result))
        target = Target(owner, Parent(), 'synthetic-session', {'targetId': 'synthetic-target', 'type': kind})
        ready = asyncio.get_running_loop().create_future()
        owner.ready['synthetic-target'] = ready
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            await target.initialize()
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(owner.browser.fatal, fatal)
        self.assertFalse(target.pending)
        self.assertTrue(ready.done())
        if fatal:
            self.assertIsNotNone(ready.exception())
            command = {'Fetch.enable': 'fetch_enable', 'Network.enable': 'network_enable', 'Target.setAutoAttach': 'auto_attach',
                       'Runtime.runIfWaitingForDebugger': 'resume_target'}[methods[-1]]
            self.assertEqual(owner.browser.fatal_context, dict(target_type=kind,
                command=command, protocol_error_code=errors[methods[-1]] if type(errors[methods[-1]]) is int else None))
        else:
            self.assertTrue(ready.result())
        return methods

    async def test_guard_and_recursive_pause_before_resume(self):
        self.assertEqual(await self.check_initialization(),
                         ['Fetch.enable', 'Network.enable', 'Target.setAutoAttach', 'Runtime.runIfWaitingForDebugger'])

    async def test_unexpected_guard_error_never_resumes_child(self):
        for code in (-32000, -32602, None, '-32601', True):
            with self.subTest(code=code):
                self.assertEqual(await self.check_initialization(errors={'Fetch.enable': code}, fatal=True), ['Fetch.enable'])

    async def test_worker_uses_owner_fetch_agent_and_keeps_recursive_attach(self):
        self.assertEqual(await self.check_initialization(kind='worker'),
                         ['Network.enable', 'Target.setAutoAttach', 'Runtime.runIfWaitingForDebugger'])

    async def test_unsupported_fetch_resumes_all_child_types(self):
        for kind in ('worklet', 'service_worker', 'shared_worker', 'iframe', 'unknown', 'future_target'):
            with self.subTest(kind=kind):
                self.assertEqual(await self.check_initialization(errors={'Fetch.enable': -32601}, kind=kind),
                    ['Fetch.enable', 'Network.enable', 'Target.setAutoAttach', 'Runtime.runIfWaitingForDebugger'])

    async def test_unsupported_recursive_attach_still_resumes_worklet(self):
        self.assertEqual(await self.check_initialization(kind='worklet', errors={
            'Fetch.enable': -32601, 'Target.setAutoAttach': -32601}),
            ['Fetch.enable', 'Network.enable', 'Target.setAutoAttach', 'Runtime.runIfWaitingForDebugger'])

    async def test_unexpected_attach_or_resume_error_remains_fatal(self):
        for method in ('Target.setAutoAttach', 'Runtime.runIfWaitingForDebugger'):
            with self.subTest(method=method):
                await self.check_initialization(errors={method: -32000}, fatal=True)
        await self.check_initialization(errors={'Runtime.runIfWaitingForDebugger': -32601}, fatal=True)
