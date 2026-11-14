"""Synthetic home/navigation checks; all requests and WebSockets stay intercepted."""
import json
import runpy
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
OV = runpy.run_path(str(ROOT / 'check_hub_ui.py'))
NOW = datetime(2026, 11, 14, 10, 30, tzinfo=ZoneInfo('Asia/Shanghai'))
HOT = [dict(position=i+1, word=word, hot_value=12000000-i*743000, fetched_at=NOW.isoformat()) for i, word in enumerate([
    '虚构：城市书店的周末夜读', '虚构：小院里的秋日音乐会', '虚构：江边步道新添休息长椅', '虚构：社区食堂的暖心午餐', '虚构：年轻人重拾手写信',
    '虚构：屋顶花园迎来收获季', '虚构：一杯热茶的慢时光', '虚构：公交站旁的口袋公园', '虚构：旧物修补课堂开课了', '虚构：图书馆里的无声陪伴'])]
PROGRESS = dict(started_at='2026-11-14T10:29:00+08:00', finished_at=None, requests=38, batch_limit=1500, top_items=620, reply_items=143, video_index=2, video_total=4)
JOBS = [dict(id='synthetic-'+status, status=status, query=dict(keywords=[word], start_date='2026-11-07', end_date='2026-11-13', platform='douyin'), created_at=1794623280, cache_hit=False) for status, word in [('running', '虚构银杏'), ('queued', '虚构热茶')]]


def main(output=None):
    output = Path(output) if output else None
    if output:
        output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for width in (1280, 430, 768, 769):
            mobile=width<=768
            context = browser.new_context(viewport=dict(width=width, height=932 if mobile else 1000), locale='zh-CN', timezone_id='Asia/Shanghai', service_workers='block', has_touch=mobile, is_mobile=mobile)
            errors, unexpected, sockets, requests = [], [], [], []
            state = dict(idle=False, fail=set(), login=False, baidu=False)
            def websocket(ws):
                sockets.append('blocked')
                ws.close()
            context.route_web_socket('**/*', websocket)
            def route(r):
                u = urlparse(r.request.url)
                if r.request.method!='GET' or u.netloc!='home.synthetic.invalid':
                    unexpected.append((r.request.method, u.hostname, u.path))
                    r.abort()
                    return
                if u.path=='/':
                    r.fulfill(content_type='text/html', body=(ROOT/'hub.html').read_text())
                    return
                key, query = u.path.removeprefix('/api/v1/'), parse_qs(u.query)
                requests.append(key)
                if key in state['fail']:
                    r.fulfill(status=503, content_type='application/json', body=json.dumps(dict(error=dict(message='虚构读取失败'))))
                    return
                data = {
                    'platforms':dict(platforms=OV['PLATFORMS']),
                    'status':dict(browser='login_required' if state['login'] else 'browser_ready', login_state='expired' if state['login'] else 'cookie_present', login_mode='desktop', counts=dict(keywords=1, points=7), limits=dict(online_attempts_per_day=50), daily_online_attempts=12, hotlist=dict(enabled=not state['idle'], interval_seconds=3600, daily_success_limit=48, daily_successes=10), **({'platform_status':{'baidu':dict(browser='browser_idle', daily=dict(attempts=3, http_reserved=5), limits=dict(logical_daily=20, http_daily=100))}} if state['baidu'] else {})),
                    'history':dict(datasets=[dict(metadata=OV['META'], fetched=1793498400)], latest_available_date=OV['END']),
                    'jobs':dict(jobs=[] if state['idle'] else JOBS), 'groups':dict(groups=[]), 'keys':dict(keys=[]), 'login/sms':dict(state='idle'),
                    'comments/status':dict(probe='logged_in', running=not state['idle'], progress=None if state['idle'] else PROGRESS),
                    'comments/runs':dict(runs=[], usage=dict(today_used=626, daily_limit=3000, batch_limit=1500)),
                    'series':dict(keyword='杭州', points=[dict(date=f'2026-10-{25+i}', value=12000+i*900, composite_index=30000+i*1200) for i in range(7)]),
                    'insights/ranges':dict(ranges=[], attempts=[], live_request=False),
                    'hot/latest':dict(items=[] if state['idle'] else HOT, fetched_at=None if state['idle'] else NOW.isoformat()),
                    'hot/rising':dict(items=[] if state['idle'] else [dict(item, rank_rise=15-i*3, new_entry=i==0, heat_pct=0.8-i*.2, rank_rise_hit=True, heat_hit=True, z_score=None, z_hit=None, search_index_ratio=None) for i,item in enumerate(HOT[:3])], current_fetched_at=None if state['idle'] else NOW.isoformat(), baseline_fetched_at='2026-11-14T09:30:00+08:00', thresholds=dict(min_age_seconds=1800,max_age_seconds=7200,lookback_seconds=86400,rank_rise=10,rank_max=30,heat_pct=.5,heat_delta=100000,z_min=2,z_min_samples=5)),
                }.get(key)
                if key=='insights':
                    data=dict(status='available',result=dict(metadata=OV['META'],data=dict(search_related=OV['RELATED'],content_related=OV['RELATED'][10:]) if query['kind'][0]=='related' else dict(dimensions=OV['AUDIENCE'])))
                if data is None:
                    unexpected.append(('unhandled', u.path))
                    r.abort()
                    return
                r.fulfill(content_type='application/json',body=json.dumps(data,ensure_ascii=False))
            context.route('**/*',route)
            page=context.new_page()
            page.clock.set_fixed_time(NOW)
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto('https://home.synthetic.invalid/')
            page.wait_for_function("document.querySelector('#homeJobs').textContent.includes('虚构银杏') && document.querySelector('#homeHot li') && !refreshing")
            assert page.locator('[data-section=home]').is_visible()
            assert page.locator('[data-tab=home]').get_attribute('aria-current')=='page'
            assert page.locator('[data-section=overview]').is_hidden()
            nav=page.locator('.nav')
            for text in ('首页','热榜','抖音评论采集'):
                assert text in nav.text_content()
            assert page.locator('#homeHot li').count()==5
            assert '620' in page.locator('#homeCommentsTask').inner_text()
            assert '运行中' in page.locator('#homeJobs').inner_text() and '排队中' in page.locator('#homeJobs').inner_text()
            assert '626' in page.locator('#homeCommentsUsage').inner_text()
            assert page.locator('#homeBaiduMeter').is_hidden()
            assert page.locator('.mobile-more').is_visible()==mobile
            assert page.locator('.nav-icon').first.is_visible()==mobile
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            if output and width in (1280,430):
                page.screenshot(path=str(output/f'hub-home-{width}.png'),full_page=True)
                if mobile:
                    page.screenshot(path=str(output/'hub-home-tabbar-430.png'),full_page=False)
            if mobile:
                assert nav.evaluate('(n)=>getComputedStyle(n).position')=='fixed'
                assert nav.locator('button:visible').count()==5
                assert nav.locator('button:visible').evaluate_all('(ns)=>ns.every(n=>n.getBoundingClientRect().height>=44)')
                page.evaluate('scrollTo(0,document.body.scrollHeight)')
                assert page.locator('.home-shortcuts').bounding_box()['y']+page.locator('.home-shortcuts').bounding_box()['height'] <= nav.bounding_box()['y']
            assert page.locator('.nav').evaluate('(n)=>getComputedStyle(n).position') == ('fixed' if mobile else 'static')
            page.locator('#homeQuery').click()
            assert page.locator('#queryDialog').is_visible()
            page.evaluate("$('#queryDialog').close()")
            page.locator('#homeComments').click()
            assert page.locator('[data-section=comments]').is_visible()
            assert page.locator('#commentsVideoInput').evaluate('(n)=>n===document.activeElement')
            page.evaluate("tab('home')")
            page.locator('[data-home-tab=hot]').first.click()
            page.wait_for_function("document.querySelector('#hotRows tr')?.textContent.includes('城市书店')")
            assert page.locator('[data-section=hot] #overviewHot').is_visible()
            assert page.locator('#hotRows tr').count()==10
            assert page.locator('[data-rising-key]:visible').count()==4
            page.locator('#hotRisingDetails').check()
            assert page.locator('[data-rising-key]:visible').count()==11
            page.locator('#hotRisingDetails').uncheck()
            if output and width==1280:
                page.evaluate('scrollTo(0,0)')
                page.screenshot(path=str(output/'hub-home-hot-1280.png'),full_page=True)
            page.locator('[data-tab=overview]').click()
            assert page.locator('[data-section=overview] #platformChips').is_visible()
            assert page.locator('#viewChips [data-source=hot_search]').count()==0
            page.locator('[data-tab=home]').click()
            # Every failed home read stays local and recovers through its own retry.
            for key, panel, loader in [('comments/runs','homeCommentsUsage','loadHomeUsage()'),('comments/status','homeCommentsTask','loadComments()'),('hot/latest','homeHot','loadHomeHot()'),('status','homeIndexUsage','loadHubStatus().catch(()=>{})'),('jobs','homeJobs','refresh()')]:
                state['fail']={key}
                page.evaluate(loader)
                assert page.locator('#'+panel+' [data-home-retry]').is_visible(), panel
                assert '读取失败' in page.locator('#'+panel).inner_text()
                if key=='hot/latest':
                    page.evaluate('loadHubStatus()')
                    assert page.locator('#homeHot [data-home-retry]').is_visible()
                assert '626' in page.locator('#homeCommentsUsage').inner_text() or key=='comments/runs'
                state['fail']=set()
                page.locator('#'+panel+' [data-home-retry]').click()
                page.wait_for_function('(id)=>!document.querySelector("#"+id+" [data-home-retry]")',arg=panel)
            state.update(idle=True, baidu=True, login=True)
            page.evaluate('refresh()')
            page.wait_for_function("!refreshing && document.querySelector('#homeJobs').textContent.includes('暂无运行')")
            assert page.locator('#homeConnections').evaluate('(n)=>n.classList.contains("needs-login")')
            assert page.locator('#homeBaiduMeter').is_visible()
            page.wait_for_function("document.querySelector('#homeCommentsTask').textContent.includes('空闲')")
            assert page.locator('#homeHot').inner_text().count('未开启')==1
            if output and width==1280:
                page.evaluate("scrollTo(0,0);$('#toast').hidden=true")
                page.screenshot(path=str(output/'hub-home-idle-1280.png'),full_page=True)
            if mobile:
                page.locator('[data-tab=more]').click()
                assert page.locator('[data-section=more]').is_visible()
                page.locator('[data-section=more] [data-home-tab=connections]').click()
                assert page.locator('[data-section=connections]').is_visible()
                assert page.locator('[data-tab=more]').get_attribute('aria-current')=='page'
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert not errors, errors
            assert not unexpected, unexpected
            assert not sockets, sockets
            print(f'PASS home {width}: default, usage, queue, retries, shortcuts, hot, mobile, pageerror=0, external/write/WebSocket=0')
            context.close()
        browser.close()


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--screenshots')
    main(parser.parse_args().screenshots)
