// Node VM only: synthetic forms/pointers, no browser storage or network.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('hub.html','utf8');
const source=html.slice(html.indexOf('// SMS login:'),html.indexOf('// End SMS login.'));
const tests=[];function test(name,run){tests.push({name,run})}
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject}}
function fixture(){
 const nodes={},calls=[],timers=new Map(),revoked=[],notices=[];let serial=0,blobs=0;
 const $=id=>nodes[id]??=(function(){let src='';const node={hidden:false,disabled:false,value:'',textContent:'',style:{},captured:null,getBoundingClientRect:()=>({left:10,top:20,width:200,height:100}),setPointerCapture(id){this.captured=id},hasPointerCapture(id){return this.captured===id},releasePointerCapture(){this.captured=null},getAttribute(key){return key==='src'?src:null},removeAttribute(key){if(key==='src')src=''}};Object.defineProperty(node,'src',{get:()=>src,set:value=>{src=value;queueMicrotask(()=>node.onload?.())}});return node})();
 const state={state:'idle',phone:'',resend_in:0,attempts_left:0,frame_id:0,challenge_epoch:0,verification_mode:null,message:'尚未开始短信登录'};
 const f={$,calls,timers,revoked,notices,state,probes:[],error:null,frameError:null};
 const ctx=vm.createContext({$,Math,douyinNeedsLogin:false,commentsProbe:null,document:{querySelectorAll:()=>[]},commentsAction(action,body){f.probes.push({action,body})},renderBaiduLogin(){},Image:class{set src(value){if(f.preview)f.preview(this);else queueMicrotask(()=>this.onload())}},URL:{createObjectURL(){return 'blob:synthetic-'+(++blobs)},revokeObjectURL(url){revoked.push(url)}},
   setTimeout(fn,delay){const id=++serial;timers.set(id,{fn,delay});return id},clearTimeout(id){timers.delete(id)},
   localStorage:{setItem(){throw Error('storage write')}},sessionStorage:{setItem(){throw Error('storage write')}},
   toast(message){notices.push(message)},openDesktop(){f.desktop=true},
   async api(path,options){calls.push({path,body:options?.body});if(f.error&&options){const e=f.error;f.error=null;throw e}return f.respond?f.respond(path,options):{...state}},
   async fetch(path,options){calls.push({path,options});if(f.frameError)return {ok:false,json:async()=>({error:{code:'stale_frame',message:'画面已更新，请重新操作'}})};return {ok:true,blob:async()=>({synthetic:true})}}
 });vm.runInContext(source,ctx);
 f.run=code=>vm.runInContext(code,ctx);f.render=async changes=>{Object.assign(state,changes);ctx.data={...state};f.run('renderSms(data)');await new Promise(resolve=>setImmediate(resolve))};
 f.event=(x,y,t,id=1)=>({clientX:x,clientY:y,timeStamp:t,pointerId:id,button:0,isPrimary:true,preventDefault(){}});
 f.manual=async()=>{f.run('smsEnabled=true');await f.render({state:'verify_send',phone:'138****8000',verification_mode:'manual',frame_id:1,challenge_epoch:1})};
 return f;
}


test('manual QR follows frame and disappears with verification',async()=>{
 const f=fixture();await f.manual();await f.render({qr_available:true});
 assert(!f.$('#smsQrPanel').hidden);assert.equal(f.$('#smsQrImage').src,'/api/v1/login/sms/qr?v=1');
 assert.equal(f.$('#smsQrDownload').href,'/api/v1/login/sms/qr?v=1');
 await f.render({frame_id:2});assert.equal(f.$('#smsQrImage').src,'/api/v1/login/sms/qr?v=2');
 await f.render({qr_available:false});assert(f.$('#smsQrPanel').hidden);
 await f.render({qr_available:true,verification_mode:'desktop'});assert(f.$('#smsQrPanel').hidden);
});

test('all states and buttons, masked number only',async()=>{
 for(const state of ['idle','opening','sending','verify_send','code_sent','submitting','code_invalid','verify_login','succeeded','already_logged_in','rate_limited','failed','timeout','cancelled']){
  const f=fixture();f.run('smsEnabled=true');
  if(['succeeded','already_logged_in'].includes(state))await f.render({state:'opening'});
  const verify=state.startsWith('verify_'),active=['opening','sending','verify_send','code_sent','submitting','code_invalid','verify_login'].includes(state);
  await f.render({state,verification_mode:verify?'manual':null,resend_in:60,attempts_left:3,phone:'138****8000'});
  assert.equal(f.$('#smsPhoneForm').hidden,active||['succeeded','already_logged_in'].includes(state));assert.equal(f.$('#smsCodeForm').hidden,!['code_sent','code_invalid'].includes(state));assert.equal(f.$('#smsVerification').hidden,!verify);assert.equal(f.$('#smsCancel').hidden,!active);assert.equal(f.$('#smsNumber').textContent,'138****8000');
  assert.equal(f.run('smsTimer!==null'),active);
 }
 const f=fixture();await f.render({phone:'13800138000'});assert.equal(f.$('#smsNumber').textContent,'');
});

test('completed login refreshes the comments probe once and keeps its fallback when needed',async()=>{
 const f=fixture();f.run("setSmsLoginStatus({browser:'login_required',login_state:'no_session_cookie'})");
 await f.render({state:'opening'});await f.render({state:'succeeded'});
 assert.equal(f.probes.length,1);assert.equal(f.probes[0].action,'probe');assert.deepEqual(Object.keys(f.probes[0].body),[]);
 await f.render({state:'succeeded'});assert.equal(f.probes.length,1);
 f.$('#qrDesktop').hidden=true;
 for(const probe of ['not_logged_in','verification_required']){
  f.run(`commentsProbe='${probe}';renderDouyinLoginActions()`);assert(!f.$('#desktopLogin').hidden);
 }
 f.run("commentsProbe='logged_in';renderDouyinLoginActions()");assert(f.$('#desktopLogin').hidden);assert(f.$('#smsDesktop').hidden);
});
test('restored browser login hides idle SMS form without sending anything',async()=>{
 for(const login_state of ['cookie_present','qr_session']){
  const f=fixture();await f.render({state:'idle'});
  f.run(`setSmsLoginStatus({browser:'browser_ready',login_state:'${login_state}'})`);
  assert(f.$('#smsPhoneForm').hidden);assert(f.$('#smsCodeForm').hidden);
  assert(f.$('#smsState').textContent.startsWith('已登录'));assert(f.$('#smsDesktop').hidden);
  assert(f.$('#smsNumber').textContent.includes('未提供脱敏手机号'));
  await f.render({state:'idle'});assert(f.$('#smsPhoneForm').hidden);
  assert.equal(f.calls.length,0);
 }
 assert(html.includes('Promise.all([loadHubStatus(),'));
});
test('status arriving before SMS snapshot still hides stale code and verification UI',async()=>{
 for(const state of ['idle','code_sent','verify_login']){
  const f=fixture();f.run("setSmsLoginStatus({browser:'browser_ready',login_state:'cookie_present'})");
  await f.render({state,phone:'138****8000',verification_mode:'manual',frame_id:1,resend_in:60});
  for(const id of ['#smsPhoneForm','#smsCodeForm','#smsVerification','#smsCancel'])assert(f.$(id).hidden);
  assert.equal(f.$('#smsNumber').textContent,'138****8000');assert.equal(f.$('#smsCountdown').textContent,'');
  assert.equal(f.calls.length,0);
 }
});
test('new SMS success hides forms immediately; later invalid status wins over terminal snapshot',async()=>{
 for(const state of ['succeeded','already_logged_in']){
  for(const login_state of ['expired','rejected','no_session_cookie','unknown']){
   const f=fixture();f.run("setSmsLoginStatus({browser:'login_required',login_state:'no_session_cookie'})");
   await f.render({state:'opening'});
   await f.render({state,phone:'138****8000',message:'官方窗口已登录'});
   assert(f.$('#smsPhoneForm').hidden);assert(f.$('#smsCodeForm').hidden);
   assert(f.$('#smsState').textContent.startsWith('已登录'));
   f.run(`setSmsLoginStatus({browser:'login_required',login_state:'${login_state}'})`);
   await f.render({state});assert(!f.$('#smsPhoneForm').hidden);
   assert(!f.$('#smsState').textContent.includes('已登录'));assert.equal(f.calls.length,0);
  }
 }
});
test('historical terminal snapshots cannot override current expired status on reload',async()=>{
 for(const state of ['succeeded','already_logged_in']){
  const f=fixture();f.run("setSmsLoginStatus({browser:'login_required',login_state:'expired'})");
  await f.render({state,message:'官方窗口已登录'});
  assert(!f.$('#smsPhoneForm').hidden);assert(!f.$('#smsState').textContent.includes('已登录'));
  assert.equal(f.calls.length,0);
 }
});
test('immediate successful action response hides form even without an opening snapshot',async()=>{
 const f=fixture();f.run("smsEnabled=true;setSmsLoginStatus({browser:'login_required',login_state:'no_session_cookie'})");
 await f.render({state:'idle'});f.state.state='already_logged_in';
 await f.run("smsAction('start',{phone:'13800138000'})");
 assert(f.$('#smsPhoneForm').hidden);assert(f.$('#smsState').textContent.startsWith('已登录'));
 assert.equal(f.calls.filter(c=>c.body).length,1);
});
test('connection loss and logout restore form; only masked phone is displayed',async()=>{
 const f=fixture();await f.render({state:'idle'});
 for(const state of ["{browser:'unavailable',login_state:'unknown'}",'null',"{browser:'browser_ready',login_state:'rejected'}"]){
  f.run("setSmsLoginStatus({browser:'browser_ready',login_state:'cookie_present'})");
  f.run(`setSmsLoginStatus(${state})`);assert(!f.$('#smsPhoneForm').hidden);
 }
 f.run("setSmsLoginStatus({browser:'browser_ready',login_state:'cookie_present'})");
 for(const phone of ['13800138000','<img src=x onerror=alert(1)>','138****8000 extra']){
  await f.render({phone});assert.equal(f.$('#smsNumber').textContent,'账号信息：当前会话未提供脱敏手机号');
 }
 assert(source.includes('catch(e){setSmsLoginStatus(null,revision);renderBaiduLogin(null);throw e}'));
});
test('status response or failure started before SMS success cannot override it',async()=>{
 for(const response of ["{browser:'login_required',login_state:'no_session_cookie'}",'null']){
  const f=fixture();await f.render({state:'submitting'});
  f.run('const earlierRevision=smsLoginRevision');
  await f.render({state:'succeeded'});
  f.run(`setSmsLoginStatus(${response},earlierRevision)`);
  await f.render({state:'succeeded'});
  assert(f.$('#smsPhoneForm').hidden);assert(f.$('#smsCodeForm').hidden);
  assert(f.$('#smsState').textContent.startsWith('已登录'));
  f.run("setSmsLoginStatus({browser:'login_required',login_state:'expired'})");
  assert(!f.$('#smsPhoneForm').hidden);
 }
 assert(source.includes('const revision=smsLoginRevision;'));
});
test('repeated terminal action is fresh confirmation after a status failure',async()=>{
 const f=fixture();f.run('smsEnabled=true');await f.render({state:'opening'});
 await f.render({state:'already_logged_in'});f.run('setSmsLoginStatus(null)');
 assert(!f.$('#smsPhoneForm').hidden);
 await f.run("smsAction('start',{phone:'13800138000'})");
 assert(f.$('#smsPhoneForm').hidden);assert(f.$('#smsState').textContent.startsWith('已登录'));
 assert.equal(f.calls.filter(c=>c.body).length,1);
});
test('status renders login before SMS snapshot and survives a failed SMS read',async()=>{
 const f=fixture();f.respond=async path=>{if(path==='status')return {browser:'browser_ready',login_state:'cookie_present'};throw Error('synthetic SMS read failure')};
 await f.run('loadHubStatus()');assert(f.$('#smsPhoneForm').hidden);
 f.run('smsEnabled=true');await f.run('loadSms()');
 assert(f.$('#smsPhoneForm').hidden);assert(f.$('#smsCodeForm').hidden);
});
test('status request failure clears login, but an older pending failure does not',async()=>{
 const f=fixture();f.run("setSmsLoginStatus({browser:'browser_ready',login_state:'cookie_present'})");
 f.respond=async()=>{throw Error('synthetic status failure')};
 await assert.rejects(f.run('loadHubStatus()'));assert(!f.$('#smsPhoneForm').hidden);
 const pending=deferred();f.respond=()=>pending.promise;
 await f.render({state:'submitting'});const request=f.run('loadHubStatus()');
 await f.render({state:'succeeded'});pending.reject(Error('older status failure'));
 await assert.rejects(request);assert(f.$('#smsPhoneForm').hidden);
});
test('actual dashboard refresh preserves confirmed login when history, jobs or charts fail',async()=>{
 for(const failure of ['history','jobs','series']){
  const f=fixture();await f.render({state:'idle'});
  for(const id of ['#notice','#browserBadge'])f.$(id).classList={remove(){},toggle(){}};
  f.$('#sessionDetail').textContent='';
  f.respond=async path=>{
   if(path===failure)throw Error('synthetic '+failure+' failure');
   if(path==='status')return {browser:'browser_ready',login_state:'cookie_present',login_mode:'sms',counts:{keywords:0,points:0},daily_online_attempts:0,limits:{online_attempts_per_day:50}};
   return path==='history'?{datasets:[]}:path==='jobs'?{jobs:[]}:f.state;
  };
  f.run(`let refreshing=false,datasets=[],jobs=[],platforms=[],qrEnabled=false,qrInitialized=false,qrTimer=null,reportBatch=null;
   const document={title:'',querySelectorAll:()=>[]},esc=String;function readingInfo(){return false};function infoTip(status){return status};function fmt(){};function initializeRanges(){};function browserLoginLabel(){return 'ready'};function renderHistory(){};function renderJobs(){};function renderBaiduLogin(){};
   async function refreshReportBatch(){};async function loadInsights(){};async function loadSeries(){${failure==='series'?"throw Error('synthetic series failure')":''}}`);
  f.run(html.slice(html.indexOf('async function refresh(){'),html.indexOf("$('#refreshButton').onclick")));
  await f.run('refresh()');assert(f.$('#smsPhoneForm').hidden);assert(f.$('#smsCodeForm').hidden);
  assert(f.$('#smsState').textContent.startsWith('已登录'));assert(f.notices.length);
  assert(!f.calls.some(c=>c.body));
 }
});
test('auto/manual/remote-desktop display and countdown',async()=>{
 const f=fixture();await f.manual();assert(!f.$('#smsRefresh').hidden);
 await f.render({verification_mode:'auto'});assert.equal(f.$('#smsState').textContent,'自动验证中…');assert(f.$('#smsRefresh').hidden);assert(f.$('#smsTextPanel').hidden);assert(f.$('#smsImage').hidden);
 await f.render({verification_mode:'desktop'});assert(f.$('#smsVerifyHint').textContent.includes('远程桌面'));assert(!f.$('#smsDesktop').hidden);f.$('#smsDesktop').onclick();assert(f.desktop);
 await f.render({state:'code_sent',resend_in:60,attempts_left:3});assert(f.$('#smsResend').disabled);assert.equal(f.$('#smsResend').textContent,'60 秒后重发');
 await f.render({resend_in:0});assert(!f.$('#smsResend').disabled);
});
test('post-CAPTCHA code rejection restores code entry and resend countdown',async()=>{
 const f=fixture();f.run('smsEnabled=true');
 await f.render({state:'verify_login',verification_mode:'manual',attempts_left:2,resend_in:30});
 assert(f.$('#smsCodeForm').hidden);assert(!f.$('#smsVerification').hidden);
 await f.render({state:'code_invalid',verification_mode:null,frame_id:0});
 assert(!f.$('#smsCodeForm').hidden);assert(f.$('#smsVerification').hidden);
 assert(f.$('#smsResend').disabled);
 await f.render({resend_in:0});assert(!f.$('#smsResend').disabled);
 f.$('#smsCode').value='246810';
 await f.$('#smsCodeForm').onsubmit({preventDefault(){}});
 assert(f.calls.some(c=>c.path==='login/sms/code'&&c.body.code==='246810'));
 assert.equal(f.$('#smsCode').value,'');
});
test('submitted sensitive inputs clear immediately even on failure',async()=>{
 const f=fixture();f.run('smsEnabled=true');
 for(const [form,input,path,key,value] of [['smsPhoneForm','smsPhone','start','phone','13800138000'],['smsCodeForm','smsCode','code','code','246810'],['smsTextForm','smsText','text','text','A1']]){
  f.$('#'+input).value=value;f.error=Object.assign(Error('synthetic failure'),{code:'invalid_payload'});
  const pending=f.$('#'+form).onsubmit({preventDefault(){}});assert.equal(f.$('#'+input).value,'');await pending;
  const call=f.calls.find(c=>c.path==='login/sms/'+path);assert.equal(call.body[key],value);assert(!call.path.includes(value));assert.equal(f.$('#'+input).value,'');
 }
 assert(!/localStorage|sessionStorage|URLSearchParams/.test(source));
});
for(const order of ['before action response','after action response','error after action response'])test('pre-action status is discarded: '+order,async()=>{
 const f=fixture();f.run('smsEnabled=true');const old=deferred(),action=deferred(),fresh=deferred();let reads=0;
 const idle={...f.state},opening={...idle,state:'opening',message:'opening fixture'};
 f.respond=(path,options)=>options?action.promise:(++reads===1?old.promise:fresh.promise);
 const status=f.run('loadSms()');f.$('#smsPhone').value='13800138000';
 const start=f.$('#smsPhoneForm').onsubmit({preventDefault(){}});
 if(order==='before action response'){old.resolve(idle);await status;assert.equal(f.run('smsSnapshot'),null)}
 action.resolve(opening);await new Promise(resolve=>setImmediate(resolve));
 assert.equal(f.run('smsSnapshot.state'),'opening');assert(!f.$('#smsCancel').hidden);
 if(order==='error after action response')old.reject(Error('obsolete status failure'));
 else old.resolve(idle);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(f.run('smsSnapshot.state'),'opening');assert.equal(f.$('#smsState').textContent,'opening fixture');
 assert(f.$('#smsPhoneForm').hidden);assert(!f.$('#smsCancel').hidden);assert.equal(reads,2);
 fresh.resolve({...opening,state:'code_sent',attempts_left:3,resend_in:60});
 await Promise.all([status,start]);assert(!f.$('#smsCodeForm').hidden);assert(!f.$('#smsSubmit').disabled);
 assert(f.run('smsTimer!==null'));assert.equal(f.notices.length,0);
});
for(const outcome of ['late success','late error','completion before cancel response'])test('cancel interrupts pending pointer: '+outcome,async()=>{
 const f=fixture();await f.manual();const pointer=deferred(),cancel=deferred(),refresh=deferred(),previous={...f.state};
 f.respond=(path,options)=>path==='login/sms/pointer'?pointer.promise:path==='login/sms/cancel'?cancel.promise:refresh.promise;
 const image=f.$('#smsImage');image.onpointerdown(f.event(10,20,0));image.onpointermove(f.event(80,30,4000));
 const replay=image.onpointerup(f.event(110,40,8000));
 assert(!f.$('#smsCancel').disabled);assert(!f.$('#smsCancel').hidden);
 const cancellation=f.$('#smsCancel').onclick();
 assert.equal(f.calls.filter(c=>c.path==='login/sms/cancel').length,1);assert(f.$('#smsCancel').disabled);
 await f.$('#smsCancel').onclick();await f.run("smsAction('resend')");
 assert.equal(f.calls.filter(c=>c.path==='login/sms/cancel').length,1);
 assert(!f.calls.some(c=>c.path==='login/sms/resend'));
 if(outcome==='completion before cancel response'){
  pointer.resolve(previous);await replay;assert(f.$('#smsCancel').disabled);
  await f.run("smsAction('start',{phone:'13800138000'})");assert(!f.calls.some(c=>c.path==='login/sms/start'));
 }
 Object.assign(f.state,{state:'cancelled',verification_mode:null,frame_id:0,message:'cancelled fixture'});
 cancel.resolve({...f.state});await new Promise(resolve=>setImmediate(resolve));
 assert.equal(f.run('smsSnapshot.state'),'cancelled');assert(f.$('#smsCancel').hidden);assert(image.hidden);
 if(outcome==='late error')pointer.reject(Object.assign(Error('obsolete pointer failure'),{code:'stale_frame'}));
 else pointer.resolve(previous);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(f.run('smsSnapshot.state'),'cancelled');assert(f.$('#smsVerification').hidden);
 assert.equal(f.$('#smsState').textContent,'cancelled fixture');assert.equal(f.notices.length,0);
 refresh.resolve({...f.state});await Promise.all([replay,cancellation]);
 assert(!f.$('#smsStart').disabled);assert.equal(f.run('smsTimer'),null);
});
test('tap maps mobile CSS rectangle to normalized coordinates',async()=>{
 const f=fixture();await f.manual();f.$('#smsImage').onpointerdown(f.event(60,45,100));await f.$('#smsImage').onpointerup(f.event(60,45,180));
 const call=f.calls.find(c=>c.path==='login/sms/pointer');assert.equal(call.body.kind,'tap');assert.deepEqual(JSON.parse(JSON.stringify(call.body.points)),[[.25,.25,0]]);assert.equal(call.body.frame,1);
});
test('drag timing, sample spacing, endpoints and finger feedback',async()=>{
 const f=fixture();await f.manual();const image=f.$('#smsImage');image.onpointerdown(f.event(10,20,100));
 image.onpointermove(f.event(20,25,108));image.onpointermove(f.event(40,30,116));image.onpointermove(f.event(80,40,132));assert.equal(f.$('#smsFinger').style.left,'35%');
 await image.onpointerup(f.event(110,70,140));const points=f.calls.find(c=>c.path==='login/sms/pointer').body.points;
 assert.equal(points[0][2],0);assert.equal(points.at(-1)[2],40);assert.equal(points.at(-1)[0],.5);assert(points.every((p,i)=>!i||p[2]-points[i-1][2]>=16));assert(f.$('#smsFinger').hidden);
});
test('200-point cap, clamped coordinates, original final time',async()=>{
 const f=fixture();await f.manual();const image=f.$('#smsImage');image.onpointerdown(f.event(10,20,0));
 for(let i=1;i<500;i++)image.onpointermove(f.event(10+i,20+i,16*i));await image.onpointerup(f.event(900,900,8000));
 const points=f.calls.find(c=>c.path==='login/sms/pointer').body.points;assert.equal(points.length,200);assert.equal(points.at(-1)[2],8000);assert(points.every(p=>p[0]>=0&&p[0]<=1&&p[1]>=0&&p[1]<=1));
});
test('overlong, cancelled and extra-pointer gestures never submit',async()=>{
 const f=fixture();await f.manual();const image=f.$('#smsImage');image.onpointerdown(f.event(10,20,0));image.onpointermove(f.event(80,30,100,2));assert.equal(f.run('smsGesture.points.length'),1);await image.onpointerup(f.event(80,30,10001));
 image.onpointerdown(f.event(10,20,0));image.onpointercancel();await image.onpointerup(f.event(80,30,100));assert(!f.calls.some(c=>c.path==='login/sms/pointer'));
});
test('animation freezes displayed frame through gesture, epoch changes cancel',async()=>{
 const f=fixture();await f.manual();const image=f.$('#smsImage'),src=image.src;image.onpointerdown(f.event(10,20,100));
 await f.render({frame_id:2});assert.equal(image.src,src);image.onpointermove(f.event(80,30,140));await image.onpointerup(f.event(110,40,180));
 assert.equal(f.calls.find(c=>c.path==='login/sms/pointer').body.frame,1);
 image.onpointerdown(f.event(10,20,200));await f.render({frame_id:3,challenge_epoch:2});assert.equal(f.run('smsGesture'),null);
});
test('loading keeps the visible frame interactive and freezes it during a gesture',async()=>{
 const f=fixture();await f.manual();const image=f.$('#smsImage'),src=image.src;let preview;
 f.preview=p=>{preview=p};await f.render({frame_id:2});assert(f.run('smsFrameLoading'));assert(!image.hidden);assert.equal(image.src,src);
 image.onpointerdown(f.event(10,20,100));preview.onload();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(image.src,src);assert(!f.run('smsFrameLoading'));await image.onpointerup(f.event(10,20,150));
 assert.equal(f.calls.find(c=>c.path==='login/sms/pointer').body.frame,1);
});
test('stale-frame response refreshes and tells user to repeat gesture',async()=>{
 const f=fixture();await f.manual();f.error=Object.assign(Error('stale'),{code:'stale_frame'});const image=f.$('#smsImage');image.onpointerdown(f.event(10,20,0));await image.onpointerup(f.event(10,20,60));
 assert(f.notices.includes('画面已更新，请重新操作'));assert(f.revoked.length);assert(f.calls.filter(c=>c.path==='login/sms').length);
});
test('frame fetch version errors schedule fresh status, no image loop',async()=>{
 const f=fixture();f.frameError=true;await f.manual();assert(f.$('#smsImage').hidden);assert([...f.timers.values()].some(t=>t.delay===200));assert.equal(f.calls.filter(c=>c.path.includes('/frame?')).length,1);
});
test('mode switch clears inputs, images and polling; QR remains separate',async()=>{
 const f=fixture();await f.manual();f.$('#smsPhone').value='13800138000';f.$('#smsCode').value='246810';f.run("setSmsMode('qr')");assert(f.$('#smsLoginCard').hidden);assert.equal(f.$('#smsPhone').value,'');assert.equal(f.$('#smsCode').value,'');assert(f.$('#smsImage').hidden);assert.equal(f.run('smsEnabled'),false);assert(f.revoked.length);
 assert(html.includes("qrEnabled=state.login_mode==='qr'"));assert(html.includes("setSmsMode(state.login_mode)"));
});
test('mobile form contract, no duplicate IDs, text panel starts collapsed',async()=>{
 assert(html.includes('type="tel" inputmode="numeric" autocomplete="tel"'));assert(html.includes('autocomplete="one-time-code"'));assert(html.includes('touch-action:none'));assert(html.includes('<details id="smsTextPanel">'));assert(!html.includes('<details id="smsTextPanel" open'));
 const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);assert.equal(ids.length,new Set(ids).size);
});
(async()=>{for(const {name,run} of tests){await run();console.log('ok - '+name)}console.log(`SMS UI: ${tests.length} tests passed, 0 failed`);})().catch(e=>{console.error(e);process.exitCode=1});
