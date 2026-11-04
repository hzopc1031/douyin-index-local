async (query) => {
  const fail = status => ({status});
  if (location.origin !== query._origin) return fail('network_error');
  const parse = text => {
    let i = 0;
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
      const match = /^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(text.slice(i));
      if (!match) throw Error(); i += match[0].length;
    };
    value(0); ws(); if (i !== text.length) throw Error();
    return JSON.parse(text);
  };
  const deadline = promise => Promise.race([promise, new Promise((_, reject) => setTimeout(() => reject(Error()), 25000))]);
  const get = async (path, headers = {}) => {
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
      if (!body || !Number.isInteger(body.status)) return fail('response_schema');
      if (body.status !== 0) return fail(({10000:'login_required',10001:'rate_limited',10002:'bad_request',10018:'abnormal_access'})[body.status] || 'business_error');
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
    if (!data || !/^[A-Za-z0-9_-]{1,128}$/.test(data.uniqid) || !Array.isArray(data.userIndexes) || data.userIndexes.length > query.keywords.length) return fail('response_schema');
    const table = await get('/Interface/api/ptbk?' + new URLSearchParams({uniqid:data.uniqid}));
    if (table.status !== 'ok') return table;
    const ptbk = table.data;
    if (typeof ptbk !== 'string' || !ptbk.length || ptbk.length > 256 || ptbk.length % 2) return fail('response_schema');
    const half = ptbk.length / 2, left = ptbk.slice(0, half), right = ptbk.slice(half);
    if (new Set(left).size !== half || new Set(right).size !== half) return fail('response_schema');
    const byWord = new Map();
    for (const item of data.userIndexes) {
      const word = typeof item.word === 'string' ? parse(item.word) : item.word;
      if (!Array.isArray(word) || word.length !== 1 || !word[0] || Object.keys(word[0]).sort().join(',') !== 'name,wordType' || word[0].wordType !== 1 || !query.keywords.includes(word[0].name) || byWord.has(word[0].name)) return fail('response_schema');
      byWord.set(word[0].name, item);
    }
    const start = Date.parse(query.start_date + 'T00:00:00Z'), end = Date.parse(query.end_date + 'T00:00:00Z');
    const days = (end - start) / 86400000 + 1, rows = [];
    for (const word of query.keywords) for (const device of ['all','pc','wise']) {
      let tokens = Array(days).fill('');
      if (byWord.has(word)) {
        const series = byWord.get(word)[device];
        if (!series || series.startDate !== query.start_date || series.endDate !== query.end_date || (series.type || 'day') !== 'day' || typeof series.data !== 'string' || series.data.length > 6200) return fail('response_schema');
        let plain = '';
        for (const char of series.data) { const n = left.indexOf(char); if (n < 0) return fail('response_schema'); plain += right[n]; }
        tokens = plain.split(',');
        if (tokens.length !== days) return fail('response_schema');
      }
      for (let i = 0; i < days; i++) {
        const token = tokens[i];
        if (token && !/^[0-9]{1,15}$/.test(token)) return fail('response_schema');
        rows.push({keyword:word,device,date:new Date(start + i * 86400000).toISOString().slice(0,10),search_index:token ? Number(token) : null,value_status:token ? 'available' : 'missing'});
      }
    }
    return {status:'ok', rows};
  } catch (_) { return fail('network_error'); }
}
