"""Shared credential redaction and rotating local diagnostics."""
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re


KEY_NAMES = ('cookie|token|session|sessionid|sid_|sid|uid_tt|odin_tt|ttwid|mstoken|a_bogus|'
             'verifyfp|s_v_web_id|csrf|passport|passwd|password|secret|auth|key')


def redact(text) -> str:
    """Redact credential assignments and long opaque strings in diagnostics."""
    text = str(text)
    # Cookie headers contain several assignments; redact the whole header first.
    text = re.sub(
        r"(?i)(?<![\w.-])([\w.-]*cookie[\w.-]*[\"']?[^\S\r\n]*[:=][^\S\r\n]*[\"']?)[^\r\n\"'}]*",
        r'\1***', text)
    text = re.sub(
        r"(?i)(?<![\w.-])([\w.-]*(?:" + KEY_NAMES + r")[\w.-]*[\"']?[^\S\r\n]*[:=][^\S\r\n]*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;\x26}\"']+)",
        r'\1***', text)
    return re.sub(r'[A-Za-z0-9_\-+/=]{24,}', '***', text)


class RedactingFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class RedactingFormatter(logging.Formatter):
    def formatException(self, exc_info):
        return redact(super().formatException(exc_info))

    def format(self, record):
        return redact(super().format(record))


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o640)
        except OSError:
            pass
        return stream


def _env_int(name, default):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def configure_logging(component: str, root: Path):
    directory = Path(os.environ.get('INDEX_HUB_LOG_DIR', Path(root) / 'logs'))
    directory.mkdir(mode=0o750, parents=True, exist_ok=True)
    path = directory / f'{component}.log'
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    if any(getattr(handler, '_index_hub_component', None) == component
           for handler in logger.handlers):
        return path
    file_handler = _PrivateRotatingFileHandler(
        path, maxBytes=_env_int('INDEX_HUB_LOG_MAX_BYTES', 5 * 1024 * 1024),
        backupCount=_env_int('INDEX_HUB_LOG_BACKUPS', 5), encoding='utf-8')
    formatter = RedactingFormatter('%(asctime)s %(levelname)s %(name)s %(message)s')
    for handler in (file_handler, logging.StreamHandler()):
        handler._index_hub_component = component
        handler.addFilter(RedactingFilter())
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return path
