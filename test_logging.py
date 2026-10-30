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
                self.assertEqual(len(added), 2)
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
