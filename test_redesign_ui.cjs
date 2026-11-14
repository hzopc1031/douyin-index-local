// Offline summary edge cases and preservation of the single-file UI contract.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('hub.html','utf8'),script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const summary=script.slice(script.indexOf('function drawChart('),script.indexOf(' const dates=',script.indexOf('function drawChart(')))+'}';
const nodes={},$=id=>nodes[id]??={parentElement:{lastChild:{}},innerHTML:''};
const ctx=vm.createContext({$,platformInfo:()=>({metric_name:'搜索指数'}),chosenPlatform:'douyin',numberLabel:v=>v==null?'—':String(v),esc:String,infoTip:s=>s});
vm.runInContext(summary,ctx);
function render(points){ctx.points=points;vm.runInContext('drawChart({points})',ctx);return $('#trendSummary').innerHTML}
const point=(date,value)=>({date,value,composite_index:null});
assert(render([point('2026-11-01',100),point('2026-11-02',120)]).includes('+20.0%'));
assert(render([point('2026-11-01',100),point('2026-11-02',80)]).includes('-20.0%'));
for(const points of [[point('2026-11-02',120)],[point('2026-11-01',0),point('2026-11-02',120)],[point('2026-11-01',null),point('2026-11-02',120)],[point('2026-11-01',100),point('2026-11-02',null)],[point('2026-11-01',100),point('2026-11-03',120)]]){
 const result=render(points);assert(result.includes('<strong>—</strong>'));assert(!result.includes('NaN'));assert(!result.includes('Infinity'));
}
const missing=render([point('2026-11-01',null)]);assert(missing.includes('class="summary-number">—'));assert(missing.includes('无数据'));
const css=html.match(/<style>([\s\S]*?)<\/style>/)[1],afterTokens=css.slice(css.indexOf('}'));
assert(!/#[a-f\d]{3,8}\b|\brgba?\(|\boklch\(/i.test(afterTokens));
assert(!/font-family:(?!var\()/i.test(afterTokens));assert(!/font-size:(?:[0-9]|1[0-2])px/.test(afterTokens));
assert(!/letter-spacing:-|transition:\s*all/.test(css));
assert(css.includes('html,body{overflow-x:clip}'));assert(css.includes('@media(prefers-reduced-motion:reduce)'));
const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);assert.equal(ids.length,new Set(ids).size,'duplicate DOM id');
console.log('PASS redesign: daily change, missing/zero/gap dates, color/font tokens, type floor, motion and unique IDs');
