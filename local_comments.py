"""Single owned local comment job; never exposes browser credentials or raw logs."""
from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
import threading
from pathlib import Path

MAX_OUTPUT = 1024 * 1024


def _dependencies_available():
    return all(importlib.util.find_spec(name) is not None for name in ('playwright', 'psutil'))


class LocalCommentJob:
    def __init__(self, root, *, popen=subprocess.Popen, platform=None, frozen=None, on_finished=None,
                 dependency_check=_dependencies_available):
        self.root = Path(root)
        self.popen = popen
        self.platform = sys.platform if platform is None else platform
        self.frozen = getattr(sys, 'frozen', False) if frozen is None else frozen
        self.on_finished = on_finished
        self.dependency_check = dependency_check
        self._guard = threading.Lock()
        self._process = None
        self._state = {'state': 'idle'}

    def start(self, text):
        if self.platform != 'darwin':
            return {'ok': False, 'code': 'platform_unsupported', 'message': '本机评论采集目前仅提供 macOS 实验入口。'}
        if self.frozen:
            return {'ok': False, 'code': 'source_required', 'message': '请通过源码和项目虚拟环境启动评论工具。'}
        if not self.dependency_check():
            return {'ok': False, 'code': 'dependencies_missing', 'message': '请先安装评论工具依赖和 Playwright Chromium，步骤见项目 README。'}
        if not isinstance(text, str) or len(text) > 10000:
            return {'ok': False, 'code': 'invalid_urls', 'message': '请每行填写一个视频链接。'}
        urls = [line.strip() for line in text.splitlines() if line.strip()]
        if not 1 <= len(urls) <= 10:
            return {'ok': False, 'code': 'invalid_urls', 'message': '请填写 1—10 个视频链接。'}
        from dyc.site import parse_url
        from dyc.errors import Stop
        try:
            urls = [parse_url(url).url for url in urls]
        except Stop:
            return {'ok': False, 'code': 'invalid_urls', 'message': '存在无法识别的视频链接。'}
        with self._guard:
            if self._process is not None:
                return {'ok': False, 'code': 'busy', 'message': '已有评论任务正在运行。'}
            try:
                process = self.popen([sys.executable, '-m', 'dyc', 'fetch-local', *urls],
                    cwd=str(self.root / 'tools' / 'douyin_comments'), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False)
            except (OSError, ValueError):
                return {'ok': False, 'code': 'start_failed', 'message': '无法启动评论工具，请检查本地依赖。'}
            self._process = process
            self._state = {'state': 'running'}
            try:
                threading.Thread(target=self._collect, args=(process,), daemon=True, name='local-comments').start()
            except RuntimeError:
                self._process = None
                self._state = {'state': 'stopped', 'code': 'local_process_error'}
                try:
                    process.terminate()
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                        process.wait(timeout=2)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                finally:
                    if process.stdout:
                        process.stdout.close()
                return {'ok': False, 'code': 'start_failed', 'message': '无法启动评论任务监控。'}
        return {'ok': True, 'state': 'running'}

    def _collect(self, process):
        content = bytearray()
        oversized = False
        try:
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                if len(content) + len(chunk) <= MAX_OUTPUT:
                    content.extend(chunk)
                else:
                    oversized = True
            code = process.wait()
            data = json.loads(content.decode('utf-8')) if not oversized else None
            if not isinstance(data, dict) or type(data.get('exit_code')) is not int or data['exit_code'] != code:
                raise ValueError('invalid output')
            reason = data.get('exit_reason')
            if not isinstance(reason, str) or not reason.isascii() or not reason.replace('_', '').isalnum() or len(reason) > 80:
                raise ValueError('invalid reason')
            out = data.get('out')
            if out is not None and (not isinstance(out, str) or len(out) > 4096):
                raise ValueError('invalid output path')
            state = {'state': 'complete' if code == 0 else 'stopped', 'code': reason,
                     'exit_code': code, 'out': out}
            if reason == 'partial' and isinstance(data.get('videos'), list):
                allowed = {'comment_template_missing', 'short_link_unresolved'}
                details = [v.get('reason') for v in data['videos'] if isinstance(v, dict)]
                state['details'] = sorted({item for item in details if isinstance(item, str) and item in allowed})
        except Exception:
            state = {'state': 'stopped', 'code': 'local_process_error'}
        finally:
            if process.stdout:
                try:
                    process.stdout.close()
                except OSError:
                    pass
        with self._guard:
            if self._process is process:
                self._process = None
                self._state = state
        if self.on_finished is not None:
            self.on_finished()

    def status(self):
        with self._guard:
            return dict(self._state)

    def stop(self):
        with self._guard:
            process = self._process
            if process is None:
                return {'ok': False, 'code': 'not_running'}
            try:
                process.terminate()
            except OSError:
                return {'ok': False, 'code': 'stop_failed'}
        return {'ok': True}

    def close(self):
        with self._guard:
            process = self._process
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            # Preserve exact process identities before forced shutdown; never
            # terminate an unrelated process if a PID has been recycled.
            descendants = []
            try:
                import psutil
            except ImportError:
                psutil = None
            if psutil is not None:
                try:
                    parent = psutil.Process(process.pid)
                    for child in parent.children(recursive=True):
                        try:
                            descendants.append((child.pid, child.create_time()))
                        except psutil.Error:
                            continue
                except (OSError, psutil.Error):
                    pass
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
            for pid, created in descendants:
                try:
                    child = psutil.Process(pid)
                    if child.create_time() == created:
                        child.kill()
                except (OSError, psutil.Error):
                    continue
        except OSError:
            pass
