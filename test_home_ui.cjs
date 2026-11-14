// Offline home readings: preserve missing values, isolate failures and escape API text.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('hub.html','utf8'),script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
const nodes={},$=id=>nodes[id]??={innerHTML:'',hidden:false,classList:{toggle(){}}};
const ctx=vm.createContext({$,esc:s=>String(s??'').replaceAll('<','&lt;'),hotText:v=>v==null?'—':String(v).replaceAll('<','&lt;'),hotTime:String,hotEmptyText:()=> '暂无快照',commentsName:String});
vm.runInContext(script.slice(script.indexOf('let homeHubState='),script.indexOf("$('#homeRefresh').onclick")),ctx);
ctx.renderHomeUsage({today_used:0,daily_limit:3000,batch_limit:1500});assert($('#homeCommentsUsage').innerHTML.includes('>0<'));assert($('#homeCommentsUsage').innerHTML.includes('1500'));
ctx.renderHomeUsage();assert($('#homeCommentsUsage').innerHTML.includes('—'));assert(!$('#homeCommentsUsage').innerHTML.includes('>0<'));
ctx.renderHomeStatus({daily_online_attempts:2,limits:{online_attempts_per_day:50}});assert($('#homeBaiduMeter').hidden);assert($('#homeIndexUsage').innerHTML.includes('>2<'));
ctx.renderHomeStatus({platform_status:{baidu:{daily:{attempts:0},limits:{logical_daily:20}}}});assert(!$('#homeBaiduMeter').hidden);assert($('#homeBaiduUsage').innerHTML.includes('>0<'));
ctx.renderHomeJobs([{status:'running',query:{keywords:['<script>']}},{status:'queued',query:{keywords:['虚构乙']}},{status:'succeeded',query:{keywords:['不显示']}}]);assert($('#homeJobs').innerHTML.includes('&lt;script>'));assert($('#homeJobs').innerHTML.includes('排队中'));assert(!$('#homeJobs').innerHTML.includes('不显示'));
ctx.renderHomeJobs([]);assert($('#homeJobs').innerHTML.includes('暂无运行'));
ctx.renderHomeComments({running:true,waiting_for_index:true,progress:{video_index:1,video_total:2,requests:0,batch_limit:1500}});assert($('#homeCommentsTask').innerHTML.includes('等待指数'));assert($('#homeCommentsTask').innerHTML.includes('>0 / 1500<'));assert($('#homeCommentsTask').innerHTML.includes('—'));
ctx.renderHomeComments({paused:true,pause_reason:'verification_required'});assert($('#homeCommentsTask').innerHTML.includes('已暂停'));
ctx.renderHomeHot({items:Array.from({length:10},(_,i)=>({position:i+1,word:'虚构'+i,hot_value:i}))});assert.equal(($('#homeHot').innerHTML.match(/<li>/g)||[]).length,5);
ctx.homeError('homeHot',Error('<img>'),'hot');assert($('#homeHot').innerHTML.includes('&lt;img>'));assert($('#homeHot').innerHTML.includes('data-home-retry="hot"'));assert($('#homeIndexUsage').innerHTML);
console.log('PASS home: budgets, missing/zero, independent errors, queue, progress, top five and escaping');
