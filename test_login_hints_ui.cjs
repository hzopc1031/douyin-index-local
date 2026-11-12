// Synthetic login display states and QR timers; no browser, network or credentials.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('hub.html','utf8');
function fixture(statuses=[]){
 const nodes={},timers=[],requests=[],revoked=[],clicks=[],notices=[],refreshes=[];
 const $=id=>nodes[id]??={hidden:false,value:'',textContent:'',innerHTML:'',querySelector(selector){return $(id+' '+selector)},removeAttribute(name){delete this[name]}};
 const ctx=vm.createContext({$,chosenPlatform:'baidu',chosenSource:'index',platformInfo:()=>({available:true}),esc:String,
  document:{querySelectorAll:()=>[],addEventListener(event,handler){if(event==='click')clicks.push(handler)}},
  sessionStorage:{getItem:()=>null,setItem(){}},readingInfo:()=>false,infoTip:String,statusName:s=>s==='failed'?'失败':s,
  toast:message=>notices.push(message),refresh:async()=>{refreshes.push(1)},
  async api(path,options){requests.push({path,options});if(ctx.submissionError)throw Object.assign(Error('合成暂停提示'),{status:ctx.submissionStatus??409,code:ctx.submissionError});return {ok:true}},
  URL:{createObjectURL:()=> 'blob:synthetic',revokeObjectURL:url=>revoked.push(url)},
  setTimeout(fn,delay){timers.push({fn,delay});return timers.length},clearTimeout(){},
  async fetch(path,options){requests.push({path,options});const status=statuses.shift()??200;return {status,ok:status===200,blob:async()=>({synthetic:true})}}
 });
 vm.runInContext(html.slice(html.indexOf('let baiduLoginState='),html.indexOf('let baiduQrActive=')),ctx);
 for(const id of ['resumeButton','baiduResumeButton'])vm.runInContext(html.split('\n').find(line=>line.startsWith("$('#"+id+"').onclick=")),ctx);
 vm.runInContext(html.slice(html.indexOf('let reportBatch=null'),html.indexOf("$('#retryBundle').onclick")),ctx);
 vm.runInContext(html.slice(html.indexOf('let baiduQrActive='),html.indexOf('async function pollBaiduLogin(')),ctx);
 return {$,ctx,timers,requests,revoked,clicks,notices,refreshes,run:code=>vm.runInContext(code,ctx)};
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
 assert(f.$('#'+platform+'QueryNote button').hidden);
 for(const source of ['hot_search','topic']){
  f.ctx.chosenSource=source;f.run(`renderLoginHints('${platform}',false,true)`);
  assert(f.$('#indexLoginStatus').hidden);
 }
 f.$('#queryPlatform').value='other';f.run(`renderLoginHints('${platform}',false,true)`);
 assert(f.$('#'+platform+'QueryNote').hidden);
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 for(const platform of ['baidu','douyin']){
  const f=fixture(),name=platform==='baidu'?'百度':'抖音';f.ctx.chosenPlatform=platform;f.$('#queryPlatform').value=platform;
  if(platform==='baidu')f.run("renderBaiduLogin({browser:'browser_ready',paused:{code:'login_required'}})");
  else f.run("renderLoginHints('douyin',true,false,{code:'manual_resume_required'})");
  assert(!f.$('#'+platform+'QueryNote').hidden);
  assert.equal(f.$('#'+platform+'QueryNote span').textContent,name+'已登录，但采集仍暂停：请先恢复'+name+'采集，否则新的在线查询会被拒绝（已缓存的完整结果仍可返回）。');
  assert(f.$('#'+platform+'QueryNote button').outerHTML.includes(`data-resume-platform="${platform}"`));
  assert(f.$('#'+platform+'QueryNote button').outerHTML.includes('恢复'+name+'采集'));
  assert(!f.$('#submitButton').disabled);
  assert(f.$('#indexLoginStatus').innerHTML.includes('实时查询需登录 · '+name+'已登录 · 采集已暂停'));
  assert(f.$('#indexLoginStatus').innerHTML.includes(`data-resume-platform="${platform}"`));
  assert.equal(f.requests.length,0);
  await f.clicks[0]({target:{closest:selector=>selector==='[data-resume-platform]'?{dataset:{resumePlatform:platform}}:null}});
  await tick();
  assert.equal(f.requests.length,1);assert.equal(f.requests[0].path,'collection/resume');
  assert.equal(f.requests[0].options.method,'POST');
  assert.deepEqual(JSON.parse(JSON.stringify(f.requests[0].options.body)),platform==='baidu'?{platform:'baidu'}:{});
  assert(f.notices[0].includes('已恢复'));assert.equal(f.refreshes.length,1);
 }
 for(const loggedIn of [false,true]){
  const f=fixture();f.ctx.submissionError='collection_paused';
  f.ctx.platformModules=()=>[];f.ctx.insightDates=()=>{};
  f.run(`renderBaiduLogin({browser:'${loggedIn?'browser_ready':'login_required'}',paused:{code:'login_required'}})`);
  f.run("reportBatch=makeReportBatch({platform:'baidu',keywords:['合成词'],start_date:'2026-10-21',end_date:'2026-10-27'},'paused')");
  await f.run('submitReportBatch()');
  assert.equal(f.run('reportBatch.entries[0].code'),'collection_paused');
  assert(f.run('reportBatch.entries[0].rejected'));assert.equal(f.run('reportBatch.entries[0].status'),'failed');
  assert(f.$('#reportProgressRows').innerHTML.includes('未创建'));
  assert(!f.$('#reportProgressRows').innerHTML.includes('>失败<'));
  assert(f.$('#reportProgressRows').innerHTML.includes(loggedIn?'恢复百度采集':'去平台连接登录'));
  if(loggedIn)assert(f.$('#reportProgressRows').innerHTML.includes('恢复后请重新提交查询'));
  assert(f.$('#reportProgressTitle').textContent.includes('1 / 1 项已结束'));
  assert(f.$('#retryBundle').hidden);
  assert.equal(f.notices.at(-1),'百度采集已暂停，任务未创建：请先登录或恢复采集后重新提交。');
  await f.run('refreshReportBatch();submitReportBatch()');
  assert.equal(f.requests.length,1);assert.equal(f.requests[0].path,'queries');assert.equal(f.requests[0].options.method,'POST');
 }
 for(const [status,code] of [[400,'invalid_query'],[429,'queue_full']]){
  const f=fixture();f.ctx.submissionError=code;f.ctx.submissionStatus=status;f.ctx.platformModules=()=>[];
  f.run("reportBatch=makeReportBatch({platform:'baidu',keywords:['合成词']},'rejected')");
  await f.run('submitReportBatch()');
  assert.equal(f.run('reportBatch.entries[0].code'),code);
  assert(f.$('#reportProgressRows').innerHTML.includes('未创建'));
  assert(!f.$('#reportProgressRows').innerHTML.includes('data-resume-platform'));
  assert.equal(f.notices.at(-1),'百度任务未创建，请处理提示后重新提交。');
  await f.run('submitReportBatch()');assert.equal(f.requests.length,1);
 }
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
 console.log('PASS login hints: both platforms, unknown/ready/paused states, explicit resume, rejected submissions without retry, cached submission, public views; QR retries, limits, non-503 errors, cancellation and stale refresh');
})().catch(e=>{console.error(e);process.exitCode=1});
