'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let csrf = '', nonce = '', state = null, busy = false, gesture = null, frameId = '';
  const terminal = new Set(['succeeded', 'failed', 'expired', 'cancelled']);
  const messages = {ready: '已就绪。可发短信或切换扫码。', code_sent: '平台已接受发码请求，请查看短信并输入验证码。',
    submitting: '已提交，正在等待官方登录确认。', verification: '请完成下方安全验证。',
    succeeded: '登录成功，dyc 登录态已保存。', failed: '登录已停止，可能遇到验证或访问限制。请联系管理员查看状态。',
    expired: '会话已到期并释放。', cancelled: '会话已结束并释放。'};
  const errors = {send_interval: '发码间隔至少 60 秒。', send_hour_limit: '已达到每小时 3 次发码上限。',
    send_day_limit: '已达到北京时间当日 6 次发码上限。', stale_frame: '验证页面已变化，请刷新截图后重试。',
    frame_unavailable: '截图暂未就绪，请稍后刷新。', replayed_action: '操作已处理，请等待刷新状态。',
    replayed_code: '此验证码已提交过，请勿重复提交。', unauthorized: '口令已失效、已用过或不正确。',
    csrf: '连接来源校验失败，请使用完整私网网址。', busy: '正在处理上一项操作，请稍候。',
    wrong_state: '当前不能执行此操作；可切换登录方式或结束会话。',
    quota_unavailable: '发码配额检查失败，请联系管理员。', invalid_request: '请求格式不正确。'};
  const reasons = {challenge_detected: 'challenge_detected：检测到安全验证，已停止。',
    login_step_timeout: '操作超时，已停止。', browser_operation_failed: '页面操作失败，已停止。',
    login_failed: '登录准备或操作失败。', code_invalid: '验证码错误。', sms_unconfirmed: '发码前置检查或发送结果未确认。'};
  const steps = {profile_lock: '登录资源锁定', display_start: '启动登录环境',
    playwright_start: '启动浏览器', browser_launch: '启动浏览器', isolated_context: '准备登录页面',
    redirect_guard: '准备登录保护', navigate: '打开登录页面', login_entry: '准备登录页面',
    phone_input_ready: '准备手机号输入', observe_login: '登录状态检查', send_sms: '发码检查'};
  function clearInputs() { for (const id of ['token', 'phone', 'code']) $(id).value = ''; }
  function controls() {
    for (const id of ['unlock', 'send', 'submit', 'smsMode', 'qrMode', 'refresh', 'cancel'])
      $(id).disabled = busy || Boolean(gesture) || terminal.has(state?.state);
    $('retry').disabled = busy || !state?.retryable;
    if (state) {
      $('send').disabled ||= state.resend_in > 0 || !['ready', 'code_sent'].includes(state.state);
      $('submit').disabled ||= state.state !== 'code_sent';
    }
  }
  function render(data) {
    state = data; nonce = data.nonce;
    $('status').textContent = data.preparation_failed ? '手机登录准备失败。可手动重新开始。' : messages[data.state] || '正在准备。';
    $('retry').hidden = !data.retryable;
    $('modes').hidden = terminal.has(data.state);
    $('sms').hidden = data.mode !== 'sms' || terminal.has(data.state);
    $('picture').hidden = !data.failure_image && (!(data.state === 'verification' || data.mode === 'qr') || terminal.has(data.state));
    if (data.failure_image) { $('frame').src = 'data:image/png;base64,' + data.failure_image; $('hint').textContent = '检测到安全验证，已停止；截图已脱敏。'; $('refresh').hidden = true; } else $('refresh').hidden = false;
    $('details').textContent = terminal.has(data.state) ? '' : `${data.phone || ''} 剩余 ${data.expires_in} 秒`;
    if (data.state === 'code_sent') $('details').textContent += `；发码冷却 ${data.resend_in} 秒`;
    if (data.failure) $('details').textContent = `失败步骤：${steps[data.failure.step] || (data.preparation_failed ? '准备' : '登录操作')}；原因：${reasons[data.failure.reason] || '页面检查失败或访问受限。'}`;
    if (data.send_result && (data.state === 'code_sent' ||
        (data.state === 'failed' && data.send_result.outcome !== 'accepted'))) {
      $('status').textContent = data.send_result.description;
      $('details').textContent += `；HTTP ${data.send_result.http_status ?? '未捕获'}；业务码 ${data.send_result.business_code ?? '未捕获'}`;
    }
    if (data.state === 'failed' && (data.send_result || data.failure?.step === 'send_sms')) {
        const d = data.send_result?.diagnostic || data.failure?.send_diagnostic || {};
        const yes = v => v === true ? '是' : v === false ? '否' : '未知';
        const buttons = {countdown: '倒计时', other: '其他', '获取验证码': '获取验证码', '发送验证码': '发送验证码', '重新获取验证码': '重新获取验证码', '重新发送验证码': '重新发送验证码', '重新发送': '重新发送'};
        const hints = {none: '无', frequent: '发送过于频繁', phone_format: '手机号格式', security: '安全验证', network: '网络错误', other: '其他'};
        $('details').textContent += `；命中目标：${yes(d.hit)}；短信面板激活：${yes(d.panel_active)}；事件 pointerdown/mousedown/click：${['pointerdown', 'mousedown', 'click'].map(k => `${yes(d.events?.[k]?.arrived)}（可信：${yes(d.events?.[k]?.trusted)}）`).join('、')}；按钮：${buttons[d.button] || '其他'}；提示：${hints[d.hint] || '其他'}`;
    }
    controls();
    if (terminal.has(data.state)) {
      clearInputs(); if (!data.failure_image) $('frame').removeAttribute('src'); frameId = '';
      if (data.state === 'succeeded') { csrf = ''; nonce = ''; }
    }
  }
  async function post(path, data) {
    const requestAction = data.action;
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
        if (result.preparation_failed && result.csrf) {
          csrf = result.csrf;
          $('auth').hidden = true; $('login').hidden = false;
          render(result);
          return result;
        } else if (result.error === 'login_failed') {
          $('status').textContent = '手机登录准备失败，已停止。请联系管理员查看状态。';
          if (result.failure) $('details').textContent = `失败步骤：${result.failure.step}；原因：${result.failure.reason}`;
        }
        throw new Error((requestAction === 'send' ? '发码步骤被拒绝：' : '操作被拒绝：') + (errors[result.error] || '请求未通过检查。'));
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
    catch (error) { $('error').textContent = error.message.startsWith('操作') || error.message.startsWith('发码步骤被拒绝：') || Object.values(errors).includes(error.message)
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
  $('retry').onclick = () => run(async () => {
    $('status').textContent = '正在重新准备手机登录，可能需要约 90 秒。';
    clearInputs(); await action('retry');
  });
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
