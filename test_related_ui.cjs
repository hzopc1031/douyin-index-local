// Synthetic related dates, rendering and read requests; no network.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('hub.html','utf8'),nodes={};
const $=id=>nodes[id]??={value:'',dataset:{},textContent:'',innerHTML:'',querySelectorAll(){return []}};
const calls=[];let reading=false;
const ctx=vm.createContext({$,URLSearchParams,esc:String,chosenPlatform:'douyin',readingInfo:()=>reading,
 sessionStorage:{getItem(){return null}},rangeError(){return ''},
 api:async path=>{if(path.startsWith('insights/ranges?'))return {ranges:[]};calls.push(new URLSearchParams(path.split('?')[1]));return {status:'not_collected'}},
  moduleEntry(){return null},moduleState(){return '未采集'},insightMessage(kind,target,message){$(target).textContent=message}});
vm.runInContext('let tipSequence=0;'+html.slice(html.indexOf('function infoTip('),html.indexOf('function openInfo(')),ctx);
vm.runInContext("let relatedLatestDate='2026-10-25';"+
 html.slice(html.indexOf('const insightTitles='),html.indexOf('let insightRevision=')),ctx);
vm.runInContext(html.slice(html.indexOf('let reportBatch=null'),html.indexOf('function renderReportProgress(')),ctx);
ctx.query={platform:'douyin',keywords:['合成词'],start_date:'2026-09-28',end_date:'2026-10-27'};
let batch=vm.runInContext("makeReportBatch(query,'fixture')",ctx);
assert.deepEqual(Array.from(batch.entries,e=>e.body.end_date),['2026-10-27','2026-10-25','2026-10-27']);
assert(batch.entries.every(e=>e.body.start_date==='2026-09-28'));
for(const end of ['2026-10-24','2026-10-25']){
 ctx.query.end_date=end;batch=vm.runInContext("makeReportBatch(query,'early')",ctx);
 assert(batch.entries.every(e=>e.body.end_date===end));
}
ctx.query.start_date='2026-10-26';ctx.query.end_date='2026-10-27';
assert.throws(()=>vm.runInContext("makeReportBatch(query,'invalid')",ctx),/开始日须不晚于/);
$('#keywordSelect').value='合成词';$('#insightStart').value='2026-09-28';$('#insightEnd').value='2026-10-27';$('#relatedSort').value='relation_score';
ctx.result={data:{search_related:[],content_related:[]},metadata:{start_date:'2026-09-28',end_date:'2026-10-25',source:'synthetic',fetched_at:'synthetic'}};
vm.runInContext("renderInsight('related','#relatedData',result)",ctx);
for(const id of ['#relatedData','#relatedSummary']){
 assert($(id).innerHTML.includes('关联词数据到 10-25'));
 assert.match($(id).innerHTML, /class="info-popover"[^>]*>关联词数据截至 2026-10-25，使用 2026-09-28 至 2026-10-25/);
 assert(!$(id).innerHTML.includes('<p class="tiny">关联词数据截至'));
}
vm.runInContext("let insightRevision=1;function insightContext(){return 'fixture'};"+
 html.slice(html.indexOf('async function loadInsight('),html.indexOf('async function loadInsights(')),ctx);
(async()=>{
 await vm.runInContext("loadInsight('related','#relatedData',1,'fixture')",ctx);
 assert.deepEqual(calls.map(p=>p.get('end_date')),['2026-10-27','2026-10-25']);
 assert(calls.every(p=>p.get('start_date')==='2026-09-28'));
 assert($('#relatedData').innerHTML.includes('关联词查询使用 2026-09-28 至 2026-10-25'));
 calls.length=0;$('#insightEnd').value='2026-10-24';
 await vm.runInContext("loadInsight('related','#relatedData',1,'fixture')",ctx);
 assert.deepEqual(calls.map(p=>p.get('end_date')),['2026-10-24']);
 calls.length=0;$('#insightEnd').value='2026-10-27';
 vm.runInContext("relatedLatestDate='2026-10-26'",ctx);
 ctx.api=async path=>{
  if(path.startsWith('insights/ranges?'))return {ranges:[{status:'available',start_date:'2026-09-28',end_date:'2026-10-25'}]};
  const params=new URLSearchParams(path.split('?')[1]);calls.push(params);
  return params.get('end_date')==='2026-10-25'?{status:'available',result:ctx.result}:{status:'not_collected'};
 };
 await vm.runInContext("loadInsight('related','#relatedData',1,'fixture')",ctx);
 assert.deepEqual(calls.map(p=>p.get('end_date')),['2026-10-27','2026-10-26','2026-10-25']);
 assert($('#relatedData').innerHTML.includes('关联词数据截至 2026-10-25'));
 calls.length=0;$('#insightEnd').value='2026-10-27';
 ctx.api=async path=>{calls.push(new URLSearchParams(path.split('?')[1]));return {status:'available',result:{...ctx.result,metadata:{...ctx.result.metadata,end_date:'2026-10-27'}}}};
 await vm.runInContext("loadInsight('related','#relatedData',1,'fixture')",ctx);
 assert.equal(calls.length,1);assert($('#relatedData').innerHTML.includes('关联词数据截至 2026-10-27'));
 ctx.api=async path=>path.startsWith('insights/ranges?')?{ranges:[]}:{status:'no_data',result:ctx.result};
 await vm.runInContext("loadInsight('related','#relatedData',1,'fixture')",ctx);
 for(const id of ['#relatedData','#relatedSummary']){
  assert($(id).innerHTML.includes('<span>无数据</span>'));
  assert.match($(id).innerHTML,/class="info-popover"[^>]*>平台未返回本日期段的关联词，不代表指数为零。 关联词查询使用 2026-09-28 至 2026-10-25/);
 }
 const before=$('#relatedData').innerHTML;reading=true;
 ctx.api=async()=>({status:'available',result:ctx.result});
 await vm.runInContext("loadInsight('related','#relatedData',1,'fixture')",ctx);
 assert.equal($('#relatedData').innerHTML,before);
 ctx.api=async()=>{throw Error('synthetic')};
 await vm.runInContext("loadInsight('related','#relatedData',1,'fixture')",ctx);
 assert.equal($('#relatedData').innerHTML,before);
 assert.equal($('#relatedData').textContent,'');
 reading=false;ctx.moduleEntry=()=>({body:{start_date:'2026-09-28',end_date:'2026-10-23'}});
 ctx.api=async path=>path.startsWith('insights/ranges?')?{ranges:[]}:{status:'not_collected'};
 await vm.runInContext("loadInsight('related','#relatedData',1,'fixture')",ctx);
 assert($('#relatedData').innerHTML.includes('关联词查询使用 2026-09-28 至 2026-10-23'));
 console.log('related dates: submission, unchanged trend/audience, earlier input, empty interval, labels and exact/fallback reads passed');
})().catch(e=>{console.error(e);process.exitCode=1});
