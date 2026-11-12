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
            for width in (360, 430, 1280):
                page = browser.new_page(viewport={'width': width, 'height': 940})
                writes, errors, unexpected, login_opens = [], [], [], []
                low_volume = [False]
                baidu_state = ['login_in_progress']
                baidu_paused = [None]
                qr_failed, history_failed = [False], [False]
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
                        if qr_failed[0]:
                            return r.fulfill(status=503, content_type='application/json', body='{"error":{"code":"qr_not_ready"}}')
                        return r.fulfill(content_type='image/png', body=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aR1kAAAAASUVORK5CYII='))
                    if path == 'login/baidu/open':
                        assert r.request.method == 'POST'
                        assert r.request.headers.get('x-hub-request') == '1'
                        assert r.request.post_data_json == {}
                        login_opens.append(path)
                        return r.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'state': 'login_required'}))
                    if r.request.method == 'POST':
                        writes.append((path, r.request.post_data_json))
                        data = {'job': dict(id='synthetic-job', status='queued', cache_hit=False)}
                    elif path == 'platforms': data = {'platforms': PLATFORMS}
                    elif path == 'status':
                        data = dict(login_mode='desktop', browser='login_required', login_state='no_session_cookie',
                                    counts=dict(keywords=1, points=0), daily_online_attempts=0,
                                    limits=dict(online_attempts_per_day=50), cooldown_remaining_seconds=0,
                                    platform_status={'baidu': dict(browser=baidu_state[0], paused=baidu_paused[0],
                                        daily=dict(attempts=0, http_reserved=0),
                                        limits=dict(logical_daily=120, http_daily=400))})
                    elif path == 'history':
                        if history_failed[0]:
                            return r.fulfill(status=503, content_type='application/json', body='{"error":{"message":"合成历史读取失败"}}')
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
                    elif path == 'hot/latest': data = {'items': []}
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
                page.locator('[data-tab=connections]').click()
                assert page.locator('[data-section=connections] #baiduLoginCard').is_visible()
                assert page.locator('#baiduLoginCard #baiduLoginButton').is_visible()
                assert page.locator('#baiduLoginButton').count() == 1
                assert page.locator('#baiduQrPanel').count() == 1
                assert page.locator('#baiduLoginCard #baiduQrPanel').count() == 1
                assert page.locator('#queryDialog #baiduLoginButton, #queryDialog #baiduQrPanel').count() == 0
                assert page.locator('#baiduQueryTip, #baiduQueryStatus').count() == 0
                assert '登录中' in page.locator('#baiduLoginStatus').inner_text()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                if output:
                    page.screenshot(path=str(output/f'hub-connections-{width}.png'))
                assert not login_opens and not writes
                page.locator('[data-tab=overview]').click()
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
                baidu_state[0] = 'login_required'
                page.evaluate('refresh()')
                assert page.locator('#baiduLoginNotice').count() == 0
                assert not login_opens and not writes
                page.locator('#viewChips [data-source=hot_search]').click()
                assert page.locator('#baiduLoginNotice').count() == 0
                page.locator('#viewChips [data-source=index]').click()
                assert page.locator('#baiduLoginNotice').count() == 0
                baidu_state[0] = 'browser_ready'
                baidu_paused[0] = {'code': 'login_required'}
                page.evaluate('refresh()')
                page.locator('#openQuery').click()
                page.locator('#queryPlatform').select_option('baidu')
                assert page.locator('#baiduQueryNote').is_hidden()
                page.locator('#closeQuery').click()
                baidu_state[0] = 'login_required'
                baidu_paused[0] = None
                page.evaluate('refresh()')
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
                assert page.locator('#baiduQueryNote').inner_text() == '去平台连接登录'
                assert page.locator('#baiduQueryNote button').count() == 1
                query_login = page.locator('#baiduQueryNote [data-login-platform=baidu]')
                assert query_login.is_visible() and query_login.inner_text() == '去平台连接登录'
                if output:
                    page.screenshot(path=str(output/f'baidu-query-login-{width}.png'))
                query_login.focus()
                page.keyboard.press('Enter')
                assert not page.locator('#queryDialog').evaluate('(node)=>node.open')
                assert page.locator('[data-section=connections]').is_visible()
                assert page.locator('#baiduLoginCard').evaluate('(node)=>node===document.activeElement')
                page.locator('#baiduLoginButton').click()
                page.wait_for_function("document.querySelector('#baiduQrState').textContent.includes('使用百度')")
                assert login_opens == ['login/baidu/open']
                assert page.locator('#baiduQrImage').is_visible()
                assert page.locator('#baiduDesktop').is_hidden()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                qr_failed[0] = True
                page.locator('#baiduQrRefresh').click()
                page.wait_for_function("!document.querySelector('#baiduDesktop').hidden")
                assert page.locator('#baiduLoginCard #baiduDesktop').is_visible()
                assert page.locator('#baiduQrImage').is_hidden()
                assert page.evaluate("document.querySelector('#baiduDesktop').onclick === openDesktop")
                qr_failed[0] = False
                page.locator('#baiduQrRefresh').click()
                page.wait_for_function("document.querySelector('#baiduQrState').textContent.includes('使用百度')")
                assert page.locator('#baiduDesktop').is_hidden()
                baidu_state[0] = 'browser_ready'
                baidu_paused[0] = {'code': 'login_required'}
                page.evaluate('pollBaiduLogin()')
                page.wait_for_function("document.querySelector('#baiduQrState').textContent.includes('已登录')")
                assert page.locator('#baiduQrImage').is_hidden()
                assert page.locator('#baiduQrPanel').is_hidden()
                assert page.locator('#baiduLoginButton').is_hidden()
                assert '已登录' in page.locator('#baiduLoginStatus').inner_text()
                assert '采集已暂停' in page.locator('#baiduLoginStatus').inner_text()
                history_failed[0] = True
                page.evaluate('refresh()')
                assert page.locator('#baiduLoginButton').is_hidden()
                assert '已登录' in page.locator('#baiduLoginStatus').inner_text()
                history_failed[0] = False
                page.locator('[data-tab=overview]').click()
                assert page.locator('#baiduLoginNotice').count() == 0
                page.locator('#openQuery').click()
                assert page.locator('#queryPlatform').input_value() == 'baidu'
                assert page.locator('#baiduQueryNote').is_hidden()
                assert query_login.is_hidden()
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
                page.locator('#platformChips [data-platform=baidu]').click()
                baidu_state[0] = 'login_required'
                page.evaluate('refresh()')
                failed_job = dict(id='synthetic-login-failure', query=q, status='failed',
                                  created_at=0, error=dict(code='login_required', message='百度未登录'))
                page.evaluate('''job => {
                    jobs=[job];renderJobs();
                    reportBatch={query:job.query,entries:[{body:job.query,label:'指数趋势',
                        jobId:job.id,status:job.status,error:job.error.message,lastJob:job}]};
                    renderReportProgress();
                }''', failed_job)
                page.locator('[data-tab=tasks]').click()
                job_login = page.locator('#jobRows [data-login-platform=baidu]')
                assert job_login.is_visible() and job_login.inner_text() == '去平台连接登录'
                job_login.click()
                assert page.locator('#baiduLoginCard').evaluate('(node)=>node===document.activeElement')
                page.locator('[data-tab=overview]').click()
                page.locator('#reportProgress').evaluate('(node)=>node.open=true')
                progress_login = page.locator('#reportProgressRows [data-login-platform=baidu]')
                assert progress_login.is_visible() and progress_login.inner_text() == '去平台连接登录'
                progress_login.click()
                assert page.locator('#baiduLoginCard').evaluate('(node)=>node===document.activeElement')
                # A completed login does not clear the worker's manual-resume pause.
                for browser_state in ('browser_ready', 'signature_unavailable'):
                    for pause_code in ('login_required', 'authentication_required', 'verification_required', 'not_logged_in'):
                        page.evaluate('state => renderBaiduLogin(state)',
                                      dict(browser=browser_state, paused={'code': pause_code}))
                        assert page.locator('#baiduLoginButton').is_hidden()
                        assert page.locator('#baiduQrPanel').is_hidden()
                        assert '采集已暂停' in page.locator('#baiduLoginStatus').inner_text()
                        assert '需要登录' not in page.locator('#baiduLoginStatus').inner_text()
                        page.locator('[data-tab=tasks]').click()
                        assert job_login.is_hidden()
                        assert page.locator('#baiduResumeButton').is_visible()
                        page.locator('[data-tab=overview]').click()
                        assert progress_login.is_hidden()
                        assert page.locator('#baiduLoginNotice').count() == 0
                        page.locator('#openQuery').click()
                        assert page.locator('#baiduQueryNote').is_hidden()
                        page.locator('#closeQuery').click()
                        page.locator('[data-tab=connections]').click()
                # Rendering history after login must not recreate login actions.
                page.evaluate('renderJobs();renderReportProgress()')
                assert job_login.count() == 0 and progress_login.count() == 0
                page.evaluate("renderBaiduLogin({browser:'login_required'});renderJobs();renderReportProgress()")
                assert page.locator('#baiduLoginButton').is_visible()
                page.locator('[data-tab=tasks]').click()
                assert job_login.is_visible()
                page.locator('[data-tab=overview]').click()
                assert progress_login.is_visible()
                assert page.locator('#baiduLoginNotice').count() == 0
                page.locator('#openQuery').click()
                assert query_login.is_visible()
                page.locator('#closeQuery').click()
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
