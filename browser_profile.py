"""Private service-owned browser storage and exclusive process locks."""
import fcntl
import logging
import os
from pathlib import Path
import re
import socket
import stat
import threading

from hub_paths import state_dir

logger = logging.getLogger(__name__)
_guard = threading.Lock()
_held = set()
_warned = False


class ProfileError(RuntimeError):
    pass


class ProfileBusy(ProfileError):
    code = 'profile_busy'


def profile_root(root):
    return Path(os.environ.get('INDEX_HUB_PROFILE_DIR', state_dir(root) / 'browser-profiles'))


def persist_enabled():
    global _warned
    value = os.environ.get('INDEX_HUB_PERSIST_LOGIN', '1')
    if value not in ('0', '1'):
        with _guard:
            if not _warned:
                logger.warning('Invalid INDEX_HUB_PERSIST_LOGIN; using 1')
                _warned = True
    return value != '0'


def prepare_profile(root, name):
    if not re.fullmatch('[a-z0-9-]+', name):
        raise ProfileError('Invalid profile name')
    directory = profile_root(root)
    for path in (directory, directory / name):
        try:
            os.makedirs(path, mode=0o700, exist_ok=True)
            info = path.lstat()
        except OSError:
            raise ProfileError('Cannot prepare browser profile') from None
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ProfileError('Browser profile must be an owned directory')
        if info.st_mode & 0o077:
            logger.warning('Tightening browser profile permissions: %s', path)
        os.chmod(path, 0o700)
    return directory / name


class ProfileLock:
    def __init__(self, profile_dir):
        path = Path(profile_dir).absolute()
        self.path = path.parent / ('.' + path.name + '.lock')
        self._fd = None

    def acquire(self):
        with _guard:
            if self.path in _held:
                raise ProfileBusy('profile_busy')
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                    raise ProfileError('Unsafe profile lock')
                os.fchmod(fd, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ProfileBusy('profile_busy') from None
            except BaseException:
                os.close(fd)
                raise
            _held.add(self.path)
            self._fd = fd
        return self

    def release(self):
        with _guard:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
                _held.discard(self.path)

    __enter__ = acquire

    def __exit__(self, *args):
        self.release()


def clear_stale_singleton(profile_dir):
    directory = Path(profile_dir)
    lock = directory / 'SingletonLock'
    if not os.path.lexists(lock):
        return
    alive = False
    try:
        host, pid = os.readlink(lock).rsplit('-', 1)
        pid = int(pid)
        if host == socket.gethostname() and pid > 0:
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                pass
            except PermissionError:
                alive = True
    except (OSError, ValueError):
        pass
    if alive:
        raise ProfileBusy('profile_busy')
    for name in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
        try:
            (directory / name).unlink()
        except FileNotFoundError:
            pass
    logger.info('removed stale Chromium singleton lock')
