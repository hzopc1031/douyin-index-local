"""Synthetic audience-state/range regression and screenshots; all network is intercepted.

.venv/bin/python check_audience_ui.py --screenshots /workspace/content/dev
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright
from check_hub_ui import AUDIENCE, META, PLATFORMS, ROOT, START, END

OLD_START, OLD_END = '2026-09-26', '2026-10-25'


def run(output=None):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for width, height in [(430, 932), (1280, 900)]:
                context = browser.new_context(viewport=dict(width=width, height=height), locale='zh-CN',
                                              timezone_id='Asia/Shanghai', service_workers='block')
                page = context.new_page()
                page.clock.set_fixed_time(datetime(2026, 11, 2, tzinfo=timezone.utc))
                errors, unexpected, writes, held = [], [], [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                fixture = dict(status='not_collected', attempts=[], ranges=[], jobs=[], legacy=False,
                               range_error=False, hold=False, read_error=False)

                def route(r):
                    req, url = r.request, urlparse(r.request.url)
                    if url.netloc != 'audience-ui.invalid':
                        unexpected.append(req.url)
                        r.abort()
                        return
                    if req.method != 'GET':
                        writes.append(req.url)
                        r.abort()
                        return
                    if url.path == '/':
                        r.fulfill(content_type='text/html', body=(ROOT/'hub.html').read_text())
                        return
                    path, q = url.path.removeprefix('/api/v1/'), parse_qs(url.query)
                    data = None
                    if path == 'platforms': data = dict(platforms=PLATFORMS)
                    elif path == 'status':
                        data = dict(browser='browser_ready', login_state='cookie_present', login_mode='desktop',
                                    counts=dict(keywords=2, points=14), daily_online_attempts=0,
                                    limits=dict(online_attempts_per_day=50), cooldown_remaining_seconds=0, paused=None, last_result=None)
                    elif path == 'history': data = dict(datasets=[dict(metadata={**META, 'keywords':['酵素','akg']}, fetched=1793498400)])
                    elif path == 'jobs': data = dict(jobs=fixture['jobs'])
                    elif path == 'keys': data = dict(keys=[])
                    elif path == 'series': data = dict(points=[])
                    elif path == 'insights/ranges':
                        if fixture['legacy'] or fixture['range_error']:
                            r.fulfill(status=404 if fixture['legacy'] else 503, content_type='text/html', body='unavailable')
                            return
                        data = dict(ranges=fixture['ranges'], attempts=fixture['attempts'], live_request=False)
                    elif path == 'insights':
                        status = fixture['status'] if q['kind'][0]=='audience' else 'not_collected'
                        data = dict(status=status, live_request=False, result=None)
                        if status in ('available','no_data'):
                            data['result'] = dict(metadata={**META, 'start_date':q['start_date'][0], 'end_date':q['end_date'][0]},
                                                  data=dict(dimensions=AUDIENCE), no_data=status=='no_data')
                        if q['kind'][0]=='audience' and fixture['hold']:
                            held.append((r, data))
                            return
                        if q['kind'][0]=='audience' and fixture['read_error']:
                            r.fulfill(status=503, content_type='application/json', body=json.dumps({'error':{'message':'合成读取错误'}}))
                            return
                    if data is None:
                        unexpected.append(req.url)
                        r.abort()
                    else:
                        r.fulfill(content_type='application/json', body=json.dumps(data))

                context.route('**/*', route)
                page.goto('http://audience-ui.invalid/')
                page.wait_for_function("document.querySelector('#audienceSummary').textContent.startsWith('未采集')")
                page.get_by_role('tab', name='人群特征', exact=True).click()
                assert '酵素' in page.locator('#audienceData').inner_text()
                assert '没有已存' in page.locator('#audienceRanges').inner_text()
                # Opening the existing query form prefills context but does not submit.
                page.locator('#collectAudience').click()
                assert page.locator('#queryDialog').is_visible()
                assert page.locator('#queryWords').input_value() == '酵素'
                assert page.locator('#queryStart').input_value() == START
                assert page.locator('#queryEnd').input_value() == END
                assert not writes
                page.locator('#closeQuery').click()
                page.evaluate("""() => {const n=document.createElement('span');n.textContent=' · 离线合成样例';n.style='font-size:10px;color:#b2c8bc';document.querySelector('.brand').append(n)}""")
                page.evaluate('scrollTo(0,0)')
                if output and width == 430:
                    page.screenshot(path=str(output/'hub-audience-uncollected.png'))

                # Complete server history must retain not-yet-created tasks from this session.
                page.evaluate("reportBatch=makeReportBatch({platform:'douyin',keywords:['酵素'],start_date:'2026-10-25',end_date:'2026-10-31'},'synthetic-pending');renderReportProgress();loadInsights()")
                assert '待提交' in page.locator('#audienceData').inner_text()
                assert '继续提交未确认任务' in page.locator('#audienceData').inner_text()
                assert not page.locator('#collectAudience').count()
                page.evaluate("reportBatch.entries.find(e=>e.body.kind==='audience').error='synthetic disconnect';loadInsights()")
                assert '提交未确认' in page.locator('#audienceData').inner_text()
                assert not writes
                page.evaluate('reportBatch=null;renderReportProgress()')

                for status, attempt, title in [
                    ('no_data', None, '平台无数据'),
                    ('not_collected', dict(status='failed', error_code='business_error'), '采集失败'),
                    ('not_collected', dict(status='no_data', error_code='skipped_trend_no_data'), '趋势无数据已跳过'),
                    ('not_collected', dict(status='queued'), '人群采集待完成'),
                    ('future_state', None, '人群状态待确认'),
                ]:
                    fixture['status'] = status
                    fixture['attempts'] = [{**attempt, 'start_date':START, 'end_date':END, 'updated_at':1793600000}] if attempt else []
                    page.evaluate('loadInsights()')
                    assert page.locator('#audienceData h3').inner_text() == title
                    assert page.locator('#audienceSummary').inner_text().startswith(title)
                    assert not page.locator('#audienceData .rank-row').count()
                # Failed reads are not failed collections, and old figures must disappear.
                fixture.update(status='available', attempts=[])
                page.evaluate('loadInsights()')
                assert page.locator('#audienceData .rank-row').count() > 0
                fixture['read_error'] = True
                page.evaluate('loadInsights()')
                assert '读取失败' in page.locator('#audienceData').inner_text()
                assert not page.locator('#audienceData .rank-row').count()
                fixture.update(read_error=False, range_error=True, status='not_collected')
                page.evaluate('loadInsights()')
                assert '列表读取失败' in page.locator('#audienceRanges').inner_text()
                fixture['range_error'] = False

                # Full list deduplicates ranges and excludes platform-empty dates.
                fixture.update(status='available', ranges=[
                    dict(start_date=START, end_date=END, status='available'),
                    dict(start_date=OLD_START, end_date=OLD_END, status='available'),
                    dict(start_date=OLD_START, end_date=OLD_END, status='available'),
                    dict(start_date='2026-08-01', end_date='2026-08-07', status='no_data')])
                page.locator('#keywordSelect').select_option('akg')
                page.wait_for_function("document.querySelectorAll('[data-audience-range]').length===2")
                page.locator('[data-audience-range]').nth(1).click()
                page.wait_for_function("document.querySelector('#audienceData').textContent.includes('2026-09-26')")
                assert page.locator('#insightStart').input_value() == OLD_START
                assert page.locator('#insightEnd').input_value() == OLD_END
                assert page.locator('[data-audience-range][aria-pressed=true]').count() == 1
                assert page.locator('#audienceData .insight-block').count() == len(AUDIENCE)
                assert '—' in page.locator('#audienceData').inner_text()  # missing TGI stays missing
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.evaluate('scrollTo(0,0)')
                if output:
                    page.screenshot(path=str(output/('hub-audience-ranges.png' if width==430 else 'hub-audience-desktop.png')))
                # An available saved result wins over a later failed/skipped task.
                fixture['attempts'] = [dict(start_date=OLD_START, end_date=OLD_END, status='failed', error_code='business_error')]
                page.evaluate('loadInsights()')
                assert page.locator('#audienceData .rank-row').count() > 0

                # A late success/failure for the old keyword must never repaint a new selection.
                for late_error in (False, True):
                    fixture.update(hold=True, status='available')
                    page.evaluate('void loadInsights()')
                    page.wait_for_timeout(50)
                    assert held
                    fixture.update(hold=False, status='not_collected', ranges=[], attempts=[])
                    page.locator('#keywordSelect').select_option('酵素' if page.locator('#keywordSelect').input_value()=='akg' else 'akg')
                    page.wait_for_function("document.querySelector('#audienceSummary').textContent.startsWith('未采集')")
                    for r, data in held:
                        r.fulfill(status=503 if late_error else 200, content_type='application/json', body=json.dumps({'error':{'message':'old failure'}} if late_error else data))
                    held.clear()
                    page.wait_for_timeout(50)
                    assert page.locator('#audienceSummary').inner_text().startswith('未采集')

                # HTML-only upgrade on the old gateway: plain-text 404 falls back to recent jobs.
                fixture.update(legacy=True, status='not_collected', jobs=[dict(id='synthetic', query=dict(platform='douyin', keywords=['akg'], kind='audience', start_date=START, end_date=END),
                               status='succeeded', result=dict(no_data=False), created_at=1793600000)])
                page.evaluate('refresh()')
                page.locator('#keywordSelect').select_option('akg')
                page.evaluate('loadInsights()')
                assert '可能遗漏更早' in page.locator('#audienceRanges').inner_text()
                assert page.locator('[data-audience-range]').count() == 1
                assert '无法确认是否曾失败或跳过' in page.locator('#audienceData').inner_text()
                fixture['jobs'][0].update(status='no_data', error=dict(code='skipped_trend_no_data'))
                page.evaluate("$('#insightStart').value='2026-10-25';$('#insightEnd').value='2026-10-31';refresh()")
                assert page.locator('#audienceData h3').inner_text() == '趋势无数据已跳过'
                page.locator('[data-platform=baidu]').click()
                assert page.locator('#audienceRanges').inner_text() == ''
                assert '尚未接入' in page.locator('#audienceData').inner_text()
                assert not errors, errors
                assert not unexpected, unexpected
                assert not writes, writes
                print(f'PASS audience {width}px: four states, full/legacy ranges, explicit switching, stale responses, zero writes')
                context.close()
        finally:
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshots', type=Path)
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    run(args.screenshots)
