"""Child-target interception must precede its first script; failure never resumes."""
import asyncio
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from types import SimpleNamespace
import unittest

from dyc.sms_cdp import Target


class TargetTests(unittest.IsolatedAsyncioTestCase):
    async def check_initialization(self, fail=False, kind="iframe"):
        methods = []
        owner = SimpleNamespace(browser=SimpleNamespace(fatal=False), ready={}, closing=False)
        target = None
        class Parent:
            async def send(self, method, params):
                message = json.loads(params['message'])
                methods.append(message['method'])
                result = {'id': message['id'], 'result': {}}
                if fail and message['method'] == 'Fetch.enable':
                    result = {'id': message['id'], 'error': {'code': -32601, 'message': 'synthetic private browser error'}}
                asyncio.get_running_loop().call_soon(target.receive, json.dumps(result))
        target = Target(owner, Parent(), 'synthetic-session', {'targetId': 'synthetic-target', 'type': kind})
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            await target.initialize()
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(owner.browser.fatal, fail)
        self.assertFalse(target.pending)
        if fail:
            self.assertEqual(owner.browser.fatal_context, dict(target_type=kind,
                command='fetch_enable', protocol_error_code=-32601))
        return methods

    async def test_guard_and_recursive_pause_before_resume(self):
        self.assertEqual(await self.check_initialization(),
                         ['Fetch.enable', 'Target.setAutoAttach', 'Runtime.runIfWaitingForDebugger'])

    async def test_failed_guard_never_resumes_child(self):
        self.assertEqual(await self.check_initialization(fail=True), ['Fetch.enable'])

    async def test_worker_uses_owner_fetch_agent_and_keeps_recursive_attach(self):
        self.assertEqual(await self.check_initialization(kind='worker'),
                         ['Target.setAutoAttach', 'Runtime.runIfWaitingForDebugger'])

    async def test_unsupported_target_preserves_type_and_numeric_protocol_code(self):
        self.assertEqual(await self.check_initialization(fail=True, kind='worklet'), ['Fetch.enable'])
