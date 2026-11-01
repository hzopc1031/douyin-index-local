'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let csrf = '', nonce = '', state = null, busy = false, gesture = null, frameId = '';
  const terminal = new Set(['succeeded', 'failed', 'expired', 'cancelled']);
  const messages = {ready: '已就绪。可发短信或切换扫码。', code_sent: '已请求发码，请查看短信并输入验证码。',
    submitting: '已提交，正在等待官方登录确认。', verification: '请完成下方安全验证。',
    succeeded: '登录成功，dyc 登录态已保存。', failed: '登录未完成。请回终端检查或稍后重新启动。',
    expired: '会话已到期并释放。', cancelled: '会话已结束并释放。'};
  const errors = {send_interval: '发码间隔至少 60 秒。', send_hour_limit: '已达到每小时 3 次发码上限。',
    send_day_limit: '已达到北京时间当日 6 次发码上限。', stale_frame: '验证页面已变化，请刷新截图后重试。',
    frame_unavailable: '截图暂未就绪，请稍后刷新。', replayed_action: '操作已处理，请等待刷新状态。',
    replayed_code: '此验证码已提交过，请勿重复提交。', unauthorized: '口令已失效、已用过或不正确。',
    csrf: '连接来源校验失败，请使用终端显示的完整私网网址。', busy: '正在处理上一项操作，请稍候。',
    wrong_state: '当前不能执行此操作；可切换登录方式或结束会话。'};
  function clearInputs() { for (const id of ['token', 'phone', 'code']) $(id).value = ''; }
  function render(data) {
    state = data; nonce = data.nonce;
    $('status').textContent = messages[data.state] || '正在准备。';
    $('sms').hidden = data.mode !== 'sms' || terminal.has(data.state);
    $('picture').hidden = !(data.state === 'verification' || data.mode === 'qr') || terminal.has(data.state);
    $('details').textContent = `${data.phone || ''} 剩余 ${data.expires_in} 秒；发码冷却 ${data.resend_in} 秒；自动缺口尝试 ${data.auto_attempts}/3`;
    $('send').disabled = data.resend_in > 0 || !['ready', 'code_sent'].includes(data.state);
    $('submit').disabled = data.state !== 'code_sent';
    for (const id of ['smsMode', 'qrMode', 'cancel']) $(id).disabled = terminal.has(data.state);
    if (terminal.has(data.state)) {
      clearInputs(); $('frame').removeAttribute('src'); frameId = ''; csrf = ''; nonce = '';
    }
  }
  async function post(path, data) {
    let raw = JSON.stringify(data);
    for (const key of Object.keys(data)) delete data[key];
    try {
      const pending = fetch(path, {method: 'POST', cache: 'no-store', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-Dyc-CSRF': csrf}, body: raw});
      raw = '';
      const response = await pending;
      const result = await response.json();
      if (!response.ok) throw new Error(errors[result.error] || '操作未完成，请刷新状态或回终端重新启动。');
      return result;
    } finally { raw = ''; }
  }
  async function frame() {
    const data = await post('/api', {action: 'frame'});
    render(data);
    if (data.image) {
      frameId = data.frame;
      $('frame').src = 'data:image/png;base64,' + data.image;
      $('hint').textContent = data.state === 'verification' ? '请在图中拖动滑块或点选。文字和输入框已遮盖。' : '请用抖音 App 扫码；可在另一屏幕展示此二维码。';
    }
  }
  async function run(fn) {
    if (busy || gesture) return;
    busy = true; $('error').textContent = '';
    try { await fn(); }
    catch (error) { $('error').textContent = error.message.startsWith('操作') || Object.values(errors).includes(error.message)
      ? error.message : '连接中断。请回终端查看；已提交的操作不要重复发送。'; }
    finally { busy = false; }
  }
  async function action(action, extra = {}) {
    const result = await post('/api', {action, nonce, ...extra});
    render(result);
    if (!terminal.has(result.state) && (result.state === 'verification' || result.mode === 'qr')) await frame();
  }
  $('unlock').onclick = () => run(async () => {
    const data = {token: $('token').value}; clearInputs();
    const result = await post('/auth', data); csrf = result.csrf;
    $('auth').hidden = true; $('login').hidden = false; render(result);
  });
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
    if (p[2] > 5000) { $('error').textContent = '拖动请在 5 秒内完成，然后刷新截图。'; return; }
    if (p[2] > saved.points.at(-1)[2]) saved.points.push(p);
    run(() => action('pointer', {frame: saved.frame, points: saved.points}));
  };
  $('frame').onpointercancel = () => { gesture = null; };
  window.addEventListener('pagehide', () => { clearInputs(); csrf = nonce = frameId = ''; $('frame').removeAttribute('src'); });
  setInterval(() => {
    if (!csrf || terminal.has(state?.state) || busy || gesture) return;
    run(async () => { render(await post('/api', {action: 'status'}));
      if (!terminal.has(state.state) && (state.state === 'verification' || state.mode === 'qr')) await frame(); });
  }, 2500);
})();
