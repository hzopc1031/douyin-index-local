// Synthetic initial-date regression. No browser, account or network.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('hub.html','utf8');
function setup(batch=null){
 const nodes={};const $=id=>nodes[id]??={value:'',min:'2024-11-02',max:'2026-11-01'};
 const hints=[];
 const context=vm.createContext({$,reportBatch:batch,document:{querySelectorAll(){return [{set textContent(v){hints[0]=v},get textContent(){return hints[0]||''}}]}},dateHint(){},selectRangePreset(){},updateRange(){}});
 context.hints=hints;
 vm.runInContext(html.slice(html.indexOf('function presetStart('),html.indexOf('function selectRangePreset(')),context);
 vm.runInContext(html.slice(html.indexOf('let initialRangesSet='),html.indexOf('function dateHint(')),context);
 return {$,hints,initialize:history=>{context.history=history;vm.runInContext('initializeRanges(history)',context)}};
}
const old={start_date:'2026-09-26',end_date:'2026-10-25',keywords:['合成词']};
const history={latest_available_date:'2026-10-31',datasets:[{metadata:old}]};
let t=setup();t.initialize({...history,related_latest_available_date:'2026-10-26',latest_available_date_source:'official_cadence'});
for(const prefix of ['query','insight']){
 assert.equal(t.$('#'+prefix+'Start').value,'2026-10-02');
 assert.equal(t.$('#'+prefix+'End').value,'2026-10-31');
}
assert.equal(t.hints[0],'默认截止日：2026-10-31（按官方搜索指数更新节奏推算）。 关联词默认截至 2026-10-26。 快捷段以当前结束日期为准。');
t=setup();t.initialize({...history,latest_available_date_source:'stored_search_index'});
assert.equal(t.hints[0],'默认截止日：2026-10-31（回退到本地已存搜索指数）。 快捷段以当前结束日期为准。');
t=setup();t.initialize(history);
assert.equal(t.hints[0],'默认截止日：2026-10-31。 快捷段以当前结束日期为准。');
t.$('#queryStart').value='2026-10-01';t.$('#insightEnd').value='2026-10-20';
t.initialize({...history,latest_available_date:'2026-11-01'});
assert.equal(t.$('#queryStart').value,'2026-10-01');assert.equal(t.$('#insightEnd').value,'2026-10-20');
for(const latest of [undefined,null,'bad']){
 t=setup();t.initialize({...history,latest_available_date:latest});
 assert.equal(t.$('#queryStart').value,'');assert.equal(t.$('#insightEnd').value,'');
}
t=setup({query:old,entries:[{jobId:'done',status:'succeeded'}]});t.initialize(history);
assert.equal(t.$('#insightEnd').value,'2026-10-31');
t=setup({query:old,entries:[{jobId:null,error:'unconfirmed'}]});t.initialize(history);
assert.equal(t.$('#queryWords').value,'合成词');
for(const prefix of ['query','insight']){
 assert.equal(t.$('#'+prefix+'Start').value,old.start_date);
 assert.equal(t.$('#'+prefix+'End').value,old.end_date);
}
t=setup();t.$('#queryEnd').value='2026-10-20';t.initialize(history);
assert.equal(t.$('#queryStart').value,'');assert.equal(t.$('#queryEnd').value,'2026-10-20');
console.log('initial dates: latest known day, 30 days, empty/legacy history, preserved edits and pending batch passed');
