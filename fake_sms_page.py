"""Loopback-only aiohttp SMS fixture; no upstream requests or saved credentials.

The PNG is generated in memory. DOM geometry matches SMS_SELECTORS without
calibrating them against a real page. Toggles deliberately use English labels
so inactive challenges do not trigger the production text probes.
"""
from aiohttp import web


PAGE = r'''<!doctype html><meta charset="utf-8"><title>Synthetic SMS login</title>
<style>
body {margin:24px;background:#fff;font:16px sans-serif} [hidden] {display:none!important}
[role=tab], button {cursor:pointer;padding:8px} input {height:28px;width:180px}
#options {margin:16px 0} #options input {width:18px;height:18px}
#captcha_container {position:absolute;left:80px;top:260px;width:320px;height:270px;background:#ebebeb}
#puzzle {position:absolute;top:0;left:0;width:320px;height:240px;pointer-events:none}
#captcha-verify-image {position:absolute;top:0;left:0;width:320px;height:180px;pointer-events:none}
.captcha_verify_img_slide {position:absolute;left:10px;top:60px;width:60px;height:60px;pointer-events:none}
.secsdk-captcha-drag-wrapper {position:absolute;left:10px;top:200px;width:300px;height:24px;touch-action:none}
.secsdk-captcha-drag-icon {position:absolute;left:0;top:0;width:40px;height:24px;background:#646464;touch-action:none}
#challenge-label {position:absolute;top:242px;left:5px}
</style>
<div id="tabs"><span role="tab" id="qr-tab">扫码登录</span> | <span role="tab" id="sms-tab">验证码登录</span></div>
<div id="qr">Synthetic QR placeholder</div>
<div id="options">
<label><input type="checkbox" id="rotation">Rotation</label>
<label><input type="checkbox" id="frequent">Rate limit</label>
<label><input type="checkbox" id="second">Second challenge</label>
</div>
<div id="form" hidden>
<input id="phone" type="tel" placeholder="手机号" autocomplete="off">
<button id="send">获取验证码</button><br><br>
<input id="code" autocomplete="one-time-code" placeholder="验证码">
<button id="submit">登录</button><div id="error"></div>
</div>
<div id="captcha_container" hidden data-challenge-id="synthetic-0">
<img id="puzzle" src="/puzzle.png"><div id="captcha-verify-image"></div>
<div class="captcha_verify_img_slide"></div><div class="secsdk-captcha-drag-wrapper">
<div class="secsdk-captcha-drag-icon"></div></div><span id="challenge-label"></span></div>
<div id="success" hidden>已登录</div>
<script>
const el = id => document.getElementById(id);
const handle = document.querySelector('.secsdk-captcha-drag-icon');
const target = 190, trackTravel = 260, puzzleTravel = 260;
window.audit = {drags:[], inputTrusted:[], sends:0, codeSubmits:0, tabs:0, passed:0};
// Only fixture controls/booleans enter query strings; never phone/code.
const config = new URLSearchParams(location.search);
for (const name of ['rotation','frequent','second']) el(name).checked = config.get(name) === '1';
const countdownSeconds = 4; // Hub's real >=60s resend guard is deliberately unchanged.
let serial = 0, phase = '', remaining = 0, timer = null, drag = null;
for (const id of ['phone','code']) el(id).addEventListener('input', e => audit.inputTrusted.push(e.isTrusted));
el('sms-tab').onclick = () => {audit.tabs++; el('qr').hidden = true; el('form').hidden = false;};
el('qr-tab').onclick = () => {el('qr').hidden = false; el('form').hidden = true;};
function challenge(stage, count) {
  phase = stage; remaining = count; serial++;
  el('captcha_container').hidden = false;
  el('captcha_container').dataset.challengeId = 'synthetic-'+serial;
  el('challenge-label').textContent = el('rotation').checked ? '旋转图片完成安全验证' : '拖动滑块完成拼图';
  handle.style.left = '0px';
}
function sent() {
  audit.sends++; let left = countdownSeconds;
  el('send').disabled = true; el('send').textContent = left+'秒后重发';
  clearInterval(timer); timer = setInterval(() => {
    left--; el('send').textContent = left > 0 ? left+'秒后重发' : '重新发送';
    if (!left) {clearInterval(timer); el('send').disabled = false;}
  }, 1000);
}
function success() {
  // All fixture state stays in DOM memory; session cookie has no expiry.
  document.cookie = 'sessionid=synthetic-sms-session; Path=/; SameSite=Strict';
  el('phone').value = ''; el('code').value = '';
  el('form').hidden = el('tabs').hidden = el('options').hidden = true;
  el('success').hidden = false; history.replaceState(null, '', '/success');
}
function loginResult() {
  if (config.get('post_code_error') === '1') el('error').textContent = '验证码已过期';
  else success();
}
el('send').onclick = () => {
  el('error').textContent = '';
  if (!/^1[3-9][0-9]{9}$/.test(el('phone').value)) return;
  if (el('frequent').checked) {el('error').textContent = '发送频繁'; return;}
  challenge('send', Number(config.get('send_challenges') || 1));
};
el('submit').onclick = () => {
  audit.codeSubmits++; el('error').textContent = '';
  const correct = el('code').value === '246810'; el('code').value = '';
  if (!correct) {el('error').textContent = '验证码错误'; return;}
  if (el('second').checked) challenge('login', Number(config.get('login_challenges') || 1));
  else loginResult();
};
handle.addEventListener('pointerdown', e => {
  drag = [{x:e.clientX,y:e.clientY,t:performance.now(),trusted:e.isTrusted}];
  if (e.isTrusted) handle.setPointerCapture(e.pointerId);
});
handle.addEventListener('pointermove', e => {
  if (!drag) return;
  drag.push({x:e.clientX,y:e.clientY,t:performance.now(),trusted:e.isTrusted});
  handle.style.left = Math.max(0,Math.min(trackTravel,e.clientX-drag[0].x))+'px';
});
handle.addEventListener('pointerup', e => {
  if (!drag) return;
  drag.push({x:e.clientX,y:e.clientY,t:performance.now(),trusted:e.isTrusted});
  const dx = (e.clientX-drag[0].x)*puzzleTravel/trackTravel;
  const duration = drag.at(-1).t-drag[0].t;
  const maxStep = Math.max(...drag.slice(1).map((p,i) => Math.hypot(p.x-drag[i].x,p.y-drag[i].y)));
  const trusted = drag.every(p => p.trusted);
  const smooth = drag.length >= 16 && duration >= 500 && duration <= 10000 && maxStep <= 12;
  const passed = trusted && smooth && Math.abs(dx-target) <= 5 && !el('rotation').checked;
  audit.drags.push({trusted,smooth,passed,dx,duration,maxStep,points:drag.length}); drag = null;
  if (!passed) {handle.style.left = '0px'; return;}
  audit.passed++;
  // Leave the old geometry stable until the queued release/capture completes.
  setTimeout(() => {
    if (--remaining > 0) challenge(phase, remaining);
    else {el('captcha_container').hidden = true; phase === 'send' ? sent() : loginResult();}
  }, 100);
});
</script>'''


def create_fake_app():
    from solver_fixture import gap_fixture
    capture, _ = gap_fixture()

    async def page(request):
        return web.Response(text=PAGE, content_type='text/html', headers={'Cache-Control': 'no-store'})

    async def puzzle(request):
        return web.Response(body=capture.png, content_type='image/png', headers={'Cache-Control': 'no-store'})

    app = web.Application()
    app.router.add_get('/login', page)
    app.router.add_get('/puzzle.png', puzzle)
    return app
