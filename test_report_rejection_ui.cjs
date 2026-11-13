// Synthetic full-batch rejection and uncertain submission recovery, without network.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('hub.html','utf8');
function fixture(){
 const nodes={},calls=[],notices=[];let serial=0;
 const $=id=>nodes[id]??={value:'',hidden:false,textContent:'',innerHTML:'',addEventListener(){},close(){this.open=false},focus(){this.focused=true},scrollIntoView(){this.scrolled=true}};
 const ctx=vm.createContext({$,calls,chosenPlatform:'baidu',sessionStorage:{getItem:()=>null,setItem(){}},esc:String,infoTip:String,statusName:String,readingInfo:()=>false,loginAction:()=>'',toast:message=>notices.push(message),
  platformModules:()=>['related','audience'],insightDates:(kind,start_date,end_date)=>({start_date,end_date}),queryRegions:q=>q,dateHint(){},updateRange:()=>true,drawPlatforms(){},refresh:async()=>{},crypto:{randomUUID:()=> 'synthetic-'+(++serial)},
  async api(path,options){calls.push({path,key:options.headers['Idempotency-Key'],body:options.body});if(ctx.failure)throw ctx.failure;return {job:{id:'job-'+calls.length,status:'queued'}}}});
 vm.runInContext(html.slice(html.indexOf('let reportBatch=null'),html.indexOf('(async()=>{try{platforms=')),ctx);
 $('#queryPlatform').value='baidu';$('#queryWords').value='合成词甲';$('#queryStart').value='2026-10-21';$('#queryEnd').value='2026-10-27';
 const f={ctx,$,calls,notices,run:code=>vm.runInContext(code,ctx)};
 f.submit=async word=>{if(word)$('#queryWords').value=word;$('#queryDialog').open=true;await $('#queryForm').onsubmit({preventDefault(){}})};
 return f;
}
(async()=>{
 for(const [code,status] of [['collection_paused',409],['platform_unavailable',422],['unauthorized',401],['forbidden',403],['csrf_rejected',403],['authentication_throttled',429]]){
  const f=fixture();f.ctx.failure=Object.assign(Error('合成平台拒绝'),{status,code});await f.submit();
  assert.equal(f.calls.length,1);assert(f.run('reportBatch.entries.every(e=>e.rejected&&!e.jobId&&e.code==='+JSON.stringify(code)+')'));
  assert.equal((f.$('#reportProgressRows').innerHTML.match(/未创建/g)||[]).length,3);assert(!f.$('#reportProgressRows').innerHTML.includes('待提交'));assert(f.$('#retryBundle').hidden);
  const firstKey=f.calls[0].key;f.ctx.failure=null;await f.submit();
  assert.equal(f.calls.length,4);assert.equal(f.calls[1].path,'queries');assert.notEqual(f.calls[1].key,firstKey);assert(f.run('reportBatch.entries.every(e=>e.jobId)'));
  const secondKey=f.calls[1].key;await f.submit('合成词乙');assert.equal(f.calls.length,7);assert.equal(f.calls[4].path,'queries');assert.notEqual(f.calls[4].key,secondKey);assert.equal(f.calls[4].body.keywords[0],'合成词乙');
 }
 for(const failure of [Error('合成网络错误'),Object.assign(Error('合成服务错误'),{status:503,code:'unavailable'})]){
  const f=fixture();f.ctx.failure=failure;await f.submit();const firstKey=f.calls[0].key;
  assert(f.run('reportBatch.entries[0].error&&!reportBatch.entries[0].rejected'));
  for(const word of ['合成词乙','合成词甲']){
   f.$('#reportProgress').open=false;await f.submit(word);
   assert.equal(f.calls.length,1);assert(f.$('#reportProgress').open);assert(f.$('#retryBundle').focused&&f.$('#retryBundle').scrolled);assert(!f.$('#queryDialog').open);assert(f.notices.at(-1).includes('「本次完整查询」'));
  }
  // A later authentication rejection cannot disprove an earlier accepted request.
  f.ctx.failure=Object.assign(Error('合成认证拒绝'),{status:401,code:'unauthorized'});await f.$('#retryBundle').onclick();
  assert.equal(f.calls[1].key,firstKey);assert(!f.run('reportBatch.entries[0].rejected'));assert(!f.$('#retryBundle').hidden);
  f.ctx.failure=null;await f.$('#retryBundle').onclick();assert.equal(f.calls[2].key,firstKey);assert(f.run('reportBatch.entries.every(e=>e.jobId)'));assert.equal(f.calls.length,5);
 }
 const f=fixture();await f.submit();const first=f.calls[0].key;await f.submit();assert.equal(f.calls.length,6);assert.notEqual(f.calls[3].key,first);
 console.log('PASS platform batch rejection, fresh identical/different queries, uncertain interception and stable retry keys');
})().catch(e=>{console.error(e);process.exitCode=1});
