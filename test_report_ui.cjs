// Synthetic orchestration checks; no network and no credentials.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('hub.html','utf8');
const code=html.slice(html.indexOf('let reportBatch=null'),html.indexOf("$('#retryBundle').onclick"));
const nodes={};const $=id=>nodes[id]??={value:'',hidden:false,textContent:'',innerHTML:'',disabled:false};
let calls=[],fail=true;const ctx=vm.createContext({$,sessionStorage:{getItem(){return null},setItem(){}},esc:s=>String(s),statusName:s=>s,toast(){},api:async(path,options)=>{calls.push({path,key:options?.headers?.['Idempotency-Key'],body:options?.body});if(path==='insights/queries'&&fail){fail=false;throw Error('simulated disconnect')}return {job:{id:'job-'+calls.length,status:'queued',cache_hit:false}}}});
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
 $('#keywordSelect').value='酵素';$('#insightStart').value='2026-10-21';$('#insightEnd').value='2026-10-27';assert(vm.runInContext("moduleState('related')",ctx).includes('queued'));$('#insightStart').value='2026-10-20';assert(vm.runInContext("moduleState('related')",ctx).includes('尚无'));
 const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);assert.equal(ids.length,new Set(ids).size);for(const id of ['reportProgress','retryBundle','relatedData','audienceData'])assert(ids.includes(id));assert(!html.includes("$('#insightKind')"));assert(!html.includes("$('#collectInsight')"));
 console.log('report grouping, independent endpoints, partial submission recovery, idempotency, read-only refresh and shared-context checks passed');
})().catch(e=>{console.error(e);process.exitCode=1});
