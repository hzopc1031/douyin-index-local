const assert = require('assert');
const vm = require('vm');
const {execFileSync} = require('child_process');
const guard = execFileSync('python3', ['-c', 'from dyc.hub_comments import GUARD_JS; print(GUARD_JS)'], {env:{...process.env, PYTHONPATH:'tools/douyin_comments'}, encoding:'utf8'});
const arm = execFileSync('python3', ['-c', 'from dyc.hub_comments import ARM_JS; print(ARM_JS)'], {env:{...process.env, PYTHONPATH:'tools/douyin_comments'}, encoding:'utf8'});
let calls = 0, urls = [];
class XHR {
  open(method, url) { this.url = url; }
  addEventListener(event, callback) { this.callback = callback; }
  send() { calls++; urls.push(this.url); this.status = 200; this.responseText = '{"status_code":0}'; this.callback(); }
  abort() { this.aborted = true; }
}
const context = {URL, location:{href:'https://www.douyin.com/'}, sessionStorage:{getItem:()=>null, removeItem:()=>{}}, localStorage:{getItem:k=>k==='xmst'?'fresh':null}, XMLHttpRequest:XHR};
context.window = context;
context.fetch = () => { calls++; return Promise.resolve({status:200, clone:()=>({text:()=>Promise.resolve('{"status_code":0}')})}); };
vm.runInNewContext(guard, context);
(async () => {
  const base = '/aweme/v1/web/comment/list/';
  const storage = new Map();
  const sessionStorage = {getItem:k=>storage.get(k) ?? null, setItem:(k,v)=>storage.set(k,v), removeItem:k=>storage.delete(k)};
  function documentContext() {
    class PageXHR {
      open(method, url) { this.url = url; PageXHR.lastUrl = url; }
      addEventListener(event, callback) { this.callback = callback; }
      send() { this.status = 200; this.responseText = '{}'; this.callback(); }
      abort() { this.aborted = true; }
    }
    const ctx = {URL, location:{href:'https://www.douyin.com/jingxuan'}, sessionStorage, localStorage:{getItem:()=>null}, XMLHttpRequest:PageXHR, fetch:()=>Promise.resolve({status:200, clone:()=>({text:()=>Promise.resolve('{}')})})};
    ctx.window = ctx;
    return ctx;
  }
  const page = documentContext();
  vm.runInNewContext(guard, page);
  const armPage = vm.runInNewContext(arm, page);
  assert(armPage({video:'100', root:''}));
  page.location.href = 'https://www.douyin.com/video/100';
  const wrong = new page.XMLHttpRequest(); wrong.open('GET', base + '?aweme_id=101'); wrong.send();
  assert(wrong.aborted); assert.equal(page.__dycSent, false);
  const first = new page.XMLHttpRequest(); first.open('GET', base + '?aweme_id=100'); first.send();
  assert.equal(page.__dycSent, true); assert.equal(JSON.parse(page.__dycResult).status, 200);
  assert(armPage({video:'100', root:'5'}));
  const replyFirst = new page.XMLHttpRequest(); replyFirst.open('GET', base + 'reply/?item_id=100&comment_id=5&reply_marker=kept'); replyFirst.send();
  assert.equal(page.__dycSent, true); assert.equal(JSON.parse(page.__dycResult).status, 200);
  assert(armPage({video:'100', root:'5'}));
  assert(page.__dycReplay({video:'100', root:'5', cursor:'1'}));
  assert.equal(page.__dycSent, true);
  assert.equal(new URL(page.XMLHttpRequest.lastUrl).searchParams.get('reply_marker'), 'kept');
  assert(armPage({video:'200', root:''}));
  const previous = new page.XMLHttpRequest(); previous.open('GET', base + '?aweme_id=100'); previous.send();
  assert(previous.aborted); assert.equal(page.__dycSent, false);
  const fresh = documentContext();
  vm.runInNewContext(guard, fresh);
  const freshWrong = new fresh.XMLHttpRequest(); freshWrong.open('GET', base + '?aweme_id=100'); freshWrong.send();
  assert(freshWrong.aborted); assert.equal(fresh.__dycSent, false);
  const freshFirst = new fresh.XMLHttpRequest(); freshFirst.open('GET', base + '?aweme_id=200'); freshFirst.send();
  assert.equal(fresh.__dycSent, true); assert.equal(JSON.parse(fresh.__dycResult).status, 200);
  assert(fresh.__dycReplay({video:'200', root:'', cursor:'1'}) === false);
  fresh.__dycArm = true;
  assert(fresh.__dycReplay({video:'200', root:'', cursor:'1'}));
  await assert.rejects(context.fetch(base));
  await assert.rejects(context.fetch(new URL(base, context.location.href)));
  assert.equal(calls, 0);
  context.__dycArm = true;
  await context.fetch(new URL(base + '?aweme_id=1&cursor=0&common=kept&msToken=old&a_bogus=stale&X-Bogus=stale&_signature=stale', context.location.href));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(JSON.parse(context.__dycResult).status, 200);
  assert.equal(context.__dycSent, true);
  assert.equal(context.__dycReplay({video:'1', root:'', cursor:'20'}), false);
  context.__dycArm = true;
  assert(context.__dycReplay({video:'1', root:'', cursor:'20'}));
  let replay = new URL(urls.at(-1));
  assert.equal(replay.searchParams.get('common'), 'kept');
  assert.equal(replay.searchParams.get('cursor'), '20');
  assert.equal(replay.searchParams.get('msToken'), 'fresh');
  assert.equal(replay.searchParams.has('a_bogus'), false);
  assert.equal(replay.searchParams.has('X-Bogus'), false);
  assert.equal(replay.searchParams.has('_signature'), false);
  context.localStorage.getItem = () => null;
  context.__dycArm = true;
  assert(context.__dycReplay({video:'1', root:'', cursor:'30'}));
  replay = new URL(urls.at(-1));
  assert.equal(replay.searchParams.get('common'), 'kept');
  assert.equal(replay.searchParams.get('cursor'), '30');
  assert.equal(replay.searchParams.get('msToken'), 'old');
  context.__dycArm = true; context.__dycRoot = '123';
  assert(context.__dycReplay({video:'1', root:'123', cursor:'0'}));
  replay = new URL(urls.at(-1));
  assert.equal(replay.pathname, base + 'reply/');
  assert.equal(replay.searchParams.get('comment_id'), '123');
  assert.equal(replay.searchParams.get('common'), 'kept');
  const blocked = new XHR(); blocked.open('GET', base + 'reply/'); blocked.send();
  assert.equal(blocked.aborted, true);
  context.__dycArm = true; context.__dycRoot = '123';
  const reply = new XHR(); reply.open('GET', base + 'reply/?item_id=1&comment_id=123&reply_common=kept'); reply.send();
  assert.equal(JSON.parse(context.__dycResult).text, '{"status_code":0}');
  assert.equal(calls, 5);
  context.__dycArm = true; context.__dycRoot = '456';
  assert(context.__dycReplay({video:'2', root:'456', cursor:'8'}));
  replay = new URL(urls.at(-1));
  assert.equal(replay.searchParams.get('item_id'), '2');
  assert.equal(replay.searchParams.has('aweme_id'), false);
  assert.equal(replay.searchParams.get('reply_common'), 'kept');
  context.__dycArm = true; context.__dycRoot = null; context.__dycSent = false;
  await context.fetch(base, {signal: {aborted: true}});
  assert.equal(context.__dycSent, false);
})().catch(error => { console.error(error); process.exitCode = 1; });

const scroll = execFileSync('python3', ['-c', 'from dyc.hub_comments import SCROLL_JS; print(SCROLL_JS)'], {env:{...process.env, PYTHONPATH:'tools/douyin_comments'}, encoding:'utf8'});
function scrollCase({rect = {left: 10, right: 300}, ancestor = false, root = '', risk = false, duplicate = false} = {}) {
  let clicks = 0, expanded = 0, ordinary = 0, windowScroll = 0;
  const element = (left = 10, right = 300) => ({getBoundingClientRect:()=>({left, right, top:10, bottom:100, width:100, height:90}), style:{visibility:'visible', display:'block', overflowY:'visible'}});
  const button = {...element(), innerText:'1', style:{...element().style, cursor:'pointer'}, querySelector:()=>({}), matches:()=>false, click:()=>clicks++};
  const other = {...element(), innerText:'3', querySelector:()=>({})};
  const share = {...element(), innerText:'分享', contains:e=>e === share};
  const row = {...element(), children:[other, button, other, share], parentElement:null};
  share.parentElement = row;
  const expand = {...element(), innerText:'展开 3 条回复', click:()=>expanded++};
  const reply = {...element(), innerText:'回复', click:()=>ordinary++};
  const item = {...element(), innerText:'小明 测试评论 展开 3 条回复', querySelectorAll:()=>[reply, expand]};
  const parent = {...element(), scrollHeight:900, clientHeight:100, style:{visibility:'visible', display:'block', overflowY:'auto'}};
  const box = {...element(rect.left, rect.right), getBoundingClientRect:()=>({left:rect.left, right:rect.right, top:rect.top ?? 10, bottom:rect.bottom ?? 100, width:100, height:90}), scrollHeight:500, clientHeight:ancestor ? 500 : 100,
    style:{visibility:'visible', display:'block', overflowY:ancestor ? 'visible' : 'auto'}, parentElement:ancestor ? parent : null,
    querySelectorAll:()=>duplicate ? [item, item] : [item]};
  const doc = {readyState:'complete', body:{innerText:''}, documentElement:{scrollHeight:1000}, querySelectorAll:q=>
    q.includes('captcha') ? (risk ? [element()] : []) : q === '[data-e2e="comment-list"]' ? [box] : q === '[data-e2e="video-share-icon-container"]' ? [share] : []};
  const ctx = {document:doc, innerWidth:1000, innerHeight:800, getComputedStyle:e=>e.style, scrollTo:()=>windowScroll++};
  ctx.window = ctx;
  const run = vm.runInNewContext(scroll, ctx);
  const action = run({root, text:'测试评论', nickname:'小明'});
  return {ctx, box, parent, clicks, expanded, ordinary, windowScroll, action};
}
for (const rect of [{left:1000, right:1100}, {left:1100, right:1200}, {left:10, right:300, top:2268, bottom:2358}]) {
  const result = scrollCase({rect});
  assert.equal(result.clicks, 1); assert.equal(result.ctx.__dycArm, true);
}
for (const ancestor of [false, true]) {
  const result = scrollCase({ancestor});
  assert.equal(result.clicks, 0); assert.equal(result.windowScroll, 0);
  assert.equal((ancestor ? result.parent : result.box).scrollTop, ancestor ? 900 : 500);
}
const closedThread = scrollCase({root:'123', rect:{left:1000, right:1100}});
assert.equal(closedThread.action, 'opened'); assert.equal(closedThread.clicks, 1);
const thread = scrollCase({root:'123'});
assert.equal(thread.expanded, 1); assert.equal(thread.ordinary, 0);
assert.equal(scrollCase({root:'123', duplicate:true}).expanded, 0);
const challenged = scrollCase({risk:true, rect:{left:1000, right:1100}});
assert.equal(challenged.clicks, 0); assert.equal(challenged.ctx.__dycArm, undefined);

// The visible action row has four icon controls; only its second item is clicked.
{
  const clicked = [];
  const style = {visibility:'visible', display:'block', cursor:'pointer'};
  const make = (label, text) => ({innerText:text, style, getBoundingClientRect:()=>({left:10,right:50,top:100,bottom:130,width:40,height:30}),
    querySelector:()=>({}), matches:()=>false, click:()=>clicked.push(label)});
  const items = [make('like','3'), make('comment','1'), make('favorite','1'), make('share','分享')];
  const row = {...make('row',''), getBoundingClientRect:()=>({left:67,right:500,top:686,bottom:732,width:433,height:46}), children:items, parentElement:null};
  const share = items[3]; share.parentElement = row; share.contains = e=>e === share;
  const hidden = {...make('hidden','1'), getBoundingClientRect:()=>({left:0,right:0,top:0,bottom:0,width:0,height:0})};
  const player = {...make('player',''), getBoundingClientRect:()=>({left:72,right:960,top:72,bottom:632,width:888,height:560})};
  const ctx = {innerWidth:1000, innerHeight:800, getComputedStyle:e=>e.style,
    document:{readyState:'complete', body:{innerText:'继续播放 使用客户端免弹窗'}, querySelectorAll:q=>q === '[data-e2e="video-share-icon-container"]' ? [share] : q === '[data-e2e="player-container"]' ? [player] : q === 'video' || q.includes('comment') || q.includes('captcha') ? [hidden] : []}};
  ctx.window = ctx;
  assert.equal(vm.runInNewContext(scroll, ctx)({root:'', opened:false}), true);
  assert.equal(ctx.__dycArm, true);
  assert.deepEqual(clicked, ['comment']);
  items[1].style = {...style, cursor:'default'};
  assert.equal(vm.runInNewContext(scroll, ctx)({root:'', opened:false}), false);
  items[1].style = {...style, pointerEvents:'none'};
  assert.equal(vm.runInNewContext(scroll, ctx)({root:'', opened:false}), false);
  assert.deepEqual(clicked, ['comment']);
}
