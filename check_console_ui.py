"""Console presentation regression and screenshots; synthetic GET routes only."""
import argparse
import json
import re
import runpy
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
HOME = runpy.run_path(str(ROOT / 'check_home_ui.py'))
OV, NOW = HOME['OV'], HOME['NOW']
RUN, VIDEO = '20261114T103000', '7300000000000000001'
VIDEOS = [dict(video_id=VIDEO, title='虚构视频：窗边的秋日散步记录', reported=138, displayed='138', top_count=121, reply_count=2, pages=26, status='partial', reason='video_page_budget')]
RUNS = [dict(run_id=RUN, finished_at=NOW.isoformat(), requests=26, videos=VIDEOS, exit_reason='video_page_budget')]
ROWS = [dict(video_id=VIDEO, comment_id=str(i), level='1', root_comment_id=str(i), text=text, nickname=name, like_count=str(likes), ip_label='浙江', created_at=NOW.isoformat()) for i, (text, name, likes) in enumerate([
    ('虚构评论：把路边的小风景留在这里，慢慢走总会有新的发现。', '虚构小竹', 128),
    ('虚构评论：'+ '这段长评论用于检查展开和折叠后的阅读。'*14, '虚构小云', 86),
    ('虚构评论：周末也想带上一杯热茶去散步。', '虚构小禾', 9)])]
ROWS.append(dict(ROWS[0], comment_id='reply', level='2', root_comment_id='0', text='虚构回复：下次一起出门。', nickname='虚构小鹤', like_count='16'))


def static_checks():
    html = (ROOT / 'hub.html').read_text()
    css = html.split('<style>')[1].split('</style>')[0]
    assert len(re.findall(r':root\s*\{', css)) == 1
    assert not re.search(r'gradient\(|--ui-color|--electric|--hero|transition\s*:|animation\s*:', css.replace('transition:none', '').replace('animation:none', ''))
    assert not re.search(r'INDEX ATLAS|洞察趋势|抢先一步|hero-kicker|[✳✦]', html, re.I)
    assert 'env(safe-area-inset-bottom)' in css and 'minmax(0,1fr)' in css
    assert set(re.findall(r'var\((--[\w-]+)', css)) <= set(re.findall(r'(--[\w-]+)\s*:', css))
    print('PASS static: one theme, no hero/gradient/motion, safe area and shrinkable grid')


def main(output=None):
    static_checks()
    output = Path(output) if output else None
    if output:
        output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for width in (375, 430, 768, 769, 800, 850, 1280):
            mobile = width <= 768
            context = browser.new_context(viewport=dict(width=width, height=932 if mobile else 1000), locale='zh-CN', timezone_id='Asia/Shanghai', service_workers='block', has_touch=mobile, is_mobile=mobile)
            errors, unexpected, sockets, held = [], [], [], []
            state = dict(loading=True, empty=False, error=False, running=True)
            def websocket(ws):
                sockets.append('blocked')
                ws.close()
            context.route_web_socket('**/*', websocket)
            def route(r):
                u = urlparse(r.request.url)
                if r.request.method != 'GET' or u.netloc != 'console.synthetic.invalid':
                    unexpected.append((r.request.method, u.hostname, u.path))
                    r.abort()
                    return
                if u.path == '/':
                    r.fulfill(content_type='text/html', body=(ROOT/'hub.html').read_text())
                    return
                key, query = u.path.removeprefix('/api/v1/'), parse_qs(u.query)
                if key == 'comments/runs' and state['loading']:
                    held.append(r)
                    return
                if key == 'comments/runs' and state['error']:
                    r.fulfill(status=503, content_type='application/json', body=json.dumps(dict(error=dict(message='演示：读取失败，请稍后刷新'))))
                    return
                data = {
                    'platforms': dict(platforms=OV['PLATFORMS']),
                    'status': dict(browser='browser_ready', login_state='cookie_present', login_mode='desktop', counts=dict(keywords=1, points=7), limits=dict(online_attempts_per_day=50), daily_online_attempts=12, hotlist=dict(enabled=True)),
                    'history': dict(datasets=[dict(metadata=OV['META'], fetched=1793498400)], latest_available_date=OV['END']),
                    'jobs': dict(jobs=HOME['JOBS']), 'groups': dict(groups=[]), 'keys': dict(keys=[]), 'login/sms': dict(state='idle'),
                    'comments/status': dict(probe='logged_in', running=state['running'], progress=HOME['PROGRESS'] if state['running'] else None),
                    'comments/runs': dict(runs=[] if state['empty'] else RUNS, usage=dict(today_used=626, daily_limit=3000, batch_limit=1500)),
                    'comments/runs/'+RUN: dict(run_id=RUN, total=0 if state['empty'] else len(ROWS), truncated=False, comments=[] if state['empty'] else ROWS),
                    'series': dict(keyword='演示关键词', points=[dict(date=f'2026-10-{25+i}', value=12000+i*900, composite_index=30000+i*1200) for i in range(7)]),
                    'insights/ranges': dict(ranges=[], attempts=[], live_request=False),
                    'hot/latest': dict(items=HOME['HOT'], fetched_at=NOW.isoformat()), 'hot/rising': dict(items=[], live_request=False),
                }.get(key)
                if key == 'insights':
                    data = dict(status='available', result=dict(metadata=OV['META'], data=dict(search_related=OV['RELATED'], content_related=OV['RELATED'][10:]) if query['kind'][0]=='related' else dict(dimensions=OV['AUDIENCE'])))
                if data is None:
                    unexpected.append(('unhandled', u.path))
                    r.abort()
                    return
                r.fulfill(content_type='application/json', body=json.dumps(data, ensure_ascii=False))
            context.route('**/*', route)
            page = context.new_page()
            page.clock.set_fixed_time(NOW)
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto('https://console.synthetic.invalid/')
            page.wait_for_function("document.querySelector('#homeJobs').textContent.includes('虚构银杏') && !refreshing")
            assert held and '正在读取' in page.locator('#homeCommentsUsage').inner_text()
            state['loading'] = False
            for r in held:
                r.fulfill(content_type='application/json', body=json.dumps(dict(runs=RUNS, usage=dict(today_used=626, daily_limit=3000, batch_limit=1500))))
            held.clear()
            page.wait_for_function("document.querySelector('#homeCommentsUsage').textContent.includes('626')")

            contrast = page.evaluate("""()=>{
                const ctx=document.createElement('canvas').getContext('2d',{willReadFrequently:true});
                const lum=token=>{ctx.clearRect(0,0,1,1);ctx.fillStyle=getComputedStyle(document.documentElement).getPropertyValue(token);ctx.fillRect(0,0,1,1);const c=Array.from(ctx.getImageData(0,0,1,1).data).slice(0,3).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4});return .2126*c[0]+.7152*c[1]+.0722*c[2]};
                const ratio=(a,b)=>{const x=lum(a),y=lum(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05)};
                return Object.fromEntries(['--bg','--panel','--deep'].flatMap(bg=>['--ink','--ink-2','--muted','--cool'].map(fg=>[fg+'/'+bg,ratio(fg,bg)] )).concat([['button',ratio('--accent-ink','--accent')]]));
            }""")
            assert all(v >= (4.5 if '--muted' in k or k=='button' else 7) for k,v in contrast.items()), contrast
            if width == 1280:
                print('PASS contrast: '+json.dumps({k:round(v,2) for k,v in contrast.items()}))

            def check_layout(view):
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (view, width)
                outside = page.locator('.card:visible,.summary-grid:visible,.home-heading:visible,.source-toolbar:visible,.overview-toolbar:visible,input:visible,select:visible,textarea:visible,.connection-rows:visible,.nav:visible').evaluate_all('(ns)=>ns.filter(n=>{const b=n.getBoundingClientRect();return b.x<0 || b.right>innerWidth+1}).map(n=>n.id||n.className)')
                assert not outside, (view,width,'clipped content',outside)
                assert page.locator('input:visible,select:visible,textarea:visible').evaluate_all('(ns)=>ns.every(n=>parseFloat(getComputedStyle(n).fontSize)>=16)'), (view,width,'input font')
                small = page.locator('button:visible,input:visible:not([type=checkbox]),select:visible,textarea:visible,summary:visible,.btn:visible').evaluate_all('(ns)=>ns.filter(n=>n.getBoundingClientRect().height<44).map(n=>[n.id,n.className,n.getBoundingClientRect().height])')
                assert not small, (view,width,'touch height',small)
                assert page.locator('body').evaluate('(n)=>getComputedStyle(n).fontVariantNumeric') == 'tabular-nums'

            def screenshot(view):
                page.evaluate('scrollTo(0,0)')
                if output:
                    page.screenshot(path=str(output/f'hub-console-polish-{view}-{width}.png'), full_page=True)
                check_layout(view)

            screenshot('home')
            assert page.locator('.home-usage').bounding_box()['y'] < page.locator('.home-columns').bounding_box()['y'] < page.locator('.home-shortcuts').bounding_box()['y']
            assert '上海自然日' in page.locator('.home-heading').inner_text()
            assert page.locator('#homeHot .tiny').is_visible()
            assert page.locator('.home-hot strong').evaluate_all('(ns)=>ns.every(n=>getComputedStyle(n).textAlign==="right" && getComputedStyle(n).fontVariantNumeric==="tabular-nums")')
            if mobile:
                nav = page.locator('.nav')
                assert nav.evaluate('(n)=>getComputedStyle(n).position') == 'fixed'
                assert nav.locator('button:visible').count() == 5
                page.locator('.home-shortcuts .btn').last.focus()
                page.locator('.home-shortcuts .btn').last.scroll_into_view_if_needed()
                assert page.locator('.home-shortcuts .btn').last.bounding_box()['y']+44 <= nav.bounding_box()['y']
                page.evaluate('scrollTo(0,0)')
                page.locator('#connectionDisclosure summary').click()
                assert page.locator('.connection-rows').is_visible()
                trigger = page.locator('#connectionDisclosure summary').bounding_box()
                assert page.locator('.connection-rows').bounding_box()['y'] >= trigger['y']+trigger['height']+4
                for row in page.locator('.connection-row').all():
                    box = row.bounding_box()
                    assert box['x'] >= 0 and box['x']+box['width'] <= width and box['y']+box['height'] < nav.bounding_box()['y']
                screenshot('connection')
                page.locator('#connectionDisclosure summary').click()
            else:
                assert page.locator('.connection-rows').is_visible()
                assert page.locator('.nav').evaluate('(n)=>getComputedStyle(n).position') == 'static'
                assert page.locator('.side').bounding_box()['width'] < page.locator('.main').bounding_box()['x']+1

            page.locator('#homeQuery').click()
            assert page.locator('#queryDialog').is_visible()
            page.locator('#queryRange').evaluate('(n)=>n.open=true')
            page.locator('#queryWords').fill('演示关键词')
            page.locator('#queryWords').focus()
            assert page.locator('#queryWords').evaluate('(n)=>getComputedStyle(n).outlineStyle') == 'solid'
            screenshot('query')
            page.locator('#submitButton').focus()
            page.locator('#submitButton').scroll_into_view_if_needed()
            box=page.locator('#submitButton').bounding_box()
            assert box['y'] >= 0 and box['y']+box['height'] <= page.viewport_size['height']
            page.keyboard.press('Escape')
            assert page.locator('#queryDialog').is_hidden()

            page.evaluate("tab('overview')")
            page.wait_for_function("document.querySelector('#audienceSummary .audience-leader')")
            screenshot('trend')
            assert page.locator('.rank-value').first.evaluate('(n)=>getComputedStyle(n).textAlign') == 'right'
            page.locator('#resultTab-trend').click()
            assert page.locator('#chart svg').is_visible()
            screenshot('chart')
            page.locator('#resultTab-summary').click()
            page.evaluate("async()=>{tab('comments');await loadCommentsRuns();await loadCommentsRun('"+RUN+"')}")
            assert page.locator('.cm-thread').count() == 3
            assert page.locator('.cm-meta>span:first-of-type').evaluate_all('(ns)=>ns.every(n=>getComputedStyle(n).textAlign==="right" && getComputedStyle(n).fontVariantNumeric==="tabular-nums")')
            assert page.locator('#commentsStartButton').is_disabled()
            assert page.locator('#commentsPauseButton').is_visible()
            screenshot('comments')
            for field in ('#commentsSearch', '#commentsSort'):
                assert page.locator(field).evaluate('(n)=>n.clientWidth-parseFloat(getComputedStyle(n).paddingLeft)-parseFloat(getComputedStyle(n).paddingRight)>=200'), (field,width)
                page.locator(field).focus()
                assert page.locator(field).evaluate('(n)=>getComputedStyle(n).outlineWidth') == '2px'
                box = page.locator(field).bounding_box()
                if mobile:
                    assert box['y']+box['height'] < page.locator('.nav').bounding_box()['y']
            page.locator('#commentsSearch').fill('不存在的演示评论')
            assert '没有匹配' in page.locator('#commentsList').inner_text()
            page.locator('#commentsSearch').fill('')
            state.update(empty=True, running=False)
            page.evaluate("async()=>{await loadComments();await loadCommentsRuns();await loadCommentsRun('"+RUN+"')}")
            assert '这次没有采到评论' in page.locator('#commentsList').inner_text()
            assert page.locator('#commentsStartButton').is_enabled()
            assert page.locator('#commentsPauseButton').is_hidden()
            state['error'] = True
            page.evaluate('loadCommentsRuns()')
            assert '读取失败' in page.locator('#commentsRuns').inner_text()
            assert page.locator('#homeCommentsUsage [data-home-retry]').count() == 1
            page.emulate_media(reduced_motion='reduce')
            assert page.locator('button').first.evaluate('(n)=>getComputedStyle(n).transitionDuration') == '0s'
            assert not errors, errors
            assert not unexpected, unexpected
            assert not sockets, sockets
            print(f'PASS console {width}: home/trend/chart/comments/query/connection, loading/empty/error, font/focus/nav/filters/numbers, external/write/WebSocket=0')
            context.close()
        browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--screenshots')
    main(parser.parse_args().screenshots)
