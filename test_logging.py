"""Synthetic, isolated logging tests; no network or credentials."""
import io
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hub_logging import RedactingFilter, RedactingFormatter, configure_logging, redact


class LoggingTests(unittest.TestCase):
    def test_redact(self):
        text = 'cookie=SECRET token: SECRET Authorization: Basic ' + 'A' * 32
        result = redact(text)
        self.assertNotIn('SECRET', result)
        self.assertNotIn('Basic', result)
        self.assertNotIn('A' * 32, result)
        self.assertIn('cookie=***', result)

    def test_message_args_and_exception(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(RedactingFilter())
        handler.setFormatter(RedactingFormatter('%(message)s'))
        logger = logging.Logger('isolated')
        logger.addHandler(handler)
        try:
            logger.warning('cookie=%s', 'SECRET')
            try:
                raise ValueError('token: SECRET')
            except ValueError:
                logger.exception('failed cookie=%s', 'SECRET')
            record = logging.makeLogRecord({'msg': 'cached exception', 'exc_text': 'password=SECRET',
                                             'levelno': logging.ERROR})
            logger.handle(record)
            output = stream.getvalue()
            self.assertNotIn('SECRET', output)
            self.assertIn('ValueError: token: ***', output)
        finally:
            logger.removeHandler(handler)
            handler.close()

    def test_configuration(self):
        logger = logging.getLogger()
        original_handlers, original_level = logger.handlers[:], logger.level
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
                'INDEX_HUB_LOG_DIR': str(Path(temp) / 'logs'),
                'INDEX_HUB_LOG_MAX_BYTES': '256', 'INDEX_HUB_LOG_BACKUPS': '2'}):
            try:
                path = configure_logging('fixture', Path(temp))
                handlers = logger.handlers[:]
                self.assertEqual(configure_logging('fixture', Path(temp)), path)
                self.assertEqual(logger.handlers, handlers)
                self.assertEqual(path.stat().st_mode & 0o777, 0o640)
                added = [h for h in handlers if h not in original_handlers]
                # File logging only: no unrotated stderr copy unless explicitly enabled.
                self.assertEqual(len(added), 1)
                file_handler = next(h for h in added if isinstance(h, RotatingFileHandler))
                self.assertEqual((file_handler.maxBytes, file_handler.backupCount), (256, 2))
                # Exercise only the added handlers, leaving the test runner's logging alone.
                for handler in added:
                    handler.handle(logging.makeLogRecord({'msg': 'token=%s readable', 'args': ('SECRET',),
                                                         'levelno': logging.INFO, 'levelname': 'INFO'}))
                    handler.flush()
                self.assertIn('readable', path.read_text())
                self.assertNotIn('SECRET', path.read_text())
                file_handler.doRollover()
                self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            finally:
                for handler in logger.handlers[:]:
                    if handler not in original_handlers:
                        logger.removeHandler(handler)
                        handler.close()
                logger.setLevel(original_level)


class StderrAndAuthorizationTests(unittest.TestCase):
    def test_stderr_copy_is_opt_in(self):
        logger = logging.getLogger()
        original = logger.handlers[:]
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
                'INDEX_HUB_LOG_DIR': str(Path(temp) / 'logs'), 'INDEX_HUB_LOG_STDERR': '1'}):
            try:
                configure_logging('stderr-fixture', Path(temp))
                added = [h for h in logger.handlers if h not in original]
                self.assertEqual(len(added), 2)
                self.assertEqual(sum(not isinstance(h, RotatingFileHandler) for h in added), 1)
            finally:
                for handler in logger.handlers[:]:
                    if handler not in original:
                        logger.removeHandler(handler)
                        handler.close()

    def test_authorization_values(self):
        from hub_logging import redact
        for sample, secret in (('Authorization: Basic YWRtaW46cGFzcw==', 'YWRtaW46cGFzcw'),
                               ('Bearer dk_abcdefgh12345', 'dk_abcdefgh12345'),
                               ("{'authorization': 'Bearer idx_short'}", 'idx_short')):
            with self.subTest(sample=sample):
                self.assertNotIn(secret, redact(sample))



class DiagnosticRedactionTests(unittest.TestCase):
    def test_preserves_job_ids_and_diagnostic_keys(self):
        for job in ('cd9733faa68235988c49e957abcdef01', 'abcd13812345678abcdef01234567890'):
            line = f'job_id={job} job={job} completed {job}'
            self.assertEqual(redact(line), line)
        line = ('keyword=人工智能 cache_key=baidu:ai:110000 region_key=110000 '
                'key=all key=region_beijing keyword:object "key": "search" "keyword": "人工智能"')
        self.assertEqual(redact(line), line)

    def test_secret_fields_remain_masked(self):
        for name, value in (('cookie', 'SECRET'), ('token', 'SECRET'),
                            ('password', 'hunter2'), ('passwd', 'hunter2'),
                            ('BDUSS', 'FakeBdussValueNotReal1234567890'),
                            ('api_key', 'SECRET'), ('apikey', 'SECRET'),
                            ('access_key', 'SECRET'), ('secret_key', 'SECRET'),
                            ('private_key', 'SECRET'), ('auth_key', 'SECRET'),
                            ('signing_key', 'SECRET'), ('client_key', 'SECRET'),
                            ('token', 'cd9733faa68235988c49e957abcdef01')):
            with self.subTest(name=name):
                self.assertNotIn(value, redact(f'{name}={value}'))
        for scheme in ('Basic', 'Bearer', 'Digest', 'Token'):
            self.assertNotIn('YWRtaW46cGFzcw==', redact(f'Authorization: {scheme} YWRtaW46cGFzcw=='))
        self.assertEqual(redact('phone 13812345678'), 'phone 1**********')

    def test_identifiers_and_opaque_tokens(self):
        from hub_logging import redact
        line = 'QR login finished state=verification_required stage=close accepted=3'
        self.assertEqual(redact(line), line)
        for token in ('R8cX7pQ2mN9vK4sT6bH1jF5dW0zL3uYe', 'QWxhZGRpbjpvcGVuIHNlc2FtZVhZWg==', 'a=R8cX7pQ2mN9vK4sT6bH1jF5dW0zL3u', 'sessionid=xyz', 'A'*24):
            self.assertIn('***', redact(token))
