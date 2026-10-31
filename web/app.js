/**
 * EdgeAgent 前端（原生 JS，无框架、无构建）
 *
 * 安全约定：
 * - 所有来自服务端/模型的文本一律用 textContent 写入，绝不使用 innerHTML；
 * - token 只存在内存与 sessionStorage，且从 URL 读取后立刻从地址栏清除；
 * - 断线时不做"自动重放"（避免重复执行命令），而是重连后回读 /api/state 恢复视图。
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);

  const state = {
    token: '',
    sessionId: '',
    mode: 'restricted',
    streaming: false,
    online: null,
    streamBroken: false,
    abortCtrl: null,
    streamBubble: null,
    cards: new Map(),
    knownConfirms: new Set(),
    sessions: [],
  };

  let modalResolve = null;

  /* ------------------------------------------------------------------ 基础工具 */
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function fmtClock() {
    const d = new Date();
    return d.toLocaleTimeString('zh-CN', { hour12: false });
  }

  function scrollDown() {
    const box = $('messages');
    box.scrollTop = box.scrollHeight;
  }

  /* ------------------------------------------------------------------ 弹窗 */
  function showModal({ title, text, requireCheck = false, okText = '确认' }) {
    $('modal-title').textContent = title;
    $('modal-text').textContent = text;
    $('modal-check-row').classList.toggle('hidden', !requireCheck);
    $('modal-check').checked = false;
    $('modal-ok').textContent = okText;
    $('modal-ok').disabled = requireCheck;
    $('modal').classList.remove('hidden');
    return new Promise((resolve) => {
      modalResolve = resolve;
    });
  }

  function closeModal(result) {
    $('modal').classList.add('hidden');
    if (modalResolve) {
      const fn = modalResolve;
      modalResolve = null;
      fn(result);
    }
  }

  /* ------------------------------------------------------------------ 网络 */
  async function api(path, options = {}) {
    const { method = 'GET', body } = options;
    const headers = { Authorization: 'Bearer ' + state.token };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const res = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: 'no-store',
    });
    if (res.status === 401) {
      requireToken('Token 无效或已失效，请重新输入');
      throw new Error('unauthorized');
    }
    if (!res.ok) {
      let detail = 'HTTP ' + res.status;
      try {
        const data = await res.json();
        if (data && data.detail) detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
      } catch (err) {
        /* 忽略解析失败 */
      }
      throw new Error(detail);
    }
    return res.json();
  }

  async function ping() {
    try {
      const res = await fetch('/healthz', { cache: 'no-store' });
      const data = res.ok ? await res.json() : null;
      if (data && data.version) $('ver').textContent = 'v' + data.version;
      setOnline(res.ok);
    } catch (err) {
      setOnline(false);
    }
  }

  function setOnline(ok) {
    if (state.online === ok) return;
    const wasOffline = state.online === false;
    state.online = ok;
    $('conn-dot').className = 'dot ' + (ok ? 'online' : 'offline');
    $('conn-text').textContent = ok ? '已连接' : '离线';
    if (!ok) {
      showBanner('与本地代理的连接已断开，正在自动重连…（恢复后会自动刷新状态）', true);
    } else if (wasOffline) {
      showBanner('连接已恢复，状态已刷新', false);
      if (state.streamBroken) {
        appendSystem('连接中断过：本轮执行可能仍在服务端继续，请查看右侧「最近命令」或点击中止。');
      }
      refreshState();
    }
  }

  let bannerTimer = null;
  function showBanner(text, sticky) {
    const box = $('banner');
    box.textContent = text;
    box.classList.remove('hidden');
    if (bannerTimer) clearTimeout(bannerTimer);
    if (!sticky) bannerTimer = setTimeout(() => box.classList.add('hidden'), 4000);
  }

  /* ------------------------------------------------------------------ Token */
  function requireToken(message) {
    $('gate').classList.remove('hidden');
    $('gate-error').textContent = message || '';
    $('gate-token').value = '';
    $('gate-token').focus();
  }

  function readToken() {
    const url = new URL(window.location.href);
    const fromUrl = url.searchParams.get('token');
    if (fromUrl) {
      state.token = fromUrl.trim();
      sessionStorage.setItem('edgeagent_token', state.token);
      url.searchParams.delete('token');
      window.history.replaceState(null, '', url.pathname + url.hash);
      return;
    }
    state.token = sessionStorage.getItem('edgeagent_token') || '';
  }

  /* ------------------------------------------------------------------ 消息渲染 */
  function appendMessage(role, text) {
    const wrap = el('div', 'msg msg-' + role);
    wrap.appendChild(el('div', 'msg-role', role === 'user' ? '我' : role === 'assistant' ? 'EdgeAgent' : '系统'));
    const bubble = el('div', 'bubble', text);
    wrap.appendChild(bubble);
    $('messages').appendChild(wrap);
    scrollDown();
    return bubble;
  }

  function appendUser(text) {
    return appendMessage('user', text);
  }

  function appendSystem(text) {
    return appendMessage('system', text);
  }

  function appendError(text) {
    return appendMessage('error', text);
  }

  function appendToken(delta) {
    if (!delta) return;
    if (!state.streamBubble) {
      const wrap = el('div', 'msg msg-assistant');
      wrap.appendChild(el('div', 'msg-role', 'EdgeAgent'));
      const bubble = el('div', 'bubble caret', '');
      wrap.appendChild(bubble);
      $('messages').appendChild(wrap);
      state.streamBubble = bubble;
    }
    state.streamBubble.textContent += delta;
    scrollDown();
  }

  function endStreamBubble() {
    if (state.streamBubble) {
      state.streamBubble.classList.remove('caret');
      state.streamBubble = null;
    }
  }

  function renderPlan(steps) {
    if (!Array.isArray(steps) || !steps.length) return;
    const card = el('div', 'plan-card');
    card.appendChild(el('h4', null, '执行计划'));
    steps.forEach((step, index) => {
      const row = el('div', 'plan-step');
      row.appendChild(el('span', 'plan-idx', String(index + 1) + '.'));
      const risk = step.risk || 'unknown';
      row.appendChild(el('span', 'risk-badge risk-' + risk, risk));
      row.appendChild(el('span', 'plan-command', step.command || ''));
      card.appendChild(row);
    });
    $('messages').appendChild(card);
    scrollDown();
  }

  function renderKnowledge(hits) {
    if (!Array.isArray(hits) || !hits.length) return;
    const card = el('div', 'plan-card');
    card.appendChild(el('h4', null, '本地知识库命中 ' + hits.length + ' 段'));
    hits.forEach((hit) => {
      const row = el('div', 'plan-step');
      const score = typeof hit.score === 'number' ? hit.score : 0;
      row.appendChild(el('span', 'risk-badge risk-low', score.toFixed(2)));
      const text = el('span', 'plan-command');
      text.textContent = (hit.source || '') + ' — ' + String(hit.snippet || '').slice(0, 90);
      row.appendChild(text);
      card.appendChild(row);
    });
    $('messages').appendChild(card);
    scrollDown();
  }

  function renderCommandCard(data) {    const card = el('div', 'cmd-card');
    card.dataset.commandId = data.id;

    const head = el('div', 'cmd-head');
    const risk = data.risk || 'medium';
    head.appendChild(el('span', 'risk-badge risk-' + risk, risk));
    head.appendChild(el('span', null, 'PowerShell'));
    const status = el('span', 'cmd-status', data.require_confirm ? '等待确认' : '已提交执行');
    head.appendChild(status);
    card.appendChild(head);

    const body = el('div', 'cmd-body');
    const code = el('div', 'cmd-code', data.command || '');
    body.appendChild(code);
    body.appendChild(el('div', 'cmd-reason', data.reason || ''));
    const meta = el('div', 'cmd-meta');
    meta.appendChild(el('span', null, '工作目录：' + (data.cwd || '—')));
    meta.appendChild(el('span', null, '超时：' + (data.timeout || 30) + 's'));
    meta.appendChild(el('span', null, '模式：' + (data.mode === 'full' ? '完全访问' : '限制访问')));
    body.appendChild(meta);

    if (data.require_confirm) {
      const actions = el('div', 'cmd-actions');
      const approve = el('button', 'approve', '确认执行');
      const reject = el('button', 'reject', '拒绝');
      approve.type = 'button';
      reject.type = 'button';
      approve.addEventListener('click', () => confirmCommand(data.id, true, status, actions));
      reject.addEventListener('click', () => confirmCommand(data.id, false, status, actions));
      actions.appendChild(approve);
      actions.appendChild(reject);
      body.appendChild(actions);
    }

    card.appendChild(body);
    $('messages').appendChild(card);
    state.cards.set(data.id, { card, status, body });
    scrollDown();
  }

  async function confirmCommand(commandId, approved, statusEl, actionsEl) {
    if (actionsEl) actionsEl.remove();
    if (statusEl) statusEl.textContent = approved ? '已确认，执行中…' : '已拒绝';
    try {
      await api('/api/confirm', { method: 'POST', body: { session_id: state.sessionId, command_id: commandId, approved } });
    } catch (err) {
      if (statusEl) statusEl.textContent = '确认失败：' + err.message;
    }
  }

  function fillResult(data) {
    const entry = state.cards.get(data.id);
    if (!entry) return;
    if (entry.status) {
      const status = data.timed_out ? '超时终止' : 'exit=' + data.exit_code;
      entry.status.textContent = status + ' · ' + data.duration_ms + 'ms';
    }
    const out = el('div', 'output');
    out.appendChild(el('div', 'output-label', 'stdout' + (data.truncated ? '（已截断）' : '')));
    const preOut = el('pre', null, data.stdout || '(空)');
    out.appendChild(preOut);
    if (data.stderr) {
      const errLabel = el('div', 'output-label', 'stderr');
      errLabel.classList.add('tag-warn');
      out.appendChild(errLabel);
      const preErr = el('pre', 'err', data.stderr);
      out.appendChild(preErr);
    }
    entry.card.appendChild(out);
    scrollDown();
  }

  function markCard(commandId, text, kind) {
    const entry = state.cards.get(commandId);
    if (!entry || !entry.status) return;
    entry.status.textContent = text;
    if (kind === 'error') entry.status.classList.add('tag-warn');
    const actions = entry.body.querySelector('.cmd-actions');
    if (actions) actions.remove();
  }

  function handleError(data) {
    const code = data.code || 'error';
    if (code === 'parse_retry' || code === 'parse_warning') {
      appendSystem(data.message);
      return;
    }
    const payload = data.data || {};
    if (payload.id) {
      markCard(payload.id, code === 'guard_denied' ? '已被安全策略拒绝' : '未执行', 'error');
    }
    appendError(data.message || '发生未知错误');
  }

  function finalizeTurn(data) {
    endStreamBubble();
    const summary = (data && data.summary) || '本轮结束';
    if (data && data.awaiting_user) {
      appendMessage('assistant', summary);
    } else {
      appendMessage('done', summary);
    }
    setBusy(false);
    refreshState();
    loadSessions();
  }

  /* ------------------------------------------------------------------ 发送/流式 */
  function setBusy(busy) {
    $('btn-send').disabled = busy;
    $('btn-abort').disabled = !busy;
    $('input').disabled = busy;
  }

  async function send(text) {
    if (state.streaming || !text.trim()) return;
    state.streaming = true;
    state.streamBroken = false;
    setBusy(true);
    endStreamBubble();
    appendUser(text);
    $('input').value = '';
    autoGrow();

    const body = { session_id: state.sessionId, message: text, mode: state.mode };
    if (state.mode === 'full') body.confirm_full_access = true;

    const controller = new AbortController();
    state.abortCtrl = controller;
    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + state.token },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (res.status === 401) {
        requireToken('Token 无效或已失效，请重新输入');
        return;
      }
      if (!res.ok) {
        let detail = 'HTTP ' + res.status;
        try {
          const data = await res.json();
          if (data && data.detail) detail = data.detail;
        } catch (err) {
          /* ignore */
        }
        throw new Error(detail);
      }
      await readStream(res);
    } catch (err) {
      if (err.name === 'AbortError') {
        appendSystem('已取消本地连接（服务端可能仍在收尾，请以状态面板为准）');
      } else {
        state.streamBroken = true;
        appendError('请求失败：' + (err.message || err));
      }
    } finally {
      state.streaming = false;
      state.abortCtrl = null;
      endStreamBubble();
      setBusy(false);
      refreshState();
      loadSessions();
    }
  }

  async function readStream(res) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let index = buffer.indexOf('\n\n');
      while (index >= 0) {
        const chunk = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        handleChunk(chunk);
        index = buffer.indexOf('\n\n');
      }
    }
  }

  function handleChunk(chunk) {
    for (const line of chunk.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed.startsWith('data:')) continue; // 忽略 ": ping" 心跳
      const payload = trimmed.slice(5).trim();
      if (!payload) continue;
      let event;
      try {
        event = JSON.parse(payload);
      } catch (err) {
        continue;
      }
      try {
        handleEvent(event);
      } catch (err) {
        // 单个事件渲染失败不能中断整个 SSE 读取循环，
        // 否则后面的事件（包括确认请求）全部收不到，界面看起来就是"卡住"。
        console.error('[edgeagent] 事件渲染失败', event && event.type, err);
      }
    }
  }

  function handleEvent(event) {
    const data = event.data || {};
    switch (event.type) {
      case 'token':
        appendToken(data.delta);
        break;
      case 'plan':
        endStreamBubble();
        renderPlan(data.steps);
        break;
      case 'command':
        endStreamBubble();
        renderCommandCard(data);
        break;
      case 'await_confirm':
        state.knownConfirms.add(data.id);
        // 立即显示固定确认条，不等下一次轮询（8 秒太久）
        renderPendingBar([
          {
            id: data.id,
            command: data.command,
            risk: data.risk,
            reason: data.reason,
          },
        ]);
        // 兜底：若上一帧 command 因渲染异常没画出卡片，这里补一张，
        // 否则用户会看到"永远在等"却没有任何按钮可点。
        if (!state.cards.has(data.id)) {
          renderCommandCard({
            id: data.id,
            command: data.command,
            risk: data.risk,
            reason: data.reason,
            cwd: data.cwd || '—',
            timeout: data.timeout || 30,
            require_confirm: true,
            mode: state.mode,
          });
        }
        break;
      case 'knowledge':
        endStreamBubble();
        renderKnowledge(data.hits);
        break;
      case 'result':
        fillResult(data);
        break;
      case 'error':
        handleError(data);
        break;
      case 'mode_changed':
        applyMode(data.mode, false);
        break;
      case 'done':
        finalizeTurn(data);
        break;
      default:
        break;
    }
  }

  /* ------------------------------------------------------------------ 模式 */
  function applyMode(mode, fromUser) {
    const next = mode === 'full' ? 'full' : 'restricted';
    const changed = next !== state.mode;
    state.mode = next;
    document.querySelectorAll('.mode-btn').forEach((btn) => {
      btn.classList.toggle('is-active', btn.dataset.mode === next);
    });
    if (changed && !fromUser) {
      appendSystem(
        next === 'full'
          ? '已切换到完全访问模式。'
          : '已回到限制访问模式（切换或闲置超时自动降级），所有命令将再次受白名单与目录限制。'
      );
    }
  }

  async function switchMode(target) {
    if (target === state.mode) return;
    if (target === 'full') {
      const ok = await showModal({
        title: '切换到完全访问模式',
        text:
          '完全访问模式允许更宽范围的 PowerShell 命令：\n\n' +
          '· 每条命令执行前都需要你点击“确认执行”\n' +
          '· 格式化磁盘、关机重启、强删系统目录、修改 Defender、提权、下载执行远程脚本等命令会被永久拒绝\n' +
          '· 闲置 15 分钟或会话重置会自动退回限制访问模式\n\n' +
          '请确认你了解风险并会逐条审阅命令。',
        requireCheck: true,
        okText: '我了解风险，切换',
      });
      if (!ok) {
        applyMode(state.mode, true);
        return;
      }
      try {
        await api('/api/mode', { method: 'POST', body: { session_id: state.sessionId, mode: 'full', confirm_full_access: true } });
        applyMode('full', true);
        appendSystem('已切换到完全访问模式（本次切换已写入审计日志）。');
      } catch (err) {
        appendError('切换失败：' + err.message);
      }
    } else {
      try {
        await api('/api/mode', { method: 'POST', body: { session_id: state.sessionId, mode: 'restricted' } });
        applyMode('restricted', true);
        appendSystem('已切回限制访问模式。');
      } catch (err) {
        appendError('切换失败：' + err.message);
      }
    }
  }

  /* ------------------------------------------------------------------ 会话 */
  async function loadSessions() {
    try {
      const data = await api('/api/sessions');
      state.sessions = data.sessions || [];
      renderSessions();
    } catch (err) {
      /* 静默失败，避免离线时刷屏 */
    }
  }

  function renderSessions() {
    const box = $('session-list');
    box.textContent = '';
    if (!state.sessions.length) {
      box.appendChild(el('div', 'session-meta', '暂无会话'));
      return;
    }
    state.sessions.forEach((session) => {
      const btn = el('button', 'session-item' + (session.session_id === state.sessionId ? ' is-active' : ''));
      btn.type = 'button';
      btn.appendChild(el('span', 'session-title', session.title || session.session_id));
      btn.appendChild(el('span', 'session-meta', (session.mode === 'full' ? '完全访问' : '限制访问') + ' · ' + (session.messages || 0) + ' 条'));
      btn.addEventListener('click', () => selectSession(session.session_id));
      box.appendChild(btn);
    });
  }

  async function selectSession(sessionId) {
    if (state.streaming) {
      showBanner('当前有任务在执行，请先中止后再切换会话', true);
      return;
    }
    state.sessionId = sessionId;
    localStorage.setItem('edgeagent_session', sessionId);
    clearMessages();
    await loadHistory(sessionId);
    applyMode(state.mode, true);
    await refreshState();
    loadSessions();
  }

  async function loadHistory(sessionId) {
    if (!sessionId) return;
    try {
      const data = await api('/api/session/history?session_id=' + encodeURIComponent(sessionId));
      applyMode(data.mode || 'restricted', true);
      (data.messages || []).forEach((msg) => {
        if (msg.kind === 'correction') return;
        const content = String(msg.content || '');
        if (msg.role === 'user') {
          if (content.startsWith('【系统回执】')) {
            appendSystem(content.replace('【系统回执】', '系统回执：'));
          } else if (content.startsWith('【命令执行结果】')) {
            const first = content.split('\n').slice(0, 4).join(' · ');
            appendSystem('执行结果：' + first.replace('【命令执行结果】', ''));
          } else {
            appendUser(content);
          }
        } else if (msg.role === 'assistant') {
          appendMessage('assistant', content);
        }
      });
    } catch (err) {
      appendError('加载历史失败：' + err.message);
    }
  }

  function clearMessages() {
    $('messages').textContent = '';
    state.cards.clear();
    state.knownConfirms.clear();
    state.streamBubble = null;
  }

  async function newSession() {
    try {
      const data = await api('/api/session/reset', { method: 'POST', body: { session_id: null } });
      state.sessionId = data.session.session_id;
      localStorage.setItem('edgeagent_session', state.sessionId);
      clearMessages();
      applyMode('restricted', true);
      appendSystem('已新建会话，默认使用限制访问模式。');
      await loadSessions();
      await refreshState();
    } catch (err) {
      appendError('新建会话失败：' + err.message);
    }
  }

  async function resetSession() {
    const ok = await showModal({
      title: '重置当前会话',
      text: '将清空对话历史并回到限制访问模式，正在执行的命令会被中止。确定继续？',
      okText: '重置',
    });
    if (!ok) return;
    try {
      const data = await api('/api/session/reset', { method: 'POST', body: { session_id: state.sessionId } });
      state.sessionId = data.session.session_id;
      localStorage.setItem('edgeagent_session', state.sessionId);
      clearMessages();
      applyMode('restricted', true);
      appendSystem('会话已重置。');
      await loadSessions();
      await refreshState();
    } catch (err) {
      appendError('重置失败：' + err.message);
    }
  }

  async function abort() {
    if (!state.sessionId) return;
    $('btn-abort').disabled = true;
    try {
      await api('/api/abort', { method: 'POST', body: { session_id: state.sessionId, kill_process: true } });
      appendSystem('已发送中止请求（挂起的确认被拒绝，运行中的进程树已尝试终止）。');
    } catch (err) {
      appendError('中止失败：' + err.message);
      $('btn-abort').disabled = false;
    }
  }

  /* ------------------------------------------------------------------ 状态面板 */
  async function refreshState() {
    if (!state.sessionId || !state.token) return;
    try {
      const data = await api('/api/state?session_id=' + encodeURIComponent(state.sessionId));
      renderState(data);
      if (data.mode && data.mode !== state.mode) applyMode(data.mode, false);
      renderPending(data.pending_confirms || []);
      renderPendingBar(data.pending_confirms || []);
    } catch (err) {
      /* 离线时忽略 */
    }
  }

  function renderState(data) {
    $('st-cwd').textContent = data.cwd || '—';
    const allowed = $('st-allowed');
    allowed.textContent = '';
    (data.allowed_dirs || []).forEach((dir) => allowed.appendChild(el('li', 'mono', dir)));

    const system = data.system || {};
    const dl = $('st-system');
    dl.textContent = '';
    const disks = Array.isArray(system.disks) ? system.disks : [];
    const nets = Array.isArray(system.network) ? system.network : [];
    const topProcs = (system.top_processes && system.top_processes.by_cpu) || [];
    const rows = [
      ['系统', system.os || '—'],
      ['CPU', (system.cpu === null || system.cpu === undefined ? '采样中' : system.cpu + '%') + ' / ' + (system.cpu_count || '?') + ' 核'],
      ['内存', system.mem === null || system.mem === undefined ? '—' : system.mem + '%'],
      ['磁盘', system.disk ? system.disk.free_gb + ' GB 可用 / ' + system.disk.total_gb + ' GB' : '—'],
      ['盘符', disks.length ? disks.map((d) => d.drive + ' 剩 ' + d.free_percent + '%').join(' · ') : '—'],
      ['网卡', nets.length ? nets.map((n) => (n.interface || '?') + ' ' + (n.ipv4 || '') + ' → ' + (n.gateway || '')).join(' · ') : '—'],
      ['Top 进程', topProcs.length ? topProcs.slice(0, 3).map((p) => (p.name || '?') + ' ' + (p.cpu_seconds || 0) + 's').join(' · ') : '—'],
      ['时间', system.time || '—'],
    ];
    rows.forEach(([k, v]) => {
      dl.appendChild(el('dt', null, k));
      dl.appendChild(el('dd', null, v));
    });

    const recent = $('st-recent');
    recent.textContent = '';
    (data.recent_commands || []).slice().reverse().forEach((cmd) => {
      const li = el('li');
      li.appendChild(el('span', 'rcmd', cmd.command || ''));
      li.appendChild(el('span', 'rmeta', (cmd.risk || '?') + ' · ' + (cmd.status || '') + ' · ' + (cmd.ts || '')));
      recent.appendChild(li);
    });
    if (!(data.recent_commands || []).length) recent.appendChild(el('li', 'rmeta', '暂无'));
  }

  let pendingCurrent = null;

  function renderPendingBar(pending) {
    const bar = $('pending-bar');
    const list = pending || [];
    if (!list.length) {
      bar.hidden = true;
      pendingCurrent = null;
      return;
    }
    const item = list[0];
    pendingCurrent = item.id;
    $('pending-tag').textContent = (item.risk || 'medium') + ' 待确认';
    $('pending-cmd').textContent = item.command || '';
    $('pending-cmd').title = (item.reason || '') + '\n\n' + (item.command || '');
    bar.hidden = false;
  }

  async function decidePending(approved) {
    if (!pendingCurrent) return;
    const commandId = pendingCurrent;
    const approve = $('pending-approve');
    const reject = $('pending-reject');
    approve.disabled = true;
    reject.disabled = true;
    try {
      await api('/api/confirm', {
        method: 'POST',
        body: { session_id: state.sessionId, command_id: commandId, approved },
      });
      $('pending-bar').hidden = true;
      pendingCurrent = null;
      appendSystem(approved ? '已确认执行该命令。' : '已拒绝该命令，模型会尝试更安全的方案。');
    } catch (err) {
      if (err.message !== 'unauthorized') appendError('确认失败：' + err.message);
    } finally {
      approve.disabled = false;
      reject.disabled = false;
    }
  }

  function renderPending(pending) {
    pending.forEach((item) => {
      // 用"卡片是否真的在 DOM 里"判断，而不是只看 knownConfirms。
      // 否则一旦 command 帧渲染失败、await_confirm 又提前登记了 id，
      // 这里就会永远跳过，用户卡在"等待确认"却无按钮可点。
      if (state.cards.has(item.id)) return;
      state.knownConfirms.add(item.id);
      renderCommandCard({
        id: item.id,
        command: item.command,
        risk: item.risk,
        reason: item.reason,
        cwd: item.cwd || '—',
        timeout: item.timeout || 30,
        require_confirm: true,
        mode: state.mode,
      });
      appendSystem('恢复了一条此前挂起的确认请求（可能是断线重连前发出的）。');
    });
  }

  /* ------------------------------------------------------------------ 输入框 */
  function autoGrow() {
    const input = $('input');
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 160) + 'px';
  }

  /* ------------------------------------------------------------------ 初始化 */
  /* ------------------------------------------------------------------ AI 服务商 */
  const providerState = { data: null, kind: 'openai' };

  function providerLabelText(cur) {
    if (!cur) return 'AI 服务商';
    if (cur.kind === 'mock') return '内置模型（离线）';
    if (cur.kind === 'web') return (cur.preset_label || cur.preset) + ' · 网页版';
    return (cur.preset_label || cur.preset) + ' · ' + (cur.model || '');
  }

  function renderProviderBadge() {
    const cur = providerState.data && providerState.data.current;
    $('provider-label').textContent = providerLabelText(cur);
    const dot = $('provider-dot');
    dot.className = 'provider-dot';
    if (!cur) return;
    if (cur.kind === 'mock') dot.classList.add('warn');
    else if (cur.kind === 'web') dot.classList.add('ok');
    else dot.classList.add(cur.api_key_configured ? 'ok' : 'off');
  }

  async function loadProviders() {
    try {
      providerState.data = await api('/api/providers');
      if (providerState.data && providerState.data.current) {
        providerState.kind = providerState.data.current.kind;
      }
    } catch (err) {
      if (err.message === 'unauthorized') return;
      providerState.data = null;
    }
    renderProviderBadge();
  }

  function setProviderStatus(text, kind) {
    const node = $('pp-status');
    node.textContent = text || '';
    node.className = 'pp-status' + (kind ? ' ' + kind : '');
  }

  function providerItem(opts) {
    const btn = el('button', 'pp-item');
    btn.type = 'button';
    if (opts.current) btn.classList.add('is-current');
    if (opts.disabled) btn.disabled = true;

    const main = el('div', 'pp-main');
    const name = el('div', 'pp-name');
    name.appendChild(el('span', null, opts.title));
    if (opts.tag) name.appendChild(el('span', 'pp-tag ' + (opts.tagKind || ''), opts.tag));
    main.appendChild(name);
    if (opts.sub) main.appendChild(el('div', 'pp-sub', opts.sub));
    btn.appendChild(main);
    if (opts.current) btn.appendChild(el('span', 'pp-tag ok', '使用中'));
    btn.addEventListener('click', opts.onClick);
    return btn;
  }

  function renderProviderList() {
    const list = $('pp-list');
    list.textContent = '';
    const data = providerState.data;
    const cur = data && data.current;
    const kind = providerState.kind;

    if (!data) {
      list.appendChild(el('div', 'pp-empty', '无法读取服务商列表，请检查服务是否在线'));
      return;
    }

    if (kind === 'openai') {
      data.presets.forEach((preset) => {
        const current = cur && cur.kind === 'openai' && cur.preset === preset.id;
        const hasKey = preset.api_key_configured;
        list.appendChild(
          providerItem({
            title: preset.label,
            sub: preset.note || preset.base_url || '自定义接口地址',
            tag: hasKey ? '已配密钥' : preset.api_key_env,
            tagKind: hasKey ? 'ok' : 'warn',
            current,
            onClick: () => chooseProvider('openai', preset.id),
          })
        );
      });
    } else if (kind === 'web') {
      // 网页版是抓页面，稳定性天然不如 API，这里明说，避免被当成主力
      list.appendChild(
        el('div', 'pp-note', '网页版直接驱动浏览器里的对话页，不需要密钥。站点改版后可能需要更新适配器。')
      );
      if (!data.web_available) {
        const stub = data.web_driver || {};
        const why = stub.websockets === false ? '缺少 websockets 依赖' : '找不到 Microsoft Edge';
        list.appendChild(el('div', 'pp-empty', '网页版驱动不可用：' + why + '。'));
      }
      const sites = data.web_sites || [];
      if (!sites.length) {
        list.appendChild(el('div', 'pp-empty', '暂无可用的网页版站点'));
      }
      sites.forEach((site) => {
        const current = cur && cur.kind === 'web' && cur.preset === site.id;
        list.appendChild(
          providerItem({
            title: site.label,
            sub: site.url,
            tag: current ? '已选' : '免密钥',
            tagKind: current ? 'ok' : '',
            current,
            disabled: !data.web_available,
            onClick: () => chooseProvider('web', site.id),
          })
        );
      });
    } else {
      list.appendChild(
        providerItem({
          title: '内置模型（离线）',
          sub: '脚本化回复，只用于验证界面与流程，不产生真实智能',
          tag: cur && cur.kind === 'mock' ? '使用中' : '',
          tagKind: 'warn',
          current: cur && cur.kind === 'mock',
          onClick: () => chooseProvider('mock', 'mock'),
        })
      );
    }

    const loginBtn = $('pp-web-login');
    loginBtn.hidden = kind !== 'web' || !data.web_available;
    loginBtn.textContent = '打开网页登录';
    loginBtn.dataset.stage = 'start';
  }

  async function chooseProvider(kind, preset, model) {
    setProviderStatus('切换中…', 'busy');
    try {
      const res = await api('/api/provider', {
        method: 'POST',
        body: { kind, preset, model: model || null },
      });
      providerState.data = providerState.data || {};
      providerState.data.current = res.current;
      providerState.kind = res.current.kind;
      renderProviderBadge();
      renderProviderList();
      if (res.warning) {
        setProviderStatus(res.warning, 'err');
        appendSystem('已切换 AI 服务商，但还缺少密钥：' + res.warning);
      } else {
        setProviderStatus('已切换到 ' + providerLabelText(res.current), 'ok');
        appendSystem('AI 服务商已切换为：' + providerLabelText(res.current));
      }
    } catch (err) {
      if (err.message === 'unauthorized') return;
      setProviderStatus(err.message, 'err');
    }
  }

  async function testProvider() {
    setProviderStatus('正在测试连接…', 'busy');
    $('pp-test').disabled = true;
    try {
      const res = await api('/api/provider/test', { method: 'POST', body: {} });
      if (res.ok) {
        setProviderStatus('连接正常 · ' + res.latency_ms + 'ms · ' + (res.sample || ''), 'ok');
      } else {
        setProviderStatus(res.message || '连接失败', 'err');
      }
    } catch (err) {
      if (err.message === 'unauthorized') return;
      setProviderStatus(err.message, 'err');
    } finally {
      $('pp-test').disabled = false;
    }
  }

  async function webLogin() {
    const btn = $('pp-web-login');
    const start = btn.dataset.stage !== 'finish';
    setProviderStatus(start ? '正在打开浏览器，请稍候…' : '正在保存登录态…', 'busy');
    btn.disabled = true;
    try {
      const res = await api('/api/web/login', { method: 'POST', body: { start } });
      if (start) {
        setProviderStatus(res.message || '浏览器已打开', 'ok');
        btn.dataset.stage = 'finish';
        btn.textContent = '我已完成登录';
      } else if (res.logged_in) {
        setProviderStatus(res.message || '已登录', 'ok');
        btn.dataset.stage = 'done';
        btn.textContent = '已登录';
        appendSystem('网页版已就绪：' + (res.message || '') + ' 现在可以直接发消息，模型回复从网页抓取。');
      } else {
        setProviderStatus(res.message || '未检测到登录状态', 'err');
        btn.dataset.stage = 'finish';
        btn.textContent = '我已完成登录';
      }
    } catch (err) {
      if (err.message === 'unauthorized') return;
      setProviderStatus(err.message, 'err');
    } finally {
      btn.disabled = false;
    }
  }

  async function boot() {
    readToken();
    if (!state.token) {
      requireToken('');
      return;
    }
    $('gate').classList.add('hidden');
    try {
      const health = await api('/api/health');
      $('ver').textContent = 'v' + health.version;
      if (!health.llm_configured) {
        appendSystem('服务端未配置大模型密钥（.env 中的 LLM_API_KEY），聊天会被拒绝。请配置后重启代理。');
      }
    } catch (err) {
      if (err.message === 'unauthorized') return;
      appendError('健康检查失败：' + err.message);
    }

    await loadProviders();
    await loadSessions();
    const saved = localStorage.getItem('edgeagent_session');
    const exists = state.sessions.some((s) => s.session_id === saved);
    if (saved && exists) {
      state.sessionId = saved;
      await loadHistory(saved);
    } else if (state.sessions.length) {
      state.sessionId = state.sessions[0].session_id;
      localStorage.setItem('edgeagent_session', state.sessionId);
      await loadHistory(state.sessionId);
    } else {
      await newSession();
    }
    applyMode(state.mode, true);
    await refreshState();
    setOnline(true);
  }

  function bind() {
    $('gate-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const value = $('gate-token').value.trim();
      if (!value) {
        $('gate-error').textContent = '请输入 token';
        return;
      }
      state.token = value;
      sessionStorage.setItem('edgeagent_token', state.token);
      $('gate-error').textContent = '';
      await boot();
    });

    $('composer').addEventListener('submit', (event) => {
      event.preventDefault();
      send($('input').value);
    });

    $('input').addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        send($('input').value);
      }
    });
    $('input').addEventListener('input', autoGrow);

    document.querySelectorAll('.mode-btn').forEach((btn) => {
      btn.addEventListener('click', () => switchMode(btn.dataset.mode));
    });

    $('btn-abort').addEventListener('click', abort);

    // 固定确认条（待确认时一定可见，不依赖聊天流里的卡片）
    $('pending-approve').addEventListener('click', () => decidePending(true));
    $('pending-reject').addEventListener('click', () => decidePending(false));

    // ---- AI 服务商面板 ----
    $('btn-provider').addEventListener('click', (event) => {
      event.stopPropagation();
      const panel = $('provider-panel');
      if (!panel.hidden) {
        panel.hidden = true;
        return;
      }
      const rect = $('btn-provider').getBoundingClientRect();
      const width = 388;
      panel.style.left = Math.max(10, Math.min(rect.left, window.innerWidth - width - 12)) + 'px';
      panel.style.top = Math.round(rect.bottom + 6) + 'px';
      panel.hidden = false;
      renderProviderList();
    });
    $('pp-close').addEventListener('click', () => {
      $('provider-panel').hidden = true;
    });
    $('provider-panel').addEventListener('click', (event) => event.stopPropagation());
    document.addEventListener('click', () => {
      $('provider-panel').hidden = true;
    });
    document.querySelectorAll('.pp-tab').forEach((tab) => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.pp-tab').forEach((t) => t.classList.remove('is-active'));
        tab.classList.add('is-active');
        providerState.kind = tab.dataset.kind;
        setProviderStatus('');
        renderProviderList();
      });
    });
    $('pp-test').addEventListener('click', testProvider);
    $('pp-web-login').addEventListener('click', webLogin);
    $('btn-new').addEventListener('click', newSession);
    $('btn-reset').addEventListener('click', resetSession);

    $('btn-panel').addEventListener('click', () => {
      document.querySelector('.layout').classList.toggle('panel-collapsed');
      refreshState();
    });
    $('btn-panel-close').addEventListener('click', () => {
      document.querySelector('.layout').classList.add('panel-collapsed');
    });

    $('modal-check').addEventListener('change', () => {
      $('modal-ok').disabled = !$('modal-check').checked;
    });
    $('modal-cancel').addEventListener('click', () => closeModal(false));
    $('modal-ok').addEventListener('click', () => closeModal(true));

    window.addEventListener('online', () => ping());
    window.addEventListener('offline', () => setOnline(false));
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) {
        ping();
        refreshState();
      }
    });
  }

  bind();
  ping();
  setInterval(ping, 5000);
  setInterval(refreshState, 8000);
  boot();
})();
