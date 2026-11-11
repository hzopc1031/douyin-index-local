import http.cookies
import json
import re
from urllib.parse import urlparse
from urllib.request import Request, urlopen

UA = 'Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36'
REFERER = 'https://www.iesdouyin.com/'


def video_id(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in {'www.douyin.com', 'www.iesdouyin.com'}:
        raise ValueError('不支持的抖音链接')
    match = re.fullmatch(r'/(?:video|note|share/video)/(\d+)/?', parsed.path)
    if not match:
        raise ValueError('无法识别抖音视频 ID')
    return match.group(1)


def resolve_id(url):
    parsed = urlparse(url)
    if parsed.scheme == 'https' and parsed.hostname == 'v.douyin.com':
        with urlopen(Request(url, headers={'User-Agent': UA}), timeout=30) as response:
            url = response.geturl()
    return video_id(url)


def router_data(html):
    match = re.search(r'(?:window\.)?_ROUTER_DATA\s*=\s*', html)
    if not match:
        raise ValueError('分享页缺少视频数据')
    return json.JSONDecoder().raw_decode(html[match.end():].lstrip())[0]


def parse_page(html):
    data = router_data(html)
    for page in data['loaderData'].values():
        items = page.get('videoInfoRes', {}).get('item_list', []) if isinstance(page, dict) else []
        if items:
            item = items[0]
            video = item['video']
            duration = video.get('duration')
            if isinstance(duration, (int, float)) and duration > 1000:
                duration /= 1000
            title = next((line.strip() for line in item.get('desc', '').splitlines() if line.strip()), '')[:80]
            return {'title': title, 'author': item.get('author', {}).get('nickname'),
                    'duration_seconds': duration, 'width': video.get('width'), 'height': video.get('height'),
                    'play_url': video['play_addr']['url_list'][0]}
    raise ValueError('分享页没有视频数据')


def fetch(video_id_):
    body = json.dumps({'region': 'cn', 'aid': 1768, 'needFid': False, 'service': 'www.ixigua.com',
                       'migrate_info': {'ticket': '', 'source': 'node'}, 'cbUrlProtocol': 'https', 'union': True}).encode()
    request = Request('https://ttwid.bytedance.com/ttwid/union/register/', data=body,
                      headers={'Content-Type': 'application/json', 'User-Agent': UA})
    with urlopen(request, timeout=30) as response:
        cookie = http.cookies.SimpleCookie()
        for header in response.headers.get_all('Set-Cookie', []):
            cookie.load(header)
        if 'ttwid' not in cookie:
            raise ValueError('未取得 ttwid')
        ttwid = cookie['ttwid'].value
    request = Request(f'https://www.iesdouyin.com/share/video/{video_id_}/',
                      headers={'User-Agent': UA, 'Referer': REFERER, 'Cookie': 'ttwid=' + ttwid})
    with urlopen(request, timeout=30) as response:
        return parse_page(response.read().decode('utf-8'))
