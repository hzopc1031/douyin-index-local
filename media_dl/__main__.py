import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from . import asr as transcription, douyin, xiaohongshu
from .safety import safe_log

INTERVAL = 3


def download(url, out, item_id, referer=douyin.REFERER):
    if urlparse(url).scheme != 'https':
        raise ValueError('播放地址无效')
    candidates = [url.replace('/playwm/', '/play/', 1), url] if '/playwm/' in url else [url]
    for candidate in candidates:
        name = None
        try:
            request = Request(candidate, headers={'User-Agent': douyin.UA, 'Referer': referer})
            with urlopen(request, timeout=60) as response, tempfile.NamedTemporaryFile(dir=out, delete=False) as temp:
                name = temp.name
                content_type = response.headers.get_content_type()
                first = response.read(4096)
                temp.write(first)
                while chunk := response.read(1024 * 1024):
                    temp.write(chunk)
                size = temp.tell()
            is_mp4 = len(first) >= 12 and first[4:8] == b'ftyp'
            is_webm = first.startswith(b'\x1a\x45\xdf\xa3')
            looks_html = first.lstrip().lower().startswith((b'<!doctype html', b'<html'))
            if size < 1024 or looks_html or not (content_type.startswith('video/') or is_mp4 or is_webm):
                raise ValueError('下载内容不是视频')
            ext = '.webm' if is_webm or content_type == 'video/webm' else '.mp4'
            dest = out / (item_id + ext)
            os.replace(name, dest)
            return dest.name
        except Exception:
            if name and os.path.exists(name):
                os.unlink(name)
    raise ValueError('视频下载失败')


def process(url, out, asr=False, model='base', proxy=None):
    platform = 'xiaohongshu' if xiaohongshu.is_xhs(url) else 'douyin'
    item_id = hashlib.sha256(url.encode()).hexdigest()[:16]
    stored_url = urlunsplit(urlsplit(url)._replace(query='', fragment='')) if platform == 'xiaohongshu' else url
    meta = dict(id=item_id, platform=platform, url=stored_url, title=None, author=None,
                duration_seconds=None, width=None, height=None,
                fetched_at=datetime.now(timezone.utc).isoformat(), video_file=None,
                subtitle_file=None, subtitle_source='none', error=None)
    try:
        if platform == 'xiaohongshu':
            item_id, info, video_file = xiaohongshu.fetch(url, out, proxy=proxy)
            meta['id'] = item_id
            meta.update(info)
            meta['video_file'] = video_file
        else:
            item_id = douyin.resolve_id(url)
            meta['id'] = item_id = re.sub(r'[^a-zA-Z0-9_-]', '_', item_id)[:100]
            info = douyin.fetch(item_id)
            play_url = info.pop('play_url')
            meta.update(info)
            meta['video_file'] = download(play_url, out, item_id)
        if asr:
            try:
                subtitle = out / (item_id + '.srt')
                transcription.transcribe(out / meta['video_file'], subtitle, model)
                meta['subtitle_file'] = subtitle.name
                meta['subtitle_source'] = 'asr'
            except ImportError:
                meta['error'] = '转写失败：未安装 faster-whisper'
            except Exception:
                meta['error'] = '转写失败'
    except Exception as exc:
        if platform == 'xiaohongshu' and str(exc) == xiaohongshu.LOGIN_REQUIRED:
            meta['error'] = str(exc)
        else:
            meta['error'] = '链接解析或下载失败'
    (out / (item_id + '.meta.json')).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return meta


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'login':
        parser = argparse.ArgumentParser(description='登录平台')
        parser.add_argument('platform', choices=('xhs',))
        parser.add_argument('--proxy')
        args = parser.parse_args(sys.argv[2:])
        return xiaohongshu.login(proxy=args.proxy)
    parser = argparse.ArgumentParser(description='批量下载视频')
    parser.add_argument('urls', nargs='*')
    parser.add_argument('--input')
    parser.add_argument('--proxy')
    parser.add_argument('--out', default='./media-dl-out')
    parser.add_argument('--asr', action='store_true')
    parser.add_argument('--asr-model', choices=('base', 'small'), default='base')
    args = parser.parse_args()
    urls = list(args.urls)
    if args.input:
        try:
            urls.extend(line.strip() for line in Path(args.input).read_text(encoding='utf-8').splitlines()
                        if line.strip() and not line.lstrip().startswith('#'))
        except OSError:
            parser.error('无法读取输入文件')
    if not urls:
        parser.error('请提供链接或输入文件')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    failed = False
    for index, url in enumerate(urls):
        if index:
            time.sleep(INTERVAL)
        result = process(url, out, args.asr, args.asr_model, args.proxy)
        failed |= bool(result['error'])
        print(safe_log(f"{result['id']}: {'失败' if result['error'] else '完成'}"))
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
