// Synthetic login display states and QR timers; no browser, network or credentials.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('hub.html','utf8');
function fixture(statuses=[]){
 const nodes={},timers=[],requests=[],revoked=[];
 const $=id=>nodes[id]??={hidden:false,value:'',textContent:'',innerHTML:'',querySelector(){return $(id+' span')},removeAttribute(name){delete this[name]}};
 const ctx=vm.createContext({$,chosenPlatform:'baidu',chosenSource:'index',platformInfo:()=>({available:true}),esc:String,
  URL:{createObjectURL:()=> 'blob:synthetic',revokeObjectURL:url=>revoked.push(url)},
  setTimeout(fn,delay){timers.push({fn,delay});return timers.length},clearTimeout(){},
  async fetch(path,options){requests.push({path,options});const status=statuses.shift()??200;return {status,ok:status===200,blob:async()=>({synthetic:true})}}
 });
 vm.runInContext(html.slice(html.indexOf('function renderLoginHints('),html.indexOf('function renderBaiduLogin(')),ctx);
 vm.runInContext(html.slice(html.indexOf('let baiduQrActive='),html.indexOf('async function pollBaiduLogin(')),ctx);
 return {$,ctx,timers,requests,revoked,run:code=>vm.runInContext(code,ctx)};
}
for(const platform of ['baidu','douyin']){
 const f=fixture(),name=platform==='baidu'?'百度':'抖音';f.ctx.chosenPlatform=platform;f.$('#queryPlatform').value=platform;
 f.run(`renderLoginHints('${platform}',false,true)`);
 assert(!f.$('#'+platform+'QueryNote').hidden);
 assert.equal(f.$('#'+platform+'QueryNote span').textContent,name+'未登录：只能返回已缓存的完整结果，新的在线查询需先登录。');
 assert(f.$('#indexLoginStatus').innerHTML.includes(`data-login-platform="${platform}"`));
 assert(!f.$('#submitButton').disabled);
 f.run(`renderLoginHints('${platform}',true,false)`);
 assert(f.$('#'+platform+'QueryNote').hidden);
 assert.equal(f.$('#indexLoginStatus').innerHTML,'实时查询需登录 · '+name+'已登录');
 assert(!f.$('#indexLoginStatus').innerHTML.includes('data-login-platform'));
 f.run(`renderLoginHints('${platform}',false,false)`);
 assert(!f.$('#'+platform+'QueryNote').hidden);
 assert(f.$('#'+platform+'QueryNote span').textContent.includes('登录状态待确认'));
 assert(!f.$('#indexLoginStatus').innerHTML.includes('data-login-platform'));
 for(const source of ['hot_search','topic']){
  f.ctx.chosenSource=source;f.run(`renderLoginHints('${platform}',false,true)`);
  assert(f.$('#indexLoginStatus').hidden);
 }
 f.$('#queryPlatform').value='other';f.run(`renderLoginHints('${platform}',false,true)`);
 assert(f.$('#'+platform+'QueryNote').hidden);
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 for(const statuses of [[503,503,200],[503,503,503],[403],[500]]){
  const expected=statuses.length,success=statuses.at(-1)===200,f=fixture([...statuses]);
  f.run('baiduQrActive=true');const pending=f.run('loadBaiduQr()');await tick();
  for(let attempt=1;attempt<expected;attempt++){
   assert.equal(f.requests.length,attempt);
   assert.equal(f.$('#baiduQrState').textContent,`正在获取二维码…（第 ${attempt}/3 次）`);
   assert(f.$('#baiduDesktop').hidden);assert(f.$('#baiduQrImage').hidden);
   const timer=f.timers.shift();assert.equal(timer.delay,3000);timer.fn();await tick();
  }
  await pending;assert.equal(f.requests.length,expected);assert.equal(f.timers.length,0);
  assert(f.requests.every(r=>r.path==='/api/v1/login/baidu/qr'&&r.options.cache==='no-store'&&r.options.headers['X-Hub-Request']==='1'));
  assert.equal(f.$('#baiduQrImage').hidden,!success);assert.equal(f.$('#baiduDesktop').hidden,success);
  assert(f.$('#baiduQrState').textContent.includes(success?'请使用百度':'二维码尚未就绪'));
 }
 for(const cancel of ['baiduQrActive=false;clearBaiduQr()', 'clearBaiduQr()', 'baiduQrRevision++']){
  const f=fixture([503,200]);f.run('baiduQrActive=true');const pending=f.run('loadBaiduQr()');await tick();
  f.run(cancel);f.timers.shift().fn();await pending;
  assert.equal(f.requests.length,1);assert(f.$('#baiduQrImage').hidden);assert(f.$('#baiduDesktop').hidden);
 }
 const f=fixture([503,200]);f.run('baiduQrActive=true');const older=f.run('loadBaiduQr()');await tick();
 await f.run('loadBaiduQr()');assert.equal(f.$('#baiduQrImage').src,'blob:synthetic');
 f.timers.shift().fn();await older;assert.equal(f.requests.length,2);assert(!f.$('#baiduQrImage').hidden);
 console.log('PASS login hints: both platforms, unknown/ready states, cached submission, public views; QR retries, limits, non-503 errors, cancellation and stale refresh');
})().catch(e=>{console.error(e);process.exitCode=1});
