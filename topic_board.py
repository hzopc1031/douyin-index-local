"""Hotspot-topic read contract. No network, no retries, no invented rows.

Live collection waits for the server Qt browser login. Until that integration
lands, GET only returns snapshots already stored in topic_snapshots. An empty
board stays empty: missing heat is null, never zero. Category names are the
filter vocabulary and must be checked against the live 热点宝 page during that
integration; they are not collected topics.
"""

import os

from hub_store import HubError

TOPIC_NOTE = (
    '热点宝话题不是搜索指数，也不是热搜榜热度。'
    '采集待服务器 Qt 浏览器登录后联调，当前不抓取、不伪造话题。'
    '缺失为 null，不补 0。遇到验证码或限频应停止，且不自动重试。'
)

# Local filter labels only. Recalibrate this tuple against the live page
# when the Qt session is wired; do not fill an empty board with sample topics.
TOPIC_CATEGORIES = (
    '全部', '美食', '旅行', '泛生活', '娱乐', '颜值', '才艺', '剧情', '影视',
    '二次元', '游戏', '体育', '财经', '科技', '教育', '母婴', '时尚', '三农',
    '动物', '汽车', '医疗健康', '法律', '情感', '社会', '时政',
)
TOPIC_RANKS = {'total': '总榜', 'rising': '飙升'}
TOPIC_WINDOW = '1d'


def topic_enabled():
    """Exact 1 marks the board as waiting for login. It does not start a fetch."""
    return os.environ.get('INDEX_HUB_TOPIC_ENABLED') == '1'


def validate_topic_query(query):
    allowed = {'category', 'rank', 'window'}
    if set(query) - allowed or any(len(query.getall(key)) != 1 for key in query):
        raise HubError('invalid_parameters', '话题榜只接受 category、rank、window。')
    category = query.get('category', '全部')
    rank = query.get('rank', 'total')
    window = query.get('window', TOPIC_WINDOW)
    if category not in TOPIC_CATEGORIES:
        raise HubError('invalid_parameters', '垂类不在可筛选范围内。')
    if rank not in TOPIC_RANKS:
        raise HubError('invalid_parameters', '榜单只能是总榜或飙升。')
    if window != TOPIC_WINDOW:
        raise HubError('invalid_parameters', '当前只支持近1天。')
    return category, rank, window


def topic_view(snapshot, *, enabled):
    if snapshot['items']:
        status, label = 'available', '已保存'
    elif not enabled:
        status, label = 'disabled', '未启用'
    else:
        status, label = 'not_logged_in', '未登录'
    return {'status': status, 'status_label': label, 'window': snapshot['window'],
            'window_label': '近1天', 'rank': snapshot['rank'], 'rank_label': TOPIC_RANKS[snapshot['rank']],
            'category': snapshot['category'], 'categories': list(TOPIC_CATEGORIES),
            'items': snapshot['items'], 'fetched_at': snapshot['fetched_at'],
            'live_request': False, 'note': TOPIC_NOTE}
