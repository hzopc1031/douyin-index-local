"""Synthetic SMS transport/UI contract tests; only local sockets and temp state."""
import asyncio
import base64
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import app
import hub_bridge
import test_hub
from hub_logging import RedactingFilter, RedactingFormatter, redact
from hub_store import HubError
from hub_web import SMS_ERRORS
from login_driver import Capture, DriverError, FakeDriver, FrameMetadata
from sms_login import (FRAME_BASE64_LIMIT, FRAME_PNG_LIMIT, LoginCoordinator,
                       SmsLoginManager, public_sms_status)
from test_sms_login import PHONE, CODE, page, success

PNG = b'\x89PNG\r\n\x1a\nsynthetic-only'


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, INDEX_HUB_LOGIN_MODE='sms')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.driver = FakeDriver()
        self.driver.current = page()
        self.manager = SmsLoginManager(self.driver, threading.Lock(), coordinator=LoginCoordinator(), solver=None)
        self.api = Mock()
        self.api._sms_manager.return_value = self.manager
        self.addCleanup(self.manager.cancel)

    def test_all_commands_dispatch_and_strict_payloads(self):
        manager = self.api._sms_manager.return_value = Mock()
        snapshot = self.manager.status()
        for name in ('start', 'status', 'submit', 'resend', 'pointer', 'type_into_focused', 'cancel'):
            getattr(manager, name).return_value = snapshot
        manager.frame.return_value = dict(png=PNG, frame_id=1, challenge_epoch=1, width=20, height=10)
        cases = [('start', {'phone': PHONE}, 'start', (PHONE,)), ('status', {}, 'status', ()),
                 ('code', {'code': CODE}, 'submit', (CODE,)), ('resend', {}, 'resend', ()),
                 ('pointer', {'frame': 1, 'kind': 'tap', 'points': [[.2, .3, 0]]}, 'pointer', (1, 'tap', [[.2, .3, 0]])),
                 ('text', {'text': 'abc'}, 'type_into_focused', ('abc',)), ('cancel', {}, 'cancel', ())]
        for action, body, method, args in cases:
            with self.subTest(action=action):
                result = hub_bridge.sms_command(self.api, 'sms_'+action, body)
                self.assertTrue(result['ok'])
                getattr(manager, method).assert_called_with(*args)
                self.assertEqual(hub_bridge.sms_command(self.api, 'sms_'+action, {**body, 'extra': 1})['code'], 'invalid_payload')
        result = hub_bridge.sms_command(self.api, 'sms_frame', {'frame': 1})
        self.assertEqual(base64.b64decode(result['png_base64']), PNG)
        self.assertEqual(set(result), {'ok', 'png_base64', 'frame_id', 'challenge_epoch', 'width', 'height'})
        self.assertTrue(hub_bridge.sms_command(self.api, 'sms_status')['ok'])

    def test_encoded_frame_limit(self):
        manager = self.api._sms_manager.return_value = Mock()
        manager.frame.return_value = dict(png=b'x'*FRAME_PNG_LIMIT, frame_id=1, challenge_epoch=1, width=1, height=1)
        result = hub_bridge.sms_command(self.api, 'sms_frame', {'frame': 1})
        self.assertEqual(len(result['png_base64']), FRAME_BASE64_LIMIT)
        manager.frame.return_value['png'] += b'x'
        self.assertEqual(hub_bridge.sms_command(self.api, 'sms_frame', {'frame': 1})['code'], 'frame_too_large')

    def test_mode_switch_and_unavailable_browser(self):
        for mode in ('qr', 'desktop'):
            with patch.dict(os.environ, INDEX_HUB_LOGIN_MODE=mode):
                self.assertEqual(hub_bridge.sms_command(self.api, 'sms_start', {'phone': PHONE})['code'], 'sms_disabled')
        self.api._sms_manager.assert_not_called()
        with tempfile.TemporaryDirectory() as root, patch.object(app, 'OUTPUT', Path(root)):
            api = app.Api()
            self.assertEqual(hub_bridge.sms_command(api, 'sms_status', {})['code'], 'browser_unavailable')

    def test_app_wiring_fetch_lock_and_qr_clear(self):
        with tempfile.TemporaryDirectory() as root, patch.object(app, 'OUTPUT', Path(root)), \
                patch('login_driver.QtLoginDriver', return_value=self.driver) as factory:
            api = app.Api()
            api._page = Mock()
            api._qr = Mock()
            manager = api._sms_manager()
            self.assertIs(manager, api._sms_manager())
            self.assertIs(manager.coordinator, api._login_coordinator)
            self.assertIs(manager._fetch_lock, api._lock)
            api._page.native.get_cookies.return_value = []
            self.assertFalse(factory.call_args.kwargs['session_present']())
            api._page.get_cookies.assert_not_called()
            with patch.object(manager, '_launch'):
                manager.start(PHONE)
            try:
                self.assertFalse(api._lock.acquire(blocking=False))
                self.assertEqual(api.fetch_batch('synthetic', '2026-10-25', cloud=True)['code'], 'busy')
                manager._observe(manager._session, success())
                api._qr.holder.clear.assert_called_once_with()
            finally:
                manager.cancel()
                manager._release(manager._session)
            self.assertTrue(api._lock.acquire(blocking=False))
            api._lock.release()

    def test_unix_commands_remain_short_while_browser_is_blocked(self):
        entered, unblock = threading.Event(), threading.Event()
        def probe():
            entered.set()
            unblock.wait(5)
            return page()
        self.driver.probe = probe
        with tempfile.TemporaryDirectory() as root, patch.object(hub_bridge, '_socket_path', return_value=Path(root)/'browser.sock'):
            server = hub_bridge.start_bridge(self.api)
            try:
                for command, payload in [('sms_start', {'phone': PHONE}), ('sms_status', {}), ('sms_cancel', {})]:
                    start = time.monotonic()
                    result = hub_bridge.call_bridge(command, payload, 2)
                    self.assertLess(time.monotonic()-start, 2)
                    self.assertTrue(result['ok'])
                    self.assertNotIn(PHONE, json.dumps(result))
                    if command == 'sms_start':
                        self.assertTrue(entered.wait(1))
            finally:
                unblock.set()
                self.manager.cancel()
                if self.manager._session:
                    self.manager._session.thread.join(3)
                server.shutdown()
                server.server_close()
            self.assertIsNone(self.manager._session.lease)

    def test_unix_pointer_wait_keeps_controls_and_browser_locks_available(self):
        entered = threading.Event()
        def drag(points, *, cancel, **kwargs):
            entered.set()
            cancel.wait(8)  # A supported long replay, interrupted by real cancel.
            if cancel.is_set():
                raise DriverError('cancelled')
        self.driver.drag = drag
        self.driver.probes.extend([page(), page(verification=True)])
        with tempfile.TemporaryDirectory(dir='/tmp') as root, \
                patch.object(hub_bridge, '_socket_path', return_value=Path(root)/'browser.sock'):
            server = hub_bridge.start_bridge(self.api)
            results = []
            pointer = None
            try:
                self.manager.start(PHONE)
                deadline = time.monotonic()+2
                while not self.manager.status()['frame_id'] and time.monotonic() < deadline:
                    time.sleep(.01)
                frame_id = self.manager.status()['frame_id']
                self.assertGreater(frame_id, 0)
                body = {'frame': frame_id, 'kind': 'drag', 'points': [[.1, .5, 0], [.8, .5, 8000]]}
                pointer = threading.Thread(target=lambda: results.append(
                    hub_bridge.call_bridge('sms_pointer', body, 12)))
                pointer.start()
                self.assertTrue(entered.wait(2))
                status = hub_bridge.call_bridge('sms_status', {}, 2)
                self.assertEqual(status['state'], 'verify_send')
                self.assertTrue(self.manager._fetch_lock.locked())
                self.assertEqual(hub_bridge.call_bridge('sms_start', {'phone': PHONE}, 2)['code'], 'login_busy')
                self.assertEqual(hub_bridge.call_bridge('sms_pointer', body, 2)['code'], 'sms_rate_limited')
                cancelled = hub_bridge.call_bridge('sms_cancel', {}, 2)
                self.assertEqual(cancelled['state'], 'cancelled')
                pointer.join(2)
                self.assertFalse(pointer.is_alive())
                self.assertEqual(results, [{'ok': False, 'code': 'cancelled'}])
                self.manager._session.thread.join(2)
                self.assertIsNone(self.manager._session.lease)
                self.assertFalse(self.manager._fetch_lock.locked())
            finally:
                self.manager.cancel()
                if pointer is not None:
                    pointer.join(3)
                if self.manager._session:
                    self.manager._session.thread.join(3)
                server.shutdown()
                server.server_close()

    def test_exception_logs_never_format_payload(self):
        class Server:
            def __init__(self, address, handler): self.handler = handler
            def serve_forever(self): pass
        with tempfile.TemporaryDirectory() as root, patch.object(hub_bridge, '_socket_path', return_value=Path(root)/'browser.sock'), \
                patch.object(hub_bridge.socketserver, 'ThreadingUnixStreamServer', Server), patch.object(hub_bridge.os, 'chmod'):
            server = hub_bridge.start_bridge(self.api)
        for error in (RuntimeError(PHONE+' '+CODE), HubError(CODE, PHONE)):
            self.api._sms_manager.side_effect = error
            handler = object.__new__(server.handler)
            handler.request = Mock()
            handler.rfile = io.BytesIO(json.dumps({'command': 'sms_start', 'payload': {'phone': PHONE}}).encode()+b'\n')
            handler.wfile = io.BytesIO()
            with self.assertLogs('index_hub.bridge', 'WARNING') as logs:
                handler.handle()
            output = str(logs.output)+handler.wfile.getvalue().decode()
            self.assertNotIn(PHONE, output)
            self.assertNotIn(CODE, output)
            self.assertIn('command=sms_start', output)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mode = patch.dict(os.environ, INDEX_HUB_LOGIN_MODE='sms')
        self.mode.start()
        self.addCleanup(self.mode.stop)
        await test_hub.ApiTests.asyncSetUp(self)
        self.now = 1000.
        self.driver = FakeDriver()
        self.driver.current = page()
        self.driver.capture = Capture(PNG, FrameMetadata(0, 0, 20, 10))
        self.manager = SmsLoginManager(self.driver, threading.Lock(), coordinator=LoginCoordinator(), solver=None,
                                       clock=lambda: self.now)
        self.launch = patch.object(self.manager, '_launch')
        self.launch.start()
        self.api = Mock()
        self.api._sms_manager.return_value = self.manager
        self.commands = []
        def bridge(command, payload, timeout):
            self.commands.append((command, timeout))
            if command == 'status':
                return {'ok': True, 'state': 'browser_ready', 'login_state': 'cookie_present'}
            return hub_bridge.sms_command(self.api, command, payload)
        self.hub.bridge = bridge

    async def asyncTearDown(self):
        self.manager.cancel()
        if self.manager._session:
            self.manager._release(self.manager._session)
        self.launch.stop()
        await test_hub.ApiTests.asyncTearDown(self)

    async def post(self, action, body, expected=200, headers=None):
        response = await self.client.post('/api/v1/login/sms/'+action, headers=self.admin if headers is None else headers, json=body)
        data = await response.json()
        self.assertEqual(response.status, expected, data)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertNotIn(PHONE, json.dumps(data))
        self.assertNotIn(CODE, json.dumps(data))
        return data

    async def start_sent(self):
        await self.post('start', {'phone': PHONE})
        self.session = self.manager._session
        self.manager._observe(self.session, page())
        self.manager._step(self.session)
        self.manager._observe(self.session, page(countdown=60))

    async def test_every_route_admin_basic_only_and_csrf(self):
        routes = [('GET', '', None), ('GET', '/frame?v=1', None),
                  *[('POST', '/'+a, b) for a, b in [('start', {'phone': PHONE}), ('code', {'code': CODE}),
                    ('resend', {}), ('pointer', {'frame': 1, 'kind': 'tap', 'points': [[0, 0, 0]]}),
                    ('text', {'text': 'abc'}), ('cancel', {})]]]
        for method, suffix, body in routes:
            for headers, expected in [({}, 401), (self.bearer, 403), ({'X-Hub-Request': '1'}, 401)]:
                response = await self.client.request(method, '/api/v1/login/sms'+suffix, headers=headers, json=body)
                self.assertEqual(response.status, expected)
            if method == 'POST':
                for headers in ({'Authorization': self.admin['Authorization']}, {**self.admin, 'Origin': 'https://wrong.invalid'}):
                    response = await self.client.post('/api/v1/login/sms'+suffix, headers=headers, json=body)
                    self.assertEqual(response.status, 403)
        self.assertEqual(self.commands, [])
        ticket = await self.client.post('/api/v1/desktop/ticket', headers=self.admin, json={})
        value = (await ticket.json())['ticket']
        response = await self.client.get('/api/v1/login/sms?ticket='+value)
        self.assertEqual(response.status, 401)

    async def test_strict_schemas_and_duplicate_json(self):
        invalid = [('start', {'phone': 13800138000}), ('start', {'phone': PHONE, 'extra': 1}),
                   ('start', {'phone': '1380013800x'}), ('code', {'code': '１２３４'}), ('code', {'code': '1'}),
                   ('code', {'code': CODE, 'nonce': 'synthetic'}), ('resend', []), ('cancel', {'x': 1}),
                   ('text', {'text': 'too-long!'}), ('pointer', {'frame': True, 'kind': 'tap', 'points': [[0, 0, 0]]}),
                   ('pointer', {'frame': 1, 'kind': 'tap', 'points': [[2, 0, 0]]}),
                   ('pointer', {'frame': 1, 'kind': 'drag', 'points': [[0, 0, 0], [1, 1, 5]]})]
        for action, body in invalid:
            await self.post(action, body, 400)
        for raw in ('{"phone":"13800138000","phone":"13800138000"}', '{"phone":NaN}'):
            response = await self.client.post('/api/v1/login/sms/start', headers={**self.admin, 'Content-Type': 'application/json'}, data=raw)
            self.assertEqual(response.status, 400)
        for path in ('/frame', '/frame?v=0', '/frame?v=true', '/frame?v=1&v=2', '?phone=synthetic'):
            response = await self.client.get('/api/v1/login/sms'+path, headers=self.admin)
            self.assertEqual(response.status, 400, path)
        response = await self.client.get('/api/v1/login/sms', headers=self.admin, json={'phone': PHONE})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.commands, [])

    async def test_send_submit_consumption_and_manual_resume(self):
        await self.start_sent()
        self.assertEqual(self.hub.store.get('paused'), {'code': 'login_in_progress'})
        response = await self.client.post('/api/v1/collection/resume', headers=self.admin, json={})
        self.assertEqual(response.status, 409)
        await self.post('resend', {}, 429)
        result = await self.post('code', {'code': CODE})
        self.assertEqual(result['state'], 'submitting')
        self.assertEqual(result['phone'], '138****8000')
        self.assertEqual((await self.post('code', {'code': CODE}, 409))['error']['code'], 'code_consumed')
        self.manager._observe(self.session, success())
        self.manager._release(self.session)
        await self.hub.sync_sms_pause()  # Works even if the user closed the tab.
        self.assertEqual(self.hub.store.get('paused'), {'code': 'manual_resume_required'})
        response = await self.client.post('/api/v1/collection/resume', headers=self.admin, json={})
        self.assertEqual(response.status, 200)
        self.assertIsNone(self.hub.store.get('paused'))
        for command, timeout in self.commands:
            self.assertEqual(timeout, 2)

    async def test_terminal_states_never_resume_automatically(self):
        from sms_login import TERMINAL
        await self.start_sent()
        for state in TERMINAL | {'idle'}:
            self.hub.store.set('paused', {'code': 'login_in_progress'})
            with patch.object(self.hub, 'sms_call', AsyncMock(return_value={'state': state})):
                await self.hub.sync_sms_pause()
            self.assertEqual(self.hub.store.get('paused'), {'code': 'manual_resume_required'})

    async def test_fallback_mode_switch_can_resolve_old_sms_pause(self):
        for mode in ('desktop', 'qr'):
            with patch.dict(os.environ, INDEX_HUB_LOGIN_MODE=mode):
                self.hub.store.set('paused', {'code': 'login_in_progress'})
                await self.hub.sync_sms_pause()
                self.assertEqual(self.hub.store.get('paused'), {'code': 'manual_resume_required'})
                # Resume can also reconcile directly, before the next worker tick.
                self.hub.store.set('paused', {'code': 'login_in_progress'})
                response = await self.client.post('/api/v1/collection/resume', headers=self.admin, json={})
                self.assertEqual(response.status, 200)
                self.assertIsNone(self.hub.store.get('paused'))

    async def test_start_pause_precedes_bridge_and_fetch_preflight_rechecks(self):
        original = self.hub.bridge
        observations = []
        def checked(command, payload, timeout):
            if command == 'sms_start':
                # SQLite stays on the event-loop thread; observe the write
                # before dispatch rather than reading it inside this worker.
                observations.append(True)
            return original(command, payload, timeout)
        self.hub.bridge = checked
        original_call = self.hub.sms_call
        async def check_pause(action, body):
            self.assertEqual(self.hub.store.get('paused'), {'code': 'login_in_progress'})
            return await original_call(action, body)
        with patch.object(self.hub, 'sms_call', check_pause):
            await self.post('start', {'phone': PHONE})
        self.assertTrue(observations)
        self.hub.store.set('paused', None)
        self.hub.store.submit(test_hub.QUERY, 'admin', 'synthetic-pause-race')
        async def preflight():
            self.hub.store.set('paused', {'code': 'login_in_progress'})
            return 'browser_ready'
        with patch.object(self.hub, 'browser_status', preflight):
            self.assertFalse(await self.hub.run_one())
        self.assertFalse(any(c in ('query', 'insight') for c, _ in self.commands))
        self.assertIsNone(self.hub.store.get('daily'))

    async def test_admission_failure_restores_existing_pause_and_ambiguous_timeout_stays_paused(self):
        for prior in (None, {'code': 'rate_limited'}):
            self.hub.store.set('paused', prior)
            self.manager._fetch_lock.acquire()
            try:
                await self.post('start', {'phone': PHONE}, 409)
                self.assertEqual(self.hub.store.get('paused'), prior)
            finally:
                self.manager._fetch_lock.release()
        with patch.object(self.hub, 'bridge', side_effect=TimeoutError(PHONE+' '+CODE)):
            await self.post('start', {'phone': PHONE}, 503)
        self.assertEqual(self.hub.store.get('paused'), {'code': 'login_in_progress'})

    async def test_rejected_start_preserves_inflight_workers_safety_pause(self):
        query_entered, query_release = threading.Event(), threading.Event()
        start_entered, start_release = threading.Event(), threading.Event()
        queries = []

        def bridge(command, payload, timeout):
            if command == 'status':
                return {'ok': True, 'state': 'browser_ready'}
            if command == 'query':
                queries.append(payload)
                query_entered.set()
                if not query_release.wait(5):
                    raise AssertionError('query fixture deadline exceeded')
                return {'ok': False, 'code': 'rate_limited'}
            if command == 'sms_start':
                start_entered.set()
                if not start_release.wait(5):
                    raise AssertionError('start fixture deadline exceeded')
                return {'ok': False, 'code': 'busy_fetching'}
            raise AssertionError('unexpected fixture command')

        self.hub.bridge = bridge
        running, _ = self.hub.store.submit(test_hub.QUERY, 'admin', 'inflight-pause')
        queued, _ = self.hub.store.submit(test_hub.QUERY, 'admin', 'queued-pause')
        worker = asyncio.create_task(self.hub.run_one())
        start = None
        try:
            self.assertTrue(await asyncio.to_thread(query_entered.wait, 2))
            start = asyncio.create_task(self.post('start', {'phone': PHONE}, 409))
            self.assertTrue(await asyncio.to_thread(start_entered.wait, 2))
            self.assertEqual(self.hub.store.get('paused'), {'code': 'login_in_progress'})
            query_release.set()
            self.assertTrue(await asyncio.wait_for(worker, 2))
            paused = self.hub.store.get('paused')
            self.assertEqual(paused['code'], 'rate_limited')
            self.assertIn('since', paused)
            start_release.set()
            result = await asyncio.wait_for(start, 2)
            self.assertEqual(result['error']['code'], 'busy_fetching')
            self.assertEqual(self.hub.store.get('paused'), paused)
            self.assertEqual(self.hub.store.job(running['id'])['status'], 'failed')
            # Remove the interval guard so only the preserved safety pause can
            # prevent the next queued job from attempting collection.
            self.hub.store.set('last_attempt_at', 0)
            self.assertFalse(await self.hub.run_one())
            self.assertEqual(self.hub.store.job(queued['id'])['status'], 'queued')
            self.assertEqual(len(queries), 1)
            self.assertEqual(self.hub.store.get('daily')['count'], 1)
        finally:
            query_release.set()
            start_release.set()
            await asyncio.gather(worker, *([start] if start else []), return_exceptions=True)

    async def test_all_error_status_mappings_and_no_exception_echo(self):
        for code, (status, _) in SMS_ERRORS.items():
            with patch.object(self.hub, 'bridge', return_value={'ok': False, 'code': code, 'message': PHONE+' '+CODE}):
                result = await self.post('cancel', {}, status)
                self.assertEqual(result['error']['code'], code)
        for value in ({'ok': False, 'code': CODE, 'message': PHONE}, {'ok': True, 'state': 'idle', 'phone': PHONE}):
            with patch.object(self.hub, 'bridge', return_value=value):
                await self.post('cancel', {}, 503)
        with patch.object(self.hub, 'bridge', side_effect=RuntimeError(PHONE+' '+CODE)):
            await self.post('cancel', {}, 503)

    async def test_frames_modes_epochs_animation_and_pointer_timeout_budget(self):
        await self.start_sent()
        self.manager._observe(self.session, page(verification=True, challenge_key='synthetic-one'))
        self.manager._capture(self.session)
        response = await self.client.get('/api/v1/login/sms', headers=self.admin)
        data = await response.json()
        self.assertEqual(data['verification_mode'], 'manual')
        self.assertGreater(data['challenge_epoch'], 0)
        old_frame, epoch = data['frame_id'], data['challenge_epoch']
        response = await self.client.get('/api/v1/login/sms/frame?v='+str(old_frame), headers=self.admin)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), PNG)
        self.assertEqual(response.headers['Content-Type'], 'image/png')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.driver.capture = Capture(PNG+b'animation', self.driver.capture.metadata)
        self.manager._capture(self.session)
        self.assertEqual(self.session.challenge_epoch, epoch)
        self.assertTrue(self.manager._valid_frame(self.session, old_frame))
        response = await self.client.get('/api/v1/login/sms/frame?v='+str(old_frame), headers=self.admin)
        self.assertEqual(response.status, 409)
        with patch.object(self.manager, 'pointer', return_value=self.manager.status()) as pointer:
            await self.post('pointer', {'frame': old_frame, 'kind': 'tap', 'points': [[.5, .5, 0]]})
            pointer.assert_called_once_with(old_frame, 'tap', [[.5, .5, 0]])
            self.assertIn(('sms_pointer', 12), self.commands)
        self.manager._observe(self.session, page(verification=True, challenge_key='synthetic-two'))
        self.manager._capture(self.session)
        self.assertFalse(self.manager._valid_frame(self.session, old_frame))
        await self.post('pointer', {'frame': old_frame, 'kind': 'tap', 'points': [[.5, .5, 0]]}, 409)
        for mode in ('auto', 'desktop'):
            self.session.verification_mode = mode
            response = await self.client.get('/api/v1/login/sms', headers=self.admin)
            self.assertEqual((await response.json())['verification_mode'], mode)
            await self.post('text', {'text': 'abc'}, 409)

    async def test_real_rate_guards_surface_as_429(self):
        await self.start_sent()
        await self.post('resend', {}, 429)
        self.now += 61
        self.manager._send_times.extend([self.now, self.now])
        await self.post('resend', {}, 429)
        self.manager._send_times.clear()
        self.manager._day_count = 6
        await self.post('resend', {}, 429)
        self.session.code_count = 5
        await self.post('code', {'code': CODE}, 429)
        self.manager._observe(self.session, page(verification=True))
        self.manager._capture(self.session)
        self.session.text_count = 5
        await self.post('text', {'text': 'abc'}, 429)
        self.session.pointer_times.extend([self.now]*3)
        await self.post('pointer', {'frame': self.session.frame_id, 'kind': 'tap', 'points': [[0, 0, 0]]}, 429)
        await self.post('cancel', {})
        self.manager._release(self.session)
        self.manager._last_start = self.now
        await self.post('start', {'phone': PHONE}, 429)

    async def test_polling_budget_does_not_starve_cancel(self):
        # Status + changing image at 1Hz would consume the old 120/min quota.
        for _ in range(125):
            response = await self.client.get('/api/v1/login/sms', headers=self.admin)
            self.assertEqual(response.status, 200)
        await self.post('cancel', {})

    async def test_response_and_state_log_leak_scan(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(RedactingFilter())
        handler.setFormatter(RedactingFormatter('%(message)s'))
        logger = logging.getLogger()
        logger.addHandler(handler)
        try:
            await self.start_sent()
            await self.post('code', {'code': CODE})
            await self.post('cancel', {})
            for path in ('/api/v1/login/sms', '/api/v1/status', '/api/v1/jobs'):
                response = await self.client.get(path, headers=self.admin)
                data = await response.text()
                self.assertNotIn(PHONE, data)
                self.assertNotIn(CODE, data)
            self.assertNotIn(PHONE, stream.getvalue())
            self.assertNotIn(CODE, stream.getvalue())
            for path in self.root.rglob('*'):
                if path.is_file():
                    contents = path.read_bytes()
                    self.assertNotIn(PHONE.encode(), contents)
                    self.assertNotIn(CODE.encode(), contents)
        finally:
            logger.removeHandler(handler)
            handler.close()


class RedactionTests(unittest.TestCase):
    def test_phone_and_sms_assignment_fallbacks(self):
        self.assertEqual(redact('number '+PHONE), 'number 1**********')
        for name in ('phone', 'mobile', 'sms', 'otp', 'verify_code', 'code', 'text'):
            for template in ('{}={}', "'{}': '{}'", '"{}": "{}"'):
                for value in (PHONE, CODE):
                    self.assertNotIn(value, redact(template.format(name, value)))
        self.assertEqual(redact('command=sms_code code=code_consumed'), 'command=sms_code code=code_consumed')

    def test_snapshot_allowlist(self):
        value = dict(state='idle', phone='', cookie='synthetic-cookie', code=CODE, message=PHONE)
        result = public_sms_status(value)
        self.assertNotIn('cookie', result)
        self.assertNotIn(CODE, str(result))
        self.assertNotIn(PHONE, str(result))
