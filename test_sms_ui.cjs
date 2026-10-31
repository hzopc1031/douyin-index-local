// Node VM only: synthetic forms/pointers, no browser storage or network.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('hub.html','utf8');
const source=html.slice(html.indexOf('// SMS login:'),html.indexOf('// End SMS login.'));
const tests=[];function test(name,run){tests.push({name,run})}
function fixture(){
 const nodes={},calls=[],timers=new Map(),revoked=[],notices=[];let serial=0,blobs=0;
 const $=id=>nodes[id]??=(function(){let src='';const node={hidden:false,disabled:false,value:'',textContent:'',style:{},captured:null,getBoundingClientRect:()=>({left:10,top:20,width:200,height:100}),setPointerCapture(id){this.captured=id},hasPointerCapture(id){return this.captured===id},releasePointerCapture(){this.captured=null},getAttribute(key){return key==='src'?src:null},removeAttribute(key){if(key==='src')src=''}};Object.defineProperty(node,'src',{get:()=>src,set:value=>{src=value;queueMicrotask(()=>node.onload?.())}});return node})();
 const state={state:'idle',phone:'',resend_in:0,attempts_left:0,frame_id:0,challenge_epoch:0,verification_mode:null,message:'尚未开始短信登录'};
 const f={$,calls,timers,revoked,notices,state,error:null,frameError:null};
 const ctx=vm.createContext({$,Math,URL:{createObjectURL(){return 'blob:synthetic-'+(++blobs)},revokeObjectURL(url){revoked.push(url)}},
   setTimeout(fn,delay){const id=++serial;timers.set(id,{fn,delay});return id},clearTimeout(id){timers.delete(id)},
   localStorage:{setItem(){throw Error('storage write')}},sessionStorage:{setItem(){throw Error('storage write')}},
   toast(message){notices.push(message)},openDesktop(){f.desktop=true},
   async api(path,options){calls.push({path,body:options?.body});if(f.error&&options){const e=f.error;f.error=null;throw e}return {...state}},
   async fetch(path,options){calls.push({path,options});if(f.frameError)return {ok:false,json:async()=>({error:{code:'stale_frame',message:'画面已更新，请重新操作'}})};return {ok:true,blob:async()=>({synthetic:true})}}
 });vm.runInContext(source,ctx);
 f.run=code=>vm.runInContext(code,ctx);f.render=async changes=>{Object.assign(state,changes);ctx.data={...state};f.run('renderSms(data)');await new Promise(resolve=>setImmediate(resolve))};
 f.event=(x,y,t,id=1)=>({clientX:x,clientY:y,timeStamp:t,pointerId:id,button:0,isPrimary:true,preventDefault(){}});
 f.manual=async()=>{f.run('smsEnabled=true');await f.render({state:'verify_send',phone:'138****8000',verification_mode:'manual',frame_id:1,challenge_epoch:1})};
 return f;
}

test('all states and buttons, masked number only',async()=>{
 const f=fixture();f.run('smsEnabled=true');
 for(const state of ['idle','opening','sending','verify_send','code_sent','submitting','code_invalid','verify_login','succeeded','already_logged_in','rate_limited','failed','timeout','cancelled']){
  const verify=state.startsWith('verify_'),active=['opening','sending','verify_send','code_sent','submitting','code_invalid','verify_login'].includes(state);
  await f.render({state,verification_mode:verify?'manual':null,resend_in:60,attempts_left:3,phone:'138****8000'});
  assert.equal(f.$('#smsPhoneForm').hidden,active);assert.equal(f.$('#smsCodeForm').hidden,!['code_sent','code_invalid'].includes(state));assert.equal(f.$('#smsVerification').hidden,!verify);assert.equal(f.$('#smsCancel').hidden,!active);assert.equal(f.$('#smsNumber').textContent,'138****8000');
  assert.equal(f.run('smsTimer!==null'),active);
 }
 await f.render({phone:'13800138000'});assert.equal(f.$('#smsNumber').textContent,'');
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
