"""Exercise a second platform with synthetic routes only."""
import json
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from check_hub_ui import PLATFORMS, META

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page()
    writes = []
    def route(r):
        request = r.request
        path = urlparse(request.url).path
        if path == '/':
            r.fulfill(content_type='text/html', body=Path('hub.html').read_text())
            return
        name = path.removeprefix('/api/v1/')
        values = {
            'platforms': {'platforms': PLATFORMS + [{'id': 'fake_wechat', 'name': '合成微信', 'available': True,
                'metric_name': '合成指数', 'unit': '分', 'region': '全国', 'description': '合成来源', 'modules': []}]},
            'status': {'browser': 'browser_ready', 'login_state': 'cookie_present', 'login_mode': 'desktop',
                'counts': {'keywords': 1, 'points': 1}, 'daily_online_attempts': 0,
                'limits': {'online_attempts_per_day': 50}, 'cooldown_remaining_seconds': 0, 'paused': None},
            'history': {'datasets': [{'metadata': META, 'fetched': 1}], 'latest_available_date': '2026-10-31'},
            'jobs': {'jobs': []}, 'keys': {'keys': []},
            'series': {'platform': 'fake_wechat', 'keyword': '杭州', 'points': [{'date': '2026-10-31', 'value': 42}]},
            'insights/ranges': {'ranges': [], 'attempts': []},
            'insights': {'status': 'empty'},
        }
        if request.method == 'POST':
            writes.append((name, request.post_data_json))
            data = {'job': {'id': 'mock', 'status': 'queued', 'cache_hit': False}}
        else:
            data = values.get(name, {'job': {'id': 'mock', 'status': 'queued', 'cache_hit': False}})
        r.fulfill(content_type='application/json', body=json.dumps(data))
    page.route('**/*', route)
    page.goto('http://hub-ui.invalid/')
    page.locator('[data-platform="fake_wechat"]').click()
    page.wait_for_function("document.querySelector('#trendSummary').textContent.includes('合成指数')")
    assert page.locator('#resultTab-related').is_hidden()
    assert page.locator('#resultTab-audience').is_hidden()
    assert '合成来源' in page.locator('#chartMeta').inner_text()
    page.locator('#openQuery').click()
    assert page.locator('#queryPlatform').input_value() == 'fake_wechat'
    page.locator('#queryWords').fill('杭州')
    page.locator('#submitButton').click()
    page.wait_for_function("!document.querySelector('#queryDialog').open")
    assert writes and writes[0][0] == 'queries' and writes[0][1]['platform'] == 'fake_wechat'
    assert all(name != 'insights/queries' for name, _ in writes)
    browser.close()
print('Synthetic second-platform UI passed')
