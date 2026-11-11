const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('hub.html','utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
const start=script.indexOf('// Comments collection:'),end=script.indexOf('// End comments collection.');
assert(start>=0&&end>start);
for(const id of ['overviewComments','commentsStatus','commentsProbeState','commentsBusyReason','commentsPauseReason','commentsVideoInput','commentsStartButton','commentsPauseButton','commentsResumeButton','commentsLoginButton'])assert(html.includes(`id="${id}"`),id);
assert(!/id="comments[^" ]*(?:Phone|Code|Qr)/i.test(html));
function fixture(){
 const nodes={},calls=[],timers=new Map();let timer=0;
 const $=id=>nodes[id]??={hidden:false,value:'',textContent:'',scrollIntoView(){this.scrolled=true},setAttribute(){},focus(){this.focused=true}};
 const f={$,calls,timers,status:{probe:null,running:false},tab:null,error:null};
 const ctx=vm.createContext({$,tab(name){f.tab=name},setTimeout(fn,delay){timers.set(++timer,{fn,delay});return timer},clearTimeout(id){timers.delete(id)},async api(path,options){calls.push({path,body:options?.body});if(options&&f.error){const e=f.error;f.error=null;throw e}return path==='comments/status'?f.status:{ok:true}}});
 vm.runInContext(script.slice(start,end),ctx);f.run=code=>vm.runInContext(code,ctx);return f;
}
(async()=>{
 const f=fixture();await new Promise(setImmediate);
 f.$('#commentsVideoInput').value='  https://example.com/1  \n\nhttps://example.com/2\r\n';
 await f.$('#commentsStartButton').onclick();
 assert.deepStrictEqual(Array.from(f.calls.find(c=>c.path==='comments/start').body.videos),['https://example.com/1','https://example.com/2']);
 await f.$('#commentsPauseButton').onclick();await f.$('#commentsResumeButton').onclick();
 for(const path of ['comments/pause','comments/resume'])assert.deepStrictEqual(Object.keys(f.calls.find(c=>c.path===path).body),[]);
 assert(f.$('#commentsMessage').textContent.includes('再次点击开始'));
 f.status={probe:'not_logged_in',paused:true,pause_reason:'verification_required',waiting_for_index:false};await f.run('loadComments()');
 assert(!f.$('#commentsLoginButton').hidden);assert(f.$('#commentsPauseReason').textContent.includes('verification_required'));
 f.$('#commentsLoginButton').onclick();assert.equal(f.tab,'connections');assert(f.$('#smsLoginCard').focused);
 f.$('#smsLoginCard').hidden=true;f.$('#commentsLoginButton').onclick();assert(f.$('#qrLoginCard').focused);
 f.$('#qrLoginCard').hidden=true;f.$('#commentsLoginButton').onclick();assert(f.$('#desktopLoginMode').focused);
 f.error=Error('comments_busy：正在采集评论，稍后重试');await f.$('#commentsStartButton').onclick();assert(f.$('#commentsMessage').textContent.includes('comments_busy'));
 f.status={probe:'logged_in',running:true,waiting_for_index:true};await f.run('loadComments()');assert(f.$('#commentsStatus').textContent.includes('waiting_for_index'));assert(f.$('#commentsBusyReason').textContent.includes('等待指数'));assert.equal(f.timers.size,1);
 f.status={running:false};await f.run('loadComments()');assert.equal(f.timers.size,0);
 console.log('comments UI: 1 focused suite passed');
})().catch(e=>{console.error(e);process.exitCode=1});
