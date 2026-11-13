"""Offline Playwright UI checks/screenshots. Every URL is fulfilled locally.

Run: .venv/bin/python check_hub_ui.py --screenshots /path/to/output
No gateway, account, browser profile or live platform endpoint is used.
"""
import argparse
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
START, END = '2026-10-25', '2026-10-31'
DEFAULT_START = '2026-10-02'
WORDS = ['杭州旅游', '西湖', '杭州美食', '灵隐寺', '杭州周末', '西溪湿地', '杭州住宿']
RELATED = [dict(keyword=WORDS[i] if i < len(WORDS) else f'杭州主题 {i+1}',
                relation_score=950-i*17, search_index=9000-i*101,
                composite_index=None if i == 49 else 13000-i*135) for i in range(50)]
DIMENSIONS = [('gender', ['女性', '男性']), ('age', ['24—30 岁', '31—40 岁', '18—23 岁', '41—50 岁', '50 岁以上']),
              ('province', ['浙江', '江苏', '上海', '广东', '北京']), ('city', ['杭州', '上海', '宁波', '南京']),
              ('yuntu_city_level', ['新一线', '一线', '二线']), ('interest', ['旅行', '美食', '摄影', '生活']),
              ('interest_category', ['城市生活', '本地游']), ('interest_category_v2', ['探店', '攻略'])]
AUDIENCE = [dict(dimension=key, name=key, labels=[dict(name=name, value=620-i*67, tgi=None if i==0 else 110+i)
           for i, name in enumerate(names + ([f'其他分组 {j}' for j in range(20)] if key in ('province','city','interest') else []))]) for key,names in DIMENSIONS]
META = dict(start_date=START, end_date=END, source='离线合成样例', fetched_at='2026-11-01T10:00:00+08:00',
            keywords=['杭州'], complete=True, verification='pending')
PLATFORMS = [dict(id=key, name=name, available=key!='google_trends', metric_name='搜索指数', description='离线合成数据',
                    modules=(['related','audience'] if key=='douyin' else []))
             for key,name in [('douyin','抖音'),('baidu','百度指数'),('google_trends','Google Trends')]]


def run(output=None):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for width,height in [(430,932),(1280,900)]:
                context = browser.new_context(viewport=dict(width=width,height=height), has_touch=width==430, is_mobile=width==430, locale='zh-CN', timezone_id='Asia/Shanghai', service_workers='block')
                page = context.new_page()
                page.clock.install(time=datetime(2026, 11, 2, tzinfo=timezone.utc))
                errors, unexpected, writes, status_reads, held = [], [], [], [], []
                page.on('pageerror', lambda error: (errors.append(str(error)), print('UI error:', error, flush=True)))
                fixture = {'history': True, 'related': 'available', 'audience': 'available', 'missing_latest': False, 'all_missing': False, 'login': 'cookie_present', 'paused': None, 'topic_login': 'logged_in', 'topic_categories': True, 'comments_probe': 'not_logged_in',
                           'groups': [dict(id='a'*32, name='合成组', keywords=['杭州', '西湖'])]}

                def route(request_route):
                    request = request_route.request
                    url = urlparse(request.url)
                    if url.netloc != 'hub-ui.invalid':
                        unexpected.append(request.url)
                        request_route.abort()
                        return
                    if url.path == '/':
                        request_route.fulfill(content_type='text/html', body=(ROOT/'hub.html').read_text())
                        return
                    path, query = url.path.removeprefix('/api/v1/'), parse_qs(url.query)
                    data = None
                    if request.method != 'GET':
                        writes.append((path, request.post_data_json))
                        if path in ('queries','insights/queries'):
                            data = dict(job=dict(id=f'mock-{len(writes)}',status='queued',cache_hit=False))
                        elif path in ('comments/start', 'comments/pause', 'comments/resume'):
                            fixture['comments_running'] = path == 'comments/start'
                            fixture['comments_paused'] = path == 'comments/pause'
                            data = dict(ok=True)
                        elif path == 'comments/probe':
                            data = dict(ok=True, probe=fixture['comments_probe'])
                        elif path == 'desktop/ticket':
                            data = dict(ticket=secrets.token_urlsafe(24))
                        elif path == 'topics/login/refresh':
                            data = dict(state='need_scan', qr_available=True)
                        elif path == 'topics/categories/refresh':
                            fixture['topic_login'], fixture['topic_categories'] = 'logged_in', True
                            data = dict(status='ok', item_count=1, live_request=True)
                        elif request.method == 'POST' and path == 'topics/collect-once':
                            body = request.post_data_json or {}
                            if fixture.get('topic_collect_error'):
                                code = fixture['topic_collect_error']
                                if code in ('daily_limit', 'paused', 'interval', 'invalid_parameters'):
                                    request_route.fulfill(status=429 if code in ('daily_limit', 'interval') else 409, content_type='application/json', body=json.dumps({'error': {'code': code, 'message': 'fixture'}}))
                                    return
                                data = dict(status=code)
                            else:
                                data = dict(status='ok', item_count=0, fields=[], live_request=True,
                                            rank=body.get('rank'), category=body.get('category'))
                        elif request.method == 'PUT' and path.startswith('groups/') and path.count('/') == 1:
                            body = request.post_data_json or {}
                            gid = path.split('/', 1)[1]
                            updated = dict(id=gid, name=body.get('name'), keywords=list(body.get('keywords') or []))
                            fixture['groups'] = [updated if item['id'] == gid else item for item in fixture['groups']]
                            if not any(item['id'] == gid for item in fixture['groups']):
                                fixture['groups'].insert(0, updated)
                            data = dict(group=updated, live_request=False, note='已更新关键词组，未提交搜索指数任务。')
                    elif path == 'platforms': data = dict(platforms=PLATFORMS)
                    elif path == 'status':
                        status_reads.append(request.url)
                        data = dict(browser='browser_ready' if fixture['login'] in ('cookie_present', 'qr_session') else 'login_required',login_state=fixture['login'],login_mode=fixture.get('login_mode', 'desktop'),counts=dict(keywords=1,points=7),
                                    daily_online_attempts=0,limits=dict(online_attempts_per_day=50),cooldown_remaining_seconds=0,paused=fixture['paused'],last_result=None, hotlist=dict(enabled=fixture.get('hot_enabled', True), paused=fixture.get('hot_paused'), interval_seconds=3600, daily_success_limit=48, daily_successes=0))
                    elif path == 'login/qr':
                        data = dict(state=fixture['qr_state'],
                                    browser='browser_ready' if fixture['login'] in ('cookie_present', 'qr_session') else 'login_required',
                                    session=dict(has_session=fixture['qr_state']=='succeeded'))
                    elif path == 'login/sms': data = dict(state='idle', browser='browser_ready')
                    elif path == 'history': data = dict(datasets=[dict(metadata=META,fetched=1793498400)] if fixture['history'] else [], latest_available_date=END if fixture['history'] else None)
                    elif path == 'jobs': data = dict(jobs=fixture.get('jobs', []))
                    elif path == 'jobs/synthetic-douyin-login-failure': data = dict(job=fixture['jobs'][0])
                    elif path == 'keys': data = dict(keys=[])
                    elif path == 'comments/status': data = dict(probe=fixture['comments_probe'], running=fixture.get('comments_running', False), paused=fixture.get('comments_paused', False), pause_reason='user' if fixture.get('comments_paused') else None, output=fixture.get('comments_output'))
                    elif path == 'comments/runs': data = dict(runs=fixture.get('comments_runs', []), usage=dict(today_used=fixture.get('comments_used'), daily_limit=3000, batch_limit=1500))
                    elif path.startswith('comments/runs/'):
                        run_id = path.split('/')[2]
                        comments = fixture.get('comments_rows', []) if run_id == '20261113T101112' else []
                        data = dict(run_id=run_id, total=len(comments), truncated=False, comments=comments)
                    elif path == 'insights/ranges': data = dict(ranges=[], attempts=[], live_request=False)
                    elif path == 'series':
                        data = dict(keyword='杭州',points=[dict(date=f'2026-10-{25+i}', value=None if fixture['all_missing'] or fixture['missing_latest'] and i==6 else 12000+i*900,
                                    composite_index=None if fixture['all_missing'] else 30000+i*1200) for i in range(7)])
                    elif path == 'insights':
                        kind = query['kind'][0]
                        data = dict(status=fixture[kind],result=dict(metadata=META,data=dict(search_related=RELATED,content_related=RELATED[10:]) if kind=='related' else dict(dimensions=AUDIENCE)))
                    elif path == 'hot/latest':
                        data = dict(board='word_list', fetched_at='2026-11-04T12:00:00+08:00', live_request=False,
                                    note='热榜热度不是搜索指数，也不是创作者中心综合指数。',
                                    items=[dict(position=1, word='合成话题甲', hot_value=12083012, fetched_at='2026-11-04T12:00:00+08:00'),
                                           dict(position=2, word='合成话题乙', hot_value=None, fetched_at='2026-11-04T12:00:00+08:00')])
                        if fixture.get('hot_empty') and 'board' not in query:
                            data.update(items=[], fetched_at=None)
                        if 'board' in query:
                            data.update(board=query['board'][0], items=[dict(position=1, word='合成百度话题甲', hot_value=654321, fetched_at=data['fetched_at'])])
                    elif path == 'hot/word':
                        data = dict(points=[dict(position=1, word='合成话题甲', hot_value=12083012, fetched_at='2026-11-04T12:00:00+08:00')], live_request=False)
                    elif path == 'groups' and request.method == 'GET':
                        data = dict(groups=fixture['groups'],
                                    note='工作台热度分，不是搜索次数，也不是抖音综合指数。')
                    elif path.startswith('groups/') and path.endswith('/score'):
                        words = fixture['groups'][0]['keywords']
                        data = dict(note='工作台热度分，不是搜索次数，也不是抖音综合指数。',
                                    weights=dict(search=0.7, hot=0.3),
                                    search=dict(score=None, missing=[dict(day='2026-11-03', keyword=words[-1])]),
                                    hot=dict(score=None, snapshot_status='available',
                                             members=[dict(keyword=words[0], status='exact', ratio=1),
                                                      dict(keyword=words[-1], status='未上榜', ratio=None)],
                                             substring_hits=[dict(keyword=words[-1], word=words[-1] + '醋鱼')]),
                                    fusion=dict(score=None))
                    elif path == 'topics/login':
                        data = dict(state=fixture['topic_login'], qr_available=fixture['topic_login']=='need_scan')
                    elif path == 'topics/login/qr':
                        if fixture.get('topic_qr_error'):
                            request_route.fulfill(status=503, content_type='application/json', body='{}')
                            return
                        request_route.fulfill(content_type='image/png', body=(ROOT/'tests/fixtures/topic-qr-synthetic.png').read_bytes())
                        return
                    elif path == 'topics':
                        if fixture.get('topic_error'):
                            request_route.fulfill(status=503, content_type='application/json', body='{}')
                            return
                        data = dict(status='available', status_label='尚未采集', window='1d', window_label='近1天',
                                    rank=query.get('rank', ['total'])[0],
                                    rank_label='飙升' if query.get('rank', ['total'])[0] == 'rising' else '总榜',
                                    category=query.get('category', ['全部'])[0],
                                    categories=(['全部', '美食', '旅行', '科技', '科技科普', '科技产品'] if fixture['topic_categories'] else ['全部']), items=[], fetched_at=None,
                                    live_request=False, note='热点宝话题不是搜索指数，待联调，不伪造话题。')
                    elif path == 'hot/rising':
                        data = dict(live_request=False, note='这是本库热榜快照的计算结果，不是搜索指数，也不是官方飙升榜。',
                                    current_fetched_at='2026-11-04T12:00:00+08:00', baseline_fetched_at=None,
                                    thresholds=dict(min_age_seconds=1800, max_age_seconds=21600, lookback_seconds=86400,
                                                    rank_rise=10, rank_max=15, heat_pct=0.5, heat_delta=500000,
                                                    z_min_samples=8, z_min=2),
                                    items=[dict(word='合成话题甲', position=1, new_entry=True, rank_rise=None,
                                                rank_rise_hit=None, heat_pct=None, heat_hit=None, z_score=None,
                                                z_hit=None, search_index_ratio=None)])
                        if 'hot_rising_items' in fixture:
                            data['items'] = fixture['hot_rising_items']
                        if fixture.get('hot_empty') and 'board' not in query:
                            data.update(items=[], current_fetched_at=None)
                    elif path.startswith('jobs/mock-'): data = dict(job=dict(id=path[5:], status='queued',cache_hit=False))
                    if data is None:
                        unexpected.append(request.method+' '+request.url)
                        request_route.abort()
                    elif path == fixture.get('hold'): held.append((request_route, data))
                    else: request_route.fulfill(content_type='application/json',body=json.dumps(data))

                context.route('**/*', route)
                page.goto('http://hub-ui.invalid/#api')
                page.wait_for_function("document.querySelector('#audienceSummary').textContent.includes('浙江')", timeout=10000)
                # The removed API module cannot be reached by navigation, hash or tab().
                assert page.locator('.nav [data-tab]').evaluate_all('(nodes)=>nodes.map(n=>n.dataset.tab)') == ['overview', 'comments', 'tasks', 'connections']
                assert page.locator('[data-tab=api], a[href="#api"]').count() == 0
                assert 'API 与密钥' not in page.locator('.nav').inner_text()
                assert page.locator('[data-section=api]').is_hidden()
                assert page.locator('[data-section=overview]').is_visible()
                page.evaluate("tab('connections');tab('api')")
                assert page.locator('[data-section=connections]').is_visible()
                assert page.locator('[data-section=api]').is_hidden()
                assert page.locator('#pageTitle').inner_text() == '平台连接'
                assert page.locator('.nav .active').get_attribute('data-tab') == 'connections'
                page.evaluate("location.hash='';location.hash='api'")
                assert page.locator('[data-section=api]').is_hidden()
                assert page.locator('[data-section=connections]').is_visible()
                page.evaluate("location.hash='';tab('overview')")
                if output and width == 1280:
                    page.screenshot(path=str(output/'hub-1113-nav-no-api-1280.png'), full_page=True)
                assert page.locator('#platformChips [data-platform]').evaluate_all('(nodes)=>nodes.map(n=>n.dataset.platform)') == ['douyin', 'baidu']
                assert page.locator('#queryPlatform option').evaluate_all('(nodes)=>nodes.map(n=>n.value)') == ['douyin', 'baidu']
                page.wait_for_function("document.querySelector('#statPlatforms').textContent==='2'")
                assert page.locator('#statPlatforms').text_content() == '2'
                for selector in ('body', '#platformList', '#connectionCards', '#queryPlatform'):
                    text = page.locator(selector).inner_text() if selector == 'body' else page.locator(selector).text_content()
                    assert all(name not in text for name in ('Google Trends', '谷歌趋势', '待接入'))
                assert 'google_trends' not in (ROOT/'hub.html').read_text()
                assert page.locator('#queryStart').input_value() == DEFAULT_START
                assert page.locator('#queryEnd').input_value() == END
                assert page.locator('#insightRangeLabel').inner_text().endswith('30 天')
                assert page.locator('#resultPanel-summary').is_visible()
                assert page.locator('#relatedSummary .rank-row').count() == 3
                assert page.locator('#audienceSummary .audience-leader').count() == 3
                assert page.locator('#audienceSummary').bounding_box()['y'] + page.locator('#audienceSummary').bounding_box()['height'] < height
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                if output:
                    page.evaluate("""() => {const n=document.createElement('div');n.textContent='离线合成样例 · 未调用真实查询';n.style='float:right;font-size:9px;font-weight:400;letter-spacing:0;color:#b2c8bc;padding-top:5px';document.querySelector('.brand').append(n)}""")
                    page.screenshot(path=str(output/f'hub-tips-{"mobile" if width==430 else "desktop"}.png'))
                # Native popovers: touch, mouse hover, keyboard, dismissal and viewport bounds.
                tip = page.locator('#relatedSummary .info-button')
                popup = page.locator('#' + tip.get_attribute('aria-describedby'))
                assert tip.get_attribute('aria-controls') == popup.get_attribute('id')
                assert tip.get_attribute('aria-expanded') == 'false'
                assert not popup.is_visible()
                assert '关联分不是概率' not in page.locator('#relatedSummary').inner_text()
                assert '关联词数据到 10-31' in page.locator('#relatedSummary').inner_text()
                if width == 430:
                    tip.tap()
                else:
                    tip.hover()
                    assert popup.is_visible()
                    popup.hover()
                    assert popup.is_visible()  # Hovered explanation remains readable.
                    page.locator('h2').filter(has_text='关键词洞察').hover()
                    assert not popup.is_visible()
                    tip.click()
                assert popup.is_visible()
                if width == 430:
                    tip.tap()
                    assert not popup.is_visible()
                    assert tip.get_attribute('aria-expanded') == 'false'
                    tip.tap()
                assert tip.get_attribute('aria-expanded') == 'true'
                assert '关联分不是概率' in popup.inner_text()
                bounds = popup.bounding_box()
                assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width
                assert bounds['y'] >= 0 and bounds['y'] + bounds['height'] <= height
                if output and width == 430:
                    page.screenshot(path=str(output/'hub-tips-popover-mobile.png'))
                before_reads = len(status_reads)
                page.clock.fast_forward(15000)
                assert len(status_reads) == before_reads
                assert popup.is_visible()
                page.keyboard.press('Escape')
                assert not popup.is_visible()
                assert tip.get_attribute('aria-expanded') == 'false'
                tip.focus()
                page.keyboard.press('Enter')
                assert popup.is_visible()
                page.keyboard.press('Escape')
                assert tip.evaluate('(n) => n === document.activeElement')
                page.keyboard.press('Space')
                assert popup.is_visible()
                heading = page.get_by_role('heading', name='关键词洞察', exact=True)
                heading.tap() if width == 430 else heading.click()
                assert not popup.is_visible()
                assert tip.get_attribute('aria-expanded') == 'false'
                assert not writes
                page.clock.fast_forward(15000)
                page.wait_for_function('!refreshing')
                assert len(status_reads) > before_reads
                # A response already in flight must not remove the open/focused explanation.
                for endpoint, target in [('status', '#browserBadge'), ('series', '#trendSummary'), ('insights', '#relatedSummary')]:
                    fixture['hold'] = endpoint
                    page.evaluate('void refresh()')
                    page.wait_for_timeout(80)
                    assert held, endpoint
                    active_tip = page.locator(target + ' .info-button')
                    active_tip.focus()
                    page.keyboard.press('Enter')
                    active_popup = page.locator('#' + active_tip.get_attribute('aria-controls'))
                    for pending, data in held:
                        pending.fulfill(content_type='application/json', body=json.dumps(data))
                    held.clear()
                    fixture['hold'] = None
                    page.wait_for_function('!refreshing')
                    assert active_popup.is_visible(), endpoint
                    assert active_tip.evaluate('(n) => n === document.activeElement'), endpoint
                    page.keyboard.press('Escape')
                    heading.click()
                page.get_by_role('tab', name='关联需求', exact=True).click()
                assert page.locator('#relatedData .rank-row').count() == 10
                assert page.locator('#relatedData tbody tr:visible').count() == 0
                page.locator('#relatedData summary').first.click()
                assert page.locator('#relatedData tbody tr:visible').count() == 50
                assert '—' in page.locator('#relatedData tbody tr').nth(49).inner_text()
                page.evaluate('refresh()')
                assert page.locator('#relatedData details').first.get_attribute('open') is not None
                page.locator('#relatedSort').select_option('search_index')
                page.wait_for_function("document.querySelector('#relatedData p.tiny').textContent.includes('搜索指数')")
                assert page.locator('#relatedData details').first.get_attribute('open') is not None
                page.get_by_role('tab', name='人群特征', exact=True).click()
                assert page.locator('#audienceData .insight-block').count() == 8
                assert page.locator('#audienceData details').count() == 8
                assert page.locator('#audienceData tbody tr:visible').count() == 0
                page.locator('#audienceData summary').nth(2).click()
                assert page.locator('#audienceData tbody tr:visible').count() == 25
                page.get_by_role('tab', name='趋势', exact=True).click()
                page.locator('#chart svg').focus()
                page.keyboard.press('ArrowRight')
                assert page.locator('#chartTooltip').is_visible()
                page.get_by_role('tab', name='趋势', exact=True).focus()
                page.keyboard.press('End')
                assert page.get_by_role('tab', name='历史', exact=True).get_attribute('aria-selected')=='true'
                page.keyboard.press('Home')
                assert page.get_by_role('tab', name='概要', exact=True).get_attribute('aria-selected')=='true'
                page.locator('#openQuery').click()
                query_tip = page.locator('#queryForm .info-button')
                query_tip.focus()
                page.keyboard.press('Enter')
                assert query_tip.get_attribute('aria-expanded') == 'true'
                page.keyboard.press('Escape')
                assert page.locator('#queryDialog').is_visible()
                assert query_tip.get_attribute('aria-expanded') == 'false'
                assert not writes
                page.locator('#queryWords').fill('杭州')
                page.locator('#queryRange>summary').click()
                page.locator('[data-range=query][data-period="30d"]').click()
                assert page.locator('#queryStart').input_value() == '2026-10-02'
                if output and width==430:
                    page.screenshot(path=str(output/'hub-tips-range-mobile.png'))
                expected = {'7d':'2026-10-25','3m':'2026-08-01','6m':'2026-05-01','12m':'2025-11-01'}
                for period,start in expected.items():
                    page.locator(f'[data-range=query][data-period="{period}"]').click()
                    assert page.locator('#queryStart').input_value()==start
                assert page.evaluate("presetStart('2024-02-29','12m')") == '2023-03-01'
                assert page.evaluate("presetStart('2025-05-31','3m')") == '2025-03-01'
                assert page.evaluate("rangeDays('2024-01-01','2025-12-31')") == 731
                # Test explicit 731 cap independently from the two-calendar-year bounds.
                page.evaluate("$('#queryStart').min='2024-01-01';$('#queryEnd').max='2026-12-31';$('#queryStart').value='2024-01-01';$('#queryEnd').value='2025-12-31'")
                assert page.evaluate("rangeError('query')") == ''
                page.locator('#queryEnd').fill('2026-01-01')
                assert '731' in page.evaluate("rangeError('query')")
                # Direct submit-event dispatch also exercises the JS guard, even bypassing native validity.
                page.locator('#queryForm').dispatch_event('submit')
                assert '731' in page.locator('#queryRangeError').inner_text()
                assert len(writes)==0
                page.locator('#queryStart').fill('2026-10-31')
                page.locator('#queryEnd').fill('2026-10-25')
                page.locator('#queryForm').dispatch_event('submit')
                assert '晚于' in page.locator('#queryRangeError').inner_text()
                assert len(writes)==0
                page.locator('#queryStart').fill('2026-10-25')
                page.locator('#queryEnd').fill(END)
                page.locator('#queryEnd').dispatch_event('change')
                assert page.locator('#queryRangeError').inner_text()==''
                page.locator('#queryEnd').fill('')
                page.locator('[data-range=query][data-period="7d"]').click()
                assert '结束日期' in page.locator('#queryRangeError').inner_text()
                assert page.locator('#queryEnd').input_value()==''
                page.locator('#queryEnd').fill(END)
                page.locator('#queryEnd').dispatch_event('change')
                # Modal errors must be visible in the top layer even on a short phone viewport.
                page.set_viewport_size(dict(width=width,height=700))
                for words in ['杭州\n杭州','一\n二\n三\n四\n五\n六']:
                    page.locator('#queryWords').fill(words)
                    page.locator('#submitButton').click()
                    assert page.locator('#queryError').is_visible()
                    error_box=page.locator('#queryError').bounding_box()
                    assert 0 <= error_box['y'] and error_box['y']+error_box['height'] <= 700
                    assert '不同的关键词' in page.locator('#queryError').inner_text()
                    assert len(writes)==0
                page.locator('#queryWords').fill('杭州')
                page.evaluate("reportBatch=makeReportBatch({platform:'douyin',keywords:['其他词'],start_date:'2026-10-25',end_date:'2026-10-31'},'unconfirmed');reportBatch.entries[0].error='合成网络错误'")
                page.locator('#submitButton').click()
                assert '上一批' in page.locator('#toast').inner_text()
                assert '「本次完整查询」' in page.locator('#toast').inner_text()
                assert not page.locator('#queryDialog').is_visible()
                assert page.locator('#reportProgress').evaluate('(node)=>node.open')
                assert page.locator('#retryBundle').is_visible()
                assert page.locator('#retryBundle').evaluate('(node)=>node===document.activeElement')
                retry_box = page.locator('#retryBundle').bounding_box()
                assert 0 <= retry_box['y'] and retry_box['y']+retry_box['height'] <= 700
                assert len(writes)==0
                page.evaluate('reportBatch=null;saveBatch()')
                page.locator('#openQuery').click()
                page.set_viewport_size(dict(width=width,height=height))
                page.keyboard.press('Escape')
                assert not page.locator('#queryDialog').is_visible()
                assert page.locator('#openQuery').evaluate('e=>e===document.activeElement')
                page.evaluate("$('#insightStart').value='2024-01-01';$('#insightEnd').value='2024-01-07'")
                assert page.evaluate("rangeError('insight')") == ''
                page.evaluate("$('#insightStart').value='2026-10-25';$('#insightEnd').value='2026-10-31'")
                # Missing and unavailable states never become zero or stale successful summaries.
                fixture['missing_latest']=True
                page.evaluate('loadSeries()')
                assert '—' in page.locator('#trendSummary').inner_text()
                fixture['all_missing']=True
                page.evaluate('loadSeries()')
                assert '无数据' in page.locator('#trendSummary').inner_text()
                assert '已存' not in page.locator('#trendSummary').inner_text()
                assert '—' in page.locator('#trendSummary').inner_text()
                fixture['all_missing']=False
                page.evaluate('loadSeries()')
                fixture['related']='no_data'
                page.evaluate('loadInsights()')
                assert ''.join(page.locator('#relatedSummary').inner_text().split()) == '无数据ⓘ'
                fixture['related']='available'
                fixture['audience']='missing'
                page.evaluate('loadInsights()')
                assert '人群状态待确认' in page.locator('#audienceSummary').inner_text()
                page.locator('#platformChips [data-platform=baidu]').click()
                page.wait_for_function("document.querySelector('#relatedSummary').textContent.includes('不支持此模块')")
                page.wait_for_function("document.querySelector('#trendSummary').textContent.includes('暂无趋势数据')")
                assert '尚未接入' not in page.locator('#trendSummary').inner_text()
                assert page.locator('#viewChips').is_visible()
                assert '搜索指数' in page.locator('#viewChips').inner_text()
                assert '热搜榜' in page.locator('#viewChips').inner_text()
                assert page.locator('#groupPanel').is_hidden()
                assert page.locator('#overviewHot').is_hidden()
                with page.expect_request(lambda req: 'hot/latest?board=baidu_realtime' in req.url):
                    page.locator('[data-source=hot_search]').click()
                page.wait_for_function("document.querySelector('#hotIntro').textContent.includes('百度热搜')")
                assert '不是百度搜索指数' in page.locator('#hotIntro').text_content()
                assert page.locator('#groupPanel').is_hidden()
                page.locator('#platformChips [data-platform=douyin]').click()
                assert page.locator('#viewChips').is_visible()
                assert '搜索指数' in page.locator('#viewChips').inner_text()
                assert '热搜榜' not in page.locator('#platformChips').inner_text()
                assert page.locator('[data-tab=hot]').count() == 0
                assert '热榜' not in page.locator('.nav').inner_text()
                page.locator('[data-source=hot_search]').click()
                page.wait_for_function("document.querySelector('#hotRows').textContent.includes('合成话题甲')")
                assert '搜索页热榜' in page.locator('#hotIntro').text_content()
                assert '不是搜索指数' in page.locator('#hotIntro').text_content()
                assert '热点宝' in page.locator('#hotIntro').text_content()
                assert page.locator('#hotResume').is_hidden()
                assert page.locator('#hotRows tr').nth(0).locator('td').nth(0).inner_text() == '1'
                assert page.locator('#hotRows tr').nth(0).locator('td').nth(2).inner_text() == '12083012'
                assert page.locator('#hotRows tr').nth(1).locator('td').nth(0).inner_text() == '2'
                assert page.locator('#hotRows tr').nth(1).locator('td').nth(2).inner_text() == '—'
                fixture['hot_empty'] = True
                for enabled, paused, expected in (
                    (False, None, '抖音热榜自动抓取未开启（服务端配置），暂无快照。开启后每小时抓取一次，无需登录。'),
                    (True, None, '尚未抓到热榜快照，首次抓取完成后显示。'),
                    (True, dict(code='rate_limited', message='合成限频 <img src=x>'), '热榜抓取已暂停：合成限频 <img src=x>'),
                    (False, dict(code='access_denied'), '热榜抓取已暂停：access_denied'),
                ):
                    fixture.update(hot_enabled=enabled, hot_paused=paused)
                    page.evaluate('loadHot()')
                    assert expected in page.locator('#hotRows').inner_text()
                    assert expected in page.locator('#hotRisingRows').inner_text()
                    assert page.locator('#hotResume').is_visible() == bool(paused)
                    page.evaluate("$('#hotRisingButton').onclick()")
                    page.wait_for_function("document.querySelector('#hotRisingBaseline').textContent.includes('当前快照 —')")
                    assert expected in page.locator('#hotRisingRows').inner_text()
                    assert page.locator('#overviewHot [data-login-platform]').count() == 0
                    assert page.locator('#hotRows img, #hotRisingRows img').count() == 0
                    assert page.locator('table:has(#hotRows) thead').is_hidden()
                    assert page.locator('table:has(#hotRisingRows) thead').is_hidden()
                    empty_cell = page.locator('#hotRisingRows td')
                    assert empty_cell.is_visible(), empty_cell.evaluate('(n)=>n.parentElement.parentElement.outerHTML')
                    assert empty_cell.bounding_box()['width'] <= width
                fixture.update(hot_empty=False, hot_enabled=True, hot_paused=None)
                page.evaluate('loadHot()')
                page.wait_for_function("document.querySelector('#hotRows').textContent.includes('合成话题甲')")
                assert page.locator('#hotSentence').is_hidden()
                page.locator('#overviewHot > details.card > summary').click()
                assert page.locator('#hotSentence').is_visible()
                page.locator('#hotSentence').fill('s1')
                page.locator('#hotWordButton').click()
                page.wait_for_function("document.querySelector('#hotHistory').textContent.includes('合成话题甲')")
                page.locator('#hotRisingButton').click()
                page.wait_for_function("document.querySelector('#hotRisingRows').textContent.includes('合成话题甲')")
                assert '本库热榜快照' in page.locator('#hotRisingNote').text_content()
                assert '不是搜索指数' in page.locator('#hotRisingNote').text_content()
                assert '名次上升 ≥ 10' in page.locator('#hotRisingThresholds').text_content()
                assert page.locator('#hotRisingRows tr').nth(0).locator('td').nth(7).inner_text() == '—'
                assert page.locator('#hotRisingRows tr').nth(0).locator('td').nth(9).inner_text() == '—'
                # Sorting changes only the Douyin rising view; all values below are fictional.
                fixture['hot_rising_items'] = [
                    dict(word='示例词甲', position=10, new_entry=False, rank_rise=2, rank_rise_hit=False,
                         heat_pct=0.2, heat_hit=True, z_score=0, z_hit=False, search_index_ratio=0),
                    dict(word='示例词丙', position=None, new_entry=None, rank_rise=None, rank_rise_hit=None,
                         heat_pct=None, heat_hit=None, z_score=None, z_hit=None, search_index_ratio=None),
                    dict(word='示例词乙', position=2, new_entry=True, rank_rise=10, rank_rise_hit=True,
                         heat_pct=0.05, heat_hit=False, z_score=2, z_hit=True, search_index_ratio=1.2),
                    dict(word='示例词丁', position=5, new_entry=False,
                         heat_pct=-0.1, heat_hit=False, z_score=-1, z_hit=False, search_index_ratio=0.8),
                ]
                default_order = ['示例词甲', '示例词丙', '示例词乙', '示例词丁']
                def rising_order():
                    return page.locator('#hotRisingRows tr td:first-child').all_text_contents()
                page.evaluate('loadHotRising()')
                assert rising_order() == default_order
                assert page.locator('[data-rising-key] button').count() == 10
                assert page.locator('[data-rising-key][aria-sort=none]').count() == 10
                page.evaluate("document.querySelector('#overviewHot > details.card').open=false;scrollTo(0,0)")
                if output and width == 1280:
                    page.screenshot(path=str(output/'hub-1113-rising-sort-before-1280.png'), full_page=True)
                rank_header = page.locator('[data-rising-key=rank_rise]')
                rank_button = rank_header.locator('button')
                rank_button.click()
                assert rising_order() == ['示例词乙', '示例词甲', '示例词丙', '示例词丁']
                assert rank_header.get_attribute('aria-sort') == 'descending'
                assert '▼' in rank_button.inner_text()
                if output and width == 1280:
                    page.evaluate('scrollTo(0,0)')
                    page.screenshot(path=str(output/'hub-1113-rising-sort-after-1280.png'), full_page=True)
                # Native buttons keep focus and support keyboard sorting, including default reset.
                assert rank_button.evaluate('(n)=>n===document.activeElement')
                rank_button.press('Enter')
                assert rising_order() == ['示例词甲', '示例词乙', '示例词丙', '示例词丁']
                assert rank_header.get_attribute('aria-sort') == 'ascending'
                assert '▲' in rank_button.inner_text()
                rank_button.press('Space')
                assert rising_order() == default_order
                assert rank_header.get_attribute('aria-sort') == 'none'
                # Other numeric columns, boolean hits and Chinese collation use their raw values.
                expected_orders = {
                    'word': (['示例词乙', '示例词甲', '示例词丁', '示例词丙'], ['示例词丙', '示例词丁', '示例词甲', '示例词乙']),
                    'position': (['示例词甲', '示例词丁', '示例词乙', '示例词丙'], ['示例词乙', '示例词丁', '示例词甲', '示例词丙']),
                    'new_entry': (['示例词乙', '示例词甲', '示例词丁', '示例词丙'], ['示例词甲', '示例词丁', '示例词乙', '示例词丙']),
                    'rank_rise_hit': (['示例词乙', '示例词甲', '示例词丙', '示例词丁'], ['示例词甲', '示例词乙', '示例词丙', '示例词丁']),
                    'heat_pct': (['示例词甲', '示例词乙', '示例词丁', '示例词丙'], ['示例词丁', '示例词乙', '示例词甲', '示例词丙']),
                    'heat_hit': (['示例词甲', '示例词乙', '示例词丁', '示例词丙'], ['示例词乙', '示例词丁', '示例词甲', '示例词丙']),
                    'z_score': (['示例词乙', '示例词甲', '示例词丁', '示例词丙'], ['示例词丁', '示例词甲', '示例词乙', '示例词丙']),
                    'z_hit': (['示例词乙', '示例词甲', '示例词丁', '示例词丙'], ['示例词甲', '示例词丁', '示例词乙', '示例词丙']),
                    'search_index_ratio': (['示例词乙', '示例词丁', '示例词甲', '示例词丙'], ['示例词甲', '示例词丁', '示例词乙', '示例词丙']),
                }
                for key, (descending, ascending) in expected_orders.items():
                    button = page.locator(f'[data-rising-key={key}] button')
                    button.click()
                    assert rising_order() == descending, key
                    button.click()
                    assert rising_order() == ascending, key
                    button.click()
                    assert rising_order() == default_order, key
                rank_button.click()
                # Reload with new rows, then change views/platforms while preserving Douyin's choice.
                fixture['hot_rising_items'][0]['rank_rise'] = 20
                page.evaluate('loadHotRising()')
                assert rising_order() == ['示例词甲', '示例词乙', '示例词丙', '示例词丁']
                assert rank_header.get_attribute('aria-sort') == 'descending'
                page.locator('[data-source=index]').click()
                page.locator('[data-source=hot_search]').click()
                page.evaluate('loadHotRising()')
                assert rank_header.get_attribute('aria-sort') == 'descending'
                assert rising_order() == ['示例词甲', '示例词乙', '示例词丙', '示例词丁']
                page.locator('[data-platform=baidu]').click()
                assert page.locator('[data-rising-key] button').count() == 0
                page.locator('[data-source=hot_search]').click()
                page.evaluate('loadHotRising()')
                assert rising_order() == default_order
                assert page.locator('[data-rising-key][aria-sort]').count() == 0
                assert rank_header.inner_text() == '名次上升'
                rank_header.click()
                assert rising_order() == default_order
                assert page.locator('table:has(#hotRows) th button').count() == 0
                page.locator('[data-platform=douyin]').click()
                page.locator('[data-source=hot_search]').click()
                page.evaluate('loadHotRising()')
                assert rank_header.get_attribute('aria-sort') == 'descending'
                assert rising_order() == ['示例词甲', '示例词乙', '示例词丙', '示例词丁']
                assert page.locator('#hotRows tr td:nth-child(2)').all_text_contents() == ['合成话题甲', '合成话题乙']
                rank_button.click()
                rank_button.click()
                assert rising_order() == default_order
                del fixture['hot_rising_items']
                assert page.locator('#topicCategory').is_hidden()
                assert page.locator('#groupPanel').is_hidden()
                page.locator('[data-source=index]').click()
                assert page.locator('#groupPanel').is_visible()
                assert page.locator('[data-tab=groups]').count() == 0
                assert '关键词组' not in page.locator('.nav').inner_text()
                page.locator('[data-tab=overview]').click()
                page.wait_for_function("document.querySelector('#groupSelect').textContent.includes('合成组')")
                assert '工作台热度分' in page.locator('#groupIntro').text_content()
                assert '不是搜索次数' in page.locator('#groupIntro').text_content()
                assert '名称只用于显示' in page.locator('#groupIntro').text_content()
                assert '不会触发搜索查询' in page.locator('#groupIntro').text_content()
                assert not page.locator('#groupPanel').evaluate('(node)=>node.open')
                assert page.locator('#groupScoreButton').is_hidden()
                page.locator('#groupPanel > summary').click()
                assert page.locator('#groupScoreButton').is_visible()
                assert page.locator('#groupEdit').inner_text() == '编辑这组'
                page.locator('#groupScoreButton').click()
                page.wait_for_function("document.querySelector('#groupMembers').textContent.includes('未上榜')")
                assert '0.7' in page.locator('#groupWeights').inner_text()
                assert page.locator('#groupFusionScore').inner_text() == '—'
                assert '西湖醋鱼' in page.locator('#groupMembers').inner_text()
                page.locator('#groupEdit').click()
                assert page.locator('#groupName').input_value() == '合成组'
                assert page.locator('#groupWords').input_value() == '杭州\n西湖'
                assert page.locator('#groupCreate').inner_text() == '更新关键词组'
                page.locator('#groupWords').fill('杭州\n灵隐寺')
                group_writes = len(writes)
                page.locator('#groupCreate').click()
                page.wait_for_function("document.querySelector('#groupMembers').textContent.includes('灵隐寺')")
                assert '灵隐寺醋鱼' in page.locator('#groupMembers').inner_text()
                assert '未上榜' in page.locator('#groupMembers').inner_text()
                assert page.locator('#groupFusionScore').inner_text() == '—'
                assert page.locator('#groupSearchScore').inner_text() == '—'
                assert page.locator('#groupHotScore').inner_text() == '—'
                assert '合成组' in page.locator('#groupSelect').inner_text()
                assert page.locator('#groupCreate').inner_text() == '保存关键词组'
                assert page.locator('#groupWords').input_value() == ''
                assert writes[group_writes] == ('groups/' + 'a' * 32, dict(name='合成组', keywords=['杭州', '灵隐寺']))
                assert all(item[0] not in ('queries', 'insights/queries') for item in writes[group_writes:])
                page.locator('[data-source=hot_search]').click()
                page.wait_for_function("document.querySelector('#hotRows').textContent.includes('合成话题甲')")
                assert '—' in page.locator('#hotRows').inner_text()
                assert '搜索页热榜' in page.locator('#hotIntro').text_content()
                assert '不是搜索指数' in page.locator('#hotIntro').text_content()
                assert page.locator('#keywordSelect').is_hidden()
                assert page.locator('#resultTab-summary').is_hidden()
                assert page.locator('#groupPanel').is_hidden()
                assert page.locator('#topicCategory').is_hidden()
                page.locator('[data-source=topic]').click()
                page.locator('#topicCategory').click()
                page.wait_for_function("document.querySelector('#topicCategoryList').textContent.includes('美食')")
                assert page.locator('#topicWindow').inner_text() == '近1天'
                assert '尚未采集' in page.locator('#topicStatus').inner_text()
                assert page.locator('label[for="topicCategory"]').inner_text() == '垂类'
                assert page.locator('#topicCategoryQuery').count() == 0
                page.locator('#topicCategory').fill('科技')
                page.wait_for_function("()=>[...document.querySelectorAll('#topicCategoryList [role=option]')].map(o=>o.textContent).join(',')==='科技,科技科普,科技产品'")
                page.locator('#topicCategory').fill('   ')
                page.wait_for_function("()=>document.querySelector('#topicCategoryList').textContent.includes('全部')&&document.querySelector('#topicCategoryList').textContent.includes('旅行')")
                page.locator('#topicCategory').fill('科技')
                with page.expect_response(lambda r: 'topics/collect-once' in r.url and r.request.method == 'POST'):
                    page.locator('#topicCategory').press('Enter')
                page.wait_for_function("!document.querySelector('#topicStatus').textContent.includes('正在采集')")
                assert writes[-1] == ('topics/collect-once', dict(rank='total', category='科技'))
                page.locator('#topicCategory').fill('')
                page.wait_for_function("()=>[...document.querySelectorAll('#topicCategoryList [role=option]')].some(o=>o.textContent==='旅行')")
                assert '不是搜索指数' in page.locator('#topicIntro').inner_text()
                assert '待联调' in page.locator('#topicStatus').inner_text()
                before_topic = len(writes)
                with page.expect_response(lambda r: 'topics/collect-once' in r.url and r.request.method == 'POST'):
                    page.locator('#topicCategoryList [role=option]').filter(has_text='旅行').click()
                page.wait_for_function("!document.querySelector('#topicStatus').textContent.includes('正在采集')")
                assert writes[before_topic][0] == 'topics/collect-once'
                assert writes[before_topic][1] == dict(rank='total', category='旅行')
                with page.expect_response(lambda r: 'topics/collect-once' in r.url and r.request.method == 'POST'):
                    page.locator('#topicRankRising').click()
                page.wait_for_function("document.querySelector('#topicRankRising').getAttribute('aria-pressed')==='true'")
                assert writes[before_topic + 1][1] == dict(rank='rising', category='旅行')
                assert '合成话题' not in page.locator('#topicRows').inner_text()
                assert '0' not in page.locator('#topicRows').inner_text()
                page.locator('#platformChips [data-platform=douyin]').click()
                assert page.locator('#openQuery').is_visible()
                assert page.locator('#groupPanel').is_visible()
                assert page.locator('#overviewTopic').is_hidden()
                page.locator('[data-tab=overview]').click()
                # A real click submits only the mocked routes and preserves existing request shape.
                page.locator('#openQuery').click()
                assert page.locator('#douyinQueryNote').is_hidden()
                page.locator('#submitButton').click()
                page.wait_for_function("reportBatch?.entries.every(e=>e.jobId)")
                assert len(writes)==7, writes
                assert [path for path,_ in writes]==['groups/'+'a'*32,'topics/collect-once','topics/collect-once','topics/collect-once','queries','insights/queries','insights/queries']
                assert writes[4][1]==dict(platform='douyin',keywords=['杭州'],start_date=START,end_date=END)
                assert not page.locator('#queryDialog').is_visible()
                page.wait_for_function('!refreshing')
                fixture.update(login='expired', paused=dict(code='authentication_required'))
                page.evaluate('refresh()')
                assert '需要重新登录' in page.locator('#browserBadge').inner_text()
                assert page.locator('#notice').is_visible()
                assert '恢复采集' in page.locator('#notice').inner_text()
                assert not page.locator('#browserBadge .info-button').count()
                assert '抖音未登录' in page.locator('#indexLoginStatus').inner_text()
                page.locator('#viewChips [data-source=hot_search]').click()
                assert page.locator('#indexLoginStatus').is_hidden()
                assert page.locator('[data-login-platform]:visible').count() == 0
                page.locator('#viewChips [data-source=index]').click()
                page.locator('#openQuery').click()
                assert page.locator('#douyinQueryNote').is_visible()
                assert page.locator('#douyinQueryNote span').inner_text() == '抖音未登录：只能返回已缓存的完整结果，新的在线查询需先登录。'
                assert page.locator('#submitButton').is_enabled()
                page.locator('#douyinQueryNote [data-login-platform=douyin]').click()
                assert page.locator('#queryDialog').is_hidden()
                assert page.locator('[data-section=connections]').is_visible()
                assert page.evaluate("document.activeElement === document.querySelector('#douyinLoginCard')")
                failed_job = dict(id='synthetic-douyin-login-failure',
                                  query=dict(platform='douyin', keywords=['杭州'], start_date=START, end_date=END),
                                  status='failed', created_at=0,
                                  error=dict(code='authentication_required', message='抖音未登录'))
                fixture['jobs'] = [failed_job]
                page.evaluate('''job => {
                    jobs=[job];renderJobs();
                    reportBatch={query:job.query,entries:[{body:job.query,label:'指数趋势',
                        jobId:job.id,status:job.status,error:job.error.message,lastJob:job}]};
                    renderReportProgress();
                }''', failed_job)
                page.locator('[data-tab=tasks]').click()
                job_login = page.locator('#jobRows [data-login-platform=douyin]')
                assert job_login.is_visible() and job_login.inner_text() == '去平台连接登录'
                job_login.click()
                assert page.locator('#douyinLoginCard').evaluate('(node)=>node===document.activeElement')
                page.locator('[data-tab=overview]').click()
                page.locator('#reportProgress').evaluate('(node)=>node.open=true')
                progress_login = page.locator('#reportProgressRows [data-login-platform=douyin]')
                assert progress_login.is_visible() and progress_login.inner_text() == '去平台连接登录'
                progress_login.click()
                assert page.locator('#douyinLoginCard').evaluate('(node)=>node===document.activeElement')
                fixture.update(login='cookie_present', paused=None)
                with page.expect_response(lambda r: urlparse(r.url).path == '/api/v1/comments/probe'):
                    page.evaluate('refresh()')
                page.locator('[data-tab=tasks]').click()
                assert job_login.is_hidden()
                page.locator('[data-tab=overview]').click()
                assert progress_login.is_hidden()
                assert page.locator('#indexLoginStatus').inner_text() == '实时查询需登录 · 抖音已登录'
                assert page.locator('[data-login-platform]:visible').count() == 0
                page.locator('#openQuery').click()
                assert page.locator('#douyinQueryNote').is_hidden()
                page.locator('#closeQuery').click()
                fixture['jobs'] = []
                fixture['history']=False
                page.evaluate('sessionStorage.clear()')
                page.reload()
                page.wait_for_function("document.querySelector('#historyRows').textContent.includes('暂无结果')")
                assert page.locator('#queryStart').input_value()==''
                assert page.locator('#queryEnd').input_value()==''
                assert len(writes)==8 and writes[-1] == ('comments/probe', {}), writes
                fixture.update(topic_login='need_scan', topic_categories=False, topic_error=True)
                page.locator('[data-source=topic]').click()
                page.wait_for_function("!document.querySelector('#topicQrPanel').hidden && document.querySelector('#topicQrImage').naturalWidth > 0")
                assert page.locator('#topicLoginCard').is_visible()
                assert '/api/v1/topics/login/qr?' in page.locator('#topicQrDownload').get_attribute('href')
                fixture['topic_login'] = 'expired'
                page.evaluate('loadTopicLogin()')
                page.wait_for_function("document.querySelector('#topicLoginStatus').textContent.includes('二维码已失效，正在刷新')")
                assert page.locator('#topicQrPanel').is_hidden()
                assert page.locator('#topicQrImage').get_attribute('src') is None
                assert page.locator('#topicQrDownload').get_attribute('href') is None
                fixture['topic_login'] = 'need_scan'
                page.evaluate('loadTopicLogin()')
                page.wait_for_function("!document.querySelector('#topicQrPanel').hidden && document.querySelector('#topicQrImage').naturalWidth > 0")
                fixture['topic_qr_error'] = True
                page.evaluate('loadTopicLogin()')
                page.wait_for_function("document.querySelector('#topicQrPanel').hidden && !document.querySelector('#topicQrImage').hasAttribute('src')")
                assert page.locator('#topicQrDownload').get_attribute('href') is None
                fixture['topic_qr_error'] = False
                page.evaluate('loadTopicLogin()')
                page.wait_for_function("!document.querySelector('#topicQrPanel').hidden && document.querySelector('#topicQrImage').naturalWidth > 0")
                if output and width == 1280:
                    page.screenshot(path=str(output/'H46-after.png'), full_page=True)
                fixture['topic_error'] = False
                page.evaluate("""async () => {
                    const original = readingInfo;
                    readingInfo = () => true;
                    document.querySelector('#topicQrPanel').hidden = true;
                    try { await loadTopics(false); } finally { readingInfo = original; }
                }""")
                page.wait_for_function("!document.querySelector('#topicQrPanel').hidden && document.querySelector('#topicQrImage').naturalWidth > 0")
                page.locator('#topicLoginRefresh').click()
                page.wait_for_function("!topicLoginBusy && document.querySelector('#topicQrImage').naturalWidth > 0")
                page.locator('#topicCategoriesRefresh').click()
                page.wait_for_function("document.querySelector('#topicLoginCard').hidden")
                assert writes[-2:] == [('topics/login/refresh', {}), ('topics/categories/refresh', {})]
                fixture['topic_login'] = 'logged_in'
                for code, label in [('daily_limit', '上限'), ('interval', '超时'), ('paused', '暂停'),
                                    ('invalid_params', '参数'), ('invalid_parameters', '参数')]:
                    fixture['topic_collect_error'] = code
                    page.evaluate("chooseTopicCategory('科技')")
                    page.wait_for_function("label => document.querySelector('#topicStatus').textContent.includes(label)", arg=label)
                    assert label in page.locator('#topicRows').inner_text()
                fixture['topic_collect_error'] = None
                fixture['hold'] = 'topics/collect-once'
                page.evaluate("chooseTopicCategory('科技')")
                page.wait_for_function("document.querySelector('#topicRows').textContent.includes('采集中')")
                page.wait_for_timeout(100)
                assert held
                fixture['hold'] = None
                page.evaluate("chooseTopicCategory('旅行')")
                page.wait_for_function("document.querySelector('#topicStatus').textContent.includes('尚未采集')")
                old_route, old_data = held.pop()
                old_route.fulfill(content_type='application/json', body=json.dumps(dict(status='invalid_params')))
                page.wait_for_timeout(100)
                assert page.locator('#topicCategory').input_value() == '旅行'
                assert '参数' not in page.locator('#topicStatus').inner_text()
                # The sidebar reaches the single existing comments card without collecting.
                writes_before_navigation = len(writes)
                comments_nav = page.locator('.nav [data-tab=comments]')
                assert comments_nav.is_visible()
                assert '评论采集' in comments_nav.inner_text()
                with page.expect_response(lambda r: urlparse(r.url).path == '/api/v1/comments/probe'):
                    comments_nav.click()
                assert page.locator('[data-section=comments]').is_visible()
                assert page.locator('[data-section=comments] #overviewComments').is_visible()
                assert page.locator('#overviewComments').count() == 1
                assert page.locator('#overviewComments #commentsProbeButton').count() == 0
                assert page.locator('.top #desktopLogin').count() == 0
                assert page.locator('#pageTitle').inner_text() == '抖音评论采集'
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#commentsVideoInput').fill('合成草稿，不提交')
                for probe in ('not_logged_in', 'verification_required'):
                    fixture['comments_probe'] = probe
                    page.evaluate('loadComments()')
                    login = page.locator('#commentsLoginButton')
                    assert login.is_visible()
                    assert login.inner_text() == '前往平台连接登录'
                    login.click()
                    assert page.locator('[data-section=connections]').is_visible()
                    assert page.locator('#douyinLoginCard').is_visible()
                    assert page.locator('#douyinLoginCard #commentsProbeButton').is_visible()
                    assert page.evaluate("document.querySelector('#douyinLoginCard') === document.activeElement")
                    assert page.locator('#douyinLoginCard #desktopLogin').is_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    with page.expect_response(lambda r: urlparse(r.url).path == '/api/v1/comments/probe'):
                        comments_nav.click()
                    assert page.locator('#commentsVideoInput').input_value() == '合成草稿，不提交'
                page.locator('[data-tab=connections]').click()
                fixture['comments_probe'] = 'logged_in'
                with page.expect_response(lambda r: urlparse(r.url).path == '/api/v1/comments/probe'):
                    comments_nav.click()
                page.wait_for_function("document.querySelector('#commentsLoginButton').hidden")
                assert page.locator('#commentsLoginButton').is_hidden()
                assert not page.locator('#overviewComments button:visible').filter(has_text='登录').count()
                page.locator('[data-tab=overview]').click()
                assert page.locator('#overviewComments').is_hidden()
                assert writes[writes_before_navigation:] == [('comments/probe', {})] * 4
                # Login checks and the existing ticketed desktop action stay in platform connections.
                page.locator('[data-tab=connections]').click()
                assert page.locator('#commentsProbeButton').is_hidden()
                assert page.locator('#desktopLogin').is_hidden()
                fixture['comments_probe'] = 'verification_required'
                page.evaluate('loadComments()')
                with page.expect_response(lambda r: urlparse(r.url).path == '/api/v1/comments/probe'):
                    page.locator('#douyinLoginCard #commentsProbeButton').click()
                page.wait_for_function("!document.querySelector('#commentsProbeButton').disabled")
                assert writes[-1] == ('comments/probe', {})
                assert page.locator('#douyinLoginCard #commentsConnectionState').is_visible()
                assert page.locator('#commentsConnectionState').inner_text() == '评论主站：需要验证'
                fixture['login_mode'] = 'qr'
                for qr_state in ('idle', 'succeeded'):
                    fixture['qr_state'] = qr_state
                    page.evaluate('refresh()')
                    page.wait_for_function('!qrLoading')
                    page.evaluate('loadQr()')
                    assert page.locator('#qrLoginCard').is_visible()
                    assert page.locator('#desktopLoginMode').is_hidden()
                    assert page.locator('#qrDesktop').is_hidden()
                    assert page.locator('#douyinLoginCard #desktopLogin').is_visible()
                fixture['comments_probe'] = 'logged_in'
                page.evaluate('loadComments()')
                for mode in ('desktop', 'sms', 'qr'):
                    fixture.update(login_mode=mode, login='cookie_present', qr_state='succeeded')
                    page.evaluate('refresh()')
                    page.wait_for_function('!smsLoading && !qrLoading')
                    if mode == 'qr':
                        page.evaluate('loadQr()')
                        assert page.locator('#qrLoginCard').is_visible()
                        for selector in ('#qrStart', '#qrRefresh', '#qrCancel', '#qrDesktop'):
                            assert page.locator(selector).is_hidden(), selector
                    assert page.locator('#commentsProbeButton').is_hidden()
                    assert page.locator('#desktopLogin').is_hidden()
                    assert page.locator('#smsPhoneForm').is_hidden()
                    assert page.locator('#smsDesktop').is_hidden()
                # A later logout must restore QR actions even if the previous round succeeded.
                before_qr_success = len(writes)
                fixture['login'] = 'no_session_cookie'
                page.evaluate('refresh()')
                page.wait_for_function('!qrLoading')
                assert page.locator('#qrStart').is_visible()
                assert page.locator('#qrStart').evaluate('(node)=>typeof node.onclick === "function"')
                assert page.locator('#qrDesktop').is_hidden()
                assert page.locator('#desktopLogin').is_visible()
                assert len(writes) == before_qr_success
                page.evaluate("renderQr({state:'qr_ready',browser:'login_required',expires_in:180,session:{}})")
                qr_success = dict(state='succeeded', browser='browser_ready', session=dict(has_session=True))
                with page.expect_response(lambda r: urlparse(r.url).path == '/api/v1/comments/probe'):
                    page.evaluate('renderQr', qr_success)
                page.evaluate('renderQr', qr_success)
                for selector in ('#qrStart', '#qrRefresh', '#qrCancel', '#qrDesktop', '#desktopLogin'):
                    assert page.locator(selector).is_hidden(), selector
                assert writes[before_qr_success:] == [('comments/probe', {})]
                fixture['login'] = 'cookie_present'
                page.evaluate('refresh()')
                assert page.locator('#qrStart').is_hidden()
                assert writes[before_qr_success:] == [('comments/probe', {})]
                fixture.update(login='expired', qr_state='verification_required')
                page.evaluate('refresh()')
                page.wait_for_function('!qrLoading')
                page.evaluate('loadQr()')
                assert page.locator('#qrDesktop').is_visible()
                assert page.locator('#desktopLogin').is_hidden()
                fixture.update(login_mode='sms')
                page.evaluate('refresh()')
                page.wait_for_function('!smsLoading')
                assert page.locator('#smsPhoneForm').is_visible()
                assert page.locator('#smsDesktop').is_visible()
                assert page.locator('#desktopLogin').is_hidden()
                fixture.update(login='cookie_present', comments_probe='not_logged_in')
                page.evaluate('loadComments()')
                fixture['login_mode'] = 'desktop'
                page.evaluate('refresh()')
                assert page.locator('#desktopLogin').count() == 1
                assert page.locator('#douyinLoginCard #desktopLogin').is_visible()
                assert page.evaluate("document.querySelector('#desktopLogin').onclick === openDesktop")
                page.evaluate("""() => {
                    window.syntheticDesktop = {opened: false, navigated: false, closed: false};
                    window.open = () => {
                        window.syntheticDesktop.opened = true;
                        return {opener: null,
                            set location(value) { window.syntheticDesktop.navigated = Boolean(value); },
                            close() { window.syntheticDesktop.closed = true; }};
                    };
                }""")
                with page.expect_response(lambda r: urlparse(r.url).path == '/api/v1/desktop/ticket'):
                    page.locator('#desktopLogin').click()
                page.wait_for_function('window.syntheticDesktop.navigated')
                assert page.evaluate('window.syntheticDesktop.opened && !window.syntheticDesktop.closed')
                assert writes[-1] == ('desktop/ticket', {})
                assert writes[writes_before_navigation:] == [('comments/probe', {})] * 7 + [('desktop/ticket', {})]
                # Saved comments, progress and every displayed string use offline synthetic exports.
                page.evaluate("async () => {tab('comments');await loadCommentsRuns();await loadComments()}")
                assert page.locator('#commentsStatus').inner_text() == '采集状态：空闲'
                assert page.locator('#commentsRuns').inner_text() == '还没有评论运行记录。填写视频链接后点击开始采集。'
                assert page.locator('#commentsUsage').inner_text() == '今日用量未知 / 3000 · 每批最多 1500'
                fixture['comments_used'] = 17
                fixture['comments_output'] = dict(run_id='20261113T101112', requests=3, videos=[dict(
                    video_id='100', top_count=1, reply_count=2, status='complete', reason='exhausted')])
                fixture['comments_rows'] = [dict(video_id='100', comment_id='2', level='2', root_comment_id='1',
                    text='示例回复二', nickname='测试用户乙', reply_to_nickname='测试用户甲', ip_label='测试属地乙', like_count='0', created_at='2026-11-13 10:00'),
                    dict(video_id='100', comment_id='1', level='1', text='示例评论一 <img src=x onerror="window.commentXss=1">',
                         nickname='测试用户甲 <script>window.commentXss=1</script>', ip_label='测试属地甲', like_count='5', created_at='2026-11-13 09:00'),
                    dict(video_id='100', comment_id='3', level='2', root_comment_id='missing', text='示例独立回复三', nickname='测试用户丙')]
                fixture['comments_runs'] = [dict(run_id=run_id, finished_at='2026-11-13T10:11:12+08:00',
                    exit_reason='complete', requests=3, videos=fixture['comments_output']['videos'])
                    for run_id in ('20261113T101112', '20261112T101112')]
                page.locator('#commentsVideoInput').fill('https://synthetic.invalid/video/100')
                page.locator('#commentsStartButton').click()
                page.wait_for_function('commentsRunning')
                assert page.locator('#toast').inner_text() == '已开始采集…'
                assert page.locator('#commentsMessage').inner_text() == '已开始采集…'
                assert page.locator('#commentsStatus').inner_text() == '采集状态：采集中 · 已请求 3 次'
                latest = page.locator('#commentsLatest').inner_text()
                for expected in ('20261113T101112', '视频 100', '一级 1', '回复 2', '请求数：3', '已采完'):
                    assert expected in latest
                fixture['comments_running'] = False
                page.clock.fast_forward(5000)
                page.wait_for_function("document.querySelector('#commentsList').textContent.includes('示例评论一')")
                assert page.locator('#commentsStatus').inner_text() == '采集状态：已完成'
                assert page.locator('#toast').inner_text() == '采集完成'
                assert page.locator('#commentsUsage').inner_text() == '今日已用 17 / 3000 · 每批最多 1500'
                assert page.locator('#commentsRuns [data-comments-run]').count() == 2
                assert not page.locator('#commentsList .cm-replies').evaluate('(node)=>node.open')
                page.locator('#commentsList .cm-replies>summary').click()
                assert page.locator('#commentsList .cm-replies').evaluate('(node)=>node.open')
                rows = page.locator('#commentsList .comment-row')
                assert '示例评论一' in rows.nth(0).inner_text()
                assert '示例回复二' in rows.nth(1).inner_text()
                assert '示例独立回复三' in rows.nth(2).inner_text()
                assert rows.locator('.status').count() == 0
                assert '—' in rows.nth(2).locator('.tiny').inner_text()
                assert rows.nth(1).evaluate('(node)=>getComputedStyle(node).marginLeft') == '24px'
                assert '测试属地甲' in rows.nth(0).inner_text() and '点赞 5' in rows.nth(0).inner_text()
                assert '<img' in rows.nth(0).inner_text() and '<script>' in rows.nth(0).inner_text()
                assert page.locator('#commentsList img, #commentsList script').count() == 0
                assert page.evaluate('window.commentXss === undefined')
                assert page.locator('#commentsList').evaluate('(node)=>getComputedStyle(node).maxHeight') == '480px'
                assert page.locator('#commentsDownload').get_attribute('href') == '/api/v1/comments/runs/20261113T101112/comments.csv'
                page.locator('[data-comments-run="20261112T101112"]').click()
                page.wait_for_function("document.querySelector('#commentsList').textContent==='这次没有采到评论'")
                assert page.locator('#commentsDownload').get_attribute('href') == '/api/v1/comments/runs/20261112T101112/comments.csv'
                fixture['comments_output'] = None
                page.evaluate('async () => {commentsOutput=null;await loadCommentsRuns();await loadComments()}')
                assert '20261113T101112' in page.locator('#commentsLatest').inner_text()
                page.locator('[data-comments-run="20261113T101112"]').click()
                page.wait_for_function("document.querySelector('#commentsList').textContent.includes('示例评论一')")
                # New comments controls use the same routed, fictional run data.
                fixture['comments_rows'] += [
                    dict(video_id='100', comment_id='4', level='1', text='虚构的长评论' * 30,
                         nickname='虚构长文用户', like_count='2', created_at='2026-11-14T09:00:00+08:00', user_id='不可展示的虚构用户编号'),
                    dict(video_id='7300000000000000002', comment_id='5', level='1', text='另一条虚构视频的评论',
                         nickname='虚构另一用户', like_count='100', created_at='2026-11-12T09:00:00+08:00')]
                fixture['comments_runs'][0]['videos'] += [dict(video_id='7300000000000000002', title='虚构视频标题',
                    reported=1, top_count=1, reply_count=0, pages=1, status='complete', reason='exhausted')]
                page.evaluate("async () => {await loadCommentsRuns();await loadCommentsRun('20261113T101112')}")
                assert '另一条虚构视频' in page.locator('#commentsList .cm-thread').first.inner_text()
                assert '虚构视频标题' in page.locator('#commentsVideoSummary').inner_text()
                assert '不可展示的虚构用户编号' not in page.locator('#commentsList').inner_text()
                assert page.locator('#commentsVideo option[value="100"]').inner_text() == '视频 …100'
                assert page.locator('#commentsVideo option[value="7300000000000000002"]').inner_text() == '虚构视频标题'
                page.locator('#commentsSort').select_option('time')
                assert '虚构的长评论' in page.locator('#commentsList .cm-thread').first.inner_text()
                long_text = page.locator('#commentsList .cm-text')
                assert '2026-11-14 09:00' in page.locator('#commentsList .cm-thread').first.inner_text()
                assert not long_text.locator('p').is_visible()
                assert long_text.locator('summary').evaluate('(node)=>getComputedStyle(node).listStyleType') == 'none'
                assert long_text.locator('.cm-expand').is_visible()
                assert not long_text.locator('.cm-close').is_visible()
                long_text.locator('summary').click()
                assert long_text.locator('p').is_visible()
                assert long_text.locator('.cm-close').is_visible()
                assert not long_text.locator('.cm-expand').is_visible()
                long_text.locator('summary').click()
                assert not long_text.locator('p').is_visible()
                assert long_text.locator('.cm-expand').is_visible()
                page.locator('#commentsSearch').fill('示例回复二')
                assert page.locator('#commentsList .cm-thread').count() == 1
                assert '示例评论一' in page.locator('#commentsList .cm-thread').inner_text()
                page.locator('#commentsSearch').fill('虚构长文用户')
                assert page.locator('#commentsList .cm-thread').count() == 1
                assert '虚构的长评论' in page.locator('#commentsList').inner_text()
                page.locator('#commentsSearch').fill('')
                page.locator('#commentsVideo').select_option('7300000000000000002')
                assert page.locator('#commentsList .cm-thread').count() == 1
                assert '另一条虚构视频' in page.locator('#commentsList').inner_text()
                page.locator('#commentsVideo').select_option('')
                page.locator('#commentsSort').select_option('likes')
                page.evaluate("renderComments({probe:'verification_required',paused:true,pause_reason:'verification_required'})")
                assert page.locator('#commentsStatus').inner_text() == '采集状态：已暂停'
                assert page.locator('#commentsPauseReason').inner_text() == '暂停原因：需要验证'
                assert page.locator('#commentsProbeState').inner_text() == '主站登录：需要验证'
                page.evaluate("renderComments({running:true,waiting_for_index:true})")
                assert page.locator('#commentsStatus').inner_text() == '采集状态：等待指数采集'
                page.evaluate("renderComments({running:false,paused:true,pause_reason:'user'})")
                assert page.locator('#toast').inner_text() == '采集已暂停：手动暂停'
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                # Check the six primary surfaces at 360px as well; all requests remain routed above.
                fixture.update(login='cookie_present', login_mode='desktop', comments_probe='logged_in', hot_empty=True, hot_enabled=False, hot_paused=None, history=True, related='available', audience='available', missing_latest=False, all_missing=False)
                page.evaluate("async () => {reportBatch=null;await refresh();renderBaiduLogin({browser:'browser_ready'});closeInfo();$('#toast').hidden=true;$('#groupPanel').open=false}")
                fixture['comments_rows'] = [dict(row, text='示例评论一' if row['level']=='1' else '示例回复二', nickname='测试用户甲' if row['level']=='1' else '测试用户乙') for row in fixture['comments_rows']]
                fixture['comments_output'] = fixture['comments_runs'][0]
                page.evaluate("async () => {tab('comments');$('#commentsMessage').textContent='';await loadComments();await loadCommentsRuns();await loadCommentsRun('20261113T101112')}")
                # Capture only fictional comments; the malicious strings were checked above.
                if output:
                    page.evaluate('scrollTo(0,0)')
                    page.screenshot(path=str(output/f'hub-1113-comments-{width}.png'), full_page=True)
                for view in ('comments', 'overview', 'hot-empty', 'baidu-hot', 'connections', 'query'):
                    if view == 'overview':
                        page.evaluate("async () => {tab('overview');chosenPlatform='douyin';chosenSource='index';drawPlatforms();$('#keywordSelect').value='杭州';$('#insightStart').value='2026-10-02';$('#insightEnd').value='2026-10-31';updateRange('insight');resultTab('summary');await showSource()}")
                        assert page.locator('#indexLoginStatus').is_hidden()
                    elif view in ('hot-empty', 'baidu-hot'):
                        platform = 'douyin' if view == 'hot-empty' else 'baidu'
                        page.evaluate("async platform => {chosenPlatform=platform;chosenSource='hot_search';drawPlatforms();document.querySelector('#overviewHot > details.card').open=false;await loadHot();await loadHotRising()}", platform)
                        assert page.locator('#overviewHot [data-login-platform]').count() == 0
                        if platform == 'baidu':
                            assert '合成百度话题甲' in page.locator('#hotRows').inner_text()
                            assert page.locator('#hotRows td').nth(2).inner_text() == '654321'
                    elif view == 'connections':
                        page.evaluate("tab('connections')")
                        assert page.locator('#sessionDetail').is_visible()
                        assert page.locator('#douyinLoginCard #sessionDetail').count() == 1
                    elif view == 'query':
                        page.evaluate("tab('overview');chosenPlatform='douyin';chosenSource='index';drawPlatforms();$('#openQuery').click();$('#queryWords').value='合成关键词';$('#queryStart').value='2026-10-25';$('#queryEnd').value='2026-10-31';updateRange('query');$('#queryRange').open=false")
                    if output and view != 'comments':
                        page.evaluate('scrollTo(0,0)')
                        page.screenshot(path=str(output/f'hub-1113-{view}-{width}.png'), full_page=True)
                    page.set_viewport_size(dict(width=360, height=932))
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), view
                    if view == 'query':
                        assert page.locator('#queryDialog').evaluate('(n)=>n.scrollWidth<=n.clientWidth')
                        page.locator('#closeQuery').click()
                    if view == 'comments':
                        for selector in ('#commentsStartButton', '#commentsPauseButton', '#commentsResumeButton'):
                            assert page.locator(selector).evaluate('(n)=>n.getBoundingClientRect().height<50')
                    page.set_viewport_size(dict(width=width, height=height))
                assert not unexpected, unexpected
                assert not errors, errors
                print(f'PASS {width}px: defaults, all presets, leap/month ends, 731-day guard, tabs/keyboard/popovers, login notice, full data, missing values, offline submit, comments navigation/login')
                context.close()
        finally:
            browser.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshots',type=Path)
    args=parser.parse_args()
    if args.screenshots: args.screenshots.mkdir(parents=True,exist_ok=True)
    run(args.screenshots)
