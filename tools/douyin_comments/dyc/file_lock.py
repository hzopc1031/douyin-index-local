"""Nonblocking exclusive file locks; callers own descriptor lifetime."""
import os


def acquire(fd, *, windows=None, backend=None):
    windows = os.name == 'nt' if windows is None else windows
    if windows:
        if backend is None:
            import msvcrt as backend
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            backend.locking(fd, backend.LK_NBLCK, 1)
        except OSError:
            raise BlockingIOError('lock unavailable') from None
    else:
        if backend is None:
            import fcntl as backend
        backend.flock(fd, backend.LOCK_EX | backend.LOCK_NB)


def release(fd, *, windows=None, backend=None):
    windows = os.name == 'nt' if windows is None else windows
    if windows:
        if backend is None:
            import msvcrt as backend
        os.lseek(fd, 0, os.SEEK_SET)
        backend.locking(fd, backend.LK_UNLCK, 1)
    else:
        if backend is None:
            import fcntl as backend
        backend.flock(fd, backend.LOCK_UN)
