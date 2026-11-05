async (query) => {
  const fail = status => ({status});
  const schemaKeys = new Set(['status','data','uniqid','userIndexes','word','name','wordType','all','pc','wise','startDate','endDate','type','generalRatio','wordlist','wordGraph']);
  const digest = (value, depth = 0) => {
    const type = value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value;
    if (type === 'array') return {type, length:value.length, ...(value.length && depth < 3 ? {items:digest(value[0], depth)} : {})};
    if (type !== 'object' || depth >= 3) return {type};
    const keys = Object.create(null);
    for (const key of Object.keys(value)) if (schemaKeys.has(key)) keys[key] = digest(value[key], depth + 1);
    return {type, keys};
  };
  const typeOf = value => value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value;
  const safeKey = key => /^[A-Za-z_][A-Za-z0-9_]{0,31}$/.test(key) ? key : 'masked';
  const pattern = value => Array.from(value, ch => /[0-9]/.test(ch) ? '9' : /[A-Za-z]/.test(ch) ? 'a' : ch.codePointAt(0) > 127 ? 'C' : ch.charCodeAt(0) < 32 || ch.charCodeAt(0) === 127 ? 'C' : ch).slice(0, 64).join('');
  const shape = (value, depth = 0) => {
    const type = typeOf(value);
    if (depth >= 2) return {type};
    if (type === 'string') return {type, length:Array.from(value).length, pattern:pattern(value)};
    if (type === 'number') return {type, digits:(String(value).match(/[0-9]/g) || []).length};
    if (type === 'array') return {type, length:value.length, items:value.slice(0, 4).map(item => shape(item, depth + 1))};
    if (type === 'object') {
      const values = Object.create(null);
      for (const key of Object.keys(value).sort().slice(0, 8)) {
        const name = safeKey(key);
        if (!(name in values)) values[name] = shape(value[key], depth + 1);
      }
      return {type, keys:Object.keys(values).sort(), values};
    }
    return {type};
  };
  const entryKeys = data => {
    const entries = Object.create(null);
    if (Array.isArray(data?.wordlist)) for (const group of data.wordlist) {
      if (!Array.isArray(group?.wordGraph)) continue;
      for (const item of group.wordGraph) if (item && typeof item === 'object' && !Array.isArray(item)) {
        for (const key of Object.keys(item)) {
          const name = safeKey(key), type = typeOf(item[key]);
          entries[name] = entries[name] && entries[name] !== type ? 'mixed' : type;
        }
      }
    }
    return Object.fromEntries(Object.entries(entries).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0));
  };
  const schema = (value, diag = {}) => {
    const date = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : null;
    return {status:'response_schema', schema_digest:digest(value), schema_diag:{
      reason:diag.reason || 'index_shape', request_start:date(query.start_date), request_end:date(query.end_date),
      response_start:date(diag.response_start), response_end:date(diag.response_end),
      word_type:Number.isFinite(diag.word_type) ? diag.word_type : null, name_matches:diag.name_matches === true,
      ptbk_length:Number.isInteger(diag.ptbk_length) ? diag.ptbk_length : 0,
      decoded_points:Number.isInteger(diag.decoded_points) ? diag.decoded_points : null,
      data_length:Number.isInteger(diag.data_length) ? diag.data_length : null,
      data_type:diag.data_type || null, datelist_sent:query.related_date ? query.related_date.replaceAll('-', '') : 'none',
      period_returned:diag.period_returned === true, ...(diag.period_shape ? {period_shape:diag.period_shape} : {}), ...(diag.entry_keys ? {entry_keys:diag.entry_keys} : {})}};
  };
  if (location.origin !== query._origin) return fail('network_error');
  const parse = text => {
    let i = 0;
    const primitive = /(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/y;
    const ws = () => { while (/\s/.test(text[i] || '') && i < text.length) i++; };
    const string = () => {
      const start = i++;
      while (i < text.length) {
        if (text[i] === '\\') { i += 2; continue; }
        if (text[i++] === '"') return JSON.parse(text.slice(start, i));
      }
      throw Error();
    };
    const value = depth => {
      if (depth > 100) throw Error();
      ws();
      if (text[i] === '"') { string(); return; }
      if (text[i] === '{') {
        i++; ws(); const keys = new Set();
        if (text[i] === '}') { i++; return; }
        while (true) {
          ws(); if (text[i] !== '"') throw Error();
          const key = string(); if (keys.has(key)) throw Error(); keys.add(key);
          ws(); if (text[i++] !== ':') throw Error(); value(depth + 1); ws();
          const next = text[i++]; if (next === '}') return; if (next !== ',') throw Error();
        }
      }
      if (text[i] === '[') {
        i++; ws(); if (text[i] === ']') { i++; return; }
        while (true) { value(depth + 1); ws(); const next = text[i++]; if (next === ']') return; if (next !== ',') throw Error(); }
      }
      primitive.lastIndex = i;
      const match = primitive.exec(text);
      if (!match) throw Error(); i = primitive.lastIndex;
    };
    value(0); ws(); if (i !== text.length) throw Error();
    return JSON.parse(text);
  };
  const deadline = promise => Promise.race([promise, new Promise((_, reject) => setTimeout(() => reject(Error()), 25000))]);
  const get = async (path, headers = {}) => {
    const now = new Date(Date.now() + 8 * 3600000);
    if (now.toISOString().slice(0, 10) !== query._budget_day || (now.getUTCHours() === 23 && now.getUTCMinutes() === 59 && now.getUTCSeconds() >= 30)) return fail('baidu_daily_limit');
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 25000);
    try {
      const response = await fetch(path, {headers, credentials: 'same-origin', redirect: 'error', signal: controller.signal});
      if ((response.headers.get('content-type') || '').includes('html')) return fail('verification_required');
      if (!response.ok) return fail('http_error');
      const reader = response.body.getReader();
      const chunks = []; let size = 0;
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        size += value.length;
        if (size > 1048576) { controller.abort(); return fail('response_schema'); }
        chunks.push(value);
      }
      const bytes = new Uint8Array(size); let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
      const text = new TextDecoder('utf-8', {fatal: true}).decode(bytes);
      if (text.trimStart().startsWith('<')) return fail('verification_required');
      let body;
      try { body = parse(text); } catch (_) { return fail('response_schema'); }
      if (!body || !Number.isInteger(body.status)) return schema(body);
      if (body.status !== 0) return fail(({10000:'login_required',10001:'rate_limited',10002:'bad_request',10018:'abnormal_access'})[body.status] || 'business_error');
      if (!Object.prototype.hasOwnProperty.call(body, 'data')) return schema(body);
      return {status:'ok', data:body.data};
    } finally { clearTimeout(timer); }
  };
  try {
    if (query.kind === 'related') {
      const params = new URLSearchParams({'wordlist[]':query.keywords[0]});
      if (query.related_date) params.set('datelist', query.related_date.replaceAll('-', ''));
      const graph = await get('/api/WordGraph/multi?' + params);
      if (graph.status !== 'ok') return graph;
      const data = graph.data;
      const dataType = data === null ? 'null' : Array.isArray(data) ? 'array' : typeof data;
      if (dataType !== 'object') return schema(data, {reason:'related_data_type', data_type:dataType, data_length:dataType === 'string' ? data.length : null});
      const periodReturned = Object.prototype.hasOwnProperty.call(data, 'period');
      const keys = entryKeys(data), period = data.period;
      let dates;
      if (typeof period === 'string') {
        const match = /^(\d{4}(?:\d{4}|-\d{2}-\d{2}))(?:\||~|,| - |至)(\d{4}(?:\d{4}|-\d{2}-\d{2}))$/.exec(period);
        if (match) dates = match.slice(1);
      } else if (Array.isArray(period) && period.length === 2) dates = period;
      else if (period && typeof period === 'object') {
        for (const [first, last] of [['startDate','endDate'], ['start','end'], ['beginDate','endDate'], ['start_date','end_date'], ['begin','end']]) {
          if (Object.keys(period).length === 2 && Object.hasOwn(period, first) && Object.hasOwn(period, last)) { dates = [period[first], period[last]]; break; }
        }
      }
      const iso = date => {
        if (typeof date !== 'string') return null;
        const match = /^(\d{4})(?:(\d{2})(\d{2})|-(\d{2})-(\d{2}))$/.exec(date);
        return match && match[1] !== '0000' ? `${match[1]}-${match[2] || match[4]}-${match[3] || match[5]}` : null;
      };
      const [start, end] = dates ? dates.map(iso) : [null, null];
      const startMs = Date.parse(start + 'T00:00:00Z'), endMs = Date.parse(end + 'T00:00:00Z');
      if (!start || !end || !Number.isFinite(startMs) || !Number.isFinite(endMs) || new Date(startMs).toISOString().slice(0,10) !== start || new Date(endMs).toISOString().slice(0,10) !== end || (endMs - startMs) / 86400000 < 5 || (endMs - startMs) / 86400000 > 7) return schema(data, {reason:'related_period', data_type:dataType, period_returned:periodReturned, ...(periodReturned ? {period_shape:shape(period)} : {}), entry_keys:keys});
      if (!Array.isArray(data.wordlist) || data.wordlist.length !== 1 || !data.wordlist[0] || typeof data.wordlist[0] !== 'object' || Array.isArray(data.wordlist[0]) || !Array.isArray(data.wordlist[0].wordGraph) || data.wordlist[0].wordGraph.length > 100 || data.wordlist[0].wordGraph.some(item => !item || typeof item !== 'object' || Array.isArray(item) || typeof item.word !== 'string' || !item.word.trim() || item.word.length > 200)) return schema(data, {reason:'related_shape', data_type:dataType, period_returned:periodReturned, entry_keys:keys});
      return {status:'ok', meta:{entry_keys:keys}, data:{period:{start_date:start, end_date:end}, wordlist:[{wordGraph:data.wordlist[0].wordGraph.map(item => ({word:item.word}))}]}};
    }
    if (!window.Paris || typeof window.Paris.getAcsInstance !== 'function') return fail('signature_unavailable');
    let signature;
    try {
      signature = await deadline((async () => {
        const acs = await new Promise((resolve, reject) => window.Paris.getAcsInstance((err, instance) => err ? reject(err) : resolve(instance)));
        if (!acs || typeof acs.getSign !== 'function') throw Error();
        return new Promise((resolve, reject) => acs.getSign((err, sign) => err ? reject(err) : resolve(sign)));
      })());
      if (typeof signature !== 'string' || !signature) return fail('signature_unavailable');
    } catch (_) { return fail('signature_unavailable'); }
    const params = new URLSearchParams({word:JSON.stringify(query.keywords.map(name => [{name,wordType:1}])), area:'0', startDate:query.start_date, endDate:query.end_date});
    const index = await get('/api/SearchApi/index?' + params, {'Cipher-Text':signature});
    if (index.status !== 'ok') return index;
    const data = index.data;
    if (!data || typeof data !== 'object' || Array.isArray(data) || typeof data.uniqid !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(data.uniqid) || !Array.isArray(data.userIndexes) || data.userIndexes.length > query.keywords.length) return schema(data);
    const table = await get('/Interface/api/ptbk?' + new URLSearchParams({uniqid:data.uniqid}));
    if (table.status !== 'ok') return table;
    const ptbk = table.data;
    const info = {ptbk_length:typeof ptbk === 'string' ? ptbk.length : 0};
    if (typeof ptbk !== 'string' || !ptbk.length || ptbk.length > 256 || ptbk.length % 2) return schema(data, {...info, reason:'ptbk'});
    const half = ptbk.length / 2, left = ptbk.slice(0, half), right = ptbk.slice(half);
    if (new Set(left).size !== half || new Set(right).size !== half) return schema(data, {...info, reason:'ptbk'});
    const byWord = new Map();
    for (const item of data.userIndexes) {
      if (!item || typeof item !== 'object' || Array.isArray(item)) return schema(data, {...info, reason:'word'});
      let word;
      try { word = typeof item.word === 'string' ? parse(item.word) : item.word; } catch (_) { return schema(data, {...info, reason:'word'}); }
      if (!Array.isArray(word) || word.length !== 1 || !word[0] || typeof word[0] !== 'object' || Array.isArray(word[0])) return schema(data, {...info, reason:'word'});
      const wordInfo = {...info, word_type:word[0].wordType, name_matches:query.keywords.includes(word[0].name)};
      if (typeof word[0].name !== 'string' || !word[0].name || !wordInfo.name_matches || byWord.has(word[0].name)) return schema(data, {...wordInfo, reason:'word'});
      if (word[0].wordType !== 1) return schema(data, {...wordInfo, reason:'word_type'});
      byWord.set(word[0].name, item);
    }
    const start = Date.parse(query.start_date + 'T00:00:00Z'), end = Date.parse(query.end_date + 'T00:00:00Z');
    const days = (end - start) / 86400000 + 1, rows = [];
    for (const word of query.keywords) {
      const wordRows = [];
      for (const device of ['all','pc','wise']) {
      let tokens = Array(days).fill('');
      if (byWord.has(word)) {
        const series = byWord.get(word)[device];
        const detail = {...info, word_type:1, name_matches:true, response_start:series?.startDate, response_end:series?.endDate,
          data_length:typeof series?.data === 'string' ? series.data.length : null};
        // Long daily series can exceed the former seven-day-sized bound.
        if (!series || typeof series !== 'object' || Array.isArray(series) || (series.type === undefined ? 'day' : series.type) !== 'day' || typeof series.data !== 'string') return schema(data, {...detail, reason:'series_shape'});
        if (series.data.length > Math.min(65536, Math.max(6200, days * 64))) return schema(data, {...detail, reason:'data_length'});
        if (series.startDate !== query.start_date) return schema(data, {...detail, reason:'start_date'});
        const responseEnd = typeof series.endDate === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(series.endDate) ? Date.parse(series.endDate + 'T00:00:00Z') : NaN;
        if (!Number.isFinite(responseEnd) || new Date(responseEnd).toISOString().slice(0,10) !== series.endDate || responseEnd < start || responseEnd > end) return schema(data, {...detail, reason:'end_date'});
        let plain = '';
        for (const char of series.data) { const n = left.indexOf(char); if (n < 0) return schema(data, {...detail, reason:'token'}); plain += right[n]; }
        tokens = plain.split(',');
        if (tokens.length !== (responseEnd - start) / 86400000 + 1) return schema(data, {...detail, reason:'point_count', decoded_points:tokens.length});
        while (tokens.length && tokens[tokens.length - 1] === '') tokens.pop();
      }
      for (let i = 0; i < tokens.length; i++) {
        const token = tokens[i];
        if (token && !/^[0-9]{1,15}$/.test(token)) return schema(data, {...info, reason:'token', word_type:1, name_matches:true, decoded_points:tokens.length});
        wordRows.push({keyword:word,device,date:new Date(start + i * 86400000).toISOString().slice(0,10),search_index:token ? Number(token) : null,value_status:token ? 'available' : 'missing'});
      }
      }
      const count = Math.min(...['all','pc','wise'].map(device => wordRows.filter(row => row.device === device).length));
      rows.push(...wordRows.filter(row => Date.parse(row.date + 'T00:00:00Z') < start + count * 86400000));
    }
    return {status:'ok', rows};
  } catch (_) { return fail('network_error'); }
}
