// Synthetic orchestration checks; no network and no credentials.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('hub.html','utf8');
const code=html.slice(html.indexOf('let reportBatch=null'),html.indexOf("$('#retryBundle').onclick"));
const nodes={};const $=id=>nodes[id]??={value:'',hidden:false,textContent:'',innerHTML:'',disabled:false};
let calls=[],fail=true;const ctx=vm.createContext({$,chosenPlatform:'douyin',sessionStorage:{getItem(){return null},setItem(){}},esc:s=>String(s),readingInfo:()=>false,infoTip:(status,detail)=>status+' '+detail,statusName:s=>s,toast(){},api:async(path,options)=>{calls.push({path,key:options?.headers?.['Idempotency-Key'],body:options?.body});if(path==='insights/queries'&&fail){fail=false;throw Error('simulated disconnect')}return {job:{id:'job-'+calls.length,status:'queued',cache_hit:false}}}});
vm.runInContext('let relatedLatestDate=null;'+html.slice(html.indexOf('function insightDates('),html.indexOf('function renderInsight(')),ctx);
vm.runInContext(code,ctx);
const query={platform:'douyin',keywords:['酵素','消化酶'],start_date:'2026-10-21',end_date:'2026-10-27'};
ctx.query=query;
(async()=>{
 vm.runInContext("reportBatch=makeReportBatch(query,'test-group')",ctx);
 let batch=vm.runInContext('reportBatch',ctx);assert.equal(batch.entries.length,5);assert.equal(batch.entries[0].body.keywords.length,2);assert(batch.entries.slice(1).every(e=>e.body.keywords.length===1));assert.equal(new Set(batch.entries.map(e=>e.idem)).size,5);
 await vm.runInContext('submitReportBatch()',ctx);assert.equal(calls.length,2);assert(batch.entries[0].jobId);assert.equal(batch.entries[1].jobId,null);assert(!vm.runInContext('bundleSubmitting',ctx));
 await vm.runInContext('submitReportBatch()',ctx);assert.equal(calls.length,6);assert.equal(calls[1].key,calls[2].key);assert.equal(calls.filter(c=>c.path==='queries').length,1);assert(batch.entries.every(e=>e.jobId));
 const five=vm.runInContext("makeReportBatch({...query,keywords:['a','b','c','d','e']},'five')",ctx);assert.equal(five.entries.length,11);
 const before=calls.filter(c=>c.body).length;await vm.runInContext('refreshReportBatch()',ctx);assert.equal(calls.filter(c=>c.body).length,before);
 $('#keywordSelect').value='酵素';$('#insightStart').value='2026-10-21';$('#insightEnd').value='2026-10-27';assert(vm.runInContext("moduleState('related')",ctx).includes('queued'));$('#insightStart').value='2026-10-20';assert(vm.runInContext("moduleState('related')",ctx).includes('未采集'));
 const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);assert.equal(ids.length,new Set(ids).size);for(const id of ['reportProgress','retryBundle','relatedData','audienceData'])assert(ids.includes(id));assert(!html.includes("$('#insightKind')"));assert(!html.includes("$('#collectInsight')"));
 vm.runInContext("relatedLatestDate='2026-10-23';reportBatch=makeReportBatch(query,'dated')",ctx);
 batch=vm.runInContext('reportBatch',ctx);const related=batch.entries.find(e=>e.body.kind==='related');
 related.jobId='dated-job';related.status='queued';$('#insightStart').value=query.start_date;
 vm.runInContext("relatedLatestDate='2026-10-25'",ctx);
 assert(vm.runInContext("moduleState('related')",ctx).includes('queued'));
 related.error='synthetic failure';assert.equal(vm.runInContext("moduleState('related')",ctx),'synthetic failure');
 $('#insightEnd').value='2026-10-23';assert.equal(vm.runInContext("moduleState('related')",ctx),'synthetic failure');
 $('#insightEnd').value='2026-10-24';assert(vm.runInContext("moduleState('related')",ctx).includes('未采集'));
 $('#insightEnd').value=query.end_date;vm.runInContext("chosenPlatform='baidu'",ctx);
 assert(vm.runInContext("moduleState('related')",ctx).includes('未采集'));
 console.log('report grouping, independent endpoints, partial submission recovery, idempotency, read-only refresh and shared-context checks passed');
})().catch(e=>{console.error(e);process.exitCode=1});

const qrNodes={};const qr$=id=>qrNodes[id]??={hidden:false,textContent:'',innerHTML:'',removeAttribute(){},getAttribute(){return null}};
const qrCtx=vm.createContext({$:qr$,esc:String,clearTimeout(){},setTimeout(){},loadQr(){}});
vm.runInContext('let qrTimer=null;'+html.slice(html.indexOf('function renderQr('),html.indexOf('async function openDesktop(')),qrCtx);
for(const browser of ['browser_ready','login_required']){
 qrCtx.data={state:'verification_required',browser};vm.runInContext('renderQr(data)',qrCtx);
 assert.equal(qr$('#qrState').innerHTML.includes('已检测到登录凭据（有效性以最近取数为准）'),browser==='browser_ready');
 assert.equal(qr$('#qrDesktop').hidden,browser==='browser_ready');
 if(browser==='browser_ready'){assert(qr$('#qrRefresh').hidden);assert(!qr$('#qrStart').hidden);assert.equal(qr$('#qrStart').textContent,'重新扫码')}
 else assert(qr$('#qrState').innerHTML.includes('需要安全验证'));
}
console.log('QR browser-ready copy checks passed');

vm.runInContext(html.slice(html.indexOf('function browserLoginLabel('),html.indexOf('function renderQr(')),qrCtx);
for(const [login_state,reason] of Object.entries({no_session_cookie:'未检测到登录凭据',expired:'登录凭据已过期',rejected:'当前凭据已被最近取数拒绝',unknown:'暂时无法读取登录凭据'})){
 qrCtx.data={browser:'login_required',login_state};const label=vm.runInContext('browserLoginLabel(data)',qrCtx);
 assert(label.includes('需要重新登录'));assert(label.includes(reason));assert(!label.includes('已登录'));
}
qrCtx.data={browser:'browser_ready',login_state:'cookie_present'};
assert.equal(vm.runInContext('browserLoginLabel(data)',qrCtx),'页面就绪 · 已检测到登录凭据（有效性以最近取数为准）');
qrCtx.data={browser:'browser_ready',login_state:'qr_session'};assert(vm.runInContext('browserLoginLabel(data)',qrCtx).includes('扫码会话在内存中'));
assert(html.includes('const label=browserLoginLabel(state)'));assert(html.includes("!ready?label+'。已保存的历史数据仍可读取。'"));
console.log('login_state badge, notice and credential-presence copy checks passed');
