"""Deterministic, synthetic-only SMS state/guard tests; no real browser/login."""
from datetime import datetime
import io
import logging
import threading
import time
import unittest
from unittest.mock import Mock, patch

from login_driver import Capture, DriverError, DriverTimeout, FakeDriver, FrameMetadata
from sms_login import (FRAME_PNG_LIMIT, LoginBusy, LoginCoordinator, SmsLoginError, SmsLoginManager,
                       SmsLoginSession, TERMINAL, VERIFY)

PHONE = '13800138000'
CODE = '246810'
RECT = dict(x=20, y=30, width=100, height=40)


def page(**changes):
    result = dict(url='https://creator.douyin.com/login', login_page=True,
                  login_form=True, session_present=False, navigation_ready=True, countdown=None,
                  found={'phone': True, 'send': True},
                  rects={key: RECT.copy() for key in ('open', 'tab', 'phone', 'send', 'code', 'submit', 'verification')})
    result.update(changes)
    return result


def success(**changes):
    return page(url='https://creator.douyin.com/home', login_page=False,
                login_form=False, session_present=True, **changes)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.
        self.wall = datetime.fromisoformat('2026-10-31T12:00:00+08:00').timestamp()
        self.driver = FakeDriver()
        self.driver.current = page()
        self.fetch = threading.Lock()
        self.coordinator = LoginCoordinator()
        self.manager = SmsLoginManager(self.driver, self.fetch, coordinator=self.coordinator,
                                       clock=lambda: self.now, wall_clock=lambda: self.wall)
        # Step the exact production worker without sleep/races in transition tests.
        self.launch = patch.object(self.manager, '_launch').start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.manager.cancel()
        if self.manager._session:
            self.manager._release(self.manager._session)

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises((SmsLoginError, DriverError)) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(PHONE, str(caught.exception))
        self.assertNotIn(CODE, str(caught.exception))

    def state(self, expected):
        self.assertEqual(self.manager.status()['state'], expected)

    def start(self):
        self.manager.start(PHONE)
        self.state('opening')
        self.assertTrue(self.fetch.locked())

    def step(self, observation=None, advance=1):
        self.now += advance
        if observation is not None:
            self.driver.probes.append(observation)
        self.manager._step(self.manager._session)

    def sending(self):
        self.start()
        self.step(page())
        self.state('sending')
        self.step()  # Send action, no blocking in start().

    def sent(self, verify=False):
        self.sending()
        if verify:
            self.step(page(verification=True))
            self.state('verify_send')
        self.step(page(countdown=60))
        self.state('code_sent')

    def submitted(self):
        self.manager.submit(CODE)
        self.state('submitting')
        self.step()

    def test_idle_opening_sending_code_sent_submitting_success(self):
        self.state('idle')
        self.sent()
        self.assertEqual(self.driver.actions[:3], [('click', 70, 50), ('type_text', 11), ('click', 70, 50)])
        self.submitted()
        self.step(success())
        self.state('succeeded')
        self.assertIsNone(self.manager._session.phone)
        self.assertFalse(self.manager._session.nonce)
        self.assertIsNone(self.manager._session.frame)
        self.assertEqual(self.manager._session.used_codes, set())

    def test_already_logged_in_does_not_touch_page_or_send_budget(self):
        self.start()
        self.step(success())
        self.state('already_logged_in')
        self.assertEqual(self.driver.actions, [])
        self.assertEqual(len(self.manager._send_times), 0)

    def test_success_requires_cookie_no_login_form_and_official_origin(self):
        for changes in ({'session_present': False}, {'login_form': True}, {'login_page': True},
                        {'url': 'http://creator.douyin.com/home'}, {'url': 'https://evil.invalid/home'},
                        {'url': 'https://creator.douyin.com:444/home'}):
            observation = success()
            observation.update(changes)
            self.assertFalse(self.manager._success(observation))

    def test_public_authentication_returns_once_before_success_and_callback(self):
        callback = self.manager._on_success = Mock()
        self.sent()
        self.submitted()
        authenticated = {**success(), 'url': 'https://www.douyin.com/jingxuan'}
        self.assertFalse(self.manager._success(authenticated))
        self.step(authenticated)
        self.state('submitting')
        callback.assert_not_called()
        self.assertTrue(self.fetch.locked())
        self.step()
        self.assertEqual(self.driver.actions[-1], ('return_to_creator',))
        # A cookie and a complete creator load alone do not prove readiness.
        for observation in (page(session_present=True), success(navigation_ready=False),
                            {**success(), 'session_present': False}):
            self.step(observation)
            self.state('submitting')
            callback.assert_not_called()
        self.step(success())
        self.state('succeeded')
        callback.assert_called_once_with()
        self.assertEqual(self.driver.actions.count(('return_to_creator',)), 1)
        self.assertEqual(len(self.manager._send_times), 1)

    def test_public_existing_session_requires_creator_readiness_without_sending(self):
        self.start()
        self.step({**success(), 'url': 'https://www.douyin.com/jingxuan'})
        self.state('opening')
        self.step()
        self.step(success())
        self.state('already_logged_in')
        self.assertEqual(self.driver.actions, [('return_to_creator',)])
        self.assertEqual(len(self.manager._send_times), 0)

    def test_creator_return_failure_cancel_timeout_and_unready_page_never_succeed(self):
        callback = self.manager._on_success = Mock()
        for outcome in ('load_failure', 'timeout', 'cancel', 'login', 'public', 'untrusted'):
            with self.subTest(outcome=outcome):
                self.start()
                public = {**success(), 'url': 'https://www.douyin.com/jingxuan'}
                self.step(public)
                if outcome in ('load_failure', 'timeout'):
                    error = DriverError('navigation_failed') if outcome == 'load_failure' else DriverTimeout()
                    with patch.object(self.driver, 'return_to_creator', side_effect=error):
                        self.step()
                elif outcome == 'cancel':
                    self.manager.cancel()
                    self.step()
                else:
                    self.step()
                    observation = {'login': page(session_present=True), 'public': public,
                                   'untrusted': {**success(), 'url': 'http://127.0.0.1/'}}[outcome]
                    self.step(observation, advance=16)
                self.state('cancelled' if outcome == 'cancel' else 'failed')
                callback.assert_not_called()
                self.assertIsNone(self.manager._session.phone)
                self.assertEqual(len(self.manager._send_times), 0)
                self.cleanup()
                self.now += 10

    def test_untrusted_origin_never_admits_send_or_success(self):
        urls = ('http://creator.douyin.com/login', 'http://127.0.0.1/login',
                'http://www.douyin.com/jingxuan', 'https://www.douyin.com.evil.invalid/jingxuan',
                'https://www.douyin.com:444/jingxuan', 'https://fixture@www.douyin.com/jingxuan',
                'https://creator.douyin.com.evil.invalid/login',
                'https://creator.douyin.com:444/login', 'https://fixture@creator.douyin.com/login',
                'https://creator.douyin.com:invalid/login', 'https://[invalid', '', None)
        for url in urls:
            with self.subTest(url=url):
                self.start()
                self.step(page(url=url))
                self.state('failed')
                self.assertEqual(self.driver.actions, [])
                self.assertEqual(len(self.manager._send_times), 0)
                self.assertIsNone(self.manager._session.phone)
                self.assertFalse(self.manager._success(dict(url=url, login_page=False,
                                                          login_form=False, session_present=True)))
                self.cleanup()
                self.now += 10

    def test_navigation_after_queuing_phone_code_or_text_aborts_input(self):
        for kind in ('send', 'code', 'text'):
            with self.subTest(kind=kind):
                if kind == 'send':
                    self.start()
                    self.step(page())
                else:
                    self.sent()
                    if kind == 'code':
                        self.manager.submit(CODE)
                    else:
                        self.submitted()
                        self.step(page(verification=True))
                        self.manager.type_into_focused(CODE)
                before = list(self.driver.actions)
                self.driver.current = page(url='http://127.0.0.1/login')
                self.step()
                self.state('failed')
                self.assertEqual(self.driver.actions, before)
                self.assertIsNone(self.manager._session.phone)
                self.assertEqual(list(self.manager._session.actions), [])
                self.cleanup()
                self.now += 61

    def test_navigation_between_focus_type_and_submit_aborts(self):
        for stage in ('focus', 'type'):
            with self.subTest(stage=stage):
                self.start()
                self.step(page())
                method = 'click' if stage == 'focus' else 'type_text'
                original = getattr(self.driver, method)
                def navigate(*args, **kwargs):
                    original(*args, **kwargs)
                    self.driver.current = page(url='http://127.0.0.1/login')
                with patch.object(self.driver, method, side_effect=navigate):
                    before = len(self.driver.actions)
                    self.step()
                self.state('failed')
                self.assertEqual([a[0] for a in self.driver.actions[before:]],
                                 ['click'] if stage == 'focus' else ['click', 'type_text'])
                self.cleanup()
                self.now += 61

    def test_navigation_away_while_waiting_for_code_aborts_round(self):
        self.sent()
        self.step(page(url='http://127.0.0.1/login', countdown=60))
        self.state('failed')
        self.assertIsNone(self.manager._session.phone)
        self.error('login_not_active', self.manager.submit, CODE)

    def test_tab_preparation_and_missing_selectors(self):
        self.start()
        self.step(page(found={'tab': True}))
        self.state('opening')
        self.step()
        self.assertEqual(self.driver.actions, [('click', 70, 50)])
        self.step(page())
        self.state('sending')

    def test_header_opens_once_and_waits_for_redirected_dialog(self):
        self.start()
        self.step(page(url='https://www.douyin.com/', login_page=False, login_form=False,
                       found={'open': True}))
        self.step()
        self.assertEqual(self.driver.actions, [('click', 70, 50)])
        self.step(page(url='https://www.douyin.com/jingxuan', found={}))
        self.state('opening')
        self.step(page(found={'open': True}))
        self.assertEqual(self.driver.actions, [('click', 70, 50)])
        self.step(page(url='https://www.douyin.com/jingxuan', found={'phone': True, 'send': True, 'tab': True}))
        self.state('sending')
        self.assertFalse(self.manager._session.tab_clicked)

    def test_visible_form_during_header_navigation_cannot_consume_send_allowance(self):
        self.start()
        self.step(page(url='https://www.douyin.com/', login_page=False, login_form=False,
                       found={'open': True}))
        self.step()
        incomplete = page(url='https://www.douyin.com/jingxuan', navigation_ready=False)
        for _ in range(3):
            self.step(incomplete)
            self.state('opening')
            self.assertEqual(len(self.manager._send_times), 0)
            self.assertEqual(self.manager._day_count, 0)
            self.assertFalse(self.manager._session.nonce)
            self.assertEqual(list(self.manager._session.actions), [])
        self.assertEqual(self.driver.actions, [('click', 70, 50)])
        self.step({**incomplete, 'navigation_ready': True})
        self.state('sending')
        self.step()
        self.assertEqual(len(self.manager._send_times), 1)
        self.assertEqual(self.driver.actions[1:], [('click', 70, 50), ('type_text', 11), ('click', 70, 50)])

    def test_incomplete_navigation_keeps_cancel_and_deadline_guards(self):
        for outcome in ('cancel', 'opening_deadline', 'round_deadline', 'untrusted'):
            with self.subTest(outcome=outcome):
                self.start()
                self.step(page(navigation_ready=False))
                if outcome == 'cancel':
                    self.manager.cancel()
                    self.step()
                elif outcome == 'untrusted':
                    self.step(page(navigation_ready=False, url='http://127.0.0.1/'))
                else:
                    self.step(page(navigation_ready=False), advance=16 if outcome == 'opening_deadline' else 600)
                self.state({'cancel': 'cancelled', 'round_deadline': 'timeout'}.get(outcome, 'failed'))
                self.assertEqual(self.driver.actions, [])
                self.assertEqual(len(self.manager._send_times), 0)
                self.assertIsNone(self.manager._session.phone)
                self.cleanup()
                self.driver.probes.clear()  # Round deadline terminates before reading the queued fixture.
                self.now += 10

    def test_active_sms_pane_never_clicks_tab(self):
        self.start()
        self.step(page(found={'tab': True, 'sms_pane': True}))
        self.state('opening')
        self.assertEqual(list(self.manager._session.actions), [])
        self.step(page(found={'phone': True, 'send': True, 'tab': True, 'sms_pane': True}))
        self.state('sending')
        self.assertFalse(self.manager._session.tab_clicked)
        self.assertEqual(self.driver.actions, [])

    def test_disabled_controls_wait_without_retyping_or_reclicking(self):
        self.start()
        self.step(page())
        with patch.object(self.driver, 'element_rect', side_effect=[RECT, None, RECT]):
            self.step()
        self.assertEqual([a[0] for a in self.driver.actions], ['click', 'type_text', 'click'])
        self.step(page(countdown=60))
        self.manager.submit(CODE)
        with patch.object(self.driver, 'element_rect', side_effect=[RECT, None, None]), \
                patch('sms_login.time.monotonic', side_effect=[100, 100, 101, 106]):
            self.step()
        self.state('failed')
        self.assertEqual([a[0] for a in self.driver.actions[3:]], ['click', 'type_text'])

    def test_opening_failed_for_missing_or_stuck_tab(self):
        for found in ({}, {'tab': True}, {'open': True}):
            with self.subTest(found=found):
                self.start()
                self.step(page(found=found))
                if found:
                    self.step()
                self.step(page(found=found), advance=16)
                self.state('failed')
                self.cleanup()
                self.now += 10

    def test_send_verification_self_transition_then_code_sent(self):
        self.sent(verify=True)
        self.assertGreater(self.manager._frame_version, 0)

    def test_verify_send_refresh_and_timeout(self):
        self.start()
        self.step(page())
        self.step()
        self.step(page(verification=True))
        self.state('verify_send')
        version = self.manager.status()['frame_id']
        self.driver.capture = Capture(b'changed-synthetic-png', self.driver.capture.metadata)
        self.step(page(verification=True))
        self.state('verify_send')
        self.assertGreater(self.manager.status()['frame_id'], version)
        self.step(advance=600)
        self.state('timeout')

    def test_login_verification_self_transition_then_success(self):
        self.sent()
        self.submitted()
        self.step(page(verification=True))
        self.state('verify_login')
        self.step(page(verification=True))
        self.state('verify_login')
        self.step(success())
        self.state('succeeded')

    def test_priority_success_rate_verification_error_countdown(self):
        self.start()
        self.step(success(rate_limited=True, verification=True, error=True, code_invalid=True))
        self.state('already_logged_in')
        self.cleanup()
        self.now += 10
        self.start()
        self.step(page(rate_limited=True, verification=True, error=True, countdown=60))
        self.state('rate_limited')
        self.cleanup()
        self.now += 10
        self.sent()
        self.submitted()
        self.step(page(verification=True, code_invalid=True, countdown=60))
        self.state('verify_login')

    def test_invalid_code_can_retry_only_new_codes_three_attempts(self):
        self.sent()
        for index, code in enumerate((CODE, '246811', '246812'), 1):
            self.manager.submit(code)
            self.error('code_consumed', self.manager.submit, code)
            self.step()
            self.step(page(code_invalid=True))
            self.state('code_invalid' if index < 3 else 'failed')
            if index < 3:
                self.error('code_consumed', self.manager.submit, code)
                self.assertEqual(self.manager.status()['attempts_left'], 3-index)
        self.assertIsNone(self.manager._session.phone)

    def overtaken_submission_probe(self, attempt, *, before_observe=False, timeout=False):
        self.sent()
        session = self.manager._session
        # Model the prior rejected submissions without introducing other codes.
        session.submissions = session.code_count = attempt-1
        self.manager._set(session, 'code_invalid')
        self.driver.current = page(code_invalid=True)
        if timeout:
            session.probe_timeouts = 2
        nonce = session.nonce
        actions_before = list(self.driver.actions)
        entered, release = threading.Event(), threading.Event()
        errors = []

        def wait_for_submission():
            entered.set()
            if not release.wait(3):
                raise AssertionError('submission did not release the old probe')

        original_probe, original_observe = self.driver.probe, self.manager._observe
        def probe():
            observation = original_probe()
            wait_for_submission()
            if timeout:
                raise DriverTimeout()
            return observation

        def observe(*args, **kwargs):
            wait_for_submission()
            return original_observe(*args, **kwargs)

        def worker():
            try:
                self.step()
            except Exception as exc:
                errors.append(type(exc))

        target, method, callback = ((self.manager, '_observe', observe) if before_observe
                                    else (self.driver, 'probe', probe))
        with patch.object(target, method, side_effect=callback):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.manager.submit(CODE, nonce=nonce)['state'], 'submitting')
                action = session.actions[0]
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.state('submitting')
        self.assertTrue(session.code_consumed)
        self.assertEqual(session.submissions, attempt)
        self.assertEqual(session.code_count, attempt)
        self.assertEqual(self.manager.status()['attempts_left'], 3-attempt)
        self.assertEqual(session.nonce, nonce)
        self.assertFalse(session.cancelled.is_set())
        self.assertEqual(list(session.actions), [action])
        self.assertFalse(action.done.is_set())
        self.assertEqual(self.driver.actions, actions_before)
        self.error('code_consumed', self.manager.submit, CODE, nonce=nonce)
        self.error('code_consumed', self.manager.resend)
        self.step()  # The admitted input must still type and submit exactly once.
        self.assertEqual(self.driver.actions[len(actions_before):],
                         [('click', 70, 50), ('type_text', len(CODE)), ('click', 70, 50)])
        self.assertTrue(action.done.is_set())
        self.assertEqual(action.error, '')
        self.assertIsNone(action.value)
        self.assertFalse(action.nonce)
        self.assertEqual(list(session.actions), [])
        self.state('submitting')
        self.assertTrue(session.code_consumed)

    def test_old_invalid_probe_cannot_unconsume_second_submission(self):
        self.overtaken_submission_probe(2)
        self.step(page(code_invalid=True))
        self.state('code_invalid')
        self.assertFalse(self.manager._session.code_consumed)
        self.assertEqual(self.manager.status()['attempts_left'], 1)
        self.error('code_consumed', self.manager.submit, CODE)
        self.error('resend_too_early', self.manager.resend)

    def test_old_invalid_probe_cannot_fail_third_submission_before_input(self):
        self.overtaken_submission_probe(3)
        self.step(page(code_invalid=True))
        self.state('failed')  # Only the fresh rejection exhausts the send.
        self.assertTrue(self.manager._session.code_consumed)
        self.assertIsNone(self.manager._session.phone)
        self.error('login_not_active', self.manager.submit, CODE)

    def test_third_submission_can_succeed_after_old_invalid_probe(self):
        self.overtaken_submission_probe(3)
        self.step(success())
        self.state('succeeded')

    def test_submission_after_probe_return_before_observe_discards_result(self):
        self.overtaken_submission_probe(3, before_observe=True)
        self.step(success())
        self.state('succeeded')

    def test_overtaken_probe_timeout_cannot_discard_admitted_submission(self):
        self.overtaken_submission_probe(3, timeout=True)
        self.assertEqual(self.manager._session.probe_timeouts, 2)
        self.step(success())
        self.state('succeeded')
        self.assertEqual(self.manager._session.probe_timeouts, 0)

    def test_resend_changes_nonce_and_rejects_old_generation(self):
        self.sent()
        old = self.manager._session.nonce
        self.error('resend_too_early', self.manager.resend)
        self.now += 61
        self.manager.resend()
        self.state('sending')
        self.assertNotEqual(old, self.manager._session.nonce)
        self.step()
        self.step(page(countdown=60))
        self.error('code_consumed', self.manager.submit, CODE, nonce=old)
        self.manager.submit(CODE, nonce=self.manager._session.nonce)
        self.state('submitting')

    def test_resend_after_code_invalid(self):
        self.sent()
        self.submitted()
        self.step(page(code_invalid=True))
        self.now += 61
        self.manager.resend()
        self.state('sending')
        self.assertEqual(self.manager.status()['attempts_left'], 3)

    def test_post_verification_code_rejection_preserves_submission_budget(self):
        for attempt in (1, 2, 3):
            with self.subTest(attempt=attempt):
                self.sent()
                session = self.manager._session
                session.submissions = attempt-1  # Prior attempts within this send.
                self.submitted()
                self.step(page(verification=True))
                self.state('verify_login')
                self.step(page(code_invalid=True))
                self.state('code_invalid' if attempt < 3 else 'failed')
                self.assertEqual(self.manager.status()['attempts_left'], 3-attempt)
                self.assertIsNone(session.frame)
                self.assertIsNone(session.manual_pending)
                if attempt < 3:
                    self.assertFalse(session.code_consumed)
                    self.error('code_consumed', self.manager.submit, CODE)
                    self.error('resend_too_early', self.manager.resend)
                else:
                    self.assertTrue(session.cancelled.is_set())
                    self.assertIsNone(session.phone)
                self.cleanup()
                self.now += 61

    def test_post_verification_code_rejection_allows_resend_and_submit(self):
        self.sent()
        old_nonce = self.manager._session.nonce
        self.submitted()
        self.step(page(verification=True))
        self.step(page(code_invalid=True))
        self.state('code_invalid')
        self.now += 61
        self.manager.resend()
        self.step()
        self.step(page(countdown=60))
        self.state('code_sent')
        self.assertNotEqual(old_nonce, self.manager._session.nonce)
        self.error('code_consumed', self.manager.submit, CODE, nonce=old_nonce)
        self.submitted()
        self.step(success())
        self.state('succeeded')

    def test_countdown_minimum_and_official_longer_countdown(self):
        self.start()
        self.step(page())
        sent_at = self.now
        self.step()
        self.step(page(countdown=2))
        self.assertGreaterEqual(self.manager._session.resend_at, sent_at+60)
        self.step(page(countdown=120))
        self.assertGreaterEqual(self.manager.status()['resend_in'], 120)

    def test_code_sent_idle_and_whole_round_deadlines(self):
        self.sent()
        self.step(page(countdown=0), advance=299)
        self.state('code_sent')
        self.step(advance=1)
        self.state('timeout')
        self.cleanup()
        self.now += 10
        self.start()
        self.step(advance=600)
        self.state('timeout')

    def test_three_consecutive_probe_timeouts_and_reset(self):
        self.start()
        self.step(DriverTimeout())
        self.step(DriverTimeout())
        self.state('opening')
        self.step(page(found={'tab': True}))
        self.assertEqual(self.manager._session.probe_timeouts, 0)
        self.step()  # queued tab action
        for count in (1, 2, 3):
            self.step(DriverTimeout())
            self.state('failed' if count == 3 else 'opening')

    def test_verification_probe_timeouts_do_not_capture_or_fail_early(self):
        self.sending()
        self.step(page(verification=True))
        captures = len(self.driver.actions)
        for index in range(3):
            self.step(DriverTimeout())
            self.state('failed' if index == 2 else 'verify_send')
        self.assertEqual(len(self.driver.actions), captures)

    def test_probe_and_capture_cadences(self):
        self.sending()
        self.step(page(verification=True), advance=0)
        probe, grab = Mock(wraps=self.driver.probe), Mock(wraps=self.driver.grab)
        with patch.object(self.driver, 'probe', probe), patch.object(self.driver, 'grab', grab):
            self.step(advance=.69)
            probe.assert_not_called()
            grab.assert_not_called()
            self.step(advance=.011)
            probe.assert_not_called()
            grab.assert_called_once()
            self.step(advance=.30)
            probe.assert_called_once()

    def test_official_countdown_survives_cancel_and_new_round(self):
        self.sent()
        self.step(page(countdown=120))
        self.cleanup()
        self.now += 61
        self.error('resend_too_early', self.manager.start, PHONE)
        self.now += 60
        self.start()
        self.step(page())
        self.state('sending')

    def test_cancelled_send_verification_restarts_with_a_fresh_send_nonce(self):
        self.manager.solver = None
        self.start()
        self.step(page())
        self.step()
        self.step(self.gap('synthetic-old'))
        old_nonce = self.manager._session.nonce
        self.cleanup()
        self.now += 10
        self.error('resend_too_early', self.manager.start, PHONE)
        self.now += 61
        self.start()
        session = self.manager._session
        with patch.object(self.driver, 'reset_login') as reset:
            self.step(self.gap('synthetic-old'))
            self.state('opening')
            self.assertFalse(session.nonce)
            self.assertIsNone(session.frame)
            self.assertEqual(len(self.manager._send_times), 1)
            self.error('code_consumed', self.manager.submit, CODE)
            self.step()  # Reset is worker-owned, before any new send/input.
            reset.assert_called_once_with(cancel=session.cancelled)
        self.step(page())
        self.state('sending')
        self.assertTrue(session.nonce)
        self.assertNotEqual(session.nonce, old_nonce)
        self.assertEqual(len(self.manager._send_times), 2)
        self.assertEqual(self.manager._day_count, 2)
        self.step()
        self.step(self.gap('synthetic-new'))
        self.step(page(countdown=60))
        self.state('code_sent')
        self.error('code_consumed', self.manager.submit, CODE, nonce=old_nonce)
        self.manager.submit(CODE, nonce=session.nonce)
        self.error('code_consumed', self.manager.submit, CODE)
        self.step()
        self.step(success())
        self.state('succeeded')

    def test_opening_reset_never_adopts_persistent_verification_or_old_countdown(self):
        for observation, expected in ((self.gap(), 'failed'), (page(countdown=60), 'rate_limited')):
            with self.subTest(expected=expected):
                self.start()
                with patch.object(self.driver, 'reset_login') as reset:
                    self.step(self.gap())
                    self.state('opening')
                    self.step()
                    self.step(observation)
                    self.state(expected)
                    reset.assert_called_once()
                self.assertFalse(self.manager._session.nonce)
                self.assertEqual(len(self.manager._send_times), 0)
                self.assertEqual(self.driver.actions, [])
                self.error('login_not_active', self.manager.submit, CODE)
                self.cleanup()
                self.now += 61

    def test_opening_reset_failure_stops_without_sending(self):
        for error in (DriverTimeout(), DriverError('navigation_changed')):
            with self.subTest(error=type(error).__name__):
                self.start()
                with patch.object(self.driver, 'reset_login', side_effect=error):
                    self.step(self.gap())
                    self.step()
                self.state('failed')
                self.assertEqual(len(self.manager._send_times), 0)
                self.assertEqual(self.driver.actions, [])
                self.assertIsNone(self.manager._session.phone)
                self.cleanup()
                self.now += 10

    def test_deadline_callback_is_generation_guarded_and_cancels_inflight_work(self):
        self.start()
        session = self.manager._session
        session.thread = Mock()
        timers = []
        def timer(seconds, callback):
            timer = Mock()
            timers.append((seconds, callback, timer))
            return timer
        with patch('sms_login.threading.Timer', timer):
            self.manager._set(session, 'code_sent')
            self.assertEqual(timers[-1][0], 300)
            stale = timers[-1][1]
            self.now += 299
            self.manager._set(session, 'submitting')
            self.assertEqual(timers[-1][0], 301)
            self.now += 2
            stale()
            self.state('submitting')
            self.now += 299
            timers[-1][1]()
            self.state('timeout')
            self.assertTrue(session.cancelled.is_set())
            self.assertIsNone(session.phone)

    def test_every_active_state_can_fail_cancel_or_timeout(self):
        for source in ('opening', 'sending', 'verify_send', 'code_sent', 'submitting', 'code_invalid', 'verify_login'):
            for final in ('failed', 'cancelled', 'timeout'):
                with self.subTest(source=source, final=final):
                    self.start()
                    session = self.manager._session
                    session.state = source
                    if final == 'failed':
                        self.step(page(error=True))
                    elif final == 'cancelled':
                        self.manager.cancel()
                    else:
                        self.step(advance=600)
                    self.state(final)
                    self.assertIsNone(session.phone)
                    self.assertFalse(session.actions)
                    self.cleanup()
                    self.now += 10

    def test_sending_and_submitting_stall_fail(self):
        for source in ('sending', 'submitting'):
            self.start()
            self.manager._session.state = source
            self.step(page(), advance=16)
            self.state('failed')
            self.cleanup()
            self.now += 10

    def test_phone_code_and_text_validation(self):
        for phone in (None, PHONE+'0', '12800138000', '１３８００１３８０００', PHONE+'\n'):
            self.error('invalid_phone', self.manager.start, phone)
        for code in (None, '123', '123456789', '２４６８１０', CODE+'\n'):
            self.error('invalid_code', self.manager.submit, code)
        for text in ('', '123456789', 'hello world', '\n', '中文'):
            self.error('invalid_text', self.manager.type_into_focused, text)

    def test_nonblocking_fetch_and_coordinator(self):
        self.fetch.acquire()
        self.error('busy_fetching', self.manager.start, PHONE)
        self.fetch.release()
        lease = self.coordinator.acquire()
        self.error('login_busy', self.manager.start, PHONE)
        self.coordinator.release(lease)
        self.start()
        self.error('login_busy', self.manager.start, PHONE)
        self.manager.cancel()
        # Still held until the old worker drains any in-flight browser action.
        self.error('login_busy', self.manager.start, PHONE)
        self.manager._release(self.manager._session)
        self.assertFalse(self.fetch.locked())
        self.error('sms_rate_limited', self.manager.start, PHONE)
        self.now += 10
        self.start()

    def test_start_failure_releases_both_locks(self):
        self.launch.side_effect = RuntimeError(PHONE)
        self.error('login_failed', self.manager.start, PHONE)
        self.assertFalse(self.fetch.locked())
        self.assertIsNone(self.manager._session.phone)
        self.coordinator.release(self.coordinator.acquire())

    def test_hourly_daily_send_counts_and_shanghai_midnight(self):
        # Limits contain timestamps/counts only, independent of phone/session.
        now = self.now
        for i in range(3):
            self.manager._send_allowed(now)
            self.manager._send_times.append(now)
            self.manager._day_count += 1
            now += 61
        self.error('sms_rate_limited', self.manager._send_allowed, now)
        now += 3600
        for i in range(3):
            self.manager._send_allowed(now)
            self.manager._send_times.append(now)
            self.manager._day_count += 1
            now += 61
        now += 3600
        self.error('sms_rate_limited', self.manager._send_allowed, now)
        self.wall = datetime.fromisoformat('2026-11-01T00:00:00+08:00').timestamp()
        self.manager._send_allowed(now)
        self.assertEqual(self.manager._day_count, 0)
        self.assertNotIn(PHONE, repr(self.manager._send_times))

    def test_send_budget_applies_across_rounds_even_cancelled(self):
        self.sent()
        self.cleanup()
        self.now += 10
        self.error('resend_too_early', self.manager.start, PHONE)
        self.assertEqual(self.manager._day_count, 1)

    def test_five_code_and_text_admissions_per_round(self):
        self.sent()
        for index in range(5):
            if index and index % 2 == 0:
                self.now += 61
                self.manager.resend()
                self.step()
                self.step(page(countdown=60))
            self.manager.submit(str(246810+index))
            self.step()
            self.step(page(code_invalid=True))
        self.error('sms_rate_limited', self.manager.submit, '246819')
        session = self.manager._session
        session.state = 'verify_login'
        for _ in range(5):
            self.manager.type_into_focused('A1')
        self.error('sms_rate_limited', self.manager.type_into_focused, 'A1')
        self.assertEqual(session.text_count, 5)

    def test_frame_version_metadata_crop_and_no_leak(self):
        self.sent()
        self.submitted()
        self.step(page(verification=True))
        frame = self.manager.frame()
        self.assertEqual(set(frame), {'frame_id', 'challenge_epoch', 'width', 'height', 'png'})
        self.assertEqual(self.driver.actions[-1], ('grab', dict(x=4, y=14, width=132, height=72)))
        self.step(page(verification=True))
        self.assertEqual(self.manager.frame()['frame_id'], frame['frame_id'])
        self.driver.capture = Capture(b'synthetic-png', FrameMetadata(10, 20, 300, 150, 2, 2))
        self.step(page(verification=True))
        self.assertGreater(self.manager.frame()['frame_id'], frame['frame_id'])
        self.assertGreater(self.manager.status()['challenge_epoch'], frame['challenge_epoch'])
        self.error('stale_frame', self.manager.frame, frame['frame_id'])
        self.error('stale_frame', self.manager.pointer, frame['frame_id'], 'tap', [[.5, .5, 0]])
        self.assertEqual(set(self.manager.status()), {'state', 'phone', 'resend_in', 'attempts_left',
                         'frame_id', 'message', 'verification_mode', 'challenge_epoch', 'frame_width', 'frame_height', 'qr_available'})
        self.assertEqual(self.manager.status()['phone'], '138****8000')
        self.assertNotIn(PHONE, repr(self.manager.status())+repr(self.manager._session))
        self.assertNotIn(CODE, repr(self.manager.status())+repr(self.manager._session))

    def test_capture_fallback_full_view_and_frame_size_bound(self):
        self.start()
        self.manager._session.state = 'verify_send'
        self.driver.current = page(rects={})
        self.manager._capture(self.manager._session)
        self.assertEqual(self.driver.actions[-1], ('grab', None))
        self.driver.capture = Capture(b'x'*(FRAME_PNG_LIMIT+1), FrameMetadata(0, 0, 300, 150))
        with self.assertRaisesRegex(DriverError, 'frame_too_large'):
            self.manager._capture(self.manager._session)

    def gap(self, key='synthetic-challenge'):
        return page(verification=True, challenge_kind='gap_slider', challenge_key=key)

    def enter_gap(self, key='synthetic-challenge'):
        self.step(self.gap(key))
        self.step(self.gap(key))  # A stable challenge epoch, not just animated pixels.

    def test_solver_absent_none_exception_and_path_require_page_evidence(self):
        for mode in ('absent', 'none', 'exception', 'path'):
            with self.subTest(mode=mode):
                solver = None if mode == 'absent' else Mock()
                if solver:
                    solver.solve.return_value = [[.1, .8, 0], [.7, .8, 800]] if mode == 'path' else None
                    if mode == 'exception':
                        solver.solve.side_effect = RuntimeError(PHONE+CODE)
                self.manager.solver = solver
                self.start()
                self.step(page())
                self.step()
                self.enter_gap()
                self.state('verify_send')
                self.assertIsNotNone(self.manager.frame()['png'])
                self.step(page(countdown=60))
                self.submitted()
                self.enter_gap('synthetic-login-challenge')
                self.state('verify_login')
                session = self.manager._session
                if solver:
                    phases = [c.args[1]['phase'] for c in solver.solve.call_args_list]
                    self.assertEqual(phases, ['verify_send', 'verify_login'] if mode == 'path' else ['verify_send'])
                if mode == 'path':
                    self.assertEqual(session.auto_submissions, 2)
                    self.step(self.gap('synthetic-login-challenge'), advance=3)
                self.assertEqual(session.verification_mode, 'manual')
                self.step(success())
                self.state('succeeded')
                self.cleanup()
                self.now += 3600

    def test_three_automatic_submissions_per_round_across_both_phases(self):
        solver = Mock()
        solver.solve.return_value = [[.1, .8, 0], [.7, .8, 800]]
        self.manager.solver = solver
        self.start()
        self.step(page())
        self.step()
        self.enter_gap('synthetic-send')
        self.step(page(countdown=60))
        self.submitted()
        self.enter_gap('synthetic-login-1')
        self.enter_gap('synthetic-login-2')
        self.enter_gap('synthetic-login-3')
        session = self.manager._session
        self.assertEqual(session.auto_submissions, 3)
        self.assertEqual(solver.solve.call_count, 3)
        self.assertEqual(sum(a[0] == 'drag' for a in self.driver.actions), 3)
        self.assertEqual(session.verification_mode, 'manual')
        self.state('verify_login')

    def test_manual_result_timeout_does_not_follow_a_new_puzzle_in_either_phase(self):
        from sms_login import _Action
        self.manager.solver = None
        self.start()
        self.step(page())
        self.step()
        for phase in ('verify_send', 'verify_login'):
            with self.subTest(phase=phase):
                self.step(self.gap('synthetic-first'))
                self.state(phase)
                session = self.manager._session
                old_frame = session.frame_id
                action = _Action('pointer', (old_frame, 'drag', [[.1, .8, 0], [.7, .8, 800]]))
                self.manager._action(session, action)
                self.assertFalse(action.error)
                deadline = session.manual_pending
                self.assertIsNotNone(deadline)
                # The first observation of the next puzzle can arrive after
                # the old result deadline; it is still a new manual challenge.
                self.step(self.gap('synthetic-second'), advance=4)
                self.assertGreater(self.now, deadline)
                self.assertIsNone(session.manual_pending)
                self.assertEqual(session.verification_mode, 'manual')
                self.assertFalse(session.desktop_requested)
                self.assertIsNotNone(self.manager.frame()['png'])
                self.error('stale_frame', self.manager.pointer, old_frame, 'tap', [[.5, .5, 0]])
                self.step(self.gap('synthetic-second'), advance=4)
                self.assertEqual(session.verification_mode, 'manual')
                action = _Action('pointer', (session.frame_id, 'drag', [[.1, .8, 0], [.7, .8, 800]]))
                self.manager._action(session, action)
                self.assertFalse(action.error)
                self.assertGreater(session.manual_pending, deadline)
                if phase == 'verify_send':
                    self.step(page(countdown=60))
                    self.state('code_sent')
                    self.submitted()
                else:
                    self.step(success())
                    self.state('succeeded')

    def test_manual_result_timeout_survives_animation_and_layout_changes(self):
        from sms_login import _Action
        self.manager.solver = None
        self.start()
        self.step(page())
        self.step()
        self.step(self.gap())
        session = self.manager._session
        action = _Action('pointer', (session.frame_id, 'drag', [[.1, .8, 0], [.7, .8, 800]]))
        self.manager._action(session, action)
        self.assertFalse(action.error)
        deadline = session.manual_pending
        self.driver.capture = Capture(b'animated-synthetic-png', self.driver.capture.metadata)
        self.step(self.gap())
        self.assertEqual(session.manual_pending, deadline)
        moved = self.gap()
        moved['rects']['slider_handle'] = dict(x=120, y=30, width=40, height=40)
        self.step(moved)
        self.assertEqual(session.manual_pending, deadline)
        self.step(moved)
        self.assertEqual(session.verification_mode, 'desktop')
        self.assertIsNone(session.frame)

    def test_animation_updates_picture_without_invalidating_manual_input(self):
        self.sending()
        self.step(page(verification=True, challenge_key='synthetic-one'))
        session = self.manager._session
        old = session.frame_id
        epoch = session.challenge_epoch
        self.driver.capture = Capture(b'animated-synthetic-png', self.driver.capture.metadata)
        self.step(page(verification=True, challenge_key='synthetic-one'))
        self.assertGreater(session.frame_id, old)
        self.assertEqual(session.challenge_epoch, epoch)
        self.assertTrue(self.manager._valid_frame(session, old))
        # Retrieval still has picture-version semantics; gestures use epochs.
        self.error('stale_frame', self.manager.frame, old)
        from sms_login import _Action
        action = _Action('pointer', (old, 'tap', [[.5, .5, 0]]))
        self.manager._action(session, action)
        self.assertFalse(action.error)
        self.assertIn(('click', 150, 75), self.driver.actions)
        for index in range(5):
            self.driver.capture = Capture(f'animation-{index}'.encode(), self.driver.capture.metadata)
            self.manager._capture(session)
        self.error('stale_frame', self.manager.pointer, old, 'tap', [[.5, .5, 0]])
        recent = session.frame_id
        self.step(page(verification=True, challenge_key='synthetic-two'))
        self.error('stale_frame', self.manager.pointer, recent, 'tap', [[.5, .5, 0]])

    def test_fallback_order_auto_manual_then_remote_and_rotation_direct(self):
        solver = Mock()
        solver.solve.return_value = [[.1, .8, 0], [.7, .8, 800]]
        self.manager.solver = solver
        self.sending()
        self.enter_gap()
        session = self.manager._session
        self.assertEqual(session.verification_mode, 'auto')
        self.step(self.gap(), advance=3)
        self.assertEqual(session.verification_mode, 'manual')
        from sms_login import _Action
        action = _Action('pointer', (session.frame_id, 'drag', [[.1, .8, 0], [.7, .8, 800]]))
        self.manager._action(session, action)
        self.assertFalse(action.error)
        self.step(self.gap(), advance=3)
        self.assertEqual(session.verification_mode, 'desktop')
        self.assertIn('远程桌面', self.manager.status()['message'])
        self.error('remote_desktop_required', self.manager.frame)
        self.step(success())  # Remote desktop continues in the same official page.
        self.state('succeeded')
        self.cleanup()
        self.now += 61
        solver.reset_mock()
        self.sending()
        before = len(self.driver.actions)
        self.step(page(verification=True, challenge_kind='rotation'))
        self.assertEqual(self.manager._session.verification_mode, 'desktop')
        solver.solve.assert_not_called()
        self.assertEqual(len(self.driver.actions), before)

    def test_unknown_challenges_use_manual_and_stop_signals_prevent_solver(self):
        solver = Mock()
        self.manager.solver = solver
        self.sending()
        self.step(page(verification=True, challenge_kind='click'))
        self.assertEqual(self.manager._session.verification_mode, 'manual')
        solver.solve.assert_not_called()
        self.step(page(verification=True, blocked=True))
        self.state('failed')
        solver.solve.assert_not_called()

    def test_failed_automatic_replay_is_counted_and_never_retried(self):
        solver = Mock()
        solver.solve.return_value = [[.1, .8, 0], [.7, .8, 800]]
        self.manager.solver = solver
        self.sending()
        with patch.object(self.driver, 'drag', side_effect=DriverError('driver_failed')):
            self.enter_gap()
        session = self.manager._session
        self.assertEqual(session.auto_submissions, 1)
        self.assertEqual(session.verification_mode, 'manual')
        self.enter_gap('synthetic-replacement')
        solver.solve.assert_called_once()
        self.assertEqual(session.verification_mode, 'manual')

    def test_automatic_replay_blocks_manual_admission_and_refresh_does_not_retry(self):
        solver = Mock()
        solver.solve.return_value = [[.1, .8, 0], [.7, .8, 800]]
        self.manager.solver = solver
        self.sending()
        self.enter_gap()
        self.error('verification_busy', self.manager.type_into_focused, 'A1')
        self.error('verification_busy', self.manager.pointer, self.manager.status()['frame_id'], 'tap', [[.5, .5, 0]])
        self.manager.refresh_frame()
        self.step(self.gap(), advance=.7)
        solver.solve.assert_called_once()

    def test_layout_or_piece_motion_is_not_a_new_automatic_attempt(self):
        solver = Mock()
        solver.solve.return_value = [[.1, .8, 0], [.7, .8, 800]]
        self.manager.solver = solver
        self.sending()
        self.enter_gap()
        changed = self.gap()
        changed['rects']['slider_piece'] = dict(x=120, y=30, width=40, height=40)
        self.step(changed)
        self.step(changed)
        solver.solve.assert_called_once()
        self.assertEqual(self.manager._session.verification_mode, 'manual')

    def test_page_changed_or_cancelled_during_inference_never_drags(self):
        for changed in (self.gap('synthetic-new'), page(rate_limited=True), success(), 'cancel'):
            self.sending()
            solver = Mock()
            def solve(*args):
                if changed == 'cancel':
                    self.manager.cancel()
                else:
                    self.driver.probes.append(changed)
                return [[.1, .8, 0], [.7, .8, 800]]
            solver.solve.side_effect = solve
            self.manager.solver = solver
            before = sum(a[0] == 'drag' for a in self.driver.actions)
            self.enter_gap()
            self.assertEqual(sum(a[0] == 'drag' for a in self.driver.actions), before)
            self.cleanup()
            self.now += 3600

    def test_queued_code_scrubbed_on_cancel_and_stale_nonce_never_types(self):
        self.sent()
        self.manager.submit(CODE)
        action = self.manager._session.actions[0]
        self.manager.cancel()
        self.assertIsNone(action.value)
        self.assertEqual(action.nonce, b'')
        self.assertTrue(action.done.is_set())
        self.assertEqual([a for a in self.driver.actions if a[0] == 'type_text'], [('type_text', 11)])

    def test_success_callback_is_only_after_official_success(self):
        callback = Mock()
        self.manager._on_success = callback
        self.sent()
        callback.assert_not_called()
        self.submitted()
        self.step(success())
        callback.assert_called_once_with()


class WorkerTests(unittest.TestCase):
    def make_manager(self, driver):
        manager = SmsLoginManager(driver, threading.Lock(), coordinator=LoginCoordinator())
        self.addCleanup(self.stop, manager)
        return manager

    @staticmethod
    def stop(manager):
        manager.cancel()
        if manager._session and manager._session.thread:
            manager._session.thread.join(6)

    def wait_state(self, manager, state, timeout=4):
        deadline = time.monotonic()+timeout
        while manager.status()['state'] != state and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(manager.status()['state'], state)

    def test_slow_probe_does_not_block_commands_and_cancel_retains_lease_until_drained(self):
        entered, release = threading.Event(), threading.Event()
        driver = FakeDriver()
        def probe():
            entered.set()
            release.wait(3)
            return success()
        driver.probe = probe
        manager = self.make_manager(driver)
        started = time.monotonic()
        manager.start(PHONE)
        self.assertLess(time.monotonic()-started, .5)
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        manager.status()
        manager.cancel()
        self.assertLess(time.monotonic()-started, .5)
        self.assertTrue(manager._fetch_lock.locked())
        release.set()
        manager._session.thread.join(2)
        self.assertFalse(manager._fetch_lock.locked())
        self.assertEqual(manager.status()['state'], 'cancelled')

    def test_probe_failure_sanitized_and_releases_locks(self):
        driver = FakeDriver([RuntimeError(PHONE+CODE)])
        manager = self.make_manager(driver)
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        manager.start(PHONE)
        manager._session.thread.join(2)
        self.assertEqual(manager.status()['state'], 'failed')
        self.assertFalse(manager._fetch_lock.locked())
        self.assertNotIn(PHONE, output.getvalue())
        self.assertNotIn(CODE, output.getvalue())

    def test_manual_pointer_mapping_refresh_rate_limit_and_text(self):
        driver = FakeDriver([page(), page(verification=True)])
        driver.capture = Capture(b'synthetic-png', FrameMetadata(10, 20, 200, 100, 2, 2))
        manager = self.make_manager(driver)
        manager.start(PHONE)
        self.wait_state(manager, 'verify_send')
        self.assertEqual(driver.actions[:3], [('click', 70, 50), ('type_text', 11), ('click', 70, 50)])
        del driver.actions[:3]  # Subsequent assertions cover verification input only.
        deadline = time.monotonic()+1
        while not manager.status()['frame_id'] and time.monotonic() < deadline:
            time.sleep(.01)
        frame_id = manager.status()['frame_id']
        for _ in range(3):
            manager.pointer(frame_id, 'tap', [[.5, .5, 0]])
        clicks = [a for a in driver.actions if a[0] == 'click']
        self.assertEqual(clicks, [('click', 35, 32.5)]*3)
        with self.assertRaisesRegex(SmsLoginError, 'sms_rate_limited'):
            manager.pointer(frame_id, 'tap', [[.5, .5, 0]])
        manager._session.pointer_times.clear()
        manager.pointer(frame_id, 'drag', [[0, 0, 0], [1, 1, 160]])
        self.assertIn(('drag', [(10, 20, 0), (60, 45, 160)]), driver.actions)
        manager.type_into_focused('A1')
        deadline = time.monotonic()+1
        while ('type_text', 2) not in driver.actions and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIn(('type_text', 2), driver.actions)
        self.assertGreaterEqual(sum(a[0] == 'grab' for a in driver.actions), 5)

    def test_qr_and_sms_mutually_exclusive_and_qr_fetch_lock(self):
        from qr_login import QrLoginManager
        from hub_bridge import qr_command
        entered, release = threading.Event(), threading.Event()
        manager = self.make_manager(FakeDriver([page(), page(verification=True)]))
        qr = QrLoginManager(browser_mode='headless', coordinator=manager.coordinator,
                            fetch_lock=manager._fetch_lock)
        def qr_run(session):
            entered.set()
            release.wait(3)
            session._set('cancelled')
        with patch.object(qr, '_run', qr_run), patch.dict('os.environ', {}, clear=True):
            qr_session = qr.start()
            self.assertTrue(entered.wait(1))
            with self.assertRaisesRegex(LoginBusy, 'login_busy'):
                manager.start(PHONE)
            self.assertTrue(manager._fetch_lock.locked())
            release.set()
            qr_session.thread.join(2)
            manager.start(PHONE)
            with self.assertRaisesRegex(LoginBusy, 'login_busy'):
                qr.start()
            with patch.dict('os.environ', {'INDEX_HUB_LOGIN_MODE': 'qr'}):
                api = Mock()
                api._qr_manager.return_value = qr
                self.assertEqual(qr_command(api, 'qr_start')['code'], 'login_busy')
            self.stop(manager)
            manager._fetch_lock.acquire()
            try:
                with self.assertRaisesRegex(LoginBusy, 'busy_fetching'):
                    qr.start()
            finally:
                manager._fetch_lock.release()
            # Failed admission leaves no phantom QR session/lease.
            qr_session = qr.start()
            qr_session.thread.join(2)
            self.assertFalse(manager._fetch_lock.locked())


if __name__ == '__main__':
    unittest.main()
