"""Private files, count-only quota, profile lock and independently owned Xvfb."""

import asyncio
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import random
import shutil
import signal
import stat
import subprocess
import tempfile
import time

from .errors import Stop
from .models import SHANGHAI


def private_dir(path):
    path = Path(path).expanduser().absolute()
    # Refuse symlink ancestors as well: a private leaf alone is insufficient.
    for part in (path, *path.parents):
        if part.is_symlink():
            raise Stop(5, "directory_symlink")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.stat()
    except OSError:
        raise Stop(5, "directory_unavailable") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Stop(5, "directory_permissions")
    return path


def private_open(path, flags=os.O_WRONLY | os.O_CREAT | os.O_TRUNC):
    try:
        fd = os.open(path, (flags & ~os.O_TRUNC) | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        raise Stop(5, "file_unavailable") from None
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        os.close(fd)
        raise Stop(5, "file_permissions")
    if flags & os.O_TRUNC:
        os.ftruncate(fd, 0)
    return fd


def atomic_text(path, text):
    path = Path(path)
    if path.is_symlink():
        raise Stop(5, "file_symlink")
    fd, temp = tempfile.mkstemp(prefix=".dyc-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        parent = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Profile:
    def __init__(self, path, clear_stale=True):
        self.path = Path(path)
        self.fd = None
        self.clear_stale = clear_stale

    def __enter__(self):
        self.path = private_dir(self.path)
        self.fd = private_open(self.path / ".dyc.lock", os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise Stop(5, "profile_locked") from None
        try:
            # This profile is exclusively ours. Never touch any other profile.
            singleton = self.path / "SingletonLock"
            if singleton.is_symlink():
                host, _, pid = os.readlink(singleton).rpartition("-")
                if host != os.uname().nodename or not pid.isdigit():
                    raise Stop(5, "profile_lock_unknown")
                if Path(f"/proc/{pid}").exists():
                    raise Stop(5, "profile_browser_alive")
            for entry in self.path.glob("Singleton*") if self.clear_stale else ():
                if entry.is_symlink() or entry.is_file():
                    entry.unlink()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def mem_available(path="/proc/meminfo"):
    try:
        for line in Path(path).read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    raise Stop(5, "memory_unknown")


def check_memory(minimum=2 * 1024**3, path="/proc/meminfo"):
    available = mem_available(path)
    if available < minimum:
        raise Stop(5, "low_memory")
    return available


class DailyCounter:
    def __init__(self, state, today=None, limit=3000):
        self.state = private_dir(state)
        self.today = today or (lambda: datetime.now(SHANGHAI).date().isoformat())
        self.limit = min(limit, 3000)

    @contextmanager
    def locked(self):
        fd = private_open(self.state / ".usage.lock", os.O_RDWR | os.O_CREAT)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Stop(5, "usage_locked") from None
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self, day=None):
        day = day or self.today()
        path = self.state / "usage.json"
        if not path.exists() and not path.is_symlink():
            return 0
        try:
            fd = private_open(path, os.O_RDONLY)
            with os.fdopen(fd) as stream:
                data = json.load(stream)
            if (set(data) != {"day", "count"} or type(data["count"]) is not int
                    or data["count"] < 0 or not isinstance(data["day"], str)):
                raise ValueError
            datetime.strptime(data["day"], "%Y-%m-%d")
            if data["day"] > day:
                raise ValueError
            return data["count"] if data["day"] == day else 0
        except (OSError, ValueError, TypeError):
            raise Stop(5, "usage_corrupt") from None

    def usage(self):
        with self.locked():
            return self._read()

    def check_start(self):
        if self.usage() >= self.limit:
            raise Stop(5, "daily_cap_at_start")

    def reserve(self):
        with self.locked():
            day = self.today()
            count = self._read(day)
            if count >= self.limit:
                raise Stop(2, "daily_cap")
            atomic_text(self.state / "usage.json", json.dumps(
                {"day": day, "count": count + 1}) + "\n")


class Budget:
    def __init__(self, counter, batch_limit=1500, video_limit=1500):
        self.counter = counter
        self.limit = min(batch_limit, 1500)
        self.video_limit = min(video_limit, 1500)
        self.used = self.video_used = 0

    def new_video(self):
        self.video_used = 0

    def reserve(self):
        if self.used >= self.limit:
            raise Stop(2, "batch_cap")
        if self.video_used >= self.video_limit:
            raise Stop(2, "video_page_budget")
        self.counter.reserve()
        self.used += 1
        self.video_used += 1


class Pacer:
    def __init__(self, clock=time.monotonic, sleep=asyncio.sleep, rng=None):
        self.clock, self.sleep = clock, sleep
        self.rng = rng or random.SystemRandom()
        self.ready = 0
        self.count = 0

    async def before(self):
        delay = self.ready - self.clock()
        if delay > 0:
            await self.sleep(delay)

    def completed(self):
        self.count += 1
        delay = self.rng.uniform(2, 4)
        if self.count % 15 == 0:
            delay += self.rng.uniform(8, 15)
        self.ready = self.clock() + delay

    async def between_videos(self):
        await self.before()
        await self.sleep(self.rng.uniform(20, 40))


class Display:
    def __init__(self, supplied=None):
        self.supplied = supplied
        self.process = None
        self.name = supplied

    async def start(self):
        if self.supplied:
            return self.name
        if not shutil.which("Xvfb"):
            raise Stop(5, "xvfb_missing")
        read_fd, write_fd = os.pipe()
        try:
            self.process = subprocess.Popen(
                ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1280x900x24", "-nolisten", "tcp"],
                pass_fds=(write_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        finally:
            os.close(write_fd)
        os.set_blocking(read_fd, False)
        try:
            buffer = b""
            for _ in range(100):
                try:
                    buffer += os.read(read_fd, 64)
                except BlockingIOError:
                    pass
                if b"\n" in buffer:
                    number = buffer.strip().decode("ascii")
                    if number.isdigit():
                        self.name = ":" + number
                        return self.name
                    break
                if self.process.poll() is not None:
                    break
                await asyncio.sleep(0.05)
            raise Stop(5, "xvfb_start_failed")
        finally:
            os.close(read_fd)

    async def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            for _ in range(20):
                if self.process.poll() is not None:
                    return
                await asyncio.sleep(0.025)
            self.process.kill()
            self.process.wait(timeout=1)


def browser_defaults():
    if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ and Path("/opt/ms-playwright").is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/ms-playwright"
