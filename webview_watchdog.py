"""Bound startup time and Linux process-tree RSS; never reinitialise Qt."""
import logging
import os
from pathlib import Path
import time

EXIT_CODE = 75
log = logging.getLogger(__name__)


def bounded_env(name, default, low, high):
    try:
        return max(low, min(high, int(os.environ.get(name, default))))
    except ValueError:
        return default


def process_tree_rss_mb(pid=None):
    """RSS sum (shared pages may be counted twice), including Chromium children."""
    pid = os.getpid() if pid is None else pid
    processes = {}
    for entry in Path('/proc').glob('[0-9]*'):
        try:
            stat = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            processes[int(entry.name)] = (int(stat[1]), int(stat[21]))
        except (OSError, ValueError, IndexError):
            continue
    descendants = {pid}
    while True:
        added = {p for p, (parent, _) in processes.items() if parent in descendants}
        if added <= descendants:
            break
        descendants |= added
    return sum(processes.get(p, (0, 0))[1] for p in descendants) * os.sysconf('SC_PAGE_SIZE') / 1048576


def watch_startup(loaded, release, *, timeout=90, max_rss_mb=2500,
                  clock=time.monotonic, rss=process_tree_rss_mb, exit_fn=os._exit):
    deadline = clock() + timeout
    while not loaded.is_set():
        if clock() >= deadline:
            if loaded.is_set():
                return
            log.error('official window failed to load within %ss; exiting for systemd restart', timeout)
            break
        if rss() > max_rss_mb:
            # Qt may finish loading while /proc is being sampled.
            if loaded.is_set():
                return
            log.error('webview startup RSS exceeded %s MB; exiting for systemd restart', max_rss_mb)
            break
        if loaded.wait(min(0.2, max(0, deadline - clock()))):
            return
    else:
        return
    try:
        release()
    finally:
        exit_fn(EXIT_CODE)
