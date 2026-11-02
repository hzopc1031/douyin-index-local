'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let csrf = '', nonce = '', state = null, busy = false, gesture = null, frameId = '';
  const terminal = new Set(['succeeded', 'failed', 'expired', 'cancelled']);
  const messages = {ready: '已就绪。可发短信或切换扫码。', code_sent: '已请求发码，请查看短信并输入验证码。',
    submitting: '已提交，正在等待官方登录确认。', verification: '请完成下方安全验证。',
    succeeded: '登录成功，dyc 登录态已保存。', failed: '登录已停止，可能遇到验证或访问限制。请联系管理员查看状态。',
    expired: '会话已到期并释放。', cancelled: '会话已结束并释放。'};
  const errors = {send_interval: '发码间隔至少 60 秒。', send_hour_limit: '已达到每小时 3 次发码上限。',
    send_day_limit: '已达到北京时间当日 6 次发码上限。', stale_frame: '验证页面已变化，请刷新截图后重试。',
    frame_unavailable: '截图暂未就绪，请稍后刷新。', replayed_action: '操作已处理，请等待刷新状态。',
    replayed_code: '此验证码已提交过，请勿重复提交。', unauthorized: '口令已失效、已用过或不正确。',
    csrf: '连接来源校验失败，请使用完整私网网址。', busy: '正在处理上一项操作，请稍候。',
    wrong_state: '当前不能执行此操作；可切换登录方式或结束会话。'};
  function clearInputs() { for (const id of ['token', 'phone', 'code']) $(id).value = ''; }
  function controls() {
    for (const id of ['unlock', 'send', 'submit', 'smsMode', 'qrMode', 'refresh', 'cancel'])
      $(id).disabled = busy || Boolean(gesture) || terminal.has(state?.state);
    if (state) {
      $('send').disabled ||= state.resend_in > 0 || !['ready', 'code_sent'].includes(state.state);
      $('submit').disabled ||= state.state !== 'code_sent';
    }
  }
  function render(data) {
    state = data; nonce = data.nonce;
    $('status').textContent = messages[data.state] || '正在准备。';
    $('sms').hidden = data.mode !== 'sms' || terminal.has(data.state);
    $('picture').hidden = !(data.state === 'verification' || data.mode === 'qr') || terminal.has(data.state);
    $('details').textContent = `${data.phone || ''} 剩余 ${data.expires_in} 秒；发码冷却 ${data.resend_in} 秒`;
    controls();
    if (terminal.has(data.state)) {
      clearInputs(); $('frame').removeAttribute('src'); frameId = ''; csrf = ''; nonce = '';
    }
  }
  async function post(path, data) {
    let raw = JSON.stringify(data);
    for (const key of Object.keys(data)) delete data[key];
    try {
      // /auth and mode changes can take 90 seconds. Keep the request pending;
      // busy suppresses polling until it completes, with no client-side retry.
      const pending = fetch(path, {method: 'POST', cache: 'no-store', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-Dyc-CSRF': csrf}, body: raw});
      raw = '';
      const response = await pending;
      const result = await response.json();
      if (!response.ok) {
        if (result.error === 'login_failed') {
          $('status').textContent = '手机登录准备失败，已停止。请联系管理员查看状态。';
          if (result.failure) $('details').textContent = `失败步骤：${result.failure.step}；原因：${result.failure.reason}`;
        }
        throw new Error(errors[result.error] || '操作未完成，请联系管理员查看状态。');
      }
      return result;
    } finally { raw = ''; }
  }
  async function frame() {
    const data = await post('/api', {action: 'frame'});
    render(data);
    if (data.image) {
      frameId = data.frame;
      $('frame').src = 'data:image/png;base64,' + data.image;
      $('hint').textContent = data.state === 'verification'
        ? (data.prompt || '请在图中拖动滑块或点选。文字和输入框已遮盖。')
        : '请用抖音 App 扫码；可在另一屏幕展示此二维码。';
    }
  }
  async function run(fn) {
    if (busy || gesture) return;
    busy = true; controls(); $('error').textContent = '';
    try { await fn(); }
    catch (error) { $('error').textContent = error.message.startsWith('操作') || Object.values(errors).includes(error.message)
      ? error.message : '连接中断。请联系管理员查看状态；已提交的操作不要重复发送。'; }
    finally { busy = false; controls(); }
  }
  async function action(action, extra = {}) {
    const result = await post('/api', {action, nonce, ...extra});
    render(result);
    if (!terminal.has(result.state) && (result.state === 'verification' || result.mode === 'qr')) await frame();
  }
  const noToken = document.body.dataset.noToken === 'true';
  const unlock = () => run(async () => {
    const data = noToken ? {} : {token: $('token').value}; clearInputs();
    const result = await post('/auth', data); csrf = result.csrf;
    $('auth').hidden = true; $('login').hidden = false; render(result);
  });
  $('unlock').onclick = unlock;
  if (noToken) {
    $('auth').hidden = true;
    $('status').textContent = '正在准备手机登录，可能需要约 90 秒，请保持页面打开。';
    $('expiry').textContent = '登录会话最长 10 分钟；遇到安全验证或访问限制会停止。';
    unlock();
  }
  $('send').onclick = () => run(async () => {
    const data = {phone: $('phone').value}; clearInputs(); await action('send', data);
  });
  $('submit').onclick = () => run(async () => {
    const data = {code: $('code').value}; clearInputs(); await action('code', data);
  });
  $('smsMode').onclick = () => run(() => action('mode', {mode: 'sms'}));
  $('qrMode').onclick = () => run(() => action('mode', {mode: 'qr'}));
  $('cancel').onclick = () => run(() => action('cancel'));
  $('refresh').onclick = () => run(frame);
  function point(event, start) {
    const rect = $('frame').getBoundingClientRect();
    return [Math.min(1, Math.max(0, (event.clientX-rect.left)/rect.width)),
      Math.min(1, Math.max(0, (event.clientY-rect.top)/rect.height)), Math.round(performance.now()-start)];
  }
  $('frame').onpointerdown = event => {
    if (busy || gesture || state?.state !== 'verification' || !frameId) return;
    event.preventDefault(); $('frame').setPointerCapture(event.pointerId);
    const start = performance.now(); const p = point(event, start); p[2] = 0;
    gesture = {start, points: [p], frame: frameId, id: event.pointerId};
    controls();
  };
  $('frame').onpointermove = event => {
    if (!gesture || gesture.id !== event.pointerId) return;
    const p = point(event, gesture.start);
    if (p[2] < 5000 && p[2] - gesture.points.at(-1)[2] >= 25 && gesture.points.length < 150) gesture.points.push(p);
  };
  $('frame').onpointerup = event => {
    if (!gesture || gesture.id !== event.pointerId) return;
    const saved = gesture; gesture = null;
    const p = point(event, saved.start);
    if (p[2] > 5000) { controls(); $('error').textContent = '拖动请在 5 秒内完成，然后刷新截图。'; return; }
    if (p[2] > saved.points.at(-1)[2]) saved.points.push(p);
    run(() => action('pointer', {frame: saved.frame, points: saved.points}));
  };
  $('frame').onpointercancel = () => { gesture = null; controls(); };
  window.addEventListener('pagehide', () => { clearInputs(); csrf = nonce = frameId = ''; $('frame').removeAttribute('src'); });
  setInterval(() => {
    if (!csrf || terminal.has(state?.state) || busy || gesture) return;
    run(async () => { render(await post('/api', {action: 'status'}));
      if (!terminal.has(state.state) && (state.state === 'verification' || state.mode === 'qr')) await frame(); });
  }, 2500);
})();
