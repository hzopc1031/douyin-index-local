"""Offline Baidu dashboard check: every request is fulfilled or aborted locally."""
import argparse
import datetime as dt
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright
from hub_store import PLATFORMS
from test_baidu_adapter import query
import baidu_adapter as b


def run(output=None):
    q = query()
    raw = b.public_result(b.page_absence(q, 'low_volume'))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for width in (430, 1280):
                page = browser.new_page(viewport={'width': width, 'height': 940})
                writes, errors, unexpected = [], [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                def route(r):
                    url = urlsplit(r.request.url)
                    if url.netloc != 'baidu-ui.invalid':
                        unexpected.append(r.request.url)
                        return r.abort()
                    path = url.path.removeprefix('/api/v1/')
                    if url.path == '/':
                        return r.fulfill(content_type='text/html', body=Path('hub.html').read_text())
                    if r.request.method == 'POST':
                        writes.append((path, r.request.post_data_json))
                        data = {'job': dict(id='synthetic-job', status='queued', cache_hit=False)}
                    elif path == 'platforms': data = {'platforms': PLATFORMS}
                    elif path == 'status':
                        data = dict(login_mode='desktop', browser='login_required', login_state='no_session_cookie',
                                    counts=dict(keywords=1, points=0), daily_online_attempts=0,
                                    limits=dict(online_attempts_per_day=50), cooldown_remaining_seconds=0,
                                    platform_status={'baidu': dict(browser='login_in_progress', paused=None,
                                        daily=dict(attempts=0, http_reserved=0),
                                        limits=dict(logical_daily=120, http_daily=400))})
                    elif path == 'history':
                        data = {'datasets': [{'metadata': raw['metadata'], 'fetched': dt.datetime.now().timestamp()}], 'latest_available_date': None}
                    elif path == 'keys': data = {'keys': []}
                    elif path == 'jobs': data = {'jobs': []}
                    elif path.startswith('jobs/'): data = {'job': dict(id='synthetic-job', status='queued', cache_hit=False)}
                    elif path == 'series':
                        params = parse_qs(url.query)
                        platform = params['platform'][0]
                        device = params.get('device', ['all'])[0]
                        data = {**b.DIMENSIONS, 'platform': platform, 'device': device, 'keyword': q['keywords'][0], 'points': [
                            dict(date=row['date'], value=(100 + i * 20 + {'all': 0, 'pc': 30, 'wise': 60}[device]), composite_index=200+i*10 if platform == 'douyin' else None, value_status='available') for i, row in enumerate(raw['rows']) if row['device'] == 'all']}
                    else:
                        unexpected.append(path)
                        return r.abort()
                    r.fulfill(content_type='application/json', body=json.dumps(data, ensure_ascii=False))
                page.route('**/*', route)
                page.goto('http://baidu-ui.invalid/')
                page.wait_for_function('!refreshing && platforms.length===3')
                assert '登录中' in page.locator('#baiduBudgetStatus').inner_text()
                page.locator('#insightStart').fill(q['start_date'])
                page.locator('#insightEnd').fill(q['end_date'])
                page.locator('#resultTab-trend').click()
                page.evaluate('loadSeries()')
                page.wait_for_selector('#chart svg', timeout=5000)
                if output and width == 1280:
                    page.screenshot(path=str(output/'hub-douyin-desktop.png'))
                    page.locator('#chartMeta .info-button').hover()
                    page.screenshot(path=str(output/'hub-douyin-tips-desktop.png'))
                    page.keyboard.press('Escape')
                page.locator('[data-platform=baidu]').click()
                page.locator('#insightRange').evaluate('(node)=>node.open=true')
                page.locator('#insightStart').fill(q['start_date'])
                page.locator('#insightEnd').fill(q['end_date'])
                page.evaluate('loadSeries()')
                page.wait_for_selector('#chart svg')
                assert page.locator('#chart svg polyline').count() > 0
                for device in ('all', 'pc', 'wise'):
                    page.locator(f'[data-device={device}]').click()
                    page.wait_for_function('(device) => document.querySelector("#exportLink").href.includes("device="+device)', device)
                    assert page.locator('#chart svg circle').count() > 0
                    if output and width == 1280:
                        page.screenshot(path=str(output/f'baidu-trend-{device}-desktop.png'))
                if output and width == 1280:
                    page.locator('#baiduDevices .info-button').hover()
                    page.screenshot(path=str(output/'baidu-tips-desktop.png'))
                    page.keyboard.press('Escape')
                assert '综合指数' not in page.locator('#trendSummary').inner_text()
                assert 'platform=baidu' in page.locator('#exportLink').get_attribute('href')
                page.locator('#openQuery').click()
                page.locator('#queryPlatform').select_option('baidu')
                assert page.locator('#baiduQueryNote').is_visible()
                assert '远程桌面扫码登录' in page.locator('#baiduQueryNote').inner_text()
                assert page.locator('#submitButton').inner_text() == '查询 →'
                if output and width == 1280:
                    page.screenshot(path=str(output/'baidu-query-desktop.png'))
                page.locator('#queryWords').fill(q['keywords'][0])
                page.locator('#queryRange').evaluate('(node)=>node.open=true')
                page.locator('#queryStart').fill(q['start_date'])
                page.locator('#queryEnd').fill(q['end_date'])
                page.locator('#submitButton').click()
                page.wait_for_function('reportBatch?.entries.every(e=>e.jobId)')
                assert writes == [('queries', q)], writes
                assert not errors, errors
                assert not unexpected, unexpected
                print(f'PASS Baidu {width}px: selectable, pending login, null/low-volume, source, single task, no extra modules')
                page.close()
        finally:
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    run(args.screenshots)
