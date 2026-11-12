"""Offline Baidu dashboard check: every request is fulfilled or aborted locally."""
import argparse
import base64
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
                low_volume = [False]
                baidu_state = ['login_in_progress']
                page.on('pageerror', lambda error: errors.append(str(error)))
                def route(r):
                    url = urlsplit(r.request.url)
                    if url.netloc != 'baidu-ui.invalid':
                        unexpected.append(r.request.url)
                        return r.abort()
                    path = url.path.removeprefix('/api/v1/')
                    if url.path == '/':
                        return r.fulfill(content_type='text/html', body=Path('hub.html').read_text())
                    if path == 'login/baidu/qr':
                        assert r.request.headers.get('x-hub-request') == '1'
                        return r.fulfill(content_type='image/png', body=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aR1kAAAAASUVORK5CYII='))
                    if path == 'login/baidu/open':
                        return r.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'state': 'login_required'}))
                    if r.request.method == 'POST':
                        writes.append((path, r.request.post_data_json))
                        data = {'job': dict(id='synthetic-job', status='queued', cache_hit=False)}
                    elif path == 'platforms': data = {'platforms': PLATFORMS}
                    elif path == 'status':
                        data = dict(login_mode='desktop', browser='login_required', login_state='no_session_cookie',
                                    counts=dict(keywords=1, points=0), daily_online_attempts=0,
                                    limits=dict(online_attempts_per_day=50), cooldown_remaining_seconds=0,
                                    platform_status={'baidu': dict(browser=baidu_state[0], paused=None,
                                        daily=dict(attempts=0, http_reserved=0),
                                        limits=dict(logical_daily=120, http_daily=400))})
                    elif path == 'history':
                        data = {'datasets': [{'metadata': {**raw['metadata'], 'keywords': ['合成词甲', '合成词乙'], 'verification': 'pending'}, 'fetched': dt.datetime.now().timestamp()}], 'latest_available_date': None}
                    elif path == 'keys': data = {'keys': []}
                    elif path == 'comments/status': data = {'probe': 'not_logged_in', 'running': False, 'paused': False}
                    elif path == 'insights/ranges': data = {'ranges': [], 'attempts': [], 'live_request': False}
                    elif path == 'insights':
                        params = parse_qs(url.query)
                        if params.get('platform') == ['baidu'] and params.get('kind') == ['related']:
                            data = {'status': 'available', 'result': {
                                'data': {'search_related': [{'keyword': '合成关联词', 'relation_score': None, 'search_index': None, 'composite_index': None}], 'content_related': []},
                                'metadata': {**q, 'verification': 'pending', 'source': '离线合成样例', 'fetched_at': '离线合成样例',
                                    'related_period': {'start_date': '2025-11-09', 'end_date': '2026-11-01', 'days': 358}}}}
                        elif params.get('platform') == ['baidu'] and params.get('kind') == ['audience']:
                            data = {'status': 'available', 'result': {'data': {'value_unit': 'upstream_raw', 'dimensions': [{'dimension': 'gender', 'name': '性别', 'labels': [{'name': '合成分组', 'value': 12, 'tgi': None}]}]}, 'metadata': {**q, 'verification': 'pending', 'source': '离线合成样例', 'fetched_at': '离线合成样例', 'audience_period': {'start_date': '2026-10-01', 'end_date': '2026-10-07', 'days': 7}}}}
                        else: data = {'status': 'no_data'}
                    elif path == 'jobs': data = {'jobs': []}
                    elif path == 'groups': data = {'groups': [], 'live_request': False}
                    elif path.startswith('jobs/'): data = {'job': dict(id='synthetic-job', status='queued', cache_hit=False)}
                    elif path == 'export.csv':
                        params = parse_qs(url.query)
                        assert params.get('device') == ['all_devices']
                        return r.fulfill(content_type='text/csv', body='\ufeffkeyword,device\r\n'+params['keyword'][0]+',all\r\n'+params['keyword'][0]+',pc\r\n'+params['keyword'][0]+',wise\r\n')
                    elif path == 'series':
                        params = parse_qs(url.query)
                        platform = params['platform'][0]
                        device = params.get('device', ['all'])[0]
                        data = {**b.DIMENSIONS, 'platform': platform, 'device': device, 'keyword': params['keyword'][0], 'points': [
                            dict(date=row['date'], value=None if low_volume[0] else (100 + i * 20 + {'all': 0, 'pc': 30, 'wise': 60}[device]), composite_index=200+i*10 if platform == 'douyin' else None, value_status='low_volume' if low_volume[0] else 'available') for i, row in enumerate(raw['rows']) if row['device'] == 'all']}
                    else:
                        unexpected.append(path)
                        return r.abort()
                    r.fulfill(content_type='application/json', body=json.dumps(data, ensure_ascii=False))
                page.route('**/*', route)
                page.goto('http://baidu-ui.invalid/')
                page.wait_for_function('!refreshing && platforms.length===3')
                assert '登录中' in page.locator('#baiduBudgetStatus').inner_text()
                page.locator('#insightRange').evaluate('(node)=>node.open=true')
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
                page.locator('#platformChips [data-platform=baidu]').click()
                page.locator('#insightRange').evaluate('(node)=>node.open=true')
                page.locator('#insightStart').fill(q['start_date'])
                page.locator('#insightEnd').fill(q['end_date'])
                page.evaluate('loadSeries()')
                page.wait_for_selector('#chart svg')
                assert page.locator('#chart svg polyline').count() >= 2
                assert '合成词甲' in page.locator('#chart').inner_text()
                assert '合成词乙' in page.locator('#chart').inner_text()
                with page.expect_download() as download_info:
                    page.locator('#exportLink').click()
                download = download_info.value
                csv = Path(download.path()).read_text(encoding='utf-8-sig')
                assert csv.count('keyword,device') == 1 and all(word in csv for word in ('合成词甲', '合成词乙'))
                page.locator('#chartMode').select_option('indexed')
                page.wait_for_function("document.querySelector('#metricLabel').textContent.includes('独立归一化')")
                assert page.locator('#chart svg polyline').count() >= 2
                for device in ('all', 'pc', 'wise'):
                    page.locator(f'[data-device={device}]').click()
                    page.wait_for_function('(device) => document.querySelector("#exportLink").href.includes("device=all_devices")', arg=device)
                    page.wait_for_selector('#chart svg circle')
                    assert page.locator('#chart svg circle').count() > 0
                    if output and width == 1280:
                        page.screenshot(path=str(output/f'baidu-trend-{device}-desktop.png'))
                if output and width == 1280:
                    page.locator('#baiduDevices .info-button').hover()
                    page.screenshot(path=str(output/'baidu-tips-desktop.png'))
                    page.keyboard.press('Escape')
                assert '综合指数' not in page.locator('#trendSummary').inner_text()
                assert 'platform=baidu' in page.locator('#exportLink').get_attribute('href')
                assert page.locator('#resultTab-related').is_visible()
                assert page.locator('#resultTab-audience').is_visible()
                page.locator('#resultTab-related').click()
                page.wait_for_function("document.querySelector('#relatedData').textContent.includes('合成关联词')")
                related = page.locator('#relatedData')
                related.locator('.info-button').hover()
                assert '百度相关词窗口：2025-11-09 至 2026-11-01（358 天）' in related.inner_text()
                assert '离线合成样例' in related.inner_text()
                assert '页面核对待记录' in related.inner_text()
                assert page.locator('#relatedSort option[value=composite_index]').is_hidden()
                page.locator('#resultTab-audience').click()
                page.wait_for_function("document.querySelector('#audienceData').textContent.includes('合成分组')")
                assert '页面核对待记录' in page.locator('#audienceData').inner_text()
                assert '单位为上游原值、尚未与官方页核对' in page.locator('#audienceData').inner_text()
                assert '2026-10-01 至 2026-10-07' in page.locator('#audienceData').inner_text()
                assert '单日快照' not in related.inner_text()
                if output and width == 1280:
                    page.screenshot(path=str(output/'2026-11-05-baidu-related-period-ui-desktop.png'))
                page.keyboard.press('Escape')
                page.locator('#resultTab-trend').click()
                low_volume[0] = True
                page.evaluate('loadSeries()')
                page.wait_for_function("document.querySelector('#trendSummary').textContent.includes('低量')")
                assert page.locator('#chart svg').count() == 0
                page.locator('#openQuery').click()
                page.locator('#queryPlatform').select_option('baidu')
                assert page.locator('#baiduQueryNote').is_visible()
                assert '中枢扫码登录' in page.locator('#baiduQueryNote').inner_text()
                page.locator('#baiduLoginButton').click()
                page.wait_for_function("document.querySelector('#baiduQrState').textContent.includes('使用百度')")
                assert page.locator('#baiduQrImage').is_visible()
                page.locator('#baiduQrRefresh').click()
                page.wait_for_function("document.querySelector('#baiduQrState').textContent.includes('使用百度')")
                baidu_state[0] = 'browser_ready'
                page.evaluate('pollBaiduLogin()')
                page.wait_for_function("document.querySelector('#baiduQrState').textContent.includes('已登录')")
                assert page.locator('#baiduQrImage').is_hidden()
                assert page.locator('#submitButton').inner_text() == '查询 →'
                if output and width == 1280:
                    page.screenshot(path=str(output/'baidu-query-desktop.png'))
                page.locator('#queryWords').fill(q['keywords'][0])
                page.locator('#queryRange').evaluate('(node)=>node.open=true')
                page.locator('#queryStart').fill(q['start_date'])
                page.locator('#queryEnd').fill(q['end_date'])
                page.locator('#submitButton').click()
                page.wait_for_function('reportBatch?.entries.every(e=>e.jobId)')
                assert writes == [('queries', q), ('insights/queries', {**q, 'kind': 'related'}), ('insights/queries', {**q, 'kind': 'audience'})], writes
                page.locator('#platformChips [data-platform=douyin]').click()
                assert page.locator('#baiduWords').is_hidden() and page.locator('#keywordSelect').is_visible()
                page.wait_for_function("!document.querySelector('#showComposite').parentElement.hidden")
                assert not errors, errors
                assert not unexpected, unexpected
                print(f'PASS Baidu {width}px: selectable, pending login, null/low-volume, source, trend and related tasks')
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
