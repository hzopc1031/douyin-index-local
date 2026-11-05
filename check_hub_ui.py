"""Offline Playwright UI checks/screenshots. Every URL is fulfilled locally.

Run: .venv/bin/python check_hub_ui.py --screenshots /path/to/output
No gateway, account, browser profile or live platform endpoint is used.
"""
import argparse
import json
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
PLATFORMS = [dict(id=key, name=name, available=key=='douyin', metric_name='搜索指数', description='离线合成数据')
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
                fixture = {'history': True, 'related': 'available', 'audience': 'available', 'missing_latest': False, 'all_missing': False, 'login': 'cookie_present', 'paused': None,
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
                        data = dict(browser='browser_ready',login_state=fixture['login'],login_mode='desktop',counts=dict(keywords=1,points=7),
                                    daily_online_attempts=0,limits=dict(online_attempts_per_day=50),cooldown_remaining_seconds=0,paused=fixture['paused'],last_result=None)
                    elif path == 'history': data = dict(datasets=[dict(metadata=META,fetched=1793498400)] if fixture['history'] else [], latest_available_date=END if fixture['history'] else None)
                    elif path == 'jobs': data = dict(jobs=[])
                    elif path == 'keys': data = dict(keys=[])
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
                    elif path == 'topics':
                        data = dict(status='disabled', status_label='未启用', window='1d', window_label='近1天',
                                    rank=query.get('rank', ['total'])[0],
                                    rank_label='飙升' if query.get('rank', ['total'])[0] == 'rising' else '总榜',
                                    category=query.get('category', ['全部'])[0],
                                    categories=['全部', '美食', '旅行'], items=[], fetched_at=None,
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
                    elif path.startswith('jobs/mock-'): data = dict(job=dict(id=path[5:], status='queued',cache_hit=False))
                    if data is None:
                        unexpected.append(request.method+' '+request.url)
                        request_route.abort()
                    elif path == fixture.get('hold'): held.append((request_route, data))
                    else: request_route.fulfill(content_type='application/json',body=json.dumps(data))

                context.route('**/*', route)
                page.goto('http://hub-ui.invalid/')
                page.wait_for_function("document.querySelector('#audienceSummary').textContent.includes('浙江')", timeout=10000)
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
                page.evaluate("reportBatch=makeReportBatch({platform:'douyin',keywords:['其他词'],start_date:'2026-10-25',end_date:'2026-10-31'},'unconfirmed')")
                page.locator('#submitButton').click()
                assert '上一批' in page.locator('#queryError').inner_text()
                assert len(writes)==0
                page.evaluate('reportBatch=null;saveBatch()')
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
                page.locator('[data-platform=baidu]').click()
                page.wait_for_function("document.querySelector('#relatedSummary').textContent.includes('尚未接入')")
                assert '尚未接入' in page.locator('#trendSummary').inner_text()
                assert page.locator('#viewChips').is_hidden()
                assert page.locator('#groupPanel').is_hidden()
                assert page.locator('#overviewHot').is_hidden()
                page.locator('[data-platform=douyin]').click()
                assert page.locator('#viewChips').is_visible()
                assert '搜索指数' in page.locator('#viewChips').inner_text()
                assert '热搜榜' not in page.locator('#platformChips').inner_text()
                assert page.locator('[data-tab=hot]').count() == 0
                assert '热榜' not in page.locator('.nav').inner_text()
                page.locator('[data-source=hot_search]').click()
                page.wait_for_function("document.querySelector('#hotRows').textContent.includes('合成话题甲')")
                assert '搜索页热榜' in page.locator('#hotIntro').inner_text()
                assert '不是搜索指数' in page.locator('#hotIntro').inner_text()
                assert '热点宝' in page.locator('#hotIntro').inner_text()
                assert page.locator('#hotResume').is_hidden()
                assert page.locator('#hotRows tr').nth(0).locator('td').nth(0).inner_text() == '1'
                assert page.locator('#hotRows tr').nth(0).locator('td').nth(2).inner_text() == '12083012'
                assert page.locator('#hotRows tr').nth(1).locator('td').nth(0).inner_text() == '2'
                assert page.locator('#hotRows tr').nth(1).locator('td').nth(2).inner_text() == '—'
                page.locator('#hotSentence').fill('s1')
                page.locator('#hotWordButton').click()
                page.wait_for_function("document.querySelector('#hotHistory').textContent.includes('合成话题甲')")
                page.locator('#hotRisingButton').click()
                page.wait_for_function("document.querySelector('#hotRisingRows').textContent.includes('合成话题甲')")
                assert '本库热榜快照' in page.locator('#hotRisingNote').inner_text()
                assert '不是搜索指数' in page.locator('#hotRisingNote').inner_text()
                assert '名次上升 ≥ 10' in page.locator('#hotRisingThresholds').inner_text()
                assert page.locator('#hotRisingRows tr').nth(0).locator('td').nth(7).inner_text() == '—'
                assert page.locator('#hotRisingRows tr').nth(0).locator('td').nth(9).inner_text() == '—'
                assert page.locator('#topicCategory').is_hidden()
                assert page.locator('#groupPanel').is_hidden()
                page.locator('[data-source=index]').click()
                assert page.locator('#groupPanel').is_visible()
                assert page.locator('[data-tab=groups]').count() == 0
                assert '关键词组' not in page.locator('.nav').inner_text()
                page.locator('[data-tab=overview]').click()
                page.wait_for_function("document.querySelector('#groupSelect').textContent.includes('合成组')")
                assert '工作台热度分' in page.locator('#groupIntro').inner_text()
                assert '不是搜索次数' in page.locator('#groupIntro').inner_text()
                assert '名称只用于显示' in page.locator('#groupIntro').inner_text()
                assert '不会触发搜索查询' in page.locator('#groupIntro').inner_text()
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
                assert '搜索页热榜' in page.locator('#hotIntro').inner_text()
                assert '不是搜索指数' in page.locator('#hotIntro').inner_text()
                assert page.locator('#keywordSelect').is_hidden()
                assert page.locator('#resultTab-summary').is_hidden()
                assert page.locator('#groupPanel').is_hidden()
                assert page.locator('#topicCategory').is_hidden()
                page.locator('[data-source=topic]').click()
                page.wait_for_function("document.querySelector('#topicCategory').textContent.includes('美食')")
                assert page.locator('#topicWindow').inner_text() == '近1天'
                assert '未启用' in page.locator('#topicStatus').inner_text()
                assert '不是搜索指数' in page.locator('#topicIntro').inner_text()
                assert '待联调' in page.locator('#topicStatus').inner_text()
                page.locator('#topicCategory').select_option('旅行')
                page.locator('#topicRankRising').click()
                page.wait_for_function("document.querySelector('#topicRankRising').getAttribute('aria-pressed')==='true'")
                assert '合成话题' not in page.locator('#topicRows').inner_text()
                assert '0' not in page.locator('#topicRows').inner_text()
                page.locator('[data-platform=douyin]').click()
                assert page.locator('#openQuery').is_visible()
                assert page.locator('#groupPanel').is_visible()
                assert page.locator('#overviewTopic').is_hidden()
                page.locator('[data-tab=overview]').click()
                # A real click submits only the mocked routes and preserves existing request shape.
                page.locator('#openQuery').click()
                page.locator('#submitButton').click()
                page.wait_for_function("reportBatch?.entries.every(e=>e.jobId)")
                assert len(writes)==4
                assert [path for path,_ in writes]==['groups/'+'a'*32,'queries','insights/queries','insights/queries']
                assert writes[1][1]==dict(platform='douyin',keywords=['杭州'],start_date=START,end_date=END)
                assert not page.locator('#queryDialog').is_visible()
                page.wait_for_function('!refreshing')
                fixture.update(login='expired', paused=dict(code='authentication_required'))
                page.evaluate('refresh()')
                assert '需要重新登录' in page.locator('#browserBadge').inner_text()
                assert page.locator('#notice').is_visible()
                assert '恢复采集' in page.locator('#notice').inner_text()
                assert not page.locator('#browserBadge .info-button').count()
                fixture.update(login='cookie_present', paused=None)
                fixture['history']=False
                page.evaluate('sessionStorage.clear()')
                page.reload()
                page.wait_for_function("document.querySelector('#historyRows').textContent.includes('暂无结果')")
                assert page.locator('#queryStart').input_value()==''
                assert page.locator('#queryEnd').input_value()==''
                assert len(writes)==4
                assert not unexpected, unexpected
                assert not errors, errors
                print(f'PASS {width}px: defaults, all presets, leap/month ends, 731-day guard, tabs/keyboard/popovers, login notice, full data, missing values, offline submit')
                context.close()
        finally:
            browser.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshots',type=Path)
    args=parser.parse_args()
    if args.screenshots: args.screenshots.mkdir(parents=True,exist_ok=True)
    run(args.screenshots)
