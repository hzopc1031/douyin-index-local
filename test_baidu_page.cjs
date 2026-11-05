const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const script = fs.readFileSync('baidu_page.js', 'utf8');
const query = {keywords:['synthetic-word'], start_date:'2026-10-01', end_date:'2026-10-07', _origin:'https://index.baidu.com', _budget_day:new Date(Date.now()+8*3600000).toISOString().slice(0,10)};
async function run(mode) {
  const series = {startDate:query.start_date, endDate:query.end_date, type:'day', data:'ababababababc'};
  const word = {name:'synthetic-word', wordType:1};
  if (mode === 'truncated_end') { series.endDate = '2026-10-05'; series.data = 'ababababc'; }
  if (mode === 'trailing_empty') series.data = 'abababab cbb'.replace(' ', '');
  if (mode === 'end_after') series.endDate = '2026-10-08';
  if (mode === 'start_mismatch') series.startDate = '2026-10-02';
  if (mode === 'bad_word_type') word.wordType = 2;
  if (mode === 'wrong_name') word.name = 'synthetic-secret';
  if (mode === 'point_count') series.data = 'abc';
  const data = {uniqid:'synthetic-secret', userIndexes:[{word:[word], all:series, pc:series, wise:series}]};
  const fetch = async path => new Response(JSON.stringify({status:0, data:path.startsWith('/api/') ? data : 'abc1,2'}), {headers:{'content-type':'application/json'}});
  const timer = (...args) => { const handle = setTimeout(...args); handle.unref(); return handle; };
  const context = {location:{origin:query._origin}, window:{Paris:{getAcsInstance:cb => cb(null,{getSign:cb => cb(null,'synthetic-signature')})}}, fetch, URLSearchParams, AbortController, TextDecoder, Uint8Array, Response, setTimeout:timer, clearTimeout};
  return vm.runInNewContext(script, context)(query);
}
(async () => {
  // Synthetic fixtures only: exercise request shapes and validation offline.
  async function audience(overrides = {}) {
    const paths = [];
    const social = {result:[{word:'synthetic-word', gender:[{desc:'男',rate:0.6,tgi:120}], age:[{desc:'青年',rate:0.4,tgi:90}]}]};
    const interest = {result:[{word:'synthetic-word', interest:[]}]};
    const region = {region:[{key:'synthetic-word',period:'20261001|20261007',prov:{917:1000},city:{138:800}}]};
    const fetch = async url => { paths.push(url); const data = url.includes('baseAttributes') ? social : url.includes('interest') ? interest : (overrides.region || region); return new Response(JSON.stringify({status:0,data}), {headers:{'content-type':'application/json'}}); };
    const context = {location:{origin:query._origin}, fetch, URLSearchParams, AbortController, TextDecoder, Uint8Array, Response, setTimeout, clearTimeout};
    const result = await vm.runInNewContext(script, context)({...query,kind:'audience'});
    assert.equal(paths.length, 3);
    const urls = paths.map(p => new URL(p, query._origin));
    assert.equal(urls[0].pathname, '/api/SocialApi/baseAttributes');
    assert.equal(urls[1].pathname, '/api/SocialApi/interest');
    assert.equal(urls[1].searchParams.get('typeid'), '');
    assert.equal(urls[0].searchParams.get('datelist'), null);
    assert.equal(urls[2].pathname, '/api/SearchApi/region');
    assert.equal(urls[2].searchParams.get('startDate'), query.start_date);
    assert.equal(urls[2].searchParams.get('endDate'), query.end_date);
    return result;
  }
  const audienceResult = await audience();
  assert.equal(audienceResult.status, 'ok');
  assert.equal(audienceResult.data.period.days, 7);
  const badPeriod = await audience({region:{region:[{period:'20261001|20271002',prov:{917:1000},city:{138:800}}]}});
  assert.equal(badPeriod.schema_diag.period_rule, 'span_out_of_range');
  const relatedQuery = {...query, kind:'related', end_date:'2026-11-04'};
  async function related(data, fallback = false) {
    let path;
    const fetch = async url => { path = url; return new Response(JSON.stringify({status:0, data}), {headers:{'content-type':'application/json'}}); };
    const context = {location:{origin:query._origin}, fetch, URLSearchParams, AbortController, TextDecoder, Uint8Array, Response, setTimeout, clearTimeout};
    const result = await vm.runInNewContext(script, context)({...relatedQuery, ...(fallback ? {related_date:'2026-11-01'} : {})});
    assert.equal(new URL(path, query._origin).searchParams.get('datelist'), fallback ? '20261101' : null);
    return result;
  }
  const valid = {period:'20261026|20261101', wordlist:[{wordGraph:[]}]};
  const good = await related(valid);
  assert.equal(good.status, 'ok');
  assert.equal(good.data.period.start_date, '2026-10-26');
  assert.equal(good.data.period.end_date, '2026-11-01');
  assert.equal((await related(valid, true)).status, 'ok');
  for (const period of [undefined, '', '20260230|20260306', '00000101|00000107', '20261001|20271002', '20261101|20261026']) {
    const result = await related({...valid, period});
    assert.equal(result.schema_diag.reason, 'related_period');
    assert.equal(result.schema_diag.period_returned, period !== undefined);
  }
  assert.equal((await related({...valid, period:'20261001|20261030'})).data.period.days, 30);
  for (const [period, rule, days] of [['20261001|20271002','span_out_of_range',367], ['20261101|20261026','start_after_end',-5], ['20261332|20270101','invalid_date',null], ['bad','unknown_format',null]]) {
    const diag = (await related({...valid, period})).schema_diag;
    assert.equal(diag.period_rule, rule); assert.equal(diag.period_days, days);
  }
  for (const keyword of [null, 'private-word', {}, [], 5]) {
    const result = await related({...valid, wordlist:[{keyword:'query', wordGraph:[{word:'result', keyword, pv:123, sim:0.7, ratio:-2}]}]});
    assert.equal(result.status, 'ok');
    assert.equal(JSON.stringify(result.data.wordlist[0].wordGraph[0]), JSON.stringify({word:'result', pv:123, sim:0.7, ratio:-2}));
    assert(!JSON.stringify(result.data).includes('private-word'));
  }
  for (const [item, rule] of [[{word:'x',pv:'12'},'pv_invalid'], [{word:'x',sim:-1},'sim_invalid'], [{word:'x',ratio:true},'ratio_invalid'], [{},'word_missing'], [{word:'x'.repeat(201)},'word_too_long'], [null,'item_not_object']]) {
    const diag = (await related({...valid, wordlist:[{wordGraph:[item]}]})).schema_diag;
    assert.equal(diag.reason, 'related_entry_shape'); assert.equal(diag.entry_rule, rule);
  }
  assert.equal((await related({...valid, wordlist:[{wordGraph:Array(101).fill({word:'x'})}]})).schema_diag.reason, 'too_many_entries');
  for (const sep of ['|', '~', ',', ' - ', '至']) for (const a of ['20261026', '2026-10-26']) for (const b of ['20261101', '2026-11-01']) {
    const result = await related({...valid, period:a + sep + b});
    assert.equal(result.status, 'ok');
    assert.equal(result.data.period.start_date, '2026-10-26');
    assert.equal(result.data.period.end_date, '2026-11-01');
  }
  for (const period of [{startDate:'20261026',endDate:'20261101'}, {start:'20261026',end:'20261101'}, {beginDate:'20261026',endDate:'20261101'}, {start_date:'20261026',end_date:'20261101'}, {begin:'20261026',end:'20261101'}, ['20261026','2026-11-01']]) assert.equal((await related({...valid, period})).status, 'ok');
  const graph = [{word:'synthetic', score:1, '敏感42':'secret'}, {word:'other', score:'secret'}];
  const success = await related({...valid, wordlist:[{wordGraph:graph}]});
  assert.equal(JSON.stringify(success.meta.entry_keys), JSON.stringify({masked:'string', score:'mixed', word:'string'}));
  const failure = await related({...valid, period:'2026年10月26日-2026年11月01日', wordlist:[{wordGraph:graph}]});
  assert.equal(failure.schema_diag.period_shape.pattern, '9999C99C99C-9999C99C99C');
  assert(!/[0-8\u4e00-\u9fff]|[b-zA-BD-Z]/.test(failure.schema_diag.period_shape.pattern));
  assert.equal(JSON.stringify(failure.schema_diag.entry_keys), JSON.stringify(success.meta.entry_keys));
  const objectShape = (await related({...valid, period:{start:'secret42', '敏感42':[1,{x:'secret'}]}})).schema_diag.period_shape;
  assert.equal(JSON.stringify(objectShape), JSON.stringify({type:'object',keys:['masked','start'],values:{start:{type:'string',length:8,pattern:'aaaaaa99'},masked:{type:'array',length:2,items:[{type:'number'},{type:'object'}]}}}));
  const arrayShape = (await related({...valid, period:['secret', 123, true, null, 'ignored']})).schema_diag.period_shape;
  assert.equal(JSON.stringify(arrayShape), JSON.stringify({type:'array',length:5,items:[{type:'string',length:6,pattern:'aaaaaa'},{type:'number',digits:3},{type:'boolean'},{type:'null'}]}));
  const secret = 'synthetic-response-secret';
  const badType = await related(secret);
  assert.equal(badType.schema_diag.reason, 'related_data_type');
  assert.equal(badType.schema_diag.data_type, 'string');
  assert.equal(badType.schema_diag.data_length, secret.length);
  assert(!JSON.stringify(badType).includes(secret));
  assert.equal((await related({period:'20261026|20261101', wordlist:[]})).schema_diag.reason, 'related_wordlist_shape');
  for (const mode of ['truncated_end', 'trailing_empty']) {
    const result = await run(mode);
    assert.equal(result.status, 'ok');
    assert.equal(result.rows.length, 15);
    assert(result.rows.every(row => row.date <= '2026-10-05' && row.search_index !== 0));
  }
  for (const [mode, reason] of [['end_after','end_date'], ['start_mismatch','start_date'], ['bad_word_type','word_type'], ['wrong_name','word'], ['point_count','point_count']]) {
    const result = await run(mode);
    assert.equal(result.status, 'response_schema');
    assert.equal(result.schema_diag.reason, reason);
    for (const secret of ['synthetic-secret', 'synthetic-word', 'abc1,2']) assert(!JSON.stringify(result).includes(secret));
  }
  console.log('Baidu in-page date tolerance and safe diagnostics passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
