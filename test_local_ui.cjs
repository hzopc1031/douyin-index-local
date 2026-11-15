// Offline UI logic against a minimal synthetic DOM; no browser or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor() { this.children = []; this.value = ''; this.disabled = false; this.textContent = ''; this.style = {}; }
  addEventListener() {}
  replaceChildren() { this.children = []; }
  append(child) { this.children.push(child); }
  setAttribute() {}
}

const elements = new Map();
const document = {
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
  createElement() { return new Element(); },
};
const api = {};
const context = vm.createContext({ document, pywebview: { api }, setInterval: () => 1, clearInterval: () => {} });
const html = fs.readFileSync('ui-batch.html', 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
vm.runInContext(script, context);
const element = id => document.getElementById(id);

(async () => {
  api.fetch_hotlist = async () => ({ ok: true, data: { fetched_at: 'synthetic', items: [
    { position: 1, word: '<img src=x onerror=alert(1)>', hot_value: null },
  ] } });
  await vm.runInContext('fetchHotlist()', context);
  assert.equal(element('hot-items').children[0].textContent, '1. <img src=x onerror=alert(1)> · 热度 —');
  assert.equal(element('hot-fetch').disabled, false);

  api.local_comments_start = async () => ({ ok: false, message: 'invalid' });
  await vm.runInContext('startComments()', context);
  assert.equal(element('comment-status').textContent, 'invalid');
  assert.equal(element('comment-start').disabled, false);

  api.local_comments_start = async () => ({ ok: true });
  api.local_comments_status = async () => ({ state: 'stopped', code: 'low_memory', out: '/synthetic' });
  await vm.runInContext('startComments()', context);
  assert.match(element('comment-status').textContent, /可用内存不足 2 GiB/);
  assert.match(element('comment-status').textContent, /\/synthetic/);
  assert.equal(element('comment-start').disabled, false);
  assert.equal(element('comment-stop').disabled, true);
  api.local_comments_status = async () => ({ state: 'stopped', code: 'partial', details: ['comment_template_missing'] });
  await vm.runInContext('pollComments()', context);
  assert.match(element('comment-status').textContent, /视频页面未提供可识别的评论请求/);
  console.log('Local UI synthetic checks passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
