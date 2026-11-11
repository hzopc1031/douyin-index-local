import json
import os
from pathlib import Path
import re
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from .douyin import UA


def is_xhs(url):
    host = urlparse(url).hostname or ''
    return any(host == domain or host.endswith('.' + domain) for domain in
               ('xiaohongshu.com', 'xhslink.com', 'xhslink.cn', 'rednote.com'))


def cookie():
    value = os.environ.get('XHS_COOKIE')
    if value:
        return value.strip()
    path = os.environ.get('XHS_COOKIE_FILE')
    if path:
        return Path(path).read_text(encoding='utf-8').strip()
    return ''


def resolve_id(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not is_xhs(url):
        raise ValueError('不支持的小红书链接')
    if parsed.hostname in ('xhslink.com', 'www.xhslink.com', 'xhslink.cn', 'www.xhslink.cn'):
        with urlopen(Request(url, headers={'User-Agent': UA}), timeout=30) as response:
            url = response.geturl()
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not is_xhs(url):
            raise ValueError('短链跳转地址无效')
    match = re.fullmatch(r'/(?:explore|discovery/item)/([a-zA-Z0-9_-]+)/?', parsed.path)
    if not match:
        raise ValueError('无法识别小红书笔记 ID')
    return match.group(1)


def parse_page(html, item_id):
    match = re.search(r'window\.__INITIAL_STATE__\s*=\s*', html)
    if not match:
        raise ValueError('笔记页缺少数据')
    state = json.JSONDecoder().raw_decode(html[match.end():].lstrip())[0]
    note = state['note']['noteDetailMap'][item_id]['note']
    video = note['video']
    streams = video['media']['stream']
    stream = next(entry for entries in streams.values() for entry in entries if entry.get('masterUrl') or entry.get('master_url'))
    duration = video.get('duration')
    if isinstance(duration, (int, float)) and duration > 1000:
        duration /= 1000
    return {'title': note.get('title') or note.get('desc', '').split('\n')[0][:80],
            'author': note.get('user', {}).get('nickname'), 'duration_seconds': duration,
            'width': stream.get('width'), 'height': stream.get('height'),
            'play_url': stream.get('masterUrl') or stream.get('master_url')}


def fetch(item_id, cookie_value):
    request = Request(f'https://www.xiaohongshu.com/explore/{item_id}',
                      headers={'User-Agent': UA, 'Cookie': cookie_value})
    with urlopen(request, timeout=30) as response:
        return parse_page(response.read().decode('utf-8'), item_id)
