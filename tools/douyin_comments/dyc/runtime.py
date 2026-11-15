"""Private files, count-only quota, profile lock and independently owned Xvfb."""

import asyncio
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import random
import secrets
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time

from .errors import Stop
from .models import SHANGHAI
from .file_lock import acquire as acquire_lock, release as release_lock

_MACOS = sys.platform == 'darwin'
_WINDOWS = sys.platform == 'win32'

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
    if _WINDOWS:
        from .windows_private import replace_private_file
        replace_private_file(path, text.encode('utf-8'))
        return
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
            acquire_lock(self.fd)
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
                if _MACOS:
                    try:
                        os.kill(int(pid), 0)
                    except ProcessLookupError:
                        alive = False
                    except (PermissionError, OSError):
                        alive = True
                    else:
                        alive = True
                else:
                    alive = Path(f"/proc/{pid}").exists()
                if alive:
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
            try:
                release_lock(self.fd)
            finally:
                os.close(self.fd)
                self.fd = None


def mem_available(path="/proc/meminfo"):
    if (_MACOS or _WINDOWS) and path == '/proc/meminfo':
        try:
            import psutil
        except ImportError:
            raise Stop(5, "memory_unknown") from None
        try:
            return psutil.virtual_memory().available
        except (psutil.Error, OSError, ValueError):
            raise Stop(5, "memory_unknown") from None
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


# Hub 显式启用新额度，CLI 保留原来的默认额度。
BATCH_LIMIT = 1500
DAILY_LIMIT = 3000
HUB_GAP_MIN = 0.6
HUB_GAP_MAX = 1.0
HUB_REST_EVERY = 50
HUB_REST_MIN = 1
HUB_REST_MAX = 2
HUB_VIDEO_GAP_MIN = 1
HUB_VIDEO_GAP_MAX = 2


class DailyCounter:
    def __init__(self, state, today=None, limit=150):
        self.state = private_dir(state)
        self.today = today or (lambda: datetime.now(SHANGHAI).date().isoformat())
        self.limit = min(limit, DAILY_LIMIT)

    @contextmanager
    def locked(self):
        fd = private_open(self.state / ".usage.lock", os.O_RDWR | os.O_CREAT)
        acquired = False
        try:
            try:
                acquire_lock(fd)
                acquired = True
            except BlockingIOError:
                raise Stop(5, "usage_locked") from None
            yield
        finally:
            try:
                if acquired:
                    release_lock(fd)
            finally:
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
    def __init__(self, counter, batch_limit=25, video_limit=25):
        self.counter = counter
        self.limit = min(batch_limit, BATCH_LIMIT)
        self.video_limit = min(video_limit, BATCH_LIMIT)
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
    gap = (2, 4)
    rest_every = 15
    rest = (8, 15)
    video_gap = (20, 40)

    def __init__(self, clock=time.monotonic, sleep=asyncio.sleep, rng=None):
        self.clock, self.sleep = clock, sleep
        self.rng = rng or random.SystemRandom()
        self.ready = 0
        self.count = 0

    async def before(self):
        delay = self.ready - self.clock()
        if delay > 0:
            await self.sleep(delay)

    def completed(self, started=None):
        self.count += 1
        delay = self.rng.uniform(*self.gap)
        if self.count % self.rest_every == 0:
            delay += self.rng.uniform(*self.rest)
        # Hub counts request time inside the gap; CLI keeps its post-response gap.
        self.ready = (self.clock() if started is None else started) + delay

    async def between_videos(self):
        await self.before()
        await self.sleep(self.rng.uniform(*self.video_gap))


class HubPacer(Pacer):
    gap = (HUB_GAP_MIN, HUB_GAP_MAX)
    rest_every = HUB_REST_EVERY
    rest = (HUB_REST_MIN, HUB_REST_MAX)
    video_gap = (HUB_VIDEO_GAP_MIN, HUB_VIDEO_GAP_MAX)


class Display:
    def __init__(self, supplied=None):
        self.supplied = supplied
        self.process = None
        self.name = supplied
        self.authority = None
        self._auth_dir = None

    def environment(self):
        if (_MACOS or _WINDOWS) and not self.supplied:
            return {}
        env = {"DISPLAY": self.name}
        if self.authority is not None:
            env["XAUTHORITY"] = str(self.authority)
        return env

    async def start(self):
        if self.supplied:
            return self.name
        if _MACOS or _WINDOWS:
            return None
        if not shutil.which("Xvfb"):
            raise Stop(5, "xvfb_missing")
        read_fd = write_fd = None
        try:
            self._auth_dir = tempfile.TemporaryDirectory(prefix="dyc-xauth-")
            self.authority = Path(self._auth_dir.name) / "Xauthority"
            # Xauthority's binary format: FamilyWild and an empty display
            # number allow -displayfd to choose a display after auth is ready.
            # The random cookie is private to this server and its Chromium.
            fields = (b"", b"", b"MIT-MAGIC-COOKIE-1", secrets.token_bytes(16))
            record = struct.pack("!H", 65535) + b"".join(
                struct.pack("!H", len(value)) + value for value in fields)
            with os.fdopen(private_open(self.authority, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as stream:
                stream.write(record)
            read_fd, write_fd = os.pipe()
            self.process = subprocess.Popen(
                ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1280x900x24",
                 "-nolisten", "tcp", "-auth", str(self.authority)],
                pass_fds=(write_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            os.close(write_fd)
            write_fd = None
            os.set_blocking(read_fd, False)
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
        except BaseException:
            await self.close()
            raise
        finally:
            for fd in (read_fd, write_fd):
                if fd is not None:
                    os.close(fd)

    async def close(self):
        try:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                for _ in range(20):
                    if self.process.poll() is not None:
                        break
                    await asyncio.sleep(0.025)
                else:
                    self.process.kill()
                    self.process.wait(timeout=1)
        finally:
            if self._auth_dir is not None:
                self._auth_dir.cleanup()
                self._auth_dir = None


def browser_defaults():
    if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ and Path("/opt/ms-playwright").is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/ms-playwright"
