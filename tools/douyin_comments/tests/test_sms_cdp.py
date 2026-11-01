"""Child-target interception must precede its first script; failure never resumes."""
import asyncio
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from types import SimpleNamespace
import unittest

from dyc.sms_cdp import Target


class TargetTests(unittest.IsolatedAsyncioTestCase):
    async def check_initialization(self, fail=False):
        methods = []
        owner = SimpleNamespace(browser=SimpleNamespace(fatal=False), ready={}, closing=False)
        target = None
        class Parent:
            async def send(self, method, params):
                message = json.loads(params['message'])
                methods.append(message['method'])
                result = {'id': message['id'], 'result': {}}
                if fail and message['method'] == 'Fetch.enable':
                    result = {'id': message['id'], 'error': {'message': 'synthetic private browser error'}}
                asyncio.get_running_loop().call_soon(target.receive, json.dumps(result))
        target = Target(owner, Parent(), 'synthetic-session', {'targetId': 'synthetic-target'})
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            await target.initialize()
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(owner.browser.fatal, fail)
        self.assertFalse(target.pending)
        return methods

    async def test_guard_and_recursive_pause_before_resume(self):
        self.assertEqual(await self.check_initialization(),
                         ['Fetch.enable', 'Target.setAutoAttach', 'Runtime.runIfWaitingForDebugger'])

    async def test_failed_guard_never_resumes_child(self):
        self.assertEqual(await self.check_initialization(fail=True), ['Fetch.enable'])
