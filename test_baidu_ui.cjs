// Synthetic deferred responses: switching platforms must not draw an old response.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('hub.html','utf8');
const code=html.slice(html.indexOf('async function loadSeries()'),html.indexOf("document.querySelectorAll('[data-device]')"));
const nodes={};const $=id=>nodes[id]??={value:'',style:{},dataset:{},textContent:'',innerHTML:''};
$('#keywordSelect').value='合成词';
const modulesCode=html.slice(html.indexOf('function platformInfo('),html.indexOf('function drawPlatforms('));
const modulesCtx=vm.createContext({platforms:[{id:'douyin',modules:['related','audience']},{id:'baidu',modules:['related']},{id:'empty'}],chosenPlatform:'douyin'});
vm.runInContext(modulesCode,modulesCtx);
assert.deepEqual(Array.from(modulesCtx.platformModules('douyin')),['related','audience']);
assert.deepEqual(Array.from(modulesCtx.platformModules('baidu')),['related']);
assert.deepEqual(Array.from(modulesCtx.platformModules('empty')),[]);
const calls=[],drawn=[];
const ctx=vm.createContext({$,chosenPlatform:'douyin',platforms:[{id:'douyin',available:true},{id:'baidu',available:true}],
 baiduDevice:'all',rangeError:()=>'',insightContext:()=>ctx.chosenPlatform,readingInfo:()=>false,esc:String,URLSearchParams,drawChart:data=>drawn.push(data),
 api:path=>new Promise(resolve=>calls.push({path,resolve}))});
vm.runInContext(code,ctx);
(async()=>{
 const old=vm.runInContext('loadSeries()',ctx);
 ctx.chosenPlatform='baidu';
 const current=vm.runInContext('loadSeries()',ctx);
 assert(calls[0].path.includes('platform=douyin'));assert(calls[1].path.includes('platform=baidu'));
 calls[1].resolve({platform:'baidu'});await current;
 calls[0].resolve({platform:'douyin'});await old;
 assert.deepEqual(drawn,[{platform:'baidu'}]);
 $('#keywordSelect').value='';$('#exportLink').style.display='';
 await vm.runInContext('loadSeries()',ctx);
 assert.equal($('#exportLink').style.display,'none');
 assert($('#trendSummary').textContent.includes('暂无趋势数据'));
 assert.equal(calls.length,2);
 $('#keywordSelect').value='合成词';$('#chart').dataset.platform='douyin';$('#chartMeta').textContent='old Douyin metadata';$('#exportLink').style.display='';
 ctx.api=async()=>{throw Error('synthetic failed series')};
 await assert.rejects(vm.runInContext('loadSeries()',ctx));
 assert.equal($('#exportLink').style.display,'none');assert.equal($('#chartMeta').textContent,'');
 assert.equal($('#chart').dataset.platform,'baidu');
 const posted=[];ctx.toast=()=>{};ctx.refresh=async()=>{};ctx.api=async(path,options)=>posted.push(options.body);
 vm.runInContext(html.slice(html.indexOf("$('#resumeButton').onclick"),html.indexOf('async function loadKeys(')),ctx);
 await $('#resumeButton').onclick();await $('#baiduResumeButton').onclick();
 assert.deepEqual(JSON.parse(JSON.stringify(posted)),[{}, {platform:'baidu'}]);
 console.log('PASS explicit resume targets');
 console.log('PASS Baidu platform switch: reject stale Douyin response, clear export without data');
})().catch(e=>{console.error(e);process.exitCode=1});
