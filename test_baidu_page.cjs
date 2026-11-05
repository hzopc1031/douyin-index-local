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
  for (const period of [undefined, '', '20260230|20260306', '00000101|00000107', '20261001|20261020', '20261101|20261026']) {
    const result = await related({...valid, period});
    assert.equal(result.schema_diag.reason, 'related_period');
    assert.equal(result.schema_diag.period_returned, period !== undefined);
  }
  const secret = 'synthetic-response-secret';
  const badType = await related(secret);
  assert.equal(badType.schema_diag.reason, 'related_data_type');
  assert.equal(badType.schema_diag.data_type, 'string');
  assert.equal(badType.schema_diag.data_length, secret.length);
  assert(!JSON.stringify(badType).includes(secret));
  assert.equal((await related({period:'20261026|20261101', wordlist:[]})).schema_diag.reason, 'related_shape');
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
