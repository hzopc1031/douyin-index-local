async (query) => {
  const fail = status => ({status});
  const schemaKeys = new Set(['status','data','uniqid','userIndexes','word','name','wordType','all','pc','wise','startDate','endDate','type','generalRatio']);
  const digest = (value, depth = 0) => {
    const type = value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value;
    if (type === 'array') return {type, length:value.length, ...(value.length && depth < 3 ? {items:digest(value[0], depth)} : {})};
    if (type !== 'object' || depth >= 3) return {type};
    const keys = Object.create(null);
    for (const key of Object.keys(value)) if (schemaKeys.has(key)) keys[key] = digest(value[key], depth + 1);
    return {type, keys};
  };
  const schema = value => ({status:'response_schema', schema_digest:digest(value)});
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
    if (typeof ptbk !== 'string' || !ptbk.length || ptbk.length > 256 || ptbk.length % 2) return schema(data);
    const half = ptbk.length / 2, left = ptbk.slice(0, half), right = ptbk.slice(half);
    if (new Set(left).size !== half || new Set(right).size !== half) return schema(data);
    const byWord = new Map();
    for (const item of data.userIndexes) {
      if (!item || typeof item !== 'object' || Array.isArray(item)) return schema(data);
      let word;
      try { word = typeof item.word === 'string' ? parse(item.word) : item.word; } catch (_) { return schema(data); }
      if (!Array.isArray(word) || word.length !== 1 || !word[0] || typeof word[0] !== 'object' || Array.isArray(word[0]) || typeof word[0].name !== 'string' || !word[0].name || word[0].wordType !== 1 || !query.keywords.includes(word[0].name) || byWord.has(word[0].name)) return schema(data);
      byWord.set(word[0].name, item);
    }
    const start = Date.parse(query.start_date + 'T00:00:00Z'), end = Date.parse(query.end_date + 'T00:00:00Z');
    const days = (end - start) / 86400000 + 1, rows = [];
    for (const word of query.keywords) for (const device of ['all','pc','wise']) {
      let tokens = Array(days).fill('');
      if (byWord.has(word)) {
        const series = byWord.get(word)[device];
        // Long daily series can exceed the former seven-day-sized bound.
        if (!series || typeof series !== 'object' || Array.isArray(series) || series.startDate !== query.start_date || series.endDate !== query.end_date || (series.type === undefined ? 'day' : series.type) !== 'day' || typeof series.data !== 'string' || series.data.length > Math.min(65536, Math.max(6200, days * 64))) return schema(data);
        let plain = '';
        for (const char of series.data) { const n = left.indexOf(char); if (n < 0) return schema(data); plain += right[n]; }
        tokens = plain.split(',');
        if (tokens.length !== days) return schema(data);
      }
      for (let i = 0; i < days; i++) {
        const token = tokens[i];
        if (token && !/^[0-9]{1,15}$/.test(token)) return schema(data);
        rows.push({keyword:word,device,date:new Date(start + i * 86400000).toISOString().slice(0,10),search_index:token ? Number(token) : null,value_status:token ? 'available' : 'missing'});
      }
    }
    return {status:'ok', rows};
  } catch (_) { return fail('network_error'); }
}
