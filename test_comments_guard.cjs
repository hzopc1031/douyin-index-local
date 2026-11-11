const assert = require('assert');
const vm = require('vm');
const {execFileSync} = require('child_process');
const guard = execFileSync('python3', ['-c', 'from dyc.hub_comments import GUARD_JS; print(GUARD_JS)'], {env:{...process.env, PYTHONPATH:'tools/douyin_comments'}, encoding:'utf8'});
let calls = 0;
class XHR {
  open(method, url) { this.url = url; }
  addEventListener(event, callback) { this.callback = callback; }
  send() { calls++; this.status = 200; this.responseText = '{"status_code":0}'; this.callback(); }
  abort() { this.aborted = true; }
}
const context = {URL, location:{href:'https://www.douyin.com/'}, sessionStorage:{getItem:()=>null, removeItem:()=>{}}, XMLHttpRequest:XHR};
context.window = context;
context.fetch = () => { calls++; return Promise.resolve({status:200, clone:()=>({text:()=>Promise.resolve('{"status_code":0}')})}); };
vm.runInNewContext(guard, context);
(async () => {
  const base = '/aweme/v1/web/comment/list/';
  await assert.rejects(context.fetch(base));
  await assert.rejects(context.fetch(new URL(base, context.location.href)));
  assert.equal(calls, 0);
  context.__dycArm = true;
  await context.fetch(new URL(base, context.location.href));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(JSON.parse(context.__dycResult).status, 200);
  assert.equal(context.__dycSent, true);
  const blocked = new XHR(); blocked.open('GET', base + 'reply/'); blocked.send();
  assert.equal(blocked.aborted, true);
  context.__dycArm = true; context.__dycRoot = '123';
  const reply = new XHR(); reply.open('GET', base + 'reply/?comment_id=123'); reply.send();
  assert.equal(JSON.parse(context.__dycResult).text, '{"status_code":0}');
  assert.equal(calls, 2);
  context.__dycArm = true; context.__dycRoot = null; context.__dycSent = false;
  await context.fetch(base, {signal: {aborted: true}});
  assert.equal(context.__dycSent, false);
  assert(!guard.includes('searchParams.delete'));
})().catch(error => { console.error(error); process.exitCode = 1; });
