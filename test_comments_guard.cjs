const assert = require('assert');
const vm = require('vm');
const source = require('fs').readFileSync('tools/douyin_comments/dyc/hub_comments.py', 'utf8');
const script = name => source.match(new RegExp('^' + name + ' = r?"""([\\s\\S]*?)"""', 'm'))[1];
const guard = script('GUARD_JS'), arm = script('ARM_JS'), scroll = script('SCROLL_JS');
const base = '/aweme/v1/web/comment/list/';
const flush = () => new Promise(resolve => setImmediate(resolve));
function page({storage = new Map(), top, href = 'https://www.douyin.com/video/100', immediate = true} = {}) {
  const calls = [], navigations = [];
  class XHR {
    open(method, url) { this.method = method; this.url = url; }
    addEventListener(event, callback) { this.callback = callback; }
    send(body) { calls.push({method:this.method, url:this.url, body}); if (immediate) this.complete(); }
    complete(text = '{"status_code":0}', status = 200, responseURL = '') {
      this.status = status; this.responseText = text; this.responseURL = responseURL;
      if (this.callback) this.callback();
    }
    abort() { this.aborted = true; }
  }
  const ctx = {URL, URLSearchParams, Date, Math, document:{}, location:new URL(href), XMLHttpRequest:XHR,
    sessionStorage:{getItem:k=>storage.get(k) ?? null, setItem:(k,v)=>storage.set(k,v), removeItem:k=>storage.delete(k)},
    fetch:(input, init)=>{
      if (init?.signal?.aborted || input?.signal?.aborted) return Promise.reject(new Error('aborted'));
      calls.push({url:typeof input === 'string' ? input : input.url || input.href});
      return Promise.resolve({status:200, clone:()=>({text:()=>Promise.resolve('{"status_code":0}')})});
    }};
  ctx.location.assign = url => navigations.push(new URL(url, ctx.location.href));
  ctx.window = ctx; ctx.top = top || ctx;
  vm.runInNewContext(guard, ctx);
  return {ctx, calls, storage, navigations, begin:(root = '', cursor = '0', video = '100')=>
    vm.runInNewContext(arm, ctx)({video, root, cursor})};
}
function request(ctx, url = base + '?aweme_id=100&cursor=0', method = 'GET', body) {
  const xhr = new ctx.XMLHttpRequest(); xhr.open(method, url); xhr.send(body); return xhr;
}
function sdk(ctx) {
  const open = ctx.XMLHttpRequest.prototype.open, send = ctx.XMLHttpRequest.prototype.send;
  ctx.sdkCalls = 0;
  ctx.XMLHttpRequest.prototype.open = function() { ctx.sdkCalls++; return open.apply(this, arguments); };
  ctx.XMLHttpRequest.prototype.send = function() { ctx.sdkCalls++; return send.apply(this, arguments); };
  const fetch = ctx.fetch;
  ctx.fetch = function() { ctx.sdkCalls++; return fetch.apply(this, arguments); };
}
async function captureTests() {
  const idle = page();
  assert(!request(idle.ctx).aborted);
  await idle.ctx.fetch(base + '?aweme_id=100');
  assert.equal(idle.calls.length, 2);
  assert.equal(idle.ctx.__dycRead().sent, false);

  for (const path of [base, '/aweme/v2/web/comment/list/variant', '/custom/comment/list/']) {
    const p = page(); p.begin(); sdk(p.ctx);
    p.ctx.localStorage = {getItem:k=>k==='xmst'?'fresh':null};
    const query = '?item_id=100&cursor=0&common=kept&msToken=synthetic&a_bogus=synthetic&X-Bogus=synthetic&_signature=synthetic';
    assert(!request(p.ctx, path + query).aborted);
    assert.equal(JSON.parse(p.ctx.__dycRead().result).status, 200);
    assert(p.begin('', '20'));
    assert(p.ctx.__dycReplay({video:'100', root:'', cursor:'20'}));
    const replay = new URL(p.calls.at(-1).url);
    assert.equal(replay.pathname, path);
    assert.deepEqual([...replay.searchParams.entries()], [['item_id','100'],['cursor','20'],['common','kept'],['msToken','fresh']]);
    assert.equal(p.ctx.sdkCalls, 4);
    p.begin('123');
    request(p.ctx, '/aweme/v2/web/comment/list/reply/?item_id=100&comment_id=123&cursor=0');
    p.begin('456', '8');
    assert(p.ctx.__dycReplay({video:'100', root:'456', cursor:'8'}));
    const reply = new URL(p.calls.at(-1).url);
    assert.equal(reply.searchParams.get('comment_id'), '456');
    assert.equal(reply.searchParams.get('cursor'), '8');
  }
  const unrelated = page(); unrelated.begin(); sdk(unrelated.ctx);
  request(unrelated.ctx, '/aweme/v1/web/other/list/?aweme_id=100');
  assert.equal(unrelated.ctx.__dycRead().sent, false);
  assert.equal(unrelated.ctx.__dycReplay({video:'100', root:'', cursor:'0'}), false);
  for (const query of ['aweme_id=101', 'aweme_id=100&aweme_id=101', 'aweme_id=100&item_id=100', 'aweme_id=100&comment_id=1']) {
    const p = page(); p.begin(); sdk(p.ctx);
    assert(!request(p.ctx, base + '?' + query).aborted);
    assert.equal(p.ctx.__dycRead().sent, false);
    assert.equal(p.ctx.__dycReplay({video:'100', root:'', cursor:'0'}), false);
  }
  const unsupported = page(); unsupported.begin();
  assert(!request(unsupported.ctx, base + '?aweme_id=100', 'POST', 'x').aborted);
  assert.equal(JSON.parse(unsupported.ctx.__dycRead().result).kind, 'unsupported_request');
  const late = page({immediate:false}); late.begin(); const stale = request(late.ctx);
  late.ctx.__dycClose(); late.begin('', '1'); const current = request(late.ctx, base + '?aweme_id=100&cursor=1');
  stale.complete('old'); assert.equal(late.ctx.__dycRead().result, null);
  current.complete('new'); assert.equal(JSON.parse(late.ctx.__dycRead().result).text, 'new');
  late.ctx.__dycClose(); current.complete('after close'); assert.equal(JSON.parse(late.ctx.__dycRead().result).text, 'new');
  const expired = page(); expired.begin();
  const ticket = JSON.parse(expired.storage.get('__dycPending')); ticket.expires = 0;
  expired.storage.set('__dycPending', JSON.stringify(ticket));
  assert(!request(expired.ctx).aborted); assert.equal(expired.ctx.__dycRead().sent, false);
  const foreign = page(); foreign.begin(); sdk(foreign.ctx);
  assert(!request(foreign.ctx, 'https://example.invalid' + base + '?aweme_id=100').aborted);
  assert.equal(foreign.ctx.__dycRead().sent, false);
  assert.equal(foreign.ctx.__dycReplay({video:'100', root:'', cursor:'0'}), false);
  const nativeOnly = page(); nativeOnly.begin(); request(nativeOnly.ctx);
  nativeOnly.begin('', '1');
  assert.equal(nativeOnly.ctx.__dycReplay({video:'100', root:'', cursor:'1'}), false);
  const first = page(); first.begin();
  const afterReload = page({storage:first.storage});
  assert(!request(afterReload.ctx).aborted);
  assert.equal(afterReload.ctx.__dycRead().sent, true);
  const modal = page({storage:first.storage, href:'https://www.douyin.com/jingxuan?modal_id=100'});
  assert(!request(modal.ctx).aborted);
  assert.equal(modal.ctx.__dycRead().result, null);
  console.log('comments guard capture regressions passed');
}
captureTests().catch(error => { console.error(error); process.exitCode = 1; });

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
  assert.equal(result.clicks, 1); assert.equal(result.action, 'opened');
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
  assert.equal(vm.runInNewContext(scroll, ctx)({root:'', opened:false}), 'opened');
  assert.deepEqual(clicked, ['comment']);
  items[1].style = {...style, cursor:'default'};
  assert.equal(vm.runInNewContext(scroll, ctx)({root:'', opened:false}), false);
  items[1].style = {...style, pointerEvents:'none'};
  assert.equal(vm.runInNewContext(scroll, ctx)({root:'', opened:false}), false);
  assert.deepEqual(clicked, ['comment']);
}
