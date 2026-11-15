"""Small machine-readable local entry point shared by desktop integrations."""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from datetime import date, timedelta
from pathlib import Path

from hotlist_client import HotListClient, HotListError
from storage import ResultStore


def main(argv=None, *, stdout=None, stderr=None, client_factory=HotListClient):
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    parser = argparse.ArgumentParser(description='本机数据工具（当前支持抖音热榜）')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('hotlist', help='获取一次抖音热榜')
    cache = sub.add_parser('index-cache', help='读取单词七天的完整本地缓存')
    cache.add_argument('--keyword', required=True)
    cache.add_argument('--end-date', required=True)
    comments = sub.add_parser('comment-summary', help='读取指定本地评论摘要')
    comments.add_argument('--run-id', required=True)
    args = parser.parse_args(argv)
    if args.command == 'comment-summary':
        try:
            if not re.fullmatch(r'\d{8}-\d{6}-[0-9a-f]{10}', args.run_id):
                raise ValueError('invalid or unsupported')
            base = Path.home() / 'dyc-output'
            if os.name == 'nt':
                # Windows requires ACL and reparse checks, not POSIX mode bits.
                source = str(Path(__file__).resolve().parent / 'tools' / 'douyin_comments')
                if source not in sys.path:
                    sys.path.insert(0, source)
                from dyc.errors import Stop
                from dyc.windows_private import read_private_file, verify_private_directory
                try:
                    verify_private_directory(base)
                    raw = read_private_file(base / args.run_id / 'summary.md')
                except Stop:
                    raise ValueError('private file unavailable') from None
            else:
                if any(part.is_symlink() for part in (base, *base.parents)):
                    raise ValueError('symlink')
                base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    folder_fd = os.open(args.run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base_fd)
                    try:
                        for directory_fd in (base_fd, folder_fd):
                            info = os.fstat(directory_fd)
                            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                                raise ValueError('directory permissions')
                        fd = os.open('summary.md', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=folder_fd)
                        with os.fdopen(fd, 'rb') as stream:
                            info = os.fstat(stream.fileno())
                            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                                raise ValueError('permissions')
                            raw = stream.read(256 * 1024 + 1)
                    finally:
                        os.close(folder_fd)
                finally:
                    os.close(base_fd)
            if len(raw) > 256 * 1024:
                raise ValueError('too large')
            text = raw.decode('utf-8')
            print(json.dumps({'ok': True, 'data': {'run_id': args.run_id, 'summary': text}}, ensure_ascii=True), file=stdout)
            return 0
        except (ValueError, OSError, UnicodeError):
            print(json.dumps({'ok': False, 'error': {'code': 'summary_unavailable'}}, ensure_ascii=True), file=stderr)
            return 1
    if args.command == 'index-cache':
        try:
            end = date.fromisoformat(args.end_date)
            if end.isoformat() != args.end_date:
                raise ValueError('invalid date')
            start = end - timedelta(days=6)
            store = ResultStore(Path(__file__).resolve().parent / 'results')
            hit = store.lookup([args.keyword], start.isoformat(), end.isoformat())
            if hit is None:
                print(json.dumps({'ok': False, 'error': {'code': 'cache_miss'}}, ensure_ascii=True), file=stderr)
                return 1
            result = hit['result']
            metadata = result['metadata']
            rows = [{key: row.get(key) for key in ('keyword', 'date', 'search_index', 'composite_index')}
                    for row in result['rows']]
            data = {'keyword': args.keyword, 'start_date': start.isoformat(), 'end_date': end.isoformat(),
                    'fetched_at': metadata['fetched_at'], 'rows': rows}
            print(json.dumps({'ok': True, 'data': data}, ensure_ascii=True, allow_nan=False), file=stdout)
            return 0
        except (ValueError, OverflowError, OSError):
            print(json.dumps({'ok': False, 'error': {'code': 'invalid_or_unavailable'}}, ensure_ascii=True), file=stderr)
            return 1
    client = client_factory()
    try:
        result = client.fetch()
    except HotListError as exc:
        print(json.dumps({'ok': False, 'error': exc.as_dict()}, ensure_ascii=True), file=stderr)
        return 1
    finally:
        client.close()
    print(json.dumps({'ok': True, 'data': result}, ensure_ascii=True, allow_nan=False), file=stdout)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
